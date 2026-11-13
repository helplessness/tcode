"""FastMCP 客户端 - 用于连接 FastMCP 服务器"""

import aiohttp
import json
from typing import Any, Optional, Dict, List


class FastMCPClient:
    """FastMCP 服务器客户端"""

    def __init__(self, base_url: str, timeout: int = 30):
        """
        初始化 FastMCP 客户端

        Args:
            base_url: MCP 服务器基础 URL，如 "http://127.0.0.1:9999"
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session: Optional[aiohttp.ClientSession] = None
        self.server_info: Optional[Dict] = None
        self.session_id: Optional[str] = None
        self._request_id = 0

    async def connect(self):
        """连接到 MCP 服务器"""
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )

        # 生成 session ID
        import uuid

        self.session_id = str(uuid.uuid4())

        # MCP 协议握手 - 先发送 initialize
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        initialize_request = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tcode-agent", "version": "1.0.0"},
            },
            "id": 1,
        }

        try:
            async with self.session.post(
                f"{self.base_url}/mcp", json=initialize_request, headers=headers
            ) as resp:
                if resp.status == 200:
                    # 读取 SSE 流格式响应
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/event-stream" in content_type:
                        text = await resp.text()
                        # 解析 SSE 数据
                        import re

                        data_match = re.search(r"data: ({.*?})\n", text, re.DOTALL)
                        if data_match:
                            result = json.loads(data_match.group(1))
                            self.server_info = result.get("result", {}).get(
                                "serverInfo", {}
                            )
                            print(
                                f"Connected to MCP server: {self.server_info.get('name', 'Unknown')}"
                            )
                    else:
                        result = await resp.json()
                        self.server_info = result.get("result", {}).get(
                            "serverInfo", {}
                        )
                        print(
                            f"Connected to MCP server: {self.server_info.get('name', 'Unknown')}"
                        )
                else:
                    print(f"Initialize failed: {resp.status}")
        except Exception as e:
            print(f"Initialize error: {e}")

        # 发送 initialized 通知（不需要响应）
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }

        try:
            async with self.session.post(
                f"{self.base_url}/mcp", json=initialized_notification, headers=headers
            ):
                pass  # 通知不需要响应
        except Exception:
            pass

    def _next_request_id(self) -> int:
        """生成下一个请求 ID"""
        self._request_id += 1
        return self._request_id

    async def disconnect(self):
        """断开连接"""
        if self.session:
            await self.session.close()
            self.session = None

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        调用 MCP 工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        if self.session is None:
            await self.connect()

        # MCP JSON-RPC 请求
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": {"sessionId": self.session_id},
            },
            "id": self._next_request_id(),
        }

        # FastMCP Streamable HTTP requires both content types
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        async with self.session.post(
            f"{self.base_url}/mcp", json=request, headers=headers
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"MCP tool call failed: {resp.status} - {error_text}")

            result = await resp.json()

            # 处理 JSON-RPC 错误
            if "error" in result:
                error = result["error"]
                raise Exception(f"MCP Error: {error.get('message', 'Unknown error')}")

            return result.get("result", {})

    async def list_tools(self) -> List[Dict[str, Any]]:
        """
        列出所有可用工具

        Returns:
            工具列表
        """
        if self.session is None:
            await self.connect()

        request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {"_meta": {"sessionId": self.session_id}},
            "id": self._next_request_id(),
        }

        # FastMCP Streamable HTTP requires both content types
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        async with self.session.post(
            f"{self.base_url}/mcp", json=request, headers=headers
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"MCP list tools failed: {resp.status} - {error_text}")

            result = await resp.json()

            if "error" in result:
                error = result["error"]
                raise Exception(f"MCP Error: {error.get('message', 'Unknown error')}")

            return result.get("result", {}).get("tools", [])

    async def get_resource(self, uri: str) -> List[Dict[str, Any]]:
        """
        获取 MCP 资源

        Args:
            uri: 资源 URI，如 "config://app"

        Returns:
            完整的 ResourceContents DTO 列表
        """
        if self.session is None:
            await self.connect()

        request = {
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {"uri": uri, "_meta": {"sessionId": self.session_id}},
            "id": self._next_request_id(),
        }

        # FastMCP Streamable HTTP requires both content types
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        async with self.session.post(
            f"{self.base_url}/mcp", json=request, headers=headers
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(
                    f"MCP get resource failed: {resp.status} - {error_text}"
                )

            result = await resp.json()

            if "error" in result:
                error = result["error"]
                raise Exception(f"MCP Error: {error.get('message', 'Unknown error')}")

            contents = result.get("result", {}).get("contents", [])
            return [dict(item) for item in contents if isinstance(item, dict)]

    async def list_resources(self) -> List[Dict[str, Any]]:
        """
        列出所有可用资源

        Returns:
            资源列表
        """
        if self.session is None:
            await self.connect()

        request = {
            "jsonrpc": "2.0",
            "method": "resources/list",
            "params": {"_meta": {"sessionId": self.session_id}},
            "id": self._next_request_id(),
        }

        # FastMCP Streamable HTTP requires both content types
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        async with self.session.post(
            f"{self.base_url}/mcp", json=request, headers=headers
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(
                    f"MCP list resources failed: {resp.status} - {error_text}"
                )

            result = await resp.json()
            return result.get("result", {}).get("resources", [])

    def __del__(self):
        """析构函数，确保关闭连接"""
        if self.session is not None:
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.session.close())
                else:
                    loop.run_until_complete(self.session.close())
            except Exception:
                pass
