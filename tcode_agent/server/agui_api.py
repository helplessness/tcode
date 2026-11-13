"""公开 AG-UI 0.0.59 Gateway 与 Harness 管理 API。"""

from __future__ import annotations

import os
import uuid
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..agui.auth import APIKeyPrincipal, APIKeyStore, DEFAULT_SCOPES
from ..agui.input_policy import InputPolicyError, validate_public_input
from ..agui.mapper import runtime_event_to_agui
from ..agui.models import RunAgentInput
from ..agui.rate_limit import GatewayRateLimiter, RateLimitExceeded
from ..agui.sse import bounded_sse_stream
from ..runtime.agent_backend import RunContext
from ..runtime.gateway import (
    agent_backend_registry,
    interaction_store,
    run_coordinator,
    runtime_registry,
)
from ..runtime.interaction_store import (
    InteractionConflictError,
    InteractionNotFoundError,
)
from ..runtime.run_coordinator import RunConflictError, RunUnavailableError
from ..external_harness.handoff_store import HarnessHandoffStore
from ..runtime.model_policy import (
    ModelPolicyError,
    native_model_ids,
    resolve_runtime_model,
)
from ..session.manager import SessionManager
from .agent_selection import normalize_primary_agent_name
from .auth import CurrentUserContext, auth_required, require_current_user
from .state import ServerState
from .workdirs import resolve_user_workdir_path, workspace_state, WorkspaceContext


router = APIRouter(prefix="/api/ag-ui/v1", tags=["AG-UI"])
api_key_store = APIKeyStore()
rate_limiter = GatewayRateLimiter()
harness_handoff_store = HarnessHandoffStore()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CancelRunBody(StrictBody):
    thread_id: str = Field(alias="threadId", min_length=1, max_length=200)


class ResolveInteractionBody(StrictBody):
    resolution: dict[str, Any] = Field(default_factory=dict, max_length=128)


class CreateAPIKeyBody(StrictBody):
    scopes: list[str] = Field(default_factory=lambda: sorted(DEFAULT_SCOPES), max_length=32)
    workspace_ids: list[str] = Field(default_factory=list, alias="workspaceIds", max_length=128)
    runtime_ids: list[str] = Field(default_factory=list, alias="runtimeIds", max_length=128)
    ttl_seconds: int = Field(default=30 * 24 * 60 * 60, alias="ttlSeconds", ge=60, le=365 * 24 * 60 * 60)


def _enabled() -> None:
    if str(os.environ.get("TCODE_AGUI_ENABLED", "0")).lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise HTTPException(404, "AG-UI Gateway is disabled")


def _identity(request: Request) -> tuple[CurrentUserContext, APIKeyPrincipal | None]:
    user = require_current_user(request)
    principal = getattr(request.state, "agui_api_key", None)
    return user, principal if isinstance(principal, APIKeyPrincipal) else None


def _authorize(
    principal: APIKeyPrincipal | None,
    *,
    scope: str,
    workspace_id: str = "",
    runtime_id: str = "",
) -> None:
    if principal is None:
        return
    if not principal.permits(scope):
        raise HTTPException(403, f"API key is missing scope: {scope}")
    if workspace_id and principal.workspace_ids and workspace_id not in principal.workspace_ids:
        raise HTTPException(403, "API key cannot access this workspace")
    if runtime_id and principal.allowed_runtime_ids and runtime_id not in principal.allowed_runtime_ids:
        raise HTTPException(403, "API key cannot execute this runtime")


def _rate_limit(
    user: CurrentUserContext,
    principal: APIKeyPrincipal | None,
    *,
    action: str,
    env_name: str,
    default: int,
) -> None:
    identity = (
        f"key:{principal.key_id}"
        if principal is not None
        else f"user:{user.tenant_id}:{user.user_id}"
    )
    try:
        configured = int(os.environ.get(env_name, str(default)))
    except (TypeError, ValueError):
        configured = default
    try:
        rate_limiter.consume(identity, action, limit=max(1, configured))
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            "AG-UI rate limit exceeded",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


