/**
 * plugin-bridge（自带 bridge 客户端）的协议测试：node --test
 *
 * 它替代的是 AstrBot 注入的 `/api/plugin/page/bridge-sdk.js` —— 那条路在
 * sandbox iframe 里必然 401。协议很短但必须和 Dashboard 的
 * `dashboard/plugin_page_bridge.js` 严格一致，这里用假父窗口逐条锁住：
 * 通道名、ready 握手、request/response 配对、SSE 订阅与 params 传递。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { BRIDGE_CHANNEL, createPluginBridge } from "../../pages/terminal/plugin-bridge.js";

/** 造一个假 window：记录发往父页面的消息，并能把父页面的消息灌回来。 */
function createFakeWindow({ parented = true } = {}) {
  const listeners = [];
  const sent = [];
  const attrs = {};
  const win = {
    location: { origin: "https://dash.example" },
    sent,
    document: {
      documentElement: {
        setAttribute: (key, value) => {
          attrs[key] = value;
        },
      },
    },
    addEventListener: (type, handler) => {
      if (type === "message") listeners.push(handler);
    },
    removeEventListener: (type, handler) => {
      const index = listeners.indexOf(handler);
      if (index >= 0) listeners.splice(index, 1);
    },
  };
  const parent = {
    postMessage(message, targetOrigin) {
      sent.push({ message, targetOrigin });
    },
  };
  win.parent = parented ? parent : win;
  win.__attrs = attrs;
  win.__listenerCount = () => listeners.length;
  /** 模拟父页面 → 子页面的消息 */
  win.__emit = (data) => {
    for (const handler of listeners.slice()) {
      handler({ data, source: parent, origin: "https://dash.example" });
    }
  };
  /** 取最后一条发往父页面的消息 */
  win.__last = () => sent.at(-1)?.message;
  return win;
}

const contextMessage = (context) => ({ channel: BRIDGE_CHANNEL, kind: "context", context });

test("建好就向父页面报 ready，通道名固定", () => {
  const win = createFakeWindow();
  createPluginBridge(win);
  assert.equal(win.sent.length, 1);
  assert.equal(win.__last().kind, "ready");
  assert.equal(win.__last().channel, "astrbot-plugin-page");
  assert.equal(win.sent[0].targetOrigin, "https://dash.example");
});

test("opaque origin（sandbox iframe）下目标 origin 退化为 *", () => {
  const win = createFakeWindow();
  win.location.origin = "null";
  createPluginBridge(win);
  assert.equal(win.sent[0].targetOrigin, "*");
});

test("收到 context 后 ready() 兑现，并同步 data-theme", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  win.__emit(contextMessage({ isDark: true, locale: "zh-CN" }));
  const context = await bridge.ready();
  assert.equal(context.isDark, true);
  assert.equal(bridge.getLocale(), "zh-CN");
  assert.equal(win.__attrs["data-theme"], "dark");
});

test("apiGet 发 api:get 请求并用 response 兑现", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  const pending = bridge.apiGet("sessions", { limit: "50" });
  const request = win.__last();
  assert.equal(request.kind, "request");
  assert.equal(request.action, "api:get");
  assert.equal(request.endpoint, "sessions");
  assert.deepEqual(request.params, { limit: "50" });

  win.__emit({
    channel: BRIDGE_CHANNEL,
    kind: "response",
    requestId: request.requestId,
    ok: true,
    data: { sessions: [] },
  });
  assert.deepEqual(await pending, { sessions: [] });
});

test("apiPost 失败时把父页面的错误变成 rejection", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  const pending = bridge.apiPost("send", { message: "hi" });
  const request = win.__last();
  assert.equal(request.action, "api:post");
  assert.deepEqual(request.body, { message: "hi" });

  win.__emit({
    channel: BRIDGE_CHANNEL,
    kind: "response",
    requestId: request.requestId,
    ok: false,
    error: "会话不存在六",
  });
  await assert.rejects(pending, /会话不存在六/);
});

test("subscribeSSE 把 topic 放进 params，而不是拼进 endpoint", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  const opened = [];
  const messages = [];

  const subscription = bridge.subscribeSSE(
    "stream",
    {
      onOpen: () => opened.push("open"),
      onMessage: (event) => messages.push(event),
      onError: () => opened.push("error"),
    },
    { session_id: "group:111" },
  );

  const request = win.__last();
  assert.equal(request.action, "sse:subscribe");
  assert.equal(request.endpoint, "stream");
  assert.deepEqual(request.params, { session_id: "group:111" });

  win.__emit({
    channel: BRIDGE_CHANNEL,
    kind: "response",
    requestId: request.requestId,
    ok: true,
    data: { subscriptionId: request.subscriptionId },
  });
  const id = await subscription;
  assert.equal(id, request.subscriptionId);

  win.__emit({ channel: BRIDGE_CHANNEL, kind: "sse_state", subscriptionId: id, state: "open" });
  win.__emit({
    channel: BRIDGE_CHANNEL,
    kind: "sse_message",
    subscriptionId: id,
    data: JSON.stringify({ event: "message", data: "hi" }),
    eventType: "message",
    lastEventId: "7",
  });

  assert.deepEqual(opened, ["open"]);
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].parsed, { event: "message", data: "hi" });
  assert.equal(messages[0].eventType, "message");
  assert.equal(messages[0].lastEventId, "7");
});

test("unsubscribeSSE 发完就走，不等父页面回执，也不再派发消息", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  const messages = [];
  const subscription = bridge.subscribeSSE("stream", { onMessage: (e) => messages.push(e) }, {});
  const request = win.__last();
  win.__emit({
    channel: BRIDGE_CHANNEL,
    kind: "response",
    requestId: request.requestId,
    ok: true,
    data: {},
  });
  const id = await subscription;

  // 父页面不会回这条；卡住就说明退订在等一个永远不来的 ack
  await bridge.unsubscribeSSE(id);
  const unsubscribe = win.__last();
  assert.equal(unsubscribe.action, "sse:unsubscribe");
  assert.equal(unsubscribe.subscriptionId, id);

  win.__emit({ channel: BRIDGE_CHANNEL, kind: "sse_message", subscriptionId: id, data: "x" });
  assert.equal(messages.length, 0);
});

test("没有父页面时请求直接 reject，而不是永远挂着", async () => {
  const win = createFakeWindow({ parented: false });
  const bridge = createPluginBridge(win);
  await assert.rejects(bridge.apiGet("sessions"), /没有可用的 AstrBot 父页面/);
});

test("陌生来源 / 陌生通道的消息一律忽略", async () => {
  const win = createFakeWindow();
  const bridge = createPluginBridge(win);
  let resolved = false;
  bridge.ready().then(() => {
    resolved = true;
  });
  win.__emit({ channel: "other-channel", kind: "context", context: { isDark: true } });
  win.__emit(contextMessage({ locale: "en-US" }));
  await Promise.resolve();
  assert.equal(resolved, true);
  assert.equal(bridge.getLocale(), "en-US");
});
