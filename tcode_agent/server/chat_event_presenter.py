"""聊天流中的工具事件与过程说明展示逻辑。

这个模块只负责把运行时对象整理成前端协议，不负责执行工具或维护会话。
"""

from __future__ import annotations

import json
from typing import Any

from ..protocol import tool_result_event, tool_using_event
from .session_presenter import _serialize_frontend_tool_call


def frontend_tool_call_key(tool_call: Any) -> tuple[str, str]:
    call_id = str(getattr(tool_call, "call_id", "") or "").strip()
    if call_id:
        return ("call_id", call_id)
    return (
        str(getattr(tool_call, "tool_name", "") or ""),
        json.dumps(getattr(tool_call, "arguments", {}) or {}, sort_keys=True),
    )


def merge_frontend_tool_call(
    tool_calls_data: list[dict[str, Any]], tool_call: Any
) -> tuple[dict[str, Any], bool]:
    key = frontend_tool_call_key(tool_call)
    payload = _serialize_frontend_tool_call(tool_call)
    for index, existing in enumerate(tool_calls_data):
        existing_call_id = str(existing.get("call_id", "") or "").strip()
        existing_key = (
            ("call_id", existing_call_id)
            if existing_call_id
            else (
                str(existing.get("tool_name", "") or ""),
                json.dumps(existing.get("arguments", {}) or {}, sort_keys=True),
            )
        )
        if existing_key != key:
            continue
        tool_calls_data[index] = {
            **existing,
            **payload,
            "arguments": payload.get("arguments") or existing.get("arguments") or {},
        }
        return tool_calls_data[index], False
    tool_calls_data.append(payload)
    return payload, True


async def send_frontend_tool_events(
    websocket: Any,
    *,
    session_id: str,
    item: Any,
    tool_calls_data: list[dict[str, Any]],
    seen_tool_calls: set[tuple[str, str]],
    emit_using_status: bool = True,
) -> None:
    if not getattr(item, "tool_calls", None):
        return

    for tool_call in item.tool_calls:
        tool_call_info, is_new = merge_frontend_tool_call(tool_calls_data, tool_call)
        key = frontend_tool_call_key(tool_call)
        if emit_using_status and is_new and key not in seen_tool_calls:
            seen_tool_calls.add(key)
            await websocket.send_json(
                tool_using_event(
                    session_id=session_id,
                    tool_name=tool_call.tool_name,
                    tool_args=tool_call.arguments,
                    call_id=str(getattr(tool_call, "call_id", "") or ""),
                    message_id=str(getattr(item, "id", "") or ""),
                )
            )
        elif is_new:
            seen_tool_calls.add(key)

        # 某些工具结果是前端第一次看到该调用，结果事件不能依赖开始事件。
        if item.role == "tool":
            await websocket.send_json(
                tool_result_event(
                    session_id=session_id,
                    tool_call=tool_call_info,
                    message_id=str(getattr(item, "parent_id", "") or ""),
                )
            )