def _workspace_path(user: CurrentUserContext, workspace_id: str) -> Path:
    server_workdir = ServerState.get_workdir()
    context = WorkspaceContext(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        workspace_id=workspace_id,
    )
    state = workspace_state(server_workdir, context)
    if state is not None and str(state.get("status") or "active") != "active":
        raise HTTPException(423, "Workspace is not active")
    path = resolve_user_workdir_path(
        server_workdir,
        workspace_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    if not path.is_dir():
        raise HTTPException(404, "Workspace does not exist")
    return path


async def _ensure_session(
    run_input: RunAgentInput,
    *,
    user: CurrentUserContext,
    workdir: Path,
) -> None:
    tcode = run_input.forwarded_props.tcode
    session = await SessionManager.get(
        run_input.thread_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        workspace_id=tcode.workspace_id,
    )
    if session is None:
        models = {tcode.runtime_id: tcode.model_id} if tcode.model_id else {}
        await SessionManager.create(
            session_id=run_input.thread_id,
            title="AG-UI session",
            agent_name="build",
            workdir=workdir.as_posix(),
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            workspace_id=tcode.workspace_id,
            metadata={
                "transport": "ag-ui",
                "harness": {
                    "runtime_id": tcode.runtime_id,
                    "model_id": tcode.model_id,
                    "models": models,
                    "switch_version": 0,
                },
            },
        )
        return
    if Path(str(session.workdir or "")).resolve(strict=False) != workdir.resolve(strict=False):
        raise HTTPException(409, "Thread is bound to another workspace path")
    metadata = dict(session.metadata or {})
    if not session.parent_id and (
        session.agent_name == "general" or metadata.get("cli_requested_agent") == "general"
    ):
        # Repair the old Gateway default before the native adapter resumes the
        # thread through the CLI. Its saved selector otherwise restores general.
        agent_name = normalize_primary_agent_name(session.agent_name) or "build"
        if metadata.get("cli_requested_agent") == "general":
            metadata["cli_requested_agent"] = agent_name
        await SessionManager.update(
            session.id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            workspace_id=tcode.workspace_id,
            agent_name=agent_name,
            metadata=metadata,
        )


@router.get("/agents")
async def list_agents(request: Request):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="agents:read")
    _rate_limit(user, principal, action="read", env_name="TCODE_AGUI_READS_PER_MINUTE", default=300)
    return {
        "agents": [
            {
                "id": "default",
                "name": "TCode",
                "defaultRuntimeId": "tcode-native",
                "protocol": "AG-UI",
                "protocolVersion": "0.0.59",
            }
        ]
    }


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="agents:read")
    _rate_limit(user, principal, action="read", env_name="TCODE_AGUI_READS_PER_MINUTE", default=300)
    if agent_id != "default":
        raise HTTPException(404, "Agent not found")
    backend = agent_backend_registry.get("tcode-native")
    return {
        "id": "default",
        "name": "TCode",
        "defaultRuntimeId": "tcode-native",
        "protocol": "AG-UI",
        "protocolVersion": "0.0.59",
        "capabilities": await backend.get_capabilities() if backend else {},
    }


@router.get("/agents/{agent_id}/capabilities")
async def get_agent_capabilities(agent_id: str, request: Request):
    agent = await get_agent(agent_id, request)
    return agent["capabilities"]


@router.get("/runtimes")
async def list_runtimes(request: Request, includeModels: bool = False, workspaceId: str = ""):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="runtimes:read")
    _rate_limit(user, principal, action="read", env_name="TCODE_AGUI_READS_PER_MINUTE", default=300)
    native = agent_backend_registry.get("tcode-native")
    echo = agent_backend_registry.get("direct-echo")
    external = await runtime_registry.probes(include_models=includeModels)
    external_ids = {item.runtime_id for item in external}
    framework_backends = []
    for backend in agent_backend_registry.list():
        if backend.backend_id in {"tcode-native", "direct-echo", *external_ids}:
            continue
        try:
            capabilities = await backend.get_capabilities()
            if not isinstance(capabilities, dict):
                capabilities = {}
            backend_available = True
            backend_diagnostics: list[str] = []
        except Exception as exc:
            capabilities = {}
            backend_available = False
            backend_diagnostics = [
                f"backend capability discovery failed: {str(exc)[:300]}"
            ]
        models = capabilities.get("models") if isinstance(capabilities, dict) else []
        framework_backends.append(
            {
                "runtimeId": backend.backend_id,
                "displayName": str(
                    capabilities.get("displayName") or backend.backend_id
                ),
                "available": backend_available,
                "enabled": True,
                "release": str(capabilities.get("release") or "experimental"),
                "models": list(models) if includeModels and isinstance(models, list) else [],
                "capabilities": capabilities,
                "diagnostics": backend_diagnostics,
                "sourceType": "entrypoint",
                "sourceName": "tcode.agent_backends",
                "trustState": "trusted",
                "compatibilityState": (
                    "compatible" if backend_available else "error"
                ),
                "conformanceState": str(
                    capabilities.get("conformanceState") or "unverified"
                ),
                "capabilitiesVerified": list(
                    capabilities.get("verifiedCapabilities") or []
                ),
            }
        )
    return {
        "defaultRuntimeId": "tcode-native",
        "runtimes": [
            {
                "runtimeId": "tcode-native",
                "displayName": "TCode Native",
                "available": True,
                "enabled": True,
                "release": "stable",
                "models": (
                    native_model_ids(_workspace_path(user, workspaceId).as_posix())
                    if includeModels and workspaceId
                    else []
                ),
                "capabilities": await native.get_capabilities() if native else {},
                "sourceType": "builtin",
                "sourceName": "tcode-agent",
                "trustState": "builtin",
                "compatibilityState": "compatible",
                "conformanceState": "builtin",
                "capabilitiesVerified": [
                    "text",
                    "images",
                    "tools",
                    "resume",
                    "cancel",
                ],
            },
            {
                "runtimeId": "direct-echo",
                "displayName": "Direct Echo",
                "available": True,
                "enabled": True,
                "release": "stable",
                "capabilities": await echo.get_capabilities() if echo else {},
                "sourceType": "builtin",
                "sourceName": "tcode-agent",
                "trustState": "builtin",
                "compatibilityState": "compatible",
                "conformanceState": "builtin",
                "capabilitiesVerified": ["text", "cancel"],
            },
            *[
                item
                for item in framework_backends
                if principal is None
                or not principal.allowed_runtime_ids
                or item["runtimeId"] in principal.allowed_runtime_ids
            ],
            *[
                item.model_dump(mode="json", by_alias=True)
                for item in external
                if principal is None
                or not principal.allowed_runtime_ids
                or item.runtime_id in principal.allowed_runtime_ids
            ],
        ],
    }


