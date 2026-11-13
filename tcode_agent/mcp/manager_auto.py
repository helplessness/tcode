"""MCP Manager - Auto-load MCP servers from mcp.json (Fixed with header-based session)"""

import asyncio
import aiohttp
import hmac
import json
import re
import os
import uuid
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

from ..config.manager import ConfigManager
from .apps import (
    MCP_APP_MIME_TYPE,
    MCP_APPS_EXTENSION_ID,
    resource_uri_from_metadata,
    tool_is_visible_to_app,
    tool_is_visible_to_model,
)
from .sandbox_boundary import mcp_sandbox_boundary_error


MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_LEGACY_PROTOCOL_VERSION = "2024-11-05"


class MCPSessionExpiredError(RuntimeError):
    """The server rejected the session before dispatching the invocation."""


@dataclass
class MCPStatus:
    status: str
    error: Optional[str] = None


@dataclass
class MCPClient:
    name: str
    url: str
    session: aiohttp.ClientSession
    session_id: Optional[str] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    protocol_version: str = MCP_PROTOCOL_VERSION
    server_capabilities: Dict[str, Any] = field(default_factory=dict)
    status: MCPStatus = field(default_factory=lambda: MCPStatus(status="connected"))


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class MCPManager(metaclass=WorkspaceRegistryMeta):

    """MCP Manager with header-based session management (like original tcode)"""
    _workspace_fields = ('_clients', '_status', '_server_sources', '_server_configs', '_initialized', '_connection_locks')

    _clients: Dict[str, MCPClient] = {}
    _status: Dict[str, MCPStatus] = {}
    _server_sources: Dict[str, str] = {}
    _server_configs: Dict[str, Dict[str, Any]] = {}
    _initialized: bool = False
    _connection_locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def _console_log(message: str) -> None:
        """Print MCP discovery details only when verbose initialization is enabled."""
        enabled = os.getenv("TCODE_CLI_SHOW_TOOL_INIT_LOGS", "1").strip().lower()
        if enabled not in {"0", "false", "no", "off", "quiet"}:
            print(message)

    @staticmethod
    def _server_env_key(server_name: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "_", str(server_name or "").upper()).strip("_")

    @staticmethod
    def _resolve_header_environment(headers: Dict[str, Any]) -> Dict[str, str]:
        """Expand ${ENV_NAME} placeholders without exposing values in errors."""

        pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = os.environ.get(name)
            if value is None:
                raise ValueError(f"Missing environment variable for MCP header: {name}")
            return value

        return {
            str(key): pattern.sub(replace, str(value))
            for key, value in headers.items()
            if value is not None
        }

    @classmethod
    def _coerce_timeout_ms(cls, value: Any, default: int) -> int:
        try:
            parsed = int(float(str(value).strip()))
        except Exception:
            parsed = int(default)
        return max(1000, min(parsed, 24 * 60 * 60 * 1000))

    @classmethod
    def _timeout_ms_from_env(cls, name: str, unit: str, default: int) -> Optional[int]:
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        if unit == "seconds":
            try:
                raw = str(int(float(raw) * 1000))
            except Exception:
                return None
        return cls._coerce_timeout_ms(raw, default)

    @classmethod
    def _timeout_ms_from_mapping(
        cls,
        mapping: Any,
        key: str,
        *,
        unit: str,
        default: int,
    ) -> Optional[int]:
        if not isinstance(mapping, dict):
            return None
        candidates = [
            key,
            key.lower(),
            key.replace("-", "_"),
            key.replace("_", "-"),
        ]
        value = None
        for candidate in candidates:
            if candidate in mapping:
                value = mapping.get(candidate)
                break
        if value is None:
            return None
        if isinstance(value, dict):
            for item_key, item_unit in (
                ("call_timeout_seconds", "seconds"),
                ("timeout_seconds", "seconds"),
                ("call_timeout", "ms"),
                ("timeout", "ms"),
                ("timeout_ms", "ms"),
            ):
                if item_key not in value:
                    continue
                return cls._coerce_config_timeout_ms(
                    value.get(item_key),
                    unit=item_unit,
                    default=default,
                )
            return None
        return cls._coerce_config_timeout_ms(value, unit=unit, default=default)

    @classmethod
    def _coerce_config_timeout_ms(cls, value: Any, *, unit: str, default: int) -> int:
        if unit == "seconds":
            try:
                value = int(float(str(value).strip()) * 1000)
            except Exception:
                value = default
        return cls._coerce_timeout_ms(value, default)

    @classmethod
    def _tool_config_timeout_ms(
        cls,
        config: dict[str, Any],
        tool_name: str,
        default: int,
    ) -> Optional[int]:
        tool = str(tool_name or "").strip()
        if not tool:
            return None

        for field, unit in (
            ("tool_timeout_seconds", "seconds"),
            ("tool_timeouts_seconds", "seconds"),
            ("tool_call_timeout_seconds", "seconds"),
            ("tool_timeouts", "ms"),
            ("tool_call_timeouts", "ms"),
        ):
            resolved = cls._timeout_ms_from_mapping(
                config.get(field),
                tool,
                unit=unit,
                default=default,
            )
            if resolved is not None:
                return resolved

        tools_cfg = config.get("tools")
        resolved = cls._timeout_ms_from_mapping(
            tools_cfg,
            tool,
            unit="ms",
            default=default,
        )
        if resolved is not None:
            return resolved

        return None

    @classmethod
    def _server_config_timeout_ms(cls, config: dict[str, Any], default: int) -> int:
        for key, unit in (
            ("call_timeout_seconds", "seconds"),
            ("timeout_seconds", "seconds"),
            ("call_timeout", "ms"),
        ):
            if key in config:
                return cls._coerce_config_timeout_ms(
                    config.get(key),
                    unit=unit,
                    default=default,
                )
        return default

    @classmethod
    def _call_timeout_ms(cls, server_name: str, tool_name: str = "") -> int:
        config = cls._server_configs.get(server_name, {}) or {}
        default = cls._coerce_timeout_ms(config.get("timeout", 30000), 30000)
        server_key = cls._server_env_key(server_name)
        tool_key = cls._server_env_key(tool_name)

        if server_key and tool_key:
            for name, unit in (
                (f"TCODE_MCP_CALL_TIMEOUT_MS_{server_key}_{tool_key}", "ms"),
                (f"TCODE_MCP_CALL_TIMEOUT_SECONDS_{server_key}_{tool_key}", "seconds"),
            ):
                resolved = cls._timeout_ms_from_env(name, unit, default)
                if resolved is not None:
                    return resolved

        if server_key:
            for name, unit in (
                (f"TCODE_MCP_CALL_TIMEOUT_MS_{server_key}", "ms"),
                (f"TCODE_MCP_CALL_TIMEOUT_SECONDS_{server_key}", "seconds"),
            ):
                resolved = cls._timeout_ms_from_env(name, unit, default)
                if resolved is not None:
                    return resolved

        tool_config_timeout = cls._tool_config_timeout_ms(config, tool_name, default)
        if tool_config_timeout is not None:
            return tool_config_timeout

        server_config_timeout = cls._server_config_timeout_ms(config, default)
        if server_config_timeout != default:
            return server_config_timeout

        for name, unit in (
            ("TCODE_MCP_CALL_TIMEOUT_MS", "ms"),
            ("TCODE_MCP_CALL_TIMEOUT_SECONDS", "seconds"),
        ):
            resolved = cls._timeout_ms_from_env(name, unit, default)
            if resolved is not None:
                return resolved

        return default

    @classmethod
    def _connect_timeout_ms(
        cls,
        server_name: str,
        config: dict[str, Any],
        default: int,
    ) -> int:
        server_key = cls._server_env_key(server_name)
        candidates: list[Optional[int]] = []

        if server_key:
            for prefix in ("HANDSHAKE", "CONNECT"):
                for name, unit in (
                    (f"TCODE_MCP_{prefix}_TIMEOUT_MS_{server_key}", "ms"),
                    (f"TCODE_MCP_{prefix}_TIMEOUT_SECONDS_{server_key}", "seconds"),
                ):
                    candidates.append(cls._timeout_ms_from_env(name, unit, default))

        for key, unit in (
            ("handshake_timeout_seconds", "seconds"),
            ("connect_timeout_seconds", "seconds"),
            ("handshake_timeout_ms", "ms"),
            ("connect_timeout_ms", "ms"),
            ("connect_timeout", "ms"),
        ):
            if key in config:
                candidates.append(
                    cls._coerce_config_timeout_ms(
                        config.get(key),
                        unit=unit,
                        default=default,
                    )
                )

        for prefix in ("HANDSHAKE", "CONNECT"):
            for name, unit in (
                (f"TCODE_MCP_{prefix}_TIMEOUT_MS", "ms"),
                (f"TCODE_MCP_{prefix}_TIMEOUT_SECONDS", "seconds"),
            ):
                candidates.append(cls._timeout_ms_from_env(name, unit, default))

        for value in candidates:
            if value is not None:
                return min(value, default)
        return default

    @staticmethod
    def _parse_server_set(env_name: str, default: str = "") -> set[str]:
        raw = os.getenv(env_name, default).strip()
        if not raw:
            return set()
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    @classmethod
    def _is_direct_call_allowed(cls, server_name: str) -> bool:
        # Explicit bypass for job-protocol runner subprocess.
        if os.getenv("SYMPHONY_ALLOW_NON_DIRECT_MCP", "").strip() == "1":
            return True

        server = (server_name or "").strip().lower()
        config = next(
            (value for name, value in cls._server_configs.items() if name.lower() == server),
            {},
        )
        invocation_mode = str(config.get("invocation_mode") or "auto").strip().lower()
        if server == "job-protocol-mcp" or invocation_mode == "direct":
            return True
        if invocation_mode == "async":
            return False

        enforce = os.getenv("SYMPHONY_ENFORCE_DIRECT_WHITELIST", "").strip().lower()
        if enforce not in {"1", "true", "yes", "on"}:
            return True

        direct = cls._parse_server_set("SYMPHONY_DIRECT_MCP_SERVERS")
        never_route = cls._parse_server_set(
            "SYMPHONY_NEVER_ROUTE_MCP_SERVERS", default="job-protocol-mcp"
        )

        if server in never_route:
            return True
        if direct and server in direct:
            return True
        return False

    @classmethod
    def get_server_route_info(cls, server_name: str) -> Dict[str, Any]:
        server = (server_name or "").strip().lower()
        if not server:
            return {
                "route_mode": "unknown",
                "user_callable": False,
                "submit_via": None,
                "route_label": "未知",
                "route_reason": "未提供 MCP server 名称。",
                "policy_flags": {},
            }

        bypass = os.getenv("SYMPHONY_ALLOW_NON_DIRECT_MCP", "").strip() == "1"
        enforce = os.getenv("SYMPHONY_ENFORCE_DIRECT_WHITELIST", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        direct = cls._parse_server_set("SYMPHONY_DIRECT_MCP_SERVERS")
        routed = cls._parse_server_set("SYMPHONY_ROUTED_MCP_SERVERS")
        never_route = cls._parse_server_set(
            "SYMPHONY_NEVER_ROUTE_MCP_SERVERS", default="job-protocol-mcp"
        )
        direct_allowed = cls._is_direct_call_allowed(server)
        config = next(
            (value for name, value in cls._server_configs.items() if name.lower() == server),
            {},
        )
        invocation_mode = str(config.get("invocation_mode") or "auto").strip().lower()
        configured_submit_via = str(config.get("submit_via") or "job-protocol-mcp").strip()

        in_direct = server in direct
        in_routed = server in routed
        in_never_route = server in never_route

        if bypass:
            route_mode = "direct"
            route_label = "直连已放开"
            route_reason = (
                "当前进程启用了 SYMPHONY_ALLOW_NON_DIRECT_MCP=1，"
                "已临时放开非直连 MCP 的直接调用限制。"
            )
        elif invocation_mode == "direct":
            route_mode = "direct"
            route_label = "配置为直连"
            route_reason = "该 MCP 在服务配置中明确允许直接调用。"
        elif invocation_mode == "async":
            route_mode = "job_protocol"
            route_label = f"经 {configured_submit_via} 提交"
            route_reason = f"该 MCP 在服务配置中设为异步任务，实际执行通过 {configured_submit_via} 提交。"
        elif in_never_route:
            route_mode = "direct"
            route_label = "保留直连"
            route_reason = (
                "该服务器在 SYMPHONY_NEVER_ROUTE_MCP_SERVERS 中，"
                "不会被转发到 Symphony 队列。"
            )
        elif direct_allowed:
            route_mode = "direct"
            if in_direct:
                route_label = "允许直连"
                route_reason = (
                    "该服务器在 SYMPHONY_DIRECT_MCP_SERVERS 中，"
                    "允许用户或 agent 直接调用。"
                )
            else:
                route_label = "默认直连"
                route_reason = (
                    "当前没有被直连白名单策略拦住，因此仍可直接调用。"
                )
        else:
            route_mode = "job_protocol"
            if in_routed:
                route_label = "经 job-protocol 提交"
                route_reason = (
                    "该服务器在 SYMPHONY_ROUTED_MCP_SERVERS 中，"
                    "前端只展示连接状态，实际执行必须通过 job-protocol-mcp 提交任务。"
                )
            elif enforce:
                route_label = "白名单外禁止直连"
                route_reason = (
                    "当前开启了 SYMPHONY_ENFORCE_DIRECT_WHITELIST，"
                    "该服务器不在直连白名单内，因此只能通过 Symphony/job-protocol 执行。"
                )
            else:
                route_label = "受路由策略控制"
                route_reason = (
                    "该服务器当前不建议直接调用，"
                    "请通过 Symphony/job-protocol 发起任务。"
                )

        effective_direct = route_mode == "direct"
        return {
            "route_mode": route_mode,
            "user_callable": effective_direct,
            "submit_via": None if effective_direct else configured_submit_via,
            "route_label": route_label,
            "route_reason": route_reason,
            "policy_flags": {
                "bypass_non_direct": bypass,
                "enforce_direct_whitelist": enforce,
                "in_direct_servers": in_direct,
                "in_routed_servers": in_routed,
                "in_never_route_servers": in_never_route,
                "configured_invocation_mode": invocation_mode,
            },
        }

    @classmethod
    def get_routing_policy_snapshot(cls) -> Dict[str, Any]:
        return {
            "direct_servers": sorted(cls._parse_server_set("SYMPHONY_DIRECT_MCP_SERVERS")),
            "routed_servers": sorted(cls._parse_server_set("SYMPHONY_ROUTED_MCP_SERVERS")),
            "never_route_servers": sorted(
                cls._parse_server_set(
                    "SYMPHONY_NEVER_ROUTE_MCP_SERVERS", default="job-protocol-mcp"
                )
            ),
            "enforce_direct_whitelist": os.getenv(
                "SYMPHONY_ENFORCE_DIRECT_WHITELIST", ""
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            "submit_via": "job-protocol-mcp",
        }

    @classmethod
    def resolve_mcp_config_path(cls, workdir: str = None) -> str:
        resolved_workdir = workdir or os.getenv("TCODE_WORKDIR") or str(
            Path(__file__).resolve().parents[2]
        )
        explicit = os.getenv("TCODE_MCP_CONFIG")
        if explicit:
            return explicit
        profile = os.getenv("TCODE_MCP_PROFILE", "").strip().lower()
        if profile in {"job-protocol-only", "job_protocol_only"}:
            lightweight = Path(resolved_workdir) / "config" / "mcp_job_protocol_only.json"
            if lightweight.exists():
                return str(lightweight)
        return ConfigManager.get_config_root_file_path(
            "mcp.json",
            resolved_workdir,
            prefer_effective=True,
        )

    @classmethod
    async def initialize(cls, workdir: str = None):
        if cls._initialized:
            return

        cls._clients = {}
        cls._status = {}
        cls._server_sources = {}
        cls._server_configs = {}

        if workdir is None:
            from ..runtime.workspace_registry import plugin_workspace
            workdir = (
                plugin_workspace.get()
                or os.getenv("TCODE_WORKDIR")
                or str(Path(__file__).resolve().parents[2])
            )

        mcp_config_path = cls.resolve_mcp_config_path(workdir)

        try:
            from ..server.managed_job_protocol import effective_config, read_config
            mcp_config = effective_config(read_config(mcp_config_path))
        except Exception:
            cls._initialized = True
            return

        if not mcp_config:
            cls._initialized = True
            return

        configured_servers = {
            name: cfg
            for name, cfg in mcp_config.items()
            if isinstance(name, str) and not name.startswith("$") and isinstance(cfg, dict)
        }

        cls._console_log(f"Found {len(configured_servers)} MCP server(s) in mcp.json")

        tasks = [
            cls._connect_server(name, cfg, source=str(mcp_config_path))
            for name, cfg in configured_servers.items()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        cls._initialized = True

        for name, status in cls._status.items():
            icon = "✓" if status.status == "connected" else "✗"
            cls._console_log(f"  {icon} {name}: {status.status}")

    @classmethod
    async def reload_config(cls, workdir: str = None):
        """Refresh file-owned servers without disconnecting plugin MCPs."""
        if not cls._initialized:
            await cls.initialize(workdir)
            return
        source = cls.resolve_mcp_config_path(workdir)
        path = Path(source)
        from ..server.managed_job_protocol import effective_config
        config = effective_config(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
        if not isinstance(config, dict):
            raise ValueError("MCP 配置必须是对象")
        servers = {name: entry for name, entry in config.items()
                   if not name.startswith("$") and isinstance(entry, dict)}
        for name in list(cls._server_sources):
            if cls._server_sources[name] != source or name in servers:
                continue
            async with cls._connection_locks.setdefault(name, asyncio.Lock()):
                client = cls._clients.pop(name, None)
                if client:
                    await client.session.close()
                cls._status.pop(name, None)
                cls._server_sources.pop(name, None)
                cls._server_configs.pop(name, None)
        for name, entry in servers.items():
            owner = cls._server_sources.get(name)
            if owner and owner != source:
                continue
            if cls._server_configs.get(name) == entry and cls._status.get(name, MCPStatus("unknown")).status == "connected":
                continue
            await cls._connect_server(name, entry, source=source)
            if entry.get("enabled") is False:
                cls._clients.pop(name, None)

    @classmethod
    async def _connect_server(cls, name: str, config: dict, source: str = "", *, only_if_missing=False):
        lock = cls._connection_locks.setdefault(name, asyncio.Lock())
        async with lock:
            await cls._connect_server_locked(name, config, source, only_if_missing=only_if_missing)

    @classmethod
    async def _connect_server_locked(cls, name: str, config: dict, source: str = "", *, only_if_missing=False):
        from ..server.desktop_config import remote_services_paused
        if remote_services_paused():
            cls._status[name] = MCPStatus(status='disabled', error='桌面远端服务已暂停')
            return
        current = cls._clients.get(name)
        if (only_if_missing and config.get("enabled") is not False
                and current is not None and current.url == config.get("url") and not current.session.closed
                and cls._status.get(name, current.status).status == "connected"):
            return
        session: Optional[aiohttp.ClientSession] = None
        try:
            existing_client = cls._clients.get(name)
            if existing_client:
                await existing_client.session.close()

            cls._server_sources[name] = source
            cls._server_configs[name] = dict(config)

            if config.get("enabled") is False:
                cls._status[name] = MCPStatus(status="disabled")
                return

            url = config.get("url")
            try:
                timeout_ms = max(1000, int(config.get("timeout", 30000) or 30000))
            except (TypeError, ValueError):
                timeout_ms = 30000
            connect_timeout_ms = cls._connect_timeout_ms(name, config, timeout_ms)
            connect_timeout = aiohttp.ClientTimeout(total=connect_timeout_ms / 1000)
            extra_headers = config.get("headers", {})

            if not url:
                cls._status[name] = MCPStatus(
                    status="failed", error="No URL configured"
                )
                return

            if extra_headers is None:
                extra_headers = {}
            if not isinstance(extra_headers, dict):
                cls._status[name] = MCPStatus(
                    status="failed", error="Invalid headers config: must be object"
                )
                return

            # Resolve secrets at connection time so public templates never need
            # to contain credentials. Missing variables fail closed and the
            # error reports only the variable name.
            normalized_headers = cls._resolve_header_environment(extra_headers)

            cls._console_log(f"Connecting to MCP server: {name} ({url})")

            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000)
            )

            base_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if normalized_headers:
                base_headers.update(normalized_headers)

            # 1. Initialize. Prefer the current protocol and MCP Apps extension.
            # A small legacy retry keeps older non-App MCP servers usable while
            # leaving the negotiated protocol explicit on every later request.
            attempts = (
                (
                    MCP_PROTOCOL_VERSION,
                    {
                        "extensions": {
                            MCP_APPS_EXTENSION_ID: {
                                "mimeTypes": [MCP_APP_MIME_TYPE],
                            }
                        }
                    },
                ),
                (MCP_LEGACY_PROTOCOL_VERSION, {}),
            )
            session_id = ""
            server_info: dict[str, Any] = {}
            server_capabilities: dict[str, Any] = {}
            protocol_version = MCP_PROTOCOL_VERSION
            initialize_error = "Initialize failed"
            for requested_version, client_capabilities in attempts:
                init_request = {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": requested_version,
                        "capabilities": client_capabilities,
                        "clientInfo": {"name": "tcode", "version": "1.0.0"},
                    },
                    "id": 1,
                }
                resp = await session.post(
                    url,
                    json=init_request,
                    headers=base_headers,
                    timeout=connect_timeout,
                )
                if resp.status != 200:
                    initialize_error = f"Initialize failed: {resp.status}"
                    resp.close()
                    continue
                result = await cls._read_jsonrpc_response(resp)
                candidate_session_id = str(
                    resp.headers.get("mcp-session-id") or ""
                ).strip()
                resp.close()
                if not isinstance(result, dict) or result.get("error"):
                    initialize_error = "Initialize returned a JSON-RPC error"
                    continue
                initialize_result = result.get("result", {})
                if not isinstance(initialize_result, dict):
                    initialize_error = "Initialize returned an invalid result"
                    continue
                if not candidate_session_id:
                    initialize_error = "No session ID in response"
                    continue
                session_id = candidate_session_id
                server_info = dict(initialize_result.get("serverInfo") or {})
                server_capabilities = dict(
                    initialize_result.get("capabilities") or {}
                )
                negotiated = str(
                    initialize_result.get("protocolVersion") or requested_version
                ).strip()
                protocol_version = (
                    negotiated
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", negotiated)
                    else requested_version
                )
                break

            if not session_id:
                cls._status[name] = MCPStatus(
                    status="failed",
                    error=initialize_error,
                )
                await session.close()
                return

            cls._console_log(f"  Session ID: {session_id}")
            cls._console_log(f"  Connected to: {server_info.get('name', 'Unknown')}")

            # 2. Send initialized notification (with session ID in header)
            init_headers = {
                **base_headers,
                "mcp-session-id": session_id,
                "MCP-Protocol-Version": protocol_version,
            }
            resp2 = await session.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                headers=init_headers,
                timeout=connect_timeout,
            )
            resp2.close()

            # 3. List tools (with session ID in header - SDK behavior)
            tools = await cls._list_tools(
                session,
                url,
                init_headers,
                request_timeout=connect_timeout,
            )
            resources: list[dict[str, Any]] = []
            if isinstance(server_capabilities.get("resources"), dict):
                resources = await cls._list_resources(
                    session,
                    url,
                    init_headers,
                    request_timeout=connect_timeout,
                )

            client = MCPClient(
                name=name,
                url=url,
                session=session,
                session_id=session_id,
                extra_headers=normalized_headers,
                tools=tools,
                resources=resources,
                protocol_version=protocol_version,
                server_capabilities=server_capabilities,
                status=MCPStatus(status="connected"),
            )

            cls._clients[name] = client
            cls._status[name] = MCPStatus(status="connected")

            cls._console_log(f"  Available tools: {len(tools)}")
            for tool in tools[:5]:
                cls._console_log(f"    - {tool.get('name', 'unknown')}")

        except asyncio.CancelledError:
            if session is not None and not session.closed:
                await session.close()
            if cls._clients.get(name) is existing_client:
                cls._clients.pop(name, None)
            cls._status[name] = MCPStatus(status="failed", error="MCP handshake cancelled")
            raise
        except asyncio.TimeoutError:
            if session is not None and not session.closed:
                await session.close()
            timeout_seconds = connect_timeout_ms / 1000 if "connect_timeout_ms" in locals() else 0
            error = f"MCP handshake timed out after {timeout_seconds:g}s"
            cls._console_log(f"  ✗ Failed to connect: {error}")
            cls._status[name] = MCPStatus(status="failed", error=error)
        except Exception as e:
            if session is not None and not session.closed:
                await session.close()
            cls._console_log(f"  ✗ Failed to connect: {e}")
            cls._status[name] = MCPStatus(status="failed", error=str(e))

    @classmethod
    async def reconnect_server(cls, server_name: str, *, only_if_missing=False) -> MCPStatus:
        if not cls._initialized:
            await cls.initialize()
        lock = cls._connection_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            return await cls._reconnect_server_locked(server_name, only_if_missing=only_if_missing)

    @classmethod
    async def _reconnect_server_locked(cls, server_name: str, *, only_if_missing=False) -> MCPStatus:
        config = cls._server_configs.get(server_name)
        if not config:
            status = MCPStatus(status="failed", error="No server config available")
            cls._status[server_name] = status
            return status

        await cls._connect_server_locked(
            server_name,
            dict(config),
            source=cls._server_sources.get(server_name, ""),
            only_if_missing=only_if_missing,
        )
        return cls._status.get(server_name, MCPStatus(status="unknown"))

    @classmethod
    async def _invalidate_expired_session(cls, server_name, client, status, body) -> bool:
        """Return whether expiry was confirmed, without evicting newer clients."""
        if status != 404:
            return False
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        if (not isinstance(error, dict) or payload.get("jsonrpc") != "2.0"
                or error.get("code") != -32600 or error.get("message") != "Session not found"):
            return False
        # A late failure from an old request must not evict a newer connection.
        if cls._clients.get(server_name) is not client:
            return True
        cls._clients.pop(server_name)
        client.status = MCPStatus(status="failed", error="MCP会话已失效；本次工具调用不会自动重试。")
        cls._status[server_name] = client.status
        await client.session.close()
        return True

    @classmethod
    async def ensure_session(cls, server_name: str) -> None:
        """Check a session using protocol ping, never replay a tool invocation."""
        if not cls._initialized:
            await cls.initialize()
        if not cls._is_server_published(server_name):
            raise RuntimeError(f"MCP server '{server_name}' not found")
        lock = cls._connection_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            await cls._ensure_session_locked(server_name)

    @classmethod
    async def _ensure_session_locked(cls, server_name: str) -> None:
        client = cls._clients.get(server_name)
        if client is None:
            status = await cls._reconnect_server_locked(server_name, only_if_missing=True)
            if status.status != "connected":
                raise RuntimeError(f"MCP session unavailable: {server_name}")
            return
        response = await client.session.post(
            client.url,
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                     "mcp-session-id": client.session_id, "MCP-Protocol-Version": client.protocol_version,
                     **client.extra_headers},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        try:
            if response.status != 200:
                body = await response.text()
                if await cls._invalidate_expired_session(server_name, client, response.status, body):
                    status = await cls._reconnect_server_locked(server_name, only_if_missing=True)
                    if status.status == "connected":
                        return
                    raise RuntimeError(f"MCP session reconnect failed: {status.error or status.status}")
                raise RuntimeError(f"MCP session preflight failed: HTTP {response.status}")
            result = await cls._read_jsonrpc_response(response, request_id=1)
            if "error" in result:
                raise RuntimeError("MCP session ping returned a JSON-RPC error")
        finally:
            response.close()

    @classmethod
    async def reconnect_failed(cls, names: Optional[list[str]] = None) -> Dict[str, MCPStatus]:
        if not cls._initialized:
            await cls.initialize()

        wanted = {str(name).strip() for name in (names or []) if str(name).strip()}
        targets = [
            name
            for name, status in list(cls._status.items())
            if (not wanted or name in wanted)
            and str(status.status or "").strip().lower() == "failed"
            and name in cls._server_configs
        ]
        if targets:
            await asyncio.gather(
                *[cls.reconnect_server(name, only_if_missing=True) for name in targets],
                return_exceptions=True,
            )
        return {name: cls._status.get(name, MCPStatus(status="unknown")) for name in targets}

    @classmethod
    async def _emit_progress_notification(
        cls,
        progress_callback: Any,
        payload: dict[str, Any],
    ) -> None:
        if not callable(progress_callback):
            return
        try:
            result = progress_callback(payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            # Progress is advisory and must never fail the MCP request itself.
            pass

    @classmethod
    async def _read_jsonrpc_response(
        cls,
        resp: aiohttp.ClientResponse,
        *,
        request_id: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            payload = await resp.json()
            return payload if isinstance(payload, dict) else {}
        return await cls._read_sse_jsonrpc_response(
            resp,
            request_id=request_id,
            progress_callback=progress_callback,
        )

    @classmethod
    async def _read_sse_jsonrpc_response(
        cls,
        resp: aiohttp.ClientResponse,
        *,
        request_id: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        data_lines: list[str] = []
        pending = ""

        def _parse_payload(payload: str) -> Optional[dict[str, Any]]:
            text = str(payload or "").strip()
            if not text or text == "[DONE]":
                return None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else {}

        async def _handle_payload(parsed: dict[str, Any]) -> Optional[dict[str, Any]]:
            method = str(parsed.get("method", "") or "").strip().lower()
            if method == "notifications/progress":
                params = parsed.get("params")
                await cls._emit_progress_notification(
                    progress_callback,
                    params if isinstance(params, dict) else {},
                )
                return None
            if method:
                # Other notifications are out-of-band and are not tool results.
                return None
            if request_id is not None and parsed.get("id") != request_id:
                return None
            return parsed

        async def _consume_data_lines() -> Optional[dict[str, Any]]:
            if not data_lines:
                return None
            parsed = _parse_payload("\n".join(data_lines))
            if parsed is None:
                return None
            data_lines.clear()
            return await _handle_payload(parsed)

        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            pending += chunk.decode("utf-8", errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    parsed = await _consume_data_lines()
                    if parsed is not None:
                        return parsed
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                parsed = _parse_payload(data)
                if parsed is not None:
                    handled = await _handle_payload(parsed)
                    if handled is not None:
                        return handled
                    continue
                data_lines.append(data)

        if pending.strip().startswith("data:"):
            data_lines.append(pending.strip()[len("data:") :].strip())
        parsed = await _consume_data_lines()
        if parsed is not None:
            return parsed
        return {}

    @classmethod
    async def _list_tools(
        cls,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict,
        *,
        request_timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> List[Dict[str, Any]]:
        """List the bounded MCP tool catalog, following pagination cursors."""

        tools: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page in range(32):
            request_id = 2 + page
            resp = await session.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/list",
                    "params": {"cursor": cursor} if cursor else {},
                    "id": request_id,
                },
                headers=headers,
                timeout=request_timeout,
            )
            if resp.status != 200:
                resp.close()
                break
            try:
                payload = await cls._read_jsonrpc_response(
                    resp,
                    request_id=request_id,
                )
                if "error" in payload:
                    break
                result = payload.get("result", {})
                page_tools = result.get("tools", []) if isinstance(result, dict) else []
                tools.extend(
                    dict(item)
                    for item in page_tools
                    if isinstance(item, dict)
                )
                if len(tools) >= 2_048:
                    return tools[:2_048]
                next_cursor = str(result.get("nextCursor") or "").strip()
            finally:
                resp.close()
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tools

    @classmethod
    async def _list_resources(
        cls,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict,
        *,
        request_timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> List[Dict[str, Any]]:
        """Read a bounded resource catalog for metadata fallback."""

        resources: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        try:
            for page in range(32):
                request_id = 100 + page
                response = await session.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "method": "resources/list",
                        "params": {"cursor": cursor} if cursor else {},
                        "id": request_id,
                    },
                    headers=headers,
                    timeout=request_timeout,
                )
                if response.status != 200:
                    response.close()
                    break
                try:
                    payload = await cls._read_jsonrpc_response(
                        response,
                        request_id=request_id,
                    )
                    if "error" in payload:
                        break
                    result = payload.get("result", {})
                    page_resources = (
                        result.get("resources", [])
                        if isinstance(result, dict)
                        else []
                    )
                    resources.extend(
                        dict(item)
                        for item in page_resources
                        if isinstance(item, dict)
                    )
                    if len(resources) >= 2_048:
                        return resources[:2_048]
                    next_cursor = str(result.get("nextCursor") or "").strip()
                finally:
                    response.close()
                if not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            return resources
        except Exception:
            return resources

    @classmethod
    async def get_tools(cls) -> Dict[str, Any]:
        if not cls._initialized:
            await cls.initialize()

        tools = {}
        for client_name, client in cls._clients.items():
            if not cls._is_server_published(client_name):
                continue
            for tool in client.tools:
                if (client_name, tool["name"]) == ("job-protocol-mcp", "lookup_job_submission"):
                    continue  # Worker-only receipt lookup; never advertise it to models.
                if not tool_is_visible_to_model(tool.get("_meta")):
                    continue
                tool_name = f"mcp_{client_name}_{tool['name']}"
                tools[tool_name] = {
                    "server": client_name,
                    "tool": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                    "_meta": dict(tool.get("_meta") or {}),
                }
        return tools

    @classmethod
    def get_server_tool(cls, server_name: str, tool_name: str) -> dict[str, Any] | None:
        client = cls._clients.get(server_name)
        if client is None:
            return None
        for tool in client.tools:
            if str(tool.get("name") or "") == str(tool_name or ""):
                return dict(tool)
        return None

    @classmethod
    def get_app_tools_for_resource(cls, server_name: str, uri: str) -> list[str]:
        """Return app-visible tools explicitly bound to one UI resource."""

        client = cls._clients.get(server_name)
        if client is None:
            return []
        names: list[str] = []
        for tool in client.tools:
            metadata = tool.get("_meta") if isinstance(tool, dict) else None
            if not tool_is_visible_to_app(metadata):
                continue
            if resource_uri_from_metadata(metadata, {}) != uri:
                continue
            name = str(tool.get("name") or "").strip()
            if name:
                names.append(name)
        return names

    @classmethod
    def get_resource_metadata(cls, server_name: str, uri: str) -> dict[str, Any]:
        client = cls._clients.get(server_name)
        if client is None:
            return {}
        for resource in client.resources:
            if str(resource.get("uri") or "") == uri:
                return dict(resource.get("_meta") or {})
        return {}

    @classmethod
    def get_server_capability_digest(cls, server_name: str) -> str:
        import hashlib

        client = cls._clients.get(server_name)
        if client is None:
            return ""
        payload = {
            "protocolVersion": client.protocol_version,
            "capabilities": client.server_capabilities,
            "tools": [
                {"name": item.get("name"), "_meta": item.get("_meta")}
                for item in client.tools
            ],
            "resources": [
                {
                    "uri": item.get("uri"),
                    "mimeType": item.get("mimeType"),
                    "_meta": item.get("_meta"),
                }
                for item in client.resources
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    async def read_resource(cls, server_name: str, uri: str) -> list[dict[str, Any]]:
        """读取 MCP App 资源，同时保留 MCP ResourceContents DTO。"""

        from .apps import validate_resource_contents, validate_resource_uri

        resource_uri = validate_resource_uri(uri)
        if not cls._initialized:
            await cls.initialize()
        if not cls._is_server_published(server_name):
            raise Exception(f"MCP server '{server_name}' not found")

        client = cls._clients.get(server_name)
        if not client:
            raise Exception(f"MCP server '{server_name}' not found")
        request_id = 1
        request = {
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {"uri": resource_uri},
            "id": request_id,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": client.session_id,
            "MCP-Protocol-Version": client.protocol_version,
            **client.extra_headers,
        }
        response = await client.session.post(
            client.url,
            json=request,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        )
        try:
            if response.status != 200:
                raise RuntimeError(f"MCP resource read failed: HTTP {response.status}")
            result = await cls._read_jsonrpc_response(response, request_id=request_id)
            if "error" in result:
                raise RuntimeError(
                    f"MCP resource read failed: {result['error'].get('message', 'unknown error')}"
                )
            contents = result.get("result", {}).get("contents", [])
            return validate_resource_contents(
                contents,
                resource_uri,
                fallback_metadata=cls.get_resource_metadata(server_name, resource_uri),
            )
        finally:
            response.close()

    @classmethod
    async def call_tool(
        cls,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        progress_callback: Any = None,
        progress_token: Optional[str] = None,
        _job_worker_token: str | None = None,
    ) -> Any:
        from ..runtime.async_resume import assert_submission_allowed
        assert_submission_allowed(server_name, tool_name)
        boundary_error = mcp_sandbox_boundary_error(server_name, tool_name)
        if boundary_error:
            raise PermissionError(boundary_error)

        if not cls._is_direct_call_allowed(server_name):
            raise Exception(
                f"Direct MCP call is disabled by policy for server '{server_name}'. "
                "Use Symphony/job-protocol routing instead."
            )

        if not cls._initialized:
            await cls.initialize()

        if not cls._is_server_published(server_name):
            if (server_name, tool_name) == ("job-protocol-mcp", "submit_job"):
                raise RuntimeError("MCP server 'job-protocol-mcp' not found. Remote calculation was "
                                   "not submitted. Configure Job Protocol and register the remote executor.")
            raise Exception(f"MCP server '{server_name}' not found")

        from .dynamic_headers import snapshot
        business, call_context = snapshot(server_name, arguments)
        if call_context:
            return await cls._call_with_context(server_name, tool_name, business, call_context, progress_callback)
        arguments = business
        if (server_name, tool_name) == ("job-protocol-mcp", "describe_target_tool"):
            await cls.ensure_session(server_name)
        client = cls._clients.get(server_name)
        if not client and server_name in cls._server_configs:
            status = cls._status.get(server_name)
            if status is not None and status.status != "connected":
                await cls.reconnect_server(server_name, only_if_missing=True)
                client = cls._clients.get(server_name)
        if not client:
            status = cls._status.get(server_name)
            detail = f": {status.error}" if status and status.error else ""
            raise Exception(f"MCP server '{server_name}' not found{detail}")

        # Use header-based session (SDK behavior)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": client.session_id,
            "MCP-Protocol-Version": client.protocol_version,
        }
        if client.extra_headers:
            headers.update(client.extra_headers)
        if _job_worker_token is not None:
            from ..server.managed_job_protocol import LOCAL_URL, WORKER_HEADER
            if (server_name != "job-protocol-mcp" or tool_name not in {"submit_job", "lookup_job_submission"}
                    or not _job_worker_token or client.url != os.environ.get(LOCAL_URL)):
                raise PermissionError("内置 Job Protocol 提交凭据只能用于本机后台派发")
            headers[WORKER_HEADER] = _job_worker_token

        request_id = 1
        params: dict[str, Any] = {"name": tool_name, "arguments": arguments}
        if callable(progress_callback):
            params["_meta"] = {
                "progressToken": str(progress_token or f"tcode-{uuid.uuid4().hex}")
            }
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": params,
            "id": request_id,
        }

        timeout_ms = cls._call_timeout_ms(server_name, tool_name)
        resp: Optional[aiohttp.ClientResponse] = None
        try:
            resp = await client.session.post(
                client.url,
                json=request,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000),
                **({"allow_redirects": False} if _job_worker_token is not None else {}),
            )
            if resp.status != 200:
                error_text = await resp.text()
                if await cls._invalidate_expired_session(server_name, client, resp.status, error_text):
                    raise MCPSessionExpiredError(f"MCP session expired: {server_name}")
                raise Exception(f"MCP tool call failed: {resp.status} - {error_text}")

            result = await cls._read_jsonrpc_response(
                resp,
                request_id=request_id,
                progress_callback=progress_callback,
            )
            if "error" in result:
                error = result["error"]
                raise Exception(
                    f"MCP Error: {error.get('message', 'Unknown error')}"
                )
            return result.get("result", {})
        except asyncio.TimeoutError as exc:
            timeout_seconds = timeout_ms / 1000
            raise TimeoutError(
                f"MCP tool call timed out after {timeout_seconds:g}s: "
                f"{server_name}.{tool_name}"
            ) from exc
        except aiohttp.ClientError as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise RuntimeError(
                f"MCP transport error calling {server_name}.{tool_name}: {detail}"
            ) from exc
        finally:
            if resp is not None:
                resp.close()

    @classmethod
    async def _call_with_context(cls, server_name, tool_name, arguments, call_context, progress_callback=None):
        """A fresh SDK session binds initialize, tools/call and cleanup to one context."""
        from ..server.desktop_config import remote_services_paused
        if remote_services_paused():
            raise ValueError('桌面远端服务已暂停，未发送连接请求')
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport, SSETransport
        from .dynamic_headers import render
        config = cls._server_configs[server_name]
        if config.get('enabled') is False:
            raise ValueError('MCP 服务已停用，未发送请求')
        headers = cls._resolve_header_environment(config.get('headers') or {})
        headers.update(render(call_context['rules'], call_context['values']))
        transport = SSETransport if config.get('transport') == 'sse' else StreamableHttpTransport
        timeout = cls._call_timeout_ms(server_name, tool_name) / 1000
        try:
            async def progress(progress, total=None, message=None):
                if progress_callback:
                    await progress_callback({'progress': progress, 'total': total, 'message': message})
            async with Client(transport(config['url'], headers=headers), timeout=timeout,
                              init_timeout=cls._connect_timeout_ms(server_name, config, 30000) / 1000) as client:
                result = await client.call_tool(tool_name, arguments, raise_on_error=False, progress_handler=progress)
                return {'content': [block.model_dump(mode='json', by_alias=True) for block in result.content],
                        'isError': result.is_error, 'structuredContent': result.structured_content,
                        **({'_meta': result.meta} if getattr(result, 'meta', None) else {})}
        except Exception as exc:
            # Provider exceptions may include the entire request/header value.
            raise RuntimeError(f'MCP 动态上下文调用失败：{server_name}.{tool_name} ({type(exc).__name__})') from None

    @classmethod
    async def call_app_tool(
        cls,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        resource_uri: str,
        allowed_tools: list[str] | tuple[str, ...],
        capability_digest: str = "",
    ) -> Any:
        """Call a tool only when the issued App capability still authorizes it."""

        if capability_digest and not hmac.compare_digest(
            capability_digest,
            cls.get_server_capability_digest(server_name),
        ):
            raise PermissionError("MCP App capability changed; reload the App")
        if tool_name not in set(allowed_tools):
            raise PermissionError("MCP App tool is outside the issued capability")
        tool = cls.get_server_tool(server_name, tool_name)
        if tool is None or not tool_is_visible_to_app(tool.get("_meta")):
            raise PermissionError("MCP tool is not visible to this App")
        if resource_uri_from_metadata(tool.get("_meta"), {}) != resource_uri:
            raise PermissionError("MCP tool belongs to a different App resource")
        return await cls.call_tool(server_name, tool_name, arguments)

    @classmethod
    async def get_status(cls) -> Dict[str, MCPStatus]:
        return {
            name: status
            for name, status in cls._status.items()
            if cls._is_server_published(name)
        }

    @classmethod
    def _is_server_published(cls, name: str) -> bool:
        """Hide instance-scoped plugin MCP servers until PluginRegistry commit."""

        source = str(cls._server_sources.get(name) or "")
        if not source.startswith("plugin:"):
            return True
        ownership = source.removeprefix("plugin:")
        if ":" not in ownership:
            # Legacy plugin sources have no instance identity and retain their
            # historical immediate visibility.
            return True
        plugin_name, instance_id = ownership.rsplit(":", 1)
        if not plugin_name or not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            # A legacy plugin name may itself contain ':'. Only the UUID-hex
            # suffix emitted by PluginManager activates the pending barrier.
            return True
        from ..plugins.registry import PluginRegistry

        loaded = PluginRegistry.get(plugin_name)
        return loaded is not None and loaded.instance_id == instance_id

    @classmethod
    def get_server_records(
        cls,
        source: Optional[str] = None,
        *,
        include_pending: bool = False,
    ) -> List[Dict[str, Any]]:
        names = list(cls._status.keys())
        if source is not None:
            names = [name for name in names if cls._server_sources.get(name) == source]
        if not include_pending:
            names = [name for name in names if cls._is_server_published(name)]

        records: List[Dict[str, Any]] = []
        for name in sorted(names):
            status = cls._status.get(name, MCPStatus(status="unknown"))
            client = cls._clients.get(name)
            config = cls._server_configs.get(name, {})
            records.append(
                {
                    "name": name,
                    "status": status.status,
                    "connected": status.status == "connected",
                    "error": status.error,
                    "url": client.url if client else config.get("url"),
                    "tools": len(client.tools) if client else 0,
                    "tool_names": [tool.get("name", "unknown") for tool in (client.tools if client else [])],
                    "resources": len(client.resources) if client else 0,
                    "protocol_version": client.protocol_version if client else None,
                    "server_capabilities": dict(client.server_capabilities) if client else {},
                    "source": cls._server_sources.get(name, ""),
                }
            )
        return records

    @classmethod
    async def register_servers(
        cls, servers: Dict[str, Dict[str, Any]], source: str
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        if not cls._initialized:
            await cls.initialize()

        registered: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for name, config in servers.items():
            if not isinstance(config, dict):
                errors.append({"component": name, "error": "invalid MCP config: expected object"})
                continue

            existing_source = cls._server_sources.get(name)
            if existing_source and existing_source != source:
                errors.append(
                    {
                        "component": name,
                        "error": f"MCP server name conflict: '{name}' already registered from {existing_source}",
                    }
                )
                continue

            await cls._connect_server(name, config, source=source)
            registered.extend(
                cls.get_server_records(source=source, include_pending=True)
            )

        unique_registered = {item["name"]: item for item in registered}
        return list(unique_registered.values()), errors

    @classmethod
    async def unregister_servers_by_source(cls, source: str) -> List[str]:
        names = [name for name, server_source in cls._server_sources.items() if server_source == source]
        removed: List[str] = []
        for name in names:
            client = cls._clients.pop(name, None)
            if client:
                await client.session.close()
            cls._status.pop(name, None)
            cls._server_sources.pop(name, None)
            cls._server_configs.pop(name, None)
            removed.append(name)
        return removed

    @classmethod
    async def close(cls):
        from ..plugins.manager import PluginManager
        from ..runtime.workspace_registry import plugin_workspace
        try:
            manager = next((item for item in PluginManager._instances.values()
                if item.workdir == plugin_workspace.get()), None)
            if manager is not None:
                await manager.trigger("mcp.disconnect", {}, {})
        except Exception:
            pass

        for client in cls._clients.values():
            await client.session.close()
        cls._clients.clear()
        cls._status.clear()
        cls._server_sources.clear()
        cls._server_configs.clear()
        cls._initialized = False

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._initialized
