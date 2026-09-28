"""SQLite 会话存储：校验用户归属，并用事务与版本号保护整轮状态提交。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


# 会话已被其他请求更新，拒绝用旧快照覆盖新状态。
class SessionConflict(RuntimeError):
    pass


# 单个会话的快照：历史、摘要、待办及用于并发校验的版本号。
@dataclass
class Session:
    user_id: str
    session_id: str
    history: list[dict] = field(default_factory=list)
    summary: str = ""
    todos: list[dict] = field(default_factory=list)
    revision: int = 0
    steps_used: int = 0  # 当前请求已消耗的模型决策轮数，恢复时不重置。


# 持久化会话内容，以及每个用户当前选中的会话。
class SessionStore:
    # 确保数据库目录存在，并初始化会话表和当前会话表。
    def __init__(self, path: str = "data/agent.sqlite3"):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    history TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '',
                    todos TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS active_sessions (
                    user_id TEXT PRIMARY KEY, session_id TEXT NOT NULL
                );
            """)
            # 兼容此前已创建的数据库，不清空用户历史。
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "steps_used" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN steps_used INTEGER NOT NULL DEFAULT 0")

    # 每次操作使用独立连接；成功提交、异常回滚，最后关闭连接。
    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # 生成独立 session_id，并在同一事务内设为该用户的当前会话。
    def create(self, user_id: str) -> Session:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        session = Session(user_id, uuid4().hex)
        with self._connect() as db:
            db.execute("INSERT INTO sessions(session_id, user_id) VALUES (?, ?)",
                       (session.session_id, user_id))
            db.execute("INSERT OR REPLACE INTO active_sessions VALUES (?, ?)",
                       (user_id, session.session_id))
        return session

    # 未指定 ID 时加载当前会话；始终同时检查 user_id 和 session_id。
    def load(self, user_id: str, session_id: str | None = None) -> Session:
        with self._connect() as db:
            if session_id is None:
                active = db.execute("SELECT session_id FROM active_sessions WHERE user_id=?",
                                    (user_id,)).fetchone()
                if active is None:
                    raise LookupError("Create or switch to a session first")
                session_id = active["session_id"]
            row = db.execute("SELECT * FROM sessions WHERE user_id=? AND session_id=?",
                             (user_id, session_id)).fetchone()
        if row is None:
            raise LookupError("Session not found for this user")
        return Session(user_id, session_id, json.loads(row["history"]), row["summary"],
                       json.loads(row["todos"]), row["revision"], row["steps_used"])

    # 确认会话属于该用户后，持久化新的当前选择。
    def switch(self, user_id: str, session_id: str) -> Session:
        session = self.load(user_id, session_id)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO active_sessions VALUES (?, ?)",
                       (user_id, session_id))
        return session

    # 按创建顺序列出该用户的所有会话 ID。
    def list(self, user_id: str) -> list[str]:
        with self._connect() as db:
            return [row[0] for row in db.execute(
                "SELECT session_id FROM sessions WHERE user_id=? ORDER BY rowid", (user_id,))]

    # 仅在版本匹配时一次提交历史、摘要和待办，防止并发覆盖。
    def save(self, session: Session) -> None:
        with self._connect() as db:
            result = db.execute("""UPDATE sessions SET history=?, summary=?, todos=?, steps_used=?,
                revision=revision+1 WHERE user_id=? AND session_id=? AND revision=?""",
                (json.dumps(session.history, ensure_ascii=False), session.summary,
                 json.dumps(session.todos, ensure_ascii=False), session.steps_used, session.user_id,
                 session.session_id, session.revision))
            if result.rowcount != 1:
                raise SessionConflict("Session changed concurrently; reload before retrying")
        session.revision += 1
