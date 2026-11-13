"""Application assembly, lifecycle hooks and compatibility exports.

Domain routers own their endpoints. Existing native adapters and Python callers
can still import entry points here; new code should import the owning module.
See docs/MAINTENANCE.md for the module map.
"""

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import stat as stat
from dataclasses import (
    fields as fields,
    replace as replace,
)
from datetime import (
    datetime as datetime,
    timezone as timezone,
)
from pathlib import (
    Path as Path,
)

from ..browser import (
    browser_live_paths as browser_live_paths,
    read_browser_live_state as read_browser_live_state,
    safe_browser_session_id as safe_browser_session_id,
)
from ..guard.outcome_guard import (
    validate_outcome as validate_outcome,
)
from ..presentation import (
    normalize_content_blocks as normalize_content_blocks,
)
from ..protocol import (
    permission_request_event as permission_request_event,
    tool_using_event as tool_using_event,
    usage_status_event as usage_status_event,
)
from ..protocol.v2 import (
    RuntimeEventEmitter as RuntimeEventEmitter,
    runtime_message_from_legacy as runtime_message_from_legacy,
)
from ..protocol.v2.adapters import (
    TCodeWebSocketAdapter as TCodeWebSocketAdapter,
)
from ..session.store_sqlite import SessionSQLiteStore
from ..research import (
    ResearchDeadLetterReplayer as ResearchDeadLetterReplayer,
    ResearchEventStore as ResearchEventStore,
    ResearchPackIndexScheduler as ResearchPackIndexScheduler,
    load_research_settings as load_research_settings,
    normalize_research_chat_context as normalize_research_chat_context,
    selected_asset_ids as selected_asset_ids,
)
from ..runtime.agent_backend import (
    NormalizedMessage as NormalizedMessage,
    NormalizedRunInput as NormalizedRunInput,
    RunContext as RunContext,
)
from ..runtime.capacity import (
    CapacityRejected as CapacityRejected,
    ExecutionCapacityController as ExecutionCapacityController,
)
from ..runtime.evidence_ledger import (
    EvidenceLedger as EvidenceLedger,
)
from ..runtime.gateway import (
    agent_backend_registry as agent_backend_registry,
    guidance_mode_for_runtime as guidance_mode_for_runtime,
    run_coordinator as run_coordinator,
    runtime_registry as runtime_registry,
)
from ..runtime.guidance_store import (
    GuidanceSQLiteStore as GuidanceSQLiteStore,
)
from ..runtime.interaction_store import (
    InteractionConflictError as InteractionConflictError,
    InteractionNotFoundError as InteractionNotFoundError,
)
from ..runtime.model_policy import (
    ModelPolicyError as ModelPolicyError,
    native_model_ids as native_model_ids,
    resolve_runtime_model as resolve_runtime_model,
)
from ..runtime.run_coordinator import (
    RunConflictError as RunConflictError,
    RunUnavailableError as RunUnavailableError,
)
from ..runtime.sandbox import (
    get_sandbox_runtime as get_sandbox_runtime,
    load_sandbox_config as load_sandbox_config,
    sandbox_enabled as sandbox_enabled,
)
from ..runtime.telemetry import (
    TelemetryGateway as TelemetryGateway,
    redact_telemetry_text as redact_telemetry_text,
)
from ..scheduled_tasks import (
    get_scheduled_task_service as get_scheduled_task_service,
)
from ..session.events import (
    SessionEventStore as SessionEventStore,
)
from ..session.transcript import (
    without_legacy_native_agui_user_echoes as without_legacy_native_agui_user_echoes,
)
from ..task.outcome_contract import (
    OutcomeContract as OutcomeContract,
)
from ..task.outcome_revision import (
    OutcomeContractRevisionPlanner as OutcomeContractRevisionPlanner,
    invalidate_superseded_message_verifications as invalidate_superseded_message_verifications,
)
from ..workflow import (
    WorkflowStageState as WorkflowStageState,
)
from ..workflow.applicability import (
    assess_workflow_applicability as assess_workflow_applicability,
    workflow_original_request as workflow_original_request,
)
from . import (
    agui_api as agui_api,
    auth_api as auth_api,
    browser_live_api as browser_live_api,
    catalog_api as catalog_api,
    chat_control_api as chat_control_api,
    chat_socket as chat_socket,
    evidence_api as evidence_api,
    files_api as files_api,
    structure_api as structure_api,
    harness_api as harness_api,
    human_review_api as human_review_api,
    intelligent_ui_api as intelligent_ui_api,
    job_artifacts_api as job_artifacts_api,
    kanban_api as kanban_api,
    mcp_apps_api as mcp_apps_api,
    outcome_api as outcome_api,
    paper_library_api as paper_library_api,
    sandbox_api as sandbox_api,
    sessions_api as sessions_api,
    skills_api as skills_api,
    subagents_api as subagents_api,
    symphony_api as symphony_api,
    tracker_api as tracker_api,
    wiki_api as wiki_api,
    workflow_api as workflow_api,
    workspaces_api as workspaces_api,
)
from ._shared import (
    _ASYNC_ISSUE_AUTO_RESUME_TASK as _ASYNC_ISSUE_AUTO_RESUME_TASK,
    _ASYNC_WATCHER_TASK as _ASYNC_WATCHER_TASK,
    Agent as Agent,
    AgentMode as AgentMode,
    Any as Any,
    BuiltinAgents as BuiltinAgents,
    ConfigManager as ConfigManager,
    CurrentUserContext as CurrentUserContext,
    Dict as Dict,
    EmbeddingProvider as EmbeddingProvider,
    FaissVectorStore as FaissVectorStore,
    HTTPException as HTTPException,
    MCPManager as MCPManager,
    MemoryManager as MemoryManager,
    Message as Message,
    Optional as Optional,
    PluginCommandRegistry as PluginCommandRegistry,
    PluginManager as PluginManager,
    Request as Request,
    Response as Response,
    RuntimeArtifactManager as RuntimeArtifactManager,
    RuntimeTaskManager as RuntimeTaskManager,
    ServerState as ServerState,
    Session as Session,
    SessionArchiveManager as SessionArchiveManager,
    SessionManager as SessionManager,
    SharedTracker as SharedTracker,
    SkillManager as SkillManager,
    TaskRuntimeStore as TaskRuntimeStore,
    TaskStateGraphManager as TaskStateGraphManager,
    TeamCatalog as TeamCatalog,
    TeamChannel as TeamChannel,
    ToolContext as ToolContext,
    WorkerRegistry as WorkerRegistry,
    WorkflowCandidate as WorkflowCandidate,
    WorkflowRouteDecision as WorkflowRouteDecision,
    WorkflowRun as WorkflowRun,
    WorkflowRunManager as WorkflowRunManager,
    WorkflowStageTemplate as WorkflowStageTemplate,
    WorkflowTemplate as WorkflowTemplate,
    WorkflowTemplateRegistry as WorkflowTemplateRegistry,
    WorkflowVersionConflictError as WorkflowVersionConflictError,
    _active_workflow_run_for_session as _active_workflow_run_for_session,
    _append_message_to_session as _append_message_to_session,
    _async_issue_auto_resume_loop as _async_issue_auto_resume_loop,
    _attempt_server_side_workflow_auto_route as _attempt_server_side_workflow_auto_route,
    _bind_guidance_v2_to_agent as _bind_guidance_v2_to_agent,
    _bootstrap_fixed_team_workers as _bootstrap_fixed_team_workers,
    _cancel_symphony_issues_for_session as _cancel_symphony_issues_for_session,
    _candidate_human_review_store_workdirs as _candidate_human_review_store_workdirs,
    _chat_runtime_status_payload as _chat_runtime_status_payload,
    _cleanup_disconnected_chat_session as _cleanup_disconnected_chat_session,
    _confirm_workflow_auto_route as _confirm_workflow_auto_route,
    _delete_child_sessions as _delete_child_sessions,
    _ensure_delegated_agent_session as _ensure_delegated_agent_session,
    _ensure_session_guidance_drain_v2 as _ensure_session_guidance_drain_v2,
    _frontend_outcome_validation_enabled as _frontend_outcome_validation_enabled,
    _get_or_create_subagent_runtime_agent as _get_or_create_subagent_runtime_agent,
    _guidance_owner_scope_from_session as _guidance_owner_scope_from_session,
    _guidance_store_workdir as _guidance_store_workdir,
    _human_review_config_path as _human_review_config_path,
    _inject_workflow_declined_direct_instruction as _inject_workflow_declined_direct_instruction,
    _inject_workflow_discovery_instruction as _inject_workflow_discovery_instruction,
    _load_aggregated_human_review_queue as _load_aggregated_human_review_queue,
    _load_human_review_config as _load_human_review_config,
    _merge_resource_context as _merge_resource_context,
    _migrate_session_guidance_v2 as _migrate_session_guidance_v2,
    _refine_workflow_route_with_model as _refine_workflow_route_with_model,
    _resolve_paper_workflow_agent as _resolve_paper_workflow_agent,
    _run_subagent_turn_background as _run_subagent_turn_background,
    _select_or_create_auto_workflow_run as _select_or_create_auto_workflow_run,
    _symphony_refresh_timeout_seconds as _symphony_refresh_timeout_seconds,
    _terminate_session_processes as _terminate_session_processes,
    _trigger_symphony_refresh as _trigger_symphony_refresh,
    _workflow_auto_route_needs_confirmation as _workflow_auto_route_needs_confirmation,
    _workflow_recommended_primary_agent as _workflow_recommended_primary_agent,
    app as app,
    auth_required as auth_required,
    authenticate as authenticate,
    base64 as base64,
    cancel_worker_task as cancel_worker_task,
    clone_provider as clone_provider,
    copy as copy,
    create_provider as create_provider,
    current_user_from_request as current_user_from_request,
    detect_workflow_route as detect_workflow_route,
    ensure_auth_store as ensure_auth_store,
    guard_protein_design_brainstorming_route as guard_protein_design_brainstorming_route,
    inspect_image_file as inspect_image_file,
    is_protein_design_brainstorm_mode as is_protein_design_brainstorm_mode,
    math as math,
    parse_workflow_markers as parse_workflow_markers,
    question_request_event as question_request_event,
    question_timeout_event as question_timeout_event,
    re as re,
    root as root,
    should_auto_route_workflow_follow_up as should_auto_route_workflow_follow_up,
    socket as socket,
    strip_workflow_markers as strip_workflow_markers,
    subprocess as subprocess,
    swarm_router as swarm_router,
    uuid as uuid,
    uvicorn as uvicorn,
    uvicorn_log_config as uvicorn_log_config,
    workdir_utils as workdir_utils,
    worker_task_active as worker_task_active,
    workflow_routing_cards as workflow_routing_cards,
)
from .config_files import (
    _agent_mcp_config_path as _agent_mcp_config_path,
)
from .workspace_scope import (
    _resolve_session_workdir as _resolve_session_workdir,
    _resolve_user_workdir_path as _resolve_user_workdir_path,
    _resolve_workflow_workdir as _resolve_workflow_workdir,
    _runtime_session_matches_context as _runtime_session_matches_context,
    _session_owner_kwargs as _session_owner_kwargs,
    _session_owner_kwargs_from_session as _session_owner_kwargs_from_session,
    _user_workdirs_root as _user_workdirs_root,
    _workdir_name_from_path as _workdir_name_from_path,
    _workspace_context_from_request as _workspace_context_from_request,
)
from .catalog_api import (
    _resolve_agent_catalog_workdir as _resolve_agent_catalog_workdir,
    get_team_catalog as get_team_catalog,
    list_agents as list_agents,
    mcp_tools as mcp_tools,
)
from .chat_setup import (
    _initialize_chat_turn_runtime as _initialize_chat_turn_runtime,
)
from .guidance_runtime import (
    guidance_runtime as guidance_runtime,
)
from .issue_records import (
    _extract_submit_input_from_description as _extract_submit_input_from_description,
)
from .job_artifacts_api import (
    get_job_workspace as get_job_workspace,
)
from .job_workspace import (
    _read_json_file as _read_json_file,
)
from .kanban import (
    KanbanHiddenStore as KanbanHiddenStore,
)
from .linear_client import (
    _linear_list_projects as _linear_list_projects,
)
from .message_resources import (
    _build_message_resource_context as _build_message_resource_context,
)
from .outcome_api import (
    confirm_outcome_result as confirm_outcome_result,
)
from .plugin_warmup import (
    plugin_request_wait_seconds as plugin_request_wait_seconds,
    plugin_startup_wait_seconds as plugin_startup_wait_seconds,
    schedule_plugin_startup as schedule_plugin_startup,
    wait_for_plugin_startup as wait_for_plugin_startup,
    wait_for_plugin_warmup as wait_for_plugin_warmup,
)
from .session_lifecycle import (
    _SESSION_DELETE_ERRORS as _SESSION_DELETE_ERRORS,
    _SESSION_DELETE_PENDING as _SESSION_DELETE_PENDING,
    _SESSION_DELETE_TASKS as _SESSION_DELETE_TASKS,
    _delete_session_tree_background as _delete_session_tree_background,
    _schedule_session_delete as _schedule_session_delete,
)
from .session_presenter import (
    _serialize_frontend_tool_call as _serialize_frontend_tool_call,
)
from .sessions_api import (
    _serialize_session_summary as _serialize_session_summary,
    _sessions_with_runtime_usage_overlay as _sessions_with_runtime_usage_overlay,
    delete_session as delete_session,
    get_session as get_session,
    list_sessions as list_sessions,
)
from .subagents_api import (
    bootstrap_session as bootstrap_session,
)
from .wiki_shares import (
    WikiShareStore as WikiShareStore,
)
from .workflow_api import (
    _append_workflow_final_report_message_if_needed as _append_workflow_final_report_message_if_needed,
    _append_workflow_stage_narration_message_if_needed as _append_workflow_stage_narration_message_if_needed,
    _auto_continue_workflow_run_from_current_stage as _auto_continue_workflow_run_from_current_stage,
    _ensure_workflow_stage_agent_session as _ensure_workflow_stage_agent_session,
    _execute_workflow_stage_with_protein_worker as _execute_workflow_stage_with_protein_worker,
    _get_workflow_run_or_404 as _get_workflow_run_or_404,
    _recover_workflow_interrupted_stages as _recover_workflow_interrupted_stages,
    _reset_workflow_worker_session_messages as _reset_workflow_worker_session_messages,
    _schedule_workflow_auto_continue as _schedule_workflow_auto_continue,
    _send_workflow_assistant_message as _send_workflow_assistant_message,
    _send_workflow_main_tab_message as _send_workflow_main_tab_message,
    _start_workflow_auto_continue_background as _start_workflow_auto_continue_background,
    _upsert_workflow_parent_assistant_message as _upsert_workflow_parent_assistant_message,
    _upsert_workflow_stage_running_narration as _upsert_workflow_stage_running_narration,
    _workflow_auto_continue_on_create as _workflow_auto_continue_on_create,
    _workflow_completion_outcome_bundle as _workflow_completion_outcome_bundle,
    _workflow_completion_outcome_contract as _workflow_completion_outcome_contract,
    _workflow_reset_worker_context_enabled as _workflow_reset_worker_context_enabled,
    _workflow_run_ready_for_auto_continue as _workflow_run_ready_for_auto_continue,
    _workflow_session_owner_kwargs as _workflow_session_owner_kwargs,
    _workflow_stage_running_lease_heartbeat as _workflow_stage_running_lease_heartbeat,
    _workflow_status_message_id as _workflow_status_message_id,
    list_workflow_templates as list_workflow_templates,
    revalidate_workflow_completion as revalidate_workflow_completion,
)
from .workflow_dispatch import (
    discover_workflow as discover_workflow,
    prepare_workflow as prepare_workflow,
    resume_workflow_route as resume_workflow_route,
)
from .workspaces_api import (
    _WORKSPACE_PURGE_TASKS as _WORKSPACE_PURGE_TASKS,
    _purge_workspace_data as _purge_workspace_data,
    _quiesce_workspace_sessions as _quiesce_workspace_sessions,
    _record_targets_hidden_workspace as _record_targets_hidden_workspace,
    _resume_interrupted_workspace_purges as _resume_interrupted_workspace_purges,
    _terminate_workspace_tracker_jobs as _terminate_workspace_tracker_jobs,
    delete_workdir as delete_workdir,
    list_deleted_workdirs as list_deleted_workdirs,
    purge_workdir as purge_workdir,
)

