"""团队目录。

这里不负责真正创建 worker，只负责回答三个问题：
1. 这个 agent 名字是不是某个团队；
2. 这个团队在前端该怎么展示；
3. manager / app / 前端在做模式判断时该看哪些名字。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from ..runtime.registrations import RegistrationDisposer, RegistrationToken


def _dedupe_names(*groups: Any) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        items = group if isinstance(group, (list, tuple, set)) else [group]
        for item in items:
            name = str(item or "").strip()
            if not name:
                continue
            lowered = name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            values.append(name)
    return tuple(values)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


@dataclass
class TeamWorkerCatalogEntry:
    name: str
    agent_name: str = ""
    role: str = ""
    enabled: bool = True
    visible: bool = True
    resident: bool = False
    permission_preset: str = "developer"
    deletable: bool = False
    peer_whitelist: tuple[str, ...] = ()
    skill_tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = str(self.name or "").strip().lower()
        self.name = name
        self.agent_name = str(self.agent_name or "").strip()
        self.role = str(self.role or name).strip() or name
        self.enabled = bool(self.enabled)
        self.visible = bool(self.visible)
        self.resident = bool(self.resident)
        self.permission_preset = str(self.permission_preset or "developer").strip() or "developer"
        self.deletable = bool(self.deletable)
        self.peer_whitelist = _dedupe_names(self.peer_whitelist)
        self.skill_tags = _dedupe_names(self.skill_tags)
        self.metadata = dict(self.metadata or {})

    @classmethod
    def from_config(
        cls,
        value: Any,
        *,
        resident_names: Optional[set[str]] = None,
        disabled_names: Optional[set[str]] = None,
        visible_names: Optional[set[str]] = None,
    ) -> Optional["TeamWorkerCatalogEntry"]:
        resident_names = resident_names or set()
        disabled_names = disabled_names or set()
        visible_names = visible_names or set()

        if isinstance(value, TeamWorkerCatalogEntry):
            data = {
                "name": value.name,
                "agent_name": value.agent_name,
                "role": value.role,
                "enabled": value.enabled,
                "visible": value.visible,
                "resident": value.resident,
                "permission_preset": value.permission_preset,
                "deletable": value.deletable,
                "peer_whitelist": list(value.peer_whitelist),
                "skill_tags": list(value.skill_tags),
                "metadata": dict(value.metadata or {}),
            }
        elif isinstance(value, dict):
            data = dict(value)
        else:
            data = {"name": str(value or "").strip()}

        name = str(data.get("name") or data.get("worker_name") or "").strip().lower()
        if not name:
            return None

        enabled = _as_bool(data.get("enabled"), True)
        if name in disabled_names:
            enabled = False

        if "visible" in data:
            visible = _as_bool(data.get("visible"), True)
        elif visible_names:
            visible = name in visible_names
        else:
            visible = True

        resident = _as_bool(data.get("resident"), name in resident_names)
        if name in resident_names:
            resident = True

        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return cls(
            name=name,
            agent_name=str(data.get("agent_name") or "").strip(),
            role=str(data.get("role") or name).strip(),
            enabled=enabled,
            visible=visible,
            resident=resident,
            permission_preset=str(data.get("permission_preset") or "developer").strip() or "developer",
            deletable=_as_bool(data.get("deletable"), False),
            peer_whitelist=tuple(
                str(item or "").strip()
                for item in (data.get("peer_whitelist") or [])
                if str(item or "").strip()
            ),
            skill_tags=tuple(
                str(item or "").strip()
                for item in (data.get("skill_tags") or [])
                if str(item or "").strip()
            ),
            metadata=dict(metadata or {}),
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agent_name": self.agent_name,
            "role": self.role,
            "enabled": self.enabled,
            "visible": self.visible,
            "resident": self.resident,
            "permission_preset": self.permission_preset,
            "deletable": self.deletable,
            "peer_whitelist": list(self.peer_whitelist),
            "skill_tags": list(self.skill_tags),
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class TeamCatalogEntry:
    key: str
    display_name: str
    runtime_agent: str
    visible_agent_name: str = ""
    slug: str = ""
    source: str = "builtin"
    delegation_reason: str = ""
    hidden_agent_names: tuple[str, ...] = ()
    execution_agent_names: tuple[str, ...] = ()
    bootstrap_agent_names: tuple[str, ...] = ()
    team_mode_agent_names: tuple[str, ...] = ()
    workflow_mode_agent_names: tuple[str, ...] = ()
    forced_scheduler_agent_names: tuple[str, ...] = ()
    mode_hint_visible: str = ""
    mode_hint_runtime: str = ""
    supports_memory: bool = True
    supports_subagents: bool = True
    worker_names: tuple[str, ...] = ()
    resident_worker_names: tuple[str, ...] = ()
    disabled_worker_names: tuple[str, ...] = ()
    session_prefixes: tuple[str, ...] = ()
    workers: tuple[TeamWorkerCatalogEntry, ...] = ()

    def __post_init__(self) -> None:
        runtime = str(self.runtime_agent or "").strip()
        visible = str(self.visible_agent_name or runtime).strip() or runtime
        slug = str(self.slug or self.key or runtime).strip() or runtime
        hidden = self.hidden_agent_names
        if not hidden and visible != runtime:
            hidden = (runtime,)

        self.runtime_agent = runtime
        self.visible_agent_name = visible
        self.slug = slug
        self.hidden_agent_names = _dedupe_names(hidden)
        self.execution_agent_names = _dedupe_names(
            self.execution_agent_names or (runtime,)
        )
        self.bootstrap_agent_names = _dedupe_names(
            self.bootstrap_agent_names or (visible, runtime)
        )
        self.team_mode_agent_names = _dedupe_names(
            self.team_mode_agent_names or self.bootstrap_agent_names
        )
        self.workflow_mode_agent_names = _dedupe_names(
            self.workflow_mode_agent_names or self.team_mode_agent_names
        )
        self.forced_scheduler_agent_names = _dedupe_names(
            self.forced_scheduler_agent_names or self.workflow_mode_agent_names
        )
        legacy_resident_names = {
            name.lower() for name in _dedupe_names(self.resident_worker_names)
        }
        disabled_names = {
            name.lower() for name in _dedupe_names(self.disabled_worker_names)
        }
        visible_names = {
            name.lower() for name in _dedupe_names(self.worker_names)
        }

        parsed_workers: list[TeamWorkerCatalogEntry] = []
        seen_workers: set[str] = set()
        for item in self.workers or ():
            worker = TeamWorkerCatalogEntry.from_config(
                item,
                resident_names=legacy_resident_names,
                disabled_names=disabled_names,
                visible_names=visible_names,
            )
            if worker is None or worker.name in seen_workers:
                continue
            seen_workers.add(worker.name)
            parsed_workers.append(worker)

        for name in _dedupe_names(self.worker_names, self.resident_worker_names):
            normalized_name = name.lower()
            if normalized_name in seen_workers:
                continue
            parsed_workers.append(
                TeamWorkerCatalogEntry(
                    name=normalized_name,
                    enabled=normalized_name not in disabled_names,
                    visible=True,
                    resident=normalized_name in legacy_resident_names,
                )
            )
            seen_workers.add(normalized_name)

        self.workers = tuple(parsed_workers)
        self.disabled_worker_names = _dedupe_names(disabled_names)
        self.resident_worker_names = _dedupe_names(
            [
                worker.name
                for worker in self.workers
                if worker.enabled and worker.visible and worker.resident
            ]
        )
        self.worker_names = _dedupe_names(
            [
                worker.name
                for worker in self.workers
                if worker.enabled and worker.visible
            ]
        )
        self.session_prefixes = _dedupe_names(self.session_prefixes)

    def matches_agent_name(self, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        return normalized in {
            name.lower()
            for name in _dedupe_names(
                self.runtime_agent,
                self.visible_agent_name,
                self.hidden_agent_names,
                self.execution_agent_names,
                self.bootstrap_agent_names,
                self.team_mode_agent_names,
                self.workflow_mode_agent_names,
                self.forced_scheduler_agent_names,
            )
        }

    def mode_hint(self, actual_agent_name: Optional[str] = None) -> str:
        actual = str(actual_agent_name or "").strip()
        if actual and actual == self.runtime_agent and self.mode_hint_runtime:
            return self.mode_hint_runtime
        return self.mode_hint_visible or self.mode_hint_runtime or self.display_name

    def delegation_payload(self) -> dict[str, str]:
        return {
            "agent_name": self.runtime_agent,
            "display_name": self.display_name,
            "slug": self.slug,
            "reason": self.delegation_reason or self.display_name,
        }

    def to_api_dict(self, *, available: bool) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "runtime_agent": self.runtime_agent,
            "visible_agent_name": self.visible_agent_name,
            "slug": self.slug,
            "source": self.source,
            "delegation_reason": self.delegation_reason,
            "hidden_agent_names": list(self.hidden_agent_names),
            "execution_agent_names": list(self.execution_agent_names),
            "bootstrap_agent_names": list(self.bootstrap_agent_names),
            "team_mode_agent_names": list(self.team_mode_agent_names),
            "workflow_mode_agent_names": list(self.workflow_mode_agent_names),
            "forced_scheduler_agent_names": list(self.forced_scheduler_agent_names),
            "mode_hint_visible": self.mode_hint_visible,
            "mode_hint_runtime": self.mode_hint_runtime,
            "supports_memory": self.supports_memory,
            "supports_subagents": self.supports_subagents,
            "worker_names": list(self.worker_names),
            "resident_worker_names": list(self.resident_worker_names),
            "disabled_worker_names": list(self.disabled_worker_names),
            "session_prefixes": list(self.session_prefixes),
            "workers": [worker.to_api_dict() for worker in self.workers],
            "available": available,
        }


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class TeamCatalog(metaclass=WorkspaceRegistryMeta):

    """统一维护固定团队和插件团队的元数据。"""
    _workspace_fields = ('_plugin_entries', '_plugin_sources', '_plugin_registrations')

    _plugin_entries: dict[str, TeamCatalogEntry] = {}
    _plugin_sources: dict[str, str] = {}
    _plugin_registrations: dict[str, RegistrationToken] = {}
    _manager_agent_names = {"manager"}

    _builtin_entries: dict[str, TeamCatalogEntry] = {
        "protein": TeamCatalogEntry(
            key="protein",
            display_name="蛋白设计团队",
            runtime_agent="protein_orchestrator",
            visible_agent_name="protein_orchestrator",
            slug="protein",
            source="builtin",
            delegation_reason="识别到蛋白设计/结构/序列类任务",
            mode_hint_visible="固定专家团队模式：自动使用预置蛋白设计专家协作，不需要手动建团队。",
            mode_hint_runtime="固定专家团队模式：自动使用预置蛋白设计专家协作，不需要手动建团队。",
            worker_names=(
                "generalist",
                "requirements",
                "paper_generalist",
                "design",
                "structure",
                "scoring",
                "experiment",
                "risk",
                "verifier",
                "report",
            ),
            resident_worker_names=("generalist",),
            session_prefixes=("protein-",),
        )
    }

    @classmethod
    def register(
        cls,
        entry: TeamCatalogEntry,
        *,
        source: Optional[str] = None,
        instance_id: str = "",
        replace_existing: bool = True,
    ) -> RegistrationDisposer:
        normalized_source = str(source or entry.source or "plugin").strip() or "plugin"
        if not replace_existing and entry.key in cls._plugin_entries:
            existing_source = cls._plugin_sources.get(entry.key, "unknown")
            raise ValueError(
                f"team catalog key conflict: '{entry.key}' already registered from {existing_source}"
            )
        token = RegistrationToken.create(
            registry="team_catalog",
            key=entry.key,
            source=normalized_source,
            instance_id=instance_id,
        )
        entry.source = normalized_source
        cls._plugin_entries[entry.key] = entry
        cls._plugin_sources[entry.key] = normalized_source
        cls._plugin_registrations[entry.key] = token
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        current = cls._plugin_registrations.get(token.key)
        if current != token:
            return False
        cls._plugin_entries.pop(token.key, None)
        cls._plugin_sources.pop(token.key, None)
        cls._plugin_registrations.pop(token.key, None)
        return True

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        normalized = str(source or "").strip()
        if not normalized:
            return []
        to_remove = [
            key
            for key, item_source in cls._plugin_sources.items()
            if item_source == normalized
            and (
                not legacy_only
                or not cls._plugin_registrations.get(key)
                or not cls._plugin_registrations[key].instance_id
            )
        ]
        for key in to_remove:
            cls._plugin_entries.pop(key, None)
            cls._plugin_sources.pop(key, None)
            cls._plugin_registrations.pop(key, None)
        return to_remove

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        normalized = str(instance_id or "").strip()
        if not normalized:
            return []
        tokens = [
            token
            for token in cls._plugin_registrations.values()
            if token.instance_id == normalized
        ]
        return [token.key for token in tokens if cls.unregister_exact(token)]

    @classmethod
    def _all_entries(cls) -> list[TeamCatalogEntry]:
        merged = {**cls._dynamic_builtin_entries(), **cls._plugin_entries}
        return [merged[key] for key in sorted(merged.keys())]

    @classmethod
    def _dynamic_builtin_entries(cls) -> dict[str, TeamCatalogEntry]:
        entries = dict(cls._builtin_entries)
        protein = entries.get("protein")
        if protein is not None:
            try:
                from .protein_team_config import load_protein_team_config

                config = load_protein_team_config()
                workers = []
                for item in config.get("workers", []) or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "") or "").strip().lower()
                    if not name:
                        continue
                    workers.append(
                        TeamWorkerCatalogEntry.from_config(
                            {
                                "name": name,
                                "agent_name": str(item.get("agent_name") or f"worker_{name}"),
                                "role": str(item.get("role") or name),
                                "enabled": item.get("enabled", True),
                                "visible": item.get("visible", True),
                                "resident": item.get(
                                    "resident",
                                    bool(item.get("resident_generalist", False)),
                                ),
                            }
                        )
                    )
                workers = [worker for worker in workers if worker is not None]
                if workers:
                    entries["protein"] = replace(
                        protein,
                        worker_names=tuple(
                            worker.name
                            for worker in workers
                            if worker.enabled and worker.visible
                        ),
                        resident_worker_names=tuple(
                            worker.name
                            for worker in workers
                            if worker.enabled and worker.visible and worker.resident
                        ),
                        disabled_worker_names=tuple(
                            worker.name for worker in workers if not worker.enabled
                        ),
                        workers=tuple(workers),
                    )
            except Exception:
                pass
        return entries

    @classmethod
    def _is_available(cls, entry: TeamCatalogEntry) -> bool:
        from ..agent.agents import BuiltinAgents

        return BuiltinAgents.get(entry.runtime_agent) is not None

    @classmethod
    def list(cls, *, available_only: bool = False) -> list[TeamCatalogEntry]:
        entries = cls._all_entries()
        if not available_only:
            return entries
        return [entry for entry in entries if cls._is_available(entry)]

    @classmethod
    def get_by_key(
        cls, key: str, *, available_only: bool = False
    ) -> Optional[TeamCatalogEntry]:
        normalized = str(key or "").strip().lower()
        if not normalized:
            return None
        for entry in cls.list(available_only=available_only):
            if entry.key.lower() == normalized:
                return entry
        return None

    @classmethod
    def get_by_runtime_agent(
        cls, agent_name: str, *, available_only: bool = False
    ) -> Optional[TeamCatalogEntry]:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return None
        for entry in cls.list(available_only=available_only):
            if entry.runtime_agent.lower() == normalized:
                return entry
        return None

    @classmethod
    def get_by_visible_agent_name(
        cls, agent_name: str, *, available_only: bool = False
    ) -> Optional[TeamCatalogEntry]:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return None
        for entry in cls.list(available_only=available_only):
            if entry.visible_agent_name.lower() == normalized:
                return entry
        return None

    @classmethod
    def get_by_agent_name(
        cls, agent_name: str, *, available_only: bool = False
    ) -> Optional[TeamCatalogEntry]:
        normalized = str(agent_name or "").strip()
        if not normalized:
            return None
        for entry in cls.list(available_only=available_only):
            if entry.matches_agent_name(normalized):
                return entry
        return None

    @classmethod
    def get_worker(
        cls,
        team_key: str,
        worker_name: str,
        *,
        available_only: bool = False,
    ) -> Optional[TeamWorkerCatalogEntry]:
        entry = cls.get_by_key(team_key, available_only=available_only)
        if entry is None:
            return None
        normalized = str(worker_name or "").strip().lower()
        if not normalized:
            return None
        if normalized.startswith("worker_"):
            normalized = normalized.removeprefix("worker_")
        for worker in entry.workers or ():
            if worker.name == normalized:
                return worker
        return None

    @classmethod
    def get_worker_by_session(
        cls,
        session_id: str,
        worker_name: str,
    ) -> Optional[TeamWorkerCatalogEntry]:
        normalized_session = str(session_id or "").strip().lower()
        if not normalized_session:
            return None
        for entry in cls.list(available_only=False):
            for prefix in entry.session_prefixes or ():
                if normalized_session.startswith(str(prefix or "").strip().lower()):
                    return cls.get_worker(entry.key, worker_name)
        return None

    @classmethod
    def worker_is_enabled(cls, team_key: str, worker_name: str) -> bool:
        worker = cls.get_worker(team_key, worker_name)
        return True if worker is None else bool(worker.enabled)

    @classmethod
    def worker_is_visible(cls, team_key: str, worker_name: str) -> bool:
        worker = cls.get_worker(team_key, worker_name)
        return True if worker is None else bool(worker.enabled and worker.visible)

    @classmethod
    def resolve_bootstrap_agent_name(cls, agent_name: str) -> str:
        entry = cls.get_by_agent_name(agent_name, available_only=True)
        if entry is None:
            return str(agent_name or "").strip()
        return entry.runtime_agent

    @classmethod
    def to_visible_agent_name(cls, agent_name: str) -> str:
        raw = str(agent_name or "").strip()
        if not raw:
            return ""
        entry = cls.get_by_agent_name(raw, available_only=False)
        if entry is None:
            return raw
        return entry.visible_agent_name or raw

    @classmethod
    def resolve_execution_agent_name(
        cls,
        *,
        selected_agent_name: str,
        current_agent_name: str,
    ) -> str:
        selected = str(selected_agent_name or "").strip()
        current = str(current_agent_name or "").strip()
        if not selected:
            return selected
        entry = cls.get_by_visible_agent_name(selected, available_only=False)
        if entry is None:
            return selected
        if current and current.lower() in {name.lower() for name in entry.execution_agent_names}:
            return current
        return selected

    @classmethod
    def is_frontend_hidden_agent(cls, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        hidden = {
            name.lower()
            for entry in cls.list(available_only=False)
            for name in entry.hidden_agent_names
        }
        return normalized in hidden

    @classmethod
    def is_fixed_team_bootstrap_agent(cls, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        names = {
            name.lower()
            for entry in cls.list(available_only=False)
            for name in entry.bootstrap_agent_names
        }
        return normalized in names

    @classmethod
    def is_fixed_team_agent(cls, agent_name: str) -> bool:
        return cls.get_by_agent_name(agent_name, available_only=False) is not None

    @classmethod
    def is_team_mode_agent(cls, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        if normalized in cls._manager_agent_names:
            return True
        names = {
            name.lower()
            for entry in cls.list(available_only=False)
            for name in entry.team_mode_agent_names
        }
        return normalized in names

    @classmethod
    def is_workflow_mode_agent(cls, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        if normalized in cls._manager_agent_names:
            return True
        names = {
            name.lower()
            for entry in cls.list(available_only=False)
            for name in entry.workflow_mode_agent_names
        }
        return normalized in names

    @classmethod
    def uses_forced_scheduler(cls, agent_name: str) -> bool:
        normalized = str(agent_name or "").strip().lower()
        if not normalized:
            return False
        if normalized in cls._manager_agent_names:
            return True
        names = {
            name.lower()
            for entry in cls.list(available_only=False)
            for name in entry.forced_scheduler_agent_names
        }
        return normalized in names

    @classmethod
    def visible_alias_entries(cls) -> list[TeamCatalogEntry]:
        return [
            entry
            for entry in cls.list(available_only=True)
            if entry.visible_agent_name and entry.visible_agent_name != entry.runtime_agent
        ]

    @classmethod
    def api_payload(cls) -> list[dict[str, Any]]:
        return [
            entry.to_api_dict(available=cls._is_available(entry))
            for entry in cls.list(available_only=False)
        ]
