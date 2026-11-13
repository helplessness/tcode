from __future__ import annotations

import re
from typing import Any


EVIDENCE_PATTERNS = [
    r"\bevidence\b",
    r"\bcitation\b",
    r"\bsource\b",
    r"\bartifact\b",
    r"\btool_output\b",
    r"\bPMID[:\s]?\d+\b",
    r"\bDOI[:\s]?[0-9.]+/",
    r"文献",
    r"证据",
    r"来源",
]


def grade_evidence_trace(
    task: dict[str, Any],
    output: str,
    tool_calls: list[dict[str, Any]],
    *,
    returncode: int = 0,
    error: str = "",
) -> dict[str, float]:
    scores: dict[str, float] = {}
    metrics = {str(item) for item in task.get("metrics", [])}
    requires_evidence = bool(task.get("requires_evidence")) or "evidence_rate" in metrics
    expect_blocked = bool(task.get("expect_blocked")) or "blocked_when_insufficient_info" in metrics
    wants_trace = bool(task.get("requires_trace")) or "trace_completeness" in metrics

    if requires_evidence:
        scores["evidence_rate"] = 1.0 if _has_evidence(output) else 0.0

    if expect_blocked:
        scores["blocked_when_insufficient_info"] = (
            1.0 if _looks_blocked(output) else 0.0
        )

    if wants_trace:
        scores["trace_completeness"] = _trace_completeness(tool_calls)

    if "failure_rate" in metrics:
        scores["failure_rate"] = 1.0 if int(returncode or 0) != 0 or str(error or "").strip() else 0.0

    return scores


def _has_evidence(output: str) -> bool:
    text = str(output or "")
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in EVIDENCE_PATTERNS)


def _looks_blocked(output: str) -> bool:
    text = str(output or "")
    lowered = text.lower()
    return (
        "blocked" in lowered
        or "缺少" in text
        or "无法" in text
        or "不能可靠" in text
        or "insufficient" in lowered
        or "missing" in lowered
    )


def _trace_completeness(tool_calls: list[dict[str, Any]]) -> float:
    if not tool_calls:
        return 0.0
    total = 0.0
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        checks = [
            bool(str(call.get("tool_name") or call.get("name") or "").strip()),
            "arguments" in call or "input" in call,
            any(key in call for key in ("state", "status", "returncode", "error")),
            any(key in call for key in ("output", "output_ref", "artifact", "artifacts")),
        ]
        total += sum(1.0 for passed in checks if passed) / len(checks)
    return total / len(tool_calls)
