"""验收真实客户端到 SQLite 的完整链路；仅在 HTTP 边界控制响应与故障。"""
import json
from collections import deque
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError, RateLimitError

from agent import AgentRuntime, ContextPolicy
from agent.context import ContextLimitError, encoded
from agent.llm import ModelOutputError
from agent.sessions import SessionStore


# 构造协议中的一次工具调用，参数解析仍交给产品代码执行。
def call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments}}


# 构造标准 HTTP 响应体，不替换 DeepSeekClient 或 Runtime。
def reply(content="完成", calls=None, reason=None):
    return {"id": "response-1", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": reason or ("tool_calls" if calls else "stop"),
                         "message": {"role": "assistant", "content": content, "tool_calls": calls}}]}


# 所有验收请求都在传输层截获，缺少预期响应即失败，禁止意外访问外网。
@pytest.fixture
def wire(monkeypatch):
    model, search, requests, sleeps = deque(), deque(), [], []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://model.test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setattr("openai._base_client.time.sleep", sleeps.append)

    def handle(transport, request):
        body = json.loads(request.content) if request.content else None
        requests.append((request, body))
        queue = model if request.url.host == "model.test" else search
        assert request.url.host in ("model.test", "zh.wikipedia.org"), request.url
        assert queue, f"Unexpected request: {request.url}"
        item = queue.popleft()
        if item == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if callable(item):
            item = item(body)
        if isinstance(item, httpx.Response):
            item.request = request
            return item
        return httpx.Response(200, json=item, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    yield SimpleNamespace(model=model, search=search, requests=requests, sleeps=sleeps)
    assert not model and not search, "Some expected HTTP requests were never made"


# 在临时数据库中创建真实 Runtime，各验收用例独立运行。
@pytest.fixture
def runtime(tmp_path, wire):
    instance = AgentRuntime(str(tmp_path / "acceptance.db"))
    instance.create_session("alice")
    yield instance
    instance.client._client.close()


# 提取模型请求中的工具结果，便于检查错误反馈和调用配对。
def results(body):
    return [m for m in body["messages"] if m["role"] == "tool"]


# 检查每组调用后都有一一对应的工具结果，且不存在游离的结果消息。
def assert_pairs(messages):
    pending = []
    for message in messages:
        if message["role"] == "tool":
            assert pending and message["tool_call_id"] == pending.pop(0)
        else:
            assert not pending
            pending = [c["id"] for c in message.get("tool_calls", [])]
    assert not pending


def test_direct_answer(runtime, wire):
    """直接回答只调用模型一次，答案持久化且 Schema 另行传递。"""
    wire.model.append(reply("你好"))
    result = runtime.run("alice", "你好")
    body = wire.requests[0][1]
    assert result.answer == "你好" and result.steps == 1 and not result.truncated
    assert {t["function"]["name"] for t in body["tools"]} == {"calculator", "search", "todo"}
    assert "tool_calls" not in encoded(body["messages"])
    assert runtime.store.load("alice").history[-1] == {"role": "assistant", "content": "你好"}


@pytest.mark.parametrize("name,args,expected", [
    ("calculator", {"expression": "2+3*4"}, {"expression": "2+3*4", "result": 14}),
    ("search", {"query": "Python"}, {"query": "Python", "source": "wikipedia", "results": [
        {"title": "Python", "snippet": "语言", "url": "https://zh.wikipedia.org/wiki/Python"}]}),
    ("todo", {"action": "add", "text": "买牛奶"}, {"action": "add", "item": {
        "id": 1, "text": "买牛奶", "done": False}}),
])
def test_three_tools(runtime, wire, name, args, expected):
    """分别经过三个真实工具处理器，检查下一轮收到实际执行结果。"""
    if name == "search":
        wire.search.append(["Python", ["Python"], ["语言"], ["https://zh.wikipedia.org/wiki/Python"]])
    wire.model.append(reply("这段调用说明不应回灌", [call(name, args)]))

    def check(body):
        assert json.loads(results(body)[0]["content"]) == expected
        assert "这段调用说明不应回灌" not in encoded(body)
        assert_pairs(body["messages"])
        if name == "todo":
            assert runtime.store.load("alice").todos[0]["text"] == "买牛奶"
            assert "买牛奶" in body["messages"][1]["content"]
        return reply("已完成")

    wire.model.append(check)
    assert runtime.run("alice", "请使用指定工具").steps == 2
    if name == "search":
        request = next(r for r, _ in wire.requests if r.url.host == "zh.wikipedia.org")
        assert request.url.params["search"] == "Python" and request.url.params["limit"] == "5"


def test_multistep_and_multiple_calls(runtime, wire):
    """单轮多工具和跨轮依赖都可执行；todo 的增、查、完成状态正确落库。"""
    wire.model.extend([
        reply(calls=[call("calculator", {"expression": "1+1"}, "a"),
                     call("todo", {"action": "add", "text": "买牛奶"}, "b")]),
        reply(calls=[call("todo", {"action": "list"}, "c")]),
        reply(calls=[call("todo", {"action": "done", "item_id": 1}, "d")]),
        reply("已完成待办"),
    ])
    result = runtime.run("alice", "计算并处理待办")
    assert result.steps == 4
    assert runtime.store.load("alice").todos == [{"id": 1, "text": "买牛奶", "done": True}]
    assert_pairs(runtime.store.load("alice").history)
    listed = json.loads(results(wire.requests[3][1])[2]["content"])
    assert listed["items"][0]["done"] is False  # 旧结果不因后续状态变化被修改。


def test_plain_followup(runtime, wire):
    """普通追问带入此前用户事实和助手回答，不需要调用工具。"""
    wire.model.append(reply("记住了，你的项目叫海星。"))
    runtime.run("alice", "我的项目叫海星")

    def followup(body):
        history = body["messages"][2:]
        assert [m["role"] for m in history] == ["user", "assistant", "user"]
        assert history[0]["content"] == "我的项目叫海星"
        assert history[-1]["content"] == "它叫什么？"
        return reply("海星")

    wire.model.append(followup)
    assert runtime.run("alice", "它叫什么？").answer == "海星"


def test_tool_followup_after_restart(runtime, wire):
    """重启后的工具追问保留上次的调用与结果，并能引用结果继续计算。"""
    wire.model.extend([reply(calls=[call("calculator", {"expression": "2+3*4"})]), reply("14")])
    session = runtime.run("alice", "计算 2+3*4").session_id
    restored = AgentRuntime(runtime.store.path)

    def followup(body):
        assert json.loads(results(body)[0]["content"])["result"] == 14
        assert body["messages"][-1]["content"] == "把刚才结果乘 2"
        assert_pairs(body["messages"])
        return reply(calls=[call("calculator", {"expression": "14*2"}, "next")])

    wire.model.extend([followup, reply("28")])
    result = restored.run("alice", "把刚才结果乘 2", session)
    assert result.answer == "28"
    assert json.loads(results(wire.requests[-1][1])[-1]["content"])["result"] == 28
    restored.client._client.close()


def test_session_isolation_in_model_context(runtime, wire):
    """运行链路中切换会话不会把历史、摘要或 todo 带入其他用户或会话。"""
    first = runtime.store.load("alice")
    first.summary = "私有摘要"
    runtime.store.save(first)
    wire.model.extend([reply(calls=[call("todo", {"action": "add", "text": "私有待办"})]), reply("保存了")])
    runtime.run("alice", "私有问题")
    second = runtime.create_session("alice")
    runtime.create_session("bob")
    for user in ("alice", "bob"):
        wire.model.append(reply("独立回答"))
        runtime.run(user, "你好")
        assert "私有" not in encoded(wire.requests[-1][1])
    with pytest.raises(LookupError):
        runtime.run("bob", "越权读取", first.session_id)
    restarted = AgentRuntime(runtime.store.path)
    restarted.switch_session("alice", first.session_id)
    wire.model.append(reply("恢复了"))
    restarted.run("alice", "继续")
    assert "私有摘要" in encoded(wire.requests[-1][1])
    assert "私有待办" in encoded(wire.requests[-1][1])
    assert restarted.store.load("alice", second.session_id).todos == []
    restarted.client._client.close()


def test_compression_through_api_and_reload(runtime, wire):
    """越过阈值后真实走摘要请求，再用摘要与新状态构造决策上下文。"""
    runtime.context.policy = ContextPolicy(max_chars=2400, recent_turns=1)
    state = runtime.store.load("alice")
    state.summary = "旧事实：用户喜欢中文"
    state.history = [{"role": "user", "content": "项目叫海星。" + "旧背景" * 1000},
                     {"role": "assistant", "content": "记住了"}]
    state.todos = [{"id": 1, "text": "待确认部署时间", "done": False}]
    runtime.store.save(state)

    def summarize(body):
        assert "tools" not in body
        source = json.loads(body["messages"][1]["content"])
        assert source["previous_summary"] == state.summary
        assert "项目叫海星" in encoded(source["history"])
        return reply("用户喜欢中文；项目海星。")

    def answer(body):
        text = encoded(body["messages"])
        assert len(text) <= 2400 and "旧背景" not in text
        assert "项目海星" in text and "待确认部署时间" in text
        assert body["messages"][-1]["content"] == "项目叫什么？"
        assert "tools" in body
        return reply("海星；还需确认部署时间。")

    wire.model.extend([summarize, answer])
    assert runtime.run("alice", "项目叫什么？").steps == 1
    saved = SessionStore(runtime.store.path).load("alice")
    assert saved.summary == "用户喜欢中文；项目海星。" and len(saved.history) == 2


@pytest.mark.parametrize("bad_summary", ["", "超" * 101])
def test_bad_summary_does_not_destroy_history(runtime, wire, bad_summary):
    """摘要空白或超限时旧历史不被覆盖，当前问题仍可恢复。"""
    runtime.context.policy = ContextPolicy(max_chars=800, recent_turns=1, summary_chars=100)
    state = runtime.store.load("alice")
    state.history = [{"role": "user", "content": "旧" * 1000}, {"role": "assistant", "content": "收到"}]
    runtime.store.save(state)
    wire.model.append(reply(bad_summary))
    with pytest.raises((ContextLimitError, ModelOutputError)):
        runtime.run("alice", "追问")
    saved = runtime.store.load("alice")
    assert saved.summary == "" and saved.history[:2] == state.history
    assert saved.history[-1]["content"] == "追问"


def test_max_steps(runtime, wire):
    """连续工具请求到达上限后停止，最后一组调用结果仍然完整保存。"""
    runtime.max_steps = 2
    wire.model.extend([reply(calls=[call("calculator", {"expression": "1+1"}, str(i))]) for i in range(2)])
    result = runtime.run("alice", "不断计算")
    assert result.truncated and result.steps == 2 and len(wire.requests) == 2
    saved = runtime.store.load("alice")
    assert saved.steps_used == 2
    assert_pairs(saved.history)
    with pytest.raises(ValueError, match="No interrupted"):
        runtime.resume("alice")


@pytest.mark.parametrize("name,args,error", [
    ("calculator", "{bad", "valid JSON"),
    ("calculator", "[]", "JSON object"),
    ("calculator", '{"expression":NaN}', "JSON constant"),
    ("calculator", {}, "missing required"),
    ("calculator", {"expression": 123}, "string"),
    ("calculator", {"expression": "1", "extra": 2}, "unexpected"),
    ("calculator", {"expression": "1/0"}, "division by zero"),
    ("calculator", {"expression": "__import__('os')"}, "unsupported"),
    ("todo", {"action": "done", "item_id": 999}, "no item"),
    ("todo", {"action": "add"}, "text"),
    ("todo", {"action": "done", "item_id": True}, "integer"),
    ("todo", {"action": "delete"}, "invalid action"),
    ("search", {"query": " "}, "query"),
    ("unknown", {}, "Unknown tool"),
])
def test_bad_arguments_and_tool_errors_reach_model(runtime, wire, name, args, error):
    """非法参数和工具异常反馈到对应 call_id，模型可在总步骤预算内纠正。"""
    wire.model.append(reply(calls=[call(name, args)]))

    def recover(body):
        tool_result = results(body)[0]
        assert tool_result["tool_call_id"] == "c1"
        assert error in json.loads(tool_result["content"])["error"]
        return reply(calls=[call("calculator", {"expression": "2+2"}, "fixed")])

    wire.model.extend([recover, reply("修正后为 4")])
    assert runtime.run("alice", "执行工具").steps == 3
    assert runtime.store.load("alice").todos == []
    assert_pairs(runtime.store.load("alice").history)


# 非法结构必须在整组工具执行前拒绝，不能先执行其中看起来合法的 todo。
INVALID_OUTPUTS = [
    {}, [], "not an object", {"choices": []}, {"choices": [{"message": None, "finish_reason": "stop"}]},
    reply("", reason="stop"), reply("部分输出", reason="length"), reply("拒绝", reason="content_filter"),
    reply(123), reply([{"text": "不是字符串"}]),
    {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "不能执行", "refusal": "refused"}}]},
    reply(calls=[call("todo", {"action": "add", "text": "不得写入"}), call("todo", {}, "c1")]),
    reply(calls=[call("todo", {"action": "add", "text": "不得写入"}, "")]),
    reply(calls=[{"id": "x", "type": "function", "function": {"name": "todo", "arguments": None}}]),
    reply(calls=[{"id": "x", "type": "function", "function": None}]),
    reply(calls=[{"id": "x", "type": "other", "function": {"name": "todo", "arguments": "{}"}}]),
    reply(calls=[{"id": 123, "type": "function", "function": {"name": "todo", "arguments": "{}"}}]),
    reply(calls=[{"id": "x", "type": "function", "function": {"name": "todo", "arguments": {}}}]),
    reply(calls=[call("todo", {})], reason="stop"), reply("没有调用", reason="tool_calls"),
]


