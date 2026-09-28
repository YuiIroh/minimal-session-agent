"""实际工具实现：计算器、真实百科搜索与按 session 隔离的待办列表。"""
from __future__ import annotations

from typing import Any
import math

import httpx

from .tools import Tool, ToolError, ToolRegistry


# 计算器：手写递归下降解析，避免执行任意 Python 表达式。

# 算术词法、语法或运算错误，最终转换为统一的 ToolError。
class _CalcError(Exception):
    pass


# 把表达式切成数字和运算符，拒绝不支持的字符。
def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(expression)
    while i < n:
        c = expression[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or c == ".":
            j = i
            while j < n and (expression[j].isdigit() or expression[j] == "."):
                j += 1
            tokens.append(expression[i:j])
            i = j
            continue
        if c in "+-*/()":
            tokens.append(c)
            i += 1
            continue
        raise _CalcError(f"unsupported character {c!r}")
    return tokens


class _Parser:
    """递归下降解析器，按加减、乘除、正负号与括号的优先级计算。"""

    # 保存词元列表，并将读取位置置于开头。
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    # 查看当前位置的词元，不移动游标。
    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    # 取出一个词元并推进游标；提前结束时抛出错误。
    def _advance(self) -> str:
        token = self._peek()
        if token is None:
            raise _CalcError("unexpected end of expression")
        self.pos += 1
        return token

    # 解析完整表达式，并拒绝未消费的尾部内容。
    def parse(self) -> float:
        if not self.tokens:
            raise _CalcError("empty expression")
        value = self._expr()
        if self.pos != len(self.tokens):
            raise _CalcError("unexpected trailing input")
        return value

    # 从最低优先级的加减层开始解析。
    def _expr(self) -> float:
        return self._add_sub()

    # 先计算乘除子表达式，再按从左到右的顺序处理加减。
    def _add_sub(self) -> float:
        value = self._mul_div()
        while self._peek() in ("+", "-"):
            op = self._advance()
            rhs = self._mul_div()
            value = value + rhs if op == "+" else value - rhs
        return value

    # 先处理正负号和括号，再计算乘除并检查除零。
    def _mul_div(self) -> float:
        value = self._unary()
        while self._peek() in ("*", "/"):
            op = self._advance()
            rhs = self._unary()
            if op == "*":
                value *= rhs
            else:
                if rhs == 0:
                    raise _CalcError("division by zero")
                value /= rhs
        return value

    # 递归处理数字或括号前的一元正负号。
    def _unary(self) -> float:
        if self._peek() == "-":
            self._advance()
            return -self._unary()
        if self._peek() == "+":
            self._advance()
            return self._unary()
        return self._primary()

    # 读取数字，或递归计算括号中的表达式。
    def _primary(self) -> float:
        token = self._advance()
        if token == "(":
            value = self._expr()
            if self._advance() != ")":
                raise _CalcError("missing closing parenthesis")
            return value
        try:
            return float(token)
        except ValueError:
            raise _CalcError(f"invalid number {token!r}")


def calculator(expression: str) -> dict[str, Any]:
    """校验并计算四则表达式，不使用 eval；整数结果返回 int。"""
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("calculator: 'expression' must be a non-empty string")
    try:
        value = _Parser(_tokenize(expression)).parse()
    except _CalcError as exc:
        raise ToolError(f"calculator: {exc}") from exc
    if not math.isfinite(value):
        raise ToolError("calculator: result is not finite")
    if value.is_integer():
        return {"expression": expression, "result": int(value)}
    return {"expression": expression, "result": value}


# 通过公开百科 API 搜索标题与链接；不返回虚构天气或固定占位数据。
def search(query: str) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ToolError("search: query must contain 1-500 characters")
    try:
        response = httpx.get(
            "https://zh.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": query.strip(), "limit": 5,
                    "namespace": 0, "format": "json"},
            headers={"User-Agent": "MinimalAgent/1.0 (educational search client)"}, timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
        if (not isinstance(data, list) or len(data) != 4
                or any(not isinstance(items, list) for items in data[1:])
                or len({len(items) for items in data[1:]}) != 1
                or any(not isinstance(item, str) for items in data[1:] for item in items)):
            raise ValueError("unexpected search response")
        return {"query": query, "source": "wikipedia", "results": [
            {"title": title, "snippet": snippet, "url": url}
            for title, snippet, url in zip(data[1][:5], data[2][:5], data[3][:5])]}
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"search failed: {exc}") from exc


# 直接修改当前会话的待办快照，由 Runtime 统一持久化。
class TodoStore:
    # 绑定当前 Session，避免不同会话共享待办状态。
    def __init__(self, session) -> None:
        self.session = session

    # 在当前会话内生成递增 ID，并新增未完成待办。
    def add(self, text: str) -> dict[str, Any]:
        item = {"id": max((x["id"] for x in self.session.todos), default=0) + 1,
                "text": text, "done": False}
        self.session.todos.append(item)
        return dict(item)

    # 返回待办副本，避免调用者直接改动内部字典。
    def list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.session.todos]

    # 按当前会话内的 ID 标记完成，找不到时抛错。
    def mark_done(self, item_id: int) -> dict[str, Any]:
        for item in self.session.todos:
            if item["id"] == item_id:
                item["done"] = True
                return dict(item)
        raise ToolError(f"todo: no item with id {item_id}")


# 分发新增、列表和完成操作，并检查对应操作的必需参数。
def _handle_todo(
    store: TodoStore,
    action: str,
    text: str | None = None,
    item_id: int | None = None,
) -> dict[str, Any]:
    if action == "add":
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ToolError("todo: 'text' must contain 1-1000 characters")
        return {"action": "add", "item": store.add(text)}
    if action == "list":
        return {"action": "list", "items": store.list()}
    if action == "done":
        if item_id is None:
            raise ToolError("todo: 'item_id' is required for action 'done'")
        return {"action": "done", "item": store.mark_done(item_id)}
    raise ToolError(f"todo: unknown action {action!r}")


# 工具注册：模型看到描述与参数，Runtime 调用对应本地函数。

# 注册三个工具，将 Schema 和实际执行函数关联起来。
def build_registry(session) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="calculator",
            description=(
                "Evaluate a basic arithmetic expression. Supports +, -, *, /, "
                "parentheses and unary minus. Returns the numeric result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression, e.g. '(1+2)*3'.",
                    }
                },
                "required": ["expression"],
            },
            func=calculator,
        )
    )
    registry.register(Tool(
        name="search",
        description="Search Chinese Wikipedia for article titles and links. Not live weather or general web search.",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Encyclopedia search query."}},
            "required": ["query"]},
        func=search,
    ))
    store = TodoStore(session)
    registry.register(
        Tool(
            name="todo",
            description=(
                "Manage a todo list. action='add' needs 'text'; action='done' needs "
                "'item_id'; action='list' returns all items."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "done"],
                        "description": "Operation to perform.",
                    },
                    "text": {"type": "string", "description": "Todo text (for 'add')."},
                    "item_id": {"type": "integer", "description": "Item id (for 'done')."},
                },
                "required": ["action"],
            },
            func=lambda **kwargs: _handle_todo(store, **kwargs),
        )
    )
    return registry
