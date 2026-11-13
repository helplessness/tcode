"""Symphony issue lifecycle and execution callbacks.

Routes retain their original paths and are registered by app.py.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..runtime import TaskRuntimeStore
from ..runtime.shared_tracker import SharedTracker
from . import workdirs as workdir_utils
from ._shared import (
    _cancel_active_issue,
    _issue_managed_workspace_context,
    _load_symphony_memory_issue_store,
    _load_visible_symphony_issues,
    _locate_deleted_symphony_issue,
    _locate_symphony_issue_store_entry,
    _resolve_local_issue_store_workdir,
    _save_symphony_memory_issue_store,
    _trigger_symphony_refresh,
)
from .workspace_scope import (
    _resolve_session_workdir,
    _workspace_context_from_request,
)
from .issue_records import (
    _is_terminal_issue_state,
    _normalize_issue_state_text,
)
from .linear_client import (
    _linear_create_issue,
    _linear_project_info,
    _linear_project_slug,
)
from .job_workspace import (
    _build_issue_workspace_summary,
    _build_job_timeline,
    _normalize_lifecycle_status,
)
from .schemas import (
    SymphonyIssueRequest,
    SymphonyJobCallbackRequest,
    SymphonyLocalIssueBatchDeleteRequest,
    SymphonyLocalIssueDeleteRequest,
    SymphonyLocalIssueRequest,
    SymphonyLocalIssueUpdateRequest,
)
from .state import ServerState

router = APIRouter()


@router.post("/api/symphony/issue")
async def create_symphony_issue(request: SymphonyIssueRequest):
    description = request.description.strip()
    if not description:
        raise HTTPException(400, "description is required")

    title = (request.title or "").strip()
    if not title:
        title = description.splitlines()[0][:120]

    slug = (request.project_slug or _linear_project_slug() or "").strip()
    if not slug:
        raise HTTPException(
            400, "project_slug is required (or set LINEAR_PROJECT_SLUG)"
        )

    project_info = await _linear_project_info(slug)
    issue = await _linear_create_issue(
        project_info["project_id"],
        project_info["team_id"],
        title,
        description,
    )
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "url": issue.get("url"),
    }


@router.post("/api/symphony/local-issue")
async def create_symphony_local_issue(
    payload: SymphonyLocalIssueRequest,
    request: Request,
    session_id: str = Query(default=""),
):
    context = _workspace_context_from_request(request)
    description = payload.description.strip()
    if not description:
        raise HTTPException(400, "description is required")

    title = (payload.title or "").strip()
    if not title:
        title = description.splitlines()[0][:120]

    issue_id = f"local-{uuid.uuid4().hex[:10]}"
    created_at = datetime.now(timezone.utc)
    identifier = f"LOCAL-{created_at.strftime('%Y%m%d%H%M%S%f')}"
    now = created_at.isoformat().replace("+00:00", "Z")

    issue_record = {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "description": description,
        "state": "Todo",
        "execution_channel": "symphony",
        "labels": ["local", "symphony"],
        "created_at": now,
        "updated_at": now,
    }

    issue_store_workdir = await _resolve_local_issue_store_workdir(session_id, context)
    normalized_session_id = str(session_id or "").strip()
    if normalized_session_id:
        issue_record["resume_data"] = {
            "session_id": normalized_session_id,
        }
    issue_record["workspace"] = {
        "session_id": normalized_session_id,
        "parent_session_id": "",
        "workdir": issue_store_workdir,
        "issue_identifier": identifier,
    }
    issue_record = SharedTracker.bind_workspace_owner(
        issue_record,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
    )
    store = _load_symphony_memory_issue_store(issue_store_workdir)
    issues = store.get("issues", [])
    if not isinstance(issues, list):
        issues = []
    issues.append(issue_record)
    store["issues"] = issues
    _save_symphony_memory_issue_store(store, issue_store_workdir)

    refresh_ok = await _trigger_symphony_refresh()

    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "status": "queued",
        "tracker": "sqlite",
        "refresh_triggered": refresh_ok,
    }


async def _reconcile_terminal_symphony_runtimes(
    issues: list[dict[str, Any]],
    *,
    fallback_workdir: str = "",
) -> None:
    groups: dict[tuple[str, str, str], dict[str, set[str]]] = {}
    resolved_workdirs: dict[str, str] = {}
    for issue in issues:
        state = str(issue.get("state", "") or "").strip().lower()
        status = {
            "done": "collecting",
            "closed": "collecting",
            "completed": "collecting",
            "failed": "failed",
            "error": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(state)
        if not status:
            continue

        workspace = (
            issue.get("workspace")
            if isinstance(issue.get("workspace"), dict)
            else {}
        )
        resume_data = (
            issue.get("resume_data")
            if isinstance(issue.get("resume_data"), dict)
            else {}
        )
        session_id = str(
            workspace.get("session_id")
            or resume_data.get("session_id")
            or ""
        ).strip()
        runtime_workdir = str(
            workspace.get("workdir") or fallback_workdir or ""
        ).strip()
        if not runtime_workdir and session_id:
            if session_id not in resolved_workdirs:
                resolved_workdirs[session_id] = await _resolve_session_workdir(session_id)
            runtime_workdir = resolved_workdirs[session_id]

        group = groups.setdefault(
            (runtime_workdir, session_id, status),
            {"references": set(), "states": set()},
        )
        for value in (issue.get("id"), issue.get("identifier")):
            text = str(value or "").strip()
            if text:
                group["references"].add(text)
        group["states"].add(state)

    for (runtime_workdir, session_id, status), group in groups.items():
        TaskRuntimeStore.transition_linked_open(
            group["references"],
            status=status,
            phase="terminal_issue_observed",
            source="symphony_issue_reconcile",
            session_id=session_id,
            workdir=runtime_workdir or None,
            metadata={"issue_states": sorted(group["states"])},
        )




@router.get("/api/symphony/local-issues")
async def list_symphony_local_issues(
    request: Request,
    limit: int = 50,
    session_id: str = Query(default=""),
    filter_session_ids: str = Query(default=""),
    workspace_id: str = Query(default=""),
    q: str = "",
):
    context = _workspace_context_from_request(request, workspace_id=workspace_id)
    session_filters = [
        item.strip()
        for item in str(filter_session_ids or "").split(",")
        if item.strip()
    ]
    selected, total_count, _issue_store_paths = await _load_visible_symphony_issues(
        session_id=session_id,
        filter_session_ids=session_filters,
        workspace_id=workspace_id,
        limit=limit,
        context=context,
        **({"query": q} if q else {}),
    )
    enriched: list[dict[str, Any]] = []
    for issue in selected:
        if not isinstance(issue, dict):
            continue
        issue_obj = dict(issue)
        workspace = issue_obj.get("workspace")
        if not isinstance(workspace, dict):
            workspace = _build_issue_workspace_summary(issue_obj)
            issue_obj["workspace"] = workspace
        job_id = str(workspace.get("job_id", "") or "").strip()
        if job_id:
            workspace["lifecycle_status"] = _normalize_lifecycle_status(workspace, issue_obj)
            issue_obj["timeline"] = _build_job_timeline(job_id, workspace, issue_obj)
        else:
            issue_obj["timeline"] = []
        enriched.append(issue_obj)

    await _reconcile_terminal_symphony_runtimes(enriched)

    return {
        "count": len(enriched),
        "total": total_count,
        "filters": {
            "session_ids": session_filters,
            "workspace_id": str(workspace_id or "").strip(),
        },
        "issues": enriched,
    }


@router.patch("/api/symphony/local-issue")
async def update_symphony_local_issue(
    payload: SymphonyLocalIssueUpdateRequest,
    request: Request,
):
    context = _workspace_context_from_request(request)
    issue_id = (payload.id or "").strip()
    issue_identifier = (payload.identifier or "").strip()
    if not issue_id and not issue_identifier:
        raise HTTPException(400, "id or identifier is required")

    located = await _locate_symphony_issue_store_entry(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
        context=context,
        owner_wide=True,
    )
    if located is None:
        not_found_key = issue_id or issue_identifier
        raise HTTPException(404, f"local issue not found: {not_found_key}")
    issue_store_workdir, store, issues, _, target_issue = located

    current_state = str(target_issue.get("state", "")).strip()
    next_state = current_state
    changed = False
    cancellation_result: Optional[dict[str, Any]] = None
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(400, "title cannot be empty")
        target_issue["title"] = title
        changed = True

    if payload.description is not None:
        description = payload.description.strip()
        if not description:
            raise HTTPException(400, "description cannot be empty")
        target_issue["description"] = description
        changed = True

    if payload.state is not None:
        state = payload.state.strip()
        if not state:
            raise HTTPException(400, "state cannot be empty")
        cancelling = _normalize_issue_state_text(state) in {"cancelled", "canceled"}
        if cancelling and _is_terminal_issue_state(current_state):
            raise HTTPException(
                409,
                f"任务已经处于终态 {current_state or 'unknown'}，取消请求未生效。",
            )
        if (
            cancelling
            and not _is_terminal_issue_state(current_state)
        ):
            cancellation_result = await _cancel_active_issue(target_issue, workdir=issue_store_workdir)
        else:
            target_issue["state"] = state
        next_state = str(target_issue["state"])
        changed = True

    if payload.labels is not None:
        labels = [str(label).strip() for label in payload.labels if str(label).strip()]
        target_issue["labels"] = labels
        changed = True

    if not changed:
        raise HTTPException(400, "no updatable fields provided")

    now = datetime.utcnow().isoformat() + "Z"
    target_issue["updated_at"] = now

    store["issues"] = issues
    _save_symphony_memory_issue_store(store, issue_store_workdir)

    task_id = str(target_issue.get("id", "") or "").strip()
    if task_id and payload.state is not None and str(current_state).strip() != str(next_state).strip():
        event_kind = {
            "todo": "queued",
            "in progress": "started",
            "done": "completed",
            "closed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(str(next_state).strip().lower(), "updated")
        try:
            SharedTracker.append_event(
                task_id=task_id,
                kind=event_kind,
                source="app",
                payload={
                    "previous_state": current_state,
                    "next_state": next_state,
                    "identifier": str(target_issue.get("identifier", "") or ""),
                },
                workdir=issue_store_workdir,
            )
        except Exception:
            pass

    refresh_ok = await _trigger_symphony_refresh()

    return {
        "status": "updated",
        "refresh_triggered": refresh_ok,
        "issue": target_issue,
        "cancellation": cancellation_result,
    }


async def _delete_symphony_local_issue_for_owner(
    payload: SymphonyLocalIssueDeleteRequest,
    *,
    context: workdir_utils.WorkspaceContext,
    cancel_active: bool = False,
) -> dict[str, Any]:
    """Cancel when requested, then tombstone one owner-scoped local issue."""

    issue_id = (payload.id or "").strip()
    issue_identifier = (payload.identifier or "").strip()
    if not issue_id and not issue_identifier:
        raise HTTPException(400, "id or identifier is required")

    located = await _locate_symphony_issue_store_entry(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
        context=context,
        owner_wide=True,
    )
    if located is None:
        deleted = await _locate_deleted_symphony_issue(
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            context=context,
        )
        if deleted is not None:
            return {
                "status": "already_deleted",
                "issue": {
                    "id": str(deleted.get("id") or deleted.get("task_id") or ""),
                    "identifier": str(deleted.get("identifier") or ""),
                    "state": str(deleted.get("state") or "Cancelled"),
                    "deleted_at": str(deleted.get("deleted_at") or ""),
                },
            }
        not_found_key = issue_id or issue_identifier
        raise HTTPException(404, f"local issue not found: {not_found_key}")
    issue_store_workdir, store, issues, target_index, target_issue = located

    issue_workspace_context = _issue_managed_workspace_context(
        target_issue,
        store_workdir=issue_store_workdir,
    )
    if issue_workspace_context is not None:
        workspace_state = workdir_utils.workspace_state(
            ServerState.get_workdir(),
            issue_workspace_context,
        ) or {}
        workspace_status = str(workspace_state.get("status") or "active").strip().lower()
        if workspace_status in {"deleting", "purging"}:
            action_hint = (
                "请先在 Workspace 回收站中恢复或重试删除该 Workspace"
                if workspace_status == "deleting"
                else "请等待 Workspace 永久删除完成"
            )
            raise HTTPException(
                409,
                f"Workspace“{issue_workspace_context.workspace_id}”处于 "
                f"{workspace_status} 状态；{action_hint}，再删除异步任务。",
            )

    current_state = str(target_issue.get("state", "")).strip().lower()
    cancellation_result: Optional[dict[str, Any]] = None
    if payload.forget_unresolved_submission:
        # Validate and tombstone atomically before touching the materialized
        # store: removing from that store would itself mark the task deleted.
        try:
            removed = SharedTracker.delete_issue(
                task_id=str(target_issue.get("id") or ""),
                identifier=str(target_issue.get("identifier") or ""),
                workdir=issue_store_workdir,
                forget_unresolved_submission=True,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "deleted", "issue": removed, "cancellation": None, "local_only": True}
    if not _is_terminal_issue_state(current_state):
        if not cancel_active:
            raise HTTPException(
                400,
                "only terminal issues can be deleted manually; cancel active issues first",
            )
        cancellation_result = await _cancel_active_issue(target_issue, workdir=issue_store_workdir)
        if cancellation_result.get("pending"):
            raise HTTPException(409, "取消请求已保存，后台正在确认远程停止；确认前不能删除任务记录。")

    removed = issues.pop(target_index)
    store["issues"] = issues
    _save_symphony_memory_issue_store(store, issue_store_workdir)
    try:
        SharedTracker.delete_issue(
            task_id=str(removed.get("id", "") or ""),
            identifier=str(removed.get("identifier", "") or ""),
            workdir=issue_store_workdir,
        )
    except Exception as exc:
        # Tracker is the authoritative projection source. Restore the visible
        # record when tombstoning fails so a successful response cannot be
        # followed by the supposedly deleted task reappearing on the next poll.
        issues.insert(target_index, removed)
        store["issues"] = issues
        try:
            _save_symphony_memory_issue_store(store, issue_store_workdir)
        except Exception:
            pass
        raise HTTPException(500, f"failed to persist issue deletion: {exc}") from exc

    return {
        "status": "deleted",
        "issue": removed,
        "cancellation": cancellation_result,
    }


@router.delete("/api/symphony/local-issue")
async def delete_symphony_local_issue(
    payload: SymphonyLocalIssueDeleteRequest,
    request: Request,
):
    return await _delete_symphony_local_issue_for_owner(
        payload,
        context=_workspace_context_from_request(request),
    )


@router.post("/api/symphony/local-issues/batch-delete")
async def batch_delete_symphony_local_issues(
    payload: SymphonyLocalIssueBatchDeleteRequest,
    request: Request,
):
    """Delete multiple visible issues; active items are cancelled first."""

    references = list(payload.issues or [])
    if not references:
        raise HTTPException(400, "at least one issue is required")
    if len(references) > 100:
        raise HTTPException(400, "at most 100 issues can be deleted at once")

    context = _workspace_context_from_request(request)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for reference in references:
        issue_id = str(reference.id or "").strip()
        identifier = str(reference.identifier or "").strip()
        key = (issue_id, identifier)
        if not issue_id and not identifier:
            results.append(
                {
                    "id": "",
                    "identifier": "",
                    "status": "failed",
                    "status_code": 400,
                    "error": "id or identifier is required",
                }
            )
            continue
        if key in seen:
            results.append(
                {
                    "id": issue_id,
                    "identifier": identifier,
                    "status": "duplicate",
                }
            )
            continue
        seen.add(key)
        try:
            result = await _delete_symphony_local_issue_for_owner(
                reference,
                context=context,
                cancel_active=bool(payload.cancel_active),
            )
            issue = result.get("issue") if isinstance(result.get("issue"), dict) else {}
            results.append(
                {
                    "id": str(issue.get("id") or issue_id),
                    "identifier": str(issue.get("identifier") or identifier),
                    "status": str(result.get("status") or "deleted"),
                    "cancellation": result.get("cancellation"),
                }
            )
        except HTTPException as exc:
            results.append(
                {
                    "id": issue_id,
                    "identifier": identifier,
                    "status": "failed",
                    "status_code": int(exc.status_code),
                    "error": str(exc.detail),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": issue_id,
                    "identifier": identifier,
                    "status": "failed",
                    "status_code": 500,
                    "error": str(exc),
                }
            )

    deleted_statuses = {"deleted", "already_deleted", "duplicate"}
    deleted_count = sum(
        1 for item in results if str(item.get("status") or "") in deleted_statuses
    )
    failed_count = sum(
        1 for item in results if str(item.get("status") or "") == "failed"
    )
    return {
        "status": (
            "deleted"
            if failed_count == 0
            else "failed"
            if deleted_count == 0
            else "partial"
        ),
        "requested_count": len(references),
        "deleted_count": deleted_count,
        "failed_count": failed_count,
        "results": results,
    }


@router.post("/api/symphony/job-callback")
async def symphony_job_callback(payload: SymphonyJobCallbackRequest):
    """Callback endpoint for Symphony's after_run hook.

    When Symphony completes a routed long-running task, its after_run hook
    can POST to this endpoint to immediately inject the result into the
    originating agent session. Falls back to the periodic watcher if this
    endpoint is not called.
    """
    from ..runtime.async_watcher import _check_completed_jobs as force_check
    from ..runtime.async_watcher import is_injected

    identifier = str(payload.identifier or "").strip()
    if not identifier:
        return {"status": "ignored", "reason": "missing identifier"}

    if is_injected(identifier):
        return {"status": "already_injected", "identifier": identifier}

    # Force an immediate check for this specific job
    await force_check(
        workdir=ServerState.get_workdir(),
        agent_event_emitter=None,
    )

    injected = is_injected(identifier)
    return {
        "status": "injected" if injected else "pending",
        "identifier": identifier,
        "injected": injected,
        "note": "Result will be injected on next poll cycle if not already done." if not injected else "Result successfully injected into agent session.",
    }
