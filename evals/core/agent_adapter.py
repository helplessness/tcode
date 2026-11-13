from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.schema import AgentRunResult, EvalTask


def run_agent(
    task: EvalTask,
    *,
    adapter: str,
    agent: str,
    model: str,
    workdir: Path,
    command: str = "",
    timeout: int = 900,
) -> AgentRunResult:
    adapter_name = str(adapter or "fixture").strip().lower()
    if adapter_name == "fixture":
        return _run_fixture(task)
    if adapter_name == "tcode":
        return _run_tcode(task, agent=agent, model=model, workdir=workdir, timeout=timeout)
    if adapter_name == "command":
        return _run_command(task, command=command, timeout=timeout)
    raise ValueError(f"unknown adapter: {adapter}")


def _run_fixture(task: EvalTask) -> AgentRunResult:
    started = time.monotonic()
    output = str(task.raw.get("fixture_output") or "").strip()
    if not output:
        expected = task.expected
        answer = str(expected.get("answer") or "").strip()
        if answer:
            output = answer
        else:
            mutants = (
                expected.get("true_top_mutants")
                or expected.get("high_fitness_mutants")
                or []
            )
            variants = (
                expected.get("true_top_variants")
                or expected.get("top_k_variants")
                or []
            )
            if variants:
                output = (
                    '{"ranked_variants":'
                    + repr(list(variants)).replace("'", '"')
                    + ',"selection_logic":"fixture output","limitations":"no real model run"}'
                )
            else:
                output = (
                    '{"recommended_mutants":'
                    + repr(list(mutants)).replace("'", '"')
                    + ',"rationale":"fixture output","limitations":"no real model run"}'
                )
    tool_calls = list(task.raw.get("fixture_tool_calls") or [])
    usage = dict(task.raw.get("fixture_usage") or {})
    messages = list(task.raw.get("fixture_messages") or [])
    if not messages:
        messages = [
            {
                "role": "user",
                "content": task.render_prompt(),
                "tool_calls": [],
            },
            {
                "role": "assistant",
                "content": output,
                "tool_calls": tool_calls,
                "usage": usage or None,
            },
        ]
    duration_s = float(task.raw.get("fixture_duration_s") or 0.0)
    if duration_s <= 0:
        duration_s = time.monotonic() - started
    return AgentRunResult(
        final_output=output,
        tool_calls=tool_calls,
        subagent_calls=list(task.raw.get("fixture_subagent_calls") or []),
        usage=usage,
        artifacts=list(task.raw.get("fixture_artifacts") or []),
        messages=messages,
        duration_s=duration_s,
    )


def _run_tcode(
    task: EvalTask,
    *,
    agent: str,
    model: str,
    workdir: Path,
    timeout: int,
) -> AgentRunResult:
    runner = workdir / "scripts" / "tcode_agent_eval_runner.py"
    if not runner.exists():
        raise FileNotFoundError(f"missing tcode runner: {runner}")

    cmd = [
        sys.executable,
        str(runner),
        "--cwd",
        str(workdir),
        "--agent",
        str(agent or "build"),
    ]
    if model:
        cmd.extend(["--model", model])

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(workdir)
        if not existing_pythonpath
        else str(workdir) + os.pathsep + existing_pythonpath
    )

    proc = subprocess.run(
        cmd,
        input=task.render_prompt(),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    try:
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError("runner output must be a JSON object")
    except Exception as exc:
        return AgentRunResult(
            final_output="",
            stderr=proc.stderr.strip(),
            returncode=proc.returncode or 1,
            error=f"invalid structured tcode runner output: {exc}",
        )

    payload_error = str(payload.get("error") or "").strip()
    if proc.returncode != 0 and not payload_error:
        payload_error = proc.stderr.strip() or f"tcode eval runner exited with {proc.returncode}"
    return AgentRunResult(
        final_output=str(payload.get("final_output") or ""),
        tool_calls=_dict_items(payload.get("tool_calls")),
        subagent_calls=_dict_items(payload.get("subagent_calls")),
        usage=dict(payload.get("usage") or {})
        if isinstance(payload.get("usage"), dict)
        else {},
        artifacts=[str(item) for item in (payload.get("artifacts") or [])],
        messages=_dict_items(payload.get("messages")),
        duration_s=_nonnegative_float(payload.get("duration_s")),
        stderr=proc.stderr.strip(),
        returncode=proc.returncode,
        error=payload_error,
    )


def _run_command(task: EvalTask, *, command: str, timeout: int) -> AgentRunResult:
    if not command:
        raise ValueError("--command is required when --adapter command")
    proc = subprocess.run(
        command,
        input=task.render_prompt(),
        text=True,
        capture_output=True,
        timeout=timeout,
        shell=True,
    )
    return AgentRunResult(
        final_output=proc.stdout.strip(),
        tool_calls=_extract_tool_calls(proc.stderr),
        subagent_calls=_extract_subagent_calls(proc.stdout + "\n" + proc.stderr),
        stderr=proc.stderr.strip(),
        returncode=proc.returncode,
        error="" if proc.returncode == 0 else proc.stderr.strip(),
    )


def _extract_tool_calls(stderr: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for line in str(stderr or "").splitlines():
        match = re.search(r"工具[:：]\s*([A-Za-z0-9_.:/@-]+)", line)
        if not match:
            match = re.search(r"tool[:：]\s*([A-Za-z0-9_.:/@-]+)", line, re.IGNORECASE)
        if match:
            calls.append({"tool_name": match.group(1), "arguments": {}})
    return calls


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _nonnegative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _extract_subagent_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    patterns = [
        r"subagent[:：]\s*([A-Za-z0-9_.:/@-]+)",
        r"子代理[:：]\s*([A-Za-z0-9_.:/@-]+)",
        r"agentId[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:/@-]+)",
        r"assign_task\([^)]*agent[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:/@-]+)",
        r"sessions_spawn\([^)]*agentId[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:/@-]+)",
    ]
    seen: set[str] = set()
    for line in str(text or "").splitlines():
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            name = match.group(1).strip().strip("\"'")
            if not name:
                continue
            key = f"{name}::{line.strip()}"
            if key in seen:
                continue
            seen.add(key)
            calls.append({"agent": name, "raw": line.strip()})
    return calls
