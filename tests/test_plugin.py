"""Plugin 行为测试：用假宿主替代 AstrBot，验证入站/出站捕获与会话归并。

不导入真实 AstrBot，只用 minimal stub 注入 sys.modules，
这样可以验证 main.py 里最容易出错的逻辑：
  * 群聊消息合并到 group:<id>
  * Bot 出站消息进入同一条会话
  * 终端发出消息不被出站钩子重复入库
  * 配置项与迁移逻辑
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class EventMessageType(enum.Flag):
    """镜像宿主的 astrbot.core.star.filter.event_message_type.EventMessageType。"""

    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


# ── 假 AstrBot 宿主 ────────────────────────────────────────


class FakeEvent:
    def __init__(self, umo, group_id="", sender_id="", sender_name="", text="",
                 chain=None, message_id="", self_id="bot-1", platform="aiocqhttp"):
        self.unified_msg_origin = umo
        self.message_str = text
        self._result = types.SimpleNamespace(chain=chain or [])
        self.message_obj = types.SimpleNamespace(
            group_id=group_id,
            sender=types.SimpleNamespace(user_id=sender_id, nickname=sender_name),
            message=chain or [],
            message_id=message_id,
            timestamp=1700000000.0,
            raw_message={},
        )
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._platform = platform

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return self._self_id

    def get_platform_name(self):
        return self._platform

    def get_platform_id(self):
        return f"{self._platform}-inst"

    def get_result(self):
        return self._result


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeImage:
    """类名必须与宿主一致——片段类型是按类名判定的。"""

    def __init__(self, url="", file="", path=""):
        self.url = url
        self.file = file
        self.path = path


class FakeRecord:
    def __init__(self, file=""):
        self.file = file


class Node:
    """宿主合并转发的单条聊天记录。"""

    def __init__(self, name="", uin="", content=None, forward_id=None):
        self.name = name
        self.uin = uin
        self.content = content or []
        if forward_id is not None:
            self.id = forward_id


class At:
    """宿主 At 组件。类名必须是 At——片段类型按类名判定。"""

    def __init__(self, qq=None, name=""):
        self.qq = qq
        self.name = name


class AtAll(At):
    """宿主的 @全体成员 组件：qq 固定为 "all"。"""

    def __init__(self, qq="all", name=""):
        super().__init__(qq=qq, name=name)


class Reply:
    """宿主 Reply（引用）组件。respond 阶段会把它和 At 一起当「头部」剥离。"""

    def __init__(self, id="", sender_id=None):
        self.id = id
        self.sender_id = sender_id


def _install_fake_astrbot():
    """构造最小 astrbot 包，只包含 main.py 用到的符号。"""
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class _Filter:
        @staticmethod
        def after_message_sent(*args, **kwargs):
            def wrapper(fn):
                fn._is_after_sent = True
                return fn

            return wrapper

        @staticmethod
        def on_decorating_result(*args, **kwargs):
            def wrapper(fn):
                fn._is_decorating_result = True
                return fn

            return wrapper

        @staticmethod
        def event_message_type(message_type, **kwargs):
            def wrapper(fn):
                fn._is_message_listener = True
                fn._event_message_type = message_type
                return fn

            return wrapper

        @staticmethod
        def on_astrbot_loaded(*args, **kwargs):
            def wrapper(fn):
                fn._is_astrbot_loaded = True
                return fn

            return wrapper

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(FakePlain(text))
            return self

    event_mod.filter = _Filter
    event_mod.filter.EventMessageType = EventMessageType
    event_mod.AstrMessageEvent = FakeEvent
    event_mod.MessageChain = MessageChain

    comp = types.ModuleType("astrbot.api.message_components")

    class _Image:
        @staticmethod
        def fromURL(url):
            return FakeImage(url=url)

        @staticmethod
        def fromFileSystem(path):
            return FakeImage(file=f"file://{path}", path=path)

        @staticmethod
        def fromBase64(base64):
            return FakeImage(file=f"base64://{base64}")

        @staticmethod
        def fromBytes(byte):
            return FakeImage(file="base64://" + byte.hex())

    comp.Plain = FakePlain
    comp.Image = _Image
    comp.At = At
    comp.AtAll = AtAll
    comp.Reply = Reply

    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object

    web = types.ModuleType("astrbot.api.web")

    # Quart 是 AstrBot 内置依赖，测试环境里用最小桩替代
    quart = types.ModuleType("quart")

    class Response:
        def __init__(self, body="", mimetype="text/plain", headers=None):
            self.body = body
            self.mimetype = mimetype
            self.headers = headers or {}

    quart.Response = Response
    quart.jsonify = lambda payload, *a, **k: payload
    quart.request = types.SimpleNamespace(
        args={},
        method="GET",
        get_json=lambda *a, **k: None,
    )

    sys.modules.update(
        {
            "astrbot": pkg,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": comp,
            "astrbot.api.star": star,
            "astrbot.api.web": web,
            "quart": quart,
        }
    )
    api.AstrBotConfig = dict


class FakeContext:
    def __init__(self):
        self.routes = {}
        self.sent = []
        self.platform_manager = types.SimpleNamespace(get_insts=lambda: [])
        # AstrBot 自身的消息历史管理器（自动导入用）
        self.message_history_manager = FakeHistoryManager()

    def register_web_api(self, path, handler, methods, desc):
        self.routes[path] = (handler, methods)

    async def send_message(self, target, chain):
        self.sent.append((target, chain))

    def get_platform_inst(self, platform_id):
        for platform in self.platform_manager.get_insts():
            if platform.meta().id == platform_id:
                return platform
        raise RuntimeError("平台实例不存在")


class FakeHistoryManager:
    """stand-in for PlatformMessageHistoryManager：按 (platform_id, user_id) 存历史。

    分页语义与宿主一致：page=1 是**最新**的一页（DB 里按 id DESC 取），
    取回来之后再反转成时间正序。忽略 page_size 的桩会让「只导第一页」的
    缺陷测不出来。
    """

    def __init__(self):
        self.rows = {}
        self.calls = []

    def seed(self, platform_id, user_id, rows):
        self.rows[(platform_id, user_id)] = list(rows)

    async def get(self, platform_id, user_id, page=1, page_size=200):
        self.calls.append((platform_id, user_id, page, page_size))
        ordered = sorted(
            self.rows.get((platform_id, user_id), []),
            key=lambda row: int(row["id"]),
            reverse=True,
        )
        start = max(0, (int(page) - 1) * int(page_size))
        chunk = ordered[start : start + int(page_size)]
        chunk.reverse()
        return chunk


class FakePlatform:
    """假平台实例：模拟 aiocqhttp 的 get_group_list / get_friend_list。"""

    def __init__(self, platform_id, adapter="aiocqhttp", groups=None, friends=None):
        self._platform_id = platform_id
        self._adapter = adapter
        self.groups = groups or []
        self.friends = friends or []
        self.calls = []

    def meta(self):
        return types.SimpleNamespace(id=self._platform_id, name=self._adapter, type=self._adapter)

    def get_client(self):
        platform = self

        class _Api:
            async def call_action(self, action, **kwargs):
                platform.calls.append(action)
                if action == "get_group_list":
                    return platform.groups
                if action == "get_friend_list":
                    return platform.friends
                return []

        return types.SimpleNamespace(api=_Api())


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    _install_fake_astrbot()
    monkeypatch.setenv("WEBCHAT_TEST_DATA", str(tmp_path))

    import importlib

    if "main" in sys.modules:
        del sys.modules["main"]
    main = importlib.import_module("main")
    # 强制数据目录到 tmp
    monkeypatch.setattr(main, "_resolve_data_dir", lambda: str(tmp_path))

    ctx = FakeContext()
    instance = main.Plugin(ctx, {"max_log_per_session": 50})
    return instance


def inbound(plugin, **kwargs):
    plugin._record_inbound(FakeEvent(**kwargs))


def test_inbound_listener_is_registered(plugin):
    """入站监听必须带 @filter.event_message_type。

    宿主 waking_check 会跳过 ``event_filters`` 为空的 handler，没有这个
    装饰器时 ``on_message`` 一次都不会被调用：终端只剩启动时导入的历史，
    群里的新消息永远到不了——用户感知就是「信息没有及时接收」。
    """
    handler = type(plugin).on_message
    assert getattr(handler, "_is_message_listener", False), (
        "on_message 缺少 @filter.event_message_type，AstrBot 不会把入站消息派发给它"
    )
    assert getattr(handler, "_event_message_type", None) == EventMessageType.ALL, (
        "监听范围必须是 ALL，否则群聊/私聊只会收到一半"
    )


def test_broadcast_soon_keeps_task_reference(plugin, monkeypatch):
    """广播任务要挂到 _bg_tasks 上：裸 create_task 可能被 GC，事件就丢了。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_broadcast(event_type, data):
        started.set()
        await release.wait()

    monkeypatch.setattr(plugin, "_sse_broadcast", fake_broadcast)

    async def scenario():
        plugin._broadcast_soon("message", {"session_id": "group:1"})
        await started.wait()
        return len(plugin._bg_tasks)

    try:
        assert _run(scenario()) == 1, "广播任务没有被 _bg_tasks 持有，可能被 GC 丢弃"
    finally:
        for task in list(plugin._bg_tasks):
            task.cancel()


