"""影巢 HDHive API 服务 - 搜索 + 转存"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings


_ED2K_URL_RE = re.compile(
    r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9a-f]{32}\|(?:[^|\r\n]*\|)*/",
    re.IGNORECASE,
)


def _response_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_response_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_response_text(item) for item in value)
    return str(value)


def _extract_ed2k_urls(value) -> list[str]:
    """递归提取 Symedia 响应中的 ED2K 文件链接，按出现顺序去重。"""
    urls: list[str] = []
    seen: set[str] = set()

    def collect(item) -> None:
        if isinstance(item, str):
            text = item.replace("\\/", "/")
            for match in _ED2K_URL_RE.finditer(text):
                url = match.group(0)
                key = url.lower()
                if key not in seen:
                    seen.add(key)
                    urls.append(url)
            return
        if isinstance(item, dict):
            for nested in item.values():
                collect(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                collect(nested)

    collect(value)
    return urls


def _is_offline_submitted_response(data, message: str = "") -> bool:
    """识别上游已经完成离线任务提交的响应，避免重复添加。"""
    if isinstance(data, dict):
        if str(data.get("transfer_mode") or "").strip().lower() == "ed2k_offline":
            return True
        offline = data.get("offline")
        if isinstance(offline, dict):
            state = offline.get("state")
            if state is True or state == 1 or str(state or "").strip().lower() in {"1", "true", "success"}:
                return True
    text = f"{message or ''} {_response_text(data)}".lower()
    return any(
        marker in text
        for marker in (
            "离线任务已提交",
            "离线任务添加成功",
            "离线下载已提交",
            "offline task submitted",
            "offline task added",
        )
    )


def _is_transfer_failure_response(data, message: str = "") -> bool:
    """某些 Symedia 响应仍会返回 HTTP 200/success=true，失败需以业务文案判定。"""
    text = f"{message or ''} {_response_text(data)}".lower()
    return any(
        marker in text
        for marker in (
            "没有成功转存的链接",
            "无成功转存",
            "未成功转存",
            "转存失败",
            "离线任务提交失败",
            "离线任务添加失败",
            "transfer failed",
            "failed to transfer",
            "offline task failed",
        )
    )


def _is_already_transferred_response(data, message: str = "") -> bool:
    text = f"{message or ''} {_response_text(data)}".lower()
    return any(
        marker in text
        for marker in (
            "你已经转存过该文件",
            "已经转存过",
            "已转存过",
            "已在 115",
            "已在115",
            "already transferred",
            "already exists",
            "already owned",
        )
    )


class HDHiveClient:
    """
    影巢资源中心 API 客户端

    模式:
    - openapi: https://hdhive.com/api/open + X-API-Key
    - symedia: 走 Symedia 搜索/转存
    - cms: 复用 CMS 已授权的 authx 代理
    """

    BASE_URL = "https://hdhive.com/api/open"
    CMS_AUTHX_BASE = "https://authx.771885.xyz"
    CMS_API_PREFIX = "/api/hdhive"
    CMS_USER_AGENT = "auth-admin-hdhive-proxy-python/1.0"
    CMS_MODE_ALIASES = {"cms", "authx", "cms-authx", "cms_proxy"}

    def __init__(self, api_key: str = "", proxy: str = ""):
        self.api_key = api_key or settings.hdhive_api_key
        self.mode = (settings.hdhive_mode or "openapi").strip().lower()
        if self.mode in self.CMS_MODE_ALIASES:
            self.mode = "cms"
        self.symedia_url = self._normalize_symedia_url(settings.symedia_url or "")
        self.symedia_cloud_type = settings.symedia_cloud_type or "channel_115"
        self.cms_authx_url = str(
            getattr(settings, "hdhive_cms_authx_url", "") or self.CMS_AUTHX_BASE
        ).strip().rstrip("/")
        self.cms_token_path = str(getattr(settings, "hdhive_cms_token_path", "") or "").strip()
        self.proxy = proxy or settings.proxy
        timeout = httpx.Timeout(240.0, connect=8.0, write=30.0, pool=30.0) if self.mode == "symedia" else 15.0
        client_kwargs = {"timeout": timeout, "verify": False}
        if self.proxy and not (self.mode == "symedia" and self._is_private_url(self.symedia_url)):
            client_kwargs["proxy"] = self.proxy
            print(f"[HDHive] 使用代理: {self.proxy}")
        elif self.proxy and self.mode == "symedia":
            print(f"[HDHive] Symedia 内网地址 {self.symedia_url}，已跳过全局代理")
        self._client = httpx.AsyncClient(**client_kwargs)
        self._cms_token_lock = asyncio.Lock()
        self._cms_token_memory: dict = {}
        self._cms_token_memory_mtime = 0.0
        self._cms_token_cache_path = self._resolve_cms_token_cache_path()

    @property
    def is_configured(self) -> bool:
        """检查是否已配置"""
        if self.mode == "symedia":
            return bool(self.symedia_url and (settings.symedia_token or settings.symedia_cookie))
        if self.mode == "cms":
            return bool(self.cms_authx_url and self.cms_token_path and Path(self.cms_token_path).is_file())
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

    @staticmethod
    def _normalize_symedia_url(value: str) -> str:
        """兼容旧配置：Symedia host 网络实例当前 API 监听在 18095。"""
        text = (value or "").strip().rstrip("/")
        if not text:
            return ""
        parsed = urlparse(text)
        if parsed.hostname == "192.168.31.213" and parsed.port == 8095:
            return text.replace(":8095", ":18095", 1)
        return text

    @staticmethod
    def _is_private_url(value: str) -> bool:
        host = urlparse(value or "").hostname or ""
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return host.endswith(".local")

    async def _symedia_search_request(self, payload: dict) -> httpx.Response:
        """搜索请求是只读操作，允许重试来兜住 Symedia/影巢偶发慢查询。"""
        last_error: Exception | None = None
        url = self._symedia_api_url("/discover/search_resources")
        for attempt in range(1, 4):
            try:
                return await self._client.post(
                    url,
                    json=payload,
                    headers=self._symedia_headers(),
                )
            except httpx.TransportError as e:
                last_error = e
                print(f"[HDHive] Symedia 搜索连接失败 {attempt}/3: {type(e).__name__}: {e}")
                if attempt < 3:
                    await asyncio.sleep(1.2 * attempt)
        detail = f"{type(last_error).__name__}: {last_error}" if last_error else "未知网络错误"
        raise RuntimeError(f"Symedia/影巢搜索超时，请稍后重试或检查 Symedia 影巢插件状态（{detail}）")

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
        if self.mode == "cms":
            return await self._cms_search(keyword, tmdb_id, emby_item_id, media_type)

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
        if self.mode == "cms":
            return await self._cms_unlock(slug)
        slug = self._normalize_openapi_slug(slug)
        return await self._post("/resources/unlock", {"slug": slug})

    async def transfer(self, slug: str, folder_id: str = "") -> dict | None:
        """
        转存到 115 - /transfer
        folder_id: 目标文件夹 ID，空则用默认
        """
        if self.mode != "symedia":
            slug = self._normalize_openapi_slug(slug)
        data = {"slug": slug}
        if folder_id:
            data["folder_id"] = folder_id
        return await self._post("/transfer", data)

    async def unlock_and_transfer(self, slug: str, folder_id: str = "", force_transfer: bool = False) -> dict | None:
        """解锁并转存到 115
        1. 调用 HDHive unlock 获取 115 分享链接
        2. 如果资源尚未拥有，通过 115 API 转存到目标文件夹
        """
        if self.mode == "symedia":
            return await self._symedia_transfer(slug, folder_id, force_transfer=force_transfer)

        slug = self._normalize_openapi_slug(slug)
        unlock_result = await self.unlock(slug)
        if not unlock_result:
            return {"status": "error", "message": "HDHive unlock 返回空", "data": None}
        if unlock_result.get("success") is False:
            return {
                "status": "error",
                "message": str(unlock_result.get("message") or "HDHive 解锁失败"),
                "data": unlock_result,
            }

        data = unlock_result.get("data") or {}
        if data.get("already_owned") and not force_transfer:
            return {"status": "already_owned", "message": "资源已在 115 中", "data": data}

        full_url = data.get("full_url") or data.get("url", "")
        if not full_url:
            if data.get("already_owned") and force_transfer:
                return {"status": "error", "message": "资源已在 115 中，但解锁结果没有分享链接，无法强制重新转存", "data": unlock_result}
            return {"status": "error", "message": "HDHive 解锁结果无分享链接", "data": unlock_result}

        target_folder = folder_id or settings.cloud115_folder_id or "0"
        ed2k_urls = _extract_ed2k_urls(full_url)
        if ed2k_urls:
            return await self._submit_ed2k_offline(
                ed2k_urls,
                target_folder,
                full_url,
                transfer_data=unlock_result,
            )

        from app.services.cloud115 import Cloud115Client
        c115 = Cloud115Client()
        try:
            parsed = c115.extract_share_code(full_url)
            if not parsed:
                # 非 115 网盘链接（如夸克/百度等）
                return {"status": "not_115", "message": "非 115 网盘资源", "data": None}

            result = await c115.transfer_from_share(
                parsed["share_code"],
                parsed.get("receive_code", ""),
                target_folder
            )
            if result:
                if isinstance(result, dict):
                    result.setdefault("_share_code", parsed["share_code"])
                    result.setdefault("_receive_code", parsed.get("receive_code", ""))
                    result.setdefault("_target_folder", target_folder)
                state = result.get("state")
                if state is True:
                    return {"status": "transferred", "message": "115 转存已提交，等待落地校验", "data": result}
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
            if self.mode == "cms":
                return {"ok": False, "message": "未配置 CMS 影巢：请挂载 hdhive-openapi.json"}
            return {"ok": False, "message": "未配置影巢 API Key"}
        if self.mode == "cms":
            try:
                result = await self._cms_post("/me", {})
                if result.get("success") is True or result.get("data"):
                    return {"ok": True, "message": "CMS 影巢连接成功（已复用 CMS 授权）"}
                return {"ok": False, "message": "CMS 影巢连接失败"}
            except RuntimeError as e:
                return {"ok": False, "message": str(e)}
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

    @staticmethod
    def _normalize_openapi_slug(value: str) -> str:
        """OpenAPI 只接受资源 slug；Symedia 搜索结果可能把完整 URL 放在 slug 字段。"""
        text = (value or "").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            path = urlparse(text).path.rstrip("/")
            if path:
                return path.rsplit("/", 1)[-1]
        return text

    @staticmethod
    def _resolve_cms_token_cache_path() -> str:
        """Keep refreshed CMS credentials in the application's writable data area."""
        db_path = str(getattr(settings, "db_path", "") or "").strip()
        if db_path:
            return str(Path(db_path).expanduser().parent / "hdhive-openapi.cache.json")
        return "/tmp/hdhive-openapi.cache.json"

    def _cms_headers(self) -> dict:
        return {
            "User-Agent": self.CMS_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _cms_api_url(self, path: str) -> str:
        suffix = path if path.startswith("/") else f"/{path}"
        prefix = self.CMS_API_PREFIX
        if suffix == prefix or suffix.startswith(f"{prefix}/"):
            return f"{self.cms_authx_url}{suffix}"
        return f"{self.cms_authx_url}{prefix}{suffix}"

    def _read_cms_token_json(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"CMS 影巢 token 文件无法读取: {path}") from e
        if not isinstance(data, dict):
            raise RuntimeError("CMS 影巢 token 文件格式无效")
        return data

    def _load_cms_token_file(self) -> dict:
        source = Path(self.cms_token_path)
        cache = Path(self._cms_token_cache_path)
        source_mtime = source.stat().st_mtime if source.is_file() else 0.0
        cache_mtime = cache.stat().st_mtime if cache.is_file() else 0.0
        if self._cms_token_memory and self._cms_token_memory_mtime >= source_mtime:
            return dict(self._cms_token_memory)
        if cache.is_file() and cache_mtime >= source_mtime:
            try:
                cached = self._read_cms_token_json(cache)
                if cached.get("access_token") or cached.get("refresh_token"):
                    self._cms_token_memory = dict(cached)
                    self._cms_token_memory_mtime = cache_mtime
                    return dict(cached)
            except RuntimeError:
                pass
        if not source.is_file():
            raise RuntimeError(f"CMS 影巢 token 文件不存在: {source}")
        data = self._read_cms_token_json(source)
        self._cms_token_memory = dict(data)
        self._cms_token_memory_mtime = source_mtime
        return data

    def _save_cms_token_cache(self, values: dict) -> None:
        """Update an app-owned cache only; the mounted CMS token source is read-only."""
        current = dict(self._cms_token_memory)
        if not current:
            source = Path(self.cms_token_path)
            if source.is_file():
                try:
                    current = self._read_cms_token_json(source)
                except RuntimeError:
                    current = {}
        current.update({key: value for key, value in values.items() if value is not None})
        self._cms_token_memory = current
        self._cms_token_memory_mtime = time.time()
        cache = Path(self._cms_token_cache_path)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache.with_name(f"{cache.name}.tmp")
            temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(cache)
        except OSError as e:
            print(f"[HDHive] CMS token 已刷新并保留在内存，缓存写入跳过: {type(e).__name__}")

    def _redact_cms_message(self, value: object) -> str:
        text = str(value or "")
        for key in ("access_token", "refresh_token"):
            secret = str(self._cms_token_memory.get(key) or "").strip()
            if secret:
                text = text.replace(secret, "***")
        text = re.sub(
            r"(?i)\b(access[_-]?token|refresh[_-]?token|token)\b\s*([:=])\s*([\"']?)[^,\s\"'}]+",
            r"\1\2***",
            text,
        )
        text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer ***", text)
        return text[:300]

    def _format_cms_auth_error(
        self,
        message: str = "",
        *,
        code: str = "",
        description: str = "",
        status: int = 0,
        action: str = "请求",
    ) -> str:
        """Turn authx responses into actionable, token-safe messages."""
        safe_message = self._redact_cms_message(message)
        safe_description = self._redact_cms_message(description)
        safe_code = self._redact_cms_message(code)
        blob = " ".join(value for value in (safe_message, safe_description, safe_code) if value).lower()
        if "openapi_reauth_required" in blob or "重新授权" in blob or "revok" in blob:
            return (
                "CMS 影巢授权已失效（refresh_token 已撤销）。"
                "请到 CMS 重新完成影巢 OpenAPI 授权，并确认 hdhive-openapi.json 已更新后再试。"
            )
        if "openapi_refresh_required" in blob or "access token expired" in blob or "token expired" in blob:
            return "CMS 影巢 access_token 已过期，刷新失败；请重新授权后再试。"
        if "refresh_token" in blob and ("缺失" in blob or "missing" in blob):
            return "CMS 影巢 refresh_token 缺失，请先在 CMS 完成影巢授权。"
        detail = safe_message or safe_description
        if detail:
            return f"CMS 影巢{action}失败: {detail}" + (f"（{safe_code}）" if safe_code else "")
        if status:
            return f"CMS 影巢{action}失败: HTTP {status}"
        return f"CMS 影巢{action}失败"

    @staticmethod
    def _cms_response_data(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _cms_error_values(data: dict) -> tuple[str, str, str]:
        return (
            str(data.get("message") or ""),
            str(data.get("description") or ""),
            str(data.get("code") or data.get("error") or ""),
        )

    @classmethod
    def _cms_response_requires_refresh(cls, response: httpx.Response, data: dict) -> bool:
        if response.status_code in (401, 403):
            return True
        message, description, code = cls._cms_error_values(data)
        text = " ".join(value for value in (message, description, code) if value).lower()
        return any(
            marker in text
            for marker in (
                "openapi_refresh_required",
                "access token expired",
                "token expired",
                "invalid token",
                "invalid_token",
                "unauthorized",
                "expired",
                "过期",
                "失效",
                "未授权",
            )
        )

    async def _cms_refresh_token(self) -> str:
        async with self._cms_token_lock:
            config = self._load_cms_token_file()
            refresh_token = str(config.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError("CMS 影巢 refresh_token 缺失，请先在 CMS 完成影巢授权。")
            try:
                response = await self._client.post(
                    self._cms_api_url("/oauth/refresh"),
                    json={"refresh_token": refresh_token},
                    headers=self._cms_headers(),
                )
            except httpx.HTTPError as e:
                raise RuntimeError("CMS 影巢刷新请求异常，请稍后重试。") from e
            payload = self._cms_response_data(response)
            if response.is_error or payload.get("success") is False:
                message, description, code = self._cms_error_values(payload)
                raise RuntimeError(
                    self._format_cms_auth_error(
                        message or description,
                        code=code,
                        description=description,
                        status=response.status_code,
                        action="刷新",
                    )
                )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            access_token = str(data.get("access_token") or "").strip()
            if not access_token:
                raise RuntimeError("CMS 影巢 token 刷新未返回 access_token")
            self._save_cms_token_cache(
                {
                    "access_token": access_token,
                    "refresh_token": data.get("refresh_token") or refresh_token,
                    "token_type": data.get("token_type") or config.get("token_type") or "Bearer",
                    "expires_in": data.get("expires_in") or config.get("expires_in"),
                    "refresh_expires_in": data.get("refresh_expires_in") or config.get("refresh_expires_in"),
                    "scope": data.get("scope") or config.get("scope"),
                    "scopes": data.get("scopes") or config.get("scopes"),
                }
            )
            return access_token

    async def _cms_access_token(self) -> str:
        config = self._load_cms_token_file()
        access_token = str(config.get("access_token") or "").strip()
        return access_token or await self._cms_refresh_token()

    async def _cms_post(self, path: str, payload: dict | None = None, *, allow_refresh: bool = True) -> dict:
        if not self.is_configured:
            raise RuntimeError("CMS 影巢未配置：请挂载 hdhive-openapi.json")
        body = dict(payload or {})
        body["access_token"] = await self._cms_access_token()
        response: httpx.Response | None = None
        data: dict = {}
        for attempt in range(2):
            try:
                response = await self._client.post(
                    self._cms_api_url(path),
                    json=body,
                    headers=self._cms_headers(),
                )
            except httpx.HTTPError as e:
                raise RuntimeError("CMS 影巢请求异常，请稍后重试。") from e
            data = self._cms_response_data(response)
            if attempt == 0 and allow_refresh and self._cms_response_requires_refresh(response, data):
                body["access_token"] = await self._cms_refresh_token()
                continue
            break
        if response is None:
            raise RuntimeError("CMS 影巢请求未得到响应")
        if response.is_error or data.get("success") is False:
            message, description, code = self._cms_error_values(data)
            raise RuntimeError(
                self._format_cms_auth_error(
                    message or description,
                    code=code,
                    description=description,
                    status=response.status_code,
                )
            )
        return data

    @staticmethod
    def _normalize_cms_resource(item: dict, media_type: str = "movie") -> dict | None:
        if not isinstance(item, dict):
            return None
        raw_slug = str(
            item.get("slug")
            or item.get("hdhive_slug")
            or item.get("resource_url")
            or item.get("page_url")
            or item.get("url")
            or ""
        ).strip()
        resource_url = str(item.get("resource_url") or item.get("page_url") or item.get("url") or "").strip()
        resource_hint = f"{raw_slug} {resource_url}".lower()
        raw_pan_type = str(item.get("pan_type") or item.get("drive_type") or "").strip().lower()
        if raw_pan_type in {"115", "channel_115"} or "/resource/115/" in resource_hint:
            pan_type = "115"
        elif raw_pan_type == "ed2k" or resource_hint.startswith("ed2k://") or "/resource/ed2k/" in resource_hint:
            pan_type = "ed2k"
        else:
            return None
        slug = HDHiveClient._normalize_openapi_slug(raw_slug)
        if not slug:
            return None
        normalized = dict(item)
        if not resource_url:
            resource_url = f"https://hdhive.com/resource/{pan_type}/{slug}"
        normalized["hdhive_slug"] = slug
        normalized["slug"] = slug
        normalized["resource_url"] = resource_url
        normalized["page_url"] = resource_url
        normalized["pan_type"] = pan_type
        normalized["resource_kind"] = pan_type
        normalized["is_ed2k"] = pan_type == "ed2k"
        normalized.setdefault("source", ["HDHive"])
        normalized.setdefault("media_type", media_type)
        normalized["_source_label"] = "hdhive-cms"
        return normalized

    async def _cms_search(
        self,
        keyword: str = "",
        tmdb_id: int = 0,
        emby_item_id: str = "",
        media_type: str = "movie",
    ) -> list[dict]:
        if not self.is_configured:
            raise RuntimeError("CMS 影巢未配置：请挂载 hdhive-openapi.json")
        if not tmdb_id and emby_item_id:
            tmdb_id, resolved_type = await self._resolve_emby_info(emby_item_id)
            media_type = resolved_type or media_type
        media_type = self._normalize_media_type(media_type)
        if not tmdb_id and keyword:
            tmdb_id = await self._resolve_keyword_tmdb_id(keyword, media_type)
        if not tmdb_id:
            return []
        payload = await self._cms_post(
            "/resources",
            {"resource_type": media_type, "tmdb_id": str(tmdb_id)},
        )
        items = payload.get("data") if isinstance(payload.get("data"), list) else []
        return [
            normalized
            for item in items
            if (normalized := self._normalize_cms_resource(item, media_type)) is not None
        ]

    async def _resolve_keyword_tmdb_id(self, keyword: str, media_type: str = "movie") -> int:
        api_key = str(getattr(settings, "tmdb_api_key", "") or "").strip()
        if not api_key:
            return 0
        endpoint = "search/tv" if media_type == "tv" else "search/movie"
        try:
            response = await self._client.get(
                f"https://api.themoviedb.org/3/{endpoint}",
                params={"api_key": api_key, "query": keyword, "language": "zh-CN", "page": 1},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError:
            return 0
        except Exception:
            return 0
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            return 0
        value = results[0].get("id") if isinstance(results[0], dict) else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _cms_unlock(self, slug: str) -> dict:
        slug = self._normalize_openapi_slug(slug)
        if not slug:
            return {"success": False, "message": "缺少资源 slug", "data": None}
        try:
            payload = await self._cms_post("/resources/unlock", {"slug": slug})
        except RuntimeError as e:
            return {"success": False, "message": str(e), "data": None}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        data = dict(data)
        if not data.get("full_url"):
            url = str(data.get("url") or "").strip()
            access_code = str(data.get("access_code") or "").strip()
            if url and access_code and "password=" not in url:
                separator = "&" if "?" in url else "?"
                data["full_url"] = f"{url}{separator}password={access_code}"
            elif url:
                data["full_url"] = url
        return {
            "success": bool(payload.get("success", True)),
            "message": str(payload.get("message") or ""),
            "data": data,
        }

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

        base_payload = {
            "title": keyword or "",
            "media_type": media_type,
            "tmdb_id": tmdb_id or None,
            "target": "hdhive",
        }
        cloud_types = ("channel_115", "ed2k")
        responses = await asyncio.gather(
            *(
                self._symedia_search_request({**base_payload, "cloud_type": cloud_type})
                for cloud_type in cloud_types
            ),
            return_exceptions=True,
        )

        results: list[dict] = []
        seen_resources: set[str] = set()
        errors: list[str] = []
        for cloud_type, response in zip(cloud_types, responses):
            if isinstance(response, BaseException):
                errors.append(f"{cloud_type}: {response}")
                continue
            if response.status_code in (401, 403):
                raise RuntimeError("Symedia 认证失败：请检查 Token/Cookie")
            try:
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                errors.append(f"{cloud_type}: {type(e).__name__}: {e}")
                continue
            hdhive = data.get("hdhive") if isinstance(data, dict) else {}
            if hdhive and hdhive.get("configured") is False:
                errors.append(f"{cloud_type}: {hdhive.get('message') or 'Symedia 影巢未授权'}")
                continue
            items = hdhive.get("items", []) if isinstance(hdhive, dict) else []
            for index, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                normalized = dict(item)
                resource_url = str(normalized.get("resource_url") or normalized.get("url") or "").strip()
                original_slug = str(normalized.get("slug") or "").strip()
                resource_hint = (resource_url or original_slug).lower()
                inferred_pan_type = ""
                if resource_hint.startswith("ed2k://") or "/resource/ed2k/" in resource_hint:
                    inferred_pan_type = "ed2k"
                elif "/resource/115/" in resource_hint:
                    inferred_pan_type = "115"
                raw_pan_type = str(
                    normalized.get("pan_type")
                    or inferred_pan_type
                    or ("ed2k" if cloud_type == "ed2k" else "115")
                ).strip().lower()
                if raw_pan_type in {"115", "channel_115"}:
                    pan_type = "115"
                elif raw_pan_type == "ed2k":
                    pan_type = "ed2k"
                else:
                    continue
                resource_key = (resource_url or original_slug).rstrip("/").casefold()
                if not resource_key:
                    resource_key = f"{cloud_type}:{index}:{normalized.get('title') or ''}".casefold()
                if resource_key in seen_resources:
                    continue
                seen_resources.add(resource_key)
                if resource_url:
                    normalized["hdhive_slug"] = original_slug
                    normalized["slug"] = resource_url
                    normalized.setdefault("page_url", resource_url)
                normalized["pan_type"] = pan_type
                normalized["resource_kind"] = pan_type
                normalized["is_ed2k"] = pan_type == "ed2k"
                normalized.setdefault("source", ["HDHive"])
                results.append(normalized)
        if errors:
            detail = "; ".join(errors)
            if not results:
                raise RuntimeError(f"Symedia 影巢搜索失败: {detail}")
            print(f"[HDHive] Symedia 影巢搜索部分失败，已保留 {len(results)} 条有效结果: {detail}")
        print(f"[HDHive] Symedia 搜索 tmdb_id={tmdb_id} 返回 {len(results)} 条结果")
        return results

    async def _submit_ed2k_offline(
        self,
        ed2k_urls: list[str],
        parent_id: str,
        resource_url: str,
        transfer_data=None,
    ) -> dict:
        """将解锁得到的 ED2K 链接直接提交到 115 目标目录。"""
        from app.services.cloud115 import Cloud115Client

        unique_urls = list(dict.fromkeys(url for url in ed2k_urls if url))
        data = dict(transfer_data) if isinstance(transfer_data, dict) else {}
        if transfer_data is not None and not isinstance(transfer_data, dict):
            data["symedia_response"] = transfer_data
        data.update({
            "transfer_mode": "ed2k_offline",
            "ed2k_url": unique_urls[0] if unique_urls else "",
            "ed2k_urls": unique_urls,
            "_target_folder": str(parent_id or "0"),
            "_resource_url": resource_url,
        })
        if not unique_urls:
            return {"status": "error", "message": "影巢解锁结果无有效 ED2K 链接", "data": data}

        client = Cloud115Client()
        submissions: list[dict] = []
        try:
            for ed2k_url in unique_urls:
                try:
                    result = await client.create_offline_task(ed2k_url, str(parent_id or "0"))
                except Exception as e:
                    result = {
                        "state": False,
                        "success": False,
                        "error": f"115 离线接口异常: {e}",
                    }
                submissions.append({"url": ed2k_url, "response": result})
        finally:
            await client.close()

        def submitted(item: dict) -> bool:
            result = item.get("response")
            if not isinstance(result, dict):
                return False
            state = result.get("state")
            return state is True or state == 1 or str(state or "").strip().lower() in {"1", "true", "success"}

        accepted = [item for item in submissions if submitted(item)]
        data["offline"] = submissions[0]["response"] if len(submissions) == 1 else submissions
        data["offline_results"] = submissions
        if not accepted:
            first = submissions[0].get("response") if submissions else None
            error = ""
            if isinstance(first, dict):
                error = str(
                    first.get("error_msg")
                    or first.get("error")
                    or first.get("message")
                    or first.get("msg")
                    or ""
                ).strip()
            return {
                "status": "error",
                "message": f"ED2K 提交 115 离线失败: {error or '115 API 未确认任务'}",
                "data": data,
            }
        if len(accepted) < len(submissions):
            return {
                "status": "transferred",
                "message": f"ED2K 已部分提交到 115 离线目录（{len(accepted)}/{len(submissions)}）",
                "data": data,
            }
        return {
            "status": "transferred",
            "message": "ED2K 已提交到 115 离线目录",
            "data": data,
        }

    async def _symedia_transfer(self, resource_url: str, folder_id: str = "", force_transfer: bool = False) -> dict | None:
        if not self.is_configured:
            return {"status": "error", "message": "未配置 Symedia：请设置 URL 和 Token/Cookie", "data": None}
        if not resource_url:
            return {"status": "error", "message": "Symedia 转存缺少资源链接", "data": None}

        parent_id = folder_id or settings.symedia_parent_id or settings.cloud115_folder_id or "0"
        direct_ed2k_urls = _extract_ed2k_urls(resource_url)
        if direct_ed2k_urls:
            return await self._submit_ed2k_offline(
                direct_ed2k_urls,
                parent_id,
                resource_url,
            )
        resource_lower = resource_url.lower()
        transfer_cloud_type = "ed2k" if "/resource/ed2k/" in resource_lower else self.symedia_cloud_type
        payload = {
            "cloud_type": transfer_cloud_type,
            "parent_id": parent_id,
            "url": resource_url,
        }
        if force_transfer:
            payload["force"] = True
            payload["force_transfer"] = True
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
        if isinstance(data, dict):
            data.setdefault("_target_folder", parent_id)
            data.setdefault("_resource_url", resource_url)
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("msg") or data.get("error") or "")
        if _is_already_transferred_response(data, message):
            return {"status": "already_owned", "message": "资源已在 115 中（已转存过该文件）", "data": data}
        if resp.is_error:
            return {"status": "error", "message": f"Symedia 转存失败: HTTP {resp.status_code}", "data": data}
        if (isinstance(data, dict) and data.get("success") is False) or _is_transfer_failure_response(data, message):
            return {"status": "error", "message": message or "Symedia 转存失败", "data": data}
        ed2k_urls = _extract_ed2k_urls(data)
        if ed2k_urls:
            if _is_offline_submitted_response(data, message):
                if isinstance(data, dict):
                    data.setdefault("transfer_mode", "ed2k_offline")
                    data.setdefault("ed2k_url", ed2k_urls[0])
                    data.setdefault("ed2k_urls", ed2k_urls)
                return {
                    "status": "transferred",
                    "message": message or "ED2K 已提交到 115 离线目录",
                    "data": data,
                }
            return await self._submit_ed2k_offline(
                ed2k_urls,
                parent_id,
                resource_url,
                transfer_data=data,
            )
        if isinstance(data, dict) and data.get("success") is True:
            return {"status": "transferred", "message": message or "Symedia 转存已提交，等待落地校验", "data": data}
        return {"status": "submitted", "message": message or "已提交 Symedia 转存，等待落地确认", "data": data}
