---
description: AI agent 指南 — 项目架构、数据流、关键约定
---

# astrbot_plugin_webchat — AGENTS.md

## 项目概览

AstrBot 插件，提供一个 Web 终端 UI，通过 AstrBot 消息平台**双向**收发消息并支持历史检索。

**技术栈**: Python (Quart) 后端 + SQLite + 纯前端 JS (Vanilla, ESM module)

## 架构

### 分层（重要）

| 文件 | 职责 | 依赖 |
|---|---|---|
| `webchat_core.py` | 纯逻辑：片段序列化、SQLite 存储、会话键 | 仅标准库，**不 import AstrBot** |
| `main.py` | 插件入口：事件监听、Web API、SSE、生命周期 | AstrBot SDK + Quart + webchat_core |
| `pages/terminal/webchat-lib.js` | 前端纯函数：去重、排序、格式化、高亮 | 无（不碰 DOM/bridge） |
| `pages/terminal/app.js` | 状态、渲染、SSE、弹窗 | DOM + bridge + webchat-lib |

**这条边界是刻意的**：`webchat_core.py` 和 `webchat-lib.js` 能脱离宿主单测，改动逻辑时优先放这里。

### 后端 (`main.py`)
- **`Plugin(Star)`**
  - `on_message()` → `_record_inbound()`：入站消息入库并广播
  - `on_bot_message_sent()`（`@filter.after_message_sent()`）：出站消息入库
  - `_ensure_session()`：幂等注册会话，新会话广播 `session` 事件
  - 16 个 REST 端点，见 `_register_web_apis()` 的 routes 列表
  - `_sse_broadcast()` / `_broadcast_soon()`：SSE 广播（后者在无事件循环时安全降级）
  - `terminate()`：清理 SSE 与缓存
- **数据存储**: `data/plugin_data/astrbot_plugin_webchat/astrbot_plugin_webchat.db`
  - 启动时 `_migrate_legacy_db()` 从旧插件目录复制数据库
  - WAL + `busy_timeout=5000`，每次操作独立连接

### 前端 (`pages/terminal/`)
- `index.html` — 结构 + 首屏主题/紧凑模式内联脚本（避免闪烁）
- `webchat-lib.js` — 纯函数（有测试）
- `app.js` — 控制器，`window.AstrBotPluginView`（回退 `AstrBotPluginPage`）

### 路由前缀
AstrBot 在 `/api/plug/` 下注册插件 API。bridge 自动加前缀；原生 `EventSource` 需完整路径：
```
/api/plug/astrbot_plugin_webchat/stream
```

## 核心数据流

### 入站
```
AstrBot event → on_message() → _record_inbound()
  → 群聊: session_key = "group:{group_id}"   私聊: session_key = umo
  → _ensure_session() → store.upsert_session()
  → store.add_message(type="received") → 按 id 裁剪
  → _broadcast_soon("message", saved)
```

> ⚠️ `on_message` **必须**带 `@_message_listener()`
> （即 `filter.event_message_type(EventMessageType.ALL)`）。
> 宿主 `waking_check` 里写着 `if len(handler.event_filters) == 0: continue`——
> 没装饰器的方法根本不会被派发，终端只会显示启动时导入的历史，
> 群里的新消息一条都收不到。`test_inbound_listener_is_registered` 专门守着这条。


### 出站（Bot / 插件主动推送）
```
after_message_sent 钩子 → on_bot_message_sent()
  → serialize_chain(event.get_result().chain)
  → 会话不存在则丢弃；命中 _recent_sends 指纹则丢弃（终端已入库）
  → store.add_message(type="bot") → 广播
```

### 发送
```
前端 apiPost("send", {session_id, message, segments})
  → api_send() → _build_chain(segments) → context.send_message(send_target, chain)
  → 记录 _recent_sends 指纹 → store.add_message(type="sent") → 广播
```

## 数据库

