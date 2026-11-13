"""Fail-closed sandbox boundary checks for browser-like MCP servers."""

from __future__ import annotations

import fnmatch
import os
from typing import Any


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_patterns(name: str, default: str = "") -> list[str]:
    raw = str(os.environ.get(name, default) or "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _matches_patterns(value: str, patterns: list[str]) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    return any(
        pattern == normalized
        or (("*" in pattern or "?" in pattern) and fnmatch.fnmatch(normalized, pattern))
        for pattern in patterns
    )


def _broker_only_servers() -> set[str]:
    return set(
        _env_patterns("TCODE_SANDBOX_MCP_BROKER_ONLY_SERVERS", "job-protocol-mcp")
    )


def _sandboxed_servers() -> list[str]:
    return _env_patterns("TCODE_SANDBOXED_MCP_SERVERS")


def _required_servers() -> list[str]:
    return _env_patterns(
        "TCODE_SANDBOX_REQUIRED_MCP_SERVERS",
        "*browser*,*playwright*,*chrome*,*cdp*",
    )


def _browser_patterns() -> list[str]:
    return _env_patterns(
        "TCODE_BROWSER_MCP_SERVER_PATTERNS",
        "*browser*,*playwright*,*chrome*,*cdp*",
    )


def _browser_target() -> str:
    target = str(
        os.environ.get("TCODE_BROWSER_MCP_TARGET", "sandbox") or "sandbox"
    ).strip().lower()
    return target if target in {"sandbox", "host", "node"} else "sandbox"


def _server_flags(server_name: str, tool_name: str) -> tuple[str, bool, bool]:
    server = str(server_name or "").strip().lower()
    tool = str(tool_name or "").strip().lower()
    server_tool = f"{server}.{tool}" if tool else server
    sandboxed = _matches_patterns(server, _sandboxed_servers()) or _matches_patterns(
        server_tool, _sandboxed_servers()
    )
    browser_like = _matches_patterns(server, _browser_patterns()) or _matches_patterns(
        server_tool, _browser_patterns()
    )
    return server, sandboxed, browser_like


def mcp_sandbox_boundary_error(server_name: str, tool_name: str) -> str:
    """Return a fail-closed error for an unsafe direct MCP call."""
    server, sandboxed, browser_like = _server_flags(server_name, tool_name)
    if not server or server in _broker_only_servers():
        return ""

    server_tool = f"{server}.{str(tool_name or '').strip().lower()}"
    if browser_like:
        target = _browser_target()
        if target == "host" and not _env_truthy("TCODE_BROWSER_ALLOW_HOST_CONTROL"):
            return (
                f"Browser-like MCP server '{server_name}' is configured for host target, "
                "but TCODE_BROWSER_ALLOW_HOST_CONTROL is not enabled."
            )
        if target == "sandbox" and not sandboxed:
            return (
                f"Browser-like MCP server '{server_name}' must run behind a sandbox boundary. "
                "Start it as a Docker/gVisor MCP service and add it to "
                "TCODE_SANDBOXED_MCP_SERVERS."
            )
        if target == "node" and not _env_truthy("TCODE_BROWSER_NODE_ALLOWED"):
            return (
                f"Browser-like MCP server '{server_name}' requests node target, "
                "but TCODE_BROWSER_NODE_ALLOWED is not enabled."
            )

    required = _matches_patterns(server, _required_servers()) or _matches_patterns(
        server_tool, _required_servers()
    )
    if required and not sandboxed:
        return (
            f"MCP server '{server_name}' is marked sandbox-required but is not listed in "
            "TCODE_SANDBOXED_MCP_SERVERS."
        )
    return ""


def mcp_sandbox_boundary_metadata(server_name: str, tool_name: str) -> dict[str, Any]:
    _server, sandboxed, browser_like = _server_flags(server_name, tool_name)
    return {
        "mcp_sandbox_checked": True,
        "mcp_sandbox_asserted": sandboxed,
        "mcp_browser_like": browser_like,
        "mcp_browser_target": _browser_target() if browser_like else "",
    }
