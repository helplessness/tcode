"""Owner-scoped and administrator sandbox runtime management.

Routes retain their original paths and are registered by app.py.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..runtime.sandbox import get_sandbox_runtime
from .auth import require_current_user
from .auth_api import _require_tenant_admin

router = APIRouter()


@router.get("/api/sandbox/runtimes")
async def list_own_sandbox_runtimes(request: Request, workspace_id: str = ""):
    actor = require_current_user(request)
    runtimes = await get_sandbox_runtime().list_persistent_runtimes(
        tenant_id=actor.tenant_id,
        user_id=actor.user_id,
        workspace_id=str(workspace_id or "").strip(),
    )
    return {"runtimes": [_public_sandbox_runtime(item) for item in runtimes]}


def _public_sandbox_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    hidden = {
        "runtime_key",
        "host_workspace",
        "spec_fingerprint",
        "network_name",
        "network_attachments",
    }
    return {
        str(key): value
        for key, value in dict(runtime or {}).items()
        if str(key) not in hidden
    }


def _sandbox_runtime_management_flags(payload: dict[str, Any]) -> tuple[bool, bool]:
    for field in ("force", "purge_data"):
        if field in payload and not isinstance(payload[field], bool):
            raise HTTPException(400, f"{field} must be a boolean")
    return payload.get("force", False), payload.get("purge_data", False)


@router.post("/api/sandbox/runtimes/{runtime_id}/{action}")
async def manage_own_sandbox_runtime(
    runtime_id: str,
    action: str,
    payload: dict[str, Any],
    request: Request,
):
    actor = require_current_user(request)
    force, purge_data = _sandbox_runtime_management_flags(payload)
    try:
        result = await get_sandbox_runtime().manage_persistent_runtime(
            runtime_id,
            action,
            owner={"tenant_id": actor.tenant_id, "user_id": actor.user_id},
            owner_scope="user",
            force=force,
            purge_data=purge_data,
        )
        return _public_sandbox_runtime(result)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        status_code = int(getattr(exc, "status_code", 409) or 409)
        if status_code < 400 or status_code >= 500:
            status_code = 409
        raise HTTPException(status_code, str(exc)) from exc


@router.get("/api/admin/sandbox-runtimes")
async def list_admin_sandbox_runtimes(
    request: Request,
    user_id: str = "",
    workspace_id: str = "",
):
    actor = _require_tenant_admin(request)
    runtimes = await get_sandbox_runtime().list_persistent_runtimes(
        tenant_id=actor.tenant_id,
        user_id=str(user_id or "").strip(),
        workspace_id=str(workspace_id or "").strip(),
    )
    return {"runtimes": [_public_sandbox_runtime(item) for item in runtimes]}


@router.post("/api/admin/sandbox-runtimes/{runtime_id}/{action}")
async def manage_admin_sandbox_runtime(
    runtime_id: str,
    action: str,
    payload: dict[str, Any],
    request: Request,
):
    actor = _require_tenant_admin(request)
    force, purge_data = _sandbox_runtime_management_flags(payload)
    try:
        result = await get_sandbox_runtime().manage_persistent_runtime(
            runtime_id,
            action,
            owner={"tenant_id": actor.tenant_id},
            owner_scope="tenant",
            force=force,
            purge_data=purge_data,
        )
        return _public_sandbox_runtime(result)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        status_code = int(getattr(exc, "status_code", 409) or 409)
        if status_code < 400 or status_code >= 500:
            status_code = 409
        raise HTTPException(status_code, str(exc)) from exc
