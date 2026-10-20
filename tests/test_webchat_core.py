"""webchat_core 纯逻辑层测试：片段序列化、存储、裁剪、分页、检索、会话键。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webchat_core  # noqa: E402
from webchat_core import (  # noqa: E402
    MessageStore,
    astrbot_history_to_message,
    clamp_int,
    coerce_bool,
    coerce_float,
    collect_image_urls,
    has_mention_markup,
    loads_segments,
    make_session_key,
    preview_of,
    segments_to_text,
    serialize_chain,
    session_key_for_group,
    session_key_for_private,
    text_to_segments,
)

# ── 假组件：模拟 AstrBot 消息链元素，避免依赖宿主 ──────────────────


class Plain:
    def __init__(self, text: str):
        self.text = text


class Image:
    def __init__(self, url: str = "", file: str = ""):
        self.url = url
        self.file = file


class At:
    def __init__(self, qq: str):
        self.qq = qq


class Record:
    def __init__(self, file: str = ""):
        self.file = file


class Weird:
    """未知组件类型，用于验证降级路径。"""


# ── 输入清洗 ──────────────────────────────────────────────


def test_coerce_float_handles_garbage():
    assert coerce_float("1.5") == 1.5
    assert coerce_float(None, default=7.0) == 7.0
    assert coerce_float("abc", default=2.0) == 2.0
    assert coerce_float(float("nan"), default=3.0) == 3.0
    assert coerce_float(float("inf"), default=4.0) == 4.0


def test_clamp_int_bounds():
    assert clamp_int("500", 1, 200, 50) == 200
    assert clamp_int("0", 1, 200, 50) == 1
    assert clamp_int("abc", 1, 200, 50) == 50
    assert clamp_int(None, 1, 200, 50) == 50


def test_coerce_bool_accepts_common_forms():
    assert coerce_bool("1") is True
    assert coerce_bool("true") is True
    assert coerce_bool("0") is False
    assert coerce_bool(None, default=True) is True
    assert coerce_bool("garbage", default=True) is True


# ── 会话键 ────────────────────────────────────────────────


def test_session_keys_are_stable_and_prefixed():
    assert session_key_for_group(12345) == "group:12345"
    assert session_key_for_group(" 12345 ") == "group:12345"
    assert session_key_for_private("aiocqhttp:FriendMessage:9") == "aiocqhttp:FriendMessage:9"
    assert session_key_for_private("", "9") == "private:9"
    assert session_key_for_private("", "") == ""


def test_make_session_key_prefers_group_id_for_groups():
    assert make_session_key(True, "777", "uma") == "group:777"
    assert make_session_key(False, "", "umo-x", "u1") == "umo-x"


# ── 片段序列化 ────────────────────────────────────────────


def test_serialize_chain_covers_common_components():
    segments = serialize_chain(
        [Plain("你好"), At("42"), Image(url="https://x/1.png"), Record(file="/tmp/a.wav")]
    )
    assert [item["type"] for item in segments] == ["text", "at", "image", "record"]
    assert segments[0]["text"] == "你好"
    assert segments[1]["target"] == "42"
    assert segments[2]["url"] == "https://x/1.png"


def test_serialize_chain_drops_empty_plain_and_unknown_survives():
    segments = serialize_chain([Plain(""), Plain("  "), Weird()])
    assert len(segments) == 1
    assert segments[0]["type"] == "other"
    assert segments[0]["name"] == "weird"


def test_serialize_chain_handles_none_and_broken_component():
    assert serialize_chain(None) == []
    assert serialize_chain([]) == []

    class Boom:
        @property
        def text(self):
            raise RuntimeError("boom")

    # 属性抛异常时必须降级而不是整体崩溃
    assert serialize_chain([Boom()])[0]["type"] == "other"


def test_segments_to_text_renders_placeholders():
    segments = [
        {"type": "text", "text": "看这个"},
        {"type": "image", "url": "u"},
        {"type": "at", "target": "9"},
    ]
    assert segments_to_text(segments) == "看这个[图片]@9"
    assert segments_to_text([]) == ""
    assert segments_to_text(None) == ""
    # 纯 @ 消息补的零宽空格是投递用的，不能混进正文预览
    assert segments_to_text([{"type": "text", "text": "\u200b"}]) == ""
    assert (
        segments_to_text([{"type": "at", "target": "9"}, {"type": "text", "text": "\u200b"}])
        == "@9"
    )


def test_at_component_keeps_nickname_from_text_attribute():
    """宿主 At 子类可能用 text 承载昵称，不能退化成只显示 QQ 号。"""

    class TextAt:
        def __init__(self, qq, text):
            self.qq = qq
            self.text = text

    segment = serialize_chain([TextAt("123456", "小明")])[0]
    assert segment == {"type": "at", "target": "123456", "text": "小明"}


def test_mention_tag_with_id_is_converted():
    """模型写出的 <mention id="42"/> 必须变成 at 片段，而不是当正文发出去。"""
    assert text_to_segments('<mention id="42"/> 在吗') == [
        {"type": "at", "target": "42"},
        {"type": "text", "text": " 在吗"},
    ]
    assert text_to_segments('<mention qq="99"/>') == [{"type": "at", "target": "99"}]
    assert text_to_segments("<mention:88>你好") == [
        {"type": "at", "target": "88"},
        {"type": "text", "text": "你好"},
    ]
    assert text_to_segments("<mention id='7'/>") == [{"type": "at", "target": "7"}]


def test_mention_tag_is_detected_without_building_segments():
    assert has_mention_markup('<mention id="1"/>x')
    assert has_mention_markup("<at id='9'/>")
    assert not has_mention_markup("普通消息 @某人")


def test_mention_paired_with_name_resolves_to_id():
    def resolve(name):
        return {"小明": "u1"}.get(name)

    assert text_to_segments("<mention>小明</mention>你好", resolve) == [
        {"type": "at", "target": "u1", "text": "小明"},
        {"type": "text", "text": "你好"},
    ]


def test_unresolvable_mention_name_degrades_to_readable_text():
    """查不到 QQ 号时宁可留下 @昵称，也绝不能把尖括号标签发给用户。"""
    assert text_to_segments("<mention>外星人</mention>你好", lambda name: None) == [
        {"type": "text", "text": "@外星人你好"}
    ]
    assert text_to_segments("<mention>外星人</mention>你好") == [
        {"type": "text", "text": "@外星人你好"}
    ]


def test_bare_at_is_converted_only_when_resolvable():
    def resolve(name):
        return {"小明": "u1", "123456": "123456"}.get(name)

    assert text_to_segments("@小明 你好", resolve) == [
        {"type": "at", "target": "u1", "text": "小明"},
        {"type": "text", "text": " 你好"},
    ]
    # 查不到就保持原样，避免把正文里的 @ 全改坏
    assert text_to_segments("@查无此人 你好", resolve) == [
        {"type": "text", "text": "@查无此人 你好"}
    ]
    # 邮箱、@ 后跟空格等常规写法不受影响
    assert text_to_segments("a@b.com 和 @ 一下", resolve) == [
        {"type": "text", "text": "a@b.com 和 @ 一下"}
    ]


def test_bare_at_respects_allow_flag_and_all_alias():
    def resolve(name):
        return {"小明": "u1"}.get(name)

    assert text_to_segments("@小明", resolve, allow_bare_at=False) == [
        {"type": "text", "text": "@小明"}
    ]
    assert text_to_segments("@全体成员 开会", resolve) == [
        {"type": "at", "target": "all", "text": "全体成员"},
        {"type": "text", "text": " 开会"},
    ]


def test_plain_text_passes_through_as_single_segment():
    assert text_to_segments("你好 @ 一下") == [{"type": "text", "text": "你好 @ 一下"}]
    assert text_to_segments("") == []


def test_angle_bracket_words_are_not_treated_as_mention_tags():
    """`<atom>` / `<attachment>` / `<at most 3>` 是正文，不能被当标签吞掉。"""
    for text in (
        "<atom>",
        "<attachment> 附件在这",
        "<at most 3 items> 可以吗",
        "<attempt> 失败",
        "<mentionable>",
    ):
        assert not has_mention_markup(text), text
        assert text_to_segments(text) == [{"type": "text", "text": text}], text


def test_stray_mention_tag_is_stripped_but_text_survives():
    """落单的标签本身要去掉，但同一段里的正文必须原样保留。"""
    assert text_to_segments("看看 </at> 这个") == [{"type": "text", "text": "看看  这个"}]
    assert text_to_segments("<mention>") == []
    assert text_to_segments("<at/>") == []


def test_mention_name_attribute_resolves_like_paired_form():
    assert text_to_segments('<at name="小明"/>', lambda name: {"小明": "u1"}.get(name)) == [
        {"type": "at", "target": "u1", "text": "小明"}
    ]
    # 解析不出 ID 时退化成明文
    assert text_to_segments('<at name="外星人"/>', lambda name: None) == [
        {"type": "text", "text": "@外星人"}
    ]


def test_allow_tags_false_leaves_markup_alone():
    """convert_mention_tags=false 时只处理手打 @，标签原样保留。"""
    resolve = lambda name: {"小明": "u1"}.get(name)  # noqa: E731
    assert text_to_segments("<mention id='42'/> hi", resolve, allow_tags=False) == [
        {"type": "text", "text": "<mention id='42'/> hi"}
    ]
    assert text_to_segments("@小明 hi", resolve, allow_tags=False) == [
        {"type": "at", "target": "u1", "text": "小明"},
        {"type": "text", "text": " hi"},
    ]
    assert text_to_segments(None) == []


def test_segments_json_roundtrip_is_lossless():
    segments = [{"type": "text", "text": "中文"}, {"type": "image", "url": "https://a/b.png"}]
    raw = loads_segments(__import__("json").dumps(segments, ensure_ascii=False))
    assert raw == segments


def test_loads_segments_degrades_on_bad_data():
    assert loads_segments("not json") == []
    assert loads_segments('{"a": 1}') == []
    assert loads_segments(None) == []
    assert loads_segments([{"type": "text"}, "junk"]) == [{"type": "text"}]


def test_preview_prefers_segments_then_content():
    assert preview_of({"segments": [{"type": "text", "text": "hi"}]}) == "hi"
    assert preview_of({"segments": [], "content": "fallback"}) == "fallback"
    assert preview_of({}) == "[非文本消息]"


# ── AstrBot 历史记录 → 本插件消息 ─────────────────────────


def test_astrbot_history_maps_plain_at_and_reply():
    """AstrBot platform_message_history 的 content 结构要能无损映射。"""
    message = astrbot_history_to_message(
        {
            "id": 7,
            "created_at": "2026-10-20 03:00:00",
            "sender_id": "u1",
            "sender_name": "小明",
            "content": {
                "type": "user",
                "message": [
                    {"type": "plain", "text": "你好"},
                    {"type": "at", "user_id": "9", "name": "小红"},
                    {"type": "reply", "message_id": "55", "sender_name": "张三", "text": "原文"},
                ],
            },
        }
    )
    assert message["type"] == "received"
    assert message["sender"] == "小明"
    assert message["sender_id"] == "u1"
    assert message["content"] == "你好@小红[引用 原文]"
    assert [s["type"] for s in message["segments"]] == ["text", "at", "reply"]
    assert message["segments"][1] == {"type": "at", "target": "9", "text": "小红"}
    assert message["platform_message_id"] == "astrbot:7"
    assert message["timestamp"] > 0


def test_astrbot_history_at_without_user_id_degrades_to_text():
    """历史里的 at 只有昵称、没有 user_id 时不能生成空 target 的 at 片段。"""
    message = astrbot_history_to_message(
        {
            "id": 2,
            "content": {
                "type": "user",
                "message": [
                    {"type": "at", "name": "小红"},
                    {"type": "plain", "text": "在吗"},
                ],
            },
        }
    )
    assert message["segments"] == [{"type": "text", "text": "@小红在吗"}]
    assert message["content"] == "@小红在吗"


def test_astrbot_history_bot_role_becomes_bot_message():
    message = astrbot_history_to_message(
        {"id": 1, "content": {"type": "bot", "message": [{"type": "plain", "text": "我在"}]}}
    )
    assert message["type"] == "bot"
    assert message["content"] == "我在"


def test_astrbot_history_degrades_on_bad_input():
    assert astrbot_history_to_message(None) is None
    assert astrbot_history_to_message({}) is None
    assert astrbot_history_to_message({"content": {}}) is None
    assert astrbot_history_to_message({"content": {"message": []}}) is None
    # 未知 part 类型降级成文本，不静默丢信息
    message = astrbot_history_to_message(
        {"id": 3, "content": {"type": "user", "message": [{"type": "weird"}]}}
    )
    assert message["segments"][0]["type"] == "text"


def test_astrbot_history_timestamp_parsing():
    """created_at 是 naive UTC datetime，要转成合理的 unix 秒。"""
    naive = astrbot_history_to_message(
        {"id": 1, "created_at": "2026-10-20 03:00:00", "content": {"type": "user",
         "message": [{"type": "plain", "text": "x"}]}}
    )
    assert 1_700_000_000 < naive["timestamp"] < 2_000_000_000
    # 已经是 unix 秒就直接用
    epoch = astrbot_history_to_message(
        {"id": 2, "created_at": 1_700_000_000, "content": {"type": "user",
         "message": [{"type": "plain", "text": "x"}]}}
    )
    assert epoch["timestamp"] == 1_700_000_000
    # 缺失/坏值不崩，回退当前时间
    missing = astrbot_history_to_message(
        {"id": 3, "content": {"type": "user", "message": [{"type": "plain", "text": "x"}]}}
    )
    assert missing["timestamp"] > 0


# ── 存储：建表与写入 ──────────────────────────────────────


@pytest.fixture()
def store(tmp_path):
    return MessageStore(str(tmp_path / "sub" / "t.db"), max_log=5)


def test_init_schema_is_idempotent(store):
    store.init_schema()
    store.init_schema()
    assert os.path.exists(store.db_path)


def test_upsert_session_preserves_pinned_and_created_at(store):
    store.init_schema()
    store.upsert_session("group:1", {"peer_name": "群一", "is_group": True, "group_id": "1"})
    store.set_pinned("group:1", True)
    # 再次上报不应清掉置顶
    store.upsert_session("group:1", {"peer_name": "群一改名", "is_group": True})
    session = [s for s in store.load_sessions() if s["id"] == "group:1"][0]
    assert session["pinned"] is True
    assert session["peer_name"] == "群一改名"
    assert session["created_at"] > 0


def test_upsert_session_does_not_blank_existing_fields(store):
    store.init_schema()
    store.upsert_session("group:2", {"peer_name": "原名", "platform": "aiocqhttp"})
    store.upsert_session("group:2", {"peer_name": "", "platform": ""})
    session = [s for s in store.load_sessions() if s["id"] == "group:2"][0]
    assert session["peer_name"] == "原名"
    assert session["platform"] == "aiocqhttp"


def test_add_message_returns_frontend_ready_payload(store):
    store.init_schema()
    payload = store.add_message(
        "group:1",
        {
            "type": "received",
            "sender": "小明",
            "sender_id": "1001",
            "segments": [{"type": "text", "text": "在吗"}],
            "platform": "aiocqhttp",
            "timestamp": 1000.0,
        },
    )
    assert payload["preview"] == "在吗"
    assert payload["content"] == "在吗"
    assert payload["id"] > 0
    assert payload["type"] == "received"


def test_add_message_normalizes_unknown_type(store):
    store.init_schema()
    payload = store.add_message("group:1", {"type": "nonsense", "content": "x"})
    assert payload["type"] == "received"


def test_trim_keeps_newest_by_id(store):
    store.init_schema()
    for index in range(8):
        store.add_message(
            "group:1", {"content": f"m{index}", "timestamp": 1000.0 + index}
        )
    messages = store.query_messages("group:1")
    assert [m["content"] for m in messages] == ["m3", "m4", "m5", "m6", "m7"]
    assert store.count_messages("group:1") == 5


def test_trim_uses_id_not_timestamp(store):
    """时间戳乱序时，裁剪仍按插入顺序保留最新 N 条（与游标分页同口径）。

    展示顺序由 timestamp 决定，所以这里只断言“保留了哪几条”。
    """
    store.init_schema()
    for index in range(7):
        store.add_message("group:1", {"content": f"m{index}", "timestamp": 2000.0 - index})
    contents = {m["content"] for m in store.query_messages("group:1")}
    assert contents == {"m2", "m3", "m4", "m5", "m6"}


# ── 存储：读取、分页、检索 ────────────────────────────────


def test_query_page_pagination_walks_backwards(store):
    store.max_log = 100
    store.init_schema()
    for index in range(25):
        store.add_message("group:1", {"content": f"m{index}", "timestamp": 1000.0 + index})

    page1, has_more1, cursor1 = store.query_page("group:1", limit=10)
    assert [m["content"] for m in page1] == [f"m{i}" for i in range(15, 25)]
    assert has_more1 is True
    assert cursor1 == page1[0]["id"]

    page2, has_more2, cursor2 = store.query_page("group:1", limit=10, before_id=cursor1)
    assert [m["content"] for m in page2] == [f"m{i}" for i in range(5, 15)]
    assert has_more2 is True

    page3, has_more3, cursor3 = store.query_page("group:1", limit=10, before_id=cursor2)
    assert [m["content"] for m in page3] == [f"m{i}" for i in range(0, 5)]
    assert has_more3 is False
    assert cursor3 is None


def test_query_page_on_empty_session(store):
    store.init_schema()
    page, has_more, cursor = store.query_page("group:none", limit=10)
    assert page == []
    assert has_more is False
    assert cursor is None


def test_query_messages_with_limit_returns_newest_window(store):
    store.max_log = 100
    store.init_schema()
    for index in range(10):
        store.add_message("group:1", {"content": f"m{index}", "timestamp": 1000.0 + index})
    latest = store.query_messages("group:1", limit=3)
    assert [m["content"] for m in latest] == ["m7", "m8", "m9"]


def test_query_messages_since_filters(store):
    store.init_schema()
    store.add_message("group:1", {"content": "old", "timestamp": 100.0})
    store.add_message("group:1", {"content": "new", "timestamp": 200.0})
    assert [m["content"] for m in store.query_messages("group:1", since=150.0)] == ["new"]


def test_session_summaries_batches_counts_and_last_message(store):
    store.init_schema()
    for key in ("group:1", "group:2"):
        for index in range(3):
            store.add_message(key, {"content": f"{key}-{index}", "timestamp": 1000.0 + index})

    summaries = store.session_summaries()
    assert summaries["group:1"]["message_count"] == 3
    assert summaries["group:1"]["last_message"]["content"] == "group:1-2"
    assert summaries["group:2"]["last_message"]["content"] == "group:2-2"


def test_mention_resolver_maps_names_and_ids(store):
    """昵称/群号 → 用户 ID 的解析：先查本会话参与者，再查全局联系人。"""
    store.init_schema()
    store.add_message(
        "group:1", {"sender": "小明", "sender_id": "u1", "content": "a", "timestamp": 1.0}
    )
    store.add_message(
        "group:2", {"sender": "小红", "sender_id": "u2", "content": "b", "timestamp": 2.0}
    )

    resolve = store.mention_resolver("group:1")
    assert resolve("小明") == "u1"
    # 已在别处出现过的昵称也能解析，避免"换个群就 @ 不到"
    assert resolve("小红") == "u2"
    # 直接给 ID 原样返回
    assert resolve("u1") == "u1"
    assert resolve("123456") == "123456"
    assert resolve("查无此人") is None
    assert resolve("") is None


def test_mention_resolver_prefers_session_participants_on_conflict(store):
    """同名不同人时，优先用当前会话里的人，避免在群里 @ 错对象。"""
    store.init_schema()
    store.add_message(
        "group:1", {"sender": "小明", "sender_id": "local-id", "content": "a", "timestamp": 2.0}
    )
    store.add_message(
        "group:2", {"sender": "小明", "sender_id": "other-id", "content": "b", "timestamp": 1.0}
    )

    assert store.mention_resolver("group:1")("小明") == "local-id"


def test_mention_resolver_handles_missing_schema_and_bad_session(store):
    resolve = store.mention_resolver("")
    assert resolve("小明") is None
    # QQ 号最短 5 位，太短的数字当正文处理，避免误 @
    assert resolve("123456") == "123456"
    assert resolve("42") is None


def test_query_participants_groups_all_sessions_in_one_pass(store):
    store.init_schema()
    store.add_message(
        "group:1", {"sender": "小明", "sender_id": "1", "content": "a", "timestamp": 1.0}
    )
    store.add_message(
        "group:1", {"sender": "小红", "sender_id": "2", "content": "b", "timestamp": 2.0}
    )
    store.add_message(
        "group:2", {"sender": "小刚", "sender_id": "3", "content": "c", "timestamp": 3.0}
    )

    grouped = store.query_participants()
    assert {p["id"] for p in grouped["group:1"]} == {"1", "2"}
    assert {p["id"] for p in grouped["group:2"]} == {"3"}
    assert grouped["group:1"][0]["message_count"] == 1


def test_nickname_update_backfills_history(store):
    store.init_schema()
    store.add_message(
        "group:1", {"sender": "旧名", "sender_id": "9", "content": "a", "platform": "p"}
    )
    # 同一个人后来改了昵称
    store.add_message(
        "group:1", {"sender": "新名", "sender_id": "9", "content": "b", "platform": "p"}
    )
    messages = store.query_messages("group:1")
    assert [m["sender_name"] for m in messages] == ["新名", "新名"]


def test_search_messages_escapes_like_wildcards(store):
    store.init_schema()
    store.add_message("group:1", {"content": "折扣 100%", "timestamp": 1.0})
    store.add_message("group:1", {"content": "普通消息", "timestamp": 2.0})

    assert len(store.search_messages("100%")) == 1
    # % 作为通配符时不应命中所有消息
    assert store.search_messages("折%") == []
    assert len(store.search_messages("普通")) == 1


def test_search_messages_scoped_to_session(store):
    store.init_schema()
    store.add_message("group:1", {"content": "关键词", "timestamp": 1.0})
    store.add_message("group:2", {"content": "关键词", "timestamp": 2.0})
    assert len(store.search_messages("关键词")) == 2
    assert len(store.search_messages("关键词", session_id="group:1")) == 1


def test_search_empty_keyword_returns_nothing(store):
    store.init_schema()
    store.add_message("group:1", {"content": "x", "timestamp": 1.0})
    assert store.search_messages("") == []
    assert store.search_messages("   ") == []


# ── 存储：删除与导出 ──────────────────────────────────────


def test_delete_message_returns_removed_row(store):
    store.init_schema()
    saved = store.add_message("group:1", {"content": "待删", "timestamp": 1.0})
    removed = store.delete_message(saved["id"])
    assert removed is not None
    assert removed["content"] == "待删"
    assert store.delete_message(saved["id"]) is None
    assert store.count_messages("group:1") == 0


def test_delete_session_removes_messages(store):
    store.init_schema()
    store.upsert_session("group:1", {"peer_name": "群"})
    store.add_message("group:1", {"content": "a", "timestamp": 1.0})
    assert store.delete_session("group:1") is True
    assert store.load_sessions() == []
    assert store.count_messages("group:1") == 0


def test_delete_all_and_per_session(store):
    store.init_schema()
    store.add_message("a", {"content": "1"})
    store.add_message("b", {"content": "2"})
    assert store.delete_session_messages("a") == 1
    assert store.count_messages("b") == 1
    assert store.delete_all_messages() == 1
    assert store.count_messages("b") == 0


def test_export_session_json_and_txt(store):
    store.init_schema()
    store.upsert_session("group:1", {"peer_name": "群一"})
    store.add_message("group:1", {"sender": "小明", "content": "你好", "timestamp": 1000.0})

    as_json = store.export_session("group:1", fmt="json")
    assert as_json["session"]["peer_name"] == "群一"
    assert as_json["messages"][0]["content"] == "你好"

    as_txt = store.export_session("group:1", fmt="txt")
    assert "小明: 你好" in as_txt["text"]


# ── 图片地址收集 ──────────────────────────────────────────


def test_collect_image_urls_filters_non_http():
    segments = [
        {"type": "image", "url": "https://a/1.png"},
        {"type": "image", "file": "file:///tmp/2.png"},
        {"type": "image", "file": "/tmp/3.png"},
        {"type": "text", "text": "nope"},
    ]
    assert collect_image_urls(segments) == ["https://a/1.png"]


# ── 组件子类识别（真实宿主常见场景） ──────────────────────


def test_component_subclasses_are_recognized():
    """AstrBot 适配器常返回组件子类，必须按后缀识别而不是退化成 other。"""

    class BasePlain:
        def __init__(self, text):
            self.text = text

    class AiocqhttpPlain(BasePlain):
        pass

    class BaseImage:
        def __init__(self, url):
            self.url = url

    class LarkImage(BaseImage):
        pass

    segments = serialize_chain([AiocqhttpPlain("hi"), LarkImage("https://x/1.png")])
    assert [s["type"] for s in segments] == ["text", "image"]


def test_short_names_are_not_misidentified():
    """"Format"/"Surface" 这类短类名不能被误判成 at/face。"""

    class Format:
        pass

    class Surface:
        pass

    segments = serialize_chain([Format(), Surface()])
    assert all(s["type"] == "other" for s in segments)


# ── 导入水位（幂等） ──────────────────────────────────────


def test_import_watermark_roundtrip(store):
    store.init_schema()
    assert store.get_import_watermark("aiocqhttp-inst", "umo-x") == 0
    store.set_import_watermark("aiocqhttp-inst", "umo-x", 42)
    assert store.get_import_watermark("aiocqhttp-inst", "umo-x") == 42
    # 水位只升不降，避免乱序历史把已导入的部分重新拉一遍
    store.set_import_watermark("aiocqhttp-inst", "umo-x", 10)
    assert store.get_import_watermark("aiocqhttp-inst", "umo-x") == 42
    store.set_import_watermark("aiocqhttp-inst", "umo-x", 99)
    assert store.get_import_watermark("aiocqhttp-inst", "umo-x") == 99


def test_import_watermark_is_per_scope(store):
    store.init_schema()
    store.set_import_watermark("p1", "umo-a", 5)
    assert store.get_import_watermark("p1", "umo-b") == 0
    assert store.get_import_watermark("p2", "umo-a") == 0


def test_has_astrbot_message_detects_duplicates(store):
    store.init_schema()
    store.add_message(
        "group:1",
        {
            "content": "你好",
            "sender_id": "u1",
            "platform_message_id": "astrbot:7",
            "timestamp": 1000.0,
        },
    )
    assert store.has_astrbot_message("group:1", 7) is True
    assert store.has_astrbot_message("group:1", 8) is False
    assert store.has_astrbot_message("group:2", 7) is False


def test_has_astrbot_message_ignores_empty_marker(store):
    store.init_schema()
    store.add_message("group:1", {"content": "x", "timestamp": 1.0})
    assert store.has_astrbot_message("group:1", 0) is False


# ── 检索：FTS5 + LIKE 双路径 ──────────────────────────────


def test_fts_index_is_created(store):
    store.init_schema()
    assert store.fts_enabled is True


def test_short_queries_fall_back_to_like(store):
    """trigram 无法匹配 <3 字符，中文双字检索必须走 LIKE。"""
    store.init_schema()
    store.add_message("g1", {"content": "我有三个苹果", "timestamp": 1.0})
    store.add_message("g1", {"content": "今天天气不错", "timestamp": 2.0})

    assert [m["content"] for m in store.search_messages("苹果")] == ["我有三个苹果"]
    assert [m["content"] for m in store.search_messages("天气")] == ["今天天气不错"]


def test_long_queries_use_fts(store):
    store.init_schema()
    store.add_message("g1", {"content": "我有三个苹果", "timestamp": 1.0})
    store.add_message("g1", {"content": "今天天气不错", "timestamp": 2.0})

    assert [m["content"] for m in store.search_messages("三个苹")] == ["我有三个苹果"]
    assert [m["content"] for m in store.search_messages("今天天气")] == ["今天天气不错"]


def test_search_results_respect_limit_and_order(store):
    store.init_schema()
    for index in range(6):
        store.add_message("g1", {"content": f"关键词 {index}", "timestamp": float(index)})
    results = store.search_messages("关键词", limit=3)
    assert len(results) == 3
    # 最新优先
    assert [m["content"] for m in results] == ["关键词 5", "关键词 4", "关键词 3"]


def test_fts_index_follows_deletes(store):
    store.init_schema()
    saved = store.add_message("g1", {"content": "临时消息", "timestamp": 1.0})
    assert store.search_messages("临时消息")
    store.delete_message(saved["id"])
    assert store.search_messages("临时消息") == []


def test_fts_index_follows_session_delete(store):
    store.init_schema()
    store.upsert_session("g1", {"peer_name": "群"})
    store.add_message("g1", {"content": "会话内消息", "timestamp": 1.0})
    store.delete_session("g1")
    assert store.search_messages("会话内消息") == []


def test_fts_index_follows_trim(store):
    """裁剪掉的消息不应还能被搜到。"""
    store.max_log = 2
    store.init_schema()
    for index in range(4):
        store.add_message("g1", {"content": f"第{index}条讯息", "timestamp": float(index)})
    assert store.search_messages("第0条讯息") == []
    assert store.search_messages("第3条讯息")


def test_fts_rebuild_on_reopen(store, tmp_path):
    """重开数据库时若索引行数不一致会自动回填。"""
    store.init_schema()
    store.add_message("g1", {"content": "持久化内容", "timestamp": 1.0})

    reopened = MessageStore(store.db_path, max_log=store.max_log)
    reopened.init_schema()
    assert reopened.fts_enabled is True
    assert [m["content"] for m in reopened.search_messages("持久化内容")] == ["持久化内容"]


def test_fts_does_not_rebuild_on_every_boot(tmp_path):
    """旧库（有数据、没索引）回填一次后必须收敛，不能每次启动都整表重建。

    触发器会把空 content 也写进索引，回填语句如果过滤掉空内容，
    行数永远对不上，于是每次启动都 DELETE + 全量重建。
    """
    db_path = str(tmp_path / "legacy.db")
    legacy = MessageStore(db_path)
    conn = legacy.connect()
    try:
        conn.executescript(webchat_core._SCHEMA_SQL)
        conn.execute(
            "INSERT INTO messages (session_id, type, sender, sender_id, content, "
            "segments, timestamp) VALUES ('s', 'received', 'a', '1', '', '[]', 1)"
        )
        conn.execute(
            "INSERT INTO messages (session_id, type, sender, sender_id, content, "
            "segments, timestamp) VALUES ('s', 'received', 'a', '1', 'hello', '[]', 2)"
        )
        conn.commit()
    finally:
        conn.close()

    for _ in range(3):
        store = MessageStore(db_path)
        store.init_schema()
        conn = store.connect()
        try:
            indexed = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        assert indexed == total


def test_search_survives_fts_unavailable(store, monkeypatch):
    """FTS 建表失败时必须静默降级，检索仍然可用。"""
    store.init_schema()
    store.add_message("g1", {"content": "降级也能搜到", "timestamp": 1.0})
    store.fts_enabled = False
    assert [m["content"] for m in store.search_messages("降级也能搜到")] == ["降级也能搜到"]
    assert [m["content"] for m in store.search_messages("降级")] == ["降级也能搜到"]


def test_search_escapes_fts_syntax(store):
    """关键词里的 FTS 语法字符不能破坏查询，也不能命中无关行。"""
    store.init_schema()
    store.add_message("g1", {"content": '说 "你好" 吧', "timestamp": 1.0})
    store.add_message("g1", {"content": "普通文本内容", "timestamp": 2.0})

    # 含引号的关键词应被当作字面量，而不是把查询语法搞坏
    quoted = store.search_messages('"你好"')
    assert [m["content"] for m in quoted] == ['说 "你好" 吧']

    # FTS 运算符作为普通词时不应命中所有行
    assert store.search_messages("AND OR NOT") == []
    # 通配符在 LIKE 回退路径下要被转义成字面量
    assert store.search_messages("普通%") == []


# ── 入站乐观锁：同一条消息短窗口内只入库一次 ──────────────


def test_recent_duplicate_detects_same_message(store):
    """同一会话、同一发送者、同一内容、时间相近 → 视为重复。"""
    store.init_schema()
    assert store.has_recent_duplicate("group:1", "u1", "还真是", 1000.0, window=8) is False
    store.add_message(
        "group:1",
        {"type": "received", "sender_id": "u1", "content": "还真是", "timestamp": 1000.0},
    )
    assert store.has_recent_duplicate("group:1", "u1", "还真是", 1002.0, window=8) is True


def test_recent_duplicate_respects_window(store):
    """超出时间窗口就不算重复，避免把用户真的连发两次吞掉。"""
    store.init_schema()
    store.add_message(
        "group:1",
        {"type": "received", "sender_id": "u1", "content": "还真是", "timestamp": 1000.0},
    )
    assert store.has_recent_duplicate("group:1", "u1", "还真是", 1030.0, window=8) is False


def test_recent_duplicate_ignores_different_sender_or_text(store):
    store.init_schema()
    store.add_message(
        "group:1",
        {"type": "received", "sender_id": "u1", "content": "还真是", "timestamp": 1000.0},
    )
    assert store.has_recent_duplicate("group:1", "u2", "还真是", 1001.0) is False
    assert store.has_recent_duplicate("group:1", "u1", "别的", 1001.0) is False
    assert store.has_recent_duplicate("group:2", "u1", "还真是", 1001.0) is False


def test_recent_duplicate_covers_text_and_bot_types(store):
    """终端发出的 sent 与 Bot 回推的 received 都要能命中，才能拦住重复。"""
    store.init_schema()
    store.add_message(
        "group:1",
        {"type": "sent", "sender_id": "", "content": "还真是", "timestamp": 1000.0},
    )
    # sent 没有 sender_id，空与空匹配
    assert store.has_recent_duplicate("group:1", "", "还真是", 1001.0) is True


def test_recent_duplicate_empty_text_is_never_duplicate(store):
    store.init_schema()
    store.add_message("group:1", {"content": "", "sender_id": "u1", "timestamp": 1000.0})
    assert store.has_recent_duplicate("group:1", "u1", "", 1001.0) is False


def test_recent_duplicate_ignore_sender_for_bot_echoes(store):
    """Bot 回推时发送者与终端发送时不同，需要能忽略发送者匹配。"""
    store.init_schema()
    store.add_message(
        "group:1",
        {"type": "sent", "sender_id": "", "content": "还真是", "timestamp": 1000.0},
    )
    # 默认按发送者比较：不匹配
    assert store.has_recent_duplicate("group:1", "bot-1", "还真是", 1001.0) is False
    # 明确是 Bot 自身消息时放宽发送者条件
    assert store.has_recent_duplicate(
        "group:1", "bot-1", "还真是", 1001.0, ignore_sender=True
    ) is True
