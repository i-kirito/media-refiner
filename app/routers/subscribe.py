"""订阅洗版 - 规则驱动自动匹配下载"""

from __future__ import annotations
import asyncio
import uuid
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import unquote, urlparse
from fastapi import APIRouter, HTTPException, Body
from app.config import settings
from pydantic import BaseModel
from app.models.schemas import SubscribeRule
from app.database import (
    save_subscribe_rule, list_subscribe_rules, get_subscribe_rule,
    delete_subscribe_rule, add_subscribe_log, list_subscribe_logs,
    get_subscribe_logs_for_item, load_quality_cache,
    add_subscribe_review, list_subscribe_reviews, update_subscribe_review, count_pending_reviews,
    clear_subscribe_reviews, get_subscribe_review,
    add_subscribe_ignore, remove_subscribe_ignore, list_subscribe_ignores, get_subscribe_ignore_ids,
    has_processed_item, delete_subscribe_history,
)
from app.services.emby import EmbyClient
from app.services.moviepilot import MoviePilotClient
from app.services.hdhive import HDHiveClient
from app.services.telegram import TelegramNotifier, _fmt_bytes, _fmt_resolution, _fmt_codec, _fmt_hdr, _fmt_quality
from app.services.quality_scanner import parse_filename_resolution, parse_filename_codec, parse_filename_video_range

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/subscribe", tags=["订阅洗版"])

_run_status = {"running": False, "progress": 0, "current": "", "total": 0, "processed": 0}
_path_size_cache: dict[str, tuple[float, int]] = {}
_PATH_SIZE_CACHE_TTL = 600.0
_STRM_READ_LIMIT = 8192


class HistoryDownloadControlPayload(BaseModel):
    action: str
    delete_files: bool = True


def _review_already_handled_response(review: dict) -> dict:
    """审核项已不在 pending 时返回幂等响应，避免前端/旧按钮弹错误。"""
    status = review.get("status", "")
    label = {
        "approved": "已通过",
        "rejected": "已拒绝",
        "ignored": "已忽略",
        "failed": "已失败",
    }.get(status, "已处理")
    message = review.get("message") or ""
    text = f"审核项{label}: {message}" if message else f"审核项{label}"
    return {
        "status": "success",
        "message": text,
        "data": {"already_handled": True, "review_status": status},
    }


def _should_cleanup_history_download_task(logs: list[dict]) -> bool:
    """失败的缺集下载历史删除时，同步尝试清理下载器任务。"""
    markers = (
        "缺集 MP 下载失败",
        "拆包筛选未确认",
        "qBittorrent",
        "Transmission",
        "downloader=",
        "hash=",
        "torrent_id=",
        "未在种子文件中识别到目标集",
    )
    for log in logs or []:
        if log.get("rule_name") != "缺集管理" or log.get("action") != "error":
            continue
        message = str(log.get("message") or "")
        if any(marker in message for marker in markers):
            return True
    return False


def _history_resource_title(message: str) -> str:
    match = re.search(r"资源[:：]\s*(.*?)(?:\s+·\s+|$)", str(message or ""))
    return match.group(1).strip() if match else ""


def _history_seasons(text: str) -> set[int]:
    value = str(text or "")
    seasons: set[int] = set()
    for match in re.finditer(r"(?<![a-z0-9])s0?(\d{1,2})\s*(?:-|~|至|到)\s*s?0?(\d{1,2})(?![a-z0-9])", value, re.I):
        start = int(match.group(1))
        end = int(match.group(2))
        seasons.update(range(min(start, end), max(start, end) + 1))
    for match in re.finditer(r"(?<![a-z0-9])s0?(\d{1,2})(?![a-z0-9])", value, re.I):
        seasons.add(int(match.group(1)))
    for match in re.finditer(r"第\s*0?(\d{1,2})\s*季", value, re.I):
        seasons.add(int(match.group(1)))
    return seasons


def _history_download_match_consistent(item: dict) -> bool:
    expected = _history_seasons(
        " ".join([
            str(item.get("_history_item_name") or ""),
            str(item.get("_history_resource_title") or item.get("title") or item.get("name") or ""),
        ])
    )
    actual = _history_seasons(str(item.get("ui_download_name") or ""))
    return not expected or not actual or bool(expected & actual)


