"""
astrbot_plugin_webchat — Web 终端向 QQ 群发送/接收消息
"""

import asyncio
import json
import logging
import os
import sqlite3
import time

from quart import jsonify, request, Response

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

        # 已发现的会话索引  index -> session_key
        self.session_index: list[str] = []
        # 会话元数据  session_key -> {platform, group_id, group_name, is_group}
        self.session_meta: dict[str, dict] = {}

        max_log = int(self.config.get("max_log_per_session", 200))
        self.max_log = max_log

        # SQLite 初始化
        db_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = os.path.join(db_dir, f"{PLUGIN_NAME}.db")
        self._init_db()
        self._load_sessions_from_db()

        # SSE 推送客户端  client_id -> asyncio.Queue
        self._sse_clients: dict[str, asyncio.Queue] = {}
        self._sse_counter = 0

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
        context.register_web_api(
            f"/{PLUGIN_NAME}/stream",
            self.api_stream,
            ["GET"],
            "SSE 实时消息推送",
        )

        # 直接注册到 Quart 路由，确保 SSE 流式响应不被包装
        try:
            app = context.get_web_app() if hasattr(context, "get_web_app") else None
            if app is None:
                app = getattr(context, "quart_app", None) or getattr(context, "app", None)
            if app is not None:
                app.add_url_rule(
                    f"/{PLUGIN_NAME}/stream",
                    f"{PLUGIN_NAME}_stream",
                    self._stream_view,
                    methods=["GET"],
                )
                logger.info("SSE stream 路由已注册到 Quart")
        except Exception as e:
            logger.debug(f"Quart 路由注册跳过: {e}")

        logger.info(f"astrbot_plugin_webchat 已加载 (db: {self.db_path})")

    # ── SQLite ─────────────────────────────────────────────────────────

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    is_group INTEGER NOT NULL DEFAULT 0,
                    send_target TEXT NOT NULL DEFAULT '',
                    display_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_ts
                    ON messages(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_session_id
                    ON messages(session_id);
            """)
            conn.commit()
        finally:
            conn.close()

    def _load_sessions_from_db(self):
        conn = self._get_db()
        try:
            # 兼容旧库：补 send_target 列
            try:
                conn.execute("SELECT send_target FROM sessions LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE sessions ADD COLUMN send_target TEXT NOT NULL DEFAULT ''")
                conn.commit()

            rows = conn.execute(
                "SELECT id, platform, group_id, group_name, is_group, send_target "
                "FROM sessions ORDER BY display_order"
            ).fetchall()
            for r in rows:
                sid = r["id"]
                self.session_meta[sid] = {
                    "platform": r["platform"],
                    "group_id": r["group_id"],
                    "group_name": r["group_name"],
                    "is_group": bool(r["is_group"]),
                    "send_target": r["send_target"],
                }
                self.session_index.append(sid)
            if rows:
                logger.info(f"从数据库恢复 {len(rows)} 个会话")
        finally:
            conn.close()

    def _save_session(self, sid: str, meta: dict):
        conn = self._get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, platform, group_id, group_name, is_group, send_target, display_order) "
                "VALUES (?, ?, ?, ?, ?, ?, "
                "COALESCE((SELECT display_order FROM sessions WHERE id = ?), "
                "(SELECT COALESCE(MAX(display_order), 0) + 1 FROM sessions)))",
                (
                    sid,
                    meta.get("platform", ""),
                    meta.get("group_id", ""),
                    meta.get("group_name", ""),
                    1 if meta.get("is_group", False) else 0,
                    meta.get("send_target", ""),
                    sid,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _save_message(self, session_id: str, msg: dict):
        conn = self._get_db()
        try:
            conn.execute(
                "INSERT INTO messages (session_id, type, sender, sender_id, content, group_id, platform, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    msg["type"],
                    msg.get("sender", ""),
                    msg.get("sender_id", ""),
                    msg.get("content", ""),
                    msg.get("group_id", ""),
                    msg.get("platform", ""),
                    msg.get("timestamp", 0),
                ),
            )
            # 超出上限删最旧的
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?)",
                (session_id, session_id, self.max_log),
            )
            conn.commit()
        finally:
            conn.close()

    def _query_messages(self, session_id: str, since: float = 0, limit: int = 0) -> list[dict]:
        conn = self._get_db()
        try:
            if limit > 0:
                # 取最新 limit 条，再按时间正序返回
                rows = conn.execute(
                    "SELECT * FROM ("
                    "SELECT * FROM messages WHERE session_id = ? AND timestamp > ? "
                    "ORDER BY timestamp DESC LIMIT ?"
                    ") sub ORDER BY timestamp",
                    (session_id, since, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND timestamp > ? "
                    "ORDER BY timestamp",
                    (session_id, since),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _count_messages(self, session_id: str) -> int:
        conn = self._get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row["cnt"]
        finally:
            conn.close()

    def _delete_messages(self, session_id: str = ""):
        conn = self._get_db()
        try:
            if session_id:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            else:
                conn.execute("DELETE FROM messages")
            conn.commit()
        finally:
            conn.close()

    # ── SSE 推送 ────────────────────────────────────────────────────────

    async def _sse_broadcast(self, event_type: str, data: dict):
        """向所有 SSE 客户端广播事件。"""
        payload = json.dumps(data, ensure_ascii=False)
        dead: list[str] = []
        for cid, q in self._sse_clients.items():
            try:
                q.put_nowait((event_type, payload))
            except asyncio.QueueFull:
                dead.append(cid)
        for cid in dead:
            self._sse_clients.pop(cid, None)

    async def _stream_view(self):
        """SSE 流式端点（直接注册到 Quart 路由）。"""
        self._sse_counter += 1
        cid = f"cli-{self._sse_counter}-{id(request)}"
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._sse_clients[cid] = q
        logger.info(f"SSE 客户端连接: {cid}")

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return Response(
            self._sse_generate(cid, q),
            mimetype="text/event-stream",
            headers=headers,
        )

    async def _sse_generate(self, cid: str, q: asyncio.Queue):
        """SSE 事件生成器。"""
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    event_type, payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self._sse_clients.pop(cid, None)
            logger.info(f"SSE 客户端断开: {cid}")

    async def api_stream(self):
        """SSE 推送端点（register_web_api 回退）。"""
        return await self._stream_view()

    # ── 消息事件监听 ────────────────────────────────────────────────

    @staticmethod
    def _session_key_for_group(group_id: str) -> str:
        """用 group_id 构造稳定的群级会话键，不依赖 UMO 格式。"""
        return f"group:{group_id}"

    @staticmethod
    def _extract_group_name(message_obj, group_id: str) -> str:
        """尝试从消息对象中提取群名。"""
        try:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                name = raw.get("group_name", "") or ""
                if name:
                    return name
        except Exception:
            pass
        return group_id if group_id else "私聊"

    def _register_session(self, session_key: str, meta: dict):
        """注册一个新会话到索引和数据库。"""
        if session_key not in self.session_meta:
            self.session_meta[session_key] = meta
            self.session_index.append(session_key)
            self._save_session(session_key, meta)
            logger.info(f"发现新会话: {meta.get('group_name', session_key)}")
            # 通知前端有新会话（需要在事件循环中调度）
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._sse_broadcast("session", {
                    "id": session_key,
                    "platform": meta.get("platform", ""),
                    "group_id": meta.get("group_id", ""),
                    "group_name": meta.get("group_name", ""),
                    "is_group": meta.get("is_group", False),
                }))
            except RuntimeError:
                pass
        elif meta.get("send_target"):
            # 更新群聊的发送目标 UMO（取最新到达的）
            self.session_meta[session_key]["send_target"] = meta["send_target"]

    def _append_message(self, session_key: str, msg: dict):
        """追加消息到 SQLite。"""
        self._save_message(session_key, msg)

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

        if hasattr(event, "adapter"):
            platform = type(event.adapter).__name__

        is_group = bool(group_id)

        # 群聊 → group_id 做键，所有群成员归同一会话
        if is_group:
            session_key = self._session_key_for_group(group_id)
            group_name = self._extract_group_name(message_obj, group_id)
            display_name = group_name
        else:
            session_key = umo
            display_name = sender_name or str(sender_id) or "私聊"

        self._register_session(session_key, {
            "platform": platform,
            "group_id": group_id,
            "group_name": display_name,
            "is_group": is_group,
            "send_target": umo,
        })

        self._append_message(session_key, {
            "type": "received",
            "sender": sender_name or str(sender_id),
            "sender_id": str(sender_id),
            "content": event.message_str,
            "group_id": group_id,
            "platform": platform,
            "timestamp": time.time(),
        })

        # 推送给 SSE 客户端
        await self._sse_broadcast("message", {
            "session_id": session_key,
            "type": "received",
            "sender": sender_name or str(sender_id),
            "sender_id": str(sender_id),
            "content": event.message_str,
            "group_id": group_id,
            "platform": platform,
            "timestamp": time.time(),
        })

        return

    # ── Web API ─────────────────────────────────────────────────────

    async def api_sessions(self):
        """返回所有会话列表。?with_history=1 附带每个会话的历史消息。"""
        with_history = request.args.get("with_history", "") == "1"
        sessions = []
        for session_key in self.session_index:
            meta = self.session_meta.get(session_key, {})
            entry = {
                "id": session_key,
                "platform": meta.get("platform", ""),
                "group_id": meta.get("group_id", ""),
                "group_name": meta.get("group_name", ""),
                "is_group": meta.get("is_group", False),
                "message_count": self._count_messages(session_key),
            }
            if with_history:
                entry["messages"] = self._query_messages(session_key, limit=self.max_log)
            sessions.append(entry)
        return jsonify({"sessions": sessions})

    async def api_send(self):
        """
        向指定会话发送消息。
        请求体: {"session_id": "会话key", "message": "消息内容"}
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

        meta = self.session_meta.get(session_id)
        if not meta:
            return jsonify({"error": "会话不存在"}), 404

        # 群聊用 send_target（原始 UMO）发送，私聊直接用 session_id
        send_target = meta.get("send_target") or session_id

        try:
            chain = MessageChain().message(message)
            await self.context.send_message(send_target, chain)

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
            messages = self._query_messages(session_id, since)
            return jsonify({"messages": messages})

        # 返回所有会话的消息
        all_messages = {}
        for umo in self.session_index:
            msgs = self._query_messages(umo, since)
            if msgs:
                all_messages[umo] = msgs
        return jsonify({"messages": all_messages})

    async def api_clear(self):
        """清空指定会话或所有消息历史。"""
        data = await request.get_json() or {}
        session_id = data.get("session_id", "")

        self._delete_messages(session_id)
        if session_id:
            return jsonify({"status": "ok", "message": f"已清空会话 {session_id}"})
        else:
            return jsonify({"status": "ok", "message": "已清空所有消息历史"})