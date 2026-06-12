"""下载/转存 API - MP 下载 + 影巢转存 115"""

from fastapi import APIRouter, HTTPException, Query
import json

from app.services.moviepilot import MoviePilotClient
from app.services.hdhive import HDHiveClient
from app.services.cloud115 import Cloud115Client
from app.database import (
    add_subscribe_log,
    get_upgrade_plan,
    list_upgrade_plans,
    update_upgrade_plan_status,
)

router = APIRouter(prefix="/api/download", tags=["下载/转存"])


def _plan_current_quality(plan: dict | None) -> dict:
    """解析 upgrade_plans.current_quality_json。"""
    if not plan:
        return {}
    raw = plan.get("current_quality_json") or "{}"
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        return {}


def _plan_item_name(plan: dict | None, fallback: str = "") -> str:
    current = _plan_current_quality(plan)
    return current.get("name") or fallback or (plan or {}).get("emby_item_id") or "未知条目"


def _plan_item_id(plan: dict | None) -> str:
    return (plan or {}).get("emby_item_id") or ""


async def _notify_download_result(title: str, detail: str):
    """发送下载/转存结果通知。动态内容用纯文本，避免 Markdown 特殊字符导致发送失败。"""
    from app.services.telegram import TelegramNotifier

    tg = TelegramNotifier()
    try:
        if not tg._is_event_enabled("download"):
            return
        await tg.send_message(f"⬇️ 下载/转存结果\n\n{title}\n{detail}", parse_mode="")
    finally:
        await tg.close()


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
    plan = await get_upgrade_plan(plan_id) if plan_id else None
    item_id = _plan_item_id(plan)
    item_name = _plan_item_name(plan, title)

    if plan_id:
        if plan:
            await update_upgrade_plan_status(plan_id, "downloading", f"已提交下载: {title or item_name}")

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
            progress = f"MoviePilot 下载已提交: {title or item_name}"
            if plan_id:
                await update_upgrade_plan_status(plan_id, "done", progress)
            await add_subscribe_log(
                "",
                "手动洗版",
                "download",
                item_name,
                item_id,
                progress,
            )
            await _notify_download_result("✅ MoviePilot 下载已提交", f"条目: {item_name}\n资源: {title or '未知'}")
            return {"status": "success", "data": result}
        msg = result.get("message", "下载提交失败") if result else "API 无响应"
        raise HTTPException(500, msg)
    except HTTPException as e:
        if plan_id:
            await update_upgrade_plan_status(plan_id, "failed", str(e.detail))
        await add_subscribe_log("", "手动洗版", "error", item_name, item_id, f"MP 下载失败: {e.detail}")
        await _notify_download_result("❌ MoviePilot 下载失败", f"条目: {item_name}\n原因: {e.detail}")
        raise
    except Exception as e:
        if plan_id:
            await update_upgrade_plan_status(plan_id, "failed", str(e))
        await add_subscribe_log("", "手动洗版", "error", item_name, item_id, f"MP 下载异常: {e}")
        await _notify_download_result("❌ MoviePilot 下载异常", f"条目: {item_name}\n原因: {e}")
        raise
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

    plan = await get_upgrade_plan(plan_id) if plan_id else None
    item_id = _plan_item_id(plan)
    item_name = _plan_item_name(plan)

    if plan_id:
        if plan:
            await update_upgrade_plan_status(plan_id, "transferring", "已提交转存到 115")

    hd = HDHiveClient()
    try:
        result = await hd.unlock_and_transfer(resource_id, folder_id)
        if result:
            status = result.get("status", "transferred")
            if status == "error":
                raise HTTPException(500, result.get("message", "转存失败"))
            if status == "already_owned":
                progress = result.get("message", "资源已在 115 中")
                if plan_id:
                    await update_upgrade_plan_status(plan_id, "done", progress)
                await add_subscribe_log("", "手动洗版", "transfer", item_name, item_id, f"影巢转存: {progress}")
                await _notify_download_result("ℹ️ 影巢资源已在 115 中", f"条目: {item_name}\n{progress}")
                return {"status": "already_owned", "message": result.get("message", "资源已在 115 中"), "data": result}
            if status == "not_115":
                raise HTTPException(400, result.get("message", "非 115 网盘资源，无法转存"))
            progress = result.get("message") or "影巢资源已转存到 115"
            if plan_id:
                await update_upgrade_plan_status(plan_id, "done", progress)
            await add_subscribe_log("", "手动洗版", "transfer", item_name, item_id, f"影巢转存: {progress}")
            await _notify_download_result("✅ 影巢转存已提交", f"条目: {item_name}\n{progress}")
            return {"status": "success", "data": result}
        raise HTTPException(500, "转存失败")
    except HTTPException as e:
        if plan_id:
            await update_upgrade_plan_status(plan_id, "failed", str(e.detail))
        await add_subscribe_log("", "手动洗版", "error", item_name, item_id, f"影巢转存失败: {e.detail}")
        await _notify_download_result("❌ 影巢转存失败", f"条目: {item_name}\n原因: {e.detail}")
        raise
    except Exception as e:
        if plan_id:
            await update_upgrade_plan_status(plan_id, "failed", str(e))
        await add_subscribe_log("", "手动洗版", "error", item_name, item_id, f"影巢转存异常: {e}")
        await _notify_download_result("❌ 影巢转存异常", f"条目: {item_name}\n原因: {e}")
        raise
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