def _transfer_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _transfer_candidate_names(result: dict, resp: dict | None) -> list[str]:
    names: list[str] = []
    for source in (result or {}, (resp or {}).get("data") if isinstance(resp, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("title", "name", "file_name", "resource_title", "slug", "_resource_url"):
            value = str(source.get(key) or "").strip()
            if value and not value.startswith(("http://", "https://")):
                names.append(value)
    return list(dict.fromkeys(names))


def _transfer_names_match(target_names: list[str], candidate_names: list[str]) -> bool:
    normalized_targets = [_transfer_match_text(name) for name in target_names if _transfer_match_text(name)]
    normalized_candidates = [_transfer_match_text(name) for name in candidate_names if _transfer_match_text(name)]
    for target in normalized_targets:
        for candidate in normalized_candidates:
            if len(candidate) >= 4 and (candidate in target or target in candidate):
                return True
    return False


async def _verify_hdhive_transfer_visible(result: dict, resp: dict | None) -> dict:
    """订阅转存只在 115 目标目录能看到资源时才算成功。"""
    if not settings.cloud115_cookie:
        return {"ok": False, "message": "未配置 115 Cookie，无法确认转存文件已落地"}
    if not isinstance(resp, dict):
        return {"ok": False, "message": "缺少转存响应，无法确认 115 文件"}

    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    folder_id = str(data.get("_target_folder") or settings.symedia_parent_id or settings.cloud115_folder_id or "0")
    candidate_names = _transfer_candidate_names(result, resp)

    from app.services.cloud115 import Cloud115Client

    c115 = Cloud115Client()
    try:
        share_code = str(data.get("_share_code") or "").strip()
        if share_code:
            share = await c115.list_share_files(
                share_code,
                str(data.get("_receive_code") or ""),
                recursive=False,
            )
            if isinstance(share, dict) and share.get("state"):
                for item in share.get("data") or []:
                    if isinstance(item, dict):
                        name = str(item.get("n") or item.get("name") or item.get("file_name") or "").strip()
                        if name:
                            candidate_names.append(name)

        candidate_names = list(dict.fromkeys(candidate_names))
        if not candidate_names:
            return {"ok": False, "message": "缺少资源名称，无法确认 115 文件"}

        last_names: list[str] = []
        for attempt in range(5):
            listing = await c115.list_files(folder_id, limit=115)
            if isinstance(listing, dict) and listing.get("state"):
                last_names = [
                    str(item.get("n") or item.get("name") or item.get("file_name") or "").strip()
                    for item in (listing.get("data") or [])
                    if isinstance(item, dict)
                ]
                if _transfer_names_match(last_names, candidate_names):
                    return {
                        "ok": True,
                        "message": "115 目标目录已确认资源文件",
                        "folder_id": folder_id,
                        "matched_names": last_names[:8],
                        "attempts": attempt + 1,
                    }
            await asyncio.sleep(2)
        return {
            "ok": False,
            "message": "115 目标目录未找到刚转存的资源文件",
            "folder_id": folder_id,
            "candidate_names": candidate_names[:8],
            "sample_names": last_names[:8],
        }
    finally:
        await c115.close()


async def _annotate_project_download_logs(logs: list[dict]) -> list[dict]:
    """只给本项目历史里的下载日志关联下载器状态，不展示外部手动任务。"""
    probes: list[dict] = []
    indexes: list[int] = []
    for idx, log in enumerate(logs or []):
        if log.get("action") != "download" or not log.get("item_id"):
            continue
        title = _history_resource_title(str(log.get("message") or "")) or str(log.get("item_name") or "")
        if not title:
            continue
        probes.append({
            "title": title,
            "name": title,
            "_history_item_name": str(log.get("item_name") or ""),
            "_history_resource_title": title,
        })
        indexes.append(idx)

    if not probes:
        return logs

    annotated: list[dict] = []
    mp = MoviePilotClient()
    try:
        annotated = await mp.annotate_downloader_statuses(probes[:120])
    except Exception:
        logger.exception("[Subscribe] 读取项目下载状态失败")
        return logs
    finally:
        await mp.close()

    updated = [dict(log) for log in logs]
    for idx, item in zip(indexes[:len(annotated)], annotated):
        if not item.get("ui_download_task"):
            continue
        if not _history_download_match_consistent(item):
            continue
        updated[idx].update({
            "download_task": True,
            "download_status": item.get("ui_download_status") or "",
            "download_label": item.get("ui_download_label") or "",
            "download_progress": item.get("ui_download_progress", 0),
            "download_speed": item.get("ui_download_speed", 0),
            "download_eta": item.get("ui_download_eta", 0),
            "download_name": item.get("ui_download_name") or "",
            "download_id": item.get("ui_download_id") or "",
            "download_hash": item.get("ui_download_hash") or "",
            "download_state": item.get("ui_download_state") or "",
            "download_downloader": item.get("ui_downloader") or "",
            "download_downloader_type": item.get("ui_downloader_type") or "",
        })
    return updated


def _normalise_codec(value: str) -> str:
    """统一编码命名，便于当前文件和搜索结果比较。"""
    text = (value or "").lower()
    if re.search(r"\bav1\b", text):
        return "av1"
    if re.search(r"\b(?:h[ ._-]?265|x265|hevc)\b", text):
        return "hevc"
    if re.search(r"\b(?:h[ ._-]?264|x264|avc)\b", text):
        return "h264"
    if re.search(r"\bmpeg[ ._-]?4\b", text):
        return "mpeg4"
    if re.search(r"\bmpeg[ ._-]?2\b", text):
        return "mpeg2"
    if re.search(r"\bvc[ ._-]?1\b", text):
        return "vc1"
    return ""


def _normalise_video_range(value: str) -> str:
    """统一 HDR/SDR 命名。"""
    text = (value or "").lower()
    if re.search(r"\b(?:dovi|dolby[ ._-]?vision|dv)\b", text):
        return "dolby_vision"
    if re.search(r"\bhdr10\+?\b|\bhdr\b|\bhlg\b", text):
        return "hdr"
    if re.search(r"\bsdr\b", text):
        return "sdr"
    return ""


def _get_video_codec(res: dict) -> str:
    """从搜索结果提取编码信息（hevc / h264 / av1 / etc）"""
    # MP: video_encode (mapped from meta.video_encode)
    codec = _normalise_codec(res.get("video_encode") or res.get("video_codec") or "")
    if codec:
        return codec
    # 标题/简介兜底，覆盖 MP 与 HDHive
    return _normalise_codec(" ".join([
        str(res.get("title") or ""),
        str(res.get("name") or ""),
        str(res.get("description") or ""),
        str(res.get("remark") or ""),
    ]))


def _get_video_range(res: dict) -> str:
    """从搜索结果提取 HDR 信息（hdr / dolby_vision / sdr）"""
    # 直接字段
    vr = _normalise_video_range(res.get("video_range") or "")
    if vr:
        return vr
    return _normalise_video_range(" ".join([
        str(res.get("title") or ""),
        str(res.get("name") or ""),
        str(res.get("description") or ""),
        str(res.get("remark") or ""),
    ]))


def _extract_quality(res: dict) -> str:
    """从搜索结果提取质量标记字符串"""
    # MP 结果: resolution 字段 ("4K", "1080p")
    q = (res.get("resolution") or "").lower()
    if q:
        return q
    # HDHive 结果: video_resolution 数组 (["4K", "1080p"])
    vr = res.get("video_resolution") or []
    if vr and isinstance(vr, list):
        return vr[0].lower()
    return ""


def _media_type_from_item(item: dict) -> str:
    """Infer the media type used by HDHive/Symedia from an Emby quality-cache item."""
    item_type = (item.get("type") or "").strip().lower()
    if item_type in ("series", "season", "episode", "tv", "tvshow"):
        return "tv"
    return "movie"


def _extract_height(quality_str: str) -> int:
    """从质量标记提取高度值，用于比较"更好" """
    q = quality_str.lower()
    if any(k in q for k in ["2160", "4k", "uhd"]):
        return 2160
    if "1080" in q:
        return 1080
    if "720" in q:
        return 720
    if "480" in q:
        return 480
    if "360" in q:
        return 360
    return 0


def _height_from_resolution(resolution: str) -> int:
    """从 3840x2160 / 2160p / 4K 等字符串提取高度。"""
    text = (resolution or "").lower()
    if "x" in text:
        try:
            return int(text.rsplit("x", 1)[-1])
        except ValueError:
            pass
    return _extract_height(text)


def _resolution_label(width: int, height: int) -> str:
    if width > 0 and height > 0:
        return f"{width}x{height}"
    return ""


def _pick_video_stream(sources: list[dict]) -> tuple[dict, dict]:
    """从 MediaSources 中挑出首个可用视频流。"""
    for source in sources or []:
        for stream in (source.get("MediaStreams") or []):
            if stream.get("Type") == "Video":
                return source, stream
    return {}, {}


def _is_strm_file(path: str) -> bool:
    if not path:
        return False
    return path.lower().split("?", 1)[0].split("#", 1)[0].endswith(".strm")


def _is_bluray_directory(current: dict) -> bool:
    """识别蓝光目录型电影。"""
    path = str(current.get("path") or "").lower()
    if "/bdmv/" in path or path.endswith("/bdmv"):
        return True
    return "bluray" in str(current.get("container") or "").lower()


def _positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _local_media_path_size(path: str) -> int:
    """Emby 对蓝光目录常给 0，这里在本容器能访问路径时按文件求和。"""
    path = str(path or "").strip()
    if not path or "\0" in path:
        return 0

    now = time.monotonic()
    cached = _path_size_cache.get(path)
    if cached and now - cached[0] < _PATH_SIZE_CACHE_TTL:
        return cached[1]

    total = 0
    try:
        if os.path.isfile(path):
            total = _file_size_with_strm_target(path, set())
        elif os.path.isdir(path):
            top_level_strm_size = _top_level_strm_targets_size(path)
            if top_level_strm_size:
                _path_size_cache[path] = (now, top_level_strm_size)
                return top_level_strm_size

            stack = [path]
            seen_dirs: set[tuple[int, int]] = set()
            seen_files: set[tuple[int, int] | str] = set()
            while stack:
                root = stack.pop()
                try:
                    root_stat = os.stat(root)
                except OSError:
                    continue
                root_key = (root_stat.st_dev, root_stat.st_ino)
                if root_key in seen_dirs:
                    continue
                seen_dirs.add(root_key)
                try:
                    with os.scandir(root) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=True):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=True):
                                    total += _file_size_with_strm_target(entry.path, seen_files)
                            except OSError:
                                continue
                except OSError:
                    continue
    except OSError:
        total = 0

    _path_size_cache[path] = (now, total)
    return total


def _read_strm_target(path: str) -> str:
    if not path.lower().endswith(".strm"):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as file:
            text = file.read(_STRM_READ_LIMIT)
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parsed = urlparse(line)
        if parsed.scheme in {"http", "https", "rtsp", "rtmp", "magnet"}:
            return ""
        if parsed.scheme == "file":
            return unquote(parsed.path or "")
        if parsed.scheme and len(parsed.scheme) > 1:
            return ""
        return line
    return ""


def _file_identity(path: str) -> tuple[int, int] | str:
    try:
        stat = os.stat(path)
        return (stat.st_dev, stat.st_ino)
    except OSError:
        return os.path.realpath(path)


def _file_size_with_strm_target(path: str, seen_files: set[tuple[int, int] | str]) -> int:
    target = _read_strm_target(path)
    media_path = target if target and os.path.exists(target) else path
    identity = _file_identity(media_path)
    if identity in seen_files:
        return 0
    seen_files.add(identity)
    try:
        if os.path.isdir(media_path):
            return _local_media_path_size(media_path)
        return os.path.getsize(media_path)
    except OSError:
        return 0


def _top_level_strm_targets_size(path: str) -> int:
    total = 0
    seen_files: set[tuple[int, int] | str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=True) or not entry.name.lower().endswith(".strm"):
                        continue
                    target = _read_strm_target(entry.path)
                    if target and os.path.exists(target):
                        total += _file_size_with_strm_target(target, seen_files)
                except OSError:
                    continue
    except OSError:
        return 0
    return total


async def _resolve_current_size_bytes(current: dict, item: dict, source: dict) -> int:
    size = (
        _positive_int(source.get("Size") if source else 0)
        or _positive_int(item.get("Size") if item else 0)
        or _positive_int(current.get("size_bytes"))
    )
    if size:
        return size

    path = str(
        (source or {}).get("Path")
        or (item or {}).get("Path")
        or current.get("path")
        or ""
    )
    if not path:
        return 0
    return await asyncio.to_thread(_local_media_path_size, path)


async def _hydrate_current_quality_from_emby(current: dict) -> dict:
    """缺关键质量字段时，尝试从 Emby 实时拉取媒体流信息。"""
    emby_id = str(current.get("emby_id") or "").strip()
    if not emby_id:
        return current

    emby = EmbyClient()
    try:
        item = await emby.get_item(
            emby_id,
            fields="MediaSources,Path,MediaStreams,ProviderIds,ProductionYear,DateCreated,MediaSourceCount,ChildCount,Size,Container,OriginalTitle,SortName"
        )
        if not item:
            current["emby_missing"] = True
            return current
        playback = await emby.get_playback_info(emby_id)
    except Exception as exc:
        logger.warning("[Subscribe] hydrate current quality failed for %s: %s", emby_id, exc)
        return current
    finally:
        await emby.close()

    playback = playback or {}
    current["metadata_source"] = "emby"
    current["name"] = item.get("Name") or current.get("name") or ""
    if item.get("ProductionYear") and not current.get("year"):
        current["year"] = item.get("ProductionYear")
    if item.get("Path") and not current.get("path"):
        current["path"] = item.get("Path")
    if item.get("ProviderIds") and not current.get("provider_ids"):
        current["provider_ids"] = item.get("ProviderIds")

    item_sources = item.get("MediaSources") or []
    playback_sources = playback.get("MediaSources") or []
    source, video_stream = _pick_video_stream(item_sources)
    if not source and item_sources:
        source = item_sources[0]
    if not video_stream:
        pb_source, pb_video_stream = _pick_video_stream(playback_sources)
        if pb_source:
            source = pb_source
        elif not source and playback_sources:
            source = playback_sources[0]
        video_stream = pb_video_stream

    if source:
        current["media_source_name"] = source.get("Name") or current.get("media_source_name") or ""
        current["container"] = source.get("Container") or current.get("container") or ""
        source_path = source.get("Path") or item.get("Path") or current.get("path") or ""
        if source_path:
            current["path"] = source_path

    size = await _resolve_current_size_bytes(current, item, source)
    if size:
        current["size_bytes"] = size
        if _positive_int(source.get("Size") if source else 0) or _positive_int(item.get("Size")):
            current["size_source"] = "emby"
        else:
            current["size_source"] = "filesystem"

    if video_stream:
        width = int(video_stream.get("Width") or 0)
        height = int(video_stream.get("Height") or 0)
        if width > 0 and height > 0:
            current["resolution"] = _resolution_label(width, height)
        codec = video_stream.get("Codec") or ""
        if codec:
            current["video_codec"] = codec
        video_range = video_stream.get("VideoRange") or ""
        if video_range:
            current["video_range"] = video_range

    if _is_bluray_directory(current) and not video_stream and not current.get("size_bytes"):
        current["metadata_note"] = "Emby 元数据：蓝光目录未提供视频流/大小"

    return current


