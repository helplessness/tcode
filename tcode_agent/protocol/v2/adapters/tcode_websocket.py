"""把现有运行时通知归一化为 TCode v2 WebSocket 事件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..emitter import ReplayWindow, RuntimeEventEmitter


def _text(value: Any, *, limit: int = 20_000) -> str:
    return str(value or "")[:limit]


def _legacy_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    # TCode Web 与服务端同版本发布。迁移期间保存原始 UI 提示字段，核心语义
    # 仍使用事件顶层的显式标识和状态，不再靠“最后一条消息”建立关联。
    return {"tcode": {"legacy": dict(payload)}}


class TCodeWebSocketAdapter:
    """每个聊天 socket 一个实例，负责旧运行通知到 v2 的边缘转换。"""

    def __init__(self, emitter: RuntimeEventEmitter) -> None:
        self.emitter = emitter
        self._run_started = False
        self._run_terminal = False
        self._messages: set[str] = set()
        self._finished_messages: set[str] = set()
        self._tools: set[str] = set()
        self._finished_tools: set[str] = set()
        self._tool_message_ids: dict[str, str] = {}
        self._activities: set[str] = set()
        self._pending_terminal_payload: dict[str, Any] | None = None
        self._send_lock = asyncio.Lock()

    async def replay_after(self, after_sequence: int, *, limit: int = 2_048) -> ReplayWindow:
        return await self.emitter.replay_after(after_sequence, limit=limit)

    async def finish_run(self, payload: dict[str, Any] | None = None) -> None:
        """在业务状态成功持久化后提交唯一 run.finished。"""

        async with self._send_lock:
            await self._finish_run(payload or self._pending_terminal_payload or {})

    async def send(self, payload: Any) -> bool:
        async with self._send_lock:
            return await self._send_unlocked(payload)

    async def _send_unlocked(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            raise TypeError("WebSocket event payload must be an object")
        await self._ensure_run_started(payload)
        event_type = str(payload.get("type") or "").strip()
        metadata = _legacy_metadata(payload)

        if event_type == "session":
            await self.emitter.emit(
                "state.snapshot",
                state={"session": dict(payload)},
                metadata=metadata,
            )
        elif event_type == "message":
            await self._send_message(payload, metadata)
        elif event_type == "tool_result":
            await self._send_tool_result(payload, metadata)
        elif event_type == "permission":
            await self.emitter.emit(
                "interaction.requested",
                interaction_id=_text(payload.get("request_id"), limit=200),
                kind="permission",
                prompt=f"允许工具 {_text(payload.get('tool_name'), limit=300)} 执行吗？",
                payload={
                    "tool_name": _text(payload.get("tool_name"), limit=300),
                    "arguments": dict(payload.get("arguments") or {}),
                },
                causation_id=_text(payload.get("call_id"), limit=200),
                metadata=metadata,
            )
        elif event_type == "question":
            question = dict(payload.get("question") or {})
            await self.emitter.emit(
                "interaction.requested",
                interaction_id=_text(payload.get("request_id"), limit=200),
                kind="question",
                prompt=_text(question.get("question") or question.get("display")),
                payload=question,
                metadata=metadata,
            )
        elif event_type == "question_timeout":
            await self.emitter.emit(
                "interaction.expired",
                interaction_id=_text(payload.get("request_id"), limit=200),
                kind="question",
                resolution={"answer": _text(payload.get("answer"))},
                metadata=metadata,
            )
        elif event_type == "status":
            await self._send_status(payload, metadata)
        elif event_type == "stream_heartbeat":
            await self._send_activity(payload, metadata, force_delta=True)
        elif event_type == "agent_event":
            await self._send_agent_event(payload, metadata)
        elif event_type == "team_delegate":
            subagent_run_id = _text(payload.get("session_id"), limit=200) or self._stable_id(
                "subagent", payload
            )
            await self.emitter.emit(
                "subagent.started",
                subagent_run_id=subagent_run_id,
                agent_id=_text(payload.get("agent_name"), limit=200) or "subagent",
                parent_run_id=self.emitter.run_id,
                data=dict(payload),
                metadata=metadata,
            )
        elif event_type in {"error", "capacity_rejected", "run_conflict", "async_resume_rejected"}:
            await self._send_run_error(payload, metadata)
        else:
            await self.emitter.emit(
                "state.snapshot",
                state={"notification": dict(payload)},
                metadata=metadata,
            )
        return True

    async def _ensure_run_started(self, payload: dict[str, Any]) -> None:
        if self._run_started:
            return
        self._run_started = True
        await self.emitter.emit(
            "run.started",
            agent_id=_text(payload.get("agent_name"), limit=200),
            metadata={"tcode": {"request_id": _text(payload.get("request_id"), limit=200)}},
        )

    async def _send_message(
        self, payload: dict[str, Any], metadata: dict[str, Any]
    ) -> None:
        message_id = _text(payload.get("message_id"), limit=200) or self._stable_id(
            "message", payload
        )
        role = _text(payload.get("role"), limit=20) or "assistant"
        if role not in {"user", "assistant", "system", "tool"}:
            role = "assistant"
        if message_id not in self._messages:
            self._messages.add(message_id)
            await self.emitter.emit(
                "message.started",
                message_id=message_id,
                role=role,
                causation_id=_text(payload.get("parent_id"), limit=200),
            )
        if bool(payload.get("done")):
            if message_id in self._finished_messages:
                return
            self._finished_messages.add(message_id)
            await self.emitter.emit(
                "message.finished",
                message={
                    "message_id": message_id,
                    "role": role,
                    "content": _text(payload.get("content"), limit=1_000_000),
                    "content_blocks": self._content_blocks(payload.get("content_blocks")),
                    "tool_calls": self._tool_calls(payload.get("tool_calls")),
                    "parent_message_id": _text(payload.get("parent_id"), limit=200),
                    "presentation": _text(payload.get("presentation"), limit=100),
                    "metadata": dict(payload.get("metadata") or {}),
                },
                metadata=metadata,
            )
            self._pending_terminal_payload = dict(payload)
            return

        delta = payload.get("delta")
        if not isinstance(delta, str):
            delta = _text(payload.get("content"), limit=256_000)
        await self.emitter.emit(
            "message.delta",
            message_id=message_id,
            delta=delta,
            stream_epoch=max(0, int(payload.get("stream_epoch", 0) or 0)),
            metadata=metadata,
        )

    async def _send_tool_result(
        self, payload: dict[str, Any], metadata: dict[str, Any]
    ) -> None:
        tool_call = dict(payload.get("tool_call") or {})
        tool_name = _text(tool_call.get("tool_name"), limit=300) or "unknown"
        call_id = _text(tool_call.get("call_id"), limit=200) or self._stable_id(
            "tool", tool_call
        )
        await self._ensure_tool_started(
            call_id,
            tool_name,
            {**tool_call, "message_id": payload.get("message_id")},
            {},
        )
        if call_id in self._finished_tools:
            return
        self._finished_tools.add(call_id)
        state = _text(tool_call.get("state"), limit=40)
        if state not in {"completed", "error", "skipped", "interrupted"}:
            state = "error" if tool_call.get("error") else "completed"
        await self.emitter.emit(
            "tool.result",
            tool_call_id=call_id,
            tool_name=tool_name,
            message_id=(
                _text(payload.get("message_id"), limit=200)
                or self._tool_message_ids.get(call_id, "")
            ),
            result=tool_call.get("output"),
            error=_text(tool_call.get("error")),
            state=state,
            metadata=metadata,
        )

    async def _send_status(
        self, payload: dict[str, Any], metadata: dict[str, Any]
    ) -> None:
        status = _text(payload.get("status"), limit=80)
        if status == "using_tool":
            tool_name = _text(payload.get("tool_name"), limit=300) or "unknown"
            call_id = _text(payload.get("call_id"), limit=200) or self._stable_id(
                "tool", payload
            )
            await self._ensure_tool_started(call_id, tool_name, payload, metadata)
        elif status == "cancelled":
            if self._run_terminal:
                return
            self._run_terminal = True
            await self.emitter.emit(
                "run.cancelled",
                reason=_text(payload.get("content"), limit=2_000) or "cancelled",
                metadata=metadata,
            )
        elif status == "thinking":
            await self._send_activity(payload, metadata)
        else:
            await self.emitter.emit(
                "state.snapshot",
                state={"status": dict(payload)},
                metadata=metadata,
            )

    async def _ensure_tool_started(
        self,
        call_id: str,
        tool_name: str,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        if call_id in self._tools:
            return
        self._tools.add(call_id)
        message_id = _text(payload.get("message_id"), limit=200)
        if message_id:
            self._tool_message_ids[call_id] = message_id
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = payload.get("tool_args")
        if not isinstance(arguments, dict):
            arguments = {}
        await self.emitter.emit(
            "tool.started",
            tool_call_id=call_id,
            tool_name=tool_name,
            message_id=message_id,
            arguments=arguments,
            metadata=metadata,
        )
        digest = hashlib.sha256(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        await self.emitter.emit(
            "tool.args_finished",
            tool_call_id=call_id,
            arguments=arguments,
            arguments_digest=f"sha256:{digest}",
        )

    async def _send_activity(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        *,
        force_delta: bool = False,
    ) -> None:
        phase = _text(payload.get("phase") or payload.get("status"), limit=160)
        call_id = _text(payload.get("call_id"), limit=200)
        activity_id = call_id or self._stable_id(
            "activity", {"phase": phase, "tool_name": payload.get("tool_name")}
        )
        existing = activity_id in self._activities
        self._activities.add(activity_id)
        status = _text(payload.get("status"), limit=80) or "running"
        is_terminal = status.lower() in {
            "completed",
            "complete",
            "done",
            "error",
            "cancelled",
            "failed",
        } or phase.lower() in {"tool_result", "final_response"}
        event_type = (
            "activity.finished"
            if is_terminal and not force_delta
            else "activity.delta"
            if existing or force_delta
            else "activity.snapshot"
        )
        kwargs = {
            "activity_id": activity_id,
            "phase": phase,
            "status": status,
            "message": _text(payload.get("message") or payload.get("content")),
            "tool_name": _text(payload.get("tool_name"), limit=300),
            "tool_call_id": call_id,
            "current": payload.get("current") if isinstance(payload.get("current"), (int, float)) else None,
            "total": payload.get("total") if isinstance(payload.get("total"), (int, float)) else None,
            "unit": _text(payload.get("unit"), limit=80),
            "stream": _text(payload.get("stream"), limit=80),
            "data": dict(payload),
            "metadata": metadata,
        }
        await self.emitter.emit(event_type, **kwargs)

    async def _send_agent_event(
        self, payload: dict[str, Any], metadata: dict[str, Any]
    ) -> None:
        kind = _text(payload.get("kind"), limit=160).lower()
        if kind == "model_output":
            projected = {
                "type": "message",
                "role": "assistant",
                "message_id": payload.get("message_id"),
                "content": payload.get("content"),
                "delta": payload.get("delta"),
                "stream_epoch": payload.get("stream_epoch"),
                # model_output.completed 表示一次模型调用结束，整个 Agent run
                # 仍可能继续执行工具；只有最终 message(done=true) 结束 run。
                "done": False,
            }
            await self._send_message(projected, metadata)
            return
        if kind in {"process_note", "llm_progress", "context_compaction", "tool_progress"}:
            await self._send_activity(payload, metadata)
            return

        subagent_id = _text(payload.get("session_id"), limit=200)
        parent_id = _text(payload.get("parent_session_id"), limit=200)
        status = _text(payload.get("status"), limit=80).lower()
        if subagent_id and parent_id and subagent_id != parent_id:
            event_type = (
                "subagent.finished"
                if status == "completed"
                else "subagent.error"
                if status == "error"
                else "subagent.started"
            )
            if event_type == "subagent.finished":
                await self.emitter.emit(
                    event_type,
                    subagent_run_id=subagent_id,
                    result=dict(payload),
                    metadata=metadata,
                )
            elif event_type == "subagent.error":
                await self.emitter.emit(
                    event_type,
                    subagent_run_id=subagent_id,
                    message=_text(payload.get("message") or payload.get("error")),
                    data=dict(payload),
                    metadata=metadata,
                )
            else:
                await self.emitter.emit(
                    event_type,
                    subagent_run_id=subagent_id,
                    agent_id=_text(payload.get("agent_name"), limit=200) or "subagent",
                    parent_run_id=self.emitter.run_id,
                    data=dict(payload),
                    metadata=metadata,
                )
            return
        await self._send_activity(payload, metadata)

    async def _send_run_error(
        self, payload: dict[str, Any], metadata: dict[str, Any]
    ) -> None:
        if self._run_terminal:
            return
        self._run_terminal = True
        message = _text(payload.get("error") or payload.get("content") or payload.get("reason"))
        await self.emitter.emit(
            "run.error",
            code=_text(payload.get("code"), limit=120) or _text(payload.get("type"), limit=120) or "run_failed",
            message=message or "Agent run failed",
            retryable=bool(payload.get("retryable")),
            metadata=metadata,
        )

    async def _finish_run(self, payload: dict[str, Any]) -> None:
        if self._run_terminal:
            return
        self._run_terminal = True
        outcome = _text(payload.get("outcome_status"), limit=40)
        if outcome not in {"completed", "partial", "blocked"}:
            outcome = "completed"
        await self.emitter.emit(
            "run.finished",
            outcome=outcome,
            result={
                "workflow_background_active": bool(payload.get("workflow_background_active")),
                "child_session_active": bool(payload.get("child_session_active")),
                **{
                    key: bool(payload[key])
                    for key in ("chat_running", "background_running")
                    if key in payload
                },
            },
            metadata=_legacy_metadata(payload),
        )

    @staticmethod
    def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"

    @staticmethod
    def _content_blocks(value: Any) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for raw in list(value or [])[:256]:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("type") or "") == "text":
                blocks.append({"type": "text", "text": _text(raw.get("text"), limit=1_000_000)})
                continue
            blocks.append(
                {
                    "type": "extension",
                    "name": _text(raw.get("type"), limit=200) or "unknown",
                    "data": dict(raw),
                    "fallback": _text(raw.get("fallback") or raw.get("text")),
                }
            )
        return blocks

    @staticmethod
    def _tool_calls(value: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for raw in list(value or [])[:256]:
            if not isinstance(raw, dict):
                continue
            tool_name = _text(raw.get("tool_name"), limit=300) or "unknown"
            call_id = _text(raw.get("call_id"), limit=200) or TCodeWebSocketAdapter._stable_id(
                "tool", raw
            )
            state = _text(raw.get("state"), limit=40)
            if state not in {"pending", "running", "completed", "error", "skipped", "interrupted"}:
                state = "completed" if raw.get("output") is not None else "pending"
            calls.append(
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "arguments": dict(raw.get("arguments") or {}),
                    "state": state,
                    "result": raw.get("output"),
                    "error": _text(raw.get("error")),
                    "metadata": dict(raw.get("metadata") or {}),
                }
            )
        return calls


__all__ = ["TCodeWebSocketAdapter"]
