"""
astrbot_plugin_webchat — 基于 SenseNova API 的终端风格 Web 聊天插件
"""

import asyncio
import json
import uuid

import aiohttp
from quart import Response, jsonify, request

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_webchat"


class Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 每个会话独立的聊天历史 (key = session_id)
        self.histories: dict[str, list[dict]] = {}
        self._http_session: aiohttp.ClientSession | None = None

        # 注册后端 API
        context.register_web_api(
            f"/{PLUGIN_NAME}/chat",
            self.api_chat,
            ["POST"],
            "发送消息（SSE 流式）",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/history",
            self.api_history,
            ["GET"],
            "获取聊天历史",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/clear",
            self.api_clear,
            ["POST"],
            "清空聊天历史",
        )

        self._log.info("astrbot_plugin_webchat 已加载")

    # ── HTTP 会话管理 ───────────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def terminate(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # ── API 处理 ────────────────────────────────────────────────────

    async def api_history(self):
        """返回所有会话的聊天历史。"""
        all_history = []
        for history in self.histories.values():
            all_history.extend(history)
        return jsonify({"history": all_history})

    async def api_clear(self):
        """清空聊天历史。"""
        self.histories.clear()
        return jsonify({"status": "ok", "message": "聊天历史已清空"})

    async def api_chat(self):
        """
        接收用户消息，调用 SenseNova API，以 SSE 流式返回结果。
        请求体: {"message": "...", "session_id": "..."(可选), "model": "..."(可选)}
        """
        data = await request.get_json()
        user_message = data.get("message", "").strip()
        session_id = data.get("session_id", "default")
        model = data.get("model") or self.config.get("model", "deepseek-v4-flash")

        if not user_message:
            return jsonify({"error": "消息不能为空"}), 400

        api_key = self.config.get("api_key", "")
        if not api_key:
            return jsonify({"error": "未配置 API Key"}), 500

        # 获取/创建会话历史
        if session_id not in self.histories:
            self.histories[session_id] = []
        history = self.histories[session_id]

        # 追加用户消息
        history.append({"role": "user", "content": user_message})

        # 裁剪历史
        max_history = self.config.get("max_history", 20) * 2  # 一轮 = user + assistant
        if len(history) > max_history:
            self.histories[session_id] = history[-max_history:]
            history = self.histories[session_id]

        # 构建请求
        base_url = self.config.get(
            "base_url", "https://token.sensenova.cn/v1/messages"
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": history,
            "max_tokens": self.config.get("max_tokens", 4096),
            "temperature": self.config.get("temperature", 0.7),
            "stream": True,
        }
        system_prompt = self.config.get("system_prompt", "").strip()
        if system_prompt:
            payload["system"] = system_prompt

        # SSE 流式响应
        async def stream_response():
            session = await self._ensure_session()
            assistant_text = ""
            try:
                async with session.post(
                    base_url, headers=headers, json=payload
                ) as resp:
                    if resp.status != 200:
                        error_body = await resp.text()
                        error_msg = f"API 错误 ({resp.status}): {error_body}"
                        yield f"event: error\ndata: {json.dumps({'error': error_msg}, ensure_ascii=False)}\n\n"
                        # 移除刚追加的用户消息
                        if history and history[-1]["role"] == "user":
                            history.pop()
                        return

                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                yield f"event: done\ndata: {json.dumps({'text': assistant_text}, ensure_ascii=False)}\n\n"
                                break
                            try:
                                event_data = json.loads(data_str)
                                event_type = event_data.get("type", "")

                                if event_type == "content_block_delta":
                                    delta = event_data.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        token = delta.get("text", "")
                                        assistant_text += token
                                        yield f"event: token\ndata: {json.dumps({'text': token}, ensure_ascii=False)}\n\n"
                                    elif delta.get("type") == "thinking_delta":
                                        thinking = delta.get("thinking", "")
                                        yield f"event: thinking\ndata: {json.dumps({'text': thinking}, ensure_ascii=False)}\n\n"

                                elif event_type == "message_delta":
                                    yield f"event: done\ndata: {json.dumps({'text': assistant_text}, ensure_ascii=False)}\n\n"
                            except json.JSONDecodeError:
                                continue

            except Exception as e:
                error_msg = f"请求异常: {e}"
                yield f"event: error\ndata: {json.dumps({'error': error_msg}, ensure_ascii=False)}\n\n"
                if history and history[-1]["role"] == "user":
                    history.pop()
                return

            # 追加助手回复到历史
            if assistant_text:
                history.append({"role": "assistant", "content": assistant_text})

        return Response(
            stream_response(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