async def _normalise_current_quality_for_display(review: dict) -> dict:
    """修正审核卡片里的当前质量，兼容旧缓存、STRM 与蓝光目录条目。"""
    current = dict((review.get("current_quality") or {}))
    if not current.get("emby_id") and review.get("item_id"):
        current["emby_id"] = str(review.get("item_id") or "")
    current = await _hydrate_current_quality_from_emby(current)

    path = current.get("path", "")
    parsed_res = parse_filename_resolution(path)
    if parsed_res and _is_strm_file(path):
        current["resolution"] = _resolution_label(*parsed_res)
    if not current.get("video_codec"):
        codec = parse_filename_codec(path)
        if codec:
            current["video_codec"] = codec
    if not current.get("video_range"):
        video_range = parse_filename_video_range(path)
        if video_range:
            current["video_range"] = video_range
    if (not current.get("resolution") or current.get("resolution") == "0x0") and _is_bluray_directory(current):
        current["resolution"] = "蓝光目录"
    if not current.get("video_codec") and _is_bluray_directory(current):
        current["video_codec"] = "原盘目录"
    if not current.get("size_bytes"):
        current["size_bytes"] = 0
    if _is_bluray_directory(current) and not current.get("metadata_note") and not current.get("size_bytes"):
        current["metadata_note"] = "Emby 元数据：蓝光目录未提供视频流/大小"
    review = dict(review)
    review["current_quality"] = current
    return review


def _candidate_height(res: dict) -> int:
    """提取搜索结果高度，字段缺失时回退标题。"""
    return (
        _extract_height(_extract_quality(res))
        or _extract_height(str(res.get("title") or ""))
        or _extract_height(str(res.get("name") or ""))
    )


def _current_height(item: dict) -> int:
    """提取当前媒体高度，缓存字段缺失时回退文件名。"""
    path = item.get("path", "")
    parsed_res = parse_filename_resolution(path)
    if parsed_res and _is_strm_file(path):
        return parsed_res[1]
    return _height_from_resolution(item.get("resolution", "")) or (parsed_res[1] if parsed_res else 0)


def _current_codec(item: dict) -> str:
    """提取当前媒体编码，兼容 STRM 仅文件名有编码的情况。"""
    return _normalise_codec(item.get("video_codec", "")) or _normalise_codec(item.get("path", ""))


def _current_video_range(item: dict) -> str:
    """提取当前媒体 HDR/SDR 标记。"""
    return _normalise_video_range(item.get("video_range", "")) or _normalise_video_range(item.get("path", ""))


def _extract_fps(text: str) -> int:
    """从标题/文件名中提取 fps；未知返回 0。"""
    m = re.search(r"(?<!\d)([1-9]\d{1,2})\s*(?:fps|帧)", (text or "").lower())
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _candidate_fps(res: dict) -> int:
    """提取搜索结果帧率。"""
    raw = res.get("_raw") or {}
    meta = raw.get("meta_info") if isinstance(raw, dict) else {}
    fps = meta.get("fps") if isinstance(meta, dict) else None
    try:
        if fps:
            return int(fps)
    except (TypeError, ValueError):
        pass
    return _extract_fps(" ".join([
        str(res.get("title") or ""),
        str(res.get("name") or ""),
        str(res.get("description") or ""),
        str(res.get("remark") or ""),
    ]))


def _current_is_remux(item: dict) -> bool:
    return "remux" in (item.get("path") or "").lower()