### sessions 表
| 列 | 说明 |
|---|---|
| id | session_key（群聊 `group:{group_id}`，私聊 `umo`）|
| platform | 适配器名（如 `aiocqhttp`，来自 `get_platform_name()`）|
| platform_id | 平台实例 ID |
| group_id / group_name | 群号 / 群名 |
| is_group | 是否群聊 |
| send_target | 发送消息用的原始 UMO |
| peer_id / peer_name | 统一展示用的对端 ID / 名称 |
| pinned | 置顶 |
| display_order | 发现顺序（排序实际由 pinned + 活跃时间决定）|
| created_at | 主动建会话的时间 |

### messages 表
| 列 | 说明 |
|---|---|
| id | 自增 PK（**分页与裁剪的唯一口径**）|
| session_id | 关联 sessions.id |
| type | `received` / `sent` / `bot` |
| sender / sender_id | 发送者昵称 / UID |
| content | 纯文本（从 segments 折叠而来）|
| segments | JSON 片段数组（图片、@、表情…）|
| platform / platform_id | 平台信息 |
| platform_message_id | 平台消息 ID（撤回用）|
| timestamp | unix 时间戳 |

**裁剪策略**: `DELETE ... WHERE id NOT IN (SELECT id ... ORDER BY id DESC LIMIT {max_log})`

> 历史版本按 `timestamp` 裁剪、按 `id` 分页，两套口径不一致会漏消息。**统一用 id，不要再改回 timestamp。**

## 检索（FTS5 + LIKE 双路径）

`store.search_messages()` 按关键词长度分流：

| 条件 | 路径 |
|---|---|
| 长度 ≥ `FTS_MIN_CHARS`（3）且 FTS 可用 | `messages_fts` trigram 索引 |
| 长度 ≤ 2 | LIKE 回退 |
| FTS 建表失败 | 全程 LIKE |

**为什么不是纯 FTS5**：trigram 分词器匹配不了少于 3 个字符的查询，
中文"苹果""天气"双字检索是主流场景，必须回退。

> ⚠️ 不要改成 `unicode61`——它把整句当一个 token，中文子串完全搜不到（实测）。

索引由 `messages_fts_insert/delete/update` 三个触发器自动同步，
新增写入路径不需要手动维护索引。

## 增量渲染

`planMessages(messages, sessionId)` 生成纯数据渲染计划，`diffPlan(prev, next)`
比对后决定最小变更：

| 场景 | 模式 | DOM 操作 |
|---|---|---|
| 尾部追加新消息 | `append` | 只 append 新增节点 |
| 尾部删除消息 | `truncate` | 只移除尾部节点 |
| 中间增删改 / 切换会话 | `rebuild` | `replaceChildren` |

节点签名包含 type / timestamp / senderName / content / segments 长度 / pending，
任一变化都会触发 rebuild。`state.renderPlan` 缓存上一轮计划，
`state.renderSessionId` 用于切换会话时强制重建。

## 群聊会话合并

群聊消息用 `group:{group_id}` 作为 session_key，同群所有人归一个会话；发送时用 `send_target`（原始 UMO）路由。`_extract_group_name()` 从 `raw_message` 多路兜底取群名。

## 组件识别

`serialize_component()` 按**类名**判断组件类型，不 import 宿主组件类：
- 先精确匹配 `_COMPONENT_ALIASES`
- 再对长度 ≥5 的别名做**后缀匹配**（兼容 `AiocqhttpPlain` 这类子类）
- 短别名（`at` / `face`）只认 **CamelCase 结尾**（`TextAt`、`LarkFace`），大小写敏感
- 都不命中则降级为 `other`，保留类名与可见文本

> 短别名必须保留大写边界：否则 `Format` 会被当成 `at`、`Surface` 当成 `face`。

## @ 提及还原（mention）

**问题**：LLM 会用 `<mention id="123"/>`、`<mention>小明</mention>`、`<at qq="1"/>`
这类占位标签表达「@某人」，但 AstrBot 核心层**没有解析它们的约定**——标签会原样发到
聊天平台，用户看到字面 `<mention>`，被 @ 的人也收不到提醒。

**修复分三层**（缺一层就会漏）：

| 层 | 位置 | 作用 |
|---|---|---|
| 发送前 | `on_before_send`（`filter.on_decorating_result`） | 把标签改写成真实 `At`，**这是根治点** |
| 入库兜底 | `on_bot_message_sent` | 流式输出不经过前一个钩子，这里再改写一次 |
| 终端发送 | `api_send` | 手打 `@昵称` / `@QQ号` 也解析成 `At` |

