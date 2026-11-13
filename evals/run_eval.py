#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
TCODE_ROOT = EVAL_ROOT.parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from core.report import print_summary, summarize_jsonl
from core.runner import run_eval


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run TCode agent eval tasks and write JSONL results.",
    )
    parser.add_argument("--dataset", default="evals/datasets/lite.jsonl")
    parser.add_argument("--adapter", default="fixture", choices=["fixture", "tcode", "command"])
    parser.add_argument("--agent", default="fixture")
    parser.add_argument("--model", default="")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--out", default="evals/results/latest.jsonl")
    parser.add_argument("--workdir", default=str(TCODE_ROOT))
    parser.add_argument("--command", default="")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-tasks", type=int, default=0)
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    dataset = (workdir / args.dataset).resolve() if not Path(args.dataset).is_absolute() else Path(args.dataset)
    out = (workdir / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)

    result_path = run_eval(
        dataset=dataset,
        adapter=args.adapter,
        agent=args.agent,
        model=args.model,
        trials=max(1, args.trials),
        out=out,
        workdir=workdir,
        command=args.command,
        timeout=args.timeout,
        max_tasks=max(0, args.max_tasks),
    )

    summary = summarize_jsonl(result_path)
    summary_path = result_path.with_suffix(result_path.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"results: {result_path}")
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