def _result_size_bytes(res: dict) -> int:
    """提取搜索结果大小，支持 MP size 和 HDHive share_size。"""
    size = res.get("size")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    text = str(res.get("share_size") or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb|mb|kb)", text, re.I)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).lower()
    multipliers = {"tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024}
    return int(value * multipliers.get(unit, 1))


def _is_smaller_than_current(item: dict, res: dict) -> bool:
    """候选资源体积小于当前文件时直接过滤；任一体积未知则不拦截。"""
    cur_size = int(item.get("size_bytes", 0) or 0)
    new_size = _result_size_bytes(res)
    return bool(cur_size and new_size and new_size < cur_size)


def _is_meaningful_upgrade(item: dict, res: dict) -> bool:
    """
    判断搜索结果是否明确优于当前版本。
    目标清晰度达标只代表“可用”，这里避免 2160p/H265 → 2160p/H265 这类同质结果进入审核。
    """
    if _is_smaller_than_current(item, res):
        return False

    cur_h = _current_height(item)
    new_h = _candidate_height(res)
    if cur_h and new_h:
        if new_h < cur_h:
            return False
        if new_h > cur_h:
            return True
    elif new_h and not cur_h:
        return True

    codec_rank = {"mpeg2": 1, "mpeg4": 1, "vc1": 1, "h264": 2, "hevc": 3, "av1": 4}
    cur_codec = _current_codec(item)
    new_codec = _get_video_codec(res)
    cur_codec_rank = codec_rank.get(cur_codec, 0)
    new_codec_rank = codec_rank.get(new_codec, 0)
    if cur_codec and new_codec and new_codec_rank < cur_codec_rank:
        return False
    if new_codec and new_codec_rank > cur_codec_rank:
        return True

    hdr_rank = {"sdr": 1, "hdr": 2, "dolby_vision": 3}
    cur_hdr = _current_video_range(item) or "sdr"
    new_hdr = _get_video_range(res)
    if new_hdr and hdr_rank.get(new_hdr, 0) > hdr_rank.get(cur_hdr, 1):
        return True

    if _is_remux(res) and not _current_is_remux(item):
        return True

    cur_size = int(item.get("size_bytes", 0) or 0)
    new_size = _result_size_bytes(res)
    if cur_size and new_size and new_size >= cur_size * 1.2:
        return True

    cur_fps = _extract_fps(item.get("path", ""))
    new_fps = _candidate_fps(res)
    if cur_fps and new_fps and new_fps > cur_fps:
        return True

    return False


def _norm_imdb_id(value: str) -> str:
    """标准化 IMDb ID。"""
    value = (value or "").strip().lower()
    return value if value.startswith("tt") else ""


def _looks_like_series_pack(res: dict) -> bool:
    """Movie 条目搜索结果里出现季号时，通常是同名剧集误匹配。"""
    title = (res.get("title") or res.get("name") or "").lower()
    if re.search(r"\bs\d{1,2}\b", title):
        return True
    if re.search(r"\bseason\s*\d+\b", title):
        return True
    if re.search(r"第\s*[0-9一二三四五六七八九十]+\s*季", title):
        return True
    return False


def _identity_matches(item: dict, res: dict) -> bool:
    """用外部 ID 和条目形态做保守匹配，避免同名错配。"""
    item_imdb = _norm_imdb_id(item.get("imdb_id", ""))
    result_imdb = _norm_imdb_id(res.get("imdbid", ""))
    if item_imdb and result_imdb and item_imdb != result_imdb:
        return False
    if item.get("type") == "Movie" and _looks_like_series_pack(res):
        return False
    return True


def _is_remux(res: dict) -> bool:
    """检测搜索结果是否为 Remux 版本"""
    # HDHive: source[] 含 "Remux"
    sources = res.get("source") or []
    if isinstance(sources, list) and any("remux" in s.lower() for s in sources):
        return True
    # MP / 通用: title 含 "Remux"
    title = (res.get("title") or res.get("name") or "").lower()
    if "remux" in title:
        return True
    return False


def _is_4k(res: dict) -> bool:
    """检测搜索结果是否为 4K 分辨率"""
    q = _extract_quality(res)
    return _extract_height(q) >= 2160


def _has_subtitle(res: dict) -> bool:
    """检测搜索结果是否含字幕"""
    # HDHive
    if res.get("subtitle_language") or res.get("subtitle_type"):
        return True
    # MP
    subtitle = res.get("subtitle") or ""
    if subtitle and subtitle.lower() not in ("", "none", "false", "0"):
        return True
    return False


def _is_free_hdhive(res: dict) -> bool:
    """HDHive 自动转存只放行免费或已解锁资源，避免无人值守时消耗点数。"""
    if res.get("is_unlocked"):
        return True
    points = res.get("unlock_points")
    if points in (None, ""):
        return False
    try:
        return float(points) <= 0
    except (TypeError, ValueError):
        return False


def _format_resource_detail(res: dict, source: str = "") -> str:
    """从搜索结果构建详细文件信息字符串"""
    title = res.get("title") or res.get("name", "")
    src_label = "影巢" if source == "hdhive" else "MoviePilot"

    lines = []
    if title:
        lines.append(f"📄 {title}")

    if source == "hdhive":
        res_list = res.get("video_resolution") or []
        res_str = res_list[0] if res_list else "未知"
        src_list = res.get("source") or []
        src_str = ", ".join(src_list) if src_list else "未知"
        size = _fmt_bytes(res.get("share_size", 0))
        sub_lang = res.get("subtitle_language", "")
        sub_type = res.get("subtitle_type", "")
        sub_str = ""
        if sub_lang:
            sub_str = f"{sub_lang}"
            if sub_type:
                sub_str += f" ({sub_type})"
        uploader = res.get("user", {}).get("nickname", "") if isinstance(res.get("user"), dict) else ""
        lines.append(f"🖥 {res_str} | 🔗 {src_str} | 💾 {size}")
        if sub_str:
            lines.append(f"📝 字幕: {sub_str}")
        if uploader:
            lines.append(f"👤 上传: {uploader}")
    else:
        # MP
        res_str = _fmt_quality(res)
        codec = _fmt_codec(res.get("video_codec") or "")
        size = _fmt_bytes(res.get("size", 0))
        subtitle = res.get("subtitle", "")
        lines.append(f"🖥 {res_str} | 🔧 {codec if codec != '未知' else '?'} | 💾 {size}")
        if subtitle and subtitle.lower() not in ("", "none", "false", "0"):
            lines.append(f"📝 字幕: {subtitle}")

    return "\n".join(lines) if lines else ""


def _meets_target(extracted_quality: str, target: str) -> bool:
    """检查结果质量是否达到目标。未知质量 → 纳入（可能更好）"""
    q = extracted_quality.lower()
    if not q:
        return True  # 未知质量，纳入
    t = target.lower()
    if t in ("4k", "2160p"):
        return any(k in q for k in ["2160", "4k", "uhd"])
    if t == "1080p":
        if any(k in q for k in ["2160", "4k", "uhd"]):
            return True
        return "1080" in q
    if t == "720p":
        if any(k in q for k in ["2160", "4k", "uhd", "1080"]):
            return True
        return "720" in q
    return True  # 无目标限制


# ─── Cron 调度 ───

def _cron_field_match(field: str, value: int) -> bool:
    """检查单个 cron 字段是否匹配当前值（支持 * /N N N-M N,M）"""
    field = field.strip()
    if field == "*":
        return True
    # */N
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    # N-M
    if "-" in field and not field.startswith("*"):
        parts = field.split("-")
        if len(parts) == 2:
            try:
                return int(parts[0]) <= value <= int(parts[1])
            except ValueError:
                return False
    # N,M,...
    if "," in field:
        return any(_cron_field_match(p, value) for p in field.split(","))
    # 精确值
    try:
        return int(field) == value
    except ValueError:
        return False


def _match_cron(expr: str) -> bool:
    """检查 5 段 cron 表达式是否匹配当前时间"""
    if not expr or not expr.strip():
        return False
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    from datetime import datetime
    now = datetime.now()
    fields = [
        (parts[0], now.minute),   # minute 0-59
        (parts[1], now.hour),     # hour   0-23
        (parts[2], now.day),      # day    1-31
        (parts[3], now.month),    # month  1-12
        (parts[4], now.weekday()),# weekday 0=Mon,6=Sun (cron: 0/7=Sun)
    ]
    # cron weekday: 0=Sun,1=Mon...6=Sat → 转成 0=Mon,1=Tue...6=Sun
    cron_wday = (now.weekday() + 1) % 7  # 0=Sun,1=Mon...6=Sat
    fields[4] = (parts[4], cron_wday)
    for f, v in fields:
        if not _cron_field_match(f, v):
            return False
    return True


_cron_scheduler_task: asyncio.Task | None = None
_cron_last_triggered: dict[str, str] = {}  # rule_id -> YYYY-MM-DD HH:MM


async def _cron_scheduler_loop():
    """后台 cron 调度主循环：每分钟检查一次所有规则的 cron 表达式"""
    global _cron_last_triggered
    logger.info("[CronScheduler] 订阅 cron 调度器已启动")
    while True:
        try:
            rules = await list_subscribe_rules()
            now = datetime.now()
            now_key = now.strftime("%Y-%m-%d %H:%M")
            for rule in rules:
                if not rule.get("enabled", True):
                    continue
                expr = (rule.get("cron_expression") or "").strip()
                if not expr:
                    continue
                # 避免同一分钟内重复触发
                rid = rule["id"]
                if _cron_last_triggered.get(rid) == now_key:
                    continue
                if _match_cron(expr):
                    logger.info(f"[CronScheduler] 触发规则 [{rule.get('name','')}] cron={expr}")
                    _cron_last_triggered[rid] = now_key
                    # 检查是否有其他任务在运行
                    if _run_status["running"]:
                        logger.warning(f"[CronScheduler] 订阅任务正在运行，跳过规则 [{rule.get('name','')}]")
                        continue
                    asyncio.create_task(_run_matching([rule]))
        except Exception as e:
            logger.warning(f"[CronScheduler] 调度异常: {e}")
        await asyncio.sleep(60)


def start_cron_scheduler():
    """启动 cron 调度器（由 main.py 调用）"""
    global _cron_scheduler_task
    if _cron_scheduler_task is None or _cron_scheduler_task.done():
        _cron_scheduler_task = asyncio.create_task(_cron_scheduler_loop())


def stop_cron_scheduler():
    """停止 cron 调度器"""
    global _cron_scheduler_task
    if _cron_scheduler_task and not _cron_scheduler_task.done():
        _cron_scheduler_task.cancel()
        _cron_scheduler_task = None


# ─── 规则 CRUD ───

@router.get("/rules")
async def list_rules():
    """列出所有订阅规则"""
    rules = await list_subscribe_rules()
    return {"status": "success", "data": rules}


@router.post("/rules")
async def create_rule(rule: SubscribeRule):
    """创建订阅规则"""
    rule.id = f"SR-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rule.created_at = now
    rule.updated_at = now
    await save_subscribe_rule(rule.model_dump())
    return {"status": "success", "data": rule.model_dump()}


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str):
    """获取单个规则"""
    rule = await get_subscribe_rule(rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    return {"status": "success", "data": rule}


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, payload: dict = Body(...)):
    """更新订阅规则"""
    existing = await get_subscribe_rule(rule_id)
    if not existing:
        raise HTTPException(404, "规则不存在")
    merged = dict(existing)
    merged.update(payload or {})
    merged["id"] = rule_id
    data = SubscribeRule(**merged).model_dump()
    data["id"] = rule_id
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await save_subscribe_rule(data)
    return {"status": "success", "data": data}


@router.delete("/rules/{rule_id}")
async def remove_rule(rule_id: str):
    """删除订阅规则"""
    ok = await delete_subscribe_rule(rule_id)
    if not ok:
        raise HTTPException(404, "规则不存在")
    return {"status": "success", "message": "规则已删除"}


# ─── 手动执行 ───

@router.post("/run")
async def run_subscribe(rule_id: str = ""):
    """手动触发订阅匹配"""
    global _run_status
    if _run_status["running"]:
        raise HTTPException(400, "已有订阅任务正在运行")

    rules = await list_subscribe_rules()
    if rule_id:
        rules = [r for r in rules if r["id"] == rule_id]
    rules = [r for r in rules if r.get("enabled", True)]
    if not rules:
        raise HTTPException(400, "没有启用的订阅规则")

    asyncio.create_task(_run_matching(rules))
    return {"status": "success", "message": f"已启动 {len(rules)} 条规则"}


# ─── 审核列表 ───

@router.get("/reviews")
async def list_reviews(status: str = "pending"):
    """列出审核条目"""
    reviews = await list_subscribe_reviews(status)
    reviews = [await _normalise_current_quality_for_display(r) for r in reviews]
    return {"status": "success", "data": reviews}


@router.get("/reviews/count")
async def review_count():
    """待审核数量"""
    cnt = await count_pending_reviews()
    return {"status": "success", "data": {"pending": cnt}}


