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
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


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
        def event_message_type(*args, **kwargs):
            def wrapper(fn):
                fn._is_message_listener = True
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
    """stand-in for PlatformMessageHistoryManager：按 (platform_id, user_id) 存历史。"""

    def __init__(self):
        self.rows = {}

    def seed(self, platform_id, user_id, rows):
        self.rows[(platform_id, user_id)] = list(rows)

    async def get(self, platform_id, user_id, page=1, page_size=200):
        return list(self.rows.get((platform_id, user_id), []))


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
