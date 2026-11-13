from __future__ import annotations

import json
import math
import re
from typing import Any


MUTATION_RE = re.compile(r"^([A-Za-z*])(\d+)([A-Za-z*])$")


def grade_combin_rank(task: dict[str, Any], output: str) -> dict[str, float]:
    metrics = {str(item) for item in task.get("metrics", [])}
    expected = task.get("expected") if isinstance(task.get("expected"), dict) else {}
    high = [
        normalize_variant(str(item))
        for item in (
            expected.get("true_top_variants")
            or expected.get("top_k_variants")
            or expected.get("high_fitness_variants")
            or []
        )
        if str(item)
    ]
    if not high:
        return {}

    recommendations = [normalize_variant(item) for item in _extract_recommendations(output)]
    input_block = task.get("input") if isinstance(task.get("input"), dict) else {}
    candidates = [
        normalize_variant(str(item))
        for item in (
            input_block.get("candidate_variants")
            or expected.get("candidate_variants")
            or []
        )
        if str(item)
    ]
    scores_by_variant = _scores_by_variant(expected)
    top_k = int(expected.get("top_k") or input_block.get("top_k") or len(high) or len(recommendations) or 1)
    top_recommendations = recommendations[: max(1, top_k)]
    high_set = set(high)
    hit_count = len(high_set & set(top_recommendations))

    scores: dict[str, float] = {}
    if "combin_topk_recall" in metrics:
        scores["combin_topk_recall"] = hit_count / len(high_set)

    if "combin_ndcg_at_k" in metrics:
        ndcg = _ndcg_at_k(top_recommendations, high_set, scores_by_variant, top_k)
        if ndcg is not None:
            scores["combin_ndcg_at_k"] = ndcg

    if "high_order_hit_rate" in metrics:
        scores["high_order_hit_rate"] = 1.0 if hit_count > 0 else 0.0

    if "Spearman" in metrics or "spearman" in metrics:
        spearman = _spearman_for_recommendations(
            recommendations,
            scores_by_variant,
            candidates,
        )
        if spearman is not None:
            scores["Spearman"] = spearman

    if "variant_validity" in metrics:
        scores["variant_validity"] = _variant_validity(recommendations, set(candidates))

    if "order_pass" in metrics:
        target_order = _target_order(task)
        scores["order_pass"] = _order_pass(recommendations, target_order)

    if "duplicate_free" in metrics:
        scores["duplicate_free"] = 1.0 if len(recommendations) == len(set(recommendations)) else 0.0

    return scores


def normalize_variant(value: str) -> str:
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return ""
    parts = [part for part in re.split(r"[:;,]+", text) if part]
    parsed: list[tuple[int, str]] = []
    for part in parts:
        match = MUTATION_RE.match(part)
        if not match:
            return text
        parsed.append((int(match.group(2)), part))
    parsed.sort(key=lambda item: (item[0], item[1]))
    return ":".join(part for _, part in parsed)


def variant_order(value: str) -> int:
    text = normalize_variant(value)
    if not text:
        return 0
    return len([part for part in text.split(":") if part])


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


def _extract_recommendations(text: str) -> list[str]:
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        return []
    raw = (
        parsed.get("ranked_variants")
        or parsed.get("recommended_variants")
        or parsed.get("variants")
        or parsed.get("ranked_mutants")
        or parsed.get("recommended_mutants")
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
                item.get("variant")
                or item.get("mutant")
                or item.get("mutation")
                or item.get("name")
                or item.get("id")
            )
            if value:
                recommendations.append(str(value))
        elif str(item):
            recommendations.append(str(item))
    return recommendations


def _scores_by_variant(expected: dict[str, Any]) -> dict[str, float]:
    raw = (
        expected.get("scores_by_variant")
        or expected.get("scores_by_variants")
        or expected.get("scores")
        or {}
    )
    if not isinstance(raw, dict):
        return {}
    scores: dict[str, float] = {}
    for key, value in raw.items():
        try:
            scores[normalize_variant(str(key))] = float(value)
        except (TypeError, ValueError):
            continue
    return scores


def _ndcg_at_k(
    recommendations: list[str],
    relevant: set[str],
    scores_by_variant: dict[str, float],
    top_k: int,
) -> float | None:
    if not relevant:
        return None
    k = max(1, int(top_k))
    use_continuous = bool(scores_by_variant)

    def gain(variant: str) -> float:
        if use_continuous:
            return max(0.0, scores_by_variant.get(variant, 0.0))
        return 1.0 if variant in relevant else 0.0

    dcg = 0.0
    for index, variant in enumerate(recommendations[:k], start=1):
        dcg += gain(variant) / math.log2(index + 1)

    if use_continuous:
        ideal_items = sorted(relevant, key=lambda item: scores_by_variant.get(item, 0.0), reverse=True)
        ideal_gains = [gain(item) for item in ideal_items[:k]]
    else:
        ideal_gains = [1.0] * min(len(relevant), k)
    idcg = sum(value / math.log2(index + 1) for index, value in enumerate(ideal_gains, start=1))
    if idcg <= 0:
        return 0.0
    return dcg / idcg


def _variant_validity(recommendations: list[str], candidates: set[str]) -> float:
    if not recommendations:
        return 0.0
    parse_ok = [variant for variant in recommendations if all(MUTATION_RE.match(part) for part in variant.split(":"))]
    if not candidates:
        return len(parse_ok) / len(recommendations)
    return sum(1.0 for item in recommendations if item in candidates) / len(recommendations)


def _target_order(task: dict[str, Any]) -> int:
    taxonomy = task.get("taxonomy") if isinstance(task.get("taxonomy"), dict) else {}
    split = str(taxonomy.get("split") or task.get("split") or "")
    match = re.match(r"(\d+)-vs-rest", split)
    if match:
        return int(match.group(1)) + 1
    expected = task.get("expected") if isinstance(task.get("expected"), dict) else {}
    try:
        return int(expected.get("target_order") or 0)
    except (TypeError, ValueError):
        return 0


def _order_pass(recommendations: list[str], target_order: int) -> float:
    if not recommendations:
        return 0.0
    if target_order <= 0:
        return 1.0
    return sum(1.0 for item in recommendations if variant_order(item) >= target_order) / len(recommendations)


def _spearman_for_recommendations(
    recommendations: list[str],
    scores_by_variant: dict[str, float],
    candidates: list[str],
) -> float | None:
    items = [item for item in candidates if item in scores_by_variant]
    if len(items) < 2:
        items = list(scores_by_variant)
    if len(items) < 2:
        return None
    actual_order = sorted(items, key=lambda item: scores_by_variant[item], reverse=True)
    actual_ranks = {item: rank for rank, item in enumerate(actual_order, start=1)}
    rec_rank = {item: rank for rank, item in enumerate(recommendations, start=1)}
    fallback = len(items) + 1
    xs = [float(actual_ranks[item]) for item in items]
    ys = [float(rec_rank.get(item, fallback)) for item in items]
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
