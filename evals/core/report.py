from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


SCORE_GROUPS = {
    "process": {
        "json_valid",
        "constraint_pass",
        "claim_safety",
        "evidence_rate",
        "trace_completeness",
        "blocked_when_insufficient_info",
    },
    "tooling": {
        "tool_precision",
        "tool_recall",
        "tool_f1",
        "tool_category_precision",
        "tool_category_recall",
        "tool_category_f1",
        "forbidden_tool_pass",
        "forbidden_tool_category_pass",
        "subagent_precision",
        "subagent_recall",
        "subagent_f1",
        "verifier_used",
    },
    "protein": {
        "protein_topk_recall",
        "protein_ndcg_at_k",
        "hit_at_k",
        "Spearman",
        "enrichment_factor",
        "candidate_validity",
        "duplicate_free",
        "topk_count_pass",
    },
    "combinatorial": {
        "combin_topk_recall",
        "combin_ndcg_at_k",
        "high_order_hit_rate",
        "variant_validity",
        "order_pass",
        "duplicate_free",
        "Spearman",
    },
    "biodesign": {
        "bdb_coverage",
        "bdb_candidate_count",
        "bdb_evaluation_depth",
        "bdb_metric_diversity",
        "bdb_verifier_used",
        "bdb_claim_safety",
        "bdb_artifact_pass",
        "bdb_style_local_score",
    },
    "system": {
        "artifact_pass",
        "failure_rate",
    },
}


def summarize_jsonl(path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))

    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_agent[str(row.get("agent") or "")].append(row)

    agents: dict[str, Any] = {}
    for agent, items in by_agent.items():
        scores_by_key: dict[str, list[float]] = defaultdict(list)
        for item in items:
            for key, value in dict(item.get("scores") or {}).items():
                if isinstance(value, (int, float)):
                    scores_by_key[key].append(float(value))

        agents[agent] = {
            "trials": len(items),
            "task_success_rate": mean([1.0 if item.get("success") else 0.0 for item in items]) if items else 0.0,
            "mean_aggregate_score": mean([float(item.get("aggregate_score") or 0.0) for item in items]) if items else 0.0,
            "avg_duration_s": mean([float(item.get("duration_s") or 0.0) for item in items]) if items else 0.0,
            "scores": {
                key: mean(values)
                for key, values in sorted(scores_by_key.items())
                if values
            },
        }
        agents[agent]["score_groups"] = _score_groups(agents[agent]["scores"])
        agents[agent]["by_category"] = _summaries_by_key(items, "category")
        agents[agent]["by_taxonomy"] = _summaries_by_taxonomy(items)

    return {
        "total_trials": len(rows),
        "agents": agents,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("Evaluation summary")
    print(f"total_trials: {summary.get('total_trials', 0)}")
    for agent, data in dict(summary.get("agents") or {}).items():
        print(
            f"- {agent}: success={data.get('task_success_rate', 0.0):.3f}, "
            f"score={data.get('mean_aggregate_score', 0.0):.3f}, "
            f"duration={data.get('avg_duration_s', 0.0):.2f}s"
        )
        scores = dict(data.get("scores") or {})
        if scores:
            parts = ", ".join(f"{k}={v:.3f}" for k, v in scores.items())
            print(f"  scores: {parts}")


def _score_groups(scores: dict[str, float]) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for group, keys in SCORE_GROUPS.items():
        values = {
            key: float(scores[key])
            for key in sorted(keys)
            if key in scores and isinstance(scores[key], (int, float))
        }
        if values:
            grouped[group] = values
    return grouped


def _summaries_by_key(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[str(item.get(key) or "unknown")].append(item)
    return {
        group: _summarize_items(group_items)
        for group, group_items in sorted(grouped.items())
    }


def _summaries_by_taxonomy(items: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        taxonomy = item.get("taxonomy") if isinstance(item.get("taxonomy"), dict) else {}
        if not taxonomy:
            continue
        for key, value in taxonomy.items():
            if str(value).strip():
                grouped[f"{key}={value}"].append(item)
    return {
        group: _summarize_items(group_items)
        for group, group_items in sorted(grouped.items())
    }


def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    scores_by_key: dict[str, list[float]] = defaultdict(list)
    for item in items:
        for key, value in dict(item.get("scores") or {}).items():
            if isinstance(value, (int, float)):
                scores_by_key[key].append(float(value))
    scores = {
        key: mean(values)
        for key, values in sorted(scores_by_key.items())
        if values
    }
    return {
        "trials": len(items),
        "task_success_rate": mean([1.0 if item.get("success") else 0.0 for item in items]) if items else 0.0,
        "mean_aggregate_score": mean([float(item.get("aggregate_score") or 0.0) for item in items]) if items else 0.0,
        "scores": scores,
        "score_groups": _score_groups(scores),
    }
