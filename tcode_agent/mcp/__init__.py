"""MCP module - Model Context Protocol integration.

Heavy MCP/client modules are imported lazily so helper submodules can be used in
tests without loading the full tool registry.
"""

__all__ = [
    "MCPConfig",
    "MCPStatus",
    "MCPType",
    "MCPOAuthConfig",
    "MCPToolDefinition",
    "MCPClient",
    "StdioMCPClient",
    "RemoteMCPClient",
    "MCPManager",
    "MCPToolAdapter",
    "FastMCPClient",
]


def __getattr__(name: str):
    if name in {"MCPConfig", "MCPStatus", "MCPType", "MCPOAuthConfig", "MCPToolDefinition"}:
        from .config import MCPConfig, MCPStatus, MCPType, MCPOAuthConfig, MCPToolDefinition

        return {
            "MCPConfig": MCPConfig,
            "MCPStatus": MCPStatus,
            "MCPType": MCPType,
            "MCPOAuthConfig": MCPOAuthConfig,
            "MCPToolDefinition": MCPToolDefinition,
        }[name]
    if name in {"MCPClient", "StdioMCPClient", "RemoteMCPClient"}:
        from .client import MCPClient, RemoteMCPClient, StdioMCPClient

        return {
            "MCPClient": MCPClient,
            "StdioMCPClient": StdioMCPClient,
            "RemoteMCPClient": RemoteMCPClient,
        }[name]
    if name in {"MCPManager", "MCPToolAdapter"}:
        from .manager import MCPManager, MCPToolAdapter

        return {"MCPManager": MCPManager, "MCPToolAdapter": MCPToolAdapter}[name]
    if name == "FastMCPClient":
        from .fastmcp_client import FastMCPClient

        return FastMCPClient
    raise AttributeError(name)
