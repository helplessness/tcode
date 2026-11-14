"""Shared server orchestration and legacy import compatibility.

Workspace ownership, configuration files and session presentation live in their
own modules. New callers should import those modules directly.
"""

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import uuid
import os
import re
import signal
import socket
import subprocess
import weakref
import zipfile
import aiohttp
from datetime import datetime
from functools import lru_cache
from typing import Awaitable, Callable, Dict, Optional, Any
from pathlib import Path
from urllib.parse import urlencode

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Query,
    Request,
    UploadFile,
    File,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from ..file_lock import StateLockBusy
from fastapi.staticfiles import StaticFiles
import uvicorn

from dataclasses import asdict, replace

from ..agent import Agent
from ..agent.agents import BuiltinAgents
from ..types import AgentMode, Message
from ..team.catalog import TeamCatalog
from ..team.routing import TeamRouteRequest, TeamRouteRule, TeamRoutingPolicy
from ..team.registry import WorkerRegistry
from ..team.channel import TeamChannel
from ..team.peer_comm import get_team_peer_settings, list_peer_threads, set_team_peer_enabled
from ..team.memory import TeamMemoryManager
from ..team.profile import worker_profile_summary
from ..team.coordinator import (
    cancel_team_background_tasks,
    cancel_worker_task,
    worker_task_active,
)
from ..providers import create_provider, clone_provider
from ..mcp.manager_auto import MCPManager
from ..plugins.api import router as plugins_router
from .swarm_api import router as swarm_router
from .mcp_config_api import router as mcp_config_router
from .websearch_config_api import router as websearch_config_router
from .research_api import router as research_router
from .scheduled_tasks_api import router as scheduled_tasks_router
from .protein_runs_api import router as protein_runs_router
from .job_runtime_api import router as job_runtime_router
from .config_runtime_api import router as config_runtime_router
from ..plugins.command_registry import PluginCommandRegistry
from ..plugins.manager import PluginManager
from ..presentation import inspect_image_file
from ..protocol import question_request_event, question_timeout_event
from ..skill.manager import SkillManager
from ..config.manager import ConfigManager
from ..session.manager import SessionManager, Session
from ..memory import MemoryManager, EmbeddingProvider, FaissVectorStore
from ..context.router import route_context
from ..runtime import RuntimeTaskManager, TaskRuntimeStore
from ..runtime import RuntimeArtifactManager
from ..runtime import SessionArchiveManager
from ..runtime.guidance import GuidanceMode, GuidanceStatus, OwnerScope
from ..runtime.guidance_store import GuidanceSQLiteStore
from ..runtime.shared_tracker import SharedTracker
from tcode_agent.runtime.tracker_token import read_tracker_token
from ..runtime.resource_import import (
    attachment_kind as _attachment_kind,
    container_path_for_workspace_relative as _container_path_for_workspace_relative,
    resolve_resource_source as _resolve_resource_source,
    stage_resource_for_workspace as _stage_resource_for_workspace,
    workspace_relative_path as _workspace_relative_path,
)
from ..runtime.question_broker import decorate_child_question
from ..runtime.env_file import load_process_env_file
from ..guard import public_outcome_projection
from ..task.state_graph import TaskStateGraphManager
from ..task.orchestration import build_action_policy
from ..workflow import (
    WorkflowCandidate,
    WorkflowRouteDecision,
    WorkflowRun,
    WorkflowRunManager,
    WorkflowTemplate,
    WorkflowStageTemplate,
    WorkflowTemplateRegistry,
    WorkflowVersionConflictError,
    detect_workflow_route,
    guard_protein_design_brainstorming_route,
    is_protein_design_brainstorm_mode,
    should_auto_route_workflow_follow_up,
    workflow_routing_cards,
    parse_workflow_markers,
    strip_workflow_markers,
)
from ..workflow.clarifications import (
    build_workflow_questions,
    infer_answer_from_context,
    serialize_answer,
)
from ..tools.base import ToolContext
from ..tools.builtin.question import QuestionTool
from ..tools.builtin.process_runtime import ProcessRuntime
from . import workdirs as workdir_utils
from .workspace_files import is_user_visible_workspace_file
from .auth import (
    AUTH_COOKIE_NAME,
    CurrentUserContext,
    add_tenant_member,
    authenticate,
    auth_required,
    clear_auth_cookie,
    clear_login_session,
    create_tenant,
    create_user_account,
    create_login_session,
    current_user_from_request,
    current_user_from_websocket,
    delete_tenant_record,
    ensure_auth_store,
    list_shareable_tenant_members,
    list_tenant_members,
    list_user_tenants,
    remove_tenant_member,
    require_current_user,
    resolve_active_tenant_user,
    serialize_user,
    set_auth_cookie,
    set_tenant_status,
    switch_login_session_tenant,
    update_tenant_member_role,
    update_tenant_name,
    validate_tenant_deletion,
    tenant_user_summaries,
)
from . import workflow_runtime
from .plugin_warmup import (
    plugin_bootstrap_wait_seconds,
    wait_for_plugin_warmup,
)
from .state import ServerState
from .workspace_scope import (
    _workspace_context_from_request,
    _workspace_context_from_websocket,
    _session_owner_kwargs,
    _session_owner_kwargs_from_session,
    _runtime_session_matches_context,
    _user_workdirs_root,
    _workdir_template_root,
    _resolve_user_workdir_path,
    _workdir_name_from_path,
    _workspace_root_from_resource_path,
    _infer_workdir_from_resource_context,
    _list_user_workdirs,
    _create_user_workdir,
    _resolve_session_workdir,
    _resolve_workdir_from_name_or_path,
    _resolve_requested_workdir_for_session,
    _resolve_workflow_workdir,
)
from .config_files import (
    _tcode_config_path_for_workdir,
    _agent_mcp_config_path,
    _load_tcode_config,
    _save_tcode_config,
    _load_agent_mcp_config,
)
from .schemas import (
    BootstrapSessionRequest,
    ChatGuideRequest,
    HumanReviewApproveRequest,
    HumanReviewDeleteRequest,
    HumanReviewRejectRequest,
    HumanReviewSubmitRequest,
    JobActionRequest,
    JobPreviewTicketRequest,
    PermissionReplyRequest,
    QuestionReplyRequest,
    SelectModelRequest,
    SideQuestionRequest,
    SubagentChatRequest,
    SymphonyIssueRequest,
    SymphonyLocalIssueBatchDeleteRequest,
    SymphonyLocalIssueDeleteRequest,
    SymphonyLocalIssueRequest,
    SymphonyLocalIssueUpdateRequest,
    SymphonyJobCallbackRequest,
    TeamChatRequest,
    TeamPeerSettingsRequest,
    TeamRouteInspectRequest,
    TrackerAttachJobRefRequest,
    TrackerCancelRequest,
    TrackerClaimNextRequest,
    TrackerCompleteRequest,
    TrackerExecutionReportRequest,
    TrackerFailRequest,
    TrackerHeartbeatRequest,
    TrackerTaskCreateRequest,
    WorkdirCreateRequest,
    WorkflowCandidateUpsertRequest,
    WorkflowProposalActionRequest,
    WorkflowRunCreateRequest,
    WorkflowRunRerunRequest,
    WorkflowRunTerminateRequest,
    WorkflowStageExecuteRequest,
    WorkflowStageTransitionRequest,
    WorkflowTemplateUpsertRequest,
)

from .job_workspace import (
    _hydrate_workspace_from_job_files as _hydrate_workspace_from_job_files_impl,
    _build_job_workspace_summary,
    _build_issue_workspace_summary,
    _normalize_review_workspace,
    _reconcile_human_review_queue,
    _sign_preview_ticket,
    _verify_preview_ticket,
    _rewrite_preview_location,
    _get_job_workspace_or_404,
    _find_related_symphony_issue,
    _human_review_config_path,
    _load_human_review_config,
    _human_review_store_path,
    _candidate_human_review_store_workdirs,
    _load_human_review_queue,
    _load_human_review_queue_for_workdir,
    _load_aggregated_human_review_queue,
    _find_human_review_store_for_update,
    _save_human_review_queue,
    _save_human_review_queue_for_workdir,
    _canonicalize_json,
    _tool_args_match,
    _has_approved_review_for_call,
    _find_matching_reviews_for_workspace,
    _latest_review_lineage_for_workspace,
    _extract_mcp_payload_dict,
    _build_submit_input_from_job_and_issue,
    _snapshot_job_state,
    _build_resubmission_input,
    _normalize_lifecycle_status,
    _append_timeline_event,
    _build_job_timeline,
    _get_job_workspace_or_404,
)


from .message_resources import (
    _MAX_MESSAGE_FILE_REFS as _MAX_MESSAGE_FILE_REFS,
    _build_message_resource_context as _build_message_resource_context,
    _count_pdfs_in_directory as _count_pdfs_in_directory,
    _count_pdfs_in_zip as _count_pdfs_in_zip,
    _extract_attachment_paths as _extract_attachment_paths,
    _extract_structured_file_ref_paths as _extract_structured_file_ref_paths,
    _inspect_local_paper_inputs as _inspect_local_paper_inputs,
    _normalize_attachment_path as _normalize_attachment_path,
    _resolve_existing_attachment_path as _resolve_existing_attachment_path,
    _resolve_workspace_file_reference as _resolve_workspace_file_reference,
    _stage_attachment_for_workspace as _stage_attachment_for_workspace,
)

from .issue_records import (
    _extract_issue_session_candidates as _extract_issue_session_candidates,
    _extract_submit_input_from_description as _extract_submit_input_from_description,
    _is_terminal_issue_state as _is_terminal_issue_state,
    _issue_lookup_key as _issue_lookup_key,
    _issue_matches_session as _issue_matches_session,
    _issue_matches_workspace as _issue_matches_workspace,
    _issue_timestamp_value as _issue_timestamp_value,
    _issue_workspace_id as _issue_workspace_id,
    _normalize_issue_state_text as _normalize_issue_state_text,
    _path_is_same_or_descendant as _path_is_same_or_descendant,
    _same_resolved_workdir as _same_resolved_workdir,
)

from .linear_client import (
    _linear_api_key as _linear_api_key,
    _linear_create_issue as _linear_create_issue,
    _linear_graphql as _linear_graphql,
    _linear_list_projects as _linear_list_projects,
    _linear_project_info as _linear_project_info,
    _linear_project_slug as _linear_project_slug,
)

from .session_presenter import (
    _subagent_integration_message,
    _extract_subagent_display_name,
    _extract_subagent_details,

    _public_frontend_message_metadata as _public_frontend_message_metadata,
    _serialize_frontend_tool_call as _serialize_frontend_tool_call,
)

from .job_workspace import (
    _artifact_entry_from_raw_path as _artifact_entry_from_raw_path,
    _artifact_source_root as _artifact_source_root,
    _build_workspace_artifact_source as _build_workspace_artifact_source,
    _build_workspace_preview_entries as _build_workspace_preview_entries,
    _extract_first_json_object as _extract_first_json_object,
    _extract_original_tool_args_from_description as _extract_original_tool_args_from_description,
    _extract_result_output_paths_from_payload as _extract_result_output_paths_from_payload,
    _extract_submit_job_input_from_description as _extract_submit_job_input_from_description,
    _file_mtime_iso as _file_mtime_iso,
    _iter_result_output_path_values as _iter_result_output_path_values,
    _normalize_preview_ports_local as _normalize_preview_ports_local,
    _path_within_root as _path_within_root,
    _preview_base_urls as _preview_base_urls,
    _preview_cookie_path as _preview_cookie_path,
    _preview_entry_kind_for_path as _preview_entry_kind_for_path,
    _preview_ticket_secret as _preview_ticket_secret,
    _proxy_remote_artifact_json as _proxy_remote_artifact_json,
    _read_json_file as _read_json_file,
    _relative_artifact_path as _relative_artifact_path,
    _resolve_workspace_artifact_entries as _resolve_workspace_artifact_entries,
    _resolve_workspace_artifacts as _resolve_workspace_artifacts,
    _result_output_artifact_entries as _result_output_artifact_entries,
    _utc_now_iso as _utc_now_iso,
)


def _load_runtime_env_file() -> None:
    workdir = os.environ.get("TCODE_WORKDIR", os.getcwd())
    env_file = os.environ.get("TCODE_ENV_FILE") or str(
        Path(workdir) / "config" / "symphony_route.env"
    )
    path = Path(env_file)
    if not path.is_file():
        return
    try:
        load_process_env_file(path)
    except Exception:
        pass


_load_runtime_env_file()


app = FastAPI(title="Tcode Agent API")

@app.exception_handler(StateLockBusy)
async def state_lock_busy_response(request: Request, exc: StateLockBusy):
    return JSONResponse(status_code=409, content={"code": exc.code, "detail": str(exc)})


app.include_router(plugins_router)
app.include_router(swarm_router)
app.include_router(mcp_config_router)
app.include_router(websearch_config_router)
app.include_router(research_router)
app.include_router(scheduled_tasks_router)
app.include_router(protein_runs_router)
app.include_router(job_runtime_router)
app.include_router(config_runtime_router)


def _is_public_auth_path(path: str) -> bool:
    normalized = str(path or "").strip() or "/"
    if normalized in {"/", "/desktop.html", "/molstar.html", "/favicon.ico"}:
        return True
    if normalized.startswith("/assets/"):
        return True
    if normalized.startswith("/api/auth/"):
        return True
    if normalized in {"/api/health", "/health"}:
        return True
    return False


def _is_inactive_tenant_management_path(path: str) -> bool:
    normalized = str(path or "").strip()
    return normalized.startswith("/api/tenants") or normalized.startswith("/api/auth/")


