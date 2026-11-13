#!/usr/bin/env python3
"""Convert OpenAI SimpleQA CSV into the local eval JSONL format."""

from __future__ import annotations

import argparse
import ast
import json
import random
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "public_data" / "simpleqa" / "simple_qa_test_set.csv"
DEFAULT_OUT = ROOT / "datasets" / "simpleqa_public_tasks.jsonl"


def _load_rows(path: Path) -> list[dict[str, Any]]:
    import pandas as pd  # type: ignore

    df = pd.read_csv(path)
    required = {"problem", "answer"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"SimpleQA CSV missing columns: {missing}")
    return df.to_dict(orient="records")


def _metadata_topic(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        data = ast.literal_eval(value)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("topic") or "").strip()


def _task_from_row(index: int, row: dict[str, Any]) -> dict[str, Any]:
    question = str(row.get("problem") or "").strip()
    answer = str(row.get("answer") or "").strip()
    topic = _metadata_topic(row.get("metadata"))
    return {
        "id": f"simpleqa-{index:04d}",
        "category": "public_simpleqa_factuality",
        "prompt": (
            "请直接回答下面的事实问题。"
            "如果你不知道，请明确说不知道，不要编造。\n"
            f"问题: {question}"
        ),
        "input": {
            "problem": question,
            "topic": topic,
        },
        "expected": {
            "answer": answer,
            "accepted_answers": [answer],
            "source_dataset": "OpenAI SimpleQA",
            "topic": topic,
        },
        "metrics": [
            "answer_match",
            "constraint_pass",
        ],
        "required_scores": [
            "constraint_pass",
        ],
        "success_threshold": 0.5,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"missing SimpleQA CSV: {input_path}")

    rows = _load_rows(input_path)
    rng = random.Random(int(args.seed))
    limit = int(args.limit)
    if limit > 0 and len(rows) > limit:
        rows = rng.sample(rows, limit)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            handle.write(json.dumps(_task_from_row(index, row), ensure_ascii=False) + "\n")

    print(f"wrote {len(rows)} SimpleQA tasks to {out_path}")
    print(f"source: {input_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

