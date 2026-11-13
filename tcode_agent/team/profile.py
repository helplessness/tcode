"""Worker profiling and adaptive dispatch scoring."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Optional

from .registry import WorkerInfo


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_RE.finditer(str(text or ""))}


def worker_profile_summary(worker: WorkerInfo) -> dict:
    total_runs = worker.success_count + worker.failure_count
    success_rate = worker.success_count / total_runs if total_runs else 0.5
    return {
        **asdict(worker),
        "success_rate": round(success_rate, 3),
    }


def score_worker_for_task(
    worker: WorkerInfo,
    *,
    task_prompt: str,
    required_tools: Optional[list[str]] = None,
    risk_level: str = "medium",
) -> float:
    prompt_tokens = _tokenize(task_prompt)
    skill_tokens = _tokenize(" ".join(worker.skill_tags + [worker.role, worker.name]))
    tool_tokens = {str(t).strip().lower() for t in (required_tools or []) if str(t).strip()}
    common_tools = {str(t).strip().lower() for t in worker.common_tools}

    task_match = len(prompt_tokens & skill_tokens) / max(1, len(prompt_tokens) or 1)

    total_runs = worker.success_count + worker.failure_count
    recent_success = worker.success_count / total_runs if total_runs else 0.5

    tool_fit = 0.0
    if tool_tokens:
        tool_fit = len(tool_tokens & common_tools) / max(1, len(tool_tokens))
    elif common_tools:
        tool_fit = 0.15

    risk_penalty = 0.0
    if risk_level == "high" and worker.permission_preset == "readonly":
        risk_penalty = 0.3
    elif risk_level == "high" and worker.permission_preset == "developer":
        risk_penalty = 0.1

    backlog_penalty = 0.25 if worker.status == "busy" else 0.0

    return task_match + recent_success + tool_fit - risk_penalty - backlog_penalty
