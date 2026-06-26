"""配置管理 API"""

from fastapi import APIRouter, HTTPException, Body
from app.config import settings
from app.models.schemas import ConfigModel

router = APIRouter(prefix="/api/config", tags=["配置"])


def _format_env_line(key: str, value: object) -> str:
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"{key} 包含非法换行")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


@router.get("")
async def get_config():
    """获取当前配置（隐藏敏感信息）"""
    # 获取 Emby ServerId
    emby_server_id = ""
    try:
        from app.services.emby import EmbyClient
        ec = EmbyClient()
        try:
            info = await ec.get_system_info()
            if info:
                emby_server_id = info.get("Id", "")
        finally:
            await ec.close()
    except Exception:
        pass

    return {
        "status": "success",
        "data": {
            "emby_host": settings.emby_host,
            "emby_server_id": emby_server_id,
            "emby_api_key": "••••" + settings.emby_api_key[-4:] if settings.emby_api_key else "",
            "moviepilot_url": settings.moviepilot_url,
            "moviepilot_token": "••••" + settings.moviepilot_token[-4:] if settings.moviepilot_token else "",
            "hdhive_api_key": "已设置" if settings.hdhive_api_key else "",
            "hdhive_mode": settings.hdhive_mode,
            "symedia_url": settings.symedia_url,
            "symedia_token": "已设置" if settings.symedia_token else "",
            "symedia_cookie": "已设置" if settings.symedia_cookie else "",
            "symedia_cloud_type": settings.symedia_cloud_type,
            "symedia_parent_id": settings.symedia_parent_id,
            "cloud115_cookie": "已设置" if settings.cloud115_cookie else "",
            "cloud115_folder_id": settings.cloud115_folder_id,
            "clouddrive_url": settings.clouddrive_url,
            "clouddrive_token": "已设置" if settings.clouddrive_token else "",
            "exclude_library_ids": settings.exclude_library_ids,
            "scan_schedule": settings.scan_schedule,
            "proxy": settings.proxy,
            "tg_bot_token": "已设置" if settings.tg_bot_token else "",
            "tg_chat_id": settings.tg_chat_id,
            "tg_notify_events": settings.tg_notify_events,
        }
    }


@router.post("")
async def save_config(config: ConfigModel):
    """保存配置到环境变量文件（仅覆盖有意义的改动，保留现有值）"""
    import os
    env_path = "/workspace/config/.env"
    try:
        data = config.model_dump()

        sensitive_keys = {
            "emby_api_key",
            "moviepilot_token",
            "hdhive_api_key",
            "symedia_token",
            "symedia_cookie",
            "cloud115_cookie",
            "clouddrive_token",
            "tg_bot_token",
        }
        masked_values = {"已设置"}

        # 密码框留空代表保留旧值；普通字段允许改回默认值或清空。
        for key in sensitive_keys:
            val = data.get(key, "")
            existing = getattr(settings, key, "")
            if existing and (not val or val in masked_values or str(val).startswith("••••")):
                data[key] = existing

        tmp_path = f"{env_path}.tmp"
        with open(tmp_path, "w") as f:
            for k, v in data.items():
                key = f"REFINER_{k.upper()}"
                f.write(_format_env_line(key, v))
        os.replace(tmp_path, env_path)
        # 重新加载配置
        for k, v in data.items():
            setattr(settings, k, v)
        return {"status": "success", "message": "配置已保存"}
    except Exception as e:
        raise HTTPException(500, f"保存失败: {e}")


@router.post("/test/emby")
async def test_emby():
    """测试 Emby 连接"""
    from app.services.emby import EmbyClient
    emby = EmbyClient()
    try:
        info = await emby.get_system_info()
        if info:
            return {"status": "success", "message": f"已连接到 Emby: {info.get('ServerName', '')}"}
        return {"status": "error", "message": "连接失败"}
    finally:
        await emby.close()


@router.post("/test/moviepilot")
async def test_moviepilot():
    """测试 MoviePilot 连接"""
    from app.services.moviepilot import MoviePilotClient
    mp = MoviePilotClient()
    try:
        result = await mp.check_connectivity()
        return {"status": "success" if result.get("ok") else "error",
                "message": result.get("message", "连接失败")}
    finally:
        await mp.close()


@router.post("/test/hdhive")
async def test_hdhive():
    """测试影巢连接"""
    from app.services.hdhive import HDHiveClient
    hd = HDHiveClient()
    try:
        result = await hd.check_connectivity()
        return {"status": "success" if result.get("ok") else "error",
                "message": result.get("message", "连接失败")}
    finally:
        await hd.close()


@router.post("/test/cloud115")
async def test_cloud115():
    """测试 115 网盘连接"""
    from app.services.cloud115 import Cloud115Client
    c115 = Cloud115Client()
    try:
        ok = await c115.check_connectivity()
        return {"status": "success" if ok else "error",
                "message": "连接成功" if ok else "连接失败"}
    finally:
        await c115.close()


@router.post("/test/clouddrive")
async def test_clouddrive():
    """测试 CloudDrive2 连接，并刷新目录缓存。"""
    from app.services.clouddrive import CloudDriveClient
    cd2 = CloudDriveClient()
    try:
        result = await cd2.check_connectivity()
        return {
            "status": "success" if result.get("ok") else "error",
            "message": result.get("message", "连接失败"),
        }
    finally:
        await cd2.close()


@router.post("/test/telegram")
async def test_telegram():
    """测试 Telegram 机器人连接"""
    from app.services.telegram import TelegramNotifier
    tg = TelegramNotifier()
    try:
        ok = await tg.check_connectivity()
        return {"status": "success" if ok else "error",
                "message": "机器人连接成功" if ok else "连接失败，请检查 Token"}
    finally:
        await tg.close()


@router.post("/test/telegram/send")
async def send_test_message():
    """发送测试通知"""
    from app.services.telegram import TelegramNotifier
    tg = TelegramNotifier()
    try:
        ok = await tg.send_message("✅ *Media Refiner* 测试通知\n\n如果你收到这条消息，说明 TG 机器人配置正确喵～")
        return {"status": "success" if ok else "error",
                "message": "消息已发送" if ok else "发送失败，请检查 Token 和 Chat ID"}
    finally:
        await tg.close()


@router.post("/schedule")
async def set_scan_schedule(body: dict = Body(...)):
    """设置定时扫描间隔（小时），0=禁用"""
    from fastapi import HTTPException
    hours = body.get("hours", "0")
    try:
        hours_int = int(hours)
        hours_int = max(0, min(168, hours_int))  # 0~168h
    except (ValueError, TypeError):
        raise HTTPException(400, "无效的间隔值")
    import os
    env_path = "/workspace/config/.env"
    try:
        # 读取现有 env，只修改 REFINER_SCAN_SCHEDULE
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if line.startswith("REFINER_SCAN_SCHEDULE="):
                        lines.append(f"REFINER_SCAN_SCHEDULE={hours_int}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"REFINER_SCAN_SCHEDULE={hours_int}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        settings.scan_schedule = str(hours_int)
        return {"status": "success", "data": {"scan_schedule": str(hours_int)}}
    except Exception as e:
        raise HTTPException(500, f"保存失败: {e}")
