"""Authenticated adapters for the shared SVG canvas service."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..agui.auth import APIKeyPrincipal
from ..agui.rate_limit import GatewayRateLimiter, RateLimitExceeded
from ..presentation.canvas import CanvasError, CanvasStore, Edit, Part, Restore, enabled
from ..session.manager import SessionManager
from .auth import require_current_user, intelligent_ui_preference
from ..presentation.canvas_research import association_state, associate, handoff_items, receive, save_handoff
from ..presentation.intelligent_page import PageStore

router = APIRouter(prefix="/api/intelligent-ui/v1", tags=["IntelligentUI"])
rate_limiter = GatewayRateLimiter()


class StylePreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style: Literal["clay", "manual"] | None = None
    enabled: bool | None = None


@router.get("/preferences")
async def read_preference(request: Request):
    user = require_current_user(request)
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "personal_settings_require_user_session")
    return await run_in_threadpool(intelligent_ui_preference, user.tenant_id, user.user_id)


@router.put("/preferences")
async def save_preference(body: StylePreference, request: Request):
    user = require_current_user(request)
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "personal_settings_require_user_session")
    return await run_in_threadpool(intelligent_ui_preference, user.tenant_id, user.user_id,
                                  save=True, style=body.style, enabled=body.enabled,
                                  update_style="style" in body.model_fields_set or body.enabled is None)


async def _effective_style(request: Request, *, generate: bool = False):
    user = require_current_user(request)
    value = await run_in_threadpool(intelligent_ui_preference, user.tenant_id, user.user_id)
    if generate and not value["effective_enabled"]:
        raise HTTPException(403, "intelligent_ui_generation_disabled")
    return value["effective_style"]


class CreateCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=1_048_576)
    parts: list[Part] = Field(default_factory=list, max_length=100)
    request_id: str = Field(min_length=1, max_length=128)


class SelectCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    element_ids: list[str] = Field(min_length=1, max_length=100)


class RevisionPurge(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    base_revision_id: str = Field(pattern=r'^rev_[0-9a-f]{32}$')
    revision_id: str = Field(pattern=r'^rev_[0-9a-f]{32}$')


class RevisionDeletion(RevisionPurge):
    deleted: bool


class AssociateCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    research_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)


class ReceiveCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    object_id: str = Field(min_length=1, max_length=256)


class HandoffSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    canvas_id: str = Field(pattern=r"^canvas_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")


@router.post("/pack-assets/{asset_id}/canvases/{object_id}/snapshots")
async def save_pack_canvas(asset_id: str, object_id: str, body: HandoffSnapshot, request: Request, session_id: str):
    return await _call(save_handoff, await _research_session(request, session_id, True), asset_id, object_id,
                       body.canvas_id, body.revision_id)


async def _research_session(request, session_id, write=False):
    from .research_compare_api import _session
    await _store(request, session_id, write=write)
    return await _session(request, session_id)


@router.get("/canvases/{canvas_id}/research")
async def canvas_research_state(canvas_id: str, request: Request, session_id: str, research_id: str = ""):
    session = await _research_session(request, session_id)
    return await _call(association_state, session, canvas_id, research_id)


@router.post("/canvases/{canvas_id}/research")
async def link_canvas_research(canvas_id: str, body: AssociateCanvas, request: Request, session_id: str):
    session = await _research_session(request, session_id, True)
    return await _call(associate, session, canvas_id, **body.model_dump())


@router.get("/pack-assets/{asset_id}/canvases")
async def pack_canvases(asset_id: str, request: Request, session_id: str):
    return await _call(handoff_items, await _research_session(request, session_id), asset_id)


@router.post("/pack-assets/{asset_id}/canvases")
async def receive_pack_canvas(asset_id: str, body: ReceiveCanvas, request: Request, session_id: str):
    from ..presentation.canvas import canvas_block
    from ..types import Message
    import uuid
    session = await _research_session(request, session_id, True)
    value = await _call(receive, session, asset_id, body.object_id)
    def attach(current):
        if not any(b.get("type") == "canvas" and b.get("canvas_id") == value["canvas_id"]
                   for m in current.messages for b in (m.content_blocks or [])):
            current.messages.append(Message(id=str(uuid.uuid4()), role="assistant",
                content="已从研究包打开本地画布副本。原研究包保持只读。",
                content_blocks=[canvas_block(value)]))
        return True
    await SessionManager.mutate(session.id, attach, tenant_id=session.tenant_id,
        user_id=session.user_id, workspace_id=session.workspace_id)
    return value


async def _store(request: Request, session_id: str, *, write: bool = False, store_type=CanvasStore) -> CanvasStore:
    if write and not enabled():
        raise HTTPException(403, "canvas_editing_disabled")
    user = require_current_user(request)
    try:
        rate_limiter.consume(f"canvas:{user.tenant_id}:{user.user_id}", "write" if write else "read",
                             limit=60 if write else 300)
    except RateLimitExceeded as exc:
        raise HTTPException(429, "canvas_rate_limited", headers={"Retry-After": str(exc.retry_after)}) from exc
    principal = getattr(request.state, "agui_api_key", None)
    if isinstance(principal, APIKeyPrincipal) and not principal.permits("canvas:write" if write else "canvas:read"):
        raise HTTPException(403, "canvas_scope_required")
    session = await SessionManager.get(session_id, tenant_id=user.tenant_id, user_id=user.user_id)
    if session is None:
        raise HTTPException(404, "session_not_found")
    try:
        return await run_in_threadpool(store_type, session.workdir, tenant_id=user.tenant_id,
                                       user_id=user.user_id, workspace_id=session.workspace_id)
    except CanvasError as exc:
        raise HTTPException(exc.status, exc.code) from exc


async def _call(method, *args, **kwargs):
    try:
        return await run_in_threadpool(method, *args, **kwargs)
    except CanvasError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "layout_quality": exc.details}
                            if exc.details else exc.code) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/canvases")
async def create_canvas(body: CreateCanvas, request: Request, session_id: str = Query(min_length=1, max_length=200)):
    store = await _store(request, session_id, write=True)
    return await _call(store.create, session_id=session_id, **body.model_dump(exclude={"parts"}), parts=body.parts,
                       style=await _effective_style(request, generate=True))


@router.get("/canvases/{canvas_id}")
async def read_canvas(canvas_id: str, request: Request,
                      session_id: str = Query(min_length=1, max_length=200), revision_id: str = Query(default="", max_length=100)):
    store = await _store(request, session_id)
    value = await _call(store.read, canvas_id, revision_id)
    value["display_style"] = await _effective_style(request)
    return value


@router.get("/canvases/{canvas_id}/revisions")
async def list_revisions(canvas_id: str, request: Request,
                         session_id: str = Query(min_length=1, max_length=200), before: str = Query(default="", max_length=100), deleted: bool = False):
    store = await _store(request, session_id)
    return await _call(store.revisions, canvas_id, before, deleted)


@router.post('/canvases/{canvas_id}/revision-deletion')
async def delete_canvas_revision(canvas_id: str, body: RevisionDeletion, request: Request, session_id: str):
    store = await _store(request, session_id, write=True)
    return await _call(store.set_revision_deleted, canvas_id, session_id=session_id, **body.model_dump())


@router.post('/pages/{page_id}/revision-deletion')
async def delete_page_revision(page_id: str, body: RevisionDeletion, request: Request, session_id: str):
    store = await _page_store(request, session_id, write=True)
    return await _call(store.set_revision_deleted, page_id, session_id=session_id, kind='page', **body.model_dump())


@router.post('/canvases/{canvas_id}/revision-purge')
async def purge_canvas_revision(canvas_id: str, body: RevisionPurge, request: Request, session_id: str):
    store = await _store(request, session_id, write=True)
    return await _call(store.purge_revision, canvas_id, session_id=session_id, **body.model_dump())


@router.post('/pages/{page_id}/revision-purge')
async def purge_page_revision(page_id: str, body: RevisionPurge, request: Request, session_id: str):
    store = await _page_store(request, session_id, write=True)
    return await _call(store.purge_revision, page_id, session_id=session_id, kind='page', **body.model_dump())


@router.post("/canvases/{canvas_id}/edits")
async def edit_canvas(canvas_id: str, body: Edit, request: Request,
                      session_id: str = Query(min_length=1, max_length=200)):
    store = await _store(request, session_id, write=True)
    return await _call(store.edit, canvas_id, body)


@router.post("/canvases/{canvas_id}/selection")
async def select_canvas(canvas_id: str, body: SelectCanvas, request: Request,
                        session_id: str = Query(min_length=1, max_length=200)):
    store = await _store(request, session_id, write=True)
    return await _call(store.select, canvas_id, session_id=session_id, **body.model_dump())


@router.post("/canvases/{canvas_id}/restore")
async def restore_canvas(canvas_id: str, body: Restore, request: Request,
                         session_id: str = Query(min_length=1, max_length=200)):
    store = await _store(request, session_id, write=True)
    return await _call(store.edit, canvas_id, body)


@router.get("/canvases/{canvas_id}/export")
async def export_canvas(canvas_id: str, request: Request,
                        session_id: str = Query(min_length=1, max_length=200), revision_id: str = Query(default="", max_length=100)):
    store = await _store(request, session_id)
    value = await _call(store.read, canvas_id, revision_id)
    return Response(value["source"], media_type="image/svg+xml", headers={
        "Content-Disposition": 'attachment; filename="canvas.svg"',
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


class PageAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    button_id: str = Field(min_length=1, max_length=96)
    selections: dict[str, int] = Field(default_factory=dict, max_length=30)


async def _page_store(request, session_id, write=False):
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "page_requires_user_session")
    return await _store(request, session_id, write=write, store_type=PageStore)


async def _page_session(request, session_id, write=False):
    await _page_store(request, session_id, write)
    from .research_compare_api import _session
    return await _session(request, session_id)


@router.get("/pages/{page_id}/revisions")
async def page_revisions(page_id: str, request: Request, session_id: str, before: str = Query(default="", max_length=100), deleted: bool = False):
    return await _call((await _page_store(request, session_id)).revisions, page_id, before, deleted)


@router.post("/pages/{page_id}/restore")
async def restore_page(page_id: str, body: Restore, request: Request, session_id: str):
    return await _call((await _page_store(request, session_id, True)).restore,
        page_id, session_id=session_id, **body.model_dump())


@router.get("/pages/{page_id}/research")
async def page_research_state(page_id: str, request: Request, session_id: str, research_id: str = ""):
    return await _call(association_state, await _page_session(request, session_id), page_id, research_id, "page")


@router.post("/pages/{page_id}/research")
async def link_page_research(page_id: str, body: AssociateCanvas, request: Request, session_id: str):
    return await _call(associate, await _page_session(request, session_id, True), page_id, **body.model_dump(), kind="page")


@router.get("/pack-assets/{asset_id}/pages")
async def pack_pages(asset_id: str, request: Request, session_id: str):
    return await _call(handoff_items, await _page_session(request, session_id), asset_id, "page")


@router.post("/pack-assets/{asset_id}/pages")
async def receive_pack_page(asset_id: str, body: ReceiveCanvas, request: Request, session_id: str):
    from ..presentation.intelligent_page import page_block
    from ..types import Message
    import uuid
    session = await _page_session(request, session_id, True)
    value = await _call(receive, session, asset_id, body.object_id, "page")
    def attach(current):
        if not any(b.get("type") == "intelligent_page" and b.get("page_id") == value["page_id"]
                   for m in current.messages for b in (m.content_blocks or [])):
            current.messages.append(Message(id=str(uuid.uuid4()), role="assistant",
                content="已从研究包打开本地交互页面副本。原研究包保持只读。", content_blocks=[page_block(value)]))
        return True
    await SessionManager.mutate(session.id, attach, tenant_id=session.tenant_id, user_id=session.user_id, workspace_id=session.workspace_id)
    return value


class PageHandoffSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_id: str = Field(pattern=r"^page_[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")


@router.post("/pack-assets/{asset_id}/pages/{object_id}/snapshots")
async def save_pack_page(asset_id: str, object_id: str, body: PageHandoffSnapshot, request: Request, session_id: str):
    return await _call(save_handoff, await _page_session(request, session_id, True), asset_id, object_id,
        body.page_id, body.revision_id, "page")


@router.get("/pages/{page_id}")
async def read_page(page_id: str, request: Request,
                    session_id: str = Query(min_length=1, max_length=200), revision_id: str = Query(default="", max_length=100)):
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "page_requires_user_session")
    store = await _store(request, session_id, store_type=PageStore)
    value = await _call(store.read_page, page_id, revision_id)
    value["display_style"] = await _effective_style(request)
    return value


@router.post("/pages/{page_id}/actions")
async def page_action(page_id: str, body: PageAction, request: Request,
                      session_id: str = Query(min_length=1, max_length=200)):
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "page_requires_user_session")
    store = await _store(request, session_id, write=True, store_type=PageStore)
    return await _call(store.action, page_id, session_id=session_id, **body.model_dump())


@router.get('/canvases/{canvas_id}/spatial-check')
async def spatial_check(canvas_id: str, request: Request, session_id: str, revision_id: str = '', suggest: bool = False):
    import json
    from ..presentation.solid_scene import cached_check
    store = await _store(request, session_id)
    value = await _call(store.read, canvas_id, revision_id)
    scene = next((p['solid_scene'] for p in value['parts'] if p.get('solid_scene')), None)
    if scene is None:
        raise HTTPException(422, '此版本没有受控三维部件数据，请使用部件库重新生成。')
    return await _call(cached_check, json.dumps(scene, sort_keys=True), suggest)
