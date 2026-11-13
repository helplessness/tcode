"""Harness 事件和能力合同断言。"""

from __future__ import annotations

from typing import Any

from ...protocol.v2 import RuntimeEventStateMachine


def validate_event_stream(events: list[Any]) -> tuple[list[str], list[str]]:
    machine = RuntimeEventStateMachine(require_contiguous=True)
    types: list[str] = []
    for event in events:
        machine.accept(event)
        types.append(str(event.type))
    failures: list[str] = []
    if "message.started" not in types:
        failures.append("missing message.started")
    if "message.delta" not in types:
        failures.append("missing message.delta")
    if types.count("message.finished") != 1:
        failures.append("expected exactly one message.finished")
    return types, failures


def observed_capabilities(events: list[Any]) -> set[str]:
    types = {str(event.type) for event in events}
    result = {"text"} if "message.finished" in types else set()
    if "tool.started" in types and (
        "tool.result" in types or "tool.args_finished" in types
    ):
        result.add("tools")
    if "interaction.requested" in types:
        result.add("interactions")
    return result


__all__ = ["observed_capabilities", "validate_event_stream"]
