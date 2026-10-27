/**
 * app.js — WebChat 终端页面控制器。
 *
 * 约定：
 *   - 纯数据变换全部在 webchat-lib.js，本文件只负责 DOM、状态和 bridge 调用
 *   - SSE 优先走 bridge.subscribeSSE，bridge 不可用时退回原生 EventSource
 *   - iframe 沙箱没有 allow-modals / 同源 localStorage，
 *     所以弹窗自绘，会话记忆优先用 bridge 上下文并回退 localStorage
 */

import {
  HISTORY_PAGE_SIZE,
  NEAR_BOTTOM_PX,
  anchorScrollTop,
  bridgeSdkUrl,
  compactId,
  compareSessions,
  computeWindow,
  dedupeMessages,
  errorText,
  formatFullTime,
  formatMessageTime,
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
  splitHighlight,
  stripMentionMarkup,
  unreadLabel,
  windowAnchor,
} from "./webchat-lib.js";

const PLUGIN_NAME = "astrbot_plugin_webchat";
const STORAGE_KEY = `${PLUGIN_NAME}_last_session`;
const THEME_KEY = `${PLUGIN_NAME}_theme`;
const COMPACT_KEY = `${PLUGIN_NAME}_compact`;
const IMAGES_KEY = `${PLUGIN_NAME}_load_images`;
const POLL_INTERVAL_MS = 2500;
/** 轮询一次最多带回多少条新消息（与后端 MAX_PAGE_SIZE 对齐）。 */
const POLL_MESSAGE_LIMIT = 200;
/** 一次轮询取满时最多连续补几次，避免消息洪峰时无限递归。 */
const POLL_MAX_DRAIN = 5;
const HISTORY_LIMIT = HISTORY_PAGE_SIZE;
/** SSE 心跳：后端每 15 秒发一个 ping，超过 45 秒没收到就认为连接已死。 */
const STREAM_STALE_MS = 45000;
const STREAM_CHECK_MS = 20000;
/** 轮询降级期间每隔多久再试一次 SSE：尽量回到实时推送，而不是永久轮询。 */
const SSE_RETRY_MS = 60000;
/** bridge 订阅迟迟不返回就当失败：宿主缓冲 SSE 时 fetch 永远等不到响应头。 */
const STREAM_OPEN_TIMEOUT_MS = 8000;
/** 宿主没注入 bridge SDK 时，自己补一次的最长等待。 */
const BRIDGE_SDK_LOAD_TIMEOUT_MS = 5000;
/** bridge 已存在但宿主迟迟不回上下文 / 不响应请求，多半是握手层坏了。 */
const BRIDGE_HANDSHAKE_TIMEOUT_MS = 8000;
/** 握手超时的报错文案，boot() 靠它区分「bridge 死了」和「后端端点本身报错」。 */
const BRIDGE_HANDSHAKE_ERROR = "bridge 握手超时";
/** 小窗自动紧凑阈值：必须与 style.css 顶部的 --compact 保持一致。 */
const COMPACT_MEDIA = "(max-width: 900px), (max-height: 720px)";
/** 超过这个条数就启用虚拟滚动；小会话保持全量渲染，行为与旧版一致。 */
const VIRTUAL_THRESHOLD = 80;
/** 虚拟窗口最少渲染多少条（也决定贴底时的窗口大小）。 */
const VIRTUAL_MIN_RENDER = 30;
/** 视口上下额外多渲染多少像素，滚动时不至于露白。 */
const VIRTUAL_OVERSCAN = 800;
/** 没测量过的消息用这个高度估算。 */
const MESSAGE_HEIGHT_FALLBACK = 56;
/** 距离顶部多少像素内就自动补一页更早消息。 */
const AUTO_LOAD_TOP_PX = 120;
/** 同一段「贴顶」只自动补一页；离开顶部（或切会话）后重新武装。 */
let autoLoadedAtTop = false;
/** 会话列表每页条数。 */
const SESSION_PAGE_SIZE = 50;
const DRAFT_KEY = `${PLUGIN_NAME}_draft`;

/**
 * bridge 由 AstrBot 返回视图 HTML 时注入的 bridge-sdk.js 提供：
 * 新版叫 window.AstrBotPluginView，旧版叫 window.AstrBotPluginPage。
 * 注入失败时这里是 null，boot() 会尝试自己补加载一次。
 */
let bridge = pickBridge();
/** 已经补加载过的 SDK 地址，失败后不重复插脚本。 */
const attemptedBridgeSdks = new Set();
/** 上一次补加载的 SDK 地址，失败时写进报错里，省得用户去猜。 */
let lastBridgeSdkAttempt = "";
const elements = {
  btnCompact: document.getElementById("btn-compact"),
  btnTheme: document.getElementById("btn-theme"),
  btnImages: document.getElementById("btn-images"),
  messages: document.getElementById("messages"),
  messageContent: document.getElementById("message-content"),
  messageItems: document.getElementById("message-items"),
  virtualTop: document.getElementById("virtual-top"),
  virtualBottom: document.getElementById("virtual-bottom"),
  replyPreview: document.getElementById("reply-preview"),
  replyPreviewName: document.getElementById("reply-preview-name"),
  replyPreviewText: document.getElementById("reply-preview-text"),
  btnReplyCancel: document.getElementById("btn-reply-cancel"),
  sessionMore: document.getElementById("session-more"),
  emptyState: document.getElementById("empty-state"),
  loadMore: document.getElementById("load-more"),
  input: document.getElementById("input"),
  composer: document.getElementById("composer"),
  btnSend: document.getElementById("btn-send"),
  btnClear: document.getElementById("btn-clear"),
  btnRefresh: document.getElementById("btn-refresh"),
  btnMobileMenu: document.getElementById("btn-mobile-menu"),
  btnDetails: document.getElementById("btn-details"),
  btnDetailsClose: document.getElementById("btn-details-close"),
  sessionList: document.getElementById("session-list"),
  sessionCount: document.getElementById("session-count"),
  sessionSearch: document.getElementById("session-search"),
  sidebarBackdrop: document.getElementById("sidebar-backdrop"),
  sidebarEmpty: document.getElementById("sidebar-empty"),
  status: document.getElementById("status"),
  statusText: document.getElementById("status-text"),
  conversationAvatar: document.getElementById("conversation-avatar"),
  conversationTitle: document.getElementById("conversation-title"),
  conversationKind: document.getElementById("conversation-kind"),
  conversationId: document.getElementById("conversation-id"),
  conversationPlatform: document.getElementById("conversation-platform"),
  detailsPanel: document.getElementById("details-panel"),
  detailsTitle: document.getElementById("details-title"),
  detailKind: document.getElementById("detail-kind"),
  detailId: document.getElementById("detail-id"),
  detailPlatform: document.getElementById("detail-platform"),
  detailCount: document.getElementById("detail-count"),
  participantList: document.getElementById("participant-list"),
  participantCount: document.getElementById("participant-count"),
  newMessagePill: document.getElementById("new-message-pill"),
  newMessageText: document.getElementById("new-message-text"),
  toastRegion: document.getElementById("toast-region"),
  dialogRegion: document.getElementById("dialog-region"),
  messageSearch: document.getElementById("message-search"),
  searchResults: document.getElementById("search-results"),
  searchCount: document.getElementById("search-count"),
  searchStatus: document.getElementById("search-status"),
  btnDiscover: document.getElementById("btn-discover"),
  btnOpenSession: document.getElementById("btn-open-session"),
  btnPin: document.getElementById("btn-pin"),
  btnExport: document.getElementById("btn-export"),
  btnAttach: document.getElementById("btn-attach"),
  attachmentInput: document.getElementById("attachment-input"),
  attachmentPreview: document.getElementById("attachment-preview"),
  lightbox: document.getElementById("lightbox"),
  lightboxImage: document.getElementById("lightbox-image"),
  lightboxCaption: document.getElementById("lightbox-caption"),
  lightboxLink: document.getElementById("lightbox-link"),
  lightboxOpen: document.getElementById("lightbox-open"),
  btnCopyImageLink: document.getElementById("btn-copy-image-link"),
};

const state = {
  sessions: new Map(),
  messages: new Map(),
  history: new Map(),
  unread: new Map(),
  currentSessionId: "",
  stream: null,
  streamGeneration: 0,
  streamStarted: false,
  /** 本轮重连要补的全局消息游标；首次连接为 null（历史刚由 loadSessions 载入）。 */
  streamCatchupAfter: null,
  /** 后台摘要已计数过的消息键（`session:messageId`），全量到达后转正、撤回时清理。 */
  summaryIds: new Set(),
  pollTimer: null,
  polling: false,
  connected: false,
  /** 最近一次收到任何 SSE 帧（含 ping）的时间，用于判断连接是否假死。 */
  lastStreamAt: 0,
  /** 轮询降级时最近一次重试 SSE 的时间，避免每次健康检查都重订阅。 */
  lastSseRetryAt: 0,
  /** 轮询游标：已确认收到的最大消息 id（全局自增，跨会话）。 */
  updatesCursor: 0,
  loadingHistory: false,
  sending: false,
  detailsOpen: false,
  newMessageCount: 0,
  lastNewMessageSender: "",
  searchQuery: "",
  searchResults: [],
  attachments: [],
  context: null,
  renderPlan: [],
  renderSessionId: "",
  /** 虚拟滚动：是否启用、当前窗口范围、按 plan key 缓存的行高。 */
  virtual: false,
  virtualWindow: { start: 0, end: 0 },
  heights: new Map(),
  /** 引用目标：{ sessionId, target(平台消息 id), senderName, text }。 */
  replyTo: null,
  /** 草稿：会话 id → 未发送文本（localStorage 失败时的内存兜底）。 */
  drafts: new Map(),
  /** 会话列表分页游标。 */
  sessionPage: { offset: 0, hasMore: false, loading: false },
  /** 是否自动请求图片；默认关，可由插件配置或工具栏开关覆盖。 */
  loadImages: false,
  /** 用户手动点开过的图片地址：窗口重渲染后仍保持已加载，不能打回占位。 */
  loadedImages: new Set(),
  /** 大图预览当前展示的图片原始地址（复制链接用）。 */
  lightboxSource: "",
};

bindEvents();
syncThemeControl();
syncCompactControl();
applyLoadImages(readInitialLoadImages(), { persist: false });
syncImagesControl();
boot();

/**
 * 图片开关的初值：本地记忆优先，其次插件配置（默认关）。
 * 配置要等 boot 里的 health 才有，所以先按本地/默认值渲染，拿到配置再修正。
 */
function readInitialLoadImages() {
  const stored = readStoredLoadImages();
  return stored === null ? false : stored;
}

// ── bridge 获取、自愈与诊断 ─────────────────────────────────
// 视图跑在受限 iframe 里，所有后端能力都要经 window.AstrBotPluginView 转发。
// 宿主正常会往 HTML 里注入 bridge-sdk.js；但注入可能没发生（页面被直接打开）
// 或者请求失败（反向代理子路径下绝对路径 404、Dashboard 登录失效 401）。
// 这几种情况以前只丢一句「未检测到 Bridge」，用户完全无从下手。

function pickBridge() {
  return window.AstrBotPluginView || window.AstrBotPluginPage || null;
}

function isUsableBridge(candidate) {
  return !!candidate && typeof candidate.apiGet === "function";
}

/** 上下文变化（语言 / 主题）只在 bridge 就绪后才能订阅，所以不放在 bindEvents 里。 */
let bridgeContextBound = false;
function bindBridgeContext() {
  if (bridgeContextBound || typeof bridge?.onContext !== "function") return;
  bridgeContextBound = true;
  bridge.onContext((context) => {
    state.context = context;
    syncThemeFromBridge();
  });
}

/**
 * 拿到可用的 bridge：优先宿主注入的那个，
 * 没有就按当前页面路径自己补加载一次（兼容子路径部署）。
 */
async function ensureBridge() {
  const injected = pickBridge();
  if (isUsableBridge(injected)) return injected;

  await loadBridgeSdkScript(bridgeSdkUrl(window.location.pathname));
  const loaded = pickBridge();
  return isUsableBridge(loaded) ? loaded : null;
}

/** 动态插一个 bridge-sdk.js；无论成败都 resolve，是否可用由调用方复查全局。 */
async function loadBridgeSdkScript(url) {
  lastBridgeSdkAttempt = url;
  // file:// 下没有同源后端，补加载只会制造一条无意义的报错
  if (!url || window.location.protocol === "file:" || attemptedBridgeSdks.has(url)) {
    return false;
  }
  attemptedBridgeSdks.add(url);

  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = url;
    script.async = true;
    script.dataset.webchatBridgeSdk = "1";

    let timer = 0;
    const finish = (ok) => {
      window.clearTimeout(timer);
      resolve(ok);
    };
    timer = window.setTimeout(() => finish(false), BRIDGE_SDK_LOAD_TIMEOUT_MS);
    script.addEventListener("load", () => finish(true));
    script.addEventListener("error", () => finish(false));
    document.head.append(script);
  });
}

