"""Run declared skill actions inside capability-selected sandboxes."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

from pydantic import Field

from ...config.manager import ConfigManager
from ...runtime.sandbox import (
    WorkspacePathMap,
    assert_inside_workspace,
    build_tool_sandbox_spec,
    get_sandbox_runtime,
    host_to_container_path,
    sandbox_session_enabled,
)
from ...runtime.sandbox.workspace_protection import (
    hidden_workspace_paths,
    read_only_workspace_paths,
    workspace_path_matches,
)
from ...skill.manager import SkillAction, SkillActionArgument, SkillManager
from ...types import ToolResult
from ...runtime.tool_progress import report_progress_line
from ..base import ParameterSchema, Tool, ToolContext
from .process_runtime import ProcessRuntime


class SkillRunParameters(ParameterSchema):
    skill: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    wait: bool = True
    timeout: int | None = None


class SkillRunTool(Tool):
    """Execute a predeclared skill entrypoint without exposing shell commands."""

    @property
    def id(self) -> str:
        return "skill_run"

    @property
    def permission_dependencies(self) -> tuple[str, ...]:
        # skill_run authorizes the target skill through the `skill` permission.
        return ("skill",)

    @property
    def description(self) -> str:
        executable = []
        for skill in SkillManager.all():
            if not skill.actions:
                continue
            action_names = ", ".join(skill.actions.keys())
            executable.append(f"- {skill.name}: {action_names}")
            if len(executable) >= 12:
                break
        catalog = "\n".join(executable) or "- No executable skill actions are currently loaded."
        return (
            "Run a declared executable action from a skill in its configured sandbox.\n"
            "The action, arguments, capability, and entrypoint are validated by the server. "
            "Do not pass shell commands or sandbox profile names.\n\n"
            f"Executable skills:\n{catalog}"
        )

    @property
    def parameters(self) -> type:
        return SkillRunParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = SkillRunParameters(**args)
        if not sandbox_session_enabled(context):
            return self._error(
                parsed,
                "skill_run requires sandbox mode",
                sandbox_required=True,
            )

        workdir = self._context_workdir(context)
        await SkillManager.load(workdir=workdir)
        skill = SkillManager.get(parsed.skill)
        if skill is None:
            return self._error(parsed, f'Skill "{parsed.skill}" was not found')

        from ...skill.tool import SkillTool
        from ...skill.runtime_binding import skill_allowed_for_agent

        extra = context.extra if isinstance(context.extra, dict) else {}
        if not skill_allowed_for_agent(skill.name, context.agent, workdir):
            return self._error(
                parsed,
                f'Skill "{skill.name}" is disabled or unavailable for agent "{context.agent}"',
            )
        if not SkillTool._is_skill_visible(skill, extra):
            return self._error(
                parsed,
                f'Skill "{skill.name}" is not allowed for agent "{context.agent}"',
            )
        permission_action = SkillTool._permission_action("skill", [skill.name], extra)
        if permission_action == "deny":
            return self._error(
                parsed,
                f'Skill "{skill.name}" is not allowed for agent "{context.agent}"',
            )
        if permission_action == "ask":
            approved = await context.ask(permission="skill", patterns=[skill.name])
            if not approved:
                return self._error(
                    parsed,
                    f'Skill "{skill.name}" approval was rejected',
                )

        action = skill.actions.get(parsed.action)
        if action is None:
            available = ", ".join(skill.actions.keys()) or "none"
            return self._error(
                parsed,
                f'Action "{parsed.action}" is not declared by skill "{skill.name}". '
                f"Available actions: {available}",
            )

        ConfigManager.load(workdir)
        profile = ConfigManager.get_sandbox_capability_profile(
            action.capability,
            workdir,
        )
        if not profile:
            return self._error(
                parsed,
                f'No sandbox profile is mapped for capability "{action.capability}"',
                sandbox_capability=action.capability,
            )
        if ConfigManager.get_sandbox_profile(profile, workdir) is None:
            return self._error(
                parsed,
                f'Sandbox profile "{profile}" mapped from capability '
                f'"{action.capability}" does not exist',
                sandbox_capability=action.capability,
                sandbox_profile=profile,
            )

        # Desktop snapshots the selected skill with the native runtime. Avoid
        # the generic resource importer, which copies external trees on the
        # host before the Seatbelt input checks can inspect them.
        sandbox_metadata = (
            {"sandbox_dir": "/skill", "skill_source_dir": str(Path(skill.location).parent)}
            if os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
            else SkillTool._sandbox_skill_metadata(skill, context)
        )
        sandbox_dir = str(sandbox_metadata.get("sandbox_dir") or "").strip()
        if not sandbox_dir:
            return self._error(
                parsed,
                "Unable to mount the skill directory into the sandbox",
                sandbox_capability=action.capability,
                sandbox_profile=profile,
            )

        try:
            entrypoint = self._validate_entrypoint(skill.location, action)
            command_parts, artifacts = self._build_command(
                action=action,
                values=parsed.arguments,
                workdir=workdir,
                sandbox_dir=sandbox_dir,
                entrypoint=entrypoint,
            )
            execution_context = self._context_with_skill_mount(
                context,
                sandbox_metadata,
            )
            timeout = max(
                1,
                min(
                    24 * 3600,
                    int(parsed.timeout or action.timeout_seconds),
                ),
            )
            spec = build_tool_sandbox_spec(
                tool_name=self.id,
                command=shlex.join(command_parts),
                requested_workdir=workdir,
                timeout_seconds=timeout,
                context=execution_context,
                expected_artifacts=[
                    item["container_path"] for item in artifacts
                ],
                sandbox_profile=profile,
                metadata={
                    "skill": skill.name,
                    "skill_action": action.name,
                    "sandbox_capability": action.capability,
                    "skill_source": skill.source or "",
                    "skill_plugin": skill.plugin or "",
                    **({"desktop_skill_source_dir": sandbox_metadata["skill_source_dir"]}
                       if "skill_source_dir" in sandbox_metadata else {}),
                },
            )
        except Exception as exc:
            return self._error(
                parsed,
                str(exc),
                sandbox_capability=action.capability,
                sandbox_profile=profile,
            )

        runtime = get_sandbox_runtime()
        if not parsed.wait:
            try:
                job = await runtime.start_job(spec)
                managed = await ProcessRuntime.register_sandbox_job(
                    job=job,
                    command=spec.command,
                    workdir=workdir,
                    owner=ProcessRuntime.owner_from_context(context),
                )
            except Exception as exc:
                return self._error(
                    parsed,
                    f"Skill action failed to start: {exc}",
                    sandbox_capability=action.capability,
                    sandbox_profile=profile,
                )
            return ToolResult(
                title=f"{skill.name}:{action.name}",
                output=(
                    "Started sandboxed skill action.\n"
                    f"process_id: {managed.process_id}\n"
                    f"sandbox_job_id: {managed.sandbox_job_id}"
                ),
                metadata={
                    "sandbox": True,
                    "skill": skill.name,
                    "skill_action": action.name,
                    "sandbox_capability": action.capability,
                    "sandbox_profile": profile,
                    "process_id": managed.process_id,
                    "sandbox_job_id": managed.sandbox_job_id,
                    "container_id": managed.sandbox_container_id,
                    "status": managed.status,
                    "artifacts": artifacts,
                    **dict(job.metadata or {}),
                },
            )

        async def output_callback(source: str, line: str, seq: int) -> None:
            await report_progress_line(
                context,
                line,
                seq=seq,
                stream=source,
            )

        try:
            job = await runtime.start_job(spec)
            managed = await ProcessRuntime.register_sandbox_job(
                job=job,
                command=spec.command,
                workdir=workdir,
                owner=ProcessRuntime.owner_from_context(context),
                output_callback=output_callback,
            )
            try:
                await ProcessRuntime.wait(managed, timeout=timeout)
            except asyncio.CancelledError:
                await ProcessRuntime.terminate(
                    managed.process_id,
                    owner=ProcessRuntime.owner_from_context(context),
                )
                raise
            except asyncio.TimeoutError:
                await ProcessRuntime.terminate(
                    managed.process_id,
                    owner=ProcessRuntime.owner_from_context(context),
                )
                output = await ProcessRuntime.output(managed, lines=5000)
                return ToolResult(
                    title=f"{skill.name}:{action.name}",
                    output=output,
                    output_path=artifacts[0]["host_path"] if artifacts else None,
                    error="Skill action timed out",
                    metadata={
                        "sandbox": True,
                        "skill": skill.name,
                        "skill_action": action.name,
                        "sandbox_capability": action.capability,
                        "sandbox_profile": profile,
                        "sandbox_status": "timed_out",
                        "process_id": managed.process_id,
                        "sandbox_job_id": managed.sandbox_job_id,
                        "container_id": managed.sandbox_container_id,
                        "container_name": managed.sandbox_container_name,
                        "exitCode": managed.returncode,
                        "artifacts": artifacts,
                        **dict(managed.sandbox_metadata or {}),
                    },
                )
        except Exception as exc:
            return self._error(
                parsed,
                f"Skill action sandbox execution failed: {exc}",
                sandbox_capability=action.capability,
                sandbox_profile=profile,
            )

        output = await ProcessRuntime.output(managed, lines=5000)
        error = None
        if managed.status == "timed_out":
            error = "Skill action timed out"
        elif managed.status == "conflict":
            error = output or "Workspace writeback conflict"
        elif managed.status != "completed" or managed.returncode not in (0, None):
            error = output or f"Skill action failed with status {managed.status}"

        output_path = artifacts[0]["host_path"] if artifacts else None
        if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
            published = managed.sandbox_metadata.get("artifacts") if managed.sandbox_metadata else None
            if isinstance(published, list) and published:
                output_path = str(Path(workdir) / str(published[0]))

        if output_path and not error:
            # stdout paths belong to the execution sandbox. Downstream tools
            # must use the artifact actually published back to this workspace.
            output += '\n' + json.dumps({'output_path': output_path}, ensure_ascii=False)

        return ToolResult(
            title=f"{skill.name}:{action.name}",
            output=output,
            output_path=output_path,
            error=error,
            metadata={
                "sandbox": True,
                "skill": skill.name,
                "skill_action": action.name,
                "sandbox_capability": action.capability,
                "sandbox_profile": profile,
                "sandbox_status": managed.status,
                "process_id": managed.process_id,
                "sandbox_job_id": managed.sandbox_job_id,
                "container_id": managed.sandbox_container_id,
                "container_name": managed.sandbox_container_name,
                "exitCode": managed.returncode,
                "artifacts": artifacts,
                **dict(managed.sandbox_metadata or {}),
            },
        )

    @staticmethod
    def _context_workdir(context: ToolContext) -> str:
        extra = context.extra if isinstance(context.extra, dict) else {}
        return str(extra.get("workdir") or os.getcwd())

    @staticmethod
    def _validate_entrypoint(skill_file: str, action: SkillAction) -> Path:
        skill_dir = Path(skill_file).expanduser().resolve().parent
        entrypoint = (skill_dir / action.entrypoint).resolve(strict=True)
        assert_inside_workspace(entrypoint, skill_dir)
        if not entrypoint.is_file():
            raise ValueError(
                f'Entrypoint "{action.entrypoint}" is not a regular file'
            )
        return entrypoint

    @classmethod
    def _build_command(
        cls,
        *,
        action: SkillAction,
        values: dict[str, Any],
        workdir: str,
        sandbox_dir: str,
        entrypoint: Path,
    ) -> tuple[list[str], list[dict[str, str]]]:
        alias_map: dict[str, str] = {}
        for canonical_name, definition in action.arguments.items():
            for alias in definition.aliases:
                alias_map[alias] = canonical_name

        resolved_values: dict[str, Any] = {}
        unknown: list[str] = []
        for key, value in values.items():
            target_name = key if key in action.arguments else alias_map.get(key)
            if not target_name:
                unknown.append(key)
                continue
            if target_name in resolved_values and resolved_values[target_name] != value:
                raise ValueError(
                    f'Conflicting values for action argument "{target_name}"'
                )
            resolved_values[target_name] = value

        unknown = sorted(set(unknown))
        if unknown:
            raise ValueError(
                "Unknown action arguments: " + ", ".join(unknown)
            )

        interpreter = cls._interpreter(action.interpreter, entrypoint.suffix)
        container_entrypoint = (
            f"{sandbox_dir.rstrip('/')}/{action.entrypoint.lstrip('/')}"
        )
        command = [*interpreter, container_entrypoint]
        artifacts: list[dict[str, str]] = []

        for name, definition in action.arguments.items():
            value = resolved_values.get(name) if name in resolved_values else definition.default
            if value is None:
                if definition.required:
                    raise ValueError(f'Action argument "{name}" is required')
                continue
            normalized, path_artifacts = cls._normalize_argument(
                definition,
                value,
                workdir=workdir,
            )
            artifacts.extend(path_artifacts)

            if definition.type == "boolean":
                if normalized:
                    if definition.positional:
                        command.append("true")
                    else:
                        command.append(definition.flag)
                continue

            items = normalized if isinstance(normalized, list) else [normalized]
            if definition.positional:
                command.extend(str(item) for item in items)
            else:
                command.append(definition.flag)
                command.extend(str(item) for item in items)

        return command, artifacts

    @staticmethod
    def _interpreter(configured: str, suffix: str) -> list[str]:
        value = str(configured or "auto").strip().lower()
        if value == "auto":
            if suffix.lower() == ".py":
                return ["python3"]
            if suffix.lower() in {".sh", ".bash"}:
                return ["bash"]
            return []
        allowed = {
            "python": ["python"],
            "python3": ["python3"],
            "node": ["node"],
            "bash": ["bash"],
            "sh": ["sh"],
            "direct": [],
        }
        if value not in allowed:
            raise ValueError(f"Unsupported skill action interpreter: {value}")
        return allowed[value]

    @classmethod
    def _normalize_argument(
        cls,
        definition: SkillActionArgument,
        value: Any,
        *,
        workdir: str,
    ) -> tuple[Any, list[dict[str, str]]]:
        arg_type = definition.type
        artifacts: list[dict[str, str]] = []

        if arg_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(
                    f'Argument "{definition.name}" must be a boolean'
                )
            normalized: Any = value
        elif arg_type == "integer":
            if isinstance(value, bool):
                raise ValueError(
                    f'Argument "{definition.name}" must be an integer'
                )
            try:
                normalized = int(value)
            except Exception as exc:
                raise ValueError(
                    f'Argument "{definition.name}" must be an integer'
                ) from exc
        elif arg_type == "number":
            if isinstance(value, bool):
                raise ValueError(
                    f'Argument "{definition.name}" must be a number'
                )
            try:
                normalized = float(value)
            except Exception as exc:
                raise ValueError(
                    f'Argument "{definition.name}" must be a number'
                ) from exc
        elif arg_type in {"path", "path_list"}:
            raw_items = value if arg_type == "path_list" else [value]
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError(
                    f'Argument "{definition.name}" must be a non-empty path list'
                )
            normalized_paths = []
            for raw_path in raw_items:
                host_path, container_path = cls._workspace_path(
                    raw_path,
                    workdir=workdir,
                    must_exist=definition.must_exist,
                )
                normalized_paths.append(container_path)
                if definition.artifact:
                    cls._assert_writable_artifact_path(
                        host_path,
                        workdir=workdir,
                        container_path=container_path,
                    )
                    artifacts.append(
                        {
                            "argument": definition.name,
                            "host_path": host_path.as_posix(),
                            "container_path": container_path,
                        }
                    )
            normalized = (
                normalized_paths if arg_type == "path_list" else normalized_paths[0]
            )
        elif arg_type == "string_list":
            if not isinstance(value, list) or not value:
                raise ValueError(
                    f'Argument "{definition.name}" must be a non-empty string list'
                )
            normalized = [str(item) for item in value]
        else:
            if isinstance(value, (dict, list)):
                raise ValueError(
                    f'Argument "{definition.name}" must be a string'
                )
            normalized = str(value)

        if definition.choices:
            choice_values = normalized if isinstance(normalized, list) else [normalized]
            invalid = [item for item in choice_values if item not in definition.choices]
            if invalid:
                raise ValueError(
                    f'Argument "{definition.name}" must be one of '
                    + ", ".join(str(item) for item in definition.choices)
                )
        return normalized, artifacts

    @staticmethod
    def _assert_writable_artifact_path(
        host_path: Path,
        *,
        workdir: str,
        container_path: str,
    ) -> None:
        workspace = Path(workdir).expanduser().resolve(strict=True)
        relative = host_path.relative_to(workspace).as_posix()
        protected_root = workspace_path_matches(relative, hidden_workspace_paths())
        access = "hidden control" if protected_root else ""
        if not protected_root:
            protected_root = workspace_path_matches(
                relative,
                read_only_workspace_paths(),
            )
            access = "read-only configuration" if protected_root else ""
        if protected_root:
            raise ValueError(
                "Skill action output path is not writable in the sandbox: "
                f"{container_path} is under {access} path {protected_root!r}. "
                "Use /workspace/tmp/... or /workspace/artifacts/... instead."
            )

    @staticmethod
    def _workspace_path(
        value: Any,
        *,
        workdir: str,
        must_exist: bool,
    ) -> tuple[Path, str]:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
            raise ValueError("Skill action path is empty or invalid")

        workspace = Path(workdir).expanduser().resolve(strict=True)
        container_root = str(
            os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace")
            or "/workspace"
        ).rstrip("/") or "/workspace"
        pathmap = WorkspacePathMap(
            workspace_id=workspace.name or "workspace",
            host_workspace=workspace,
            container_workspace=container_root,
        )

        if raw == container_root:
            host_path = workspace
        elif raw.startswith(f"{container_root}/"):
            host_path = workspace / raw[len(container_root) + 1 :]
        else:
            candidate = Path(raw).expanduser()
            host_path = candidate if candidate.is_absolute() else workspace / candidate
        host_path = assert_inside_workspace(host_path, workspace)
        if must_exist and not host_path.exists():
            raise ValueError(f"Required workspace path does not exist: {raw}")
        return host_path, host_to_container_path(host_path, pathmap)

    @staticmethod
    def _context_with_skill_mount(
        context: ToolContext,
        mount_metadata: dict[str, Any],
    ) -> ToolContext:
        extra = dict(context.extra or {})
        resource_context = dict(extra.get("resource_context") or {})
        patch = dict(mount_metadata.get("resource_context_patch") or {})
        current_mounts = list(resource_context.get("external_mounts") or [])
        incoming_mounts = list(patch.get("external_mounts") or [])
        deduped = {}
        for item in [*current_mounts, *incoming_mounts]:
            if not isinstance(item, dict):
                continue
            key = str(
                item.get("container_path")
                or item.get("source_path")
                or ""
            ).strip()
            if key:
                deduped[key] = dict(item)
        resource_context["external_mounts"] = list(deduped.values())
        extra["resource_context"] = resource_context
        return ToolContext(
            session_id=context.session_id,
            message_id=context.message_id,
            agent=context.agent,
            abort=context.abort,
            call_id=context.call_id,
            extra=extra,
            messages=context.messages,
        )

    @staticmethod
    def _error(
        parsed: SkillRunParameters,
        error: str,
        **metadata: Any,
    ) -> ToolResult:
        return ToolResult(
            title=f"{parsed.skill}:{parsed.action}",
            output="",
            error=error,
            metadata={
                "sandbox": True,
                "skill": parsed.skill,
                "skill_action": parsed.action,
                **metadata,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
