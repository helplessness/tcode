#!/usr/bin/env python3
"""Create multi-candidate ProteinGym ranking eval tasks.

Each output task is built from one DMS_id assay. The prompt shows the wild-type
sequence plus a shuffled list of candidate mutations. The hidden expected block
stores the real DMS scores and true Top-K mutants for automatic grading.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "public_data" / "proteingym" / "DMS_substitutions.parquet"
DEFAULT_OUT = ROOT / "datasets" / "proteingym_ranking_tasks.jsonl"


FORBIDDEN_STRUCTURE_TOOLS = [
    "gnina_mcp_server",
    "boltz_mcp_server",
    "ligandmpnn_run_mcp_server",
    "rfdiffusion_mcp_server",
    "placer_mcp_server",
    "plip_mcp_server",
]


def _load_frame(path: Path):
    import pandas as pd  # type: ignore

    df = pd.read_parquet(path)
    required = {
        "DMS_id",
        "mutant",
        "DMS_score",
        "DMS_score_bin",
        "target_seq",
        "mutated_sequence",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"ProteinGym parquet missing columns: {missing}")
    return df.dropna(subset=["DMS_id", "mutant", "DMS_score", "target_seq"])


def _sample_assay_rows(
    group_rows: list[dict[str, Any]],
    *,
    candidate_count: int,
    top_k: int,
    min_score_spread: float,
    rng: random.Random,
) -> list[dict[str, Any]]:
    sorted_rows = sorted(
        group_rows,
        key=lambda item: float(item["DMS_score"]),
        reverse=True,
    )
    anchors = sorted_rows[:top_k]
    remaining = sorted_rows[top_k:]
    needed = max(0, candidate_count - len(anchors))
    if len(remaining) > needed:
        remaining = rng.sample(remaining, needed)
    selected = [*anchors, *remaining[:needed]]
    rng.shuffle(selected)
    if min_score_spread > 0:
        selected_scores = [float(row["DMS_score"]) for row in selected]
        if max(selected_scores) - min(selected_scores) < min_score_spread:
            return []
    return selected


def _build_prompt(
    *,
    dms_id: str,
    target_seq: str,
    candidate_mutants: list[str],
    top_k: int,
) -> str:
    candidates = "\n".join(
        f"{idx}. {mutant}" for idx, mutant in enumerate(candidate_mutants, start=1)
    )
    return (
        "你正在做 ProteinGym 风格的突变候选排序评估。\n"
        f"Assay ID: {dms_id}\n"
        f"野生型序列: {target_seq}\n\n"
        f"候选突变如下，请推荐 Top {top_k} 个最可能提高 fitness 的突变。\n"
        f"{candidates}\n\n"
        "只输出 JSON，字段必须包含 recommended_mutants、rationale、limitations。"
        "recommended_mutants 必须按推荐强度从高到低排序。"
        "不要声称这些推荐已经过湿实验验证。"
    )


def _task_from_rows(
    *,
    task_index: int,
    dms_id: str,
    rows: list[dict[str, Any]],
    top_k: int,
    top_percentile: float,
) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda item: float(item["DMS_score"]), reverse=True)
    if top_percentile > 0:
        high_count = max(top_k, int(round(len(sorted_rows) * top_percentile)))
    else:
        high_count = top_k
    true_top = [str(row["mutant"]) for row in sorted_rows[:high_count]]
    candidate_mutants = [str(row["mutant"]) for row in rows]
    target_seq = str(rows[0]["target_seq"])
    scores_by_mutant = {
        str(row["mutant"]): float(row["DMS_score"])
        for row in rows
    }
    bins_by_mutant = {
        str(row["mutant"]): int(float(row.get("DMS_score_bin") or 0))
        for row in rows
    }

    return {
        "id": f"proteingym-ranking-{task_index:04d}",
        "category": "protein_mutation_ranking",
        "taxonomy": {
            "source": "proteingym",
            "DMS_id": dms_id,
        },
        "prompt": _build_prompt(
            dms_id=dms_id,
            target_seq=target_seq,
            candidate_mutants=candidate_mutants,
            top_k=top_k,
        ),
        "input": {
            "DMS_id": dms_id,
            "target_seq": target_seq,
            "candidate_mutants": candidate_mutants,
            "top_k": top_k,
        },
        "expected": {
            "DMS_id": dms_id,
            "top_k": top_k,
            "true_top_mutants": true_top,
            "scores_by_mutant": scores_by_mutant,
            "dms_score_bins_by_mutant": bins_by_mutant,
            "recommended_schema": [
                "recommended_mutants",
                "rationale",
                "limitations",
            ],
        },
        "expected_tools": [],
        "forbidden_tools": FORBIDDEN_STRUCTURE_TOOLS,
        "metrics": [
            "json_valid",
            "constraint_pass",
            "claim_safety",
            "protein_topk_recall",
            "protein_ndcg_at_k",
            "hit_at_k",
            "enrichment_factor",
            "candidate_validity",
            "duplicate_free",
            "topk_count_pass",
            "forbidden_tool_pass",
        ],
        "required_scores": [
            "json_valid",
            "constraint_pass",
            "claim_safety",
            "forbidden_tool_pass",
        ],
        "success_threshold": 0.65,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--num-tasks", type=int, default=30)
    parser.add_argument("--candidate-count", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-candidates", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--top-percentile", type=float, default=0.0)
    parser.add_argument("--min-score-spread", type=float, default=0.0)
    parser.add_argument(
        "--max-target-seq-len",
        type=int,
        default=1200,
        help="Skip assays whose wild-type sequence is longer than this.",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    num_tasks = max(1, int(args.num_tasks))
    candidate_count = max(2, int(args.candidate_count))
    if int(args.max_candidates) > 0:
        candidate_count = min(candidate_count, int(args.max_candidates))
    candidate_count = max(candidate_count, int(args.min_candidates))
    top_k = max(1, min(int(args.top_k), candidate_count))
    rng = random.Random(int(args.seed))

    df = _load_frame(input_path)
    counts = df.groupby("DMS_id").size()
    eligible_ids = [
        str(dms_id)
        for dms_id, count in counts.items()
        if int(count) >= candidate_count
    ]
    rng.shuffle(eligible_ids)

    tasks: list[dict[str, Any]] = []
    for dms_id in eligible_ids:
        group = df[df["DMS_id"] == dms_id]
        target_seq = str(group.iloc[0]["target_seq"])
        if len(target_seq) > int(args.max_target_seq_len):
            continue
        records = group.to_dict(orient="records")
        selected = _sample_assay_rows(
            records,
            candidate_count=candidate_count,
            top_k=top_k,
            min_score_spread=max(0.0, float(args.min_score_spread)),
            rng=rng,
        )
        if len(selected) < candidate_count:
            continue
        tasks.append(
            _task_from_rows(
                task_index=len(tasks),
                dms_id=dms_id,
                rows=selected,
                top_k=top_k,
                top_percentile=max(0.0, float(args.top_percentile)),
            )
        )
        if len(tasks) >= num_tasks:
            break

    if not tasks:
        raise SystemExit("no eligible ProteinGym assay groups found")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")

    print(f"wrote {len(tasks)} ranking tasks to {out_path}")
    print(f"source: {input_path}")
    print(f"candidate_count: {candidate_count}")
    print(f"top_k: {top_k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
