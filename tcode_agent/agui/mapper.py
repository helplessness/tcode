"""TCode v2 到 @ag-ui/core 0.0.59 的集中映射。"""

from __future__ import annotations

import json
from typing import Any

from ..protocol.v2 import RuntimeEventValue


def _metadata(event: RuntimeEventValue) -> dict[str, Any]:
    existing = dict(event.metadata or {})
    tcode = dict(existing.get("tcode") or {})
    tcode.update(
        {
            "eventId": event.event_id,
            "sequence": event.sequence,
            "protocolVersion": event.protocol_version,
        }
    )
    existing["tcode"] = tcode
    return existing


def _base(event: RuntimeEventValue, event_type: str) -> dict[str, Any]:
    return {
        "type": event_type,
        "timestamp": int(event.timestamp.timestamp() * 1000),
        "metadata": _metadata(event),
    }


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _message(message: Any) -> dict[str, Any]:
    role = str(getattr(message, "role", "assistant") or "assistant")
    if role not in {"developer", "system", "assistant", "user", "tool", "reasoning", "activity"}:
        role = "assistant"
    result: dict[str, Any] = {
        "id": str(getattr(message, "message_id", "") or ""),
        "role": role,
        "content": str(getattr(message, "content", "") or ""),
    }
    tool_calls = list(getattr(message, "tool_calls", []) or [])
    if tool_calls:
        result["toolCalls"] = [
            {
                "id": item.tool_call_id,
                "type": "function",
                "function": {
                    "name": item.tool_name,
                    "arguments": json.dumps(item.arguments, ensure_ascii=False, separators=(",", ":")),
                },
            }
            for item in tool_calls
        ]
    content_blocks = list(getattr(message, "content_blocks", []) or [])
    if content_blocks:
        result["metadata"] = {
            "tcode": {
                "contentBlocks": [
                    item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if hasattr(item, "model_dump")
                    else dict(item)
                    for item in content_blocks
                    if hasattr(item, "model_dump") or isinstance(item, dict)
                ]
            }
        }
    return result


