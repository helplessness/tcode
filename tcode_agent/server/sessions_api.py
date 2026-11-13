"""Session listing, snapshots, usage, archival and deletion endpoints.

Routes retain their original paths and are registered by app.py.
"""

from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..presentation import normalize_content_blocks
from ..guard.verification_display import delivery_message_verification
from ..protocol import SessionUsageSummary as ProtocolSessionUsageSummary
from ..runtime import RuntimeArtifactManager, SessionArchiveManager
from ..session.manager import Session, SessionManager
from ..session.transcript import without_legacy_native_agui_user_echoes
from ..session.usage import build_session_usage_tree
from ..session.usage_activity import build_usage_activity
from ..session.events import SessionEventStore
from ..session.event_archive import EventArchiveStore
from ..session.revision_archive import RevisionArchiveStore
from ..session.store_sqlite import SessionSQLiteStore
from ..team.registry import WorkerRegistry
from . import harness_api
from . import workdirs as workdir_utils
from ._shared import (
    _cleanup_fixed_team_state,
    _is_chat_session_busy,
    _is_internal_frontend_message,
    _public_outcome_validation,
)
from .workspace_scope import (
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _workdir_name_from_path,
    _workspace_context_from_request,
)
from .session_presenter import (
    _public_frontend_message_metadata,
    _serialize_frontend_tool_call,
    public_process_note,
)
from .agent_selection import normalize_primary_agent_name
from .session_lifecycle import (
    _SESSION_DELETE_ERRORS,
    _SESSION_DELETE_PENDING,
    _SESSION_DELETE_TASKS,
    _schedule_session_delete,
)
from .session_presenter import serialize_session_summary
from .state import ServerState
from .workspaces_api import _record_targets_hidden_workspace
from ..workspace.manager import AgentWorkspaceError, AgentWorkspaceManager

router = APIRouter()


@router.get("/api/session-search")
async def search_session_content(
    request: Request,
    q: str = Query("", max_length=200),
    workspace_id: str = Query(""),
    include_archived: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
):
    """Search the same owner-scoped, public transcript the UI can read."""
    tokens = q.strip().casefold().split()
    if not tokens:
        return {"items": [], "total": 0, "truncated": False}
    listings = [await list_sessions(request, archived=False, workspace_id=workspace_id)]
    if include_archived:
        listings.append(await list_sessions(request, archived=True, workspace_id=workspace_id))
    summaries = {row["session_id"]: row for listing in listings for row in listing["sessions"]}
    items: list[dict[str, Any]] = []
    total = 0
    counts = {"session": 0, "message": 0}
    unavailable = 0
    # ponytail: bounded public-transcript scan; use a public-text FTS projection
    # if large histories make measured search latency unacceptable.
    for summary in summaries.values():
        if await request.is_disconnected():
            break
        session_id = summary["session_id"]
        title = str(summary.get("title") or session_id)
        if all(token in " ".join(str(summary.get(key) or "") for key in
                                 ("title", "workdir_name", "workdir", "session_id")).casefold() for token in tokens):
            total += 1
            counts["session"] += 1
            if counts["session"] <= limit:
                items.append({"kind": "session", "id": session_id, "session_id": session_id,
                              "title": title, "snippet": str(summary.get("workdir_name") or ""),
                              "archived": bool(summary.get("is_archived"))})
        try:
            detail = await get_session(session_id, request)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            unavailable += 1
            continue
        for message in detail["messages"]:
            if message.get("role") not in {"user", "assistant"}:
                continue
            content = str(message.get("content") or "")
            folded = content.casefold()
            if not all(token in folded for token in tokens):
                continue
            total += 1
            counts["message"] += 1
            if counts["message"] > limit:
                continue
            start = max(0, folded.find(tokens[0]) - 60)
            items.append({"kind": "message", "id": str(message["id"]), "session_id": session_id,
                          "title": title, "snippet": content[start:start + 220],
                          "archived": bool(summary.get("is_archived"))})
    return {"items": items, "total": total, "truncated": total > len(items), "unavailable": unavailable}


class SessionResearchBindingRequest(BaseModel):
    research_id: str = Field(min_length=1, max_length=160)


class SessionForkRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)


@router.post('/api/sessions/{session_id}/messages/{message_id}/rewind')
async def rewind_user_message(session_id: str, message_id: str, request: Request):
    """Keep the prefix for an edited turn; canonical revisions retain old history."""
    owner = _session_owner_kwargs(_workspace_context_from_request(request))
    source = await SessionManager.get(session_id, **owner)
    if source is None or source.parent_id:
        raise HTTPException(404, 'Session not found')
    if _is_chat_session_busy(session_id):
        raise HTTPException(409, '请等待当前任务结束后再编辑')
    harness = (source.metadata or {}).get('harness') or {}
    if harness.get('runtime_id', 'tcode-native') != 'tcode-native':
        raise HTTPException(409, '当前外部运行环境不支持改写历史消息')
    cutoff = next((i for i, m in enumerate(source.messages) if m.id == message_id), -1)
    if cutoff < 0:
        raise HTTPException(404, 'Message not found')
    if source.messages[cutoff].role != 'user':
        raise HTTPException(400, '只能编辑用户发送的消息')
    prefix = list(source.messages[:cutoff])
    task = asyncio.current_task()
    if ServerState.claim_chat_turn(session_id, f'edit-{uuid.uuid4()}', task) != 'accepted':
        raise HTTPException(409, '请等待当前任务结束后再编辑')
    try:
        metadata = deepcopy(source.metadata or {})
        metadata['edited_out_message_ids'] = list(dict.fromkeys([
            *metadata.get('edited_out_message_ids', []),
            *(m.id for m in source.messages[cutoff:]),
        ]))
        await SessionManager.update(session_id, messages=prefix, metadata=metadata, **owner)
        runtime = ServerState.sessions.get(session_id)
        if runtime is not None:
            runtime['messages'] = list(prefix)
        agent = ServerState.agents.get(session_id)
        if agent is not None:
            agent.messages = list(prefix)
    finally:
        ServerState.finish_chat_turn(session_id, task)
    return {'ok': True, 'message_id': message_id}


