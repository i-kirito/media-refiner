"""CloudDrive2 API 客户端。"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRPC_WEB_EMPTY_FRAME = b"\x00\x00\x00\x00\x00"


def _decode_grpc_web_trailers(content: bytes) -> dict[str, str]:
    """解析 gRPC-Web trailers frame，兼容 CloudDrive2 返回方式。"""
    trailers: dict[str, str] = {}
    index = 0
    while index + 5 <= len(content):
        flag = content[index]
        length = int.from_bytes(content[index + 1:index + 5], "big")
        index += 5
        frame = content[index:index + length]
        index += length
        if flag & 0x80:
            text = frame.decode("utf-8", errors="ignore")
            for line in text.replace("\r\n", "\n").split("\n"):
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                trailers[key.strip().lower()] = value.strip()
    return trailers


def _grpc_status(resp: httpx.Response) -> str:
    value = resp.headers.get("grpc-status")
    if value is not None:
        return value.strip()
    trailers = _decode_grpc_web_trailers(resp.content or b"")
    return trailers.get("grpc-status", "").strip()


def _grpc_message(resp: httpx.Response) -> str:
    value = resp.headers.get("grpc-message")
    if value:
        return value.strip()
    trailers = _decode_grpc_web_trailers(resp.content or b"")
    return trailers.get("grpc-message", "").strip()


class CloudDriveClient:
    """只封装 Media Refiner 需要的 CD2 目录缓存失效能力。"""

    def __init__(self, url: str = "", token: str = ""):
        self.url = (url or settings.clouddrive_url or "").strip().rstrip("/")
        self.token = (token or settings.clouddrive_token or "").strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0), verify=False)

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.token)

    async def force_expire_dir_cache(self) -> dict:
        """让 CloudDrive2 失效目录缓存，下次访问重新读取 115 挂载目录。"""
        if not self.is_configured:
            return {"ok": False, "skipped": True, "message": "CloudDrive2 未配置"}

        endpoint = f"{self.url}/clouddrive.CloudDriveFileSrv/ForceExpireDirCache"
        try:
            resp = await self._client.post(
                endpoint,
                content=GRPC_WEB_EMPTY_FRAME,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/grpc-web+proto",
                    "x-grpc-web": "1",
                },
            )
        except httpx.TransportError as e:
            return {"ok": False, "message": f"CloudDrive2 连接失败: {type(e).__name__}: {e}"}

        status = _grpc_status(resp)
        if resp.is_error:
            return {"ok": False, "message": f"CloudDrive2 HTTP {resp.status_code}"}
        if status == "0":
            return {"ok": True, "message": "CloudDrive2 目录缓存已刷新"}
        message = _grpc_message(resp) or f"grpc-status={status or '未知'}"
        return {"ok": False, "message": f"CloudDrive2 刷新失败: {message}"}

    async def check_connectivity(self) -> dict:
        return await self.force_expire_dir_cache()

    async def close(self):
        await self._client.aclose()