/** 等宿主回上下文；超时不算致命错误，后面用 health 探针确认真实可用性。 */
async function readBridgeContext(bridgeRef) {
  if (typeof bridgeRef?.ready !== "function") return null;
  try {
    return await withTimeout(
      Promise.resolve(bridgeRef.ready()),
      BRIDGE_HANDSHAKE_TIMEOUT_MS,
    );
  } catch {
    return null;
  }
}

/**
 * bridge 存在但宿主不回话时，后面的每个 apiGet 都会永久挂起，
 * 界面就永远停在「正在连接」。这里先探一次 health 把死握手变成可读的报错。
 *
 * 注意：只有**超时**才算握手失败。宿主回了错误（比如某个后端端点抛异常）
 * 反而证明 bridge 是通的，不该把整页判死。
 */
async function probeBridgeHandshake(bridgeRef) {
  let timer = 0;
  const timeout = new Promise((_resolve, reject) => {
    timer = window.setTimeout(
      () => reject(new Error(BRIDGE_HANDSHAKE_ERROR)),
      BRIDGE_HANDSHAKE_TIMEOUT_MS,
    );
  });
  try {
    await Promise.race([Promise.resolve(bridgeRef.apiGet("health")), timeout]);
  } catch (error) {
    if (error instanceof Error && error.message === BRIDGE_HANDSHAKE_ERROR) throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function failBridgeMissing() {
  const inFrame = window.top !== window;
  // 只认宿主自己插的那个（data-webchat-bridge-sdk 是我们补加载时打的标记）
  const injected = [...document.querySelectorAll('script[src*="bridge-sdk.js"]')].find(
    (node) => !node.dataset.webchatBridgeSdk,
  );
  console.error("[WebChat] 未检测到 AstrBot 页面 Bridge", {
    inFrame,
    href: window.location.href,
    injected: injected ? injected.src : "宿主没有注入 bridge-sdk.js",
    attempted: lastBridgeSdkAttempt || "（未尝试补加载）",
  });

  setStatus("Bridge 不可用", "error");
  toast("未检测到 AstrBot 页面 Bridge", "error");
  toast(
    inFrame
      ? "bridge-sdk.js 没能加载：打开开发者工具看 Network 里 bridge-sdk.js 的状态码——401 是 Dashboard 登录失效，404 多半是 WebUI 挂在反向代理子路径下"
      : "本页要在 AstrBot WebUI 的插件详情页里打开；直接访问 URL（新标签页）不会注入 bridge",
    "error",
    12000,
  );
}

function failBridgeHandshake(error) {
  const inFrame = window.top !== window;
  console.error("[WebChat] bridge 已加载，但宿主没有响应握手", {
    inFrame,
    href: window.location.href,
    attempted: lastBridgeSdkAttempt || "（未尝试补加载）",
    error: errorText(error),
  });
  setStatus("Bridge 无响应", "error");
  toast(
    inFrame
      ? "AstrBot bridge 已注入但没有响应，刷新页面重试；仍然不行请重载插件"
      : "直接访问视图 URL 时没有 Dashboard 父页面接管 bridge；请从插件详情页进入",
    "error",
    8000,
  );
}

async function boot() {
  setStatus("正在连接", "loading");
  const usableBridge = await ensureBridge();
  if (!usableBridge) {
    failBridgeMissing();
    return;
  }
  bridge = usableBridge;

  try {
    // 两件事并行等：拿上下文 + 探一次握手。直接打开视图 URL 时 bridge 会跟
    // 自己 postMessage，请求永远不回来——没有这个探针，界面会无声地卡在「正在连接」。
    const [context] = await Promise.all([
      readBridgeContext(bridge),
      probeBridgeHandshake(bridge),
    ]);
    state.context = context;
    bindBridgeContext();
    syncThemeFromBridge();
    await syncImagePreferenceFromConfig();
    await loadSessions({ selectInitial: true });
    connectStream();
    window.setInterval(checkStreamHealth, STREAM_CHECK_MS);
  } catch (error) {
    if (error instanceof Error && error.message === BRIDGE_HANDSHAKE_ERROR) {
      failBridgeHandshake(error);
      return;
    }
    console.error("初始化失败:", error);
    setStatus("加载失败", "error");
    toast(`初始化失败: ${errorText(error)}`, "error");
  }
}

/** 插件配置里的 load_images 作为默认值；用户手动切过就以本地记忆为准。 */
async function syncImagePreferenceFromConfig() {
  if (readStoredLoadImages() !== null) return;
  try {
    const health = await bridge.apiGet("health");
    if (health && typeof health.load_images === "boolean") {
      applyLoadImages(health.load_images, { persist: false });
      syncImagesControl();
    }
  } catch {
    // health 拿不到就保持默认关，不影响终端使用
  }
}

// ── 事件绑定 ────────────────────────────────────────────────

function bindEvents() {
  elements.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });

  elements.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  elements.input.addEventListener("input", () => {
    resizeComposer();
    updateSendState();
    scheduleDraftSave();
  });

  elements.btnClear.addEventListener("click", clearMessages);
  elements.btnCompact.addEventListener("click", toggleCompactMode);
  elements.btnTheme.addEventListener("click", toggleTheme);
  elements.btnImages?.addEventListener("click", toggleLoadImages);
  elements.btnRefresh.addEventListener("click", () => loadSessions({ selectInitial: false }));
  elements.btnMobileMenu.addEventListener("click", () => toggleSidebar(true));
  elements.sidebarBackdrop.addEventListener("click", () => toggleSidebar(false));
  elements.sessionSearch.addEventListener("input", renderSessionList);
  elements.btnDetails.addEventListener("click", () => toggleDetails(true));
  elements.btnDetailsClose.addEventListener("click", () => toggleDetails(false));
  elements.newMessagePill.addEventListener("click", () => {
    scrollToBottom(true);
    markCurrentSessionRead();
  });
  elements.loadMore.addEventListener("click", loadOlderMessages);
  elements.btnReplyCancel.addEventListener("click", cancelReply);
  elements.sessionMore.addEventListener("click", loadMoreSessions);
  elements.sessionList.addEventListener("scroll", handleSessionListScroll, { passive: true });
  elements.messages.addEventListener("scroll", handleMessageScroll, { passive: true });

  elements.messageSearch.addEventListener("input", debounce(onSearchInput, 260));
  elements.btnDiscover.addEventListener("click", runDiscovery);
  elements.btnOpenSession.addEventListener("click", openNewSessionDialog);
  elements.btnPin.addEventListener("click", togglePin);
  elements.btnExport.addEventListener("click", exportSession);
  elements.btnAttach.addEventListener("click", () => elements.attachmentInput.click());
  elements.attachmentInput.addEventListener("change", onAttachmentPicked);
  elements.lightbox.addEventListener("click", closeLightbox);
  // 点在图/说明条上不该关掉预览
  elements.lightbox.querySelector(".lightbox-figure")?.addEventListener("click", (event) => {
    event.stopPropagation();
  });
  elements.btnCopyImageLink.addEventListener("click", () => {
    if (!state.lightboxSource) return;
    copyText(state.lightboxSource);
  });

  // 输入框内粘贴图片直接变成待发送附件
  elements.input.addEventListener("paste", onInputPaste);

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      toggleSidebar(false);
      toggleDetails(false);
      closeLightbox();
      closeTopDialog();
      cancelReply();
    }
  });

  window.addEventListener("beforeunload", disposeStream);

  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (event) => {
    if (!readStoredTheme()) applyTheme(event.matches ? "light" : "dark", { persist: false });
  });

  window.matchMedia(COMPACT_MEDIA).addEventListener("change", (event) => {
    if (readStoredCompact() === null) applyCompactMode(event.matches, { persist: false });
  });

  // onContext 的注册要等 bridge 就绪，见 bindBridgeContext()
}

// ── 会话加载 ────────────────────────────────────────────────

async function loadSessions({ selectInitial = false, reset = true } = {}) {
  elements.btnRefresh.disabled = true;
  const offset = reset ? 0 : state.sessionPage.offset;
  try {
    const data = await bridge.apiGet("sessions", {
      with_history: "1",
      history_limit: String(HISTORY_LIMIT),
      limit: String(SESSION_PAGE_SIZE),
      offset: String(offset),
    });
    const sessions = Array.isArray(data?.sessions) ? data.sessions : [];

    if (reset) state.sessions = new Map();

    for (const rawSession of sessions) {
      const session = normalizeSession(rawSession);
      if (!session.id) continue;
      state.sessions.set(session.id, session);

      // 首次加载填充历史；已有缓存时保留用户已经翻出来的更早记录
      if (Array.isArray(rawSession.messages) && !state.messages.has(session.id)) {
        state.messages.set(
          session.id,
          dedupeMessages(rawSession.messages.map((message) => normalizeMessage(message, session.id))),
        );
      } else if (!state.messages.has(session.id)) {
        state.messages.set(session.id, []);
      }

      const pageHistory = normalizeHistory(rawSession.history);
      const cachedHistory = state.history.get(session.id);
      if (!cachedHistory || !cachedHistory.hasMore || pageHistory.hasMore) {
        state.history.set(session.id, pageHistory);
      }

      if (!state.unread.has(session.id)) state.unread.set(session.id, 0);
    }

    state.sessionPage = {
      offset: offset + sessions.length,
      hasMore: Boolean(data?.has_more),
      loading: false,
    };

    // 首页才清理已删除会话的缓存；翻页只是追加，不能删别人
    if (reset) {
      for (const sessionId of [...state.messages.keys()]) {
        if (!state.sessions.has(sessionId)) {
          state.messages.delete(sessionId);
          state.history.delete(sessionId);
          state.unread.delete(sessionId);
        }
      }
    }

    renderSessionList();

    if (!reset) return;

    const savedSession = readStoredSession();
    const currentStillExists = state.currentSessionId && state.sessions.has(state.currentSessionId);
    const target = currentStillExists
      ? state.currentSessionId
      : savedSession && state.sessions.has(savedSession)
        ? savedSession
        : (sortedSessions()[0]?.id ?? "");

    if (target) {
      const stickToBottom = selectInitial || !currentStillExists || isNearBottomOfMessageList();
      selectSession(target, { scrollToBottom: stickToBottom, closeSidebar: false });
    } else {
      resetConversation();
    }

    setStatus(state.connected ? "实时连接" : "已连接", "connected");
  } catch (error) {
    console.error("加载会话失败:", error);
    setStatus("加载失败", "error");
    toast(`加载会话失败: ${errorText(error)}`, "error");
  } finally {
    elements.btnRefresh.disabled = false;
  }
}

async function loadMoreSessions() {
  if (state.sessionPage.loading || !state.sessionPage.hasMore) return;
  state.sessionPage.loading = true;
  elements.sessionMore.disabled = true;
  elements.sessionMore.textContent = "加载中…";
  try {
    await loadSessions({ reset: false });
  } finally {
    state.sessionPage.loading = false;
    elements.sessionMore.disabled = false;
    elements.sessionMore.textContent = "加载更多会话";
    renderSessionList();
  }
}

/** 会话列表滚到底部时自动补一页。 */
function handleSessionListScroll() {
  const list = elements.sessionList;
  if (!state.sessionPage.hasMore || state.sessionPage.loading) return;
  if (list.scrollTop + list.clientHeight < list.scrollHeight - 120) return;
  loadMoreSessions();
}

function sortedSessions() {
  return [...state.sessions.values()].sort(compareSessions);
}

// ── 会话列表 ────────────────────────────────────────────────

