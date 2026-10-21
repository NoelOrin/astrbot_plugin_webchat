import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../../pages/terminal",
);
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");

const dom = new JSDOM(html, { url: "http://localhost/", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;

// 桩：AstrBot bridge
const calls = [];
const sessionsPayload = {
  sessions: [
    {
      id: "group:111", platform: "aiocqhttp", peer_id: "111", peer_name: "测试群",
      group_id: "111", is_group: true, pinned: false, message_count: 2, last_activity: 1700000500,
      participants: [{ id: "u1", name: "小明", message_count: 2, last_seen: 1700000500 }],
      messages: [
        { id: 1, session_id: "group:111", type: "received", sender: "小明", sender_id: "u1",
          sender_name: "小明", content: "第一条", segments: [{type:"text",text:"第一条"}],
          timestamp: 1700000400 },
        { id: 2, session_id: "group:111", type: "received", sender: "小明", sender_id: "u1",
          sender_name: "小明", content: "看图", segments: [
            {type:"text",text:"看图"}, {type:"image",url:"https://example.com/a.png"},
            {type:"at",target:"u2",text:"小红"}], timestamp: 1700000500 },
      ],
      history: { has_more: true, next_cursor: 1, limit: 40 },
    },
    { id: "aiocqhttp:FriendMessage:9", platform: "aiocqhttp", peer_id: "9", peer_name: "小红",
      is_group: false, message_count: 1, last_activity: 1700000300, participants: [],
      messages: [{ id: 3, session_id: "aiocqhttp:FriendMessage:9", type: "bot", sender: "Bot",
        sender_name: "Bot", content: "自动回复", segments: [{type:"text",text:"自动回复"}],
        timestamp: 1700000300 }],
      history: { has_more: false, next_cursor: null, limit: 40 } },
  ],
};

window.AstrBotPluginView = {
  ready: async () => ({ isDark: true, locale: "zh-CN" }),
  onContext: () => {},
  apiGet: async (endpoint, params) => {
    calls.push(["GET", endpoint, params]);
    if (endpoint === "sessions") {
      const offset = Number(params?.offset || 0);
      if (offset > 0) {
        return {
          sessions: [{
            id: "group:222", platform: "aiocqhttp", peer_id: "222", peer_name: "第三个群",
            group_id: "222", is_group: true, pinned: false, message_count: 0,
            last_activity: 1699000000, participants: [], messages: [],
            history: { has_more: false, next_cursor: null, limit: 40 },
          }],
          total: 3, offset, limit: 50, has_more: false,
        };
      }
      return { ...sessionsPayload, total: 3, offset: 0, limit: 50, has_more: true };
    }
    if (endpoint === "forward") {
      return { available: true, messages: [
        { name: "转发者", uin: "77", segments: [{ type: "text", text: "转发里的消息" }] },
      ] };
    }
    if (endpoint === "history") {
      // 尊重 since / before_id 语义，模拟真实后端
      if (params?.before_id) {
        return { messages: [
          { id: 0, session_id: "group:111", type: "received", sender: "小明", sender_id: "u1",
            sender_name: "小明", content: "更早", segments: [{type:"text",text:"更早"}],
            timestamp: 1700000000 }], has_more: false, next_cursor: null };
      }
      const since = Number(params?.since || 0);
      return { messages: since > 1700000600 ? [] : [], has_more: false, next_cursor: null };
    }
    if (endpoint === "search") return { results: [
      { id: 1, session_id: "group:111", type: "received", sender_name: "小明",
        content: "第一条", segments: [{type:"text",text:"第一条"}], timestamp: 1700000400 }] };
    if (endpoint === "updates") {
      // 轮询降级：新消息来自**另一个会话**，用来验证不再只轮询当前会话
      return { messages: [
        { id: 200, session_id: "aiocqhttp:FriendMessage:9", type: "received",
          sender: "轮询", sender_id: "u8", sender_name: "轮询", content: "轮询新消息",
          segments: [{type:"text",text:"轮询新消息"}], timestamp: 1700000900 }],
        last_id: 200, has_more: false };
    }
    if (endpoint === "platforms") return { platforms: [{ id: "p1", name: "OneBot", adapter: "aiocqhttp" }] };
    if (endpoint === "peers") return { peers: [{ id: "222", name: "另一个群" }] };
    if (endpoint === "export") return { text: "导出内容" };
    return {};
  },
  apiPost: async (endpoint, body) => {
    calls.push(["POST", endpoint, body]);
    if (endpoint === "send") {
      if (body.message === "这条会失败" && !window.__failOnce) {
        window.__failOnce = true;
        throw new Error("模拟网络中断");
      }
      return { data: {
        id: 99, session_id: body.session_id, type: "sent",
        sender: "Terminal", sender_name: "你", content: body.message,
        segments: body.segments?.length ? body.segments : [{type:"text",text:body.message}],
        timestamp: 1700000600 } };
    }
    return { status: "ok" };
  },
  subscribeSSE: async (endpoint, handlers) => {
    calls.push(["SSE", endpoint]);
    window.__sseHandlers = handlers;
    setTimeout(() => handlers.onOpen(), 0);
    return "sub-1";
  },
  unsubscribeSSE: async () => {},
};

window.matchMedia = (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} });
window.CSS = window.CSS || {}; window.CSS.escape = (s) => String(s).replace(/"/g, '\\"');
window.URL.createObjectURL = () => "blob:mock";
window.URL.revokeObjectURL = () => {};
window.navigator.clipboard = { writeText: async () => {} };
window.Element.prototype.scrollIntoView = function () {};
// jsdom 未实现 scrollTo / scrollHeight 布局，打桩到真实浏览器等价行为
window.Element.prototype.scrollTo = function (opts) {
  if (opts && typeof opts.top === "number") this.scrollTop = opts.top;
};

// 把 app.js 与 webchat-lib.js 作为 ESM 加载
const code = fs.readFileSync(path.join(root, "app.js"), "utf8")
  .replace('from "./webchat-lib.js"', `from "file://${path.join(root, "webchat-lib.js")}"`);

// 把 jsdom 的 window 暴露为 Node 全局，供 ESM 模块访问
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, "navigator", {
  value: window.navigator, configurable: true, writable: true,
});
globalThis.CSS = window.CSS;
globalThis.EventSource = window.EventSource;
globalThis.FileReader = window.FileReader;
globalThis.Blob = window.Blob;
globalThis.URL = window.URL;
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

