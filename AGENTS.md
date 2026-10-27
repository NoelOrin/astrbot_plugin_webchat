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
  - 17 个 REST 端点，见 `_register_web_apis()` 的 routes 列表
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
  → api_send() → _build_chain(segments)
  → store.add_message(type="sent")      ← 必须先入库
  → 记录 _recent_sends 指纹（乐观锁）
  → context.send_message(send_target, chain)
  → 失败则 delete_message 回滚 + 清锁；成功才广播 message
```

> ⚠️ **顺序不能换：先入库，后发送。**
> 平台会把 Bot 自己发出的消息当入站事件回推，而回推可能就发生在
> `send_message` 的 `await` 里。若先发后存，那个窗口里库里还没有 `sent` 行，
> 「同会话 + 同内容」的查重必然落空，同一条就被记成 `received` + `sent` 两份。
> `test_sent_row_is_written_before_platform_send` 守着这个顺序，
> `test_failed_send_rolls_back_prewritten_row` 守着失败时的回滚。

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

## 虚拟滚动（消息列表）

plan 超过 `VIRTUAL_THRESHOLD`（80）时只渲染视口附近的一段节点：

- `#message-content` 结构固定为 `#virtual-top`（spacer）+ `#message-items` + `#virtual-bottom`
- `computeWindow()`（webchat-lib，纯函数）按「实测行高 + 估算行高」算出
  `{start, end, topPad, bottomPad, offsets}`；`offsets` 是每项的累计偏移，供锚点定位
- 行高渲染后回写到 `state.heights`（key = plan item key），图片加载/窗口 resize 后下次滚动自然重算
- 滚动用 rAF 节流；贴底时 `pinToEnd` 让窗口钉在尾部
- **每次窗口平移都用锚点回正 `scrollTop`**：`windowAnchor()` 记住视口顶部压在哪一项、
  项内偏移多少，渲染+测量后再用 `anchorScrollTop()` 把它放回原位置
- 触顶自动加载更早消息（`AUTO_LOAD_TOP_PX`），同一段「贴顶」只补一页，离开顶部后重新武装
- 小会话（≤80 条）仍走 `diffPlan` 全量增量渲染，行为与旧版一致

> ⚠️ 虚拟滚动的「跳一大截」几乎都来自**估算行高的误差**：窗口往上滑一条，
> 顶部 spacer 按 `forecast` 减去一行，但新渲染进来的那条节点是真实行高，
> 图片消息能差出 300px 以上，整屏内容就会突然位移。锚点回正就是补这个差额；
> `overflow-anchor: none` 是关掉浏览器自带的滚动锚定，否则它会和上面的补偿叠加成二次跳动。
> `webchat-lib.test.js` 的「窗口平移时按锚点回正」用混合行高模拟了一整串平移。
> 补行高必须在 `measureRenderedHeights()` **之后**重新读 `state.heights`——这一轮新渲染的项
> 行高刚测出来，用渲染前的快照会补偿成错的方向。

> ⚠️ 所有 DOM 操作的目标是 `#message-items`，**不是** `#message-content`。
> spacer 是 `#message-content` 的固定子节点，`diffPlan` 的 append/truncate 若作用在
> `#message-content` 上会把 spacer 一起删掉，滚动高度立刻归零。
> `dom-smoke` 的「增量渲染复用已有节点」断言依赖 `#message-items`。

> ⚠️ `loadOlderMessages()` 在虚拟滚动下必须**先把 `scrollTop` 抬高新插入内容的估算高度**
> 再 render，否则窗口会按旧位置重算，视口内容整体错位。

> ⚠️ 从虚拟窗口退回全量渲染（消息删到 ≤80 条）时不能走 `diffPlan`：DOM 里只有一段窗口节点，
> 增量修补会对不上，必须整树重建。

## 引用回复

- 悬停消息 →「引用」→ 输入框上方出现引用条；发送时 segments 首部插入
  `{type: "reply", target: <platform_message_id>, text: <预览>}`
- 入站 / Bot 出站的 reply 片段会由 `_enrich_reply_quotes()` 补上
  `quote: {sender, text}`（`store.quote_of()` 按 platform_message_id 反查，截 120 字）；
  前端渲染成「回复 小明：…」引用块，查不到时退回本地缓存或占位提示
- 后端 `_build_chain()` 用宿主 `Reply(id=…)` 还原；宿主没有该组件、或 target 是
  `astrbot:<id>`（历史导入的本地合成 id）时静默降级为普通消息
- `api_send` 把 reply 片段提到链首（协议端按链首识别回复关系）
- 只剩 Reply 的链会补 `\u200b` 明文段，否则宿主 respond 阶段整条丢弃
- `_send_fingerprint()` **忽略** reply 片段：不同适配器回推时可能剥掉 Reply，
  参与比较反而会把同一条记成 `sent` + `bot` 两份