@pytest.mark.parametrize("payload", INVALID_OUTPUTS)
def test_illegal_model_output_executes_no_tools(runtime, wire, payload):
    """非法响应不产生工具副作用，不保存半条 assistant/tool 消息。"""
    wire.model.append(payload)
    with pytest.raises(ModelOutputError):
        runtime.run("alice", "测试非法输出")
    state = runtime.store.load("alice")
    assert state.todos == [] and len(state.history) == 1 and state.steps_used == 1


def test_non_json_model_response(runtime, wire):
    """API 返回损坏 JSON 时给出明确模型输出错误。"""
    wire.model.append(httpx.Response(200, content=b'{bad', headers={"content-type": "application/json"}))
    with pytest.raises(ModelOutputError):
        runtime.run("alice", "你好")


@pytest.mark.parametrize("failure", ["timeout", "rate_limit"])
def test_api_transient_failure_recovers(runtime, wire, failure):
    """SDK 对超时与 429 有限重试，重试沿用同一消息而不重复写入用户输入。"""
    fault = "timeout" if failure == "timeout" else httpx.Response(
        429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "1"})
    wire.model.extend([fault, reply("恢复成功")])
    result = runtime.run("alice", "你好")
    assert result.answer == "恢复成功" and result.steps == 1
    assert wire.requests[0][1] == wire.requests[1][1]
    assert len(wire.sleeps) == 1
    if failure == "rate_limit":
        assert wire.sleeps == [1.0]
    assert len(runtime.store.load("alice").history) == 2


