"""Agent, team, model, runtime and MCP catalogs.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import json
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from fastapi import APIRouter, HTTPException, Query, Request
from ._shared import _is_chat_session_busy

from ..agent.agents import BuiltinAgents
from ..config.manager import ConfigManager
from ..mcp.manager_auto import MCPManager
from ..mcp.executor_config import resolve_executor_mcp_config, executor_config_candidates, broker_servers
from ..plugins.manager import PluginManager
from ..runtime.gateway import agent_backend_registry, runtime_registry
from ..runtime.model_policy import native_model_ids
from ..team.catalog import TeamCatalog
from ..team.routing import TeamRouteRequest, TeamRoutingPolicy
from ..types import AgentMode
from .config_files import (
    _agent_mcp_config_path,
    _load_agent_mcp_config,
    _load_tcode_config,
)
from ._shared import (
    _model_config_supports_images,
    _refresh_runtime_providers,
)
from .workspace_scope import (
    _resolve_session_workdir,
    _runtime_session_matches_context,
    _workspace_context_from_request,
)
from .message_resources import (
    _inspect_local_paper_inputs,
)
from .auth_api import _require_tenant_admin
from .chat_setup import _persist_model_selection_for_workdir
from .plugin_warmup import plugin_request_wait_seconds, wait_for_plugin_warmup
from .schemas import SelectModelRequest, TeamRouteInspectRequest
from .state import ServerState

router = APIRouter()


async def _async_executor_services() -> list[dict]:
    """Return routed execution-plane MCPs hidden behind job-protocol-mcp."""
    from .desktop_config import remote_services_paused
    if remote_services_paused():
        return []

    routed = set(MCPManager.get_routing_policy_snapshot().get("routed_servers", []))
    # Execution configs may contain targets (including demos) outside the
    # explicit routing list. Keep the same per-service config precedence.
    for path in executor_config_candidates(workdir=ServerState.get_workdir(), include_foreground=False):
        try:
            configured = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(configured, dict):
            for name, entry in configured.items():
                if isinstance(entry, dict) and name not in broker_servers() and entry.get("invocation_mode") != "direct":
                    routed.add(name)
    configs: dict[str, dict] = {}
    services: list[tuple[str, dict]] = []
    for name in sorted(routed):
        path = resolve_executor_mcp_config(name, workdir=ServerState.get_workdir())
        if path is None:
            continue
        key = path.as_posix()
        if key not in configs:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                configs[key] = value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                configs[key] = {}
        entry = configs[key].get(name)
        if isinstance(entry, dict) and entry.get("enabled", True) is not False and str(entry.get("invocation_mode") or "auto").strip().lower() != "direct":
            services.append((name, entry))

    async def inspect(name: str, entry: dict) -> dict:
        connected = False
        error = ""
        try:
            headers = MCPManager._resolve_header_environment(entry.get("headers") or {})
            headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            url = str(entry.get("url") or "")
            initialize = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "tcode-health", "version": "1.0.0"}},
            }
            async with aiohttp.ClientSession() as client:
                session_id = ""
                async with asyncio.timeout(5):
                    if entry.get("transport") == "sse":
                        async with client.get(url, headers=headers, allow_redirects=False) as response:
                            response.raise_for_status()
                            event, data = "", []
                            endpoint = ""
                            async for raw in response.content:
                                line = raw.decode("utf-8").rstrip("\r\n")
                                if line.startswith("event:"):
                                    event = line[6:].strip()
                                elif line.startswith("data:"):
                                    data.append(line[5:].strip())
                                elif not line:
                                    if event == "endpoint":
                                        endpoint = urljoin(url, "\n".join(data))
                                        break
                                    event, data = "", []
                            origin, target = urlparse(url), urlparse(endpoint)
                            if not endpoint or (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
                                raise ValueError("invalid SSE endpoint origin")
                            async with client.post(endpoint, headers=headers, json=initialize, allow_redirects=False) as sent:
                                sent.raise_for_status()
                            payload = await MCPManager._read_jsonrpc_response(response, request_id=1)
                    else:
                        async with client.post(url, headers=headers, json=initialize, allow_redirects=False) as response:
                            response.raise_for_status()
                            payload = await MCPManager._read_jsonrpc_response(response, request_id=1)
                            session_id = response.headers.get("mcp-session-id")
                    result = payload.get("result") if isinstance(payload, dict) else None
                    connected = bool(isinstance(result, dict) and result.get("protocolVersion") and isinstance(result.get("serverInfo"), dict) and not payload.get("error"))
                    if not connected:
                        error = "服务未返回有效的 MCP 握手响应"
                if connected and session_id:
                    try:
                        async with asyncio.timeout(1), client.delete(url, headers={
                            **headers, "mcp-session-id": session_id,
                            "MCP-Protocol-Version": str(result["protocolVersion"]),
                        }, allow_redirects=False):
                            pass
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
        except aiohttp.ClientResponseError as exc:
            error = f"MCP 握手失败（HTTP {exc.status}）"
        except Exception as exc:
            # Never surface URLs, authentication headers or response bodies.
            error = f"MCP 握手未通过（{type(exc).__name__}）"
        tool_timeouts = entry.get("tool_timeout_seconds")
        return {
            "name": name,
            "status": "connected" if connected else "unreachable",
            "connected": connected,
            "health_check": "mcp_initialize",
            "error": error,
            "tools": len(tool_timeouts) if isinstance(tool_timeouts, dict) else 0,
            "callable_tools": 0,
            "route_mode": "job_protocol",
            "user_callable": False,
            "submit_via": "job-protocol-mcp",
            "route_label": "异步执行端",
            "route_reason": "已验证 MCP 握手；实际任务由 job-protocol-mcp 提交，执行结果以任务记录为准。" if connected else "MCP 握手未通过；实际任务由 job-protocol-mcp 提交。",
            "policy_flags": {"execution_plane": True, "configured_invocation_mode": "async"},
        }

    return list(await asyncio.gather(*(inspect(name, entry) for name, entry in services)))


async def _resolve_agent_catalog_workdir(
    request: Request,
    *,
    session_id: str = "",
    workdir_name: str = "",
) -> str:
    """Resolve the registry scope used by agent and team catalog requests."""

    session_text = str(session_id or "").strip()
    workdir_text = str(workdir_name or "").strip()
    if not session_text and not workdir_text:
        return ServerState.get_workdir()
    context = _workspace_context_from_request(request, workdir_name=workdir_text)
    return await _resolve_session_workdir(session_text, context)


@router.get("/api/agents")
async def list_agents(
    request: Request,
    session_id: str = Query(default=""),
    workdir_name: str = Query(default=""),
):
    from ..runtime.workspace_registry import registry_workspace

    workdir = await _resolve_agent_catalog_workdir(
        request,
        session_id=session_id,
        workdir_name=workdir_name,
    )
    with registry_workspace(workdir):
        await wait_for_plugin_warmup(
            workdir,
            timeout=plugin_request_wait_seconds(),
        )
        await PluginManager.get_global(workdir=workdir, ensure_configured=False)
        hidden_agent_names = {
            agent_name
            for entry in TeamCatalog.list(available_only=True)
            for agent_name in entry.hidden_agent_names
            if agent_name
        }
        agents = [
            {
                "name": agent.name,
                "description": agent.description,
                "mode": agent.mode.value,
            }
            for agent in BuiltinAgents.list()
            if (
                not agent.hidden
                and agent.mode in {AgentMode.PRIMARY, AgentMode.MANAGER}
                and agent.name not in hidden_agent_names
            )
        ]
        for entry in TeamCatalog.visible_alias_entries():
            agents.append(
                {
                    "name": entry.visible_agent_name,
                    "description": entry.mode_hint_visible or entry.display_name,
                    "mode": AgentMode.PRIMARY.value,
                }
            )
    return {"agents": agents}


@router.get("/api/teams/catalog")
async def get_team_catalog(
    request: Request,
    session_id: str = Query(default=""),
    workdir_name: str = Query(default=""),
):
    from ..runtime.workspace_registry import registry_workspace

    workdir = await _resolve_agent_catalog_workdir(
        request,
        session_id=session_id,
        workdir_name=workdir_name,
    )
    with registry_workspace(workdir):
        await wait_for_plugin_warmup(
            workdir,
            timeout=plugin_request_wait_seconds(),
        )
        await PluginManager.get_global(workdir=workdir, ensure_configured=False)
        teams = TeamCatalog.api_payload()
    return {"teams": teams}


@router.get("/api/teams/routing/rules")
async def get_team_routing_rules():
    return {"rules": TeamRoutingPolicy.list_rules()}


@router.post("/api/teams/routing/inspect")
async def inspect_team_routing(request: TeamRouteInspectRequest):
    message = str(request.message or "").strip()
    inspection = _inspect_local_paper_inputs(message)
    route_request = TeamRouteRequest(
        message=message,
        requested_agent_name=str(request.requested_agent_name or "").strip(),
        requested_execution_profile=str(request.requested_execution_profile or "").strip(),
        metadata={
            "paper_inspection": inspection,
        },
    )
    decision = TeamRoutingPolicy.route(route_request, available_only=True)
    return {
        "matched": decision is not None,
        "decision": decision.to_payload() if decision else None,
        "rules": TeamRoutingPolicy.list_rules(),
    }


@router.get("/api/models")
async def list_models(request: Request, session_id: str = Query("")):
    workspace_context = _workspace_context_from_request(request)
    target_workdir = (
        await _resolve_session_workdir(session_id, workspace_context)
        if str(session_id or "").strip()
        else ServerState.get_workdir()
    )
    config = _load_tcode_config(target_workdir)
    providers = config.get("provider", {})
    if not isinstance(providers, dict):
        providers = {}

    models = []
    for provider_id, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        model_map = provider_cfg.get("models", {})
        if not isinstance(model_map, dict):
            continue

        for model_id, model_cfg in model_map.items():
            model_name = model_id
            if isinstance(model_cfg, dict):
                model_name = str(model_cfg.get("name") or model_id)
            supports_images = _model_config_supports_images(model_cfg)
            models.append(
                {
                    "id": f"{provider_id}/{model_id}",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "name": model_name,
                    "supports_images": supports_images,
                }
            )

    selected = ConfigManager.resolve_model_selection(target_workdir)
    session_model = ""
    runtime_session = ServerState.sessions.get(str(session_id or "").strip(), {}) if str(session_id or "").strip() else {}
    if isinstance(runtime_session, dict) and _runtime_session_matches_context(
        runtime_session,
        workspace_context,
        include_workspace=True,
    ):
        session_model = str(runtime_session.get("model") or "").strip()
    model_ids = {str(item.get("id") or "").strip() for item in models if isinstance(item, dict)}
    first_model = str(models[0].get("id") or "").strip() if models else ""
    if session_model and session_model not in model_ids:
        session_model = ""
    current_model = str(
        session_model
        or config.get("model")
        or f"{selected.get('provider_id', '')}/{selected.get('model_id', '')}"
    ).strip()
    if current_model and model_ids and current_model not in model_ids:
        current_model = f"{selected.get('provider_id', '')}/{selected.get('model_id', '')}".strip("/")
    if current_model and model_ids and current_model not in model_ids:
        current_model = first_model

    return {
        "config_scope": "session" if str(session_id or "").strip() else "workspace",
        "config_workdir": target_workdir,
        "current_model": current_model,
        "models": models,
    }


@router.get("/api/chat/runtimes")
async def list_chat_runtimes(request: Request, session_id: str = Query("")):
    """返回当前会话可选择的原生与外部 Harness。"""

    from ..runtime.platform_capabilities.policy import (
        runtime_platform_capability_summary,
    )

    workspace_context = _workspace_context_from_request(request)
    target_workdir = (
        await _resolve_session_workdir(session_id, workspace_context)
        if str(session_id or "").strip()
        else ServerState.get_workdir()
    )
    native_models = native_model_ids(target_workdir)
    native_backend = agent_backend_registry.get("tcode-native")
    external = await runtime_registry.probes(include_models=True)
    external_ids = {item.runtime_id for item in external}
    framework_backends = []
    for backend in agent_backend_registry.list():
        if backend.backend_id in {"tcode-native", "direct-echo", *external_ids}:
            continue
        try:
            capabilities = await backend.get_capabilities()
            if not isinstance(capabilities, dict):
                capabilities = {}
            capabilities = {
                **capabilities,
                "platformBridge": runtime_platform_capability_summary(
                    backend.backend_id,
                    capabilities,
                ),
            }
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
                "runtime_id": backend.backend_id,
                "display_name": str(capabilities.get("displayName") or backend.backend_id),
                "available": backend_available,
                "enabled": True,
                "release": str(capabilities.get("release") or "experimental"),
                "models": list(models) if isinstance(models, list) else [],
                "diagnostics": backend_diagnostics,
                "capabilities": capabilities,
                "source_type": "entrypoint",
                "source_name": "tcode.agent_backends",
                "trust_state": "trusted",
                "compatibility_state": (
                    "compatible" if backend_available else "error"
                ),
                "conformance_state": str(
                    capabilities.get("conformanceState") or "unverified"
                ),
                "capabilities_verified": list(
                    capabilities.get("verifiedCapabilities") or []
                ),
            }
        )
    return {
        "default_runtime_id": "tcode-native",
        "preferred_model_id": "deepseek/deepseek-v4-flash",
        "runtimes": [
            {
                "runtime_id": "tcode-native",
                "display_name": "TCode",
                "available": True,
                "enabled": True,
                "release": "stable",
                "models": native_models,
                "capabilities": (
                    await native_backend.get_capabilities()
                    if native_backend
                    else {}
                ),
                "source_type": "builtin",
                "source_name": "tcode-agent",
                "trust_state": "builtin",
                "compatibility_state": "compatible",
                "conformance_state": "builtin",
                "capabilities_verified": [
                    "text",
                    "images",
                    "tools",
                    "resume",
                    "cancel",
                ],
            },
            *framework_backends,
            *[
                {
                    "runtime_id": item.runtime_id,
                    "display_name": item.display_name,
                    "available": item.available,
                    "enabled": item.enabled,
                    "release": item.release,
                    "models": item.models,
                    "diagnostics": item.diagnostics,
                    "capabilities": {
                        **item.capabilities,
                        "platformBridge": runtime_platform_capability_summary(
                            item.runtime_id,
                            item.capabilities,
                        ),
                    },
                    "source_type": item.source_type,
                    "source_name": item.source_name,
                    "distribution_name": item.distribution_name,
                    "distribution_version": item.distribution_version,
                    "definition_digest": item.definition_digest,
                    "trust_state": item.trust_state,
                    "compatibility_state": item.compatibility_state,
                    "conformance_state": item.conformance_state,
                    "capabilities_verified": item.capabilities_verified,
                }
                for item in external
            ],
        ],
    }


@router.post("/api/models/select")
async def select_model(payload: SelectModelRequest, request: Request):
    model_ref = (payload.model or "").strip()
    session_id = str(payload.session_id or "").strip()
    workspace_context = _workspace_context_from_request(request)
    if not session_id:
        _require_tenant_admin(request)
    target_workdir = (
        await _resolve_session_workdir(session_id, workspace_context)
        if session_id
        else ServerState.get_workdir()
    )
    if session_id and _is_chat_session_busy(session_id):
        raise HTTPException(409, '任务运行中不能切换模型，请等待结束或先停止任务')
    model_ref = _persist_model_selection_for_workdir(
        model_ref,
        workdir=target_workdir,
        prefer_local=bool(session_id),
    )
    if session_id and session_id in ServerState.sessions:
        ServerState.get_or_create_session(session_id, model=model_ref)
    await _refresh_runtime_providers()

    return {
        "status": "ok",
        "config_scope": "session" if session_id else "workspace",
        "config_workdir": target_workdir,
        "model": model_ref,
    }


@router.get("/api/agents/mcp")
async def list_agent_mcp_mapping():
    data = _load_agent_mcp_config()
    agents_cfg = data.get("agents", {})
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}

    mapping = {}
    for agent_name, cfg in agents_cfg.items():
        if not isinstance(cfg, dict):
            continue

        allowed_tools = cfg.get("allowed_tools", [])
        if allowed_tools == "all":
            normalized_tools = "all"
        elif isinstance(allowed_tools, list):
            normalized_tools = [str(item) for item in allowed_tools]
        else:
            normalized_tools = []

        mapping[agent_name] = {
            "allowed_tools": normalized_tools,
            "description": str(cfg.get("description", "")),
        }

    config_path = _agent_mcp_config_path()
    source = str(config_path.name) if config_path else ""

    return {
        "source": source,
        "agents": mapping,
    }


@router.get("/api/mcp/status")
async def mcp_status(refresh_failed: bool = Query(False)):
    await MCPManager.initialize(ServerState.get_workdir())
    if refresh_failed:
        await MCPManager.reconnect_failed()
    status = await MCPManager.get_status()
    tools = await MCPManager.get_tools()
    servers = []
    for name, mcp_status in status.items():
        server_tools = [t for t in tools.values() if t.get("server") == name]
        route_info = MCPManager.get_server_route_info(name)
        servers.append(
            {
                "name": name,
                "status": mcp_status.status,
                "connected": mcp_status.status == "connected",
                "error": mcp_status.error or "",
                "tools": len(server_tools),
                "callable_tools": len(server_tools) if route_info.get("user_callable") else 0,
                "route_mode": route_info.get("route_mode"),
                "user_callable": bool(route_info.get("user_callable")),
                "submit_via": route_info.get("submit_via"),
                "route_label": route_info.get("route_label"),
                "route_reason": route_info.get("route_reason"),
                "policy_flags": route_info.get("policy_flags", {}),
            }
        )
    known = {str(server["name"]).lower() for server in servers}
    servers.extend(
        service
        for service in await _async_executor_services()
        if str(service["name"]).lower() not in known
    )
    return {
        "servers": servers,
        "routing": MCPManager.get_routing_policy_snapshot(),
    }


@router.get("/api/mcp/tools")
async def mcp_tools(server: Optional[str] = None):
    await MCPManager.initialize(ServerState.get_workdir())
    if server:
        route_info = MCPManager.get_server_route_info(server)
        if not route_info.get("user_callable"):
            return {
                "tools": [],
                "direct_call_allowed": False,
                "route_mode": route_info.get("route_mode"),
                "submit_via": route_info.get("submit_via"),
                "message": route_info.get("route_reason"),
            }

    tools = await MCPManager.get_tools()
    if server:
        tools = {k: v for k, v in tools.items() if v.get("server") == server}
    return {
        "tools": list(tools.values()),
        "direct_call_allowed": True,
        "route_mode": "direct",
        "submit_via": None,
        "message": "",
    }
