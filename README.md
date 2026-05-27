# Media Refiner · 媒体洗版工坊

自动化媒体质量升级系统：扫描 Emby 媒体库 → 发现低质量条目 → 搜索更高清版本 → 下载/转存升级。

## 功能

- **质量扫描** — 综合分辨率、编码、HDR、码率评分，支持文件名分辨率回退
- **多源搜索** — MoviePilot 种子搜索 + HDHive 影巢 115 资源搜索
- **双通道下载** — MoviePilot 下载 / 影巢解锁→115 转存
- **订阅洗版** — 规则驱动自动匹配、定时 cron 触发、Telegram 审核
- **Web 面板** — 仪表盘、条目列表、洗版计划、配置管理、操作历史
- **TG Bot** — 审核卡片推送、回调操作、远程指令控制

## 技术栈

| 层面 | 选型 |
|------|------|
| 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (aiosqlite, WAL) |
| 前端 | Jinja2 + Tailwind CSS + Chart.js |
| 通知 | Telegram Bot API |
| 部署 | Docker + docker-compose |

## 快速开始

### 1. 环境配置

```bash
cp config/.env.example config/.env
# 编辑 config/.env 填入实际值
```

### 2. 启动

```bash
# Docker 部署
docker compose up -d

# 或本地运行
pip install -r requirements.txt
python run.py
```

服务默认监听 `http://localhost:10308`，Web 面板访问 `/`。

## 配置说明

| 环境变量 | 必填 | 说明 |
|----------|------|------|
| `REFINER_EMBY_HOST` | ✅ | Emby 服务地址 |
| `REFINER_EMBY_API_KEY` | ✅ | Emby API Key |
| `REFINER_MOVIEPILOT_URL` | — | MoviePilot 地址 |
| `REFINER_MOVIEPILOT_TOKEN` | — | MoviePilot API Token |
| `REFINER_HDHIVE_API_KEY` | — | HDHive API Key |
| `REFINER_CLOUD115_COOKIE` | — | 115 网盘 Cookie |
| `REFINER_TG_BOT_TOKEN` | — | Telegram Bot Token |
| `REFINER_TG_CHAT_ID` | — | Telegram Chat ID |
| `REFINER_SCAN_SCHEDULE` | — | 定时扫描间隔（小时） |
| `REFINER_PROXY` | — | 全局代理地址 |

完整配置见 `config/.env.example`。

## 项目结构

```
media-refiner/
├── app/
│   ├── main.py            # FastAPI 入口
│   ├── config.py           # 全局配置
│   ├── database.py         # SQLite ORM
│   ├── services/           # 外部服务客户端
│   │   ├── emby.py         # Emby API
│   │   ├── quality_scanner.py  # 质量评分引擎
│   │   ├── moviepilot.py   # MoviePilot API
│   │   ├── hdhive.py       # HDHive API
│   │   ├── cloud115.py     # 115 网盘 API
│   │   └── telegram.py     # TG Bot
│   ├── routers/            # API 路由
│   ├── templates/          # Jinja2 页面
│   └── static/             # CSS/JS
├── config/
│   └── .env.example        # 配置模板
├── Dockerfile
├── docker-compose.yml
└── deploy.sh               # 部署脚本
```

## License

MIT