@router.post('/api/sessions/{session_id}/compact')
async def compact_session(session_id: str, request: Request):
    """Manually compact native context, keeping the original canonical archive."""
    from ..providers import create_provider
    from ..session.compaction import SessionCompaction, CompactionConfig
    from ..runtime.workspace_registry import registry_workspace

    context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(context)
    session = await SessionManager.get(session_id, **owner)
    if session is None:
        raise HTTPException(404, 'Session not found')
    projection = harness_api.harness_projection_for_session(session)
    if projection.get('runtime_id', 'tcode-native') != 'tcode-native':
        raise HTTPException(409, '当前 Harness 不支持手动压缩')
    if _is_chat_session_busy(session_id):
        raise HTTPException(409, '请等待当前任务完成后压缩')
    task = asyncio.current_task()
    if ServerState.claim_chat_turn(session_id, f'compact-{uuid.uuid4()}', task) != 'accepted':
        raise HTTPException(409, '当前会话正在运行或压缩')
    provider = None
    try:
        if len(session.messages) < 2:
            return {'status': 'skipped', 'original_tokens': 0, 'compressed_tokens': 0, 'tokens_saved': 0}
        # Summarize a copy: provider failure must not mutate the live session
        # or cached Agent. Persistence still uses the normal session store.
        working = replace(session, messages=deepcopy(session.messages), metadata=deepcopy(session.metadata))
        with registry_workspace(session.workdir):
            provider = create_provider(workdir=session.workdir, model_ref=projection.get('model_id') or session.metadata.get('model') or None)
            result = await SessionCompaction.process(working, provider, auto=False, config=CompactionConfig.from_env(), raise_on_provider_error=True)
        if result.status == 'success' and result.tokens_saved <= 0:
            result.status = 'skipped'
        if result.status == 'success':
            # Archive the original audit chain before replacing hot context.
            SessionEventStore.compact(session_id, snapshot=SessionManager._serialize_session(session),
                owner={'tenant_id': session.tenant_id, 'user_id': session.user_id, 'workspace_id': session.workspace_id})
            await SessionManager.update(session_id, **owner, messages=working.messages,
                compaction_count=session.compaction_count + 1,
                last_compacted_at=datetime.now(timezone.utc).timestamp(),
                total_tokens_saved=session.total_tokens_saved + max(0, result.tokens_saved))
            agent = ServerState.agents.get(session_id)
            if agent is not None:
                agent.messages = list(working.messages)
            runtime = ServerState.sessions.get(session_id)
            if runtime is not None:
                runtime['messages'] = list(working.messages)
        return {'status': result.status, 'original_tokens': result.original_tokens,
                'compressed_tokens': result.compressed_tokens, 'tokens_saved': result.tokens_saved}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, '上下文压缩失败，请检查模型连接或会话存储后重试；原始历史归档不会被删除') from exc
    finally:
        ServerState.finish_chat_turn(session_id, task)
        if provider is not None:
            close = getattr(provider, 'close', None)
            if close is not None:
                try:
                    closed = close()
                    if hasattr(closed, '__await__'):
                        await closed
                except Exception:
                    # Cleanup must not turn a committed summary into a failed
                    # response and encourage a duplicate compaction request.
                    pass


@router.get("/api/sessions/{session_id}/agent-workspace")
async def get_main_agent_workspace(session_id: str, request: Request):
    """Show a main session's private files and pending changes."""
    workspace_context = _workspace_context_from_request(request)
    session = await SessionManager.get(
        session_id, **_session_owner_kwargs(workspace_context)
    )
    if session is None or session.parent_id:
        raise HTTPException(404, "Session not found")
    workspace_metadata = dict((session.metadata or {}).get("agent_workspace") or {})
    control_workdir = str(
        workspace_metadata.get("control_workdir") or session.workdir or ""
    ).strip()
    if not control_workdir:
        raise HTTPException(404, "Session workspace not found")
    workspace_id = str(workspace_metadata.get("workspace_id") or "").strip()
    if not workspace_id:
        workspace_id = AgentWorkspaceManager.workspace_id_for(
            control_workdir=control_workdir,
            kind="main-agent",
            task_id=session_id,
            strategy=str(workspace_metadata.get("strategy") or "snapshot"),
        )
    workspace = AgentWorkspaceManager.load(
        control_workdir=control_workdir, workspace_id=workspace_id
    )
    if workspace is None or workspace.status == "cleaned":
        raise HTTPException(404, "Session workspace not found")
    workspace = AgentWorkspaceManager.inspect(workspace)
    return {
        "session_id": session_id,
        "control_workdir": control_workdir,
        "execution_workdir": workspace.execution_workdir,
        "changed_files": workspace.changed_files,
        "dirty": workspace.dirty,
        "error": workspace.error,
    }


