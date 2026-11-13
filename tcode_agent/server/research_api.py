"""Owner-scoped read and maintenance API for the Research Journal."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..research import (
    AttributionCorrectionInput,
    ResearchActivityBuilder,
    ResearchActivityStore,
    ResearchAttributionEngine,
    ResearchDerivationEngine,
    ResearchDerivationStore,
    ResearchDeadLetterReplayer,
    ResearchEventStore,
    ResearchExperienceStore,
    ResearchExperimentStore,
    ResearchContinuationStore,
    ResearchExperienceProjector,
    ResearchJournalReconciler,
    ResearchKnowledgeManager,
    ResearchKnowledgeStore,
    ResearchPackV2Builder,
    ResearchPackV2Scope,
    ResearchContinuationContextBuilder,
    ResearchContextAuditStore,
    ResearchPackImporter,
    ResearchPackImportStore,
    ResearchPackAssetStore,
    ResearchPackRecoveryManager,
    ResearchPackIndexScheduler,
    ResearchProjectDeletionManager,
    ResearchPackStore,
    ResearchReadService,
    ResearchRoutingStore,
    ResearchTopicEngine,
    ResearchTopicManager,
    ResearchTopicStore,
    ResearchEvolutionManager,
    load_research_settings,
    research_candidate_digest,
)
from ..research.activities import ACTIVITY_SCHEMA_VERSION
from ..research.topics import is_topic_artifact_event, is_topic_tool_event
from ..session.manager import SessionManager
from ..workflow.manager import WorkflowRunManager
from ..workflow.registry import WorkflowTemplateRegistry
from . import workdirs as workdir_utils
from .auth import current_user_from_request
from .state import ServerState


router = APIRouter(prefix="/api/research", tags=["research"])

class ResearchSnapshotRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    request_id: str = Field(min_length=1, max_length=180)

def _snapshot_store(request: Request):
    from ..research.snapshots import ResearchSnapshotStore
    return ResearchSnapshotStore(**_experiment_owner(_workspace_context(request)))

async def _snapshot_call(call, *args, **kwargs):
    try: return await asyncio.to_thread(call, *args, **kwargs)
    except LookupError as exc: raise HTTPException(404, str(exc)) from exc
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc

@router.get("/projects/{research_id}/snapshots")
async def list_research_snapshots(research_id: str, request: Request, offset: int = Query(0, ge=0), hidden: bool = False):
    return await _snapshot_call(_snapshot_store(request).list, research_id, offset=offset, hidden=hidden)

@router.post("/projects/{research_id}/snapshots")
async def capture_research_snapshot(research_id: str, payload: ResearchSnapshotRequest, request: Request):
    return await _snapshot_call(_snapshot_store(request).capture, research_id, payload.title, payload.request_id)

@router.get("/snapshots/{snapshot_id}")
async def read_research_snapshot(snapshot_id: str, request: Request):
    return await _snapshot_call(_snapshot_store(request).read, snapshot_id)

@router.get("/snapshots/{base_id}/compare/{head_id}")
async def compare_research_snapshots(base_id: str, head_id: str, request: Request, offset: int = Query(0, ge=0)):
    return await _snapshot_call(_snapshot_store(request).compare, base_id, head_id, offset=offset)

@router.delete("/snapshots/{snapshot_id}")
async def hide_research_snapshot(snapshot_id: str, request: Request):
    return await _snapshot_call(_snapshot_store(request).hide, snapshot_id, True)

@router.post("/snapshots/{snapshot_id}/restore")
async def restore_research_snapshot(snapshot_id: str, request: Request):
    return await _snapshot_call(_snapshot_store(request).hide, snapshot_id, False)


class AttributionRunRequest(BaseModel):
    event_ids: list[str] = Field(default_factory=list, max_length=1000)
    force: bool = False
    limit: int = Field(default=100, ge=1, le=5000)


class AttributionCorrectionRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=40)
    candidate_id: str = Field(default="", max_length=160)
    research_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(default="", max_length=180)


class DeadLetterReplayRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=5000)


class TopicRunRequest(BaseModel):
    limit: int = Field(default=1000, ge=1, le=5000)


class CandidateRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(default="", max_length=1000)


class CandidateConfirmRequest(BaseModel):
    title: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=1000)
    expected_candidate_digest: str = Field(default="", max_length=160)


class CandidateDismissRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class CandidateMergeRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=2, max_length=50)
    title: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=1000)


class CandidateSplitGroup(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    event_ids: list[str] = Field(min_length=1, max_length=5000)


class CandidateSplitRequest(BaseModel):
    groups: list[CandidateSplitGroup] = Field(min_length=2, max_length=50)
    reason: str = Field(default="", max_length=1000)


class ResearchEventActionRequest(BaseModel):
    candidate_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=1000)


class ResearchProjectDeleteRequest(BaseModel):
    confirmation: str = Field(min_length=2, max_length=3)
    preview_token: str = Field(default="", max_length=64)


class EpisodeRefreshRequest(BaseModel):
    force_open: bool = True


class HypothesisWriteRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(default="", max_length=4000)
    test_plan: str = Field(default="", max_length=4000)
    falsification_condition: str = Field(default="", max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=1000)
    counter_evidence_refs: list[str] = Field(default_factory=list, max_length=1000)


class HypothesisStatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=40)


class HypothesisGenerateRequest(BaseModel):
    limit: int = Field(default=3, ge=1, le=8)


class ExperimentCreateRequest(BaseModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    reason: str = Field(default="", max_length=4000)
    workflow_run_id: str = Field(default="", max_length=240)
    parent_experiment_id: str | None = Field(default=None, max_length=160)
    execution_mode: str = Field(default="shared_workspace", max_length=40)
    input_artifacts: list[dict[str, object]] = Field(default_factory=list, max_length=1000)


class ExperimentUpdateRequest(BaseModel):
    status: str = Field(min_length=1, max_length=40)
    decision: str = Field(default="", max_length=40)
    result_summary: str = Field(default="", max_length=8000)
    workflow_run_id: str = Field(default="", max_length=240)


class ContinuationStartRequest(BaseModel):
    max_runs: int = Field(ge=1, le=1000)
    max_minutes: int = Field(ge=1, le=525600)
    checkpoint_every: int = Field(default=1, ge=1, le=100)
    max_tokens: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=0, ge=0)
    max_failures: int = Field(default=0, ge=0)
    max_cost_micros: int = Field(default=0, ge=0)


class ReproductionCreateRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=180)
    source_experiment_id: str = Field(min_length=1, max_length=180)
    session_id: str = Field(default="", max_length=180)


class FindingCreateRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=4000)
    provenance: str = Field(default="user_stated", max_length=40)
    source_event_ids: list[str] = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=1000)
    episode_id: str = Field(default="", max_length=160)
    scope: dict[str, object] = Field(default_factory=dict)


class FindingChallengeRequest(FindingCreateRequest):
    conflict_type: str = Field(default="direct_contradiction", max_length=80)
    summary: str = Field(default="", max_length=4000)


class ConflictCreateRequest(BaseModel):
    conflict_type: str = Field(default="interpretation_dispute", max_length=80)
    original_refs: list[str] = Field(min_length=1, max_length=100)
    challenging_refs: list[str] = Field(min_length=1, max_length=100)
    original_evidence_ids: list[str] = Field(default_factory=list, max_length=1000)
    challenging_evidence_ids: list[str] = Field(default_factory=list, max_length=1000)
    summary: str = Field(min_length=1, max_length=4000)


class ConflictReviewRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


class ConflictResolveRequest(BaseModel):
    result: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=1000)
    scope: dict[str, object] = Field(default_factory=dict)


class ConflictReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=1000)


class KnowledgeLinkRequest(BaseModel):
    subject_type: str = Field(min_length=1, max_length=40)
    subject_id: str = Field(min_length=1, max_length=240)
    object_type: str = Field(min_length=1, max_length=40)
    object_id: str = Field(min_length=1, max_length=500)
    relation: str = Field(min_length=1, max_length=40)
    side: str = Field(default="neutral", max_length=40)
    metadata: dict[str, object] = Field(default_factory=dict)


class ResearchPackPreviewRequest(BaseModel):
    mode: str = Field(default="project", max_length=40)
    research_ids: list[str] = Field(default_factory=list, max_length=3)
    started_at: str = Field(default="", max_length=64)
    ended_at: str = Field(default="", max_length=64)
    privacy_profile: str = Field(default="strict", max_length=40)
    attribution_mode: str = Field(default="anonymous", max_length=40)


class ResearchPackBuildRequest(ResearchPackPreviewRequest):
    expected_source_digest: str = Field(default="", max_length=160)
    acknowledge_privacy: bool = False
    display_name: str = Field(default="", max_length=160)


class ResearchPackImportRequest(BaseModel):
    inspection_id: str = Field(min_length=1, max_length=180)
    duplicate_action: str = Field(default="open_existing", max_length=40)
    title: str = Field(default="", max_length=160)


class ResearchContextPreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)


class ResearchReadHandoffRequest(BaseModel):
    question: str = Field(default="", max_length=20000)


class ResearchReadSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=50)


class ResearchPackReadSearchRequest(ResearchReadSearchRequest):
    source_research_id: str = Field(default="", max_length=160)


class ResearchPackIndexRebuildRequest(BaseModel):
    force_embeddings: bool = False


def _workspace_context(request: Request) -> workdir_utils.WorkspaceContext:
    user = current_user_from_request(request)
    context = workdir_utils.resolve_workspace_context(
        headers=None if user is not None else request.headers,
        query_params=request.query_params,
        tenant_id=user.tenant_id if user is not None else "",
        user_id=user.user_id if user is not None else "",
    )
    try:
        workdir_utils.assert_workspace_active(ServerState.get_workdir(), context)
    except HTTPException as exc:
        if exc.status_code == 410:
            raise HTTPException(
                410,
                "当前 Workspace 已删除，请切换到仍存在的 Workspace 或新建会话。",
            ) from exc
        raise
    return context


def _workspace_path(context: workdir_utils.WorkspaceContext) -> str:
    return workdir_utils.resolve_user_workdir_path(
        ServerState.get_workdir(),
        context.workspace_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    ).as_posix()


def _read_service(context: workdir_utils.WorkspaceContext) -> ResearchReadService:
    return ResearchReadService(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=load_research_settings(),
    )


def _schedule_pack_index(
    context: workdir_utils.WorkspaceContext,
    asset_id: str,
) -> None:
    normalized = str(asset_id or "").strip()
    if not normalized:
        return

    ResearchPackIndexScheduler.schedule(
        asset_id=normalized,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=load_research_settings(),
    )


def _settings_summary() -> dict[str, object]:
    settings = load_research_settings()
    return {
        "enabled": settings.enabled,
        "journal_enabled": settings.journal_enabled,
        "journal_active": settings.journal_active,
        "attribution_enabled": settings.attribution_enabled,
        "attribution_shadow_mode": settings.attribution_shadow_mode,
        "attribution_shadow_active": settings.attribution_shadow_active,
        "attribution_auto_dispatch": settings.attribution_auto_dispatch,
        "attribution_model_enabled": settings.attribution_model_enabled,
        "attribution_batch_size": settings.attribution_batch_size,
        "attribution_debounce_ms": settings.attribution_debounce_ms,
        "attribution_session_context_minutes": (
            settings.attribution_session_context_minutes
        ),
        "topics_active": settings.topics_active,
        "activities_enabled": settings.activities_enabled,
        "activities_active": settings.activities_active,
        "activity_incomplete_minutes": settings.activity_incomplete_minutes,
        "activity_incremental_max_events": settings.activity_incremental_max_events,
        "experience_projection_enabled": settings.experience_projection_enabled,
        "experience_projection_active": settings.experience_projection_active,
        "topic_auto_discovery": settings.topic_auto_discovery,
        "topic_minimum_meaningful_events": (
            settings.topic_minimum_meaningful_events
        ),
        "topic_cross_session_signal": settings.topic_cross_session_signal,
        "topic_keyword_overlap_threshold": (
            settings.topic_keyword_overlap_threshold
        ),
        "topic_stable_score_threshold": settings.topic_stable_score_threshold,
        "topic_max_candidates": settings.topic_max_candidates,
        "topic_max_projects_per_workspace": (
            settings.topic_max_projects_per_workspace
        ),
        "routing_enabled": settings.routing_enabled,
        "routing_active": settings.routing_active,
        "routing_model_fallback_enabled": settings.routing_model_fallback_enabled,
        "routing_confidence_threshold": settings.routing_confidence_threshold,
        "routing_context_token_budget": settings.routing_context_token_budget,
        "routing_imported_context_token_budget": (
            settings.routing_imported_context_token_budget
        ),
        "routing_imported_max_assets": settings.routing_imported_max_assets,
        "routing_fallback_timeout_seconds": settings.routing_fallback_timeout_seconds,
        "episodes_active": settings.episodes_active,
        "checkpoints_active": settings.checkpoints_active,
        "conflicts_active": settings.conflicts_active,
        "knowledge_links_enabled": settings.knowledge_links_enabled,
        "knowledge_links_active": settings.knowledge_links_active,
        "knowledge_max_wiki_pages": settings.knowledge_max_wiki_pages,
        "resume_context_active": settings.resume_context_active,
        "episode_auto_build": settings.episode_auto_build,
        "episode_max_events": settings.episode_max_events,
        "auto_reconcile_on_startup": settings.auto_reconcile_on_startup,
        "reconcile_recent_days": settings.reconcile_recent_days,
        "topics_enabled": settings.topics_enabled,
        "episodes_enabled": settings.episodes_enabled,
        "checkpoints_enabled": settings.checkpoints_enabled,
        "conflicts_enabled": settings.conflicts_enabled,
        "resume_context_enabled": settings.resume_context_enabled,
        "resume_context_target_tokens": settings.resume_context_target_tokens,
        "resume_context_min_relevance": settings.resume_context_min_relevance,
        "resume_context_max_findings": settings.resume_context_max_findings,
        "resume_context_max_episodes": settings.resume_context_max_episodes,
        "resume_context_audit_enabled": settings.resume_context_audit_enabled,
        "research_pack_enabled": settings.research_pack_enabled,
        "research_pack_active": settings.research_pack_active,
        "research_pack_max_events": settings.research_pack_max_events,
        "research_pack_max_bytes": settings.research_pack_max_bytes,
        "research_pack_import_enabled": settings.research_pack_import_enabled,
        "research_pack_import_recovery_enabled": (
            settings.research_pack_import_recovery_enabled
        ),
        "research_pack_import_active": settings.research_pack_import_active,
        "research_pack_import_max_archive_bytes": (
            settings.research_pack_import_max_archive_bytes
        ),
        "research_pack_import_max_uncompressed_bytes": (
            settings.research_pack_import_max_uncompressed_bytes
        ),
        "research_pack_import_max_files": settings.research_pack_import_max_files,
        "content_preview_chars": settings.content_preview_chars,
        "pending_ttl_days": settings.pending_ttl_days,
        "episode_idle_candidate_minutes": settings.episode_idle_candidate_minutes,
        "episode_idle_force_partial_minutes": (
            settings.episode_idle_force_partial_minutes
        ),
        "resume_token_budget": settings.resume_token_budget,
        "background_token_ratio": settings.background_token_ratio,
        "write_fail_open": settings.write_fail_open,
    }


@router.get("/events")
async def list_research_events(
    request: Request,
    session_id: str = Query(""),
    turn_id: str = Query(""),
    trace_id: str = Query(""),
    event_type: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.journal_active:
        return {"events": [], "settings": _settings_summary()}
    events = await asyncio.to_thread(
        ResearchEventStore.list_events,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        session_id=session_id,
        turn_id=turn_id,
        trace_id=trace_id,
        event_type=event_type,
        limit=limit,
        settings=settings,
    )
    return {
        "events": [event.to_dict() for event in events],
        "count": len(events),
        "settings": _settings_summary(),
    }


@router.get("/health")
async def get_research_health(request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.journal_active:
        return {
            "status": "disabled",
            "event_count": 0,
            "session_count": 0,
            "trace_count": 0,
            "settings": _settings_summary(),
        }
    path = _workspace_path(context)

    def load_health() -> dict[str, object]:
        owner = {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "workspace_id": context.workspace_id,
            "workdir": path,
            "settings": settings,
        }
        health = ResearchEventStore.health(**owner)
        health.pop("database_path", None)
        return {
            "status": "ok",
            **health,
            "attribution": ResearchEventStore.attribution_health(**owner),
            "topics": ResearchTopicStore.health(**owner),
            "activities": ResearchActivityStore.health(**owner),
            "experiences": ResearchExperienceStore.health(**owner),
            "routing": ResearchRoutingStore.health(**owner),
            "derivations": ResearchDerivationStore.health(**owner),
            "knowledge": ResearchKnowledgeStore.health(**owner),
            "dead_letter": ResearchDeadLetterReplayer.health(
                workdir=path,
                settings=settings,
            ),
            "settings": _settings_summary(),
        }

    return await asyncio.to_thread(load_health)


@router.get("/routes")
async def list_research_turn_routes(
    request: Request,
    turn_id: str = Query("", max_length=240),
    include_history: bool = Query(False),
    limit: int = Query(200, ge=1, le=5000),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
        "workdir": _workspace_path(context),
        "settings": settings,
    }
    if turn_id:
        route = await asyncio.to_thread(
            ResearchRoutingStore.get_active_route,
            turn_id,
            **owner,
        )
        routes = [route] if route is not None else []
    else:
        routes = await asyncio.to_thread(
            ResearchRoutingStore.list_routes,
            **owner,
            include_history=include_history,
            limit=limit,
        )
    return {
        "routes": [route.to_dict() for route in routes],
        "count": len(routes),
        "settings": _settings_summary(),
    }


@router.get("/experiences")
async def list_research_experiences(
    request: Request,
    research_id: str = Query("", max_length=160),
    unassigned_only: bool = Query(False),
    durability: str = Query("", max_length=40),
    started_at: str = Query("", max_length=48),
    ended_at: str = Query("", max_length=48),
    include_history: bool = Query(False),
    limit: int = Query(500, ge=1, le=5000),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        items = await asyncio.to_thread(
            ResearchExperienceStore.list_items,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            research_id=research_id,
            unassigned_only=unassigned_only,
            durability=durability,
            started_at=started_at,
            ended_at=ended_at,
            include_history=include_history,
            limit=limit,
            workdir=_workspace_path(context),
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "experiences": [item.to_dict() for item in items],
        "count": len(items),
        "settings": _settings_summary(),
    }


@router.post("/reconcile")
async def reconcile_research_journal(request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.journal_active:
        return {"status": "disabled", "report": {}}
    sessions = await SessionManager.list(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
    )
    report = ResearchJournalReconciler(
        workdir=_workspace_path(context)
    ).reconcile(sessions)
    return {"status": "completed", "report": report.to_dict()}


@router.post("/attribution/run")
async def run_research_attribution(
    payload: AttributionRunRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.attribution_shadow_active:
        raise HTTPException(409, "Attribution Shadow is not active")
    try:
        engine = ResearchAttributionEngine(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
        report = await asyncio.to_thread(
            engine.run,
            trigger_kind="manual_api",
            event_ids=payload.event_ids,
            force=payload.force,
            limit=payload.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    activity_count = 0
    if report.status != "failed" and settings.activities_active:
        builder = _activity_builder(context, settings)
        if payload.event_ids:
            activity_report = await asyncio.to_thread(
                builder.apply_events,
                payload.event_ids,
            )
            activity_count = activity_report.activity_count
            if settings.experience_projection_active:
                projector = ResearchExperienceProjector(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    workdir=_workspace_path(context),
                    settings=settings,
                )
                await asyncio.to_thread(
                    projector.apply_activities,
                    activity_report.affected_activity_ids,
                )
                await asyncio.to_thread(
                    projector.remove_activity_keys,
                    activity_report.removed_activity_keys,
                )
        else:
            activities = await asyncio.to_thread(
                builder.rebuild_owner,
                limit=5000,
            )
            activity_count = len(activities)
            if settings.experience_projection_active:
                await asyncio.to_thread(
                    ResearchExperienceProjector(
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        workspace_id=context.workspace_id,
                        workdir=_workspace_path(context),
                        settings=settings,
                    ).apply_activities,
                    [item.activity_id for item in activities],
                )
    return {
        "status": report.status,
        "report": report.to_dict(),
        "activity_count": activity_count,
    }


@router.get("/attribution/links")
async def list_research_attribution_links(
    request: Request,
    event_id: str = Query(""),
    state: str = Query(""),
    relevance: str = Query(""),
    active_only: bool = Query(True),
    limit: int = Query(200, ge=1, le=5000),
):
    context = _workspace_context(request)
    links = ResearchEventStore.list_attribution_links(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        event_id=event_id,
        state=state,
        relevance=relevance,
        active_only=active_only,
        limit=limit,
    )
    return {"count": len(links), "links": [item.to_dict() for item in links]}


@router.get("/attribution/runs")
async def list_research_attribution_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
):
    context = _workspace_context(request)
    runs = ResearchEventStore.list_attribution_runs(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        limit=limit,
    )
    return {"count": len(runs), "runs": runs}


@router.post("/attribution/corrections")
async def correct_research_attribution(
    payload: AttributionCorrectionRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.attribution_shadow_active:
        raise HTTPException(409, "Attribution Shadow is not active")
    correction = AttributionCorrectionInput(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        event_id=payload.event_id,
        action=payload.action,
        actor_id=context.user_id,
        candidate_id=payload.candidate_id,
        research_id=payload.research_id,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
    )
    if payload.candidate_id and settings.topics_active:
        candidate = ResearchTopicStore.get_candidate(
            payload.candidate_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
        if candidate is None:
            raise HTTPException(400, "candidate does not belong to owner")
    try:
        stored, report, link = ResearchAttributionEngine(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        ).correct(correction)
        if settings.topics_active:
            ResearchTopicManager(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                workdir=_workspace_path(context),
                settings=settings,
            ).sync_attribution_correction(
                payload.event_id,
                action=payload.action,
                actor_id=context.user_id,
                candidate_id=payload.candidate_id,
                reason=payload.reason,
                correction_id=str(stored.get("correction_id") or ""),
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "status": report.status,
        "correction": stored,
        "link": link.to_dict() if link else None,
        "report": report.to_dict(),
    }


@router.get("/attribution/evaluation")
async def get_research_attribution_evaluation(request: Request):
    context = _workspace_context(request)
    report = ResearchEventStore.attribution_evaluation(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    return {"status": "ok", "evaluation": report}


@router.post("/dead-letter/replay")
async def replay_research_dead_letter(
    payload: DeadLetterReplayRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.journal_active:
        raise HTTPException(409, "Research Journal is not active")
    report = ResearchDeadLetterReplayer.replay(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        limit=payload.limit,
        settings=settings,
    )
    return {"status": "completed", "report": report}


def _topic_manager(
    context: workdir_utils.WorkspaceContext,
    settings,
) -> ResearchTopicManager:
    if not settings.topics_active:
        raise HTTPException(409, "Topic Candidate Preview is not active")
    return ResearchTopicManager(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


@router.post("/topics/run")
async def run_research_topics(payload: TopicRunRequest, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.topics_active:
        raise HTTPException(409, "Topic Candidate Preview is not active")
    engine = ResearchTopicEngine(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    report = await asyncio.to_thread(
        engine.run,
        trigger_kind="manual_api",
        limit=payload.limit,
    )
    activity_count = 0
    if settings.activities_active:
        activities = await asyncio.to_thread(
            _activity_builder(context, settings).rebuild_owner,
            limit=5000,
        )
        activity_count = len(activities)
        if settings.experience_projection_active:
            await asyncio.to_thread(
                ResearchExperienceProjector(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    workdir=_workspace_path(context),
                    settings=settings,
                ).apply_activities,
                [item.activity_id for item in activities],
            )
    return {
        "status": report.status,
        "report": report.to_dict(),
        "activity_count": activity_count,
    }


@router.get("/candidates")
async def list_research_candidates(
    request: Request,
    state: list[str] = Query(default=[]),
    include_inactive: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
):
    context = _workspace_context(request)
    # Once a candidate becomes a formal project it is historical provenance,
    # not an active candidate. Callers can still request it explicitly through
    # include_inactive=true or state=confirmed.
    candidate_states = state or (
        [] if include_inactive else ["emerging", "stable"]
    )
    candidates = await asyncio.to_thread(
        ResearchTopicStore.list_candidates,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        states=candidate_states,
        include_inactive=include_inactive,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {
        "count": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "settings": _settings_summary(),
    }


@router.get("/candidates/{candidate_id}")
async def get_research_candidate(candidate_id: str, request: Request):
    context = _workspace_context(request)
    path = _workspace_path(context)

    def load_detail():
        candidate = ResearchTopicStore.get_candidate(
            candidate_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=path,
        )
        if candidate is None:
            return None, []
        events = ResearchTopicStore.list_candidate_events(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            candidate_id=candidate_id,
            limit=5000,
            workdir=path,
        )
        return candidate, events

    candidate, events = await asyncio.to_thread(load_detail)
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    return {
        "candidate": candidate.to_dict(),
        "candidate_digest": research_candidate_digest(candidate),
        "events": [event.to_dict() for event in events],
        "event_count": len(events),
    }


@router.post("/candidates/{candidate_id}/rename")
async def rename_research_candidate(
    candidate_id: str,
    payload: CandidateRenameRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        candidate, operation = _topic_manager(
            context, load_research_settings()
        ).rename(
            candidate_id,
            title=payload.title,
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"candidate": candidate.to_dict(), "operation": operation}


@router.post("/candidates/{candidate_id}/confirm")
async def confirm_research_candidate(
    candidate_id: str,
    payload: CandidateConfirmRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        candidate, project, operation = _topic_manager(
            context, load_research_settings()
        ).confirm(
            candidate_id,
            actor_id=context.user_id,
            title=payload.title,
            description=payload.description,
            reason=payload.reason,
            expected_candidate_digest=payload.expected_candidate_digest,
        )
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(
            409 if "changed since preview" in message else 400,
            message,
        )
    source_events = await asyncio.to_thread(
        ResearchTopicStore.list_candidate_events,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        candidate_id=candidate_id,
        limit=5000,
        workdir=_workspace_path(context),
    )
    bound_session_ids: list[str] = []
    for session_id in dict.fromkeys(
        str(item.session_id or "").strip() for item in source_events
    ):
        if not session_id:
            continue
        try:
            bound = await SessionManager.bind_research_project(
                session_id,
                research_id=project.research_id,
                mode_source="candidate_confirmed",
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
            )
        except ValueError:
            bound = None
        if bound is not None:
            bound_session_ids.append(session_id)
    await _lock_superseded_research_sessions(context, project.research_id)
    return {
        "candidate": candidate.to_dict(),
        "project": project.to_dict(),
        "operation": operation,
        "bound_session_ids": bound_session_ids,
    }


@router.post("/candidates/{candidate_id}/dismiss")
async def dismiss_research_candidate(
    candidate_id: str,
    payload: CandidateDismissRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        candidate, operation = _topic_manager(
            context, load_research_settings()
        ).dismiss(
            candidate_id,
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"candidate": candidate.to_dict(), "operation": operation}


@router.post("/candidates/merge")
async def merge_research_candidates(
    payload: CandidateMergeRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        candidate, operation = _topic_manager(
            context, load_research_settings()
        ).merge(
            payload.candidate_ids,
            actor_id=context.user_id,
            title=payload.title,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"candidate": candidate.to_dict(), "operation": operation}


@router.post("/candidates/{candidate_id}/split")
async def split_research_candidate(
    candidate_id: str,
    payload: CandidateSplitRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        candidates, operation = _topic_manager(
            context, load_research_settings()
        ).split(
            candidate_id,
            [group.model_dump() for group in payload.groups],
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "candidates": [candidate.to_dict() for candidate in candidates],
        "operation": operation,
    }


@router.get("/organize/events")
async def list_research_organize_events(
    request: Request,
    state: str = Query("pending"),
    relevance: str = Query("research"),
    limit: int = Query(200, ge=1, le=1000),
):
    context = _workspace_context(request)
    path = _workspace_path(context)
    links = ResearchEventStore.list_attribution_links(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=path,
        state=state,
        relevance=relevance,
        active_only=True,
        limit=limit,
    )
    event_ids = [link.event_id for link in links]
    events = ResearchEventStore.list_events_for_attribution(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=path,
        event_ids=event_ids,
        limit=max(1, len(event_ids)),
    ) if event_ids else []
    event_by_id = {event.event_id: event for event in events}
    rows = [
        {"event": event_by_id[link.event_id].to_dict(), "link": link.to_dict()}
        for link in links
        if link.event_id in event_by_id
    ]
    return {"count": len(rows), "items": rows}


def _activity_builder(
    context: workdir_utils.WorkspaceContext,
    settings,
) -> ResearchActivityBuilder:
    if not settings.activities_active:
        raise HTTPException(409, "Research Activity projection is not active")
    return ResearchActivityBuilder(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


@router.get("/organize/activities")
async def list_research_organize_activities(
    request: Request,
    review_state: str = Query("pending"),
    limit: int = Query(500, ge=1, le=2000),
):
    """Return user-readable activity cards while keeping raw Event APIs intact."""

    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.activities_active:
        return {"count": 0, "activities": [], "settings": _settings_summary()}
    activities = await asyncio.to_thread(
        ResearchActivityStore.list,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        review_state=review_state,
        limit=limit,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if any(activity.schema_version < ACTIVITY_SCHEMA_VERSION for activity in activities):
        await asyncio.to_thread(
            _activity_builder(context, settings).rebuild_owner,
            limit=5000,
        )
        activities = await asyncio.to_thread(
            ResearchActivityStore.list,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            review_state=review_state,
            limit=limit,
            workdir=_workspace_path(context),
            settings=settings,
        )
    return {
        "count": len(activities),
        "activities": [activity.to_dict() for activity in activities],
        "settings": _settings_summary(),
    }


def _pending_activity_event_ids(activity, *, context, settings) -> list[str]:
    links = ResearchEventStore.list_attribution_links(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        state="pending",
        relevance="research",
        active_only=True,
        limit=5000,
        settings=settings,
    )
    pending = {link.event_id for link in links}
    return [event_id for event_id in activity.semantic_event_ids if event_id in pending]


@router.post("/activities/{activity_id}/include")
async def include_research_activity(
    activity_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    builder = _activity_builder(context, settings)
    activity = ResearchActivityStore.get(
        activity_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if activity is None:
        await asyncio.to_thread(builder.rebuild_owner, limit=5000)
        activity = ResearchActivityStore.get(
            activity_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
    if activity is None:
        raise HTTPException(404, "activity not found")
    candidate_id = payload.candidate_id or activity.candidate_id
    if not candidate_id:
        raise HTTPException(400, "candidate_id is required")
    event_ids = _pending_activity_event_ids(
        activity, context=context, settings=settings
    )
    manager = _topic_manager(context, settings)
    processed: list[str] = []
    errors: list[dict[str, str]] = []
    reason = payload.reason or f"用户纳入研究活动 {activity.activity_id}"
    for event_id in event_ids:
        try:
            manager.assign_event(
                event_id,
                candidate_id=candidate_id,
                actor_id=context.user_id,
                reason=reason,
                action="include_activity_event",
            )
            processed.append(event_id)
        except ValueError as exc:
            errors.append({"event_id": event_id, "error": str(exc)})
    activity_report = await asyncio.to_thread(builder.apply_events, activity.event_ids)
    if settings.experience_projection_active:
        projector = ResearchExperienceProjector(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
        await asyncio.to_thread(
            projector.apply_activities,
            activity_report.affected_activity_ids,
        )
        await asyncio.to_thread(
            projector.remove_activity_keys,
            activity_report.removed_activity_keys,
        )
    activities = ResearchActivityStore.list(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
        limit=2000,
    )
    return {
        "status": "partial" if errors else "completed",
        "activity_id": activity.activity_id,
        "processed_event_ids": processed,
        "errors": errors,
        "activity_count": len(activities),
    }


@router.post("/activities/{activity_id}/exclude")
async def exclude_research_activity(
    activity_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    builder = _activity_builder(context, settings)
    activity = ResearchActivityStore.get(
        activity_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if activity is None:
        await asyncio.to_thread(builder.rebuild_owner, limit=5000)
        activity = ResearchActivityStore.get(
            activity_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
    if activity is None:
        raise HTTPException(404, "activity not found")
    event_ids = _pending_activity_event_ids(
        activity, context=context, settings=settings
    )
    manager = _topic_manager(context, settings)
    processed: list[str] = []
    errors: list[dict[str, str]] = []
    reason = payload.reason or f"用户排除研究活动 {activity.activity_id}"
    for event_id in event_ids:
        try:
            manager.exclude_event(
                event_id,
                actor_id=context.user_id,
                reason=reason,
            )
            processed.append(event_id)
        except ValueError as exc:
            errors.append({"event_id": event_id, "error": str(exc)})
    activity_report = await asyncio.to_thread(builder.apply_events, activity.event_ids)
    if settings.experience_projection_active:
        projector = ResearchExperienceProjector(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        )
        await asyncio.to_thread(
            projector.apply_activities,
            activity_report.affected_activity_ids,
        )
        await asyncio.to_thread(
            projector.remove_activity_keys,
            activity_report.removed_activity_keys,
        )
    activities = ResearchActivityStore.list(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
        limit=2000,
    )
    return {
        "status": "partial" if errors else "completed",
        "activity_id": activity.activity_id,
        "processed_event_ids": processed,
        "errors": errors,
        "activity_count": len(activities),
    }


@router.post("/events/{event_id}/include")
async def include_research_event(
    event_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    if not payload.candidate_id:
        raise HTTPException(400, "candidate_id is required")
    try:
        membership, link, report, operation = _topic_manager(
            context, load_research_settings()
        ).assign_event(
            event_id,
            candidate_id=payload.candidate_id,
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "membership": membership.to_dict(),
        "link": link.to_dict() if link else None,
        "report": report.to_dict(),
        "operation": operation,
    }


@router.post("/events/{event_id}/move")
async def move_research_event(
    event_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    if not payload.candidate_id:
        raise HTTPException(400, "candidate_id is required")
    try:
        membership, link, report, operation = _topic_manager(
            context, load_research_settings()
        ).assign_event(
            event_id,
            candidate_id=payload.candidate_id,
            actor_id=context.user_id,
            reason=payload.reason,
            action="move_event",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "membership": membership.to_dict(),
        "link": link.to_dict() if link else None,
        "report": report.to_dict(),
        "operation": operation,
    }


@router.post("/events/{event_id}/exclude")
async def exclude_research_event(
    event_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        correction, link, report, operation = _topic_manager(
            context, load_research_settings()
        ).exclude_event(
            event_id,
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "correction": correction,
        "link": link.to_dict() if link else None,
        "report": report.to_dict(),
        "operation": operation,
    }


@router.post("/events/{event_id}/restore")
async def restore_research_event(
    event_id: str,
    payload: ResearchEventActionRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        correction, report, link = ResearchAttributionEngine(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        ).correct(
            AttributionCorrectionInput(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                event_id=event_id,
                action="restore_frozen",
                actor_id=context.user_id,
                reason=payload.reason,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return {
        "correction": correction,
        "link": link.to_dict() if link else None,
        "report": report.to_dict(),
    }


@router.get("/projects")
async def list_research_projects(
    request: Request,
    include_archived: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
):
    context = _workspace_context(request)
    projects = await asyncio.to_thread(
        ResearchTopicStore.list_projects,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        include_archived=include_archived,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(projects), "projects": [item.to_dict() for item in projects]}


@router.get("/center")
async def get_research_center(request: Request):
    """Return the current Workspace research-center projection in one snapshot."""

    context = _workspace_context(request)
    path = _workspace_path(context)
    candidates, projects = await asyncio.gather(
        asyncio.to_thread(
            ResearchTopicStore.list_candidates,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            include_inactive=True,
            limit=1000,
            workdir=path,
        ),
        asyncio.to_thread(
            ResearchTopicStore.list_projects,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            include_archived=True,
            limit=1000,
            workdir=path,
        ),
    )
    open_candidates = [
        item for item in candidates if item.state in {"emerging", "stable"}
    ][:3]
    current = next((item for item in projects if item.status != "archived"), None)
    history = [item for item in projects if item.status == "archived"]
    return {
        "current_project": current.to_dict() if current else None,
        "candidates": [item.to_dict() for item in open_candidates],
        "history": [item.to_dict() for item in history],
        "candidate_limit": 3,
        "project_limit": 1,
    }


@router.get("/projects/{research_id}")
async def get_research_project(research_id: str, request: Request):
    context = _workspace_context(request)
    project = ResearchTopicStore.get_project(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    if project is None:
        raise HTTPException(404, "project not found")
    return {"project": project.to_dict()}


_PROVENANCE_TECH_REF_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:session|trace|event|turn|call|episode|checkpoint|run)"
    r"\s*[:#-]\s*[a-z0-9._:-]+"
)


def _provenance_text(value: object, limit: int = 360) -> str:
    lines = [
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip() and not line.lstrip().startswith("# Task Contract")
    ]
    text = _PROVENANCE_TECH_REF_PATTERN.sub(" ", " ".join(lines))
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-—:：")
    return text[:limit]


def _provenance_event_category(event) -> str:
    event_type = str(event.event_type or "").strip().lower()
    source_kind = str(event.source_kind or "").strip().lower()
    if event_type.startswith("message.") or source_kind == "session_message":
        return "message"
    if event_type.startswith("mcp.") or source_kind == "mcp_tool":
        return "mcp"
    if event_type.startswith("workflow.") or source_kind == "workflow":
        return "workflow"
    if event_type.startswith("agent.") or source_kind == "subagent":
        return "agent"
    if event_type.startswith(("artifact.", "document.")) or source_kind in {
        "artifact",
        "tool_artifact",
        "document",
    }:
        return "artifact"
    if event_type.startswith("file.") or source_kind in {"file_upload", "file_change"}:
        return "file"
    if event_type.startswith("tool.") or source_kind == "tool_call":
        return "tool"
    return "other"


def _provenance_event_readable_value(event) -> object:
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in (
        "content_preview",
        "output_preview",
        "error_preview",
        "paper_title",
        "document_title",
        "artifact_title",
        "display_name",
        "summary_text",
        "summary",
        "statement",
        "description",
        "task",
        "filename",
    ):
        value = payload.get(key)
        if str(value or "").strip():
            return value
    return ""


def _provenance_event_display_value(event) -> object:
    readable = _provenance_event_readable_value(event)
    if str(readable or "").strip():
        return readable
    payload = event.payload if isinstance(event.payload, dict) else {}
    return payload.get("title") or event.source_ref or event.event_type


def _is_provenance_technical_event(event) -> bool:
    event_type = str(event.event_type or "").strip().lower()
    category = _provenance_event_category(event)
    readable = str(_provenance_event_readable_value(event) or "").strip()
    if (
        event_type
        in {
            "session.created",
            "session.archived",
            "session.resumed",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "turn.cancelled",
            "message.compacted",
        }
        or event_type.endswith(".started")
        or event_type == "agent.spawned"
    ):
        return True
    if event_type == "message.created" and readable.startswith("# Task Contract"):
        return True
    if event_type == "message.created" and not readable:
        return True
    if category == "artifact":
        title = str((event.payload or {}).get("title") or "").strip().lower()
        has_readable_title = bool(
            title
            and title not in {"artifact", "file", "output", "result", "task", "tool artifact"}
        )
        return not readable and not has_readable_title
    if category in {"tool", "mcp", "workflow", "agent"}:
        return not readable
    return False


def _provenance_source_title(event, category: str) -> str:
    if category == "message":
        return "用户研究请求" if event.actor_type == "user" else "Agent 研究记录"
    if category == "file":
        return "上传研究文件" if event.event_type == "file.uploaded" else "更新研究文件"
    if category == "artifact":
        return "生成研究产物"
    if category == "agent":
        return "子 Agent 研究过程"
    if category == "workflow":
        return "研究工作流结果"
    if category == "mcp":
        return "MCP 执行失败" if event.event_type.endswith("failed") else "MCP 执行结果"
    if category == "tool":
        return "工具执行失败" if event.event_type.endswith("failed") else "工具执行结果"
    return "研究记录"


def _project_provenance_payload(*, context, research_id: str) -> dict[str, object] | None:
    path = _workspace_path(context)
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
    }
    project = ResearchTopicStore.get_project(
        research_id,
        **owner,
        workdir=path,
    )
    if project is None:
        return None

    candidates = []
    event_by_id = {}
    for candidate_id in project.origin_candidate_ids:
        candidate = ResearchTopicStore.get_candidate(
            candidate_id,
            **owner,
            workdir=path,
        )
        if candidate is None:
            continue
        candidates.append(candidate)
        for event in ResearchTopicStore.list_candidate_events(
            candidate_id=candidate_id,
            **owner,
            workdir=path,
            limit=5000,
        ):
            event_by_id[event.event_id] = event

    events = sorted(
        event_by_id.values(),
        key=lambda item: (item.occurred_at, item.ingested_at, item.event_id),
    )
    origin_ids = {candidate.candidate_id for candidate in candidates}
    operations = [
        operation
        for operation in ResearchTopicStore.list_operations(
            **owner,
            limit=1000,
            workdir=path,
        )
        if (
            origin_ids.intersection(operation.get("source_candidate_ids") or [])
            or origin_ids.intersection(operation.get("target_candidate_ids") or [])
            or str((operation.get("after") or {}).get("research_id") or "") == research_id
        )
    ]
    confirmation = next(
        (item for item in reversed(operations) if item.get("action") == "confirm_project"),
        None,
    )

    event_ids = set(event_by_id)
    activities = []
    artifact_group_ids: set[str] = set()
    stored_activities = ResearchActivityStore.list(
        **owner,
        review_state="",
        limit=2000,
        workdir=path,
    )
    if any(
        activity.schema_version < ACTIVITY_SCHEMA_VERSION
        for activity in stored_activities
    ):
        settings = load_research_settings()
        if settings.activities_active:
            ResearchActivityBuilder(
                **owner,
                workdir=path,
                settings=settings,
            ).rebuild_owner(limit=5000)
            stored_activities = ResearchActivityStore.list(
                **owner,
                review_state="",
                limit=2000,
                workdir=path,
                settings=settings,
            )
    for activity in stored_activities:
        if not (
            activity.research_id == research_id
            or activity.candidate_id in origin_ids
            or event_ids.intersection(activity.event_ids)
        ):
            continue
        groups = list(getattr(activity, "artifact_groups", []) or [])
        artifact_group_ids.update(
            str(group.get("group_id") or "")
            for group in groups
            if str(group.get("group_id") or "")
        )
        activities.append(
            {
                "activity_id": activity.activity_id,
                "activity_type": activity.activity_type,
                "title": activity.title,
                "summary": activity.summary,
                "status": activity.status,
                "started_at": activity.started_at,
                "ended_at": activity.ended_at,
                "artifact_group_count": len(groups),
                "agent_calls": list(getattr(activity, "agent_calls", []) or []),
            }
        )

    sessions = {event.session_id for event in events if event.session_id}
    # Keep these two summary counters identical to Topic Candidate cards.
    tool_event_count = sum(1 for event in events if is_topic_tool_event(event))
    artifact_event_count = sum(
        1 for event in events if is_topic_artifact_event(event)
    )
    files = {
        event.event_id
        for event in events
        if event.source_kind in {"file_upload", "file_change"}
        or event.event_type.startswith("file.")
    }
    child_agents = {
        str(
            event.payload.get("subagent_session_id")
            or event.payload.get("child_session_id")
            or event.source_id
            or event.event_id
        )
        for event in events
        if event.source_kind == "subagent" or event.event_type.startswith("agent.")
    }

    initial_intent = next(
        (
            _provenance_text(event.payload.get("content_preview"), 500)
            for event in events
            if event.actor_type == "user"
            and event.source_kind == "session_message"
            and _provenance_text(event.payload.get("content_preview"), 500)
        ),
        "",
    ) or next(
        (candidate.goal_summary for candidate in candidates if candidate.goal_summary),
        project.description or project.title,
    )

    key_sources: list[dict[str, object]] = []
    source_counts = {
        "message": 0,
        "tool": 0,
        "mcp": 0,
        "artifact": 0,
        "workflow": 0,
        "agent": 0,
        "file": 0,
        "other": 0,
    }
    for event in events:
        if _is_provenance_technical_event(event):
            continue
        category = _provenance_event_category(event)
        title = _provenance_source_title(event, category)
        summary = (
            _provenance_text(_provenance_event_display_value(event), 360)
            or title
        )
        kind = {
            "message": "user_intent" if event.actor_type == "user" else "agent_result",
            "file": "file",
            "tool": "tool_result",
            "mcp": "tool_result",
            "artifact": "artifact",
            "workflow": "workflow_result",
            "agent": "agent_result",
        }.get(category, "other")
        source_counts[category] += 1
        key_sources.append(
            {
                "event_id": event.event_id,
                "kind": kind,
                "category": category,
                "title": title,
                "summary": summary,
                "occurred_at": event.occurred_at,
                "can_exclude": True,
            }
        )
        if len(key_sources) >= 5000:
            break

    return {
        "project": project.to_dict(),
        "origin_candidates": [candidate.to_dict() for candidate in candidates],
        "confirmation": confirmation,
        "initial_intent": initial_intent,
        "counts": {
            "events": len(events),
            "sessions": len(sessions),
            "tools": tool_event_count,
            "files": len(files),
            "artifacts": artifact_event_count,
            "artifact_groups": len(artifact_group_ids),
            "agents": len(child_agents),
            "activities": len(activities),
        },
        "source_counts": source_counts,
        "key_sources": key_sources,
        "activities": activities[:12],
        "history": operations[:20],
    }


@router.get("/projects/{research_id}/provenance")
async def get_research_project_provenance(research_id: str, request: Request):
    """Explain how a formal project was formed without exposing raw audit noise."""

    context = _workspace_context(request)
    payload = await asyncio.to_thread(
        _project_provenance_payload,
        context=context,
        research_id=research_id,
    )
    if payload is None:
        raise HTTPException(404, "project not found")
    return payload


async def _research_sessions(context, research_id: str = ""):
    sessions = await SessionManager.list(
        include_archived=None,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
    )
    return [
        session for session in sessions
        if str((session.metadata or {}).get("mode") or "") == "research"
        and (not research_id or str((session.metadata or {}).get("research_id") or "") == research_id)
    ]


async def _lock_superseded_research_sessions(context, current_research_id: str) -> None:
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
    }
    for session in await _research_sessions(context):
        metadata = dict(session.metadata or {})
        if (
            SessionManager.is_archived_session(session)
            and str(metadata.get("research_id") or "") != current_research_id
        ):
            await SessionManager.set_archive_locked(session.id, True, **owner)


async def _archive_research_sessions(context, research_id: str) -> list[str]:
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
    }
    affected: list[str] = []
    for session in await _research_sessions(context, research_id):
        if SessionManager.is_archived_session(session):
            continue
        await SessionManager.set_archived(
            session.id,
            archived=True,
            archived_by_user=False,
            archive_cause="project_cascade",
            archive_locked=False,
            **owner,
        )
        affected.append(session.id)
    return affected


async def _restore_research_sessions(context, research_id: str) -> list[str]:
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
    }
    affected: list[str] = []
    for session in await _research_sessions(context, research_id):
        metadata = dict(session.metadata or {})
        if str(metadata.get("archive_cause") or "") != "project_cascade":
            continue
        await SessionManager.set_archive_locked(session.id, False, **owner)
        await SessionManager.set_archived(
            session.id,
            archived=False,
            archived_by_user=False,
            **owner,
        )
        affected.append(session.id)
    return affected


@router.post("/projects/{research_id}/activate")
async def activate_research_project(research_id: str, request: Request):
    context = _workspace_context(request)
    try:
        project, operation = ResearchTopicManager(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
        ).set_project_status(
            research_id,
            status="active",
            actor_id=context.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    restored_session_ids = await _restore_research_sessions(context, research_id)
    await _lock_superseded_research_sessions(context, research_id)
    return {"project": project.to_dict(), "operation": operation, "restored_session_ids": restored_session_ids}


@router.post("/projects/{research_id}/archive")
async def archive_research_project(research_id: str, request: Request):
    context = _workspace_context(request)
    try:
        project, operation = ResearchTopicManager(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
        ).set_project_status(
            research_id,
            status="archived",
            actor_id=context.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    archived_session_ids = await _archive_research_sessions(context, research_id)
    return {"project": project.to_dict(), "operation": operation, "archived_session_ids": archived_session_ids}


@router.delete("/projects/{research_id}")
async def delete_research_project(
    research_id: str,
    payload: ResearchProjectDeleteRequest,
    request: Request,
):
    """Permanently delete a project, its Research Sessions and managed files."""

    context = _workspace_context(request)
    manager = ResearchProjectDeletionManager(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    if str(payload.confirmation or "").strip().lower() != "yes":
        raise HTTPException(400, "confirmation must be yes")
    project = ResearchTopicStore.get_project(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    if project is None:
        raise HTTPException(404, "project not found")
    research_sessions = await _research_sessions(context, research_id)
    preview_payload = {
        "research_id": project.research_id,
        "updated_at": project.updated_at,
        "session_ids": sorted(session.id for session in research_sessions),
    }
    expected_preview_token = hashlib.sha256(
        json.dumps(preview_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if payload.preview_token != expected_preview_token:
        raise HTTPException(409, "删除影响范围已变化，请重新查看删除预览")
    if project.status != "archived":
        ResearchTopicManager(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
        ).set_project_status(research_id, status="archived", actor_id=context.user_id)
        await _archive_research_sessions(context, research_id)
    try:
        report = await asyncio.to_thread(
            manager.delete,
            research_id,
            actor_id=context.user_id,
            confirmation=payload.confirmation,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        if "must be archived" in str(exc):
            raise HTTPException(409, "请先归档课题，再执行永久删除")
        raise HTTPException(500, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    deleted_session_ids: list[str] = []
    for session in research_sessions:
        await SessionManager.delete(
            session.id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        deleted_session_ids.append(session.id)
    return {
        "status": "deleted",
        "deletion": report.to_dict(public=True),
        "deleted_session_ids": deleted_session_ids,
    }


@router.get("/projects/{research_id}/deletion-preview")
async def get_research_project_deletion_preview(research_id: str, request: Request):
    context = _workspace_context(request)
    project = ResearchTopicStore.get_project(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    if project is None:
        raise HTTPException(404, "project not found")
    sessions = await _research_sessions(context, research_id)
    payload = {
        "research_id": project.research_id,
        "updated_at": project.updated_at,
        "session_ids": sorted(session.id for session in sessions),
    }
    token = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "project": project.to_dict(),
        "research_session_count": len(sessions),
        "research_session_ids": payload["session_ids"],
        "preview_token": token,
        "warning": "永久删除课题会同时删除相关研究会话及其消息，且不可恢复。",
    }


@router.get("/topic-operations")
async def list_research_topic_operations(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
):
    context = _workspace_context(request)
    operations = ResearchTopicStore.list_operations(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(operations), "operations": operations}


def _derivation_engine(
    context: workdir_utils.WorkspaceContext,
    settings,
) -> ResearchDerivationEngine:
    if not settings.episodes_active:
        raise HTTPException(409, "Episode Preview is not active")
    return ResearchDerivationEngine(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


def _evolution_manager(
    context: workdir_utils.WorkspaceContext,
    settings,
) -> ResearchEvolutionManager:
    if not settings.conflicts_active:
        raise HTTPException(409, "Research Conflict Preview is not active")
    return ResearchEvolutionManager(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


def _knowledge_manager(
    context: workdir_utils.WorkspaceContext,
    settings,
    *,
    require_active: bool = True,
) -> ResearchKnowledgeManager:
    if require_active and not settings.knowledge_links_active:
        raise HTTPException(409, "Research Knowledge Links are not active")
    return ResearchKnowledgeManager(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


def _pack_builder(
    context: workdir_utils.WorkspaceContext,
    settings,
    *,
    require_active: bool = True,
) -> ResearchPackV2Builder:
    if require_active and not settings.research_pack_active:
        raise HTTPException(409, "ResearchPack export is not active")
    return ResearchPackV2Builder(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


def _pack_importer(
    context: workdir_utils.WorkspaceContext,
    settings,
    *,
    require_active: bool = True,
) -> ResearchPackImporter:
    if require_active and not settings.research_pack_import_active:
        raise HTTPException(409, "ResearchPack import is not active")
    return ResearchPackImporter(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )


def _experiment_owner(context) -> dict[str, str]:
    return {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
        "workdir": _workspace_path(context),
    }


def _ensure_workflow_run(workflow_run_id: str, context) -> None:
    run_id = workflow_run_id.strip()
    if not run_id:
        return
    run = WorkflowRunManager.get(run_id, workdir=_workspace_path(context))
    if run is None:
        raise HTTPException(404, f"workflow run not found: {run_id}")
    metadata = dict(run.metadata or {})
    for key, expected in (("tenant_id", context.tenant_id), ("user_id", context.user_id), ("workspace_id", context.workspace_id)):
        actual = str(metadata.get(key) or "").strip()
        if actual and actual != str(expected):
            raise HTTPException(404, f"workflow run not found: {run_id}")


@router.get("/projects/{research_id}/hypotheses")
async def list_research_hypotheses(research_id: str, request: Request):
    context = _workspace_context(request)
    items = await asyncio.to_thread(
        ResearchExperimentStore.list_hypotheses,
        research_id,
        **_experiment_owner(context),
    )
    return {"count": len(items), "hypotheses": [item.to_dict() for item in items]}


@router.post("/projects/{research_id}/hypotheses")
async def create_research_hypothesis(research_id: str, payload: HypothesisWriteRequest, request: Request):
    context = _workspace_context(request)
    try:
        item = await asyncio.to_thread(
            ResearchExperimentStore.create_hypothesis,
            research_id=research_id,
            statement=payload.statement,
            rationale=payload.rationale,
            test_plan=payload.test_plan,
            falsification_condition=payload.falsification_condition,
            evidence_refs=payload.evidence_refs,
            counter_evidence_refs=payload.counter_evidence_refs,
            created_by=context.user_id,
            **_experiment_owner(context),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"hypothesis": item.to_dict()}


@router.post("/projects/{research_id}/hypotheses/generate")
async def generate_research_hypotheses(research_id: str, payload: HypothesisGenerateRequest, request: Request):
    context = _workspace_context(request); owner = _experiment_owner(context)
    checkpoint = await asyncio.to_thread(ResearchDerivationStore.get_checkpoint, research_id, **owner)
    if checkpoint is None: raise HTTPException(409, "research checkpoint is required before generating hypotheses")
    raw = [*(checkpoint.working_hypotheses or []), *(checkpoint.open_questions or []), *(checkpoint.next_actions or [])]
    statements: list[str] = []
    for value in raw:
        text = str(value.get("statement") or value.get("value") or value.get("summary") or value.get("title") or "").strip() if isinstance(value, dict) else str(value).strip()
        if text and text not in statements: statements.append(text)
    existing = {item.statement.strip().casefold() for item in await asyncio.to_thread(ResearchExperimentStore.list_hypotheses, research_id, **owner)}
    created = []
    for statement in statements:
        if statement.casefold() in existing: continue
        created.append(await asyncio.to_thread(ResearchExperimentStore.create_hypothesis, research_id=research_id, statement=statement, rationale="从当前研究检查点提取，需用户审核", test_plan="请在批准前补充验证计划", falsification_condition="请在批准前补充证伪条件", created_by="agent", **owner))
        if len(created) >= payload.limit: break
    return {"count": len(created), "hypotheses": [item.to_dict() for item in created]}


@router.put("/hypotheses/{hypothesis_id}")
async def revise_research_hypothesis(hypothesis_id: str, payload: HypothesisWriteRequest, request: Request):
    context = _workspace_context(request)
    try:
        item = await asyncio.to_thread(
            ResearchExperimentStore.revise_hypothesis,
            hypothesis_id,
            statement=payload.statement,
            rationale=payload.rationale,
            test_plan=payload.test_plan,
            falsification_condition=payload.falsification_condition,
            **_experiment_owner(context),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"hypothesis": item.to_dict()}


@router.post("/hypotheses/{hypothesis_id}/status")
async def set_research_hypothesis_status(hypothesis_id: str, payload: HypothesisStatusRequest, request: Request):
    context = _workspace_context(request)
    try:
        item = await asyncio.to_thread(
            ResearchExperimentStore.set_hypothesis_status,
            hypothesis_id,
            payload.status,
            **_experiment_owner(context),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"hypothesis": item.to_dict()}


@router.get("/projects/{research_id}/experiments")
async def list_research_experiments(research_id: str, request: Request):
    context = _workspace_context(request)
    items = await asyncio.to_thread(
        ResearchExperimentStore.list_experiments,
        research_id,
        **_experiment_owner(context),
    )
    return {"count": len(items), "experiments": [item.to_dict() for item in items]}


@router.get("/projects/{research_id}/experiment-runs")
async def list_research_experiment_runs(research_id: str, request: Request, page: int = 1):
    from ..research.workspace_service import ResearchWorkspaceService
    context = _workspace_context(request)
    service = ResearchWorkspaceService(tenant_id=context.tenant_id, user_id=context.user_id,
        workspace_id=context.workspace_id, workdir=_workspace_path(context))
    await asyncio.to_thread(service._project, research_id)
    return await asyncio.to_thread(service.experiment_runs, page=page, page_size=10)


@router.post("/projects/{research_id}/experiments")
async def create_research_experiment(research_id: str, payload: ExperimentCreateRequest, request: Request):
    context = _workspace_context(request)
    _ensure_workflow_run(payload.workflow_run_id, context)
    try:
        item = await asyncio.to_thread(
            ResearchExperimentStore.create_experiment,
            research_id=research_id,
            hypothesis_id=payload.hypothesis_id,
            title=payload.title,
            reason=payload.reason,
            workflow_run_id=payload.workflow_run_id,
            parent_experiment_id=payload.parent_experiment_id,
            execution_mode=payload.execution_mode,
            input_artifacts=payload.input_artifacts,
            created_by=context.user_id,
            **_experiment_owner(context),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"experiment": item.to_dict()}


@router.patch("/experiments/{experiment_id}")
async def update_research_experiment(experiment_id: str, payload: ExperimentUpdateRequest, request: Request):
    context = _workspace_context(request)
    _ensure_workflow_run(payload.workflow_run_id, context)
    try:
        item = await asyncio.to_thread(
            ResearchExperimentStore.update_experiment,
            experiment_id,
            status=payload.status,
            decision=payload.decision,
            result_summary=payload.result_summary,
            workflow_run_id=payload.workflow_run_id,
            **_experiment_owner(context),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"experiment": item.to_dict()}


@router.get("/experiments/{experiment_id}/compare/{other_experiment_id}")
async def compare_research_experiments(experiment_id: str, other_experiment_id: str, request: Request):
    context = _workspace_context(request)
    try:
        left, right = await asyncio.gather(
            asyncio.to_thread(ResearchExperimentStore.get_experiment, experiment_id, **_experiment_owner(context)),
            asyncio.to_thread(ResearchExperimentStore.get_experiment, other_experiment_id, **_experiment_owner(context)),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if left.research_id != right.research_id:
        raise HTTPException(404, "experiments not found in the same project")
    left_value, right_value = left.to_dict(), right.to_dict()
    fields = ("hypothesis_id", "hypothesis_revision", "status", "decision", "result_summary", "run_snapshot")
    return {
        "left": left_value,
        "right": right_value,
        "same_hypothesis": left.hypothesis_id == right.hypothesis_id,
        "differences": {
            field: {"left": left_value.get(field), "right": right_value.get(field)}
            for field in fields if left_value.get(field) != right_value.get(field)
        },
    }


@router.get("/projects/{research_id}/continuation")
async def get_research_continuation(research_id: str, request: Request):
    context = _workspace_context(request)
    item = await asyncio.to_thread(ResearchContinuationStore.get, research_id, **_experiment_owner(context))
    if item is not None:
        item = await asyncio.to_thread(ResearchContinuationStore.reconcile, research_id, **_experiment_owner(context))
    return {"continuation": item.to_dict() if item else None}


@router.post("/projects/{research_id}/continuation/start")
async def start_research_continuation(research_id: str, payload: ContinuationStartRequest, request: Request):
    context = _workspace_context(request)
    try:
        item = await asyncio.to_thread(ResearchContinuationStore.start, research_id, max_runs=payload.max_runs, max_minutes=payload.max_minutes, max_tokens=payload.max_tokens, max_tool_calls=payload.max_tool_calls, max_failures=payload.max_failures, max_cost_micros=payload.max_cost_micros, checkpoint_every=payload.checkpoint_every, **_experiment_owner(context))
        next_run = await asyncio.to_thread(ResearchContinuationStore.advance, research_id, **_experiment_owner(context))
        if next_run is not None:
            from .workflow_api import _schedule_workflow_auto_continue_for_run
            _schedule_workflow_auto_continue_for_run(next_run, workdir=_workspace_path(context))
    except LookupError as exc: raise HTTPException(404, str(exc)) from exc
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    return {"continuation": item.to_dict()}


@router.post("/projects/{research_id}/continuation/{action}")
async def control_research_continuation(research_id: str, action: str, request: Request):
    context = _workspace_context(request)
    if action not in {"pause", "resume", "reconcile"}: raise HTTPException(404, "unknown continuation action")
    try:
        if action == 'resume':
            current = await asyncio.to_thread(ResearchContinuationStore.reconcile, research_id, **_experiment_owner(context))
            if current.stop_reason.endswith('_limit_reached'):
                raise HTTPException(409, '续跑预算已达上限，恢复不会追加预算；请明确调整续跑预算后再继续。')
        item = await asyncio.to_thread(
            ResearchContinuationStore.reconcile if action == "reconcile" else ResearchContinuationStore.set_status,
            research_id, *(() if action == "reconcile" else (("paused" if action == "pause" else "running"),)), **_experiment_owner(context),
        )
        if action == "resume":
            next_run = await asyncio.to_thread(ResearchContinuationStore.advance, research_id, **_experiment_owner(context))
            if next_run is not None:
                from .workflow_api import _schedule_workflow_auto_continue_for_run
                _schedule_workflow_auto_continue_for_run(next_run, workdir=_workspace_path(context))
            item = await asyncio.to_thread(ResearchContinuationStore.reconcile, research_id, **_experiment_owner(context))
    except LookupError as exc: raise HTTPException(404, str(exc)) from exc
    return {"continuation": item.to_dict()}


@router.post("/projects/{research_id}/reproductions")
async def create_research_reproduction(research_id: str, payload: ReproductionCreateRequest, request: Request):
    context = _workspace_context(request); owner = _experiment_owner(context)
    source = await asyncio.to_thread(ResearchPackAssetStore.get_object, payload.asset_id, payload.source_experiment_id, object_type="experiment", **owner)
    if source is None: raise HTTPException(404, "source experiment was not found in ResearchPack")
    source_value = dict(source.payload or {})
    source_hypothesis_id = str(source_value.get("hypothesis_id") or "")
    source_hypothesis = await asyncio.to_thread(ResearchPackAssetStore.get_object, payload.asset_id, source_hypothesis_id, object_type="hypothesis", **owner)
    if source_hypothesis is None: raise HTTPException(409, "source hypothesis is missing from ResearchPack")
    hypothesis_value = dict(source_hypothesis.payload or {})
    hypothesis = await asyncio.to_thread(ResearchExperimentStore.create_hypothesis, research_id=research_id, statement=str(hypothesis_value.get("statement") or "复现来源假设"), rationale=f"复现 ResearchPack {payload.asset_id}", test_plan=str(hypothesis_value.get("test_plan") or ""), falsification_condition=str(hypothesis_value.get("falsification_condition") or ""), created_by=context.user_id, **owner)
    await asyncio.to_thread(ResearchExperimentStore.set_hypothesis_status, hypothesis.hypothesis_id, "approved", **owner)
    snapshot = dict(source_value.get("run_snapshot") or {})
    template_key = str(snapshot.get("template_key") or "").strip()
    issues: list[str] = []
    if not snapshot: issues.append("source_run_snapshot_missing")
    if not payload.session_id.strip(): issues.append("target_session_required")
    if template_key and WorkflowTemplateRegistry.get(template_key) is None: issues.append("workflow_template_unavailable")
    if snapshot and not template_key: issues.append("workflow_template_missing")
    template = WorkflowTemplateRegistry.get(template_key) if template_key else None
    if template and snapshot.get("template_digest"):
        current_template_digest = hashlib.sha256(
            json.dumps(template.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if current_template_digest != snapshot.get("template_digest"):
            issues.append("workflow_template_changed")
    source_environment = dict(snapshot.get("environment") or {})
    source_commit = str(source_environment.get("git_commit") or "").strip()
    source_digest = str(source_environment.get("workspace_digest") or "").strip()
    current_environment = ResearchExperimentStore.workspace_git_state(owner["workdir"])
    current_commit = str(current_environment.get("git_commit") or "").strip()
    if source_commit and current_commit and source_commit != current_commit:
        issues.append("git_commit_mismatch")
    elif source_digest and current_environment.get("workspace_digest") != source_digest:
        issues.append("workspace_content_mismatch")
    run = None
    if not issues:
        run = await asyncio.to_thread(
            WorkflowRunManager.create_run,
            template_key=template_key,
            session_id=payload.session_id.strip(),
            input_payload=dict(snapshot.get("input") or {}),
            metadata={
                "tenant_id": context.tenant_id, "user_id": context.user_id,
                "workspace_id": context.workspace_id, "research_id": research_id,
                "created_from": "research_pack_reproduction",
                "source_asset_id": payload.asset_id,
                "source_experiment_id": payload.source_experiment_id,
            },
            workdir=owner["workdir"],
        )
    experiment = await asyncio.to_thread(ResearchExperimentStore.create_experiment, research_id=research_id, hypothesis_id=hypothesis.hypothesis_id, title=f"复现：{source_value.get('title') or payload.source_experiment_id}", reason="从导入的 ResearchPack 创建独立复现实验", workflow_run_id=run.run_id if run else "", source_asset_id=payload.asset_id, source_experiment_id=payload.source_experiment_id, created_by=context.user_id, **owner)
    if run is not None:
        from .workflow_api import _schedule_workflow_auto_continue_for_run
        _schedule_workflow_auto_continue_for_run(run, workdir=owner["workdir"])
    return {"hypothesis": hypothesis.to_dict(), "experiment": experiment.to_dict(), "source": source_value, "reproduction_ready": not issues, "compatibility_issues": issues}


@router.get("/experiments/{experiment_id}/reproduction-compare")
async def compare_research_reproduction(experiment_id: str, request: Request):
    context = _workspace_context(request); owner = _experiment_owner(context)
    try: local = await asyncio.to_thread(ResearchExperimentStore.get_experiment, experiment_id, **owner)
    except LookupError as exc: raise HTTPException(404, str(exc)) from exc
    if not local.source_asset_id or not local.source_experiment_id: raise HTTPException(409, "experiment is not a reproduction")
    source = await asyncio.to_thread(ResearchPackAssetStore.get_object, local.source_asset_id, local.source_experiment_id, object_type="experiment", **owner)
    if source is None: raise HTTPException(404, "source experiment is unavailable")
    original = dict(source.payload or {})
    fields = ("status", "decision", "result_summary", "workflow_run_id", "hypothesis_revision", "run_snapshot")
    return {"original": original, "reproduction": local.to_dict(), "differences": {field: {"original": original.get(field), "reproduction": local.to_dict().get(field)} for field in fields if original.get(field) != local.to_dict().get(field)}}


@router.get("/projects/{research_id}/episodes")
async def list_research_episodes(
    research_id: str,
    request: Request,
    include_inactive: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
):
    context = _workspace_context(request)
    episodes = ResearchDerivationStore.list_episodes(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        include_inactive=include_inactive,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(episodes), "episodes": [item.to_dict() for item in episodes]}


@router.post("/projects/{research_id}/episodes/refresh")
async def refresh_research_episodes(
    research_id: str,
    payload: EpisodeRefreshRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        report = _derivation_engine(context, settings).run_project(
            research_id,
            trigger_kind="manual_api",
            force_open=payload.force_open,
            force_reason="manual_save",
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return {"status": report.status, "report": report.to_dict()}


@router.get("/episodes/{episode_id}")
async def get_research_episode(episode_id: str, request: Request):
    context = _workspace_context(request)
    path = _workspace_path(context)
    episode = ResearchDerivationStore.get_episode(
        episode_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=path,
    )
    if episode is None:
        raise HTTPException(404, "episode not found")
    project_events = ResearchDerivationStore.list_project_events(
        episode.research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=5000,
        workdir=path,
    )
    source_ids = set(episode.event_ids)
    events = [event for event in project_events if event.event_id in source_ids]
    return {
        "episode": episode.to_dict(),
        "events": [event.to_dict() for event in events],
    }


@router.get("/projects/{research_id}/checkpoint")
async def get_research_checkpoint(research_id: str, request: Request):
    context = _workspace_context(request)
    checkpoint = ResearchDerivationStore.get_checkpoint(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    return {"checkpoint": checkpoint.to_dict() if checkpoint else None}


@router.post("/projects/{research_id}/checkpoint/refresh")
async def refresh_research_checkpoint(research_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    if not settings.checkpoints_active:
        raise HTTPException(409, "Checkpoint Preview is not active")
    try:
        checkpoint, created = _derivation_engine(context, settings).refresh_checkpoint(
            research_id
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    return {
        "created": created,
        "checkpoint": checkpoint.to_dict() if checkpoint else None,
    }


@router.get("/projects/{research_id}/context-preview")
async def preview_research_context(research_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        capsule = ResearchDerivationEngine(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        ).context_capsule(research_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"capsule": capsule}


@router.post("/projects/{research_id}/continuation-context/preview")
async def preview_research_continuation_context(
    research_id: str,
    payload: ResearchContextPreviewRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    project = ResearchTopicStore.get_project(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if project is None:
        raise HTTPException(404, "Research Project was not found")
    try:
        result = ResearchContinuationContextBuilder(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workdir=_workspace_path(context),
            settings=settings,
        ).build(
            payload.question,
            research_id=research_id,
            persist_audit=False,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"context": result.to_dict(include_prompt=True), "settings": _settings_summary()}


@router.get("/projects/{research_id}/read/brief")
async def get_research_read_brief(research_id: str, request: Request):
    """Return the bounded routing layer for a research project."""

    context = _workspace_context(request)
    try:
        return _read_service(context).get_brief(research_id)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.post("/projects/{research_id}/read/handoff")
async def get_research_read_handoff(
    research_id: str,
    payload: ResearchReadHandoffRequest,
    request: Request,
):
    """Build a quality-gated handoff snapshot for a question or new agent."""

    context = _workspace_context(request)
    try:
        return _read_service(context).get_handoff(
            research_id,
            question=payload.question,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.post("/projects/{research_id}/read/search")
async def search_research_read_layer(
    research_id: str,
    payload: ResearchReadSearchRequest,
    request: Request,
):
    """Search canonical and linked knowledge layers inside one project scope."""

    context = _workspace_context(request)
    try:
        return _read_service(context).search(
            research_id,
            payload.query,
            limit=payload.limit,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/projects/{research_id}/read/objects")
async def list_research_read_objects(
    research_id: str,
    request: Request,
    object_type: str = Query(..., min_length=1, max_length=40),
    query: str = Query("", max_length=1000),
    status: list[str] = Query(default=[]),
    limit: int = Query(20, ge=1, le=500),
    cursor: str = Query("", max_length=4000),
):
    """Page through first-class research objects with opaque cursors."""

    context = _workspace_context(request)
    service = _read_service(context)
    kind = object_type.strip().lower()
    try:
        if kind == "finding":
            return service.list_findings(
                research_id,
                query=query,
                statuses=status,
                limit=limit,
                cursor=cursor,
            )
        if kind == "conflict":
            return service.list_conflicts(
                research_id,
                statuses=status or ("open", "under_review"),
                query=query,
                limit=limit,
                cursor=cursor,
            )
        if kind == "episode":
            return service.list_episodes(
                research_id,
                query=query,
                limit=limit,
                cursor=cursor,
            )
        if kind == "experience":
            return service.list_experience(
                research_id,
                query=query,
                limit=limit,
                cursor=cursor,
            )
        raise ValueError(f"unsupported research object type: {kind}")
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/projects/{research_id}/read/objects/{object_type}/{object_id}")
async def get_research_read_object(
    research_id: str,
    object_type: str,
    object_id: str,
    request: Request,
):
    """Read one exact object, including its provenance and layer availability."""

    context = _workspace_context(request)
    try:
        return _read_service(context).get_object(
            research_id,
            object_type,
            object_id,
        )
    except (FileNotFoundError, ValueError) as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.lower() or "not linked" in message.lower() else 400
        raise HTTPException(status_code, message)


@router.get("/contexts/audits")
async def list_research_context_audits(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
):
    context = _workspace_context(request)
    return {
        "audits": ResearchContextAuditStore.list_recent(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            limit=limit,
            workdir=_workspace_path(context),
        )
    }


@router.get("/projects/{research_id}/findings")
async def list_research_findings(
    research_id: str,
    request: Request,
    status: list[str] = Query(default=[]),
    limit: int = Query(500, ge=1, le=1000),
):
    context = _workspace_context(request)
    findings = ResearchDerivationStore.list_findings(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        statuses=status,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(findings), "findings": [item.to_dict() for item in findings]}


@router.post("/projects/{research_id}/findings")
async def create_research_finding(
    research_id: str,
    payload: FindingCreateRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        finding, change, created = _evolution_manager(
            context, load_research_settings()
        ).create_finding(
            research_id,
            statement=payload.statement,
            provenance=payload.provenance,
            source_event_ids=payload.source_event_ids,
            evidence_ids=payload.evidence_ids,
            episode_id=payload.episode_id,
            scope=payload.scope,
            actor_id=context.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"created": created, "finding": finding.to_dict(), "change": change}


@router.get("/findings/{finding_id}/history")
async def get_research_finding_history(finding_id: str, request: Request):
    context = _workspace_context(request)
    finding = ResearchDerivationStore.get_finding(
        finding_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    if finding is None:
        raise HTTPException(404, "finding not found")
    versions = ResearchDerivationStore.list_finding_versions(
        finding_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
    )
    return {"finding": finding.to_dict(), "versions": versions}


@router.post("/findings/{finding_id}/challenge")
async def challenge_research_finding(
    finding_id: str,
    payload: FindingChallengeRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        original, challenger, conflict, change = _evolution_manager(
            context, load_research_settings()
        ).challenge_finding(
            finding_id,
            statement=payload.statement,
            provenance=payload.provenance,
            source_event_ids=payload.source_event_ids,
            evidence_ids=payload.evidence_ids,
            episode_id=payload.episode_id,
            scope=payload.scope,
            conflict_type=payload.conflict_type,
            summary=payload.summary,
            actor_id=context.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "original": original.to_dict(),
        "challenger": challenger.to_dict(),
        "conflict": conflict.to_dict(),
        "change": change,
    }


@router.get("/projects/{research_id}/changes")
async def list_research_changes(
    research_id: str,
    request: Request,
    limit: int = Query(500, ge=1, le=1000),
):
    context = _workspace_context(request)
    changes = ResearchDerivationStore.list_changes(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(changes), "changes": changes}


@router.get("/projects/{research_id}/conflicts")
async def list_research_conflicts(
    research_id: str,
    request: Request,
    status: list[str] = Query(default=[]),
    limit: int = Query(500, ge=1, le=1000),
):
    context = _workspace_context(request)
    conflicts = ResearchDerivationStore.list_conflicts(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        statuses=status,
        limit=limit,
        workdir=_workspace_path(context),
    )
    return {"count": len(conflicts), "conflicts": [item.to_dict() for item in conflicts]}


@router.post("/projects/{research_id}/conflicts")
async def create_research_conflict(
    research_id: str,
    payload: ConflictCreateRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        conflict, change, created = _evolution_manager(
            context, load_research_settings()
        ).create_conflict(
            research_id,
            conflict_type=payload.conflict_type,
            original_refs=payload.original_refs,
            challenging_refs=payload.challenging_refs,
            summary=payload.summary,
            actor_id=context.user_id,
            original_evidence_ids=payload.original_evidence_ids,
            challenging_evidence_ids=payload.challenging_evidence_ids,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"created": created, "conflict": conflict.to_dict(), "change": change}


@router.get("/conflicts/{conflict_id}")
async def get_research_conflict(conflict_id: str, request: Request):
    context = _workspace_context(request)
    path = _workspace_path(context)
    conflict = ResearchDerivationStore.get_conflict(
        conflict_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=path,
    )
    if conflict is None:
        raise HTTPException(404, "conflict not found")
    resolutions = ResearchDerivationStore.list_resolutions(
        conflict_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=path,
    )
    return {
        "conflict": conflict.to_dict(),
        "resolutions": [item.to_dict() for item in resolutions],
    }


@router.post("/conflicts/{conflict_id}/review")
async def review_research_conflict(
    conflict_id: str,
    payload: ConflictReviewRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        conflict, change = _evolution_manager(
            context, load_research_settings()
        ).review_conflict(
            conflict_id,
            actor_id=context.user_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"conflict": conflict.to_dict(), "change": change}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_research_conflict(
    conflict_id: str,
    payload: ConflictResolveRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        conflict, resolution, change = _evolution_manager(
            context, load_research_settings()
        ).resolve_conflict(
            conflict_id,
            result=payload.result,
            actor_id=context.user_id,
            reason=payload.reason,
            evidence_ids=payload.evidence_ids,
            scope=payload.scope,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "conflict": conflict.to_dict(),
        "resolution": resolution.to_dict(),
        "change": change,
    }


@router.post("/conflicts/{conflict_id}/reopen")
async def reopen_research_conflict(
    conflict_id: str,
    payload: ConflictReopenRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        conflict, change = _evolution_manager(
            context, load_research_settings()
        ).reopen_conflict(
            conflict_id,
            actor_id=context.user_id,
            reason=payload.reason,
            evidence_ids=payload.evidence_ids,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"conflict": conflict.to_dict(), "change": change}


@router.get("/projects/{research_id}/knowledge")
async def get_research_knowledge(
    research_id: str,
    request: Request,
    include_history: bool = Query(False),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        snapshot = _knowledge_manager(
            context, settings, require_active=False
        ).snapshot(research_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if include_history:
        links = ResearchKnowledgeStore.list_links(
            research_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            include_history=True,
            limit=10000,
            workdir=_workspace_path(context),
            settings=settings,
        )
        snapshot["history"] = [item.to_dict() for item in links]
    snapshot["settings"] = {
        "knowledge_links_enabled": settings.knowledge_links_enabled,
        "knowledge_links_active": settings.knowledge_links_active,
    }
    return snapshot


@router.post("/projects/{research_id}/knowledge/refresh")
async def refresh_research_knowledge(research_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        report = _knowledge_manager(context, settings).refresh(research_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "completed", "report": report}


@router.post("/projects/{research_id}/knowledge/lint")
async def lint_research_knowledge(research_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        report = _knowledge_manager(context, settings).lint(research_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"lint": report.to_dict()}


@router.post("/projects/{research_id}/knowledge/links")
async def create_research_knowledge_link(
    research_id: str,
    payload: KnowledgeLinkRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        link = _knowledge_manager(context, settings).create_manual_link(
            research_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            object_type=payload.object_type,
            object_id=payload.object_id,
            relation=payload.relation,
            side=payload.side,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"link": link.to_dict()}


@router.post("/knowledge/links/{link_id}/remove")
async def remove_research_knowledge_link(link_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        link = _knowledge_manager(context, settings).remove_link(link_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"link": link.to_dict()}


@router.post("/projects/{research_id}/packs/preview")
async def preview_research_pack(
    research_id: str,
    payload: ResearchPackPreviewRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        preview = _pack_builder(context, settings).preview_scope(
            ResearchPackV2Scope(
                mode="project",
                research_ids=[research_id],
                started_at=payload.started_at,
                ended_at=payload.ended_at,
            ),
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"preview": preview.to_dict()}


@router.post("/projects/{research_id}/packs/build")
async def build_research_pack(
    research_id: str,
    payload: ResearchPackBuildRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        export = _pack_builder(context, settings).build_scope(
            ResearchPackV2Scope(
                mode="project",
                research_ids=[research_id],
                started_at=payload.started_at,
                ended_at=payload.ended_at,
            ),
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
            expected_source_digest=payload.expected_source_digest,
            acknowledge_privacy=payload.acknowledge_privacy,
            display_name=payload.display_name,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if (
            "changed" in message or "blocked" in message or "acknowledgement" in message
        ) else 400
        raise HTTPException(status_code, message)
    return {"export": export.to_dict()}


@router.get("/projects/{research_id}/packs")
async def list_research_packs(
    research_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    builder = _pack_builder(context, settings, require_active=False)
    try:
        builder._project(research_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    exports = ResearchPackStore.list_exports(
        research_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit,
        workdir=_workspace_path(context),
        settings=settings,
    )
    return {
        "count": len(exports),
        "exports": [item.to_dict() for item in exports],
        "settings": {
            "research_pack_enabled": settings.research_pack_enabled,
            "research_pack_active": settings.research_pack_active,
            "research_pack_max_bytes": settings.research_pack_max_bytes,
        },
    }


def _pack_scope(payload: ResearchPackPreviewRequest) -> ResearchPackV2Scope:
    return ResearchPackV2Scope(
        mode=payload.mode,
        research_ids=payload.research_ids,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
    )


@router.post("/packs/preview")
async def preview_research_pack_scope(
    payload: ResearchPackPreviewRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        preview = _pack_builder(context, settings).preview_scope(
            _pack_scope(payload),
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"preview": preview.to_dict()}


@router.post("/packs/build")
async def build_research_pack_scope(
    payload: ResearchPackBuildRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        export = _pack_builder(context, settings).build_scope(
            _pack_scope(payload),
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
            expected_source_digest=payload.expected_source_digest,
            acknowledge_privacy=payload.acknowledge_privacy,
            display_name=payload.display_name,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if (
            "changed" in message
            or "blocked" in message
            or "acknowledgement" in message
        ) else 400
        raise HTTPException(status_code, message)
    return {"export": export.to_dict()}


@router.get("/packs")
async def list_workspace_research_packs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    exports = ResearchPackStore.list_exports(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit,
        workdir=_workspace_path(context),
        settings=settings,
    )
    return {
        "count": len(exports),
        "exports": [item.to_dict() for item in exports],
        "settings": {
            "research_pack_enabled": settings.research_pack_enabled,
            "research_pack_active": settings.research_pack_active,
            "research_pack_max_bytes": settings.research_pack_max_bytes,
        },
    }


@router.get("/packs/{pack_id}/download")
async def download_research_pack(pack_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    export = ResearchPackStore.get_export(
        pack_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if export is None:
        raise HTTPException(404, "ResearchPack was not found")
    path = ResearchPackStore.archive_file(
        export,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if path is None:
        raise HTTPException(409, "ResearchPack archive is not ready")
    return FileResponse(
        path.as_posix(),
        media_type="application/zip",
        filename=export.archive_name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/packs/imports/inspect")
async def inspect_research_pack_import(
    request: Request,
    file: UploadFile = File(...),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    importer = _pack_importer(context, settings)
    filename = Path(file.filename or "").name
    if not filename.endswith(".researchpack.zip"):
        raise HTTPException(400, "only .researchpack.zip archives can be imported")
    root = settings.research_pack_import_path(_workspace_path(context))
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".upload-{uuid.uuid4().hex}.researchpack.zip"
    size = 0
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.research_pack_import_max_archive_bytes:
                    raise HTTPException(413, "ResearchPack archive exceeds the compressed size limit")
                output.write(chunk)
        if size <= 0:
            raise HTTPException(400, "ResearchPack archive is empty")
        inspection = importer.inspect_upload(temporary, archive_name=filename)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()
    return {"inspection": inspection.to_dict()}


@router.post("/packs/imports")
async def import_research_pack(
    payload: ResearchPackImportRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        imported = _pack_importer(context, settings).import_copy(
            payload.inspection_id,
            duplicate_action=payload.duplicate_action,
            title=payload.title,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        status = 409 if any(
            token in message.lower()
            for token in ("workspace", "expired", "changed", "already", "not active")
        ) else 400
        raise HTTPException(status, message)
    if imported.status == "completed" and imported.asset_id:
        _schedule_pack_index(context, imported.asset_id)
    return {"import": imported.to_dict()}


@router.get("/packs/imports")
async def list_research_pack_imports(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    hidden: bool = False,
    offset: int = Query(0, ge=0, le=1000000),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    imports = await asyncio.to_thread(
        ResearchPackImportStore.list_imports,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit + 1,
        hidden=hidden,
        offset=offset,
        workdir=_workspace_path(context),
        settings=settings,
    )
    return {
        "count": min(len(imports), limit),
        "imports": [item.to_dict() for item in imports[:limit]],
        "next_offset": offset + limit if len(imports) > limit else None,
        "settings": {
            "research_pack_import_enabled": settings.research_pack_import_enabled,
            "research_pack_import_active": settings.research_pack_import_active,
            "research_pack_import_recovery_enabled": (
                settings.research_pack_import_recovery_enabled
            ),
            "max_archive_bytes": settings.research_pack_import_max_archive_bytes,
            "inspection_ttl_minutes": settings.research_pack_import_inspection_ttl_minutes,
        },
    }


@router.get("/packs/imports/{inspection_or_import_id}")
async def get_research_pack_import(
    inspection_or_import_id: str,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    item = ResearchPackImportStore.get(
        inspection_or_import_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workdir=_workspace_path(context),
        settings=settings,
    )
    if item is None:
        raise HTTPException(404, "ResearchPack import was not found")
    return {"import": item.to_dict()}


@router.get("/pack-assets")
async def list_research_pack_assets(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    archived: bool = False,
    offset: int = Query(0, ge=0, le=1000000),
):
    context = _workspace_context(request)
    settings = load_research_settings()
    assets = ResearchPackAssetStore.list_assets(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        limit=limit + 1,
        status='archived' if archived else 'active',
        offset=offset,
        workdir=_workspace_path(context),
        settings=settings,
    )
    return {
        "count": min(len(assets), limit),
        "assets": [item.to_dict() for item in assets[:limit]],
        "next_offset": offset + limit if len(assets) > limit else None,
    }


async def _set_pack_listing_removed(request: Request, object_id: str, kind: str, removed: bool):
    context = _workspace_context(request)
    store = ResearchPackImportStore if kind == 'import' else ResearchPackAssetStore
    method = store.set_hidden if kind == 'import' else store.set_archived
    try:
        changed = await asyncio.to_thread(method, object_id,
            tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id,
            workdir=_workspace_path(context), **({'hidden':removed} if kind == 'import' else {'archived':removed}))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not changed:
        raise HTTPException(404, '研究资料或记录不存在')
    return {'removed':removed, 'id':object_id}


@router.delete('/packs/imports/{import_id}')
async def remove_research_pack_import(import_id: str, request: Request):
    return await _set_pack_listing_removed(request, import_id, 'import', True)


@router.post('/packs/imports/{import_id}/restore')
async def restore_research_pack_import(import_id: str, request: Request):
    return await _set_pack_listing_removed(request, import_id, 'import', False)


@router.delete('/pack-assets/{asset_id}')
async def remove_research_pack_asset(asset_id: str, request: Request):
    return await _set_pack_listing_removed(request, asset_id, 'asset', True)


@router.post('/pack-assets/{asset_id}/restore')
async def restore_research_pack_asset(asset_id: str, request: Request):
    return await _set_pack_listing_removed(request, asset_id, 'asset', False)


@router.get("/pack-assets/{asset_id}")
async def get_research_pack_asset(asset_id: str, request: Request):
    context = _workspace_context(request)
    settings = load_research_settings()
    owner = {
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "workspace_id": context.workspace_id,
    }
    path = _workspace_path(context)
    asset = ResearchPackAssetStore.get_asset(
        asset_id,
        **owner,
        workdir=path,
        settings=settings,
    )
    if asset is None:
        raise HTTPException(404, "ResearchPack asset was not found")
    scopes = ResearchPackAssetStore.list_scopes(
        asset.asset_id,
        **owner,
        workdir=path,
        settings=settings,
    )
    overlays = ResearchPackAssetStore.list_overlays(
        asset.asset_id,
        **owner,
        workdir=path,
        settings=settings,
    )
    return {
        "asset": asset.to_dict(),
        "scopes": [item.to_dict() for item in scopes],
        "overlays": [item.to_dict() for item in overlays],
        "object_counts": ResearchPackAssetStore.object_counts(
            asset.asset_id,
            **owner,
            workdir=path,
            settings=settings,
        ),
        "overlay_object_counts": ResearchPackAssetStore.overlay_object_counts(
            asset.asset_id,
            **owner,
            workdir=path,
            settings=settings,
        ),
    }


@router.get("/pack-assets/{asset_id}/read/overview")
async def get_research_pack_asset_read_overview(asset_id: str, request: Request):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).get_pack_overview,
            asset_id,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/pack-assets/{asset_id}/read/scopes")
async def list_research_pack_asset_read_scopes(asset_id: str, request: Request):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).list_pack_scopes,
            asset_id,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.post("/pack-assets/{asset_id}/read/search")
async def search_research_pack_asset_read_layer(
    asset_id: str,
    payload: ResearchPackReadSearchRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        result = await asyncio.to_thread(
            _read_service(context).search_pack,
            asset_id,
            payload.query,
            source_research_id=payload.source_research_id,
            limit=payload.limit,
            ready_only=True,
        )
        if (
            dict(result.get("index") or {}).get("status") != "ready"
            or bool(result.get("index_stale", False))
        ):
            _schedule_pack_index(context, asset_id)
        return result
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/pack-assets/{asset_id}/read/index")
async def get_research_pack_asset_index_status(asset_id: str, request: Request):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).get_pack_index_status,
            asset_id,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.post("/pack-assets/{asset_id}/read/index/rebuild")
async def rebuild_research_pack_asset_index(
    asset_id: str,
    payload: ResearchPackIndexRebuildRequest,
    request: Request,
):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).rebuild_pack_index,
            asset_id,
            force_embeddings=payload.force_embeddings,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/pack-assets/{asset_id}/read/objects")
async def list_research_pack_asset_read_objects(
    asset_id: str,
    request: Request,
    object_type: str = Query("", max_length=40),
    source_research_id: str = Query("", max_length=160),
    query: str = Query("", max_length=1000),
    limit: int = Query(20, ge=1, le=500),
    cursor: str = Query("", max_length=4000),
):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).list_pack_objects,
            asset_id,
            object_type=object_type,
            source_research_id=source_research_id,
            query=query,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.get("/pack-assets/{asset_id}/read/objects/{object_type}/{object_id}")
async def get_research_pack_asset_read_object(
    asset_id: str,
    object_type: str,
    object_id: str,
    request: Request,
):
    context = _workspace_context(request)
    try:
        return await asyncio.to_thread(
            _read_service(context).get_pack_object,
            asset_id,
            object_type,
            object_id,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 400, str(exc))


@router.post("/pack-assets/{asset_id}/packs/preview")
async def preview_research_pack_asset_overlay(
    asset_id: str,
    payload: ResearchPackPreviewRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        preview = await asyncio.to_thread(
            _pack_builder(context, settings).preview_asset_overlay,
            asset_id,
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"preview": preview.to_dict()}


@router.post("/pack-assets/{asset_id}/packs/build")
async def build_research_pack_asset_overlay(
    asset_id: str,
    payload: ResearchPackBuildRequest,
    request: Request,
):
    context = _workspace_context(request)
    settings = load_research_settings()
    try:
        export = await asyncio.to_thread(
            _pack_builder(context, settings).build_asset_overlay,
            asset_id,
            privacy_profile=payload.privacy_profile,
            attribution_mode=payload.attribution_mode,
            expected_source_digest=payload.expected_source_digest,
            acknowledge_privacy=payload.acknowledge_privacy,
            display_name=payload.display_name,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if any(
            token in message.lower()
            for token in ("changed", "blocked", "acknowledgement", "conflict")
        ) else 400
        raise HTTPException(status_code, message)
    return {"export": export.to_dict()}


class HypothesisEvidenceReview(BaseModel):
    expected_source_hash: str = Field(max_length=128)
    object_type: str = Field(min_length=1, max_length=40)
    object_id: str = Field(min_length=1, max_length=500)
    relation: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=4000)
    scope: str = Field(default="", max_length=4000)
    hypothesis_revision: int = Field(ge=1)


def _hypothesis_map(request):
    from ..research.hypothesis_map import HypothesisMap
    context = _workspace_context(request)
    return HypothesisMap(tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id, workdir=_workspace_path(context))


@router.get("/projects/{research_id}/hypothesis-map")
async def get_hypothesis_map(research_id: str, request: Request, offset: int = Query(0, ge=0), hypothesis_id: str = Query("", max_length=160), relation_offset: int = Query(0, ge=0), source_offset: int = Query(0, ge=0)):
    try:
        return await asyncio.to_thread(_hypothesis_map(request).read, research_id, offset=offset, hypothesis_id=hypothesis_id, relation_offset=relation_offset, source_offset=source_offset)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc))


@router.get("/projects/{research_id}/hypothesis-map/source")
async def get_hypothesis_map_source(research_id: str, request: Request, object_type: str = Query(..., max_length=40), object_id: str = Query(..., max_length=500)):
    try:
        return await asyncio.to_thread(_hypothesis_map(request).read_source, research_id, object_type, object_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc))


@router.post("/projects/{research_id}/hypothesis-map/{hypothesis_id}/review")
async def review_hypothesis_evidence(research_id: str, hypothesis_id: str, payload: HypothesisEvidenceReview, request: Request):
    try:
        link = await asyncio.to_thread(_hypothesis_map(request).review, research_id, hypothesis_id, **payload.model_dump())
        return {"link": link}
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc))


@router.get("/pack-assets/{asset_id}/hypothesis-map")
async def get_pack_hypothesis_map(asset_id: str, request: Request, offset: int = Query(0, ge=0), hypothesis_id: str = Query("", max_length=160), relation_offset: int = Query(0, ge=0), source_offset: int = Query(0, ge=0)):
    from ..research.hypothesis_map import ImportedHypothesisMap
    context = _workspace_context(request)
    service = ImportedHypothesisMap(tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id, workdir=_workspace_path(context))
    try:
        return await asyncio.to_thread(service.read, asset_id, offset=offset, hypothesis_id=hypothesis_id, relation_offset=relation_offset, source_offset=source_offset)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc))


@router.get("/pack-assets/{asset_id}/hypothesis-map/source")
async def get_pack_hypothesis_source(asset_id: str, request: Request, object_type: str = Query(..., max_length=40), object_id: str = Query(..., max_length=500)):
    from ..research.hypothesis_map import ImportedHypothesisMap
    context = _workspace_context(request)
    service = ImportedHypothesisMap(tenant_id=context.tenant_id, user_id=context.user_id, workspace_id=context.workspace_id, workdir=_workspace_path(context))
    try:
        return await asyncio.to_thread(service.read_source, asset_id, object_type, object_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc))
