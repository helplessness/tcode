"""Schemas and normalization helpers for structured memory."""

from __future__ import annotations

from typing import Any


STRUCTURED_MEMORY_KEYS = [
    "task_type",
    "goal",
    "constraints",
    "decision",
    "outcome",
    "confidence",
    "toolchain",
    "failure_reason",
]


def normalize_structured_fields(fields: dict[str, Any]) -> dict[str, Any]:
    raw = dict(fields or {})
    constraints = raw.get("constraints", [])
    toolchain = raw.get("toolchain", [])
    confidence = raw.get("confidence", 0.0)

    if isinstance(constraints, str):
        constraints = [part.strip() for part in constraints.split(",") if part.strip()]
    elif not isinstance(constraints, list):
        constraints = []

    if isinstance(toolchain, str):
        toolchain = [part.strip() for part in toolchain.split(",") if part.strip()]
    elif not isinstance(toolchain, list):
        toolchain = []

    try:
        confidence = max(0.0, min(1.0, float(confidence or 0.0)))
    except Exception:
        confidence = 0.0

    return {
        "task_type": str(raw.get("task_type", "") or "").strip(),
        "goal": str(raw.get("goal", "") or "").strip(),
        "constraints": [str(v).strip() for v in constraints if str(v).strip()],
        "decision": str(raw.get("decision", "") or "").strip(),
        "outcome": str(raw.get("outcome", "") or "").strip(),
        "confidence": confidence,
        "toolchain": [str(v).strip() for v in toolchain if str(v).strip()],
        "failure_reason": str(raw.get("failure_reason", "") or "").strip(),
    }
