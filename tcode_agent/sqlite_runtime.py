"""Process-local coordination for SQLite stores and native connection lifecycle."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any


SQLITE_CONNECTION_LOCK = threading.RLock()
SQLITE_LIFECYCLE_LOCK = threading.RLock()


class SQLiteConnection(sqlite3.Connection):
    """Serialize native connection teardown across the process."""

    def close(self) -> None:
        with SQLITE_LIFECYCLE_LOCK:
            super().close()

    def __del__(self) -> None:
        # Some callers still rely on collection instead of an explicit close.
        try:
            self.close()
        except Exception:
            pass


def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    """Serialize SQLite's Unix VFS open/close lifecycle across stores."""
    kwargs.setdefault("factory", SQLiteConnection)
    with SQLITE_LIFECYCLE_LOCK:
        return sqlite3.connect(*args, **kwargs)
