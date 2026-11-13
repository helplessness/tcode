"""TCode SDK v1 entrypoint for the enzyme entity discovery demo."""

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
    register_ontology_package,
)


_PLUGIN_ROOT = Path(__file__).resolve().parent
_TOOLS_ROOT = _PLUGIN_ROOT / "tools"
_TOOLS_PACKAGE_NAME = f"tcode_enzyme_entity_discovery_tools_{uuid.uuid4().hex[:8]}"
_TOOLS_SPEC = importlib.util.spec_from_file_location(
    _TOOLS_PACKAGE_NAME,
    _TOOLS_ROOT / "__init__.py",
    submodule_search_locations=[str(_TOOLS_ROOT)],
)
if _TOOLS_SPEC is None or _TOOLS_SPEC.loader is None:
    raise ImportError("cannot load enzyme entity discovery tool package")
_TOOLS_MODULE = importlib.util.module_from_spec(_TOOLS_SPEC)
sys.modules[_TOOLS_SPEC.name] = _TOOLS_MODULE
try:
    _TOOLS_SPEC.loader.exec_module(_TOOLS_MODULE)
except Exception:
    sys.modules.pop(_TOOLS_SPEC.name, None)
    raise
EntityToolService = _TOOLS_MODULE.EntityToolService


class Plugin:
    """Bind a validated public SQLite release and controlled query tools."""

    info = PluginMetadata(
        name="enzyme-entity-discovery-demo",
        version="0.17.1",
        description="本地酶实体事实、机制片段反事实桥接、候选家族与有界通路发现插件。",
        author="TCode",
        plugin_type=("tool", "workflow"),
    )

    def __init__(self) -> None:
        self._context: PluginContext | None = None
        self._tool_service: Any | None = None
        self._ontology_disposer: Any | None = None

    async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
        """Validate and register the packaged entity database."""

        self._context = ctx
        self._tool_service = EntityToolService(
            ctx,
            dict(config or {}),
            plugin_root=_PLUGIN_ROOT,
            plugin_version=self.info.version,
        )
        summary = self._tool_service.validate_source_installation()
        self._ontology_disposer = register_ontology_package(self.info.name, _PLUGIN_ROOT / "knowledge")
        definitions = self._tool_service.definitions()
        for definition in definitions:
            ctx.tools.register(definition)
        ctx.logger.info(
            "enzyme-entity-discovery-demo %s 已加载；bundle=%s，%s 个实体，%s 个工具",
            self.info.version,
            summary["bundle_digest"],
            summary["entities"],
            len(definitions),
        )

    async def health(self) -> PluginHealth:
        """Report the pinned bundle and registered query surface."""

        if self._context is None or self._tool_service is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        try:
            summary = self._tool_service.validate_source_installation()
        except Exception as exc:
            return PluginHealth(
                PluginHealthStatus.UNHEALTHY,
                "实体事实包校验失败",
                {"error_type": type(exc).__name__},
            )
        return PluginHealth(
            PluginHealthStatus.HEALTHY,
            "公共实体库已校验；工具调用读取只读发布版本",
            {**summary, "tools": len(self._tool_service.definitions()), "offline_only": True},
        )

    async def stop(self, _reason: str = "unload") -> None:
        """Release only process-local indexes; workspace evidence remains auditable."""

        if self._tool_service is not None:
            self._tool_service.close()
        if self._ontology_disposer is not None:
            self._ontology_disposer()
        self._ontology_disposer = None
        self._tool_service = None
        self._context = None
