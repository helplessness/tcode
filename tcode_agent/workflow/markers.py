"""Workflow marker parsing for reviewable agent proposals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_MARKER_RE = re.compile(
    r"\[workflow:(candidate|revision|final_report|not_applicable)\]([\s\S]*?)\[/workflow:\1\]",
    re.IGNORECASE,
)
_MAX_MARKERS = 8
_MAX_PAYLOAD_BYTES = 8192


@dataclass(slots=True)
class WorkflowMarker:
    marker_type: str
    payload: dict[str, Any]
    raw: str


def parse_workflow_markers(text: str) -> list[WorkflowMarker]:
    """Parse bounded workflow proposal markers from assistant text."""

    if not text:
        return []

    markers: list[WorkflowMarker] = []
    for match in _MARKER_RE.finditer(str(text)):
        if len(markers) >= _MAX_MARKERS:
            break
        marker_type = str(match.group(1) or "").strip().lower()
        # Recognize the retired marker only for stripping old worker output.
        # It must never become a proposal or pause an already-started run.
        if marker_type == "not_applicable":
            continue
        raw_payload = str(match.group(2) or "").strip()
        if len(raw_payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            continue
        try:
            payload = json.loads(raw_payload)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if marker_type == "candidate" and not _has_candidate_signal(payload):
            continue
        if marker_type == "revision" and not _has_any_text(payload, ("summary", "reason", "required_revision", "target_stage_id")):
            continue
        if marker_type == "final_report" and not _has_any_text(payload, ("final_report", "report", "summary")):
            continue
        markers.append(
            WorkflowMarker(
                marker_type=marker_type,
                payload=payload,
                raw=str(match.group(0) or ""),
            )
        )
    return markers


def strip_retired_workflow_markers(text: str) -> str:
    """Discard old runtime-fit control markers before normal stage validation."""

    if not text:
        return text
    return _MARKER_RE.sub(
        lambda match: "" if match.group(1).lower() == "not_applicable" else match.group(0),
        str(text),
    ).strip()


def strip_workflow_markers(text: str) -> str:
    """Remove workflow markers from text shown or persisted as summaries."""

    if not text:
        return text
    return _MARKER_RE.sub("", str(text)).replace("\n\n\n", "\n\n").strip()


def _has_candidate_signal(payload: dict[str, Any]) -> bool:
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        return _has_any_text(candidate, ("candidate_id", "summary", "description"))
    return _has_any_text(payload, ("candidate_id", "summary", "description"))


def _has_any_text(payload: dict[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value is not None and not isinstance(value, (dict, list, tuple, set)):
            return True
    return False
