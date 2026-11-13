from __future__ import annotations

import json
import math
from typing import Any


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


def grade_json_valid(task: dict[str, Any], output: str) -> dict[str, float]:
    parsed = _extract_json(output)
    if parsed is None:
        return {"json_valid": 0.0}
    expected = task.get("expected") if isinstance(task.get("expected"), dict) else {}
    schema = expected.get("recommended_schema") or []
    if not schema:
        return {"json_valid": 1.0}
    if not isinstance(parsed, dict):
        return {"json_valid": 0.0}
    missing = [str(field) for field in schema if str(field) not in parsed]
    return {"json_valid": 0.0 if missing else 1.0}


def grade_protein_rank(task: dict[str, Any], output: str) -> dict[str, float]:
    metrics = {str(item) for item in task.get("metrics", [])}
    expected = task.get("expected") if isinstance(task.get("expected"), dict) else {}
    high = [
        str(item)
        for item in (
            expected.get("true_top_mutants")
            or expected.get("high_fitness_mutants")
            or []
        )
        if str(item)
    ]
    if not high:
        return {}

    recommendations = _extract_recommendations(output)
    if not recommendations:
        text = str(output or "")
        recommendations = [mutant for mutant in high if mutant in text]

    candidate_mutants = [
        str(item)
        for item in (
            task.get("input", {}).get("candidate_mutants", [])
            if isinstance(task.get("input"), dict)
            else []
        )
        if str(item)
    ]
    top_k = int(expected.get("top_k") or task.get("input", {}).get("top_k") or len(high) or len(recommendations) or 1)
    top_recommendations = recommendations[: max(1, top_k)]
    high_set = set(high)
    hit_count = len(high_set & set(top_recommendations))
    scores: dict[str, float] = {}

    if "protein_topk_recall" in metrics:
        scores["protein_topk_recall"] = hit_count / len(high_set)

    ndcg = _binary_ndcg_at_k(recommendations, high_set, top_k)
    if ndcg is not None and "protein_ndcg_at_k" in metrics:
        scores["protein_ndcg_at_k"] = ndcg

    if "hit_at_k" in metrics:
        scores["hit_at_k"] = 1.0 if hit_count > 0 else 0.0

    if "enrichment_factor" in metrics:
        scores["enrichment_factor"] = _enrichment_factor(
            recommendations=top_recommendations,
            relevant=high_set,
            candidate_count=len(candidate_mutants) or len(set(recommendations) | high_set),
            top_k=top_k,
        )

    if "Spearman" in metrics or "spearman" in metrics:
        spearman = _spearman_for_recommendations(
            recommendations,
            _scores_by_mutant(expected),
            candidate_mutants,
        )
        if spearman is not None:
            scores["Spearman"] = spearman

    if "candidate_validity" in metrics:
        scores["candidate_validity"] = _candidate_validity(
            recommendations,
            set(candidate_mutants),
        )

    if "duplicate_free" in metrics:
        scores["duplicate_free"] = 1.0 if len(recommendations) == len(set(recommendations)) else 0.0

    if "topk_count_pass" in metrics:
        scores["topk_count_pass"] = 1.0 if len(recommendations) >= top_k else 0.0

    return scores


def _extract_recommendations(text: str) -> list[str]:
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        return []
    raw = (
        parsed.get("ranked_mutants")
        or parsed.get("recommendations")
        or parsed.get("recommended_mutants")
        or parsed.get("mutants")
        or []
    )
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        return []

    recommendations: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = (
                item.get("mutant")
                or item.get("mutation")
                or item.get("variant")
                or item.get("name")
                or item.get("id")
            )
            if value:
                recommendations.append(str(value))
        elif str(item):
            recommendations.append(str(item))
    return recommendations


def _binary_ndcg_at_k(
    recommendations: list[str],
    relevant: set[str],
    top_k: int,
) -> float | None:
    if not relevant:
        return None
    k = max(1, int(top_k))
    dcg = 0.0
    for index, mutant in enumerate(recommendations[:k], start=1):
        rel = 1.0 if mutant in relevant else 0.0
        dcg += rel / math.log2(index + 1)

    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    if idcg <= 0:
        return 0.0
    return dcg / idcg


def _scores_by_mutant(expected: dict[str, Any]) -> dict[str, float]:
    raw = (
        expected.get("scores_by_mutant")
        or expected.get("scores")
        or expected.get("dms_scores")
        or {}
    )
    if not isinstance(raw, dict):
        return {}
    scores: dict[str, float] = {}
    for key, value in raw.items():
        try:
            scores[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return scores


def _candidate_validity(recommendations: list[str], candidates: set[str]) -> float:
    if not recommendations:
        return 0.0
    if not candidates:
        return 1.0
    return sum(1.0 for item in recommendations if item in candidates) / len(recommendations)


def _enrichment_factor(
    *,
    recommendations: list[str],
    relevant: set[str],
    candidate_count: int,
    top_k: int,
) -> float:
    if not recommendations or not relevant or candidate_count <= 0 or top_k <= 0:
        return 0.0
    observed_rate = len(set(recommendations[:top_k]) & relevant) / max(1, min(top_k, len(recommendations)))
    random_rate = min(1.0, len(relevant) / candidate_count)
    if random_rate <= 0:
        return 0.0
    return observed_rate / random_rate


def _spearman_for_recommendations(
    recommendations: list[str],
    scores_by_mutant: dict[str, float],
    candidate_mutants: list[str],
) -> float | None:
    candidates = [item for item in candidate_mutants if item in scores_by_mutant]
    if len(candidates) < 2:
        candidates = list(scores_by_mutant)
    if len(candidates) < 2:
        return None

    actual_order = sorted(candidates, key=lambda item: scores_by_mutant[item], reverse=True)
    actual_ranks = {item: rank for rank, item in enumerate(actual_order, start=1)}
    predicted_ranks: dict[str, float] = {}
    rec_rank = {item: rank for rank, item in enumerate(recommendations, start=1)}
    fallback_rank = len(candidates) + 1
    for item in candidates:
        predicted_ranks[item] = float(rec_rank.get(item, fallback_rank))

    xs = [float(actual_ranks[item]) for item in candidates]
    ys = [predicted_ranks[item] for item in candidates]
    return _pearson(xs, ys)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    denom_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if denom_x <= 0 or denom_y <= 0:
        return None
    return numerator / (denom_x * denom_y)
