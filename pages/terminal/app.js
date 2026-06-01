const bridge = window.AstrBotPluginPage;

const output = document.getElementById("output");
const input = document.getElementById("input");
const btnSend = document.getElementById("btn-send");
const btnClear = document.getElementById("btn-clear");
const btnRefresh = document.getElementById("btn-refresh");
const sessionSelect = document.getElementById("session-select");
const statusEl = document.getElementById("status");

const PLUGIN_NAME = "astrbot_plugin_webchat";
const STORAGE_KEY = `${PLUGIN_NAME}_last_session`;

let currentSession = "";
let polling = false;
let pollTimer = null;
let sseSource = null;

// 所有会话消息缓存  session_id -> [messages]
const sessionMessages = {};

// ── 初始化 ─────────────────────────────────────────────────────────

await bridge.ready();
appendSystem("webchat:// 终端已就绪");
await loadSessionsWithHistory();
connectSSE();
input.focus();

// ── 事件绑定 ───────────────────────────────────────────────────────

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

btnSend.addEventListener("click", sendMessage);
btnClear.addEventListener("click", clearMessages);
btnRefresh.addEventListener("click", loadSessionsWithHistory);

sessionSelect.addEventListener("change", () => {
  currentSession = sessionSelect.value;
  if (currentSession) {
    try { localStorage.setItem(STORAGE_KEY, currentSession); } catch {}
    renderSession(currentSession);
    const label = sessionSelect.options[sessionSelect.selectedIndex].text;
    appendSystem(`已切换到: ${label}`);
    startPolling();
  } else {
    appendSystem("请选择一个会话");
    stopPolling();
  }
});

// ── 加载会话 + 历史 ─────────────────────────────────────────────────

async function loadSessionsWithHistory() {
  try {
    const data = await bridge.apiGet("sessions", { with_history: "1" });
    const sessions = data.sessions || [];

    // 保留当前选中
    const prev = sessionSelect.value;
    sessionSelect.innerHTML = '<option value="">-- 选择会话 --</option>';

    for (const s of sessions) {
      // 缓存历史消息
      if (s.messages) {
        sessionMessages[s.id] = s.messages;
      }
      appendSessionOption(s);
    }

    // 恢复上次选中的会话，或自动选中第一个
    const saved = (() => {
      try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
    })();
    const target = (saved && sessions.some((s) => s.id === saved)) ? saved
                 : sessions.length > 0 ? sessions[0].id
                 : "";
    if (target) {
      sessionSelect.value = target;
      sessionSelect.dispatchEvent(new Event("change"));
    }

    setStatus(`● ${sessions.length} 个会话`, false);
  } catch (err) {
    setStatus("● 加载失败", true);
    console.error("加载会话失败:", err);
  }
}

function appendSessionOption(s) {
  const opt = document.createElement("option");
  opt.value = s.id;
  const tag = s.is_group ? "群聊" : "私聊";
  const name = s.group_name || s.group_id || s.platform;
  opt.textContent = `[${tag}] ${name} (${s.message_count}条)`;
  sessionSelect.appendChild(opt);
}

// ── SSE 实时推送 ────────────────────────────────────────────────────

function connectSSE() {
  disconnectSSE();
  try {
    // 尝试标准 SSE 端点
    sseSource = new EventSource(`/api/plug/${PLUGIN_NAME}/stream`);

    sseSource.addEventListener("connected", () => {
      setStatus(`● 已连接 (SSE)`, false);
      // SSE 连接成功后可以停止轮询
      stopPolling();
    });

    sseSource.addEventListener("message", (e) => {
      try {
        const msg = JSON.parse(e.data);
        handleMessageEvent(msg);
      } catch (err) {
        console.error("SSE 消息解析失败:", err);
      }
    });

    sseSource.addEventListener("session", (e) => {
      try {
        const session = JSON.parse(e.data);
        handleSessionEvent(session);
      } catch (err) {
        console.error("SSE 会话解析失败:", err);
      }
    });

    sseSource.onerror = () => {
      console.warn("SSE 连接失败，回退到轮询");
      disconnectSSE();
      // 回退：如果当前有选中会话，使用轮询
      if (currentSession) startPolling();
    };
  } catch (err) {
    console.warn("SSE 不可用:", err);
  }
}

function disconnectSSE() {
  if (sseSource) {
    sseSource.close();
    sseSource = null;
  }
}

function handleMessageEvent(msg) {
  const sessionId = msg.session_id;
  if (!sessionId) return;

  // 缓存消息
  if (!sessionMessages[sessionId]) {
    sessionMessages[sessionId] = [];
  }
  sessionMessages[sessionId].push(msg);

  // 裁剪到上限
  if (sessionMessages[sessionId].length > 500) {
    sessionMessages[sessionId] = sessionMessages[sessionId].slice(-500);
  }

  // 如果是当前会话，追加到输出
  if (sessionId === currentSession) {
    appendMessage(msg);
    scrollToBottom();
  }

  // 更新下拉列表中的消息数
  updateSessionCount(sessionId, sessionMessages[sessionId].length);
}

