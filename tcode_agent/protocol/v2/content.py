"""跨端可安全降级的 v2 内容块。"""

from __future__ import annotations

from typing import Any, Annotated, Literal, Union

from pydantic import Field

from .envelope import OptionalIdentifier, ProtocolV2Model


class TextContent(ProtocolV2Model):
    type: Literal["text"]
    text: str = Field(max_length=1_000_000)


class ArtifactContent(ProtocolV2Model):
    type: Literal["artifact"]
    artifact_id: OptionalIdentifier = ""
    uri: str = Field(default="", max_length=4_096)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    title: str = Field(default="", max_length=500)
    fallback: str = Field(default="", max_length=20_000)


class ExtensionContent(ProtocolV2Model):
    """未知富内容的受控扩展点；客户端必须优先展示 fallback。"""

    type: Literal["extension"]
    name: str = Field(min_length=1, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict, max_length=128)
    fallback: str = Field(default="", max_length=20_000)


ContentBlock = Annotated[
    Union[TextContent, ArtifactContent, ExtensionContent],
    Field(discriminator="type"),
]


__all__ = ["ArtifactContent", "ContentBlock", "ExtensionContent", "TextContent"]