@router.post("/agents/{agent_id}/run")
async def run_agent(agent_id: str, payload: RunAgentInput, request: Request):
    _enabled()
    if agent_id != "default":
        raise HTTPException(404, "Agent not found")
    user, principal = _identity(request)
    tcode = payload.forwarded_props.tcode
    _authorize(
        principal,
        scope="runs:create",
        workspace_id=tcode.workspace_id,
        runtime_id=tcode.runtime_id,
    )
    _authorize(
        principal,
        scope="runtimes:execute",
        workspace_id=tcode.workspace_id,
        runtime_id=tcode.runtime_id,
    )
    if tcode.disconnect_policy == "detach":
        _authorize(principal, scope="runs:detach")
    _rate_limit(user, principal, action="run", env_name="TCODE_AGUI_RUNS_PER_MINUTE", default=60)
    try:
        validate_public_input(payload.model_dump(mode="json", by_alias=True))
    except InputPolicyError as exc:
        raise HTTPException(413, str(exc)) from exc
    workdir = _workspace_path(user, tcode.workspace_id)
    await _ensure_session(payload, user=user, workdir=workdir)
    session = await SessionManager.get(
        payload.thread_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        workspace_id=tcode.workspace_id,
    )
    metadata = dict(session.metadata or {}) if session is not None else {
        "transport": "ag-ui",
        "harness": {
            "runtime_id": tcode.runtime_id,
            "model_id": tcode.model_id,
            "models": ({tcode.runtime_id: tcode.model_id} if tcode.model_id else {}),
            "switch_version": 0,
        },
    }
    if not isinstance(metadata.get("harness"), dict):
        metadata["harness"] = {
            "runtime_id": tcode.runtime_id,
            "model_id": tcode.model_id,
            "models": ({tcode.runtime_id: tcode.model_id} if tcode.model_id else {}),
            "switch_version": 0,
        }
        if session is not None:
            await SessionManager.update(
                session.id,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                workspace_id=tcode.workspace_id,
                metadata=metadata,
            )
            session.metadata = metadata
    harness_state: dict[str, Any] = {}
    try:
        harness_state = harness_handoff_store.assert_run_allowed(
            owner_key=f"{user.tenant_id}:{user.user_id}",
            workspace_id=tcode.workspace_id,
            session_id=payload.thread_id,
            runtime_id=tcode.runtime_id,
            session_metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(
            409,
            {
                "code": str(getattr(exc, "code", "invalid_runtime_transition")),
                "message": str(exc),
                "current_state": harness_handoff_store.projection(
                    dict(getattr(exc, "state", {}) or harness_state or {})
                ) if harness_state or getattr(exc, "state", None) else {},
            },
        ) from exc
    normalized = payload.normalized(agent_id)
    try:
        model = await resolve_runtime_model(
            tcode.runtime_id,
            tcode.model_id,
            workdir=workdir.as_posix(),
            runtime_registry=runtime_registry,
            backend_registry=agent_backend_registry,
        )
    except ModelPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    normalized.model_id = model.model_id
    pending_handoff = harness_handoff_store.pending_for_target(
        owner_key=f"{user.tenant_id}:{user.user_id}",
        workspace_id=tcode.workspace_id,
        session_id=payload.thread_id,
        target_runtime_id=tcode.runtime_id,
    )
    if pending_handoff and not any(
        str(item.get("type") or "") == "tcode.harness-handoff"
        for item in normalized.context
        if isinstance(item, dict)
    ):
        normalized.context.append(pending_handoff)
    normalized.forwarded_props.setdefault("tcode", {})["publicServer"] = auth_required()
    request_id = str(request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}")[:200]
    context = RunContext(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        workspace_id=tcode.workspace_id,
        workdir=workdir.as_posix(),
        session_id=payload.thread_id,
        run_id=payload.run_id,
        agent_id=agent_id,
        runtime_id=tcode.runtime_id,
        request_id=request_id,
        api_key_id=principal.key_id if principal else "",
        handoff_id=str((pending_handoff or {}).get("handoff_id") or ""),
    )
    try:
        prepared = await run_coordinator.prepare(
            normalized,
            context,
            client_request_id=tcode.client_request_id,
        )
    except RunConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={"code": exc.code, "message": str(exc), "requestId": request_id},
        )
    except RunUnavailableError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": "runtime_unavailable", "message": str(exc), "requestId": request_id},
        )

    async def mapped_events() -> AsyncIterator[dict[str, Any]]:
        try:
            async with aclosing(run_coordinator.stream(prepared)) as events:
                async for event in events:
                    mapped = runtime_event_to_agui(event)
                    if mapped is not None:
                        yield mapped
        finally:
            # Guidance can be submitted through the TCode control API while an
            # AG-UI client owns the foreground run. Resume its durable queue as
            # soon as that run releases the shared coordinator claim.
            from ._shared import _ensure_session_guidance_drain_v2
            from .guidance_runtime import guidance_v2_enabled

            if guidance_v2_enabled():
                _ensure_session_guidance_drain_v2(payload.thread_id)

    async def cancel_disconnected() -> None:
        await run_coordinator.cancel(
            owner_key=f"{user.tenant_id}:{user.user_id}",
            session_id=payload.thread_id,
            run_id=payload.run_id,
        )

    return StreamingResponse(
        bounded_sse_stream(
            mapped_events(),
            disconnect_policy=tcode.disconnect_policy,
            on_disconnect=cancel_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: CancelRunBody, request: Request):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="runs:cancel")
    _rate_limit(user, principal, action="write", env_name="TCODE_AGUI_WRITES_PER_MINUTE", default=120)
    cancelled = await run_coordinator.cancel(
        owner_key=f"{user.tenant_id}:{user.user_id}",
        session_id=payload.thread_id,
        run_id=run_id,
    )
    if not cancelled:
        raise HTTPException(404, "Active run not found")
    return {"runId": run_id, "status": "cancelling"}