def test_sse_keepalive_emits_ping(plugin):
    """空闲时也要发真正的 ping 事件：注释行在 bridge 里会被丢掉，
    前端就分不清「连接还活着但没消息」和「连接已经死了」。"""
    plugin.SSE_KEEPALIVE = 0.01
    queue: asyncio.Queue = asyncio.Queue()
    gen = plugin._sse_generate("cli-test", queue)

    async def scenario():
        frames = [await gen.__anext__() for _ in range(3)]
        await gen.aclose()
        return frames

    frames = _run(scenario())
    assert frames[0].startswith("event: connected")
    assert frames[2].startswith("event: ping"), f"空闲帧不是 ping: {frames[2]!r}"
    assert "data:" in frames[2]
    assert "cli-test" not in plugin._sse_clients


def test_sse_returns_host_streaming_response(plugin, monkeypatch):
    """SSE 必须返回宿主的 StreamingResponse。

    宿主分发插件 API 时会把 Quart Response 走 `await get_data()` 整段读出来，
    SSE 生成器永不结束 → 请求永久挂起，浏览器一个字节都收不到
    （这就是 0.3.1 当年要绕开「包装」的原因）。
    """

    sentinel = object()
    fake_web = types.ModuleType("astrbot.api.web")
    fake_web.stream_response = lambda content, **kwargs: sentinel
    monkeypatch.setitem(sys.modules, "astrbot.api.web", fake_web)

    response = _run(plugin.api_stream())

    assert response is sentinel, "没有走宿主的流式响应，SSE 会被缓冲掉"


def test_sse_falls_back_to_quart_on_old_host(plugin, monkeypatch):
    """老宿主没有 astrbot.api.web.stream_response 时退回 Quart Response。"""
    monkeypatch.delitem(sys.modules, "astrbot.api.web", raising=False)

    response = _run(plugin.api_stream())

    quart = sys.modules["quart"]
    assert isinstance(response, quart.Response)
    assert response.mimetype == "text/event-stream"


def _open_sse(plugin, monkeypatch, args):
    """Keep the real stream generator while bypassing only host response wrapping."""
    import main

    monkeypatch.setattr(main.request, "args", args)
    monkeypatch.setattr(main, "_streaming_response", lambda content, headers: content)
    return _run(plugin.api_stream())


@pytest.mark.parametrize("topic", ["group:2", ""])
def test_sse_other_session_gets_bounded_summary(plugin, monkeypatch, topic):
    gen = _open_sse(plugin, monkeypatch, {"session_id": topic})
    # The generator runs after the original request context has gone away.
    monkeypatch.setattr(sys.modules["main"].request, "args", {"session_id": "group:1"})
    message = {
        "id": 42, "session_id": "group:1", "type": "received",
        "sender": "小明", "sender_id": "u1", "sender_name": "明明",
        "content": "文" * 121, "preview": "图" * 121, "timestamp": 1700000000,
        "segments": [{"type": "image", "url": "large-image"}],
        "platform": "aiocqhttp", "platform_message_id": "secret",
    }

    async def scenario():
        await gen.__anext__()
        await gen.__anext__()
        await plugin._sse_broadcast("message", message)
        frame = await gen.__anext__()
        await gen.aclose()
        return frame

    frame = _run(scenario())
    event, data = frame.strip().split("\n")
    assert event == "event: message_summary"
    assert json.loads(data.removeprefix("data: ")) == {
        "id": 42, "session_id": "group:1", "type": "received",
        "sender": "小明", "sender_id": "u1", "sender_name": "明明",
        "content": "文" * 120, "preview": "图" * 120, "timestamp": 1700000000,
    }
    assert message["content"] == "文" * 121


@pytest.mark.parametrize("args", [{}, {"session_id": "group:1"}])
def test_sse_legacy_and_active_session_receive_full_message(plugin, monkeypatch, args):
    gen = _open_sse(plugin, monkeypatch, args)
    message = {"id": 7, "session_id": "group:1", "content": "x" * 200,
               "segments": [{"type": "text", "text": "x" * 200}]}

    async def scenario():
        await gen.__anext__()
        await gen.__anext__()
        await plugin._sse_broadcast("message", message)
        frame = await gen.__anext__()
        await gen.aclose()
        return frame

    frame = _run(scenario())
    event, data = frame.strip().split("\n")
    assert event == "event: message"
    assert json.loads(data.removeprefix("data: ")) == message


@pytest.mark.parametrize("event_type", [
    "session", "session_updated", "session_removed", "message_removed",
    "cleared", "sessions_discovered",
])
def test_sse_scoped_stream_keeps_global_events(plugin, monkeypatch, event_type):
    gen = _open_sse(plugin, monkeypatch, {"session_id": "group:1"})
    data = {"session_id": "group:2", "id": 42}

    async def scenario():
        await gen.__anext__()
        await gen.__anext__()
        await plugin._sse_broadcast(event_type, data)
        frame = await gen.__anext__()
        await gen.aclose()
        return frame

    assert _run(scenario()) == f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@pytest.mark.parametrize("cleanup", ["disconnect", "terminate", "dead_queue"])
def test_sse_topic_metadata_is_cleaned_up(plugin, monkeypatch, cleanup):
    gen = _open_sse(plugin, monkeypatch, {"session_id": "group:1"})
    cid, queue = next(iter(plugin._sse_clients.items()))
    assert isinstance(queue, asyncio.Queue)
    assert plugin._sse_topics[cid] == "group:1"

    async def scenario():
        await gen.__anext__()
        if cleanup == "terminate":
            await plugin.terminate()
        elif cleanup == "dead_queue":
            for _ in range(queue.maxsize):
                queue.put_nowait(("ping", "{}"))
            await plugin._sse_broadcast("session", {"id": "group:2"})
        else:
            await gen.aclose()
        assert cid not in plugin._sse_clients
        assert cid not in plugin._sse_topics
        await gen.aclose()

    _run(scenario())


def test_messages_after_walks_all_sessions_by_id(plugin):
    """轮询游标必须按全局 id、跨会话：只轮询当前会话会漏掉别人的消息，
    按 timestamp 则同一秒的多条会互相覆盖。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="群里第一条")
    inbound(plugin, umo="aiocqhttp:FriendMessage:9", sender_id="9",
            sender_name="小红", text="私聊第一条")

    rows = plugin.store.messages_after(0, limit=10)
    assert [m["content"] for m in rows] == ["群里第一条", "私聊第一条"]
    assert [m["content"] for m in plugin.store.messages_after(rows[0]["id"], limit=10)] == [
        "私聊第一条"
    ]


def test_updates_endpoint_uses_id_cursor(plugin, monkeypatch):
    """轮询端点：after_id 之后的全部新消息 + last_id + has_more。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="一")
    inbound(plugin, umo="aiocqhttp:FriendMessage:9", sender_id="9",
            sender_name="小红", text="二")

    quart = sys.modules["quart"]
    monkeypatch.setattr(quart.request, "args", {"after_id": "1", "limit": "10"})

    payload = _run(plugin.api_updates())
    assert [m["id"] for m in payload["messages"]] == [2]
    assert payload["last_id"] == 2
    assert payload["has_more"] is False
    assert any(path.endswith("/updates") for path in plugin.context.routes)


