"""日志查看 API"""

from fastapi import APIRouter, Query
from app.log_buffer import get_log_buffer

router = APIRouter(prefix="/api/logs", tags=["日志"])


@router.get("")
async def get_logs(
    level: str = Query("", description="过滤级别: DEBUG/INFO/WARNING/ERROR"),
    limit: int = Query(200, description="返回条数"),
):
    """获取内存中的日志"""
    buf = get_log_buffer()
    logs = buf.get_logs(level=level, limit=limit)
    return {"status": "success", "data": logs}


@router.post("/clear")
async def clear_logs():
    """清空日志缓冲区"""
    buf = get_log_buffer()
    buf.clear()
    return {"status": "success", "message": "日志已清空"}
