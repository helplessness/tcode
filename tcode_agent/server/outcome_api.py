"""Outcome confirmation and requested repair handling.

Routes retain their original paths and are registered by app.py.
"""

import hashlib
import sqlite3
from dataclasses import fields
from datetime import datetime, timezone
from typing import Any, Optional, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.concurrency import run_in_threadpool

from .auth import require_current_user, result_verification_settings
from ..agui.auth import APIKeyPrincipal

from ..guard.outcome_guard import (
    OutcomeValidationResult,
    apply_user_outcome_confirmation,
    public_outcome_projection,
)
from ..guard.verification_display import delivery_message_verification
from ..session.events import SessionEventStore
from ..session.manager import SessionManager
from ..task.outcome_contract import OutcomeContract
from ..types import Message
from ..runtime.verification import effective_verification_settings, verification_usage_summary
from ..workflow import WorkflowRunManager
from ._shared import (
    _chat_runtime_status_payload,
    _ensure_session_guidance_drain,
    _persist_session_guidance_queue,
    _public_outcome_validation,
)
from .workspace_scope import (
    _resolve_session_workdir,
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _session_owner_kwargs_from_session,
    _workspace_context_from_request,
)
from .chat_control_api import _guide_chat_v2
from .guidance_runtime import guidance_v2_enabled
from .schemas import ChatGuideRequest, OutcomeConfirmationRequest
from .state import ServerState

router = APIRouter()


class ResultVerificationPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    mode: Literal["light", "standard", "strict"] | None = None


@router.get("/api/config/result-verification")
async def get_result_verification_preference(request: Request):
    user = require_current_user(request)
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "personal_settings_require_user_session")
    settings = await run_in_threadpool(result_verification_settings, user.tenant_id, user.user_id)
    return {**settings, "effective": effective_verification_settings(settings)}


@router.put("/api/config/result-verification")
async def save_result_verification_preference(body: ResultVerificationPreference, request: Request):
    user = require_current_user(request)
    if isinstance(getattr(request.state, "agui_api_key", None), APIKeyPrincipal):
        raise HTTPException(403, "personal_settings_require_user_session")
    settings = await run_in_threadpool(result_verification_settings, user.tenant_id, user.user_id,
                                      enabled=body.enabled, mode=body.mode)
    return {**settings, "effective": effective_verification_settings(settings)}


@router.get("/api/sessions/{session_id}/verification-usage")
async def get_verification_usage(session_id: str, request: Request):
    context = _workspace_context_from_request(request)
    session = await SessionManager.get(session_id, **_session_owner_kwargs(context))
    if session is None:
        raise HTTPException(404, "Session not found")
    try:
        return await run_in_threadpool(verification_usage_summary, session)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "verification_usage_unavailable") from exc


def _outcome_confirmation_turn_snapshot(
    session: Any,
    message_id: str,
) -> dict[str, Any]:
    """Find the durable terminal Turn that produced one assistant result."""

    normalized_message_id = str(message_id or "").strip()
    metadata = dict(getattr(session, "metadata", {}) or {})
    latest = metadata.get("last_turn")
    if (
        isinstance(latest, dict)
        and str(latest.get("final_message_id") or "").strip()
        == normalized_message_id
    ):
        return dict(latest)

    try:
        events = SessionEventStore.list(str(getattr(session, "id", "") or ""))
    except Exception:
        events = []
    for event in reversed(events):
        if str(event.get("event_type") or "") != "turn.state":
            continue
        event_metadata = dict(event.get("metadata") or {})
        if not bool(event_metadata.get("terminal")):
            continue
        payload = dict(event.get("payload") or {})
        if str(payload.get("final_message_id") or "").strip() == normalized_message_id:
            return payload
    return {}


def _outcome_validation_result(value: Any) -> OutcomeValidationResult:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    allowed = {item.name for item in fields(OutcomeValidationResult)}
    values = {key: payload[key] for key in allowed if key in payload}
    if not str(values.get("verdict") or "").strip():
        raise ValueError("Outcome validation snapshot is incomplete")
    return OutcomeValidationResult(**values)


