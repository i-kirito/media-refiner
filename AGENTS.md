# AGENTS.md — Media Refiner 开发指引

## 项目概述

Media Refiner（媒体洗版工坊）是一套自动化媒体质量升级系统：
- **扫描** Emby 媒体库，对视频文件进行质量评分（分辨率/编码/HDR/码率）
- **发现** 低质量条目，通过 MoviePilot / 影巢(HDHive) 搜索更高清版本
- **执行** 下载（MoviePilot 种子）或转存（影巢 → 115 网盘）
- **订阅** 支持规则驱动的自动洗版，定时 cron 触发
- **通知** 通过 Telegram Bot 推送审核卡片并支持回调操作

## 技术栈

| 层面   | 选型                          |
|--------|-------------------------------|
| 框架   | FastAPI + Uvicorn             |
| 数据库 | SQLite (aiosqlite, WAL 模式)  |
| 前端   | Jinja2 模板 + 原生 JS/CSS     |
| HTTP   | httpx (AsyncClient)           |
| 配置   | pydantic-settings, .env 文件  |
| 部署   | Docker, docker-compose        |

## 项目结构

```
media-refiner/
├── AGENTS.md                  # 本文件
├── run.py                     # 入口：uvicorn.run("app.main:app")
├── requirements.txt           # Python 依赖
├── Dockerfile                 # 基于 python:3.11-slim
├── docker-compose.yml         # 端口 10308→10309，挂载 /data 和 /config
├── deploy.sh                  # 部署脚本：构建→传输NAS→compose up
├── backup_env.sh              # .env 备份
├── config/
│   ├── .env                   # 运行时环境变量（REFINER_ 前缀）
│   ├── .env.example
│   └── env_backups/           # 部署脚本自动备份
├── data/
│   └── refiner.db             # SQLite 数据库
└── app/
    ├── main.py                # FastAPI app, lifespan 管理
    ├── config.py              # pydantic-settings 配置类
    ├── database.py            # SQLite CRUD 操作
    ├── log_buffer.py          # 日志缓冲
    ├── models/
    │   └── schemas.py         # Pydantic 请求/响应模型
    ├── services/
    │   ├── emby.py            # Emby API 客户端
    │   ├── quality_scanner.py # 质量评分 + 扫描引擎
    │   ├── moviepilot.py      # MoviePilot API 搜索/下载
    │   ├── hdhive.py          # 影巢 API 搜索/解锁/转存
    │   ├── cloud115.py        # 115 网盘转存/离线下载
    │   └── telegram.py        # TG Bot: 通知推送 + callback 轮询
    ├── routers/
    │   ├── views.py           # Web 页面路由（Jinja2 渲染）
    │   ├── scan.py            # /api/scan 质量扫描 API + 定时调度
    │   ├── items.py           # /api/items 条目忽略/洗版/验证
    │   ├── search.py          # /api/search 搜索高清片源
    │   ├── download.py        # /api/download MP下载/影巢转存/115离线
    │   ├── config.py          # /api/config 配置CRUD + 连通性测试
    │   ├── subscribe.py       # /api/subscribe 订阅规则 + 自动洗版引擎
    │   ├── logs.py            # /api/logs 日志/活动记录
    │   └── telegram.py        # /api/telegram webhook 端点
    ├── templates/             # Jinja2 HTML 模板
    │   ├── base.html
    │   ├── dashboard.html     # 首页仪表盘
    │   ├── items.html         # 低质量条目列表
    │   ├── plans.html         # 洗版计划
    │   ├── config.html        # 系统配置
    │   └── history.html       # 操作历史/日志
    └── static/
        ├── style.css
        ├── dashboard.css
        └── dashboard.js
```

## 编码约定

### Python

- 使用 Python 3.11+，类型注解用 `|` 语法（如 `str | None`）
- 所有 async 函数标注 `async def`，HTTP 客户端用 `httpx.AsyncClient`
- 异常处理使用 `try/finally` 确保 `client.aclose()` 被调用
- 配置键统一打 `REFINER_` 前缀，pydantic-settings 自动映射
- 数据库操作统一通过 `app/database.py` 中的函数，不在 router 中直接写 SQL
- 日志使用 `logging.getLogger(__name__)`，初始化在 `app/log_buffer.py`

### 前端

- 原生 JS 无框架，通过 `<script>` 内联或 dashboard.js 引入
- AJAX 请求走 `/api/...`，模板渲染走 `/...` 路由
- CSS 文件在 `app/static/` 下，在 `base.html` 中通过 `link` 引入

### API 设计

- RESTful: `GET /api/resource` 查询, `POST /api/resource` 创建/操作
- 统一响应格式: `{"status": "success|error", "data": ..., "message": "..."}`
- 敏感字段在 GET 输出时做脱敏（如 `"••••" + key[-4:]`）

### Emby 交互模式

- EmbyClient 通过 `api_key` 认证，首次使用自动获取 `UserId`
- 获取媒体列表时递归拉取，通过 `ParentId` 限定库
- 获取单条目需要 `UserId`，否则 Emby 返回 404

### 质量扫描

- 评分 0-100，综合分辨率、编码、HDR、码率权重
- 支持文件名分辨率回退（Emby 解析异常时）
- 支持排除指定 Emby 媒体库（`exclude_library_ids`）
- 定时扫描通过 `scan_schedule` 配置，分钟级轮询检查

### 订阅洗版

- 规则存储在 `subscribe_rules` 表（`data_json` 存完整规则 JSON）
- 执行时遍历缓存中的低质量条目，匹配规则条件
- 自动搜索→生成审核卡片（DB + TG 双写），等待确认
- 支持 `auto_approve` 自动通过，cron 表达式定时触发

### Telegram 集成

- 通过 `check_pending_callbacks()` 轮询 callback query（3s 间隔）
- 支持指令: `/scan`, `/run`, `/rules`, `/reviews`, `/logs`, `/clear`, `/status`, `/reset`
- 审核卡片带 inline keyboard 按钮（✅通过 / ❌拒绝 / 🔕忽略）

### Docker 部署

- 工作目录 `/workspace`，数据库默认 `/workspace/data/refiner.db`
- 配置文件在 `/workspace/config/.env`
- `deploy.sh` 构建 linux/amd64 镜像，传输到 NAS，compose up
- 支持 `deploy.sh quick <file>` 热更新单个文件到运行容器

## 注意事项

- 不要 `git commit` 或创建分支，除非明确要求
- 修改代码时保持现有风格（命名、缩进、日志格式）
- DB_PATH 硬编码为基于 `settings.db_path` 的 `Path` 对象，不要随意改动
- HDHive / MoviePilot / Cloud115 的 API 地址和认证方式各有不同，修改时注意对照现有实现
- URL 参数带中文时注意编码（httpx 的 `params` 参数会自动处理）
- `.env` 中敏感字段不要直接打印到日志
