"""可恢复、幂等的交互状态与工具执行 fence。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from ..sqlite_runtime import connect as sqlite_connect

import jsonschema


class InteractionConflictError(ValueError):
    pass


class InteractionNotFoundError(LookupError):
    pass


class InteractionCancelledError(RuntimeError):
    pass


def _db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd())) / ".tcode" / "agent-gateway.sqlite3").resolve(strict=False)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class InteractionStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    interaction_id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    origin_run_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL,
                    response_schema_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    resolution_json TEXT NOT NULL DEFAULT '{}',
                    resolution_digest TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_interactions_owner_pending
                ON interactions(owner_key, session_id, status, created_at);
                CREATE TABLE IF NOT EXISTS tool_execution_fences (
                    owner_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    interaction_id TEXT NOT NULL,
                    approved_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_event_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_key, session_id, tool_call_id)
                );
                """
            )
            connection.commit()

    def create(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        origin_run_id: str,
        kind: str,
        reason: str,
        prompt: str,
        request: dict[str, Any],
        response_schema: dict[str, Any] | None = None,
        tool_call_id: str = "",
        expires_in_seconds: int = 3600,
        interaction_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        normalized_kind = str(kind or "").strip()
        if normalized_kind not in {"question", "permission", "review"}:
            raise ValueError("unsupported interaction kind")
        interaction_id = interaction_id or f"int_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + max(30, min(int(expires_in_seconds), 7 * 24 * 60 * 60))
        request_json = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        schema_json = json.dumps(response_schema or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                if str(record.get("owner_key") or "") != owner_key or str(record.get("request_json") or "") != request_json:
                    raise InteractionConflictError("interaction id was reused with different request")
                return self._public(record)
            connection.execute(
                """
                INSERT INTO interactions (
                    interaction_id, owner_key, workspace_id, session_id,
                    origin_run_id, tool_call_id, kind, reason, prompt,
                    request_json, response_schema_json, status, expires_at,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    owner_key,
                    workspace_id,
                    session_id,
                    origin_run_id,
                    tool_call_id,
                    normalized_kind,
                    str(reason or "interaction_required")[:200],
                    str(prompt or "")[:20_000],
                    request_json,
                    schema_json,
                    expires_at,
                    str(idempotency_key or "")[:200],
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
        return self._public(dict(row))

    def get(self, interaction_id: str, *, owner_key: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=? AND owner_key=?",
                (interaction_id, owner_key),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        if str(record.get("status") or "") == "pending" and float(record.get("expires_at") or 0) <= time.time():
            self._set_status(interaction_id, owner_key=owner_key, status="expired")
            record["status"] = "expired"
        return self._public(record)

    def list_pending(self, *, owner_key: str, session_id: str = "") -> list[dict[str, Any]]:
        clauses = ["owner_key=?", "status='pending'", "expires_at>?"]
        params: list[Any] = [owner_key, time.time()]
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM interactions WHERE {' AND '.join(clauses)} ORDER BY created_at ASC",
                tuple(params),
            ).fetchall()
        return [self._public(dict(row)) for row in rows]

    def resolve(
        self,
        interaction_id: str,
        *,
        owner_key: str,
        resolution: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        digest = _digest(resolution)
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=? AND owner_key=?",
                (interaction_id, owner_key),
            ).fetchone()
            if row is None:
                raise InteractionNotFoundError("interaction not found")
            record = dict(row)
            status = str(record.get("status") or "")
            if status == "resolved":
                if str(record.get("resolution_digest") or "") != digest:
                    raise InteractionConflictError("interaction was already resolved differently")
                return "existing", self._public(record)
            if status != "pending":
                raise InteractionConflictError(f"interaction is {status}")
            if float(record.get("expires_at") or 0) <= now:
                connection.execute(
                    "UPDATE interactions SET status='expired', updated_at=? WHERE interaction_id=?",
                    (now, interaction_id),
                )
                connection.commit()
                raise InteractionConflictError("interaction expired")
            schema = json.loads(str(record.get("response_schema_json") or "{}"))
            if schema:
                jsonschema.validate(resolution, schema)
            connection.execute(
                """
                UPDATE interactions SET status='resolved', resolution_json=?,
                    resolution_digest=?, resolved_at=?, updated_at=?
                WHERE interaction_id=? AND owner_key=? AND status='pending'
                """,
                (
                    json.dumps(resolution, ensure_ascii=False, separators=(",", ":")),
                    digest,
                    now,
                    now,
                    interaction_id,
                    owner_key,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
        public = self._public(dict(updated))
        for waiter in self._waiters.pop(interaction_id, []):
            if not waiter.done():
                waiter.set_result(dict(resolution))
        return "resolved", public

    async def wait_for_resolution(self, interaction_id: str, *, owner_key: str) -> dict[str, Any]:
        existing = self.get(interaction_id, owner_key=owner_key)
        if existing is None:
            raise InteractionNotFoundError("interaction not found")
        if existing["status"] == "resolved":
            return dict(existing.get("resolution") or {})
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiters.setdefault(interaction_id, []).append(future)
        try:
            while True:
                existing = self.get(interaction_id, owner_key=owner_key)
                if existing is None:
                    raise InteractionNotFoundError("interaction not found")
                if existing["status"] == "resolved":
                    return dict(existing.get("resolution") or {})
                if existing["status"] == "cancelled":
                    raise InteractionCancelledError("interaction was cancelled")
                if existing["status"] == "expired":
                    raise asyncio.TimeoutError("interaction expired")
                remaining = float(existing["expires_at"]) - time.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError("interaction expired")
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future), timeout=min(0.25, remaining)
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            waiters = self._waiters.get(interaction_id, [])
            if future in waiters:
                waiters.remove(future)
            if not waiters:
                self._waiters.pop(interaction_id, None)

    def cancel_pending_for_run(self, *, owner_key: str, origin_run_id: str) -> int:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT interaction_id FROM interactions
                WHERE owner_key=? AND origin_run_id=? AND status='pending'
                """,
                (owner_key, origin_run_id),
            ).fetchall()
            connection.execute(
                """
                UPDATE interactions SET status='cancelled', updated_at=?
                WHERE owner_key=? AND origin_run_id=? AND status='pending'
                """,
                (now, owner_key, origin_run_id),
            )
            connection.commit()
        for row in rows:
            interaction_id = str(row["interaction_id"])
            for waiter in self._waiters.pop(interaction_id, []):
                if not waiter.done():
                    waiter.set_exception(
                        InteractionCancelledError("interaction was cancelled")
                    )
        return len(rows)

    def reserve_tool_execution(
        self,
        *,
        owner_key: str,
        session_id: str,
        tool_call_id: str,
        interaction_id: str,
        approved: Any,
    ) -> str:
        approved_digest = _digest(approved)
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM tool_execution_fences
                WHERE owner_key=? AND session_id=? AND tool_call_id=?
                """,
                (owner_key, session_id, tool_call_id),
            ).fetchone()
            if row is not None:
                record = dict(row)
                if str(record.get("approved_digest") or "") != approved_digest:
                    raise InteractionConflictError("tool approval changed after fence reservation")
                return str(record.get("status") or "reserved")
            connection.execute(
                """
                INSERT INTO tool_execution_fences (
                    owner_key, session_id, tool_call_id, interaction_id,
                    approved_digest, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (owner_key, session_id, tool_call_id, interaction_id, approved_digest, now),
            )
            connection.commit()
        return "reserved"

    def complete_tool_execution(
        self,
        *,
        owner_key: str,
        session_id: str,
        tool_call_id: str,
        result_event_id: str,
        failed: bool = False,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tool_execution_fences SET status=?, result_event_id=?, updated_at=?
                WHERE owner_key=? AND session_id=? AND tool_call_id=?
                """,
                (
                    "failed" if failed else "succeeded",
                    result_event_id,
                    time.time(),
                    owner_key,
                    session_id,
                    tool_call_id,
                ),
            )
            connection.commit()

    def _set_status(self, interaction_id: str, *, owner_key: str, status: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "UPDATE interactions SET status=?, updated_at=? WHERE interaction_id=? AND owner_key=?",
                (status, time.time(), interaction_id, owner_key),
            )
            connection.commit()

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: record.get(key)
            for key in (
                "interaction_id",
                "workspace_id",
                "session_id",
                "origin_run_id",
                "tool_call_id",
                "kind",
                "reason",
                "prompt",
                "status",
                "expires_at",
                "created_at",
                "updated_at",
                "resolved_at",
            )
        }
        for target, source in (
            ("request", "request_json"),
            ("response_schema", "response_schema_json"),
            ("resolution", "resolution_json"),
        ):
            try:
                result[target] = json.loads(str(record.get(source) or "{}"))
            except Exception:
                result[target] = {}
        return result


__all__ = [
    "InteractionCancelledError",
    "InteractionConflictError",
    "InteractionNotFoundError",
    "InteractionStore",
]
