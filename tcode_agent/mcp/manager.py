"""MCP Manager - Manages MCP server connections and tool integration"""

import asyncio
from typing import Any, Optional

from .client import MCPClient, StdioMCPClient, RemoteMCPClient
from .config import MCPConfig, MCPStatus, MCPToolDefinition
from ..tools.base import Tool, ToolContext
from ..types import ToolResult


class MCPToolAdapter(Tool):
    """Adapter to convert MCP tools to TCode tools"""

    def __init__(self, client: MCPClient, tool_def: MCPToolDefinition):
        self._client = client
        self._tool_def = tool_def

    @property
    def id(self) -> str:
        return f"{self._client.name}_{self._tool_def.name}"

    @property
    def description(self) -> str:
        return self._tool_def.description

    @property
    def parameters(self) -> type:
        import pydantic
        from ..tools.base import ParameterSchema

        schema = self._tool_def.input_schema
        properties = schema.get("properties", {})

        annotations = {}
        for k, v in properties.items():
            annotations[k] = Any

        return pydantic.create_model(f"MCPTool_{self._tool_def.name}", **annotations)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._client.call_tool(self._tool_def.name, args)


class MCPManager:
    """Manages MCP server connections and tool integration"""

    _clients: dict[str, MCPClient] = {}
    _status: dict[str, MCPStatus] = {}
    _configs: dict[str, MCPConfig] = {}

    @classmethod
    def configure(cls, configs: dict[str, dict]):
        """Configure MCP servers from config dictionary"""
        for name, config in configs.items():
            cls._configs[name] = MCPConfig.from_dict(name, config)

    @classmethod
    def add(cls, name: str, config: MCPConfig):
        """Add an MCP server configuration"""
        cls._configs[name] = config

    @classmethod
    async def connect(cls, name: str) -> MCPStatus:
        """Connect to an MCP server"""
        from ..server.desktop_config import remote_services_paused
        if remote_services_paused():
            cls._status[name] = MCPStatus.DISABLED
            return cls._status[name]
        config = cls._configs.get(name)
        if not config or not config.enabled:
            cls._status[name] = MCPStatus.DISABLED
            return cls._status[name]

        if name in cls._clients:
            await cls._clients[name].disconnect()

        if config.type.value == "local":
            client = StdioMCPClient(config)
        else:
            client = RemoteMCPClient(config)

        try:
            if await client.connect():
                cls._clients[name] = client
                cls._status[name] = MCPStatus.CONNECTED
            else:
                cls._status[name] = MCPStatus.FAILED
        except Exception:
            cls._status[name] = MCPStatus.FAILED

        return cls._status[name]

    @classmethod
    async def disconnect(cls, name: str):
        """Disconnect from an MCP server"""
        if name in cls._clients:
            await cls._clients[name].disconnect()
            del cls._clients[name]
        cls._status[name] = MCPStatus.DISABLED

    @classmethod
    async def reconnect(cls, name: str) -> MCPStatus:
        """Reconnect an MCP server"""
        await cls.disconnect(name)
        return await cls.connect(name)

    @classmethod
    def get_tools(cls) -> dict[str, Tool]:
        """Get all tools from connected MCP servers"""
        tools = {}

        for name, client in cls._clients.items():
            if not client.is_connected:
                continue

            try:
                mcp_tools = asyncio.get_event_loop().run_until_complete(
                    client.list_tools()
                )
                for tool_def in mcp_tools:
                    tools[f"{name}_{tool_def.name}"] = MCPToolAdapter(client, tool_def)
            except Exception:
                pass

        return tools

    @classmethod
    async def get_tools_async(cls) -> dict[str, Tool]:
        """Get all tools from connected MCP servers (async version)"""
        tools = {}

        for name, client in cls._clients.items():
            if not client.is_connected:
                continue

            try:
                mcp_tools = await client.list_tools()
                for tool_def in mcp_tools:
                    tools[f"{name}_{tool_def.name}"] = MCPToolAdapter(client, tool_def)
            except Exception:
                pass

        return tools

    @classmethod
    def get_status(cls, name: str) -> MCPStatus:
        """Get connection status for an MCP server"""
        return cls._status.get(name, MCPStatus.DISABLED)

    @classmethod
    def get_all_status(cls) -> dict[str, MCPStatus]:
        """Get all MCP server statuses"""
        return dict(cls._status)

    @classmethod
    async def authenticate(cls, name: str) -> bool:
        """Handle OAuth authentication for MCP server"""
        config = cls._configs.get(name)
        if not config or config.type.value != "remote":
            return False
        return True

    @classmethod
    async def close_all(cls):
        """Close all MCP connections"""
        for name in list(cls._clients.keys()):
            await cls.disconnect(name)