const tmp = path.join(root, "__smoke_app.mjs");
fs.writeFileSync(tmp, code);
await import("file://" + tmp);

await new Promise((r) => setTimeout(r, 400));

const doc = window.document;
const results = [];
const check = (name, cond, extra = "") => results.push([cond ? "PASS" : "FAIL", name, extra]);

check("会话列表渲染出 2 项", doc.querySelectorAll(".session-item").length === 2,
  doc.querySelectorAll(".session-item").length);
check("状态为实时连接", doc.getElementById("status-text").textContent === "实时连接",
  doc.getElementById("status-text").textContent);
check("默认选中第一个会话", doc.getElementById("conversation-title").textContent === "测试群",
  doc.getElementById("conversation-title").textContent);
check("消息渲染出 2 条", doc.querySelectorAll(".message-row").length === 2,
  doc.querySelectorAll(".message-row").length);
// 默认不加载图片：只出占位按钮，绝不能出现带 src 的 img（否则浏览器照样下载）
check("默认不自动渲染图片", doc.querySelectorAll(".message-image").length === 0,
  doc.querySelectorAll(".message-image").length);
check("图片渲染为占位按钮", doc.querySelectorAll(".message-image-placeholder").length === 1,
  doc.querySelectorAll(".message-image-placeholder").length);
check("@ 片段渲染为 chip", doc.querySelectorAll(".segment-chip.at").length === 1);

// 点占位 → 只加载这一张，且走插件代理
{
  const placeholder = doc.querySelector(".message-image-placeholder");
  placeholder?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  const image = doc.querySelector(".message-image");
  check("点占位后加载该图", Boolean(image), image ? "有 img" : "没有 img");
  check("图片走插件代理", (image?.src || "").includes("/media?url="), image?.src);
}

