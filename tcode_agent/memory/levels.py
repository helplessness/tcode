"""Tiered memory helpers (L0/L1/L2)."""

from __future__ import annotations

from typing import Any


def build_l0_summary(
    *,
    task_type: str,
    goal: str,
    outcome: str,
    confidence: float,
    failure_reason: str,
) -> str:
    parts = []
    if task_type:
        parts.append(f"task_type={task_type}")
    if goal:
        parts.append(f"goal={goal[:120]}")
    if outcome:
        parts.append(f"outcome={outcome[:160]}")
    if confidence:
        parts.append(f"confidence={round(float(confidence), 3)}")
    if failure_reason:
        parts.append(f"failure={failure_reason[:120]}")
    return " | ".join(part for part in parts if part)


def build_l1_overview(
    *,
    constraints: list[str],
    decision: str,
    toolchain: list[str],
    tags: list[str],
    source: str,
) -> str:
    parts = []
    if source:
        parts.append(f"source={source}")
    if constraints:
        parts.append(f"constraints={', '.join(constraints[:6])}")
    if decision:
        parts.append(f"decision={decision[:240]}")
    if toolchain:
        parts.append(f"toolchain={', '.join(toolchain[:8])}")
    if tags:
        parts.append(f"tags={', '.join(tags[:8])}")
    return "\n".join(parts)


def build_l2_detail(
    *,
    content: str,
    outcome: str,
    raw_context: str = "",
) -> str:
    sections = []
    if content:
        sections.append(f"content:\n{content}")
    if outcome and outcome != content:
        sections.append(f"outcome:\n{outcome}")
    if raw_context:
        sections.append(f"raw_context:\n{raw_context}")
    return "\n\n".join(sections)


def choose_memory_level(level: str, item: Any) -> str:
    normalized = str(level or "l1").strip().lower()
    if normalized == "l0":
        return str(getattr(item, "l0_summary", "") or getattr(item, "content", ""))
    if normalized == "l2":
        return str(getattr(item, "l2_detail", "") or getattr(item, "content", ""))
    return str(getattr(item, "l1_overview", "") or getattr(item, "content", ""))