function renderSessionList() {
  const query = elements.sessionSearch.value;
  const sessions = sortedSessions().filter((session) => matchesSession(session, query));

  elements.sessionList.replaceChildren();
  const fragment = document.createDocumentFragment();

  for (const session of sessions) {
    const button = document.createElement("button");
    const active = session.id === state.currentSessionId;
    button.type = "button";
    button.className = `session-item${active ? " active" : ""}${session.pinned ? " pinned" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.dataset.sessionId = session.id;
    button.addEventListener("click", () => selectSession(session.id));

    const avatar = createElement(
      "span",
      `avatar ${session.isGroup ? "avatar-group" : "avatar-private"}`,
      getInitials(session.peerName),
    );
    avatar.title = session.peerName;

    const copy = document.createElement("span");
    copy.className = "session-copy";
    const nameRow = document.createElement("span");
    nameRow.className = "session-name-row";
    const dot = createElement("span", `type-dot${session.isGroup ? "" : " private"}`);
    nameRow.append(dot, createElement("span", "session-name", session.peerName));
    if (session.pinned) nameRow.append(createElement("span", "pin-mark", "置顶"));

    const previewText = getSessionPreview(session);
    const preview = createElement("span", "session-preview", previewText);
    preview.title = previewText;
    copy.append(nameRow, preview);

    const side = document.createElement("span");
    side.className = "session-side";
    const lastTimestamp = session.lastMessage?.timestamp || session.lastActivity || 0;
    side.append(
      createElement("span", "session-time", lastTimestamp ? formatSessionTime(lastTimestamp) : ""),
    );
    const unread = unreadLabel(state.unread.get(session.id));
    if (unread) side.append(createElement("span", "unread-badge", unread));

    button.append(avatar, copy, side);
    fragment.append(button);
  }

  elements.sessionList.append(fragment);
  elements.sessionList.hidden = sessions.length === 0;
  elements.sidebarEmpty.hidden = sessions.length !== 0;

  const total = state.sessions.size;
  elements.sessionCount.textContent =
    sessions.length === total ? String(total) : `${sessions.length}/${total}`;

  // 有关键词时只在已加载的会话里筛，不假装能搜到还没拉下来的页
  elements.sessionMore.hidden = !state.sessionPage.hasMore || Boolean(query.trim()) || !sessions.length;
  elements.sessionMore.disabled = state.sessionPage.loading;
  elements.sessionMore.textContent = state.sessionPage.loading ? "加载中…" : "加载更多会话";
}

// ── 会话切换 ────────────────────────────────────────────────

function selectSession(sessionId, { scrollToBottom: shouldScroll = true, closeSidebar = true } = {}) {
  const session = state.sessions.get(sessionId);
  if (!session) return;

  const changed = state.currentSessionId !== sessionId;
  if (changed) {
    saveDraft(state.currentSessionId);
    autoLoadedAtTop = false;
  }
  state.currentSessionId = sessionId;
  rememberSession(sessionId);
  state.newMessageCount = 0;
  state.lastNewMessageSender = "";
  hideNewMessagePill();
  state.unread.set(sessionId, 0);

  elements.input.disabled = false;
  elements.input.placeholder = session.isGroup
    ? `发送到 ${session.peerName}`
    : `发送给 ${session.peerName}`;
  if (changed) {
    cancelReply();
    elements.input.value = readDraft(sessionId);
  }
  resizeComposer();
  updateSendState();

  renderSessionList();
  updateConversationHeader(session);
  renderParticipants(session);
  renderMessages({ scrollToBottom: shouldScroll || changed });

  if (closeSidebar) toggleSidebar(false);
  if (changed && state.streamStarted) connectStream();
  // 这里**不要**因为 state.connected 还是 false 就 startPolling()：
  // boot() 先 loadSessions() 再 connectStream()，选会话时 SSE 还没开始连，
  // connected 必然是 false。在这里兜底轮询会让终端「一启动就降级轮询」，
  // 之后即使 SSE 连上也已经白跑了一轮。轮询只由流生命周期触发：
  // 连接失败/超时/心跳假死时各监听器自己 startPolling()。
}

function resetConversation() {
  const changed = Boolean(state.currentSessionId);
  state.currentSessionId = "";
  if (changed && state.streamStarted) connectStream();
  elements.input.disabled = true;
  elements.input.value = "";
  elements.input.placeholder = "选择一个会话后输入消息";
  elements.conversationTitle.textContent = "选择一个会话";
  elements.conversationKind.hidden = true;
  elements.conversationId.textContent = "尚未连接会话";
  elements.conversationPlatform.hidden = true;
  elements.conversationAvatar.textContent = "#";
  elements.conversationAvatar.className = "avatar avatar-large";
  elements.emptyState.hidden = false;
  elements.messageItems.replaceChildren();
  setVirtualSpacers(0, 0);
  state.virtual = false;
  state.virtualWindow = { start: 0, end: 0 };
  elements.loadMore.hidden = true;
  cancelReply();
  elements.detailsTitle.textContent = "暂未选择";
  elements.detailKind.textContent = "-";
  elements.detailId.textContent = "-";
  elements.detailPlatform.textContent = "-";
  elements.detailCount.textContent = "0";
  elements.participantCount.textContent = "0";
  elements.participantList.replaceChildren();
  hideNewMessagePill();
  resizeComposer();
  updateSendState();
  elements.searchResults.replaceChildren();
  elements.searchCount.textContent = "";
}

function updateConversationHeader(session) {
  const kindLabel = session.isGroup ? "群聊" : "私聊";
  elements.conversationAvatar.textContent = getInitials(session.peerName);
  elements.conversationAvatar.className =
    `avatar avatar-large ${session.isGroup ? "avatar-group" : "avatar-private"}`;
  elements.conversationTitle.textContent = session.peerName;
  elements.conversationTitle.title = session.peerName;
  elements.conversationKind.textContent = kindLabel;
  elements.conversationKind.className = `badge ${session.isGroup ? "group" : "private"}`;
  elements.conversationKind.hidden = false;
  elements.conversationId.textContent = session.isGroup
    ? `群号 ${session.groupId || session.peerId}`
    : `私聊 ID ${session.peerId}`;
  elements.conversationId.title = elements.conversationId.textContent;
  elements.conversationPlatform.textContent = session.platform || "未知平台";
  elements.conversationPlatform.hidden = !session.platform;

  elements.detailsTitle.textContent = session.peerName;
  elements.detailKind.textContent = kindLabel;
  elements.detailId.textContent = session.peerId || "-";
  elements.detailPlatform.textContent = session.platform || "-";
  elements.detailCount.textContent = String(session.messageCount || 0);

  elements.btnPin.classList.toggle("active", Boolean(session.pinned));
  elements.btnPin.title = session.pinned ? "取消置顶" : "置顶会话";
  elements.btnPin.setAttribute("aria-pressed", session.pinned ? "true" : "false");
}

function renderParticipants(session) {
  const participants = [...(session.participants || [])].sort((a, b) => {
    if (a.lastSeen !== b.lastSeen) return b.lastSeen - a.lastSeen;
    return String(a.name).localeCompare(String(b.name), "zh-CN");
  });
  elements.participantCount.textContent = String(participants.length);
  elements.participantList.replaceChildren();

  if (!participants.length) {
    elements.participantList.append(createElement("p", "participant-id", "暂无参与者信息"));
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const person of participants) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "participant";
    row.title = `点击 @ ${person.name || person.id}`;
    row.addEventListener("click", () => mentionPerson(person));

    const avatar = createElement("span", "avatar avatar-private", getInitials(person.name));
    const copy = document.createElement("div");
    copy.className = "participant-copy";
    const name = createElement("div", "participant-name", person.name || person.id);
    name.title = person.name || person.id;
    const id = createElement("div", "participant-id", `ID ${compactId(person.id, 8, 6)}`);
    id.title = `ID ${person.id}`;
    copy.append(name, id);
    row.append(avatar, copy, createElement("span", "participant-count", `${person.messageCount || 0} 条`));
    fragment.append(row);
  }
  elements.participantList.append(fragment);
}

function mentionPerson(person) {
  if (!state.currentSessionId || !person?.id) return;
  const target = String(person.id);
  // 只走附件 chip 这一条路径：后端会把它直接变成 At 段。
  // 不要再往输入框塞 "@昵称"，否则会被后端的 @ 解析再展开一次，变成两个 @。
  if (!state.attachments.some((item) => item.type === "at" && item.target === target)) {
    state.attachments.push({ type: "at", target, text: person.name });
  }
  renderAttachments();
  elements.input.focus();
}

// ── 消息渲染 ────────────────────────────────────────────────

function renderMessages({ scrollToBottom: shouldScroll = false } = {}) {
  const sessionId = state.currentSessionId;
  const session = state.sessions.get(sessionId);
  const messages = state.messages.get(sessionId) || [];
  const previousTop = elements.messages.scrollTop;

  elements.emptyState.hidden = messages.length > 0 || !session;
  elements.loadMore.hidden = !(session && state.history.get(sessionId)?.hasMore);
  elements.loadMore.disabled = state.loadingHistory;

  if (!session) {
    elements.messageItems.replaceChildren();
    setVirtualSpacers(0, 0);
    state.renderPlan = [];
    state.renderSessionId = "";
    state.virtual = false;
    state.virtualWindow = { start: 0, end: 0 };
    return;
  }

  const switching = state.renderSessionId !== sessionId;
  if (switching) {
    // 切换会话：diff 无法推断（空计划会被判成 append），必须清空后整树重建
    elements.messageItems.replaceChildren();
    state.heights.clear();
    setVirtualSpacers(0, 0);
    elements.messages.scrollTop = 0;
    state.renderPlan = [];
    state.renderSessionId = sessionId;
    state.virtualWindow = { start: 0, end: 0 };
  }

  const previousPlan = state.renderPlan;
  const nextPlan = planMessages(messages, sessionId);
  state.renderPlan = nextPlan;

  // 少量消息保持全量渲染：小会话行为与旧版完全一致，虚拟滚动只在大列表生效
  const wasVirtual = state.virtual;
  const virtual = nextPlan.length > VIRTUAL_THRESHOLD;
  state.virtual = virtual;

  if (virtual) {
    renderVirtualWindow({ session, plan: nextPlan, pinToEnd: shouldScroll });
  } else {
    if (wasVirtual) {
      // 从虚拟窗口退回全量渲染：DOM 里只剩一段窗口节点，按 plan diff 增量修补会对不上，
      // 必须整树重建，否则会残留/错位
      const fragment = document.createDocumentFragment();
      for (const item of nextPlan) fragment.append(buildPlanNode(item, session));
      elements.messageItems.replaceChildren(fragment);
    } else {
      applyPlanDiff(diffPlan(previousPlan, nextPlan), previousPlan, nextPlan, session);
    }
    setVirtualSpacers(0, 0);
    state.virtualWindow = { start: 0, end: nextPlan.length };
  }

  if (shouldScroll) {
    requestAnimationFrame(() => {
      elements.messages.scrollTop = elements.messages.scrollHeight;
      markCurrentSessionRead();
    });
  } else if (!virtual) {
    elements.messages.scrollTop = previousTop;
  }
}

// ── 虚拟滚动 ────────────────────────────────────────────────

function setVirtualSpacers(topPad, bottomPad) {
  elements.virtualTop.style.height = `${Math.max(0, Math.round(topPad))}px`;
  elements.virtualBottom.style.height = `${Math.max(0, Math.round(bottomPad))}px`;
  elements.virtualTop.hidden = topPad <= 0;
  elements.virtualBottom.hidden = bottomPad <= 0;
}

/** 估算未测量行高：已测量行的平均值，没有测量值时用兜底值。 */
function averageMessageHeight() {
  if (!state.heights.size) return MESSAGE_HEIGHT_FALLBACK;
  let total = 0;
  for (const value of state.heights.values()) total += value;
  return Math.max(24, total / state.heights.size) || MESSAGE_HEIGHT_FALLBACK;
}

/** 只渲染视口附近的一段 plan，上下用 spacer 撑住滚动高度。 */
function renderVirtualWindow({ session, plan, pinToEnd = false }) {
  const container = elements.messages;
  const heights = plan.map((item) => state.heights.get(item.key));
  const win = computeWindow({
    count: plan.length,
    heights,
    scrollTop: container.scrollTop,
    viewport: container.clientHeight || 0,
    forecast: averageMessageHeight(),
    overscan: VIRTUAL_OVERSCAN,
    minRender: VIRTUAL_MIN_RENDER,
    pinToEnd,
  });

  // 视口顶部压在哪个 plan 项上：窗口平移后靠它把内容钉回原位。
  // 未渲染消息只有估算高度，估算误差（尤其是图片）会让整屏内容上下跳一大截。
  const anchor = pinToEnd ? null : windowAnchor(win.offsets, container.scrollTop);

  const fragment = document.createDocumentFragment();
  for (let index = win.start; index < win.end; index += 1) {
    fragment.append(buildPlanNode(plan[index], session));
  }
  elements.messageItems.replaceChildren(fragment);
  setVirtualSpacers(win.topPad, win.bottomPad);
  state.virtualWindow = { start: win.start, end: win.end };

  // 替换节点后浏览器可能已经钳过 scrollTop，这里记下来当作用户位移的基准
  const scrollTopAfterSwap = container.scrollTop;
  requestAnimationFrame(() => {
    // 这一帧里又渲染过别的计划/窗口：DOM 已经不是这一轮的内容，测量和回正都得跳过，
    // 否则会把新窗口的行高记到旧计划的 key 上，越滚越乱
    if (state.renderPlan !== plan || state.virtualWindow.start !== win.start) return;
    measureRenderedHeights(plan);
    restoreVirtualAnchor({ plan, win, anchor, scrollTopAfterSwap });
  });
}

/**
 * 窗口平移后把锚点放回渲染前的位置。
 *
 * 只补偿窗口内部那些刚被渲染出来的项：它们的高度现在是实测值，
 * 而 spacer 用的是估算值，两者之差就是肉眼看到的「跳一下」。
 */
function restoreVirtualAnchor({ plan, win, anchor, scrollTopAfterSwap }) {
  if (!state.virtual || !anchor) return;
  if (anchor.index < win.start || anchor.index >= win.end) return;

  // 必须在 measureRenderedHeights 之后重新取：这一轮新渲染的项行高刚刚才测出来
  const heights = plan.map((item) => state.heights.get(item.key) || 0);
  const target = anchorScrollTop({
    topPad: win.topPad,
    start: win.start,
    index: anchor.index,
    heights,
    forecast: averageMessageHeight(),
    delta: anchor.delta,
  });
  // 渲染到这一帧之间用户还在滚：把这段位移叠加回来，不能把用户拽回去
  const container = elements.messages;
  const userMoved = container.scrollTop - scrollTopAfterSwap;
  const next = target + userMoved;
  if (Math.abs(next - container.scrollTop) > 0.5) {
    container.scrollTop = next;
  }
}

/** 渲染后回写实测行高，下一轮窗口计算就能用上。 */
function measureRenderedHeights(plan) {
  if (!state.virtual) return;
  const nodes = elements.messageItems.children;
  const start = state.virtualWindow.start;
  for (let offset = 0; offset < nodes.length; offset += 1) {
    const item = plan[start + offset];
    if (!item) break;
    const node = nodes[offset];
    const height = node.getBoundingClientRect?.().height || node.offsetHeight || 0;
    if (height > 0) state.heights.set(item.key, height);
  }
}

function refreshVirtualWindow() {
  if (!state.virtual) return;
  const session = state.sessions.get(state.currentSessionId);
  const plan = state.renderPlan;
  if (!session || !plan.length) return;
  renderVirtualWindow({ session, plan, pinToEnd: false });
}

/** 按 diff 结果做最小 DOM 变更，而不是每次重建整棵树。 */
function applyPlanDiff(diff, previousPlan, nextPlan, session) {
  const content = elements.messageItems;

  if (diff.mode === "append") {
    if (diff.start === diff.end) return;
    const fragment = document.createDocumentFragment();
    for (let index = diff.start; index < diff.end; index += 1) {
      fragment.append(buildPlanNode(nextPlan[index], session));
    }
    content.append(fragment);
    return;
  }

  if (diff.mode === "truncate") {
    // 移除多余节点：从后往前删，避免索引漂移
    let removed = 0;
    while (content.lastChild && removed < diff.end - diff.start) {
      content.removeChild(content.lastChild);
      removed += 1;
    }
    return;
  }

  // rebuild：整体替换
  const fragment = document.createDocumentFragment();
  for (const item of nextPlan) fragment.append(buildPlanNode(item, session));
  content.replaceChildren(fragment);
}

function buildPlanNode(item, session) {
  if (item.kind === "date") {
    return createElement("div", "date-separator", item.label);
  }
  const node = renderMessage(item.message, session, item.grouped);
  node.dataset.planKey = item.key;
  return node;
}

function renderMessage(message, session, grouped) {
  const row = document.createElement("article");
  const isSent = message.type === "sent";
  const isBot = message.type === "bot";
  row.className =
    `message-row ${isSent ? "sent" : isBot ? "bot" : "received"}${grouped ? " grouped" : ""}${
      message.failed ? " failed" : ""
    }`;
  row.dataset.messageId = message.id || "";

  const displayName = isSent ? "你" : message.senderName || (isBot ? "Bot" : "未知用户");
  const avatar = createElement(
    "span",
    `avatar ${isSent || !session.isGroup ? "avatar-private" : "avatar-group"}`,
    getInitials(displayName),
  );
  avatar.title = displayName;

  const stack = document.createElement("div");
  stack.className = "message-stack";

  if (!grouped) {
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const author = createElement("span", "message-author", displayName);
    author.title = displayName;
    meta.append(author);

    if (message.type === "received" && message.senderId) {
      const senderId = createElement("span", "sender-id", `#${compactId(message.senderId)}`);
      senderId.title = `发送者 ID: ${message.senderId}`;
      meta.append(senderId);
    }

    const time = createElement("time", "message-time", formatMessageTime(message.timestamp));
    time.dateTime = message.timestamp ? new Date(message.timestamp * 1000).toISOString() : "";
    time.title = formatFullTime(message.timestamp);
    meta.append(time);
    stack.append(meta);
  }

  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  appendBubbleContent(bubble, message);
  stack.append(bubble);

  if (message.id || message.failed) {
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const copyButton = createElement("button", "message-action", "复制");
    copyButton.type = "button";
    copyButton.addEventListener("click", () => copyText(messageText(message)));
    actions.append(copyButton);

    // 引用：只有带平台消息 id 的入站消息才能被引用（Bot/终端自己发的行没有）
    if (message.type === "received" && isQuotableId(message.platformMessageId)) {
      const replyButton = createElement("button", "message-action", "引用");
      replyButton.type = "button";
      replyButton.addEventListener("click", () => startReply(message));
      actions.append(replyButton);
    }

    if (message.failed) {
      const retryButton = createElement("button", "message-action", "重试");
      retryButton.type = "button";
      retryButton.addEventListener("click", () => retryMessage(message));
      const discardButton = createElement("button", "message-action danger", "丢弃");
      discardButton.type = "button";
      discardButton.addEventListener("click", () =>
        removeMessageByKey(message.sessionId, messageKey(message)),
      );
      actions.append(retryButton, discardButton);
    } else {
      const deleteButton = createElement("button", "message-action danger", "删除");
      deleteButton.type = "button";
      deleteButton.addEventListener("click", () => deleteMessage(message));
      actions.append(deleteButton);
    }
    stack.append(actions);
  }

  row.append(avatar, stack);
  return row;
}

