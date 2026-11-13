#!/usr/bin/env python3
"""Generic team orchestration eval runner.

Use this for datasets that care about subagent routing, verifier usage, and
final task quality. It accepts any JSONL task file supported by the core eval
runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
TCODE_ROOT = EVAL_ROOT.parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from core.report import print_summary, summarize_jsonl
from core.runner import run_eval


def _resolve_under_workdir(value: str, *, workdir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workdir / path).resolve()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return text.strip("-") or "agent"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run generic team orchestration evals.",
    )
    parser.add_argument(
        "--dataset",
        default="evals/datasets/team_orchestration_smoke.jsonl",
        help="JSONL eval dataset. Tasks may include expected_subagents, forbidden_subagents, verifier_required, and normal protein/tool metrics.",
    )
    parser.add_argument("--adapter", default="fixture", choices=["fixture", "tcode", "command"])
    parser.add_argument("--agent", default="fixture")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--command",
        default="",
        help="Command used when --adapter command. The task prompt is passed on stdin.",
    )
    parser.add_argument("--workdir", default=str(TCODE_ROOT))
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--max-tasks", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    dataset = _resolve_under_workdir(args.dataset, workdir=workdir)
    out = (
        _resolve_under_workdir(args.out, workdir=workdir)
        if str(args.out or "").strip()
        else EVAL_ROOT
        / "results"
        / f"team-{_slug(args.adapter)}-{_slug(args.agent)}.jsonl"
    )

    print("running team orchestration eval...")
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