# ── 入站 ──────────────────────────────────────────────────


def test_group_messages_merge_into_one_session(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="第一条")
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u2", sender_name="小红", text="第二条")

    assert plugin.session_index == ["group:1"]
    assert plugin.store.count_messages("group:1") == 2


def test_private_message_uses_umo_key(plugin):
    inbound(plugin, umo="aiocqhttp:FriendMessage:9", sender_id="9",
            sender_name="小明", text="私聊")
    assert plugin.session_index == ["aiocqhttp:FriendMessage:9"]
    assert plugin.session_meta["aiocqhttp:FriendMessage:9"]["is_group"] is False


def test_platform_name_is_adapter_not_class(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    assert plugin.session_meta["group:1"]["platform"] == "aiocqhttp"


def test_rich_segments_are_stored(plugin):
    chain = [FakePlain("看图"), FakeImage(url="https://e/1.png")]
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", chain=chain, text="看图")
    messages = plugin.store.query_messages("group:1")
    assert [s["type"] for s in messages[0]["segments"]] == ["text", "image"]
    assert "看图" in messages[0]["preview"]


# ── <mention> 标签还原为真实 At ───────────────────────────


def test_pre_send_hook_rewrites_mention_tag_before_delivery(plugin):
    """根治点：发送前钩子必须把标签换成 At，这样平台收到的才是真 @。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain('<mention id="123"/> 来看')],
    )
    _run(plugin.on_before_send(event))

    chain = event.get_result().chain
    assert [type(c).__name__ for c in chain] == ["At", "FakePlain"]
    assert chain[0].qq == "123"
    assert chain[1].text == " 来看"


def test_pre_send_hook_leaves_clean_chain_untouched(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    plain = FakePlain("普通回复")
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1", chain=[plain])
    assert plugin._rewrite_mentions(event) is False
    assert event.get_result().chain == [plain]


def test_pre_send_hook_rewrites_bare_at_to_known_member(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="我叫小明")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain("@小明 你看")],
    )
    _run(plugin.on_before_send(event))

    chain = event.get_result().chain
    assert chain[0].qq == "u1"
    assert chain[0].name == "小明"


def test_pre_send_hook_keeps_unknown_bare_at_as_text(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    plain = FakePlain("@查无此人 hi")
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1", chain=[plain])
    _run(plugin.on_before_send(event))

    assert event.get_result().chain == [plain]


def test_pre_send_hook_can_be_disabled(plugin):
    plugin.config["convert_mention_tags"] = False
    plain = FakePlain('<mention id="1"/> x')
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1", chain=[plain])
    _run(plugin.on_before_send(event))
    assert event.get_result().chain == [plain]


def test_pre_send_hook_keeps_images_and_existing_at(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[At(qq="7"), FakePlain('<mention id="9"/>看图'), FakeImage(url="https://e/1.png")],
    )
    _run(plugin.on_before_send(event))

    chain = event.get_result().chain
    assert [type(c).__name__ for c in chain] == ["At", "At", "FakePlain", "FakeImage"]
    assert [getattr(c, "qq", None) for c in chain[:2]] == ["7", "9"]


def test_pure_at_chain_keeps_a_sendable_text_segment(plugin):
    """只有 At 的消息链会被 AstrBot 的 respond 阶段跳过，必须补一个可见段。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain('<mention id="5"/>')],
    )
    _run(plugin.on_before_send(event))

    chain = event.get_result().chain
    assert [type(c).__name__ for c in chain] == ["At", "FakePlain"]
    # 必须是 strip() 之后仍非空的段，否则仍会被 respond 阶段过滤掉
    assert chain[1].text.strip() != ""


def test_bot_mention_tag_is_sent_as_real_at(plugin):
    """AI 回复里的 <mention id="123"/> 必须变成真实 At 组件，而不是字面标签。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="叫他来")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain('<mention id="123"/> 来看这个')],
    )
    _run(plugin.on_bot_message_sent(event))

    chain = event.get_result().chain
    assert [type(c).__name__ for c in chain] == ["At", "FakePlain"]
    assert chain[0].qq == "123"
    assert "<mention" not in chain[1].text


def test_mention_tag_never_reaches_storage_as_literal(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain('<mention id="9"/> 你好')],
    )
    _run(plugin.on_bot_message_sent(event))

    stored = plugin.store.query_messages("group:1")[-1]
    assert "<mention" not in stored["content"]
    assert [s["type"] for s in stored["segments"]] == ["at", "text"]
    assert stored["segments"][0]["target"] == "9"


def test_mention_name_resolves_via_known_participants(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="我叫小明")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain("<mention>小明</mention>你来说")],
    )
    _run(plugin.on_bot_message_sent(event))

    chain = event.get_result().chain
    assert chain[0].qq == "u1"
    assert chain[1].text == "你来说"


def test_unknown_mention_name_becomes_readable_at_text(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain("<mention>外星人</mention>在吗")],
    )
    _run(plugin.on_bot_message_sent(event))

    chain = event.get_result().chain
    assert len(chain) == 1
    assert chain[0].text == "@外星人在吗"


def test_mention_tags_inside_existing_at_segments_are_untouched(plugin):
    """已有真实 At 组件时不能被标签改写逻辑打乱顺序。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        chain=[FakePlain("看"), At(qq="7"), FakePlain(" 这条")],
    )
    _run(plugin.on_bot_message_sent(event))

    chain = event.get_result().chain
    assert [type(c).__name__ for c in chain] == ["FakePlain", "At", "FakePlain"]
    assert chain[1].qq == "7"


def test_plain_bot_reply_is_unchanged(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="在吗")
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1",
                      chain=[FakePlain("我在")])
    _run(plugin.on_bot_message_sent(event))

    chain = event.get_result().chain
    assert len(chain) == 1
    assert chain[0].text == "我在"


# ── 出站 ──────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_bot_outgoing_message_is_recorded(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="在吗")

    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1",
                      chain=[FakePlain("我在")])
    _run(plugin.on_bot_message_sent(event))

    messages = plugin.store.query_messages("group:1")
    assert [m["type"] for m in messages] == ["received", "bot"]
    assert messages[1]["content"] == "我在"


def test_outgoing_ignored_for_unknown_session(plugin):
    event = FakeEvent(umo="elsewhere:GroupMessage:404", group_id="404",
                      chain=[FakePlain("无关")])
    _run(plugin.on_bot_message_sent(event))
    assert plugin.store.count_messages("group:404") == 0


def test_outgoing_ignored_when_disabled(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="在吗")
    plugin.config["capture_bot_messages"] = False

    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1",
                      chain=[FakePlain("我在")])
    _run(plugin.on_bot_message_sent(event))
    assert plugin.store.count_messages("group:1") == 1


def test_outgoing_empty_chain_is_ignored(plugin):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="在吗")
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1", group_id="1", chain=[])
    _run(plugin.on_bot_message_sent(event))
    assert plugin.store.count_messages("group:1") == 1


