from __future__ import annotations

import json
from typing import Any

from graders.constraint import DEFAULT_FORBIDDEN_CLAIMS
from graders.tool_trace import infer_tool_categories


EVALUATION_CATEGORIES = {
    "structure_prediction",
    "stability_scoring",
    "interface_analysis",
    "affinity_prediction",
    "literature_evidence",
}


def grade_biodesign_depth(
    task: dict[str, Any],
    output: str,
    tool_calls: list[dict[str, Any]],
    subagent_calls: list[dict[str, Any]],
    artifacts: list[str],
) -> dict[str, float]:
    metrics = {str(item) for item in task.get("metrics", [])}
    if not _should_grade(task, metrics):
        return {}

    parsed = _extract_json(output)
    expected_depth = task.get("expected_depth") if isinstance(task.get("expected_depth"), dict) else {}
    min_candidates = _positive_int(expected_depth.get("min_candidates"), default=1)
    min_eval_tools = _positive_int(expected_depth.get("min_eval_tools_per_candidate"), default=1)
    min_distinct_metrics = _positive_int(expected_depth.get("min_distinct_eval_metrics"), default=1)

    candidates = _extract_candidates(parsed)
    evaluation_rows = _extract_evaluation_rows(parsed)
    candidate_count = len(candidates)
    tool_categories = set(infer_tool_categories(tool_calls))
    matrix_categories = _metric_categories(evaluation_rows)
    covered_categories = tool_categories | matrix_categories
    expected_categories = {
        str(item)
        for item in task.get("expected_tool_categories", [])
        if str(item).strip()
    }

    scores: dict[str, float] = {}
    bdb_candidate_count = min(candidate_count / min_candidates, 1.0) if min_candidates else 1.0
    bdb_coverage = (
        len(expected_categories & covered_categories) / len(expected_categories)
        if expected_categories
        else 1.0
    )
    bdb_evaluation_depth = _evaluation_depth_score(
        evaluation_rows,
        candidate_count,
        min_eval_tools,
    )
    bdb_metric_diversity = (
        min(len(matrix_categories) / min_distinct_metrics, 1.0)
        if min_distinct_metrics
        else 1.0
    )
    bdb_verifier_used = 1.0 if _verifier_used(parsed, tool_calls, subagent_calls) else 0.0
    bdb_claim_safety = 0.0 if _claim_hit(output) else 1.0
    bdb_artifact_pass = _bdb_artifact_pass(task, parsed, artifacts)

    values = {
        "bdb_candidate_count": bdb_candidate_count,
        "bdb_coverage": bdb_coverage,
        "bdb_evaluation_depth": bdb_evaluation_depth,
        "bdb_metric_diversity": bdb_metric_diversity,
        "bdb_verifier_used": bdb_verifier_used,
        "bdb_claim_safety": bdb_claim_safety,
        "bdb_artifact_pass": bdb_artifact_pass,
    }
    for key, value in values.items():
        if key in metrics or key == "bdb_artifact_pass":
            scores[key] = value

    if "bdb_style_local_score" in metrics:
        scores["bdb_style_local_score"] = (
            20 * bdb_coverage
            + 20 * bdb_candidate_count
            + 20 * bdb_evaluation_depth
            + 15 * bdb_metric_diversity
            + 10 * bdb_verifier_used
            + 10 * bdb_artifact_pass
            + 5 * bdb_claim_safety
        )

    return scores


def _should_grade(task: dict[str, Any], metrics: set[str]) -> bool:
    if str(task.get("category") or "") == "biodesignbench_style":
        return True
    return any(key.startswith("bdb_") for key in metrics)


def _extract_json(text: str) -> Any:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(value[start : end + 1])
        except Exception:
            return None
    return None


def _extract_candidates(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("candidates") or parsed.get("candidate_table") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    candidates: list[str] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            value = item.get("id") or item.get("candidate_id") or item.get("name")
            candidates.append(str(value or f"candidate_{index}"))
        elif str(item):
            candidates.append(str(item))
    return candidates


def _extract_evaluation_rows(parsed: Any) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("evaluation_matrix") or parsed.get("evaluations") or []
    if isinstance(raw, dict):
        rows: list[dict[str, Any]] = []
        for candidate_id, values in raw.items():
            if isinstance(values, dict):
                for metric, value in values.items():
                    rows.append(
                        {
                            "candidate_id": candidate_id,
                            "metric": metric,
                            "value": value,
                        }
                    )
            elif isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        rows.append({"candidate_id": candidate_id, **item})
            else:
                rows.append({"candidate_id": candidate_id, "value": values})
        return rows
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _metric_categories(rows: list[dict[str, Any]]) -> set[str]:
    categories: set[str] = set()
    for row in rows:
        category = str(
            row.get("metric_category")
            or row.get("category")
            or row.get("tool_category")
            or ""
        ).strip()
        if category:
            categories.add(category)
    return categories


def _evaluation_depth_score(
    rows: list[dict[str, Any]],
    candidate_count: int,
    min_eval_tools: int,
) -> float:
    if candidate_count <= 0 or min_eval_tools <= 0:
        return 0.0
    eval_rows = [
        row for row in rows
        if str(row.get("metric_category") or row.get("category") or row.get("tool_category") or "") in EVALUATION_CATEGORIES
        or not str(row.get("metric_category") or row.get("category") or row.get("tool_category") or "")
    ]
    depth = len(eval_rows) / candidate_count
    return min(depth / min_eval_tools, 1.0)


def _verifier_used(
    parsed: Any,
    tool_calls: list[dict[str, Any]],
    subagent_calls: list[dict[str, Any]],
) -> bool:
    if isinstance(parsed, dict):
        verifier = parsed.get("verifier") or parsed.get("verification_decision")
        if isinstance(verifier, dict) and bool(verifier.get("used", True)):
            return True
        if verifier:
            return True
    categories = set(infer_tool_categories(tool_calls))
    if "review_verification" in categories:
        return True
    for call in subagent_calls or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("agent") or call.get("name") or "").lower()
        if "verifier" in name or "review" in name or "critic" in name:
            return True
    return False


def _claim_hit(output: str) -> bool:
    text = str(output or "").lower()
    return any(item.lower() in text for item in DEFAULT_FORBIDDEN_CLAIMS if item)


def _bdb_artifact_pass(task: dict[str, Any], parsed: Any, artifacts: list[str]) -> float:
    expected = [
        str(item).strip()
        for item in task.get("expected_artifacts", [])
        if str(item).strip()
    ]
    if not expected:
        return 1.0
    observed = {str(item) for item in artifacts or []}
    passed = 0
    for item in expected:
        if item in observed or _structured_artifact_present(item, parsed):
            passed += 1
    return passed / len(expected)


def _structured_artifact_present(name: str, parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    aliases = {
        "candidate_table": ["candidates", "candidate_table"],
        "evaluation_matrix": ["evaluation_matrix", "evaluations"],
        "final_report": ["final_report", "final_recommendation", "limitations"],
        "verification_decision": ["verifier", "verification_decision"],
    }
    key = str(name or "").strip().lower()
    for field in aliases.get(key, [key]):
        if parsed.get(field):
            return True
    return False


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
