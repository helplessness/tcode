"""规范化运行事件到 Session Canonical Event Log 的持久化规则。"""

from __future__ import annotations

from typing import Any

from .events import RuntimeEventValue


PERSISTED_EVENT_TYPES = frozenset(
    {
        "run.started",
        "run.finished",
        "run.error",
        "run.cancelled",
        "run.interrupted",
        "message.finished",
        "tool.args_finished",
        "tool.result",
        "activity.finished",
        "interaction.requested",
        "interaction.resolved",
        "interaction.cancelled",
        "interaction.expired",
        "subagent.started",
        "subagent.finished",
        "subagent.error",
        "state.snapshot",
        "messages.snapshot",
    }
)


def should_persist_runtime_event(event: RuntimeEventValue) -> bool:
    """高频 delta 不落盘，只在边界事件保存合并后的事实。"""

    return event.type in PERSISTED_EVENT_TYPES


def runtime_event_session_spec(event: RuntimeEventValue) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    return {
        "event_type": f"runtime.v2.{event.type}",
        "payload": {"event": payload},
        "causation_id": event.causation_id,
        "idempotency_key": f"runtime-v2:{event.event_id}",
        "metadata": {
            "protocol": event.protocol,
            "protocol_version": event.protocol_version,
            "run_id": event.run_id,
            "runtime_sequence": event.sequence,
        },
    }


def apply_runtime_event_projection(
    projection: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """把已持久化 v2 边界事件投影成可重建的运行摘要。"""

    current = dict(projection or {})
    event_type = str(event.get("type") or "")
    current["last_sequence"] = int(event.get("sequence", 0) or 0)
    current["last_event_id"] = str(event.get("event_id") or "")
    current["run_id"] = str(event.get("run_id") or "")
    current["status"] = event_type

    if event_type == "message.finished":
        message = event.get("message")
        if isinstance(message, dict):
            messages = dict(current.get("messages") or {})
            messages[str(message.get("message_id") or "")] = dict(message)
            current["messages"] = messages
    elif event_type == "messages.snapshot":
        current["messages"] = {
            str(item.get("message_id") or ""): dict(item)
            for item in list(event.get("messages") or [])
            if isinstance(item, dict) and str(item.get("message_id") or "")
        }
    elif event_type == "tool.result":
        tools = dict(current.get("tools") or {})
        tools[str(event.get("tool_call_id") or "")] = {
            "state": event.get("state"),
            "result": event.get("result"),
            "error": event.get("error"),
        }
        current["tools"] = tools
    elif event_type == "interaction.requested":
        interactions = dict(current.get("interactions") or {})
        interactions[str(event.get("interaction_id") or "")] = dict(event)
        current["interactions"] = interactions
    elif event_type.startswith("interaction."):
        interactions = dict(current.get("interactions") or {})
        interactions.pop(str(event.get("interaction_id") or ""), None)
        current["interactions"] = interactions
    elif event_type == "state.snapshot" and isinstance(event.get("state"), dict):
        current["state"] = dict(event["state"])
    return current


__all__ = [
    "PERSISTED_EVENT_TYPES",
    "apply_runtime_event_projection",
    "runtime_event_session_spec",
    "should_persist_runtime_event",
]
