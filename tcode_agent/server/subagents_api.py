"""Subagent and team-worker conversations and inspection.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import json
import hashlib
import uuid
from dataclasses import asdict
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agent.agents import BuiltinAgents
from ..session.manager import SessionManager
from ..workspace.manager import AgentWorkspaceManager
from ..session.revision_archive import RevisionArchiveStore
from types import SimpleNamespace
from ..runtime.manager import RuntimeTaskManager
from ..team.catalog import TeamCatalog
from ..team.channel import TeamChannel
from ..team.peer_comm import (
    get_team_peer_settings,
    list_peer_threads,
    set_team_peer_enabled,
)
from ..team.profile import worker_profile_summary
from ..team.registry import WorkerRegistry
from ._shared import (
    _active_fixed_team_key_for_session,
    _bootstrap_fixed_team_workers,
    _chat_runtime_status_payload,
    _collect_session_ancestor_ids,
    _ensure_session_guidance_drain_v2,
    _is_chat_session_busy,
    _is_internal_frontend_message,
    _list_descendant_sessions_recursive,
    _merge_resource_context,
    _resolve_subagent_display_status,
    _resolve_worker_display_status,
    _run_subagent_turn_background,
    _serialize_frontend_tool_call,
    _session_matches_active_fixed_team,
)
from .session_presenter import (
    _extract_subagent_details,
)
from .workspace_scope import (
    _resolve_session_workdir,
    _resolve_workdir_from_name_or_path,
    _runtime_session_matches_context,
    _session_owner_kwargs,
    _session_owner_kwargs_from_session,
    _workdir_name_from_path,
    _workspace_context_from_request,
)
from .message_resources import (
    _build_message_resource_context,
)
from .agent_selection import normalize_primary_agent_name
from .chat_setup import _record_agent_mode_switch
from .input_limits import ChatInputLimitError, validate_chat_message
from .plugin_warmup import plugin_bootstrap_wait_seconds, wait_for_plugin_warmup
from .schemas import (
    BootstrapSessionRequest,
    SideQuestionRequest,
    SubagentChatRequest,
    TeamChatRequest,
    TeamPeerSettingsRequest,
)
from .state import ServerState
from .side_chat_context import build_side_chat_context
from .session_lifecycle import _SESSION_DELETE_PENDING
from .side_chat_injection import make_side_chat_import, promote_side_chat_artifacts, rewrite_paths
from ..message_context import is_real_user_message, side_chat_import_message

router = APIRouter()


class WorkerExecutionControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['pause', 'resume']
    tokens: Optional[int] = Field(default=None, gt=0, strict=True)
    tool_calls: Optional[int] = Field(default=None, gt=0, strict=True)


@router.post('/api/subagents/{session_id}/tasks/{task_id}/control')
async def control_worker_execution(session_id: str, task_id: str,
                                   payload: WorkerExecutionControl, request: Request):
    from ..team.coordinator import worker_task_active, notify_worker_control
    owner = _session_owner_kwargs(_workspace_context_from_request(request))
    session = await SessionManager.get(session_id, **owner)
    if session is None:
        raise HTTPException(404, 'Session not found')
    record = RuntimeTaskManager.get(task_id)
    if record is None or record.get('worker_session_id') != session_id:
        raise HTTPException(404, 'Worker task not found')
    if not worker_task_active(session_id):
        raise HTTPException(409, '执行现场已结束或服务已重启，不能原地恢复；保留已有进度，不自动重跑。')
    try:
        record = RuntimeTaskManager.control_execution(task_id, action=payload.action,
            limits={key: getattr(payload, key) for key in ('tokens', 'tool_calls')
                    if getattr(payload, key) is not None})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    notify_worker_control(task_id)
    return {'task': {key: value for key, value in record.items() if key != 'claim_token'}}


@router.get("/api/sessions/{parent_session_id}/side-chats")
async def list_side_chats(parent_session_id: str, request: Request):
    """Durable direct children are the open tabs, not a workspace-wide UI cache."""
    owner = _session_owner_kwargs(_workspace_context_from_request(request))
    if not await SessionManager.get(parent_session_id, **owner):
        raise HTTPException(404, "Parent session not found")
    children = await SessionManager.list(parent_id=parent_session_id, **owner)
    return {"side_chats": [{
        "session_id": child.id, "parent_session_id": parent_session_id,
        "agent_name": child.agent_name, "title": child.title,
        "status": "running" if _is_chat_session_busy(child.id) else "idle",
        "created_at": child.created_at, "side_question": True,
        "message_count": sum(m.role in {"user", "assistant"} for m in child.messages),
        "last_message": next((m.content for m in reversed(child.messages) if m.role in {"user", "assistant"}), ""),
    } for child in sorted(children, key=lambda s: s.created_at)
        if (child.metadata or {}).get("side_question") and child.id not in _SESSION_DELETE_PENDING]}


@router.post("/api/sessions/{parent_session_id}/side-chats/{side_session_id}/inject")
async def inject_side_chat(parent_session_id: str, side_session_id: str, request: Request):
    owner = _session_owner_kwargs(_workspace_context_from_request(request))
    parent = await SessionManager.get(parent_session_id, **owner)
    side = await SessionManager.get(side_session_id, **owner)
    if not parent or not side or side.parent_id != parent_session_id or not (side.metadata or {}).get("side_question"):
        raise HTTPException(404, "Side chat not found")
    if side_session_id in _SESSION_DELETE_PENDING or parent_session_id in _SESSION_DELETE_PENDING:
        raise HTTPException(409, "会话正在关闭，不能注入")
    if _is_chat_session_busy(side.id):
        raise HTTPException(409, "侧聊完成后可以注入完整上下文")
    # Bring back cold history as well, so compaction does not silently turn
    # "whole conversation" into only the last reply. Durable storage and model
    # context assembly remain separate; the normal builder budgets this later.
    history = {}
    revisions = await asyncio.to_thread(RevisionArchiveStore.search, side.id, owner=owner, limit=None, workdir=ServerState.get_workdir())
    for revision in reversed(revisions):
        archived = await asyncio.to_thread(RevisionArchiveStore.read, session_id=side.id, revision=revision["revision"], owner=owner, workdir=ServerState.get_workdir())
        if archived is None:
            raise HTTPException(409, "侧聊的历史归档暂不可读，未注入，请重试")
        for row in archived.get("messages", []):
            data = row.get("message_json", row)
            if isinstance(data, str):
                data = json.loads(data)
            message = SessionManager._message_from_dict(data)
            history[message.id] = message
    history.update({message.id: message for message in side.messages})
    raw_messages = _side_chat_snapshot(SimpleNamespace(id=side.id,messages=list(history.values())), public=False)["messages"]
    imports = (parent.metadata or {}).get("side_chat_imports") or {}
    transferred = {mid for record in imports.values() if record.get("side_session_id") == side.id for mid in record.get("source_message_ids", [])}
    messages = [message for message in raw_messages if message["id"] not in transferred]
    def user_directives(ids, artifacts):
        ids = set(ids)
        return [{"message_id": f"side:{side.id}:{message.id}",
            "instruction": rewrite_paths(message.content, side.workdir, artifacts["artifact_root"]),
        } for message in history.values() if message.id in ids and is_real_user_message(message)]
    if not messages:
        if transferred:
            records = [record for record in imports.values() if record.get("side_session_id") == side.id]
            record = dict(records[-1])
            if "user_directives" not in record:
                body = json.loads(record["content"].split("\n", 2)[2])
                record["user_directives"] = user_directives(record.get("source_message_ids", []), body["artifacts"])
                record["pending"] = True
                def upgrade(target):
                    target.metadata["side_chat_imports"][record["id"]] = record
                    for message in target.messages:
                        if message.id == record["id"]:
                            message.metadata = side_chat_import_message(record).metadata
                    return True
                if not await SessionManager.mutate(parent.id, upgrade, **owner):
                    raise HTTPException(404, "主会话已关闭，未注入上下文")
        else:
            raise HTTPException(400, "侧聊还没有内容可以注入")
    else:
        version = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        try:
            artifacts = await asyncio.to_thread(promote_side_chat_artifacts, side, parent, version)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, f"资料转入未完成：{exc}") from exc
        messages = rewrite_paths(messages, side.workdir, artifacts["artifact_root"])
        record = make_side_chat_import(side, messages, artifacts)
        record["source_message_ids"] = [message["id"] for message in messages]
        record["user_directives"] = user_directives(record["source_message_ids"], artifacts)
        def save(target):
            ledger = target.metadata.setdefault("side_chat_imports", {})
            if record["id"] not in ledger:
                ledger[record["id"]] = record
                target.messages.append(side_chat_import_message(record))
            return True
        if not await SessionManager.mutate(parent.id, save, **owner):
            raise HTTPException(404, "主会话已关闭，未注入上下文")
    guidance = None
    if _is_chat_session_busy(parent.id) and not record.get("guidance_id"):
        from .chat_control_api import guide_chat
        from .schemas import ChatGuideRequest
        guidance = await guide_chat(ChatGuideRequest(
            session_id=parent.id,
            message="侧聊的完整提问、回复和执行证据已注入当前会话。请结合这些已完成的讨论及其中的新需求，继续调整当前任务；不要重复执行已完成的操作。",
            mode="steer", client_request_id=record["id"], descendant_policy="preserve",
        ), request)
        if isinstance(guidance, JSONResponse):
            guidance = json.loads(guidance.body)
        def remember(target):
            target.metadata["side_chat_imports"][record["id"]]["guidance_id"] = guidance.get("guidance_id") or record["id"]
        await SessionManager.mutate(parent.id, remember, **owner)
    return {"status": "injected", "mode": "guidance" if guidance else "context", "message_count": len(messages), "duplicate": not bool(messages), "guidance": guidance}


def _side_chat_snapshot(session, *, public=True):
    return {
        "type": "side_chat_snapshot", "session_id": session.id,
        "messages": [{
            "id": m.id, "role": m.role, "content": m.content,
            "created_at": m.created_at,
            "presentation": getattr(m, "presentation", None),
            "content_blocks": getattr(m, "content_blocks", []) or [],
            "input_images": getattr(m, "input_images", []) or [],
            "research_grounding": getattr(m, "research_grounding", {}) or {},
            "process_notes": getattr(m, "process_notes", []) or [],
            "tool_calls": [_serialize_frontend_tool_call(tc) for tc in (m.tool_calls or [])],
        } for m in session.messages if not (m.metadata or {}).get("side_chat_context")
            and (not public or not _is_internal_frontend_message(m))],
        "runtime": _chat_runtime_status_payload(session.id),
        "question": ServerState.pending_question_request(session.id),
    }


@router.get("/api/subagents/{session_id}/events")
async def side_chat_events(session_id: str, request: Request):
    owner = _session_owner_kwargs(_workspace_context_from_request(request))
    session = await SessionManager.get(session_id, **owner)
    if not session or not (session.metadata or {}).get("side_question"):
        raise HTTPException(404, "Side chat not found")

    async def stream():
        queue = asyncio.Queue(maxsize=256)
        async def enqueue(event):
            # ponytail: bounded live queue; snapshots reconcile a slow reader.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        unsubscribe = ServerState.subscribe_session_events(session_id, enqueue)
        try:
            current = await SessionManager.get(session_id, **owner)
            if current is None:
                return
            yield "data: " + json.dumps(_side_chat_snapshot(current), ensure_ascii=False) + "\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Reconcile missed events and recover a pending clarification.
                    current = await SessionManager.get(session_id, **owner)
                    if current is None:
                        yield 'data: {"type":"side_chat_deleted"}\n\n'
                        return
                    event = _side_chat_snapshot(current)
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        finally:
            unsubscribe()
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _start_side_chat_turn(session, parent_id, message, *, owner, output_format=None, outcome_contract=None):
    """Reserve only this tab before yielding; hiding the inspector keeps it running."""
    ServerState.set_running(session.id, True)
    ServerState.clear_session_cancelled(session.id)
    async def run():
        try:
            result = await _run_subagent_turn_background(
                session.id, parent_id, message, integrate_to_parent=False,
                output_format=output_format, outcome_contract=outcome_contract, owner_kwargs=owner,
            )
            await ServerState.send_session_event(session.id, {"type": "side_chat_result", **result})
        except asyncio.CancelledError:
            await ServerState.send_session_event(session.id, {"type": "side_chat_result", "status": "cancelled", "session_id": session.id})
        except Exception as exc:
            await ServerState.send_session_event(session.id, {"type": "side_chat_result", "status": "failed", "session_id": session.id, "error": str(exc)})
        finally:
            ServerState.set_running(session.id, False)
            if ServerState.get_turn_task(session.id) is asyncio.current_task():
                ServerState.set_turn_task(session.id, None)
            current = await SessionManager.get(session.id, **owner)
            if current is not None:
                await ServerState.send_session_event(session.id, _side_chat_snapshot(current))
    task = asyncio.create_task(run())
    ServerState.set_turn_task(session.id, task)
    return {"status": "running", "session_id": session.id, "agent_name": session.agent_name, "running": True}


@router.get("/api/subagents")
async def list_subagents(request: Request, parent_session_id: str = Query(...)):
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    parent = await SessionManager.get(parent_session_id, **session_owner)
    if not parent:
        raise HTTPException(404, "Parent session not found")
    active_team_key = await _active_fixed_team_key_for_session(parent_session_id)
    descendants = await _list_descendant_sessions_recursive(
        parent_session_id,
        owner_kwargs=session_owner,
        include_side_chats=False,
    )
    depth_by_session_id = {parent_session_id: 0}
    lineage_by_session_id: dict[str, list[str]] = {
        parent_session_id: [parent_session_id]
    }
    sub_sessions = [
        session
        for session in descendants
        if _session_matches_active_fixed_team(
            session.id,
            team_key=active_team_key,
            worker_name=str(getattr(WorkerRegistry.get(session.id), "name", "") or ""),
            agent_name=str(getattr(session, "agent_name", "") or ""),
        )
    ]
    subagent_payloads = []
    latest_runtime_by_worker: dict[str, dict] = {}
    for task in RuntimeTaskManager.list_for_session(parent_session_id):
        worker_session_id = str(task.get("worker_session_id") or "").strip()
        if worker_session_id and worker_session_id not in latest_runtime_by_worker:
            latest_runtime_by_worker[worker_session_id] = task
    for s in sub_sessions:
        direct_parent_id = str(getattr(s, "parent_id", "") or "").strip()
        parent_depth = depth_by_session_id.get(direct_parent_id, 0)
        depth_by_session_id[s.id] = parent_depth + 1
        parent_lineage = lineage_by_session_id.get(
            direct_parent_id,
            [parent_session_id],
        )
        lineage_by_session_id[s.id] = [*parent_lineage, s.id]
        worker = WorkerRegistry.get(s.id)
        runtime_task = latest_runtime_by_worker.get(s.id)
        runtime = _chat_runtime_status_payload(s.id)
        metadata = getattr(s, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        catalog_worker = None
        if worker is not None:
            if active_team_key:
                catalog_worker = TeamCatalog.get_worker(active_team_key, worker.name)
            else:
                catalog_worker = TeamCatalog.get_worker_by_session(s.id, worker.name)
        subagent_payloads.append(
            {
                "session_id": s.id,
                "parent_session_id": direct_parent_id,
                "root_parent_session_id": parent_session_id,
                "depth": depth_by_session_id[s.id],
                "lineage": lineage_by_session_id[s.id],
                "agent_name": s.agent_name,
                "title": s.title,
                "status": _resolve_subagent_display_status(s, worker=worker, runtime=runtime),
                "created_at": s.created_at,
                "message_count": len(s.messages or []),
                "last_message": (s.messages[-1].content if s.messages else ""),
                "deletable": getattr(worker, "deletable", True),
                "running": bool(runtime.get("running")),
                "pending_guidance_count": int(runtime.get("pending_guidance_count", 0) or 0),
                "guidance_worker_active": bool(runtime.get("guidance_worker_active")),
                "worker_status": str(getattr(worker, "status", "") or "").strip().lower(),
                "lifecycle_status": str((runtime_task or {}).get("lifecycle_status") or "available"),
                "current_runtime_task": (
                    {
                        key: value
                        for key, value in runtime_task.items()
                        if key not in {"claim_token"}
                    }
                    if runtime_task
                    else None
                ),
                "goal_ancestry": list((runtime_task or {}).get("goal_ancestry") or []),
                "budget": dict((runtime_task or {}).get("budget") or {}),
                "team_worker": catalog_worker.to_api_dict() if catalog_worker else None,
                "resident_worker": bool(getattr(catalog_worker, "resident", False)),
                "worker_visible": True if catalog_worker is None else bool(catalog_worker.visible),
                "worker_enabled": True if catalog_worker is None else bool(catalog_worker.enabled),
                "persisted_status": s.status,
                "side_question": bool(metadata.get("side_question")),
                "source_parent_agent": str(metadata.get("source_parent_agent") or ""),
                **_extract_subagent_details(s),
            }
        )
    return {
        "parent_session_id": parent_session_id,
        "subagents": subagent_payloads,
    }


@router.post("/api/sessions/bootstrap")
async def bootstrap_session(payload: BootstrapSessionRequest, request: Request):
    workspace_context = _workspace_context_from_request(
        request,
        workdir_name=str(payload.workdir_name or ""),
    )
    session_owner = _session_owner_kwargs(workspace_context)
    session_workspace_owner = _session_owner_kwargs(
        workspace_context,
        include_workspace=True,
    )
    requested_agent_name = normalize_primary_agent_name(payload.agent_name) or "build"
    await wait_for_plugin_warmup(
        ServerState.get_workdir(),
        timeout=plugin_bootstrap_wait_seconds(),
    )
    agent_name = TeamCatalog.resolve_bootstrap_agent_name(requested_agent_name) or "build"
    session_id = str(payload.session_id or uuid.uuid4())
    persisted = await SessionManager.get(session_id, **session_owner)
    previous_agent_name = str(
        (
            ServerState.sessions.get(session_id, {}).get("agent_name")
            if session_id in ServerState.sessions
            and _runtime_session_matches_context(
                ServerState.sessions.get(session_id),
                workspace_context,
            )
            else ""
        )
        or getattr(persisted, "agent_name", "")
        or ""
    ).strip()
    resolved_workdir = ""
    if str(payload.workdir_name or "").strip() or str(payload.workdir_path or "").strip():
        resolved_workdir = _resolve_workdir_from_name_or_path(
            workdir_name=str(payload.workdir_name or ""),
            workdir_path=str(payload.workdir_path or ""),
            context=workspace_context,
        )
    elif persisted:
        resolved_workdir = (
            str(getattr(persisted, "workdir", "") or "").strip()
            or await _resolve_session_workdir(session_id, workspace_context)
        )
    else:
        resolved_workdir = await _resolve_session_workdir(session_id, workspace_context)

    session = ServerState.get_or_create_session(
        session_id,
        agent_name,
        workdir=resolved_workdir,
    )
    session["agent_name"] = agent_name
    session["workdir"] = resolved_workdir
    session["tenant_id"] = workspace_context.tenant_id
    session["user_id"] = workspace_context.user_id
    session["workspace_id"] = workspace_context.workspace_id

    if not persisted:
        await SessionManager.create(
            session_id=session_id,
            parent_id=None,
            title="New Chat",
            agent_name=agent_name,
            workdir=resolved_workdir,
            tenant_id=workspace_context.tenant_id,
            user_id=workspace_context.user_id,
            workspace_id=workspace_context.workspace_id,
        )
    else:
        existing_workdir = str(getattr(persisted, "workdir", "") or "").strip()
        if existing_workdir and existing_workdir != resolved_workdir:
            raise HTTPException(409, "session workdir is immutable")
        await SessionManager.update(
            session_id,
            **session_workspace_owner,
            agent_name=agent_name,
            workdir=existing_workdir or resolved_workdir,
        )

    if previous_agent_name and previous_agent_name != agent_name:
        await _record_agent_mode_switch(
            session_id,
            from_agent=previous_agent_name,
            to_agent=agent_name,
            owner_kwargs=session_workspace_owner,
        )
    elif previous_agent_name and TeamCatalog.get_by_agent_name(agent_name, available_only=False):
        refreshed = await SessionManager.get(session_id, **session_owner)
        metadata = dict(getattr(refreshed, "metadata", {}) or {})
        last_switch = metadata.get("last_mode_switch")
        last_switch_to = (
            str((last_switch or {}).get("to") or "").strip()
            if isinstance(last_switch, dict)
            else ""
        )
        if last_switch_to != agent_name:
            await _record_agent_mode_switch(
                session_id,
                from_agent="previous-context",
                to_agent=agent_name,
                force=True,
                owner_kwargs=session_workspace_owner,
            )

    created_count = await _bootstrap_fixed_team_workers(session_id, agent_name)

    return {
        "session_id": session_id,
        "agent_name": agent_name,
        "workdir": resolved_workdir,
        "workdir_name": _workdir_name_from_path(resolved_workdir, workspace_context),
        "tenant_id": workspace_context.tenant_id,
        "user_id": workspace_context.user_id,
        "workspace_id": workspace_context.workspace_id,
        "workers_created": created_count,
    }


@router.post("/api/subagents/chat")
async def chat_with_subagent(payload: SubagentChatRequest, request: Request):
    try:
        message = validate_chat_message(payload.message)
    except ChatInputLimitError as exc:
        raise HTTPException(413, detail=exc.as_websocket_event()) from exc
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    parent_session = await SessionManager.get(payload.parent_session_id, **session_owner)
    if not parent_session:
        raise HTTPException(404, "Parent session not found")
    if payload.parent_session_id in _SESSION_DELETE_PENDING:
        raise HTTPException(409, "主会话正在关闭")
    sub_session = await SessionManager.get(payload.subagent_session_id, **session_owner)
    if not sub_session:
        raise HTTPException(404, "Subagent session not found")
    if sub_session.id in _SESSION_DELETE_PENDING:
        raise HTTPException(409, "聊天正在关闭")

    ancestor_ids = await _collect_session_ancestor_ids(
        sub_session.id,
        owner_kwargs=session_owner,
    )
    if payload.parent_session_id not in ancestor_ids:
        raise HTTPException(403, "Subagent does not belong to parent session")
    execution_parent_session_id = str(sub_session.parent_id or "").strip()
    if not execution_parent_session_id:
        raise HTTPException(409, "Subagent session has no execution parent")
    sub_session_owner = _session_owner_kwargs_from_session(sub_session)

    if _is_chat_session_busy(sub_session.id):
        raise HTTPException(409, "Subagent is already busy; use guidance to continue")

    metadata = getattr(sub_session, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get("side_question"):
        # The tab may have been opened before the main conversation advanced.
        # Refresh its background at each turn without copying raw tool history.
        try:
            snapshot, context_message = build_side_chat_context(
                parent_session, source_message_id=(metadata.get("side_chat_context") or {}).get("source_message_id", ""),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        metadata = {**metadata, "side_chat_context": snapshot}
        updates = {"metadata": metadata}
        if BuiltinAgents.get(parent_session.agent_name):
            updates["agent_name"] = parent_session.agent_name
        if not any(m.role == "user" for m in sub_session.messages):
            updates["title"] = f"侧边聊天 · {message[:36]}"
            updates["messages"] = [context_message]
        await SessionManager.update(sub_session.id, **sub_session_owner, **updates)
    if payload.background:
        if not metadata.get("side_question"):
            raise HTTPException(400, "Background side chat requires a side-question session")
        return _start_side_chat_turn(
            sub_session, execution_parent_session_id, message, owner=sub_session_owner,
            output_format=payload.output_format, outcome_contract=payload.outcome_contract,
        )
    integrate_to_parent = payload.integrate_to_parent
    if integrate_to_parent is None:
        integrate_to_parent = not bool(metadata.get("side_question"))

    try:
        result = await _run_subagent_turn_background(
            sub_session.id,
            execution_parent_session_id,
            message,
            output_format=payload.output_format,
            outcome_contract=payload.outcome_contract,
            integrate_to_parent=bool(integrate_to_parent),
            owner_kwargs=sub_session_owner,
        )
        if result.get("status") == "failed":
            raise HTTPException(500, str(result.get("error") or "Subagent chat failed"))
        return {
            **result,
            **_chat_runtime_status_payload(sub_session.id),
        }
    finally:
        _ensure_session_guidance_drain_v2(sub_session.id)


@router.post("/api/subagents/side-question")
async def ask_side_question(payload: SideQuestionRequest, request: Request):
    try:
        message = validate_chat_message(payload.message)
    except ChatInputLimitError as exc:
        raise HTTPException(413, detail=exc.as_websocket_event()) from exc
    if not message.strip() and not payload.create_only:
        raise HTTPException(400, "message is required")
    workspace_context = _workspace_context_from_request(request)
    session_owner = _session_owner_kwargs(workspace_context)
    parent_session = await SessionManager.get(payload.parent_session_id, **session_owner)
    if not parent_session:
        raise HTTPException(404, "Parent session not found")
    if parent_session.id in _SESSION_DELETE_PENDING:
        raise HTTPException(409, "主会话正在关闭")

    if getattr(parent_session, "parent_id", None) and _is_chat_session_busy(parent_session.id):
        raise HTTPException(409, "Running subagent cannot open a side question; use guidance or re-ask")

    parent_context = _workspace_context_from_request(
        None,
        tenant_id=str(getattr(parent_session, "tenant_id", "") or ""),
        user_id=str(getattr(parent_session, "user_id", "") or ""),
        workspace_id=str(getattr(parent_session, "workspace_id", "") or ""),
    )
    parent_agent_name = str(getattr(parent_session, "agent_name", "") or "").strip()
    agent_name = parent_agent_name if BuiltinAgents.get(parent_agent_name) else "general"

    side_session_id = f"side-{payload.parent_session_id[:8]}-{uuid.uuid4().hex[:8]}"
    title = f"侧边聊天 · {message[:36]}" if message.strip() else "侧边聊天"

    parent_metadata = dict(getattr(parent_session, "metadata", {}) or {})
    parent_runtime = ServerState.get_or_create_session(
        parent_session.id,
        agent_name=parent_agent_name or "build",
    )
    resource_context = _merge_resource_context(
        parent_runtime.get("resource_context"),
        parent_metadata.get("resource_context"),
    )
    side_workdir = str(
        getattr(parent_session, "workdir", "")
        or await _resolve_session_workdir(payload.parent_session_id, parent_context)
    )
    try:
        incoming_resource_context = _build_message_resource_context(
            message,
            session_id=side_session_id,
            workdir=side_workdir,
            file_refs=payload.file_refs,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    resource_context = _merge_resource_context(
        resource_context,
        incoming_resource_context,
    )

    try:
        context_snapshot, context_message = build_side_chat_context(parent_session, source_message_id=payload.source_message_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        workspace = await asyncio.to_thread(
            AgentWorkspaceManager.create, control_workdir=side_workdir,
            task_id=side_session_id, session_id=side_session_id, kind="side_chat",
            strategy="protein_run", domain="side_chat", agent_name=agent_name,
            resource_context=resource_context,
        )
        if not workspace.isolated:
            raise ValueError("侧聊需要独立临时工作区，不能使用共享目录")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    side_workdir = workspace.execution_workdir
    resource_context = workspace.resource_context
    child_session = await SessionManager.create(
        session_id=side_session_id,
        parent_id=payload.parent_session_id,
        title=title,
        agent_name=agent_name,
        workdir=side_workdir,
        tenant_id=parent_context.tenant_id,
        user_id=parent_context.user_id,
        workspace_id=parent_context.workspace_id,
        metadata={
            "resource_context": resource_context,
            "side_question": True,
            "display_name": "侧边聊天",
            "source_parent_agent": parent_agent_name,
            "side_chat_context": context_snapshot,
            "agent_workspace": workspace.to_dict(),
        },
    )
    await SessionManager.update(child_session.id, **_session_owner_kwargs_from_session(child_session), messages=[context_message])

    child_runtime = ServerState.get_or_create_session(
        child_session.id,
        agent_name=agent_name,
        scheduler_mode="supervisor",
        workdir=str(getattr(child_session, "workdir", "") or ""),
    )
    child_runtime["tenant_id"] = parent_context.tenant_id
    child_runtime["user_id"] = parent_context.user_id
    child_runtime["workspace_id"] = parent_context.workspace_id
    if resource_context:
        child_runtime["resource_context"] = dict(resource_context)

    if payload.create_only:
        return {"status": "idle", "session_id": child_session.id, "parent_session_id": parent_session.id, "agent_name": agent_name, "running": False}

    child_owner = _session_owner_kwargs_from_session(child_session)
    try:
        result = await _run_subagent_turn_background(
            child_session.id,
            payload.parent_session_id,
            message,
            output_format=payload.output_format,
            outcome_contract=payload.outcome_contract,
            integrate_to_parent=False,
            owner_kwargs=child_owner,
        )
        if result.get("status") == "failed":
            raise HTTPException(500, str(result.get("error") or "Side question failed"))
        return {
            **result,
            "parent_session_id": payload.parent_session_id,
            **_chat_runtime_status_payload(child_session.id),
        }
    finally:
        _ensure_session_guidance_drain_v2(child_session.id)


@router.get("/api/team/{team_id}/workers")
async def get_team_workers(team_id: str):
    """List all workers created by a manager session."""
    active_team_key = await _active_fixed_team_key_for_session(team_id)
    workers = [
        worker
        for worker in WorkerRegistry.list_for_team(team_id)
        if _session_matches_active_fixed_team(
            worker.session_id,
            team_key=active_team_key,
            worker_name=worker.name,
            agent_name=f"worker_{worker.name}",
        )
    ]
    return {
        "team_id": team_id,
        "workers": [
            (
                lambda runtime, catalog_worker: {
                    **asdict(w),
                    "status": _resolve_worker_display_status(w, runtime=runtime),
                    "running": bool(runtime.get("running")),
                    "pending_guidance_count": int(runtime.get("pending_guidance_count", 0) or 0),
                    "guidance_worker_active": bool(runtime.get("guidance_worker_active")),
                    "profile": worker_profile_summary(w),
                    "team_worker": catalog_worker.to_api_dict() if catalog_worker else None,
                    "resident_worker": bool(getattr(catalog_worker, "resident", False)),
                    "visible": True if catalog_worker is None else bool(catalog_worker.visible),
                    "enabled": True if catalog_worker is None else bool(catalog_worker.enabled),
                }
            )(
                _chat_runtime_status_payload(w.session_id),
                TeamCatalog.get_worker(active_team_key, w.name)
                if active_team_key
                else TeamCatalog.get_worker_by_session(w.session_id, w.name),
            )
            for w in workers
        ],
    }


@router.get("/api/team/{team_id}/events")
async def get_team_events(team_id: str, limit: int = Query(default=100)):
    """Get recent channel events for a team."""
    channel = TeamChannel.get_or_create(team_id, ServerState.get_workdir())
    events = channel.history(limit=limit)
    return {"team_id": team_id, "events": [asdict(e) for e in events]}


@router.get("/api/team/{team_id}/peer-settings")
async def get_team_peer_settings_api(team_id: str):
    return {"team_id": team_id, **(await get_team_peer_settings(team_id))}


@router.post("/api/team/{team_id}/peer-settings")
async def update_team_peer_settings_api(team_id: str, request: TeamPeerSettingsRequest):
    return {"team_id": team_id, **(await set_team_peer_enabled(team_id, bool(request.enabled)))}


@router.get("/api/team/{team_id}/peer-threads")
async def get_team_peer_threads_api(team_id: str):
    return {
        "team_id": team_id,
        "threads": await list_peer_threads(team_id, ServerState.get_workdir()),
    }


@router.post("/api/team/{team_id}/chat")
async def team_chat(team_id: str, payload: TeamChatRequest, request: Request):
    """Send a direct message to a named worker in a team."""
    try:
        message = validate_chat_message(payload.message)
    except ChatInputLimitError as exc:
        raise HTTPException(413, detail=exc.as_websocket_event()) from exc
    workspace_context = _workspace_context_from_request(request)
    request_owner = _session_owner_kwargs(workspace_context)
    parent_session = await SessionManager.get(team_id, **request_owner)
    if not parent_session:
        raise HTTPException(404, "Team session not found")
    worker = WorkerRegistry.get_by_name(team_id, payload.worker_name.lower())
    if not worker:
        raise HTTPException(404, f"Worker '{payload.worker_name}' not found in team {team_id}")
    active_team_key = await _active_fixed_team_key_for_session(team_id)
    if active_team_key and not TeamCatalog.worker_is_enabled(active_team_key, worker.name):
        raise HTTPException(404, f"Worker '{payload.worker_name}' is disabled for this team")

    session_owner = _session_owner_kwargs_from_session(parent_session)
    sub_session = await SessionManager.get(worker.session_id, **session_owner)
    if not sub_session:
        raise HTTPException(404, "Worker session not found")

    if _is_chat_session_busy(sub_session.id):
        raise HTTPException(409, "Worker is already busy; use guidance to continue")

    try:
        result = await _run_subagent_turn_background(
            sub_session.id,
            team_id,
            message,
            output_format=payload.output_format,
            outcome_contract=payload.outcome_contract,
            owner_kwargs=session_owner,
        )
        if result.get("status") == "failed":
            raise HTTPException(500, str(result.get("error") or "Worker chat failed"))
        return {
            "session_id": sub_session.id,
            "worker_name": payload.worker_name,
            "response": result.get("response", ""),
            "status": result.get("status", "completed"),
            **_chat_runtime_status_payload(sub_session.id),
        }
    finally:
        _ensure_session_guidance_drain_v2(sub_session.id)