def test_terminal_send_with_at_is_not_recorded_twice(plugin, monkeypatch):
    """终端发了 "@小明 看这个"，出站钩子不能再存一条（@ 会被拆成 At 段）。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="我叫小明")

    _send(plugin, monkeypatch, "group:1", "@小明 看这个")
    target, chain = plugin.context.sent[-1]

    # 平台回声：同一条消息链再次经过出站钩子
    echo = FakeEvent(umo=target, group_id="1", chain=list(chain.chain))
    _run(plugin.on_bot_message_sent(echo))

    sent = [m for m in plugin.store.query_messages("group:1") if m["type"] == "sent"]
    assert len(sent) == 1
    assert plugin.store.count_messages("group:1") == 2  # 1 received + 1 sent


# ── 生命周期与配置 ────────────────────────────────────────


def test_sessions_are_restored_after_restart(plugin, tmp_path, monkeypatch):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="持久化")

    import main as main_module

    with monkeypatch.context() as patch:
        patch.setattr(main_module, "_resolve_data_dir", lambda: str(tmp_path))
        reloaded = main_module.Plugin(FakeContext(), {})
    assert "group:1" in reloaded.session_index
    assert reloaded.store.count_messages("group:1") == 1


def test_max_log_from_config_is_applied(tmp_path, monkeypatch):
    _install_fake_astrbot()
    import importlib

    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "_resolve_data_dir", lambda: str(tmp_path))
    instance = main.Plugin(FakeContext(), {"max_log_per_session": 2})

    for index in range(5):
        inbound(instance, umo="aiocqhttp:GroupMessage:1", group_id="1",
                sender_id="u1", sender_name="小明", text=f"m{index}")
    assert instance.store.count_messages("group:1") == 2


def test_invalid_config_value_falls_back(plugin, tmp_path, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "_resolve_data_dir", lambda: str(tmp_path / "x"))
    instance = main_module.Plugin(FakeContext(), {"max_log_per_session": "垃圾值"})
    assert instance.max_log >= 1


def test_terminate_clears_state(plugin):
    plugin._recent_sends["group:1"] = ("x", 0.0)
    _run(plugin.terminate())
    assert plugin._recent_sends == {}
    assert plugin._sse_clients == {}


# ── 终端发送：手打 @ 也要变成真 @ ────────────────────────


def _send(plugin, monkeypatch, session_id, message, segments=None):
    """驱动 api_send，绕开 Quart 的 request 全局。"""
    import main as main_module

    body = {"session_id": session_id, "message": message, "segments": segments}

    async def get_json(*args, **kwargs):
        return body

    monkeypatch.setattr(
        main_module,
        "request",
        types.SimpleNamespace(get_json=get_json, args={}, method="POST"),
        raising=False,
    )
    return _run(plugin.api_send())


def test_terminal_send_turns_bare_at_into_at_component(plugin, monkeypatch):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="我叫小明")

    _send(plugin, monkeypatch, "group:1", "@小明 看这个")

    target, chain = plugin.context.sent[-1]
    assert target == "aiocqhttp:GroupMessage:1"
    assert [type(c).__name__ for c in chain.chain] == ["At", "FakePlain"]
    assert chain.chain[0].qq == "u1"
    assert chain.chain[1].text == " 看这个"


def test_terminal_send_keeps_plain_text_when_at_unresolvable(plugin, monkeypatch):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    _send(plugin, monkeypatch, "group:1", "@查无此人 hi")

    _, chain = plugin.context.sent[-1]
    assert [type(c).__name__ for c in chain.chain] == ["FakePlain"]
    assert chain.chain[0].text == "@查无此人 hi"


def test_sent_row_is_written_before_platform_send(plugin, monkeypatch):
    """sent 行必须在 send_message **之前**落库。

    否则平台在那个 await 里回推时，库里什么都查不到 → 同一条记两份。
    """
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")

    context = plugin.context
    original = context.send_message
    seen_at_send = {}

    async def inspect_then_send(target, chain):
        seen_at_send["rows"] = [
            m["content"] for m in plugin.store.query_messages("group:111")
        ]
        await original(target, chain)

    monkeypatch.setattr(context, "send_message", inspect_then_send)

    _send(plugin, monkeypatch, "group:111", "先入库")

    assert "先入库" in seen_at_send["rows"], (
        f"发送时库里还没有 sent 行: {seen_at_send['rows']}"
    )


def test_failed_send_rolls_back_prewritten_row(plugin, monkeypatch):
    """发送失败要把预写的记录删掉，不能在终端留一条没发出去的消息。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")
    before = plugin.store.count_messages("group:111")

    async def boom(target, chain):
        raise RuntimeError("协议端掉线")

    monkeypatch.setattr(plugin.context, "send_message", boom)

    response = _send(plugin, monkeypatch, "group:111", "发不出去")

    assert response[1] == 500, response
    assert plugin.store.count_messages("group:111") == before, "失败后残留了预写的 sent 行"
    assert "group:111" not in plugin._recent_sends, "失败后没有清掉乐观锁"


def test_terminal_send_stores_resolved_segments(plugin, monkeypatch):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="我叫小明")

    _send(plugin, monkeypatch, "group:1", "@小明 看这个")

    stored = plugin.store.query_messages("group:1")[-1]
    assert stored["type"] == "sent"
    assert [s["type"] for s in stored["segments"]] == ["at", "text"]
    assert stored["segments"][0]["target"] == "u1"


# ── 宿主兼容 ──────────────────────────────────────────────


def test_plugin_loads_on_host_without_decorating_hook(tmp_path, monkeypatch):
    """老版本 AstrBot 没有 filter.on_decorating_result，插件必须照样能加载。"""
    _install_fake_astrbot()
    event_mod = sys.modules["astrbot.api.event"]
    monkeypatch.delattr(event_mod.filter, "on_decorating_result", raising=False)

    sys.modules.pop("main", None)
    import importlib

    main = importlib.import_module("main")
    monkeypatch.setattr(main, "_resolve_data_dir", lambda: str(tmp_path))
    instance = main.Plugin(FakeContext(), {})

    # 钩子被降级成空装饰器后，方法仍然存在但不再被注册
    assert not getattr(instance.on_before_send, "_is_decorating_result", False)


# ── 自动发现：平台群/好友 + AstrBot 历史导入 ──────────────


def _plugin_with_platform(groups=None, friends=None, config=None):
    """建一个带假平台实例的插件，返回 (plugin, platform)。"""
    _install_fake_astrbot()
    import importlib
    import tempfile

    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    main._resolve_data_dir = lambda: tempfile.mkdtemp()

    platform = FakePlatform(
        "aiocqhttp-inst",
        groups=groups if groups is not None else [
            {"group_id": 111, "group_name": "测试群"},
            {"group_id": 222, "group_name": "另一个群"},
        ],
        friends=friends if friends is not None else [{"user_id": 9, "nickname": "好友甲"}],
    )
    ctx = FakeContext()
    ctx.platform_manager = types.SimpleNamespace(get_insts=lambda: [platform])
    return main.Plugin(ctx, config or {}), platform, ctx


def test_discover_creates_group_sessions_without_any_message():
    """群无需等新消息，启动就建好会话。"""
    plugin, _, _ = _plugin_with_platform()

    result = _run(plugin.discover_and_import())

    assert "group:111" in plugin.session_index
    assert "group:222" in plugin.session_index
    assert plugin.session_meta["group:111"]["peer_name"] == "测试群"
    assert result["groups_created"] == 2


def test_discover_sets_adapter_name_not_instance_id():
    """platform 字段必须是适配器名，否则联系人关联与 @ 解析会对不上。"""
    plugin, _, _ = _plugin_with_platform()

    _run(plugin.discover_and_import())

    meta = plugin.session_meta["group:111"]
    assert meta["platform"] == "aiocqhttp"
    assert meta["platform_id"] == "aiocqhttp-inst"
    assert meta["send_target"] == "aiocqhttp-inst:GroupMessage:111"


def test_discover_skips_inactive_friends():
    """好友按活跃度：没有历史记录就不建会话。"""
    plugin, _, ctx = _plugin_with_platform(friends=[{"user_id": 9, "nickname": "好友甲"}])

    _run(plugin.discover_and_import())

    assert not any(key.startswith("aiocqhttp-inst:FriendMessage") for key in plugin.session_index)


def test_discover_creates_active_friend_from_history():
    """好友有 AstrBot 历史就说明活跃过，要建会话。"""
    plugin, _, ctx = _plugin_with_platform(friends=[{"user_id": 9, "nickname": "好友甲"}])
    ctx.message_history_manager.seed(
        "aiocqhttp-inst", "aiocqhttp-inst:FriendMessage:9",
        [{"id": 3, "created_at": 1700000000, "sender_id": "9", "sender_name": "好友甲",
          "content": {"type": "user", "message": [{"type": "plain", "text": "老朋友"}]}}],
    )

    _run(plugin.discover_and_import())

    key = "aiocqhttp-inst:FriendMessage:9"
    assert key in plugin.session_index
    assert plugin.session_meta[key]["peer_name"] == "好友甲"


