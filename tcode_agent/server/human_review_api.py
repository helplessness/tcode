"""Human review queue, approvals and rejection endpoints.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ..runtime import TaskRuntimeStore
from ..runtime.shared_tracker import SharedTracker
from ..task.orchestration import build_action_policy
from . import workflow_runtime
from ._shared import (
    _list_descendant_sessions_recursive,
    _resume_session_after_human_review,
)
from .workspace_scope import (
    _session_owner_kwargs,
    _user_workdirs_root,
    _workspace_context_from_request,
)
from .auth import auth_required
from .job_workspace import (
    _find_human_review_store_for_update,
    _has_approved_review_for_call,
    _load_aggregated_human_review_queue,
    _load_human_review_config,
    _load_human_review_queue,
    _load_human_review_queue_for_workdir,
    _normalize_review_workspace,
    _save_human_review_queue,
    _save_human_review_queue_for_workdir,
)
from .schemas import (
    HumanReviewApproveRequest,
    HumanReviewDeleteRequest,
    HumanReviewRejectRequest,
    HumanReviewSubmitRequest,
)
from .state import ServerState
from .workflow_api import _sync_workflow_human_review_decision

router = APIRouter()


@router.get("/api/human-review/config")
async def get_human_review_config():
    config = _load_human_review_config()
    return {
        "enabled": config.get("enabled", False),
        "mode": config.get("mode", "disabled"),
        "required_tools": list(config.get("required_review_tools", {}).keys()),
        "auto_review_rules": config.get("auto_review_rules", {}),
    }


@router.get("/api/v1/human-review/config")
async def get_human_review_config_v1():
    return await get_human_review_config()


def _human_review_record_workdir(review: dict[str, Any], fallback_workdir: str = "") -> str:
    review_data = (
        review.get("review_data")
        if isinstance(review.get("review_data"), dict)
        else {}
    )
    workspace = (
        review_data.get("workspace")
        if isinstance(review_data.get("workspace"), dict)
        else {}
    )
    return str(
        review_data.get("store_workdir")
        or review_data.get("workdir")
        or workspace.get("workdir")
        or fallback_workdir
        or ""
    ).strip()


def _human_review_visible_for_request(
    review: dict[str, Any],
    request: Request,
    *,
    fallback_workdir: str = "",
) -> bool:
    if not auth_required():
        return True
    workdir_text = _human_review_record_workdir(review, fallback_workdir)
    if not workdir_text:
        return False
    workspace_context = _workspace_context_from_request(request)
    try:
        Path(workdir_text).resolve().relative_to(_user_workdirs_root(workspace_context).resolve())
    except Exception:
        return False
    return True


def _require_human_review_access(
    review: dict[str, Any],
    request: Request,
    *,
    fallback_workdir: str = "",
) -> None:
    if not _human_review_visible_for_request(
        review,
        request,
        fallback_workdir=fallback_workdir,
    ):
        raise HTTPException(404, "review not found")


@router.get("/api/human-review/queue")
async def list_human_review_queue(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
    review_id: str = "",
    review_identifier: str = "",
    session_id: str = "",
):
    safe_limit = max(1, min(500, int(limit)))
    store = _load_aggregated_human_review_queue()
    reviews = store.get("reviews", [])
    reviews = [
        r
        for r in reviews
        if isinstance(r, dict) and _human_review_visible_for_request(r, request)
    ]
    _reconcile_resolved_human_review_runtimes(reviews)

    if status:
        status_lower = status.lower()
        reviews = [
            r for r in reviews if str(r.get("status", "")).lower() == status_lower
        ]

    if session_id.strip():
        scope_id = session_id.strip()
        descendants = await _list_descendant_sessions_recursive(
            scope_id,
            owner_kwargs=_session_owner_kwargs(_workspace_context_from_request(request)),
            include_side_chats=False,
        )
        scope_ids = {scope_id, *(child.id for child in descendants)}
        reviews = [r for r in reviews if
            str((r.get('review_data') or {}).get('session_id') or '') in scope_ids
            or str((r.get('review_data') or {}).get('parent_session_id') or '') == scope_id]

    if review_id.strip():
        reviews = [r for r in reviews if str(r.get("id") or "") == review_id.strip()]
    if review_identifier.strip():
        reviews = [r for r in reviews if str(r.get("identifier") or "") == review_identifier.strip()]
    ordered = list(reversed(reviews))
    selected = ordered[:safe_limit]

    return {
        "count": len(selected),
        "total": len(reviews),
        "reviews": selected,
    }


@router.post("/api/human-review/submit")
async def submit_human_review(request: HumanReviewSubmitRequest):
    config = _load_human_review_config()
    if not config.get("enabled", False):
        raise HTTPException(400, "Human review is not enabled")

    issue_id = request.issue_id or f"review-{uuid.uuid4().hex[:10]}"
    issue_identifier = (
        request.issue_identifier
        or f"REVIEW-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    )
    now = datetime.utcnow().isoformat() + "Z"
    review_data = request.review_data or {}
    if not isinstance(review_data, dict):
        review_data = {}

    review_record = {
        "id": issue_id,
        "identifier": issue_identifier,
        "tool_server": request.tool_server,
        "tool_name": request.tool_name,
        "tool_args": request.tool_args,
        "reason": request.reason
        or config.get("required_review_tools", {})
        .get(request.tool_server, {})
        .get("reason", "需要人工确认"),
        "review_data": review_data,
        "status": "pending_review",
        "created_at": now,
        "updated_at": now,
        "reviewer": None,
        "reviewed_at": None,
        "comments": None,
    }
    review_record["review_data"]["workspace"] = _normalize_review_workspace(
        review_record, review_record["review_data"]
    )
    review_workspace = review_record["review_data"].get("workspace")
    if isinstance(review_workspace, dict):
        task_id = str(review_workspace.get("task_id", "") or "").strip()
        issue_ref = str(review_workspace.get("issue_identifier", "") or "").strip()
        if not task_id and issue_ref:
            tracker_task = SharedTracker.get_task(
                identifier=issue_ref, workdir=ServerState.get_workdir()
            )
            if tracker_task:
                task_id = str(tracker_task.get("task_id", "") or "").strip()
                review_workspace["task_id"] = task_id
        if task_id:
            review_record["task_id"] = task_id

    store = _load_human_review_queue()
    reviews = store.get("reviews", [])
    reviews.append(review_record)
    store["reviews"] = reviews
    _save_human_review_queue(store)

    task_id = str(review_record.get("task_id", "") or "").strip()
    if task_id:
        try:
            SharedTracker.append_event(
                task_id=task_id,
                kind="review_pending",
                source="human_review",
                payload={
                    "review_identifier": issue_identifier,
                    "reason": str(review_record.get("reason", "") or ""),
                },
                workdir=ServerState.get_workdir(),
            )
        except Exception:
            pass

    return {
        "id": issue_id,
        "identifier": issue_identifier,
        "status": "pending_review",
        "message": "Review submitted successfully",
    }


def _transition_human_review_runtime(
    review: dict[str, Any],
    *,
    status: str,
    phase: str,
    workdir: str,
) -> None:
    review_data = (
        review.get("review_data")
        if isinstance(review.get("review_data"), dict)
        else {}
    )
    TaskRuntimeStore.transition_linked_open(
        [review.get("id"), review.get("identifier")],
        status=status,
        phase=phase,
        source="human_review",
        session_id=str(review_data.get("session_id", "") or "").strip(),
        workdir=workdir or None,
        metadata={"review_decision": str(review.get("status", "") or status)},
    )


def _reconcile_resolved_human_review_runtimes(reviews: list[Any]) -> None:
    groups: dict[tuple[str, str, str, str], set[str]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_status = str(review.get("status", "") or "").strip().lower()
        runtime_status = {
            "approved": "completed",
            "rejected": "cancelled",
        }.get(review_status)
        if not runtime_status:
            continue
        review_data = (
            review.get("review_data")
            if isinstance(review.get("review_data"), dict)
            else {}
        )
        workspace = (
            review_data.get("workspace")
            if isinstance(review_data.get("workspace"), dict)
            else {}
        )
        review_workdir = str(
            review_data.get("store_workdir")
            or review_data.get("workdir")
            or workspace.get("workdir")
            or ""
        ).strip()
        session_id = str(review_data.get("session_id", "") or "").strip()
        references = groups.setdefault(
            (review_workdir, session_id, runtime_status, review_status),
            set(),
        )
        for value in (review.get("id"), review.get("identifier")):
            text = str(value or "").strip()
            if text:
                references.add(text)

    for (workdir, session_id, runtime_status, review_status), references in groups.items():
        TaskRuntimeStore.transition_linked_open(
            references,
            status=runtime_status,
            phase=f"reconciled_{review_status}",
            source="human_review",
            session_id=session_id,
            workdir=workdir or None,
            metadata={"review_decision": review_status},
        )


@router.post("/api/human-review/approve")
async def approve_human_review(
    payload: HumanReviewApproveRequest,
    request: Request,
):
    issue_id = (payload.issue_id or "").strip()
    issue_identifier = (payload.issue_identifier or "").strip()
    if not issue_id and not issue_identifier:
        raise HTTPException(400, "id or identifier is required")

    found = _find_human_review_store_for_update(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
    )
    if found is None:
        not_found_key = issue_id or issue_identifier
        raise HTTPException(404, f"review not found: {not_found_key}")
    review_store_workdir, store, reviews, target_review = found
    _require_human_review_access(
        target_review,
        request,
        fallback_workdir=review_store_workdir,
    )

    current_status = str(target_review.get("status", "")).lower()
    if current_status != "pending_review":
        raise HTTPException(400, f"review is not pending (current: {current_status})")

    now = datetime.utcnow().isoformat() + "Z"
    approved_override = (
        payload.approved_tool_args_override
        if isinstance(payload.approved_tool_args_override, dict)
        and payload.approved_tool_args_override
        else None
    )
    has_feedback = bool(
        str(payload.comments or "").strip()
        or str(payload.resume_instruction or "").strip()
    )
    if payload.apply_feedback_before_execution is None:
        has_revision_guidance = bool(str(payload.resume_instruction or "").strip())
    else:
        has_revision_guidance = bool(
            payload.apply_feedback_before_execution and has_feedback
        )
    if approved_override:
        execution_disposition = "execute_override"
    elif has_revision_guidance:
        execution_disposition = "revise_before_execute"
    else:
        execution_disposition = "execute_original"

    target_review["status"] = "approved"
    target_review["reviewed_at"] = now
    target_review["updated_at"] = now
    target_review["comments"] = payload.comments
    target_review["approved_tool_args_override"] = approved_override
    target_review["resume_instruction"] = payload.resume_instruction
    target_review["execution_disposition"] = execution_disposition
    target_review["reviewer"] = "human"

    store["reviews"] = reviews
    _save_human_review_queue_for_workdir(store, review_store_workdir)
    _transition_human_review_runtime(
        target_review,
        status="completed",
        phase="approved",
        workdir=review_store_workdir,
    )

    resume_requested = payload.resume_after_approval is not False
    resume_state: dict[str, Any] = {
        "requested": resume_requested,
        "status": "not_requested",
    }
    if workflow_runtime.is_workflow_confirmation_review(target_review):
        synced_run = await _sync_workflow_human_review_decision(
            target_review,
            decision="approved",
            workdir=review_store_workdir,
        )
        target_review["resume_status"] = "completed" if synced_run else "failed"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        target_review["resume_result"] = {
            "status": target_review["resume_status"],
            "workflow_run_id": str(getattr(synced_run, "run_id", "") or ""),
            "workflow_status": str(getattr(synced_run, "status", "") or ""),
        }
        _save_human_review_queue_for_workdir(store, review_store_workdir)
        resume_state = {
            "requested": True,
            "status": target_review["resume_status"],
            "session_id": str(
                (target_review.get("review_data") or {}).get("session_id", "")
            ),
        }
    elif resume_requested:
        target_review["resume_status"] = "queued"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_human_review_queue_for_workdir(store, review_store_workdir)

        review_id = str(target_review.get("id", "")).strip()

        async def _runner():
            result = await _resume_session_after_human_review(target_review)
            latest = _load_human_review_queue_for_workdir(review_store_workdir)
            latest_reviews = latest.get("reviews", [])
            now2 = datetime.utcnow().isoformat() + "Z"
            synced_review: Optional[dict[str, Any]] = None
            for item in latest_reviews:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id", "")).strip() != review_id:
                    continue
                item["resume_status"] = str(result.get("status", "unknown"))
                item["resume_result"] = result
                item["resume_updated_at"] = now2
                synced_review = dict(item)
                break
            latest["reviews"] = latest_reviews
            _save_human_review_queue_for_workdir(latest, review_store_workdir)
            if synced_review is not None:
                await _sync_workflow_human_review_decision(
                    synced_review,
                    decision="approved",
                    workdir=review_store_workdir,
                )

        asyncio.create_task(_runner())
        resume_state = {
            "requested": True,
            "status": "queued",
            "session_id": str(
                (target_review.get("review_data") or {}).get("session_id", "")
            ),
        }
    else:
        target_review["resume_status"] = "not_requested"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_human_review_queue_for_workdir(store, review_store_workdir)

    review_workspace = (
        (target_review.get("review_data") or {}).get("workspace")
        if isinstance(target_review.get("review_data"), dict)
        else {}
    )
    task_id = str(
        (review_workspace or {}).get("task_id")
        or target_review.get("task_id")
        or ""
    ).strip()
    if task_id:
        review_decision = {
            "status": "approved",
            "review_identifier": str(target_review.get("identifier", "") or ""),
            "comments": str(payload.comments or ""),
            "resume_instruction": str(payload.resume_instruction or ""),
            "execution_disposition": execution_disposition,
            "override_applied": bool(approved_override),
            "resume_requested": resume_requested,
            "resume_status": str(resume_state.get("status", "") or ""),
            "reviewed_at": now,
        }
        try:
            SharedTracker.patch_task(
                task_id=task_id,
                workspace_patch={
                    "review_lineage": {
                        "identifier": str(target_review.get("identifier", "") or ""),
                        "status": "approved",
                        "reason": str(target_review.get("reason", "") or ""),
                        "reviewed_at": now,
                        "resume_status": str(resume_state.get("status", "") or ""),
                        "execution_disposition": execution_disposition,
                    },
                    "review_decision": review_decision,
                    "action_policy": build_action_policy(
                        lifecycle_status=str((review_workspace or {}).get("status", "") or "review_pending"),
                        job_id=str((review_workspace or {}).get("job_id", "") or ""),
                        has_submit_input=bool((review_workspace or {}).get("input_snapshot")),
                        resume_supported=bool((review_workspace or {}).get("resume_supported", False)),
                        resume_command=str((review_workspace or {}).get("resume_command", "") or ""),
                        review_status="approved",
                    ),
                },
                metadata_patch={"review_decision": review_decision},
                event_kind="review_approved",
                source="human_review",
                workdir=review_store_workdir,
            )
        except Exception:
            pass

    return {
        "status": "approved",
        "review": target_review,
        "message": "Review approved",
        "resume": resume_state,
    }


@router.post("/api/human-review/reject")
async def reject_human_review(
    payload: HumanReviewRejectRequest,
    request: Request,
):
    issue_id = (payload.issue_id or "").strip()
    issue_identifier = (payload.issue_identifier or "").strip()
    if not issue_id and not issue_identifier:
        raise HTTPException(400, "id or identifier is required")

    if not payload.reason:
        raise HTTPException(400, "rejection reason is required")

    found = _find_human_review_store_for_update(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
    )
    if found is None:
        not_found_key = issue_id or issue_identifier
        raise HTTPException(404, f"review not found: {not_found_key}")
    review_store_workdir, store, reviews, target_review = found
    _require_human_review_access(
        target_review,
        request,
        fallback_workdir=review_store_workdir,
    )

    current_status = str(target_review.get("status", "")).lower()
    if current_status != "pending_review":
        raise HTTPException(400, f"review is not pending (current: {current_status})")

    now = datetime.utcnow().isoformat() + "Z"
    target_review["status"] = "rejected"
    target_review["reviewed_at"] = now
    target_review["updated_at"] = now
    target_review["rejection_reason"] = payload.reason
    target_review["comments"] = payload.comments
    target_review["resume_instruction"] = payload.resume_instruction
    target_review["reviewer"] = "human"

    store["reviews"] = reviews
    _save_human_review_queue_for_workdir(store, review_store_workdir)
    _transition_human_review_runtime(
        target_review,
        status="cancelled",
        phase="rejected",
        workdir=review_store_workdir,
    )

    resume_requested = payload.resume_after_rejection is not False
    resume_state: dict[str, Any] = {
        "requested": resume_requested,
        "status": "not_requested",
    }
    if workflow_runtime.is_workflow_confirmation_review(target_review):
        synced_run = await _sync_workflow_human_review_decision(
            target_review,
            decision="rejected",
            workdir=review_store_workdir,
        )
        target_review["resume_status"] = "completed" if synced_run else "failed"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        target_review["resume_result"] = {
            "status": target_review["resume_status"],
            "workflow_run_id": str(getattr(synced_run, "run_id", "") or ""),
            "workflow_status": str(getattr(synced_run, "status", "") or ""),
        }
        _save_human_review_queue_for_workdir(store, review_store_workdir)
        resume_state = {
            "requested": True,
            "status": target_review["resume_status"],
            "session_id": str(
                (target_review.get("review_data") or {}).get("session_id", "")
            ),
        }
    elif resume_requested:
        target_review["resume_status"] = "queued"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_human_review_queue_for_workdir(store, review_store_workdir)

        review_id = str(target_review.get("id", "")).strip()

        async def _runner():
            result = await _resume_session_after_human_review(
                target_review, decision="rejected"
            )
            latest = _load_human_review_queue_for_workdir(review_store_workdir)
            latest_reviews = latest.get("reviews", [])
            now2 = datetime.utcnow().isoformat() + "Z"
            synced_review: Optional[dict[str, Any]] = None
            for item in latest_reviews:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id", "")).strip() != review_id:
                    continue
                item["resume_status"] = str(result.get("status", "unknown"))
                item["resume_result"] = result
                item["resume_updated_at"] = now2
                synced_review = dict(item)
                break
            latest["reviews"] = latest_reviews
            _save_human_review_queue_for_workdir(latest, review_store_workdir)
            if synced_review is not None:
                await _sync_workflow_human_review_decision(
                    synced_review,
                    decision="rejected",
                    workdir=review_store_workdir,
                )

        asyncio.create_task(_runner())
        resume_state = {
            "requested": True,
            "status": "queued",
            "session_id": str(
                (target_review.get("review_data") or {}).get("session_id", "")
            ),
        }
    else:
        target_review["resume_status"] = "not_requested"
        target_review["resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_human_review_queue_for_workdir(store, review_store_workdir)

    review_workspace = (
        (target_review.get("review_data") or {}).get("workspace")
        if isinstance(target_review.get("review_data"), dict)
        else {}
    )
    task_id = str(
        (review_workspace or {}).get("task_id")
        or target_review.get("task_id")
        or ""
    ).strip()
    if task_id:
        review_decision = {
            "status": "rejected",
            "review_identifier": str(target_review.get("identifier", "") or ""),
            "reason": str(payload.reason or ""),
            "comments": str(payload.comments or ""),
            "resume_instruction": str(payload.resume_instruction or ""),
            "resume_requested": resume_requested,
            "resume_status": str(resume_state.get("status", "") or ""),
            "reviewed_at": now,
        }
        try:
            SharedTracker.patch_task(
                task_id=task_id,
                status="cancelled",
                workspace_patch={
                    "review_lineage": {
                        "identifier": str(target_review.get("identifier", "") or ""),
                        "status": "rejected",
                        "reason": str(target_review.get("reason", "") or ""),
                        "reviewed_at": now,
                        "resume_status": str(resume_state.get("status", "") or ""),
                    },
                    "review_decision": review_decision,
                    "status": "cancelled",
                    "issue_state": "cancelled",
                    "lifecycle_status": "cancelled",
                    "action_policy": build_action_policy(
                        lifecycle_status="cancelled",
                        job_id=str((review_workspace or {}).get("job_id", "") or ""),
                        has_submit_input=bool((review_workspace or {}).get("input_snapshot")),
                        review_status="rejected",
                    ),
                },
                metadata_patch={"review_decision": review_decision},
                event_kind="review_rejected",
                source="human_review",
                error_summary=str(payload.reason or ""),
                workdir=review_store_workdir,
            )
        except Exception:
            pass

    return {
        "status": "rejected",
        "review": target_review,
        "message": "Review rejected",
        "resume": resume_state,
    }


@router.post("/api/human-review/delete")
async def delete_human_review(
    payload: HumanReviewDeleteRequest,
    request: Request,
):
    issue_id = (payload.issue_id or "").strip()
    issue_identifier = (payload.issue_identifier or "").strip()
    if not issue_id and not issue_identifier:
        raise HTTPException(400, "id or identifier is required")

    found = _find_human_review_store_for_update(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
    )
    if found is None:
        not_found_key = issue_id or issue_identifier
        raise HTTPException(404, f"review not found: {not_found_key}")
    review_store_workdir, store, reviews, found_review = found
    _require_human_review_access(
        found_review,
        request,
        fallback_workdir=review_store_workdir,
    )
    if not isinstance(reviews, list):
        reviews = []

    removed: Optional[dict[str, Any]] = None
    kept: list[dict[str, Any]] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        matched = False
        if issue_id and str(review.get("id", "")).strip() == issue_id:
            matched = True
        if (
            issue_identifier
            and str(review.get("identifier", "")).strip() == issue_identifier
        ):
            matched = True

        if matched and removed is None:
            removed = review
            continue
        kept.append(review)

    if removed is None:
        removed = found_review

    store["reviews"] = kept
    _save_human_review_queue_for_workdir(store, review_store_workdir)
    _transition_human_review_runtime(
        removed,
        status="cancelled",
        phase="deleted",
        workdir=review_store_workdir,
    )

    return {
        "status": "deleted",
        "message": "Review deleted",
        "review": removed,
    }


def _check_input_params(tool_args: dict, conditions: dict) -> tuple[bool, str]:
    """Check if input parameters match review conditions"""
    for param, condition in conditions.items():
        if param in tool_args:
            op = condition.get("operator", "eq")
            threshold = condition.get("value")
            value = tool_args[param]

            if op == "gt" and isinstance(value, (int, float)) and value > threshold:
                return True, f"parameter_{param}_exceeds_threshold"
            elif op == "lt" and isinstance(value, (int, float)) and value < threshold:
                return True, f"parameter_{param}_below_threshold"
            elif op == "gte" and isinstance(value, (int, float)) and value >= threshold:
                return True, f"parameter_{param}_gte_threshold"
            elif op == "lte" and isinstance(value, (int, float)) and value <= threshold:
                return True, f"parameter_{param}_lte_threshold"
            elif op == "eq" and str(value) == str(threshold):
                return True, f"parameter_{param}_equals_{threshold}"
            elif op == "neq" and str(value) != str(threshold):
                return True, f"parameter_{param}_not_equals_{threshold}"
    return False, ""


def _check_output_result(output: str, conditions: dict) -> tuple[bool, str]:
    """Check if tool output matches review conditions"""
    for field, condition in conditions.items():
        op = condition.get("operator", "contains")
        threshold = condition.get("value")

        if field == "output_contains" and op == "contains":
            if str(threshold) in output:
                return True, f"output_contains_{threshold}"
        elif field == "output_length" and isinstance(threshold, (int, float)):
            if op == "gt" and len(output) > threshold:
                return True, f"output_length_exceeds_{threshold}"
            elif op == "lt" and len(output) < threshold:
                return True, f"output_length_below_{threshold}"
        elif field == "output_matches" and op == "regex":
            import re

            if re.search(str(threshold), output):
                return True, "output_matches_regex"
    return False, ""


@router.post("/api/human-review/check-required")
async def check_human_review_required(payload: dict[str, Any], request: Request):
    """Check if a tool call requires human review based on config and auto-rules"""
    tool_server = str(payload.get("tool_server", "")).strip().lower()
    tool_name = str(payload.get("tool_name", "")).strip()
    tool_args = payload.get("tool_args", {})
    user_message = str(payload.get("user_message", "") or "").lower()
    tool_output = str(payload.get("tool_output", "") or "")

    config = _load_human_review_config()
    if not config.get("enabled", False):
        return {"requires_review": False, "reason": "human_review_disabled"}

    allowed_workdir_root = ""
    if auth_required():
        allowed_workdir_root = _user_workdirs_root(
            _workspace_context_from_request(request)
        ).as_posix()
    approved = _has_approved_review_for_call(
        tool_server,
        tool_name,
        tool_args,
        allowed_workdir_root=allowed_workdir_root,
    )
    if approved is not None:
        approved_override = approved.get("approved_tool_args_override")
        if not isinstance(approved_override, dict):
            approved_override = None
        return {
            "requires_review": False,
            "reason": "approved_review_reused",
            "trigger": "approved_review_match",
            "approved_review_identifier": str(approved.get("identifier", "") or ""),
            "approved_comments": str(approved.get("comments", "") or ""),
            "approved_tool_args_override": approved_override,
        }

    required_tools = config.get("required_review_tools", {})
    auto_rules = config.get("auto_review_rules", {})
    output_rules = config.get("output_based_review", {})

    # Check 1: Input parameter based review
    if tool_server in required_tools:
        tool_config = required_tools[tool_server]
        if tool_name in tool_config.get("tools", []):
            auto_conditions = tool_config.get("auto_review_conditions", {})

            if auto_conditions:
                match, trigger = _check_input_params(tool_args, auto_conditions)
                if match:
                    return {
                        "requires_review": True,
                        "reason": tool_config.get("reason", "需要人工确认"),
                        "trigger": trigger,
                        "check_type": "input_param",
                    }
            else:
                return {
                    "requires_review": True,
                    "reason": tool_config.get("reason", "需要人工确认"),
                    "trigger": "tool_in_required_list",
                    "check_type": "input_param",
                }

    # Check 2: Output based review
    if output_rules.get("enabled", False) and tool_output:
        output_tool_rules = output_rules.get("rules", {})
        if tool_server in output_tool_rules:
            tool_rule = output_tool_rules[tool_server]
            if tool_name in tool_rule.get("tools", []):
                conditions = tool_rule.get("conditions", {})
                match, trigger = _check_output_result(tool_output, conditions)
                if match:
                    return {
                        "requires_review": True,
                        "reason": "输出结果需要人工确认",
                        "trigger": trigger,
                        "check_type": "output_result",
                    }

    # Check 3: User request keywords
    if auto_rules.get("user_request_keywords", {}).get("enabled", False):
        keywords = auto_rules["user_request_keywords"].get("keywords", [])
        for keyword in keywords:
            if keyword.lower() in user_message:
                return {
                    "requires_review": True,
                    "reason": f"User message contains review keyword: {keyword}",
                    "trigger": "user_keyword_match",
                    "check_type": "user_keyword",
                }

    return {"requires_review": False, "reason": "no_review_required"}
