"""v2 事件顺序、生命周期和幂等约束。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import RuntimeEventValue


RUN_TERMINALS = {"run.finished", "run.error", "run.cancelled", "run.interrupted"}


class RuntimeEventInvariantError(ValueError):
    """事件无法安全投影时抛出，调用方应触发 snapshot 重同步。"""


@dataclass
class RuntimeEventStateMachine:
    """按 session 验证序列以及 message/tool/run 的合法转换。"""

    session_id: str = ""
    last_sequence: int = 0
    event_ids: set[str] = field(default_factory=set)
    sequence_ids: dict[int, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    activities: dict[str, str] = field(default_factory=dict)
    interactions: dict[str, str] = field(default_factory=dict)
    require_contiguous: bool = True

    def accept(self, event: RuntimeEventValue) -> bool:
        """校验并记录事件；完全相同的 event_id 重放返回 ``False``。"""

        if self.session_id and event.session_id != self.session_id:
            raise RuntimeEventInvariantError("event session_id does not match projection")
        if not self.session_id:
            self.session_id = event.session_id

        if event.event_id in self.event_ids:
            return False
        previous_at_sequence = self.sequence_ids.get(event.sequence)
        if previous_at_sequence and previous_at_sequence != event.event_id:
            raise RuntimeEventInvariantError("sequence is already occupied by another event")
        if (
            self.require_contiguous
            and self.last_sequence
            and event.sequence != self.last_sequence + 1
        ):
            raise RuntimeEventInvariantError(
                f"event sequence gap: expected {self.last_sequence + 1}, got {event.sequence}"
            )
        if event.sequence <= self.last_sequence:
            raise RuntimeEventInvariantError("event sequence moved backwards")

        self._accept_lifecycle(event)
        self.event_ids.add(event.event_id)
        self.sequence_ids[event.sequence] = event.event_id
        self.last_sequence = event.sequence
        return True

    def _accept_lifecycle(self, event: RuntimeEventValue) -> None:
        event_type = event.type
        run_status = self.runs.get(event.run_id)
        if event_type == "run.started":
            if run_status and run_status != "started":
                raise RuntimeEventInvariantError("terminal run cannot restart")
            self.runs[event.run_id] = "started"
            return
        if event_type in RUN_TERMINALS:
            if run_status is None:
                raise RuntimeEventInvariantError("run terminal requires run.started")
            if run_status in RUN_TERMINALS:
                raise RuntimeEventInvariantError("run already has a terminal event")
            self.runs[event.run_id] = event_type
            return

        if run_status in RUN_TERMINALS:
            raise RuntimeEventInvariantError("event cannot follow a terminal run")

        if event_type == "message.started":
            if self.messages.get(event.message_id) == "finished":
                raise RuntimeEventInvariantError("finished message cannot restart")
            self.messages[event.message_id] = "started"
        elif event_type == "message.delta":
            if self.messages.get(event.message_id) != "started":
                raise RuntimeEventInvariantError("message.delta requires message.started")
        elif event_type == "message.finished":
            message_id = event.message.message_id
            if self.messages.get(message_id) != "started":
                raise RuntimeEventInvariantError("message.finished requires message.started")
            self.messages[message_id] = "finished"
        elif event_type == "tool.started":
            if self.tools.get(event.tool_call_id) in {"completed", "error", "skipped", "interrupted"}:
                raise RuntimeEventInvariantError("finished tool cannot restart")
            self.tools[event.tool_call_id] = "started"
        elif event_type in {"tool.args", "tool.args_finished"}:
            if self.tools.get(event.tool_call_id) != "started":
                raise RuntimeEventInvariantError(f"{event_type} requires tool.started")
        elif event_type == "tool.result":
            if self.tools.get(event.tool_call_id) != "started":
                raise RuntimeEventInvariantError("tool.result requires tool.started")
            self.tools[event.tool_call_id] = event.state
        elif event_type in {"activity.snapshot", "activity.delta"}:
            self.activities[event.activity_id] = "running"
        elif event_type == "activity.finished":
            self.activities[event.activity_id] = "finished"
        elif event_type == "interaction.requested":
            self.interactions[event.interaction_id] = "requested"
        elif event_type.startswith("interaction."):
            if self.interactions.get(event.interaction_id) != "requested":
                raise RuntimeEventInvariantError(
                    f"{event_type} requires interaction.requested"
                )
            self.interactions[event.interaction_id] = event_type.removeprefix("interaction.")


__all__ = ["RUN_TERMINALS", "RuntimeEventInvariantError", "RuntimeEventStateMachine"]