def _serialize_session_summary(session: Session) -> dict[str, Any]:
    session_context = _workspace_context_from_request(
        None,
        tenant_id=str(getattr(session, "tenant_id", "") or ""),
        user_id=str(getattr(session, "user_id", "") or ""),
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
    )
    summary = serialize_session_summary(
        session,
        default_workdir=ServerState.get_workdir(),
        session_context=session_context,
        workdir_name=_workdir_name_from_path,
    )
    if not session.parent_id:
        summary["agent_name"] = normalize_primary_agent_name(summary.get("agent_name"))
    return summary


def _sessions_with_runtime_usage_overlay(
    workspace_context,
    *,
    include_workspace: bool = False,
    include_archived: bool = True,
) -> list[Session]:
    from ..session.manager import SessionManager

    server_workdir = ServerState.get_workdir()
    visibility_cache: dict[workdir_utils.WorkspaceContext, bool] = {}
    sessions = dict(
        SessionManager.get_all_sync(
            **_session_owner_kwargs(
                workspace_context,
                include_workspace=include_workspace,
            )
        )
    )
    sessions = {
        session_id: session
        for session_id, session in sessions.items()
        if not _record_targets_hidden_workspace(
            session,
            server_workdir=server_workdir,
            visibility_cache=visibility_cache,
        )
    }
    if not include_archived:
        sessions = {
            session_id: session
            for session_id, session in sessions.items()
            if not SessionManager.is_archived_session(session)
        }

    now_ts = float(datetime.now().timestamp())
    for session_id, runtime in ServerState.sessions.items():
        if _record_targets_hidden_workspace(
            runtime,
            server_workdir=server_workdir,
            visibility_cache=visibility_cache,
        ):
            continue
        if not _runtime_session_matches_context(
            runtime,
            workspace_context,
            include_workspace=include_workspace,
        ):
            continue

        persisted = sessions.get(session_id)
        if persisted is not None and not include_archived and SessionManager.is_archived_session(persisted):
            continue

        runtime_agent = ServerState.agents.get(session_id)
        runtime_messages = list(
            getattr(runtime_agent, "messages", [])
            or (runtime or {}).get("messages", [])
            or []
        )
        if persisted is not None:
            sessions[session_id] = replace(
                persisted,
                agent_name=str((runtime or {}).get("agent_name") or persisted.agent_name or "build"),
                status=str((runtime or {}).get("status") or persisted.status or "active"),
                workdir=str((runtime or {}).get("workdir") or persisted.workdir or ""),
                last_active_at=now_ts if ServerState.is_running(session_id) else persisted.last_active_at,
                messages=runtime_messages or list(getattr(persisted, "messages", []) or []),
            )
            continue

        worker = WorkerRegistry.get(session_id)
        parent_id = ""
        if worker is not None:
            parent_id = str(getattr(worker, "team_id", "") or "").strip()
        if not parent_id and isinstance(runtime, dict):
            parent_id = str(runtime.get("parent_id", "") or "").strip()

        sessions[session_id] = Session(
            id=session_id,
            parent_id=parent_id or None,
            title=(
                runtime_messages[0].content[:120]
                if runtime_messages
                else str((runtime or {}).get("title") or "Runtime session")
            ),
            agent_name=str((runtime or {}).get("agent_name", "build") or "build"),
            status=str((runtime or {}).get("status", "active") or "active"),
            tenant_id=workspace_context.tenant_id,
            user_id=workspace_context.user_id,
            workspace_id=str((runtime or {}).get("workspace_id", "") or workspace_context.workspace_id),
            workdir=str((runtime or {}).get("workdir", "") or ServerState.get_workdir()),
            created_at=now_ts,
            last_active_at=now_ts,
            messages=runtime_messages,
        )

    return list(sessions.values())


@router.get("/api/usage/sessions")
async def get_session_usage_summary(
    request: Request,
    workspace_id: str = Query(""),
    include_archived: bool = Query(True),
):
    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=str(workspace_id or ""),
    )
    filter_workspace = str(workspace_id or "").strip()
    sessions = _sessions_with_runtime_usage_overlay(
        workspace_context,
        include_workspace=bool(filter_workspace),
        include_archived=bool(include_archived),
    )
    summary = build_session_usage_tree(sessions)
    payload = {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": filter_workspace or "",
        **summary,
    }
    return ProtocolSessionUsageSummary.model_validate(payload).model_dump(
        mode="json",
        exclude_unset=True,
    )


@router.get("/api/usage/activity")
async def get_session_usage_activity(
    request: Request,
    workspace_id: str = Query(""),
    include_archived: bool = Query(True),
    utc_offset_minutes: int = Query(0, ge=-720, le=840),
):
    context = _workspace_context_from_request(request, workspace_id=str(workspace_id or ""))
    sessions = await asyncio.to_thread(
        _sessions_with_runtime_usage_overlay,
        context,
        include_workspace=bool(str(workspace_id or "").strip()),
        include_archived=include_archived,
    )
    paths = [SessionEventStore.path_for(session.id, create=False)
             for session in sessions]
    today = datetime.now(timezone(timedelta(minutes=utc_offset_minutes))).date().isoformat()
    return await asyncio.to_thread(build_usage_activity, paths, utc_offset_minutes, today=today)


