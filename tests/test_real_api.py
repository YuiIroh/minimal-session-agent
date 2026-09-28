"""需显式开启的真实 API 集成测试，会访问网络并产生 API 费用。"""
import os

import pytest

from agent import AgentRuntime, ContextPolicy


# 通过真实模型验证工具执行、会话恢复和旧历史摘要召回。
@pytest.mark.skipif(os.getenv("RUN_REAL_API_TESTS") != "1" or not os.getenv("DEEPSEEK_API_KEY"),
                    reason="Set RUN_REAL_API_TESTS=1 and DEEPSEEK_API_KEY for real API tests")
def test_real_session_tools_restore_and_summary(tmp_path):
    path = str(tmp_path / "real.db")
    runtime = AgentRuntime(path, policy=ContextPolicy(recent_turns=1))
    session = runtime.create_session("user")
    runtime.run("user", "请必须调用 todo 工具新增待办：买牛奶。")
    assert runtime.store.load("user").todos[0]["text"] == "买牛奶"
    other = runtime.create_session("user")
    assert other.todos == []
    restored = AgentRuntime(path, policy=ContextPolicy(max_chars=3500, recent_turns=1))
    restored.switch_session("user", session.session_id)
    state = restored.store.load("user")
    state.history.extend([{"role": "user", "content": "关键事实：我的项目叫海星。" + "背景说明。" * 1000},
                          {"role": "assistant", "content": "已记录项目名海星。"}])
    restored.store.save(state)
    result = restored.run("user", "我的项目叫什么？还有哪些未完成待办？")
    state = restored.store.load("user")
    assert state.summary and "海星" in result.answer and "牛奶" in result.answer
    assert restored.store.load("user", other.session_id).history == []
