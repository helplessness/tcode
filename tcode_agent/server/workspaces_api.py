"""Workspace creation, soft deletion, purge and recovery.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import os
import shutil
import signal
import stat
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ..mcp.manager_auto import MCPManager
from ..research import (
    ResearchDeadLetterReplayer,
    ResearchEventStore,
    load_research_settings,
)
from ..runtime.sandbox import get_sandbox_runtime, load_sandbox_config, sandbox_enabled
from ..runtime.shared_tracker import SharedTracker
from ..runtime.workspace_registry import registry_workspace
from ..scheduled_tasks import get_scheduled_task_service
from ..session.manager import SessionManager
from ..team.registry import WorkerRegistry
from . import workdirs as workdir_utils
from ._shared import (
    _cancel_symphony_issues_for_session,
    _terminate_session_processes,
)
from .workspace_scope import (
    _create_user_workdir,
    _list_user_workdirs,
    _session_owner_kwargs,
    _session_owner_kwargs_from_session,
    _user_workdirs_root,
    _workdir_template_root,
    _workspace_context_from_request,
)
from .kanban import KanbanHiddenStore
from .schemas import WorkdirCreateRequest
from .state import ServerState
from .wiki_shares import WikiShareStore

router = APIRouter()


_WORKSPACE_DELETE_LOCKS: weakref.WeakValueDictionary[
    tuple[str, str, str], asyncio.Lock
] = weakref.WeakValueDictionary()


_WORKSPACE_PURGE_TASKS: dict[tuple[str, str, str], asyncio.Task] = {}


_WORKSPACE_PURGE_RESULTS: dict[tuple[str, str, str], dict[str, Any]] = {}


def _cleanup_error_message(error: object) -> str:
    text = str(error)
    if "MCP server 'job-protocol-mcp' not found" in text:
        return "工作区仍有未确认停止的远程任务，但任务服务当前未连接。请在设置中检查 Job Protocol 连接后重试清理；为保护数据，文件尚未永久删除。"
    return text


@router.get("/api/workdirs")
async def list_workdirs(request: Request):
    workspace_context = _workspace_context_from_request(request)
    workdirs = _list_user_workdirs(workspace_context)
    max_per_user = workdir_utils.max_workspaces_per_user()
    return {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": workspace_context.workspace_id,
        "root": _user_workdirs_root(workspace_context).as_posix(),
        "template_root": _workdir_template_root().as_posix(),
        "max_workspaces_per_user": max_per_user,
        "remaining_workspace_slots": max(0, max_per_user - len(workdirs)),
        "workdirs": workdirs,
    }


@router.post("/api/workdirs")
async def create_workdir(payload: WorkdirCreateRequest, request: Request):
    workspace_context = _workspace_context_from_request(
        request,
        workdir_name=payload.name,
    )
    created = _create_user_workdir(payload.name, workspace_context)
    return {
        "status": "created",
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workdir": created,
    }


async def _quiesce_workspace_sessions(
    workspace_context: workdir_utils.WorkspaceContext,
) -> dict[str, Any]:
    owner = _session_owner_kwargs(workspace_context, include_workspace=True)
    server_workdir = ServerState.get_workdir()
    persisted_sessions = SessionManager.get_all_sync()
    matched_sessions = {
        session.id: session
        for session in persisted_sessions.values()
        if _record_targets_workspace(
            session,
            workspace_context,
            server_workdir=server_workdir,
        )
    }
    session_ids = set(matched_sessions)
    session_ids.update(
        session_id
        for session_id, runtime in list(ServerState.sessions.items())
        if _record_targets_workspace(
            runtime,
            workspace_context,
            server_workdir=server_workdir,
        )
    )
    warnings: dict[str, str] = {}
    for session_id in sorted(session_ids):
        try:
            await ServerState.cancel_session(session_id, clear_guidance=True)
            persisted = matched_sessions.get(session_id)
            await _terminate_session_processes(
                session_id,
                owner_kwargs=(
                    _session_owner_kwargs_from_session(persisted)
                    if persisted is not None
                    else owner
                ),
            )
            await ServerState.cleanup_session(session_id)
        except Exception as exc:
            warnings[session_id] = str(exc)
    return {
        "quiesced_session_ids": sorted(session_ids),
        "warnings": warnings,
    }


def _workspace_owner_key(
    workspace_context: workdir_utils.WorkspaceContext,
) -> tuple[str, str, str]:
    return (
        workspace_context.tenant_id,
        workspace_context.user_id,
        workspace_context.workspace_id,
    )


def _record_value(record: Any, name: str) -> str:
    if isinstance(record, dict):
        value = record.get(name)
        metadata = record.get("metadata")
    else:
        value = getattr(record, name, None)
        metadata = getattr(record, "metadata", None)
    if value not in (None, ""):
        return str(value).strip()
    if isinstance(metadata, dict):
        return str(metadata.get(name) or "").strip()
    return ""


def _record_owner_key(record: Any) -> tuple[str, str, str]:
    return tuple(
        _record_value(record, name)
        for name in ("tenant_id", "user_id", "workspace_id")
    )


def _record_targets_workspace(
    record: Any,
    workspace_context: workdir_utils.WorkspaceContext,
    *,
    server_workdir: str,
) -> bool:
    """Match both current owner metadata and legacy path-encoded ownership."""

    actual_context = workdir_utils.workspace_context_from_managed_path(
        server_workdir,
        _record_value(record, "workdir"),
    )
    if actual_context is not None:
        return actual_context == workspace_context
    return _record_owner_key(record) == _workspace_owner_key(workspace_context)


def _record_targets_hidden_workspace(
    record: Any,
    *,
    server_workdir: str,
    visibility_cache: Optional[dict[workdir_utils.WorkspaceContext, bool]] = None,
) -> bool:
    """Treat a managed workdir as authoritative over stale owner metadata."""

    actual_context = workdir_utils.workspace_context_from_managed_path(
        server_workdir,
        _record_value(record, "workdir"),
    )
    if actual_context is None:
        tenant_id, user_id, workspace_id = _record_owner_key(record)
        if not all((tenant_id, user_id, workspace_id)):
            return False
        actual_context = workdir_utils.resolve_workspace_context(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    if visibility_cache is not None and actual_context in visibility_cache:
        return visibility_cache[actual_context]
    hidden = workdir_utils.workspace_is_hidden(server_workdir, actual_context)
    if visibility_cache is not None:
        visibility_cache[actual_context] = hidden
    return hidden


def _move_workspace_to_purge_quarantine(
    target: Path,
    quarantine: Path,
) -> bool:
    """Move files atomically before central records are removed."""

    quarantine.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if quarantine.exists():
            # A previous purge may have moved the original tree and then a
            # late watcher/worker recreated the visible workspace before the
            # operation failed.  Permanent deletion is already authorized, so
            # both trees are purge residue.  Keep them in place until central
            # records are removed, then delete and verify both below.
            return True
        os.replace(target, quarantine)
        return True
    return quarantine.exists()


def _make_purge_tree_owner_writable(root: Path) -> None:
    """Restore owner permissions needed to remove an isolated workspace tree.

    Protein Run inputs and other immutable snapshots may intentionally use
    0555 directories and 0444 files.  Once the workspace has been moved into
    purge quarantine, those protections are no longer useful and prevent
    ``shutil.rmtree`` from unlinking children.  Never follow symlinks while
    normalizing permissions: linked resources must be unlinked, not changed.
    """

    if root.is_symlink():
        raise RuntimeError("workspace purge quarantine must not be a symlink")

    def make_writable(path: Path, *, directory: bool) -> None:
        try:
            current_mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(current_mode):
            return
        required = stat.S_IRUSR | stat.S_IWUSR
        if directory:
            required |= stat.S_IXUSR
        mode = stat.S_IMODE(current_mode) | required
        chmod_kwargs = (
            {"follow_symlinks": False}
            if os.chmod in os.supports_follow_symlinks
            else {}
        )
        os.chmod(path, mode, **chmod_kwargs)

    make_writable(root, directory=True)

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current_root, directories, files in os.walk(
        root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        current = Path(current_root)
        make_writable(current, directory=True)
        for name in directories:
            make_writable(current / name, directory=True)
        for name in files:
            make_writable(current / name, directory=False)


def _workspace_tracker_store_path(
    target: Path,
    quarantine: Path,
) -> str:
    if target.exists():
        return target.as_posix()
    if quarantine.exists():
        return quarantine.as_posix()
    return ""


async def _terminate_workspace_tracker_jobs(
    workspace_context: workdir_utils.WorkspaceContext,
    *,
    target: Path,
    quarantine: Path,
) -> dict[str, Any]:
    """Revoke Tracker leases and terminate known Job Protocol jobs."""

    tracker_workdir = _workspace_tracker_store_path(target, quarantine)
    if not tracker_workdir:
        return {
            "tasks_cancelled": 0,
            "jobs_terminated": 0,
            "job_refs": 0,
            "processes_signalled": 0,
        }
    try:
        tasks = await asyncio.to_thread(
            SharedTracker.list_tasks,
            tracker_workdir,
            kind="symphony_local_issue",
            include_deleted=True,
            limit=10000,
        )
    except Exception:
        tasks = []

    owner = {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": workspace_context.workspace_id,
    }
    job_refs: list[dict[str, Any]] = []
    cancelled = 0
    seen_jobs: set[tuple[str, str]] = set()
    job_tasks: dict[tuple[str, str], list[str]] = {}
    execution_pids: set[int] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        if any(
            str(workspace.get(key) or "").strip() != expected
            for key, expected in owner.items()
        ):
            continue
        identifier = str(task.get("identifier") or "").strip()
        if identifier:
            try:
                from ..runtime.async_watcher import unregister

                unregister(identifier)
            except Exception:
                pass
        job_ref = task.get("job_ref") if isinstance(task.get("job_ref"), dict) else {}
        status = str(task.get("status") or "").strip().lower()
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        purge = metadata.get("workspace_purge") if isinstance(metadata.get("workspace_purge"), dict) else {}
        # A local purge cancellation is not a remote termination receipt.
        pending_termination = status == "cancelled" and bool(purge) and not purge.get("remote_terminated")
        if status in {"completed", "failed", "cancelled", "duplicate"} and not pending_termination:
            continue
        job_key = (
            str(job_ref.get("job_id") or "").strip(),
            str(job_ref.get("process_id") or "").strip(),
        )
        if all(job_key) and job_key not in seen_jobs:
            seen_jobs.add(job_key)
            job_refs.append(dict(job_ref))
        if all(job_key):
            job_tasks.setdefault(job_key, []).append(str(task.get("task_id") or task.get("id") or ""))
        if pending_termination:
            continue
        try:
            execution_pid = int(metadata.get("execution_pid") or 0)
        except (TypeError, ValueError):
            execution_pid = 0
        if execution_pid > 1:
            execution_pids.add(execution_pid)
        updated = await asyncio.to_thread(
            SharedTracker.record_result,
            task_id=str(task.get("task_id") or task.get("id") or ""),
            status="cancelled",
            source="workspace_purge",
            result_summary="cancelled by workspace purge generation fence",
            metadata_patch={
                "workspace_purge": {
                    "generation": workdir_utils.workspace_generation(
                        ServerState.get_workdir(), workspace_context
                    ),
                    "cancelled_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            workdir=tracker_workdir,
        )
        if updated is not None:
            cancelled += 1

    processes_signalled = 0
    for execution_pid in execution_pids:
        try:
            os.killpg(execution_pid, signal.SIGTERM)
            processes_signalled += 1
        except ProcessLookupError:
            continue
        except (OSError, PermissionError):
            continue

    terminated = 0
    # Gateway connections belong to the backend service scope, not whichever
    # chat workspace happened to issue the deletion request.
    with registry_workspace(ServerState.get_workdir()):
        if job_refs and not MCPManager.is_initialized():
            await MCPManager.initialize(ServerState.get_workdir())
        for job_ref in job_refs:
            response = await MCPManager.call_tool(
                "job-protocol-mcp",
                "terminate_job",
                {
                    "input": {
                        "job_id": str(job_ref.get("job_id") or ""),
                        "process_id": str(job_ref.get("process_id") or ""),
                        "job_token": str(job_ref.get("job_token") or ""),
                        **dict(job_ref.get("request_scope") or {}),
                    }
                },
            )
            if isinstance(response, dict) and response.get("isError") is True:
                raise RuntimeError(
                    "job-protocol terminate_job failed for "
                    f"{job_ref.get('job_id')}: {response}"
                )
            for task_id in job_tasks[(str(job_ref['job_id']).strip(), str(job_ref['process_id']).strip())]:
                recorded = await asyncio.to_thread(SharedTracker.get_task, task_id=task_id, workdir=tracker_workdir)
                purge_metadata = dict(((recorded or {}).get('metadata') or {}).get('workspace_purge') or {})
                await asyncio.to_thread(
                    SharedTracker.record_result, task_id=task_id, status="cancelled", source="workspace_purge",
                    metadata_patch={"workspace_purge": {**purge_metadata, "remote_terminated": True}}, workdir=tracker_workdir,
                )
            terminated += 1
    return {
        "tasks_cancelled": cancelled,
        "jobs_terminated": terminated,
        "job_refs": len(job_refs),
        "processes_signalled": processes_signalled,
    }


async def _purge_workspace_data(
    workspace_context: workdir_utils.WorkspaceContext,
) -> dict[str, Any]:
    server_workdir = ServerState.get_workdir()
    owner = _session_owner_kwargs(workspace_context, include_workspace=True)
    key = _workspace_owner_key(workspace_context)
    target = workdir_utils.resolve_user_workdir_path(
        server_workdir,
        workspace_context.workspace_id,
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
    )
    quarantine = workdir_utils.workspace_purge_path(server_workdir, workspace_context)
    lock = _WORKSPACE_DELETE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        workdir_utils.set_workspace_state(
            server_workdir,
            workspace_context,
            "purging",
            purge_stage="fencing",
        )
        try:
            tracker_job_cleanup = await _terminate_workspace_tracker_jobs(
                workspace_context,
                target=target,
                quarantine=quarantine,
            )
            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="quiescing_sessions",
            )
            session_rows = [
                session
                for session in await SessionManager.list()
                if _record_targets_workspace(
                    session,
                    workspace_context,
                    server_workdir=server_workdir,
                )
            ]
            sessions = await _quiesce_workspace_sessions(workspace_context)
            for session in session_rows:
                await _cancel_symphony_issues_for_session(session.id)

            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="sandbox_cleanup",
            )
            sandbox_cleanup: dict[str, Any] = {"status": "disabled"}
            if sandbox_enabled():
                sandbox_config = load_sandbox_config()
                try:
                    sandbox_cleanup = await get_sandbox_runtime(
                        sandbox_config
                    ).cleanup_workspace(
                        tenant_id=workspace_context.tenant_id,
                        user_id=workspace_context.user_id,
                        workspace_id=workspace_context.workspace_id,
                        purge_data=True,
                    )
                except Exception as sandbox_exc:
                    # The remote runner is optional unless require_runner=true.
                    # Its missing token or temporary unavailability must not
                    # leave local Workspace metadata permanently undeletable.
                    # Keep the warning in the terminal tombstone so operators
                    # can see that remote cleanup was not confirmed.
                    if sandbox_config.require_runner or not sandbox_config.runner_url:
                        raise
                    sandbox_cleanup = {
                        "status": "skipped_unavailable",
                        "cleanup_pending": True,
                        "warning": str(sandbox_exc)[:2000],
                    }
                if (
                    sandbox_cleanup.get("status") != "skipped_unavailable"
                    and sandbox_cleanup.get("runtime_data_purged") is not True
                ):
                    raise RuntimeError(
                        "sandbox runner does not confirm purge_data support; upgrade runner before retrying"
                    )
            if sessions["warnings"]:
                raise RuntimeError(
                    "; ".join(
                        f"session {session_id}: {error}"
                        for session_id, error in sessions["warnings"].items()
                    )
                )

            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="workspace_data_cleanup",
            )
            research_deleted: dict[str, Any] = {
                "records": {},
                "removed_paths": [],
                "dead_letters": 0,
            }
            research_workdir = target if target.exists() else quarantine
            research_settings = load_research_settings()
            research_db = research_settings.database_path(
                research_workdir.as_posix()
            )
            if research_db.exists():
                research_deleted = await asyncio.to_thread(
                    ResearchEventStore.delete_workspace_data,
                    tenant_id=workspace_context.tenant_id,
                    user_id=workspace_context.user_id,
                    workspace_id=workspace_context.workspace_id,
                    workdir=research_workdir.as_posix(),
                    settings=research_settings,
                )
            dead_letter_path = research_settings.dead_letter_path(
                research_workdir.as_posix()
            )
            if dead_letter_path.exists():
                research_deleted["dead_letters"] = await asyncio.to_thread(
                    ResearchDeadLetterReplayer.delete_workspace_data,
                    tenant_id=workspace_context.tenant_id,
                    user_id=workspace_context.user_id,
                    workspace_id=workspace_context.workspace_id,
                    workdir=research_workdir.as_posix(),
                    settings=research_settings,
                )

            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="quarantine",
            )
            files_quarantined = await asyncio.to_thread(
                _move_workspace_to_purge_quarantine,
                target,
                quarantine,
            )

            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="metadata_cleanup",
            )
            for session in session_rows:
                await WorkerRegistry.delete(session.id)
            # 必须为每条根/子 Session 的 canonical event log 写入墓碑。
            # 仅清 SQLite 会在重启重放快照时复活；按实际 workdir 收集 ID
            # 还能覆盖旧版本写错 owner metadata 的历史 Session。
            await SessionManager.clear(
                session_ids=[session.id for session in session_rows]
            )

            scheduled_deleted = await asyncio.to_thread(
                get_scheduled_task_service().store.delete_workspace_data,
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=workspace_context.workspace_id,
            )
            tracker_deleted = await asyncio.to_thread(
                SharedTracker.delete_workspace_data,
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=workspace_context.workspace_id,
                workdir=server_workdir,
            )
            hidden_kanban_deleted = await asyncio.to_thread(
                KanbanHiddenStore.delete_workspace_data,
                server_workdir,
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=workspace_context.workspace_id,
            )
            wiki_shares_deleted = await asyncio.to_thread(
                WikiShareStore(server_workdir).delete_workspace_data,
                tenant_id=workspace_context.tenant_id,
                owner_user_id=workspace_context.user_id,
                workspace_id=workspace_context.workspace_id,
            )

            workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purging",
                purge_stage="delete_files",
            )
            # Delete both the atomic quarantine and any visible workspace
            # recreated by a late background writer.  This also makes retrying
            # a partially completed purge idempotent.
            for purge_tree in (quarantine, target):
                if not purge_tree.exists():
                    continue
                await asyncio.to_thread(
                    _make_purge_tree_owner_writable,
                    purge_tree,
                )
                await asyncio.to_thread(shutil.rmtree, purge_tree)
            remaining = [
                path.as_posix()
                for path in (target, quarantine)
                if path.exists()
            ]
            if remaining:
                raise RuntimeError(
                    "workspace purge left residual paths: " + ", ".join(remaining)
                )
            state = workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purged",
                purge_stage="complete",
            )
            result = {
                "status": "purged",
                "tenant_id": workspace_context.tenant_id,
                "user_id": workspace_context.user_id,
                "workspace_id": workspace_context.workspace_id,
                "files_quarantined": files_quarantined,
                "sessions_deleted": len(session_rows),
                "scheduled_records_deleted": sum(scheduled_deleted.values()),
                "tracker_records_deleted": sum(tracker_deleted.values()),
                "tracker_job_cleanup": tracker_job_cleanup,
                "hidden_kanban_records_deleted": hidden_kanban_deleted,
                "wiki_share_records_deleted": sum(wiki_shares_deleted.values()),
                "research_records_deleted": sum(
                    dict(research_deleted.get("records") or {}).values()
                ),
                "research_dead_letters_deleted": int(
                    research_deleted.get("dead_letters") or 0
                ),
                "research_paths_deleted": list(
                    research_deleted.get("removed_paths") or []
                ),
                "sandbox": sandbox_cleanup,
                "workspace_state": state,
            }
            _WORKSPACE_PURGE_RESULTS[key] = result
            return result
        except Exception as exc:
            failed_state = workdir_utils.workspace_state(
                server_workdir, workspace_context
            ) or {}
            state = workdir_utils.set_workspace_state(
                server_workdir,
                workspace_context,
                "purge_failed",
                last_error=_cleanup_error_message(exc),
                purge_stage=str(failed_state.get("purge_stage") or "failed"),
            )
            _WORKSPACE_PURGE_RESULTS[key] = {
                "status": "purge_failed",
                "tenant_id": workspace_context.tenant_id,
                "user_id": workspace_context.user_id,
                "workspace_id": workspace_context.workspace_id,
                "error": _cleanup_error_message(exc),
                "workspace_state": state,
            }
            return _WORKSPACE_PURGE_RESULTS[key]


def _workspace_purge_done(
    key: tuple[str, str, str],
):
    def _done(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _WORKSPACE_PURGE_RESULTS[key] = {
                "status": "purge_failed",
                "error": str(exc),
            }
        finally:
            if _WORKSPACE_PURGE_TASKS.get(key) is task:
                _WORKSPACE_PURGE_TASKS.pop(key, None)

    return _done


def _resume_interrupted_workspace_purges() -> int:
    """Resume durable purge state machines left in ``purging`` on restart."""

    server_workdir = ServerState.get_workdir()
    resumed = 0
    for row in workdir_utils.list_all_workspace_states(
        server_workdir,
        statuses={"purging"},
    ):
        context = workdir_utils.WorkspaceContext(
            tenant_id=str(row.get("tenant_id") or ""),
            user_id=str(row.get("user_id") or ""),
            workspace_id=str(row.get("workspace_id") or ""),
        )
        key = _workspace_owner_key(context)
        existing = _WORKSPACE_PURGE_TASKS.get(key)
        if existing is not None and not existing.done():
            continue
        task = asyncio.create_task(
            _purge_workspace_data(context),
            name=(
                "workspace-purge-recovery:"
                f"{context.tenant_id}:{context.user_id}:{context.workspace_id}"
            ),
        )
        _WORKSPACE_PURGE_TASKS[key] = task
        task.add_done_callback(_workspace_purge_done(key))
        resumed += 1
    return resumed


@router.delete("/api/workdirs/{workdir_name}")
async def delete_workdir(workdir_name: str, request: Request):
    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=workdir_name,
        workdir_name=workdir_name,
    )
    target = workdir_utils.resolve_user_workdir_path(
        ServerState.get_workdir(),
        workdir_name,
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
    )
    if not target.is_dir():
        raise HTTPException(404, "workdir not found")

    key = (
        workspace_context.tenant_id,
        workspace_context.user_id,
        workspace_context.workspace_id,
    )
    lock = _WORKSPACE_DELETE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        workdir_utils.set_workspace_state(
            ServerState.get_workdir(),
            workspace_context,
            "deleting",
        )
        try:
            sessions = await _quiesce_workspace_sessions(workspace_context)
            sandbox_cleanup: dict[str, Any] = {"status": "disabled"}
            if sandbox_enabled():
                sandbox_config = load_sandbox_config()
                try:
                    sandbox_cleanup = await get_sandbox_runtime(
                        sandbox_config
                    ).cleanup_workspace(
                        tenant_id=workspace_context.tenant_id,
                        user_id=workspace_context.user_id,
                        workspace_id=workspace_context.workspace_id,
                    )
                except Exception as sandbox_exc:
                    # A configured remote runner is optional unless
                    # require_runner=true.  Missing local credentials or an
                    # unavailable runner must not make an otherwise quiescent
                    # local Workspace impossible to move to the recycle bin.
                    # Permanent purge remains strict and will retry runner data
                    # cleanup before deleting files.
                    if sandbox_config.require_runner or not sandbox_config.runner_url:
                        raise
                    sandbox_cleanup = {
                        "status": "skipped_unavailable",
                        "cleanup_pending": True,
                        "warning": str(sandbox_exc)[:2000],
                    }
            if sessions["warnings"]:
                raise RuntimeError(
                    "; ".join(
                        f"session {session_id}: {error}"
                        for session_id, error in sessions["warnings"].items()
                    )
                )
            state = workdir_utils.set_workspace_state(
                ServerState.get_workdir(),
                workspace_context,
                "deleted",
            )
        except Exception as exc:
            rollback_error = ""
            if sandbox_enabled():
                try:
                    await get_sandbox_runtime().restore_workspace(
                        tenant_id=workspace_context.tenant_id,
                        user_id=workspace_context.user_id,
                        workspace_id=workspace_context.workspace_id,
                    )
                except Exception as restore_exc:
                    rollback_error = str(restore_exc)
            detail = str(exc)
            if rollback_error:
                detail = f"{detail}; sandbox rollback failed: {rollback_error}"
            workdir_utils.set_workspace_state(
                ServerState.get_workdir(),
                workspace_context,
                "deleting" if rollback_error else "active",
                last_error=detail,
            )
            raise HTTPException(409, f"workspace cleanup failed: {detail}") from exc

    return {
        "status": "deleted",
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": workspace_context.workspace_id,
        "files_preserved": True,
        "sessions": sessions,
        "sandbox": sandbox_cleanup,
        "workspace_state": state,
    }


@router.get("/api/workdirs/deleted")
async def list_deleted_workdirs(request: Request):
    workspace_context = _workspace_context_from_request(request)
    states = workdir_utils.list_workspace_states(
        ServerState.get_workdir(),
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
        statuses={"deleting", "deleted", "purging", "purge_failed", "purged"},
    )
    items: list[dict[str, Any]] = []
    for state in states:
        context = workdir_utils.WorkspaceContext(
            tenant_id=workspace_context.tenant_id,
            user_id=workspace_context.user_id,
            workspace_id=str(state.get("workspace_id") or ""),
        )
        target = workdir_utils.resolve_user_workdir_path(
            ServerState.get_workdir(),
            context.workspace_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        quarantine = workdir_utils.workspace_purge_path(
            ServerState.get_workdir(), context
        )
        files_present = target.exists() or quarantine.exists()
        # Normally a purged record is terminal and should stay out of the
        # recycle bin.  Surface it only when files reappeared or cleanup left
        # residue, otherwise the user has no way to inspect or remove it.
        if str(state.get("status") or "") == "purged" and not files_present:
            continue
        items.append(
            {
                **state,
                "last_error": _cleanup_error_message(state.get("last_error") or ""),
                "files_present": files_present,
                "residual_files": (
                    str(state.get("status") or "") == "purged"
                    and files_present
                ),
                "purge_running": _workspace_owner_key(context)
                in _WORKSPACE_PURGE_TASKS,
            }
        )
    return {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workdirs": items,
    }


@router.post("/api/workdirs/{workdir_name}/purge", status_code=202)
async def purge_workdir(
    workdir_name: str,
    payload: dict[str, Any],
    request: Request,
):
    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=workdir_name,
        workdir_name=workdir_name,
    )
    if str(payload.get("confirm_workspace_id") or "") != workdir_name:
        raise HTTPException(
            400,
            "confirm_workspace_id must exactly match workspace_id",
        )
    state = workdir_utils.workspace_state(
        ServerState.get_workdir(), workspace_context
    ) or {}
    status = str(state.get("status") or "active")
    if status == "active":
        raise HTTPException(409, "soft-delete workspace before permanent deletion")
    if status in {"deleting"}:
        raise HTTPException(409, f"workspace is {status}; retry after it becomes deleted")
    if status == "purged":
        target = workdir_utils.resolve_user_workdir_path(
            ServerState.get_workdir(),
            workspace_context.workspace_id,
            tenant_id=workspace_context.tenant_id,
            user_id=workspace_context.user_id,
        )
        quarantine = workdir_utils.workspace_purge_path(
            ServerState.get_workdir(), workspace_context
        )
        if not target.exists() and not quarantine.exists():
            return {
                "status": "purged",
                "workspace_id": workspace_context.workspace_id,
                "workspace_state": state,
            }
    if status not in {"deleted", "purging", "purge_failed", "purged"}:
        raise HTTPException(409, f"workspace cannot be purged from status {status}")

    key = _workspace_owner_key(workspace_context)
    existing = _WORKSPACE_PURGE_TASKS.get(key)
    if existing is not None and not existing.done():
        return {
            "status": "purging",
            "workspace_id": workspace_context.workspace_id,
            "workspace_state": state,
        }
    task = asyncio.create_task(
        _purge_workspace_data(workspace_context),
        name=(
            "workspace-purge:"
            f"{workspace_context.tenant_id}:"
            f"{workspace_context.user_id}:"
            f"{workspace_context.workspace_id}"
        ),
    )
    _WORKSPACE_PURGE_TASKS[key] = task
    task.add_done_callback(_workspace_purge_done(key))
    return {
        "status": "purging",
        "workspace_id": workspace_context.workspace_id,
        "workspace_state": workdir_utils.set_workspace_state(
            ServerState.get_workdir(), workspace_context, "purging"
        ),
    }


@router.get("/api/workdirs/{workdir_name}/purge")
async def workspace_purge_status(workdir_name: str, request: Request):
    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=workdir_name,
        workdir_name=workdir_name,
    )
    state = workdir_utils.workspace_state(
        ServerState.get_workdir(), workspace_context
    )
    if state is None:
        raise HTTPException(404, "workspace state not found")
    key = _workspace_owner_key(workspace_context)
    result = dict(_WORKSPACE_PURGE_RESULTS.get(key) or {})
    return {
        "status": str(state.get("status") or ""),
        "workspace_id": workspace_context.workspace_id,
        "running": key in _WORKSPACE_PURGE_TASKS,
        "error": _cleanup_error_message(state.get("last_error") or ""),
        "workspace_state": state,
        "result": result,
    }


@router.post("/api/workdirs/{workdir_name}/restore")
async def restore_workdir(workdir_name: str, request: Request):
    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=workdir_name,
        workdir_name=workdir_name,
    )
    target = workdir_utils.resolve_user_workdir_path(
        ServerState.get_workdir(),
        workdir_name,
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
    )
    current_state = workdir_utils.workspace_state(
        ServerState.get_workdir(), workspace_context
    ) or {}
    current_status = str(current_state.get("status") or "active")
    if current_status == "purging":
        raise HTTPException(409, "workspace permanent deletion is running")
    if current_status == "purge_failed":
        raise HTTPException(409, "workspace permanent deletion failed; retry purge")
    if current_status == "purged":
        raise HTTPException(410, "workspace files were permanently deleted")
    if not target.is_dir():
        raise HTTPException(404, "workdir files not found")
    workdir_utils.assert_workspace_activation_allowed(
        ServerState.get_workdir(),
        workspace_context,
    )
    if sandbox_enabled():
        try:
            await get_sandbox_runtime().restore_workspace(
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=workspace_context.workspace_id,
            )
        except Exception as exc:
            raise HTTPException(409, f"sandbox restore failed: {exc}") from exc
    state = workdir_utils.set_workspace_state(
        ServerState.get_workdir(),
        workspace_context,
        "active",
    )
    return {
        "status": "restored",
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": workspace_context.workspace_id,
        "workspace_state": state,
    }