_RESEARCH_STARTUP_MAINTENANCE_TASK: asyncio.Task | None = None
_RESEARCH_ROUTING_RETRY_TASK: asyncio.Task | None = None




app.include_router(auth_api.router)
from . import feishu_api
app.include_router(feishu_api.router)


app.include_router(sandbox_api.router)




app.include_router(browser_live_api.router)


app.include_router(chat_socket.router)


app.include_router(chat_control_api.router)


app.include_router(outcome_api.router)


@app.get("/api/chat/status/{session_id}")
async def chat_status(session_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    persisted = await SessionManager.get(
        session_id,
        **_session_owner_kwargs(workspace_context),
    )
    runtime = ServerState.sessions.get(session_id)
    runtime_matches = isinstance(runtime, dict) and _runtime_session_matches_context(
        runtime, workspace_context
    )
    if persisted is None and not runtime_matches:
        raise HTTPException(404, "Session not found")
    if persisted is not None:
        await _migrate_session_guidance_v2(persisted)
        if not ServerState.is_running(session_id):
            _ensure_session_guidance_drain_v2(session_id)
        owner_scope = _guidance_owner_scope_from_session(persisted)
        counts = await asyncio.to_thread(
            GuidanceSQLiteStore.pending_counts,
            session_id=session_id,
            owner=owner_scope,
            workdir=_guidance_store_workdir(),
        )
        pending_total = int(counts.get("pending_steer_count", 0)) + int(
            counts.get("pending_queue_count", 0)
        )
        return {
            **_chat_runtime_status_payload(session_id),
            "pending_guidance_count": pending_total,
            "pending_guidance_interrupt": bool(counts.get("pending_steer_count", 0)),
            "guidance_worker_active": pending_total > 0,
            **counts,
        }
    return _chat_runtime_status_payload(session_id)


app.include_router(workspaces_api.router)


app.include_router(evidence_api.router)


app.include_router(kanban_api.router)


app.include_router(sessions_api.router)


app.include_router(subagents_api.router)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "sessions": len(ServerState.sessions)}