def runtime_event_to_agui(event: RuntimeEventValue) -> dict[str, Any] | None:
    event_type = event.type
    if event_type == "run.started":
        value = _base(event, "RUN_STARTED")
        value.update({"threadId": event.session_id, "runId": event.run_id})
        if event.parent_run_id:
            value["parentRunId"] = event.parent_run_id
        return value
    if event_type == "run.finished":
        value = _base(event, "RUN_FINISHED")
        value.update(
            {
                "threadId": event.session_id,
                "runId": event.run_id,
                "result": event.result,
                "outcome": {"type": "success"},
            }
        )
        return value
    if event_type == "run.interrupted":
        value = _base(event, "RUN_FINISHED")
        interrupt_id = event.interaction_id or f"interrupt_{event.run_id}"
        value.update(
            {
                "threadId": event.session_id,
                "runId": event.run_id,
                "outcome": {
                    "type": "interrupt",
                    "interrupts": [
                        {
                            "id": interrupt_id,
                            "reason": event.reason,
                            "metadata": {"tcode": {"interactionId": event.interaction_id}},
                        }
                    ],
                },
            }
        )
        return value
    if event_type == "run.cancelled":
        value = _base(event, "RUN_FINISHED")
        value.update(
            {
                "threadId": event.session_id,
                "runId": event.run_id,
                "result": {"status": "cancelled", "reason": event.reason},
            }
        )
        return value
    if event_type == "run.error":
        value = _base(event, "RUN_ERROR")
        value.update({"message": event.message, "code": event.code})
        return value
    if event_type == "message.started":
        role = event.role if event.role != "tool" else "assistant"
        value = _base(event, "TEXT_MESSAGE_START")
        value.update({"messageId": event.message_id, "role": role})
        return value
    if event_type == "message.delta":
        value = _base(event, "TEXT_MESSAGE_CONTENT")
        value.update({"messageId": event.message_id, "delta": event.delta})
        return value
    if event_type == "message.finished":
        value = _base(event, "TEXT_MESSAGE_END")
        value.update({"messageId": event.message.message_id})
        content_blocks = list(event.message.content_blocks or [])
        if content_blocks:
            value["metadata"]["tcode"]["contentBlocks"] = [
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
                for item in content_blocks
            ]
        return value
    if event_type == "tool.started":
        value = _base(event, "TOOL_CALL_START")
        value.update(
            {
                "toolCallId": event.tool_call_id,
                "toolCallName": event.tool_name,
                "parentMessageId": event.message_id or None,
            }
        )
        return value
    if event_type == "tool.args":
        value = _base(event, "TOOL_CALL_ARGS")
        value.update({"toolCallId": event.tool_call_id, "delta": event.delta})
        return value
    if event_type == "tool.args_finished":
        value = _base(event, "TOOL_CALL_END")
        value.update({"toolCallId": event.tool_call_id})
        return value
    if event_type == "tool.result":
        value = _base(event, "TOOL_CALL_RESULT")
        value.update(
            {
                "messageId": event.message_id or f"tool_result_{event.tool_call_id}",
                "toolCallId": event.tool_call_id,
                "content": _json_text(event.result if not event.error else {"error": event.error}),
                "role": "tool",
            }
        )
        return value
    if event_type in {"activity.snapshot", "activity.delta", "activity.finished"}:
        content = {
            "phase": event.phase,
            "status": event.status,
            "message": event.message,
            "toolName": event.tool_name,
            "toolCallId": event.tool_call_id,
            "current": event.current,
            "total": event.total,
            "unit": event.unit,
            "stream": event.stream,
            **event.data,
        }
        if event_type == "activity.delta":
            value = _base(event, "ACTIVITY_DELTA")
            value.update(
                {
                    "messageId": event.activity_id,
                    "activityType": event.phase or "TCODE_ACTIVITY",
                    "patch": [{"op": "replace", "path": "", "value": content}],
                }
            )
        else:
            value = _base(event, "ACTIVITY_SNAPSHOT")
            value.update(
                {
                    "messageId": event.activity_id,
                    "activityType": event.phase or "TCODE_ACTIVITY",
                    "content": content,
                    "replace": True,
                }
            )
        return value
    if event_type == "state.snapshot":
        value = _base(event, "STATE_SNAPSHOT")
        value["snapshot"] = event.state
        return value
    if event_type == "state.delta":
        value = _base(event, "STATE_DELTA")
        value["delta"] = [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in event.patch]
        return value
    if event_type == "messages.snapshot":
        value = _base(event, "MESSAGES_SNAPSHOT")
        value["messages"] = [_message(item) for item in event.messages]
        return value
    if event_type == "subagent.started":
        value = _base(event, "SUBAGENT_STARTED")
        value.update(
            {
                "subagentRunId": event.subagent_run_id,
                "name": event.agent_id,
                "description": str(event.data.get("description") or ""),
            }
        )
        optional = {
            "parentSubagentRunId": event.parent_run_id,
            "parentToolCallId": event.data.get("parentToolCallId"),
            "parentMessageId": event.data.get("parentMessageId"),
        }
        value.update({key: item for key, item in optional.items() if item})
        return value
    if event_type == "subagent.finished":
        value = _base(event, "SUBAGENT_FINISHED")
        value.update({"subagentRunId": event.subagent_run_id, "outcome": {"type": "success"}, "result": event.result})
        return value
    if event_type == "subagent.error":
        value = _base(event, "SUBAGENT_ERROR")
        value.update({"subagentRunId": event.subagent_run_id, "message": event.message})
        return value
    if event_type.startswith("interaction."):
        value = _base(event, "CUSTOM")
        value.update(
            {
                "name": f"tcode.{event_type}",
                "value": event.model_dump(mode="json", by_alias=True),
            }
        )
        return value
    return None


__all__ = ["runtime_event_to_agui"]
