"""Telegram 机器人 API 路由"""

from fastapi import APIRouter, HTTPException
from app.services.telegram import TelegramNotifier

router = APIRouter(prefix="/api/telegram", tags=["Telegram"])


@router.get("/updates")
async def get_updates():
    """获取最近的更新（用于自动获取 Chat ID）"""
    tg = TelegramNotifier()
    try:
        updates = await tg.get_updates()
        result = []
        for upd in updates:
            msg = upd.get("message", upd.get("my_chat_member", {}))
            chat = msg.get("chat", {})
            if chat.get("id"):
                result.append({
                    "chat_id": chat["id"],
                    "chat_type": chat.get("type", ""),
                    "title": chat.get("title") or chat.get("first_name", "") or "",
                })
        # 去重
        seen = set()
        unique = []
        for r in result:
            key = r["chat_id"]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return {"status": "success", "data": unique}
    finally:
        await tg.close()


@router.post("/check-callbacks")
async def check_tg_callbacks():
    """手动触发一次 TG callback 查询（用于调试）"""
    tg = TelegramNotifier()
    try:
        await tg.check_pending_callbacks()
        return {"status": "success", "message": "已检查 TG callback"}
    finally:
        await tg.close()
