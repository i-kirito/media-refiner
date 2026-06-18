"""Media Refiner - 媒体洗版工坊"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

# 必须在任何其他模块导入之前初始化日志系统
from app.log_buffer import init_logging
init_logging()

from app.database import init_db
from app.routers import scan, items, search, download, config as config_router, views, logs, subscribe, telegram, gaps

logger = logging.getLogger(__name__)

_tg_poll_task = None


async def _tg_callback_poller():
    """后台轮询 TG callback 查询，处理审核操作 + 文本指令"""
    from app.services.telegram import TelegramNotifier
    tg = TelegramNotifier()
    commands_set = False
    try:
        while True:
            try:
                if tg.is_configured:
                    if not commands_set:
                        await tg.set_bot_commands()
                        commands_set = True
                    await tg.check_pending_callbacks()
            except Exception as e:
                logger.warning(f"[TGPoll] callback 轮询异常: {e}")
            await asyncio.sleep(3)
    finally:
        await tg.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    await init_db()
    # 代理字段迁移（兼容旧版本 hdhive_proxy / tg_proxy）
    _migrate_proxy()
    # 启动定时扫描调度器
    from app.routers.scan import start_scheduler
    start_scheduler()
    # 启动订阅 cron 调度器
    from app.routers.subscribe import start_cron_scheduler
    start_cron_scheduler()
    # 启动 TG callback 轮询
    global _tg_poll_task
    _tg_poll_task = asyncio.create_task(_tg_callback_poller())
    yield
    # 清理
    from app.routers.subscribe import stop_cron_scheduler
    stop_cron_scheduler()
    if _tg_poll_task:
        _tg_poll_task.cancel()


def _migrate_proxy():
    """从旧的 hdhive_proxy/tg_proxy 迁移到统一 proxy 字段"""
    import os
    env_path = "/workspace/config/.env"
    if not os.path.exists(env_path):
        return
    from app.config import settings
    if settings.proxy:
        return  # 已有新值，无需迁移
    old_proxy = os.environ.get("REFINER_HDHIVE_PROXY", "") or os.environ.get("REFINER_TG_PROXY", "")
    if old_proxy:
        settings.proxy = old_proxy
        # 写入 .env
        try:
            lines = []
            found = False
            with open(env_path, "r") as f:
                for line in f:
                    if line.startswith("REFINER_PROXY="):
                        found = True
                        break
                    if not line.startswith("REFINER_HDHIVE_PROXY=") and not line.startswith("REFINER_TG_PROXY="):
                        lines.append(line)
            if not found:
                lines.append(f"REFINER_PROXY={old_proxy}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            print(f"[Migrate] 代理已从旧字段迁移: {old_proxy}")
        except Exception as e:
            print(f"[Migrate] 代理迁移失败: {e}")


app = FastAPI(
    title="Media Refiner - 媒体洗版工坊",
    description="通过 Emby API 盘点媒体质量，使用 MoviePilot/影巢/115 进行自动洗版",
    version="1.1.0",
    lifespan=lifespan,
)

# 静态文件
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 注册路由
app.include_router(scan.router)
app.include_router(items.router)
app.include_router(search.router)
app.include_router(download.router)
app.include_router(config_router.router)
app.include_router(logs.router)
app.include_router(subscribe.router)
app.include_router(telegram.router)
app.include_router(gaps.router)
app.include_router(views.router)