def _ensure_session_archive_bundle(
    session: Session,
    *,
    force: bool = False,
) -> dict[str, str]:
    workdir = str(getattr(session, "workdir", "") or ServerState.get_workdir()).strip()
    if not workdir:
        workdir = ServerState.get_workdir()

    if not force and SessionArchiveManager.bundle_exists(session.id, workdir):
        return SessionArchiveManager.bundle_paths(session.id, workdir)

    serialized = SessionManager._serialize_session(session)
    serialized.pop("display_messages", None)
    messages = [
        SessionManager._message_to_dict(message)
        for message in SessionManager.visible_messages(session)
    ]
    artifacts = RuntimeArtifactManager.list_for_session(session.id, workdir)
    return SessionArchiveManager.write_bundle(
        session_data=serialized,
        messages=messages,
        artifacts=artifacts,
        workdir=workdir,
    )


@router.get("/api/sessions")
async def list_sessions(
    request: Request,
    archived: bool = Query(False),
    workspace_id: str = Query(""),
):
    """List top-level sessions from SessionManager"""
    from ..session.manager import SessionManager

    workspace_context = _workspace_context_from_request(
        request,
        workspace_id=str(workspace_id or ""),
    )
    filter_workspace = str(workspace_id or "").strip()
    server_workdir = ServerState.get_workdir()
    visibility_cache: dict[workdir_utils.WorkspaceContext, bool] = {}
    persisted_sessions = await SessionManager.list(
        **_session_owner_kwargs(
            workspace_context,
            include_workspace=bool(filter_workspace),
        )
    )
    sessions = {
        session.id: session
        for session in persisted_sessions
        if not _record_targets_hidden_workspace(
            session,
            server_workdir=server_workdir,
            visibility_cache=visibility_cache,
        )
    }
    root_sessions = [
        s
        for s in sessions.values()
        if (
            not s.parent_id
            and s.id not in _SESSION_DELETE_PENDING
            and SessionManager.is_archived_session(s) == bool(archived)
        )
    ]
    root_by_id = {s.id: s for s in root_sessions}

    # Merge active in-memory root sessions so refresh works even before first disk sync.
    if not archived:
        for session_id, runtime in ServerState.sessions.items():
            if session_id in _SESSION_DELETE_PENDING:
                continue
            if _record_targets_hidden_workspace(
                runtime,
                server_workdir=server_workdir,
                visibility_cache=visibility_cache,
            ):
                continue
            if not _runtime_session_matches_context(
                runtime,
                workspace_context,
                include_workspace=bool(filter_workspace),
            ):
                continue
            if session_id in root_by_id:
                continue
            persisted_session = sessions.get(session_id)
            if persisted_session is not None:
                if str(getattr(persisted_session, "parent_id", "") or "").strip():
                    continue
                if SessionManager.is_archived_session(persisted_session):
                    continue
            worker = WorkerRegistry.get(session_id)
            if worker is not None:
                continue
            agent = ServerState.agents.get(session_id)
            runtime_messages = list(
                getattr(agent, "messages", []) or runtime.get("messages", []) or []
            )
            synthetic = Session(
                id=session_id,
                parent_id=None,
                title=(
                    runtime_messages[0].content[:120] if runtime_messages else "New Chat"
                ),
                agent_name=str(runtime.get("agent_name", "build") or "build"),
                status="active",
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=str(
                    runtime.get("workspace_id", "") or workspace_context.workspace_id
                ),
                workdir=str(runtime.get("workdir", "") or ServerState.get_workdir()),
                created_at=float(datetime.now().timestamp()),
                last_active_at=float(datetime.now().timestamp()),
                messages=runtime_messages,
            )
            root_by_id[session_id] = synthetic
    else:
        for session in root_by_id.values():
            try:
                _ensure_session_archive_bundle(session, force=False)
            except Exception:
                pass

    def _sort_key(item: Session) -> tuple[float, str]:
        if archived:
            timestamp = float(SessionManager.archived_at(item) or item.last_active_at or 0)
        else:
            timestamp = float(item.last_active_at or item.created_at or 0)
        return (-timestamp, str(item.id))

    return {
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": filter_workspace or "",
        "sessions": [
            _serialize_session_summary(s)
            for s in sorted(root_by_id.values(), key=_sort_key)
        ]
    }


