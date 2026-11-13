"""MCP client implementations"""

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import aiohttp

from .config import MCPConfig, MCPStatus, MCPToolDefinition
from ..types import ToolResult
from ..runtime.local_process import start_local_process, stop_local_process


class MCPClient(ABC):
    """Base class for MCP clients"""

    def __init__(self, config: MCPConfig):
        self._config = config
        self._connected = False

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the MCP server"""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect from the MCP server"""
        pass

    @abstractmethod
    async def list_tools(self) -> list[MCPToolDefinition]:
        """List available tools from the MCP server"""
        pass

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """Call a tool on the MCP server"""
        pass


class StdioMCPClient(MCPClient):
    """Local MCP server using stdio"""

    def __init__(self, config: MCPConfig):
        super().__init__(config)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lifecycle_lock = asyncio.Lock()

    async def connect(self) -> bool:
        command = self._config.command
        if (not isinstance(command, (list, tuple)) or not command or not command[0]
                or not all(isinstance(arg, str) for arg in command)):
            return False

        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            raise RuntimeError('桌面本地 stdio MCP 尚未接入原生隔离，请使用远程 MCP')

        async with self._lifecycle_lock:
            try:
                if self._process:
                    if self._process.returncode is None:
                        return self._connected
                    await self._disconnect()

                self._process = await start_local_process(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                self._connected = True
                return True
            except Exception:
                return False

    async def disconnect(self):
        async with self._lifecycle_lock:
            await self._disconnect()

    async def _disconnect(self):
        if self._process:
            if not await stop_local_process(self._process, discard_output=True):
                raise RuntimeError("本地 MCP 服务退出未确认")
            self._process = None
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        # Implementation would send JSON-RPC request to list tools
        # This is a simplified version
        return []

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        # Implementation would send JSON-RPC request to call tool
        return ToolResult(
            title=name, output="", error="StdioMCPClient call_tool not implemented"
        )


class RemoteMCPClient(MCPClient):
    """Remote MCP server using HTTP/SSE"""

    def __init__(self, config: MCPConfig):
        super().__init__(config)
        self._session = None

    async def connect(self) -> bool:
        if not self._config.url:
            return False

        try:
            self._session = aiohttp.ClientSession(headers=self._config.headers or {})
            self._connected = True
            return True
        except Exception:
            return False

    async def disconnect(self):
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False

    async def list_tools(self) -> list[MCPToolDefinition]:
        if not self._session or not self._config.url:
            return []

        try:
            async with self._session.get(
                f"{self._config.url}/tools",
                timeout=aiohttp.ClientTimeout(total=self._config.timeout / 1000),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [
                        MCPToolDefinition(
                            name=tool["name"],
                            description=tool.get("description", ""),
                            input_schema=tool.get("inputSchema", {}),
                            client=self.name,
                        )
                        for tool in data.get("tools", [])
                    ]
        except Exception:
            pass

        return []

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if not self._session or not self._config.url:
            return ToolResult(title=name, output="", error="Not connected")

        try:
            async with self._session.post(
                f"{self._config.url}/tools/{name}",
                json={"arguments": arguments},
                timeout=aiohttp.ClientTimeout(total=self._config.timeout / 1000),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return ToolResult(
                        title=data.get("title", name),
                        output=data.get("content", ""),
                        metadata=data.get("metadata", {}),
                    )
                else:
                    return ToolResult(
                        title=name, output="", error=f"HTTP {resp.status}"
                    )
        except Exception as e:
            return ToolResult(title=name, output="", error=str(e))
