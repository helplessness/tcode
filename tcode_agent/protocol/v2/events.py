"""TCode v2 规范化运行事件。"""

from __future__ import annotations

from typing import Any, Annotated, Literal, Union

from pydantic import Field, RootModel, TypeAdapter

from .envelope import EventEnvelope, Identifier, OptionalIdentifier, ProtocolV2Model
from .messages import RuntimeMessage


class RunStartedEvent(EventEnvelope):
    type: Literal["run.started"]
    agent_id: OptionalIdentifier = ""
    parent_run_id: OptionalIdentifier = ""


class RunFinishedEvent(EventEnvelope):
    type: Literal["run.finished"]
    outcome: Literal["completed", "partial", "blocked"] = "completed"
    result: dict[str, Any] = Field(default_factory=dict, max_length=64)


class RunErrorEvent(EventEnvelope):
    type: Literal["run.error"]
    code: str = Field(default="run_failed", min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=20_000)
    retryable: bool = False


class RunCancelledEvent(EventEnvelope):
    type: Literal["run.cancelled"]
    reason: str = Field(default="cancelled", max_length=2_000)


class RunInterruptedEvent(EventEnvelope):
    type: Literal["run.interrupted"]
    reason: str = Field(default="interaction_required", max_length=2_000)
    interaction_id: OptionalIdentifier = ""


class MessageStartedEvent(EventEnvelope):
    type: Literal["message.started"]
    message_id: Identifier
    role: Literal["user", "assistant", "system", "tool"]
    parent_message_id: OptionalIdentifier = ""


class MessageDeltaEvent(EventEnvelope):
    type: Literal["message.delta"]
    message_id: Identifier
    delta: str = Field(max_length=256_000)
    stream_epoch: int = Field(default=0, ge=0)


class MessageFinishedEvent(EventEnvelope):
    type: Literal["message.finished"]
    message: RuntimeMessage


class ToolStartedEvent(EventEnvelope):
    type: Literal["tool.started"]
    tool_call_id: Identifier
    tool_name: str = Field(min_length=1, max_length=300)
    message_id: OptionalIdentifier = ""
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=512)


class ToolArgsEvent(EventEnvelope):
    type: Literal["tool.args"]
    tool_call_id: Identifier
    delta: str = Field(max_length=256_000)


class ToolArgsFinishedEvent(EventEnvelope):
    type: Literal["tool.args_finished"]
    tool_call_id: Identifier
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=512)
    arguments_digest: str = Field(default="", max_length=128)


class ToolResultEvent(EventEnvelope):
    type: Literal["tool.result"]
    tool_call_id: Identifier
    tool_name: str = Field(default="", max_length=300)
    message_id: OptionalIdentifier = ""
    result: Any = None
    error: str = Field(default="", max_length=20_000)
    state: Literal["completed", "error", "skipped", "interrupted"] = "completed"


class ActivityFields(EventEnvelope):
    activity_id: Identifier
    phase: str = Field(default="", max_length=160)
    status: str = Field(default="running", max_length=80)
    message: str = Field(default="", max_length=20_000)
    tool_name: str = Field(default="", max_length=300)
    tool_call_id: OptionalIdentifier = ""
    current: float | None = None
    total: float | None = None
    unit: str = Field(default="", max_length=80)
    stream: str = Field(default="", max_length=80)
    data: dict[str, Any] = Field(default_factory=dict, max_length=128)


class ActivitySnapshotEvent(ActivityFields):
    type: Literal["activity.snapshot"]


class ActivityDeltaEvent(ActivityFields):
    type: Literal["activity.delta"]


class ActivityFinishedEvent(ActivityFields):
    type: Literal["activity.finished"]


InteractionKind = Literal["question", "permission", "review"]


class InteractionRequestedEvent(EventEnvelope):
    type: Literal["interaction.requested"]
    interaction_id: Identifier
    kind: InteractionKind
    prompt: str = Field(default="", max_length=20_000)
    payload: dict[str, Any] = Field(default_factory=dict, max_length=128)


class InteractionResolvedEvent(EventEnvelope):
    type: Literal["interaction.resolved"]
    interaction_id: Identifier
    kind: InteractionKind
    resolution: dict[str, Any] = Field(default_factory=dict, max_length=128)


