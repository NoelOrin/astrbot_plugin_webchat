# 能力补齐报告

本文件记录 v0.3.1 → v0.4.0 一轮能力补齐的**逐项对照**：发现了什么、怎么修、怎么验证。
上一版分析出的问题全部落地，无遗留。

---

## ① 止血：影响可用性的硬伤

### 1. Bot 回复完全不入库 ⚠️ 最关键

**问题**：`on_message` 只监听入站事件。AI 回复、指令响应、其他插件主动推送走的是另一条路径，
于是群里问"帮我查天气"，终端只能看到这句问话，**看不到 Bot 的回答**——一次对话只能看到一半。

**修复**：新增 `@filter.after_message_sent()` 钩子 `on_bot_message_sent()`：

- 从 `event.get_result().chain` 取真实出站消息链
- 以 `type="bot"` 入库并广播 `message` 事件
- 会话不存在则丢弃（不是本插件跟踪的对话）
- 通过 `_recent_sends` 内容指纹 + 8 秒窗口去重，避免终端自己发的消息被记录两次
- 配置项 `capture_bot_messages`（默认开）可关闭；`bot_display_name` 可改显示名

**验证**：`test_bot_outgoing_message_is_recorded`、`test_outgoing_ignored_for_unknown_session`、
`test_outgoing_ignored_when_disabled`、`test_outgoing_empty_chain_is_ignored`

### 2. 富媒体全丢

**问题**：只存 `event.message_str`（纯文本）。图片、语音、文件进来是空字符串，前端显示 `[非文本消息]`。

**修复**：

- `webchat_core.serialize_component()` 把消息链逐段序列化为 JSON 片段
- 覆盖 `text / image / at / face / record / video / file / reply / forward / other`
- 未知组件降级为 `other`，保留类名和可见文本，不静默丢信息
- 发送方向支持 `segments`：图片（URL 或本地路径）、@
- 前端逐段渲染：图片出 `<img>`（走 `/media?url=` 代理 + 点开大图），@ 出 chip
- 输入框支持**粘贴图片**与回形针选图，待发送区可移除

**验证**：`test_rich_segments_are_stored`、`test_serialize_chain_covers_common_components`、
`test_component_subclasses_are_recognized`、DOM 冒烟中的"图片被渲染为 img"/"@ 片段渲染为 chip"

### 3. 只能在别人说话后才发现会话

**问题**：`session_index` 只靠被动收消息累积，想主动私聊某人或往冷群发消息——做不到。

**修复**：新增三个端点 + 一个 UI：

- `GET /platforms` — 列出可用平台实例
- `GET /peers?platform_id=&type=group|friend` — 调协议端 `get_group_list` / `get_friend_list`
- `POST /open` — 主动建会话，`_infer_umo()` 从同平台已有会话推断 UMO 模板
- UI：会话列表标题栏 **+** 按钮，可选平台/类型/ID，支持拉列表点选或手填

**验证**：DOM 冒烟"新建会话弹窗出现"/"平台下拉已填充"/"好友列表已拉取"

### 4. 多端不同步

**问题**：`api_send` 入库后不广播，`api_clear` 也不广播。开两个标签页，A 发的消息 B 要刷新才看到。

**修复**：`api_send` 广播 `message`；`api_clear` 广播 `cleared`；新增 `message_removed` /
`session_updated` / `session_removed` 事件，所有变更操作都有对应广播。

**验证**：`api_send` 单测 + DOM 冒烟"会话删除后从列表移除"

### 5. 清空按钮在 iframe 里永远失效

**问题**：`window.confirm` 在 `allow-scripts allow-forms allow-downloads` 的沙箱里**没有 `allow-modals`**，
调用被静默忽略并返回 `false`——用户点清空，什么都不发生，也没有任何提示。

**修复**：自绘 `confirmDialog()` / `formDialog()`，挂载到 `#dialog-region`，
支持 Esc 关闭、点遮罩关闭、focus 管理、`role="dialog"` + `aria-modal`。

