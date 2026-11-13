"""Skill tool - Load specialized skills"""

import fnmatch
import os
from pathlib import Path
from typing import Any

from ..tools.base import Tool, ToolContext, ParameterSchema
from ..types import ToolResult
from ..runtime.resource_import import (
    mount_resource_for_workspace,
    stage_resource_for_workspace,
)
from .manager import SkillManager, SkillInfo
from .runtime_binding import skill_allowed_for_agent, skill_runtime_binding


class SkillParameters(ParameterSchema):
    """Parameters for skill tool"""

    name: str


class SkillTool(Tool):
    """Load a specialized skill that provides domain-specific instructions"""

    @staticmethod
    def _permission_action(
        permission: str,
        patterns: list[str],
        extra: dict[str, Any] | None,
    ) -> str:
        if not isinstance(extra, dict):
            return "allow"
        resolver = extra.get("__permission_resolve__")
        if callable(resolver):
            try:
                result = resolver(permission, patterns)
            except TypeError:
                result = resolver(permission)
            normalized = str(result or "").strip().lower()
            if normalized in {"allow", "deny", "ask"}:
                return normalized
        return "allow"

    @property
    def id(self) -> str:
        return "skill"

    @property
    def permission_dependencies(self) -> tuple[str, ...]:
        # Loading instructions and executing declared actions are one capability.
        # skill_run still performs its own skill visibility and sandbox checks.
        return ("skill_run",)

    @property
    def description(self) -> str:
        skills = SkillManager.all()

        if not skills:
            return "Load a specialized skill. No skills available."

        max_visible = 80
        visible_skills = skills[:max_visible]
        skill_list = "\n".join(
            [f"- **{s.name}**: {s.description}" for s in visible_skills]
        )
        remainder = max(0, len(skills) - len(visible_skills))
        more_line = (
            f"\n... and {remainder} more skill(s)."
            if remainder > 0
            else ""
        )

        return f"""Load a specialized skill that provides domain-specific instructions and workflows.

This catalog is authoritative for currently loaded skills. When the user asks what skills are available,
prefer this catalog and do not run workspace filesystem scans only for skill enumeration.

Available skills:
{skill_list}
{more_line}

Use this tool when you recognize that a task matches one of the available skills."""

    @property
    def parameters(self) -> type:
        return SkillParameters

    @staticmethod
    def _normalize_patterns(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
        if not isinstance(value, list):
            return []
        return [str(item or "").strip() for item in value if str(item or "").strip()]

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except Exception:
            return False

    @classmethod
    def _default_skill_dirs(cls, extra: dict[str, Any] | None) -> list[Path]:
        if not isinstance(extra, dict):
            return []
        roots = extra.get("default_skill_roots") or []
        directories: list[Path] = []
        for raw_root in roots:
            text = str(raw_root or "").strip()
            if not text:
                continue
            try:
                root = Path(text).expanduser().resolve()
            except Exception:
                continue
            if root.is_file():
                root = root.parent
            for rel in SkillManager.DEFAULT_SKILL_DIRS:
                directories.append((root / rel).resolve())
        return directories

    @classmethod
    def _is_default_root_skill(cls, skill: SkillInfo, extra: dict[str, Any] | None) -> bool:
        if skill.source_kind == 'project':
            return False
        directories = cls._default_skill_dirs(extra)
        if not directories:
            return False
        try:
            skill_path = Path(skill.location).expanduser().resolve()
        except Exception:
            return False
        return any(cls._path_is_within(skill_path, directory) for directory in directories)

    @classmethod
    def _is_skill_visible(cls, skill: SkillInfo, extra: dict[str, Any] | None) -> bool:
        if not cls._is_default_root_skill(skill, extra):
            return cls._permission_action("skill", [skill.name], extra) != "deny"
        if not isinstance(extra, dict) or not bool(extra.get("default_skill_enabled")):
            return False
        patterns = cls._normalize_patterns(extra.get("default_skill_patterns"))
        if not patterns:
            return cls._permission_action("skill", [skill.name], extra) != "deny"
        if not any(fnmatch.fnmatch(skill.name, pattern) for pattern in patterns):
            return False
        return cls._permission_action("skill", [skill.name], extra) != "deny"

    @staticmethod
    def _related_files(skill: SkillInfo) -> list[str]:
        skill_dir = os.path.dirname(skill.location)
        related_files: list[str] = []
        try:
            for root, dirs, files in os.walk(skill_dir):
                for file in files:
                    if file != "SKILL.md":
                        related_files.append(os.path.join(root, file))
                    if len(related_files) >= 10:
                        break
                if len(related_files) >= 10:
                    break
        except Exception:
            pass
        return related_files

    @classmethod
    def _sandbox_skill_metadata(
        cls,
        skill: SkillInfo,
        context: ToolContext | None,
    ) -> dict[str, Any]:
        if context is None:
            return {}
        try:
            from ..runtime.sandbox.tool_policy import sandbox_session_enabled

            if not sandbox_session_enabled(context):
                return {}
        except Exception:
            return {}

        extra = context.extra if isinstance(context.extra, dict) else {}
        workdir = str(extra.get("workdir", "") or "").strip()
        if not workdir:
            return {}

        skill_dir = os.path.dirname(skill.location)
        # Keep workspace-local and safely copied skills on the workspace mount so
        # persistent runtimes remain eligible. A dynamic bind mount is the bounded
        # fallback for skills that exceed the normal import limits.
        staged_item = stage_resource_for_workspace(
            skill_dir,
            session_id=context.session_id,
            workdir=workdir,
        )
        if not staged_item.get("import_error") and staged_item.get("container_path"):
            return {
                "sandbox_dir": staged_item.get("container_path"),
                "host_dir": staged_item.get("source_path"),
                "workspace_dir": staged_item.get("path"),
                "sandbox_access": "workspace",
                "import_mode": staged_item.get("import_mode"),
                "imported": bool(staged_item.get("imported")),
            }

        mounted_item = mount_resource_for_workspace(
            skill_dir,
            session_id=context.session_id,
            workdir=workdir,
        )
        if mounted_item.get("import_error") or not mounted_item.get("container_path"):
            return {}

        mount = {
            "name": skill.name,
            "source_path": mounted_item.get("source_path"),
            "container_path": mounted_item.get("container_path"),
            "mount_target_path": (
                mounted_item.get("mount_target_path") or mounted_item.get("path")
            ),
            "kind": "skill",
            "read_only": True,
            "resource_type": "skill",
        }
        return {
            "sandbox_dir": mounted_item.get("container_path"),
            "host_dir": mounted_item.get("source_path"),
            "workspace_dir": mounted_item.get("path"),
            "sandbox_access": "read_only_mount",
            "import_mode": "mount",
            "import_error": staged_item.get("import_error"),
            "resource_context_patch": {"external_mounts": [mount]},
        }

    @classmethod
    def _sandbox_skill_mount_metadata(
        cls,
        skill: SkillInfo,
        context: ToolContext | None,
    ) -> dict[str, Any]:
        """Backward-compatible alias for callers using the old helper name."""

        return cls._sandbox_skill_metadata(skill, context)

    @classmethod
    def render_skill_content(
        cls,
        skill: SkillInfo,
        context: ToolContext | None = None,
    ) -> tuple[str, dict[str, Any]]:
        skill_dir = os.path.dirname(skill.location)
        sandbox_metadata = cls._sandbox_skill_metadata(skill, context)
        sandbox_dir = str(sandbox_metadata.get("sandbox_dir") or "").strip()
        display_dir = sandbox_dir or skill_dir
        related_files = cls._related_files(skill)
        display_files: list[str] = []
        for file_path in related_files:
            if sandbox_dir:
                try:
                    rel = Path(file_path).resolve().relative_to(Path(skill_dir).resolve()).as_posix()
                    display_files.append(f"{sandbox_dir.rstrip('/')}/{rel}")
                    continue
                except Exception:
                    pass
            display_files.append(file_path)
        files_content = "\n".join([f"<file>{f}</file>" for f in display_files])
        actions_content = ""
        if skill.actions:
            action_lines = ["", "Executable actions (use the `skill_run` tool):"]
            for action in skill.actions.values():
                summary = action.description or action.entrypoint
                action_lines.append(
                    f"- `{action.name}` [{action.capability}]: {summary}"
                )
                if action.arguments:
                    argument_summaries = []
                    for argument in action.arguments.values():
                        traits = [argument.type]
                        if argument.required and argument.default is None:
                            traits.append("required")
                        elif argument.default is not None:
                            traits.append(f"default={argument.default!r}")
                        else:
                            traits.append("optional")
                        traits.append(
                            "positional"
                            if argument.positional
                            else f"flag={argument.flag}"
                        )
                        if argument.must_exist:
                            traits.append("must exist")
                        if argument.artifact:
                            traits.append("output artifact")
                        if argument.aliases:
                            traits.append("aliases=" + ",".join(argument.aliases))
                        if argument.choices:
                            traits.append(
                                "choices=" + ",".join(
                                    str(choice) for choice in argument.choices
                                )
                            )
                        argument_summaries.append(
                            f"`{argument.name}` ({', '.join(traits)})"
                        )
                    action_lines.append(
                        "  Arguments: " + "; ".join(argument_summaries)
                    )
            actions_content = "\n".join(action_lines) + "\n"
        sandbox_note = ""
        sandbox_access = str(sandbox_metadata.get("sandbox_access") or "").strip()
        if sandbox_access == "read_only_mount":
            sandbox_note = (
                "\nSandbox note: this skill directory is mounted read-only inside the sandbox. "
                "Use the base directory above and the container's `python`/`python3`; "
                "do not use host Python interpreter paths or host skill paths in bash/exec commands.\n"
            )
        elif sandbox_access == "workspace":
            sandbox_note = (
                "\nSandbox note: this skill directory is available inside the active workspace. "
                "Use the base directory above and the container's `python`/`python3`; "
                "do not use host Python interpreter paths or host skill paths in bash/exec commands.\n"
            )
        output = f"""<skill_content name="{skill.name}">
# Skill: {skill.name}

{skill.content}
{actions_content}

Base directory for this skill: {display_dir}
Relative paths in this skill are relative to this base directory.
{sandbox_note}

<skill_files>
{files_content}
</skill_files>
</skill_content>"""
        metadata = {"name": skill.name, "dir": display_dir}
        if sandbox_dir:
            metadata.update(sandbox_metadata)
        else:
            metadata["host_dir"] = skill_dir
        return output, metadata

    @staticmethod
    def _record_load_event(
        *,
        context: ToolContext,
        skill_name: str,
        success: bool,
        error: str = "",
        skill: SkillInfo | None = None,
    ) -> None:
        try:
            from .routing_config import load_skill_routing_config
            from .trace import SkillRoutingTraceStore, utc_now_iso

            extra = context.extra if isinstance(context.extra, dict) else {}
            workdir = str(extra.get("workdir") or "").strip() or None
            route_trace = extra.get("skill_routing_trace")
            route_trace = route_trace if isinstance(route_trace, dict) else {}
            event = {
                "event_type": "skill_load",
                "created_at": utc_now_iso(),
                "route_request_id": route_trace.get("request_id"),
                "router_version": route_trace.get("router_version"),
                "catalog_snapshot_id": route_trace.get("catalog_snapshot_id"),
                "session_id": context.session_id,
                "message_id": context.message_id,
                "agent_name": context.agent,
                "skill_name": skill_name,
                "skill_id": getattr(skill, "skill_id", "") if skill else "",
                "success": bool(success),
                "error": str(error or "")[:500] or None,
            }
            SkillRoutingTraceStore.append_route_trace(
                event,
                workdir=workdir,
                config=load_skill_routing_config(workdir),
            )
        except Exception:
            pass

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = SkillParameters(**args)
        skill_name = parsed.name

        # Load skills if not loaded
        if not SkillManager._loaded:
            await SkillManager.load(
                workdir=context.extra.get("workdir") if context.extra else None
            )

        extra = context.extra if isinstance(context.extra, dict) else {}
        explicit_roots = [
            str(item or "").strip()
            for item in (extra.get("default_skill_roots") or [])
            if str(item or "").strip()
        ]
        if explicit_roots:
            await SkillManager.load_explicit_roots(explicit_roots)

        skill = SkillManager.get(skill_name)

        if not skill:
            available = ", ".join(
                skill_info.name
                for skill_info in SkillManager.all()
                if self._is_skill_visible(skill_info, extra)
            ) or "none"
            error = f'Skill "{skill_name}" not found. Available skills: {available}'
            self._record_load_event(
                context=context,
                skill_name=skill_name,
                success=False,
                error=error,
            )
            return ToolResult(
                title="Skill Not Found",
                output="",
                error=error,
            )
        binding = skill_runtime_binding(skill.name)
        workdir = str(extra.get("workdir") or "").strip() or None
        if (
            binding is not None
            and binding.agent.lower() != str(context.agent or "").strip().lower()
        ):
            error = (
                f'Skill "{skill.name}" is bound to dedicated agent '
                f'"{binding.agent}". Delegate with subagent_type=agent-browser.'
            )
            self._record_load_event(
                context=context,
                skill_name=skill_name,
                success=False,
                error=error,
                skill=skill,
            )
            return ToolResult(
                title="Skill Requires Dedicated Agent",
                output="",
                error=error,
                metadata={
                    "skill_name": skill.name,
                    "required_agent": binding.agent,
                    "sandbox_profile": binding.sandbox_profile,
                    "delegate_subagent_type": "agent-browser",
                },
            )
        if not skill_allowed_for_agent(skill.name, context.agent, workdir):
            error = f'Skill "{skill.name}" is disabled for agent "{context.agent}".'
            self._record_load_event(
                context=context,
                skill_name=skill_name,
                success=False,
                error=error,
                skill=skill,
            )
            return ToolResult(
                title="Skill Disabled",
                output="",
                error=error,
            )
        if not self._is_skill_visible(skill, extra):
            error = f'Skill "{skill_name}" is not allowed for agent "{context.agent}".'
            self._record_load_event(
                context=context,
                skill_name=skill_name,
                success=False,
                error=error,
                skill=skill,
            )
            return ToolResult(
                title="Skill Not Allowed",
                output="",
                error=error,
            )

        action = self._permission_action("skill", [skill_name], extra)
        if action == "deny":
            error = f'Skill "{skill_name}" is not allowed for agent "{context.agent}".'
            self._record_load_event(
                context=context,
                skill_name=skill_name,
                success=False,
                error=error,
                skill=skill,
            )
            return ToolResult(
                title="Skill Not Allowed",
                output="",
                error=error,
            )
        if action == "ask":
            approved = await context.ask(permission="skill", patterns=[skill_name])
            if not approved:
                error = f'Skill "{skill_name}" requires approval and was rejected for agent "{context.agent}".'
                self._record_load_event(
                    context=context,
                    skill_name=skill_name,
                    success=False,
                    error=error,
                    skill=skill,
                )
                return ToolResult(
                    title="Skill Approval Rejected",
                    output="",
                    error=error,
                )

        output, metadata = self.render_skill_content(skill, context)
        if binding is not None:
            metadata.update(
                {
                    "required_agent": binding.agent,
                    "sandbox_profile": binding.sandbox_profile,
                }
            )
        self._record_load_event(
            context=context,
            skill_name=skill_name,
            success=True,
            skill=skill,
        )

        return ToolResult(
            title=f"Loaded skill: {skill.name}",
            output=output,
            metadata=metadata,
        )

    @classmethod
    def register(cls):
        from ..tools.registry import ToolRegistry

        ToolRegistry.register(cls())
