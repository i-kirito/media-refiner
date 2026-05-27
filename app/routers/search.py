"""搜索 API - MP + 影巢（并行 + 超时）"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from typing import Optional

from app.services.moviepilot import MoviePilotClient
from app.services.hdhive import HDHiveClient

router = APIRouter(prefix="/api/search", tags=["搜索"])


@router.get("/moviepilot")
async def search_moviepilot(keyword: str = Query(..., description="搜索关键词")):
    """通过 MoviePilot 搜索高质量资源"""
    mp = MoviePilotClient()
    try:
        results = await mp.search(keyword)
        return {"status": "success", "data": results, "source": "moviepilot"}
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "data": [], "source": "moviepilot"}
    except Exception as e:
        return {"status": "error", "message": f"搜索异常: {e}", "data": [], "source": "moviepilot"}
    finally:
        await mp.close()


@router.get("/hdhive")
async def search_hdhive(
    keyword: str = Query("", description="搜索关键词"),
    emby_id: str = Query("", description="Emby 条目 ID（可选，用于解析 TMDB ID）"),
    media_type: str = "movie",
):
    """通过影巢搜索高质量资源（推荐提供 emby_id 以获得 TMDB ID 精准搜索）"""
    hd = HDHiveClient()
    try:
        results = await hd.search(keyword=keyword, emby_item_id=emby_id, media_type=media_type)
        return {"status": "success", "data": results, "source": "hdhive"}
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "data": [], "source": "hdhive"}
    except Exception as e:
        return {"status": "error", "message": f"搜索异常: {e}", "data": [], "source": "hdhive"}
    finally:
        await hd.close()


@router.get("/all")
async def search_all(
    keyword: str = Query(..., description="搜索关键词"),
    emby_id: str = Query("", description="Emby 条目 ID（可选，影巢需要 TMDB ID）"),
):
    """并行搜索 MP 和 影巢（MP 60s / 影巢 15s 超时）"""

    async def _search_with_timeout(client_cls, search_fn, *args, **kwargs):
        client = client_cls()
        try:
            fn = getattr(client, search_fn)
            to = 60.0 if client_cls.__name__ == "MoviePilotClient" else 15.0
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=to)
            return result, None, None
        except asyncio.TimeoutError:
            to = 60.0 if client_cls.__name__ == "MoviePilotClient" else 15.0
            return [], None, f"请求超时（{int(to)}s）"
        except RuntimeError as e:
            return [], None, str(e)
        except Exception as e:
            return [], None, f"异常: {e}"
        finally:
            await client.close()

    mp_task = _search_with_timeout(MoviePilotClient, "search", keyword)
    hd_task = _search_with_timeout(HDHiveClient, "search", keyword=keyword, emby_item_id=emby_id, media_type="movie")

    mp_results, hd_results = await asyncio.gather(mp_task, hd_task)

    data = {
        "moviepilot": mp_results[0] or [],
        "hdhive": hd_results[0] or [],
    }

    errors = {}
    if mp_results[2]:
        errors["moviepilot"] = mp_results[2]
    if hd_results[2]:
        errors["hdhive"] = hd_results[2]
    if errors:
        data["errors"] = errors

    return {"status": "success", "data": data}
