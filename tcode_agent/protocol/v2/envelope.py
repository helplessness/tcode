"""TCode Agent Events v2 的公共事件信封。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer


PROTOCOL_ID = "tcode.agent.events"
PROTOCOL_VERSION = "2.0.0"

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
OptionalIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=200),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProtocolV2Model(BaseModel):
    """所有 v2 模型默认拒绝未声明字段。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=False,
    )


class EventEnvelope(ProtocolV2Model):
    """所有规范化运行事件共享的、可独立路由的字段。"""

    protocol: Literal["tcode.agent.events"]
    protocol_version: Literal["2.0.0"]
    type: str = Field(min_length=1, max_length=80)
    event_id: Identifier
    session_id: Identifier
    run_id: Identifier
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utc_now)
    causation_id: OptionalIdentifier = ""
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "EventEnvelope",
    "Identifier",
    "OptionalIdentifier",
    "PROTOCOL_ID",
    "PROTOCOL_VERSION",
    "ProtocolV2Model",
    "utc_now",
]
