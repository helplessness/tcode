"""ACP stable v1 newline JSON-RPC stdio Driver。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ... import __version__
from ...protocol.v2 import RuntimeEventValue
from ...runtime.agent_backend import NormalizedRunInput, RunContext
from ...runtime.harness_context import compose_harness_prompt
from ...runtime.interaction_store import InteractionStore
from ...runtime.tool_bridge.transports.acp_mcp import acp_mcp_server
from ..process_manager import HarnessProcessManager
from ..runtime_def import HarnessRuntimeDef
from ..security import assert_workspace, minimal_environment, render_argv
from ..session_binding import HarnessSessionBindingStore
from ..handoff_store import HarnessHandoffStore


ACP_SCHEMA_VERSION = "1.21.0"
ACP_PROTOCOL_VERSION = 1
ACP_SCHEMA_SHA256 = "caf62ff962ada396878372ced11efb2c6764e59d90919a38583c319948931a42"
ACP_STREAM_READER_LIMIT = 2_100_000
logger = logging.getLogger(__name__)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))\)")


def _native_image_prompt_blocks(
    prompt_text: str,
    *,
    workdir: str,
    enabled: bool,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    if not enabled:
        return blocks
    from ...presentation import inspect_image_file

    root = Path(workdir).resolve(strict=True)
    total = 0
    seen: set[str] = set()
    for match in _MARKDOWN_IMAGE_RE.finditer(prompt_text):
        raw = str(match.group(1) or match.group(2) or "").strip()
        candidate = root / raw.removeprefix("/workspace/") if raw.startswith("/workspace/") else Path(raw)
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            unresolved_relative = candidate.absolute().relative_to(root)
            cursor = root
            for part in unresolved_relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("image path traverses a symbolic link")
            target = candidate.resolve(strict=True)
            target.relative_to(root)
            if target.as_posix() in seen:
                continue
            inspected = inspect_image_file(target)
            size = int(inspected.get("size") or 0)
            if size <= 0 or size > 8 * 1024 * 1024 or total + size > 16 * 1024 * 1024:
                continue
            seen.add(target.as_posix())
            total += size
            blocks.append(
                {
                    "type": "image",
                    "data": base64.b64encode(target.read_bytes()).decode("ascii"),
                    "mimeType": str(inspected.get("mime_type") or "image/jpeg"),
                }
            )
        except (OSError, ValueError):
            continue
        if len(blocks) >= 3:
            break
    return blocks


def _same_harness_recovery_context(
    messages: list[Any], *, max_messages: int = 12, max_chars: int = 12_000
) -> str:
    """Build bounded canonical context when an ACP native session cannot load."""

    selected: list[str] = []
    remaining = max(1_000, int(max_chars))
    for item in reversed(list(messages or [])[:-1]):
        role = str(getattr(item, "role", "") or "").strip()
        content = getattr(item, "content", "")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        line = f"{role}: {text}"
        if len(line) > remaining:
            line = line[-remaining:]
        selected.append(line)
        remaining -= len(line)
        if len(selected) >= max(1, int(max_messages)) or remaining <= 0:
            break
    if not selected:
        return ""
    selected.reverse()
    return (
        "[TCode same-Harness recovery]\n"
        "The previous native Harness session could not be loaded. Continue from "
        "this bounded canonical conversation context; newer instructions win.\n"
        + "\n".join(selected)
        + "\n[/TCode same-Harness recovery]"
    )


class AcpProtocolError(RuntimeError):
    pass


class AcpStdioDriver:
    def __init__(
        self,
        *,
        process_manager: HarnessProcessManager,
        interaction_store: InteractionStore,
        binding_store: HarnessSessionBindingStore,
        handoff_store: HarnessHandoffStore | None = None,
    ) -> None:
        self.process_manager = process_manager
        self.interaction_store = interaction_store
        self.binding_store = binding_store
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
        argv = render_argv(runtime.invocation.args, workspace=workdir, model_id=run_input.model_id)
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv,
            cwd=workdir,
            env=minimal_environment(extra=context.platform_environment()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            # ACP uses newline-delimited JSON-RPC. Session/load and tool result
            # frames can legitimately exceed asyncio's 64 KiB default.
            limit=ACP_STREAM_READER_LIMIT,
        )
        await self.process_manager.register(context.run_id, process)
        stderr_task = asyncio.create_task(self._drain_stderr(process), name=f"acp-stderr-{context.run_id}")
        next_id = 0

        async def send(value: dict[str, Any]) -> None:
            if process.stdin is None:
                raise AcpProtocolError("ACP stdin is unavailable")
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 24_000_000:
                raise AcpProtocolError("ACP outgoing message exceeds limit")
            process.stdin.write(encoded + b"\n")
            await process.stdin.drain()

        async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal next_id
            next_id += 1
            request_id = next_id
            await send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                value = await self._read_message(process)
                if value.get("id") != request_id:
                    if "id" in value and "method" in value:
                        await send(
                            {
                                "jsonrpc": "2.0",
                                "id": value["id"],
                                "error": {"code": -32601, "message": "Method unavailable during setup"},
                            }
                        )
                    continue
                if "error" in value:
                    raise AcpProtocolError(str(value.get("error") or "ACP request failed"))
                result = value.get("result")
                return dict(result) if isinstance(result, dict) else {}

        try:
            mcp_servers = (
                [
                    acp_mcp_server(
                        socket_path=context.platform_bridge_socket,
                        token=context.platform_bridge_token,
                    )
                ]
                if context.platform_bridge_socket and context.platform_bridge_token
                else []
            )
            initialized = await request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "TCode", "version": __version__},
                },
            )
            if int(initialized.get("protocolVersion") or 0) != ACP_PROTOCOL_VERSION:
                raise AcpProtocolError("ACP protocolVersion negotiation failed")
            agent_info = initialized.get("agentInfo") if isinstance(initialized.get("agentInfo"), dict) else {}
            runtime_version = str(agent_info.get("version") or "")
            capabilities = initialized.get("agentCapabilities") if isinstance(initialized.get("agentCapabilities"), dict) else {}
            capabilities_digest = hashlib.sha256(
                json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            owner_key = f"{context.tenant_id}:{context.user_id}"
            binding = self.binding_store.get(
                owner_key=owner_key,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                runtime_id=runtime.id,
            )
            had_binding = binding is not None
            loaded_native_session = False
            session_result: dict[str, Any] = {}
            if (
                binding
                and runtime.capabilities.resume
                and str(binding.get("runtime_version") or "") == runtime_version
                and str(binding.get("capabilities_digest") or "") == capabilities_digest
            ):
                try:
                    session_result = await request(
                        "session/load",
                        {
                            "sessionId": str(binding["native_session_ref"]),
                            "cwd": workdir,
                            "mcpServers": mcp_servers,
                        },
                    )
                    session_result.setdefault("sessionId", str(binding["native_session_ref"]))
                    loaded_native_session = True
                except AcpProtocolError:
                    session_result = {}
            if not session_result:
                session_result = await request(
                    "session/new", {"cwd": workdir, "mcpServers": mcp_servers}
                )
            native_session_id = str(session_result.get("sessionId") or "")
            if not native_session_id:
                raise AcpProtocolError("ACP session did not return sessionId")
            self.binding_store.put(
                owner_key=owner_key,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                runtime_id=runtime.id,
                native_session_ref=native_session_id,
                runtime_version=runtime_version,
                capabilities_digest=capabilities_digest,
                resume_metadata={"schemaVersion": ACP_SCHEMA_VERSION},
            )
            if run_input.model_id:
                await request(
                    "session/set_config_option",
                    {"sessionId": native_session_id, "configId": "model", "value": run_input.model_id},
                )

            next_id += 1
            prompt_request_id = next_id
            composed_prompt = compose_harness_prompt(
                run_input.latest_user_text(),
                run_input.context,
                include_platform_protocol=True,
                platform_snapshot=context.platform_snapshot,
            )
            prompt_text = composed_prompt.text
            if had_binding and not loaded_native_session and not composed_prompt.handoff_included:
                recovery_context = _same_harness_recovery_context(run_input.messages)
                if recovery_context:
                    prompt_text = f"{recovery_context}\n\n{prompt_text}"
            if composed_prompt.delivery_failed and context.handoff_id:
                context.handoff_delivery_failed = True
                self.handoff_store.mark_failed(
                    context.handoff_id, error="invalid_handoff_schema"
                )
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_request_id,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": native_session_id,
                        "prompt": _native_image_prompt_blocks(
                            prompt_text,
                            workdir=workdir,
                            enabled=context.platform_snapshot.get("nativeImageInput") is True,
                        ),
                    },
                }
            )
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
                            "result": "delivered" if delivered else "already_consumed",
                        },
                        tenant_id=context.tenant_id,
                        user_id=context.user_id,
                        workspace_id=context.workspace_id,
                        session_id=context.session_id,
                        request_id=context.request_id,
                    )
                except Exception:
                    logger.debug("Harness delivery telemetry failed", exc_info=True)

            messages: dict[str, str] = {}
            message_order: list[str] = []
            tools: dict[str, dict[str, Any]] = {}
            activities: set[str] = set()
            while True:
                value = await self._read_message(process)
                if value.get("id") == prompt_request_id:
                    if "error" in value:
                        raise AcpProtocolError(str(value.get("error") or "ACP prompt failed"))
                    stop_reason = str((value.get("result") or {}).get("stopReason") or "end_turn")
                    if stop_reason == "cancelled":
                        context.cancelled.set()
                    break
                if value.get("method") == "session/update":
                    params = value.get("params") if isinstance(value.get("params"), dict) else {}
                    update = params.get("update") if isinstance(params.get("update"), dict) else {}
                    async for event in self._update_events(update, context, messages, message_order, tools, activities):
                        yield event
                    continue
                if "id" in value and value.get("method") == "session/request_permission":
                    async for event in self._handle_permission(value, send, context):
                        yield event
                    continue
                if "id" in value and "method" in value:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "id": value["id"],
                            "error": {"code": -32601, "message": "Client capability not enabled"},
                        }
                    )

            for index, message_id in enumerate(message_order):
                yield context.event(
                    "message.finished",
                    message={
                        "message_id": message_id,
                        "role": "assistant",
                        "content": messages[message_id],
                        "content_blocks": (
                            list(context.platform_content_blocks)
                            if index == len(message_order) - 1
                            else []
                        ),
                    },
                )
        finally:
            if process.returncode is None:
                await self.process_manager.terminate(process)
            await self.process_manager.unregister(context.run_id, process)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _update_events(
        self,
        update: dict[str, Any],
        context: RunContext,
        messages: dict[str, str],
        message_order: list[str],
        tools: dict[str, dict[str, Any]],
        activities: set[str],
    ) -> AsyncIterator[RuntimeEventValue]:
        kind = str(update.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            content = update.get("content") if isinstance(update.get("content"), dict) else {}
            if str(content.get("type") or "") != "text":
                return
            message_id = str(update.get("messageId") or "") or f"msg_{context.run_id}"
            if message_id not in messages:
                messages[message_id] = ""
                message_order.append(message_id)
                yield context.event("message.started", message_id=message_id, role="assistant")
            delta = str(content.get("text") or "")
            messages[message_id] += delta
            if delta:
                yield context.event("message.delta", message_id=message_id, delta=delta)
            return
        if kind == "tool_call":
            call_id = str(update.get("toolCallId") or "")
            if not call_id:
                return
            arguments = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            name = str(update.get("title") or update.get("kind") or "tool")[:300]
            tools[call_id] = {"name": name, "arguments": arguments}
            yield context.event(
                "tool.started",
                tool_call_id=call_id,
                tool_name=name,
                arguments=arguments,
            )
            yield context.event(
                "tool.args_finished",
                tool_call_id=call_id,
                arguments=arguments,
                arguments_digest="sha256:" + hashlib.sha256(
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            )
            return
        if kind == "tool_call_update":
            call_id = str(update.get("toolCallId") or "")
            if not call_id:
                return
            status = str(update.get("status") or "")
            if status not in {"completed", "failed"}:
                return
            info = tools.get(call_id)
            if info is None:
                info = {"name": str(update.get("title") or "tool")[:300], "arguments": {}}
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
            result = update.get("rawOutput")
            if result is None:
                result = update.get("content") or []
            error = "ACP tool call failed" if status == "failed" else ""
            yield context.event(
                "tool.result",
                tool_call_id=call_id,
                tool_name=info["name"],
                result=result,
                error=error,
                state="error" if error else "completed",
            )
            return
        if kind in {"plan", "agent_thought_chunk", "usage_update"}:
            activity_id = f"acp_{kind}"
            event_type = "activity.snapshot" if activity_id not in activities else "activity.delta"
            activities.add(activity_id)
            message = ""
            if kind == "agent_thought_chunk":
                content = update.get("content") if isinstance(update.get("content"), dict) else {}
                message = str(content.get("text") or "")
            yield context.event(
                event_type,
                activity_id=activity_id,
                phase=kind,
                status="running",
                message=message,
                data={key: value for key, value in update.items() if key != "sessionUpdate"},
            )

    async def _handle_permission(
        self,
        value: dict[str, Any],
        send: Any,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        options = params.get("options") if isinstance(params.get("options"), list) else []
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        owner_key = f"{context.tenant_id}:{context.user_id}"
        record = self.interaction_store.create(
            owner_key=owner_key,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            origin_run_id=context.run_id,
            tool_call_id=str(tool_call.get("toolCallId") or ""),
            kind="permission",
            reason="acp_permission",
            prompt=str(tool_call.get("title") or "外部 Agent 请求执行工具"),
            request={"toolCall": tool_call, "options": options},
            response_schema={
                "type": "object",
                "properties": {"optionId": {"type": "string", "minLength": 1}},
                "required": ["optionId"],
                "additionalProperties": False,
            },
        )
        yield context.event(
            "interaction.requested",
            interaction_id=record["interaction_id"],
            kind="permission",
            prompt=record["prompt"],
            payload=record["request"],
        )
        try:
            resolution = await self.interaction_store.wait_for_resolution(
                record["interaction_id"], owner_key=owner_key
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await send(
                {"jsonrpc": "2.0", "id": value["id"], "result": {"outcome": {"outcome": "cancelled"}}}
            )
            if context.cancelled.is_set():
                return
            raise
        option_id = str(resolution.get("optionId") or "")
        valid_ids = {str(item.get("optionId") or "") for item in options if isinstance(item, dict)}
        if option_id not in valid_ids:
            raise AcpProtocolError("permission resolution selected an unknown option")
        await send(
            {
                "jsonrpc": "2.0",
                "id": value["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )
        yield context.event(
            "interaction.resolved",
            interaction_id=record["interaction_id"],
            kind="permission",
            resolution=resolution,
        )

    @staticmethod
    async def _read_message(process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise AcpProtocolError("ACP stdout is unavailable")
        line = await asyncio.wait_for(process.stdout.readline(), timeout=300)
        if not line:
            raise AcpProtocolError(f"ACP process exited with code {process.returncode}")
        if len(line) > 2_000_000:
            raise AcpProtocolError("ACP message exceeds line limit")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcpProtocolError("ACP emitted invalid newline JSON-RPC") from exc
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise AcpProtocolError("ACP message is not a JSON-RPC 2.0 object")
        return value

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> bytes:
        if process.stderr is None:
            return b""
        collected = bytearray()
        while True:
            chunk = await process.stderr.read(8192)
            if not chunk:
                break
            if len(collected) < 128_000:
                collected.extend(chunk[: 128_000 - len(collected)])
        return bytes(collected)


__all__ = [
    "ACP_PROTOCOL_VERSION",
    "ACP_SCHEMA_SHA256",
    "ACP_SCHEMA_VERSION",
    "AcpProtocolError",
    "AcpStdioDriver",
]
