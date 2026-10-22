/**
 * webchat-lib 单元测试：node --test pages/terminal/webchat-lib.test.js
 *
 * 这些断言锁住的是“容易被重构悄悄改掉”的行为：
 * 去重键、排序稳定性、预览文案、时间分组、高亮切分。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compactId,
  compareSessions,
  computeWindow,
  dayKey,
  dedupeMessages,
  formatDateLabel,
  formatSessionTime,
  getInitials,
  getSessionPreview,
  imageSources,
  isNearBottom,
  matchesSession,
  mergeMessages,
  messageKey,
  messageText,
  normalizeHistory,
  normalizeMessage,
  normalizeSegment,
  normalizeSession,
  planMessages,
  diffPlan,
  segmentsToText,
  shouldGroupWithPrevious,
  splitHighlight,
  str,
  stripMentionMarkup,
  unreadLabel,
  windowAnchor,
  anchorScrollTop,
} from "./webchat-lib.js";

const T = 1700000000; // 固定时间戳，避免测试依赖当前时间

// ── 归一化 ──────────────────────────────────────────────

test("normalizeMessage 填充缺省值并保持类型合法", () => {
  const message = normalizeMessage({ id: 7, session_id: "s1", content: "hi" }, "s1");
  assert.equal(message.id, "7");
  assert.equal(message.sessionId, "s1");
  assert.equal(message.type, "received");
  assert.equal(message.senderName, "未知用户");
  assert.deepEqual(message.segments, []);
});

test("normalizeMessage 保留三种消息类型，非法类型降级", () => {
  for (const type of ["received", "sent", "bot"]) {
    assert.equal(normalizeMessage({ type }, "s").type, type);
  }
  assert.equal(normalizeMessage({ type: "weird" }, "s").type, "received");
});

test("normalizeMessage 过滤坏片段", () => {
  const message = normalizeMessage(
    { segments: [{ type: "text", text: "a" }, null, "junk", { type: "image", url: "u" }] },
    "s",
  );
  assert.equal(message.segments.length, 2);
  assert.equal(message.segments[1].url, "u");
});

test("normalizeSession 从参与者推断名称", () => {
  const session = normalizeSession({
    id: "s1",
    is_group: true,
    participants: [{ id: "1", name: "小明", message_count: 3, last_seen: T }],
  });
  assert.equal(session.peerName, "小明");
  assert.equal(session.messageCount, 0);
  assert.equal(session.participants[0].name, "小明");
});

test("normalizeSession 在无名称时回退到 ID", () => {
  const session = normalizeSession({ id: "group:99", peer_id: "99", is_group: true });
  assert.equal(session.peerId, "99");
  assert.equal(session.peerName, "99");
});

// ── 去重与排序 ──────────────────────────────────────────

test("messageKey 优先用 id，缺 id 时用内容指纹", () => {
  assert.equal(messageKey({ id: "5", sessionId: "s" }), "id:5");
  const fingerprint = messageKey({ id: null, sessionId: "s", timestamp: 1, type: "sent", senderId: "u", content: "x" });
  assert.match(fingerprint, /^local:s:1:sent:u:x$/);
});

test("dedupeMessages 按 id 去重且保持时间升序", () => {
  const messages = dedupeMessages([
    { id: "2", sessionId: "s", timestamp: T + 10 },
    { id: "1", sessionId: "s", timestamp: T },
    { id: "2", sessionId: "s", timestamp: T + 10 },
  ]);
  assert.deepEqual(messages.map((m) => m.id), ["1", "2"]);
});

test("dedupeMessages 丢弃缺 sessionId 的脏数据", () => {
  const messages = dedupeMessages([{ id: "1" }, { id: "2", sessionId: "s", timestamp: T }]);
  assert.equal(messages.length, 1);
});

test("dedupeMessages 相同时间戳按 id 稳定排序", () => {
  const messages = dedupeMessages([
    { id: "10", sessionId: "s", timestamp: T },
    { id: "9", sessionId: "s", timestamp: T },
  ]);
  assert.deepEqual(messages.map((m) => m.id), ["9", "10"]);
});

test("mergeMessages 幂等合并历史与实时消息", () => {
  const existing = [{ id: "1", sessionId: "s", timestamp: T }];
  const incoming = [{ id: "1", sessionId: "s", timestamp: T }, { id: "2", sessionId: "s", timestamp: T + 1 }];
  assert.equal(mergeMessages(existing, incoming).length, 2);
  assert.equal(mergeMessages([], []).length, 0);
});

// ── 会话排序与匹配 ──────────────────────────────────────

test("compareSessions 置顶优先", () => {
  const pinned = { peerName: "a", pinned: true, lastActivity: 1 };
  const normal = { peerName: "b", pinned: false, lastActivity: 999 };
  assert.ok(compareSessions(pinned, normal) < 0);
});

test("compareSessions 按最后活跃时间倒序", () => {
  const newer = { peerName: "a", lastActivity: 0, lastMessage: { timestamp: T + 100 } };
  const older = { peerName: "b", lastActivity: 0, lastMessage: { timestamp: T } };
  assert.ok(compareSessions(newer, older) < 0);
});

test("matchesSession 空查询匹配全部", () => {
  assert.equal(matchesSession({ peerName: "群" }, ""), true);
  assert.equal(matchesSession({ peerName: "群" }, "   "), true);
});

test("matchesSession 覆盖名称、ID 与平台，忽略大小写", () => {
  const session = { peerName: "技术群", peerId: "123", platform: "Aiocqhttp" };
  assert.equal(matchesSession(session, "技术"), true);
  assert.equal(matchesSession(session, "123"), true);
  assert.equal(matchesSession(session, "aiocqhttp"), true);
  assert.equal(matchesSession(session, "不存在"), false);
});

// ── 预览与文本 ──────────────────────────────────────────

test("getSessionPreview 区分 sent / bot / received", () => {
  const base = { peerName: "群", peerId: "1", isGroup: true };
  assert.equal(
    getSessionPreview({ ...base, lastMessage: { type: "sent", content: "hi", senderName: "你" } }),
    "你: hi",
  );
  assert.equal(
    getSessionPreview({ ...base, lastMessage: { type: "bot", content: "在", senderName: "Bot" } }),
    "Bot: 在",
  );
  assert.equal(
    getSessionPreview({ ...base, lastMessage: { type: "received", content: "喂", senderName: "小明" } }),
    "小明: 喂",
  );
});

test("getSessionPreview 无消息时展示群号/ID", () => {
  assert.equal(getSessionPreview({ isGroup: true, groupId: "88", peerId: "88" }), "群号 88");
  assert.equal(getSessionPreview({ isGroup: false, peerId: "u1" }), "ID u1");
});

test("segmentsToText 拼接文本与占位符", () => {
  const text = segmentsToText([
    { type: "text", text: "看图" },
    { type: "image", url: "u" },
    { type: "at", target: "9" },
  ]);
  assert.equal(text, "看图@9");
  assert.equal(segmentsToText([]), "");
  assert.equal(segmentsToText(null), "");
});

test("messageText 在无片段时回退 content", () => {
  assert.equal(messageText({ segments: [], content: "原文" }), "原文");
  assert.equal(messageText({ segments: [], content: "" }), "[非文本消息]");
  assert.equal(messageText({ segments: [{ type: "text", text: "片段" }], content: "原文" }), "片段");
});

test("stripMentionMarkup 把历史残留标签降级成可读文本", () => {
  // 修复前已经入库的老消息，界面上不能再显示尖括号
  assert.equal(stripMentionMarkup('<mention id="42"/> 在吗'), "@某人 在吗");
  assert.equal(stripMentionMarkup("<mention>小明</mention>你好"), "@小明你好");
  assert.equal(stripMentionMarkup("<mention:88>你好"), "@某人你好");
  assert.equal(stripMentionMarkup("普通消息"), "普通消息");
  assert.equal(stripMentionMarkup(""), "");
  assert.equal(stripMentionMarkup(null), "");
});

test("segmentsToText 清洗文本片段里的标签", () => {
  const text = segmentsToText([
    { type: "text", text: '<mention id="1"/>看图' },
    { type: "at", target: "9", text: "小红" },
  ]);
  assert.equal(text, "@某人看图@小红");
});

test("imageSources 只取图片片段且有值", () => {
  const sources = imageSources({
    segments: [
      { type: "image", url: "https://a/1.png" },
      { type: "image", file: "/tmp/2.png" },
      { type: "image" },
      { type: "text", text: "x" },
    ],
  });
  assert.deepEqual(sources, ["https://a/1.png", "/tmp/2.png"]);
  assert.deepEqual(imageSources({ segments: null }), []);
});

// ── 头像与 ID ───────────────────────────────────────────

test("getInitials 中文取首字，英文取首字母", () => {
  assert.equal(getInitials("张三"), "张");
  assert.equal(getInitials("John Doe"), "JD");
  assert.equal(getInitials("alice"), "AL");
  assert.equal(getInitials(""), "?");
  assert.equal(getInitials(null), "?");
});

test("compactId 仅在超长时折叠", () => {
  assert.equal(compactId("123"), "123");
  assert.match(compactId("123456789012345678"), /^123456…/);
});

test("unreadLabel 上限 99+", () => {
  assert.equal(unreadLabel(0), "");
  assert.equal(unreadLabel(3), "3");
  assert.equal(unreadLabel(100), "99+");
});

// ── 时间 ────────────────────────────────────────────────

test("dayKey 同一时刻不同日期返回不同键", () => {
  assert.equal(dayKey(0), "unknown");
  assert.notEqual(dayKey(T), dayKey(T + 86400));
});

test("formatDateLabel 识别今天与昨天", () => {
  const now = T * 1000;
  assert.equal(formatDateLabel(T, now), "今天");
  assert.equal(formatDateLabel(T - 86400, now), "昨天");
  assert.match(formatDateLabel(T - 86400 * 20, now), /\d{4}/);
});

test("formatSessionTime 今天只显示时刻", () => {
  const now = T * 1000;
  assert.match(formatSessionTime(T, now), /^\d{2}:\d{2}$/);
  assert.equal(formatSessionTime(0, now), "");
});

// ── 高亮与分组 ──────────────────────────────────────────

test("splitHighlight 切出命中片段", () => {
  const parts = splitHighlight("我有三个苹果", "苹果");
  assert.deepEqual(parts, [
    { text: "我有三个", hit: false },
    { text: "苹果", hit: true },
  ]);
});

test("splitHighlight 支持多处命中与无命中", () => {
  const parts = splitHighlight("aXbXc", "X");
  assert.equal(parts.filter((p) => p.hit).length, 2);
  assert.deepEqual(splitHighlight("abc", "z"), [{ text: "abc", hit: false }]);
  assert.deepEqual(splitHighlight("abc", ""), [{ text: "abc", hit: false }]);
});

test("shouldGroupWithPrevious 同人相邻短时间才合并", () => {
  const a = { type: "received", senderId: "1", senderName: "小明", timestamp: T };
  const close = { ...a, timestamp: T + 60 };
  const far = { ...a, timestamp: T + 3600 };
  const otherUser = { ...close, senderId: "2" };
  assert.equal(shouldGroupWithPrevious(close, a), true);
  assert.equal(shouldGroupWithPrevious(far, a), false);
  assert.equal(shouldGroupWithPrevious(otherUser, a), false);
  assert.equal(shouldGroupWithPrevious(a, null), false);
});

test("isNearBottom 用阈值判断", () => {
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 900, clientHeight: 100 }), true);
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 100, clientHeight: 100 }), false);
});

test("normalizeHistory 统一游标类型", () => {
  assert.deepEqual(normalizeHistory({ has_more: true, next_cursor: 5 }), {
    hasMore: true,
    nextCursor: "5",
  });
  assert.deepEqual(normalizeHistory(null), { hasMore: false, nextCursor: null });
});

test("str 处理 null/undefined", () => {
  assert.equal(str(null), "");
  assert.equal(str(undefined, "d"), "d");
  assert.equal(str(0), "0");
});

// ── 渲染计划与差异 ──────────────────────────────────────

test("planMessages 生成日期分隔符与消息节点", () => {
  const plan = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "B", senderId: "b",
        content: "y", timestamp: T + 10 },
    ],
    "s",
  );
  assert.deepEqual(plan.map((p) => p.kind), ["date", "message", "message"]);
  assert.equal(plan[0].label, formatDateLabel(T));
});

test("planMessages 跨天时插入第二个分隔符", () => {
  const plan = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 86400 },
    ],
    "s",
  );
  assert.equal(plan.filter((p) => p.kind === "date").length, 2);
});

test("planMessages 相邻同人短消息标记为 grouped", () => {
  const plan = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 30 },
      { id: "3", sessionId: "s", type: "received", senderName: "B", senderId: "b",
        content: "z", timestamp: T + 40 },
    ],
    "s",
  );
  const messages = plan.filter((p) => p.kind === "message");
  assert.deepEqual(messages.map((m) => m.grouped), [false, true, false]);
});

test("planMessages 空列表返回空计划", () => {
  assert.deepEqual(planMessages([], "s"), []);
  assert.deepEqual(planMessages(null, "s"), []);
});

test("diffPlan 检测纯追加", () => {
  const first = planMessages(
    [{ id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
       content: "x", timestamp: T }],
    "s",
  );
  const second = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 10 },
    ],
    "s",
  );
  assert.deepEqual(diffPlan(first, second), { mode: "append", start: first.length, end: second.length });
});

test("diffPlan 检测纯截断", () => {
  const full = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 10 },
    ],
    "s",
  );
  const trimmed = full.slice(0, 2);
  assert.deepEqual(diffPlan(full, trimmed), { mode: "truncate", start: 2, end: full.length });
});

test("diffPlan 中间修改需要重建", () => {
  const a = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "x", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 10 },
    ],
    "s",
  );
  const b = planMessages(
    [
      { id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "改了", timestamp: T },
      { id: "2", sessionId: "s", type: "received", senderName: "A", senderId: "a",
        content: "y", timestamp: T + 10 },
    ],
    "s",
  );
  assert.equal(diffPlan(a, b).mode, "rebuild");
});

test("diffPlan 相同计划视为无需变更的 append", () => {
  const plan = planMessages(
    [{ id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
       content: "x", timestamp: T }],
    "s",
  );
  assert.deepEqual(diffPlan(plan, plan), { mode: "append", start: plan.length, end: plan.length });
});

test("diffPlan 首次渲染走 append（旧计划为空）", () => {
  const plan = planMessages(
    [{ id: "1", sessionId: "s", type: "received", senderName: "A", senderId: "a",
       content: "x", timestamp: T }],
    "s",
  );
  assert.deepEqual(diffPlan([], plan), { mode: "append", start: 0, end: plan.length });
});

// ── 虚拟滚动窗口 ────────────────────────────────────────────

test("computeWindow 空列表返回空窗口", () => {
  assert.deepEqual(computeWindow({ count: 0 }), {
    start: 0, end: 0, topPad: 0, bottomPad: 0, offsets: [0],
  });
});

test("computeWindow 贴顶时从 0 开始且至少渲染 minRender 条", () => {
  const win = computeWindow({
    count: 100, heights: [], scrollTop: 0, viewport: 300,
    forecast: 100, overscan: 0, minRender: 5,
  });
  assert.equal(win.start, 0);
  assert.equal(win.end, 5);
  assert.equal(win.topPad, 0);
  assert.equal(win.bottomPad, 9500);
});

test("computeWindow 滚到中间时按估算高度切片", () => {
  const win = computeWindow({
    count: 100, heights: [], scrollTop: 1000, viewport: 300,
    forecast: 100, overscan: 0, minRender: 1,
  });
  assert.equal(win.start, 10);
  assert.equal(win.end, 13);
  assert.equal(win.topPad, 1000);
  assert.equal(win.bottomPad, 8700);
});

test("computeWindow 用实测高度覆盖估算值", () => {
  const heights = new Array(100).fill(100);
  heights[10] = 400; // 一张大图：占满视口，后面的消息被推到窗口外
  const win = computeWindow({
    count: 100, heights, scrollTop: 1000, viewport: 300,
    forecast: 100, overscan: 0, minRender: 2,
  });
  assert.equal(win.start, 10);
  assert.equal(win.end, 12, "高图占满视口时只需渲染它自己 + minRender 兜底");
  assert.equal(win.topPad, 1000);
});

test("computeWindow pinToEnd 时窗口贴底", () => {
  const win = computeWindow({
    count: 100, heights: [], scrollTop: 0, viewport: 300,
    forecast: 100, minRender: 5, pinToEnd: true,
  });
  assert.equal(win.end, 100);
  assert.equal(win.start, 95);
  assert.equal(win.bottomPad, 0);
});

test("computeWindow 条数少于 minRender 时全部渲染", () => {
  const win = computeWindow({
    count: 3, heights: [], scrollTop: 0, viewport: 300,
    forecast: 100, minRender: 5,
  });
  assert.deepEqual(win, { start: 0, end: 3, topPad: 0, bottomPad: 0, offsets: [0, 100, 200, 300] });
});

test("computeWindow 返回累计偏移，供滚动锚点定位", () => {
  const win = computeWindow({
    count: 3, heights: [10, 20, 30], scrollTop: 0, viewport: 100,
    forecast: 10, overscan: 0, minRender: 1,
  });
  assert.deepEqual(win.offsets, [0, 10, 30, 60]);
});

test("windowAnchor 找出视口顶部落在哪一项、项内偏移多少", () => {
  const offsets = [0, 100, 250, 400];
  assert.deepEqual(windowAnchor(offsets, 0), { index: 0, delta: 0 });
  assert.deepEqual(windowAnchor(offsets, 120), { index: 1, delta: 20 });
  assert.deepEqual(windowAnchor(offsets, 250), { index: 2, delta: 0 });
  // 滚过末尾时钉在最后一项，不能算出界索引
  assert.deepEqual(windowAnchor(offsets, 9999), { index: 2, delta: 9749 });
  assert.equal(windowAnchor([], 100), null);
});

test("anchorScrollTop 用实测高度把锚点放回原位，抵消估算误差", () => {
  // 窗口从第 10 项开始；锚点在第 14 项、项内偏移 50。
  // 估算（forecast=100）时锚点应在 1000 + 400 + 50；
  // 但前四项实测是 60/60/380/60，真实位置必须按实测算。
  const heights = new Array(10).fill(0).concat([60, 60, 380, 60]);
  assert.equal(
    anchorScrollTop({ topPad: 1000, start: 10, index: 14, heights, forecast: 100, delta: 50 }),
    1000 + 560 + 50,
  );
  // 没测过的高度退回估算值，不能当 0 算短
  assert.equal(
    anchorScrollTop({ topPad: 0, start: 0, index: 2, heights: [0, 40], forecast: 90, delta: 0 }),
    40 + 90,
  );
  // 锚点就在窗口顶部时位置就是 topPad
  assert.equal(
    anchorScrollTop({ topPad: 700, start: 12, index: 12, heights: [], forecast: 90, delta: 0 }),
    700,
  );
});

test("窗口平移时按锚点回正，内容不会跳一大截", () => {
  // 200 条消息，每 9 条一张大图：估算高度和实测差得远，正是以前「跳一大截」的场景
  const count = 200;
  const realHeights = Array.from({ length: count }, (_, index) => (index % 9 === 4 ? 380 : 62));
  const measured = new Array(count).fill(0);
  const forecast = () => {
    const known = measured.filter((height) => height > 0);
    if (!known.length) return 56;
    return Math.max(24, known.reduce((sum, height) => sum + height, 0) / known.length);
  };
  const layoutTop = (win, index) => {
    let top = win.topPad;
    for (let cursor = win.start; cursor < index; cursor += 1) top += measured[cursor] || forecast();
    return top;
  };
  // 和 app.js 的 renderVirtualWindow 同序：算窗口 → 取锚点 → 渲染并测量 → 回正
  const computeStep = (scrollTop) => computeWindow({
    count,
    heights: measured,
    scrollTop,
    viewport: 620,
    forecast: forecast(),
    overscan: 800,
    minRender: 30,
  });
  const measure = (win) => {
    for (let index = win.start; index < win.end; index += 1) measured[index] = realHeights[index];
  };

  let win = computeStep(0);
  measure(win);
  let scrollTop = Math.max(0, win.topPad + realHeights.reduce((sum, height) => sum + height, 0) - 620);

  for (let step = 0; step < 10; step += 1) {
    // 用户往上滚 400px
    scrollTop -= 400;
    win = computeStep(scrollTop);
    const anchor = windowAnchor(win.offsets, scrollTop);
    const screenBefore = layoutTop(win, anchor.index) - scrollTop;

    measure(win);
    scrollTop = anchorScrollTop({
      topPad: win.topPad,
      start: win.start,
      index: anchor.index,
      heights: measured,
      forecast: forecast(),
      delta: anchor.delta,
    });

    const screenAfter = layoutTop(win, anchor.index) - scrollTop;
    assert.ok(
      Math.abs(screenAfter - screenBefore) < 1,
      `第 ${step + 1} 次窗口平移后锚点移动了 ${(screenAfter - screenBefore).toFixed(1)}px`,
    );
  }
});

// ── 消息归一化：平台消息 id / 失败态 / 转发嵌套 ──────────────

test("normalizeMessage 保留 platform_message_id 供引用使用", () => {
  const message = normalizeMessage(
    { id: 7, session_id: "s", platform_message_id: "m-42", content: "hi" },
    "s",
  );
  assert.equal(message.platformMessageId, "m-42");
});

test("normalizeMessage 保留发送失败标记，便于渲染重试按钮", () => {
  const message = normalizeMessage({ id: null, session_id: "s", failed: true, content: "x" }, "s");
  assert.equal(message.failed, true);
});

test("normalizeSegment 保留合并转发的嵌套消息", () => {
  const [segment] = [{
    type: "forward", text: "[聊天记录]", messages: [
      { name: "小明", uin: "42", segments: [{ type: "text", text: "早" }] },
    ],
  }].map(normalizeSegment);
  assert.equal(segment.messages.length, 1);
  assert.equal(segment.messages[0].name, "小明");
  assert.deepEqual(segment.messages[0].segments, [{ type: "text", text: "早" }]);
});