app.include_router(catalog_api.router)
from .plugin_async_targets_api import router as plugin_async_targets_router
app.include_router(plugin_async_targets_router)


app.include_router(skills_api.router)
from . import goal_api
app.include_router(goal_api.router)


app.include_router(symphony_api.router)


app.include_router(tracker_api.router)


@app.get("/api/symphony/projects")
async def list_symphony_projects(search: Optional[str] = None):
    projects = await _linear_list_projects(search=search)
    return {
        "count": len(projects),
        "projects": projects,
    }


app.include_router(files_api.router)
from .artifact_services_api import router as artifact_services_router
app.include_router(artifact_services_router)
app.include_router(structure_api.router)
app.include_router(agui_api.router)
app.include_router(harness_api.router)
app.include_router(mcp_apps_api.router)
app.include_router(intelligent_ui_api.router)
from .research_compare_api import router as research_compare_router
app.include_router(research_compare_router)

app.include_router(job_artifacts_api.workspace_router)
app.include_router(workflow_api.router)
app.include_router(job_artifacts_api.router)


app.include_router(wiki_api.router)
app.include_router(paper_library_api.router)

app.include_router(human_review_api.router)

from . import ontology_api
app.include_router(ontology_api.router)


async def _execute_scheduled_agent_task(task: dict[str, Any]) -> str:
    """Run a confirmed scheduled instruction in its isolated persistent session."""

    instruction = str(task.get("instruction") or "").strip()
    compact_instruction = re.sub(r"[\s，,。.!！?？：:]", "", instruction)
    if re.search(r"(?:和我|跟我|向用户|给用户).*(?:打个?招呼|问候)", compact_instruction):
        return "你好！很高兴准时来和你打个招呼。"
    reminder_match = re.fullmatch(r"提醒用户[：:]?(.+)", instruction.strip())
    if reminder_match:
        return f"提醒：{reminder_match.group(1).strip()}"

    tenant_id = str(task.get("tenant_id") or workdir_utils.default_tenant_id())
    user_id = str(task.get("user_id") or "local-user")
    workspace_id = str(task.get("workspace_id") or "workspace-1")
    owner = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
    source_session_id = str(task.get("source_session_id") or "").strip()
    source_session = None
    if source_session_id:
        source_session = await SessionManager.get(source_session_id, **owner)

    execution_session_id = str(
        task.get("execution_session_id") or f"scheduled-{task.get('id')}"
    )
    workdir = str(
        task.get("workdir")
        or getattr(source_session, "workdir", "")
        or ServerState.get_workdir()
    ).strip()
    agent_name = str(task.get("agent_name") or "build").strip() or "build"
    if not BuiltinAgents.get(agent_name):
        agent_name = "build"

    persisted = await SessionManager.get(execution_session_id, **owner)
    if persisted is None:
        persisted = await SessionManager.create(
            session_id=execution_session_id,
            parent_id=source_session_id or "__scheduled_tasks__",
            title=f"定时任务 · {str(task.get('name') or '')[:100]}",
            agent_name=agent_name,
            workdir=workdir,
            **owner,
            metadata={
                "scheduled_task_id": str(task.get("id") or ""),
                "scheduled_task_session": True,
            },
        )

    runtime = ServerState.get_or_create_session(
        execution_session_id,
        agent_name,
        "supervisor",
        "standard",
        workdir=workdir,
    )
    runtime["tenant_id"] = tenant_id
    runtime["user_id"] = user_id
    runtime["workspace_id"] = workspace_id
    runtime["workdir"] = workdir
    agent = ServerState.get_or_create_agent(execution_session_id)
    agent._existing_session = persisted
    if not agent.messages and getattr(persisted, "messages", None):
        agent.messages = list(persisted.messages)

    context_lines: list[str] = []
    if source_session is not None:
        for message in list(getattr(source_session, "messages", []) or [])[-12:]:
            role = str(getattr(message, "role", "") or "")
            content = str(getattr(message, "content", "") or "").strip()
            if role in {"user", "assistant"} and content:
                context_lines.append(f"{role}: {content[:1800]}")
    source_context = "\n".join(context_lines)
    if len(source_context) > 12_000:
        source_context = source_context[-12_000:]

    prompt_parts = [
        "[已确认的定时任务到期执行]",
        f"任务名称：{task.get('name') or '定时任务'}",
        f"用户指令：{instruction}",
        "请现在执行该指令，并把最终可交付结果作为回复。不要创建、修改或删除任何定时任务。",
        "任务的时间条件已经满足；把指令中的“稍后”“一分钟后”“下周”等未来时态视为现在，不要再次等待或安排时间。",
        "如果操作需要新的交互式授权或关键信息仍然缺失，请明确说明无法执行的原因，不要猜测。",
    ]
    if source_context:
        prompt_parts.extend(["创建任务时关联会话的近期上下文：", source_context])
    prompt = "\n\n".join(prompt_parts)

    async def _reject_background_permission(_tool_name: str, _arguments: Dict[str, Any]):
        return "reject"

    async def _background_question(_payload: Dict[str, Any]):
        return "后台定时执行无法向用户追问；请停止依赖缺失信息的操作并清楚说明。"

    original_agent_info = agent.agent_info
    permission = dict(getattr(original_agent_info, "permission", {}) or {})
    permission["scheduled_task"] = "deny"
    try:
        scheduled_max_steps = max(
            1, min(int(os.environ.get("TCODE_SCHEDULE_MAX_AGENT_STEPS", "12")), 50)
        )
    except Exception:
        scheduled_max_steps = 12
    original_steps = getattr(original_agent_info, "steps", None)
    effective_steps = (
        min(int(original_steps), scheduled_max_steps)
        if original_steps
        else scheduled_max_steps
    )
    agent.agent_info = replace(
        original_agent_info,
        permission=permission,
        steps=effective_steps,
    )
    agent.set_execution_profile("fast")
    agent.set_permission_handler(_reject_background_permission)
    agent.set_question_handler(_background_question)
    agent.set_agent_event_emitter(None)
    try:
        await _initialize_chat_turn_runtime(runtime, fallback_workdir=workdir)
        result = await agent.run(prompt)
        await SessionManager.update(
            execution_session_id,
            **owner,
            messages=list(agent.messages),
            agent_name=agent_name,
            workdir=workdir,
            last_active_at=datetime.now().timestamp(),
        )
        output = str(getattr(result, "content", "") or "").strip()
        return output or "任务已执行完成。"
    finally:
        agent.agent_info = original_agent_info
        agent.set_permission_handler(None)
        agent.set_question_handler(None)
        agent.set_agent_event_emitter(None)


