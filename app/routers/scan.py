"""质量扫描 API"""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Body
from app.services.emby import EmbyClient
from app.services.quality_scanner import QualityScanner
from app.models.schemas import ScanRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scan", tags=["质量扫描"])

_scanner: QualityScanner | None = None
_scan_task: asyncio.Task | None = None
_schedule_task: asyncio.Task | None = None


def get_scanner() -> QualityScanner:
    global _scanner
    if _scanner is None:
        _scanner = QualityScanner(EmbyClient())
    return _scanner


async def _run_scan(excluded: list[str]) -> bool:
    """后台执行扫描，捕获异常防止 Task 崩溃"""
    try:
        scanner = get_scanner()
        await scanner.scan(excluded)
        return True
    except asyncio.CancelledError:
        logger.info("[Scan] 扫描任务被取消")
        scanner = get_scanner()
        scanner._is_scanning = False
        scanner._progress = 0
        return False
    except Exception as e:
        logger.exception(f"[Scan] 扫描异常: {e}")
        scanner = get_scanner()
        scanner._is_scanning = False
        scanner._progress = 0
        scanner._current_item = f"错误: {e}"
        return False


async def _run_scheduled_scan(excluded: list[str]):
    """定时质量扫描完成后，同步启动一次缺集扫描。"""
    ok = await _run_scan(excluded)
    if not ok:
        return
    try:
        from app.routers.gaps import start_gap_scan_if_idle

        started = start_gap_scan_if_idle("质量扫描定时完成，准备同步缺集扫描")
        if started:
            logger.info("[Scheduler] 质量扫描完成，已同步启动缺集扫描")
        else:
            logger.info("[Scheduler] 质量扫描完成，缺集扫描已在运行，跳过同步启动")
    except Exception as e:
        logger.exception("[Scheduler] 同步启动缺集扫描失败: %s", e)


async def _scheduler_loop():
    """定时扫描调度器"""
    from app.config import settings
    from app.database import get_quality_cache_age

    while True:
        try:
            schedule_hours = int(settings.scan_schedule or "0")
            if schedule_hours > 0:
                cache_info = await get_quality_cache_age()
                if cache_info and cache_info.get("created_at"):
                    from app.database import parse_cache_time
                    last_time = parse_cache_time(cache_info["created_at"])
                    if last_time:
                        elapsed = (datetime.now() - last_time).total_seconds() / 3600
                        should_scan = elapsed >= schedule_hours
                    else:
                        should_scan = True
                else:
                    should_scan = True  # 从未扫描过

                if should_scan:
                    scanner = get_scanner()
                    if not scanner.progress["is_scanning"]:
                        logger.info(f"[Scheduler] 触发定时扫描（间隔 {schedule_hours}h）")
                        from app.config import settings as cfg
                        excluded = [x.strip() for x in cfg.exclude_library_ids.split(",") if x.strip()]
                        asyncio.create_task(_run_scheduled_scan(excluded))
        except Exception as e:
            logger.error(f"[Scheduler] 调度异常: {e}")

        await asyncio.sleep(60)  # 每分钟检查一次


def start_scheduler():
    """启动定时扫描调度器"""
    global _schedule_task
    if _schedule_task is None or _schedule_task.done():
        _schedule_task = asyncio.create_task(_scheduler_loop())
        logger.info("[Scheduler] 定时扫描调度器已启动")


def stop_scheduler():
    """停止定时扫描调度器"""
    global _schedule_task
    if _schedule_task and not _schedule_task.done():
        _schedule_task.cancel()
        _schedule_task = None
        logger.info("[Scheduler] 定时扫描调度器已停止")


@router.get("/status")
async def scan_status():
    """获取扫描状态（含缓存年龄、定时计划）"""
    scanner = get_scanner()
    from app.database import get_quality_cache_age, load_quality_cache
    from app.config import settings
    cache_info = await get_quality_cache_age()
    progress = dict(scanner.progress)
    cache_summary = {}

    if not progress.get("is_scanning"):
        _, cache_summary, _ = await load_quality_cache()
        if cache_summary:
            total_count = int(cache_summary.get("total_count") or 0)
            progress.update(
                {
                    "progress": 100 if total_count else progress.get("progress", 0),
                    "current_item": "已加载缓存" if total_count else progress.get("current_item", ""),
                    "total_count": total_count,
                    "scanned_count": total_count,
                    "scan_time": cache_summary.get("scan_time", ""),
                }
            )

    # 计算下次扫描时间
    next_scan = None
    schedule_hours = int(settings.scan_schedule or "0")
    if schedule_hours > 0 and cache_info and cache_info.get("created_at"):
        from app.database import parse_cache_time
        last_time = parse_cache_time(cache_info["created_at"])
        if last_time:
            from datetime import timedelta
            next_time = last_time + timedelta(hours=schedule_hours)
            if next_time > datetime.now():
                next_scan = next_time.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "status": "success",
        "data": {
            **progress,
            "cache": cache_info,
            "summary": cache_summary,
            "schedule": {
                "enabled": schedule_hours > 0,
                "interval_hours": schedule_hours,
                "next_scan": next_scan,
            }
        }
    }


@router.post("/start")
async def start_scan(request: ScanRequest | None = Body(None)):
    """开始质量扫描（后台执行，立即返回）。支持 JSON body 或空请求。"""
    global _scan_task
    scanner = get_scanner()
    if scanner.progress["is_scanning"]:
        raise HTTPException(400, "已有扫描任务在进行中")

    # 合并配置中的排除库ID
    from app.config import settings
    configured_excluded = [x.strip() for x in settings.exclude_library_ids.split(",") if x.strip()]
    request_excluded = request.excluded_libraries if request else []
    excluded = list(set(configured_excluded + request_excluded))

    # 在后台执行扫描，不阻塞响应
    scanner._is_scanning = True
    scanner._progress = 0
    scanner._total = 0
    scanner._scanned = 0
    scanner._current_item = "扫描任务排队中..."
    _scan_task = asyncio.create_task(_run_scan(excluded))
    return {"status": "success", "data": {"is_scanning": True, "message": "扫描已启动"}}


@router.get("/items")
async def get_quality_items(
    min_score: int = 0,
    max_score: int = 60,
    library_id: str = "",
    resolution: str = "",
    video_codec: str = "",
    video_range: str = "",
    anomaly: str = "",
    search: str = "",
    sort_by: str = "quality_score",
    sort_order: str = "asc",
    page: int = 1,
    page_size: int = 50,
):
    """获取低质量条目列表"""
    scanner = get_scanner()
    result = await scanner.get_items(
        min_score=min_score,
        max_score=max_score,
        library_id=library_id,
        resolution=resolution,
        video_codec=video_codec,
        video_range=video_range,
        anomaly=anomaly,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )
    return {"status": "success", "data": result}
