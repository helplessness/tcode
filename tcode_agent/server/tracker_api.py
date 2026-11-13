"""Tracker task claims, leases and execution reports.

Routes retain their original paths and are registered by app.py.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ..runtime.shared_tracker import SharedTracker
from ..team.coordinator import cancel_team_background_tasks
from . import workdirs as workdir_utils
from ._shared import (
    _require_tracker_api_auth,
    _shared_tracker_db_path_text,
)
from .workspace_scope import (
    _resolve_user_workdir_path,
    _session_owner_kwargs,
    _workspace_context_from_request,
)
from .schemas import (
    TrackerAttachJobRefRequest,
    TrackerCancelRequest,
    TrackerClaimNextRequest,
    TrackerCompleteRequest,
    TrackerExecutionReportRequest,
    TrackerFailRequest,
    TrackerHeartbeatRequest,
    TrackerTaskCreateRequest,
)
from .state import ServerState

router = APIRouter()


def _tracker_request_scope(
    request: Request,
    *,
    require_active: bool = True,
) -> tuple[workdir_utils.WorkspaceContext, str]:
    workspace_context = _workspace_context_from_request(request)
    if require_active:
        workdir_utils.assert_workspace_active(
            ServerState.get_workdir(),
            workspace_context,
        )
    tracker_workdir = _resolve_user_workdir_path(
        workspace_context.workspace_id, workspace_context
    ).as_posix()
    return workspace_context, tracker_workdir


def _tracker_task_generation(task: dict[str, Any]) -> int:
    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    try:
        return int(workspace.get("workspace_generation") or 0)
    except (TypeError, ValueError):
        return 0


def _assert_tracker_task_generation(
    task: dict[str, Any],
    workspace_context: workdir_utils.WorkspaceContext,
    tracker_workdir: str,
    *,
    bind_legacy: bool = True,
) -> dict[str, Any]:
    """Reject writes from a task created before the current purge fence."""

    current = workdir_utils.workspace_generation(
        ServerState.get_workdir(), workspace_context
    )
    expected = _tracker_task_generation(task)
    if expected and expected != current:
        raise HTTPException(
            409,
            f"stale workspace generation: task={expected}, current={current}",
        )
    if not expected and bind_legacy:
        patched = SharedTracker.patch_task(
            task_id=str(task.get("task_id") or task.get("id") or ""),
            workspace_patch={"workspace_generation": current},
            event_kind="workspace_generation_bound",
            source="workspace_lifecycle",
            workdir=tracker_workdir,
        )
        if isinstance(patched, dict):
            return patched
    return task


@router.post("/api/tracker/tasks")
async def create_tracker_task(request: Request, payload: TrackerTaskCreateRequest):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    task = payload.task if isinstance(payload.task, dict) else {}
    if not task:
        raise HTTPException(400, "task is required")
    task = SharedTracker.bind_workspace_owner(
        task,
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
        workspace_id=workspace_context.workspace_id,
        workspace_generation=workdir_utils.workspace_generation(
            ServerState.get_workdir(), workspace_context
        ),
    )
    record = SharedTracker.upsert_issue(
        task,
        tracker_workdir,
        source=str(payload.source or "api"),
        append_event=bool(payload.append_event if payload.append_event is not None else True),
    )
    task_payload = SharedTracker.get_task(
        task_id=str(record.get("id", "") or ""), workdir=tracker_workdir
    )
    return {
        "tracker": "sqlite",
        "db_path": _shared_tracker_db_path_text(tracker_workdir),
        "task": task_payload or record,
    }


@router.post("/api/tracker/tasks/by-identifier/{identifier}/report-execution")
async def report_tracker_execution(
    request: Request,
    identifier: str,
    payload: TrackerExecutionReportRequest,
):
    """Trusted execution callback used by Symphony's adapter/runner.

    The Symphony JSON file is only a disposable consumer projection. Execution
    state must enter the control plane through this authenticated endpoint.
    """

    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    task = SharedTracker.get_task(identifier=identifier, workdir=tracker_workdir)
    if task is None:
        raise HTTPException(404, f"tracker task not found: {identifier}")
    task = _assert_tracker_task_generation(
        task, workspace_context, tracker_workdir
    )
    status = str(payload.status or "").strip().lower()
    if status not in {"running", "completed", "failed", "cancelled"}:
        raise HTTPException(400, "invalid execution status")
    updated = SharedTracker.record_result(
        task_id=str(task.get("task_id") or task.get("id") or ""),
        identifier=identifier,
        status=status,
        source=str(payload.source or "symphony_adapter").strip(),
        result_summary=str(payload.result_summary or ""),
        error_summary=str(payload.error_summary or ""),
        workspace_patch=(
            payload.workspace_patch
            if isinstance(payload.workspace_patch, dict)
            else None
        ),
        metadata_patch=(
            payload.metadata_patch
            if isinstance(payload.metadata_patch, dict)
            else None
        ),
        job_ref=payload.job_ref if isinstance(payload.job_ref, dict) else None,
        workdir=tracker_workdir,
    )
    if updated is None:
        raise HTTPException(404, f"tracker task not found: {identifier}")
    return {"tracker": "sqlite", "task": updated}


@router.get("/api/tracker/tasks")
async def list_tracker_tasks(
    request: Request,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 100,
):
    _require_tracker_api_auth(request, allow_user=True)
    _, tracker_workdir = _tracker_request_scope(request)
    tasks = SharedTracker.list_tasks(
        tracker_workdir,
        kind=kind,
        status=status,
        channel=channel,
        limit=max(1, min(1000, int(limit))),
    )
    return {
        "tracker": "sqlite",
        "db_path": _shared_tracker_db_path_text(tracker_workdir),
        "count": len(tasks),
        "tasks": tasks,
    }


@router.get("/api/tracker/tasks/{task_id}")
async def get_tracker_task(request: Request, task_id: str):
    _require_tracker_api_auth(request, allow_user=True)
    _, tracker_workdir = _tracker_request_scope(request)
    task = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if task is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    return {
        "tracker": "sqlite",
        "db_path": _shared_tracker_db_path_text(tracker_workdir),
        "task": task,
    }


@router.get("/api/tracker/tasks/{task_id}/events")
async def get_tracker_task_events(request: Request, task_id: str):
    _require_tracker_api_auth(request, allow_user=True)
    _, tracker_workdir = _tracker_request_scope(request)
    task = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if task is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    events = SharedTracker.list_task_events(task_id, tracker_workdir)
    return {
        "task_id": task_id,
        "count": len(events),
        "events": events,
    }


@router.post("/api/tracker/claim-next")
async def claim_next_tracker_task(request: Request, payload: TrackerClaimNextRequest):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(
        request,
        require_active=False,
    )
    workspace_state = (
        workdir_utils.workspace_state(
            ServerState.get_workdir(),
            workspace_context,
        )
        or {}
    )
    workspace_status = str(workspace_state.get("status") or "active").strip().lower()
    if workdir_utils.workspace_is_hidden(
        ServerState.get_workdir(),
        workspace_context,
    ):
        return {
            "task": None,
            "db_path": "",
            "scope_available": False,
            "workspace_status": workspace_status,
            "retry_after_seconds": 60,
        }
    try:
        task = None
        for _ in range(20):
            candidate = SharedTracker.claim_next(
                worker_id=payload.worker_id,
                channel=payload.channel,
                lease_seconds=max(10, int(payload.lease_seconds or 30)),
                allowed_servers=list(payload.allowed_servers or []),
                workdir=tracker_workdir,
            )
            if candidate is None:
                break
            try:
                task = _assert_tracker_task_generation(
                    candidate, workspace_context, tracker_workdir
                )
                break
            except HTTPException:
                SharedTracker.record_result(
                    task_id=str(candidate.get("task_id") or candidate.get("id") or ""),
                    status="cancelled",
                    source="workspace_generation_fence",
                    result_summary="cancelled because workspace generation is stale",
                    workdir=tracker_workdir,
                )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task": task,
        "db_path": _shared_tracker_db_path_text(tracker_workdir),
        "scope_available": True,
        "workspace_status": workspace_status,
    }


@router.post("/api/tracker/tasks/{task_id}/heartbeat")
async def heartbeat_tracker_task(
    request: Request, task_id: str, payload: TrackerHeartbeatRequest
):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    existing = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if existing is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    _assert_tracker_task_generation(existing, workspace_context, tracker_workdir)
    try:
        task = SharedTracker.heartbeat(
            task_id=task_id,
            claim_token=payload.claim_token,
            worker_id=payload.worker_id,
            lease_seconds=max(10, int(payload.lease_seconds or 30)),
            status=str(payload.status or "").strip(),
            workspace_patch=payload.workspace_patch if isinstance(payload.workspace_patch, dict) else None,
            result_summary=str(payload.result_summary or ""),
            error_summary=str(payload.error_summary or ""),
            workdir=tracker_workdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task": SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
        or task
    }


@router.post("/api/tracker/tasks/{task_id}/attach-job-ref")
async def attach_tracker_job_ref(
    request: Request, task_id: str, payload: TrackerAttachJobRefRequest
):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    existing = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if existing is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    _assert_tracker_task_generation(existing, workspace_context, tracker_workdir)
    try:
        task = SharedTracker.attach_job_ref(
            task_id=task_id,
            claim_token=payload.claim_token,
            job_ref=payload.job_ref if isinstance(payload.job_ref, dict) else {},
            workspace_patch=payload.workspace_patch if isinstance(payload.workspace_patch, dict) else None,
            worker_id=str(payload.worker_id or "").strip(),
            workdir=tracker_workdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task": SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
        or task
    }


@router.post("/api/tracker/tasks/{task_id}/complete")
async def complete_tracker_task(
    request: Request, task_id: str, payload: TrackerCompleteRequest
):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    existing = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if existing is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    _assert_tracker_task_generation(existing, workspace_context, tracker_workdir)
    try:
        task = SharedTracker.complete_task(
            task_id=task_id,
            claim_token=payload.claim_token,
            result_summary=str(payload.result_summary or ""),
            workspace_patch=payload.workspace_patch if isinstance(payload.workspace_patch, dict) else None,
            job_ref=payload.job_ref if isinstance(payload.job_ref, dict) else None,
            workdir=tracker_workdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task": SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
        or task
    }


@router.post("/api/tracker/tasks/{task_id}/fail")
async def fail_tracker_task(
    request: Request, task_id: str, payload: TrackerFailRequest
):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    existing = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if existing is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    _assert_tracker_task_generation(existing, workspace_context, tracker_workdir)
    try:
        task = SharedTracker.fail_task(
            task_id=task_id,
            claim_token=payload.claim_token,
            error_summary=str(payload.error_summary or ""),
            workspace_patch=payload.workspace_patch if isinstance(payload.workspace_patch, dict) else None,
            job_ref=payload.job_ref if isinstance(payload.job_ref, dict) else None,
            workdir=tracker_workdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task": SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
        or task
    }


@router.post("/api/tracker/tasks/{task_id}/cancel")
async def cancel_tracker_task(
    request: Request, task_id: str, payload: TrackerCancelRequest
):
    _require_tracker_api_auth(request)
    workspace_context, tracker_workdir = _tracker_request_scope(request)
    existing = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir)
    if existing is None:
        raise HTTPException(404, f"tracker task not found: {task_id}")
    _assert_tracker_task_generation(existing, workspace_context, tracker_workdir)
    try:
        task = SharedTracker.cancel_task(
            task_id=task_id,
            claim_token=str(payload.claim_token or "").strip(),
            result_summary=str(payload.result_summary or ""),
            workspace_patch=payload.workspace_patch if isinstance(payload.workspace_patch, dict) else None,
            source=str(payload.source or "api"),
            workdir=tracker_workdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # 取消任务后，同步取消关联的 agent 会话，
    # 这样 auto-resume 循环才能检测到 cancelled 状态并注入结果通知 agent
    task_detail = SharedTracker.get_task(task_id=task_id, workdir=tracker_workdir) or task
    session_id = str(task_detail.get("session_id", "") or "").strip()
    if session_id:
        workspace_context = _workspace_context_from_request(request)
        session_owner = _session_owner_kwargs(workspace_context)
        try:
            await cancel_team_background_tasks(session_id)
            await ServerState.cancel_session(session_id)
        except Exception:
            pass  # 会话取消失败不阻塞任务取消响应

    return {"task": task_detail}
