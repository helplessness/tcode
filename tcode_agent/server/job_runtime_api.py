"""统一 Job Runtime 的 Owner 约束查询、Cursor 与取消 API。"""

from __future__ import annotations

from typing import Any
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..runtime.jobs import (
    JobAccessError,
    JobNotFoundError,
    JobCancellationUnsupported,
    JobCursorConflict,
    JobOwner,
    TaskRuntimeJobProvider,
    job_contract_document,
)
from ..session.manager import SessionManager
from ..runtime.shared_tracker import SharedTracker
from .auth import require_current_user


router = APIRouter(prefix="/api/runtime/jobs", tags=["runtime-jobs"])


class JobCancelRequest(BaseModel):
    """取消请求必须携带 Session、Workspace 与客户端最后观察到的 Cursor。"""

    session_id: str
    workspace_id: str
    expected_cursor: int


class JobArtifactRecoveryRequest(BaseModel):
    session_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    tracker_identifier: str = Field(min_length=1)
    bundle_id: str = Field(pattern=r"^bundle_[A-Za-z0-9_-]{1,100}$")
    service_id: str = Field(default='', pattern=r'^$|^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


@router.post("/{job_id}/recover-artifacts")
async def recover_runtime_job_artifacts(job_id: str, payload: JobArtifactRecoveryRequest, request: Request):
    provider, owner = await _provider_for_request(request, session_id=payload.session_id, workspace_id=payload.workspace_id)
    task = await asyncio.to_thread(SharedTracker.get_task, identifier=payload.tracker_identifier, workdir=provider.workdir)
    workspace = (task or {}).get("workspace") or {}
    ref = (task or {}).get("job_ref") or {}
    references = [value for value in (ref.get("job_id"), ref.get("id"), workspace.get("job_id")) if value]
    if (not task or task.get("session_id") != payload.session_id
            or any(workspace.get(key) != value for key, value in owner.to_dict().items())
            or not references or any(value != job_id for value in references)):
        raise HTTPException(404, "job not found")
    if task.get("status") not in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "任务尚未结束，不能手工关联产物。")
    from ..workspace.artifact_recovery import recover_task_artifacts
    from ..artifact_transport import ArtifactMaterializationError
    try:
        return await asyncio.to_thread(recover_task_artifacts, control=provider.workdir, task=task, bundle_id=payload.bundle_id,
                                       **({'service_id': payload.service_id} if payload.service_id else {}))
    except (ValueError, ArtifactMaterializationError) as exc:
        # Do not leak configured service URLs or host paths through transport errors.
        detail = "产物取回校验失败，请核对引用、文件归属与完整性；原任务状态未改变。" if isinstance(exc, ArtifactMaterializationError) else str(exc)
        raise HTTPException(409, detail) from exc


async def _provider_for_request(
    request: Request,
    *,
    session_id: str,
    workspace_id: str,
) -> tuple[TaskRuntimeJobProvider, JobOwner]:
    """验证认证用户拥有 Session，并构造绑定 Session 工作目录的 Provider。"""

    user = require_current_user(request)
    owner = JobOwner(
        tenant_id=str(user.tenant_id or ""),
        user_id=str(user.user_id or ""),
        workspace_id=str(workspace_id or "").strip(),
    )
    session = await SessionManager.get(
        str(session_id or "").strip(),
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        workspace_id=owner.workspace_id,
    )
    if session is None:
        raise HTTPException(404, "session or job not found")
    return TaskRuntimeJobProvider(workdir=str(session.workdir or "") or None), owner


@router.get("/contract")
async def get_job_contract() -> dict[str, Any]:
    """返回不包含业务数据的 Job v1 合同文档。"""

    return {"success": True, "contract": job_contract_document()}


@router.get("/{job_id}")
async def get_runtime_job(
    job_id: str,
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
    tracker_identifier: str = "",
) -> dict[str, Any]:
    """读取当前用户、工作区和 Session 共同拥有的 Job。"""

    provider, owner = await _provider_for_request(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    try:
        record = await provider.read(job_id, owner)
    except JobNotFoundError as exc:
        task = SharedTracker.get_task(identifier=tracker_identifier, workdir=provider.workdir) if tracker_identifier else None
        workspace = (task or {}).get("workspace") or {}
        job_ref = (task or {}).get("job_ref") or {}
        references = [value for value in (
            job_ref.get("job_id"), job_ref.get("id"), workspace.get("job_id")
        ) if value not in (None, "")]
        if (
            task
            and task.get("execution_channel") == "bridge"
            and task.get("session_id") == session_id
            and all(workspace.get(key) == value for key, value in owner.to_dict().items())
            and references
            and all(value == job_id for value in references)
        ):
            return {"success": True, "job": None, "projection_state": "tracker_only"}
        raise HTTPException(404, "job not found") from exc
    except JobAccessError as exc:
        raise HTTPException(404, "job not found") from exc
    return {"success": True, "job": record.to_dict()}


@router.get("/{job_id}/events")
async def get_runtime_job_events(
    job_id: str,
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
    after_cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """返回指定单调 Cursor 之后的有界脱敏 Job 事件。"""

    provider, owner = await _provider_for_request(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    try:
        events = await provider.events(
            job_id,
            owner,
            after_cursor=after_cursor,
            limit=limit,
        )
    except JobAccessError as exc:
        raise HTTPException(404, "job not found") from exc
    return {
        "success": True,
        "events": [event.to_dict() for event in events],
        "next_cursor": events[-1].cursor if events else after_cursor,
    }


@router.post("/{job_id}/cancel")
async def cancel_runtime_job(
    job_id: str,
    payload: JobCancelRequest,
    request: Request,
) -> dict[str, Any]:
    """在 Owner 与 Cursor 都匹配后调用经过审核的 Provider Canceller。"""

    provider, owner = await _provider_for_request(
        request,
        session_id=payload.session_id,
        workspace_id=payload.workspace_id,
    )
    try:
        record = await provider.cancel(
            job_id,
            owner,
            expected_cursor=payload.expected_cursor,
        )
    except JobAccessError as exc:
        raise HTTPException(404, "job not found") from exc
    except JobCursorConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except JobCancellationUnsupported as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "job": record.to_dict()}


__all__ = ["router"]