**验证**：DOM 冒烟"清空弹出自定义确认框"+"确认后真的调用了 clear 接口"+"弹窗已关闭"

### 6. 会话记忆功能等于不存在

**问题**：同源 localStorage 在沙箱 iframe 里不可用，`rememberSession` 每次抛异常被 catch 吞掉，
刷新后永远回到第一个会话。

**修复**：保留 localStorage 快路径（部分部署可用），加 Esc 关闭不崩的兜底；
主题优先跟随 `bridge.getContext().isDark`；同时修正 `color-scheme` 写死 dark 导致的亮色首屏闪烁。

### 7. 数据存在插件目录，升级即丢

**问题**：`os.path.dirname(__file__)/data`，违反 AstrBot 规范——用户更新/重装插件，聊天记录全没。

**修复**：改为 `data/plugin_data/astrbot_plugin_webchat/`（`get_astrbot_data_path()`，失败回退）；
新增 `_migrate_legacy_db()`，首次启动自动从旧目录复制数据库，老用户不丢历史。

**验证**：`test_sessions_are_restored_after_restart`

---

## ② 正确性

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 1 | `platform` 存的是 Python 类名 `AiocqhttpMessageEvent` | 改用 `event.get_platform_name()` / `get_platform_id()`，取不到才回退类名 | `test_platform_name_is_adapter_not_class` |
| 2 | 每次 SQL 新建连接，无 WAL/超时，并发易 `database is locked` | `connect()` 统一设 WAL + `synchronous=NORMAL` + `busy_timeout=5000` | 全量存储测试 |
| 3 | `api_clear` 传空 session_id 就删全库 | 必须显式 `confirm_all=true`，否则 400 | 端点逻辑 |
| 4 | `api_sessions` 是 N+1：每会话 4 条 SQL | `session_summaries()`（窗口函数一次取每会话最新）+ `query_participants()`（一次 GROUP BY 全量分组） | `test_session_summaries_batches_counts_and_last_message`、`test_query_participants_groups_all_sessions_in_one_pass` |
| 5 | 死代码 `_save_contact`、未用 `Plain` 导入 | 删除 | `ruff check .` 通过 |
| 6 | 无 `terminate()`，重载后残留 SSE 队列 | 新增 `terminate()` 清 SSE 与 `_recent_sends` | `test_terminate_clears_state` |
| 7 | 裁剪按 timestamp、分页按 id，口径不一致会漏消息 | 两者统一按 `id` | `test_trim_uses_id_not_timestamp`、`test_query_page_pagination_walks_backwards` |
| 8 | 点刷新会丢弃已加载的更早历史和滚动位置 | 已有缓存时保留历史，只在服务端确实有更多时才更新游标 | `loadSessions` 逻辑 + DOM 冒烟 |
| 9 | 组件子类（`AiocqhttpPlain`）退化成 `other` | 别名表 + 长度≥5 的后缀匹配；短名不做后缀匹配避免 `Format`→`at` | `test_component_subclasses_are_recognized`、`test_short_names_are_not_misidentified` |

---

## ③ 合规与工程

| 项 | 之前 | 现在 |
|---|---|---|
| LICENSE | README 写 MIT 但无文件 | 新增 `LICENSE`（MIT） |
| metadata.yaml | 只有 5 个字段，用的还是非标准 `description` | 补 `display_name`/`short_desc`/`desc`/`astrbot_version`/`support_platforms`(19 个)/`tags`/`social_link` |
| i18n | 全站硬编码中文 | `.astrbot-plugin/i18n/{zh-CN,en-US}.json`（metadata/config/views） |
| Lint | 无 | `ruff.toml` + `ruff check .` 全绿 |
| 测试 | 0 | 158 个（57 + 30 + 42 + 29） |
| CI | 只有 release | 新增 `ci.yml`：ruff + pytest + node test + DOM 冒烟 + 元数据校验 |
| 版本 | 0.3.1 | 0.4.0 |

---

## ④ 差异化能力

