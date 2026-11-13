"""Wiki access scopes, background indexing and chat context resolution."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .paper_wiki_loader import paper_wiki_module
from .wiki_shares import WikiShareScope, WikiShareStore
from ._shared import (
    ConfigManager,
    CurrentUserContext,
    ServerState,
    require_current_user,
    resolve_active_tenant_user,
    tenant_user_summaries,
    workdir_utils,
)
from .workspace_scope import (
    _resolve_user_workdir_path,
)


_PAPER_WIKI_VECTOR_TASKS: dict[str, dict[str, Any]] = {}
_PAPER_WIKI_REGENERATION_TASKS: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class _PaperWikiAccess:
    store: Any
    owner_context: workdir_utils.WorkspaceContext
    actor_user_id: str
    scope: WikiShareScope | None = None

    @property
    def read_only(self) -> bool:
        return self.scope is not None


def _paper_wiki_store_for_context(
    context: workdir_utils.WorkspaceContext,
    *,
    allow_disabled: bool = False,
):
    """Resolve a server-controlled Wiki store from a logical workspace identity."""
    wiki = paper_wiki_module()
    PaperWikiStore = wiki.PaperWikiStore
    WikiValidationError = wiki.WikiValidationError

    workdir_utils.assert_workspace_active(ServerState.get_workdir(), context)
    target_workdir = _resolve_user_workdir_path(context.workspace_id, context)
    config = _paper_wiki_plugin_config(target_workdir)
    enabled = bool(config.get("__plugin_enabled__", False)) and bool(
        config.get("library_wiki_enabled", True)
    )
    if not allow_disabled and not enabled:
        raise WikiValidationError(
            "Paper Wiki is disabled; set library_wiki_enabled=true and restart the server"
        )
    library_root = (
        target_workdir
        / ".tcode"
        / "plugins"
        / "paper_research_workflow"
        / "library"
    )
    return PaperWikiStore(
            library_root=library_root,
            workdir=target_workdir.as_posix(),
            vector_search_enabled=bool(
                config.get("library_wiki_vector_search_enabled", True)
            ),
            vector_index_min_pages=max(
                1,
                int(config.get("library_wiki_vector_index_min_pages", 1) or 1),
            ),
            vector_search_weight=max(
                0.0,
                min(
                    1.0,
                    float(config.get("library_wiki_vector_search_weight", 0.35) or 0.35),
                ),
            ),
        )


def _paper_wiki_access_for_actor(
    actor: CurrentUserContext,
    workspace_id: str = "",
    *,
    source_id: str = "",
    allow_disabled: bool = False,
    require_write: bool = False,
) -> _PaperWikiAccess:
    normalized_source = str(source_id or "").strip()
    if normalized_source:
        if require_write:
            raise HTTPException(403, "Shared Wiki sources are read-only")
        scope = WikiShareStore(ServerState.get_workdir()).resolve_source(
            tenant_id=actor.tenant_id,
            grantee_user_id=actor.user_id,
            source_id=normalized_source,
        )
        # A grant is necessary but never sufficient: both the tenant member and
        # workspace lifecycle are checked again on every read.
        resolve_active_tenant_user(
            tenant_id=scope.tenant_id,
            user_id=scope.owner_user_id,
        )
        context = workdir_utils.WorkspaceContext(
            tenant_id=scope.tenant_id,
            user_id=scope.owner_user_id,
            workspace_id=scope.workspace_id,
        )
        return _PaperWikiAccess(
            store=_paper_wiki_store_for_context(context, allow_disabled=allow_disabled),
            owner_context=context,
            actor_user_id=actor.user_id,
            scope=scope,
        )
    context = workdir_utils.resolve_workspace_context(
        tenant_id=actor.tenant_id,
        user_id=actor.user_id,
        workspace_id=workspace_id,
    )
    return _PaperWikiAccess(
        store=_paper_wiki_store_for_context(context, allow_disabled=allow_disabled),
        owner_context=context,
        actor_user_id=actor.user_id,
    )


def _paper_wiki_access_for_request(
    request: Request,
    workspace_id: str = "",
    *,
    source_id: str = "",
    allow_disabled: bool = False,
    require_write: bool = False,
) -> _PaperWikiAccess:
    return _paper_wiki_access_for_actor(
        require_current_user(request),
        workspace_id,
        source_id=source_id,
        allow_disabled=allow_disabled,
        require_write=require_write,
    )


def _paper_wiki_store_for_request(
    request: Request,
    workspace_id: str = "",
    *,
    allow_disabled: bool = False,
):
    """Backward-compatible owner-only resolver used by Wiki mutation routes."""

    access = _paper_wiki_access_for_request(
        request,
        workspace_id,
        allow_disabled=allow_disabled,
        require_write=True,
    )
    return access.store, access.owner_context


def _paper_wiki_plugin_config(workdir: Path) -> dict[str, Any]:
    path = Path(
        ConfigManager.get_config_root_file_path(
            "plugins.json",
            str(workdir),
            prefer_effective=True,
        )
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for item in payload.get("plugins", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() == "paper-research-workflow":
            config = dict(item.get("config") or {})
            config["__plugin_enabled__"] = bool(item.get("enabled", True))
            return config
    return {}


def _paper_wiki_http_error(exc: Exception) -> HTTPException:
    wiki = paper_wiki_module()
    WikiConflictError = wiki.WikiConflictError
    WikiNotFoundError = wiki.WikiNotFoundError
    WikiValidationError = wiki.WikiValidationError

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, WikiNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, WikiConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, WikiValidationError):
        return HTTPException(400, str(exc))
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(400, str(exc))
    print(f"[paper_wiki] request failed: {type(exc).__name__}: {exc}")
    return HTTPException(500, "Paper Wiki internal error; see server logs for details")


def _schedule_paper_wiki_vector_rebuild(
    store,
    *,
    actor: str,
    force: bool = False,
    force_embeddings: bool = False,
) -> dict[str, Any]:
    vector_status = store.vector_status()
    if not bool(vector_status.get("enabled")):
        return {"job_id": "", "status": "disabled", "job_type": "vector_rebuild"}
    if not bool(vector_status.get("available")):
        return {"job_id": "", "status": "unavailable", "job_type": "vector_rebuild"}
    key = store.root.as_posix()
    existing = _PAPER_WIKI_VECTOR_TASKS.get(key)
    if existing and not existing["task"].done():
        return dict(existing["job"])
    job = store.create_job(
        job_type="vector_rebuild",
        payload={"actor": actor, "force": bool(force), "force_embeddings": bool(force_embeddings)},
        message="等待重建 Wiki 向量索引",
    )
    job_id = str(job["job_id"])

    async def run() -> None:
        try:
            await asyncio.sleep(0.6)
            await asyncio.to_thread(
                store.update_job,
                job_id,
                status="running",
                progress=0.15,
                message="正在增量更新页面向量与索引",
            )
            result = await asyncio.to_thread(store.rebuild_vector_index, force=force, **(
                {"force_embeddings": True} if force_embeddings else {}
            ))
            message = (
                "Wiki 页面数未达到向量索引阈值，继续使用 SQLite FTS5"
                if result.get("status") == "below_threshold"
                else "Wiki 向量索引已更新"
            )
            await asyncio.to_thread(
                store.update_job,
                job_id,
                status="completed",
                progress=1.0,
                message=message,
                result=result,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(
                    store.update_job,
                    job_id,
                    status="failed",
                    progress=1.0,
                    message="Wiki 向量索引任务因服务停止而取消",
                    result={"error_type": "CancelledError"},
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            print(f"[paper_wiki] vector rebuild failed: {type(exc).__name__}: {exc}")
            try:
                await asyncio.to_thread(
                    store.update_job,
                    job_id,
                    status="failed",
                    progress=1.0,
                    message=f"{type(exc).__name__}: 向量索引重建失败，请查看服务端日志",
                    result={"error_type": type(exc).__name__},
                )
            except Exception:
                pass
        finally:
            _PAPER_WIKI_VECTOR_TASKS.pop(key, None)

    task = asyncio.get_running_loop().create_task(run())
    _PAPER_WIKI_VECTOR_TASKS[key] = {"task": task, "job": job}
    return job


def _schedule_paper_wiki_regeneration(
    store,
    *,
    proposal_id: str,
    actor: str,
    instructions: str,
) -> dict[str, Any]:
    PaperWikiLLMRegenerator = paper_wiki_module().PaperWikiLLMRegenerator

    key = f"{store.root.as_posix()}::{proposal_id}"
    existing = _PAPER_WIKI_REGENERATION_TASKS.get(key)
    if existing and not existing["task"].done():
        return dict(existing["job"])
    proposal = store.get_proposal(proposal_id)
    if str(proposal.get("status") or "") != "pending_review":
        raise ValueError(f"proposal is not pending: {proposal.get('status')}")
    job = store.create_job(
        job_type="regenerate_proposal",
        payload={
            "proposal_id": proposal_id,
            "actor": actor,
            "instructions": str(instructions or "")[:6000],
        },
        message="等待 LLM 重新生成 Wiki Proposal",
    )
    job_id = str(job["job_id"])

    async def run() -> None:
        try:
            await asyncio.to_thread(
                store.update_job,
                job_id,
                status="running",
                progress=0.1,
                message="正在整理原 Proposal 和 Evidence",
            )
            replacement = await PaperWikiLLMRegenerator(store=store).regenerate(
                proposal_id,
                instructions=instructions,
                created_by=actor,
            )
            await asyncio.to_thread(
                store.update_job,
                job_id,
                status="completed",
                progress=1.0,
                message="LLM 已生成新的待审核 Proposal",
                result={
                    "proposal_id": proposal_id,
                    "replacement_proposal_id": replacement.get("proposal_id"),
                    "summary": replacement.get("regeneration_summary"),
                },
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(
                    store.update_job,
                    job_id,
                    status="failed",
                    progress=1.0,
                    message="LLM 重新生成任务因服务停止而取消",
                    result={"error_type": "CancelledError"},
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            print(f"[paper_wiki] regeneration failed: {type(exc).__name__}: {exc}")
            try:
                await asyncio.to_thread(
                    store.update_job,
                    job_id,
                    status="failed",
                    progress=1.0,
                    message=f"{type(exc).__name__}: LLM 重新生成失败，请查看服务端日志",
                    result={"proposal_id": proposal_id, "error_type": type(exc).__name__},
                )
            except Exception:
                pass
        finally:
            _PAPER_WIKI_REGENERATION_TASKS.pop(key, None)

    task = asyncio.get_running_loop().create_task(run())
    _PAPER_WIKI_REGENERATION_TASKS[key] = {"task": task, "job": job}
    return job


def _paper_wiki_scope_kwargs(scope: WikiShareScope | None) -> dict[str, Any]:
    if scope is None:
        return {}
    return {
        "allowed_page_ids": scope.page_ids,
        "allowed_page_types": scope.page_types,
    }


def _paper_wiki_strip_storage_details(value: Any) -> Any:
    hidden = {
        "figure_image_paths",
        "image_path",
        "markdown_path",
        "page_image_path",
        "page_image_paths",
        "workdir",
        "workspace_root",
        "library_root",
        "record_path",
        "pdf_path",
        "source_path",
        "table_image_path",
        "tables_extract_path",
        "user_notes",
        "user_notes_revision",
        "user_notes_updated_at",
        "user_notes_updated_by",
        "visual_manifest_path",
    }
    if isinstance(value, dict):
        return {
            str(key): _paper_wiki_strip_storage_details(item)
            for key, item in value.items()
            if str(key) not in hidden
        }
    if isinstance(value, list):
        return [_paper_wiki_strip_storage_details(item) for item in value]
    return value


def _paper_wiki_require_scoped_page(
    access: _PaperWikiAccess,
    page: dict[str, Any],
) -> None:
    if access.scope is not None and not access.scope.permits_page(page):
        raise HTTPException(404, "Wiki page not found or no longer shared")


PAPER_WIKI_CHAT_CONTEXT_SCHEMA = "tcode.wiki-chat-context.v1"


def _normalize_paper_wiki_chat_context(
    value: Any,
    *,
    actor: CurrentUserContext,
    workspace_id: str,
) -> dict[str, Any]:
    """Resolve a client-side Wiki selection to a server-authorized locator.

    The chat context intentionally contains no page body or Claims. It only
    disambiguates phrases such as "这个 Wiki"; the Agent must still use the
    existing Wiki read tools to load the latest bounded content.
    """

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("wiki_context must be an object")
    raw_page = value.get("page")
    if raw_page in (None, ""):
        return {
            "schema": PAPER_WIKI_CHAT_CONTEXT_SCHEMA,
            "mode": "sticky",
            "page": None,
        }
    if not isinstance(raw_page, dict):
        raise ValueError("wiki_context.page must be an object or null")
    page_id = str(raw_page.get("page_id") or "").strip()
    source_id = str(raw_page.get("source_id") or "").strip()
    if not page_id:
        raise ValueError("wiki_context.page.page_id is required")
    access = _paper_wiki_access_for_actor(
        actor,
        workspace_id,
        source_id=source_id,
    )
    page = access.store.get_page(page_id, include_history=False)
    _paper_wiki_require_scoped_page(access, page)
    return {
        "schema": PAPER_WIKI_CHAT_CONTEXT_SCHEMA,
        "mode": "sticky",
        "page": {
            "page_id": str(page.get("page_id") or page_id),
            "source_id": source_id,
            "title": str(page.get("title") or "Wiki 页面")[:240],
            "page_type": str(page.get("page_type") or "")[:120],
            "status": str(page.get("status") or "")[:80],
            "revision": max(0, int(page.get("revision") or 0)),
            "access": "shared_read_only" if access.read_only else "owner",
            "owner_user_id": access.owner_context.user_id,
            "owner_workspace_id": access.owner_context.workspace_id,
        },
    }


def _paper_wiki_operation_matches_scope(
    store: Any,
    operation: dict[str, Any],
    scope: WikiShareScope | None,
) -> bool:
    if scope is None:
        return True
    operation_type = str(operation.get("type") or "")
    page_payload = operation.get("page") if isinstance(operation.get("page"), dict) else {}
    page_id = str(page_payload.get("page_id") or operation.get("page_id") or "").strip()
    page_type = str(page_payload.get("page_type") or "").strip()
    if page_id in scope.page_ids or page_type in scope.page_types:
        return True
    if page_id and operation_type != "upsert_page":
        try:
            return scope.permits_page(store.get_page(page_id))
        except Exception:
            return False
    return False


def _paper_wiki_proposal_matches_scope(
    store: Any,
    proposal: dict[str, Any],
    scope: WikiShareScope | None,
) -> bool:
    if scope is None:
        return True
    return any(
        _paper_wiki_operation_matches_scope(store, operation, scope)
        for operation in list(proposal.get("operations") or [])
    )


def _paper_wiki_filter_proposal_for_scope(
    store: Any,
    proposal: dict[str, Any],
    scope: WikiShareScope,
) -> dict[str, Any]:
    """Remove operations and metadata belonging only to unshared pages."""

    result = dict(proposal)
    indexed_operations = [
        (index, dict(operation))
        for index, operation in enumerate(list(proposal.get("operations") or []))
        if _paper_wiki_operation_matches_scope(store, operation, scope)
    ]
    permitted_indexes = {index for index, _ in indexed_operations}
    index_mapping = {
        original_index: filtered_index
        for filtered_index, (original_index, _operation) in enumerate(indexed_operations)
    }
    permitted_page_ids: set[str] = set()
    permitted_doc_ids: set[str] = set()
    for _index, operation in indexed_operations:
        page = operation.get("page") if isinstance(operation.get("page"), dict) else {}
        before = operation.get("before") if isinstance(operation.get("before"), dict) else {}
        page_id = str(page.get("page_id") or operation.get("page_id") or "").strip()
        if page_id:
            permitted_page_ids.add(page_id)
        for payload in (page, before):
            permitted_doc_ids.update(
                str(item) for item in payload.get("source_doc_ids") or [] if str(item)
            )
        if page_id and not page:
            try:
                current = store.get_page(page_id)
                permitted_doc_ids.update(
                    str(item) for item in current.get("source_doc_ids") or [] if str(item)
                )
            except Exception:
                pass
    result["operations"] = [operation for _index, operation in indexed_operations]
    result["operation_count"] = len(indexed_operations)
    result["base_revisions"] = {
        page_id: revision
        for page_id, revision in dict(proposal.get("base_revisions") or {}).items()
        if page_id in permitted_page_ids
    }
    result["trigger_doc_ids"] = [
        doc_id
        for doc_id in list(proposal.get("trigger_doc_ids") or [])
        if str(doc_id) in permitted_doc_ids
    ]
    review_model = dict(proposal.get("review_model") or {})
    if review_model:
        review_model["operations"] = [
            {**item, "operation_index": index_mapping[int(item.get("operation_index", -1))]}
            for item in list(review_model.get("operations") or [])
            if int(item.get("operation_index", -1)) in permitted_indexes
        ]
        review_model["change_count"] = sum(
            len(item.get("claim_changes") or [])
            for item in review_model["operations"]
        )
        result["review_model"] = review_model
    review_selection = dict(proposal.get("review_selection") or {})
    if review_selection:
        review_selection["operations"] = [
            {**item, "operation_index": index_mapping[int(item.get("operation_index", -1))]}
            for item in list(review_selection.get("operations") or [])
            if int(item.get("operation_index", -1)) in permitted_indexes
        ]
        result["review_selection"] = review_selection
    result["applied_operations"] = [
        operation
        for operation in list(proposal.get("applied_operations") or [])
        if _paper_wiki_operation_matches_scope(store, operation, scope)
    ]
    # Free-form validation messages may mention unshared page IDs.
    result["validation"] = {
        "valid": bool((proposal.get("validation") or {}).get("valid", False)),
        "errors": [],
        "warnings": [],
    }
    result["read_only"] = True
    result["access"] = scope.as_dict()
    return _paper_wiki_strip_storage_details(result)


def _paper_wiki_source_payload(
    scope: WikiShareScope,
    labels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    owner = labels.get(scope.owner_user_id, {})
    return {
        **scope.as_dict(),
        "owner": {
            "user_id": scope.owner_user_id,
            "name": str(owner.get("name") or ""),
            "email": str(owner.get("email") or ""),
        },
        "grant_count": len(scope.share_ids),
    }


def _paper_wiki_accessible_sources(actor: CurrentUserContext) -> list[dict[str, Any]]:
    scopes = WikiShareStore(ServerState.get_workdir()).list_accessible_sources(
        tenant_id=actor.tenant_id,
        grantee_user_id=actor.user_id,
    )
    labels = tenant_user_summaries(
        tenant_id=actor.tenant_id,
        user_ids=[scope.owner_user_id for scope in scopes],
    )
    return [_paper_wiki_source_payload(scope, labels) for scope in scopes]


async def _paper_wiki_search_accessible_for_actor(
    actor: CurrentUserContext,
    *,
    workspace_id: str,
    query: str,
    limit: int,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Bounded fan-out search over the actor's own and explicitly shared Wikis."""

    safe_limit = max(1, min(50, int(limit or 8)))
    max_sources = max(
        1,
        min(64, int(os.environ.get("TCODE_WIKI_SEARCH_MAX_SOURCES", "24") or 24)),
    )
    per_source_limit = max(2, min(10, safe_limit))
    per_source_timeout = max(
        0.2,
        min(5.0, float(os.environ.get("TCODE_WIKI_SEARCH_SOURCE_TIMEOUT_SECONDS", "1.5") or 1.5)),
    )
    total_timeout = max(
        0.5,
        min(10.0, float(os.environ.get("TCODE_WIKI_SEARCH_TOTAL_TIMEOUT_SECONDS", "3") or 3)),
    )
    concurrency = max(
        1,
        min(12, int(os.environ.get("TCODE_WIKI_SEARCH_CONCURRENCY", "4") or 4)),
    )
    semaphore = asyncio.Semaphore(concurrency)
    all_hits: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    timed_out_sources: list[str] = []

    try:
        own = _paper_wiki_access_for_actor(actor, workspace_id)
        own_result = await asyncio.wait_for(
            asyncio.to_thread(
                own.store.search,
                query,
                limit=per_source_limit,
                include_archived=include_archived,
            ),
            timeout=per_source_timeout,
        )
        for hit in list(own_result.get("hits") or []):
            hit = _paper_wiki_strip_storage_details(dict(hit))
            hit.update(
                {
                    "source_id": "",
                    "source_access": "owner",
                    "source_owner_user_id": actor.user_id,
                    "source_workspace_id": own.owner_context.workspace_id,
                }
            )
            hit["accessible_score"] = float(hit.get("score") or 0.0) + 0.03
            all_hits.append(hit)
        source_results.append(
            {
                "source_id": "",
                "access": "owner",
                "workspace_id": own.owner_context.workspace_id,
                "hit_count": len(own_result.get("hits") or []),
                "status": "ok",
            }
        )
    except asyncio.TimeoutError:
        timed_out_sources.append("own")
    except Exception as exc:
        source_results.append(
            {"source_id": "", "access": "owner", "status": "skipped", "reason": type(exc).__name__}
        )

    scopes = WikiShareStore(ServerState.get_workdir()).list_accessible_sources(
        tenant_id=actor.tenant_id,
        grantee_user_id=actor.user_id,
    )[:max_sources]

    async def search_shared(scope: WikiShareScope) -> tuple[WikiShareScope, dict[str, Any]]:
        async with semaphore:
            access = _paper_wiki_access_for_actor(
                actor,
                source_id=scope.source_id,
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    access.store.search,
                    query,
                    limit=per_source_limit,
                    include_archived=include_archived,
                    **_paper_wiki_scope_kwargs(scope),
                ),
                timeout=per_source_timeout,
            )
            return scope, result

    tasks = [asyncio.create_task(search_shared(scope)) for scope in scopes]
    task_scopes = {task: scope for task, scope in zip(tasks, scopes)}
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=total_timeout)
        for task in pending:
            task.cancel()
        for task in pending:
            scope = task_scopes[task]
            timed_out_sources.append(scope.source_id)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                scope, result = task.result()
            except asyncio.TimeoutError:
                timed_out_sources.append(task_scopes[task].source_id)
                continue
            except Exception as exc:
                failed_scope = task_scopes[task]
                source_results.append(
                    {
                        "source_id": failed_scope.source_id,
                        "access": "shared",
                        "status": "skipped",
                        "reason": type(exc).__name__,
                    }
                )
                continue
            hits = list(result.get("hits") or [])
            for hit in hits:
                hit = _paper_wiki_strip_storage_details(dict(hit))
                hit.update(
                    {
                        "source_id": scope.source_id,
                        "source_access": "shared_read_only",
                        "source_owner_user_id": scope.owner_user_id,
                        "source_workspace_id": scope.workspace_id,
                        "read_only": True,
                    }
                )
                hit["accessible_score"] = float(hit.get("score") or 0.0)
                all_hits.append(hit)
            source_results.append(
                {
                    "source_id": scope.source_id,
                    "access": "shared_read_only",
                    "workspace_id": scope.workspace_id,
                    "owner_user_id": scope.owner_user_id,
                    "hit_count": len(hits),
                    "status": "ok",
                }
            )
    all_hits.sort(
        key=lambda item: (
            float(item.get("accessible_score") or 0.0),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return {
        "query": str(query or ""),
        "hits": all_hits[:safe_limit],
        "total": min(len(all_hits), safe_limit),
        "candidate_count": len(all_hits),
        "source_count": 1 + len(scopes),
        "searched_sources": source_results,
        "timed_out_sources": timed_out_sources,
        "partial": bool(timed_out_sources),
        "access_model": "own_plus_explicit_same_tenant_shares",
    }