async def _notify_scheduled_task_completion(
    task: dict[str, Any],
    completion: dict[str, Any],
) -> None:
    """Persist one scheduled result in the originating user chat."""
    source_session_id = str(task.get("source_session_id") or "").strip()
    if not source_session_id:
        return
    owner = {
        "tenant_id": str(task.get("tenant_id") or workdir_utils.default_tenant_id()),
        "user_id": str(task.get("user_id") or "local-user"),
        "workspace_id": str(task.get("workspace_id") or "workspace-1"),
    }
    persisted = await SessionManager.get(source_session_id, **owner)
    if persisted is None:
        return

    run_id = str(completion.get("run_id") or "").strip()
    message_id = f"scheduled-run-{run_id}" if run_id else str(uuid.uuid4())
    if any(
        str(getattr(message, "id", "") or "") == message_id
        for message in list(getattr(persisted, "messages", []) or [])
    ):
        return

    success = bool(completion.get("success"))
    task_name = str(task.get("name") or "定时任务").strip() or "定时任务"
    body = str(
        (completion.get("output") if success else completion.get("error")) or ""
    ).strip()
    if not body:
        body = "任务已执行完成。" if success else "任务执行失败，请前往定时任务中心查看详情。"
    heading = f"⏰ 定时任务完成：{task_name}" if success else f"⚠️ 定时任务失败：{task_name}"
    await _append_message_to_session(
        source_session_id,
        Message(
            role="assistant",
            content=f"{heading}\n\n{body}",
            id=message_id,
        ),
        owner_kwargs=owner,
    )


