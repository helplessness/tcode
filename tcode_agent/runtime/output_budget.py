"""Bounded text projections shared by tools, sandboxes, and model history."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BoundedText:
    """A bounded projection plus the size of the original text."""

    text: str
    original_chars: int
    shown_chars: int
    truncated: bool

    @property
    def omitted_chars(self) -> int:
        return max(0, self.original_chars - self.shown_chars)


@dataclass(frozen=True)
class BoundedBytes:
    data: bytes
    original_bytes: int
    shown_bytes: int
    truncated: bool


def _marker(label: str, *, original_chars: int, shown_chars: int) -> str:
    return (
        f"\n...[{label}: original_chars={original_chars}, "
        f"shown_chars={shown_chars}]...\n"
    )


def bound_text(
    value: Any,
    *,
    max_chars: int,
    label: str = "output truncated",
    head_ratio: float = 0.7,
) -> BoundedText:
    """Return a head/tail projection whose rendered size never exceeds max_chars."""

    text = "" if value is None else str(value)
    limit = max(256, int(max_chars))
    if len(text) <= limit:
        return BoundedText(text, len(text), len(text), False)

    ratio = min(0.9, max(0.1, float(head_ratio)))
    marker = _marker(label, original_chars=len(text), shown_chars=0)
    content_limit = max(0, limit - len(marker))
    head_chars = min(len(text), int(content_limit * ratio))
    tail_chars = max(0, content_limit - head_chars)
    shown = head_chars + tail_chars
    marker = _marker(label, original_chars=len(text), shown_chars=shown)

    # The digit count in shown_chars can change the marker length. Recalculate
    # until the projection is guaranteed to fit the configured boundary.
    while head_chars + tail_chars + len(marker) > limit and tail_chars > 0:
        tail_chars -= 1
        shown = head_chars + tail_chars
        marker = _marker(label, original_chars=len(text), shown_chars=shown)
    while head_chars + tail_chars + len(marker) > limit and head_chars > 0:
        head_chars -= 1
        shown = head_chars + tail_chars
        marker = _marker(label, original_chars=len(text), shown_chars=shown)

    projected = text[:head_chars] + marker + (text[-tail_chars:] if tail_chars else "")
    return BoundedText(projected[:limit], len(text), shown, True)


def project_tool_output(value: Any, *, max_chars: int) -> BoundedText:
    """Bound one tool result while keeping oversized JSON syntactically valid."""

    text = "" if value is None else str(value)
    limit = max(1000, int(max_chars))
    if len(text) <= limit:
        return BoundedText(text, len(text), len(text), False)

    try:
        json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return bound_text(
            text,
            max_chars=limit,
            label="tool output truncated",
        )

    preview_budget = max(64, (limit - 512) // 2)
    while preview_budget >= 16:
        payload = {
            "_tcode_output_truncated": True,
            "original_chars": len(text),
            "preview_head": text[:preview_budget],
            "preview_tail": text[-preview_budget:],
            "guidance": "Request a narrower result or use an artifact path.",
        }
        projected = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(projected) <= limit:
            shown = min(len(text), preview_budget * 2)
            return BoundedText(projected, len(text), shown, True)
        preview_budget //= 2

    projected = json.dumps(
        {
            "_tcode_output_truncated": True,
            "original_chars": len(text),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return BoundedText(projected[:limit], len(text), 0, True)


class BoundedTextBuffer:
    """Streaming head/tail buffer with memory usage bounded by max_chars."""

    def __init__(self, max_chars: int, *, head_ratio: float = 0.7):
        self.max_chars = max(256, int(max_chars))
        ratio = min(0.9, max(0.1, float(head_ratio)))
        self._head_limit = max(1, int(self.max_chars * ratio))
        self._tail_limit = max(1, self.max_chars - self._head_limit)
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self.original_chars = 0

    @property
    def truncated(self) -> bool:
        return self.original_chars > self.max_chars

    def append(self, value: Any) -> None:
        text = "" if value is None else str(value)
        if not text:
            return
        self.original_chars += len(text)

        if len(self._head) < self._head_limit:
            take = min(self._head_limit - len(self._head), len(text))
            self._head += text[:take]
            text = text[take:]
        if not text:
            return

        self._tail.append(text)
        self._tail_chars += len(text)
        while self._tail and self._tail_chars > self._tail_limit:
            excess = self._tail_chars - self._tail_limit
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                self._tail_chars -= len(first)
            else:
                self._tail[0] = first[excess:]
                self._tail_chars -= excess

    def render(self, *, label: str = "output truncated") -> BoundedText:
        retained = self._head + "".join(self._tail)
        if not self.truncated:
            return BoundedText(
                retained,
                self.original_chars,
                len(retained),
                False,
            )

        marker = _marker(
            label,
            original_chars=self.original_chars,
            shown_chars=0,
        )
        content_limit = max(0, self.max_chars - len(marker))
        desired_head = min(len(self._head), int(content_limit * 0.7))
        tail_text = "".join(self._tail)
        desired_tail = min(len(tail_text), max(0, content_limit - desired_head))
        shown = desired_head + desired_tail
        marker = _marker(
            label,
            original_chars=self.original_chars,
            shown_chars=shown,
        )
        while desired_head + desired_tail + len(marker) > self.max_chars and desired_tail:
            desired_tail -= 1
            shown = desired_head + desired_tail
            marker = _marker(
                label,
                original_chars=self.original_chars,
                shown_chars=shown,
            )
        text = (
            self._head[:desired_head]
            + marker
            + (tail_text[-desired_tail:] if desired_tail else "")
        )
        return BoundedText(text[: self.max_chars], self.original_chars, shown, True)


class BoundedBytesBuffer:
    """Streaming byte buffer retaining fixed-size head and tail windows."""

    def __init__(self, max_bytes: int, *, head_ratio: float = 0.7):
        self.max_bytes = max(256, int(max_bytes))
        ratio = min(0.9, max(0.1, float(head_ratio)))
        self._head_limit = max(1, int(self.max_bytes * ratio))
        self._tail_limit = max(1, self.max_bytes - self._head_limit)
        self._head = bytearray()
        self._tail = bytearray()
        self.original_bytes = 0

    @property
    def truncated(self) -> bool:
        return self.original_bytes > self.max_bytes

    def append(self, value: bytes | bytearray | memoryview) -> None:
        data = bytes(value)
        if not data:
            return
        self.original_bytes += len(data)
        if len(self._head) < self._head_limit:
            take = min(self._head_limit - len(self._head), len(data))
            self._head.extend(data[:take])
            data = data[take:]
        if not data:
            return
        self._tail.extend(data)
        if len(self._tail) > self._tail_limit:
            del self._tail[: len(self._tail) - self._tail_limit]

    def render(self, *, label: str = "output truncated") -> BoundedBytes:
        if not self.truncated:
            data = bytes(self._head + self._tail)
            return BoundedBytes(data, self.original_bytes, len(data), False)

        marker_template = (
            "\n...[{label}: original_bytes={original}, shown_bytes={shown}]...\n"
        )
        marker = marker_template.format(
            label=label,
            original=self.original_bytes,
            shown=0,
        ).encode("ascii", errors="replace")
        content_limit = max(0, self.max_bytes - len(marker))
        head_bytes = min(len(self._head), int(content_limit * 0.7))
        tail_bytes = min(len(self._tail), max(0, content_limit - head_bytes))
        shown = head_bytes + tail_bytes
        marker = marker_template.format(
            label=label,
            original=self.original_bytes,
            shown=shown,
        ).encode("ascii", errors="replace")
        while head_bytes + tail_bytes + len(marker) > self.max_bytes and tail_bytes:
            tail_bytes -= 1
            shown = head_bytes + tail_bytes
            marker = marker_template.format(
                label=label,
                original=self.original_bytes,
                shown=shown,
            ).encode("ascii", errors="replace")
        data = (
            bytes(self._head[:head_bytes])
            + marker
            + (bytes(self._tail[-tail_bytes:]) if tail_bytes else b"")
        )
        return BoundedBytes(data[: self.max_bytes], self.original_bytes, shown, True)


__all__ = [
    "BoundedText",
    "BoundedTextBuffer",
    "BoundedBytes",
    "BoundedBytesBuffer",
    "bound_text",
    "project_tool_output",
]
