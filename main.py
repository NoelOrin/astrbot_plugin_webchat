"""
astrbot_plugin_webchat — Web 终端向 QQ 群发送/接收消息
"""

import json
import logging
import time
from collections import defaultdict

from quart import jsonify, request

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_webchat"
logger = logging.getLogger(PLUGIN_NAME)


class Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 所有会话的消息日志  key = unified_msg_origin
        self.message_logs: dict[str, list[dict]] = defaultdict(list)
        # 已发现的会话索引  index -> unified_msg_origin
        self.session_index: list[str] = []
        # 会话元数据  umo -> {platform, group_id, group_name}
        self.session_meta: dict[str, dict] = {}

        max_log = int(self.config.get("max_log_per_session", 200))
        self.max_log = max_log

        # 注册 Web API
        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions",
            self.api_sessions,
            ["GET"],
            "获取可用会话列表",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/send",
            self.api_send,
            ["POST"],
            "向指定会话发送消息",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/history",
            self.api_history,
            ["GET"],
            "获取会话消息历史",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/clear",
            self.api_clear,
            ["POST"],
            "清空消息历史",
        )

        logger.info("astrbot_plugin_webchat 已加载")

    # ── 消息事件监听 ────────────────────────────────────────────────

    def _register_session(self, umo: str, meta: dict):
        """注册一个新会话到索引。"""
        if umo not in self.session_meta:
            self.session_meta[umo] = meta
            self.session_index.append(umo)
            logger.info(f"发现新会话: {meta.get('group_name', umo)}")

    def _append_message(self, umo: str, msg: dict):
        """追加消息到日志，超出上限自动裁剪。"""
        self.message_logs[umo].append(msg)
        if len(self.message_logs[umo]) > self.max_log:
            self.message_logs[umo] = self.message_logs[umo][-self.max_log:]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """捕获所有消息，记录到终端日志。"""
        umo = event.unified_msg_origin
        message_obj = event.message_obj
        group_id = getattr(message_obj, "group_id", "")
        sender = getattr(message_obj, "sender", None)
        sender_name = getattr(sender, "nickname", "") if sender else ""
        sender_id = getattr(sender, "user_id", "") if sender else ""
        platform = type(event).__name__

        # 尝试从适配器名获取平台
        if hasattr(event, "adapter"):
            platform = type(event.adapter).__name__

        # 注册会话
        self._register_session(umo, {
            "platform": platform,
            "group_id": group_id,
            "group_name": group_id or "私聊",
        })

        # 追加消息
        self._append_message(umo, {
            "type": "received",
            "sender": sender_name or str(sender_id),
            "sender_id": str(sender_id),
            "content": event.message_str,
            "group_id": group_id,
            "platform": platform,
            "timestamp": time.time(),
        })

        # 不阻止事件传播，让 AstrBot 正常处理（LLM 等）
        return

    # ── Web API ─────────────────────────────────────────────────────

    async def api_sessions(self):
        """返回所有已发现的会话列表。"""
        sessions = []
        for umo in self.session_index:
            meta = self.session_meta.get(umo, {})
            sessions.append({
                "id": umo,
                "platform": meta.get("platform", ""),
                "group_id": meta.get("group_id", ""),
                "group_name": meta.get("group_name", ""),
                "message_count": len(self.message_logs.get(umo, [])),
            })
        return jsonify({"sessions": sessions})

    async def api_send(self):
        """
        向指定会话发送消息。
        请求体: {"session_id": "umo字符串", "message": "消息内容"}
        """
        data = await request.get_json()
        if not data:
            return jsonify({"error": "无效的请求体"}), 400

        session_id = data.get("session_id", "").strip()
        message = data.get("message", "").strip()

        if not session_id:
            return jsonify({"error": "请选择会话"}), 400
        if not message:
            return jsonify({"error": "消息不能为空"}), 400

        # 验证会话存在
        if session_id not in self.session_meta:
            return jsonify({"error": "会话不存在"}), 404

        try:
            chain = MessageChain().message(message)
            await self.context.send_message(session_id, chain)

            # 记录到本地日志
            self._append_message(session_id, {
                "type": "sent",
                "sender": "Terminal",
                "content": message,
                "timestamp": time.time(),
            })

            return jsonify({"status": "ok", "message": "消息已发送"})
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return jsonify({"error": f"发送失败: {e}"}), 500

    async def api_history(self):
        """
        获取指定会话的消息历史。
        ?session_id=xxx  不传则返回所有会话。
        ?since=timestamp  可选，只返回该时间戳之后的消息。
        """
        session_id = request.args.get("session_id", "")
        since = float(request.args.get("since", "0"))

        if session_id:
            messages = self.message_logs.get(session_id, [])
            if since:
                messages = [m for m in messages if m.get("timestamp", 0) > since]
            return jsonify({"messages": messages})

        # 返回所有会话
        all_messages = {}
        for umo, msgs in self.message_logs.items():
            if since:
                msgs = [m for m in msgs if m.get("timestamp", 0) > since]
            if msgs:
                all_messages[umo] = msgs
        return jsonify({"messages": all_messages})

    async def api_clear(self):
        """清空指定会话或所有消息历史。"""
        data = await request.get_json() or {}
        session_id = data.get("session_id", "")

        if session_id:
            self.message_logs[session_id] = []
            return jsonify({"status": "ok", "message": f"已清空会话 {session_id}"})
        else:
            self.message_logs.clear()
            return jsonify({"status": "ok", "message": "已清空所有消息历史"})
