"""缺集管理 API - 扫描 Emby 剧集缺口并对接 MP / 影巢补齐。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import settings
from app.database import (
    add_gap_ignore_episode,
    add_gap_ignore_series,
    get_gap_config,
    get_gap_ignore_targets,
    list_gap_transfer_marks,
    list_gap_ignores,
    load_gap_cache,
    make_gap_episode_target_id,
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

GAP_TRANSFER_SUCCESS_STATUSES = {"transferred", "already_owned", "submitted", "success", "ok"}


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


class GapUnignorePayload(BaseModel):
    id: str = ""
    type: str = ""
    ids: list[str] = Field(default_factory=list)
    target: str = ""


class GapSearchPayload(BaseModel):
    series_id: str = ""
    series_name: str
    season: int = 1
    episodes: list[int] = Field(default_factory=list)
    tmdb_id: int | str = 0
    type: str = "tv"


class GapDownloadPayload(BaseModel):
    result: dict = Field(default_factory=dict)
    torrent_url: str = ""
    tmdb_id: int | str = 0
    folder_id: str = ""
    slug: str = ""
    series_name: str = ""
    season: int = 0
    episodes: list[int] = Field(default_factory=list)


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


def _gap_result_title(result: dict) -> str:
    return (
        result.get("title")
        or result.get("name")
        or result.get("resource_name")
        or result.get("resource_title")
        or result.get("share_name")
        or ""
    )


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


def _gap_library_label(lib: dict) -> str:
    return lib.get("Name") or str(lib.get("ItemId") or "") or "剧集库"


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


async def _scan_one_series(emby: EmbyClient, series: dict, lib: dict, ignored: set[str]) -> dict | None:
    series_id = str(series.get("Id") or "")
    if not series_id or series_id in ignored:
        return None

    fields = (
        "ParentIndexNumber,IndexNumber,Name,Overview,PremiereDate,DateCreated,"
        "SeriesId,SeriesName,SeasonId,SeasonName,LocationType,IsMissing,"
        "IsVirtualItem,MissingEpisode,ProviderIds,Path,MediaSources"
    )
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

    gaps: list[dict] = []
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


async def _scan_gaps_task():
    global _scan_status
    emby = EmbyClient()
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
                _scan_status["processed"] = processed
                _scan_status["progress"] = min(99, int(processed / total * 100) if total else 0)
                _scan_status["skipped_libraries"] = skipped_libraries
                _scan_status["current_item"] = f"跳过 {lib_name}: {e}"
                await asyncio.sleep(0.4)
                continue

            semaphore = asyncio.Semaphore(1)
            failed_count = 0

            async def run_one(series: dict) -> dict | None:
                async with semaphore:
                    return await _scan_one_series(emby, series, lib, ignored)

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
                    _scan_status["processed"] = processed
                    _scan_status["progress"] = min(99, int(processed / total * 100) if total else 0)
                if failed_count > max(5, int(total * 0.05)):
                    raise RuntimeError(f"Emby 请求失败过多，已停止扫描（失败 {failed_count} 项）")
                await asyncio.sleep(0.4)

        results.sort(key=lambda x: (-_safe_int(x.get("gap_count"), 0), x.get("series_name", "")))
        skipped_ids = {str(item.get("id") or "") for item in skipped_libraries}
        scannable_ids = {str(lib.get("ItemId") or "") for lib, _ in scannable_libraries}
        summary = {
            "series_count": len(results),
            "gap_count": sum(_safe_int(item.get("gap_count"), 0) for item in results),
            "library_count": len(tv_libraries),
            "scanned_library_count": len(scannable_ids - skipped_ids),
            "skipped_library_count": len(skipped_libraries),
            "skipped_libraries": skipped_libraries,
            "processed": processed,
        }
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


async def _hydrate_cache_if_needed():
    if _scan_status.get("results") or _scan_status.get("is_scanning"):
        return
    results, summary, created_at = await load_gap_cache()
    if results:
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
    _scan_task = asyncio.create_task(_scan_gaps_task())
    return _success({"started": True}, "已启动缺集扫描")


@router.get("/scan/progress")
async def gap_scan_progress():
    """获取缺集扫描进度和最新结果。"""
    await _hydrate_cache_if_needed()
    return _success(dict(_scan_status))


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
    if not payload.series_id or payload.season_number < 0 or payload.episode_number <= 0:
        return _error("缺少剧集或集数参数")
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
    if start <= 0:
        return []
    if end <= 0:
        end = start
    if end < start:
        start, end = end, start
    return [ep for ep in range(start, end + 1) if ep in targets]


def _episode_match_ratio(title: str, season: int, episodes: list[int]) -> tuple[float, list[int]]:
    text = _normalise_title(title)
    if not text or not episodes:
        return 0.0, []

    season = _safe_int(season, 0)
    targets = sorted({_safe_int(ep, 0) for ep in episodes if _safe_int(ep, 0) > 0})
    target_set = set(targets)
    if season < 0 or not targets:
        return 0.0, []

    season_tokens = [
        f"s{season:02d}",
        f"s{season}",
        f"season {season}",
        f"season.{season}",
        f"第{season}季",
    ]
    if season == 0:
        season_tokens.extend(["specials", "special", "特别篇", "特别", "番外", "ova"])
    explicit_seasons = {
        _safe_int(x, 0)
        for x in re.findall(r"(?<![a-z0-9])s0?(\d{1,2})(?!\d)", text)
    }
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
    has_target_season = (
        season in explicit_seasons
        or any(token in text for token in season_tokens if token)
        or (season > 0 and collection_count >= season)
    )
    if season == 0 and re.search(r"(?<![a-z0-9])sp(?![a-z0-9])", text):
        has_target_season = True
    if explicit_seasons and season not in explicit_seasons:
        return 0.0, []

    matched: set[int] = set()
    for match in re.finditer(rf"(?<![a-z0-9])s0?{season}[ ._-]*e0?(\d{{1,3}})(?:\s*(?:-|~|至|到)\s*e?0?(\d{{1,3}}))?", text):
        matched.update(_episode_range(_safe_int(match.group(1)), _safe_int(match.group(2)), target_set))

    if has_target_season:
        for match in re.finditer(r"(?<![a-z0-9])e0?(\d{1,3})(?:\s*(?:-|~|至|到)\s*e?0?(\d{1,3}))?(?!\d)", text):
            matched.update(_episode_range(_safe_int(match.group(1)), _safe_int(match.group(2)), target_set))
        for match in re.finditer(r"第\s*(\d{1,3})(?:\s*(?:-|~|至|到)\s*(\d{1,3}))?\s*[集话話]", text):
            matched.update(_episode_range(_safe_int(match.group(1)), _safe_int(match.group(2)), target_set))
        for match in re.finditer(r"(?:更新到|更新至|更到|更至|至|到)\s*(\d{1,3})\s*[集话話]", text):
            matched.update(_episode_range(1, _safe_int(match.group(1)), target_set))

    if matched:
        return len(matched) / max(1, len(targets)), sorted(matched)
    if has_target_season and not re.search(r"(?<![a-z0-9])e\d{1,3}(?!\d)", text):
        return (1.0 if len(targets) > 1 else 0.65), targets
    return 0.0, []


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


def _annotate_gap_match(result: dict, season: int, episodes: list[int]) -> dict:
    item = dict(result)
    ratio, matched = _episode_match_ratio(_result_match_text(item), season, episodes)
    item["ui_episode_match_ratio"] = ratio
    item["ui_episode_match_text"] = f"{len(matched)}/{len(episodes)}" if episodes else "0/0"
    item["ui_matched_episodes"] = matched
    return item


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
        if not _is_gap_transfer_success_status(status):
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


def _search_keywords(series_name: str, season: int, episodes: list[int]) -> list[str]:
    name = series_name.strip()
    if not name:
        return []
    episodes = sorted({_safe_int(x, 0) for x in episodes if _safe_int(x, 0) > 0})
    if len(episodes) == 1:
        ep = episodes[0]
        if season == 0:
            return [
                f"{name} S00E{ep:02d}",
                f"{name} S00",
                f"{name} 特别篇",
                f"{name} SP",
            ]
        return [
            f"{name} S{season:02d}E{ep:02d}",
            f"{name} S{season:02d}",
            f"{name} 第{season}季 第{ep}集",
        ]
    if season == 0:
        return [
            f"{name} S00",
            f"{name} 特别篇",
            f"{name} Specials",
            name,
        ]
    return [
        f"{name} S{season:02d}",
        f"{name} 第{season}季",
        name,
    ]


@router.post("/search_mp")
async def search_gap_moviepilot(payload: GapSearchPayload):
    episodes = sorted({_safe_int(x, 0) for x in payload.episodes if _safe_int(x, 0) > 0})
    if not episodes:
        return _error("缺少待补集数", {"genes": [], "results": []})

    mp = MoviePilotClient()
    genes: list[dict] = []
    merged: list[dict] = []
    errors: list[str] = []
    try:
        for keyword in _search_keywords(payload.series_name, payload.season, episodes):
            try:
                results = await mp.search(keyword, media_type="tv")
                genes.append({"keyword": keyword, "count": len(results)})
                merged.extend(results)
            except RuntimeError as e:
                errors.append(str(e))
                break
            except Exception as e:
                errors.append(f"{keyword}: {e}")
        annotated = [_annotate_gap_match(item, payload.season, episodes) for item in _dedupe_results(merged)]
        annotated.sort(
            key=lambda x: (
                -float(x.get("ui_episode_match_ratio") or 0),
                -_safe_int(x.get("seeders"), 0),
                -_safe_int(x.get("size"), 0),
            )
        )
        if errors and not annotated:
            return _error("; ".join(errors), {"genes": genes, "results": []})
        return _success({"genes": genes, "results": annotated[:80], "errors": errors})
    finally:
        await mp.close()


@router.post("/search_hdhive")
async def search_gap_hdhive(payload: GapSearchPayload):
    hd = HDHiveClient()
    try:
        tmdb_id = _safe_int(payload.tmdb_id, 0)
        results = await hd.search(
            keyword=payload.series_name,
            tmdb_id=tmdb_id,
            emby_item_id=payload.series_id,
            media_type="tv",
        )
        episodes = sorted({_safe_int(x, 0) for x in payload.episodes if _safe_int(x, 0) > 0})
        annotated = [_annotate_gap_match(item, payload.season, episodes) for item in _dedupe_results(results)]
        annotated = await _apply_gap_transfer_marks(annotated)
        annotated.sort(
            key=lambda x: (
                -float(x.get("ui_episode_match_ratio") or 0),
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


@router.post("/download")
async def download_gap_moviepilot(payload: GapDownloadPayload):
    result = dict(payload.result or {})
    torrent_url = payload.torrent_url or result.get("enclosure") or result.get("download_url") or ""
    if not torrent_url:
        return _error("缺少下载链接")

    mp = MoviePilotClient()
    try:
        resp = await mp.download(torrent_url, torrent_info=result, tmdbid=_safe_int(payload.tmdb_id, 0))
        if resp and resp.get("success"):
            return _success(resp, "MoviePilot 下载已提交")
        return _error(resp.get("message", "MoviePilot 下载提交失败") if resp else "MoviePilot API 无响应", resp)
    except Exception as e:
        return _error(f"MoviePilot 下载异常: {e}")
    finally:
        await mp.close()


@router.post("/download_hdhive")
async def download_gap_hdhive(payload: GapDownloadPayload):
    result = payload.result or {}
    slug = payload.slug or result.get("slug") or result.get("id") or ""
    if not slug:
        return _error("缺少影巢资源标识")

    hd = HDHiveClient()
    try:
        folder_id = payload.folder_id or settings.cloud115_folder_id or "0"
        resp = await hd.unlock_and_transfer(slug, folder_id)
        if not resp:
            await _notify_gap_hdhive_transfer(payload, {}, False, "影巢 API 无响应")
            return _error("影巢 API 无响应")
        status = _gap_transfer_status(resp)
        if status == "error":
            message = resp.get("message", "影巢转存失败")
            await _notify_gap_hdhive_transfer(payload, resp, False, message)
            return _error(message, resp)
        ok = _is_gap_transfer_success_status(status)
        if not ok:
            message = resp.get("message") or f"影巢转存未完成: {status or '未知状态'}"
            await _notify_gap_hdhive_transfer(payload, resp, False, message)
            return _error(message, resp)
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
        await _notify_gap_hdhive_transfer(payload, resp, ok, resp.get("message", ""))
        return _success(resp, resp.get("message") or "影巢转存已提交")
    except Exception as e:
        await _notify_gap_hdhive_transfer(payload, {"status": "error"}, False, f"影巢转存异常: {e}")
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
