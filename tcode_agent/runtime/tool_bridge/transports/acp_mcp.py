"""ACP v1 stdio MCP descriptor for the per-Run platform bridge."""

from __future__ import annotations

import sys


def acp_mcp_server(*, socket_path: str, token: str) -> dict:
    return {
        "name": "tcode-platform",
        "command": sys.executable,
        "args": ["-m", "tcode_agent.runtime.tool_bridge.server"],
        "env": [
            {"name": "TCODE_PLATFORM_BRIDGE_SOCKET", "value": socket_path},
            {"name": "TCODE_PLATFORM_BRIDGE_TOKEN", "value": token},
        ],
        "_meta": {"tcode": {"schema": "tcode.platform-bridge-mcp.v1"}},
    }


__all__ = ["acp_mcp_server"]
