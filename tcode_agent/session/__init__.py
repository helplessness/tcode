"""Session management"""

from .manager import Session, SessionManager
from .events import SessionEventIntegrityError, SessionEventStore
from .store_sqlite import SessionSQLiteStore

__all__ = [
    "Session",
    "SessionEventIntegrityError",
    "SessionEventStore",
    "SessionManager",
    "SessionSQLiteStore",
]

"""
Session 从 sessions.json 升级成了 SQLite + FTS，并新增了 session_search 工具
"""
