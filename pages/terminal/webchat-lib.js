/**
 * webchat-lib.js — 纯函数层，不触碰 DOM / bridge。
 *
 * 抽出来的目的：这些逻辑（去重、合并、时间格式、ID 压缩、消息片段渲染数据）
 * 最容易在重构中悄悄改变行为，独立成模块后可以用 node --test 直接锁住。
 */

export const HISTORY_PAGE_SIZE = 40;
export const NEAR_BOTTOM_PX = 80;
export const MESSAGE_TYPES = ["received", "sent", "bot"];

const DAY_MS = 86400000;

/** 安全取字符串。 */
export function str(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

/** 安全取有限数字。 */
export function num(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/** 安全取布尔，兼容 "1"/"true"。 */
export function bool(value, fallback = false) {
  if (typeof value === "boolean") return value;
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(text)) return true;
  if (["0", "false", "no", "off", ""].includes(text)) return false;
  return fallback;
}

/** 归一化单个消息片段。 */
export function normalizeSegment(raw) {
  if (!raw || typeof raw !== "object") return null;
  const type = str(raw.type, "text");
  const segment = { type };
  if (raw.text !== undefined) segment.text = str(raw.text);
  if (raw.url) segment.url = str(raw.url);
  if (raw.file) segment.file = str(raw.file);
  if (raw.target) segment.target = str(raw.target);
  if (raw.name) segment.name = str(raw.name);
  if (raw.id !== undefined && raw.id !== null && raw.id !== "") segment.id = str(raw.id);
  // 引用片段带上被引用消息的昵称 / 内容，前端才能显示「回复 小明：…」
  if (raw.quote && typeof raw.quote === "object") {
    segment.quote = { sender: str(raw.quote.sender), text: str(raw.quote.text) };
  }
  // 合并转发的聊天记录：每条保留昵称 / 账号 / 内部片段
  if (Array.isArray(raw.messages)) {
    segment.messages = raw.messages.map((node) => ({
      name: str(node?.name),
      uin: str(node?.uin),
      segments: Array.isArray(node?.segments)
        ? node.segments.map(normalizeSegment).filter(Boolean)
        : [],
    }));
  }
  return segment;
}

/** 归一化消息对象。 */
export function normalizeMessage(raw, sessionId) {
  const timestamp = num(raw?.timestamp, 0);
  const senderId = str(raw?.sender_id);
  const senderName = str(raw?.sender_name || raw?.sender || senderId || "未知用户");
  const type = MESSAGE_TYPES.includes(raw?.type) ? raw.type : "received";
  const segments = Array.isArray(raw?.segments)
    ? raw.segments.map(normalizeSegment).filter(Boolean)
    : [];
  return {
    id: raw?.id === null || raw?.id === undefined ? null : String(raw.id),
    sessionId: str(raw?.session_id, sessionId),
    type,
    sender: str(raw?.sender),
    senderName,
    senderId,
    content: str(raw?.content),
    preview: str(raw?.preview || raw?.content),
    segments,
    groupId: str(raw?.group_id),
    platform: str(raw?.platform),
    platformMessageId: str(raw?.platform_message_id || raw?.platformMessageId),
    timestamp,
    pending: Boolean(raw?.pending),
    failed: Boolean(raw?.failed),
  };
}

/** 消息唯一键：有数据库 id 用 id，否则用内容指纹（乐观消息可能还没有 id）。 */
export function messageKey(message) {
  if (message?.id !== null && message?.id !== undefined) return `id:${message.id}`;
  return [
    "local",
    message?.sessionId,
    message?.timestamp,
    message?.type,
    message?.senderId,
    message?.content,
  ].join(":");
}

/** 去重 + 按时间/ID 稳定排序。 */
export function dedupeMessages(messages) {
  const unique = new Map();
  for (const message of messages || []) {
    if (!message || !message.sessionId) continue;
    unique.set(messageKey(message), message);
  }
  return [...unique.values()].sort(compareMessages);
}

/** 时间升序；同一时间按 ID 升序，保证渲染顺序稳定。 */
export function compareMessages(a, b) {
  if (a.timestamp !== b.timestamp) return a.timestamp - b.timestamp;
  const aId = a.id === null ? Number.MAX_SAFE_INTEGER : Number(a.id);
  const bId = b.id === null ? Number.MAX_SAFE_INTEGER : Number(b.id);
  return aId - bId;
}

/** 合并入站/历史消息到现有列表，自动去重排序。 */
export function mergeMessages(existing, incoming) {
  return dedupeMessages([...(existing || []), ...(incoming || [])]);
}

/** 归一化会话对象。 */
export function normalizeSession(raw) {
  const isGroup = Boolean(raw?.is_group);
  const participants = Array.isArray(raw?.participants)
    ? raw.participants.map((person) => ({
        id: str(person?.id),
        name: str(person?.name ?? person?.id ?? "未知用户"),
        messageCount: num(person?.message_count, 0),
        lastSeen: num(person?.last_seen, 0),
      }))
    : [];
  const peerId = str(raw?.peer_id || raw?.group_id || raw?.id);
  const peerName = str(
    raw?.peer_name ||
      raw?.group_name ||
      participants[0]?.name ||
      peerId ||
      (isGroup ? "群聊" : "私聊"),
  );
  return {
    id: str(raw?.id),
    peerId,
    peerName,
    groupId: str(raw?.group_id),
    groupName: str(raw?.group_name),
    platform: str(raw?.platform),
    platformId: str(raw?.platform_id),
    isGroup,
    pinned: Boolean(raw?.pinned),
    messageCount: num(raw?.message_count, 0),
    lastActivity: num(raw?.last_activity, 0),
    participants,
    lastMessage: raw?.last_message
      ? normalizeMessage(raw.last_message, str(raw.id))
      : null,
  };
}

/** 会话排序：置顶优先，其次按最后活跃时间，最后按名称。 */
export function compareSessions(a, b) {
  if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
  const aTime = a.lastMessage?.timestamp || a.lastActivity || 0;
  const bTime = b.lastMessage?.timestamp || b.lastActivity || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(a.peerName).localeCompare(String(b.peerName), "zh-CN");
}

/** 会话搜索匹配（名称 / ID / 群号 / 平台）。 */
export function matchesSession(session, query) {
  const keyword = str(query).trim().toLocaleLowerCase("zh-CN");
  if (!keyword) return true;
  return [
    session.peerName,
    session.peerId,
    session.groupId,
    session.groupName,
    session.platform,
  ]
    .filter(Boolean)
    .some((value) => str(value).toLocaleLowerCase("zh-CN").includes(keyword));
}

/** 会话列表预览文本。 */
export function getSessionPreview(session) {
  const message = session?.lastMessage;
  if (!message) {
    return session?.isGroup ? `群号 ${session.groupId || session.peerId}` : `ID ${session.peerId}`;
  }
  const text = message.preview || message.content || segmentsToText(message.segments) || "[非文本消息]";
  if (message.type === "sent") return `你: ${text}`;
  if (message.type === "bot") return `${message.senderName || "Bot"}: ${text}`;
  return `${message.senderName}: ${text}`;
}

/**
 * 把历史消息里残留的 <mention ...> / <at ...> 标签降级成可读文本。
 *
 * 修复前的旧数据可能已经把字面标签存进了库，界面不能直接显示尖括号。
 * 新消息由后端在发送前转成真实 At，不会再走到这里。
 */
export function stripMentionMarkup(text) {
  const source = str(text);
  if (!source || !/<\/?(?:mention|at)\b/i.test(source)) return source;
  return source
    .replace(/<(?:mention|at)\s*>\s*([^<>]{1,64}?)\s*<\/(?:mention|at)\s*>/gi, "@$1")
    .replace(/<\/?(?:mention|at)\b[^<>]*\/?>/gi, () => "@某人");
}

/** 片段 → 纯文本（用于预览、复制）。 */
export function segmentsToText(segments) {
  if (!Array.isArray(segments) || !segments.length) return "";
  const parts = [];
  for (const segment of segments) {
    switch (segment.type) {
      case "text":
        parts.push(stripMentionMarkup(str(segment.text)));
        break;
      case "at":
        parts.push(`@${segment.text || segment.target || ""}`);
        break;
      case "image":
        break;
      case "face":
      case "record":
      case "video":
      case "file":
      case "forward":
      case "reply":
        parts.push(str(segment.text));
        break;
      default:
        parts.push(str(segment.text));
    }
  }
  return parts.join("").trim();
}

/** 消息气泡文本：优先片段，其次 content。 */
export function messageText(message) {
  return segmentsToText(message?.segments) || str(message?.content) || "[非文本消息]";
}

/** 消息中的图片地址列表。 */
export function imageSources(message) {
  if (!Array.isArray(message?.segments)) return [];
  return message.segments
    .filter((segment) => segment.type === "image")
    .map((segment) => str(segment.url || segment.file))
    .filter(Boolean);
}

/** 头像占位字符：中文取首字，英文取首字母。 */
export function getInitials(value) {
  const text = str(value, "?").trim();
  if (!text) return "?";
  if (/[\u3400-\u9fff]/u.test(text)) return text.slice(0, 1);
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
  return text.slice(0, 2).toUpperCase();
}

/** 超长 ID 折叠显示。 */
export function compactId(value, keep = 6, tail = 5) {
  const text = str(value);
  if (text.length <= keep + tail + 1) return text;
  return `${text.slice(0, keep)}…${text.slice(-tail)}`;
}

/** 本地日期键，用于插入日期分隔符。 */
export function dayKey(timestamp) {
  if (!timestamp) return "unknown";
  const date = new Date(timestamp * 1000);
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

/** 日期分隔符文案：今天/昨天/具体日期。 */
export function formatDateLabel(timestamp, now = Date.now()) {
  if (!timestamp) return "未知时间";
  const key = dayKey(timestamp);
  if (key === dayKey(now / 1000)) return "今天";
  if (key === dayKey((now - DAY_MS) / 1000)) return "昨天";
  return new Date(timestamp * 1000).toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

/** HH:MM。 */
export function formatMessageTime(timestamp) {
  if (!timestamp) return "--:--";
  return new Date(timestamp * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** 会话列表时间：今天显示时刻，其他显示月/日。 */
export function formatSessionTime(timestamp, now = Date.now()) {
  if (!timestamp) return "";
  if (dayKey(timestamp) === dayKey(now / 1000)) return formatMessageTime(timestamp);
  return new Date(timestamp * 1000).toLocaleDateString("zh-CN", {
    month: "numeric",
    day: "numeric",
  });
}

/** 完整时间（悬浮提示用）。 */
export function formatFullTime(timestamp) {
  if (!timestamp) return "未知时间";
  return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
}

/** 搜索命中片段的高亮切分：[{text, hit}]。 */
export function splitHighlight(text, keyword) {
  const source = str(text);
  const needle = str(keyword).trim();
  if (!needle) return [{ text: source, hit: false }];
  const lowerSource = source.toLocaleLowerCase("zh-CN");
  const lowerNeedle = needle.toLocaleLowerCase("zh-CN");
  const parts = [];
  let cursor = 0;
  let index = lowerSource.indexOf(lowerNeedle);
  while (index !== -1) {
    if (index > cursor) parts.push({ text: source.slice(cursor, index), hit: false });
    parts.push({ text: source.slice(index, index + needle.length), hit: true });
    cursor = index + needle.length;
    index = lowerSource.indexOf(lowerNeedle, cursor);
  }
  if (cursor < source.length) parts.push({ text: source.slice(cursor), hit: false });
  return parts.length ? parts : [{ text: source, hit: false }];
}

/** 未读角标文案。 */
export function unreadLabel(count) {
  const value = num(count, 0);
  if (value <= 0) return "";
  return value > 99 ? "99+" : String(value);
}

/** 相邻消息是否应合并显示（同一发送者、2 分钟以内）。 */
export function shouldGroupWithPrevious(current, previous, windowSeconds = 120) {
  if (!previous || !current) return false;
  if (previous.type !== current.type) return false;
  if (previous.senderId !== current.senderId) return false;
  if (previous.senderName !== current.senderName) return false;
  if (dayKey(previous.timestamp) !== dayKey(current.timestamp)) return false;
  return Math.abs(current.timestamp - previous.timestamp) <= windowSeconds;
}

/** 判断是否接近底部（自动跟随新消息）。 */
export function isNearBottom({ scrollHeight, scrollTop, clientHeight }, threshold = NEAR_BOTTOM_PX) {
  const distance = num(scrollHeight) - num(scrollTop) - num(clientHeight);
  return distance <= threshold;
}

/** 分页状态归一化。 */
export function normalizeHistory(raw) {
  return {
    hasMore: Boolean(raw?.has_more),
    nextCursor: raw?.next_cursor === null || raw?.next_cursor === undefined
      ? null
      : String(raw.next_cursor),
  };
}

/** 错误对象 → 用户可读文案。 */
export function errorText(error) {
  if (error instanceof Error) return error.message;
  if (error === null || error === undefined) return "未知错误";
  return String(error);
}

// ── 增量渲染计划 ────────────────────────────────────────────
// 全量 replaceChildren 在长会话里会明显卡顿。这里把「该渲染什么」
// 算成纯数据计划，再由 app.js 做最小 DOM 变更。

/** 消息节点签名：任一字段变化都需要重建该节点。 */
function messageSignature(message, grouped) {
  return [
    grouped ? "g" : "s",
    message.type,
    message.timestamp,
    message.senderName,
    message.content,
    (message.segments || []).length,
    message.pending ? "p" : "",
  ].join("|");
}

/**
 * 生成渲染计划：日期分隔符 + 消息节点，按显示顺序排列。
 * @returns {Array<{kind:"date"|"message", key:string, label?:string, message?:object, grouped?:boolean, signature?:string}>}
 */
export function planMessages(messages, sessionId = "") {
  const items = [];
  let previousDay = "";
  let previousMessage = null;

  for (const message of messages || []) {
    const day = dayKey(message.timestamp);
    if (day !== previousDay) {
      items.push({
        kind: "date",
        key: `day:${sessionId}:${day}`,
        signature: `date|${day}`,
        label: formatDateLabel(message.timestamp),
      });
      previousDay = day;
      previousMessage = null;
    }
    const grouped = shouldGroupWithPrevious(message, previousMessage);
    items.push({
      kind: "message",
      key: `msg:${sessionId}:${messageKey(message)}`,
      signature: messageSignature(message, grouped),
      message,
      grouped,
    });
    previousMessage = message;
  }
  return items;
}

/**
 * 比较两个计划，决定最小变更方式。
 *
 * - `append`：旧计划是新计划的前缀，只需追加尾部
 * - `truncate`：新计划是旧计划的前缀，只需移除尾部
 * - `rebuild`：中间有增删改，整体重建
 */
export function diffPlan(previous, next) {
  const oldPlan = previous || [];
  const newPlan = next || [];

  let common = 0;
  while (
    common < oldPlan.length &&
    common < newPlan.length &&
    oldPlan[common].key === newPlan[common].key &&
    oldPlan[common].signature === newPlan[common].signature
  ) {
    common += 1;
  }

  if (common === oldPlan.length && common <= newPlan.length) {
    return { mode: "append", start: common, end: newPlan.length };
  }
  if (common === newPlan.length && common < oldPlan.length) {
    return { mode: "truncate", start: common, end: oldPlan.length };
  }
  return { mode: "rebuild", start: 0, end: newPlan.length };
}

/**
 * 计算虚拟滚动的渲染窗口。
 *
 * 只渲染视口附近的一段节点，用上下两个 spacer 撑住滚动高度：
 * 消息上千条也不会把整棵树塞进 DOM。高度优先用实测值（`heights`），
 * 没测过的用 `forecast` 估算——聊天列表的行高差异大，估算是唯一的办法。
 *
 * @returns {{start:number, end:number, topPad:number, bottomPad:number, offsets:number[]}}
 *   `offsets` 是每一项的累计偏移（长度 total + 1），供滚动锚点定位使用。
 */
export function computeWindow({
  count,
  heights = [],
  scrollTop = 0,
  viewport = 0,
  forecast = 56,
  overscan = 600,
  minRender = 24,
  pinToEnd = false,
} = {}) {
  const total = Math.max(0, Math.floor(num(count, 0)));
  if (!total) return { start: 0, end: 0, topPad: 0, bottomPad: 0, offsets: [0] };

  const fallback = num(forecast, 56) > 0 ? num(forecast, 56) : 56;
  const heightAt = (index) => {
    const measured = num(heights[index], 0);
    return measured > 0 ? measured : fallback;
  };

  const offsets = new Array(total + 1);
  offsets[0] = 0;
  for (let index = 0; index < total; index += 1) {
    offsets[index + 1] = offsets[index] + heightAt(index);
  }

  const minCount = Math.max(1, Math.floor(num(minRender, 24)));
  if (total <= minCount) {
    return { start: 0, end: total, topPad: 0, bottomPad: 0, offsets };
  }

  if (pinToEnd) {
    const start = Math.max(0, total - minCount);
    return { start, end: total, topPad: offsets[start], bottomPad: 0, offsets };
  }

  const top = Math.max(0, num(scrollTop, 0) - Math.max(0, num(overscan, 0)));
  const viewHeight = num(viewport, 0) > 0 ? num(viewport, 0) : 600;
  const bottom = num(scrollTop, 0) + viewHeight + Math.max(0, num(overscan, 0));

  let start = 0;
  while (start < total - 1 && offsets[start + 1] <= top) start += 1;

  let end = start + 1;
  while (end < total && offsets[end] < bottom) end += 1;

  if (end - start < minCount) {
    end = Math.min(total, start + minCount);
    start = Math.max(0, end - minCount);
  }

  return { start, end, topPad: offsets[start], bottomPad: offsets[total] - offsets[end], offsets };
}

/**
 * 视口顶部当前压在哪一项上，以及压在项内的什么位置。
 *
 * 窗口平移（往上翻/往下滑）时用它当锚点：渲染完成后把这一项放回原来的
 * 屏幕位置，未测量消息的估算误差就不会把整屏内容顶走。
 *
 * @returns {{index:number, delta:number}|null} `delta` 是项内偏移（像素）
 */
export function windowAnchor(offsets, scrollTop = 0) {
  const list = Array.isArray(offsets) ? offsets : [];
  if (list.length < 2) return null;
  const top = Math.max(0, num(scrollTop, 0));
  let index = 0;
  while (index < list.length - 2 && list[index + 1] <= top) index += 1;
  return { index, delta: top - list[index] };
}

/**
 * 窗口平移后重新算出的滚动位置：锚点 + 项内偏移。
 *
 * `start` 与 `index` 之间的项都在本轮窗口里渲染过，高度是实测值；
 * `topPad` 是浏览器里顶部 spacer 的真实高度。两者相加就是锚点在文档里的真实位置。
 * 没测到高度的项退回 `forecast` 估算，不能按 0 计算。
 */
export function anchorScrollTop({ topPad = 0, start = 0, index = 0, heights = [], forecast = 0, delta = 0 } = {}) {
  const from = Math.max(0, Math.floor(num(start, 0)));
  const to = Math.max(from, Math.floor(num(index, 0)));
  const fallback = Math.max(0, num(forecast, 0));
  let offset = Math.max(0, num(topPad, 0));
  for (let cursor = from; cursor < to; cursor += 1) {
    const measured = num(heights[cursor], 0);
    offset += measured > 0 ? measured : fallback;
  }
  return offset + num(delta, 0);
}
