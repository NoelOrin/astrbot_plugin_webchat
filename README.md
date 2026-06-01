# astrbot_plugin_webchat

基于 SenseNova API 的 **终端风格 Web 聊天插件**，为 AstrBot 提供一个极客感十足的网页聊天界面。

## ✨ 功能特性

- **终端风格 UI** — 黑底绿字、闪烁光标、命令行交互体验
- **SSE 流式输出** — 逐 token 实时返回，打字机效果
- **思维链展示** — 模型推理过程（thinking）可折叠查看
- **多轮对话** — 自动维护上下文历史，支持配置最大记忆轮数
- **SenseNova 兼容** — 使用 Anthropic Messages API 格式，支持 `deepseek-v4-flash`、`sensenova-6.7-flash-lite` 等模型
- **可视化配置** — 通过 AstrBot WebUI 直接配置 API Key、模型、温度等参数

## 🚀 安装

1. 将本仓库克隆或复制到 AstrBot 的插件目录：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/your-username/astrbot_plugin_webchat.git
```

2. 重启 AstrBot 或在 WebUI 中重新加载插件

3. 在插件管理页面启用 `astrbot_plugin_webchat`

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页面中填写：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_key` | string | — | SenseNova API Key（必填） |
| `base_url` | string | `https://token.sensenova.cn/v1/messages` | API 端点地址 |
| `model` | string | `deepseek-v4-flash` | 模型 ID |
| `system_prompt` | text | `你是一个严谨的助手` | 系统提示词 |
| `max_tokens` | int | `4096` | 最大输出 Token 数 |
| `temperature` | float | `0.7` | 采样温度，范围 [0, 2) |
| `max_history` | int | `20` | 最大上下文轮数 |

### 获取 API Key

前往 [SenseNova 开放平台](https://token.sensenova.cn/) 注册并创建 API Key。

## 💬 使用

1. 启用插件后，在 AstrBot WebUI 插件卡片中点击进入 **Terminal** 页面
2. 在输入框中输入消息，按 **Enter** 或点击 **发送**
3. 模型回复会以流式逐字显示，思考过程可点击折叠查看
4. 点击 **清空** 按钮可重置聊天记录

## 📁 项目结构

```
astrbot_plugin_webchat/
├── main.py              # 后端核心：插件逻辑 + SSE 流式 API
├── _conf_schema.json    # 配置项 Schema 定义
├── metadata.yaml        # 插件元数据（名称、版本、作者）
├── .gitignore
└── pages/
    └── terminal/
        ├── index.html   # 终端 UI 页面
        ├── style.css    # 黑客终端风格样式
        └── app.js       # 前端逻辑（bridge SDK + SSE 流式接收）
```

### 后端 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plug/astrbot_plugin_webchat/chat` | POST | 发送消息，返回 SSE 流式响应 |
| `/api/plug/astrbot_plugin_webchat/history` | GET | 获取聊天历史 |
| `/api/plug/astrbot_plugin_webchat/clear` | POST | 清空聊天历史 |

### 技术栈

- **后端**：Python、Quart（AstrBot 内置）、aiohttp
- **前端**：原生 HTML/CSS/JS、AstrBot Bridge SDK
- **API**：SenseNova Anthropic Messages API 兼容接口（SSE 流式）

## 📄 License

MIT
