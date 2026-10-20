"""
astrbot_plugin_webchat — Web 终端向 QQ 群/私聊发送和接收消息

设计要点：
  * 收发双向：入站走 ``on_message``，出站（Bot / 插件主动推送）走 ``after_message_sent``
  * 富媒体：消息链按片段（segments）序列化存储，前端逐段渲染
  * 存储：``data/plugin_data/<plugin>/`` 下，WAL 模式，按 id 裁剪与游标分页口径一致
  * 实时：SSE 优先，前端可走 bridge 或原生 EventSource，失败降级轮询
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from quart import Response, jsonify, request

try:  # 正常情况：以 data.plugins.<dir>.main 包路径加载
    from .webchat_core import (
        DEFAULT_MAX_LOG,
        MessageStore,
        clamp_int,
        coerce_bool,
        coerce_float,
        has_mention_markup,
        is_http_url,
        now_ts,
        serialize_chain,
        session_key_for_group,
        session_key_for_private,
        text_to_segments,
    )
except ImportError:  # 兜底：宿主以裸模块名加载时走同目录导入
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from webchat_core import (  # type: ignore[no-redef]
        DEFAULT_MAX_LOG,
        MessageStore,
        clamp_int,
        coerce_bool,
        coerce_float,
        has_mention_markup,
        is_http_url,
        now_ts,
        serialize_chain,
        session_key_for_group,
        session_key_for_private,
        text_to_segments,
    )

PLUGIN_NAME = "astrbot_plugin_webchat"
LEGACY_DB_NAME = f"{PLUGIN_NAME}.db"

logger = logging.getLogger(PLUGIN_NAME)


def _resolve_data_dir() -> str:
    """数据目录：优先 AstrBot 规范路径，失败时退化到插件目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(base, exist_ok=True)
    return base


def _optional_hook(name: str):
    """取一个可选的 filter 钩子；宿主没有该 API 时退化成空装饰器。

    装饰器在类体执行（也就是 import 期）就会调用 filter，
    老版本 AstrBot 缺少 on_decorating_result 时不能让整个插件崩掉。
    """
    hook = getattr(filter, name, None)
    if callable(hook):
        return hook()
    logger.warning(f"当前 AstrBot 版本不支持 filter.{name}，该功能已跳过")

    def decorator(fn):
        return fn

    return decorator


class Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self.data_dir = _resolve_data_dir()
        self.db_path = os.path.join(self.data_dir, LEGACY_DB_NAME)
        self._migrate_legacy_db()

        max_log = clamp_int(
            self.config.get("max_log_per_session", DEFAULT_MAX_LOG),
            1,
            100_000,
            DEFAULT_MAX_LOG,
        )
        self.max_log = max_log
        self.store = MessageStore(self.db_path, max_log=max_log)
        self.store.init_schema()

        # 会话索引：保持稳定顺序，供前端展示
        self.session_index: list[str] = []
        self.session_meta: dict[str, dict] = {}
        self._load_sessions()

        # SSE 客户端
        self._sse_clients: dict[str, asyncio.Queue] = {}
        self._sse_counter = 0
        # 终端发出消息的短时指纹，用于抑制 after_message_sent 的重复入库
        self._recent_sends: dict[str, tuple[str, float]] = {}

        self._register_web_apis()

        logger.info(f"{PLUGIN_NAME} 已加载 (db: {self.db_path}, max_log: {self.max_log})")

    # ── 初始化辅助 ──────────────────────────────────────────────

    def _migrate_legacy_db(self) -> None:
        """把旧版插件目录下的数据库搬到规范数据目录，避免升级丢历史。"""
        legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", LEGACY_DB_NAME)
        if os.path.abspath(legacy) == os.path.abspath(self.db_path) or not os.path.exists(legacy):
            return
        if os.path.exists(self.db_path):
            return
        try:
            shutil.copy2(legacy, self.db_path)
            logger.info(f"已迁移旧数据库: {legacy} -> {self.db_path}")
        except OSError as exc:
            logger.warning(f"旧数据库迁移失败，将使用新库: {exc}")

    def _load_sessions(self) -> None:
        self.session_index = []
        self.session_meta = {}
        for meta in self.store.load_sessions():
            session_id = meta["id"]
            self.session_meta[session_id] = meta
            self.session_index.append(session_id)
        if self.session_index:
            logger.info(f"从数据库恢复 {len(self.session_index)} 个会话")

    def _register_web_apis(self) -> None:
        routes = [
            ("sessions", self.api_sessions, ["GET"], "会话列表（可带历史）"),
            ("send", self.api_send, ["POST"], "向指定会话发送消息"),
            ("history", self.api_history, ["GET"], "会话消息历史 / 游标分页"),
            ("clear", self.api_clear, ["POST"], "清空消息历史"),
            ("stream", self.api_stream, ["GET"], "SSE 实时消息推送"),
            ("search", self.api_search, ["GET"], "消息全文检索"),
            # 老版 bridge 没有通用请求方法，额外接受 POST + ?method=DELETE/PATCH
            (
                "messages/<int:message_id>",
                self.api_delete_message,
                ["DELETE", "POST"],
                "删除单条消息",
            ),
            (
                "sessions/<path:session_id>",
                self.api_session_update,
                ["PATCH", "DELETE", "POST"],
                "会话置顶/改名/删除",
            ),
            ("platforms", self.api_platforms, ["GET"], "可用平台与适配器"),
            ("peers", self.api_peers, ["GET"], "拉取群列表/好友列表"),
            ("open", self.api_open_session, ["POST"], "主动创建会话"),
            ("media", self.api_media, ["GET"], "图片代理"),
            ("export", self.api_export, ["GET"], "导出会话记录"),
            ("health", self.api_health, ["GET"], "运行状态"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except Exception as exc:  # 老版本 AstrBot 可能不支持某方法
                logger.warning(f"注册路由失败 {path} ({methods}): {exc}")

    # ── SSE ────────────────────────────────────────────────────

    async def _sse_broadcast(self, event_type: str, data: dict) -> None:
        if not self._sse_clients:
            return
        payload = json.dumps(data, ensure_ascii=False, default=str)
        dead: list[str] = []
        for cid, queue in list(self._sse_clients.items()):
            try:
                queue.put_nowait((event_type, payload))
            except asyncio.QueueFull:
                dead.append(cid)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._sse_clients.pop(cid, None)

    def _broadcast_soon(self, event_type: str, data: dict) -> None:
        """在可能没有事件循环的上下文里安全地广播。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._sse_broadcast(event_type, data))

    async def _sse_generate(self, cid: str, queue: asyncio.Queue):
        try:
            yield "event: connected\ndata: {}\n\n"
            hello = json.dumps({"clients": len(self._sse_clients)}, ensure_ascii=False)
            yield f"event: hello\ndata: {hello}\n\n"
            while True:
                try:
                    event_type, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            self._sse_clients.pop(cid, None)
            logger.debug(f"SSE 客户端断开: {cid}")

    async def api_stream(self):
        self._sse_counter += 1
        cid = f"cli-{self._sse_counter}-{id(request)}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._sse_clients[cid] = queue
        logger.info(f"SSE 客户端连接: {cid}")
        return Response(
            self._sse_generate(cid, queue),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── 事件：入站 ──────────────────────────────────────────────

    @staticmethod
    def _event_platform(event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (平台适配器名, 平台实例 id)。取不到时回退类名。"""
        name = ""
        platform_id = ""
        try:
            name = str(event.get_platform_name() or "")
        except Exception:
            name = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        if not name:
            adapter = getattr(event, "adapter", None)
            name = type(adapter).__name__ if adapter is not None else type(event).__name__
        return name, platform_id

    @staticmethod
    def _extract_group_name(message_obj, group_id: str) -> str:
        try:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                group = raw.get("group") or {}
                candidates = [
                    getattr(message_obj, "group_name", ""),
                    raw.get("group_name"),
                    group.get("group_name") if isinstance(group, dict) else "",
                    group.get("name") if isinstance(group, dict) else "",
                ]
                for candidate in candidates:
                    if candidate:
                        return str(candidate)
        except Exception:
            pass
        return group_id or "私聊"

    def _group_id_of(self, event: AstrMessageEvent) -> str:
        message_obj = event.message_obj
        group_id = getattr(message_obj, "group_id", "") or ""
        if not group_id:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                group_id = raw.get("group_id", "") or ""
        return str(group_id)

    def _ensure_session(self, session_id: str, meta: dict) -> bool:
        """注册/更新会话，返回是否为新会话。"""
        is_new = session_id not in self.session_meta
        if is_new:
            self.session_meta[session_id] = meta
            self.session_index.append(session_id)
            self.store.upsert_session(session_id, meta)
            logger.info(f"发现新会话: {meta.get('peer_name') or session_id}")
            self._broadcast_soon("session", self._session_payload(session_id))
            return True

        current = self.session_meta[session_id]
        changed = False
        for key, value in meta.items():
            if value not in (None, "", False) and current.get(key) != value:
                current[key] = value
                changed = True
        if changed:
            self.store.upsert_session(session_id, current)
        return False

    def _session_payload(self, session_id: str) -> dict:
        meta = self.session_meta.get(session_id, {})
        return {
            "id": session_id,
            "platform": meta.get("platform", ""),
            "platform_id": meta.get("platform_id", ""),
            "group_id": meta.get("group_id", ""),
            "group_name": meta.get("group_name", ""),
            "is_group": bool(meta.get("is_group")),
            "peer_id": meta.get("peer_id") or meta.get("group_id") or session_id,
            "peer_name": meta.get("peer_name") or meta.get("group_name") or "",
            "pinned": bool(meta.get("pinned")),
        }

    async def on_message(self, event: AstrMessageEvent):
        """记录所有入站消息。"""
        try:
            self._record_inbound(event)
        except Exception as exc:
            logger.exception(f"记录入站消息失败: {exc}")

    def _record_inbound(self, event: AstrMessageEvent) -> dict | None:
        message_obj = event.message_obj
        group_id = self._group_id_of(event)
        is_group = bool(group_id)
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        sender = getattr(message_obj, "sender", None)
        sender_name = str(getattr(sender, "nickname", "") or "") if sender else ""
        sender_id = str(getattr(sender, "user_id", "") or "") if sender else ""
        if not sender_name or not sender_id:
            try:
                sender_name = sender_name or str(event.get_sender_name() or "")
                sender_id = sender_id or str(event.get_sender_id() or "")
            except Exception:
                pass

        platform, platform_id = self._event_platform(event)

        if is_group:
            session_key = session_key_for_group(group_id)
            display_name = self._extract_group_name(message_obj, group_id)
        else:
            session_key = session_key_for_private(umo, sender_id)
            display_name = sender_name or sender_id or "私聊"

        if not session_key:
            return None

        self._ensure_session(
            session_key,
            {
                "platform": platform,
                "platform_id": platform_id,
                "group_id": group_id,
                "group_name": display_name,
                "is_group": is_group,
                "send_target": umo,
                "peer_id": str(group_id or sender_id or ""),
                "peer_name": display_name,
            },
        )

        segments = serialize_chain(getattr(message_obj, "message", None))
        saved = self.store.add_message(
            session_key,
            {
                "type": "received",
                "sender": sender_name or sender_id,
                "sender_id": sender_id,
                "content": str(getattr(event, "message_str", "") or ""),
                "segments": segments,
                "group_id": group_id,
                "platform": platform,
                "platform_id": platform_id,
                "platform_message_id": str(getattr(message_obj, "message_id", "") or ""),
                "timestamp": coerce_float(getattr(message_obj, "timestamp", 0), now_ts())
                or now_ts(),
            },
        )
        self._broadcast_soon("message", saved)
        return saved

    # ── 事件：出站（Bot / 插件主动推送） ──────────────────────────

    @_optional_hook("on_decorating_result")
    async def on_before_send(self, event: AstrMessageEvent):
        """发送前把 <mention> 标签 / 手打 @ 改写成真实 At 组件。

        这是 <mention> 问题的根治点：AstrBot 核心不认这类占位标签，
        LLM 写出 <mention id="123"/> 时会原样发到平台，用户看到字面标签、
        被 @ 的人也收不到提醒。在消息真正发出前改写，平台才会收到 At 段。
        """
        if not coerce_bool(self.config.get("convert_mention_tags", True), True):
            return
        try:
            self._rewrite_mentions(event)
        except Exception as exc:
            logger.debug(f"改写 @ 标记失败（忽略）: {exc}")

    def _rewrite_mentions(self, event: AstrMessageEvent) -> bool:
        """就地重写 event 结果链：文本里的 @ 标记 → At，标签绝不外泄。"""
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return False

        from astrbot.api.message_components import At, Plain

        resolve = self.store.mention_resolver(self._session_key_of(event))
        allow_bare = coerce_bool(self.config.get("parse_bare_at", True), True)

        rewritten: list = []
        changed = False
        for component in chain:
            text = getattr(component, "text", None)
            if not isinstance(component, Plain) or not isinstance(text, str) or not text:
                rewritten.append(component)
                continue
            if not (has_mention_markup(text) or (allow_bare and "@" in text)):
                rewritten.append(component)
                continue
            segments = text_to_segments(text, resolve, allow_bare_at=allow_bare)
            if not has_mention_markup(text) and not any(
                segment.get("type") == "at" for segment in segments
            ):
                rewritten.append(component)  # 没有真的解析出 @，原样保留
                continue
            for segment in segments:
                target = str(segment.get("target") or "")
                if segment.get("type") == "at" and target:
                    rewritten.append(At(qq=target, name=str(segment.get("text") or "")))
                else:
                    rewritten.append(Plain(str(segment.get("text") or "")))
            changed = True

        if changed:
            # AstrBot 的 respond 阶段会跳过「只有 Reply/At」的消息链，
            # 改写后若只剩 At，这条消息就会被静默丢弃；
            # 补一个零宽空格明文段，保证 @ 真的发得出去。
            if rewritten and all(isinstance(item, At) for item in rewritten):
                rewritten.append(Plain("\u200b"))
            chain[:] = rewritten
        return changed

    def _session_key_of(self, event: AstrMessageEvent) -> str:
        """事件 → 本插件的会话键，用于把昵称解析到正确的群成员。"""
        group_id = self._group_id_of(event)
        if group_id:
            return session_key_for_group(group_id)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        self_id = ""
        with contextlib.suppress(Exception):
            self_id = str(event.get_self_id() or "")
        return session_key_for_private(umo, self_id)

    @filter.after_message_sent()
    async def on_bot_message_sent(self, event: AstrMessageEvent):
        """Bot 回复、指令回复、其他插件主动推送在发送后入库，补齐对话另一半。"""
        if not coerce_bool(self.config.get("capture_bot_messages", True), True):
            return
        try:
            # 兜底：流式输出等场景不会经过 on_decorating_result，
            # 这里再改写一次，至少保证终端里存下来的是 @ 而不是字面标签。
            if coerce_bool(self.config.get("convert_mention_tags", True), True):
                with contextlib.suppress(Exception):
                    self._rewrite_mentions(event)
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            segments = serialize_chain(chain)
            if not segments:
                return
            text = "".join(
                str(segment.get("text") or "")
                for segment in segments
                if segment.get("type") == "text"
            ).strip()

            group_id = self._group_id_of(event)
            is_group = bool(group_id)
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            platform, platform_id = self._event_platform(event)
            self_id = ""
            with contextlib.suppress(Exception):
                self_id = str(event.get_self_id() or "")

            if is_group:
                session_key = session_key_for_group(group_id)
            else:
                session_key = session_key_for_private(umo, self_id)
            if not session_key or session_key not in self.session_meta:
                # 没有对应会话说明不是本插件跟踪的对话，忽略
                return
            if self._is_echo_of_terminal_send(
                session_key, self._send_fingerprint(segments)
            ):
                return

            saved = self.store.add_message(
                session_key,
                {
                    "type": "bot",
                    "sender": str(self.config.get("bot_display_name", "") or "Bot"),
                    "sender_id": self_id,
                    "content": text,
                    "segments": segments,
                    "group_id": group_id,
                    "platform": platform,
                    "platform_id": platform_id,
                    "timestamp": now_ts(),
                },
            )
            await self._sse_broadcast("message", saved)
        except Exception as exc:
            logger.debug(f"记录出站消息失败（忽略）: {exc}")

    @staticmethod
    def _send_fingerprint(segments: list[dict]) -> str:
        """发送指纹：把 @ 片段折叠成 @名，抹平「文本 vs At 段」的表示差异。

        终端发出的文本会被拆成 At + 文本，出站钩子再折回纯文本，
        两边表示不同但语义相同，必须归一化后才能可靠去重。
        """
        parts: list[str] = []
        for segment in segments:
            kind = segment.get("type")
            if kind == "at":
                parts.append(f"@{segment.get('text') or segment.get('target') or ''}")
            elif kind == "text":
                parts.append(str(segment.get("text") or ""))
            elif kind == "image":
                parts.append("[图片]")
            else:
                parts.append(str(segment.get("text") or ""))
        return "".join("".join(parts).split())

    def _is_echo_of_terminal_send(self, session_key: str, fingerprint: str) -> bool:
        """终端刚发出的消息可能再次经过出站钩子，按内容+时间去重。"""
        record = self._recent_sends.get(session_key)
        if not record:
            return False
        recorded, stamp = record
        if time.time() - stamp > 8:
            self._recent_sends.pop(session_key, None)
            return False
        return recorded == fingerprint

    # ── API：会话 ──────────────────────────────────────────────

    async def api_sessions(self):
        """会话列表。?with_history=1 附带每个会话最新一页消息。"""
        with_history = coerce_bool(request.args.get("with_history", ""))
        history_limit = clamp_int(request.args.get("history_limit", 40), 1, 200, 40)
        keyword = str(request.args.get("q", "") or "").strip()

        summaries = self.store.session_summaries()
        participants_map = self.store.query_participants()

        sessions = []
        for session_key in self.session_index:
            meta = self.session_meta.get(session_key, {})
            summary = summaries.get(session_key, {})
            peer_id = meta.get("peer_id") or meta.get("group_id") or session_key
            peer_name = meta.get("peer_name") or meta.get("group_name") or ""
            if keyword:
                haystack = " ".join([peer_name, str(peer_id), meta.get("platform", "")])
                if keyword.lower() not in haystack.lower():
                    continue
            entry = {
                "id": session_key,
                "platform": meta.get("platform", ""),
                "platform_id": meta.get("platform_id", ""),
                "group_id": meta.get("group_id", ""),
                "group_name": meta.get("group_name", ""),
                "is_group": bool(meta.get("is_group")),
                "peer_id": str(peer_id),
                "peer_name": peer_name or str(peer_id),
                "pinned": bool(meta.get("pinned")),
                "message_count": summary.get("message_count", 0),
                "last_activity": summary.get("last_activity", 0),
                "participants": participants_map.get(session_key, []),
                "last_message": summary.get("last_message"),
            }
            if with_history:
                page, has_more, next_cursor = self.store.query_page(
                    session_key, limit=history_limit
                )
                entry["messages"] = page
                entry["history"] = {
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "limit": history_limit,
                }
            sessions.append(entry)

        sessions.sort(key=lambda item: (not item["pinned"], -(item["last_activity"] or 0)))
        return jsonify({"sessions": sessions})

    @staticmethod
    def _effective_method() -> str:
        """兼容 POST + ?method=DELETE/PATCH 的方法覆盖写法。"""
        override = str(request.args.get("method", "") or "").upper()
        if request.method == "POST" and override in ("DELETE", "PATCH"):
            return override
        return str(request.method or "").upper()

    async def api_session_update(self, session_id: str):
        """PATCH: 置顶/改名；DELETE: 删除会话。"""
        if session_id not in self.session_meta:
            return jsonify({"error": "会话不存在"}), 404

        if self._effective_method() == "DELETE":
            self.store.delete_session(session_id)
            self.session_meta.pop(session_id, None)
            if session_id in self.session_index:
                self.session_index.remove(session_id)
            self._recent_sends.pop(session_id, None)
            await self._sse_broadcast("session_removed", {"id": session_id})
            return jsonify({"status": "ok"})

        data = await request.get_json(silent=True) or {}
        if "pinned" in data:
            pinned = coerce_bool(data.get("pinned"))
            self.store.set_pinned(session_id, pinned)
            self.session_meta[session_id]["pinned"] = pinned
        if "peer_name" in data:
            name = str(data.get("peer_name") or "").strip()
            if name:
                self.store.rename_session(session_id, name)
                self.session_meta[session_id]["peer_name"] = name
        payload = self._session_payload(session_id)
        payload["pinned"] = bool(self.session_meta[session_id].get("pinned"))
        await self._sse_broadcast("session_updated", payload)
        return jsonify({"status": "ok", "session": payload})

    # ── API：消息 ──────────────────────────────────────────────

    async def api_send(self):
        """发送消息。body: {session_id, message, segments?}"""
        data = await request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or "").strip()
        message = str(data.get("message") or "").strip()
        extra_segments = data.get("segments")

        if not session_id:
            return jsonify({"error": "请选择会话"}), 400
        if not message and not extra_segments:
            return jsonify({"error": "消息不能为空"}), 400

        meta = self.session_meta.get(session_id)
        if not meta:
            return jsonify({"error": "会话不存在"}), 404

        send_target = meta.get("send_target") or session_id
        segments: list[dict] = []
        if message:
            # 手打的 @昵称 / @QQ号 还原成真实 At，否则在 QQ 里只是文本
            segments.extend(
                text_to_segments(
                    message,
                    self.store.mention_resolver(session_id),
                    allow_bare_at=coerce_bool(self.config.get("parse_bare_at", True), True),
                )
            )
        if isinstance(extra_segments, list):
            for segment in extra_segments:
                if isinstance(segment, dict) and segment.get("type") in ("image", "at"):
                    segments.append(segment)

        try:
            chain = self._build_chain(segments)
            self._recent_sends[session_id] = (
                self._send_fingerprint(segments),
                time.time(),
            )
            await self.context.send_message(send_target, chain)
        except Exception as exc:
            self._recent_sends.pop(session_id, None)
            logger.error(f"发送消息失败: {exc}")
            return jsonify({"error": f"发送失败: {exc}"}), 500

        saved = self.store.add_message(
            session_id,
            {
                "type": "sent",
                "sender": "Terminal",
                "sender_id": "",
                "content": message,
                "segments": segments,
                "platform": meta.get("platform", ""),
                "platform_id": meta.get("platform_id", ""),
                "group_id": meta.get("group_id", ""),
                "timestamp": now_ts(),
            },
        )
        # 其他标签页也要看到自己发出的消息
        await self._sse_broadcast("message", saved)
        return jsonify({"status": "ok", "data": saved})

    @staticmethod
    def _build_chain(segments: list[dict]):
        """把片段还原成 AstrBot 消息链，图片按可访问地址尽力还原。"""
        from astrbot.api.message_components import Image

        chain = MessageChain()
        for segment in segments:
            kind = segment.get("type")
            if kind == "text" and segment.get("text"):
                chain.message(str(segment["text"]))
            elif kind == "image":
                source = str(segment.get("url") or segment.get("file") or "")
                if not source:
                    continue
                try:
                    if is_http_url(source):
                        chain.chain.append(Image.fromURL(source))
                    else:
                        chain.chain.append(Image.fromFileSystem(source))
                except Exception as exc:
                    logger.warning(f"图片还原失败: {exc}")
            elif kind == "at":
                try:
                    from astrbot.api.message_components import At

                    target = str(segment.get("target") or "")
                    if target:
                        # 带上昵称：出站钩子折回文本时才能与原文对上，
                        # 也便于平台侧展示「@小明」而不是「@123456」
                        chain.chain.append(At(qq=target, name=str(segment.get("text") or "")))
                except Exception as exc:
                    logger.warning(f"@ 还原失败: {exc}")
            elif kind == "plain" and segment.get("text"):
                chain.message(str(segment["text"]))
        return chain

    async def api_history(self):
        """?session_id= 时返回消息；带 limit/before_id 走游标分页。"""
        session_id = str(request.args.get("session_id", "") or "")
        try:
            since = coerce_float(request.args.get("since", 0))
        except Exception:
            since = 0.0
        paged = "limit" in request.args or "before_id" in request.args
        limit = clamp_int(request.args.get("limit", 50), 1, 200, 50)
        before_id = clamp_int(request.args.get("before_id", 0), 0, 2**62, 0)

        if session_id:
            if paged:
                messages, has_more, next_cursor = self.store.query_page(
                    session_id, limit=limit, before_id=before_id
                )
                return jsonify(
                    {"messages": messages, "has_more": has_more, "next_cursor": next_cursor}
                )
            messages = self.store.query_messages(session_id, since)
            return jsonify({"messages": messages, "has_more": False, "next_cursor": None})

        all_messages: dict[str, list[dict]] = {}
        for session_key in self.session_index:
            messages = self.store.query_messages(session_key, since)
            if messages:
                all_messages[session_key] = messages
        return jsonify({"messages": all_messages})

    async def api_search(self):
        """?q=关键词 &session_id=（可选）&limit="""
        keyword = str(request.args.get("q", "") or "").strip()
        if not keyword:
            return jsonify({"error": "缺少查询关键词"}), 400
        session_id = str(request.args.get("session_id", "") or "")
        limit = clamp_int(request.args.get("limit", 50), 1, 200, 50)
        results = self.store.search_messages(keyword, session_id=session_id, limit=limit)
        # 附带会话展示名，方便前端直接渲染结果
        for item in results:
            meta = self.session_meta.get(item["session_id"], {})
            item["session_peer_name"] = meta.get("peer_name") or item["session_id"]
            item["session_is_group"] = bool(meta.get("is_group"))
        return jsonify({"query": keyword, "results": results, "count": len(results)})

    async def api_delete_message(self, message_id: int):
        """删除单条消息（本地记录；可尝试同步撤回平台消息）。"""
        message = self.store.delete_message(int(message_id))
        if message is None:
            return jsonify({"error": "消息不存在"}), 404
        if coerce_bool(request.args.get("remote", "")):
            await self._try_recall(message)
        await self._sse_broadcast(
            "message_removed", {"id": message["id"], "session_id": message["session_id"]}
        )
        return jsonify({"status": "ok", "data": message})

    async def _try_recall(self, message: dict) -> bool:
        """尽力调用协议端撤回；失败只记日志，不影响本地删除。"""
        platform_message_id = str(message.get("platform_message_id") or "")
        platform_id = str(message.get("platform_id") or "")
        if not platform_message_id or not platform_id:
            return False
        try:
            platform = self.context.get_platform_inst(platform_id)
            client = platform.get_client()
            await client.api.call_action(
                "delete_msg", message_id=platform_message_id
            )
            return True
        except Exception as exc:
            logger.info(f"平台撤回失败（已忽略）: {exc}")
            return False

    async def api_clear(self):
        """清空历史。body: {session_id?} — 不带 session_id 时要求 confirm_all=true。"""
        data = await request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or "").strip()

        if not session_id:
            if not coerce_bool(data.get("confirm_all")):
                return jsonify(
                    {"error": "清空全部记录需要显式确认（confirm_all=true）"}
                ), 400
            removed = self.store.delete_all_messages()
            await self._sse_broadcast("cleared", {"session_id": ""})
            return jsonify({"status": "ok", "removed": removed})

        if session_id not in self.session_meta:
            return jsonify({"error": "会话不存在"}), 404
        removed = self.store.delete_session_messages(session_id)
        await self._sse_broadcast("cleared", {"session_id": session_id})
        return jsonify({"status": "ok", "removed": removed})

    # ── API：主动建会话 ────────────────────────────────────────

    async def api_platforms(self):
        """列出可用平台实例，供“新建会话”选择。"""
        platforms: list[dict] = []
        try:
            for platform in self.context.platform_manager.get_insts():
                meta = getattr(platform, "meta", None)
                platforms.append(
                    {
                        "id": str(getattr(meta, "id", "") or ""),
                        "name": str(getattr(meta, "name", "") or ""),
                        "adapter": str(getattr(meta, "type", "") or ""),
                    }
                )
        except Exception as exc:
            logger.warning(f"读取平台列表失败: {exc}")
        return jsonify({"platforms": platforms})

    async def api_peers(self):
        """拉取群列表/好友列表（依赖协议端支持，失败返回空列表）。"""
        platform_id = str(request.args.get("platform_id", "") or "")
        kind = str(request.args.get("type", "group") or "group")
        peers: list[dict] = []
        if platform_id:
            action = "get_group_list" if kind == "group" else "get_friend_list"
            try:
                platform = self.context.get_platform_inst(platform_id)
                client = platform.get_client()
                raw = await client.api.call_action(action)
                items = raw if isinstance(raw, list) else raw.get("data") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    peer_id = item.get("group_id") or item.get("user_id") or ""
                    name = item.get("group_name") or item.get("nickname") or str(peer_id)
                    if peer_id:
                        peers.append({"id": str(peer_id), "name": str(name)})
            except Exception as exc:
                logger.info(f"拉取 {action} 失败（已忽略）: {exc}")
        return jsonify({"peers": peers, "type": kind})

    async def api_open_session(self):
        """主动创建会话：body: {platform_id, peer_id, type: group|friend, name?}"""
        data = await request.get_json(silent=True) or {}
        platform_id = str(data.get("platform_id") or "").strip()
        peer_id = str(data.get("peer_id") or "").strip()
        kind = str(data.get("type") or "group").strip()
        name = str(data.get("name") or "").strip()

        if not platform_id or not peer_id:
            return jsonify({"error": "缺少 platform_id 或 peer_id"}), 400
        if not peer_id.isdigit():
            return jsonify({"error": "会话 ID 必须是纯数字"}), 400

        is_group = kind == "group"
        session_key = (
            session_key_for_group(peer_id) if is_group else session_key_for_private("", peer_id)
        )
        if session_key in self.session_meta:
            return jsonify({"status": "exists", "session": self._session_payload(session_key)})

        send_target = self._infer_umo(platform_id, is_group, peer_id)
        meta = {
            "platform": platform_id,
            "platform_id": platform_id,
            "group_id": peer_id if is_group else "",
            "group_name": name or (peer_id if is_group else ""),
            "is_group": is_group,
            "send_target": send_target,
            "peer_id": peer_id,
            "peer_name": name or peer_id,
            "created_at": now_ts(),
        }
        self._ensure_session(session_key, meta)
        return jsonify({"status": "ok", "session": self._session_payload(session_key)})

    def _infer_umo(self, platform_id: str, is_group: bool, peer_id: str) -> str:
        """从同平台已有会话推断 UMO 模板，把最后一段替换成目标 id。"""
        for meta in self.session_meta.values():
            if str(meta.get("platform_id") or "") != platform_id:
                continue
            if bool(meta.get("is_group")) != is_group:
                continue
            template = str(meta.get("send_target") or "")
            if not template:
                continue
            head, _, tail = template.rpartition(":")
            if head and tail:
                return f"{head}:{peer_id}"
        kind = "GroupMessage" if is_group else "FriendMessage"
        return f"{platform_id}:{kind}:{peer_id}"

    # ── API：媒体代理 ──────────────────────────────────────────

    async def api_media(self):
        """代理远程图片，规避防盗链与混合内容问题。?url=。"""
        url = str(request.args.get("url", "") or "")
        if not is_http_url(url):
            return jsonify({"error": "仅支持 http(s) 图片地址"}), 400
        try:
            import aiohttp

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session, session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return jsonify({"error": f"上游返回 {resp.status}"}), 502
                content_type = resp.headers.get("Content-Type", "image/png")
                if not content_type.startswith("image/"):
                    return jsonify({"error": "目标不是图片"}), 415
                body = await resp.read()
                if len(body) > 8 * 1024 * 1024:
                    return jsonify({"error": "图片过大"}), 413
                return Response(
                    body,
                    mimetype=content_type,
                    headers={"Cache-Control": "public, max-age=3600"},
                )
        except Exception as exc:
            logger.info(f"媒体代理失败: {exc}")
            return jsonify({"error": "图片获取失败"}), 502

    # ── API：导出与健康检查 ────────────────────────────────────

    async def api_export(self):
        session_id = str(request.args.get("session_id", "") or "")
        fmt = str(request.args.get("format", "json") or "json")
        if not session_id or session_id not in self.session_meta:
            return jsonify({"error": "会话不存在"}), 404
        payload = self.store.export_session(session_id, fmt="txt" if fmt == "txt" else "json")
        return jsonify(payload)

    async def api_health(self):
        return jsonify(
            {
                "plugin": PLUGIN_NAME,
                "version": "0.4.0",
                "db": self.db_path,
                "sessions": len(self.session_index),
                "sse_clients": len(self._sse_clients),
                "max_log_per_session": self.max_log,
                "capture_bot_messages": coerce_bool(
                    self.config.get("capture_bot_messages", True), True
                ),
            }
        )

    # ── 生命周期 ──────────────────────────────────────────────

    async def terminate(self):
        """插件卸载/重载时清理 SSE 与缓存。"""
        self._sse_clients.clear()
        self._recent_sends.clear()
        logger.info(f"{PLUGIN_NAME} 已卸载")
