---
description: AI agent 指南 — 项目架构、数据流、关键约定
---

# astrbot_plugin_webchat — AGENTS.md

## 项目概览

AstrBot 插件，提供一个 Web 终端 UI，通过 AstrBot 消息平台向 QQ 群/私聊发送和接收消息。

**技术栈**: Python (Quart) 后端 + 纯前端 JS (Vanilla, ESM module)

## 架构

### 后端 (`main.py`)
- **`Plugin(Star)`** — AstrBot 插件入口
  - `on_message()`: 监听所有消息事件，按 `group_id` 归入会话、存 SQLite、推 SSE
  - `api_sessions`, `api_send`, `api_history`, `api_clear`: REST API
  - `api_stream`: SSE 流式推送端点（注册在 `/api/plug/{plugin_name}/stream`）
- **数据存储**: SQLite (`data/astrbot_plugin_webchat.db`)
- **SSE 推送**: `asyncio.Queue` 队列，每客户端一个，广播消息/会话事件

### 前端 (`pages/terminal/`)
- `index.html` — 终端 UI 骨架
- `app.js` — ESM 模块，`window.AstrBotPluginPage` bridge 调用后端 API
- 启动即加载所有会话+历史（`loadSessionsWithHistory`），之后通过 SSE 实时更新

### 路由前缀
AstrBot 在 `/api/plug/` 下注册插件 API。前端 bridge 自动加前缀，但原生 `EventSource` 需要写完整路径：
```
/api/plug/astrbot_plugin_webchat/stream
```

## 核心数据流

### 消息接收
```
AstrBot event → on_message()
  → 群聊: session_key = "group:{group_id}"  私聊: session_key = umo
  → _register_session() → 存 SQLite sessions 表
  → _save_message() → 存 SQLite messages 表
  → _sse_broadcast("message", {...}) → 推送 SSE 给前端
```

### 消息发送
```
前端 POST /api/plug/.../send {session_id, message}
  → api_send() → meta.send_target (群聊原始 UMO) → context.send_message()
  → _save_message() → 存 SQLite
```

### 初始化加载
```
前端 loadSessionsWithHistory() → GET .../sessions?with_history=1
  → 返回所有会话元数据 + 最新 N 条消息 (N = max_log_per_session)
  → 前端缓存到 sessionMessages{}，切换会话即时渲染
```

## 数据库

### sessions 表
| 列 | 说明 |
|---|---|
| id | session_key（群聊 `group:{group_id}`，私聊 `umo`）|
| platform | 适配器名 |
| group_id | 群号（私聊为空）|
| group_name | 群名/显示名 |
| is_group | 是否群聊 |
| send_target | 发送消息用的原始 UMO |
| display_order | 显示顺序 |

### messages 表
| 列 | 说明 |
|---|---|
| id | 自增 PK |
| session_id | 关联 sessions.id |
| type | `received` / `sent` |
| sender | 发送者昵称 |
| sender_id | 发送者 UID |
| content | 消息文本 |
| group_id | 群号 |
| platform | 适配器名 |
| timestamp | unix 时间戳 |

**裁剪策略**: 每插入一条消息后 `DELETE ... ORDER BY timestamp DESC LIMIT {max_log}`，保留最新 N 条。

## 群聊会话合并

**关键设计**: 群聊消息用 `group:{group_id}` 作为 session_key，同群所有人消息归入同一会话。发送时用 `send_target`（原始 UMO）路由到群。`_extract_group_name()` 从 `message_obj.raw_message.group_name` 提取群名。

## SSE 实时推送

- 端点: `GET /api/plug/astrbot_plugin_webchat/stream`
- 事件: `message`（新消息）、`session`（新会话）、`connected`（连接成功）
- keepalive: 每 15 秒发送注释行
- 客户端断开自动清理队列
- 前端自动降级回轮询（2 秒间隔）

## 配置 (`_conf_schema.json`)

```json
{
  "max_log_per_session": { "type": "int", "default": 200 }
}
```

## 发布流程

版本号在 `metadata.yaml`:
- 手动改 `version: x.x.x` → 推 main
- GitHub Actions 自动检测变更 → 创建 `v{x.x.x}` tag + Release (自动生成 changelog)

## 代码约定

- **无外部依赖**（除 AstrBot SDK + Quart）
- 群聊 session_key 始终以 `group:` 开头，不要依赖 UMO 格式解析
- `_register_session()` 是幂等的 — 已存在则只更新 `send_target`
- `_query_messages(limit=N)` 返回**最新** N 条（用子查询 `ORDER BY timestamp DESC LIMIT N` 再外层正序排）
- frontend: `const sessionMessages = {}` 是唯一消息缓存，切换会话不做 API 请求
