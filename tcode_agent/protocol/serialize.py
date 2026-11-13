"""核心 WebSocket 事件的校验与序列化入口。"""

from __future__ import annotations

from typing import Any

from .models import (
    PermissionRequestEvent,
    QuestionRequestEvent,
    QuestionTimeoutEvent,
    ToolResultEvent,
    ToolUsingEvent,
    UsageStatusEvent,
)


def _dump(model) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def tool_using_event(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    call_id: str = "",
    message_id: str = "",
    session_id: str = "",
    content: str = "",
) -> dict[str, Any]:
    return _dump(
        ToolUsingEvent(
            type="status",
            status="using_tool",
            tool_name=tool_name,
            tool_args=tool_args,
            call_id=call_id,
            message_id=message_id,
            session_id=session_id,
            content=content or f"Using {tool_name}...",
        )
    )


def tool_result_event(
    *,
    session_id: str,
    tool_call: dict[str, Any],
    message_id: str = "",
) -> dict[str, Any]:
    normalized_tool_call = dict(tool_call or {})
    if normalized_tool_call.get("state") not in {
        "pending",
        "running",
        "completed",
        "error",
    }:
        normalized_tool_call.pop("state", None)
    return _dump(
        ToolResultEvent(
            type="tool_result",
            session_id=session_id,
            message_id=message_id,
            tool_call=normalized_tool_call,
        )
    )


def permission_request_event(
    *,
    request_id: str,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return _dump(
        PermissionRequestEvent(
            type="permission",
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
    )


def question_request_event(
    *,
    request_id: str,
    session_id: str,
    question: dict[str, Any],
) -> dict[str, Any]:
    return _dump(
        QuestionRequestEvent(
            type="question",
            request_id=request_id,
            session_id=session_id,
            question=question,
        )
    )


def question_timeout_event(
    *, request_id: str, session_id: str, answer: str = ""
) -> dict[str, Any]:
    return _dump(
        QuestionTimeoutEvent(
            type="question_timeout",
            request_id=request_id,
            session_id=session_id,
            answer=answer,
        )
    )


def usage_status_event(usage: dict[str, Any]) -> dict[str, Any]:
    return _dump(UsageStatusEvent(type="status", status="usage", usage=usage))
