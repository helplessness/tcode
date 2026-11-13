from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from ...sqlite_runtime import connect as sqlite_connect


@dataclass(frozen=True)
class PersistentRuntimeRecord:
    runtime_id: str
    runtime_key: str
    tenant_id: str
    user_id: str
    workspace_id: str
    profile_name: str
    host_workspace: str
    image: str
    spec_fingerprint: str
    container_id: str
    container_name: str
    status: str
    network_policy: str
    network_name: str
    network_attachments: str
    idle_stop_seconds: int
    retention_seconds: int
    created_at: float
    last_used_at: float
    stopped_at: float = 0.0
    generation: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PersistentRuntimeRegistry:
    """SQLite-backed registry for workspace-affine sandbox containers."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS persistent_runtimes (
                    runtime_id TEXT PRIMARY KEY,
                    runtime_key TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    host_workspace TEXT NOT NULL,
                    image TEXT NOT NULL,
                    spec_fingerprint TEXT NOT NULL DEFAULT '',
                    container_id TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    network_policy TEXT NOT NULL,
                    network_name TEXT NOT NULL,
                    network_attachments TEXT NOT NULL,
                    idle_stop_seconds INTEGER NOT NULL,
                    retention_seconds INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    stopped_at REAL NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(persistent_runtimes)"
                ).fetchall()
            }
            if "spec_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE persistent_runtimes "
                    "ADD COLUMN spec_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_persistent_runtimes_owner
                ON persistent_runtimes (tenant_id, user_id, workspace_id)
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _record(row: sqlite3.Row | None) -> PersistentRuntimeRecord | None:
        return PersistentRuntimeRecord(**dict(row)) if row is not None else None

    def get(self, runtime_id: str) -> PersistentRuntimeRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM persistent_runtimes WHERE runtime_id = ?",
                (str(runtime_id or "").strip(),),
            ).fetchone()
        return self._record(row)

    def get_by_key(self, runtime_key: str) -> PersistentRuntimeRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM persistent_runtimes WHERE runtime_key = ?",
                (str(runtime_key or "").strip(),),
            ).fetchone()
        return self._record(row)

    def put(self, record: PersistentRuntimeRecord) -> None:
        values = record.to_dict()
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column != "runtime_id"
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO persistent_runtimes ({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(runtime_id) DO UPDATE SET {updates}
                """,
                tuple(values[column] for column in columns),
            )

    def update(self, runtime_id: str, **changes: Any) -> PersistentRuntimeRecord | None:
        allowed = {
            "container_id",
            "container_name",
            "status",
            "network_name",
            "network_attachments",
            "last_used_at",
            "stopped_at",
            "generation",
        }
        normalized = {key: value for key, value in changes.items() if key in allowed}
        if not normalized:
            return self.get(runtime_id)
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE persistent_runtimes SET {assignments} WHERE runtime_id = ?",
                (*normalized.values(), str(runtime_id or "").strip()),
            )
        return self.get(runtime_id)

    def touch(self, runtime_id: str) -> PersistentRuntimeRecord | None:
        return self.update(
            runtime_id,
            status="running",
            last_used_at=time.time(),
            stopped_at=0.0,
        )

    def list(
        self,
        *,
        tenant_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
    ) -> list[PersistentRuntimeRecord]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("workspace_id", workspace_id),
        ):
            text = str(value or "").strip()
            if text:
                clauses.append(f"{column} = ?")
                parameters.append(text)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM persistent_runtimes{where} ORDER BY last_used_at DESC",
                tuple(parameters),
            ).fetchall()
        return [PersistentRuntimeRecord(**dict(row)) for row in rows]

    def delete(self, runtime_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM persistent_runtimes WHERE runtime_id = ?",
                (str(runtime_id or "").strip(),),
            )
