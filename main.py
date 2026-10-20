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
import re
import shutil
import time

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from quart import Response, jsonify, request

try:  # 正常情况：以 data.plugins.<dir>.main 包路径加载
    from .webchat_core import (
        DEFAULT_MAX_LOG,
        MAX_PAGE_SIZE,
        MessageStore,
        astrbot_history_to_message,
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
        MAX_PAGE_SIZE,
        MessageStore,
        astrbot_history_to_message,
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


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号，避免和 api_health 里的硬编码漂移。

    运行时零外部依赖，所以不引入 yaml——版本号格式固定，正则足够。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                matched = re.match(r"""^\s*version\s*:\s*["']?([^"'\s#]+)""", line)
                if matched:
                    return matched.group(1)
    except OSError as exc:
        logger.debug(f"读取 metadata.yaml 失败（忽略）: {exc}")
    return "unknown"


PLUGIN_VERSION = _read_plugin_version()


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


def _message_listener():
    """注册「所有入站消息」监听器。

    AstrBot 只派发**带 filter** 的 handler：``waking_check`` 里明确有
    ``if len(handler.event_filters) == 0: continue``。所以 ``on_message``
    必须显式声明 ``@filter.event_message_type(EventMessageType.ALL)``，
    否则它永远不会被调用——终端只能看到启动时导入的历史，新消息收不到。

    老版本宿主缺少该 API 时退化成空装饰器并告警，至少不影响插件加载。
    """
    register = getattr(filter, "event_message_type", None)
    all_types = getattr(getattr(filter, "EventMessageType", None), "ALL", None)
    if callable(register) and all_types is not None:
        return register(all_types)
    logger.warning("当前 AstrBot 版本不支持 filter.event_message_type，入站消息将不会被记录")

    def decorator(fn):
        return fn

    return decorator


def _streaming_response(generator, headers: dict[str, str]):
    """给 SSE 生成器挑一个「不会被宿主缓冲」的响应类型。

    AstrBot 4.x 的插件 API 由 dashboard 统一分发，返回的 Quart ``Response``
    会被 ``_coerce_view_result`` 认成 Quart 响应，再走
    ``_quart_response_to_starlette``——里面是 ``await response.get_data()``，
    也就是**把整个响应体读成一个 bytes**。SSE 生成器永不结束，于是请求永久挂起，
    一个字节都到不了浏览器（前端表现为一直「正在连接」）。

    宿主自己的 ``stream_response`` 返回 Starlette ``StreamingResponse``，
    分发层对 Starlette Response 是原样透传的，这才真正流式。

    老宿主没有该 API 时退回 Quart Response（那里本来就直接吃生成器）。
    """
    try:
        from astrbot.api.web import stream_response
    except ImportError:
        stream_response = None  # type: ignore[assignment]
    if stream_response is not None:
        return stream_response(
            generator, content_type="text/event-stream", headers=headers
        )
    return Response(generator, mimetype="text/event-stream", headers=headers)


class Plugin(Star):
    # 入站乐观锁的短窗口（秒）：平台回推 / 多路径推送的同一条消息会被合并
    INBOUND_DEDUPE_WINDOW = 8.0
    # SSE 空闲心跳间隔（秒）：前端拿它判断连接是否假死
    SSE_KEEPALIVE = 15

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
        # 后台任务必须持引用：只丢给 loop 的话可能被 GC 掉
        self._bg_tasks: set[asyncio.Task] = set()
        # 自动发现串行化：启动任务与 POST /discover 同时跑会重复导入
        self._discovery_lock = asyncio.Lock()

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
            ("updates", self.api_updates, ["GET"], "全局 id 游标拉新消息（轮询降级）"),
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
            ("discover", self.api_discover, ["POST"], "自动发现会话并导入历史"),
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
        """在可能没有事件循环的上下文里安全地广播。

        走 ``_spawn`` 而不是裸 ``create_task``：任务必须持有引用，
        否则事件可能在真正推送前被 GC 掉，前端就会「消息来得不及时」。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spawn(self._sse_broadcast(event_type, data))

    async def _sse_generate(self, cid: str, queue: asyncio.Queue):
        try:
            yield "event: connected\ndata: {}\n\n"
            hello = json.dumps({"clients": len(self._sse_clients)}, ensure_ascii=False)
            yield f"event: hello\ndata: {hello}\n\n"
            while True:
                try:
                    event_type, payload = await asyncio.wait_for(
                        queue.get(), timeout=self.SSE_KEEPALIVE
                    )
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    # 用 ping 事件而不是注释行：注释在 bridge 里会被丢掉，
                    # 前端就分不清「连接还在、只是没人说话」和「连接已经死了」。
                    yield "event: ping\ndata: {}\n\n"
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
        headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return _streaming_response(self._sse_generate(cid, queue), headers)

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

    def _is_duplicate_inbound(
        self, session_key: str, sender_id: str, content: str, timestamp: float
    ) -> bool:
        """Bot 自身消息的乐观锁：短窗口内已有同内容就跳过。

        只用于「发送者是 Bot 自己」的回推——平台会把 Bot 在其它端发出的消息
        也当作入站事件下发，一次发送可能因此被记成两条。

        这里刻意忽略发送者：终端发出时 sender_id 为空，平台回推时是 Bot 的 ID，
        只比发送者会漏判。其他用户的消息不经过这里，不会互相吞掉。
        """
        return self.store.has_recent_duplicate(
            session_key,
            sender_id,
            content,
            timestamp,
            window=self.INBOUND_DEDUPE_WINDOW,
            ignore_sender=True,
        )

    def _sender_is_self(self, event: AstrMessageEvent, sender_id: str) -> bool:
        """发送者是不是 Bot 自己（平台把自身消息回推的情况）。"""
        if not sender_id:
            return False
        self_id = ""
        with contextlib.suppress(Exception):
            self_id = str(event.get_self_id() or "")
        return bool(self_id) and sender_id == self_id

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

    @_message_listener()
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

        # 不忽略 Bot 自身的消息（部分平台只在回推里才看得到自己发的消息），
        # 但只对 Bot 自己做的去重：别人连发两条相同的话必须都保留。
        content = str(getattr(event, "message_str", "") or "")
        timestamp = coerce_float(getattr(message_obj, "timestamp", 0), now_ts()) or now_ts()
        if self._sender_is_self(event, sender_id):
            # 先查内存里的乐观锁：平台回推可能就发生在 send_message 的 await 里，
            # 那时 api_send 还没把 sent 行写进去，库查重一定落空 → 同一条记两次。
            if self._consume_pending_echo(session_key, content):
                logger.debug(f"忽略终端刚发出消息的回推: {session_key} / {content[:20]}")
                return None
            if self._is_duplicate_inbound(session_key, sender_id, content, timestamp):
                logger.debug(f"忽略 Bot 自身消息的重复回推: {session_key} / {content[:20]}")
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
                "content": content,
                "segments": segments,
                "group_id": group_id,
                "platform": platform,
                "platform_id": platform_id,
                "platform_message_id": str(getattr(message_obj, "message_id", "") or ""),
                "timestamp": timestamp,
            },
        )
        self._broadcast_soon("message", saved)
        return saved

    # ── 自动发现：平台群/好友 + AstrBot 自身历史 ──────────────────

    @_optional_hook("on_astrbot_loaded")
    async def on_loaded(self):
        """AstrBot 加载完成后，后台补齐会话列表与历史，不阻塞启动。"""
        if not self._discovery_enabled():
            return
        self._spawn(self.discover_and_import())

    def _spawn(self, coro) -> None:
        """跑一个后台任务，并持有引用直到结束（避免任务被 GC 中断）。"""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _discovery_enabled(self) -> bool:
        return coerce_bool(self.config.get("auto_discover_sessions", True), True) or coerce_bool(
            self.config.get("import_astrbot_history", True), True
        )

    async def discover_and_import(self) -> dict:
        """拉平台群/好友建会话，并导入 AstrBot 自身的历史消息。

        群全部自动建；好友按活跃度（有历史才建），避免上千好友把列表撑爆。
        任何一步失败都只记在该步骤的结果里，不影响其它来源。
        """
        async with self._discovery_lock:
            return await self._discover_and_import_locked()

    async def _discover_and_import_locked(self) -> dict:
        result = {
            "groups_created": 0,
            "friends_created": 0,
            "history_imported": 0,
            "errors": [],
        }
        if coerce_bool(self.config.get("auto_discover_sessions", True), True):
            await self._discover_platforms(result)
        if coerce_bool(self.config.get("import_astrbot_history", True), True):
            await self._import_astrbot_history(result)
        self._broadcast_soon("sessions_discovered", dict(result))
        logger.info(
            "自动发现完成：群 +%s、好友 +%s、历史 +%s%s",
            result["groups_created"],
            result["friends_created"],
            result["history_imported"],
            f"，错误 {len(result['errors'])} 条" if result["errors"] else "",
        )
        return result

    async def _discover_platforms(self, result: dict) -> None:
        """遍历平台实例，把群/好友注册成会话。"""
        include_friends = coerce_bool(
            self.config.get("auto_discover_include_friends", True), True
        )
        try:
            platforms = list(self.context.platform_manager.get_insts())
        except Exception as exc:
            result["errors"].append(f"读取平台列表失败: {exc}")
            return

        for platform in platforms:
            meta = getattr(platform, "meta", None)
            meta = meta() if callable(meta) else None
            platform_id = str(getattr(meta, "id", "") or "")
            adapter = str(getattr(meta, "name", "") or platform_id)
            if not platform_id:
                continue

            groups = await self._call_platform(platform, "get_group_list", result)
            for item in groups:
                if not isinstance(item, dict):
                    continue
                group_id = str(item.get("group_id") or "").strip()
                if not group_id:
                    continue
                name = str(item.get("group_name") or group_id)
                if self._register_discovered(
                    session_key_for_group(group_id),
                    platform_id=platform_id,
                    adapter=adapter,
                    peer_id=group_id,
                    peer_name=name,
                    is_group=True,
                ):
                    result["groups_created"] += 1

            if not include_friends:
                continue
            friends = await self._call_platform(platform, "get_friend_list", result)
            for item in friends:
                if not isinstance(item, dict):
                    continue
                user_id = str(item.get("user_id") or "").strip()
                if not user_id:
                    continue
                send_target = self._infer_umo(platform_id, False, user_id)
                # 好友按活跃度：没有历史记录就不建，避免私聊列表膨胀
                if not await self._friend_is_active(platform_id, send_target):
                    continue
                if self._register_discovered(
                    session_key_for_private(send_target, user_id),
                    platform_id=platform_id,
                    adapter=adapter,
                    peer_id=user_id,
                    peer_name=str(item.get("nickname") or item.get("remark") or user_id),
                    is_group=False,
                    send_target=send_target,
                ):
                    result["friends_created"] += 1

    async def _call_platform(self, platform, action: str, result: dict) -> list:
        """调协议端列表接口；不支持时静默降级为空列表。"""
        try:
            client = platform.get_client()
            api = getattr(client, "api", client)
            raw = await api.call_action(action)
        except Exception as exc:
            result["errors"].append(f"{action} 失败: {exc}")
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, list):
                return data
        return []

    def _register_discovered(
        self,
        session_key: str,
        *,
        platform_id: str,
        adapter: str,
        peer_id: str,
        peer_name: str,
        is_group: bool,
        send_target: str = "",
    ) -> bool:
        """注册一个自动发现的会话；已存在时只补空缺，绝不覆盖用户改动。"""
        if not session_key:
            return False
        if session_key in self.session_meta:
            self._fill_discovered_gaps(
                session_key,
                adapter,
                send_target or self._infer_umo(platform_id, is_group, peer_id),
            )
            return False

        meta = {
            # platform 存适配器名（如 aiocqhttp），platform_id 才是实例 ID
            "platform": adapter,
            "platform_id": platform_id,
            "group_id": peer_id if is_group else "",
            "group_name": peer_name if is_group else "",
            "is_group": is_group,
            "send_target": send_target or self._infer_umo(platform_id, is_group, peer_id),
            "peer_id": peer_id,
            "peer_name": peer_name or peer_id,
        }
        self._ensure_session(session_key, meta)
        return True

    def _fill_discovered_gaps(self, session_key: str, adapter: str, send_target: str) -> None:
        current = self.session_meta.get(session_key) or {}
        changed = False
        # platform 在旧版本里可能被错误地写成了实例 ID，这里顺手纠正
        platform_stale = not current.get("platform") or (
            current.get("platform") == current.get("platform_id")
        )
        if platform_stale and adapter and current.get("platform") != adapter:
            current["platform"] = adapter
            changed = True
        if send_target and not current.get("send_target"):
            current["send_target"] = send_target
            changed = True
        if changed:
            self.store.upsert_session(session_key, current)

    async def _friend_is_active(self, platform_id: str, send_target: str) -> bool:
        """好友是否活跃：本插件已存过消息，或 AstrBot 里有历史。"""
        session_key = session_key_for_private(send_target, send_target.rpartition(":")[2])
        if session_key and self.store.count_messages(session_key):
            return True
        manager = getattr(self.context, "message_history_manager", None)
        if manager is None:
            return False
        try:
            rows = await self._fetch_history(
                manager, platform_id, send_target, page_size=1, max_pages=1
            )
        except Exception:
            return False
        return bool(rows)

    async def _import_astrbot_history(self, result: dict) -> None:
        """把 AstrBot 自身的 platform_message_history 增量导入本插件。

        只导本插件已知的会话（群 + 已知私聊），user_id 就是它们的 UMO。
        """
        manager = getattr(self.context, "message_history_manager", None)
        if manager is None:
            return

        for session_key in list(self.session_index):
            meta = self.session_meta.get(session_key) or {}
            platform_id = str(meta.get("platform_id") or "")
            scope = str(meta.get("send_target") or "")
            if not platform_id or not scope:
                continue
            try:
                imported = await self._import_scope(manager, session_key, platform_id, scope)
            except Exception as exc:
                result["errors"].append(f"导入历史失败 {session_key}: {exc}")
                continue
            result["history_imported"] += imported

    async def _import_scope(self, manager, session_key: str, platform_id: str, scope: str) -> int:
        rows = await self._fetch_history(manager, platform_id, scope)
        if not rows:
            return 0

        watermark = self.store.get_import_watermark(platform_id, scope)
        imported = 0
        for row_dict in rows:
            try:
                row_id = int(row_dict.get("id"))
            except (TypeError, ValueError):
                continue
            if row_id <= watermark:
                continue
            if self.store.has_astrbot_message(session_key, row_id):
                continue
            message = astrbot_history_to_message(row_dict)
            if message is None:
                continue
            meta = self.session_meta.get(session_key) or {}
            message.update(
                {
                    "platform": meta.get("platform", ""),
                    "platform_id": platform_id,
                    "group_id": meta.get("group_id", ""),
                }
            )
            self.store.add_message(session_key, message)
            imported += 1

        # 水位推到「本轮真正见到过的最新 id」。_fetch_history 会翻完所有页，
        # 所以这里不会跳过任何更早的记录。
        highest = max([int(row_dict["id"]) for row_dict in rows] + [watermark])
        self.store.set_import_watermark(platform_id, scope, highest)
        return imported

    async def _fetch_history(
        self,
        manager,
        platform_id: str,
        scope: str,
        page_size: int = 500,
        max_pages: int = 40,
    ) -> list[dict]:
        """按页取回历史，直到取空或达到页数上限；结果按 id 升序。

        AstrBot 的 ``page=1`` 是**最新**的一页，只取第一页会漏掉更早的记录，
        而水位一旦推上去，那些漏掉的记录就永远补不回来了。
        """
        collected: dict[int, dict] = {}
        for page in range(1, max(1, max_pages) + 1):
            try:
                rows = manager.get(
                    platform_id=platform_id, user_id=scope, page=page, page_size=page_size
                )
            except TypeError:  # 老签名，不支持分页
                if page > 1:
                    break
                rows = manager.get(platform_id, scope)
            if asyncio.iscoroutine(rows):
                rows = await rows
            batch = list(rows or [])
            for row in batch:
                row_dict = row if isinstance(row, dict) else self._history_row_dict(row)
                try:
                    row_id = int(row_dict.get("id"))
                except (TypeError, ValueError):
                    continue
                collected[row_id] = row_dict
            if len(batch) < page_size:
                break
        return [collected[row_id] for row_id in sorted(collected)]

    @staticmethod
    def _history_row_dict(row) -> dict:
        """把 SQLModel 行转成 dict，只取我们关心的字段。"""
        return {
            "id": getattr(row, "id", None),
            "created_at": getattr(row, "created_at", None),
            "sender_id": getattr(row, "sender_id", None),
            "sender_name": getattr(row, "sender_name", None),
            "content": getattr(row, "content", None),
        }

    # ── 事件：出站（Bot / 插件主动推送） ──────────────────────────

    @_optional_hook("on_decorating_result")
    async def on_before_send(self, event: AstrMessageEvent):
        """发送前把 <mention> 标签 / 手打 @ 改写成真实 At 组件。

        这是 <mention> 问题的根治点：AstrBot 核心不认这类占位标签，
        LLM 写出 <mention id="123"/> 时会原样发到平台，用户看到字面标签、
        被 @ 的人也收不到提醒。在消息真正发出前改写，平台才会收到 At 段。
        """
        allow_tags = coerce_bool(self.config.get("convert_mention_tags", True), True)
        allow_bare = coerce_bool(self.config.get("parse_bare_at", True), True)
        if not (allow_tags or allow_bare):
            return
        try:
            self._rewrite_mentions(event, allow_tags=allow_tags, allow_bare=allow_bare)
        except Exception as exc:
            logger.debug(f"改写 @ 标记失败（忽略）: {exc}")

    def _rewrite_mentions(
        self,
        event: AstrMessageEvent,
        *,
        allow_tags: bool | None = None,
        allow_bare: bool | None = None,
    ) -> bool:
        """就地重写 event 结果链：文本里的 @ 标记 → At，标签绝不外泄。"""
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return False

        from astrbot.api.message_components import At, Plain

        if allow_tags is None:
            allow_tags = coerce_bool(self.config.get("convert_mention_tags", True), True)
        if allow_bare is None:
            allow_bare = coerce_bool(self.config.get("parse_bare_at", True), True)
        # respond 阶段会跳过「只有 Reply/At」的消息链（#2670），
        # 改写后只剩这两类时必须补一个零宽明文段。
        header_types: tuple[type, ...] = (At,)
        with contextlib.suppress(ImportError):
            from astrbot.api.message_components import Reply as _Reply

            header_types = (At, _Reply)

        resolve = self.store.mention_resolver(self._session_key_of(event))

        rewritten: list = []
        changed = False
        for component in chain:
            text = getattr(component, "text", None)
            if not isinstance(component, Plain) or not isinstance(text, str) or not text:
                rewritten.append(component)
                continue
            tagged = allow_tags and has_mention_markup(text)
            if not (tagged or (allow_bare and "@" in text)):
                rewritten.append(component)
                continue
            segments = text_to_segments(
                text, resolve, allow_bare_at=allow_bare, allow_tags=allow_tags
            )
            if not tagged and not any(
                segment.get("type") == "at" for segment in segments
            ):
                rewritten.append(component)  # 没有真的解析出 @，原样保留
                continue
            if not segments:
                # 整段就是占位标签（如 `<mention>`），改写后什么都不剩。
                # 直接丢掉这一段：宁可这条消息不发，也不能让标签外泄。
                changed = True
                continue
            for segment in segments:
                target = str(segment.get("target") or "")
                if segment.get("type") == "at" and target:
                    rewritten.append(self._at_component(target, str(segment.get("text") or "")))
                else:
                    rewritten.append(Plain(str(segment.get("text") or "")))
            changed = True

        if changed:
            if rewritten and all(isinstance(item, header_types) for item in rewritten):
                rewritten.append(Plain("\u200b"))
            chain[:] = rewritten
        return changed

    @staticmethod
    def _at_component(target: str, name: str = ""):
        """构造 At 组件；@全体成员用宿主专门的 AtAll。"""
        from astrbot.api.message_components import At

        if target == "all":
            try:
                from astrbot.api.message_components import AtAll

                return AtAll()
            except Exception:  # 老宿主没有 AtAll，或构造签名不同
                pass
        return At(qq=target, name=name)

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
            allow_tags = coerce_bool(self.config.get("convert_mention_tags", True), True)
            allow_bare = coerce_bool(self.config.get("parse_bare_at", True), True)
            if allow_tags or allow_bare:
                with contextlib.suppress(Exception):
                    self._rewrite_mentions(event, allow_tags=allow_tags, allow_bare=allow_bare)
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            segments = serialize_chain(chain)
            if not segments:
                return
            text = "".join(
                str(segment.get("text") or "")
                for segment in segments
                if segment.get("type") == "text"
            )
            # 纯 @ 的消息会被补一个零宽空格保证发得出去，
            # 但它是投递用的，不能混进入库的纯文本预览里。
            text = text.replace("\u200b", "").strip()

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

    @staticmethod
    def _echo_key(text: str) -> str:
        """回声比对用的归一化：去掉全部空白与零宽字符。"""
        return "".join(str(text or "").split()).replace("\u200b", "")

    def _consume_pending_echo(self, session_key: str, content: str) -> bool:
        """这条入站是不是「终端刚发出去那条」的回推？命中就**释放乐观锁**。

        为什么不能只靠库查重：``api_send`` 是先 ``await send_message`` 再写 sent 行，
        而平台回推恰好可能发生在那个 await 里。这时库里还没有 sent 记录，
        ``has_recent_duplicate`` 一定查不到，同一条就被记成 received + sent 两份。

        这里是内存锁，设置发生在 send_message **之前**，正好覆盖这个窗口。
        命中即 pop：回声已经处理完，再来的同内容消息就不是回声了，
        不能让锁在 8 秒窗口里继续误吞真实消息。
        """
        record = self._recent_sends.get(session_key)
        if not record:
            return False
        fingerprint, stamp = record
        if time.time() - stamp > self.INBOUND_DEDUPE_WINDOW:
            self._recent_sends.pop(session_key, None)
            return False
        inbound = self._echo_key(content)
        if not fingerprint or not inbound:
            return False
        # 回声可能把 At 段折回文本（"@小明 你好"），也可能只剩正文（"你好"）
        bare = re.sub(r"^(?:@[^\s@]+)+", "", fingerprint)
        if inbound not in (fingerprint, bare):
            return False
        self._recent_sends.pop(session_key, None)
        return True

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
            if with_history and entry["message_count"]:
                # 自动发现会让会话数量达到几百个，空会话再查一次是纯浪费
                page, has_more, next_cursor = self.store.query_page(
                    session_key, limit=history_limit
                )
                entry["messages"] = page
                entry["history"] = {
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "limit": history_limit,
                }
            elif with_history:
                entry["messages"] = []
                entry["history"] = {"has_more": False, "next_cursor": None,
                                    "limit": history_limit}
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
                    allow_tags=coerce_bool(
                        self.config.get("convert_mention_tags", True), True
                    ),
                )
            )
        if isinstance(extra_segments, list):
            for segment in extra_segments:
                if isinstance(segment, dict) and segment.get("type") in ("image", "at"):
                    segments.append(segment)

        try:
            chain = self._build_chain(segments)
        except Exception as exc:
            logger.error(f"构造消息链失败: {exc}")
            return jsonify({"error": f"发送失败: {exc}"}), 500

        # **先入库再发送**：平台回推可能就落在下面 send_message 的 await 里，
        # 先写完 sent 行，任何时刻到达的回声都能被「同会话 + 同内容」查重拦下。
        # （原先先发后存，那个 await 窗口里库里什么都没有，必然漏判成重复。）
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
        # 出站钩子会看到这条消息经过发送管线，用指纹把它认成「终端已入库」而跳过
        self._recent_sends[session_id] = (self._send_fingerprint(segments), time.time())
        try:
            await self.context.send_message(send_target, chain)
        except Exception as exc:
            self._recent_sends.pop(session_id, None)
            # 发送失败：删掉预写的记录，别在终端留一条其实没发出去的消息
            self.store.delete_message(saved["id"])
            logger.error(f"发送消息失败: {exc}")
            return jsonify({"error": f"发送失败: {exc}"}), 500

        # 其他标签页也要看到自己发出的消息
        await self._sse_broadcast("message", saved)
        return jsonify({"status": "ok", "data": saved})

    def _build_chain(self, segments: list[dict]):
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
                target = str(segment.get("target") or "")
                if target:
                    # 带上昵称：出站钩子折回文本时才能与原文对上，
                    # 也便于平台侧展示「@小明」而不是「@123456」
                    try:
                        chain.chain.append(
                            self._at_component(target, str(segment.get("text") or ""))
                        )
                    except Exception as exc:
                        logger.warning(f"@ 还原失败: {exc}")
            elif kind == "plain" and segment.get("text"):
                chain.message(str(segment["text"]))
            elif kind not in ("reply", "plain", "text"):
                logger.debug(f"发送时跳过不支持的片段类型: {kind}")
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

    async def api_updates(self):
        """?after_id=N&limit= —— 轮询降级：一次带回所有会话的新消息。

        前端拿全局消息 id 做游标（不是 timestamp）：同一秒的多条消息、
        发送端时钟回拨都不会漏；一次请求覆盖所有会话，冷会话也会更新未读数。
        """
        after_id = clamp_int(request.args.get("after_id", 0), 0, 2**62, 0)
        limit = clamp_int(request.args.get("limit", MAX_PAGE_SIZE), 1, MAX_PAGE_SIZE, MAX_PAGE_SIZE)
        messages = self.store.messages_after(after_id, limit=limit)
        last_id = messages[-1]["id"] if messages else after_id
        return jsonify(
            {
                "messages": messages,
                "last_id": last_id,
                "has_more": len(messages) >= limit,
            }
        )

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
                # 宿主里 meta 是**方法**（Platform.meta），不调用只能拿到 bound method，
                # 结果 id/name 全是空串，前端平台下拉框就永远是空的。
                meta = getattr(platform, "meta", None)
                meta = meta() if callable(meta) else meta
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

    async def api_discover(self):
        """手动重跑自动发现：拉群/好友建会话 + 补导入 AstrBot 历史。"""
        result = await self.discover_and_import()
        result["sessions"] = [
            self._session_payload(key) for key in list(self.session_index)
        ]
        return jsonify({"status": "ok", "result": result})

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
            # platform 必须是适配器名（aiocqhttp），不能写成平台实例 ID，
            # 否则联系人关联与 @昵称解析都会对不上。
            "platform": self._adapter_name(platform_id),
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

    def _adapter_name(self, platform_id: str) -> str:
        """平台实例 ID → 适配器名；取不到时退回实例 ID（与旧行为一致）。"""
        try:
            platform = self.context.get_platform_inst(platform_id)
            meta = getattr(platform, "meta", None)
            meta = meta() if callable(meta) else meta
            name = str(getattr(meta, "name", "") or "")
            if name:
                return name
        except Exception:
            pass
        return platform_id

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
                "version": PLUGIN_VERSION,
                "db": self.db_path,
                "sessions": len(self.session_index),
                "sse_clients": len(self._sse_clients),
                "max_log_per_session": self.max_log,
                "capture_bot_messages": coerce_bool(
                    self.config.get("capture_bot_messages", True), True
                ),
                # 前端据此决定首屏是否直接请求图片；工具栏开关可在本地覆盖
                "load_images": coerce_bool(self.config.get("load_images", False), False),
            }
        )

    # ── 生命周期 ──────────────────────────────────────────────

    async def terminate(self):
        """插件卸载/重载时清理 SSE、后台任务与缓存。"""
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        self._sse_clients.clear()
        self._recent_sends.clear()
        logger.info(f"{PLUGIN_NAME} 已卸载")