@router.get("/interactions")
async def list_interactions(request: Request, threadId: str = ""):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="threads:read")
    _rate_limit(user, principal, action="read", env_name="TCODE_AGUI_READS_PER_MINUTE", default=300)
    return {
        "interactions": interaction_store.list_pending(
            owner_key=f"{user.tenant_id}:{user.user_id}", session_id=threadId
        )
    }


@router.post("/interactions/{interaction_id}/resolve")
async def resolve_interaction(
    interaction_id: str,
    payload: ResolveInteractionBody,
    request: Request,
):
    _enabled()
    user, principal = _identity(request)
    _authorize(principal, scope="runs:create")
    _rate_limit(user, principal, action="write", env_name="TCODE_AGUI_WRITES_PER_MINUTE", default=120)
    try:
        status, interaction = interaction_store.resolve(
            interaction_id,
            owner_key=f"{user.tenant_id}:{user.user_id}",
            resolution=payload.resolution,
        )
    except InteractionNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except InteractionConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": status, "interaction": interaction}


@router.post("/api-keys", status_code=201)
async def create_api_key(payload: CreateAPIKeyBody, request: Request):
    _enabled()
    user, principal = _identity(request)
    if principal is not None:
        raise HTTPException(403, "API keys can only be managed through a user session")
    try:
        token, metadata = api_key_store.create(
            user,
            scopes=set(payload.scopes),
            workspace_ids=set(payload.workspace_ids),
            allowed_runtime_ids=set(payload.runtime_ids),
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"token": token, **metadata}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request):
    _enabled()
    user, principal = _identity(request)
    if principal is not None:
        raise HTTPException(403, "API keys can only be managed through a user session")
    if not api_key_store.revoke(
        key_id, tenant_id=user.tenant_id, user_id=user.user_id
    ):
        raise HTTPException(404, "API key not found")
    return {"keyId": key_id, "revoked": True}


__all__ = ["router"]