@pytest.mark.parametrize("failure,exception", [("timeout", APITimeoutError), ("rate_limit", RateLimitError)])
def test_api_exhaustion_then_resume_without_repeating_tool(runtime, wire, failure, exception):
    """重试耗尽后保留已执行工具，重启恢复不会重复新增 todo。"""
    wire.model.append(reply(calls=[call("todo", {"action": "add", "text": "只新增一次"})]))
    for _ in range(3):
        wire.model.append("timeout" if failure == "timeout" else httpx.Response(
            429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "1"}))
    with pytest.raises(exception):
        runtime.run("alice", "新增待办")
    assert len(wire.requests) == 4 and len(wire.sleeps) == 2
    state = runtime.store.load("alice")
    assert len(state.todos) == 1 and state.steps_used == 2 and state.history[-1]["role"] == "tool"
    assert wire.requests[1][1] == wire.requests[2][1] == wire.requests[3][1]
    restored = AgentRuntime(runtime.store.path)
    wire.model.append(reply("已保存待办"))
    assert restored.resume("alice").steps == 3
    saved = restored.store.load("alice")
    assert len(saved.todos) == 1
    assert sum(m["role"] == "user" for m in saved.history) == 1
    restored.client._client.close()


def test_resume_does_not_reset_step_budget(runtime, wire):
    """恢复请求也受原步骤上限约束，不能借超时无限重新获得预算。"""
    runtime.max_steps = 1
    wire.model.extend(["timeout"] * 3)
    with pytest.raises(APITimeoutError):
        runtime.run("alice", "你好")
    restored = AgentRuntime(runtime.store.path, max_steps=1)
    result = restored.resume("alice")
    assert result.truncated and result.steps == 1 and len(wire.requests) == 3
    restored.client._client.close()


