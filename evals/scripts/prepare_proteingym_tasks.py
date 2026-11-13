#!/usr/bin/env python3
"""Create eval tasks from ProteinGym data.

This script prefers the downloaded parquet file when pandas/pyarrow are
available. If those optional dependencies are missing, it writes a tiny fallback
dataset so the evaluation harness can still be smoke-tested.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "public_data" / "proteingym" / "DMS_substitutions.parquet"
DEFAULT_OUT = ROOT / "datasets" / "protein_tasks.jsonl"


FALLBACK_ROWS = [
    {
        "mutant": "L205R",
        "mutated_sequence": "MSTNPKPQRKTKRNTNRRPQDVKFPGGGQIVGGVYLLPRRGPRLG",
        "DMS_score": 7.08,
        "DMS_score_bin": 1,
    },
    {
        "mutant": "W236L",
        "mutated_sequence": "MSTNPKPQRKTKRNTNRRPQDVKFPGGGQIVGGVYLLPRRGPRLG",
        "DMS_score": 5.99,
        "DMS_score_bin": 1,
    },
    {
        "mutant": "P222K",
        "mutated_sequence": "MSTNPKPQRKTKRNTNRRPQDVKFPGGGQIVGGVYLLPRRGPRLG",
        "DMS_score": -0.95,
        "DMS_score_bin": 0,
    },
]


def _write_jsonl(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for idx, row in enumerate(rows):
            mutant = str(row.get("mutant") or "").strip()
            sequence = str(row.get("mutated_sequence") or "").strip()
            score = float(row.get("DMS_score") or 0.0)
            score_bin = int(row.get("DMS_score_bin") or 0)
            high_fitness = [mutant] if score_bin == 1 else []
            task = {
                "id": f"proteingym-substitution-{idx:04d}",
                "category": "protein_mutation_ranking",
                "prompt": (
                    "请基于 ProteinGym 风格的候选突变信息推荐可能提高 fitness 的突变。"
                    "输出 JSON，字段包含 recommended_mutants、rationale、limitations。"
                    f"\n候选突变: {mutant}"
                    f"\n突变后序列: {sequence[:240]}"
                ),
                "input": {
                    "mutant": mutant,
                    "mutated_sequence": sequence,
                },
                "expected": {
                    "dms_score": score,
                    "dms_score_bin": score_bin,
                    "high_fitness_mutants": high_fitness,
                    "recommended_schema": [
                        "recommended_mutants",
                        "rationale",
                        "limitations",
                    ],
                },
                "metrics": [
                    "json_valid",
                    "constraint_pass",
                    "protein_topk_recall",
                ],
                "required_scores": [
                    "json_valid",
                    "constraint_pass",
                ],
                "success_threshold": 0.75,
            }
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")


def _load_from_parquet(path: Path, limit: int, seed: int) -> list[dict[str, Any]]:
    import pandas as pd  # type: ignore

    df = pd.read_parquet(path)
    required = {"mutant", "mutated_sequence", "DMS_score"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"ProteinGym parquet missing columns: {missing}")
    if "DMS_score_bin" not in df.columns:
        cutoff = df["DMS_score"].median()
        df["DMS_score_bin"] = (df["DMS_score"] >= cutoff).astype(int)

    df = df.dropna(subset=["mutant", "mutated_sequence", "DMS_score"])
    if len(df) > limit:
        df = df.sample(n=limit, random_state=seed)
    return df.to_dict(orient="records")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--require-parquet",
        action="store_true",
        help="Fail instead of using fallback rows when the parquet file cannot be read.",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    limit = max(1, int(args.limit))
    seed = int(args.seed)

    rows: list[dict[str, Any]]
    if input_path.exists():
        try:
            rows = _load_from_parquet(input_path, limit=limit, seed=seed)
            source = str(input_path)
        except Exception as exc:
            if args.require_parquet:
                raise SystemExit(
                    "failed to read ProteinGym parquet. Install a parquet engine first, "
                    "for example:\n"
                    "  python3 -m pip install pyarrow\n"
                    f"original error: {exc}"
                ) from exc
            rows = random.Random(seed).sample(FALLBACK_ROWS, k=min(limit, len(FALLBACK_ROWS)))
            source = f"fallback rows; parquet could not be read: {exc}"
    else:
        if args.require_parquet:
            raise SystemExit(f"missing ProteinGym parquet: {input_path}")
        rows = random.Random(seed).sample(FALLBACK_ROWS, k=min(limit, len(FALLBACK_ROWS)))
        source = f"fallback rows; missing parquet: {input_path}"

    _write_jsonl(rows, out_path)
    print(f"wrote {len(rows)} tasks to {out_path}")
    print(f"source: {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