def _outcome_repair_guidance_message(feedback: str) -> str:
    normalized = str(feedback or "").strip()
    if normalized:
        return (
            f"对上一结果的验收意见：{normalized}\n\n"
            "请基于原验收标准继续修改，并重新完成验证。"
        )
    return (
        "用户未填写具体修改要求。请自动判断最小修复路径，补齐缺失条件后"
        "局部重跑或重新验证，直到满足原验收标准。"
    )


async def _resolve_advisory_delivery(payload, persisted, target, workspace_context):
    """Accept a delivery, never attest evidence or approve a Workflow stage."""
    from ..runtime.task_runtime import TaskRuntimeStore

    session_id, message_id = persisted.id, target.id
    owner = _session_owner_kwargs_from_session(persisted)
    verification = dict(target.verification or {})
    state = verification.get("delivery_state")
    if verification.get("delivery_request_id") == payload.client_request_id and state:
        return {"status": "duplicate_request", "confirmation_state": state,
                "verification": public_outcome_projection(delivery_message_verification(target, persisted.metadata)), **_chat_runtime_status_payload(session_id)}
    messages = list(persisted.messages)
    index = next(i for i, message in enumerate(messages) if message.id == message_id)
    if state == "accepted" or verification.get("superseded") or any(
        (message.role == "user" and message.presentation != "outcome_confirmation")
        or (message.role == "assistant"
            and message.presentation not in {"outcome_confirmation", "async_completion", "commentary", "workflow_narration"}
            and (not message.tool_calls or message.verification))
        for message in messages[index + 1:]
    ):
        raise HTTPException(409, {"code": "outcome_no_longer_current", "message": "请处理最新的回答；这份结果已接受或已有后续对话。"})
    runtime = _chat_runtime_status_payload(session_id)
    summary = TaskRuntimeStore.summary_for_session(session_id, workdir=persisted.workdir,
                                                     session_metadata=persisted.metadata)
    turn_running = runtime.get("direct_running", runtime.get("running", False)) or runtime.get("harness_running")
    counts = summary.get("active_counts") or {}
    if ServerState.is_running(session_id) or turn_running or runtime.get("question_request") or runtime.get("pending_guidance_count") or any(counts.get(key, 0) for key in ("waiting_user", "waiting_review")):
        raise HTTPException(409, {"code": "session_busy", "message": "当前回答还在生成，或有问题、人工审核需要处理，请先处理后再接受结果。"})
    runs = WorkflowRunManager.list_runs(session_id=session_id, workdir=persisted.workdir)
    if any(run.status in {"review_pending", "waiting_user"} for run in runs):
        raise HTTPException(409, {"code": "workflow_review_pending", "message": "这项任务还有单独的审核或执行步骤，请先处理，不能在这里直接完成。"})

    guidance = {}
    if payload.decision == "modify":
        feedback = str(payload.feedback or "").strip()
        if not feedback:
            raise HTTPException(400, {"code": "feedback_required", "message": "请简单写下希望修改的内容。"})
        snapshot = _outcome_confirmation_turn_snapshot(persisted, message_id)
        contract = dict(dict(snapshot.get("extra") or {}).get("outcome_contract") or {}) or None
        repair_message = "请修改上一份回答，保留已有可用结果，不重复已经成功的操作。用户的修改要求：\n" + feedback
        if guidance_v2_enabled():
            result = await _guide_chat_v2(
                payload=ChatGuideRequest(session_id=session_id, message=repair_message, mode="queue",
                    client_request_id=f"delivery-repair:{payload.client_request_id}",
                    descendant_policy="preserve", outcome_contract=contract),
                message=repair_message, persisted=persisted, runtime=ServerState.sessions.get(session_id),
                session_owner=owner, session_workdir=persisted.workdir)
            guidance = {"message": repair_message, "guidance_id": result.get("guidance_id"),
                        "mode": "queue", "guidance_status": result.get("guidance_status")}
        else:
            item = ServerState.enqueue_guidance(session_id, message=repair_message,
                outcome_contract=contract, interrupt=False, source="outcome_confirmation")
            await _persist_session_guidance_queue(session_id, owner_kwargs=owner)
            _ensure_session_guidance_drain(session_id)
            guidance = {"message": repair_message, "guidance_id": item.get("id"), "mode": "queue"}

    decided_at = datetime.now(timezone.utc).timestamp()
    state = "accepted" if payload.decision == "accept" else "modification_requested"
    # Re-read after queue persistence; do not erase new guidance metadata.
    persisted = await SessionManager.get(session_id, **owner) or persisted
    messages = list(persisted.messages)
    target = next(message for message in messages if message.id == message_id)
    target.verification = {**dict(target.verification or {}), "delivery_state": state,
                          "delivery_request_id": payload.client_request_id}
    metadata = dict(persisted.metadata or {})
    metadata["delivery_acceptance"] = {"message_id": message_id, "state": state, "decided_at": decided_at}
    await SessionManager.update(session_id, **owner, messages=messages, metadata=metadata,
        status="completed" if state == "accepted" else "active",
        related_events=[{"event_type": "outcome.delivery_decision", "payload": metadata["delivery_acceptance"],
                         "idempotency_key": f"delivery:{payload.client_request_id}"}])
    active_agent = ServerState.agents.get(session_id)
    if active_agent is not None:
        active_agent.messages = list(messages)
    return {"status": state, "session_id": session_id, "message_id": message_id,
            "decision": payload.decision, "confirmation_state": state,
            "verification": public_outcome_projection(delivery_message_verification(target, metadata)), "repair_guidance": guidance or None,
            "task_runtime": TaskRuntimeStore.summary_for_session(session_id, workdir=persisted.workdir,
                session_metadata=metadata),
            **_chat_runtime_status_payload(session_id)}


