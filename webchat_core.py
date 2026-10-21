"""
webchat_core — 纯逻辑层。

本模块**不导入 AstrBot**，只依赖标准库，因此可以脱离宿主框架独立单元测试：

    pytest tests/

职责：
  * 消息链 ↔ 可存储片段（segments）的相互转换
  * SQLite 存储（WAL、忙等待、按 id 裁剪、游标分页、批量汇总、全文检索）
  * 会话键（session key）构造与输入清洗
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 3

# FTS5 trigram 分词器无法匹配少于 3 个字符的查询（"苹果" 命中不了）。
# 中文双字检索极常见，所以更短的查询走 LIKE 回退。
FTS_MIN_CHARS = 3

DEFAULT_MAX_LOG = 200
MAX_PAGE_SIZE = 200
MAX_SEARCH_LIMIT = 200

# 消息类型：received = 对方发来的；sent = 从 Web 终端手动发出；bot = Bot/插件自动发出
MESSAGE_TYPES = ("received", "sent", "bot")

# 片段类型，前端按 type 决定渲染方式
SEGMENT_TYPES = (
    "text",
    "image",
    "at",
    "face",
    "record",
    "video",
    "file",
    "reply",
    "forward",
    "other",
)


# ── 数值与输入清洗 ────────────────────────────────────────────────────


def now_ts() -> float:
    """当前时间戳（秒，浮点）。"""
    return time.time()


def coerce_float(value: Any, default: float = 0.0) -> float:
    """把任意输入转成有限浮点数，失败时回退默认值。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """把任意输入转成 [low, high] 区间内的整数。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result))


def coerce_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔转换，兼容 "1"/"true"/"yes"/"on"。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def session_key_for_group(group_id: Any) -> str:
    """群聊会话键。群聊始终用 group:<id>，不依赖 UMO 格式。"""
    return f"group:{str(group_id or '').strip()}"


def session_key_for_private(umo: str, peer_id: Any = "") -> str:
    """私聊会话键。优先用 UMO（可回发），缺失时退回 peer id。"""
    umo_text = str(umo or "").strip()
    if umo_text:
        return umo_text
    peer_text = str(peer_id or "").strip()
    return f"private:{peer_text}" if peer_text else ""


def make_session_key(is_group: bool, group_id: Any, umo: str, peer_id: Any = "") -> str:
    """按群聊/私聊规则生成稳定的会话键。"""
    if is_group:
        return session_key_for_group(group_id)
    return session_key_for_private(umo, peer_id)


# ── 消息片段序列化 ────────────────────────────────────────────────────


def _clean(value: Any) -> Any:
    """把空值折叠成 None，便于从片段里剔除。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _first_attr(component: Any, *names: str) -> Any:
    for name in names:
        try:
            value = getattr(component, name, None)
        except Exception:  # 属性可能是会抛异常的 property
            continue
        cleaned = _clean(value)
        if cleaned is not None:
            return cleaned
    return None


# 组件类名 → 片段类型。比 SEGMENT_TYPES 多出 plain 等宿主组件名。
_COMPONENT_ALIASES = {
    "plain": "text",
    "text": "text",
    "at": "at",
    "image": "image",
    "face": "face",
    "record": "record",
    "video": "video",
    "file": "file",
    "reply": "reply",
    "quote": "reply",
    "node": "forward",
    "nodes": "forward",
    "forward": "forward",
}

# 仅对足够长的名字做后缀匹配，避免 "Format" 被误判成 at、"Surface" 判成 face
_SUFFIX_MIN_LEN = 5

# 短别名（at / face）不能裸后缀匹配，但宿主子类常用 CamelCase 结尾（TextAt、LarkFace）。
# 要求首字母大写，才能把 "Format" 排除在外。
_SHORT_ALIASES = {"at": "at", "face": "face"}


def _component_kind(component: Any) -> str:
    """按类名判断组件类型；先精确匹配，再对长子串做后缀匹配以兼容子类。"""
    name = type(component).__name__
    raw = name.lower()
    if raw in _COMPONENT_ALIASES:
        return _COMPONENT_ALIASES[raw]
    for alias, kind in _COMPONENT_ALIASES.items():
        if len(alias) >= _SUFFIX_MIN_LEN and raw.endswith(alias):
            return kind
    for alias, kind in _SHORT_ALIASES.items():
        # "TextAt" 命中，"Format" 不命中（结尾是小写 at）
        if len(name) > len(alias) and name.endswith(alias.capitalize()):
            return kind
    return raw


# 合并转发只展开一层：更深的结构留占位，避免异常转发变成递归炸弹。
MAX_FORWARD_DEPTH = 1

_FORWARD_CQ_RE = re.compile(r"\[CQ:forward,([^\]]*)\]")


def _cq_forward_ids(text: str) -> list[str]:
    """从 CQ 码里取 forward 的 id；参数按逗号切，避免把 resid= 之类误当成 id。"""
    ids: list[str] = []
    for match in _FORWARD_CQ_RE.finditer(str(text or "")):
        for param in match.group(1).split(","):
            key, _, value = param.partition("=")
            if key.strip() == "id" and value.strip():
                ids.append(value.strip())
    return ids


def _serialize_forward(component: Any, depth: int = 0) -> dict:
    """把 Node / Nodes / Forward 组件转成带嵌套聊天记录的 forward 片段。

    宿主对转发的承载方式不统一：
      - 单个 ``Node``：自己就是一条聊天记录，``content`` 是消息组件列表
      - ``Nodes`` / ``Forward``：``nodes`` / ``content`` 是一组 ``Node``
      - 有些适配器只给一个转发 id，内容要靠 ``get_forward_msg`` 再拉

    取不到内容时仍返回可点击的片段（带 id 或只有标题）。
    """
    segment: dict = {"type": "forward", "text": "[聊天记录]", "messages": []}
    forward_id = _first_attr(component, "id", "message_id", "resid", "forward_id")
    if forward_id is None and isinstance(getattr(component, "data", None), dict):
        forward_id = component.data.get("id") or component.data.get("resid")
    if forward_id is not None:
        segment["id"] = str(forward_id)
    if depth >= MAX_FORWARD_DEPTH:
        # 深层嵌套只留占位，避免递归炸弹；不带 messages 表示「不再展开」
        segment.pop("messages")
        return segment

    # 注意：_component_kind 会把 Node/Nodes/Forward 统一归一成 forward，
    # 这里要看原始类名才能区分「单个 Node」和「Nodes 容器」。
    if type(component).__name__.lower() == "node":
        # 单个 Node：内容就是自己的消息链；空 Node 视为「只有 id、内容待拉取」
        content = _first_attr(component, "content", "message")
        if isinstance(content, (list, tuple)) and content:
            segment["messages"] = [_serialize_forward_node(component, depth)]
        return segment

    nodes = _first_attr(component, "nodes", "content", "message")
    if isinstance(nodes, dict):
        # OneBot 风格：{"messages": [...]} 或 {"content": [...]}
        segment["messages"] = onebot_forward_to_messages(nodes)
        return segment
    if not isinstance(nodes, (list, tuple)):
        return segment

    messages: list[dict] = []
    for node in nodes:
        try:
            messages.append(_serialize_forward_node(node, depth))
        except Exception:
            continue
    segment["messages"] = messages
    return segment


