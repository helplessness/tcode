#!/usr/bin/env python3
"""Run a single tcode agent turn from stdin prompt.

This script is intended to be called by Symphony codex adapter.
It executes tcode's native Agent with configured MCP/tools and prints
only the final assistant content to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from tracker_reporting import TrackerReportError, report_execution
    from job_timeout import job_wait_timeout_seconds, local_wait_timeout_error
except ImportError:  # pragma: no cover - package-style test imports
    from scripts.tracker_reporting import TrackerReportError, report_execution
    from scripts.job_timeout import job_wait_timeout_seconds, local_wait_timeout_error


def _bootstrap_path() -> None:
    script_dir = Path(__file__).resolve().parent
    tcode_root = script_dir.parent
    if str(tcode_root) not in sys.path:
        sys.path.insert(0, str(tcode_root))


def _extract_submit_input_from_prompt(prompt: str) -> dict:
    marker = "submit_job.input:"
    idx = prompt.find(marker)
    if idx < 0:
        return {}
    text = prompt[idx + len(marker) :]
    start = text.find("{")
    if start < 0:
        return {}
    text = text[start:]
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return {}
    return {}


def _extract_issue_identifier(prompt: str) -> str:
    match = re.search(r"LOCAL-[A-Za-z0-9._-]+", str(prompt or ""), re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _extract_artifact_raw_result(artifact_paths: list[str]) -> str:
    for p in artifact_paths:
        path = Path(p)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if isinstance(data, dict):
            structured = data.get("structuredContent")
            if isinstance(structured, (dict, list)):
                return json.dumps(structured, ensure_ascii=False)
            content = data.get("content")
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                    elif isinstance(item, str) and item.strip():
                        parts.append(item.strip())
                if parts:
                    return "\n".join(parts)
            return json.dumps(data, ensure_ascii=False)
    return ""


def _should_use_job_protocol_direct(prompt: str) -> bool:
    flag = os.environ.get("TCODE_RUNNER_JOB_PROTOCOL_DIRECT", "1").strip().lower()
    enabled = flag in {"1", "true", "yes", "on"}
    if not enabled:
        return False
    return "submit_job.input:" in prompt


def _unwrap_mcp_response(raw: Any) -> dict:
    """MCPManager.call_tool returns the JSON-RPC result which is {content, structuredContent, isError}.
    Unwrap to the actual data dict, preferring structuredContent."""
    if not isinstance(raw, dict):
        return {}
    # Already unwrapped (no MCP envelope keys)
    if "structuredContent" not in raw and "content" not in raw and "isError" not in raw:
        return raw
    structured = raw.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = raw.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item["text"])
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
    return raw


def _inject_pythonpath_if_needed(submit_input: dict) -> dict:
    """If command calls mcp_tool_job_runner.py but lacks PYTHONPATH, prepend it.
    This fixes the case where job commands are hand-crafted (e.g. via curl tests)
    without setting PYTHONPATH, causing ModuleNotFoundError for tcode_agent."""
    cmd = submit_input.get("command", "")
    if not isinstance(cmd, str):
        return submit_input
    if "mcp_tool_job_runner" not in cmd:
        return submit_input
    if "PYTHONPATH" in cmd:
        return submit_input
    # Find the tcode root: script is at <tcode_root>/scripts/mcp_tool_job_runner.py
    import re as _re
    m = _re.search(r"(\S+)/scripts/mcp_tool_job_runner\.py", cmd)
    if not m:
        return submit_input
    tcode_root = m.group(1)
    # Use `export PYTHONPATH=...; cmd` so the variable is visible to ALL sub-commands,
    # including those after `&&` (e.g. `mkdir -p ... && python3 runner.py ...`).
    # Using `VAR=value cmd1 && cmd2` would only set VAR for cmd1, not cmd2.
    patched = {**submit_input, "command": f"export PYTHONPATH={tcode_root}:${{PYTHONPATH:-}}; {cmd}"}
    return patched


async def _run_job_protocol_direct(cwd: str, prompt: str) -> str:
    from tcode_agent.mcp.manager_auto import MCPManager

    submit_input = _extract_submit_input_from_prompt(prompt)
    if not submit_input:
        if "submit_job.input:" in prompt:
            raise RuntimeError(
                "submit_job.input found in prompt but JSON parse failed "
                "(check escaping in 'command' field — nested quotes must use '\\\"' or single-quoted shell args)"
            )
        raise RuntimeError("submit_job.input not found in prompt")

    # Validate before submission, so accepted zero spellings cannot fail later.
    timeout_s = job_wait_timeout_seconds(submit_input.get("timeout_seconds"))
    submit_input = _inject_pythonpath_if_needed(submit_input)
    identifier = _extract_issue_identifier(prompt)
    if identifier:
        try:
            await asyncio.to_thread(
                report_execution,
                identifier=identifier,
                status="running",
                submit_input=submit_input,
                metadata_patch={"runner_submit_preflight_at": time.time()},
                source="symphony_runner",
            )
        except TrackerReportError as exc:
            status_code = int(getattr(exc, "status_code", 0) or 0)
            if 400 <= status_code < 500:
                raise RuntimeError(
                    f"workspace lifecycle rejected job submission: {exc}"
                ) from exc

    if not MCPManager.is_initialized():
        await MCPManager.initialize(workdir=cwd)

    try:
        submit_raw = await MCPManager.call_tool(
            "job-protocol-mcp", "submit_job", {"input": submit_input}
        )
        submit = _unwrap_mcp_response(submit_raw)
        job_id = str(submit.get("job_id", "")).strip()
        process_id = str(submit.get("process_id", "")).strip()
        if not job_id or not process_id:
            raise RuntimeError(f"invalid submit response: {submit_raw}")
        request_scope = {
            key: submit_input[key]
            for key in (
                "issuer",
                "agent_id",
                "subject",
                "tenant_id",
                "user_id",
                "workspace_id",
                "capabilities",
                "workspace_grant",
            )
            if submit_input.get(key) not in (None, "", [])
        }
        request_scope["job_token"] = str(submit.get("job_token", "") or "")
        if identifier:
            job_ref = {
                "job_id": job_id,
                "process_id": process_id,
                "run_id": str(submit_input.get("run_id", "") or ""),
                "workdir": str(submit_input.get("workdir", "") or ""),
                "job_token": str(submit.get("job_token", "") or ""),
                "request_scope": {
                    key: value
                    for key, value in request_scope.items()
                    if key != "job_token"
                },
            }
            try:
                await asyncio.to_thread(
                    report_execution,
                    identifier=identifier,
                    status="running",
                    submit_input=submit_input,
                    job_ref=job_ref,
                    workspace_patch={
                        "job_id": job_id,
                        "process_id": process_id,
                        "submitted_at": str(submit.get("submitted_at", "") or ""),
                    },
                    source="symphony_runner",
                )
            except TrackerReportError:
                # The adapter reports terminal state as a second reconciliation
                # path. Do not abandon a successfully submitted compute job only
                # because the control-plane callback was briefly unavailable.
                pass

        deadline = time.time() + timeout_s + 60 if timeout_s > 0 else None
        terminal = {
            "completed",
            "failed",
            "timed_out",
            "terminated",
            "cancelled",
            "canceled",
            "error",
            "done",
        }
        final_status = None

        while deadline is None or time.time() < deadline:
            status_raw = await MCPManager.call_tool(
                "job-protocol-mcp",
                "get_job_status",
                {
                    "input": {
                        "job_id": job_id,
                        "process_id": process_id,
                        "tail_lines": 200,
                        **request_scope,
                    }
                },
            )
            status = _unwrap_mcp_response(status_raw)
            final_status = str(status.get("status", "")).strip().lower()
            if final_status in terminal:
                break
            await asyncio.sleep(2)

        if final_status not in terminal:
            raise TimeoutError("Job Protocol status wait expired")

        try:
            collect_raw = await MCPManager.call_tool(
                "job-protocol-mcp",
                "collect_job_result",
                {
                    "input": {
                        "job_id": job_id,
                        "artifact_paths": submit_input.get("expected_artifacts", []),
                        "validation_rules": {"must_exist": True, "non_empty": True},
                        **request_scope,
                    }
                },
            )
            collect = _unwrap_mcp_response(collect_raw)
        except Exception as exc:
            if final_status != "timed_out":
                raise
            print(f"Diagnostic collection failed: {exc}", file=sys.stderr)
            raise TimeoutError("Job Protocol reported local wait timeout") from exc

        # Collect the original diagnostics first, but do not turn the broker's
        # local waiting deadline into a successful adapter exit.
        if final_status == "timed_out":
            raise TimeoutError("Job Protocol reported local wait timeout")

        artifacts = collect.get("artifacts") if isinstance(collect, dict) else []
        if isinstance(artifacts, list):
            raw = _extract_artifact_raw_result([str(x) for x in artifacts])
            if raw:
                return raw

        return json.dumps(collect, ensure_ascii=False)
    finally:
        await MCPManager.close()


async def _run_once(
    cwd: str,
    agent_name: str,
    prompt: str,
    model_ref: str = "",
) -> str:
    if _should_use_job_protocol_direct(prompt):
        try:
            return await _run_job_protocol_direct(cwd=cwd, prompt=prompt)
        except RuntimeError as e:
            # If JSON parse failed (malformed command field), fall through to LLM agent
            # which can call submit_job/get_job_status/collect_job_result itself.
            if "JSON parse failed" not in str(e) and "not found in prompt" not in str(e):
                raise
            # else: fall through to LLM below

    from tcode_agent.agent.agent import Agent
    from tcode_agent.agent.agents import BuiltinAgents
    from tcode_agent.config.manager import ConfigManager
    from tcode_agent.mcp.manager_auto import MCPManager
    from tcode_agent.providers import create_provider

    ConfigManager.clear()
    ConfigManager.load(cwd)

    provider = create_provider(
        workdir=cwd,
        model_ref=str(model_ref or "").strip() or None,
    )

    agent_info = BuiltinAgents.get(agent_name) or BuiltinAgents.BUILD
    agent_cfg = ConfigManager.get_agent(agent_name)
    agent_info = BuiltinAgents.apply_config(agent_info, agent_cfg)

    agent = Agent(
        agent_info=agent_info,
        provider=provider,
        session_id=f"symphony-{uuid.uuid4().hex[:10]}",
        workdir=cwd,
    )

    if not MCPManager.is_initialized():
        await MCPManager.initialize(workdir=cwd)

    try:
        result = await agent.run(prompt)
        return (result.content or "").strip()
    finally:
        await MCPManager.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True, help="Working directory for tcode agent")
    parser.add_argument("--agent", default="build", help="Builtin agent name")
    parser.add_argument(
        "--model",
        default="",
        help="Optional model override in provider/model format, e.g. alibaba-cn/kimi-k2.5",
    )
    parser.add_argument("--prompt", default="", help="Prompt fallback when stdin is empty")
    args = parser.parse_args()

    _bootstrap_path()

    cwd = os.path.abspath(args.cwd)
    # Prefer explicit --prompt for interactive CLI usage.
    # Symphony adapter does not pass --prompt, so it still uses stdin.
    if args.prompt.strip():
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("empty prompt", file=sys.stderr)
        return 2

    capture_out = io.StringIO()
    capture_err = io.StringIO()

    try:
        with contextlib.redirect_stdout(capture_out), contextlib.redirect_stderr(capture_err):
            answer = asyncio.run(
                _run_once(
                    cwd=cwd,
                    agent_name=args.agent,
                    prompt=prompt,
                    model_ref=args.model,
                )
            )
    except Exception as exc:
        noisy = capture_out.getvalue().strip()
        noisy_err = capture_err.getvalue().strip()
        if noisy:
            print(noisy, file=sys.stderr)
        if noisy_err:
            print(noisy_err, file=sys.stderr)
        if isinstance(exc, TimeoutError) and _should_use_job_protocol_direct(prompt):
            print(local_wait_timeout_error(), file=sys.stderr)
        else:
            print(f"tcode runner failed: {exc}", file=sys.stderr)
        return 1

    noisy = capture_out.getvalue().strip()
    noisy_err = capture_err.getvalue().strip()
    if noisy:
        print(noisy, file=sys.stderr)
    if noisy_err:
        print(noisy_err, file=sys.stderr)

    sys.stdout.write(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
