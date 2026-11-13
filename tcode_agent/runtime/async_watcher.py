"""AsyncResultWatcher: polls for completed Symphony-routed long tasks and
injects results back into the originating agent session as synthetic tool results.

Lifecycle:
1. When a Symphony-routed MCP job is submitted (LOCAL-xxx accepted), register it.
2. A background asyncio task polls SharedTracker (SQLite) every few seconds.
   Legacy JSON is imported once when a new Tracker database is empty; it is
   never used as a live fallback or authority.
3. When a LOCAL issue transitions to Done/Closed, call `collect_job_result` via MCP.
4. Persist an owner-scoped async completion event without replacing the transcript.
5. Append or queue a standalone user-visible completion card.
6. Mark as injected (dedup by LOCAL identifier).
7. Notify the frontend. A foreground Agent turn starts only after an explicit
   continuation action, unless an orchestrated workflow opted into ``auto``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..presentation.job_results import tmcp_content_blocks, tmcp_summary_text

_registry: dict[str, dict[str, Any]] = {}
_injected: set[str] = set()
_pending_session_ids: set[str] = set()
_lock = asyncio.Lock()
_watcher_task: Optional[asyncio.Task] = None
_recovery_task: Optional[asyncio.Task] = None
_health: dict[str, Any] = {
    "started_at": "",
    "last_poll_at": "",
    "last_success_at": "",
    "last_error_at": "",
    "last_error": "",
    "consecutive_failures": 0,
}

_PENDING_CONTINUATIONS_METADATA_KEY = "pending_continuations"


def _record_async_runtime(
    identifier: str,
    *,
    status: str,
    phase: str,
    source: str = "async_watcher",
    session_id: str = "",
    target_server: str = "",
    target_tool: str = "",
    workdir: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    try:
        from .task_runtime import (
            TaskRuntimeStore,
            task_runtime_id_for_external_job,
        )

        TaskRuntimeStore.record_event(
            runtime_id=task_runtime_id_for_external_job(identifier),
            runtime_kind="mcp_job",
            status=status,
            phase=phase,
            source=source,
            session_id=session_id,
            title=f"{target_server}.{target_tool}".strip("."),
            external_id=identifier,
            tool_name=target_tool,
            metadata={
                "identifier": identifier,
                "target_server": target_server,
                "target_tool": target_tool,
                "supports_resume": True,
                "interrupt_behavior": "async_job",
                **dict(metadata or {}),
            },
            workdir=workdir or None,
        )
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"collecting", "completed", "failed", "cancelled"}:
            TaskRuntimeStore.transition_linked_open(
                identifier,
                status=normalized_status,
                phase=phase,
                source=source,
                session_id=session_id,
                workdir=workdir or None,
                metadata={"linked_external_job": identifier},
            )
    except Exception:
        pass


def _issue_state_to_runtime_status(state: str) -> str:
    normalized = str(state or "").strip().lower()
    if normalized in {"todo", "queued", "open", "pending"}:
        return "waiting_external"
    if normalized in {"in_progress", "in progress", "running", "doing"}:
        return "running"
    if normalized in {"done", "closed", "completed"}:
        return "collecting"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"failed", "error"}:
        return "failed"
    return "waiting_external"


def _poll_interval_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("TCODE_ASYNC_WATCHER_INTERVAL_SECONDS", "3") or "3"))
    except Exception:
        return 3.0


def _retry_limit() -> int:
    try:
        return max(1, min(20, int(os.environ.get("TCODE_ASYNC_WATCHER_RETRY_LIMIT", "5") or "5")))
    except (TypeError, ValueError):
        return 5


def _retry_delay_seconds(attempt: int) -> float:
    try:
        base = max(1.0, float(os.environ.get("TCODE_ASYNC_WATCHER_RETRY_BASE_SECONDS", "3") or "3"))
        maximum = max(base, float(os.environ.get("TCODE_ASYNC_WATCHER_RETRY_MAX_SECONDS", "120") or "120"))
    except (TypeError, ValueError):
        base, maximum = 3.0, 120.0
    return min(maximum, base * (2 ** max(0, int(attempt) - 1)))


def _prune_registry() -> None:
    try:
        retention = max(
            300.0,
            float(os.environ.get("TCODE_ASYNC_WATCHER_FAILED_RETENTION_SECONDS", "86400") or "86400"),
        )
    except (TypeError, ValueError):
        retention = 86400.0
    now = time.time()
    for identifier, reg in list(_registry.items()):
        if str(reg.get("sync_status") or "") != "sync_failed":
            continue
        last_error_at = str(reg.get("last_error_at") or "").strip()
        try:
            failed_at = datetime.fromisoformat(last_error_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            failed_at = float(reg.get("submitted_at") or now)
        if now - failed_at >= retention:
            _registry.pop(identifier, None)


def _enabled() -> bool:
    raw = os.environ.get("TCODE_ASYNC_WATCHER_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# ── Public API ───────────────────────────────────────────────────


def register(
    job_identifier: str,
    session_id: str,
    *,
    job_id: str = "",
    target_server: str = "",
    target_tool: str = "",
    workdir: str = "",
    protein_run: Optional[dict[str, Any]] = None,
    watcher_state: Optional[dict[str, Any]] = None,
    workspace_generation: int = 0,
    origin_turn_id: str = "",
    origin_request_id: str = "",
    continuation_policy: str = "notify",
    task_context: Optional[dict[str, Any]] = None,
) -> None:
    """Register a LOCAL job for completion watching.

    Called immediately after a Symphony-routed MCP tool returns accepted_async.
    """
    if not _enabled():
        return
    key = str(job_identifier or "").strip()
    if not key or not session_id:
        return

    persisted_state = dict(watcher_state or {})
    _registry[key] = {
        "session_id": str(session_id).strip(),
        "job_id": str(job_id or "").strip(),
        "target_server": str(target_server or "").strip(),
        "target_tool": str(target_tool or "").strip(),
        "task_context": dict(task_context or persisted_state.get("task_context") or {}),
        "submitted_at": time.time(),
        "workdir": str(workdir or "").strip(),
        "status": "queued",
        "protein_run": dict(protein_run or {}),
        "sync_status": str(persisted_state.get("sync_status") or "pending"),
        "retry_count": max(0, int(persisted_state.get("retry_count") or 0)),
        "next_retry_at": 0.0,
        "last_error": str(persisted_state.get("last_error") or ""),
        "last_error_at": str(persisted_state.get("last_error_at") or ""),
        "run_settled": bool(persisted_state.get("run_settled", False)),
        "workspace_generation": max(
            0,
            int(
                workspace_generation
                or persisted_state.get("workspace_generation")
                or 0
            ),
        ),
        "origin_turn_id": str(
            origin_turn_id or persisted_state.get("origin_turn_id") or ""
        ).strip(),
        "origin_request_id": str(
            origin_request_id or persisted_state.get("origin_request_id") or ""
        ).strip(),
        "continuation_policy": str(
            continuation_policy
            or persisted_state.get("continuation_policy")
            or "notify"
        ).strip().lower(),
    }
    _injected.discard(key)
    _record_async_runtime(
        key,
        status="waiting_external" if str(job_id or '').strip() else "queued",
        phase="registered",
        session_id=str(session_id).strip(),
        target_server=str(target_server or "").strip(),
        target_tool=str(target_tool or "").strip(),
        workdir=str(workdir or "").strip(),
        metadata={
            "job_id": str(job_id or "").strip(),
            "protein_run": dict(protein_run or {}),
            "origin_turn_id": str(origin_turn_id or "").strip(),
            "task_context": dict(task_context or persisted_state.get("task_context") or {}),
            "origin_request_id": str(origin_request_id or "").strip(),
            "continuation_policy": str(continuation_policy or "notify").strip().lower(),
        },
    )


def unregister(job_identifier: str) -> bool:
    """Stop watching one job without erasing its delivered-result marker."""

    identifier = str(job_identifier or "").strip()
    if not identifier:
        return False
    return _registry.pop(identifier, None) is not None


def recover_pending_from_tracker(workdir: str = "", *, limit: int = 500) -> int:
    """Re-register in-flight LOCAL jobs from SharedTracker after process restart."""
    if not _enabled():
        return 0
    try:
        from .shared_tracker import SharedTracker

        page_size = max(1, min(1000, int(limit or 500)))
        tasks: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = SharedTracker.list_tasks(
                workdir or None,
                kind="symphony_local_issue",
                limit=page_size,
                offset=offset,
            )
            tasks.extend(item for item in page if isinstance(item, dict))
            if len(page) < page_size:
                break
            offset += len(page)
    except Exception:
        tasks = []

    terminal_statuses = {"completed", "failed", "cancelled", "duplicate"}
    recovered = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "") or "").strip().lower()
        identifier = str(task.get("identifier", "") or "").strip()
        if not identifier or identifier in _registry:
            continue
        try:
            from ..workspace.async_protein_run import (
                extract_link,
                needs_terminal_recovery,
            )

            protein_run = extract_link(task)
        except Exception:
            protein_run = {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        watcher_state = metadata.get("async_watcher") if isinstance(metadata.get("async_watcher"), dict) else {}
        if watcher_state.get('sync_status') == 'discarded':
            continue
        needs_sync_recovery = str(watcher_state.get("sync_status") or "").strip() in {
            "syncing",
            "retry_wait",
            "sync_failed",
        }
        # A generic job can finish while the desktop is closed, before the
        # watcher ever records delivery. A persisted remote receipt is required;
        # old pre-submission failures are not rediscovered as accepted jobs.
        if (task.get('job_ref') or {}).get('job_id') and watcher_state.get('sync_status') != 'succeeded':
            needs_sync_recovery = True
        if status in terminal_statuses and not needs_sync_recovery and not (
            protein_run and needs_terminal_recovery(protein_run, identifier)
        ):
            continue
        resume_data = task.get("resume_data") if isinstance(task.get("resume_data"), dict) else {}
        session_id = str(
            resume_data.get("parent_session_id")
            or task.get("parent_session_id")
            or task.get("session_id")
            or resume_data.get("session_id")
            or ""
        ).strip()
        if not session_id:
            continue
        job_ref = task.get("job_ref") if isinstance(task.get("job_ref"), dict) else {}
        task_workspace = (
            task.get("workspace")
            if isinstance(task.get("workspace"), dict)
            else {}
        )
        register(
            identifier,
            session_id,
            job_id=str(job_ref.get("job_id") or ""),
            target_server=str(task.get("tool_server", "") or ""),
            target_tool=str(task.get("tool_name", "") or ""),
            workdir=workdir,
            protein_run=protein_run,
            watcher_state=watcher_state,
            workspace_generation=int(
                task_workspace.get("workspace_generation") or 0
            ),
            origin_turn_id=str(resume_data.get("origin_turn_id") or ""),
            task_context=dict(resume_data.get("task_context") or {}),
            origin_request_id=str(resume_data.get("origin_request_id") or ""),
            continuation_policy=str(
                resume_data.get("continuation_policy") or "notify"
            ),
        )
        recovered += 1
    try:
        from ..workspace.async_protein_run import discover_pending_runs

        discovered = discover_pending_runs(
            workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd(),
            limit=None,
        )
    except Exception:
        discovered = []
    for item in discovered:
        identifier = str(item.get("identifier") or "").strip()
        if not identifier or identifier in _registry:
            continue
        register(
            identifier,
            str(item.get("session_id") or "").strip(),
            target_server=str(item.get("target_server") or "").strip(),
            target_tool=str(item.get("target_tool") or "").strip(),
            workdir=str(item.get("workdir") or workdir or "").strip(),
            protein_run=(
                dict(item.get("protein_run") or {})
                if isinstance(item.get("protein_run"), dict)
                else {}
            ),
            origin_turn_id=str(item.get("origin_turn_id") or ""),
            origin_request_id=str(item.get("origin_request_id") or ""),
            continuation_policy=str(item.get("continuation_policy") or "notify"),
        )
        recovered += 1
    return recovered


def is_injected(job_identifier: str) -> bool:
    return str(job_identifier or "").strip() in _injected


def pending_count() -> int:
    return sum(1 for v in _registry.values() if v["status"] not in {"done", "injected"})


def health_snapshot() -> dict[str, Any]:
    _prune_registry()
    records = list(_registry.values())
    pending = [item for item in records if str(item.get("status") or "") not in {"done", "injected"}]
    failed = [item for item in records if str(item.get("sync_status") or "") == "sync_failed"]
    oldest = min((float(item.get("submitted_at") or 0) for item in pending), default=0.0)
    return {
        "enabled": _enabled(),
        "running": bool(_watcher_task is not None and not _watcher_task.done()),
        **dict(_health),
        "registered_count": len(records),
        "pending_count": len(pending),
        "sync_failed_count": len(failed),
        "injected_cache_size": len(_injected),
        "oldest_pending_at": (
            datetime.fromtimestamp(oldest, timezone.utc).isoformat().replace("+00:00", "Z")
            if oldest > 0
            else ""
        ),
    }


async def start_watcher(
    *,
    workdir: str = "",
    agent_event_emitter: Optional[Any] = None,
) -> None:
    """Start the background watcher task. Idempotent — safe to call multiple times."""
    global _watcher_task, _recovery_task
    if not _enabled():
        return

    async with _lock:
        recover_pending_from_tracker(workdir)
        if _watcher_task is not None and not _watcher_task.done():
            return
        _health["started_at"] = _health.get("started_at") or _utc_now_iso()
        _watcher_task = asyncio.create_task(
            _poll_loop(workdir=workdir, agent_event_emitter=agent_event_emitter)
        )
        _recovery_task = asyncio.create_task(_recovery_loop(workdir))


async def stop_watcher() -> None:
    global _watcher_task, _recovery_task
    async with _lock:
        if _watcher_task is not None and not _watcher_task.done():
            _watcher_task.cancel()
            try:
                await _watcher_task
            except asyncio.CancelledError:
                pass
        _watcher_task = None
        if _recovery_task is not None:
            _recovery_task.cancel()
            await asyncio.gather(_recovery_task, return_exceptions=True)
            _recovery_task = None


async def _recovery_loop(workdir: str) -> None:
    from .remote_recovery import reconcile_roots
    while True:
        try:
            await reconcile_roots(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd())
        except asyncio.CancelledError:
            return
        except Exception:
            # A storage/network failure must not kill the persistent loop.
            pass
        await asyncio.sleep(3)


# ── Internal polling loop ────────────────────────────────────────


async def _poll_loop(
    *,
    workdir: str = "",
    agent_event_emitter: Optional[Any] = None,
) -> None:
    """Background loop that polls canonical SharedTracker state."""
    interval = _poll_interval_seconds()

    while True:
        _prune_registry()
        _health["last_poll_at"] = _utc_now_iso()
        try:
            from .remote_recovery import tracker_roots
            for root in tracker_roots(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()):
                recover_pending_from_tracker(root)
            await _check_completed_jobs(
                workdir=workdir,
                agent_event_emitter=agent_event_emitter,
            )
            await _flush_pending_continuations()
            _health["last_success_at"] = _utc_now_iso()
            _health["last_error"] = ""
            _health["consecutive_failures"] = 0
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _health["last_error_at"] = _utc_now_iso()
            _health["last_error"] = str(exc)[:2000]
            _health["consecutive_failures"] = int(_health.get("consecutive_failures") or 0) + 1
            print(f"[async-watcher] poll failed: {exc}")
        await asyncio.sleep(interval)


async def _check_completed_jobs(
    *,
    workdir: str = "",
    agent_event_emitter: Optional[Any] = None,
) -> None:
    """Scan canonical Tracker tasks for completed LOCAL jobs and inject results."""
    if not _registry:
        return

    issues = _load_registry_issues(workdir)
    if not issues:
        return

    terminal_states = {
        "done",
        "closed",
        "cancelled",
        "canceled",
        "completed",
        "failed",
        "error",
    }

    for issue in issues:
        if (issue.get("workspace") or {}).get("transfer_state") in {"pending", "downloading", "retry_wait"}:
            continue
        workspace = issue.get("workspace") or {}
        # Remote failures also collect diagnostics before terminal delivery.
        # Local/pre-submission failures and cancellations keep their old path.
        if str(issue.get("state") or "").strip().lower() in {"done", "completed", "closed", "failed", "error"} and (workspace.get("job_ref") or {}).get("job_id") and workspace.get("transfer_state") != "ready":
            continue
        identifier = str(issue.get("identifier", "") or "").strip()
        if not identifier:
            continue

        state = str(issue.get("state", "") or "").strip().lower()
        if state not in terminal_states:
            # Update in-flight status but don't inject yet
            if identifier in _registry:
                previous = str(_registry[identifier].get("status", "") or "")
                previous_job_id = str(_registry[identifier].get('job_id') or '')
                job_id = str((workspace.get('job_ref') or {}).get('job_id') or workspace.get('job_id') or previous_job_id).strip()
                _registry[identifier]['job_id'] = job_id
                runtime_status = _issue_state_to_runtime_status(state)
                if not job_id and str(issue.get('execution_channel') or workspace.get('execution_channel') or '') == 'bridge':
                    runtime_status = 'queued'
                heartbeat_at = str(issue.get("claim_heartbeat_at") or "").strip()
                previous_heartbeat = str(
                    _registry[identifier].get("last_heartbeat_at") or ""
                ).strip()
                _registry[identifier]["status"] = state
                _registry[identifier]["last_heartbeat_at"] = heartbeat_at
                if previous != state or heartbeat_at != previous_heartbeat or previous_job_id != job_id:
                    try:
                        from ..workspace.async_protein_run import extract_link, update_status

                        update_status(
                            extract_link(_registry[identifier], issue),
                            identifier=identifier,
                            status=runtime_status,
                            phase="poll",
                            target_server=str(_registry[identifier].get("target_server", "") or ""),
                            target_tool=str(_registry[identifier].get("target_tool", "") or ""),
                            metadata={
                                "issue_state": state,
                                "job_id": _resolved_job_id(
                                    identifier,
                                    _registry[identifier],
                                    issue,
                                ),
                                "last_heartbeat_at": heartbeat_at,
                            },
                        )
                    except Exception:
                        pass
                    _record_async_runtime(
                        identifier,
                        status=runtime_status,
                        phase="poll",
                        session_id=str(_registry[identifier].get("session_id", "") or ""),
                        target_server=str(_registry[identifier].get("target_server", "") or ""),
                        target_tool=str(_registry[identifier].get("target_tool", "") or ""),
                        workdir=str(_registry[identifier].get("workdir", "") or workdir or ""),
                        metadata={"issue_state": state, "job_id": job_id},
                    )
            continue

        # Already injected — skip
        if identifier in _injected:
            continue

        # Not in our registry — skip
        reg = _registry.get(identifier)
        if reg is None:
            continue

        if str(reg.get("sync_status") or "") == "sync_failed":
            continue
        next_retry_at = float(reg.get("next_retry_at") or 0)
        if next_retry_at > time.time():
            continue

        _record_async_runtime(
            identifier,
            status="collecting",
            phase="terminal_detected",
            session_id=str(reg.get("session_id", "") or ""),
            target_server=str(reg.get("target_server", "") or ""),
            target_tool=str(reg.get("target_tool", "") or ""),
            workdir=str(reg.get("workdir", "") or workdir or ""),
            metadata={"issue_state": state},
        )
        try:
            from ..workspace.async_protein_run import extract_link, update_status

            update_status(
                extract_link(reg, issue),
                identifier=identifier,
                status="collecting",
                phase="terminal_detected",
                target_server=str(reg.get("target_server", "") or ""),
                target_tool=str(reg.get("target_tool", "") or ""),
                metadata={
                    "issue_state": state,
                    "job_id": _resolved_job_id(identifier, reg, issue),
                },
            )
        except Exception:
            pass

        # Transition to done — collect and inject
        await _inject_result(
            identifier=identifier,
            reg=reg,
            issue=issue,
            agent_event_emitter=agent_event_emitter,
            workdir=workdir,
        )


def _patch_tracker_sync_state(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    workdir: str,
) -> None:
    payload = {
        "sync_status": str(reg.get("sync_status") or "pending"),
        "retry_count": max(0, int(reg.get("retry_count") or 0)),
        "next_retry_at": (
            datetime.fromtimestamp(float(reg.get("next_retry_at") or 0), timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if float(reg.get("next_retry_at") or 0) > 0
            else ""
        ),
        "last_error": str(reg.get("last_error") or "")[:2000],
        "last_error_at": str(reg.get("last_error_at") or ""),
        "run_settled": bool(reg.get("run_settled", False)),
        "updated_at": _utc_now_iso(),
    }
    try:
        from .shared_tracker import SharedTracker
    except Exception:
        return
    candidates = _tracker_workdir_candidates(reg=reg, issue=issue, workdir=workdir) or [""]
    for candidate_workdir in candidates:
        try:
            updated = SharedTracker.patch_task(
                identifier=identifier,
                metadata_patch={"async_watcher": payload},
                workspace_patch={"async_watcher": payload},
                event_kind="async_sync_updated",
                source="async_watcher",
                workdir=candidate_workdir,
            )
            if updated is not None:
                return
        except Exception:
            continue


def _update_run_sync_state(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    status: str,
    phase: str,
    error: str = "",
    continuation: Optional[dict[str, Any]] = None,
) -> None:
    try:
        from ..workspace.async_protein_run import extract_link, update_status

        update_status(
            extract_link(reg, issue),
            identifier=identifier,
            status=status,
            phase=phase,
            target_server=str(reg.get("target_server") or issue.get("tool_server") or ""),
            target_tool=str(reg.get("target_tool") or issue.get("tool_name") or ""),
            result_summary=str((continuation or {}).get("result_summary") or ""),
            error_summary=str(error or (continuation or {}).get("error_summary") or "")[:4000],
            metadata={
                "sync_status": str(reg.get("sync_status") or ""),
                "sync_retry_count": max(0, int(reg.get("retry_count") or 0)),
                "sync_last_error_at": str(reg.get("last_error_at") or ""),
                "sync_next_retry_at": (
                    datetime.fromtimestamp(float(reg.get("next_retry_at") or 0), timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if float(reg.get("next_retry_at") or 0) > 0
                    else ""
                ),
                "job_id": _resolved_job_id(identifier, reg, issue),
            },
        )
    except Exception as exc:
        print(f"[async-watcher] failed to persist Run sync state for {identifier}: {exc}")


def _mark_sync_failure(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    workdir: str,
    error: Exception | str,
) -> None:
    retry_count = max(0, int(reg.get("retry_count") or 0)) + 1
    exhausted = retry_count >= _retry_limit() and not (issue.get("workspace") or {}).get("recovery")
    reg["retry_count"] = retry_count
    reg["sync_status"] = "sync_failed" if exhausted else "retry_wait"
    reg["status"] = reg["sync_status"]
    reg["last_error"] = str(error)[:2000]
    reg["last_error_at"] = _utc_now_iso()
    reg["next_retry_at"] = 0.0 if exhausted else time.time() + _retry_delay_seconds(retry_count)
    _patch_tracker_sync_state(
        identifier=identifier,
        reg=reg,
        issue=issue,
        workdir=workdir,
    )
    _update_run_sync_state(
        identifier=identifier,
        reg=reg,
        issue=issue,
        status="collecting",
        phase="sync_failed" if exhausted else "sync_retry_wait",
        error=str(error),
    )
    _record_async_runtime(
        identifier,
        status="failed" if exhausted else "collecting",
        phase="sync_failed" if exhausted else "sync_retry_wait",
        session_id=str(reg.get("session_id") or ""),
        target_server=str(reg.get("target_server") or ""),
        target_tool=str(reg.get("target_tool") or ""),
        workdir=str(reg.get("workdir") or workdir or ""),
        metadata={
            "error": str(error)[:2000],
            "retry_count": retry_count,
            "next_retry_at": reg.get("next_retry_at"),
        },
    )


def _mark_sync_success(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    workdir: str,
    continuation: dict[str, Any],
) -> None:
    reg["sync_status"] = "succeeded"
    reg["status"] = "injected"
    reg["next_retry_at"] = 0.0
    reg["last_error"] = ""
    reg["injected_at"] = time.time()
    _patch_tracker_sync_state(
        identifier=identifier,
        reg=reg,
        issue=issue,
        workdir=workdir,
    )
    _update_run_sync_state(
        identifier=identifier,
        reg=reg,
        issue=issue,
        status=str(continuation.get("status") or "completed"),
        phase="result_delivered",
        continuation=continuation,
    )
    _injected.add(identifier)
    while len(_injected) > 4096:
        _injected.pop()
    _registry.pop(identifier, None)


async def _inject_result(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    agent_event_emitter: Optional[Any] = None,
    workdir: str = "",
) -> bool:
    """Collect job result and inject into the agent session."""
    session_id = str(reg.get("session_id", "") or "").strip()
    allowed, fence_reason = _workspace_lifecycle_allows_write(
        reg=reg,
        issue=issue,
        workdir=workdir,
    )
    if not allowed:
        print(f"[async-watcher] Discarded {identifier}: {fence_reason}")
        _registry.pop(identifier, None)
        _injected.add(identifier)
        return False
    from ..session.manager import SessionManager
    from ..server.session_lifecycle import _SESSION_DELETE_PENDING
    session_deleted = session_id in _SESSION_DELETE_PENDING
    if not session_deleted and session_id and await SessionManager.get(session_id) is None:
        from ..session.events import SessionEventStore
        projection = await asyncio.to_thread(SessionEventStore.project, session_id)
        session_deleted = bool(projection.get('deleted'))
    if session_deleted:
        # A late result must not resurrect a deleted session or retry delivery
        # forever. Keep calculation state/evidence, revoke only its chat sink.
        reg.update(sync_status='discarded', status='done', last_error='originating session deleted')
        _patch_tracker_sync_state(identifier=identifier, reg=reg, issue=issue, workdir=workdir)
        _registry.pop(identifier, None)
        _injected.add(identifier)
        return False
    reg["sync_status"] = "syncing"
    reg["status"] = "collecting"
    reg["next_retry_at"] = 0.0
    _patch_tracker_sync_state(identifier=identifier, reg=reg, issue=issue, workdir=workdir)
    try:
        result_output = await _collect_job_result(identifier, reg, issue)
        if str(result_output or "").startswith("[async-watcher] collect_job_result failed:"):
            raise RuntimeError(str(result_output))
        result_content = result_output or _build_fallback_result(identifier, issue)
        if ((issue.get("workspace") or {}).get("cancellation") or {}).get("request_id"):
            reg["continuation_policy"] = "notify"
        continuation = _build_continuation_payload(
            identifier=identifier,
            reg=reg,
            issue=issue,
            result_output=result_content,
            additional_artifacts=list((issue.get("workspace") or {}).get("artifacts") or []) if (issue.get("workspace") or {}).get("transfer_state") == "ready" else None,
            materialized_output_path=str((issue.get("workspace") or {}).get("output_root") or "") if (issue.get("workspace") or {}).get("transfer_state") == "ready" else "",
        )
        allowed, fence_reason = _workspace_lifecycle_allows_write(
            reg=reg,
            issue=issue,
            workdir=workdir,
        )
        if not allowed:
            print(f"[async-watcher] Discarded {identifier}: {fence_reason}")
            _registry.pop(identifier, None)
            _injected.add(identifier)
            return False
        from ..workspace.async_protein_run import extract_link, settle_result

        run_link = extract_link(reg, issue)
        settlement_details = (
            dict(reg.get("settlement_details") or {})
            if isinstance(reg.get("settlement_details"), dict)
            else {}
        )
        if run_link and not bool(reg.get("run_settled", False)):
            issue_workspace = _workspace_from_issue(issue)
            result_sources = [
                path.as_posix()
                for path in _candidate_result_files(
                    reg,
                    issue,
                    _parse_json_object(result_content),
                )
            ]
            result_sources.extend(list(continuation.get("artifacts") or []))
            output_abs_path = str(continuation.get("output_abs_path") or "").strip()
            if output_abs_path:
                result_sources.append(output_abs_path)
            settled = await asyncio.to_thread(
                settle_result,
                run_link,
                identifier=identifier,
                status=str(continuation.get("status") or "completed"),
                target_server=str(
                    reg.get("target_server") or issue.get("tool_server") or ""
                ),
                target_tool=str(
                    reg.get("target_tool") or issue.get("tool_name") or ""
                ),
                result_output=result_content,
                result_summary=str(continuation.get("result_summary") or ""),
                error_summary=str(continuation.get("error_summary") or ""),
                artifact_paths=result_sources,
                source_roots=[
                    issue_workspace.get("workdir"),
                    reg.get("workdir"),
                    workdir,
                ],
                metadata={
                    "issue_state": str(continuation.get("issue_state") or ""),
                    "delivery_status": str(
                        continuation.get("delivery_status") or ""
                    ),
                    "job_id": _resolved_job_id(identifier, reg, issue),
                },
                settlement_out=settlement_details,
            )
            if not settled:
                raise RuntimeError("Protein Run result settlement returned false")
            reg["run_settled"] = True
            reg["settlement_details"] = dict(settlement_details)
        elif not run_link and continuation.get('status') == 'completed':
            from ..workspace.async_protein_run import materialize_completed_generic_result
            output_root = str(_workspace_from_issue(issue).get('workdir') or '')
            control_workdir = str(reg.get('workdir') or workdir)
            if output_root and control_workdir and not (Path(output_root) / 'mcp_result.json').exists():
                materialized = await asyncio.to_thread(
                    materialize_completed_generic_result, Path(control_workdir), Path(output_root), result_content,
                )
                if materialized:
                    settlement_details = {
                        'agent_artifacts': [materialized.as_posix()],
                        'agent_output_root': materialized.parent.as_posix(),
                    }
        if settlement_details.get("agent_artifacts"):
            continuation = _build_continuation_payload(
                identifier=identifier,
                reg=reg,
                issue=issue,
                result_output=result_content,
                additional_artifacts=list(
                    settlement_details.get("agent_artifacts") or []
                ),
                materialized_output_path=str(
                    settlement_details.get("agent_output_root") or ""
                ),
            )

        _record_tracker_result(
            identifier=identifier,
            reg=reg,
            issue=issue,
            continuation=continuation,
            result_output=result_content,
            workdir=workdir,
        )

        allowed, fence_reason = _workspace_lifecycle_allows_write(
            reg=reg,
            issue=issue,
            workdir=workdir,
        )
        if not allowed:
            print(
                f"[async-watcher] Suppressed delivery for {identifier}: "
                f"{fence_reason}"
            )
            _registry.pop(identifier, None)
            _injected.add(identifier)
            return False

        if not session_id:
            raise RuntimeError("originating session id is missing")

        from .asset_history import record_async_job

        # Settlement has materialized assets and lifecycle fencing passed.
        # Delivery retries must not replace this terminal snapshot.
        await record_async_job(str(reg.get("workdir") or ""), identifier, completed=True)
        collected_job_id = _resolved_job_id(identifier, reg, issue)
        issue_workspace = _workspace_from_issue(issue)
        job_ref = (
            issue_workspace.get("job_ref")
            if isinstance(issue_workspace.get("job_ref"), dict)
            else {}
        )
        from .async_completion import record_completion_event

        completion_event = await record_completion_event(
            session_id=session_id,
            identifier=identifier,
            continuation=continuation,
            job_id=collected_job_id,
            process_id=str(job_ref.get("process_id") or ""),
            run_id=str(job_ref.get("run_id") or issue_workspace.get("run_id") or ""),
            origin_turn_id=str(reg.get("origin_turn_id") or ""),
            task_context=dict(reg.get("task_context") or {}),
            origin_request_id=str(reg.get("origin_request_id") or ""),
            continuation_policy=str(reg.get("continuation_policy") or "notify"),
        )
        if completion_event is None:
            raise RuntimeError(f"originating session not found: {session_id}")

        continuation_policy = str(
            reg.get("continuation_policy") or "notify"
        ).strip().lower()
        if continuation_policy == "store_only":
            delivery_status = "stored"
        else:
            delivery_status = await _append_or_queue_continuation(
                session_id=session_id,
                continuation=continuation,
            )
        continuation["delivery_status"] = delivery_status
        await record_completion_event(
            session_id=session_id,
            identifier=identifier,
            continuation=continuation,
            job_id=collected_job_id,
            process_id=str(job_ref.get("process_id") or ""),
            run_id=str(job_ref.get("run_id") or issue_workspace.get("run_id") or ""),
            origin_turn_id=str(reg.get("origin_turn_id") or ""),
            task_context=dict(reg.get("task_context") or {}),
            origin_request_id=str(reg.get("origin_request_id") or ""),
            continuation_policy=str(reg.get("continuation_policy") or "notify"),
        )
        if delivery_status not in {"delivered", "already_delivered", "stored"}:
            _pending_session_ids.add(session_id)

        _mark_sync_success(
            identifier=identifier,
            reg=reg,
            issue=issue,
            workdir=workdir,
            continuation=continuation,
        )
        _record_async_runtime(
            identifier,
            status="completed",
            phase="injected",
            session_id=session_id,
            target_server=str(reg.get("target_server", "") or ""),
            target_tool=str(reg.get("target_tool", "") or ""),
            workdir=str(reg.get("workdir", "") or workdir or ""),
            metadata={
                "completion_event_id": str(completion_event.get("id") or ""),
                "result_preview": result_content[:2000],
                "delivery_status": delivery_status,
            },
        )

        # Step 3: Notify frontend so it can trigger a new agent turn
        if agent_event_emitter is not None:
            try:
                result = agent_event_emitter({
                    "kind": "async_job_completed",
                    "session_id": session_id,
                    "job_identifier": identifier,
                    "target_server": reg.get("target_server", ""),
                    "target_tool": reg.get("target_tool", ""),
                    "message": f"Async job {identifier} completed and result injected.",
                })
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
        return True
    except Exception as exc:
        print(f"[async-watcher] Failed to synchronize result for {identifier}: {exc}")
        _mark_sync_failure(
            identifier=identifier,
            reg=reg,
            issue=issue,
            workdir=workdir,
            error=exc,
        )
        return False


async def reconcile_job(
    job_identifier: str,
    *,
    session_id: str,
    job_id: str = "",
    target_server: str = "",
    target_tool: str = "",
    workdir: str = "",
    protein_run: Optional[dict[str, Any]] = None,
    agent_event_emitter: Optional[Any] = None,
) -> dict[str, Any]:
    """Force one terminal job through the idempotent synchronization path."""

    identifier = str(job_identifier or "").strip()
    if not identifier:
        return {"status": "invalid", "error": "job identifier is required"}
    previous_reg = dict(_registry.get(identifier) or {})
    register(
        identifier,
        session_id,
        job_id=job_id,
        target_server=target_server,
        target_tool=target_tool,
        workdir=workdir,
        protein_run=protein_run,
    )
    reg = _registry.get(identifier)
    if reg is None:
        return {"status": "disabled", "error": "async watcher is disabled"}
    reg.update(
        {
            "sync_status": "pending",
            "retry_count": 0,
            "next_retry_at": 0.0,
            "last_error": "",
            "last_error_at": "",
        }
    )
    _injected.discard(identifier)
    issues = _load_registry_issues(workdir)
    try:
        from ..server.state import ServerState

        server_workdir = str(ServerState.get_workdir() or "").strip()
    except Exception:
        server_workdir = ""
    if server_workdir and server_workdir != str(workdir or "").strip():
        issues = _dedupe_issues([*issues, *_load_issues(server_workdir)])
    issue = next(
        (
            item
            for item in issues
            if str(item.get("identifier") or "").strip() == identifier
        ),
        None,
    )
    if issue is None:
        if previous_reg:
            _registry[identifier] = previous_reg
        else:
            _registry.pop(identifier, None)
        return {"status": "not_found", "error": "tracker issue not found"}
    persisted_sync = (
        (issue.get("workspace") or {}).get("async_watcher")
        if isinstance(issue.get("workspace"), dict)
        and isinstance((issue.get("workspace") or {}).get("async_watcher"), dict)
        else {}
    )
    if persisted_sync:
        reg["run_settled"] = bool(persisted_sync.get("run_settled", False))
    state = str(issue.get("state") or "").strip().lower()
    if state not in {"done", "closed", "cancelled", "canceled", "completed", "failed", "error"}:
        reg["status"] = state or "queued"
        return {"status": "not_terminal", "issue_state": state or "unknown"}
    synchronized = await _inject_result(
        identifier=identifier,
        reg=reg,
        issue=issue,
        agent_event_emitter=agent_event_emitter,
        workdir=workdir,
    )
    latest = _registry.get(identifier, reg)
    return {
        "status": "succeeded" if synchronized else str(latest.get("sync_status") or "sync_failed"),
        "issue_state": state,
        "retry_count": max(0, int(latest.get("retry_count") or 0)),
        "error": str(latest.get("last_error") or ""),
    }


def _collect_job_id_candidates(
    identifier: str,
    reg: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Return job-protocol ids to try, preferring the real submitted job id."""
    candidates: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    issue = issue if isinstance(issue, dict) else {}
    workspace = issue.get("workspace") if isinstance(issue.get("workspace"), dict) else {}
    job_ref = workspace.get("job_ref") if isinstance(workspace.get("job_ref"), dict) else {}
    if not job_ref and isinstance(issue.get("job_ref"), dict):
        job_ref = issue.get("job_ref") or {}

    for source in (job_ref, workspace, reg):
        if not isinstance(source, dict):
            continue
        _add(source.get("job_id"))

    if not str(identifier or "").upper().startswith("LOCAL-"):
        _add(identifier)
    return [
        candidate
        for candidate in candidates
        if not candidate.upper().startswith("LOCAL-")
    ]


