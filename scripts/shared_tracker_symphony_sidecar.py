#!/usr/bin/env python3
"""Project canonical Tracker tasks into Symphony's disposable JSON inbox.

The JSON file is never imported back into SharedTracker. Adapter/runner state
returns through the authenticated Tracker execution-report API.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux deployments provide it
    fcntl = None

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


class TrackerRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0):
        self.status_code = int(status_code or 0)
        super().__init__(message)


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


def _worker_id() -> str:
    return str(os.environ.get("TCODE_SYMPHONY_SIDECAR_ID", "") or "").strip() or f"symphony-sidecar-{uuid.uuid4().hex[:8]}"


def _allowed_servers() -> list[str]:
    raw = str(os.environ.get("TCODE_SYMPHONY_ALLOWED_SERVERS", "") or "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _poll_interval() -> float:
    try:
        return max(1.0, float(os.environ.get("TCODE_SYMPHONY_SIDECAR_POLL_SECONDS", "2")))
    except Exception:
        return 2.0


def _lease_seconds() -> int:
    try:
        return max(15, int(os.environ.get("TCODE_SYMPHONY_SIDECAR_LEASE_SECONDS", "30")))
    except Exception:
        return 30


def _symphony_api_base() -> str:
    return str(os.environ.get("SYMPHONY_API_BASE", "http://127.0.0.1:7890") or "").rstrip("/")


def _issue_store_path() -> Path:
    configured = str(os.environ.get("SYMPHONY_MEMORY_ISSUES_FILE", "") or "").strip()
    if configured:
        return Path(configured)
    root = str(os.environ.get("TCODE_PROJECT_ROOT", TCODE_ROOT.parent.as_posix()) or TCODE_ROOT.parent.as_posix())
    return Path(root) / "tcode" / "tmp" / "symphony_memory_issues.json"


def _load_issue_store() -> dict[str, Any]:
    path = _issue_store_path()
    if not path.exists():
        return {"issues": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            issues = data.get("issues", [])
            if isinstance(issues, list):
                return {"issues": issues}
    except Exception:
        pass
    return {"issues": []}


@contextmanager
def _issue_projection_lock():
    path = _issue_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _save_issue_store(store: dict[str, Any]) -> None:
    path = _issue_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _tracker_status_to_projection_state(status: str) -> str:
    return {
        "queued": "Todo",
        "review_pending": "Todo",
        "claimed": "In Progress",
        "running": "In Progress",
        "retrying": "In Progress",
        "completed": "Done",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "duplicate": "Duplicate",
    }.get(str(status or "").strip().lower(), "Todo")


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
            raise TrackerRequestError(
                str((data or {}).get("detail") or data or f"tracker request failed: {resp.status}"),
                status_code=resp.status,
            )
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
            raise TrackerRequestError(
                str((data or {}).get("detail") or data or f"tracker request failed: {resp.status}"),
                status_code=resp.status,
            )
        return data if isinstance(data, dict) else {}


async def _claim_next(session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
    for scope in _tracker_scopes():
        if _tracker_scope_poll_suspended(scope):
            continue
        data = await _tracker_post(
            session,
            "/api/tracker/claim-next",
            {
                "worker_id": _worker_id(),
                "channel": "symphony",
                "lease_seconds": _lease_seconds(),
                "allowed_servers": _allowed_servers(),
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
    status: str,
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


async def _trigger_refresh() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_symphony_api_base()}/api/v1/refresh",
                json={},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status < 400
    except Exception:
        return False


def _materialize_issue(
    task: dict[str, Any],
    *,
    state_override: str = "",
) -> dict[str, Any]:
    with _issue_projection_lock():
        store = _load_issue_store()
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            issues = []

        task_id = str(task.get("task_id", "") or task.get("id", "") or "").strip()
        identifier = str(task.get("identifier", "") or "").strip()
        existing: Optional[dict[str, Any]] = None
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if str(issue.get("id", "") or "") == task_id or (
                identifier and str(issue.get("identifier", "") or "") == identifier
            ):
                existing = issue
                break

        workspace = (
            dict(task.get("workspace"))
            if isinstance(task.get("workspace"), dict)
            else {}
        )
        job_ref = task.get("job_ref") if isinstance(task.get("job_ref"), dict) else {}
        if job_ref:
            workspace["job_ref"] = dict(job_ref)
        tracker_status = str(
            task.get("tracker_status") or task.get("status") or "queued"
        ).strip().lower()
        issue_record = {
            "id": task_id,
            "identifier": identifier,
            "title": str(task.get("title", "") or ""),
            "description": str(task.get("description", "") or ""),
            # A freshly claimed Tracker task is projected as Todo exactly once
            # so Symphony can consume it. No JSON state is ever imported back.
            "state": str(state_override or "").strip()
            or _tracker_status_to_projection_state(tracker_status),
            "tracker_status": tracker_status,
            "labels": task.get("labels") if isinstance(task.get("labels"), list) else ["local", "symphony"],
            "created_at": str(task.get("created_at", "") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace": workspace,
            "result_summary": str(task.get("result_summary", "") or ""),
            "error_summary": str(task.get("error_summary", "") or ""),
            "fingerprint": str(task.get("fingerprint", "") or ""),
            "resume_data": task.get("resume_data") if isinstance(task.get("resume_data"), dict) else {},
            "_tracker_projection": {
                "source": "shared_tracker.sqlite",
                "authoritative": False,
                "version": 1,
                "workspace_generation": int(workspace.get("workspace_generation") or 1),
            },
        }

        if existing is not None:
            for index, issue in enumerate(issues):
                if issue is existing:
                    issues[index] = issue_record
                    break
        else:
            issues.append(issue_record)
        store["issues"] = issues
        store["_projection"] = {
            "source": "shared_tracker.sqlite",
            "authoritative": False,
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _save_issue_store(store)
        return issue_record


async def _run_claimed_task(session: aiohttp.ClientSession, task: dict[str, Any]) -> None:
    task_id = str(task.get("task_id", "") or task.get("id", "") or "").strip()
    claim_token = str(task.get("claim_token", "") or "").strip()
    scope = task.get("_tracker_scope") if isinstance(task.get("_tracker_scope"), dict) else None
    if not task_id or not claim_token:
        return

    _materialize_issue(task, state_override="Todo")
    await _trigger_refresh()

    while True:
        try:
            latest = await _get_task(session, task_id, scope)
        except TrackerRequestError as exc:
            if not 400 <= exc.status_code < 500:
                raise
            terminal_status = (
                "cancelled" if exc.status_code in {409, 410} else "failed"
            )
            terminal_projection = {
                **task,
                "status": terminal_status,
                "tracker_status": terminal_status,
                "error_summary": f"tracker rejected execution: {exc}",
            }
            _materialize_issue(terminal_projection)
            await _trigger_refresh()
            return
        tracker_status = str(
            latest.get("tracker_status") or latest.get("status") or ""
        ).strip().lower()
        if tracker_status in {"completed", "failed", "cancelled", "duplicate"}:
            _materialize_issue(latest)
            await _trigger_refresh()
            return
        try:
            await _heartbeat(
                session,
                task_id,
                claim_token,
                status="running",
                scope=scope,
            )
        except TrackerRequestError as exc:
            if not 400 <= exc.status_code < 500:
                raise
            terminal_status = (
                "cancelled" if exc.status_code in {409, 410} else "failed"
            )
            terminal_projection = {
                **task,
                "status": terminal_status,
                "tracker_status": terminal_status,
                "error_summary": f"tracker rejected execution: {exc}",
            }
            _materialize_issue(terminal_projection)
            await _trigger_refresh()
            return
        await asyncio.sleep(_poll_interval())


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                task = await _claim_next(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[symphony-sidecar] loop error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(_poll_interval())
                continue
            if not task:
                await asyncio.sleep(_poll_interval())
                continue
            try:
                await _run_claimed_task(session, task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[symphony-sidecar] task error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(_poll_interval())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
