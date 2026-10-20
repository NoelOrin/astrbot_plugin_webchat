# astrbot_plugin_webchat

通过 **Web 终端** 收发并检索 AstrBot 所有会话消息的插件。

## ✨ 功能特性

- **双向记录** — 不只是收消息：Bot 的回复、指令响应、其他插件主动推送也一并入库，一次对话两半都看得到
- **富媒体消息** — 图片、@、表情、语音、文件、合并转发按片段保存，图片可点开大图预览
- **@ 真正生效** — Bot/AI 回复里的 `<mention id="123"/>` 等占位标签会在发送前还原成真实 At 消息段；
  输入框手打 `@昵称` / `@QQ号` 也会解析成真 @（查不到时保留 `@昵称` 原文，绝不把标签发给用户）
- **实时推送** — 优先使用 AstrBot 的 `bridge.subscribeSSE`，不可用时自动降级为 2.5 秒轮询
- **消息检索** — 按关键词全文搜索历史消息，点击结果自动翻页并定位高亮
- **会话管理** — 置顶、导出 TXT、删除会话；可以主动发起还没说过话的群聊/私聊
- **身份映射** — 同时展示群号、私聊 ID、发送者 ID 与最新昵称，点参与者即可 @ 他
- **不打断阅读** — 向上翻阅历史时新消息只弹气泡提醒，点击后回到最新消息
- **现代消息界面** — shadcn 风格双主题界面，支持桌面双栏与移动端会话抽屉
- **亮色/暗色主题** — 首次跟随 AstrBot WebUI，手动切换后自动记住选择
- **紧凑模式** — 窄屏或低高度窗口自动压缩布局，也可手动切换并记住选择
- **数据安全** — 记录存放在 `data/plugin_data/`，重装插件不会丢；旧版本数据库自动迁移

## 🚀 安装

1. 将本仓库克隆或复制到 AstrBot 的插件目录：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/NoelOrin/astrbot_plugin_webchat.git
```

2. 重启 AstrBot 或在 WebUI 中重新加载插件

3. 在插件管理页面启用 `astrbot_plugin_webchat`

## 🖥️ 进入终端页面

1. 打开 AstrBot WebUI
2. 左侧菜单点击 **「插件」**
3. 找到 `astrbot_plugin_webchat` 卡片，**点击卡片进入插件详情页**
4. 详情页顶部会出现 **「终端」** 视图，点击即可进入

页面路径参考：

```
http://<你的AstrBot地址>/webui#/plugins/astrbot_plugin_webchat
```

## 💬 使用

1. 进入终端页面后，在左侧会话列表中选择群聊或私聊
2. 在底部输入框输入消息，按 **Enter** 或点击 **发送**；Shift+Enter 换行
3. 点回形针或直接 **粘贴** 可以附带图片
4. 新消息通过 SSE 实时显示，连接不可用时自动降级轮询

### 消息浏览

- 会话列表展示群名/私聊昵称、群号或用户 ID、最新消息与未读数，置顶会话排在最前
- 会话详情展示类型、ID、平台与参与者昵称映射；点击参与者即可 @ 他
- 在消息列表向上浏览历史时，新消息到达不会改变当前滚动位置
- 底部出现新消息气泡时可点击，随后平滑滚动到最新消息
- 点击消息列表顶部的加载按钮，可按游标继续获取更早记录
- 悬停消息出现「复制 / 删除」操作

### 消息检索

在会话列表下方的搜索框输入关键词，会在当前会话内检索并高亮命中内容；
点击结果会自动跳转到该消息，必要时自动向前翻页加载。

### 新建会话

点击会话列表标题右侧的 **+**，选择平台与类型，可以从协议端拉取群/好友列表，
也可以直接手动填写群号或用户 ID。

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页面中：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_log_per_session` | int | `200` | 每个会话最大消息记录数，超出后裁剪最早的 |
| `capture_bot_messages` | bool | `true` | 是否记录 Bot/插件主动发出的消息 |
| `bot_display_name` | string | `Bot` | 出站消息在终端里的显示名称 |

## 📁 项目结构

