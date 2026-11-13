"""Declarative human-confirmation records for committed workflow stages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..runtime.shared_tracker import SharedTracker
from .models import WorkflowRun, WorkflowStageState


WORKFLOW_CONFIRMATION_KIND = "workflow_stage_confirmation"


def _utc_now_iso() -> str:
    """Return one timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def workflow_confirmation_state(
    stage: Optional[WorkflowStageState],
) -> dict[str, Any]:
    """Return a validated declarative confirmation state for one stage."""

    if stage is None:
        return {}
    state = (stage.metadata or {}).get("human_confirmation_state")
    if not isinstance(state, dict):
        return {}
    if str(state.get("kind") or "").strip() != WORKFLOW_CONFIRMATION_KIND:
        return {}
    return dict(state)


def is_workflow_confirmation_review(review: dict[str, Any]) -> bool:
    """Return whether a Human Review row belongs to a workflow stage gate."""

    review_data = (
        review.get("review_data")
        if isinstance(review.get("review_data"), dict)
        else {}
    )
    return bool(review_data.get("workflow_confirmation")) and str(
        review.get("tool_name") or ""
    ).strip() == WORKFLOW_CONFIRMATION_KIND


def ensure_workflow_confirmation_review(
    run: WorkflowRun,
    *,
    workdir: str = "",
) -> Optional[dict[str, Any]]:
    """Create the idempotent Human Review row for a pending workflow gate."""

    if str(run.status or "").strip().lower() != "review_pending":
        return None
    stage = next(
        (
            item
            for item in run.stage_states
            if item.stage_id == str(run.current_stage_id or "").strip()
        ),
        None,
    )
    if stage is None or str(stage.status or "").strip().lower() != "review_pending":
        return None
    state = workflow_confirmation_state(stage)
    if str(state.get("status") or "").strip().lower() != "pending_review":
        return None

    review_id = str(state.get("review_id") or "").strip()
    review_identifier = str(state.get("review_identifier") or "").strip()
    if not review_id or not review_identifier:
        raise ValueError("workflow human confirmation is missing a stable review id")

    store = SharedTracker.load_review_store(workdir or None)
    reviews = [
        item
        for item in (store.get("reviews") or [])
        if isinstance(item, dict)
    ]
    existing = next(
        (
            item
            for item in reviews
            if str(item.get("id") or "").strip() == review_id
            or str(item.get("identifier") or "").strip() == review_identifier
        ),
        None,
    )
    if existing is not None:
        return dict(existing)

    config = (
        dict(state.get("config") or {})
        if isinstance(state.get("config"), dict)
        else {}
    )
    created_at = _utc_now_iso()
    owner = {
        key: str((run.metadata or {}).get(key) or "").strip()
        for key in ("tenant_id", "user_id", "workspace_id")
    }
    workspace = {
        "job_id": "",
        "process_id": "",
        "run_id": run.run_id,
        "tool": "workflow",
        "task_type": WORKFLOW_CONFIRMATION_KIND,
        "workdir": str(workdir or "").strip(),
        "log_file": "",
        "exitcode_file": "",
        "expected_artifacts": list(stage.artifact_refs.keys()),
        "context_file": "",
        "session_id": run.session_id,
        "parent_session_id": "",
        "issue_identifier": review_identifier,
        "preview_ports": [],
        "preview_urls": [],
        "status": "review_pending",
        "workflow_run_id": run.run_id,
        "workflow_stage_id": stage.stage_id,
        **{key: value for key, value in owner.items() if value},
    }
    review_data = {
        "workflow_confirmation": True,
        "workflow_run_id": run.run_id,
        "workflow_stage_id": stage.stage_id,
        "workflow_template_key": run.template_key,
        "session_id": run.session_id,
        "workdir": str(workdir or "").strip(),
        "store_workdir": str(workdir or "").strip(),
        "artifact_refs": dict(stage.artifact_refs or {}),
        "result_summary": str(stage.result_summary or "")[:4000],
        "tool_trace_context": {
            "workflow_run_id": run.run_id,
            "workflow_stage_id": stage.stage_id,
            "workflow_template_key": run.template_key,
            "workflow_stage_attempt": max(1, int(stage.attempt or 0)),
        },
        "workspace": workspace,
        **{key: value for key, value in owner.items() if value},
    }
    review_record = {
        "id": review_id,
        "identifier": review_identifier,
        "tool_server": "workflow",
        "tool_name": WORKFLOW_CONFIRMATION_KIND,
        "tool_args": {
            "run_id": run.run_id,
            "stage_id": stage.stage_id,
            "template_key": run.template_key,
            "attempt": max(1, int(stage.attempt or 0)),
        },
        "reason": str(
            config.get("reason")
            or config.get("title")
            or f"请确认 Workflow 阶段：{stage.label or stage.stage_id}"
        )[:1200],
        "review_data": review_data,
        "status": "pending_review",
        "created_at": created_at,
        "updated_at": created_at,
        "reviewer": None,
        "reviewed_at": None,
        "comments": None,
    }
    reviews.append(review_record)
    SharedTracker.sync_review_store(
        {"reviews": reviews, "history": list(store.get("history") or [])},
        workdir or None,
    )
    return dict(review_record)


__all__ = [
    "WORKFLOW_CONFIRMATION_KIND",
    "ensure_workflow_confirmation_review",
    "is_workflow_confirmation_review",
    "workflow_confirmation_state",
]
