"""规范化消息和工具快照模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .content import ContentBlock
from .envelope import Identifier, OptionalIdentifier, ProtocolV2Model


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class RuntimeToolCall(ProtocolV2Model):
    tool_call_id: Identifier
    tool_name: str = Field(min_length=1, max_length=300)
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=512)
    state: Literal[
        "pending", "running", "completed", "error", "skipped", "interrupted"
    ] = "pending"
    result: Any = None
    error: str = Field(default="", max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)


class RuntimeMessage(ProtocolV2Model):
    message_id: Identifier
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(default="", max_length=1_000_000)
    content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=256)
    tool_calls: list[RuntimeToolCall] = Field(default_factory=list, max_length=256)
    parent_message_id: OptionalIdentifier = ""
    presentation: str = Field(default="", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)


def runtime_message_from_legacy(source: Any) -> RuntimeMessage:
    """把持久化 Message/dataclass 安全投影为 v2 snapshot。"""

    role = str(_value(source, "role", "assistant") or "assistant")
    if role not in {"user", "assistant", "system", "tool"}:
        role = "assistant"
    message_id = str(_value(source, "id", "") or _value(source, "message_id", "")).strip()
    if not message_id:
        raise ValueError("legacy message is missing an id")

    calls: list[RuntimeToolCall] = []
    for index, raw in enumerate(list(_value(source, "tool_calls", []) or [])[:256]):
        state_value = _value(raw, "state", "pending")
        state = str(getattr(state_value, "value", state_value) or "pending")
        if state not in {"pending", "running", "completed", "error", "skipped", "interrupted"}:
            state = "pending"
        call_id = str(_value(raw, "call_id", "") or f"{message_id}:tool:{index}").strip()
        calls.append(
            RuntimeToolCall(
                tool_call_id=call_id[:200],
                tool_name=str(_value(raw, "tool_name", "unknown") or "unknown")[:300],
                arguments=dict(_value(raw, "arguments", {}) or {}),
                state=state,
                result=_value(raw, "output"),
                error=str(_value(raw, "error", "") or "")[:20_000],
                metadata=dict(list(dict(_value(raw, "metadata", {}) or {}).items())[:64]),
            )
        )

    blocks: list[dict[str, Any]] = []
    for raw in list(_value(source, "content_blocks", []) or [])[:256]:
        if not isinstance(raw, dict):
            continue
        block_type = str(raw.get("type") or "")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(raw.get("text") or "")[:1_000_000]})
        elif block_type == "artifact":
            blocks.append(
                {
                    "type": "artifact",
                    "artifact_id": str(raw.get("artifact_id") or "")[:200],
                    "uri": str(raw.get("uri") or "")[:4096],
                    "media_type": str(raw.get("media_type") or "application/octet-stream")[:200],
                    "title": str(raw.get("title") or "")[:500],
                    "fallback": str(raw.get("fallback") or "")[:20_000],
                }
            )
        else:
            blocks.append(
                {
                    "type": "extension",
                    "name": str(raw.get("type") or "unknown")[:200],
                    "data": dict(list(raw.items())[:128]),
                    "fallback": str(raw.get("fallback") or raw.get("text") or "")[:20_000],
                }
            )

    return RuntimeMessage(
        message_id=message_id[:200],
        role=role,
        content=str(_value(source, "content", "") or "")[:1_000_000],
        content_blocks=blocks,
        tool_calls=calls,
        parent_message_id=str(_value(source, "parent_id", "") or "")[:200],
        presentation=str(_value(source, "presentation", "") or "")[:100],
        metadata={key: value for key, value in list(dict(_value(source, "metadata", {}) or {}).items())[:64]
                  if key not in {"reasoning_content", "reasoning_blocks", "private_prompt", "system_prompt", "chain_of_thought", "hidden_reasoning"}},
    )


__all__ = ["RuntimeMessage", "RuntimeToolCall", "runtime_message_from_legacy"]
