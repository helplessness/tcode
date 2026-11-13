"""Unified runtime lifecycle records for tools, jobs, workflows, and reviews."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..types import ToolResult


@lru_cache(maxsize=16)
def _read_runtime_index(path: Path, signature: tuple[int, ...]) -> dict[str, dict[str, Any]]:
    # The signature invalidates external writes, replacements and recreated files.
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


TASK_RUNTIME_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked"}
TASK_RUNTIME_INSIGHT_STATUSES = (
    "waiting_user",
    "waiting_review",
    "waiting_external",
    "blocked",
    "running",
)
TASK_RUNTIME_KNOWN_STATUSES = (
    "queued",
    "running",
    "waiting_user",
    "waiting_review",
    "waiting_external",
    "collecting",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)
TASK_RUNTIME_LINKED_OPEN_STATUSES = {
    "queued",
    "running",
    "waiting_user",
    "waiting_review",
    "waiting_external",
    "collecting",
}


def task_runtime_id(kind: str, primary_id: str, *, session_id: str = "") -> str:
    normalized_kind = str(kind or "runtime").strip().lower() or "runtime"
    normalized_primary = str(primary_id or "").strip() or f"generated-{uuid.uuid4()}"
    normalized_session = str(session_id or "").strip()
    if normalized_session:
        return f"{normalized_kind}:{normalized_session}:{normalized_primary}"
    return f"{normalized_kind}:{normalized_primary}"


def task_runtime_id_for_tool_call(session_id: str, call_id: str) -> str:
    return task_runtime_id("tool", call_id, session_id=session_id)


def task_runtime_id_for_external_job(identifier: str) -> str:
    return task_runtime_id("mcp_job", identifier)


def task_runtime_id_for_human_review(identifier: str) -> str:
    return task_runtime_id("human_review", identifier)


def task_runtime_id_for_workflow(run_id: str) -> str:
    return task_runtime_id("workflow", run_id)


def compact_runtime_text(
    value: Any,
    *,
    result_budget_tokens: int = 0,
    default_chars: int = 2000,
) -> str:
    text = str(value or "")
    try:
        budget_chars = int(result_budget_tokens or 0) * 4
    except Exception:
        budget_chars = 0
    limit = max(240, min(budget_chars or default_chars, 48_000))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + " ...[runtime-truncated]"


def task_runtime_status_for_tool_result(result: ToolResult) -> str:
    metadata = dict(getattr(result, "metadata", {}) or {})
    tool_error = str(getattr(result, "error", "") or "").strip()
    workflow_status = str(metadata.get("status", "") or "").strip().lower()

    if bool(metadata.get("awaiting_input")) or bool(metadata.get("waiting_for_user")):
        return "waiting_user"
    if workflow_status in {
        "awaiting_input",
        "waiting_for_user",
        "needs_parent_input",
        "needs_user_input",
    }:
        return "waiting_user"
    if bool(metadata.get("human_review_required")) or (str(metadata.get("tracker", "")) == "human_review" and not (metadata.get('auto_approved') is True and workflow_status == 'approved')):
        return "waiting_review"
    if workflow_status == "review_pending":
        return "waiting_review"
    # Queries of an existing async job retain accepted_async as provenance.
    # Its explicit terminal result must not remain an open execution forever.
    if workflow_status in {"completed", "failed", "cancelled", "canceled", "terminated"}:
        if workflow_status in {"cancelled", "canceled", "terminated"}:
            return "cancelled"
        return "failed" if tool_error or workflow_status == "failed" else "completed"
    if bool(metadata.get("accepted_async")) or bool(metadata.get("async_stage_refs")):
        return "waiting_external"
    if workflow_status in {"waiting_external_job", "collecting"}:
        return "waiting_external"
    if workflow_status in {"blocked"}:
        return "blocked"
    if workflow_status in {"cancelled", "canceled", "terminated"}:
        return "cancelled"
    if tool_error:
        if bool(metadata.get("sandbox_policy_blocked")) or bool(metadata.get("mcp_sandbox_blocked")):
            return "blocked"
        return "failed"
    return "completed"


class TaskRuntimeStore:
    """Filesystem-backed runtime lifecycle index plus append-only event log."""

    _LOCK = threading.RLock()

    @classmethod
    def _base_dir(cls, workdir: Optional[str] = None) -> Path:
        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        path = Path(base) / ".tcode" / "runtime" / "task_runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _index_path(cls, workdir: Optional[str] = None) -> Path:
        return cls._base_dir(workdir) / "runtimes.json"

    @classmethod
    def _events_path(cls, workdir: Optional[str] = None) -> Path:
        return cls._base_dir(workdir) / "events.jsonl"

    @staticmethod
    def _safe_key(value: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
        return text[:240] or f"runtime:{uuid.uuid4()}"

    @classmethod
    def _jsonable(cls, value: Any, *, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return compact_runtime_text(value, default_chars=4000)
        if depth >= 5:
            return compact_runtime_text(repr(value), default_chars=1000)
        if isinstance(value, dict):
            payload: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 80:
                    payload["__truncated_keys__"] = len(value) - index
                    break
                payload[compact_runtime_text(key, default_chars=160)] = cls._jsonable(
                    item,
                    depth=depth + 1,
                )
            return payload
        if isinstance(value, (list, tuple, set)):
            values = list(value)
            payload = [cls._jsonable(item, depth=depth + 1) for item in values[:80]]
            if len(values) > 80:
                payload.append(f"...[{len(values) - 80} more items]")
            return payload
        return compact_runtime_text(repr(value), default_chars=1000)

    @classmethod
    def _read_index(cls, workdir: Optional[str] = None) -> dict[str, dict[str, Any]]:
        path = cls._index_path(workdir).resolve()
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size,
                         stat.st_mtime_ns, stat.st_ctime_ns)
            return _read_runtime_index(path, signature)
        except Exception:
            return {}

    @classmethod
    def _load_index(cls, workdir: Optional[str] = None) -> dict[str, dict[str, Any]]:
        # Writers own their snapshot; mutations must never change the read cache.
        return deepcopy(cls._read_index(workdir))

    @classmethod
    def _save_index(
        cls,
        index: dict[str, dict[str, Any]],
        workdir: Optional[str] = None,
    ) -> None:
        path = cls._index_path(workdir)
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def _append_event(
        cls,
        event: dict[str, Any],
        workdir: Optional[str] = None,
    ) -> None:
        path = cls._events_path(workdir)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    @classmethod
    def record_event(
        cls,
        *,
        runtime_id: str,
        runtime_kind: str,
        status: str,
        phase: str = "",
        source: str = "",
        session_id: str = "",
        title: str = "",
        parent_runtime_id: str = "",
        external_id: str = "",
        tool_name: str = "",
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        key = cls._safe_key(runtime_id)
        normalized_status = str(status or "running").strip().lower() or "running"
        normalized_phase = str(phase or normalized_status).strip().lower() or normalized_status
        safe_metadata = cls._jsonable(metadata or {})

        with cls._LOCK:
            index = cls._load_index(workdir)
            record = dict(index.get(key) or {})
            previous_status = str(record.get("status", "") or "")
            if not record:
                record = {
                    "runtime_id": key,
                    "runtime_kind": str(runtime_kind or "").strip(),
                    "created_at": now,
                    "event_count": 0,
                }
            record.update(
                {
                    "runtime_kind": str(runtime_kind or record.get("runtime_kind") or "").strip(),
                    "status": normalized_status,
                    "phase": normalized_phase,
                    "source": str(source or record.get("source") or "").strip(),
                    "updated_at": now,
                }
            )
            for field, value in (
                ("session_id", session_id),
                ("title", title),
                ("parent_runtime_id", parent_runtime_id),
                ("external_id", external_id),
                ("tool_name", tool_name),
            ):
                text = str(value or "").strip()
                if text:
                    record[field] = text
            if normalized_status in TASK_RUNTIME_TERMINAL_STATUSES:
                record["completed_at"] = now
            record["metadata"] = {
                **dict(record.get("metadata") or {}),
                **(safe_metadata if isinstance(safe_metadata, dict) else {}),
            }
            record["event_count"] = int(record.get("event_count", 0) or 0) + 1

            event = {
                "event_id": f"rt-{uuid.uuid4()}",
                "runtime_id": key,
                "runtime_kind": record.get("runtime_kind", ""),
                "session_id": record.get("session_id", ""),
                "title": record.get("title", ""),
                "external_id": record.get("external_id", ""),
                "tool_name": record.get("tool_name", ""),
                "status": normalized_status,
                "phase": normalized_phase,
                "source": record.get("source", ""),
                "previous_status": previous_status,
                "created_at": now,
                "metadata": safe_metadata,
            }
            record["last_event_id"] = event["event_id"]
            index[key] = record
            cls._save_index(index, workdir)
            cls._append_event(event, workdir)
            return record

    @classmethod
    def get(cls, runtime_id: str, *, workdir: Optional[str] = None) -> Optional[dict[str, Any]]:
        return deepcopy(cls._read_index(workdir).get(cls._safe_key(runtime_id)))

    @classmethod
    def list(
        cls,
        *,
        session_id: str = "",
        runtime_kind: str = "",
        workdir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        items = list(cls._read_index(workdir).values())
        if session_id:
            items = [
                item
                for item in items
                if str(item.get("session_id", "") or "") == str(session_id)
            ]
        if runtime_kind:
            items = [
                item
                for item in items
                if str(item.get("runtime_kind", "") or "") == str(runtime_kind)
            ]
        items.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
        return deepcopy(items)

    @staticmethod
    def _record_reference_values(record: dict[str, Any]) -> set[str]:
        values: set[str] = set()

        def _add(value: Any) -> None:
            text = str(value or "").strip()
            if text:
                values.add(text)

        _add(record.get("external_id"))
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            return values

        for key in ("id", "identifier", "job_id", "process_id", "review_id"):
            _add(metadata.get(key))
        for nested_key in ("external_refs", "result_metadata"):
            nested = metadata.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in (
                "id",
                "identifier",
                "job_id",
                "process_id",
                "review_id",
                "sandbox_job_id",
            ):
                _add(nested.get(key))
        return values

    @classmethod
    def transition_linked_open(
        cls,
        references: Any,
        *,
        status: str,
        phase: str = "",
        source: str = "runtime_reconcile",
        session_id: str = "",
        workdir: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Transition open runtime rows linked to the same external task/review."""

        raw_references = (
            references
            if isinstance(references, (list, tuple, set))
            else [references]
        )
        wanted = {
            str(value or "").strip()
            for value in raw_references
            if str(value or "").strip()
        }
        if not wanted:
            return []

        normalized_status = str(status or "completed").strip().lower() or "completed"
        normalized_session_id = str(session_id or "").strip()
        candidates: list[dict[str, Any]] = []
        with cls._LOCK:
            for record in cls._load_index(workdir).values():
                current_status = str(record.get("status", "") or "").strip().lower()
                if current_status not in TASK_RUNTIME_LINKED_OPEN_STATUSES:
                    continue
                if current_status == normalized_status:
                    continue
                record_session_id = str(record.get("session_id", "") or "").strip()
                if normalized_session_id and record_session_id != normalized_session_id:
                    continue
                if wanted.isdisjoint(cls._record_reference_values(record)):
                    continue
                candidates.append(dict(record))

        transitioned: list[dict[str, Any]] = []
        for record in candidates:
            transitioned.append(
                cls.record_event(
                    runtime_id=str(record.get("runtime_id", "") or ""),
                    runtime_kind=str(record.get("runtime_kind", "") or "runtime"),
                    status=normalized_status,
                    phase=phase or normalized_status,
                    source=source,
                    session_id=str(record.get("session_id", "") or ""),
                    title=str(record.get("title", "") or ""),
                    parent_runtime_id=str(record.get("parent_runtime_id", "") or ""),
                    external_id=str(record.get("external_id", "") or ""),
                    tool_name=str(record.get("tool_name", "") or ""),
                    metadata={
                        "linked_transition_references": sorted(wanted),
                        **dict(metadata or {}),
                    },
                    workdir=workdir,
                )
            )
        return transitioned

    @classmethod
    def events(
        cls,
        *,
        runtime_id: str = "",
        session_id: str = "",
        workdir: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        path = cls._events_path(workdir)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        wanted = cls._safe_key(runtime_id) if runtime_id else ""
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                if wanted and str(event.get("runtime_id", "") or "") != wanted:
                    continue
                if session_id and str(event.get("session_id", "") or "") != str(session_id):
                    continue
                events.append(event)
        if limit is not None:
            safe_limit = max(0, int(limit))
            return events[-safe_limit:] if safe_limit else []
        return events

    @staticmethod
    def _timestamp_seconds(value: Any) -> float:
        """Normalize persisted second/millisecond timestamps for comparisons."""

        try:
            timestamp = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return max(0.0, timestamp)

    @classmethod
    def _session_outcome_context(
        cls,
        session_metadata: Optional[dict[str, Any]],
    ) -> tuple[str, float]:
        metadata = session_metadata if isinstance(session_metadata, dict) else {}
        last_turn = metadata.get("last_turn")
        delivery = dict(metadata.get("delivery_acceptance") or {})
        if delivery.get("state") == "accepted" and (
            not isinstance(last_turn, dict) or not last_turn.get("final_message_id")
            or last_turn.get("final_message_id") == delivery.get("message_id")
        ):
            return "completed", cls._timestamp_seconds(delivery.get("decided_at"))
        if not isinstance(last_turn, dict):
            return "", 0.0

        extra = last_turn.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        outcome_status = str(
            extra.get("outcome_status") or last_turn.get("state") or ""
        ).strip().lower()
        outcome_at = cls._timestamp_seconds(last_turn.get("updated_at"))
        if outcome_status == "blocked":
            turn_id = str(last_turn.get("turn_id") or "").strip()
            for event in metadata.get("async_completion_events") or []:
                if not isinstance(event, dict) or not turn_id or event.get("origin_turn_id") != turn_id:
                    continue
                if (
                    str(event.get("status") or "").lower() != "completed"
                    or not event.get("output_path")
                ):
                    continue
                try:
                    completed_at = datetime.fromisoformat(str(event.get("updated_at") or "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if completed_at > outcome_at:
                    outcome_status, outcome_at = "completed", completed_at
        return outcome_status, outcome_at

    @classmethod
    def summary_for_session(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
        item_limit: int = 50,
        event_limit: int = 20,
        session_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Return the canonical runtime-insight payload for one session.

        The item feed intentionally focuses on actionable lifecycle states. Terminal
        records remain represented in ``total``/``counts`` and in the event feed.
        """

        normalized_session_id = str(session_id or "").strip()
        items = (
            cls.list(session_id=normalized_session_id, workdir=workdir)
            if normalized_session_id
            else []
        )
        counts = {status: 0 for status in TASK_RUNTIME_KNOWN_STATUSES}
        for item in items:
            status = str(item.get("status", "") or "").strip().lower()
            if not status:
                continue
            counts[status] = int(counts.get(status, 0) or 0) + 1

        session_outcome_status, session_outcome_updated_at = (
            cls._session_outcome_context(session_metadata)
        )
        resolved_runtime_ids: set[str] = set()
        if session_outcome_status == "completed" and session_outcome_updated_at > 0:
            for item in items:
                status = str(item.get("status", "") or "").strip().lower()
                item_updated_at = cls._timestamp_seconds(item.get("updated_at"))
                if (
                    status == "blocked"
                    and item_updated_at > 0
                    and item_updated_at <= session_outcome_updated_at
                ):
                    resolved_runtime_ids.add(str(item.get("runtime_id", "") or ""))
        if session_outcome_status in {
            "completed",
            "failed",
            "interrupted",
            "cancelled",
        } and session_outcome_updated_at > 0:
            for item in items:
                status = str(item.get("status", "") or "").strip().lower()
                runtime_kind = str(
                    item.get("runtime_kind", "") or ""
                ).strip().lower()
                item_updated_at = cls._timestamp_seconds(item.get("updated_at"))
                if (
                    runtime_kind == "agent_tool"
                    and status in {"queued", "running"}
                    and item_updated_at > 0
                    and item_updated_at <= session_outcome_updated_at
                ):
                    # 已结束回合不可能继续持有同步 Agent Tool；异步任务会使用
                    # waiting_external 等独立状态，因此这里仅收口旧版遗留的
                    # queued/running 展示记录，不隐藏真实异步任务。
                    resolved_runtime_ids.add(str(item.get("runtime_id", "") or ""))

        insight_items = [
            item
            for item in items
            if str(item.get("status", "") or "").strip().lower()
            in TASK_RUNTIME_INSIGHT_STATUSES
            and str(item.get("runtime_id", "") or "") not in resolved_runtime_ids
        ]
        resolved_items = [
            item
            for item in items
            if str(item.get("runtime_id", "") or "") in resolved_runtime_ids
        ]
        safe_item_limit = max(0, int(item_limit or 0))
        safe_event_limit = max(0, int(event_limit or 0))
        recent_events = (
            cls.events(
                session_id=normalized_session_id,
                workdir=workdir,
            )
            if safe_event_limit
            else []
        )
        recent_events.sort(
            key=lambda event: float(event.get("created_at", 0.0) or 0.0),
            reverse=True,
        )

        # Older event rows predate the display fields. Hydrate them from the current
        # index record so the UI can render one stable event contract.
        records_by_id = {
            str(item.get("runtime_id", "") or ""): item
            for item in items
        }
        hydrated_events: list[dict[str, Any]] = []
        for event in recent_events[:safe_event_limit]:
            record = records_by_id.get(str(event.get("runtime_id", "") or ""), {})
            hydrated_events.append(
                {
                    "title": record.get("title", ""),
                    "external_id": record.get("external_id", ""),
                    "tool_name": record.get("tool_name", ""),
                    **event,
                }
            )

        active_counts = {status: 0 for status in TASK_RUNTIME_INSIGHT_STATUSES}
        for item in insight_items:
            status = str(item.get("status", "") or "").strip().lower()
            active_counts[status] = int(active_counts.get(status, 0) or 0) + 1
        resolved_counts = {"blocked": len(resolved_items)}
        return {
            "session_id": normalized_session_id,
            "session_outcome_status": session_outcome_status,
            "session_outcome_updated_at": session_outcome_updated_at,
            "total": len(items),
            "active_total": sum(active_counts.values()),
            "counts": counts,
            "active_counts": active_counts,
            "resolved_counts": resolved_counts,
            "items": insight_items[:safe_item_limit],
            "resolved_items": resolved_items[:safe_item_limit],
            "recent_events": hydrated_events,
            "updated_at": max(
                (float(item.get("updated_at", 0.0) or 0.0) for item in items),
                default=0.0,
            ),
        }
