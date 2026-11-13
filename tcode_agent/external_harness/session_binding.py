"""TCode thread 与外部 harness 原生 session 的隔离绑定。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from ..sqlite_runtime import connect as sqlite_connect


def _db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd())) / ".tcode" / "agent-gateway.sqlite3").resolve(strict=False)


class HarnessSessionBindingStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_sessions (
                    owner_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    native_session_ref TEXT NOT NULL,
                    runtime_version TEXT NOT NULL DEFAULT '',
                    capabilities_digest TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    resume_metadata_json TEXT NOT NULL DEFAULT '{}',
                    last_used_at REAL NOT NULL,
                    PRIMARY KEY(owner_key, workspace_id, session_id, runtime_id)
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def get(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        runtime_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM harness_sessions
                WHERE owner_key=? AND workspace_id=? AND session_id=? AND runtime_id=?
                """,
                (owner_key, workspace_id, session_id, runtime_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def put(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        runtime_id: str,
        native_session_ref: str,
        runtime_version: str,
        capabilities_digest: str,
        resume_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO harness_sessions (
                    owner_key, workspace_id, session_id, runtime_id,
                    native_session_ref, runtime_version, capabilities_digest,
                    status, resume_metadata_json, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(owner_key, workspace_id, session_id, runtime_id)
                DO UPDATE SET native_session_ref=excluded.native_session_ref,
                    runtime_version=excluded.runtime_version,
                    capabilities_digest=excluded.capabilities_digest,
                    status='active', resume_metadata_json=excluded.resume_metadata_json,
                    last_used_at=excluded.last_used_at
                """,
                (
                    owner_key,
                    workspace_id,
                    session_id,
                    runtime_id,
                    native_session_ref,
                    runtime_version,
                    capabilities_digest,
                    json.dumps(resume_metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )
            connection.commit()

    def delete_session(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """DELETE FROM harness_sessions
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (owner_key, workspace_id, session_id),
            )
            connection.commit()


__all__ = ["HarnessSessionBindingStore"]
