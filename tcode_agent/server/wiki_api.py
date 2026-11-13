"""Paper Wiki HTTP routes. Access policy and background work live in wiki_service."""

import asyncio
import mimetypes
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .wiki_shares import WikiShareStore
from ._shared import (
    CurrentUserContext,
    ServerState,
    _request_has_valid_wiki_service_token,
    list_shareable_tenant_members,
    require_current_user,
    resolve_active_tenant_user,
)
from .wiki_service import (
    _paper_wiki_access_for_actor,
    _paper_wiki_access_for_request,
    _paper_wiki_accessible_sources,
    _paper_wiki_filter_proposal_for_scope,
    _paper_wiki_http_error,
    _paper_wiki_plugin_config,
    _paper_wiki_proposal_matches_scope,
    _paper_wiki_require_scoped_page,
    _paper_wiki_scope_kwargs,
    _paper_wiki_search_accessible_for_actor,
    _paper_wiki_store_for_request,
    _paper_wiki_strip_storage_details,
    _schedule_paper_wiki_regeneration,
    _schedule_paper_wiki_vector_rebuild,
)


router = APIRouter()


@router.get("/api/paper-wiki/shared-sources")
async def paper_wiki_shared_sources(request: Request):
    actor = require_current_user(request)
    return {"sources": _paper_wiki_accessible_sources(actor)}


@router.get("/api/paper-wiki/share-context")
async def paper_wiki_share_context(request: Request, workspace_id: str = ""):
    actor = require_current_user(request)
    access = _paper_wiki_access_for_actor(actor, workspace_id, allow_disabled=True)
    share_store = WikiShareStore(ServerState.get_workdir())
    members = [
        item
        for item in list_shareable_tenant_members(
            actor_user_id=actor.user_id,
            tenant_id=actor.tenant_id,
        )
        if str(item.get("user_id") or "") != actor.user_id
    ]
    return {
        "tenant_id": actor.tenant_id,
        "owner_user_id": actor.user_id,
        "workspace_id": access.owner_context.workspace_id,
        "members": members,
        "collections": share_store.list_collections(
            tenant_id=actor.tenant_id,
            owner_user_id=actor.user_id,
            workspace_id=access.owner_context.workspace_id,
        ),
        "shares": share_store.list_owner_shares(
            tenant_id=actor.tenant_id,
            owner_user_id=actor.user_id,
            workspace_id=access.owner_context.workspace_id,
        ),
    }


@router.post("/api/paper-wiki/share-collections")
async def paper_wiki_create_share_collection(payload: dict[str, Any], request: Request):
    actor = require_current_user(request)
    workspace_id = str(payload.get("workspace_id") or "")
    access = _paper_wiki_access_for_actor(actor, workspace_id, allow_disabled=True, require_write=True)
    page_ids = payload.get("page_ids") or []
    if not isinstance(page_ids, list):
        raise HTTPException(400, "page_ids must be a list")
    for page_id in page_ids:
        await asyncio.to_thread(access.store.get_page, str(page_id))
    collection = WikiShareStore(ServerState.get_workdir()).create_collection(
        tenant_id=actor.tenant_id,
        owner_user_id=actor.user_id,
        workspace_id=access.owner_context.workspace_id,
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        page_ids=page_ids,
        actor_user_id=actor.user_id,
    )
    return {"collection": collection}


@router.put("/api/paper-wiki/share-collections/{collection_id}")
async def paper_wiki_update_share_collection(
    collection_id: str, payload: dict[str, Any], request: Request
):
    actor = require_current_user(request)
    workspace_id = str(payload.get("workspace_id") or "")
    access = _paper_wiki_access_for_actor(actor, workspace_id, allow_disabled=True, require_write=True)
    page_ids = payload.get("page_ids") or []
    if not isinstance(page_ids, list):
        raise HTTPException(400, "page_ids must be a list")
    for page_id in page_ids:
        await asyncio.to_thread(access.store.get_page, str(page_id))
    collection = WikiShareStore(ServerState.get_workdir()).update_collection(
        tenant_id=actor.tenant_id,
        owner_user_id=actor.user_id,
        workspace_id=access.owner_context.workspace_id,
        collection_id=collection_id,
        actor_user_id=actor.user_id,
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        page_ids=page_ids,
    )
    return {"collection": collection}