def _serialize_forward_node(node: Any, depth: int) -> dict:
    """单条聊天记录：昵称 / 账号 / 内部消息链。"""
    name = _first_attr(node, "name", "nickname", "sender_nickname")
    uin = _first_attr(node, "uin", "user_id", "sender_id", "qq")
    content = _first_attr(node, "content", "message")

    segments: list[dict] = []
    if isinstance(content, dict):
        parsed = onebot_forward_to_messages(content)
        if parsed and not name:
            name, uin = parsed[0]["name"], parsed[0]["uin"]
        segments = [seg for item in parsed for seg in item["segments"]]
    elif isinstance(content, (list, tuple)):
        for item in content:
            try:
                segments.append(serialize_component(item, depth + 1))
            except Exception:
                continue

    return {
        "name": str(name or ""),
        "uin": str(uin or ""),
        "segments": segments,
    }


def extract_forward_ids(raw: Any) -> list[str]:
    """从原始事件里扒出合并转发的 id（宿主组件丢字段时的兜底）。

    ``raw`` 可能是 OneBot 事件 dict（``message`` 段列表 + ``raw_message`` CQ 码）、
    消息段列表，或一段 CQ 码字符串。只取 id，不猜内容。
    """
    found: list[str] = []

    def _from_segments(segments: Any) -> None:
        if not isinstance(segments, (list, tuple)):
            return
        for item in segments:
            if not isinstance(item, dict) or str(item.get("type")) != "forward":
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            value = data.get("id") or data.get("resid")
            if value:
                found.append(str(value))

    def _from_cq(text: Any) -> None:
        if isinstance(text, str):
            found.extend(_cq_forward_ids(text))

    if isinstance(raw, dict):
        _from_segments(raw.get("message"))
        _from_segments(raw.get("messages"))
        _from_cq(raw.get("raw_message"))
        _from_cq(raw.get("message_str"))
    elif isinstance(raw, (list, tuple)):
        _from_segments(raw)
    else:
        _from_cq(raw)

    unique: list[str] = []
    for value in found:
        if value and value not in unique:
            unique.append(value)
    return unique


def serialize_component(component: Any, depth: int = 0) -> dict:
    """把单个消息组件转成可 JSON 存储的片段字典。

    只做鸭子类型读取，不 import AstrBot 的组件类，避免版本差异导致 ImportError。
    """
    kind = _component_kind(component)
    text = _first_attr(component, "text")
    url = _first_attr(component, "url")
    file = _first_attr(component, "file", "path")
    name = _first_attr(component, "name", "file_name")

    if kind == "text":
        return {"type": "text", "text": str(text or "")}

    if kind == "image":
        segment = {
            "type": "image",
            "url": url,
            "file": file,
            "text": text,
            "name": name,
        }
        return {k: v for k, v in segment.items() if v is not None}

    if kind == "at":
        qq = _first_attr(component, "qq", "user_id", "target")
        # text 兜底：有些宿主 At 子类用 text 承载昵称
        label = name or _first_attr(component, "text")
        return {"type": "at", "target": str(qq) if qq is not None else "",
                "text": str(label or qq or "")}

    if kind == "face":
        face_id = _first_attr(component, "id", "face_id")
        return {"type": "face", "text": f"[表情 {face_id}]" if face_id else "[表情]"}

    if kind == "record":
        return {"type": "record", "url": url, "file": file, "text": "[语音]"}

    if kind in ("video",):
        return {"type": "video", "url": url, "file": file, "text": "[视频]"}

    if kind == "file":
        return {"type": "file", "url": url, "file": file, "text": f"[文件] {name or ''}".strip()}

    if kind in ("reply", "quote"):
        reply_id = _first_attr(component, "id", "message_id")
        return {"type": "reply", "target": str(reply_id) if reply_id else "",
                "text": "[引用]"}

    if kind in ("node", "nodes", "forward"):
        return _serialize_forward(component, depth)

    # 未知组件：保留类型名与可见文本，避免信息全丢
    return {"type": "other", "name": kind, "text": str(text or f"[{kind}]")}


def serialize_chain(chain: Iterable[Any] | None) -> list[dict]:
    """把消息链转成片段列表；跳过空文本片段。"""
    if not chain:
        return []
    segments: list[dict] = []
    for component in chain:
        try:
            segment = serialize_component(component)
        except Exception:
            continue
        if segment.get("type") == "text" and not segment.get("text"):
            continue
        segments.append(segment)
    return segments


# ── OneBot 原始转发（get_forward_msg）→ 片段 ─────────────────────


