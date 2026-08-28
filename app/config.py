"""全局配置管理"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """应用配置，优先从环境变量读取"""

    # Emby 连接
    emby_host: str = ""
    emby_api_key: str = ""

    # MoviePilot 连接（用于搜索和下载）
    moviepilot_url: str = ""
    moviepilot_token: str = ""

    # 影巢 连接（用于搜索高清晰片源）
    hdhive_api_key: str = ""      # X-API-Key 认证
    hdhive_mode: str = "openapi"  # openapi / symedia / cms
    hdhive_cms_authx_url: str = "https://authx.771885.xyz"
    hdhive_cms_token_path: str = "/cms-config/hdhive-openapi.json"
    tmdb_api_key: str = ""        # CMS 纯关键词搜索时解析 TMDB ID
    symedia_url: str = ""
    symedia_token: str = ""
    symedia_cookie: str = ""
    symedia_cloud_type: str = "channel_115"
    symedia_parent_id: str = ""

    # 115 网盘（用于转存）
    cloud115_cookie: str = ""
    cloud115_folder_id: str = "0"  # 115 转存目标目录 ID，0=根目录

    # CloudDrive2（用于转存后刷新挂载目录缓存）
    clouddrive_url: str = ""
    clouddrive_token: str = ""

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
