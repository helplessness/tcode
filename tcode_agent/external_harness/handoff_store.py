"""Durable Harness selection and one-shot context handoff storage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from ..sqlite_runtime import connect as sqlite_connect

from ..runtime.harness_context import HarnessHandoffContext


def _db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (
        Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
        / ".tcode"
        / "agent-gateway.sqlite3"
    ).resolve(strict=False)


class HarnessHandoffError(RuntimeError):
    code = "harness_handoff_error"

    def __init__(self, message: str, *, state: dict[str, Any] | None = None) -> None:
        self.state = dict(state or {})
        super().__init__(message)


class HarnessStateConflict(HarnessHandoffError):
    code = "harness_state_conflict"


class HarnessSwitchInProgress(HarnessHandoffError):
    code = "harness_switch_in_progress"


class HarnessRuntimeMismatch(HarnessHandoffError):
    code = "invalid_runtime_transition"


class HarnessIdempotencyConflict(HarnessHandoffError):
    code = "harness_state_conflict"


def switch_request_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class HarnessHandoffStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS session_harness_state (
                    owner_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    current_runtime_id TEXT NOT NULL DEFAULT 'tcode-native',
                    runtime_models_json TEXT NOT NULL DEFAULT '{}',
                    switch_version INTEGER NOT NULL DEFAULT 0,
                    preparing_operation_id TEXT NOT NULL DEFAULT '',
                    preparing_expires_at REAL NOT NULL DEFAULT 0,
                    pending_handoff_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_key, workspace_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS harness_handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    from_runtime_id TEXT NOT NULL,
                    to_runtime_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    recent_messages_json TEXT NOT NULL,
                    summary_status TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    warning_code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_run_id TEXT NOT NULL DEFAULT '',
                    delivery_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    delivered_at REAL NOT NULL DEFAULT 0,
                    superseded_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_harness_handoffs_pending
                ON harness_handoffs(
                    owner_key, workspace_id, session_id, to_runtime_id,
                    status, created_at
                );
                CREATE TABLE IF NOT EXISTS harness_switch_idempotency (
                    owner_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner_key, session_id, idempotency_key)
                );
                """
            )
            connection.commit()
            # Serialize the schema inspection across workers. Early pre-release
            # builds used a payload-level UNIQUE constraint, which forced a
            # delivered handoff_id to be reused when users switched away and
            # back without adding a message.
            connection.execute("BEGIN IMMEDIATE")
            table_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='harness_handoffs'"
            ).fetchone()
            table_sql = str(table_row["sql"] or "") if table_row is not None else ""
            if "UNIQUE" in table_sql.upper():
                connection.execute("DROP INDEX IF EXISTS idx_harness_handoffs_pending")
                connection.execute(
                    "ALTER TABLE harness_handoffs RENAME TO harness_handoffs_legacy_unique"
                )
                connection.execute(
                    """
                    CREATE TABLE harness_handoffs (
                        handoff_id TEXT PRIMARY KEY,
                        owner_key TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        from_runtime_id TEXT NOT NULL,
                        to_runtime_id TEXT NOT NULL,
                        source_revision INTEGER NOT NULL,
                        source_digest TEXT NOT NULL,
                        summary_json TEXT NOT NULL,
                        recent_messages_json TEXT NOT NULL,
                        summary_status TEXT NOT NULL,
                        truncated INTEGER NOT NULL DEFAULT 0,
                        warning_code TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        delivered_run_id TEXT NOT NULL DEFAULT '',
                        delivery_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        delivered_at REAL NOT NULL DEFAULT 0,
                        superseded_at REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO harness_handoffs (
                        handoff_id, owner_key, workspace_id, session_id,
                        from_runtime_id, to_runtime_id, source_revision,
                        source_digest, summary_json, recent_messages_json,
                        summary_status, truncated, warning_code, status,
                        delivered_run_id, delivery_error, created_at,
                        delivered_at, superseded_at
                    )
                    SELECT
                        handoff_id, owner_key, workspace_id, session_id,
                        from_runtime_id, to_runtime_id, source_revision,
                        source_digest, summary_json, recent_messages_json,
                        summary_status, truncated, warning_code, status,
                        delivered_run_id, delivery_error, created_at,
                        delivered_at, superseded_at
                    FROM harness_handoffs_legacy_unique
                    """
                )
                connection.execute("DROP TABLE harness_handoffs_legacy_unique")
                connection.execute(
                    """
                    CREATE INDEX idx_harness_handoffs_pending
                    ON harness_handoffs(
                        owner_key, workspace_id, session_id, to_runtime_id,
                        status, created_at
                    )
                    """
                )
            connection.commit()

    @staticmethod
    def _metadata_defaults(metadata: dict[str, Any] | None) -> tuple[str, dict[str, str], int]:
        harness = (
            metadata.get("harness")
            if isinstance(metadata, dict) and isinstance(metadata.get("harness"), dict)
            else {}
        )
        runtime_id = str(harness.get("runtime_id") or "tcode-native").strip() or "tcode-native"
        models = {
            str(key): str(value)
            for key, value in dict(harness.get("models") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        legacy_model = str(harness.get("model_id") or "").strip()
        if legacy_model:
            models.setdefault(runtime_id, legacy_model)
        try:
            version = max(0, int(harness.get("switch_version") or 0))
        except (TypeError, ValueError):
            version = 0
        return runtime_id, models, version

    @staticmethod
    def _decode_state(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            models = json.loads(str(value.get("runtime_models_json") or "{}"))
        except Exception:
            models = {}
        value["runtime_models"] = models if isinstance(models, dict) else {}
        value.pop("runtime_models_json", None)
        return value

    def get_state(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        session_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        runtime_id, models, version = self._metadata_defaults(session_metadata)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO session_harness_state (
                    owner_key, workspace_id, session_id, current_runtime_id,
                    runtime_models_json, switch_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_key,
                    workspace_id,
                    session_id,
                    runtime_id,
                    json.dumps(models, ensure_ascii=False, separators=(",", ":")),
                    version,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE session_harness_state
                SET preparing_operation_id='', preparing_expires_at=0
                WHERE owner_key=? AND workspace_id=? AND session_id=?
                  AND preparing_expires_at > 0 AND preparing_expires_at < ?
                """,
                (owner_key, workspace_id, session_id, now),
            )
            connection.commit()
            row = connection.execute(
                """SELECT * FROM session_harness_state
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (owner_key, workspace_id, session_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to create harness state")
        return self._decode_state(row)

    def idempotent_result(
        self,
        *,
        owner_key: str,
        session_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT request_digest, response_json
                   FROM harness_switch_idempotency
                   WHERE owner_key=? AND session_id=? AND idempotency_key=?""",
                (owner_key, session_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_digest"]) != request_digest:
            raise HarnessIdempotencyConflict(
                "Idempotency-Key was already used with a different request"
            )
        response_json = str(row["response_json"] or "")
        if not response_json:
            raise HarnessSwitchInProgress("the idempotent switch is still in progress")
        value = json.loads(response_json)
        return dict(value) if isinstance(value, dict) else None

    def reserve_switch(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        target_runtime_id: str,
        expected_current_runtime_id: str,
        expected_switch_version: int,
        operation_id: str,
        idempotency_key: str,
        request_digest: str,
        lease_seconds: float = 30,
    ) -> tuple[str, dict[str, Any]]:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM session_harness_state
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (owner_key, workspace_id, session_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("harness state must be initialized before reserve")
            state = self._decode_state(row)
            preparing = str(state.get("preparing_operation_id") or "")
            preparing_expires = float(state.get("preparing_expires_at") or 0)
            if preparing and preparing_expires >= now and preparing != operation_id:
                connection.rollback()
                raise HarnessSwitchInProgress("another Harness switch is being prepared", state=state)
            current = str(state.get("current_runtime_id") or "tcode-native")
            version = int(state.get("switch_version") or 0)
            if current != expected_current_runtime_id or version != expected_switch_version:
                connection.rollback()
                raise HarnessStateConflict("the Harness state changed", state=state)
            existing_key = connection.execute(
                """SELECT request_digest, response_json
                   FROM harness_switch_idempotency
                   WHERE owner_key=? AND session_id=? AND idempotency_key=?""",
                (owner_key, session_id, idempotency_key),
            ).fetchone()
            if existing_key is not None:
                connection.rollback()
                if str(existing_key["request_digest"]) != request_digest:
                    raise HarnessIdempotencyConflict(
                        "Idempotency-Key was already used with a different request"
                    )
                if str(existing_key["response_json"] or ""):
                    return "cached", state
                raise HarnessSwitchInProgress("the idempotent switch is still in progress", state=state)
            connection.execute(
                """INSERT INTO harness_switch_idempotency (
                       owner_key, session_id, idempotency_key, request_digest,
                       operation_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    owner_key,
                    session_id,
                    idempotency_key,
                    request_digest,
                    operation_id,
                    now,
                ),
            )
            if target_runtime_id == current:
                connection.commit()
                return "noop", state
            connection.execute(
                """UPDATE session_harness_state
                   SET preparing_operation_id=?, preparing_expires_at=?, updated_at=?
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (
                    operation_id,
                    now + max(5.0, float(lease_seconds)),
                    now,
                    owner_key,
                    workspace_id,
                    session_id,
                ),
            )
            connection.commit()
            state["preparing_operation_id"] = operation_id
            state["preparing_expires_at"] = now + max(5.0, float(lease_seconds))
            return "reserved", state

    def complete_noop(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        idempotency_key: str,
        runtime_id: str,
        model_id: str,
        response: dict[str, Any],
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT runtime_models_json FROM session_harness_state
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (owner_key, workspace_id, session_id),
            ).fetchone()
            if row is not None and model_id:
                try:
                    models = json.loads(str(row["runtime_models_json"] or "{}"))
                except Exception:
                    models = {}
                models = dict(models) if isinstance(models, dict) else {}
                models[runtime_id] = model_id
                connection.execute(
                    """UPDATE session_harness_state
                       SET runtime_models_json=?, updated_at=?
                       WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                    (
                        json.dumps(models, ensure_ascii=False, separators=(",", ":")),
                        time.time(),
                        owner_key,
                        workspace_id,
                        session_id,
                    ),
                )
            connection.execute(
                """UPDATE harness_switch_idempotency SET response_json=?
                   WHERE owner_key=? AND session_id=? AND idempotency_key=?""",
                (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    owner_key,
                    session_id,
                    idempotency_key,
                ),
            )
            connection.commit()

    def commit_switch(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        operation_id: str,
        idempotency_key: str,
        from_runtime_id: str,
        to_runtime_id: str,
        model_id: str,
        source_revision: int,
        source_digest: str,
        handoff: HarnessHandoffContext | None,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM session_harness_state
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (owner_key, workspace_id, session_id),
            ).fetchone()
            if row is None or str(row["preparing_operation_id"] or "") != operation_id:
                connection.rollback()
                raise HarnessStateConflict("Harness switch reservation was lost")
            connection.execute(
                """UPDATE harness_handoffs SET status='superseded', superseded_at=?
                   WHERE owner_key=? AND workspace_id=? AND session_id=? AND status='pending'""",
                (now, owner_key, workspace_id, session_id),
            )
            pending_handoff_id = ""
            if handoff is not None:
                pending_handoff_id = handoff.handoff_id
                summary_json = json.dumps(
                    handoff.summary.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                recent_json = json.dumps(
                    [item.model_dump(mode="json") for item in handoff.recent_messages],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                connection.execute(
                    """INSERT INTO harness_handoffs (
                           handoff_id, owner_key, workspace_id, session_id,
                           from_runtime_id, to_runtime_id, source_revision,
                           source_digest, summary_json, recent_messages_json,
                           summary_status, truncated, warning_code, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        handoff.handoff_id,
                        owner_key,
                        workspace_id,
                        session_id,
                        from_runtime_id,
                        to_runtime_id,
                        int(source_revision),
                        source_digest,
                        summary_json,
                        recent_json,
                        handoff.summary_status,
                        int(handoff.truncated),
                        handoff.warning_code,
                        now,
                    ),
                )
            models = self._decode_state(row).get("runtime_models", {})
            models = dict(models) if isinstance(models, dict) else {}
            if model_id:
                models[to_runtime_id] = model_id
            new_version = int(row["switch_version"] or 0) + 1
            connection.execute(
                """UPDATE session_harness_state
                   SET current_runtime_id=?, runtime_models_json=?, switch_version=?,
                       preparing_operation_id='', preparing_expires_at=0,
                       pending_handoff_id=?, updated_at=?
                   WHERE owner_key=? AND workspace_id=? AND session_id=?""",
                (
                    to_runtime_id,
                    json.dumps(models, ensure_ascii=False, separators=(",", ":")),
                    new_version,
                    pending_handoff_id,
                    now,
                    owner_key,
                    workspace_id,
                    session_id,
                ),
            )
            response = dict(response)
            response["switch_version"] = new_version
            if response.get("handoff") and pending_handoff_id:
                response["handoff"] = dict(response["handoff"])
                response["handoff"]["handoff_id"] = pending_handoff_id
            connection.execute(
                """UPDATE harness_switch_idempotency SET response_json=?
                   WHERE owner_key=? AND session_id=? AND idempotency_key=?""",
                (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    owner_key,
                    session_id,
                    idempotency_key,
                ),
            )
            connection.commit()
            return response

    def abort_switch(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE session_harness_state
                   SET preparing_operation_id='', preparing_expires_at=0
                   WHERE owner_key=? AND workspace_id=? AND session_id=?
                     AND preparing_operation_id=?""",
                (owner_key, workspace_id, session_id, operation_id),
            )
            connection.execute(
                """DELETE FROM harness_switch_idempotency
                   WHERE owner_key=? AND session_id=? AND idempotency_key=?
                     AND operation_id=? AND response_json=''""",
                (owner_key, session_id, idempotency_key, operation_id),
            )
            connection.commit()

    def pending_for_target(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        target_runtime_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM harness_handoffs
                   WHERE owner_key=? AND workspace_id=? AND session_id=?
                     AND to_runtime_id=? AND status='pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (owner_key, workspace_id, session_id, target_runtime_id),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            context = HarnessHandoffContext(
                handoff_id=str(value["handoff_id"]),
                from_runtime_id=str(value["from_runtime_id"]),
                to_runtime_id=str(value["to_runtime_id"]),
                source_revision=int(value["source_revision"]),
                summary=json.loads(str(value["summary_json"])),
                recent_messages=json.loads(str(value["recent_messages_json"])),
                summary_status=str(value["summary_status"]),
                truncated=bool(value["truncated"]),
                warning_code=str(value.get("warning_code") or ""),
            )
        except Exception as exc:
            self.mark_failed(str(value.get("handoff_id") or ""), error=type(exc).__name__)
            return None
        return context.model_dump(mode="json")

    def mark_delivered(self, handoff_id: str, *, run_id: str) -> bool:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE harness_handoffs
                   SET status='delivered', delivered_run_id=?, delivered_at=?
                   WHERE handoff_id=? AND status='pending'""",
                (run_id, now, handoff_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE session_harness_state SET pending_handoff_id='', updated_at=?
                       WHERE pending_handoff_id=?""",
                    (now, handoff_id),
                )
            connection.commit()
            return bool(cursor.rowcount)

    def mark_failed(self, handoff_id: str, *, error: str) -> bool:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE harness_handoffs
                   SET status='failed', delivery_error=?, delivered_at=?
                   WHERE handoff_id=? AND status='pending'""",
                (str(error or "")[:500], now, handoff_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE session_harness_state SET pending_handoff_id='', updated_at=?
                       WHERE pending_handoff_id=?""",
                    (now, handoff_id),
                )
            connection.commit()
            return bool(cursor.rowcount)

    def assert_run_allowed(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        session_id: str,
        runtime_id: str,
        session_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.get_state(
            owner_key=owner_key,
            workspace_id=workspace_id,
            session_id=session_id,
            session_metadata=session_metadata,
        )
        preparing = str(state.get("preparing_operation_id") or "")
        if preparing and float(state.get("preparing_expires_at") or 0) >= time.time():
            raise HarnessSwitchInProgress("Harness switch is still preparing", state=state)
        current = str(state.get("current_runtime_id") or "tcode-native")
        if current != runtime_id:
            raise HarnessRuntimeMismatch(
                f"requested runtime {runtime_id!r} is not the current Harness", state=state
            )
        return state

    def projection(self, state: dict[str, Any]) -> dict[str, Any]:
        pending_id = str(state.get("pending_handoff_id") or "")
        summary_status = ""
        truncated = False
        warning_code = ""
        previous_runtime_id = ""
        if pending_id:
            with self._lock, closing(self._connect()) as connection:
                row = connection.execute(
                    """SELECT summary_status, truncated, warning_code, from_runtime_id
                       FROM harness_handoffs WHERE handoff_id=? AND status='pending'""",
                    (pending_id,),
                ).fetchone()
            if row is not None:
                summary_status = str(row["summary_status"] or "")
                truncated = bool(row["truncated"])
                warning_code = str(row["warning_code"] or "")
                previous_runtime_id = str(row["from_runtime_id"] or "")
        current = str(state.get("current_runtime_id") or "tcode-native")
        models = dict(state.get("runtime_models") or {})
        return {
            "runtime_id": current,
            "model_id": str(models.get(current) or ""),
            "models": models,
            "switch_version": int(state.get("switch_version") or 0),
            "pending_handoff_id": pending_id,
            "handoff_notice_pending": bool(pending_id),
            "summary_status": summary_status,
            "truncated": truncated,
            "warning_code": warning_code,
            "previous_runtime_id": previous_runtime_id,
        }

    def delete_session(
        self, *, owner_key: str, workspace_id: str, session_id: str
    ) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM harness_handoffs WHERE owner_key=? AND workspace_id=? AND session_id=?",
                (owner_key, workspace_id, session_id),
            )
            connection.execute(
                "DELETE FROM harness_switch_idempotency WHERE owner_key=? AND session_id=?",
                (owner_key, session_id),
            )
            connection.execute(
                "DELETE FROM session_harness_state WHERE owner_key=? AND workspace_id=? AND session_id=?",
                (owner_key, workspace_id, session_id),
            )
            connection.commit()


__all__ = [
    "HarnessHandoffError",
    "HarnessHandoffStore",
    "HarnessIdempotencyConflict",
    "HarnessRuntimeMismatch",
    "HarnessStateConflict",
    "HarnessSwitchInProgress",
    "switch_request_digest",
]