def test_discover_can_be_disabled():
    plugin, platform, _ = _plugin_with_platform(config={"auto_discover_sessions": False})

    _run(plugin.discover_and_import())

    assert plugin.session_index == []
    assert platform.calls == []


def test_import_history_maps_messages_and_is_idempotent():
    """同一批历史导入两次不能重复入库。"""
    plugin, _, ctx = _plugin_with_platform()
    ctx.message_history_manager.seed(
        "aiocqhttp-inst", "aiocqhttp-inst:GroupMessage:111",
        [
            {"id": 1, "created_at": 1700000000, "sender_id": "u1", "sender_name": "小明",
             "content": {"type": "user", "message": [{"type": "plain", "text": "第一条"}]}},
            {"id": 2, "created_at": 1700000060, "sender_id": "bot", "sender_name": "bot",
             "content": {"type": "bot", "message": [{"type": "plain", "text": "回复"}]}},
        ],
    )

    _run(plugin.discover_and_import())
    first = [m["content"] for m in plugin.store.query_messages("group:111")]
    assert first == ["第一条", "回复"]
    assert [m["type"] for m in plugin.store.query_messages("group:111")] == ["received", "bot"]

    _run(plugin.discover_and_import())
    second = [m["content"] for m in plugin.store.query_messages("group:111")]
    assert second == first, "重复导入会破坏历史"


def test_import_history_incremental_after_watermark():
    """水位之后新增的旧记录要能补进来。"""
    plugin, _, ctx = _plugin_with_platform()
    manager = ctx.message_history_manager
    manager.seed(
        "aiocqhttp-inst", "aiocqhttp-inst:GroupMessage:111",
        [{"id": 1, "created_at": 1700000000, "sender_id": "u1", "sender_name": "小明",
          "content": {"type": "user", "message": [{"type": "plain", "text": "旧"}]}}],
    )
    _run(plugin.discover_and_import())
    assert plugin.store.count_messages("group:111") == 1

    manager.seed(
        "aiocqhttp-inst", "aiocqhttp-inst:GroupMessage:111",
        [
            {"id": 1, "created_at": 1700000000, "sender_id": "u1", "sender_name": "小明",
             "content": {"type": "user", "message": [{"type": "plain", "text": "旧"}]}},
            {"id": 2, "created_at": 1700000100, "sender_id": "u1", "sender_name": "小明",
             "content": {"type": "user", "message": [{"type": "plain", "text": "新"}]}},
        ],
    )
    _run(plugin.discover_and_import())

    contents = [m["content"] for m in plugin.store.query_messages("group:111")]
    assert contents == ["旧", "新"]


def test_import_history_pages_through_all_rows():
    """超过一页的历史必须全量导入：只取 page=1 会漏掉更早的记录，
    而水位一旦推到最新 id，那些记录就永远补不回来了。"""
    plugin, _, ctx = _plugin_with_platform()
    manager = ctx.message_history_manager
    manager.seed(
        "aiocqhttp-inst", "aiocqhttp-inst:GroupMessage:111",
        [
            {"id": i, "created_at": 1700000000 + i, "sender_id": "u1", "sender_name": "小明",
             "content": {"type": "user", "message": [{"type": "plain", "text": f"m{i}"}]}}
            for i in range(1, 1201)
        ],
    )
    plugin.max_log = 5000
    plugin.store.max_log = 5000

    _run(plugin.discover_and_import())

    assert plugin.store.count_messages("group:111") == 1200
    assert plugin.store.has_astrbot_message("group:111", 1), "最早的一条也被导入了"
    assert plugin.store.get_import_watermark(
        "aiocqhttp-inst", "aiocqhttp-inst:GroupMessage:111"
    ) == 1200
    # 第二遍不再重复导入
    _run(plugin.discover_and_import())
    assert plugin.store.count_messages("group:111") == 1200


def test_high_watermark_does_not_block_older_history():
    """水位被写高过（旧版本只取 page=1 就推到底）时，更早的历史仍要能补进来。

    这是「没有自动同步消息」的主因：库里一行都没有，水位却已经是最新 id，
    旧实现拿它当过滤条件，于是历史永远导不进来。
    """
    plugin, _, ctx = _plugin_with_platform()
    manager = ctx.message_history_manager
    scope = "aiocqhttp-inst:GroupMessage:111"
    manager.seed(
        "aiocqhttp-inst", scope,
        [
            {"id": i, "created_at": 1700000000 + i, "sender_id": "u1", "sender_name": "小明",
             "content": {"type": "user", "message": [{"type": "plain", "text": f"m{i}"}]}}
            for i in range(1, 6)
        ],
    )
    # 模拟旧版本遗留的水位：已经推到 5，但一条都没入库
    plugin.store.set_import_watermark("aiocqhttp-inst", scope, 5)
    assert plugin.store.count_messages("group:111") == 0

    result = _run(plugin.discover_and_import())

    assert result["history_imported"] == 5, result
    assert plugin.store.count_messages("group:111") == 5
    assert plugin.store.has_astrbot_message("group:111", 1), "更早的记录仍然没补进来"


def test_legacy_watermark_key_is_ignored():
    """旧格式水位键（不带版本号）必须读不到。

    否则升级后仍然拿着被污染的旧水位，历史还是导不进来。
    """
    plugin, _, _ = _plugin_with_platform()
    scope = "aiocqhttp-inst:GroupMessage:111"
    conn = plugin.store.connect()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (f"history_import:aiocqhttp-inst:{scope}", "999"),
        )
        conn.commit()
    finally:
        conn.close()

    assert plugin.store.get_import_watermark("aiocqhttp-inst", scope) == 0
    # 新键带版本号，和旧键不会互相覆盖
    plugin.store.set_import_watermark("aiocqhttp-inst", scope, 7)
    assert plugin.store.get_import_watermark("aiocqhttp-inst", scope) == 7


def test_import_history_survives_missing_manager():
    """老版本宿主没有 message_history_manager 时不能崩。"""
    plugin, _, ctx = _plugin_with_platform()
    del ctx.message_history_manager

    result = _run(plugin.discover_and_import())

    assert "group:111" in plugin.session_index
    assert result["history_imported"] == 0


def test_platform_failure_does_not_break_discovery():
    """协议端不支持 get_group_list 时静默降级，其它来源照常。"""
    plugin, platform, _ = _plugin_with_platform()

    async def boom(action, **kwargs):
        raise RuntimeError("协议端不支持")

    platform.get_client = lambda: types.SimpleNamespace(api=types.SimpleNamespace(call_action=boom))

    result = _run(plugin.discover_and_import())

    assert plugin.session_index == []
    assert result["errors"], "失败要留在结果里，便于排障"


def test_discover_keeps_pinned_and_custom_names():
    """自动发现不能覆盖用户置顶/改名。"""
    plugin, _, _ = _plugin_with_platform()
    plugin._record_inbound(FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                                     sender_id="u1", sender_name="小明", text="在吗"))
    plugin.store.set_pinned("group:111", True)
    plugin.store.rename_session("group:111", "我的自定义群名")
    plugin.session_meta["group:111"]["peer_name"] = "我的自定义群名"

    _run(plugin.discover_and_import())

    row = {s["id"]: s for s in plugin.store.load_sessions()}["group:111"]
    assert row["pinned"] is True
    assert row["peer_name"] == "我的自定义群名"


# ── 入站乐观锁：同一消息不重复入库 ────────────────────────


