# 接收消息事件

> 来源: https://docs.astrbot.app/dev/star/guides/listen-message-event.html

事件监听器可以收到平台下发的消息内容，可以实现指令、指令组、事件监听等功能。

事件监听器的注册器在 `astrbot.api.event.filter` 下，需要先导入。**请务必导入**，否则会和 Python 的高阶函数 `filter` 冲突。

```python
from astrbot.api.event import filter, AstrMessageEvent
```

---

## 消息与事件

AstrBot 接收消息平台下发的消息，并将其封装为 `AstrMessageEvent` 对象，传递给插件进行处理。

### AstrMessageEvent

`AstrMessageEvent` 是 AstrBot 的消息事件对象，其中存储了消息发送者、消息内容等信息。

### AstrBotMessage

`AstrBotMessage` 是 AstrBot 的消息对象，通过 `event.message_obj` 获取：

```python
class AstrBotMessage:
    type: MessageType           # 消息类型
    self_id: str                # 机器人的识别 id
    session_id: str             # 会话 id（取决于 unique_session 的设置）
    message_id: str             # 消息 id
    group_id: str = ""          # 群组 id，私聊时为空
    sender: MessageMember       # 发送者
    message: List[BaseMessageComponent]  # 消息链
    message_str: str            # 纯文本消息字符串
    raw_message: object         # 原始消息对象
    timestamp: int              # 消息时间戳
```

### 消息链

消息链描述一个消息的结构，是一个有序列表，常见消息段类型：

| 类型 | 说明 |
|------|------|
| `Plain` | 文本消息段 |
| `At` | 提及消息段 |
| `Image` | 图片消息段 |
| `Record` | 语音消息段 |
| `Video` | 视频消息段 |
| `File` | 文件消息段 |

OneBot v11 平台额外支持：`Face`、`Node`、`Nodes`、`Poke`。

---

## 指令

```python
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        '''这是 hello world 指令'''
        user_name = event.get_sender_name()
        message_str = event.message_str
        yield event.plain_result(f"Hello, {user_name}!")
```

> **注意**: 指令不能带空格，否则 AstrBot 会将其解析到第二个参数。

### 带参指令

AstrBot 会自动解析指令参数：

```python
@filter.command("add")
async def add(self, event: AstrMessageEvent, a: int, b: int):
    # /add 1 2 -> 结果是: 3
    yield event.plain_result(f"Wow! The answer is {a + b}!")
```

### 指令组

```python
@filter.command_group("math")
def math():
    pass

@math.command("add")
async def add(self, event: AstrMessageEvent, a: int, b: int):
    yield event.plain_result(f"结果是: {a + b}")

@math.command("sub")
async def sub(self, event: AstrMessageEvent, a: int, b: int):
    yield event.plain_result(f"结果是: {a - b}")
```

指令组函数内不需要实现任何函数，请直接 `pass`。理论上指令组可以无限嵌套。

### 指令别名 (v3.4.28+)

```python
@filter.command("help", alias={'帮助', 'helpme'})
async def help(self, event: AstrMessageEvent):
    yield event.plain_result("这是一个计算器插件。")
```

---

## 事件类型过滤

### 接收所有

```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_all_message(self, event: AstrMessageEvent):
    yield event.plain_result("收到了一条消息。")
```

### 群聊和私聊

```python
@filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
async def on_private_message(self, event: AstrMessageEvent):
    yield event.plain_result("收到了一条私聊消息。")
```

`EventMessageType` 包含: `PRIVATE_MESSAGE`、`GROUP_MESSAGE`、`ALL`。

### 消息平台过滤

```python
@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL)
async def on_aiocqhttp(self, event: AstrMessageEvent):
    yield event.plain_result("收到了一条信息")
```

`PlatformAdapterType` 支持: `AIOCQHTTP`、`QQOFFICIAL`、`QQOFFICIAL_WEBHOOK`、`TELEGRAM`、`WECOM`、`WECOM_AI_BOT`、`LARK`、`DINGTALK`、`DISCORD`、`SLACK`、`KOOK`、`VOCECHAT`、`WEIXIN_OFFICIAL_ACCOUNT`、`SATORI`、`MISSKEY`、`LINE`、`MATRIX`、`WEIXIN_OC`、`MATTERMOST`、`WEBCHAT`、`ALL`。

### 管理员指令

```python
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("test")
async def test(self, event: AstrMessageEvent):
    pass
```

### 多个过滤器

支持同时使用多个过滤器（AND 逻辑）：

```python
@filter.command("helloworld")
@filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
async def helloworld(self, event: AstrMessageEvent):
    yield event.plain_result("你好！")
```

---

## 事件钩子

> 事件钩子**不支持**与 `@filter.command` 等装饰器一起使用。

### Bot 初始化完成 (v3.4.34+)

```python
@filter.on_astrbot_loaded()
async def on_astrbot_loaded(self):
    print("AstrBot 初始化完成")
```

### LLM 请求时

```python
from astrbot.api.provider import ProviderRequest

@filter.on_llm_request()
async def my_custom_hook_1(self, event: AstrMessageEvent, req: ProviderRequest):
    req.system_prompt += "自定义 system_prompt"
```

### LLM 请求完成时

```python
from astrbot.api.provider import LLMResponse

@filter.on_llm_response()
async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
    print(resp)
```

### 发送消息前

```python
import astrbot.api.message_components as Comp

@filter.on_decorating_result()
async def on_decorating_result(self, event: AstrMessageEvent):
    result = event.get_result()
    chain = result.chain
    chain.append(Comp.Plain("!"))
```

### 发送消息后

```python
@filter.after_message_sent()
async def after_message_sent(self, event: AstrMessageEvent):
    pass
```

---

## 优先级

```python
@filter.command("helloworld", priority=1)
async def helloworld(self, event: AstrMessageEvent):
    yield event.plain_result("Hello!")
```

默认优先级是 0，数值越大越先执行。

## 控制事件传播

```python
@filter.command("check_ok")
async def check_ok(self, event: AstrMessageEvent):
    ok = self.check()
    if not ok:
        yield event.plain_result("检查失败")
        event.stop_event()  # 停止事件传播
```

当事件停止传播，后续所有步骤将不会被执行。
