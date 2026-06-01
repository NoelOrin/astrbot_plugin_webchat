const bridge = window.AstrBotPluginPage;

const output = document.getElementById("output");
const input = document.getElementById("input");
const btnSend = document.getElementById("btn-send");
const btnClear = document.getElementById("btn-clear");
const status = document.getElementById("status");

let streaming = false;
let currentAssistant = null;
let currentThinking = null;
let currentThinkingContent = "";
let currentTextContent = "";

// ── 初始化 ─────────────────────────────────────────────────────────

await bridge.ready();
appendSystem("webchat:// 终端已就绪。输入消息开始对话。");
input.focus();

// ── 事件绑定 ───────────────────────────────────────────────────────

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

btnSend.addEventListener("click", sendMessage);
btnClear.addEventListener("click", clearChat);

// ── 发送消息 ───────────────────────────────────────────────────────

async function sendMessage() {
  const text = input.value.trim();
  if (!text || streaming) return;

  input.value = "";
  appendUser(text);

  streaming = true;
  setStatus("生成中...", false);
  btnSend.disabled = true;
  input.disabled = true;

  // 创建助手消息容器
  currentAssistant = appendAssistant("");
  currentThinkingContent = "";
  currentTextContent = "";

  // 使用 bridge SSE 订阅
  try {
    await bridge.subscribeSSE(
      "chat",
      {
        onOpen() {
          setStatus("● 生成中...", false);
        },
        onMessage(event) {
          handleSSEMessage(event);
        },
        onError(err) {
          setStatus("● 连接错误", true);
          appendError("SSE 连接异常");
          finishStreaming();
        },
      },
      {
        message: text,
        session_id: getSessionId(),
      },
    );
  } catch (err) {
    // subscribeSSE 不支持 body，需要改用 POST 回退
    await fetchViaPost(text);
  }
}

// SSE 消息处理
function handleSSEMessage(event) {
  const type = event.lastEventId || event.raw.split("\n")[0]?.replace("event: ", "");

  try {
    // event.parsed 可能是对象或字符串
    const data = typeof event.parsed === "object" ? event.parsed : JSON.parse(event.raw.split("data: ")[1] || "{}");

    if (data.text !== undefined) {
      // 判断是 thinking 还是 text
      // bridge SSE 会把 event type 信息丢失，我们需要从 raw 中解析
      const rawLines = event.raw.split("\n");
      let eventType = "";
      for (const line of rawLines) {
        if (line.startsWith("event: ")) {
          eventType = line.substring(7).trim();
          break;
        }
      }

      if (eventType === "thinking") {
        currentThinkingContent += data.text;
        updateAssistantContent();
      } else if (eventType === "token") {
        currentTextContent += data.text;
        updateAssistantContent();
      } else if (eventType === "done") {
        finishStreaming();
      }
    }

    if (data.error) {
      appendError(data.error);
      finishStreaming();
    }
  } catch {
    // 忽略解析错误
  }
}

// 更新助手消息显示
function updateAssistantContent() {
  if (!currentAssistant) return;
  let html = "";

  if (currentThinkingContent) {
    html += `<span class="msg-thinking">💭 ${escapeHtml(currentThinkingContent)}</span>\n`;
  }

  html += escapeHtml(currentTextContent);

  if (streaming) {
    html += '<span class="cursor"></span>';
  }

  currentAssistant.innerHTML = html;
  scrollToBottom();
}

// POST 回退方案（bridge.subscribeSSE 可能不支持自定义 body）
async function fetchViaPost(text) {
  try {
    const resp = await fetch(`/api/plug/${PLUGIN_NAME}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: getSessionId(),
      }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      appendError(err.error || `HTTP ${resp.status}`);
      finishStreaming();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      let eventType = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.substring(7).trim();
        } else if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.substring(6));
            if (eventType === "token") {
              currentTextContent += data.text;
            } else if (eventType === "thinking") {
              currentThinkingContent += data.text;
            } else if (eventType === "done") {
              // handled below
            } else if (eventType === "error") {
              appendError(data.error);
            }
            updateAssistantContent();
          } catch {}
        }
      }
    }

    finishStreaming();
  } catch (err) {
    appendError(`请求失败: ${err.message}`);
    finishStreaming();
  }
}

// 完成流式输出
function finishStreaming() {
  streaming = false;
  btnSend.disabled = false;
  input.disabled = false;
  input.focus();
  setStatus("● 已连接", false);

  // 移除光标
  if (currentAssistant) {
    const cursor = currentAssistant.querySelector(".cursor");
    if (cursor) cursor.remove();

    // 最终渲染：thinking 折叠 + 正文
    let html = "";
    if (currentThinkingContent) {
      html += `<details><summary class="msg-thinking">💭 思考过程</summary><span class="msg-thinking">${escapeHtml(currentThinkingContent)}</span></details>\n`;
    }
    html += escapeHtml(currentTextContent);
    currentAssistant.innerHTML = html || "(空回复)";
  }

  currentAssistant = null;
  currentThinkingContent = "";
  currentTextContent = "";

  // 取消 SSE 订阅
  try {
    bridge.unsubscribeSSE();
  } catch {}
}

// ── 清空聊天 ───────────────────────────────────────────────────────

async function clearChat() {
  try {
    await bridge.apiPost("clear", {});
    output.innerHTML = "";
    appendSystem("聊天记录已清空。");
  } catch {
    appendError("清空失败");
  }
}

// ── DOM 操作 ───────────────────────────────────────────────────────

function appendUser(text) {
  const div = document.createElement("div");
  div.className = "msg msg-user";
  div.textContent = text;
  output.appendChild(div);
  scrollToBottom();
}

function appendAssistant(text) {
  const div = document.createElement("div");
  div.className = "msg msg-assistant";
  div.innerHTML = text ? escapeHtml(text) : '<span class="cursor"></span>';
  output.appendChild(div);
  scrollToBottom();
  return div;
}

function appendError(text) {
  const div = document.createElement("div");
  div.className = "msg msg-error";
  div.textContent = text;
  output.appendChild(div);
  scrollToBottom();
}

function appendSystem(text) {
  const div = document.createElement("div");
  div.className = "msg msg-system";
  div.textContent = text;
  output.appendChild(div);
  scrollToBottom();
}

function setStatus(text, isError) {
  status.textContent = text;
  status.className = isError ? "status error" : "status";
}

function scrollToBottom() {
  output.scrollTop = output.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function getSessionId() {
  // 使用 bridge 上下文生成稳定 session ID
  const ctx = bridge.getContext();
  return ctx?.pluginName || "default";
}

// ── 常量 ───────────────────────────────────────────────────────────

const PLUGIN_NAME = "astrbot_plugin_webchat";
