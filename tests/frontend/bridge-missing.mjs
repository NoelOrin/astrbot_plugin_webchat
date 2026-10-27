/**
 * bridge 缺失 / 自愈场景的冒烟。
 *
 * 视图跑在 Dashboard 的受限 iframe 里，后端能力全靠宿主注入的
 * bridge-sdk.js 提供的 window.AstrBotPluginView。注入可能压根没发生
 * （用户直接打开页面），也可能请求失败（反向代理子路径下绝对路径 404）。
 * 这两种情况以前都只丢一句「未检测到 Bridge」，用户无从下手。
 *
 * 场景 1：直接打开页面 —— 必须给出可诊断提示，而不是停在「正在连接」。
 * 场景 2：子路径部署 —— 补加载的 SDK 地址要带上部署前缀，而不是打到域名根。
 *
 * 约定：只断言不会自行消失的信号（状态文案、console 诊断、注入的 script），
 * toast 到点会被移除，不能当稳定断言。
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
  .replace('from "./webchat-lib.js"', `from "file://${path.join(root, "webchat-lib.js")}"`);

const results = [];
const check = (name, cond, extra = "") => results.push([cond ? "PASS" : "FAIL", name, extra]);

const errors = [];
const originalError = console.error;
console.error = (...args) => {
  errors.push(args);
};

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

const tempFiles = [];
async function bootPage(url) {
  const dom = new JSDOM(html, {
    url,
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  exposeGlobals(dom.window);
  const tmp = path.join(os.tmpdir(), `webchat-bridge-${process.pid}-${tempFiles.length}.mjs`);
  tempFiles.push(tmp);
  fs.writeFileSync(tmp, appSource);
  await import("file://" + tmp);
  return dom.window;
}

try {
  // ── 场景 1：file:// 直接打开，没有宿主、没有 bridge ──
  const standalone = await bootPage("file:///tmp/webchat-standalone/index.html");
  await new Promise((r) => setTimeout(r, 300));

  const statusText = () => standalone.document.getElementById("status-text").textContent;
  check("直接打开时状态栏显示 Bridge 不可用", statusText() === "Bridge 不可用", statusText());

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
    "诊断里给出页面地址",
    typeof diagnosis?.[1]?.href === "string" && diagnosis[1].href.includes("index.html"),
    JSON.stringify(diagnosis?.[1] || null),
  );

  const toastText = standalone.document.getElementById("toast-region").textContent;
  check("提示保留原文案", toastText.includes("未检测到 AstrBot 页面 Bridge"), toastText);
  check("提示说明要从插件详情页进入", toastText.includes("插件详情页"), toastText);
  check("没有 bridge 时不谎报已连接", !statusText().includes("实时连接"), statusText());

  // ── 场景 2：/astrbot 子路径 + 宿主注入失败，且 SDK 只认 query token ──
  // 视图 iframe 是 sandbox（opaque origin），Dashboard 的 SameSite=Strict cookie
  // 不会被 <script src> 带出去，所以裸地址必然 401，必须带上 asset_token。
  const subpath = await bootPage(
    "http://localhost/astrbot/api/v1/plugins/astrbot_plugin_webchat/views/terminal/_t/tok/" +
      "?asset_token=tok&theme=light",
  );
  await new Promise((r) => setTimeout(r, 200));

  const scripts = () => [
    ...subpath.document.querySelectorAll("script[data-webchat-bridge-sdk]"),
  ];
  check("宿主没注入时自己补加载 bridge SDK", scripts().length > 0, String(scripts().length));
  check(
    "第一个候选带上 asset_token（否则 sandbox 里一定 401）",
    scripts()[0]?.getAttribute("src") ===
      "/astrbot/api/plugin/page/bridge-sdk.js?asset_token=tok",
    scripts()[0]?.getAttribute("src"),
  );

  // 第一个候选 401 → 必须回落到裸地址兜底，而不是直接放弃
  scripts()[0]?.dispatchEvent(new subpath.Event("error"));
  await new Promise((r) => setTimeout(r, 200));
  check(
    "带 token 失败后回落裸地址，且前缀不丢",
    scripts()[1]?.getAttribute("src") === "/astrbot/api/plugin/page/bridge-sdk.js",
    scripts()[1]?.getAttribute("src"),
  );

  // SDK 加载成功后必须继续启动，而不是继续报「Bridge 不可用」
  subpath.AstrBotPluginView = {
    ready: () => new Promise(() => {}),
    apiGet: () => new Promise(() => {}),
  };
  scripts()[1]?.dispatchEvent(new subpath.Event("load"));
  await new Promise((r) => setTimeout(r, 120));
  const healedStatus = subpath.document.getElementById("status-text").textContent;
  check("补加载成功后不会再报 Bridge 不可用", healedStatus !== "Bridge 不可用", healedStatus);

  // ── 场景 3：两个候选都失败（401）——诊断要交代 token 与尝试过的地址 ──
  errors.length = 0;
  const denied = await bootPage(
    "http://localhost/api/v1/plugins/astrbot_plugin_webchat/views/terminal/_t/tok/" +
      "?asset_token=tok&theme=light",
  );
  await new Promise((r) => setTimeout(r, 200));
  const deniedScripts = () => [
    ...denied.document.querySelectorAll("script[data-webchat-bridge-sdk]"),
  ];
  deniedScripts()[0]?.dispatchEvent(new denied.Event("error"));
  await new Promise((r) => setTimeout(r, 200));
  deniedScripts()[1]?.dispatchEvent(new denied.Event("error"));
  await new Promise((r) => setTimeout(r, 200));

  const deniedDiag = errors.find(
    ([message]) => typeof message === "string" && message.includes("未检测到 AstrBot 页面 Bridge"),
  );
  check("401 后落到 Bridge 不可用", denied.document.getElementById("status-text").textContent === "Bridge 不可用");
  check("诊断标明带了 asset_token", deniedDiag?.[1]?.hasAssetToken === true, JSON.stringify(deniedDiag?.[1] || null));
  check(
    "诊断列出两次尝试且 token 已打码",
    JSON.stringify(deniedDiag?.[1]?.attempted || []).includes("asset_token=***") &&
      (deniedDiag?.[1]?.attempted || []).length === 2,
    JSON.stringify(deniedDiag?.[1]?.attempted || null),
  );
} finally {
  console.error = originalError;
  for (const file of tempFiles) fs.rmSync(file, { force: true });
}

const failed = results.filter((r) => r[0] === "FAIL");
for (const [status, name, extra] of results) {
  console.log(`${status === "PASS" ? "✔" : "✘"} ${name}${status === "FAIL" ? "  <= " + extra : ""}`);
}
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
