"""MoviePilot API 服务 - 搜索 + 下载"""

from __future__ import annotations
import httpx
from typing import Optional
from app.config import settings


class MoviePilotClient:
    """MoviePilot API 客户端"""

    def __init__(self, url: str = "", token: str = ""):
        self.url = (url or settings.moviepilot_url).rstrip("/")
        self.token = token or settings.moviepilot_token
        self._client = httpx.AsyncClient(timeout=60.0, verify=False)

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
            return resp.json()
        except httpx.HTTPStatusError as e:
            print(f"[MoviePilot] GET {path} HTTP {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            print(f"[MoviePilot] GET {path} failed: {e}")
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
            return resp.json() if resp.content else {"status": "ok"}
        except httpx.HTTPStatusError as e:
            msg = e.response.text[:200]
            print(f"[MoviePilot] POST {path} HTTP {e.response.status_code}: {msg}")
            return {"success": False, "message": f"HTTP {e.response.status_code}: {msg}"}
        except Exception as e:
            print(f"[MoviePilot] POST {path} failed: {e}")
            return {"success": False, "message": str(e)}

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
