/**
 * bridge 获取路径的冒烟。
 *
 * 视图跑在 Dashboard 的 sandbox iframe 里，所有后端能力都靠 postMessage 转发。
 * AstrBot 4.29.x 注入的 `/api/plugin/page/bridge-sdk.js` 在里面必然 401
 * （登录 cookie 是 SameSite=Strict，opaque origin 的 <script src> 带不出去；
 * 带 ?asset_token= 实测也过不了），所以页面必须能自己顶上。
 *
 * 场景 1：顶层直接打开 —— 没有父页面，必须立刻给出可诊断提示。
 * 场景 2：在 iframe 里但宿主没注入 SDK —— 用页面自带的客户端握手，
 *         且**不能**再回去请求那条 401 的路由。
 */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const root = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../../pages/terminal",
);
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const appSource = fs
  .readFileSync(path.join(root, "app.js"), "utf8")
  .replace('from "./webchat-lib.js"', `from "file://${path.join(root, "webchat-lib.js")}"`)
  .replace('from "./plugin-bridge.js"', `from "file://${path.join(root, "plugin-bridge.js")}"`);

const results = [];
const check = (name, cond, extra = "") => results.push([cond ? "PASS" : "FAIL", name, extra]);

const errors = [];
const originalError = console.error;
console.error = (...args) => {
  errors.push(args);
};

const tempFiles = [];

/** 把 jsdom 的 window 暴露成 Node 全局，app.js 作为 ESM 只能在模块求值时读一次。 */
function exposeGlobals(window) {
  window.matchMedia = (q) => ({
    matches: false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  });
  window.CSS = window.CSS || {};
  window.CSS.escape = (s) => String(s).replace(/"/g, '\\"');
  window.Element.prototype.scrollIntoView = function () {};

  globalThis.window = window;
  globalThis.document = window.document;
  Object.defineProperty(globalThis, "navigator", {
    value: window.navigator,
    configurable: true,
    writable: true,
  });
  globalThis.CSS = window.CSS;
  globalThis.EventSource = window.EventSource;
  globalThis.FileReader = window.FileReader;
  globalThis.Blob = window.Blob;
  globalThis.URL = window.URL;
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
}

async function bootPage(url, { withParent = false } = {}) {
  const dom = new JSDOM(html, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const posted = [];
  if (withParent) {
    // jsdom 里 window.top 不可重定义，但 window.parent 可以，
    // 而 app 判断「有没有父页面」用的正是 parent。
    Object.defineProperty(window, "parent", {
      configurable: true,
      get: () => ({
        postMessage(message, targetOrigin) {
          posted.push({ message, targetOrigin });
        },
      }),
    });
  }
  exposeGlobals(window);
  const tmp = path.join(os.tmpdir(), `webchat-bridge-${process.pid}-${tempFiles.length}.mjs`);
  tempFiles.push(tmp);
  fs.writeFileSync(tmp, appSource);
  await import("file://" + tmp);
  return { window, posted };
}

try {
  // ── 场景 1：file:// 直接打开，没有宿主、没有父页面 ──
  const standalone = await bootPage("file:///tmp/webchat-standalone/index.html");
  await new Promise((r) => setTimeout(r, 300));

  const status = (win) => win.document.getElementById("status-text").textContent;
  check(
    "直接打开时状态栏显示 Bridge 不可用",
    status(standalone.window) === "Bridge 不可用",
    status(standalone.window),
  );

  const diagnosis = errors.find(
    ([message]) => typeof message === "string" && message.includes("未检测到 AstrBot 页面 Bridge"),
  );
  check("直接打开时控制台留下结构化诊断", !!diagnosis, JSON.stringify(errors));
  check(
    "诊断里标明不在 iframe 内",
    diagnosis?.[1]?.inFrame === false,
    JSON.stringify(diagnosis?.[1] || null),
  );
  check(
    "诊断里标明 bridge 来源是 none",
    diagnosis?.[1]?.bridgeSource === "none",
    JSON.stringify(diagnosis?.[1] || null),
  );

  const toastText = standalone.window.document.getElementById("toast-region").textContent;
  check("提示保留原文案", toastText.includes("未检测到 AstrBot 页面 Bridge"), toastText);
  check("提示说明要从插件详情页进入", toastText.includes("插件详情页"), toastText);
  check(
    "不再去请求必然 401 的 bridge-sdk.js",
    standalone.window.document.querySelector('script[src*="bridge-sdk.js"]') === null,
  );

  // ── 场景 2：iframe 里、宿主没注入 SDK ──
  errors.length = 0;
  const framed = await bootPage(
    "http://localhost/api/v1/plugins/astrbot_plugin_webchat/views/terminal/_t/tok/?asset_token=tok&theme=light",
    { withParent: true },
  );
  await new Promise((r) => setTimeout(r, 200));

  check(
    "iframe 里不再报 Bridge 不可用",
    status(framed.window) !== "Bridge 不可用",
    status(framed.window),
  );
  check(
    "iframe 里也不请求 bridge-sdk.js",
    framed.window.document.querySelector('script[src*="bridge-sdk.js"]') === null,
  );
  const ready = framed.posted.find(({ message }) => message?.kind === "ready");
  check("自带客户端主动向父页面报 ready", !!ready, JSON.stringify(framed.posted));
  check(
    "消息走 astrbot-plugin-page 协议通道",
    ready?.message?.channel === "astrbot-plugin-page",
    JSON.stringify(ready?.message || null),
  );
} finally {
  console.error = originalError;
  for (const file of tempFiles) fs.rmSync(file, { force: true });
}

const failed = results.filter((r) => r[0] === "FAIL");
for (const [state, name, extra] of results) {
  console.log(`${state === "PASS" ? "✔" : "✘"} ${name}${state === "FAIL" ? "  <= " + extra : ""}`);
}
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
