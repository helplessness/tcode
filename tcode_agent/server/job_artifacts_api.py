"""Job workspaces, actions, artifacts and preview endpoints.

Routes retain their original paths and are registered by app.py.
"""

import mimetypes
import os
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ._shared import (
    _PREVIEW_TICKET_COOKIE,
    _PREVIEW_TICKET_TTL_SECONDS,
    _job_action_diagnostics,
    _load_job_registry,
    _proxy_remote_artifact_download,
    _resolve_job_artifact_access,
    _submit_job_action,
)
from .workspace_scope import (
    _workspace_context_from_request,
)
from .job_workspace import (
    _normalize_preview_ports_local,
    _preview_cookie_path,
    _preview_entry_kind_for_path,
    _proxy_remote_artifact_json,
    _relative_artifact_path,
)
from .auth import auth_required
from .files_api import _is_unsafe_inline_file
from .job_workspace import (
    _build_job_timeline,
    _build_job_workspace_summary,
    _build_resubmission_input,
    _build_submit_input_from_job_and_issue,
    _find_related_symphony_issue,
    _get_job_workspace_or_404,
    _normalize_lifecycle_status,
    _rewrite_preview_location,
    _sign_preview_ticket,
    _verify_preview_ticket,
)
from .schemas import JobActionRequest, JobPreviewTicketRequest

workspace_router = APIRouter()
router = APIRouter()


def _get_job_workspace_for_request(job_id: str, request: Request) -> dict[str, Any]:
    workspace = _get_job_workspace_or_404(job_id)
    if not auth_required():
        return workspace
    workspace_context = _workspace_context_from_request(request)
    owner = workspace.get("owner") if isinstance(workspace.get("owner"), dict) else {}
    owner_tenant = str(owner.get("tenant_id") or "").strip()
    owner_user = str(owner.get("subject") or owner.get("user_id") or "").strip()
    owner_workspace = str(workspace.get("workspace_id") or "").strip()
    if not all((owner_tenant, owner_user, owner_workspace)):
        raise HTTPException(404, f"job not found: {job_id}")
    if (
        owner_tenant != workspace_context.tenant_id
        or owner_user != workspace_context.user_id
        or owner_workspace != workspace_context.workspace_id
    ):
        raise HTTPException(404, f"job not found: {job_id}")
    return workspace


@workspace_router.get("/api/jobs/{job_id}/workspace")
async def get_job_workspace(job_id: str, request: Request):
    workspace = _get_job_workspace_for_request(job_id, request)
    related_issue = _find_related_symphony_issue(workspace)
    if related_issue:
        workspace["issue_identifier"] = str(
            workspace.get("issue_identifier") or related_issue.get("identifier") or ""
        )
        workspace["cancel_actions"] = (
            related_issue.get("cancel_actions")
            if isinstance(related_issue.get("cancel_actions"), list)
            else []
        )
        workspace["issue_state"] = str(related_issue.get("state", "") or "")
        workspace["result_summary"] = str(
            related_issue.get("result_summary", "") or ""
        )
    else:
        workspace.setdefault("cancel_actions", [])
        workspace.setdefault("issue_state", "")
        workspace.setdefault("result_summary", "")
    workspace["lifecycle_status"] = _normalize_lifecycle_status(workspace, related_issue)
    workspace["timeline"] = _build_job_timeline(job_id, workspace, related_issue)
    workspace.update(await _job_action_diagnostics(job_id, workspace, related_issue))

    return {
        "job_id": job_id,
        "workspace": workspace,
    }


@router.get("/api/jobs/{job_id}/timeline")
async def get_job_timeline(job_id: str, request: Request):
    workspace = _get_job_workspace_for_request(job_id, request)
    related_issue = _find_related_symphony_issue(workspace)
    if related_issue:
        workspace["cancel_actions"] = (
            related_issue.get("cancel_actions")
            if isinstance(related_issue.get("cancel_actions"), list)
            else []
        )
        workspace["issue_state"] = str(related_issue.get("state", "") or "")
        workspace["result_summary"] = str(related_issue.get("result_summary", "") or "")
    timeline = _build_job_timeline(job_id, workspace, related_issue)
    return {
        "job_id": job_id,
        "lifecycle_status": _normalize_lifecycle_status(workspace, related_issue),
        "timeline": timeline,
    }


