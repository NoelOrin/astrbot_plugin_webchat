/**
 * scroll-layout.mjs —— 真实布局桩下的滚动行为冒烟测试。
 *
 * 为什么单独一个文件：jsdom 没有布局，`offsetHeight` / `scrollHeight` 恒为 0，
 * 于是「按估算行高撑出的假底部」这类 bug 在 dom-smoke 里根本不可见——
 * 0.4.15 的「切会话停在顶部/中段」就是这么漏出去的。
 *
 * 这里给消息列表补一套最小但真实的布局模型：
 *   - 每条消息的行高由内容长度决定（长文更高、图片占位更高）
 *   - #messages.scrollHeight = top spacer + 已渲染行高之和 + bottom spacer
 *   - scrollTop 被钳制在 [0, scrollHeight - clientHeight]
 *
 * 断言锁住的是「会话级滚动意图」的完整行为。
 *
 * 运行：node tests/frontend/scroll-layout.mjs
 */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../../pages/terminal",
);
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");

const dom = new JSDOM(html, {
  url: "http://localhost/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

const results = [];
function check(name, ok, extra = "") {
  results.push([ok ? "PASS" : "FAIL", name, extra]);
}

// ── 布局模型 ────────────────────────────────────────────────
// 行高：基准 34 + 每 20 个字符多 18px，带图片再多 150px。
// 这样长文/图片消息和短消息差得足够大，估算误差能真实暴露出来。
const ROW_BASE = 34;
const IMAGE_EXTRA = 150;
function rowHeight(el) {
  const text = el.querySelector(".message-text")?.textContent || "";
  const images = el.querySelectorAll(".message-image-placeholder, .message-image").length;
  return ROW_BASE + Math.ceil(text.length / 20) * 18 + images * IMAGE_EXTRA;
}

function installLayout() {
  const list = window.document.getElementById("messages");
  const items = window.document.getElementById("message-items");
  const top = window.document.getElementById("virtual-top");
  const bottom = window.document.getElementById("virtual-bottom");

  const px = (el) => parseFloat(el?.style?.height || "0") || 0;

  Object.defineProperty(list, "clientHeight", { get: () => 600, configurable: true });
  Object.defineProperty(list, "scrollHeight", {
    get: () => {
      let sum = px(top) + px(bottom);
      for (const row of items.children) sum += rowHeight(row);
      return Math.round(sum);
    },
    configurable: true,
  });
  // scrollTop 必须钳制：真实浏览器写超了会被改成 maxTop，
  // app 依赖这个行为来匹配「程序性滚动」的目标值。
  let scrollTopValue = 0;
  Object.defineProperty(list, "scrollTop", {
    get: () => scrollTopValue,
    set: (value) => {
      const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
      scrollTopValue = Math.min(Math.max(0, Math.round(Number(value) || 0)), maxTop);
    },
    configurable: true,
  });
  list.scrollTo = (opts) => {
    if (opts && typeof opts.top === "number") list.scrollTop = opts.top;
  };
}

// ── bridge 桩 ───────────────────────────────────────────────
const now = Math.floor(Date.now() / 1000);
const LONG_ID = "group:long";

function makeMessage(id, index) {
  const withImage = index % 6 === 0;
  const long = index % 4 === 0;
  const content = withImage
    ? `图片消息 ${index}`
    : long
      ? `${"很长的正文内容 ".repeat(8)}${index}`
      : `普通消息 ${index}`;
  const segments = [{ type: "text", text: content }];
  if (withImage) segments.push({ type: "image", url: `https://example.com/${index}.png` });
  return {
    id, session_id: LONG_ID, type: index % 2 ? "received" : "sent",
    sender: index % 2 ? "小明" : "你", sender_id: index % 2 ? "u1" : "me",
    sender_name: index % 2 ? "小明" : "你", content, segments,
    timestamp: now - (160 - index) * 60,
  };
}

const longMessages = [];
for (let index = 0; index < 160; index += 1) longMessages.push(makeMessage(1000 + index, index));

const sessions = [
  {
    id: LONG_ID, platform: "aiocqhttp", peer_id: "111", peer_name: "长会话群",
    group_id: "111", group_name: "长会话群", is_group: true, pinned: false,
    message_count: 160, last_activity: now,
    participants: [{ id: "u1", name: "小明", message_count: 80, last_seen: now }],
    messages: longMessages, history: { has_more: true, next_cursor: 1000, limit: 40 },
  },
  {
    id: "friend:short", platform: "aiocqhttp", peer_id: "9", peer_name: "小红",
    is_group: false, message_count: 1, last_activity: now - 300, participants: [],
    messages: [{ id: 3, session_id: "friend:short", type: "bot", sender: "Bot",
      sender_name: "Bot", content: "自动回复", segments: [{ type: "text", text: "自动回复" }],
      timestamp: now - 300 }],
    history: { has_more: false, next_cursor: null, limit: 40 },
  },
];

window.AstrBotPluginView = {
  ready: async () => ({ isDark: true, locale: "zh-CN" }),
  onContext: () => {},
  apiGet: async (endpoint, params) => {
    if (endpoint === "sessions") {
      return { sessions, total: sessions.length, offset: 0, limit: 50, has_more: false };
    }
    if (endpoint === "history") {
      const before = Number(params?.before_id || 0);
      if (!before) return { messages: [], has_more: false, next_cursor: null };
      const older = longMessages.filter((m) => m.id < before).slice(-40);
      return { messages: older, has_more: older.length >= 40, next_cursor: older[0]?.id ?? null };
    }
    if (endpoint === "updates") {
      return { messages: [], last_id: Number(params?.after_id || 0), has_more: false };
    }
    if (endpoint === "search") {
      const q = String(params?.q || "");
      return { results: longMessages.filter((m) => m.content.includes(q)) };
    }
    return {};
  },
  apiPost: async (endpoint, body) => {
    if (endpoint === "send") {
      return { data: { id: 9000, session_id: body.session_id, type: "sent",
        sender: "Terminal", sender_name: "你", content: body.message,
        segments: body.segments?.length ? body.segments : [{ type: "text", text: body.message }],
        timestamp: now } };
    }
    return { status: "ok" };
  },
  subscribeSSE: async (endpoint, handlers, params) => {
    window.__sseHandlers = handlers;
    setTimeout(() => handlers.onOpen(), 0);
    return "sub-1";
  },
  unsubscribeSSE: async () => {},
};

window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.CSS = window.CSS || {};
window.CSS.escape = (s) => String(s).replace(/"/g, '\\"');
window.URL.createObjectURL = () => "blob:mock";
window.URL.revokeObjectURL = () => {};
window.navigator.clipboard = { writeText: async () => {} };
window.Element.prototype.scrollIntoView = function () {};

installLayout();

globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, "navigator", { value: window.navigator, configurable: true, writable: true });
globalThis.CSS = window.CSS;
globalThis.HTMLElement = window.HTMLElement;
globalThis.Element = window.Element;
globalThis.Node = window.Node;
globalThis.Event = window.Event;
globalThis.CustomEvent = window.CustomEvent;
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
globalThis.EventSource = window.EventSource;
globalThis.FileReader = window.FileReader;
globalThis.Blob = window.Blob;
globalThis.URL = window.URL;
globalThis.localStorage = window.localStorage;

const code = fs.readFileSync(path.join(root, "app.js"), "utf8")
  .replace('from "./webchat-lib.js"', `from "file://${path.join(root, "webchat-lib.js")}"`);
const tmp = path.join(root, "__scroll_layout_app.mjs");
fs.writeFileSync(tmp, code);
try {
  await import("file://" + tmp);
} finally {
  fs.unlinkSync(tmp);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const doc = window.document;
const list = () => doc.getElementById("messages");

function scrollState() {
  const el = list();
  return {
    top: Math.round(el.scrollTop),
    distance: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
    first: doc.querySelector("#message-items .message-row")?.dataset.messageId,
    last: [...doc.querySelectorAll("#message-items .message-row")].at(-1)?.dataset.messageId,
  };
}

function clickSession(name) {
  [...doc.querySelectorAll(".session-item")]
    .find((el) => el.textContent.includes(name))
    ?.click();
}

/** 模拟用户上翻：先 wheel（意图提前切换），再真实位移 + scroll 事件。 */
function userScrollTo(top) {
  const el = list();
  el.dispatchEvent(new window.WheelEvent("wheel", { deltaY: -200, bubbles: true }));
  el.scrollTop = top;
  el.dispatchEvent(new window.Event("scroll"));
}

await sleep(900);

// ── 1. 首屏进入长会话必须落在最新消息 ────────────────────────
{
  const state = scrollState();
  check("首屏进入长会话贴底", state.distance <= 2, JSON.stringify(state));
  check("首屏最后一条是最新消息", state.last === "1159", String(state.last));
  // 假底部（按估算行高撑出来）会比真底部矮一截，这里顺带守住 spacer 已按实测重算
  check("贴底时底部没有多余 spacer",
    parseFloat(doc.getElementById("virtual-bottom").style.height || "0") === 0,
    doc.getElementById("virtual-bottom").style.height);
}

// ── 2. 拖动滚动条上翻（没有 wheel/touch）：意图也要切到阅读 ──
{
  const el = list();
  el.scrollTop = 4000;
  el.dispatchEvent(new window.Event("scroll"));
  await sleep(300);
  const before = scrollState();

  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 5001, session_id: LONG_ID, type: "received", sender: "小明", sender_id: "u1",
    sender_name: "小明", content: "拖滚动条后到达的消息",
    segments: [{ type: "text", text: "拖滚动条后到达的消息" }], timestamp: now + 1 } });
  await sleep(400);

  const after = scrollState();
  check("拖动滚动条上翻后新消息不拽回底部",
    Math.abs(after.top - before.top) <= 2, `${before.top} -> ${after.top}`);
  check("阅读态显示新消息气泡", !doc.getElementById("new-message-pill").hidden);
}

