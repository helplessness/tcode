"""跨传输共享的 run 协调器、幂等投影和取消入口。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import aclosing, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from ..sqlite_runtime import connect as sqlite_connect

from ..adapters.registry import AgentBackendRegistry
from ..protocol.v2 import RuntimeEventStateMachine, RuntimeEventValue
from ..session.manager import SessionManager
from ..session.transcript import without_legacy_native_agui_user_echoes
from ..types import Message
from .agent_backend import NormalizedRunInput, RunContext


RUN_TERMINALS = {"run.finished", "run.error", "run.cancelled", "run.interrupted"}


class RunConflictError(ValueError):
    def __init__(self, message: str, *, code: str = "run_id_conflict") -> None:
        super().__init__(message)
        self.code = code


class RunUnavailableError(ValueError):
    pass


def canonical_request_digest(run_input: NormalizedRunInput) -> str:
    payload = run_input.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _gateway_db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    base = Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
    return (base / ".tcode" / "agent-gateway.sqlite3").resolve(strict=False)


class AgentRunStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _gateway_db_path()).resolve(strict=False)
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
                CREATE TABLE IF NOT EXISTS agent_runs (
                    owner_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    client_request_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    terminal_outcome TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    terminal_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    finished_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (owner_key, session_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_owner_status
                ON agent_runs(owner_key, status, created_at);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            migrations = {
                "worker_id": "TEXT NOT NULL DEFAULT ''",
                "heartbeat_at": "REAL NOT NULL DEFAULT 0",
                "lease_expires_at": "REAL NOT NULL DEFAULT 0",
                "cancel_requested_at": "REAL NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE agent_runs ADD COLUMN {name} {definition}"
                    )
            connection.commit()

    @staticmethod
    def _lost_terminal() -> dict[str, Any]:
        return {
            "type": "run.error",
            "code": "worker_lost",
            "message": "The worker lease expired before the run completed",
            "retryable": True,
        }

    def recover_stale(self, *, queued_grace_seconds: float = 30.0) -> int:
        """把失去 worker 租约的执行投影为可重试终态。"""

        now = time.time()
        terminal = self._lost_terminal()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status='failed', terminal_outcome='error', error_code='worker_lost',
                    terminal_json=?, finished_at=?, lease_expires_at=0
                WHERE (
                    status IN ('running','committing') AND lease_expires_at > 0 AND lease_expires_at < ?
                ) OR (
                    status='queued' AND created_at < ?
                )
                """,
                (
                    json.dumps(terminal, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    now - max(1.0, float(queued_grace_seconds)),
                ),
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def reserve(
        self,
        *,
        owner_key: str,
        workspace_id: str,
        run_input: NormalizedRunInput,
        request_digest: str,
        client_request_id: str,
    ) -> tuple[str, dict[str, Any]]:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id=?",
                (owner_key, run_input.thread_id, run_input.run_id),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if str(row.get("request_digest") or "") != request_digest:
                    raise RunConflictError("runId was already used with different input")
                return "existing", row
            if connection.execute(
                "SELECT 1 FROM agent_runs WHERE owner_key=? AND session_id=? AND status IN ('queued','running','committing') LIMIT 1",
                (owner_key, run_input.thread_id),
            ).fetchone():
                raise RunConflictError("当前会话已有前台回合，请等待本轮结束。", code="session_busy")
            connection.execute(
                """
                INSERT INTO agent_runs (
                    owner_key, workspace_id, session_id, run_id, parent_run_id,
                    agent_id, runtime_id, request_digest, client_request_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    owner_key,
                    workspace_id,
                    run_input.thread_id,
                    run_input.run_id,
                    run_input.parent_run_id,
                    run_input.agent_id,
                    run_input.runtime_id,
                    request_digest,
                    client_request_id,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id=?",
                (owner_key, run_input.thread_id, run_input.run_id),
            ).fetchone()
            return "new", dict(row)

    def update(
        self,
        owner_key: str,
        session_id: str,
        run_id: str,
        *,
        status: str,
        last_sequence: int,
        terminal: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        terminal = dict(terminal or {})
        terminal_type = str(terminal.get("type") or "")
        outcome = str(terminal.get("outcome") or terminal_type.removeprefix("run.") or "")
        error_code = str(terminal.get("code") or "")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE agent_runs SET status=?, last_sequence=?, terminal_outcome=?,
                    error_code=?, terminal_json=?,
                    started_at=CASE WHEN started_at=0 THEN ? ELSE started_at END,
                    finished_at=CASE WHEN ? IN ('succeeded','failed','cancelled','interrupted')
                        THEN ? ELSE finished_at END,
                    heartbeat_at=?,
                    lease_expires_at=CASE WHEN ? IN ('succeeded','failed','cancelled','interrupted')
                        THEN 0 ELSE lease_expires_at END
                WHERE owner_key=? AND session_id=? AND run_id=?
                """,
                (
                    status,
                    int(last_sequence),
                    outcome[:120],
                    error_code[:120],
                    json.dumps(terminal, ensure_ascii=False, separators=(",", ":")),
                    now,
                    status,
                    now,
                    now,
                    status,
                    owner_key,
                    session_id,
                    run_id,
                ),
            )
            connection.commit()

    def try_claim(
        self,
        owner_key: str,
        session_id: str,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        max_active: int,
    ) -> str:
        """原子领取 queued run，并对所有 worker 施加全局并发上限。"""

        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested_at FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id=?",
                (owner_key, session_id, run_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return "missing"
            if str(row["status"] or "") != "queued":
                connection.rollback()
                return "not_queued"
            if float(row["cancel_requested_at"] or 0) > 0:
                terminal = {"type": "run.cancelled", "reason": "cancelled_before_start"}
                connection.execute(
                    """
                    UPDATE agent_runs SET status='cancelled', terminal_outcome='cancelled',
                        terminal_json=?, finished_at=?
                    WHERE owner_key=? AND session_id=? AND run_id=?
                    """,
                    (
                        json.dumps(terminal, separators=(",", ":")),
                        now,
                        owner_key,
                        session_id,
                        run_id,
                    ),
                )
                connection.commit()
                return "cancelled"
            if connection.execute(
                "SELECT 1 FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id<>? AND status IN ('running','committing') LIMIT 1",
                (owner_key, session_id, run_id),
            ).fetchone():
                connection.rollback()
                return "session_busy"
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE status IN ('running','committing') AND lease_expires_at >= ?",
                    (now,),
                ).fetchone()[0]
            )
            if active >= max(1, int(max_active)):
                connection.rollback()
                return "capacity"
            connection.execute(
                """
                UPDATE agent_runs SET status='running', worker_id=?, heartbeat_at=?,
                    lease_expires_at=?, started_at=CASE WHEN started_at=0 THEN ? ELSE started_at END
                WHERE owner_key=? AND session_id=? AND run_id=? AND status='queued'
                """,
                (
                    worker_id,
                    now,
                    now + max(2.0, float(lease_seconds)),
                    now,
                    owner_key,
                    session_id,
                    run_id,
                ),
            )
            connection.commit()
            return "claimed"

    def heartbeat(
        self,
        owner_key: str,
        session_id: str,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs SET heartbeat_at=?, lease_expires_at=?
                WHERE owner_key=? AND session_id=? AND run_id=?
                    AND worker_id=? AND status IN ('running','committing')
                """,
                (
                    now,
                    now + max(2.0, float(lease_seconds)),
                    owner_key,
                    session_id,
                    run_id,
                    worker_id,
                ),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def request_cancel(self, owner_key: str, session_id: str, run_id: str) -> bool:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id=?",
                (owner_key, session_id, run_id),
            ).fetchone()
            if row is None:
                return False
            status = str(row["status"] or "")
            if status in {"queued", "running", "committing"}:
                connection.execute(
                    """
                    UPDATE agent_runs SET cancel_requested_at=?
                    WHERE owner_key=? AND session_id=? AND run_id=?
                    """,
                    (now, owner_key, session_id, run_id),
                )
                connection.commit()
                return True
            return status == "cancelled"

    def cancel_requested(self, owner_key: str, session_id: str, run_id: str) -> bool:
        row = self.get(owner_key, session_id, run_id)
        return bool(row and float(row.get("cancel_requested_at") or 0) > 0)

    def get(self, owner_key: str, session_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE owner_key=? AND session_id=? AND run_id=?",
                (owner_key, session_id, run_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def active_for_session(
        self, owner_key: str, session_id: str
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE owner_key=? AND session_id=? AND status IN ('queued','running','committing')
                ORDER BY created_at DESC LIMIT 1
                """,
                (owner_key, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def is_session_active(self, session_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM agent_runs
                WHERE session_id=? AND status IN ('queued','running','committing')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return row is not None


@dataclass
class PreparedRun:
    kind: str
    run_input: NormalizedRunInput
    context: RunContext
    backend: Any
    request_digest: str
    existing: dict[str, Any]
    heartbeat_task: asyncio.Task | None = None


@dataclass
class _ActiveRun:
    backend: Any
    context: RunContext
    task: asyncio.Task[Any] | None = None


class RunCoordinator:
    def __init__(
        self,
        registry: AgentBackendRegistry,
        *,
        store: AgentRunStore | None = None,
        max_concurrent_runs: int | None = None,
    ) -> None:
        self.registry = registry
        self.store = store or AgentRunStore()
        configured = max_concurrent_runs or int(os.environ.get("TCODE_AGUI_MAX_CONCURRENT_RUNS", "16"))
        self.max_concurrent_runs = max(1, min(int(configured), 512))
        self._semaphore = asyncio.Semaphore(self.max_concurrent_runs)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.lease_seconds = max(
            3.0, min(float(os.environ.get("TCODE_AGUI_RUN_LEASE_SECONDS", "30")), 300.0)
        )
        self.poll_seconds = max(
            0.05, min(float(os.environ.get("TCODE_AGUI_RUN_POLL_SECONDS", "0.25")), 5.0)
        )
        self.queued_grace_seconds = max(
            self.lease_seconds * 4,
            float(os.environ.get("TCODE_AGUI_QUEUED_GRACE_SECONDS", "300")),
        )
        self.store.recover_stale(queued_grace_seconds=self.queued_grace_seconds)
        self._active: dict[tuple[str, str, str], _ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def prepare(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
        *,
        client_request_id: str = "",
    ) -> PreparedRun:
        await asyncio.to_thread(
            self.store.recover_stale,
            queued_grace_seconds=self.queued_grace_seconds,
        )
        backend = self.registry.get(run_input.runtime_id)
        if backend is None:
            raise RunUnavailableError(f"runtime is not registered: {run_input.runtime_id}")
        owner_key = f"{context.tenant_id}:{context.user_id}"
        from ..presentation.result_selection import enabled, latest_selections, CONTEXT_SCHEMA
        from .async_resume import RESUME_CONTEXT_SCHEMA
        session = await SessionManager.get(context.session_id, tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id)
        # A client-authored capsule is not restoration authority.
        clean_context = [c for c in run_input.context if c.get("schema") not in {CONTEXT_SCHEMA, RESUME_CONTEXT_SCHEMA}]
        digest = canonical_request_digest(run_input.model_copy(update={"context": clean_context}))
        if enabled() and session:
            selections = latest_selections(session.messages)
            from ..presentation.research_compare import validate_selections
            selections = await asyncio.to_thread(validate_selections, selections, session.workdir, session)
            if selections:
                clean_context.append({"schema": CONTEXT_SCHEMA, "selections": selections})
        run_input = run_input.model_copy(update={"context": clean_context})
        latest_user = next((m for m in reversed(run_input.messages) if m.role == "user"), None)
        context.async_origin = {
            **(context.async_origin or {}),
            "session_id": context.session_id,
            "request_id": context.request_id,
            "request": run_input.latest_user_text(),
            "message_id": latest_user.id if latest_user else "",
        }
        resources = dict((getattr(session, "metadata", {}) or {}).get("resource_context") or {})
        context.platform_tool_state["extra"] = {
            "workdir": context.workdir, "request_id": context.request_id,
            "origin_request_id": context.request_id, "user_message": run_input.latest_user_text(),
            "tool_trace_context": {"run_id": context.run_id},
            **{key: getattr(context, key) for key in ("tenant_id", "user_id", "workspace_id")},
            "resource_context": {key: value for key, value in resources.items()
                                 if key in {"uploaded_files", "external_mounts"}},
            "model_context_snapshot": {"messages": [m.model_dump(mode="json") for m in run_input.messages
                                                     if m.role in {"user", "assistant"}]},
        }
        kind, existing = self.store.reserve(
            owner_key=owner_key,
            workspace_id=context.workspace_id,
            run_input=run_input,
            request_digest=digest,
            client_request_id=client_request_id,
        )
        return PreparedRun(kind, run_input, context, backend, digest, existing)

    async def stream(self, prepared: PreparedRun) -> AsyncIterator[RuntimeEventValue]:
        from .async_resume import (active_origin, active_resume, claim_resume,
                                   finish_resume, resume_context_json)
        from .async_completion import async_continuation_identifier, mark_completion_continued, rollback_completion_continued
        context = prepared.context
        scope = context.async_resume_scope
        resume_token = origin_token = None
        completed = False
        pending_terminal = None
        terminal_delivered = False
        try:
            if prepared.kind != "existing":
                identifier = async_continuation_identifier(prepared.run_input.latest_user_text())
                if identifier and scope is None:
                    session = await SessionManager.get(context.session_id,
                        **{key: getattr(context, key) for key in ("tenant_id", "user_id", "workspace_id")})
                    try:
                        scope = claim_resume(session, identifier, context.request_id)
                    except ValueError as exc:
                        # Reject before backend start; don't leave a queued Run.
                        event = context.event("run.error", code="async_resume_rejected", message=str(exc), retryable=False)
                        self.store.update(f"{context.tenant_id}:{context.user_id}", context.session_id,
                                          context.run_id, status="failed", last_sequence=event.sequence,
                                          terminal=event.model_dump(mode="json", by_alias=True))
                        yield event
                        return
                    context.async_resume_scope = scope
                resume_token = active_resume.set(scope)
                origin_token = active_origin.set(context.async_origin)
                if scope:
                    prepared.run_input = prepared.run_input.model_copy(update={
                        "context": [*prepared.run_input.context, json.loads(resume_context_json(scope))]})
                    extra = context.platform_tool_state["extra"]
                    extra["resource_context"].update(scope["task_context"].get("resource_context") or {})
            async with aclosing(self._stream_run(prepared)) as events:
                async for event in events:
                    if event.type in RUN_TERMINALS:
                        pending_terminal = event
                    else:
                        yield event
            if scope and prepared.kind != "existing" and pending_terminal is not None:
                if pending_terminal.type == "run.finished" and pending_terminal.outcome == "completed":
                    try:
                        await mark_completion_continued(session_id=context.session_id, identifier=scope["identifier"],
                            attempt_id=scope["claim_id"],
                            **{key: getattr(context, key) for key in ("tenant_id", "user_id", "workspace_id")})
                        completed = True
                    except Exception as exc:
                        completed = False
                        # The withheld terminal has not occupied a public sequence.
                        context._sequence = pending_terminal.sequence - 1
                        pending_terminal = context.event("run.error", code="continuation_commit_failed",
                            message=str(exc)[:2000] or type(exc).__name__, retryable=True)
                        self.store.update(f"{context.tenant_id}:{context.user_id}", context.session_id,
                            context.run_id, status="failed", last_sequence=pending_terminal.sequence,
                            terminal=pending_terminal.model_dump(mode="json", by_alias=True))
            if pending_terminal is not None and prepared.kind != "existing":
                status = {"run.finished": "succeeded", "run.error": "failed",
                          "run.cancelled": "cancelled", "run.interrupted": "interrupted"}[pending_terminal.type]
                try:
                    self.store.update(f"{context.tenant_id}:{context.user_id}", context.session_id,
                        context.run_id, status=status, last_sequence=pending_terminal.sequence,
                        terminal=pending_terminal.model_dump(mode="json", by_alias=True))
                except Exception as exc:
                    if completed and scope:
                        await rollback_completion_continued(session_id=context.session_id, identifier=scope["identifier"],
                            attempt_id=scope["claim_id"],
                            **{key: getattr(context, key) for key in ("tenant_id", "user_id", "workspace_id")})
                    completed = False
                    context._sequence = pending_terminal.sequence - 1
                    pending_terminal = context.event("run.error", code="run_commit_failed",
                        message=str(exc)[:2000] or type(exc).__name__, retryable=True)
                    self.store.update(f"{context.tenant_id}:{context.user_id}", context.session_id,
                        context.run_id, status="failed", last_sequence=pending_terminal.sequence,
                        terminal=pending_terminal.model_dump(mode="json", by_alias=True))
            if scope and prepared.kind != "existing":
                finish_resume(scope, completed=completed)
            if pending_terminal is not None:
                terminal_delivered = True
                yield pending_terminal
        finally:
            try:
                if prepared.heartbeat_task is not None:
                    prepared.heartbeat_task.cancel()
                    await asyncio.gather(prepared.heartbeat_task, return_exceptions=True)
                if pending_terminal is not None and not terminal_delivered and prepared.kind != "existing":
                    row = self.store.get(f"{context.tenant_id}:{context.user_id}", context.session_id, context.run_id)
                    if row and row["status"] in {"running", "committing"}:
                        self.store.update(f"{context.tenant_id}:{context.user_id}", context.session_id, context.run_id,
                            status="cancelled", last_sequence=context._sequence,
                            terminal={"type": "run.cancelled", "reason": "continuation_interrupted"})
                if scope and prepared.kind != "existing":
                    finish_resume(scope, completed=completed)
            finally:
                if origin_token is not None:
                    active_origin.reset(origin_token)
                if resume_token is not None:
                    active_resume.reset(resume_token)

    async def _stream_run(self, prepared: PreparedRun) -> AsyncIterator[RuntimeEventValue]:
        context = prepared.context
        owner_key = f"{context.tenant_id}:{context.user_id}"
        key = (owner_key, context.session_id, context.run_id)
        if prepared.kind == "existing":
            async for event in self._stream_existing(prepared):
                yield event
            return

        async with self._semaphore:
            while True:
                claim = await asyncio.to_thread(
                    self.store.try_claim,
                    owner_key,
                    context.session_id,
                    context.run_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    max_active=self.max_concurrent_runs,
                )
                if claim == "claimed":
                    break
                if claim == "capacity":
                    await asyncio.sleep(self.poll_seconds)
                    continue
                if claim == "cancelled":
                    yield context.event("run.cancelled", reason="cancelled_before_start")
                    return
                if claim == "session_busy":
                    event = context.event("run.error", code="session_busy", message="当前会话已有前台回合。", retryable=True)
                    self.store.update(owner_key, context.session_id, context.run_id, status="failed",
                        last_sequence=event.sequence, terminal=event.model_dump(mode="json", by_alias=True))
                    yield event
                    return
                raise RunConflictError("run can no longer be claimed")
            async with self._lock:
                if key in self._active:
                    raise RunConflictError("run is already active")
                active = _ActiveRun(prepared.backend, context, asyncio.current_task())
                self._active[key] = active

            machine = RuntimeEventStateMachine(require_contiguous=True)
            terminal: RuntimeEventValue | None = None
            completed_messages: list[dict[str, Any]] = []
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(owner_key, context),
                name=f"agui-heartbeat:{context.run_id}",
            )
            prepared.heartbeat_task = heartbeat_task
            cancel_watch_task = asyncio.create_task(
                self._cancel_watch_loop(owner_key, active),
                name=f"agui-cancel-watch:{context.run_id}",
            )
            try:
                await self._prepare_platform_bridge(prepared)
                await self._repair_legacy_native_transcript(prepared)
                started = context.event(
                    "run.started",
                    agent_id=prepared.run_input.agent_id,
                    parent_run_id=prepared.run_input.parent_run_id,
                )
                machine.accept(started)
                self.store.update(
                    owner_key,
                    context.session_id,
                    context.run_id,
                    status="running",
                    last_sequence=started.sequence,
                )
                yield started

                async with aclosing(prepared.backend.run(prepared.run_input, context)) as backend_events:
                    async for event in backend_events:
                        if event.type in RUN_TERMINALS:
                            terminal = event
                            break
                        machine.accept(event)
                        if event.type == "message.finished":
                            completed_messages.append(event.message.model_dump(mode="json"))
                        yield event

                if terminal is None:
                    terminal = (
                        context.event("run.cancelled", reason="cancelled")
                        if context.cancelled.is_set()
                        else context.event(
                            "run.finished",
                            outcome="completed",
                            result={
                                "artifacts": [
                                    {
                                        key: item[key]
                                        for key in (
                                            "artifact_id",
                                            "title",
                                            "media_type",
                                            "size",
                                            "sha256",
                                            "download_url",
                                        )
                                        if key in item
                                    }
                                    for item in context.platform_artifacts
                                ]
                            }
                            if context.platform_artifacts
                            else {},
                        )
                    )
                # Validate a backend terminal before any durable commit, without
                # occupying its public slot until the commit barrier succeeds.
                copy.deepcopy(machine).accept(terminal)
            except asyncio.CancelledError:
                context.cancelled.set()
                try:
                    await prepared.backend.cancel(context.run_id, context)
                finally:
                    if terminal is None:
                        terminal = context.event("run.cancelled", reason="client_disconnected")
                raise
            except Exception as exc:
                context._sequence = machine.last_sequence
                if context.cancelled.is_set():
                    terminal = context.event("run.cancelled", reason="cancelled")
                else:
                    terminal = context.event(
                        "run.error",
                        code="backend_failed",
                        message=str(exc)[:20_000] or type(exc).__name__,
                        retryable=False,
                    )
            finally:
                if terminal is None:
                    status = "cancelled"
                    terminal_json: dict[str, Any] = {"type": "run.cancelled", "reason": "cancelled"}
                else:
                    terminal_json = terminal.model_dump(mode="json", by_alias=True)
                    status = {
                        "run.finished": "succeeded",
                        "run.error": "failed",
                        "run.cancelled": "cancelled",
                        "run.interrupted": "interrupted",
                    }.get(terminal.type, "failed")
                try:
                    # Keep the foreground claim until its transcript is durable.
                    # Watchers cannot see idle while an older snapshot is pending.
                    if completed_messages:
                        await self._persist_completed_messages(prepared, completed_messages, status)
                    self.store.update(
                        owner_key,
                        context.session_id,
                        context.run_id,
                        status="committing" if status == "succeeded" else status,
                        last_sequence=context._sequence,
                        terminal=terminal_json,
                    )
                except Exception as exc:
                    # Never expose success before persistence. Reuse only the
                    # unsent terminal slot, keeping public sequences contiguous.
                    context._sequence = machine.last_sequence
                    terminal = context.event("run.error", code="run_commit_failed",
                        message=str(exc)[:2000] or type(exc).__name__, retryable=True)
                    status = "failed"
                    try:
                        self.store.update(owner_key, context.session_id, context.run_id,
                            status=status, last_sequence=terminal.sequence,
                            terminal=terminal.model_dump(mode="json", by_alias=True))
                    except Exception:
                        logging.getLogger(__name__).exception("run_failure_commit_failed run_id=%s", context.run_id)
                finally:
                    try:
                        async with self._lock:
                            self._active.pop(key, None)
                        cancel_watch_task.cancel()
                        await asyncio.gather(
                            cancel_watch_task, return_exceptions=True
                        )
                    finally:
                        # The bridge is a Run-scoped bearer capability. Revoke it even
                        # when durable status or transcript persistence fails.
                        self._release_platform_bridge(context, status)

            if terminal is not None:
                machine.accept(terminal)
                yield terminal

    async def _prepare_platform_bridge(self, prepared: PreparedRun) -> None:
        """Issue a least-privilege bridge token only for a newly claimed external Run."""

        context = prepared.context
        if context.runtime_id in {"tcode-native", "direct-echo"}:
            return
        try:
            from .platform_capabilities.assembler import PlatformCapabilityAssembler
            from .tool_bridge.session import platform_bridge_runtime

            runtime_capabilities = await prepared.backend.get_capabilities()
            snapshot = await PlatformCapabilityAssembler().assemble(
                prepared.run_input,
                context,
                runtime_capabilities,
            )
            if snapshot is None:
                return
            token, socket_path = await platform_bridge_runtime.issue(snapshot, context)
            context.platform_snapshot = snapshot.public_dict()
            context.platform_bridge_token = token
            context.platform_bridge_socket = socket_path
            context.platform_tool_state.update(
                {
                    "messages": list(prepared.run_input.messages),
                    "user_message": prepared.run_input.latest_user_text(),
                }
            )
        except Exception as exc:
            context.platform_snapshot = {
                "schema": "tcode.platform-capability-snapshot.v1",
                "status": "degraded",
                "runtimeId": context.runtime_id,
                "runId": context.run_id,
                "capabilities": [],
                "reason": str(exc)[:500] or type(exc).__name__,
            }

    @staticmethod
    def _release_platform_bridge(context: RunContext, reason: str) -> None:
        """Revoke the Run token and remove its staged read-only Skill copy."""

        if context.platform_bridge_token:
            try:
                from .tool_bridge.session import platform_bridge_runtime

                platform_bridge_runtime.revoke(context.platform_bridge_token, reason)
            except Exception:
                pass
        try:
            from .platform_capabilities.staging import cleanup_staging

            cleanup_staging(context.workdir, context.run_id)
        except Exception:
            pass

    async def cancel(self, *, owner_key: str, session_id: str, run_id: str) -> bool:
        requested = await asyncio.to_thread(
            self.store.request_cancel, owner_key, session_id, run_id
        )
        key = (owner_key, session_id, run_id)
        async with self._lock:
            active = self._active.get(key)
        if active is None:
            return requested
        active.context.cancelled.set()
        await active.backend.cancel(run_id, active.context)
        return True

    async def cancel_active(self, *, owner_key: str, session_id: str) -> bool:
        row = await asyncio.to_thread(
            self.store.active_for_session, owner_key, session_id
        )
        if row is None:
            return False
        return await self.cancel(
            owner_key=owner_key,
            session_id=session_id,
            run_id=str(row.get("run_id") or ""),
        )

    async def _heartbeat_loop(self, owner_key: str, context: RunContext) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            alive = await asyncio.to_thread(
                self.store.heartbeat,
                owner_key,
                context.session_id,
                context.run_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if not alive:
                context.cancelled.set()
                return

    async def _cancel_watch_loop(self, owner_key: str, active: _ActiveRun) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            if await asyncio.to_thread(
                self.store.cancel_requested,
                owner_key,
                active.context.session_id,
                active.context.run_id,
            ):
                active.context.cancelled.set()
                await active.backend.cancel(active.context.run_id, active.context)
                return

    async def _stream_existing(self, prepared: PreparedRun) -> AsyncIterator[RuntimeEventValue]:
        context = prepared.context
        row = prepared.existing
        session = await SessionManager.get(
            context.session_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        if session is not None:
            from ..protocol.v2 import runtime_message_from_legacy

            messages = [runtime_message_from_legacy(item) for item in list(session.messages or [])]
            yield context.event("messages.snapshot", messages=messages)
        status = str(row.get("status") or "")
        if status in {"queued", "running", "committing"}:
            yield context.event(
                "run.error",
                code="run_already_active",
                message="The same run is already active",
                retryable=True,
            )
            return
        try:
            terminal_data = json.loads(str(row.get("terminal_json") or "{}"))
        except Exception:
            terminal_data = {}
        terminal_type = str(terminal_data.get("type") or "")
        if terminal_type == "run.finished":
            yield context.event(
                "run.finished",
                outcome=terminal_data.get("outcome") or "completed",
                result=terminal_data.get("result") or {},
            )
        elif terminal_type == "run.interrupted":
            yield context.event(
                "run.interrupted",
                reason=terminal_data.get("reason") or "interaction_required",
                interaction_id=terminal_data.get("interaction_id") or "",
            )
        elif terminal_type == "run.cancelled":
            yield context.event("run.cancelled", reason=terminal_data.get("reason") or "cancelled")
        else:
            yield context.event(
                "run.error",
                code=terminal_data.get("code") or row.get("error_code") or "run_failed",
                message=terminal_data.get("message") or "The previous run failed",
                retryable=False,
            )

    async def _persist_completed_messages(
        self,
        prepared: PreparedRun,
        completed: list[dict[str, Any]],
        status: str,
    ) -> None:
        context = prepared.context
        additions: list[Message] = []
        for source in prepared.run_input.messages:
            content = source.content if isinstance(source.content, str) else json.dumps(source.content, ensure_ascii=False)
            source_payload = source.model_dump(mode="python")
            source_metadata = source_payload.get("metadata")
            metadata = (
                dict(source_metadata) if isinstance(source_metadata, dict) else {}
            )
            metadata["source_runtime_id"] = context.runtime_id
            additions.append(
                Message(
                    role=source.role,
                    content=content,
                    id=source.id,
                    metadata=metadata,
                )
            )
        for item in completed:
            message_id = str(item.get("message_id") or "")
            if not message_id:
                continue
            additions.append(
                Message(
                    role=str(item.get("role") or "assistant"),
                    content=str(item.get("content") or ""),
                    id=message_id,
                    parent_id=str(item.get("parent_message_id") or "") or None,
                    content_blocks=list(item.get("content_blocks") or []),
                    metadata={"source_runtime_id": context.runtime_id},
                )
            )

        def commit(session):
            existing = without_legacy_native_agui_user_echoes(session.messages or [], session_metadata=session.metadata)
            known = {message.id for message in existing}
            for message in additions:
                if message.id not in known:
                    existing.append(message)
                    known.add(message.id)
            if session.display_messages:
                session.display_messages = SessionManager.visible_messages(session, current_messages=existing)
            session.messages = existing
            session.status = "idle" if status == "succeeded" else status
            session.last_active_at = time.time()

        await SessionManager.mutate(
            context.session_id,
            commit,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )

    async def _repair_legacy_native_transcript(self, prepared: PreparedRun) -> None:
        context = prepared.context
        if context.runtime_id != "tcode-native":
            return
        session = await SessionManager.get(
            context.session_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        if session is None:
            return
        existing = list(session.messages or [])
        cleaned = without_legacy_native_agui_user_echoes(
            existing,
            session_metadata=getattr(session, "metadata", {}),
        )
        if len(cleaned) == len(existing):
            return
        await SessionManager.update(
            context.session_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            messages=cleaned,
        )


__all__ = [
    "AgentRunStore",
    "PreparedRun",
    "RunConflictError",
    "RunCoordinator",
    "RunUnavailableError",
    "canonical_request_digest",
]
