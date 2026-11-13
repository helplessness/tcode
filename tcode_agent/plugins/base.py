"""Base abstractions for Tcode plugins."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..tools.base import Tool
from .permissions import PluginPermission


class PluginType(str, Enum):
    TOOL = "tool"
    HOOK = "hook"
    AUTH = "auth"
    PROVIDER = "provider"
    MCP = "mcp"
    OTHER = "other"


@dataclass
class PluginInfo:
    name: str
    version: str
    description: str
    author: Optional[str] = None
    homepage: Optional[str] = None
    plugin_type: list[str] = field(default_factory=list)
    config_model: Optional[str] = None
    permissions: list[PluginPermission] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "plugin_type": list(self.plugin_type),
            "config_model": self.config_model,
            "permissions": [permission.to_dict() for permission in self.permissions],
        }


@dataclass
class PluginContext:
    """Legacy repository-plugin context.

    New v0-style plugins receive the controlled context from
    ``tcode_agent.plugins.context`` through ``start``. This broad mutable
    object remains only as a migration adapter for repository plugins that
    still implement ``initialize``/``after_register``.
    """
    workdir: str
    directory: str
    server_url: str
    config: dict[str, Any] = field(default_factory=dict)
    plugin_config: dict[str, Any] = field(default_factory=dict)
    project: dict[str, Any] = field(default_factory=dict)
    shared_state: dict[str, Any] = field(default_factory=dict)
    permissions: list[PluginPermission] = field(default_factory=list)
    runtime: Any = None
    session_id: Optional[str] = None
    agent_name: Optional[str] = None

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.shared_state.get(key, default)

    def set_state(self, key: str, value: Any):
        self.shared_state[key] = value


LegacyPluginContext = PluginContext


class PluginBase(ABC):
    """Class-style plugin base.

    Plugins may also be plain async functions that return a manifest dict.
    """

    @property
    def info(self) -> PluginInfo:
        raise NotImplementedError()

    async def initialize(self, ctx: PluginContext) -> None:
        return None

    async def after_register(self, ctx: PluginContext) -> None:
        """Called after plugin components are registered in the runtime."""
        return None

    async def shutdown(self) -> None:
        return None

    def get_tools(self) -> list[Tool]:
        return []

    def get_hooks(self) -> dict[str, Any]:
        return {}

    def get_config_model(self):
        return None

    def get_permissions(self) -> list[PluginPermission]:
        return []