// ── 3. 键盘上翻（PageUp 走 scroll 事件）同样切到阅读 ─────────
{
  const el = list();
  const before = scrollState();
  el.scrollTop = Math.max(0, before.top - 800);
  el.dispatchEvent(new window.Event("scroll"));
  await sleep(300);
  const afterKey = scrollState();

  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 5002, session_id: LONG_ID, type: "received", sender: "小明", sender_id: "u1",
    sender_name: "小明", content: "键盘上翻后到达的消息",
    segments: [{ type: "text", text: "键盘上翻后到达的消息" }], timestamp: now + 2 } });
  await sleep(400);

  const after = scrollState();
  check("键盘上翻后新消息不拽回底部",
    Math.abs(after.top - afterKey.top) <= 2, `${afterKey.top} -> ${after.top}`);
}

// ── 4. 滚回底部恢复跟随 ─────────────────────────────────────
{
  const el = list();
  el.scrollTop = el.scrollHeight;
  el.dispatchEvent(new window.Event("scroll"));
  await sleep(300);

  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 5003, session_id: LONG_ID, type: "received", sender: "小明", sender_id: "u1",
    sender_name: "小明", content: "回底后到达的消息",
    segments: [{ type: "text", text: "回底后到达的消息" }], timestamp: now + 3 } });
  await sleep(400);

  const after = scrollState();
  check("滚回底部后新消息继续跟随", after.last === "5003" && after.distance <= 2, JSON.stringify(after));
  check("恢复跟随后气泡消失", doc.getElementById("new-message-pill").hidden);
}

