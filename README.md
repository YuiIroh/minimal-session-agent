# 持久化 Agent（真实 DeepSeek API）

在 PyCharm 或 VS Code 中打开项目，选择安装了 requirements.txt 依赖的 Python 解释器，将 `.env.example` 复制为 `.env` 并填写密钥，然后直接运行根目录的 `main.py`。无需命令行参数。

使用 Python 3.10 或以上版本（本地验证为 Python 3.12）。依赖安装命令：`python -m pip install -r requirements.txt`。

修改 main.py 顶部配置：

| 配置 | 用途 |
| --- | --- |
| USER_ID | 用户标识，由调用方提供 |
| SESSION_ID | 指定已有会话；None 恢复该用户上次选中的会话 |
| CREATE_NEW_SESSION | True 时新建并选中一个独立会话；完成创建后改回 False |
| QUESTION | 本次提问 |
| RESUME | True 时继续中断请求，不再次追加 QUESTION |

默认数据库是项目目录下 `data/agent.sqlite3`；IDE 工作目录变化不会改变 main.py 使用的数据库位置。可通过 AGENT_DB_PATH 更改。历史、摘要和 todo 均保存在 session 内，重启程序可恢复。user_id 与 session_id 分开；数据库读取、切换和更新均校验二者。当前选中会话按用户持久化；多窗口使用时显式传 session_id，避免共享当前选择。user_id 是调用方提供的标识，不是认证机制。

公开接口：

```python
from agent import AgentRuntime

agent = AgentRuntime("data/agent.sqlite3")
a = agent.create_session("alice")
agent.run("alice", "帮我添加待办：买牛奶", a.session_id)
b = agent.create_session("alice")
agent.run("alice", "我有哪些待办？", b.session_id)  # 独立空列表
agent.switch_session("alice", a.session_id)
agent.run("alice", "我有哪些待办？")  # 恢复 A
```

仅保留真实 API 客户端。原 CLI、脚本化模型和固定搜索数据已移除；三个工具为 calculator、search、todo：计算器与 todo 在本地执行，search 调用中文维基百科的公开 API，返回标题、简介（可能为空）和链接；它不是实时天气或通用网页搜索。入口统一为 main.py。

上下文由系统规则、旧历史摘要、当前未完成 todo、最近对话及当前工具结果构成。工具 Schema 通过 API 的 tools 参数另传；不保存或回灌 trace 和调用前的长篇说明。工具结果超过上限时变成带 truncated 标记的有效 JSON，完整 todo 状态仍存在数据库。

默认限制为消息序列化后 24000 字符、摘要 3000 字符、单条工具结果 3000 字符、输入 6000 字符，保留最近 3 个用户轮次（包括当前轮）。这是字符预算，不是精确 token 预算，不包含另传的工具 Schema。超过阈值时通过同一个真实 API 合并旧摘要与旧历史，保留关键事实、偏好、决策和未完成事项。按完整用户轮次切分，工具调用与结果不会拆开。摘要有效后替换旧历史并持久化，不保留完整历史归档。

摘要请求失败或摘要无效时，数据库中的旧历史保持不变。最近对话、必要状态和当前结果本身仍超限时，抛出 ContextLimitError；可以调大 ContextPolicy.max_chars 或新建会话，不静默删除当前信息。摘要质量依赖模型，不能保证无损。

每轮工具完成后将状态变化与整组调用/结果原子提交，再构造下一步 context。同一 session 并发写入使用 revision 乐观锁，冲突抛 SessionConflict，避免覆盖。当前工具只修改本地会话、计算或读取百科搜索结果；若以后增加外部副作用工具，还需单独设计幂等机制。

模型请求失败会抛出异常，已提交状态保留；用 resume() 或 main.py 的 RESUME=True 继续。已提交的工具调用不重做。完成的请求不可 resume。每个新请求默认最多执行 8 轮模型决策；步骤数在调用前持久化，resume 沿用剩余预算，摘要调用另计。旧数据库会自动补充步骤计数列。

测试可在 IDE 中运行 tests。普通测试验证真实 SQLite、context 转换和工具；验收测试仅在 HTTP 传输边界控制响应，不替换 Runtime 或 DeepSeekClient，也不恢复 FakeLLM 运行入口。SDK 保留单套重试预算：超时与临时限流最多重试两次，耗尽后异常上抛，已提交状态可恢复；认证失败不重试。真实 API 测试需要在测试运行配置中设置 DEEPSEEK_API_KEY 与 RUN_REAL_API_TESTS=1，会产生实际 API 调用。

阅读顺序与修改清单见 [AGENT_FLOW.md](AGENT_FLOW.md)。

本次验收的覆盖映射、修复范围和实测结果见 [ACCEPTANCE.md](ACCEPTANCE.md)。

原始任务 Prompt 见 [prompt.md](prompt.md)；实际运行的系统/摘要 Prompt 和工具 Schema 见 [PROMPTS.md](PROMPTS.md)。开发中遇到的问题与尚未完成、未验证的事项见 [ISSUES_AND_LIMITATIONS.md](ISSUES_AND_LIMITATIONS.md)。
