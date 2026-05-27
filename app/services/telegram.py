"""Telegram 通知服务 + 审核操作交互"""

import httpx
import json
import logging
from app.config import settings

logger = logging.getLogger(__name__)

# 可用通知事件定义
NOTIFY_EVENTS = {
    "review": "🆕 新审核项",
    "scan": "📊 扫描完成",
    "download": "⬇️ 下载/转存结果",
}


def _fmt_bytes(b):
    """格式化字节数为可读字符串"""
    if not b:
        return "未知"
    try:
        b = int(b)
        if b >= 1073741824:
            return f"{b / 1073741824:.1f} GB"
        if b >= 1048576:
            return f"{b / 1048576:.1f} MB"
        return f"{b} B"
    except (ValueError, TypeError):
        return str(b)


def _fmt_resolution(res: str) -> str:
    """友好的分辨率标签"""
    if not res or res == "0x0":
        return "未知"
    h = res.split("x")[-1] if "x" in res else ""
    if h == "2160":
        return "4K"
    if h == "1080":
        return "1080p"
    if h == "720":
        return "720p"
    return res


def _fmt_codec(codec: str) -> str:
    """格式化编码"""
    c = (codec or "").lower()
    if "h265" in c or "hevc" in c:
        return "HEVC"
    if "h264" in c or "avc" in c:
        return "H.264"
    if "av1" in c:
        return "AV1"
    if "vc1" in c:
        return "VC-1"
    if "mpeg" in c:
        return "MPEG"
    return (codec or "未知").upper()


def _fmt_hdr(video_range: str) -> str:
    """格式化 HDR"""
    r = (video_range or "").lower()
    if "dolby" in r or "dv" in r:
        return "Dolby Vision"
    if "hdr10" in r:
        return "HDR10"
    if "hdr" in r:
        return "HDR"
    if "sdr" in r or not r:
        return "SDR"
    return r


def _fmt_quality(res: dict) -> str:
    """从搜索结果提取质量标记"""
    q = (res.get("resolution") or "").lower()
    if q:
        return q
    vr = res.get("video_resolution") or []
    if vr and isinstance(vr, list):
        return vr[0].lower()
    return ""


def _build_review_card_text(review: dict) -> tuple[str, str]:
    """
    构建审核详细对比文本。
    返回 (text, source_label) 用于 TG 消息。
    格式与网页双栏卡片一致：
      ── 当前文件 ──
      文件名: xxx
      分辨率: 1080p
      编码: H.264
      HDR: SDR
      大小: 12.3 GB

      ── 目标文件 ──
      种子: xxx
      分辨率: 4K
      来源: 影巢
      编码: HEVC
      大小: 15.6 GB
    """
    current = review.get("current_quality", {}) or {}
    target = review.get("search_result", {}) or {}
    src = review.get("source", "")
    src_label = "影巢" if src == "hdhive" else "MoviePilot"

    item_name = review.get("item_name", "未知")

    # 当前文件
    cur_name = current.get("name", item_name)
    cur_res = _fmt_resolution(current.get("resolution", ""))
    cur_codec = _fmt_codec((current.get("video_codec") or ""))
    cur_hdr = _fmt_hdr(current.get("video_range", ""))
    cur_size = _fmt_bytes(current.get("size_bytes", 0))

    lines = [
        f"📽 *{item_name}*",
        "",
        "`── 当前文件 ──`",
        f"📄 文件名: `{cur_name}`",
        f"🖥 分辨率: `{cur_res}`",
        f"🔧 编码: `{cur_codec}`",
        f"🌈 HDR: `{cur_hdr}`",
        f"💾 大小: `{cur_size}`",
        "",
    ]

    # 目标文件
    target_title = target.get("title") or target.get("name", "")
    if src == "hdhive":
        t_res_list = target.get("video_resolution") or []
        t_res = t_res_list[0] if t_res_list else "未知"
        t_source_list = target.get("source") or []
        t_source = ", ".join(t_source_list) if t_source_list else "未知"
        t_codec = "未知"
        t_size = _fmt_bytes(target.get("share_size", 0))
        # 字幕信息
        sub_lang = target.get("subtitle_language", "")
        sub_type = target.get("subtitle_type", "")
        sub_str = ""
        if sub_lang:
            sub_str = f"{sub_lang}"
            if sub_type:
                sub_str += f" ({sub_type})"
    else:
        # MP
        t_res = _fmt_quality(target)
        t_source = src_label
        t_codec = _fmt_codec(target.get("video_codec") or "")
        t_size = _fmt_bytes(target.get("size", 0))
        t_seeders = target.get("seeders", 0)
        t_site = target.get("site_name", "")
        sub_str = ""

    lines.append("`── 目标文件 ──`")
    if target_title:
        lines.append(f"📄 种子: `{target_title}`")
    lines.append(f"🖥 分辨率: `{t_res}`")
    lines.append(f"🔗 来源: `{t_source}`")
    if t_codec != "未知":
        lines.append(f"🔧 编码: `{t_codec}`")
    lines.append(f"💾 大小: `{t_size}`")
    if t_site:
        lines.append(f"🏠 站点: `{t_site}`")
    if src == "moviepilot" and t_seeders is not None:
        lines.append(f"🧲 做种: `{t_seeders}`")
    if sub_str:
        lines.append(f"📝 字幕: `{sub_str}`")

    # HDHive 额外信息
    if src == "hdhive":
        unlock_pts = target.get("unlock_points", "")
        uploader = target.get("user", {}).get("nickname", "") if isinstance(target.get("user"), dict) else ""
        if unlock_pts:
            lines.append(f"🔓 解锁积分: `{unlock_pts}`")
        if uploader:
            lines.append(f"👤 上传者: `{uploader}`")
        remark = target.get("remark", "")
        if remark:
            lines.append(f"📝 备注: `{remark[:80]}`")

    text = "\n".join(lines)
    return text, src_label


