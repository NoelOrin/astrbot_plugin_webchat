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

    def __init__(self, url="", file=""):
        self.url = url
        self.file = file


class FakeRecord:
    def __init__(self, file=""):
        self.file = file


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
            return FakeImage(file=path)

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


def test_health_exposes_load_images_default(plugin):
    """前端启动时从 /health 读图片开关的默认值：缺省必须是关。"""
    assert _run(plugin.api_health())["load_images"] is False

    plugin.config["load_images"] = True
    assert _run(plugin.api_health())["load_images"] is True


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