```
astrbot_plugin_webchat/
├── main.py                  # 插件入口：事件监听 + Web API + SSE
├── webchat_core.py          # 纯逻辑层：片段序列化 + SQLite 存储（可独立测试）
├── _conf_schema.json        # 配置项 Schema
├── metadata.yaml            # 插件元数据
├── ruff.toml                # Lint 配置
├── LICENSE                  # MIT
├── .astrbot-plugin/i18n/    # 中英文案
├── docs/                    # AstrBot 插件开发文档参考
├── pages/
│   └── terminal/
│       ├── index.html       # 页面结构
│       ├── style.css        # 主题与响应式样式
│       ├── webchat-lib.js   # 前端纯函数（去重、排序、格式化、高亮）
│       ├── webchat-lib.test.js
│       └── app.js           # 状态管理、渲染、SSE、弹窗
└── tests/
    ├── test_webchat_core.py # 存储与序列化单测
    ├── test_plugin.py       # 插件行为测试（假宿主）
    └── frontend/dom-smoke.mjs  # jsdom 端到端冒烟
```

### 后端 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plug/astrbot_plugin_webchat/sessions` | GET | 会话列表（`with_history=1` 附带最新一页消息） |
| `/api/plug/astrbot_plugin_webchat/send` | POST | 发送消息（支持 `segments` 附带图片/@） |
| `/api/plug/astrbot_plugin_webchat/history` | GET | 历史：`since` 增量，或 `limit` + `before_id` 游标分页 |
| `/api/plug/astrbot_plugin_webchat/search` | GET | 关键词检索消息 |
| `/api/plug/astrbot_plugin_webchat/clear` | POST | 清空历史（不带 `session_id` 需 `confirm_all=true`） |
| `/api/plug/astrbot_plugin_webchat/stream` | GET | SSE 实时推送 |
| `/api/plug/astrbot_plugin_webchat/messages/<id>` | DELETE | 删除单条消息（`?remote=1` 尝试平台撤回） |
| `/api/plug/astrbot_plugin_webchat/sessions/<id>` | PATCH / DELETE | 置顶改名 / 删除会话 |
| `/api/plug/astrbot_plugin_webchat/platforms` | GET | 可用平台实例 |
| `/api/plug/astrbot_plugin_webchat/peers` | GET | 拉取群列表/好友列表 |
| `/api/plug/astrbot_plugin_webchat/open` | POST | 主动创建会话 |
| `/api/plug/astrbot_plugin_webchat/media` | GET | 图片代理（规避防盗链） |
| `/api/plug/astrbot_plugin_webchat/export` | GET | 导出会话记录 |
| `/api/plug/astrbot_plugin_webchat/health` | GET | 运行状态 |

### SSE 事件

| 事件 | 触发时机 |
|------|----------|
| `connected` / `hello` | 连接建立 |
| `message` | 收到消息 / Bot 发出消息 / 终端发送成功 |
| `session` | 发现新会话 |
| `session_updated` | 会话置顶或改名 |
| `session_removed` | 会话被删除 |
| `message_removed` | 单条消息被删除 |
| `cleared` | 会话或全部历史被清空 |

### 工作原理

1. **入站捕获** — `@filter.event_message_type(ALL)` 监听所有经过 AstrBot 的消息
2. **出站捕获** — `@filter.after_message_sent()` 记录 Bot 与插件主动发出的消息
3. **@ 还原** — `@filter.on_decorating_result()` 在发送前把 `<mention>` 标签改写成真实 At 段
4. **会话归并** — 群聊统一用 `group:<group_id>` 作为键，同群所有人归一个会话
5. **片段存储** — 消息链序列化为 JSON 片段，前端逐段渲染
6. **实时推送** — bridge SSE 优先，失败降级轮询
7. **身份映射** — 按平台与用户 ID 记录最新昵称，历史消息自动补全显示名

### 技术栈

- **后端**：Python、Quart（AstrBot 内置）、SQLite（WAL 模式）
- **前端**：原生 HTML/CSS/JS、ESM 模块、AstrBot Bridge SDK
- **消息平台**：通过 AstrBot 适配器接入（OneBot v11、QQ Official、Telegram 等）

## 🧪 开发

```bash
# 后端：lint + 单测
pip install ruff pytest
ruff check .
python -m pytest tests -q

# 前端：纯函数测试 + DOM 冒烟
node --test pages/terminal/webchat-lib.test.js
npm install --no-save jsdom && node tests/frontend/dom-smoke.mjs
```

## 📄 License

MIT — 见 [LICENSE](./LICENSE)
