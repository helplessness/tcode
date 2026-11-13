#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "datasets" / "combingym_tasks.jsonl"


BUILTIN_ROWS = [
    {"assay_id": "toy-enzyme-a", "variant": "A12V", "order": 1, "score": 0.5},
    {"assay_id": "toy-enzyme-a", "variant": "G35D", "order": 1, "score": 0.7},
    {"assay_id": "toy-enzyme-a", "variant": "L88F", "order": 1, "score": 0.8},
    {"assay_id": "toy-enzyme-a", "variant": "T103M", "order": 1, "score": 0.4},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:G35D", "order": 2, "score": 1.1},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:L88F", "order": 2, "score": 1.0},
    {"assay_id": "toy-enzyme-a", "variant": "G35D:L88F", "order": 2, "score": 1.3},
    {"assay_id": "toy-enzyme-a", "variant": "G35D:T103M", "order": 2, "score": 0.9},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:G35D:L88F", "order": 3, "score": 2.2},
    {"assay_id": "toy-enzyme-a", "variant": "G35D:L88F:T103M", "order": 3, "score": 1.9},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:L88F:T103M", "order": 3, "score": 1.4},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:G35D:T103M", "order": 3, "score": 1.5},
    {"assay_id": "toy-enzyme-a", "variant": "A12V:G35D:L88F:T103M", "order": 4, "score": 2.6},
    {"assay_id": "toy-binder-b", "variant": "E10K", "order": 1, "score": 0.2},
    {"assay_id": "toy-binder-b", "variant": "R22A", "order": 1, "score": 0.4},
    {"assay_id": "toy-binder-b", "variant": "Y41F", "order": 1, "score": 0.6},
    {"assay_id": "toy-binder-b", "variant": "D55N", "order": 1, "score": 0.3},
    {"assay_id": "toy-binder-b", "variant": "E10K:Y41F", "order": 2, "score": 1.0},
    {"assay_id": "toy-binder-b", "variant": "R22A:Y41F", "order": 2, "score": 1.2},
    {"assay_id": "toy-binder-b", "variant": "Y41F:D55N", "order": 2, "score": 0.9},
    {"assay_id": "toy-binder-b", "variant": "E10K:R22A:Y41F", "order": 3, "score": 1.8},
    {"assay_id": "toy-binder-b", "variant": "R22A:Y41F:D55N", "order": 3, "score": 2.0},
    {"assay_id": "toy-binder-b", "variant": "E10K:Y41F:D55N", "order": 3, "score": 1.5},
    {"assay_id": "toy-binder-b", "variant": "E10K:R22A:Y41F:D55N", "order": 4, "score": 2.3},
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare CombinGym-style eval tasks.")
    parser.add_argument("--input", default="", help="Optional CSV with assay_id,variant,order,score columns.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--candidate-count", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--splits", default="1-vs-rest,2-vs-rest,3-vs-rest")
    args = parser.parse_args()

    rows = _load_rows(Path(args.input).expanduser()) if args.input else list(BUILTIN_ROWS)
    rng = random.Random(int(args.seed))
    tasks = build_tasks(
        rows,
        splits=[item.strip() for item in str(args.splits).split(",") if item.strip()],
        candidate_count=max(2, int(args.candidate_count)),
        top_k=max(1, int(args.top_k)),
        rng=rng,
    )
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")
    print(f"wrote {len(tasks)} CombinGym-style tasks to {out_path}")
    return 0


def build_tasks(
    rows: list[dict[str, Any]],
    *,
    splits: list[str],
    candidate_count: int,
    top_k: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    by_assay: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_assay[str(row.get("assay_id") or "default")].append(row)

    tasks: list[dict[str, Any]] = []
    for assay_id, assay_rows in sorted(by_assay.items()):
        for split in splits:
            max_train_order = _split_order(split)
            if max_train_order <= 0:
                continue
            train = [row for row in assay_rows if int(row["order"]) <= max_train_order]
            candidates = [row for row in assay_rows if int(row["order"]) > max_train_order]
            if not train or not candidates:
                continue
            if len(candidates) > candidate_count:
                candidates = rng.sample(candidates, candidate_count)
            candidates = sorted(candidates, key=lambda row: str(row["variant"]))
            sorted_candidates = sorted(candidates, key=lambda row: float(row["score"]), reverse=True)
            true_top = [str(row["variant"]) for row in sorted_candidates[:top_k]]
            scores = {str(row["variant"]): float(row["score"]) for row in candidates}
            candidate_variants = [str(row["variant"]) for row in candidates]
            task_index = len(tasks)
            tasks.append(
                {
                    "id": f"combingym-style-{split}-{task_index:04d}",
                    "category": "combinatorial_mutation_design",
                    "taxonomy": {
                        "source": "combingym_style",
                        "split": split,
                        "assay_id": assay_id,
                    },
                    "prompt": _prompt(assay_id, split, train, candidate_variants, top_k),
                    "input": {
                        "assay_id": assay_id,
                        "split": split,
                        "train_variants": [
                            {
                                "variant": str(row["variant"]),
                                "order": int(row["order"]),
                                "score": float(row["score"]),
                            }
                            for row in train
                        ],
                        "candidate_variants": candidate_variants,
                        "top_k": top_k,
                    },
                    "expected": {
                        "top_k": top_k,
                        "target_order": max_train_order + 1,
                        "true_top_variants": true_top,
                        "scores_by_variant": scores,
                        "recommended_schema": [
                            "ranked_variants",
                            "selection_logic",
                            "limitations",
                        ],
                    },
                    "metrics": [
                        "json_valid",
                        "constraint_pass",
                        "claim_safety",
                        "combin_topk_recall",
                        "combin_ndcg_at_k",
                        "high_order_hit_rate",
                        "variant_validity",
                        "order_pass",
                        "duplicate_free",
                    ],
                    "required_scores": [
                        "json_valid",
                        "constraint_pass",
                        "claim_safety",
                    ],
                    "success_threshold": 0.7,
                }
            )
    return tasks


def _load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    "assay_id": row.get("assay_id") or "default",
                    "variant": row["variant"],
                    "order": int(row.get("order") or str(row["variant"]).count(":") + 1),
                    "score": float(row["score"]),
                }
            )
        return rows


def _split_order(split: str) -> int:
    if split.startswith("1-vs-rest"):
        return 1
    if split.startswith("2-vs-rest"):
        return 2
    if split.startswith("3-vs-rest"):
        return 3
    return 0


def _prompt(
    assay_id: str,
    split: str,
    train_rows: list[dict[str, Any]],
    candidate_variants: list[str],
    top_k: int,
) -> str:
    train_text = "\n".join(
        f"- {row['variant']}: score={float(row['score']):.3f}"
        for row in train_rows
    )
    candidate_text = "\n".join(
        f"{idx}. {variant}" for idx, variant in enumerate(candidate_variants, start=1)
    )
    return (
        "你正在做 CombinGym-style 组合突变评估。\n"
        f"Assay ID: {assay_id}\n"
        f"Split: {split}\n\n"
        "已知低阶突变数据：\n"
        f"{train_text}\n\n"
        f"候选高阶组合如下，请推荐 Top {top_k} 个最可能提升功能的组合突变。\n"
        f"{candidate_text}\n\n"
        "只输出 JSON，字段必须包含 ranked_variants、selection_logic、limitations。"
        "ranked_variants 必须按推荐强度从高到低排序，且只能来自候选列表。"
        "不要声称这些推荐已经过湿实验验证。"
    )


if __name__ == "__main__":
    raise SystemExit(main())
