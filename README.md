# astrbot_plugin_webchat

通过 **Web 终端** 向 QQ 群发送和接收消息的 AstrBot 插件。

## ✨ 功能特性

- **终端风格 UI** — 黑底绿字、极客命令行交互体验
- **会话发现** — 自动发现 AstrBot 已接入的 QQ 群/私聊会话
- **消息发送** — 从终端直接向 QQ 群发送消息
- **消息接收** — 实时轮询显示群内收到的消息
- **消息历史** — 保留每个会话的收发记录
- **无需配置** — 直接使用 AstrBot 已接入的消息平台

## 🚀 安装

1. 将本仓库克隆或复制到 AstrBot 的插件目录：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/NoelOrin/astrbot_plugin_webchat.git
```

2. 重启 AstrBot 或在 WebUI 中重新加载插件

3. 在插件管理页面启用 `astrbot_plugin_webchat`

## 💬 使用

1. 启用插件后，在 AstrBot WebUI 插件卡片中点击进入 **Terminal** 页面
2. 在顶部下拉框中选择一个 QQ 群会话
3. 在底部输入框中输入消息，按 **Enter** 或点击 **发送**
4. 群内收到的消息会实时显示在终端中

### 消息格式

```
--- webchat:// 终端已就绪 ---
--- 已切换到: 123456789 [AIOCQHTTP] ---
[14:30:01] <张三> 大家好
[14:30:15] >>> 你好！
[14:30:20] <李四> 欢迎
```

- `[时间] >>> 消息` — 你发送的消息（右侧蓝色）
- `[时间] <发送者> 消息` — 收到的消息（左侧灰色）

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页面中：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_log_per_session` | int | `200` | 每个会话最大消息记录数 |

## 📁 项目结构

```
astrbot_plugin_webchat/
├── main.py              # 后端：消息捕获 + Web API
├── _conf_schema.json    # 配置项 Schema
├── metadata.yaml        # 插件元数据
├── .gitignore
├── README.md
├── docs/                # AstrBot 插件开发文档参考
└── pages/
    └── terminal/
        ├── index.html   # 终端 UI 页面
        ├── style.css    # 终端风格样式
        └── app.js       # 前端逻辑（轮询 + 消息渲染）
```

### 后端 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plug/astrbot_plugin_webchat/sessions` | GET | 获取可用会话列表 |
| `/api/plug/astrbot_plugin_webchat/send` | POST | 向指定会话发送消息 |
| `/api/plug/astrbot_plugin_webchat/history` | GET | 获取消息历史（支持 `since` 参数增量拉取） |
| `/api/plug/astrbot_plugin_webchat/clear` | POST | 清空消息历史 |

### 工作原理

1. **消息捕获** — 插件注册 `@filter.event_message_type(ALL)` 事件监听器，捕获所有经过 AstrBot 的消息
2. **会话注册** — 通过 `event.unified_msg_origin` 自动发现并注册新会话
3. **消息发送** — 调用 `context.send_message(umo, chain)` 通过 AstrBot 向目标平台发送消息
4. **前端轮询** — 每 2 秒拉取新消息，支持增量拉取（`since` 时间戳）

### 技术栈

- **后端**：Python、Quart（AstrBot 内置）
- **前端**：原生 HTML/CSS/JS、AstrBot Bridge SDK
- **消息平台**：通过 AstrBot 适配器接入（OneBot v11、QQ Official 等）

## 📄 License

MIT
