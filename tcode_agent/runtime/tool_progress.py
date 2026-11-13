"""Structured progress events for long-running tool executions."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional


PROGRESS_MARKER = "::tcode-progress"
_FRACTION_PATTERN = re.compile(r"(?<!\d)(?P<current>\d+)\s*/\s*(?P<total>\d+)(?!\d)")
_PROGRESS_HINT_PATTERN = re.compile(
    r"(?:progress|processed|processing|completed|downloaded|parsed|read|"
    r"进度|已读|读取|完成|处理|下载|解析|入库|文献|论文|篇)",
    re.IGNORECASE,
)


def _clean_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _optional_number(value: Any) -> Optional[int | float]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _optional_int(value: Any) -> Optional[int]:
    parsed = _optional_number(value)
    return int(parsed) if parsed is not None else None


@dataclass(frozen=True)
class ToolProgressEvent:
    """A transport-neutral semantic progress update."""

    message: str
    current: Optional[int | float] = None
    total: Optional[int | float] = None
    unit: str = ""
    seq: Optional[int] = None
    stream: str = ""
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value not in (None, "")
        }


def coerce_tool_progress_event(value: Any) -> Optional[ToolProgressEvent]:
    """Normalize a tool-supplied progress value into the shared contract."""

    if isinstance(value, ToolProgressEvent):
        return value
    if isinstance(value, str):
        message = _clean_text(value)
        return ToolProgressEvent(message=message) if message else None
    if not isinstance(value, dict):
        return None

    current = _optional_number(value.get("current"))
    total = _optional_number(value.get("total"))
    if total is not None and total <= 0:
        total = None
    message = _clean_text(value.get("message") or value.get("text"))
    unit = _clean_text(value.get("unit"), limit=48)
    if not message and current is not None and total is not None:
        suffix = f" {unit}" if unit else ""
        message = f"进度 {current}/{total}{suffix}"
    if not message:
        return None

    return ToolProgressEvent(
        message=message,
        current=current,
        total=total,
        unit=unit,
        seq=_optional_int(value.get("seq")),
        stream=_clean_text(value.get("stream"), limit=24),
        status=_clean_text(value.get("status") or "running", limit=24).lower(),
    )


def parse_tool_progress_line(
    line: str,
    *,
    seq: Optional[int] = None,
    stream: str = "",
) -> Optional[ToolProgressEvent]:
    """Parse a structured marker or a conservative human-readable fraction."""

    text = str(line or "").strip()
    if not text:
        return None

    if text.startswith(PROGRESS_MARKER):
        raw_payload = text[len(PROGRESS_MARKER) :].strip()
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("seq", seq)
        payload.setdefault("stream", stream)
        return coerce_tool_progress_event(payload)

    fraction = _FRACTION_PATTERN.search(text)
    if not fraction or not _PROGRESS_HINT_PATTERN.search(text):
        return None
    current = int(fraction.group("current"))
    total = int(fraction.group("total"))
    if total <= 0:
        return None
    return ToolProgressEvent(
        message=_clean_text(text),
        current=current,
        total=total,
        seq=seq,
        stream=_clean_text(stream, limit=24),
    )


async def report_progress_line(
    context: Any,
    line: str,
    *,
    seq: Optional[int] = None,
    stream: str = "",
) -> None:
    """Parse one output line and report it through a ToolContext when meaningful."""

    event = parse_tool_progress_line(line, seq=seq, stream=stream)
    if event is None:
        return
    reporter = getattr(context, "report_progress", None)
    if callable(reporter):
        result = reporter(event)
        if hasattr(result, "__await__"):
            await result
