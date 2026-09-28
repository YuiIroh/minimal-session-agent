"""工具定义、注册与基础参数校验；具体调用由模型决定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ToolError(Exception):
    """工具参数或执行过程不符合要求时抛出，供 Runtime 回传给模型。"""


# Schema 描述工具用法，func 指向实际执行的本地函数。
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # 参数对象的 JSON Schema
    func: Callable[..., Any]

    # 生成兼容 API 的工具描述，不包含本地函数对象。
    def openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 按名称保存工具，供模型获取描述、Runtime 查找执行函数。
class ToolRegistry:
    # 为当前注册表建立独立的工具字典。
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # 注册工具并拒绝重名，避免执行时产生歧义。
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    # 按模型给出的工具名查找；未注册时返回 None。
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # 汇总所有工具描述，作为 API 的 tools 参数。
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_spec() for tool in self._tools.values()]


def validate_arguments(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """检查必填字段、未知字段、基础类型和枚举；业务语义由工具继续校验。"""
    if not isinstance(args, dict):
        raise ToolError(f"{tool.name}: arguments must be a JSON object")

    properties = tool.parameters.get("properties", {}) or {}
    required = set(tool.parameters.get("required", []) or [])

    missing = required - set(args.keys())
    if missing:
        raise ToolError(
            f"{tool.name}: missing required argument(s): {', '.join(sorted(missing))}"
        )

    unknown = set(args.keys()) - set(properties.keys())
    if unknown:
        raise ToolError(
            f"{tool.name}: unexpected argument(s): {', '.join(sorted(unknown))}"
        )

    for name, value in args.items():
        spec = properties[name]
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolError(f"{tool.name}: {name} must be a string")
        if expected == "integer" and type(value) is not int:
            raise ToolError(f"{tool.name}: {name} must be an integer")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"{tool.name}: invalid {name}")
    return args
