"""Chat WebSocket transport and streaming turn orchestration.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..agent.agents import BuiltinAgents
from ..plugins.command_registry import PluginCommandRegistry
from ..presentation import normalize_content_blocks
from ..protocol import permission_request_event, tool_using_event, usage_status_event
from ..protocol.v2 import RuntimeEventEmitter
from ..protocol.v2.adapters import TCodeWebSocketAdapter
from ..research import (
    ResearchPackIndexScheduler,
    normalize_research_chat_context,
    selected_asset_ids,
)
from ..runtime.agent_backend import NormalizedMessage, NormalizedRunInput, RunContext
from ..runtime.capacity import CapacityRejected, ExecutionCapacityController
from ..runtime.computer import end_computer_task
from ..runtime.gateway import agent_backend_registry, run_coordinator, runtime_registry
from ..runtime.model_policy import ModelPolicyError, resolve_runtime_model
from ..runtime.run_coordinator import RunConflictError, RunUnavailableError
from ..runtime.telemetry import redact_telemetry_text
from ..runtime.structure_view import bridge as structure_bridge
from ..session.manager import SessionManager
from ..team.catalog import TeamCatalog
from ..types import AgentMode, Message
from ..workflow import guard_protein_design_brainstorming_route
from . import harness_api
from . import workdirs as workdir_utils
from ._shared import (
    _append_message_to_session,
    _attempt_server_side_workflow_auto_route,
    _bind_guidance_v2_to_agent,
    _bootstrap_fixed_team_workers,
    _build_websocket_question_handler,
    _chat_runtime_status_payload,
    _cleanup_disconnected_chat_session,
    _cleanup_fixed_team_state,
    _delegated_session_lock,
    _ensure_delegated_agent_session,
    _ensure_delegated_team_session,
    _ensure_session_guidance_drain,
    _ensure_session_guidance_drain_v2,
    _env_truthy,
    _frontend_outcome_validation_enabled,
    _inject_protein_design_resume_instruction,
    _inject_protein_direct_execution_instruction,
    _inject_workflow_declined_direct_instruction,
    _legacy_protein_auto_route_request_allows,
    _merge_resource_context,
    _message_input_images,
    _pick_manager_team_delegate_for_session,
    _protein_design_resume_execution_mode,
    _protein_route_should_switch_agent,
    _public_outcome_validation,
    _remember_last_delegated_team,
    _resolve_paper_workflow_agent,
    _run_subagent_turn_background,
    _runtime_team_snapshot,
)
from .workspace_scope import (
    _infer_workdir_from_resource_context,
    _resolve_requested_workdir_for_session,
    _resolve_session_workdir,
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _workdir_name_from_path,
    _workspace_context_from_websocket,
)
from .message_resources import (
    _build_message_resource_context,
)
from .session_presenter import (
    _public_frontend_message_metadata,
    serialize_runtime_messages,
)
from .issue_records import (
    _same_resolved_workdir,
)
from .agent_selection import normalize_primary_agent_name
from .auth import CurrentUserContext, auth_required, current_user_from_websocket
from .chat_event_presenter import (
    send_frontend_tool_events as _send_frontend_tool_events,
)
from .chat_setup import (
    _initialize_chat_turn_runtime,
    _persist_model_selection_for_workdir,
    _record_agent_mode_switch,
)
from .delegation_routing import (
    build_permission_delegation_message as _build_permission_delegation_message,
)
from .delegation_routing import (
    pick_permission_denied_delegate as _pick_permission_denied_delegate,
)
from .guidance_runtime import guidance_v2_enabled
from .input_limits import (
    ChatInputLimitError,
    validate_chat_frame,
    validate_chat_message,
)
from .native_execution import configure_native_agent
from .state import ServerState
from .chat_transport import ChatTransport
from .stream_activity import StreamActivityTracker, cancel_pending_stream_item
from .wiki_service import _normalize_paper_wiki_chat_context
from .workflow_dispatch import (
    discover_workflow,
    prepare_workflow,
    resume_workflow_route,
)

router = APIRouter()


def _websocket_is_disconnected(websocket: WebSocket) -> bool:
    return (
        websocket.client_state is WebSocketState.DISCONNECTED
        or websocket.application_state is WebSocketState.DISCONNECTED
    )


async def _finalize_computer_task(task_id: str, terminal_id: str) -> bool:
    """Run bounded cleanup without letting cleanup faults replace turn outcome."""
    cleanup = asyncio.create_task(end_computer_task(task_id, terminal_id))
    try:
        return await asyncio.wait_for(asyncio.shield(cleanup), timeout=6)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=6)
        except (asyncio.CancelledError, Exception):
            pass
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Computer terminal finalizer failed for %s: %s",
            task_id, type(exc).__name__,
        )
        return False


_TURN_TERMINAL_OUTCOMES = frozenset({
    "completed", "cancelled", "error", "maximum-duration", "stalled",
    "delegated-cancelled", "workflow-completed", "native-dispatch-failed",
})


class _ComputerTurnFinalizer:
    """Exactly-once task cleanup shared by every terminal turn exit."""

    def __init__(self, task_id: str, request_id: str) -> None:
        self.task_id = task_id
        self.request_id = request_id
        self.finished = False
        self.preserve_for_transport = False
        self.committed_outcome: str | None = None

    def commit(self, outcome: str) -> None:
        if outcome not in _TURN_TERMINAL_OUTCOMES:
            raise ValueError(f"unknown terminal outcome: {outcome}")
        if self.committed_outcome is not None and self.committed_outcome != outcome:
            raise RuntimeError("turn terminal outcome already committed")
        self.committed_outcome = outcome

    def commit_if_running(self, outcome: str) -> bool:
        """Commit an exception outcome only when no terminal fact exists yet."""
        if self.committed_outcome is not None:
            return False
        self.commit(outcome)
        return True

    def preserve_on_disconnect(self) -> None:
        if self.committed_outcome is None:
            self.preserve_for_transport = True

    async def finish(self) -> bool:
        if self.finished or self.preserve_for_transport or self.committed_outcome is None:
            return False
        self.finished = True
        return await _finalize_computer_task(
            self.task_id, f"{self.request_id}:{self.committed_outcome}"
        )


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """Serve one owner-scoped chat socket and supervise every agent turn.

    The endpoint authenticates and resolves the workspace, claims at most one
    foreground turn per session, streams model/tool events, persists the final
    session, and releases all handlers in ``finally`` blocks.  Streaming uses a
    shielded ``__anext__`` task so heartbeat timeouts do not cancel healthy
    tools; explicit session cancellation must therefore cancel and reap that
    child task before reporting the cancelled transport terminal.

    Args:
        websocket: Accepted FastAPI WebSocket carrying chat and control frames.

    Returns:
        ``None`` after the peer disconnects or the socket is closed.  Per-turn
        failures are serialized to the socket and do not escape the endpoint.
    """

    if auth_required():
        user = current_user_from_websocket(websocket)
        if user is None:
            await websocket.close(code=4401)
            return
        if user.tenant_status != "active":
            await websocket.close(code=4423)
            return
        websocket.state.current_user = user
    await websocket.accept()

    session_id = None
    transport = ChatTransport(websocket)
    claimed_turn_session_id: str | None = None
    claimed_turn_task: asyncio.Task | None = None
    claimed_turn_lease: Any = None
    active_computer_finalizer: _ComputerTurnFinalizer | None = None
    resume_scope = None
    resume_token = None
    resume_completed = False
    origin_token = None
    requested_protocol_version = str(
        websocket.query_params.get("protocol_version") or "1.0.0"
    ).strip()
    if requested_protocol_version not in {"1", "1.0", "1.0.0", "2", "2.0", "2.0.0"}:
        await websocket.close(code=4400, reason="unsupported protocol_version")
        return
    use_protocol_v2 = requested_protocol_version in {"2", "2.0", "2.0.0"}
    websocket.send_json = transport.send_json  # type: ignore[method-assign]

    def release_turn_claim() -> None:
        """Release the foreground turn and capacity lease on every exit path."""
        nonlocal claimed_turn_session_id, claimed_turn_task, claimed_turn_lease
        nonlocal resume_scope, resume_token, resume_completed
        nonlocal origin_token
        if origin_token is not None:
            from ..runtime.async_resume import active_origin
            active_origin.reset(origin_token)
            origin_token = None
        if resume_scope is not None:
            from ..runtime.async_resume import active_resume, finish_resume
            try:
                finish_resume(resume_scope, completed=resume_completed)
            except Exception:
                logging.getLogger(__name__).exception("async_resume_claim_settlement_failed session_id=%s", session_id)
            finally:
                if resume_token is not None:
                    active_resume.reset(resume_token)
                resume_scope, resume_token, resume_completed = None, None, False
        if claimed_turn_session_id:
            ServerState.finish_chat_turn(claimed_turn_session_id, claimed_turn_task)
            claimed_turn_session_id = None
            claimed_turn_task = None
        if claimed_turn_lease is not None:
            claimed_turn_lease.release()
            claimed_turn_lease = None

    try:
        while True:
            if resume_scope is not None or origin_token is not None:
                release_turn_claim()
            # Receive message
            try:
                data = await websocket.receive_text()
            except WebSocketDisconnect:
                transport.closed = True
                break
            except RuntimeError as exc:
                # Browser refresh / tab switch can close socket between turns.
                if "WebSocket is not connected" in str(exc):
                    transport.closed = True
                    break
                raise
            try:
                data = validate_chat_frame(data)
            except ChatInputLimitError as exc:
                await websocket.send_json(exc.as_websocket_event())
                continue
            request = json.loads(data)
            if not isinstance(request, dict):
                await websocket.send_json(
                    ChatInputLimitError(
                        code="invalid_chat_request",
                        actual=0,
                        limit=0,
                        message="聊天请求必须是 JSON 对象。",
                    ).as_websocket_event()
                )
                continue
            print(
                "[WS] Received frame: "
                f"action={str(request.get('action') or 'chat')[:40]} "
                f"chars={len(data)}"
            )
            client_request_id = str(request.get("request_id") or "").strip()[:160]
            if not client_request_id:
                client_request_id = f"run_{uuid.uuid4().hex}"
            if str(request.get("action") or "").strip().lower() == "replay":
                replay_session_id = str(request.get("session_id") or "").strip()
                if not use_protocol_v2 or not replay_session_id:
                    await transport.send_raw_json(
                        {
                            "type": "error",
                            "code": "invalid_replay_request",
                            "error": "v2 replay requires session_id and protocol_version=2.0.0",
                            "done": True,
                        }
                    )
                    await websocket.close(code=1000)
                    return
                replay_context = _workspace_context_from_websocket(
                    websocket,
                    tenant_id=str(request.get("tenant_id") or "").strip(),
                    user_id=str(request.get("user_id") or "").strip(),
                    workspace_id=str(request.get("workspace_id") or "").strip(),
                    workdir_name=str(request.get("workdir_name") or "").strip(),
                )
                replay_owner = _session_owner_kwargs(replay_context)
                replay_session = await SessionManager.get(
                    replay_session_id,
                    **replay_owner,
                )
                if replay_session is None:
                    await transport.send_raw_json(
                        {
                            "type": "error",
                            "code": "session_not_found",
                            "error": "Session not found or inaccessible",
                            "session_id": replay_session_id,
                            "done": True,
                        }
                    )
                    await websocket.close(code=1000)
                    return
                session_id = replay_session_id
                replay_emitter = RuntimeEventEmitter(
                    session_id=replay_session_id,
                    run_id=f"replay_{client_request_id}"[:200],
                    sink=transport.send_raw_json,
                    owner=replay_owner,
                    workdir=str(getattr(replay_session, "workdir", "") or ""),
                    persist=False,
                )
                transport.adapter = TCodeWebSocketAdapter(replay_emitter)
                try:
                    after_sequence = max(0, int(request.get("after_sequence", 0) or 0))
                except (TypeError, ValueError):
                    after_sequence = 0
                replay_window = await transport.adapter.replay_after(after_sequence)
                if replay_window.gap or not replay_window.events:
                    await replay_emitter.emit(
                        "messages.snapshot",
                        messages=serialize_runtime_messages(replay_session),
                        metadata={
                            "tcode": {
                                "replay": {
                                    "requested_after": after_sequence,
                                    "oldest_sequence": replay_window.oldest_sequence,
                                    "latest_sequence": replay_window.latest_sequence,
                                    "buffer_gap": replay_window.gap,
                                }
                            }
                        },
                    )
                await websocket.close(code=1000)
                return
            try:
                message = validate_chat_message(request.get("message", ""))
            except ChatInputLimitError as exc:
                await websocket.send_json(
                    exc.as_websocket_event(
                        session_id=str(request.get("session_id") or ""),
                        request_id=client_request_id,
                    )
                )
                continue
            stream = bool(request.get("stream", True))
            output_format = request.get("output_format")
            outcome_contract = request.get("outcome_contract")
            requested_research_context_present = "research_context" in request
            requested_research_context = request.get("research_context")
            requested_wiki_context_present = "wiki_context" in request
            requested_wiki_context = request.get("wiki_context")
            requested_file_refs = request.get("file_refs")
            selected_skills = request.get('selected_skills', [])
            if (not isinstance(selected_skills, list) or len(selected_skills) > 8
                    or any(not isinstance(name, str) or not name.strip() or len(name) > 200 for name in selected_skills)):
                raise RuntimeError('selected_skills must contain at most 8 valid skill names')
            selected_skills = list(dict.fromkeys(name.strip() for name in selected_skills))
            requested_agent_name = normalize_primary_agent_name(request.get("agent_name"))
            requested_tenant_id = str(request.get("tenant_id") or "").strip()
            requested_user_id = str(request.get("user_id") or "").strip()
            requested_workspace_id = str(request.get("workspace_id") or "").strip()
            requested_workdir_name = str(request.get("workdir_name") or "").strip()
            requested_workdir_path = str(request.get("workdir") or "").strip()
            requested_model_ref = str(
                request.get("model") or request.get("model_ref") or ""
            ).strip()
            requested_runtime_id = str(request.get("runtime_id") or "").strip()
            workspace_context = _workspace_context_from_websocket(
                websocket,
                tenant_id=requested_tenant_id,
                user_id=requested_user_id,
                workspace_id=requested_workspace_id,
                workdir_name=requested_workdir_name,
            )
            workdir_utils.assert_workspace_active(
                ServerState.get_workdir(),
                workspace_context,
            )
            session_owner = _session_owner_kwargs(workspace_context)
            session_workspace_owner = _session_owner_kwargs(
                workspace_context,
                include_workspace=True,
            )
            requested_execution_profile = (
                str(request.get("execution_profile") or "").strip().lower()
            )
            requested_scheduler_mode = (
                str(request.get("scheduler_mode") or "supervisor").strip().lower()
            )
            requested_reasoning_effort = (
                str(request.get("reasoning_effort") or "").strip().lower()
            )
            requested_use_swarms = request.get("use_swarms")
            requested_swarm_topology = (
                str(request.get("swarm_topology") or "concurrent").strip().lower()
            )
            requested_external_swarm_communication_mode = (
                str(request.get("external_swarm_communication_mode") or "")
                .strip()
                .lower()
            )
            if requested_external_swarm_communication_mode == "all":
                requested_external_swarm_communication_mode = "hybrid"
            requested_external_swarm_bus = (
                str(request.get("external_swarm_bus") or "").strip().lower()
            )
            requested_external_swarm_rounds = request.get("external_swarm_rounds")
            requested_external_swarm_agent_timeout_seconds = request.get(
                "external_swarm_agent_timeout_seconds"
            )
            requested_external_swarm_max_retries = request.get(
                "external_swarm_max_retries"
            )
            requested_external_swarm_fallback_on_error = request.get(
                "external_swarm_fallback_on_error"
            )
            requested_route_symphony_on_mcp_tool = request.get(
                "route_symphony_on_mcp_tool"
            )
            requested_desktop_permission_mode = "ask"
            if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
                from .desktop_config import desktop_permission_mode

                requested_desktop_permission_mode = desktop_permission_mode(
                    request.get("desktop_permission_mode")
                )
            if not isinstance(requested_use_swarms, bool):
                requested_use_swarms = None
            if requested_swarm_topology not in {
                "concurrent",
                "sequential",
                "voting",
                "rearrange",
            }:
                requested_swarm_topology = "concurrent"
            if requested_external_swarm_communication_mode not in {
                "",
                "hybrid",
                "p2p",
                "broadcast",
                "handoff",
            }:
                requested_external_swarm_communication_mode = ""
            if requested_external_swarm_bus not in {"", "memory", "redis", "kafka"}:
                requested_external_swarm_bus = ""
            if not isinstance(requested_external_swarm_fallback_on_error, bool):
                requested_external_swarm_fallback_on_error = None
            if not isinstance(requested_route_symphony_on_mcp_tool, bool):
                requested_route_symphony_on_mcp_tool = None

            if not isinstance(requested_external_swarm_rounds, int):
                requested_external_swarm_rounds = None
            else:
                requested_external_swarm_rounds = max(
                    1, min(8, requested_external_swarm_rounds)
                )

            if not isinstance(
                requested_external_swarm_agent_timeout_seconds, (int, float)
            ):
                requested_external_swarm_agent_timeout_seconds = None
            else:
                requested_external_swarm_agent_timeout_seconds = max(
                    5.0,
                    min(600.0, float(requested_external_swarm_agent_timeout_seconds)),
                )

            if not isinstance(requested_external_swarm_max_retries, int):
                requested_external_swarm_max_retries = None
            else:
                requested_external_swarm_max_retries = max(
                    0, min(5, requested_external_swarm_max_retries)
                )
            if requested_scheduler_mode not in {
                "supervisor",
                "chain",
                "swarm",
                "auto_swarm_builder",
            }:
                requested_scheduler_mode = "supervisor"
            from ..runtime.execution_profile import normalize_execution_profile

            requested_execution_profile = normalize_execution_profile(
                requested_execution_profile
            )
            if requested_reasoning_effort not in {"", "minimal", "low", "medium", "high"}:
                requested_reasoning_effort = ""

            requested_agent_name, paper_auto_resolution = _resolve_paper_workflow_agent(
                requested_agent_name=requested_agent_name,
                requested_execution_profile=requested_execution_profile,
                message=message,
            )
            resource_session_id = str(request.get("session_id") or "").strip() or str(uuid.uuid4())
            request["session_id"] = resource_session_id
            session_id = resource_session_id
            resource_workdir = ""
            resource_persisted = None
            resource_runtime = None
            try:
                resource_persisted = await SessionManager.get(
                    resource_session_id,
                    **session_owner,
                )
                resource_runtime = ServerState.sessions.get(resource_session_id)
                if not _runtime_session_matches_context(
                    resource_runtime,
                    workspace_context,
                ):
                    resource_runtime = None
                resource_persisted_workdir = str(
                    getattr(resource_persisted, "workdir", "") or ""
                ).strip()
                resource_runtime_workdir = str(
                    (
                        resource_runtime.get("workdir")
                        if isinstance(resource_runtime, dict)
                        else ""
                    )
                    or ""
                ).strip()
                resource_requested_workdir = ""
                if requested_workdir_name or requested_workdir_path:
                    resource_requested_workdir = _resolve_requested_workdir_for_session(
                        workdir_name=requested_workdir_name,
                        workdir_path=requested_workdir_path,
                        existing_workdir=resource_persisted_workdir or resource_runtime_workdir,
                        context=workspace_context,
                    )
                resource_workdir = (
                    resource_persisted_workdir
                    or resource_runtime_workdir
                    or resource_requested_workdir
                    or await _resolve_session_workdir(
                        resource_session_id,
                        workspace_context,
                    )
                )
            except Exception:
                resource_workdir = ServerState.get_workdir()

            if use_protocol_v2:
                emitter = RuntimeEventEmitter(
                    session_id=session_id,
                    run_id=client_request_id,
                    sink=transport.send_raw_json,
                    owner=session_workspace_owner,
                    workdir=resource_workdir,
                )
                transport.adapter = TCodeWebSocketAdapter(emitter)
                if (
                    resource_persisted is not None
                    and emitter.replay.after(session_id, 0).latest_sequence == 0
                ):
                    await emitter.emit(
                        "messages.snapshot",
                        messages=serialize_runtime_messages(resource_persisted),
                        metadata={"tcode": {"bootstrap": True}},
                    )

            # Cancellation is a control message, not a new turn, so it must not
            # compete for the foreground turn claim. The owner-scoped lookup
            # above also prevents a socket from cancelling another tenant's run.
            if request.get("action") == "cancel":
                ServerState.mark_session_cancelled(session_id)
                await run_coordinator.cancel_active(
                    owner_key=f"{workspace_context.tenant_id}:{workspace_context.user_id}",
                    session_id=session_id,
                )
                if (
                    resource_persisted is not None or resource_runtime is not None
                ) and ServerState.is_running(session_id):
                    await ServerState.cancel_session(session_id)
                    await websocket.send_json(
                        {
                            "type": "status",
                            "status": "cancelled",
                            "session_id": session_id,
                            "content": "Cancelled",
                        }
                    )
                continue

            from .goal_mode import public_goal, ACTIVE as GOAL_ACTIVE
            current_goal = public_goal(await SessionManager.get(session_id, **session_workspace_owner))
            if current_goal and current_goal['status'] in GOAL_ACTIVE:
                await websocket.send_json({'type': 'error', 'error': '目标模式正在推进，请先暂停目标，再发送普通消息。', 'done': True})
                continue

            # An explicit new user request is the only event that reopens a
            # session after Stop. Background guidance must not do this itself.
            ServerState.clear_session_cancelled(session_id)

            try:
                if not requested_runtime_id:
                    resolved_harness_state = (
                        harness_api.harness_handoff_store.get_state(
                            owner_key=(
                                f"{workspace_context.tenant_id}:"
                                f"{workspace_context.user_id}"
                            ),
                            workspace_id=workspace_context.workspace_id,
                            session_id=session_id,
                            session_metadata=(
                                dict(getattr(resource_persisted, "metadata", {}) or {})
                                if resource_persisted is not None
                                else None
                            ),
                        )
                    )
                    requested_runtime_id = str(
                        resolved_harness_state.get("current_runtime_id")
                        or "tcode-native"
                    )
                harness_state = harness_api.harness_handoff_store.assert_run_allowed(
                    owner_key=(
                        f"{workspace_context.tenant_id}:{workspace_context.user_id}"
                    ),
                    workspace_id=workspace_context.workspace_id,
                    session_id=session_id,
                    runtime_id=requested_runtime_id,
                    session_metadata=(
                        dict(getattr(resource_persisted, "metadata", {}) or {})
                        if resource_persisted is not None
                        else None
                    ),
                )
            except Exception as exc:
                code = str(getattr(exc, "code", "invalid_runtime_transition"))
                raise RuntimeError(f"{code}: {exc}") from exc

            from ..runtime.async_completion import async_continuation_identifier
            resume_identifier = str(request.get("async_continuation_id") or async_continuation_identifier(message)).strip()
            if resume_identifier:
                from ..runtime.async_resume import active_resume, claim_resume
                try:
                    if async_continuation_identifier(message) not in {"", resume_identifier}:
                        raise ValueError("async_resume_identifier_mismatch")
                    if _chat_runtime_status_payload(session_id).get("chat_running"):
                        raise ValueError("当前问题仍在回答，请等待本轮结束后再继续异步任务。")
                    # Only owner-resolved server records may provide restoration
                    # context. Client summaries/tokens are not trusted evidence.
                    resume_scope = claim_resume(resource_persisted, resume_identifier, client_request_id)
                    resume_token = active_resume.set(resume_scope)
                except ValueError as exc:
                    await websocket.send_json({"type": "async_resume_rejected", "session_id": session_id,
                                               "error": str(exc), "done": True})
                    await websocket.close(code=1000)
                    return

            from ..runtime.async_resume import active_origin
            origin_token = active_origin.set({"session_id": session_id, "request_id": client_request_id,
                "request": message, "message_id": str(uuid.uuid4())})

            if requested_runtime_id != "tcode-native":
                if selected_skills:
                    raise RuntimeError('当前 Harness 不支持 TCode Skill 选择')
                if not use_protocol_v2 or transport.adapter is None:
                    raise RuntimeError("external harnesses require protocol_version=2.0.0")
                definition = runtime_registry.get(requested_runtime_id)
                backend = agent_backend_registry.get(requested_runtime_id)
                if definition is None and backend is None:
                    raise RuntimeError(f"Unknown harness runtime: {requested_runtime_id}")
                if definition is not None:
                    allowed, reason = runtime_registry.allowed_for_request(
                        requested_runtime_id,
                        public_server=auth_required(),
                    )
                    if not allowed:
                        raise RuntimeError(reason)
                try:
                    runtime_model = await resolve_runtime_model(
                        requested_runtime_id,
                        requested_model_ref,
                        workdir=resource_workdir,
                        runtime_registry=runtime_registry,
                        backend_registry=agent_backend_registry,
                    )
                except ModelPolicyError as exc:
                    raise RuntimeError(str(exc)) from exc

                persisted_messages: list[NormalizedMessage] = []
                for item in list(getattr(resource_persisted, "messages", []) or []):
                    role_value = getattr(getattr(item, "role", ""), "value", getattr(item, "role", ""))
                    role = str(role_value or "").strip()
                    message_id = str(getattr(item, "id", "") or "").strip()
                    if role not in {"user", "assistant", "system", "tool"} or not message_id:
                        continue
                    persisted_messages.append(
                        NormalizedMessage(
                            id=message_id,
                            role=role,
                            content=getattr(item, "content", ""),
                        )
                    )
                persisted_messages.append(
                    NormalizedMessage(
                        id=f"{client_request_id}:user"[:200],
                        role="user",
                        content=message,
                    )
                )
                pending_handoff = harness_api.harness_handoff_store.pending_for_target(
                    owner_key=(
                        f"{workspace_context.tenant_id}:{workspace_context.user_id}"
                    ),
                    workspace_id=workspace_context.workspace_id,
                    session_id=session_id,
                    target_runtime_id=requested_runtime_id,
                )
                normalized_run = NormalizedRunInput(
                    thread_id=session_id,
                    run_id=client_request_id,
                    agent_id=str(requested_agent_name or "default"),
                    runtime_id=requested_runtime_id,
                    model_id=runtime_model.model_id,
                    messages=persisted_messages,
                    context=[pending_handoff] if pending_handoff else [],
                    forwarded_props={
                        "tcode": {
                            "workspaceId": workspace_context.workspace_id,
                            "runtimeId": requested_runtime_id,
                            "modelId": runtime_model.model_id,
                            "publicServer": auth_required(),
                        }
                    },
                )
                run_context = RunContext(
                    tenant_id=workspace_context.tenant_id,
                    user_id=workspace_context.user_id,
                    workspace_id=workspace_context.workspace_id,
                    workdir=resource_workdir,
                    session_id=session_id,
                    run_id=client_request_id,
                    agent_id=normalized_run.agent_id,
                    runtime_id=requested_runtime_id,
                    request_id=client_request_id,
                    async_resume_scope=resume_scope,
                    async_origin=active_origin.get(),
                    handoff_id=str(
                        (pending_handoff or {}).get("handoff_id") or ""
                    ),
                )
                try:
                    prepared = await run_coordinator.prepare(
                        normalized_run,
                        run_context,
                        client_request_id=client_request_id,
                    )
                except RunConflictError as exc:
                    await websocket.send_json({"type": "run_conflict", "session_id": session_id,
                        "reason": exc.code, "running": exc.code == "session_busy", "done": True})
                    await websocket.close(code=1000)
                    return
                except RunUnavailableError as exc:
                    raise RuntimeError(str(exc)) from exc

                if resource_persisted is not None:
                    def mark_running(session):
                        session.metadata = {**session.metadata,
                            "harness": harness_api.harness_handoff_store.projection(harness_state)}
                        session.model = runtime_model.model_id
                        session.status = "running"
                    try:
                        await SessionManager.mutate(session_id, mark_running, **session_workspace_owner)
                    except BaseException:
                        # Preparation owns a queued reservation already. A failed
                        # session write must not strand it until stale recovery.
                        run_coordinator.store.update(f"{workspace_context.tenant_id}:{workspace_context.user_id}",
                            session_id, client_request_id, status="failed", last_sequence=0,
                            terminal={"type": "run.error", "code": "run_commit_failed", "message": "Unable to persist run startup"})
                        raise

                envelope_fields = {
                    "protocol",
                    "protocol_version",
                    "type",
                    "event_id",
                    "session_id",
                    "run_id",
                    "sequence",
                    "timestamp",
                    "causation_id",
                    "metadata",
                }
                from contextlib import aclosing
                async with aclosing(run_coordinator.stream(prepared)) as runtime_events:
                    async for runtime_event in runtime_events:
                        event_payload = runtime_event.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                        for field_name in envelope_fields:
                            event_payload.pop(field_name, None)
                        await transport.adapter.emitter.emit(
                            runtime_event.type,
                            causation_id=runtime_event.causation_id,
                            metadata=runtime_event.metadata,
                            **event_payload,
                        )
                # The transport consumes the one-shot handoff after its prompt
                # reaches the target Harness. Mirror the cleared projection so
                # Session metadata also survives clients that do not refetch.
                try:
                    latest_session = await SessionManager.get(
                        session_id,
                        **session_workspace_owner,
                    )
                    if latest_session is not None:
                        refreshed_harness_state = (
                            harness_api.harness_handoff_store.get_state(
                                owner_key=(
                                    f"{workspace_context.tenant_id}:"
                                    f"{workspace_context.user_id}"
                                ),
                                workspace_id=workspace_context.workspace_id,
                                session_id=session_id,
                            )
                        )
                        def refresh_harness(session):
                            session.metadata = {**session.metadata,
                                "harness": harness_api.harness_handoff_store.projection(refreshed_harness_state)}
                        await SessionManager.mutate(
                            session_id,
                            refresh_harness,
                            **session_workspace_owner,
                        )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "harness_delivery_projection_failed session_id=%s",
                        session_id,
                    )
                try:
                    await websocket.close(code=1000)
                except Exception:
                    pass
                return

            claimed_turn_task = asyncio.current_task()
            from .goal_mode import claim_ordinary_turn
            claim_status = await claim_ordinary_turn(
                session_id,
                session_workspace_owner,
                client_request_id,
                claimed_turn_task,
            )
            if claim_status == 'goal_mode_active':
                release_turn_claim()
                await websocket.send_json({'type': 'error', 'error': '目标模式正在推进，请先暂停目标，再发送普通消息。', 'done': True})
                await websocket.close(code=1000)
                return
            if claim_status != "accepted":
                release_turn_claim()
                await websocket.send_json(
                    {
                        "type": "run_conflict",
                        "reason": claim_status,
                        "session_id": session_id,
                        "request_id": client_request_id,
                        "running": claim_status != "duplicate_completed",
                        "done": True,
                    }
                )
                try:
                    await websocket.close(code=1000)
                except Exception:
                    pass
                return
            claimed_turn_session_id = session_id
            try:
                claimed_turn_lease = await ExecutionCapacityController.current().acquire(
                    kind="chat",
                    tenant_id=workspace_context.tenant_id,
                    user_id=workspace_context.user_id,
                    session_id=session_id,
                    request_id=client_request_id,
                )
            except CapacityRejected as exc:
                ServerState.finish_chat_turn(
                    claimed_turn_session_id,
                    claimed_turn_task,
                )
                claimed_turn_session_id = None
                claimed_turn_task = None
                await websocket.send_json(
                    {
                        "type": "capacity_rejected",
                        "code": exc.code,
                        "content": exc.detail,
                        "retry_after_seconds": exc.retry_after_seconds,
                        "session_id": session_id,
                        "request_id": client_request_id,
                        "done": True,
                    }
                )
                continue
            active_computer_finalizer = _ComputerTurnFinalizer(
                session_id, client_request_id
            )
            native_handoff = harness_api.harness_handoff_store.pending_for_target(
                owner_key=(
                    f"{workspace_context.tenant_id}:{workspace_context.user_id}"
                ),
                workspace_id=workspace_context.workspace_id,
                session_id=session_id,
                target_runtime_id="tcode-native",
            )
            if native_handoff:
                harness_api.harness_handoff_store.mark_delivered(
                    str(native_handoff.get("handoff_id") or ""),
                    run_id=client_request_id,
                )
                if resource_persisted is not None:
                    refreshed_harness_state = (
                        harness_api.harness_handoff_store.get_state(
                            owner_key=(
                                f"{workspace_context.tenant_id}:"
                                f"{workspace_context.user_id}"
                            ),
                            workspace_id=workspace_context.workspace_id,
                            session_id=session_id,
                        )
                    )
                    metadata = dict(
                        getattr(resource_persisted, "metadata", {}) or {}
                    )
                    metadata["harness"] = (
                        harness_api.harness_handoff_store.projection(
                            refreshed_harness_state
                        )
                    )
                    await SessionManager.update(
                        session_id,
                        **session_workspace_owner,
                        metadata=metadata,
                    )
            incoming_resource_context = _build_message_resource_context(
                message,
                session_id=resource_session_id,
                workdir=resource_workdir,
                file_refs=requested_file_refs,
            )
            current_input_images = _message_input_images(
                incoming_resource_context,
                workdir=resource_workdir,
            )
            workflow_route_decision = await discover_workflow(
                message=message,
                session_id=str(request.get("session_id") or uuid.uuid4()),
                resource_context=incoming_resource_context,
                requested_agent_name=str(requested_agent_name or ""),
                workdir=resource_workdir,
            )
            if (
                _protein_route_should_switch_agent(
                    requested_agent_name=str(requested_agent_name or ""),
                    requested_execution_profile=requested_execution_profile,
                    decision=workflow_route_decision,
                )
                and _legacy_protein_auto_route_request_allows(str(requested_agent_name or ""))
            ):
                requested_agent_name = workflow_route_decision.recommended_agent
                requested_scheduler_mode = workflow_route_decision.recommended_scheduler_mode

            # Get or create session
            session_id = request.get("session_id")
            if not session_id:
                session_id = str(uuid.uuid4())
            print(f"[WS] Session: {session_id}, Message: {message[:50]}...")
            # Resolve agent name aliases (e.g., paper_auto -> paper_auto_coordinator)
            # First, check for hardcoded paper team aliases
            _paper_team_aliases = {
                "paper_auto": "paper_auto_coordinator",
                "paper_team": "paper_auto_coordinator",
                "paper_workflow": "paper_auto_coordinator",
            }
            if requested_agent_name in _paper_team_aliases:
                requested_agent_name = _paper_team_aliases[requested_agent_name]
            _protein_orchestrator_aliases = {
                "protein-design": "protein_orchestrator",
            }
            if requested_agent_name in _protein_orchestrator_aliases:
                requested_agent_name = _protein_orchestrator_aliases[requested_agent_name]
            
            # Only resolve via TeamCatalog if not already a valid builtin agent
            if requested_agent_name:
                if not BuiltinAgents.get(requested_agent_name):
                    resolved_agent_name = TeamCatalog.get_by_agent_name(requested_agent_name, available_only=False)
                    if resolved_agent_name:
                        requested_agent_name = resolved_agent_name.runtime_agent

            if requested_agent_name and not BuiltinAgents.get(requested_agent_name):
                raise RuntimeError(f"Unknown agent: {requested_agent_name}")
            if requested_agent_name:
                requested_agent = BuiltinAgents.get(requested_agent_name)
                if requested_agent and requested_agent.mode not in {AgentMode.PRIMARY, AgentMode.MANAGER}:
                    raise RuntimeError(
                        f"Agent '{requested_agent_name}' is not a primary agent"
                    )

            persisted_existing = await SessionManager.get(
                session_id,
                **session_workspace_owner,
            )
            runtime_existing = ServerState.sessions.get(session_id)
            if not _runtime_session_matches_context(
                runtime_existing,
                workspace_context,
                include_workspace=True,
            ):
                runtime_existing = None
            persisted_existing_workdir = str(
                getattr(persisted_existing, "workdir", "") or ""
            ).strip()
            runtime_existing_workdir = str(
                (runtime_existing.get("workdir") if isinstance(runtime_existing, dict) else "")
                or ""
            ).strip()
            requested_workdir = ""
            if requested_workdir_name or requested_workdir_path:
                requested_workdir = _resolve_requested_workdir_for_session(
                    workdir_name=requested_workdir_name,
                    workdir_path=requested_workdir_path,
                    existing_workdir=persisted_existing_workdir or runtime_existing_workdir,
                    context=workspace_context,
                )
            inferred_resource_workdir = _infer_workdir_from_resource_context(
                incoming_resource_context
            )
            existing_session_workdir = (
                persisted_existing_workdir or runtime_existing_workdir
            )
            if (
                existing_session_workdir
                and _same_resolved_workdir(
                    existing_session_workdir,
                    ServerState.get_workdir(),
                )
                and (requested_workdir or inferred_resource_workdir)
            ):
                existing_session_workdir = ""
            session_workdir = (
                existing_session_workdir
                or requested_workdir
                or inferred_resource_workdir
                or await _resolve_session_workdir(session_id, workspace_context)
            )
            if requested_workdir and session_workdir and requested_workdir != session_workdir:
                raise RuntimeError("session workdir is immutable")
            if requested_model_ref:
                requested_model_ref = _persist_model_selection_for_workdir(
                    requested_model_ref,
                    workdir=session_workdir,
                    prefer_local=True,
                )
            existing_agent_name = str(
                (
                    runtime_existing.get("agent_name")
                    if isinstance(runtime_existing, dict)
                    else ""
                )
                or getattr(persisted_existing, "agent_name", "")
                or ""
            ).strip()
            existing_team_entry = TeamCatalog.get_by_agent_name(
                existing_agent_name, available_only=False
            )
            original_route_decision = workflow_route_decision
            workflow_route_decision = resume_workflow_route(
                decision=workflow_route_decision, session_id=session_id,
                workdir=session_workdir, message=message,
                requested_agent_name=str(requested_agent_name or ""),
            )
            if workflow_route_decision is not original_route_decision:
                requested_agent_name = workflow_route_decision.recommended_agent
                requested_scheduler_mode = workflow_route_decision.recommended_scheduler_mode
            workflow_route_decision = guard_protein_design_brainstorming_route(
                workflow_route_decision,
                str(requested_agent_name or ""),
                workdir=session_workdir,
                message=message,
            )
            normalized_requested_agent = str(requested_agent_name or "").strip()
            mode_switch_record = None
            if (
                session_id
                and TeamCatalog.is_team_mode_agent(existing_agent_name)
                and existing_agent_name != normalized_requested_agent
            ):
                cleanup_summary = await _cleanup_fixed_team_state(session_id)
                try:
                    print(
                        f"[WS] Fixed-team mode switch for {session_id}: "
                        f"{existing_agent_name} -> {normalized_requested_agent} "
                        f"(deleted_children={len(cleanup_summary['deleted_child_sessions'])}, "
                        f"removed_workers={len(cleanup_summary['removed_workers'])})"
                    )
                except Exception:
                    pass
            if session_id and existing_agent_name != normalized_requested_agent and normalized_requested_agent:
                mode_switch_record = await _record_agent_mode_switch(
                    session_id,
                    from_agent=existing_agent_name,
                    to_agent=normalized_requested_agent,
                    owner_kwargs=session_workspace_owner,
                )
            elif session_id and normalized_requested_agent:
                existing_metadata = dict(getattr(persisted_existing, "metadata", {}) or {})
                last_switch = existing_metadata.get("last_mode_switch")
                last_switch_to = (
                    str((last_switch or {}).get("to") or "").strip()
                    if isinstance(last_switch, dict)
                    else ""
                )
                if (
                    last_switch_to != normalized_requested_agent
                    and TeamCatalog.get_by_agent_name(normalized_requested_agent, available_only=False)
                ):
                    mode_switch_record = await _record_agent_mode_switch(
                        session_id,
                        from_agent=existing_agent_name or "previous-context",
                        to_agent=normalized_requested_agent,
                        force=True,
                        owner_kwargs=session_workspace_owner,
                    )

            session = ServerState.get_or_create_session(
                session_id,
                requested_agent_name,
                requested_scheduler_mode,
                requested_execution_profile,
                workdir=session_workdir,
                model=requested_model_ref or None,
            )
            ServerState.set_session_event_sender(session_id, websocket.send_json)
            session["workdir"] = session_workdir
            if requested_model_ref:
                session["model"] = requested_model_ref
            session["tenant_id"] = workspace_context.tenant_id
            session["user_id"] = workspace_context.user_id
            session["workspace_id"] = workspace_context.workspace_id
            resource_context = incoming_resource_context
            if resource_context:
                session["resource_context"] = _merge_resource_context(
                    session.get("resource_context"),
                    resource_context,
                )
            if paper_auto_resolution.get("pdf_count"):
                session["paper_input_summary"] = paper_auto_resolution
            existing_snapshot_metadata = dict(
                getattr(persisted_existing, "metadata", {}) or {}
            )
            persisted_workspace = dict(
                existing_snapshot_metadata.get("agent_workspace") or {}
            )
            persisted_execution_workdir = str(
                persisted_workspace.get("execution_workdir") or ""
            ).strip()
            if persisted_execution_workdir and Path(
                persisted_execution_workdir
            ).is_dir():
                session["control_workdir"] = session_workdir
                session["agent_workspace_id"] = str(
                    persisted_workspace.get("workspace_id") or ""
                )
                session["agent_execution_workdir"] = persisted_execution_workdir
            if (
                not requested_model_ref
                and str(existing_snapshot_metadata.get("config_snapshot_id") or "").strip()
            ):
                session["config_snapshot_id"] = str(
                    existing_snapshot_metadata.get("config_snapshot_id")
                )
            agent = ServerState.get_or_create_agent(session_id)
            if persisted_existing is not None:
                # A freshly forked/reopened chat must have its inherited history
                # before the first new turn starts; background hydration can race
                # the provider call.
                agent.messages = list(
                    getattr(persisted_existing, "messages", []) or []
                )
                session["messages"] = list(agent.messages)
                _bind_guidance_v2_to_agent(agent, persisted_existing)
            agent._active_client_request_id = client_request_id
            agent._next_selected_skills = selected_skills
            effective_execution_profile = configure_native_agent(
                agent, session, execution_profile=requested_execution_profile,
                reasoning_effort=requested_reasoning_effort,
            )
            agent.set_use_swarms(requested_use_swarms)
            agent.set_swarm_topology(requested_swarm_topology)
            agent.set_external_swarm_communication_mode(
                requested_external_swarm_communication_mode
            )
            agent.set_external_swarm_bus(requested_external_swarm_bus)
            agent.set_external_swarm_rounds(requested_external_swarm_rounds)
            agent.set_external_swarm_agent_timeout_seconds(
                requested_external_swarm_agent_timeout_seconds
            )
            agent.set_external_swarm_max_retries(requested_external_swarm_max_retries)
            agent.set_external_swarm_fallback_on_error(
                requested_external_swarm_fallback_on_error
            )
            agent.set_route_symphony_on_mcp_tool(requested_route_symphony_on_mcp_tool)
            if mode_switch_record is not None:
                persisted_after_switch = await SessionManager.get(
                    session_id,
                    **session_workspace_owner,
                )
                if persisted_after_switch is not None:
                    agent.messages = list(getattr(persisted_after_switch, "messages", []) or [])
                    session["messages"] = list(agent.messages)

            # Ensure root session is persisted for refresh/reload history recovery.
            persisted_root = await SessionManager.get(
                session_id,
                **session_workspace_owner,
            )
            if not persisted_root:
                persisted_root = await SessionManager.create(
                    session_id=session_id,
                    parent_id=None,
                    title=(message or "New Chat")[:120],
                    agent_name=session.get("agent_name", "build"),
                    workdir=session_workdir,
                    tenant_id=workspace_context.tenant_id,
                    user_id=workspace_context.user_id,
                    workspace_id=workspace_context.workspace_id,
                    metadata={
                        "execution_profile": session.get("execution_profile", "standard"),
                        "scheduler_mode": session.get("scheduler_mode", "supervisor"),
                        "reasoning_effort": session.get("reasoning_effort"),
                        "model": session.get("model"),
                        "resource_context": session.get("resource_context", {}),
                        "config_snapshot_id": session.get("config_snapshot_id", ""),
                        "config_snapshot_workdir": session.get(
                            "config_snapshot_workdir", session_workdir
                        ),
                        "config_fingerprint": session.get("config_fingerprint", ""),
                        "config_policy_fingerprint": session.get(
                            "config_policy_fingerprint", ""
                        ),
                        "config_summary": session.get("config_summary", {}),
                    },
                )
            else:
                if not getattr(persisted_root, "title", "") and message:
                    await SessionManager.update(
                        session_id,
                        **session_workspace_owner,
                        title=message[:120],
                    )
                metadata = dict(getattr(persisted_root, "metadata", {}) or {})
                metadata["execution_profile"] = session.get("execution_profile", "standard")
                metadata["scheduler_mode"] = session.get("scheduler_mode", "supervisor")
                metadata["reasoning_effort"] = session.get("reasoning_effort")
                metadata["model"] = session.get("model")
                metadata["resource_context"] = session.get("resource_context", {})
                metadata["config_snapshot_id"] = session.get(
                    "config_snapshot_id", metadata.get("config_snapshot_id", "")
                )
                metadata["config_snapshot_workdir"] = session.get(
                    "config_snapshot_workdir",
                    metadata.get("config_snapshot_workdir", session_workdir),
                )
                metadata["config_fingerprint"] = session.get(
                    "config_fingerprint", metadata.get("config_fingerprint", "")
                )
                metadata["config_policy_fingerprint"] = session.get(
                    "config_policy_fingerprint",
                    metadata.get("config_policy_fingerprint", ""),
                )
                metadata["config_summary"] = session.get(
                    "config_summary", metadata.get("config_summary", {})
                )
                await SessionManager.update(
                    session_id,
                    **session_workspace_owner,
                    metadata=metadata,
                )
                persisted_root.metadata = metadata
                if (
                    not persisted_existing_workdir
                    or (
                        _same_resolved_workdir(
                            persisted_existing_workdir,
                            ServerState.get_workdir(),
                        )
                        and not _same_resolved_workdir(
                            persisted_existing_workdir,
                            session_workdir,
                        )
                    )
                ):
                    await SessionManager.update(
                        session_id,
                        **session_workspace_owner,
                        workdir=session_workdir,
                    )
                    persisted_root.workdir = session_workdir
                persisted_root.tenant_id = workspace_context.tenant_id
                persisted_root.user_id = workspace_context.user_id
                persisted_root.workspace_id = workspace_context.workspace_id
            if persisted_root is not None:
                root_metadata = dict(getattr(persisted_root, "metadata", {}) or {})
                if requested_research_context_present:
                    research_context = normalize_research_chat_context(
                        requested_research_context,
                        tenant_id=workspace_context.tenant_id,
                        user_id=workspace_context.user_id,
                        workspace_id=workspace_context.workspace_id,
                        workdir=session_workdir,
                    )
                    root_metadata["research_context"] = research_context
                    await SessionManager.update(
                        session_id,
                        **session_workspace_owner,
                        metadata=root_metadata,
                    )
                    persisted_root.metadata = root_metadata
                else:
                    research_context = (
                        dict(root_metadata.get("research_context") or {})
                        if isinstance(root_metadata.get("research_context"), dict)
                        else {}
                    )
                session["research_context"] = research_context
                if requested_wiki_context_present:
                    websocket_actor = getattr(websocket.state, "current_user", None)
                    if not isinstance(websocket_actor, CurrentUserContext):
                        websocket_actor = CurrentUserContext(
                            user_id=workspace_context.user_id,
                            tenant_id=workspace_context.tenant_id,
                            email="",
                        )
                    wiki_context = _normalize_paper_wiki_chat_context(
                        requested_wiki_context,
                        actor=websocket_actor,
                        workspace_id=workspace_context.workspace_id,
                    )
                    root_metadata["wiki_context"] = wiki_context
                    await SessionManager.update(
                        session_id,
                        **session_workspace_owner,
                        metadata=root_metadata,
                    )
                    persisted_root.metadata = root_metadata
                else:
                    wiki_context = (
                        dict(root_metadata.get("wiki_context") or {})
                        if isinstance(root_metadata.get("wiki_context"), dict)
                        else {}
                    )
                session["wiki_context"] = wiki_context
                for selected_asset_id in selected_asset_ids(research_context):
                    ResearchPackIndexScheduler.schedule(
                        asset_id=selected_asset_id,
                        tenant_id=workspace_context.tenant_id,
                        user_id=workspace_context.user_id,
                        workspace_id=workspace_context.workspace_id,
                        workdir=session_workdir,
                    )
                agent._existing_session = persisted_root

            auto_workflow_run = None

            # 团队入口刷新后，继续补齐固定成员，避免右侧面板丢人。
            if (
                auto_workflow_run is None
                and TeamCatalog.is_fixed_team_bootstrap_agent(session.get("agent_name"))
            ):
                asyncio.create_task(
                    _bootstrap_fixed_team_workers(
                        session_id,
                        str(session.get("agent_name", "") or ""),
                    )
                )


            if (
                (not agent.messages)
                and persisted_root
                and getattr(persisted_root, "messages", None)
            ):
                agent.messages = list(persisted_root.messages)

            # Expand file paths in the message to content when appropriate.
            def _is_text_file(full_path: str) -> bool:
                try:
                    if not os.path.isfile(full_path):
                        return False
                    size = os.path.getsize(full_path)
                    # if too large, prefer path
                    if size > 64 * 1024:
                        return False
                    with open(full_path, "rb") as f:
                        head = f.read(8192)
                        if b"\x00" in head:
                            return False
                        try:
                            head.decode("utf-8")
                            return True
                        except Exception:
                            return False
                except Exception:
                    return False

            def _safe_read_text(full_path: str) -> str:
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return ""

            import re

            def _expand_paths_in_message(msg: str) -> str:
                # Matches blocks like:
                # ### File: <name>\nPath: <path>\n
                # File: <name>\nPath: <path>\n
                pattern = re.compile(
                    r"^\s*(?:#{1,6}\s*)?File:\s*(?P<name>.+?)\s*\n\s*Path:\s*(?P<path>[^\n]+)(?:\n|$)",
                    re.M,
                )

                def _repl(m: re.Match) -> str:
                    name = m.group("name").strip()
                    path = m.group("path").strip()
                    # Only allow paths inside workdir for security
                    base_dir = Path(ServerState.get_workdir()).expanduser().resolve(strict=False)
                    try:
                        candidate = Path(path).expanduser()
                        if not candidate.is_absolute():
                            candidate = base_dir / candidate
                        full_path_obj = candidate.resolve(strict=False)
                        full_path_obj.relative_to(base_dir)
                    except Exception:
                        return m.group(0)

                    if _is_text_file(full_path_obj.as_posix()):
                        content = _safe_read_text(full_path_obj.as_posix())
                        if content:
                            return f"### File: {name}\n\n{content}"
                    # fallback keep path unchanged
                    return m.group(0)

                return pattern.sub(lambda mm: _repl(mm), msg)

            try:
                message = _expand_paths_in_message(message)
            except Exception as e:
                print(f"[WS] Failed expanding paths: {e}")

            try:
                command_result = PluginCommandRegistry.resolve_message(message)
                if command_result:
                    message = command_result["rendered_message"]
                    command = command_result["command"]
                    if command.agent and not requested_agent_name:
                        requested_agent_name = command.agent
                    await websocket.send_json(
                        {
                            "type": "plugin_command",
                            "name": command.name,
                            "arguments": command_result["arguments"],
                        }
                    )
            except Exception as e:
                print(f"[WS] Failed resolving plugin command: {e}")

            # Send session ID
            print(f"[WS] Sending session: {session_id}")
            await websocket.send_json(
                {
                    "type": "session",
                    "session_id": session_id,
                    "agent_name": session.get("agent_name", "build"),
                    "execution_profile": session.get("execution_profile", "standard"),
                    "scheduler_mode": session.get("scheduler_mode", "supervisor"),
                    "workdir": str(session.get("workdir", "") or ""),
                    "workdir_name": _workdir_name_from_path(
                        str(session.get("workdir", "") or ""),
                        workspace_context,
                    ),
                    "tenant_id": workspace_context.tenant_id,
                    "user_id": workspace_context.user_id,
                    "workspace_id": workspace_context.workspace_id,
                    "research_context": session.get("research_context", {}),
                    "wiki_context": session.get("wiki_context", {}),
                }
            )
            auto_workflow_run = None
            workflow_auto_route_declined_decision = None
            try:
                admission = await prepare_workflow(
                    session_id=session_id,
                    workdir=str(session.get("workdir", "") or session_workdir),
                    message=message, decision=workflow_route_decision, agent=agent,
                    owner_kwargs=session_workspace_owner,
                    question_handler=_build_websocket_question_handler(session_id, websocket),
                )
                auto_workflow_run = admission.run
                workflow_route_decision = admission.decision
                workflow_auto_route_declined_decision = admission.declined_decision
            except Exception as exc:
                # An explicit workflow must not silently become a different agent task.
                active_computer_finalizer.commit("error")
                try:
                    await websocket.send_json({
                        "type": "error", "session_id": session_id,
                        "content": f"workflow admission failed: {exc}", "done": True,
                    })
                finally:
                    await active_computer_finalizer.finish()
                    active_computer_finalizer = None
                continue

            # Send thinking status
            print("[WS] Sending thinking status")
            await websocket.send_json(
                {
                    "type": "status",
                    "status": "thinking",
                    "session_id": session_id,
                    "request_id": client_request_id,
                    "content": "Processing...",
                }
            )

            manager_team_delegate = None
            if session.get("agent_name") == "manager":
                manager_team_delegate = await _pick_manager_team_delegate_for_session(
                    session_id,
                    message,
                )

            # Define stream ownership before entering the cancellable turn so
            # early cancellation cannot observe unbound local variables.
            stream_iter: Any = None
            next_item_task: asyncio.Task | None = None
            try:
                if auto_workflow_run is not None:
                    takeover_succeeded = False
                    try:
                        takeover_succeeded = await _attempt_server_side_workflow_auto_route(
                            session_id=session_id,
                            workdir=str(session.get("workdir", "") or session_workdir),
                            message=message,
                            decision=workflow_route_decision,
                            run=auto_workflow_run,
                            websocket=websocket,
                            resource_context=session.get("resource_context") or {},
                        )
                    except Exception as exc:
                        print(f"[WS] server-side workflow auto-route takeover failed: {exc}")

                    if takeover_succeeded:
                        active_computer_finalizer.commit("workflow-completed")
                        if use_protocol_v2 and transport.adapter is not None:
                            await transport.adapter.finish_run()
                        continue

                    active_computer_finalizer.commit("native-dispatch-failed")
                    await websocket.send_json({
                        "type": "error", "session_id": session_id,
                        "content": "Native workflow dispatch failed; refusing an alternate agent execution",
                        "done": True,
                    })
                    continue
                elif workflow_auto_route_declined_decision is not None:
                    _inject_workflow_declined_direct_instruction(
                        agent,
                        decision=workflow_auto_route_declined_decision,
                    )
                    await websocket.send_json(
                        {
                            "type": "workflow_auto_route",
                            "session_id": session_id,
                            "decision": workflow_auto_route_declined_decision.to_dict(),
                            "run": None,
                            "auto_execution_scheduled": False,
                            "route_phase": "declined_by_user",
                            "done": False,
                        }
                    )
                elif _protein_design_resume_execution_mode(
                    workflow_route_decision
                ) in {"direct", "agent_loop"}:
                    _inject_protein_design_resume_instruction(
                        agent,
                        decision=workflow_route_decision,
                    )
                elif workflow_route_decision.intent_family == "direct_execution":
                    _inject_protein_direct_execution_instruction(
                        agent,
                        decision=workflow_route_decision,
                    )

                if manager_team_delegate:
                    await _remember_last_delegated_team(
                        session_id,
                        str(manager_team_delegate.get("agent_name") or ""),
                    )
                    delegated_team_session, _ = await _ensure_delegated_team_session(
                        session_id,
                        manager_team_delegate["agent_name"],
                        resource_context=session.get("resource_context") or {},
                    )
                    workers_created = await _bootstrap_fixed_team_workers(
                        delegated_team_session.id,
                        delegated_team_session.agent_name,
                    )

                    await websocket.send_json(
                        {
                            "type": "team_delegate",
                            "parent_session_id": session_id,
                            "session_id": delegated_team_session.id,
                            "agent_name": delegated_team_session.agent_name,
                            "display_name": manager_team_delegate["display_name"],
                            "reason": manager_team_delegate["reason"],
                            "team_key": manager_team_delegate.get("team_key"),
                            "route_rule": manager_team_delegate.get("route_rule"),
                            "route_source": manager_team_delegate.get("route_source"),
                            "workers_created": workers_created,
                            "team_snapshot": _runtime_team_snapshot(),
                        }
                    )

                    await _append_message_to_session(
                        session_id,
                        Message(
                            role="user",
                            content=message,
                            id=active_origin.get()["message_id"],
                            input_images=current_input_images,
                        ),
                    )

                    delegated_result = await _run_subagent_turn_background(
                        delegated_team_session.id,
                        session_id,
                        message,
                        output_format=output_format
                        if isinstance(output_format, dict)
                        else None,
                        outcome_contract=outcome_contract
                        if isinstance(outcome_contract, dict)
                        else None,
                        integrate_to_parent=False,
                        owner_kwargs=session_workspace_owner,
                        input_images=current_input_images,
                    )
                    if delegated_result.get("status") == "failed":
                        delegated_error = str(
                            delegated_result.get("error")
                            or "委派团队未返回可用结果"
                        )
                        delegated_result["response"] = str(
                            delegated_result.get("response")
                            or delegated_result.get("last_response")
                            or f"团队执行未完成。原因：{delegated_error}"
                        )
                    if delegated_result.get("status") == "cancelled":
                        active_computer_finalizer.commit("delegated-cancelled")
                        await websocket.send_json(
                            {"type": "status", "status": "cancelled", "content": "Cancelled"}
                        )
                        continue

                    assistant_message = Message(
                        role="assistant",
                        content=str(delegated_result.get("response") or ""),
                        id=str(delegated_result.get("message_id") or uuid.uuid4()),
                        research_grounding=dict(
                            delegated_result.get("research_grounding") or {}
                        ),
                        verification=_public_outcome_validation(
                            delegated_result.get("verification")
                            or delegated_result.get("outcome_validation")
                        ),
                    )
                    await _append_message_to_session(session_id, assistant_message)
                    if resume_scope is not None and delegated_result.get("status") not in {"failed", "cancelled"}:
                        from ..runtime.async_resume import commit_resume
                        await commit_resume(resume_scope, session_id=session_id, **session_workspace_owner)
                        resume_completed = True
                    done_runtime = _chat_runtime_status_payload(
                        session_id,
                        direct_running_override=False,
                    )

                    active_computer_finalizer.commit("completed")
                    await websocket.send_json(
                        {
                            "type": "message",
                            **done_runtime,
                            "role": "assistant",
                            "content": assistant_message.content,
                            "message_id": assistant_message.id,
                            "research_grounding": assistant_message.research_grounding or None,
                            "verification": assistant_message.verification or None,
                            "done": True,
                            **(
                                {
                                    "outcome_status": str(
                                        delegated_result.get("outcome_status") or ""
                                    ),
                                    "outcome_validation": _public_outcome_validation(
                                        delegated_result.get("outcome_validation")
                                    ),
                                }
                                if _frontend_outcome_validation_enabled()
                                else {}
                            ),
                        }
                    )
                    if use_protocol_v2 and transport.adapter is not None:
                        await transport.adapter.finish_run()
                    continue

                await _initialize_chat_turn_runtime(
                    session,
                    fallback_workdir=session_workdir or ServerState.get_workdir(),
                )

                async def permission_handler(tool_name: str, arguments: Dict[str, Any]):
                    if requested_desktop_permission_mode == "auto":
                        return "always"
                    request_id = str(uuid.uuid4())
                    future = ServerState.create_permission_future(
                        session_id, request_id
                    )

                    await websocket.send_json(
                        permission_request_event(
                            request_id=request_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            arguments=arguments,
                        )
                    )

                    try:
                        decision = await asyncio.wait_for(future, timeout=120)
                        return decision
                    except asyncio.TimeoutError:
                        return "reject"
                    finally:
                        ServerState.resolve_permission_future(
                            session_id, request_id, "reject"
                        )

                question_handler = _build_websocket_question_handler(
                    session_id,
                    websocket,
                )
                agent.set_permission_handler(permission_handler)
                agent._review_read_permissions = (
                    os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
                    and requested_desktop_permission_mode == "ask"
                )
                agent.set_question_handler(question_handler)
                # 标记当前前端请求是否开启流式，供工具执行器按需补发实时事件。
                setattr(agent, "_frontend_stream", bool(stream))

                stream_activity = StreamActivityTracker()
                async def agent_event_emitter(payload: Dict[str, Any]):
                    event = dict(payload or {})
                    event.setdefault("type", "agent_event")
                    event.setdefault("parent_session_id", session_id)
                    event_kind = str(event.get("kind") or event.get("type") or "agent_event")
                    event_status = str(event.get("status") or "").strip()
                    stream_activity.touch(
                        f"{event_kind}:{event_status}" if event_status else event_kind
                    )
                    # Legacy process notes are runtime status only. Public narration is
                    # emitted and persisted as an assistant commentary message.
                    if event_kind == "process_note":
                        return
                    await websocket.send_json(event)

                agent.set_agent_event_emitter(agent_event_emitter)

                # Track message boundary for this run
                before_count = len(agent.messages)

                tool_calls_data = []
                seen_tool_calls = set()
                delegated_outcome_status = ""
                delegated_outcome_validation: dict[str, Any] = {}

                from ..runtime.async_completion import trusted_completion_receipt

                async_receipt = trusted_completion_receipt(
                    message,
                    resource_persisted,
                )
                user_message_metadata: dict[str, Any] = {
                    "source_runtime_id": "tcode-native"
                }
                if async_receipt:
                    user_message_metadata["async_completion_receipt"] = async_receipt
                # Only this direct human ingress supplies computer task intent.
                # Async wakeups/tool messages must never replace it.
                agent._next_computer_task_text = message if not async_receipt else ""
                agent.set_next_user_message_context(
                    metadata=user_message_metadata,
                    message_id=active_origin.get()["message_id"],
                )
                agent._active_structure_context = structure_bridge.current_context((
                    workspace_context.tenant_id,
                    workspace_context.user_id,
                    workspace_context.workspace_id,
                    session_id,
                ))

                if stream:
                    result = None
                    final_content = ""
                    last_usage = None
                    first_stream_item_received = False
                    try:
                        first_item_timeout = float(
                            os.environ.get(
                                "TCODE_AGENT_STREAM_FIRST_ITEM_TIMEOUT_SECONDS", "45"
                            )
                        )
                    except Exception:
                        first_item_timeout = 45.0
                    try:
                        next_item_timeout = float(
                            os.environ.get(
                                "TCODE_AGENT_STREAM_NEXT_ITEM_TIMEOUT_SECONDS", "120"
                            )
                        )
                    except Exception:
                        next_item_timeout = 120.0
                    try:
                        max_stall_seconds = float(
                            os.environ.get(
                                "TCODE_AGENT_STREAM_MAX_STALL_SECONDS", "600"
                            )
                        )
                    except Exception:
                        max_stall_seconds = 0.0
                    try:
                        max_turn_seconds = float(
                            os.environ.get(
                                "TCODE_AGENT_STREAM_MAX_TURN_SECONDS", "3600"
                            )
                        )
                    except Exception:
                        max_turn_seconds = 3600.0
                    first_item_timeout = max(5.0, min(first_item_timeout, 600.0))
                    next_item_timeout = max(5.0, min(next_item_timeout, 1200.0))
                    max_stall_seconds = max(0.0, min(max_stall_seconds, 7200.0))
                    max_turn_seconds = max(0.0, min(max_turn_seconds, 24 * 3600.0))

                    print(
                        f"[WS] Starting agent stream: session={session_id}, "
                        f"agent={session.get('agent_name', 'build')}"
                    )
                    stream_activity.touch("agent_stream_started")
                    agent.set_next_user_input_images(current_input_images)
                    stream_iter = agent.run_stream(
                        message,
                        output_format=output_format,
                        outcome_contract=outcome_contract
                        if isinstance(outcome_contract, dict)
                        else None,
                    )
                    while True:
                        current_timeout = (
                            first_item_timeout
                            if not first_stream_item_received
                            else next_item_timeout
                        )
                        if next_item_task is None:
                            next_item_task = asyncio.create_task(stream_iter.__anext__())
                        try:
                            item = await asyncio.wait_for(
                                asyncio.shield(next_item_task), timeout=current_timeout
                            )
                            next_item_task = None
                            stream_activity.touch("stream_item")
                        except StopAsyncIteration:
                            next_item_task = None
                            break
                        except asyncio.TimeoutError as exc:
                            phase = "first-item" if not first_stream_item_received else "next-item"
                            stalled_for = stream_activity.idle_seconds()
                            turn_elapsed = stream_activity.elapsed_seconds()
                            if max_turn_seconds > 0 and turn_elapsed >= max_turn_seconds:
                                active_computer_finalizer.commit("maximum-duration")
                                await cancel_pending_stream_item(
                                    next_item_task,
                                    stream_iter,
                                )
                                next_item_task = None
                                raise RuntimeError(
                                    f"Agent turn exceeded maximum duration of "
                                    f"{max_turn_seconds:.0f}s; "
                                    f"last_activity={stream_activity.last_activity_kind}"
                                ) from exc
                            if max_stall_seconds > 0 and stalled_for >= max_stall_seconds:
                                active_computer_finalizer.commit("stalled")
                                await cancel_pending_stream_item(
                                    next_item_task,
                                    stream_iter,
                                )
                                next_item_task = None
                                raise RuntimeError(
                                    f"Agent stream stalled ({phase}) for {stalled_for:.0f}s; "
                                    f"last_activity={stream_activity.last_activity_kind}"
                                ) from exc
                            try:
                                await websocket.send_json(
                                    {
                                        "type": "stream_heartbeat",
                                        "kind": "stream_heartbeat",
                                        "status": "waiting",
                                        "session_id": session_id,
                                        "parent_session_id": session_id,
                                        "phase": phase,
                                        "timeout_seconds": current_timeout,
                                        "stalled_for_seconds": round(stalled_for, 1),
                                        "turn_elapsed_seconds": round(turn_elapsed, 1),
                                        "last_activity_kind": stream_activity.last_activity_kind,
                                    }
                                )
                            except Exception:
                                await cancel_pending_stream_item(
                                    next_item_task,
                                    stream_iter,
                                )
                                next_item_task = None
                                raise
                            continue

                        if not first_stream_item_received:
                            first_stream_item_received = True
                            print(f"[WS] Agent stream produced first item: session={session_id}")
                        if item.tool_calls:
                            await _send_frontend_tool_events(
                                websocket,
                                session_id=session_id,
                                item=item,
                                tool_calls_data=tool_calls_data,
                                seen_tool_calls=seen_tool_calls,
                            )

                        if item.role == "assistant":
                            content = item.content or ""
                            delta = (
                                content[len(final_content) :]
                                if content.startswith(final_content)
                                else content
                            )
                            final_content = content

                            # Track usage from the last message
                            if item.usage:
                                last_usage = item.usage

                            await websocket.send_json(
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": content,
                                    "delta": delta,
                                    "message_id": item.id,
                                    "presentation": getattr(item, "presentation", None),
                                    "metadata": _public_frontend_message_metadata(item) or None,
                                    "tool_calls": None,
                                    "done": False,
                                }
                            )
                            # Yield to the event loop so the browser can receive and render
                            # status/tool updates between fast tool executions.
                            if item.tool_calls:
                                await asyncio.sleep(0)
                            result = item

                    if result is None:
                        raise RuntimeError("No assistant response generated")

                    final_content = result.content
                    # Use the last captured usage from stream
                    if (
                        last_usage is None
                        and hasattr(agent, "total_usage")
                        and agent.total_usage.total_tokens > 0
                    ):
                        last_usage = agent.total_usage
                else:
                    print(
                        f"[WS] Starting agent run: session={session_id}, "
                        f"agent={session.get('agent_name', 'build')}"
                    )
                    result = await agent.run(
                        message,
                        output_format=output_format,
                        outcome_contract=outcome_contract
                        if isinstance(outcome_contract, dict)
                        else None,
                    )
                    print(f"[WS] Agent run completed: session={session_id}")

                    new_messages = agent.messages[before_count:]
                    for msg in new_messages:
                        await _send_frontend_tool_events(
                            websocket,
                            session_id=session_id,
                            item=msg,
                            tool_calls_data=tool_calls_data,
                            seen_tool_calls=seen_tool_calls,
                            emit_using_status=False,
                        )

                    for tc in tool_calls_data:
                        await websocket.send_json(
                            tool_using_event(
                                session_id=session_id,
                                tool_name=tc["tool_name"],
                                tool_args=tc["arguments"],
                                call_id=str(tc.get("call_id", "") or ""),
                            )
                        )

                    final_content = result.content

                # Generic fallback: if any agent hits a tool-level permission boundary and
                # the tool names a legal registered agent, delegate deterministically.  This is
                # intentionally not manager-only: build/general agents are also forbidden
                # from routing protected MCP targets directly.
                if tool_calls_data:
                    permission_delegate = _pick_permission_denied_delegate(
                        str(session.get("agent_name", "") or ""),
                        tool_calls_data,
                    )

                    if permission_delegate:
                        delegated_team_entry = TeamCatalog.get_by_key(
                            str(permission_delegate.get("team_key", "") or ""),
                            available_only=True,
                        )
                        if delegated_team_entry is not None:
                            await _remember_last_delegated_team(
                                session_id,
                                str(permission_delegate.get("agent_name") or ""),
                            )
                        delegated_agent_name = str(
                            permission_delegate.get("agent_name") or ""
                        ).strip()
                        if delegated_agent_name and BuiltinAgents.get(
                            delegated_agent_name
                        ):
                            delegated_team_session, _ = await _ensure_delegated_agent_session(
                                session_id,
                                delegated_agent_name,
                                resource_context=session.get("resource_context") or {},
                                delegation_id=str(
                                    permission_delegate.get("delegation_id") or ""
                                ),
                            )
                            workers_created = 0
                            if delegated_team_entry is not None:
                                workers_created = await _bootstrap_fixed_team_workers(
                                    delegated_team_session.id,
                                    delegated_team_session.agent_name,
                                )

                            await websocket.send_json(
                                {
                                    "type": "team_delegate",
                                    "delegation_kind": (
                                        "team"
                                        if delegated_team_entry is not None
                                        else "agent"
                                    ),
                                    "parent_session_id": session_id,
                                    "session_id": delegated_team_session.id,
                                    "agent_name": delegated_team_session.agent_name,
                                    "display_name": str(
                                        permission_delegate.get("display_name")
                                        or delegated_team_session.agent_name
                                    ),
                                    "reason": str(
                                        permission_delegate.get("reason")
                                        or permission_delegate.get("display_name")
                                        or delegated_team_session.agent_name
                                    ),
                                    "team_key": (
                                        delegated_team_entry.key
                                        if delegated_team_entry is not None
                                        else ""
                                    ),
                                    "route_rule": str(
                                        permission_delegate.get("route_rule")
                                        or "tool_permission_delegate"
                                    ),
                                    "route_source": str(
                                        permission_delegate.get("route_source")
                                        or "runtime_tool_metadata"
                                    ),
                                    "tool_name": permission_delegate.get("tool_name"),
                                    "allowed_agents": permission_delegate.get("allowed_agents"),
                                    "delegation_id": permission_delegate.get("delegation_id"),
                                    "delegation_contract": permission_delegate.get(
                                        "delegation_contract"
                                    ),
                                    "workers_created": workers_created,
                                    "team_snapshot": _runtime_team_snapshot(),
                                }
                            )

                            delegated_message = _build_permission_delegation_message(
                                permission_delegate,
                                message,
                            )
                            run_lock_key = (
                                f"run:{delegated_team_session.id}:"
                                f"{permission_delegate.get('delegation_id') or ''}"
                            )
                            async with _delegated_session_lock(run_lock_key):
                                delegated_result = await _run_subagent_turn_background(
                                    delegated_team_session.id,
                                    session_id,
                                    delegated_message,
                                    output_format=output_format
                                    if isinstance(output_format, dict)
                                    else None,
                                    outcome_contract=outcome_contract
                                    if isinstance(outcome_contract, dict)
                                    else None,
                                    integrate_to_parent=False,
                                    owner_kwargs=session_workspace_owner,
                                )
                            if delegated_result.get("status") == "failed":
                                delegated_error = str(
                                    delegated_result.get("error")
                                    or "委派执行未返回可用结果"
                                )
                                delegated_result["response"] = str(
                                    delegated_result.get("response")
                                    or delegated_result.get("last_response")
                                    or (
                                        "自动委派未完成。执行代理返回错误："
                                        f"{delegated_error}"
                                    )
                                )
                            if delegated_result.get("status") == "cancelled":
                                active_computer_finalizer.commit("delegated-cancelled")
                                await websocket.send_json(
                                    {
                                        "type": "status",
                                        "status": "cancelled",
                                        "content": "Cancelled",
                                    }
                                )
                                continue

                            delegated_content = str(
                                delegated_result.get("response")
                                or delegated_result.get("last_response")
                                or ""
                            )
                            final_content = delegated_content or final_content
                            delegated_outcome_status = str(
                                delegated_result.get("outcome_status") or ""
                            )
                            delegated_outcome_validation = dict(
                                delegated_result.get("outcome_validation") or {}
                            )
                            tool_calls_data = []
                            result = Message(
                                role="assistant",
                                content=final_content,
                                id=str(delegated_result.get("message_id") or str(uuid.uuid4())),
                                research_grounding=dict(
                                    delegated_result.get("research_grounding") or {}
                                ),
                                verification=_public_outcome_validation(
                                    delegated_result.get("verification")
                                    or delegated_result.get("outcome_validation")
                                ),
                            )

                if not final_content and tool_calls_data:
                    tool_summaries = []
                    for tc in tool_calls_data:
                        if tc["tool_name"] == "write":
                            tool_summaries.append(
                                f"Created file: {tc['arguments'].get('filePath', 'unknown')}"
                            )
                        elif tc["tool_name"] == "edit":
                            tool_summaries.append(
                                f"Edited file: {tc['arguments'].get('filePath', 'unknown')}"
                            )
                        elif tc["tool_name"] == "bash":
                            tool_summaries.append(
                                f"Executed command: {tc['arguments'].get('command', 'unknown')[:50]}..."
                            )
                        elif tc["tool_name"] == "read":
                            tool_summaries.append(
                                f"Read file: {tc['arguments'].get('filePath', tc['arguments'].get('path', 'unknown'))}"
                            )
                        else:
                            tool_summaries.append(f"Used tool: {tc['tool_name']}")
                    final_content = "✅ " + ", ".join(tool_summaries)

                usage_data = None
                completed_at = int(datetime.now().timestamp() * 1000)
                for persisted_message in agent.messages[before_count:]:
                    raw_metadata = getattr(persisted_message, "metadata", None)
                    message_metadata = (
                        dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
                    )
                    message_metadata.setdefault("source_runtime_id", "tcode-native")
                    if persisted_message is result:
                        message_metadata.setdefault("completed_at", completed_at)
                    persisted_message.metadata = message_metadata

                # Try to get usage from result.usage first, then from agent.total_usage
                result_usage = getattr(result, "usage", None)
                if result_usage:
                    usage_data = {
                        "prompt_tokens": getattr(result_usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(
                            result_usage, "completion_tokens", 0
                        ),
                        "total_tokens": getattr(result_usage, "total_tokens", 0),
                    }
                elif (
                    hasattr(agent, "total_usage") and agent.total_usage.total_tokens > 0
                ):
                    # Fallback to agent's total_usage if result doesn't have usage
                    usage_data = {
                        "prompt_tokens": agent.total_usage.prompt_tokens,
                        "completion_tokens": agent.total_usage.completion_tokens,
                        "total_tokens": agent.total_usage.total_tokens,
                    }

                if usage_data:
                    await websocket.send_json(usage_status_event(usage_data))

                message_runtime = _chat_runtime_status_payload(
                    session_id,
                    direct_running_override=False,
                )
                message_data = {
                    "type": "message",
                    **message_runtime,
                    "role": "assistant",
                    "content": final_content,
                    "message_id": result.id,
                    "presentation": getattr(result, "presentation", None),
                    "metadata": _public_frontend_message_metadata(result) or None,
                    "tool_calls": tool_calls_data if tool_calls_data else None,
                    "content_blocks": normalize_content_blocks(
                        getattr(result, "content_blocks", [])
                    )
                    or None,
                    "research_grounding": dict(
                        getattr(result, "research_grounding", {}) or {}
                    )
                    or None,
                    "verification": _public_outcome_validation(
                        getattr(result, "verification", None)
                        or delegated_outcome_validation
                        or dict(getattr(agent, "_last_outcome_validation", {}) or {})
                    )
                    if _frontend_outcome_validation_enabled()
                    else None,
                    "usage": usage_data,
                    "done": True,
                }
                # The agent has reached a terminal result even if delivery of
                # that result races a short-lived WebSocket disconnect.
                active_computer_finalizer.commit("completed")
                if _frontend_outcome_validation_enabled():
                    message_data["outcome_status"] = delegated_outcome_status or str(
                        getattr(agent, "_last_outcome_status", "") or ""
                    )
                    message_data["outcome_validation"] = _public_outcome_validation(
                        delegated_outcome_validation
                        or dict(getattr(agent, "_last_outcome_validation", {}) or {})
                    )
                print(
                    "[WS] Sending final message: "
                    f"tool_calls={len(tool_calls_data)} chars={len(final_content or '')}"
                )
                await websocket.send_json(message_data)

                metadata_update = None
                agent_resource_context = getattr(agent, "_session_resource_context", None)
                if isinstance(agent_resource_context, dict):
                    session["resource_context"] = _merge_resource_context(
                        session.get("resource_context"),
                        agent_resource_context,
                    )
                    persisted_current = await SessionManager.get(
                        session_id,
                        **session_owner,
                    )
                    metadata_update = dict(
                        getattr(persisted_current, "metadata", {}) or {}
                    )
                    metadata_update["execution_profile"] = session.get(
                        "execution_profile",
                        "standard",
                    )
                    metadata_update["scheduler_mode"] = session.get(
                        "scheduler_mode",
                        "supervisor",
                    )
                    metadata_update["reasoning_effort"] = session.get("reasoning_effort")
                    metadata_update["model"] = session.get("model")
                    metadata_update["resource_context"] = session.get(
                        "resource_context",
                        {},
                    )

                await SessionManager.update(
                    session_id,
                    **session_workspace_owner,
                    messages=list(agent.messages),
                    agent_name=session.get("agent_name", "build"),
                    workdir=str(
                        session.get("workdir", "")
                        or await _resolve_session_workdir(
                            session_id,
                            workspace_context,
                        )
                    ),
                    last_active_at=datetime.now().timestamp(),
                    **({"metadata": metadata_update} if metadata_update is not None else {}),
                )
                if resume_scope is not None:
                    from ..runtime.async_resume import commit_resume
                    await commit_resume(resume_scope, session_id=session_id, **session_workspace_owner)
                    resume_completed = True
                if use_protocol_v2 and transport.adapter is not None:
                    await transport.adapter.finish_run(message_data)

            except asyncio.CancelledError:
                # ``__anext__`` is deliberately shielded from heartbeat
                # timeouts.  Explicit turn cancellation must still reap it;
                # otherwise a cancelled session leaves tool execution running
                # without an owning WebSocket task.
                active_computer_finalizer.commit_if_running("cancelled")
                if next_item_task is not None and stream_iter is not None:
                    await cancel_pending_stream_item(next_item_task, stream_iter)
                    next_item_task = None
                await websocket.send_json(
                    {"type": "status", "status": "cancelled", "content": "Cancelled"}
                )
            except Exception as e:
                if isinstance(e, WebSocketDisconnect) or _websocket_is_disconnected(websocket):
                    if resume_scope is not None and stream_iter is not None:
                        # Reap only this restore's model stream before allowing
                        # a retry; do not cancel the independent compute job.
                        await cancel_pending_stream_item(next_item_task, stream_iter)
                        next_item_task = None
                    active_computer_finalizer.preserve_on_disconnect()
                    raise
                active_computer_finalizer.commit_if_running("error")
                if next_item_task is not None and stream_iter is not None:
                    await cancel_pending_stream_item(next_item_task, stream_iter)
                    next_item_task = None
                error_text = redact_telemetry_text(
                    str(e) or "Unknown error",
                    limit=2_000,
                )
                try:
                    await _append_message_to_session(
                        session_id,
                        Message(
                            role="assistant",
                            content=f"发生错误: {error_text}",
                            id=str(uuid.uuid4()),
                        ),
                    )
                except Exception:
                    pass
                await websocket.send_json(
                    {"type": "error", "error": error_text, "session_id": session_id}
                )
            finally:
                await active_computer_finalizer.finish()
                active_computer_finalizer = None
                if session_id and session_id in ServerState.agents:
                    ServerState.agents[session_id].set_permission_handler(None)
                    ServerState.agents[session_id]._review_read_permissions = False
                    ServerState.agents[session_id].set_question_handler(None)
                    ServerState.agents[session_id].set_agent_event_emitter(None)
                release_turn_claim()
                if session_id and guidance_v2_enabled():
                    _ensure_session_guidance_drain_v2(session_id)
                elif session_id and ServerState.pending_guidance_count(session_id) > 0:
                    _ensure_session_guidance_drain(session_id)

    except WebSocketDisconnect:
        transport.closed = True
        if active_computer_finalizer is not None:
            active_computer_finalizer.preserve_on_disconnect()
            active_computer_finalizer = None
        release_turn_claim()
        if session_id:
            cleanup_tmp_dir = _env_truthy(
                "TCODE_CLEANUP_UPLOAD_ON_DISCONNECT",
                default=True,
            )
            # A chat socket is intentionally closed by the frontend after the
            # foreground acknowledgement.  An auto-routed Workflow may already
            # own a background task at that point, so treating this transport
            # disconnect as a session shutdown would cancel the task during its
            # startup delay and leave the durable run stuck in ``queued``.
            # Explicit cancel/session/workspace deletion still goes through
            # ``ServerState.cancel_session`` and retains its cancellation
            # semantics.
            await _cleanup_disconnected_chat_session(
                session_id,
                cleanup_tmp_dir=cleanup_tmp_dir,
            )
    except Exception as exc:
        if active_computer_finalizer is not None:
            active_computer_finalizer.commit_if_running("error")
            await active_computer_finalizer.finish()
            active_computer_finalizer = None
        release_turn_claim()
        error_text = redact_telemetry_text(
            str(getattr(exc, "detail", "") or exc) or "Unknown error",
            limit=2_000,
        )
        try:
            print(f"[WS] Unhandled startup error: {error_text}")
            await websocket.send_json(
                {"type": "error", "error": error_text, "session_id": session_id}
            )
        except Exception:
            transport.closed = True
    finally:
        # Covers cancellation during admission/startup as well as normal loop
        # exit; a failed restore must not strand the foreground lease or scope.
        release_turn_claim()
