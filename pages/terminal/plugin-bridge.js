/**
 * plugin-bridge.js — 自带的 AstrBot 插件视图 bridge 客户端。
 *
 * 为什么要有这个东西：
 *   视图 iframe 是 `sandbox="allow-scripts allow-forms allow-downloads"`（没有
 *   `allow-same-origin`），文档是 opaque origin。AstrBot 注入的
 *   `/api/plugin/page/bridge-sdk.js` 只认 `?asset_token=` 这一种认证，而
 *   4.29.x 上这个组合实际会 401（登录 cookie 因为 SameSite=Strict 不会跟着
 *   `<script src>` 发出去，query token 那条路又没走到认证中间件里）。
 *   于是 `window.AstrBotPluginView` 永远不存在，页面只能干瞪眼。
 *
 * 换个法子：bridge SDK 本质就是一段 postMessage 转发器，协议很短。我们自己
 * 实现一份，作为**视图自己的静态资源**加载（跟着 path token 走，天然 200），
 * 直接和父页面（Dashboard 的 PluginViewPage）握手，完全绕开那条 401 的路由。
 *
 * 协议（与 AstrBot `dashboard/plugin_page_bridge.js` 一致，勿随意改）：
 *   子 → 父  {channel, kind:"ready"}
 *            {channel, kind:"request", requestId, action, ...}
 *              action = api:get {endpoint, params} | api:post {endpoint, body}
 *                       sse:subscribe {endpoint, params, subscriptionId}
 *                       sse:unsubscribe {subscriptionId}
 *   父 → 子  {channel, kind:"context", context}
 *            {channel, kind:"response", requestId, ok, data|error}
 *            {channel, kind:"sse_message", subscriptionId, data, eventType, lastEventId}
 *            {channel, kind:"sse_state", subscriptionId, state:"open"|"error"}
 *
 * ⚠️ endpoint 必须是插件内相对路径，**不能带 query**（父页面会校验并抛
 *    "Plugin bridge endpoint is invalid."）；query 走第三个参数 params。
 */

export const BRIDGE_CHANNEL = "astrbot-plugin-page";

/** 按 "a.b.c" 取 i18n 文案，与宿主 SDK 的 key 约定一致。 */
function getByPath(source, key) {
  if (!source || typeof source !== "object" || !key) return undefined;
  return String(key)
    .split(".")
    .reduce(
      (current, part) =>
        current && typeof current === "object" && part in current ? current[part] : undefined,
      source,
    );
}

/**
 * 建一个 bridge 客户端。
 * @param {Window} win 视图自己的 window（测试里传假 window）
 */