@router.post("/reviews/{review_id}/approve")
async def approve_review(review_id: int):
    """批准审核项并执行下载/转存"""
    review = await get_subscribe_review(review_id)
    if not review:
        raise HTTPException(404, "审核项不存在或已被清空")
    if review.get("status") != "pending":
        return _review_already_handled_response(review)

    src = review.get("source", "")
    item_name = review.get("item_name", "")
    result = review.get("search_result", {})
    action_type = review.get("action_type", "")

    try:
        if action_type == "download":
            mp = MoviePilotClient()
            try:
                torrent_url = result.get("enclosure", "")
                if not torrent_url:
                    raise ValueError("无下载链接")
                # 解析 TMDB ID 以帮助 MP 识别媒体
                tmdbid = 0
                item_id = review.get("item_id", "")
                if item_id:
                    try:
                        from app.services.emby import EmbyClient
                        emby = EmbyClient()
                        try:
                            emby_item = await emby.get_item(item_id)
                            if emby_item:
                                pid = emby_item.get("ProviderIds", {})
                                tmdb_str = pid.get("Tmdb", "")
                                if tmdb_str and tmdb_str.isdigit():
                                    tmdbid = int(tmdb_str)
                        finally:
                            await emby.close()
                    except Exception:
                        pass
                resp = await mp.download(torrent_url, torrent_info=result, tmdbid=tmdbid)
                if not resp or not resp.get("success"):
                    raise ValueError(f"下载提交失败: {resp.get('message', 'MP API 未返回成功') if resp else 'API 无响应'}")
                await update_subscribe_review(review_id, "approved", "下载已推送")
                await add_subscribe_log(review.get("rule_id", ""), review.get("rule_name", ""), "download", item_name, review.get("item_id", ""), f"审核通过 - MP 下载")
                try:
                    from app.services.telegram import _fmt_bytes
                    mp_title = result.get("title", "")
                    mp_size_str = _fmt_bytes(result.get("size", 0))
                    mp_res = _fmt_quality(result) or ""
                    parts = [f"⬇️ *MP 下载已推送*"]
                    if mp_title:
                        parts.append(f"\n📦 `{mp_title[:80]}`")
                    if mp_size_str and mp_size_str != "未知":
                        parts.append(f"💾 {mp_size_str}")
                    tg = TelegramNotifier()
                    await tg.send_message("\n".join(parts))
                    await tg.close()
                except Exception:
                    pass
            finally:
                await mp.close()

        elif action_type == "transfer":
            hd = HDHiveClient()
            try:
                slug = result.get("slug", "")
                if not slug:
                    raise ValueError("无转存标识")
                resp = await hd.unlock_and_transfer(slug)
                if not resp:
                    raise ValueError("转存失败（HDHive API 无响应）")
                status = resp.get("status", "")
                if status in ("transferred", "already_owned"):
                    verify = await _verify_hdhive_transfer_visible(result, resp)
                    if isinstance(resp, dict):
                        resp["cloud115_verify"] = verify
                    if not verify.get("ok"):
                        raise ValueError(verify.get("message") or "115 目标目录未确认转存文件")
                if status == "transferred":
                    await update_subscribe_review(review_id, "approved", "转存成功")
                    await add_subscribe_log(review.get("rule_id", ""), review.get("rule_name", ""), "transfer", item_name, review.get("item_id", ""), f"审核通过 - HDHive 转存成功")
                elif status == "already_owned":
                    await update_subscribe_review(review_id, "approved", "资源已在 115 中")
                    await add_subscribe_log(review.get("rule_id", ""), review.get("rule_name", ""), "transfer", item_name, review.get("item_id", ""), f"审核通过 - 资源已在 115 中")
                else:
                    err_msg = resp.get("message", f"转存异常: {status}")
                    raise ValueError(err_msg)
                # TG 通知 - 转存结果
                try:
                    tg = TelegramNotifier()
                    hd_title = result.get("title", "")
                    hd_size = result.get("share_size", "")
                    status_icon = "✅" if status == "transferred" else "ℹ️"
                    status_msg = "转存成功" if status == "transferred" else "已在 115 中"
                    parts = [f"{status_icon} *115 {status_msg}*"]
                    if hd_title:
                        parts.append(f"\n📦 `{hd_title[:80]}`")
                    if hd_size:
                        parts.append(f"💾 {hd_size}")
                    await tg.send_message("\n".join(parts))
                    await tg.close()
                except Exception:
                    pass
            finally:
                await hd.close()

        else:
            raise ValueError(f"未知操作类型: {action_type}")

        return {"status": "success", "message": "已批准并执行"}

    except Exception as e:
        await update_subscribe_review(review_id, "failed", str(e))
        return {"status": "error", "message": f"执行失败: {e}"}


@router.post("/reviews/{review_id}/reject")
async def reject_review(review_id: int):
    """拒绝审核项（仅拒绝此次结果，下次仍会搜索但排除此结果）"""
    review = await get_subscribe_review(review_id)
    if not review:
        raise HTTPException(404, "审核项不存在或已被清空")
    if review.get("status") != "pending":
        return _review_already_handled_response(review)
    await update_subscribe_review(review_id, "rejected", "用户拒绝")

    # 保存被拒绝的结果标识，下次搜索时排除
    from app.database import add_rejected_result
    sr = review.get("search_result") or {}
    item_id = review.get("item_id", "")
    result_key = sr.get("enclosure") or sr.get("slug") or sr.get("page_url") or ""
    result_label = sr.get("title") or _extract_quality(sr) or ""
    if item_id and result_key:
        await add_rejected_result(item_id, result_key, result_label)

    return {"status": "success", "message": "已拒绝，下次跳过此结果"}


@router.post("/reviews/{review_id}/ignore")
async def ignore_review(review_id: int):
    """忽略审核项，以后不再扫描此媒体（加入该规则的忽略列表）"""
    review = await get_subscribe_review(review_id)
    if not review:
        raise HTTPException(404, "审核项不存在或已被清空")
    if review.get("status") != "pending":
        return _review_already_handled_response(review)
    rule_id = review.get("rule_id", "")
    item_id = review.get("item_id", "")
    item_name = review.get("item_name", "")
    await add_subscribe_ignore(rule_id, item_id, item_name)
    await update_subscribe_review(review_id, "ignored", "已忽略，后续不再扫描")
    return {"status": "success", "message": "已忽略"}


@router.get("/ignored")
async def list_ignored(rule_id: str = ""):
    """列出被忽略的媒体项"""
    items = await list_subscribe_ignores(rule_id)
    return {"status": "success", "data": items}


@router.delete("/ignored/{rule_id}/{item_id}")
async def unignore_item(rule_id: str, item_id: str):
    """取消忽略（恢复扫描）"""
    await remove_subscribe_ignore(rule_id, item_id)
    return {"status": "success", "message": "已取消忽略"}


@router.delete("/history/{item_id}")
async def delete_item_history(item_id: str):
    """删除指定条目的所有历史记录（日志+审核+忽略），恢复可搜索状态"""
    logs = await get_subscribe_logs_for_item(item_id)
    cleanup = {"deleted": 0, "matched": 0, "message": ""}
    if _should_cleanup_history_download_task(logs):
        mp = MoviePilotClient()
        try:
            cleanup = await mp.cleanup_history_download_tasks(
                item_name=str((logs[0] or {}).get("item_name") or "") if logs else "",
                item_id=item_id,
                logs=logs,
                delete_files=False,
            )
        except Exception as e:
            logger.exception("[Subscribe] 删除历史联动清理下载器失败: %s", item_id)
            cleanup = {"deleted": 0, "matched": 0, "message": f"下载器清理失败: {e}"}
        finally:
            await mp.close()

    result = await delete_subscribe_history(item_id)
    cleanup_message = cleanup.get("message") or ""
    suffix = f"，{cleanup_message}" if cleanup_message else ""
    return {
        "status": "success",
        "message": f"已删除 {result['logs']} 条日志、{result['reviews']} 条审核、{result['ignores']} 条忽略{suffix}",
        "data": {**result, "download_cleanup": cleanup},
    }


@router.post("/history/{item_id}/download")
async def control_history_download(item_id: str, payload: HistoryDownloadControlPayload):
    """控制本项目历史记录关联的下载器任务。"""
    action = str(payload.action or "").strip().lower()
    if action not in {"pause", "resume", "delete"}:
        raise HTTPException(status_code=400, detail="不支持的下载器操作")

    logs = await get_subscribe_logs_for_item(item_id)
    download_logs = [log for log in logs or [] if log.get("action") == "download"]
    if not download_logs:
        raise HTTPException(status_code=404, detail="未找到该条目的下载历史")

    mp = MoviePilotClient()
    try:
        result = await mp.control_history_download_tasks(
            item_name=str((download_logs[0] or {}).get("item_name") or ""),
            item_id=item_id,
            logs=download_logs,
            action=action,
            delete_files=bool(payload.delete_files),
        )
    except Exception as e:
        logger.exception("[Subscribe] 控制历史下载器任务失败: %s", item_id)
        raise HTTPException(status_code=500, detail=f"下载器操作失败: {e}") from e
    finally:
        await mp.close()

    if not result.get("success"):
        raise HTTPException(status_code=404 if not result.get("matched") else 400, detail=result.get("message") or "下载器操作失败")
    return {
        "status": "success",
        "message": result.get("message") or "下载器操作已提交",
        "data": result,
    }


@router.get("/history")
async def list_history():
    """合并历史记录：subscribe_reviews（非 pending）+ subscribe_logs（已完成动作，按 item_id 去重取最新）"""
    from app.database import list_subscribe_logs as db_list_logs
    from app.database import list_recent_gap_transfer_marks

    reviews = await list_subscribe_reviews("history")
    logs = await db_list_logs(limit=500)
    gap_marks = await list_recent_gap_transfer_marks(limit=200)

    # 过滤已完成动作，按 item_id 去重（保留最新一条）。
    # 缺集管理需要同时展示同一季集的解锁和下载动作，因此按 item_id + action 去重。
    completed_actions = {"download", "transfer", "auto_approved", "unlock"}
    seen_items = set()
    unique_logs = []
    for log in reversed(logs):  # reversed 使最后一条（最新）覆盖前面
        action = log.get("action")
        is_gap_log = log.get("rule_name") == "缺集管理"
        if (action in completed_actions or (is_gap_log and action == "error")) and log.get("item_id"):
            iid = log["item_id"]
            dedupe_key = f"{iid}|{action}" if is_gap_log else iid
            if dedupe_key not in seen_items:
                seen_items.add(dedupe_key)
                unique_logs.append(log)
    # unique_logs 现在是按 created_at 升序，反转一下让最新的在前面
    unique_logs.reverse()

    existing_gap_keys = {f"{log.get('item_id')}|{log.get('action')}" for log in unique_logs if log.get("rule_name") == "缺集管理"}
    for mark in gap_marks:
        season = int(mark.get("season_number") or 0)
        episodes = mark.get("episodes") or []
        episode_key = "-".join(f"{int(ep):02d}" for ep in episodes if str(ep).isdigit()) or "all"
        base_key = f"gap:name:{(mark.get('series_name') or '').strip().lower() or 'unknown'}:s{season:02d}:{episode_key}"
        status = str(mark.get("status") or "").strip().lower()
        action = "unlock" if status == "unlocked" else "transfer"
        dedupe_key = f"{base_key}|{action}"
        if dedupe_key in existing_gap_keys:
            continue
        title = (mark.get("series_name") or mark.get("resource_title") or "缺集资源").strip() or "缺集资源"
        episode_text = f"S{season:02d}"
        if episodes:
            episode_text = f"S{season:02d} " + ", ".join(f"E{int(ep):02d}" for ep in episodes[:12] if str(ep).isdigit())
        status_text = {
            "unlocked": "已解锁",
            "transferred": "已转存",
            "already_owned": "已在 115",
            "submitted": "已提交转存",
            "success": "已转存",
            "ok": "已转存",
        }.get(status, status or "已处理")
        unique_logs.append({
            "id": f"gap-mark:{mark.get('resource_key')}",
            "rule_id": "",
            "rule_name": "缺集管理",
            "action": action,
            "item_name": f"{title} {episode_text}".strip(),
            "item_id": base_key,
            "message": f"缺集影巢处理 · {episode_text} · 资源: {mark.get('resource_title') or title} · 状态: {status_text}",
            "created_at": mark.get("updated_at"),
        })

    unique_logs = await _annotate_project_download_logs(unique_logs)

    return {
        "status": "success",
        "data": {
            "reviews": reviews,
            "logs": unique_logs,
        }
    }


