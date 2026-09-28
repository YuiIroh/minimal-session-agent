# Agent 验收记录

本次按用户列出的验收项及已有 Agent 范围核查：真实 API、三个工具、session 与 context。不引入 CLI、FakeLLM 运行入口、RAG、任务队列或其他业务功能。两类追问分别指普通对话追问、基于工具结果的追问。

## 实测结果

**73 passed，1 skipped，0 warnings**。跳过的是需要 `RUN_REAL_API_TESTS=1` 与 `DEEPSEEK_API_KEY` 的真实联网集成测试。本次没有据此宣称实际模型的决策质量、摘要质量或外部搜索可用性已通过在线验收。

测试入口为 `tests/`，可直接在 IDE 的 pytest 配置中运行。通常使用 `python -m pytest -q`；本次环境的默认临时目录不可访问，因此实际验证使用项目内全新的临时目录：

```powershell
$testRunPath = Join-Path (Get-Location) ('data/test-' + [guid]::NewGuid().ToString('N'))
python -m pytest -q -p no:cacheprovider --basetemp=$testRunPath --tb=short
```

`test_acceptance.py` 仅在 HTTP 传输层安排协议响应、超时与限流，实际经过 SDK、DeepSeekClient、Runtime、工具处理器、context 和 SQLite。测试不替换模型客户端方法，不接触用户数据库，也不会意外访问外网。普通回答和摘要的语义正确性仍需真实模型集成测试确认。

## 验收映射

| 验收项 | pytest 证据 | 检查内容 |
| --- | --- | --- |
| 直接回答 | `test_direct_answer` | 单轮返回、答案持久化、Schema 另传 |
| 三个工具 | `test_three_tools`（3 组）、`test_model_decides_tool_not_user_keywords` | calculator、search、todo 分别实际执行；严格按模型调用分发 |
| 多步循环 | `test_multistep_and_multiple_calls` | 单轮多调用、跨轮依赖、todo 增/查/完成、历史结果不被后来状态篡改 |
| 普通追问 | `test_plain_followup` | 下一请求包含此前用户事实、助手回答与当前问题 |
| 工具追问 | `test_tool_followup_after_restart` | 重启后保留完整调用与结果，基于上次结果继续执行工具 |
| session 隔离与恢复 | `test_session_isolation_in_model_context`、`test_session_isolation_and_restart` | 用户与会话隔离、当前选择恢复、历史/摘要/todo 不串入其他上下文 |
| 压缩 | `test_compression_through_api_and_reload`、`test_compaction_keeps_whole_recent_turns_and_persists_summary` | 触发摘要请求，合并旧摘要，持久化后重新加载；按完整轮次保留配对 |
| 压缩失败与长度边界 | `test_bad_summary_does_not_destroy_history`、`test_invalid_summary_preserves_history`、`test_current_context_overflow_is_explicit_without_cutting_pairs`、`test_long_tool_result_keeps_pair_and_full_state` | 无效摘要不覆盖原历史；当前内容超限明确报错；工具预览有限且完整状态保留 |
| 最大步骤 | `test_max_steps`、`test_resume_does_not_reset_step_budget` | 正常循环到限停止，异常恢复不重置剩余预算 |
| 非法模型输出 | `test_illegal_model_output_executes_no_tools`（20 组）、`test_non_json_model_response` | 缺字段、空答案、截断、拒绝、错误字段类型、重复/缺失 ID、结束原因与调用不一致；整组工具执行前拒绝 |
| 非法参数与工具错误 | `test_bad_arguments_and_tool_errors_reach_model`（14 组）、`test_search_failure_is_a_tool_error`（3 组） | 损坏 JSON、非对象、NaN、缺字段、类型/枚举/未知字段、未知工具、除零、搜索超时/503/坏结构；按 call_id 回传后可纠正 |
| API 超时及限流 | `test_api_transient_failure_recovers`、`test_api_exhaustion_then_resume_without_repeating_tool` | 各自验证重试成功、首次加两次重试后停止、429 的 Retry-After、同一消息重发、恢复不重复写工具 |
| 不可重试错误 | `test_authentication_failure_is_not_retried` | 401 不重试 |
| 既有状态兼容 | `test_existing_database_migration_preserves_session`、`test_conflict_cannot_partially_commit_todos_or_history` | 旧库升级不丢会话，版本冲突不产生部分提交 |

## 修复缺项及 Review 顺序

1. **`agent/tools_impl.py`：补齐 search。** 先前删去固定搜索数据后只剩两个工具；现在注册真实中文百科 API 搜索，返回标题、简介和链接，不恢复虚构天气或占位结果。其能力范围是百科检索，非通用网页搜索或实时天气查询。同时拒绝计算器的非有限结果，避免 Infinity 混入 JSON。
2. **`agent/llm.py`：补齐原始响应校验。** 直接验证 HTTP JSON，避免 SDK 宽松转换掩盖错误。拒绝损坏结构、拒绝/截断输出、非法工具字段、重复调用 ID 和空答案，再交给 Runtime。API 超时、429 保留 SDK 的单套有限重试，未增加外层重试。
3. **`agent/parser.py`：补齐参数边界。** 非字符串参数与 NaN/Infinity 明确拒绝；可解析的调用结构中，非法参数仍作为工具错误回传给模型修正。
4. **`agent/sessions.py` → `agent/runtime.py`：修复恢复时的预算重置。** 保存当前请求的 `steps_used`，模型调用前先记账，resume 继续剩余步骤；兼容旧库自动加列，原会话内容保留。正常新问题会开启新的步骤预算，摘要调用仍另计。
5. **`tests/test_acceptance.py`：完整验收链路。** 优先阅读上述表格对应测试，再看已有的 session/context 边界测试。真实联网测试单列在 `tests/test_real_api.py`。

`main.py` 仍为 IDE 直接运行入口；新增和修改的代码继续保留简短中文注释。