**解析规则**（`webchat_core.text_to_segments`）：

- `has_mention_markup()` 快速判断，普通消息零开销直接放行
- 认 `<mention>`/`<at>` 的四类写法：成对、冒号、属性、自闭合；`id/qq/user_id/uid/target` 都算 ID
- 昵称 → ID 由 `MessageStore.mention_resolver(session_key)` 解析：**先本会话参与者，再全局联系人**
  （同名不同人时优先本会话，避免 @ 错人）
- **解析不出来就退化成 `@昵称` 明文**，绝不允许尖括号标签外泄
- `@全体成员` / `@all` 映射成 `target="all"`，发消息时用宿主的 `AtAll` 组件而不是 `At(qq="all")`
- 手打 `@` 只在**解析成功时**才转 `At`；邮箱（`a@b.com`）、`@ 空格` 不受影响
- 手打的 5~12 位纯数字当 QQ 号直接转 `At`；更短的数字按正文处理，避免误 @
- 标签必须**带边界**：`<` 后紧跟 `mention`/`at` 且后面是空白、`/`、`>` 或 `:` 才算标签。
  少了这条边界，`<atom>`、`<attachment>`、`<at most 3 items>` 这类正文会被整段吞掉
- 属性段只认 `key=value`；`<at name="小明"/>` 这种只给昵称的写法也会尝试解析

> ⚠️ 改写后若整条链**只剩 Reply/At**（引用 + @ 是常见组合），必须补一个 `\u200b` 明文段。
> AstrBot 的 respond 阶段会跳过「只含 Reply/At」的消息链（`astrbot/core/pipeline/respond/stage.py`），
> 否则消息被静默丢弃。

> ⚠️ `_send_fingerprint()` 把 @ 折成 `@名` 并去掉全部空白，用于终端回声去重。
> 文本被拆成 `At + 文本` 后表示会变，不归一化就会让同一条消息入库两次。

## 入站乐观锁（只对 Bot 自身生效）

部分平台会把 Bot 在自己其它端发出的消息**当作入站事件再推一遍**，
于是同一条消息被记成两条（一条 `sent`、一条 `received`），界面上就重复了。

`_record_inbound()` 里的处理：

```python
if self._sender_is_self(event, sender_id) and self._is_duplicate_inbound(...):
    return None
```

- **只对 `sender_id == event.get_self_id()` 生效**：其他用户重复发同样的话必须全部保留
- 判断前先比 `sender_id`，是 Bot 才查重，普通消息零额外查询
- 查重时**忽略发送者**（`ignore_sender=True`）：终端发出时 `sender_id` 为空，
  平台回推时是 Bot 的 ID，只比发送者会漏判
- 窗口 `INBOUND_DEDUPE_WINDOW = 8s`，只按「同会话 + 同内容 + 时间窗」判定

> ⚠️ 不要把去重放宽到所有用户——会吞掉「刷屏」「+1」这类正常重复消息，
> `test_other_users_duplicates_are_always_kept` 专门守着这条。

## 切换会话必须整树重建

`renderMessages()` 里**不要**用 `diffPlan([], nextPlan)` 来表达「切换会话」：

```js
// ❌ 错的：oldPlan 为空时 common === 0 === oldPlan.length 恒成立，永远返回 append
const previousPlan = state.renderSessionId === sessionId ? state.renderPlan : [];
```

空计划会被判成 `append`，旧会话的节点一个都不删，新会话里就会残留上一个会话的消息。
正确做法是直接 `replaceChildren` 重建整棵树。

## SSE 实时推送

- 端点: `GET /api/plug/astrbot_plugin_webchat/stream`
- 事件: `connected`、`hello`、`ping`、`message`、`session`、`session_updated`、`session_removed`、`message_removed`、`cleared`、`sessions_discovered`
- keepalive: 每 `SSE_KEEPALIVE`（15 秒）发一个 **`ping` 事件**（不是注释行）
- 前端优先 `bridge.subscribeSSE("stream", ...)`；bridge 无此方法才退回原生 `EventSource`
- 全部失败时降级轮询（2.5 秒）

