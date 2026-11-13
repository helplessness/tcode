"""Exec tool - start shell commands with process tracking."""

import asyncio
import os
import re
from typing import Any

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...runtime.tool_progress import report_progress_line
from ...runtime.command_policy import authorize_command, denied_command_result
from .process_runtime import ProcessRuntime
from ...skill.manager import SkillManager
from ...runtime.sandbox import (
    build_tool_sandbox_spec,
    get_sandbox_runtime,
    tool_should_run_in_sandbox,
)


class ExecParameters(ParameterSchema):
    """Parameters for exec tool."""

    command: str
    description: str | None = None
    workdir: str | None = None
    wait: bool | None = True    
    timeout: int | None = None


class ExecTool(Tool):
    """Execute a command and manage it as a long-running process."""

    @property
    def id(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return """Run commands in the full workspace execution environment.
Use this instead of `execute_code` for third-party Python packages, dependency
installation, arbitrary modules, long-running computation, or background work.
When sandbox mode is enabled, commands use the workspace sandbox runtime and may
reuse its installed user dependencies. Set wait=false for background work, then
use `process` with the returned process_id for status/output/wait/terminate.
Set wait=true to block until completion (or timeout)."""

    @property
    def parameters(self) -> type:
        return ExecParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ExecParameters(**args)

        async def output_callback(source: str, line: str, seq: int) -> None:
            await report_progress_line(
                context,
                line,
                seq=seq,
                stream=source,
            )
        # If the command targets a skill directory and the skill declares a
        # preferred python interpreter in its SKILL.md frontmatter, prefer
        # invoking that interpreter. This helps honor `compatibility.python`.
        try:
            target_dir = None
            # prefer explicit workdir
            if parsed.workdir:
                target_dir = str(parsed.workdir)
            else:
                # match leading `cd <dir> && <rest>` patterns
                m = re.match(r"^\s*cd\s+([^&;]+)\s*&&\s*(.*)$", parsed.command)
                if m:
                    target_dir = m.group(1).strip().strip('"')

            if target_dir:
                skill_md = os.path.join(target_dir, "SKILL.md")
                if os.path.isfile(skill_md):
                    try:
                        skill_info = SkillManager._parse_skill_file(skill_md)
                        if skill_info and isinstance(skill_info.metadata, dict):
                            # try nested compatibility.python, then top-level python
                            python_path = None
                            comp = skill_info.metadata.get("compatibility")
                            if isinstance(comp, dict):
                                python_path = comp.get("python")
                            # fallback to top-level key if parser flattened it
                            if not python_path:
                                python_path = skill_info.metadata.get("python")
                            if python_path and isinstance(python_path, str) and python_path.strip():
                                # replace first occurrence of plain 'python' with the full path
                                parsed_command = parsed.command
                                # handle cases where command uses 'python3' or 'python'
                                parsed_command = re.sub(r"\bpython3?\b", python_path, parsed_command, count=1)
                                parsed = ExecParameters(**{**args, "command": parsed_command})
                    except Exception:
                        pass
        except Exception:
            pass

        command_decision = await authorize_command(
            parsed.command,
            context=context,
            tool_name=self.id,
            requested_workdir=parsed.workdir,
            background=not bool(parsed.wait),
        )
        if not command_decision.allowed:
            return denied_command_result(
                command_decision,
                title=parsed.description or "Exec command",
            )

        if tool_should_run_in_sandbox(self.id, context):
            phase = "build_spec"
            job = None
            managed = None
            try:
                spec = build_tool_sandbox_spec(
                    tool_name=self.id,
                    command=parsed.command,
                    requested_workdir=parsed.workdir,
                    timeout_seconds=parsed.timeout,
                    context=context,
                    metadata={"description": parsed.description or ""},
                )
                phase = "start_job"
                runtime = get_sandbox_runtime()
                job = await runtime.start_job(spec)
                phase = "register_job"
                managed = await ProcessRuntime.register_sandbox_job(
                    job=job,
                    command=parsed.command,
                    workdir=parsed.workdir,
                    owner=ProcessRuntime.owner_from_context(context),
                    output_callback=output_callback if parsed.wait else None,
                )
                if parsed.wait:
                    phase = "wait"
                    try:
                        await ProcessRuntime.wait(managed, timeout=spec.timeout_seconds)
                    except asyncio.CancelledError:
                        await ProcessRuntime.terminate(
                            managed.process_id,
                            owner=ProcessRuntime.owner_from_context(context),
                        )
                        raise
                    except asyncio.TimeoutError:
                        phase = "timeout_output"
                        output = await ProcessRuntime.output(managed, lines=5000)
                        managed.output_callback = None
                        return ToolResult(
                            title=parsed.description or "Exec timeout",
                            output=output,
                            error=(
                                f"Process still running after {spec.timeout_seconds}s. "
                                f"Use process tool with process_id={managed.process_id}."
                            ),
                            metadata={
                                "process_id": managed.process_id,
                                "sandbox": True,
                                "sandbox_job_id": managed.sandbox_job_id,
                                "container_id": managed.sandbox_container_id,
                                "container_name": managed.sandbox_container_name,
                                "status": managed.status,
                                "timed_out": True,
                                **dict(managed.sandbox_metadata or {}),
                            },
                        )
                    phase = "final_output"
                    output = await ProcessRuntime.output(managed, lines=5000)
                    result_error = None
                    if managed.status == "timed_out":
                        result_error = "Command timed out"
                    elif managed.status == "conflict":
                        result_error = output or "Workspace writeback conflict"
                    elif managed.status in {"terminated", "missing"}:
                        result_error = (
                            f"Sandbox command ended with status {managed.status}"
                        )
                    return ToolResult(
                        title=parsed.description or "Exec completed",
                        output=output,
                        error=result_error,
                        metadata={
                            "process_id": managed.process_id,
                            "sandbox": True,
                            "sandbox_job_id": managed.sandbox_job_id,
                            "container_id": managed.sandbox_container_id,
                            "container_name": managed.sandbox_container_name,
                            "status": managed.status,
                            "returncode": managed.returncode,
                            "wait_timeout_seconds": spec.timeout_seconds,
                            **dict(managed.sandbox_metadata or {}),
                        },
                    )
                return ToolResult(
                    title=parsed.description or "Exec started",
                    output=(
                        f"Started sandbox process.\n"
                        f"process_id: {managed.process_id}\n"
                        f"sandbox_job_id: {managed.sandbox_job_id}\n"
                        f"container_id: {managed.sandbox_container_id}\n"
                        f"command: {parsed.command}"
                    ),
                    metadata={
                        "process_id": managed.process_id,
                        "sandbox": True,
                        "sandbox_job_id": managed.sandbox_job_id,
                        "container_id": managed.sandbox_container_id,
                        "container_name": managed.sandbox_container_name,
                        "status": managed.status,
                        "command": parsed.command,
                        **dict(job.metadata or {}),
                    },
                )
            except Exception as exc:
                metadata: dict[str, Any] = {
                    "sandbox": True,
                    "sandbox_phase": phase,
                    "exception_type": type(exc).__name__,
                    "retryable": phase
                    in {"start_job", "wait", "final_output", "timeout_output"},
                }
                if job is not None:
                    metadata.update(
                        {
                            "sandbox_job_id": str(
                                getattr(job, "sandbox_job_id", "") or ""
                            ),
                            "container_id": str(getattr(job, "container_id", "") or ""),
                            "container_name": str(
                                getattr(job, "container_name", "") or ""
                            ),
                        }
                    )
                if managed is not None:
                    metadata.update(
                        {
                            "process_id": managed.process_id,
                            "sandbox_job_id": managed.sandbox_job_id,
                            "container_id": managed.sandbox_container_id,
                            "container_name": managed.sandbox_container_name,
                            "status": managed.status,
                            **dict(managed.sandbox_metadata or {}),
                        }
                    )
                return ToolResult(
                    title=parsed.description or "Exec error",
                    output="",
                    error=(
                        "Sandbox execution failed during "
                        f"{phase}: {ProcessRuntime.describe_error(exc)}"
                    ),
                    metadata=metadata,
                )

        try:
            managed = await ProcessRuntime.start(
                parsed.command,
                command_decision.workdir,
                owner=ProcessRuntime.owner_from_context(context),
                output_callback=output_callback if parsed.wait else None,
            )
        except (OSError, RuntimeError) as exc:
            return ToolResult(title=parsed.description or "Exec start failed", output="", error=str(exc))

        if not parsed.wait:
            return ToolResult(
                title=parsed.description or "Exec started",
                output=(
                    f"Started process.\n"
                    f"process_id: {managed.process_id}\n"
                    f"command: {parsed.command}"
                ),
                metadata={
                    "process_id": managed.process_id,
                    "status": managed.status,
                    "command": parsed.command,
                },
            )

        timeout = max(1, min(3600, int(parsed.timeout or 120)))
        try:
            await ProcessRuntime.wait(managed, timeout=timeout)
        except asyncio.CancelledError:
            await ProcessRuntime.terminate(
                managed.process_id,
                owner=ProcessRuntime.owner_from_context(context),
            )
            raise
        except asyncio.TimeoutError:
            output = await managed.snapshot_output(lines=200)
            managed.output_callback = None
            return ToolResult(
                title=parsed.description or "Exec timeout",
                output=output,
                error=(
                    f"Process still running after {timeout}s. "
                    f"Use process tool with process_id={managed.process_id}."
                ),
                metadata={
                    "process_id": managed.process_id,
                    "status": managed.status,
                    "timed_out": True,
                },
            )

        output = await managed.snapshot_output(lines=500)
        return ToolResult(
            title=parsed.description or "Exec completed",
            output=output,
            metadata={
                "process_id": managed.process_id,
                "status": managed.status,
                "returncode": managed.returncode,
                "wait_timeout_seconds": timeout,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
