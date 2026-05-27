"""Emby API 服务 - 复用与 emby-pulse 相同的接口模式"""

from __future__ import annotations
import httpx
from typing import Any, Optional
from app.config import settings


class EmbyClient:
    """Emby API 客户端"""

    def __init__(self, host: str = "", api_key: str = "", user_id: str = ""):
        self.host = (host or settings.emby_host).rstrip("/")
        self.api_key = api_key or settings.emby_api_key
        self._user_id = user_id
        self._client = httpx.AsyncClient(timeout=60.0, verify=False)

    async def _get(self, path: str, params: dict = None) -> dict | list | None:
        """GET 请求"""
        if params is None:
            params = {}
        params["api_key"] = self.api_key
        url = f"{self.host}{path}"
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Emby] GET {path} failed: {e}")
            return None

    async def _post(self, path: str, data: Any = None) -> dict | None:
        """POST 请求"""
        url = f"{self.host}{path}?api_key={self.api_key}"
        try:
            resp = await self._client.post(url, json=data)
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "ok"}
        except Exception as e:
            print(f"[Emby] POST {path} failed: {e}")
            return None

    async def _delete(self, path: str) -> bool:
        """DELETE 请求"""
        url = f"{self.host}{path}?api_key={self.api_key}"
        try:
            resp = await self._client.delete(url)
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[Emby] DELETE {path} failed: {e}")
            return False

    async def _ensure_user_id(self) -> str:
        """自动获取 Emby 第一个 UserId"""
        if self._user_id:
            return self._user_id
        users = await self._get("/emby/Users")
        if isinstance(users, list) and users:
            self._user_id = users[0].get("Id", "")
            return self._user_id
        return ""

    # ─── 公共 API ───

    async def get_system_info(self) -> dict | None:
        """获取系统信息 /System/Info/Public"""
        return await self._get("/System/Info/Public")

    async def get_libraries(self) -> list[dict]:
        """获取媒体库列表 /Library/VirtualFolders"""
        result = await self._get("/Library/VirtualFolders", {"api_key": self.api_key})
        return result if isinstance(result, list) else []

    async def get_items(
        self,
        include_types: str = "Movie",
        fields: str = "MediaSources,Path,MediaStreams,ProviderIds,DateCreated,Overview",
        recursive: bool = True,
        start_index: int = 0,
        limit: int = 200,
        sort_by: str = "SortName",
        sort_order: str = "Ascending",
        parent_id: str = "",
    ) -> dict:
        """获取媒体项 /emby/Items
        Args:
            parent_id: 媒体库 ItemId，传入后仅返回该库条目（Emby 不支持多库 ParentId 过滤）
        """
        params = {
            "Recursive": str(recursive).lower(),
            "IncludeItemTypes": include_types,
            "Fields": fields,
            "StartIndex": start_index,
            "Limit": limit,
            "SortBy": sort_by,
            "SortOrder": sort_order,
        }
        if parent_id:
            params["ParentId"] = parent_id
        result = await self._get("/emby/Items", params)
        return result if isinstance(result, dict) else {"Items": [], "TotalRecordCount": 0}

    async def get_item(self, item_id: str, fields: str = "ProviderIds") -> dict | None:
        """获取单个媒体项（需要 UserId，否则 Emby 返回 404）"""
        uid = await self._ensure_user_id()
        if not uid:
            return None
        params = {}
        if fields:
            params["Fields"] = fields
        return await self._get(f"/Users/{uid}/Items/{item_id}", params)

    async def get_playback_info(self, item_id: str) -> dict | None:
        """获取播放信息（含流信息）"""
        return await self._get(f"/emby/Items/{item_id}/PlaybackInfo")

    async def scan_library(self, library_id: str = "") -> bool:
        """触发媒体库扫描"""
        if library_id:
            result = await self._post(f"/Library/Refresh/{library_id}")
        else:
            result = await self._post("/Library/Refresh")
        return result is not None

    async def delete_item(self, item_id: str) -> bool:
        """删除媒体项"""
        return await self._delete(f"/emby/Items/{item_id}")

    async def get_item_by_tmdb(self, tmdb_id: str, media_type: str = "Movie") -> dict | None:
        """通过 TMDB ID 查找项目"""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": media_type,
            "Fields": "MediaSources,Path",
            "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
        }
        result = await self._get("/emby/Items", params)
        if isinstance(result, dict) and result.get("Items"):
            return result["Items"][0]
        return None

    async def get_users(self) -> list[dict]:
        """获取用户列表"""
        result = await self._get("/emby/Users")
        return result if isinstance(result, list) else []

    async def close(self):
        await self._client.aclose()
