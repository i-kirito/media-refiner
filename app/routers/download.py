"""下载/转存 API - MP 下载 + 影巢转存 115"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import uuid
from datetime import datetime

from app.services.moviepilot import MoviePilotClient
from app.services.hdhive import HDHiveClient
from app.services.cloud115 import Cloud115Client
from app.database import save_upgrade_plan, list_upgrade_plans, get_upgrade_plan

router = APIRouter(prefix="/api/download", tags=["下载/转存"])


@router.post("/moviepilot")
async def download_via_moviepilot(
    plan_id: str = Query(default=""),
    torrent_url: str = Query(...),
    title: str = Query(""),
    tmdbid: int = Query(default=0),
    site_name: str = Query(default=""),
    description: str = Query(default=""),
    page_url: str = Query(default=""),
    size: float = Query(default=0),
    seeders: int = Query(default=0),
):
    """通过 MoviePilot 添加下载任务（plan_id 可选，无计划时仅下载不追踪）"""
    if plan_id:
        plan = await get_upgrade_plan(plan_id)
        if plan:
            plan["status"] = "downloading"
            plan["progress"] = f"已提交下载: {title}"
            await save_upgrade_plan(plan)

    # 构建 torrent_info 用于 MP 媒体识别
    torrent_info = {
        "title": title,
        "site_name": site_name,
        "description": description,
        "page_url": page_url,
        "size": size,
        "seeders": seeders,
    }

    mp = MoviePilotClient()
    try:
        result = await mp.download(torrent_url, torrent_info=torrent_info, tmdbid=tmdbid)
        if result and result.get("success"):
            return {"status": "success", "data": result}
        msg = result.get("message", "下载提交失败") if result else "API 无响应"
        raise HTTPException(500, msg)
    finally:
        await mp.close()


@router.post("/hdhive-transfer")
async def transfer_via_hdhive(
    plan_id: str = Query(default=""),
    resource_id: str = Query(...),
    folder_id: str = Query(default=""),
):
    """通过影巢解锁并转存到 115（plan_id 可选，无计划时仅转存不追踪）"""
    # 使用配置中的默认目录 ID
    from app.config import settings
    if not folder_id:
        folder_id = settings.cloud115_folder_id or "0"

    if plan_id:
        plan = await get_upgrade_plan(plan_id)
        if plan:
            plan["status"] = "transferring"
            plan["progress"] = "已提交转存到 115"
            await save_upgrade_plan(plan)

    hd = HDHiveClient()
    try:
        result = await hd.unlock_and_transfer(resource_id, folder_id)
        if result:
            status = result.get("status", "transferred")
            if status == "error":
                raise HTTPException(500, result.get("message", "转存失败"))
            if status == "already_owned":
                return {"status": "already_owned", "message": result.get("message", "资源已在 115 中"), "data": result}
            return {"status": "success", "data": result}
        raise HTTPException(500, "转存失败")
    finally:
        await hd.close()


@router.post("/cloud115-offline")
async def create_offline_task(
    url: str = Query(...),
    folder_id: str = Query(default="0"),
):
    """创建 115 离线下载任务"""
    c115 = Cloud115Client()
    try:
        result = await c115.create_offline_task(url, folder_id)
        return {"status": "success", "data": result}
    finally:
        await c115.close()


@router.post("/cloud115-transfer-share")
async def transfer_from_share(
    share_url: str = Query(...),
    folder_id: str = Query(default="0"),
):
    """从 115 分享链接转存"""
    parsed = Cloud115Client.extract_share_code(share_url)
    if not parsed:
        raise HTTPException(400, "无法解析 115 分享链接")

    c115 = Cloud115Client()
    try:
        result = await c115.transfer_from_share(
            parsed["share_code"], parsed["receive_code"], folder_id
        )
        return {"status": "success", "data": result}
    finally:
        await c115.close()


@router.get("/plans")
async def list_plans(status: str = ""):
    """列出所有洗版计划"""
    plans = await list_upgrade_plans(status)
    return {"status": "success", "data": plans}


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    """获取洗版计划详情"""
    plan = await get_upgrade_plan(plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    return {"status": "success", "data": plan}
