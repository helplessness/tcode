"""非 ACP Harness 的受控进程与 JSONL 事件映射。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from ...protocol.v2 import RuntimeEventValue
from ...runtime.agent_backend import NormalizedRunInput, RunContext
from ...runtime.harness_context import compose_harness_prompt
from ..handoff_store import HarnessHandoffStore
from ..process_manager import HarnessProcessManager
from ..runtime_def import HarnessRuntimeDef
from ..security import assert_workspace, minimal_environment, render_argv
from .jsonl import JsonLineDecoder, semantic_events_from_json


logger = logging.getLogger(__name__)


class ProcessJsonlDriver:
    def __init__(
        self,
        *,
        process_manager: HarnessProcessManager,
        handoff_store: HarnessHandoffStore | None = None,
    ) -> None:
        self.process_manager = process_manager
        self.handoff_store = handoff_store or HarnessHandoffStore()

    async def run(
        self,
        runtime: HarnessRuntimeDef,
        executable: str,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            raise RuntimeError('桌面外部 CLI Harness 尚未接入原生隔离')
        workdir = assert_workspace(context.workdir)
        argv = render_argv(
            runtime.invocation.args,
            workspace=workdir,
            model_id=run_input.model_id,
        )
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv,
            cwd=workdir,
            env=minimal_environment(extra=context.platform_environment()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await self.process_manager.register(context.run_id, process)
        stderr_task = asyncio.create_task(self._drain_stderr(process))
        message_id = f"msg_{context.run_id}"
        message_started = False
        text_parts: list[str] = []
        tools: dict[str, dict[str, Any]] = {}
        decoder = JsonLineDecoder()
        try:
            if process.stdin is not None:
                composed_prompt = compose_harness_prompt(
                    run_input.latest_user_text(),
                    run_input.context,
                    include_platform_protocol=True,
                    platform_snapshot=context.platform_snapshot,
                )
                if composed_prompt.delivery_failed and context.handoff_id:
                    context.handoff_delivery_failed = True
                    self.handoff_store.mark_failed(
                        context.handoff_id, error="invalid_handoff_schema"
                    )
                prompt = composed_prompt.text.encode("utf-8")
                if len(prompt) > 2_000_000:
                    raise ValueError("harness prompt exceeds byte limit")
                process.stdin.write(prompt)
                await process.stdin.drain()
                if composed_prompt.handoff_included:
                    delivered = self.handoff_store.mark_delivered(
                        composed_prompt.handoff_id, run_id=context.run_id
                    )
                    logger.info(
                        "harness_handoff_delivery handoff_id=%s runtime_id=%s run_id=%s result=%s",
                        composed_prompt.handoff_id,
                        context.runtime_id,
                        context.run_id,
                        "delivered" if delivered else "already_consumed",
                    )
                    try:
                        from ...runtime.telemetry import TelemetryGateway

                        TelemetryGateway.emit(
                            "harness.handoff.delivery",
                            {
                                "handoff_id": composed_prompt.handoff_id,
                                "runtime_id": context.runtime_id,
                                "result": (
                                    "delivered" if delivered else "already_consumed"
                                ),
                            },
                            tenant_id=context.tenant_id,
                            user_id=context.user_id,
                            workspace_id=context.workspace_id,
                            session_id=context.session_id,
                            request_id=context.request_id,
                        )
                    except Exception:
                        logger.debug("Harness delivery telemetry failed", exc_info=True)
                process.stdin.close()
            if process.stdout is None:
                raise RuntimeError("harness stdout is unavailable")
            if runtime.invocation.stream_format == "plain":
                while chunk := await process.stdout.read(8192):
                    delta = chunk.decode(errors="replace")
                    if not message_started:
                        message_started = True
                        yield context.event(
                            "message.started", message_id=message_id, role="assistant"
                        )
                    text_parts.append(delta)
                    yield context.event("message.delta", message_id=message_id, delta=delta)
            else:
                while chunk := await process.stdout.read(8192):
                    for value in decoder.feed(chunk):
                        async for event in self._semantic_events(
                            semantic_events_from_json(
                                value, runtime.invocation.stream_format
                            ),
                            context,
                            message_id,
                            text_parts,
                            tools,
                            message_started,
                        ):
                            if event.type == "message.started":
                                message_started = True
                            yield event
                for value in decoder.finish():
                    async for event in self._semantic_events(
                        semantic_events_from_json(value, runtime.invocation.stream_format),
                        context,
                        message_id,
                        text_parts,
                        tools,
                        message_started,
                    ):
                        if event.type == "message.started":
                            message_started = True
                        yield event
            exit_code = await process.wait()
            stderr = await stderr_task
            if exit_code != 0:
                diagnostic = stderr.decode(errors="replace").strip()[:4_000]
                raise RuntimeError(
                    f"harness exited with code {exit_code}: {diagnostic}"
                )
            if message_started:
                yield context.event(
                    "message.finished",
                    message={
                        "message_id": message_id,
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "content_blocks": list(context.platform_content_blocks),
                    },
                )
        finally:
            if process.returncode is None:
                await self.process_manager.terminate(process)
            await self.process_manager.unregister(context.run_id, process)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _semantic_events(
        self,
        values: list[dict[str, Any]],
        context: RunContext,
        message_id: str,
        text_parts: list[str],
        tools: dict[str, dict[str, Any]],
        message_started: bool,
    ) -> AsyncIterator[RuntimeEventValue]:
        for value in values:
            kind = str(value.get("kind") or "")
            if kind == "text":
                if not message_started:
                    message_started = True
                    yield context.event(
                        "message.started", message_id=message_id, role="assistant"
                    )
                delta = str(value.get("delta") or "")
                if delta:
                    text_parts.append(delta)
                    yield context.event(
                        "message.delta", message_id=message_id, delta=delta
                    )
            elif kind == "tool_start":
                call_id = str(value.get("id") or "") or f"tool_{len(tools) + 1}"
                name = str(value.get("name") or "tool")[:300]
                arguments = (
                    value.get("arguments")
                    if isinstance(value.get("arguments"), dict)
                    else {}
                )
                tools[call_id] = {"name": name, "arguments": arguments}
                yield context.event(
                    "tool.started",
                    tool_call_id=call_id,
                    tool_name=name,
                    message_id=message_id if message_started else "",
                    arguments=arguments,
                )
                digest = hashlib.sha256(
                    json.dumps(
                        arguments, ensure_ascii=False, sort_keys=True
                    ).encode()
                ).hexdigest()
                yield context.event(
                    "tool.args_finished",
                    tool_call_id=call_id,
                    arguments=arguments,
                    arguments_digest=f"sha256:{digest}",
                )
            elif kind == "tool_result":
                call_id = str(value.get("id") or "") or f"tool_{len(tools) + 1}"
                info = tools.get(call_id)
                if info is None:
                    info = {
                        "name": str(value.get("name") or "tool")[:300],
                        "arguments": {},
                    }
                    tools[call_id] = info
                    yield context.event(
                        "tool.started",
                        tool_call_id=call_id,
                        tool_name=info["name"],
                        arguments={},
                    )
                    yield context.event(
                        "tool.args_finished",
                        tool_call_id=call_id,
                        arguments={},
                        arguments_digest="",
                    )
                error = str(value.get("error") or "")
                yield context.event(
                    "tool.result",
                    tool_call_id=call_id,
                    tool_name=info["name"],
                    result=value.get("result"),
                    error=error,
                    state="error" if error else "completed",
                )
            elif kind == "error":
                raise RuntimeError(
                    str(value.get("message") or "harness failed")[:20_000]
                )

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> bytes:
        if process.stderr is None:
            return b""
        return (await process.stderr.read(128_001))[:128_000]


__all__ = ["ProcessJsonlDriver"]