function appendBubbleContent(bubble, message) {
  const segments = message.segments || [];
  const images = imageSources(message);

  // 引用块排在最前面，像聊天软件那样先说明「在回复谁」
  for (const segment of segments) {
    if (segment.type === "reply") bubble.append(buildQuoteBlock(segment, message));
  }

  if (images.length) {
    const gallery = createElement("div", "message-images");
    for (const source of images) {
      gallery.append(
        state.loadImages || state.loadedImages.has(source)
          ? buildImage(source)
          : buildImagePlaceholder(source),
      );
    }
    bubble.append(gallery);
  }

  // 非文本片段（@、表情、语音、文件…）用 chip 展示；引用已经单独渲染
  const chips = segments.filter(
    (segment) => segment.type !== "text" && segment.type !== "image" && segment.type !== "reply",
  );
  if (chips.length) {
    const parts = createElement("div", "message-segments");
    for (const segment of chips) parts.append(renderSegmentChip(segment));
    bubble.append(parts);
  }

  const visibleText = segments.length
    ? segments
        .map((segment) => (segment.type === "text" ? stripMentionMarkup(segment.text) : ""))
        .join("")
    : "";
  // 有结构化片段（图片之外的 @/表情/语音/文件/转发）时，正文不要再把占位文案重复一遍
  const hasStructured = segments.some(
    (segment) => segment.type !== "text" && segment.type !== "image",
  );
  const fallbackText =
    visibleText || (images.length || hasStructured ? "" : messageText(message));
  if (fallbackText) bubble.append(createElement("span", "message-text", fallbackText));

  if (message.pending) bubble.append(createElement("span", "message-pending", "发送中…"));
  if (message.failed) bubble.append(createElement("span", "message-failed", "发送失败，可重试"));
}

/** 引用块：作者 + 被引用内容；本地缓存里找得到就用缓存兜底。 */
function buildQuoteBlock(segment, message) {
  const quote = resolveQuote(segment, message);
  const block = createElement("div", "quote-block");
  const author = quote?.sender ? `回复 ${quote.sender}` : "引用消息";
  const text =
    quote?.text ||
    (segment.text && segment.text !== "[引用]" ? segment.text : "（原消息不在本地记录里）");

  block.append(createElement("span", "quote-block-author", author));
  block.append(createElement("span", "quote-block-text", String(text).slice(0, 120)));
  block.title = String(text);
  return block;
}

function resolveQuote(segment, message) {
  if (segment.quote?.text) return segment.quote;
  const target = String(segment.target || "");
  if (!target) return null;
  const sessionId = message?.sessionId || state.currentSessionId;
  const list = state.messages.get(sessionId) || [];
  const found = list.find((item) => String(item.platformMessageId || "") === target);
  if (!found) return null;
  return { sender: found.senderName, text: messageText(found) };
}

function buildImage(source) {
  const image = document.createElement("img");
  image.className = "message-image";
  image.loading = "lazy";
  image.alt = "图片消息";
  image.src = mediaUrl(source);
  image.addEventListener("click", (event) => {
    event.stopPropagation();
    openLightbox(image.src, source);
  });
  image.addEventListener("load", () => {
    // 图片真实尺寸决定了行高，加载完回写一次长度，虚拟窗口才不会跳
    if (state.virtual) requestAnimationFrame(() => measureRenderedHeights(state.renderPlan));
  });
  image.addEventListener("error", () => {
    image.replaceWith(createElement("span", "message-media-fallback", "[图片加载失败]"));
  });
  return image;
}

/**
 * 图片占位按钮：默认不加载任何图片，点这一张才发请求。
 *
 * 注意不能把地址塞进 `<img src>` 后靠 CSS 隐藏——那样浏览器照样会下载。
 */
function buildImagePlaceholder(source) {
  const placeholder = createElement("button", "message-image-placeholder");
  placeholder.type = "button";
  placeholder.title = "点击加载这张图片";
  placeholder.append(
    createElement("span", "message-image-placeholder-icon", "🖼"),
    createElement("span", "message-image-placeholder-text", "点击加载图片"),
  );
  placeholder.addEventListener("click", (event) => {
    event.stopPropagation();
    // 记住「这一张被点开过」：虚拟滚动重渲染时按它还原成 <img>，否则会退回占位
    state.loadedImages.add(source);
    // 只替换这一张，不改变全局开关
    placeholder.replaceWith(buildImage(source));
  });
  return placeholder;
}

function renderSegmentChip(segment) {
  const label =
    segment.text ||
    (segment.type === "image"
      ? "[图片]"
      : segment.type === "file"
        ? `[文件] ${segment.name || ""}`
        : `[${segment.type}]`);

  // 合并转发：渲染成按钮，点开看聊天记录
  if (segment.type === "forward") {
    const count = Array.isArray(segment.messages) ? segment.messages.length : 0;
    const chip = createElement("button", "segment-chip forward forward-chip");
    chip.type = "button";
    chip.title = "点击查看聊天记录";
    chip.textContent = count
      ? `聊天记录 · ${count} 条`
      : segment.id
        ? "聊天记录 · 点击加载"
        : "聊天记录";
    chip.addEventListener("click", () => openForwardDialog(segment));
    return chip;
  }

  const chip = createElement("span", `segment-chip ${segment.type}`);
  chip.textContent = String(label).trim();
  return chip;
}

// ── 合并转发详情 ────────────────────────────────────────────

async function openForwardDialog(segment) {
  const body = createElement("div", "forward-body");
  const nodes = Array.isArray(segment.messages) ? segment.messages : [];

  if (nodes.length) {
    renderForwardMessages(body, nodes);
  } else if (segment.id && state.currentSessionId) {
    body.append(createElement("p", "dialog-description", "正在加载聊天记录…"));
    try {
      const fetched = await fetchForwardMessages(state.currentSessionId, segment.id);
      body.replaceChildren();
      if (fetched.length) {
        renderForwardMessages(body, fetched);
      } else {
        body.append(
          createElement("p", "dialog-description", "协议端没有返回内容（可能不支持拉取合并转发）"),
        );
      }
    } catch (error) {
      body.replaceChildren(
        createElement("p", "dialog-description", `加载失败：${errorText(error)}`),
      );
    }
  } else {
    body.append(createElement("p", "dialog-description", "这条合并转发没有可展开的内容"));
  }

  infoDialog({ title: "聊天记录", content: body });
}

function renderForwardMessages(container, nodes) {
  for (const node of nodes) {
    const row = createElement("div", "forward-row");
    const head = createElement("div", "forward-head");
    head.append(createElement("span", "forward-name", node.name || node.uin || "未知"));
    if (node.uin) head.append(createElement("span", "forward-uin", `#${compactId(node.uin)}`));
    row.append(head);

    const parts = [];
    for (const item of node.segments || []) {
      if (item.type === "text") parts.push(item.text);
      else if (item.type === "image") parts.push("[图片]");
      else if (item.type === "at") parts.push(`@${item.text || item.target}`);
      else if (item.type === "forward") parts.push("[聊天记录]");
      else parts.push(item.text || `[${item.type}]`);
    }
    row.append(createElement("div", "forward-text", parts.join("").trim() || "[空消息]"));
    container.append(row);
  }
}