def _resolved_job_id(
    identifier: str,
    reg: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
) -> str:
    """Return a real job-protocol id without mistaking LOCAL-* for one."""

    for candidate in _collect_job_id_candidates(identifier, reg, issue):
        if not candidate.upper().startswith("LOCAL-"):
            return candidate
    return ""


async def _collect_job_result(
    identifier: str,
    reg: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
) -> str:
    """Call collect_job_result via MCP and return the output text."""
    try:
        issue = issue if isinstance(issue, dict) else {}
        state = str(issue.get("state") or issue.get("status") or "").strip().lower()
        if state in {"failed", "error", "cancelled", "canceled", "terminated"}:
            if state in {"failed", "error"} and (_workspace_from_issue(issue).get("recovery") or {}).get("collected"):
                persisted = str(issue.get("result_summary") or "").strip()
                if isinstance(_parse_json_object(persisted), dict):
                    return persisted
            return json.dumps(
                {
                    "status": "cancelled"
                    if state in {"cancelled", "canceled", "terminated"}
                    else "failed",
                    "identifier": identifier,
                    "error_summary": str(issue.get("error_summary") or "").strip(),
                    "result_summary": str(issue.get("result_summary") or "").strip(),
                },
                ensure_ascii=False,
            )

        from ..mcp.manager_auto import MCPManager

        candidates = _collect_job_id_candidates(identifier, reg, issue)
        if not candidates:
            return (
                "[async-watcher] collect_job_result failed: "
                f"no real job-protocol job_id is attached to {identifier}"
            )

        # Bridge/Symphony workers already call collect_job_result before they
        # mark a task completed. Reuse that persisted payload when available
        # instead of issuing a second, potentially non-idempotent collection.
        persisted_result = str(issue.get("result_summary") or "").strip()
        if state in {"completed", "complete", "done", "closed", "success"} and persisted_result:
            # Tracker summaries are deliberately bounded.  A structured MCP
            # result can therefore be cut in the middle of its JSON payload,
            # which would silently discard presentation blocks such as GNINA's
            # ranking table.  Reuse complete JSON and ordinary text summaries,
            # but recollect a truncated structured payload from Job Protocol.
            structured_prefix = persisted_result.lstrip().startswith(("{", "["))
            if not structured_prefix or _parse_json_object(persisted_result) is not None:
                return persisted_result

        if not MCPManager.is_initialized():
            await MCPManager.initialize()

        last_error = ""
        for job_id in candidates:
            if job_id.startswith("tcode-job-"):
                from .remote_cancel import call_job_tool
                ref = issue.get("job_ref") or {}
                if ref.get("job_id") != job_id:
                    last_error = "missing original local Job Protocol reference"
                    continue
                result = await call_job_tool(ref, "collect_job_result")
            else:
                if os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"):
                    last_error = "original external Job Protocol not active"
                    continue
                result = await MCPManager.call_tool(
                    "job-protocol-mcp", "collect_job_result", {"input": {"job_id": job_id}},
                )

            if isinstance(result, dict):
                if result.get("isError"):
                    last_error = str(
                        result.get("content", [{}])[0].get("text", "")
                        if isinstance(result.get("content"), list)
                        else ""
                    ) or json.dumps(result)
                    continue
                # Extract text content from MCP response
                content = result.get("content", [])
                if isinstance(content, list) and content:
                    return str(content[0].get("text", json.dumps(result)))
                return json.dumps(result)

            return str(result)
        return (
            "[async-watcher] collect_job_result failed: "
            + (last_error or f"no collectable job id for {identifier}")
        )
    except Exception as e:
        return f"[async-watcher] collect_job_result failed: {e}"


