"""解析模型的公开输出：区分工具调用、调用说明和最终答案。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMResponse, ToolCall


class ParseError(ValueError):
    """工具参数无法解析成合法 JSON 对象时抛出。"""


# 一轮响应的解析结果；thought 仅指模型公开输出的调用说明。
@dataclass
class ParsedTurn:
    thought: str | None = None
    answer: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    # 有工具调用则继续执行，否则进入最终回答分支。
    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


def parse_arguments(raw: str) -> dict[str, Any]:
    """将工具参数从 JSON 字符串转为字典，空字符串按空参数处理。"""
    if not isinstance(raw, str):
        raise ParseError("tool arguments must be a JSON string")
    if raw.strip() == "":
        return {}
    try:
        # JSON 标准不接受 NaN/Infinity，不能把这些值作为正常参数传入工具。
        data = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ParseError(f"tool arguments are not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("tool arguments must be a JSON object")
    return data


# 拒绝 Python JSON 解码器默认接受的非有限数值扩展。
def _reject_constant(value: str):
    raise ParseError(f"invalid JSON constant: {value}")


def parse_response(response: LLMResponse) -> ParsedTurn:
    """有 tool_calls 时提取调用信息，否则把正文作为最终答案。"""
    if response.tool_calls:
        # 正文仅作为公开的调用说明，Runtime 不会把它写入历史。
        return ParsedTurn(thought=response.content, tool_calls=response.tool_calls)
    # 没有工具调用时，正文就是最终答案。
    return ParsedTurn(answer=response.content or "", thought=None)