@router.post("/reviews/batch-approve")
async def batch_approve():
    """批量通过所有待审核项"""
    reviews = await list_subscribe_reviews("pending")
    results = {"success": 0, "failed": 0, "errors": []}
    for rev in reviews:
        try:
            # 重新调用单个批准
            src = rev.get("source", "")
            result = rev.get("search_result", {})
            action_type = rev.get("action_type", "")

            if action_type == "download":
                mp = MoviePilotClient()
                try:
                    torrent_url = result.get("enclosure", "")
                    # 解析 TMDB ID
                    tmdbid = 0
                    item_id = rev.get("item_id", "")
                    if item_id:
                        try:
                            from app.services.emby import EmbyClient
                            emby = EmbyClient()
                            try:
                                emby_item = await emby.get_item(item_id)
                                if emby_item:
                                    pid = emby_item.get("ProviderIds", {})
                                    tmdb_str = pid.get("Tmdb", "")
                                    if tmdb_str and tmdb_str.isdigit():
                                        tmdbid = int(tmdb_str)
                            finally:
                                await emby.close()
                        except Exception:
                            pass
                    resp = await mp.download(torrent_url, torrent_info=result, tmdbid=tmdbid)
                    if not resp or not resp.get("success"):
                        raise ValueError(f"下载提交失败: {resp.get('message', 'API 无响应') if resp else 'API 无响应'}")
                    await update_subscribe_review(rev["id"], "approved", "批量批准 - 下载")
                    await add_subscribe_log(rev.get("rule_id",""), rev.get("rule_name",""), "download", rev.get("item_name",""), rev.get("item_id",""), "批量批准")
                    results["success"] += 1
                finally:
                    await mp.close()

            elif action_type == "transfer":
                hd = HDHiveClient()
                try:
                    resp = await hd.unlock_and_transfer(result.get("slug", ""))
                    if not resp:
                        raise ValueError("转存提交失败")
                    st = resp.get("status", "")
                    if st in ("transferred", "already_owned"):
                        verify = await _verify_hdhive_transfer_visible(result, resp)
                        if isinstance(resp, dict):
                            resp["cloud115_verify"] = verify
                        if not verify.get("ok"):
                            raise ValueError(verify.get("message") or "115 目标目录未确认转存文件")
                        await update_subscribe_review(rev["id"], "approved", "批量批准 - 转存成功")
                        await add_subscribe_log(rev.get("rule_id",""), rev.get("rule_name",""), "transfer", rev.get("item_name",""), rev.get("item_id",""), "批量批准")
                        results["success"] += 1
                    else:
                        raise ValueError(resp.get("message", f"转存异常: {st}"))
                finally:
                    await hd.close()
            else:
                results["failed"] += 1
                results["errors"].append(f"{rev.get('item_name','')}: 未知操作")
        except Exception as e:
            await update_subscribe_review(rev["id"], "failed", str(e))
            results["failed"] += 1
            results["errors"].append(f"{rev.get('item_name','')}: {e}")

    return {"status": "success", "data": results}


@router.delete("/reviews")
async def clear_reviews():
    """清空所有审核记录"""
    total = await clear_subscribe_reviews()
    return {"status": "success", "message": f"已清空 {total} 条审核记录"}


@router.get("/status")
async def subscribe_status():
    """获取订阅任务运行状态"""
    return {"status": "success", "data": _run_status}


@router.get("/logs")
async def subscribe_logs(limit: int = 50):
    """获取订阅执行日志"""
    logs = await list_subscribe_logs(limit)
    return {"status": "success", "data": logs}


# ─── 后台匹配 ───

async def _run_subscribe(rule_id: str = ""):
    """加载启用规则并执行匹配（供 TG 命令调用）；rule_id 非空时只运行指定规则"""
    rules = await list_subscribe_rules()
    if rule_id:
        rules = [r for r in rules if r["id"] == rule_id]
    rules = [r for r in rules if r.get("enabled", True)]
    if rules:
        await _run_matching(rules)