// 工具栏开关：打开后其余图片自动加载，关闭后回到占位
{
  const toggle = doc.getElementById("btn-images");
  check("图片开关按钮存在", Boolean(toggle));
  check("图片开关默认按下状态为 false", toggle?.getAttribute("aria-pressed") === "false",
    toggle?.getAttribute("aria-pressed"));
  toggle?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  check("开启后自动加载图片", doc.querySelectorAll(".message-image").length === 1,
    doc.querySelectorAll(".message-image").length);
  check("开启后 aria-pressed 为 true", toggle?.getAttribute("aria-pressed") === "true",
    toggle?.getAttribute("aria-pressed"));
  toggle?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  check("关闭后回到占位", doc.querySelectorAll(".message-image").length === 0 &&
    doc.querySelectorAll(".message-image-placeholder").length === 1,
    JSON.stringify({ img: doc.querySelectorAll(".message-image").length,
      holder: doc.querySelectorAll(".message-image-placeholder").length }));
}
check("日期分隔符存在", doc.querySelectorAll(".date-separator").length >= 1);
check("加载更早按钮可见", !doc.getElementById("load-more").hidden);
check("参与者被渲染", doc.querySelectorAll(".participant").length === 1);
check("置顶按钮存在", Boolean(doc.getElementById("btn-pin")));
check("自动发现按钮存在", Boolean(doc.getElementById("btn-discover")));

// 点发现按钮 → 调 /discover 并刷新会话列表
{
  const before = calls.filter(([m, e]) => m === "POST" && e === "discover").length;
  doc.getElementById("btn-discover").click();
  await new Promise((r) => setTimeout(r, 60));
  const after = calls.filter(([m, e]) => m === "POST" && e === "discover").length;
  check("点击发现会调用 /discover", after === before + 1);
}

// 切到私聊会话，验证 bot 消息类名
const target = [...doc.querySelectorAll(".session-item")]
  .find((el) => el.textContent.includes("小红"));
check("找到小红会话按钮", Boolean(target));
target.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 60));
check("切换会话后标题更新", doc.getElementById("conversation-title").textContent === "小红",
  doc.getElementById("conversation-title").textContent);
check("bot 消息有专属样式类", doc.querySelectorAll(".message-row.bot").length === 1,
  doc.querySelectorAll(".message-row.bot").length);
// 切换会话必须清掉上一个会话的消息，不能残留
{
  const texts = [...doc.querySelectorAll("#message-content .message-row")]
    .map((el) => el.textContent || "");
  const leaked = texts.filter((t) => t.includes("第一条") || t.includes("看图"));
  check("切换会话后不残留上个会话的消息", leaked.length === 0, `残留 ${leaked.length} 条: ${leaked.join(" | ")}`);
  check("切换会话后只显示新会话消息", texts.length === 1, `实际 ${texts.length} 条`);
}

// 搜索
const searchInput = doc.getElementById("message-search");
searchInput.value = "第一条";
searchInput.dispatchEvent(new window.Event("input", { bubbles: true }));
await new Promise((r) => setTimeout(r, 500));
check("搜索结果渲染", doc.querySelectorAll(".search-hit").length === 1,
  doc.querySelectorAll(".search-hit").length);
check("命中关键词被高亮", doc.querySelectorAll(".search-hit-body mark").length === 1,
  doc.querySelectorAll(".search-hit-body mark").length);

// 加载更早消息
const loadMoreBtn = doc.getElementById("load-more");
if (!loadMoreBtn.hidden) {
  loadMoreBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  check("加载更早消息后行数增加", doc.querySelectorAll(".message-row").length === 1,
    doc.querySelectorAll(".message-row").length);
}

// 清空弹窗（替代 window.confirm）
const clearBtn = doc.getElementById("btn-clear");
clearBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 60));
check("清空弹出自定义确认框", doc.querySelectorAll(".dialog-overlay").length === 1,
  doc.querySelectorAll(".dialog-overlay").length);
const confirmBtn = doc.querySelector(".dialog-actions .button-danger");
confirmBtn?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 100));
const clearCall = calls.find((c) => c[1] === "clear");
check("确认后真的调用了 clear 接口", Boolean(clearCall), JSON.stringify(clearCall || null));
check("弹窗已关闭", doc.querySelectorAll(".dialog-overlay").length === 0);

// 新建会话弹窗
doc.getElementById("btn-open-session").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 200));
check("新建会话弹窗出现", doc.querySelectorAll(".dialog-overlay").length === 1);
check("平台下拉已填充", doc.querySelectorAll(".dialog-input option").length >= 3,
  doc.querySelectorAll(".dialog-input option").length);