> ⚠️ keepalive 必须是真事件。bridge 在宿主侧解析 SSE，注释行（`: ping`）没有 data
> 会被丢弃，iframe 收不到；于是「插件重载 / 服务重启掐断流」时前端仍以为连接活着
> （`state.connected === true`），既不重连也不降级轮询，只能手动刷新页面才能再收到消息。
> 现在前端用 `checkStreamHealth()` 判活：超过 `STREAM_STALE_MS`（45 秒）没有任何帧就主动重连。

> ⚠️ **SSE 必须返回宿主的 `astrbot.api.web.stream_response`，不能用 Quart `Response`。**
> AstrBot 4.x 的 dashboard 统一分发插件 API，Quart Response 会被
> `_coerce_view_result` → `_quart_response_to_starlette` 处理，而后者是
> `await response.get_data()`——**把整个响应体读成一个 bytes**。SSE 生成器永不结束，
> 请求就永久挂起，浏览器一个字节都收不到（前端表现为一直「正在连接」）。
> 这是 0.3.1 当年额外 `app.add_url_rule` 想绕开的「包装」；现在宿主给了
> `stream_response`，返回 Starlette `StreamingResponse`，分发层原样透传。
> `test_sse_returns_host_streaming_response` 守着这条，别退回去。

> ⚠️ 同理，前端 `bridge.subscribeSSE` 可能永远不 resolve（服务端被缓冲时 fetch 等不到
> 响应头）。`connectBridgeSSE` 用 `withTimeout`（8 秒）兜底：超时即退回原生 EventSource
> 并把轮询先跑起来，别让界面停在「正在连接」而什么都不做。


## 轮询降级（`/updates`）

SSE 不可用时前端每 2.5 秒拉一次 `GET /updates?after_id=<全局消息 id>`，
后端 `store.messages_after()` 一次带回**所有会话**的新消息（id 升序）。

- **游标是全局消息 id，不是 timestamp**：同一秒多条、发送端时钟回拨都不会漏
- 一次请求覆盖所有会话，冷会话的未读数/预览也会更新——旧实现只轮询当前会话，别人的消息永远不会来
- 游标只由轮询结果推进；本地发送 / SSE 到达的消息**不**推进游标，
  否则可能跳过并发插入的那几条（`startPolling()` 时用 `maxKnownMessageId()` 起跳）
- 后端按 `MAX_PAGE_SIZE` 分页，返回 `has_more`；前端最多连续补拉 `POLL_MAX_DRAIN` 次


## 前端约束（iframe 沙箱）

插件视图跑在 `allow-scripts allow-forms allow-downloads` 的 iframe 里，**没有** `allow-modals`、**没有**同源 localStorage：

- ❌ 不要用 `window.confirm` / `alert` — 会被静默忽略。用 `confirmDialog()` / `formDialog()`
- ⚠️ `localStorage` 可能抛异常，读写一律 try/catch 并回退
- ⚠️ bridge 只有 `apiGet` / `apiPost`（新版本另有 `apiFetch`）；PATCH/DELETE 走后端 `?method=` 覆盖，后端 `_effective_method()` 解析
- 图片走 `/media?url=` 代理，规避防盗链与混合内容

## 配置 (`_conf_schema.json`)

```json
{
  "max_log_per_session": { "type": "int", "default": 200 },
  "capture_bot_messages": { "type": "bool", "default": true },
  "bot_display_name": { "type": "string", "default": "Bot" },
  "convert_mention_tags": { "type": "bool", "default": true },
  "parse_bare_at": { "type": "bool", "default": true },
  "auto_discover_sessions": { "type": "bool", "default": true },
  "auto_discover_include_friends": { "type": "bool", "default": true },
  "import_astrbot_history": { "type": "bool", "default": true },
  "load_images": { "type": "bool", "default": false }
}
```

## 图片默认不加载

图片走 `/media?url=` 代理，一张图就是一次服务端外网请求。默认**全部折叠**：