async function fetchForwardMessages(sessionId, forwardId) {
  const data = await bridge.apiGet("forward", { session_id: sessionId, id: forwardId });
  const raw = Array.isArray(data?.messages) ? data.messages : [];
  return raw.map((node) => ({
    name: String(node?.name || ""),
    uin: String(node?.uin || ""),
    segments: (Array.isArray(node?.segments) ? node.segments : [])
      .map(normalizeSegment)
      .filter(Boolean),
  }));
}

// ── 滚动与未读 ──────────────────────────────────────────────

function isNearBottomOfMessageList() {
  return isNearBottom(
    {
      scrollHeight: elements.messages.scrollHeight,
      scrollTop: elements.messages.scrollTop,
      clientHeight: elements.messages.clientHeight,
    },
    NEAR_BOTTOM_PX,
  );
}

let virtualScrollFrame = 0;

function handleMessageScroll() {
  if (isNearBottomOfMessageList() && (state.unread.get(state.currentSessionId) || 0) > 0) {
    markCurrentSessionRead();
  }
  maybeAutoLoadOlder();
  if (!state.virtual || virtualScrollFrame) return;
  virtualScrollFrame = requestAnimationFrame(() => {
    virtualScrollFrame = 0;
    refreshVirtualWindow();
  });
}

/** 滚到顶部自动补一页更早消息，不用非得去点「加载更早消息」。 */
function maybeAutoLoadOlder() {
  const container = elements.messages;
  if (container.scrollTop > AUTO_LOAD_TOP_PX) {
    autoLoadedAtTop = false;
    return;
  }
  if (autoLoadedAtTop || state.loadingHistory) return;
  const history = state.history.get(state.currentSessionId);
  if (!history?.hasMore || history.nextCursor == null) return;
  autoLoadedAtTop = true;
  loadOlderMessages();
}

function markCurrentSessionRead() {
  const sessionId = state.currentSessionId;
  if (!sessionId) return;
  state.unread.set(sessionId, 0);
  state.newMessageCount = 0;
  hideNewMessagePill();
  renderSessionList();
}

function scrollToBottom(smooth = false) {
  if (state.virtual) {
    const session = state.sessions.get(state.currentSessionId);
    if (session) renderVirtualWindow({ session, plan: state.renderPlan, pinToEnd: true });
  }
  elements.messages.scrollTo({
    top: elements.messages.scrollHeight,
    behavior: smooth ? "smooth" : "auto",
  });
}

// ── 历史分页 ────────────────────────────────────────────────

async function loadOlderMessages() {
  const sessionId = state.currentSessionId;
  const history = state.history.get(sessionId);
  if (!sessionId || !history?.hasMore || history.nextCursor == null || state.loadingHistory) return;

  state.loadingHistory = true;
  elements.loadMore.disabled = true;
  elements.loadMore.querySelector("span").textContent = "正在加载";
  const previousHeight = elements.messages.scrollHeight;
  const previousTop = elements.messages.scrollTop;

  try {
    const data = await bridge.apiGet("history", {
      session_id: sessionId,
      limit: String(HISTORY_LIMIT),
      before_id: String(history.nextCursor),
    });
    const older = (Array.isArray(data?.messages) ? data.messages : []).map((message) =>
      normalizeMessage(message, sessionId),
    );
    const merged = mergeMessages(older, state.messages.get(sessionId) || []);
    const beforePlan = state.renderPlan;
    state.messages.set(sessionId, merged);
    state.history.set(sessionId, normalizeHistory(data));

    if (state.virtual) {
      // 更早的消息插在 plan 头部：先把滚动位置抬高同样的高度，窗口才不会错位
      const nextPlan = planMessages(merged, sessionId);
      const added = Math.max(0, nextPlan.length - beforePlan.length);
      const forecast = averageMessageHeight();
      let addedHeight = 0;
      for (let index = 0; index < added; index += 1) {
        addedHeight += state.heights.get(nextPlan[index].key) || forecast;
      }
      elements.messages.scrollTop = previousTop + addedHeight;
      renderMessages();
    } else {
      renderMessages();
      requestAnimationFrame(() => {
        elements.messages.scrollTop =
          previousTop + (elements.messages.scrollHeight - previousHeight);
      });
    }
  } catch (error) {
    toast(`历史消息加载失败: ${errorText(error)}`, "error");
  } finally {
    state.loadingHistory = false;
    elements.loadMore.disabled = false;
    elements.loadMore.querySelector("span").textContent = "加载更早消息";
    renderMessages();
  }
}

// ── 发送 ────────────────────────────────────────────────────

function updateSendState() {
  const hasContent = elements.input.value.trim().length > 0 || state.attachments.length > 0;
  elements.btnSend.disabled = !hasContent || state.sending || !state.currentSessionId;
}

async function sendMessage() {
  const session = state.sessions.get(state.currentSessionId);
  const text = elements.input.value.trim();
  if (!session) {
    toast("请先选择一个会话", "error");
    return;
  }
  if ((!text && !state.attachments.length) || state.sending) return;

  const reply = state.replyTo?.sessionId === session.id ? state.replyTo : null;
  const attachments = state.attachments;
  // 引用必须排在最前，协议端按链首识别回复关系
  const segments = [];
  if (reply) {
    segments.push({ type: "reply", target: reply.target, text: reply.text });
  }
  segments.push(...attachments);

  state.sending = true;
  elements.btnSend.disabled = true;
  elements.input.value = "";
  state.attachments = [];
  renderAttachments();
  resizeComposer();
  clearDraft(session.id);

  try {
    const response = await bridge.apiPost("send", {
      session_id: session.id,
      message: text,
      segments,
    });
    const message = response?.data
      ? normalizeMessage(response.data, session.id)
      : normalizeMessage(
          {
            id: null,
            session_id: session.id,
            type: "sent",
            sender: "Terminal",
            sender_name: "你",
            content: text,
            segments: [...(text ? [{ type: "text", text }] : []), ...attachments],
            timestamp: Date.now() / 1000,
            pending: true,
          },
          session.id,
        );
    applyOutgoingMessage(message);
    cancelReply();
  } catch (error) {
    // 失败不回滚：留一条可重试的本地气泡，消息内容不能凭空消失
    const failed = normalizeMessage(
      {
        id: null,
        session_id: session.id,
        type: "sent",
        sender: "Terminal",
        sender_name: "你",
        content: text,
        segments: [...(text ? [{ type: "text", text }] : []), ...attachments],
        timestamp: Date.now() / 1000,
        failed: true,
      },
      session.id,
    );
    failed.retryPayload = { message: text, segments };
    addLocalMessage(failed, { scrollToBottom: true });
    toast(`发送失败: ${errorText(error)}`, "error");
  } finally {
    state.sending = false;
    updateSendState();
  }
}

async function retryMessage(message) {
  if (state.sending || !message.retryPayload) return;
  const payload = message.retryPayload;
  state.sending = true;
  updateSendState();
  try {
    const response = await bridge.apiPost("send", {
      session_id: message.sessionId,
      message: payload.message,
      segments: payload.segments,
    });
    removeMessageByKey(message.sessionId, messageKey(message));
    if (response?.data) {
      applyOutgoingMessage(normalizeMessage(response.data, message.sessionId));
    }
    toast("已重新发送");
  } catch (error) {
    toast(`重试失败: ${errorText(error)}`, "error");
  } finally {
    state.sending = false;
    updateSendState();
  }
}

/** 本地（未落库）消息：发送失败的气泡走这里，不推进会话计数与预览。 */
function addLocalMessage(message, { scrollToBottom: shouldScroll = false } = {}) {
  const messages = state.messages.get(message.sessionId) || [];
  state.messages.set(message.sessionId, mergeMessages(messages, [message]));
  if (message.sessionId === state.currentSessionId) {
    renderMessages({ scrollToBottom: shouldScroll });
  }
}

function removeMessageByKey(sessionId, key) {
  const messages = state.messages.get(sessionId);
  if (!Array.isArray(messages)) return;
  const next = messages.filter((item) => messageKey(item) !== key);
  if (next.length === messages.length) return;
  state.messages.set(sessionId, next);
  if (sessionId === state.currentSessionId) renderMessages();
}

// ── 引用回复 ────────────────────────────────────────────────

/** 只有平台真实消息 id 能引用；AstrBot 历史导入的 astrbot:<id> 是本地合成 id。 */
function isQuotableId(value) {
  const id = String(value || "");
  return Boolean(id) && !id.startsWith("astrbot:");
}

function startReply(message) {
  const sessionId = state.currentSessionId;
  if (!sessionId || !isQuotableId(message?.platformMessageId)) return;
  const preview = messageText(message).replace(/\s+/g, " ").slice(0, 60);
  state.replyTo = {
    sessionId,
    target: message.platformMessageId,
    senderName: message.senderName || "对方",
    text: preview || "[消息]",
  };
  renderReplyPreview();
  elements.input.focus();
}

function renderReplyPreview() {
  const reply = state.replyTo;
  elements.replyPreview.hidden = !reply;
  if (!reply) return;
  elements.replyPreviewName.textContent = reply.senderName;
  elements.replyPreviewText.textContent = reply.text;
}

function cancelReply() {
  if (!state.replyTo) return;
  state.replyTo = null;
  renderReplyPreview();
}

// ── 草稿 ────────────────────────────────────────────────────

let draftTimer = 0;

function draftStorageKey(sessionId) {
  return `${DRAFT_KEY}:${sessionId}`;
}

function scheduleDraftSave() {
  if (draftTimer) window.clearTimeout(draftTimer);
  const sessionId = state.currentSessionId;
  draftTimer = window.setTimeout(() => {
    draftTimer = 0;
    // 切换会话时 selectSession 已经同步存过旧草稿，这里只处理没切走的情况
    if (state.currentSessionId === sessionId) saveDraft(sessionId);
  }, 400);
}

function saveDraft(sessionId) {
  if (!sessionId) return;
  const text = elements.input.value;
  if (!text.trim()) {
    clearDraft(sessionId);
    return;
  }
  state.drafts.set(sessionId, text);
  try {
    window.localStorage.setItem(draftStorageKey(sessionId), text);
  } catch {
    // iframe 沙箱里 localStorage 可能不可用，内存里的 drafts 仍然生效
  }
}

function readDraft(sessionId) {
  if (!sessionId) return "";
  if (state.drafts.has(sessionId)) return state.drafts.get(sessionId);
  try {
    return window.localStorage.getItem(draftStorageKey(sessionId)) || "";
  } catch {
    return "";
  }
}

function clearDraft(sessionId) {
  const id = sessionId || state.currentSessionId;
  if (!id) return;
  state.drafts.delete(id);
  try {
    window.localStorage.removeItem(draftStorageKey(id));
  } catch {
    // 同上
  }
}

function applyOutgoingMessage(message) {
  const session = state.sessions.get(state.currentSessionId);
  if (!session) return;
  const messages = state.messages.get(session.id) || [];
  if (message.id && messages.some((item) => item.id === message.id)) return; // SSE 已先到
  state.messages.set(session.id, mergeMessages(messages, [message]));
  session.messageCount += 1;
  session.lastMessage = message;
  elements.detailCount.textContent = String(session.messageCount);
  renderSessionList();
  renderMessages({ scrollToBottom: true });
}

// ── 附件 ────────────────────────────────────────────────────

async function onAttachmentPicked(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (file) await addAttachmentFromFile(file);
}

function onInputPaste(event) {
  const items = event.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        addAttachmentFromFile(file);
      }
      return;
    }
  }
}

async function addAttachmentFromFile(file) {
  if (!file.type.startsWith("image/")) {
    toast("目前只支持粘贴或选择图片", "error");
    return;
  }
  if (file.size > 4 * 1024 * 1024) {
    toast("图片过大（上限 4MB）", "error");
    return;
  }
  try {
    const dataUrl = await readAsDataURL(file);
    state.attachments.push({ type: "image", url: dataUrl, name: file.name });
    renderAttachments();
  } catch (error) {
    toast(`读取图片失败: ${errorText(error)}`, "error");
  }
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("读取失败"));
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  elements.attachmentPreview.replaceChildren();
  if (!state.attachments.length) {
    elements.attachmentPreview.hidden = true;
    updateSendState();
    return;
  }
  elements.attachmentPreview.hidden = false;
  state.attachments.forEach((item, index) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    if (item.type === "image") {
      const image = document.createElement("img");
      image.src = item.url;
      image.alt = "待发送图片";
      chip.append(image);
    } else {
      chip.append(createElement("span", "attachment-text", `@${item.text || item.target}`));
    }
    const remove = createElement("button", "attachment-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "移除附件");
    remove.addEventListener("click", () => {
      state.attachments.splice(index, 1);
      renderAttachments();
    });
    chip.append(remove);
    elements.attachmentPreview.append(chip);
  });
  updateSendState();
}