check("好友列表已拉取", doc.querySelectorAll(".peer-item").length === 1,
  doc.querySelectorAll(".peer-item").length);

// 主题切换
const before = doc.documentElement.dataset.theme;
doc.getElementById("btn-theme").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
check("主题可切换", doc.documentElement.dataset.theme !== before,
  `${before} -> ${doc.documentElement.dataset.theme}`);

// 紧凑模式
doc.getElementById("btn-compact").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
check("紧凑模式可切换", doc.documentElement.dataset.compact === "true",
  doc.documentElement.dataset.compact);

// SSE 消息推送
window.__sseHandlers.onMessage({ eventType: "message", parsed: {
  id: 100, session_id: "group:111", type: "received", sender: "新来的",
  sender_id: "u9", sender_name: "新来的", content: "实时消息",
  segments: [{type:"text",text:"实时消息"}], timestamp: 1700000700 } });
await new Promise((r) => setTimeout(r, 100));
const totalMessages = doc.querySelectorAll(".message-row").length;
const unreadBadges = doc.querySelectorAll(".unread-badge").length;
const pillVisible = !doc.getElementById("new-message-pill").hidden;
check("SSE 新消息进入当前会话或计入未读", totalMessages >= 1 || unreadBadges >= 1 || pillVisible,
  JSON.stringify({ totalMessages, unreadBadges, pillVisible }));

// 心跳帧只用于判活，不能产生任何消息或重渲染
{
  const rowsBefore = doc.querySelectorAll(".message-row").length;
  window.__sseHandlers.onMessage({ eventType: "ping", parsed: {} });
  await new Promise((r) => setTimeout(r, 30));
  const rowsAfter = doc.querySelectorAll(".message-row").length;
  check("ping 心跳不改变消息列表", rowsAfter === rowsBefore, `${rowsBefore} -> ${rowsAfter}`);
}

// ── 增量渲染：追加时不应重建已有节点 ──────────────────────
// 先切回群会话，确保追加消息落在当前渲染的会话上
const groupButton = [...doc.querySelectorAll(".session-item")]
  .find((el) => el.textContent.includes("测试群"));
groupButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 120));

const content = doc.getElementById("message-items");
const beforeNodes = [...content.children];
window.__sseHandlers.onMessage({ eventType: "message", parsed: {
  id: 101, session_id: "group:111", type: "received", sender: "追加者",
  sender_id: "u7", sender_name: "追加者", content: "追加的一条",
  segments: [{type:"text",text:"追加的一条"}], timestamp: 1700000800 } });
await new Promise((r) => setTimeout(r, 150));

const afterNodes = [...content.children];
const reused = beforeNodes.filter((node) => afterNodes.includes(node)).length;
check("增量渲染复用了已有节点", reused === beforeNodes.length,
  `reused ${reused}/${beforeNodes.length}`);
check("增量渲染确实追加了新节点", afterNodes.length > beforeNodes.length,
  `${beforeNodes.length} -> ${afterNodes.length}`);

// 删除消息应走 truncate/rebuild 且不残留
window.__sseHandlers.onMessage({ eventType: "message_removed",
  parsed: { id: 101, session_id: "group:111" } });
await new Promise((r) => setTimeout(r, 120));
const afterRemove = doc.querySelectorAll('[data-message-id="101"]').length;
check("message_removed 后节点被清掉", afterRemove === 0, afterRemove);

// 启动必须先走 SSE：boot() 先选会话再连流，selectSession 不能因为
// connected 还是 false 就提前 startPolling()，否则一启动就降级轮询。
{
  const bootPolls = calls.filter((c) => c[0] === "GET" && c[1] === "updates");
  check("启动阶段不提前降级轮询", bootPolls.length === 0, `updates 调用 ${bootPolls.length} 次`);
}

// 轮询降级：SSE 断开后，非当前会话的新消息也要及时到达并计入未读
window.__sseHandlers.onError();
await new Promise((r) => setTimeout(r, 200));
{
  const pollCalls = calls.filter((c) => c[0] === "GET" && c[1] === "updates");
  const friendRow = [...doc.querySelectorAll(".session-item")]
    .find((el) => el.textContent.includes("小红"));
  const friendUnread = Number(friendRow?.querySelector(".unread-badge")?.textContent || 0);
  check("SSE 断开后启动全局轮询", pollCalls.length >= 1, `updates 调用 ${pollCalls.length} 次`);
  check("轮询到非当前会话的新消息并计入未读", friendUnread >= 1,
    `未读 ${friendUnread}，会话项 ${friendRow?.textContent?.trim()}`);
}

