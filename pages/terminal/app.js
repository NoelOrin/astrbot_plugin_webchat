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
  compactId,
  compareSessions,
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
  messageText,
  normalizeHistory,
  normalizeMessage,
  normalizeSession,
  planMessages,
  diffPlan,
  splitHighlight,
  stripMentionMarkup,
  unreadLabel,
} from "./webchat-lib.js";

const PLUGIN_NAME = "astrbot_plugin_webchat";
const STORAGE_KEY = `${PLUGIN_NAME}_last_session`;
const THEME_KEY = `${PLUGIN_NAME}_theme`;
const COMPACT_KEY = `${PLUGIN_NAME}_compact`;
const POLL_INTERVAL_MS = 2500;
/** 轮询一次最多带回多少条新消息（与后端 MAX_PAGE_SIZE 对齐）。 */
const POLL_MESSAGE_LIMIT = 200;
/** 一次轮询取满时最多连续补几次，避免消息洪峰时无限递归。 */
const POLL_MAX_DRAIN = 5;
const HISTORY_LIMIT = HISTORY_PAGE_SIZE;
/** SSE 心跳：后端每 15 秒发一个 ping，超过 45 秒没收到就认为连接已死。 */
const STREAM_STALE_MS = 45000;
const STREAM_CHECK_MS = 20000;
/** 小窗自动紧凑阈值：必须与 style.css 顶部的 --compact 保持一致。 */
const COMPACT_MEDIA = "(max-width: 900px), (max-height: 720px)";

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

const elements = {
  btnCompact: document.getElementById("btn-compact"),
  btnTheme: document.getElementById("btn-theme"),
  messages: document.getElementById("messages"),
  messageContent: document.getElementById("message-content"),
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
};

const state = {
  sessions: new Map(),
  messages: new Map(),
  history: new Map(),
  unread: new Map(),
  currentSessionId: "",
  stream: null,
  pollTimer: null,
  polling: false,
  connected: false,
  /** 最近一次收到任何 SSE 帧（含 ping）的时间，用于判断连接是否假死。 */
  lastStreamAt: 0,
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
};

bindEvents();
syncThemeControl();
syncCompactControl();
boot();

async function boot() {
  setStatus("正在连接", "loading");
  if (!bridge || typeof bridge.apiGet !== "function") {
    setStatus("Bridge 不可用", "error");
    toast("未检测到 AstrBot 页面 Bridge", "error");
    return;
  }
  try {
    state.context = await bridge.ready().catch(() => null);
    syncThemeFromBridge();
    await loadSessions({ selectInitial: true });
    connectStream();
    window.setInterval(checkStreamHealth, STREAM_CHECK_MS);
  } catch (error) {
    console.error("初始化失败:", error);
    setStatus("加载失败", "error");
    toast(`初始化失败: ${errorText(error)}`, "error");
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
  });

  elements.btnClear.addEventListener("click", clearMessages);
  elements.btnCompact.addEventListener("click", toggleCompactMode);
  elements.btnTheme.addEventListener("click", toggleTheme);
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
  elements.messages.addEventListener("scroll", handleMessageScroll, { passive: true });

  elements.messageSearch.addEventListener("input", debounce(onSearchInput, 260));
  elements.btnDiscover.addEventListener("click", runDiscovery);
  elements.btnOpenSession.addEventListener("click", openNewSessionDialog);
  elements.btnPin.addEventListener("click", togglePin);
  elements.btnExport.addEventListener("click", exportSession);
  elements.btnAttach.addEventListener("click", () => elements.attachmentInput.click());
  elements.attachmentInput.addEventListener("change", onAttachmentPicked);
  elements.lightbox.addEventListener("click", closeLightbox);

  // 输入框内粘贴图片直接变成待发送附件
  elements.input.addEventListener("paste", onInputPaste);

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      toggleSidebar(false);
      toggleDetails(false);
      closeLightbox();
      closeTopDialog();
    }
  });

  window.addEventListener("beforeunload", disposeStream);

  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (event) => {
    if (!readStoredTheme()) applyTheme(event.matches ? "light" : "dark", { persist: false });
  });

  window.matchMedia(COMPACT_MEDIA).addEventListener("change", (event) => {
    if (readStoredCompact() === null) applyCompactMode(event.matches, { persist: false });
  });

  if (typeof bridge?.onContext === "function") {
    bridge.onContext((context) => {
      state.context = context;
      syncThemeFromBridge();
    });
  }
}

