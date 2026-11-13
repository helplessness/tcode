"""Codex/Claude/通用 NDJSON 的有界解析器。"""

from __future__ import annotations

import json
from typing import Any


class HarnessOutputError(ValueError):
    pass


class JsonLineDecoder:
    def __init__(self, *, max_line_bytes: int = 1_048_576, max_total_bytes: int = 32_000_000) -> None:
        self.max_line_bytes = max_line_bytes
        self.max_total_bytes = max_total_bytes
        self.buffer = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.total += len(chunk)
        if self.total > self.max_total_bytes:
            raise HarnessOutputError("harness stdout exceeded total byte limit")
        self.buffer.extend(chunk)
        if len(self.buffer) > self.max_line_bytes and b"\n" not in self.buffer:
            raise HarnessOutputError("harness stdout line exceeded byte limit")
        result: list[dict[str, Any]] = []
        while b"\n" in self.buffer:
            raw, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            if len(raw) > self.max_line_bytes:
                raise HarnessOutputError("harness stdout line exceeded byte limit")
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HarnessOutputError("harness emitted invalid JSONL") from exc
            if isinstance(value, dict):
                result.append(value)
        return result

    def finish(self) -> list[dict[str, Any]]:
        if not self.buffer.strip():
            return []
        return self.feed(b"\n")


def semantic_events_from_json(value: dict[str, Any], stream_format: str) -> list[dict[str, Any]]:
    if stream_format == "codex-jsonl":
        return _codex_events(value)
    if stream_format == "claude-stream-json":
        return _claude_events(value)
    if isinstance(value.get("delta"), str):
        return [{"kind": "text", "delta": value["delta"]}]
    if isinstance(value.get("text"), str):
        return [{"kind": "text", "delta": value["text"]}]
    return []


def _codex_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = str(value.get("type") or "")
    item = value.get("item") if isinstance(value.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    if event_type in {"item.started", "item.updated", "item.completed"}:
        if item_type in {"agent_message", "message"}:
            text = str(item.get("text") or item.get("content") or "")
            return [{"kind": "text", "delta": text}] if text and event_type != "item.updated" else []
        if item_type in {"command_execution", "mcp_tool_call", "tool_call", "file_change"}:
            call_id = str(item.get("id") or item.get("call_id") or "")
            name = str(item.get("name") or item_type)
            if event_type == "item.started":
                return [{"kind": "tool_start", "id": call_id, "name": name, "arguments": item.get("arguments") or {"command": item.get("command")}}]
            if event_type == "item.completed":
                failed = str(item.get("status") or "completed") in {"failed", "error"}
                return [{"kind": "tool_result", "id": call_id, "name": name, "result": item.get("aggregated_output") or item.get("result") or item.get("output"), "error": str(item.get("error") or "") if failed else ""}]
    if event_type in {"turn.failed", "error"}:
        return [{"kind": "error", "message": str(value.get("message") or value.get("error") or "Codex failed")}]
    return []


def _claude_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = str(value.get("type") or "")
    if event_type == "assistant":
        message = value.get("message") if isinstance(value.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        return [
            {"kind": "text", "delta": str(block.get("text") or "")}
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
    if event_type == "stream_event":
        event = value.get("event") if isinstance(value.get("event"), dict) else {}
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [{"kind": "text", "delta": str(delta["text"])}]
    if event_type == "result" and bool(value.get("is_error")):
        return [{"kind": "error", "message": str(value.get("result") or "Claude failed")}]
    return []


__all__ = ["HarnessOutputError", "JsonLineDecoder", "semantic_events_from_json"]