## 失败重发与会话草稿

- 发送失败不再回滚消失：本地留一条 `failed: true` 的气泡，带「重试 / 丢弃」
- 重试复用原 payload（`message.retryPayload`），成功后移除本地行并插入服务端返回行
- 草稿：输入防抖 400ms 写 `localStorage`（`astrbot_plugin_webchat_draft:<sessionId>`），
  切会话时同步保存/恢复，发送成功清除；localStorage 不可用时退化为 `state.drafts` 内存表
- 附件（dataURL）体积大，不持久化

## 会话列表分页

- `GET /sessions?limit=&offset=` 返回 `{sessions, total, has_more}`；不传 `limit` 仍返回全量
- 排序为「置顶 → 最近活跃 → 会话键」，分页切片稳定，不会因并列项抖动而重复/漏项
- 前端首屏 `SESSION_PAGE_SIZE`（50）条，列表底部「加载更多会话」+ 触底自动补页
- 首页加载才清理已删除会话缓存；翻页只做追加，不能把别的页删掉

## 合并转发（聊天记录）

- `serialize_component()` 兼容宿主的三种形态：
  - **单个 `Node`**：本身是一条聊天记录，`content` 是消息组件列表
  - **`Nodes` / `Forward` 容器**：`nodes` / `content` 是一组 `Node`
  - **只有 id**：内容留给点开时再拉
  统一序列化成 `{type:"forward", text:"[聊天记录]", id?, messages:[{name, uin, segments}]}`
- `_component_kind()` 会把 Node/Nodes/Forward 归一成 `forward`，判断「单个 Node」必须看
  `type(component).__name__.lower() == "node"`，否则会把它当容器、把组件当聊天记录
- 宿主组件丢 id 时，`_attach_forward_ids()` 从 `message_obj.raw_message` 的 OneBot 段 /
  CQ 码（`[CQ:forward,id=…]`）里把 id 捞回来（`webchat_core.extract_forward_ids`）
- 点 chip 时：有内嵌 `messages` 直接展示；只有 id 则调 `GET /forward?session_id=&id=`，
  后端 `_fetch_forward_messages()` 尽力调 `get_forward_msg`，失败返回空列表
- 前端 `infoDialog()` 渲染只读详情，Esc / 点遮罩 / 「关闭」都能退出
- 有结构化片段（@/表情/语音/文件/转发）时，气泡正文**不再**回退显示 `messageText()`
  的占位文案，避免「[合并转发]」在 chip 和正文里各出现一次

> ⚠️ 加这个功能**之前**入库的转发记录只存了 `[合并转发]` 文案，没有 id、没有嵌套内容，
> 永远无法展开；只有新入库的转发才能点开。别为了「修旧数据」去拿 `platform_message_id`
> 当转发 id，两者不是一回事。

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

`_record_inbound()` 里的处理（**两把锁，缺一不可**）：

```python
if self._sender_is_self(event, sender_id):
    if self._consume_pending_echo(session_key, content):   # 内存锁，命中即解锁
        return None
    if self._is_duplicate_inbound(...):                    # 库查重，兜底
        return None
```

- **只对 `sender_id == event.get_self_id()` 生效**：其他用户重复发同样的话必须全部保留
- 判断前先比 `sender_id`，是 Bot 才查重，普通消息零额外查询
- 查重时**忽略发送者**（`ignore_sender=True`）：终端发出时 `sender_id` 为空，
  平台回推时是 Bot 的 ID，只比发送者会漏判
- 窗口 `INBOUND_DEDUPE_WINDOW = 8s`，只按「同会话 + 同内容 + 时间窗」判定

> ⚠️ **内存锁必须先查，且命中即释放**。
> 平台回推可能就发生在 `api_send` 那次 `send_message` 的 `await` 里。现在
> `sent` 行是在发送**之前**写入的（见「发送」一节的顺序约定），所以库查重也能命中；
> 但内存锁仍然是第一道、最快的判断，且它命中后 `pop()` 立刻解锁——
> 回声既然已经处理完，锁留在 8 秒窗口里只会继续误吞后面的真实消息。
> `test_echo_arriving_before_sent_row_is_deduped`、`test_echo_releases_optimistic_lock`
> 两条测试分别守着「竞态」和「自动解锁」，撤掉修复会立刻失败。

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

- 端点: `GET /api/plug/astrbot_plugin_webchat/stream?session_id=<会话>`
- 事件: `connected`、`hello`、`ping`、`message`、`message_summary`、`session`、`session_updated`、`session_removed`、`message_removed`、`cleared`、`sessions_discovered`
- keepalive: 每 `SSE_KEEPALIVE`（15 秒）发一个 **`ping` 事件**（不是注释行）
- 前端优先 `bridge.subscribeSSE("stream?session_id=…", ...)`；bridge 无此方法才退回原生 `EventSource`
- 全部失败时降级轮询（2.5 秒）

