"""Pydantic 数据模型"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime


# ─── 质量扫描 ───

class ScanRequest(BaseModel):
    """发起质量扫描"""
    strategy: str = Field(default="quality", description="扫描策略: quality / full")
    excluded_libraries: list[str] = Field(default_factory=list)
    custom_weights: Optional[dict[str, int]] = None


class ScanProgress(BaseModel):
    """扫描进度"""
    is_scanning: bool = False
    progress: int = 0           # 0-100
    current_item: str = ""
    total_count: int = 0
    scanned_count: int = 0


class QualityItem(BaseModel):
    """一条质量盘点条目"""
    emby_id: str = ""
    name: str = ""
    year: Optional[int] = None
    type: str = "Movie"          # Movie / Series / Episode
    resolution: str = ""         # 3840x2160, 1920x1080, ...
    video_codec: str = ""        # hevc, h264, av1, ...
    video_range: str = ""        # DolbyVision, HDR10, SDR
    bitrate: Optional[int] = None
    path: str = ""
    library_id: str = ""
    library_name: str = ""
    quality_score: int = 0       # 0-100 自定义质量评分
    size_bytes: Optional[int] = None


class QualitySummary(BaseModel):
    """质量汇总"""
    total_count: int = 0
    scan_time: Optional[str] = None
    resolution_dist: dict[str, int] = {}
    codec_dist: dict[str, int] = {}
    hdr_dist: dict[str, int] = {}


# ─── 搜索与洗版 ───

class SearchResult(BaseModel):
    """搜索结果（MP/影巢）"""
    source: str = ""             # moviepilot / hdhive
    title: str = ""
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    quality: str = ""            # 2160p / 1080p / ...
    size: Optional[int] = None
    torrent_url: str = ""
    seeders: Optional[int] = None
    leechers: Optional[int] = None
    score: int = 0               # 匹配度评分


class UpgradePlan(BaseModel):
    """洗版计划"""
    id: str = ""
    emby_item_id: str = ""
    current_quality: QualityItem
    target_quality: str = ""     # 目标质量标准
    search_results: list[SearchResult] = []
    selected_index: int = -1     # -1 表示未选择
    status: str = "pending"      # pending / downloading / transferring / replacing / done / failed
    progress: str = ""
    created_at: str = ""


class UpgradeRequest(BaseModel):
    """发起洗版"""
    item_id: str = ""
    search_source: str = "auto"  # auto / moviepilot / hdhive
    target_quality: str = "2160p"  # 4K / 1080p / ...


# ─── 下载/转存 ───

class DownloadTask(BaseModel):
    """下载/转存任务"""
    id: str = ""
    item_id: str = ""
    source: str = ""             # moviepilot / hdhive / cloud115
    torrent_url: str = ""
    status: str = "pending"      # pending / downloading / seeding / done / failed
    progress: float = 0.0
    message: str = ""
    created_at: str = ""


class ConfigModel(BaseModel):
    """配置文件模型"""
    emby_host: str = ""
    emby_api_key: str = ""
    moviepilot_url: str = ""
    moviepilot_token: str = ""
    hdhive_api_key: str = ""        # 影巢 API Key (X-API-Key)
    hdhive_mode: str = "openapi"    # openapi / symedia / cms
    hdhive_cms_authx_url: str = "https://authx.771885.xyz"
    hdhive_cms_token_path: str = "/cms-config/hdhive-openapi.json"
    tmdb_api_key: str = ""
    symedia_url: str = ""
    symedia_token: str = ""
    symedia_cookie: str = ""
    symedia_cloud_type: str = "channel_115"
    symedia_parent_id: str = ""
    cloud115_cookie: str = ""
    cloud115_folder_id: str = "0"   # 115 转存目录 ID
    clouddrive_url: str = ""         # CloudDrive2 地址
    clouddrive_token: str = ""       # CloudDrive2 API Token
    exclude_library_ids: str = ""
    scan_schedule: str = "0"  # 定时扫描间隔（小时），0=禁用
    # 全局代理（影巢 + Telegram 共用）
    proxy: str = ""
    # Telegram 通知
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_notify_events: str = ""  # 逗号分隔: review,scan,download


class SubscribeRule(BaseModel):
    """订阅洗版规则 - 自动匹配低画质媒体并推送下载"""
    id: str = ""
    name: str = "默认规则"
    enabled: bool = True
    # 触发条件
    max_score: int = 60
    min_current_resolution: str = ""   # "" = 不限, 如 "720p"
    # 目标要求
    target_resolution: str = "1080p"   # 升级目标
    target_codec: str = ""             # 首选编码，如 "hevc"
    target_hdr: str = ""               # 首选 HDR，如 "hdr"
    # 范围
    library_ids: list[str] = []
    # 来源
    source: str = "moviepilot"         # moviepilot / hdhive / both
    source_priority: str = "hdhive"    # 优先来源: hdhive / moviepilot（source=both 时生效）
    # 排序偏好（拖拽排序，排在前面的优先度高）
    prefer_order: list[str] = Field(default_factory=list)  # 可选值: "remux", "4k", "subtitle"，顺序即优先级
    # 自动审核：勾选的条件全部满足时跳过审核直接执行（推送到 MP / 转存 115）
    auto_approve: bool = False
    auto_approve_conditions: list[str] = Field(default_factory=list)  # 可选值: "remux", "4k", "subtitle"
    # 调度
    cron_expression: str = ""          # cron 表达式，如 "0 3 * * *"（每日3点），空=不自动执行
    interval_hours: int = 6
    batch_size: int = Field(default=20, ge=1, le=1000)  # 每次运行最多处理
    # 状态
    last_run: str = ""
    total_upgraded: int = 0
    created_at: str = ""
    updated_at: str = ""


class ReplaceResult(BaseModel):
    """替换结果"""
    success: bool = False
    message: str = ""
    old_path: str = ""
    new_path: str = ""
