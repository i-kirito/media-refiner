"""全局配置管理"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """应用配置，优先从环境变量读取"""

    # Emby 连接
    emby_host: str = "http://192.168.31.213:8096"
    emby_api_key: str = ""

    # MoviePilot 连接（用于搜索和下载）
    moviepilot_url: str = ""
    moviepilot_token: str = ""

    # 影巢 连接（用于搜索高清晰片源）
    hdhive_api_key: str = ""      # X-API-Key 认证

    # 115 网盘（用于转存）
    cloud115_cookie: str = ""
    cloud115_folder_id: str = "0"  # 115 转存目标目录 ID，0=根目录

    # 本服务
    host: str = "0.0.0.0"
    port: int = 10308
    secret_key: str = "media-refiner-secret-key-change-me"
    db_path: str = "/workspace/data/refiner.db"

    # 扫描默认排除的分类ID（半角逗号分隔）
    exclude_library_ids: str = ""

    # 定时扫描（小时数，0=不启用）
    scan_schedule: str = "0"

    # 全局代理（影巢 + Telegram 共用）
    proxy: str = ""

    # Telegram 通知
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_notify_events: str = ""

    model_config = {"env_prefix": "REFINER_", "env_file": "/workspace/config/.env", "extra": "ignore"}


settings = Settings()