def _build_fallback_result(identifier: str, issue: dict[str, Any]) -> str:
    """Build a minimal result message when collect_job_result is unavailable."""
    return json.dumps({
        "job_id": identifier,
        "status": str(issue.get("state", "done")),
        "title": str(issue.get("title", "")),
        "note": "Async job completed. Full result unavailable via MCP; check workspace artifacts.",
    }, ensure_ascii=False)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_json_object(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    value = text.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    start_positions = [pos for pos in (value.find("{"), value.find("[")) if pos >= 0]
    if not start_positions:
        return None
    try:
        decoder = json.JSONDecoder()
        decoded, _ = decoder.raw_decode(value[min(start_positions):])
        return decoded
    except Exception:
        return None


def _short_text(value: Any, limit: int = 900) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _content_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def _nested_get(payload: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    for nested_key in ("structuredContent", "result", "data", "summary"):
        nested = payload.get(nested_key)
        value = _nested_get(nested, keys)
        if value not in (None, ""):
            return value
    return None


def _business_failure_reason(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if bool(payload.get("isError")):
        return "tool_result_is_error"
    for key in ("status", "state", "result_status"):
        value = payload.get(key)
        text = str(value or "").strip().lower()
        if text in {"failed", "error", "cancelled", "canceled", "terminated", "timed_out"}:
            return f"{key}={value}"
    for key in ("result_code", "validated", "success", "ok"):
        if key in payload and payload.get(key) is False:
            return f"{key}=false"
    for nested_key in ("structuredContent", "result", "data", "summary"):
        reason = _business_failure_reason(payload.get(nested_key))
        if reason:
            return reason
    return ""


def _extract_error_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "error_summary", "message", "detail", "stderr"):
        value = payload.get(key)
        if value:
            return _short_text(value)
    text = _content_text(payload)
    if text:
        return _short_text(text)
    for nested_key in ("structuredContent", "result", "data", "summary"):
        nested = payload.get(nested_key)
        error = _extract_error_summary(nested)
        if error:
            return error
    return ""


def _workspace_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    workspace = issue.get("workspace") if isinstance(issue.get("workspace"), dict) else {}
    return dict(workspace or {})


def _candidate_result_files(reg: dict[str, Any], issue: dict[str, Any], collect_payload: Any) -> list[Path]:
    candidates: list[Path] = []

    def _add(path_value: Any, base: str = "") -> None:
        text = str(path_value or "").strip()
        if not text:
            return
        path = Path(text)
        if not path.is_absolute() and base:
            path = Path(base) / path
        try:
            resolved = path.expanduser().resolve(strict=False)
        except Exception:
            resolved = path
        if resolved not in candidates:
            candidates.append(resolved)

    workspace = _workspace_from_issue(issue)
    for base in (
        workspace.get("workdir"),
        reg.get("workdir"),
        issue.get("workdir"),
    ):
        base_text = str(base or "").strip()
        if base_text:
            _add(Path(base_text) / "mcp_result.json")

    for source in (workspace, issue, collect_payload if isinstance(collect_payload, dict) else {}):
        if not isinstance(source, dict):
            continue
        artifacts = source.get("artifacts") or source.get("expected_artifacts") or []
        if isinstance(artifacts, list):
            base_text = str(source.get("workdir") or workspace.get("workdir") or "").strip()
            for artifact in artifacts:
                _add(artifact, base=base_text)

    return candidates


def _read_json_file(path: Path) -> Any:
    try:
        if path.is_file() and path.stat().st_size > 0:
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _collect_artifact_paths(issue: dict[str, Any], collect_payload: Any) -> list[str]:
    paths: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in paths:
            paths.append(text)

    workspace = _workspace_from_issue(issue)
    for source in (collect_payload if isinstance(collect_payload, dict) else {}, workspace, issue):
        if not isinstance(source, dict):
            continue
        for key in ("artifacts", "expected_artifacts", "rejected_artifacts"):
            values = source.get(key)
            if isinstance(values, list):
                for item in values:
                    _add(item)
    return paths


def _extract_summary_text(*payloads: Any) -> str:
    for payload in payloads:
        if isinstance(payload, str):
            text = payload.strip()
            if text:
                return _short_text(text, 4000)
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("result_summary", "summary", "message", "detail", "output", "stdout"):
            value = payload.get(key)
            if value:
                return _short_text(value, 4000)
        text = _content_text(payload)
        if text:
            return _short_text(text, 4000)
        for nested_key in ("structuredContent", "result", "data", "summary"):
            nested = payload.get(nested_key)
            text = _extract_summary_text(nested)
            if text:
                return text
    return ""


def _tracker_workdir_candidates(
    *,
    reg: dict[str, Any],
    issue: dict[str, Any],
    workdir: str = "",
) -> list[str]:
    candidates: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if text not in candidates:
            candidates.append(text)

    workspace = _workspace_from_issue(issue)
    _add(issue.get("_tracker_workdir"))
    _add(reg.get("workdir"))
    _add(workdir)
    for key in ("workspace_root", "__workspace_root", "tmcp_workspace_root"):
        _add(workspace.get(key))
    input_snapshot = (
        workspace.get("input_snapshot")
        if isinstance(workspace.get("input_snapshot"), dict)
        else {}
    )
    for key in ("workspace_root", "__workspace_root", "tmcp_workspace_root"):
        _add(input_snapshot.get(key))
        if isinstance(input_snapshot.get("input_config"), dict):
            _add(input_snapshot["input_config"].get(key))
    return candidates


def _record_tracker_result(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    continuation: dict[str, Any],
    result_output: str,
    workdir: str = "",
) -> None:
    try:
        from .shared_tracker import SharedTracker
    except Exception:
        return

    status = str(continuation.get("status") or "").strip().lower()
    if status not in {"completed", "failed", "cancelled"}:
        status = "completed"

    workspace = _workspace_from_issue(issue)
    job_ref = workspace.get("job_ref") if isinstance(workspace.get("job_ref"), dict) else {}
    artifacts = continuation.get("artifacts") if isinstance(continuation.get("artifacts"), list) else []
    output_path = str(continuation.get("output_path") or "").strip()
    output_abs_path = str(continuation.get("output_abs_path") or "").strip()
    now = _utc_now_iso()
    observed_issue_state = str(continuation.get("issue_state") or "").strip()
    result_summary = str(continuation.get("result_summary") or "").strip()
    error_summary = str(continuation.get("error_summary") or "").strip()

    workspace_patch: dict[str, Any] = {
        "status": status,
        "issue_state": status,
        "tracker_status": status,
        "lifecycle_status": status,
        "resume_supported": status == "completed",
    }
    materialization = (reg.get("settlement_details") or {}).get("artifact_materialization") or {}
    if isinstance(materialization.get("remote_catalog"), dict):
        workspace_patch.update(artifact_catalog=materialization["remote_catalog"], transfer_state="ready")
    if observed_issue_state:
        workspace_patch["observed_issue_state"] = observed_issue_state
    if result_summary:
        workspace_patch["result_summary"] = result_summary
    if error_summary:
        workspace_patch["error_summary"] = error_summary
    if output_path:
        workspace_patch["output_root"] = output_path
        workspace_patch["output_path"] = output_path
    if output_abs_path:
        workspace_patch["output_abs_path"] = output_abs_path
    if artifacts:
        workspace_patch["artifacts"] = artifacts
    if status == "completed":
        workspace_patch["completed_at"] = now
    elif status == "failed":
        workspace_patch["failed_at"] = now
    else:
        workspace_patch["cancelled_at"] = now
    try:
        from ..task.orchestration import build_action_policy

        workspace_patch["action_policy"] = build_action_policy(
            lifecycle_status=status,
            job_id=str(job_ref.get("job_id", "") or ""),
            has_submit_input=bool(workspace.get("input_snapshot")),
            resume_supported=False,
        )
    except Exception:
        pass

    metadata_patch = {
        "async_watcher": {
            "identifier": identifier,
            "status": status,
            "recorded_at": now,
            "delivery_status": str(continuation.get("delivery_status") or ""),
        },
        "final_answer": result_summary if status == "completed" else "",
        "artifact_paths": artifacts,
        "output_path": output_path,
        "output_abs_path": output_abs_path,
        "result_preview": _short_text(result_output, 2000),
        "can_continue": status == "completed",
    }

    candidates = _tracker_workdir_candidates(
        reg=reg,
        issue=issue,
        workdir=workdir,
    )
    if not candidates:
        candidates = [""]
    for candidate_workdir in candidates:
        try:
            updated = SharedTracker.record_result(
                identifier=identifier,
                status=status,
                source="async_watcher",
                result_summary=result_summary,
                error_summary=error_summary,
                workspace_patch=workspace_patch,
                job_ref=job_ref if isinstance(job_ref, dict) else None,
                metadata_patch=metadata_patch,
                workdir=candidate_workdir,
            )
            if updated is not None:
                return
        except Exception:
            continue


def _nested_dict_value(payload: Any, keys: tuple[str, ...]) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _workspace_root_candidates(
    *,
    reg: dict[str, Any],
    issue: dict[str, Any],
) -> list[Path]:
    candidates: list[Path] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        path = Path(text).expanduser()
        if not path.is_absolute():
            registration_root = Path(str(reg.get("workdir") or "")).expanduser()
            if not registration_root.is_absolute():
                return
            path = registration_root / path
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved not in candidates:
            candidates.append(resolved)

    workspace = _workspace_from_issue(issue)
    input_snapshot = (
        workspace.get("input_snapshot")
        if isinstance(workspace.get("input_snapshot"), dict)
        else {}
    )
    for source in (workspace, issue, reg, input_snapshot):
        if not isinstance(source, dict):
            continue
        for key in ("workspace_root", "__workspace_root", "tmcp_workspace_root"):
            _add(source.get(key))

    _add(_nested_dict_value(input_snapshot, ("input_config", "workspace_root")))
    _add(_nested_dict_value(input_snapshot, ("input_config", "__workspace_root")))

    # In live watcher registrations, reg.workdir is the actual workspace root.
    # Recovered records may only have the Symphony job directory, so keep this
    # as a later fallback and only use it if the resulting path exists.
    _add(reg.get("workdir"))
    return candidates


def _resolve_workspace_relative_output_path(
    output_path: Any,
    *,
    reg: dict[str, Any],
    issue: dict[str, Any],
) -> str:
    text = str(output_path or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if path.is_absolute():
        try:
            return path.resolve(strict=False).as_posix()
        except Exception:
            return path.as_posix()

    fallback = ""
    for root in _workspace_root_candidates(reg=reg, issue=issue):
        candidate = (root / path).resolve(strict=False)
        if not fallback:
            fallback = candidate.as_posix()
        if candidate.exists():
            return candidate.as_posix()
    return fallback


def _result_payload_for_summary(
    *,
    reg: dict[str, Any],
    issue: dict[str, Any],
    result_output: str,
) -> tuple[Any, Any, list[str]]:
    collect_payload = _parse_json_object(result_output)
    if isinstance(collect_payload, dict) and not collect_payload.get('isError'):
        # The tracker persists the MCP envelope, while Job Protocol's receipt
        # and actual result live in its structured response.
        for candidate in (collect_payload.get('structuredContent'), _parse_json_object(_content_text(collect_payload))):
            if isinstance(candidate, dict) and candidate.get('job_id') and 'status' in candidate:
                collect_payload = candidate
                break
    result_payload = None
    for path in _candidate_result_files(reg, issue, collect_payload):
        loaded = _read_json_file(path)
        if loaded is not None:
            result_payload = loaded
            break
    if result_payload is None:
        result_payload = collect_payload
    artifacts = _collect_artifact_paths(issue, collect_payload)
    return collect_payload, result_payload, artifacts


def _continuation_status(issue: dict[str, Any], result_payload: Any) -> tuple[str, str]:
    state = str(issue.get("state") or issue.get("status") or "").strip()
    normalized = state.lower()
    if normalized in {"cancelled", "canceled", "terminated"}:
        return "cancelled", f"issue_state={state}"
    result_state = str(_nested_get(result_payload, ("status", "state", "result_status")) or "").strip().lower()
    if normalized not in {"failed", "error"} and result_state in {"cancelled", "canceled", "terminated"}:
        return "cancelled", f"result_status={result_state}"
    reason = _business_failure_reason(result_payload)
    if normalized in {"failed", "error"} or reason:
        return "failed", reason or f"issue_state={state}"
    return "completed", ""


def _completion_result_receipt(collect_payload: Any, result_payload: Any) -> dict[str, Any]:
    """Build a bounded, machine-readable receipt from the collected result.

    ``result_summary`` is presentation data and may be truncated.  Outcome
    validation must therefore use this compact host-generated projection of
    the complete Job Protocol response instead of reparsing display text.
    """

    source = collect_payload if isinstance(collect_payload, dict) else result_payload
    if not isinstance(source, dict):
        return {}
    presentation = source.get("presentation")
    if not isinstance(presentation, dict):
        presentation = {}
    target_result = source.get("target_result")
    if not isinstance(target_result, dict):
        target_result = {}
    output_bundle = source.get("output_bundle")
    if not isinstance(output_bundle, dict):
        structured = target_result.get("structuredContent")
        if isinstance(structured, dict):
            output_bundle = structured.get("output_bundle")
    rejected = source.get("rejected_artifacts")
    rejected_count = len(rejected) if isinstance(rejected, list) else 0
    return {
        "schema": "tcode.async-completion-receipt.v1",
        "job_id": str(source.get("job_id") or presentation.get("job_id") or "").strip(),
        "run_id": str(_nested_get(source, ("run_id",)) or "").strip(),
        "status": str(source.get("status") or "").strip(),
        "validated": source.get("validated") is True,
        "rejected_artifact_count": rejected_count,
        "target_is_error": bool(target_result.get("isError")),
        "bundle_id": (
            str(output_bundle.get("bundle_id") or "").strip()
            if isinstance(output_bundle, dict)
            else ""
        ),
    }


def _build_continuation_payload(
    *,
    identifier: str,
    reg: dict[str, Any],
    issue: dict[str, Any],
    result_output: str,
    additional_artifacts: Optional[list[str]] = None,
    materialized_output_path: str = "",
) -> dict[str, Any]:
    collect_payload, result_payload, artifacts = _result_payload_for_summary(
        reg=reg,
        issue=issue,
        result_output=result_output,
    )
    for item in list(additional_artifacts or []):
        text = str(item or "").strip()
        if text and text not in artifacts:
            artifacts.append(text)
    status, failure_reason = _continuation_status(issue, result_payload)
    target_server = str(reg.get("target_server") or issue.get("tool_server") or "").strip()
    target_tool = str(reg.get("target_tool") or issue.get("tool_name") or "").strip()
    tool_label = ".".join(part for part in (target_server, target_tool) if part) or "异步任务"
    workspace = _workspace_from_issue(issue)
    output_path = str(materialized_output_path or "").strip() or _nested_get(
        result_payload,
        ("output_path", "output_dir", "result_path"),
    )
    if not output_path:
        output_path = workspace.get("output_root") or workspace.get("workdir") or ""
    error_summary = (
        str(issue.get("error_summary") or "").strip()
        or _extract_error_summary(result_payload)
        or _extract_error_summary(collect_payload)
    )
    from .remote_recovery import _failure_detail
    worker_failure = _parse_json_object(_failure_detail(result_payload))
    if isinstance(worker_failure, dict) and worker_failure.get("failure_kind") == "local_wait_timeout":
        error_summary = json.dumps(worker_failure, ensure_ascii=False)
    issue_summary = str(issue.get("result_summary") or "").strip()
    state = str(issue.get("state", "") or "").strip() or status
    result_summary = issue_summary or _extract_summary_text(result_payload, collect_payload)
    content_blocks = tmcp_content_blocks(
        collect_payload,
        session_id=str(reg.get("session_id") or "").strip(),
    )
    presentation_summary = tmcp_summary_text(collect_payload)
    result_receipt = _completion_result_receipt(collect_payload, result_payload)
    failure_record = _parse_json_object(error_summary)
    local_wait_timeout = status == "failed" and isinstance(failure_record, dict) and (
        failure_record.get("failure_kind") == "local_wait_timeout"
        or (failure_record.get("status") == "timed_out"
            and failure_record.get("job_id") and failure_record.get("process_id"))
    )

    lines = [
        f"[Symphony:{identifier}]",
        f"{tool_label} " + ("本地等待已超时，远端计算结果未确认。" if local_wait_timeout else f"异步任务已{'失败' if status == 'failed' else '取消' if status == 'cancelled' else '完成'}。"),
        f"任务编号: `{identifier}`",
        f"任务状态: {state}",
    ]
    materialization = (reg.get("settlement_details") or {}).get("artifact_materialization") or {}
    catalog = workspace.get("artifact_catalog") or materialization.get("remote_catalog")
    if isinstance(catalog, dict):
        lines.append(f"远端产物清单已登记：{catalog.get('total_files', 0)} 个文件，共 {catalog.get('total_size', 0)} 字节；"
                     "文件内容未自动下载。使用 artifact_query 的 list 分页查看，再按当前分析需要 resolve 单个引用；清单不代表文件内容已读取或验证。")
    if output_path:
        lines.append(f"结果目录: `{output_path}`")
        absolute_output_path = _resolve_workspace_relative_output_path(
            output_path,
            reg=reg,
            issue=issue,
        )
    if artifacts:
        preview = ", ".join(f"`{item}`" for item in artifacts[:5])
        suffix = f" 等 {len(artifacts)} 项" if len(artifacts) > 5 else ""
        lines.append(f"相关产物: {preview}{suffix}")
    if status == "failed":
        detail = error_summary or issue_summary or failure_reason or "未捕获到更详细错误，请查看任务日志和 mcp_result.json。"
        lines.append(f"失败原因: {_short_text(detail, 1200)}")
        lines.append("下一步: 先核查原远端任务及已有产物；本地超时不等于远端计算失败或已停止。不得自动重提或假定没有结果。" if local_wait_timeout else "下一步: 你可以回复“重试”让我基于这个任务重新提交，或回复新的参数/文件路径后再继续。")
        result_summary = ""
        error_summary = detail
    elif status == "cancelled":
        lines.append("下一步: 任务已取消。需要重新运行时，回复“重新提交”并给出需要保留或调整的参数。")
        error_summary = error_summary or issue_summary or failure_reason or "任务已取消。"
    else:
        detail = (
            presentation_summary
            or result_summary
            or _nested_get(result_payload, ("summary", "message", "detail"))
        )
        if detail:
            lines.append(f"结果摘要: {_short_text(detail, 900)}")
            if not result_summary:
                result_summary = _short_text(detail, 4000)
        if isinstance(collect_payload, dict) and collect_payload.get('result_type') == 'generic':
            target = collect_payload.get('target_result')
            if isinstance(target, dict) and not target.get('isError'):
                data = target.get('structuredContent') or target.get('data')
                if isinstance(data, dict):
                    lines.append(f"已回传结构化结果: {_short_text(json.dumps(data, ensure_ascii=False), 2000)}")
        lines.append(
            ("下一步: 结果清单已回传，文件内容按需下载。" if isinstance(catalog, dict) else "下一步: 结果已经安全回传到原会话。") + "点击消息卡片上的“继续此任务”后，"
            f"编排器会创建独立的新轮次继续处理；也可以回复“继续异步任务 {identifier}”。"
        )

    return {
        "id": identifier,
        "kind": "async_job_completed",
        "identifier": identifier,
        "target_server": target_server,
        "target_tool": target_tool,
        "status": status,
        "issue_state": state,
        "message": "\n".join(lines),
        "content_blocks": content_blocks,
        "presentation_summary": _short_text(presentation_summary, 4000),
        "result_summary": _short_text(result_summary, 4000),
        "result_receipt": result_receipt,
        "error_summary": _short_text(error_summary, 4000),
        "artifacts": artifacts,
        "failure_reason": failure_reason,
        "can_continue": status == "completed",
        "output_path": str(output_path or "").strip(),
        "output_abs_path": absolute_output_path if output_path else "",
        "created_at": _utc_now_iso(),
        "delivered_at": "",
    }


def _session_is_running(session_id: str) -> bool:
    try:
        from ..server.state import ServerState
        if ServerState.is_running(session_id):
            return True
        from .gateway import run_coordinator
        return bool(run_coordinator.store.is_session_active(session_id))
    except Exception:
        return False


def _continuation_message_exists(messages: list[Any], identifier: str) -> bool:
    marker = f"[Symphony:{identifier}]"
    for msg in messages:
        if marker in str(getattr(msg, "content", "") or ""):
            return True
    return False


def _upsert_pending_continuation(metadata: dict[str, Any], continuation: dict[str, Any]) -> list[dict[str, Any]]:
    pending = metadata.get(_PENDING_CONTINUATIONS_METADATA_KEY)
    items = [dict(item) for item in pending if isinstance(item, dict)] if isinstance(pending, list) else []
    identifier = str(continuation.get("identifier") or continuation.get("id") or "").strip()
    replaced = False
    for index, item in enumerate(items):
        if str(item.get("identifier") or item.get("id") or "").strip() == identifier:
            items[index] = {**item, **continuation}
            replaced = True
            break
    if not replaced:
        items.append(dict(continuation))
    metadata[_PENDING_CONTINUATIONS_METADATA_KEY] = items[-50:]
    return metadata[_PENDING_CONTINUATIONS_METADATA_KEY]


async def _append_or_queue_continuation(
    *,
    session_id: str,
    continuation: dict[str, Any],
) -> str:
    from ..session.manager import SessionManager
    from ..types import Message

    identifier = str(continuation.get("identifier") or continuation.get("id") or "").strip()
    if not identifier:
        return "missing_identifier"

    def _mutate(session: Any) -> str:
        from ..server.session_lifecycle import _SESSION_DELETE_PENDING
        if session_id in _SESSION_DELETE_PENDING:
            return 'session_deleting'
        metadata = dict(getattr(session, "metadata", {}) or {})
        messages = list(getattr(session, "messages", []) or [])
        next_continuation = dict(continuation)

        if _continuation_message_exists(messages, identifier):
            next_continuation["delivered_at"] = (
                next_continuation.get("delivered_at") or _utc_now_iso()
            )
            next_continuation["delivery_status"] = "delivered"
            _upsert_pending_continuation(metadata, next_continuation)
            session.metadata = metadata
            return "already_delivered"

        # Check inside the SessionManager critical section so a watcher never
        # appends between a foreground turn claim and that turn's commit.
        if _session_is_running(session_id):
            next_continuation["delivery_status"] = "pending"
            _upsert_pending_continuation(metadata, next_continuation)
            session.metadata = metadata
            return "queued"

        message = Message(
            role="assistant",
            content=str(next_continuation.get("message") or ""),
            id=f"msg-async-continuation-{identifier}",
            presentation="async_completion",
            content_blocks=list(next_continuation.get("content_blocks") or []),
        )
        messages.append(message)
        next_continuation["message_id"] = message.id
        next_continuation["delivered_at"] = _utc_now_iso()
        next_continuation["delivery_status"] = "delivered"
        _upsert_pending_continuation(metadata, next_continuation)
        session.messages = messages
        session.metadata = metadata
        session.last_active_at = datetime.now().timestamp()
        return "delivered"

    result = await SessionManager.mutate(session_id, _mutate)
    return str(result or "missing_session")


async def _flush_pending_continuations() -> None:
    from ..session.manager import SessionManager
    from ..session.store_sqlite import SessionSQLiteStore

    session_ids = set(_pending_session_ids)
    # A process may restart after persisting an event but before appending its
    # standalone card. Recover pending deliveries from canonical session
    # metadata instead of relying only on the in-memory watcher registry.
    session_ids.update(await asyncio.to_thread(SessionSQLiteStore.list_pending_continuation_ids))
    for session_id in session_ids:
        if _session_is_running(session_id):
            continue
        session = await SessionManager.get(session_id)
        if session is None:
            _pending_session_ids.discard(session_id)
            continue
        pending = dict(getattr(session, "metadata", {}) or {}).get(
            _PENDING_CONTINUATIONS_METADATA_KEY
        )
        if not isinstance(pending, list):
            _pending_session_ids.discard(session_id)
            continue
        still_pending = False
        for item in pending:
            if not isinstance(item, dict):
                continue
            if str(item.get("delivery_status") or "").strip() == "delivered":
                continue
            delivery_status = await _append_or_queue_continuation(
                session_id=session_id,
                continuation=item,
            )
            if delivery_status not in {"delivered", "already_delivered"}:
                still_pending = True
        if not still_pending:
            _pending_session_ids.discard(session_id)


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get("identifier") or issue.get("id") or "").strip()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = issue
    return [by_key[key] for key in order if key in by_key]


def _load_registry_issues(workdir: str = "") -> list[dict[str, Any]]:
    issues = list(_load_issues(workdir))
    loaded_workdirs = {str(workdir or "").strip()}
    for reg in list(_registry.values()):
        reg_workdir = str(reg.get("workdir", "") or "").strip()
        if not reg_workdir or reg_workdir in loaded_workdirs:
            continue
        loaded_workdirs.add(reg_workdir)
        issues.extend(_load_issues(reg_workdir))
    return _dedupe_issues(issues)


def _load_issues(workdir: str = "") -> list[dict[str, Any]]:
    try:
        from .shared_tracker import SharedTracker

        store = SharedTracker.load_issue_store(workdir or None)
        issues = store.get("issues", []) if isinstance(store, dict) else []
        if isinstance(issues, list):
            loaded: list[dict[str, Any]] = []
            for item in issues:
                if not isinstance(item, dict):
                    continue
                copied = dict(item)
                copied["_tracker_workdir"] = str(workdir or "").strip()
                loaded.append(copied)
            return loaded
    except Exception:
        return []


def _workspace_lifecycle_allows_write(
    *,
    reg: dict[str, Any],
    issue: dict[str, Any],
    workdir: str = "",
) -> tuple[bool, str]:
    """Fence late watcher writes after purge or workspace-id reuse."""

    try:
        from ..workspace_lifecycle import managed_workspace_lifecycle_for_path
    except Exception:
        return True, ""

    workspace = _workspace_from_issue(issue)
    expected = 0
    for value in (
        workspace.get("workspace_generation"),
        reg.get("workspace_generation"),
    ):
        try:
            expected = max(expected, int(value or 0))
        except (TypeError, ValueError):
            continue
    server_root = str(os.environ.get("TCODE_WORKDIR", "") or "").strip()
    candidates = (
        issue.get("_tracker_workdir"),
        reg.get("workdir"),
        workdir,
        workspace.get("workspace_root"),
    )
    for candidate in candidates:
        if not str(candidate or "").strip():
            continue
        lifecycle = managed_workspace_lifecycle_for_path(
            candidate,
            server_workdir=server_root or None,
        )
        if lifecycle is None:
            continue
        status = str(lifecycle.get("status") or "active").strip().lower()
        current = max(1, int(lifecycle.get("generation") or 1))
        if status != "active":
            return False, f"workspace is {status}"
        if expected and expected != current:
            return False, (
                f"stale workspace generation: task={expected}, current={current}"
            )
        return True, ""
    return True, ""
