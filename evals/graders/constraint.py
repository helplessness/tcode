from __future__ import annotations

from typing import Any


DEFAULT_FORBIDDEN_CLAIMS = [
    "实验验证证明",
    "已在实验中确认",
    "wet-lab validated",
    "experimentally validated",
]


def grade_constraints(task: dict[str, Any], output: str) -> dict[str, float]:
    text = str(output or "")
    forbidden_phrases = [str(item) for item in task.get("forbidden_phrases", [])]
    forbidden = [*DEFAULT_FORBIDDEN_CLAIMS, *forbidden_phrases]
    required = [str(item) for item in task.get("required_phrases", [])]

    forbidden_hit = any(item and item.lower() in text.lower() for item in forbidden)
    claim_hit = any(
        item and item.lower() in text.lower()
        for item in DEFAULT_FORBIDDEN_CLAIMS
    )
    required_ok = all(item.lower() in text.lower() for item in required if item)
    return {
        "constraint_pass": 1.0 if (not forbidden_hit and required_ok) else 0.0,
        "claim_safety": 0.0 if claim_hit else 1.0,
    }
