"""REST API for server-authoritative Harness switching."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..external_harness.handoff_store import HarnessHandoffStore
from ..runtime.gateway import agent_backend_registry, run_coordinator, runtime_registry
from ..runtime.harness_handoff_summary import HarnessHandoffSummarizer
from ..runtime.harness_switch import HarnessSwitchError, HarnessSwitchService
from ..runtime.guidance import OwnerScope
from ..runtime.guidance_store import GuidanceSQLiteStore
from ..session.manager import Session, SessionManager
from ._shared import (
    ServerState,
    auth_required,
)
from .workspace_scope import (
    _session_owner_kwargs,
    _workspace_context_from_request,
)


router = APIRouter()
harness_handoff_store = HarnessHandoffStore()


def _guidance_is_pending(session: Session) -> bool:
    return GuidanceSQLiteStore.has_active_for_session(
        session_id=session.id,
        owner=OwnerScope(
            tenant_id=str(session.tenant_id or ""),
            user_id=str(session.user_id or ""),
            workspace_id=str(session.workspace_id or ""),
        ),
        workdir=ServerState.get_workdir(),
    )


harness_switch_service = HarnessSwitchService(
    store=harness_handoff_store,
    summarizer=HarnessHandoffSummarizer(),
    runtime_registry=runtime_registry,
    backend_registry=agent_backend_registry,
    run_coordinator=run_coordinator,
    native_is_active=ServerState.is_running,
    guidance_is_pending=_guidance_is_pending,
)


class HarnessSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_runtime_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(default="", max_length=300)
    expected_current_runtime_id: str = Field(min_length=1, max_length=200)
    expected_switch_version: int = Field(ge=0)


async def _owned_session(session_id: str, request: Request) -> Session:
    context = _workspace_context_from_request(request)
    session = await SessionManager.get(
        session_id,
        **_session_owner_kwargs(context),
    )
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "session_or_runtime_not_found",
                "message": "Session was not found",
            },
        )
    return session


def harness_projection_for_session(session: Session) -> dict[str, Any]:
    owner_key = f"{session.tenant_id}:{session.user_id}"
    state = harness_handoff_store.get_state(
        owner_key=owner_key,
        workspace_id=session.workspace_id,
        session_id=session.id,
        session_metadata=dict(session.metadata or {}),
    )
    return harness_handoff_store.projection(state)


@router.get("/api/sessions/{session_id}/harness-state")
async def get_harness_state(session_id: str, request: Request):
    session = await _owned_session(session_id, request)
    return {
        "session_id": session.id,
        **harness_projection_for_session(session),
    }


@router.post("/api/sessions/{session_id}/harness-switch")
async def switch_harness(
    session_id: str,
    payload: HarnessSwitchRequest,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    session = await _owned_session(session_id, request)
    try:
        return await harness_switch_service.switch(
            session=session,
            target_runtime_id=payload.target_runtime_id,
            model_id=payload.model_id,
            expected_current_runtime_id=payload.expected_current_runtime_id,
            expected_switch_version=payload.expected_switch_version,
            idempotency_key=idempotency_key,
            public_server=auth_required(),
        )
    except HarnessSwitchError as exc:
        state = dict(exc.state or {})
        projection = (
            harness_handoff_store.projection(state)
            if state and "current_runtime_id" in state
            else {}
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                "current_state": projection,
            },
        ) from exc


__all__ = [
    "HarnessSwitchRequest",
    "harness_handoff_store",
    "harness_projection_for_session",
    "harness_switch_service",
    "router",
]
