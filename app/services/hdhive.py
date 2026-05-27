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
        self.proxy = proxy or settings.proxy
        client_kwargs = {"timeout": 15.0, "verify": False}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy
            print(f"[HDHive] 使用代理: {self.proxy}")
        self._client = httpx.AsyncClient(**client_kwargs)

    @property
    def is_configured(self) -> bool:
        """检查是否已配置"""
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

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
        if not self.is_configured:
            raise RuntimeError("影巢未配置：请先在系统配置中设置 API Key")

        # 从 emby_id 获取 TMDB ID
        if not tmdb_id and emby_item_id:
            tmdb_id = await self._resolve_tmdb_id(emby_item_id)

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
        from app.services.emby import EmbyClient
        emby = EmbyClient()
        try:
            item = await emby.get_item(emby_item_id)
            if item:
                pid = item.get("ProviderIds", {})
                tmdb_str = pid.get("Tmdb", "")
                if tmdb_str and tmdb_str.isdigit():
                    return int(tmdb_str)
        except Exception as e:
            print(f"[HDHive] 解析 TMDB ID 失败: {e}")
        finally:
            await emby.close()
        return 0

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
            return {"ok": False, "message": "未配置影巢 API Key"}
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