### 会话主题订阅

**一条流只推一个会话的完整消息**，切换会话时重建连接：

| 客户端 | 收到的 `message` |
|---|---|
| 不带 `session_id`（老客户端） | 全量，行为不变 |
| `session_id` 与消息一致 | 全量，含 segments |
| `session_id` 不一致，或显式空串 | `message_summary`（截断 120 字，无 segments） |

- 主题存在 `_sse_topics[cid]`，与 `_sse_clients[cid]`（队列）分开维护，生成器 `finally`、
  `terminate()`、队列满淘汰三处都要一起清，否则会残留主题
- 生命周期类事件（`session_removed`、`cleared`、`sessions_discovered`）**始终全局广播**，
  否则后台会话改名/删除后其它窗口不刷新
- 前端用 `message_summary` 只更新未读 / 列表预览 / 计数，`summaryIds` 记下
  `session:messageId`；同一 id 的完整消息后来到达时**不再重复计数**，只补全正文

> ⚠️ **切换会话必须重建连接，不能只改订阅参数。** `connectStream()` 里先
> `disposeStream()` 再按 `state.currentSessionId` 拼端点；`state.streamGeneration`
> 每次递增，旧连接的回调（bridge 的迟到 resolve、原生 EventSource 的 error/消息）
> 一律靠代次比对丢弃。快速连点会话时，迟到订阅会被立刻 `unsubscribeSSE` 掉，
> 不能让旧连接改掉 `state.connected` 或塞进旧会话的消息。
> `dom-smoke` 的「切换会话重建 SSE / 旧连接消息被忽略 / 迟到订阅立即被退订」守着这三条。

> ⚠️ **重建会留下一个空窗**：旧流已关、新流未连上时到达的消息不会重放。所以
> `connectStream()` 先记下 `maxKnownMessageId()` 当 `streamCatchupAfter`，新流
> `onOpen` 后先按它拉一轮 `/updates`（全局补拉，覆盖后台会话），再按 `/history`
> 把后台期间只收到摘要的当前会话消息补全。首次连接不补——历史刚由 `loadSessions()` 载入。
> 补历史必须走「只合并、不计数」的路径：会话的 `message_count` 已经包含那些行，
> 逐条当新消息处理会把总数从 200 撑成 350，并让未读和最后一条预览一起错位。

> ⚠️ **撤回一条只有摘要的消息时，`removeMessageLocally()` 找不到缓存行会直接 return。**
> 必须同时认 `summaryIds`：删掉标记、减一次计数、未读数回落、预览回退到上一条。
> `clearMessageCaches()` / `handleCleared()` / `handleSessionRemoved()` 都要清掉对应
> 标记，否则长期后台流量会让这个集合无限增长。

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

> ⚠️ **不要在 `selectSession()` 里因为 `state.connected === false` 就 `startPolling()`。**
> `boot()` 的顺序是「先 `loadSessions()`（内部会选初始会话），再 `connectStream()`」，
> 选会话时 SSE 还没开始连，`connected` 必然是 `false`。在这里兜底轮询会让终端
> **一启动就降级轮询**，之后即使 SSE 连上，第一轮轮询也已经白跑。
> 轮询只由流生命周期触发：连接超时 / `onError` / `onerror` / 心跳假死时各自调用。
> `dom-smoke` 的「启动阶段不提前降级轮询」守着这条。

> ⚠️ **降级轮询不是终点。** SSE 不可用而进入轮询后，`checkStreamHealth()` 每
> `SSE_RETRY_MS`（60 秒）会再 `connectStream()` 试一次 SSE——插件重载、临时断网结束、
> 反向代理恢复后都能自动切回实时推送，而不是永久停在 2.5 秒轮询上。
> 重试期间状态保持「轮询连接」，SSE 一旦 `onOpen` 就 `stopPolling()` 并显示「实时连接」。


## 前端约束（iframe 沙箱）

插件视图跑在 `allow-scripts allow-forms allow-downloads` 的 iframe 里，**没有** `allow-modals`、**没有**同源 localStorage：

- ❌ 不要用 `window.confirm` / `alert` — 会被静默忽略。用 `confirmDialog()` / `formDialog()`
- ⚠️ `localStorage` 可能抛异常，读写一律 try/catch 并回退
- ⚠️ bridge 只有 `apiGet` / `apiPost`（新版本另有 `apiFetch`）；PATCH/DELETE 走后端 `?method=` 覆盖，后端 `_effective_method()` 解析
- 图片走 `/media?url=` 代理，规避防盗链与混合内容

## bridge 缺失时的自愈与诊断