// ── 检索 ────────────────────────────────────────────────────

function onSearchInput() {
  const query = elements.messageSearch.value.trim();
  state.searchQuery = query;
  if (!query) {
    state.searchResults = [];
    elements.searchStatus.textContent = "";
    renderSearchResults();
    return;
  }
  runSearch(query);
}

async function runSearch(query) {
  elements.searchStatus.textContent = "检索中…";
  try {
    const data = await bridge.apiGet("search", {
      q: query,
      session_id: state.currentSessionId || "",
      limit: "60",
    });
    if (state.searchQuery !== query) return; // 输入已更新，丢弃旧结果
    state.searchResults = (Array.isArray(data?.results) ? data.results : []).map((item) =>
      normalizeMessage(item, item.session_id),
    );
    elements.searchStatus.textContent = "";
    renderSearchResults();
  } catch (error) {
    elements.searchStatus.textContent = `检索失败: ${errorText(error)}`;
  }
}

function renderSearchResults() {
  const query = state.searchQuery;
  elements.searchResults.replaceChildren();
  elements.searchCount.textContent = query ? String(state.searchResults.length) : "";

  if (!query) {
    elements.searchResults.hidden = true;
    return;
  }
  elements.searchResults.hidden = false;
  if (!state.searchResults.length) {
    elements.searchResults.append(createElement("p", "search-empty", "没有匹配的消息"));
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const message of state.searchResults) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "search-hit";
    const head = createElement("span", "search-hit-head");
    head.append(createElement("span", "search-hit-sender", message.senderName));
    head.append(createElement("span", "search-hit-time", formatFullTime(message.timestamp)));
    const body = createElement("span", "search-hit-body");
    for (const part of splitHighlight(messageText(message), query)) {
      body.append(
        part.hit ? createElement("mark", "", part.text) : document.createTextNode(part.text),
      );
    }
    row.append(head, body);
    row.addEventListener("click", () => jumpToMessage(message));
    fragment.append(row);
  }
  elements.searchResults.append(fragment);
}

async function jumpToMessage(message) {
  const sessionId = message.sessionId;
  if (!state.sessions.has(sessionId)) return;
  if (sessionId !== state.currentSessionId) {
    selectSession(sessionId, { scrollToBottom: false });
  }
  const selector = message.id ? `[data-message-id="${CSS.escape(String(message.id))}"]` : "";
  let target = selector ? elements.messageContent.querySelector(selector) : null;

  // 虚拟滚动下目标可能还没渲染：直接按 plan 里的位置滚过去再取节点
  if (!target && state.virtual && selector) {
    target = scrollVirtualWindowToMessage(message);
  }

  // 目标不在当前窗口时，继续向前翻页直到命中或到底
  for (let attempt = 0; attempt < 6 && !target; attempt += 1) {
    const history = state.history.get(sessionId);
    if (!history?.hasMore) break;
    await loadOlderMessages();
    target = selector ? elements.messageContent.querySelector(selector) : null;
  }

  if (!target && state.virtual && selector) {
    target = scrollVirtualWindowToMessage(message);
  }

  if (target) {
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.classList.add("flash");
    window.setTimeout(() => target.classList.remove("flash"), 1600);
  } else {
    toast("该消息已不在当前记录中", "error");
  }
}

// ── 消息操作 ────────────────────────────────────────────────

/** 把虚拟窗口滚到指定消息所在的位置，返回渲染出来的节点（找不到返回 null）。 */
function scrollVirtualWindowToMessage(message) {
  const session = state.sessions.get(state.currentSessionId);
  const plan = state.renderPlan;
  if (!session || !plan.length) return null;
  const index = plan.findIndex(
    (item) => item.kind === "message" && String(item.message?.id) === String(message.id),
  );
  if (index < 0) return null;

  const forecast = averageMessageHeight();
  let offset = 0;
  for (let cursor = 0; cursor < index; cursor += 1) {
    offset += state.heights.get(plan[cursor].key) || forecast;
  }
  elements.messages.scrollTop = offset;
  renderVirtualWindow({ session, plan, pinToEnd: false });
  return elements.messageItems.querySelector(
    `[data-message-id="${CSS.escape(String(message.id))}"]`,
  );
}

async function deleteMessage(message) {
  if (!message.id) return;
  const confirmed = await confirmDialog({
    title: "删除这条消息？",
    description: "仅从本插件的记录中移除，不影响平台上的原始消息。",
    confirmText: "删除",
    danger: true,
  });
  if (!confirmed) return;
  try {
    await apiDelete(`messages/${message.id}`);
    removeMessageLocally(message.sessionId, String(message.id));
    toast("已删除");
  } catch (error) {
    toast(`删除失败: ${errorText(error)}`, "error");
  }
}

function removeMessageLocally(sessionId, messageId) {
  const messages = state.messages.get(sessionId);
  if (!Array.isArray(messages)) return;
  const next = messages.filter((item) => item.id !== messageId);
  const summaryRemoved = state.summaryIds.delete(`${sessionId}:${messageId}`);
  const removed = messages.length - next.length || Number(summaryRemoved);
  if (summaryRemoved) state.unread.set(sessionId, Math.max(0, (state.unread.get(sessionId) || 0) - 1));
  if (removed <= 0) return;

  state.messages.set(sessionId, next);
  const session = state.sessions.get(sessionId);
  if (session) {
    session.messageCount = Math.max(0, session.messageCount - removed);
    if (session.lastMessage?.id === messageId) session.lastMessage = next[next.length - 1] || null;
    if (sessionId === state.currentSessionId) {
      elements.detailCount.textContent = String(session.messageCount);
    }
  }
  renderSessionList();
  if (sessionId === state.currentSessionId) renderMessages();
}

async function togglePin() {
  const session = state.sessions.get(state.currentSessionId);
  if (!session) return;
  const pinned = !session.pinned;
  try {
    await bridge.apiPost(`sessions/${encodeURIComponent(session.id)}`, { pinned });
    session.pinned = pinned;
    renderSessionList();
    elements.btnPin.classList.toggle("active", pinned);
    elements.btnPin.setAttribute("aria-pressed", pinned ? "true" : "false");
    elements.btnPin.title = pinned ? "取消置顶" : "置顶会话";
    toast(pinned ? "已置顶" : "已取消置顶");
  } catch (error) {
    toast(`操作失败: ${errorText(error)}`, "error");
  }
}

