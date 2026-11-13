"""Configuration manager for tcode.json"""

import copy
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class ActiveConfigBinding:
    """One task-scoped immutable configuration binding."""

    snapshot_id: str
    workdir: str
    config: dict[str, Any]


class ConfigManager:
    """Manages merged tcode.json configuration."""

    _config: dict[str, Any] = {}
    _loaded: bool = False
    _config_path: Optional[str] = None
    _config_paths: list[str] = []
    _loaded_workdir: Optional[str] = None
    _loaded_config_root: Optional[str] = None
    _active_binding: ContextVar[Optional[ActiveConfigBinding]] = ContextVar(
        "tcode_active_config_binding",
        default=None,
    )

    @classmethod
    def _config_view(cls) -> dict[str, Any]:
        binding = cls._active_binding.get()
        return binding.config if binding is not None else cls._config

    @classmethod
    def get_active_snapshot_id(cls) -> str:
        """Return the snapshot bound to the current async execution context."""

        binding = cls._active_binding.get()
        return binding.snapshot_id if binding is not None else ""

    @classmethod
    def get_active_snapshot_workdir(cls) -> str:
        binding = cls._active_binding.get()
        return binding.workdir if binding is not None else ""

    @classmethod
    @contextmanager
    def bind_snapshot(
        cls,
        snapshot_id: str,
        config: dict[str, Any],
        *,
        workdir: str,
    ):
        """Pin all ConfigManager reads in this context to one task snapshot."""

        frozen_copy = copy.deepcopy(dict(config or {}))
        binding = ActiveConfigBinding(
            snapshot_id=str(snapshot_id or "").strip(),
            workdir=cls._normalize_dir(workdir),
            config=frozen_copy,
        )
        token = cls._active_binding.set(binding)
        try:
            yield MappingProxyType(frozen_copy)
        finally:
            cls._active_binding.reset(token)

    @classmethod
    def _normalize_dir(cls, value: Optional[str], fallback: Optional[str] = None) -> str:
        base = str(value or fallback or os.getcwd()).strip() or os.getcwd()
        normalized = os.path.abspath(os.path.expanduser(base))
        if os.path.isfile(normalized):
            normalized = os.path.dirname(normalized)
        return normalized

    @classmethod
    def resolve_config_root(cls, workdir: str = None) -> str:
        explicit = str(os.environ.get("TCODE_CONFIG_ROOT", "") or "").strip()
        return cls._normalize_dir(explicit or workdir, fallback=os.getcwd())

    @classmethod
    def _collect_config_paths(cls, workdir: str) -> list[str]:
        current = cls.resolve_config_root(workdir)
        if os.environ.get("TCODE_DESKTOP_TEST_INSTANCE") == "1":
            candidate = os.path.join(current, "tcode.json")
            return [candidate] if os.path.isfile(candidate) else []
        directories: list[str] = []
        while True:
            directories.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

        config_paths: list[str] = []
        for directory in reversed(directories):
            candidate = os.path.join(directory, "tcode.json")
            if os.path.exists(candidate):
                config_paths.append(candidate)
        return config_paths

    @classmethod
    def _find_config_path(cls, workdir: str) -> Optional[str]:
        paths = cls._collect_config_paths(workdir)
        if paths:
            return paths[-1]
        return None

    @classmethod
    def _load_local_model_override(
        cls,
        workdir: str,
        config_paths: list[str],
    ) -> Optional[str]:
        """Load a workspace-local model without replacing the core config root.

        ``TCODE_CONFIG_ROOT`` intentionally pins providers, plugins, and other
        trusted configuration to the server project.  The web model selector,
        however, persists a per-session choice to ``<workdir>/tcode.json``.
        Only merge that local ``model`` field so the choice can take effect
        without allowing the workspace file to override the rest of the core
        configuration.
        """
        explicit_root = str(os.environ.get("TCODE_CONFIG_ROOT", "") or "").strip()
        if not explicit_root:
            return None

        local_path = cls.get_local_config_path(workdir)
        normalized_paths = {os.path.abspath(path) for path in config_paths}
        if os.path.abspath(local_path) in normalized_paths or not os.path.isfile(local_path):
            return None

        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        value = data.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @classmethod
    def _deep_merge(cls, base: Any, override: Any) -> Any:
        if isinstance(base, dict) and isinstance(override, dict):
            merged = {key: copy.deepcopy(value) for key, value in base.items()}
            for key, value in override.items():
                if key in merged:
                    merged[key] = cls._deep_merge(merged[key], value)
                else:
                    merged[key] = copy.deepcopy(value)
            return merged
        return copy.deepcopy(override)

    @classmethod
    def get_config_paths(cls, workdir: str = None) -> list[str]:
        if workdir is None:
            workdir = os.getcwd()
        return cls._collect_config_paths(workdir)

    @classmethod
    def get_local_config_path(cls, workdir: str = None) -> str:
        if workdir is None:
            workdir = os.getcwd()
        return os.path.join(cls._normalize_dir(workdir), "tcode.json")

    @classmethod
    def get_config_root_file_path(
        cls,
        filename: str,
        workdir: str = None,
        *,
        prefer_effective: bool = True,
    ) -> str:
        name = str(filename or "").strip()
        if not name:
            raise ValueError("filename is required")

        base_dir = cls.resolve_config_root(workdir)
        if prefer_effective:
            effective = cls.get_effective_config_path(workdir)
            if effective:
                base_dir = os.path.dirname(effective)
        return os.path.join(base_dir, name)

    @classmethod
    def get_effective_config_root(cls, workdir: str = None) -> str:
        effective = cls.get_effective_config_path(workdir)
        if effective:
            return os.path.dirname(effective)
        return cls.resolve_config_root(workdir)

    @classmethod
    def get_primary_config_path(cls, workdir: str = None) -> Optional[str]:
        if workdir is None:
            workdir = os.getcwd()
        paths = cls._collect_config_paths(workdir)
        if paths:
            return paths[0]
        return None

    @classmethod
    def get_effective_config_path(cls, workdir: str = None) -> Optional[str]:
        if workdir is None:
            workdir = os.getcwd()
        return cls._find_config_path(workdir)

    @classmethod
    def get_loaded_workdir(cls) -> Optional[str]:
        return cls._loaded_workdir

    @classmethod
    def get_loaded_config_root(cls) -> Optional[str]:
        return cls._loaded_config_root

    @classmethod
    def get_loaded_config_paths(cls) -> list[str]:
        return list(cls._config_paths)

    @classmethod
    def get_config_copy(cls) -> dict[str, Any]:
        return copy.deepcopy(cls._config_view())

    @classmethod
    def load(cls, workdir: str = None) -> dict[str, Any]:
        """Load merged configuration from all tcode.json files on the path chain."""
        binding = cls._active_binding.get()
        if binding is not None:
            return binding.config
        if workdir is None:
            workdir = os.getcwd()
        normalized_workdir = cls._normalize_dir(workdir)
        normalized_config_root = cls.resolve_config_root(normalized_workdir)

        if (
            cls._loaded
            and cls._loaded_workdir == normalized_workdir
            and cls._loaded_config_root == normalized_config_root
        ):
            return cls._config

        config_paths = cls._collect_config_paths(normalized_workdir)
        if not config_paths:
            cls._config = {}
            cls._loaded = True
            cls._config_path = None
            cls._config_paths = []
            cls._loaded_workdir = normalized_workdir
            cls._loaded_config_root = normalized_config_root
            return cls._config

        merged_config: dict[str, Any] = {}
        for config_path in config_paths:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged_config = cls._deep_merge(merged_config, data)
            except Exception as e:
                print(f"Failed to load config {config_path}: {e}")

        local_model = cls._load_local_model_override(normalized_workdir, config_paths)
        if local_model:
            merged_config["model"] = local_model

        cls._config = merged_config
        cls._loaded = True
        cls._config_path = config_paths[-1]
        cls._config_paths = config_paths
        cls._loaded_workdir = normalized_workdir
        cls._loaded_config_root = normalized_config_root
        return cls._config

    @classmethod
    def get_agent(cls, name: str) -> Optional[dict[str, Any]]:
        """Get agent configuration by name.

        Compatible with TCode style config:
        - preferred: config.agent.<name>
        - deprecated fallback: config.mode.<name>
        """
        if not cls._loaded:
            cls.load()

        config = cls._config_view()
        agents = config.get("agent", {})
        if isinstance(agents, dict) and name in agents:
            return agents.get(name)

        modes = config.get("mode", {})
        if isinstance(modes, dict):
            return modes.get(name)
        return None

    @classmethod
    def get_default_agent(cls) -> Optional[str]:
        """Get default agent name from config (TCode compatible)."""
        if not cls._loaded:
            cls.load()

        value = cls._config_view().get("default_agent")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @classmethod
    def get_permission(cls, tool: str) -> str:
        """Get global permission for a tool"""
        if not cls._loaded:
            cls.load()

        permissions = cls._config_view().get("permission", {})
        return permissions.get(tool, "allow")

    @classmethod
    def get_provider(cls, name: str) -> Optional[dict[str, Any]]:
        """Get provider configuration by name"""
        if not cls._loaded:
            cls.load()

        providers = cls._config_view().get("provider", {})
        if isinstance(providers, dict):
            return providers.get(name)
        return None

    @classmethod
    def get_model(cls) -> Optional[str]:
        """Get configured default model in provider/model format."""
        if not cls._loaded:
            cls.load()

        value = cls._config_view().get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @classmethod
    def parse_model_ref(cls, model_ref: str) -> Optional[tuple[str, str]]:
        """Parse provider/model reference."""
        if not isinstance(model_ref, str) or "/" not in model_ref:
            return None
        provider_id, model_id = model_ref.split("/", 1)
        provider_id = provider_id.strip()
        model_id = model_id.strip()
        if not provider_id or not model_id:
            return None
        return provider_id, model_id

    @classmethod
    def resolve_model_selection(cls, workdir: str = None) -> dict[str, Any]:
        """Resolve active provider/model using TCode-like precedence."""
        if workdir is not None:
            cls.load(workdir)
        elif not cls._loaded:
            cls.load()

        providers = cls._config_view().get("provider", {})
        if not isinstance(providers, dict):
            providers = {}

        model_ref = cls.get_model()
        if model_ref:
            parsed = cls.parse_model_ref(model_ref)
            if parsed:
                provider_id, model_id = parsed
                return {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "provider": providers.get(provider_id, {}),
                    "source": "model",
                }

        for provider_id, provider_cfg in providers.items():
            models = {}
            if isinstance(provider_cfg, dict):
                models = provider_cfg.get("models", {})
            if isinstance(models, dict) and models:
                first_model_id = next(iter(models.keys()))
                return {
                    "provider_id": provider_id,
                    "model_id": first_model_id,
                    "provider": provider_cfg,
                    "source": "provider.models",
                }

        return {
            "provider_id": "ollama",
            "model_id": "qwen3-coder-next:latest",
            "provider": {},
            "source": "fallback",
        }

    @classmethod
    def get_all_providers(cls) -> dict[str, Any]:
        """Get all provider configurations"""
        if not cls._loaded:
            cls.load()

        providers = cls._config_view().get("provider", {})
        return providers if isinstance(providers, dict) else {}

    @classmethod
    def get_embeddings(cls) -> dict[str, Any]:
        """Get embedding configuration."""
        if not cls._loaded:
            cls.load()

        embeddings = cls._config_view().get("embeddings", {})
        return embeddings if isinstance(embeddings, dict) else {}

    @classmethod
    def get_runtime(cls) -> dict[str, Any]:
        """Get runtime configuration."""
        if not cls._loaded:
            cls.load()

        runtime = cls._config_view().get("runtime", {})
        return runtime if isinstance(runtime, dict) else {}

    @classmethod
    def get_sandbox_profiles(cls, workdir: str = None) -> dict[str, Any]:
        """Get configured sandbox profiles."""
        if not cls._loaded:
            cls.load(workdir)
        elif workdir is not None:
            cls.load(workdir)

        profiles = cls._config_view().get("sandbox_profiles", {})
        return profiles if isinstance(profiles, dict) else {}

    @classmethod
    def get_sandbox_profile(
        cls,
        name: str,
        workdir: str = None,
    ) -> Optional[dict[str, Any]]:
        profile_name = str(name or "").strip()
        if not profile_name:
            return None
        profiles = cls.get_sandbox_profiles(workdir)
        profile = profiles.get(profile_name)
        return profile if isinstance(profile, dict) else None

    @classmethod
    def get_sandbox_tool_profile(
        cls,
        tool_name: str,
        workdir: str = None,
    ) -> Optional[dict[str, str]]:
        """Return a configured tool-to-profile binding.

        String values are treated as fixed bindings. Object values support
        ``profile`` and ``mode`` where mode is ``fixed`` or ``default``.
        """
        normalized = str(tool_name or "").strip()
        if not normalized:
            return None
        if not cls._loaded:
            cls.load(workdir)
        elif workdir is not None:
            cls.load(workdir)

        bindings = cls._config_view().get("sandbox_tool_profiles", {})
        if not isinstance(bindings, dict):
            return None
        raw = bindings.get(normalized)
        if isinstance(raw, str):
            profile = raw.strip()
            return {"profile": profile, "mode": "fixed"} if profile else None
        if not isinstance(raw, dict):
            return None
        profile = str(raw.get("profile") or "").strip()
        mode = str(raw.get("mode") or "fixed").strip().lower()
        if not profile or mode not in {"fixed", "default"}:
            return None
        return {"profile": profile, "mode": mode}

    @classmethod
    def get_sandbox_capability_profile(
        cls,
        capability: str,
        workdir: str = None,
    ) -> Optional[str]:
        normalized = str(capability or "").strip()
        if not normalized:
            return None
        if not cls._loaded:
            cls.load(workdir)
        elif workdir is not None:
            cls.load(workdir)

        mappings = cls._config_view().get("sandbox_capability_profiles", {})
        if not isinstance(mappings, dict):
            return None
        raw = mappings.get(normalized)
        if isinstance(raw, str):
            return raw.strip() or None
        if isinstance(raw, dict):
            profile = str(raw.get("profile") or "").strip()
            return profile or None
        return None

    @classmethod
    def get_agent_sandbox_config(
        cls,
        agent_name: str,
        workdir: str = None,
    ) -> dict[str, Any]:
        normalized = str(agent_name or "").strip()
        if not normalized:
            return {}
        if not cls._loaded:
            cls.load(workdir)
        elif workdir is not None:
            cls.load(workdir)

        agent = cls.get_agent(normalized)
        if not isinstance(agent, dict):
            return {}

        sandbox = agent.get("sandbox", {})
        return dict(sandbox) if isinstance(sandbox, dict) else {}

    @classmethod
    def get_agent_sandbox_profile(
        cls,
        agent_name: str,
        workdir: str = None,
    ) -> Optional[str]:
        normalized = str(agent_name or "").strip()
        if not normalized:
            return None
        if not cls._loaded:
            cls.load(workdir)
        elif workdir is not None:
            cls.load(workdir)

        agent = cls.get_agent(normalized)
        if not isinstance(agent, dict):
            return None

        candidates: list[Any] = [
            agent.get("sandbox_profile"),
            (agent.get("sandbox") or {}).get("profile")
            if isinstance(agent.get("sandbox"), dict)
            else None,
            (agent.get("options") or {}).get("sandbox_profile")
            if isinstance(agent.get("options"), dict)
            else None,
        ]
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text
        return None

    @classmethod
    def reload(cls, workdir: str = None, *, emit: bool = True):
        """Reload configuration"""
        binding = cls._active_binding.get()
        if binding is not None:
            # A running task is immutable.  An internal plugin/config lookup must
            # never clear the process-global cache or observe newer disk values.
            return copy.deepcopy(binding.config)
        if emit:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    async def _emit():
                        try:
                            from ..plugins.manager import PluginManager
                            manager = await PluginManager.get_global(workdir or os.getcwd())
                            await manager.trigger("config.reload", {"workdir": workdir or ""}, {})
                        except Exception:
                            pass
                    loop.create_task(_emit())
            except Exception:
                pass

        cls._loaded = False
        cls._config = {}
        cls._config_path = None
        cls._config_paths = []
        cls._loaded_workdir = None
        cls._loaded_config_root = None
        return cls.load(workdir)

    @classmethod
    def is_loaded(cls) -> bool:
        """Check if configuration is loaded"""
        return cls._loaded

    @classmethod
    def clear(cls):
        """Clear loaded configuration"""
        cls._config = {}
        cls._loaded = False
        cls._config_path = None
        cls._config_paths = []
        cls._loaded_workdir = None
        cls._loaded_config_root = None
