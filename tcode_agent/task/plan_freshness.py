"""Freshness metadata for structured plans and todo-derived task state."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_goal(goal: str) -> str:
    return " ".join(str(goal or "").strip().split())


def goal_hash(goal: str) -> str:
    return _stable_hash({"goal": normalize_goal(goal)})


def todo_content_hash(todos: list[dict[str, Any]] | None) -> str:
    """Hash todo content and order while ignoring transient status changes."""
    normalized: list[dict[str, str]] = []
    for item in todos or []:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content", "") or "").strip().split())
        if not content:
            continue
        todo_id = str(item.get("id", "") or item.get("todo_id", "") or "").strip()
        normalized.append({"id": todo_id, "content": content})
    return _stable_hash({"todos": normalized})


def _coerce_revision(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def update_task_fingerprint(
    metadata: dict[str, Any],
    *,
    goal: str,
    todos: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update session task fingerprint and bump revision on goal/todo-content drift."""
    updated = dict(metadata or {})
    goal_text = normalize_goal(goal) or normalize_goal(str(updated.get("task_goal", "") or ""))
    next_goal_hash = goal_hash(goal_text)
    next_todo_hash = todo_content_hash(todos)

    previous_revision = _coerce_revision(updated.get("task_revision"))
    previous_goal_hash = str(updated.get("task_goal_hash", "") or "").strip()
    previous_todo_hash = str(updated.get("task_todo_hash", "") or "").strip()
    had_fingerprint = previous_revision > 0 or bool(previous_goal_hash) or bool(previous_todo_hash)
    changed = (
        previous_goal_hash != next_goal_hash
        or previous_todo_hash != next_todo_hash
    )

    if not had_fingerprint:
        revision = max(1, previous_revision)
    elif changed:
        revision = previous_revision + 1
    else:
        revision = previous_revision

    updated["task_goal"] = goal_text or "Current task plan"
    updated["task_revision"] = revision
    updated["task_goal_hash"] = next_goal_hash
    updated["task_todo_hash"] = next_todo_hash
    # Establishing the first plan is not a task switch. Bind previously
    # activated tools once; later goal changes still invalidate them.
    discovery = updated.get("tool_discovery")
    if not previous_goal_hash and isinstance(discovery, dict) and not discovery.get("task_goal_hash"):
        updated["tool_discovery"] = {**discovery, "task_goal_hash": next_goal_hash}
    return updated, {
        "task_revision": revision,
        "task_goal_hash": next_goal_hash,
        "task_todo_hash": next_todo_hash,
        "task_goal": updated["task_goal"],
    }


def current_task_fingerprint(
    metadata: dict[str, Any] | None,
    *,
    todos: list[dict[str, Any]] | None = None,
    fallback_goal: str = "",
) -> dict[str, Any]:
    data = dict(metadata or {})
    goal_text = normalize_goal(str(data.get("task_goal", "") or "")) or normalize_goal(fallback_goal)
    return {
        "task_revision": _coerce_revision(data.get("task_revision")),
        "task_goal_hash": str(data.get("task_goal_hash", "") or "").strip() or goal_hash(goal_text),
        "task_todo_hash": str(data.get("task_todo_hash", "") or "").strip() or todo_content_hash(todos),
        "task_goal": goal_text,
    }


def stamp_plan_source(plan: Any, fingerprint: dict[str, Any]) -> None:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    metadata["source_revision"] = _coerce_revision(fingerprint.get("task_revision"))
    metadata["source_goal_hash"] = str(fingerprint.get("task_goal_hash", "") or "").strip()
    metadata["source_todo_hash"] = str(fingerprint.get("task_todo_hash", "") or "").strip()
    metadata["source_goal"] = str(fingerprint.get("task_goal", "") or "").strip()
    metadata["stale"] = False
    metadata.pop("stale_reasons", None)
    setattr(plan, "metadata", metadata)


def validate_plan_freshness(plan: Any, fingerprint: dict[str, Any]) -> list[str]:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    errors: list[str] = []

    source_revision = _coerce_revision(metadata.get("source_revision"))
    current_revision = _coerce_revision(fingerprint.get("task_revision"))
    if source_revision <= 0:
        errors.append("plan.freshness: plan has no source task revision; recreate or re-enter plan mode")
    elif current_revision > 0 and source_revision != current_revision:
        errors.append(
            "plan.freshness: plan source revision "
            f"{source_revision} does not match current task revision {current_revision}"
        )

    source_goal_hash = str(metadata.get("source_goal_hash", "") or "").strip()
    current_goal_hash = str(fingerprint.get("task_goal_hash", "") or "").strip()
    if source_goal_hash and current_goal_hash and source_goal_hash != current_goal_hash:
        errors.append("plan.freshness: plan goal differs from the current task goal")
    elif not source_goal_hash:
        errors.append("plan.freshness: plan has no source goal hash")

    source_todo_hash = str(metadata.get("source_todo_hash", "") or "").strip()
    current_todo_hash = str(fingerprint.get("task_todo_hash", "") or "").strip()
    if source_todo_hash and current_todo_hash and source_todo_hash != current_todo_hash:
        errors.append("plan.freshness: todo content/order changed since this plan was created")
    elif not source_todo_hash:
        errors.append("plan.freshness: plan has no source todo hash")

    return errors


def apply_freshness_result(plan: Any, errors: list[str]) -> None:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    metadata["stale"] = bool(errors)
    if errors:
        metadata["stale_reasons"] = list(errors)
    else:
        metadata.pop("stale_reasons", None)
    setattr(plan, "metadata", metadata)
    existing_errors = [str(err) for err in (getattr(plan, "validation_errors", []) or [])]
    freshness_prefix = "plan.freshness:"
    kept = [err for err in existing_errors if not err.startswith(freshness_prefix)]
    setattr(plan, "validation_errors", kept + list(errors))
