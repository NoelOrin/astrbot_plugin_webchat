# AstrBot 插件开发文档

本目录收录了开发 `astrbot_plugin_webchat` 时参考的 AstrBot 官方文档。

## 目录

| 文档 | 说明 |
|------|------|
| [listen-message-event.md](./listen-message-event.md) | 消息事件监听：指令、指令组、事件过滤、事件钩子 |
| [send-message.md](./send-message.md) | 消息发送：被动回复、主动推送、富媒体消息 |
| [plugin-config.md](./plugin-config.md) | 插件配置：`_conf_schema.json` 定义、类型、特殊字段 |
| [plugin-pages.md](./plugin-pages.md) | 插件 Pages：前端页面、Bridge API、SSE、国际化 |
| [gap-analysis.md](./gap-analysis.md) | 本项目能力补齐记录：问题、修法、验证证据 |

> ⚠️ 本目录文档是开发时（v0.3.x）抄录的参考，部分内容已落后于官方：
> 官方现在推荐 `views/` 目录（`pages/` 作为兼容别名）、`astrbot.api.web` 的
> `json_response`/`error_response`/`stream_response`、以及 `window.AstrBotPluginView`。
> 以 https://docs.astrbot.app 为准。

## 官方文档

- 完整文档: https://docs.astrbot.app/dev/star/plugin-new.html
- GitHub: https://github.com/AstrBotDevs/AstrBot
