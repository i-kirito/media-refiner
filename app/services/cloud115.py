"""115 网盘 API 服务 - 复用 emby-pulse cloud115 插件逻辑"""

from __future__ import annotations
import httpx
import re
from urllib.parse import quote
from app.config import settings


class Cloud115Client:
    """115 网盘 API 客户端"""

    def __init__(self, cookie: str = ""):
        self.cookie = cookie or settings.cloud115_cookie
        client_kwargs = {"timeout": 30.0, "verify": False}
        if settings.proxy:
            client_kwargs["proxy"] = settings.proxy
            print(f"[Cloud115] 使用代理: {settings.proxy}")
        self._client = httpx.AsyncClient(**client_kwargs)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if extra:
            headers.update(extra)
        return headers

    def _share_headers(self, share_code: str, receive_code: str = "") -> dict:
        referer = f"https://115cdn.com/s/{share_code}"
        if receive_code:
            referer = f"{referer}?password={receive_code}"
        return self._headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "Origin": "https://115cdn.com",
            "X-Requested-With": "XMLHttpRequest",
        })

    async def _get(self, url: str) -> dict | None:
        headers = self._headers()
        try:
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Cloud115] GET failed: {e}")
            return None

    async def _post(self, url: str, data: dict = None) -> dict | None:
        headers = self._headers({
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            resp = await self._client.post(url, data=data, headers=headers)
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "ok"}
        except Exception as e:
            print(f"[Cloud115] POST failed: {e}")
            return None

    async def get_folders(self) -> list[dict]:
        """
        获取 115 文件夹列表
        复用 emby-pulse cloud115 的 /115/folders 逻辑
        """
        result = await self._get("https://webapi.115.com/files?aid=1&cid=0&o=user_ptname&asc=1&offset=0&limit=100&show_dir=1")
        if isinstance(result, dict):
            return result.get("data", result.get("dir", []))
        return []

    async def create_offline_task(self, url: str, folder_id: str = "0") -> dict | None:
        """
        创建离线下载任务
        """
        data = {
            "url": url,
            "folder_id": folder_id,
        }
        return await self._post("https://webapi.115.com/files/add", data)

    async def create_folder(self, name: str, parent_id: str = "0") -> dict | None:
        """创建 115 文件夹。"""
        data = {
            "pid": parent_id,
            "cname": name,
        }
        return await self._post("https://webapi.115.com/files/add", data)

    async def transfer_from_share(self, share_code: str, receive_code: str = "",
                                   folder_id: str = "0", file_id: str = "") -> dict | None:
        """
        从分享链接转存
        POST /share/receive → share_code + receive_code + cid
        """
        transfer_data = {
            "share_code": share_code,
            "cid": folder_id,
        }
        if receive_code:
            transfer_data["receive_code"] = receive_code
        if file_id:
            transfer_data["file_id"] = file_id

        result = await self._post("https://webapi.115.com/share/receive", transfer_data)
        return result

    async def get_share_snap(
        self,
        share_code: str,
        receive_code: str = "",
        cid: str = "0",
        offset: int = 0,
        limit: int = 100,
    ) -> dict | None:
        """读取 115 分享快照；用于普通转存被判重后按 file_id 重新接收。"""
        params = {
            "share_code": share_code,
            "receive_code": receive_code,
            "cid": cid or "0",
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 100), 100)),
            "asc": 1,
            "o": "file_name",
            "format": "json",
        }
        try:
            resp = await self._client.get(
                "https://webapi.115.com/share/snap",
                params=params,
                headers=self._share_headers(share_code, receive_code),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Cloud115] SHARE SNAP failed: {e}")
            return None

    async def list_share_files(
        self,
        share_code: str,
        receive_code: str = "",
        cid: str = "0",
        recursive: bool = False,
        max_depth: int = 3,
    ) -> dict:
        """分页列出分享目录下文件。"""
        items: list[dict] = []
        last_response: dict | None = None
        limit = 100
        for offset in range(0, 5000, limit):
            result = await self.get_share_snap(
                share_code,
                receive_code,
                cid=cid,
                offset=offset,
                limit=limit,
            )
            last_response = result
            if not isinstance(result, dict) or not result.get("state"):
                return {
                    "state": False,
                    "error": result.get("error") if isinstance(result, dict) else "分享快照读取失败",
                    "data": items,
                }
            data = result.get("data") or {}
            batch = data.get("list") or []
            if not isinstance(batch, list):
                batch = []
            for item in batch:
                if isinstance(item, dict):
                    next_item = dict(item)
                    next_item.setdefault("__share_parent_cid", str(cid or "0"))
                    items.append(next_item)
            count = int(data.get("count") or len(items))
            if len(batch) < limit or len(items) >= count:
                break

        if recursive and max_depth > 0:
            nested: list[dict] = []
            for item in items:
                is_folder = str(item.get("fc", "1")) == "0" or (item.get("cid") and not item.get("fid"))
                child_cid = str(item.get("cid") or "").strip()
                if not is_folder or not child_cid:
                    continue
                child_result = await self.list_share_files(
                    share_code,
                    receive_code,
                    cid=child_cid,
                    recursive=True,
                    max_depth=max_depth - 1,
                )
                if child_result.get("state"):
                    nested.extend(child_result.get("data") or [])
            items.extend(nested)
        return {"state": True, "data": items, "raw": last_response}

    async def copy_files(self, file_ids: list[str], folder_id: str = "0") -> dict | None:
        """复制已有 115 文件/目录到目标目录。目录复制同样使用 fid[n] 参数。"""
        data = {"pid": folder_id}
        for idx, file_id in enumerate(file_ids):
            if file_id:
                data[f"fid[{idx}]"] = str(file_id)
        if len(data) <= 1:
            return {"state": False, "error": "缺少要复制的文件"}
        return await self._post("https://webapi.115.com/files/copy", data)

    async def move_files(self, file_ids: list[str], folder_id: str = "0") -> dict | None:
        """移动已有 115 文件/目录到目标目录。"""
        data = {"pid": folder_id}
        for idx, file_id in enumerate(file_ids):
            if file_id:
                data[f"fid[{idx}]"] = str(file_id)
        if len(data) <= 1:
            return {"state": False, "error": "缺少要移动的文件"}
        return await self._post("https://webapi.115.com/files/move", data)

    async def search_files(self, keyword: str, limit: int = 30) -> dict | None:
        """搜索账号内已有文件，用于 115 已转存但目标目录丢失时兜底复制。"""
        if not keyword:
            return None
        encoded_keyword = quote(str(keyword), safe="")
        return await self._get(
            "https://webapi.115.com/files/search?"
            f"aid=1&cid=0&type=0&offset=0&limit={max(1, min(limit, 115))}"
            f"&format=json&show_dir=1&search_value={encoded_keyword}"
        )

    async def list_files(self, folder_id: str, limit: int = 115) -> dict | None:
        """列出目录内容。"""
        return await self._get(
            "https://webapi.115.com/files?"
            f"aid=1&cid={folder_id}&o=file_name&asc=1&offset=0&limit={max(1, min(limit, 115))}"
            "&show_dir=1&format=json"
        )

    @staticmethod
    def extract_share_code(text: str) -> dict | None:
        """提取 115 分享码和提取码"""
        patterns = [
            r"115\.com[/]*(?:[^\s/]+/)?[^\s?]*\?.*[&?]share_code=([a-zA-Z0-9]+)",
            r"115\.com[/]*(?:[^\s/]+/)?[^\s?]*\?share_code=([a-zA-Z0-9]+)",
            r"115\.com[/]*(?:[^\s/]+/)?sharing/([a-zA-Z0-9]+)",
            r"115\.com/s/([a-zA-Z0-9]+)",          # 115 短链接格式
            r"115cdn\.com/s/([a-zA-Z0-9]+)",       # 115cdn 短链接格式
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                share_code = m.group(1)
                # 提取提取码
                pw = re.search(r"[?&]password=([^&#\s]+)", text)
                return {
                    "share_code": share_code,
                    "receive_code": pw.group(1) if pw else "",
                }
        return None

    async def check_connectivity(self) -> bool:
        """检查连接"""
        result = await self._get("https://my.115.com/?ct=ajax&ac=nav")
        return result is not None

    async def close(self):
        await self._client.aclose()
