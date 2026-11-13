"""Derive user-facing mechanism and route limits from the original request."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 10


def _request_text(reaction_input: Mapping[str, Any]) -> str:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                collect(item)

    for key in ("natural_language_request", "target_description", "constraints", "context"):
        collect(reaction_input.get(key))
    return "\n".join(values)


def _bounded(value: int) -> int:
    return max(1, min(MAX_RESULT_LIMIT, value))


def _subject_limit(text: str, subjects: str) -> int | None:
    patterns = (
        rf"(?:{subjects}).{{0,12}}?(?:前|最多|不超过|返回|展示|列出)\s*(\d{{1,2}})\s*(?:条|个|项)?",
        rf"(?:{subjects}).{{0,12}}?(\d{{1,2}})\s*(?:条|个|项)",
        rf"(?:前|最多|不超过|返回|展示|列出)\s*(\d{{1,2}})\s*(?:条|个|项)?(?:的)?(?:{subjects})",
        rf"(\d{{1,2}})\s*(?:条|个|项)(?:的)?(?:{subjects})",
        rf"(?:{subjects}).{{0,8}}?top\s*[-:：]?\s*(\d{{1,2}})",
        rf"top\s*[-:：]?\s*(\d{{1,2}}).{{0,8}}?(?:{subjects})",
    )
    matches = [int(value) for pattern in patterns for value in re.findall(pattern, text, flags=re.I)]
    return _bounded(matches[-1]) if matches else None


def requested_result_limits(reaction_input: Mapping[str, Any]) -> dict[str, int]:
    """Return generation/display targets; absent user guidance defaults to five."""

    text = _request_text(reaction_input)
    mechanisms = _subject_limit(text, r"机制(?:假说)?|弱假说")
    routes = _subject_limit(text, r"(?:反应|催化|酶)?路线|通路")
    candidates = _subject_limit(text, r"候选酶|具体酶|酶候选|候选")
    generic_top = re.findall(r"(?i)(?<![A-Za-z])top\s*[-:：]?\s*(\d{1,2})(?!\d)", text)
    if candidates is None and generic_top:
        candidates = _bounded(int(generic_top[-1]))
    combined = re.findall(
        r"(?:机制(?:假说)?|弱假说)\s*(?:和|与|、|及)\s*(?:路线|通路).{0,12}?(\d{1,2})\s*(?:条|个|项)?",
        text,
        flags=re.I,
    )
    if combined:
        mechanisms = routes = _bounded(int(combined[-1]))
    combined_prefix = re.findall(
        r"(\d{1,2})\s*(?:条|个|项)?\s*(?:机制(?:假说)?|弱假说)\s*(?:和|与|、|及)\s*(?:路线|通路)",
        text,
        flags=re.I,
    )
    if combined_prefix:
        mechanisms = routes = _bounded(int(combined_prefix[-1]))
    return {
        "candidates": candidates or DEFAULT_RESULT_LIMIT,
        "mechanisms": mechanisms or DEFAULT_RESULT_LIMIT,
        "routes": routes or DEFAULT_RESULT_LIMIT,
    }


__all__ = ["DEFAULT_RESULT_LIMIT", "MAX_RESULT_LIMIT", "requested_result_limits"]