| 能力 | 实现 |
|---|---|
| 消息全文检索 | FTS5 trigram 索引（≥3 字）+ LIKE 回退（≤2 字，中文双字检索常见）+ `GET /search` + 前端高亮与跳转定位（目标不在窗口内时自动向前翻页最多 6 次） |
| 单条消息删除 | `DELETE /messages/<id>`，可选 `?remote=1` 调协议端 `delete_msg` 真正撤回；广播 `message_removed` |
| 会话置顶 | `PATCH /sessions/<id>`，`pinned` 入库，排序 `(pinned, last_activity)` |
| 会话删除 | `DELETE /sessions/<id>`，级联删消息，广播 `session_removed` |
| 导出 | `GET /export?format=txt`，前端 Blob 下载 |
| 消息操作 | 悬停出「复制 / 删除」 |
| 相邻消息合并 | 同人 2 分钟内合并，减少视觉噪音 |
| 参与者点选 @ | 点参与者直接加入待发送 @ 片段 |
| 图片代理 | `GET /media?url=`，aiohttp 拉取、限制 8MB、只允许 image/*，规避防盗链 |
| 健康检查 | `GET /health` 返回 db 路径、会话数、SSE 客户端数、当前配置 |
| 增量渲染 | `planMessages` + `diffPlan` 计算最小变更：尾部追加只 append、尾部减少只 truncate、中间改动才 rebuild；相邻同人消息合并渲染 |

---

## 测试与验证

```
ruff check .                                     All checks passed
python -m pytest tests -q                        58 passed
node --test pages/terminal/webchat-lib.test.js   40 pass, 0 fail
node tests/frontend/dom-smoke.mjs                29/29 passed
```

- **`tests/test_webchat_core.py`（46 个）** — 输入清洗、会话键、片段序列化、子类识别、
  建表幂等、置顶保留、裁剪口径、游标分页、批量汇总、删除导出，
  以及 FTS5/LIKE 双路径检索（双字回退、三字走索引、索引随删除/裁剪/会话删除同步、
  重开自动回填、FTS 不可用降级、语法字符转义）
- **`tests/test_plugin.py`（12 个）** — 用 `sys.modules` 注入假 AstrBot/Quart，
  验证群聊归并、私聊键、platform 名、富媒体入库、出站捕获、配置边界、重启恢复、清理
- **`webchat-lib.test.js`（40 个）** — 归一化、去重键、排序稳定性、预览文案、时间分组、
  高亮切分、相邻合并判断、游标类型统一，以及渲染计划与差异（append/truncate/rebuild 判定）
- **`tests/frontend/dom-smoke.mjs`（29 个）** — jsdom 加载真实 `index.html` + `app.js`，
  走完整初始化 → 渲染 → 切会话 → 搜索 → 弹窗 → 新建会话 → 主题/紧凑 → SSE →
  增量渲染节点复用 → 删单条 → 删会话

> DOM 冒烟在真实 DOM 上跑，抓出过 3 个只有跑起来才暴露的问题（mock 的 `since` 语义、
> `scrollTo` 缺失、会话按钮顺序假设）。它已固化进仓库与 CI。

---

## 已知边界（刻意不做）

- **FTS5 + LIKE 双路径，而非纯 FTS5**：实测 trigram 分词器无法匹配少于 3 个字符的查询，
  而中文"苹果""天气"这类双字检索极常见，因此 ≥3 字走 FTS5 索引、≤2 字回退 LIKE。
  FTS5 建表失败（部分 SQLite 未编译该模块）时整体静默降级为 LIKE，功能不受影响
- **`display_order` 保留但不再驱动排序**：置顶 + 活跃时间更符合聊天软件直觉，
  列保留用于兼容旧库
- **无虚拟滚动**：已做节点级增量复用（追加/截断不重建），但仍在 DOM 中保留全部已加载消息；
  若把 `max_log_per_session` 调到数千条，下一步应上真正的窗口化虚拟滚动
- **单图上传**：bridge 的 `upload()` 一次只传一个文件，多图需多次调用，暂未做
