from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import mean
from typing import Any

from core.agent_adapter import run_agent
from core.schema import EvalTask
from graders.artifact import grade_artifacts
from graders.biodesign_depth import grade_biodesign_depth
from graders.combin_rank import grade_combin_rank
from graders.constraint import grade_constraints
from graders.evidence_trace import grade_evidence_trace
from graders.general_task import grade_general_task
from graders.protein_rank import grade_json_valid, grade_protein_rank
from graders.team_trace import grade_team_trace
from graders.tool_trace import grade_tool_trace


def load_tasks(path: Path, *, max_tasks: int = 0) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            tasks.append(EvalTask.from_dict(json.loads(line)))
            if max_tasks and len(tasks) >= max_tasks:
                break
    return tasks


def grade_trial(
    task: EvalTask,
    result: Any,
    *,
    workdir: Path,
) -> tuple[dict[str, float], float, bool]:
    raw = task.raw
    scores: dict[str, float] = {}
    metrics = set(task.metrics)

    if "json_valid" in metrics:
        scores.update(grade_json_valid(raw, result.final_output))
    scores.update(grade_general_task(raw, result.final_output))
    scores.update(grade_constraints(raw, result.final_output))
    scores.update(grade_tool_trace(raw, result.tool_calls))
    scores.update(grade_team_trace(raw, getattr(result, "subagent_calls", [])))
    scores.update(
        grade_evidence_trace(
            raw,
            result.final_output,
            result.tool_calls,
            returncode=result.returncode,
            error=result.error,
        )
    )
    scores.update(
        grade_artifacts(
            raw,
            result.artifacts,
            workdir=workdir,
            output=result.final_output,
        )
    )
    protein_metrics = {
        "protein_topk_recall",
        "protein_ndcg_at_k",
        "hit_at_k",
        "Spearman",
        "spearman",
        "enrichment_factor",
        "candidate_validity",
        "duplicate_free",
        "topk_count_pass",
    }
    if metrics & protein_metrics:
        scores.update(grade_protein_rank(raw, result.final_output))
    if (
        "combin_topk_recall" in metrics
        or "combin_ndcg_at_k" in metrics
        or "high_order_hit_rate" in metrics
    ):
        scores.update(grade_combin_rank(raw, result.final_output))
    scores.update(
        grade_biodesign_depth(
            raw,
            result.final_output,
            result.tool_calls,
            getattr(result, "subagent_calls", []),
            result.artifacts,
        )
    )

    aggregate = mean([_aggregate_value(key, value) for key, value in scores.items()]) if scores else 0.0
    required = [str(item) for item in raw.get("required_scores", []) if str(item)]
    if required:
        success = all(scores.get(key, 0.0) >= 1.0 for key in required)
    else:
        success = aggregate >= float(raw.get("success_threshold", 0.8))
    if result.returncode != 0 or result.error:
        success = False

    return scores, aggregate, success


def _aggregate_value(key: str, value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if key.endswith("_local_score") or numeric > 1.0:
        numeric = numeric / 100.0 if numeric > 1.0 else numeric
    return min(max(numeric, 0.0), 1.0)


def run_eval(
    *,
    dataset: Path,
    adapter: str,
    agent: str,
    model: str,
    trials: int,
    out: Path,
    workdir: Path,
    command: str = "",
    timeout: int = 900,
    max_tasks: int = 0,
) -> Path:
    tasks = load_tasks(dataset, max_tasks=max_tasks)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8") as handle:
        for task in tasks:
            for trial in range(max(1, trials)):
                started = time.time()
                try:
                    result = run_agent(
                        task,
                        adapter=adapter,
                        agent=agent,
                        model=model,
                        workdir=workdir,
                        command=command,
                        timeout=timeout,
                    )
                    scores, aggregate, success = grade_trial(
                        task,
                        result,
                        workdir=workdir,
                    )
                    row = {
                        "task_id": task.id,
                        "category": task.category,
                        "taxonomy": task.raw.get("taxonomy", {}),
                        "agent": agent,
                        "adapter": adapter,
                        "model": model,
                        "trial": trial,
                        "success": success,
                        "aggregate_score": aggregate,
                        "scores": scores,
                        **result.to_dict(),
                    }
                    if result.duration_s <= 0:
                        row["duration_s"] = time.time() - started
                except Exception as exc:
                    row = {
                        "task_id": task.id,
                        "category": task.category,
                        "taxonomy": task.raw.get("taxonomy", {}),
                        "agent": agent,
                        "adapter": adapter,
                        "model": model,
                        "trial": trial,
                        "success": False,
                        "aggregate_score": 0.0,
                        "scores": {},
                        "duration_s": time.time() - started,
                        "final_output": "",
                        "tool_calls": [],
                        "subagent_calls": [],
                        "usage": {},
                        "artifacts": [],
                        "messages": [],
                        "stderr": "",
                        "returncode": 1,
                        "error": str(exc),
                    }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return out