// session_removed 事件
window.__sseHandlers.onMessage({ eventType: "session_removed", parsed: { id: "group:111" } });
await new Promise((r) => setTimeout(r, 80));
check("会话删除后从列表移除", doc.querySelectorAll(".session-item").length === 1,
  doc.querySelectorAll(".session-item").length);

// ── 会话列表分页：底部「加载更多」 ──────────────────────────
const sessionsBefore = doc.querySelectorAll(".session-item").length;
check("会话列表有更多页时显示加载更多", !doc.getElementById("session-more").hidden,
  doc.getElementById("session-more").hidden);
doc.getElementById("session-more").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 150));
const pagedCall = calls.find((c) => c[0] === "GET" && c[1] === "sessions" && Number(c[2]?.offset) > 0);
check("加载更多会话带 offset 参数", Boolean(pagedCall), JSON.stringify(pagedCall?.[2] || null));
check("加载更多会话后列表追加", doc.querySelectorAll(".session-item").length === sessionsBefore + 1,
  `${sessionsBefore} -> ${doc.querySelectorAll(".session-item").length}`);
check("最后一页后隐藏加载更多", doc.getElementById("session-more").hidden === true,
  doc.getElementById("session-more").hidden);

// ── 合并转发：点开看聊天记录 ────────────────────────────────
{
  // 清掉前面「新建会话」测试遗留的弹窗，避免干扰计数
  doc.querySelectorAll("#dialog-region .dialog-overlay").forEach((node) => node.remove());
  const friendButton = [...doc.querySelectorAll(".session-item")]
    .find((el) => el.textContent.includes("小红"));
  friendButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));

  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 301, session_id: "aiocqhttp:FriendMessage:9", type: "received",
    sender: "小红", sender_id: "u9", sender_name: "小红", content: "[聊天记录]",
    segments: [{ type: "forward", text: "[聊天记录]", messages: [
      { name: "小明", uin: "42", segments: [{ type: "text", text: "早" }] },
      { name: "小红", uin: "9", segments: [{ type: "text", text: "早呀" }] },
    ] }],
    timestamp: 1700001000 } });
  await new Promise((r) => setTimeout(r, 120));

  const chip = doc.querySelector('[data-message-id="301"] .segment-chip.forward');
  check("合并转发渲染为可点击 chip", Boolean(chip), chip?.textContent);
  chip?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  check("点开合并转发弹出聊天记录", doc.querySelectorAll("#dialog-region .dialog-overlay").length === 1,
    doc.querySelectorAll("#dialog-region .dialog-overlay").length);
  check("聊天记录里渲染出嵌套消息", doc.querySelectorAll(".forward-row").length === 2,
    doc.querySelectorAll(".forward-row").length);
  const overlays = doc.querySelectorAll("#dialog-region .dialog-overlay");
  overlays[overlays.length - 1]?.querySelector(".button-primary")
    ?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  check("聊天记录弹窗可关闭", doc.querySelectorAll("#dialog-region .dialog-overlay").length === 0);
}

// ── 只有转发 id 的消息：点开时按需拉取内容 ──────────────────
{
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 305, session_id: "aiocqhttp:FriendMessage:9", type: "received",
    sender: "小红", sender_id: "u9", sender_name: "小红", content: "[合并转发]",
    segments: [{ type: "forward", text: "[合并转发]", id: "f-305" }],
    timestamp: 1700001400 } });
  await new Promise((r) => setTimeout(r, 120));

  const row = doc.querySelector('[data-message-id="305"]');
  check("转发消息不再重复显示占位正文", !row?.querySelector(".message-text"),
    row?.textContent?.trim());
  const chip = row?.querySelector(".segment-chip.forward");
  check("只有 id 的转发也是可点击 chip", Boolean(chip));
  chip?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 150));

  const fetchCall = calls.find(
    (c) => c[0] === "GET" && c[1] === "forward" && c[2]?.id === "f-305",
  );
  check("点开时按 id 拉取转发内容", Boolean(fetchCall), JSON.stringify(fetchCall?.[2] || null));
  check("按需拉取后显示聊天记录", doc.querySelectorAll("#dialog-region .forward-row").length === 1,
    doc.querySelectorAll("#dialog-region .forward-row").length);
  doc.querySelectorAll("#dialog-region .dialog-overlay").forEach((node) => node.remove());
}

