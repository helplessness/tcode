"""Plugin manager with discovery, lifecycle, and hook integration."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pydantic

from ..agent.agents import BuiltinAgents
from ..config.manager import ConfigManager
from ..guard.deterministic_verifiers import ensure_core_verifiers
from ..guard.verifier_registry import VerifierRegistry
from ..mcp.manager_auto import MCPManager
from ..prompt.registry import PromptRegistry
from ..runtime.context import RuntimeContext, RuntimeIdentity, apply_service_providers
from ..runtime.effects import EffectScope
from ..runtime.service_providers import RUNTIME_SERVICE_KEYS, RuntimeServiceProvider
from ..runtime.plugin_admission import (
    plugin_admission_policy,
    plugin_execution_mode,
    public_workspace_id,
)
from ..skill.manager import SkillManager
from ..team.catalog import TeamCatalog, TeamCatalogEntry
from ..team.routing import TeamRoutingPolicy
from ..types import AgentInfo, AgentMode
from ..tools.base import Tool
from ..tools.registry import ToolRegistry
from ..workflow import WorkflowTemplate, WorkflowTemplateRegistry
from ..plugin_sdk.testing import validate_plugin_package
from .base import PluginBase, PluginContext as LegacyPluginContext, PluginInfo, PluginType
from .command_registry import PluginCommandRegistry, load_commands_from_directory
from .context import (
    PUBLIC_PLUGIN_SERVICE_KEYS,
    PluginContext as ControlledPluginContext,
    PluginContextError,
    PluginServiceUnavailableError,
)
from .contributions import ContributionBuffer, PluginContributionError
from .hooks import HookDispatchMode, HookRegistry
from .isolation import IsolatedPluginWorker, isolation_backend
from .lifecycle import (
    PluginHealth,
    PluginHealthStatus,
    PluginInstanceState,
    PluginInstanceStatus,
    normalize_plugin_health,
)
from .manifest import (
    PluginManifest,
    PluginManifestEntryError,
    PluginManifestError,
    PluginManifestPermissionError,
    PluginManifestSchemaError,
    load_manifest,
    plan_manifest_order,
)
from .permissions import (
    PluginPermission,
    PluginPermissionScope,
    PluginRuntime,
    normalize_patterns,
)
from .registry import LoadedPlugin, PluginRegistry


def _safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_") or "plugin"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class _PreparedPlugin:
    """保存导入任何插件代码前已完成的准入结果。"""

    plugin_config: dict[str, Any]
    plugin_root: Path
    manifest: PluginManifest
    validated_config: dict[str, Any]
    granted_permissions: tuple[PluginPermission, ...]
    execution_mode: str
    workflow_templates: tuple[WorkflowTemplate, ...]


from ..runtime.workspace_registry import bind_plugin_workspace, in_manager_workspace


class PluginManager:
    _instances: dict[tuple[str, str, str], "PluginManager"] = {}
    HEALTH_TIMEOUT_SECONDS = 5.0

    def __init__(self, workdir: str, config_path: Optional[str] = None):
        self.workdir = str(Path(workdir).resolve())
        default_config_path = ConfigManager.get_config_root_file_path(
            "plugins.json",
            self.workdir,
            prefer_effective=True,
        )
        self._inherited_config_path = Path(default_config_path)
        self.config_path = Path(config_path) if config_path else Path(self.workdir) / ".tcode" / "plugins.json"
        self._shared_state: dict[str, Any] = {}
        self._initialized = False
        self._load_task: Optional[asyncio.Task] = None
        self._ensure_task: Optional[asyncio.Task] = None
        self._load_errors: dict[str, str] = {}
        self._load_diagnostics: dict[str, dict[str, str]] = {}
        self._instance_statuses: dict[str, PluginInstanceStatus] = {}

    @classmethod
    async def get_global(
        cls,
        workdir: Optional[str] = None,
        config_path: Optional[str] = None,
        *,
        ensure_configured: bool = True,
    ) -> "PluginManager":
        """返回指定工作区的共享插件管理器。

        首次访问始终完成一次配置加载，保证调用方拿到已经初始化的运行时。
        后续访问只有在 ``ensure_configured`` 为真时才协调外部配置变化；纯查询
        API 会传入假值，避免一次 GET 请求反复导入失败插件或改变运行状态。

        参数:
            workdir: 插件 Context 使用的工作目录。
            config_path: 可选的显式插件配置文件路径。
            ensure_configured: 是否在已初始化后补载尚未进入 Registry 的启用插件。

        返回:
            与工作目录、配置根和显式配置路径绑定的共享管理器。
        """

        resolved = str(Path(workdir or os.getcwd()).resolve())
        bind_plugin_workspace(resolved)
        resolved_config_path = str(Path(config_path).resolve()) if config_path else str(Path(resolved) / ".tcode" / "plugins.json")
        resolved_config_root = ConfigManager.resolve_config_root(resolved)
        cache_key = (resolved, resolved_config_root, resolved_config_path)
        manager = cls._instances.get(cache_key)
        if manager is None:
            manager = cls(resolved, config_path=config_path)
            cls._instances[cache_key] = manager
        if not manager._initialized:
            current_task = asyncio.current_task()
            if manager._load_task is current_task:
                return manager
            if manager._load_task is None or manager._load_task.done():
                manager._load_task = asyncio.create_task(manager.load_all())
            await asyncio.shield(manager._load_task)
        elif ensure_configured:
            await manager.ensure_configured_plugins_loaded()
        return manager

    @classmethod
    def drop_global(cls, workdir: Optional[str] = None):
        resolved = str(Path(workdir or os.getcwd()).resolve())
        keys = [key for key in cls._instances if key[0] == resolved]
        for key in keys:
            cls._instances.pop(key, None)

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> None:
        from ..atomic_storage import atomic_write_json
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)

    def persist_plugin_enabled_state(
        self,
        *,
        name: str,
        enabled: bool,
        path: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        from ..atomic_storage import file_transaction
        with file_transaction(self.config_path):
            return self._persist_plugin_enabled_state(
                name=name, enabled=enabled, path=path, config=config,
            )

    def _persist_plugin_enabled_state(
        self, *, name: str, enabled: bool,
        path: Optional[str] = None, config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        raw = self._read_json_file(self.config_path)
        if not isinstance(raw, dict):
            raw = {}

        plugins = raw.get("plugins")
        if not isinstance(plugins, list):
            plugins = []

        wanted_name = str(name or "").strip()
        wanted_path = str(path or "").strip()
        matched_index: Optional[int] = None
        existing_entry: dict[str, Any] = {}

        for index, item in enumerate(plugins):
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or "").strip().lower()
            item_path = str(item.get("path") or item.get("module") or "").strip()
            if wanted_name and item_name == wanted_name.lower():
                matched_index = index
                existing_entry = dict(item)
                break
            if wanted_path and item_path and item_path == wanted_path:
                matched_index = index
                existing_entry = dict(item)
                break

        merged = dict(existing_entry)
        if not existing_entry:
            inherited = next((item for item in self._load_configured_plugins()
                if str(item.get("name") or "").lower() == wanted_name.lower()), {})
            if inherited:
                inherited_root = self._resolve_plugin_root(inherited)
                merged.update(inherited)
                merged.pop("_source_root", None)
                if inherited_root is not None:
                    merged["path"] = str(inherited_root)
        merged["name"] = wanted_name or str(existing_entry.get("name") or "").strip()
        if wanted_path or not merged.get("path"):
            merged["path"] = wanted_path or str(existing_entry.get("path") or "").strip()
        merged["enabled"] = bool(enabled)
        merged["source"] = "plugins.json"
        if config is not None:
            merged["config"] = dict(config)
        else:
            merged["config"] = dict(merged.get("config") or {})

        if matched_index is None:
            plugins.append(merged)
        else:
            plugins[matched_index] = merged

        raw["plugins"] = plugins
        if "auto_discover" not in raw:
            raw["auto_discover"] = False
        self._write_json_file(self.config_path, raw)
        return merged

    def _build_context(
        self,
        plugin_config: Optional[dict[str, Any]] = None,
        permissions: Optional[list[PluginPermission]] = None,
        *,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> LegacyPluginContext:
        ctx = LegacyPluginContext(
            workdir=self.workdir,
            directory=self.workdir,
            server_url=os.environ.get("TCODE_SERVER_URL", "http://127.0.0.1:5554"),
            config=ConfigManager.reload(self.workdir, emit=False),
            plugin_config=dict(plugin_config or {}),
            project={"workdir": self.workdir},
            shared_state=self._shared_state,
            permissions=list(permissions or []),
            session_id=session_id,
            agent_name=agent_name,
        )
        ctx.runtime = PluginRuntime(
            workdir=self.workdir,
            permissions=ctx.permissions,
            agent_name=ctx.agent_name,
        )
        return ctx

    def _begin_instance(self, name: str) -> PluginInstanceStatus:
        status = PluginInstanceStatus(
            name=str(name or "plugin").strip() or "plugin",
            instance_id=uuid.uuid4().hex,
        )
        self._instance_statuses[status.name] = status
        return status

    def _public_workspace_id(self) -> str:
        return public_workspace_id(self.workdir)

    @staticmethod
    def _desktop_defers_to_remote(prepared: _PreparedPlugin) -> bool:
        """Keep trusted server plugins out of the desktop host process.

        Their tools are exposed by the configured remote Job Protocol MCP.  A
        deferred plugin is an expected desktop topology choice, not a load
        failure and therefore must not appear in ``plugin.runtime`` errors.
        """

        return (
            os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
            and prepared.execution_mode not in {"isolated-worker", "trusted-worker"}
        )

    def _status_for(self, name: str) -> Optional[PluginInstanceStatus]:
        return self._instance_statuses.get(str(name or "").strip())

    def _transition(
        self,
        status: PluginInstanceStatus,
        state: PluginInstanceState,
        *,
        reason: str = "",
        error: Optional[BaseException | str] = None,
    ) -> None:
        status.transition(state, reason=reason, error=error)

    @staticmethod
    def _health_handler(instance: Any) -> Optional[Any]:
        handler = instance.get("health") if isinstance(instance, dict) else getattr(instance, "health", None)
        return handler if callable(handler) else None

    @staticmethod
    def _shutdown_handler(instance: Any) -> Optional[Any]:
        stop = instance.get("stop") if isinstance(instance, dict) else getattr(instance, "stop", None)
        shutdown = instance.get("shutdown") if isinstance(instance, dict) else getattr(instance, "shutdown", None)
        if callable(stop):
            async def _stop(reason: str = "unload") -> None:
                await _maybe_await(stop(reason))

            return _stop
        if callable(shutdown):
            async def _shutdown(_reason: str = "unload") -> None:
                await _maybe_await(shutdown())

            return _shutdown
        return None

    async def _check_health_handler(self, handler: Optional[Any]) -> PluginHealth:
        if handler is None:
            return PluginHealth(
                PluginHealthStatus.NOT_DECLARED,
                "plugin does not declare a health callback",
            )
        try:
            result = await asyncio.wait_for(
                _maybe_await(handler()),
                timeout=self.HEALTH_TIMEOUT_SECONDS,
            )
            return normalize_plugin_health(result)
        except asyncio.TimeoutError:
            return PluginHealth(
                PluginHealthStatus.UNHEALTHY,
                f"health check timed out after {self.HEALTH_TIMEOUT_SECONDS:g}s",
            )
        except Exception as exc:
            return PluginHealth(
                PluginHealthStatus.UNHEALTHY,
                f"health check failed: {type(exc).__name__}: {exc}",
            )

    def _validate_runtime_service_requirements(self, manifest: PluginManifest) -> None:
        available = {key.name for key in PUBLIC_PLUGIN_SERVICE_KEYS}
        missing = sorted(set(manifest.requires) - available)
        if missing:
            raise PluginServiceUnavailableError(
                "required services have no published runtime Provider: "
                + ", ".join(missing)
            )

    def _iter_auto_discovered_plugins(self) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        config_root = Path(ConfigManager.get_effective_config_root(self.workdir))
        plugins_root = config_root / "plugins"
        if plugins_root.is_dir():
            for child in sorted(plugins_root.iterdir()):
                if child.name.startswith((".", "_")):
                    continue
                if (
                    child.is_dir()
                    and (child / "plugin.py").is_file()
                    and (child / "plugin.json").is_file()
                ):
                    discovered.append(
                        {
                            "name": child.name.replace("_", "-"),
                            "path": f"./plugins/{child.name}",
                            "enabled": True,
                            "config": {},
                            "source": "auto_discover",
                        }
                    )
        return discovered

    def _plugin_config_paths(self) -> list[Path]:
        paths: list[Path] = [self.config_path]
        for config_path in ConfigManager.get_config_paths(self.workdir):
            sibling_plugins = Path(config_path).parent / "plugins.json"
            if sibling_plugins not in paths:
                paths.append(sibling_plugins)
        if self.config_path not in paths:
            paths.append(self.config_path)
        if self._inherited_config_path not in paths:
            paths.append(self._inherited_config_path)
        return paths

    def _load_configured_plugins(self) -> list[dict[str, Any]]:
        tcode_cfg = ConfigManager.reload(self.workdir, emit=False)

        configured: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(item: dict[str, Any], *, source_root: Optional[Path] = None):
            name = str(item.get("name") or "").strip()
            path = str(item.get("path") or item.get("module") or "").strip()
            if not name and path:
                name = Path(path).stem if path.endswith(".py") else Path(path).name or path
            if not name:
                return
            key = name.lower()
            if key in seen:
                return
            seen.add(key)
            configured.append(
                {
                    "name": name,
                    "path": path,
                    "enabled": bool(item.get("enabled", True)),
                    "config": dict(item.get("config") or {}),
                    "permissions": dict(item.get("permissions") or {}),
                    "source": str(item.get("source") or "config"),
                    "_source_root": str(source_root or Path(self.workdir)),
                }
            )

        task_snapshot = tcode_cfg.get("__task_snapshot_plugins__", {})
        if ConfigManager.get_active_snapshot_id() and isinstance(task_snapshot, dict):
            snapshot_source_root = Path(
                str(task_snapshot.get("config_root") or self.workdir)
            )
            for item in [
                *list(task_snapshot.get("configured") or []),
                *list(task_snapshot.get("discovered") or []),
            ]:
                if not isinstance(item, dict):
                    continue
                source_root = str(item.get("_source_root") or self.workdir)
                add(item, source_root=Path(source_root))
            # tcode.json may also declare plugins; it is already frozen by
            # ConfigManager and therefore safe to add after the plugins.json set.
            for item in tcode_cfg.get("plugins", []):
                if isinstance(item, dict):
                    add(item, source_root=snapshot_source_root)
            for item in tcode_cfg.get("plugin", []):
                if isinstance(item, str):
                    add(
                        {
                            "name": _safe_name(Path(item).stem or item),
                            "path": item,
                            "enabled": True,
                            "source": "tcode.json:plugin:snapshot",
                        },
                        source_root=snapshot_source_root,
                    )
            return configured

        auto_discover = True
        for config_path in self._plugin_config_paths():
            plugins_cfg = self._read_json_file(config_path)
            if not isinstance(plugins_cfg, dict):
                continue
            auto_discover = bool(plugins_cfg.get("auto_discover", auto_discover))
            source_root = config_path.parent
            configured_root = Path(ConfigManager.resolve_config_root(self.workdir)).resolve()
            project_root_value = str(os.environ.get("TCODE_PROJECT_ROOT", "") or "").strip()
            if project_root_value and config_path.parent.resolve() == configured_root:
                # Private runtime configuration commonly lives in
                # ``<project>/.tcode-local``.  Plugin paths and permission
                # patterns remain project-relative so moving secrets out of the
                # repository root does not silently revoke access to a plugin's
                # own packaged resources.
                source_root = Path(project_root_value).expanduser().resolve()
            elif (
                config_path.parent.resolve() == configured_root
                and configured_root.name == ".tcode-local"
            ):
                # The standard private configuration directory lives directly
                # below the project root.  Keep project-relative plugin paths
                # usable even when a manual server restart forgot to export
                # TCODE_PROJECT_ROOT, but only when at least one configured
                # relative plugin path really exists below that parent.
                inferred_project_root = configured_root.parent.resolve()
                configured_items = [
                    item for item in plugins_cfg.get("plugins", [])
                    if isinstance(item, dict)
                ]
                has_project_relative_plugin = any(
                    bool(plugin_path := str(item.get("path") or item.get("module") or "").strip())
                    and not Path(plugin_path).expanduser().is_absolute()
                    and (inferred_project_root / plugin_path).resolve().exists()
                    for item in configured_items
                )
                if has_project_relative_plugin:
                    source_root = inferred_project_root
            for item in plugins_cfg.get("plugins", []):
                if isinstance(item, dict):
                    add(item, source_root=source_root)

        for item in tcode_cfg.get("plugins", []):
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("source", "tcode.json:plugins")
                add(item, source_root=Path(ConfigManager.get_effective_config_path(self.workdir) or self.workdir).parent)

        for item in tcode_cfg.get("plugin", []):
            if isinstance(item, str):
                add(
                    {"name": _safe_name(Path(item).stem or item), "path": item, "enabled": True, "source": "tcode.json:plugin"},
                    source_root=Path(ConfigManager.get_effective_config_path(self.workdir) or self.workdir).parent,
                )

        if auto_discover:
            for item in self._iter_auto_discovered_plugins():
                add(item, source_root=Path(self.workdir))

        return configured

    @in_manager_workspace
    def list_available_plugins(self) -> list[dict[str, Any]]:
        loaded_names = {plugin.name for plugin in PluginRegistry.list_all()}
        available_map: dict[str, dict[str, Any]] = {}

        def resolve_description(item: dict[str, Any]) -> str:
            name = str(item.get("name") or "").strip() or "plugin"
            plugin_root = self._resolve_plugin_root(item)
            try:
                manifest = load_manifest(plugin_root)
            except PluginManifestError:
                manifest = None
            if manifest is not None and str(manifest.description or "").strip():
                return str(manifest.description).strip()
            source = str(item.get("source") or "config").strip()
            return f"Plugin from {source}."

        # Include configured plugins (including disabled) so the UI can re-load
        # entries that were previously unloaded from plugins.json.
        for item in self._load_configured_plugins():
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            desktop_delivery = "local"
            try:
                plugin_root = self._resolve_plugin_root(item)
                manifest = load_manifest(plugin_root)
                if (
                    os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
                    and manifest.desktop_runtime != "trusted-worker"
                    and plugin_execution_mode(
                        manifest.name, manifest.requested_trust, plugin_root
                    ) != "isolated-worker"
                ):
                    desktop_delivery = "remote"
            except (PluginManifestError, OSError, ValueError, TypeError):
                pass
            remote_state = os.environ.get("TCODE_DESKTOP_REMOTE_STATE", "unconfigured")
            available_map[key] = {
                "name": name,
                "path": str(item.get("path") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
                "source": str(item.get("source") or "config"),
                "description": resolve_description(item),
                "loaded": name in loaded_names,
                "loadable": desktop_delivery == "local",
                "desktop_delivery": desktop_delivery,
                "availability_message": (
                    "由远程计算服务提供"
                    if desktop_delivery == "remote" and remote_state == "configured"
                    else "等待配置远程 Job Protocol"
                    if desktop_delivery == "remote"
                    else ""
                ),
            }

        # Keep auto-discovered plugins visible as fallback discovery entries.
        for item in self._iter_auto_discovered_plugins():
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in available_map:
                continue
            available_map[key] = {
                **item,
                "enabled": bool(item.get("enabled", True)),
                "description": resolve_description(item),
                "loaded": name in loaded_names,
            }

        return sorted(available_map.values(), key=lambda entry: str(entry.get("name") or "").lower())

    def _resolve_source_path(self, source: str) -> Optional[Path]:
        return self._resolve_source_path_with_root(source, base_root=None)

    def _resolve_source_path_with_root(
        self,
        source: str,
        base_root: Optional[Path],
    ) -> Optional[Path]:
        if not source:
            return None
        text = source.removeprefix("file://")
        path = Path(text)
        if not path.is_absolute():
            roots: list[Path] = []
            for candidate in (
                base_root,
                Path(self.workdir),
                Path(str(os.environ.get("TCODE_WORKDIR", "") or "")),
                Path(os.getcwd()),
            ):
                if candidate is None:
                    continue
                try:
                    resolved_root = candidate.expanduser().resolve()
                except Exception:
                    continue
                if str(resolved_root) and resolved_root not in roots:
                    roots.append(resolved_root)

            for root in roots:
                candidate_path = (root / text).resolve()
                if candidate_path.exists():
                    return candidate_path
            return None

        path = path.resolve()
        if path.exists():
            return path
        return None

    def _resolve_plugin_root(self, plugin_config: dict[str, Any]) -> Optional[Path]:
        source = str(
            plugin_config.get("path")
            or plugin_config.get("module")
            or plugin_config.get("name")
            or ""
        )
        source_root = Path(str(plugin_config.get("_source_root") or self.workdir))
        path = self._resolve_source_path_with_root(source, source_root)
        if path is None:
            return None
        return path if path.is_dir() else path.parent

    def _plugin_mcp_source(
        self,
        plugin_name: str,
        instance_id: str = "",
    ) -> str:
        base = f"plugin:{plugin_name}"
        normalized_instance = str(instance_id or "").strip()
        return f"{base}:{normalized_instance}" if normalized_instance else base

    @staticmethod
    def _plugin_transaction_mode() -> str:
        value = os.environ.get("TCODE_PLUGIN_TRANSACTION_MODE", "compat")
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"compat", "enforce"} else "compat"

    def _load_plugin_module(self, source: str, *, source_root: Optional[Path] = None):
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            raise RuntimeError('桌面插件代码不得导入宿主进程')
        path = self._resolve_source_path_with_root(source, source_root)
        if path is None:
            return importlib.import_module(source)

        if path.is_dir():
            module_path = path / "plugin.py"
            if not module_path.is_file():
                module_path = path / "__init__.py"
        else:
            module_path = path

        if not module_path.is_file():
            raise FileNotFoundError(f"plugin entry not found: {source}")

        module_name = f"tcode_plugin_{_safe_name(module_path.stem)}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin module: {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def _instantiate_plugin(
        self,
        module: Any,
        plugin_config: dict[str, Any],
        *,
        entry_symbol: str = "Plugin",
    ) -> Any:
        entry = getattr(module, entry_symbol, None)
        if entry is None:
            raise PluginManifestEntryError(
                f"plugin entry symbol is not exported: {entry_symbol}"
            )

        if inspect.isclass(entry):
            return entry()

        signature = inspect.signature(entry)
        ctx = self._build_context(plugin_config)
        if len(signature.parameters) >= 1:
            result = await _maybe_await(entry(ctx))
        else:
            result = await _maybe_await(entry())
        return result

    def _validate_plugin_config(self, instance: Any, plugin_config: dict[str, Any]) -> dict[str, Any]:
        model = None
        if hasattr(instance, "get_config_model"):
            model = instance.get_config_model()
        elif isinstance(instance, dict):
            model = instance.get("config_model")

        raw_config = dict(plugin_config.get("config") or {})
        if model is None:
            return raw_config
        if inspect.isclass(model) and issubclass(model, pydantic.BaseModel):
            validated = model.model_validate(raw_config)
            return validated.model_dump()
        return raw_config

    def _normalize_info(self, instance: Any, plugin_config: dict[str, Any]) -> PluginInfo:
        raw_info = getattr(instance, "info", None)
        if isinstance(instance, dict):
            raw_info = instance.get("info", raw_info)

        if isinstance(raw_info, PluginInfo):
            info = raw_info
        elif isinstance(raw_info, dict):
            info = PluginInfo(
                name=str(raw_info.get("name") or plugin_config.get("name") or "plugin"),
                version=str(raw_info.get("version") or "0.1.0"),
                description=str(raw_info.get("description") or ""),
                author=raw_info.get("author"),
                homepage=raw_info.get("homepage"),
                plugin_type=[str(item) for item in raw_info.get("plugin_type", [])],
                config_model=raw_info.get("config_model"),
                permissions=[
                    PluginPermission.from_dict(item)
                    for item in raw_info.get("permissions", [])
                    if isinstance(item, dict)
                ],
            )
        elif raw_info is not None and all(
            hasattr(raw_info, field) for field in ("name", "version")
        ):
            info = PluginInfo(
                name=str(getattr(raw_info, "name") or plugin_config.get("name") or "plugin"),
                version=str(getattr(raw_info, "version") or "0.1.0"),
                description=str(getattr(raw_info, "description", "") or ""),
                author=getattr(raw_info, "author", None),
                homepage=getattr(raw_info, "homepage", None),
                plugin_type=[
                    str(item)
                    for item in (getattr(raw_info, "plugin_type", ()) or ())
                ],
                config_model=getattr(raw_info, "config_model", None),
            )
        else:
            info = PluginInfo(
                name=str(plugin_config.get("name") or "plugin"),
                version="0.1.0",
                description="",
                plugin_type=[],
            )

        return info

    def _resolve_granted_permissions(
        self,
        requested: list[PluginPermission],
        plugin_config: dict[str, Any],
        *,
        default_deny: bool = False,
    ) -> list[PluginPermission]:
        override_map = dict(plugin_config.get("permissions") or {})
        source_root = str(plugin_config.get("_source_root") or self.workdir)
        requested_scopes = {permission.scope for permission in requested}
        unknown_overrides = sorted(set(override_map) - requested_scopes)
        if unknown_overrides:
            raise PermissionError(
                "plugin permission override has no Manifest v2 request: "
                + ", ".join(unknown_overrides)
            )
        granted: list[PluginPermission] = []
        for permission in requested:
            override = override_map.get(permission.scope)
            requested_patterns = normalize_patterns(
                permission.scope,
                list(permission.patterns),
                source_root,
            )
            patterns = [] if default_deny and permission.scope not in override_map else list(requested_patterns)
            if override is False:
                patterns = []
            elif isinstance(override, str):
                patterns = [override]
            elif isinstance(override, list):
                patterns = [str(item).strip() for item in override if str(item).strip()]
            elif isinstance(override, dict):
                override_patterns = override.get("patterns") or override.get("allow")
                if isinstance(override_patterns, str):
                    patterns = [override_patterns]
                elif isinstance(override_patterns, list):
                    patterns = [str(item).strip() for item in override_patterns if str(item).strip()]
                if "required" in override:
                    if bool(override.get("required")) != permission.required:
                        raise PermissionError(
                            "plugin permission required flag is Manifest-owned: "
                            f"{permission.scope}"
                        )

            normalized = normalize_patterns(permission.scope, patterns, source_root)
            excess = sorted(set(normalized) - set(requested_patterns))
            if excess:
                raise PermissionError(
                    "plugin permission grant exceeds Manifest v2 request for "
                    f"{permission.scope}: {', '.join(excess)}"
                )
            if permission.required and not normalized:
                raise PermissionError(
                    f"required plugin permission denied: {permission.scope}"
                )
            if normalized:
                granted.append(
                    PluginPermission(
                        scope=permission.scope,
                        patterns=normalized,
                        description=permission.description,
                        required=permission.required,
                    )
                )
        return granted

    def _normalize_tools(self, instance: Any) -> list[Tool]:
        tools: Any = []
        if hasattr(instance, "get_tools"):
            tools = instance.get_tools()
        elif isinstance(instance, dict):
            tools = instance.get("tools", [])

        if isinstance(tools, dict):
            tools = list(tools.values())
        return [tool for tool in tools if isinstance(tool, Tool)]

    @staticmethod
    def _apply_manifest_info(info: PluginInfo, manifest: PluginManifest) -> None:
        if info.name != manifest.name:
            raise PluginManifestEntryError(
                f"entry PluginInfo.name {info.name!r} does not match manifest name "
                f"{manifest.name!r}",
                path=manifest.manifest_path,
            )
        if info.version != manifest.version:
            raise PluginManifestEntryError(
                f"entry PluginInfo.version {info.version!r} does not match manifest "
                f"version {manifest.version!r}",
                path=manifest.manifest_path,
            )
        if info.permissions:
            raise PluginManifestEntryError(
                "runtime PluginInfo.permissions is not supported; declare permissions "
                "only in Manifest v2",
                path=manifest.manifest_path,
            )
        info.description = manifest.description or info.description
        info.author = manifest.author or info.author
        info.homepage = manifest.homepage or info.homepage
        info.permissions = list(manifest.permissions)

    def _resolve_component_paths(
        self,
        plugin_root: Optional[Path],
        entries: list[str],
    ) -> list[Path]:
        if plugin_root is None:
            return []
        resolved: list[Path] = []
        for item in entries:
            candidate = (plugin_root / item).resolve()
            if candidate.exists():
                resolved.append(candidate)
        return resolved

    def _load_plugin_agents(self, paths: list[Path], source: str) -> tuple[list[AgentInfo], list[dict[str, str]]]:
        """
        Load agents from the given paths.
        """
        agents: list[AgentInfo] = []
        errors: list[dict[str, str]] = []
        for base in paths:
            files = [base] if base.is_file() else sorted(base.glob("*.json"))
            for file_path in files:
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        options = dict(item.get("options") or {})
                        options.setdefault("__definition_path__", str(file_path))
                        options.setdefault("__definition_dir__", str(file_path.parent))
                        team_config = item.get("team") or item.get("team_config")
                        if isinstance(team_config, dict):
                            options.setdefault("team", team_config)
                        allowed_subagents = item.get("allowed_subagents", [])
                        if isinstance(allowed_subagents, list):
                            normalized_allowed = [
                                str(agent_name).strip()
                                for agent_name in allowed_subagents
                                if str(agent_name).strip()
                            ]
                            if normalized_allowed:
                                options.setdefault("allowed_subagents", normalized_allowed)
                        mode_value = str(item.get("mode") or AgentMode.SUBAGENT.value)
                        try:
                            mode = AgentMode(mode_value)
                        except Exception:
                            mode = AgentMode.SUBAGENT
                        agents.append(
                            AgentInfo(
                                name=str(item.get("name") or file_path.stem),
                                description=str(item.get("description") or ""),
                                mode=mode,
                                native=False,
                                hidden=bool(item.get("hidden", False)),
                                temperature=item.get("temperature"),
                                top_p=item.get("top_p"),
                                color=item.get("color"),
                                permission=dict(item.get("permission") or {"*": "allow"}),
                                model=item.get("model"),
                                variant=item.get("variant"),
                                prompt=self._resolve_agent_prompt(
                                    item.get("prompt"),
                                    definition_path=file_path,
                                ),
                                options=options,
                                steps=item.get("steps"),
                            )
                        )
                except Exception as exc:
                    errors.append({"component": str(file_path), "error": str(exc)})
        return agents, errors

    def _register_plugin_team_catalog(
        self,
        agents: list[AgentInfo],
        source: str,
        *,
        instance_id: str = "",
        replace: bool = True,
    ) -> list[Any]:
        disposers: list[Any] = []
        for agent in agents:
            team_config = dict((agent.options or {}).get("team") or {})
            if not team_config:
                continue

            def _names(*keys: str) -> tuple[str, ...]:
                values: list[str] = []
                def _append(item: Any) -> None:
                    if isinstance(item, dict):
                        item = item.get("name") or item.get("agent_name") or ""
                    text = str(item or "").strip()
                    if text:
                        values.append(text)

                for key in keys:
                    raw = team_config.get(key)
                    if isinstance(raw, str):
                        _append(raw)
                    elif isinstance(raw, (list, tuple)):
                        for item in raw:
                            _append(item)
                return tuple(values)

            key = str(
                team_config.get("key")
                or team_config.get("team_key")
                or agent.name
            ).strip()
            if not key:
                continue

            display_name = str(
                team_config.get("display_name")
                or (agent.options or {}).get("display_name")
                or agent.name
            ).strip()
            runtime_agent = str(team_config.get("runtime_agent") or agent.name).strip()
            visible_agent = str(
                team_config.get("visible_agent_name")
                or team_config.get("visible_agent")
                or runtime_agent
            ).strip()
            raw_workers = team_config.get("workers")
            workers = tuple(raw_workers) if isinstance(raw_workers, (list, tuple)) else ()

            disposer = TeamCatalog.register(
                TeamCatalogEntry(
                    key=key,
                    display_name=display_name or key,
                    runtime_agent=runtime_agent,
                    visible_agent_name=visible_agent,
                    slug=str(team_config.get("slug") or key).strip() or key,
                    source=source,
                    delegation_reason=str(team_config.get("delegation_reason") or ""),
                    hidden_agent_names=_names("hidden_agent_names", "hidden_agents"),
                    execution_agent_names=_names("execution_agent_names", "execution_agents"),
                    bootstrap_agent_names=_names("bootstrap_agent_names", "bootstrap_agents"),
                    team_mode_agent_names=_names("team_mode_agent_names", "team_mode_agents"),
                    workflow_mode_agent_names=_names("workflow_mode_agent_names", "workflow_mode_agents"),
                    forced_scheduler_agent_names=_names(
                        "forced_scheduler_agent_names",
                        "forced_scheduler_agents",
                    ),
                    mode_hint_visible=str(team_config.get("mode_hint_visible") or ""),
                    mode_hint_runtime=str(team_config.get("mode_hint_runtime") or ""),
                    supports_memory=bool(team_config.get("supports_memory", True)),
                    supports_subagents=bool(team_config.get("supports_subagents", True)),
                    worker_names=_names("worker_names", "visible_worker_names", "workers"),
                    resident_worker_names=_names(
                        "resident_worker_names",
                        "resident_workers",
                    ),
                    disabled_worker_names=_names(
                        "disabled_worker_names",
                        "disabled_workers",
                    ),
                    session_prefixes=_names("session_prefixes", "worker_session_prefixes"),
                    workers=workers,
                ),
                source=source,
                instance_id=instance_id,
                replace_existing=replace,
            )
            disposers.append(disposer)
        return disposers

    def _resolve_agent_prompt(self, value: Any, *, definition_path: Path) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            file_value = str(value.get("file") or "").strip()
            if file_value:
                return self._read_agent_prompt_file(file_value, definition_path=definition_path)
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(".md"):
            return self._read_agent_prompt_file(text, definition_path=definition_path)
        return value

    def _read_agent_prompt_file(self, value: str, *, definition_path: Path) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = (definition_path.parent / path).resolve()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        return f"[prompt file not found: {value}]"

    def _load_plugin_mcp_metadata(self, paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        servers: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for base in paths:
            files = [base] if base.is_file() else sorted(base.glob("*.json"))
            for file_path in files:
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict) and any(
                        key in data for key in ("name", "url", "command", "transport")
                    ):
                        items = [data]
                    elif isinstance(data, dict):
                        items = []
                        for name, item in data.items():
                            if not isinstance(name, str) or name.startswith("$") or not isinstance(item, dict):
                                continue
                            payload = dict(item)
                            payload.setdefault("name", name)
                            items.append(payload)
                    else:
                        items = [data]
                    for item in items:
                        if isinstance(item, dict):
                            payload = dict(item)
                            payload.setdefault("name", file_path.stem)
                            payload.setdefault("source", str(file_path))
                            servers.append(payload)
                except Exception as exc:
                    errors.append({"component": str(file_path), "error": str(exc)})
        return servers, errors

    async def _register_plugin_mcp_servers(
        self,
        plugin_name: str,
        servers: list[dict[str, Any]],
        *,
        instance_id: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        normalized: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []

        for server in servers:
            name = str(server.get("name") or "").strip()
            source = str(server.get("source") or plugin_name)
            if not name:
                errors.append({"component": source, "error": "plugin MCP server is missing a name"})
                continue
            if name in normalized:
                errors.append({"component": source, "error": f"duplicate plugin MCP server name: {name}"})
                continue

            config = {
                key: value
                for key, value in server.items()
                if key not in {"name", "source"}
            }
            # Disabled plugin declarations are documentation/override stubs. Do
            # not let them initialize the process-wide MCP manager and block
            # application startup on unrelated remote servers.
            if config.get("enabled") is False:
                continue
            if not isinstance(config.get("url"), str) or not str(config.get("url") or "").strip():
                errors.append({"component": source, "error": f"plugin MCP server '{name}' is missing url"})
                continue
            normalized[name] = config

        if not normalized:
            return [], errors

        registered, register_errors = await MCPManager.register_servers(
            normalized,
            source=self._plugin_mcp_source(plugin_name, instance_id),
        )
        errors.extend(register_errors)
        return registered, errors

    def _normalize_hooks(self, instance: Any) -> tuple[dict[str, list[Any]], dict[str, list[str]]]:
        hooks: Any = {}
        if hasattr(instance, "get_hooks"):
            hooks = instance.get_hooks()
        elif isinstance(instance, dict):
            hooks = instance.get("hooks", {})

        normalized: dict[str, list[Any]] = {}
        names: dict[str, list[str]] = {}
        if not isinstance(hooks, dict):
            return normalized, names

        for event, handlers in hooks.items():
            if callable(handlers):
                handlers = [handlers]
            elif not isinstance(handlers, (list, tuple)):
                continue
            normalized[event] = [handler for handler in handlers if callable(handler)]
            names[event] = [getattr(handler, "__name__", "hook") for handler in normalized[event]]
        return normalized, names

    def _prepare_plugin(self, plugin_config: dict[str, Any]) -> _PreparedPlugin:
        """在导入前校验 Manifest、配置、权限与宿主信任准入。"""

        plugin_root = self._resolve_plugin_root(plugin_config)
        manifest = load_manifest(plugin_root)
        configured_name = str(plugin_config.get("name") or "").strip()
        if configured_name and configured_name != manifest.name:
            raise PluginManifestSchemaError(
                f"configured plugin name {configured_name!r} does not match manifest "
                f"name {manifest.name!r}",
                path=manifest.manifest_path,
            )
        assert plugin_root is not None
        execution_mode = plugin_execution_mode(
            manifest.name,
            manifest.requested_trust,
            plugin_root,
        )
        if (
            os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
            and execution_mode == "controlled-host"
            and manifest.desktop_runtime == "trusted-worker"
        ):
            execution_mode = "trusted-worker"
        if execution_mode == "quarantined":
            raise PluginManifestPermissionError(
                f"plugin {manifest.name!r} is quarantined and cannot execute",
                path=manifest.manifest_path,
            )
        validated_config = manifest.validate_config(
            dict(plugin_config.get("config") or {})
        )
        workflow_templates: tuple[WorkflowTemplate, ...] = ()
        if manifest.workflows:
            package_errors = validate_plugin_package(plugin_root)
            if package_errors:
                raise PluginManifestSchemaError(
                    "plugin package validation failed before entry import: "
                    + "; ".join(package_errors),
                    path=manifest.manifest_path,
                )
            try:
                workflow_templates = WorkflowTemplateRegistry.load_plugin_templates(
                    plugin_root,
                    manifest.workflows,
                    source=manifest.name,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise PluginManifestSchemaError(
                    f"invalid declarative workflow contribution: {exc}",
                    path=manifest.manifest_path,
                ) from exc
        if execution_mode == "isolated-worker":
            isolated_scopes = {
                PluginPermissionScope.PROCESS_EXEC.value,
                PluginPermissionScope.NETWORK_OUTBOUND.value,
                PluginPermissionScope.TOOLS_INVOKE.value,
                PluginPermissionScope.SKILLS_INVOKE.value,
                PluginPermissionScope.SANDBOX_PROFILE.value,
            }
            unsupported_permissions = sorted(
                permission.scope
                for permission in manifest.permissions
                if permission.scope not in isolated_scopes
            )
            if unsupported_permissions:
                raise PluginManifestPermissionError(
                    "unknown community plugins requested unsupported direct permissions: "
                    + ", ".join(unsupported_permissions),
                    path=manifest.manifest_path,
                )
            if set(manifest.contributes) - {"tools"}:
                raise PluginManifestPermissionError(
                    "isolated community v1 supports tool contributions only",
                    path=manifest.manifest_path,
                )
            if any(
                (
                    manifest.commands,
                    manifest.skills if not manifest.async_targets else (),
                    manifest.agents,
                    manifest.mcp_servers,
                    manifest.requires,
                    manifest.optional,
                )
            ):
                raise PluginManifestPermissionError(
                    "isolated community v1 cannot load executable host components or services; "
                    "declarative workflows remain allowed",
                    path=manifest.manifest_path,
                )
            if manifest.async_targets:
                for component in manifest.skills:
                    for child in (plugin_root / component).rglob('*'):
                        if child.is_symlink() or (child.is_file() and child.name != 'SKILL.md'):
                            raise PluginManifestPermissionError('isolated async plugins may contribute Markdown-only runner skills', path=manifest.manifest_path)
            if isolation_backend() == "unavailable":
                raise PluginManifestPermissionError(
                    "unknown plugin requires a strong isolated worker backend",
                    path=manifest.manifest_path,
                )
            try:
                granted_permissions = self._resolve_granted_permissions(
                    list(manifest.permissions),
                    plugin_config,
                    default_deny=True,
                )
            except PermissionError as exc:
                raise PluginManifestPermissionError(
                    str(exc),
                    path=manifest.manifest_path,
                ) from exc
        else:
            try:
                granted_permissions = self._resolve_granted_permissions(
                    list(manifest.permissions),
                    ({**plugin_config, "_source_root": self.workdir}
                     if manifest.scope == "workspace" and execution_mode == "trusted-worker"
                     else plugin_config),
                )
            except PermissionError as exc:
                raise PluginManifestPermissionError(
                    str(exc),
                    path=manifest.manifest_path,
                ) from exc
        return _PreparedPlugin(
            plugin_config=dict(plugin_config),
            plugin_root=plugin_root.resolve(),
            manifest=manifest,
            validated_config=validated_config,
            granted_permissions=tuple(granted_permissions),
            execution_mode=execution_mode,
            workflow_templates=workflow_templates,
        )

    def _external_manifest_providers(
        self,
        *,
        exclude_names: Optional[set[str]] = None,
    ) -> dict[str, str]:
        """合并宿主原生服务和已加载插件声明的外部 Provider。"""

        providers = {
            service.name: "tcode.runtime-services@1"
            for service in RUNTIME_SERVICE_KEYS
        }
        excluded = exclude_names or set()
        for loaded in PluginRegistry.list_all():
            if loaded.name in excluded or not isinstance(loaded.manifest, dict):
                continue
            for raw_service in loaded.manifest.get("provides", []):
                service = str(raw_service or "").strip()
                if service:
                    providers[service] = loaded.name
        return providers

    def _record_load_error(self, name: str, error: BaseException) -> None:
        self._load_errors[name] = str(error)
        status = self._status_for(name)
        if status is not None:
            if status.state is not PluginInstanceState.FAILED:
                self._transition(
                    status,
                    PluginInstanceState.FAILED,
                    reason="plugin load failed",
                    error=error,
                )
        if isinstance(error, PluginManifestError):
            self._load_diagnostics[name] = error.to_dict()
        elif isinstance(error, (PluginContextError, PluginContributionError)):
            self._load_diagnostics[name] = {
                "category": "composition",
                "code": error.code,
                "message": str(error),
                "path": "",
            }
        else:
            self._load_diagnostics[name] = {
                "category": "runtime",
                "code": "plugin.runtime",
                "message": str(error),
                "path": "",
            }
        if status is not None:
            self._load_diagnostics[name].update(
                {
                    "instance_id": status.instance_id,
                    "state": status.state.value,
                }
            )

    @in_manager_workspace
    async def load_plugin(self, plugin_config: dict[str, Any]) -> Optional[LoadedPlugin]:
        if not plugin_config.get("enabled", True):
            return None
        configured_name = str(
            plugin_config.get("name") or plugin_config.get("path") or "plugin"
        ).strip()
        status = self._begin_instance(configured_name)
        try:
            self._transition(status, PluginInstanceState.VALIDATING)
            prepared = self._prepare_plugin(plugin_config)
            if prepared.manifest.name != status.name:
                self._instance_statuses.pop(status.name, None)
                status.name = prepared.manifest.name
                self._instance_statuses[status.name] = status
            plan_manifest_order(
                [prepared.manifest],
                external_providers=self._external_manifest_providers(
                    exclude_names={prepared.manifest.name}
                ),
            )
            self._validate_runtime_service_requirements(prepared.manifest)
            self._transition(status, PluginInstanceState.RESOLVED)
            loaded = await self._load_prepared_plugin(prepared, status=status)
            if prepared.execution_mode in {'isolated-worker', 'trusted-worker'} and prepared.manifest.mcp_servers:
                paths = self._resolve_component_paths(prepared.plugin_root, list(prepared.manifest.mcp_servers))
                metadata, errors = self._load_plugin_mcp_metadata(paths)
                async def dispose_declared_mcp():
                    await MCPManager.unregister_servers_by_source(self._plugin_mcp_source(loaded.name, loaded.instance_id))
                loaded.effect_scope.add(dispose_declared_mcp, label='declarative-mcp')
                loaded.mcp_servers, registration_errors = await self._register_plugin_mcp_servers(loaded.name, metadata, instance_id=loaded.instance_id)
                loaded.component_errors.extend(errors + registration_errors)
            await self._restore_async_targets(prepared, loaded)
            self._load_errors.pop(status.name, None)
            self._load_diagnostics.pop(status.name, None)
            return loaded
        except Exception as exc:
            self._record_load_error(status.name, exc)
            raise

    async def _restore_async_targets(self, prepared, loaded, *, inactive_only=False):
        if prepared.manifest.async_targets:
            from .async_targets import restore_plugin

            loaded.async_target_root = str(prepared.plugin_root)
            await restore_plugin(self.workdir, loaded, inactive_only=inactive_only)

    async def _load_prepared_plugin(
        self,
        prepared: _PreparedPlugin,
        *,
        status: Optional[PluginInstanceStatus] = None,
    ) -> LoadedPlugin:
        """按预准入模式加载受控主进程或强隔离 Worker 插件。"""

        if (
            os.environ.get('TCODE_DESKTOP_MANAGED') == '1'
            and prepared.execution_mode not in {'isolated-worker', 'trusted-worker'}
        ):
            raise RuntimeError('桌面插件代码不得导入宿主进程；仅支持隔离型插件工具')

        if prepared.execution_mode in {"isolated-worker", "trusted-worker"}:
            return await self._load_isolated_prepared_plugin(
                prepared,
                status=status,
            )
        plugin_config = prepared.plugin_config
        plugin_root = prepared.plugin_root
        manifest = prepared.manifest

        status = status or self._begin_instance(manifest.name)
        if status.state is PluginInstanceState.DISCOVERED:
            self._transition(status, PluginInstanceState.RESOLVED)
        self._transition(status, PluginInstanceState.LOADING)
        instance_id = status.instance_id

        module = self._load_plugin_module(
            str(manifest.entry_path),
            source_root=plugin_root,
        )
        instance = await self._instantiate_plugin(
            module,
            plugin_config,
            entry_symbol=manifest.entry_symbol,
        )
        validated_config = self._validate_plugin_config(instance, plugin_config)
        manifest.validate_config(validated_config)
        info = self._normalize_info(instance, plugin_config)
        self._apply_manifest_info(info, manifest)
        granted_permissions = list(prepared.granted_permissions)
        legacy_ctx: Optional[LegacyPluginContext] = None
        start_handler = instance.get("start") if isinstance(instance, dict) else getattr(instance, "start", None)
        if manifest.contributes and not callable(start_handler):
            raise PluginContributionError(
                "Manifest contributes requires the public start(ctx, config) lifecycle"
            )
        if not callable(start_handler):
            self._transition(status, PluginInstanceState.STARTING)
            legacy_ctx = self._build_context(
                validated_config,
                permissions=granted_permissions,
            )
            if hasattr(instance, "initialize"):
                await _maybe_await(instance.initialize(legacy_ctx))
        tools = self._normalize_tools(instance)
        hooks, hook_names = self._normalize_hooks(instance)
        component_errors: list[dict[str, str]] = []

        command_paths = self._resolve_component_paths(
            plugin_root,
            list(manifest.commands),
        )
        loaded_commands = []
        for command_path in command_paths:
            if command_path.is_dir():
                loaded_commands.extend(load_commands_from_directory(command_path, info.name))

        skill_paths = self._resolve_component_paths(
            plugin_root,
            list(manifest.skills),
        )

        agent_paths = self._resolve_component_paths(
            plugin_root,
            list(manifest.agents),
        )
        loaded_agents, agent_errors = self._load_plugin_agents(agent_paths, info.name)
        component_errors.extend(agent_errors)

        mcp_paths = self._resolve_component_paths(
            plugin_root,
            list(manifest.mcp_servers),
        )
        loaded_mcp_metadata, mcp_errors = self._load_plugin_mcp_metadata(mcp_paths)
        component_errors.extend(mcp_errors)

        if not info.plugin_type:
            plugin_types: list[str] = []
            if tools:
                plugin_types.append(PluginType.TOOL.value)
            if hooks:
                plugin_types.append(PluginType.HOOK.value)
            if not plugin_types:
                plugin_types.append(PluginType.OTHER.value)
            info.plugin_type = plugin_types

        existing = PluginRegistry.get(info.name)
        if existing:
            await self.unload_plugin(existing.name)

        effect_scope = EffectScope(f"plugin:{info.name}:{instance_id}")
        post_shutdown_effect_scope = EffectScope(
            f"plugin:{info.name}:{instance_id}:post-shutdown"
        )
        runtime_context = RuntimeContext(
            RuntimeIdentity(workspace_id=self.workdir),
            name=f"plugin:{info.name}:{instance_id}",
            effects=effect_scope,
        )
        shutdown_handler = self._shutdown_handler(instance)
        health_handler = self._health_handler(instance)
        mcp_source = self._plugin_mcp_source(info.name, instance_id)
        transaction_mode = self._plugin_transaction_mode()
        contribution_buffer = ContributionBuffer(
            allowed=manifest.contributes,
            source=info.name,
            instance_id=instance_id,
            plugin_version=info.version,
            allowed_side_effects=tuple(
                level
                for level, scope in (
                    ("read", PluginPermissionScope.FILESYSTEM_READ.value),
                    ("write", PluginPermissionScope.FILESYSTEM_WRITE.value),
                    ("process", PluginPermissionScope.PROCESS_EXEC.value),
                )
                if any(item.scope == scope for item in granted_permissions)
            ),
            permission_scopes=tuple(
                sorted({item.scope for item in granted_permissions})
            ),
            permission_runtime=PluginRuntime(
                workdir=self.workdir,
                permissions=granted_permissions,
                agent_name=info.name,
            ),
            permission_source_root=str(plugin_root),
            permission_config_root=str(
                plugin_config.get("_source_root") or self.workdir
            ),
        )

        try:
            await apply_service_providers(runtime_context, [RuntimeServiceProvider()])
            controlled_ctx = ControlledPluginContext.create(
                runtime=runtime_context,
                name=info.name,
                version=info.version,
                instance_id=instance_id,
                api_version=manifest.api_version,
                requested_trust=manifest.requested_trust,
                scope=manifest.scope,
                workspace_id=self._public_workspace_id(),
                config=validated_config,
                required=manifest.requires,
                optional=manifest.optional,
                service_keys={key.name: key for key in PUBLIC_PLUGIN_SERVICE_KEYS},
                contribution_buffer=contribution_buffer,
                workdir=self.workdir,
                permissions=tuple(granted_permissions),
                agent_name=info.name,
            )
            missing_required = controlled_ctx.services.missing_required()
            if missing_required:
                raise PluginServiceUnavailableError(
                    "required services are unavailable: " + ", ".join(missing_required)
                )

            self._transition(status, PluginInstanceState.STARTING)
            if callable(start_handler):
                await _maybe_await(start_handler(controlled_ctx, controlled_ctx.config))
                # Public-lifecycle plugins must not regain private Tool/Hook
                # access through legacy contribution methods.
                legacy_tools = self._normalize_tools(instance)
                legacy_hooks, _legacy_hook_names = self._normalize_hooks(instance)
                if legacy_tools or legacy_hooks:
                    raise PluginContributionError(
                        "start() plugins must use ctx.tools/ctx.events instead of "
                        "legacy get_tools/get_hooks contributions"
                    )
            contribution_buffer.seal()
            tools = [*tools, *contribution_buffer.tool_adapters()]
            if info.plugin_type == [PluginType.OTHER.value] and (
                contribution_buffer.tools
                or contribution_buffer.prompts
                or contribution_buffer.events
                or contribution_buffer.verifiers
                or prepared.workflow_templates
            ):
                info.plugin_type = []
            for plugin_type, present in (
                (PluginType.TOOL.value, bool(contribution_buffer.tools)),
                ("prompt", bool(contribution_buffer.prompts)),
                ("event", bool(contribution_buffer.events)),
                ("verifier", bool(contribution_buffer.verifiers)),
                ("workflow", bool(prepared.workflow_templates)),
            ):
                if present and plugin_type not in info.plugin_type:
                    info.plugin_type.append(plugin_type)

            self._transition(status, PluginInstanceState.WARMING)
            initial_health = await self._check_health_handler(health_handler)
            status.set_health(initial_health)
            if initial_health.status is PluginHealthStatus.UNHEALTHY:
                raise RuntimeError(initial_health.summary or "plugin health check failed")

            async with post_shutdown_effect_scope.transaction(
                f"plugin:{info.name}:{instance_id}:mcp"
            ) as mcp_transaction:

                async def _dispose_mcp() -> None:
                    await MCPManager.unregister_servers_by_source(mcp_source)

                # Adopt the cleanup before the first await: a partially
                # connected MCP server is still owned if registration raises.
                mcp_transaction.add(_dispose_mcp, label=f"mcp-source:{mcp_source}")
                active_mcp_servers, mcp_registration_errors = (
                    await self._register_plugin_mcp_servers(
                        info.name,
                        loaded_mcp_metadata,
                        instance_id=instance_id,
                    )
                )
                component_errors.extend(mcp_registration_errors)

                async with effect_scope.transaction(
                    f"plugin:{info.name}:{instance_id}:publish"
                ) as transaction:
                    for workflow_template in prepared.workflow_templates:
                        disposer = WorkflowTemplateRegistry.register_plugin_template(
                            workflow_template,
                            source=info.name,
                            instance_id=instance_id,
                            replace=False,
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"workflow:{workflow_template.template_key}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for tool in tools:
                        disposer = ToolRegistry.register_custom(
                            tool,
                            source=info.name,
                            instance_id=instance_id,
                            replace=False,
                        )
                        transaction.add(
                            disposer,
                            label=f"tool:{tool.id}:{disposer.token.registration_id}",
                        )

                    ensure_core_verifiers()
                    for verifier_definition in contribution_buffer.verifiers:
                        disposer = VerifierRegistry.register_plugin(
                            verifier_definition,
                            source=info.name,
                            instance_id=instance_id,
                            granted_capabilities=tuple(
                                sorted({item.scope for item in granted_permissions})
                            ),
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"verifier:{verifier_definition.verifier_id}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for prompt_contribution in contribution_buffer.prompts:
                        disposer = PromptRegistry.register(
                            prompt_contribution,
                            workspace_id=controlled_ctx.scope.workspace_id,
                            source=info.name,
                            instance_id=instance_id,
                            replace=False,
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"prompt:{prompt_contribution.name}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for subscription in contribution_buffer.events:
                        disposer = HookRegistry.register(
                            event=contribution_buffer.internal_event(subscription.event),
                            name=subscription.name,
                            handler=contribution_buffer.event_handler(subscription),
                            source=info.name,
                            priority=0,
                            instance_id=instance_id,
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"public-event:{subscription.event.value}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )
                    for command in loaded_commands:
                        disposer = PluginCommandRegistry.register(
                            command,
                            source=info.name,
                            instance_id=instance_id,
                            replace=False,
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"command:{command.name}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    def _adopt_skill_disposer(disposer: Any) -> None:
                        transaction.add(
                            disposer,
                            label=(
                                f"{disposer.token.registry}:"
                                f"{disposer.token.key}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for skill_path in skill_paths:
                        if skill_path.is_dir():
                            await SkillManager.register_plugin_directory(
                                str(skill_path),
                                info.name,
                                instance_id=instance_id,
                                replace=False,
                                on_disposer=_adopt_skill_disposer,
                                raise_errors=True,
                            )

                    for agent in loaded_agents:
                        disposer = BuiltinAgents.register_custom(
                            agent,
                            source=info.name,
                            instance_id=instance_id,
                            replace=False,
                        )
                        transaction.add(
                            disposer,
                            label=(
                                f"agent:{agent.name}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for disposer in self._register_plugin_team_catalog(
                        loaded_agents,
                        info.name,
                        instance_id=instance_id,
                        replace=False,
                    ):
                        transaction.add(
                            disposer,
                            label=(
                                f"team:{disposer.token.key}:"
                                f"{disposer.token.registration_id}"
                            ),
                        )

                    for event, handlers in hooks.items():
                        for index, handler in enumerate(handlers):
                            disposer = HookRegistry.register(
                                event=event,
                                name=getattr(handler, "__name__", f"hook_{index}"),
                                handler=handler,
                                source=info.name,
                                priority=0,
                                instance_id=instance_id,
                            )
                            transaction.add(
                                disposer,
                                label=(
                                    f"hook:{disposer.token.event}:"
                                    f"{disposer.token.name}:"
                                    f"{disposer.token.registration_id}"
                                ),
                            )

                    registered_agent_names = list(
                        dict.fromkeys(
                            [
                                *[agent.name for agent in loaded_agents],
                                *[
                                    name
                                    for name, source in BuiltinAgents._custom_agent_sources.items()
                                    if source == info.name
                                ],
                            ]
                        )
                    )
                    loaded = LoadedPlugin(
                        name=info.name,
                        source=str(
                            plugin_config.get("path")
                            or plugin_config.get("module")
                            or plugin_config.get("name")
                            or info.name
                        ),
                        info=info,
                        instance_id=instance_id,
                        config=validated_config,
                        tools=tools,
                        hooks=hook_names,
                        prompt_contributions=[
                            item.name for item in contribution_buffer.prompts
                        ],
                        event_subscriptions=[
                            item.event.value for item in contribution_buffer.events
                        ],
                        verifier_contributions=[
                            item.verifier_id for item in contribution_buffer.verifiers
                        ],
                        sdk_v1_admitted=bool(
                            manifest.contributes or manifest.workflows
                        ),
                        execution_mode=prepared.execution_mode,
                        manifest=manifest.to_dict(),
                        commands=[command.name for command in loaded_commands],
                        skills=[
                            skill.name
                            for skill in SkillManager.all()
                            if skill.plugin == info.name
                        ],
                        agents=registered_agent_names,
                        workflows=[
                            template.template_key
                            for template in prepared.workflow_templates
                        ],
                        mcp_servers=active_mcp_servers,
                        component_errors=component_errors,
                        granted_permissions=granted_permissions,
                        raw_instance=instance,
                        shutdown_handler=shutdown_handler,
                        effect_scope=effect_scope,
                        post_shutdown_effect_scope=post_shutdown_effect_scope,
                        runtime_context=runtime_context,
                        controlled_context=controlled_ctx,
                        instance_status=status,
                        health_handler=health_handler,
                    )
                    registry_disposer = PluginRegistry.register(
                        loaded,
                        replace=False,
                    )
                    transaction.add(
                        registry_disposer,
                        label=(
                            f"plugin-registry:{info.name}:"
                            f"{registry_disposer.token.registration_id}"
                        ),
                    )

                    # At this point every managed contribution is present and
                    # PluginRegistry is the publication barrier. The callback
                    # can never observe a half-populated plugin. Enforce mode
                    # still rolls the complete instance back on callback error.
                    after_register = getattr(instance, "after_register", None)
                    if callable(after_register):
                        try:
                            await _maybe_await(after_register(legacy_ctx))
                        except Exception as exc:
                            if transaction_mode == "enforce":
                                raise RuntimeError(
                                    f"plugin after_register failed: {exc}"
                                ) from exc
                            component_errors.append(
                                {"component": "after_register", "error": str(exc)}
                            )
                    transaction.commit()
                mcp_transaction.commit()
            if component_errors or initial_health.status is PluginHealthStatus.DEGRADED:
                self._transition(
                    status,
                    PluginInstanceState.DEGRADED,
                    reason=(
                        initial_health.summary
                        or f"{len(component_errors)} component error(s)"
                    ),
                )
            else:
                self._transition(status, PluginInstanceState.ACTIVE)
            return loaded
        except BaseException as load_error:
            self._transition(
                status,
                PluginInstanceState.ROLLING_BACK,
                reason="load transaction failed",
                error=load_error,
            )
            cleanup_errors: list[BaseException] = []
            try:
                await runtime_context.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                self._cleanup_plugin_registry_fallbacks(
                    info.name,
                    instance_id=instance_id,
                    legacy_only=True,
                )
            except BaseException as exc:
                cleanup_errors.append(exc)
            if callable(shutdown_handler):
                try:
                    await _maybe_await(shutdown_handler("load_failed"))
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                await post_shutdown_effect_scope.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await MCPManager.unregister_servers_by_source(mcp_source)
            except BaseException as exc:
                cleanup_errors.append(exc)
            if cleanup_errors:
                self._transition(
                    status,
                    PluginInstanceState.FAILED,
                    reason="load rollback completed with cleanup failures",
                    error=load_error,
                )
                raise BaseExceptionGroup(
                    f"plugin '{info.name}' load and rollback failed",
                    [load_error, *cleanup_errors],
                ) from load_error
            self._transition(
                status,
                PluginInstanceState.FAILED,
                reason="load rolled back",
                error=load_error,
            )
            raise

    async def _load_isolated_prepared_plugin(
        self,
        prepared: _PreparedPlugin,
        *,
        status: Optional[PluginInstanceStatus] = None,
    ) -> LoadedPlugin:
        """在独立进程中加载未知社区插件，主进程只发布代理工具。"""

        manifest = prepared.manifest
        plugin_config = prepared.plugin_config
        status = status or self._begin_instance(manifest.name)
        if status.state is PluginInstanceState.DISCOVERED:
            self._transition(status, PluginInstanceState.RESOLVED)
        self._transition(status, PluginInstanceState.LOADING)
        worker = IsolatedPluginWorker(
            manifest=manifest,
            config=prepared.validated_config,
            instance_id=status.instance_id,
            workspace_id=self._public_workspace_id(),
            workdir=self.workdir,
            granted_permissions=prepared.granted_permissions,
            execution_mode=prepared.execution_mode,
        )
        effect_scope = EffectScope(
            f"plugin:{manifest.name}:{status.instance_id}:isolated"
        )
        try:
            self._transition(status, PluginInstanceState.STARTING)
            await worker.start()
            info_data = worker.plugin_info
            info = PluginInfo(
                name=manifest.name,
                version=manifest.version,
                description=str(
                    info_data.get("description") or manifest.description or ""
                ),
                author=str(info_data.get("author") or manifest.author or "") or None,
                homepage=str(
                    info_data.get("homepage") or manifest.homepage or ""
                ) or None,
                plugin_type=[
                    str(item)
                    for item in info_data.get("plugin_type") or [PluginType.TOOL.value]
                ],
            )
            if prepared.workflow_templates and "workflow" not in info.plugin_type:
                info.plugin_type.append("workflow")
            skill_paths = self._resolve_component_paths(
                prepared.plugin_root, list(manifest.skills)
            )
            agent_paths = self._resolve_component_paths(
                prepared.plugin_root, list(manifest.agents)
            )
            loaded_agents, component_errors = self._load_plugin_agents(
                agent_paths, info.name
            )
            contribution_buffer = ContributionBuffer(
                allowed=tuple({*manifest.contributes, "tools"}),
                source=manifest.name,
                instance_id=status.instance_id,
                plugin_version=manifest.version,
                allowed_side_effects=tuple(
                    level
                    for level, scopes in (
                        ("read", {
                            PluginPermissionScope.NETWORK_OUTBOUND.value,
                            PluginPermissionScope.FILESYSTEM_READ.value,
                            PluginPermissionScope.MODEL_EMBEDDING.value,
                        }),
                        ("write", {PluginPermissionScope.FILESYSTEM_WRITE.value}),
                        ("process", {
                            PluginPermissionScope.PROCESS_EXEC.value,
                            PluginPermissionScope.TOOLS_INVOKE.value,
                            PluginPermissionScope.SKILLS_INVOKE.value,
                        }),
                    )
                    if any(
                        permission.scope in scopes
                        for permission in prepared.granted_permissions
                    )
                ),
                permission_scopes=tuple(
                    sorted({item.scope for item in prepared.granted_permissions})
                ),
                permission_runtime=PluginRuntime(
                    workdir=self.workdir,
                    permissions=list(prepared.granted_permissions),
                    agent_name=manifest.name,
                ),
                permission_source_root=str(prepared.plugin_root),
                permission_config_root=str(
                    prepared.plugin_config.get("_source_root") or self.workdir
                ),
            )
            for definition in worker.tool_definitions():
                contribution_buffer.add_tool(definition)
            for contribution in worker.prompt_contributions():
                contribution_buffer.add_prompt(contribution)
            contribution_buffer.seal()
            tools = contribution_buffer.tool_adapters()
            self._transition(status, PluginInstanceState.WARMING)
            initial_health = await self._check_health_handler(worker.health)
            status.set_health(initial_health)
            if initial_health.status is PluginHealthStatus.UNHEALTHY:
                raise RuntimeError(
                    initial_health.summary or "isolated plugin health check failed"
                )
            async with effect_scope.transaction(
                f"plugin:{manifest.name}:{status.instance_id}:publish"
            ) as transaction:
                for workflow_template in prepared.workflow_templates:
                    disposer = WorkflowTemplateRegistry.register_plugin_template(
                        workflow_template,
                        source=manifest.name,
                        instance_id=status.instance_id,
                        replace=False,
                    )
                    transaction.add(
                        disposer,
                        label=(
                            f"isolated-workflow:{workflow_template.template_key}:"
                            f"{disposer.token.registration_id}"
                        ),
                    )
                def _adopt_skill_disposer(disposer: Any) -> None:
                    transaction.add(
                        disposer,
                        label=(
                            f"{disposer.token.registry}:{disposer.token.key}:"
                            f"{disposer.token.registration_id}"
                        ),
                    )

                for skill_path in skill_paths:
                    if skill_path.is_dir():
                        await SkillManager.register_plugin_directory(
                            str(skill_path), info.name,
                            instance_id=status.instance_id,
                            replace=False,
                            on_disposer=_adopt_skill_disposer,
                            raise_errors=True,
                        )
                for agent in loaded_agents:
                    disposer = BuiltinAgents.register_custom(
                        agent,
                        source=info.name,
                        instance_id=status.instance_id,
                        replace=False,
                    )
                    transaction.add(
                        disposer,
                        label=f"agent:{agent.name}:{disposer.token.registration_id}",
                    )
                for disposer in self._register_plugin_team_catalog(
                    loaded_agents, info.name,
                    instance_id=status.instance_id,
                    replace=False,
                ):
                    transaction.add(
                        disposer,
                        label=f"team:{disposer.token.key}:{disposer.token.registration_id}",
                    )
                for tool in tools:
                    disposer = ToolRegistry.register_custom(
                        tool,
                        source=manifest.name,
                        instance_id=status.instance_id,
                        replace=False,
                    )
                    transaction.add(
                        disposer,
                        label=f"isolated-tool:{tool.id}:{disposer.token.registration_id}",
                    )
                for contribution in contribution_buffer.prompts:
                    disposer = PromptRegistry.register(
                        contribution, workspace_id=self._public_workspace_id(),
                        source=manifest.name, instance_id=status.instance_id, replace=False,
                    )
                    transaction.add(disposer, label=f"isolated-prompt:{contribution.name}")
                loaded = LoadedPlugin(
                    name=manifest.name,
                    source=str(
                        plugin_config.get("path")
                        or plugin_config.get("module")
                        or manifest.name
                    ),
                    info=info,
                    instance_id=status.instance_id,
                    config=dict(prepared.validated_config),
                    tools=tools,
                    sdk_v1_admitted=True,
                    execution_mode=prepared.execution_mode,
                    manifest=manifest.to_dict(),
                    workflows=[
                        template.template_key
                        for template in prepared.workflow_templates
                    ],
                    skills=[
                        skill.name for skill in SkillManager.all()
                        if skill.plugin == info.name
                    ],
                    agents=[agent.name for agent in loaded_agents],
                    component_errors=component_errors,
                    granted_permissions=list(prepared.granted_permissions),
                    raw_instance=worker,
                    shutdown_handler=worker.stop,
                    prompt_contributions=[item.name for item in contribution_buffer.prompts],
                    effect_scope=effect_scope,
                    instance_status=status,
                    health_handler=worker.health,
                )
                registry_disposer = PluginRegistry.register(
                    loaded,
                    replace=False,
                )
                transaction.add(
                    registry_disposer,
                    label=(
                        f"isolated-plugin-registry:{manifest.name}:"
                        f"{registry_disposer.token.registration_id}"
                    ),
                )
                transaction.commit()
            self._transition(
                status,
                PluginInstanceState.DEGRADED
                if initial_health.status is PluginHealthStatus.DEGRADED
                else PluginInstanceState.ACTIVE,
                reason=(
                    initial_health.summary
                    if initial_health.status is PluginHealthStatus.DEGRADED
                    else ""
                ),
            )
            return loaded
        except BaseException as load_error:
            self._transition(
                status,
                PluginInstanceState.ROLLING_BACK,
                reason="isolated load transaction failed",
                error=load_error,
            )
            cleanup_errors: list[BaseException] = []
            try:
                await effect_scope.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await worker.stop("load_failed")
            except BaseException as exc:
                cleanup_errors.append(exc)
            self._transition(
                status,
                PluginInstanceState.FAILED,
                reason=(
                    "isolated rollback completed with cleanup failures"
                    if cleanup_errors
                    else "isolated load rolled back"
                ),
                error=load_error,
            )
            if cleanup_errors:
                raise BaseExceptionGroup(
                    f"isolated plugin '{manifest.name}' load and rollback failed",
                    [load_error, *cleanup_errors],
                ) from load_error
            raise

    @in_manager_workspace
    async def load_all(self) -> list[LoadedPlugin]:
        plugins: list[LoadedPlugin] = []
        self._load_errors = {}
        self._load_diagnostics = {}
        prepared_by_name: dict[str, _PreparedPlugin] = {}
        for plugin_config in self._load_configured_plugins():
            if not plugin_config.get("enabled", True):
                continue
            configured_name = str(
                plugin_config.get("name")
                or plugin_config.get("path")
                or "plugin"
            )
            status = self._begin_instance(configured_name)
            try:
                self._transition(status, PluginInstanceState.VALIDATING)
                prepared = self._prepare_plugin(plugin_config)
                if self._desktop_defers_to_remote(prepared):
                    self._instance_statuses.pop(status.name, None)
                    self._load_errors.pop(status.name, None)
                    self._load_diagnostics.pop(status.name, None)
                    continue
                prepared_by_name[prepared.manifest.name] = prepared
                self._transition(status, PluginInstanceState.RESOLVED)
            except Exception as exc:
                self._record_load_error(configured_name, exc)

        ordered: tuple[PluginManifest, ...] = ()
        if prepared_by_name:
            try:
                ordered = plan_manifest_order(
                    [item.manifest for item in prepared_by_name.values()],
                    external_providers=self._external_manifest_providers(
                        exclude_names=set(prepared_by_name)
                    ),
                )
            except Exception as exc:
                for name in prepared_by_name:
                    self._record_load_error(name, exc)

        for manifest in ordered:
            prepared = prepared_by_name[manifest.name]
            try:
                # Re-check against providers that actually reached publication.
                # A runtime failure in an earlier provider must prevent dependent
                # plugin code from being imported.
                plan_manifest_order(
                    [manifest],
                    external_providers=self._external_manifest_providers(
                        exclude_names={manifest.name}
                    ),
                )
                self._validate_runtime_service_requirements(manifest)
                loaded = await self._load_prepared_plugin(
                    prepared,
                    status=self._status_for(manifest.name),
                )
                # Repair approvals left inactive by older shutdown behavior.
                await self._restore_async_targets(prepared, loaded, inactive_only=True)
                plugins.append(loaded)
                self._load_errors.pop(manifest.name, None)
                self._load_diagnostics.pop(manifest.name, None)
            except Exception as exc:
                self._record_load_error(manifest.name, exc)

        self._initialized = True
        await self.trigger(
            "config.loaded",
            {"workdir": self.workdir},
            {
                "plugins": [plugin.name for plugin in plugins],
                "errors": dict(self._load_errors),
            },
        )
        return plugins

    def _repair_plugin_team_catalog_from_registered_agents(self) -> None:
        grouped: dict[str, list[AgentInfo]] = {}
        custom_agents = getattr(BuiltinAgents, "_custom_agents", {}) or {}
        custom_sources = getattr(BuiltinAgents, "_custom_agent_sources", {}) or {}
        for name, agent in custom_agents.items():
            if not isinstance((agent.options or {}).get("team"), dict):
                continue
            source = str(custom_sources.get(name) or (agent.options or {}).get("plugin") or "plugin").strip()
            if not source:
                source = "plugin"
            grouped.setdefault(source, []).append(agent)

        for source, agents in grouped.items():
            self._register_plugin_team_catalog(agents, source)

    @in_manager_workspace
    async def ensure_configured_plugins_loaded(self) -> None:
        current_task = asyncio.current_task()
        if self._load_task is current_task or self._ensure_task is current_task:
            return
        if self._ensure_task is not None and not self._ensure_task.done():
            await self._ensure_task
            return

        async def _ensure() -> None:
            configured = self._load_configured_plugins()
            loaded_names = {plugin.name for plugin in PluginRegistry.list_all()}
            prepared_by_name: dict[str, _PreparedPlugin] = {}
            for plugin_config in configured:
                if not plugin_config.get("enabled", True):
                    continue
                configured_name = str(
                    plugin_config.get("name")
                    or plugin_config.get("path")
                    or plugin_config.get("module")
                    or "plugin"
                ).strip()
                if configured_name in loaded_names:
                    continue
                try:
                    status = self._begin_instance(configured_name)
                    self._transition(status, PluginInstanceState.VALIDATING)
                    prepared = self._prepare_plugin(plugin_config)
                    if self._desktop_defers_to_remote(prepared):
                        self._instance_statuses.pop(status.name, None)
                        self._load_errors.pop(status.name, None)
                        self._load_diagnostics.pop(status.name, None)
                        continue
                    prepared_by_name[prepared.manifest.name] = prepared
                    self._transition(status, PluginInstanceState.RESOLVED)
                except Exception as exc:
                    self._record_load_error(configured_name, exc)

            ordered: tuple[PluginManifest, ...] = ()
            if prepared_by_name:
                try:
                    ordered = plan_manifest_order(
                        [item.manifest for item in prepared_by_name.values()],
                        external_providers=self._external_manifest_providers(),
                    )
                except Exception as exc:
                    for name in prepared_by_name:
                        self._record_load_error(name, exc)

            for manifest in ordered:
                try:
                    plan_manifest_order(
                        [manifest],
                        external_providers=self._external_manifest_providers(
                            exclude_names={manifest.name}
                        ),
                    )
                    self._validate_runtime_service_requirements(manifest)
                    prepared = prepared_by_name[manifest.name]
                    loaded = await self._load_prepared_plugin(
                        prepared,
                        status=self._status_for(manifest.name),
                    )
                    await self._restore_async_targets(prepared, loaded, inactive_only=True)
                    loaded_names.add(loaded.name)
                    self._load_errors.pop(manifest.name, None)
                    self._load_diagnostics.pop(manifest.name, None)
                except Exception as exc:
                    self._record_load_error(manifest.name, exc)

            self._repair_plugin_team_catalog_from_registered_agents()

        self._ensure_task = asyncio.create_task(_ensure())
        await self._ensure_task

    def _cleanup_plugin_registry_fallbacks(
        self,
        source: str,
        *,
        instance_id: str = "",
        legacy_only: bool,
    ) -> dict[str, list[str]]:
        """收敛实例级所有权，再按需清理没有令牌的旧来源贡献。"""

        removed: dict[str, list[str]] = {
            "tools": [],
            "commands": [],
            "skills": [],
            "agents": [],
            "teams": [],
            "routing": [],
            "hooks": [],
            "prompts": [],
            "workflows": [],
            "verifiers": [],
        }
        if instance_id:
            removed["tools"].extend(
                ToolRegistry.unregister_custom_by_instance(instance_id)
            )
            removed["commands"].extend(
                PluginCommandRegistry.unregister_by_instance(instance_id)
            )
            removed["skills"].extend(SkillManager.unregister_by_instance(instance_id))
            removed["agents"].extend(
                BuiltinAgents.unregister_custom_by_instance(instance_id)
            )
            removed["teams"].extend(TeamCatalog.unregister_by_instance(instance_id))
            removed["routing"].extend(
                TeamRoutingPolicy.unregister_by_instance(instance_id)
            )
            hook_count = HookRegistry.unregister_by_instance(instance_id)
            if hook_count:
                removed["hooks"].append(str(hook_count))
            removed["prompts"].extend(
                PromptRegistry.unregister_by_instance(instance_id)
            )
            removed["workflows"].extend(
                WorkflowTemplateRegistry.unregister_by_instance(instance_id)
            )
            removed["verifiers"].extend(
                VerifierRegistry.unregister_by_instance(instance_id)
            )

        removed["tools"].extend(
            ToolRegistry.unregister_custom_by_source(source, legacy_only=legacy_only)
        )
        removed["commands"].extend(
            PluginCommandRegistry.unregister_by_source(source, legacy_only=legacy_only)
        )
        removed["skills"].extend(
            SkillManager.unregister_by_source(source, legacy_only=legacy_only)
        )
        removed["agents"].extend(
            BuiltinAgents.unregister_custom_by_source(source, legacy_only=legacy_only)
        )
        removed["teams"].extend(
            TeamCatalog.unregister_by_source(source, legacy_only=legacy_only)
        )
        removed["routing"].extend(
            TeamRoutingPolicy.unregister_by_source(source, legacy_only=legacy_only)
        )
        hook_count = HookRegistry.unregister_by_source(
            source,
            legacy_only=legacy_only,
        )
        if hook_count:
            removed["hooks"].append(str(hook_count))
        removed["prompts"].extend(
            PromptRegistry.unregister_by_source(source, legacy_only=legacy_only)
        )
        removed["workflows"].extend(
            WorkflowTemplateRegistry.unregister_by_source(
                source,
                legacy_only=legacy_only,
            )
        )
        removed["verifiers"].extend(
            VerifierRegistry.unregister_by_source(
                source,
                legacy_only=legacy_only,
            )
        )
        return removed

    @in_manager_workspace
    async def unload_plugin(self, name: str, *, shutdown: bool = False) -> bool:
        loaded = PluginRegistry.get(name)
        if loaded is None:
            return False
        if not shutdown and (loaded.manifest or {}).get('asyncTargets'):
            from .async_targets import deactivate_plugin
            # Stop new submissions before removing skills/registrations.
            await deactivate_plugin(self.workdir, name)
        status = loaded.instance_status or self._status_for(name)
        if status is not None:
            self._transition(
                status,
                PluginInstanceState.DRAINING,
                reason="plugin unload requested",
            )
        loaded = PluginRegistry.unregister(name)
        assert loaded is not None
        cleanup_errors: list[BaseException] = []

        if loaded.effect_scope is None:
            try:
                self._cleanup_plugin_registry_fallbacks(
                    name,
                    legacy_only=False,
                )
            except BaseException as exc:
                cleanup_errors.append(exc)
        else:
            try:
                if loaded.runtime_context is not None:
                    await loaded.runtime_context.close()
                else:
                    await loaded.effect_scope.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                self._cleanup_plugin_registry_fallbacks(
                    name,
                    instance_id=loaded.instance_id,
                    legacy_only=True,
                )
            except BaseException as exc:
                cleanup_errors.append(exc)

        if loaded.shutdown_handler:
            try:
                await _maybe_await(loaded.shutdown_handler("unload"))
            except BaseException as exc:
                cleanup_errors.append(exc)

        if status is not None:
            self._transition(
                status,
                PluginInstanceState.STOPPED,
                reason="plugin stop callback and owned effects completed",
            )

        if loaded.post_shutdown_effect_scope is not None:
            try:
                await loaded.post_shutdown_effect_scope.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
            mcp_sources = [
                self._plugin_mcp_source(name, loaded.instance_id),
                self._plugin_mcp_source(name),
            ]
        else:
            mcp_sources = [self._plugin_mcp_source(name)]
        for mcp_source in dict.fromkeys(mcp_sources):
            try:
                await MCPManager.unregister_servers_by_source(mcp_source)
            except BaseException as exc:
                cleanup_errors.append(exc)

        if cleanup_errors:
            if status is not None:
                self._transition(
                    status,
                    PluginInstanceState.FAILED,
                    reason="plugin unload completed with cleanup failures",
                    error=cleanup_errors[0],
                )
            raise BaseExceptionGroup(
                f"plugin '{name}' unload cleanup failed",
                cleanup_errors,
            )
        if status is not None:
            self._transition(status, PluginInstanceState.UNLOADED)
        return True

    @in_manager_workspace
    async def unload_all(self, *, shutdown: bool = False):
        for plugin in list(PluginRegistry.list_all()):
            await self.unload_plugin(plugin.name, shutdown=shutdown)
        self._initialized = False

    @in_manager_workspace
    async def reload_all(self) -> list[LoadedPlugin]:
        await self.unload_all()
        return await self.load_all()

    @in_manager_workspace
    async def trigger(
        self,
        event: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        *,
        mode: str | HookDispatchMode = HookDispatchMode.SERIAL,
    ) -> dict[str, Any]:
        return await HookRegistry.dispatch(
            event,
            input_data=input_data,
            output_data=output_data,
            mode=mode,
        )

    @in_manager_workspace
    async def refresh_plugin_health(self, name: str) -> dict[str, Any]:
        loaded = PluginRegistry.get(name)
        if loaded is None:
            status = self._status_for(name)
            if status is None:
                raise KeyError(f"plugin not found: {name}")
            return status.to_dict()
        status = loaded.instance_status or self._status_for(name)
        if status is None:
            status = self._begin_instance(name)
            status.instance_id = loaded.instance_id
            loaded.instance_status = status
        health = await self._check_health_handler(loaded.health_handler)
        # Ignore a late health result after concurrent unload/replacement.
        if PluginRegistry.get(name) is not loaded:
            return status.to_dict()
        status.set_health(health)
        if health.status is PluginHealthStatus.HEALTHY:
            if loaded.component_errors:
                self._transition(
                    status,
                    PluginInstanceState.DEGRADED,
                    reason=f"{len(loaded.component_errors)} component error(s) remain",
                )
            else:
                self._transition(
                    status,
                    PluginInstanceState.ACTIVE,
                    reason="health check recovered",
                )
        elif health.status in {
            PluginHealthStatus.DEGRADED,
            PluginHealthStatus.UNHEALTHY,
        }:
            self._transition(
                status,
                PluginInstanceState.DEGRADED,
                reason=health.summary or health.status.value,
            )
        return status.to_dict()

    @in_manager_workspace
    async def refresh_all_plugin_health(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for loaded in list(PluginRegistry.list_all()):
            reports.append(await self.refresh_plugin_health(loaded.name))
        return reports

    def get_instance_statuses(self) -> dict[str, dict[str, Any]]:
        return {
            name: status.to_dict()
            for name, status in sorted(self._instance_statuses.items())
        }

    def get_load_errors(self) -> dict[str, str]:
        return dict(self._load_errors)

    def get_load_diagnostics(self) -> dict[str, dict[str, str]]:
        return {name: dict(value) for name, value in self._load_diagnostics.items()}

    def get_doctor_report(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        admission_policy = plugin_admission_policy()
        configured = {item["name"]: item for item in self._load_configured_plugins()}
        loaded_map = {plugin.name: plugin for plugin in PluginRegistry.list_all()}
        for name, plugin_config in configured.items():
            plugin_root = self._resolve_plugin_root(plugin_config)
            manifest_exists = bool(plugin_root and (plugin_root / "plugin.json").is_file())
            loaded = loaded_map.get(name)
            manifest_data: dict[str, Any] = {}
            manifest_valid = False
            manifest_diagnostic: dict[str, str] = {}
            if loaded and isinstance(loaded.manifest, dict):
                manifest_data = dict(loaded.manifest)
                manifest_valid = True
            else:
                try:
                    manifest_data = load_manifest(plugin_root).to_dict()
                    manifest_valid = True
                except PluginManifestError as exc:
                    manifest_diagnostic = exc.to_dict()
            dependency_checks = []
            permission_items = []
            if loaded:
                permission_items = [item.to_dict() for item in loaded.granted_permissions]
                for permission in loaded.granted_permissions:
                    if permission.scope == "process.exec":
                        for pattern in permission.patterns:
                            dependency_checks.append(
                                {
                                    "name": pattern,
                                    "installed": shutil.which(pattern) is not None,
                                    "path": shutil.which(pattern),
                                }
                            )
            reports.append(
                {
                    "name": name,
                    "configured": True,
                    "loaded": loaded is not None,
                    "path": str(plugin_root) if plugin_root else str(plugin_config.get("path") or ""),
                    "manifest": manifest_exists,
                    "manifest_valid": manifest_valid,
                    "api_version": str(manifest_data.get("apiVersion") or ""),
                    "scope": str(manifest_data.get("scope") or ""),
                    "requested_trust": str(
                        manifest_data.get("requestedTrust") or ""
                    ),
                    "provides": list(manifest_data.get("provides") or []),
                    "requires": list(manifest_data.get("requires") or []),
                    "optional": list(manifest_data.get("optional") or []),
                    "contributes": list(manifest_data.get("contributes") or []),
                    "sdk_v1_required": bool(manifest_data.get("contributes") or []),
                    "execution_mode": (
                        loaded.execution_mode
                        if loaded is not None
                        else plugin_execution_mode(
                            name,
                            str(manifest_data.get("requestedTrust") or "quarantined"),
                            plugin_root or self.workdir,
                        )
                        if manifest_valid
                        else "quarantined"
                    ),
                    "sdk_v1_admitted": bool(loaded and loaded.sdk_v1_admitted),
                    "prompt_consumer": admission_policy["prompt_consumer"],
                    "manifest_diagnostic": manifest_diagnostic,
                    "commands": len(loaded.commands) if loaded else 0,
                    "skills": len(loaded.skills) if loaded else 0,
                    "agents": len(loaded.agents) if loaded else 0,
                    "tools": len(loaded.tools) if loaded else 0,
                    "hooks": sum(len(items) for items in (loaded.hooks.values() if loaded else [])),
                    "mcp_servers": len(loaded.mcp_servers) if loaded else 0,
                    "mcp_details": list(loaded.mcp_servers) if loaded else [],
                    "permissions": permission_items,
                    "dependencies": dependency_checks,
                    "load_error": self._load_errors.get(name, ""),
                    "load_diagnostic": dict(
                        self._load_diagnostics.get(name) or {}
                    ),
                    "component_errors": loaded.component_errors if loaded else [],
                    "instance": (
                        loaded.instance_status.to_dict()
                        if loaded and loaded.instance_status is not None
                        else (
                            self._instance_statuses[name].to_dict()
                            if name in self._instance_statuses
                            else {}
                        )
                    ),
                }
            )
        return reports


async def create_plugin_manager(
    workdir: str, config_path: Optional[str] = None
) -> PluginManager:
    manager = PluginManager(workdir=workdir, config_path=config_path)
    await manager.load_all()
    PluginManager._instances[str(Path(workdir).resolve())] = manager
    return manager