@router.post("/api/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    payload: SessionForkRequest,
    request: Request,
):
    """Create a new top-level chat from history through one assistant message."""

    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    source = await SessionManager.get(session_id, **session_owner)
    if source is None or source.parent_id:
        raise HTTPException(404, "Session not found")
    if _is_chat_session_busy(session_id):
        raise HTTPException(409, "Cannot fork a running session")

    cutoff = next(
        (
            index
            for index, message in enumerate(source.messages)
            if str(getattr(message, "id", "") or "") == payload.message_id
        ),
        -1,
    )
    if cutoff < 0:
        raise HTTPException(404, "Message not found")
    if str(getattr(source.messages[cutoff], "role", "") or "") != "assistant":
        raise HTTPException(400, "A chat can only be forked from an assistant response")

    cloned_messages = []
    message_ids: dict[str, str] = {}
    for message in source.messages[: cutoff + 1]:
        data = SessionManager._message_to_dict(message)
        original_id = str(data.get("id") or "")
        cloned_id = str(uuid.uuid4())
        if original_id:
            message_ids[original_id] = cloned_id
        data["id"] = cloned_id
        metadata = dict(data.get("metadata") or {})
        if original_id:
            metadata["forked_from_message_id"] = original_id
        data["metadata"] = metadata
        cloned_messages.append(SessionManager._message_from_dict(data))

    for message in cloned_messages:
        parent_id = str(getattr(message, "parent_id", "") or "")
        if parent_id in message_ids:
            message.parent_id = message_ids[parent_id]

    metadata = dict(getattr(source, "metadata", {}) or {})
    for key in (
        "archived_at",
        "archived_by_user",
        "archive_cause",
        "archive_locked",
        "async_completion_events",
    ):
        metadata.pop(key, None)
    new_session_id = str(uuid.uuid4())
    source_workspace = dict((source.metadata or {}).get("agent_workspace") or {})
    source_execution_workdir = str(
        source_workspace.get("execution_workdir") or ""
    ).strip()
    control_workdir = (
        source_execution_workdir
        if source_execution_workdir and Path(source_execution_workdir).is_dir()
        else str(source.workdir or ServerState.get_workdir())
    )
    strategy = (
        "git_worktree"
        if AgentWorkspaceManager._repo_root(
            Path(control_workdir).expanduser().resolve(strict=False)
        ) is not None
        else "snapshot"
    )
    try:
        workspace = AgentWorkspaceManager.create(
            control_workdir=control_workdir,
            task_id=new_session_id,
            session_id=new_session_id,
            kind="main-agent",
            strategy=strategy,
            copy_control_changes=strategy == "git_worktree",
        )
    except AgentWorkspaceError as exc:
        raise HTTPException(500, f"Failed to create fork workspace: {exc}") from exc

    metadata.update(
        {
            "forked_from_session_id": source.id,
            "forked_from_message_id": payload.message_id,
            "forked_at": datetime.now().timestamp(),
            "agent_workspace": {
                "workspace_id": workspace.workspace_id,
                "strategy": workspace.strategy,
                "control_workdir": workspace.control_workdir,
                "execution_workdir": workspace.execution_workdir,
                "base_sha": workspace.base_sha,
                "isolated": workspace.isolated,
            },
        }
    )
    source_title = str(source.title or "New Chat").strip() or "New Chat"
    title = source_title if source_title.endswith("（分支）") else source_title + "（分支）"
    try:
        created = await SessionManager.create(
            session_id=new_session_id,
            title=title[:240],
            agent_name=source.agent_name,
            workdir=source.workdir,
            tenant_id=source.tenant_id,
            user_id=source.user_id,
            workspace_id=source.workspace_id,
            metadata=metadata,
        )
    except Exception:
        AgentWorkspaceManager.cleanup(workspace, force=True)
        raise
    try:
        await SessionManager.update(
            created.id,
            messages=cloned_messages,
            **session_owner,
        )
        created = await SessionManager.get(created.id, **session_owner)
        if created is None:
            raise RuntimeError("forked session disappeared after persistence")
    except Exception as exc:
        await SessionManager.delete(new_session_id, **session_owner)
        AgentWorkspaceManager.cleanup(workspace, force=True)
        raise HTTPException(500, f"Failed to persist forked session: {exc}") from exc

    source_runtime = ServerState.sessions.get(session_id, {})
    runtime = ServerState.get_or_create_session(
        created.id,
        agent_name=created.agent_name,
        scheduler_mode=str(source_runtime.get("scheduler_mode") or "supervisor"),
        execution_profile=str(source_runtime.get("execution_profile") or "standard"),
        workdir=str(created.workdir or ServerState.get_workdir()),
    )
    runtime["messages"] = list(cloned_messages)
    runtime["control_workdir"] = control_workdir
    runtime["agent_workspace_id"] = workspace.workspace_id
    runtime["agent_execution_workdir"] = workspace.execution_workdir
    if source_runtime.get("model"):
        runtime["model"] = source_runtime["model"]

    return {"session": await get_session(created.id, request)}


@router.get("/api/async-completions")
async def list_async_completions(
    request: Request,
    session_id: str = Query(""),
    unread_only: bool = Query(False),
):
    from ..runtime.async_completion import list_completion_events

    workspace_context = _workspace_context_from_request(request)
    events = await list_completion_events(
        session_id=str(session_id or ""),
        unread_only=bool(unread_only),
        **_session_owner_kwargs(workspace_context),
    )
    return {
        "events": events,
        "unread_count": sum(
            1 for event in events if not str(event.get("read_at") or "").strip()
        ),
    }


@router.post("/api/async-completions/read-all")
async def read_all_async_completions(request: Request):
    from ..runtime.async_completion import mark_completion_read

    context = _workspace_context_from_request(request)
    changed = await mark_completion_read(**_session_owner_kwargs(context))
    return {"status": "ok", "changed": changed}


@router.post("/api/async-completions/{event_id}/read")
async def read_async_completion(event_id: str, request: Request):
    from ..runtime.async_completion import (
        list_completion_events,
        mark_completion_read,
    )

    workspace_context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(workspace_context)
    existing = await list_completion_events(**owner)
    if not any(str(item.get("id") or "") == str(event_id or "") for item in existing):
        raise HTTPException(404, "Async completion event not found")
    changed = await mark_completion_read(event_id=event_id, **owner)
    return {"status": "ok", "event_id": event_id, "changed": changed}


