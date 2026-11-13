"""Chat cancellation, guidance queues and interaction replies.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import hashlib
import json
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..runtime.capacity import ExecutionCapacityController
from ..runtime.gateway import guidance_mode_for_runtime, run_coordinator
from ..runtime.guidance import (
    GuidanceMode,
    GuidanceStaleTurn,
    GuidanceStatus,
    GuidanceSupersedeConflict,
    GuidanceVersionConflict,
    OwnerScope,
)
from ..runtime.guidance import (
    GuidanceNotCancellable as GuidanceV2NotCancellable,
)
from ..runtime.guidance import (
    GuidanceNotFound as GuidanceV2NotFound,
)
from ..runtime.guidance import (
    GuidanceQueueFull as GuidanceV2QueueFull,
)
from ..runtime.guidance_store import GuidanceSQLiteStore
from ..runtime.interaction_store import (
    InteractionConflictError,
    InteractionNotFoundError,
)
from ..runtime.telemetry import TelemetryGateway
from ..session.manager import Session, SessionManager
from ..task.outcome_contract import OutcomeContract
from ..task.outcome_revision import (
    OutcomeContractRevisionPlanner,
    invalidate_superseded_message_verifications,
)
from ..team.coordinator import (
    cancel_team_background_tasks,
    cancel_worker_task,
    worker_task_active,
)
from . import agui_api, harness_api
from .message_resources import (
    _build_message_resource_context,
)
from ._shared import (
    _cancel_descendant_sessions,
    _cancel_runtime_subtree_for_interrupt_guidance,
    _chat_runtime_status_payload,
    _direct_session_turn_active,
    _ensure_session_guidance_drain_v2,
    _guidance_store_workdir,
    _list_descendant_sessions_recursive,
    _merge_resource_context,
    _migrate_session_guidance_v2,
    _notify_parent_sessions_of_subagent_interrupt,
    _workflow_background_task_active,
)
from .workspace_scope import (
    _resolve_session_workdir,
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _session_owner_kwargs_from_session,
    _workspace_context_from_request,
)
from .auth import auth_required, require_current_user
from .guidance_runtime import guidance_runtime
from .input_limits import ChatInputLimitError, validate_chat_message
from .schemas import ChatGuideRequest, PermissionReplyRequest, QuestionReplyRequest
from .state import ServerState

router = APIRouter()


@router.post("/api/chat/cancel")
async def cancel_chat(
    request: Request,
    session_id: str = Query(...),
    notify_parent: bool = Query(False),
    auto_replan: bool = Query(False),
    controller_session_id: str = Query(""),
    clear_guidance: bool = Query(False),
):
    workspace_context = _workspace_context_from_request(request)
    request_owner = _session_owner_kwargs(workspace_context)
    persisted = await SessionManager.get(session_id, **request_owner)
    runtime = ServerState.sessions.get(session_id)
    runtime_matches = isinstance(runtime, dict) and _runtime_session_matches_context(
        runtime, workspace_context
    )
    if persisted is None and not runtime_matches:
        raise HTTPException(404, "Session not found")
    session_owner = (
        _session_owner_kwargs_from_session(persisted)
        if persisted is not None
        else _session_owner_kwargs(workspace_context, include_workspace=True)
    )
    runtime_before = _chat_runtime_status_payload(session_id)
    was_busy = bool(
        runtime_before.get("running")
        or runtime_before.get("pending_guidance_count", 0)
        or runtime_before.get("guidance_worker_active")
    )
    # Stop the foreground turn before slower descendant/team cleanup.  The old
    # order left the model free to start more tools while the HTTP request spent
    # several seconds walking the collaboration tree.
    ServerState.mark_session_cancelled(session_id)
    await ServerState.cancel_session(session_id, clear_guidance=clear_guidance)
    cancelled_team_task_details: dict[str, dict[str, Any]] = {}
    cancelled_worker_task = cancel_worker_task(session_id)
    cancelled_harness_run = await run_coordinator.cancel_active(
        owner_key=f"{workspace_context.tenant_id}:{workspace_context.user_id}",
        session_id=session_id,
    )

    for team_session_id in [session_id] + [
        session.id
        for session in await _list_descendant_sessions_recursive(
            session_id,
            owner_kwargs=session_owner,
            include_side_chats=False,
        )
    ]:
        cancelled = await cancel_team_background_tasks(team_session_id)
        if cancelled.get("cancelled_worker_session_ids") or cancelled.get("cancelled_auxiliary_tasks"):
            cancelled_team_task_details[team_session_id] = cancelled

    cancelled_descendant_session_ids = await _cancel_descendant_sessions(
        session_id,
        clear_guidance=clear_guidance,
        owner_kwargs=session_owner,
    )

    extra: dict[str, Any] = {}
    if (
        notify_parent
        and was_busy
        and persisted is not None
        and str(getattr(persisted, "parent_id", "") or "").strip()
    ):
        extra = await _notify_parent_sessions_of_subagent_interrupt(
            persisted,
            controller_session_id=str(controller_session_id or "").strip(),
            auto_replan=bool(auto_replan),
            owner_kwargs=session_owner,
        )

    return {
        "status": "cancelled",
        "session_id": session_id,
        "cancelled_descendant_session_ids": cancelled_descendant_session_ids,
        "cancelled_worker_task": cancelled_worker_task,
        "cancelled_harness_run": cancelled_harness_run,
        "cancelled_team_task_details": cancelled_team_task_details,
        **_chat_runtime_status_payload(session_id),
        **extra,
    }


async def _guide_chat_v2(
    *,
    payload: ChatGuideRequest,
    message: str,
    persisted: Session,
    runtime: Optional[dict[str, Any]],
    session_owner: dict[str, str],
    start_if_idle: bool = True,
) -> dict[str, Any]:
    await _migrate_session_guidance_v2(persisted)
    guidance_workdir = _guidance_store_workdir()
    owner_scope = OwnerScope(
        tenant_id=str(getattr(persisted, "tenant_id", "") or "").strip(),
        user_id=str(getattr(persisted, "user_id", "") or "").strip(),
        workspace_id=str(getattr(persisted, "workspace_id", "") or "").strip(),
    )
    requested_mode = GuidanceMode(
        str(payload.mode or ("steer" if bool(payload.interrupt) else "queue"))
    )
    harness_state = harness_api.harness_handoff_store.get_state(
        owner_key=f"{owner_scope.tenant_id}:{owner_scope.user_id}",
        workspace_id=owner_scope.workspace_id,
        session_id=persisted.id,
        session_metadata=dict(getattr(persisted, "metadata", {}) or {}),
    )
    guidance_runtime_id = str(
        harness_state.get("current_runtime_id") or "tcode-native"
    )
    runtime_models = dict(harness_state.get("runtime_models") or {})
    guidance_model_id = str(
        runtime_models.get(guidance_runtime_id)
        or getattr(persisted, "model", "")
        or ""
    ).strip()
    active_harness_run = run_coordinator.store.active_for_session(
        f"{owner_scope.tenant_id}:{owner_scope.user_id}",
        persisted.id,
    )
    source_run_id = str((active_harness_run or {}).get("run_id") or "").strip()
    runtime_guidance_mode = await guidance_mode_for_runtime(guidance_runtime_id)
    if runtime_guidance_mode == "unsupported":
        raise HTTPException(
            409,
            {
                "code": "harness_guidance_unsupported",
                "message": (
                    f"Harness {guidance_runtime_id} does not support Guidance"
                ),
            },
        )
    accepted_request_mode = (
        GuidanceMode.QUEUE
        if requested_mode is GuidanceMode.STEER
        and runtime_guidance_mode == "queue_only"
        else requested_mode
    )
    initial_effect = (
        "downgraded_runtime_queue_only"
        if accepted_request_mode is not requested_mode
        else ""
    )
    restart_turn_steer = bool(
        accepted_request_mode is GuidanceMode.STEER
        and runtime_guidance_mode == "restart_turn"
        and source_run_id
    )
    descendant_policy = str(payload.descendant_policy or "preserve")
    if bool(payload.cancel_descendants):
        descendant_policy = "cancel_all"
    client_request_id = str(payload.client_request_id or uuid.uuid4())
    active_turn = dict((getattr(persisted, "metadata", {}) or {}).get("active_turn") or {})
    received_phase = str(
        active_turn.get("state")
        or ((runtime or {}).get("phase") if isinstance(runtime, dict) else "")
        or ("running" if ServerState.is_running(persisted.id) else "idle")
    )
    # The UI is already running while the native Harness is still registering
    # its turn. Never silently convert an explicit native steer into a new turn.
    native_steer = accepted_request_mode is GuidanceMode.STEER and runtime_guidance_mode == "in_turn"
    preparation_task = ServerState.get_turn_task(persisted.id)
    deadline = asyncio.get_running_loop().time() + 6.0
    try:
        while True:
            try:
                item, duplicate = await asyncio.to_thread(
                    GuidanceSQLiteStore.accept,
                    session_id=persisted.id,
                    owner=owner_scope,
                    client_request_id=client_request_id,
                    target_turn_id=str(payload.target_turn_id or ""),
                    mode=accepted_request_mode,
                    message=message,
                    file_refs=list(payload.file_refs or []),
                    output_format=(
                        dict(payload.output_format)
                        if isinstance(payload.output_format, dict)
                        else None
                    ),
                    outcome_contract=(
                        dict(payload.outcome_contract)
                        if isinstance(payload.outcome_contract, dict)
                        else None
                    ),
                    contract_revision=None,
                    apply_policy=str(payload.apply_policy or "next_safe_point"),
                    descendant_policy=descendant_policy,
                    supersedes_guidance_id=str(payload.supersedes_guidance_id or ""),
                    received_phase=received_phase,
                    workdir=guidance_workdir,
                    runtime_id=guidance_runtime_id,
                    model_id=guidance_model_id,
                    harness_switch_version=int(harness_state.get("switch_version") or 0),
                    source_run_id=source_run_id,
                    allow_unbound_steer=restart_turn_steer,
                    initial_effect=initial_effect,
                    require_active_steer=native_steer,
                )
                break
            except GuidanceStaleTurn:
                if (
                    not native_steer
                    or payload.target_turn_id
                    or (preparation_task is not None and ServerState.get_turn_task(persisted.id) is not preparation_task)
                    or asyncio.get_running_loop().time() >= deadline
                ):
                    raise
                preparation_task = ServerState.get_turn_task(persisted.id)
                await asyncio.sleep(0.15)
    except GuidanceV2NotFound as exc:
        raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from exc
    except GuidanceStaleTurn as exc:
        raise HTTPException(409, {
            "code": exc.code,
            "message": "当前回合还未就绪或已经结束，请稍后重试，或选择排队。",
        }) from exc
    except GuidanceSupersedeConflict as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
    except GuidanceV2QueueFull as exc:
        raise HTTPException(
            429,
            {"code": exc.code, "message": str(exc)},
            headers={"Retry-After": "1"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_guidance", "message": str(exc)}) from exc

    guidance_runtime.ensure_workdir(guidance_workdir)
    TelemetryGateway.emit(
        "guidance.accept",
        {
            "mode": item.mode.value,
            "requested_mode": requested_mode.value,
            "result": "duplicate" if duplicate else "accepted",
            "status": item.status.value,
            "downgraded": item.mode is not requested_mode,
        },
        tenant_id=owner_scope.tenant_id,
        user_id=owner_scope.user_id,
        workspace_id=owner_scope.workspace_id,
        session_id=item.session_id,
        request_id=client_request_id,
    )

    async def _classify_guidance(
        classified_item,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], str]:
        active_agent = ServerState.agents.get(classified_item.session_id)
        descendant_sessions = await _list_descendant_sessions_recursive(
            classified_item.session_id,
            owner_kwargs=session_owner,
        )
        active_descendants: list[dict[str, Any]] = []
        for descendant in descendant_sessions:
            descendant_id = str(getattr(descendant, "id", "") or "").strip()
            if not descendant_id:
                continue
            if not (
                _direct_session_turn_active(descendant_id)
                or _workflow_background_task_active(descendant_id)
                or worker_task_active(descendant_id)
            ):
                continue
            delegation_scope = dict(
                (getattr(descendant, "metadata", {}) or {}).get("delegation_scope")
                or {}
            )
            active_descendants.append(
                {
                    "session_id": descendant_id,
                    "parent_session_id": str(
                        getattr(descendant, "parent_id", "") or ""
                    ),
                    "parent_contract_id": str(
                        delegation_scope.get("parent_contract_id") or ""
                    ),
                    "parent_contract_version": int(
                        delegation_scope.get("parent_contract_version") or 0
                    ),
                    "goal_fingerprint": str(
                        delegation_scope.get("goal_fingerprint") or ""
                    ),
                    "scope_summary": str(
                        delegation_scope.get("scope_summary") or ""
                    )[:1200],
                    "artifact_paths": list(
                        delegation_scope.get("artifact_paths") or []
                    )[:32],
                }
            )
        active_contract = getattr(active_agent, "_active_outcome_contract", None)
        if not isinstance(active_contract, OutcomeContract) and active_agent is not None:
            raw_contract = dict(getattr(active_agent, "_last_outcome_contract", {}) or {})
            if raw_contract:
                try:
                    active_contract = OutcomeContract.compile(
                        str(raw_contract.get("goal") or classified_item.message),
                        supplied=raw_contract,
                    )
                except Exception:
                    active_contract = None
        from .auth import session_result_verification_settings
        verification_settings = await session_result_verification_settings(persisted)
        revision = await OutcomeContractRevisionPlanner.plan(
            active_contract if isinstance(active_contract, OutcomeContract) else None,
            classified_item.message,
            provider=getattr(active_agent, "provider", None) if verification_settings["enabled"] and verification_settings["mode"] != "light" else None,
            proposed_by="user",
            usage_session=persisted,
            conversation_context=(
                list(getattr(active_agent, "messages", []) or [])[-8:]
                if active_agent is not None
                else []
            ),
            active_descendants=active_descendants,
        )
        revision_payload = revision.to_dict(include_contract=False)
        effective_contract = classified_item.outcome_contract
        if effective_contract is None and revision.can_apply and revision.revised_contract is not None:
            effective_contract = revision.revised_contract.to_dict()
        effect = (
            "turn_cancel_requested"
            if str(revision.classification or "").upper() == "CANCEL"
            and not revision.needs_user_confirmation
            else "contract_revised"
            if revision.can_apply
            else "replan"
        )
        return revision_payload, effective_contract, effect

    async def _cancel_invalidated_descendants(classified_item) -> list[str]:
        policy = str(classified_item.descendant_policy or "preserve")
        if policy == "cancel_all":
            cancelled = await _cancel_runtime_subtree_for_interrupt_guidance(
                classified_item.session_id,
                clear_guidance=False,
                owner_kwargs=session_owner,
            )
            cancelled_ids = list(
                cancelled.get("cancelled_descendant_session_ids") or []
            )
            for descendant_id in cancelled_ids:
                descendant = await SessionManager.get(descendant_id, **session_owner)
                if descendant is None:
                    continue
                descendant_metadata = dict(
                    getattr(descendant, "metadata", {}) or {}
                )
                descendant_metadata["superseded_context"] = True
                descendant_metadata["superseded_by_guidance_id"] = (
                    classified_item.id
                )
                await SessionManager.update(
                    descendant_id,
                    **session_owner,
                    metadata=descendant_metadata,
                )
            return cancelled_ids
        if policy != "cancel_invalidated":
            return []
        if str(
            os.environ.get("TCODE_STEER_DESCENDANT_POLICY_ENABLED", "0") or "0"
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return []
        revision_payload = dict(classified_item.contract_revision or {})
        impact = dict(revision_payload.get("descendant_impact") or {})
        if not bool(revision_payload.get("can_apply")):
            return []
        try:
            confidence = float(impact.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.85:
            return []
        requested_ids = {
            str(value or "").strip()
            for value in list(impact.get("candidate_session_ids") or [])
            if str(value or "").strip()
        }
        descendants = await _list_descendant_sessions_recursive(
            classified_item.session_id,
            owner_kwargs=session_owner,
        )
        cancelled: list[str] = []
        for descendant in descendants:
            descendant_id = str(getattr(descendant, "id", "") or "").strip()
            if descendant_id not in requested_ids:
                continue
            delegation_scope = dict(
                (getattr(descendant, "metadata", {}) or {}).get("delegation_scope")
                or {}
            )
            if not delegation_scope:
                continue
            descendant_metadata = dict(getattr(descendant, "metadata", {}) or {})
            if descendant_metadata.get("uncancellable_external_side_effect"):
                continue
            cancel_worker_task(descendant_id)
            await cancel_team_background_tasks(descendant_id)
            await ServerState.cancel_session(descendant_id, clear_guidance=False)
            descendant_metadata["superseded_context"] = True
            descendant_metadata["superseded_by_guidance_id"] = classified_item.id
            await SessionManager.update(
                descendant_id,
                **session_owner,
                metadata=descendant_metadata,
            )
            cancelled.append(descendant_id)
        return cancelled

    async def _after_guidance_classified(classified_item) -> list[str]:
        cancelled = await _cancel_invalidated_descendants(classified_item)
        if (
            classified_item.mode is GuidanceMode.STEER
            and classified_item.status is GuidanceStatus.PENDING
            and str(classified_item.runtime_id or "") != "tcode-native"
        ):
            mode = await guidance_mode_for_runtime(
                str(classified_item.runtime_id or "")
            )
            if mode == "restart_turn":
                owner_key = f"{classified_item.tenant_id}:{classified_item.user_id}"
                await run_coordinator.cancel_active(
                    owner_key=owner_key,
                    session_id=classified_item.session_id,
                )
                deadline = asyncio.get_running_loop().time() + 5.0
                while run_coordinator.store.active_for_session(
                    owner_key, classified_item.session_id
                ) is not None:
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.05)
                _ensure_session_guidance_drain_v2(classified_item.session_id)
        return cancelled

    async def _cancel_guided_turn(classified_item) -> None:
        if classified_item.descendant_policy == "cancel_all":
            await _cancel_runtime_subtree_for_interrupt_guidance(
                classified_item.session_id,
                clear_guidance=False,
                owner_kwargs=session_owner,
            )
        if str(classified_item.runtime_id or "") != "tcode-native":
            await run_coordinator.cancel_active(
                owner_key=f"{classified_item.tenant_id}:{classified_item.user_id}",
                session_id=classified_item.session_id,
            )
        await ServerState.cancel_session(classified_item.session_id, clear_guidance=False)

    if item.status.value == "classifying":
        guidance_runtime.schedule_classification(
            workdir=guidance_workdir,
            owner=owner_scope,
            classifier=_classify_guidance,
            cancel_turn=_cancel_guided_turn,
            post_classify=_after_guidance_classified,
        )
    if start_if_idle and item.mode is GuidanceMode.QUEUE and not ServerState.is_running(item.session_id):
        if not duplicate:
            ServerState.clear_session_cancelled(item.session_id)
        _ensure_session_guidance_drain_v2(item.session_id)

    counts = await asyncio.to_thread(
        GuidanceSQLiteStore.pending_counts,
        session_id=item.session_id,
        owner=owner_scope,
        workdir=guidance_workdir,
    )
    pending_total = int(counts.get("pending_steer_count", 0)) + int(
        counts.get("pending_queue_count", 0)
    )
    return {
        "status": "duplicate_request" if duplicate else "accepted",
        "session_id": item.session_id,
        "guidance_id": item.id,
        "client_request_id": item.client_request_id,
        "idempotency": "client_provided" if payload.client_request_id else "server_generated",
        "requested_mode": requested_mode.value,
        "accepted_mode": item.mode.value,
        "target_turn_id": item.target_turn_id,
        "sequence": item.sequence,
        "guidance_status": item.status.value,
        "apply_state": (
            "understanding_guidance"
            if item.status.value == "classifying"
            else "queued_for_next_turn"
        ),
        "received_phase": item.received_phase,
        "downgrade_reason": item.effect if item.effect.startswith("downgraded_") else "",
        **_chat_runtime_status_payload(item.session_id),
        "pending_guidance_count": pending_total,
        "pending_guidance_interrupt": bool(counts.get("pending_steer_count", 0)),
        "guidance_worker_active": pending_total > 0,
        **counts,
    }


@router.post("/api/chat/guide")
async def guide_chat(payload: ChatGuideRequest, request: Request):
    session_id = str(payload.session_id or "").strip()
    try:
        message = validate_chat_message(payload.message).strip()
    except ChatInputLimitError as exc:
        raise HTTPException(413, detail=exc.as_websocket_event()) from exc
    if not session_id:
        raise HTTPException(400, "session_id is required")
    if not message:
        raise HTTPException(400, "message is required")

    workspace_context = _workspace_context_from_request(request)
    request_owner = _session_owner_kwargs(workspace_context)
    persisted = await SessionManager.get(session_id, **request_owner)
    runtime = ServerState.sessions.get(session_id)
    if not _runtime_session_matches_context(runtime, workspace_context):
        runtime = None
    if not persisted and not runtime:
        raise HTTPException(404, "Session not found")
    if persisted is not None and (persisted.metadata or {}).get("side_question"):
        raise HTTPException(409, "侧边聊天不支持追加指导；请中止当前回复或等待结束后发送。")
    session_owner = (
        _session_owner_kwargs_from_session(persisted)
        if persisted is not None
        else _session_owner_kwargs(workspace_context, include_workspace=True)
    )

    session_context = (
        _workspace_context_from_request(
            None,
            tenant_id=str(getattr(persisted, "tenant_id", "") or ""),
            user_id=str(getattr(persisted, "user_id", "") or ""),
            workspace_id=str(getattr(persisted, "workspace_id", "") or ""),
        )
        if persisted is not None
        else workspace_context
    )
    session_workdir = str(
        getattr(persisted, "workdir", "")
        or ((runtime or {}).get("workdir") if isinstance(runtime, dict) else "")
        or await _resolve_session_workdir(session_id, session_context)
    ).strip()
    try:
        incoming_resource_context = _build_message_resource_context(
            message,
            session_id=session_id,
            workdir=session_workdir,
            file_refs=payload.file_refs,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if incoming_resource_context:
        persisted_metadata = dict(getattr(persisted, "metadata", {}) or {})
        merged_resource_context = _merge_resource_context(
            (runtime or {}).get("resource_context") if isinstance(runtime, dict) else None,
            persisted_metadata.get("resource_context"),
        )
        merged_resource_context = _merge_resource_context(
            merged_resource_context,
            incoming_resource_context,
        )
        if runtime is None:
            runtime = ServerState.get_or_create_session(
                session_id,
                agent_name=str(getattr(persisted, "agent_name", "") or "build"),
                workdir=session_workdir,
            )
            runtime["tenant_id"] = session_context.tenant_id
            runtime["user_id"] = session_context.user_id
            runtime["workspace_id"] = session_context.workspace_id
        runtime["resource_context"] = dict(merged_resource_context)
        active_agent = ServerState.agents.get(session_id)
        if active_agent is not None:
            active_agent.set_session_resource_context(merged_resource_context)
        if persisted is not None:
            persisted_metadata["resource_context"] = merged_resource_context
            await SessionManager.update(
                session_id,
                **session_owner,
                metadata=persisted_metadata,
            )

    if persisted is None:
        raise HTTPException(
            409,
            {
                "code": "session_not_durable",
                "message": "Session must be persisted before accepting Guidance v2",
            },
        )
    result = await _guide_chat_v2(
        payload=payload,
        message=message,
        persisted=persisted,
        runtime=runtime,
        session_owner=session_owner,
    )
    return JSONResponse(
        result,
        status_code=200 if result.get("status") == "duplicate_request" else 202,
    )


@router.get("/api/chat/guidance/{session_id}")
async def chat_guidance_queue(session_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    persisted = await SessionManager.get(
        session_id,
        **_session_owner_kwargs(workspace_context),
    )
    if persisted is None:
        raise HTTPException(404, "Session not found")
    await _migrate_session_guidance_v2(persisted)
    owner_scope = OwnerScope(
        tenant_id=str(getattr(persisted, "tenant_id", "") or ""),
        user_id=str(getattr(persisted, "user_id", "") or ""),
        workspace_id=str(getattr(persisted, "workspace_id", "") or ""),
    )
    guidance_workdir = _guidance_store_workdir()
    items = await asyncio.to_thread(
        GuidanceSQLiteStore.list_for_session,
        session_id=session_id,
        owner=owner_scope,
        workdir=guidance_workdir,
        after_sequence=0,
        limit=500,
    )
    counts = await asyncio.to_thread(
        GuidanceSQLiteStore.pending_counts,
        session_id=session_id,
        owner=owner_scope,
        workdir=guidance_workdir,
    )
    pending_total = int(counts.get("pending_steer_count", 0)) + int(
        counts.get("pending_queue_count", 0)
    )
    return {
        "status": "ok",
        "session_id": session_id,
        "items": [item.public_projection() for item in items],
        **_chat_runtime_status_payload(session_id),
        "pending_guidance_count": pending_total,
        "pending_guidance_interrupt": bool(counts.get("pending_steer_count", 0)),
        "guidance_worker_active": pending_total > 0,
        **counts,
    }


@router.get("/api/admin/runtime-capacity")
async def runtime_capacity_status(request: Request):
    """返回不含消息内容的执行积压、拒绝和遥测反压指标。"""

    user = require_current_user(request)
    if auth_required() and str(user.role or "").lower() not in {"owner", "admin"}:
        raise HTTPException(403, "Admin or owner role required")
    return {
        "status": "ok",
        "execution": ExecutionCapacityController.current().snapshot(),
        "guidance": await asyncio.to_thread(GuidanceSQLiteStore.queue_snapshot, workdir=_guidance_store_workdir()),
        "telemetry": TelemetryGateway.snapshot(),
    }


@router.delete("/api/chat/guidance/{session_id}/{guidance_id}")
async def delete_chat_guidance(
    session_id: str,
    guidance_id: str,
    request: Request,
):
    normalized_guidance_id = str(guidance_id or "").strip()
    if not normalized_guidance_id:
        raise HTTPException(400, "guidance_id is required")

    workspace_context = _workspace_context_from_request(request)
    persisted = await SessionManager.get(
        session_id,
        **_session_owner_kwargs(workspace_context),
    )
    if persisted is None:
        raise HTTPException(404, "Session not found")
    await _migrate_session_guidance_v2(persisted)
    owner_scope = OwnerScope(
        tenant_id=str(getattr(persisted, "tenant_id", "") or ""),
        user_id=str(getattr(persisted, "user_id", "") or ""),
        workspace_id=str(getattr(persisted, "workspace_id", "") or ""),
    )
    guidance_workdir = _guidance_store_workdir()
    try:
        cancelled = await asyncio.to_thread(
            GuidanceSQLiteStore.cancel_pending,
            guidance_id=normalized_guidance_id,
            expected_version=None,
            session_id=session_id,
            owner=owner_scope,
            workdir=guidance_workdir,
        )
    except GuidanceV2NotFound as exc:
        raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from exc
    except GuidanceV2NotCancellable as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
    counts = await asyncio.to_thread(
        GuidanceSQLiteStore.pending_counts,
        session_id=session_id,
        owner=owner_scope,
        workdir=guidance_workdir,
    )
    items = await asyncio.to_thread(
        GuidanceSQLiteStore.list_for_session,
        session_id=session_id,
        owner=owner_scope,
        workdir=guidance_workdir,
        after_sequence=0,
        limit=500,
    )
    pending_total = int(counts.get("pending_steer_count", 0)) + int(
        counts.get("pending_queue_count", 0)
    )
    return {
        "status": "cancelled",
        "session_id": session_id,
        "guidance_id": cancelled.id,
        "version": cancelled.version,
        "items": [item.public_projection() for item in items],
        **_chat_runtime_status_payload(session_id),
        "pending_guidance_count": pending_total,
        "pending_guidance_interrupt": bool(counts.get("pending_steer_count", 0)),
        "guidance_worker_active": pending_total > 0,
        **counts,
    }


async def _require_interaction_session(request: Request, session_id: str) -> Any:
    """Authorize the owner before resolving a process-wide pending interaction."""
    context = _workspace_context_from_request(request)
    persisted = await SessionManager.get(session_id, **_session_owner_kwargs(context))
    runtime = ServerState.sessions.get(session_id)
    if persisted is None and not (
        isinstance(runtime, dict) and _runtime_session_matches_context(runtime, context)
    ):
        raise HTTPException(404, "Session not found")
    return context


@router.post("/api/chat/permission")
async def reply_permission(payload: PermissionReplyRequest, request: Request):
    context = await _require_interaction_session(request, payload.session_id)
    response = payload.response.strip().lower()
    if response not in {"once", "always", "reject", "allow"}:
        raise HTTPException(400, "Invalid permission response")

    ok = ServerState.resolve_permission_future(
        payload.session_id, payload.request_id, response
    )
    if not ok:
        owner_key = f"{context.tenant_id}:{context.user_id}"
        interaction = agui_api.interaction_store.get(
            payload.request_id, owner_key=owner_key
        )
        if interaction is None or interaction.get("kind") != "permission":
            raise HTTPException(404, "Permission request not found or expired")
        options = interaction.get("request", {}).get("options", [])
        if not options:
            try:
                agui_api.interaction_store.resolve(
                    payload.request_id,
                    owner_key=owner_key,
                    resolution={"decision": "deny" if response == "reject" else "allow"},
                )
            except (InteractionNotFoundError, InteractionConflictError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "status": "ok",
                "session_id": payload.session_id,
                "request_id": payload.request_id,
            }
        desired_kinds = {
            "once": ("allow_once", "allow"),
            "allow": ("allow_once", "allow"),
            "always": ("allow_always", "allow"),
            "reject": ("reject_once", "reject_always", "reject"),
        }[response]
        option_id = ""
        for desired in desired_kinds:
            match = next(
                (
                    item
                    for item in options
                    if isinstance(item, dict)
                    and (
                        str(item.get("kind") or "").lower() == desired
                        or str(item.get("optionId") or "").lower() == desired
                    )
                ),
                None,
            )
            if match is not None:
                option_id = str(match.get("optionId") or "")
                break
        if not option_id:
            raise HTTPException(409, "Harness did not offer a compatible permission option")
        try:
            agui_api.interaction_store.resolve(
                payload.request_id,
                owner_key=owner_key,
                resolution={"optionId": option_id},
            )
        except (InteractionNotFoundError, InteractionConflictError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return {
        "status": "ok",
        "session_id": payload.session_id,
        "request_id": payload.request_id,
    }


@router.post("/api/chat/question")
async def reply_question(payload: QuestionReplyRequest, request: Request):
    context = await _require_interaction_session(request, payload.session_id)
    if payload.answers is not None or ServerState.pending_question_requests(payload.session_id):
        try:
            resolved = ServerState.resolve_question_group(
                payload.session_id,
                payload.answers if payload.answers is not None else {payload.request_id: payload.answer},
            )
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {'status': 'ok', 'session_id': payload.session_id, 'request_ids': resolved}
    ok = ServerState.resolve_question_future(
        payload.session_id, payload.request_id, payload.answer
    )
    if not ok:
        try:
            agui_api.interaction_store.resolve(
                payload.request_id,
                owner_key=f"{context.tenant_id}:{context.user_id}",
                resolution={"answer": payload.answer},
            )
        except InteractionNotFoundError as exc:
            raise HTTPException(404, "Question request not found or expired") from exc
        except InteractionConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    return {
        "status": "ok",
        "session_id": payload.session_id,
        "request_id": payload.request_id,
    }
