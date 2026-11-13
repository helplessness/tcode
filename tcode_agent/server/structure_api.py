"""Authenticated browser end of the session-scoped Mol* bridge."""
from fastapi import APIRouter, HTTPException, Request
from urllib.parse import urlsplit

from .workspace_scope import (
    _workspace_context_from_request,
    _session_owner_kwargs,
)
from .auth import auth_required, require_current_user
from ..session.manager import SessionManager
from ..runtime.structure_view import Exchange, bridge

router = APIRouter()


async def _owner(request: Request, session_id: str, workspace_id: str):
    if auth_required():
        require_current_user(request)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        source = urlsplit(origin)
        loopbacks = {"localhost", "127.0.0.1", "::1"}
        dev_proxy = source.hostname in loopbacks and request.url.hostname in loopbacks and source.port == 5173 and request.url.port == 8080
        if not dev_proxy:
            raise HTTPException(403, "Cross-origin viewer request refused")
    context = _workspace_context_from_request(request, workspace_id=workspace_id)
    scope = _session_owner_kwargs(context, include_workspace=True)
    if not await SessionManager.get(session_id, **scope):
        raise HTTPException(404, "Session not found")
    return context.tenant_id, context.user_id, context.workspace_id, session_id


@router.post("/api/structure-view/{session_id}")
async def exchange(request: Request, session_id: str, workspace_id: str, payload: Exchange):
    owner = await _owner(request, session_id, workspace_id)
    try:
        return bridge.exchange(owner, payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/api/structure-view/{session_id}")
async def disconnect(request: Request, session_id: str, workspace_id: str, viewer_id: str):
    owner = await _owner(request, session_id, workspace_id)
    bridge.disconnect(owner, viewer_id)
    return {"ok": True}
