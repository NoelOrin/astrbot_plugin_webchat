# 发送消息

> 来源: https://docs.astrbot.app/dev/star/guides/send-message.html

## 被动消息

被动消息指的是机器人被动回复消息：

```python
@filter.command("helloworld")
async def helloworld(self, event: AstrMessageEvent):
    yield event.plain_result("Hello!")
    yield event.plain_result("你好！")
    yield event.image_result("path/to/image.jpg")       # 发送本地图片
    yield event.image_result("https://example.com/img.jpg")  # 发送 URL 图片
```

## 主动消息

主动消息指的是机器人主动推送消息。某些平台可能不支持主动消息发送。

通过 `event.unified_msg_origin` 获取会话唯一 ID，存储后可随时发送消息：

```python
from astrbot.api.event import MessageChain

@filter.command("helloworld")
async def helloworld(self, event: AstrMessageEvent):
    umo = event.unified_msg_origin
    message_chain = MessageChain().message("Hello!").file_image("path/to/image.jpg")
    await self.context.send_message(event.unified_msg_origin, message_chain)
```

## 富媒体消息

使用 `MessageChain` 构建消息链：

```python
import astrbot.api.message_components as Comp

@filter.command("helloworld")
async def helloworld(self, event: AstrMessageEvent):
    chain = [
        Comp.At(qq=event.get_sender_id()),          # At 消息发送者
        Comp.Plain("来看这个图："),
        Comp.Image.fromURL("https://example.com/image.jpg"),
        Comp.Image.fromFileSystem("path/to/image.jpg"),
        Comp.Plain("这是一个图片。")
    ]
    yield event.chain_result(chain)
```

### 文件

```python
Comp.File(file="path/to/file.txt", name="file.txt")
```

### 语音

```python
path = "path/to/record.wav"  # 暂时只接受 wav 格式
Comp.Record(file=path, url=path)
```

### 视频

```python
path = "path/to/video.mp4"
Comp.Video.fromFileSystem(path=path)
Comp.Video.fromURL(url="https://example.com/video.mp4")
```

### 合并转发（OneBot v11）

```python
from astrbot.api.message_components import Node, Plain, Image

node = Node(
    uin=905617992,
    name="Soulter",
    content=[
        Plain("hi"),
        Image.fromFileSystem("test.jpg")
    ]
)
yield event.chain_result([node])
```