async function exportSession() {
  const session = state.sessions.get(state.currentSessionId);
  if (!session) return;
  try {
    const data = await bridge.apiGet("export", { session_id: session.id, format: "txt" });
    const blob = new Blob([data?.text || ""], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${session.peerName || "webchat"}.txt`;
    link.click();
    URL.revokeObjectURL(url);
    toast("已导出");
  } catch (error) {
    toast(`导出失败: ${errorText(error)}`, "error");
  }
}

async function clearMessages() {
  const session = state.sessions.get(state.currentSessionId);
  if (!session) {
    toast("请先选择一个会话", "error");
    return;
  }
  const confirmed = await confirmDialog({
    title: `清空「${session.peerName}」的记录？`,
    description: "只清除本插件保存的历史消息，平台侧不受影响。",
    confirmText: "清空",
    danger: true,
  });
  if (!confirmed) return;

  elements.btnClear.disabled = true;
  try {
    await bridge.apiPost("clear", { session_id: session.id });
    state.messages.set(session.id, []);
    state.history.set(session.id, { hasMore: false, nextCursor: null });
    state.unread.set(session.id, 0);
    session.messageCount = 0;
    session.lastMessage = null;
    session.participants = [];
    elements.detailCount.textContent = "0";
    renderSessionList();
    renderParticipants(session);
    renderMessages({ scrollToBottom: true });
    toast("消息记录已清空");
  } catch (error) {
    toast(`清空失败: ${errorText(error)}`, "error");
  } finally {
    elements.btnClear.disabled = false;
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    toast("复制失败，请手动选中文本", "error");
  }
}

// ── 自动发现 ────────────────────────────────────────────────

async function runDiscovery() {
  if (elements.btnDiscover.disabled) return;
  elements.btnDiscover.disabled = true;
  try {
    const data = await bridge.apiPost("discover", {});
    const result = data?.result || {};
    const groups = Number(result.groups_created) || 0;
    const friends = Number(result.friends_created) || 0;
    const history = Number(result.history_imported) || 0;
    const errors = Array.isArray(result.errors) ? result.errors : [];

    // loadSessions 只在「没有缓存」时才写 state.messages，
    // 不清缓存的话，新导入的历史对当前已缓存的会话永远不可见。
    clearMessageCaches();
    await loadSessions({ selectInitial: false });
    if (errors.length) {
      // 部分来源失败（常见于协议端不支持列表接口），如实提示而不是假装成功
      toast(`发现完成，但有 ${errors.length} 处失败：${errors[0]}`, "error");
      return;
    }
    toast(`新增群 ${groups}、好友 ${friends}，导入历史 ${history} 条`);
  } catch (error) {
    toast(`自动发现失败: ${errorText(error)}`, "error");
  } finally {
    elements.btnDiscover.disabled = false;
  }
}

/** 丢掉消息缓存，强制下一次 loadSessions 重新拉历史（发现/导入后用）。 */
function clearMessageCaches() {
  state.messages.clear();
  state.history.clear();
  state.summaryIds.clear();
}

// ── 新建会话 ────────────────────────────────────────────────

async function openNewSessionDialog() {
  let platforms = [];
  try {
    const data = await bridge.apiGet("platforms");
    platforms = Array.isArray(data?.platforms) ? data.platforms : [];
  } catch (error) {
    toast(`读取平台失败: ${errorText(error)}`, "error");
    return;
  }
  if (!platforms.length) {
    toast("没有可用的消息平台", "error");
    return;
  }

  const form = document.createElement("form");
  form.className = "dialog-form";
  form.addEventListener("submit", (event) => event.preventDefault());

  const platformSelect = document.createElement("select");
  platformSelect.className = "dialog-input";
  for (const platform of platforms) {
    const option = document.createElement("option");
    option.value = platform.id;
    option.textContent = platform.name || platform.id || platform.adapter;
    platformSelect.append(option);
  }

  const typeSelect = document.createElement("select");
  typeSelect.className = "dialog-input";
  for (const [value, label] of [
    ["group", "群聊"],
    ["friend", "私聊"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    typeSelect.append(option);
  }

  const peerInput = document.createElement("input");
  peerInput.className = "dialog-input";
  peerInput.placeholder = "群号或用户 ID（纯数字）";
  peerInput.inputMode = "numeric";

  const peerList = createElement("div", "peer-list");

  const loadPeers = async () => {
    peerList.replaceChildren(createElement("p", "peer-empty", "加载中…"));
    try {
      const data = await bridge.apiGet("peers", {
        platform_id: platformSelect.value,
        type: typeSelect.value,
      });
      const peers = Array.isArray(data?.peers) ? data.peers : [];
      peerList.replaceChildren();
      if (!peers.length) {
        peerList.append(createElement("p", "peer-empty", "协议端不支持拉取列表，请手动填写 ID"));
        return;
      }
      for (const peer of peers.slice(0, 200)) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "peer-item";
        item.textContent = `${peer.name} (${peer.id})`;
        item.addEventListener("click", () => {
          peerInput.value = peer.id;
          peerInput.focus();
        });
        peerList.append(item);
      }
    } catch (error) {
      peerList.replaceChildren(createElement("p", "peer-empty", `拉取失败: ${errorText(error)}`));
    }
  };
  platformSelect.addEventListener("change", loadPeers);
  typeSelect.addEventListener("change", loadPeers);

  const field = (label, control) => {
    const wrapper = document.createElement("label");
    wrapper.className = "dialog-field";
    wrapper.append(createElement("span", "dialog-label", label), control);
    return wrapper;
  };
  form.append(field("平台", platformSelect), field("类型", typeSelect), field("会话 ID", peerInput));

  const confirmed = await formDialog({
    title: "新建会话",
    description: "可以主动发起一个还没产生过消息的群聊或私聊。",
    content: form,
    extra: peerList,
    confirmText: "创建",
    onOpen: loadPeers,
  });
  if (!confirmed) return;

  const peerId = peerInput.value.trim();
  if (!/^\d+$/.test(peerId)) {
    toast("会话 ID 必须是纯数字", "error");
    return;
  }

  try {
    const data = await bridge.apiPost("open", {
      platform_id: platformSelect.value,
      peer_id: peerId,
      type: typeSelect.value,
    });
    const sessionId = data?.session?.id;
    if (sessionId) {
      await loadSessions({ selectInitial: false });
      if (state.sessions.has(sessionId)) selectSession(sessionId);
      toast("会话已就绪");
    }
  } catch (error) {
    toast(`创建失败: ${errorText(error)}`, "error");
  }
}

// ── 流式连接 ────────────────────────────────────────────────

function connectStream() {
  const catchupAfter = state.streamStarted ? maxKnownMessageId() : null;
  state.streamStarted = true;
  disposeStream();
  state.streamCatchupAfter = catchupAfter;
  const generation = state.streamGeneration;
  const sessionId = state.currentSessionId;
  const endpoint = `stream?session_id=${encodeURIComponent(sessionId)}`;
  setStatus(state.polling ? "轮询连接" : "正在连接", "loading");
  if (typeof bridge?.subscribeSSE === "function") {
    connectBridgeSSE(generation, endpoint, sessionId);
  } else {
    connectNativeSSE(generation, endpoint, sessionId);
  }
}

function streamOpened(generation, sessionId) {
  if (generation !== state.streamGeneration) return;
  state.connected = true;
  state.lastStreamAt = Date.now();
  stopPolling();
  setStatus("实时连接", "connected");
  // 首次连接的历史由 loadSessions 载入，不需要再补；只有重连/切换才有缺口。
  const catchupAfter = state.streamCatchupAfter;
  if (catchupAfter == null) return;
  catchUpStream(generation, catchupAfter);
  // 先订阅再补齐：/updates 覆盖切换窗口，/history 把后台期间只收到摘要的消息补全。
  if (sessionId) {
    const knownLastId = Number(state.sessions.get(sessionId)?.lastMessage?.id || 0);
    bridge.apiGet("history", { session_id: sessionId }).then((data) => {
      if (generation !== state.streamGeneration) return;
      const historical = [];
      for (const raw of data?.messages || []) {
        if (Number(raw.id) > knownLastId) handleIncomingMessage(raw);
        else {
          historical.push(normalizeMessage(raw, sessionId));
          state.summaryIds.delete(`${sessionId}:${raw.id}`);
        }
      }
      state.messages.set(sessionId, mergeMessages(state.messages.get(sessionId) || [], historical));
      renderMessages();
    }).catch((error) => {
      if (generation !== state.streamGeneration) return;
      console.warn("重连消息补齐失败:", error);
      state.connected = false;
      startPolling();
    });
  }
}

async function catchUpStream(generation, cursor) {
  try {
    while (generation === state.streamGeneration) {
      const data = await bridge.apiGet("updates", { after_id: String(cursor), limit: String(POLL_MESSAGE_LIMIT) });
      if (generation !== state.streamGeneration) return;
      for (const raw of data?.messages || []) handleIncomingMessage(raw);
      const next = Number(data?.last_id);
      if (!data?.has_more || !Number.isFinite(next) || next <= cursor) return;
      cursor = next;
    }
  } catch (error) {
    if (generation !== state.streamGeneration) return;
    console.warn("SSE 切换补齐失败:", error);
    state.connected = false;
    startPolling();
  }
}

async function connectBridgeSSE(generation, endpoint, sessionId) {
  let subscription;
  try {
    subscription = bridge.subscribeSSE(endpoint, {
      onOpen() { streamOpened(generation, sessionId); },
      onMessage(event) {
        if (generation !== state.streamGeneration) return;
        state.lastStreamAt = Date.now();
        handleStreamEvent(event.eventType, event.parsed ?? safeParse(event.raw));
      },
      onError() {
        if (generation !== state.streamGeneration) return;
        state.connected = false;
        startPolling();
      },
    });
    const id = await withTimeout(subscription, STREAM_OPEN_TIMEOUT_MS);
    if (generation !== state.streamGeneration) {
      Promise.resolve(bridge.unsubscribeSSE?.(id)).catch(() => {});
      return;
    }
    state.stream = { kind: "bridge", id };
  } catch (error) {
    Promise.resolve(subscription).then((id) => {
      if (id != null) return bridge.unsubscribeSSE?.(id);
    }).catch(() => {});
    if (generation !== state.streamGeneration) return;
    console.warn("bridge SSE 未就绪，退回原生 EventSource:", error);
    // 让超时订阅的所有迟到回调失效。
    const next = ++state.streamGeneration;
    state.connected = false;
    startPolling();
    connectNativeSSE(next, endpoint, sessionId);
  }
}

/** 给可能永不 resolve 的 Promise 加超时，超时后按失败处理。 */
function withTimeout(promise, ms, message = "操作超时") {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), ms);
    Promise.resolve(promise).then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function connectNativeSSE(generation, endpoint, sessionId) {
  try {
    const source = new EventSource(`/api/plug/${PLUGIN_NAME}/${endpoint}`, { withCredentials: true });
    state.stream = { kind: "native", source };

    const timer = window.setTimeout(() => {
      if (generation !== state.streamGeneration || state.connected) return;
      source.close();
      startPolling();
    }, STREAM_OPEN_TIMEOUT_MS);
    state.stream.timer = timer;
    source.addEventListener("connected", () => {
      if (generation !== state.streamGeneration) return;
      window.clearTimeout(timer);
      streamOpened(generation, sessionId);
    });
    // 心跳只用来证明连接还活着，不触发任何渲染
    source.addEventListener("ping", () => {
      if (generation !== state.streamGeneration) return;
      state.lastStreamAt = Date.now();
    });
    for (const type of [
      "message",
      "message_summary",
      "session",
      "session_updated",
      "session_removed",
      "message_removed",
      "cleared",
      "sessions_discovered",
    ]) {
      source.addEventListener(type, (event) => {
        if (generation !== state.streamGeneration) return;
        state.lastStreamAt = Date.now();
        handleStreamEvent(type, safeParse(event.data));
      });
    }
    source.onerror = () => {
      if (generation !== state.streamGeneration) return;
      if (state.connected) setStatus("正在重连", "loading");
      state.connected = false;
      startPolling();
    };
  } catch (error) {
    console.warn("SSE 不可用:", error);
    state.connected = false;
    startPolling();
  }
}

function disposeStream() {
  state.streamGeneration += 1;
  state.connected = false;
  state.lastStreamAt = 0;
  if (!state.stream) return;
  window.clearTimeout(state.stream.timer);
  if (state.stream.kind === "bridge" && typeof bridge?.unsubscribeSSE === "function") {
    Promise.resolve(bridge.unsubscribeSSE(state.stream.id)).catch(() => {});
  } else if (state.stream.source) {
    state.stream.source.close();
  }
  state.stream = null;
  state.lastStreamAt = 0;
}

/**
 * SSE 假死自愈。
 *
 * 插件重载 / 服务重启会掐断流，但 bridge 收到 `sse_state: closed` 时只转发
 * open/error——iframe 侧完全无感，`state.connected` 一直是 true，也不降级轮询，
 * 于是界面要手动刷新才会再收到消息。后端每 15 秒发一个 ping，
 * 这里超过 45 秒没收到任何帧就主动重连。
 */
function checkStreamHealth() {
  // ① SSE 已连上但心跳假死：主动重连
  if (state.stream && state.connected && state.lastStreamAt) {
    if (Date.now() - state.lastStreamAt < STREAM_STALE_MS) return;
    console.warn("SSE 心跳超时，主动重连");
    setStatus("正在重连", "loading");
    state.connected = false;
    startPolling();
    connectStream();
    return;
  }
  // ② 正在轮询降级：每隔 SSE_RETRY_MS 再试一次 SSE。
  // 不这样做的话，一旦降级就永远停在轮询——SSE 恢复（插件重载、临时断网结束）
  // 也回不来，违背「尽量走 SSE」。重试期间状态保持「轮询连接」，成功后自然切回。
  if (state.polling && !state.connected) {
    const now = Date.now();
    if (!state.lastSseRetryAt) {
      state.lastSseRetryAt = now;
      return;
    }
    if (now - state.lastSseRetryAt < SSE_RETRY_MS) return;
    state.lastSseRetryAt = now;
    connectStream();
  }
}

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch (error) {
    console.error("SSE 负载解析失败:", error);
    return null;
  }
}

function handleStreamEvent(type, payload) {
  switch (type) {
    case "message_summary":
      handleIncomingMessage(payload, { summary: true });
      break;
    case "message":
      handleIncomingMessage(payload);
      break;
    case "session":
      handleSessionEvent(payload);
      break;
    case "session_updated":
      handleSessionUpdated(payload);
      break;
    case "session_removed":
      handleSessionRemoved(payload);
      break;
    case "message_removed":
      removeMessageLocally(String(payload?.session_id || ""), String(payload?.id ?? ""));
      break;
    case "cleared":
      handleCleared(payload);
      break;
    case "sessions_discovered":
      // 启动时的自动发现在后台跑完后会广播这个事件，其它窗口要跟着刷新；
      // 自己点按钮触发的（按钮禁用中）已经在 runDiscovery 里刷新过了。
      if (!elements.btnDiscover.disabled) {
        clearMessageCaches();
        loadSessions({ selectInitial: false });
      }
      break;
    default:
      break;
  }
}

function handleIncomingMessage(raw, { summary = false } = {}) {
  const sessionId = String(raw?.session_id || "");
  if (!sessionId) return;

  let session = state.sessions.get(sessionId);
  if (!session) {
    // 未知会话：先把会话列表拉回来，避免出现只有消息没有会话的孤岛
    loadSessions({ selectInitial: false }).catch(() => {});
    session = normalizeSession({ id: sessionId, peer_id: sessionId, peer_name: sessionId });
    state.sessions.set(sessionId, session);
    state.messages.set(sessionId, []);
    state.history.set(sessionId, { hasMore: false, nextCursor: null });
    state.unread.set(sessionId, 0);
  }

  const message = normalizeMessage(raw, sessionId);
  const messages = state.messages.get(sessionId) || [];
  if (message.id && messages.some((item) => item.id === message.id)) return;

  const summaryKey = `${sessionId}:${message.id}`;
  const counted = state.summaryIds.has(summaryKey);
  if (summary && counted) return;
  if (summary) state.summaryIds.add(summaryKey);
  else state.summaryIds.delete(summaryKey);
  if (!summary) state.messages.set(sessionId, mergeMessages(messages, [message]));
  if (counted) {
    if (!summary && sessionId === state.currentSessionId) renderMessages();
    return;
  }
  const isCurrent = sessionId === state.currentSessionId && !summary;
  const shouldFollow = isCurrent && isNearBottomOfMessageList();
  const previousTop = elements.messages.scrollTop;

  session.messageCount += 1;
  session.lastMessage = message;
  session.lastActivity = message.timestamp;
  mergeIncomingParticipant(session, message);

  if (isCurrent) {
    elements.detailCount.textContent = String(session.messageCount);
    renderParticipants(session);
    renderMessages({ scrollToBottom: shouldFollow });
    if (shouldFollow) {
      markCurrentSessionRead();
    } else {
      elements.messages.scrollTop = previousTop;
      state.unread.set(sessionId, (state.unread.get(sessionId) || 0) + 1);
      state.newMessageCount += 1;
      state.lastNewMessageSender = message.senderName;
      showNewMessagePill(message.senderName);
    }
  } else {
    state.unread.set(sessionId, (state.unread.get(sessionId) || 0) + 1);
  }

  renderSessionList();
}

function mergeIncomingParticipant(session, message) {
  if (!message.senderId || message.type === "sent") return;
  const existing = session.participants.find((person) => person.id === message.senderId);
  if (existing) {
    existing.name = message.senderName || existing.name;
    existing.messageCount += 1;
    existing.lastSeen = message.timestamp;
    return;
  }
  session.participants.push({
    id: message.senderId,
    name: message.senderName,
    messageCount: 1,
    lastSeen: message.timestamp,
  });
}

function handleSessionEvent(raw) {
  const session = normalizeSession(raw);
  if (!session.id || state.sessions.has(session.id)) return;
  state.sessions.set(session.id, session);
  state.messages.set(session.id, []);
  state.history.set(session.id, { hasMore: false, nextCursor: null });
  state.unread.set(session.id, 0);
  renderSessionList();
  if (!state.currentSessionId) selectSession(session.id);
}

function handleSessionUpdated(raw) {
  const session = state.sessions.get(String(raw?.id || ""));
  if (!session) return;
  session.pinned = Boolean(raw.pinned);
  if (raw.peer_name) session.peerName = String(raw.peer_name);
  renderSessionList();
  if (session.id === state.currentSessionId) updateConversationHeader(session);
}

function clearSummaryIds(sessionId) {
  for (const key of state.summaryIds) {
    if (key.startsWith(`${sessionId}:`)) state.summaryIds.delete(key);
  }
}

function handleSessionRemoved(payload) {
  const sessionId = String(payload?.id || "");
  if (!sessionId) return;
  clearSummaryIds(sessionId);
  state.sessions.delete(sessionId);
  state.messages.delete(sessionId);
  state.history.delete(sessionId);
  state.unread.delete(sessionId);
  renderSessionList();
  if (state.currentSessionId === sessionId) resetConversation();
}

function handleCleared(payload) {
  const sessionId = String(payload?.session_id || "");
  if (!sessionId) {
    for (const id of state.sessions.keys()) handleCleared({ session_id: id });
    return;
  }
  clearSummaryIds(sessionId);
  state.unread.set(sessionId, 0);
  state.messages.set(sessionId, []);
  state.history.set(sessionId, { hasMore: false, nextCursor: null });
  const session = state.sessions.get(sessionId);
  if (session) {
    session.messageCount = 0;
    session.lastMessage = null;
    session.participants = [];
  }
  if (sessionId === state.currentSessionId) {
    renderMessages({ scrollToBottom: true });
    renderParticipants(session || { participants: [] });
    elements.detailCount.textContent = "0";
  }
  renderSessionList();
}

function showNewMessagePill(senderName) {
  const count = state.newMessageCount;
  const sender = senderName || state.lastNewMessageSender;
  elements.newMessageText.textContent =
    count > 1 ? `${count} 条新消息` : sender ? `${sender} 发来新消息` : "有新消息";
  elements.newMessagePill.hidden = false;
}

function hideNewMessagePill() {
  elements.newMessagePill.hidden = true;
}

// ── 轮询降级 ────────────────────────────────────────────────

function startPolling() {
  if (state.connected || state.polling) return;
  state.polling = true;
  // 游标只从「已知的最大 id」起跳，之后只由轮询结果推进：
  // 本地发送 / SSE 到达的消息不推进游标，否则可能跳过并发插入的那几条。
  state.updatesCursor = maxKnownMessageId();
  setStatus("轮询连接", "loading");
  state.pollTimer = window.setInterval(pollNewMessages, POLL_INTERVAL_MS);
  pollNewMessages();
}

function stopPolling() {
  state.polling = false;
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/** 当前已知的所有消息里最大的 id（含会话摘要里的最后一条）。 */
function maxKnownMessageId() {
  let max = 0;
  const consider = (value) => {
    const id = Number(value);
    if (Number.isFinite(id) && id > max) max = id;
  };
  for (const session of state.sessions.values()) consider(session.lastMessage?.id);
  for (const list of state.messages.values()) {
    for (const message of list) consider(message.id);
  }
  return max;
}

/**
 * 轮询降级：一次请求带回**所有会话**的新消息。
 *
 * 旧实现只轮询当前会话、且用 timestamp 当游标——别的会话有新消息时
 * 列表和未读数都不会动，同一秒的多条消息还可能被漏掉。
 */
async function pollNewMessages(drain = 0) {
  if (!state.polling || state.connected) return;
  try {
    const data = await bridge.apiGet("updates", {
      after_id: String(state.updatesCursor || 0),
      limit: String(POLL_MESSAGE_LIMIT),
    });
    const messages = Array.isArray(data?.messages) ? data.messages : [];
    for (const raw of messages) handleIncomingMessage(raw);
    const lastId = Number(data?.last_id);
    if (Number.isFinite(lastId) && lastId > state.updatesCursor) {
      state.updatesCursor = lastId;
    }
    if (state.polling && !state.connected) setStatus("轮询连接", "loading");
    if (data?.has_more && drain < POLL_MAX_DRAIN) {
      window.setTimeout(() => pollNewMessages(drain + 1), 50);
    }
  } catch (error) {
    console.warn("轮询失败:", error);
    if (state.polling && !state.connected) setStatus("连接异常", "error");
  }
}

// ── 面板 ────────────────────────────────────────────────────

function toggleSidebar(open) {
  document.body.classList.toggle("sidebar-open", Boolean(open));
}

function toggleDetails(open) {
  state.detailsOpen = Boolean(open);
  elements.detailsPanel.classList.toggle("open", state.detailsOpen);
  elements.btnDetails.setAttribute("aria-expanded", state.detailsOpen ? "true" : "false");
}

function openLightbox(src, source = "") {
  const original = String(source || "");
  const isLink = /^https?:\/\//i.test(original);
  state.lightboxSource = isLink ? original : "";

  elements.lightboxImage.src = src;
  elements.lightbox.hidden = false;

  // 图片走 /media 代理，浏览器地址栏拿不到原图地址，这里显式给出来
  elements.lightboxCaption.hidden = !original;
  elements.lightboxLink.textContent = isLink ? original : "本地上传的图片";
  elements.lightboxLink.title = original;
  elements.btnCopyImageLink.hidden = !isLink;
  elements.lightboxOpen.hidden = !isLink;
  elements.lightboxOpen.href = isLink ? original : "#";
}

function closeLightbox() {
  if (elements.lightbox.hidden) return;
  elements.lightbox.hidden = true;
  elements.lightboxImage.src = "";
  state.lightboxSource = "";
  elements.lightboxCaption.hidden = true;
}

function setStatus(text, mode = "default") {
  elements.statusText.textContent = text;
  elements.status.classList.toggle("connected", mode === "connected");
  elements.status.classList.toggle("error", mode === "error");
}

function toast(message, type = "default", duration = 3600) {
  const node = createElement("div", `toast${type === "error" ? " error" : ""}`, message);
  elements.toastRegion.append(node);
  window.setTimeout(() => node.remove(), duration);
}

function resizeComposer() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 130)}px`;
}

// ── 弹窗（替代 window.confirm） ─────────────────────────────
// 插件视图 iframe 的 sandbox 没有 allow-modals，原生 confirm 会被静默忽略。

function confirmDialog({ title, description, confirmText = "确定", danger = false }) {
  const body = createElement("p", "dialog-description", description || "");
  return formDialog({ title, content: body, confirmText, danger });
}

function formDialog({
  title,
  description = "",
  content = null,
  extra = null,
  confirmText = "确定",
  danger = false,
  onOpen = null,
}) {
  return new Promise((resolve) => {
    const overlay = createElement("div", "dialog-overlay");
    const panel = createElement("div", "dialog-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");

    panel.append(createElement("h2", "dialog-title", title));
    if (description) panel.append(createElement("p", "dialog-description", description));
    if (content) panel.append(content);
    if (extra) panel.append(extra);

    const actions = createElement("div", "dialog-actions");
    const cancel = createElement("button", "button button-ghost", "取消");
    cancel.type = "button";
    const confirm = createElement(
      "button",
      `button ${danger ? "button-danger" : "button-primary"}`,
      confirmText,
    );
    confirm.type = "button";
    actions.append(cancel, confirm);
    panel.append(actions);
    overlay.append(panel);
    elements.dialogRegion.append(overlay);

    let settled = false;
    const close = (value) => {
      if (settled) return;
      settled = true;
      overlay.remove();
      resolve(value);
    };
    cancel.addEventListener("click", () => close(null));
    confirm.addEventListener("click", () => close(true));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(null);
    });
    if (onOpen) onOpen();
    confirm.focus();
  });
}