def test_self_inbound_message_is_recorded_once(plugin):
    """平台把 Bot 自己发的消息当 received 回推时，同一条不能被记两次。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗", self_id="bot-1")
    before = plugin.store.count_messages("group:111")

    for _ in range(2):  # 同样的回推来两次
        inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                sender_id="bot-1", sender_name="Bot", text="还真是", self_id="bot-1")

    # 不忽略 Bot 自己的消息：第一次要记下来，只吞掉重复的
    assert plugin.store.count_messages("group:111") == before + 1
    assert [m["content"] for m in plugin.store.query_messages("group:111")][-1] == "还真是"


def test_self_inbound_private_message_is_recorded_once(plugin):
    inbound(plugin, umo="aiocqhttp-inst:FriendMessage:9", sender_id="9", sender_name="好友",
            text="你好", self_id="bot-1")
    key = plugin.session_index[0]
    before = plugin.store.count_messages(key)

    for _ in range(3):
        inbound(plugin, umo="aiocqhttp-inst:FriendMessage:9", sender_id="bot-1",
                sender_name="Bot", text="我在", self_id="bot-1")

    assert plugin.store.count_messages(key) == before + 1


def test_other_users_are_still_recorded(plugin):
    """正常用户的入站消息不能被误伤。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="你好", self_id="bot-1")
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u2", sender_name="小红", text="大家好", self_id="bot-1")
    assert plugin.store.count_messages("group:111") == 2


def test_same_user_repeating_same_text_is_kept(plugin):
    """用户真的连发两次同样的话，不能当成重复吞掉（时间不同）。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="刷屏", self_id="bot-1")
    second = FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                       sender_id="u1", sender_name="小明", text="刷屏", self_id="bot-1")
    second.message_obj.timestamp = 1700000000.0 + 60  # 一分钟后
    plugin._record_inbound(second)

    assert plugin.store.count_messages("group:111") == 2


def test_terminal_send_then_platform_echo_is_not_duplicated(plugin, monkeypatch):
    """截图场景：终端发一条，平台把它回推 → 只能出现一条。"""
    import time

    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")
    _send(plugin, monkeypatch, "group:111", "还真是")

    # 平台回推：sender 是 Bot 自己，时间在发送后 1~2 秒
    now = time.time()
    for offset in (1.0, 2.0):
        echo = FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                         sender_id="bot-1", sender_name="Bot", text="还真是")
        echo.message_obj.timestamp = now + offset
        plugin._record_inbound(echo)

    rows = [m for m in plugin.store.query_messages("group:111") if m["content"] == "还真是"]
    assert len(rows) == 1, f"重复了 {len(rows)} 条"
    assert rows[0]["type"] == "sent"


def test_echo_arriving_before_sent_row_is_deduped(plugin, monkeypatch):
    """平台回推可能比 api_send 入库更早（就发生在 send_message 的 await 里）。

    那一刻库里还没有 sent 行，只靠 has_recent_duplicate 一定查不到，
    同一条会被记成 received + sent 两份。内存乐观锁必须覆盖这个窗口。
    """
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")

    context = plugin.context
    original = context.send_message

    async def send_then_echo(target, chain):
        await original(target, chain)
        # 库里此时还没有 sent 行，模拟平台立刻回推 Bot 自己的消息
        plugin._record_inbound(
            FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                      sender_id="bot-1", sender_name="Bot", text="重复吗", self_id="bot-1")
        )

    monkeypatch.setattr(context, "send_message", send_then_echo)

    _send(plugin, monkeypatch, "group:111", "重复吗")

    rows = [m for m in plugin.store.query_messages("group:111") if m["content"] == "重复吗"]
    assert len(rows) == 1, f"重复了 {len(rows)} 条"
    assert rows[0]["type"] == "sent", rows[0]["type"]


def test_echo_releases_optimistic_lock(plugin, monkeypatch):
    """回声到达后必须自动解锁：锁留在窗口里会继续误吞之后的消息。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")
    _send(plugin, monkeypatch, "group:111", "解锁测试")
    assert "group:111" in plugin._recent_sends, "发送后应当持有乐观锁"

    plugin._record_inbound(
        FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                  sender_id="bot-1", sender_name="Bot", text="解锁测试", self_id="bot-1")
    )

    assert "group:111" not in plugin._recent_sends, "回声命中后乐观锁没有释放"
    rows = [m for m in plugin.store.query_messages("group:111") if m["content"] == "解锁测试"]
    assert len(rows) == 1


def test_echo_with_at_prefix_still_matches(plugin, monkeypatch):
    """回声把 At 段折回文本（"@小明 收到"）时也要能对上锁。"""
    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在")
    _send(plugin, monkeypatch, "group:111", "@小明 收到")

    plugin._record_inbound(
        FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                  sender_id="bot-1", sender_name="Bot", text="@小明 收到", self_id="bot-1")
    )

    rows = [m for m in plugin.store.query_messages("group:111") if "收到" in m["content"]]
    assert len(rows) == 1, f"重复了 {len(rows)} 条"


def test_other_users_duplicates_are_always_kept(plugin):
    """去重只对 Bot 自己生效：别人在窗口内连发相同内容必须都保留。"""
    import time

    inbound(plugin, umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
            sender_id="u1", sender_name="小明", text="在吗")
    now = time.time()
    for offset in (0.5, 1.0, 1.5):  # 1.5 秒内连发三次相同内容
        ev = FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                       sender_id="u1", sender_name="小明", text="刷屏")
        ev.message_obj.timestamp = now + offset
        plugin._record_inbound(ev)

    rows = [m for m in plugin.store.query_messages("group:111") if m["content"] == "刷屏"]
    assert len(rows) == 3, f"只保留了 {len(rows)} 条，用户的重复消息被吞了"


# ── @ 标记改写：只含 Reply/At 的链不能被 respond 阶段丢掉 ──────────


def test_mention_rewrite_keeps_header_only_chain_deliverable():
    """上游 respond 阶段会跳过「只含 Reply/At」的链（#2670）。

    引用 + @ 改写后正好只剩这两类，必须补一个零宽明文段，否则消息静默丢失。
    """
    from astrbot.api.message_components import Plain, Reply

    plugin, _, _ = _plugin_with_platform()
    event = FakeEvent(
        "aiocqhttp-inst:GroupMessage:111",
        group_id="111",
        chain=[Reply(id="9", sender_id=1), Plain('<mention id="123"/>')],
    )

    assert plugin._rewrite_mentions(event) is True

    chain = event.get_result().chain
    assert [type(item).__name__ for item in chain] == ["Reply", "At", "FakePlain"]
    assert chain[-1].text == "\u200b"


def test_mention_rewrite_uses_at_all_for_everyone():
    """@全体成员 要用宿主的 AtAll，不能塞一个 qq="all" 的普通 At。"""
    from astrbot.api.message_components import Plain

    plugin, _, _ = _plugin_with_platform()
    event = FakeEvent(
        "aiocqhttp-inst:GroupMessage:111",
        group_id="111",
        chain=[Plain("<mention>全体成员</mention> 开会")],
    )

    plugin._rewrite_mentions(event)

    chain = event.get_result().chain
    assert type(chain[0]).__name__ == "AtAll"
    assert chain[0].qq == "all"


def test_mention_rewrite_leaves_plain_angle_brackets_alone():
    """`<atom>` 这类正文不是标签，改写阶段必须原样放过。"""
    from astrbot.api.message_components import Plain

    plugin, _, _ = _plugin_with_platform()
    event = FakeEvent(
        "aiocqhttp-inst:GroupMessage:111",
        group_id="111",
        chain=[Plain("<attachment> 附件在这")],
    )

    assert plugin._rewrite_mentions(event) is False
    assert event.get_result().chain[0].text == "<attachment> 附件在这"


def test_mention_rewrite_respects_tag_and_bare_at_flags():
    """convert_mention_tags / parse_bare_at 必须互相独立。"""
    from astrbot.api.message_components import Plain

    plugin, _, _ = _plugin_with_platform()
    plugin._record_inbound(
        FakeEvent(umo="aiocqhttp-inst:GroupMessage:111", group_id="111",
                  sender_id="u1", sender_name="小明", text="我叫小明")
    )

    # 只关标签：手打 @ 仍然要解析
    only_bare = FakeEvent("aiocqhttp-inst:GroupMessage:111", group_id="111",
                          chain=[Plain("@小明 看这个")])
    assert plugin._rewrite_mentions(only_bare, allow_tags=False, allow_bare=True) is True
    assert [type(item).__name__ for item in only_bare.get_result().chain] == ["At", "FakePlain"]

    # 只关手打 @：标签仍然要解析
    only_tags = FakeEvent("aiocqhttp-inst:GroupMessage:111", group_id="111",
                          chain=[Plain('<mention id="42"/> 看这个')])
    assert plugin._rewrite_mentions(only_tags, allow_tags=True, allow_bare=False) is True
    assert [type(item).__name__ for item in only_tags.get_result().chain] == ["At", "FakePlain"]


