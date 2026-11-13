#!/usr/bin/env python3
"""Minimal Codex app-server compatible adapter.

This adapter implements the subset of JSON-RPC methods Symphony expects:
- initialize
- thread/start
- turn/start

For each turn, it invokes the native tcode backend runner in a subprocess and emits either:
- turn/completed
- turn/failed

Environment variables:
- TCODE_ADAPTER_RUNNER: python script path for native tcode execution
- TCODE_ADAPTER_PYTHON: python binary for TCODE_ADAPTER_RUNNER (default: python3)
- TCODE_RUN_CWD: optional fixed cwd for tcode runner (overrides request cwd)
- TCODE_ADAPTER_LOG: optional file path to persist run logs
- TCODE_PROJECT_ROOT: optional working directory override for backend run cwd
- TCODE_ADAPTER_TIMEOUT_SECONDS: subprocess timeout in seconds (default: 600,
  maximum: 86400; legacy millisecond values are normalized)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from runtime_env import load_env_file, resolve_env_path
    from job_timeout import job_wait_timeout_seconds, local_wait_timeout_error
    from tracker_reporting import (
        TrackerReportError,
        extract_submit_input_from_prompt,
        report_execution,
    )
except ImportError:  # pragma: no cover - package-style test imports
    from scripts.runtime_env import load_env_file, resolve_env_path
    from scripts.job_timeout import job_wait_timeout_seconds, local_wait_timeout_error
    from scripts.tracker_reporting import (
        TrackerReportError,
        extract_submit_input_from_prompt,
        report_execution,
    )


class AdapterState:
    def __init__(self) -> None:
        self.thread_id = f"thread-{uuid.uuid4().hex[:12]}"
        self.current_cwd = os.getcwd()
        self.lock = threading.Lock()


STATE = AdapterState()
ACTIVE_THREADS: list[threading.Thread] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _log(text: str) -> None:
    log_path = os.environ.get("TCODE_ADAPTER_LOG", "").strip()
    if not log_path:
        return
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"[{_utc_now()}] {text}\n")


def _extract_prompt(params: dict[str, Any]) -> str:
    def _collect_from_content(content: Any) -> list[str]:
        parts: list[str] = []
        if isinstance(content, str):
            if content.strip():
                parts.append(content)
            return parts
        if not isinstance(content, list):
            return parts

        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return parts

    # Format A: params.input = [{"type":"text","text":"..."}, ...]
    blocks = params.get("input", [])
    if isinstance(blocks, list):
        parts: list[str] = []
        for item in blocks:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n\n".join(parts).strip()

    # Format B: params.prompt = "..."
    prompt_text = params.get("prompt")
    if isinstance(prompt_text, str) and prompt_text.strip():
        return prompt_text.strip()

    # Format C: params.messages = [{"role":"user","content":...}, ...]
    messages = params.get("messages")
    if isinstance(messages, list):
        msg_parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip().lower()
            if role and role not in {"user", "system", "developer"}:
                continue
            msg_parts.extend(_collect_from_content(msg.get("content")))
        if msg_parts:
            return "\n\n".join(msg_parts).strip()

    # Format D: params.content = "..." or list[content-part]
    content_parts = _collect_from_content(params.get("content"))
    if content_parts:
        return "\n\n".join(content_parts).strip()

    return ""


def _resolve_run_cwd(requested_cwd: str) -> str:
    forced_tcode_cwd = os.environ.get("TCODE_RUN_CWD", "").strip()
    if forced_tcode_cwd:
        return forced_tcode_cwd

    if requested_cwd:
        return requested_cwd
    forced_root = os.environ.get("TCODE_PROJECT_ROOT", "").strip()
    return forced_root or os.getcwd()


def _build_tcode_cmd(cwd: str) -> list[str]:
    python_bin = os.environ.get("TCODE_ADAPTER_PYTHON", "python3").strip() or "python3"
    configured_runner = os.environ.get("TCODE_ADAPTER_RUNNER", "").strip()
    if configured_runner:
        runner = configured_runner
    else:
        runner = str(Path(__file__).resolve().parent / "tcode_agent_runner.py")

    agent_name = os.environ.get("TCODE_ADAPTER_AGENT", "build").strip() or "build"
    return [python_bin, runner, "--cwd", cwd, "--agent", agent_name]


def _load_env_file_into(env: dict[str, str], env_file: Path) -> None:
    if not env_file.is_file():
        return
    try:
        load_env_file(env, env_file)
    except Exception as exc:  # pragma: no cover
        _log(f"failed_to_load_env_file path={env_file} error={exc}")


def _apply_route_policy_env(env: dict[str, str], cwd: str) -> None:
    # Prefer explicit file override.
    explicit = (env.get("TCODE_ENV_FILE") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(resolve_env_path(explicit, base=cwd))

    tcode_workdir = (env.get("TCODE_WORKDIR") or "").strip()
    if tcode_workdir:
        candidates.append(Path(tcode_workdir) / "config" / "symphony_route.env")

    project_root = (env.get("TCODE_PROJECT_ROOT") or "").strip()
    if project_root:
        candidates.append(Path(project_root) / "tcode" / "config" / "symphony_route.env")

    candidates.append(Path(cwd) / "config" / "symphony_route.env")

    for candidate in candidates:
        if candidate.is_file():
            _load_env_file_into(env, candidate)
            break

    # Keep job-protocol direct and block direct calls for non-whitelisted servers.
    env.setdefault("SYMPHONY_ENFORCE_DIRECT_WHITELIST", "true")
    env.setdefault("SYMPHONY_DIRECT_MCP_SERVERS", "job-protocol-mcp")
    env.setdefault("SYMPHONY_NEVER_ROUTE_MCP_SERVERS", "job-protocol-mcp")


def _extract_issue_identifier(title: str) -> str:
    if not title:
        return ""
    match = re.search(r"LOCAL-[A-Za-z0-9._-]+", title, flags=re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _extract_issue_identifier_from_prompt(prompt: str) -> str:
    # Prompt has a stable line: "- Issue: `LOCAL-...`".
    match = re.search(r"-\s*Issue:\s*`([^`]+)`", prompt)
    if match:
        return match.group(1).strip()
    fallback = re.search(r"LOCAL-[A-Za-z0-9._-]+", prompt, flags=re.IGNORECASE)
    return fallback.group(0).strip() if fallback else ""


def _failure_state() -> str:
    # Execution failures are not cancellations.  Keeping them distinct lets the
    # tracker preserve the adapter/runner error instead of reporting a fake user
    # cancellation.
    configured = os.environ.get("TCODE_ADAPTER_FAILURE_STATE", "").strip()
    return configured or "Failed"


def _adapter_timeout_seconds(job_input: dict[str, Any] | None = None) -> int | None:
    """Keep ordinary turn limits; formal jobs use the broker/runner deadline.

    Symphony's workflow timeout uses milliseconds, while this adapter option
    uses seconds.  Older deployments copied ``turn_timeout_ms`` directly into
    ``TCODE_ADAPTER_TIMEOUT_SECONDS``; values such as 72,000,000 then exceed
    Python's underlying poll timeout and fail immediately with
    ``timeout is too large``.  Normalize an obvious millisecond value and
    bound all other values to one day.
    """

    # The broker/runner owns the job deadline after submission. A second timer
    # starting at process launch would also charge input transfer/startup time.
    if job_input and job_input.get("target_server"):
        job_wait_timeout_seconds(job_input.get("timeout_seconds"))
        return None
    default_seconds = 600
    maximum_seconds = 24 * 60 * 60
    raw = str(os.environ.get("TCODE_ADAPTER_TIMEOUT_SECONDS", "") or "").strip()
    try:
        value = int(raw) if raw else default_seconds
    except (TypeError, ValueError):
        _log(f"invalid adapter timeout {raw!r}; using {default_seconds}s")
        return default_seconds
    if value <= 0:
        _log(f"non-positive adapter timeout {value}; using {default_seconds}s")
        return default_seconds
    if value > maximum_seconds:
        legacy_seconds = value // 1000
        if (
            value >= 1_000_000
            and value % 1000 == 0
            and 1 <= legacy_seconds <= maximum_seconds
        ):
            _log(
                f"normalized legacy millisecond adapter timeout "
                f"{value}ms to {legacy_seconds}s"
            )
            return legacy_seconds
        _log(f"adapter timeout {value}s exceeds limit; clamped to {maximum_seconds}s")
        return maximum_seconds
    return value


def _extract_job_input_from_prompt(prompt: str) -> dict[str, Any]:
    return extract_submit_input_from_prompt(prompt)


def _artifact_paths(job_input: dict[str, Any]) -> list[Path]:
    workdir = str(job_input.get("workdir", "")).strip()
    expected = job_input.get("expected_artifacts", [])
    if not workdir or not isinstance(expected, list):
        return []
    paths: list[Path] = []
    for item in expected:
        rel = str(item).strip()
        if rel:
            paths.append(Path(workdir) / rel)
    return paths


def _is_artifact_complete(job_input: dict[str, Any]) -> bool:
    paths = _artifact_paths(job_input)
    if not paths:
        return False
    return all(path.exists() for path in paths)


def _artifact_result_summary(job_input: dict[str, Any]) -> str:
    """Extract concise raw tool result from artifact JSON when available."""
    for path in _artifact_paths(job_input):
        if not path.exists() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        # Prefer structured content as canonical MCP output.
        structured = data.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False)

        # Fallback: text content from MCP envelope.
        content = data.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text.strip())
                elif isinstance(item, str) and item.strip():
                    text_parts.append(item.strip())
            if text_parts:
                return "\n".join(text_parts)

        if isinstance(content, (dict, str, int, float, bool)):
            return json.dumps(content, ensure_ascii=False)

        # Last fallback: compact whole artifact object.
        return json.dumps(data, ensure_ascii=False)

    return ""


def _compact_text(text: str, max_len: int = 1200) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + " ...[truncated]"


def _update_issue_state(
    issue_identifier: str,
    new_state: str,
    *,
    submit_input: dict[str, Any] | None = None,
    environment: dict[str, str] | None = None,
    turn_id: str = "",
    result_summary: str = "",
    error_summary: str = "",
    extra_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not issue_identifier:
        return {}
    normalized_state = str(new_state or "").strip().lower()
    status = {
        "in progress": "running",
        "running": "running",
        "done": "completed",
        "closed": "completed",
        "completed": "completed",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(normalized_state, "running")
    metadata_patch = dict(extra_updates or {})
    if turn_id:
        metadata_patch["last_turn_id"] = turn_id
    try:
        return report_execution(
            identifier=issue_identifier,
            status=status,
            submit_input=submit_input,
            result_summary=_compact_text(result_summary),
            error_summary=_compact_text(error_summary),
            metadata_patch=metadata_patch,
            source="symphony_adapter",
            environment=environment,
        )
    except TrackerReportError as exc:
        _log(
            f"failed_to_report_tracker identifier={issue_identifier!r} "
            f"state={new_state!r} error={exc}"
        )
        return {
            "_tracker_report_error": str(exc),
            "_tracker_report_status": int(getattr(exc, "status_code", 0) or 0),
        }


def _tracker_report_blocks_launch(report: dict[str, Any]) -> bool:
    status_code = int(report.get("_tracker_report_status") or 0)
    return 400 <= status_code < 500


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_turn(turn_id: str, prompt: str, cwd: str, issue_identifier: str) -> None:
    env = os.environ.copy()
    _apply_route_policy_env(env, cwd)
    # Prevent routing back to Symphony to avoid recursive loop.
    env.setdefault("SYMPHONY_ROUTE_ON_MCP_TOOL", "0")

    backend = "tcode"
    cmd = _build_tcode_cmd(cwd)
    job_input = _extract_job_input_from_prompt(prompt)
    try:
        timeout_seconds = _adapter_timeout_seconds(job_input)
    except ValueError as exc:
        error = str(exc)
        _update_issue_state(issue_identifier, _failure_state(), submit_input=job_input,
                            environment=env, turn_id=turn_id, error_summary=error)
        _send({"method": "turn/failed", "params": {"turnId": turn_id, "error": error}})
        return
    prompt_preview = prompt.replace("\n", "\\n")[:600]
    _log(
        f"run turn={turn_id} backend={backend} cwd={cwd} "
        f"prompt_len={len(prompt)} prompt_preview={prompt_preview!r} cmd={cmd!r}"
    )
    _log(
        "policy "
        f"route_on_mcp={env.get('SYMPHONY_ROUTE_ON_MCP_TOOL')} "
        f"enforce_direct_whitelist={env.get('SYMPHONY_ENFORCE_DIRECT_WHITELIST')} "
        f"direct_servers={env.get('SYMPHONY_DIRECT_MCP_SERVERS')} "
        f"never_route_servers={env.get('SYMPHONY_NEVER_ROUTE_MCP_SERVERS')}"
    )

    preflight = _update_issue_state(
        issue_identifier,
        "In Progress",
        submit_input=job_input,
        environment=env,
        turn_id=turn_id,
        extra_updates={"execution_preflight_at": _utc_now()},
    )
    if _tracker_report_blocks_launch(preflight):
        error = str(
            preflight.get("_tracker_report_error")
            or "workspace lifecycle rejected execution"
        )
        _send(
            {
                "method": "turn/failed",
                "params": {"turnId": turn_id, "error": error},
            }
        )
        return

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        started_report = _update_issue_state(
            issue_identifier,
            "In Progress",
            submit_input=job_input,
            environment=env,
            turn_id=turn_id,
            extra_updates={
                "execution_pid": proc.pid,
                "execution_started_at": _utc_now(),
            },
        )
        if _tracker_report_blocks_launch(started_report):
            _kill_process_group(proc)
            proc.communicate(timeout=5)
            error = str(
                started_report.get("_tracker_report_error")
                or "workspace lifecycle rejected execution"
            )
            _send(
                {
                    "method": "turn/failed",
                    "params": {"turnId": turn_id, "error": error},
                }
            )
            return
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            # Kill the entire process group so tcode descendants do not linger.
            _kill_process_group(proc)
        except Exception:
            pass
        _log(f"turn={turn_id} timeout={timeout_seconds}s")
        compute_wait = bool(job_input.get("target_server"))
        error = local_wait_timeout_error() if compute_wait else f"{backend} timeout after {timeout_seconds}s"
        if not compute_wait and _is_artifact_complete(job_input):
            _update_issue_state(
                issue_identifier,
                "Done",
                submit_input=job_input,
                environment=env,
                turn_id=turn_id,
                result_summary="Completed by artifact fallback after timeout",
                extra_updates={"execution_pid": None},
            )
            _send(
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": turn_id,
                        "ok": True,
                    },
                }
            )
        else:
            _update_issue_state(
                issue_identifier,
                _failure_state(),
                submit_input=job_input,
                environment=env,
                turn_id=turn_id,
                error_summary=error,
                extra_updates={"execution_pid": None},
            )
            _send(
                {
                    "method": "turn/failed",
                    "params": {
                        "turnId": turn_id,
                        "error": error,
                    },
                }
            )
        return
    except Exception as exc:  # pragma: no cover - runtime guard
        _log(f"turn={turn_id} launch_error={exc}")
        _update_issue_state(
            issue_identifier,
            _failure_state(),
            submit_input=job_input,
            environment=env,
            turn_id=turn_id,
            error_summary=f"adapter launch failed: {exc}",
            extra_updates={"execution_pid": None},
        )
        _send(
            {
                "method": "turn/failed",
                "params": {
                    "turnId": turn_id,
                    "error": f"adapter launch failed: {exc}",
                },
            }
        )
        return

    combined_output = (stdout or "") + (stderr or "")
    artifact_summary = _artifact_result_summary(job_input)
    _log(f"turn={turn_id} exit={proc.returncode} output={combined_output[:4000]!r}")

    if proc.returncode == 0:
        _update_issue_state(
            issue_identifier,
            "Done",
            submit_input=job_input,
            environment=env,
            turn_id=turn_id,
            result_summary=(artifact_summary or combined_output),
            extra_updates={"execution_pid": None},
        )
        _send(
            {
                "method": "turn/completed",
                "params": {
                    "turnId": turn_id,
                    "ok": True,
                },
            }
        )
    else:
        # Interpreter shutdown warnings may follow the runner's structured error.
        wait_error = ""
        final_error = ""
        if job_input.get("target_server"):
            for line in reversed((stderr or "").splitlines()):
                if line.strip().startswith("tcode runner failed:"):
                    final_error = line.strip()
                    break
                try:
                    detail = json.loads(line)
                except ValueError:
                    continue
                if isinstance(detail, dict) and detail.get("isError") is True:
                    final_error = line.strip()
                    if detail.get("failure_kind") == "local_wait_timeout":
                        wait_error = local_wait_timeout_error()
                    break
        if not final_error and _is_artifact_complete(job_input):
            _update_issue_state(
                issue_identifier,
                "Done",
                submit_input=job_input,
                environment=env,
                turn_id=turn_id,
                result_summary=(artifact_summary or "Completed by artifact fallback with non-zero process exit"),
                extra_updates={"execution_pid": None},
            )
            _send(
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": turn_id,
                        "ok": True,
                    },
                }
            )
        else:
            _update_issue_state(
                issue_identifier,
                _failure_state(),
                submit_input=job_input,
                environment=env,
                turn_id=turn_id,
                error_summary=(wait_error or final_error or combined_output or f"{backend} exit {proc.returncode}"),
                extra_updates={"execution_pid": None},
            )
            _send(
                {
                    "method": "turn/failed",
                    "params": {
                        "turnId": turn_id,
                        "error": wait_error or final_error or f"{backend} exit {proc.returncode}",
                    },
                }
            )


def _handle_request(req: dict[str, Any]) -> None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        _send(
            {
                "id": req_id,
                "result": {
                    "protocolVersion": "0.1.0",
                    "capabilities": {},
                    "serverInfo": {
                        "name": "tcode-codex-adapter",
                        "version": "0.1.0",
                    },
                },
            }
        )
        return

    if method == "initialized":
        return

    if method == "thread/start":
        with STATE.lock:
            STATE.thread_id = f"thread-{uuid.uuid4().hex[:12]}"
            cwd = params.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                STATE.current_cwd = cwd
        _send({"id": req_id, "result": {"thread": {"id": STATE.thread_id}}})
        return

    if method == "turn/start":
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        cwd = params.get("cwd") if isinstance(params, dict) else None
        if not isinstance(cwd, str) or not cwd.strip():
            cwd = STATE.current_cwd

        run_cwd = _resolve_run_cwd(cwd)
        prompt = _extract_prompt(params if isinstance(params, dict) else {})
        if not prompt:
            keys = sorted((params or {}).keys()) if isinstance(params, dict) else []
            _log(f"turn={turn_id} empty_prompt_extracted keys={keys!r}")
        issue_identifier = _extract_issue_identifier(str(params.get("title", ""))) if isinstance(params, dict) else ""
        if not issue_identifier:
            issue_identifier = _extract_issue_identifier_from_prompt(prompt)
        _send({"id": req_id, "result": {"turn": {"id": turn_id}}})

        thread = threading.Thread(target=_run_turn, args=(turn_id, prompt, run_cwd, issue_identifier), daemon=True)
        ACTIVE_THREADS.append(thread)
        thread.start()
        return

    # Unknown method: respond to request ids so caller won't hang.
    if req_id is not None:
        _send({"id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            _log(f"invalid_json={line[:500]!r}")
            continue

        if isinstance(payload, dict):
            _handle_request(payload)

    # Best effort: flush any in-flight turn before process exits.
    for thread in list(ACTIVE_THREADS):
        thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
