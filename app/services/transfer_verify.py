"""Helpers for confirming 115 transfers are visible in the target folder."""

from __future__ import annotations

import asyncio
import re

from app.config import settings
from app.services.cloud115 import Cloud115Client


def _match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _base_media_name(value: str) -> str:
    text = re.sub(r"\{[^{}]*\}", " ", str(value or ""))
    text = re.sub(r"[\(（]\s*(?:19|20)\d{2}\s*[\)）]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-_—")


def _candidate_names(result: dict, resp: dict | None) -> list[str]:
    names: list[str] = []
    sources = [result or {}]
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        sources.append(resp["data"])
    for source in sources:
        for key in ("title", "name", "file_name", "resource_title", "remark", "description", "slug", "_resource_url"):
            value = str(source.get(key) or "").strip()
            if value and not value.startswith(("http://", "https://")):
                names.append(value)
                base_name = _base_media_name(value)
                if base_name and base_name != value:
                    names.append(base_name)
    return list(dict.fromkeys(names))


def _names_match(target_names: list[str], candidate_names: list[str]) -> bool:
    targets = [_match_text(name) for name in target_names if _match_text(name)]
    candidates = [_match_text(name) for name in candidate_names if _match_text(name)]
    for target in targets:
        for candidate in candidates:
            if len(candidate) >= 4 and (candidate in target or target in candidate):
                return True
    return False


def _item_name(item: dict) -> str:
    return str(item.get("n") or item.get("name") or item.get("file_name") or "").strip()


def _item_timestamp(item: dict) -> float:
    values: list[float] = []
    for key in ("t", "te", "tp", "upt", "ptime", "utime"):
        try:
            value = float(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 1_000_000_000_000:
            value /= 1000
        if value > 0:
            values.append(value)
    return max(values, default=0.0)


def _account_search_terms(result: dict, candidate_names: list[str]) -> list[str]:
    terms: list[str] = []
    joined = " ".join(candidate_names)
    tmdb_match = re.search(r"tmdbid\s*[=_-]\s*(\d{2,10})", joined, re.I)
    if tmdb_match:
        terms.append(f"tmdbid={tmdb_match.group(1)}")
    for key in ("title", "name", "resource_title"):
        value = _base_media_name(str((result or {}).get(key) or ""))
        if len(_match_text(value)) >= 4 and value not in terms:
            terms.append(value)
    return terms[:3]


def _exact_tmdb_id(text: str) -> str:
    match = re.search(r"tmdbid\s*[=_-]\s*(\d{2,10})", str(text or ""), re.I)
    return match.group(1) if match else ""


async def _find_recent_account_item(
    c115: Cloud115Client,
    result: dict,
    candidate_names: list[str],
    not_before: float,
) -> dict | None:
    expected_tmdb = _exact_tmdb_id(" ".join(candidate_names))
    for term in _account_search_terms(result, candidate_names):
        search = await c115.search_files(term, limit=40)
        if not isinstance(search, dict) or not search.get("state"):
            continue
        for item in search.get("data") or []:
            if not isinstance(item, dict):
                continue
            name = _item_name(item)
            if not name or not _names_match([name], candidate_names):
                continue
            item_tmdb = _exact_tmdb_id(name)
            if expected_tmdb and item_tmdb and item_tmdb != expected_tmdb:
                continue
            timestamp = _item_timestamp(item)
            if not timestamp or timestamp < not_before:
                continue
            return {
                "name": name,
                "file_id": str(item.get("cid") or item.get("fid") or ""),
                "timestamp": timestamp,
                "search_term": term,
            }
    return None


async def verify_hdhive_transfer_visible(
    result: dict,
    resp: dict | None,
    *,
    attempts: int = 5,
    delay_seconds: float = 2,
    account_search_after: float | None = None,
) -> dict:
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
                        name = _item_name(item)
                        if name:
                            candidate_names.append(name)

        candidate_names = list(dict.fromkeys(candidate_names))
        if not candidate_names:
            return {"ok": False, "message": "缺少资源名称，无法确认 115 文件"}

        last_names: list[str] = []
        for attempt in range(max(1, int(attempts or 1))):
            listing = await c115.list_files(folder_id, limit=115)
            if isinstance(listing, dict) and listing.get("state"):
                last_names = [
                    _item_name(item)
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
            if attempt + 1 < max(1, int(attempts or 1)) and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        if account_search_after is not None:
            account_item = await _find_recent_account_item(
                c115,
                result or {},
                candidate_names,
                float(account_search_after or 0),
            )
            if account_item:
                return {
                    "ok": True,
                    "message": "115 已确认资源入库",
                    "folder_id": folder_id,
                    "matched_names": [account_item["name"]],
                    "account_item": account_item,
                    "mode": "account_search",
                }
        return {
            "ok": False,
            "pending": True,
            "message": "115 转存已提交，等待资源落地入库",
            "folder_id": folder_id,
            "candidate_names": candidate_names[:8],
            "sample_names": last_names[:8],
        }
    finally:
        await c115.close()
