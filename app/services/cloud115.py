"""115 网盘 API 服务 - 复用 emby-pulse cloud115 插件逻辑"""

from __future__ import annotations
import httpx
import re
from typing import Optional
from app.config import settings


class Cloud115Client:
    """115 网盘 API 客户端"""

    def __init__(self, cookie: str = ""):
        self.cookie = cookie or settings.cloud115_cookie
        self._client = httpx.AsyncClient(timeout=30.0, verify=False)

    async def _get(self, url: str) -> dict | None:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        try:
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Cloud115] GET failed: {e}")
            return None

    async def _post(self, url: str, data: dict = None) -> dict | None:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        }
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

    async def transfer_from_share(self, share_code: str, receive_code: str = "",
                                   folder_id: str = "0") -> dict | None:
        """
        从分享链接转存
        POST /share/receive → share_code + receive_code + cid
        注意: 不需要 snap 步骤，直接传即可
        """
        transfer_data = {
            "share_code": share_code,
            "cid": folder_id,
        }
        if receive_code:
            transfer_data["receive_code"] = receive_code

        result = await self._post("https://webapi.115.com/share/receive", transfer_data)
        return result

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
