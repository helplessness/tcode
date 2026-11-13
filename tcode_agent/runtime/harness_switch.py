"""Server-authoritative Harness switching for an existing TCode session."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Callable

from ..external_harness.handoff_store import (
    HarnessHandoffStore,
    switch_request_digest,
)
from ..session.manager import Session, SessionManager
from .harness_context import (
    HarnessHandoffContext,
    collect_key_result_candidates,
    select_recent_handoff_rounds,
    source_digest,
)
from .harness_handoff_summary import HarnessHandoffSummarizer
from .model_policy import ModelPolicyError, resolve_runtime_model


logger = logging.getLogger(__name__)
_RUNTIME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
HANDOFF_NOTICE = (
    "已切换 Harness。系统仅继承最近 8 轮文本和一份简短交接摘要；"
    "Harness 私有记忆、完整研究状态及未完成工具链不会迁移。"
    "研究中途切换可能丢失记忆并显著降低任务效果。"
)


class HarnessSwitchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.state = dict(state or {})
        super().__init__(message)


class HarnessSwitchService:
    def __init__(
        self,
        *,
        store: HarnessHandoffStore,
        summarizer: HarnessHandoffSummarizer,
        runtime_registry: Any,
        backend_registry: Any,
        run_coordinator: Any,
        native_is_active: Callable[[str], bool] | None = None,
        guidance_is_pending: Callable[[Session], bool] | None = None,
    ) -> None:
        self.store = store
        self.summarizer = summarizer
        self.runtime_registry = runtime_registry
        self.backend_registry = backend_registry
        self.run_coordinator = run_coordinator
        self.native_is_active = native_is_active or (lambda _session_id: False)
        self.guidance_is_pending = guidance_is_pending or (lambda _session: False)

    @staticmethod
    def _telemetry(
        event_name: str,
        payload: dict[str, Any],
        *,
        session: Session,
        request_id: str,
    ) -> None:
        try:
            from .telemetry import TelemetryGateway

            TelemetryGateway.emit(
                event_name,
                payload,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                session_id=session.id,
                request_id=request_id,
            )
        except Exception:
            logger.debug("Harness telemetry emit failed", exc_info=True)

    async def _mirror_session_projection(
        self,
        *,
        session: Session,
        workspace_id: str,
        state: dict[str, Any],
        model_id: str,
        last_active_at: float | None = None,
    ) -> None:
        """Best-effort cache projection; SQLite remains the authoritative state."""

        metadata = dict(session.metadata or {})
        metadata["harness"] = self.store.projection(state)
        update: dict[str, Any] = {
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "workspace_id": workspace_id,
            "metadata": metadata,
            "model": model_id,
        }
        if last_active_at is not None:
            update["last_active_at"] = last_active_at
        try:
            await SessionManager.update(session.id, **update)
        except Exception:
            # Reads overlay the SQLite projection, so a transient Session cache
            # failure must not turn an already committed switch into a false 500.
            logger.exception(
                "harness_session_projection_failed session_id=%s switch_version=%s",
                session.id,
                state.get("switch_version"),
            )

    async def _resolve_model(
        self,
        *,
        runtime_id: str,
        model_id: str,
        workdir: str,
        public_server: bool,
    ) -> str:
        if not _RUNTIME_ID.fullmatch(runtime_id):
            raise HarnessSwitchError(
                "invalid_runtime_transition",
                "invalid runtime id",
                status_code=400,
            )
        backend = self.backend_registry.get(runtime_id)
        definition = self.runtime_registry.get(runtime_id)
        if backend is None and runtime_id != "tcode-native":
            raise HarnessSwitchError(
                "session_or_runtime_not_found",
                "Harness runtime was not found",
                status_code=404,
            )
        if definition is not None:
            allowed, reason = self.runtime_registry.allowed_for_request(
                runtime_id, public_server=public_server
            )
            if not allowed:
                raise HarnessSwitchError(
                    "harness_switch_forbidden", reason, status_code=403
                )
        try:
            selection = await resolve_runtime_model(
                runtime_id,
                model_id,
                workdir=workdir,
                runtime_registry=self.runtime_registry,
                backend_registry=self.backend_registry,
            )
        except ModelPolicyError as exc:
            raise HarnessSwitchError(
                "runtime_model_invalid", str(exc), status_code=422
            ) from exc
        return selection.model_id

    async def switch(
        self,
        *,
        session: Session,
        target_runtime_id: str,
        model_id: str,
        expected_current_runtime_id: str,
        expected_switch_version: int,
        idempotency_key: str,
        public_server: bool = False,
    ) -> dict[str, Any]:
        owner_key = f"{session.tenant_id}:{session.user_id}"
        workspace_id = str(session.workspace_id or "")
        session_id = str(session.id or "")
        workdir = str(session.workdir or "")
        target_runtime_id = str(target_runtime_id or "").strip()
        expected_current_runtime_id = str(expected_current_runtime_id or "").strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise HarnessSwitchError(
                "invalid_runtime_transition",
                "a valid Idempotency-Key is required",
                status_code=400,
            )
        selected_model = await self._resolve_model(
            runtime_id=target_runtime_id,
            model_id=model_id,
            workdir=workdir,
            public_server=public_server,
        )
        request_payload = {
            "target_runtime_id": target_runtime_id,
            "model_id": selected_model,
            "expected_current_runtime_id": expected_current_runtime_id,
            "expected_switch_version": int(expected_switch_version),
        }
        digest = switch_request_digest(request_payload)
        try:
            cached = self.store.idempotent_result(
                owner_key=owner_key,
                session_id=session_id,
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "harness_switch_persist_failed"))
            raise HarnessSwitchError(
                code,
                str(exc),
                status_code=(409 if code in {
                    "harness_state_conflict",
                    "harness_switch_in_progress",
                } else 500),
            ) from exc
        if cached is not None:
            return cached

        if self.native_is_active(session_id) or self.run_coordinator.store.active_for_session(
            owner_key, session_id
        ):
            raise HarnessSwitchError(
                "session_run_active",
                "the Session is running and cannot change Harness",
                status_code=409,
            )
        if self.guidance_is_pending(session):
            raise HarnessSwitchError(
                "harness_guidance_pending",
                "pending Guidance must finish before changing Harness",
                status_code=409,
            )

        state = self.store.get_state(
            owner_key=owner_key,
            workspace_id=workspace_id,
            session_id=session_id,
            session_metadata=dict(session.metadata or {}),
        )
        operation_id = f"switch_{uuid.uuid4().hex}"
        try:
            kind, reserved_state = self.store.reserve_switch(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
                target_runtime_id=target_runtime_id,
                expected_current_runtime_id=expected_current_runtime_id,
                expected_switch_version=int(expected_switch_version),
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "harness_switch_persist_failed"))
            status_code = 409 if code in {
                "harness_state_conflict",
                "harness_switch_in_progress",
            } else 500
            raise HarnessSwitchError(
                code,
                str(exc),
                status_code=status_code,
                state=getattr(exc, "state", state),
            ) from exc

        previous_runtime = str(reserved_state.get("current_runtime_id") or "tcode-native")
        if kind == "cached":
            cached = self.store.idempotent_result(
                owner_key=owner_key,
                session_id=session_id,
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if cached is not None:
                return cached
        if kind == "noop":
            response = {
                "session_id": session_id,
                "previous_runtime_id": previous_runtime,
                "current_runtime_id": previous_runtime,
                "model_id": selected_model,
                "switch_version": int(reserved_state.get("switch_version") or 0),
                "switched": False,
                "mid_session": False,
                "handoff": None,
                "notice": None,
            }
            self.store.complete_noop(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                runtime_id=previous_runtime,
                model_id=selected_model,
                response=response,
            )
            refreshed = self.store.get_state(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
            )
            await self._mirror_session_projection(
                session=session,
                workspace_id=workspace_id,
                state=refreshed,
                model_id=selected_model,
            )
            self._telemetry(
                "harness.switch",
                {
                    "from_runtime_id": previous_runtime,
                    "to_runtime_id": previous_runtime,
                    "result": "noop",
                    "switch_version": response["switch_version"],
                },
                session=session,
                request_id=operation_id,
            )
            return response

        try:
            messages = list(session.messages or [])
            max_rounds = max(
                1,
                min(
                    int(os.environ.get("TCODE_HARNESS_HANDOFF_RECENT_ROUNDS", "8")),
                    32,
                ),
            )
            selection = select_recent_handoff_rounds(messages, max_rounds=max_rounds)
            revision = int(session.current_revision or 0)
            candidates = collect_key_result_candidates(messages)
            handoff: HarnessHandoffContext | None = None
            summary_status = ""
            warning_code = ""
            if selection.conversation_started:
                summary_result = await self.summarizer.summarize(
                    selection=selection,
                    candidates=candidates,
                    workdir=workdir,
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    request_id=operation_id,
                )
                summary_status = summary_result.status
                warning_code = summary_result.warning_code
                handoff = HarnessHandoffContext(
                    handoff_id=f"handoff_{uuid.uuid4().hex}",
                    from_runtime_id=previous_runtime,
                    to_runtime_id=target_runtime_id,
                    source_revision=revision,
                    summary=summary_result.summary,
                    recent_messages=selection.messages,
                    summary_status=summary_result.status,
                    truncated=selection.truncated,
                    warning_code=summary_result.warning_code,
                )
                logger.info(
                    "harness_handoff_summary status=%s model=%s duration_ms=%d source_chars=%d",
                    summary_result.status,
                    summary_result.model_id,
                    summary_result.duration_ms,
                    summary_result.source_chars,
                )
                self._telemetry(
                    "harness.handoff.summary",
                    {
                        "status": summary_result.status,
                        "model_id": summary_result.model_id,
                        "duration_ms": summary_result.duration_ms,
                        "source_chars": summary_result.source_chars,
                    },
                    session=session,
                    request_id=operation_id,
                )

            latest = await SessionManager.get(
                session_id,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                workspace_id=workspace_id,
            )
            if latest is None or int(latest.current_revision or 0) != revision:
                raise HarnessSwitchError(
                    "harness_state_conflict",
                    "Session changed while the handoff was being summarized",
                    status_code=409,
                    state=reserved_state,
                )

            mid_session = selection.conversation_started
            handoff_response = (
                {
                    "handoff_id": handoff.handoff_id,
                    "summary_status": summary_status,
                    "inherited_rounds": selection.inherited_rounds,
                    "truncated": selection.truncated,
                    "warning_code": warning_code,
                }
                if handoff is not None
                else None
            )
            response = self.store.commit_switch(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                from_runtime_id=previous_runtime,
                to_runtime_id=target_runtime_id,
                model_id=selected_model,
                source_revision=revision,
                source_digest=source_digest(
                    source_revision=revision,
                    messages=selection.messages,
                    candidates=candidates,
                ),
                handoff=handoff,
                response={
                    "session_id": session_id,
                    "previous_runtime_id": previous_runtime,
                    "current_runtime_id": target_runtime_id,
                    "model_id": selected_model,
                    "switch_version": int(reserved_state.get("switch_version") or 0) + 1,
                    "switched": True,
                    "mid_session": mid_session,
                    "handoff": handoff_response,
                    "notice": (
                        {
                            "code": "harness_mid_session_memory_risk",
                            "severity": "warning",
                            "message": HANDOFF_NOTICE,
                        }
                        if mid_session
                        else None
                    ),
                },
            )
            state = self.store.get_state(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
            )
            await self._mirror_session_projection(
                session=latest,
                workspace_id=workspace_id,
                state=state,
                model_id=selected_model,
                last_active_at=time.time(),
            )
            logger.info(
                "harness_handoff_created session_id=%s handoff_id=%s from_runtime_id=%s to_runtime_id=%s source_revision=%d source_digest=%s summary_status=%s inherited_rounds=%d truncated=%s",
                session_id,
                str((response.get("handoff") or {}).get("handoff_id") or ""),
                previous_runtime,
                target_runtime_id,
                revision,
                source_digest(
                    source_revision=revision,
                    messages=selection.messages,
                    candidates=candidates,
                ),
                summary_status,
                selection.inherited_rounds,
                selection.truncated,
            )
            self._telemetry(
                "harness.switch",
                {
                    "from_runtime_id": previous_runtime,
                    "to_runtime_id": target_runtime_id,
                    "result": "switched",
                    "switch_version": response["switch_version"],
                    "summary_status": summary_status,
                    "inherited_rounds": selection.inherited_rounds,
                    "truncated": selection.truncated,
                },
                session=session,
                request_id=operation_id,
            )
            return response
        except HarnessSwitchError:
            self.store.abort_switch(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
            )
            raise
        except Exception as exc:
            self.store.abort_switch(
                owner_key=owner_key,
                workspace_id=workspace_id,
                session_id=session_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
            )
            raise HarnessSwitchError(
                "harness_switch_persist_failed",
                "Harness switch could not be persisted",
                status_code=500,
                state=reserved_state,
            ) from exc


__all__ = ["HANDOFF_NOTICE", "HarnessSwitchError", "HarnessSwitchService"]