@router.get("/api/jobs/{job_id}/submission")
async def get_job_submission(job_id: str, request: Request):
    _get_job_workspace_for_request(job_id, request)
    registry = _load_job_registry()
    job = registry.get(job_id)
    if not isinstance(job, dict):
        raise HTTPException(404, f"job not found: {job_id}")

    workspace = _build_job_workspace_summary(job_id, job)
    issue = _find_related_symphony_issue(workspace)
    submit_input = _build_submit_input_from_job_and_issue(job_id, job, issue)
    if not submit_input:
        raise HTTPException(404, "submit payload is not available for this job")

    retry_input = _build_resubmission_input(
        submit_input=submit_input,
        source_workspace=workspace,
        lineage_key="retry_of",
        lineage_value=job_id,
    )
    resume_input = None
    if bool(workspace.get("resume_supported", False)) or str(workspace.get("resume_command", "") or "").strip():
        resume_input = _build_resubmission_input(
            submit_input=submit_input,
            source_workspace=workspace,
            lineage_key="resume_of",
            lineage_value=job_id,
        )
        resume_command = str(workspace.get("resume_command", "") or "").strip()
        if resume_command:
            resume_input["command"] = resume_command

    return {
        "job_id": job_id,
        "submit_input": submit_input,
        "retry_input": retry_input,
        "resume_input": resume_input,
        "resume_supported": bool(resume_input),
    }


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, payload: JobActionRequest, request: Request):
    _get_job_workspace_for_request(job_id, request)
    return await _submit_job_action(
        job_id=job_id,
        action="retry",
        reuse_issue=bool(payload.reuse_issue if payload.reuse_issue is not None else True),
        reason=str(payload.reason or "").strip(),
    )


@router.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, payload: JobActionRequest, request: Request):
    _get_job_workspace_for_request(job_id, request)
    return await _submit_job_action(
        job_id=job_id,
        action="resume",
        reuse_issue=bool(payload.reuse_issue if payload.reuse_issue is not None else True),
        reason=str(payload.reason or "").strip(),
    )


@router.get("/api/jobs/{job_id}/artifacts")
async def list_job_artifacts(
    job_id: str,
    request: Request,
    path: str = Query(default=""),
    root: str = Query(default=""),
):
    workspace = _get_job_workspace_for_request(job_id, request)
    artifact_source, target_path, normalized_root = _resolve_job_artifact_access(
        workspace,
        root=root,
        path=path,
    )
    remote_base_url = str(artifact_source.get("remote_base_url", "") or "").strip()
    if remote_base_url:
        data = await _proxy_remote_artifact_json(
            remote_base_url,
            f"/api/jobs/{job_id}/artifacts",
            {"path": path, "root": root},
        )
        data.setdefault("job_id", job_id)
        data.setdefault("artifact_source", artifact_source)
        return data

    if target_path is None or not target_path.is_dir():
        raise HTTPException(400, "Artifact path is not a directory")

    items: list[dict[str, Any]] = []
    for entry in os.scandir(target_path):
        if entry.name in ["__pycache__", "node_modules", ".git", "venv", ".venv"]:
            continue
        item = {
            "name": entry.name,
            "path": _relative_artifact_path(entry.path, normalized_root),
            "type": "directory" if entry.is_dir() else "file",
        }
        if not entry.is_dir():
            try:
                item["size"] = entry.stat().st_size
            except Exception:
                item["size"] = 0
            item["preview_type"] = _preview_entry_kind_for_path(entry.path)
        items.append(item)
    items.sort(key=lambda item: (item["type"] != "directory", str(item["name"]).lower()))
    return {
        "job_id": job_id,
        "path": path,
        "root": normalized_root,
        "artifact_source": artifact_source,
        "items": items,
    }


@router.get("/api/jobs/{job_id}/artifacts/content")
async def get_job_artifact_content(
    job_id: str,
    request: Request,
    path: str = Query(...),
    root: str = Query(default=""),
):
    workspace = _get_job_workspace_for_request(job_id, request)
    artifact_source, target_path, normalized_root = _resolve_job_artifact_access(
        workspace,
        root=root,
        path=path,
    )
    remote_base_url = str(artifact_source.get("remote_base_url", "") or "").strip()
    if remote_base_url:
        data = await _proxy_remote_artifact_json(
            remote_base_url,
            f"/api/jobs/{job_id}/artifacts/content",
            {"path": path, "root": root},
        )
        data.setdefault("job_id", job_id)
        data.setdefault("artifact_source", artifact_source)
        return data

    if target_path is None or not target_path.is_file():
        raise HTTPException(404, "Artifact file not found")
    try:
        content = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Cannot read artifact file as text")

    if "\x00" in content[:8192]:
        raise HTTPException(400, "Binary artifact file")

    return {
        "job_id": job_id,
        "path": path,
        "root": normalized_root,
        "content": content,
        "size": target_path.stat().st_size,
        "content_type": mimetypes.guess_type(target_path.name)[0] or "text/plain",
        "preview_type": _preview_entry_kind_for_path(target_path.as_posix()),
        "artifact_source": artifact_source,
    }