// ── 会话加载 ────────────────────────────────────────────────

async function loadSessions({ selectInitial = false } = {}) {
  elements.btnRefresh.disabled = true;
  try {
    const data = await bridge.apiGet("sessions", {
      with_history: "1",
      history_limit: String(HISTORY_LIMIT),
    });
    const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
    const nextSessions = new Map();

    for (const rawSession of sessions) {
      const session = normalizeSession(rawSession);
      if (!session.id) continue;
      nextSessions.set(session.id, session);

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

    // 清掉已删除会话的缓存
    for (const sessionId of [...state.sessions.keys()]) {
      if (!nextSessions.has(sessionId)) {
        state.sessions.delete(sessionId);
        state.messages.delete(sessionId);
        state.history.delete(sessionId);
        state.unread.delete(sessionId);
      }
    }

    state.sessions = nextSessions;
    const savedSession = readStoredSession();
    const currentStillExists = state.currentSessionId && nextSessions.has(state.currentSessionId);
    const target = currentStillExists
      ? state.currentSessionId
      : savedSession && nextSessions.has(savedSession)
        ? savedSession
        : (sortedSessions()[0]?.id ?? "");

    renderSessionList();

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
}

// ── 会话切换 ────────────────────────────────────────────────

function selectSession(sessionId, { scrollToBottom: shouldScroll = true, closeSidebar = true } = {}) {
  const session = state.sessions.get(sessionId);
  if (!session) return;

  const changed = state.currentSessionId !== sessionId;
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
  resizeComposer();
  updateSendState();

  renderSessionList();
  updateConversationHeader(session);
  renderParticipants(session);
  renderMessages({ scrollToBottom: shouldScroll || changed });

  if (closeSidebar) toggleSidebar(false);
  if (!state.connected && sessionId) startPolling();
}

function resetConversation() {
  state.currentSessionId = "";
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
  elements.messageContent.replaceChildren();
  elements.loadMore.hidden = true;
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
    elements.messageContent.replaceChildren();
    state.renderPlan = [];
    state.renderSessionId = "";
    return;
  }

  const nextPlan = planMessages(messages, sessionId);

  if (state.renderSessionId !== sessionId) {
    // 切换会话：DOM 里是上一个会话的节点，diff 无法推断（空计划会被判成 append），
    // 必须直接清空后整树重建，否则旧会话的消息会残留在新会话里。
    const fragment = document.createDocumentFragment();
    for (const item of nextPlan) fragment.append(buildPlanNode(item, session));
    elements.messageContent.replaceChildren(fragment);
  } else {
    applyPlanDiff(diffPlan(state.renderPlan, nextPlan), state.renderPlan, nextPlan, session);
  }

  state.renderPlan = nextPlan;
  state.renderSessionId = sessionId;

  if (shouldScroll) {
    requestAnimationFrame(() => {
      elements.messages.scrollTop = elements.messages.scrollHeight;
      markCurrentSessionRead();
    });
  } else {
    elements.messages.scrollTop = previousTop;
  }
}

/** 按 diff 结果做最小 DOM 变更，而不是每次重建整棵树。 */
function applyPlanDiff(diff, previousPlan, nextPlan, session) {
  const content = elements.messageContent;

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
    `message-row ${isSent ? "sent" : isBot ? "bot" : "received"}${grouped ? " grouped" : ""}`;
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

  if (message.id) {
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const copyButton = createElement("button", "message-action", "复制");
    copyButton.type = "button";
    copyButton.addEventListener("click", () => copyText(messageText(message)));
    const deleteButton = createElement("button", "message-action danger", "删除");
    deleteButton.type = "button";
    deleteButton.addEventListener("click", () => deleteMessage(message));
    actions.append(copyButton, deleteButton);
    stack.append(actions);
  }

  row.append(avatar, stack);
  return row;
}

function appendBubbleContent(bubble, message) {
  const segments = message.segments || [];
  const images = imageSources(message);

  if (images.length) {
    const gallery = createElement("div", "message-images");
    for (const source of images) {
      const image = document.createElement("img");
      image.className = "message-image";
      image.loading = "lazy";
      image.alt = "图片消息";
      image.src = mediaUrl(source);
      image.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(image.src);
      });
      image.addEventListener("error", () => {
        image.replaceWith(createElement("span", "message-media-fallback", "[图片加载失败]"));
      });
      gallery.append(image);
    }
    bubble.append(gallery);
  }

  // 非文本片段（@、表情、语音、文件…）用 chip 展示
  const chips = segments.filter((segment) => segment.type !== "text" && segment.type !== "image");
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
  const fallbackText = visibleText || (images.length ? "" : messageText(message));
  if (fallbackText) bubble.append(createElement("span", "message-text", fallbackText));

  if (message.pending) bubble.append(createElement("span", "message-pending", "发送中…"));
}