function infoDialog({ title, content = null }) {
  const overlay = createElement("div", "dialog-overlay");
  const panel = createElement("div", "dialog-panel dialog-panel-wide");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");

  panel.append(createElement("h2", "dialog-title", title));
  const body = createElement("div", "dialog-body");
  if (content) body.append(content);
  panel.append(body);

  const actions = createElement("div", "dialog-actions");
  const closeButton = createElement("button", "button button-primary", "关闭");
  closeButton.type = "button";
  actions.append(closeButton);
  panel.append(actions);
  overlay.append(panel);

  const close = () => overlay.remove();
  closeButton.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  elements.dialogRegion.append(overlay);
  closeButton.focus();
}

function closeTopDialog() {
  const dialogs = elements.dialogRegion.querySelectorAll(".dialog-overlay");
  const last = dialogs[dialogs.length - 1];
  if (last) last.remove();
}

// ── 主题与紧凑模式 ──────────────────────────────────────────

function isCompactMode() {
  return document.documentElement.dataset.compact === "true";
}

function toggleCompactMode() {
  applyCompactMode(!isCompactMode());
}

function applyCompactMode(compact, { persist = true } = {}) {
  document.documentElement.dataset.compact = String(Boolean(compact));
  if (persist) {
    try {
      window.localStorage.setItem(COMPACT_KEY, String(Boolean(compact)));
    } catch {
      // 存储不可用时紧凑模式仍然生效
    }
  }
  syncCompactControl();
}

function syncCompactControl() {
  const compact = isCompactMode();
  const label = compact ? "退出紧凑模式" : "开启紧凑模式";
  elements.btnCompact.setAttribute("aria-label", label);
  elements.btnCompact.setAttribute("aria-pressed", compact ? "true" : "false");
  elements.btnCompact.title = label;
}

function readStoredCompact() {
  try {
    const value = window.localStorage.getItem(COMPACT_KEY);
    return value === "true" || value === "false" ? value : null;
  } catch {
    return null;
  }
}

// ── 图片加载开关 ────────────────────────────────────────────

function toggleLoadImages() {
  applyLoadImages(!state.loadImages);
}

/**
 * 切换是否自动请求图片。
 *
 * 关掉时消息里的图片只渲染成占位标签，点一下才加载那一张——
 * 群相册、表情包刷屏时不会一次性拉几十个请求，也避免走 /media 代理。
 */
function applyLoadImages(enabled, { persist = true } = {}) {
  const next = Boolean(enabled);
  if (state.loadImages === next) {
    syncImagesControl();
    return;
  }
  state.loadImages = next;
  // 全局关掉 = 全部折叠：手动点开过的那些也一起收回去
  if (!next) state.loadedImages.clear();
  if (persist) {
    try {
      window.localStorage.setItem(IMAGES_KEY, String(next));
    } catch {
      // 存储不可用时开关仍然本次生效
    }
  }
  syncImagesControl();
  // 已渲染的图片/占位要跟着变，切会话时整树重建，这里同样强制重建
  state.renderSessionId = "";
  if (state.currentSessionId) renderMessages();
}

function syncImagesControl() {
  if (!elements.btnImages) return;
  const enabled = state.loadImages;
  const label = enabled ? "关闭自动加载图片" : "自动加载图片";
  elements.btnImages.setAttribute("aria-label", label);
  elements.btnImages.setAttribute("aria-pressed", enabled ? "true" : "false");
  elements.btnImages.title = enabled ? "图片自动加载（点击改为占位）" : "图片已折叠（点击自动加载）";
}

function readStoredLoadImages() {
  try {
    const value = window.localStorage.getItem(IMAGES_KEY);
    return value === "true" || value === "false" ? value === "true" : null;
  } catch {
    return null;
  }
}

function getActiveTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function toggleTheme() {
  applyTheme(getActiveTheme() === "light" ? "dark" : "light");
}

function applyTheme(theme, { persist = true } = {}) {
  const nextTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = nextTheme;
  if (persist) {
    try {
      window.localStorage.setItem(THEME_KEY, nextTheme);
    } catch {
      // 切换仍然生效
    }
  }
  syncThemeControl();
}

function syncThemeControl() {
  const isLight = getActiveTheme() === "light";
  const label = isLight ? "切换到暗色主题" : "切换到亮色主题";
  elements.btnTheme.setAttribute("aria-label", label);
  elements.btnTheme.setAttribute("aria-pressed", isLight ? "true" : "false");
  elements.btnTheme.title = label;
}

function readStoredTheme() {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "";
  } catch {
    return "";
  }
}

/** 用户没手动选过主题时，跟随 AstrBot WebUI 的主题。 */
function syncThemeFromBridge() {
  if (readStoredTheme()) return;
  const isDark = state.context?.isDark;
  if (typeof isDark !== "boolean") return;
  applyTheme(isDark ? "dark" : "light", { persist: false });
}

// ── 工具 ────────────────────────────────────────────────────

function createElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

/** 远程图片走插件代理，规避防盗链与混合内容；data/blob 直出。 */
function mediaUrl(source) {
  const text = String(source || "");
  if (!text) return "";
  if (text.startsWith("data:") || text.startsWith("blob:")) return text;
  if (/^https?:\/\//i.test(text)) {
    return `/api/plug/${PLUGIN_NAME}/media?url=${encodeURIComponent(text)}`;
  }
  return text;
}

function readStoredSession() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    // iframe 沙箱里可能不可用，回退到列表第一个会话
    return "";
  }
}

function rememberSession(sessionId) {
  try {
    window.localStorage.setItem(STORAGE_KEY, sessionId);
  } catch {
    // 同上，静默忽略
  }
}

async function apiDelete(endpoint) {
  if (typeof bridge.apiFetch === "function") {
    return bridge.apiFetch(endpoint, { method: "DELETE" });
  }
  // 老 bridge 没有通用请求方法，退回 POST（后端同时接受两种方法）
  return bridge.apiPost(`${endpoint}?method=DELETE`, {});
}
