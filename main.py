"""IDE 运行入口：配置用户与会话，加载环境变量后发起真实 API 请求。"""
from pathlib import Path
import os

from dotenv import load_dotenv

from agent import AgentRuntime

PROJECT_ROOT = Path(__file__).resolve().parent
USER_ID = "local-user"
SESSION_ID = None  # 填已有 ID 可切换；None 恢复上次选中的会话。
CREATE_NEW_SESSION = False  # True 表示每次运行都创建并选中一个空会话。
QUESTION = "用计算器计算 (1+2)*3，然后告诉我结果。"
RESUME = False  # 继续中断请求，不再次追加 QUESTION。


# 相对项目根目录定位配置和数据库，再创建、恢复或切换会话。
def main():
    load_dotenv(PROJECT_ROOT / ".env")
    db_path = Path(os.getenv("AGENT_DB_PATH", "data/agent.sqlite3"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    agent = AgentRuntime(str(db_path))
    if CREATE_NEW_SESSION:
        session = agent.create_session(USER_ID)
    elif SESSION_ID:
        session = agent.switch_session(USER_ID, SESSION_ID)
    else:
        try:
            session = agent.store.load(USER_ID)
        except LookupError:
            session = agent.create_session(USER_ID)
    print(f"user_id={USER_ID}, session_id={session.session_id}")
    result = (agent.resume(USER_ID, session.session_id) if RESUME else
              agent.run(USER_ID, QUESTION, session.session_id))
    print(result.answer)


if __name__ == "__main__":
    main()
