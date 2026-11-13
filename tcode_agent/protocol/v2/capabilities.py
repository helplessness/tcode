"""运行时与客户端的 v2 能力协商模型。"""

from __future__ import annotations

from pydantic import Field

from .envelope import PROTOCOL_VERSION, ProtocolV2Model


class RuntimeCapabilities(ProtocolV2Model):
    protocol_version: str = PROTOCOL_VERSION
    event_types: list[str] = Field(default_factory=list, max_length=128)
    content_types: list[str] = Field(default_factory=lambda: ["text", "artifact", "extension"])
    supports_replay: bool = True
    supports_state_delta: bool = True
    supports_interactions: bool = True
    max_replay_events: int = Field(default=2_048, ge=1, le=100_000)


__all__ = ["RuntimeCapabilities"]