- 消息里的图片渲染成 `.message-image-placeholder` 按钮，**不产生任何请求**
- 点占位按钮只加载那一张（替换成 `<img>`），不改变全局开关
- 工具栏 `#btn-images` 是全局开关，`aria-pressed` 反映状态，记忆在 localStorage
- 插件配置 `load_images`（默认 false）只作为**初始默认值**；用户手动切过就以本地记忆为准
- 前端从 `/health` 的 `load_images` 读默认值，只读一次（`syncImagePreferenceFromConfig`）

> ⚠️ 占位**不能**写成 `<img src=… >` + CSS 隐藏——浏览器照样会下载，等于没省。
> `dom-smoke.mjs` 里 `默认不自动渲染图片` 这条断言就是防这个的。

## 平台支持范围

**仅支持 QQ**：`aiocqhttp`（OneBot v11）、`qq_official`、`qq_official_webhook`。

- `metadata.yaml` 的 `support_platforms` 只列这三个，**不要再加**未验证的适配器
- 其它平台（Telegram / Discord / Slack…）的 At 语义、群成员接口、UMO 格式都不同，
  代码里虽有通用兜底，但没有实测过，不对外声明支持
- 新增平台支持时，必须先在该平台实机验证「收发 + @ + 自动发现」三件事，再改声明

## 自动发现（会话预填充）

**问题**：插件靠入站消息被动发现会话，全新安装时终端是空的，得等群里有人说话。

**方案**：`@filter.on_astrbot_loaded()` → `discover_and_import()`，后台任务，不阻塞启动。

| 来源 | 做法 |
|---|---|
| 平台群列表 | `get_group_list` → **全部**建会话 |
| 平台好友列表 | `get_friend_list` → **仅活跃好友**建会话（有 AstrBot 历史或本插件已有消息） |
| AstrBot 历史 | `context.message_history_manager.get(platform_id, user_id, page=…)` → 逐页取完后增量导入 |

**关键约定**：

- `user_id` 就是 UMO，等于本插件的 `send_target`，两边天然对齐
- `platform` 存**适配器名**（`aiocqhttp`），`platform_id` 才是实例 ID；
  `api_open_session` 曾经把两者写成同一个值，已修
- 已存在的会话**只补空缺**（`_fill_discovered_gaps`），不覆盖用户改过的群名/置顶
- 历史导入靠 `meta` 表的 `history_import:{platform_id}:{scope}` 水位 + `platform_message_id="astrbot:{id}"` 双保险，
  重复跑不会重复入库
- AstrBot 的 `platform_message_history` 默认**关闭**（`group_message_history_enable=false`），
  多数环境导入为 0 条属正常降级
- `GET /sessions?with_history=1` 只对 `message_count > 0` 的会话查历史，几百个空会话不会拖慢首屏

手动重跑：前端会话列表的「自动发现」按钮 → `POST /discover`。

## 测试

| 命令 | 覆盖 |
|---|---|
| `ruff check .` | 后端 lint |
| `python -m pytest tests -q` | 存储/序列化 + 插件行为（假宿主）|
| `node --test pages/terminal/webchat-lib.test.js` | 前端纯函数 |
| `node tests/frontend/dom-smoke.mjs` | jsdom 端到端（需 `npm i jsdom`）|

`tests/test_plugin.py` 用 `sys.modules` 注入假 AstrBot/Quart，因此**不需要装 AstrBot 就能跑**。
新增宿主 API 依赖时，记得同步在 `_install_fake_astrbot()` 里补桩。

## 发布流程

版本号在 `metadata.yaml`:
- 手动改 `version: x.x.x` → 推 main
- GitHub Actions 自动检测变更 → 创建 `v{x.x.x}` tag + Release (自动生成 changelog)
- `ci.yml` 在每次 push/PR 跑 lint + 三套测试 + 元数据校验

## 代码约定

- **运行时零外部依赖**（除 AstrBot SDK + Quart）；`aiohttp` 按需延迟导入
- 群聊 session_key 始终以 `group:` 开头，不要依赖 UMO 格式解析
- `_ensure_session()` 幂等 — 已存在时只更新非空字段，不覆盖置顶/自定义名
- 消息读取一律按 id 排序做游标，避免 timestamp 重复或乱序导致漏数据
- 前端缓存 `state.messages` / `state.history` 是唯一真相，切换会话不再请求
- 刷新会话时保留用户已翻出的更早历史，不要整段覆盖
