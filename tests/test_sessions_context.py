"""直接测试 SQLite、上下文转换和真实本地工具，不替换模型客户端。"""
import json
import sqlite3

import pytest

from agent.context import ContextBuilder, ContextLimitError, ContextPolicy, bounded_result
from agent.llm import LLMResponse
from agent.sessions import SessionConflict, SessionStore
from agent.tools import ToolError, validate_arguments
from agent.tools_impl import build_registry, calculator


# 验证用户与会话隔离，以及重新打开数据库后的状态恢复。
def test_session_isolation_and_restart(tmp_path):
    path = str(tmp_path / "sessions.db")
    store = SessionStore(path)
    first = store.create("alice")
    first.history = [{"role": "user", "content": "private"}]
    first.summary = "Alice facts"
    build_registry(first).get("todo").func(action="add", text="buy milk")
    store.save(first)
    second = store.create("alice")
    assert second.history == [] and second.summary == "" and second.todos == []
    bob = store.create("bob")
    with pytest.raises(LookupError):
        store.switch("bob", first.session_id)
    assert store.load("bob").session_id == bob.session_id
    store.switch("alice", first.session_id)
    restarted = SessionStore(path)
    restored = restarted.load("alice")
    assert restored.history == first.history and restored.summary == first.summary
    assert restored.todos[0]["text"] == "buy milk"
    assert restarted.load("alice", second.session_id).todos == []
    assert restarted.list("alice") == [first.session_id, second.session_id]


# 验证旧版本写入冲突时，历史与待办都不会部分提交。
def test_conflict_cannot_partially_commit_todos_or_history(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    first = store.create("alice")
    stale = store.load("alice")
    first.history.append({"role": "user", "content": "winner"})
    store.save(first)
    build_registry(stale).get("todo").func(action="add", text="must not commit")
    stale.history.append({"role": "user", "content": "loser"})
    with pytest.raises(SessionConflict):
        store.save(stale)
    saved = store.load("alice")
    assert saved.todos == [] and saved.history == first.history


# 构造包含完整工具调用与结果的一轮历史，供上下文边界测试使用。
def conversation_turn(number):
    return [{"role": "user", "content": f"question {number}"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": str(number), "type": "function", "function": {
                    "name": "calculator", "arguments": '{"expression":"1+1"}'}}]},
            {"role": "tool", "tool_call_id": str(number), "content": '{"result":2}'},
            {"role": "assistant", "content": "2"}]


# 验证压缩保留完整的最近轮次，且摘要可持久化恢复。
def test_compaction_keeps_whole_recent_turns_and_persists_summary(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create("alice")
    session.history = conversation_turn(1) + conversation_turn(2) + conversation_turn(3)
    session.history.append({"role": "user", "content": "current"})
    store.save(session)
    context = ContextBuilder(ContextPolicy(recent_turns=2))
    old, recent = context.split(session)
    assert old == conversation_turn(1) + conversation_turn(2)
    assert recent == conversation_turn(3) + [{"role": "user", "content": "current"}]
    # 用响应数据验证摘要提交边界，不调用或替换模型客户端。
    context.apply_summary(session, LLMResponse("Key fact: 1+1=2; unfinished: current"), recent, store)
    restored = store.load("alice")
    assert restored.history == recent and "Key fact" in restored.summary
    messages = context.messages(restored)
    assert "Key fact" in messages[1]["content"]
    assert messages[3]["tool_calls"][0]["id"] == messages[4]["tool_call_id"]


# 验证空摘要、超长摘要和未完成摘要不会覆盖原历史。
@pytest.mark.parametrize("response", [LLMResponse(""), LLMResponse("x" * 101),
                                      LLMResponse("partial", finish_reason="length")])
def test_invalid_summary_preserves_history(tmp_path, response):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create("alice")
    session.history = conversation_turn(1)
    store.save(session)
    context = ContextBuilder(ContextPolicy(summary_chars=100))
    with pytest.raises(ContextLimitError):
        context.apply_summary(session, response, [], store)
    assert store.load("alice").history == conversation_turn(1)
    assert session.history == conversation_turn(1)


# 验证当前上下文超限时明确报错，工具配对保持完整。
def test_current_context_overflow_is_explicit_without_cutting_pairs(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create("alice")
    session.history = conversation_turn(1)
    context = ContextBuilder(ContextPolicy(max_chars=100))
    with pytest.raises(ContextLimitError):
        context.build(session, None, store)  # 没有可压缩的旧轮次，不会调用模型。
    assert session.history == conversation_turn(1)


# 验证待办状态及时反映到上下文，截断结果仍是有效 JSON。
def test_state_reflects_tool_updates_and_results_are_bounded(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create("alice")
    todo = build_registry(session).get("todo")
    todo.func(action="add", text="pending")
    store.save(session)
    assert "pending" in ContextBuilder().messages(session)[1]["content"]
    todo.func(action="done", item_id=1)
    store.save(session)
    assert "pending" not in ContextBuilder().messages(session)[1]["content"]
    assert store.load("alice").todos[0]["done"] is True
    content = bounded_result({"text": '\\"\n中文' * 2000}, 128)
    assert len(content) <= 128 and json.loads(content)["truncated"] is True


# 验证错误动作、参数类型和未知字段被拒绝。
@pytest.mark.parametrize("args", [{"action": "wrong"}, {"action": "done", "item_id": True},
                                  {"action": "add", "text": 123}, {"extra": "x"}])
def test_tool_validation(tmp_path, args):
    store = SessionStore(str(tmp_path / "sessions.db"))
    tool = build_registry(store.create("alice")).get("todo")
    with pytest.raises(ToolError):
        validate_arguments(tool, args)


# 验证只注册实际工具，并检查计算结果和除零错误。
def test_calculator_and_registry(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    registry = build_registry(store.create("alice"))
    assert {t["function"]["name"] for t in registry.schemas()} == {"calculator", "search", "todo"}
    assert calculator("(1+2)*3")["result"] == 9
    with pytest.raises(ToolError):
        calculator("1/0")


def test_existing_database_migration_preserves_session(tmp_path):
    """旧数据库补充步骤计数列后，历史和当前会话选择保持不变。"""
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    with db:
        db.executescript("""
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                history TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '',
                todos TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE active_sessions (user_id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
            INSERT INTO sessions(session_id,user_id,summary) VALUES ('old-session','alice','old facts');
            INSERT INTO active_sessions VALUES ('alice','old-session');
        """)
    db.close()
    store = SessionStore(path)
    state = store.load("alice")
    assert state.session_id == "old-session" and state.summary == "old facts" and state.steps_used == 0
    state.steps_used = 2
    store.save(state)
    assert SessionStore(path).load("alice").steps_used == 2


def test_calculator_rejects_non_finite_result():
    """浮点溢出不能以 Infinity 形式混入工具 JSON 结果。"""
    with pytest.raises(ToolError, match="not finite"):
        calculator("9" * 400)