async def _run_research_startup_maintenance() -> None:
    try:
        from ..research import (
            ResearchMaintenanceRunner,
            ResearchPackRecoveryManager,
            load_research_settings,
        )

        settings = load_research_settings()
        if not (
            (settings.journal_active and settings.auto_reconcile_on_startup)
            or settings.research_pack_import_recovery_enabled
            or settings.routing_active
        ):
            return
        server_workdir = ServerState.get_workdir()
        if settings.routing_active:
            from ..research.journal import ResearchEventSink
            sessions_for_recovery = await asyncio.to_thread(SessionSQLiteStore.list_workspace_metadata)

            def recover_routing_outboxes():
                candidates = [server_workdir, *[
                    str(session.get("workdir") or "")
                    for session in sessions_for_recovery
                    if not _record_targets_hidden_workspace(session, server_workdir=server_workdir)
                ]]
                storage_root = workdir_utils.workspace_storage_root(server_workdir)
                # Include existing workspaces with no session records as well.
                # Iterate all candidates; a fixed prefix cap would strand later
                # workspaces on every restart.
                for workspace_path in storage_root.glob("*/*/*"):
                    if not workspace_path.is_dir() or not workspace_path.resolve().is_relative_to(storage_root):
                        continue
                    context = workdir_utils.workspace_context_from_managed_path(server_workdir, workspace_path)
                    if context is not None and not workdir_utils.workspace_is_hidden(server_workdir, context):
                        candidates.append(str(workspace_path))
                ResearchEventSink.discover_routing_workspaces(candidates, settings=settings)
                ResearchEventSink.retry_routing_workspaces(settings=settings)

            await asyncio.to_thread(recover_routing_outboxes)
        if settings.journal_active and settings.auto_reconcile_on_startup:
            sessions = [
                session
                for session in await SessionManager.list()
                if not _record_targets_hidden_workspace(
                    session,
                    server_workdir=server_workdir,
                )
            ]
            await asyncio.to_thread(
                ResearchMaintenanceRunner.run,
                sessions,
                settings=settings,
            )
        if settings.research_pack_import_recovery_enabled:
            storage_root = workdir_utils.workspace_storage_root(server_workdir)

            def recover_workspace_databases() -> None:
                if not storage_root.is_dir():
                    return
                database_paths = list(
                    storage_root.glob("*/*/*/.tcode/research/research.sqlite3")
                )[:1000]
                for database_path in database_paths:
                    try:
                        relative = database_path.resolve(strict=False).relative_to(
                            storage_root
                        )
                        tenant_id, user_id, workspace_id = relative.parts[:3]
                        workspace_path = database_path.resolve(strict=False).parents[2]
                        workspace_context = (
                            workdir_utils.workspace_context_from_managed_path(
                                server_workdir,
                                workspace_path,
                            )
                        )
                        if (
                            workspace_context is not None
                            and workdir_utils.workspace_is_hidden(
                                server_workdir,
                                workspace_context,
                            )
                        ):
                            continue
                        ResearchPackRecoveryManager(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            workdir=workspace_path.as_posix(),
                            settings=settings,
                        ).run(recover_importing=True)
                    except Exception:
                        continue

            await asyncio.to_thread(recover_workspace_databases)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _research_routing_retry_loop() -> None:
    from ..research.journal import ResearchEventSink
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(ResearchEventSink.retry_routing_workspaces)


