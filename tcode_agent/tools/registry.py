"""Tool registry for managing available tools"""

import fnmatch
import json
import os
from typing import Any, Optional, TYPE_CHECKING

from .. import __version__ as TCODE_VERSION
from ..types import AgentInfo, ToolResult
from ..runtime.registrations import RegistrationDisposer, RegistrationToken
from .base import Tool
from ..runtime.execution_profile import profile_allows_tool
from ..runtime.sandbox import sandbox_tool_block_reason


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class ToolRegistry(metaclass=WorkspaceRegistryMeta):

    """Registry for all available tools"""
    _workspace_fields = ('_custom_tools', '_custom_tool_sources', '_custom_tool_registrations')

    _tools: dict[str, Tool] = {}
    _custom_tools: dict[str, Tool] = {}
    _custom_tool_sources: dict[str, str] = {}
    _custom_tool_registrations: dict[str, RegistrationToken] = {}
    _hidden_from_definitions: set[str] = {
        "bash",
        "memory_read",
        "memory_write",
    }
    _profile_scoped_from_definitions: set[str] = {
        "workflow_run",
    }
    _protein_tool_ids: set[str] = {
        "protein_candidate",
    }
    _globally_safe_tool_ids: set[str] = {
        "present_image",
        "look_at",
    }
    _team_tool_ids: set[str] = {
        "create_worker",
        "assign_task",
        "list_teams",
        "list_workers",
        "terminate_worker",
        "team_memory_write",
        "team_memory_read",
        "task_graph_read",
        "ask_peer_question",
        "answer_peer_question",
    }
    _evidence_tool_ids: set[str] = {
        "evidence_write",
        "evidence_search",
        "evidence_graph",
        "evidence_doctor",
    }
    _research_tool_ids: set[str] = {
        "research_read",
        "research_workspace",
        "research_action_commit",
    }
    # Keep compatibility implementations registered, but do not spend model
    # context on duplicate or operator-only entry points during normal turns.
    _deferred_tool_ids: set[str] = {
        "memory_cleanup",
        "skill_install",
        "skill_source",
        "skill_run",
        "subagent_guide",
    }
    _workflow_agent_names: set[str] = {
        "protein_orchestrator",
        "protein-design",
    }
    _workflow_profile_names: set[str] = {
        "protein-design",
        "orchestrated",
    }
    _team_agent_names: set[str] = {
        "manager",
        "protein_orchestrator",
        "protein-design",
    }
    _team_profile_names: set[str] = {
        "protein-design",
        "orchestrated",
        "manager-only",
    }
    _evidence_agent_names: set[str] = {
        "protein_orchestrator",
        "protein-design",
        "paper_reviewer",
        "paper-review",
        "literature_reviewer",
    }
    _evidence_profile_names: set[str] = {
        "protein-design",
        "orchestrated",
        "paper-review",
        "research-web",
        "bio-safe",
    }
    _research_agent_names: set[str] = {
        "paper_auto",
        "paper_reviewer",
        "literature_reviewer",
        "researcher",
    }
    _research_profile_names: set[str] = {
        "paper-review",
        "research-web",
    }
    _protein_agent_names: set[str] = {
        "protein_orchestrator",
        "protein-design",
    }
    _protein_worker_agent_names: set[str] = {
        "worker_requirements",
        "worker_paper_generalist",
        "worker_design",
        "worker_structure",
        "worker_scoring",
        "worker_experiment",
        "worker_risk",
        "worker_verifier",
        "worker_report",
    }
    _protein_profile_names: set[str] = {
        "protein-design",
        "orchestrated",
        "bio-safe",
    }
    _INVALID_ALLOWLIST_INPUT = "__invalid_allowed_tools_input__"

    @classmethod
    def register(cls, tool: Tool):
        """Register a tool"""
        cls._tools[tool.id] = tool

    @classmethod
    def register_custom(
        cls,
        tool: Tool,
        source: Optional[str] = None,
        *,
        instance_id: str = "",
        replace: bool = True,
    ) -> RegistrationDisposer:
        """Register a custom tool and return an instance-exact disposer."""
        if not replace and tool.id in cls._custom_tools:
            existing_source = cls._custom_tool_sources.get(tool.id, "unknown")
            raise ValueError(
                f"custom tool name conflict: '{tool.id}' already registered from {existing_source}"
            )
        token = RegistrationToken.create(
            registry="tools",
            key=tool.id,
            source=str(source or ""),
            instance_id=instance_id,
        )
        cls._custom_tools[tool.id] = tool
        if source:
            cls._custom_tool_sources[tool.id] = source
        else:
            cls._custom_tool_sources.pop(tool.id, None)
        cls._custom_tool_registrations[tool.id] = token
        return RegistrationDisposer(token, cls.unregister_custom_exact)

    @classmethod
    def unregister_custom_exact(cls, token: RegistrationToken) -> bool:
        current = cls._custom_tool_registrations.get(token.key)
        if current != token:
            return False
        cls._custom_tools.pop(token.key, None)
        cls._custom_tool_sources.pop(token.key, None)
        cls._custom_tool_registrations.pop(token.key, None)
        return True

    @classmethod
    def get(cls, tool_id: str) -> Optional[Tool]:
        """Get a tool by ID"""
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1' and sandbox_tool_block_reason(tool_id):
            return None
        return cls._tools.get(tool_id) or cls._custom_tools.get(tool_id)

    @classmethod
    def definition_provenance(cls, tool: Tool) -> dict[str, str]:
        """Return non-authoritative identity metadata for definition snapshots.

        Execution lookup remains unchanged.  Object identity is used so a
        custom tool shadowing a built-in name is not attributed to the wrong
        registry entry.
        """

        tool_id = str(getattr(tool, "id", "") or "").strip()
        if tool_id and cls._custom_tools.get(tool_id) is tool:
            token = cls._custom_tool_registrations.get(tool_id)
            source = str(
                cls._custom_tool_sources.get(tool_id)
                or getattr(tool, "source", "")
                or "unknown"
            ).strip()
            provider_version = str(
                getattr(tool, "plugin_version", "") or "unversioned"
            ).strip()
            definition = getattr(tool, "definition", None)
            definition_version = str(
                getattr(definition, "version", "")
                or provider_version
                or "unversioned"
            ).strip()
            return {
                "provider_id": f"plugin:{source}",
                "provider_instance_id": str(
                    getattr(token, "instance_id", "")
                    or getattr(tool, "instance_id", "")
                    or ""
                ).strip(),
                "provider_version": provider_version or "unversioned",
                "definition_version": definition_version or "unversioned",
            }
        return {
            "provider_id": "tcode.builtin",
            "provider_instance_id": "process",
            "provider_version": TCODE_VERSION,
            "definition_version": TCODE_VERSION,
        }

    @classmethod
    def permission_closure(cls, tool_ids: list[str]) -> list[str]:
        """Expand a tool allowlist with dependencies declared by each tool."""
        resolved: list[str] = []
        seen: set[str] = set()
        pending = [str(item or "").strip() for item in tool_ids]
        while pending:
            tool_id = pending.pop(0)
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            resolved.append(tool_id)
            tool = cls.get(tool_id)
            if tool is None:
                continue
            for dependency in tuple(tool.permission_dependencies or ()):
                normalized = str(dependency or "").strip()
                if normalized and normalized not in seen:
                    pending.append(normalized)
        return resolved

    @classmethod
    def normalize_allowlist(cls, raw: Any) -> str | list[str]:
        """Normalize public allowlist input into exact registered-tool patterns.

        Tool arguments sometimes arrive through an adapter as a JSON-encoded
        array string instead of a native JSON array.  Treat that representation
        exactly like the native array; otherwise quotes and brackets become
        bogus tool IDs and a valid delegated task is rejected before launch.
        """

        if raw is None:
            return "all"
        if isinstance(raw, str):
            text = raw.strip()
            if not text or text.lower() == "all":
                return "all"
            if text.startswith("["):
                try:
                    decoded = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, list):
                    raw = decoded
                else:
                    raw = [part.strip() for part in text.split(",")]
            else:
                raw = [part.strip() for part in text.split(",")]
        elif isinstance(raw, (tuple, set)):
            raw = list(raw)

        if not isinstance(raw, list):
            # Explicit but malformed input must fail closed.  Only an omitted
            # value, an empty string, or the exact scalar string "all" grants
            # the complete registry.
            return [cls._INVALID_ALLOWLIST_INPUT]

        items = [
            str(item or "").strip()
            for item in raw
            if str(item or "").strip()
        ]
        if not items:
            return []
        if len(items) == 1 and items[0].lower() == "all":
            return "all"
        return cls.permission_closure(items)

    @classmethod
    def unknown_allowlist_ids(cls, tool_ids: list[str]) -> list[str]:
        """Return exact allowlist entries that are not registered tool IDs."""
        unknown: list[str] = []
        for item in tool_ids:
            tool_id = str(item or "").strip()
            if not tool_id:
                continue
            if "*" in tool_id or "?" in tool_id or tool_id.startswith("mcp_"):
                continue
            if cls.get(tool_id) is None:
                unknown.append(tool_id)
        return list(dict.fromkeys(unknown))

    @classmethod
    def unregister(cls, tool_id: str):
        """Unregister a tool"""
        if tool_id in cls._tools:
            del cls._tools[tool_id]
        if tool_id in cls._custom_tools:
            del cls._custom_tools[tool_id]
        if tool_id in cls._custom_tool_sources:
            del cls._custom_tool_sources[tool_id]
        cls._custom_tool_registrations.pop(tool_id, None)

    @classmethod
    def unregister_custom_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        """Unregister all custom tools originating from a specific source."""
        if not source:
            return []
        to_remove = [
            tool_id
            for tool_id, tool_source in cls._custom_tool_sources.items()
            if tool_source == source
            and (
                not legacy_only
                or not cls._custom_tool_registrations.get(tool_id)
                or not cls._custom_tool_registrations[tool_id].instance_id
            )
        ]
        for tool_id in to_remove:
            cls.unregister(tool_id)
        return to_remove

    @classmethod
    def unregister_custom_by_instance(cls, instance_id: str) -> list[str]:
        normalized = str(instance_id or "").strip()
        if not normalized:
            return []
        tokens = [
            token
            for token in cls._custom_tool_registrations.values()
            if token.instance_id == normalized
        ]
        return [token.key for token in tokens if cls.unregister_custom_exact(token)]

    @classmethod
    def all(
        cls,
        agent: Optional[AgentInfo] = None,
        *,
        execution_profile: Optional[str] = None,
        explicit_workflow: bool = False,
        discoverable: bool = False,
    ) -> list[Tool]:
        """Get admitted tools. Host-selected workflows bypass only default visibility,
        never permissions, sandbox, agent availability, or execution-profile filters.
        """
        # 构建模式按需发现全部获准工具；仅绕过默认展示，不绕过后续安全检查。
        build_discovery = discoverable and str(getattr(agent, "name", "") or "").strip().lower() == "build"
        tools = list(cls._tools.values()) + list(cls._custom_tools.values())
        tools = [
            t
            for t in tools
            if build_discovery or (t.id == "workflow_run" and explicit_workflow) or cls._is_visible_in_definitions(
                t.id,
                agent=agent,
                execution_profile=execution_profile,
            )
        ]
        tools = [t for t in tools if not sandbox_tool_block_reason(t.id)]

        if agent:
            tools = [t for t in tools if t.available_to_agent(agent.name)]
            disabled = cls._get_disabled_tools(agent.permission)
            for tool_id in cls._globally_safe_tool_ids:
                if cls._explicit_permission_action(agent, tool_id) != "deny":
                    disabled.discard(tool_id)
            tools = [t for t in tools if t.id not in disabled]

        if execution_profile:
            tools = [
                t
                for t in tools
                if profile_allows_tool(
                    execution_profile,
                    t.id,
                    getattr(t, "capability", None),
                )
            ]

        return tools

    @classmethod
    def _explicit_permission_action(
        cls,
        agent: Optional[AgentInfo],
        tool_id: str,
    ) -> Optional[str]:
        """Resolve explicit non-* permission rules for visibility exceptions."""
        if not agent:
            return None
        permissions = dict(getattr(agent, "permission", {}) or {})
        action: Optional[str] = None
        for pattern, value in permissions.items():
            if pattern == "*" or isinstance(value, dict) or not isinstance(value, str):
                continue
            if pattern == tool_id:
                action = value
                continue
            if ("*" in pattern or "?" in pattern) and fnmatch.fnmatch(tool_id, pattern):
                action = value
        normalized = str(action or "").strip().lower()
        return normalized or None

    @classmethod
    def _workflow_run_visible(
        cls,
        *,
        agent: Optional[AgentInfo],
        execution_profile: Optional[str],
    ) -> bool:
        explicit_action = cls._explicit_permission_action(agent, "workflow_run")
        if explicit_action == "deny":
            return False
        if explicit_action in {"allow", "ask"}:
            return True

        try:
            from ..runtime.execution_profile import normalize_execution_profile

            profile_name = normalize_execution_profile(execution_profile)
        except Exception:
            profile_name = ""
        if profile_name in cls._workflow_profile_names:
            return True

        agent_name = str(getattr(agent, "name", "") or "").strip().lower()
        if agent_name in cls._workflow_agent_names:
            return True
        if agent_name.startswith("protein_") and agent_name.endswith("_orchestrator"):
            return True
        return False

    @classmethod
    def _profile_or_agent_visible(
        cls,
        *,
        agent: Optional[AgentInfo],
        execution_profile: Optional[str],
        profile_names: set[str],
        agent_names: set[str],
    ) -> bool:
        try:
            from ..runtime.execution_profile import normalize_execution_profile

            profile_name = normalize_execution_profile(execution_profile)
        except Exception:
            profile_name = ""
        if profile_name in profile_names:
            return True

        agent_name = str(getattr(agent, "name", "") or "").strip().lower()
        if agent_name in agent_names:
            return True
        if agent_name.startswith("worker_"):
            worker_alias = agent_name.replace("worker_", "protein_", 1)
            if worker_alias in agent_names:
                return True
        return False

    @classmethod
    def _is_visible_in_definitions(
        cls,
        tool_id: str,
        *,
        agent: Optional[AgentInfo],
        execution_profile: Optional[str],
    ) -> bool:
        normalized = str(tool_id or "").strip()
        if normalized == "context_expand":
            from ..context.compression.contracts import Policy
            policy = Policy.from_env().restricted((getattr(agent, "options", {}) or {}).get("context_compression"))
            return policy.mode == "enforce" and bool(policy.tools)
        if normalized in cls._hidden_from_definitions:
            return False
        explicit_action = cls._explicit_permission_action(agent, normalized)
        registered = cls._custom_tools.get(normalized) or cls._tools.get(normalized)
        if normalized in cls._deferred_tool_ids or getattr(registered, "on_demand", False):
            return explicit_action in {"allow", "ask"}
        if normalized == "browser":
            return str(getattr(agent, "name", "") or "").strip().lower() == "browser"
        if normalized == "workflow_run":
            return cls._workflow_run_visible(
                agent=agent,
                execution_profile=execution_profile,
            )
        if normalized in cls._team_tool_ids:
            if explicit_action in {"allow", "ask"}:
                return True
            return cls._profile_or_agent_visible(
                agent=agent,
                execution_profile=execution_profile,
                profile_names=cls._team_profile_names,
                agent_names=cls._team_agent_names,
            )
        if normalized in cls._evidence_tool_ids:
            return cls._profile_or_agent_visible(
                agent=agent,
                execution_profile=execution_profile,
                profile_names=cls._evidence_profile_names,
                agent_names=cls._evidence_agent_names,
            )
        if normalized in cls._research_tool_ids:
            if explicit_action in {"allow", "ask"}:
                return True
            return cls._profile_or_agent_visible(
                agent=agent,
                execution_profile=execution_profile,
                profile_names=cls._research_profile_names,
                agent_names=cls._research_agent_names,
            )
        if normalized in cls._protein_tool_ids:
            agent_name = str(getattr(agent, "name", "") or "").strip().lower()
            if (
                agent_name.startswith("protein_")
                or agent_name in cls._protein_worker_agent_names
            ):
                return True
            return cls._profile_or_agent_visible(
                agent=agent,
                execution_profile=execution_profile,
                profile_names=cls._protein_profile_names,
                agent_names=cls._protein_agent_names,
            )
        if normalized in cls._profile_scoped_from_definitions:
            return False
        return True

    @classmethod
    def ids(
        cls,
        agent: Optional[AgentInfo] = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[str]:
        """Get all tool IDs"""
        return [t.id for t in cls.all(agent, execution_profile=execution_profile)]

    @classmethod
    def _get_disabled_tools(cls, permissions: dict[str, Any]) -> set[str]:
        """Get set of disabled tool IDs based on permissions

        Supports:
        - Simple allow/deny: {"tool_name": "deny"}
        - Wildcard patterns: {"*": "allow", "specific_tool": "deny"}
        - Nested permissions: {"bash": {"git*": "allow", "*": "deny"}}
        """
        disabled = set()
        tool_ids = set(cls._tools.keys()) | set(cls._custom_tools.keys())
        if not tool_ids:
            return disabled

        rules: list[tuple[str, str]] = []
        for key, value in permissions.items():
            if isinstance(value, dict):
                nested_actions = {
                    str(action or "").strip().lower()
                    for action in value.values()
                    if isinstance(action, str)
                }
                rules.append(
                    (key, "allow" if nested_actions.intersection({"allow", "ask"}) else "deny")
                )
                continue
            if isinstance(value, str):
                rules.append((key, value))

        for tool_id in tool_ids:
            action: Optional[str] = None
            for pattern, value in rules:
                if pattern == "*":
                    action = value
                    continue
                if "*" in pattern or "?" in pattern:
                    if fnmatch.fnmatch(tool_id, pattern):
                        action = value
                    continue
                if pattern == tool_id:
                    action = value

            if action == "deny":
                disabled.add(tool_id)

        return disabled

    @classmethod
    def clear(cls):
        """Clear all custom tools"""
        cls._custom_tools.clear()
        cls._custom_tool_sources.clear()