function renderSegmentChip(segment) {
  const chip = createElement("span", `segment-chip ${segment.type}`);
  const label =
    segment.text ||
    (segment.type === "image"
      ? "[图片]"
      : segment.type === "file"
        ? `[文件] ${segment.name || ""}`
        : `[${segment.type}]`);
  chip.textContent = String(label).trim();
  return chip;
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

function handleMessageScroll() {
  if (isNearBottomOfMessageList() && (state.unread.get(state.currentSessionId) || 0) > 0) {
    markCurrentSessionRead();
  }
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
    state.messages.set(sessionId, mergeMessages(older, state.messages.get(sessionId) || []));
    state.history.set(sessionId, normalizeHistory(data));
    renderMessages();
    requestAnimationFrame(() => {
      elements.messages.scrollTop = previousTop + (elements.messages.scrollHeight - previousHeight);
    });
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

  const attachments = state.attachments;
  state.sending = true;
  elements.btnSend.disabled = true;
  elements.input.value = "";
  state.attachments = [];
  renderAttachments();
  resizeComposer();

  try {
    const response = await bridge.apiPost("send", {
      session_id: session.id,
      message: text,
      segments: attachments,
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
            segments: [
              ...(text ? [{ type: "text", text }] : []),
              ...attachments,
            ],
            timestamp: Date.now() / 1000,
            pending: true,
          },
          session.id,
        );
    applyOutgoingMessage(message);
  } catch (error) {
    elements.input.value = text;
    state.attachments = attachments;
    renderAttachments();
    resizeComposer();
    toast(`发送失败: ${errorText(error)}`, "error");
    elements.input.focus();
  } finally {
    state.sending = false;
    updateSendState();
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

  // 目标不在当前窗口时，继续向前翻页直到命中或到底
  for (let attempt = 0; attempt < 6 && !target; attempt += 1) {
    const history = state.history.get(sessionId);
    if (!history?.hasMore) break;
    await loadOlderMessages();
    target = selector ? elements.messageContent.querySelector(selector) : null;
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
  const removed = messages.length - next.length;
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
  disposeStream();
  if (typeof bridge?.subscribeSSE === "function") {
    connectBridgeSSE();
  } else {
    connectNativeSSE();
  }
}

async function connectBridgeSSE() {
  try {
    const subscriptionId = await bridge.subscribeSSE("stream", {
      onOpen() {
        state.connected = true;
        state.lastStreamAt = Date.now();
        stopPolling();
        setStatus("实时连接", "connected");
      },
      onMessage(event) {
        state.lastStreamAt = Date.now();
        handleStreamEvent(event.eventType, event.parsed ?? safeParse(event.raw));
      },
      onError() {
        if (state.connected) setStatus("正在重连", "loading");
        state.connected = false;
        startPolling();
      },
    });
    state.stream = { kind: "bridge", id: subscriptionId };
  } catch (error) {
    console.warn("bridge SSE 不可用，退回原生 EventSource:", error);
    connectNativeSSE();
  }
}

function connectNativeSSE() {
  try {
    const source = new EventSource(`/api/plug/${PLUGIN_NAME}/stream`, { withCredentials: true });
    state.stream = { kind: "native", source };

    source.addEventListener("connected", () => {
      state.connected = true;
      state.lastStreamAt = Date.now();
      stopPolling();
      setStatus("实时连接", "connected");
    });
    // 心跳只用来证明连接还活着，不触发任何渲染
    source.addEventListener("ping", () => {
      state.lastStreamAt = Date.now();
    });
    for (const type of [
      "message",
      "session",
      "session_updated",
      "session_removed",
      "message_removed",
      "cleared",
      "sessions_discovered",
    ]) {
      source.addEventListener(type, (event) => {
        state.lastStreamAt = Date.now();
        handleStreamEvent(type, safeParse(event.data));
      });
    }
    source.onerror = () => {
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
  if (!state.stream) return;
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
  if (!state.stream || !state.connected || !state.lastStreamAt) return;
  if (Date.now() - state.lastStreamAt < STREAM_STALE_MS) return;
  console.warn("SSE 心跳超时，主动重连");
  setStatus("正在重连", "loading");
  state.connected = false;
  connectStream();
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

function handleIncomingMessage(raw) {
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

  const isCurrent = sessionId === state.currentSessionId;
  const shouldFollow = isCurrent && isNearBottomOfMessageList();
  const previousTop = elements.messages.scrollTop;

  state.messages.set(sessionId, mergeMessages(messages, [message]));
  session.messageCount += 1;
  session.lastMessage = message;
  session.lastActivity = message.timestamp;
  mergeIncomingParticipant(session, message);

  if (isCurrent) {
    elements.detailCount.textContent = String(session.messageCount);
    renderParticipants(session);
    renderMessages();
    if (shouldFollow) {
      requestAnimationFrame(() => {
        scrollToBottom(false);
        markCurrentSessionRead();
      });
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

function handleSessionRemoved(payload) {
  const sessionId = String(payload?.id || "");
  if (!sessionId) return;
  state.sessions.delete(sessionId);
  state.messages.delete(sessionId);
  state.history.delete(sessionId);
  state.unread.delete(sessionId);
  renderSessionList();
  if (state.currentSessionId === sessionId) resetConversation();
}

function handleCleared(payload) {
  const sessionId = String(payload?.session_id || "");
  if (!sessionId) return;
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
    setStatus("轮询连接", "loading");
    if (data?.has_more && drain < POLL_MAX_DRAIN) {
      window.setTimeout(() => pollNewMessages(drain + 1), 50);
    }
  } catch (error) {
    console.warn("轮询失败:", error);
    setStatus("连接异常", "error");
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

function openLightbox(src) {
  elements.lightboxImage.src = src;
  elements.lightbox.hidden = false;
}

function closeLightbox() {
  if (elements.lightbox.hidden) return;
  elements.lightbox.hidden = true;
  elements.lightboxImage.src = "";
}

function setStatus(text, mode = "default") {
  elements.statusText.textContent = text;
  elements.status.classList.toggle("connected", mode === "connected");
  elements.status.classList.toggle("error", mode === "error");
}

function toast(message, type = "default") {
  const node = createElement("div", `toast${type === "error" ? " error" : ""}`, message);
  elements.toastRegion.append(node);
  window.setTimeout(() => node.remove(), 3600);
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