export function createPluginBridge(win = window) {
  const pending = new Map();
  const sseHandlers = new Map();
  const contextHandlers = new Set();
  let requestCounter = 0;
  let subscriptionCounter = 0;
  let context = null;
  let readyResolve = null;
  const readyPromise = new Promise((resolve) => {
    readyResolve = resolve;
  });

  // opaque origin（sandbox iframe）下 location.origin 可能不是可用目标，
  // postMessage 用 "*" 兜底；接收侧只认 window.parent 这个来源。
  const targetOrigin = (() => {
    const origin = win?.location?.origin;
    return typeof origin === "string" && origin && origin !== "null" ? origin : "*";
  })();

  function parentWindow() {
    const parent = win?.parent;
    return parent && parent !== win ? parent : null;
  }

  function send(payload) {
    const parent = parentWindow();
    if (!parent) return false;
    try {
      parent.postMessage({ channel: BRIDGE_CHANNEL, ...payload }, targetOrigin);
      return true;
    } catch {
      return false;
    }
  }

  function nextRequestId() {
    requestCounter += 1;
    return `webchat_req_${requestCounter}`;
  }

  function request(action, payload) {
    const requestId = nextRequestId();
    return new Promise((resolve, reject) => {
      if (!send({ kind: "request", requestId, action, ...payload })) {
        reject(new Error("没有可用的 AstrBot 父页面，无法转发请求"));
        return;
      }
      pending.set(requestId, { resolve, reject });
    });
  }

  /**
   * 只发不等回执。
   * 退订这类「发了就够」的指令不能用 request：父页面万一不回，
   * 调用方就永远挂着（切会话时尤其致命）。
   */
  function fire(action, payload) {
    return send({ kind: "request", requestId: nextRequestId(), action, ...payload });
  }

  function applyContext(next) {
    if (!next || typeof next !== "object") return;
    context = { ...(context || {}), ...next };
    if (typeof next.isDark === "boolean" && win?.document?.documentElement) {
      win.document.documentElement.setAttribute(
        "data-theme",
        next.isDark ? "dark" : "light",
      );
    }
    if (readyResolve) {
      readyResolve(context);
      readyResolve = null;
    }
    contextHandlers.forEach((handler) => {
      try {
        handler(context);
      } catch (error) {
        console.error("[WebChat] bridge context handler 失败:", error);
      }
    });
  }

  function onParentMessage(event) {
    // 只接受父页面的消息；opaque origin 下没法比 origin，就比 source
    if (!event || event.source !== win?.parent) return;
    const message = event.data;
    if (!message || message.channel !== BRIDGE_CHANNEL) return;

    if (message.kind === "context") {
      applyContext(message.context);
      return;
    }
    if (message.kind === "response") {
      const entry = pending.get(message.requestId);
      if (!entry) return;
      pending.delete(message.requestId);
      if (message.ok) entry.resolve(message.data);
      else entry.reject(new Error(message.error || "插件 bridge 请求失败"));
      return;
    }
    if (message.kind === "sse_message") {
      const handlers = sseHandlers.get(message.subscriptionId);
      handlers?.onMessage?.({
        raw: message.data,
        parsed: parseMaybeJson(message.data),
        eventType: message.eventType || "message",
        lastEventId: message.lastEventId ?? "",
      });
      return;
    }
    if (message.kind === "sse_state") {
      const handlers = sseHandlers.get(message.subscriptionId);
      if (message.state === "open") handlers?.onOpen?.();
      if (message.state === "error") handlers?.onError?.();
    }
  }

  win?.addEventListener?.("message", onParentMessage);
  // 主动报 ready：父页面收到后会回一条 context（即使它已经因为 iframe load 推过一次）
  send({ kind: "ready" });

  return {
    /** 标记这是自带客户端，诊断里要用 */
    __source: "webchat-local",
    ready: () => readyPromise,
    getContext: () => context,
    getLocale: () => context?.locale || "zh-CN",
    getI18n: () => context?.i18n || {},
    t(key, fallback = "") {
      const locales = [context?.locale, "zh-CN", "en-US"].filter(Boolean);
      for (const locale of locales) {
        const value = getByPath(context?.i18n?.[locale], key);
        if (value !== undefined && value !== null) {
          return typeof value === "string" ? value : String(value);
        }
      }
      return fallback;
    },
    onContext(handler) {
      if (typeof handler !== "function") return () => {};
      contextHandlers.add(handler);
      if (context) handler(context);
      return () => contextHandlers.delete(handler);
    },
    apiGet(endpoint, params) {
      return request("api:get", { endpoint, params: params || {} });
    },
    apiPost(endpoint, body) {
      return request("api:post", { endpoint, body: body || {} });
    },
    subscribeSSE(endpoint, handlers, params) {
      subscriptionCounter += 1;
      const subscriptionId = `webchat_sse_${subscriptionCounter}`;
      sseHandlers.set(subscriptionId, handlers || {});
      return request("sse:subscribe", {
        endpoint,
        params: params || {},
        subscriptionId,
      })
        .then(() => subscriptionId)
        .catch((error) => {
          sseHandlers.delete(subscriptionId);
          throw error;
        });
    },
    async unsubscribeSSE(subscriptionId) {
      sseHandlers.delete(subscriptionId);
      // 不等回执：关流失败也不该拖住前端，父页面在 iframe 卸载时也会自己收尾
      fire("sse:unsubscribe", { subscriptionId });
    },
    dispose() {
      win?.removeEventListener?.("message", onParentMessage);
      pending.clear();
      sseHandlers.clear();
      contextHandlers.clear();
    },
  };
}

function parseMaybeJson(value) {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}
