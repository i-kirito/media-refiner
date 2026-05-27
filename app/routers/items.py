"""条目操作 API - 忽略/洗版/详情"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import uuid
from datetime import datetime

from app.services.emby import EmbyClient
from app.database import add_ignore_item, save_upgrade_plan, get_upgrade_plan, remove_item_from_cache
from app.models.schemas import QualityItem, UpgradePlan, UpgradeRequest

router = APIRouter(prefix="/api/items", tags=["条目操作"])


@router.post("/{item_id}/ignore")
async def ignore_item(item_id: str, name: str = Query("")):
    """忽略某条目（下次扫描不再显示）"""
    await add_ignore_item(item_id, name)
    return {"status": "success", "message": f"已忽略: {name}"}


@router.post("/{item_id}/upgrade")
async def create_upgrade_plan(item_id: str, request: UpgradeRequest = None):
    """为某条目创建洗版计划"""
    emby = EmbyClient()
    try:
        item = await emby.get_item(item_id)
        if not item:
            await remove_item_from_cache(item_id)
            raise HTTPException(404, "该条目在 Emby 中已不存在，已自动从列表移除")

        plan_id = f"UP-{uuid.uuid4().hex[:8].upper()}"
        plan = {
            "id": plan_id,
            "emby_item_id": item_id,
            "current_quality": {
                "emby_id": item_id,
                "name": item.get("Name", ""),
                "type": "Movie",
                "resolution": "",
                "video_codec": "",
                "video_range": "",
                "path": item.get("Path", ""),
            },
            "target_quality": request.target_quality if request else "2160p",
            "search_results": [],
            "selected_index": -1,
            "status": "pending",
            "progress": "等待搜索高质量来源",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 提取视频信息
        sources = item.get("MediaSources") or []
        if sources:
            ms = sources[0]
            streams = ms.get("MediaStreams") or []
            for s in streams:
                if s.get("Type") == "Video":
                    plan["current_quality"]["resolution"] = f"{s.get('Width',0)}x{s.get('Height',0)}"
                    plan["current_quality"]["video_codec"] = s.get("Codec", "")
                    plan["current_quality"]["video_range"] = s.get("VideoRange", "")

        await save_upgrade_plan(plan)
        return {"status": "success", "data": plan}
    finally:
        await emby.close()


@router.get("/{item_id}/detail")
async def get_item_detail(item_id: str):
    """获取 Emby 条目详情"""
    emby = EmbyClient()
    try:
        item = await emby.get_item(item_id)
        if not item:
            # 条目不存在 → 从缓存清理
            await remove_item_from_cache(item_id)
            raise HTTPException(404, "该条目在 Emby 中已不存在，已自动从列表移除")
        return {"status": "success", "data": item}
    finally:
        await emby.close()


@router.post("/{item_id}/refresh")
async def refresh_item(item_id: str):
    """刷新 Emby 媒体库（强制重新识别）"""
    emby = EmbyClient()
    try:
        ok = await emby.scan_library()
        return {"status": "success" if ok else "error",
                "message": "已触发媒体库刷新" if ok else "刷新失败"}
    finally:
        await emby.close()


@router.post("/verify-cache")
async def verify_cache():
    """批量验证缓存中的条目是否仍在 Emby 中，清理已删除的"""
    from app.database import load_quality_cache, remove_item_from_cache

    items, summary, _ = await load_quality_cache()
    if not items:
        return {"status": "success", "data": {"checked": 0, "removed": 0}}

    emby = EmbyClient()
    removed = 0
    try:
        for item in items[:]:
            eid = item.get("emby_id", "")
            if not eid:
                continue
            exists = await emby.get_item(eid)
            if not exists:
                await remove_item_from_cache(eid)
                removed += 1
    finally:
        await emby.close()

    return {"status": "success", "data": {"checked": len(items), "removed": removed}}
