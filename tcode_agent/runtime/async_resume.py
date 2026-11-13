"""Owner-scoped, result-only restoration of a completed asynchronous task."""

from __future__ import annotations

from contextlib import closing
from contextvars import ContextVar
import json
import os
import socket
import uuid
from typing import Any

from ..session.manager import SessionManager
from ..session.store_sqlite import SessionSQLiteStore
from ..sqlite_runtime import connect
from .async_completion import session_completion_events, trusted_completion_receipt


active_resume: ContextVar[dict[str, Any] | None] = ContextVar("async_resume", default=None)
active_origin: ContextVar[dict[str, Any] | None] = ContextVar("async_task_origin", default=None)


async def capture_task_context(context) -> dict[str, Any]:
    """Freeze the human request before subsequent chat/compaction can replace it."""
    extra = dict(getattr(context, "extra", {}) or {})
    frozen = (active_resume.get() or {}).get("task_context") or active_origin.get()
    if frozen:
        return {**frozen, "model_context": dict(extra.get("model_context_snapshot") or {}), "resource_context": {
            key: value for key, value in dict(extra.get("resource_context") or {}).items()
            if key in {"uploaded_files", "external_mounts"}
        }}
    session_id = str(extra.get("parent_session_id") or context.session_id)
    owner = {key: extra[key] for key in ("tenant_id", "user_id", "workspace_id") if extra.get(key)}
    session = await SessionManager.get(session_id, **owner)
    messages = list(getattr(context, "messages", []) or [])
    source_session_id = str(context.session_id) if messages else session_id
    if not messages and session:
        messages = SessionManager.visible_messages(session)
    original = next((message for message in reversed(messages)
                     if str(getattr(message, "role", "")) == "user"), None)
    if original is None:
        return {}
    return {
        "session_id": source_session_id,
        "message_id": str(original.id),
        "request": str(original.content),
        "model_context": dict(extra.get("model_context_snapshot") or {}),
        "resource_context": {key: value for key, value in
            dict((getattr(session, "metadata", {}) or {}).get("resource_context") or {}).items()
            if key in {"uploaded_files", "external_mounts"}},
    }


def _connect():
    # Claims have their own transactional authority, not a cached Session JSON
    # field which a concurrent foreground commit could overwrite.
    path = SessionSQLiteStore.db_path().with_name("async-resumes.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(str(path), timeout=5)
    connection.execute("""CREATE TABLE IF NOT EXISTS claims (
        owner TEXT NOT NULL, session_id TEXT NOT NULL, identifier TEXT NOT NULL,
        claim_id TEXT NOT NULL, state TEXT NOT NULL, process TEXT NOT NULL,
        PRIMARY KEY(owner, session_id, identifier))""")
    return connection


def claim_resume(session, identifier: str, request_id: str) -> dict[str, Any]:
    """Validate host evidence and atomically claim one continuation attempt."""
    from ..server.session_lifecycle import _SESSION_DELETE_PENDING
    if session is None or session.id in _SESSION_DELETE_PENDING:
        raise ValueError("async_resume_session_unavailable")
    event = next((item for item in reversed(session_completion_events(session))
                  if item.get("identifier") == identifier), None)
    if not event or not trusted_completion_receipt(f"继续异步任务 {identifier}。", session):
        raise ValueError("async_resume_result_unavailable_or_already_continued")
    receipt_job_id = str((event.get("result_receipt") or {}).get("job_id") or "")
    if receipt_job_id and event.get("job_id") and receipt_job_id != event["job_id"]:
        raise ValueError("async_resume_result_identity_mismatch")
    task_context = dict(event.get("task_context") or {})
    if not task_context.get("request"):
        origin = str(event.get("origin_turn_id") or "")
        original = next((message for message in SessionManager.visible_messages(session)
                         if message.id == origin and str(message.role) == "user"), None)
        if original is None:
            raise ValueError("async_resume_original_context_unavailable")
        task_context = {"session_id": session.id, "message_id": original.id, "request": original.content}
    if not event.get("target_server") or not event.get("target_tool") or not request_id:
        raise ValueError("async_resume_identity_unavailable")
    owner = json.dumps([getattr(session, key, "") for key in ("tenant_id", "user_id", "workspace_id")])
    # Receipt enrichment may change an event ID, never the accepted job ID.
    key = (owner, session.id, identifier)
    claim_id = uuid.uuid4().hex
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT state, process FROM claims WHERE owner=? AND session_id=? AND identifier=?", key).fetchone()
        orphaned = False
        if existing and existing[0] == "running":
            host, _, pid = existing[1].rpartition(":")
            if host == socket.gethostname() and pid.isdigit():
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    orphaned = True
                except PermissionError:
                    pass
        if existing and existing[0] != "failed" and not orphaned:
            raise ValueError("async_resume_already_claimed")
        connection.execute("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, ?, 'running', ?)",
                           (*key, claim_id, f"{socket.gethostname()}:{os.getpid()}"))
    return {
        "key": key, "request_id": request_id, "claim_id": claim_id, "identifier": identifier,
        "target_server": str(event["target_server"]), "target_tool": str(event["target_tool"]),
        "task_context": task_context,
        "result": {name: event.get(name) for name in (
            "id", "identifier", "job_id", "run_id", "status", "result_summary",
            "result_receipt", "content_blocks", "artifacts", "output_path", "output_abs_path",
        )},
    }


def finish_resume(scope: dict[str, Any], *, completed: bool) -> None:
    with closing(_connect()) as connection, connection:
        connection.execute("""UPDATE claims SET state=? WHERE owner=? AND session_id=?
            AND identifier=? AND claim_id=? AND state='running'""",
            ("completed" if completed else "failed", *scope["key"], scope["claim_id"]))


def assert_submission_allowed(server: str, tool: str) -> None:
    scope = active_resume.get()
    replay = scope and str(server).lower() == scope["target_server"].lower() and str(tool) == scope["target_tool"]
    raw_submission = scope and str(server) == "job-protocol-mcp" and str(tool) == "submit_job"
    if replay or raw_submission:
        # A continuation may analyze its existing result or perform other steps,
        # but cannot restart the originating compute target, even via a child.
        # A new human turn has no restore scope and keeps normal submission rules.
        raise PermissionError("async_resume_compute_replay_blocked: resume uses the existing result; submit a new calculation in a new request")


RESUME_CONTEXT_SCHEMA = "tcode.async-resume-context.v1"


def resume_context_json(scope: dict[str, Any] | None = None) -> str:
    scope = scope or active_resume.get()
    if not scope:
        return ""
    return json.dumps({"schema": RESUME_CONTEXT_SCHEMA,
                       "task": scope["task_context"], "result": scope["result"]}, ensure_ascii=False)


async def commit_resume(scope: dict[str, Any], *, session_id: str, **owner) -> None:
    """Acknowledge consumption only after its durable event was updated."""
    from .async_completion import mark_completion_continued
    result = await mark_completion_continued(session_id=session_id,
        identifier=scope["identifier"], attempt_id=scope["claim_id"], **owner)
    if not result.get("matched"):
        raise RuntimeError("async_resume_completion_event_missing")