// ── 引用回复：引用条 + 发送 segments ────────────────────────
{
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 302, session_id: "aiocqhttp:FriendMessage:9", type: "received",
    sender: "小红", sender_id: "u9", sender_name: "小红", content: "这条要引用",
    platform_message_id: "m-302",
    segments: [{ type: "text", text: "这条要引用" }], timestamp: 1700001100 } });
  await new Promise((r) => setTimeout(r, 120));

  const row = doc.querySelector('[data-message-id="302"]');
  const replyButton = [...(row?.querySelectorAll(".message-action") || [])]
    .find((el) => el.textContent === "引用");
  check("收到的消息有引用按钮", Boolean(replyButton));
  replyButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  check("点击引用后出现引用条", !doc.getElementById("reply-preview").hidden,
    doc.getElementById("reply-preview").hidden);
  check("引用条显示被引用内容", doc.getElementById("reply-preview-text").textContent.includes("这条要引用"),
    doc.getElementById("reply-preview-text").textContent);

  const input = doc.getElementById("input");
  input.value = "同意";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  doc.getElementById("composer")
    .dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 150));

  const sendCall = [...calls].reverse().find((c) => c[0] === "POST" && c[1] === "send");
  check("引用随消息一起提交", sendCall?.[2]?.segments?.[0]?.type === "reply",
    JSON.stringify(sendCall?.[2]?.segments || null));
  check("引用目标带平台消息 id", sendCall?.[2]?.segments?.[0]?.target === "m-302",
    sendCall?.[2]?.segments?.[0]?.target);
  check("发送成功后引用条消失", doc.getElementById("reply-preview").hidden === true,
    doc.getElementById("reply-preview").hidden);
}

// ── 收到引用消息：显示被引用的人与内容 ──────────────────────
{
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 303, session_id: "aiocqhttp:FriendMessage:9", type: "received",
    sender: "小红", sender_id: "u9", sender_name: "小红", content: "同意",
    segments: [
      { type: "reply", target: "m-302", text: "原消息",
        quote: { sender: "小明", text: "原消息内容" } },
      { type: "text", text: "同意" },
    ], timestamp: 1700001200 } });
  await new Promise((r) => setTimeout(r, 120));

  const block = doc.querySelector('[data-message-id="303"] .quote-block');
  check("引用消息渲染引用块", Boolean(block));
  check("引用块显示被引用人与内容",
    (block?.textContent || "").includes("回复 小明") &&
      (block?.textContent || "").includes("原消息内容"),
    block?.textContent);

  const replied = doc.querySelector('[data-message-id="303"] .message-text');
  check("引用不影响正文渲染", replied?.textContent === "同意", replied?.textContent);
}

// ── 图片原始链接：大图预览里能看到并复制 ────────────────────
{
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 304, session_id: "aiocqhttp:FriendMessage:9", type: "received",
    sender: "小红", sender_id: "u9", sender_name: "小红", content: "",
    segments: [{ type: "image", url: "https://example.com/original.png" }],
    timestamp: 1700001300 } });
  await new Promise((r) => setTimeout(r, 120));

  const holder = doc.querySelector('[data-message-id="304"] .message-image-placeholder');
  holder?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 60));

  const image = doc.querySelector('[data-message-id="304"] .message-image');
  check("长图/普通图都能点开预览", Boolean(image));
  image?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 60));

  check("预览里显示原始链接",
    doc.getElementById("lightbox-link").textContent === "https://example.com/original.png",
    doc.getElementById("lightbox-link").textContent);
  check("原始链接可打开",
    doc.getElementById("lightbox-open").getAttribute("href") === "https://example.com/original.png",
    doc.getElementById("lightbox-open").getAttribute("href"));
  check("复制链接按钮可见", doc.getElementById("btn-copy-image-link").hidden === false);

  doc.getElementById("lightbox").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 50));
  check("点遮罩关闭预览", doc.getElementById("lightbox").hidden === true);
}