@router.get("/api/jobs/{job_id}/artifacts/download")
async def download_job_artifact(
    job_id: str,
    request: Request,
    path: str = Query(...),
    root: str = Query(default=""),
    inline: Optional[bool] = Query(default=False),
):
    workspace = _get_job_workspace_for_request(job_id, request)
    artifact_source, target_path, normalized_root = _resolve_job_artifact_access(
        workspace,
        root=root,
        path=path,
    )
    remote_base_url = str(artifact_source.get("remote_base_url", "") or "").strip()
    if remote_base_url:
        return await _proxy_remote_artifact_download(
            remote_base_url,
            f"/api/jobs/{job_id}/artifacts/download",
            {"path": path, "root": root, "inline": "1" if inline else "0"},
        )

    if target_path is None or not target_path.is_file():
        raise HTTPException(404, "Artifact file not found")

    media_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
    safe_inline = bool(inline) and not _is_unsafe_inline_file(target_path.name, media_type)
    response = FileResponse(
        path=target_path.as_posix(),
        filename=target_path.name,
        media_type=media_type if safe_inline else "application/octet-stream",
        content_disposition_type="inline" if safe_inline else "attachment",
    )
    return response


@router.post("/api/jobs/{job_id}/preview-ticket")
async def create_job_preview_ticket(job_id: str, payload: JobPreviewTicketRequest, request: Request):
    workspace = _get_job_workspace_for_request(job_id, request)
    preview_ports = _normalize_preview_ports_local(workspace.get("preview_ports"))
    if int(payload.port) not in preview_ports:
        raise HTTPException(400, f"preview port is not enabled for job: {payload.port}")

    expires_at = int(time.time()) + _PREVIEW_TICKET_TTL_SECONDS
    ticket = _sign_preview_ticket(job_id, int(payload.port), expires_at)
    return {
        "job_id": job_id,
        "port": int(payload.port),
        "ticket": ticket,
        "expires_at": datetime.utcfromtimestamp(expires_at).isoformat() + "Z",
        "open_url": f"/api/job-preview/{job_id}/{int(payload.port)}?ticket={ticket}",
    }


@router.get("/api/job-preview/{job_id}/{port}")
@router.get("/api/job-preview/{job_id}/{port}/{subpath:path}")
async def proxy_job_preview(
    job_id: str,
    port: int,
    request: Request,
    subpath: str = "",
    ticket: Optional[str] = Query(default=None),
):
    workspace = _get_job_workspace_or_404(job_id)
    preview_ports = _normalize_preview_ports_local(workspace.get("preview_ports"))
    if int(port) not in preview_ports:
        raise HTTPException(403, "Preview port is not allowed")

    ticket_value = (ticket or request.cookies.get(_PREVIEW_TICKET_COOKIE) or "").strip()
    if not ticket_value or not _verify_preview_ticket(ticket_value, job_id, port):
        raise HTTPException(401, "Invalid or expired preview ticket")

    filtered_params = []
    for key, value in request.query_params.multi_items():
        if key != "ticket":
            filtered_params.append((key, value))
    query_string = urlencode(filtered_params, doseq=True)

    if ticket:
        clean_path = f"/api/job-preview/{job_id}/{port}"
        if subpath:
            clean_path = f"{clean_path}/{subpath}"
        if query_string:
            clean_path = f"{clean_path}?{query_string}"
        response = RedirectResponse(url=clean_path, status_code=307)
        response.set_cookie(
            key=_PREVIEW_TICKET_COOKIE,
            value=ticket_value,
            max_age=_PREVIEW_TICKET_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path=_preview_cookie_path(job_id, port),
        )
        return response

    target_url = f"http://127.0.0.1:{int(port)}"
    if subpath:
        target_url = f"{target_url}/{subpath}"
    if query_string:
        target_url = f"{target_url}?{query_string}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, allow_redirects=False) as upstream:
                body = await upstream.read()
                headers: dict[str, str] = {}
                content_type = upstream.headers.get("Content-Type")
                if content_type:
                    headers["content-type"] = content_type
                location = upstream.headers.get("Location")
                if location:
                    headers["location"] = _rewrite_preview_location(
                        location, job_id, port
                    )

                response = Response(
                    content=body,
                    status_code=upstream.status,
                    headers=headers,
                )
                response.set_cookie(
                    key=_PREVIEW_TICKET_COOKIE,
                    value=ticket_value,
                    max_age=_PREVIEW_TICKET_TTL_SECONDS,
                    httponly=True,
                    samesite="lax",
                    path=_preview_cookie_path(job_id, port),
                )
                return response
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Preview upstream request failed: {exc}")