def onebot_segment_to_segment(raw: Any) -> dict | None:
    """把 OneBot 的 ``{type, data}`` 消息段转成本插件的片段字典。

    只在「协议端按需拉取转发内容」时使用，格式与 AstrBot 组件无关。
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}

    if kind == "text":
        return {"type": "text", "text": str(data.get("text") or "")}
    if kind == "at":
        qq = str(data.get("qq") or "")
        return {"type": "at", "target": qq, "text": f"@{qq}"}
    if kind == "image":
        segment = {
            "type": "image",
            "url": data.get("url"),
            "file": data.get("file"),
        }
        return {k: v for k, v in segment.items() if v}
    if kind == "face":
        face_id = data.get("id")
        return {"type": "face", "text": f"[表情 {face_id}]" if face_id else "[表情]"}
    if kind in ("reply", "quote"):
        return {"type": "reply", "target": str(data.get("id") or ""), "text": "[引用]"}
    if kind == "record":
        return {"type": "record", "url": data.get("url"), "file": data.get("file"),
                "text": "[语音]"}
    if kind == "video":
        return {"type": "video", "url": data.get("url"), "file": data.get("file"),
                "text": "[视频]"}
    if kind == "file":
        name = data.get("name") or data.get("file") or ""
        return {"type": "file", "url": data.get("url"), "file": data.get("file"),
                "text": f"[文件] {name}".strip()}
    if kind in ("node", "forward"):
        content = data.get("content")
        messages = onebot_forward_to_messages({"messages": content})
        segment = {"type": "forward", "text": "[聊天记录]", "messages": messages}
        if data.get("id"):
            segment["id"] = str(data["id"])
        return segment
    return {"type": "other", "name": kind, "text": f"[{kind}]"}


def onebot_forward_to_messages(payload: Any) -> list[dict]:
    """解析 ``get_forward_msg`` 返回的聊天记录列表。

    结构：``{"messages": [{"sender": {...}, "message": [{type, data}, ...]}]}``。
    任何一层不符合预期都跳过，绝不让一条脏数据把整条转发打挂。
    """
    if not isinstance(payload, dict):
        return []
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []

    messages: list[dict] = []
    for entry in raw_messages:
        if not isinstance(entry, dict):
            messages.append({"name": "", "uin": "", "segments": []})
            continue
        sender = entry.get("sender") if isinstance(entry.get("sender"), dict) else {}
        name = sender.get("nickname") or sender.get("card") or entry.get("name") or ""
        uin = sender.get("user_id") or sender.get("uin") or entry.get("uin") or ""
        raw_segments = entry.get("message")

        segments: list[dict] = []
        if isinstance(raw_segments, list):
            for raw_segment in raw_segments:
                parsed = onebot_segment_to_segment(raw_segment)
                if parsed:
                    segments.append(parsed)
        elif isinstance(raw_segments, dict):
            parsed = onebot_segment_to_segment(raw_segments)
            if parsed:
                segments.append(parsed)

        messages.append(
            {"name": str(name or ""), "uin": str(uin or ""), "segments": segments}
        )
    return messages


# ── 出站文本 → 片段（把 <mention> 标签 / 手打 @ 还原成真实 At） ──────────

# LLM 与插件常用 <mention id="123"/> 这类占位标记表达「@某人」。
# AstrBot 核心层并没有解析它们的约定，于是会原样发到聊天平台——
# 用户看到的是字面的 "<mention>"，被 @ 的人当然收不到提醒。
_TAG = r"(?:mention|at)"

# 标签名后必须紧跟空白、"/" 或 ">"。少了这个边界，`<atom>` / `<attachment>` /
# `<at most 3 items>` 这类普通文本会被整段当成标签吞掉。
_TAG_END = r"(?=[\s/>:])"

# 属性段只认 `key=value` 形式。`<at most 3 items>` 这种不是标签，必须原样留在正文里。
_ATTRS = r"""(?P<attrs>(?:\s+[A-Za-z_][\w-]*\s*=\s*(?:"[^"<>]*"|'[^'<>]*'|[^\s"'<>/>]+))*)"""

_MENTION_TOKEN_RE = re.compile(
    rf"""
    <(?P<paired_tag>{_TAG}){_TAG_END}\s*>\s*(?P<paired_name>[^<>]{{1,64}}?)\s*</(?P=paired_tag)\s*>
  | <(?P<colon_tag>{_TAG}){_TAG_END}\s*:\s*(?P<colon_id>[^<>\s]+)\s*>
  | <(?P<attr_tag>{_TAG}){_TAG_END}{_ATTRS}\s*/?>
  | </?(?P<stray_tag>{_TAG}){_TAG_END}\s*/?>
    """,
    re.VERBOSE | re.IGNORECASE,
)

_MENTION_ID_ATTR_RE = re.compile(
    r"""(?:^|\s)(?:id|qq|user_id|uid|qq_id|target)\s*=\s*["']?\s*([^"'\s/>]+)""",
    re.IGNORECASE,
)

# 只带昵称、没给 ID 的写法（`<at name="小明"/>`）也能靠名字解析出 ID。
_MENTION_NAME_ATTR_RE = re.compile(
    r"""(?:^|\s)(?:name|nickname|nick)\s*=\s*(?:"([^"<>]*)"|'([^'<>]*)'|([^\s"'<>/>]+))""",
    re.IGNORECASE,
)

# 手打 @昵称 / @QQ号。邮箱（a@b.com）与 "@ 一下" 这类写法必须排除在外。
_BARE_AT_RE = re.compile(r"(?<![A-Za-z0-9_.@\-])@(?P<name>[A-Za-z0-9_\u4e00-\u9fff·\-]{1,32})")

_ALL_ALIASES = frozenset({"all", "everyone", "全体", "全体成员", "所有人"})


def has_mention_markup(text: Any) -> bool:
    """文本里是否含 <mention ...> / <at ...> 标记（用于跳过绝大多数普通消息）。"""
    if not text:
        return False
    return bool(_MENTION_TOKEN_RE.search(str(text)))


def text_to_segments(
    text: Any,
    resolve: Callable[[str], str | None] | None = None,
    *,
    allow_bare_at: bool = True,
    allow_tags: bool = True,
) -> list[dict]:
    """把一段出站文本拆成片段，把 @ 语义还原成 at 片段。

    ``resolve`` 把昵称 / QQ 号映射成用户 ID；查不到时**保留人类可读的 @昵称**，
    绝不会把尖括号标签原样留在消息里。

    ``allow_tags`` 为 False 时完全不认 ``<mention>``/``<at>`` 标记（对应配置项
    ``convert_mention_tags``），只处理手打 ``@``。
    """
    raw = "" if text is None else str(text)
    if not raw:
        return []

    segments: list[dict] = []
    if not allow_tags:
        _append_text(segments, raw, resolve, allow_bare_at)
        return segments

    cursor = 0
    for match in _MENTION_TOKEN_RE.finditer(raw):
        _append_text(segments, raw[cursor:match.start()], resolve, allow_bare_at)
        cursor = match.end()
        segment = _mention_segment(match, resolve)
        if segment is not None:
            _push_segment(segments, segment)
    _append_text(segments, raw[cursor:], resolve, allow_bare_at)
    return segments


def _resolve_mention(name: str, resolve: Callable[[str], str | None] | None) -> str:
    target = str(name or "").strip()
    if not target:
        return ""
    if target.lower() in _ALL_ALIASES:
        return "all"
    if resolve is None:
        return ""
    try:
        resolved = resolve(target)
    except Exception:
        return ""
    return str(resolved).strip() if resolved else ""


def _at_segment(target: str, text: str = "") -> dict:
    segment = {"type": "at", "target": str(target)}
    if text:
        segment["text"] = text
    return segment


def _mention_segment(
    match: re.Match, resolve: Callable[[str], str | None] | None
) -> dict | None:
    paired = match.group("paired_name")
    if paired is not None:
        name = paired.strip()
        if not name:
            return None
        target = _resolve_mention(name, resolve)
        if target:
            return _at_segment(target, name)
        # 解析不出 ID 时退化成可读文本，标签本身不能外泄
        return {"type": "text", "text": f"@{name}"}

    colon_id = match.group("colon_id")
    if colon_id:
        return _at_segment(colon_id)

    attrs = match.group("attrs") or ""
    if attrs:
        found = _MENTION_ID_ATTR_RE.search(attrs)
        if found:
            return _at_segment(found.group(1))
        named = _MENTION_NAME_ATTR_RE.search(attrs)
        if named:
            name = next((group for group in named.groups() if group), "").strip()
            if name:
                target = _resolve_mention(name, resolve)
                if target:
                    return _at_segment(target, name)
                return {"type": "text", "text": f"@{name}"}
    return None


def _append_text(
    segments: list[dict],
    chunk: str,
    resolve: Callable[[str], str | None] | None,
    allow_bare_at: bool,
) -> None:
    if not chunk:
        return
    if not allow_bare_at or resolve is None:
        _push_segment(segments, {"type": "text", "text": chunk})
        return

    cursor = 0
    for match in _BARE_AT_RE.finditer(chunk):
        name = match.group("name")
        target = _resolve_mention(name, resolve)
        if not target:
            continue  # 查不到就整段原样保留，避免误伤正文里的 @
        _push_segment(segments, {"type": "text", "text": chunk[cursor:match.start()]})
        _push_segment(segments, _at_segment(target, name))
        cursor = match.end()
    _push_segment(segments, {"type": "text", "text": chunk[cursor:]})


def _push_segment(segments: list[dict], segment: dict) -> None:
    """追加片段；相邻文本合并，空文本丢弃。"""
    if segment.get("type") != "text":
        segments.append(segment)
        return
    text = str(segment.get("text") or "")
    if not text:
        return
    if segments and segments[-1].get("type") == "text":
        segments[-1]["text"] = str(segments[-1].get("text") or "") + text
        return
    segments.append({"type": "text", "text": text})


def segments_to_text(segments: Sequence[dict] | None) -> str:
    """把片段列表折成纯文本预览（不含图片/文件占位之外的冗余信息）。"""
    if not segments:
        return ""
    parts: list[str] = []
    for segment in segments:
        kind = segment.get("type")
        if kind == "text":
            parts.append(str(segment.get("text") or ""))
        elif kind == "at":
            parts.append(f"@{segment.get('text') or segment.get('target') or ''}")
        elif kind == "image":
            parts.append("[图片]")
        elif kind in ("record", "video", "file", "forward"):
            parts.append(str(segment.get("text") or f"[{kind}]"))
        elif kind in ("face", "reply"):
            parts.append(str(segment.get("text") or ""))
    # 纯 @ 消息会补一个 \u200b 明文段保证发得出去，那是投递用的，
    # 不能混进落库的纯文本预览（复制/搜索都会看见一个看不见的字符）。
    return "".join(parts).replace("\u200b", "").strip()


def dumps_segments(segments: Sequence[dict] | None) -> str:
    """片段列表 → JSON 字符串（落库用）。"""
    try:
        return json.dumps(list(segments or []), ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def loads_segments(raw: Any) -> list[dict]:
    """JSON 字符串 → 片段列表，坏数据一律降级为空列表。"""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def preview_of(message: dict) -> str:
    """消息的一句话预览，供会话列表使用。"""
    segments = message.get("segments") or []
    text = segments_to_text(segments)
    if text:
        return text
    return str(message.get("content") or "") or "[非文本消息]"


# ── AstrBot 自身历史记录（platform_message_history）→ 本插件消息 ──────

# AstrBot 把消息链存成 {"type": "user"|"bot", "message": [part, ...]}，
# part 的 type 只有 plain / at / reply 三种（其余组件已经退化成 [Image] 这类文本）。
_ASTRBOT_ROLE_TO_TYPE = {"user": "received", "bot": "bot"}


def _history_timestamp(raw: Any) -> float:
    """把 AstrBot 的 created_at 转成 unix 秒。

    该列是 naive UTC datetime，直接当本地时间用会有 8 小时偏差；
    SQLite 里也可能已经读成字符串或数字，这里逐种兜底。
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value > 0:
            return value
    text = str(raw or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError):
            pass
    return now_ts()


def _history_segment(part: dict) -> dict | None:
    """单个 part → 片段；认不出就降级成文本，绝不静默丢信息。"""
    kind = str(part.get("type") or "").strip().lower()
    if kind == "plain":
        text = str(part.get("text") or "")
        return {"type": "text", "text": text} if text else None
    if kind == "at":
        target = str(part.get("user_id") or "").strip()
        name = str(part.get("name") or "").strip()
        if not target:
            # 没有 ID 就还原不出 At：留成 @昵称 明文，别生成一个空的 at 片段
            return {"type": "text", "text": f"@{name}"} if name else None
        return {"type": "at", "target": target, "text": name or target}
    if kind == "reply":
        snippet = str(part.get("text") or "").strip()
        return {
            "type": "reply",
            "target": str(part.get("message_id") or ""),
            "text": f"[引用 {snippet}]" if snippet else "[引用]",
        }
    text = str(part.get("text") or "").strip()
    return {"type": "text", "text": text or f"[{kind or '未知'}]"}


def astrbot_history_to_message(row: Any) -> dict | None:
    """把一条 platform_message_history 记录转成 add_message 需要的字典。

    返回 None 表示这条记录没有可用内容，调用方应跳过。
    """
    if isinstance(row, dict):
        getter = row.get
    else:
        getter = lambda key, default=None: getattr(row, key, default)  # noqa: E731

    content = getter("content") or {}
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            content = {}
    if not isinstance(content, dict):
        return None

    parts = content.get("message")
    if not isinstance(parts, list):
        return None

    segments: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        segment = _history_segment(part)
        if segment is None:
            continue
        if segment["type"] == "text" and segments and segments[-1]["type"] == "text":
            segments[-1]["text"] = f"{segments[-1]['text']}{segment['text']}"
            continue
        segments.append(segment)
    if not segments:
        return None

    role = str(content.get("type") or "user").strip().lower()
    row_id = getter("id")
    sender_id = str(getter("sender_id") or "")
    sender_name = str(getter("sender_name") or "")
    if role == "bot" and not sender_name:
        sender_name = "Bot"

    return {
        "type": _ASTRBOT_ROLE_TO_TYPE.get(role, "received"),
        "sender": sender_name or sender_id,
        "sender_id": sender_id,
        "content": segments_to_text(segments),
        "segments": segments,
        "platform_message_id": f"astrbot:{row_id}" if row_id is not None else "",
        "timestamp": _history_timestamp(getter("created_at")),
    }


# ── 存储层 ────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0,
    send_target TEXT NOT NULL DEFAULT '',
    peer_id TEXT NOT NULL DEFAULT '',
    peer_name TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'received',
    sender TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    segments TEXT NOT NULL DEFAULT '[]',
    group_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    platform_message_id TEXT NOT NULL DEFAULT '',
    timestamp REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
    platform TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, user_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id);
"""

# FTS5 索引：trigram 分词器支持子串匹配与中日韩文本，无需外部分词器。
# 建表可能因 SQLite 编译选项失败，因此与主 schema 分开、允许降级。
_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, tokenize='trigram case_sensitive 0');
"""

# 用触发器让索引与 messages 表自动保持一致，避免每条写入路径都要手动维护。
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;
"""

_fts_logger = logging.getLogger("astrbot_plugin_webchat.fts")


def logger_fts_unavailable(exc: Exception) -> None:
    """FTS5 不可用时只提示一次级别很低的日志——LIKE 回退仍然可用。"""
    _fts_logger.info("FTS5 索引不可用，检索将回退到 LIKE: %s", exc)

# 旧库升级：列名 → 建列 DDL 片段
_SESSION_COLUMNS = {
    "platform_id": "TEXT NOT NULL DEFAULT ''",
    "peer_id": "TEXT NOT NULL DEFAULT ''",
    "peer_name": "TEXT NOT NULL DEFAULT ''",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "REAL NOT NULL DEFAULT 0",
}

_MESSAGE_COLUMNS = {
    "segments": "TEXT NOT NULL DEFAULT '[]'",
    "platform_id": "TEXT NOT NULL DEFAULT ''",
    "platform_message_id": "TEXT NOT NULL DEFAULT ''",
}


class MessageStore:
    """SQLite 存储。每次操作独立连接，配合 WAL + busy_timeout 保证并发安全。"""

    def __init__(self, db_path: str, max_log: int = DEFAULT_MAX_LOG):
        self.db_path = db_path
        self.max_log = max(1, int(max_log))
        # FTS5 索引是否可用；不可用时全部查询走 LIKE 回退
        self.fts_enabled = False
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)

    # ── 连接与建表 ──────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError:
            pass
        return conn

    def init_schema(self) -> None:
        """建表 + 旧库补列 + 回填历史数据。可重复调用。"""
        conn = self.connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            self._ensure_columns(conn, "sessions", _SESSION_COLUMNS)
            self._ensure_columns(conn, "messages", _MESSAGE_COLUMNS)
            conn.execute(
                "UPDATE sessions SET peer_id = COALESCE(NULLIF(group_id, ''), id) "
                "WHERE is_group = 1 AND peer_id = ''"
            )
            conn.execute(
                "UPDATE sessions SET peer_id = COALESCE(("
                "SELECT sender_id FROM messages WHERE session_id = sessions.id "
                "AND sender_id != '' ORDER BY id DESC LIMIT 1), id) "
                "WHERE is_group = 0 AND peer_id = ''"
            )
            conn.execute(
                "UPDATE sessions SET peer_name = group_name "
                "WHERE peer_name = '' AND group_name != ''"
            )
            conn.execute("UPDATE sessions SET created_at = 0 WHERE created_at IS NULL")
            conn.commit()
            # 从历史消息回填联系人昵称（幂等）
            conn.execute(
                "INSERT OR IGNORE INTO contacts (platform, user_id, nickname, updated_at) "
                "SELECT platform, sender_id, sender, MAX(timestamp) FROM messages "
                "WHERE sender_id != '' AND sender != '' GROUP BY platform, sender_id"
            )
            self._init_fts(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """建 FTS5 索引并回填；SQLite 不支持时静默降级到 LIKE。"""
        try:
            conn.executescript(_FTS_SQL)
            conn.executescript(_FTS_TRIGGERS_SQL)
        except sqlite3.DatabaseError as exc:
            self.fts_enabled = False
            logger_fts_unavailable(exc)
            return
        self.fts_enabled = True
        try:
            indexed = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if indexed != total:
                # 触发器会索引空 content（与下面这条语句保持同一口径），
                # 两边口径一旦不一致，每次启动都会判定「索引不全」并整表重建。
                conn.execute("DELETE FROM messages_fts")
                conn.execute(
                    "INSERT INTO messages_fts (rowid, content) SELECT id, content FROM messages"
                )
        except sqlite3.DatabaseError as exc:
            self.fts_enabled = False
            logger_fts_unavailable(exc)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ── 会话 ───────────────────────────────────────────────────

    def upsert_session(self, session_id: str, meta: dict) -> None:
        """幂等写入会话；已存在时只更新提供的非空字段。"""
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO sessions (id, platform, platform_id, group_id, group_name, "
                "is_group, send_target, peer_id, peer_name, pinned, display_order, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "COALESCE((SELECT display_order FROM sessions WHERE id = ?), "
                "         (SELECT COALESCE(MAX(display_order), 0) + 1 FROM sessions)), "
                "COALESCE((SELECT created_at FROM sessions WHERE id = ?), ?)) "
                "ON CONFLICT(id) DO UPDATE SET "
                "platform = COALESCE(NULLIF(excluded.platform, ''), sessions.platform), "
                "platform_id = COALESCE(NULLIF(excluded.platform_id, ''), sessions.platform_id), "
                "group_id = COALESCE(NULLIF(excluded.group_id, ''), sessions.group_id), "
                "group_name = COALESCE(NULLIF(excluded.group_name, ''), sessions.group_name), "
                "is_group = excluded.is_group, "
                "send_target = COALESCE(NULLIF(excluded.send_target, ''), sessions.send_target), "
                "peer_id = COALESCE(NULLIF(excluded.peer_id, ''), sessions.peer_id), "
                "peer_name = COALESCE(NULLIF(excluded.peer_name, ''), sessions.peer_name)",
                (
                    session_id,
                    str(meta.get("platform", "") or ""),
                    str(meta.get("platform_id", "") or ""),
                    str(meta.get("group_id", "") or ""),
                    str(meta.get("group_name", "") or ""),
                    1 if meta.get("is_group") else 0,
                    str(meta.get("send_target", "") or ""),
                    str(meta.get("peer_id", "") or ""),
                    str(meta.get("peer_name", "") or ""),
                    1 if meta.get("pinned") else 0,
                    session_id,
                    session_id,
                    coerce_float(meta.get("created_at"), now_ts()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_sessions(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, platform, platform_id, group_id, group_name, is_group, "
                "send_target, peer_id, peer_name, pinned, display_order, created_at "
                "FROM sessions ORDER BY pinned DESC, display_order, id"
            ).fetchall()
        finally:
            conn.close()
        return [self._session_row_to_dict(row) for row in rows]

    @staticmethod
    def _session_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "platform": row["platform"] or "",
            "platform_id": row["platform_id"] or "",
            "group_id": row["group_id"] or "",
            "group_name": row["group_name"] or "",
            "is_group": bool(row["is_group"]),
            "send_target": row["send_target"] or "",
            "peer_id": row["peer_id"] or "",
            "peer_name": row["peer_name"] or "",
            "pinned": bool(row["pinned"]),
            "display_order": row["display_order"] or 0,
            "created_at": coerce_float(row["created_at"]),
        }

    def set_pinned(self, session_id: str, pinned: bool) -> bool:
        conn = self.connect()
        try:
            cursor = conn.execute(
                "UPDATE sessions SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def rename_session(self, session_id: str, peer_name: str) -> bool:
        conn = self.connect()
        try:
            cursor = conn.execute(
                "UPDATE sessions SET peer_name = ? WHERE id = ?",
                (peer_name, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部消息。"""
        conn = self.connect()
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    # ── 消息写入 ────────────────────────────────────────────────

    def add_message(self, session_id: str, message: dict) -> dict:
        """写入一条消息并裁剪；返回可直接下发给前端的消息字典。"""
        timestamp = coerce_float(message.get("timestamp"), now_ts()) or now_ts()
        sender = str(message.get("sender") or "")
        sender_id = str(message.get("sender_id") or "")
        platform = str(message.get("platform") or "")
        platform_id = str(message.get("platform_id") or "")
        segments = message.get("segments")
        segments = segments if isinstance(segments, list) else []
        content = str(message.get("content") or "") or segments_to_text(segments)
        msg_type = str(message.get("type") or "received")
        if msg_type not in MESSAGE_TYPES:
            msg_type = "received"

        conn = self.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, type, sender, sender_id, content, segments, "
                "group_id, platform, platform_id, platform_message_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    msg_type,
                    sender,
                    sender_id,
                    content,
                    dumps_segments(segments),
                    str(message.get("group_id") or ""),
                    platform,
                    platform_id,
                    str(message.get("platform_message_id") or ""),
                    timestamp,
                ),
            )
            message_id = cursor.lastrowid
            if sender_id and sender:
                conn.execute(
                    "INSERT INTO contacts (platform, user_id, nickname, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(platform, user_id) DO UPDATE SET "
                    "nickname = excluded.nickname, updated_at = excluded.updated_at",
                    (platform, sender_id, sender, timestamp),
                )
            self._trim(conn, session_id)
            conn.commit()
        finally:
            conn.close()

        return {
            "id": message_id,
            "session_id": session_id,
            "type": msg_type,
            "sender": sender,
            "sender_name": sender or sender_id,
            "sender_id": sender_id,
            "content": content,
            "segments": segments,
            "preview": segments_to_text(segments) or content or "[非文本消息]",
            "group_id": str(message.get("group_id") or ""),
            "platform": platform,
            "platform_id": platform_id,
            "platform_message_id": str(message.get("platform_message_id") or ""),
            "timestamp": timestamp,
            "pending": bool(message.get("pending")),
        }

    def _trim(self, conn: sqlite3.Connection, session_id: str) -> None:
        """按自增 id 保留最新 max_log 条（与游标分页口径一致）。"""
        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND id NOT IN "
            "(SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
            (session_id, session_id, self.max_log),
        )

    # ── 消息读取 ────────────────────────────────────────────────

    def _rows_to_messages(self, rows: Sequence[sqlite3.Row]) -> list[dict]:
        messages = [self._message_row_to_dict(row) for row in rows]
        return self._enrich(messages)

    @staticmethod
    def _message_row_to_dict(row: sqlite3.Row) -> dict:
        keys = row.keys()
        segments = loads_segments(row["segments"] if "segments" in keys else None)
        content = row["content"] or ""
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "type": row["type"] or "received",
            "sender": row["sender"] or "",
            "sender_name": row["sender"] or row["sender_id"] or "未知用户",
            "sender_id": row["sender_id"] or "",
            "content": content,
            "segments": segments,
            "preview": segments_to_text(segments) or content or "[非文本消息]",
            "group_id": row["group_id"] or "",
            "platform": row["platform"] or "",
            "platform_id": (row["platform_id"] if "platform_id" in keys else "") or "",
            "platform_message_id": (
                row["platform_message_id"] if "platform_message_id" in keys else ""
            )
            or "",
            "timestamp": coerce_float(row["timestamp"]),
        }

    def _enrich(self, messages: list[dict]) -> list[dict]:
        """用 contacts 表补齐历史消息的当前昵称。"""
        if not messages:
            return messages
        keys = {
            (message["platform"], message["sender_id"])
            for message in messages
            if message.get("sender_id")
        }
        if not keys:
            return messages
        conn = self.connect()
        try:
            nicknames = {
                (row["platform"], row["user_id"]): row["nickname"]
                for row in conn.execute(
                    "SELECT platform, user_id, nickname FROM contacts WHERE nickname != ''"
                )
            }
        finally:
            conn.close()
        for message in messages:
            key = (message.get("platform", ""), message.get("sender_id", ""))
            nickname = nicknames.get(key)
            if nickname:
                message["sender_name"] = nickname
        return messages

    def query_messages(self, session_id: str, since: float = 0.0, limit: int = 0) -> list[dict]:
        """按时间正序取消息；limit>0 时取最新 limit 条。"""
        conn = self.connect()
        try:
            if limit > 0:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE session_id = ? AND timestamp > ? "
                    "ORDER BY timestamp DESC, id DESC LIMIT ?) sub ORDER BY timestamp, id",
                    (session_id, since, clamp_int(limit, 1, MAX_PAGE_SIZE, 50)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND timestamp > ? "
                    "ORDER BY timestamp, id",
                    (session_id, since),
                ).fetchall()
        finally:
            conn.close()
        return self._rows_to_messages(rows)

    def messages_after(self, after_id: int, limit: int = MAX_PAGE_SIZE) -> list[dict]:
        """按全局 id 游标取「比 after_id 新」的消息，跨会话、id 升序。

        轮询降级用：一次请求带回所有会话的新消息，既不用逐会话发 N 次请求，
        也不依赖 timestamp——同一秒多条、或发送端时钟回拨都不会漏。

        裁剪与分页都以 id 为准，这里的游标和它们同口径。
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                (max(0, int(after_id)), clamp_int(limit, 1, MAX_PAGE_SIZE, MAX_PAGE_SIZE)),
            ).fetchall()
        finally:
            conn.close()
        return self._rows_to_messages(rows)

    # ── 导入水位：保证 AstrBot 历史只导一次 ──────────────────────

    # 水位键版本。旧版（v1）只取 page=1 就把水位推到最新 id，
    # 更早的记录被永久挡住、再也补不回来——换版本号让那些被污染的水位失效一次，
    # 下一次导入会全量扫一遍，重复行由 platform_message_id 去重兜住。
    IMPORT_WATERMARK_VERSION = 2

    @classmethod
    def _watermark_key(cls, platform_id: str, scope: str) -> str:
        return f"history_import:v{cls.IMPORT_WATERMARK_VERSION}:{platform_id}:{scope}"

    def get_import_watermark(self, platform_id: str, scope: str) -> int:
        """已导入到哪一条 platform_message_history（按 id），0 表示没导过。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (self._watermark_key(platform_id, scope),),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()
        try:
            return max(0, int(str(row["value"]).strip())) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_import_watermark(self, platform_id: str, scope: str, value: int) -> None:
        """只升不降：乱序回放旧记录时不能把水位拉回去。

        单调性由 SQL 的 ``WHERE`` 条件保证（单语句原子），
        不要拆成「先读再写」——两个并发导入会互相覆盖。
        """
        target = max(0, int(value))
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                (self._watermark_key(platform_id, scope), str(target)),
            )
            conn.commit()
        finally:
            conn.close()

    def has_recent_duplicate(
        self,
        session_id: str,
        sender_id: str,
        content: str,
        timestamp: float,
        window: float = 8.0,
        ignore_sender: bool = False,
    ) -> bool:
        """乐观锁：同会话 + 同发送者 + 同内容 + 时间窗内已存在，视为重复。

        不做类型区分——终端发出的 sent、Bot 回推的 received 都要能互相命中，
        否则同一条消息会被不同来源各记一次。

        ``ignore_sender``：Bot 自身消息在「终端发出（sender_id 为空）」与
        「平台回推（sender_id 是 Bot）」两种形态下发送者不同，此时只比内容与时间。
        """
        text = str(content or "").strip()
        if not session_id or not text:
            return False
        try:
            moment = float(timestamp)
        except (TypeError, ValueError):
            return False
        span = max(0.0, float(window))
        sql = "SELECT 1 FROM messages WHERE session_id = ? AND TRIM(content) = ? "
        params: list[Any] = [session_id, text]
        if not ignore_sender:
            sql += "AND sender_id = ? "
            params.append(str(sender_id or ""))
        sql += "AND timestamp >= ? AND timestamp <= ? LIMIT 1"
        params.extend([moment - span, moment + span])
        conn = self.connect()
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()
        return row is not None

    QUOTE_PREVIEW_CHARS = 120

    def quote_of(self, session_id: str, platform_message_id: str) -> dict | None:
        """按平台消息 id 找回被引用消息的发送者与内容（引用展示用）。

        ``astrbot:<id>`` 是历史导入时本地合成的 id，平台侧不存在，直接跳过。
        """
        target = str(platform_message_id or "").strip()
        if not session_id or not target or target.startswith("astrbot:"):
            return None
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT sender, content FROM messages "
                "WHERE session_id = ? AND platform_message_id = ? ORDER BY id DESC LIMIT 1",
                (session_id, target),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "sender": str(row["sender"] or ""),
            "text": str(row["content"] or "").strip()[: self.QUOTE_PREVIEW_CHARS],
        }

    def has_astrbot_message(self, session_id: str, row_id: int) -> bool:
        """这条 AstrBot 历史是否已经导进来过（按 platform_message_id 留痕）。"""
        try:
            marker = f"astrbot:{int(row_id)}"
        except (TypeError, ValueError):
            return False
        if marker == "astrbot:0":
            return False
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND platform_message_id = ? LIMIT 1",
                (session_id, marker),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()
        return row is not None

    def mention_resolver(self, session_id: str) -> Callable[[str], str | None]:
        """构造「昵称 / QQ 号 → 用户 ID」的解析器，供 text_to_segments 使用。

        优先当前会话的参与者（同名时避免 @ 错人），再用全局联系人兜底，
        这样换个群 @ 以前聊过的人也能命中。
        """
        names: dict[str, str] = {}
        ids: set[str] = set()
        local_ids: dict[str, set[str]] = {}

        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT m.sender_id, "
                "COALESCE(NULLIF(c.nickname, ''), NULLIF(m.sender, '')) AS name, "
                "CASE WHEN m.session_id = ? THEN 1 ELSE 0 END AS local, "
                "MAX(m.timestamp) AS last_seen "
                "FROM messages m LEFT JOIN contacts c "
                "  ON c.platform = m.platform AND c.user_id = m.sender_id "
                "WHERE m.sender_id != '' "
                "GROUP BY m.sender_id, name, local "
                "ORDER BY local ASC, last_seen ASC",
                (session_id or "",),
            ).fetchall()
            # 联系人表里有 ID 但没有消息记录的也要能 @ 到（手打 QQ 号场景）。
            # 单独 try：表不存在时不能把上面已经查到的 rows 一起丢掉。
            try:
                contact_ids = [
                    str(contact["user_id"] or "").strip()
                    for contact in conn.execute("SELECT user_id FROM contacts")
                ]
            except sqlite3.OperationalError:
                contact_ids = []
            ids.update(user_id for user_id in contact_ids if user_id)
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()

        for row in rows:
            user_id = str(row["sender_id"] or "").strip()
            if not user_id:
                continue
            ids.add(user_id)
            label = str(row["name"] or "").strip()
            # 后写入的覆盖先写入的：local 与 last_seen 升序，最终留下最新的本地昵称
            if label:
                names[label] = user_id
                if row["local"]:
                    local_ids.setdefault(label, set()).add(user_id)

        # 本会话里同名但不同人：宁可退化成 @昵称 明文，也不能 @ 错人
        ambiguous = {label for label, users in local_ids.items() if len(users) > 1}

        def resolve(name: str) -> str | None:
            key = str(name or "").strip().lstrip("@").strip()
            if not key:
                return None
            # 手打的 QQ 号（5~12 位）本身就是 ID，不用查表；更短的当正文处理，避免误 @
            if key in ids:
                return key
            if key.isdigit() and 5 <= len(key) <= 12:
                return key
            if key in ambiguous:
                return None
            return names.get(key)

        return resolve

    def query_page(
        self, session_id: str, limit: int = 50, before_id: int = 0
    ) -> tuple[list[dict], bool, int | None]:
        """按消息 id 游标向前翻页，返回（正序消息, 是否还有更早, 下一页游标）。"""
        safe_limit = clamp_int(limit, 1, MAX_PAGE_SIZE, 50)
        before = clamp_int(before_id, 0, 2**62, 0)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND (? <= 0 OR id < ?) "
                "ORDER BY id DESC LIMIT ?",
                (session_id, before, before, safe_limit + 1),
            ).fetchall()
        finally:
            conn.close()
        has_more = len(rows) > safe_limit
        page = list(rows[:safe_limit])
        page.reverse()
        messages = self._rows_to_messages(page)
        next_cursor = messages[0]["id"] if (messages and has_more) else None
        return messages, has_more, next_cursor

    def count_messages(self, session_id: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        return int(row["cnt"]) if row else 0

    def query_participants(self, limit_per_session: int = 50) -> dict[str, list[dict]]:
        """一次查出所有会话的参与者，按会话分组（避免每会话一次查询）。"""
        cap = clamp_int(limit_per_session, 1, MAX_PAGE_SIZE, 50)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT m.session_id, m.sender_id, "
                "COALESCE(NULLIF(c.nickname, ''), NULLIF(m.sender, ''), m.sender_id) AS name, "
                "COUNT(*) AS message_count, MAX(m.timestamp) AS last_seen "
                "FROM messages m LEFT JOIN contacts c "
                "  ON c.platform = m.platform AND c.user_id = m.sender_id "
                "WHERE m.sender_id != '' "
                "GROUP BY m.session_id, m.sender_id "
                "ORDER BY m.session_id, last_seen DESC"
            ).fetchall()
        finally:
            conn.close()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            bucket = grouped.setdefault(row["session_id"], [])
            if len(bucket) >= cap:
                continue
            bucket.append(
                {
                    "id": row["sender_id"],
                    "name": row["name"] or row["sender_id"],
                    "message_count": row["message_count"],
                    "last_seen": coerce_float(row["last_seen"]),
                }
            )
        return grouped

    def session_summaries(self) -> dict[str, dict]:
        """一次查出每个会话的消息数与最后一条消息。"""
        conn = self.connect()
        try:
            counts = {
                row["session_id"]: (int(row["cnt"]), coerce_float(row["last_ts"]))
                for row in conn.execute(
                    "SELECT session_id, COUNT(*) AS cnt, MAX(timestamp) AS last_ts "
                    "FROM messages GROUP BY session_id"
                )
            }
            latest_rows = conn.execute(
                "SELECT * FROM (SELECT *, ROW_NUMBER() OVER ("
                "  PARTITION BY session_id ORDER BY timestamp DESC, id DESC) AS rn "
                "FROM messages) WHERE rn = 1"
            ).fetchall()
        finally:
            conn.close()
        latest = {
            row["session_id"]: self._message_row_to_dict(row) for row in latest_rows
        }
        enriched = dict(latest.items())
        if enriched:
            by_id = self._enrich(list(enriched.values()))
            enriched = {message["session_id"]: message for message in by_id}
        result: dict[str, dict] = {}
        for session_id in set(counts) | set(enriched):
            count, last_ts = counts.get(session_id, (0, 0.0))
            result[session_id] = {
                "message_count": count,
                "last_message": enriched.get(session_id),
                "last_activity": last_ts,
            }
        return result

    # ── 检索 ────────────────────────────────────────────────────

    def search_messages(
        self, query: str, session_id: str = "", limit: int = 50
    ) -> list[dict]:
        """按关键词检索消息正文。

        策略：
          * 关键词 >= ``FTS_MIN_CHARS`` 且 FTS5 可用 → 走 trigram 索引
          * 更短的关键词（中文双字检索很常见）→ 回退 LIKE
          * FTS5 不可用 → 全部回退 LIKE

        两条路径都做通配符转义，避免 ``%``/``_`` 被当作模式。
        """
        keyword = str(query or "").strip()
        if not keyword:
            return []
        cap = clamp_int(limit, 1, MAX_SEARCH_LIMIT, 50)

        if self.fts_enabled and len(keyword) >= FTS_MIN_CHARS:
            try:
                results = self._search_fts(keyword, session_id, cap)
                if results is not None:
                    return results
            except sqlite3.DatabaseError:
                # 索引异常时本次降级，后续仍会尝试 FTS
                pass
        return self._search_like(keyword, session_id, cap)

    def _search_fts(
        self, keyword: str, session_id: str, cap: int
    ) -> list[dict] | None:
        """trigram 索引检索。返回 None 表示索引不可用，应回退。"""
        # 双引号内是短语查询；内部引号需转义成两个双引号
        phrase = '"' + keyword.replace('"', '""') + '"'
        sql = (
            "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
            "WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [phrase]
        if session_id:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(cap)
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        return self._rows_to_messages(rows)

    def _search_like(self, keyword: str, session_id: str, cap: int) -> list[dict]:
        escaped = (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        sql = "SELECT * FROM messages WHERE content LIKE ? ESCAPE '\\'"
        params: list[Any] = [pattern]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(cap)
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return self._rows_to_messages(rows)

    # ── 删除 ────────────────────────────────────────────────────

    def delete_message(self, message_id: int) -> dict | None:
        """删除单条消息，返回被删消息（用于撤回/广播）。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            message = self._message_row_to_dict(row)
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()
            return message
        finally:
            conn.close()

    def get_message(self, message_id: int) -> dict | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            return self._rows_to_messages([row])[0]
        finally:
            conn.close()

    def delete_session_messages(self, session_id: str) -> int:
        conn = self.connect()
        try:
            cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete_all_messages(self) -> int:
        conn = self.connect()
        try:
            cursor = conn.execute("DELETE FROM messages")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # ── 导出 ────────────────────────────────────────────────────

    def export_session(self, session_id: str, fmt: str = "json") -> dict:
        """导出会话内容，供前端下载。"""
        sessions = {item["id"]: item for item in self.load_sessions()}
        session = sessions.get(session_id)
        messages = self.query_messages(session_id)
        if fmt == "txt":
            lines = []
            for message in messages:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(message["timestamp"]))
                lines.append(f"[{stamp}] {message['sender_name']}: {message['preview']}")
            return {"session": session, "text": "\n".join(lines)}
        return {"session": session, "messages": messages}


# ── 片段里的图片地址提取 ────────────────────────────────────────────

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_http_url(value: Any) -> bool:
    return bool(_URL_RE.match(str(value or "").strip()))


def collect_image_urls(segments: Sequence[dict] | None) -> list[str]:
    """取出片段中的图片可访问地址，供媒体代理使用。"""
    urls: list[str] = []
    for segment in segments or []:
        if segment.get("type") != "image":
            continue
        for key in ("url", "file"):
            value = str(segment.get(key) or "").strip()
            if value and is_http_url(value):
                urls.append(value)
                break
    return urls
