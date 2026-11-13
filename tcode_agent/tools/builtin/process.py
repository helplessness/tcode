"""Process tool - manage long-running processes started by exec."""

import asyncio
from typing import Any, Optional

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from .process_runtime import ProcessRuntime


class ProcessParameters(ParameterSchema):
    """Parameters for process tool."""

    action: str
    process_id: Optional[str] = None
    lines: int | None = 200
    timeout: int | None = 120


class ProcessTool(Tool):
    """Inspect/control process lifecycle for commands started via exec."""

    @property
    def id(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return """Manage long-running or background processes started by `exec(wait=false)`.
Actions:
- list: list all tracked processes
- status: get one process status
- output: get latest output lines
- wait: wait for process completion
- terminate: stop a running process"""

    @property
    def parameters(self) -> type:
        return ProcessParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ProcessParameters(**args)
        action = parsed.action.strip().lower()
        owner = ProcessRuntime.owner_from_context(context)

        if action == "list":
            items = await ProcessRuntime.list(owner=owner)
            if not items:
                return ToolResult(title="Process list", output="No tracked processes.")
            lines = ["Tracked processes:"]
            for p in items:
                if ProcessRuntime.is_sandbox(p):
                    await ProcessRuntime.refresh(p)
                lines.append(
                    f"- {p.process_id} | {p.status} | rc={p.returncode} | cmd={p.command}"
                )
            return ToolResult(title="Process list", output="\n".join(lines))

        if action in {"status", "output", "wait", "terminate"} and not parsed.process_id:
            return ToolResult(
                title="Process error",
                output="",
                error="process_id is required for this action",
            )

        if action == "status":
            return await self._status(parsed.process_id, owner)
        if action == "output":
            return await self._output(parsed.process_id, parsed.lines or 200, owner)
        if action == "wait":
            return await self._wait(parsed.process_id, parsed.timeout or 120, owner)
        if action == "terminate":
            return await self._terminate(parsed.process_id, owner)

        return ToolResult(
            title="Process error",
            output="",
            error=f"Unsupported action: {parsed.action}",
        )

    async def _status(
        self,
        process_id: str,
        owner: dict[str, str],
    ) -> ToolResult:
        managed = await ProcessRuntime.get(process_id, owner=owner)
        if not managed:
            return ToolResult(title="Process status", output="", error="process not found")
        if ProcessRuntime.is_sandbox(managed):
            await ProcessRuntime.refresh(managed)
        return ToolResult(
            title="Process status",
            output=(
                f"process_id: {managed.process_id}\n"
                f"status: {managed.status}\n"
                f"returncode: {managed.returncode}\n"
                f"command: {managed.command}"
                + (
                    f"\nsandbox_job_id: {managed.sandbox_job_id}\n"
                    f"container_id: {managed.sandbox_container_id}"
                    if ProcessRuntime.is_sandbox(managed)
                    else ""
                )
            ),
            metadata={
                "process_id": managed.process_id,
                "status": managed.status,
                "returncode": managed.returncode,
                "sandbox": ProcessRuntime.is_sandbox(managed),
                "sandbox_job_id": managed.sandbox_job_id,
                "container_id": managed.sandbox_container_id,
            },
        )

    async def _output(
        self,
        process_id: str,
        lines: int,
        owner: dict[str, str],
    ) -> ToolResult:
        managed = await ProcessRuntime.get(process_id, owner=owner)
        if not managed:
            return ToolResult(title="Process output", output="", error="process not found")
        content = await ProcessRuntime.output(managed, lines=lines)
        if not content:
            content = "(no output yet)"
        return ToolResult(
            title="Process output",
            output=content,
            metadata={
                "process_id": managed.process_id,
                "status": managed.status,
                "sandbox": ProcessRuntime.is_sandbox(managed),
                "sandbox_job_id": managed.sandbox_job_id,
            },
        )

    async def _wait(
        self,
        process_id: str,
        timeout: int,
        owner: dict[str, str],
    ) -> ToolResult:
        managed = await ProcessRuntime.get(process_id, owner=owner)
        if not managed:
            return ToolResult(title="Process wait", output="", error="process not found")
        wait_timeout = max(1, min(3600, int(timeout)))
        try:
            await ProcessRuntime.wait(managed, timeout=wait_timeout)
        except asyncio.TimeoutError:
            return ToolResult(
                title="Process wait",
                output="",
                error=f"wait timeout after {wait_timeout}s",
                metadata={"process_id": managed.process_id, "status": managed.status},
            )
        if managed.status == "running" and managed.proc is not None:
            managed.returncode = managed.proc.returncode
            managed.status = "completed" if (managed.proc.returncode or 0) == 0 else "failed"
        content = await ProcessRuntime.output(managed, lines=500)
        return ToolResult(
            title="Process completed",
            output=content,
            metadata={
                "process_id": managed.process_id,
                "status": managed.status,
                "returncode": managed.returncode,
                "sandbox": ProcessRuntime.is_sandbox(managed),
                "sandbox_job_id": managed.sandbox_job_id,
            },
        )

    async def _terminate(
        self,
        process_id: str,
        owner: dict[str, str],
    ) -> ToolResult:
        managed = await ProcessRuntime.get(process_id, owner=owner)
        if managed is None:
            return ToolResult(title="Process terminate", output="", error="process not found")
        try:
            ok = await ProcessRuntime.terminate(process_id, owner=owner)
        except (OSError, RuntimeError):
            ok = False
        return ToolResult(
            title="Process terminate",
            output=f"Process status: {managed.status}" if ok else "",
            error=None if ok else "Process exit not confirmed",
            metadata={"process_id": process_id, "status": managed.status},
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
