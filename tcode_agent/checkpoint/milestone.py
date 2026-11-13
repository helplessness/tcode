"""Deterministic milestone checkpoints used to assemble model context.

The transcript remains the audit source.  A milestone checkpoint is a compact,
source-linked execution index that can be rebuilt from that transcript.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from ..message_context import is_real_user_message
from .manager import Checkpoint, CheckpointManager


CHECKPOINT_SCHEMA_VERSION = "tcode.milestone-checkpoint/v1"
_TERMINAL_STATES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "interrupted",
    "cancelled",
}
_PATH_KEYS = {
    "path",
    "output_path",
    "record_path",
    "artifact_path",
    "downloaded_pdf_paths",
    "tool_execution_log_paths",
    "artifacts",
}
_TASK_ID_KEYS = {
    "task_id",
    "job_id",
    "identifier",
    "session_id",
    "workflow_run_id",
    "subagent_session_id",
}
_LOCAL_JOB_RE = re.compile(r"\bLOCAL-[A-Za-z0-9._:-]+\b")


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.environ.get(name, default)).strip()))
    except (TypeError, ValueError):
        return max(minimum, default)


def _short(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + " ...[truncated]"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return _short(value, 240)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _json_safe(asdict(value), depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:80]]
    return _short(value, 400)


def _message_metadata(message: Any) -> dict[str, Any]:
    metadata = getattr(message, "metadata", {})
    return dict(metadata) if isinstance(metadata, dict) else {}


def _tool_metadata(tool_call: Any) -> dict[str, Any]:
    metadata = getattr(tool_call, "metadata", {})
    return dict(metadata) if isinstance(metadata, dict) else {}


def _tool_status(tool_call: Any) -> str:
    if str(getattr(tool_call, "error", "") or "").strip():
        return "error"
    state = getattr(tool_call, "state", None)
    value = str(getattr(state, "value", state) or "").strip().lower()
    return value or "completed"


def _latest_contract(metadata: dict[str, Any], supplied: Any = None) -> dict[str, Any]:
    if supplied is not None:
        if hasattr(supplied, "to_dict"):
            try:
                return dict(supplied.to_dict() or {})
            except Exception:
                pass
        if isinstance(supplied, dict):
            return dict(supplied)
    for key in ("active_turn", "last_turn"):
        turn = metadata.get(key)
        if not isinstance(turn, dict):
            continue
        extra = turn.get("extra")
        if not isinstance(extra, dict):
            continue
        contract = extra.get("outcome_contract")
        if isinstance(contract, dict):
            return dict(contract)
    contract = metadata.get("contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _is_real_user_message(message: Any) -> bool:
    return is_real_user_message(message)


def _continuation_only(value: str) -> bool:
    return str(value or "").strip().lower().strip("。.!！,， ") in {
        "继续", "好的", "好", "好的继续", "好的，继续", "请继续", "继续执行", "继续下一步",
        "continue", "please continue", "go on", "proceed", "ok", "okay",
    }


def _user_directives(messages: list[Any]) -> list[dict[str, str]]:
    directives = []
    for message in messages:
        metadata = getattr(message, "metadata", {}) or {}
        if metadata.get("event_type") == "side_chat_import":
            # Explicitly imported human requests remain directives even when
            # long tool evidence is sliced/compressed. Internal retries are not.
            directives.extend(dict(item) for item in metadata.get("user_directives", [])
                if isinstance(item, dict) and item.get("instruction"))
        elif _is_real_user_message(message) and not _continuation_only(str(getattr(message, "content", "") or "")):
            directives.append({"message_id": str(getattr(message, "id", "") or ""),
                "instruction": str(getattr(message, "content", "") or "").strip()})
    return directives


def merge_user_directives(previous: Any, messages: list[Any]) -> list[dict[str, str]]:
    """Keep the ordered source ledger; later instructions can revise earlier ones.

    Do not infer revocation from age, word matching, or a summary budget. Reading
    full messages also upgrades older checkpoints whose entries were truncated.
    """
    current = {}
    for item in _user_directives(messages):
        key = item["message_id"] or item["instruction"]
        current[key] = item
    stopped_ids = {str(getattr(message, "id", "")) for message in messages
                   if (getattr(message, "metadata", {}) or {}).get("turn_interruption")
                   or (str(getattr(message, "role", "")) == "user"
                       and not _is_real_user_message(message)
                       and (getattr(message, "metadata", {}) or {}).get("event_type") != "side_chat_import")}
    older = {str(item.get("message_id") or item.get("instruction")): dict(item)
        for item in (previous or []) if isinstance(item, dict)
        and str(item.get("message_id") or "") not in stopped_ids}
    old_keys, new_keys = list(older), list(current)
    records = []
    for operation, old_start, old_end, new_start, new_end in SequenceMatcher(
        a=old_keys, b=new_keys, autojunk=False,
    ).get_opcodes():
        if operation in {"delete", "replace"}:
            records.extend(older[key] for key in old_keys[old_start:old_end])
        if operation in {"equal", "insert", "replace"}:
            records.extend(current[key] for key in new_keys[new_start:new_end])
    return records


def _goal_snapshot(messages: list[Any], contract: dict[str, Any]) -> dict[str, Any]:
    user_messages = [message for message in messages if _is_real_user_message(message)]
    latest_user = user_messages[-1] if user_messages else None
    current = str(contract.get("goal") or "").strip()
    directives = _user_directives(messages)
    if _continuation_only(current) and directives:
        current = directives[-1]["instruction"]
    if not current and latest_user is not None:
        current = str(getattr(latest_user, "content", "") or "").strip()
    return {
        "current": _short(current, 4000),
        "original": directives[0] if directives else {},
        "user_directives": directives,
        "goal_id": str(contract.get("goal_id") or ""),
        "contract_id": str(contract.get("contract_id") or ""),
        "contract_version": _safe_int(contract.get("version")),
        "revision": len(user_messages),
        "source_message_id": str(getattr(latest_user, "id", "") or ""),
    }


def _incremental_goal_snapshot(
    messages: list[Any],
    contract: dict[str, Any],
    previous_goal: dict[str, Any],
) -> dict[str, Any]:
    user_messages = [message for message in messages if _is_real_user_message(message)]
    latest_user = user_messages[-1] if user_messages else None
    current = str(contract.get("goal") or "").strip()
    directives = _user_directives(messages)
    if _continuation_only(current):
        current = directives[-1]["instruction"] if directives else str(previous_goal.get("current") or "")
    if not current and latest_user is not None:
        candidate = str(getattr(latest_user, "content", "") or "").strip()
        if not _continuation_only(candidate):
            current = candidate
    if not current:
        current = str(previous_goal.get("current") or "")
    return {
        "current": _short(current, 4000),
        "original": previous_goal.get("original") or (directives[0] if directives else {}),
        "user_directives": merge_user_directives(previous_goal.get("user_directives"), messages),
        "goal_id": str(contract.get("goal_id") or previous_goal.get("goal_id") or ""),
        "contract_id": str(
            contract.get("contract_id") or previous_goal.get("contract_id") or ""
        ),
        "contract_version": _safe_int(
            contract.get("version"),
            _safe_int(previous_goal.get("contract_version")),
        ),
        "revision": _safe_int(previous_goal.get("revision")) + len(user_messages),
        "source_message_id": str(
            getattr(latest_user, "id", "")
            or previous_goal.get("source_message_id")
            or ""
        ),
    }


def _merge_records(
    previous: Any,
    current: Any,
    *,
    keys: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[tuple[str, ...], int] = {}
    for item in [*(previous or []), *(current or [])]:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        marker = tuple(str(normalized.get(key) or "") for key in keys)
        if marker in positions and any(marker):
            merged[positions[marker]] = normalized
            continue
        positions[marker] = len(merged)
        merged.append(normalized)
    return merged[-limit:]


def _incremental_source(
    messages: list[Any], previous: Optional[Checkpoint]
) -> tuple[list[Any], dict[str, Any]]:
    if previous is None:
        return messages, {}
    previous_state = dict(previous.state_snapshot or {})
    if str(previous_state.get("schema_version") or "") != CHECKPOINT_SCHEMA_VERSION:
        return messages, {}
    cursor = max(0, int(previous.message_cursor or 0))
    if cursor > len(messages):
        return messages, {}
    if cursor:
        source = previous_state.get("source")
        recent_ids = (
            list(source.get("recent_message_ids") or [])
            if isinstance(source, dict)
            else []
        )
        expected_id = str(recent_ids[-1] or "") if recent_ids else ""
        actual_id = str(getattr(messages[cursor - 1], "id", "") or "")
        if expected_id and expected_id != actual_id:
            return messages, {}
    return messages[cursor:], previous_state


def _collect_paths(value: Any, *, key: str = "", depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    paths: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            normalized_key = str(child_key or "").strip().lower()
            if normalized_key in _PATH_KEYS:
                paths.extend(_collect_paths(child, key=normalized_key, depth=depth + 1))
            elif isinstance(child, (dict, list, tuple)):
                paths.extend(_collect_paths(child, key=normalized_key, depth=depth + 1))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            paths.extend(_collect_paths(child, key=key, depth=depth + 1))
    elif isinstance(value, str):
        text = value.strip()
        if text and (key in _PATH_KEYS or text.startswith(("/", "./", "../"))):
            paths.append(text)
    return paths


def _artifact_snapshots(messages: list[Any], limit: int) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        for tool_call in getattr(message, "tool_calls", []) or []:
            metadata = _tool_metadata(tool_call)
            for path in _collect_paths(metadata):
                if path in seen:
                    continue
                seen.add(path)
                artifacts.append(
                    {
                        "path": path,
                        "tool": str(getattr(tool_call, "tool_name", "") or ""),
                        "call_id": str(getattr(tool_call, "call_id", "") or ""),
                        "status": _tool_status(tool_call),
                    }
                )
    return artifacts[-limit:]


def _task_references(metadata: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(metadata.get(key) or "")
        for key in _TASK_ID_KEYS
        if str(metadata.get(key) or "").strip()
    }


def _tool_state_snapshots(
    messages: list[Any],
    *,
    preview_chars: int,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    completed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for message in messages:
        if str(getattr(message, "role", "") or "") != "tool":
            continue
        content = str(getattr(message, "content", "") or "")
        content_hash = hashlib.sha256(
            content.encode("utf-8", errors="ignore")
        ).hexdigest()
        for tool_call in getattr(message, "tool_calls", []) or []:
            metadata = _tool_metadata(tool_call)
            call_id = str(getattr(tool_call, "call_id", "") or "")
            status = _tool_status(tool_call)
            item = {
                "tool": str(getattr(tool_call, "tool_name", "") or ""),
                "call_id": call_id,
                "status": status,
                "summary": _short(content, preview_chars),
                "references": _task_references(metadata),
            }
            if status in {"error", "failed", "interrupted"}:
                blocked.append(item)
            else:
                completed.append(item)
            evidence.append(
                {
                    "call_id": call_id,
                    "tool": item["tool"],
                    "status": status,
                    "content_hash": content_hash,
                    "content_preview": _short(content, preview_chars),
                    "output_path": str(metadata.get("output_path") or ""),
                    "references": _task_references(metadata),
                }
            )
    return completed[-limit:], blocked[-limit:], evidence[-limit:]


def _assistant_decisions(messages: list[Any], limit: int = 4) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    for message in messages:
        if str(getattr(message, "role", "") or "") != "assistant":
            continue
        if getattr(message, "tool_calls", None) or bool(getattr(message, "summary", False)):
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if not content or str(getattr(message, "presentation", "") or "") == "async_completion":
            continue
        decisions.append(
            {
                "message_id": str(getattr(message, "id", "") or ""),
                "summary": _short(content, 800),
            }
        )
    return decisions[-limit:]


def _async_snapshots(messages: list[Any], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for item in metadata.get("pending_continuations") or []:
        if not isinstance(item, dict):
            continue
        snapshots.append(
            {
                "identifier": str(item.get("identifier") or item.get("id") or ""),
                "status": str(
                    item.get("status")
                    or item.get("delivery_status")
                    or item.get("continuation_status")
                    or "pending"
                ),
                "result_summary": _short(item.get("result_summary") or item.get("message"), 1200),
                "task_id": str(item.get("task_id") or item.get("job_id") or ""),
            }
        )
    known = {str(item.get("identifier") or "") for item in snapshots}
    for message in messages[-40:]:
        content = str(getattr(message, "content", "") or "")
        for identifier in _LOCAL_JOB_RE.findall(content):
            if identifier in known:
                continue
            known.add(identifier)
            snapshots.append(
                {
                    "identifier": identifier,
                    "status": "referenced",
                    "result_summary": "",
                    "task_id": "",
                }
            )
    return snapshots[-20:]


def _active_state(metadata: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    active = metadata.get("active_turn")
    if not isinstance(active, dict):
        return [], []
    in_progress: list[dict[str, Any]] = []
    for item in active.get("pending_tool_calls") or []:
        if not isinstance(item, dict):
            continue
        in_progress.append(
            {
                "tool": str(item.get("tool_name") or item.get("name") or ""),
                "call_id": str(item.get("call_id") or item.get("id") or ""),
                "status": str(item.get("state") or "pending"),
            }
        )
    next_actions: list[str] = []
    state = str(active.get("state") or "").strip()
    if state:
        next_actions.append(f"Continue turn {active.get('turn_id') or ''} from state {state}.")
    return in_progress[-12:], next_actions


def build_milestone_checkpoint(
    *,
    session_id: str,
    messages: list[Any],
    session_metadata: Optional[dict[str, Any]] = None,
    contract: Any = None,
    milestone: str,
    agent_name: str = "",
    title: str = "",
    previous: Optional[Checkpoint] = None,
) -> Checkpoint:
    """Build a source-linked checkpoint without asking a model to summarize."""

    metadata = dict(session_metadata or {})
    contract_snapshot = _latest_contract(metadata, contract)
    preview_chars = _env_int("TCODE_CHECKPOINT_EVIDENCE_PREVIEW_CHARS", 600, minimum=120)
    evidence_limit = _env_int("TCODE_CHECKPOINT_EVIDENCE_LIMIT", 24, minimum=4)
    artifact_limit = _env_int("TCODE_CHECKPOINT_ARTIFACT_LIMIT", 40, minimum=4)
    source_messages, previous_state = _incremental_source(messages, previous)
    completed_delta, blocked_delta, evidence_delta = _tool_state_snapshots(
        source_messages,
        preview_chars=preview_chars,
        limit=evidence_limit,
    )
    previous_execution = (
        dict(previous_state.get("state") or {}) if previous_state else {}
    )
    completed = _merge_records(
        previous_execution.get("completed"),
        completed_delta,
        keys=("call_id", "tool"),
        limit=evidence_limit,
    )
    blocked = _merge_records(
        previous_execution.get("blocked"),
        blocked_delta,
        keys=("call_id", "tool"),
        limit=evidence_limit,
    )
    evidence = _merge_records(
        previous_state.get("evidence_index") if previous_state else [],
        evidence_delta,
        keys=("call_id", "content_hash"),
        limit=evidence_limit,
    )
    artifacts = _merge_records(
        previous_state.get("artifacts") if previous_state else [],
        _artifact_snapshots(source_messages, artifact_limit),
        keys=("path",),
        limit=artifact_limit,
    )
    in_progress, next_actions = _active_state(metadata)
    async_tasks = _merge_records(
        previous_state.get("async_tasks") if previous_state else [],
        _async_snapshots(source_messages, metadata),
        keys=("identifier", "task_id"),
        limit=20,
    )
    unresolved = [
        _short(item.get("summary") or item.get("tool"), 600)
        for item in blocked[-8:]
        if str(item.get("summary") or item.get("tool") or "").strip()
    ]
    unresolved.extend(
        f"Async task {item.get('identifier')} is {item.get('status')}."
        for item in async_tasks
        if str(item.get("status") or "").lower()
        not in {"completed", "delivered", "continued", "succeeded"}
    )
    previous_goal = (
        dict(previous_state.get("goal") or {}) if previous_state else {}
    )
    goal_snapshot = (
        _incremental_goal_snapshot(source_messages, contract_snapshot, previous_goal)
        if previous_state
        else _goal_snapshot(messages, contract_snapshot)
    )
    # Rebuild from the available audit history as well as the previous ledger,
    # so upgrading an old, bounded checkpoint recovers omitted directives.
    goal_snapshot["user_directives"] = merge_user_directives(
        previous_goal.get("user_directives"), messages,
    )
    decisions = _merge_records(
        previous_state.get("decisions") if previous_state else [],
        _assistant_decisions(source_messages),
        keys=("message_id",),
        limit=4,
    )
    state_snapshot = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "milestone": str(milestone or "manual"),
        "goal": goal_snapshot,
        "state": {
            "completed": completed,
            "in_progress": in_progress,
            "blocked": blocked,
            "open_questions": list(dict.fromkeys(unresolved))[-12:],
        },
        "artifacts": artifacts,
        "decisions": decisions,
        "async_tasks": async_tasks,
        "evidence_index": evidence,
        "next_actions": next_actions,
        "source": {
            "session_id": session_id,
            "message_cursor": len(messages),
            "recent_message_ids": [
                str(getattr(message, "id", "") or "") for message in messages[-16:]
            ],
        },
    }
    fingerprint_payload = {
        "milestone": state_snapshot["milestone"],
        "goal": state_snapshot["goal"],
        "message_cursor": len(messages),
        "last_message_id": str(getattr(messages[-1], "id", "") or "") if messages else "",
        "completed_call_ids": [item.get("call_id") for item in completed],
        "blocked_call_ids": [item.get("call_id") for item in blocked],
        "async_tasks": async_tasks,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            _json_safe(fingerprint_payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="ignore")
    ).hexdigest()
    artifact_projection = {
        str(item.get("path") or index): item for index, item in enumerate(artifacts)
    }
    last_turn = metadata.get("last_turn")
    terminal_state = (
        str(last_turn.get("state") or "").strip().lower()
        if isinstance(last_turn, dict)
        else ""
    )
    step_name = "completed" if terminal_state == "completed" else "execute"
    return Checkpoint(
        task_id=session_id,
        session_id=session_id,
        round_index=int(state_snapshot["goal"].get("revision") or 0),
        step_name=step_name,
        message_cursor=len(messages),
        state_snapshot=state_snapshot,
        contract_snapshot=_json_safe(contract_snapshot),
        artifacts=artifact_projection,
        pending_action=next_actions[0] if next_actions else "",
        resume_point=str(milestone or "manual"),
        metadata={
            "agent_name": str(agent_name or ""),
            "title": str(title or ""),
            "content_fingerprint": fingerprint,
            "source_message_count": len(messages),
        },
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        milestone=str(milestone or "manual"),
        evidence_refs=evidence,
        unresolved_questions=list(dict.fromkeys(unresolved))[-12:],
    )


def save_milestone_checkpoint(
    *,
    session_id: str,
    messages: list[Any],
    workdir: Optional[str],
    session_metadata: Optional[dict[str, Any]] = None,
    contract: Any = None,
    milestone: str,
    agent_name: str = "",
    title: str = "",
) -> Checkpoint:
    previous = CheckpointManager.load(session_id, workdir=workdir)
    checkpoint = build_milestone_checkpoint(
        session_id=session_id,
        messages=messages,
        session_metadata=session_metadata,
        contract=contract,
        milestone=milestone,
        agent_name=agent_name,
        title=title,
        previous=previous,
    )
    candidate_id = checkpoint.id
    persisted_id = CheckpointManager.save(checkpoint, workdir=workdir)
    if persisted_id != candidate_id:
        # A concurrent/newer milestone may have won the guarded write. Return
        # the actual current projection so callers never inject stale context.
        return CheckpointManager.load(session_id, workdir=workdir) or checkpoint
    checkpoint.id = persisted_id
    return checkpoint


def infer_session_milestone(session: Any) -> str:
    """Return a milestone name only for states worth checkpointing."""

    messages = list(getattr(session, "messages", []) or [])
    if messages and str(getattr(messages[-1], "presentation", "") or "") in {
        "async_completion",
        "async_final",
    }:
        return "async_completed"
    metadata = dict(getattr(session, "metadata", {}) or {})
    active = metadata.get("active_turn")
    if isinstance(active, dict):
        state = str(active.get("state") or "").strip().lower()
        if state == "preparing":
            return "goal_changed"
        if state.startswith("waiting_"):
            return "waiting_for_input"
        if state == "tool_result_recorded":
            threshold = _env_int("TCODE_CHECKPOINT_TOOL_BATCH_SIZE", 3, minimum=1)
            latest = CheckpointManager.load(
                str(getattr(session, "id", "") or ""),
                workdir=str(getattr(session, "workdir", "") or "") or None,
            )
            cursor = int(getattr(latest, "message_cursor", 0) or 0) if latest else 0
            recent_tools = sum(
                1
                for message in messages[cursor:]
                if str(getattr(message, "role", "") or "") == "tool"
            )
            if recent_tools >= threshold:
                return "tool_batch"
    last_turn = metadata.get("last_turn")
    if isinstance(last_turn, dict):
        state = str(last_turn.get("state") or "").strip().lower()
        if state in _TERMINAL_STATES:
            return "turn_completed"
    return ""


def maybe_save_session_milestone(session: Any) -> Optional[Checkpoint]:
    milestone = infer_session_milestone(session)
    if not milestone:
        return None
    return save_milestone_checkpoint(
        session_id=str(getattr(session, "id", "") or ""),
        messages=list(getattr(session, "messages", []) or []),
        workdir=str(getattr(session, "workdir", "") or "") or None,
        session_metadata=dict(getattr(session, "metadata", {}) or {}),
        milestone=milestone,
        agent_name=str(getattr(session, "agent_name", "") or ""),
        title=str(getattr(session, "title", "") or ""),
    )
