"""Resolve authenticated workspace ownership and session working directories."""

from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, WebSocket

from ..session.manager import Session, SessionManager
from . import workdirs as workdir_utils
from .auth import CurrentUserContext, current_user_from_request
from .state import ServerState

_DEFAULT_USER_WORKDIRS = workdir_utils.DEFAULT_USER_WORKDIRS


def _workspace_context_from_request(
    request: Optional[Request] = None,
    *,
    tenant_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    workdir_name: str = "",
) -> workdir_utils.WorkspaceContext:
    user = current_user_from_request(request) if request is not None else None
    return workdir_utils.resolve_workspace_context(
        headers=None if user is not None else getattr(request, "headers", None),
        query_params=getattr(request, "query_params", None),
        tenant_id=user.tenant_id if user is not None else tenant_id,
        user_id=user.user_id if user is not None else user_id,
        workspace_id=workspace_id,
        workdir_name=workdir_name,
    )


def _workspace_context_from_websocket(
    websocket: WebSocket,
    *,
    tenant_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    workdir_name: str = "",
) -> workdir_utils.WorkspaceContext:
    user = getattr(websocket.state, "current_user", None)
    authenticated = isinstance(user, CurrentUserContext)
    return workdir_utils.resolve_workspace_context(
        headers=None if authenticated else getattr(websocket, "headers", None),
        query_params=getattr(websocket, "query_params", None),
        tenant_id=user.tenant_id if authenticated else tenant_id,
        user_id=user.user_id if authenticated else user_id,
        workspace_id=workspace_id,
        workdir_name=workdir_name,
    )


def _session_owner_kwargs(
    context: Optional[workdir_utils.WorkspaceContext],
    *,
    include_workspace: bool = False,
) -> dict[str, str]:
    if context is None:
        return {}
    payload = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
    }
    if include_workspace:
        payload["workspace_id"] = context.workspace_id
    return payload


def _session_owner_kwargs_from_session(
    session: Optional[Session],
    *,
    include_workspace: bool = True,
) -> dict[str, str]:
    if session is None:
        return {}
    payload = {
        "tenant_id": str(getattr(session, "tenant_id", "") or "").strip(),
        "user_id": str(getattr(session, "user_id", "") or "").strip(),
    }
    if include_workspace:
        payload["workspace_id"] = str(
            getattr(session, "workspace_id", "") or ""
        ).strip()
    return {key: value for key, value in payload.items() if value}


def _runtime_session_matches_context(
    runtime: Any,
    context: Optional[workdir_utils.WorkspaceContext],
    *,
    include_workspace: bool = False,
) -> bool:
    if context is None or not isinstance(runtime, dict):
        return True
    tenant_id = str(runtime.get("tenant_id", "") or workdir_utils.default_tenant_id()).strip()
    user_id = str(runtime.get("user_id", "") or workdir_utils.default_user_id()).strip()
    workspace_id = str(
        runtime.get("workspace_id", "") or workdir_utils.default_workspace_id()
    ).strip()
    if tenant_id != context.tenant_id:
        return False
    if user_id != context.user_id:
        return False
    if include_workspace and workspace_id != context.workspace_id:
        return False
    return True


