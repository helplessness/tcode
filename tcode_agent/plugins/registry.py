"""Loaded plugin registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..runtime.effects import EffectScope
from ..runtime.registrations import RegistrationDisposer, RegistrationToken
from ..tools.base import Tool
from .base import PluginInfo
from .lifecycle import PluginInstanceStatus
from .permissions import PluginPermission


@dataclass
class LoadedPlugin:
    """保存已发布插件的身份、贡献所有权和运行状态。"""

    name: str
    source: str
    info: PluginInfo
    instance_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    prompt_contributions: list[str] = field(default_factory=list)
    event_subscriptions: list[str] = field(default_factory=list)
    verifier_contributions: list[str] = field(default_factory=list)
    sdk_v1_admitted: bool = False
    execution_mode: str = "controlled-host"
    manifest: Optional[dict[str, Any]] = None
    commands: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    async_target_root: str = ''
    component_errors: list[dict[str, str]] = field(default_factory=list)
    granted_permissions: list[PluginPermission] = field(default_factory=list)
    raw_instance: Optional[Any] = None
    shutdown_handler: Optional[Any] = None
    effect_scope: Optional[EffectScope] = field(default=None, repr=False)
    post_shutdown_effect_scope: Optional[EffectScope] = field(default=None, repr=False)
    runtime_context: Optional[Any] = field(default=None, repr=False)
    controlled_context: Optional[Any] = field(default=None, repr=False)
    instance_status: Optional[PluginInstanceStatus] = field(default=None, repr=False)
    health_handler: Optional[Any] = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """返回前端和 Doctor 可安全使用的脱敏字典。"""

        data = self.info.to_dict()
        data.update(
            {
                "source": self.source,
                "instance_id": self.instance_id,
                "config": {str(key): "[configured]" for key in self.config},
                "tools": [tool.id for tool in self.tools],
                "hooks": self.hooks,
                "prompt_contributions": list(self.prompt_contributions),
                "event_subscriptions": list(self.event_subscriptions),
                "verifier_contributions": list(self.verifier_contributions),
                "sdk_v1_admitted": self.sdk_v1_admitted,
                "execution_mode": self.execution_mode,
                "manifest": self.manifest,
                "commands": list(self.commands),
                "skills": list(self.skills),
                "agents": list(self.agents),
                "workflows": list(self.workflows),
                "mcp_servers": list(self.mcp_servers),
                "component_errors": list(self.component_errors),
                "granted_permissions": [
                    permission.to_dict() for permission in self.granted_permissions
                ],
                "state": (
                    self.instance_status.state.value
                    if self.instance_status is not None
                    else "active"
                ),
                "health": (
                    self.instance_status.health.to_dict()
                    if self.instance_status is not None
                    else {
                        "status": "not_declared",
                        "summary": "lifecycle status is unavailable",
                        "details": {},
                        "checked_at": "",
                    }
                ),
            }
        )
        return data


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class PluginRegistry(metaclass=WorkspaceRegistryMeta):
    _workspace_fields = ('_plugins', '_registrations')

    _plugins: dict[str, LoadedPlugin] = {}
    _registrations: dict[str, RegistrationToken] = {}

    @classmethod
    def register(
        cls,
        plugin: LoadedPlugin,
        *,
        replace: bool = True,
    ) -> RegistrationDisposer:
        if not replace and plugin.name in cls._plugins:
            raise ValueError(f"plugin '{plugin.name}' is already published")
        token = RegistrationToken.create(
            registry="plugins",
            key=plugin.name,
            source=plugin.source,
            instance_id=plugin.instance_id,
        )
        cls._plugins[plugin.name] = plugin
        cls._registrations[plugin.name] = token
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        current = cls._registrations.get(token.key)
        if current != token:
            return False
        cls._plugins.pop(token.key, None)
        cls._registrations.pop(token.key, None)
        return True

    @classmethod
    def unregister(cls, name: str) -> Optional[LoadedPlugin]:
        cls._registrations.pop(name, None)
        return cls._plugins.pop(name, None)

    @classmethod
    def get(cls, name: str) -> Optional[LoadedPlugin]:
        return cls._plugins.get(name)

    @classmethod
    def list_all(cls) -> list[LoadedPlugin]:
        return list(cls._plugins.values())

    @classmethod
    def clear(cls):
        cls._plugins.clear()
        cls._registrations.clear()
