const bridge = window.AstrBotPluginPage;

const output = document.getElementById("output");
const input = document.getElementById("input");
const btnSend = document.getElementById("btn-send");
const btnClear = document.getElementById("btn-clear");
const status = document.getElementById("status");

let streaming = false;
let currentAssistant = null;
let currentThinkingContent = "";
let currentTextContent = "";
let sseSubscriptionId = null;

// 每个标签页唯一 session ID
const SESSION_ID = crypto.randomUUID?.() || `tab-${Date.now()}-${Math.random().toString(36).slice(2)}`;

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
  setStatus("● 生成中...", false);
  btnSend.disabled = true;
  input.disabled = true;

  // 创建助手消息容器
  currentAssistant = appendAssistant("");
  currentThinkingContent = "";
  currentTextContent = "";

  // bridge.subscribeSSE 不支持自定义 POST body，直接用 fetch
  await fetchViaPost(text);
}

// POST 流式请求
async function fetchViaPost(text) {
  let eventType = "";
  try {
    const resp = await fetch(`/api/plug/${PLUGIN_NAME}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: SESSION_ID,
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
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        if (trimmed.startsWith("event: ")) {
          eventType = trimmed.substring(7).trim();
        } else if (trimmed.startsWith("data: ")) {
          const jsonStr = trimmed.substring(6);
          try {
            const data = JSON.parse(jsonStr);
            switch (eventType) {
              case "token":
                currentTextContent += data.text || "";
                updateAssistantContent();
                break;
              case "thinking":
                currentThinkingContent += data.text || "";
                updateAssistantContent();
                break;
              case "done":
                finishStreaming();
                return;
              case "error":
                appendError(data.error || "未知错误");
                finishStreaming();
                return;
            }
          } catch {
            // 非 JSON 数据，跳过
          }
        }
      }
    }

    // 流读取完毕但未收到 done 事件
    finishStreaming();
  } catch (err) {
    appendError(`请求失败: ${err.message}`);
    finishStreaming();
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

// 完成流式输出
function finishStreaming() {
  if (!streaming) return; // 防止重复调用
  streaming = false;
  btnSend.disabled = false;
  input.disabled = false;
  input.focus();
  setStatus("● 已连接", false);

  // 移除光标，最终渲染
  if (currentAssistant) {
    const cursor = currentAssistant.querySelector(".cursor");
    if (cursor) cursor.remove();

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

// ── 常量 ───────────────────────────────────────────────────────────

const PLUGIN_NAME = "astrbot_plugin_webchat";