@router.post("/api/sessions/{session_id}/async-completions/read")
async def read_session_async_completions(session_id: str, request: Request):
    from ..runtime.async_completion import mark_completion_read

    workspace_context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(workspace_context)
    session = await SessionManager.get(session_id, **owner)
    if session is None:
        raise HTTPException(404, "Session not found")
    changed = await mark_completion_read(
        session_id=session_id,
        **owner,
    )
    return {"status": "ok", "session_id": session_id, "changed": changed}


@router.post(
    "/api/sessions/{session_id}/async-completions/{identifier}/continued"
)
async def continue_session_async_completion(
    session_id: str,
    identifier: str,
    request: Request,
):
    from ..runtime.async_completion import session_completion_events

    workspace_context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(workspace_context)
    session = await SessionManager.get(session_id, **owner)
    if session is None:
        raise HTTPException(404, "Session not found")
    event = next((event for event in session_completion_events(session)
                  if event.get("identifier") == identifier), None)
    if event is None:
        raise HTTPException(404, "Async completion event not found")
    if event.get("continuation_status") != "continued":
        raise HTTPException(409, "Continuation has not been committed by the server")

    return {
        "status": "ok",
        "session_id": session_id,
        "identifier": identifier,
        "matched": True,
    }


@router.post("/api/sessions/{session_id}/archive")
async def archive_session(session_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    target = await SessionManager.get(session_id, **session_owner)
    if not target:
        raise HTTPException(404, "Session not found")
    if target.parent_id:
        raise HTTPException(400, "Only top-level sessions can be archived")
    if _is_chat_session_busy(session_id):
        raise HTTPException(409, "Cannot archive a running session")

    updated = await SessionManager.set_archived(
        session_id,
        archived=True,
        archived_by_user=True,
        archive_cause="user",
        archive_locked=False,
        **session_owner,
    )
    if updated is None:
        raise HTTPException(404, "Session not found")
    archive_bundle = _ensure_session_archive_bundle(updated, force=True)

    return {
        "status": "ok",
        "session": _serialize_session_summary(updated),
        "archive_bundle": archive_bundle,
    }


@router.post("/api/sessions/{session_id}/research")
async def bind_session_to_research(
    session_id: str,
    payload: SessionResearchBindingRequest,
    request: Request,
):
    """Bind a top-level Session to the Workspace's current formal project."""

    from ..research import ResearchTopicStore

    workspace_context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(workspace_context)
    session = await SessionManager.get(session_id, **owner)
    if session is None or session.parent_id:
        raise HTTPException(404, "Session not found")
    project = ResearchTopicStore.get_project(
        payload.research_id,
        tenant_id=workspace_context.tenant_id,
        user_id=workspace_context.user_id,
        workspace_id=str(session.workspace_id or workspace_context.workspace_id),
        workdir=str(session.workdir or "") or None,
    )
    if project is None:
        raise HTTPException(404, "Research Project not found")
    if project.status == "archived":
        raise HTTPException(409, "不能把会话绑定到已归档课题")
    try:
        updated = await SessionManager.bind_research_project(
            session_id,
            research_id=project.research_id,
            mode_source="user_confirmed",
            **owner,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Session not found")
    return {
        "status": "ok",
        "session": _serialize_session_summary(updated),
        "project": project.to_dict(),
    }


@router.delete("/api/sessions/{session_id}/research")
async def unbind_session_from_research(session_id: str, request: Request):
    """Convert an unlocked top-level Research Session back to general mode."""

    workspace_context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(workspace_context)
    session = await SessionManager.get(session_id, **owner)
    if session is None or session.parent_id:
        raise HTTPException(404, "Session not found")
    try:
        updated = await SessionManager.unbind_research_project(session_id, **owner)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Session not found")
    return {"status": "ok", "session": _serialize_session_summary(updated)}


@router.post("/api/sessions/{session_id}/unarchive")
async def unarchive_session(session_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    target = await SessionManager.get(session_id, **session_owner)
    if not target:
        raise HTTPException(404, "Session not found")
    if target.parent_id:
        raise HTTPException(400, "Only top-level sessions can be unarchived")

    try:
        updated = await SessionManager.set_archived(
            session_id,
            archived=False,
            archived_by_user=False,
            **session_owner,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Session not found")

    return {
        "status": "ok",
        "session": _serialize_session_summary(updated),
    }


@router.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, payload: dict[str, Any], request: Request):
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    target = await SessionManager.get(session_id, **session_owner)
    if not target:
        raise HTTPException(404, "Session not found")
    if target.parent_id:
        raise HTTPException(400, "Only top-level sessions can be renamed")

    title = str(payload.get("title", "") or "").strip()
    if not title:
        raise HTTPException(400, "title is required")

    await SessionManager.update(
        session_id,
        **session_owner,
        title=title,
        last_active_at=datetime.now().timestamp(),
    )
    updated = await SessionManager.get(session_id, **session_owner)
    if not updated:
        raise HTTPException(404, "Session not found")

    runtime = ServerState.sessions.get(session_id)
    if isinstance(runtime, dict):
        runtime["title"] = title
    elif runtime is not None:
        try:
            setattr(runtime, "title", title)
        except Exception:
            pass

    return {
        "status": "ok",
        "session": _serialize_session_summary(updated),
    }


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """Delete a session and all nested sub-sessions."""
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    scheduled = _schedule_session_delete(session_id, owner_kwargs=session_owner)
    return {
        "status": "deleting",
        "deleted_session_id": session_id,
        "scheduled": scheduled,
        "pending": True,
        "error": _SESSION_DELETE_ERRORS.get(session_id, ""),
    }


@router.get("/api/sessions/{session_id}/delete-status")
async def get_session_delete_status(session_id: str):
    task = _SESSION_DELETE_TASKS.get(session_id)
    return {
        "session_id": session_id,
        "pending": session_id in _SESSION_DELETE_PENDING,
        "running": bool(task is not None and not task.done()),
        "error": _SESSION_DELETE_ERRORS.get(session_id, ""),
    }


@router.delete("/api/sessions/{session_id}/subagents")
async def delete_session_subagents(session_id: str, request: Request):
    workspace_context = _workspace_context_from_request(request)
    parent = await SessionManager.get(
        session_id,
        **_session_owner_kwargs(workspace_context),
    )
    if not parent:
        raise HTTPException(404, "Session not found")
    cleanup_summary = await _cleanup_fixed_team_state(session_id)
    return {
        "status": "ok",
        "parent_session_id": session_id,
        "deleted_session_ids": cleanup_summary.get("deleted_child_sessions", []),
        "removed_worker_session_ids": cleanup_summary.get("removed_workers", []),
    }


async def _archived_session_owner(session_id: str, request: Request) -> dict[str, str]:
    context = _workspace_context_from_request(request)
    owner = _session_owner_kwargs(context, include_workspace=True)
    if await SessionManager.get(session_id, **owner) is None:
        raise HTTPException(404, "Session not found")
    return owner


@router.get("/api/sessions/{session_id}/archived-events")
async def search_archived_events(
    session_id: str,
    request: Request,
    q: str = "",
    event_type: str = "",
    created_from: float | None = None,
    created_to: float | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    owner = await _archived_session_owner(session_id, request)
    items = EventArchiveStore.search(
        session_id,
        query=q,
        event_type=event_type,
        created_from=created_from,
        created_to=created_to,
        owner=owner,
        limit=limit,
        workdir=ServerState.get_workdir(),
    )
    return {"session_id": session_id, "count": len(items), "events": items}


@router.get("/api/sessions/{session_id}/archived-events/{archive_event_id}")
async def get_archived_event(session_id: str, archive_event_id: int, request: Request):
    owner = await _archived_session_owner(session_id, request)
    event = EventArchiveStore.read(
        archive_event_id,
        session_id=session_id,
        owner=owner,
        workdir=ServerState.get_workdir(),
    )
    if event is None:
        raise HTTPException(404, "Archived event not found")
    return event


@router.get("/api/sessions/{session_id}/archived-revisions")
async def search_archived_revisions(
    session_id: str,
    request: Request,
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
):
    owner = await _archived_session_owner(session_id, request)
    items = RevisionArchiveStore.search(
        session_id,
        query=q,
        owner=owner,
        limit=limit,
        workdir=ServerState.get_workdir(),
    )
    return {"session_id": session_id, "count": len(items), "revisions": items}


@router.get("/api/sessions/{session_id}/archived-revisions/{revision}")
async def get_archived_revision(session_id: str, revision: int, request: Request):
    owner = await _archived_session_owner(session_id, request)
    payload = RevisionArchiveStore.read(
        session_id=session_id,
        revision=revision,
        owner=owner,
        workdir=ServerState.get_workdir(),
    )
    if payload is None:
        raise HTTPException(404, "Archived revision not found")
    return payload


@router.post("/api/sessions/{session_id}/archived-revisions/{revision}/restore")
async def restore_archived_revision(session_id: str, revision: int, request: Request):
    owner = await _archived_session_owner(session_id, request)
    try:
        return SessionSQLiteStore.restore_archived_revision(
            session_id,
            revision,
            workdir=ServerState.get_workdir(),
            **owner,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    """Get a specific session with messages"""
    from ..session.manager import SessionManager

    if session_id in _SESSION_DELETE_PENDING:
        raise HTTPException(404, "Session is being deleted")

    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    runtime = ServerState.sessions.get(session_id)
    if not _runtime_session_matches_context(runtime, workspace_context):
        runtime = None
    runtime_agent = ServerState.agents.get(session_id) if runtime else None
    runtime_messages = list(
        getattr(runtime_agent, "messages", [])
        or (runtime or {}).get("messages", [])
        or []
    )

    session = await SessionManager.get(session_id, **session_owner)
    if not session:
        if runtime:
            worker = WorkerRegistry.get(session_id)
            inferred_parent_id = ""
            if worker is not None:
                inferred_parent_id = str(getattr(worker, "team_id", "") or "").strip()
            elif isinstance(runtime, dict):
                inferred_parent_id = str(runtime.get("parent_id", "") or "").strip()
            session = Session(
                id=session_id,
                parent_id=inferred_parent_id or None,
                title=(
                    runtime_messages[0].content[:120]
                    if runtime_messages
                    else "New Chat"
                ),
                agent_name=str(runtime.get("agent_name", "build") or "build"),
                status="active",
                tenant_id=workspace_context.tenant_id,
                user_id=workspace_context.user_id,
                workspace_id=str(runtime.get("workspace_id", "") or workspace_context.workspace_id),
                workdir=str(runtime.get("workdir", "") or ServerState.get_workdir()),
                created_at=float(datetime.now().timestamp()),
                last_active_at=float(datetime.now().timestamp()),
                messages=runtime_messages,
            )
        else:
            raise HTTPException(404, "Session not found")

    persisted_messages = list(getattr(session, "messages", []) or [])
    
    # Smart message selection:
    # 1. If session is actively running, prefer runtime (it may have uncommitted new messages)
    # 2. If runtime is empty/shorter, always use persisted (never regress message history)
    # 3. If runtime is longer/equal and has content, use runtime
    # 4. Fall back to persisted if available
    if not runtime_messages:
        # No runtime messages, always use persisted
        response_messages = persisted_messages
    elif ServerState.is_running(session_id):
        # Session actively running: runtime has uncommitted progress, use it
        response_messages = runtime_messages
    elif len(runtime_messages) >= len(persisted_messages):
        # Runtime is longer or equal, use it
        response_messages = runtime_messages
    else:
        # Runtime is shorter (e.g., after agent restart), use persisted
        # This prevents message loss on page refresh
        response_messages = persisted_messages

    # Keep runtime field overrides aligned with the selected message source.
    use_runtime_messages = response_messages is runtime_messages
    response_messages = without_legacy_native_agui_user_echoes(
        response_messages,
        session_metadata=getattr(session, "metadata", {}),
    )
    from ..runtime.async_completion import restore_missing_completion_result_messages

    response_messages = restore_missing_completion_result_messages(
        session,
        response_messages,
    )
    response_messages = SessionManager.visible_messages(
        session,
        current_messages=response_messages,
    )
    from .goal_mode import goal_message_verifications
    goal_verifications = goal_message_verifications(session, response_messages)

    response_agent_name = str(
        ((runtime or {}).get("agent_name") if use_runtime_messages else "")
        or getattr(session, "agent_name", "")
        or "build"
    )
    if not session.parent_id:
        response_agent_name = normalize_primary_agent_name(response_agent_name)
    response_workdir = str(
        ((runtime or {}).get("workdir") if use_runtime_messages else "")
        or getattr(session, "workdir", "")
        or ""
    )
    response_status = str(
        ((runtime or {}).get("status") if use_runtime_messages else "")
        or getattr(session, "status", "active")
        or "active"
    )
    response_updated_at = (
        float(datetime.now().timestamp())
        if use_runtime_messages and ServerState.is_running(session_id)
        else session.last_active_at
    )
    archived_at = SessionManager.archived_at(session)
    response_metadata = dict(getattr(session, "metadata", {}) or {})
    if isinstance(response_metadata.get("async_completion_events"), list):
        response_metadata["async_completion_events"] = [
            {key: value for key, value in event.items() if key != "task_context"}
            for event in response_metadata["async_completion_events"] if isinstance(event, dict)
        ]
    try:
        response_metadata["harness"] = harness_api.harness_projection_for_session(
            session
        )
    except Exception:
        # Session history remains readable if the auxiliary handoff projection
        # is temporarily unavailable.
        pass

    return {
        "session_id": session.id,
        "parent_id": session.parent_id,
        "title": session.title,
        "agent_name": response_agent_name,
        "status": response_status,
        "tenant_id": str(getattr(session, "tenant_id", "") or ""),
        "user_id": str(getattr(session, "user_id", "") or ""),
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
        "workdir": response_workdir,
        "workdir_name": _workdir_name_from_path(
            response_workdir,
            workspace_context,
        ),
        "metadata": response_metadata,
        "created_at": session.created_at,
        "updated_at": response_updated_at,
        "is_archived": archived_at is not None,
        "archived_at": archived_at,
        "mode": str(response_metadata.get("mode") or "general"),
        "research_id": str(response_metadata.get("research_id") or ""),
        "mode_source": str(response_metadata.get("mode_source") or "default"),
        "archive_cause": str(response_metadata.get("archive_cause") or ""),
        "archive_locked": bool(response_metadata.get("archive_locked")),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at or None,
                "process_notes": [
                    public_process_note(note)
                    for note in list(getattr(m, "process_notes", []) or [])
                ] or None,
                "presentation": getattr(m, "presentation", None),
                "content_blocks": normalize_content_blocks(
                    getattr(m, "content_blocks", [])
                )
                or None,
                "research_grounding": dict(
                    getattr(m, "research_grounding", {}) or {}
                )
                or None,
                "verification": _public_outcome_validation(
                    goal_verifications.get(m.id, delivery_message_verification(m, session.metadata))
                )
                if m.id in goal_verifications or getattr(m, "verification", None)
                else None,
                "metadata": _public_frontend_message_metadata(m) or None,
                "tool_calls": [
                    _serialize_frontend_tool_call(tc)
                    for tc in (m.tool_calls or [])
                ]
                if m.tool_calls
                else None,
                "usage": {
                    "prompt_tokens": m.usage.prompt_tokens,
                    "completion_tokens": m.usage.completion_tokens,
                    "total_tokens": m.usage.total_tokens,
                }
                if m.usage
                else None,
            }
            for m in response_messages
            # 过滤掉内部编排提示，避免其作为"用户"消息显示在前端
            if not (
                _is_internal_frontend_message(m)
                or (
                    getattr(m, "role", "") == "user"
                    and str(getattr(m, "content", "") or "").startswith("Async Symphony issue finished:")
                )
            )
        ],
    }
