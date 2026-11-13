from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "tool_category_map.json"


@lru_cache(maxsize=1)
def _tool_category_map() -> dict[str, list[str]]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(category): [str(item) for item in aliases if str(item).strip()]
        for category, aliases in data.items()
        if isinstance(aliases, list)
    }


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tool_names(tool_calls: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool_name") or call.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _tool_categories(tool_calls: list[dict[str, Any]]) -> list[str]:
    categories: list[str] = []
    category_map = _tool_category_map()
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        explicit = str(call.get("tool_category") or call.get("category") or "").strip()
        if explicit:
            categories.append(explicit)
            continue
        name = str(call.get("tool_name") or call.get("name") or "").strip()
        if not name:
            continue
        matched = _category_for_tool_name(name, category_map)
        if matched:
            categories.append(matched)
    return categories


def _category_for_tool_name(
    name: str,
    category_map: dict[str, list[str]],
) -> str:
    norm_name = _norm(name)
    if not norm_name:
        return ""
    best_category = ""
    best_len = 0
    for category, aliases in category_map.items():
        for alias in aliases:
            norm_alias = _norm(alias)
            if not norm_alias:
                continue
            if norm_alias in norm_name or norm_name in norm_alias:
                if len(norm_alias) > best_len:
                    best_category = category
                    best_len = len(norm_alias)
    return best_category


def _matches(actual: str, expected: str) -> bool:
    return _norm(actual) == _norm(expected)


def _f1_scores(expected: list[str], actual: list[str], prefix: str) -> dict[str, float]:
    expected_unique = list(dict.fromkeys(item for item in expected if item))
    actual_unique = list(dict.fromkeys(item for item in actual if item))
    if not expected_unique:
        return {}

    matched_expected = [
        item
        for item in expected_unique
        if any(_matches(actual_item, item) for actual_item in actual_unique)
    ]
    matched_actual = [
        item
        for item in actual_unique
        if any(_matches(item, expected_item) for expected_item in expected_unique)
    ]
    precision = len(matched_actual) / len(actual_unique) if actual_unique else 0.0
    recall = len(matched_expected) / len(expected_unique)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )
    return {
        f"{prefix}_precision": precision,
        f"{prefix}_recall": recall,
        f"{prefix}_f1": f1,
    }


def _any_match(actual_items: list[str], expected: str) -> bool:
    return any(_matches(actual, expected) for actual in actual_items)


def grade_tool_trace(task: dict[str, Any], tool_calls: list[dict[str, Any]]) -> dict[str, float]:
    expected = [str(item).strip() for item in task.get("expected_tools", []) if str(item).strip()]
    forbidden = [str(item).strip() for item in task.get("forbidden_tools", []) if str(item).strip()]
    expected_categories = [
        str(item).strip()
        for item in task.get("expected_tool_categories", [])
        if str(item).strip()
    ]
    forbidden_categories = [
        str(item).strip()
        for item in task.get("forbidden_tool_categories", [])
        if str(item).strip()
    ]
    names = _tool_names(tool_calls)
    categories = _tool_categories(tool_calls)
    scores: dict[str, float] = {}

    if expected:
        scores.update(_f1_scores(expected, names, "tool"))

    if forbidden:
        forbidden_hit = any(_any_match(names, item) for item in forbidden)
        scores["forbidden_tool_pass"] = 0.0 if forbidden_hit else 1.0

    if expected_categories:
        scores.update(_f1_scores(expected_categories, categories, "tool_category"))

    if forbidden_categories:
        forbidden_category_hit = any(
            _any_match(categories, item) for item in forbidden_categories
        )
        scores["forbidden_tool_category_pass"] = (
            0.0 if forbidden_category_hit else 1.0
        )

    return scores


def infer_tool_categories(tool_calls: list[dict[str, Any]]) -> list[str]:
    return _tool_categories(tool_calls)