@router.delete("/api/paper-wiki/share-collections/{collection_id}")
async def paper_wiki_delete_share_collection(
    collection_id: str, request: Request, workspace_id: str = ""
):
    actor = require_current_user(request)
    access = _paper_wiki_access_for_actor(actor, workspace_id, allow_disabled=True, require_write=True)
    WikiShareStore(ServerState.get_workdir()).delete_collection(
        tenant_id=actor.tenant_id,
        owner_user_id=actor.user_id,
        workspace_id=access.owner_context.workspace_id,
        collection_id=collection_id,
        actor_user_id=actor.user_id,
    )
    return {"status": "deleted", "collection_id": collection_id}


@router.post("/api/paper-wiki/shares")
async def paper_wiki_grant_share(payload: dict[str, Any], request: Request):
    actor = require_current_user(request)
    workspace_id = str(payload.get("workspace_id") or "")
    access = _paper_wiki_access_for_actor(actor, workspace_id, require_write=True)
    target_type = str(payload.get("target_type") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    if target_type == "page":
        await asyncio.to_thread(access.store.get_page, target_id)
    elif target_type == "entity_type":
        matching = await asyncio.to_thread(
            access.store.list_pages,
            page_type=target_id,
            include_archived=True,
            limit=1,
        )
        approved = await asyncio.to_thread(access.store.list_entity_types)
        known_types = {str(item.get("type_id") or "") for item in approved.get("types") or []}
        if int(matching.get("total") or 0) == 0 and target_id not in known_types:
            raise HTTPException(404, "Wiki entity type not found")
    grantees = payload.get("grantee_user_ids") or []
    if not isinstance(grantees, list):
        raise HTTPException(400, "grantee_user_ids must be a list")
    active_members = {
        str(item.get("user_id") or "")
        for item in list_shareable_tenant_members(
            actor_user_id=actor.user_id,
            tenant_id=actor.tenant_id,
        )
    }
    requested = {str(item or "").strip() for item in grantees if str(item or "").strip()}
    invalid = sorted(requested - active_members)
    if invalid:
        raise HTTPException(400, "All grantees must be active members of the current tenant")
    shares = WikiShareStore(ServerState.get_workdir()).grant(
        tenant_id=actor.tenant_id,
        owner_user_id=actor.user_id,
        workspace_id=access.owner_context.workspace_id,
        target_type=target_type,
        target_id=target_id,
        grantee_user_ids=requested,
        actor_user_id=actor.user_id,
    )
    return {"shares": shares, "count": len(shares)}


@router.delete("/api/paper-wiki/shares/{share_id}")
async def paper_wiki_revoke_share(
    share_id: str, request: Request, workspace_id: str = ""
):
    actor = require_current_user(request)
    access = _paper_wiki_access_for_actor(actor, workspace_id, allow_disabled=True, require_write=True)
    WikiShareStore(ServerState.get_workdir()).revoke(
        tenant_id=actor.tenant_id,
        owner_user_id=actor.user_id,
        workspace_id=access.owner_context.workspace_id,
        share_id=share_id,
        actor_user_id=actor.user_id,
    )
    return {"status": "revoked", "share_id": share_id}


@router.get("/api/paper-wiki/search-accessible")
async def paper_wiki_search_accessible(
    request: Request,
    q: str = "",
    workspace_id: str = "",
    limit: int = 8,
    include_archived: bool = False,
):
    try:
        return await _paper_wiki_search_accessible_for_actor(
            require_current_user(request),
            workspace_id=workspace_id,
            query=q,
            limit=limit,
            include_archived=include_archived,
        )
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


def _require_wiki_service_actor(payload: dict[str, Any], request: Request) -> CurrentUserContext:
    if not _request_has_valid_wiki_service_token(request):
        raise HTTPException(401, "Valid Wiki service token is required")
    tenant_id = str(payload.get("tenant_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not tenant_id or not user_id:
        raise HTTPException(400, "tenant_id and user_id are required")
    return resolve_active_tenant_user(tenant_id=tenant_id, user_id=user_id)


@router.post("/api/paper-wiki/service/search")
async def paper_wiki_service_search(payload: dict[str, Any], request: Request):
    """Trusted external-Agent search, constrained to one active user's access graph."""

    try:
        actor = _require_wiki_service_actor(payload, request)
        return await _paper_wiki_search_accessible_for_actor(
            actor,
            workspace_id=str(payload.get("workspace_id") or ""),
            query=str(payload.get("query") or payload.get("q") or ""),
            limit=int(payload.get("limit") or 8),
            include_archived=bool(payload.get("include_archived", False)),
        )
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/service/pages/{page_id}")
async def paper_wiki_service_page(
    page_id: str, payload: dict[str, Any], request: Request
):
    """Trusted external-Agent page read with the same revocation checks as the UI."""

    try:
        actor = _require_wiki_service_actor(payload, request)
        access = _paper_wiki_access_for_actor(
            actor,
            str(payload.get("workspace_id") or ""),
            source_id=str(payload.get("source_id") or ""),
        )
        page = await asyncio.to_thread(
            access.store.get_page,
            page_id,
            include_history=bool(payload.get("include_history", False)),
        )
        _paper_wiki_require_scoped_page(access, page)
        # Filesystem materialization details are never part of the service API.
        page = _paper_wiki_strip_storage_details(page)
        page["access"] = (
            {**access.scope.as_dict(), "mode": "shared_read_only"}
            if access.scope
            else {
                "mode": "owner",
                "read_only": False,
                "workspace_id": access.owner_context.workspace_id,
            }
        )
        return page
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/stats")
async def paper_wiki_stats(
    request: Request, workspace_id: str = "", source_id: str = ""
):
    try:
        access = _paper_wiki_access_for_request(
            request,
            workspace_id,
            source_id=source_id,
            allow_disabled=True,
        )
        store = access.store
        result = await asyncio.to_thread(
            store.stats_for_scope,
            **_paper_wiki_scope_kwargs(access.scope),
        ) if access.scope else await asyncio.to_thread(store.stats)
        config = _paper_wiki_plugin_config(store.workdir)
        result["enabled"] = bool(config.get("__plugin_enabled__", False)) and bool(
            config.get("library_wiki_enabled", True)
        )
        result["features"] = {
            "regeneration_enabled": bool(
                config.get("library_wiki_regeneration_enabled", True)
            ),
            "markdown_sync_enabled": bool(
                config.get("library_wiki_markdown_sync_enabled", True)
            ),
            "markdown_sync_interval_seconds": max(
                2,
                min(
                    300,
                    int(config.get("library_wiki_markdown_sync_interval_seconds", 5) or 5),
                ),
            ),
            "vector_search_enabled": bool(
                config.get("library_wiki_vector_search_enabled", True)
            ),
        }
        result["access"] = (
            {**access.scope.as_dict(), "mode": "shared_read_only"}
            if access.scope
            else {
                "mode": "owner",
                "read_only": False,
                "workspace_id": access.owner_context.workspace_id,
            }
        )
        if (
            not access.read_only
            and result["enabled"]
            and (result.get("vector") or {}).get("enabled")
            and (result.get("vector") or {}).get("available")
            and (result.get("vector") or {}).get("dirty")
        ):
            vector_job = _schedule_paper_wiki_vector_rebuild(
                store,
                actor="wiki_stats",
            )
            result["vector_job_id"] = vector_job.get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/entity-types")
async def paper_wiki_entity_types(
    request: Request, workspace_id: str = "", source_id: str = "", include_all: bool = False
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        if access.scope:
            return await asyncio.to_thread(
                access.store.list_entity_types_for_scope,
                **_paper_wiki_scope_kwargs(access.scope),
            )
        return await asyncio.to_thread(access.store.list_entity_types, include_all=include_all)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/entity-types")
async def request_entity_type(payload: dict[str, Any], request: Request):
    try:
        store, context = _paper_wiki_store_for_request(request, str(payload.get("workspace_id") or ""))
        return await asyncio.to_thread(store.save_entity_type, str(payload.get("type_id") or ""), str(payload.get("display_name") or ""), actor=context.user_id)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/entity-types/{type_id}/review")
async def review_entity_type(type_id: str, payload: dict[str, Any], request: Request):
    try:
        store, context = _paper_wiki_store_for_request(request, str(payload.get("workspace_id") or ""))
        return await asyncio.to_thread(store.review_entity_type, type_id, str(payload.get("status") or ""), actor=context.user_id, reason=str(payload.get("reason") or ""), expected_status=str(payload.get("expected_status") or ""))
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/pages/{page_id}/note-draft")
async def get_note_draft(page_id: str, request: Request, workspace_id: str = ""):
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(store.note_draft, page_id, actor=context.user_id)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.put("/api/paper-wiki/pages/{page_id}/note-draft")
async def save_note_draft(page_id: str, payload: dict[str, Any], request: Request):
    try:
        store, context = _paper_wiki_store_for_request(request, str(payload.get("workspace_id") or ""))
        return await asyncio.to_thread(store.note_draft, page_id, actor=context.user_id, content=str(payload.get("content") or ""), base_revision=int(payload.get("base_revision") or 0), expected_revision=int(payload.get("expected_revision") or 0), delete=bool(payload.get("delete", False)))
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/pages")
async def paper_wiki_pages(
    request: Request,
    workspace_id: str = "",
    status: str = "",
    page_type: str = "",
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    source_id: str = "",
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        return await asyncio.to_thread(
            access.store.list_pages,
            status=status,
            page_type=page_type,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            **_paper_wiki_scope_kwargs(access.scope),
        )
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/search")
async def paper_wiki_search(
    request: Request,
    q: str = "",
    workspace_id: str = "",
    limit: int = 8,
    status: str = "",
    include_archived: bool = False,
    source_id: str = "",
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        store = access.store
        context = access.owner_context
        config = _paper_wiki_plugin_config(store.workdir)
        if not access.read_only and bool(config.get("library_wiki_markdown_sync_enabled", True)):
            try:
                await asyncio.to_thread(
                    store.reconcile_external_edits,
                    actor=context.user_id,
                    force=False,
                    dry_run=False,
                    max_pages=200,
                )
            except Exception as sync_exc:
                print(
                    "[paper_wiki] search-time Markdown reconcile skipped: "
                    f"{type(sync_exc).__name__}: {sync_exc}"
                )
        if not access.read_only and store.vector_status().get("dirty"):
            _schedule_paper_wiki_vector_rebuild(store, actor="wiki_search")
        result = await asyncio.to_thread(
            store.search,
            q,
            limit=limit,
            status=status,
            include_archived=include_archived,
            **_paper_wiki_scope_kwargs(access.scope),
        )
        return _paper_wiki_strip_storage_details(result) if access.scope else result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/pages/{page_id}")
async def paper_wiki_page(
    page_id: str,
    request: Request,
    workspace_id: str = "",
    include_history: bool = False,
    source_id: str = "",
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        page = await asyncio.to_thread(
            access.store.get_page,
            page_id,
            include_history=include_history,
        )
        _paper_wiki_require_scoped_page(access, page)
        if access.scope:
            page["access"] = {**access.scope.as_dict(), "mode": "shared_read_only"}
            page = _paper_wiki_strip_storage_details(page)
        else:
            page.update(await asyncio.to_thread(access.store.get_page_user_notes, page_id))
        return page
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/pages")
async def paper_wiki_create_page(payload: dict[str, Any], request: Request):
    raise HTTPException(
        403,
        "Paper Wiki pages are Agent-managed; users may edit page notes only",
    )


@router.put("/api/paper-wiki/pages/{page_id}")
async def paper_wiki_update_page(page_id: str, payload: dict[str, Any], request: Request):
    raise HTTPException(
        403,
        "Agent-derived Wiki fields are read-only; use the user-notes endpoint",
    )


@router.put("/api/paper-wiki/pages/{page_id}/user-notes")
async def paper_wiki_update_page_user_notes(
    page_id: str,
    payload: dict[str, Any],
    request: Request,
):
    workspace_id = str(payload.get("workspace_id", "") or "").strip()
    expected_revision = payload.get("expected_notes_revision")
    user_notes = payload.get("user_notes", "")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(
            store.save_page_user_notes,
            page_id,
            str(user_notes or ""),
            actor=context.user_id,
            expected_revision=(
                int(expected_revision) if expected_revision is not None else None
            ),
        )
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/pages/{page_id}/archive")
async def paper_wiki_archive_page(page_id: str, payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    expected_revision = payload.get("expected_revision")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        result = await asyncio.to_thread(
            store.archive_page,
            page_id,
            actor=context.user_id,
            expected_revision=(int(expected_revision) if expected_revision is not None else None),
        )
        result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
            store, actor=context.user_id
        ).get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.delete("/api/paper-wiki/pages/{page_id}")
async def paper_wiki_purge_page(page_id: str, payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    expected_revision = payload.get("expected_revision")
    confirmation = str(payload.get("confirmation") or "").strip()
    if confirmation != "yes":
        raise HTTPException(400, "Enter lowercase yes to confirm permanent deletion")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        page = await asyncio.to_thread(store.get_page, page_id)
        if str(page.get("status") or "") != "archived":
            raise HTTPException(409, "Archive the Wiki page before permanently deleting it")
        current_revision = int(page.get("revision") or 0)
        if expected_revision is not None and current_revision != int(expected_revision):
            raise HTTPException(
                409,
                f"page revision conflict: expected {expected_revision}, current {current_revision}",
            )

        # Revoke access first. If the subsequent content purge fails, the page is
        # still archived and recoverable, while no stale share remains active.
        share_cleanup = await asyncio.to_thread(
            WikiShareStore(ServerState.get_workdir()).purge_page_references,
            tenant_id=context.tenant_id,
            owner_user_id=context.user_id,
            workspace_id=context.workspace_id,
            page_id=page_id,
            actor_user_id=context.user_id,
        )
        result = await asyncio.to_thread(
            store.purge_page,
            page_id,
            actor=context.user_id,
            expected_revision=(
                int(expected_revision) if expected_revision is not None else None
            ),
        )
        result["share_cleanup"] = share_cleanup
        try:
            result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
                store, actor=context.user_id
            ).get("job_id")
        except Exception as vector_exc:
            result["vector_job_id"] = ""
            result.setdefault("cleanup_warnings", []).append(
                f"Failed to schedule vector index refresh: {type(vector_exc).__name__}"
            )
        if result.get("cleanup_warnings"):
            print(
                "[paper_wiki] page purge completed with cleanup warnings: "
                + "; ".join(str(item) for item in result["cleanup_warnings"])
            )
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/pages/{page_id}/restore")
async def paper_wiki_restore_page(page_id: str, payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    expected_revision = payload.get("expected_revision")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        result = await asyncio.to_thread(
            store.restore_page,
            page_id,
            actor=context.user_id,
            expected_revision=(int(expected_revision) if expected_revision is not None else None),
        )
        result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
            store, actor=context.user_id
        ).get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/pages/{page_id}/rollback")
async def paper_wiki_rollback_page(page_id: str, payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    try:
        revision = int(payload.get("revision") or 0)
        if revision <= 0:
            raise ValueError("revision must be positive")
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        result = await asyncio.to_thread(
            store.rollback_page,
            page_id,
            revision,
            actor=context.user_id,
        )
        result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
            store, actor=context.user_id
        ).get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/claims/{claim_id}/evidence")
async def paper_wiki_claim_evidence(
    claim_id: str,
    request: Request,
    workspace_id: str = "",
    source_id: str = "",
):
    from ..evidence.presentation import present_evidence_record

    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        result = await asyncio.to_thread(access.store.claim_evidence, claim_id)
        _paper_wiki_require_scoped_page(access, result.get("claim") or {})
        result["evidence"] = [
            present_evidence_record(record) for record in result.get("evidence") or []
        ]
        return _paper_wiki_strip_storage_details(result) if access.scope else result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/evidence")
async def paper_wiki_evidence(payload: dict[str, Any], request: Request):
    from ..evidence.presentation import present_evidence_record

    workspace_id = str(payload.get("workspace_id") or "").strip()
    source_id = str(payload.get("source_id") or "").strip()
    evidence_ids = payload.get("evidence_ids") or []
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise HTTPException(400, "evidence_ids must be a non-empty list")
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        requested_ids = [str(item) for item in evidence_ids[:50]]
        if access.scope:
            allowed = await asyncio.to_thread(
                access.store.allowed_evidence_ids,
                **_paper_wiki_scope_kwargs(access.scope),
            )
            requested_ids = [item for item in requested_ids if item in allowed]
        records = await asyncio.to_thread(
            access.store.evidence_records, requested_ids, limit=50
        )
        found_ids = {
            str(record.get("evidence_id") or "").strip()
            for record in records
            if str(record.get("evidence_id") or "").strip()
        }
        missing_ids = [item for item in requested_ids if item not in found_ids]
        response = {
            "evidence_ids": requested_ids,
            "requested_evidence_ids": requested_ids,
            "missing_evidence_ids": missing_ids,
            "evidence_state": (
                "none"
                if not requested_ids
                else "complete"
                if not missing_ids
                else "partial"
                if found_ids
                else "missing"
            ),
            "evidence": [present_evidence_record(record) for record in records],
        }
        return _paper_wiki_strip_storage_details(response) if access.scope else response
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/evidence/{evidence_id}/images/{image_index}")
async def paper_wiki_evidence_image(
    evidence_id: str,
    image_index: int,
    request: Request,
    workspace_id: str = "",
    source_id: str = "",
):
    from ..evidence.presentation import resolve_evidence_image

    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        normalized_id = str(evidence_id or "").strip()
        if not normalized_id:
            raise HTTPException(400, "evidence_id is required")
        if access.scope:
            allowed = await asyncio.to_thread(
                access.store.allowed_evidence_ids,
                **_paper_wiki_scope_kwargs(access.scope),
            )
            if normalized_id not in allowed:
                raise HTTPException(404, "Evidence image not found")
        records = await asyncio.to_thread(
            access.store.evidence_records, [normalized_id], limit=1
        )
        if not records:
            raise HTTPException(404, "Evidence not found")
        try:
            target = resolve_evidence_image(
                records[0], image_index, workdir=str(access.store.workdir)
            )
        except (IndexError, FileNotFoundError):
            raise HTTPException(404, "Evidence image not found")
        except PermissionError:
            raise HTTPException(403, "Evidence image is outside the workspace")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        response = FileResponse(
            target,
            media_type=media_type,
            filename=target.name,
            content_disposition_type="inline",
        )
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/proposals")
async def paper_wiki_proposals(
    request: Request,
    workspace_id: str = "",
    status: str = "",
    limit: int = 50,
    source_id: str = "",
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        result = await asyncio.to_thread(
            access.store.list_proposals, status=status, limit=limit
        )
        if access.scope:
            visible: list[dict[str, Any]] = []
            for summary in list(result.get("proposals") or []):
                proposal = await asyncio.to_thread(
                    access.store.get_proposal, str(summary.get("proposal_id") or "")
                )
                if _paper_wiki_proposal_matches_scope(access.store, proposal, access.scope):
                    filtered = _paper_wiki_filter_proposal_for_scope(
                        access.store, proposal, access.scope
                    )
                    filtered.pop("operations", None)
                    filtered.pop("review_model", None)
                    filtered.pop("applied_operations", None)
                    visible.append(filtered)
            return {"proposals": visible, "count": len(visible), "read_only": True}
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/proposals/{proposal_id}")
async def paper_wiki_proposal(
    proposal_id: str,
    request: Request,
    workspace_id: str = "",
    source_id: str = "",
):
    try:
        access = _paper_wiki_access_for_request(
            request, workspace_id, source_id=source_id
        )
        proposal = await asyncio.to_thread(access.store.get_proposal, proposal_id)
        if not _paper_wiki_proposal_matches_scope(access.store, proposal, access.scope):
            raise HTTPException(404, "Wiki proposal not found or no longer shared")
        if access.scope:
            proposal = _paper_wiki_filter_proposal_for_scope(
                access.store, proposal, access.scope
            )
        return proposal
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/proposals/{proposal_id}/approve")
async def paper_wiki_approve_proposal(
    proposal_id: str,
    payload: dict[str, Any],
    request: Request,
):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    proposal_hash = str(payload.get("proposal_hash") or "").strip()
    if not proposal_hash:
        raise HTTPException(400, "proposal_hash is required")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        result = await asyncio.to_thread(
            store.approve_and_apply_proposal,
            proposal_id,
            actor=context.user_id,
            expected_hash=proposal_hash,
            comment=str(payload.get("comment") or ""),
            selection=(payload.get("selection") if isinstance(payload.get("selection"), dict) else None),
        )
        config = _paper_wiki_plugin_config(store.workdir)
        if bool(config.get("library_wiki_lint_after_apply", True)):
            result["lint"] = await asyncio.to_thread(store.lint, persist=True)
        result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
            store, actor=context.user_id
        ).get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/proposals/{proposal_id}/reject")
async def paper_wiki_reject_proposal(
    proposal_id: str,
    payload: dict[str, Any],
    request: Request,
):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "rejection reason is required")
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(
            store.reject_proposal,
            proposal_id,
            actor=context.user_id,
            reason=reason,
        )
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/proposals/{proposal_id}/regenerate")
async def paper_wiki_regenerate_proposal(
    proposal_id: str,
    payload: dict[str, Any],
    request: Request,
):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    instructions = str(payload.get("instructions") or "").strip()
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        config = _paper_wiki_plugin_config(store.workdir)
        if not bool(config.get("library_wiki_regeneration_enabled", True)):
            raise ValueError("Paper Wiki LLM regeneration is disabled")
        job = _schedule_paper_wiki_regeneration(
            store,
            proposal_id=proposal_id,
            actor=context.user_id,
            instructions=instructions,
        )
        return {
            "status": "queued",
            "job": job,
            "proposal_id": proposal_id,
        }
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/jobs")
async def paper_wiki_jobs(
    request: Request,
    workspace_id: str = "",
    status: str = "",
    limit: int = 30,
):
    try:
        store, _ = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(store.list_jobs, status=status, limit=limit)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/jobs/{job_id}")
async def paper_wiki_job(job_id: str, request: Request, workspace_id: str = ""):
    try:
        store, _ = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(store.get_job, job_id)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/reconcile")
async def paper_wiki_reconcile(payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        config = _paper_wiki_plugin_config(store.workdir)
        if not bool(config.get("library_wiki_markdown_sync_enabled", True)):
            raise ValueError("Paper Wiki Markdown integrity validation is disabled")
        result = await asyncio.to_thread(
            store.reconcile_external_edits,
            actor=context.user_id,
            force=bool(payload.get("force", False)),
            dry_run=bool(payload.get("dry_run", False)),
            max_pages=max(1, min(10_000, int(payload.get("max_pages") or 2000))),
        )
        if int(result.get("changed_count") or 0) > 0:
            result["vector_job_id"] = _schedule_paper_wiki_vector_rebuild(
                store, actor=context.user_id
            ).get("job_id")
        return result
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.get("/api/paper-wiki/vector/status")
async def paper_wiki_vector_status(request: Request, workspace_id: str = ""):
    try:
        store, _ = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(store.vector_status)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/vector/rebuild")
async def paper_wiki_vector_rebuild(payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    try:
        store, context = _paper_wiki_store_for_request(request, workspace_id)
        job = _schedule_paper_wiki_vector_rebuild(
            store,
            actor=context.user_id,
            force=bool(payload.get("force", False)),
            force_embeddings=bool(payload.get("force_embeddings", False)),
        )
        return {
            "status": "queued" if job.get("job_id") else str(job.get("status") or "unavailable"),
            "job": job,
        }
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc


@router.post("/api/paper-wiki/lint")
async def paper_wiki_lint(payload: dict[str, Any], request: Request):
    workspace_id = str(payload.get("workspace_id") or "").strip()
    try:
        store, _ = _paper_wiki_store_for_request(request, workspace_id)
        return await asyncio.to_thread(store.lint, persist=True)
    except Exception as exc:
        raise _paper_wiki_http_error(exc) from exc

