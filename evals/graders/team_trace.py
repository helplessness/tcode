from __future__ import annotations

from typing import Any


def _subagent_names(subagent_calls: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in subagent_calls or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("agent") or call.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _matches(actual: str, expected: str) -> bool:
    a = actual.strip().lower().replace("_", "-")
    e = expected.strip().lower().replace("_", "-")
    if not a or not e:
        return False
    return a == e or e in a or a in e


def _any_match(names: list[str], expected: str) -> bool:
    return any(_matches(name, expected) for name in names)


def grade_team_trace(
    task: dict[str, Any],
    subagent_calls: list[dict[str, Any]],
) -> dict[str, float]:
    expected_block = task.get("expected") if isinstance(task.get("expected"), dict) else {}
    expected = [
        str(item).strip()
        for item in (
            task.get("expected_subagents")
            or expected_block.get("expected_subagents")
            or []
        )
        if str(item).strip()
    ]
    forbidden = [
        str(item).strip()
        for item in (
            task.get("forbidden_subagents")
            or expected_block.get("forbidden_subagents")
            or []
        )
        if str(item).strip()
    ]
    verifier_names = [
        str(item).strip()
        for item in (
            task.get("verifier_subagents")
            or expected_block.get("verifier_subagents")
            or ["verifier", "critic", "reviewer", "protein-design-loop-verifier"]
        )
        if str(item).strip()
    ]
    verifier_required = bool(
        task.get("verifier_required")
        or expected_block.get("verifier_required")
    )

    names = _subagent_names(subagent_calls)
    scores: dict[str, float] = {}

    if expected:
        expected_hits = sum(1 for item in expected if _any_match(names, item))
        actual_expected_hits = sum(
            1 for name in set(names)
            if any(_matches(name, item) for item in expected)
        )
        precision = actual_expected_hits / len(set(names)) if set(names) else 0.0
        recall = expected_hits / len(expected)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall > 0
            else 0.0
        )
        scores.update(
            {
                "subagent_precision": precision,
                "subagent_recall": recall,
                "subagent_f1": f1,
            }
        )

    if forbidden:
        forbidden_hit = any(_any_match(names, item) for item in forbidden)
        scores["forbidden_subagent_pass"] = 0.0 if forbidden_hit else 1.0

    if verifier_required:
        verifier_hit = any(_any_match(names, item) for item in verifier_names)
        scores["verifier_used"] = 1.0 if verifier_hit else 0.0

    return scores

