"""缺集管理 API - 扫描 Emby 剧集缺口并对接 MP / 影巢补齐。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import settings
from app.database import (
    add_subscribe_log,
    add_gap_ignore_episode,
    add_gap_ignore_season,
    add_gap_ignore_series,
    get_gap_config,
    get_gap_ignore_targets,
    list_gap_transfer_marks,
    list_gap_ignores,
    load_gap_cache,
    make_gap_episode_target_id,
    make_gap_season_target_id,
    remove_gap_ignore,
    remove_gap_ignores,
    save_gap_cache,
    save_gap_config,
    save_gap_transfer_mark,
)
from app.services.emby import EmbyClient
from app.services.hdhive import HDHiveClient
from app.services.moviepilot import MoviePilotClient
from app.services.telegram import TelegramNotifier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gaps", tags=["缺集管理"])

_scan_task: asyncio.Task | None = None
_refresh_lock = asyncio.Lock()
_scan_status: dict[str, Any] = {
    "is_scanning": False,
    "progress": 0,
    "total": 0,
    "processed": 0,
    "current_item": "",
    "results": [],
    "summary": {},
    "error": "",
    "started_at": "",
    "finished_at": "",
    "created_at": "",
    "skipped_libraries": [],
}

GAP_TRANSFER_SUCCESS_STATUSES = {"transferred", "already_owned", "success", "ok"}
GAP_RESOURCE_MARK_STATUSES = GAP_TRANSFER_SUCCESS_STATUSES | {"unlocked"}
OFFICIAL_GROUP_KEYWORDS = ("官组", "官方", "hiveweb", "hhweb", "hdsweb")


def _gap_scan_summary(
    results: list[dict],
    tv_libraries: list[dict],
    scannable_libraries: list[tuple[dict, int]],
    skipped_libraries: list[dict],
    processed: int,
    *,
    total: int = 0,
    partial: bool = False,
) -> dict:
    skipped_ids = {str(item.get("id") or "") for item in skipped_libraries}
    scannable_ids = {str(lib.get("ItemId") or "") for lib, _ in scannable_libraries}
    return {
        "series_count": len(results),
        "gap_count": sum(_safe_int(item.get("gap_count"), 0) for item in results),
        "library_count": len(tv_libraries),
        "scanned_library_count": len(scannable_ids - skipped_ids),
        "skipped_library_count": len(skipped_libraries),
        "skipped_libraries": skipped_libraries,
        "processed": processed,
        "total": total,
        "partial": partial,
    }


class GapConfigPayload(BaseModel):
    excluded_libraries: list[str] | str = Field(default_factory=list)
    cache_interval_hours: int | str = 6
    client_type: str = ""
    client_url: str = ""
    client_user: str = ""
    client_pass: str = ""


class GapEpisodePayload(BaseModel):
    series_id: str
    series_name: str = ""
    season_number: int = 0
    episode_number: int = 0


class GapSeriesPayload(BaseModel):
    series_id: str = ""
    series_name: str = ""
    series_ids: list[str] = Field(default_factory=list)
    items: list[dict] = Field(default_factory=list)


class GapRefreshSeriesPayload(BaseModel):
    series_id: str


class GapUnignorePayload(BaseModel):
    id: str = ""
    type: str = ""
    ids: list[str] = Field(default_factory=list)
    target: str = ""


class GapTargetPayload(BaseModel):
    season: int = 0
    episodes: list[int] = Field(default_factory=list)
    season_missing: bool = False


class GapSearchPayload(BaseModel):
    series_id: str = ""
    series_name: str
    year: int | str = ""
    season: int = 1
    episodes: list[int] = Field(default_factory=list)
    targets: list[GapTargetPayload] = Field(default_factory=list)
    tmdb_id: int | str = 0
    imdb_id: str = ""
    library_name: str = ""
    type: str = "tv"
    full_series: bool = False


class GapDownloadPayload(BaseModel):
    result: dict = Field(default_factory=dict)
    torrent_url: str = ""
    tmdb_id: int | str = 0
    folder_id: str = ""
    slug: str = ""
    series_id: str = ""
    series_name: str = ""
    season: int = 0
    episodes: list[int] = Field(default_factory=list)
    targets: list[GapTargetPayload] = Field(default_factory=list)
    full_series: bool = False
    force_transfer: bool = False
    force_mismatch: bool = False


class GapMoviePilotSubscribePayload(BaseModel):
    series_id: str = ""
    series_name: str
    year: int | str = ""
    season: int = 0
    episodes: list[int] = Field(default_factory=list)
    targets: list[GapTargetPayload] = Field(default_factory=list)
    tmdb_id: int | str = 0


class GapDownloadStatusPayload(BaseModel):
    results: list[dict] = Field(default_factory=list)


def _success(data: Any = None, message: str = "") -> dict:
    payload = {"status": "success", "data": data}
    if message:
        payload["message"] = message
    return payload


def _error(message: str, data: Any = None) -> dict:
    return {"status": "error", "message": message, "data": data}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _gap_result_size_bytes(result: dict) -> int:
    """提取影巢/MP 资源体积，用于缺集候选排序。"""
    size = result.get("size")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    text = " ".join(
        str(result.get(key) or "")
        for key in ("share_size", "size_text", "title", "name", "remark")
    )
    match = re.search(r"(\d+(?:\.\d+)?)\s*(tib|tb|gib|gb|g|mib|mb|m|kib|kb|k)", text, re.I)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {
        "tib": 1024**4,
        "tb": 1024**4,
        "gib": 1024**3,
        "gb": 1024**3,
        "g": 1024**3,
        "mib": 1024**2,
        "mb": 1024**2,
        "m": 1024**2,
        "kib": 1024,
        "kb": 1024,
        "k": 1024,
    }
    return int(value * multipliers.get(unit, 1))


def _is_official_gap_resource(result: dict) -> bool:
    """识别影巢官组资源，优先使用 API 字段，兼容备注/组名兜底。"""
    for key in ("is_official", "official", "ui_is_official"):
        value = result.get(key)
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in {"1", "true", "yes", "y"}:
            return True
    text_parts: list[str] = []
    for key in ("group", "release_group", "sharer", "title", "name", "remark"):
        value = result.get(key)
        if value:
            text_parts.append(str(value))
    source = result.get("source")
    if isinstance(source, list):
        text_parts.extend(str(x) for x in source if x)
    elif source:
        text_parts.append(str(source))
    text = " ".join(text_parts).lower()
    return any(keyword in text for keyword in OFFICIAL_GROUP_KEYWORDS)


def _telegram_code(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("`", "'").strip()
    if len(text) > limit:
        text = f"{text[:limit - 1]}…"
    return f"`{text}`" if text else ""


def _gap_episode_summary(season: int, episodes: list[int]) -> str:
    nums = sorted({_safe_int(ep, 0) for ep in episodes if _safe_int(ep, 0) > 0})
    if not nums:
        return f"S{season:02d}"
    if len(nums) > 1 and nums == list(range(nums[0], nums[-1] + 1)):
        return f"S{season:02d} E{nums[0]:02d}-E{nums[-1]:02d}"
    return f"S{season:02d} " + ", ".join(f"E{num:02d}" for num in nums[:12])


def _normalise_gap_targets(
    season: int = 0,
    episodes: list[int] | None = None,
    targets: list[Any] | None = None,
) -> list[dict]:
    grouped: dict[int, set[int]] = defaultdict(set)
    season_only: set[int] = set()
    for target in targets or []:
        if isinstance(target, BaseModel):
            raw = target.model_dump()
        elif isinstance(target, dict):
            raw = target
        else:
            continue
        target_season = _safe_int(raw.get("season"), -1)
        if target_season < 0:
            continue
        raw_episodes = raw.get("episodes") or []
        if bool(raw.get("season_missing") or raw.get("season_only") or raw.get("full_season")) and not raw_episodes:
            season_only.add(target_season)
            grouped.setdefault(target_season, set())
        for ep in raw_episodes:
            ep_num = _safe_int(ep, 0)
            if ep_num > 0:
                grouped[target_season].add(ep_num)
    fallback_season = _safe_int(season, -1)
    if fallback_season >= 0:
        for ep in episodes or []:
            ep_num = _safe_int(ep, 0)
            if ep_num > 0:
                grouped[fallback_season].add(ep_num)
    return [
        {
            "season": item_season,
            "episodes": sorted(item_episodes),
            "season_missing": item_season in season_only and not item_episodes,
        }
        for item_season, item_episodes in sorted(grouped.items())
        if item_episodes or item_season in season_only
    ]


def _gap_targets_summary(targets: list[dict]) -> str:
    if not targets:
        return "全部缺失"
    if len(targets) == 1:
        target = targets[0]
        if bool(target.get("season_missing")) and not target.get("episodes"):
            return f"S{_safe_int(target.get('season'), 0):02d} 整季"
        return _gap_episode_summary(target["season"], target["episodes"])
    parts = [
        f"S{_safe_int(target.get('season'), 0):02d} 整季"
        if bool(target.get("season_missing")) and not target.get("episodes")
        else _gap_episode_summary(target["season"], target["episodes"])
        for target in targets[:4]
    ]
    extra = len(targets) - len(parts)
    return " / ".join(parts) + (f" / +{extra}季" if extra > 0 else "")


def _full_series_episode_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    patterns = [
        r"(?<![a-z0-9])s0?\d{1,2}[ ._-]*e0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*(?:e|ep)?0*(\d{1,4}))?(?!\d)",
        r"(?<![a-z0-9])(?:e|ep|episode)[ ._-]*0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*(?:e|ep)?0*(\d{1,4}))?(?!\d)",
        r"第\s*0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*0*(\d{1,4}))?\s*[集期话話]",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            start = _safe_int(match.group(1), 0)
            end = _safe_int(match.group(2), start)
            if start > 0:
                spans.append((min(start, end), max(start, end)))
    return spans


def _is_full_series_gap_resource(result: dict) -> tuple[bool, str]:
    text = _normalise_title(_result_match_text(result))
    if not text:
        return False, "未知资源"

    pack_signal = bool(
        _season_collection_count(text)
        or re.search(
            r"全集|合集|整季|全季|全\s*\d{1,4}\s*[集期话話]|"
            r"complete|full[ ._-]*season|season[ ._-]*pack|batch|collection",
            text,
            re.I,
        )
    )
    season_signal = bool(
        re.search(r"(?<![a-z0-9])s0?\d{1,2}(?![ ._-]*(?:e|ep)\d)(?![a-z0-9])", text)
        or re.search(r"(?<![a-z0-9])season[ ._-]*\d{1,2}(?![ ._-]*(?:e|ep)\d)(?![a-z0-9])", text)
        or re.search(r"第\s*\d{1,2}\s*季", text)
        or re.search(r"(?<![a-z0-9])specials?(?![a-z0-9])", text)
    )
    episode_spans = _full_series_episode_spans(text)
    broad_episode_ranges = [
        (start, end)
        for start, end in episode_spans
        if start <= 1 and end - start + 1 >= 6
    ]
    has_broad_episode_range = bool(broad_episode_ranges)
    has_single_episode = any(
        start == end and not any(range_start <= start <= range_end for range_start, range_end in broad_episode_ranges)
        for start, end in episode_spans
    )
    ongoing_pack_signal = bool(re.search(r"(?:更新到|更新至|更到|更至|至|到)\s*0*\d{1,4}\s*[集期话話]", text))

    if has_single_episode and not (pack_signal or has_broad_episode_range or ongoing_pack_signal):
        return False, "单集资源"
    if pack_signal or season_signal or has_broad_episode_range or ongoing_pack_signal:
        return True, "全集"
    if episode_spans or has_single_episode:
        return False, "单集资源"
    return False, "非全集"


def _mark_full_series_gap_result(result: dict) -> dict:
    item = dict(result)
    is_full_series, label = _is_full_series_gap_resource(item)
    item["ui_episode_match_ratio"] = 1 if is_full_series else 0
    item["ui_episode_match_kind"] = "full_series" if is_full_series else "none"
    item["ui_episode_match_text"] = "全集" if is_full_series else label
    item["ui_full_series"] = is_full_series
    if is_full_series:
        item["ui_download_mode"] = "normal"
    else:
        item["ui_download_blocked"] = True
        item["ui_download_block_reason"] = f"{label}，全集重下不会提交"
    return item


def _download_gap_targets_from_result(result: dict, targets: list[dict], series_name: str = "") -> tuple[str, list[dict], list[dict]]:
    annotated_result = _annotate_gap_match_for_targets(result, targets, series_name=series_name) if targets else dict(result)
    matched_targets = annotated_result.get("ui_gap_targets")
    if not isinstance(matched_targets, list) or not matched_targets:
        matched_targets = result.get("ui_gap_targets") if isinstance(result.get("ui_gap_targets"), list) else []

    if not matched_targets and str(annotated_result.get("ui_episode_match_kind") or "") != "none":
        matched_targets = [
            {
                "season": annotated_result.get("ui_target_season"),
                "episodes": annotated_result.get("ui_target_episodes") or [],
                "matched_episodes": annotated_result.get("ui_matched_episodes") or [],
                "file_season": annotated_result.get("ui_file_season") or annotated_result.get("ui_target_season"),
                "match_kind": annotated_result.get("ui_episode_match_kind"),
            }
        ]

    display_grouped: dict[int, set[int]] = defaultdict(set)
    file_grouped: dict[int, set[int]] = defaultdict(set)
    display_season_only: set[int] = set()
    file_season_only: set[int] = set()
    kinds: set[str] = set()
    for target in matched_targets or []:
        if not isinstance(target, dict):
            continue
        match_kind = str(target.get("match_kind") or "").strip()
        if not match_kind or match_kind == "none":
            continue
        display_season = _safe_int(target.get("season"), -1)
        file_season = _safe_int(target.get("file_season"), display_season)
        raw_episodes = target.get("matched_episodes") or target.get("episodes") or []
        episodes = sorted({_safe_int(ep, 0) for ep in raw_episodes if _safe_int(ep, 0) > 0})
        season_missing = bool(target.get("season_missing")) and not episodes
        if display_season < 0 or file_season < 0 or (not episodes and not season_missing):
            continue
        kinds.add(match_kind)
        if season_missing:
            display_grouped.setdefault(display_season, set())
            file_grouped.setdefault(file_season, set())
            display_season_only.add(display_season)
            file_season_only.add(file_season)
        else:
            display_grouped[display_season].update(episodes)
            file_grouped[file_season].update(episodes)

    display_targets = [
        {
            "season": item_season,
            "episodes": sorted(item_episodes),
            "season_missing": item_season in display_season_only and not item_episodes,
        }
        for item_season, item_episodes in sorted(display_grouped.items())
        if item_episodes or item_season in display_season_only
    ]
    file_targets = [
        {
            "season": item_season,
            "episodes": sorted(item_episodes),
            "season_missing": item_season in file_season_only and not item_episodes,
        }
        for item_season, item_episodes in sorted(file_grouped.items())
        if item_episodes or item_season in file_season_only
    ]
    if not display_targets and targets:
        display_targets = targets
    if not file_targets and display_targets:
        file_targets = display_targets

    if not kinds:
        match_kind = str(annotated_result.get("ui_episode_match_kind") or "")
    elif kinds.intersection({"season_pack", "episode_pack"}):
        match_kind = "season_pack" if "season_pack" in kinds else "episode_pack"
    else:
        match_kind = "episode"
    return match_kind, display_targets, file_targets


def _is_season_only_gap_target(target: dict | None) -> bool:
    if not isinstance(target, dict):
        return False
    return bool(target.get("season_missing")) and not target.get("episodes")


def _is_season_only_gap_match(item: dict, targets: list[dict]) -> bool:
    if str(item.get("ui_episode_match_kind") or "") != "season_pack":
        return False
    season_only = {
        _safe_int(target.get("season"), -1)
        for target in targets
        if _is_season_only_gap_target(target)
    }
    if not season_only:
        return False
    matched_targets = item.get("ui_gap_targets")
    if isinstance(matched_targets, list):
        for target in matched_targets:
            if (
                isinstance(target, dict)
                and _safe_int(target.get("season"), -1) in season_only
                and not target.get("episodes")
            ):
                return True
    return _safe_int(item.get("ui_target_season"), -1) in season_only and not item.get("ui_target_episodes")


def _gap_result_title(result: dict) -> str:
    return (
        result.get("title")
        or result.get("name")
        or result.get("resource_name")
        or result.get("resource_title")
        or result.get("share_name")
        or ""
    )


def _gap_history_item_id(payload: GapDownloadPayload) -> str:
    series_id = str(payload.series_id or "").strip()
    tmdb_id = _safe_int(payload.tmdb_id, 0)
    if series_id:
        target = f"series:{series_id}"
    elif tmdb_id > 0:
        target = f"tmdb:{tmdb_id}"
    else:
        name = re.sub(r"[^a-z0-9]+", "-", str(payload.series_name or "").strip().lower()).strip("-")
        target = f"name:{name or 'unknown'}"
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    if targets:
        target_key = "|".join(
            f"s{_safe_int(item.get('season'), 0):02d}:"
            + "-".join(f"{episode:02d}" for episode in item.get("episodes", []))
            for item in targets
        )
        return f"gap:{target}:{target_key}"
    return f"gap:{target}:all"


def _gap_history_item_name(payload: GapDownloadPayload) -> str:
    series_name = str(payload.series_name or "").strip() or "缺集资源"
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    if bool(payload.full_series) and not targets:
        episode_text = "全集重下"
    else:
        episode_text = _gap_targets_summary(targets) if targets else _gap_episode_summary(_safe_int(payload.season, 0), payload.episodes)
    return f"{series_name} {episode_text}".strip()


def _gap_subscribe_history_item_id(payload: GapMoviePilotSubscribePayload, targets: list[dict]) -> str:
    series_id = str(payload.series_id or "").strip()
    tmdb_id = _safe_int(payload.tmdb_id, 0)
    if series_id:
        target = f"series:{series_id}"
    elif tmdb_id > 0:
        target = f"tmdb:{tmdb_id}"
    else:
        name = re.sub(r"[^a-z0-9]+", "-", str(payload.series_name or "").strip().lower()).strip("-")
        target = f"name:{name or 'unknown'}"
    if targets:
        target_key = "|".join(
            f"s{_safe_int(item.get('season'), 0):02d}:"
            + "-".join(f"{episode:02d}" for episode in item.get("episodes", []))
            for item in targets
        )
        return f"gap-subscribe:{target}:{target_key}"
    return f"gap-subscribe:{target}:all"


def _gap_subscribe_history_item_name(payload: GapMoviePilotSubscribePayload, targets: list[dict]) -> str:
    series_name = str(payload.series_name or "").strip() or "缺集订阅"
    return f"{series_name} {_gap_targets_summary(targets)}".strip()


def _mp_subscription_best_version_enabled(subscription: dict | None) -> bool:
    if not isinstance(subscription, dict):
        return False
    return (
        _safe_int(subscription.get("best_version"), 0) > 0
        or _safe_int(subscription.get("best_version_full"), 0) > 0
        or _safe_int(subscription.get("current_priority"), 0) > 0
        or bool(subscription.get("episode_priority"))
    )


async def _disable_mp_gap_best_version(mp: MoviePilotClient, subscription: dict | None) -> tuple[bool, str]:
    """缺集订阅必须保持普通订阅，避免 MP 洗版按全集包逻辑处理。"""
    if not _mp_subscription_best_version_enabled(subscription):
        return False, ""
    resp = await mp.disable_subscription_best_version(subscription or {})
    if resp and resp.get("success"):
        return True, ""
    message = resp.get("message", "关闭洗版失败") if isinstance(resp, dict) else "MoviePilot API 无响应"
    return False, message


def _mp_gap_service_unavailable(mp: MoviePilotClient, message: str = "") -> bool:
    if getattr(mp, "last_status_code", 0) in {502, 503, 504}:
        return True
    return "MoviePilot 服务暂不可用" in str(message or "")


def _gap_transfer_status_label(status: str) -> str:
    return {
        "unlocked": "已解锁",
        "transferred": "已转存",
        "already_owned": "已在 115",
        "submitted": "已提交转存",
        "success": "已转存",
        "ok": "已转存",
        "not_115": "非 115 资源",
        "password_error": "分享密码异常",
        "error": "失败",
    }.get(str(status or "").strip().lower(), str(status or "").strip() or "未知状态")


def _gap_history_message(prefix: str, payload: GapDownloadPayload, result: dict | None = None, status: str = "", extra: str = "") -> str:
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    target_text = "全集重下" if bool(payload.full_series) and not targets else (
        _gap_targets_summary(targets) if targets else _gap_episode_summary(_safe_int(payload.season, 0), payload.episodes)
    )
    parts = [prefix, target_text]
    title = _gap_result_title(result or payload.result or {})
    if title:
        parts.append(f"资源: {title}")
    if status:
        parts.append(f"状态: {_gap_transfer_status_label(status)}")
    if extra:
        parts.append(extra)
    return " · ".join(part for part in parts if part)


def _normalize_gap_resource_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))
    return text.strip("/")


def _gap_resource_key_candidates(result: dict) -> list[str]:
    keys: list[str] = []
    for field in ("slug", "resource_url", "page_url", "url", "id", "hdhive_slug"):
        value = result.get(field)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        key = _normalize_gap_resource_key(value)
        if key and key not in keys:
            keys.append(key)
    return keys


def _gap_transfer_status(resp: dict | None) -> str:
    if not isinstance(resp, dict):
        return ""
    status = str(resp.get("status") or "").strip().lower()
    if status:
        return status
    data = resp.get("data")
    if isinstance(data, dict):
        nested_status = str(data.get("status") or "").strip().lower()
        if nested_status:
            return nested_status
        if data.get("success") is True:
            return "success"
    if resp.get("success") is True:
        return "success"
    return ""


def _is_gap_transfer_success_status(status: str) -> bool:
    return str(status or "").strip().lower() in GAP_TRANSFER_SUCCESS_STATUSES


def _is_gap_resource_mark_status(status: str) -> bool:
    return str(status or "").strip().lower() in GAP_RESOURCE_MARK_STATUSES


async def _refresh_clouddrive_after_gap_transfer(status: str) -> dict:
    """影巢转存完成后，让 CD2 重新读取 115 挂载目录；失败不影响转存结果。"""
    if not _is_gap_transfer_success_status(status):
        return {"ok": False, "skipped": True, "message": "状态无需刷新 CloudDrive2"}

    from app.services.clouddrive import CloudDriveClient

    cd2 = CloudDriveClient()
    try:
        result = await cd2.force_expire_dir_cache()
    finally:
        await cd2.close()

    if result.get("skipped"):
        logger.info("[Gaps] CloudDrive2 未配置，跳过目录缓存刷新")
    elif result.get("ok"):
        logger.info("[Gaps] CloudDrive2 目录缓存刷新成功")
    else:
        logger.warning("[Gaps] CloudDrive2 目录缓存刷新失败: %s", result.get("message", "未知错误"))
    return result


def _gap_target_brief(targets: list[dict]) -> str:
    if not targets:
        return "目标缺口"
    return _gap_targets_summary(targets)


def _season_number_from_text(text: str) -> int:
    value = str(text or "")
    for pattern in (
        r"(?<![a-z0-9])s0?(\d{1,2})(?![a-z0-9])",
        r"season[ ._-]*0?(\d{1,2})",
        r"第\s*0?(\d{1,2})\s*季",
    ):
        match = re.search(pattern, value, re.I)
        if match:
            return _safe_int(match.group(1), -1)
    return -1


def _season_episode_from_text(text: str, fallback_season: int = -1) -> tuple[int, int]:
    value = str(text or "")
    match = re.search(r"(?<![a-z0-9])s0?(\d{1,2})[ ._-]*e0?(\d{1,4})(?!\d)", value, re.I)
    if match:
        return _safe_int(match.group(1), -1), _safe_int(match.group(2), -1)
    match = re.search(r"(?<![a-z0-9])e0?(\d{1,4})(?!\d)", value, re.I)
    if match and fallback_season >= 0:
        return fallback_season, _safe_int(match.group(1), -1)
    return fallback_season, -1


async def _cloud115_collect_seasons(c115, folder_id: str, depth: int = 0, season_hint: int = -1) -> tuple[dict[int, set[int]], set[int], list[str], list[str]]:
    result = await c115.list_files(str(folder_id), limit=115)
    episodes_by_season: dict[int, set[int]] = defaultdict(set)
    seasons_with_content: set[int] = set()
    names: list[str] = []
    path_ids: list[str] = []
    if not isinstance(result, dict) or not result.get("state"):
        return episodes_by_season, seasons_with_content, names, path_ids

    for item in result.get("path") or []:
        if isinstance(item, dict) and item.get("cid"):
            path_ids.append(str(item.get("cid")))

    for item in result.get("data") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("n") or "")
        if name:
            names.append(name)
        is_folder = _safe_int(item.get("fc"), 1) == 0 or (item.get("cid") and not item.get("fid"))
        if is_folder:
            item_season = _season_number_from_text(name)
            next_hint = item_season if item_season >= 0 else season_hint
            if next_hint >= 0:
                seasons_with_content.add(next_hint)
            if depth < 3 and item.get("cid"):
                child_eps, child_seasons, child_names, _ = await _cloud115_collect_seasons(
                    c115,
                    str(item.get("cid")),
                    depth + 1,
                    next_hint,
                )
                names.extend(child_names[:20])
                seasons_with_content.update(child_seasons)
                for season, eps in child_eps.items():
                    episodes_by_season[season].update(eps)
            continue
        season, episode = _season_episode_from_text(name, season_hint)
        if season >= 0:
            seasons_with_content.add(season)
            if episode > 0:
                episodes_by_season[season].add(episode)
    return episodes_by_season, seasons_with_content, names, path_ids


def _cloud115_folder_satisfies_targets(
    episodes_by_season: dict[int, set[int]],
    seasons_with_content: set[int],
    targets: list[dict],
) -> bool:
    if not targets:
        return bool(seasons_with_content or any(episodes_by_season.values()))
    for target in targets:
        season = _safe_int(target.get("season"), -1)
        if season < 0:
            continue
        episodes = sorted({_safe_int(ep, 0) for ep in target.get("episodes") or [] if _safe_int(ep, 0) > 0})
        if episodes:
            if not set(episodes).issubset(episodes_by_season.get(season, set())):
                return False
        elif season not in seasons_with_content and not episodes_by_season.get(season):
            return False
    return True


def _cloud115_item_name(item: dict) -> str:
    return str(item.get("n") or item.get("file_name") or item.get("name") or "").strip()


def _cloud115_item_is_folder(item: dict) -> bool:
    return _safe_int(item.get("fc"), 1) == 0 or bool(item.get("cid") and not item.get("fid"))


def _gap_recover_search_terms(payload: GapDownloadPayload, result: dict) -> list[str]:
    terms: list[str] = []
    for value in (
        _gap_result_title(result),
        result.get("resource_title"),
        result.get("name"),
        payload.series_name,
    ):
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)
        simple = re.sub(r"[\(\（][^\)\）]*[\)\）]", "", text).strip()
        if simple and simple not in terms:
            terms.append(simple)
    return terms[:4]


def _gap_transfer_folder_name(payload: GapDownloadPayload, result: dict) -> str:
    title = _gap_result_title(result) or payload.series_name or "缺集转存"
    title = re.sub(r"[\r\n\t]+", " ", str(title or "")).strip()
    title = re.sub(r'[\\/:*?"<>|]+', " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    if len(title) > 80:
        title = title[:80].rstrip(" .")
    return title or "缺集转存"


async def _ensure_cloud115_child_folder(c115, parent_id: str, folder_name: str) -> dict:
    parent_id = str(parent_id or "0")
    folder_name = str(folder_name or "").strip()
    if not folder_name:
        return {"ok": True, "folder_id": parent_id, "folder_name": ""}

    async def find_existing() -> str:
        listing = await c115.list_files(parent_id, limit=115)
        if not isinstance(listing, dict) or not listing.get("state"):
            return ""
        for item in listing.get("data") or []:
            if not isinstance(item, dict) or not _cloud115_item_is_folder(item):
                continue
            if _cloud115_item_name(item) == folder_name:
                return str(item.get("cid") or "").strip()
        return ""

    existing_id = await find_existing()
    if existing_id:
        return {"ok": True, "folder_id": existing_id, "folder_name": folder_name, "existing": True}

    created = await c115.create_folder(folder_name, parent_id)
    data = created.get("data") if isinstance(created, dict) else {}
    folder_id = ""
    if isinstance(data, dict):
        folder_id = str(data.get("cid") or data.get("file_id") or data.get("id") or "").strip()
    if not folder_id and isinstance(created, dict):
        folder_id = str(created.get("cid") or created.get("file_id") or created.get("id") or "").strip()
    if isinstance(created, dict) and created.get("state") is True and folder_id:
        return {"ok": True, "folder_id": folder_id, "folder_name": folder_name, "data": created}

    # 115 在并发创建或同名目录存在时可能不直接返回 cid，再查一次。
    existing_id = await find_existing()
    if existing_id:
        return {"ok": True, "folder_id": existing_id, "folder_name": folder_name, "existing": True, "data": created}

    message = ""
    if isinstance(created, dict):
        message = created.get("error") or created.get("message") or ""
    return {"ok": False, "message": message or f"115 创建目录失败: {folder_name}", "data": created}


async def _verify_cloud115_transfer_target(c115, folder_id: str, targets: list[dict]) -> dict:
    episodes_by_season, seasons_with_content, names, _ = await _cloud115_collect_seasons(c115, folder_id)
    ok = _cloud115_folder_satisfies_targets(episodes_by_season, seasons_with_content, targets)
    return {
        "ok": ok,
        "episodes_by_season": {
            str(season): sorted(episodes)
            for season, episodes in sorted(episodes_by_season.items())
        },
        "seasons_with_content": sorted(seasons_with_content),
        "sample_names": names[:8],
        "message": "115 目标目录校验通过" if ok else "115 目标目录未找到匹配缺口文件",
    }


async def _wait_verify_cloud115_transfer_target(
    c115,
    folder_id: str,
    targets: list[dict],
    attempts: int = 5,
    delay: float = 2.0,
) -> dict:
    """115 转存可能异步落地，短轮询确认目标目录真的出现缺口文件。"""
    last_result: dict = {}
    for attempt in range(max(1, attempts)):
        last_result = await _verify_cloud115_transfer_target(c115, folder_id, targets)
        if last_result.get("ok"):
            if attempt:
                last_result["attempts"] = attempt + 1
            return last_result
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    last_result["attempts"] = attempts
    return last_result


def _iter_text_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_text_values(item)


def _share_item_name(item: dict) -> str:
    return str(item.get("n") or item.get("name") or item.get("file_name") or "").strip()


def _share_item_id(item: dict) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "").strip()


def _share_item_is_folder(item: dict) -> bool:
    return _safe_int(item.get("fc"), 1) == 0 or bool(item.get("cid") and not item.get("fid"))


def _share_item_parent_cid(item: dict) -> str:
    return str(item.get("__share_parent_cid") or "0").strip() or "0"


def _share_item_episode(name: str) -> tuple[int, int] | None:
    match = re.search(r"(?<![a-z0-9])s0?(\d{1,2})[ ._-]*e0?(\d{1,4})(?!\d)", str(name or ""), re.I)
    if not match:
        return None
    return _safe_int(match.group(1), -1), _safe_int(match.group(2), -1)


def _share_item_matches_gap_targets(item: dict, targets: list[dict], full_series: bool = False) -> bool:
    if full_series or not targets:
        return True
    name = _share_item_name(item)
    episode = _share_item_episode(name)
    season_from_name = _season_number_from_text(name)
    for target in targets:
        season = _safe_int(target.get("season"), -1)
        if season < 0:
            continue
        episodes = sorted({_safe_int(ep, 0) for ep in target.get("episodes") or [] if _safe_int(ep, 0) > 0})
        if episode:
            item_season, item_episode = episode
            if item_season != season:
                continue
            if not episodes or item_episode in episodes:
                return True
        elif not episodes and season_from_name == season:
            return True
    return False


def _select_share_items_for_gap_targets(items: list[dict], targets: list[dict], full_series: bool = False) -> list[dict]:
    """Choose the smallest set of share items to receive, preferring folders over their children."""
    valid_items = [item for item in items if isinstance(item, dict)]
    if full_series or not targets:
        root_items = [item for item in valid_items if _share_item_parent_cid(item) == "0"]
        return root_items or valid_items

    season_only = {
        _safe_int(target.get("season"), -1)
        for target in targets
        if _is_season_only_gap_target(target)
    }
    selected: list[dict] = []
    selected_ids: set[str] = set()
    covered_seasons: set[int] = set()

    for item in valid_items:
        if not _share_item_is_folder(item):
            continue
        season = _season_number_from_text(_share_item_name(item))
        if season in season_only:
            item_id = _share_item_id(item)
            if item_id and item_id not in selected_ids:
                selected.append(item)
                selected_ids.add(item_id)
                covered_seasons.add(season)

    for item in valid_items:
        if _share_item_is_folder(item):
            continue
        episode = _share_item_episode(_share_item_name(item))
        if episode and episode[0] in covered_seasons:
            continue
        if not _share_item_matches_gap_targets(item, targets, full_series=False):
            continue
        item_id = _share_item_id(item)
        if item_id and item_id not in selected_ids:
            selected.append(item)
            selected_ids.add(item_id)

    if selected:
        return selected
    return [
        item
        for item in valid_items
        if _share_item_matches_gap_targets(item, targets, full_series=False)
    ]


async def _receive_already_owned_share_files(
    c115,
    payload: GapDownloadPayload,
    result: dict,
    transfer_resp: dict | None,
    folder_id: str,
    targets: list[dict],
) -> dict:
    """普通整包转存被 115 判重时，改用分享内 file_id 精准重新接收缺口文件。"""
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for text in _iter_text_values({"result": result, "transfer": transfer_resp or {}, "slug": payload.slug}):
        parsed = c115.extract_share_code(text)
        if not parsed:
            continue
        key = (str(parsed.get("share_code") or ""), str(parsed.get("receive_code") or ""))
        if key[0] and key not in seen:
            seen.add(key)
            candidates.append(parsed)
    if not candidates:
        return {"ok": False, "skipped": True, "message": "影巢返回体里没有可用于重新接收的 115 分享链接"}

    target_text = "全集资源" if payload.full_series and not targets else _gap_target_brief(targets)
    last_message = ""
    for parsed in candidates:
        share_code = parsed.get("share_code") or ""
        receive_code = parsed.get("receive_code") or ""
        files = await c115.list_share_files(share_code, receive_code, recursive=True)
        if not files.get("state"):
            last_message = files.get("error") or "115 分享快照读取失败"
            continue
        matched_items = _select_share_items_for_gap_targets(
            files.get("data") or [],
            targets,
            full_series=payload.full_series,
        )
        file_ids = []
        for item in matched_items:
            item_id = _share_item_id(item)
            if item_id and item_id not in file_ids:
                file_ids.append(item_id)
        if not file_ids:
            last_message = f"115 分享内没有匹配 {target_text} 的文件"
            continue

        folder_result = await _ensure_cloud115_child_folder(
            c115,
            folder_id,
            _gap_transfer_folder_name(payload, result),
        )
        if not folder_result.get("ok"):
            return folder_result
        destination_id = str(folder_result.get("folder_id") or folder_id)
        destination_name = str(folder_result.get("folder_name") or "")

        received_count = 0
        chunk_results: list[dict] = []
        failed_messages: list[str] = []
        for offset in range(0, len(file_ids), 50):
            chunk = file_ids[offset:offset + 50]
            receive_result = await c115.transfer_from_share(
                share_code,
                receive_code,
                destination_id,
                file_id=",".join(chunk),
            )
            state = isinstance(receive_result, dict) and receive_result.get("state") is True
            data = receive_result.get("data") if isinstance(receive_result, dict) else {}
            chunk_results.append({
                "state": state,
                "errno": receive_result.get("errno") if isinstance(receive_result, dict) else None,
                "error": (receive_result.get("error") or receive_result.get("message") or "")
                if isinstance(receive_result, dict) else "115 API 无响应",
                "file_count": len(chunk),
                "recv_file_count": data.get("recv_file_count") if isinstance(data, dict) else None,
            })
            if state:
                recv_count = data.get("recv_file_count") if isinstance(data, dict) else 0
                received_count += _safe_int(recv_count, 0) or len(chunk)
            else:
                message = chunk_results[-1]["error"] or "115 重新接收失败"
                failed_messages.append(str(message))
        if received_count > 0:
            verify_result = await _verify_cloud115_transfer_target(c115, destination_id, targets)
            if not verify_result.get("ok"):
                return {
                    "ok": False,
                    "message": verify_result.get("message") or f"115 已接收但未在目标目录校验到 {target_text}",
                    "folder_id": destination_id,
                    "folder_name": destination_name,
                    "data": chunk_results,
                    "verify": verify_result,
                }
            return {
                "ok": True,
                "mode": "share_receive",
                "message": (
                    f"已从 115 分享重新接收 {target_text} 到"
                    f"「{destination_name}」目录（{received_count} 个文件）"
                    if destination_name
                    else f"已从 115 分享重新接收 {target_text}（{received_count} 个文件）"
                ),
                "file_count": received_count,
                "folder_id": destination_id,
                "folder_name": destination_name,
                "data": chunk_results,
                "verify": verify_result,
            }
        if failed_messages:
            last_message = "；".join(failed_messages[:3])
    return {"ok": False, "message": last_message or "115 分享重新接收失败"}


async def _recover_already_owned_gap_transfer(
    payload: GapDownloadPayload,
    result: dict,
    folder_id: str,
    transfer_resp: dict | None = None,
) -> dict:
    """115 拒绝重复接收时，尝试从账号内已有文件复制回目标目录。"""
    if not settings.cloud115_cookie:
        return {"ok": False, "skipped": True, "message": "未配置 115 Cookie，无法查找已接收文件"}

    from app.services.cloud115 import Cloud115Client

    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    target_text = _gap_target_brief(targets)
    c115 = Cloud115Client()
    try:
        share_result = await _receive_already_owned_share_files(
            c115,
            payload,
            result,
            transfer_resp,
            folder_id,
            targets,
        )
        if share_result.get("ok"):
            return share_result
        share_message = "" if share_result.get("skipped") else str(share_result.get("message") or "")

        checked: set[str] = set()
        for term in _gap_recover_search_terms(payload, result):
            search = await c115.search_files(term, limit=40)
            if not isinstance(search, dict) or not search.get("state"):
                continue
            for item in search.get("data") or []:
                if not isinstance(item, dict):
                    continue
                is_folder = _safe_int(item.get("fc"), 1) == 0 or (item.get("cid") and not item.get("fid"))
                source_id = str(item.get("cid") or item.get("fid") or "").strip()
                if not is_folder or not source_id or source_id in checked:
                    continue
                checked.add(source_id)
                episodes_by_season, seasons_with_content, _, path_ids = await _cloud115_collect_seasons(c115, source_id)
                if not _cloud115_folder_satisfies_targets(episodes_by_season, seasons_with_content, targets):
                    continue
                if str(folder_id) in path_ids:
                    return {
                        "ok": True,
                        "message": f"115 目标目录已有包含 {target_text} 的文件夹，已刷新 CD2",
                        "source_id": source_id,
                        "already_in_target": True,
                    }
                copy_result = await c115.copy_files([source_id], folder_id)
                if isinstance(copy_result, dict) and copy_result.get("state") is True:
                    return {
                        "ok": True,
                        "message": f"已从 115 账号内复制包含 {target_text} 的已有文件夹到转存目录",
                        "source_id": source_id,
                        "data": copy_result,
                    }
                if isinstance(copy_result, dict):
                    message = copy_result.get("error") or copy_result.get("message") or "115 复制失败"
                else:
                    message = "115 复制无响应"
                if isinstance(copy_result, dict) and _safe_int(copy_result.get("errno"), 0) == 990009:
                    return {
                        "ok": True,
                        "message": f"115 正在复制包含 {target_text} 的已有文件夹，请稍后查看 CD2",
                        "source_id": source_id,
                        "data": copy_result,
                    }
                return {"ok": False, "message": f"115 已找到已有文件夹，但复制失败: {message}", "data": copy_result}
        return {
            "ok": False,
            "message": (
                f"115 提示已转存过，但账号内未找到包含 {target_text} 的已有文件；"
                f"{share_message or '无法直接重新接收'}"
            ),
        }
    finally:
        await c115.close()


async def _notify_gap_hdhive_transfer(payload: GapDownloadPayload, resp: dict, ok: bool, message: str = ""):
    """发送缺集影巢解锁/转存结果；通知失败不影响业务接口。"""
    try:
        tg = TelegramNotifier()
        if not tg.is_configured:
            await tg.close()
            return

        status = _gap_transfer_status(resp)
        icon = "✅" if ok and status == "transferred" else ("ℹ️" if ok else "❌")
        status_label = {
            "unlocked": "已解锁",
            "transferred": "转存成功",
            "already_owned": "已在 115 中",
            "submitted": "已提交转存",
            "success": "转存成功",
            "ok": "转存成功",
            "not_115": "非 115 资源",
            "password_error": "分享密码异常",
            "error": "转存失败",
        }.get(status, message or status or "转存完成")

        result = payload.result or {}
        lines = [f"{icon} *缺集影巢解锁结果*", f"状态: *{status_label}*"]
        if payload.series_name:
            lines.append(f"剧集: {_telegram_code(payload.series_name)}")
        episode_text = _gap_episode_summary(_safe_int(payload.season, 0), payload.episodes)
        if episode_text:
            lines.append(f"缺集: `{episode_text}`")
        title = _gap_result_title(result)
        if title:
            lines.append(f"资源: {_telegram_code(title)}")
        size = result.get("share_size") or result.get("size") or ""
        if size:
            lines.append(f"大小: `{size}`")
        points = result.get("unlock_points")
        if points not in (None, ""):
            lines.append(f"积分: `{points}`")
        if message and not ok:
            lines.append(f"详情: {_telegram_code(message, 180)}")

        await tg.send_message("\n".join(lines))
        await tg.close()
    except Exception as e:
        logger.warning("[Gaps] Telegram 缺集转存通知失败: %s", e)


def _episode_target_id(series_id: str, season: int, episode: int) -> str:
    return make_gap_episode_target_id(series_id, season, episode)


def _season_target_id(series_id: str, season: int) -> str:
    return make_gap_season_target_id(series_id, season)


def _series_tmdb_id(series: dict) -> int:
    provider_ids = series.get("ProviderIds") or {}
    return _safe_int(provider_ids.get("Tmdb") or provider_ids.get("TmdbId"), 0)


def _tmdb_season_air_date(season: dict) -> str:
    return str(
        season.get("air_date")
        or season.get("AirDate")
        or season.get("first_air_date")
        or season.get("FirstAirDate")
        or ""
    ).strip()


def _tmdb_season_started(season: dict) -> bool:
    air_date = _tmdb_season_air_date(season)
    if not air_date:
        return False
    try:
        return datetime.fromisoformat(air_date[:10]).date() <= datetime.now().date()
    except ValueError:
        return False


def _tmdb_season_map(seasons: list[dict]) -> dict[int, dict]:
    season_map: dict[int, dict] = {}
    for season in seasons:
        season_num = _safe_int(season.get("season_number") or season.get("season"), 0)
        episode_count = _safe_int(
            season.get("episode_count")
            or season.get("episodeCount")
            or season.get("total_episode")
            or season.get("totalEpisode"),
            0,
        )
        if season_num <= 0 or episode_count <= 0 or not _tmdb_season_started(season):
            continue
        season_map[season_num] = season
    if len(season_map) > 80:
        return {}
    return season_map


async def _fetch_expected_season_map(
    mp: MoviePilotClient | None,
    series: dict,
    season_cache: dict[str, dict[int, dict]] | None = None,
    timeout: float | None = None,
) -> dict[int, dict]:
    tmdb_id = _series_tmdb_id(series)
    if tmdb_id <= 0 or not mp or not mp.is_configured:
        return {}
    cache_key = str(tmdb_id)
    if season_cache is not None and cache_key in season_cache:
        return season_cache[cache_key]

    season_map: dict[int, dict] = {}
    try:
        if timeout and timeout > 0:
            seasons = await asyncio.wait_for(mp.get_tmdb_seasons(tmdb_id), timeout=timeout)
        else:
            seasons = await mp.get_tmdb_seasons(tmdb_id)
        season_map = _tmdb_season_map(seasons)
    except asyncio.TimeoutError:
        logger.info(
            "[Gaps] 读取 TMDB 季信息超时，降级用 Emby 本地推断: %s tmdb=%s timeout=%.1fs",
            series.get("Name") or series.get("Id") or "",
            tmdb_id,
            timeout or 0,
        )
    except Exception as e:
        logger.info(
            "[Gaps] 读取 TMDB 季信息失败: %s tmdb=%s: %s",
            series.get("Name") or series.get("Id") or "",
            tmdb_id,
            e,
        )
    if season_cache is not None:
        season_cache[cache_key] = season_map
    return season_map


def _missing_season_numbers(
    season_numbers: set[int],
    series: dict,
    lib: dict,
    expected_seasons: set[int] | None = None,
) -> list[int]:
    existing_nums = {num for num in season_numbers if num > 0}
    nums = sorted(existing_nums)
    missing: set[int] = set()

    expected_nums = sorted(num for num in (expected_seasons or set()) if num > 0)
    if expected_nums:
        return [num for num in expected_nums if num not in existing_nums]

    # TMDB 季信息偶尔会读取失败。只有在缺少权威季列表时，才根据本地
    # 季号推断前置/中间缺季；避免把“TMDB 只有 S01、本地误入 S05”
    # 这类季号异常误判为 S02-S04 整季缺失。
    max_inferred_leading_missing = 8
    if not expected_nums and nums and 1 < nums[0] <= max_inferred_leading_missing + 1:
        missing.update(range(1, nums[0]))

    if len(nums) >= 2:
        inferred = [num for num in range(nums[0], nums[-1] + 1) if num not in existing_nums]
        if len(inferred) > 8:
            logger.info(
                "[Gaps] 跳过大跨度缺季推断: %s %s -> %s",
                series.get("Name") or series.get("Id") or "",
                nums,
                inferred,
            )
        else:
            missing.update(inferred)

    return sorted(missing)


def _gap_library_label(lib: dict) -> str:
    return lib.get("Name") or str(lib.get("ItemId") or "") or "剧集库"


def _gap_summary_from_results(results: list[dict], base: dict | None = None) -> dict:
    """根据当前缓存结果重算缺集统计，保留全库扫描相关计数。"""
    base = dict(base or {})
    library_ids = {str(item.get("library_id") or "") for item in results if item.get("library_id")}
    library_names = {str(item.get("library_name") or "") for item in results if item.get("library_name")}
    fallback_library_count = len(library_ids or library_names)
    return {
        **base,
        "series_count": len(results),
        "gap_count": sum(_safe_int(item.get("gap_count"), 0) for item in results),
        "library_count": _safe_int(base.get("library_count"), fallback_library_count),
        "processed": _safe_int(base.get("processed"), len(results)),
    }


def _gap_series_library_key(item: dict) -> str:
    return str(item.get("library_id") or item.get("library_name") or "").strip().lower()


def _normalise_gap_series_name(name: Any) -> str:
    text = str(name or "").strip().casefold()
    text = re.sub(r"[\s\u3000]+", " ", text)
    return text


def _gap_series_name_year_key(item: dict) -> str:
    name = _normalise_gap_series_name(item.get("series_name"))
    if not name:
        return ""
    year = str(item.get("year") or "").strip()
    return f"lib:{_gap_series_library_key(item)}|name:{name}|year:{year}"


def _gap_series_dedupe_keys(item: dict) -> list[str]:
    """同库同名同年优先视为同一剧，兼容 Emby ProviderIds 被刮出多套的情况。"""
    keys: list[str] = []
    name_year_key = _gap_series_name_year_key(item)
    if name_year_key:
        keys.append(name_year_key)
    library_key = str(item.get("library_id") or item.get("library_name") or "").strip().lower()
    tmdb_id = str(item.get("tmdb_id") or "").strip()
    if tmdb_id and tmdb_id != "0":
        keys.append(f"lib:{library_key}|tmdb:{tmdb_id}")
    imdb_id = str(item.get("imdb_id") or "").strip().lower()
    if imdb_id:
        keys.append(f"lib:{library_key}|imdb:{imdb_id}")
    if not keys:
        series_id = str(item.get("series_id") or "").strip()
        keys.append(f"lib:{library_key}|series:{series_id}")
    return keys


def _gap_series_rank(item: dict) -> tuple[int, int, int, int]:
    provider_score = int(bool(str(item.get("tmdb_id") or "").strip())) + int(bool(str(item.get("imdb_id") or "").strip()))
    return (
        _safe_int(item.get("gap_count"), 0),
        provider_score,
        -_safe_int(item.get("year"), 9999),
        -_safe_int(item.get("series_id"), 0),
    )


def _retarget_gap_id(series_id: str, gap: dict) -> dict:
    item = dict(gap)
    season = _safe_int(item.get("season"), 0)
    episode = _safe_int(item.get("episode"), 0)
    if bool(item.get("season_missing")) or episode <= 0:
        item["id"] = _season_target_id(series_id, season)
    else:
        item["id"] = _episode_target_id(series_id, season, episode)
    return item


def _is_non_tmdb_season_anomaly_gap(gap: dict) -> bool:
    """本地错误高季号造成的中间季推断，不应作为真实缺季参与统计。"""
    if not isinstance(gap, dict):
        return False
    if str(gap.get("anomaly_type") or "") != "season_number_mismatch":
        return False
    if str(gap.get("source") or "") != "season_inferred":
        return False
    season = _safe_int(gap.get("season"), -1)
    tmdb_seasons = {_safe_int(value, -1) for value in (gap.get("tmdb_seasons") or [])}
    return season > 0 and bool(tmdb_seasons) and season not in tmdb_seasons


def _normalise_gap_series_item(item: dict) -> dict:
    normalised = dict(item)
    gaps = [
        gap
        for gap in (normalised.get("gaps") or [])
        if isinstance(gap, dict)
        and not _is_non_tmdb_season_anomaly_gap(gap)
    ]
    normalised["gaps"] = gaps
    normalised["gap_count"] = len(gaps)
    return normalised


def _dedupe_gap_series_results(results: list[dict]) -> list[dict]:
    """合并同一媒体库内的重复 Emby Series，避免同名同年刮成多个 TMDB 后重复显示。"""
    grouped: dict[str, dict] = {}
    key_owner: dict[str, str] = {}
    group_keys: dict[str, set[str]] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        item = _normalise_gap_series_item(item)
        if not item.get("gaps"):
            continue
        keys = _gap_series_dedupe_keys(item)
        owners = [key_owner[key] for key in keys if key in key_owner and key_owner[key] in grouped]
        owner_key = owners[0] if owners else keys[0]
        if not owners:
            grouped[owner_key] = dict(item)
            group_keys[owner_key] = set(keys)
            for key in keys:
                key_owner[key] = owner_key
            continue

        for duplicate_owner in owners[1:]:
            if duplicate_owner == owner_key or duplicate_owner not in grouped:
                continue
            item = _merge_gap_series_item(grouped[owner_key], grouped[duplicate_owner])
            grouped[owner_key] = item
            for key in group_keys.get(duplicate_owner, set()):
                key_owner[key] = owner_key
                group_keys.setdefault(owner_key, set()).add(key)
            grouped.pop(duplicate_owner, None)
            group_keys.pop(duplicate_owner, None)

        existing = grouped[owner_key]
        keeper = _merge_gap_series_item(existing, item)
        grouped[owner_key] = keeper
        for key in set(keys) | set(_gap_series_dedupe_keys(keeper)):
            key_owner[key] = owner_key
            group_keys.setdefault(owner_key, set()).add(key)

    return list(grouped.values())


def _merge_gap_series_item(existing: dict, item: dict) -> dict:
    if _gap_series_rank(item) > _gap_series_rank(existing):
        keeper = dict(item)
        duplicate = existing
    else:
        keeper = dict(existing)
        duplicate = item

    for field in ("tmdb_id", "imdb_id", "year", "overview", "poster", "library_id", "library_name"):
        if not keeper.get(field) and duplicate.get(field):
            keeper[field] = duplicate.get(field)

    merged_ids = []
    for value in [
        keeper.get("series_id"),
        duplicate.get("series_id"),
        *(keeper.get("merged_series_ids") or []),
        *(duplicate.get("merged_series_ids") or []),
    ]:
        text = str(value or "").strip()
        if text and text not in merged_ids:
            merged_ids.append(text)

    keeper_id = str(keeper.get("series_id") or "")
    gap_map: dict[tuple[int, int, bool], dict] = {}
    for source in [keeper, duplicate]:
        for gap in source.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            season = _safe_int(gap.get("season"), 0)
            episode = _safe_int(gap.get("episode"), 0)
            season_missing = bool(gap.get("season_missing")) or episode <= 0
            gap_key = (season, 0 if season_missing else episode, season_missing)
            current = gap_map.get(gap_key)
            candidate = _retarget_gap_id(keeper_id, gap)
            if not current or (str(candidate.get("source") or "") == "tmdb" and str(current.get("source") or "") != "tmdb"):
                gap_map[gap_key] = candidate

    keeper["merged_series_ids"] = merged_ids
    keeper["gaps"] = sorted(gap_map.values(), key=lambda x: (_safe_int(x.get("season"), 0), _safe_int(x.get("episode"), 0)))
    keeper["gap_count"] = len(keeper["gaps"])
    return keeper


def _is_missing_emby_episode(item: dict) -> bool:
    location = str(item.get("LocationType") or "").lower()
    return bool(
        item.get("IsMissing")
        or item.get("MissingEpisode")
        or item.get("IsVirtualItem")
        or location == "virtual"
    )


def _clean_episode_title(name: str, season: int, episode: int) -> str:
    text = (name or "").strip()
    if text:
        return text
    return f"S{season:02d}E{episode:02d}"


def _episode_filename(path: Any) -> str:
    text = str(path or "").strip().rstrip("/\\")
    if not text:
        return ""
    if "\\" in text and ("/" not in text or re.match(r"^[a-zA-Z]:\\", text)):
        return PureWindowsPath(text).name
    return PurePosixPath(text.replace("\\", "/")).name


def _episode_media_sources(item: dict) -> list[dict]:
    sources: list[dict] = []
    for source in item.get("MediaSources") or []:
        if not isinstance(source, dict):
            continue
        source_path = str(source.get("Path") or "").strip()
        source_filename = _episode_filename(source_path)
        if not source_path and not source_filename:
            continue
        sources.append(
            {
                "id": source.get("Id") or "",
                "name": source.get("Name") or "",
                "path": source_path,
                "filename": source_filename,
                "container": source.get("Container") or "",
                "size": _safe_int(source.get("Size"), 0),
            }
        )
    return sources


def _episode_file_row(item: dict, cached_gap: dict | None = None) -> dict:
    season = _safe_int(item.get("ParentIndexNumber") if item else (cached_gap or {}).get("season"), 0)
    episode = _safe_int(item.get("IndexNumber") if item else (cached_gap or {}).get("episode"), 0)
    season_missing = bool((cached_gap or {}).get("season_missing")) or (
        not item
        and episode <= 0
        and str((cached_gap or {}).get("source") or "") == "season_inferred"
    )
    sources = _episode_media_sources(item or {})
    primary_path = str((item or {}).get("Path") or "").strip()
    if not primary_path and sources:
        primary_path = sources[0].get("path") or ""
    filename = _episode_filename(primary_path)
    if not filename and sources:
        filename = sources[0].get("filename") or ""
    missing = _is_missing_emby_episode(item or {})
    virtual = bool((item or {}).get("IsVirtualItem")) or str((item or {}).get("LocationType") or "").lower() == "virtual"
    source = "emby" if item else str((cached_gap or {}).get("source") or "cache")
    if cached_gap and not item:
        source = str(cached_gap.get("source") or "inferred")
        missing = True
    return {
        "id": (item or {}).get("Id") or (cached_gap or {}).get("id") or "",
        "season": season,
        "episode": episode,
        "label": f"S{season:02d}" if season_missing else f"S{season:02d}E{episode:02d}",
        "title": _clean_episode_title(
            (item or {}).get("Name") or (cached_gap or {}).get("title") or ("整季缺失" if season_missing else ""),
            season,
            episode,
        ),
        "date": (item or {}).get("PremiereDate") or (item or {}).get("DateCreated") or (cached_gap or {}).get("date") or "",
        "missing": missing,
        "virtual": virtual,
        "source": source,
        "season_missing": season_missing,
        "location_type": (item or {}).get("LocationType") or "",
        "path": primary_path,
        "filename": filename,
        "media_sources": sources,
    }


async def _fetch_all_items(
    emby: EmbyClient,
    parent_id: str,
    include_types: str,
    fields: str,
    limit: int = 300,
) -> list[dict]:
    items: list[dict] = []
    start = 0
    total = 1
    while start < total:
        page = await _get_items_page(
            emby,
            parent_id=parent_id,
            include_types=include_types,
            fields=fields,
            start_index=start,
            limit=limit,
        )
        batch = (page or {}).get("Items") or []
        total = _safe_int((page or {}).get("TotalRecordCount"), len(items) + len(batch))
        if not batch:
            break
        items.extend(batch)
        start += limit
    return items


async def _get_items_page(
    emby: EmbyClient,
    parent_id: str,
    include_types: str,
    fields: str,
    start_index: int,
    limit: int,
) -> dict:
    """带重试地读取 Emby Items，避免把连接失败误当成空媒体库。"""
    params = {
        "Recursive": "true",
        "IncludeItemTypes": include_types,
        "Fields": fields,
        "StartIndex": start_index,
        "Limit": limit,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
    }
    if parent_id:
        params["ParentId"] = parent_id

    last_error = ""
    for attempt in range(4):
        result = await emby._get("/emby/Items", params)
        if isinstance(result, dict):
            return result
        last_error = f"Emby Items 返回空: type={include_types} parent={parent_id} start={start_index}"
        await asyncio.sleep(0.8 + attempt * 0.8)
    raise RuntimeError(last_error)


async def _estimate_series_total(emby: EmbyClient, library_id: str) -> int:
    page = await _get_items_page(
        emby,
        parent_id=library_id,
        include_types="Series",
        fields="ProviderIds",
        start_index=0,
        limit=1,
    )
    return _safe_int((page or {}).get("TotalRecordCount"), 0)


async def _series_library(emby: EmbyClient, series_id: str, fallback_id: str = "", fallback_name: str = "") -> dict:
    libraries = await emby.get_libraries()
    library_map = {str(lib.get("ItemId") or ""): lib for lib in libraries}
    if fallback_id and fallback_id in library_map:
        return library_map[fallback_id]
    for lib in libraries:
        if str(lib.get("CollectionType") or "").lower() not in {"tvshows", "tv"}:
            continue
        lib_id = str(lib.get("ItemId") or "")
        if not lib_id:
            continue
        data = await emby._get(
            "/emby/Items",
            {
                "ParentId": lib_id,
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "Ids": series_id,
                "Limit": 1,
            },
        )
        if isinstance(data, dict) and data.get("Items"):
            return lib
    return {"ItemId": fallback_id, "Name": fallback_name}


async def _scan_one_series(
    emby: EmbyClient,
    series: dict,
    lib: dict,
    ignored: set[str],
    mp: MoviePilotClient | None = None,
    season_cache: dict[str, dict[int, dict]] | None = None,
    tmdb_timeout: float | None = None,
) -> dict | None:
    series_id = str(series.get("Id") or "")
    if not series_id or series_id in ignored:
        return None

    fields = (
        "ParentIndexNumber,IndexNumber,Name,Overview,PremiereDate,DateCreated,"
        "SeriesId,SeriesName,SeasonId,SeasonName,LocationType,IsMissing,"
        "IsVirtualItem,MissingEpisode,ProviderIds,Path,MediaSources"
    )
    season_data = await emby._get(f"/Shows/{series_id}/Seasons", {"Fields": "IndexNumber,Path,ProviderIds"})
    season_numbers = {
        _safe_int(item.get("IndexNumber"), -1)
        for item in ((season_data or {}).get("Items") or [])
        if _safe_int(item.get("IndexNumber"), -1) > 0
    }
    episodes = await _fetch_all_items(emby, series_id, "Episode", fields, limit=300)
    if not episodes:
        return None

    seasons: dict[int, dict[int, dict]] = defaultdict(dict)
    for ep in episodes:
        season_num = _safe_int(ep.get("ParentIndexNumber"), -1)
        episode_num = _safe_int(ep.get("IndexNumber"), -1)
        if season_num < 0 or episode_num <= 0:
            continue

        slot = seasons[season_num].setdefault(
            episode_num,
            {
                "present": False,
                "missing_item": None,
                "date": ep.get("PremiereDate") or ep.get("DateCreated") or "",
                "title": _clean_episode_title(ep.get("Name") or "", season_num, episode_num),
            },
        )
        if _is_missing_emby_episode(ep):
            slot["missing_item"] = ep
            if not slot.get("title"):
                slot["title"] = _clean_episode_title(ep.get("Name") or "", season_num, episode_num)
        else:
            slot["present"] = True

    expected_season_map = await _fetch_expected_season_map(mp, series, season_cache, timeout=tmdb_timeout)
    expected_season_nums = set(expected_season_map)
    existing_season_numbers = {num for num in season_numbers if num > 0} | {num for num in seasons if num > 0}

    gaps: list[dict] = []
    for season_num in _missing_season_numbers(existing_season_numbers, series, lib, expected_season_nums):
        target_id = _season_target_id(series_id, season_num)
        if target_id in ignored:
            continue
        season_info = expected_season_map.get(season_num) or {}
        season_title = str(season_info.get("name") or "").strip()
        gap = {
            "id": target_id,
            "season": season_num,
            "episode": 0,
            "title": season_title or "整季缺失",
            "date": _tmdb_season_air_date(season_info),
            "status": 0,
            "source": "tmdb" if season_info else "season_inferred",
            "season_missing": True,
            "season_title": season_title,
            "episode_count": _safe_int(season_info.get("episode_count"), 0),
        }
        if expected_season_nums and season_num not in expected_season_nums:
            gap.update(
                {
                    "anomaly": True,
                    "anomaly_type": "season_number_mismatch",
                    "anomaly_message": (
                        f"TMDB 未收录 S{season_num:02d}，但本地存在更高季号，"
                        "可能是 Emby 入库季号错误"
                    ),
                    "tmdb_seasons": sorted(expected_season_nums),
                    "emby_seasons": sorted(existing_season_numbers),
                }
            )
        gaps.append(gap)

    for season_num in sorted(seasons):
        episode_map = seasons[season_num]
        if not episode_map:
            continue

        known_missing: dict[int, dict] = {}
        present_nums: list[int] = []
        for episode_num, slot in episode_map.items():
            if slot.get("present"):
                present_nums.append(episode_num)
            elif slot.get("missing_item"):
                known_missing[episode_num] = slot

        for episode_num, slot in sorted(known_missing.items()):
            target_id = _episode_target_id(series_id, season_num, episode_num)
            if target_id in ignored:
                continue
            missing = slot["missing_item"] or {}
            gaps.append(
                {
                    "id": target_id,
                    "season": season_num,
                    "episode": episode_num,
                    "title": _clean_episode_title(missing.get("Name") or slot.get("title", ""), season_num, episode_num),
                    "date": missing.get("PremiereDate") or missing.get("DateCreated") or slot.get("date", ""),
                    "status": 2,
                    "source": "emby",
                }
            )

        # S00 特别季编号常常不连续，只采信 Emby 明确标记的缺失项，
        # 避免把不存在的番外/OVA 编号推断成缺集。
        if season_num == 0:
            continue

        if not present_nums:
            continue

        sorted_present = sorted(set(present_nums))
        missing_set = set(known_missing)
        inferred_nums: list[int] = []

        # 只补相邻已存在集之间的小洞。长篇动画和部分剧库常把 IndexNumber
        # 写成绝对集号，跨几十集的大断层更像编号体系切换，而不是缺集。
        max_inferred_hole = 12
        first_present = sorted_present[0]
        if 1 < first_present <= max_inferred_hole + 1:
            inferred_nums.extend(range(1, first_present))
        for prev_num, next_num in zip(sorted_present, sorted_present[1:]):
            hole = next_num - prev_num - 1
            if 0 < hole <= max_inferred_hole:
                inferred_nums.extend(range(prev_num + 1, next_num))
            elif hole > max_inferred_hole:
                logger.info(
                    "[Gaps] 跳过大跨度推断: %s S%02d E%02d-E%02d",
                    series.get("Name") or series_id,
                    season_num,
                    prev_num + 1,
                    next_num - 1,
                )

        for episode_num in sorted(set(inferred_nums)):
            if episode_num in missing_set:
                continue
            target_id = _episode_target_id(series_id, season_num, episode_num)
            if target_id in ignored:
                continue
            gaps.append(
                {
                    "id": target_id,
                    "season": season_num,
                    "episode": episode_num,
                    "title": f"S{season_num:02d}E{episode_num:02d}",
                    "date": "",
                    "status": 0,
                    "source": "inferred",
                }
            )

    if not gaps:
        return None

    provider_ids = series.get("ProviderIds") or {}
    tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TmdbId") or ""
    return {
        "series_id": series_id,
        "series_name": series.get("Name") or "未命名剧集",
        "tmdb_id": str(tmdb_id or ""),
        "imdb_id": provider_ids.get("Imdb") or "",
        "year": series.get("ProductionYear") or "",
        "overview": series.get("Overview") or "",
        "poster": f"/api/gaps/poster/{series_id}",
        "library_id": str(lib.get("ItemId") or ""),
        "library_name": lib.get("Name") or "",
        "gap_count": len(gaps),
        "gaps": sorted(gaps, key=lambda x: (x.get("season", 0), x.get("episode", 0))),
    }


async def _revalidate_gap_scan_results(
    emby: EmbyClient,
    mp: MoviePilotClient,
    results: list[dict],
    ignored: set[str],
) -> list[dict]:
    """全库扫描结束前复核已命中的剧，避免长扫描期间已补全的旧缺口被写回缓存。"""
    candidates = _dedupe_gap_series_results(results)
    if not candidates:
        return []

    try:
        libraries = await emby.get_libraries()
    except Exception as e:
        logger.warning("[Gaps] 复核缺集时读取媒体库失败，保留扫描结果: %s", e)
        return candidates

    library_map = {str(lib.get("ItemId") or ""): lib for lib in libraries}
    season_cache: dict[str, dict[int, dict]] = {}
    semaphore = asyncio.Semaphore(3)

    async def recheck(item: dict) -> dict | None:
        series_id = str(item.get("series_id") or "")
        if not series_id:
            return item
        async with semaphore:
            try:
                series = await emby.get_item(
                    series_id,
                    fields=(
                        "ProviderIds,Overview,ImageTags,ProductionYear,PremiereDate,"
                        "ChildCount,RecursiveItemCount,Path,ParentId"
                    ),
                )
                if not series:
                    logger.warning("[Gaps] 复核缺集时找不到剧集，保留原结果: %s", series_id)
                    return item
                library_id = str(item.get("library_id") or series.get("ParentId") or "")
                library = library_map.get(library_id) or await _series_library(
                    emby,
                    series_id,
                    fallback_id=library_id,
                    fallback_name=str(item.get("library_name") or ""),
                )
                refreshed = await _scan_one_series(emby, series, library, ignored, mp=mp, season_cache=season_cache)
                old_ids = {str(gap.get("id") or "") for gap in (item.get("gaps") or [])}
                new_ids = {str(gap.get("id") or "") for gap in ((refreshed or {}).get("gaps") or [])}
                if old_ids != new_ids:
                    logger.info(
                        "[Gaps] 复核更新缺集: %s(%s) %s -> %s",
                        item.get("series_name") or series.get("Name") or series_id,
                        series_id,
                        len(old_ids),
                        len(new_ids),
                    )
                return refreshed
            except Exception as e:
                logger.warning("[Gaps] 复核缺集失败，保留原结果: %s %s", series_id, e)
                return item

    refreshed_results = [item for item in await asyncio.gather(*(recheck(item) for item in candidates)) if item]
    return _dedupe_gap_series_results(refreshed_results)


async def _scan_gaps_task():
    global _scan_status
    emby = EmbyClient()
    mp = MoviePilotClient()
    season_cache: dict[str, dict[int, dict]] = {}
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _scan_status.update(
        {
            "is_scanning": True,
            "progress": 0,
            "total": 0,
            "processed": 0,
            "current_item": "读取媒体库",
            "results": [],
            "summary": {},
            "error": "",
            "started_at": started_at,
            "finished_at": "",
            "skipped_libraries": [],
        }
    )
    try:
        cfg = await get_gap_config()
        excluded = {str(x) for x in cfg.get("excluded_libraries", [])}
        ignored = await get_gap_ignore_targets()

        libraries = await emby.get_libraries()
        tv_libraries = [
            lib for lib in libraries
            if (lib.get("CollectionType") == "tvshows" or str(lib.get("CollectionType") or "").lower() == "tv")
            and str(lib.get("ItemId") or "") not in excluded
        ]

        total = 0
        skipped_libraries: list[dict] = []
        scannable_libraries: list[tuple[dict, int]] = []
        for lib in tv_libraries:
            lib_id = str(lib.get("ItemId") or "")
            lib_name = _gap_library_label(lib)
            try:
                series_total = await _estimate_series_total(emby, lib_id)
                scannable_libraries.append((lib, series_total))
                total += series_total
            except Exception as e:
                logger.warning("[Gaps] 跳过剧集库 %s(%s): %s", lib_name, lib_id, e)
                skipped_libraries.append({"id": lib_id, "name": lib_name, "error": str(e)})
                _scan_status["skipped_libraries"] = skipped_libraries
                _scan_status["current_item"] = f"跳过 {lib_name}: {e}"
        _scan_status["total"] = total

        results: list[dict] = []
        processed = 0

        def publish_scan_snapshot(current_item: str = ""):
            visible_results = sorted(
                _dedupe_gap_series_results(results),
                key=lambda item: (-_safe_int(item.get("gap_count"), 0), item.get("series_name", "")),
            )
            summary = _gap_scan_summary(
                visible_results,
                tv_libraries,
                scannable_libraries,
                skipped_libraries,
                processed,
                total=total,
                partial=True,
            )
            _scan_status.update(
                {
                    "results": visible_results,
                    "summary": summary,
                    "processed": processed,
                    "progress": min(99, int(processed / total * 100) if total else 0),
                    "skipped_libraries": skipped_libraries,
                }
            )
            if current_item:
                _scan_status["current_item"] = current_item

        for lib, estimated_total in scannable_libraries:
            lib_id = str(lib.get("ItemId") or "")
            lib_name = _gap_library_label(lib)
            _scan_status["current_item"] = f"{lib_name} / 读取剧集"
            try:
                series_items = await _fetch_all_items(
                    emby,
                    lib_id,
                    "Series",
                    "ProviderIds,Overview,ImageTags,ProductionYear,PremiereDate,ChildCount,RecursiveItemCount,Path",
                    limit=200,
                )
            except Exception as e:
                logger.warning("[Gaps] 跳过剧集库 %s(%s): %s", lib_name, lib_id, e)
                skipped_libraries.append({"id": lib_id, "name": lib_name, "error": str(e)})
                processed += estimated_total
                publish_scan_snapshot(f"跳过 {lib_name}: {e}")
                await asyncio.sleep(0.4)
                continue

            semaphore = asyncio.Semaphore(1)
            failed_count = 0

            async def run_one(series: dict) -> dict | None:
                async with semaphore:
                    return await _scan_one_series(emby, series, lib, ignored, mp=mp, season_cache=season_cache)

            for start in range(0, len(series_items), 3):
                batch = series_items[start:start + 3]
                if not batch:
                    continue
                first_name = batch[0].get("Name") or ""
                _scan_status["current_item"] = f"{lib_name} / {first_name}"
                batch_results = await asyncio.gather(*(run_one(series) for series in batch), return_exceptions=True)
                for item in batch_results:
                    processed += 1
                    if isinstance(item, Exception):
                        failed_count += 1
                        logger.warning("[Gaps] 扫描剧集异常: %s", item)
                    elif item:
                        results.append(item)
                    publish_scan_snapshot(f"{lib_name} / 已发现 {len(results)} 部缺集剧集")
                if failed_count > max(5, int(total * 0.05)):
                    raise RuntimeError(f"Emby 请求失败过多，已停止扫描（失败 {failed_count} 项）")
                await asyncio.sleep(0.4)

        _scan_status.update(
            {
                "current_item": f"复核已发现缺集 {len(results)} 部",
                "progress": 99,
            }
        )
        results = await _revalidate_gap_scan_results(emby, mp, results, ignored)
        results.sort(key=lambda x: (-_safe_int(x.get("gap_count"), 0), x.get("series_name", "")))
        summary = _gap_scan_summary(
            results,
            tv_libraries,
            scannable_libraries,
            skipped_libraries,
            processed,
            total=total,
            partial=False,
        )
        await save_gap_cache(results, summary)
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _scan_status.update(
            {
                "is_scanning": False,
                "progress": 100,
                "current_item": f"扫描完成（跳过 {len(skipped_libraries)} 个库）" if skipped_libraries else "扫描完成",
                "results": results,
                "summary": summary,
                "error": "",
                "finished_at": finished_at,
                "created_at": finished_at,
                "skipped_libraries": skipped_libraries,
            }
        )
        logger.info("[Gaps] 缺集扫描完成: %s 部剧集 / %s 个缺口", summary["series_count"], summary["gap_count"])
    except Exception as e:
        logger.exception("[Gaps] 缺集扫描失败")
        _scan_status.update(
            {
                "is_scanning": False,
                "error": str(e),
                "current_item": "扫描失败",
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    finally:
        await emby.close()
        await mp.close()


async def _hydrate_cache_if_needed():
    if _scan_status.get("results") or _scan_status.get("is_scanning"):
        return
    results, summary, created_at = await load_gap_cache()
    if results:
        results = _dedupe_gap_series_results(results)
        summary = _gap_summary_from_results(results, summary)
        _scan_status.update(
            {
                "results": results,
                "summary": summary,
                "created_at": created_at or "",
                "progress": 100,
                "current_item": "已加载缓存",
            }
        )


@router.post("/scan/start")
async def start_gap_scan():
    """启动缺集扫描。"""
    global _scan_task
    if _scan_status.get("is_scanning"):
        return _error("缺集扫描正在运行")
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _scan_status.update(
        {
            "is_scanning": True,
            "progress": 0,
            "total": 0,
            "processed": 0,
            "current_item": "准备扫描",
            "results": [],
            "summary": {},
            "error": "",
            "started_at": started_at,
            "finished_at": "",
            "created_at": "",
            "skipped_libraries": [],
        }
    )
    _scan_task = asyncio.create_task(_scan_gaps_task())
    return _success({"started": True}, "已启动缺集扫描")


@router.get("/scan/progress")
async def gap_scan_progress():
    """获取缺集扫描进度和最新结果。"""
    await _hydrate_cache_if_needed()
    return _success(dict(_scan_status))


@router.get("/series/{series_id}/episodes")
async def gap_series_episodes(series_id: str):
    """查看单部剧集的 Emby 集数和文件名列表。"""
    series_id = str(series_id or "").strip()
    if not series_id:
        return _error("缺少剧集 ID")

    await _hydrate_cache_if_needed()
    cached = next(
        (
            item
            for item in (_scan_status.get("results") or [])
            if str(item.get("series_id") or "") == series_id
        ),
        None,
    )
    cached_gaps = {
        (_safe_int(gap.get("season"), -1), _safe_int(gap.get("episode"), -1)): gap
        for gap in (cached or {}).get("gaps", [])
        if _safe_int(gap.get("season"), -1) >= 0
        and (_safe_int(gap.get("episode"), -1) > 0 or bool(gap.get("season_missing")))
    }

    rows: dict[tuple[int, int], dict] = {}
    series: dict | None = None
    emby = EmbyClient()
    try:
        series = await emby.get_item(
            series_id,
            fields=(
                "ProviderIds,Overview,ImageTags,ProductionYear,PremiereDate,"
                "ChildCount,RecursiveItemCount,Path,ParentId"
            ),
        )
        if series:
            fields = (
                "ParentIndexNumber,IndexNumber,Name,Overview,PremiereDate,DateCreated,"
                "SeriesId,SeriesName,SeasonId,SeasonName,LocationType,IsMissing,"
                "IsVirtualItem,MissingEpisode,ProviderIds,Path,MediaSources"
            )
            episodes = await _fetch_all_items(emby, series_id, "Episode", fields, limit=500)
            for item in episodes:
                season = _safe_int(item.get("ParentIndexNumber"), -1)
                episode = _safe_int(item.get("IndexNumber"), -1)
                if season < 0 or episode <= 0:
                    continue
                key = (season, episode)
                row = _episode_file_row(item, cached_gaps.get(key))
                existing = rows.get(key)
                if not existing or (existing.get("missing") and not row.get("missing")):
                    rows[key] = row
                    continue
                if row.get("media_sources"):
                    known = {
                        (str(source.get("id") or ""), str(source.get("path") or ""))
                        for source in existing.get("media_sources", [])
                    }
                    for source in row.get("media_sources", []):
                        source_key = (str(source.get("id") or ""), str(source.get("path") or ""))
                        if source_key not in known:
                            existing.setdefault("media_sources", []).append(source)
                            known.add(source_key)
                    if not existing.get("filename"):
                        existing["filename"] = row.get("filename") or ""
                    if not existing.get("path"):
                        existing["path"] = row.get("path") or ""

        for key, gap in cached_gaps.items():
            if key not in rows:
                rows[key] = _episode_file_row({}, gap)

        if not series and not cached:
            return _error("Emby 中找不到该剧集，且缺集缓存里没有该剧")

        provider_ids = (series or {}).get("ProviderIds") or {}
        episode_rows = sorted(rows.values(), key=lambda item: (_safe_int(item.get("season"), 0), _safe_int(item.get("episode"), 0)))
        summary = {
            "episode_count": len(episode_rows),
            "present_count": sum(1 for item in episode_rows if not item.get("missing")),
            "missing_count": sum(1 for item in episode_rows if item.get("missing")),
            "virtual_count": sum(1 for item in episode_rows if item.get("virtual")),
            "season_missing_count": sum(1 for item in episode_rows if item.get("season_missing")),
            "source_count": sum(len(item.get("media_sources") or []) for item in episode_rows),
        }
        return _success(
            {
                "series": {
                    "series_id": series_id,
                    "series_name": (series or {}).get("Name") or (cached or {}).get("series_name") or "未命名剧集",
                    "tmdb_id": provider_ids.get("Tmdb") or provider_ids.get("TmdbId") or (cached or {}).get("tmdb_id") or "",
                    "imdb_id": provider_ids.get("Imdb") or (cached or {}).get("imdb_id") or "",
                    "year": (series or {}).get("ProductionYear") or (cached or {}).get("year") or "",
                    "library_id": (series or {}).get("ParentId") or (cached or {}).get("library_id") or "",
                    "library_name": (cached or {}).get("library_name") or "",
                },
                "summary": summary,
                "episodes": episode_rows,
            }
        )
    except Exception as e:
        logger.exception("[Gaps] 读取剧集文件列表失败: %s", series_id)
        return _error(f"读取文件列表失败: {e}")
    finally:
        await emby.close()


@router.post("/refresh_series")
async def refresh_gap_series(payload: GapRefreshSeriesPayload):
    """只刷新单部剧集的缺集缓存，避免等待下一次全库扫描。"""
    series_id = str(payload.series_id or "").strip()
    if not series_id:
        return _error("缺少剧集 ID")
    if _scan_status.get("is_scanning"):
        return _error("全库缺集扫描正在运行，请稍后再刷新单部剧集")

    async with _refresh_lock:
        await _hydrate_cache_if_needed()
        results = [dict(item) for item in (_scan_status.get("results") or [])]
        base_summary = dict(_scan_status.get("summary") or {})
        existing = next((item for item in results if str(item.get("series_id") or "") == series_id), None)

        emby = EmbyClient()
        mp = MoviePilotClient()
        try:
            series = await emby.get_item(
                series_id,
                fields=(
                    "ProviderIds,Overview,ImageTags,ProductionYear,PremiereDate,"
                    "ChildCount,RecursiveItemCount,Path,ParentId"
                ),
            )
            if not series:
                return _error("Emby 中找不到该剧集，可能已被删除或权限不足")

            library_id = str((existing or {}).get("library_id") or series.get("ParentId") or "")
            library = await _series_library(
                emby,
                series_id,
                fallback_id=library_id,
                fallback_name=(existing or {}).get("library_name") or "",
            )
            ignored = await get_gap_ignore_targets()
            refreshed = await _scan_one_series(emby, series, library, ignored, mp=mp, season_cache={}, tmdb_timeout=3.0)

            results = [item for item in results if str(item.get("series_id") or "") != series_id]
            if refreshed:
                results.append(refreshed)
            results = _dedupe_gap_series_results(results)
            results.sort(key=lambda x: (-_safe_int(x.get("gap_count"), 0), x.get("series_name", "")))

            summary = _gap_summary_from_results(results, base_summary)
            await save_gap_cache(results, summary)
            refreshed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _scan_status.update(
                {
                    "is_scanning": False,
                    "progress": 100,
                    "results": results,
                    "summary": summary,
                    "error": "",
                    "current_item": f"已刷新 {series.get('Name') or (existing or {}).get('series_name') or series_id}",
                    "finished_at": refreshed_at,
                    "created_at": refreshed_at,
                }
            )
            message = "已刷新该剧缺集数据" if refreshed else "已刷新：该剧当前无缺集，已从列表移除"
            return _success(
                {
                    "series": refreshed,
                    "removed": refreshed is None,
                    "status": dict(_scan_status),
                },
                message,
            )
        except Exception as e:
            logger.exception("[Gaps] 刷新单剧缺集失败: %s", series_id)
            return _error(f"刷新失败: {e}")
        finally:
            await emby.close()
            await mp.close()


@router.post("/scan/verify")
async def verify_gap_scan():
    """兼容 EmbyPulse 前端的状态校验接口。"""
    await _hydrate_cache_if_needed()
    return _success({"is_scanning": bool(_scan_status.get("is_scanning"))})


@router.get("/config")
async def gap_config():
    cfg = await get_gap_config()
    data = {
        "client_type": "emby",
        "client_url": settings.emby_host,
        "client_user": "",
        "client_pass": "已设置" if settings.emby_api_key else "",
        "excluded_libraries": cfg.get("excluded_libraries", []),
        "cache_interval_hours": cfg.get("cache_interval_hours", 6),
    }
    return _success(data)


@router.post("/config")
async def save_gap_config_endpoint(payload: GapConfigPayload):
    excluded = payload.excluded_libraries
    if isinstance(excluded, str):
        stripped = excluded.strip()
        if stripped.startswith("["):
            try:
                import json

                excluded = json.loads(stripped)
            except Exception:
                excluded = []
        else:
            excluded = [x.strip() for x in stripped.split(",") if x.strip()]
    excluded = [str(x) for x in excluded if str(x).strip()]
    cache_hours = _safe_int(payload.cache_interval_hours, 6)
    await save_gap_config(excluded, cache_hours)
    return _success({"excluded_libraries": excluded, "cache_interval_hours": max(1, cache_hours)}, "配置已保存")


@router.get("/libraries")
async def gap_libraries():
    cfg = await get_gap_config()
    excluded = {str(x) for x in cfg.get("excluded_libraries", [])}
    emby = EmbyClient()
    try:
        libraries = await emby.get_libraries()
        tv_libraries = []
        for lib in libraries:
            if lib.get("CollectionType") != "tvshows":
                continue
            lib_id = str(lib.get("ItemId") or "")
            tv_libraries.append(
                {
                    "id": lib_id,
                    "name": lib.get("Name") or "",
                    "type": lib.get("CollectionType") or "",
                    "excluded": lib_id in excluded,
                }
            )
        return _success(tv_libraries)
    finally:
        await emby.close()


@router.get("/ignores")
async def gap_ignores(target: str = Query(default="")):
    items = await list_gap_ignores(target)
    return _success(items)


@router.post("/ignore")
async def ignore_gap_episode(payload: GapEpisodePayload):
    if not payload.series_id or payload.season_number < 0 or (payload.episode_number <= 0 and payload.season_number <= 0):
        return _error("缺少剧集或集数参数")
    if payload.episode_number <= 0:
        await add_gap_ignore_season(payload.series_id, payload.series_name, payload.season_number)
        return _success(message="已忽略该整季缺失")
    await add_gap_ignore_episode(payload.series_id, payload.series_name, payload.season_number, payload.episode_number)
    return _success(message="已忽略该缺集")


@router.post("/ignore/series")
async def ignore_gap_series(payload: GapSeriesPayload):
    if not payload.series_id:
        return _error("缺少剧集 ID")
    await add_gap_ignore_series(payload.series_id, payload.series_name)
    return _success(message="已忽略该剧集")


@router.post("/ignore/batch_series")
async def ignore_gap_series_batch(payload: GapSeriesPayload):
    items = payload.items or []
    if not items and payload.series_ids:
        items = [{"series_id": sid, "series_name": ""} for sid in payload.series_ids]
    if payload.series_id:
        items.append({"series_id": payload.series_id, "series_name": payload.series_name})
    count = 0
    for item in items:
        series_id = str(item.get("series_id") or item.get("id") or "")
        if not series_id:
            continue
        await add_gap_ignore_series(series_id, item.get("series_name") or item.get("name") or "")
        count += 1
    return _success({"count": count}, f"已忽略 {count} 部剧集")


@router.post("/unignore")
async def unignore_gap_item(payload: GapUnignorePayload):
    target_id = payload.id
    ok = await remove_gap_ignore(target_id, payload.type or payload.target)
    return _success({"removed": ok}, "已恢复")


@router.post("/unignore/batch")
async def unignore_gap_batch(payload: GapUnignorePayload):
    ids = payload.ids or ([payload.id] if payload.id else [])
    count = await remove_gap_ignores([str(x) for x in ids])
    return _success({"count": count}, f"已恢复 {count} 项")


@router.post("/ignore/delete")
async def delete_gap_ignore(payload: GapUnignorePayload):
    ok = await remove_gap_ignore(payload.id, payload.type or payload.target)
    return _success({"removed": ok}, "已删除忽略项")


@router.post("/ignore/batch_delete")
async def delete_gap_ignore_batch(payload: GapUnignorePayload):
    ids = payload.ids or ([payload.id] if payload.id else [])
    count = await remove_gap_ignores([str(x) for x in ids])
    return _success({"count": count}, f"已删除 {count} 项")


def _normalise_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _chinese_num_to_int(text: str) -> int:
    text = (text or "").strip()
    if not text:
        return 0
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return 0


def _season_collection_count(text: str) -> int:
    """识别“两季全 / 全2季 / 2季合集”这类整包资源覆盖的季数。"""
    number = r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
    patterns = [
        rf"(?<!第){number}\s*季\s*(?:全|全集|合集|完整|完结|完)",
        rf"(?:全|全集|合集|完整)\s*{number}\s*季",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1)
        count = _safe_int(raw, 0) if raw.isdigit() else _chinese_num_to_int(raw)
        if count > 0:
            return count
    if "双季" in text or "两季" in text:
        return 2
    return 0


def _episode_range(start: int, end: int, targets: set[int]) -> list[int]:
    return [ep for ep in _episode_span(start, end) if ep in targets]


def _episode_span(start: int, end: int) -> list[int]:
    if start <= 0:
        return []
    if end <= 0:
        end = start
    if end < start:
        start, end = end, start
    return list(range(start, min(end, start + 500) + 1))


def _season_span(start: int, end: int) -> list[int]:
    if start < 0:
        return []
    if end < 0:
        end = start
    if end < start:
        start, end = end, start
    return list(range(start, min(end, start + 99) + 1))


def _series_file_season_override(series_name: str, title: str, target_season: int) -> int:
    """处理 Emby 季号和资源站文件季号不一致的长连载。"""
    target_season = _safe_int(target_season, 0)
    combined = _normalise_title(f"{series_name} {title}")
    title_text = _normalise_title(title)
    is_pokemon = any(token in combined for token in ("宝可梦", "寶可夢", "精灵宝可梦", "神奇宝贝", "宠物小精灵", "pokemon", "pokémon", "pocket monsters"))
    if is_pokemon:
        arc_rules = [
            (range(6, 10), ("advanced", "advance", "generation", "ag", "超世代", "丰缘"), 5),
            (range(10, 14), ("diamond", "pearl", "dp", "钻石", "鑽石", "珍珠", "神奥", "神奧", "2006"), 9),
            (range(14, 17), ("best wishes", "bestwish", "black", "white", "bw", "超级愿望", "超級願望", "黑白", "2010"), 13),
            (range(17, 20), (" xy", "xy", "xyz", "卡洛斯", "2013"), 16),
            (range(20, 23), ("sun", "moon", "sm", "太阳", "太陽", "月亮", "2016"), 19),
            (range(23, 26), ("journeys", "journey", "旅途", "2019"), 22),
        ]
        for seasons, tokens, offset in arc_rules:
            if target_season in seasons and any(token in title_text for token in tokens):
                return target_season - offset
    is_shinchan = any(token in combined for token in ("蜡笔小新", "蠟筆小新", "crayon shin", "kureyon shin", "shin-chan", "shinchan", "クレヨンしんちゃん"))
    if is_shinchan and target_season > 1 and re.search(r"(?<![a-z0-9])s0?1(?!\d)", title_text):
        return 1
    return 0


def _episode_match_ratio(
    title: str,
    season: int,
    episodes: list[int],
    assume_first_season_when_ambiguous: bool = False,
    series_name: str = "",
) -> tuple[float, list[int], str, int]:
    text = _normalise_title(title)
    if not text:
        return 0.0, [], "none", 0

    season = _safe_int(season, 0)
    targets = sorted({_safe_int(ep, 0) for ep in episodes if _safe_int(ep, 0) > 0})
    target_set = set(targets)
    if season < 0:
        return 0.0, [], "none", 0
    file_season = _series_file_season_override(series_name, title, season) or season
    match_seasons = {season}
    if file_season:
        match_seasons.add(file_season)

    season_tokens = [
        token
        for item_season in sorted(match_seasons)
        for token in (
            f"s{item_season:02d}",
            f"s{item_season}",
            f"season {item_season}",
            f"season.{item_season}",
            f"第{item_season}季",
        )
    ]
    if season == 0:
        season_tokens.extend(["specials", "special", "特别篇", "特别", "番外", "ova"])
    explicit_seasons = {
        _safe_int(x, 0)
        for x in re.findall(r"(?<![a-z0-9])s0?(\d{1,2})(?!\d)", text)
    }
    for left, right in re.findall(r"(?<![a-z0-9])s0?(\d{1,2})\s*(?:-|~|至|到|to)\s*s?0?(\d{1,2})(?!\d)", text, re.I):
        explicit_seasons.update(_season_span(_safe_int(left, -1), _safe_int(right, -1)))
    for left, right in re.findall(r"(?<![a-z0-9])season[ ._-]*0?(\d{1,2})\s*(?:-|~|至|到|to)\s*(?:season[ ._-]*)?0?(\d{1,2})(?!\d)", text, re.I):
        explicit_seasons.update(_season_span(_safe_int(left, -1), _safe_int(right, -1)))
    explicit_seasons.update(
        _safe_int(x, 0)
        for x in re.findall(r"第\s*(\d{1,2})\s*季", text)
    )
    for left, right in re.findall(r"第\s*(\d{1,2})\s*(?:-|~|至|到)\s*(\d{1,2})\s*季", text):
        start = _safe_int(left, 0)
        end = _safe_int(right, 0)
        if start > 0 and end > 0:
            explicit_seasons.update(range(min(start, end), max(start, end) + 1))
    explicit_seasons.update(
        _chinese_num_to_int(x)
        for x in re.findall(r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*季", text)
    )
    for left, right in re.findall(
        r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*(?:-|~|至|到)\s*([零〇一二两三四五六七八九十]{1,3})\s*季",
        text,
    ):
        start = _chinese_num_to_int(left)
        end = _chinese_num_to_int(right)
        if start > 0 and end > 0:
            explicit_seasons.update(range(min(start, end), max(start, end) + 1))
    explicit_seasons = {x for x in explicit_seasons if x >= 0}
    collection_count = _season_collection_count(text)
    has_episode_markers = bool(
        re.search(
            r"(?<![a-z0-9])s\d{1,2}[ ._-]*e\d{1,4}(?!\d)"
            r"|(?<![a-z0-9])e\d{1,4}(?!\d)"
            r"|(?<![a-z0-9])episode[ ._-]*\d{1,4}(?!\d)"
            r"|第\s*\d{1,4}\s*[集期话話]"
            r"|(?:更新到|更新至|更到|更至|至|到)\s*\d{1,4}\s*[集期话話]",
            text,
        )
    )
    has_target_season = (
        season in explicit_seasons
        or any(token in text for token in season_tokens if token)
        or (season > 0 and collection_count >= season)
    )
    if (
        assume_first_season_when_ambiguous
        and season == 1
        and not explicit_seasons
        and collection_count == 0
        and not has_episode_markers
    ):
        has_target_season = True
    if season == 0 and re.search(r"(?<![a-z0-9])sp(?![a-z0-9])", text):
        has_target_season = True
    if explicit_seasons and not explicit_seasons.intersection(match_seasons):
        return 0.0, [], "none", 0

    matched: set[int] = set()
    explicit_episode_numbers: set[int] = set()
    for item_season in sorted(match_seasons):
        for match in re.finditer(rf"(?<![a-z0-9])s0?{item_season}[ ._-]*e0*(\d{{1,4}})(?:\s*(?:-|~|至|到)\s*e?0*(\d{{1,4}}))?(?!\d)", text):
            span = _episode_span(_safe_int(match.group(1)), _safe_int(match.group(2)))
            explicit_episode_numbers.update(span)
            matched.update(ep for ep in span if ep in target_set)

    if has_target_season:
        for match in re.finditer(r"(?<![a-z0-9])e0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*e?0*(\d{1,4}))?(?!\d)", text):
            span = _episode_span(_safe_int(match.group(1)), _safe_int(match.group(2)))
            explicit_episode_numbers.update(span)
            matched.update(ep for ep in span if ep in target_set)
        for match in re.finditer(r"第\s*0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*0*(\d{1,4}))?\s*[集期话話]", text):
            span = _episode_span(_safe_int(match.group(1)), _safe_int(match.group(2)))
            explicit_episode_numbers.update(span)
            matched.update(ep for ep in span if ep in target_set)
        for match in re.finditer(r"(?:更新到|更新至|更到|更至|至|到)\s*0*(\d{1,4})\s*[集期话話]", text):
            span = _episode_span(1, _safe_int(match.group(1)))
            explicit_episode_numbers.update(span)
            matched.update(ep for ep in span if ep in target_set)

    if not targets:
        broad_episode_pack = bool(
            explicit_episode_numbers
            and min(explicit_episode_numbers) <= 1
            and len(explicit_episode_numbers) >= 6
        )
        if has_target_season and (not has_episode_markers or broad_episode_pack):
            return 1.0, [], "season_pack", file_season
        return 0.0, [], "none", 0

    if matched:
        match_kind = "episode" if explicit_episode_numbers and explicit_episode_numbers.issubset(target_set) else "episode_pack"
        return len(matched) / max(1, len(targets)), sorted(matched), match_kind, file_season
    if has_target_season and not re.search(r"(?<![a-z0-9])s\d{1,2}[ ._-]*e\d{1,4}(?!\d)|(?<![a-z0-9])(?:e|episode[ ._-]*)\d{1,4}(?!\d)", text):
        return (1.0 if len(targets) > 1 else 0.65), targets, "season_pack", file_season
    return 0.0, [], "none", 0


def _result_match_text(item: dict) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "name",
        "remark",
        "description",
        "subtitle",
        "resource_name",
        "resource_title",
        "share_name",
    ):
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(x) for x in value if x)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _annotate_gap_match(
    result: dict,
    season: int,
    episodes: list[int],
    assume_first_season_when_ambiguous: bool = False,
    series_name: str = "",
) -> dict:
    item = dict(result)
    ratio, matched, match_kind, file_season = _episode_match_ratio(
        _result_match_text(item),
        season,
        episodes,
        assume_first_season_when_ambiguous=assume_first_season_when_ambiguous,
        series_name=series_name,
    )
    item["ui_episode_match_ratio"] = ratio
    item["ui_episode_match_kind"] = match_kind
    if file_season and file_season != _safe_int(season, 0):
        item["ui_file_season"] = file_season
    if match_kind == "season_pack":
        item["ui_episode_match_text"] = "整季包"
    elif match_kind == "episode_pack":
        item["ui_episode_match_text"] = "多集包"
    elif match_kind == "none":
        item["ui_episode_match_text"] = f"0/{len(episodes)}" if episodes else "0/0"
    else:
        item["ui_episode_match_text"] = f"{len(matched)}/{len(episodes)}" if episodes else "0/0"
    item["ui_matched_episodes"] = matched
    return item


def _annotate_gap_match_for_targets(
    result: dict,
    targets: list[dict],
    assume_first_season_when_ambiguous: bool = False,
    series_name: str = "",
) -> dict:
    if not targets:
        return _annotate_gap_match(
            result,
            0,
            [],
            assume_first_season_when_ambiguous=assume_first_season_when_ambiguous,
            series_name=series_name,
        )
    candidates: list[dict] = []
    for target in targets:
        annotated = _annotate_gap_match(
            result,
            _safe_int(target.get("season"), 0),
            target.get("episodes") or [],
            assume_first_season_when_ambiguous=assume_first_season_when_ambiguous,
            series_name=series_name,
        )
        annotated["ui_target_season"] = _safe_int(target.get("season"), 0)
        annotated["ui_target_episodes"] = sorted({_safe_int(ep, 0) for ep in (target.get("episodes") or []) if _safe_int(ep, 0) > 0})
        annotated["ui_target_season_missing"] = _is_season_only_gap_target(target)
        candidates.append(annotated)

    kind_priority = {"episode": 0, "episode_pack": 1, "season_pack": 2, "none": 3}
    candidates.sort(
        key=lambda item: (
            kind_priority.get(str(item.get("ui_episode_match_kind") or "none"), 9),
            -float(item.get("ui_episode_match_ratio") or 0),
            _safe_int(item.get("ui_target_season"), 999),
        )
    )
    best = candidates[0]
    matched_targets = [
        {
            "season": item.get("ui_target_season"),
            "episodes": item.get("ui_target_episodes") or [],
            "matched_episodes": item.get("ui_matched_episodes") or [],
            "season_missing": bool(item.get("ui_target_season_missing")) and not item.get("ui_target_episodes"),
            "file_season": item.get("ui_file_season") or item.get("ui_target_season"),
            "match_kind": item.get("ui_episode_match_kind"),
            "match_text": item.get("ui_episode_match_text"),
        }
        for item in candidates
        if item.get("ui_episode_match_kind") != "none"
    ]
    best["ui_gap_targets"] = matched_targets
    if matched_targets:
        first = matched_targets[0]
        best["ui_target_season"] = first["season"]
        best["ui_target_episodes"] = first["episodes"]
        if first.get("file_season") and _safe_int(first.get("file_season"), 0) != _safe_int(first.get("season"), 0):
            best["ui_file_season"] = first["file_season"]
    return best


async def _apply_gap_transfer_marks(results: list[dict]) -> list[dict]:
    keys: list[str] = []
    candidates_by_index: dict[int, list[str]] = {}
    for idx, item in enumerate(results):
        candidates = _gap_resource_key_candidates(item)
        candidates_by_index[idx] = candidates
        keys.extend(candidates)
    marks = await list_gap_transfer_marks(keys)
    if not marks:
        return results

    for idx, item in enumerate(results):
        mark = next((marks[key] for key in candidates_by_index.get(idx, []) if key in marks), None)
        if not mark:
            continue
        status = str(mark.get("status") or "").strip().lower()
        if not _is_gap_resource_mark_status(status):
            continue
        item["is_unlocked"] = True
        item["ui_unlocked"] = True
        item["ui_transfer_marked"] = True
        item["ui_transfer_status"] = status
        item["ui_transfer_updated_at"] = mark.get("updated_at", "")
        item["status"] = status
    return results


def _dedupe_results(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        key = "|".join(
            str(item.get(k) or "")
            for k in ("enclosure", "page_url", "slug", "title", "name")
        ).strip("|")
        if not key:
            key = repr(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _unique_keywords(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _series_search_names(series_name: str, extra_names: list[str] | None = None) -> list[str]:
    name = series_name.strip()
    aliases = [name]
    aliases.extend(extra_names or [])
    normalized = _normalise_title(name)
    if any(token in normalized for token in ("宝可梦", "寶可夢", "精灵宝可梦", "神奇宝贝", "宠物小精灵", "pokemon", "pokémon", "pocket monsters")):
        aliases.extend(["宝可梦", "精灵宝可梦", "Pokémon", "Pokemon", "Pocket Monsters"])
    return _unique_keywords(aliases)


def _precise_year_search_enabled(library_name: str, year: Any) -> bool:
    if _safe_int(year, 0) <= 0:
        return False
    text = _normalise_title(library_name)
    if any(token in text for token in ("动漫", "动画", "国漫", "少儿")):
        return False
    return any(token in text for token in ("剧", "日韩", "欧美", "国产", "tv", "show"))


def _search_keywords(
    series_name: str,
    season: int,
    episodes: list[int],
    extra_names: list[str] | None = None,
    year: Any = "",
    library_name: str = "",
) -> list[str]:
    name = series_name.strip()
    if not name:
        return []
    names = _series_search_names(name, extra_names=extra_names)
    broad_names = _unique_keywords((extra_names or []) + [name] + [item for item in names if item != name])
    episodes = sorted({_safe_int(x, 0) for x in episodes if _safe_int(x, 0) > 0})
    keywords: list[str] = list(extra_names or [])
    year_num = _safe_int(year, 0)
    use_precise_year = _precise_year_search_enabled(library_name, year_num)

    def add_precise_year_keywords(item_name: str, *suffixes: str):
        if not use_precise_year:
            return
        for suffix in suffixes:
            suffix_text = str(suffix or "").strip()
            if suffix_text:
                keywords.append(f"{item_name} {year_num} {suffix_text}")
                keywords.append(f"{item_name} {suffix_text} {year_num}")
            else:
                keywords.append(f"{item_name} {year_num}")

    if len(episodes) == 1:
        ep = episodes[0]
        if season == 0:
            for item_name in names:
                add_precise_year_keywords(item_name, f"S00E{ep:02d}", "S00", "特别篇")
                keywords.extend([
                    f"{item_name} S00E{ep:02d}",
                    f"{item_name} S00",
                    f"{item_name} 特别篇",
                    f"{item_name} SP",
                ])
            keywords.extend(broad_names)
            return _unique_keywords(keywords)
        for item_name in names:
            add_precise_year_keywords(item_name, f"S{season:02d}E{ep:02d}", f"S{season:02d}", f"第{season}季 第{ep}集")
            keywords.extend([
                f"{item_name} S{season:02d}E{ep:02d}",
                f"{item_name} S{season:02d}",
                f"{item_name} 第{season}季 第{ep}集",
            ])
        keywords.extend(broad_names)
        return _unique_keywords(keywords)
    if season == 0:
        for item_name in names:
            add_precise_year_keywords(item_name, "S00", "特别篇", "Specials")
            keywords.extend([
                f"{item_name} S00",
                f"{item_name} 特别篇",
                f"{item_name} Specials",
            ])
        keywords.extend(broad_names)
        return _unique_keywords(keywords)
    for item_name in names:
        add_precise_year_keywords(item_name, f"S{season:02d}", f"第{season}季")
        keywords.extend([
            f"{item_name} S{season:02d}",
            f"{item_name} 第{season}季",
        ])
    keywords.extend(broad_names)
    return _unique_keywords(keywords)


def _gap_identity_context(item: dict, payload: GapSearchPayload) -> dict:
    target_year = _safe_int(payload.year, 0)
    target_imdb = str(payload.imdb_id or "").strip().lower()
    result_imdb = str(item.get("imdbid") or item.get("imdb_id") or item.get("imdb") or "").strip().lower()
    title_text = _normalise_title(_result_match_text(item))
    result_year = _safe_int(item.get("year"), 0)

    score = 0
    labels: list[str] = []
    mismatch = False
    if target_imdb and result_imdb:
        if target_imdb == result_imdb:
            score += 40
            labels.append("IMDb 匹配")
        else:
            score -= 60
            mismatch = True
            labels.append("IMDb 不符")
    if target_year > 0:
        title_has_year = str(target_year) in title_text
        if result_year == target_year or title_has_year:
            score += 20
            labels.append("年份匹配")
        elif result_year > 0 and result_year != target_year:
            score -= 20
            mismatch = True
            labels.append("年份不符")
    return {
        "score": score,
        "labels": labels,
        "mismatch": mismatch,
    }


def _apply_gap_identity_context(items: list[dict], payload: GapSearchPayload) -> list[dict]:
    for item in items:
        context = _gap_identity_context(item, payload)
        item["ui_identity_score"] = context["score"]
        item["ui_identity_labels"] = context["labels"]
        item["ui_identity_mismatch"] = context["mismatch"]
    return items


def _season_aliases_from_name(series_name: str, season_name: str) -> list[str]:
    normalized = _normalise_title(f"{series_name} {season_name}")
    cleaned = re.sub(r"第\s*\d+\s*季", "", season_name or "").strip()
    cleaned = re.sub(r"[（(].*?[）)]", "", cleaned).strip()
    aliases: list[str] = []
    if cleaned:
        aliases.append(f"{series_name} {cleaned}")
    is_pokemon = any(token in normalized for token in ("宝可梦", "寶可夢", "精灵宝可梦", "神奇宝贝", "宠物小精灵", "pokemon", "pokémon", "pocket monsters"))
    if is_pokemon and any(token in normalized for token in ("钻石", "鑽石", "珍珠", "神奥", "神奧")):
        aliases.extend(["宝可梦 钻石与珍珠", "精灵宝可梦 钻石与珍珠", "Pokemon Diamond and Pearl", "Pokémon Diamond and Pearl", "Pokemon DP"])
    if is_pokemon and "旅途" in normalized:
        aliases.extend(["宝可梦 旅途", "Pokemon Journeys", "Pokémon Journeys", "Pocket Monsters 2019"])
    return _unique_keywords(aliases)


async def _gap_season_search_aliases(series_id: str, series_name: str, season: int) -> list[str]:
    if not series_id or _safe_int(season, 0) <= 0:
        return []
    emby = EmbyClient()
    try:
        data = await emby._get(f"/Shows/{series_id}/Seasons", {"Fields": "IndexNumber,SortName,OriginalTitle"})
        items = data.get("Items", []) if isinstance(data, dict) else []
        for item in items:
            if _safe_int(item.get("IndexNumber"), -1) == _safe_int(season, 0):
                return _season_aliases_from_name(series_name, str(item.get("Name") or item.get("OriginalTitle") or ""))
    except Exception as e:
        logger.warning("[Gaps] 读取季别名失败: %s", e)
    finally:
        await emby.close()
    return []


@router.post("/search_mp")
async def search_gap_moviepilot(payload: GapSearchPayload):
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    full_series = bool(payload.full_series)
    if not targets and not full_series:
        return _error("缺少待补集数", {"genes": [], "results": []})

    mp = MoviePilotClient()
    genes: list[dict] = []
    merged: list[dict] = []
    errors: list[str] = []
    searched_keywords: set[str] = set()
    try:
        search_targets = targets or [{"season": _safe_int(payload.season, 1) or 1, "episodes": []}]
        for target in search_targets:
            target_season = _safe_int(target.get("season"), 0)
            target_episodes = target.get("episodes") or []
            season_aliases = await _gap_season_search_aliases(str(payload.series_id or ""), payload.series_name, target_season)
            target_start = len(merged)
            for keyword in _search_keywords(
                payload.series_name,
                target_season,
                target_episodes,
                extra_names=season_aliases,
                year=payload.year,
                library_name=payload.library_name,
            ):
                keyword_key = keyword.lower()
                if keyword_key in searched_keywords:
                    continue
                searched_keywords.add(keyword_key)
                try:
                    results = await mp.search(keyword, media_type="tv")
                    genes.append({"season": target_season, "episodes": target_episodes, "keyword": keyword, "count": len(results)})
                    merged.extend(results)
                    if full_series:
                        if len(merged) - target_start >= 120:
                            break
                        continue
                    if any(
                        _annotate_gap_match(item, target_season, target_episodes, series_name=payload.series_name).get("ui_episode_match_kind") != "none"
                        for item in results
                    ):
                        break
                    if len(merged) - target_start >= 120:
                        break
                except RuntimeError as e:
                    errors.append(str(e))
                    break
                except Exception as e:
                    errors.append(f"{keyword}: {e}")
        annotated = (
            [_mark_full_series_gap_result(item) for item in _dedupe_results(merged)]
            if full_series
            else [
                _annotate_gap_match_for_targets(item, targets, series_name=payload.series_name)
                for item in _dedupe_results(merged)
            ]
        )
        _apply_gap_identity_context(annotated, payload)
        for item in annotated:
            match_kind = item.get("ui_episode_match_kind")
            if full_series:
                item["ui_download_mode"] = "normal"
            elif _is_season_only_gap_match(item, targets):
                item["ui_download_mode"] = "normal"
                item["ui_download_hint"] = "整季缺失将直接提交 MoviePilot 下载"
            elif match_kind in {"season_pack", "episode_pack"}:
                item["ui_download_mode"] = "split"
                item["ui_download_hint"] = "将提交 MoviePilot 后按文件列表只选择缺失集数"
            elif match_kind == "episode":
                item["ui_download_mode"] = "normal"
            else:
                item["ui_download_blocked"] = True
                item["ui_download_block_reason"] = "该资源没有命中当前缺失集数，缺集管理不会自动下载"
        annotated.sort(
            key=lambda x: (
                -float(x.get("ui_episode_match_ratio") or 0),
                0 if x.get("ui_episode_match_kind") == "episode" else 1,
                0 if x.get("ui_episode_match_kind") == "episode_pack" else 1,
                -_safe_int(x.get("ui_identity_score"), 0),
                1 if x.get("ui_identity_mismatch") else 0,
                -_safe_int(x.get("seeders"), 0),
                -_safe_int(x.get("size"), 0),
            )
        )
        if errors and not annotated:
            return _error("; ".join(errors), {"genes": genes, "results": []})
        top_results = annotated[:80]
        try:
            top_results = await mp.annotate_downloader_statuses(top_results)
        except Exception:
            logger.exception("[Gaps] 读取 MoviePilot 下载器状态失败")
        return _success({"genes": genes, "results": top_results, "errors": errors})
    finally:
        await mp.close()


@router.post("/download_status")
async def gap_moviepilot_download_status(payload: GapDownloadStatusPayload):
    mp = MoviePilotClient()
    try:
        results = await mp.annotate_downloader_statuses(payload.results[:80])
        return _success({"results": results})
    except Exception as e:
        logger.exception("[Gaps] MoviePilot 下载状态读取异常")
        return _error(f"下载状态读取异常: {e}", {"results": payload.results})
    finally:
        await mp.close()


@router.post("/search_hdhive")
async def search_gap_hdhive(payload: GapSearchPayload):
    hd = HDHiveClient()
    try:
        tmdb_id = _safe_int(payload.tmdb_id, 0)
        targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
        full_series = bool(payload.full_series)
        if not targets and not full_series:
            return _error("缺少待补集数", {"results": []})
        results = await hd.search(
            keyword=payload.series_name,
            tmdb_id=tmdb_id,
            emby_item_id=payload.series_id,
            media_type="tv",
        )
        annotated = (
            [_mark_full_series_gap_result(item) for item in _dedupe_results(results)]
            if full_series
            else [
                _annotate_gap_match_for_targets(
                    item,
                    targets,
                    assume_first_season_when_ambiguous=bool(tmdb_id or payload.series_id),
                    series_name=payload.series_name,
                )
                for item in _dedupe_results(results)
            ]
        )
        _apply_gap_identity_context(annotated, payload)
        annotated = await _apply_gap_transfer_marks(annotated)
        for item in annotated:
            item["ui_size_bytes"] = _gap_result_size_bytes(item)
            item["ui_is_official"] = _is_official_gap_resource(item)
        annotated.sort(
            key=lambda x: (
                -float(x.get("ui_episode_match_ratio") or 0),
                -_safe_int(x.get("ui_identity_score"), 0),
                1 if x.get("ui_identity_mismatch") else 0,
                0 if x.get("ui_is_official") else 1,
                -_safe_int(x.get("ui_size_bytes"), 0),
                0 if x.get("is_unlocked") else 1,
                _safe_int(x.get("unlock_points"), 9999),
            )
        )
        return _success({"results": annotated[:80]})
    except RuntimeError as e:
        return _error(str(e), {"results": []})
    except Exception as e:
        return _error(f"影巢搜索异常: {e}", {"results": []})
    finally:
        await hd.close()


@router.post("/subscribe_mp")
async def subscribe_gap_moviepilot(payload: GapMoviePilotSubscribePayload):
    series_name = str(payload.series_name or "").strip()
    tmdb_id = _safe_int(payload.tmdb_id, 0)
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    if not series_name:
        return _error("缺少剧集名称")
    if tmdb_id <= 0:
        return _error("缺少 TMDB ID，MoviePilot 无法准确订阅")
    if not targets:
        return _error("缺少待订阅集数")

    mediaid = f"tmdb:{tmdb_id}"
    year = str(payload.year or "").strip()
    item_id = _gap_subscribe_history_item_id(payload, targets)
    item_name = _gap_subscribe_history_item_name(payload, targets)
    target_text = _gap_targets_summary(targets)
    results: list[dict] = []
    failures: list[str] = []
    added = 0
    existing = 0

    mp = MoviePilotClient()
    try:
        for target in targets:
            season = _safe_int(target.get("season"), 0) or 1
            episodes = sorted({
                _safe_int(ep, 0)
                for ep in target.get("episodes", [])
                if _safe_int(ep, 0) > 0
            })
            season_only = bool(target.get("season_missing")) and not episodes
            if not episodes and not season_only:
                continue
            label = f"S{season:02d} 整季" if season_only else _gap_episode_summary(season, episodes)

            current = await mp.get_subscription(mediaid, season=season, title=series_name)
            if _mp_gap_service_unavailable(mp, getattr(mp, "last_error", "")):
                reason = getattr(mp, "last_error", "") or "MoviePilot 服务暂不可用"
                failures.append(f"{label}: {reason}")
                results.append({
                    "season": season,
                    "episodes": episodes,
                    "season_missing": season_only,
                    "status": "error",
                    "message": reason,
                })
                break
            if current:
                disabled_best_version, disable_error = await _disable_mp_gap_best_version(mp, current)
                if disable_error:
                    failures.append(f"{label}: 订阅已存在，但关闭洗版失败: {disable_error}")
                existing += 1
                results.append({
                    "season": season,
                    "episodes": episodes,
                    "season_missing": season_only,
                    "status": "exists",
                    "id": current.get("id"),
                    "best_version_disabled": disabled_best_version,
                    "message": "订阅已存在，已关闭洗版" if disabled_best_version else "订阅已存在",
                })
                continue

            subscribe_payload: dict[str, Any] = {
                "name": series_name,
                "type": "电视剧",
                "tmdbid": tmdb_id,
                "mediaid": mediaid,
                "season": season,
                "lack_episode": 0 if season_only else len(episodes),
                "best_version": 0,
                "best_version_full": 0,
                "episode_priority": {},
                "note": f"Media Refiner 缺集订阅: {label}；洗版已关闭",
            }
            if year:
                subscribe_payload["year"] = year

            resp = await mp.add_subscription(subscribe_payload)
            resp_data = resp.get("data") if isinstance(resp, dict) else None
            subscribe_id = resp_data.get("id") if isinstance(resp_data, dict) else None
            subscribe_id = subscribe_id or (resp.get("id") if isinstance(resp, dict) else None)
            message = resp.get("message", "") if isinstance(resp, dict) else ""
            if resp and resp.get("success"):
                status = "exists" if "已存在" in message else "added"
                disabled_best_version = False
                disable_error = ""
                current = await mp.get_subscription(mediaid, season=season, title=series_name)
                if current:
                    disabled_best_version, disable_error = await _disable_mp_gap_best_version(mp, current)
                    if disabled_best_version:
                        message = f"{message or '订阅已添加'}，已关闭洗版"
                    elif disable_error:
                        failures.append(f"{label}: 订阅已添加，但关闭洗版失败: {disable_error}")
                if status == "exists":
                    existing += 1
                else:
                    added += 1
                results.append({
                    "season": season,
                    "episodes": episodes,
                    "season_missing": season_only,
                    "status": status,
                    "id": subscribe_id,
                    "best_version_disabled": disabled_best_version,
                    "message": message or ("订阅已存在" if status == "exists" else "订阅已添加"),
                })
                continue

            reason = message or "MoviePilot API 无响应"
            failures.append(f"{label}: {reason}")
            results.append({
                "season": season,
                "episodes": episodes,
                "season_missing": season_only,
                "status": "error",
                "message": reason,
            })
            if _mp_gap_service_unavailable(mp, reason):
                break

        if not results:
            return _error("缺少待订阅目标")

        summary_parts = []
        if added:
            summary_parts.append(f"已添加 {added} 个订阅")
        if existing:
            summary_parts.append(f"已存在 {existing} 个订阅")
        if failures:
            summary_parts.append(f"失败 {len(failures)} 个")
        message = "，".join(summary_parts) or "没有需要新增的订阅"
        data = {
            "results": results,
            "added": added,
            "existing": existing,
            "failed": len(failures),
            "failures": failures,
        }

        if added or existing:
            await add_subscribe_log(
                "",
                "缺集管理",
                "subscribe",
                item_name,
                item_id,
                f"缺集 MP 订阅: {target_text} · {message}",
            )
            return _success(data, message)

        error_message = "MoviePilot 订阅失败: " + "; ".join(failures)
        await add_subscribe_log("", "缺集管理", "error", item_name, item_id, error_message)
        return _error(error_message, data)
    except RuntimeError as e:
        message = str(e)
        await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集 MP 订阅失败: {message}")
        return _error(message)
    except Exception as e:
        logger.exception("[Gaps] MoviePilot 订阅异常")
        await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集 MP 订阅异常: {e}")
        return _error(f"MoviePilot 订阅异常: {e}")
    finally:
        await mp.close()


@router.post("/download")
async def download_gap_moviepilot(payload: GapDownloadPayload):
    result = dict(payload.result or {})
    torrent_url = payload.torrent_url or result.get("enclosure") or result.get("download_url") or ""
    if not torrent_url:
        return _error("缺少下载链接")
    full_series = bool(payload.full_series)
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    match_kind = str(result.get("ui_episode_match_kind") or "")
    if full_series and (result.get("ui_download_blocked") is True or match_kind != "full_series" or result.get("ui_full_series") is not True):
        return _error("该资源不是全集/整季包，已拦截全集重下")
    target_season = _safe_int(payload.season, 0)
    target_episodes = sorted({_safe_int(ep, 0) for ep in payload.episodes if _safe_int(ep, 0) > 0})
    display_targets = targets
    file_targets = targets
    season_only_targets = [target for target in targets if bool(target.get("season_missing")) and not target.get("episodes")]
    if targets and not full_series:
        match_kind, display_targets, file_targets = _download_gap_targets_from_result(result, targets, payload.series_name)
        if match_kind == "none":
            if bool(payload.force_mismatch):
                display_targets = targets
                file_targets = targets
                match_kind = "force"
            else:
                return _error("该资源没有命中当前缺失集数，缺集管理不会自动下载")
        if season_only_targets and (not display_targets or not file_targets):
            display_targets = season_only_targets
            file_targets = season_only_targets
    if display_targets:
        target_season = _safe_int(display_targets[0].get("season"), target_season)
        target_episodes = sorted({_safe_int(ep, 0) for ep in display_targets[0].get("episodes", []) if _safe_int(ep, 0) > 0})
    if not full_series and (not display_targets or not file_targets):
        return _error("缺少待下载集数")
    payload.season = target_season
    payload.episodes = target_episodes
    payload.targets = [GapTargetPayload(**target) for target in display_targets]

    mp = MoviePilotClient()
    item_id = _gap_history_item_id(payload)
    item_name = _gap_history_item_name(payload)
    try:
        should_split_pack = (
            not full_series
            and match_kind in {"episode_pack", "season_pack"}
            and any(
                target.get("episodes") or _is_season_only_gap_target(target)
                for target in file_targets
            )
        )
        if should_split_pack:
            resp = await mp.download_selected_episodes(
                torrent_url,
                torrent_info=result,
                tmdbid=_safe_int(payload.tmdb_id, 0),
                season=_safe_int(file_targets[0].get("season"), target_season),
                episodes=target_episodes,
                file_targets=file_targets,
            )
            history_prefix = "缺集 MP 拆包下载"
            success_message = "MoviePilot 拆包下载已提交"
        else:
            resp = await mp.download(torrent_url, torrent_info=result, tmdbid=_safe_int(payload.tmdb_id, 0))
            history_prefix = "全集重下 MP 下载" if full_series else "缺集 MP 下载"
            success_message = "MoviePilot 全集下载已提交" if full_series else "MoviePilot 下载已提交"
        if resp and resp.get("success"):
            await add_subscribe_log(
                "",
                "缺集管理",
                "download",
                item_name,
                item_id,
                _gap_history_message(history_prefix, payload, result=result, extra=resp.get("message", "")),
            )
            return _success(resp, resp.get("message") or success_message)
        message = resp.get("message", "MoviePilot 下载提交失败") if resp else "MoviePilot API 无响应"
        await add_subscribe_log(
            "",
            "缺集管理",
            "error",
            item_name,
            item_id,
            _gap_history_message("缺集 MP 下载失败", payload, result=result, extra=message),
        )
        return _error(message, resp)
    except Exception as e:
        await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集 MP 下载异常: {e}")
        return _error(f"MoviePilot 下载异常: {e}")
    finally:
        await mp.close()


@router.post("/download_hdhive")
async def download_gap_hdhive(payload: GapDownloadPayload):
    result = payload.result or {}
    slug = payload.slug or result.get("slug") or result.get("id") or ""
    if not slug:
        return _error("缺少影巢资源标识")
    full_series = bool(payload.full_series)
    match_kind = str(result.get("ui_episode_match_kind") or "")
    if full_series and (result.get("ui_download_blocked") is True or match_kind != "full_series" or result.get("ui_full_series") is not True):
        return _error("该资源不是全集/整季包，已拦截全集重下")
    targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
    if targets:
        annotated_result = _annotate_gap_match_for_targets(result, targets, series_name=payload.series_name)
        target_season = _safe_int(annotated_result.get("ui_target_season"), _safe_int(payload.season, 0))
        target_episodes = sorted({
            _safe_int(ep, 0)
            for ep in (annotated_result.get("ui_target_episodes") or payload.episodes)
            if _safe_int(ep, 0) > 0
        })
        if target_episodes:
            payload.season = target_season
            payload.episodes = target_episodes

    hd = HDHiveClient()
    item_id = _gap_history_item_id(payload)
    item_name = _gap_history_item_name(payload)
    try:
        folder_id = payload.folder_id or settings.cloud115_folder_id or "0"
        resp = await hd.unlock_and_transfer(slug, folder_id, force_transfer=bool(payload.force_transfer))
        if not resp:
            await _notify_gap_hdhive_transfer(payload, {}, False, "影巢 API 无响应")
            await add_subscribe_log("", "缺集管理", "error", item_name, item_id, "缺集影巢转存失败: 影巢 API 无响应")
            return _error("影巢 API 无响应")
        status = _gap_transfer_status(resp)
        if status == "error":
            message = resp.get("message", "影巢转存失败")
            await _notify_gap_hdhive_transfer(payload, resp, False, message)
            await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集影巢转存失败: {message}")
            return _error(message, resp)
        ok = _is_gap_resource_mark_status(status)
        if not ok:
            message = resp.get("message") or f"影巢转存未完成: {status or '未知状态'}"
            await _notify_gap_hdhive_transfer(payload, resp, False, message)
            await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集影巢转存失败: {message}")
            return _error(message, resp)
        recover_result: dict = {}
        verify_result: dict = {}
        if status == "already_owned":
            recover_result = await _recover_already_owned_gap_transfer(payload, result, folder_id, resp)
            if isinstance(resp, dict):
                resp["cloud115_recover"] = recover_result
            if not recover_result.get("ok"):
                message = recover_result.get("message") or "115 已转存记录存在，但目标目录未找到实体文件"
                await _notify_gap_hdhive_transfer(payload, resp, False, message)
                await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集影巢转存失败: {message}")
                return _error(message, resp)
            if isinstance(resp, dict):
                resp["status"] = "transferred"
                resp["message"] = recover_result.get("message") or resp.get("message", "")
            status = "transferred"
            ok = True
        elif status == "transferred":
            targets = _normalise_gap_targets(payload.season, payload.episodes, payload.targets)
            if targets:
                from app.services.cloud115 import Cloud115Client

                c115 = Cloud115Client()
                try:
                    verify_result = await _wait_verify_cloud115_transfer_target(c115, folder_id, targets)
                finally:
                    await c115.close()
            else:
                from app.services.transfer_verify import verify_hdhive_transfer_visible

                verify_result = await verify_hdhive_transfer_visible(result, resp)
            if isinstance(resp, dict):
                resp["cloud115_verify"] = verify_result
            if not verify_result.get("ok"):
                message = verify_result.get("message") or "115 转存已提交，但目标目录未找到实体文件"
                await _notify_gap_hdhive_transfer(payload, resp, False, message)
                await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集影巢转存失败: {message}")
                return _error(message, resp)
        clouddrive_result = await _refresh_clouddrive_after_gap_transfer(status)
        if isinstance(resp, dict):
            resp["clouddrive"] = clouddrive_result
        if ok:
            result_for_key = dict(result)
            if slug:
                result_for_key["slug"] = slug
            resource_key = (_gap_resource_key_candidates(result_for_key) or [""])[0]
            await save_gap_transfer_mark(
                resource_key=resource_key,
                resource_title=_gap_result_title(result),
                series_name=payload.series_name,
                season_number=_safe_int(payload.season, 0),
                episodes=payload.episodes,
                status=status,
                response=resp,
            )
            await add_subscribe_log(
                "",
                "缺集管理",
                "unlock" if status == "unlocked" else "transfer",
                item_name,
                item_id,
                _gap_history_message(
                    "全集重下影巢处理" if full_series else "缺集影巢处理",
                    payload,
                    result=result,
                    status=status,
                    extra=" · ".join(
                        part
                        for part in (
                            recover_result.get("message", ""),
                            clouddrive_result.get("message", "") if not clouddrive_result.get("skipped") else "",
                        )
                        if part
                    ),
                ),
            )
        await _notify_gap_hdhive_transfer(payload, resp, ok, resp.get("message", ""))
        default_message = "影巢资源已解锁" if status == "unlocked" else "影巢转存已提交"
        response_message = resp.get("message") or default_message
        if recover_result.get("message"):
            response_message = recover_result["message"]
        if clouddrive_result.get("ok"):
            response_message = f"{response_message}，CD2 已刷新目录缓存"
        elif not clouddrive_result.get("skipped"):
            response_message = f"{response_message}，{clouddrive_result.get('message', 'CD2 刷新失败')}"
        return _success(resp, response_message)
    except Exception as e:
        await _notify_gap_hdhive_transfer(payload, {"status": "error"}, False, f"影巢转存异常: {e}")
        await add_subscribe_log("", "缺集管理", "error", item_name, item_id, f"缺集影巢转存异常: {e}")
        return _error(f"影巢转存异常: {e}")
    finally:
        await hd.close()


@router.get("/poster/{item_id}")
async def gap_poster(item_id: str):
    """代理 Emby 海报，避免把 API Key 暴露到页面。"""
    if not settings.emby_host or not settings.emby_api_key:
        return Response(status_code=404)
    url = f"{settings.emby_host.rstrip('/')}/Items/{item_id}/Images/Primary"
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            resp = await client.get(url, params={"api_key": settings.emby_api_key}, follow_redirects=True)
            if resp.status_code >= 400 or not resp.content:
                return Response(status_code=404)
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        return Response(status_code=404)
