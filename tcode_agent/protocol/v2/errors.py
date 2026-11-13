"""v2 稳定错误合同。"""

from __future__ import annotations

from pydantic import Field

from .envelope import OptionalIdentifier, ProtocolV2Model


class ProtocolError(ProtocolV2Model):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=20_000)
    request_id: OptionalIdentifier = ""
    retryable: bool = False
    details: dict = Field(default_factory=dict, max_length=32)


__all__ = ["ProtocolError"]
