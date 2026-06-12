"""影巢 HDHive API 服务 - 搜索 + 转存"""

from __future__ import annotations
import httpx
from app.config import settings


class HDHiveClient:
    """
    影巢资源中心 API 客户端
    API 基址: https://hdhive.com/api/open
    认证方式: X-API-Key header
    """

    BASE_URL = "https://hdhive.com/api/open"

    def __init__(self, api_key: str = "", proxy: str = ""):
        self.api_key = api_key or settings.hdhive_api_key
        self.mode = (settings.hdhive_mode or "openapi").strip().lower()
        self.symedia_url = (settings.symedia_url or "").rstrip("/")
        self.symedia_cloud_type = settings.symedia_cloud_type or "channel_115"
        self.proxy = proxy or settings.proxy
        timeout = 30.0 if self.mode == "symedia" else 15.0
        client_kwargs = {"timeout": timeout, "verify": False}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy
            print(f"[HDHive] 使用代理: {self.proxy}")
        self._client = httpx.AsyncClient(**client_kwargs)

    @property
    def is_configured(self) -> bool:
        """检查是否已配置"""
        if self.mode == "symedia":
            return bool(self.symedia_url and (settings.symedia_token or settings.symedia_cookie))
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    def _symedia_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Media-Refiner/1.1",
        }
        if settings.symedia_token:
            token = settings.symedia_token.strip()
            headers["Authorization"] = token if " " in token else f"Bearer {token}"
        if settings.symedia_cookie:
            headers["Cookie"] = settings.symedia_cookie.strip()
        return headers

    def _symedia_api_url(self, path: str) -> str:
        """Return a Symedia backend API URL, accepting either host root or /api/v1 base."""
        base = self.symedia_url
        if not base.endswith("/api/v1"):
            base = f"{base}/api/v1"
        return f"{base}{path if path.startswith('/') else '/' + path}"

    async def _get(self, path: str, params: dict = None) -> dict | list | None:
        """GET 请求"""
        if not self.is_configured:
            return None
        try:
            resp = await self._client.get(
                f"{self.BASE_URL}{path}",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            print(f"[HDHive] GET {path} HTTP {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            print(f"[HDHive] GET {path} failed: {e}")
            return None

    async def _post(self, path: str, data: dict = None) -> dict | None:
        """POST 请求"""
        if not self.is_configured:
            return None
        try:
            resp = await self._client.post(
                f"{self.BASE_URL}{path}",
                json=data,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "ok"}
        except httpx.HTTPStatusError as e:
            print(f"[HDHive] POST {path} HTTP {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            print(f"[HDHive] POST {path} failed: {e}")
            return None

    async def search(self, keyword: str = "", tmdb_id: int = 0,
                     emby_item_id: str = "", media_type: str = "movie") -> list[dict]:
        """
        搜索影巢 115 资源

        优先级: tmdb_id > emby_item_id（自动解析 TMDB） > keyword
        返回资源列表，每个资源字段：
            title, share_size, unlock_points, is_unlocked,
            video_resolution[], source[], remark, slug,
            is_official, validate_status, user{username,nickname}
        """
        if self.mode == "symedia":
            return await self._symedia_search(keyword, tmdb_id, emby_item_id, media_type)

        if not self.is_configured:
            raise RuntimeError("影巢未配置：请先在系统配置中设置 API Key")

        media_type = self._normalize_media_type(media_type)

        # 从 emby_id 获取 TMDB ID
        if not tmdb_id and emby_item_id:
            tmdb_id, resolved_type = await self._resolve_emby_info(emby_item_id)
            media_type = resolved_type or media_type

        if tmdb_id:
            resources = await self._get_resources(tmdb_id, media_type)
            print(f"[HDHive] 搜索 tmdb_id={tmdb_id} 返回 {len(resources)} 条结果")
            return resources

        # 没有 TMDB ID 时尝试 keyword（部分 endpoing 支持）
        if keyword:
            result = await self._post("/check/resource", {"keyword": keyword, "type": media_type})
            if isinstance(result, dict):
                items = result.get("data", result.get("results", []))
                print(f"[HDHive] 关键词搜索 {keyword!r} 返回 {len(items)} 条结果 (来源: POST /check/resource)")
                return items
            print(f"[HDHive] 关键词搜索 {keyword!r} 返回空 (result={result})")
            return result or []

        return []

    async def _resolve_tmdb_id(self, emby_item_id: str) -> int:
        """从 Emby 条目提取 TMDB ID"""
        tmdb_id, _ = await self._resolve_emby_info(emby_item_id)
        return tmdb_id

    async def _resolve_emby_info(self, emby_item_id: str) -> tuple[int, str]:
        """从 Emby 条目提取 TMDB ID，并推断 HDHive/Symedia 媒体类型。"""
        from app.services.emby import EmbyClient
        emby = EmbyClient()
        try:
            item = await emby.get_item(
                emby_item_id,
                fields="ProviderIds,Type,SeriesId,SeriesName,ParentId",
            )
            if item:
                item_type = item.get("Type", "")
                media_type = self._normalize_media_type(item_type)
                if item_type in ("Episode", "Season") and item.get("SeriesId"):
                    series = await emby.get_item(item.get("SeriesId"), fields="ProviderIds,Type")
                    if series:
                        item = series
                        media_type = "tv"
                pid = item.get("ProviderIds", {})
                tmdb_str = pid.get("Tmdb", "")
                if tmdb_str and tmdb_str.isdigit():
                    return int(tmdb_str), media_type
        except Exception as e:
            print(f"[HDHive] 解析 TMDB ID 失败: {e}")
        finally:
            await emby.close()
        return 0, "movie"

    @staticmethod
    def _normalize_media_type(media_type: str = "movie") -> str:
        value = (media_type or "movie").strip().lower()
        if value in ("series", "season", "episode", "tvshow", "tv"):
            return "tv"
        return "movie"

    async def _get_resources(self, tmdb_id: int, media_type: str = "movie") -> list[dict]:
        """
        通过 TMDB ID 获取资源列表
        GET /api/open/resources/{type}/{tmdb_id}
        返回数组: [{title, slug, unlock_points, is_unlocked, ...}]
        """
        result = await self._get(f"/resources/{media_type}/{tmdb_id}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            data = result.get("data") or result.get("results") or []
            return data if isinstance(data, list) else []
        return []

    async def unlock(self, slug: str) -> dict | None:
        """解锁资源 - /resources/unlock"""
        if self.mode == "symedia":
            return await self._symedia_transfer(slug)
        return await self._post("/resources/unlock", {"slug": slug})

    async def transfer(self, slug: str, folder_id: str = "") -> dict | None:
        """
        转存到 115 - /transfer
        folder_id: 目标文件夹 ID，空则用默认
        """
        data = {"slug": slug}
        if folder_id:
            data["folder_id"] = folder_id
        return await self._post("/transfer", data)

    async def unlock_and_transfer(self, slug: str, folder_id: str = "") -> dict | None:
        """解锁并转存到 115
        1. 调用 HDHive unlock 获取 115 分享链接
        2. 如果资源尚未拥有，通过 115 API 转存到目标文件夹
        """
        if self.mode == "symedia":
            return await self._symedia_transfer(slug, folder_id)

        unlock_result = await self.unlock(slug)
        if not unlock_result:
            return {"status": "error", "message": "HDHive unlock 返回空", "data": None}

        data = unlock_result.get("data") or {}
        if data.get("already_owned"):
            return {"status": "already_owned", "message": "资源已在 115 中", "data": data}

        full_url = data.get("full_url") or data.get("url", "")
        if not full_url:
            return {"status": "error", "message": "HDHive 解锁结果无分享链接", "data": unlock_result}

        from app.services.cloud115 import Cloud115Client
        c115 = Cloud115Client()
        try:
            parsed = c115.extract_share_code(full_url)
            if not parsed:
                # 非 115 网盘链接（如夸克/百度等）
                return {"status": "not_115", "message": "非 115 网盘资源", "data": None}

            target_folder = folder_id or settings.cloud115_folder_id or "0"
            result = await c115.transfer_from_share(
                parsed["share_code"],
                parsed.get("receive_code", ""),
                target_folder
            )
            if result:
                state = result.get("state")
                if state is True:
                    return {"status": "transferred", "data": result}
                err = result.get("error") or ""
                if "已经转存过" in err or "already" in err.lower():
                    return {"status": "already_owned", "message": "资源已在 115 中", "data": result}
                # 检测密码错误（提取码错误 / 密码错误等）
                err_lower = err.lower()
                is_password_error = any(kw in err_lower for kw in ["密码", "提取码", "password", "receive_code", "提取码错误", "密码错误"])
                print(f"[HDHive] 115 转存返回异常: {err}")
                if is_password_error:
                    return {"status": "password_error", "message": f"115 分享密码无效: {err[:60]}", "data": result}
                return {"status": "error", "message": f"115 转存失败: {err}", "data": result}
            return {"status": "error", "message": "115 API 无响应", "data": None}
        finally:
            await c115.close()

    async def get_account(self) -> dict | None:
        """获取账户信息"""
        return await self._get("/account")

    async def get_usage(self) -> dict | None:
        """获取用量统计"""
        return await self._get("/usage")

    async def get_usage_today(self) -> dict | None:
        """获取今日用量"""
        return await self._get("/usage/today")

    async def get_vip_weekly_quota(self) -> dict | None:
        """获取永V每周免费额度"""
        return await self._get("/vip/weekly-free-quota")

    async def checkin(self, is_gambler: bool = False) -> dict | None:
        """每日签到"""
        data = {"gambler": 1} if is_gambler else {}
        return await self._post("/checkin", data)

    async def check_connectivity(self) -> dict:
        """检查连接，返回详细状态"""
        if not self.is_configured:
            if self.mode == "symedia":
                return {"ok": False, "message": "未配置 Symedia：请设置 URL 和 Token/Cookie"}
            return {"ok": False, "message": "未配置影巢 API Key"}
        if self.mode == "symedia":
            try:
                resp = await self._client.get(
                    self._symedia_api_url("/discover/hdhive_status"),
                    headers=self._symedia_headers(),
                )
                if resp.status_code in (401, 403):
                    return {"ok": False, "message": "Symedia 认证失败：请检查 Token/Cookie"}
                resp.raise_for_status()
                data = resp.json()
                if data.get("configured"):
                    return {"ok": True, "message": "Symedia 影巢连接成功"}
                return {"ok": False, "message": data.get("message") or "Symedia 影巢未授权"}
            except Exception as e:
                return {"ok": False, "message": f"Symedia 连接异常: {e}"}
        try:
            # 使用 /usage 替代已废弃的 /account
            usage = await self.get_usage()
            if usage is not None:
                return {"ok": True, "message": "连接成功"}
            # 降级: 用 /resources/movie/1 检查连通性
            result = await self._get("/resources/movie/1")
            if result is not None:
                return {"ok": True, "message": "连接成功（API 有限）"}
            return {"ok": False, "message": "连接失败：API 返回空"}
        except Exception as e:
            return {"ok": False, "message": f"连接异常: {e}"}

    async def close(self):
        await self._client.aclose()

    async def _symedia_search(
        self,
        keyword: str = "",
        tmdb_id: int = 0,
        emby_item_id: str = "",
        media_type: str = "movie",
    ) -> list[dict]:
        if not self.is_configured:
            raise RuntimeError("影巢未配置：请先配置 Symedia URL 和 Token/Cookie")
        if not tmdb_id and emby_item_id:
            tmdb_id, resolved_type = await self._resolve_emby_info(emby_item_id)
            media_type = resolved_type or media_type
        media_type = self._normalize_media_type(media_type)

        payload = {
            "title": keyword or "",
            "media_type": media_type,
            "cloud_type": self.symedia_cloud_type,
            "tmdb_id": tmdb_id or None,
            "target": "hdhive",
        }
        resp = await self._client.post(
            self._symedia_api_url("/discover/search_resources"),
            json=payload,
            headers=self._symedia_headers(),
        )
        if resp.status_code in (401, 403):
            raise RuntimeError("Symedia 认证失败：请检查 Token/Cookie")
        resp.raise_for_status()
        data = resp.json()
        hdhive = data.get("hdhive") if isinstance(data, dict) else {}
        if hdhive and hdhive.get("configured") is False:
            raise RuntimeError(hdhive.get("message") or "Symedia 影巢未授权")
        items = hdhive.get("items", []) if isinstance(hdhive, dict) else []
        results = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            resource_url = normalized.get("resource_url") or normalized.get("url") or ""
            if resource_url:
                normalized["hdhive_slug"] = normalized.get("slug", "")
                normalized["slug"] = resource_url
                normalized.setdefault("page_url", resource_url)
            normalized.setdefault("source", ["HDHive"])
            results.append(normalized)
        print(f"[HDHive] Symedia 搜索 tmdb_id={tmdb_id} 返回 {len(results)} 条结果")
        return results

    async def _symedia_transfer(self, resource_url: str, folder_id: str = "") -> dict | None:
        if not self.is_configured:
            return {"status": "error", "message": "未配置 Symedia：请设置 URL 和 Token/Cookie", "data": None}
        if not resource_url:
            return {"status": "error", "message": "Symedia 转存缺少资源链接", "data": None}

        parent_id = folder_id or settings.symedia_parent_id or settings.cloud115_folder_id or "0"
        payload = {
            "cloud_type": self.symedia_cloud_type,
            "parent_id": parent_id,
            "url": resource_url,
        }
        resp = await self._client.post(
            self._symedia_api_url("/telegramsearch/transfer"),
            json=payload,
            headers=self._symedia_headers(),
        )
        if resp.status_code in (401, 403):
            return {"status": "error", "message": "Symedia 认证失败：请检查 Token/Cookie", "data": None}
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:500]}
        if resp.is_error:
            return {"status": "error", "message": f"Symedia 转存失败: HTTP {resp.status_code}", "data": data}
        if isinstance(data, dict) and data.get("success") is False:
            return {"status": "error", "message": data.get("message") or "Symedia 转存失败", "data": data}
        return {"status": "transferred", "message": "已提交 Symedia 转存", "data": data}
