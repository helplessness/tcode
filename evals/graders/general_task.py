from __future__ import annotations

import re
import unicodedata
from typing import Any


BLOCKED_MARKERS = [
    "blocked",
    "无法继续",
    "信息不足",
    "缺少",
    "需要更多信息",
]


CLARIFICATION_MARKERS = [
    "?",
    "？",
    "请提供",
    "需要确认",
    "需要澄清",
    "能否",
]


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\s\u4e00-\u9fff.-]", "", value)
    return value.strip()


def _contains(haystack: str, needle: str) -> bool:
    normalized_haystack = _normalize(haystack)
    normalized_needle = _normalize(needle)
    return bool(normalized_needle and normalized_needle in normalized_haystack)


def _expected_block(task: dict[str, Any]) -> dict[str, Any]:
    expected = task.get("expected")
    return dict(expected) if isinstance(expected, dict) else {}


def grade_general_task(task: dict[str, Any], output: str) -> dict[str, float]:
    expected = _expected_block(task)
    text = str(output or "")
    scores: dict[str, float] = {}

    required_phrases = [
        str(item)
        for item in (
            task.get("required_phrases")
            or expected.get("required_phrases")
            or []
        )
        if str(item).strip()
    ]
    if required_phrases:
        hits = sum(1 for item in required_phrases if _contains(text, item))
        scores["required_phrase_pass"] = hits / len(required_phrases)

    accepted_answers = [
        str(item)
        for item in (
            expected.get("accepted_answers")
            or ([expected.get("answer")] if expected.get("answer") else [])
        )
        if str(item).strip()
    ]
    if accepted_answers:
        scores["answer_match"] = (
            1.0 if any(_contains(text, item) for item in accepted_answers) else 0.0
        )

    if bool(task.get("blocked_required") or expected.get("blocked_required")):
        scores["blocked_pass"] = (
            1.0 if any(_contains(text, marker) for marker in BLOCKED_MARKERS) else 0.0
        )

    if bool(task.get("clarification_required") or expected.get("clarification_required")):
        scores["clarification_pass"] = (
            1.0
            if any(marker in text for marker in CLARIFICATION_MARKERS)
            else 0.0
        )

    return scores

