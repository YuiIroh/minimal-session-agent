"""真实 DeepSeek API 客户端：把接口响应转换为统一的数据结构。"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Any, Sequence


# 响应结构不合法时终止本轮，避免把残缺或拒绝内容当成工具指令。
class ModelOutputError(ValueError):
    pass


@dataclass
class ToolCall:
    """模型请求的一次工具调用；参数暂时保留为 JSON 字符串。"""

    id: str
    name: str
    arguments: str  # 模型返回的原始 JSON 参数字符串


@dataclass
class LLMResponse:
    """统一保存响应正文、工具调用列表和结束原因。"""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    # 只根据是否存在工具调用判断模型是否需要执行工具。
    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


class DeepSeekClient:
    """通过兼容接口调用 DeepSeek；密钥、地址和模型从参数或环境变量读取。"""

    # 初始化真实客户端，设置请求超时和失败重试次数。
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        if not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set; export it or create a .env file."
            )
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60.0, max_retries=2)

    # 消息与工具 Schema 分开传递，再将首个候选响应转换为 LLMResponse。
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        try:
            # 直接验证原始 JSON，避免 SDK 的宽松类型转换掩盖非法字段。
            response = self._client.chat.completions.with_raw_response.create(**kwargs)
            data = response.http_response.json()
        except json.JSONDecodeError as exc:
            raise ModelOutputError("Model response is not valid JSON") from exc
        return self._normalize(data)

    # SDK 可能接收不完整数据；执行任何工具前统一验证整组调用结构。
    def _normalize(self, data) -> LLMResponse:
        try:
            if not isinstance(data, dict):
                raise ValueError
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            reason = choice.get("finish_reason")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            if reason not in ("stop", "tool_calls") or message.get("refusal"):
                raise ValueError
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            raw_calls = message.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list) or bool(raw_calls) != (reason == "tool_calls"):
                raise ValueError
            calls, ids = [], set()
            for call in raw_calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ValueError
                call_id, function = call.get("id"), call.get("function")
                if not isinstance(call_id, str) or not call_id.strip() or call_id in ids:
                    raise ValueError
                if not isinstance(function, dict):
                    raise ValueError
                name, arguments = function.get("name"), function.get("arguments")
                if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
                    raise ValueError
                ids.add(call_id)
                calls.append(ToolCall(call_id, name, arguments))
            if not calls and (content is None or not content.strip()):
                raise ValueError
            return LLMResponse(content=content, tool_calls=calls, finish_reason=reason)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelOutputError("Invalid, refused or incomplete model response; no tools executed") from exc
