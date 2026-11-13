"""TCode Agent Events v2 公共合同。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, create_model

from .capabilities import RuntimeCapabilities
from .content import ArtifactContent, ExtensionContent, TextContent
from .emitter import (
    ReplayWindow,
    RuntimeEventEmitter,
    RuntimeEventReplayBuffer,
    runtime_event_replay,
)
from .envelope import PROTOCOL_ID, PROTOCOL_VERSION, EventEnvelope, ProtocolV2Model
from .errors import ProtocolError
from .examples import protocol_event_examples
from .events import *  # noqa: F403 - event classes are the public protocol surface
from .events import (
    EVENT_MODELS,
    JsonPatchOperation,
    RuntimeEvent,
    RuntimeEventValue,
    parse_runtime_event,
)
from .messages import RuntimeMessage, RuntimeToolCall, runtime_message_from_legacy
from .persistence import (
    PERSISTED_EVENT_TYPES,
    apply_runtime_event_projection,
    runtime_event_session_spec,
    should_persist_runtime_event,
)
from .state_machine import RuntimeEventInvariantError, RuntimeEventStateMachine


PROTOCOL_MODELS: tuple[type[BaseModel], ...] = (
    TextContent,
    ArtifactContent,
    ExtensionContent,
    RuntimeToolCall,
    RuntimeMessage,
    RuntimeCapabilities,
    ProtocolError,
    JsonPatchOperation,
    *EVENT_MODELS,
    RuntimeEvent,
)


def protocol_schema_document() -> dict[str, Any]:
    """返回包含全部 v2 模型的稳定 JSON Schema 文档。"""

    fields = {
        f"contract_{index}": (model, ...)
        for index, model in enumerate(PROTOCOL_MODELS)
    }
    root_model = create_model("TCodeAgentEventsProtocol", **fields)
    schema = root_model.model_json_schema(ref_template="#/$defs/{model}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://tcode.local/schemas/agent-events-v2.json"
    schema["x-contract-id"] = PROTOCOL_ID
    schema["x-protocol-version"] = PROTOCOL_VERSION
    schema["x-exported-models"] = [model.__name__ for model in PROTOCOL_MODELS]
    schema["x-event-examples"] = protocol_event_examples()
    schema.pop("properties", None)
    schema.pop("required", None)
    schema["title"] = "TCode Agent Events v2"
    schema["description"] = (
        "Normalized runtime events shared by TCode WebSocket, AG-UI adapters, "
        "client SDKs and durable projections."
    )
    return schema


__all__ = [
    "ArtifactContent",
    "EventEnvelope",
    "ExtensionContent",
    "JsonPatchOperation",
    "PERSISTED_EVENT_TYPES",
    "PROTOCOL_ID",
    "PROTOCOL_MODELS",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "ProtocolV2Model",
    "ReplayWindow",
    "RuntimeCapabilities",
    "RuntimeEvent",
    "RuntimeEventEmitter",
    "RuntimeEventInvariantError",
    "RuntimeEventReplayBuffer",
    "RuntimeEventStateMachine",
    "RuntimeEventValue",
    "RuntimeMessage",
    "RuntimeToolCall",
    "TextContent",
    "apply_runtime_event_projection",
    "parse_runtime_event",
    "protocol_schema_document",
    "protocol_event_examples",
    "runtime_event_replay",
    "runtime_event_session_spec",
    "runtime_message_from_legacy",
    "should_persist_runtime_event",
    *[model.__name__ for model in EVENT_MODELS],
]
