# 插件 Pages

> 来源: https://docs.astrbot.app/dev/star/guides/plugin-pages.html

AstrBot 支持插件通过 `pages/` 目录暴露 Dashboard 页面。每个一级子目录是一个独立 Page：

```
astrbot_plugin_xxx/
├─ main.py
└─ pages/
   ├─ bridge-demo/
   │  ├─ index.html
   │  ├─ app.js
   │  ├─ style.css
   │  └─ assets/
   │     └─ logo.svg
   └─ settings/
      └─ index.html
```

AstrBot 会扫描 `pages/<page_name>/index.html`；没有 `index.html` 的目录会被忽略。

> 如果只是让用户填写几个配置项，优先使用 `_conf_schema.json`。
> 插件 Pages 更适合复杂表单、Dashboard、日志、SSE 和自定义交互流程。

用户可以在 WebUI 插件详情页中看到并进入注册的 Pages。

---

## 最小前端示例

### index.html

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Plugin Page Demo</title>
    <link rel="stylesheet" href="./style.css" />
  </head>
  <body>
    <button id="ping">Ping</button>
    <pre id="output"></pre>
    <script type="module" src="./app.js"></script>
  </body>
</html>
```

### app.js

```js
const bridge = window.AstrBotPluginPage;
const output = document.getElementById("output");
const context = await bridge.ready();
output.textContent = JSON.stringify(context, null, 2);

document.getElementById("ping").addEventListener("click", async () => {
  const result = await bridge.apiGet("ping");
  output.textContent = JSON.stringify(result, null, 2);
});
```

> 不需要手动引入 bridge SDK，AstrBot 会自动插入。

---

## 注册后端 API

前端调用 `bridge.apiGet("ping")` 时，Dashboard 会转发到 `/api/plug/<plugin_name>/ping`。

路由**必须**带上插件名作为前缀：

```python
from quart import jsonify

PLUGIN_NAME = "astrbot_plugin_xxx"

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        context.register_web_api(
            f"/{PLUGIN_NAME}/ping",
            self.page_ping,
            ["GET"],
            "Page ping",
        )

    async def page_ping(self):
        return jsonify({"message": "pong"})
```

---

## Bridge API 参考

插件 Page 中直接使用 `window.AstrBotPluginPage`：

| 方法 | 说明 |
|------|------|
| `ready()` | 等待 bridge 就绪，返回 `Promise<context>` |
| `getContext()` | 同步读取当前上下文 |
| `getLocale()` | 读取当前 WebUI 语言 |
| `getI18n()` | 读取当前插件的 i18n 资源 |
| `t(key, fallback)` | 按 key 获取文案 |
| `onContext(handler)` | 监听上下文变化（如语言切换） |
| `apiGet(endpoint, params)` | GET 请求 |
| `apiPost(endpoint, body)` | POST 请求 |
| `upload(endpoint, file)` | 上传文件 |
| `download(endpoint, params, filename)` | 下载文件 |
| `subscribeSSE(endpoint, handlers, params)` | 订阅 SSE |
| `unsubscribeSSE(subscriptionId)` | 取消 SSE 订阅 |

### ready() 上下文

```json
{
  "pluginName": "astrbot_plugin_xxx",
  "displayName": "Plugin Name",
  "pageName": "bridge-demo",
  "pageTitle": "Bridge Demo",
  "locale": "zh-CN",
  "i18n": {}
}
```

### endpoint 规则

- 必须是插件内相对路径
- 允许: `"stats"`, `"settings/save"`
- 不允许: 空字符串, `"/stats"`, `"../stats"`, `"https://example.com"`, `"stats?x=1"`
- query 参数通过 `params` 传递，不要拼进 endpoint

---

## Page 国际化

复用插件 i18n 资源文件，给 `.astrbot-plugin/i18n/<locale>.json` 增加 `pages.<page_name>`：

```json
{
  "pages": {
    "bridge-demo": {
      "title": "Bridge 演示页",
      "heading": "插件页面"
    }
  }
}
```

在 Page 内使用 `t()` 渲染文案，用 `onContext()` 响应语言切换：

```js
const bridge = window.AstrBotPluginPage;
function render() {
  document.title = bridge.t("pages.bridge-demo.title", "Bridge Demo");
}
await bridge.ready();
render();
bridge.onContext(render);
```

---

## 静态资源

AstrBot 会自动重写相对资源路径并补上 `asset_token`。正常写相对路径即可：

```
./style.css
./assets/logo.svg
```

> 不要手动追加 `asset_token`，不要用 `..` 逃逸 Page 根目录。
> 建议 SPA 使用 hash routing。

---

## 安全约束

插件 Pages 运行在受限 iframe 中：

```
allow-scripts allow-forms allow-downloads
```

- 不能访问 Dashboard cookies、LocalStorage 或同源 DOM
- 不能绕过 bridge 复用 Dashboard auth

---

## 调试建议

- 新增或删除 Page 目录后**重载插件**
- 修改 `pages/<page_name>/` 下的静态资源后，刷新 Page 即可
- 如果 Page 没出现，检查 `index.html` 是否存在以及插件是否启用

---

## SSE 订阅示例

```js
const subscriptionId = await bridge.subscribeSSE(
  "events",
  {
    onOpen() { console.log("SSE opened"); },
    onMessage(event) {
      console.log(event.raw, event.parsed, event.lastEventId);
    },
    onError() { console.warn("SSE error"); },
  },
  { topic: "logs" },
);

// 页面卸载时清理
window.addEventListener("beforeunload", () => {
  bridge.unsubscribeSSE(subscriptionId);
});
```
