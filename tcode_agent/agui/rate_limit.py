"""SQLite 固定窗口限流，供多 worker 的公开 Gateway 共用。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from ..sqlite_runtime import connect as sqlite_connect

from ..runtime.run_coordinator import _gateway_db_path


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = max(1, int(retry_after))


class GatewayRateLimiter:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _gateway_db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_rate_limits (
                    identity_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    window_started INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    PRIMARY KEY(identity_key, action, window_started)
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def consume(
        self,
        identity_key: str,
        action: str,
        *,
        limit: int,
        window_seconds: int = 60,
    ) -> int:
        normalized_limit = max(1, int(limit))
        window = max(1, int(window_seconds))
        now = int(time.time())
        started = now - now % window
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_count FROM agui_rate_limits
                WHERE identity_key=? AND action=? AND window_started=?
                """,
                (identity_key, action, started),
            ).fetchone()
            count = int(row[0]) if row else 0
            if count >= normalized_limit:
                connection.rollback()
                raise RateLimitExceeded(started + window - now)
            next_count = count + 1
            connection.execute(
                """
                INSERT INTO agui_rate_limits(identity_key, action, window_started, request_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(identity_key, action,window_started)
                DO UPDATE SET request_count=excluded.request_count
                """,
                (identity_key, action, started, next_count),
            )
            connection.execute(
                "DELETE FROM agui_rate_limits WHERE window_started < ?",
                (started - window * 2,),
            )
            connection.commit()
            return normalized_limit - next_count


__all__ = ["GatewayRateLimiter", "RateLimitExceeded"]
