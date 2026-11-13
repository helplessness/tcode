"""Durable, owner-scoped completion events for asynchronous jobs.

The completion event is the hand-off boundary between a background watcher and
the foreground conversation. Watchers may update this metadata while a chat turn
is running, but they must not replace the session transcript.
"""

from __future__ import annotations

import hashlib
import asyncio
from contextlib import closing
from types import SimpleNamespace
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from ..presentation.media import normalize_content_blocks
from ..session.manager import Session, SessionManager
from ..session.notification_reads import apply_reads, record_reads
from ..session.store_sqlite import SessionSQLiteStore
from ..sqlite_runtime import connect
from ..types import Message


ASYNC_COMPLETION_EVENTS_KEY = "async_completion_events"
ASYNC_COMPLETION_EVENT_LIMIT = 100
VALID_CONTINUATION_POLICIES = {"notify", "auto", "store_only"}
_CONTINUE_ASYNC_TASK_RE = re.compile(
    r"\A[ \t]*继续异步任务[ \t]+"
    r"(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{2,199})"
    r"[。.]?[ \t]*(?:\r?\n|\Z)"
)
_SUCCESSFUL_ASYNC_STATUSES = {"completed", "succeeded", "success", "done"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def async_continuation_identifier(message: str) -> str:
    """Return the identifier from the canonical first continuation line."""

    match = _CONTINUE_ASYNC_TASK_RE.search(str(message or ""))
    return str(match.group("identifier") or "").strip() if match is not None else ""


def normalize_continuation_policy(value: Any, *, workflow_owned: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_CONTINUATION_POLICIES:
        return normalized
    return "auto" if workflow_owned else "notify"


def _event_id(
    *,
    session_id: str,
    identifier: str,
    job_id: str = "",
    process_id: str = "",
    run_id: str = "",
) -> str:
    attempt_identity = str(job_id or process_id or run_id or identifier).strip()
    raw = f"{session_id}\n{identifier}\n{attempt_identity}"
    return "async-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def session_completion_events(session: Optional[Session]) -> list[dict[str, Any]]:
    metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
    raw = metadata.get(ASYNC_COMPLETION_EVENTS_KEY)
    if not isinstance(raw, list):
        return []
    return apply_reads(session.id, [dict(item) for item in raw if isinstance(item, dict)])


async def _notification_sessions(session_id="", **owner):
    # Snapshot only notification metadata on the event loop, without awaiting
    # the task lock. No messages, checkpoint, or running-session writes.
    if SessionManager._loaded:
        sessions = list(SessionManager._sessions.values())
    else:
        def load():
            path = SessionSQLiteStore.db_path()
            if not path.exists():
                return []
            with closing(connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)) as connection:
                rows = connection.execute(
                    "SELECT id, tenant_id, user_id, workspace_id, metadata_json FROM sessions"
                )
                return [SimpleNamespace(id=row[0], tenant_id=row[1], user_id=row[2],
                                        workspace_id=row[3], metadata=json.loads(row[4]))
                        for row in rows]
        sessions = await asyncio.to_thread(load)
    return [SimpleNamespace(id=s.id, metadata=dict(s.metadata or {}))
            for s in sessions if (not session_id or s.id == session_id)
            and SessionManager._matches_owner(s, **owner)]


def unread_completion_count(session: Optional[Session]) -> int:
    return sum(
        1
        for event in session_completion_events(session)
        if not str(event.get("read_at") or "").strip()
    )


def restore_missing_completion_result_messages(
    session: Optional[Session],
    messages: Optional[list[Any]] = None,
) -> list[Any]:
    """Rehydrate rich async results removed by older continuation behavior.

    Completion events are durable and retain the normalized content blocks.
    Older builds deleted their transcript message when the user continued, so
    use the host-owned event as a read-time projection and place the recovered
    result immediately before its canonical continuation turn.
    """

    restored = list(messages if messages is not None else getattr(session, "messages", []) or [])
    known_ids = {
        str(getattr(message, "id", "") or "").strip()
        for message in restored
    }
    for event in session_completion_events(session):
        identifier = str(event.get("identifier") or "").strip()
        message_id = f"msg-async-continuation-{identifier}"
        if message_id in ((getattr(session, 'metadata', None) or {}).get('edited_out_message_ids') or []):
            continue
        content_blocks = normalize_content_blocks(event.get("content_blocks"))
        content = str(event.get("message") or "").strip()
        if not identifier or message_id in known_ids or not content_blocks or not content:
            continue
        recovered = Message(
            role="assistant",
            content=content,
            id=message_id,
            presentation="async_completion",
            content_blocks=content_blocks,
            metadata={"source_runtime_id": "tcode-native", "recovered": True},
        )
        insert_at = len(restored)
        for index, message in enumerate(restored):
            if str(getattr(message, "role", "") or "").strip().lower() != "user":
                continue
            if async_continuation_identifier(
                str(getattr(message, "content", "") or "")
            ) == identifier:
                insert_at = index
                break
        restored.insert(insert_at, recovered)
        known_ids.add(message_id)
    return restored


def trusted_completion_receipt(
    message: str,
    session: Optional[Session],
) -> dict[str, Any]:
    """Resolve a canonical continuation prompt against a host-owned event.

    The result summary embedded in the user-visible prompt is intentionally not
    trusted by itself.  A receipt is returned only while the matching durable
    completion event is still available and both the watcher and Job Protocol
    result report successful validation.
    """

    identifier = async_continuation_identifier(message)
    if not identifier:
        return {}
    event = next(
        (
            item
            for item in reversed(session_completion_events(session))
            if str(item.get("identifier") or "").strip() == identifier
        ),
        None,
    )
    if not isinstance(event, dict):
        return {}
    if str(event.get("kind") or "").strip() != "async_completion":
        return {}
    if str(event.get("continuation_status") or "").strip().lower() == "continued":
        return {}
    if str(event.get("status") or "").strip().lower() not in _SUCCESSFUL_ASYNC_STATUSES:
        return {}
    if not bool(event.get("can_continue")) or str(event.get("error_summary") or "").strip():
        return {}

    receipt = event.get("result_receipt")
    if isinstance(receipt, dict) and receipt:
        summary = dict(receipt)
        rejected_count = summary.get("rejected_artifact_count")
        if not isinstance(rejected_count, int) or rejected_count != 0:
            return {}
        if bool(summary.get("target_is_error")):
            return {}
    else:
        # Backward compatibility for completion events created before compact
        # receipts were introduced.  Complete legacy JSON remains verifiable;
        # a truncated display summary deliberately fails closed.
        try:
            summary = json.loads(str(event.get("result_summary") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(summary, dict):
            return {}
    if str(summary.get("status") or "").strip().lower() not in _SUCCESSFUL_ASYNC_STATUSES:
        return {}
    if summary.get("validated") is not True:
        return {}
    rejected = summary.get("rejected_artifacts")
    if isinstance(rejected, list) and rejected:
        return {}
    target_result = summary.get("target_result")
    if isinstance(target_result, dict) and bool(target_result.get("isError")):
        return {}

    bundle_id = str(summary.get("bundle_id") or "").strip()
    bundle = summary.get("output_bundle")
    if not isinstance(bundle, dict) and isinstance(target_result, dict):
        structured = target_result.get("structuredContent")
        if isinstance(structured, dict):
            bundle = structured.get("output_bundle")
    if not bundle_id and isinstance(bundle, dict):
        bundle_id = str(bundle.get("bundle_id") or "").strip()
    return {
        "source": "session_async_completion_store",
        "event_id": str(event.get("id") or "").strip(),
        "identifier": identifier,
        "job_id": str(event.get("job_id") or summary.get("job_id") or "").strip(),
        "run_id": str(event.get("run_id") or "").strip(),
        "status": "completed",
        "validated": True,
        "bundle_id": bundle_id,
    }


def _upsert_event_list(
    events: list[dict[str, Any]],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(incoming.get("id") or "").strip()
    existing_index = next(
        (
            index
            for index, item in enumerate(events)
            if str(item.get("id") or "").strip() == event_id
        ),
        -1,
    )
    now = _utc_now_iso()
    if existing_index >= 0:
        existing = dict(events[existing_index])
        # Enrichment from a later collect_result pass must not make an already
        # viewed notification unread or forget a user continuation request.
        preserved = {
            key: existing.get(key)
            for key in (
                "created_at",
                "read_at",
                "continuation_status",
                "continuation_attempt_id",
                "continuation_requested_at",
                "continuation_completed_at",
                "continuation_error",
                "task_context",
            )
            if existing.get(key) not in (None, "")
        }
        merged = {**existing, **incoming, **preserved, "updated_at": now}
        events[existing_index] = merged
        return merged

    created = {
        **incoming,
        "created_at": str(incoming.get("created_at") or now),
        "updated_at": now,
        "read_at": str(incoming.get("read_at") or ""),
        "continuation_status": str(
            incoming.get("continuation_status")
            or ("available" if incoming.get("can_continue") else "not_available")
        ),
    }
    events.append(created)
    if len(events) > ASYNC_COMPLETION_EVENT_LIMIT:
        del events[: len(events) - ASYNC_COMPLETION_EVENT_LIMIT]
    return created


async def record_completion_event(
    *,
    session_id: str,
    identifier: str,
    continuation: dict[str, Any],
    job_id: str = "",
    process_id: str = "",
    run_id: str = "",
    origin_turn_id: str = "",
    origin_request_id: str = "",
    continuation_policy: str = "notify",
    task_context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    normalized_session_id = str(session_id or "").strip()
    normalized_identifier = str(identifier or "").strip()
    if not normalized_session_id or not normalized_identifier:
        return None

    event_id = _event_id(
        session_id=normalized_session_id,
        identifier=normalized_identifier,
        job_id=job_id,
        process_id=process_id,
        run_id=run_id,
    )
    normalized_policy = normalize_continuation_policy(continuation_policy)
    incoming = {
        "id": event_id,
        "kind": "async_completion",
        "session_id": normalized_session_id,
        "origin_turn_id": str(origin_turn_id or "").strip(),
        "task_context": dict(task_context or {}),
        "origin_request_id": str(origin_request_id or "").strip(),
        "identifier": normalized_identifier,
        "job_id": str(job_id or "").strip(),
        "process_id": str(process_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        "target_server": str(continuation.get("target_server") or "").strip(),
        "target_tool": str(continuation.get("target_tool") or "").strip(),
        "status": str(continuation.get("status") or "completed").strip(),
        "issue_state": str(continuation.get("issue_state") or "").strip(),
        "message": str(continuation.get("message") or "").strip(),
        "content_blocks": normalize_content_blocks(continuation.get("content_blocks")),
        "presentation_summary": str(
            continuation.get("presentation_summary") or ""
        ).strip()[:4000],
        "result_summary": str(continuation.get("result_summary") or "").strip(),
        "result_receipt": (
            dict(continuation.get("result_receipt") or {})
            if isinstance(continuation.get("result_receipt"), dict)
            else {}
        ),
        "error_summary": str(continuation.get("error_summary") or "").strip(),
        "output_path": str(continuation.get("output_path") or "").strip(),
        "output_abs_path": str(continuation.get("output_abs_path") or "").strip(),
        "artifacts": [
            str(item)
            for item in (continuation.get("artifacts") or [])
            if str(item or "").strip()
        ][:100],
        "can_continue": bool(continuation.get("can_continue")),
        "continuation_policy": normalized_policy,
        "delivery_status": str(continuation.get("delivery_status") or "pending"),
        "read_at": _utc_now_iso() if normalized_policy == "store_only" else "",
    }

    def _mutate(session: Session) -> Optional[dict[str, Any]]:
        from ..server.session_lifecycle import _SESSION_DELETE_PENDING
        if normalized_session_id in _SESSION_DELETE_PENDING:
            return None
        metadata = dict(getattr(session, "metadata", {}) or {})
        events = [
            dict(item)
            for item in (metadata.get(ASYNC_COMPLETION_EVENTS_KEY) or [])
            if isinstance(item, dict)
        ]
        stored = _upsert_event_list(events, incoming)
        metadata[ASYNC_COMPLETION_EVENTS_KEY] = events
        session.metadata = metadata
        session.last_active_at = datetime.now().timestamp()
        return dict(stored)

    return await SessionManager.mutate(normalized_session_id, _mutate)


async def mark_completion_read(
    *,
    event_id: str = "",
    session_id: str = "",
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> int:
    target_event_id = str(event_id or "").strip()
    target_session_id = str(session_id or "").strip()
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
    sessions = await _notification_sessions(target_session_id, **owner)
    now = _utc_now_iso()
    receipts = [(session.id, str(event['id']), now)
                for session in sessions for event in session_completion_events(session)
                if event.get('id') and not event.get('read_at')
                and (not target_event_id or event['id'] == target_event_id)]
    return await asyncio.to_thread(record_reads, receipts)


async def mark_completion_continued(
    *,
    session_id: str,
    identifier: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    attempt_id: str = "",
) -> dict[str, Any]:
    """Persist that an async result has started its continuation turn.

    The completion message is also the durable result presentation for rich
    Job Protocol responses.  Keep it in the transcript so tables, metrics and
    artifact links remain available; the continued event state and canonical
    user message make the action button idempotent.
    """

    target_session_id = str(session_id or "").strip()
    target_identifier = str(identifier or "").strip()
    if not target_session_id or not target_identifier:
        return {
            "matched": False,
            "events_changed": 0,
            "messages_removed": 0,
        }

    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
    candidate = await SessionManager.get(target_session_id, **owner)
    if candidate is None:
        return {
            "matched": False,
            "events_changed": 0,
            "messages_removed": 0,
        }

    def _mutate(session: Session) -> dict[str, Any]:
        metadata = dict(getattr(session, "metadata", {}) or {})
        events = [
            dict(item)
            for item in (metadata.get(ASYNC_COMPLETION_EVENTS_KEY) or [])
            if isinstance(item, dict)
        ]
        now = _utc_now_iso()
        events_changed = 0
        matched = False
        for event in events:
            if str(event.get("identifier") or "").strip() != target_identifier:
                continue
            matched = True
            if str(event.get("continuation_status") or "").strip() != "continued":
                events_changed += 1
            event["continuation_status"] = "continued"
            if attempt_id:
                event["continuation_attempt_id"] = attempt_id
            event["continuation_requested_at"] = str(
                event.get("continuation_requested_at") or now
            )
            event["continuation_completed_at"] = now
            event["read_at"] = str(event.get("read_at") or now)
            event["updated_at"] = now

        pending = metadata.get("pending_continuations")
        if isinstance(pending, list):
            next_pending = []
            for item in pending:
                if not isinstance(item, dict):
                    next_pending.append(item)
                    continue
                next_item = dict(item)
                item_identifier = str(
                    next_item.get("identifier") or next_item.get("id") or ""
                ).strip()
                if item_identifier == target_identifier:
                    matched = True
                    next_item["continuation_status"] = "continued"
                    if attempt_id:
                        next_item["continuation_attempt_id"] = attempt_id
                    next_item["consumed_at"] = now
                next_pending.append(next_item)
            metadata["pending_continuations"] = next_pending

        if events_changed:
            metadata[ASYNC_COMPLETION_EVENTS_KEY] = events
        elif matched and events:
            # Keep timestamps/status updates even when it was already continued.
            metadata[ASYNC_COMPLETION_EVENTS_KEY] = events
        session.metadata = metadata
        if matched:
            session.last_active_at = datetime.now().timestamp()
        return {
            "matched": matched,
            "events_changed": events_changed,
            "messages_removed": 0,
        }

    result = await SessionManager.mutate(target_session_id, _mutate, **owner)
    return dict(
        result
        or {
            "matched": False,
            "events_changed": 0,
            "messages_removed": 0,
        }
    )


async def rollback_completion_continued(*, session_id: str, identifier: str, attempt_id: str, **owner) -> None:
    """Undo only this failed commit attempt, never a different continuation."""
    def rollback(session):
        for collection in (ASYNC_COMPLETION_EVENTS_KEY, "pending_continuations"):
            for event in (session.metadata or {}).get(collection, []):
                if isinstance(event, dict) and event.get("identifier") == identifier and event.get("continuation_attempt_id") == attempt_id:
                    for key in ("continuation_status", "continuation_attempt_id", "continuation_requested_at", "continuation_completed_at", "consumed_at"):
                        event.pop(key, None)
    await SessionManager.mutate(session_id, rollback, **owner)


async def list_completion_events(
    *,
    session_id: str = "",
    unread_only: bool = False,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
    target_session_id = str(session_id or "").strip()
    sessions = await _notification_sessions(target_session_id, **owner)
    events: list[dict[str, Any]] = []
    for session in sessions:
        if not target_session_id and SessionManager.is_archived_session(session):
            continue
        for event in session_completion_events(session):
            if unread_only and str(event.get("read_at") or "").strip():
                continue
            # The source capsule is server-owned restoration data, not a
            # notification payload to retransmit on every UI poll.
            events.append({key: value for key, value in event.items() if key != "task_context"})
    return sorted(
        events,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