# ── 平台列表 / 版本号 ────────────────────────────────────────


def test_api_platforms_returns_real_ids():
    """宿主里 meta 是方法，不调用就只能拿到空串，前端平台下拉框会永远空着。"""
    plugin, _, _ = _plugin_with_platform()

    payload = _run(plugin.api_platforms())

    assert payload["platforms"] == [
        {"id": "aiocqhttp-inst", "name": "aiocqhttp", "adapter": "aiocqhttp"}
    ]


def test_health_version_matches_metadata():
    """api_health 的版本号必须来自 metadata.yaml，不能再硬编码。"""
    import main as main_module

    payload = _run(_plugin_with_platform()[0].api_health())

    assert payload["version"] == main_module.PLUGIN_VERSION
    assert payload["version"] != "unknown"


def test_health_exposes_load_images_default_off():
    """图片默认不自动加载：health 必须把默认值 false 告诉前端。"""
    plugin, _, _ = _plugin_with_platform()

    assert _run(plugin.api_health())["load_images"] is False

    # 配置里显式打开时如实反映
    plugin.config["load_images"] = True
    assert _run(plugin.api_health())["load_images"] is True


# ── 引用回复 ─────────────────────────────────────────────


def test_terminal_send_with_reply_builds_reply_component(plugin, monkeypatch):
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    _send(
        plugin, monkeypatch, "group:1", "同意",
        [{"type": "reply", "target": "m-42", "text": "原消息"}],
    )

    _, chain = plugin.context.sent[-1]
    assert [type(c).__name__ for c in chain.chain] == ["Reply", "FakePlain"]
    assert chain.chain[0].id == "m-42"
    assert chain.chain[1].text == "同意"


def test_terminal_send_with_reply_is_not_recorded_twice(plugin, monkeypatch):
    """引用回复经过出站钩子时，指纹必须把 reply 折成同一个 key。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    _send(
        plugin, monkeypatch, "group:1", "同意",
        [{"type": "reply", "target": "m-42", "text": "原消息"}],
    )
    target, chain = plugin.context.sent[-1]

    echo = FakeEvent(umo=target, group_id="1", chain=list(chain.chain))
    _run(plugin.on_bot_message_sent(echo))

    sent = [m for m in plugin.store.query_messages("group:1") if m["type"] == "sent"]
    assert len(sent) == 1, "引用回复被平台回声记成了两份"


def test_terminal_send_without_reply_component_degrades_to_text(plugin, monkeypatch):
    """老宿主没有 Reply 组件时，消息必须照常发出去，不能 500。"""
    import sys

    comp = sys.modules["astrbot.api.message_components"]
    monkeypatch.delattr(comp, "Reply", raising=False)
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    payload = _send(plugin, monkeypatch, "group:1", "同意",
                    [{"type": "reply", "target": "m-42", "text": "原消息"}])

    assert payload.get("status") == "ok", payload
    _, chain = plugin.context.sent[-1]
    assert [type(c).__name__ for c in chain.chain] == ["FakePlain"]
    assert chain.chain[0].text == "同意"


def test_reply_only_chain_gets_zero_width_text(plugin, monkeypatch):
    """只引用不写字时，消息链只剩 Reply，宿主会静默丢弃，必须补明文段。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    _send(plugin, monkeypatch, "group:1", "",
          [{"type": "reply", "target": "m-42", "text": "原消息"}])

    _, chain = plugin.context.sent[-1]
    names = [type(c).__name__ for c in chain.chain]
    assert names == ["Reply", "FakePlain"], names
    assert chain.chain[1].text == "\u200b"


# ── 会话列表分页 ──────────────────────────────────────────


def _call_sessions(plugin, monkeypatch, **args):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "request",
        types.SimpleNamespace(args={k: str(v) for k, v in args.items()}, method="GET"),
        raising=False,
    )
    return _run(plugin.api_sessions())


def _seed_sessions(plugin, count):
    for index in range(1, count + 1):
        inbound(plugin, umo=f"aiocqhttp:GroupMessage:{index}", group_id=str(index),
                sender_id="u1", sender_name="小明", text=f"m{index}")


def test_sessions_supports_limit_and_offset(plugin, monkeypatch):
    _seed_sessions(plugin, 5)

    first = _call_sessions(plugin, monkeypatch, limit=2, offset=0)
    second = _call_sessions(plugin, monkeypatch, limit=2, offset=2)

    assert first["total"] == 5
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert len(first["sessions"]) == 2 and len(second["sessions"]) == 2
    ids_first = {item["id"] for item in first["sessions"]}
    ids_second = {item["id"] for item in second["sessions"]}
    assert not ids_first & ids_second, "分页返回了重复会话"


def test_sessions_last_page_has_no_more(plugin, monkeypatch):
    _seed_sessions(plugin, 3)

    page = _call_sessions(plugin, monkeypatch, limit=2, offset=2)

    assert len(page["sessions"]) == 1
    assert page["has_more"] is False


def test_sessions_without_limit_returns_everything(plugin, monkeypatch):
    """不传 limit 时保持旧行为：一次返回全部，避免破坏已有调用方。"""
    _seed_sessions(plugin, 3)

    payload = _call_sessions(plugin, monkeypatch)

    assert len(payload["sessions"]) == 3
    assert payload["has_more"] is False


# ── 转发内容按需拉取 ──────────────────────────────────────


class _ForwardClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self.error:
            raise self.error
        return self.payload


class _ForwardInst:
    def __init__(self, client=None, error=None):
        self.client = client
        self.error = error

    def meta(self):
        return types.SimpleNamespace(id="aiocqhttp-inst", name="aiocqhttp")

    def get_client(self):
        if self.error:
            raise self.error
        return self.client


def _call_forward(plugin, monkeypatch, **args):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "request",
        types.SimpleNamespace(args={k: str(v) for k, v in args.items()}, method="GET"),
        raising=False,
    )
    return _run(plugin.api_forward())


def _plugin_with_forward_inst(plugin, inst):
    plugin.context.platform_manager.get_insts = lambda: [inst]
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="转发来了")
    return plugin


def test_forward_endpoint_fetches_nested_messages(plugin, monkeypatch):
    client = _ForwardClient(payload={"messages": [
        {"sender": {"nickname": "小明", "user_id": 42},
         "message": [{"type": "text", "data": {"text": "早"}}]},
    ]})
    _plugin_with_forward_inst(plugin, _ForwardInst(client=client))

    payload = _call_forward(plugin, monkeypatch, session_id="group:1", id="f-1")

    assert payload["available"] is True
    assert payload["messages"][0]["segments"] == [{"type": "text", "text": "早"}]
    action, kwargs = client.calls[0]
    assert action == "get_forward_msg"
    assert kwargs.get("message_id") == "f-1"


def test_forward_endpoint_degrades_gracefully(plugin, monkeypatch):
    """协议端不支持拉取时返回空内容，前端显示「内容不可用」而不是报错。"""
    _plugin_with_forward_inst(plugin, _ForwardInst(error=RuntimeError("协议端不支持")))

    payload = _call_forward(plugin, monkeypatch, session_id="group:1", id="f-1")

    assert payload["available"] is False
    assert payload["messages"] == []


def test_forward_endpoint_rejects_unknown_session(plugin, monkeypatch):
    response = _call_forward(plugin, monkeypatch, session_id="group:404", id="f-1")
    assert response[1] == 404


