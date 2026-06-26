"""MoviePilot API 服务 - 搜索 + 下载"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from html import unescape
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts", ".m4v", ".webm", ".rmvb",
)
SIDECARE_EXTENSIONS = (
    ".srt", ".ass", ".ssa", ".vtt", ".sup", ".idx", ".sub", ".nfo",
)


def _normalize_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, flags=re.I):
        text = f"http://{text}"
    return text.rstrip("/")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_http_error(status_code: int, text: str = "") -> str:
    body = unescape(str(text or ""))
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if status_code in {502, 503, 504}:
        detail = {
            502: "Bad Gateway",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }.get(status_code, body)
        return f"HTTP {status_code}: MoviePilot 服务暂不可用（{detail}）"
    if body:
        return f"HTTP {status_code}: {body[:180]}"
    return f"HTTP {status_code}"


def _episode_span(start: int, end: int = 0) -> list[int]:
    if start <= 0:
        return []
    if end <= 0:
        return [start]
    return list(range(min(start, end), max(start, end) + 1))


def _extract_file_episodes(filename: str, season: int = 0) -> set[int]:
    """从下载器文件名里提取集数。只用于文件级筛选，宁可少匹配也不要误选整包。"""
    raw = str(filename or "")
    # 下载器通常返回带父目录的路径，父目录可能是 "S03 E01-E11"。
    # 文件级筛选只能看文件名本身，否则整包目录会让每个文件都误判为命中目标集。
    text = re.split(r"[\\/]", raw)[-1].lower()
    if not text:
        return set()

    episodes: set[int] = set()
    season_num = _safe_int(season, 0)

    for match in re.finditer(r"(?<![a-z0-9])s0?(\d{1,2})[ ._-]*e0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*e?0*(\d{1,4}))?(?!\d)", text):
        file_season = _safe_int(match.group(1), -1)
        if season_num and file_season != season_num:
            continue
        episodes.update(_episode_span(_safe_int(match.group(2)), _safe_int(match.group(3))))

    if episodes:
        return {ep for ep in episodes if 0 < ep < 10000}

    # 文件已位于目标季目录时，常见命名会省略 Sxx，仅保留 E06 / EP06 / 第06集。
    for match in re.finditer(r"(?<![a-z0-9])(?:e|ep|episode)[ ._-]*0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*0*(\d{1,4}))?(?!\d)", text):
        episodes.update(_episode_span(_safe_int(match.group(1)), _safe_int(match.group(2))))
    for match in re.finditer(r"第\s*0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*0*(\d{1,4}))?\s*[集期话話]", text):
        episodes.update(_episode_span(_safe_int(match.group(1)), _safe_int(match.group(2))))
    for match in re.finditer(r"(?<![a-z]\.)(?<![a-z0-9])0*(\d{1,4})(?:v\d)?\s*\.(?:mkv|mp4|avi|mov|wmv|flv|ts|m2ts|m4v|webm|rmvb|srt|ass|ssa|vtt|sup|sub)\b", text):
        episodes.add(_safe_int(match.group(1)))
    for match in re.finditer(r"[\[【(（]\s*0*(\d{1,4})(?:v\d)?\s*[\]】)）]", text):
        episodes.add(_safe_int(match.group(1)))
    for match in re.finditer(r"#\s*0*(\d{1,4})(?:v\d)?(?![a-z0-9])", text):
        episodes.add(_safe_int(match.group(1)))
    for match in re.finditer(r"^\s*0*(\d{1,4})(?:v\d)?(?:\s*(?:-|~|至|到)\s*0*(\d{1,4}))?(?=\s*(?:[-_. ]|$))", text):
        episodes.update(_episode_span(_safe_int(match.group(1)), _safe_int(match.group(2))))

    return {ep for ep in episodes if 0 < ep < 10000}


def _extract_file_episode_refs(filename: str) -> set[tuple[int, int]]:
    """提取文件名中的显式 SxxEyy，用于多季包按绝对集数兜底映射。"""
    text = re.split(r"[\\/]", str(filename or ""))[-1].lower()
    refs: set[tuple[int, int]] = set()
    if not text:
        return refs
    for match in re.finditer(r"(?<![a-z0-9])s0?(\d{1,2})[ ._-]*e0*(\d{1,4})(?:\s*(?:-|~|至|到)\s*e?0*(\d{1,4}))?(?!\d)", text):
        season = _safe_int(match.group(1), 0)
        if season <= 0:
            continue
        for ep in _episode_span(_safe_int(match.group(2)), _safe_int(match.group(3))):
            if 0 < ep < 10000:
                refs.add((season, ep))
    return refs


def _season_tokens_from_segment(segment: str) -> set[int]:
    """Extract a single-season marker from one path segment, ignoring collection ranges."""
    text = str(segment or "").lower()
    if not text:
        return set()

    seasons: set[int] = set()
    if re.search(r"(?<![a-z0-9])specials?(?![a-z0-9])|特别篇|番外|ova", text):
        seasons.add(0)

    range_patterns = (
        r"(?<![a-z0-9])s0?\d{1,2}\s*(?:-|~|至|到)\s*s?0?\d{1,2}(?![a-z0-9])",
        r"(?<![a-z0-9])season[ ._-]*0?\d{1,2}\s*(?:-|~|to|至|到)\s*(?:season[ ._-]*)?0?\d{1,2}(?![a-z0-9])",
        r"第\s*\d{1,2}\s*(?:-|~|至|到)\s*\d{1,2}\s*季",
    )
    if any(re.search(pattern, text, re.I) for pattern in range_patterns):
        return seasons

    patterns = (
        r"(?<![a-z0-9])s0?(\d{1,2})(?![ ._-]*(?:e|ep)0?\d)(?![a-z0-9])",
        r"(?<![a-z0-9])season[ ._-]*0?(\d{1,2})(?![a-z0-9])",
        r"第\s*0?(\d{1,2})\s*季",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            season = _safe_int(match.group(1), -1)
            if 0 <= season < 100:
                seasons.add(season)
    return seasons


def _extract_file_seasons(filename: str) -> set[int]:
    """Extract the concrete season a downloader file belongs to."""
    raw = str(filename or "")
    parts = [part for part in re.split(r"[\\/]+", raw) if part]
    if not parts:
        return set()

    basename = parts[-1]
    refs = _extract_file_episode_refs(basename)
    if refs:
        return {season for season, _ in refs}

    basename_seasons = _season_tokens_from_segment(basename)
    if basename_seasons:
        return basename_seasons

    # Walk from the nearest parent outward. A top-level "S01-S05" collection
    # should not make every child file look like it belongs to every season.
    for segment in reversed(parts[:-1]):
        seasons = _season_tokens_from_segment(segment)
        if len(seasons) == 1:
            return seasons
    return set()


def _format_episode_list(season: int, episodes: list[int] | set[int]) -> str:
    eps = sorted({_safe_int(x, 0) for x in episodes if _safe_int(x, 0) > 0})
    season_text = f"S{_safe_int(season, 0):02d}"
    if not eps:
        return f"{season_text} 整季"
    return f"{season_text} " + ", ".join(f"E{ep:02d}" for ep in eps)


def _normalise_file_targets(
    season: int = 0,
    episodes: list[int] | None = None,
    file_targets: list[dict] | None = None,
) -> list[dict]:
    grouped: dict[int, set[int]] = {}
    season_only: set[int] = set()
    for target in file_targets or []:
        if not isinstance(target, dict):
            continue
        target_season = _safe_int(target.get("season"), -1)
        if target_season < 0:
            continue
        raw_episodes = target.get("episodes") or []
        if bool(target.get("season_missing") or target.get("season_only") or target.get("full_season")) and not raw_episodes:
            season_only.add(target_season)
        bucket = grouped.setdefault(target_season, set())
        for ep in raw_episodes:
            ep_num = _safe_int(ep, 0)
            if ep_num > 0:
                bucket.add(ep_num)

    fallback_season = _safe_int(season, -1)
    if fallback_season >= 0:
        bucket = grouped.setdefault(fallback_season, set())
        for ep in episodes or []:
            ep_num = _safe_int(ep, 0)
            if ep_num > 0:
                bucket.add(ep_num)

    return [
        {
            "season": target_season,
            "episodes": sorted(target_episodes),
            "season_missing": target_season in season_only and not target_episodes,
        }
        for target_season, target_episodes in sorted(grouped.items())
        if target_episodes or target_season in season_only
    ]


def _format_file_targets(file_targets: list[dict]) -> str:
    return " / ".join(
        part
        for part in (
            _format_episode_list(_safe_int(target.get("season"), 0), target.get("episodes") or [])
            for target in file_targets
        )
        if part
    )


def _torrent_title(result: dict | None) -> str:
    if not result:
        return ""
    raw = (result.get("_raw") or {}).get("torrent_info") or {}
    return str(result.get("title") or raw.get("title") or result.get("name") or "").strip()


def _torrent_size(result: dict | None) -> int:
    if not result:
        return 0
    raw = (result.get("_raw") or {}).get("torrent_info") or {}
    return _safe_int(result.get("size") or raw.get("size"), 0)


def _normalize_torrent_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _torrent_match_tokens(value: str) -> set[str]:
    raw_tokens = re.findall(r"[\u4e00-\u9fff]+|[a-z]+\d+|\d+[a-z]+|[a-z]+|\d+", str(value or "").lower())
    ignored = {
        "web", "dl", "webdl", "webrip", "bluray", "bdrip", "hdtv", "uhd", "remux",
        "aac", "ddp", "dts", "flac", "mp4", "mkv", "x264", "x265", "h264", "h265",
        "hevc", "avc", "hdr", "sdr", "10bit", "8bit", "bit", "h", "264", "265",
        "2", "0", "20", "5", "1", "51", "60fps", "fps",
    }
    tokens: set[str] = set()
    for token in raw_tokens:
        cleaned = token.strip()
        if len(cleaned) <= 1 or cleaned in ignored:
            continue
        if cleaned in {"720", "1080", "2160", "4320", "2020", "2021", "2022", "2023", "2024", "2025", "2026"}:
            tokens.add(cleaned)
            continue
        if cleaned.isdigit() and len(cleaned) <= 2:
            continue
        tokens.add(cleaned)
    return tokens


def _preferred_downloader_name(result: dict | None) -> str:
    if not result:
        return ""
    raw = (result.get("_raw") or {}).get("torrent_info") or {}
    return str(
        result.get("site_downloader")
        or raw.get("site_downloader")
        or result.get("downloader")
        or ""
    ).strip()


def _is_media_or_sidecar(filename: str) -> bool:
    lower = str(filename or "").lower()
    return lower.endswith(VIDEO_EXTENSIONS + SIDECARE_EXTENSIONS)


def _is_video_file(filename: str) -> bool:
    return str(filename or "").lower().endswith(VIDEO_EXTENSIONS)


class MoviePilotClient:
    """MoviePilot API 客户端"""

    def __init__(self, url: str = "", token: str = ""):
        self.url = (url or settings.moviepilot_url).rstrip("/")
        self.token = token or settings.moviepilot_token
        self._client = httpx.AsyncClient(timeout=60.0, verify=False)
        self.last_error = ""
        self.last_status_code = 0

    @property
    def is_configured(self) -> bool:
        """检查是否已配置"""
        return bool(self.url and self.token)

    async def _get(self, path: str, params: dict = None) -> dict | list | None:
        """GET 请求"""
        if params is None:
            params = {}
        if not self.is_configured:
            return None
        headers = {
            "X-API-KEY": self.token,
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.get(f"{self.url}{path}", params=params, headers=headers)
            resp.raise_for_status()
            self.last_error = ""
            self.last_status_code = 0
            return resp.json()
        except httpx.HTTPStatusError as e:
            self.last_status_code = e.response.status_code
            self.last_error = _compact_http_error(e.response.status_code, e.response.text)
            print(f"[MoviePilot] GET {path} {self.last_error}")
            return None
        except Exception as e:
            self.last_status_code = 0
            self.last_error = str(e)
            print(f"[MoviePilot] GET {path} failed: {self.last_error}")
            return None

    async def _post(self, path: str, data: dict = None) -> dict | None:
        """POST 请求"""
        if not self.is_configured:
            return None
        headers = {
            "X-API-KEY": self.token,
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.post(f"{self.url}{path}", json=data, headers=headers)
            resp.raise_for_status()
            self.last_error = ""
            self.last_status_code = 0
            return resp.json() if resp.content else {"status": "ok"}
        except httpx.HTTPStatusError as e:
            self.last_status_code = e.response.status_code
            self.last_error = _compact_http_error(e.response.status_code, e.response.text)
            print(f"[MoviePilot] POST {path} {self.last_error}")
            return {"success": False, "message": self.last_error, "status_code": e.response.status_code}
        except Exception as e:
            self.last_status_code = 0
            self.last_error = str(e)
            print(f"[MoviePilot] POST {path} failed: {self.last_error}")
            return {"success": False, "message": self.last_error}

    async def _put(self, path: str, data: dict = None) -> dict | None:
        """PUT 请求"""
        if not self.is_configured:
            return None
        headers = {
            "X-API-KEY": self.token,
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.put(f"{self.url}{path}", json=data, headers=headers)
            resp.raise_for_status()
            self.last_error = ""
            self.last_status_code = 0
            return resp.json() if resp.content else {"status": "ok"}
        except httpx.HTTPStatusError as e:
            self.last_status_code = e.response.status_code
            self.last_error = _compact_http_error(e.response.status_code, e.response.text)
            print(f"[MoviePilot] PUT {path} {self.last_error}")
            return {"success": False, "message": self.last_error, "status_code": e.response.status_code}
        except Exception as e:
            self.last_status_code = 0
            self.last_error = str(e)
            print(f"[MoviePilot] PUT {path} failed: {self.last_error}")
            return {"success": False, "message": self.last_error}

    async def search(self, keyword: str, media_type: str = "movie") -> list[dict]:
        """
        搜索资源 - /api/v1/search/title?keyword=
        兼容 MP v1/v2 不同响应格式。返回拍平后的字典列表。
        """
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置：请先在系统配置中设置地址和 API Token")

        result = await self._get("/api/v1/search/title", {"keyword": keyword})
        if result is None:
            result = await self._get("/api/v2/search/title", {"keyword": keyword})

        raw_items: list[dict] = []
        if isinstance(result, list):
            raw_items = result
        elif isinstance(result, dict):
            for key in ("results", "data", "items", "list", "resources"):
                if key in result and isinstance(result[key], list):
                    raw_items = result[key]
                    break
            # MP v2 响应格式: {success, message, data}
            if not raw_items and result.get("success") and isinstance(result.get("data"), list):
                raw_items = result["data"]

        # 拍平 meta_info + torrent_info 到顶层
        flat_items = []
        for item in raw_items:
            flat = {}
            meta = item.get("meta_info") or {}
            torr = item.get("torrent_info") or {}
            flat["title"] = torr.get("title") or meta.get("title") or meta.get("name", "")
            flat["site_name"] = torr.get("site_name", "")
            flat["size"] = torr.get("size", 0)
            flat["seeders"] = torr.get("seeders", 0)
            flat["peers"] = torr.get("peers", 0)
            flat["page_url"] = torr.get("page_url", "")
            flat["enclosure"] = torr.get("enclosure", "")
            flat["description"] = torr.get("description", "")
            flat["imdbid"] = torr.get("imdbid", "")
            flat["resolution"] = meta.get("resource_pix", "")
            flat["video_encode"] = meta.get("video_encode", "")
            flat["audio_encode"] = meta.get("audio_encode", "")
            flat["source_type"] = meta.get("resource_type", "")
            flat["year"] = meta.get("year", "")
            flat["subtitle"] = meta.get("subtitle", "")
            flat["labels"] = torr.get("labels", [])
            flat["_raw"] = item  # 保留原始数据
            flat_items.append(flat)

        return flat_items

    async def search_movie(self, keyword: str) -> list[dict]:
        """用 MoviePilot 搜索电影（使用 /api/v1/search/movie）"""
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置")
        result = await self._get("/api/v1/search/movie", {"keyword": keyword})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("results", "data", "items"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return []

    async def get_tmdb_seasons(self, tmdbid: int | str) -> list[dict]:
        """读取 TMDB 剧集所有季信息，用于判断本地缺失整季。"""
        tmdb_num = _safe_int(tmdbid, 0)
        if tmdb_num <= 0 or not self.is_configured:
            return []
        result = await self._get(f"/api/v1/tmdb/seasons/{tmdb_num}")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            for key in ("data", "items", "results", "list", "seasons", "season_info"):
                items = result.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    async def download(self, torrent_url: str, download_settings: dict = None,
                       torrent_info: dict = None, tmdbid: int = 0) -> dict | None:
        """
        添加下载 - /api/v1/download/add

        MP v2 要求 body 格式:
        {
          "torrent_in": {"title": "...", "enclosure": "...", "site_name": "...", ...},
          "tmdbid": 12345,  # 可选但推荐
          "download_setting": -1
        }

        Args:
            torrent_url: 种子下载链接
            download_settings: 下载设置 ID，-1 为默认
            torrent_info: 搜索结果的完整字段（title, site_name, description, size, seeders, page_url, imdbid 等）
            tmdbid: TMDB ID，提供后可大幅提升媒体识别准确率
        """
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置")

        # 从 torrent_info 构建 TorrentInfo，优先保留 MP 搜索返回的完整 torrent_info。
        # 只传拍平字段会丢站点、UA、Cookie、代理和下载器等信息，部分站点会因此添加失败。
        payload = {
            "torrent_in": {
                "enclosure": torrent_url,
            },
        }

        if torrent_info:
            raw_torrent = (torrent_info.get("_raw") or {}).get("torrent_info") or {}
            if isinstance(raw_torrent, dict):
                for key, value in raw_torrent.items():
                    if value is not None and value != "":
                        payload["torrent_in"][key] = value

            # 复制拍平字段，覆盖为当前审核卡片展示的候选信息
            for key in ("title", "site_name", "description", "imdbid", "page_url",
                        "size", "seeders", "peers", "labels", "enclosure",
                        "site", "site_cookie", "site_ua", "site_proxy", "site_order",
                        "site_downloader", "uploadvolumefactor", "downloadvolumefactor",
                        "hit_and_run"):
                if key in torrent_info and torrent_info[key]:
                    payload["torrent_in"][key] = torrent_info[key]

            # 自动从 MP 获取站点 Cookie/UA（仅在搜索结果缺失时补齐）
            site_name = torrent_info.get("site_name", "")
            if site_name:
                site_config = await self._get_site_config(site_name)
                if site_config:
                    cookie = site_config.get("cookie", "")
                    ua = site_config.get("ua", "")
                    if cookie and not payload["torrent_in"].get("site_cookie"):
                        payload["torrent_in"]["site_cookie"] = cookie
                    if ua and not payload["torrent_in"].get("site_ua"):
                        payload["torrent_in"]["site_ua"] = ua

        # 确保 enclosure 覆盖为正确的下载 URL
        payload["torrent_in"]["enclosure"] = torrent_url

        if tmdbid:
            payload["tmdbid"] = tmdbid

        if download_settings is not None:
            payload["download_setting"] = download_settings
        else:
            payload["download_setting"] = -1

        result = await self._post("/api/v1/download/add", payload)
        if result and "success" not in result:
            status = str(result.get("status", "")).lower()
            code = result.get("code")
            if status in ("ok", "success") or code in (0, "0"):
                result["success"] = True
        return result

    async def get_subscription(self, mediaid: str, season: int | None = None, title: str = "") -> dict | None:
        """按媒体 ID 查询 MoviePilot 订阅，mediaid 形如 tmdb:12345。"""
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置")
        mediaid = str(mediaid or "").strip()
        if not mediaid:
            return None
        params: dict[str, Any] = {}
        if season is not None:
            params["season"] = season
        if title:
            params["title"] = title
        result = await self._get(f"/api/v1/subscribe/media/{mediaid}", params)
        if isinstance(result, dict) and result.get("id"):
            return result
        return None

    async def add_subscription(self, payload: dict) -> dict | None:
        """新增 MoviePilot 订阅，不触发搜索或下载。"""
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置")
        result = await self._post("/api/v1/subscribe/", payload)
        if result and "success" not in result:
            status = str(result.get("status", "")).lower()
            code = result.get("code")
            if status in ("ok", "success") or code in (0, "0"):
                result["success"] = True
        return result

    async def update_subscription(self, payload: dict) -> dict | None:
        """更新 MoviePilot 订阅。调用方需传入完整订阅对象，避免 MP 用空值覆盖字段。"""
        if not self.is_configured:
            raise RuntimeError("MoviePilot 未配置")
        result = await self._put("/api/v1/subscribe/", payload)
        if result and "success" not in result:
            status = str(result.get("status", "")).lower()
            code = result.get("code")
            if status in ("ok", "success") or code in (0, "0"):
                result["success"] = True
        return result

    async def disable_subscription_best_version(self, subscription: dict) -> dict | None:
        """把 MP 订阅强制改为普通追剧订阅，关闭洗版和全集洗版。"""
        if not isinstance(subscription, dict) or not subscription.get("id"):
            return {"success": False, "message": "缺少订阅 ID"}
        payload = dict(subscription)
        payload.pop("completed_episode", None)
        payload["best_version"] = 0
        payload["best_version_full"] = 0
        payload["current_priority"] = None
        payload["episode_priority"] = {}
        return await self.update_subscription(payload)

    async def download_selected_episodes(
        self,
        torrent_url: str,
        torrent_info: dict = None,
        tmdbid: int = 0,
        season: int = 0,
        episodes: list[int] | None = None,
        file_targets: list[dict] | None = None,
    ) -> dict | None:
        """
        先交给 MoviePilot 添加下载，再按下载器文件列表只保留指定集数。

        MoviePilot 的公开 /download/add 不接收“只下载第几集”，但它配置里有下载器连接信息。
        这里复用 EmbyPulse 的思路：提交任务后定位新种子，按文件名集数把非目标文件优先级设为 0。
        """
        targets = _normalise_file_targets(season=season, episodes=episodes, file_targets=file_targets)
        if not targets:
            return {"success": False, "message": "缺少待下载集数"}
        target_episodes = sorted({ep for target in targets for ep in target["episodes"]})

        result = await self.download(torrent_url, torrent_info=torrent_info, tmdbid=tmdbid)
        if not result or not result.get("success"):
            return result

        selection = await self.select_downloader_files(
            torrent_info=torrent_info or {},
            download_result=result,
            season=season,
            episodes=target_episodes,
            file_targets=targets,
        )
        result["episode_selection"] = selection
        base_message = result.get("message") or "MoviePilot 下载已提交"
        selection_markers = self._download_selection_markers(selection)
        if selection.get("success"):
            marker_text = f"；{selection_markers}" if selection_markers else ""
            result["message"] = f"{base_message}，已拆包选择 {_format_file_targets(targets)}{marker_text}"
        else:
            result["success"] = False
            marker_text = f"；{selection_markers}" if selection_markers else ""
            result["message"] = f"{base_message}，但拆包筛选未确认：{selection.get('message', '未知原因')}{marker_text}"
        return result

    async def get_downloaders(self) -> list[dict]:
        """读取 MoviePilot 已启用下载器配置。"""
        result = await self._get("/api/v1/system/setting/Downloaders")
        value: Any = None
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict) and isinstance(data.get("value"), list):
                value = data.get("value")
            elif isinstance(result.get("value"), list):
                value = result.get("value")
        elif isinstance(result, list):
            value = result
        if not isinstance(value, list):
            return []
        return [
            item for item in value
            if isinstance(item, dict)
            and item.get("enabled")
            and str(item.get("type") or "").lower() in {"qbittorrent", "transmission"}
        ]

    async def select_downloader_files(
        self,
        torrent_info: dict,
        download_result: dict,
        season: int,
        episodes: list[int],
        file_targets: list[dict] | None = None,
    ) -> dict:
        downloaders = await self.get_downloaders()
        if not downloaders:
            return {"success": False, "message": "MoviePilot 没有可用的 qBittorrent/Transmission 下载器配置"}

        targets = _normalise_file_targets(season=season, episodes=episodes, file_targets=file_targets)
        if not targets:
            return {"success": False, "message": "缺少待下载集数"}

        ordered_downloaders = self._order_downloaders(downloaders, torrent_info)
        expected_size = _torrent_size(torrent_info)
        torrent_name = _torrent_title(torrent_info)
        download_id = self._download_id(download_result)
        errors: list[str] = []
        task_hint: dict = {}

        for downloader in ordered_downloaders:
            dtype = str(downloader.get("type") or "").lower()
            config = downloader.get("config") or {}
            name = str(downloader.get("name") or dtype)
            try:
                if dtype == "qbittorrent":
                    selected = await self._select_qbittorrent_files(
                        config=config,
                        expected_size=expected_size,
                        target_episodes=episodes,
                        file_targets=targets,
                        torrent_name=torrent_name,
                        torrent_hash=download_id,
                        season=season,
                    )
                elif dtype == "transmission":
                    selected = await self._select_transmission_files(
                        config=config,
                        expected_size=expected_size,
                        target_episodes=episodes,
                        file_targets=targets,
                        torrent_name=torrent_name,
                        torrent_hash=download_id,
                        season=season,
                    )
                else:
                    continue
                selected["downloader"] = name
                selected["downloader_type"] = dtype
                if selected.get("success"):
                    return selected
                if selected.get("torrent_hash") or selected.get("torrent_id") or selected.get("torrent_name"):
                    task_hint = {
                        key: selected.get(key)
                        for key in ("downloader", "downloader_type", "torrent_hash", "torrent_id", "torrent_name")
                        if selected.get(key)
                    }
                errors.append(f"{name}: {selected.get('message', '未命中任务')}")
            except Exception as e:
                logger.exception("[MoviePilot] 下载器拆包失败: %s", name)
                errors.append(f"{name}: {e}")

        payload = {"success": False, "message": "; ".join(errors) or "未找到需要筛选的下载任务"}
        payload.update(task_hint)
        return payload

    async def annotate_downloader_statuses(self, results: list[dict]) -> list[dict]:
        """把 MP 搜索结果和下载器里已存在的任务状态合并，供前端避免重复提交。"""
        items = [dict(item) for item in (results or []) if isinstance(item, dict)]
        if not items:
            return items

        downloaders = await self.get_downloaders()
        if not downloaders:
            return items

        torrent_cache: dict[tuple[str, str, str], list[dict]] = {}
        for downloader in downloaders:
            dtype = str(downloader.get("type") or "").lower()
            name = str(downloader.get("name") or dtype)
            config = downloader.get("config") or {}
            host = _normalize_url(config.get("host") or "")
            key = (dtype, name, host)
            try:
                if dtype == "qbittorrent":
                    torrent_cache[key] = await self._list_qbittorrent_torrents(config)
                elif dtype == "transmission":
                    torrent_cache[key] = await self._list_transmission_torrents(config)
            except Exception:
                logger.exception("[MoviePilot] 读取下载器任务失败: %s", name)
                torrent_cache[key] = []

        for item in items:
            expected_size = _torrent_size(item)
            torrent_name = _torrent_title(item)
            for downloader in self._order_downloaders(downloaders, item):
                dtype = str(downloader.get("type") or "").lower()
                name = str(downloader.get("name") or dtype)
                host = _normalize_url((downloader.get("config") or {}).get("host") or "")
                torrents = torrent_cache.get((dtype, name, host)) or []
                torrent, reason = self._pick_existing_downloader_torrent(
                    torrents,
                    expected_size=expected_size,
                    torrent_name=torrent_name,
                    torrent_hash="",
                    hash_field="hashString" if dtype == "transmission" else "hash",
                    size_field="totalSize" if dtype == "transmission" else "size",
                    added_field="addedDate" if dtype == "transmission" else "added_on",
                )
                if not torrent:
                    continue
                status = (
                    self._transmission_status_payload(torrent, name)
                    if dtype == "transmission"
                    else self._qbittorrent_status_payload(torrent, name)
                )
                status["match_reason"] = reason
                item["ui_download_task"] = True
                item["ui_download_status"] = status.get("status")
                item["ui_download_label"] = status.get("label")
                item["ui_download_progress"] = status.get("progress", 0)
                item["ui_download_speed"] = status.get("download_speed", 0)
                item["ui_download_eta"] = status.get("eta", 0)
                item["ui_download_name"] = status.get("name", "")
                item["ui_download_id"] = status.get("id", "")
                item["ui_download_hash"] = status.get("hash", "")
                item["ui_download_state"] = status.get("raw_state", "")
                item["ui_downloader"] = name
                item["ui_downloader_type"] = dtype
                break
        return items

    async def cleanup_history_download_tasks(
        self,
        item_name: str = "",
        item_id: str = "",
        logs: list[dict] | None = None,
        delete_files: bool = False,
    ) -> dict:
        """删除失败历史关联的下载器任务。优先使用日志中的 hash/name，旧日志再保守匹配。"""
        downloaders = await self.get_downloaders()
        if not downloaders:
            return {"deleted": 0, "matched": 0, "details": [], "message": "没有可用下载器配置"}

        hints = self._history_cleanup_hints(item_name, item_id, logs or [])
        details: list[dict] = []
        deleted = 0
        matched = 0
        for downloader in downloaders:
            dtype = str(downloader.get("type") or "").lower()
            name = str(downloader.get("name") or dtype)
            if hints["downloaders"] and name.lower() not in hints["downloaders"] and dtype not in hints["downloaders"]:
                continue
            config = downloader.get("config") or {}
            try:
                if dtype == "qbittorrent":
                    result = await self._cleanup_qbittorrent_history_task(config, name, hints, delete_files)
                elif dtype == "transmission":
                    result = await self._cleanup_transmission_history_task(config, name, hints, delete_files)
                else:
                    continue
            except Exception as e:
                logger.exception("[MoviePilot] 删除历史下载器任务异常: %s", name)
                result = {"deleted": 0, "matched": 0, "message": str(e), "downloader": name, "type": dtype}
            deleted += _safe_int(result.get("deleted"), 0)
            matched += _safe_int(result.get("matched"), 0)
            if result.get("matched") or result.get("message"):
                details.append(result)
        return {
            "deleted": deleted,
            "matched": matched,
            "details": details,
            "message": f"已删除 {deleted} 个下载器任务" if deleted else "未找到可安全删除的下载器任务",
        }

    async def control_history_download_tasks(
        self,
        item_name: str = "",
        item_id: str = "",
        logs: list[dict] | None = None,
        action: str = "",
        delete_files: bool = True,
    ) -> dict:
        """暂停、继续或删除历史记录关联的下载器任务。"""
        action = str(action or "").strip().lower()
        action_labels = {"pause": "暂停", "resume": "继续", "delete": "删除"}
        if action not in action_labels:
            return {"success": False, "affected": 0, "matched": 0, "details": [], "message": "不支持的下载器操作"}

        downloaders = await self.get_downloaders()
        if not downloaders:
            return {"success": False, "affected": 0, "matched": 0, "details": [], "message": "没有可用下载器配置"}

        hints = self._history_cleanup_hints(item_name, item_id, logs or [])
        details: list[dict] = []
        affected = 0
        matched = 0
        noop = 0
        for downloader in downloaders:
            dtype = str(downloader.get("type") or "").lower()
            name = str(downloader.get("name") or dtype)
            if hints["downloaders"] and name.lower() not in hints["downloaders"] and dtype not in hints["downloaders"]:
                continue
            config = downloader.get("config") or {}
            try:
                if dtype == "qbittorrent":
                    result = await self._control_qbittorrent_history_task(config, name, hints, action, delete_files)
                elif dtype == "transmission":
                    result = await self._control_transmission_history_task(config, name, hints, action, delete_files)
                else:
                    continue
            except Exception as e:
                logger.exception("[MoviePilot] 历史下载器任务操作异常: %s", name)
                result = {"affected": 0, "matched": 0, "message": str(e), "downloader": name, "type": dtype}
            affected += _safe_int(result.get("affected"), 0)
            matched += _safe_int(result.get("matched"), 0)
            if result.get("noop"):
                noop += _safe_int(result.get("matched"), 0)
            if result.get("matched") or result.get("message"):
                details.append(result)

        label = action_labels[action]
        if affected:
            message = f"已{label} {affected} 个下载器任务"
        elif noop:
            message = "；".join(str(item.get("message") or "") for item in details if item.get("noop")) or f"任务无需{label}"
        elif matched:
            message = f"找到 {matched} 个下载器任务，但{label}失败"
        else:
            message = "未找到可安全匹配的下载器任务"
        return {
            "success": affected > 0 or noop > 0,
            "affected": affected,
            "matched": matched,
            "noop": noop,
            "details": details,
            "message": message,
        }

    @staticmethod
    def _history_cleanup_hints(item_name: str, item_id: str, logs: list[dict]) -> dict:
        text_parts = [str(item_name or ""), str(item_id or "")]
        created_times: list[int] = []
        names: list[str] = []
        for log in logs or []:
            if not isinstance(log, dict):
                continue
            message = str(log.get("message") or "")
            text_parts.extend([
                str(log.get("item_name") or ""),
                message,
            ])
            for match in re.finditer(r"资源[:：]\s*(.*?)(?:\s+·\s+|\n|$)", message):
                title = match.group(1).strip()
                if title:
                    names.append(title)
            created = str(log.get("created_at") or "").strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    created_times.append(int(time.mktime(datetime.strptime(created[:19], fmt).timetuple())))
                    break
                except (TypeError, ValueError):
                    continue
        text = "\n".join(part for part in text_parts if part)
        downloaders = {
            match.group(1).strip().lower()
            for match in re.finditer(r"(?:^|[;；:：]\s*)([A-Za-z0-9_\-\u4e00-\u9fff]{1,30})[:：]\s*[^;；\n]*(?:未在种子文件中识别|未识别到目标集)", text, re.I)
        }
        downloaders.update(
            match.group(1).strip().lower()
            for match in re.finditer(r"downloader\s*=\s*([^\s;\n]+)", text, re.I)
        )
        hashes = {
            match.group(1).strip().lower()
            for match in re.finditer(r"(?:hash|torrent_hash)\s*=\s*([a-f0-9]{16,40})", text, re.I)
        }
        transmission_ids = {
            _safe_int(match.group(1), 0)
            for match in re.finditer(r"(?:torrent_id|transmission_id)\s*=\s*(\d+)", text, re.I)
        }
        names.extend(
            match.group(1).strip()
            for match in re.finditer(r"(?:torrent_name|name)\s*=\s*\"([^\"]{3,180})\"", text, re.I)
        )
        base_name = re.sub(r"\s+S\d{1,2}\s*(?:整季|E\d{1,4}.*)?$", "", str(item_name or ""), flags=re.I).strip()
        if base_name:
            names.append(base_name)
        season_match = re.search(r":s(\d{1,2})(?::|$)", str(item_id or ""), re.I) or re.search(r"\bS(\d{1,2})\b", str(item_name or ""), re.I)
        season = _safe_int(season_match.group(1), -1) if season_match else -1
        return {
            "downloaders": downloaders,
            "hashes": hashes,
            "transmission_ids": {value for value in transmission_ids if value > 0},
            "names": list(dict.fromkeys(name for name in names if name)),
            "season": season,
            "created_times": created_times,
        }

    @staticmethod
    def _download_selection_markers(selection: dict) -> str:
        if not isinstance(selection, dict):
            return ""
        markers: list[str] = []
        downloader = str(selection.get("downloader") or "").strip()
        torrent_hash = str(selection.get("torrent_hash") or "").strip()
        torrent_id = _safe_int(selection.get("torrent_id"), 0)
        torrent_name = str(selection.get("torrent_name") or "").strip()
        if downloader:
            markers.append(f"downloader={downloader}")
        if torrent_hash:
            markers.append(f"hash={torrent_hash}")
        if torrent_id > 0:
            markers.append(f"torrent_id={torrent_id}")
        if torrent_name:
            safe_name = torrent_name.replace('"', "'")[:180]
            markers.append(f'torrent_name="{safe_name}"')
        return " ".join(markers)

    async def _cleanup_qbittorrent_history_task(self, config: dict, downloader_name: str, hints: dict, delete_files: bool) -> dict:
        host = _normalize_url(config.get("host") or "")
        if not host:
            return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": "qBittorrent 未配置 host"}
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            login = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": config.get("username") or "", "password": config.get("password") or ""},
            )
            if login.status_code >= 400 or "Fails" in login.text:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": f"qBittorrent 登录失败: HTTP {login.status_code}"}
            torrents_resp = await client.get(f"{host}/api/v2/torrents/info", params={"filter": "all", "sort": "added_on", "reverse": "true"})
            if torrents_resp.status_code >= 400:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": f"qBittorrent 任务读取失败: HTTP {torrents_resp.status_code}"}
            torrents = torrents_resp.json() if torrents_resp.content else []
            matches = self._match_history_torrents(
                torrents if isinstance(torrents, list) else [],
                hints,
                hash_field="hash",
                name_field="name",
                added_field="added_on",
            )
            hashes = [str(item.get("hash") or "").strip() for item in matches if item.get("hash")]
            if not hashes:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": "未找到可安全匹配的 qBittorrent 任务"}
            resp = await client.post(
                f"{host}/api/v2/torrents/delete",
                data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
            )
            text = (resp.text or "").strip()
            ok = resp.status_code < 400 and not text.lower().startswith("fails")
            return {
                "deleted": len(hashes) if ok else 0,
                "matched": len(hashes),
                "downloader": downloader_name,
                "type": "qbittorrent",
                "hashes": hashes,
                "names": [str(item.get("name") or "") for item in matches],
                "message": (
                    "qBittorrent 任务已删除，未删除本地文件" if ok and not delete_files
                    else "qBittorrent 任务已删除" if ok
                    else f"qBittorrent 删除失败: HTTP {resp.status_code}{f' {text[:80]}' if text else ''}"
                ),
            }

    async def _control_qbittorrent_history_task(
        self,
        config: dict,
        downloader_name: str,
        hints: dict,
        action: str,
        delete_files: bool,
    ) -> dict:
        host = _normalize_url(config.get("host") or "")
        label = {"pause": "暂停", "resume": "继续", "delete": "删除"}.get(action, action)
        if not host:
            return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": "qBittorrent 未配置 host"}
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            login = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": config.get("username") or "", "password": config.get("password") or ""},
            )
            if login.status_code >= 400 or "Fails" in login.text:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": f"qBittorrent 登录失败: HTTP {login.status_code}"}
            torrents_resp = await client.get(f"{host}/api/v2/torrents/info", params={"filter": "all", "sort": "added_on", "reverse": "true"})
            if torrents_resp.status_code >= 400:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": f"qBittorrent 任务读取失败: HTTP {torrents_resp.status_code}"}
            torrents = torrents_resp.json() if torrents_resp.content else []
            matches = self._match_history_torrents(
                torrents if isinstance(torrents, list) else [],
                hints,
                hash_field="hash",
                name_field="name",
                added_field="added_on",
            )
            hashes = [str(item.get("hash") or "").strip() for item in matches if item.get("hash")]
            if not hashes:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "qbittorrent", "message": "未找到可安全匹配的 qBittorrent 任务"}
            affected_hashes = hashes
            if action == "delete":
                resp = await client.post(
                    f"{host}/api/v2/torrents/delete",
                    data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
                )
                text = (resp.text or "").strip()
                ok = resp.status_code < 400 and not text.lower().startswith("fails")
                error_text = f": HTTP {resp.status_code}{f' {text[:80]}' if text else ''}"
            else:
                target_matches = matches
                if action == "resume":
                    target_matches = [
                        item for item in matches
                        if self._qbittorrent_status_payload(item, downloader_name).get("status") == "paused"
                    ]
                    if not target_matches:
                        labels = []
                        for item in matches:
                            status = self._qbittorrent_status_payload(item, downloader_name)
                            label_text = str(status.get("label") or status.get("raw_state") or "").strip()
                            if label_text and label_text not in labels:
                                labels.append(label_text)
                        current = "、".join(labels) or "未暂停"
                        return {
                            "affected": 0,
                            "matched": len(matches),
                            "noop": True,
                            "downloader": downloader_name,
                            "type": "qbittorrent",
                            "hashes": hashes,
                            "names": [str(item.get("name") or "") for item in matches],
                            "message": f"qBittorrent 任务当前为{current}，无需继续",
                        }
                target_hashes = [str(item.get("hash") or "").strip() for item in target_matches if item.get("hash")]
                affected_hashes = target_hashes
                ok = await self._set_qbittorrent_torrent_state(
                    client,
                    host,
                    "|".join(target_hashes),
                    "pause" if action == "pause" else "resume",
                )
                error_text = ""
            return {
                "affected": len(affected_hashes) if ok else 0,
                "matched": len(hashes),
                "downloader": downloader_name,
                "type": "qbittorrent",
                "hashes": hashes,
                "names": [str(item.get("name") or "") for item in matches],
                "message": f"qBittorrent 任务已{label}" if ok else f"qBittorrent {label}失败{error_text}",
            }

    async def _cleanup_transmission_history_task(self, config: dict, downloader_name: str, hints: dict, delete_files: bool) -> dict:
        rpc_url = _normalize_url(config.get("host") or "")
        if not rpc_url:
            return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "Transmission 未配置 host"}
        if not rpc_url.endswith("/rpc"):
            rpc_url = f"{rpc_url}/transmission/rpc"
        auth = None
        if config.get("username") or config.get("password"):
            auth = (config.get("username") or "", config.get("password") or "")
        async with httpx.AsyncClient(timeout=10.0, verify=False, auth=auth) as client:
            session_id = await self._transmission_session_id(client, rpc_url)
            if not session_id:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "Transmission 会话初始化失败"}
            headers = {"X-Transmission-Session-Id": session_id}
            payload = {
                "method": "torrent-get",
                "arguments": {"fields": ["id", "name", "totalSize", "addedDate", "hashString"]},
            }
            resp = await client.post(rpc_url, json=payload, headers=headers)
            if resp.status_code >= 400:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": f"Transmission 任务读取失败: HTTP {resp.status_code}"}
            torrents = (resp.json().get("arguments") or {}).get("torrents") or []
            matches = self._match_history_torrents(
                torrents if isinstance(torrents, list) else [],
                hints,
                hash_field="hashString",
                name_field="name",
                added_field="addedDate",
                id_field="id",
            )
            ids = [_safe_int(item.get("id"), 0) for item in matches if _safe_int(item.get("id"), 0) > 0]
            if not ids:
                return {"deleted": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "未找到可安全匹配的 Transmission 任务"}
            remove_resp = await client.post(
                rpc_url,
                json={"method": "torrent-remove", "arguments": {"ids": ids, "delete-local-data": bool(delete_files)}},
                headers=headers,
            )
            body = remove_resp.json() if remove_resp.status_code < 400 and remove_resp.content else {}
            ok = remove_resp.status_code < 400 and str(body.get("result") or "success").lower() == "success"
            return {
                "deleted": len(ids) if ok else 0,
                "matched": len(ids),
                "downloader": downloader_name,
                "type": "transmission",
                "ids": ids,
                "names": [str(item.get("name") or "") for item in matches],
                "message": (
                    "Transmission 任务已删除，未删除本地文件" if ok and not delete_files
                    else "Transmission 任务已删除" if ok
                    else f"Transmission 删除失败: {body.get('result') or remove_resp.status_code}"
                ),
            }

    async def _control_transmission_history_task(
        self,
        config: dict,
        downloader_name: str,
        hints: dict,
        action: str,
        delete_files: bool,
    ) -> dict:
        rpc_url = _normalize_url(config.get("host") or "")
        label = {"pause": "暂停", "resume": "继续", "delete": "删除"}.get(action, action)
        if not rpc_url:
            return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "Transmission 未配置 host"}
        if not rpc_url.endswith("/rpc"):
            rpc_url = f"{rpc_url}/transmission/rpc"
        auth = None
        if config.get("username") or config.get("password"):
            auth = (config.get("username") or "", config.get("password") or "")
        async with httpx.AsyncClient(timeout=10.0, verify=False, auth=auth) as client:
            session_id = await self._transmission_session_id(client, rpc_url)
            if not session_id:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "Transmission 会话初始化失败"}
            headers = {"X-Transmission-Session-Id": session_id}
            payload = {
                "method": "torrent-get",
                "arguments": {
                    "fields": [
                        "id", "name", "totalSize", "addedDate", "hashString",
                        "status", "percentDone", "rateDownload", "leftUntilDone", "isFinished", "error",
                    ],
                },
            }
            resp = await client.post(rpc_url, json=payload, headers=headers)
            if resp.status_code >= 400:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": f"Transmission 任务读取失败: HTTP {resp.status_code}"}
            torrents = (resp.json().get("arguments") or {}).get("torrents") or []
            matches = self._match_history_torrents(
                torrents if isinstance(torrents, list) else [],
                hints,
                hash_field="hashString",
                name_field="name",
                added_field="addedDate",
                id_field="id",
            )
            ids = [_safe_int(item.get("id"), 0) for item in matches if _safe_int(item.get("id"), 0) > 0]
            if not ids:
                return {"affected": 0, "matched": 0, "downloader": downloader_name, "type": "transmission", "message": "未找到可安全匹配的 Transmission 任务"}
            method = {"pause": "torrent-stop", "resume": "torrent-start", "delete": "torrent-remove"}[action]
            arguments = {"ids": ids}
            if action == "delete":
                arguments["delete-local-data"] = bool(delete_files)
            action_resp = await client.post(rpc_url, json={"method": method, "arguments": arguments}, headers=headers)
            body = action_resp.json() if action_resp.status_code < 400 and action_resp.content else {}
            ok = action_resp.status_code < 400 and str(body.get("result") or "success").lower() == "success"
            return {
                "affected": len(ids) if ok else 0,
                "matched": len(ids),
                "downloader": downloader_name,
                "type": "transmission",
                "ids": ids,
                "names": [str(item.get("name") or "") for item in matches],
                "message": f"Transmission 任务已{label}" if ok else f"Transmission {label}失败: {body.get('result') or action_resp.status_code}",
            }

    @staticmethod
    def _match_history_torrents(
        torrents: list[dict],
        hints: dict,
        *,
        hash_field: str,
        name_field: str,
        added_field: str,
        id_field: str = "",
    ) -> list[dict]:
        if not torrents:
            return []
        hashes = {str(value or "").lower() for value in hints.get("hashes") or [] if value}
        ids = {_safe_int(value, 0) for value in hints.get("transmission_ids") or [] if _safe_int(value, 0) > 0}
        if hashes or ids:
            exact = []
            for item in torrents:
                item_hash = str(item.get(hash_field) or item.get("hash") or item.get("hashString") or "").lower()
                item_id = _safe_int(item.get(id_field), 0) if id_field else 0
                if (hashes and item_hash in hashes) or (ids and item_id in ids):
                    exact.append(item)
            return exact

        names = [str(name or "") for name in hints.get("names") or [] if str(name or "").strip()]
        normalized_names = [_normalize_torrent_name(name) for name in names if _normalize_torrent_name(name)]
        token_sets = [_torrent_match_tokens(name) for name in names if _torrent_match_tokens(name)]
        candidates: list[tuple[int, int, dict]] = []
        for item in torrents[:800]:
            item_name = str(item.get(name_field) or "")
            normalized_item = _normalize_torrent_name(item_name)
            item_tokens = _torrent_match_tokens(item_name)
            score = 0
            for normalized in normalized_names:
                if normalized and normalized_item and (normalized in normalized_item or normalized_item in normalized):
                    score = max(score, 80)
            for tokens in token_sets:
                overlap = len(tokens & item_tokens)
                if overlap >= 3 and overlap / max(1, len(tokens)) >= 0.6:
                    score = max(score, 55 + min(20, overlap * 2))
            if hints.get("season") is not None and _safe_int(hints.get("season"), -1) >= 0:
                season = _safe_int(hints.get("season"), -1)
                if re.search(rf"(?<!\d)s0?{season}(?!\d)|第\s*0?{season}\s*季", item_name, re.I):
                    score += 10
            if score >= 60:
                candidates.append((score, _safe_int(item.get(added_field), 0), item))
        if candidates:
            candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
            best_score = candidates[0][0]
            best_matches = [item for score, _, item in candidates if score == best_score]
            return best_matches if len(best_matches) == 1 else []

        created_times = [_safe_int(value, 0) for value in hints.get("created_times") or [] if _safe_int(value, 0) > 0]
        if not created_times:
            return []
        timed = []
        for item in torrents[:120]:
            added = _safe_int(item.get(added_field), 0)
            if not added:
                continue
            if min(abs(added - created) for created in created_times) > 600:
                continue
            progress = float(item.get("progress") or item.get("percentDone") or 0)
            state = str(item.get("state") or item.get("status") or "").lower()
            if progress > 0.02 and not any(marker in state for marker in ("pause", "stop", "stall")):
                continue
            timed.append(item)
        return timed if len(timed) == 1 else []

    async def _list_qbittorrent_torrents(self, config: dict) -> list[dict]:
        host = _normalize_url(config.get("host") or "")
        if not host:
            return []
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            login = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": config.get("username") or "", "password": config.get("password") or ""},
            )
            if login.status_code >= 400 or "Fails" in login.text:
                return []
            resp = await client.get(
                f"{host}/api/v2/torrents/info",
                params={"filter": "all", "sort": "added_on", "reverse": "true"},
            )
            if resp.status_code >= 400:
                return []
            data = resp.json() if resp.content else []
            return data if isinstance(data, list) else []

    async def _list_transmission_torrents(self, config: dict) -> list[dict]:
        rpc_url = _normalize_url(config.get("host") or "")
        if not rpc_url:
            return []
        if not rpc_url.endswith("/rpc"):
            rpc_url = f"{rpc_url}/transmission/rpc"
        auth = None
        if config.get("username") or config.get("password"):
            auth = (config.get("username") or "", config.get("password") or "")
        async with httpx.AsyncClient(timeout=8.0, verify=False, auth=auth) as client:
            session_id = await self._transmission_session_id(client, rpc_url)
            if not session_id:
                return []
            payload = {
                "method": "torrent-get",
                "arguments": {
                    "fields": [
                        "id", "name", "totalSize", "addedDate", "hashString",
                        "status", "percentDone", "rateDownload", "rateUpload",
                        "leftUntilDone", "isFinished", "error", "errorString",
                    ],
                },
            }
            resp = await client.post(rpc_url, json=payload, headers={"X-Transmission-Session-Id": session_id})
            if resp.status_code >= 400:
                return []
            return (resp.json().get("arguments") or {}).get("torrents") or []

    def _order_downloaders(self, downloaders: list[dict], torrent_info: dict | None) -> list[dict]:
        preferred = _preferred_downloader_name(torrent_info)
        if preferred:
            preferred_lower = preferred.lower()
            matched = [
                item for item in downloaders
                if str(item.get("name") or "").lower() == preferred_lower
            ]
            rest = [item for item in downloaders if item not in matched]
            return matched + rest
        return sorted(downloaders, key=lambda x: 0 if x.get("default") else 1)

    @staticmethod
    def _download_id(download_result: dict | None) -> str:
        if not isinstance(download_result, dict):
            return ""
        data = download_result.get("data")
        if isinstance(data, dict):
            return str(data.get("download_id") or data.get("hash") or "").strip()
        return str(download_result.get("download_id") or download_result.get("hash") or "").strip()

    async def _select_qbittorrent_files(
        self,
        config: dict,
        expected_size: int,
        target_episodes: list[int],
        file_targets: list[dict],
        torrent_name: str,
        torrent_hash: str,
        season: int,
    ) -> dict:
        host = _normalize_url(config.get("host") or "")
        if not host:
            return {"success": False, "message": "qBittorrent 未配置 host"}

        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            login = await client.post(
                f"{host}/api/v2/auth/login",
                data={"username": config.get("username") or "", "password": config.get("password") or ""},
            )
            if login.status_code >= 400 or "Fails" in login.text:
                return {"success": False, "message": f"qBittorrent 登录失败: HTTP {login.status_code}"}

            for _ in range(24):
                torrent = await self._find_qbittorrent_torrent(
                    client=client,
                    host=host,
                    expected_size=expected_size,
                    torrent_name=torrent_name,
                    torrent_hash=torrent_hash,
                )
                if torrent:
                    selected_hash = str(torrent.get("hash") or "")
                    selected_name = str(torrent.get("name") or "")
                    paused = await self._pause_qbittorrent_torrent(client, host, selected_hash)
                    if not paused:
                        return {
                            "success": False,
                            "message": "qBittorrent 暂停任务失败，已拦截避免整包下载",
                            "torrent_hash": selected_hash,
                            "torrent_name": selected_name,
                        }
                    selected = await self._apply_qbittorrent_selection(
                        client=client,
                        host=host,
                        torrent_hash=selected_hash,
                        target_episodes=target_episodes,
                        file_targets=file_targets,
                        season=season,
                    )
                    if selected.get("success"):
                        resumed = await self._resume_qbittorrent_torrent(client, host, selected_hash)
                        selected["resumed"] = resumed
                        if not resumed:
                            selected["message"] = f"{selected.get('message', '文件选择已确认')}，但 qBittorrent 恢复下载失败，请手动继续任务"
                    else:
                        await self._pause_qbittorrent_torrent(client, host, selected_hash)
                    selected["torrent_hash"] = selected_hash
                    selected["torrent_name"] = selected_name
                    return selected
                await asyncio.sleep(1)
        return {"success": False, "message": "qBittorrent 中未找到刚提交的任务"}

    async def _find_qbittorrent_torrent(
        self,
        client: httpx.AsyncClient,
        host: str,
        expected_size: int,
        torrent_name: str,
        torrent_hash: str,
    ) -> dict | None:
        resp = await client.get(f"{host}/api/v2/torrents/info", params={"filter": "all", "sort": "added_on", "reverse": "true"})
        if resp.status_code >= 400:
            return None
        torrents = resp.json() if resp.content else []
        if not isinstance(torrents, list):
            return None
        torrent, reason = self._pick_downloader_torrent(
            torrents,
            expected_size=expected_size,
            torrent_name=torrent_name,
            torrent_hash=torrent_hash,
            size_field="size",
            added_field="added_on",
        )
        if torrent:
            logger.info(
                "[MoviePilot] qB 锁定下载任务: reason=%s hash=%s name=%s",
                reason,
                str(torrent.get("hash") or "")[:12],
                str(torrent.get("name") or "")[:120],
            )
        return torrent

    async def _apply_qbittorrent_selection(
        self,
        client: httpx.AsyncClient,
        host: str,
        torrent_hash: str,
        target_episodes: list[int],
        file_targets: list[dict],
        season: int,
    ) -> dict:
        if not torrent_hash:
            return {"success": False, "message": "qBittorrent 任务缺少 hash"}
        files_resp = await client.get(f"{host}/api/v2/torrents/files", params={"hash": torrent_hash})
        if files_resp.status_code >= 400:
            return {"success": False, "message": f"qBittorrent 文件列表读取失败: HTTP {files_resp.status_code}"}
        files = files_resp.json() if files_resp.content else []
        partition = self._partition_files_by_targets(files, file_targets)
        wanted = partition["wanted"]
        unwanted = partition["unwanted"]
        logger.info(
            "[MoviePilot] qB 拆包选择 hash=%s season=%s episodes=%s mode=%s files=%s wanted=%s unwanted=%s samples=%s",
            torrent_hash[:12],
            season,
            _format_file_targets(file_targets),
            partition.get("mode"),
            len(files),
            len(wanted),
            len(unwanted),
            partition.get("wanted_names", [])[:5],
        )
        if not wanted:
            paused = await self._pause_qbittorrent_torrent(client, host, torrent_hash)
            if not paused:
                return {"success": False, "message": "未识别到目标集，且 qBittorrent 暂停/停止失败"}
            return {"success": False, "message": "未在种子文件中识别到目标集，已尝试暂停任务避免整包下载"}

        if unwanted:
            ok, message = await self._set_qbittorrent_file_priority(
                client, host, torrent_hash, unwanted, 0, "忽略非目标文件"
            )
            if not ok:
                await self._pause_qbittorrent_torrent(client, host, torrent_hash)
                return {"success": False, "message": message}
        ok, message = await self._set_qbittorrent_file_priority(
            client, host, torrent_hash, wanted, 1, "选择目标文件"
        )
        if not ok:
            await self._pause_qbittorrent_torrent(client, host, torrent_hash)
            return {"success": False, "message": message}

        verify = await self._verify_qbittorrent_selection(client, host, torrent_hash, wanted, unwanted)
        if not verify.get("success"):
            await self._pause_qbittorrent_torrent(client, host, torrent_hash)
            return verify
        return {
            "success": True,
            "message": f"已选择 {len(wanted)} 个文件，忽略 {len(unwanted)} 个文件",
            "wanted_files": len(wanted),
            "unwanted_files": len(unwanted),
            "selection_mode": partition.get("mode"),
            "wanted_samples": partition.get("wanted_names", [])[:5],
        }

    async def _set_qbittorrent_torrent_state(
        self,
        client: httpx.AsyncClient,
        host: str,
        torrent_hash: str,
        action: str,
    ) -> bool:
        if not torrent_hash:
            return False
        endpoints = [action]
        if action == "pause":
            endpoints.append("stop")
        if action == "resume":
            endpoints.append("start")
        for endpoint in endpoints:
            resp = await client.post(f"{host}/api/v2/torrents/{endpoint}", data={"hashes": torrent_hash})
            text = (resp.text or "").strip().lower()
            if resp.status_code < 400 and not text.startswith("fails"):
                return True
        return False

    async def _pause_qbittorrent_torrent(self, client: httpx.AsyncClient, host: str, torrent_hash: str) -> bool:
        return await self._set_qbittorrent_torrent_state(client, host, torrent_hash, "pause")

    async def _resume_qbittorrent_torrent(self, client: httpx.AsyncClient, host: str, torrent_hash: str) -> bool:
        return await self._set_qbittorrent_torrent_state(client, host, torrent_hash, "resume")

    async def _set_qbittorrent_file_priority(
        self,
        client: httpx.AsyncClient,
        host: str,
        torrent_hash: str,
        file_ids: list[int],
        priority: int,
        action: str,
    ) -> tuple[bool, str]:
        if not file_ids:
            return True, ""
        chunk_size = 200
        for offset in range(0, len(file_ids), chunk_size):
            chunk = file_ids[offset:offset + chunk_size]
            resp = await client.post(
                f"{host}/api/v2/torrents/filePrio",
                data={"hash": torrent_hash, "id": "|".join(map(str, chunk)), "priority": priority},
            )
            text = (resp.text or "").strip()
            if resp.status_code >= 400 or text.lower().startswith("fails"):
                logger.warning(
                    "[MoviePilot] qB %s 失败 hash=%s priority=%s offset=%s count=%s status=%s body=%s",
                    action,
                    torrent_hash[:12],
                    priority,
                    offset,
                    len(chunk),
                    resp.status_code,
                    text[:200],
                )
                return False, f"qBittorrent {action}失败: HTTP {resp.status_code}{f' {text[:80]}' if text else ''}"
        return True, ""

    async def _verify_qbittorrent_selection(
        self,
        client: httpx.AsyncClient,
        host: str,
        torrent_hash: str,
        wanted: list[int],
        unwanted: list[int],
    ) -> dict:
        resp = await client.get(f"{host}/api/v2/torrents/files", params={"hash": torrent_hash})
        if resp.status_code >= 400:
            return {"success": False, "message": f"qBittorrent 文件选择复查失败: HTTP {resp.status_code}"}
        files = resp.json() if resp.content else []
        selected = {
            _safe_int(item.get("index"), idx)
            for idx, item in enumerate(files or [])
            if _safe_int(item.get("priority"), 0) > 0
        }
        wanted_set = set(wanted)
        unwanted_set = set(unwanted)
        missing = sorted(wanted_set - selected)
        still_selected = sorted(unwanted_set & selected)
        if missing or still_selected:
            return {
                "success": False,
                "message": (
                    "qBittorrent 文件选择复查未通过"
                    f"：目标未选 {len(missing)} 个，非目标仍选 {len(still_selected)} 个"
                ),
                "missing_wanted": missing[:20],
                "still_selected": still_selected[:20],
            }
        return {"success": True, "message": "qBittorrent 文件选择已确认"}

    async def _select_transmission_files(
        self,
        config: dict,
        expected_size: int,
        target_episodes: list[int],
        file_targets: list[dict],
        torrent_name: str,
        torrent_hash: str,
        season: int,
    ) -> dict:
        rpc_url = _normalize_url(config.get("host") or "")
        if not rpc_url:
            return {"success": False, "message": "Transmission 未配置 host"}
        if not rpc_url.endswith("/rpc"):
            rpc_url = f"{rpc_url}/transmission/rpc"
        auth = None
        if config.get("username") or config.get("password"):
            auth = (config.get("username") or "", config.get("password") or "")

        async with httpx.AsyncClient(timeout=10.0, verify=False, auth=auth) as client:
            session_id = await self._transmission_session_id(client, rpc_url)
            if not session_id:
                return {"success": False, "message": "Transmission 会话初始化失败"}
            headers = {"X-Transmission-Session-Id": session_id}

            for _ in range(24):
                torrent = await self._find_transmission_torrent(
                    client=client,
                    rpc_url=rpc_url,
                    headers=headers,
                    expected_size=expected_size,
                    torrent_name=torrent_name,
                    torrent_hash=torrent_hash,
                )
                if torrent:
                    return await self._apply_transmission_selection(
                        client=client,
                        rpc_url=rpc_url,
                        headers=headers,
                        torrent=torrent,
                        target_episodes=target_episodes,
                        file_targets=file_targets,
                        season=season,
                    )
                await asyncio.sleep(1)
        return {"success": False, "message": "Transmission 中未找到刚提交的任务"}

    async def _transmission_session_id(self, client: httpx.AsyncClient, rpc_url: str) -> str:
        resp = await client.post(rpc_url, json={"method": "session-get"})
        if resp.status_code == 409:
            return resp.headers.get("X-Transmission-Session-Id", "")
        if resp.status_code < 400:
            return resp.headers.get("X-Transmission-Session-Id", "")
        return ""

    async def _find_transmission_torrent(
        self,
        client: httpx.AsyncClient,
        rpc_url: str,
        headers: dict,
        expected_size: int,
        torrent_name: str,
        torrent_hash: str,
    ) -> dict | None:
        payload = {
            "method": "torrent-get",
            "arguments": {
                "fields": ["id", "name", "totalSize", "addedDate", "hashString", "files"],
            },
        }
        resp = await client.post(rpc_url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return None
        torrents = (resp.json().get("arguments") or {}).get("torrents") or []
        torrent, reason = self._pick_downloader_torrent(
            torrents,
            expected_size=expected_size,
            torrent_name=torrent_name,
            torrent_hash=torrent_hash,
            hash_field="hashString",
            size_field="totalSize",
            added_field="addedDate",
        )
        if torrent:
            logger.info(
                "[MoviePilot] Transmission 锁定下载任务: reason=%s id=%s name=%s",
                reason,
                torrent.get("id"),
                str(torrent.get("name") or "")[:120],
            )
        return torrent

    async def _apply_transmission_selection(
        self,
        client: httpx.AsyncClient,
        rpc_url: str,
        headers: dict,
        torrent: dict,
        target_episodes: list[int],
        file_targets: list[dict],
        season: int,
    ) -> dict:
        torrent_id = torrent.get("id")
        torrent_hash = str(torrent.get("hashString") or torrent.get("hash") or "")
        torrent_name = str(torrent.get("name") or "")
        files = torrent.get("files") or []
        await client.post(
            rpc_url,
            json={"method": "torrent-stop", "arguments": {"ids": [torrent_id]}},
            headers=headers,
        )
        partition = self._partition_files_by_targets(files, file_targets)
        wanted = partition["wanted"]
        unwanted = partition["unwanted"]
        logger.info(
            "[MoviePilot] Transmission 拆包选择 season=%s episodes=%s mode=%s files=%s wanted=%s unwanted=%s samples=%s",
            season,
            _format_file_targets(file_targets),
            partition.get("mode"),
            len(files),
            len(wanted),
            len(unwanted),
            partition.get("wanted_names", [])[:5],
        )
        if not wanted:
            await client.post(
                rpc_url,
                json={"method": "torrent-stop", "arguments": {"ids": [torrent_id]}},
                headers=headers,
            )
            return {
                "success": False,
                "message": "未在种子文件中识别到目标集，已尝试暂停任务避免整包下载",
                "torrent_id": torrent_id,
                "torrent_hash": torrent_hash,
                "torrent_name": torrent_name,
            }

        payload = {
            "method": "torrent-set",
            "arguments": {"ids": [torrent_id], "files-wanted": wanted, "files-unwanted": unwanted},
        }
        resp = await client.post(rpc_url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return {"success": False, "message": f"Transmission 文件选择失败: HTTP {resp.status_code}"}
        try:
            rpc_body = resp.json()
        except Exception:
            rpc_body = {}
        if str(rpc_body.get("result") or "").lower() != "success":
            return {"success": False, "message": f"Transmission 文件选择失败: {rpc_body.get('result') or '未知响应'}"}
        verify = await self._verify_transmission_selection(
            client=client,
            rpc_url=rpc_url,
            headers=headers,
            torrent_id=torrent_id,
            wanted=wanted,
            unwanted=unwanted,
        )
        if not verify.get("success"):
            return verify
        start_resp = await client.post(
            rpc_url,
            json={"method": "torrent-start", "arguments": {"ids": [torrent_id]}},
            headers=headers,
        )
        try:
            start_body = start_resp.json() if start_resp.status_code < 400 else {}
        except Exception:
            start_body = {}
        return {
            "success": True,
            "message": (
                f"已选择 {len(wanted)} 个文件，忽略 {len(unwanted)} 个文件"
                if str(start_body.get("result") or "success").lower() == "success"
                else f"已选择 {len(wanted)} 个文件，忽略 {len(unwanted)} 个文件，但 Transmission 恢复下载失败，请手动继续任务"
            ),
            "wanted_files": len(wanted),
            "unwanted_files": len(unwanted),
            "selection_mode": partition.get("mode"),
            "wanted_samples": partition.get("wanted_names", [])[:5],
            "torrent_id": torrent_id,
            "torrent_hash": torrent_hash,
            "torrent_name": torrent_name,
        }

    async def _verify_transmission_selection(
        self,
        client: httpx.AsyncClient,
        rpc_url: str,
        headers: dict,
        torrent_id: Any,
        wanted: list[int],
        unwanted: list[int],
    ) -> dict:
        payload = {
            "method": "torrent-get",
            "arguments": {"ids": [torrent_id], "fields": ["id", "fileStats"]},
        }
        resp = await client.post(rpc_url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return {"success": False, "message": f"Transmission 文件选择复查失败: HTTP {resp.status_code}"}
        torrents = (resp.json().get("arguments") or {}).get("torrents") or []
        if not torrents:
            return {"success": False, "message": "Transmission 文件选择复查失败: 未找到任务"}
        stats = torrents[0].get("fileStats") or []
        selected = {idx for idx, item in enumerate(stats) if item.get("wanted")}
        wanted_set = set(wanted)
        unwanted_set = set(unwanted)
        missing = sorted(wanted_set - selected)
        still_selected = sorted(unwanted_set & selected)
        if missing or still_selected:
            return {
                "success": False,
                "message": (
                    "Transmission 文件选择复查未通过"
                    f"：目标未选 {len(missing)} 个，非目标仍选 {len(still_selected)} 个"
                ),
                "missing_wanted": missing[:20],
                "still_selected": still_selected[:20],
            }
        return {"success": True, "message": "Transmission 文件选择已确认"}

    @staticmethod
    def _matches_downloader_torrent(
        item: dict,
        expected_size: int,
        torrent_name: str,
        torrent_hash: str,
    ) -> bool:
        if torrent_hash:
            item_hash = str(item.get("hash") or item.get("hashString") or "").lower()
            if item_hash and item_hash == torrent_hash.lower():
                return True
        item_size = _safe_int(item.get("size") or item.get("totalSize"), 0)
        item_name = str(item.get("name") or "").lower()
        source_name = str(torrent_name or "").lower()
        name_match = bool(source_name and (source_name in item_name or item_name in source_name))
        if expected_size and item_size:
            tolerance = max(100 * 1024 * 1024, int(expected_size * 0.02))
            size_match = abs(item_size - expected_size) <= tolerance
            return size_match and (name_match or not source_name)
        return name_match

    @staticmethod
    def _pick_downloader_torrent(
        torrents: list[dict],
        expected_size: int,
        torrent_name: str,
        torrent_hash: str,
        hash_field: str = "hash",
        size_field: str = "size",
        added_field: str = "added_on",
    ) -> tuple[dict | None, str]:
        """按 MP 提交后的下载器任务特征锁定种子，参考 EmbyPulse 的截胡策略。"""
        if not isinstance(torrents, list):
            return None, ""

        now = time.time()
        recent: list[dict] = []
        source_name = str(torrent_name or "").lower()
        normalized_source = _normalize_torrent_name(source_name)
        expected_size = _safe_int(expected_size, 0)
        size_tolerance = max(100 * 1024 * 1024, int(expected_size * 0.02)) if expected_size else 0

        for item in torrents[:200]:
            item_hash = str(item.get(hash_field) or item.get("hash") or item.get("hashString") or "").lower()
            if torrent_hash and item_hash and item_hash == torrent_hash.lower():
                return item, "hash"

            added = _safe_int(item.get(added_field) or item.get("added_on") or item.get("addedDate"), 0)
            age = now - added if added > 0 else 0
            if added <= 0 or age <= 300:
                recent.append(item)

        for item in recent:
            item_size = _safe_int(item.get(size_field) or item.get("size") or item.get("totalSize"), 0)
            item_name = str(item.get("name") or "").lower()
            normalized_item = _normalize_torrent_name(item_name)
            size_match = bool(expected_size and item_size and abs(item_size - expected_size) <= size_tolerance)
            name_match = bool(
                normalized_source
                and normalized_item
                and (normalized_source in normalized_item or normalized_item in normalized_source)
            )
            if size_match and name_match:
                return item, "size+name"

        for item in recent:
            item_size = _safe_int(item.get(size_field) or item.get("size") or item.get("totalSize"), 0)
            if expected_size and item_size and abs(item_size - expected_size) <= size_tolerance:
                return item, "recent-size"

        for item in recent:
            item_name = str(item.get("name") or "").lower()
            normalized_item = _normalize_torrent_name(item_name)
            if normalized_source and normalized_item and (
                normalized_source in normalized_item or normalized_item in normalized_source
            ):
                return item, "recent-name"

        if not expected_size and len(recent) == 1:
            return recent[0], "single-recent"
        return None, ""

    @staticmethod
    def _pick_existing_downloader_torrent(
        torrents: list[dict],
        expected_size: int,
        torrent_name: str,
        torrent_hash: str,
        hash_field: str = "hash",
        size_field: str = "size",
        added_field: str = "added_on",
    ) -> tuple[dict | None, str]:
        """从下载器全部任务中匹配一个已存在的资源，不限制 added 时间。"""
        if not isinstance(torrents, list):
            return None, ""

        source_name = str(torrent_name or "").lower()
        normalized_source = _normalize_torrent_name(source_name)
        source_tokens = _torrent_match_tokens(source_name)
        expected_size = _safe_int(expected_size, 0)
        size_tolerance = max(100 * 1024 * 1024, int(expected_size * 0.02)) if expected_size else 0

        candidates: list[tuple[int, int, dict, str]] = []
        for item in torrents[:800]:
            item_hash = str(item.get(hash_field) or item.get("hash") or item.get("hashString") or "").lower()
            if torrent_hash and item_hash and item_hash == torrent_hash.lower():
                return item, "hash"

            item_size = _safe_int(item.get(size_field) or item.get("size") or item.get("totalSize"), 0)
            item_name = str(item.get("name") or "").lower()
            normalized_item = _normalize_torrent_name(item_name)
            if not normalized_source or not normalized_item:
                continue

            item_tokens = _torrent_match_tokens(item_name)
            overlap = len(source_tokens & item_tokens) if source_tokens and item_tokens else 0
            token_ratio = overlap / max(1, len(source_tokens))
            name_match = (
                normalized_source in normalized_item
                or normalized_item in normalized_source
                or (overlap >= 4 and token_ratio >= 0.66)
            )
            size_match = bool(expected_size and item_size and abs(item_size - expected_size) <= size_tolerance)
            if not name_match and not size_match:
                continue

            score = 0
            reason = ""
            if normalized_source == normalized_item:
                score += 80
                reason = "name-exact"
            elif name_match:
                score += 55 + min(20, overlap * 2)
                reason = "name-token" if overlap else "name"
            if size_match:
                score += 45
                reason = f"{reason}+size" if reason else "size"
            if not name_match:
                continue
            added = _safe_int(item.get(added_field) or item.get("added_on") or item.get("addedDate"), 0)
            candidates.append((score, added, item, reason))

        if not candidates:
            return None, ""
        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        score, _, item, reason = candidates[0]
        if score < 55:
            return None, ""
        return item, reason

    @staticmethod
    def _qbittorrent_status_payload(torrent: dict, downloader_name: str) -> dict:
        state = str(torrent.get("state") or "").strip()
        state_lower = state.lower()
        progress = max(0.0, min(1.0, float(torrent.get("progress") or 0)))
        amount_left = _safe_int(torrent.get("amount_left"), 0)
        dlspeed = _safe_int(torrent.get("dlspeed"), 0)
        eta = _safe_int(torrent.get("eta"), 0)

        if "error" in state_lower or "missing" in state_lower:
            status, label = "error", "下载异常"
        elif progress >= 0.999 or (amount_left <= 0 and state_lower in {"uploading", "stalledup", "queuedup", "pausedup", "stoppedup", "forcedup"}):
            status, label = "completed", "已完成"
        elif state_lower in {"pauseddl", "stoppeddl", "paused", "stopped"}:
            status, label = "paused", "已暂停"
        elif "checking" in state_lower:
            status, label = "checking", "校验中"
        elif state_lower in {"queueddl", "queued"}:
            status, label = "queued", "排队中"
        elif state_lower in {"stalleddl"} and dlspeed <= 0:
            status, label = "stalled", "等待下载"
        elif state_lower in {"metadl", "downloading", "forceddl", "stalleddl", "allocating"} or dlspeed > 0 or amount_left > 0:
            status, label = "downloading", "正在下载"
        else:
            status, label = "unknown", "已在下载器"

        return {
            "status": status,
            "label": label,
            "progress": progress,
            "download_speed": dlspeed,
            "eta": eta,
            "name": str(torrent.get("name") or ""),
            "id": str(torrent.get("hash") or ""),
            "hash": str(torrent.get("hash") or ""),
            "raw_state": state,
            "downloader": downloader_name,
        }

    @staticmethod
    def _transmission_status_payload(torrent: dict, downloader_name: str) -> dict:
        status_code = _safe_int(torrent.get("status"), -1)
        progress = max(0.0, min(1.0, float(torrent.get("percentDone") or 0)))
        left = _safe_int(torrent.get("leftUntilDone"), 0)
        rate = _safe_int(torrent.get("rateDownload"), 0)
        error = _safe_int(torrent.get("error"), 0)

        if error:
            status, label = "error", "下载异常"
        elif progress >= 0.999 or bool(torrent.get("isFinished")) or left <= 0:
            status, label = "completed", "已完成"
        elif status_code == 0:
            status, label = "paused", "已暂停"
        elif status_code in {1, 2, 3}:
            status, label = "checking", "校验中"
        elif status_code == 4 or rate > 0:
            status, label = "downloading", "正在下载"
        elif status_code in {5, 6}:
            status, label = "completed", "已完成"
        else:
            status, label = "unknown", "已在下载器"

        return {
            "status": status,
            "label": label,
            "progress": progress,
            "download_speed": rate,
            "eta": 0,
            "name": str(torrent.get("name") or ""),
            "id": _safe_int(torrent.get("id"), 0),
            "hash": str(torrent.get("hashString") or ""),
            "raw_state": str(status_code),
            "downloader": downloader_name,
        }

    @staticmethod
    def _partition_files_by_episode(
        files: list[dict],
        target_episodes: list[int],
        season: int,
    ) -> dict:
        return MoviePilotClient._partition_files_by_targets(
            files,
            _normalise_file_targets(season=season, episodes=target_episodes),
        )

    @staticmethod
    def _partition_files_by_targets(
        files: list[dict],
        file_targets: list[dict],
    ) -> dict:
        targets = _normalise_file_targets(file_targets=file_targets)
        target_by_season = {
            _safe_int(target.get("season"), 0): set(target.get("episodes") or [])
            for target in targets
        }
        full_season_targets = {
            _safe_int(target.get("season"), -1)
            for target in targets
            if bool(target.get("season_missing")) and not target.get("episodes")
        }
        wanted: list[int] = []
        unwanted: list[int] = []
        wanted_names: list[str] = []
        for idx, item in enumerate(files or []):
            name = str(item.get("name") or "")
            if not _is_media_or_sidecar(name):
                unwanted.append(_safe_int(item.get("index"), idx))
                continue
            file_index = _safe_int(item.get("index"), idx)
            matched = False
            for file_season, target_set in target_by_season.items():
                if file_season in full_season_targets and not target_set:
                    if file_season in _extract_file_seasons(name):
                        matched = True
                        break
                else:
                    episodes = _extract_file_episodes(name, season=file_season)
                    if episodes and episodes.intersection(target_set):
                        matched = True
                        break
            if matched:
                wanted.append(file_index)
                wanted_names.append(name)
            else:
                unwanted.append(file_index)
        if wanted:
            return {
                "wanted": wanted,
                "unwanted": unwanted,
                "wanted_names": wanted_names,
                "mode": "direct",
            }

        absolute_wanted: set[int] = set()
        absolute_names: list[str] = []
        for file_season, target_set in target_by_season.items():
            absolute = MoviePilotClient._partition_absolute_episode_pack(files, target_set, file_season)
            if absolute["wanted"]:
                absolute_wanted.update(absolute["wanted"])
                absolute_names.extend(absolute.get("wanted_names") or [])
        if absolute_wanted:
            wanted = []
            unwanted = []
            wanted_names = []
            for idx, item in enumerate(files or []):
                name = str(item.get("name") or "")
                file_index = _safe_int(item.get("index"), idx)
                if file_index in absolute_wanted:
                    wanted.append(file_index)
                    wanted_names.append(name)
                else:
                    unwanted.append(file_index)
            return {
                "wanted": wanted,
                "unwanted": unwanted,
                "wanted_names": wanted_names or absolute_names,
                "mode": "absolute",
            }
        return {
            "wanted": wanted,
            "unwanted": unwanted,
            "wanted_names": wanted_names,
            "mode": "none",
        }

    @staticmethod
    def _partition_absolute_episode_pack(
        files: list[dict],
        target_set: set[int],
        season: int,
    ) -> dict:
        """把 S01E116 这类 Emby 绝对集数映射到多季整包中的第 116 个媒体文件。"""
        if _safe_int(season, 0) != 1 or not target_set:
            return {"wanted": [], "unwanted": [], "wanted_names": [], "mode": "absolute"}

        media_records: list[tuple[int, int, int, str]] = []
        for idx, item in enumerate(files or []):
            name = str(item.get("name") or "")
            if not _is_video_file(name):
                continue
            refs = _extract_file_episode_refs(name)
            if len(refs) != 1:
                continue
            file_season, file_episode = next(iter(refs))
            media_records.append((
                file_season,
                file_episode,
                _safe_int(item.get("index"), idx),
                name,
            ))

        explicit_seasons = {row[0] for row in media_records}
        if len(explicit_seasons) < 2:
            return {"wanted": [], "unwanted": [], "wanted_names": [], "mode": "absolute"}

        media_records.sort(key=lambda row: (row[0], row[1], row[3]))
        selected_indexes: set[int] = set()
        selected_refs: set[tuple[int, int]] = set()
        selected_names: list[str] = []
        for absolute_episode in sorted(target_set):
            position = absolute_episode - 1
            if position < 0 or position >= len(media_records):
                continue
            file_season, file_episode, file_index, name = media_records[position]
            selected_indexes.add(file_index)
            selected_refs.add((file_season, file_episode))
            selected_names.append(name)
        if not selected_indexes:
            return {"wanted": [], "unwanted": [], "wanted_names": [], "mode": "absolute"}

        wanted: list[int] = []
        unwanted: list[int] = []
        wanted_names: list[str] = []
        for idx, item in enumerate(files or []):
            name = str(item.get("name") or "")
            file_index = _safe_int(item.get("index"), idx)
            if not _is_media_or_sidecar(name):
                unwanted.append(file_index)
                continue
            refs = _extract_file_episode_refs(name)
            if file_index in selected_indexes or refs.intersection(selected_refs):
                wanted.append(file_index)
                wanted_names.append(name)
            else:
                unwanted.append(file_index)
        return {
            "wanted": wanted,
            "unwanted": unwanted,
            "wanted_names": wanted_names or selected_names,
            "mode": "absolute",
        }

    async def _get_site_config(self, site_name: str) -> dict | None:
        """从 MP 获取站点配置（包含 cookie/ua）"""
        if not self.is_configured:
            return None
        sites = await self._get("/api/v1/site/")
        if not sites:
            return None
        # 按站点名称或域名匹配
        name_lower = site_name.lower()
        for site in sites if isinstance(sites, list) else (sites.get("data") or sites.get("results") or []):
            site_name_match = (site.get("name") or "").lower()
            site_domain = (site.get("domain") or "").lower()
            if name_lower in (site_name_match, site_domain) or site_domain.startswith(name_lower):
                return site
        return None

    async def check_connectivity(self) -> dict:
        """检查 MP 连接，返回详细状态"""
        if not self.is_configured:
            return {"ok": False, "message": "未配置 MoviePilot 地址或 Token"}
        try:
            # 用轻量请求测试
            result = await self._get("/api/v1/search/title", {"keyword": "test"})
            if result is not None:
                return {"ok": True, "message": "连接成功"}
            return {"ok": False, "message": "连接失败：API 返回空或认证不通过"}
        except Exception as e:
            return {"ok": False, "message": f"连接异常: {e}"}

    async def close(self):
        await self._client.aclose()
