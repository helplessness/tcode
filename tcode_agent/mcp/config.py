"""MCP configuration types"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MCPType(Enum):
    """MCP server type"""

    LOCAL = "local"
    REMOTE = "remote"


class MCPStatus(Enum):
    """MCP connection status"""

    CONNECTED = "connected"
    DISABLED = "disabled"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    NEEDS_CLIENT_REGISTRATION = "needs_client_registration"


@dataclass
class MCPConfig:
    """Configuration for an MCP server"""

    name: str
    type: MCPType
    command: Optional[list[str]] = None  # For local MCP
    url: Optional[str] = None  # For remote MCP
    enabled: bool = True
    timeout: int = 30000
    headers: Optional[dict[str, str]] = None
    oauth: Optional[dict[str, Any]] = None

    @classmethod
    def from_dict(cls, name: str, config: dict) -> "MCPConfig":
        """Create config from dictionary"""
        mcp_type = MCPType(config.get("type", "local"))

        return cls(
            name=name,
            type=mcp_type,
            command=config.get("command"),
            url=config.get("url"),
            enabled=config.get("enabled", True),
            timeout=config.get("timeout", 30000),
            headers=config.get("headers"),
            oauth=config.get("oauth"),
        )


@dataclass
class MCPOAuthConfig:
    """OAuth configuration for MCP"""

    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scope: Optional[str] = None


@dataclass
class MCPToolDefinition:
    """Definition of an MCP tool"""

    name: str
    description: str
    input_schema: dict[str, Any]
    client: str