class InteractionCancelledEvent(EventEnvelope):
    type: Literal["interaction.cancelled"]
    interaction_id: Identifier
    kind: InteractionKind
    reason: str = Field(default="cancelled", max_length=2_000)


class InteractionExpiredEvent(EventEnvelope):
    type: Literal["interaction.expired"]
    interaction_id: Identifier
    kind: InteractionKind
    resolution: dict[str, Any] = Field(default_factory=dict, max_length=128)


class SubagentStartedEvent(EventEnvelope):
    type: Literal["subagent.started"]
    subagent_run_id: Identifier
    agent_id: str = Field(default="subagent", max_length=200)
    parent_run_id: OptionalIdentifier = ""
    data: dict[str, Any] = Field(default_factory=dict, max_length=128)


class SubagentFinishedEvent(EventEnvelope):
    type: Literal["subagent.finished"]
    subagent_run_id: Identifier
    result: dict[str, Any] = Field(default_factory=dict, max_length=128)


class SubagentErrorEvent(EventEnvelope):
    type: Literal["subagent.error"]
    subagent_run_id: Identifier
    message: str = Field(default="", max_length=20_000)
    data: dict[str, Any] = Field(default_factory=dict, max_length=128)


class JsonPatchOperation(ProtocolV2Model):
    op: Literal["add", "remove", "replace", "move", "copy", "test"]
    path: str = Field(min_length=1, max_length=4_096)
    value: Any = None
    from_path: str = Field(default="", alias="from", max_length=4_096)


class StateSnapshotEvent(EventEnvelope):
    type: Literal["state.snapshot"]
    state: dict[str, Any] = Field(default_factory=dict, max_length=256)


class StateDeltaEvent(EventEnvelope):
    type: Literal["state.delta"]
    patch: list[JsonPatchOperation] = Field(default_factory=list, max_length=256)


class MessagesSnapshotEvent(EventEnvelope):
    type: Literal["messages.snapshot"]
    messages: list[RuntimeMessage] = Field(default_factory=list, max_length=10_000)


RuntimeEventValue = Annotated[
    Union[
        RunStartedEvent,
        RunFinishedEvent,
        RunErrorEvent,
        RunCancelledEvent,
        RunInterruptedEvent,
        MessageStartedEvent,
        MessageDeltaEvent,
        MessageFinishedEvent,
        ToolStartedEvent,
        ToolArgsEvent,
        ToolArgsFinishedEvent,
        ToolResultEvent,
        ActivitySnapshotEvent,
        ActivityDeltaEvent,
        ActivityFinishedEvent,
        InteractionRequestedEvent,
        InteractionResolvedEvent,
        InteractionCancelledEvent,
        InteractionExpiredEvent,
        SubagentStartedEvent,
        SubagentFinishedEvent,
        SubagentErrorEvent,
        StateSnapshotEvent,
        StateDeltaEvent,
        MessagesSnapshotEvent,
    ],
    Field(discriminator="type"),
]


class RuntimeEvent(RootModel[RuntimeEventValue]):
    pass


RUNTIME_EVENT_ADAPTER = TypeAdapter(RuntimeEventValue)


def parse_runtime_event(value: Any) -> RuntimeEventValue:
    """严格校验字典或模型，并返回具体事件子类。"""

    if isinstance(value, EventEnvelope):
        value = value.model_dump(mode="python")
    return RUNTIME_EVENT_ADAPTER.validate_python(value)


EVENT_MODELS = (
    RunStartedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    RunCancelledEvent,
    RunInterruptedEvent,
    MessageStartedEvent,
    MessageDeltaEvent,
    MessageFinishedEvent,
    ToolStartedEvent,
    ToolArgsEvent,
    ToolArgsFinishedEvent,
    ToolResultEvent,
    ActivitySnapshotEvent,
    ActivityDeltaEvent,
    ActivityFinishedEvent,
    InteractionRequestedEvent,
    InteractionResolvedEvent,
    InteractionCancelledEvent,
    InteractionExpiredEvent,
    SubagentStartedEvent,
    SubagentFinishedEvent,
    SubagentErrorEvent,
    StateSnapshotEvent,
    StateDeltaEvent,
    MessagesSnapshotEvent,
)


__all__ = [
    "EVENT_MODELS",
    "JsonPatchOperation",
    "RuntimeEvent",
    "RuntimeEventValue",
    "parse_runtime_event",
    *[model.__name__ for model in EVENT_MODELS],
]