def _request_has_valid_wiki_service_token(request: Request) -> bool:
    required = str(os.environ.get("TCODE_WIKI_SERVICE_TOKEN") or "").strip()
    if len(required) < 24:
        return False
    provided = str(request.headers.get("x-wiki-service-token") or "").strip()
    if not provided:
        authorization = str(request.headers.get("authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
    return bool(provided) and hmac.compare_digest(provided, required)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    from ..runtime.workspace_registry import bind_plugin_workspace
    bind_plugin_workspace(ServerState.get_workdir())
    public_agent_api = str(request.url.path or "").startswith(
        ("/api/ag-ui/", "/api/mcp-apps/")
    )
    authorization = str(request.headers.get("authorization") or "").strip()
    if public_agent_api and authorization.lower().startswith("bearer "):
        from ..agui.auth import APIKeyStore, bearer_token

        principal = APIKeyStore().authenticate(bearer_token(authorization))
        if principal is None:
            return Response(
                content=json.dumps(
                    {"code": "invalid_api_key", "message": "Invalid or expired API key"}
                ),
                status_code=401,
                media_type="application/json",
            )
        request.state.current_user = principal.user
        request.state.agui_api_key = principal
    if not auth_required() or _is_public_auth_path(request.url.path):
        return await call_next(request)
    if str(request.url.path or "").startswith("/api/tracker/") and _request_has_valid_tracker_api_token(request):
        return await call_next(request)
    if (
        str(request.url.path or "").startswith("/api/paper-wiki/service/")
        and _request_has_valid_wiki_service_token(request)
    ):
        request.state.wiki_service_authenticated = True
        return await call_next(request)
    user = current_user_from_request(request)
    if user is None:
        return Response(
            content=json.dumps({"detail": "Authentication required"}),
            status_code=401,
            media_type="application/json",
        )
    request.state.current_user = user
    context = _workspace_context_from_request(request)
    bind_plugin_workspace(_resolve_user_workdir_path(context.workspace_id, context))
    if user.tenant_status != "active" and not _is_inactive_tenant_management_path(
        request.url.path
    ):
        return Response(
            content=json.dumps(
                {
                    "detail": "Current tenant is inactive; restore it or switch tenants",
                    "code": "tenant_inactive",
                }
            ),
            status_code=423,
            media_type="application/json",
        )
    return await call_next(request)

_PREVIEW_TICKET_COOKIE = "tcode_job_preview_ticket"
_ASYNC_ISSUE_AUTO_RESUME_TASK: Optional[asyncio.Task] = None
_ASYNC_ISSUE_AUTO_RESUME_LOCK: Optional[asyncio.Lock] = None
_ASYNC_WATCHER_TASK: Optional[asyncio.Task] = None
_PREVIEW_TICKET_TTL_SECONDS = max(
    60, int(os.environ.get("TCODE_JOB_PREVIEW_TTL_SECONDS", "600"))
)
_GUIDANCE_QUEUE_METADATA_KEY = "pending_guidance_queue"
_TEXT_PREVIEW_SUFFIXES = {
    ".txt",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".md",
    ".fa",
    ".fasta",
    ".pdb",
    ".cif",
    ".sdf",
    ".xml",
}
_HTML_PREVIEW_SUFFIXES = {".html", ".htm"}
_FIXED_TEAM_SESSION_PREFIXES: dict[str, tuple[str, ...]] = {
    "paper": ("research-",),
    "protein": ("protein-",),
}
_PAPER_FIXED_WORKER_NAMES = {
    "paper_generalist",
    "paper_library_manager",
    "paper_library_retriever",
    "paper_search",
    "paper_reader",
    "paper_synthesis",
    "paper_review",
}
_STRUCTURE_SUFFIXES = {".pdb", ".cif", ".sdf"}
_DEFAULT_USER_WORKDIRS = workdir_utils.DEFAULT_USER_WORKDIRS


def _normalize_guidance_items(raw: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return normalized
    for item in raw:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", "") or "").strip()
        if not message:
            continue
        output_format = item.get("output_format")
        outcome_contract = item.get("outcome_contract")
        contract_revision = item.get("contract_revision")
        normalized.append(
            {
                "id": str(item.get("id") or ""),
                "message": message,
                "output_format": output_format if isinstance(output_format, dict) else None,
                "outcome_contract": outcome_contract
                if isinstance(outcome_contract, dict)
                else None,
                "contract_revision": contract_revision
                if isinstance(contract_revision, dict)
                else None,
                "interrupt": bool(item.get("interrupt", False)),
                "source": str(item.get("source", "user") or "user"),
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return normalized


def _prepare_outcome_revision_confirmation(
    agent: Any,
    contract_revision: Optional[dict[str, Any]],
    outcome_contract: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Ask for clarification in-band when semantic revision intent is uncertain."""

    revision = (
        dict(contract_revision or {})
        if isinstance(contract_revision, dict)
        else {}
    )
    if not bool(revision.get("needs_user_confirmation")) or outcome_contract is not None:
        return outcome_contract
    note = Message(
        role="system",
        content=(
            "[OUTCOME REVISION CONFIRMATION]\n"
            "The independent Revision Agent could not confidently determine whether "
            "the latest user message changes, cancels, or only discusses the running "
            "task. Do not change or cancel the active task yet. Respond with one concise "
            "clarification question that lets the user confirm the intended action."
        ),
        id=str(uuid.uuid4()),
    )
    setattr(note, "_internal", True)
    setattr(note, "_hide_from_frontend", True)
    agent.messages.append(note)
    return {
        "schema_version": "tcode.outcome/v2",
        "required_proof_level": "P1",
        "semantic_validation": "always",
        "max_repair_attempts": 0,
        "success_criteria": [
            {
                "description": (
                    "The response asks one concise clarification question and does not "
                    "claim that the active task or Contract was changed or cancelled."
                ),
                "verifier_refs": ["semantic"],
            }
        ],
        "metadata": {"kind": "outcome_revision_confirmation"},
    }


def _guidance_owner_scope_from_session(session: Session) -> OwnerScope:
    return OwnerScope(
        tenant_id=str(getattr(session, "tenant_id", "") or "").strip(),
        user_id=str(getattr(session, "user_id", "") or "").strip(),
        workspace_id=str(getattr(session, "workspace_id", "") or "").strip(),
    )


async def _clear_session_guidance(session: Session) -> None:
    await _migrate_session_guidance_v2(session)
    await asyncio.to_thread(
        GuidanceSQLiteStore.cancel_all_for_session,
        session_id=session.id,
        owner=_guidance_owner_scope_from_session(session),
        workdir=_guidance_store_workdir(),
    )


def _guidance_store_workdir() -> str:
    """Return the authority directory for the shared session/guidance database.

    A session's ``workdir`` is its execution workspace.  Session and Guidance v2
    rows, however, share the server-level ``sessions.sqlite`` store rooted at
    ``TCODE_WORKDIR``.  Keeping the two paths distinct is required for managed
    workspaces where every session executes below ``workdirs/...``.
    """

    return str(ServerState.get_workdir() or os.getcwd()).strip() or os.getcwd()


def _bind_guidance_v2_to_agent(agent: Agent, session: Session) -> None:
    from .guidance_runtime import guidance_runtime

    owner_scope = _guidance_owner_scope_from_session(session)
    workdir = _guidance_store_workdir()
    owner_id = str(getattr(agent, "_guidance_owner_id", "") or "").strip()
    if not owner_id:
        owner_id = f"turn-owner:{uuid.uuid4()}"
    guidance_runtime.ensure_workdir(workdir)
    agent.set_guidance_port(
        guidance_runtime.controller(workdir=workdir, owner=owner_scope),
        owner_id=owner_id,
    )


async def _migrate_session_guidance_v2(session: Session) -> int:
    """Idempotently import the legacy metadata queue into SQLite Guidance v2."""

    metadata = dict(getattr(session, "metadata", {}) or {})
    legacy_items = metadata.get(_GUIDANCE_QUEUE_METADATA_KEY) or []
    queue = _normalize_guidance_items(legacy_items)
    if not queue:
        return 0
    owner_scope = _guidance_owner_scope_from_session(session)
    workdir = _guidance_store_workdir()
    imported = 0
    for index, item in enumerate(queue):
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            raw_id = hashlib.sha256(
                (
                    str(index)
                    + json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                ).encode("utf-8")
            ).hexdigest()[:24]
        try:
            _, duplicate = await asyncio.to_thread(
                GuidanceSQLiteStore.accept,
                session_id=session.id,
                owner=owner_scope,
                client_request_id=f"legacy:{session.id}:{raw_id}",
                target_turn_id="",
                mode=GuidanceMode.QUEUE,
                message=str(item.get("message") or ""),
                file_refs=[],
                output_format=(
                    dict(item.get("output_format"))
                    if isinstance(item.get("output_format"), dict)
                    else None
                ),
                outcome_contract=(
                    dict(item.get("outcome_contract"))
                    if isinstance(item.get("outcome_contract"), dict)
                    else None
                ),
                contract_revision=(
                    dict(item.get("contract_revision"))
                    if isinstance(item.get("contract_revision"), dict)
                    else None
                ),
                apply_policy="next_safe_point",
                descendant_policy="preserve",
                supersedes_guidance_id="",
                received_phase="legacy_queue",
                initial_effect="legacy_queue_migrated",
                workdir=workdir,
            )
            if not duplicate:
                imported += 1
        except ValueError:
            continue
    def finish_migration(current: Session) -> None:
        metadata = dict(current.metadata or {})
        metadata[_GUIDANCE_QUEUE_METADATA_KEY] = [
            item for item in (metadata.get(_GUIDANCE_QUEUE_METADATA_KEY) or [])
            if item not in legacy_items
        ]
        current.metadata = metadata

    await SessionManager.mutate(
        session.id, finish_migration, **_session_owner_kwargs_from_session(session)
    )
    return imported


def _shared_tracker_db_path_text(workdir: Optional[str] = None) -> str:
    resolved = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    return SharedTracker.db_path(resolved).as_posix()


def _tracker_api_token() -> str:
    return read_tracker_token(os.environ)


def _request_has_valid_tracker_api_token(request: Request) -> bool:
    required = _tracker_api_token()
    if not required:
        return False
    provided = str(request.headers.get("X-Tracker-Token", "") or "").strip()
    return bool(provided) and hmac.compare_digest(provided, required)


def _require_tracker_api_auth(request: Request, *, allow_user: bool = False) -> None:
    if not auth_required():
        if (not allow_user and os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL")
                and not _request_has_valid_tracker_api_token(request)):
            raise HTTPException(401, "内置异步队列只接受受信任的后台请求")
        return
    if _request_has_valid_tracker_api_token(request):
        return
    if allow_user and current_user_from_request(request) is not None:
        return
    raise HTTPException(401, "Unauthorized tracker request")


async def _delete_child_sessions(
    parent_session_id: str,
    *,
    owner_kwargs: Optional[dict[str, str]] = None,
    include_side_chats: bool = False,
) -> list[str]:
    owner = dict(owner_kwargs or {})
    descendants = await _list_descendant_sessions_recursive(
        parent_session_id,
        owner_kwargs=owner,
        include_side_chats=include_side_chats,
    )
    deleted_session_ids: list[str] = []
    for sub in reversed(descendants):
        deleted_session_ids.append(sub.id)
        from .session_lifecycle import _delete_session_tree_background
        await _delete_session_tree_background(sub.id, owner_kwargs=_session_owner_kwargs_from_session(sub))
    return deleted_session_ids


async def _terminate_session_processes(
    session_id: str,
    *,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    summary = await ProcessRuntime.terminate_for_sessions(
        {str(session_id or "").strip()},
        owner=dict(owner_kwargs or {}),
    )
    failures = dict(summary.get("failed") or {})
    if failures:
        detail = "; ".join(
            f"{process_id}: {error}"
            for process_id, error in sorted(failures.items())
        )
        raise RuntimeError(
            f"failed to terminate processes for session {session_id}: {detail}"
        )
    summary["purged"] = await ProcessRuntime.purge_for_sessions(
        {str(session_id or "").strip()},
        owner=dict(owner_kwargs or {}),
    )
    return summary


async def _list_descendant_sessions_recursive(
    parent_session_id: str,
    *,
    owner_kwargs: Optional[dict[str, str]] = None,
    _visited: Optional[set[str]] = None,
    include_side_chats: bool = False,
) -> list[Session]:
    owner = dict(owner_kwargs or {})
    visited = _visited if _visited is not None else set()
    normalized_parent_id = str(parent_session_id or "").strip()
    if not normalized_parent_id or normalized_parent_id in visited:
        return []
    visited.add(normalized_parent_id)
    direct_children = await SessionManager.list(parent_id=parent_session_id, **owner)
    all_descendants: list[Session] = []
    for child in direct_children:
        if not include_side_chats and (child.metadata or {}).get("side_question"):
            continue
        child_id = str(getattr(child, "id", "") or "").strip()
        if not child_id or child_id in visited:
            continue
        all_descendants.append(child)
        all_descendants.extend(
            await _list_descendant_sessions_recursive(
                child_id,
                owner_kwargs=owner,
                _visited=visited,
                include_side_chats=include_side_chats,
            )
        )
    return all_descendants


async def _cancel_descendant_sessions(
    parent_session_id: str,
    *,
    clear_guidance: bool = True,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> list[str]:
    owner = dict(owner_kwargs or {})
    descendants = await _list_descendant_sessions_recursive(
        parent_session_id,
        owner_kwargs=owner,
        include_side_chats=False,
    )
    cancelled_ids: list[str] = []
    for sub in descendants:
        await ServerState.cancel_session(sub.id, clear_guidance=clear_guidance)
        cancelled_ids.append(sub.id)
    return cancelled_ids


async def _cancel_runtime_subtree_for_interrupt_guidance(
    session_id: str,
    *,
    clear_guidance: bool = True,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Cancel live work below a guided session before replanning that session.

    This covers both harness-owned child turns and coordinator-owned background
    workers. The target session itself is stopped by the caller after the new
    guidance has been durably queued.
    """
    owner = dict(owner_kwargs or {})
    descendants = await _list_descendant_sessions_recursive(
        session_id,
        owner_kwargs=owner,
        include_side_chats=False,
    )
    descendant_ids = [str(sub.id) for sub in descendants if str(sub.id or "").strip()]
    cancelled_worker_session_ids: list[str] = []
    cancelled_team_task_details: dict[str, dict[str, Any]] = {}

    for scoped_session_id in [session_id, *descendant_ids]:
        if cancel_worker_task(scoped_session_id):
            cancelled_worker_session_ids.append(scoped_session_id)
        cancelled = await cancel_team_background_tasks(scoped_session_id)
        if cancelled.get("cancelled_worker_session_ids") or cancelled.get(
            "cancelled_auxiliary_tasks"
        ):
            cancelled_team_task_details[scoped_session_id] = cancelled
            cancelled_worker_session_ids.extend(
                str(item)
                for item in cancelled.get("cancelled_worker_session_ids", [])
                if str(item or "").strip()
            )

    for sub in descendants:
        await ServerState.cancel_session(sub.id, clear_guidance=clear_guidance)

    return {
        "cancelled_descendant_session_ids": descendant_ids,
        "cancelled_worker_session_ids": list(
            dict.fromkeys(cancelled_worker_session_ids)
        ),
        "cancelled_team_task_details": cancelled_team_task_details,
    }


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _frontend_outcome_validation_enabled() -> bool:
    return _env_truthy("TCODE_EXPOSE_OUTCOME_VALIDATION", False) or _env_truthy(
        "TCODE_PROOF_PACK_ENABLED", False
    )


def _public_outcome_validation(value: Any) -> dict[str, Any]:
    """Return the intentionally small verification projection used by the UI."""

    return public_outcome_projection(value)


def _is_internal_frontend_message(message: Any) -> bool:
    if bool(getattr(message, "_hide_from_frontend", False)) or bool(
        getattr(message, "_internal", False)
    ):
        return True
    metadata = getattr(message, "metadata", {})
    if isinstance(metadata, dict) and bool(metadata.get("internal")):
        return True
    content = str(getattr(message, "content", "") or "").lstrip()
    return content.startswith(
        ("[OUTCOME VALIDATION REPAIR]", "[OUTCOME REVISION CONFIRMATION]")
    )


_LITERATURE_INTENT_PATTERNS = [
    r"文献",
    r"综述",
    r"参考文献",
    r"研究进展",
    r"最新工作",
    r"论文检索",
    r"论文调研",
    r"paper\s+survey",
    r"literature\s+review",
    r"related\s+work",
    r"research\s+progress",
    r"state\s+of\s+the\s+art",
    r"papers?\s+about",
]

_PAPER_IDENTIFIER_PATTERNS = [
    r"\bpmid\s*[:：]?\s*\d{5,}\b",
    r"\bpmcid\s*[:：]?\s*(?:pmc)?\d+\b",
    r"\bdoi\s*[:：]?\s*10\.\S+",
    r"pubmed\.ncbi\.nlm\.nih\.gov",
    r"europepmc\.org",
    r"pmc\.ncbi\.nlm\.nih\.gov",
]

_SINGLE_PAPER_DOCUMENT_PATTERNS = [
    r"目标论文",
    r"论文",
    r"文献",
    r"\bpaper\b",
    r"\barticle\b",
    r"\bjournal\b",
    r"\bfull\s*text\b",
]

_SINGLE_PAPER_ACTION_PATTERNS = [
    r"抽取",
    r"提取",
    r"解析",
    r"读取",
    r"证据",
    r"原文",
    r"表格",
    r"\bextract(?:ion)?\b",
    r"\bread(?:ing)?\b",
    r"\bparse\b",
    r"\bevidence\b",
    r"\btable\s*\d+\b",
    r"\bfigure\s*\d+\b",
    r"\benzyme\b",
    r"\bkinetic(?:s)?\b",
    r"\bbenchmark\b",
]

_FULL_RESEARCH_INTENT_PATTERNS = [
    r"综述",
    r"研究空白",
    r"研究进展",
    r"共识",
    r"分歧",
    r"recommended[_\s-]*next[_\s-]*queries",
    r"theme[_\s-]*summary",
    r"systematic\s+review",
    r"literature\s+review",
    r"related\s+work",
    r"state\s+of\s+the\s+art",
]

_PAPER_AUTO_ROUTE_FALLBACK_AGENTS = {
    "",
    "manager",
    "build",
    "general",
    "explore",
}

_PAPER_TEAM_EXPLICIT_PATTERNS = [
    r"paper\s*auto",
    r"paper\s*team",
    r"paper\s*workflow",
    r"paper\s*coordinator",
    r"paper\s*团队",
    r"paper\s*研究",
    r"文献自动协作",
    r"文献团队",
    r"论文团队",
    r"存入库",
    r"入库",
    r"导入\s*pdf",
    r"导入\s*文献",
    r"导入\s*论文",
    r"团队编排",
    r"进入团队",
    r"交给团队",
    r"协同调研",
]

_PAPER_TEAM_COORDINATION_HINTS = [
    "多篇",
    "批量",
    "筛选",
    "候选",
    "归纳",
    "对比",
    "并行",
    "协调",
    "协作",
    "review",
    "survey",
    "synthesis",
    "screening",
    "compare",
]

_PAPER_LIBRARY_META_QUERY_PATTERNS = [
    r"文献库",
    r"本地文献库",
    r"本地库",
    r"paper[_\s-]*library",
    r"library",
]

_PAPER_LIBRARY_COUNT_PATTERNS = [
    r"几篇",
    r"多少篇",
    r"数量",
    r"统计",
    r"count",
    r"stats?",
]

_PAPER_LIBRARY_LIST_PATTERNS = [
    r"所有文献",
    r"全部文献",
    r"文献标题",
    r"论文标题",
    r"文献列表",
    r"文献清单",
    r"文献信息",
    r"标题等信息",
    r"列出.*文献",
    r"列出.*标题",
    r"doc[_\s-]*id",
    r"doi",
    r"pdf\s*path",
]

_PAPER_TEAM_FOLLOW_UP_HINTS = (
    "继续",
    "接着",
    "上面",
    "以上",
    "前面",
    "刚才",
    "刚刚",
    "这些",
    "它们",
    "整理",
    "导出",
    "返回",
    "列表",
    "清单",
    "详情",
    "明细",
    "markdown",
    "md",
    "表格",
)

_NON_PAPER_FOLLOW_UP_BLOCKERS = (
    "代码",
    "bug",
    "报错",
    "函数",
    "文件",
    "python",
    "javascript",
    "typescript",
    "测试",
    "编译",
    "仓库",
    "git",
    "sqlite",
    "session",
    "tool",
    "mcp",
    "agent",
)

_DELEGATED_TEAM_METADATA_KEY = "delegated_team_agent"
_DELEGATED_AGENT_METADATA_KEY = "delegated_agent"
_DELEGATION_ID_METADATA_KEY = "delegation_id"
_LAST_DELEGATED_TEAM_METADATA_KEY = "last_delegated_team_agent"
_DELEGATED_SESSION_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _delegated_session_lock(key: str) -> asyncio.Lock:
    normalized = str(key or "").strip()
    lock = _DELEGATED_SESSION_LOCKS.get(normalized)
    if lock is None:
        lock = asyncio.Lock()
        _DELEGATED_SESSION_LOCKS[normalized] = lock
    return lock
_PROTEIN_TASK_KEYWORDS = [
    "protein",
    "enzyme",
    "ligand",
    "pdb",
    "sdf",
    "sequence",
    "mutation",
    "binding",
    "affinity",
    "docking",
    "fold",
    "protein design",
    "ligandmpnn",
    "boltz",
    "rfdiffusion",
    "蛋白",
    "酶",
    "序列",
    "突变",
    "结构",
    "对接",
    "结合",
    "亲和力",
    "折叠",
]


async def _cleanup_fixed_team_state(session_id: str) -> dict[str, Any]:
    cancelled_team_tasks = await cancel_team_background_tasks(session_id)
    deleted_child_sessions = await _delete_child_sessions(session_id)
    removed_workers = await WorkerRegistry.delete_for_team(session_id)
    try:
        TeamChannel.get_or_create(session_id, ServerState.get_workdir()).clear()
    except Exception:
        pass
    return {
        "cancelled_team_tasks": cancelled_team_tasks,
        "deleted_child_sessions": deleted_child_sessions,
        "removed_workers": removed_workers,
    }


def _fixed_team_key_from_session_prefix(session_id: str) -> str:
    normalized = str(session_id or "").strip().lower()
    for entry in TeamCatalog.list(available_only=False):
        for prefix in getattr(entry, "session_prefixes", ()) or ():
            if normalized.startswith(str(prefix or "").strip().lower()):
                return str(entry.key or "").strip().lower()
    for team_key, prefixes in _FIXED_TEAM_SESSION_PREFIXES.items():
        if any(normalized.startswith(prefix.lower()) for prefix in prefixes):
            return team_key
    return ""


def _fixed_team_worker_names(team_key: str) -> set[str]:
    normalized = str(team_key or "").strip().lower()
    entry = TeamCatalog.get_by_key(normalized, available_only=False)
    if entry is not None:
        names = {
            str(name or "").strip().lower()
            for name in (
                list(getattr(entry, "worker_names", ()) or ())
                + list(getattr(entry, "resident_worker_names", ()) or ())
            )
            if str(name or "").strip()
        }
        if names:
            return names
    if normalized == "paper":
        return set(_PAPER_FIXED_WORKER_NAMES)
    if normalized == "protein":
        try:
            from ..team.protein_team_config import load_protein_team_config

            config = load_protein_team_config()
            return {
                str(item.get("name", "")).strip().lower()
                for item in (config.get("workers") or [])
                if str(item.get("name", "")).strip()
            }
        except Exception:
            return set()
    return set()


async def _active_fixed_team_key_for_session(session_id: str) -> str:
    runtime = ServerState.sessions.get(session_id)
    runtime_agent = ""
    if isinstance(runtime, dict):
        runtime_agent = str(runtime.get("agent_name", "") or "").strip()
    persisted_agent = ""
    if not runtime_agent:
        persisted = await SessionManager.get(session_id)
        persisted_agent = str(getattr(persisted, "agent_name", "") or "").strip()
    entry = TeamCatalog.get_by_agent_name(runtime_agent or persisted_agent, available_only=False)
    return str(entry.key or "").strip().lower() if entry else ""


def _session_matches_active_fixed_team(
    session_id: str,
    *,
    team_key: str,
    worker_name: str = "",
    agent_name: str = "",
) -> bool:
    normalized_team_key = str(team_key or "").strip().lower()
    if not normalized_team_key:
        return True

    normalized_session_id = str(session_id or "").strip().lower()
    normalized_worker_name = str(worker_name or "").strip().lower()
    normalized_agent_name = str(agent_name or "").strip().lower()
    agent_team_entry = TeamCatalog.get_by_agent_name(normalized_agent_name, available_only=False)
    if agent_team_entry and str(agent_team_entry.key or "").strip().lower() != normalized_team_key:
        return False

    fixed_worker_names = _fixed_team_worker_names(normalized_team_key)
    if normalized_worker_name:
        return normalized_worker_name in fixed_worker_names

    if normalized_agent_name.startswith("worker_"):
        return normalized_agent_name.removeprefix("worker_") in fixed_worker_names

    inferred_team_key = _fixed_team_key_from_session_prefix(normalized_session_id)
    if inferred_team_key:
        return inferred_team_key == normalized_team_key

    return True


def _matches_any_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _looks_like_single_paper_task(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if _matches_any_pattern(text, _PAPER_IDENTIFIER_PATTERNS):
        return True

    has_paper_document = _matches_any_pattern(text, _SINGLE_PAPER_DOCUMENT_PATTERNS)
    has_pdf = bool(re.search(r"\bpdf\b", text, re.IGNORECASE))
    has_action = _matches_any_pattern(text, _SINGLE_PAPER_ACTION_PATTERNS)
    has_scientific_or_table_context = _matches_any_pattern(
        text,
        [
            r"\btable\s*\d+\b",
            r"\bfigure\s*\d+\b",
            r"\benzyme\b",
            r"\bkinetic(?:s)?\b",
            r"\bbenchmark\b",
            r"酶",
            r"动力学",
        ],
    )
    return (has_paper_document and has_action) or (
        has_pdf and has_action and has_scientific_or_table_context
    )


def _looks_like_literature_task(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return _matches_any_pattern(text, _LITERATURE_INTENT_PATTERNS) or _looks_like_single_paper_task(text)


def _looks_like_full_paper_research_task(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return _matches_any_pattern(text, _FULL_RESEARCH_INTENT_PATTERNS)


def _looks_like_paper_team_orchestration_task(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if _matches_any_pattern(text, _PAPER_TEAM_EXPLICIT_PATTERNS):
        return True
    if _looks_like_single_paper_task(text):
        return True
    # Route manager to paper team for paper-library meta queries (count/stats/listing)
    # so it won't repeatedly call restricted paper tools under plain manager.
    if _looks_like_paper_library_meta_query(text):
        return True
    if _looks_like_full_paper_research_task(text):
        return True
    return _looks_like_literature_task(text) and any(
        hint in text for hint in _PAPER_TEAM_COORDINATION_HINTS
    )


def _looks_like_paper_library_meta_query(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    has_library_context = any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _PAPER_LIBRARY_META_QUERY_PATTERNS
    )
    if not has_library_context and _looks_like_literature_task(text):
        has_library_context = True
    if not has_library_context:
        return False
    wants_count = any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _PAPER_LIBRARY_COUNT_PATTERNS
    )
    wants_listing = any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _PAPER_LIBRARY_LIST_PATTERNS
    )
    return wants_count or wants_listing


def _looks_like_paper_follow_up_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if _looks_like_paper_library_meta_query(text) or _looks_like_literature_task(text):
        return True
    has_follow_up_hint = any(hint in text for hint in _PAPER_TEAM_FOLLOW_UP_HINTS)
    if not has_follow_up_hint:
        return False
    if any(blocker in text for blocker in _NON_PAPER_FOLLOW_UP_BLOCKERS):
        return False
    return True


# Backward-compatible name for integrations that imported the pre-contract helper.
from .delegation_routing import (  # noqa: E402 - compatibility exports
    build_permission_delegation_message as _build_permission_delegation_message,
    pick_permission_denied_delegate as _pick_permission_denied_delegate,
)

_pick_permission_denied_team_delegate = _pick_permission_denied_delegate


def _looks_like_protein_team_task(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    decision = detect_workflow_route(text)
    if decision.should_start:
        template = WorkflowTemplateRegistry.get(decision.template_key)
        # A newly registered non-protein workflow must not be sent to a protein
        # team by this legacy team-specific caller.
        return bool(template and template.domain in {"protein", "protein-design", "protein_design"})
    signals = dict(decision.signals or {})
    if int(signals.get("meta_hits", 0) or 0) and not (
        int(signals.get("receptor_candidates", 0) or 0)
        or int(signals.get("ligand_candidates", 0) or 0)
    ):
        return False
    return any(keyword in text for keyword in _PROTEIN_TASK_KEYWORDS)


def _message_input_images(
    resource_context: Optional[dict[str, Any]],
    *,
    workdir: str,
    maximum: int = 2,
) -> list[dict[str, Any]]:
    """Validate images uploaded in this request and bind them to one model turn."""
    context = dict(resource_context or {}) if isinstance(resource_context, dict) else {}
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    candidates: list[dict[str, Any]] = []
    for item in list(context.get("uploaded_files") or []):
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip()
        if Path(raw_path).suffix.lower() not in image_suffixes:
            continue
        path = _resolve_existing_attachment_path(raw_path, workdir=workdir)
        if path is None or not path.is_file():
            raise ValueError(f"上传图片不存在或不可访问: {raw_path}")
        try:
            path.resolve(strict=True).relative_to(Path(workdir).resolve(strict=True))
            inspected = inspect_image_file(path)
        except Exception as exc:
            raise ValueError(f"上传文件不是可用图片: {path.name}") from exc
        mime = str(inspected.get("mime_type") or "")
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise ValueError(f"暂不支持该图片格式: {path.name} ({mime or 'unknown'})")
        candidates.append(
            {
                "path": str(inspected["path"]),
                "relative_path": str(item.get("relative_path") or ""),
                "name": path.name,
                "mime_type": mime,
                "size": int(inspected["size"]),
                "sha256": str(inspected["sha256"]),
                "detail": "auto",
                "source": "user_upload",
            }
        )
    if len(candidates) > maximum:
        raise ValueError(f"每轮最多可直接发送 {maximum} 张图片给视觉模型。")
    return candidates


def _model_config_supports_images(model_cfg: Any) -> Optional[bool]:
    from ..vision import model_config_supports_images

    return model_config_supports_images(model_cfg)


def _merge_resource_context(
    existing: Optional[dict[str, Any]],
    incoming: Optional[dict[str, Any]],
) -> dict[str, Any]:
    base = dict(existing or {}) if isinstance(existing, dict) else {}
    update = dict(incoming or {}) if isinstance(incoming, dict) else {}
    if not update:
        return base

    merged: dict[str, Any] = dict(base)

    def _merge_uploaded_files(
        current: list[dict[str, Any]],
        new_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for item in [*(current or []), *(new_items or [])]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("path") or "").strip()
            if not key:
                continue
            deduped[key] = dict(item)
        return list(deduped.values())

    if "uploaded_files" in update:
        merged["uploaded_files"] = _merge_uploaded_files(
            list(base.get("uploaded_files") or []),
            list(update.get("uploaded_files") or []),
        )

    if "paper_inputs" in update and isinstance(update.get("paper_inputs"), dict):
        current_inputs = dict(base.get("paper_inputs") or {}) if isinstance(base.get("paper_inputs"), dict) else {}
        next_inputs = dict(update.get("paper_inputs") or {})
        merged_inputs = dict(current_inputs)
        merged_inputs["input_count"] = int(next_inputs.get("input_count", current_inputs.get("input_count", 0)) or 0)
        merged_inputs["pdf_count"] = int(next_inputs.get("pdf_count", current_inputs.get("pdf_count", 0)) or 0)
        merged_inputs["requires_ingest"] = bool(next_inputs.get("requires_ingest", current_inputs.get("requires_ingest", False)))
        current_list = list(current_inputs.get("inputs") or [])
        next_list = list(next_inputs.get("inputs") or [])
        deduped_inputs: dict[str, dict[str, Any]] = {}
        for item in [*current_list, *next_list]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("path") or "").strip()
            if not key:
                continue
            deduped_inputs[key] = dict(item)
        merged_inputs["inputs"] = list(deduped_inputs.values())
        merged["paper_inputs"] = merged_inputs

    if "external_mounts" in update and isinstance(update.get("external_mounts"), list):
        deduped_mounts: dict[str, dict[str, Any]] = {}
        for item in [*list(base.get("external_mounts") or []), *list(update.get("external_mounts") or [])]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("container_path") or item.get("source_path") or "").strip()
            if not key:
                continue
            deduped_mounts[key] = dict(item)
        merged["external_mounts"] = list(deduped_mounts.values())

    for key, value in update.items():
        if key in {"uploaded_files", "paper_inputs", "external_mounts"}:
            continue
        merged[key] = value

    return merged


def _resolve_paper_workflow_agent(
    *,
    requested_agent_name: str,
    requested_execution_profile: str,
    message: str,
) -> tuple[str, dict[str, Any]]:
    requested = str(requested_agent_name or "").strip()
    
    # Apply paper team aliases before catalog lookup
    _paper_team_aliases = {
        "paper_auto": "paper_auto_coordinator",
        "paper_team": "paper_auto_coordinator",
        "paper_workflow": "paper_auto_coordinator",
    }
    if requested in _paper_team_aliases:
        requested = _paper_team_aliases[requested]
    
    paper_team = TeamCatalog.get_by_key("paper", available_only=True)
    requested_team_entry = TeamCatalog.get_by_agent_name(requested, available_only=True)
    inspection = _inspect_local_paper_inputs(message)
    pdf_count = int(inspection.get("pdf_count", 0) or 0)
    requires_ingest = bool(inspection.get("requires_ingest"))
    wants_team_orchestration = _looks_like_paper_team_orchestration_task(message)
    requested_lower = requested.lower()

    if paper_team and requested_team_entry and requested_team_entry.key == paper_team.key:
        if BuiltinAgents.get(paper_team.runtime_agent):
            return paper_team.runtime_agent, inspection
        return requested, inspection

    if (
        paper_team
        and wants_team_orchestration
        and requested_lower in _PAPER_AUTO_ROUTE_FALLBACK_AGENTS
        and BuiltinAgents.get(paper_team.runtime_agent)
    ):
        return paper_team.runtime_agent, inspection

    if requested_execution_profile == "orchestrated" and requested in {"", "manager"}:
        if pdf_count > 0 or requires_ingest:
            if paper_team and BuiltinAgents.get(paper_team.runtime_agent):
                return paper_team.runtime_agent, inspection
        if (
            not (pdf_count > 0 or requires_ingest)
            and wants_team_orchestration
        ):
            if paper_team and BuiltinAgents.get(paper_team.runtime_agent):
                return paper_team.runtime_agent, inspection

    return requested, inspection


def _legacy_protein_auto_route_request_allows(requested_agent_name: str) -> bool:
    """Legacy team-switch guard, never an admission rule for new workflows."""
    normalized = str(requested_agent_name or "").strip().lower()
    if not normalized:
        return True
    team_entry = TeamCatalog.get_by_agent_name(normalized, available_only=False)
    if team_entry and team_entry.key == "protein":
        return True
    return normalized in {
        "build",
        "general",
        "manager",
        "protein_orchestrator",
        "protein-design",
    }


def _protein_route_should_switch_agent(
    *,
    requested_agent_name: str,
    requested_execution_profile: str,
    decision: WorkflowRouteDecision,
) -> bool:
    if not bool(decision.should_delegate_to_recommended_agent):
        return False
    normalized = str(requested_agent_name or "").strip().lower()
    team_entry = TeamCatalog.get_by_agent_name(normalized, available_only=False)
    requested_protein_team = bool(
        team_entry and team_entry.key == "protein"
    ) or normalized in {
        "protein_orchestrator",
        "protein-design",
    }
    if requested_protein_team:
        return True
    if bool(decision.should_start):
        return True
    if (
        str(requested_execution_profile or "").strip().lower() == "orchestrated"
        and normalized in {"", "manager"}
    ):
        return True
    return False


async def _refine_workflow_route_with_model(
    *, workdir: str, session_id: str, message: str, requested_agent_name: str,
    heuristic_decision: WorkflowRouteDecision, resource_context=None,
) -> WorkflowRouteDecision:
    """Select from registered capabilities; execution still requires approval."""
    from ..workflow.discovery import discover_workflow
    from ..workflow.input_bindings import bind_workflow_request
    from ..workflow.clarifications import missing_required_fields_from_contract

    if heuristic_decision.should_start or is_protein_design_brainstorm_mode(requested_agent_name):
        return heuristic_decision
    active = _active_workflow_run_for_session(session_id=session_id, workdir=workdir)
    if active is not None and should_auto_route_workflow_follow_up(message, active):
        # Existing run recovery is handled by the normal web path. It must not
        # incur another catalog search or replace its original input request.
        return heuristic_decision
    if not workflow_routing_cards(limit=1):
        return heuristic_decision
    provider = None
    try:
        provider = create_provider(workdir=workdir)
        discovery = await discover_workflow(message, provider=provider)
    except Exception:
        discovery = await discover_workflow(message, provider=None)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
    signals = {**heuristic_decision.signals, "workflow_discovery": discovery.to_dict()}
    if discovery.status != "matched":
        return replace(heuristic_decision, signals=signals, reason=discovery.reason or heuristic_decision.reason)
    template = WorkflowTemplateRegistry.get(discovery.template_key)
    if template is None:
        return replace(heuristic_decision, signals=signals, reason="所选模板已卸载；未启动。")
    payload = bind_workflow_request(dict(heuristic_decision.input_payload), template.input_contract, message)
    # should_start asks the web startup path to present its existing confirmation.
    # Never fabricate requested_workflow_template_key for a model recommendation.
    return WorkflowRouteDecision(
        should_start=True, intent=template.template_key, intent_family="workflow",
        template_key=template.template_key, confidence=discovery.confidence, reason=discovery.reason,
        input_payload=payload, missing_required=missing_required_fields_from_contract(payload, template.input_contract),
        signals=signals, recommended_agent=requested_agent_name or "build",
        should_delegate_to_recommended_agent=False,
    )


def _active_workflow_run_for_session(
    *,
    session_id: str,
    workdir: str,
    template_key: Optional[str] = None,
) -> Optional[WorkflowRun]:
    if not str(session_id or "").strip():
        return None
    try:
            runs = WorkflowRunManager.list_runs(
                session_id=str(session_id or "").strip(),
                template_key=template_key,
                workdir=workdir,
        )
    except Exception:
        return None
    terminal_statuses = {"completed", "cancelled", "terminated"}
    for run in runs:
        if str(run.status or "").strip().lower() not in terminal_statuses:
            return run
    return None


def _protein_design_resume_execution_mode(
    decision: WorkflowRouteDecision,
) -> str:
    signals = dict(decision.signals or {})
    if not bool(signals.get("protein_design_resume_approved")):
        return ""
    mode = str(signals.get("protein_design_execution_mode") or "").strip().lower()
    if mode not in {"direct", "agent_loop", "workflow_loop"}:
        return ""
    return mode


def _workflow_auto_route_needs_confirmation(
    decision: WorkflowRouteDecision,
) -> bool:
    """Approved briefs already contain the user's workflow-loop authorization."""

    requested_template_key = str(
        (decision.signals or {}).get("requested_workflow_template_key") or ""
    ).strip()
    if requested_template_key and requested_template_key == decision.template_key:
        return False

    return _protein_design_resume_execution_mode(decision) != "workflow_loop"


def _select_or_create_auto_workflow_run(
    *,
    session_id: str,
    workdir: str,
    decision: WorkflowRouteDecision,
    message: str,
    owner_kwargs: Optional[dict[str, str]] = None,
    applicability_assessment: Optional[dict[str, Any]] = None,
    applicability_confirmed: bool = False,
    applicability_confirmation_source: str = "",
) -> WorkflowRun:
    template_key = str(decision.template_key or "").strip()
    if not template_key:
        raise ValueError("workflow template_key is required for auto workflow route")
    input_payload = dict(decision.input_payload or {})
    applicability = dict(applicability_assessment or {})
    if applicability:
        applicability["confirmation"] = {
            "confirmed": bool(applicability_confirmed),
            "source": str(applicability_confirmation_source or "") or None,
            "fingerprint": str(applicability.get("fingerprint") or ""),
        }
    metadata = {
        "auto_started": True,
        "auto_route": decision.to_dict(),
        "auto_route_last_message": str(message or "")[:1200],
        "intent_snapshot": {
            "original_user_request": str(message or "").strip(),
            "source": "workflow_auto_route",
        },
        **({"applicability": applicability} if applicability else {}),
        **dict(owner_kwargs or {}),
    }
    force_new_run = bool((decision.signals or {}).get("force_new_run"))
    existing = None if force_new_run else _active_workflow_run_for_session(
        session_id=session_id,
        workdir=workdir,
        template_key=template_key,
    )
    if existing is not None:
        from ..workflow.input_bindings import request_binding_field

        existing_metadata = dict(existing.metadata or {})
        snapshot = existing_metadata.get("template_snapshot") or {}
        bound_field = request_binding_field(snapshot.get("input_contract"))
        if bound_field and bound_field in existing.input_payload:
            # Reusing a run is not a new request-binding event.
            input_payload.pop(bound_field, None)
        resumed_metadata = {
            **existing_metadata,
            **metadata,
            "auto_resumed": True,
        }
        for immutable_key in ("intent_snapshot", "applicability"):
            if existing_metadata.get(immutable_key):
                resumed_metadata[immutable_key] = existing_metadata[immutable_key]
        return WorkflowRunManager.merge_input_payload(
            existing.run_id,
            input_payload=input_payload,
            metadata=resumed_metadata,
            workdir=workdir,
        )
    run = WorkflowRunManager.create_run(
        template_key=template_key,
        session_id=session_id,
        input_payload=input_payload,
        metadata=metadata,
        workdir=workdir,
    )
    if applicability:
        run = WorkflowRunManager.append_event(
            run.run_id,
            kind="workflow_preflight_evaluated",
            source="workflow_auto_route",
            summary=(
                "Workflow applicability accepted after user confirmation"
                if applicability_confirmed
                else "Workflow applicability accepted"
            ),
            payload={
                "verdict": applicability.get("verdict"),
                "confidence": applicability.get("confidence"),
                "reason": applicability.get("reason"),
                "source": applicability.get("source"),
                "confirmation": dict(applicability.get("confirmation") or {}),
            },
            workdir=workdir,
        )
    return run


def _schedule_workflow_auto_continue(
    *,
    session_id: str,
    run_id: str,
    workdir: str,
) -> bool:
    try:
        # Load lazily: workflow routes depend on shared runtime services.
        from .workflow_api import _schedule_workflow_auto_continue as scheduler
        if not callable(scheduler) or scheduler is _schedule_workflow_auto_continue:
            return False
        return bool(
            scheduler(
                session_id=session_id,
                run_id=run_id,
                workdir=workdir,
            )
        )
    except Exception:
        return False


def _workflow_recommended_primary_agent(
    run: WorkflowRun,
    *,
    fallback: str = "build",
) -> str:
    """按模板领域与 Agent Stage 所有者选择 Workflow 的主会话 Agent。

    Protein 模板继续进入固定蛋白团队。其他模板优先使用第一个声明为
    ``call_type=agent`` 且可作为主 Agent 的 owner，避免社区 Workflow 因
    通用 ``workflow`` 关键词被错误切换到 Protein 团队。
    """

    metadata = dict(getattr(run, "metadata", {}) or {})
    snapshot = metadata.get("template_snapshot")
    snapshot = dict(snapshot or {}) if isinstance(snapshot, dict) else {}
    domain = str(metadata.get("domain") or snapshot.get("domain") or "").strip().lower()
    if domain in {"protein", "protein-design", "protein_design"}:
        if BuiltinAgents.get("protein_orchestrator"):
            return "protein_orchestrator"

    for stage in snapshot.get("stages", []) or []:
        if not isinstance(stage, dict):
            continue
        if str(stage.get("call_type") or "").strip().lower() != "agent":
            continue
        owner = str(stage.get("owner") or "").strip()
        agent_info = BuiltinAgents.get(owner) if owner else None
        if agent_info is not None and agent_info.mode in {
            AgentMode.PRIMARY,
            AgentMode.MANAGER,
        }:
            return owner

    fallback_name = str(fallback or "").strip()
    fallback_info = BuiltinAgents.get(fallback_name) if fallback_name else None
    if fallback_info is not None and fallback_info.mode in {
        AgentMode.PRIMARY,
        AgentMode.MANAGER,
    }:
        return fallback_name
    return "build"


def _workflow_template_confirmation_summary(template: WorkflowTemplate) -> str:
    stage_labels = [
        str(getattr(stage, "label", "") or getattr(stage, "stage_id", "") or "").strip()
        for stage in (getattr(template, "stages", []) or [])
    ]
    stage_labels = [item for item in stage_labels if item]
    output_contract = dict(getattr(template, "output_contract", {}) or {})
    required_outputs = [
        str(item or "").strip()
        for item in (output_contract.get("required") or [])
        if str(item or "").strip()
    ]
    lines = [
        f"名称：{template.display_name or template.template_key}",
        f"用途：{template.description or '按模板分阶段执行任务。'}",
    ]
    if stage_labels:
        lines.append("主要阶段：" + " -> ".join(stage_labels[:8]))
    if required_outputs:
        lines.append("预期产出：" + "、".join(required_outputs[:8]))
    return "\n".join(lines)


async def _confirm_workflow_auto_route(
    *,
    session_id: str,
    workdir: str,
    decision: WorkflowRouteDecision,
    websocket: Optional[WebSocket] = None,
    question_handler: Any = None,
    applicability_assessment: Optional[dict[str, Any]] = None,
) -> bool:
    template_key = str(decision.template_key or "").strip()
    template = WorkflowTemplateRegistry.get(template_key)
    if template is None:
        return False

    if question_handler is None:
        if websocket is None:
            raise ValueError("workflow confirmation requires a question handler")
        question_handler = _build_websocket_question_handler(session_id, websocket)
    question_tool = QuestionTool()
    tool_context = ToolContext(
        session_id=session_id,
        message_id=str(uuid.uuid4()),
        agent="workflow_auto_route_confirmation",
        abort=asyncio.Event(),
        extra={
            "workdir": workdir,
            "__question_ask__": question_handler,
        },
    )
    assessment = dict(applicability_assessment or {})
    requires_override = bool(assessment.get("requires_confirmation"))
    yes = (
        "仍然运行这个 workflow"
        if requires_override
        else "是，启动这个 workflow"
    )
    no = "否，让 agent 自主解决"
    if requires_override:
        question_lines = [
            "你选择了这个 workflow，但运行前检查发现它可能不适合当前问题。",
            "",
            _workflow_template_confirmation_summary(template),
            "",
            f"检查结论：{assessment.get('verdict') or 'uncertain'}",
            f"原因：{assessment.get('reason') or '当前问题与模板适用范围没有形成明确匹配。'}",
        ]
        do_not_use_when = [
            str(item or "").strip()
            for item in (assessment.get("do_not_use_when") or [])
            if str(item or "").strip()
        ]
        if do_not_use_when:
            question_lines.append("不适用场景：" + "；".join(do_not_use_when[:4]))
        gaps = [str(item).strip() for item in (assessment.get("gaps") or []) if str(item).strip()]
        if gaps:
            question_lines.append("已知限制：" + "；".join(gaps[:4]))
        question_lines.extend(
            [
                "",
                "启动后会按既定步骤执行，不再因适用性问题暂停确认；结果中的限制会在最终回答中说明。",
                "是否仍然运行？选择“否”不会创建 Workflow 运行实例，当前 agent 会改为自主处理。",
            ]
        )
        question_text = "\n".join(question_lines)
        question_goal = "确认是否覆盖 Workflow 适用性检查结论"
    else:
        question_text = "\n".join(
            [
                "我检测到这个请求适合启动一个固定 workflow。是否启动？",
                "",
                _workflow_template_confirmation_summary(template),
                "",
                "启动后会按既定步骤执行，不再因适用性问题暂停确认；结果中的限制会在最终回答中说明。",
                "你只能选择“是”或“否”；如果选择“否”，我会让当前 agent 自主规划并解决，不启动固定 workflow。",
            ]
        )
        question_goal = "确认是否启动自动识别到的 workflow"
    result = await question_tool.execute(
        {
            "question": question_text,
            "type": "single",
            "options": [yes, no],
            "allow_custom": False,
            "default_answer": no,
            "timeout_seconds": 300,
            "goal": question_goal,
            "metadata": {
                "kind": "workflow_auto_route_confirmation",
                "template_key": template_key,
                "decision": decision.to_dict(),
                "applicability": assessment,
            },
        },
        tool_context,
    )
    selected = str((result.metadata or {}).get("selected_option") or "").strip()
    answer = str((result.metadata or {}).get("answer") or "").strip()
    return selected == yes or answer == "1" or answer == yes


def _inject_workflow_discovery_instruction(
    agent: Agent, *, decision: WorkflowRouteDecision,
) -> None:
    """Expose this turn's read-only discovery outcome without repeating it."""
    marker = "[workflow-discovery-result]"
    agent.messages[:] = [item for item in agent.messages
                         if not str(getattr(item, "content", "") or "").startswith(marker)]
    result = (decision.signals or {}).get("workflow_discovery") or {}
    status = result.get("status")
    if decision.should_start or status not in {
        "no_match", "uncertain", "no_templates", "catalog_too_large", "catalog_loading",
        "invalid_request", "invalid_response", "unavailable",
    }:
        return
    content = "\n".join([
        marker,
        f"本轮启动前已做过一次只读 Workflow 目录选择，状态：{status}。",
        "此结果不是执行授权，也不代表业务任务失败。若请求是需要持久执行、恢复或验收的复杂长任务，可以调用一次 workflow_proposal 生成流程建议。用户明确要求创建、生成或使用临时 Workflow/DAG 时，必须立即且只调用一次 workflow_proposal，并传入完整用户原文；禁止在聊天正文中自行撰写 DAG，禁止再次调用目录选择，也不要先追问执行输入或直接启动运行。不要为相同目标重复调用。",
        "若用户要求固定 Workflow 而尚未选定，可用 workflow_list_templates（不传 query）分页查看真实目录，或请用户明确选择。",
        "超时、目录超预算、未加载或响应无效不代表没有合适流程；不得据此宣称无匹配。",
        "仅在影响当前任务时向用户简述限制，不展示这条内部记录或 JSON；不要把技术状态当作普通问候的回答。",
    ])
    agent.messages.append(Message(role="system", content=content, id=str(uuid.uuid4())))


def _inject_workflow_declined_direct_instruction(
    agent: Agent,
    *,
    decision: WorkflowRouteDecision,
) -> None:
    marker = "[workflow-auto-route-declined]"
    if any(marker in str(getattr(item, "content", "") or "") for item in agent.messages):
        return
    content = "\n".join(
        [
            marker,
            f"系统曾建议启动 {decision.template_key or 'workflow'} workflow，但用户选择否。",
            "本轮不要调用 workflow_run，也不要启动固定 workflow 模板。",
            "请由当前 agent 自主分析并解决：可自行规划步骤，按需使用已有工具、paper/protein team 能力或 subagent，但最终回答要说明采用了哪些步骤、产物和阻塞项。",
            "如果任务包含已上传文献或 PDF，必须优先让 paper_generalist 或 paper_auto_coordinator 判断用户是否要保存到本地文献库；保存意图不明确时先用 question 询问，确认后再入库，否则只做临时解析和证据抽取。",
            f"原始路由原因：{decision.reason}",
        ]
    )
    agent.messages.append(
        Message(
            role="system",
            content=content,
            id=str(uuid.uuid4()),
        )
    )


def _inject_protein_direct_execution_instruction(
    agent: Agent,
    *,
    decision: WorkflowRouteDecision,
) -> None:
    marker = "[protein-direct-execution-route]"
    if any(marker in str(getattr(item, "content", "") or "") for item in agent.messages):
        return
    tool_hints = [
        str(item or "").strip()
        for item in (decision.requested_tool_hints or [])
        if str(item or "").strip()
    ]
    hint_text = "、".join(tool_hints) if tool_hints else "未显式指定工具"
    content = "\n".join(
        [
            marker,
            "系统分析：当前请求更像蛋白领域的单阶段执行需求，而不是完整多阶段 workflow。",
            f"route_confidence: {decision.confidence:.3f}",
            f"route_reason: {decision.reason}",
            f"用户显式提到的工具线索: {hint_text}",
            "请先判断用户是否只是想单独运行某个工具、skill 或 MCP 服务，或只完成一个明确阶段。",
            "如果是单阶段需求，优先直接满足当前执行目标，不要默认调用 workflow_run。",
            "只有当用户明确要求候选生成、多阶段筛选、top N、复核、报告，或确实需要跨角色长期编排时，才升级为 workflow_run。",
            "如果用户没有点名具体工具，请基于目标、输入文件和现有团队能力自行判断最合适的现有工具或 Worker。",
        ]
    )
    agent.messages.append(
        Message(
            role="system",
            content=content,
            id=str(uuid.uuid4()),
        )
    )


def _inject_protein_design_resume_instruction(
    agent: Agent,
    *,
    decision: WorkflowRouteDecision,
) -> None:
    mode = _protein_design_resume_execution_mode(decision)
    if mode not in {"direct", "agent_loop"}:
        return
    marker = f"[protein-design-approved-resume:{mode}]"
    if any(marker in str(getattr(item, "content", "") or "") for item in agent.messages):
        return
    mode_instruction = (
        "按简报直接完成交付，不创建 Agent Loop 或 Workflow Loop。"
        if mode == "direct"
        else (
            "加载 protein-design-execution-loop skill，按简报使用 "
            "assign_task(wait=true) 恢复轻量 Agent Loop。"
        )
    )
    content = "\n".join(
        [
            marker,
            "工作区中的 protein_design_brief.md 已通过 BRAINSTORM APPROVED 门禁。",
            f"approved_execution_mode: {mode}",
            "本轮是同一需求的继续或恢复，不要重新进入头脑风暴，也不要重复询问简报中已批准的内容。",
            mode_instruction,
            "只有阻塞、安全或合规风险、不可逆操作、显著新增成本，或必须修改已批准范围时才暂停询问用户。",
        ]
    )
    agent.messages.append(
        Message(
            role="system",
            content=content,
            id=str(uuid.uuid4()),
        )
    )


def _build_websocket_question_handler(
    session_id: str,
    websocket: WebSocket,
) -> Callable[[Dict[str, Any]], Awaitable[str]]:
    async def question_handler(payload: Dict[str, Any]) -> str:
        request_id = str(uuid.uuid4())
        future = ServerState.create_question_future(session_id, request_id, payload)

        default_answer = str(payload.get("default_answer") or "")
        try:
            timeout_seconds = int(payload.get("timeout_seconds", 300))
        except Exception:
            timeout_seconds = 300
        timeout_seconds = max(5, min(timeout_seconds, 1800))

        final_answer = default_answer
        try:
            event = question_request_event(
                request_id=request_id, session_id=session_id, question=payload,
            )
            if not await ServerState.send_session_event(session_id, event):
                try:
                    await websocket.send_json(event)
                except (WebSocketDisconnect, RuntimeError, OSError):
                    # The authenticated status endpoint can restore this live wait
                    # after refresh even if its original socket has disappeared.
                    pass
            answer = await asyncio.wait_for(future, timeout=timeout_seconds)
            final_answer = "" if answer is None else str(answer)
            return final_answer
        except asyncio.TimeoutError:
            siblings = ServerState.pending_question_requests(session_id)
            if siblings:
                # A group timeout cannot turn a partial/default answer into a
                # decision. Release every sibling with no answer/checkpoint.
                final_answer = ''
                default_answer = ''
                for sibling in siblings:
                    ServerState.resolve_question_future(session_id, sibling['request_id'], '')
                for sibling in siblings:
                    await ServerState.send_session_event(session_id, question_timeout_event(
                        request_id=sibling['request_id'], session_id=session_id, answer='',
                    ))
            await ServerState.send_session_event(
                session_id,
                question_timeout_event(
                    request_id=request_id,
                    session_id=session_id,
                    answer=default_answer,
                )
            )
            return final_answer
        finally:
            ServerState.resolve_question_future(
                session_id, request_id, final_answer
            )

    return question_handler


def _workflow_auto_route_pending_fields_text(fields: list[str]) -> str:
    normalized = [str(item or "").strip() for item in (fields or []) if str(item or "").strip()]
    return "、".join(normalized) if normalized else "关键信息"


async def _attempt_server_side_workflow_auto_route(
    *,
    session_id: str,
    workdir: str,
    message: str,
    decision: WorkflowRouteDecision,
    run: WorkflowRun,
    websocket: Optional[WebSocket] = None,
    resource_context: Optional[dict[str, Any]] = None,
    question_handler: Any = None,
    send_event: Any = None,
    user_message_id: str = "",
) -> bool:
    template_key = str(decision.template_key or run.template_key or "").strip()
    template = WorkflowTemplateRegistry.get(template_key)
    if template is None:
        return False

    if question_handler is None:
        if websocket is None:
            raise ValueError("workflow dispatch requires a question handler")
        question_handler = _build_websocket_question_handler(session_id, websocket)
    if send_event is None:
        if websocket is None:
            raise ValueError("workflow dispatch requires an event sender")
        send_event = websocket.send_json
    question_tool = QuestionTool()
    tool_context = ToolContext(
        session_id=session_id,
        message_id=str(uuid.uuid4()),
        agent="workflow_auto_route",
        abort=asyncio.Event(),
        extra={
            "workdir": workdir,
            "resource_context": dict(resource_context or {}),
            **{
                key: str((run.metadata or {}).get(key) or "").strip()
                for key in ("tenant_id", "user_id", "workspace_id")
                if str((run.metadata or {}).get(key) or "").strip()
            },
            "__question_ask__": question_handler,
            "__question_resolve__": lambda payload: infer_answer_from_context(
                payload,
                resource_context,
            ),
        },
    )

    run = WorkflowRunManager.resume_legacy_applicability_pause(
        run.run_id, workdir=workdir,
    )

    merged_run = WorkflowRunManager.merge_input_payload(
        run.run_id,
        input_payload=dict(decision.input_payload or {}),
        metadata={
            "auto_started": True,
            "auto_route": decision.to_dict(),
            "auto_route_last_message": str(message or "")[:1200],
            "auto_route_mode": "server_preflight",
        },
        workdir=workdir,
    )
    resolved_inputs, questions, autofilled = build_workflow_questions(
        merged_run.input_payload,
        resource_context,
        input_contract=template.input_contract,
        template_key=template_key,
    )
    merged_run = WorkflowRunManager.merge_input_payload(
        merged_run.run_id,
        input_payload=resolved_inputs,
        metadata={
            "autofilled_inputs": dict(autofilled),
        },
        workdir=workdir,
    )

    for question in questions:
        metadata = dict(question.get("metadata") or {})
        metadata["workflow_run_id"] = merged_run.run_id
        metadata["workflow_inputs"] = dict(merged_run.input_payload or {})
        question["metadata"] = metadata
        result = await question_tool.execute(question, tool_context)
        answer = serialize_answer((result.metadata or {}).get("answer"))
        field = str(metadata.get("workflow_field") or "").strip()
        if not field:
            continue
        merged_run = WorkflowRunManager.merge_input_payload(
            merged_run.run_id,
            input_payload={field: answer} if answer else {},
            metadata={"last_clarified_field": field},
            workdir=workdir,
        )

    resolved_inputs, remaining_questions, autofilled = build_workflow_questions(
        merged_run.input_payload,
        resource_context,
        input_contract=template.input_contract,
        template_key=template_key,
    )
    merged_run = WorkflowRunManager.merge_input_payload(
        merged_run.run_id,
        input_payload=resolved_inputs,
        metadata={"autofilled_inputs": dict(autofilled)},
        workdir=workdir,
    )
    unresolved_required = [
        str(dict(item.get("metadata") or {}).get("workflow_field") or "").strip()
        for item in remaining_questions
        if item.get("required")
    ]
    unresolved_required = [item for item in unresolved_required if item]

    if unresolved_required:
        merged_run = WorkflowRunManager.update_status(
            merged_run.run_id,
            status="blocked",
            metadata={
                "clarification_pending_fields": unresolved_required,
                "auto_route_phase": "clarification_pending",
            },
            current_stage_id=merged_run.current_stage_id or merged_run.next_pending_stage_id(),
            workdir=workdir,
        )
    elif (
        str(merged_run.status or "").strip().lower() == "blocked"
        and str(
            dict((merged_run.metadata or {}).get("runtime_applicability") or {}).get(
                "status"
            )
            or ""
        ).strip().lower()
        not in {"pending", "rejected"}
    ):
        merged_run = WorkflowRunManager.update_status(
            merged_run.run_id,
            status="queued",
            metadata={
                "clarification_pending_fields": [],
                "auto_route_phase": "queued",
            },
            current_stage_id=merged_run.current_stage_id or merged_run.next_pending_stage_id(),
            workdir=workdir,
        )

    auto_execution_scheduled = False
    if not unresolved_required:
        auto_execution_scheduled = _schedule_workflow_auto_continue(
            session_id=session_id,
            run_id=merged_run.run_id,
            workdir=workdir,
        )

    if not auto_execution_scheduled and not unresolved_required:
        merged_run = WorkflowRunManager.update_status(
            merged_run.run_id,
            status=merged_run.status,
            metadata={"auto_route_phase": "queued"},
            current_stage_id=merged_run.current_stage_id,
            workdir=workdir,
        )
    elif auto_execution_scheduled:
        merged_run = WorkflowRunManager.update_status(
            merged_run.run_id,
            status=merged_run.status,
            metadata={"auto_route_phase": "scheduled"},
            current_stage_id=merged_run.current_stage_id,
            workdir=workdir,
        )

    await send_event(
        {
            "type": "workflow_auto_route",
            "session_id": session_id,
            "decision": decision.to_dict(),
            "run": merged_run.to_dict(),
            "auto_execution_scheduled": auto_execution_scheduled,
            "route_phase": (
                "clarification_pending"
                if unresolved_required
                else ("scheduled" if auto_execution_scheduled else "queued")
            ),
            "clarification_pending_fields": unresolved_required,
            "done": False,
        }
    )

    if unresolved_required:
        assistant_content = (
            "我已经开始整理这项任务，但继续下一步前还需要这些信息："
            f"{_workflow_auto_route_pending_fields_text(unresolved_required)}。\n"
            "当前输入和上传文件已经保存；你直接在这里补充即可，我会从当前位置继续。"
        )
    else:
        next_stage = str(
            merged_run.current_stage_id or merged_run.next_pending_stage_id() or ""
        ).strip()
        next_stage_state = next(
            (
                item
                for item in (merged_run.stage_states or [])
                if str(getattr(item, "stage_id", "") or "").strip() == next_stage
            ),
            None,
        )
        next_stage_name = str(
            getattr(next_stage_state, "label", "")
            or getattr(next_stage_state, "stage_id", "")
            or "第一步"
        ).strip()
        total_stages = len(merged_run.stage_states or [])
        assistant_content = (
            f"我已经开始处理这项任务{f'，一共分为 {total_stages} 个步骤' if total_stages else ''}。"
            f"{f' 现在先进行“{next_stage_name}”。' if next_stage else ''}\n"
            "每完成一步，我都会在这里同步结果，然后继续下一步。"
        )

    await _append_message_to_session(
        session_id,
        Message(
            role="user",
            content=message,
            id=user_message_id or str(uuid.uuid4()),
        ),
    )
    from .workflow_api import (
        _send_workflow_assistant_message,
        _workflow_status_message_id,
    )

    await _send_workflow_assistant_message(
        merged_run,
        assistant_content,
        message_id=_workflow_status_message_id(merged_run),
        presentation="workflow_progress",
    )
    done_runtime = _chat_runtime_status_payload(
        session_id,
        direct_running_override=False,
    )
    await send_event(
        {
            "type": "done",
            **done_runtime,
            "done": True,
        }
    )
    return True


def _pick_manager_team_delegate(
    message: str,
) -> Optional[dict[str, Any]]:
    inspection = _inspect_local_paper_inputs(message)
    route_request = TeamRouteRequest(
        message=message,
        requested_agent_name="manager",
        requested_execution_profile="orchestrated",
        metadata={
            "paper_inspection": inspection,
        },
    )
    decision = TeamRoutingPolicy.route(route_request, available_only=True)
    return decision.to_payload() if decision else None


def _sticky_team_delegate_payload(
    team_agent_name: str,
    *,
    reason: str,
    route_rule: str,
    route_source: str,
) -> Optional[dict[str, Any]]:
    normalized_agent = str(team_agent_name or "").strip()
    if not normalized_agent or not BuiltinAgents.get(normalized_agent):
        return None
    team_entry = TeamCatalog.get_by_runtime_agent(normalized_agent, available_only=True)
    if not team_entry:
        return None
    return {
        "team_key": team_entry.key,
        "agent_name": normalized_agent,
        "display_name": team_entry.display_name,
        "slug": team_entry.slug,
        "reason": reason,
        "route_rule": route_rule,
        "route_source": route_source,
    }


def _runtime_team_snapshot(*, include_unavailable: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in TeamCatalog.list(available_only=False):
        available = BuiltinAgents.get(entry.runtime_agent) is not None
        if not include_unavailable and not available:
            continue
        rows.append(
            {
                "team_key": entry.key,
                "display_name": entry.display_name,
                "runtime_agent": entry.runtime_agent,
                "visible_agent_name": entry.visible_agent_name,
                "delegation_reason": entry.delegation_reason,
                "source": entry.source,
                "available": available,
            }
        )
    return rows


async def _remember_last_delegated_team(
    parent_session_id: str,
    team_agent_name: str,
) -> None:
    normalized_agent = str(team_agent_name or "").strip()
    if not normalized_agent:
        return
    runtime = ServerState.sessions.get(parent_session_id)
    if isinstance(runtime, dict):
        runtime[_LAST_DELEGATED_TEAM_METADATA_KEY] = normalized_agent

    persisted = await SessionManager.get(parent_session_id)
    if not persisted:
        return
    metadata = dict(getattr(persisted, "metadata", {}) or {})
    metadata[_LAST_DELEGATED_TEAM_METADATA_KEY] = normalized_agent
    await SessionManager.update(parent_session_id, metadata=metadata)


async def _pick_sticky_manager_team_delegate(
    session_id: str,
    message: str,
) -> Optional[dict[str, Any]]:
    remembered_agent = ""
    runtime = ServerState.sessions.get(session_id)
    if isinstance(runtime, dict):
        remembered_agent = str(
            runtime.get(_LAST_DELEGATED_TEAM_METADATA_KEY, "") or ""
        ).strip()

    if not remembered_agent:
        persisted = await SessionManager.get(session_id)
        if isinstance(getattr(persisted, "metadata", None), dict):
            remembered_agent = str(
                getattr(persisted, "metadata", {}).get(
                    _LAST_DELEGATED_TEAM_METADATA_KEY, ""
                )
                or ""
            ).strip()

    if not remembered_agent:
        for child in await SessionManager.list(parent_id=session_id):
            metadata = dict(getattr(child, "metadata", {}) or {})
            delegated_agent = str(
                metadata.get(_DELEGATED_TEAM_METADATA_KEY) or child.agent_name or ""
            ).strip()
            if not delegated_agent:
                continue
            if TeamCatalog.get_by_runtime_agent(delegated_agent, available_only=True):
                remembered_agent = delegated_agent
                break

    if not remembered_agent:
        return None

    remembered_team = TeamCatalog.get_by_runtime_agent(
        remembered_agent,
        available_only=True,
    )
    if not remembered_team:
        return None

    if remembered_team.key == "paper" and not _looks_like_paper_follow_up_message(message):
        return None

    if remembered_team.key == "protein" and not _looks_like_protein_team_task(message):
        return None

    return _sticky_team_delegate_payload(
        remembered_agent,
        reason=f"沿用上轮 {remembered_team.display_name} 上下文处理本轮跟进请求",
        route_rule="sticky_manager_team_continue",
        route_source="session_metadata",
    )


async def _pick_manager_team_delegate_for_session(
    session_id: str,
    message: str,
) -> Optional[dict[str, Any]]:
    direct = _pick_manager_team_delegate(message)
    if direct:
        return direct
    return await _pick_sticky_manager_team_delegate(session_id, message)


def _route_manager_to_paper_team(request: TeamRouteRequest) -> Optional[dict[str, Any]]:
    inspection = dict(request.metadata.get("paper_inspection") or {})
    if inspection.get("pdf_count") or inspection.get("requires_ingest"):
        return {
            "metadata": {
                "inspection": inspection,
                "matched_intent": "paper_local_input_context",
            }
    }
    if (
        not (inspection.get("pdf_count") or inspection.get("requires_ingest"))
        and _looks_like_paper_team_orchestration_task(request.message)
    ):
        return {
            "metadata": {
                "inspection": inspection,
                "matched_intent": "paper_team_orchestration",
            }
        }
    return None


def _route_manager_to_protein_team(request: TeamRouteRequest) -> Optional[dict[str, Any]]:
    if not _looks_like_protein_team_task(request.message):
        return None
    return {
        "metadata": {
            "matched_intent": "protein_team_task",
        }
    }


TeamRoutingPolicy.register_rule(
    TeamRouteRule(
        name="manager_paper_team",
        team_key="paper",
        resolver=_route_manager_to_paper_team,
        priority=200,
        source="builtin",
    ),
    source="builtin",
)
TeamRoutingPolicy.register_rule(
    TeamRouteRule(
        name="manager_protein_team",
        team_key="protein",
        resolver=_route_manager_to_protein_team,
        priority=150,
        source="builtin",
    ),
    source="builtin",
)


async def _bootstrap_fixed_team_workers(session_id: str, agent_name: str) -> int:
    persisted_session = await SessionManager.get(session_id)
    persisted_workdir = str(
        getattr(persisted_session, "workdir", "") or ""
    ).strip()
    session = ServerState.get_or_create_session(
        session_id,
        agent_name,
        workdir=persisted_workdir or None,
    )
    session_workdir = str(
        persisted_workdir
        or session.get("workdir", "")
        or ServerState.get_workdir()
    ).strip()
    if not session_workdir:
        session_workdir = ServerState.get_workdir()
    tenant_id = str(
        getattr(persisted_session, "tenant_id", "")
        or session.get("tenant_id", "")
        or ""
    ).strip()
    user_id = str(
        getattr(persisted_session, "user_id", "")
        or session.get("user_id", "")
        or ""
    ).strip()
    inferred_workspace_id = workdir_utils.workdir_name_from_path(
        ServerState.get_workdir(),
        session_workdir,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workspace_id": str(
            inferred_workspace_id
            or getattr(persisted_session, "workspace_id", "")
            or session.get("workspace_id", "")
            or ""
        ).strip(),
    }
    session["workdir"] = session_workdir
    session.update({key: value for key, value in owner.items() if value})
    created_worker_ids: list[str] = []
    manager = None
    try:
        if await wait_for_plugin_warmup(
            session_workdir,
            timeout=plugin_bootstrap_wait_seconds(),
        ):
            manager = await PluginManager.get_global(session_workdir)
    except Exception as exc:
        print(f"[WS] plugin manager bootstrap load error: {exc}")

    team_entry = TeamCatalog.get_by_runtime_agent(agent_name, available_only=False)

    if team_entry:
        try:
            from ..team.session_bootstrap import ensure_session_team_workers

            if team_entry.key == "protein":
                session["peer_communication_enabled"] = (
                    await get_team_peer_settings(session_id)
                )["enabled"]
            created_worker_ids = await ensure_session_team_workers(
                session_id=session_id,
                agent_name=agent_name,
                workdir=session_workdir,
                owner=owner,
            )
        except Exception as exc:
            print(f"[WS] team bootstrap error: {exc}")

    if manager is None:
        return len(created_worker_ids)

    try:
        bootstrap_output = await manager.trigger(
            "session.bootstrap",
            {
                "session_id": session_id,
                "agent": agent_name,
                "workdir": session_workdir,
            },
            {"workers_created": len(created_worker_ids)},
        )
        return int(
            bootstrap_output.get("workers_created", len(created_worker_ids)) or 0
        )
    except Exception:
        return len(created_worker_ids)


async def _ensure_delegated_agent_session_unlocked(
    parent_session_id: str,
    delegated_agent_name: str,
    *,
    resource_context: Optional[dict[str, Any]] = None,
    delegation_id: str,
) -> tuple[Session, bool]:
    team_entry = TeamCatalog.get_by_runtime_agent(
        delegated_agent_name,
        available_only=False,
    )
    display_name = team_entry.display_name if team_entry else delegated_agent_name
    slug = (
        team_entry.slug
        if team_entry
        else delegated_agent_name
    )
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(slug or "agent")).strip("-") or "agent"
    title = f"{display_name} · {'Team' if team_entry else 'Agent'}"
    parent_session = await SessionManager.get(parent_session_id)
    if parent_session is None:
        raise ValueError(f"Parent session not found: {parent_session_id}")
    parent_context = (
        _workspace_context_from_request(
            None,
            tenant_id=str(getattr(parent_session, "tenant_id", "") or ""),
            user_id=str(getattr(parent_session, "user_id", "") or ""),
            workspace_id=str(getattr(parent_session, "workspace_id", "") or ""),
        )
        if parent_session
        else None
    )
    parent_workspace_owner = _session_owner_kwargs(
        parent_context,
        include_workspace=True,
    )
    parent_workdir = await _resolve_session_workdir(parent_session_id, parent_context)
    normalized_delegation_id = str(delegation_id or "").strip()
    if not normalized_delegation_id:
        raise ValueError("delegation_id is required")

    children = await SessionManager.list(
        parent_id=parent_session_id,
        **parent_workspace_owner,
    )
    for child in children:
        metadata = dict(getattr(child, "metadata", {}) or {})
        child_delegation_id = str(
            metadata.get(_DELEGATION_ID_METADATA_KEY) or ""
        ).strip()
        delegated_agent = str(metadata.get(_DELEGATED_AGENT_METADATA_KEY) or "").strip()
        if (
            child_delegation_id != normalized_delegation_id
            or delegated_agent != delegated_agent_name
            or str(child.agent_name or "").strip() != delegated_agent_name
        ):
            continue
        merged_metadata = dict(metadata)
        merged_metadata[_DELEGATED_AGENT_METADATA_KEY] = delegated_agent_name
        merged_metadata[_DELEGATION_ID_METADATA_KEY] = normalized_delegation_id
        merged_metadata["workspace_strategy"] = "shared_control_workdir"
        if team_entry is not None:
            merged_metadata[_DELEGATED_TEAM_METADATA_KEY] = delegated_agent_name
        merged_metadata["display_name"] = display_name
        if resource_context:
            merged_metadata["resource_context"] = _merge_resource_context(
                merged_metadata.get("resource_context"),
                resource_context,
            )
        await SessionManager.update(
            child.id,
            **parent_workspace_owner,
            title=title,
            agent_name=delegated_agent_name,
            workdir=parent_workdir,
            metadata=merged_metadata,
        )
        runtime = ServerState.get_or_create_session(
            child.id,
            agent_name=delegated_agent_name,
            scheduler_mode="supervisor",
            workdir=parent_workdir,
        )
        if parent_context is not None:
            runtime["tenant_id"] = parent_context.tenant_id
            runtime["user_id"] = parent_context.user_id
            runtime["workspace_id"] = parent_context.workspace_id
        if merged_metadata.get("resource_context"):
            runtime["resource_context"] = dict(
                merged_metadata.get("resource_context") or {}
            )
        refreshed = await SessionManager.get(child.id, **parent_workspace_owner)
        return refreshed or child, False

    identity_digest = hashlib.sha256(
        "|".join(
            (
                str(getattr(parent_context, "tenant_id", "") or ""),
                str(getattr(parent_context, "user_id", "") or ""),
                str(getattr(parent_context, "workspace_id", "") or ""),
                parent_session_id,
                delegated_agent_name,
                normalized_delegation_id,
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    session_id = f"{'team' if team_entry else 'agent'}-{slug}-{identity_digest}"
    metadata = {
        _DELEGATED_AGENT_METADATA_KEY: delegated_agent_name,
        _DELEGATION_ID_METADATA_KEY: normalized_delegation_id,
        "display_name": display_name,
        "resource_context": dict(resource_context or {}),
        "workspace_strategy": "shared_control_workdir",
    }
    if team_entry is not None:
        metadata[_DELEGATED_TEAM_METADATA_KEY] = delegated_agent_name
    created = await SessionManager.get(session_id, **parent_workspace_owner)
    if not created:
        created = await SessionManager.create(
            session_id=session_id,
            parent_id=parent_session_id,
            title=title,
            agent_name=delegated_agent_name,
            workdir=parent_workdir,
            tenant_id=getattr(parent_context, "tenant_id", None),
            user_id=getattr(parent_context, "user_id", None),
            workspace_id=getattr(parent_context, "workspace_id", None),
            metadata=metadata,
        )
    else:
        if (
            str(getattr(created, "parent_id", "") or "") != parent_session_id
            or str(getattr(created, "agent_name", "") or "") != delegated_agent_name
        ):
            raise RuntimeError(
                f"Delegated session identity collision: {session_id}"
            )
        merged_metadata = dict(getattr(created, "metadata", {}) or {})
        if str(merged_metadata.get(_DELEGATION_ID_METADATA_KEY) or "") != normalized_delegation_id:
            raise RuntimeError(
                f"Delegated session contract collision: {session_id}"
            )
        merged_metadata.update(
            {
                _DELEGATED_AGENT_METADATA_KEY: delegated_agent_name,
                _DELEGATION_ID_METADATA_KEY: normalized_delegation_id,
                "display_name": display_name,
                "workspace_strategy": "shared_control_workdir",
            }
        )
        if team_entry is not None:
            merged_metadata[_DELEGATED_TEAM_METADATA_KEY] = delegated_agent_name
        if resource_context:
            merged_metadata["resource_context"] = _merge_resource_context(
                merged_metadata.get("resource_context"),
                resource_context,
            )
        await SessionManager.update(
            session_id,
            **parent_workspace_owner,
            parent_id=parent_session_id,
            title=title,
            agent_name=delegated_agent_name,
            workdir=parent_workdir,
            metadata=merged_metadata,
        )
        created = (
            await SessionManager.get(session_id, **parent_workspace_owner)
            or created
        )

    runtime = ServerState.get_or_create_session(
        created.id,
        agent_name=delegated_agent_name,
        scheduler_mode="supervisor",
        workdir=parent_workdir,
    )
    if parent_context is not None:
        runtime["tenant_id"] = parent_context.tenant_id
        runtime["user_id"] = parent_context.user_id
        runtime["workspace_id"] = parent_context.workspace_id
    if metadata.get("resource_context"):
        runtime["resource_context"] = dict(metadata.get("resource_context") or {})
    return created, True


async def _ensure_delegated_agent_session(
    parent_session_id: str,
    delegated_agent_name: str,
    *,
    resource_context: Optional[dict[str, Any]] = None,
    delegation_id: str = "",
) -> tuple[Session, bool]:
    normalized_id = str(delegation_id or "").strip()
    if not normalized_id:
        normalized_id = f"agent-route:{delegated_agent_name}"
    lock_key = f"create:{parent_session_id}:{delegated_agent_name}:{normalized_id}"
    async with _delegated_session_lock(lock_key):
        return await _ensure_delegated_agent_session_unlocked(
            parent_session_id,
            delegated_agent_name,
            resource_context=resource_context,
            delegation_id=normalized_id,
        )


async def _ensure_delegated_team_session(
    parent_session_id: str,
    team_agent_name: str,
    *,
    resource_context: Optional[dict[str, Any]] = None,
) -> tuple[Session, bool]:
    """Compatibility wrapper for callers that explicitly route a fixed team."""

    return await _ensure_delegated_agent_session(
        parent_session_id,
        team_agent_name,
        resource_context=resource_context,
        delegation_id=f"team-route:{team_agent_name}",
    )


async def _append_message_to_session(
    session_id: str,
    message: Message,
    *,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> None:
    owner = dict(owner_kwargs or {})
    persisted = await SessionManager.get(session_id, **owner)
    if not persisted:
        return
    next_messages = list(getattr(persisted, "messages", None) or [])
    next_messages.append(message)
    ts = datetime.now().timestamp()
    await SessionManager.update(
        session_id,
        **owner,
        messages=next_messages,
        last_active_at=ts,
    )
    persisted.messages = next_messages
    persisted.last_active_at = ts
    runtime_agent = ServerState.agents.get(session_id)
    if runtime_agent is not None:
        runtime_agent.messages = list(next_messages)


async def _persist_live_session_messages(
    session_id: str,
    *,
    agent_name: str,
    workdir: str,
    messages: list[Message],
    owner_kwargs: Optional[dict[str, str]] = None,
) -> None:
    owner = dict(owner_kwargs or {})
    ts = datetime.now().timestamp()
    await SessionManager.update(
        session_id,
        **owner,
        messages=list(messages),
        agent_name=agent_name,
        workdir=workdir,
        last_active_at=ts,
    )
    persisted = await SessionManager.get(session_id, **owner)
    if persisted is not None:
        persisted.messages = list(messages)
        persisted.last_active_at = ts


def _format_subagent_agent_event_message(payload: dict[str, Any]) -> str:
    tool_name = str(payload.get("tool_name", "") or "").strip()
    message = str(payload.get("message", "") or "").strip()
    received_messages = payload.get("received_messages")
    returned_messages = payload.get("returned_messages")

    if tool_name:
        tool_args = payload.get("tool_args")
        args_preview = ""
        if isinstance(tool_args, dict) and tool_args:
            try:
                raw = json.dumps(tool_args, ensure_ascii=False)
                args_preview = raw if len(raw) <= 240 else raw[:237] + "..."
            except Exception:
                args_preview = ""
        return f"[Subagent Event] 🔧 {tool_name}{(' ' + args_preview) if args_preview else ''}"

    if isinstance(received_messages, list) and received_messages:
        parts: list[str] = []
        for item in received_messages:
            if not isinstance(item, dict):
                continue
            src = str(item.get("from", "") or "unknown").strip()
            msg = str(item.get("message", "") or "").strip()
            if msg:
                parts.append(f"收到 {src}: {msg}")
        if parts:
            return "[Subagent Event] " + "；".join(parts)

    if isinstance(returned_messages, list) and returned_messages:
        parts: list[str] = []
        for item in returned_messages:
            if not isinstance(item, dict):
                continue
            target = str(item.get("to", "") or "unknown").strip()
            msg = str(item.get("message", "") or "").strip()
            if msg:
                parts.append(f"发给 {target}: {msg}")
        if parts:
            return "[Subagent Event] " + "；".join(parts)

    if message:
        return f"[Subagent Event] {message}"
    return ""


def _should_persist_subagent_agent_event(payload: dict[str, Any]) -> bool:
    """Keep transient progress out of the durable child conversation."""

    kind = str(payload.get("kind", "") or "").strip().lower()
    if kind == "context_compaction":
        return False
    if kind in {"process_note", "tool_runtime_progress"}:
        return False
    if kind == "llm_progress":
        status = str(payload.get("status", "") or "").strip().lower()
        return status in {"timeout", "error"}
    return True


async def _collect_session_ancestor_ids(
    session_id: str,
    *,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> list[str]:
    owner = dict(owner_kwargs or {})
    ancestors: list[str] = []
    seen: set[str] = {str(session_id or "").strip()}
    current = await SessionManager.get(session_id, **owner)
    parent_id = str(getattr(current, "parent_id", "") or "").strip() if current else ""
    while parent_id and parent_id not in seen:
        current = await SessionManager.get(parent_id, **owner)
        if current is None:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        parent_id = str(getattr(current, "parent_id", "") or "").strip() if current else ""
    return ancestors


def _build_subagent_interrupt_notice(
    sub_session: Session,
    *,
    details: dict[str, Any],
    controller_session_id: str = "",
) -> str:
    display_name = str(details.get("display_name", "") or sub_session.agent_name or "subagent").strip()
    context = details.get("context") if isinstance(details.get("context"), dict) else {}
    latest_user = str(context.get("last_user", "") or "").strip()
    latest_response = str(details.get("last_response", "") or "").strip()
    tools_used = details.get("tools_used") if isinstance(details.get("tools_used"), list) else []

    lines = [
        "[Subagent Interrupted]",
        "用户刚刚手动中止了一个子代理回合。",
        f"subagent: {display_name} ({sub_session.agent_name})",
        f"session_id: {sub_session.id}",
    ]
    if controller_session_id:
        lines.append(f"controller_session_id: {controller_session_id}")
    if latest_user:
        lines.append(f"最近子任务: {latest_user}")
    if latest_response:
        preview = latest_response if len(latest_response) <= 240 else latest_response[:237] + "..."
        lines.append(f"最近返回: {preview}")
    if tools_used:
        lines.append(f"已用工具: {', '.join(str(t) for t in tools_used if str(t).strip())}")
    lines.append("请不要再等待该子代理当前这轮结果。若仍需继续，请基于当前上下文重新规划；如果缺少关键输入，请向用户提出一个简短澄清问题。")
    return "\n".join(lines)


async def _notify_parent_sessions_of_subagent_interrupt(
    sub_session: Session,
    *,
    controller_session_id: str = "",
    auto_replan: bool = True,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    owner = dict(owner_kwargs or {})
    details = _extract_subagent_details(sub_session)
    ancestor_ids = await _collect_session_ancestor_ids(
        sub_session.id,
        owner_kwargs=owner,
    )
    preferred_controller = str(controller_session_id or "").strip()
    if preferred_controller and preferred_controller not in ancestor_ids:
        preferred_controller = ""
    notify_ids: list[str] = []
    for candidate in [preferred_controller, *ancestor_ids]:
        if candidate and candidate != sub_session.id and candidate not in notify_ids:
            notify_ids.append(candidate)

    note = _build_subagent_interrupt_notice(
        sub_session,
        details=details,
        controller_session_id=preferred_controller,
    )
    notified_session_ids: list[str] = []
    for target_session_id in notify_ids:
        target = await SessionManager.get(target_session_id, **owner)
        if not target:
            continue
        await _append_message_to_session(
            target_session_id,
            Message(role="system", content=note, id=str(uuid.uuid4())),
            owner_kwargs=owner,
        )
        notified_session_ids.append(target_session_id)

    replan_session_id = ""
    if preferred_controller and preferred_controller in notified_session_ids:
        replan_session_id = preferred_controller
    elif ancestor_ids:
        replan_session_id = ancestor_ids[-1]
    elif notified_session_ids:
        replan_session_id = notified_session_ids[0]

    replan_queued = False
    if auto_replan and replan_session_id:
        guidance_message = (
            "用户刚刚手动中止了一个子代理回合。"
            "不要再等待该子代理当前这轮结果，也不要假设它会继续返回。"
            "请基于当前上下文重新规划下一步；如果继续执行需要用户补充信息，请直接向用户提出一个简短澄清问题。"
        )
        from fastapi import HTTPException
        from .chat_control_api import _guide_chat_v2
        from .schemas import ChatGuideRequest

        target = await SessionManager.get(replan_session_id, **owner)
        if target is None:
            return {"parent_notified_session_ids": notified_session_ids, "replan_queued": False}
        try:
            await _guide_chat_v2(
                payload=ChatGuideRequest(session_id=target.id, message=guidance_message, mode="queue"),
                message=guidance_message,
                persisted=target,
                runtime=ServerState.sessions.get(target.id),
                session_owner=_session_owner_kwargs_from_session(target),
            )
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            return {
                "parent_notified_session_ids": notified_session_ids,
                "replan_session_id": replan_session_id,
                "replan_queued": False,
                "replan_rejected": "guidance.queue_full",
            }
        replan_queued = True

    return {
        "parent_notified_session_ids": notified_session_ids,
        "replan_session_id": replan_session_id,
        "replan_queued": replan_queued,
    }


def _load_job_registry() -> dict[str, Any]:
    from .tmcp_paths import load_tmcp_job_registry

    return load_tmcp_job_registry()


def _read_job_exitcode(job: dict[str, Any]) -> Optional[int]:
    path = Path(str(job.get("exitcode_file", "") or ""))
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _map_job_runtime_status(job: dict[str, Any]) -> str:
    service_status = str(
        job.get("service_status") or job.get("status") or ""
    ).strip().lower()
    if service_status in {
        "queued",
        "running",
        "completed",
        "failed",
        "timed_out",
        "terminated",
        "cancelled",
    }:
        return service_status
    pid_raw = job.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None else 0
    except Exception:
        pid = 0
    returncode = _read_job_exitcode(job)
    if returncode is not None:
        return "completed" if returncode == 0 else "failed"
    if pid > 0 and _pid_running_local(pid):
        return "running"
    return "terminated"


def _build_workspace_from_submit_input(
    submit_input: dict[str, Any],
    *,
    issue_identifier: str = "",
    session_id: str = "",
    parent_session_id: str = "",
) -> dict[str, Any]:
    workdir = str(submit_input.get("workdir", "") or "").strip()
    workspace_input = submit_input.get("workspace")
    if not isinstance(workspace_input, dict):
        workspace_input = {}

    context_file = ""
    if workdir:
        context_file = str(Path(workdir) / "job_context.json")

    return {
        "job_id": str(workspace_input.get("job_id", "") or ""),
        "process_id": str(workspace_input.get("process_id", "") or ""),
        "run_id": str(submit_input.get("run_id", "") or ""),
        "tool": str(submit_input.get("tool", "") or ""),
        "task_type": str(submit_input.get("task_type", "") or ""),
        "workdir": workdir,
        "log_file": str(workspace_input.get("log_file", "") or ""),
        "exitcode_file": str(workspace_input.get("exitcode_file", "") or ""),
        "expected_artifacts": submit_input.get("expected_artifacts", [])
        if isinstance(submit_input.get("expected_artifacts"), list)
        else [],
        "context_file": str(workspace_input.get("context_file", "") or context_file),
        "session_id": str(workspace_input.get("session_id", "") or session_id),
        "parent_session_id": str(
            workspace_input.get("parent_session_id", "") or parent_session_id
        ),
        "issue_identifier": str(
            workspace_input.get("issue_identifier", "") or issue_identifier
        ),
        "preview_ports": _normalize_preview_ports_local(
            workspace_input.get("preview_ports")
        ),
        "preview_urls": [],
        "preview_entries": workspace_input.get("preview_entries")
        if isinstance(workspace_input.get("preview_entries"), list)
        else [],
        "artifact_source": workspace_input.get("artifact_source")
        if isinstance(workspace_input.get("artifact_source"), dict)
        else {},
        "host": str(
            workspace_input.get("host")
            or workspace_input.get("execution_host")
            or socket.gethostname()
        ),
        "execution_host": str(
            workspace_input.get("execution_host") or socket.gethostname()
        ),
        "machine_label": str(
            workspace_input.get("machine_label") or socket.gethostname()
        ),
        "input_snapshot": workspace_input.get("input_snapshot")
        if isinstance(workspace_input.get("input_snapshot"), dict)
        else {},
        "input_summary": {},
        "resume_supported": bool(workspace_input.get("resume_supported", False)),
        "resume_command": str(workspace_input.get("resume_command", "") or ""),
        "retry_of": str(workspace_input.get("retry_of", "") or ""),
        "resume_of": str(workspace_input.get("resume_of", "") or ""),
        "review_lineage": workspace_input.get("review_lineage")
        if isinstance(workspace_input.get("review_lineage"), dict)
        else {},
        "timeline_ref": workspace_input.get("timeline_ref")
        if isinstance(workspace_input.get("timeline_ref"), dict)
        else {},
        "submitted_at": str(workspace_input.get("submitted_at", "") or ""),
        "started_at": str(workspace_input.get("started_at", "") or ""),
        "completed_at": str(workspace_input.get("completed_at", "") or ""),
        "cancelled_at": str(workspace_input.get("cancelled_at", "") or ""),
    }


def _hydrate_workspace_from_job_files(
    workspace: dict[str, Any]
) -> dict[str, Any]:
    # 兼容旧调用点，实际逻辑委托给 job_workspace 实现。
    return _hydrate_workspace_from_job_files_impl(workspace)


def _resolve_job_artifact_access(
    workspace: dict[str, Any],
    *,
    root: str = "",
    path: str = "",
) -> tuple[dict[str, Any], Optional[Path], str]:
    artifact_source = workspace.get("artifact_source")
    artifact_entries = (
        workspace.get("artifact_entries")
        if isinstance(workspace.get("artifact_entries"), list)
        else []
    )
    if (
        not isinstance(artifact_source, dict)
        or (
            not str(artifact_source.get("root", "") or "").strip()
            and not str(artifact_source.get("remote_base_url", "") or "").strip()
        )
    ):
        artifact_source = _build_workspace_artifact_source(
            workspace,
            artifact_entries,
        )
    else:
        artifact_source = dict(artifact_source)

    default_root = str(artifact_source.get("root", "") or "").strip()
    requested_root = str(root or "").strip()
    remote_base_url = str(artifact_source.get("remote_base_url", "") or "").strip().rstrip("/")
    if requested_root and default_root:
        if remote_base_url:
            if requested_root != default_root:
                raise HTTPException(403, "Artifact root is outside the allowed source")
        else:
            requested_path = Path(requested_root).expanduser().resolve(strict=False)
            default_path = Path(default_root).expanduser().resolve(strict=False)
            if requested_path != default_path:
                raise HTTPException(403, "Artifact root is outside the allowed source")

    effective_root = default_root or requested_root
    if not effective_root:
        raise HTTPException(404, "Artifact root is not available")
    if requested_root and not default_root and not remote_base_url:
        raise HTTPException(404, "Artifact root is not available")

    if remote_base_url:
        return artifact_source, None, effective_root

    root_path = Path(effective_root).resolve(strict=False)
    if not root_path.exists():
        raise HTTPException(404, f"Artifact root not found: {effective_root}")

    target_path = root_path
    if path:
        target_path = (root_path / path).resolve(strict=False)
    if not _path_within_root(target_path.as_posix(), root_path.as_posix()):
        raise HTTPException(403, "Artifact path is outside the allowed root")
    return artifact_source, target_path, root_path.as_posix()


async def _proxy_remote_artifact_download(
    remote_base_url: str,
    endpoint: str,
    params: dict[str, Any],
) -> Response:
    if not remote_base_url:
        raise HTTPException(502, "Remote artifact base URL is not configured")
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{remote_base_url.rstrip('/')}{endpoint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=filtered) as resp:
                body = await resp.read()
                headers: dict[str, str] = {}
                for header_name in ("Content-Type", "Content-Disposition"):
                    value = resp.headers.get(header_name)
                    if value:
                        headers[header_name.lower()] = value
                if resp.status >= 400:
                    detail = body.decode("utf-8", errors="replace")[:400]
                    raise HTTPException(resp.status, detail or "Remote artifact download failed")
                return Response(content=body, status_code=resp.status, headers=headers)
    except HTTPException:
        raise
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Remote artifact download failed: {exc}")


async def _submit_job_action(
    *,
    job_id: str,
    action: str,
    reuse_issue: bool = True,
    reason: str = "",
    expected_protein_run_id: str = "",
    expected_control_workdir: str = "",
) -> dict[str, Any]:
    registry = _load_job_registry()
    job = registry.get(job_id)
    if not isinstance(job, dict):
        raise HTTPException(404, f"job not found: {job_id}")

    workspace = _build_job_workspace_summary(job_id, job)
    issue = _find_related_symphony_issue(workspace)
    if expected_protein_run_id:
        try:
            from ..workspace.async_protein_run import extract_link

            protein_run_link = extract_link(workspace, issue or {})
        except Exception:
            protein_run_link = {}
        linked_run_id = str(
            protein_run_link.get("workspace_id")
            or protein_run_link.get("run_id")
            or ""
        ).strip()
        linked_control_workdir = str(
            protein_run_link.get("control_workdir") or ""
        ).strip()
        if linked_run_id != str(expected_protein_run_id).strip():
            raise HTTPException(409, "job 与当前 Protein Run 的关联校验失败")
        if expected_control_workdir:
            try:
                same_control_workdir = (
                    Path(linked_control_workdir).expanduser().resolve(strict=False)
                    == Path(expected_control_workdir).expanduser().resolve(strict=False)
                )
            except (OSError, RuntimeError, ValueError):
                same_control_workdir = False
            if not same_control_workdir:
                raise HTTPException(409, "job 与当前 Protein Run 的 Workspace 关联校验失败")
    lifecycle_status = _normalize_lifecycle_status(workspace, issue)
    if lifecycle_status in {"running", "review_pending", "queued"}:
        raise HTTPException(
            400,
            f"任务当前状态是 {lifecycle_status}，仍在执行/排队/审核中，不能直接{ '恢复' if action == 'resume' else '重试' }。",
        )

    if action == "resume" and not (
        bool(workspace.get("resume_supported", False))
        or str(workspace.get("resume_command", "") or "").strip()
    ):
        raise HTTPException(400, "Resume is not supported for this job")

    submit_input = _build_submit_input_from_job_and_issue(job_id, job, issue)
    if not submit_input:
        raise HTTPException(
            400,
            "当前任务缺少原始 submit_job 输入，通常是旧任务记录不完整，无法自动重试。",
        )

    snapshot_dir = _snapshot_job_state(
        job_id=job_id,
        workspace=workspace,
        issue=issue,
        reason=action,
    )
    lineage_key = "resume_of" if action == "resume" else "retry_of"
    payload = _build_resubmission_input(
        submit_input=submit_input,
        source_workspace=workspace,
        lineage_key=lineage_key,
        lineage_value=job_id,
    )
    payload_workspace = payload.get("workspace")
    if not isinstance(payload_workspace, dict):
        payload_workspace = {}
    payload_workspace = dict(payload_workspace)
    payload_workspace["timeline_ref"] = {
        "source_job_id": job_id,
        "action": action,
        "snapshot_dir": snapshot_dir,
        "issue_identifier": str(
            payload_workspace.get("issue_identifier")
            or workspace.get("issue_identifier")
            or (issue.get("identifier") if isinstance(issue, dict) else "")
            or ""
        ),
    }
    if reason:
        payload_workspace["last_action_reason"] = reason
    payload["workspace"] = payload_workspace
    if action == "resume":
        resume_command = str(workspace.get("resume_command", "") or "").strip()
        if resume_command:
            payload["command"] = resume_command

    await MCPManager.initialize(ServerState.get_workdir())
    try:
        raw_result = await MCPManager.call_tool(
            "job-protocol-mcp",
            "submit_job",
            {"input": payload},
        )
    except Exception as exc:
        text = str(exc)
        if "MCP server 'job-protocol-mcp' not found" in text:
            raise HTTPException(
                502,
                "当前 tcode 后端没有连上 job-protocol-mcp，所以不能直接重试。请先确认本机 mcp.json 里已连接 job-protocol-mcp。",
            )
        if "Direct MCP call is disabled by policy" in text:
            raise HTTPException(
                502,
                "当前环境禁止直接调用 job-protocol-mcp，暂时不能直接重试。需要调整直连策略或改成经 Symphony 重新提交。",
            )
        raise HTTPException(502, f"重试提交失败: {text}")
    result = _extract_mcp_payload_dict(raw_result)
    new_job_id = str(result.get("job_id", "") or "").strip()
    if not new_job_id:
        raise HTTPException(502, "Resubmission succeeded but no new job_id was returned")

    new_workspace = _get_job_workspace_or_404(new_job_id)
    now = _utc_now_iso()
    updated_issue: Optional[dict[str, Any]] = None
    refresh_ok = False
    notified_session_ids: list[str] = []

    if issue and reuse_issue:
        store = _load_symphony_memory_issue_store()
        issues = store.get("issues", [])
        if isinstance(issues, list):
            for idx, candidate in enumerate(issues):
                if not isinstance(candidate, dict):
                    continue
                same_id = str(candidate.get("id", "") or "") == str(issue.get("id", "") or "")
                same_identifier = str(candidate.get("identifier", "") or "") == str(issue.get("identifier", "") or "")
                if not (same_id or same_identifier):
                    continue
                history = candidate.get("job_history")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "job_id": job_id,
                        "action": action,
                        "snapshot_dir": snapshot_dir,
                        "updated_at": now,
                        "status": lifecycle_status,
                    }
                )
                candidate["job_history"] = history
                candidate["workspace"] = {
                    **new_workspace,
                    "issue_identifier": str(candidate.get("identifier", "") or new_workspace.get("issue_identifier", "") or ""),
                }
                candidate["state"] = "In Progress"
                candidate["updated_at"] = now
                candidate["execution_started_at"] = str(
                    new_workspace.get("submitted_at") or new_workspace.get("started_at") or now
                )
                candidate["cancelled_at"] = ""
                candidate["execution_pid"] = None
                candidate["cancel_actions"] = []
                candidate["error_summary"] = ""
                candidate["result_summary"] = f"已{ '恢复' if action == 'resume' else '重试' }并提交新任务: {new_job_id}"
                issues[idx] = candidate
                updated_issue = candidate
                break
            store["issues"] = issues
            _save_symphony_memory_issue_store(store)
            refresh_ok = await _trigger_symphony_refresh()
    elif issue and not reuse_issue:
        store = _load_symphony_memory_issue_store()
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        identifier = f"LOCAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        updated_issue = {
            **issue,
            "id": f"local-{uuid.uuid4().hex[:10]}",
            "identifier": identifier,
            "title": str(issue.get("title", "") or f"{workspace.get('tool', 'job')} {action}").strip(),
            "state": "In Progress",
            "created_at": now,
            "updated_at": now,
            "workspace": {
                **new_workspace,
                "issue_identifier": identifier,
            },
            "execution_started_at": str(
                new_workspace.get("submitted_at") or new_workspace.get("started_at") or now
            ),
            "cancelled_at": "",
            "execution_pid": None,
            "cancel_actions": [],
            "error_summary": "",
            "result_summary": f"已创建新的{ '恢复' if action == 'resume' else '重试' }任务: {new_job_id}",
            "job_history": [
                {
                    "job_id": job_id,
                    "action": action,
                    "snapshot_dir": snapshot_dir,
                    "updated_at": now,
                    "status": lifecycle_status,
                }
            ],
        }
        issues.append(updated_issue)
        store["issues"] = issues
        _save_symphony_memory_issue_store(store)
        refresh_ok = await _trigger_symphony_refresh()

    try:
        notified_session_ids = await _append_manual_job_action_session_note(
            source_workspace=workspace,
            new_workspace={
                **new_workspace,
                "issue_identifier": str(
                    (updated_issue or {}).get("identifier", "")
                    or new_workspace.get("issue_identifier", "")
                    or ""
                ),
            },
            action=action,
            snapshot_dir=snapshot_dir,
            issue=updated_issue or issue,
            reason=reason,
        )
    except Exception:
        notified_session_ids = []

    source_task_id = str(
        workspace.get("task_id")
        or (issue.get("id") if isinstance(issue, dict) else "")
        or ""
    ).strip()
    if source_task_id:
        try:
            SharedTracker.append_event(
                task_id=source_task_id,
                kind="retry_scheduled" if action == "retry" else "resume_scheduled",
                source="app",
                payload={
                    "source_job_id": job_id,
                    "new_job_id": new_job_id,
                    "snapshot_dir": snapshot_dir,
                    "reason": reason,
                    "reuse_issue": reuse_issue,
                },
                workdir=ServerState.get_workdir(),
            )
        except Exception:
            pass

    return {
        "status": "ok",
        "action": action,
        "source_job_id": job_id,
        "job_id": new_job_id,
        "snapshot_dir": snapshot_dir,
        "workspace": new_workspace,
        "refresh_triggered": refresh_ok,
        "issue": updated_issue,
        "submit_input": payload,
        "notified_session_ids": notified_session_ids,
    }


async def _job_action_diagnostics(
    job_id: str,
    workspace: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    lifecycle_status = _normalize_lifecycle_status(workspace, issue)
    if not str(job_id or "").strip():
        missing_reason = "当前任务还没有真正生成 job_id，通常仍停留在 Todo 或人工审核阶段。"
        action_policy = build_action_policy(
            lifecycle_status=lifecycle_status,
            job_id="",
            has_submit_input=False,
            retry_reason=missing_reason,
            resume_reason=missing_reason,
            review_status=str((workspace.get("review_lineage") or {}).get("status", "") or "")
            if isinstance(workspace.get("review_lineage"), dict)
            else "",
        )
        return {
            "retry_available": False,
            "retry_reason": missing_reason,
            "resubmit_available": False,
            "resubmit_reason": missing_reason,
            "resume_available": False,
            "resume_reason": missing_reason,
            "action_policy": action_policy,
        }

    active_reason = ""
    if lifecycle_status in {"running", "review_pending", "queued", "retrying"}:
        active_reason = (
            f"当前任务状态是 {lifecycle_status}，只有终态任务才能重试。"
        )

    registry = _load_job_registry()
    job = registry.get(job_id)
    submit_input = (
        _build_submit_input_from_job_and_issue(job_id, job, issue)
        if isinstance(job, dict)
        else {}
    )
    submit_reason = ""
    if not submit_input:
        submit_reason = "当前任务缺少原始 submit_job 输入，通常是旧任务记录不完整，无法自动重试。"

    direct_reason = ""
    try:
        await MCPManager.initialize(ServerState.get_workdir())
        statuses = await MCPManager.get_status()
        job_protocol_status = statuses.get("job-protocol-mcp")
        if job_protocol_status is None:
            direct_reason = "当前 tcode 后端没有连接到 job-protocol-mcp，无法直接重试。"
        elif str(job_protocol_status.status or "").strip().lower() != "connected":
            direct_reason = (
                "当前 tcode 后端连接 job-protocol-mcp 失败"
                + (
                    f": {job_protocol_status.error}"
                    if str(job_protocol_status.error or "").strip()
                    else ""
                )
            )
    except Exception as exc:
        direct_reason = f"当前无法检查 job-protocol-mcp 连接状态: {exc}"

    retry_reason = active_reason or submit_reason or direct_reason
    resume_supported = bool(workspace.get("resume_supported", False)) or bool(
        str(workspace.get("resume_command", "") or "").strip()
    )
    resume_reason = (
        active_reason
        or submit_reason
        or direct_reason
        or (
            ""
            if resume_supported
            else "当前执行层没有声明 resume 能力，所以只能重试，不能恢复。"
        )
    )

    action_policy = build_action_policy(
        lifecycle_status=lifecycle_status,
        job_id=job_id,
        has_submit_input=bool(submit_input),
        resume_supported=resume_supported,
        resume_command=str(workspace.get("resume_command", "") or ""),
        retry_reason=retry_reason,
        resume_reason=resume_reason,
        review_status=str((workspace.get("review_lineage") or {}).get("status", "") or "")
        if isinstance(workspace.get("review_lineage"), dict)
        else "",
    )

    return {
        "retry_available": not bool(retry_reason),
        "retry_reason": retry_reason,
        "resubmit_available": not bool(retry_reason),
        "resubmit_reason": retry_reason,
        "resume_available": not bool(resume_reason),
        "resume_reason": resume_reason,
        "action_policy": action_policy,
    }


async def _append_manual_job_action_session_note(
    *,
    source_workspace: dict[str, Any],
    new_workspace: dict[str, Any],
    action: str,
    snapshot_dir: str = "",
    issue: Optional[dict[str, Any]] = None,
    reason: str = "",
) -> list[str]:
    session_id = str(source_workspace.get("session_id", "") or "").strip()
    parent_session_id = str(source_workspace.get("parent_session_id", "") or "").strip()
    target_session_ids: list[str] = []
    for candidate in [parent_session_id, session_id]:
        if candidate and candidate not in target_session_ids:
            target_session_ids.append(candidate)

    action_text = {
        "retry": "手动重试了异步任务",
        "resume": "手动从当前结果恢复了异步任务",
    }.get(action, "手动重新提交了异步任务")
    action_label = {
        "retry": "retry",
        "resume": "resume",
    }.get(action, "resubmit")

    source_job_id = str(source_workspace.get("job_id", "") or "").strip()
    new_job_id = str(new_workspace.get("job_id", "") or "").strip()
    source_issue = str(
        source_workspace.get("issue_identifier")
        or (issue.get("identifier") if isinstance(issue, dict) else "")
        or ""
    ).strip()
    new_issue = str(new_workspace.get("issue_identifier", "") or "").strip()
    tool_name = (
        f"{new_workspace.get('tool')}.{new_workspace.get('task_type')}"
        if str(new_workspace.get("tool", "") or "").strip() and str(new_workspace.get("task_type", "") or "").strip()
        else str(new_workspace.get("tool", "") or new_workspace.get("task_type", "") or "").strip()
    )

    lines = [
        f"[JobAction] {action_label}",
        f"用户刚刚{action_text}。",
        f"原 job_id: {source_job_id or '-'}",
        f"新 job_id: {new_job_id or '-'}",
    ]
    if tool_name:
        lines.append(f"工具: {tool_name}")
    if source_issue:
        lines.append(f"原 issue: {source_issue}")
    if new_issue and new_issue != source_issue:
        lines.append(f"新 issue: {new_issue}")
    if str(new_workspace.get("workdir", "") or "").strip():
        lines.append(f"工作目录: {str(new_workspace.get('workdir', '') or '').strip()}")
    if snapshot_dir:
        lines.append(f"快照目录: {snapshot_dir}")
    if reason:
        lines.append(f"操作原因: {reason}")
    lines.append("后续如果继续本会话，请把新的 job_id 视为当前有效的异步运行。")

    content = "\n".join(lines)
    notified: list[str] = []
    now_ts = datetime.now().timestamp()
    for target_session_id in target_session_ids:
        session = await SessionManager.get(target_session_id)
        if not session:
            continue
        msg = Message(
            role="system",
            content=content,
            id=str(uuid.uuid4()),
        )
        session.messages.append(msg)
        await SessionManager.update(
            target_session_id,
            messages=list(session.messages),
            last_active_at=now_ts,
        )
        notified.append(target_session_id)

    return notified


def _build_review_resume_message(
    review: dict[str, Any],
    decision: str = "approved",
    approved_action_result: Optional[dict[str, Any]] = None,
) -> str:
    identifier = str(review.get("identifier", "") or "")
    reason = str(review.get("reason", "") or "")
    comments = str(review.get("comments", "") or "").strip()
    resume_instruction = str(review.get("resume_instruction", "") or "").strip()
    override = review.get("approved_tool_args_override")
    rejection_reason = str(review.get("rejection_reason", "") or "").strip()

    is_rejected = str(decision or "").strip().lower() == "rejected"
    if is_rejected:
        sections = [
            f"Human review rejected for {identifier}.",
            f"Original review reason: {reason}",
        ]
        if rejection_reason:
            sections.append(f"Rejection reason: {rejection_reason}")
    else:
        sections = [
            f"Human review approved for {identifier}.",
            f"Original review reason: {reason}",
        ]

    if comments:
        sections.append(f"Reviewer feedback: {comments}")
    if resume_instruction:
        sections.append(f"Resume instruction: {resume_instruction}")
    if (not is_rejected) and isinstance(override, dict) and override:
        sections.append(
            "Reviewer-provided parameter override (apply when rerunning blocked step):\n"
            + json.dumps(override, ensure_ascii=False, indent=2)
        )
    if is_rejected:
        sections.append(
            "This reviewed action is rejected. Do NOT execute the rejected tool call or equivalent "
            "high-risk operation until user provides revised inputs."
        )
        sections.append(
            "Respond to user with a concise cancellation/rejection update and propose safe next step options "
            "based on reviewer feedback."
        )
    else:
        action_result = (
            approved_action_result
            if isinstance(approved_action_result, dict)
            else {}
        )
        action_status = str(action_result.get("status", "") or "").strip().lower()
        if action_status in {"queued", "deduped"}:
            local_identifier = str(
                action_result.get("identifier", "") or ""
            ).strip()
            execution_channel = str(
                action_result.get("execution_channel", "") or ""
            ).strip()
            sections.append(
                "The approved blocked action has now been executed deterministically by the server.\n"
                f"Local queue identifier: {local_identifier or '-'}\n"
                f"Execution channel: {execution_channel or '-'}\n"
                "This confirms local asynchronous queue acceptance only. It does not yet prove that the "
                "remote target returned a job_id/process_id. Do NOT call the blocked submit tool again, "
                "and do NOT invent or infer any identifier. Report the exact local identifier above and "
                "continue only with remaining workflow steps. For any later status check, call "
                "`mcp_job-protocol-mcp_get_job_status` with "
                f"`{{\"input\": {{\"local_request_id\": \"{local_identifier}\"}}}}`."
            )
        elif action_status == "revision_requested":
            tool_server = str(review.get("tool_server", "") or "").strip()
            tool_name = str(review.get("tool_name", "") or "").strip()
            tool_args = review.get("tool_args")
            if not isinstance(tool_args, dict):
                tool_args = {}
            sections.append(
                "The original blocked action has NOT been executed because the reviewer provided revision "
                "guidance without an exact parameter override. Treat the reviewer feedback and resume "
                "instruction as requirements, revise the workflow or arguments before calling the tool, and "
                "do not submit the unchanged original call when it conflicts with that guidance.\n"
                f"Blocked tool: `{tool_server}.{tool_name}`\n"
                "Original arguments (use only as a starting point):\n"
                + json.dumps(tool_args, ensure_ascii=False, indent=2)
                + "\nA materially revised high-risk call may require a new human review so the concrete "
                "parameters can be verified."
            )
        elif action_status == "failed":
            sections.append(
                "The server attempted the approved blocked action, but it failed before queue acceptance.\n"
                f"Exact error: {str(action_result.get('error', '') or 'unknown error')}\n"
                "Do NOT claim that the task was submitted and do NOT invent a LOCAL/job/process identifier. "
                "Report this failure exactly."
            )
        else:
            tool_server = str(review.get("tool_server", "") or "").strip()
            tool_name = str(review.get("tool_name", "") or "").strip()
            tool_args = review.get("tool_args")
            if not isinstance(tool_args, dict):
                tool_args = {}
            sections.append(
                "The blocked tool action did not execute before review. Before reporting success, you MUST "
                f"rerun the approved tool action `{tool_server}.{tool_name}` with these exact arguments:\n"
                + json.dumps(tool_args, ensure_ascii=False, indent=2)
                + "\nDo not invent or infer LOCAL/job/process identifiers; report only values returned by the tool."
            )
        sections.append(
            "Continue the same workflow with full previous context. "
            "If reviewer asks to rerun an earlier step (for example step B), rerun it first, "
            "then continue remaining steps in order."
        )
        sections.append(
            "This human review approval is the explicit confirmation for the previously blocked action. "
            "Do NOT ask the user to confirm the same approved action again, and do NOT call the question tool "
            "for the same confirmation unless the reviewer explicitly requested new input or the parameters "
            "have materially changed."
        )
    return "\n\n".join(sections)


async def _enqueue_approved_proxy_review(
    review: dict[str, Any],
    *,
    session_id: str,
    agent_name: str,
    workdir: str,
    owner: dict[str, str],
    admission=None,
) -> Optional[dict[str, Any]]:
    review_data = review.get("review_data")
    if not isinstance(review_data, dict):
        return None
    if str(review_data.get("proxy_source", "") or "").strip() != "job_protocol_submit":
        return None

    if (
        str(review.get("execution_disposition", "") or "").strip().lower()
        == "revise_before_execute"
    ):
        return {
            "status": "revision_requested",
            "reason": "reviewer feedback must be applied before executing the blocked action",
        }

    server_name = str(review.get("tool_server", "") or "").strip()
    tool_name = str(review.get("tool_name", "") or "").strip()
    tool_args = review.get("approved_tool_args_override")
    if not isinstance(tool_args, dict) or not tool_args:
        tool_args = review.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}
    if not server_name or not tool_name or (not tool_args and 'custom_mcp_read_grants' not in review_data):
        return {
            "status": "failed",
            "error": "approved review is missing target server, tool, or arguments",
        }

    review_workspace = (
        review_data.get("workspace")
        if isinstance(review_data.get("workspace"), dict)
        else {}
    )
    trace_context = {}
    for source in (
        review_data.get("tool_trace_context"),
        review_workspace.get("tool_trace_context"),
        review_data,
        review_workspace,
    ):
        if not isinstance(source, dict):
            continue
        for key in (
            "workflow_run_id",
            "workflow_stage_id",
            "workflow_template_key",
            "workflow_owner_session_id",
            "workflow_actor_type",
            "parent_session_id",
            "subagent_session_id",
            "worker_name",
        ):
            value = str(source.get(key, "") or "").strip()
            if value and not trace_context.get(key):
                trace_context[key] = value
    context = ToolContext(
        session_id=session_id,
        message_id=f"review-resume:{str(review.get('identifier', '') or '')}",
        agent=agent_name or "build",
        abort=asyncio.Event(),
        call_id=f"review-resume:{str(review.get('id', '') or '')}",
        extra={
            "workdir": workdir,
            "workspace_root": workdir,
            "parent_session_id": str(
                review_data.get("parent_session_id", "") or ""
            ).strip(),
            "tenant_id": str(owner.get("tenant_id", "") or "").strip(),
            "user_id": str(owner.get("user_id", "") or "").strip(),
            "workspace_id": str(owner.get("workspace_id", "") or "").strip(),
            "async_execution_channel": str(
                review_workspace.get("execution_channel", "") or ""
            ).strip(),
            "tool_trace_context": trace_context,
            **({"request_id": str(review_data.get("origin_request_id") or "")}
               if review_data.get("goal_computation") else {}),
        },
    )

    try:
        from ..mcp.artifact_boundary import prepare_mcp_artifact_arguments
        from ..mcp._symphony import (
            enqueue_symphony_local_issue,
            validate_routed_request,
        )
        if isinstance(review_data.get("boltz_research"), dict):
            context.extra["boltz_research"] = review_data["boltz_research"]

        if "custom_mcp_read_grants" in review_data:
            from ..mcp.custom_targets import restore_review_inputs
            await restore_review_inputs(context, server_name, tool_name, tool_args, review_data['custom_mcp_read_grants'],
                                        review_data.get('custom_mcp_read_bindings'))
        if "input_file_contract" in review_data:
            from ..workspace.input_resources import pin_input_file_contract
            pin_input_file_contract(context, server_name, tool_name, review_data["input_file_contract"])
        tool_args, upload_bindings = await prepare_mcp_artifact_arguments(
            server_name, tool_name, tool_args, context,
        )
        if "custom_mcp_read_grants" in review_data:
            context._custom_mcp_upload_bindings = {(server_name, tool_name): upload_bindings}
        validation_error = validate_routed_request(
            server_name=server_name,
            tool_name=tool_name,
            normalized_args=tool_args,
            context=context,
        )
        if validation_error:
            return {"status": "failed", "error": validation_error}

        from ..runtime.async_resume import active_origin
        task_context = dict(review_data.get('task_context') or {})
        origin_token = active_origin.set(task_context or None)
        try:
            queued = await enqueue_symphony_local_issue(
                server_name=server_name,
                tool_name=tool_name,
                normalized_args=tool_args,
                context=context,
                **({'admission': admission} if admission is not None else {}),
            )
        finally:
            active_origin.reset(origin_token)
        queued_identifier = str(queued.get("identifier", "") or "").strip()
        if queued_identifier:
            from ..runtime.async_watcher import (
                register as register_async_watcher,
                start_watcher,
            )

            register_async_watcher(
                job_identifier=queued_identifier,
                session_id=session_id,
                target_server=server_name,
                target_tool=tool_name,
                workdir=workdir,
                task_context=task_context,
                origin_turn_id=str(task_context.get('message_id') or ''),
            )
            await start_watcher(workdir=workdir)
        return queued
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _extract_async_issue_resume_data(issue: dict[str, Any]) -> dict[str, str]:
    resume_data = issue.get("resume_data")
    if not isinstance(resume_data, dict):
        resume_data = {}

    description = str(issue.get("description", "") or "")

    def _pick(key: str, pattern: str) -> str:
        value = str(resume_data.get(key, "") or "").strip()
        if value:
            return value
        match = re.search(pattern, description)
        if match:
            return str(match.group(1) or "").strip()
        return ""

    return {
        "session_id": _pick("session_id", r"session_id:\s*([^\n]+)"),
        "parent_session_id": _pick("parent_session_id", r"parent_session_id:\s*([^\n]+)"),
        "agent_name": _pick("agent_name", r"agent_name:\s*([^\n]+)"),
        "target_server": _pick("target_server", r"target_server:\s*([^\n]+)"),
        "target_tool": _pick("target_tool", r"target_tool:\s*([^\n]+)"),
        "origin_turn_id": _pick("origin_turn_id", r"origin_turn_id:\s*([^\n]+)"),
        "origin_request_id": _pick("origin_request_id", r"origin_request_id:\s*([^\n]+)"),
        "continuation_policy": _pick(
            "continuation_policy",
            r"continuation_policy:\s*([^\n]+)",
        ),
        "workflow_run_id": _pick("workflow_run_id", r"workflow_run_id:\s*([^\n]+)"),
    }


def _build_async_issue_resume_message(issue: dict[str, Any], *, include_result: bool = True) -> str:
    resume_data = _extract_async_issue_resume_data(issue)
    identifier = str(issue.get("identifier", "") or "").strip()
    title = str(issue.get("title", "") or "").strip()
    state = str(issue.get("state", "") or "").strip()
    raw_result_summary = str(issue.get("result_summary", "") or "").strip()
    parsed_result_summary = _extract_first_json_object(raw_result_summary)
    if parsed_result_summary is not None:
        result_summary = json.dumps(parsed_result_summary, ensure_ascii=False)
    else:
        result_summary = raw_result_summary
    target_server = resume_data.get("target_server", "")
    target_tool = resume_data.get("target_tool", "")

    sections = [
        f"Async Symphony issue finished: {identifier or 'unknown-identifier'}.",
        f"Title: {title or 'unknown-title'}",
        f"Final state: {state or 'unknown'}",
    ]
    if target_server or target_tool:
        sections.append(
            f"Original async action: {target_server or 'unknown-server'}.{target_tool or 'unknown-tool'}"
        )
    if include_result and result_summary:
        sections.append("Returned result summary:\n" + result_summary)

    mcp_result = _read_async_issue_mcp_result(issue) if include_result else ""
    if mcp_result:
        sections.append("Full MCP result (source of truth):\n" + mcp_result)

    sections.append(
        "Continue the same conversation using full previous context. "
        "Use any previously loaded skill constraints and project rules when composing the user-facing reply."
    )
    sections.append(
        "Treat the returned result summary above as the completed async job output. "
        "Do NOT resubmit the same async job, and do NOT ask the user to repeat the same request unless the result is missing critical information."
    )
    sections.append(
        "Reply to the user now with the completed outcome, keeping the response aligned with the original workflow and skill instructions."
    )
    sections.append(
        "The reply must be self-contained. Never say 'same as above', 'as mentioned above', "
        "or merely confirm completion. Restate the important result details in the user's language, "
        "including run/job identifiers, output paths, requested candidate counts, and structured "
        "candidate values such as top_variants when present."
    )

    return "\n\n".join(sections)


def _read_async_issue_mcp_result(
    issue: dict[str, Any],
    *,
    text_limit: int = 24000,
    parse_limit_bytes: int = 2 * 1024 * 1024,
) -> str:
    """读取异步任务的完整 MCP 结果，供原会话生成自包含最终回答。"""
    workspace = issue.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    job_ref = workspace.get("job_ref")
    if not isinstance(job_ref, dict):
        job_ref = {}

    candidates: list[Path] = []

    def _add_workdir(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        path = Path(text).expanduser()
        result_path = path if path.name == "mcp_result.json" else path / "mcp_result.json"
        if result_path not in candidates:
            candidates.append(result_path)

    _add_workdir(job_ref.get("workdir"))
    _add_workdir(workspace.get("workdir"))
    context_file = str(job_ref.get("context_file") or workspace.get("context_file") or "").strip()
    if context_file:
        _add_workdir(Path(context_file).expanduser().parent)

    for path in candidates:
        try:
            if not path.is_file():
                continue
            if path.stat().st_size <= parse_limit_bytes:
                raw = path.read_text(encoding="utf-8", errors="replace")
                payload = json.loads(raw)
                rendered = json.dumps(payload, ensure_ascii=False, indent=2)
            else:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    rendered = handle.read(text_limit + 1)
            if len(rendered) > text_limit:
                return rendered[: text_limit - 80].rstrip() + "\n...[mcp_result truncated]"
            return rendered
        except Exception:
            continue
    return ""


def _build_async_issue_resume_token(issue: dict[str, Any]) -> str:
    workspace = issue.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    job_ref = workspace.get("job_ref")
    if not isinstance(job_ref, dict):
        job_ref = {}
    payload = {
        "identifier": str(issue.get("identifier", "") or "").strip(),
        "state": str(issue.get("state", "") or "").strip().lower(),
        "job_id": str(
            workspace.get("job_id")
            or job_ref.get("job_id")
            or ""
        ).strip(),
        "process_id": str(
            workspace.get("process_id")
            or job_ref.get("process_id")
            or ""
        ).strip(),
        "run_id": str(workspace.get("run_id") or job_ref.get("run_id") or "").strip(),
        "retry_of": str(workspace.get("retry_of") or "").strip(),
        "resume_of": str(workspace.get("resume_of") or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "v2:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _resume_session_after_async_issue(
    issue: dict[str, Any],
    *,
    workdir: str = "",
) -> dict[str, Any]:
    resume_data = _extract_async_issue_resume_data(issue)
    child_session_id = str(resume_data.get("session_id", "") or "").strip()
    parent_session_id = str(resume_data.get("parent_session_id", "") or "").strip()

    if not child_session_id and not parent_session_id:
        return {
            "started": False,
            "status": "skipped",
            "reason": "missing_session_id",
        }

    if not parent_session_id and child_session_id:
        try:
            child_session = await SessionManager.get(child_session_id)
            parent_session_id = str(getattr(child_session, "parent_id", "") or "").strip()
        except Exception:
            parent_session_id = ""

    # Prefer resuming in parent session so team-manager conversations receive
    # the completed async summary directly.
    session_id = parent_session_id or child_session_id

    if ServerState.is_running(session_id):
        return {
            "started": False,
            "status": "skipped",
            "reason": "session_already_running",
            "session_id": session_id,
            "target_session_id": session_id,
            "child_session_id": child_session_id,
            "parent_session_id": parent_session_id,
        }

    persisted_root = await SessionManager.get(session_id)
    from .session_lifecycle import _SESSION_DELETE_PENDING
    if persisted_root is None or session_id in _SESSION_DELETE_PENDING:
        return {'started': False, 'status': 'skipped', 'reason': 'session_deleted_or_deleting', 'session_id': session_id}
    from .goal_mode import owns_async_result
    if owns_async_result(persisted_root, issue):
        # Opt-in goal supervision owns wakeups, budgets and pause/stop decisions.
        # Keep ordinary sessions on the pre-existing auto-resume path.
        return {'started': False, 'status': 'skipped', 'reason': 'goal_mode_managed', 'session_id': session_id}
    target_workdir = str(
        getattr(persisted_root, "workdir", "")
        or workdir
        or ServerState.get_workdir()
    ).strip() or ServerState.get_workdir()
    preferred_agent = None
    if session_id == child_session_id:
        preferred_agent = str(resume_data.get("agent_name", "") or "").strip() or None
    if not preferred_agent and persisted_root:
        preferred_agent = (
            str(getattr(persisted_root, "agent_name", "") or "").strip() or None
        )

    ServerState.get_or_create_session(
        session_id,
        preferred_agent,
        workdir=target_workdir,
    )
    agent = ServerState.get_or_create_agent(session_id)
    if persisted_root is not None:
        agent._existing_session = persisted_root

    if (
        (not agent.messages)
        and persisted_root
        and getattr(persisted_root, "messages", None)
    ):
        agent.messages = list(persisted_root.messages)

    from ..runtime.async_resume import active_resume, claim_resume, finish_resume, commit_resume
    owner = {key: getattr(persisted_root, key) for key in ("tenant_id", "user_id", "workspace_id")}
    try:
        resume_scope = claim_resume(persisted_root, str(issue.get("identifier") or ""), uuid.uuid4().hex)
    except ValueError as exc:
        return {"started": False, "status": "failed", "reason": str(exc), "session_id": session_id}
    previous_handlers = (getattr(agent, "_permission_handler", None), getattr(agent, "_question_handler", None))

    async def require_human(*args, **kwargs):
        raise PermissionError("async_resume_requires_human_input")

    agent.set_permission_handler(require_human)
    agent.set_question_handler(require_human)
    resume_token = active_resume.set(resume_scope)
    resume_completed = False
    # The prompt builder injects the validated restoration context exactly once.
    resume_message = _build_async_issue_resume_message(issue, include_result=False)

    try:
        agent.set_next_user_message_context(metadata={"internal": True, "purpose": "async_continuation"})
        ServerState.set_running(session_id, True)
        ServerState.set_turn_task(session_id, asyncio.current_task())
        await MCPManager.initialize(target_workdir)
        await SkillManager.load(workdir=target_workdir)

        result = await agent.run(resume_message)

        # 使用独立展示标记，不改动已被轮次状态引用的消息 ID。
        result.presentation = "async_final"
        result.metadata = {**(getattr(result, "metadata", None) or {}),
                           "async_completion_identifier": resume_scope["identifier"]}

        # 保留独立轮次边界供前端和历史恢复使用，但将内部提示标记为隐藏。
        # 删除这个用户边界会让 async_final 被错误归到用户后续提出的问题下。
        for message in agent.messages:
            if (
                getattr(message, "role", "") == "user"
                and str(getattr(message, "content", "") or "").startswith(
                    "Async Symphony issue finished:"
                )
            ):
                message.presentation = "async_hidden_prompt"

        await SessionManager.update(
            session_id,
            messages=list(agent.messages),
            agent_name=(
                preferred_agent
                or str(
                    ServerState.sessions.get(session_id, {}).get("agent_name", "build")
                )
            ),
            workdir=target_workdir,
            last_active_at=datetime.now().timestamp(),
        )
        await commit_resume(resume_scope, session_id=session_id, **owner)
        resume_completed = True
        return {
            "started": True,
            "status": "completed",
            "session_id": session_id,
            "target_session_id": session_id,
            "child_session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "message_id": getattr(result, "id", None),
        }
    except Exception as e:
        return {
            "started": True,
            "status": "failed",
            "session_id": session_id,
            "target_session_id": session_id,
            "child_session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "error": str(e),
        }
    finally:
        try:
            finish_resume(resume_scope, completed=resume_completed)
        finally:
            active_resume.reset(resume_token)
            agent.set_permission_handler(previous_handlers[0])
            agent.set_question_handler(previous_handlers[1])
        current_turn_task = ServerState.get_turn_task(session_id)
        if current_turn_task is asyncio.current_task():
            ServerState.set_turn_task(session_id, None)
        ServerState.set_running(session_id, False)


async def _process_async_issue_auto_resumes(
    store: dict[str, Any],
    *,
    workdir: str = "",
) -> bool:
    issues = store.get("issues", [])
    if not isinstance(issues, list) or not issues:
        return False

    changed = False
    terminal_states = {"done", "closed", "completed", "failed", "cancelled", "canceled"}
    skip_statuses = {"completed", "failed", "skipped", "notified"}

    for issue in issues:
        if not isinstance(issue, dict):
            continue

        state = str(issue.get("state", "") or "").strip().lower()
        if state not in terminal_states:
            continue

        current_token = _build_async_issue_resume_token(issue)
        auto_status = str(issue.get("auto_resume_status", "pending") or "pending").strip().lower()
        saved_token = str(issue.get("auto_resume_token", "") or "").strip()

        # 旧版本 token 包含 result_summary/updated_at。升级时只迁移已处理记录，
        # 不因 token 算法变化重新唤醒历史会话。
        if auto_status in skip_statuses and not saved_token.startswith("v2:"):
            issue["auto_resume_token"] = current_token
            issue["auto_resume_updated_at"] = _utc_now_iso()
            _save_symphony_memory_issue_store(store, workdir)
            changed = True
            continue

        if saved_token != current_token:
            issue["auto_resume_status"] = "pending"
            issue["auto_resume_result"] = {}
            issue["auto_resume_token"] = current_token
            issue["auto_resume_note_token"] = ""
            issue["auto_resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
            auto_status = "pending"
            _save_symphony_memory_issue_store(store, workdir)
            changed = True

        if auto_status in skip_statuses and saved_token == current_token:
            continue

        resume_data = _extract_async_issue_resume_data(issue)
        from ..runtime.async_completion import normalize_continuation_policy

        continuation_policy = normalize_continuation_policy(
            resume_data.get("continuation_policy"),
            workflow_owned=bool(
                str(resume_data.get("workflow_run_id") or "").strip()
            ),
        )
        if not str(resume_data.get('workflow_run_id') or '').strip():
            # Chat jobs always await the explicit Continue action. A persisted
            # legacy/model-supplied auto flag must not start a new foreground turn.
            continuation_policy = 'notify'
        if continuation_policy != "auto":
            # The AsyncResultWatcher owns collection and the durable completion
            # card. Conversation-level jobs only notify; they never start a new
            # Agent turn from this polling loop.
            issue["auto_resume_status"] = "notified"
            issue["auto_resume_result"] = {
                "started": False,
                "status": "notified",
                "reason": "awaiting_explicit_continuation",
                "continuation_policy": continuation_policy,
            }
            issue["auto_resume_token"] = current_token
            issue["auto_resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
            _save_symphony_memory_issue_store(store, workdir)
            changed = True
            continue

        issue["auto_resume_status"] = "running"
        issue["auto_resume_token"] = current_token
        issue["auto_resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_symphony_memory_issue_store(store, workdir)
        changed = True

        result = await _resume_session_after_async_issue(issue, workdir=workdir)
        result_status = str(result.get("status", "unknown") or "unknown").strip().lower()
        if result_status == "skipped" and str(result.get("reason", "") or "") == "session_already_running":
            # Retry after the foreground turn releases the session. Do not
            # mutate the running Agent's message list from the watcher.
            issue["auto_resume_status"] = "pending"
        else:
            issue["auto_resume_status"] = result_status
        issue["auto_resume_result"] = result
        issue["auto_resume_token"] = current_token
        issue["auto_resume_updated_at"] = datetime.utcnow().isoformat() + "Z"
        _save_symphony_memory_issue_store(store, workdir)

    return changed


async def _run_async_issue_auto_resume_once() -> bool:
    global _ASYNC_ISSUE_AUTO_RESUME_LOCK
    if _ASYNC_ISSUE_AUTO_RESUME_LOCK is None:
        _ASYNC_ISSUE_AUTO_RESUME_LOCK = asyncio.Lock()
    if _ASYNC_ISSUE_AUTO_RESUME_LOCK.locked():
        return False
    async with _ASYNC_ISSUE_AUTO_RESUME_LOCK:
        changed = False
        for workdir in _discover_local_issue_store_workdirs():
            store = _load_symphony_memory_issue_store(workdir)
            if await _process_async_issue_auto_resumes(store, workdir=workdir):
                changed = True
        return changed


async def _async_issue_auto_resume_loop() -> None:
    interval = max(
        1.0,
        float(os.environ.get("TCODE_ASYNC_ISSUE_AUTO_RESUME_INTERVAL_SECONDS", "3")),
    )
    while True:
        try:
            await _run_async_issue_auto_resume_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)


async def _resume_session_after_human_review(
    review: dict[str, Any], decision: str = "approved"
) -> dict[str, Any]:
    review_data = review.get("review_data")
    if not isinstance(review_data, dict):
        review_data = {}
    review_workspace = (
        review_data.get("workspace")
        if isinstance(review_data.get("workspace"), dict)
        else {}
    )
    resume_workdir = str(
        review_data.get("workdir")
        or review_workspace.get("workdir")
        or ServerState.get_workdir()
    ).strip() or ServerState.get_workdir()
    tool_args = review.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}
    input_config = tool_args.get("input_config")
    if not isinstance(input_config, dict):
        input_config = {}

    def _review_owner_value(key: str) -> str:
        for source in (review_data, review_workspace, input_config, tool_args):
            value = str(source.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _infer_owner_from_workdir(path_text: str) -> dict[str, str]:
        inferred: dict[str, str] = {}
        try:
            path = Path(path_text).expanduser().resolve(strict=False)
            workdirs_root = workdir_utils.workspace_storage_root(ServerState.get_workdir())
            try:
                rel = path.relative_to(workdirs_root)
                if len(rel.parts) >= 3:
                    inferred["tenant_id"] = rel.parts[0]
                    inferred["user_id"] = rel.parts[1]
                    inferred["workspace_id"] = rel.parts[2]
            except Exception:
                pass
        except Exception:
            pass
        return inferred

    review_claims = {
        "tenant_id": _review_owner_value("tenant_id"),
        "user_id": _review_owner_value("user_id"),
        "workspace_id": _review_owner_value("workspace_id"),
    }
    inferred_owner = _infer_owner_from_workdir(resume_workdir)
    if inferred_owner.get("workspace_id"):
        review_claims["workspace_id"] = inferred_owner["workspace_id"]
        if inferred_owner.get("user_id"):
            review_claims["user_id"] = inferred_owner["user_id"]
        if inferred_owner.get("tenant_id"):
            review_claims["tenant_id"] = inferred_owner["tenant_id"]
    owner_payload = {
        "tenant_id": review_claims["tenant_id"] or workdir_utils.default_tenant_id(),
        "user_id": review_claims["user_id"] or workdir_utils.default_user_id(),
        "workspace_id": review_claims["workspace_id"]
        or workdir_utils.default_workspace_id(),
    }

    session_id = str(review_data.get("session_id", "") or "").strip()
    if not session_id:
        return {
            "started": False,
            "status": "skipped",
            "reason": "missing_session_id",
        }

    if isinstance(review_data.get("goal_tool_execution"), dict):
        from .goal_tool_review import resume_review
        return await resume_review(review, decision=decision)

    if isinstance(review_data.get("goal_computation"), dict):
        from .goal_computation import resume_approved_computation
        return await resume_approved_computation(review, decision=decision)

    if ServerState.is_running(session_id):
        return {
            "started": False,
            "status": "skipped",
            "reason": "session_already_running",
            "session_id": session_id,
        }

    persisted_root = await SessionManager.get(session_id)
    preferred_agent = str(review_data.get("agent_name", "") or "").strip() or None
    if not preferred_agent and persisted_root:
        preferred_agent = (
            str(getattr(persisted_root, "agent_name", "") or "").strip() or None
        )

    runtime_session = ServerState.get_or_create_session(
        session_id,
        preferred_agent,
        workdir=resume_workdir,
    )
    runtime_session.update(owner_payload)
    agent = ServerState.get_or_create_agent(session_id)
    if persisted_root is None:
        persisted_root = await SessionManager.get_or_create(
            session_id,
            agent_name=preferred_agent or runtime_session.get("agent_name", "build"),
            workdir=resume_workdir,
            **owner_payload,
        )
    else:
        persisted_root.workdir = resume_workdir
        persisted_root.tenant_id = owner_payload["tenant_id"]
        persisted_root.user_id = owner_payload["user_id"]
        persisted_root.workspace_id = owner_payload["workspace_id"]
        metadata = dict(getattr(persisted_root, "metadata", {}) or {})
        metadata.update(owner_payload)
        persisted_root.metadata = metadata
    agent._existing_session = persisted_root

    if (
        (not agent.messages)
        and persisted_root
        and getattr(persisted_root, "messages", None)
    ):
        agent.messages = list(persisted_root.messages)

    async def _auto_allow_permission(_tool_name: str, _arguments: Dict[str, Any]):
        return "allow"

    async def _auto_answer_question(payload: Dict[str, Any]):
        return str(payload.get("default_answer") or "")

    approved_action_result: Optional[dict[str, Any]] = None
    if str(decision or "").strip().lower() == "approved":
        approved_action_result = await _enqueue_approved_proxy_review(
            review,
            session_id=session_id,
            agent_name=preferred_agent or runtime_session.get("agent_name", "build"),
            workdir=resume_workdir,
            owner=owner_payload,
        )

    from .goal_mode import owns_async_result
    if (getattr(persisted_root, 'metadata', {}) or {}).get('transport') == 'feishu' and approved_action_result is not None:
        # Approval authorizes this exact submission, not an unrestricted agent turn.
        # The result-only completion watcher performs subsequent interpretation.
        return {'started': False, 'status': 'queued' if approved_action_result.get('identifier') else 'failed',
                'session_id': session_id, 'approved_action': approved_action_result}
    if owns_async_result(persisted_root, review):
        # The explicitly approved proxy action above is preserved. Its result is
        # handled by the existing watcher; goal continuation still needs its own
        # budget and pause checks, never an unbounded auto-allow agent turn.
        return {'started': False, 'status': 'skipped', 'reason': 'goal_mode_managed',
                'session_id': session_id, 'approved_action': approved_action_result}

    # Enqueue can yield to a newly admitted user/goal turn. Do not overwrite its handlers.
    if ServerState.is_running(session_id):
        return {'started': False, 'status': 'skipped', 'reason': 'session_already_running',
                'session_id': session_id, 'approved_action': approved_action_result}

    resume_message = _build_review_resume_message(
        review,
        decision=decision,
        approved_action_result=approved_action_result,
    )

    try:
        ServerState.set_running(session_id, True)
        ServerState.set_turn_task(session_id, asyncio.current_task())
        agent.set_permission_handler(_auto_allow_permission)
        agent.set_question_handler(_auto_answer_question)
        await MCPManager.initialize(resume_workdir)
        await SkillManager.load(workdir=resume_workdir)

        result = await agent.run(resume_message)

        await SessionManager.update(
            session_id,
            messages=list(agent.messages),
            agent_name=(
                preferred_agent
                or str(
                    ServerState.sessions.get(session_id, {}).get("agent_name", "build")
                )
            ),
            workdir=resume_workdir,
            last_active_at=datetime.now().timestamp(),
        )
        return {
            "started": True,
            "status": "completed",
            "session_id": session_id,
            "message_id": getattr(result, "id", None),
            "workdir": resume_workdir,
            "owner": owner_payload,
            "approved_action": approved_action_result,
        }
    except Exception as e:
        return {
            "started": True,
            "status": "failed",
            "session_id": session_id,
            "error": str(e),
            "approved_action": approved_action_result,
        }
    finally:
        agent.set_permission_handler(None)
        agent.set_question_handler(None)
        agent.set_agent_event_emitter(None)
        current_turn_task = ServerState.get_turn_task(session_id)
        if current_turn_task is asyncio.current_task():
            ServerState.set_turn_task(session_id, None)
        ServerState.set_running(session_id, False)


def _workflow_background_task_active(session_id: str) -> bool:
    runtime = ServerState.sessions.get(session_id)
    if not isinstance(runtime, dict):
        return False
    task = runtime.get("workflow_auto_continue_task")
    if isinstance(task, asyncio.Task):
        if task.done():
            runtime.pop("workflow_auto_continue_task", None)
            return False
        return True
    return False


async def _cleanup_disconnected_chat_session(
    session_id: str,
    *,
    cleanup_tmp_dir: bool,
) -> bool:
    """Clean an idle chat transport without cancelling owned background work.

    Returns ``True`` when the runtime was cleaned.  A live auto-continue task
    keeps the runtime (and any uploaded inputs) alive until an explicit cancel,
    session deletion, workspace teardown, or a later idle disconnect.
    """

    if _workflow_background_task_active(session_id):
        return False
    await ServerState.cleanup_session(
        session_id,
        cleanup_tmp_dir=cleanup_tmp_dir,
    )
    return True


def _direct_session_turn_active(session_id: str) -> bool:
    """Return live turn state and discard orphaned in-memory running flags."""

    turn_task = ServerState.get_turn_task(session_id)
    if turn_task is not None:
        if not turn_task.done():
            return True
        ServerState.set_turn_task(session_id, None)
        ServerState.set_running(session_id, False)
        return False

    if ServerState.is_running(session_id):
        ServerState.set_running(session_id, False)
    return False


def _descendant_session_ids_sync(parent_session_id: str) -> list[str]:
    all_sessions = SessionManager.get_all_sync()
    children_by_parent: dict[str, list[str]] = {}
    for session in all_sessions.values():
        if (getattr(session, "metadata", None) or {}).get("side_question"):
            continue
        child_id = str(getattr(session, "id", "") or "").strip()
        parent_id = str(getattr(session, "parent_id", "") or "").strip()
        if not child_id or not parent_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(child_id)

    seen: set[str] = set()
    queue = list(children_by_parent.get(parent_session_id, []))
    descendants: list[str] = []
    while queue:
        child_id = queue.pop(0)
        if child_id in seen:
            continue
        seen.add(child_id)
        descendants.append(child_id)
        queue.extend(children_by_parent.get(child_id, []))
    return descendants


def _descendant_session_active(parent_session_id: str) -> bool:
    descendant_ids = _descendant_session_ids_sync(parent_session_id)
    for child_id in descendant_ids:
        if _direct_session_turn_active(child_id):
            return True
        if _workflow_background_task_active(child_id):
            return True
        if ServerState.get_guidance_task(child_id):
            return True
        worker = WorkerRegistry.get(child_id)
        worker_status = str(getattr(worker, "status", "") or "").strip().lower()
        if worker_status in {"busy", "running"} and worker_task_active(child_id):
            return True

    # Some older runs can leave worker registry rows alive even if session rows lag behind.
    for worker in WorkerRegistry.list_for_team(parent_session_id):
        worker_status = str(getattr(worker, "status", "") or "").strip().lower()
        if worker_status in {"busy", "running"} and worker_task_active(
            worker.session_id
        ):
            return True
        if _direct_session_turn_active(worker.session_id):
            return True
        if _workflow_background_task_active(worker.session_id):
            return True
    return False


def _chat_runtime_status_payload(
    session_id: str,
    *,
    direct_running_override: Optional[bool] = None,
) -> dict[str, Any]:
    direct_running = (
        _direct_session_turn_active(session_id)
        if direct_running_override is None
        else bool(direct_running_override)
    )
    try:
        from ..runtime.gateway import run_coordinator as _agent_run_coordinator

        harness_running = _agent_run_coordinator.store.is_session_active(session_id)
    except Exception:
        harness_running = False
    workflow_background_active = _workflow_background_task_active(session_id)
    child_session_active = _descendant_session_active(session_id)
    guidance_counts = GuidanceSQLiteStore.active_counts(session_id=session_id, workdir=_guidance_store_workdir()).get(session_id, {})
    return {
        "session_id": session_id,
        "running": direct_running or harness_running or workflow_background_active or child_session_active,
        # Descendants may outlive the submitting turn. Their lifecycle is not
        # ownership of the parent's chat input. Explicit Workflow scheduling
        # still owns the parent runtime while it is executing.
        "chat_running": direct_running or harness_running or workflow_background_active,
        "background_running": workflow_background_active or child_session_active,
        "direct_running": direct_running,
        "harness_running": harness_running,
        "workflow_background_active": workflow_background_active,
        "child_session_active": child_session_active,
        "question_request": ServerState.pending_question_request(session_id),
        "question_requests": ServerState.pending_question_requests(session_id),
        "pending_guidance_count": sum(guidance_counts.values()),
        "pending_guidance_interrupt": bool(guidance_counts.get("steer")),
        "guidance_worker_active": bool(ServerState.get_guidance_task(session_id)),
    }


def _decorate_forwarded_subagent_question(
    payload: dict[str, Any],
    *,
    sub_session: Session,
) -> dict[str, Any]:
    display_name = _extract_subagent_display_name(sub_session)
    return decorate_child_question(
        payload,
        child_session_id=sub_session.id,
        child_agent_name=sub_session.agent_name,
        child_display_name=display_name,
        parent_session_id=str(sub_session.parent_id or "").strip(),
        task_id=sub_session.id,
    )


async def _forward_subagent_question_to_parent(
    *,
    parent_session_id: str,
    sub_session: Session,
    payload: Dict[str, Any],
) -> str:
    decorated_payload = _decorate_forwarded_subagent_question(payload, sub_session=sub_session)
    parent_agent = ServerState.agents.get(parent_session_id)
    if parent_agent and hasattr(parent_agent, "_resolve_question_from_context"):
        try:
            resolved_answer = parent_agent._resolve_question_from_context(decorated_payload)
            if hasattr(resolved_answer, "__await__"):
                resolved_answer = await resolved_answer
            if resolved_answer is not None and str(resolved_answer).strip():
                return str(resolved_answer)
        except Exception:
            pass

    # AG-UI uses the same clarification policy with its durable interaction
    # transport. Do not create a WebSocket-only future for this parent run.
    handler = (ServerState.sessions.get(parent_session_id) or {}).get("question_handler")
    if callable(handler):
        return await handler(decorated_payload)

    request_id = str(uuid.uuid4())
    future = ServerState.create_question_future(parent_session_id, request_id, decorated_payload)
    default_answer = str(payload.get("default_answer") or "")
    try:
        timeout_seconds = int(payload.get("timeout_seconds", 300))
    except Exception:
        timeout_seconds = 300
    timeout_seconds = max(5, min(timeout_seconds, 1800))

    final_answer = default_answer
    try:
        await ServerState.send_session_event(
            parent_session_id,
            {
                "type": "question", "request_id": request_id,
                "session_id": parent_session_id, "question": decorated_payload,
            },
        )
        answer = await asyncio.wait_for(future, timeout=timeout_seconds)
        final_answer = "" if answer is None else str(answer)
        return final_answer
    except asyncio.TimeoutError:
        await ServerState.send_session_event(
            parent_session_id,
            {
                "type": "question_timeout",
                "request_id": request_id,
                "session_id": parent_session_id,
                "answer": default_answer,
            },
        )
        return final_answer
    finally:
        ServerState.resolve_question_future(parent_session_id, request_id, final_answer)


def _is_runtime_status_busy(runtime: dict[str, Any]) -> bool:
    return bool(
        runtime.get("running")
        or int(runtime.get("pending_guidance_count", 0) or 0) > 0
        or runtime.get("guidance_worker_active")
    )


def _resolve_worker_display_status(worker, runtime: Optional[dict[str, Any]] = None) -> str:
    runtime_payload = dict(runtime or {})
    if _is_runtime_status_busy(runtime_payload):
        return "busy"

    normalized = str(getattr(worker, "status", "") or "").strip().lower()
    if normalized in {"busy", "idle", "terminated"}:
        return normalized
    return "idle"


def _resolve_subagent_display_status(
    session: Session,
    worker=None,
    runtime: Optional[dict[str, Any]] = None,
) -> str:
    runtime_payload = dict(runtime or {})
    if _is_runtime_status_busy(runtime_payload):
        return "running"

    worker_status = str(getattr(worker, "status", "") or "").strip().lower()
    if worker_status == "busy":
        return "running"
    if worker_status == "terminated":
        return "terminated"

    session_status = str(getattr(session, "status", "") or "").strip().lower()
    if session_status in {"running", "completed", "failed", "idle", "terminated"}:
        return session_status
    return "idle"


def _is_chat_session_busy(session_id: str) -> bool:
    runtime = _chat_runtime_status_payload(session_id)
    return _is_runtime_status_busy({
        **runtime, "running": runtime.get("chat_running", runtime.get("running")),
    })


async def _update_worker_runtime_state(
    session_id: str,
    *,
    status: str,
    current_task: Optional[str] = None,
    result_summary: Optional[str] = None,
    last_task: Optional[str] = None,
    increment_task_count: bool = False,
) -> None:
    worker = WorkerRegistry.get(session_id)
    if not worker:
        return

    updates: dict[str, Any] = {
        "status": status,
        "current_task": current_task,
    }
    if result_summary is not None:
        updates["result_summary"] = str(result_summary or "")[:500]
    if last_task is not None:
        updates["last_task"] = last_task
    if increment_task_count:
        updates["task_count"] = int(worker.task_count or 0) + 1
    await WorkerRegistry.update(session_id, **updates)


async def _get_or_create_subagent_runtime_agent(
    parent_session_id: str,
    sub_session: Session,
) -> tuple[Agent, dict[str, Any]]:
    agent_info = BuiltinAgents.get(sub_session.agent_name)
    if not agent_info:
        raise RuntimeError(f"Unknown subagent: {sub_session.agent_name}")

    session = ServerState.get_or_create_session(
        sub_session.id,
        agent_name=sub_session.agent_name,
        workdir=sub_session.workdir or ServerState.get_workdir(),
    )
    session["tenant_id"] = str(getattr(sub_session, "tenant_id", "") or "")
    session["user_id"] = str(getattr(sub_session, "user_id", "") or "")
    session["workspace_id"] = str(getattr(sub_session, "workspace_id", "") or "")
    persisted_resource_context = {}
    if isinstance(getattr(sub_session, "metadata", None), dict):
        persisted_resource_context = dict(
            getattr(sub_session, "metadata", {}).get("resource_context", {}) or {}
        )
    if persisted_resource_context:
        session["resource_context"] = _merge_resource_context(
            session.get("resource_context"),
            persisted_resource_context,
        )

    cached_agent = ServerState.agents.get(sub_session.id)
    cached_name = str(
        getattr(getattr(cached_agent, "agent_info", None), "name", "") or ""
    ).strip()
    if cached_agent is not None and cached_name and cached_name != sub_session.agent_name:
        provider = ServerState.providers.pop(sub_session.id, None)
        ServerState.agents.pop(sub_session.id, None)
        if provider is not None:
            close_fn = getattr(provider, "close", None)
            if close_fn:
                try:
                    result = close_fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

    if sub_session.id not in ServerState.agents:
        parent_provider = ServerState.providers.get(parent_session_id)
        parent_runtime = ServerState.sessions.get(parent_session_id) or {}
        provider = (
            clone_provider(parent_provider)
            if parent_provider
            else create_provider(
                workdir=sub_session.workdir or parent_runtime.get("workdir") or ServerState.get_workdir(),
                model_ref=parent_runtime.get("model") or None,
            )
        )
        agent = Agent(
            agent_info=agent_info,
            provider=provider,
            session_id=sub_session.id,
            workdir=sub_session.workdir or ServerState.get_workdir(),
            existing_session=sub_session,
        )
        ServerState.agents[sub_session.id] = agent
        ServerState.providers[sub_session.id] = provider

    agent = ServerState.agents[sub_session.id]
    agent._existing_session = sub_session
    if (not agent.messages) and getattr(sub_session, "messages", None):
        agent.messages = list(sub_session.messages)
    configured_reasoning = str(
        (getattr(agent.agent_info, "options", {}) or {}).get("reasoning_effort") or ""
    ).strip().lower()
    if configured_reasoning in {"minimal", "low", "medium", "high"}:
        agent.set_reasoning_effort(configured_reasoning)
    agent.set_session_resource_context(session.get("resource_context") or {})
    return agent, session


async def _integrate_subagent_result_to_parent(
    parent_session_id: str,
    sub_session: Session,
    *,
    user_message: str,
    response: str,
    details: dict[str, Any],
    owner_kwargs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    integrated_to_parent = False
    parent_message_id = ""
    integration_content = ""
    owner = dict(owner_kwargs or {})
    parent_session = await SessionManager.get(parent_session_id, **owner)
    if parent_session:
        integration_content = _subagent_integration_message(
            display_name=details.get("display_name", sub_session.agent_name),
            agent_name=sub_session.agent_name,
            user_message=user_message,
            response=details.get("last_response", response) or response,
            tools_used=details.get("tools_used", []),
            context=details.get("context", {}),
        )
        parent_msg = Message(
            role="system",
            content=integration_content,
            id=str(uuid.uuid4()),
        )
        parent_session.messages.append(parent_msg)
        await SessionManager.update(
            parent_session.id,
            **owner,
            messages=list(parent_session.messages),
            last_active_at=parent_session.last_active_at,
        )
        integrated_to_parent = True
        parent_message_id = parent_msg.id

    return {
        "integrated_to_parent": integrated_to_parent,
        "parent_message_id": parent_message_id,
        "integration_content": integration_content,
    }


async def _run_subagent_turn_background(
    session_id: str,
    parent_session_id: str,
    message: str,
    *,
    output_format: Optional[dict[str, Any]] = None,
    outcome_contract: Optional[dict[str, Any]] = None,
    contract_revision: Optional[dict[str, Any]] = None,
    integrate_to_parent: bool = True,
    allowed_tools: Any = None,
    trace_context: Optional[dict[str, Any]] = None,
    owner_kwargs: Optional[dict[str, str]] = None,
    input_images: Optional[list[dict[str, Any]]] = None,
    public_progress_handler: Any = None,
    public_commentary_handler: Any = None,
) -> dict[str, Any]:
    owner = dict(owner_kwargs or {})
    sub_session = await SessionManager.get(session_id, **owner)
    if not sub_session:
        raise RuntimeError("Subagent session not found")
    if sub_session.parent_id != parent_session_id:
        raise RuntimeError("Subagent does not belong to parent session")

    parent_session = await SessionManager.get(parent_session_id, **owner)
    parent_metadata = dict(getattr(parent_session, "metadata", {}) or {}) if parent_session else {}
    side_chat = bool((sub_session.metadata or {}).get("side_question"))
    parent_research_context = None if side_chat else parent_metadata.get("research_context")
    if isinstance(parent_research_context, dict):
        sub_metadata = dict(getattr(sub_session, "metadata", {}) or {})
        if (
            sub_metadata.get("research_context") != parent_research_context
            or not bool(sub_metadata.get("research_context_inherited"))
        ):
            sub_metadata["research_context"] = dict(parent_research_context)
            sub_metadata["research_context_inherited"] = True
            await SessionManager.update(
                sub_session.id,
                **owner,
                metadata=sub_metadata,
            )
            sub_session.metadata = sub_metadata
    elif bool(dict(getattr(sub_session, "metadata", {}) or {}).get("research_context_inherited")):
        sub_metadata = dict(getattr(sub_session, "metadata", {}) or {})
        sub_metadata.pop("research_context", None)
        sub_metadata.pop("research_context_inherited", None)
        await SessionManager.update(
            sub_session.id,
            **owner,
            metadata=sub_metadata,
        )
        sub_session.metadata = sub_metadata

    parent_wiki_context = None if side_chat else parent_metadata.get("wiki_context")
    if isinstance(parent_wiki_context, dict):
        sub_metadata = dict(getattr(sub_session, "metadata", {}) or {})
        if (
            sub_metadata.get("wiki_context") != parent_wiki_context
            or not bool(sub_metadata.get("wiki_context_inherited"))
        ):
            sub_metadata["wiki_context"] = dict(parent_wiki_context)
            sub_metadata["wiki_context_inherited"] = True
            await SessionManager.update(
                sub_session.id,
                **owner,
                metadata=sub_metadata,
            )
            sub_session.metadata = sub_metadata
    elif bool(dict(getattr(sub_session, "metadata", {}) or {}).get("wiki_context_inherited")):
        sub_metadata = dict(getattr(sub_session, "metadata", {}) or {})
        sub_metadata.pop("wiki_context", None)
        sub_metadata.pop("wiki_context_inherited", None)
        await SessionManager.update(
            sub_session.id,
            **owner,
            metadata=sub_metadata,
        )
        sub_session.metadata = sub_metadata

    agent, runtime_session = await _get_or_create_subagent_runtime_agent(
        parent_session_id,
        sub_session,
    )
    outcome_contract = _prepare_outcome_revision_confirmation(
        agent,
        contract_revision,
        outcome_contract,
    )
    original_agent_info = agent.agent_info
    side_chat = bool((sub_session.metadata or {}).get("side_question"))
    previous_frontend_stream = getattr(agent, "_frontend_stream", False)
    if side_chat:
        agent._frontend_stream = True
    if allowed_tools not in (None, "", "all"):
        agent.agent_info = BuiltinAgents.apply_allowed_tools(agent.agent_info, allowed_tools)

    async def _auto_allow_permission(_tool_name: str, _arguments: Dict[str, Any]):
        return "allow"

    async def _bridge_question_to_parent(payload: Dict[str, Any]):
        return await _forward_subagent_question_to_parent(
            parent_session_id=session_id if side_chat else parent_session_id,
            sub_session=sub_session,
            payload=payload if isinstance(payload, dict) else {},
        )

    async def _persist_current_messages() -> None:
        latest_messages = list(agent.messages)
        sub_session.messages = latest_messages
        await _persist_live_session_messages(
            sub_session.id,
            agent_name=sub_session.agent_name,
            workdir=sub_session.workdir or ServerState.get_workdir(),
            messages=latest_messages,
            owner_kwargs=owner,
        )
        if side_chat:
            from .subagents_api import _side_chat_snapshot
            await ServerState.send_session_event(session_id, _side_chat_snapshot(sub_session))

    def _workflow_stage_event_fields() -> dict[str, Any]:
        if not isinstance(trace_context, dict):
            return {}
        fields: dict[str, Any] = {}
        for key in (
            "workflow_run_id",
            "workflow_stage_id",
            "workflow_template_key",
            "workflow_owner_session_id",
            "workflow_actor_type",
        ):
            value = trace_context.get(key)
            if value is not None and str(value or "").strip():
                fields[key] = value
        return fields

    async def _emit_subagent_event_to_parent(payload: dict[str, Any]) -> None:
        event = dict(payload or {})
        details = _extract_subagent_details(sub_session)
        event.setdefault("type", "agent_event")
        event.setdefault("parent_session_id", parent_session_id)
        event.setdefault("session_id", sub_session.id)
        event.setdefault("agent_name", sub_session.agent_name)
        event.setdefault("display_name", details.get("display_name", sub_session.agent_name))
        event.update(_workflow_stage_event_fields())
        if "conversation_with" not in event and details.get("conversation_with"):
            event["conversation_with"] = details.get("conversation_with")
        if "context" not in event and details.get("context"):
            event["context"] = details.get("context")
        await ServerState.send_session_event(session_id if side_chat else parent_session_id, event)

    async def _subagent_agent_event_emitter(payload: Dict[str, Any]) -> None:
        event = dict(payload if isinstance(payload, dict) else {})
        if side_chat and str(event.get("kind") or "").startswith("model_output"):
            await _emit_subagent_event_to_parent(event)
            return
        if public_commentary_handler is not None and event.get("kind") == "model_output":
            # Only classified public tool-call text belongs in the parent reply.
            # Unclassified deltas may instead be a structured stage result.
            if event.get("status") == "completed" and event.get("presentation") == "commentary":
                await public_commentary_handler(event)
            return
        if not _should_persist_subagent_agent_event(event):
            await _emit_subagent_event_to_parent(event)
            return

        text = _format_subagent_agent_event_message(event)
        if not text:
            return
        event_msg = Message(
            role="system",
            content=text,
            id=str(uuid.uuid4()),
        )
        agent.messages.append(event_msg)
        await _persist_current_messages()
        if "message" not in event:
            event["message"] = text.removeprefix("[Subagent Event] ").strip()
        await _emit_subagent_event_to_parent(event)

    agent.set_permission_handler(_auto_allow_permission)
    agent.set_question_handler(_bridge_question_to_parent)
    agent.set_agent_event_emitter(_subagent_agent_event_emitter)
    previous_trace_context = dict(
        getattr(agent, "_active_tool_trace_context", {}) or {}
    )
    merged_trace_context = {
        **previous_trace_context,
        "parent_session_id": str(parent_session_id or "").strip(),
        "subagent_session_id": str(session_id or "").strip(),
    }
    if isinstance(trace_context, dict):
        for key, value in trace_context.items():
            merged_trace_context[str(key)] = value
    agent._active_tool_trace_context = merged_trace_context

    result: Optional[Message] = None
    seen_tool_call_keys: set[tuple[str, str]] = set()
    seen_completed_tool_keys: set[tuple[str, str]] = set()

    def _subagent_tool_call_key(tool_call: Any) -> tuple[str, str]:
        call_id = str(getattr(tool_call, "call_id", "") or "").strip()
        if call_id:
            return ("call_id", call_id)
        return (
            str(getattr(tool_call, "tool_name", "") or ""),
            json.dumps(getattr(tool_call, "arguments", {}) or {}, sort_keys=True),
        )

    try:
        await _update_worker_runtime_state(
            session_id,
            status="busy",
            current_task=message,
        )
        stage_id = ""
        if isinstance(trace_context, dict):
            stage_id = str(trace_context.get("workflow_stage_id") or "").strip()
        await _emit_subagent_event_to_parent(
            {
                "status": "running",
                "message": (
                    f"workflow stage `{stage_id}` started"
                    if stage_id
                    else "subagent started"
                ),
                "scheduler_mode": str(runtime_session.get("scheduler_mode", "supervisor") or "supervisor"),
            }
        )
        ServerState.set_running(session_id, True)
        ServerState.set_turn_task(session_id, asyncio.current_task())
        await MCPManager.initialize(ServerState.get_workdir())
        await SkillManager.load(workdir=ServerState.get_workdir())
        agent.set_next_user_input_images(input_images)
        async for item in agent.run_stream(
            message,
            output_format=output_format,
            outcome_contract=outcome_contract,
        ):
            for tool_call in getattr(item, "tool_calls", []) or []:
                key = _subagent_tool_call_key(tool_call)
                state = getattr(getattr(tool_call, "state", None), "value", "")
                if public_progress_handler is not None and state in {"completed", "error"} and key not in seen_completed_tool_keys:
                    seen_completed_tool_keys.add(key)
                    try:
                        await public_progress_handler(tool_call)
                    except Exception:
                        # Optional presentation must never interrupt execution.
                        pass
                if key in seen_tool_call_keys:
                    continue
                seen_tool_call_keys.add(key)
                await _emit_subagent_event_to_parent(
                    {
                        "status": "running",
                        "message": f"using tool {getattr(tool_call, 'tool_name', '') or 'unknown'}",
                        "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
                        "tool_args": getattr(tool_call, "arguments", {}) or {},
                        "tool_call": _serialize_frontend_tool_call(tool_call),
                    }
                )
            if isinstance(item, Message) and item.role == "assistant":
                result = item
            await _persist_current_messages()

        await _persist_current_messages()
        details = _extract_subagent_details(sub_session)
        from .workflow_runtime import collect_workflow_tool_receipts
        workflow_tool_receipts = collect_workflow_tool_receipts(
            agent.messages, seen_tool_call_keys, trace_context,
        )
        integration: dict[str, Any] = {}
        if integrate_to_parent and not side_chat:
            integration = await _integrate_subagent_result_to_parent(
                parent_session_id,
                sub_session,
                user_message=message,
                response=(result.content if result is not None else details.get("last_response", "") or ""),
                details=details,
                owner_kwargs=owner,
            )
        await _update_worker_runtime_state(
            session_id,
            status="idle",
            current_task=None,
            result_summary=(result.content if result is not None else str(details.get("last_response", "") or "")),
            last_task=message,
            increment_task_count=True,
        )
        outcome_status = str(
            getattr(agent, "_last_outcome_status", "") or ""
        ).strip().lower()
        completion_status = (
            outcome_status if outcome_status in {"partial", "blocked"} else "completed"
        )
        outcome_validation = dict(
            getattr(agent, "_last_outcome_validation", {}) or {}
        )
        await _emit_subagent_event_to_parent(
            {
                "status": completion_status,
                "message": (
                    "subagent completed"
                    if completion_status == "completed"
                    else f"subagent returned {completion_status} outcome"
                ),
                "last_response": details.get("last_response", (result.content if result is not None else "")),
                "tools_used": details.get("tools_used", []),
                "async_tool_refs": details.get("async_tool_refs", []),
                "artifact_refs": details.get("artifact_refs", {}),
                "artifact_paths": details.get("artifact_paths", []),
                "conversation_with": details.get("conversation_with", ["human"]),
                "context": details.get("context", {}),
                "contract": details.get("contract"),
                "outcome_status": completion_status,
                "outcome_validation": _public_outcome_validation(outcome_validation),
            }
        )
        return {
            "status": completion_status,
            "session_id": sub_session.id,
            "agent_name": sub_session.agent_name,
            "response": (result.content if result is not None else str(details.get("last_response", "") or "")),
            "message_id": (result.id if result is not None else ""),
            "workflow_tool_receipts": workflow_tool_receipts,
            "display_name": details.get("display_name", sub_session.agent_name),
            "tools_used": details.get("tools_used", []),
            "async_tool_refs": details.get("async_tool_refs", []),
            "artifact_refs": details.get("artifact_refs", {}),
            "artifact_paths": details.get("artifact_paths", []),
            "conversation_with": details.get("conversation_with", ["human"]),
            "last_response": details.get("last_response", (result.content if result is not None else "")),
            "context": details.get("context", {}),
            "contract": details.get("contract"),
            "outcome_status": completion_status,
            "outcome_validation": outcome_validation,
            "verification": _public_outcome_validation(
                getattr(result, "verification", None) or outcome_validation
            ),
            "research_grounding": dict(
                getattr(result, "research_grounding", {}) or {}
            )
            if result is not None
            else {},
            **integration,
        }
    except asyncio.CancelledError:
        cancelled_message = Message(
            role="system",
            content="[Guidance Cancelled] 当前子代理回合已被中断。",
            id=str(uuid.uuid4()),
        )
        try:
            agent.messages.append(cancelled_message)
            await _persist_current_messages()
        except Exception:
            pass
        await _update_worker_runtime_state(
            session_id,
            status="idle",
            current_task=None,
            result_summary="当前子代理回合已被中断。",
            last_task=message,
        )
        await _emit_subagent_event_to_parent(
            {
                "status": "cancelled",
                "message": "subagent cancelled",
            }
        )
        return {
            "status": "cancelled",
            "session_id": sub_session.id,
            "agent_name": sub_session.agent_name,
        }
    except Exception as e:
        error_message = Message(
            role="system",
            content=f"[Subagent Error] {str(e)}",
            id=str(uuid.uuid4()),
        )
        try:
            agent.messages.append(error_message)
            await _persist_current_messages()
        except Exception:
            pass
        await _update_worker_runtime_state(
            session_id,
            status="idle",
            current_task=None,
            result_summary=str(e),
            last_task=message,
        )
        await _emit_subagent_event_to_parent(
            {
                "status": "failed",
                "message": str(e),
            }
        )
        return {
            "status": "failed",
            "session_id": sub_session.id,
            "agent_name": sub_session.agent_name,
            "error": str(e),
        }
    finally:
        agent.agent_info = original_agent_info
        agent._frontend_stream = previous_frontend_stream
        agent.set_permission_handler(None)
        agent.set_question_handler(None)
        agent.set_agent_event_emitter(None)
        agent._active_tool_trace_context = previous_trace_context
        current_turn_task = ServerState.get_turn_task(session_id)
        if current_turn_task is asyncio.current_task():
            ServerState.set_turn_task(session_id, None)
        ServerState.set_running(session_id, False)


async def _run_native_session_turn_background(
    session_id: str,
    message: str,
    *,
    output_format: Optional[dict[str, Any]] = None,
    outcome_contract: Optional[dict[str, Any]] = None,
    contract_revision: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    persisted_root = await SessionManager.get(session_id)
    if not persisted_root:
        raise RuntimeError("Session not found")

    parent_session_id = str(getattr(persisted_root, "parent_id", "") or "").strip()
    if parent_session_id:
        return await _run_subagent_turn_background(
            session_id,
            parent_session_id,
            message,
            output_format=output_format,
            outcome_contract=outcome_contract,
            contract_revision=contract_revision,
        )

    preferred_agent = str(getattr(persisted_root, "agent_name", "") or "").strip() or None
    target_workdir = str(
        getattr(persisted_root, "workdir", "") or ServerState.get_workdir()
    ).strip() or ServerState.get_workdir()
    session = ServerState.get_or_create_session(
        session_id,
        preferred_agent,
        workdir=target_workdir,
    )
    session["tenant_id"] = str(getattr(persisted_root, "tenant_id", "") or "")
    session["user_id"] = str(getattr(persisted_root, "user_id", "") or "")
    session["workspace_id"] = str(getattr(persisted_root, "workspace_id", "") or "")
    resource_context = _merge_resource_context(
        session.get("resource_context"),
        dict(getattr(persisted_root, "metadata", {}) or {}).get("resource_context"),
    )
    if resource_context:
        session["resource_context"] = dict(resource_context)
    agent = ServerState.get_or_create_agent(session_id)
    agent._existing_session = persisted_root
    _bind_guidance_v2_to_agent(agent, persisted_root)
    if resource_context:
        agent.set_session_resource_context(resource_context)

    if (
        (not agent.messages)
        and persisted_root
        and getattr(persisted_root, "messages", None)
    ):
        agent.messages = list(persisted_root.messages)
    outcome_contract = _prepare_outcome_revision_confirmation(
        agent,
        contract_revision,
        outcome_contract,
    )

    async def _auto_allow_permission(_tool_name: str, _arguments: Dict[str, Any]):
        return "allow"

    async def _auto_answer_question(payload: Dict[str, Any]):
        return str(payload.get("default_answer") or "")

    async def _persist_background_commentary(payload: Dict[str, Any]) -> None:
        # The engine already appends the stable public assistant message.
        # Persist that conversation; never synthesize notes from tool status.
        if (payload.get("kind") != "model_output" or payload.get("status") != "completed"
                or payload.get("presentation") != "commentary"):
            return
        try:
            await SessionManager.update(
                session_id,
                messages=list(agent.messages),
                agent_name=session.get("agent_name", "build"),
                workdir=target_workdir,
                last_active_at=datetime.now().timestamp(),
            )
        except Exception:
            pass

    if (getattr(persisted_root, "metadata", {}) or {}).get("transport") == "feishu":
        from .feishu_runtime import require_desktop_interaction
        agent.set_permission_handler(require_desktop_interaction)
        agent.set_question_handler(require_desktop_interaction)
    else:
        agent.set_permission_handler(_auto_allow_permission)
        agent.set_question_handler(_auto_answer_question)
    agent.set_agent_event_emitter(_persist_background_commentary)

    result: Optional[Message] = None
    try:
        ServerState.set_running(session_id, True)
        ServerState.set_turn_task(session_id, asyncio.current_task())
        await MCPManager.initialize(target_workdir)
        await SkillManager.load(workdir=target_workdir)
        result = await agent.run(
            message,
            output_format=output_format,
            outcome_contract=outcome_contract,
        )
        await SessionManager.update(
            session_id,
            messages=list(agent.messages),
            agent_name=session.get("agent_name", "build"),
            workdir=target_workdir,
            last_active_at=datetime.now().timestamp(),
        )
        outcome_status = str(
            getattr(agent, "_last_outcome_status", "") or ""
        ).strip().lower()
        completion_status = (
            outcome_status if outcome_status in {"partial", "blocked"} else "completed"
        )
        return {
            "status": completion_status,
            "message_id": getattr(result, "id", None),
            "outcome_status": completion_status,
            "outcome_validation": dict(
                getattr(agent, "_last_outcome_validation", {}) or {}
            ),
        }
    except asyncio.CancelledError:
        cancelled_message = Message(
            role="system",
            content="[Guidance Cancelled] 当前继续指导已被中断。",
            id=str(uuid.uuid4()),
        )
        try:
            agent.messages.append(cancelled_message)
            await SessionManager.update(
                session_id,
                messages=list(agent.messages),
                agent_name=session.get("agent_name", "build"),
                workdir=target_workdir,
                last_active_at=datetime.now().timestamp(),
            )
        except Exception:
            pass
        return {
            "status": "cancelled",
        }
    except Exception as e:
        error_message = Message(
            role="system",
            content=f"[Guidance Error] {str(e)}",
            id=str(uuid.uuid4()),
        )
        try:
            agent.messages.append(error_message)
            await SessionManager.update(
                session_id,
                messages=list(agent.messages),
                agent_name=session.get("agent_name", "build"),
                workdir=target_workdir,
                last_active_at=datetime.now().timestamp(),
            )
        except Exception:
            pass
        return {
            "status": "failed",
            "error": str(e),
        }
    finally:
        agent.set_permission_handler(None)
        agent.set_question_handler(None)
        agent.set_agent_event_emitter(None)
        current_turn_task = ServerState.get_turn_task(session_id)
        if current_turn_task is asyncio.current_task():
            ServerState.set_turn_task(session_id, None)
        ServerState.set_running(session_id, False)


async def _run_external_session_turn_background(
    persisted_root,
    message: str,
    *,
    runtime_id: str,
    model_id: str = "",
    expected_switch_version: Optional[int] = None,
    guidance_id: str = "",
) -> dict[str, Any]:
    """Run a durable background turn through the selected external backend."""

    from ..external_harness.handoff_store import HarnessRuntimeMismatch
    from ..runtime.agent_backend import NormalizedMessage, NormalizedRunInput, RunContext
    from ..runtime.gateway import agent_backend_registry, run_coordinator, runtime_registry
    from ..runtime.model_policy import resolve_runtime_model
    from .harness_api import harness_handoff_store

    session_id = str(getattr(persisted_root, "id", "") or "").strip()
    tenant_id = str(getattr(persisted_root, "tenant_id", "") or "").strip()
    user_id = str(getattr(persisted_root, "user_id", "") or "").strip()
    workspace_id = str(getattr(persisted_root, "workspace_id", "") or "").strip()
    workdir = str(
        getattr(persisted_root, "workdir", "") or ServerState.get_workdir()
    ).strip() or ServerState.get_workdir()
    owner_key = f"{tenant_id}:{user_id}"
    state = harness_handoff_store.get_state(
        owner_key=owner_key,
        workspace_id=workspace_id,
        session_id=session_id,
        session_metadata=dict(getattr(persisted_root, "metadata", {}) or {}),
    )
    current_runtime_id = str(state.get("current_runtime_id") or "tcode-native")
    expected_runtime_id = str(runtime_id or current_runtime_id).strip()
    if current_runtime_id != expected_runtime_id:
        raise HarnessRuntimeMismatch(
            "Guidance Harness route changed before the continuation started",
            state=state,
        )
    if (
        expected_switch_version is not None
        and int(state.get("switch_version") or 0) != int(expected_switch_version)
    ):
        raise HarnessRuntimeMismatch(
            "Guidance Harness version changed before the continuation started",
            state=state,
        )
    if current_runtime_id == "tcode-native":
        raise RuntimeError("external turn dispatcher received tcode-native")
    if runtime_registry.get(current_runtime_id) is None and agent_backend_registry.get(
        current_runtime_id
    ) is None:
        raise RuntimeError(f"Unknown harness runtime: {current_runtime_id}")

    remembered_models = dict(state.get("runtime_models") or {})
    selected_model = await resolve_runtime_model(
        current_runtime_id,
        str(model_id or remembered_models.get(current_runtime_id) or ""),
        workdir=workdir,
        runtime_registry=runtime_registry,
        backend_registry=agent_backend_registry,
    )
    normalized_messages: list[NormalizedMessage] = []
    for item in list(getattr(persisted_root, "messages", []) or []):
        role_value = getattr(
            getattr(item, "role", ""), "value", getattr(item, "role", "")
        )
        role = str(role_value or "").strip()
        item_id = str(getattr(item, "id", "") or "").strip()
        if role not in {"user", "assistant", "system", "tool"} or not item_id:
            continue
        normalized_messages.append(
            NormalizedMessage(
                id=item_id,
                role=role,
                content=getattr(item, "content", ""),
            )
        )

    run_id = f"guidance-{guidance_id or uuid.uuid4().hex}"[:200]
    user_message_id = f"{run_id}:user"[:200]
    guidance_metadata = (
        {
            "guidance": {
                "id": guidance_id,
                "mode": "steer",
                "applied_safe_point": "after_recovery",
            }
        }
        if guidance_id
        else {}
    )
    normalized_messages.append(
        NormalizedMessage(
            id=user_message_id,
            role="user",
            content=message,
            metadata=guidance_metadata,
        )
    )
    pending_handoff = harness_handoff_store.pending_for_target(
        owner_key=owner_key,
        workspace_id=workspace_id,
        session_id=session_id,
        target_runtime_id=current_runtime_id,
    )
    normalized_run = NormalizedRunInput(
        thread_id=session_id,
        run_id=run_id,
        agent_id=str(getattr(persisted_root, "agent_name", "") or "default"),
        runtime_id=current_runtime_id,
        model_id=selected_model.model_id,
        messages=normalized_messages,
        context=[pending_handoff] if pending_handoff else [],
        forwarded_props={
            "tcode": {
                "workspaceId": workspace_id,
                "runtimeId": current_runtime_id,
                "modelId": selected_model.model_id,
                "publicServer": auth_required(),
            }
        },
    )
    context = RunContext(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        workdir=workdir,
        session_id=session_id,
        run_id=run_id,
        agent_id=normalized_run.agent_id,
        runtime_id=current_runtime_id,
        request_id=run_id,
        handoff_id=str((pending_handoff or {}).get("handoff_id") or ""),
    )
    prepared = await run_coordinator.prepare(
        normalized_run,
        context,
        client_request_id=run_id,
    )
    metadata = dict(getattr(persisted_root, "metadata", {}) or {})
    metadata["harness"] = harness_handoff_store.projection(state)
    await SessionManager.update(
        session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        model=selected_model.model_id,
        metadata=metadata,
        status="running",
    )

    terminal_type = ""
    terminal_error = ""
    input_persisted = False
    try:
        async for event in run_coordinator.stream(prepared):
            if event.type == "run.started" and not input_persisted:
                latest = await SessionManager.get(
                    session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                if latest is not None:
                    existing_messages = list(getattr(latest, "messages", []) or [])
                    existing_ids = {
                        str(getattr(item, "id", "") or "")
                        for item in existing_messages
                    }
                    if user_message_id not in existing_ids:
                        existing_messages.append(
                            Message(
                                role="user",
                                content=message,
                                id=user_message_id,
                                metadata={
                                    **guidance_metadata,
                                    "source_runtime_id": current_runtime_id,
                                },
                            )
                        )
                        await SessionManager.update(
                            session_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            messages=existing_messages,
                            last_active_at=datetime.now().timestamp(),
                        )
                input_persisted = True
            if event.type in {"run.finished", "run.error", "run.cancelled", "run.interrupted"}:
                terminal_type = event.type
                terminal_error = str(getattr(event, "message", "") or "")
    finally:
        latest = await SessionManager.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if latest is not None:
            refreshed_state = harness_handoff_store.get_state(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
            )
            latest_metadata = dict(getattr(latest, "metadata", {}) or {})
            latest_metadata["harness"] = harness_handoff_store.projection(
                refreshed_state
            )
            await SessionManager.update(
                session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
                metadata=latest_metadata,
                status=(
                    "idle"
                    if terminal_type == "run.finished"
                    else "cancelled"
                    if terminal_type == "run.cancelled"
                    else "failed"
                ),
            )
    return {
        "status": (
            "completed"
            if terminal_type == "run.finished"
            else "cancelled"
            if terminal_type == "run.cancelled"
            else "failed"
        ),
        "runtime_id": current_runtime_id,
        "run_id": run_id,
        **({"error": terminal_error} if terminal_error else {}),
    }


async def _run_session_turn_background(
    session_id: str,
    message: str,
    *,
    output_format: Optional[dict[str, Any]] = None,
    outcome_contract: Optional[dict[str, Any]] = None,
    contract_revision: Optional[dict[str, Any]] = None,
    runtime_id: str = "",
    model_id: str = "",
    harness_switch_version: Optional[int] = None,
    guidance_id: str = "",
) -> dict[str, Any]:
    """Dispatch a background turn through the session's authoritative Harness."""

    from .harness_api import harness_handoff_store

    persisted_root = await SessionManager.get(session_id)
    if persisted_root is None:
        raise RuntimeError("Session not found")
    owner_key = (
        f"{str(getattr(persisted_root, 'tenant_id', '') or '')}:"
        f"{str(getattr(persisted_root, 'user_id', '') or '')}"
    )
    state = harness_handoff_store.get_state(
        owner_key=owner_key,
        workspace_id=str(getattr(persisted_root, "workspace_id", "") or ""),
        session_id=session_id,
        session_metadata=dict(getattr(persisted_root, "metadata", {}) or {}),
    )
    selected_runtime_id = str(
        runtime_id or state.get("current_runtime_id") or "tcode-native"
    ).strip()
    if selected_runtime_id == "tcode-native":
        return await _run_native_session_turn_background(
            session_id,
            message,
            output_format=output_format,
            outcome_contract=outcome_contract,
            contract_revision=contract_revision,
        )
    return await _run_external_session_turn_background(
        persisted_root,
        message,
        runtime_id=selected_runtime_id,
        model_id=model_id,
        expected_switch_version=harness_switch_version,
        guidance_id=guidance_id,
    )


async def _guidance_message_persisted(
    session_id: str,
    guidance_id: str,
    *,
    owner_kwargs: dict[str, str],
) -> bool:
    session = await SessionManager.get(session_id, **owner_kwargs)
    if session is None:
        return False
    for message in list(getattr(session, "messages", []) or []):
        metadata = dict(getattr(message, "metadata", {}) or {})
        guidance = metadata.get("guidance")
        if isinstance(guidance, dict) and str(guidance.get("id") or "") == guidance_id:
            return True
    return False


async def _drain_session_guidance_v2(session_id: str) -> None:
    """Consume queued and external restart-turn guidance one at a time."""

    from ..runtime.gateway import run_coordinator
    from .guidance_runtime import guidance_runtime

    lock = ServerState.get_session_lock(session_id)
    try:
        while True:
            if ServerState.session_cancelled(session_id):
                return
            async with lock:
                persisted = await SessionManager.get(session_id)
                if persisted is None or ServerState.is_running(session_id):
                    return
                await _migrate_session_guidance_v2(persisted)
                owner_scope = _guidance_owner_scope_from_session(persisted)
                owner_kwargs = _session_owner_kwargs_from_session(persisted)
                owner_key = f"{owner_scope.tenant_id}:{owner_scope.user_id}"
                if run_coordinator.store.active_for_session(owner_key, session_id) is not None:
                    return
                workdir = _guidance_store_workdir()
                guidance_runtime.ensure_workdir(workdir)
                await asyncio.to_thread(
                    GuidanceSQLiteStore.reconcile_expired_claims,
                    owner=owner_scope,
                    workdir=workdir,
                )
                queue_owner_id = f"queue:{uuid.uuid4()}"
                user_message_id = str(uuid.uuid4())
                turn_id = f"turn-{user_message_id}"
                item = await asyncio.to_thread(
                    GuidanceSQLiteStore.claim_next_restart_turn,
                    session_id=session_id,
                    new_turn_id=turn_id,
                    owner_id=queue_owner_id,
                    owner=owner_scope,
                    workdir=workdir,
                    lease_seconds=30,
                )
                if item is None:
                    item = await asyncio.to_thread(
                        GuidanceSQLiteStore.claim_next_queue,
                        session_id=session_id,
                        new_turn_id=turn_id,
                        owner_id=queue_owner_id,
                        owner=owner_scope,
                        workdir=workdir,
                        lease_seconds=30,
                    )
                if item is None:
                    return

                if str(item.runtime_id or "tcode-native") == "tcode-native":
                    runtime_session = ServerState.get_or_create_session(
                        session_id,
                        str(getattr(persisted, "agent_name", "") or "build"),
                        workdir=str(getattr(persisted, "workdir", "") or workdir),
                    )
                    runtime_session["tenant_id"] = owner_scope.tenant_id
                    runtime_session["user_id"] = owner_scope.user_id
                    runtime_session["workspace_id"] = owner_scope.workspace_id
                    agent = ServerState.get_or_create_agent(session_id)
                    controller = guidance_runtime.controller(
                        workdir=workdir,
                        owner=owner_scope,
                    )
                    agent.set_guidance_port(controller, owner_id=queue_owner_id)
                    agent.set_next_user_message_context(
                        message_id=user_message_id,
                        metadata={
                            "guidance": {
                                "id": item.id,
                                "sequence": item.sequence,
                                "mode": item.mode.value,
                                "target_turn_id": turn_id,
                                "applied_safe_point": "after_recovery",
                                "contract_revision_id": str(
                                    (item.contract_revision or {}).get("revision_id") or ""
                                ),
                            }
                        },
                    )
                applying = (
                    await asyncio.to_thread(
                        GuidanceSQLiteStore.mark_applying,
                        items=[item],
                        owner=owner_scope,
                        workdir=workdir,
                    )
                )[0]
                run_task = asyncio.create_task(
                    _run_session_turn_background(
                        session_id,
                        applying.message,
                        output_format=applying.output_format,
                        outcome_contract=applying.outcome_contract,
                        contract_revision=applying.contract_revision,
                        runtime_id=applying.runtime_id,
                        model_id=applying.model_id,
                        harness_switch_version=applying.harness_switch_version,
                        guidance_id=applying.id,
                    )
                )
                message_written = False
                while not run_task.done():
                    if await _guidance_message_persisted(
                        session_id,
                        applying.id,
                        owner_kwargs=owner_kwargs,
                    ):
                        message_written = True
                        break
                    await asyncio.sleep(0.05)
                if not message_written:
                    message_written = await _guidance_message_persisted(
                        session_id,
                        applying.id,
                        owner_kwargs=owner_kwargs,
                    )
                if message_written:
                    applying = (
                        await asyncio.to_thread(
                            GuidanceSQLiteStore.mark_applied,
                            items=[applying],
                            effect=(
                                "restarted_same_harness"
                                if applying.mode is GuidanceMode.STEER
                                and applying.runtime_id != "tcode-native"
                                else "started_next_turn"
                            ),
                            owner=owner_scope,
                            workdir=workdir,
                        )
                    )[0]
                try:
                    result = await run_task
                except Exception as exc:
                    if not message_written:
                        try:
                            await asyncio.to_thread(
                                GuidanceSQLiteStore.mark_failed,
                                item=applying,
                                failure_code="queue_turn_not_started",
                                failure_detail=str(exc),
                                owner=owner_scope,
                                workdir=workdir,
                            )
                        except Exception:
                            pass
                    continue
                if not message_written:
                    await asyncio.to_thread(
                        GuidanceSQLiteStore.mark_failed,
                        item=applying,
                        failure_code="queue_turn_not_started",
                        failure_detail=str(result.get("error") or result.get("status") or ""),
                        owner=owner_scope,
                        workdir=workdir,
                    )
    finally:
        ServerState.set_guidance_task(session_id, None)
        try:
            persisted = await SessionManager.get(session_id)
            if persisted is not None:
                owner_scope = _guidance_owner_scope_from_session(persisted)
                workdir = _guidance_store_workdir()
                counts = await asyncio.to_thread(
                    GuidanceSQLiteStore.pending_counts,
                    session_id=session_id,
                    owner=owner_scope,
                    workdir=workdir,
                )
                if (
                    counts.get("pending_queue_count", 0) > 0
                    or counts.get("pending_restart_turn_count", 0) > 0
                ) and not ServerState.session_cancelled(session_id) and not ServerState.is_running(session_id) and run_coordinator.store.active_for_session(
                    f"{owner_scope.tenant_id}:{owner_scope.user_id}", session_id
                ) is None:
                    _ensure_session_guidance_drain_v2(session_id)
        except Exception:
            pass


def _ensure_session_guidance_drain_v2(session_id: str) -> bool:
    if ServerState.get_guidance_task(session_id):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(_drain_session_guidance_v2(session_id))
    ServerState.set_guidance_task(session_id, task)
    return True


def _symphony_memory_issues_path() -> Path:
    configured = os.environ.get("SYMPHONY_MEMORY_ISSUES_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path(ServerState.get_workdir()) / "tmp" / "symphony_memory_issues.json"


def _discover_local_issue_store_workdirs() -> list[str]:
    candidates: list[str] = []

    def _append(path: Path | str) -> None:
        text = str(path or "").strip()
        if not text:
            return
        if any(_same_resolved_workdir(existing, text) for existing in candidates):
            return
        candidates.append(text)

    server_root = Path(ServerState.get_workdir()).expanduser().resolve(strict=False)
    _append(server_root)

    workdirs_root = workdir_utils.workspace_storage_root(ServerState.get_workdir())
    if workdirs_root.is_dir():
        for tenant_dir in sorted(item for item in workdirs_root.iterdir() if item.is_dir()):
            for user_dir in sorted(item for item in tenant_dir.iterdir() if item.is_dir()):
                for workspace in sorted(item for item in user_dir.iterdir() if item.is_dir()):
                    if (workspace / "tmp" / "shared_tracker.sqlite").is_file():
                        _append(workspace)

    return candidates


def _issue_matches_workspace_context(
    issue: dict[str, Any],
    context: Optional[workdir_utils.WorkspaceContext],
    *,
    store_workdir: str = "",
) -> bool:
    """Fail closed when an HTTP request cannot prove ownership of an issue."""

    if context is None:
        return True
    workspace = issue.get("workspace") if isinstance(issue.get("workspace"), dict) else {}
    tenant_id = str(workspace.get("tenant_id") or "").strip()
    user_id = str(workspace.get("user_id") or "").strip()
    workspace_id = str(workspace.get("workspace_id") or "").strip()
    if tenant_id or user_id or workspace_id:
        return (
            tenant_id == context.tenant_id
            and user_id == context.user_id
            and workspace_id == context.workspace_id
        )

    path_candidates = [
        workspace.get("workdir"),
        workspace.get("control_workdir"),
        store_workdir,
    ]
    protein_run = workspace.get("protein_run") if isinstance(workspace.get("protein_run"), dict) else {}
    path_candidates.append(protein_run.get("control_workdir"))
    for path_text in path_candidates:
        owner = workdir_utils.workspace_context_from_managed_path(
            ServerState.get_workdir(),
            path_text,
        )
        if owner is not None:
            return owner == context
    return False


def _same_owner_user(
    left: workdir_utils.WorkspaceContext,
    right: workdir_utils.WorkspaceContext,
) -> bool:
    """Compare the authenticated owner without coupling to one workspace."""

    return (
        left.tenant_id == right.tenant_id
        and left.user_id == right.user_id
    )


async def _issue_matches_owner_context(
    issue: dict[str, Any],
    context: Optional[workdir_utils.WorkspaceContext],
    *,
    store_workdir: str = "",
) -> bool:
    """Authorize a mutation using the issue's persisted owner information.

    Mutating an issue must not depend on whichever conversation/workspace is
    currently selected in the UI.  New rows carry explicit owner claims;
    older rows can be proven through a managed path or their owning Session.
    Every fallback remains fail-closed.
    """

    if context is None:
        return True

    workspace = (
        issue.get("workspace")
        if isinstance(issue.get("workspace"), dict)
        else {}
    )
    tenant_id = str(workspace.get("tenant_id") or "").strip()
    user_id = str(workspace.get("user_id") or "").strip()
    if tenant_id or user_id:
        return tenant_id == context.tenant_id and user_id == context.user_id

    path_candidates = [
        workspace.get("workdir"),
        workspace.get("control_workdir"),
        store_workdir,
    ]
    protein_run = (
        workspace.get("protein_run")
        if isinstance(workspace.get("protein_run"), dict)
        else {}
    )
    path_candidates.append(protein_run.get("control_workdir"))
    for path_text in path_candidates:
        owner = workdir_utils.workspace_context_from_managed_path(
            ServerState.get_workdir(),
            path_text,
        )
        if owner is not None:
            return _same_owner_user(owner, context)

    resume_data = (
        issue.get("resume_data")
        if isinstance(issue.get("resume_data"), dict)
        else {}
    )
    session_ids = {
        str(value or "").strip()
        for value in (
            workspace.get("session_id"),
            workspace.get("parent_session_id"),
            resume_data.get("session_id"),
            resume_data.get("parent_session_id"),
            issue.get("session_id"),
            issue.get("parent_session_id"),
        )
        if str(value or "").strip()
    }
    for candidate_session_id in session_ids:
        try:
            session = await SessionManager.get(
                candidate_session_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
        except Exception:
            session = None
        if session is not None:
            return True
    return False


def _issue_reference_matches(
    issue: dict[str, Any],
    *,
    issue_id: str = "",
    issue_identifier: str = "",
) -> bool:
    """Match all supplied stable references, avoiding identifier collisions."""

    normalized_id = str(issue_id or "").strip()
    normalized_identifier = str(issue_identifier or "").strip()
    if not normalized_id and not normalized_identifier:
        return False
    if (
        normalized_id
        and str(issue.get("id") or issue.get("task_id") or "").strip()
        != normalized_id
    ):
        return False
    if (
        normalized_identifier
        and str(issue.get("identifier") or "").strip() != normalized_identifier
    ):
        return False
    return True


def _owner_local_issue_store_workdirs(
    context: workdir_utils.WorkspaceContext,
) -> list[str]:
    """Return tracker roots owned by one authenticated tenant/user.

    Paths remain a server-side implementation detail and are never returned to
    the client.  The legacy server-root tracker is included, but rows loaded
    from it still have to pass ``_issue_matches_owner_context``.
    """

    candidates: list[str] = []
    tracker_paths: set[str] = set()

    def _append(workdir: str | Path, *, require_tracker: bool = True) -> None:
        text = str(workdir or "").strip()
        if not text:
            return
        try:
            tracker_path = SharedTracker.db_path(text).resolve(strict=False)
        except Exception:
            return
        if require_tracker and not tracker_path.is_file():
            return
        tracker_key = tracker_path.as_posix()
        if tracker_key in tracker_paths:
            return
        tracker_paths.add(tracker_key)
        candidates.append(text)

    try:
        workdirs = workdir_utils.list_user_workdirs(
            ServerState.get_workdir(),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
    except Exception:
        workdirs = []
    for item in workdirs:
        if isinstance(item, dict):
            _append(str(item.get("path") or ""))

    # ``list_user_workdirs`` intentionally hides deleting/deleted workspaces
    # from normal navigation.  Mutation recovery still has to find their
    # tracker rows so it can distinguish "workspace is deleting" from a
    # genuinely missing issue and can operate again after a restore.  Only
    # lifecycle rows belonging to the authenticated tenant/user are expanded;
    # every issue is still checked by ``_issue_matches_owner_context`` below.
    try:
        lifecycle_rows = workdir_utils.list_workspace_states(
            ServerState.get_workdir(),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
    except Exception:
        lifecycle_rows = []
    for row in lifecycle_rows:
        workspace_id = str(row.get("workspace_id") or "").strip()
        if not workspace_id:
            continue
        try:
            hidden_workdir = workdir_utils.resolve_user_workdir_path(
                ServerState.get_workdir(),
                workspace_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
            )
        except Exception:
            continue
        _append(hidden_workdir)

    # Old routed jobs may live in the server-root tracker.  Owner verification
    # below prevents this compatibility fallback from crossing tenants/users.
    _append(ServerState.get_workdir())
    return candidates


def _issue_managed_workspace_context(
    issue: dict[str, Any],
    *,
    store_workdir: str = "",
) -> Optional[workdir_utils.WorkspaceContext]:
    """Resolve one issue to its persisted managed Workspace, if provable."""

    workspace = (
        issue.get("workspace")
        if isinstance(issue.get("workspace"), dict)
        else {}
    )
    explicit_tenant = str(workspace.get("tenant_id") or "").strip()
    explicit_user = str(workspace.get("user_id") or "").strip()
    explicit_workspace = str(workspace.get("workspace_id") or "").strip()
    if explicit_tenant and explicit_user and explicit_workspace:
        return workdir_utils.WorkspaceContext(
            tenant_id=workdir_utils.sanitize_owner_id(
                explicit_tenant,
                workdir_utils.default_tenant_id(),
            ),
            user_id=workdir_utils.sanitize_owner_id(
                explicit_user,
                workdir_utils.default_user_id(),
            ),
            workspace_id=(
                workdir_utils.sanitize_workdir_name(explicit_workspace)
                or workdir_utils.default_workspace_id()
            ),
        )

    protein_run = (
        workspace.get("protein_run")
        if isinstance(workspace.get("protein_run"), dict)
        else {}
    )
    for path_text in (
        workspace.get("workdir"),
        workspace.get("control_workdir"),
        protein_run.get("control_workdir"),
        store_workdir,
    ):
        owner = workdir_utils.workspace_context_from_managed_path(
            ServerState.get_workdir(),
            path_text,
        )
        if owner is not None:
            return owner
    return None


async def _candidate_local_issue_store_workdirs(
    session_id: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> list[str]:
    candidates: list[str] = []

    def _append(workdir: str) -> None:
        text = str(workdir or "").strip()
        if not text:
            return
        for existing in candidates:
            if _same_resolved_workdir(existing, text):
                return
        candidates.append(text)

    normalized = str(session_id or "").strip()
    if normalized:
        try:
            resolved_session_workdir = await _resolve_local_issue_store_workdir(
                normalized,
                context,
            )
            _append(resolved_session_workdir)

            # workspace workdir（如 tcode/workdirs/admin_tenant/local-user/workspace-3）里的异步任务
            # 可能实际写入主 tcode/tmp/shared_tracker.sqlite，需要把主 workdir
            # 一并纳入候选，否则“异步任务列表”会在任务完成后变空。
            current = Path(str(resolved_session_workdir or "").strip())
            for parent in (current, *current.parents):
                if parent.name == "workdirs":
                    _append(parent.parent.as_posix())
                    break
        except Exception:
            pass

    if context is not None:
        try:
            _append(_resolve_user_workdir_path(context.workspace_id, context).as_posix())
        except Exception:
            pass

    # Some older routed jobs were persisted in the server-root tracker. Keep
    # that store visible, but filter every row by its persisted owner below.
    _append(ServerState.get_workdir())
    if context is None:
        _append(Path(__file__).resolve().parents[2].as_posix())
    return candidates


async def _load_visible_symphony_issues(
    *,
    session_id: str = "",
    filter_session_ids: Optional[list[str]] = None,
    workspace_id: str = "",
    limit: int = 50,
    query: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    safe_limit = max(1, min(500, int(limit)))
    normalized_session = str(session_id or "").strip()
    candidate_workdirs = await _candidate_local_issue_store_workdirs(
        normalized_session,
        context,
    )
    workspace_roots = list(candidate_workdirs)

    merged: dict[str, dict[str, Any]] = {}
    used_paths: list[str] = []

    for index, workdir in enumerate(candidate_workdirs):
        store = _load_symphony_memory_issue_store(workdir)
        issues = store.get("issues", [])
        if not isinstance(issues, list) or not issues:
            continue

        if normalized_session and index > 0:
            issues = [
                issue
                for issue in issues
                if isinstance(issue, dict)
                and (
                    _issue_matches_session(issue, normalized_session)
                    or _issue_matches_workspace(issue, workspace_roots)
                )
            ]
            if not issues:
                continue

        used_paths.append(_shared_tracker_db_path_text(workdir))
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if not _issue_matches_workspace_context(
                issue,
                context,
                store_workdir=workdir,
            ):
                continue
            key = _issue_lookup_key(issue)
            if not key:
                continue
            current = merged.get(key)
            if current is None or _issue_timestamp_value(issue) >= _issue_timestamp_value(current):
                merged[key] = dict(issue)

    ordered = sorted(
        merged.values(),
        key=lambda issue: (
            _issue_timestamp_value(issue),
            str(issue.get("identifier", "") or issue.get("id", "") or ""),
        ),
        reverse=True,
    )
    normalized_filter_sessions = {
        str(item or "").strip()
        for item in (filter_session_ids or [])
        if str(item or "").strip()
    }
    if normalized_filter_sessions:
        ordered = [
            issue
            for issue in ordered
            if any(
                _issue_matches_session(issue, candidate)
                for candidate in normalized_filter_sessions
            )
        ]

    normalized_workspace_id = str(workspace_id or "").strip()
    if normalized_workspace_id:
        ordered = [
            issue
            for issue in ordered
            if _issue_workspace_id(issue) == normalized_workspace_id
        ]

    tokens = str(query or "")[:200].casefold().split()
    if tokens:
        ordered = [issue for issue in ordered if all(token in " ".join(
            str(issue.get(key) or "") for key in ("id", "identifier", "title", "state", "result_summary")
        ).casefold() for token in tokens)]
    if not used_paths:
        used_paths = [_shared_tracker_db_path_text(workdir) for workdir in candidate_workdirs]
    return ordered[:safe_limit], len(ordered), used_paths


async def _locate_symphony_issue_store_entry(
    *,
    issue_id: str = "",
    issue_identifier: str = "",
    session_id: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
    owner_wide: bool = False,
) -> Optional[tuple[str, dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]]:
    candidates = (
        _owner_local_issue_store_workdirs(context)
        if owner_wide and context is not None
        else await _candidate_local_issue_store_workdirs(session_id, context)
    )
    matches: list[tuple[str, dict[str, Any], list[dict[str, Any]], int, dict[str, Any]]] = []

    for workdir in candidates:
        store = _load_symphony_memory_issue_store(workdir)
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            continue
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                continue
            allowed = (
                await _issue_matches_owner_context(
                    issue,
                    context,
                    store_workdir=workdir,
                )
                if owner_wide
                else _issue_matches_workspace_context(
                    issue,
                    context,
                    store_workdir=workdir,
                )
            )
            if not allowed:
                continue
            if _issue_reference_matches(
                issue,
                issue_id=issue_id,
                issue_identifier=issue_identifier,
            ):
                matches.append((workdir, store, issues, index, issue))
                break

    if not matches:
        return None

    matches.sort(key=lambda item: _issue_timestamp_value(item[4]), reverse=True)
    return matches[0]


async def _locate_deleted_symphony_issue(
    *,
    issue_id: str = "",
    issue_identifier: str = "",
    context: workdir_utils.WorkspaceContext,
) -> Optional[dict[str, Any]]:
    """Find an owner-scoped deletion tombstone for idempotent DELETE calls."""

    matches: list[dict[str, Any]] = []
    for workdir in _owner_local_issue_store_workdirs(context):
        try:
            tasks = SharedTracker.list_tasks(
                workdir,
                kind="symphony_local_issue",
                include_deleted=True,
            )
        except Exception:
            continue
        for task in tasks:
            if (
                not isinstance(task, dict)
                or not str(task.get("deleted_at") or "").strip()
            ):
                continue
            if not _issue_reference_matches(
                task,
                issue_id=issue_id,
                issue_identifier=issue_identifier,
            ):
                continue
            if not await _issue_matches_owner_context(
                task,
                context,
                store_workdir=workdir,
            ):
                continue
            matches.append(task)
    if not matches:
        return None
    matches.sort(key=_issue_timestamp_value, reverse=True)
    return matches[0]


def _pid_running_local(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        proc = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        stat = str(proc.stdout or "").strip()
        if not stat:
            return False
        if stat.startswith("Z"):
            return False
    except Exception:
        pass
    return True


def _terminate_pid_group_local(
    pid: int, wait_seconds: float = 5.0, force: bool = True
) -> tuple[bool, str]:
    """Best-effort terminate a process group started with start_new_session=True."""
    if pid <= 0:
        return False, "invalid_pid"
    if not _pid_running_local(pid):
        return False, "not_running"

    def _send_signal(sig: int, label: str) -> tuple[bool, str]:
        direct_errors: list[str] = []
        try:
            os.kill(pid, sig)
            return True, f"{label}_pid"
        except ProcessLookupError:
            return False, "not_running"
        except PermissionError:
            direct_errors.append("permission_denied_pid")
        except Exception as exc:
            direct_errors.append(f"{label}_pid_failed:{type(exc).__name__}")

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return False, "not_running"
        except PermissionError:
            pgid = None
        except Exception:
            pgid = None

        group_errors: list[str] = []
        if pgid and pgid > 0:
            try:
                os.killpg(pgid, sig)
                return True, f"{label}_group"
            except ProcessLookupError:
                return False, "not_running"
            except PermissionError:
                group_errors.append("permission_denied_group")
            except Exception as exc:
                group_errors.append(f"{label}_group_failed:{type(exc).__name__}")
        if direct_errors or group_errors:
            return False, ",".join(direct_errors + group_errors)
        return False, f"{label}_unknown_failure"

    sent, reason = _send_signal(signal.SIGTERM, "sigterm")
    if not sent:
        return False, reason

    deadline = datetime.utcnow().timestamp() + max(0.2, float(wait_seconds))
    while datetime.utcnow().timestamp() < deadline:
        if not _pid_running_local(pid):
            return True, "terminated"
        import time

        time.sleep(0.1)

    if not force:
        return False, "sigterm_timeout"

    sent, reason = _send_signal(signal.SIGKILL, "sigkill")
    if not sent:
        return False, reason

    deadline = datetime.utcnow().timestamp() + 2.0
    while datetime.utcnow().timestamp() < deadline:
        if not _pid_running_local(pid):
            return True, "killed"
        import time

        time.sleep(0.1)
    return False, "sigkill_timeout"


def _extract_issue_job_manifest(issue: dict[str, Any]) -> dict[str, Any]:
    submit_input = _extract_submit_input_from_description(
        str(issue.get("description", "") or "")
    )
    workdir = str(submit_input.get("workdir", "") or "").strip()
    if not workdir:
        return {}
    manifest_path = Path(workdir) / "job_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


async def _cancel_active_issue(issue: dict[str, Any], *, workdir: str = "") -> dict[str, Any]:
    """Persist intent first; background reconciliation owns retries after disconnect."""
    from ..runtime.remote_recovery import reconcile_task

    tracker_workdir = workdir or ServerState.get_workdir()
    task_id = str(issue.get("id") or "").strip()
    try:
        SharedTracker.request_cancellation(task_id=task_id, workdir=tracker_workdir)
    except ValueError as exc:
        raise HTTPException(404, "task not found") from exc
    await reconcile_task(task_id, tracker_workdir)
    latest = SharedTracker.get_issue(task_id=task_id, workdir=tracker_workdir)
    if latest:
        issue.update(latest)
    cancellation = (issue.get("workspace") or {}).get("cancellation") or {}
    return {"cancelled": issue.get("state") == "Cancelled",
            "pending": cancellation.get("state") == "pending", "actions": []}


async def _cancel_symphony_issue_by_identifier(
    identifier: str,
    *,
    session_id: str = "",
    expected_protein_run_id: str = "",
    expected_control_workdir: str = "",
) -> dict[str, Any]:
    """Cancel one linked issue with a terminal-state compare-and-set guard."""

    normalized = str(identifier or "").strip()
    if not normalized:
        raise HTTPException(400, "issue identifier is required")
    located = await _locate_symphony_issue_store_entry(
        issue_identifier=normalized,
        session_id=str(session_id or "").strip(),
    )
    if located is None:
        raise HTTPException(404, f"local issue not found: {normalized}")
    issue_store_workdir, store, issues, _, issue = located
    if expected_protein_run_id:
        try:
            from ..workspace.async_protein_run import extract_link

            protein_run_link = extract_link(issue)
        except Exception:
            protein_run_link = {}
        linked_run_id = str(
            protein_run_link.get("workspace_id")
            or protein_run_link.get("run_id")
            or ""
        ).strip()
        linked_control_workdir = str(
            protein_run_link.get("control_workdir") or ""
        ).strip()
        if linked_run_id != str(expected_protein_run_id).strip():
            raise HTTPException(409, "异步任务与当前 Protein Run 的关联校验失败")
        if expected_control_workdir:
            try:
                same_control_workdir = (
                    Path(linked_control_workdir).expanduser().resolve(strict=False)
                    == Path(expected_control_workdir).expanduser().resolve(strict=False)
                )
            except (OSError, RuntimeError, ValueError):
                same_control_workdir = False
            if not same_control_workdir:
                raise HTTPException(409, "异步任务与当前 Workspace 的关联校验失败")
    current_state = str(issue.get("state") or "").strip()
    if _is_terminal_issue_state(current_state):
        raise HTTPException(
            409,
            f"任务已经处于终态 {current_state or 'unknown'}，取消请求未生效。",
        )
    cancellation = await _cancel_active_issue(issue, workdir=issue_store_workdir)
    store["issues"] = issues
    _save_symphony_memory_issue_store(store, issue_store_workdir)
    refresh_ok = await _trigger_symphony_refresh()
    return {
        "status": "cancel_requested",
        "issue": issue,
        "cancellation": cancellation,
        "refresh_triggered": refresh_ok,
    }


async def _cancel_symphony_issues_for_session(
    session_id: str,
) -> dict[str, list[str] | int]:
    if not session_id:
        return {"count": 0, "identifiers": []}
    identifiers: list[str] = []
    changed = False
    seen_keys: set[str] = set()

    for workdir in await _candidate_local_issue_store_workdirs(session_id):
        store = _load_symphony_memory_issue_store(workdir)
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            continue

        store_changed = False
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if not _issue_matches_session(issue, session_id):
                continue
            issue_key = _issue_lookup_key(issue)
            if issue_key and issue_key in seen_keys:
                continue
            if _is_terminal_issue_state(issue.get("state")):
                if issue_key:
                    seen_keys.add(issue_key)
                continue
            await _cancel_active_issue(issue, workdir=workdir)
            identifiers.append(str(issue.get("identifier", "") or ""))
            store_changed = True
            changed = True
            if issue_key:
                seen_keys.add(issue_key)

        if store_changed:
            store["issues"] = issues
            _save_symphony_memory_issue_store(store, workdir)

    if changed:
        await _trigger_symphony_refresh()
    return {"count": len(identifiers), "identifiers": identifiers}


def _extract_result_summary_from_mcp_result(data: dict[str, Any]) -> tuple[str, str]:
    """Return (state, summary) from mcp_result.json payload."""
    is_error = bool(data.get("isError"))
    structured = data.get("structuredContent")

    def _extract_business_failure_reason(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None

        for key in ("status", "state", "result_status"):
            value = payload.get(key)
            text = str(value or "").strip().lower()
            if text in {"failed", "error", "cancelled", "canceled", "terminated", "timed_out"}:
                return f"{key}={value}"

        for key in ("result_code", "validated", "success", "ok"):
            if key in payload and payload.get(key) is False:
                return f"{key}=false"

        for nested_key in ("structuredContent", "result", "data"):
            nested = payload.get(nested_key)
            reason = _extract_business_failure_reason(nested)
            if reason:
                return reason

        return None

    def _shorten(text: str, limit: int = 220) -> str:
        line = str(text or "").strip().replace("\r", "").split("\n", 1)[0].strip()
        if len(line) <= limit:
            return line
        return line[: limit - 3] + "..."

    def _compact_error_reason() -> str:
        candidates: list[str] = []
        if isinstance(structured, dict):
            for key in ("error", "message", "detail", "reason", "stderr", "traceback"):
                value = structured.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
                elif isinstance(value, dict):
                    nested = value.get("message") if isinstance(value, dict) else None
                    if isinstance(nested, str) and nested.strip():
                        candidates.append(nested)

        content = data.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        candidates.append(text)
                elif isinstance(item, str) and item.strip():
                    candidates.append(item)

        for raw in candidates:
            text = _shorten(raw)
            lower = text.lower()
            if "json parse failed" in lower or "submit_job.input" in lower:
                return "submit_job.input JSON 解析失败（常见于命令引号/转义问题）"
            if "missing required argument" in lower:
                return "参数缺失（请检查 input_config 与必填字段）"
            if "module not found" in lower or "modulenotfounderror" in lower:
                return "运行环境缺少依赖或 PYTHONPATH 配置异常"
            return text

        return "任务执行失败（未返回明确错误细节）"

    summary = ""
    if is_error:
        summary = _compact_error_reason()
    elif isinstance(structured, (dict, list)):
        summary = json.dumps(structured, ensure_ascii=False)
    else:
        content = data.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                summary = str(first.get("text", ""))
            else:
                summary = str(first)
        else:
            summary = json.dumps(data, ensure_ascii=False)

    business_failure_reason = _extract_business_failure_reason(structured)
    if not business_failure_reason:
        business_failure_reason = _extract_business_failure_reason(data.get("result"))

    max_len = 260 if (is_error or business_failure_reason) else 500
    state = "Failed" if (is_error or business_failure_reason) else "Done"
    if business_failure_reason and summary:
        summary = f"{summary}\n[detected_failure] {business_failure_reason}"
    return (state, summary[:max_len])


@lru_cache(maxsize=128)
def _read_job_result_summary(
    path: Path, signature: tuple[int, ...],
) -> Optional[tuple[str, str]]:
    # Cache only the small derived summary; changed artifacts are parsed again.
    data = json.loads(path.read_text(encoding="utf-8"))
    return _extract_result_summary_from_mcp_result(data) if isinstance(data, dict) else None


def _extract_result_summary_from_job_artifacts(workdir: Path) -> Optional[tuple[str, str]]:
    result_file = workdir / "mcp_result.json"
    if result_file.exists():
        try:
            result_file = result_file.resolve()
            stat = result_file.stat()
            summary = _read_job_result_summary(result_file, (
                stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
            ))
            if summary is not None:
                return summary
        except Exception:
            return None

    exitcode_files = sorted(workdir.glob("*.exitcode"))
    if not exitcode_files:
        return None

    try:
        exit_code = int(exitcode_files[0].read_text(encoding="utf-8").strip() or "0")
    except Exception:
        exit_code = None

    if exit_code is None:
        return None

    if exit_code == 0:
        return ("Done", "任务进程退出码为 0，但未生成 mcp_result.json")

    log_excerpt = ""
    log_files = sorted(workdir.glob("*.log"))
    if log_files:
        try:
            log_lines = log_files[0].read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [line.strip() for line in log_lines[-20:] if line.strip()]
            if tail:
                log_excerpt = tail[-1]
        except Exception:
            log_excerpt = ""

    summary = f"任务进程退出码为 {exit_code}，且未生成 mcp_result.json"
    if log_excerpt:
        summary = f"{summary}。最后日志: {log_excerpt}"
    return ("Failed", summary[:260])


def _reconcile_symphony_memory_issue_store(issues: list[dict[str, Any]]) -> bool:
    """Best-effort reconciliation for artifact-backed local issues.
    If artifact exists, derive terminal state from mcp_result.json."""
    changed = False
    now = datetime.utcnow().isoformat() + "Z"

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        workspace = _build_issue_workspace_summary(issue)
        if issue.get("workspace") != workspace:
            issue["workspace"] = workspace
            changed = True
        state = str(issue.get("state", "")).strip()
        if state not in {"In Progress", "Done", "Failed"}:
            continue

        description = str(issue.get("description", ""))
        m = re.search(r'"workdir"\s*:\s*"([^"]+)"', description)
        if not m:
            continue

        workdir = Path(m.group(1))
        try:
            artifact_state = _extract_result_summary_from_job_artifacts(workdir)
            if artifact_state is None:
                continue
            new_state, summary = artifact_state
            if (
                str(issue.get("state", "")).strip() != new_state
                or str(issue.get("result_summary", "") or "").strip() != summary
            ):
                issue["state"] = new_state
                issue["result_summary"] = summary
                issue["updated_at"] = now
                changed = True
        except Exception:
            continue

    return changed


def _load_symphony_memory_issue_store(workdir: Optional[str] = None) -> dict[str, Any]:
    resolved_workdir = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    try:
        data = SharedTracker.load_issue_store(resolved_workdir)
        issues = data.get("issues", []) if isinstance(data, dict) else []
        if not isinstance(issues, list):
            issues = []
        try:
            if _reconcile_symphony_memory_issue_store(issues):
                SharedTracker.sync_issue_store({"issues": issues}, resolved_workdir)
        except Exception as sync_error:
            print(f"[symphony-issues] reconcile/sync failed: {sync_error}")
        return {"issues": issues}
    except Exception as load_error:
        print(f"[symphony-issues] load failed: {load_error}")
        return {"issues": []}


def _save_symphony_memory_issue_store(store: dict[str, Any], workdir: Optional[str] = None):
    resolved_workdir = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    SharedTracker.sync_issue_store(store, resolved_workdir)


def _symphony_refresh_timeout_seconds() -> float:
    """返回 Symphony 刷新请求使用的有界总超时秒数。

    环境变量 ``TCODE_SYMPHONY_REFRESH_TIMEOUT_SECONDS`` 可覆盖默认的三秒，
    但最终值固定限制在 0.1 到 30 秒。非法值、非有限值或非正数都回退
    到默认值，防止配置错误重新引入无界 HTTP 等待。
    """

    default_seconds = 3.0
    raw_value = str(
        os.environ.get("TCODE_SYMPHONY_REFRESH_TIMEOUT_SECONDS", "") or ""
    ).strip()
    if not raw_value:
        return default_seconds
    try:
        timeout_seconds = float(raw_value)
    except (TypeError, ValueError):
        return default_seconds
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return default_seconds
    return max(0.1, min(timeout_seconds, 30.0))


async def _trigger_symphony_refresh() -> bool:
    """有界触发 Symphony 刷新，并把连接或响应失败降级为 ``False``。

    刷新只是本地 Issue 写入后的尽力通知，持久化结果不依赖远端服务可用。
    因此网络错误和超时继续沿用原有的布尔降级语义；任务取消仍由 asyncio
    正常向上传播，不会被本函数吞掉。
    """

    base_url = os.environ.get("SYMPHONY_API_BASE", "http://127.0.0.1:7890").rstrip("/")
    timeout_seconds = _symphony_refresh_timeout_seconds()
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=min(1.0, timeout_seconds),
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base_url}/api/v1/refresh", json={}) as resp:
                return resp.status < 400
    except Exception:
        return False


async def _resolve_local_issue_store_workdir(
    session_id: str = "",
    context: Optional[workdir_utils.WorkspaceContext] = None,
) -> str:
    normalized = str(session_id or "").strip()
    if not normalized:
        if context is not None:
            return _resolve_user_workdir_path(context.workspace_id, context).as_posix()
        return ServerState.get_workdir()
    try:
        resolved = await _resolve_session_workdir(normalized, context)
    except Exception:
        resolved = ""
    return str(resolved or ServerState.get_workdir()).strip() or ServerState.get_workdir()


async def _refresh_runtime_providers():
    for session_id, provider in list(ServerState.providers.items()):
        close_fn = getattr(provider, "close", None)
        if close_fn:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        ServerState._running[session_id] = False

    ServerState.providers.clear()
    ServerState.agents.clear()


class ImmutableStaticFiles(StaticFiles):
    """Serve Vite's content-hashed assets with long-lived browser caching."""

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in {200, 304}:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _web_index_response(path: Path) -> FileResponse:
    response = FileResponse(path)
    # index.html contains content-hashed asset names and must be revalidated on
    # every launch. Otherwise an old cached HTML file can point at deleted assets.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


class TrackerClaimAccessFilter(logging.Filter):
    """Hide successful worker claim polling while preserving errors and API logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if str(os.environ.get("TCODE_TRACKER_POLL_ACCESS_LOG", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return True
        try:
            args = record.args
            method = str(args[1]).upper()
            path = str(args[2]).split("?", 1)[0]
            status = int(args[4])
        except (IndexError, KeyError, TypeError, ValueError):
            return True
        return not (
            method == "POST"
            and path == "/api/tracker/claim-next"
            and 200 <= status < 400
        )


def uvicorn_log_config() -> dict[str, Any]:
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    filter_name = "hide_successful_tracker_claim_polling"
    config.setdefault("filters", {})[filter_name] = {"()": TrackerClaimAccessFilter}
    access_handler = config.setdefault("handlers", {}).get("access")
    if isinstance(access_handler, dict):
        handler_filters = list(access_handler.get("filters") or [])
        if filter_name not in handler_filters:
            handler_filters.append(filter_name)
        access_handler["filters"] = handler_filters
    return config


@app.get("/")
async def root():
    return _web_index_response(frontend_dist / "index.html")


@app.get("/desktop.html")
async def desktop_page():
    page = frontend_dist / "desktop.html"
    if not page.is_file():
        raise HTTPException(404, "Desktop frontend is not built")
    return _web_index_response(page)


# 源码优先使用 web/dist；wheel 和容器使用随 Python 包发布的静态资源。
source_web_dist = Path(__file__).parent.parent.parent / "web" / "dist"
packaged_web_dist = Path(__file__).parent / "static"
frontend_dist = source_web_dist if (source_web_dist / "index.html").is_file() else packaged_web_dist
if (frontend_dist / "assets").is_dir():
    app.mount("/assets", ImmutableStaticFiles(directory=frontend_dist / "assets"), name="assets")
if (frontend_dist / "vendor").is_dir():
    app.mount("/vendor", ImmutableStaticFiles(directory=frontend_dist / "vendor"), name="viewer-vendor")


@app.get("/molstar.html")
async def molstar_viewer_page():
    return _web_index_response(frontend_dist / "molstar.html")


__all__ = [name for name in globals() if not name.startswith("__")]
