from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from ..scheduled_tasks import (
    ScheduleValidationError,
    compute_next_run,
    get_scheduled_task_service,
    get_scheduled_task_store,
    normalize_task_spec,
    parse_natural_language,
)
from .auth import CurrentUserContext, require_current_user


router = APIRouter(tags=["scheduled-tasks"])


def _timestamp_iso(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _serialize_task(task: dict[str, Any]) -> dict[str, Any]:
    item = dict(task or {})
    for key in ("next_run_at", "last_run_at", "created_at", "updated_at", "deleted_at"):
        item[f"{key}_iso"] = _timestamp_iso(item.get(key))
    next_value = item.get("next_run_at")
    if next_value is not None:
        try:
            local = datetime.fromtimestamp(float(next_value), timezone.utc).astimezone(
                ZoneInfo(str(item.get("timezone") or "Asia/Shanghai"))
            )
            item["next_run_at_local"] = local.isoformat()
        except Exception:
            item["next_run_at_local"] = item.get("next_run_at_iso")
    return item


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    item = dict(run or {})
    for key in ("scheduled_for", "started_at", "finished_at"):
        item[f"{key}_iso"] = _timestamp_iso(item.get(key))
    return item


def _serialize_notification(notification: dict[str, Any]) -> dict[str, Any]:
    item = dict(notification or {})
    item["created_at_iso"] = _timestamp_iso(item.get("created_at"))
    return item


def _admin_user(request: Request) -> CurrentUserContext:
    user = require_current_user(request)
    if user.role not in {"owner", "admin"}:
        raise HTTPException(403, "Tenant admin role is required")
    return user


def _task_or_404(
    task_id: str,
    user: CurrentUserContext,
    *,
    admin: bool = False,
) -> dict[str, Any]:
    task = get_scheduled_task_store().get_task(
        task_id,
        tenant_id=user.tenant_id,
        user_id=None if admin else user.user_id,
    )
    if task is None:
        raise HTTPException(404, "Scheduled task not found")
    return task


def _draft_response(draft: dict[str, Any]) -> dict[str, Any]:
    preview = _serialize_task(dict(draft.get("spec") or {}))
    return {
        "status": "requires_confirmation",
        "requires_confirmation": True,
        "confirmation_token": draft["token"],
        "confirmation_expires_at": draft["expires_at"],
        "confirmation_expires_at_iso": _timestamp_iso(draft["expires_at"]),
        "preview": preview,
    }


def _prepare_payload(
    payload: dict[str, Any],
    user: CurrentUserContext,
    *,
    created_via: str,
) -> dict[str, Any]:
    source = dict(payload or {})
    source["created_via"] = created_via
    if not str(source.get("timezone") or "").strip():
        source["timezone"] = get_scheduled_task_store().get_preferences(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )["timezone"]
    spec = normalize_task_spec(source)
    return get_scheduled_task_store().create_draft(
        spec,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        workspace_id=str(payload.get("workspace_id") or "workspace-1"),
    )


def _combined_update_spec(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    combined = {
        "name": task.get("name"),
        "instruction": task.get("instruction"),
        "schedule_type": task.get("schedule_type"),
        "schedule": task.get("schedule"),
        "timezone": task.get("timezone"),
        "source_session_id": task.get("source_session_id"),
        "agent_name": task.get("agent_name"),
        "workdir": task.get("workdir"),
        "created_via": task.get("created_via"),
    }
    for key in combined:
        if key in payload:
            combined[key] = payload[key]
    return normalize_task_spec(combined)


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ScheduleValidationError):
        return HTTPException(400, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    if isinstance(exc, (ValueError, RuntimeError)):
        return HTTPException(409, str(exc))
    return HTTPException(500, str(exc) or "Scheduled task operation failed")


@router.post("/api/scheduled-tasks/parse")
async def parse_scheduled_task(payload: dict[str, Any], request: Request):
    user = require_current_user(request)
    try:
        timezone_name = str(payload.get("timezone") or "").strip()
        if not timezone_name:
            timezone_name = get_scheduled_task_store().get_preferences(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
            )["timezone"]
        result = parse_natural_language(
            str(payload.get("text") or ""),
            timezone_name=timezone_name,
        )
        if result.get("next_run_at") is not None:
            result["next_run_at_iso"] = _timestamp_iso(result["next_run_at"])
        return result
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.get("/api/scheduled-tasks")
async def list_scheduled_tasks(request: Request, limit: int = Query(200, ge=1, le=1000)):
    user = require_current_user(request)
    tasks = get_scheduled_task_store().list_tasks(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        limit=limit,
    )
    return {"tasks": [_serialize_task(task) for task in tasks]}


@router.post("/api/scheduled-tasks")
async def create_scheduled_task(payload: dict[str, Any], request: Request):
    user = require_current_user(request)
    token = str(payload.get("confirmation_token") or "").strip()
    try:
        if token:
            task = get_scheduled_task_store().confirm_draft(
                token,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
            )
            return {"status": "created", "task": _serialize_task(task)}
        return _draft_response(_prepare_payload(payload, user, created_via="api"))
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.get("/api/scheduled-tasks/preferences")
async def get_scheduled_task_preferences(request: Request):
    user = require_current_user(request)
    return get_scheduled_task_store().get_preferences(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )


@router.put("/api/scheduled-tasks/preferences")
async def update_scheduled_task_preferences(payload: dict[str, Any], request: Request):
    user = require_current_user(request)
    timezone_name = str(payload.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise HTTPException(400, f"无效时区：{timezone_name}") from exc
    return get_scheduled_task_store().set_preferences(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        timezone_name=timezone_name,
    )


@router.get("/api/scheduled-tasks/{task_id}")
async def get_scheduled_task(task_id: str, request: Request):
    user = require_current_user(request)
    return {"task": _serialize_task(_task_or_404(task_id, user))}


@router.patch("/api/scheduled-tasks/{task_id}")
async def update_scheduled_task(task_id: str, payload: dict[str, Any], request: Request):
    user = require_current_user(request)
    task = _task_or_404(task_id, user)
    try:
        spec = _combined_update_spec(task, payload)
        updated = get_scheduled_task_store().update_task(
            task_id,
            spec,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        return {"task": _serialize_task(updated)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.delete("/api/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: str, request: Request):
    user = require_current_user(request)
    deleted = get_scheduled_task_store().delete_task(
        task_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not deleted:
        raise HTTPException(404, "Scheduled task not found")
    return {"status": "deleted"}


async def _change_status(
    task_id: str,
    user: CurrentUserContext,
    *,
    status: str,
    admin: bool = False,
) -> dict[str, Any]:
    task = _task_or_404(task_id, user, admin=admin)
    next_run_at: float | None = None
    if status == "active":
        try:
            next_run_at = compute_next_run(
                str(task.get("schedule_type") or ""),
                dict(task.get("schedule") or {}),
                str(task.get("timezone") or "Asia/Shanghai"),
            )
            if task.get("schedule_type") in {"at", "delay"} and (
                next_run_at is None or next_run_at <= datetime.now().timestamp()
            ):
                raise ScheduleValidationError("一次性任务的执行时间已过，请编辑时间后恢复")
        except Exception as exc:
            raise _handle_error(exc) from exc
    updated = get_scheduled_task_store().set_status(
        task_id,
        status,
        tenant_id=user.tenant_id,
        user_id=None if admin else user.user_id,
        next_run_at=next_run_at,
    )
    return {"task": _serialize_task(updated)}


@router.post("/api/scheduled-tasks/{task_id}/pause")
async def pause_scheduled_task(task_id: str, request: Request):
    return await _change_status(task_id, require_current_user(request), status="paused")


@router.post("/api/scheduled-tasks/{task_id}/resume")
async def resume_scheduled_task(task_id: str, request: Request):
    return await _change_status(task_id, require_current_user(request), status="active")


async def _dispatch_task(
    task_id: str,
    user: CurrentUserContext,
    *,
    admin: bool,
    trigger: str,
) -> dict[str, Any]:
    _task_or_404(task_id, user, admin=admin)
    try:
        run_id = await get_scheduled_task_service().dispatch_now(
            task_id,
            tenant_id=user.tenant_id,
            user_id=None if admin else user.user_id,
            trigger=trigger,
        )
        return {"status": "accepted", "run_id": run_id}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.post("/api/scheduled-tasks/{task_id}/run")
async def run_scheduled_task(task_id: str, request: Request):
    return await _dispatch_task(
        task_id, require_current_user(request), admin=False, trigger="manual"
    )


@router.post("/api/scheduled-tasks/{task_id}/retry")
async def retry_scheduled_task(task_id: str, request: Request):
    return await _dispatch_task(
        task_id, require_current_user(request), admin=False, trigger="retry"
    )


@router.get("/api/scheduled-tasks/{task_id}/runs")
async def list_scheduled_task_runs(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
):
    user = require_current_user(request)
    try:
        runs = get_scheduled_task_store().list_runs(
            task_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            limit=limit,
        )
        return {"runs": [_serialize_run(run) for run in runs]}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.get("/api/notifications")
async def list_notifications(
    request: Request,
    unread_only: bool = False,
    limit: int = Query(200, ge=1, le=1000),
):
    user = require_current_user(request)
    store = get_scheduled_task_store()
    notifications = store.list_notifications(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        unread_only=unread_only,
        limit=limit,
    )
    return {
        "notifications": [_serialize_notification(item) for item in notifications],
        "unread_count": store.unread_count(
            tenant_id=user.tenant_id, user_id=user.user_id
        ),
    }


@router.post("/api/notifications/read-all")
async def read_all_notifications(request: Request):
    user = require_current_user(request)
    count = get_scheduled_task_store().mark_all_notifications_read(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    return {"status": "ok", "updated": count}


@router.post("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: str, request: Request):
    user = require_current_user(request)
    updated = get_scheduled_task_store().mark_notification_read(
        notification_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not updated:
        raise HTTPException(404, "Notification not found")
    return {"status": "ok"}


@router.get("/api/admin/scheduled-tasks")
async def admin_list_scheduled_tasks(
    request: Request,
    limit: int = Query(500, ge=1, le=1000),
):
    user = _admin_user(request)
    tasks = get_scheduled_task_store().list_tasks(
        tenant_id=user.tenant_id,
        user_id=None,
        limit=limit,
    )
    return {"tasks": [_serialize_task(task) for task in tasks]}


@router.patch("/api/admin/scheduled-tasks/{task_id}")
async def admin_update_scheduled_task(
    task_id: str, payload: dict[str, Any], request: Request
):
    user = _admin_user(request)
    task = _task_or_404(task_id, user, admin=True)
    try:
        spec = _combined_update_spec(task, payload)
        updated = get_scheduled_task_store().update_task(
            task_id,
            spec,
            tenant_id=user.tenant_id,
            user_id=None,
        )
        return {"task": _serialize_task(updated)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.delete("/api/admin/scheduled-tasks/{task_id}")
async def admin_delete_scheduled_task(task_id: str, request: Request):
    user = _admin_user(request)
    deleted = get_scheduled_task_store().delete_task(
        task_id,
        tenant_id=user.tenant_id,
        user_id=None,
    )
    if not deleted:
        raise HTTPException(404, "Scheduled task not found")
    return {"status": "deleted"}


@router.post("/api/admin/scheduled-tasks/{task_id}/pause")
async def admin_pause_scheduled_task(task_id: str, request: Request):
    return await _change_status(task_id, _admin_user(request), status="paused", admin=True)


@router.post("/api/admin/scheduled-tasks/{task_id}/resume")
async def admin_resume_scheduled_task(task_id: str, request: Request):
    return await _change_status(task_id, _admin_user(request), status="active", admin=True)


@router.post("/api/admin/scheduled-tasks/{task_id}/run")
async def admin_run_scheduled_task(task_id: str, request: Request):
    return await _dispatch_task(
        task_id, _admin_user(request), admin=True, trigger="manual"
    )


@router.post("/api/admin/scheduled-tasks/{task_id}/retry")
async def admin_retry_scheduled_task(task_id: str, request: Request):
    return await _dispatch_task(
        task_id, _admin_user(request), admin=True, trigger="retry"
    )


@router.get("/api/admin/scheduled-tasks/{task_id}/runs")
async def admin_list_scheduled_task_runs(
    task_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
):
    user = _admin_user(request)
    try:
        runs = get_scheduled_task_store().list_runs(
            task_id,
            tenant_id=user.tenant_id,
            user_id=None,
            limit=limit,
        )
        return {"runs": [_serialize_run(run) for run in runs]}
    except Exception as exc:
        raise _handle_error(exc) from exc
