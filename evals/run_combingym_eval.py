#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
TCODE_ROOT = EVAL_ROOT.parent
DEFAULT_DATASET = EVAL_ROOT / "datasets" / "combingym_tasks.jsonl"
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
    parser = argparse.ArgumentParser(description="Run CombinGym-style evals.")
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
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--split", default="all", choices=["all", "1-vs-rest", "2-vs-rest", "3-vs-rest"])
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    dataset = _resolve(args.dataset, workdir=workdir)
    if args.regenerate or not dataset.exists():
        _prepare_dataset(dataset)
    run_dataset = _filtered_dataset(dataset, split=args.split) if args.split != "all" else dataset
    out = (
        _resolve(args.out, workdir=workdir)
        if str(args.out or "").strip()
        else EVAL_ROOT / "results" / f"combingym-{_slug(args.adapter)}-{_slug(args.agent)}.jsonl"
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


def _prepare_dataset(dataset: Path) -> None:
    script = EVAL_ROOT / "scripts" / "prepare_combingym_tasks.py"
    subprocess.run([sys.executable, str(script), "--out", str(dataset)], check=True)


def _filtered_dataset(dataset: Path, *, split: str) -> Path:
    out = EVAL_ROOT / "results" / f".filtered-combingym-{_slug(split)}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with dataset.open(encoding="utf-8") as source, out.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            taxonomy = row.get("taxonomy") if isinstance(row.get("taxonomy"), dict) else {}
            if str(taxonomy.get("split") or row.get("split") or "") == split:
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
