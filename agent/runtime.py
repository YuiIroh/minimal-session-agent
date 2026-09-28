"""Agent 主循环：加载会话、构建上下文、调用模型、执行工具并持久化。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .context import ContextBuilder, ContextPolicy, bounded_result
from .llm import DeepSeekClient
from .parser import parse_arguments, parse_response
from .sessions import SessionStore
from .tools import ToolError, validate_arguments
from .tools_impl import build_registry


# 回传异常前隐藏环境密钥和常见令牌形式。
def redact(text: str) -> str:
    key = os.getenv("DEEPSEEK_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9]{8,}", "[REDACTED]", text)
    return re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1[REDACTED]", text)


# 本次请求结果，包含会话 ID、答案、决策轮数和是否超限。
@dataclass
class RunResult:
    session_id: str
    answer: str
    steps: int
    truncated: bool = False


# 协调真实客户端、会话存储和上下文构建器。
class AgentRuntime:
    # 创建存储与真实客户端，并配置每次请求的最大决策轮数。
    def __init__(self, db_path: str = "data/agent.sqlite3", max_steps: int = 8,
                 policy: ContextPolicy | None = None):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.store = SessionStore(db_path)
        self.context = ContextBuilder(policy)
        self.max_steps = max_steps
        self.client = DeepSeekClient()

    # 为用户创建并选中一个空会话。
    def create_session(self, user_id: str):
        return self.store.create(user_id)

    # 切换到该用户已有的会话，并返回恢复后的快照。
    def switch_session(self, user_id: str, session_id: str):
        return self.store.switch(user_id, session_id)

    # 校验新问题，每次重新加载会话并保存用户消息后进入循环。
    def run(self, user_id: str, user_input: str, session_id: str | None = None) -> RunResult:
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be non-empty")
        if len(user_input) > self.context.policy.input_chars:
            raise ValueError("Input exceeds input_chars")
        session = self.store.load(user_id, session_id)
        session.history.append({"role": "user", "content": user_input})
        session.steps_used = 0
        self.store.save(session)
        return self._run(session)

    # 从用户消息或工具结果继续中断的请求，不重复追加问题。
    def resume(self, user_id: str, session_id: str | None = None) -> RunResult:
        session = self.store.load(user_id, session_id)
        if not session.history or session.history[-1]["role"] not in ("user", "tool"):
            raise ValueError("No interrupted request to resume")
        return self._run(session)

    # 循环决策与执行；每轮工具状态落库后，再构造下一轮上下文。
    def _run(self, session) -> RunResult:
        for step in range(session.steps_used + 1, self.max_steps + 1):
            # 上轮工具状态和消息已提交，此时再组装下一轮上下文。
            messages = self.context.build(session, self.client, self.store)
            registry = build_registry(session)
            # 请求前记录预算，API 失败或进程重启后也不能无限重置步骤数。
            session.steps_used = step
            self.store.save(session)
            response = self.client.complete(messages, registry.schemas())
            turn = parse_response(response)
            if not turn.wants_tool:
                answer = turn.answer or ""
                if not answer.strip():
                    raise RuntimeError("Model returned an empty answer")
                session.history.append({"role": "assistant", "content": answer})
                self.store.save(session)
                return RunResult(session.session_id, answer, step)

            # 仅保存调用结构，不保存模型调用前的长篇说明。
            group = [{"role": "assistant", "content": "", "tool_calls": [
                {"id": tc.id, "type": "function", "function": {
                    "name": tc.name, "arguments": tc.arguments}} for tc in turn.tool_calls]}]
            for tc in turn.tool_calls:
                try:
                    tool = registry.get(tc.name)
                    if tool is None:
                        raise ToolError(f"Unknown tool: {tc.name}")
                    args = validate_arguments(tool, parse_arguments(tc.arguments))
                    result = tool.func(**args)
                except Exception as exc:
                    result = {"error": redact(str(exc))}
                group.append({"role": "tool", "tool_call_id": tc.id,
                              "content": bounded_result(result, self.context.policy.result_chars)})
            # 用版本号校验，一次原子提交 todo 状态和完整的工具调用/结果组。
            session.history.extend(group)
            self.store.save(session)

        answer = "I could not finish within the allowed number of steps."
        session.history.append({"role": "assistant", "content": answer})
        self.store.save(session)
        return RunResult(session.session_id, answer, self.max_steps, truncated=True)
