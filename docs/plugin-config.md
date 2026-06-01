# 插件配置

> 来源: https://docs.astrbot.app/dev/star/guides/plugin-config.html

AstrBot 提供了强大的配置解析和可视化功能，让用户在管理面板上直接配置插件。

## 配置定义

在插件目录下添加 `_conf_schema.json` 文件：

```json
{
  "token": {
    "description": "Bot Token",
    "type": "string"
  },
  "sub_config": {
    "description": "测试嵌套配置",
    "type": "object",
    "hint": "xxxx",
    "items": {
      "name": {
        "description": "testsub",
        "type": "string"
      },
      "time": {
        "description": "testsub",
        "type": "int",
        "default": 123
      }
    }
  }
}
```

## Schema 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | ✅ | 配置类型 |
| `description` | — | 配置描述 |
| `hint` | — | 提示信息（问号按钮） |
| `obvious_hint` | — | hint 是否醒目显示 |
| `default` | — | 默认值 |
| `items` | — | `object` 类型的子 Schema |
| `invisible` | — | 是否隐藏（默认 `false`） |
| `options` | — | 下拉列表可选项，如 `["chat", "agent"]` |
| `editor_mode` | — | 代码编辑器模式（>= v3.5.10） |
| `editor_language` | — | 代码语言（默认 `json`） |
| `editor_theme` | — | 主题：`vs-light` / `vs-dark` |

## 支持的 type

| type | 默认值 | 说明 |
|------|--------|------|
| `string` | `""` | 单行文本 |
| `text` | `""` | 多行文本（textarea） |
| `int` | `0` | 整数 |
| `float` | `0.0` | 浮点数 |
| `bool` | `False` | 布尔值 |
| `object` | `{}` | 嵌套对象，需配 `items` |
| `list` | `[]` | 列表 |
| `dict` | `{}` | 字典，可配 `template_schema` |
| `template_list` | — | 模板列表（>= v4.10.4） |
| `file` | `[]` | 文件上传（>= v4.13.0） |

## `_special` 字段 (v4.0.0+)

用于调用 AstrBot 提供的可视化选取功能：

| 值 | 说明 |
|---|------|
| `select_provider` | 选择模型提供商 |
| `select_provider_tts` | 选择 TTS 提供商 |
| `select_provider_stt` | 选择 STT 提供商 |
| `select_persona` | 选择人设 |
| `select_knowledgebase` | 选择知识库（多选，type 设为 `list`） |

## 在插件中使用配置

AstrBot 在载入插件时自动解析 `_conf_schema.json`，保存在 `data/config/<plugin_name>_config.json`，并在实例化时传入 `__init__()`：

```python
from astrbot.api import AstrBotConfig

class ConfigPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        print(self.config)
        # self.config.save_config()  # 保存配置
```

`AstrBotConfig` 继承自 `Dict`，拥有字典的所有方法。

## 配置更新

发布不同版本更新 Schema 时，AstrBot 会递归检查配置项，自动为缺失的配置项添加默认值、移除不存在的配置项。
