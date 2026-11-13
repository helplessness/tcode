"""Session tree deletion and background cleanup state.
"""

import asyncio

from ..runtime import SessionArchiveManager
from ..session.manager import SessionManager
from ..team.registry import WorkerRegistry
from ..workspace.manager import AgentWorkspaceManager
from ._shared import (
    _cancel_symphony_issues_for_session,
    _delete_child_sessions,
    _terminate_session_processes,
)
from .workspace_scope import (
    _session_owner_kwargs_from_session,
)
from .state import ServerState

_SESSION_DELETE_TASKS: dict[str, asyncio.Task] = {}


_SESSION_DELETE_PENDING: set[str] = set()


_SESSION_DELETE_ERRORS: dict[str, str] = {}


def _session_delete_task_done(session_id: str):
    def _done(task: asyncio.Task) -> None:
        try:
            task.result()
            _SESSION_DELETE_ERRORS.pop(session_id, None)
        except Exception as exc:
            _SESSION_DELETE_ERRORS[session_id] = str(exc)
            try:
                print(f"[session-delete] failed for {session_id}: {exc}")
            except Exception:
                pass
        finally:
            _SESSION_DELETE_PENDING.discard(session_id)
            if _SESSION_DELETE_TASKS.get(session_id) is task:
                _SESSION_DELETE_TASKS.pop(session_id, None)

    return _done


async def _delete_session_tree_background(
    session_id: str,
    *,
    owner_kwargs: dict[str, str],
) -> None:
    """删除会话树、执行资源、归档和同 ID 的 checkpoint 文件。

    参数:
        session_id: 待删除的父会话标识。
        owner_kwargs: 用于租户、用户和工作区隔离的所有者条件。
    """

    owner = dict(owner_kwargs or {})
    target = await SessionManager.get(session_id, **owner)
    workspace = None
    turn_task = ServerState.get_turn_task(session_id)
    await ServerState.cancel_session(session_id)
    if turn_task is not None and turn_task is not asyncio.current_task() and not turn_task.done():
        # The cancelled turn persists its final state in finally. Let that
        # finish before deleting rows/files, or it can recreate the workspace.
        await asyncio.wait_for(asyncio.gather(turn_task, return_exceptions=True), timeout=10)
    if target:
        await _delete_child_sessions(
            session_id,
            owner_kwargs=_session_owner_kwargs_from_session(target),
            include_side_chats=True,
        )
    await _cancel_symphony_issues_for_session(session_id)
    await _terminate_session_processes(
        session_id,
        owner_kwargs=(
            _session_owner_kwargs_from_session(target)
            if target is not None
            else owner
        ),
    )

    if target:
        workspace_metadata = dict(
            (getattr(target, "metadata", {}) or {}).get("agent_workspace") or {}
        )
        workspace_id = str(workspace_metadata.get("workspace_id") or "").strip()
        if workspace_id:
            workspace_control = str(
                workspace_metadata.get("control_workdir")
                or getattr(target, "workdir", "")
                or ServerState.get_workdir()
            )
            workspace = await asyncio.to_thread(
                AgentWorkspaceManager.load,
                control_workdir=workspace_control,
                workspace_id=workspace_id,
            )
        await SessionManager.delete(session_id, **owner)
        await asyncio.to_thread(
            SessionArchiveManager.delete_bundle,
            session_id,
            str(getattr(target, "workdir", "") or ServerState.get_workdir()),
        )

    from ..checkpoint.manager import CheckpointManager

    await asyncio.to_thread(
        CheckpointManager.delete,
        session_id,
        str(getattr(target, "workdir", "") or ServerState.get_workdir()),
    )

    await ServerState.cleanup_session(session_id)
    await WorkerRegistry.delete(session_id)
    if workspace is not None and workspace.isolated:
        # Run last: archive/checkpoint cleanup can create control directories.
        # Shared-control child workspaces own no removable directory. Delete
        # their session state only; never remove the main/shared workspace.
        result = await asyncio.to_thread(AgentWorkspaceManager.cleanup, workspace, force=True)
        if not result.removed:
            raise RuntimeError(f"临时工作区清理未完成：{result.reason}")


def _schedule_session_delete(session_id: str, *, owner_kwargs: dict[str, str]) -> bool:
    normalized = str(session_id or "").strip()
    if not normalized:
        return False
    existing = _SESSION_DELETE_TASKS.get(normalized)
    if existing is not None and not existing.done():
        _SESSION_DELETE_PENDING.add(normalized)
        return False
    _SESSION_DELETE_PENDING.add(normalized)
    task = asyncio.create_task(
        _delete_session_tree_background(normalized, owner_kwargs=owner_kwargs)
    )
    task.add_done_callback(_session_delete_task_done(normalized))
    _SESSION_DELETE_TASKS[normalized] = task
    return True