视图的所有后端能力都经 `window.AstrBotPluginView`（旧版 `AstrBotPluginPage`）转发，
这个全局由 AstrBot 返回视图 HTML 时注入的 `/api/plugin/page/bridge-sdk.js` 提供。

**三种典型故障**，以前都只丢一句「未检测到 AstrBot 页面 Bridge」：

| 现象 | 真正原因 |
|---|---|
| 页面在浏览器里直接打开 | 没有 Dashboard 父页面 → 没有注入、也没有人能响应 postMessage |
| `bridge-sdk.js` 404 | WebUI 挂在反向代理子路径（`/astrbot/`）下，宿主注入的是**绝对路径** |
| `bridge-sdk.js` 401 | Dashboard 会话失效，脚本拿不到鉴权，`<script>` 不执行 |

`boot()` 现在的顺序：`ensureBridge()` → 拿到 bridge 后再并行「等上下文 + 探 health」→ 再加载会话：

- `ensureBridge()` 先看宿主是否已注入；没有就按 `bridgeSdkUrl(location.pathname)`
  自己补加载一次。该纯函数按**第一个 `/api/`** 切出部署前缀
  （`/astrbot/api/v1/plugins/...` → `/astrbot/api/plugin/page/bridge-sdk.js`），
  所以子路径部署能自愈；地址是算出来的，**不要**改成写死 `/api/plugin/page/bridge-sdk.js`
- 补加载只在 `http(s)` 下进行（`file://` 没有同源后端），且每个地址只试一次
- `probeBridgeHandshake()` 用 `health` 探一次握手：bridge 存在但宿主不回话时，
  后面的每个 `apiGet` 都会永久挂起，界面就会无声地卡在「正在连接」。
  **只有超时**才算握手失败；宿主回了错误（端点自己抛异常）说明 bridge 是通的
- 失败分支必须写清楚怎么查：状态栏文案 + `console.error` 结构化诊断
  （是否在 iframe、页面地址、宿主注入情况、补加载地址）+ 一条长驻 12 秒的 toast
- `bindBridgeContext()` 不能在 `bindEvents()` 里订阅 `onContext`：那时 bridge 还可能不存在，
  自愈后拿到的 bridge 会漏掉主题 / 语言变化

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
- 图片走 `/media` 代理，浏览器地址栏只有代理地址；大图预览底部固定显示**原始链接**
  （`#lightbox-link`），并提供「复制链接 / 打开原图」，原始地址存在 `state.lightboxSource`

> ⚠️ 占位**不能**写成 `<img src=… >` + CSS 隐藏——浏览器照样会下载，等于没省。
> `dom-smoke.mjs` 里 `默认不自动渲染图片` 这条断言就是防这个的。

> ⚠️ 手动点开的图片必须记在 `state.loadedImages`（按图片地址），**不能只 replaceWith 改 DOM**。
> 虚拟滚动的窗口每次重渲染都会 `replaceChildren`，DOM-only 的「已加载」状态会立刻丢失，
> 图片被打回占位。工具栏整体关掉时才清空这个集合（关闭 = 全部折叠）。

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
- 历史导入**只用 `platform_message_id="astrbot:{id}"` 判重**，水位不参与「是否导入」判定：
  `meta` 里的 `history_import:v{2}:{platform_id}:{scope}` 只记录「导到哪了」
- `AstrBot` 的 `platform_message_history` 默认**关闭**（`group_message_history_enable=false`），
  多数环境导入为 0 条属正常降级；插件会在导入 0 条时打一条 info 提示去开这个开关
- `GET /sessions?with_history=1` 只对 `message_count > 0` 的会话查历史，几百个空会话不会拖慢首屏

> ⚠️ **水位不能当过滤条件**。旧版本（0.4.1）只取 `page=1`（最新一页）就把水位推到最新 id，
> 之后再用 `row_id <= watermark` 跳过——结果更早的历史**永远**补不回来，
> 用户看到的就是「没有自动同步消息」。现在水位只写不挡，靠 `platform_message_id` 幂等去重；
> 键名带 `v2` 让被污染的旧水位一次性失效。
> `test_high_watermark_does_not_block_older_history`、`test_legacy_watermark_key_is_ignored`
> 守着这两条，撤掉修复会立刻失败。

手动重跑：前端会话列表的「自动发现」按钮 → `POST /discover`。

## 测试

| 命令 | 覆盖 |
|---|---|
| `ruff check .` | 后端 lint |
| `python -m pytest tests -q` | 存储/序列化 + 插件行为（假宿主）|
| `node --test pages/terminal/webchat-lib.test.js` | 前端纯函数 |
| `node tests/frontend/dom-smoke.mjs` | jsdom 端到端（需 `npm i jsdom`）|
| `node tests/frontend/bridge-missing.mjs` | bridge 缺失 / 子路径自愈（需 `npm i jsdom`）|

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
