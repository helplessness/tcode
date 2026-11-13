"""Bash tool - Execute shell commands"""

import asyncio
import codecs
import os
from typing import Any

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...runtime.tool_progress import report_progress_line
from ...runtime.output_budget import BoundedTextBuffer, bound_text
from ...runtime.command_policy import authorize_command, denied_command_result
from ...runtime.local_process import start_local_process, stop_local_process
from ...runtime.sandbox import (
    build_tool_sandbox_spec,
    get_sandbox_runtime,
    tool_should_run_in_sandbox,
)
from .process_runtime import ProcessRuntime

class BashParameters(ParameterSchema):
    """Parameters for bash tool"""

    command: str
    description: str | None = None
    workdir: str | None = None
    timeout: int | None = None


class BashTool(Tool):
    """Execute shell commands"""

    @property
    def id(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            return ('Execute one offline command in a private copy of the current workspace using macOS Seatbelt. '
                    'Use relative paths and workdir; /workspace is not a native filesystem mount. '
                    'Use python3 -I -S for standard-library file processing. Fork, pipelines, background jobs, '
                    'network, application control, third-party packages and access outside this task are unavailable. '
                    'Prefer read/write/edit/glob/grep for file operations. Inputs are never modified in place; '
                    'changed command files are saved under artifacts/local/<job-id> and reported in metadata. '
                    'Maximum 120 seconds, 2000 input files / 64 MiB. Do not retry a denied operation on the host.')
        return """Execute a shell command.

Use this for: git, npm, docker, python, or other CLI tools.
NOT for file operations - use read/write/edit/grep/glob instead.

Parameters:
- command: The shell command to execute
- description: Brief description (5-10 words)
- workdir: Working directory (use instead of cd)
- timeout: Timeout in seconds (default: 120)

Important:
- Use workdir parameter instead of "cd dir && cmd"
- Quote paths with spaces: "path/to/file"
- Output is auto-truncated - don't use head/tail
- For git commits, follow the git workflow below

Git Commit Workflow:
1. Run: git status, git diff, git log -1
2. Analyze changes and draft commit message
3. Run: git add <files>
4. Run: git commit -m "type: description"
5. Run: git status to verify
NEVER: update git config, skip hooks, force push, use -i flag

Parallel Commands:
- Independent commands: call bash multiple times in one response
- Sequential commands: use && to chain (git add . && git commit -m "msg")
"""

    @property
    def parameters(self) -> type:
        return BashParameters

    @staticmethod
    def _capture_limit() -> int:
        try:
            value = int(os.environ.get("TCODE_PROCESS_CAPTURE_MAX_CHARS", "131072"))
        except Exception:
            value = 131072
        return max(4096, min(value, 8 * 1024 * 1024))

    @classmethod
    def _bounded_output(cls, output: str) -> tuple[str, dict[str, Any], bool]:
        bounded = bound_text(
            output,
            max_chars=cls._capture_limit(),
            label="process output truncated",
        )
        metadata = {
            "outputOriginalChars": bounded.original_chars,
            "outputShownChars": bounded.shown_chars,
            "outputCaptureLimitChars": cls._capture_limit(),
            "outputCaptureTruncated": bounded.truncated,
        }
        return bounded.text, metadata, bounded.truncated

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = BashParameters(**args)
        command_decision = await authorize_command(
            parsed.command,
            context=context,
            tool_name=self.id,
            requested_workdir=parsed.workdir,
        )
        if not command_decision.allowed:
            return denied_command_result(
                command_decision,
                title=parsed.description or "Bash command",
            )

        async def output_callback(source: str, line: str, seq: int) -> None:
            await report_progress_line(
                context,
                line,
                seq=seq,
                stream=source,
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
                    output_callback=output_callback,
                )
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
                    phase = "timeout_terminate"
                    await ProcessRuntime.terminate(
                        managed.process_id,
                        owner=ProcessRuntime.owner_from_context(context),
                    )
                    phase = "timeout_output"
                    output = await ProcessRuntime.output(managed, lines=5000)
                    output, output_metadata, output_truncated = self._bounded_output(output)
                    return ToolResult(
                        title=parsed.description or "Bash command",
                        output=output,
                        error="Command timed out",
                        metadata={
                            "exitCode": managed.returncode,
                            "sandbox": True,
                            "sandbox_status": "timed_out",
                            "process_id": managed.process_id,
                            "sandbox_job_id": managed.sandbox_job_id,
                            "container_name": managed.sandbox_container_name,
                            **output_metadata,
                            **dict(managed.sandbox_metadata or {}),
                        },
                        truncated=output_truncated,
                    )

                phase = "final_output"
                output = await ProcessRuntime.output(managed, lines=5000)
                output, output_metadata, output_truncated = self._bounded_output(output)
                metadata = {
                    "exitCode": managed.returncode,
                    "sandbox": True,
                    "sandbox_status": managed.status,
                    "process_id": managed.process_id,
                    "sandbox_job_id": managed.sandbox_job_id,
                    "container_name": managed.sandbox_container_name,
                    **output_metadata,
                    **dict(managed.sandbox_metadata or {}),
                }
                error = None
                if managed.status == "timed_out":
                    error = "Command timed out"
                elif managed.status == "conflict":
                    error = output or "Workspace writeback conflict"
                elif managed.status in {"terminated", "missing"}:
                    error = f"Sandbox command ended with status {managed.status}"
                elif managed.returncode not in (0, None):
                    metadata["failed_in_sandbox"] = True
                return ToolResult(
                    title=parsed.description or "Bash command",
                    output=output,
                    error=error,
                    metadata=metadata,
                    truncated=output_truncated,
                )
            except Exception as exc:
                metadata: dict[str, Any] = {
                    "sandbox": True,
                    "sandbox_phase": phase,
                    "exception_type": type(exc).__name__,
                    "retryable": phase in {
                        "start_job",
                        "wait",
                        "final_output",
                        "timeout_output",
                    },
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
                    title=parsed.description or "Bash command",
                    output="",
                    error=(
                        "Sandbox execution failed during "
                        f"{phase}: {ProcessRuntime.describe_error(exc)}"
                    ),
                    metadata=metadata,
                )

        capture_limit = self._capture_limit()
        stdout_buffer = BoundedTextBuffer(capture_limit)
        stderr_buffer = BoundedTextBuffer(capture_limit)
        proc: asyncio.subprocess.Process | None = None
        seq = 0

        async def terminate_process() -> bool:
            if proc is None:
                return True
            try:
                return await stop_local_process(proc, grace=0, discard_output=True)
            except OSError:
                return False

        try:
            proc = await start_local_process(
                parsed.command,
                cwd=command_decision.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def drain_stream(
                stream: asyncio.StreamReader | None,
                source: str,
                target: BoundedTextBuffer,
            ) -> None:
                nonlocal seq
                if stream is None:
                    return
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                progress_pending = ""
                while True:
                    chunk = await stream.read(64 * 1024)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    target.append(text)
                    progress_pending += text
                    while "\n" in progress_pending:
                        line, progress_pending = progress_pending.split("\n", 1)
                        seq += 1
                        await output_callback(source, line[:4096], seq)
                    if len(progress_pending) > 4096:
                        # Structured progress records are line based. Retaining a
                        # small tail avoids an oversized no-newline stream from
                        # becoming another unbounded buffer.
                        progress_pending = progress_pending[-4096:]
                final_text = decoder.decode(b"", final=True)
                target.append(final_text)
                progress_pending += final_text
                if progress_pending:
                    seq += 1
                    await output_callback(source, progress_pending, seq)

            await asyncio.wait_for(
                asyncio.gather(
                    drain_stream(proc.stdout, "stdout", stdout_buffer),
                    drain_stream(proc.stderr, "stderr", stderr_buffer),
                    proc.wait(),
                ),
                timeout=parsed.timeout or 120,
            )
            stdout_result = stdout_buffer.render(label="stdout truncated")
            stderr_result = stderr_buffer.render(label="stderr truncated")
            combined = bound_text(
                stdout_result.text + stderr_result.text,
                max_chars=capture_limit,
                label="process output truncated",
            )
            was_truncated = (
                stdout_result.truncated
                or stderr_result.truncated
                or combined.truncated
            )
            return ToolResult(
                title=parsed.description or "Bash command",
                output=combined.text,
                metadata={
                    "exitCode": proc.returncode,
                    "outputOriginalChars": (
                        stdout_result.original_chars + stderr_result.original_chars
                    ),
                    "stdoutOriginalChars": stdout_result.original_chars,
                    "stderrOriginalChars": stderr_result.original_chars,
                    "outputShownChars": combined.shown_chars,
                    "outputCaptureLimitChars": capture_limit,
                    "outputCaptureTruncated": was_truncated,
                },
                truncated=was_truncated,
            )
        except asyncio.CancelledError:
            await terminate_process()
            raise
        except asyncio.TimeoutError:
            stopped = await terminate_process()
            stdout_result = stdout_buffer.render(label="stdout truncated")
            stderr_result = stderr_buffer.render(label="stderr truncated")
            combined = bound_text(
                stdout_result.text + stderr_result.text,
                max_chars=capture_limit,
                label="process output truncated",
            )
            was_truncated = (
                stdout_result.truncated
                or stderr_result.truncated
                or combined.truncated
            )
            return ToolResult(
                title=parsed.description or "Bash command",
                output=combined.text,
                error="Command timed out" if stopped else "Command timed out; process exit not confirmed",
                truncated=was_truncated,
                metadata={
                    "outputOriginalChars": (
                        stdout_result.original_chars + stderr_result.original_chars
                    ),
                    "stdoutOriginalChars": stdout_result.original_chars,
                    "stderrOriginalChars": stderr_result.original_chars,
                    "outputShownChars": combined.shown_chars,
                    "outputCaptureLimitChars": capture_limit,
                    "outputCaptureTruncated": was_truncated,
                },
            )
        except Exception as e:
            stopped = await terminate_process()
            return ToolResult(
                title=parsed.description or "Bash command", output="",
                error=str(e) if stopped else f"{e}; process exit not confirmed",
            )

    @classmethod
    def register(cls):
        """Register this tool"""
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
