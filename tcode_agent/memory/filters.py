"""Structured, storage-agnostic filters for memory entries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


MEMORY_FILTER_FIELDS = frozenset(
    {
        "id",
        "content",
        "source",
        "task_type",
        "goal",
        "decision",
        "outcome",
        "confidence",
        "failure_reason",
        "created_at",
        "updated_at",
        "tags",
        "constraints",
        "toolchain",
    }
)
MEMORY_FILTER_OPERATORS = frozenset(
    {"eq", "neq", "contains", "in", "gt", "gte", "lt", "lte"}
)


class MemoryFilterError(ValueError):
    pass


def _entry_value(entry: Any, field: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(field)
    return getattr(entry, field, None)


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _contains(actual: Any, expected: Any) -> bool:
    expected_values = _as_sequence(expected)
    if isinstance(actual, (list, tuple, set, frozenset)):
        normalized = {str(item).lower() for item in actual}
        return all(str(item).lower() in normalized for item in expected_values)
    actual_text = str(actual or "").lower()
    return all(str(item).lower() in actual_text for item in expected_values)


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "contains":
        return _contains(actual, expected)
    if operator == "in":
        candidates = _as_sequence(expected)
        if isinstance(actual, (list, tuple, set, frozenset)):
            return any(item in candidates for item in actual)
        return actual in candidates
    if operator in {"eq", "neq"}:
        equal = actual == expected
        if isinstance(actual, str) and isinstance(expected, str):
            equal = actual.lower() == expected.lower()
        return equal if operator == "eq" else not equal

    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError) as exc:
        raise MemoryFilterError(
            f"operator '{operator}' requires numeric values"
        ) from exc
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[operator]


def _validate_field_condition(field: str, condition: Any) -> None:
    if field not in MEMORY_FILTER_FIELDS:
        raise MemoryFilterError(f"unsupported memory filter field: {field}")
    if not isinstance(condition, dict):
        return
    unknown = set(condition) - MEMORY_FILTER_OPERATORS
    if unknown:
        raise MemoryFilterError(
            "unsupported memory filter operator(s): " + ", ".join(sorted(unknown))
        )


def validate_memory_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise MemoryFilterError("memory filters must be an object")
    for key, condition in filters.items():
        if key in {"all", "any"}:
            if not isinstance(condition, list):
                raise MemoryFilterError(f"'{key}' must be a list of filter objects")
            for child in condition:
                validate_memory_filters(child)
        elif key == "not":
            validate_memory_filters(condition)
        else:
            _validate_field_condition(key, condition)
    return filters


def memory_matches_filters(entry: Any, filters: dict[str, Any] | None) -> bool:
    validated = validate_memory_filters(filters)
    if not validated:
        return True

    results: list[bool] = []
    for field, condition in validated.items():
        if field == "all":
            results.append(all(memory_matches_filters(entry, child) for child in condition))
            continue
        if field == "any":
            results.append(any(memory_matches_filters(entry, child) for child in condition))
            continue
        if field == "not":
            results.append(not memory_matches_filters(entry, condition))
            continue

        actual = _entry_value(entry, field)
        operations = condition if isinstance(condition, dict) else {"eq": condition}
        results.append(
            all(_compare(actual, operator, expected) for operator, expected in operations.items())
        )
    return all(results)


def apply_memory_filters(
    entries: Iterable[Any],
    filters: dict[str, Any] | None,
) -> list[Any]:
    validated = validate_memory_filters(filters)
    if not validated:
        return list(entries)
    return [entry for entry in entries if memory_matches_filters(entry, validated)]
