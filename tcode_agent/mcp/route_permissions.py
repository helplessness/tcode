"""Agent-to-MCP route authorization without tool-registry import side effects."""

from __future__ import annotations

from typing import Any, Optional


def normalize_allowed_routed_servers(allowed_tools: Any) -> Optional[set[str]]:
    if allowed_tools == "all":
        return None
    if not isinstance(allowed_tools, list):
        return set()

    normalized_items = [
        str(item or "").strip()
        for item in allowed_tools
        if str(item or "").strip()
    ]
    if len(normalized_items) == 1 and normalized_items[0].lower() == "all":
        return None

    servers: set[str] = set()
    for value in normalized_items:
        if value.startswith("mcp_"):
            remainder = value[len("mcp_") :]
            if "_" in remainder:
                server_name = remainder.split("_", 1)[0]
                if server_name:
                    servers.add(server_name.lower())
            continue
        if "." in value:
            value = value.split(".", 1)[0].strip()
        if value:
            servers.add(value.lower())
    return servers


def get_agent_allowed_routed_servers(context: Any) -> Optional[set[str]]:
    if context is None:
        return None
    agent_name = str(getattr(context, "agent", "") or "").strip()
    if not agent_name:
        return None
    try:
        from ..agent.agents import BuiltinAgents

        extra = getattr(context, "extra", None)
        workdir = extra.get("workdir") if isinstance(extra, dict) else None
        config = BuiltinAgents.get_agent_mcp_config(agent_name, workdir)
        if not isinstance(config, dict):
            return set()
        routed = config.get("allowed_routed_servers")
        if routed is None:
            routed = config.get("allowed_tools")
        result = normalize_allowed_routed_servers(routed)
        tool_servers = normalize_allowed_routed_servers(config.get('allowed_tools'))
        if result is not None and config.get('allow_plugin_async_targets', True) and (tool_servers is None or 'job-protocol-mcp' in tool_servers):
            from ..plugins.async_targets import approved_servers
            from ._utils import _context_control_workdir, _context_workspace_claims
            claims = _context_workspace_claims(context)
            owner = f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}"
            result |= approved_servers(_context_control_workdir(context), owner)
        return result
    except Exception:
        return set()


def agents_allowed_to_route_server(
    server_name: str,
    workdir: Optional[str] = None,
) -> list[str]:
    target = str(server_name or "").strip().lower()
    if not target:
        return []
    try:
        from ..agent.agents import BuiltinAgents

        allowed: list[tuple[int, str]] = []
        for agent in BuiltinAgents.list():
            name = str(getattr(agent, "name", "") or "").strip()
            if not name:
                continue
            config = BuiltinAgents.get_agent_mcp_config(name, workdir)
            if not isinstance(config, dict):
                continue
            routed = config.get("allowed_routed_servers")
            if routed is None:
                routed = config.get("allowed_tools")
            servers = normalize_allowed_routed_servers(routed)
            if servers is not None and target not in servers:
                continue
            try:
                priority = int(config.get("delegation_priority"))
            except (TypeError, ValueError, OverflowError):
                mode = str(getattr(agent, "mode", "") or "").strip().lower()
                priority = 50 if mode.endswith("manager") else 100
            allowed.append((priority, name))
    except Exception:
        return []

    deduplicated: dict[str, int] = {}
    for priority, name in allowed:
        deduplicated[name] = min(priority, deduplicated.get(name, priority))
    return sorted(deduplicated, key=lambda name: (deduplicated[name], name))


_normalize_allowed_routed_servers = normalize_allowed_routed_servers
_get_agent_allowed_routed_servers = get_agent_allowed_routed_servers
_agents_allowed_to_route_server = agents_allowed_to_route_server
