"""日志系统 - 文件持久化 + 内存环状缓冲区 + 日志框架集成

职责：
  1. 自定义 logging.Handler，写文件 + 保持内存环状缓冲区供 API 实时查看
  2. 兼容旧的 print() 拦截（降级为 logger.info）
  3. 日志格式：[HH:MM:SS] LEVEL [module.name] message
日志文件: /workspace/data/app.log（最多保留 3 × 5MB）
"""

import builtins
import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional

_LOG_FILE = "/workspace/data/app.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_LOG_BACKUP_COUNT = 2
_RING_MAXLEN = 500  # 内存缓冲区条数


class RingMemoryHandler(logging.Handler):
    """日志处理器：写 RotatingFile + 保留最近 N 条到内存"""

    def __init__(self, maxlen: int = _RING_MAXLEN):
        super().__init__()
        self._ring: deque[dict] = deque(maxlen=maxlen)
        # 文件 handler
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        self._file_handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8", delay=False,
        )
        self._file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        """写入文件 + 内存环"""
        # 格式化消息
        ts = datetime.now().strftime("%H:%M:%S")
        module_name = record.name if record.name != "root" else "app"
        msg_text = record.getMessage()
        entry = {
            "time": ts,
            "level": record.levelname,
            "module": module_name,
            "msg": msg_text,
            "full": f"[{ts}] {record.levelname:<7s} [{module_name}] {msg_text}",
        }
        self._ring.append(entry)
        # 写文件
        self._file_handler.emit(record)

    def get_logs(self, level: str = "", limit: int = 200) -> list[dict]:
        """获取内存日志（供 API 使用）"""
        logs = list(self._ring)
        if level and level.upper() != "ALL":
            logs = [r for r in logs if r["level"] == level.upper()]
        return logs[-limit:]

    def clear(self) -> None:
        self._ring.clear()


# ── 全局实例（模块加载时初始化） ──
_handler: Optional[RingMemoryHandler] = None
_original_print = builtins.print


def init_logging():
    """配置全局日志系统（应用启动时调用一次）"""
    global _handler
    if _handler is not None:
        return  # 防止重复初始化

    _handler = RingMemoryHandler()
    _handler.setLevel(logging.DEBUG)

    # 配置根 logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 移除已有 handler（避免重复）
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(_handler)

    # 第三方库的日志保持 INFO 以上
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    # print() 拦截 → logger.debug
    def _print_intercept(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        if msg.strip():
            logging.getLogger("app.print").debug(msg)
        _original_print(*args, **kwargs)

    builtins.print = _print_intercept


def get_log_buffer() -> RingMemoryHandler:
    """获取全局日志缓冲区（供 API 路由使用）"""
    global _handler
    if _handler is None:
        init_logging()
    return _handler