// ── 5. 无未读切走再切回：恢复上次阅读位置 ───────────────────
{
  userScrollTo(3500);
  await sleep(300);
  const reading = scrollState();

  clickSession("小红");
  await sleep(400);
  clickSession("长会话群");
  await sleep(600);

  const restored = scrollState();
  check("无未读切回恢复阅读位置",
    Math.abs(restored.top - reading.top) <= 60, `${reading.top} -> ${restored.top}`);
  check("恢复阅读位置后仍停在历史里", restored.distance > 1000, String(restored.distance));
}

// ── 6. 有未读切回：贴底看最新 ───────────────────────────────
{
  // 阅读态下收到消息 → 产生未读
  userScrollTo(3500);
  await sleep(200);
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 5004, session_id: LONG_ID, type: "received", sender: "小明", sender_id: "u1",
    sender_name: "小明", content: "阅读时到达的消息",
    segments: [{ type: "text", text: "阅读时到达的消息" }], timestamp: now + 4 } });
  await sleep(400);
  check("阅读时到达的消息计入未读",
    doc.querySelector(".session-item.active .unread-badge")?.textContent === "1",
    doc.querySelector(".session-item.active .unread-badge")?.textContent || "无");

  clickSession("小红");
  await sleep(400);
  clickSession("长会话群");
  await sleep(600);

  const after = scrollState();
  check("有未读切回贴底", after.distance <= 2 && after.last === "5004", JSON.stringify(after));
}

// ── 7. 图片开关切换不能把视口甩开 ───────────────────────────
{
  doc.getElementById("btn-images").click();
  await sleep(600);
  check("开启图片加载后仍贴底", scrollState().distance <= 2, JSON.stringify(scrollState()));
  doc.getElementById("btn-images").click();
  await sleep(600);
  check("关闭图片加载后仍贴底", scrollState().distance <= 2, JSON.stringify(scrollState()));
}

// ── 8. 搜索跳转进入阅读态，后续新消息不拽走 ─────────────────
{
  const input = doc.getElementById("message-search");
  input.value = "普通消息 12";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(700);

  const hit = doc.querySelector("#search-results .search-hit");
  check("搜索命中可点击", Boolean(hit));
  hit?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(700);
  const jumped = scrollState();

  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 5005, session_id: LONG_ID, type: "received", sender: "小明", sender_id: "u1",
    sender_name: "小明", content: "跳转后到达的消息",
    segments: [{ type: "text", text: "跳转后到达的消息" }], timestamp: now + 5 } });
  await sleep(400);

  const after = scrollState();
  check("搜索跳转后新消息不拽走视口",
    Math.abs(after.top - jumped.top) <= 2, `${jumped.top} -> ${after.top}`);
  check("搜索跳转确实定位到历史", jumped.distance > 500, String(jumped.distance));
}

const failed = results.filter((row) => row[0] === "FAIL");
for (const [status, name, extra] of results) {
  console.log(`${status === "PASS" ? "✔" : "✘"} ${name}${status === "FAIL" ? "  <= " + extra : ""}`);
}
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