class TelegramNotifier:
    """Telegram 机器人通知 + 审核交互"""

    # 类级别持久化
    _last_update_id = 0
    _last_poll_count = 0
    _review_cache: list[dict] = []  # 审核列表缓存，供选择+操作使用

    def __init__(self):
        self.token = settings.tg_bot_token
        self.chat_id = settings.tg_chat_id.strip() if settings.tg_chat_id else ""
        client_kwargs = {"timeout": 15.0}
        proxy = settings.proxy or ""
        if proxy:
            client_kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    # ─── 基础 API ───

    async def check_connectivity(self) -> bool:
        """检查机器人 Token 是否有效"""
        if not self.token:
            return False
        try:
            resp = await self._client.get(
                f"https://api.telegram.org/bot{self.token}/getMe"
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception as e:
            logger.warning(f"[Telegram] 连接检查失败: {e}")
            return False

    async def get_updates(self) -> list:
        """获取最近的更新（用于获取 Chat ID）"""
        if not self.token:
            return []
        try:
            resp = await self._client.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"timeout": 5, "limit": 10}
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("result", [])
            return []
        except Exception as e:
            logger.warning(f"[Telegram] 获取更新失败: {e}")
            return []

    async def send_message(self, text: str, parse_mode: str = "Markdown",
                           reply_markup: dict = None) -> bool:
        """发送消息到已配置的 Chat ID"""
        if not self.is_configured:
            return False
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception as e:
            logger.warning(f"[Telegram] 消息发送失败: {e}")
            return False

    async def send_notification(self, event: str, title: str, detail: str = "") -> bool:
        """发送格式化通知（仅当该事件类型已启用时）"""
        if not self._is_event_enabled(event):
            return False
        label = NOTIFY_EVENTS.get(event, event)
        text = f"{label}\n\n*{title}*"
        if detail:
            text += f"\n{detail}"
        return await self.send_message(text)

    async def edit_message_reply_markup(self, message_id: int,
                                        reply_markup: dict = None) -> bool:
        """编辑消息的 inline keyboard（用于按钮状态更新）"""
        if not self.token:
            return False
        try:
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageReplyMarkup",
                json=payload
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception as e:
            logger.warning(f"[Telegram] 编辑按钮失败: {e}")
            return False

    async def answer_callback_query(self, callback_query_id: str, text: str = "",
                                    show_alert: bool = False) -> bool:
        """答复 callback query（消除按钮 loading）"""
        if not self.token:
            return False
        try:
            payload = {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json=payload
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception as e:
            logger.warning(f"[Telegram] 答复 callback 失败: {e}")
            return False

    # ─── 审核卡片 ───

    async def send_review_card(self, review: dict) -> bool:
        """
        发送审核详细卡片 + 操作按钮。
        review dict 来自 list_subscribe_reviews()
        """
        if not self.is_configured or not self._is_event_enabled("review"):
            return False

        text, src_label = _build_review_card_text(review)
        review_id = review.get("id", 0)

        # 内联键盘：通过 / 拒绝 / 忽略
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ 通过", "callback_data": f"review_approve:{review_id}"},
                {"text": "❌ 拒绝", "callback_data": f"review_reject:{review_id}"},
                {"text": "⏭ 忽略", "callback_data": f"review_ignore:{review_id}"},
            ]]
        }

        return await self.send_message(text, reply_markup=reply_markup)

    # ─── Callback 处理 ───

    async def set_bot_commands(self) -> bool:
        """注册机器人命令菜单"""
        if not self.token:
            return False
        try:
            commands = [
                {"command": "start", "description": "🏠 欢迎 & 指令列表"},
                {"command": "help", "description": "❓ 使用帮助"},
                {"command": "scan", "description": "🔍 启动质量扫描"},
                {"command": "run", "description": "▶️ 运行所有订阅规则"},
                {"command": "rules", "description": "📜 列出订阅规则"},
                {"command": "reviews", "description": "📋 待审核项"},
                {"command": "logs", "description": "📄 最近活动日志"},
                {"command": "reset", "description": "🗑 删除条目历史，恢复搜索"},
                {"command": "clear", "description": "🗑 清空待审核项"},
                {"command": "status", "description": "📊 服务器状态"},
            ]
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self.token}/setMyCommands",
                json={"commands": commands}
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception as e:
            logger.warning(f"[Telegram] 设置命令菜单失败: {e}")
            return False

    async def check_pending_callbacks(self):
        """
        轮询检查 TG callback_query + 文本指令。
        在后台定时任务中调用。
        """
        if not self.token:
            return
        try:
            offset = TelegramNotifier._last_update_id + 1
            resp = await self._client.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 5,
                    "limit": 10,
                }
            )
            data = resp.json()
            if not data.get("ok"):
                return

            results = data.get("result", [])
            if results:
                seen_ids = [u.get('update_id') for u in results]
                logger.info(f"[TGPoll] 收到 {len(results)} 个更新, offset={offset}, "
                           f"ids={seen_ids}, last_known_id={TelegramNotifier._last_update_id}")
            else:
                if TelegramNotifier._last_poll_count < 5:
                    TelegramNotifier._last_poll_count += 1

            for update in results:
                update_id = update.get("update_id", 0)

                # 安全跳过：已见过的更新（防并发/重启重复拉取）
                if update_id <= TelegramNotifier._last_update_id:
                    continue

                TelegramNotifier._last_update_id = update_id

                # 处理 callback_query（按钮回传）
                cb = update.get("callback_query")
                if cb:
                    await self._process_callback(cb)
                    continue

                # 处理文本消息（指令）
                msg = update.get("message")
                if msg and msg.get("text", "").startswith("/"):
                    await self._handle_command(msg)

        except Exception as e:
            logger.warning(f"[Telegram] 处理更新异常: {e}")

    async def _process_callback(self, cb: dict):
        """处理一个 callback_query"""
        cb_id = cb.get("id", "")
        data_str = cb.get("data", "")
        msg = cb.get("message", {})
        message_id = msg.get("message_id", 0)

        if not data_str:
            await self.answer_callback_query(cb_id, "未知操作", show_alert=True)
            return

        # ── 无冒号的简单回调（导航/批次操作） ──
        if ":" not in data_str:
            handler = {
                "back_to_list": self._handle_back_to_list,
                "batch_approve_all": self._handle_batch_approve_all,
                "batch_smart_approve": self._handle_batch_smart_approve,
                "batch_clear_all": self._handle_batch_clear_all,
                "rule_run_all": self._handle_rule_run_all,
                "rule_refresh": self._handle_rule_refresh,
            }.get(data_str)
            if handler:
                await handler(cb_id, message_id)
            else:
                await self.answer_callback_query(cb_id, "未知操作", show_alert=True)
            return

        # ── 带冒号的审核操作回调 ──
        action_part, review_id_str = data_str.split(":", 1)

        # ── 规则相关回调 ──
        if action_part == "rule_info":
            await self._handle_rule_info(cb_id, review_id_str, message_id)
            return
        if action_part == "rule_run":
            await self._handle_rule_run_single(cb_id, review_id_str, message_id)
            return

        if not review_id_str.isdigit():
            await self.answer_callback_query(cb_id, "无效的审核ID", show_alert=True)
            return
        review_id = int(review_id_str)

        if action_part == "sel_rev":
            await self._handle_show_review_actions(cb_id, review_id, message_id)
        elif action_part == "review_approve":
            await self._handle_approve(cb_id, review_id, message_id)
        elif action_part == "review_reject":
            await self._handle_reject(cb_id, review_id, message_id)
        elif action_part == "review_ignore":
            await self._handle_ignore(cb_id, review_id, message_id)
        else:
            await self.answer_callback_query(cb_id, "未知操作", show_alert=True)
        return

    async def _handle_command(self, msg: dict):
        """处理文本指令消息"""
        text = msg.get("text", "").strip()
        chat_id = msg.get("chat", {}).get("id", "")
        if not chat_id:
            return

        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        try:
            if cmd in ("/start",):
                await self._send_command_reply(chat_id, _build_help_text())
            elif cmd in ("/help",):
                await self._send_command_reply(chat_id, _build_help_text())
            elif cmd == "/reviews":
                await self._handle_list_reviews(chat_id)
            elif cmd == "/rules":
                await self._handle_list_rules(chat_id)
            elif cmd == "/logs":
                await self._handle_logs(chat_id)
            elif cmd == "/scan":
                await self._handle_cmd_scan(chat_id)
            elif cmd == "/run":
                await self._handle_cmd_run(chat_id, args)
            elif cmd == "/clear":
                await self._handle_cmd_clear(chat_id)
            elif cmd == "/status":
                await self._handle_status(chat_id)
            elif cmd == "/reset" or cmd == "/unlock":
                await self._handle_cmd_reset(chat_id, args)
            else:
                await self._send_command_reply(chat_id,
                    f"❓ 未知指令: {cmd}\n发送 /help 查看可用指令")
        except Exception as e:
            logger.warning(f"[Telegram] 指令处理失败: {e}")

    async def _send_command_reply(self, chat_id: str, text: str):
        """向指定 chat 发送纯文本回复（不依赖 self.chat_id）"""
        if not self.token:
            return
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                }
            )
        except Exception as e:
            logger.warning(f"[Telegram] 发送回复失败: {e}")

    async def _handle_list_reviews(self, chat_id: str):
        """列出所有待审核项 → 点 #号 弹出操作菜单"""
        from app.database import list_subscribe_reviews
        TelegramNotifier._review_cache = await list_subscribe_reviews("pending")
        reviews = TelegramNotifier._review_cache
        if not reviews:
            TelegramNotifier._review_cache = []
            await self._send_command_reply(chat_id, "📋 *待审核列表*\n\n暂无待审核项 (｡･ω･｡)")
            return

        def _e(t):
            for ch in ['_', '*', '~', '>', '`', '[']:
                t = str(t).replace(ch, '\\' + ch)
            return t

        # 文本列表（#序号从 1 开始）
        show = reviews[:10]
        lines = [
            f"📋 *待审核列表*（共 {len(reviews)} 项）\n"
            "点击下方编号选择要操作的审核项\n"
        ]
        for idx, r in enumerate(show, 1):
            name = _e(r.get("item_name", "?"))
            src = "影巢" if r.get("source") == "hdhive" else "MP"
            r_res = (r.get("search_result", {}) or {})
            quality = _e(_fmt_resolution(r_res.get("resolution", "")))
            lines.append(f"`#{idx}` {name} ({src} · {quality})")
        if len(reviews) > 10:
            lines.append(f"\n...还有 {len(reviews) - 10} 项")

        # 编号选择键盘（每行 5 个编号）
        keyboard = []
        row = []
        for idx, r in enumerate(show, 1):
            row.append({"text": f"#{idx}", "callback_data": f"sel_rev:{idx - 1}"})
            if len(row) == 5:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        # 底部批次操作
        if len(reviews) > 1:
            keyboard.append([
                {"text": "🤖 智能通过", "callback_data": "batch_smart_approve"},
                {"text": "✅ 全部批准", "callback_data": "batch_approve_all"},
                {"text": "🗑 清空列表", "callback_data": "batch_clear_all"},
            ])

        reply_markup = {"inline_keyboard": keyboard}
        text = "\n".join(lines)

        if not self.token:
            return
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": reply_markup,
                }
            )
        except Exception as e:
            logger.warning(f"[Telegram] 发送审核列表失败: {e}")

    async def _handle_list_rules(self, chat_id: str):
        """列出所有订阅规则"""
        from app.database import list_subscribe_rules
        rules = await list_subscribe_rules()
        if not rules:
            await self._send_command_reply(chat_id, "📜 *订阅规则*\n\n暂无规则，在 Web 界面创建喵～")
            return

        def _escape_md(text: str) -> str:
            """转义 Markdown 特殊字符（除了反引号内的内容）"""
            for ch in ['_', '*', '~', '>', '`', '[']:
                text = text.replace(ch, '\\' + ch)
            return text

        lines = [f"📜 *订阅规则*（共 {len(rules)} 条）\n"]
        keyboard = []
        for idx, r in enumerate(rules, 1):
            name = _escape_md(r.get("name", "未命名"))
            enabled = "🟢" if r.get("enabled", True) else "🔴"
            auto = "🤖" if r.get("auto_approve") else ""
            cron = r.get("cron_expression", "")
            cron_tag = f" `⏰{cron}`" if cron else ""
            lines.append(f"{enabled} `#{idx}` {name}{auto}{cron_tag}")
            keyboard.append([{
                "text": f"{enabled} {r.get('name', '未命名')[:12]}",
                "callback_data": f"rule_info:{r['id']}"
            }])

        # 底部全局操作
        keyboard.append([
            {"text": "▶️ 运行全部", "callback_data": "rule_run_all"},
            {"text": "🔄 刷新", "callback_data": "rule_refresh"},
        ])

        reply_markup = {"inline_keyboard": keyboard}
        await self._client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "\n".join(lines),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            }
        )

    async def _handle_logs(self, chat_id: str, limit: int = 10):
        """显示最近的活动日志"""
        from app.database import get_subscribe_logs
        logs = await get_subscribe_logs(limit=limit)

        if not logs:
            await self._send_command_reply(chat_id, "📄 *活动日志*\n\n暂无记录")
            return

        # 动作图标映射
        action_icons = {
            "download": "⬇️", "transfer": "☁️", "no_match": "🔍",
            "no_target": "🚫", "error": "❌", "complete": "✅", "skip": "⏭️",
            "auto_approved": "🤖",
        }

        lines = [f"📄 *最近 {len(logs)} 条活动日志*\n"]
        for log in logs:
            action = log.get("action", "")
            icon = action_icons.get(action, "•")
            rule = log.get("rule_name", "")
            msg = (log.get("message") or "")[:60]
            item = log.get("item_name", "")
            created = log.get("created_at", "")
            time_str = str(created)[:19] if created else ""
            line = f"{icon} `{rule}` {msg}"
            if item:
                line += f"\n   📎 {item}"
            if time_str:
                line += f"\n   🕐 {time_str}"
            lines.append(line)

        await self._send_command_reply(chat_id, "\n".join(lines))

    async def _handle_show_review_actions(self, cb_id: str, idx: int, message_id: int):
        """选中某个 #编号 后，弹出操作菜单"""
        cache = TelegramNotifier._review_cache
        if idx < 0 or idx >= len(cache):
            await self.answer_callback_query(cb_id, "编号无效，请重新发送 /reviews", show_alert=True)
            return
        review = cache[idx]
        rid = review.get("id", 0)
        number = idx + 1

        # 构建审核详情文本
        text, src_label = _build_review_card_text(review)
        detail = f"📋 *待审核 #{number}*\n\n" + text

        # 操作按钮键盘
        keyboard = [
            [
                {"text": "✅ 通过", "callback_data": f"review_approve:{rid}"},
                {"text": "❌ 拒绝", "callback_data": f"review_reject:{rid}"},
                {"text": "⏭ 忽略", "callback_data": f"review_ignore:{rid}"},
            ],
            [{"text": "← 返回列表", "callback_data": "back_to_list"}],
        ]

        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": detail,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": keyboard},
                }
            )
            await self.answer_callback_query(cb_id, "")
        except Exception as e:
            logger.warning(f"[Telegram] 显示审核操作失败: {e}")
            await self.answer_callback_query(cb_id, "操作失败", show_alert=True)

    async def _handle_cmd_scan(self, chat_id: str):
        """启动质量扫描"""
        try:
            from app.routers.scan import _run_scan
            import asyncio
            asyncio.create_task(_run_scan([]))
            await self._send_command_reply(chat_id, "🔍 *质量扫描已启动*\n\n正在后台扫描 Emby 媒体库，请稍候查看结果～")
        except Exception as e:
            await self._send_command_reply(chat_id, f"❌ 启动扫描失败: {str(e)[:80]}")

    async def _handle_cmd_run(self, chat_id: str, args: list = None):
        """运行订阅规则（支持 rule_id 参数）"""
        try:
            from app.routers.subscribe import _run_subscribe
            import asyncio
            rule_id = args[0] if args else ""
            if rule_id:
                # 单规则运行：通过 API 的 rule_id 参数
                from app.database import list_subscribe_rules
                rules = await list_subscribe_rules()
                rule = next((r for r in rules if r["id"] == rule_id), None)
                if not rule:
                    await self._send_command_reply(chat_id, f"❌ 未找到规则: {rule_id}")
                    return
                asyncio.create_task(_run_subscribe(rule_id=rule_id))
                await self._send_command_reply(chat_id,
                    f"▶️ *已启动规则: {rule.get('name', '未命名')}*\n\n正在匹配，新审核项会自动推送到聊天喵～")
            else:
                asyncio.create_task(_run_subscribe())
                await self._send_command_reply(chat_id, "▶️ *订阅规则已启动*\n\n正在运行所有订阅规则匹配，新审核项会自动推送到聊天喵～")
        except Exception as e:
            await self._send_command_reply(chat_id, f"❌ 运行订阅失败: {str(e)[:80]}")

    async def _handle_cmd_clear(self, chat_id: str):
        """清空所有待审核项"""
        try:
            from app.database import clear_subscribe_reviews
            deleted = await clear_subscribe_reviews()
            await self._send_command_reply(chat_id, f"🗑 *待审核项已清空*\n\n共删除 {deleted} 项待审核记录")
        except Exception as e:
            await self._send_command_reply(chat_id, f"❌ 清空失败: {str(e)[:80]}")

    async def _handle_status(self, chat_id: str):
        """发送服务器状态"""
        from app.routers.subscribe import _run_status
        from app.database import count_pending_reviews
        try:
            pending = await count_pending_reviews()
            scanning = _run_status.get("running", False)
            progress = _run_status.get("progress", 0)
            status = f"🟢 运行中" if scanning else "⚪ 空闲"
            lines = [
                "📊 *Media Refiner 状态*\n",
                f"• 服务状态: `{status}`",
                f"• 运行进度: `{progress}%`" if scanning else None,
                f"• 待审核: `{pending} 项`",
            ]
            await self._send_command_reply(chat_id, "\n".join(line for line in lines if line))
        except Exception as e:
            await self._send_command_reply(chat_id, f"❌ 获取状态失败: {e}")

    async def _handle_cmd_reset(self, chat_id: str, args: list):
        """重置指定条目的历史记录，恢复可搜索状态
        用法: /reset <item_id>
        从活动日志中可获取 item_id
        """
        if not args:
            await self._send_command_reply(chat_id,
                "❌ 请指定条目 ID\n用法: `/reset <item_id>`\n\n"
                "在活动日志 `/logs` 中可查看各条目的 item_id。")
            return
        item_id = args[0]
        from app.database import delete_subscribe_history
        try:
            result = await delete_subscribe_history(item_id)
            total = result["logs"] + result["reviews"] + result["ignores"]
            await self._send_command_reply(chat_id,
                f"🗑 *已重置条目 {item_id}*\n\n"
                f"删除: {result['logs']} 条日志、{result['reviews']} 条审核、{result['ignores']} 条忽略\n"
                f"下次运行规则时会重新搜索此条目。")
        except Exception as e:
            await self._send_command_reply(chat_id, f"❌ 重置失败: {e}")

    async def _handle_approve(self, cb_id: str, review_id: int, message_id: int):
        """处理通过操作（来自 callback_query 按钮）"""
        from app.database import list_subscribe_reviews, update_subscribe_review, add_subscribe_log
        from app.services.moviepilot import MoviePilotClient
        from app.services.hdhive import HDHiveClient

        reviews = await list_subscribe_reviews("pending")
        review = next((r for r in reviews if r["id"] == review_id), None)
        if not review:
            await self.answer_callback_query(cb_id, "❌ 审核项不存在或已处理", show_alert=True)
            await self.edit_message_reply_markup(message_id, {
                "inline_keyboard": [[{"text": "⏳ 已处理", "callback_data": "done"}]]
            })
            return

        item_name = review.get("item_name", "")
        result = review.get("search_result", {})
        action_type = review.get("action_type", "")

        try:
            if action_type == "download":
                mp = MoviePilotClient()
                try:
                    torrent_url = result.get("enclosure", "")
                    if not torrent_url:
                        raise ValueError("无下载链接")
                    tmdbid = 0
                    item_id = review.get("item_id", "")
                    if item_id:
                        from app.services.emby import EmbyClient
                        emby = EmbyClient()
                        try:
                            emby_item = await emby.get_item(item_id)
                            if emby_item:
                                pid = emby_item.get("ProviderIds", {})
                                tmdb_str = pid.get("Tmdb", "")
                                if tmdb_str and tmdb_str.isdigit():
                                    tmdbid = int(tmdb_str)
                        finally:
                            await emby.close()
                    resp = await mp.download(torrent_url, torrent_info=result, tmdbid=tmdbid)
                    if not resp or not resp.get("success"):
                        raise ValueError(f"下载提交失败")
                    await update_subscribe_review(review_id, "approved", "✅ TG 批准 - 下载已推送")
                    await add_subscribe_log(review.get("rule_id", ""), review.get("rule_name", ""), "download", item_name, review.get("item_id", ""), "TG 审核通过")
                    await self.answer_callback_query(cb_id, f"✅ {item_name} 下载已推送", show_alert=False)
                finally:
                    await mp.close()

            elif action_type == "transfer":
                hd = HDHiveClient()
                try:
                    slug = result.get("slug", "")
                    if not slug:
                        raise ValueError("无转存标识")
                    resp = await hd.unlock_and_transfer(slug)
                    if not resp:
                        raise ValueError("转存失败（HDHive API 无响应）")
                    status = resp.get("status", "")
                    if status == "transferred":
                        await update_subscribe_review(review_id, "approved", "✅ TG 批准 - 转存成功")
                        await add_subscribe_log(review.get("rule_id", ""), review.get("rule_name", ""), "transfer", item_name, review.get("item_id", ""), "TG 审核通过 - 转存成功")
                        await self.answer_callback_query(cb_id, f"✅ {item_name} 转存成功", show_alert=False)
                    elif status == "already_owned":
                        await update_subscribe_review(review_id, "approved", "✅ TG 批准 - 已在 115 中")
                        await self.answer_callback_query(cb_id, f"ℹ️ {item_name} 已在 115 中", show_alert=False)
                    else:
                        raise ValueError(resp.get("message", f"转存异常: {status}"))
                finally:
                    await hd.close()
            else:
                raise ValueError(f"未知操作类型: {action_type}")

            await self.edit_message_reply_markup(message_id, {
                "inline_keyboard": [[{"text": "✅ 已通过", "callback_data": "done"}]]
            })

        except Exception as e:
            await update_subscribe_review(review_id, "failed", str(e))
            await self.answer_callback_query(cb_id, f"❌ 执行失败: {str(e)[:50]}", show_alert=True)
            await self.edit_message_reply_markup(message_id, {
                "inline_keyboard": [[{"text": "❌ 执行失败", "callback_data": "done"}]]
            })

    async def _handle_reject(self, cb_id: str, review_id: int, message_id: int):
        """处理拒绝操作（来自 callback_query 按钮）"""
        from app.database import list_subscribe_reviews, update_subscribe_review
        reviews = await list_subscribe_reviews("pending")
        review = next((r for r in reviews if r["id"] == review_id), None)
        if not review:
            await self.answer_callback_query(cb_id, "❌ 审核项不存在或已处理", show_alert=True)
            return
        item_name = review.get("item_name", "")
        await update_subscribe_review(review_id, "rejected", "❌ TG 拒绝")
        await self.answer_callback_query(cb_id, f"❌ 已拒绝：{item_name}", show_alert=False)
        await self.edit_message_reply_markup(message_id, {
            "inline_keyboard": [[{"text": "❌ 已拒绝", "callback_data": "done"}]]
        })

    async def _handle_ignore(self, cb_id: str, review_id: int, message_id: int):
        """处理忽略操作（来自 callback_query 按钮）"""
        from app.database import list_subscribe_reviews, update_subscribe_review, add_subscribe_ignore
        reviews = await list_subscribe_reviews("pending")
        review = next((r for r in reviews if r["id"] == review_id), None)
        if not review:
            await self.answer_callback_query(cb_id, "❌ 审核项不存在或已处理", show_alert=True)
            return
        item_name = review.get("item_name", "")
        rule_id = review.get("rule_id", "")
        item_id = review.get("item_id", "")
        await add_subscribe_ignore(rule_id, item_id, item_name)
        await update_subscribe_review(review_id, "ignored", "⏭ TG 忽略")
        await self.answer_callback_query(cb_id, f"⏭ 已忽略：{item_name}", show_alert=False)
        await self.edit_message_reply_markup(message_id, {
            "inline_keyboard": [[{"text": "⏭ 已忽略", "callback_data": "done"}]]
        })

    async def _handle_batch_approve_all(self, cb_id: str, message_id: int):
        """批量批准所有待审核项"""
        from app.database import list_subscribe_reviews
        from app.services.moviepilot import MoviePilotClient
        from app.services.hdhive import HDHiveClient
        reviews = await list_subscribe_reviews("pending")
        if not reviews:
            await self.answer_callback_query(cb_id, "没有待审核项", show_alert=True)
            return
        total = len(reviews)
        ok = 0
        fail = 0
        for r in reviews:
            review_id = r.get("id", 0)
            try:
                if r.get("action_type") == "download":
                    mp = MoviePilotClient()
                    try:
                        result = r.get("search_result", {})
                        url = result.get("enclosure", "")
                        if url:
                            await mp.download(url, torrent_info=result)
                            await self._update_review_status(review_id, "approved", "批量批准 - 下载已推送")
                            ok += 1
                        else:
                            fail += 1
                    finally:
                        await mp.close()
                elif r.get("action_type") == "transfer":
                    hd = HDHiveClient()
                    try:
                        result = r.get("search_result", {})
                        slug = result.get("slug", "")
                        if slug:
                            resp = await hd.unlock_and_transfer(slug)
                            st = resp.get("status", "") if resp else ""
                            if st in ("transferred", "already_owned"):
                                await self._update_review_status(review_id, "approved", "批量批准 - 转存成功")
                                ok += 1
                            else:
                                fail += 1
                        else:
                            fail += 1
                    finally:
                        await hd.close()
                else:
                    fail += 1
            except Exception:
                fail += 1
        msg = f"✅ 批量批准完成：成功 {ok} 项，失败 {fail} 项（共 {total}）"
        await self.answer_callback_query(cb_id, msg, show_alert=True)
        await self.edit_message_reply_markup(message_id, {"inline_keyboard": []})

    async def _handle_batch_clear_all(self, cb_id: str, message_id: int):
        """清空所有待审核项"""
        from app.database import clear_subscribe_reviews
        deleted = await clear_subscribe_reviews()
        await self.answer_callback_query(cb_id, f"🗑 已清空 {deleted} 项待审核", show_alert=True)
        await self.edit_message_reply_markup(message_id, {"inline_keyboard": []})

    async def _update_review_status(self, review_id: int, status: str, message: str):
        """更新审核状态（辅助方法）"""
        from app.database import update_subscribe_review, add_subscribe_log
        await update_subscribe_review(review_id, status, message)

    async def _handle_batch_smart_approve(self, cb_id: str, message_id: int):
        """智能通过：只批准同时满足 4K + Remux + 字幕 的审核项"""
        from app.routers.subscribe import _is_4k, _is_remux, _has_subtitle
        from app.database import list_subscribe_reviews, update_subscribe_review, add_subscribe_log
        from app.services.moviepilot import MoviePilotClient
        from app.services.hdhive import HDHiveClient
        reviews = await list_subscribe_reviews("pending")
        if not reviews:
            await self.answer_callback_query(cb_id, "没有待审核项", show_alert=True)
            return

        total = len(reviews)
        smart_ok = 0
        approved = 0
        failed = 0
        for r in reviews:
            result = r.get("search_result", {}) or {}
            # 条件检查：必须同时满足勾选的所有条件
            conditions = []
            if _is_remux(result):
                conditions.append("Remux")
            if _is_4k(result):
                conditions.append("4K")
            if _has_subtitle(result):
                conditions.append("字幕")
            # 必须同时满足 4K + Remux + 字幕才自动批准
            if not (_is_4k(result) and _is_remux(result) and _has_subtitle(result)):
                continue
            smart_ok += 1
            review_id = r.get("id", 0)
            try:
                if r.get("action_type") == "download":
                    mp = MoviePilotClient()
                    try:
                        url = result.get("enclosure", "")
                        if url:
                            await mp.download(url, torrent_info=result)
                            await update_subscribe_review(review_id, "approved", "🤖 智能通过 - 4K+Remux+字幕")
                            approved += 1
                        else:
                            failed += 1
                    finally:
                        await mp.close()
                elif r.get("action_type") == "transfer":
                    hd = HDHiveClient()
                    try:
                        slug = result.get("slug", "")
                        if slug:
                            resp = await hd.unlock_and_transfer(slug)
                            st = resp.get("status", "") if resp else ""
                            if st in ("transferred", "already_owned"):
                                await update_subscribe_review(review_id, "approved", "🤖 智能通过 - 4K+Remux+字幕")
                                approved += 1
                            else:
                                failed += 1
                        else:
                            failed += 1
                    finally:
                        await hd.close()
                else:
                    failed += 1
            except Exception:
                failed += 1

        msg = (f"🤖 智能通过完成：共 {total} 项\n"
               f"  符合条件（4K+Remux+字幕）: {smart_ok} 项\n"
               f"  已自动批准: {approved} 项\n"
               f"  执行失败: {failed} 项")
        await self.answer_callback_query(cb_id, f"🤖 智能批准 {approved}/{smart_ok} 项", show_alert=True)
        await self.edit_message_reply_markup(message_id, {"inline_keyboard": []})

    async def _handle_back_to_list(self, cb_id: str, message_id: int):
        """从详情返回审核列表"""
        cache = TelegramNotifier._review_cache
        if not cache:
            await self.answer_callback_query(cb_id, "列表已过期，请重新发送 /reviews", show_alert=True)
            return

        def _e(t):
            for ch in ['_', '*', '~', '>', '`', '[']:
                t = str(t).replace(ch, '\\' + ch)
            return t

        show = cache[:10]
        lines = [
            f"📋 *待审核列表*（共 {len(cache)} 项）\n"
            "点击下方编号选择要操作的审核项\n"
        ]
        for idx, r in enumerate(show, 1):
            name = _e(r.get("item_name", "?"))
            src = "影巢" if r.get("source") == "hdhive" else "MP"
            r_res = (r.get("search_result", {}) or {})
            quality = _e(_fmt_resolution(r_res.get("resolution", "")))
            lines.append(f"`#{idx}` {name} ({src} · {quality})")
        if len(cache) > 10:
            lines.append(f"\n...还有 {len(cache) - 10} 项")

        keyboard = []
        row = []
        for idx, r in enumerate(show, 1):
            row.append({"text": f"#{idx}", "callback_data": f"sel_rev:{idx - 1}"})
            if len(row) == 5:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        if len(cache) > 1:
            keyboard.append([
                {"text": "🤖 智能通过", "callback_data": "batch_smart_approve"},
                {"text": "✅ 全部批准", "callback_data": "batch_approve_all"},
                {"text": "🗑 清空列表", "callback_data": "batch_clear_all"},
            ])

        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": "\n".join(lines),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": keyboard},
                }
            )
            await self.answer_callback_query(cb_id, "")
        except Exception as e:
            logger.warning(f"[Telegram] 返回列表失败: {e}")

    # ─── 规则操作 ───

    async def _handle_rule_info(self, cb_id: str, rule_id: str, message_id: int):
        """显示规则详情 + 运行按钮"""
        from app.database import list_subscribe_rules
        rules = await list_subscribe_rules()
        rule = next((r for r in rules if r["id"] == rule_id), None)
        if not rule:
            await self.answer_callback_query(cb_id, "❌ 规则不存在", show_alert=True)
            return

        def _e(t):
            for ch in ['_', '*', '~', '>', '`', '[']:
                t = t.replace(ch, '\\' + ch)
            return t

        name = _e(rule.get("name", "未命名"))
        enabled = "🟢 启用" if rule.get("enabled", True) else "🔴 禁用"
        source = rule.get("source", "moviepilot")
        src_label = {"moviepilot": "MoviePilot", "hdhive": "影巢", "both": "MP+影巢"}.get(source, source)
        target_res = rule.get("target_resolution", "1080p")
        auto = "🤖 是" if rule.get("auto_approve") else "—"
        cron = rule.get("cron_expression") or "无"
        upgraded = rule.get("total_upgraded", 0)
        last_run = rule.get("last_run", "从未运行")

        lines = [
            f"📜 *{name}*",
            f"状态: `{enabled}`",
            f"来源: `{src_label}`",
            f"目标: `{target_res}`",
            f"自动审核: `{auto}`",
            f"定时: `{cron}`",
            f"已升级: `{upgraded} 条`",
            f"上次运行: `{last_run}`",
        ]
        text = "\n".join(lines)

        keyboard = [
            [
                {"text": "▶️ 运行此规则", "callback_data": f"rule_run:{rule_id}"},
            ],
            [{"text": "← 返回规则列表", "callback_data": "rule_refresh"}],
        ]
        reply_markup = {"inline_keyboard": keyboard}
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": reply_markup,
                }
            )
            await self.answer_callback_query(cb_id, "")
        except Exception as e:
            logger.warning(f"[Telegram] 显示规则详情失败: {e}")
            await self.answer_callback_query(cb_id, "操作失败", show_alert=True)

    async def _handle_rule_run_single(self, cb_id: str, rule_id: str, message_id: int):
        """通过回调运行单条规则"""
        from app.routers.subscribe import _run_subscribe
        import asyncio
        asyncio.create_task(_run_subscribe(rule_id=rule_id))
        await self.answer_callback_query(cb_id, "▶️ 规则已启动", show_alert=False)
        try:
            await self.edit_message_reply_markup(message_id, {
                "inline_keyboard": [[{"text": "✅ 运行中...", "callback_data": "done"}]]
            })
        except Exception:
            pass

    async def _handle_rule_run_all(self, cb_id: str, message_id: int):
        """运行全部规则（来自回调）"""
        from app.routers.subscribe import _run_subscribe
        import asyncio
        asyncio.create_task(_run_subscribe())
        await self.answer_callback_query(cb_id, "▶️ 全部规则已启动", show_alert=False)
        try:
            await self.edit_message_reply_markup(message_id, {
                "inline_keyboard": [[{"text": "✅ 运行中...", "callback_data": "done"}]]
            })
        except Exception:
            pass

    async def _handle_rule_refresh(self, cb_id: str, message_id: int):
        """刷新规则列表"""
        await self.answer_callback_query(cb_id, "🔄 刷新中...")
        from app.database import list_subscribe_rules
        rules = await list_subscribe_rules()
        if not rules:
            try:
                await self._client.post(
                    f"https://api.telegram.org/bot{self.token}/editMessageText",
                    json={
                        "chat_id": self.chat_id,
                        "message_id": message_id,
                        "text": "📜 *订阅规则*\n\n暂无规则，在 Web 界面创建喵～",
                        "parse_mode": "Markdown",
                    }
                )
            except Exception:
                pass
            return

        def _e(t):
            for ch in ['_', '*', '~', '>', '`', '[']:
                t = t.replace(ch, '\\' + ch)
            return t

        lines = [f"📜 *订阅规则*（共 {len(rules)} 条）\n"]
        keyboard = []
        for idx, r in enumerate(rules, 1):
            name = r.get("name", "未命名")
            enabled = "🟢" if r.get("enabled", True) else "🔴"
            auto = "🤖" if r.get("auto_approve") else ""
            cron = r.get("cron_expression", "")
            cron_tag = f" `⏰{cron}`" if cron else ""
            lines.append(f"{enabled} `#{idx}` {_e(name)}{auto}{cron_tag}")
            keyboard.append([{
                "text": f"{enabled} {name[:12]}",
                "callback_data": f"rule_info:{r['id']}"
            }])
        keyboard.append([
            {"text": "▶️ 运行全部", "callback_data": "rule_run_all"},
            {"text": "🔄 刷新", "callback_data": "rule_refresh"},
        ])
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": "\n".join(lines),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": keyboard},
                }
            )
        except Exception as e:
            logger.warning(f"[Telegram] 刷新规则列表失败: {e}")

    # ─── 事件过滤 ───

    def _is_event_enabled(self, event: str) -> bool:
        """检查事件类型是否在已启用列表中"""
        if not self.is_configured:
            return False
        enabled = settings.tg_notify_events or ""
        events = [e.strip() for e in enabled.split(",") if e.strip()]
        return event in events

    async def close(self):
        await self._client.aclose()


def _build_help_text() -> str:
    """构建帮助文本"""
    return (
        "🎬 *Media Refiner 机器人*\n"
        "媒体洗版工坊的 TG 助手 (｡･ω･｡)ﾉ♡\n\n"
        "*可用指令:*\n\n"
        "`/start` / `/help` — 显示此帮助\n"
        "`/scan` — 🔍 启动质量扫描\n"
        "`/run` — ▶️ 运行所有订阅规则\n"
        "  `/run <rule_id>` — 运行指定规则\n"
        "`/rules` — 📜 列出并管理订阅规则\n"
        "`/reviews` — 📋 列出待审核项\n"
        "`/logs` — 📄 查看最近活动日志\n"
        "`/clear` — 🗑 清空所有待审核项\n"
        "`/status` — 📊 服务器运行状态\n"
        "`/reset <item_id>` — 🗑 删除指定条目的历史记录，恢复可搜索\n\n"
        "💡 新审核项会自动推送到聊天，点击下方按钮即可快速操作。"
    )