// ── 发送失败：保留气泡并可重试 ──────────────────────────────
{
  const input = doc.getElementById("input");
  input.value = "这条会失败";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  doc.getElementById("composer")
    .dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 200));

  check("发送失败留下可重试气泡", doc.querySelectorAll(".message-row.failed").length === 1,
    doc.querySelectorAll(".message-row.failed").length);
  const retryButton = [...doc.querySelectorAll(".message-row.failed .message-action")]
    .find((el) => el.textContent === "重试");
  check("失败气泡带重试按钮", Boolean(retryButton));
  retryButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  check("重试成功后失败气泡消失", doc.querySelectorAll(".message-row.failed").length === 0,
    doc.querySelectorAll(".message-row.failed").length);
}

// ── 虚拟滚动：长会话只渲染窗口内的节点 ──────────────────────
{
  const sessionId = "aiocqhttp:FriendMessage:9";
  for (let index = 0; index < 120; index += 1) {
    window.__sseHandlers.onMessage({ eventType: "message", parsed: {
      id: 1000 + index, session_id: sessionId, type: "received",
      sender: "灌水", sender_id: "u10", sender_name: "灌水", content: `第 ${index} 条`,
      segments: [{ type: "text", text: `第 ${index} 条` }], timestamp: 1700010000 + index } });
  }
  await new Promise((r) => setTimeout(r, 400));

  const rendered = doc.querySelectorAll("#message-items .message-row").length;
  check("长会话启用虚拟滚动（只渲染窗口内节点）", rendered > 0 && rendered < 120,
    `渲染 ${rendered} 条`);
  check("虚拟滚动上下有 spacer 占位", Boolean(doc.getElementById("virtual-top")) &&
    Boolean(doc.getElementById("virtual-bottom")));
  // 贴底时窗口钉在尾部，未渲染的历史由顶部 spacer 撑住
  const topPad = parseFloat(doc.getElementById("virtual-top").style.height || "0");
  check("spacer 撑出未渲染消息的滚动高度", topPad > 0, String(topPad));

  // 图片占位点击后，窗口重渲染不能把已加载的图片又变回占位
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 2100, session_id: sessionId, type: "received", sender: "图图",
    sender_id: "u11", sender_name: "图图", content: "",
    segments: [{ type: "image", url: "https://example.com/long.png" }],
    timestamp: 1700020000 } });
  await new Promise((r) => setTimeout(r, 150));
  const imageHolder = doc.querySelector('[data-message-id="2100"] .message-image-placeholder');
  check("长会话里图片仍显示占位", Boolean(imageHolder));
  imageHolder?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  check("点击占位后加载这张图",
    doc.querySelectorAll('[data-message-id="2100"] .message-image').length === 1,
    doc.querySelectorAll('[data-message-id="2100"] .message-image').length);

  // 再进一条消息 → 虚拟窗口重渲染，已加载的图片不能被打回占位
  window.__sseHandlers.onMessage({ eventType: "message", parsed: {
    id: 2101, session_id: sessionId, type: "received", sender: "灌水",
    sender_id: "u10", sender_name: "灌水", content: "又一条",
    segments: [{ type: "text", text: "又一条" }], timestamp: 1700020010 } });
  await new Promise((r) => setTimeout(r, 200));
  check("窗口重渲染后图片保持加载",
    doc.querySelectorAll('[data-message-id="2100"] .message-image').length === 1,
    doc.querySelectorAll('[data-message-id="2100"] .message-image').length);

  // 往上滚动后窗口要跟着移动，不能一直钉在尾部
  const list = doc.getElementById("messages");
  list.scrollTop = 3000;
  list.dispatchEvent(new window.Event("scroll"));
  await new Promise((r) => setTimeout(r, 120));
  const topAfter = parseFloat(doc.getElementById("virtual-top").style.height || "0");
  check("滚动后虚拟窗口上移", topAfter !== topPad, `${topPad} -> ${topAfter}`);
}

fs.unlinkSync(tmp);

const failed = results.filter((r) => r[0] === "FAIL");
for (const [status, name, extra] of results) {
  console.log(`${status === "PASS" ? "✔" : "✘"} ${name}${status === "FAIL" ? "  <= " + extra : ""}`);
}
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