function handleSessionEvent(session) {
  // 检查是否已存在
  const existing = sessionSelect.querySelector(`option[value="${session.id}"]`);
  if (existing) return;

  // 新会话：缓存并添加下拉选项
  sessionMessages[session.id] = [];
  appendSessionOption({
    ...session,
    message_count: 0,
  });

  setStatus(`● ${sessionSelect.options.length - 1} 个会话`, false);
}

function updateSessionCount(sessionId, count) {
  const opt = sessionSelect.querySelector(`option[value="${sessionId}"]`);
  if (!opt) return;
  const text = opt.textContent.replace(/\(\d+条\)$/, `(${count}条)`);
  opt.textContent = text;
}

// ── 渲染当前会话 ────────────────────────────────────────────────────

function renderSession(sessionId) {
  output.innerHTML = "";
  const msgs = sessionMessages[sessionId] || [];
  for (const msg of msgs) {
    appendMessage(msg);
  }
  scrollToBottom();
}

// ── 发送消息 ───────────────────────────────────────────────────────

async function sendMessage() {
  const text = input.value.trim();
  if (!text || !currentSession) {
    if (!currentSession) appendSystem("请先选择一个会话");
    return;
  }

  input.value = "";
  btnSend.disabled = true;

  try {
    const resp = await bridge.apiPost("send", {
      session_id: currentSession,
      message: text,
    });

    if (resp.error) {
      appendError(resp.error);
    } else {
      const msg = {
        type: "sent",
        sender: "Terminal",
        content: text,
        timestamp: Date.now() / 1000,
      };
      // 缓存
      if (!sessionMessages[currentSession]) {
        sessionMessages[currentSession] = [];
      }
      sessionMessages[currentSession].push(msg);
      appendMessage(msg);
      scrollToBottom();
    }
  } catch (err) {
    appendError(`发送失败: ${err.message || err}`);
  } finally {
    btnSend.disabled = false;
    input.focus();
  }
}

// ── 清空消息 ───────────────────────────────────────────────────────

async function clearMessages() {
  try {
    await bridge.apiPost("clear", {
      session_id: currentSession || "",
    });
    output.innerHTML = "";
    if (currentSession) {
      sessionMessages[currentSession] = [];
      updateSessionCount(currentSession, 0);
    }
    appendSystem("消息已清空");
  } catch {
    appendError("清空失败");
  }
}

// ── 轮询回退 ───────────────────────────────────────────────────────

function startPolling() {
  if (sseSource) return; // SSE 已连接，不需要轮询
  stopPolling();
  polling = true;
  pollTimer = setInterval(pollNewMessages, 2000);
}

function stopPolling() {
  polling = false;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollNewMessages() {
  if (!currentSession || !polling) return;
  try {
    const msgs = sessionMessages[currentSession] || [];
    const lastTs = msgs.length > 0 ? msgs[msgs.length - 1].timestamp : 0;
    const data = await bridge.apiGet("history", {
      session_id: currentSession,
      since: lastTs,
    });
    const messages = data.messages || [];
    for (const msg of messages) {
      if (!sessionMessages[currentSession]) {
        sessionMessages[currentSession] = [];
      }
      sessionMessages[currentSession].push(msg);
      appendMessage(msg);
    }
    if (messages.length) scrollToBottom();
  } catch {
    // 静默失败
  }
}

// ── DOM 操作 ───────────────────────────────────────────────────────

function appendMessage(msg) {
  const div = document.createElement("div");
  const time = formatTime(msg.timestamp);

  if (msg.type === "sent") {
    div.className = "msg msg-sent";
    div.textContent = `[${time}] >>> ${msg.content}`;
  } else if (msg.type === "received") {
    div.className = "msg msg-received";
    const sender = msg.sender || "unknown";
    div.textContent = `[${time}] <${sender}> ${msg.content}`;
  } else {
    div.className = "msg msg-system";
    div.textContent = `[${time}] ${msg.content}`;
  }

  output.appendChild(div);
}

function appendSystem(text) {
  const div = document.createElement("div");
  div.className = "msg msg-system";
  div.textContent = `--- ${text} ---`;
  output.appendChild(div);
  scrollToBottom();
}

function appendError(text) {
  const div = document.createElement("div");
  div.className = "msg msg-error";
  div.textContent = text;
  output.appendChild(div);
  scrollToBottom();
}

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.className = isError ? "status error" : "status";
}

function scrollToBottom() {
  output.scrollTop = output.scrollHeight;
}

function formatTime(ts) {
  if (!ts) return "??:??";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
