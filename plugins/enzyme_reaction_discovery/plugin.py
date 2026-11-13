"""TCode SDK v1 entrypoint for enzyme reaction discovery."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tcode_agent.plugin_sdk import (
    PluginContext,
    PluginHealth,
    PluginHealthStatus,
    PluginMetadata,
)


_PLUGIN_ROOT = Path(__file__).resolve().parent
_TOOLS_ROOT = _PLUGIN_ROOT / "tools"
_TOOLS_PACKAGE_NAME = f"tcode_enzyme_reaction_discovery_tools_{uuid.uuid4().hex[:8]}"
_TOOLS_SPEC = importlib.util.spec_from_file_location(
    _TOOLS_PACKAGE_NAME,
    _TOOLS_ROOT / "__init__.py",
    submodule_search_locations=[str(_TOOLS_ROOT)],
)
if _TOOLS_SPEC is None or _TOOLS_SPEC.loader is None:
    raise ImportError("cannot load enzyme reaction discovery tool package")
_TOOLS_MODULE = importlib.util.module_from_spec(_TOOLS_SPEC)
sys.modules[_TOOLS_SPEC.name] = _TOOLS_MODULE
try:
    _TOOLS_SPEC.loader.exec_module(_TOOLS_MODULE)
except Exception:
    sys.modules.pop(_TOOLS_SPEC.name, None)
    raise
EnzymeToolService = _TOOLS_MODULE.EnzymeToolService


class Plugin:
    """Bind the closed library, deterministic tools, and declarative workflow."""

    info = PluginMetadata(
        name="enzyme-reaction-discovery",
        version="3.10.4",
        description="从酶 Wiki 机制片段构建目标事件与反事实桥接的酶逆向发现插件。",
        author="TCode",
        plugin_type=("tool", "workflow"),
    )

    def __init__(self) -> None:
        self._context: PluginContext | None = None
        self._config: dict[str, Any] = {}
        self._tool_service: Any | None = None

    async def start(
        self,
        ctx: PluginContext,
        config: Mapping[str, Any],
    ) -> None:
        """Validate the packaged library and register deterministic public tools."""

        self._context = ctx
        self._config = dict(config or {})
        self._tool_service = EnzymeToolService(
            ctx,
            self._config,
            plugin_root=_PLUGIN_ROOT,
            plugin_version=self.info.version,
        )
        summary = self._tool_service.validate_installation()
        definitions = self._tool_service.definitions()
        for definition in definitions:
            ctx.tools.register(definition)
        ctx.logger.info(
            "enzyme-reaction-discovery %s 已加载；%s 张酶 Wiki、%s 个确定性 Tools 已就绪",
            self.info.version,
            summary["card_count"],
            len(definitions),
        )

    async def health(self) -> PluginHealth:
        """Report whether the public SDK context has been bound."""

        if self._context is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        return PluginHealth(
            PluginHealthStatus.HEALTHY,
            "封闭酶 Wiki 与有界混合检索已加载；embedding 可用性在调用时确认",
            {**self._tool_service.validate_installation(), "tools": len(self._tool_service.definitions()),
             "closed_library_only": True, "retrieval_mode": self._tool_service._retriever.config["mode"],
             "embedding_capability_available": getattr(self._context, "embeddings", None) is not None},
        )

    async def stop(self, _reason: str = "unload") -> None:
        """Release only in-memory lifecycle state."""

        self._context = None
        self._config = {}
        self._tool_service = None