def _user_workdirs_root(
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> Path:
    return workdir_utils.user_workdirs_root(
        ServerState.get_workdir(),
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _workdir_template_root() -> Path:
    return workdir_utils.workdir_template_root(ServerState.get_workdir())


def _resolve_user_workdir_path(
    workdir_name: str,
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> Path:
    return workdir_utils.resolve_user_workdir_path(
        ServerState.get_workdir(),
        workdir_name,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _workdir_name_from_path(
    path_text: str,
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    return workdir_utils.workdir_name_from_path(
        ServerState.get_workdir(),
        path_text,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _workspace_root_from_resource_path(path_text: str) -> str:
    raw = str(path_text or "").strip()
    if not raw:
        return ""

    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
    except Exception:
        return ""

    workdirs_root = workdir_utils.workspace_storage_root(ServerState.get_workdir())
    try:
        rel = resolved.relative_to(workdirs_root)
        if len(rel.parts) >= 3:
            candidate = (
                workdirs_root / rel.parts[0] / rel.parts[1] / rel.parts[2]
            ).resolve(strict=False)
            if candidate.is_dir():
                return candidate.as_posix()
    except Exception:
        pass

    return ""


def _infer_workdir_from_resource_context(resource_context: Any) -> str:
    if not isinstance(resource_context, dict):
        return ""

    candidates: list[str] = []
    seen: set[str] = set()
    for item in list(resource_context.get("uploaded_files") or []):
        if not isinstance(item, dict):
            continue
        for key in ("source_path", "path"):
            candidate = _workspace_root_from_resource_path(item.get(key) or "")
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)

    return candidates[0] if len(candidates) == 1 else ""


def _list_user_workdirs(
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> list[dict[str, Any]]:
    return workdir_utils.list_user_workdirs(
        ServerState.get_workdir(),
        _DEFAULT_USER_WORKDIRS,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _create_user_workdir(
    name: str,
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> dict[str, Any]:
    return workdir_utils.create_user_workdir(
        ServerState.get_workdir(),
        name,
        _DEFAULT_USER_WORKDIRS,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _assert_session_workspace_active(
    workdir: str, *, tenant_id: str, user_id: str, workspace_id: str,
) -> None:
    # The concrete path takes precedence over stale session workspace metadata.
    server_workdir = ServerState.get_workdir()
    context = workdir_utils.resolve_workspace_context(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workdir_utils.workdir_name_from_path(
            server_workdir, workdir, tenant_id=tenant_id, user_id=user_id,
        ) or workspace_id,
    )
    workdir_utils.assert_workspace_active(server_workdir, context)


async def _resolve_session_workdir(
    session_id: str,
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    runtime = ServerState.sessions.get(session_id, {}) if session_id else {}
    runtime_workdir = (
        str(runtime.get("workdir", "") or "").strip()
        if _runtime_session_matches_context(runtime, context, include_workspace=False)
        else ""
    )
    if runtime_workdir and Path(runtime_workdir).is_dir():
        runtime_tenant_id = str(
            runtime.get("tenant_id") or (context.tenant_id if context else "")
        )
        runtime_user_id = str(
            runtime.get("user_id") or (context.user_id if context else "")
        )
        _assert_session_workspace_active(
            runtime_workdir,
            tenant_id=runtime_tenant_id,
            user_id=runtime_user_id,
            workspace_id=str(runtime.get("workspace_id") or (context.workspace_id if context else "")),
        )
        return runtime_workdir

    persisted = (
        await SessionManager.get(
            session_id,
            # The session is authoritative for its workspace.  A request that
            # only carries session_id otherwise inherits the default workspace,
            # which may be unrelated or already purged.
            **_session_owner_kwargs(context, include_workspace=False),
        )
        if session_id
        else None
    )
    persisted_workdir = str(getattr(persisted, "workdir", "") or "").strip()
    if persisted_workdir and Path(persisted_workdir).is_dir():
        persisted_tenant_id = str(
            getattr(persisted, "tenant_id", "")
            or (context.tenant_id if context else "")
        )
        persisted_user_id = str(
            getattr(persisted, "user_id", "")
            or (context.user_id if context else "")
        )
        _assert_session_workspace_active(
            persisted_workdir,
            tenant_id=persisted_tenant_id,
            user_id=persisted_user_id,
            workspace_id=str(getattr(persisted, "workspace_id", "") or (context.workspace_id if context else "")),
        )
        return persisted_workdir

    if context is not None:
        workdir_utils.assert_workspace_active(ServerState.get_workdir(), context)
    default_target = _resolve_user_workdir_path(
        (context.workspace_id if context else _DEFAULT_USER_WORKDIRS[0]),
        context,
    )
    if not default_target.is_dir():
        raise HTTPException(404, "workspace not found")
    return default_target.as_posix()


def _resolve_workdir_from_name_or_path(
    workdir_name: str = "",
    workdir_path: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    return workdir_utils.resolve_workdir_from_name_or_path(
        ServerState.get_workdir(),
        workdir_name=workdir_name,
        workdir_path=workdir_path,
        default_names=_DEFAULT_USER_WORKDIRS,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


def _resolve_requested_workdir_for_session(
    *,
    workdir_name: str = "",
    workdir_path: str = "",
    existing_workdir: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    return workdir_utils.resolve_requested_workdir_for_session(
        ServerState.get_workdir(),
        workdir_name=workdir_name,
        workdir_path=workdir_path,
        existing_workdir=existing_workdir,
        default_names=_DEFAULT_USER_WORKDIRS,
        tenant_id=(context.tenant_id if context else ""),
        user_id=(context.user_id if context else ""),
    )


async def _resolve_workflow_workdir(
    *,
    session_id: str = "",
    workdir_name: str = "",
    workdir_path: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    session_text = str(session_id or "").strip()
    if session_text:
        runtime = ServerState.sessions.get(session_text, {}) if session_text else {}
        if context is not None and not _runtime_session_matches_context(
            runtime,
            context,
            include_workspace=True,
        ):
            runtime = {}
        runtime_workdir = str(runtime.get("workdir", "") or "").strip()
        if runtime_workdir and Path(runtime_workdir).is_dir():
            return runtime_workdir

        persisted = await SessionManager.get(
            session_text,
            **_session_owner_kwargs(context, include_workspace=True),
        )
        persisted_workdir = str(getattr(persisted, "workdir", "") or "").strip()
        if persisted_workdir and Path(persisted_workdir).is_dir():
            return persisted_workdir
        if context is not None:
            raise HTTPException(404, "Session not found")

    if context is not None:
        resolved_context = workdir_utils.resolve_workspace_context(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=str(workdir_name or "").strip() or context.workspace_id,
        )
        return _resolve_user_workdir_path(resolved_context.workspace_id, resolved_context).as_posix()

    if str(workdir_name or "").strip() or str(workdir_path or "").strip():
        return _resolve_workdir_from_name_or_path(
            workdir_name=str(workdir_name or "").strip(),
            workdir_path=str(workdir_path or "").strip(),
        )

    return ServerState.get_workdir()
