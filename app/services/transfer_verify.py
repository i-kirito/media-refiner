"""Helpers for confirming 115 transfers are visible in the target folder."""

from __future__ import annotations

import asyncio
import re

from app.config import settings
from app.services.cloud115 import Cloud115Client


def _match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _candidate_names(result: dict, resp: dict | None) -> list[str]:
    names: list[str] = []
    sources = [result or {}]
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        sources.append(resp["data"])
    for source in sources:
        for key in ("title", "name", "file_name", "resource_title", "slug", "_resource_url"):
            value = str(source.get(key) or "").strip()
            if value and not value.startswith(("http://", "https://")):
                names.append(value)
    return list(dict.fromkeys(names))


def _names_match(target_names: list[str], candidate_names: list[str]) -> bool:
    targets = [_match_text(name) for name in target_names if _match_text(name)]
    candidates = [_match_text(name) for name in candidate_names if _match_text(name)]
    for target in targets:
        for candidate in candidates:
            if len(candidate) >= 4 and (candidate in target or target in candidate):
                return True
    return False


async def verify_hdhive_transfer_visible(result: dict, resp: dict | None) -> dict:
    """Return ok only after the transferred resource is visible in the 115 target folder."""
    if not settings.cloud115_cookie:
        return {"ok": False, "message": "未配置 115 Cookie，无法确认转存文件已落地"}
    if not isinstance(resp, dict):
        return {"ok": False, "message": "缺少转存响应，无法确认 115 文件"}

    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    folder_id = str(data.get("_target_folder") or settings.symedia_parent_id or settings.cloud115_folder_id or "0")
    candidate_names = _candidate_names(result, resp)

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
                if _names_match(last_names, candidate_names):
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