def test_terminal_send_ignores_synthetic_reply_id(plugin, monkeypatch):
    """astrbot:<id> 是历史导入的本地合成 id，拿去引用平台会报错。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    _send(
        plugin, monkeypatch, "group:1", "同意",
        [{"type": "reply", "target": "astrbot:42", "text": "原消息"}],
    )

    _, chain = plugin.context.sent[-1]
    assert [type(c).__name__ for c in chain.chain] == ["FakePlain"]


def test_reply_echo_without_reply_component_is_not_duplicated(plugin, monkeypatch):
    """有的适配器回推时会剥掉 Reply，指纹不能依赖它，否则同一条记两份。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息")

    _send(
        plugin, monkeypatch, "group:1", "同意",
        [{"type": "reply", "target": "m-42", "text": "原消息"}],
    )
    target, _ = plugin.context.sent[-1]

    echo = FakeEvent(umo=target, group_id="1", chain=[FakePlain("同意")])
    _run(plugin.on_bot_message_sent(echo))

    sent = [m for m in plugin.store.query_messages("group:1") if m["type"] == "sent"]
    assert len(sent) == 1


def test_inbound_reply_is_enriched_with_quoted_message(plugin):
    """收到「回复某条」时，片段里要带上被引用消息的昵称与内容，否则前端只能显示 [引用]。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="原消息", message_id="m-1")

    plugin._record_inbound(FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        sender_id="u2", sender_name="小红", text="同意",
        chain=[Reply(id="m-1"), FakePlain("同意")], message_id="m-2",
    ))

    stored = plugin.store.query_messages("group:1")[-1]
    reply = stored["segments"][0]
    assert reply["type"] == "reply"
    assert reply["quote"] == {"sender": "小明", "text": "原消息"}


def test_inbound_reply_to_missing_message_has_no_quote(plugin):
    plugin._record_inbound(FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        sender_id="u2", sender_name="小红", text="同意",
        chain=[Reply(id="m-404"), FakePlain("同意")], message_id="m-2",
    ))

    stored = plugin.store.query_messages("group:1")[-1]
    assert "quote" not in stored["segments"][0]


def test_inbound_forward_expands_single_node(plugin):
    """单个 Node 就是一条聊天记录，内容要展开存下来。"""
    plugin._record_inbound(FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        sender_id="u1", sender_name="小明", text="",
        chain=[Node("小红", "1002", [FakePlain("早")])], message_id="m-9",
    ))

    segment = plugin.store.query_messages("group:1")[-1]["segments"][0]
    assert segment["type"] == "forward"
    assert segment["messages"][0]["segments"] == [{"type": "text", "text": "早"}]


def test_inbound_forward_id_falls_back_to_raw_message(plugin):
    """宿主 Node 没带 id 时，从原始 CQ 码里把转发 id 捞回来，否则点开看不到内容。"""
    event = FakeEvent(
        umo="aiocqhttp:GroupMessage:1", group_id="1",
        sender_id="u1", sender_name="小明", text="",
        chain=[Node()], message_id="m-10",
    )
    event.message_obj.raw_message = {"raw_message": "[CQ:forward,id=f-10]"}

    plugin._record_inbound(event)

    segment = plugin.store.query_messages("group:1")[-1]["segments"][0]
    assert segment["id"] == "f-10"


# ── 终端发送图片：data URL 必须走 base64，不能当成文件路径 ──────

PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_terminal_send_data_url_image_is_persisted_not_base64(plugin, monkeypatch):
    """前端附件是 data URL：落盘后按文件发送，且库里不留整段 base64。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    _send(plugin, monkeypatch, "group:1", "", [
        {"type": "image", "url": PNG_DATA_URL, "name": "a.png"},
    ])

    _, chain = plugin.context.sent[-1]
    image = chain.chain[-1]
    assert type(image).__name__ == "FakeImage"
    # 落盘后的真实文件，不再是 data URL，也不是不存在的路径
    assert image.path and os.path.isfile(image.path)
    assert image.path.startswith(os.path.join(plugin.data_dir, "media"))
    with open(image.path, "rb") as handle:
        assert handle.read(4) == b"\x89PNG"

    # 库里只留文件路径：几万字符的 base64 绝不能进数据库
    stored = plugin.store.query_messages("group:1")[-1]["segments"][0]
    assert "base64" not in stored["url"]
    assert len(stored["url"]) < 512


def test_terminal_send_data_url_with_percent_encoding_is_decoded(plugin, monkeypatch):
    """data URL 可能被 URL 编码过（%3A / %2C），必须先还原再解析。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    encoded = PNG_DATA_URL.replace(":", "%3A").replace(",", "%2C").replace("/", "%2F")
    _send(plugin, monkeypatch, "group:1", "", [{"type": "image", "url": encoded}])

    _, chain = plugin.context.sent[-1]
    assert chain.chain[-1].file.startswith("base64://")


def test_terminal_send_http_image_still_uses_url(plugin, monkeypatch):
    """回归：http 图片仍走 fromURL，不要被 base64 分支吃掉。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    _send(plugin, monkeypatch, "group:1", "", [
        {"type": "image", "url": "https://example.com/a.png"},
    ])

    _, chain = plugin.context.sent[-1]
    assert chain.chain[-1].url == "https://example.com/a.png"


def test_terminal_send_local_file_image_uses_filesystem(plugin, monkeypatch, tmp_path):
    """回归：真实存在的本地路径仍走 fromFileSystem。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    image_file = tmp_path / "a.png"
    image_file.write_bytes(b"png")
    _send(plugin, monkeypatch, "group:1", "", [
        {"type": "image", "file": str(image_file)},
    ])

    _, chain = plugin.context.sent[-1]
    assert chain.chain[-1].path == str(image_file)


def test_local_path_with_percent_is_not_unquoted(plugin, monkeypatch, tmp_path):
    """本地文件名里的 %20 是字面量；只有 data URL 才需要 unquote。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    weird = tmp_path / "a%20b.png"
    weird.write_bytes(b"png")
    _send(plugin, monkeypatch, "group:1", "", [
        {"type": "image", "file": str(weird)},
    ])

    _, chain = plugin.context.sent[-1]
    assert chain.chain[-1].path == str(weird)


# ── 落盘附件的读取入口：必须防目录穿越 ──────────────────────

def _media(plugin, monkeypatch, **args):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "request",
        types.SimpleNamespace(args=args, method="GET", get_json=lambda *a, **k: None),
        raising=False,
    )
    return _run(plugin.api_media())


def test_media_file_serves_persisted_image(plugin, monkeypatch):
    """落盘附件要能读出来，否则前端拿到路径也显示不了。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")
    _send(plugin, monkeypatch, "group:1", "", [{"type": "image", "url": PNG_DATA_URL}])
    path = plugin.store.query_messages("group:1")[-1]["segments"][0]["file"]

    response = _media(plugin, monkeypatch, file=path)
    assert getattr(response, "mimetype", "").startswith("image/")


def test_media_file_rejects_path_traversal(plugin, monkeypatch):
    """?file= 不能读到 media 目录以外的任何文件（比如数据库）。"""
    response = _media(plugin, monkeypatch, file=plugin.db_path)
    payload, status = response
    assert status == 400
    assert "error" in payload


def test_media_file_rejects_relative_escape(plugin, monkeypatch):
    """../ 也要挡住。"""
    escaped = os.path.join(plugin.data_dir, "media", "..", "..", "x.png")
    payload, status = _media(plugin, monkeypatch, file=escaped)
    assert status == 400
    assert "error" in payload


def test_failed_send_discards_persisted_image(plugin, monkeypatch):
    """发送失败时，刚落盘的图片要清掉，不能留孤儿文件。"""
    inbound(plugin, umo="aiocqhttp:GroupMessage:1", group_id="1",
            sender_id="u1", sender_name="小明", text="x")

    async def boom(*args, **kwargs):
        raise RuntimeError("platform down")

    monkeypatch.setattr(plugin.context, "send_message", boom)
    _send(plugin, monkeypatch, "group:1", "", [{"type": "image", "url": PNG_DATA_URL}])

    media_dir = os.path.join(plugin.data_dir, "media")
    leftovers = os.listdir(media_dir) if os.path.isdir(media_dir) else []
    assert leftovers == []
    # 只剩入站那条，失败的 sent 行必须回滚掉
    assert [m["type"] for m in plugin.store.query_messages("group:1")] == ["received"]
