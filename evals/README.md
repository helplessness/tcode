# TCode Agent Evaluation

This directory contains a small, project-native evaluation harness for TCode agents.

Start with the no-model smoke test:

```bash
cd /path/to/tcode
python evals/run_eval.py --dataset evals/datasets/lite.jsonl --adapter fixture --agent fixture --trials 1 --out evals/results/smoke.jsonl
```

Then run the same dataset against a real TCode agent:

```bash
cd /path/to/tcode
python evals/run_eval.py --dataset evals/datasets/lite.jsonl --adapter tcode --agent protein_orchestrator --trials 1 --out evals/results/tcode-lite.jsonl
```

The `tcode` adapter invokes `scripts/tcode_agent_eval_runner.py`. Its stdout is
a single JSON object containing `final_output`, `tool_calls`, `subagent_calls`,
`usage`, `artifacts`, `messages`, `duration_s`, and `error`; agent diagnostic
logs remain on stderr. Fixture runs emit the same trace fields, so general,
team, and protein result JSONL files share one schema.

ProteinGym public data can be downloaded with:

```bash
cd /path/to/tcode
bash evals/scripts/download_public_data.sh --dataset dms_substitutions
python evals/scripts/prepare_proteingym_tasks.py --limit 50 --out evals/datasets/protein_tasks.jsonl
```

To use the downloaded parquet from the `tf` conda environment, install a parquet
engine once if needed:

```bash
python3 -m pip install pyarrow
cd /path/to/tcode
python3 evals/scripts/prepare_proteingym_tasks.py \
  --require-parquet \
  --limit 50 \
  --out evals/datasets/protein_tasks.jsonl
```

`pandas` alone is not enough to read parquet; it needs `pyarrow` or
`fastparquet`.

The harness writes JSONL trial records and a summary JSON next to the output file.

For a stricter ProteinGym ranking benchmark, generate one task per `DMS_id`
assay. Each task contains many candidate mutations and hides the real DMS score
ranking in `expected`.

```bash
cd /path/to/tcode
python3 evals/scripts/prepare_proteingym_ranking_tasks.py \
  --num-tasks 30 \
  --candidate-count 30 \
  --top-k 5 \
  --out evals/datasets/proteingym_ranking_tasks.jsonl

python3 evals/run_eval.py \
  --dataset evals/datasets/proteingym_ranking_tasks.jsonl \
  --adapter fixture \
  --agent fixture \
  --trials 1 \
  --out evals/results/proteingym-ranking-fixture-smoke.jsonl \
  --max-tasks 3
```

The ranking dataset scores `protein_topk_recall` and `protein_ndcg_at_k`, and
also checks that unrelated structure/docking tools are not called.

The recommended one-command wrapper is:

```bash
cd /path/to/tcode

# Cheap smoke test; does not call a real model.
python3 evals/run_proteingym_ranking_eval.py \
  --adapter fixture \
  --agent fixture \
  --max-tasks 3

# Real TCode agent run; start small.
python3 evals/run_proteingym_ranking_eval.py \
  --adapter tcode \
  --agent protein_orchestrator \
  --max-tasks 3

# Full local ranking dataset.
python3 evals/run_proteingym_ranking_eval.py \
  --adapter tcode \
  --agent protein_orchestrator \
  --max-tasks 0
```

Use `--regenerate` if you want to recreate the ranking dataset from the parquet
with different values such as `--num-tasks`, `--candidate-count`, or `--top-k`.

Generic team orchestration evals use:

```bash
cd /path/to/tcode

# Smoke test for subagent routing metrics; no real model call.
python3 evals/run_team_orchestration_eval.py \
  --adapter fixture \
  --agent fixture \
  --max-tasks 2

# Run a TCode agent as the orchestrator.
python3 evals/run_team_orchestration_eval.py \
  --dataset evals/datasets/team_orchestration_smoke.jsonl \
  --adapter tcode \
  --agent protein_orchestrator \
  --max-tasks 1

# Run any external team orchestrator. The task prompt is passed on stdin.
python3 evals/run_team_orchestration_eval.py \
  --dataset evals/datasets/team_orchestration_smoke.jsonl \
  --adapter command \
  --agent external-team \
  --command "/path/to/team_orchestrator_command" \
  --max-tasks 1
```

Team tasks may define `expected_subagents`, `forbidden_subagents`,
`verifier_required`, and `verifier_subagents`. The runner scores
`subagent_f1`, `forbidden_subagent_pass`, and `verifier_used` in addition to
normal output/domain metrics.

General capability evals use:

```bash
cd /path/to/tcode

# Local general smoke set; no real model call.
python3 evals/run_general_agent_eval.py \
  --adapter fixture \
  --agent fixture \
  --dataset evals/datasets/general_agent_smoke.jsonl \
  --max-tasks 6

# Public SimpleQA factuality set; no real model call.
python3 evals/run_general_agent_eval.py \
  --adapter fixture \
  --agent fixture \
  --dataset evals/datasets/simpleqa_public_tasks.jsonl \
  --max-tasks 5

# Real TCode agent on the local general set.
python3 evals/run_general_agent_eval.py \
  --adapter tcode \
  --agent protein_orchestrator \
  --dataset evals/datasets/general_agent_smoke.jsonl \
  --max-tasks 3

# Real TCode agent on a small SimpleQA slice.
python3 evals/run_general_agent_eval.py \
  --adapter tcode \
  --agent protein_orchestrator \
  --dataset evals/datasets/simpleqa_public_tasks.jsonl \
  --max-tasks 5
```

Public SimpleQA data is downloaded from OpenAI's `simple-evals` public storage:

```bash
bash evals/scripts/download_public_general_data.sh --dataset simpleqa
python3 evals/scripts/prepare_simpleqa_tasks.py \
  --limit 100 \
  --out evals/datasets/simpleqa_public_tasks.jsonl
```

This harness uses a lightweight `answer_match` scorer for SimpleQA. The official
SimpleQA evaluation uses an LLM grader with `CORRECT` / `INCORRECT` /
`NOT_ATTEMPTED`; add that later if you need official-score parity.