@app.on_event("startup")
async def startup_event():
    from .artifact_services_api import load_artifact_services
    load_artifact_services()
    from ..runtime.sandbox.base import strict_isolation_enabled
    from ..session.manager import SessionManager

    if strict_isolation_enabled():
        if not auth_required():
            raise RuntimeError("strict isolation requires user authentication")
        get_sandbox_runtime()
    guidance_runtime.start()
    if auth_required():
        ensure_auth_store()
    # Session recovery can replay a large canonical event projection on the
    # first access. Finish it before Uvicorn announces readiness and before the
    # desktop navigates away from its startup splash; otherwise the renderer
    # can time out /api/sessions and expose a composer backed by no live chat
    # transport (a false-ready workspace).
    session_recovery_started = asyncio.get_running_loop().time()
    await asyncio.to_thread(SessionManager._load_from_disk)
    print(
        "[startup] session projection ready "
        f"duration_ms={int((asyncio.get_running_loop().time() - session_recovery_started) * 1000)}"
    )
    schedule_plugin_startup(ServerState.get_workdir())
    await wait_for_plugin_startup(
        ServerState.get_workdir(),
        timeout=plugin_startup_wait_seconds(),
    )
    _resume_interrupted_workspace_purges()

    global _ASYNC_ISSUE_AUTO_RESUME_TASK, _ASYNC_ISSUE_AUTO_RESUME_LOCK, _ASYNC_WATCHER_TASK, _RESEARCH_STARTUP_MAINTENANCE_TASK
    global _RESEARCH_ROUTING_RETRY_TASK
    if _RESEARCH_ROUTING_RETRY_TASK is None or _RESEARCH_ROUTING_RETRY_TASK.done():
        _RESEARCH_ROUTING_RETRY_TASK = asyncio.create_task(_research_routing_retry_loop())
    SharedTracker.ensure_initialized(ServerState.get_workdir())
    _ASYNC_ISSUE_AUTO_RESUME_LOCK = asyncio.Lock()
    if (
        _RESEARCH_STARTUP_MAINTENANCE_TASK is None
        or _RESEARCH_STARTUP_MAINTENANCE_TASK.done()
    ):
        _RESEARCH_STARTUP_MAINTENANCE_TASK = asyncio.create_task(
            _run_research_startup_maintenance()
        )
    if _ASYNC_ISSUE_AUTO_RESUME_TASK is None or _ASYNC_ISSUE_AUTO_RESUME_TASK.done():
        _ASYNC_ISSUE_AUTO_RESUME_TASK = asyncio.create_task(_async_issue_auto_resume_loop())
    # Start async result watcher for long-running Symphony-routed MCP tasks
    from ..runtime.async_watcher import start_watcher as start_async_watcher

    _ASYNC_WATCHER_TASK = asyncio.create_task(
        start_async_watcher(
            workdir=ServerState.get_workdir(),
            agent_event_emitter=None,  # watcher injects directly into session messages
        )
    )
    workflow_api.recover_desktop_workflows_on_startup(ServerState.get_workdir())
    scheduled_service = get_scheduled_task_service()
    scheduled_service.set_executor(_execute_scheduled_agent_task)
    scheduled_service.set_completion_notifier(_notify_scheduled_task_completion)
    await scheduled_service.start()
    await goal_api.goal_service.start()
    from .feishu_runtime import start_feishu
    await start_feishu()
    await feishu_api.start_managed_feishu()


