"""Reconcile persisted remote references. Never submit a computation during recovery."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from .shared_tracker import SharedTracker
from .remote_cancel import call_job_tool, cancel_remote_job, job_payload
from .workspace_registry import registry_workspace

TERMINAL = {"completed", "failed", "cancelled", "duplicate"}


def _failure_detail(value, depth=0):
    """Prefer an explicit target error over an outer process-status snapshot."""
    if depth > 6 or not isinstance(value, dict):
        return ""
    for key in ("target_result", "structuredContent", "data", "result", "failure"):
        detail = _failure_detail(value.get(key), depth + 1)
        if detail:
            return detail
    blocks = value.get("content")
    for block in (blocks[:10] if isinstance(blocks, list) else []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and len(text) <= 65536:
                try:
                    detail = _failure_detail(json.loads(text), depth + 1)
                    if detail:
                        return detail
                except ValueError:
                    pass
    if value.get("isError") is True and value.get("failure_kind") == "local_wait_timeout":
        # Keep the Worker classification across persisted collection/recovery;
        # retaining only the message would lose it before desktop delivery.
        return json.dumps({
            "failure_kind": "local_wait_timeout", "remote_outcome": "unknown",
            "error": "本地等待已超时，远端计算是否结束及结果尚未确认；请先核查原任务，不要直接重提。",
        }, ensure_ascii=False)
    keys = ["error", "error_summary"]
    if (value.get("isError") is True or value.get("result_code") is False
            or str(value.get("status") or "").lower() in {"failed", "error"}
            or (value.get("stage") and value.get("code"))):
        keys.append("message")
    for key in keys:
        detail = value.get(key)
        if isinstance(detail, str) and detail.strip():
            from .telemetry import redact_telemetry_text
            return redact_telemetry_text(detail.strip(), limit=1200)
    return ""


def tracker_roots(server_root: str) -> list[str]:
    from ..server.workdirs import workspace_storage_root

    root = Path(server_root).resolve()
    roots = [str(root)]
    # Only registered storage hierarchy; do not traverse remote paths from manifests.
    storage = workspace_storage_root(str(root))
    if storage.is_dir():
        for directory in storage.glob("*/*/*"):
            workspace = directory.resolve()
            if directory.is_dir() and not directory.is_symlink() and workspace.is_relative_to(storage.resolve()):
                roots.append(str(workspace))
    return list(dict.fromkeys(roots))


def lease_expired(value: str, now: float) -> bool:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() <= now
    except (ValueError, TypeError):
        return True


def needs_recovery(task: dict, now: float) -> bool:
    workspace = task.get("workspace") or {}
    if (workspace.get("cancellation") or {}).get("state") == "pending":
        return True
    if task.get("status") == "queued" and ((task.get("job_ref") or {}).get("job_id") or workspace.get("job_id")):
        return True
    if task.get("status") in {"completed", "failed"} and (task.get("job_ref") or {}).get("job_id") and workspace.get("transfer_state") != "ready":
        return True
    return task.get("status") in {"claimed", "running", "retrying"} and lease_expired(task.get("claim_expires_at"), now)


def submission_unknown(now):
    return {"state": "submission_unknown", "next_retry_at": now + 60,
            "error": "未取得远端受理回执，提交状态待核对；不会自动重新提交。"}


async def reconcile_task(task_id: str, workdir: str) -> None:
    task = SharedTracker.get_task(task_id=task_id, workdir=workdir)
    now = time.time()
    if not task or not needs_recovery(task, now):
        return
    task = SharedTracker.claim_recovery(task_id=task_id, now=now, workdir=workdir)
    if not task:
        return
    workspace = task.get("workspace") or {}
    cancellation = dict(workspace.get("cancellation") or {})
    recovery = dict(workspace.get("recovery") or {})
    job_ref = task.get("job_ref") or {}
    status = str(task.get("status") or "")
    patch: dict = {}
    summary = ""
    metadata: dict = {}
    diagnostic_error = ""
    interrupted = False
    try:
        if not job_ref.get("job_id"):
            recovery.update(state="submission_unknown", next_retry_at=now + 60)
            recovery.setdefault("error", "远程引用尚未落盘，需核对原提交；不会自动重新提交。")
            return
        with registry_workspace(workdir):
            if cancellation.get("state") == "pending":
                receipt = await cancel_remote_job(job_ref)
                status = {"Cancelled": "cancelled", "Done": "completed", "Failed": "failed"}[receipt["state"]]
                cancellation.update(state="confirmed" if receipt["cancelled"] else "already_terminal", remote_status=receipt["remote_status"], error="")
                patch["cancellation"] = cancellation
            elif status not in TERMINAL:
                observed = job_payload(await call_job_tool(job_ref, "get_job_status"))
                if str(observed.get("job_id") or "") != str(job_ref["job_id"]) or (job_ref.get("process_id") and str(observed.get("process_id") or "") != str(job_ref["process_id"])):
                    raise ValueError("远程状态回执与任务不匹配")
                if job_ref.get("broker") == "tcode-local" and observed.get("submission_state") == "unknown":
                    recovery.update(submission_unknown(now))
                    return
                remote_status = str(observed.get("status") or "").lower()
                status = {"done": "completed", "completed": "completed", "error": "failed", "failed": "failed", "timed_out": "failed", "terminated": "cancelled", "cancelled": "cancelled", "canceled": "cancelled"}.get(remote_status, "running")
                if remote_status not in {"queued", "running", "pending", "processing", "completed", "done", "failed", "error", "timed_out", "terminated", "cancelled", "canceled"}:
                    raise ValueError("远程任务记录不可用，保留最后已知状态")
                patch["last_observed_at"] = datetime.now(timezone.utc).isoformat()
            if status in {"completed", "failed"}:
                # A failed computation may still return diagnostics. Collection
                # is read-only, tied to its accepted job, and never changes failure to success.
                patch["transfer_state"] = "downloading"
                SharedTracker.record_result(task_id=task_id, status=status, workspace_patch=patch, workdir=workdir, source="recovery")
                raw = json.loads(task.get("result_summary") or "{}") if recovery.get("collected") else await call_job_tool(job_ref, "collect_job_result")
                collected = job_payload(raw)
                if ((status == "failed" or collected.get("job_id"))
                        and str(collected.get("job_id") or "") != str(job_ref["job_id"])):
                    raise ValueError("远程产物与任务不匹配")
                if status == "failed":
                    diagnostic_error = _failure_detail(collected)
                    patch["failure_diagnostics"] = {
                        "source": "job_protocol_collection", "job_id": job_ref["job_id"],
                        "original_error_summary": (workspace.get("failure_diagnostics") or {}).get(
                            "original_error_summary", task.get("error_summary") or ""),
                        "error": diagnostic_error,
                    }
                summary = json.dumps(raw, ensure_ascii=False)
                recovery["collected"] = True
                SharedTracker.record_result(task_id=task_id, status=status, result_summary=summary,
                    workspace_patch={**patch, "recovery": recovery}, workdir=workdir, source="recovery")
                from ..artifact_transport import find_output_bundle, materialize_output_bundle
                if find_output_bundle(raw):
                    from ..server.workdirs import resolve_user_workdir_path, resolve_workspace_context, assert_workspace_active
                    if not all(workspace.get(key) for key in ("tenant_id", "user_id", "workspace_id")):
                        raise ValueError("缺少产物所属工作区信息，暂停保存")
                    server_root = os.environ.get("TCODE_WORKDIR") or workdir
                    assert_workspace_active(server_root, resolve_workspace_context(**{key: workspace[key] for key in ("tenant_id", "user_id", "workspace_id")}))
                    local_root = resolve_user_workdir_path(server_root, workspace["workspace_id"], tenant_id=workspace["tenant_id"], user_id=workspace["user_id"])
                    destination = local_root / "artifacts" / "jobs" / hashlib.sha256(task_id.encode()).hexdigest()[:20]
                    saved = await asyncio.to_thread(materialize_output_bundle, raw, destination, preserve_existing=True, automatic=True)
                    patch.update(output_root=saved["root"], artifacts=[item["path"] for item in saved["files"]])
                    if saved.get("remote_catalog"):
                        patch["artifact_catalog"] = saved["remote_catalog"]
                patch["transfer_state"] = "ready"
            if status in TERMINAL and ((task.get('metadata') or {}).get('async_watcher') or {}).get('sync_status') != 'discarded':
                metadata = {"async_watcher": {**dict((task.get("metadata") or {}).get("async_watcher") or {}), "sync_status": "retry_wait", "retry_count": 0}}
            recovery.update(state="observed", error="", attempt=0, next_retry_at=time.time() + 5)
    except asyncio.CancelledError:
        # Preserve the short lease: after restart it expires without losing intent.
        interrupted = True
        raise
    except Exception:
        attempt = min(int(recovery.get("attempt") or 1), 6)
        # User cancellation remains pending, but should not wait another full
        # minute to be picked up when a previously offline broker comes back.
        max_retry_delay = 10 if cancellation.get("state") == "pending" else 60
        if job_ref.get("broker") == "tcode-local" and recovery.get("state") == "submission_unknown":
            recovery.update(submission_unknown(now))
        else:
            recovery.update(state="retry_wait", error="连接或结果保存未完成，后台将继续核对；不会重新提交计算。", next_retry_at=time.time() + min(max_retry_delay, 2 ** attempt))
        if status in {"completed", "failed"}:
            patch["transfer_state"] = "retry_wait"
    finally:
        if not interrupted:
            recovery["lease_until"] = 0
            patch["recovery"] = recovery
            SharedTracker.record_result(task_id=task_id, status=status, workspace_patch=patch, result_summary=summary,
                error_summary=diagnostic_error, metadata_patch=metadata, workdir=workdir, source="recovery")


async def reconcile_roots(server_root: str) -> list[str]:
    roots = tracker_roots(server_root)
    semaphore = asyncio.Semaphore(4)
    async def one(task_id: str, root: str):
        async with semaphore:
            await reconcile_task(task_id, root)
    pending = []
    for root in roots:
        from ..workflow.manager import WorkflowRunManager
        for run in WorkflowRunManager.list_runs(status="terminated", workdir=root):
            if run.metadata.get("termination_cleanup_pending"):
                try:
                    await reconcile_workflow_termination(run, root)
                except Exception:
                    pass
        offset = 0
        while True:
            tasks = SharedTracker.list_tasks(root, kind="symphony_local_issue", limit=500, offset=offset)
            pending.extend(one(task["task_id"], root) for task in tasks if needs_recovery(task, time.time()))
            if len(tasks) < 500:
                break
            offset += len(tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # Existing workflow executor owns checkpoints and stage CAS. Only resume
    # eligible workflows; never replay a running local stage during discovery.
    from ..server.workflow_api import _sync_workflow_async_waiting_run, _schedule_workflow_auto_continue_for_run
    for root in roots:
        for run in WorkflowRunManager.list_runs(workdir=root):
            if run.metadata.get("terminated") or run.status not in {"queued", "running", "waiting_external_job", "collecting", "validating"}:
                continue
            try:
                with registry_workspace(root):
                    current = _sync_workflow_async_waiting_run(run, root)
                    _schedule_workflow_auto_continue_for_run(current, workdir=root)
            except Exception:
                # Version conflict or missing input remains visible; retry next scan.
                pass
    return roots


async def reconcile_workflow_termination(run, workdir: str) -> None:
    from ..workflow.manager import WorkflowRunManager
    from ..session.manager import SessionManager
    from ..server.state import ServerState

    if not run.metadata.get("terminated"):
        return
    worker_ids = {str(stage.metadata.get("worker_session_id") or "") for stage in run.stage_states} - {"", run.session_id}
    refs = {str(ref.get(key) or "") for stage in run.stage_states for ref in stage.metadata.get("async_tool_refs", []) if isinstance(ref, dict) for key in ("identifier", "issue_identifier", "issue_id", "job_id", "id")} - {""}
    parent = await SessionManager.get(run.session_id)
    errors = []
    for session_id in worker_ids:
        try:
            await ServerState.cancel_session(session_id)
        except Exception:
            errors.append("本地执行尚未停止")
    matches = []
    found_refs = set()
    for root in dict.fromkeys([workdir, os.environ.get("TCODE_WORKDIR") or workdir]):
        offset = 0
        while True:
            tasks = SharedTracker.list_tasks(root, kind="symphony_local_issue", limit=500, offset=offset)
            for task in tasks:
                workspace = task.get("workspace") or {}
                values = {str(task.get("task_id") or ""), str(task.get("identifier") or ""), str((task.get("job_ref") or {}).get("job_id") or "")}
                session_ids = {str(task.get("session_id") or ""), str(task.get("parent_session_id") or ""), str(workspace.get("session_id") or "")}
                if not (values & refs or session_ids & worker_ids):
                    continue
                # Root trackers can contain several owners. Never use a supplied
                # external reference as authorization to cancel someone else's job.
                if parent is None or any(str(workspace.get(key) or "") != str(getattr(parent, key, "") or "") for key in ("tenant_id", "user_id", "workspace_id")):
                    errors.append("关联任务所有权尚待核对")
                    continue
                found_refs.update(values & refs)
                if task["status"] not in TERMINAL or (workspace.get("cancellation") or {}).get("state") == "pending":
                    if (workspace.get("cancellation") or {}).get("state") != "pending":
                        SharedTracker.request_cancellation(task_id=task["task_id"], workdir=root)
                    matches.append((task["task_id"], root))
            if len(tasks) < 500:
                break
            offset += len(tasks)
    # Persisted intents are handled by the recovery loop; no serial 30s waits here.
    pending = bool(matches or errors or (refs - found_refs))
    latest = WorkflowRunManager.get(run.run_id, workdir=workdir)
    if latest and latest.metadata.get("terminated"):
        metadata = {"termination_cleanup_pending": pending, "termination_pending_jobs": len(matches),
                    "termination_cleanup_error": "; ".join(sorted(set(errors))) or ("部分任务引用尚未核对" if refs - found_refs else "")}
        if any(latest.metadata.get(key) != value for key, value in metadata.items()):
            WorkflowRunManager.update_status(run.run_id, status="terminated", metadata=metadata, workdir=workdir, expected_version=latest.version)