async def _run_matching(rules: list[dict]):
    """执行匹配：对每个规则扫描低质量条目 → 搜索 → 下载"""
    global _run_status
    _run_status = {"running": True, "progress": 0, "current": "", "total": 0, "processed": 0}

    try:
        # 加载质量缓存（带时间戳）
        items, _, cache_time = await load_quality_cache()
        if not items:
            await add_subscribe_log("", "系统", "error", message="质量缓存为空，请先运行扫描")
            return

        # 检查缓存时效（1 小时内为新鲜）
        from datetime import datetime
        cache_is_fresh = True
        if cache_time:
            try:
                cached_dt = datetime.strptime(cache_time, "%Y-%m-%d %H:%M:%S")
                age_minutes = int((datetime.now() - cached_dt).total_seconds() / 60)
                if age_minutes >= 60:
                    cache_is_fresh = False
                    await add_subscribe_log("", "系统", "warn", message=f"质量缓存已超过 1 小时（{age_minutes} 分钟），建议重新扫描以获得最新数据")
            except ValueError:
                pass

        for rule in rules:
            rule_id = rule["id"]
            rule_name = rule.get("name", "未命名")
            try:
                batch_size = int(rule.get("batch_size", 20) or 20)
            except (TypeError, ValueError):
                batch_size = 20
            batch_size = max(1, min(batch_size, 1000))
            max_score = rule.get("max_score", 60)
            min_res = rule.get("min_current_resolution", "").lower()
            target_res = rule.get("target_resolution", "1080p").lower()
            source = rule.get("source", "moviepilot")
            library_ids = {str(x).strip() for x in (rule.get("library_ids") or []) if str(x).strip()}

            # 过滤符合规则的低质量条目
            ignore_ids = await get_subscribe_ignore_ids(rule_id)
            candidates = []
            rejected_ids = set()  # 记录被拒绝但可重新搜索的条目
            for item in items:
                emby_id = item.get("emby_id", "")
                if library_ids and str(item.get("library_id", "")) not in library_ids:
                    continue
                # 跳过已忽略的
                if emby_id in ignore_ids:
                    continue
                score = item.get("quality_score", 0)
                if score > max_score:
                    continue
                # 按当前分辨率过滤（仅当值有效）
                if min_res:
                    res = item.get("resolution", "0x0")
                    h = int(res.split("x")[-1]) if "x" in res else 0
                    if min_res == "720p" and h >= 720:
                        continue
                    if min_res == "1080p" and h >= 1080:
                        continue
                # 跳过已成功升级的（手动批准 + 自动审核 + 待审核中）
                emby_id = item.get("emby_id", "")
                processed = await has_processed_item(rule_id, emby_id)
                if processed == 'done':
                    continue
                if processed == 'pending_review':
                    # 已有待审核记录，无需重新搜索
                    continue
                if processed == 'rejected':
                    # 被拒绝过但仍可搜索（需排除被拒绝的结果）
                    rejected_ids.add(emby_id)
                candidates.append(item)

            if not candidates:
                await add_subscribe_log(rule_id, rule_name, "skip", message="无符合条件的条目")
                continue

            # 预加载被拒绝的结果标识（用于过滤搜索结果）
            from app.database import get_rejected_result_keys
            rejected_keys_map = {}
            for cand in candidates:
                eid = cand.get("emby_id", "")
                if eid in rejected_ids:
                    keys = await get_rejected_result_keys(eid)
                    if keys:
                        rejected_keys_map[eid] = set(keys)

            target_total = min(batch_size, len(candidates))
            effective_count = 0
            _run_status["total"] = target_total
            _run_status["processed"] = 0

            for i, item in enumerate(candidates):
                if effective_count >= batch_size:
                    break

                item_id = item.get("emby_id", "")
                item_name = item.get("name", "")
                _run_status["current"] = f"{rule_name} → {item_name}（有效 {effective_count}/{target_total}）"
                _run_status["progress"] = int(effective_count / target_total * 100) if target_total else 0
                _run_status["processed"] = effective_count

                try:
                    keyword = f"{item_name}"
                    year = item.get("year")
                    if year:
                        keyword += f" {year}"

                    results = []

                    # MP 搜索
                    if source in ("moviepilot", "both"):
                        mp = MoviePilotClient()
                        try:
                            mp_results = await mp.search(keyword)
                            for r in (mp_results or []):
                                r["_source_label"] = "moviepilot"
                            results.extend(mp_results or [])
                        except Exception as e:
                            logger.warning(f"[Subscribe] MP search failed: {e}")
                        finally:
                            await mp.close()

                    # HDHive 搜索
                    if source in ("hdhive", "both"):
                        hd = HDHiveClient()
                        try:
                            hd_results = await hd.search(
                                keyword=keyword,
                                emby_item_id=item_id,
                                media_type=_media_type_from_item(item),
                            )
                            for r in (hd_results or []):
                                r["_source_label"] = "hdhive"
                            results.extend(hd_results or [])
                        except Exception as e:
                            logger.warning(f"[Subscribe] HDHive search failed: {e}")
                        finally:
                            await hd.close()

                    # ── 按年份过滤搜索结果 ──
                    # 避免同名不同年的电影被错误匹配（如 超级奶爸 1957 ≠ Big Daddy 1999）
                    item_year = item.get("year")
                    if item_year and results:
                        before = len(results)
                        filtered = []
                        discarded = 0
                        for r in results:
                            res_year = r.get("year")
                            if not res_year:
                                # 结果无年份信息，保留（如 HDHive 结果）
                                filtered.append(r)
                            elif str(res_year) == str(item_year):
                                filtered.append(r)
                            else:
                                discarded += 1
                        if discarded:
                            logger.info(f"[Subscribe] {item_name}: 按年份过滤丢弃 {discarded}/{before} 个结果（目标年份 {item_year}）")
                            results = filtered

                    # ── 外部 ID / 形态过滤 ──
                    # 有 IMDb ID 时必须一致；电影条目排除 Sxx / Season / 第x季 等剧集包结果。
                    if results:
                        before = len(results)
                        results = [r for r in results if _identity_matches(item, r)]
                        discarded = before - len(results)
                        if discarded:
                            logger.info(f"[Subscribe] {item_name}: 外部ID/形态过滤丢弃 {discarded}/{before} 个疑似错配结果")

                    # ── 排除被拒绝过的结果 ──
                    # 用户拒绝后，该条目的某些搜索结果被标记为不再命中
                    if item_id in rejected_keys_map:
                        excluded = rejected_keys_map[item_id]
                        before = len(results)
                        filtered = [r for r in results if r.get("enclosure") not in excluded and r.get("slug") not in excluded and r.get("page_url") not in excluded]
                        discarded = before - len(filtered)
                        if discarded:
                            logger.info(f"[Subscribe] {item_name}: 排除 {discarded} 个被拒绝过的结果")
                            results = filtered

                    if not results:
                        await add_subscribe_log(rule_id, rule_name, "no_match", item_name, item_id, "无搜索结果")
                        await asyncio.sleep(1.5)
                        continue

                    # 筛选满足目标的结果
                    better = []
                    # 当前条目分辨率高度（用于"更好"模式）
                    cur_h = _current_height(item)

                    # 统计各质量的计数（用于汇总日志）
                    from collections import Counter
                    quality_counts = Counter()
                    skipped = 0
                    same_quality_skipped = 0
                    smaller_size_skipped = 0
                    for r in results:
                        q = _extract_quality(r)
                        rh = _extract_height(q)
                        if target_res == "better":
                            target_ok = rh == 0 or rh > cur_h
                            if target_ok:
                                if _is_smaller_than_current(item, r):
                                    smaller_size_skipped += 1
                                elif _is_meaningful_upgrade(item, r):
                                    better.append(r)
                                    quality_counts[q or "未知"] += 1
                                else:
                                    same_quality_skipped += 1
                            else:
                                skipped += 1
                        elif _meets_target(q, target_res):
                            if _is_smaller_than_current(item, r):
                                smaller_size_skipped += 1
                            elif _is_meaningful_upgrade(item, r):
                                better.append(r)
                                quality_counts[q or "未知"] += 1
                            else:
                                same_quality_skipped += 1
                        else:
                            skipped += 1

                    # 汇总日志
                    if better:
                        summary = ", ".join(f"{q}×{c}" for q, c in quality_counts.most_common(5))
                        total_results = len(results)
                        skip_parts = []
                        if skipped:
                            skip_parts.append(f"目标不符 {skipped}")
                        if same_quality_skipped:
                            skip_parts.append(f"同质 {same_quality_skipped}")
                        if smaller_size_skipped:
                            skip_parts.append(f"体积小 {smaller_size_skipped}")
                        skip_msg = f"，跳过 {' / '.join(skip_parts)}" if skip_parts else ""
                        logger.info(
                            f"[Subscribe] {item_name}: 纳入 {len(better)}/{total_results} "
                            f"个结果 ({summary}){skip_msg}"
                        )
                    else:
                        detail_parts = []
                        if same_quality_skipped:
                            detail_parts.append(f"同质 {same_quality_skipped}")
                        if smaller_size_skipped:
                            detail_parts.append(f"体积小 {smaller_size_skipped}")
                        detail = f"，其中{' / '.join(detail_parts)}" if detail_parts else ""
                        logger.info(
                            f"[Subscribe] {item_name}: 无明确优于当前版本的目标结果 "
                            f"({target_res})（共 {len(results)} 个结果{detail}）"
                        )

                    if not better:
                        if smaller_size_skipped:
                            await add_subscribe_log(
                                rule_id,
                                rule_name,
                                "no_upgrade",
                                item_name,
                                item_id,
                                f"有 {target_res} 结果，但体积小于当前文件，已跳过",
                            )
                        elif same_quality_skipped:
                            await add_subscribe_log(
                                rule_id,
                                rule_name,
                                "no_upgrade",
                                item_name,
                                item_id,
                                f"有 {target_res} 结果，但与当前版本同质，已跳过",
                            )
                        else:
                            await add_subscribe_log(
                                rule_id,
                                rule_name,
                                "no_target",
                                item_name,
                                item_id,
                                f"无 {target_res} 以上结果",
                            )
                        await asyncio.sleep(1.5)
                        continue

                    # ── 排序：按优先级规则综合排序 ──
                    # prefer_order: ["remux", "4k", "subtitle"] 顺序即优先级
                    prefer_order = rule.get("prefer_order") or []
                    # 向后兼容旧格式的 bool 字段
                    if not prefer_order:
                        if rule.get("prefer_remux"):
                            prefer_order.append("remux")
                        if rule.get("prefer_4k"):
                            prefer_order.append("4k")
                        if rule.get("prefer_subtitle"):
                            prefer_order.append("subtitle")
                    source_priority = rule.get("source_priority", "hdhive")
                    target_codec = (rule.get("target_codec") or "").lower()
                    target_hdr = (rule.get("target_hdr") or "").lower()

                    def sort_key(r):
                        label = r.get("_source_label", "")
                        score = []
                        for pref in prefer_order:
                            if pref == "remux":
                                score.append(0 if _is_remux(r) else 1)
                            elif pref == "4k":
                                score.append(0 if _is_4k(r) else 1)
                            elif pref == "subtitle":
                                score.append(0 if _has_subtitle(r) else 1)
                        # 编码偏好（仅当设置了目标编码时生效）
                        if target_codec:
                            rc = _get_video_codec(r)
                            score.append(0 if rc == target_codec else (1 if rc else 2))
                        # HDR 偏好
                        if target_hdr:
                            rh = _get_video_range(r)
                            score.append(0 if rh == target_hdr else (1 if rh else 2))
                        # 来源优先级
                        score.append(0 if label == source_priority else 1)
                        # MP 做种数降序
                        seeders = int(r.get("seeders", 0) or 0) if label == "moviepilot" else 0
                        score.append(-seeders)
                        return tuple(score)

                    better.sort(key=sort_key)
                    best = better[0]

                    # ── MP: 0 做种过滤（有替代项时禁止下载 0 做种） ──
                    if best.get("_source_label") == "moviepilot":
                        seeders = int(best.get("seeders", 0) or 0)
                        if seeders == 0:
                            # 从排序后的列表找第一个有做种的 MP 结果
                            alt_mp = next((r for r in better if r.get("_source_label") == "moviepilot" and int(r.get("seeders", 0) or 0) > 0), None)
                            if alt_mp:
                                best = alt_mp
                                logger.info(f"[Subscribe] {item_name}: 当前 MP 最佳为 0 做种，改用做种 {best.get('seeders')} 的结果")
                            else:
                                # 所有 MP 都是 0 做种，看有没有 HDHive 备选
                                alt_hd = next((r for r in better if r.get("_source_label") == "hdhive"), None)
                                if alt_hd:
                                    best = alt_hd
                                    logger.info(f"[Subscribe] {item_name}: MP 全部 0 做种，改用 HDHive 结果")

                    src = best.get("_source_label", "")
                    action_type = "download" if src == "moviepilot" else "transfer"

                    # ── 自动审核：勾选的审核条件全部满足时跳过审核直接执行 ──
                    auto_approve = rule.get("auto_approve", False)
                    auto_conditions = rule.get("auto_approve_conditions") or []

                    if auto_approve and auto_conditions and best:
                        quality_str = _extract_quality(best)
                        auto_ok = True
                        checks = []
                        for cond in auto_conditions:
                            if cond == "remux":
                                if _is_remux(best):
                                    checks.append("Remux✓")
                                else:
                                    checks.append("Remux✗"); auto_ok = False
                            elif cond == "4k":
                                if _is_4k(best):
                                    checks.append("4K✓")
                                else:
                                    checks.append("4K✗"); auto_ok = False
                            elif cond == "subtitle":
                                if _has_subtitle(best):
                                    checks.append("字幕✓")
                                else:
                                    checks.append("字幕✗"); auto_ok = False
                        if action_type == "transfer":
                            if _is_free_hdhive(best):
                                checks.append("免费/已解锁✓")
                            else:
                                checks.append("免费/已解锁✗")
                                auto_ok = False
                        if not auto_ok:
                            # 不满足条件，回退到普通审核
                            await add_subscribe_log(rule_id, rule_name, "pending_review", item_name, item_id,
                                f"自动审核条件未满足 ({', '.join(checks)})，转为待审核")
                        else:
                            checks_str = ", ".join(checks)
                            try:
                                if action_type == "download":
                                    torrent_url = best.get("enclosure", "")
                                    if not torrent_url:
                                        raise ValueError("无下载链接")
                                    tmdbid = 0
                                    try:
                                        from app.services.emby import EmbyClient
                                        emby = EmbyClient()
                                        try:
                                            emby_item = await emby.get_item(item_id)
                                            if emby_item:
                                                pid = emby_item.get("ProviderIds", {})
                                                tmdb_str = pid.get("Tmdb", "")
                                                if tmdb_str and tmdb_str.isdigit():
                                                    tmdbid = int(tmdb_str)
                                        finally:
                                            await emby.close()
                                    except Exception:
                                        pass
                                    mp = MoviePilotClient()
                                    try:
                                        resp = await mp.download(torrent_url, torrent_info=best, tmdbid=tmdbid)
                                        if not resp or not resp.get("success"):
                                            raise ValueError(f"下载提交失败: {resp.get('message', 'MP API 未返回成功') if resp else 'API 无响应'}")
                                    finally:
                                        await mp.close()
                                    mp_detail = _format_resource_detail(best, src)
                                    mp_seeders = best.get("seeders", 0)
                                    mp_site = best.get("site_name", "")
                                    mp_extra = []
                                    if mp_site:
                                        mp_extra.append(f"🏠 {mp_site}")
                                    if mp_seeders is not None and mp_seeders != "":
                                        mp_extra.append(f"🧲 {mp_seeders}")
                                    mp_extra_str = f" [{' '.join(mp_extra)}]" if mp_extra else ""
                                    mp_log_msg = f"🤖 自动通过 {quality_str} ({checks_str}) → MP 下载已推送{mp_extra_str}"
                                    if mp_detail:
                                        mp_log_msg += f"\n{mp_detail}"
                                    await add_subscribe_log(rule_id, rule_name, "auto_approved", item_name, item_id, mp_log_msg)
                                    tg = TelegramNotifier()
                                    try:
                                        tg_text = (
                                            f"🤖 *MP 自动下载*\n"
                                            f"\n📦 {item_name}\n"
                                            f"🎯 {quality_str} · ✅ {checks_str}\n"
                                            f"🏠 {mp_site if mp_site else '?'} · 🧲 {mp_seeders if mp_seeders is not None and mp_seeders != '' else '?'}"
                                        )
                                        if mp_detail:
                                            tg_text += f"\n{mp_detail}"
                                        await tg.send_message(tg_text)
                                    finally:
                                        await tg.close()
                                else:
                                    # transfer — 自动审核转存，若密码错误则自动重试下一个结果
                                    bad_slugs = set()
                                    transfer_ok = False
                                    transfer_detail = ""
                                    # 尝试当前 best 及后续候选
                                    transfer_candidates = [best] + [r for r in better[1:] if r.get("_source_label") == "hdhive"]
                                    for candidate in transfer_candidates:
                                        slug = candidate.get("slug", "")
                                        if not slug or slug in bad_slugs:
                                            continue
                                        hd = HDHiveClient()
                                        try:
                                            resp = await hd.unlock_and_transfer(slug)
                                            if not resp:
                                                raise ValueError("转存接口无响应")
                                            st = resp.get("status", "")
                                            detail = resp.get("message", "转存成功")
                                            if st in ("transferred", "already_owned"):
                                                verify = await _verify_hdhive_transfer_visible(candidate, resp)
                                                if isinstance(resp, dict):
                                                    resp["cloud115_verify"] = verify
                                                if not verify.get("ok"):
                                                    raise ValueError(verify.get("message") or "115 目标目录未确认转存文件")
                                            if st == "transferred":
                                                transfer_ok = True
                                                transfer_detail = detail
                                                best = candidate  # 更新 best 为实际成功的
                                                break
                                            elif st == "already_owned":
                                                transfer_ok = True
                                                transfer_detail = "已在 115 中"
                                                best = candidate
                                                break
                                            elif st == "password_error":
                                                bad_slugs.add(slug)
                                                logger.warning(f"[Subscribe] {item_name}: slug {slug} 密码无效，尝试下一个")
                                                continue
                                            elif st == "not_115":
                                                bad_slugs.add(slug)
                                                logger.warning(f"[Subscribe] {item_name}: slug {slug} 非 115 资源，跳过")
                                                continue
                                            else:
                                                raise ValueError(detail)
                                        finally:
                                            await hd.close()
                                    if not transfer_ok:
                                        raise ValueError("所有 HDHive 候选均转存失败")
                                    detail = transfer_detail
                                    hd_detail = _format_resource_detail(best, "hdhive")
                                    qs = _extract_quality(best)
                                    hd_log_msg = f"🤖 自动通过 {qs} ({checks_str}) → 115 转存 {detail}"
                                    if hd_detail:
                                        hd_log_msg += f"\n{hd_detail}"
                                    await add_subscribe_log(rule_id, rule_name, "auto_approved", item_name, item_id, hd_log_msg)
                                    tg = TelegramNotifier()
                                    try:
                                        tg_text = (
                                            f"🤖 *115 自动转存*\n"
                                            f"\n📦 {item_name}\n"
                                            f"🎯 {qs} · ✅ {checks_str}\n"
                                            f"📌 {detail}"
                                        )
                                        if hd_detail:
                                            tg_text += f"\n{hd_detail}"
                                        await tg.send_message(tg_text)
                                    finally:
                                        await tg.close()
                                effective_count += 1
                                _run_status["processed"] = effective_count
                                _run_status["progress"] = int(effective_count / target_total * 100) if target_total else 100
                                await asyncio.sleep(1.5)
                                if effective_count >= batch_size:
                                    break
                                continue  # 跳过普通审核流程
                            except Exception as e:
                                logger.warning(f"[Subscribe] 自动审核执行失败: {e}")
                                await add_subscribe_log(rule_id, rule_name, "error", item_name, item_id,
                                    f"自动审核失败: {str(e)[:80]}，转为待审核")

                    # 保存到审核列表（含自动审核失败回退的情况）
                    review_id = await add_subscribe_review({
                        "rule_id": rule_id,
                        "rule_name": rule_name,
                        "item_id": item_id,
                        "item_name": item_name,
                        "current_quality": item,
                        "search_result": best,
                        "source": src,
                        "action_type": action_type,
                        "message": f"找到 {_extract_quality(best)} 版本，待审核",
                    })
                    await add_subscribe_log(rule_id, rule_name, "pending_review", item_name, item_id, f"已加入审核列表: {_extract_quality(best)}")
                    if review_id:
                        effective_count += 1
                        _run_status["processed"] = effective_count
                        _run_status["progress"] = int(effective_count / target_total * 100) if target_total else 100
                    # TG 通知 - 新审核项（详细卡片 + 操作按钮）
                    if best and review_id:
                        try:
                            tg = TelegramNotifier()
                            try:
                                sent = await tg.send_review_card({
                                    "id": review_id,
                                    "rule_id": rule_id,
                                    "rule_name": rule_name,
                                    "item_id": item_id,
                                    "item_name": item_name,
                                    "current_quality": item,
                                    "search_result": best,
                                    "source": src,
                                    "action_type": action_type,
                                })
                                if not sent:
                                    await add_subscribe_log(
                                        rule_id, rule_name, "warn", item_name, item_id,
                                        "TG 审核卡片发送失败，请检查 Bot 权限、Chat ID 或 Telegram 网络"
                                    )
                            finally:
                                await tg.close()
                        except Exception as e:
                            logger.warning(f"[Subscribe] TG 审核通知失败: {e}")
                            await add_subscribe_log(
                                rule_id, rule_name, "warn", item_name, item_id,
                                f"TG 审核通知异常: {str(e)[:80]}"
                            )

                except Exception as e:
                    await add_subscribe_log(rule_id, rule_name, "error", item_name, item_id, str(e))

                await asyncio.sleep(2)
                if effective_count >= batch_size:
                    break

            # 更新规则 last_run
            rule["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await save_subscribe_rule(rule)

        await add_subscribe_log("", "系统", "complete", message="订阅匹配执行完成")
        try:
            tg = TelegramNotifier()
            total_processed = _run_status.get("processed", 0)
            await tg.send_notification("scan", "📊 订阅匹配完成",
                f"处理了 {total_processed} 条媒体，新增待审核项")
            await tg.close()
        except Exception:
            pass

    except Exception as e:
        logger.exception(f"[Subscribe] 执行异常: {e}")
        await add_subscribe_log("", "系统", "error", message=f"执行异常: {e}")
    finally:
        _run_status = {"running": False, "progress": 100, "current": "", "total": 0, "processed": 0}