@app.on_event("shutdown")
async def shutdown_event():
    from .feishu_runtime import stop_feishu
    await stop_feishu()
    await feishu_api.stop_managed_feishu()
    await goal_api.goal_service.stop()
    from ..runtime.async_watcher import stop_watcher
    # Stop local observation/transfer, not the already accepted remote job.
    await stop_watcher()
    from ..plugins.manager import PluginManager
    for manager in list(PluginManager._instances.values()):
        try:
            await manager.trigger("system.shutdown", {"workdir": manager.workdir}, {})
        except Exception:
            pass

    global _ASYNC_ISSUE_AUTO_RESUME_TASK, _ASYNC_WATCHER_TASK, _RESEARCH_STARTUP_MAINTENANCE_TASK
    global _RESEARCH_ROUTING_RETRY_TASK
    if _RESEARCH_ROUTING_RETRY_TASK is not None:
        _RESEARCH_ROUTING_RETRY_TASK.cancel()
        await asyncio.gather(_RESEARCH_ROUTING_RETRY_TASK, return_exceptions=True)
        _RESEARCH_ROUTING_RETRY_TASK = None
    await guidance_runtime.close()
    await get_scheduled_task_service().stop()
    if _RESEARCH_STARTUP_MAINTENANCE_TASK is not None:
        if not _RESEARCH_STARTUP_MAINTENANCE_TASK.done():
            _RESEARCH_STARTUP_MAINTENANCE_TASK.cancel()
        try:
            await _RESEARCH_STARTUP_MAINTENANCE_TASK
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _RESEARCH_STARTUP_MAINTENANCE_TASK = None
    if _ASYNC_ISSUE_AUTO_RESUME_TASK is not None:
        _ASYNC_ISSUE_AUTO_RESUME_TASK.cancel()
        try:
            await _ASYNC_ISSUE_AUTO_RESUME_TASK
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _ASYNC_ISSUE_AUTO_RESUME_TASK = None
    if _ASYNC_WATCHER_TASK is not None:
        _ASYNC_WATCHER_TASK.cancel()
        try:
            await _ASYNC_WATCHER_TASK
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _ASYNC_WATCHER_TASK = None
    for sid in list(ServerState.agents.keys()):
        await ServerState.cleanup_session(sid)
    # Every workspace now owns its plugin and MCP runtime. Close all existing
    # scopes without initializing a new manager during shutdown.
    from ..runtime.workspace_registry import registry_workspace
    managers = list(PluginManager._instances.values())
    for manager in managers:
        for pending in (manager._load_task, manager._ensure_task):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        try:
            # Process exit releases local resources, not the user's persistent
            # approval or broker registration for already accepted work.
            await manager.unload_all(shutdown=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Failed to unload workspace plugins during shutdown")
    workspaces = set(MCPManager._workspace_states) | {manager.workdir for manager in managers}
    for workdir in workspaces:
        with registry_workspace(workdir):
            await MCPManager.close()
            SkillManager.clear()


def create_static_dir():
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    return static_dir


def run_server(host: str = "127.0.0.1", port: int = 8080, reload: bool = False):
    create_static_dir()
    uvicorn.run(
        "tcode_agent.server.app:app",
        host=host,
        port=port,
        reload=reload,
        log_config=uvicorn_log_config(),
    )


if __name__ == "__main__":
    run_server()
