#!/usr/bin/env python3
"""One-command ProteinGym ranking eval runner.

This wrapper prepares the ProteinGym ranking dataset when needed, runs the
selected agent adapter, and writes both JSONL trial records and a summary JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
TCODE_ROOT = EVAL_ROOT.parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from core.report import print_summary, summarize_jsonl
from core.runner import run_eval


DEFAULT_PARQUET = EVAL_ROOT / "public_data" / "proteingym" / "DMS_substitutions.parquet"
DEFAULT_DATASET = EVAL_ROOT / "datasets" / "proteingym_ranking_tasks.jsonl"


def _resolve_under_workdir(value: str, *, workdir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workdir / path).resolve()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return text.strip("-") or "agent"


def _default_out(adapter: str, agent: str) -> Path:
    name = f"proteingym-ranking-{_slug(adapter)}-{_slug(agent)}.jsonl"
    return EVAL_ROOT / "results" / name


def _prepare_dataset(
    *,
    parquet: Path,
    dataset: Path,
    regenerate: bool,
    num_tasks: int,
    candidate_count: int,
    top_k: int,
    seed: int,
    max_target_seq_len: int,
    min_candidates: int,
    max_candidates: int,
    top_percentile: float,
    min_score_spread: float,
) -> None:
    if dataset.exists() and not regenerate:
        print(f"dataset exists: {dataset}")
        return

    if not parquet.exists():
        raise FileNotFoundError(f"ProteinGym parquet not found: {parquet}")

    script = EVAL_ROOT / "scripts" / "prepare_proteingym_ranking_tasks.py"
    cmd = [
        sys.executable,
        str(script),
        "--input",
        str(parquet),
        "--out",
        str(dataset),
        "--num-tasks",
        str(max(1, num_tasks)),
        "--candidate-count",
        str(max(2, candidate_count)),
        "--top-k",
        str(max(1, top_k)),
        "--seed",
        str(seed),
        "--max-target-seq-len",
        str(max(1, max_target_seq_len)),
        "--min-candidates",
        str(max(1, min_candidates)),
        "--max-candidates",
        str(max(1, max_candidates)),
        "--top-percentile",
        str(max(0.0, top_percentile)),
        "--min-score-spread",
        str(max(0.0, min_score_spread)),
    ]
    print("preparing dataset...")
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and run ProteinGym ranking evals for TCode agents.",
    )
    parser.add_argument("--adapter", default="fixture", choices=["fixture", "tcode", "command"])
    parser.add_argument("--agent", default="fixture")
    parser.add_argument("--model", default="")
    parser.add_argument("--command", default="")
    parser.add_argument("--workdir", default=str(TCODE_ROOT))
    parser.add_argument("--parquet", default=str(DEFAULT_PARQUET))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default="")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=3,
        help="Limit tasks for cheap first runs. Use 0 to run the full dataset.",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--num-tasks", type=int, default=30)
    parser.add_argument("--candidate-count", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-target-seq-len", type=int, default=1200)
    parser.add_argument("--min-candidates", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--top-percentile", type=float, default=0.0)
    parser.add_argument("--min-score-spread", type=float, default=0.0)
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    parquet = _resolve_under_workdir(args.parquet, workdir=workdir)
    dataset = _resolve_under_workdir(args.dataset, workdir=workdir)
    out = (
        _resolve_under_workdir(args.out, workdir=workdir)
        if str(args.out or "").strip()
        else _default_out(args.adapter, args.agent)
    )

    _prepare_dataset(
        parquet=parquet,
        dataset=dataset,
        regenerate=bool(args.regenerate),
        num_tasks=int(args.num_tasks),
        candidate_count=int(args.candidate_count),
        top_k=int(args.top_k),
        seed=int(args.seed),
        max_target_seq_len=int(args.max_target_seq_len),
        min_candidates=int(args.min_candidates),
        max_candidates=int(args.max_candidates),
        top_percentile=float(args.top_percentile),
        min_score_spread=float(args.min_score_spread),
    )

    print("running eval...")
    print(f"dataset: {dataset}")
    print(f"adapter: {args.adapter}")
    print(f"agent: {args.agent}")
    print(f"max_tasks: {max(0, int(args.max_tasks))}")

    result_path = run_eval(
        dataset=dataset,
        adapter=args.adapter,
        agent=args.agent,
        model=args.model,
        trials=max(1, int(args.trials)),
        out=out,
        workdir=workdir,
        command=args.command,
        timeout=max(1, int(args.timeout)),
        max_tasks=max(0, int(args.max_tasks)),
    )

    summary = summarize_jsonl(result_path)
    summary_path = result_path.with_suffix(result_path.suffix + ".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_summary(summary)
    print(f"results: {result_path}")
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