def test_authentication_failure_is_not_retried(runtime, wire):
    """配置或认证错误不能套用临时故障的重试策略。"""
    wire.model.append(httpx.Response(401, json={"error": {"message": "invalid key"}}))
    with pytest.raises(AuthenticationError):
        runtime.run("alice", "你好")
    assert len(wire.requests) == 1 and wire.sleeps == []


@pytest.mark.parametrize("search_response", ["timeout", {"error": "bad shape"},
                                            httpx.Response(503, json={"error": "unavailable"})])
def test_search_failure_is_a_tool_error(runtime, wire, search_response):
    """真实搜索适配器的网络或格式错误按工具错误反馈，不伪造搜索结果。"""
    wire.model.append(reply(calls=[call("search", {"query": "Python"})]))
    wire.search.append(search_response)

    def answer(body):
        assert "search failed" in json.loads(results(body)[0]["content"])["error"]
        return reply("搜索暂不可用")

    wire.model.append(answer)
    assert runtime.run("alice", "搜索 Python").answer == "搜索暂不可用"


def test_long_tool_result_keeps_pair_and_full_state(runtime, wire):
    """模型只收到有标记的有限预览，数据库中的完整 todo 内容仍保留。"""
    runtime.context.policy = ContextPolicy(result_chars=128)
    text = "重要事项" * 100
    wire.model.extend([reply(calls=[call("todo", {"action": "add", "text": text})]), reply("已添加")])
    runtime.run("alice", "新增长待办")
    tool_result = results(wire.requests[-1][1])[0]
    assert len(tool_result["content"]) <= 128 and json.loads(tool_result["content"])["truncated"]
    assert_pairs(wire.requests[-1][1]["messages"])
    assert runtime.store.load("alice").todos[0]["text"] == text


def test_model_decides_tool_not_user_keywords(runtime, wire):
    """用户文本提到 calculator 时，Runtime 仍只执行模型明确选择的 search。"""
    wire.model.extend([reply(calls=[call("search", {"query": "calculator"})]), reply("找到资料")])
    wire.search.append(["calculator", [], [], []])
    runtime.run("alice", "介绍 calculator")
    data = json.loads(results(wire.requests[-1][1])[0]["content"])
    assert data["source"] == "wikipedia" and data["results"] == []