@router.post("/api/chat/outcome-confirmation")
async def confirm_outcome_result(
    payload: OutcomeConfirmationRequest,
    request: Request,
):
    """Resolve confirmation attached to an already-delivered outcome message."""

    session_id = str(payload.session_id or "").strip()
    message_id = str(payload.message_id or "").strip()
    client_request_id = str(payload.client_request_id or "").strip()
    feedback = str(payload.feedback or "").strip()
    if not session_id or not message_id or not client_request_id:
        raise HTTPException(400, "session_id, message_id and client_request_id are required")
    if len(feedback) > 4000:
        raise HTTPException(413, "Outcome confirmation feedback is too long")

    workspace_context = _workspace_context_from_request(request)
    request_owner = _session_owner_kwargs(workspace_context)
    lock = ServerState.get_session_lock(session_id)
    async with lock:
        persisted = await SessionManager.get(session_id, **request_owner)
        if persisted is None:
            raise HTTPException(404, "Session not found")
        session_owner = _session_owner_kwargs_from_session(persisted)
        messages = list(getattr(persisted, "messages", []) or [])
        target_index = next(
            (
                index
                for index, item in enumerate(messages)
                if str(getattr(item, "id", "") or "") == message_id
            ),
            -1,
        )
        if target_index < 0 or str(getattr(messages[target_index], "role", "")) != "assistant":
            raise HTTPException(404, "Outcome result message not found")

        target = messages[target_index]
        current_verification = dict(getattr(target, "verification", {}) or {})
        if current_verification.get("advisory"):
            return await _resolve_advisory_delivery(payload, persisted, target, workspace_context)
        current_state = str(
            current_verification.get("confirmation_state") or ""
        ).strip().lower()
        previous_request_id = str(
            current_verification.get("confirmation_request_id") or ""
        ).strip()
        if previous_request_id == client_request_id and current_state:
            return {
                "status": "duplicate_request",
                "session_id": session_id,
                "message_id": message_id,
                "decision": str(payload.decision),
                "confirmation_state": current_state,
                "verification": _public_outcome_validation(current_verification),
                "repair_guidance": dict(
                    current_verification.get("confirmation_guidance") or {}
                )
                or None,
                **_chat_runtime_status_payload(session_id),
            }

        if bool(current_verification.get("superseded")):
            raise HTTPException(
                409,
                {
                    "code": "outcome_contract_superseded",
                    "message": "该结果对应的验收标准已经被新目标替换，请重新验证最新结果。",
                },
            )
        requested_repair_limit = max(1, min(int(payload.max_repair_attempts or 3), 5))
        if current_state == "accepted" or (
            current_state == "repair_exhausted"
            and (
                payload.decision != "modify"
                or requested_repair_limit <= int(current_verification.get("repair_attempt") or 0)
            )
        ):
            raise HTTPException(
                409,
                {
                    "code": "outcome_confirmation_already_resolved",
                    "message": "该结果已经完成验收处理。",
                },
            )
        if current_state == "objective_failure" and payload.decision == "accept":
            raise HTTPException(
                409,
                {
                    "code": "outcome_objective_failure",
                    "message": "客观验证仍未通过，不能通过用户确认覆盖；请选择要求修改。",
                },
            )
        if (
            not bool(current_verification.get("needs_user_confirmation"))
            and current_state not in {"objective_failure", "repair_exhausted"}
        ):
            raise HTTPException(
                409,
                {
                    "code": "outcome_confirmation_not_pending",
                    "message": "该结果当前不需要补充确认。",
                },
            )
        if ServerState.is_running(session_id):
            raise HTTPException(
                409,
                {
                    "code": "session_busy",
                    "message": "当前任务仍在运行，请等待本轮结束后再处理该结果。",
                },
            )

        snapshot = _outcome_confirmation_turn_snapshot(persisted, message_id)
        extra = dict(snapshot.get("extra") or {})
        raw_validation = dict(extra.get("outcome_validation") or {})
        raw_contract = dict(extra.get("outcome_contract") or {})
        if not raw_validation or not raw_contract:
            raise HTTPException(
                409,
                {
                    "code": "outcome_confirmation_snapshot_missing",
                    "message": "没有找到该结果对应的完整验收记录，无法安全补确认。",
                },
            )

        try:
            contract = OutcomeContract.compile(
                str(raw_contract.get("goal") or ""),
                supplied=raw_contract,
            )
            validation = _outcome_validation_result(raw_validation)
        except Exception as exc:
            raise HTTPException(
                409,
                {
                    "code": "outcome_confirmation_snapshot_invalid",
                    "message": "该结果的验收记录无法恢复，请重新执行验证。",
                },
            ) from exc

        expected_contract_id = str(current_verification.get("contract_id") or "")
        try:
            expected_contract_version = int(
                current_verification.get("contract_version") or 0
            )
        except (TypeError, ValueError):
            expected_contract_version = 0
        if (
            not expected_contract_id
            or expected_contract_id != contract.contract_id
            or expected_contract_version != contract.version
        ):
            raise HTTPException(
                409,
                {
                    "code": "outcome_contract_version_mismatch",
                    "message": "结果与验收标准版本不一致，请重新验证最新结果。",
                },
            )

        repair_guidance: dict[str, Any] = {}
        repair_attempt = 0
        timeline_message: Optional[Message] = None
        if payload.decision == "accept":
            from ..runtime.workspace_registry import registry_workspace

            # Receipts belong to the validating session's workspace, not the
            # HTTP middleware's default workspace. Never re-attest them here.
            with registry_workspace(persisted.workdir):
                from ..guard.verifier_registry import VerifierRegistry

                for criterion in validation.criterion_results:
                    for receipt in criterion.get("verifier_results") or []:
                        if not isinstance(receipt, dict) or not receipt.get("registry_verified"):
                            continue
                        if not VerifierRegistry.is_authentic_result(
                            receipt, contract_id=contract.contract_id,
                            contract_version=contract.version,
                            criterion_id=str(criterion.get("criterion_id") or ""),
                            input_digest=str(receipt.get("input_digest") or ""),
                        ):
                            # A restart/unload invalidates runtime HMAC receipts.
                            # Preserve the pending result instead of persisting a
                            # misleading scientific/objective failure or score drop.
                            raise HTTPException(409, {
                                "code": "outcome_verification_receipt_stale",
                                "message": "验证凭据已失效（可能由于服务重启或插件重载），请先重新验证最终报告；无需重跑 Workflow。",
                            })
                validation = apply_user_outcome_confirmation(
                    validation, contract, accepted=True,
                )
            confirmation_state = (
                "accepted"
                if validation.verification_status == "VERIFIED"
                else "objective_failure"
            )
            if confirmation_state == "accepted":
                timeline_message = Message(
                    role="user",
                    content="已确认接受上一结果。",
                    id=f"outcome-confirmation-{client_request_id}",
                    presentation="outcome_confirmation",
                    metadata={
                        "outcome_confirmation": {
                            "target_message_id": message_id,
                            "decision": "accept",
                        }
                    },
                )
        else:
            validation = apply_user_outcome_confirmation(
                validation,
                contract,
                accepted=False,
            )
            prior_confirmations = list(
                dict(getattr(persisted, "metadata", {}) or {}).get("outcome_confirmations") or []
            )
            repair_attempt = 1
            for item in prior_confirmations:
                if not isinstance(item, dict):
                    continue
                try:
                    item_version = int(item.get("contract_version") or 0)
                except (TypeError, ValueError):
                    item_version = 0
                if (
                    str(item.get("contract_id") or "") == contract.contract_id
                    and item_version == contract.version
                    and str(item.get("decision") or "") == "modify"
                ):
                    repair_attempt += 1
            if feedback:
                validation.repair_instructions.append(
                    "User feedback: " + feedback[:1000]
                )
            extending_exhausted_limit = (
                current_state == "repair_exhausted"
                and requested_repair_limit > int(current_verification.get("repair_attempt") or 0)
            )
            if repair_attempt >= requested_repair_limit and not extending_exhausted_limit:
                confirmation_state = "repair_exhausted"
            else:
                repair_message = _outcome_repair_guidance_message(feedback)
                repair_request_id = f"outcome-repair:{client_request_id}"
                runtime = ServerState.sessions.get(session_id)
                if not _runtime_session_matches_context(runtime, workspace_context):
                    runtime = None
                session_workdir = str(
                    getattr(persisted, "workdir", "")
                    or await _resolve_session_workdir(session_id, workspace_context)
                ).strip()
                if guidance_v2_enabled():
                    guidance_result = await _guide_chat_v2(
                        payload=ChatGuideRequest(
                            session_id=session_id,
                            message=repair_message,
                            mode="queue",
                            client_request_id=repair_request_id,
                            descendant_policy="preserve",
                            outcome_contract=raw_contract,
                        ),
                        message=repair_message,
                        persisted=persisted,
                        runtime=runtime,
                        session_owner=session_owner,
                        session_workdir=session_workdir,
                    )
                    repair_guidance = {
                        "message": repair_message,
                        "guidance_id": guidance_result.get("guidance_id"),
                        "sequence": guidance_result.get("sequence"),
                        "mode": guidance_result.get("accepted_mode") or "queue",
                        "target_turn_id": guidance_result.get("target_turn_id"),
                        "guidance_status": guidance_result.get("guidance_status"),
                    }
                else:
                    item = ServerState.enqueue_guidance(
                        session_id,
                        message=repair_message,
                        outcome_contract=raw_contract,
                        interrupt=False,
                        source="outcome_confirmation",
                    )
                    await _persist_session_guidance_queue(
                        session_id,
                        owner_kwargs=session_owner,
                    )
                    _ensure_session_guidance_drain(session_id)
                    repair_guidance = {
                        "message": repair_message,
                        "guidance_id": item.get("id"),
                        "mode": "queue",
                        "guidance_status": "pending",
                    }
                confirmation_state = "modification_requested"

        decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated_verification = public_outcome_projection(validation.to_dict())
        updated_verification.update(
            {
                "contract_id": contract.contract_id,
                "contract_version": contract.version,
                "confirmation_state": confirmation_state,
                "confirmation_request_id": client_request_id,
                "confirmation_decided_at": decided_at,
            }
        )
        if repair_guidance:
            updated_verification["confirmation_guidance"] = dict(repair_guidance)
        if repair_attempt:
            updated_verification["repair_attempt"] = min(repair_attempt, requested_repair_limit)
            updated_verification["max_repair_attempts"] = requested_repair_limit

        # _guide_chat_v2 may have refreshed the persisted projection. Re-read it
        # before committing the message decision so no queue metadata is lost.
        persisted = await SessionManager.get(session_id, **session_owner) or persisted
        messages = list(getattr(persisted, "messages", []) or [])
        target_index = next(
            (
                index
                for index, item in enumerate(messages)
                if str(getattr(item, "id", "") or "") == message_id
            ),
            -1,
        )
        if target_index < 0:
            raise HTTPException(409, "Outcome result changed while confirming")
        messages[target_index].verification = updated_verification
        if timeline_message is not None and not any(
            str(getattr(item, "id", "") or "") == timeline_message.id
            for item in messages
        ):
            messages.append(timeline_message)

        persisted_metadata = dict(getattr(persisted, "metadata", {}) or {})
        last_turn = dict(persisted_metadata.get("last_turn") or {})
        if str(last_turn.get("final_message_id") or "") == message_id:
            last_extra = dict(last_turn.get("extra") or {})
            last_extra["outcome_validation"] = validation.to_dict()
            last_turn["extra"] = last_extra
            persisted_metadata["last_turn"] = last_turn
        history = list(persisted_metadata.get("outcome_confirmations") or [])
        history.append(
            {
                "message_id": message_id,
                "contract_id": contract.contract_id,
                "contract_version": contract.version,
                "decision": str(payload.decision),
                "confirmation_state": confirmation_state,
                "client_request_id": client_request_id,
                "feedback_digest": (
                    hashlib.sha256(feedback.encode("utf-8")).hexdigest()
                    if feedback
                    else ""
                ),
                "decided_at": decided_at,
                "guidance_id": str(repair_guidance.get("guidance_id") or ""),
                "repair_attempt": min(repair_attempt, requested_repair_limit) if repair_attempt else 0,
                "max_repair_attempts": requested_repair_limit,
            }
        )
        persisted_metadata["outcome_confirmations"] = history[-100:]
        await SessionManager.update(
            session_id,
            **session_owner,
            messages=messages,
            metadata=persisted_metadata,
            related_events=[
                {
                    "event_type": "outcome.user_confirmation",
                    "payload": {
                        "message_id": message_id,
                        "contract_id": contract.contract_id,
                        "contract_version": contract.version,
                        "decision": str(payload.decision),
                        "confirmation_state": confirmation_state,
                        "verification": public_outcome_projection(updated_verification),
                        "guidance_id": str(repair_guidance.get("guidance_id") or ""),
                    },
                    "idempotency_key": f"outcome-confirmation:{client_request_id}",
                }
            ],
        )

        workflow_run_id = str(
            dict(getattr(target, "metadata", {}) or {}).get("workflow_run_id") or ""
        ).strip()
        if not workflow_run_id and confirmation_state == "accepted":
            # A revision response is a new assistant message.  It may no longer
            # carry the workflow id that was attached to the original delivery,
            # even though the user's repair guidance belongs to that run.  Bind
            # acceptance to the nearest earlier workflow result that explicitly
            # requested revision so an accepted repair can unlock template save.
            for prior in reversed(messages[:target_index]):
                prior_metadata = dict(getattr(prior, "metadata", {}) or {})
                candidate_run_id = str(prior_metadata.get("workflow_run_id") or "").strip()
                prior_verification = dict(getattr(prior, "verification", {}) or {})
                if candidate_run_id and str(prior_verification.get("confirmation_state") or "") == "modification_requested":
                    workflow_run_id = candidate_run_id
                    break
        if workflow_run_id:
            try:
                workflow_workdir = await _resolve_session_workdir(session_id, workspace_context)
                WorkflowRunManager.set_delivery_acceptance(
                    workflow_run_id,
                    acceptance=("accepted" if confirmation_state == "accepted" else "revision_requested"),
                    actor=str(workspace_context.user_id or "user"),
                    feedback=feedback,
                    workdir=workflow_workdir,
                )
            except Exception:
                # Session acceptance remains authoritative; a stale/missing workflow
                # record must not turn a successful user decision into an API error.
                pass

        active_agent = ServerState.agents.get(session_id)
        if active_agent is not None:
            active_agent.messages = list(messages)
            last_contract = dict(
                getattr(active_agent, "_last_outcome_contract", {}) or {}
            )
            try:
                last_contract_version = int(last_contract.get("version") or 0)
            except (TypeError, ValueError):
                last_contract_version = 0
            if (
                str(last_contract.get("contract_id") or "") == contract.contract_id
                and last_contract_version == contract.version
            ):
                active_agent._last_outcome_validation = validation.to_dict()

        return {
            "status": "accepted" if confirmation_state == "accepted" else confirmation_state,
            "session_id": session_id,
            "message_id": message_id,
            "decision": str(payload.decision),
            "confirmation_state": confirmation_state,
            "verification": _public_outcome_validation(updated_verification),
            "repair_guidance": repair_guidance or None,
            "timeline_message": (
                {
                    "id": timeline_message.id,
                    "role": timeline_message.role,
                    "content": timeline_message.content,
                    "presentation": timeline_message.presentation,
                }
                if timeline_message is not None
                else None
            ),
            **_chat_runtime_status_payload(session_id),
        }
