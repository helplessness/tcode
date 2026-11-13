"""把现有 TCode AgentHarnessEngine 包装为默认 AgentBackend。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..protocol.v2 import RuntimeEventValue, runtime_message_from_legacy
from ..runtime.agent_backend import NormalizedRunInput, RunContext
from ..runtime.interaction_store import InteractionStore


AgentFactory = Callable[[NormalizedRunInput, RunContext], Awaitable[tuple[Any, Any]]]


class TCodeNativeBackend:
    backend_id = "tcode-native"

    def __init__(
        self,
        *,
        interaction_store: InteractionStore | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.interaction_store = interaction_store or InteractionStore()
        self._agent_factory = agent_factory or self._default_agent_factory
        self._active_agents: dict[str, Any] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def get_capabilities(self) -> dict[str, object]:
        from ..swarm.capabilities import auto_swarm_builder_availability

        return {
            "text": True,
            "images": True,
            "files": True,
            "tools": True,
            "parallelTools": True,
            "state": True,
            "interactions": True,
            "subagents": True,
            "schedulerModes": {
                "supervisor": {"available": True, "reason": ""},
                "chain": {"available": True, "reason": ""},
                "swarm": {"available": True, "reason": ""},
                "auto_swarm_builder": auto_swarm_builder_availability(),
            },
            "cancel": True,
            "resume": True,
            "guidanceMode": "in_turn",
            "permissionAuthority": "tcode",
            "release": "stable",
        }

    async def run(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        agent, provider = await self._agent_factory(run_input, context)
        latest_user = next(
            (message for message in reversed(run_input.messages) if message.role == "user"),
            None,
        )
        set_user_context = getattr(agent, "set_next_user_message_context", None)
        if latest_user is not None and callable(set_user_context):
            source_payload = latest_user.model_dump(mode="python")
            raw_metadata = source_payload.get("metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            metadata["source_runtime_id"] = context.runtime_id
            set_user_context(metadata=metadata, message_id=latest_user.id)
        self._active_agents[context.run_id] = agent
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(256)
        activity_states: set[str] = set()
        closing = False

        async def enqueue_agent_event(payload: dict[str, Any]) -> None:
            await queue.put(("agent_event", dict(payload or {})))

        async def ask_question(payload: dict[str, Any]) -> str:
            owner_key = f"{context.tenant_id}:{context.user_id}"
            prompt = str(payload.get("question") or payload.get("display") or "请补充信息")
            record = self.interaction_store.create(
                owner_key=owner_key,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                origin_run_id=context.run_id,
                kind="question",
                reason="agent_question",
                prompt=prompt,
                request=dict(payload),
                response_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string", "maxLength": 20000}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                expires_in_seconds=int(payload.get("timeout_seconds") or 3600),
            )
            await queue.put(
                (
                    "runtime_event",
                    (
                        "interaction.requested",
                        {
                            "interaction_id": record["interaction_id"],
                            "kind": "question",
                            "prompt": prompt,
                            "payload": dict(payload),
                        },
                    ),
                )
            )
            resolution = await self.interaction_store.wait_for_resolution(
                record["interaction_id"], owner_key=owner_key
            )
            await queue.put(
                (
                    "runtime_event",
                    (
                        "interaction.resolved",
                        {
                            "interaction_id": record["interaction_id"],
                            "kind": "question",
                            "resolution": resolution,
                        },
                    ),
                )
            )
            return str(resolution.get("answer") or "")

        async def ask_permission(tool_name: str, arguments: dict[str, Any]) -> str | bool:
            owner_key = f"{context.tenant_id}:{context.user_id}"
            record = self.interaction_store.create(
                owner_key=owner_key,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                origin_run_id=context.run_id,
                kind="permission",
                reason="tool_permission",
                prompt=f"允许工具 {tool_name} 执行吗？",
                request={"tool_name": tool_name, "arguments": arguments},
                response_schema={
                    "type": "object",
                    "properties": {"decision": {"enum": ["allow", "deny"]}},
                    "required": ["decision"],
                    "additionalProperties": False,
                },
            )
            await queue.put(
                (
                    "runtime_event",
                    (
                        "interaction.requested",
                        {
                            "interaction_id": record["interaction_id"],
                            "kind": "permission",
                            "prompt": record["prompt"],
                            "payload": record["request"],
                        },
                    ),
                )
            )
            resolution = await self.interaction_store.wait_for_resolution(
                record["interaction_id"], owner_key=owner_key
            )
            decision = str(resolution.get("decision") or "deny")
            await queue.put(
                (
                    "runtime_event",
                    (
                        "interaction.resolved",
                        {
                            "interaction_id": record["interaction_id"],
                            "kind": "permission",
                            "resolution": resolution,
                        },
                    ),
                )
            )
            return decision == "allow"

        if hasattr(agent, "set_agent_event_emitter"):
            agent.set_agent_event_emitter(enqueue_agent_event)
        if hasattr(agent, "set_question_handler"):
            agent.set_question_handler(ask_question)
        if hasattr(agent, "set_permission_handler"):
            agent.set_permission_handler(ask_permission)

        async def produce() -> None:
            try:
                if getattr(agent, "_native_server_session", None) == context.session_id:
                    from ..server.native_execution import run_native_turn

                    async def emit_message(message):
                        await queue.put(("message", message))

                    async def emit_state(state):
                        await queue.put(("runtime_event", ("state.snapshot", {"state": state})))

                    await run_native_turn(
                        agent=agent, run_input=run_input, context=context,
                        question_handler=ask_question, emit_message=emit_message, emit_state=emit_state,
                    )
                else:
                    # Injectable standalone agents remain useful for backend conformance tests.
                    async for message in agent.run_stream(run_input.latest_user_text()):
                        await queue.put(("message", message))
            except BaseException as exc:
                if not closing:
                    await queue.put(("error", exc))
            finally:
                if not closing:
                    await queue.put(("done", None))

        producer = asyncio.create_task(produce(), name=f"tcode-native-{context.run_id}")
        self._active_tasks[context.run_id] = producer
        seen_messages: set[str] = set()
        started_tools: set[str] = set()
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    if isinstance(payload, asyncio.CancelledError):
                        if context.cancelled.is_set():
                            break  # coordinator emits the normal run.cancelled terminal
                        raise payload
                    raise payload
                if kind == "runtime_event":
                    event_type, event_payload = payload
                    yield context.event(event_type, **event_payload)
                    continue
                if kind == "agent_event":
                    event = self._activity_event(payload, context, activity_states)
                    if event is not None:
                        yield event
                    continue
                async for event in self._message_events(
                    payload,
                    context,
                    seen_messages=seen_messages,
                    started_tools=started_tools,
                ):
                    yield event
        finally:
            closing = True
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            self._active_agents.pop(context.run_id, None)
            self._active_tasks.pop(context.run_id, None)
            # The server owns cached providers and their child inheritance.
            close = None if getattr(agent, "_native_server_session", None) else getattr(provider, "close", None)
            if close is not None:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

    async def cancel(self, run_id: str, context: RunContext) -> None:
        context.cancelled.set()
        self.interaction_store.cancel_pending_for_run(
            owner_key=f"{context.tenant_id}:{context.user_id}",
            origin_run_id=run_id,
        )
        agent = self._active_agents.get(run_id)
        if agent is not None and hasattr(agent, "stop"):
            agent.stop()
        task = self._active_tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    async def _default_agent_factory(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> tuple[Any, Any]:
        from ..server.native_execution import open_native_agent

        return await open_native_agent(run_input, context)

    async def _message_events(
        self,
        message: Any,
        context: RunContext,
        *,
        seen_messages: set[str],
        started_tools: set[str],
    ) -> AsyncIterator[RuntimeEventValue]:
        role = str(getattr(message, "role", "") or "")
        message_id = str(getattr(message, "id", "") or "") or f"msg_{uuid.uuid4().hex}"
        parent_id = str(getattr(message, "parent_id", "") or "")
        tool_calls = list(getattr(message, "tool_calls", []) or [])
        if role == "assistant" and message_id not in seen_messages:
            seen_messages.add(message_id)
            content = str(getattr(message, "content", "") or "")
            yield context.event(
                "message.started",
                message_id=message_id,
                role="assistant",
                parent_message_id=parent_id,
            )
            if content:
                yield context.event("message.delta", message_id=message_id, delta=content)
            runtime_tools: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                call_id = str(getattr(tool_call, "call_id", "") or "") or f"tool_{uuid.uuid4().hex}"
                tool_name = str(getattr(tool_call, "tool_name", "") or "unknown")
                arguments = dict(getattr(tool_call, "arguments", {}) or {})
                started_tools.add(call_id)
                yield context.event(
                    "tool.started",
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    message_id=message_id,
                    arguments=arguments,
                )
                yield context.event(
                    "tool.args_finished",
                    tool_call_id=call_id,
                    arguments=arguments,
                    arguments_digest="sha256:" + __import__("hashlib").sha256(
                        json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                )
                state = getattr(getattr(tool_call, "state", None), "value", "pending")
                runtime_tools.append(
                    {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "state": state,
                        "result": getattr(tool_call, "output", None),
                        "error": str(getattr(tool_call, "error", "") or ""),
                        "metadata": dict(getattr(tool_call, "metadata", {}) or {}),
                    }
                )
            yield context.event(
                "message.finished",
                message={
                    **runtime_message_from_legacy(message).model_dump(mode="python"),
                    "message_id": message_id,
                    "tool_calls": runtime_tools,
                },
            )
            return
        if role == "tool":
            for tool_call in tool_calls:
                call_id = str(getattr(tool_call, "call_id", "") or "")
                if not call_id:
                    continue
                if call_id not in started_tools:
                    tool_name = str(getattr(tool_call, "tool_name", "") or "unknown")
                    arguments = dict(getattr(tool_call, "arguments", {}) or {})
                    started_tools.add(call_id)
                    yield context.event(
                        "tool.started",
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        message_id=parent_id,
                        arguments=arguments,
                    )
                    yield context.event(
                        "tool.args_finished",
                        tool_call_id=call_id,
                        arguments=arguments,
                        arguments_digest="",
                    )
                state_value = str(getattr(getattr(tool_call, "state", None), "value", "completed"))
                state = state_value if state_value in {"completed", "error", "skipped", "interrupted"} else "completed"
                error = str(getattr(tool_call, "error", "") or "")
                yield context.event(
                    "tool.result",
                    tool_call_id=call_id,
                    tool_name=str(getattr(tool_call, "tool_name", "") or ""),
                    message_id=message_id,
                    result=getattr(tool_call, "output", None) or getattr(message, "content", ""),
                    error=error,
                    state="error" if error else state,
                )

    @staticmethod
    def _activity_event(
        payload: dict[str, Any],
        context: RunContext,
        states: set[str],
    ) -> RuntimeEventValue | None:
        if str(payload.get("kind") or "") != "process_note":
            return None
        phase = str(payload.get("phase") or "activity")[:160]
        call_id = str(payload.get("call_id") or "")
        activity_id = call_id or str(payload.get("activity_id") or "") or f"activity_{phase}"
        status = str(payload.get("status") or "running")[:80]
        event_type = "activity.snapshot" if activity_id not in states else "activity.delta"
        if status in {"completed", "error", "failed", "cancelled"}:
            event_type = "activity.finished"
        states.add(activity_id)
        return context.event(
            event_type,
            activity_id=activity_id,
            phase=phase,
            status=status,
            message=str(payload.get("message") or "")[:20_000],
            tool_name=str(payload.get("tool_name") or "")[:300],
            tool_call_id=call_id,
            current=payload.get("current"),
            total=payload.get("total"),
            unit=str(payload.get("unit") or "")[:80],
            stream=str(payload.get("stream") or "")[:80],
            data={},
        )


__all__ = ["TCodeNativeBackend"]
