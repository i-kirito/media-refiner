"""Web 面板页面路由 - 使用原生 Jinja2 避免 Starlette 兼容问题"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

router = APIRouter(include_in_schema=False)

_templates_dir = str(Path(__file__).parent.parent / "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    auto_reload=False,
    cache_size=0,  # 禁用缓存避免兼容问题
)


def _render(name: str, **context) -> str:
    """渲染模板"""
    template = _jinja_env.get_template(name)
    return template.render(**context)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """仪表盘首页"""
    return HTMLResponse(_render("dashboard.html", active_page="dashboard"))


@router.get("/items", response_class=HTMLResponse)
async def items_page(request: Request):
    """低质量条目列表"""
    return HTMLResponse(_render("items.html", active_page="items"))


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request):
    """洗版计划列表"""
    return HTMLResponse(_render("plans.html", active_page="plans"))


@router.get("/gaps", response_class=HTMLResponse)
async def gaps_page(request: Request):
    """缺集管理"""
    return HTMLResponse(_render("gaps.html", active_page="gaps"))


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """配置页面"""
    from app.config import settings
    from app.services.emby import EmbyClient

    # 获取所有 Movie 类型媒体库
    libraries = []
    excluded_set = set(x.strip() for x in settings.exclude_library_ids.split(",") if x.strip())
    try:
        ec = EmbyClient()
        raw_libs = await ec.get_libraries()
        for lib in raw_libs:
            if lib.get("CollectionType") == "movies":
                lib_id = lib.get("ItemId", "")
                lib_name = lib.get("Name", "")
                libraries.append({
                    "id": lib_id,
                    "name": lib_name,
                    "excluded": lib_id in excluded_set,
                })
        await ec.close()
    except Exception:
        pass

    ctx = {
        "active_page": "config",
        "config": {
            "emby_host": settings.emby_host,
            "emby_api_key": "已设置" if settings.emby_api_key else "",
            "moviepilot_url": settings.moviepilot_url,
            "moviepilot_token": "已设置" if settings.moviepilot_token else "",
            "hdhive_api_key": "已设置" if settings.hdhive_api_key else "",
            "hdhive_mode": settings.hdhive_mode,
            "hdhive_cms_authx_url": settings.hdhive_cms_authx_url,
            "hdhive_cms_token_path": settings.hdhive_cms_token_path,
            "tmdb_api_key": "已设置" if settings.tmdb_api_key else "",
            "symedia_url": settings.symedia_url,
            "symedia_token": "已设置" if settings.symedia_token else "",
            "symedia_cookie": "已设置" if settings.symedia_cookie else "",
            "symedia_cloud_type": settings.symedia_cloud_type,
            "symedia_parent_id": settings.symedia_parent_id,
            "cloud115_cookie": "已设置" if settings.cloud115_cookie else "",
            "cloud115_folder_id": settings.cloud115_folder_id,
            "exclude_library_ids": settings.exclude_library_ids,
            "scan_schedule": settings.scan_schedule,
            "proxy": settings.proxy,
            "tg_bot_token": "已设置" if settings.tg_bot_token else "",
            "tg_chat_id": settings.tg_chat_id,
            "tg_notify_events": settings.tg_notify_events,
        },
        "libraries": libraries,
    }
    return HTMLResponse(_render("config.html", **ctx))


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """历史记录"""
    return HTMLResponse(_render("history.html", active_page="history"))
