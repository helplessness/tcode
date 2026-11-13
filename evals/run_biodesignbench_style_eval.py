#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
TCODE_ROOT = EVAL_ROOT.parent
DEFAULT_DATASET = EVAL_ROOT / "datasets" / "biodesignbench_style_tasks.jsonl"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from core.report import print_summary, summarize_jsonl
from core.runner import run_eval


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return text.strip("-") or "agent"


def _resolve(value: str, *, workdir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workdir / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BioDesignBench-style local evals.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--adapter", default="fixture", choices=["fixture", "tcode", "command"])
    parser.add_argument("--agent", default="fixture")
    parser.add_argument("--model", default="")
    parser.add_argument("--command", default="")
    parser.add_argument("--workdir", default=str(TCODE_ROOT))
    parser.add_argument("--out", default="")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--max-tasks", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--approach", default="all", choices=["all", "de_novo", "redesign"])
    parser.add_argument(
        "--subject",
        default="all",
        choices=["all", "binder", "enzyme", "antibody", "scaffold", "fluorescent_protein"],
    )
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    dataset = _resolve(args.dataset, workdir=workdir)
    run_dataset = (
        _filtered_dataset(dataset, approach=args.approach, subject=args.subject)
        if args.approach != "all" or args.subject != "all"
        else dataset
    )
    out = (
        _resolve(args.out, workdir=workdir)
        if str(args.out or "").strip()
        else EVAL_ROOT / "results" / f"biodesignbench-style-{_slug(args.adapter)}-{_slug(args.agent)}.jsonl"
    )

    result_path = run_eval(
        dataset=run_dataset,
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
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"results: {result_path}")
    print(f"summary: {summary_path}")
    return 0


def _filtered_dataset(dataset: Path, *, approach: str, subject: str) -> Path:
    out = EVAL_ROOT / "results" / f".filtered-bdb-style-{_slug(approach)}-{_slug(subject)}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with dataset.open(encoding="utf-8") as source, out.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            taxonomy = row.get("taxonomy") if isinstance(row.get("taxonomy"), dict) else {}
            if approach != "all" and str(taxonomy.get("approach") or "") != approach:
                continue
            if subject != "all" and str(taxonomy.get("subject") or "") != subject:
                continue
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
