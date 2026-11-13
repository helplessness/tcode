#!/usr/bin/env python3
"""Claim bridge tasks from Shared Tracker and execute them via job-protocol-mcp."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

try:
    from runtime_env import load_process_env_file, resolve_env_path
except ImportError:  # pragma: no cover - package-style test imports
    from scripts.runtime_env import load_process_env_file, resolve_env_path


def _bootstrap_path() -> Path:
    script_dir = Path(__file__).resolve().parent
    tcode_root = script_dir.parent
    if str(tcode_root) not in sys.path:
        sys.path.insert(0, str(tcode_root))
    return tcode_root


TCODE_ROOT = _bootstrap_path()


def _load_runtime_env_file() -> None:
    env_file = str(
        os.environ.get("TCODE_ENV_FILE", "") or TCODE_ROOT / "config" / "symphony_route.env"
    ).strip()
    path = resolve_env_path(env_file, base=TCODE_ROOT)
    if not path.is_file():
        return
    try:
        load_process_env_file(path)
    except Exception:
        pass


_load_runtime_env_file()


try:
    from tracker_scope import (
        _tracker_base,
        _tracker_scopes,
        _tracker_headers,
        _tracker_scope_poll_suspended,
        _suspend_tracker_scope,
    )
except ImportError:  # package-style test imports
    from scripts.tracker_scope import (
        _tracker_base,
        _tracker_scopes,
        _tracker_headers,
        _tracker_scope_poll_suspended,
        _suspend_tracker_scope,
    )

from tcode_agent.config.manager import ConfigManager  # noqa: E402
from tcode_agent.mcp.manager_auto import MCPManager  # noqa: E402
from tcode_agent.runtime.tool_progress import (  # noqa: E402
    coerce_tool_progress_event,
    parse_tool_progress_line,
)


_PROCESS_WORKER_ID = f"bridge-{uuid.uuid4().hex[:8]}"


def _worker_id() -> str:
    return str(os.environ.get("TCODE_BRIDGE_WORKER_ID", "") or "").strip() or _PROCESS_WORKER_ID


def _allowed_servers() -> list[str]:
    """Use enabled execution configs; route/worker lists can only narrow them."""
    from tcode_agent.mcp.executor_config import executor_config_candidates, broker_servers

    configured: dict[str, dict] = {}
    for path in executor_config_candidates(workdir=os.environ.get("TCODE_WORKDIR"), include_foreground=False):
        try:
            values = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(values, dict):
            for name, entry in values.items():
                if isinstance(entry, dict):
                    configured.setdefault(name, entry)
    allowed = {name for name, entry in configured.items()
               if not name.startswith("$") and name not in broker_servers()
               and entry.get("enabled", True) is not False
               and entry.get("invocation_mode") != "direct"}
    for key in ("SYMPHONY_ROUTED_MCP_SERVERS", "TCODE_BRIDGE_ALLOWED_SERVERS"):
        restriction = {item.strip() for item in str(os.environ.get(key) or "").split(",") if item.strip()}
        if restriction:
            allowed.intersection_update(restriction)
    return sorted(allowed)


def _poll_interval() -> float:
    try:
        return max(1.0, float(os.environ.get("TCODE_BRIDGE_POLL_SECONDS", "2")))
    except Exception:
        return 2.0


def _lease_seconds() -> int:
    try:
        return max(15, int(os.environ.get("TCODE_BRIDGE_LEASE_SECONDS", "30")))
    except Exception:
        return 30


def _config_has_job_protocol(path_like: str) -> bool:
    text = str(path_like or "").strip()
    if not text:
        return False
    path = Path(text)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("job-protocol-mcp"), dict)


def _resolve_bridge_mcp_config() -> str:
    explicit = str(os.environ.get("TCODE_BRIDGE_MCP_CONFIG", "") or "").strip()
    if explicit and _config_has_job_protocol(explicit):
        return explicit

    current = str(os.environ.get("TCODE_MCP_CONFIG", "") or "").strip()
    if current and _config_has_job_protocol(current):
        return current

    configured_root = Path(os.environ.get("TCODE_CONFIG_ROOT") or TCODE_ROOT / ".tcode-local")
    preferred = configured_root / "config" / "mcp_job_protocol_only.json"
    if _config_has_job_protocol(preferred.as_posix()):
        return preferred.as_posix()

    fallback = Path(
        ConfigManager.get_config_root_file_path(
            "mcp.json",
            os.environ.get("TCODE_WORKDIR") or TCODE_ROOT.as_posix(),
            prefer_effective=True,
        )
    )
    if _config_has_job_protocol(fallback.as_posix()):
        return fallback.as_posix()

    return current or preferred.as_posix()


def _unwrap_mcp_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
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
                    parsed = json.loads(str(item.get("text") or ""))
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return raw


def _stringify_summary(payload: Any) -> str:
    if isinstance(payload, dict):
        structured = payload.get("structuredContent")
        if structured is not None:
            payload = structured
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        text = str(payload or "")
    return text[:4000]


def _extract_tool_progress(payload: Any) -> Optional[dict[str, Any]]:
    """Read progress from common TMCP fields or captured job output."""

    mappings: list[dict[str, Any]] = []
    text_candidates: list[str] = []

    def _walk(value: Any, *, depth: int = 0, key: str = "") -> None:
        if depth > 4:
            return
        if isinstance(value, dict):
            mappings.append(value)
            for child_key, child in value.items():
                _walk(child, depth=depth + 1, key=str(child_key or "").lower())
            return
        if isinstance(value, list):
            for child in value[-200:]:
                _walk(child, depth=depth + 1, key=key)
            return
        if isinstance(value, str) and key in {
            "stdout",
            "stderr",
            "output",
            "log",
            "logs",
            "tail",
            "stdout_tail",
            "stderr_tail",
            "output_tail",
            "message",
        }:
            text_candidates.append(value[-100_000:])

    _walk(payload)

    for mapping in mappings:
        nested = mapping.get("tool_progress")
        if isinstance(nested, dict):
            event = coerce_tool_progress_event(nested)
            if event is not None:
                return event.to_dict()

        progress_value = mapping.get("progress")
        if isinstance(progress_value, dict):
            event = coerce_tool_progress_event(
                {
                    "message": progress_value.get("message")
                    or progress_value.get("text")
                    or mapping.get("message"),
                    "current": progress_value.get("current")
                    if progress_value.get("current") is not None
                    else progress_value.get("progress"),
                    "total": progress_value.get("total"),
                    "unit": progress_value.get("unit"),
                    "seq": progress_value.get("seq"),
                    "stream": "mcp",
                    "status": progress_value.get("status") or "running",
                }
            )
            if event is not None:
                return event.to_dict()

        current = mapping.get("current")
        if current is None and isinstance(progress_value, (int, float)):
            current = progress_value
        total = mapping.get("total")
        if current is not None:
            event = coerce_tool_progress_event(
                {
                    "message": mapping.get("progress_message") or mapping.get("message"),
                    "current": current,
                    "total": total,
                    "unit": mapping.get("unit"),
                    "seq": mapping.get("seq"),
                    "stream": "mcp",
                    "status": "running",
                }
            )
            if event is not None:
                return event.to_dict()

    for text in reversed(text_candidates):
        for line in reversed(text.splitlines()):
            event = parse_tool_progress_line(line, stream="mcp")
            if event is not None:
                return event.to_dict()
    return None


async def _tracker_post(
    session: aiohttp.ClientSession,
    endpoint: str,
    payload: dict[str, Any],
    scope: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    async with session.post(
        f"{_tracker_base()}{endpoint}",
        headers=_tracker_headers(scope),
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 400:
            raise RuntimeError(str((data or {}).get("detail") or data or f"tracker request failed: {resp.status}"))
        return data if isinstance(data, dict) else {}


async def _tracker_get(
    session: aiohttp.ClientSession,
    endpoint: str,
    scope: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    async with session.get(
        f"{_tracker_base()}{endpoint}",
        headers=_tracker_headers(scope),
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json(content_type=None)
        if resp.status >= 400:
            raise RuntimeError(str((data or {}).get("detail") or data or f"tracker request failed: {resp.status}"))
        return data if isinstance(data, dict) else {}


async def _claim_next(session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
    allowed = _allowed_servers()
    # Plugin targets live in the local broker, not the static executor config.
    # The management credential is host-only; this does not expose it to agents.
    if os.environ.get('TCODE_PLUGIN_ASYNC_ENABLED', '1') != '0':
        try:
            from tcode_agent.plugins.async_targets import gateway
            catalog = await asyncio.wait_for(gateway('GET', '/api/v1/target-registrations'), timeout=2)
            allowed = sorted(set(allowed) | {name for name in catalog.get('servers', []) if name.startswith('plugin-')})
        except Exception:
            pass  # Built-in tasks still work when dynamic registration is unavailable.
    if not allowed:
        return None
    for scope in _tracker_scopes():
        if _tracker_scope_poll_suspended(scope):
            continue
        data = await _tracker_post(
            session,
            "/api/tracker/claim-next",
            {
                "worker_id": _worker_id(),
                "channel": "bridge",
                "lease_seconds": _lease_seconds(),
                "allowed_servers": allowed,
            },
            scope,
        )
        if data.get("scope_available") is False:
            _suspend_tracker_scope(scope, data.get("retry_after_seconds"))
            continue
        task = data.get("task")
        if isinstance(task, dict) and task:
            task["_tracker_scope"] = scope
            return task
    return None


async def _get_task(session: aiohttp.ClientSession, task_id: str, scope: Optional[dict[str, str]] = None) -> dict[str, Any]:
    data = await _tracker_get(session, f"/api/tracker/tasks/{task_id}", scope)
    return data.get("task") if isinstance(data.get("task"), dict) else {}


async def _heartbeat(
    session: aiohttp.ClientSession,
    task_id: str,
    claim_token: str,
    *,
    status: str = "running",
    workspace_patch: Optional[dict[str, Any]] = None,
    result_summary: str = "",
    error_summary: str = "",
    scope: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    data = await _tracker_post(
        session,
        f"/api/tracker/tasks/{task_id}/heartbeat",
        {
            "claim_token": claim_token,
            "worker_id": _worker_id(),
            "lease_seconds": _lease_seconds(),
            "status": status,
            "workspace_patch": workspace_patch or {},
            "result_summary": result_summary,
            "error_summary": error_summary,
        },
        scope,
    )
    return data.get("task") if isinstance(data.get("task"), dict) else {}


async def _attach_job_ref(
    session: aiohttp.ClientSession,
    task_id: str,
    claim_token: str,
    job_ref: dict[str, Any],
    workspace_patch: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    data = await _tracker_post(
        session,
        f"/api/tracker/tasks/{task_id}/attach-job-ref",
        {
            "claim_token": claim_token,
            "worker_id": _worker_id(),
            "job_ref": job_ref,
            "workspace_patch": workspace_patch or {},
        },
        scope,
    )
    return data.get("task") if isinstance(data.get("task"), dict) else {}


async def _complete(
    session: aiohttp.ClientSession,
    task_id: str,
    claim_token: str,
    *,
    result_summary: str,
    workspace_patch: Optional[dict[str, Any]] = None,
    job_ref: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, str]] = None,
) -> None:
    await _tracker_post(
        session,
        f"/api/tracker/tasks/{task_id}/complete",
        {
            "claim_token": claim_token,
            "result_summary": result_summary,
            "workspace_patch": workspace_patch or {},
            "job_ref": job_ref or {},
        },
        scope,
    )


async def _fail(
    session: aiohttp.ClientSession,
    task_id: str,
    claim_token: str,
    *,
    error_summary: str,
    workspace_patch: Optional[dict[str, Any]] = None,
    job_ref: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, str]] = None,
) -> None:
    await _tracker_post(
        session,
        f"/api/tracker/tasks/{task_id}/fail",
        {
            "claim_token": claim_token,
            "error_summary": error_summary,
            "workspace_patch": workspace_patch or {},
            "job_ref": job_ref or {},
        },
        scope,
    )


async def _cancel(
    session: aiohttp.ClientSession,
    task_id: str,
    claim_token: str,
    *,
    result_summary: str,
    workspace_patch: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, str]] = None,
) -> None:
    await _tracker_post(
        session,
        f"/api/tracker/tasks/{task_id}/cancel",
        {
            "claim_token": claim_token,
            "result_summary": result_summary,
            "workspace_patch": workspace_patch or {},
            "source": "bridge_worker",
        },
        scope,
    )


async def _init_mcp(cwd: str) -> None:
    os.environ["TCODE_MCP_CONFIG"] = _resolve_bridge_mcp_config()
    ConfigManager.clear()
    ConfigManager.load(cwd)
    if not MCPManager.is_initialized():
        await MCPManager.initialize(workdir=cwd)
    # Only protocol ping/handshake may be repeated, before submit_started is set.
    # A failure during submit_job still follows submission_unknown, never replay.
    await MCPManager.ensure_session("job-protocol-mcp")


def _job_request_scope(submit_input: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "issuer",
        "agent_id",
        "subject",
        "tenant_id",
        "user_id",
        "workspace_id",
        "capabilities",
        "workspace_grant",
    )
    return {
        key: submit_input[key]
        for key in fields
        if submit_input.get(key) not in (None, "", [])
    }


async def _terminate_job(job_ref: dict[str, Any]) -> None:
    try:
        await MCPManager.call_tool(
            "job-protocol-mcp",
            "terminate_job",
            {
                "input": {
                    "job_id": str(job_ref.get("job_id", "") or ""),
                    "process_id": str(job_ref.get("process_id", "") or ""),
                    "job_token": str(job_ref.get("job_token", "") or ""),
                    **dict(job_ref.get("request_scope") or {}),
                }
            },
        )
    except Exception:
        pass


async def _run_task(session: aiohttp.ClientSession, task: dict[str, Any]) -> None:
    task_id = str(task.get("task_id", "") or task.get("id", "") or "").strip()
    claim_token = str(task.get("claim_token", "") or "").strip()
    scope = task.get("_tracker_scope") if isinstance(task.get("_tracker_scope"), dict) else None
    if not task_id or not claim_token:
        return

    if (task.get("job_ref") or {}).get("job_id") or (task.get("workspace") or {}).get("job_id"):
        # An existing remote execution is reconciled by the backend, never submitted again.
        return

    submit_input = task.get("submit_input")
    if not isinstance(submit_input, dict) or not submit_input:
        await _fail(
            session,
            task_id,
            claim_token,
            error_summary="tracker task is missing submit_input",
            scope=scope,
        )
        return

    cwd = str(os.environ.get("TCODE_WORKDIR", TCODE_ROOT.as_posix()) or TCODE_ROOT.as_posix())
    job_ref: dict[str, Any] = {}
    workspace_patch: dict[str, Any] = {"execution_channel": "bridge"}
    submit_started = False
    execution_completed = False
    async def renew_lease() -> None:
        while True:
            await asyncio.sleep(max(1, _lease_seconds() / 3))
            await _heartbeat(session, task_id, claim_token, status="running", scope=scope)

    lease_task = asyncio.create_task(renew_lease())

    try:
        await asyncio.wait_for(_init_mcp(cwd), timeout=30)
        statuses = await MCPManager.get_status()
        status = statuses.get("job-protocol-mcp")
        if status is None or status.status != "connected":
            raise RuntimeError("Job Protocol 未连接，尚未发送计算请求")

        # Reuse the same boundary for queued inputs created before review/upload fixes.
        from tcode_agent.mcp.artifact_boundary import prepare_mcp_artifact_arguments
        from tcode_agent.tools.base import ToolContext
        workspace = task.get("workspace") or {}
        control = str(workspace.get("protein_run", {}).get("control_workdir") or cwd)
        execution = str(workspace.get("protein_run", {}).get("run_root") or control)
        context = ToolContext(
            session_id=str(task.get("session_id") or ""), message_id=task_id,
            agent="bridge", abort=None,
            extra={"workdir": control, "workspace_root": execution,
                   "agent_workspace": {"control_workdir": control, "execution_workdir": execution},
                   "workspace_id": submit_input.get("workspace_id") or "local"},
        )
        arguments = dict(submit_input.get('arguments') or {})
        if not str(submit_input.get('target_server') or '').startswith('plugin-'):
            from tcode_agent.workspace.async_protein_run import saved_submission_bindings, remap_submission_arguments
            run_link = workspace.get("protein_run")
            bindings = saved_submission_bindings(run_link) if isinstance(run_link, dict) and run_link else None
            if bindings is not None:
                arguments = remap_submission_arguments(run_link, arguments)
            legacy_declarations = False
            if bindings is None:
                from tcode_agent.workspace.input_resources import pin_input_file_contract
                if "input_file_contract" in workspace:
                    pin_input_file_contract(context, str(submit_input.get("target_server") or ""),
                                            str(submit_input.get("target_tool") or ""), workspace["input_file_contract"])
                else:
                    # Historical records without a Run retain their old parser;
                    # never fetch today's declaration to reinterpret their inputs.
                    legacy_declarations = True
            arguments, _ = await prepare_mcp_artifact_arguments(
                str(submit_input.get("target_server") or ""),
                str(submit_input.get("target_tool") or ""),
                arguments, context, saved_bindings=bindings, legacy_declarations=legacy_declarations,
            )
        submit_input = {**submit_input, "arguments": arguments}
        # Pilot the final wire check on the strict demo contract, without
        # changing Protein Run bindings or other targets' preflight behavior.
        if submit_input.get("target_server") == "simple-demo-mcp" and submit_input.get("target_tool") in {"echo_text", "sleep_then_echo"}:
            from tcode_agent.mcp._proxy import _validate_target_args_contract

            invalid = await _validate_target_args_contract(
                target_server=submit_input["target_server"],
                target_tool=submit_input["target_tool"],
                tool_args=arguments,
                context=context,
            )
            if invalid is not None:
                raise ValueError(invalid.error)
        submit_started = True
        submit_raw = await MCPManager.call_tool(
            "job-protocol-mcp", "submit_job", {"input": submit_input}
        )
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        if isinstance(submit_raw, dict) and submit_raw.get("isError"):
            raise RuntimeError("Job Protocol 返回工具错误，未取得远端任务引用")
        submit_data = _unwrap_mcp_response(submit_raw)
        job_id = str(submit_data.get("job_id", "") or "").strip()
        process_id = str(submit_data.get("process_id", "") or "").strip()
        if not job_id or not process_id:
            raise RuntimeError(f"invalid submit_job response: {_stringify_summary(submit_raw)}")

        job_ref = {
            "job_id": job_id,
            "process_id": process_id,
            "run_id": str(submit_input.get("run_id", "") or ""),
            "workdir": str(submit_input.get("workdir", "") or ""),
            "context_file": str((submit_input.get("workspace") or {}).get("context_file", "") or ""),
            "job_token": str(submit_data.get("job_token", "") or ""),
            "request_scope": _job_request_scope(submit_input),
        }
        workspace_patch.update(
            {
                "job_id": job_id,
                "process_id": process_id,
                "submitted_at": str(submit_data.get("submitted_at", "") or ""),
                "started_at": str(submit_data.get("submitted_at", "") or ""),
            }
        )
        await _attach_job_ref(session, task_id, claim_token, job_ref, workspace_patch, scope=scope)

        terminal_states = {
            "completed",
            "failed",
            "timed_out",
            "terminated",
            "cancelled",
            "canceled",
            "error",
            "done",
        }
        final_status_payload: dict[str, Any] = {}
        last_progress_signature = ""

        while True:
            latest = await _get_task(session, task_id, scope)
            if str(latest.get("tracker_status", "") or "").strip().lower() == "cancelled":
                await _terminate_job(job_ref)
                await _cancel(
                    session,
                    task_id,
                    claim_token,
                    result_summary="bridge worker observed tracker cancellation and terminated the job",
                    workspace_patch={"cancelled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                    scope=scope,
                )
                return

            status_raw = await MCPManager.call_tool(
                "job-protocol-mcp",
                "get_job_status",
                {
                    "input": {
                        "job_id": job_ref["job_id"],
                        "process_id": job_ref["process_id"],
                        "tail_lines": 200,
                        "job_token": job_ref.get("job_token", ""),
                        **dict(job_ref.get("request_scope") or {}),
                    }
                },
            )
            status_data = _unwrap_mcp_response(status_raw)
            current_status = str(status_data.get("status", "") or "").strip().lower()
            heartbeat_workspace_patch: dict[str, Any] = {
                "job_status": current_status,
                "started_at": workspace_patch.get("started_at", ""),
            }
            tool_progress = _extract_tool_progress(status_data)
            if tool_progress:
                progress_signature = json.dumps(
                    tool_progress,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if progress_signature != last_progress_signature:
                    last_progress_signature = progress_signature
                    heartbeat_workspace_patch["tool_progress"] = {
                        **tool_progress,
                        "observed_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(),
                        ),
                    }
            await _heartbeat(
                session,
                task_id,
                claim_token,
                status="running",
                workspace_patch=heartbeat_workspace_patch,
                scope=scope,
            )
            if current_status in terminal_states:
                final_status_payload = status_data
                break
            await asyncio.sleep(_poll_interval())

        final_status = str(final_status_payload.get("status", "") or "").strip().lower()
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        workspace_patch.update({"completed_at": finished_at})
        if final_status == "completed":
            execution_completed = True
            collect_raw = await MCPManager.call_tool(
                "job-protocol-mcp",
                "collect_job_result",
                {
                    "input": {
                        "job_id": job_ref["job_id"],
                        "artifact_paths": submit_input.get("expected_artifacts", []),
                        "job_token": job_ref.get("job_token", ""),
                        **dict(job_ref.get("request_scope") or {}),
                    }
                },
            )
            if not isinstance(collect_raw, dict) or not collect_raw or collect_raw.get("isError"):
                raise RuntimeError("计算已完成，但 Job Protocol 未返回可用结果")
            collect_data = _unwrap_mcp_response(collect_raw)
            await _complete(
                session,
                task_id,
                claim_token,
                result_summary=_stringify_summary(collect_data),
                workspace_patch=workspace_patch,
                job_ref=job_ref,
                scope=scope,
            )
            return

        if final_status in {"cancelled", "canceled", "terminated"}:
            await _cancel(
                session,
                task_id,
                claim_token,
                result_summary=_stringify_summary(final_status_payload),
                workspace_patch={"cancelled_at": finished_at, **workspace_patch},
                scope=scope,
            )
            return

        await _fail(
            session,
            task_id,
            claim_token,
            error_summary=_stringify_summary(final_status_payload),
            workspace_patch=workspace_patch,
            job_ref=job_ref,
            scope=scope,
        )
    except Exception as exc:
        from tcode_agent.runtime.telemetry import redact_telemetry_text
        detail = redact_telemetry_text(str(exc), limit=600)
        # Keep actionable diagnostics in the service log; do not expose raw inputs/tokens in UI.
        print(f"[bridge-worker] task {task_id} failed at "
              f"{'submit/poll' if submit_started else 'preflight'}: {type(exc).__name__}: {detail}",
              file=sys.stderr, flush=True)
        if not submit_started:
            await _fail(
                session, task_id, claim_token,
                error_summary=f"提交前检查失败，计算请求尚未发送：{detail or type(exc).__name__}",
                workspace_patch={**workspace_patch, "recovery": {"state": "submission_not_sent",
                    "error": detail or type(exc).__name__, "error_type": type(exc).__name__}}, scope=scope,
            )
            return
        # Loss of contact is not evidence of failed computation. Never submit
        # again here; recovery observes the persisted reference after lease expiry.
        try:
            if job_ref:
                await _attach_job_ref(session, task_id, claim_token, job_ref, workspace_patch, scope=scope)
            if execution_completed:
                workspace_patch.update(job_status="completed", transfer_state="retry_wait")
            recovery_error = (
                "计算已完成，结果尚未回传；后台将按原任务核对，不会重新提交。"
                if execution_completed else
                f"提交或状态查询失败：{detail or type(exc).__name__}；等待后台核对，不会自动重新提交。"
            )
            await _heartbeat(session, task_id, claim_token, status="running",
                workspace_patch={**workspace_patch, "recovery": {"state": "retry_wait" if job_ref else "submission_unknown", "error": recovery_error, "error_type": type(exc).__name__}}, scope=scope)
        except Exception:
            # The existing claimed row remains durable even if Tracker is offline.
            pass
    finally:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                task = await _claim_next(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[bridge-worker] loop error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(_poll_interval())
                continue
            if not task:
                await asyncio.sleep(_poll_interval())
                continue
            try:
                await _run_task(session, task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[bridge-worker] task error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(_poll_interval())


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(MCPManager.close())
            loop.close()
        except Exception:
            pass
