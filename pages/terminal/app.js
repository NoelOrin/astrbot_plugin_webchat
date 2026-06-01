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
let lastTimestamp = 0;
let polling = false;
let pollTimer = null;

// ── 初始化 ─────────────────────────────────────────────────────────

await bridge.ready();
appendSystem("webchat:// 终端已就绪");
await loadSessions();
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
btnRefresh.addEventListener("click", loadSessions);

sessionSelect.addEventListener("change", () => {
  currentSession = sessionSelect.value;
  output.innerHTML = "";
  lastTimestamp = 0;
  if (currentSession) {
    try { localStorage.setItem(STORAGE_KEY, currentSession); } catch {}
    const label = sessionSelect.options[sessionSelect.selectedIndex].text;
    appendSystem(`已切换到: ${label}`);
    loadHistory();
    startPolling();
  } else {
    appendSystem("请选择一个会话");
    stopPolling();
  }
});

// ── 加载会话列表 ────────────────────────────────────────────────────

async function loadSessions() {
  try {
    const data = await bridge.apiGet("sessions");
    const sessions = data.sessions || [];

    // 保留当前选中
    const prev = sessionSelect.value;
    sessionSelect.innerHTML = '<option value="">-- 选择会话 --</option>';

    for (const s of sessions) {
      const opt = document.createElement("option");
      opt.value = s.id;
      const tag = s.is_group ? "群聊" : "私聊";
      const name = s.group_name || s.group_id || s.platform;
      opt.textContent = `[${tag}] ${name} (${s.message_count}条)`;
      sessionSelect.appendChild(opt);
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

// ── 加载历史消息 ────────────────────────────────────────────────────

async function loadHistory() {
  if (!currentSession) return;
  try {
    const data = await bridge.apiGet("history", {
      session_id: currentSession,
    });
    const messages = data.messages || [];
    for (const msg of messages) {
      appendMessage(msg);
      if (msg.timestamp > lastTimestamp) {
        lastTimestamp = msg.timestamp;
      }
    }
    scrollToBottom();
  } catch (err) {
    console.error("加载历史失败:", err);
  }
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
      appendMessage({
        type: "sent",
        sender: "Terminal",
        content: text,
        timestamp: Date.now() / 1000,
      });
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
    lastTimestamp = 0;
    appendSystem("消息已清空");
  } catch {
    appendError("清空失败");
  }
}

// ── 轮询新消息 ─────────────────────────────────────────────────────

function startPolling() {
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
    const data = await bridge.apiGet("history", {
      session_id: currentSession,
      since: lastTimestamp,
    });
    const messages = data.messages || [];
    for (const msg of messages) {
      appendMessage(msg);
      if (msg.timestamp > lastTimestamp) {
        lastTimestamp = msg.timestamp;
      }
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
