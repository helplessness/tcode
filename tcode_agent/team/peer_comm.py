"""Formal single-round peer collaboration protocol with manager visibility."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..session.manager import SessionManager
from ..types import Message
from .channel import TeamChannel
from .protein_team_config import get_peer_policy
from .registry import WorkerRegistry


PEER_ENABLED_KEY = "protein_peer_communication_enabled"
_peer_tasks: dict[str, asyncio.Task] = {}


def _now_ts() -> float:
    return float(datetime.now().timestamp())


def _storage_path(team_id: str, workdir: str) -> Path:
    return Path(workdir) / ".tcode" / "teams" / f"{team_id}_peer_threads.json"


def _load_threads(team_id: str, workdir: str) -> list[dict[str, Any]]:
    path = _storage_path(team_id, workdir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _save_threads(team_id: str, workdir: str, threads: list[dict[str, Any]]) -> None:
    path = _storage_path(team_id, workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(threads, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_thread(team_id: str, workdir: str, request_id: str, update: dict[str, Any]) -> dict[str, Any] | None:
    threads = _load_threads(team_id, workdir)
    found = None
    for item in threads:
        if str(item.get("request_id", "")) == request_id:
            item.update(update)
            item["updated_at"] = _now_ts()
            found = item
            break
    if found is not None:
        _save_threads(team_id, workdir, threads)
    return found


async def get_team_peer_settings(team_id: str) -> dict[str, Any]:
    policy = get_peer_policy()
    session = await SessionManager.get(team_id)
    metadata = getattr(session, "metadata", {}) if session else {}
    enabled = metadata.get(PEER_ENABLED_KEY)
    if not isinstance(enabled, bool):
        enabled = bool(policy["enabled_default"])
    return {
        "enabled": enabled,
        "enabled_default": bool(policy["enabled_default"]),
        "allowed_reasons": list(policy["allowed_reasons"]),
        "max_messages_per_pair_per_session": int(policy["max_messages_per_pair_per_session"]),
        "round_timeout_seconds": int(policy["round_timeout_seconds"]),
    }


async def set_team_peer_enabled(team_id: str, enabled: bool) -> dict[str, Any]:
    session = await SessionManager.get(team_id)
    metadata = dict(getattr(session, "metadata", {}) or {})
    metadata[PEER_ENABLED_KEY] = bool(enabled)
    if session:
        await SessionManager.update(
            team_id,
            metadata=metadata,
            last_active_at=_now_ts(),
        )
    try:
        from ..server.app import ServerState

        runtime = ServerState.sessions.get(team_id)
        if runtime is not None:
            runtime["peer_communication_enabled"] = bool(enabled)
    except Exception:
        pass
    settings = await get_team_peer_settings(team_id)
    settings["enabled"] = bool(enabled)
    return settings




def _count_pair_rounds(team_id: str, workdir: str, sender: str, target: str) -> int:
    names = {str(sender or "").strip().lower(), str(target or "").strip().lower()}
    count = 0
    for item in _load_threads(team_id, workdir):
        if str(item.get("status", "")) not in {"pending", "answered", "timed_out"}:
            continue
        if {str(item.get("sender_worker", "")).strip().lower(), str(item.get("target_worker", "")).strip().lower()} == names:
            count += 1
    return count


async def _append_system_message(session_id: str, content: str) -> None:
    session = await SessionManager.get(session_id)
    if not session:
        return
    next_messages = [*(getattr(session, "messages", []) or []), Message(role="system", content=content)]
    await SessionManager.update(
        session_id,
        messages=next_messages,
        last_active_at=_now_ts(),
    )

    try:
        from ..server.app import ServerState

        runtime_agent = ServerState.agents.get(session_id)
        if runtime_agent is not None:
            runtime_agent.messages = list(next_messages)

        runtime_session = ServerState.sessions.get(session_id)
        if runtime_session is not None:
            runtime_session["messages"] = list(next_messages)
    except Exception:
        pass


def _thread_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": str(item.get("request_id", "")),
        "team_id": str(item.get("team_id", "")),
        "sender_worker": str(item.get("sender_worker", "")),
        "target_worker": str(item.get("target_worker", "")),
        "reason": str(item.get("reason", "")),
        "question": str(item.get("question", "")),
        "status": str(item.get("status", "")),
        "created_at": float(item.get("created_at", 0.0) or 0.0),
        "updated_at": float(item.get("updated_at", 0.0) or 0.0),
        "answered_at": item.get("answered_at"),
        "timeout_at": item.get("timeout_at"),
        "round_index": int(item.get("round_index", 0) or 0),
        "max_rounds": int(item.get("max_rounds", 0) or 0),
        "response": item.get("response") or {},
    }


async def _mark_timeout(thread: dict[str, Any], workdir: str) -> dict[str, Any]:
    if str(thread.get("status", "")) != "pending":
        return thread
    if float(thread.get("timeout_at", 0.0) or 0.0) > _now_ts():
        return thread

    team_id = str(thread.get("team_id", ""))
    request_id = str(thread.get("request_id", ""))
    updated = _update_thread(
        team_id,
        workdir,
        request_id,
        {
            "status": "timed_out",
            "answered_at": _now_ts(),
            "response": {
                "summary": "协作轮次超时，未收到结构化答复。",
                "confidence": 0.0,
                "next_action": "请由 manager 决定是否重试、改派或人工介入。",
                "blockers": ["timeout"],
            },
        },
    ) or thread

    if not bool(updated.get("timeout_notified")):
        channel = TeamChannel.get_or_create(team_id, workdir)
        metadata = {
            "request_id": request_id,
            "sender_worker": updated.get("sender_worker", ""),
            "target_worker": updated.get("target_worker", ""),
            "manager_notified": True,
        }
        await channel.publish(
            source=f"worker:{updated.get('target_worker', '')}",
            target="manager",
            event_type="peer_timeout",
            content=str(updated.get("question", "")),
            metadata=metadata,
        )
        await _append_system_message(
            team_id,
            (
                "[Peer collaboration timeout]\n"
                f"{updated.get('sender_worker', '')} -> {updated.get('target_worker', '')}\n"
                f"request_id: {request_id}\n"
                "状态：超时未答复，请由 manager 决定下一步。"
            ),
        )
        updated = _update_thread(team_id, workdir, request_id, {"timeout_notified": True}) or updated
    return updated


async def list_peer_threads(team_id: str, workdir: str) -> list[dict[str, Any]]:
    threads = []
    for item in _load_threads(team_id, workdir):
        updated = await _mark_timeout(item, workdir)
        threads.append(_thread_preview(updated))
    return sorted(threads, key=lambda item: item.get("updated_at", 0), reverse=True)


async def _finalize_peer_answer(
    *,
    team_id: str,
    workdir: str,
    request_id: str,
    target_worker: str,
    summary: str,
    confidence: float,
    next_action: str,
    blockers: list[str],
) -> dict[str, Any]:
    threads = _load_threads(team_id, workdir)
    thread = next((item for item in threads if str(item.get("request_id", "")) == request_id), None)
    if not thread:
        raise ValueError(f"request_id '{request_id}' 不存在")

    thread = await _mark_timeout(thread, workdir)
    if str(thread.get("status", "")) != "pending":
        raise ValueError(f"request_id '{request_id}' 当前状态为 {thread.get('status', '')}，不能再答复")

    expected_target = str(thread.get("target_worker", "")).strip().lower()
    if str(target_worker or "").strip().lower() != expected_target:
        raise ValueError("只有目标 Worker 可以回答这条 peer question")

    answer_payload = {
        "summary": str(summary or "").strip(),
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "next_action": str(next_action or "").strip(),
        "blockers": [str(item).strip() for item in (blockers or []) if str(item).strip()],
    }
    if not answer_payload["summary"]:
        raise ValueError("summary 不能为空")

    updated = _update_thread(
        team_id,
        workdir,
        request_id,
        {
            "status": "answered",
            "answered_at": _now_ts(),
            "response": answer_payload,
        },
    )
    if not updated:
        raise ValueError(f"request_id '{request_id}' 更新失败")

    channel = TeamChannel.get_or_create(team_id, workdir)
    metadata = {
        "request_id": request_id,
        "sender_worker": updated.get("sender_worker", ""),
        "target_worker": expected_target,
        "reason": updated.get("reason", ""),
        "manager_notified": True,
    }
    summary_text = answer_payload["summary"]
    await channel.publish(
        source=f"worker:{expected_target}",
        target=f"worker:{updated.get('sender_worker', '')}",
        event_type="peer_answer",
        content=summary_text,
        metadata=metadata | {"response": answer_payload},
    )
    await channel.publish(
        source=f"worker:{expected_target}",
        target="manager",
        event_type="peer_answer_copy",
        content=summary_text,
        metadata=metadata | {"response": answer_payload},
    )

    sender = WorkerRegistry.get_by_name(team_id, str(updated.get("sender_worker", "")))
    sender_notice = (
        "[Peer answer]\n"
        f"from: {expected_target}\n"
        f"request_id: {request_id}\n"
        f"summary: {answer_payload['summary']}\n"
        f"confidence: {answer_payload['confidence']}\n"
        f"next_action: {answer_payload['next_action'] or '-'}\n"
        f"blockers: {', '.join(answer_payload['blockers']) if answer_payload['blockers'] else '-'}"
    )
    manager_notice = (
        "[Peer collaboration answered]\n"
        f"{updated.get('sender_worker', '')} <- {expected_target}\n"
        f"request_id: {request_id}\n"
        f"summary: {answer_payload['summary']}\n"
        f"confidence: {answer_payload['confidence']}\n"
        f"next_action: {answer_payload['next_action'] or '-'}"
    )
    if sender:
        await _append_system_message(sender.session_id, sender_notice)
    await _append_system_message(team_id, manager_notice)
    return _thread_preview(updated)


async def _auto_answer_question(
    *,
    team_id: str,
    target_session_id: str,
    request_id: str,
    workdir: str,
) -> None:
    task_key = f"{team_id}:{request_id}"
    try:
        target_worker = WorkerRegistry.get(target_session_id)
        if not target_worker or target_worker.status != "idle":
            return
        session = await SessionManager.get(target_session_id)
        if not session:
            return

        from ..agent import Agent
        from ..agent.agents import BuiltinAgents
        from ..providers import create_provider

        agent_info = BuiltinAgents.get(session.agent_name)
        if not agent_info:
            return

        threads = _load_threads(team_id, workdir)
        thread = next((item for item in threads if str(item.get("request_id", "")) == request_id), None)
        if not thread or str(thread.get("status", "")) != "pending":
            return

        await WorkerRegistry.update(
            target_session_id,
            status="busy",
            current_task=f"peer_question:{request_id}",
        )
        provider = create_provider(workdir=workdir)
        agent = Agent(
            agent_info=agent_info,
            provider=provider,
            session_id=target_session_id,
            workdir=workdir,
            existing_session=session,
        )
        prompt = (
            "你收到了一条单轮 peer collaboration 请求。"
            "请不要发起新的 peer question，不要调用 task/assign_task。"
            "你必须只做一件事：调用 answer_peer_question 一次，给出结构化答复。"
            "如果信息不足，也要回答，但要把 blockers 写清楚，并把 confidence 设低。\n\n"
            f"request_id: {request_id}\n"
            f"from_worker: {thread.get('sender_worker', '')}\n"
            f"reason: {thread.get('reason', '')}\n"
            f"question:\n{thread.get('question', '')}\n"
        )
        result = await agent.run(prompt)
        latest_messages = list(agent.messages)
        await SessionManager.update(
            target_session_id,
            messages=latest_messages,
            last_active_at=_now_ts(),
        )

        refreshed = _load_threads(team_id, workdir)
        latest = next((item for item in refreshed if str(item.get("request_id", "")) == request_id), None)
        if latest and str(latest.get("status", "")) == "pending":
            fallback_summary = str(getattr(result, "content", "") or "").strip()[:1200] or "已收到请求，但未自动生成结构化答复。"
            await _finalize_peer_answer(
                team_id=team_id,
                workdir=workdir,
                request_id=request_id,
                target_worker=target_worker.name,
                summary=fallback_summary,
                confidence=0.35,
                next_action="如需更精确回复，请由 manager 发起下一轮。",
                blockers=["tool_not_used"],
            )
    except Exception:
        pass
    finally:
        worker = WorkerRegistry.get(target_session_id)
        if worker and worker.status == "busy" and str(worker.current_task or "").startswith("peer_question:"):
            await WorkerRegistry.update(target_session_id, status="idle", current_task=None)
        _peer_tasks.pop(task_key, None)


async def ask_peer_question(
    *,
    sender_session_id: str,
    target_worker_name: str,
    question: str,
    reason: str,
    workdir: str,
) -> dict[str, Any]:
    sender = WorkerRegistry.get(sender_session_id)
    if not sender:
        raise ValueError("仅 Worker 会话可以发起 peer collaboration")

    team_id = sender.team_id
    target_name = str(target_worker_name or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    body = str(question or "").strip()

    if not target_name or not body:
        raise ValueError("target_worker 和 question 不能为空")
    if target_name == sender.name:
        raise ValueError("不能给自己发起 peer collaboration")

    target = WorkerRegistry.get_by_name(team_id, target_name)
    if not target or target.status == "terminated":
        raise ValueError(f"目标 Worker '{target_name}' 不存在或已终止")

    settings = await get_team_peer_settings(team_id)
    if not settings["enabled"]:
        raise ValueError("peer communication 当前已关闭")

    if normalized_reason not in settings["allowed_reasons"]:
        allowed = ", ".join(settings["allowed_reasons"])
        raise ValueError(f"reason 不合法，仅允许: {allowed}")

    whitelist = [str(item).strip().lower() for item in (sender.peer_whitelist or []) if str(item).strip()]
    if target_name not in whitelist:
        raise ValueError(f"'{target_name}' 不在 Worker '{sender.name}' 的白名单内")

    existing_pending = [
        item for item in _load_threads(team_id, workdir)
        if str(item.get("status", "")) == "pending"
        and str(item.get("sender_worker", "")).strip().lower() == sender.name
        and str(item.get("target_worker", "")).strip().lower() == target_name
    ]
    if existing_pending:
        raise ValueError("该 Worker 对之间已有未完成的协作轮次，请先等待答复或超时")

    rounds_used = _count_pair_rounds(team_id, workdir, sender.name, target_name)
    if rounds_used >= int(settings["max_messages_per_pair_per_session"]):
        raise ValueError(
            "已达到该 Worker 对之间的最大协作轮次限制 "
            f"({settings['max_messages_per_pair_per_session']})"
        )

    request_id = uuid.uuid4().hex
    timeout_at = _now_ts() + float(settings["round_timeout_seconds"])
    thread = {
        "request_id": request_id,
        "team_id": team_id,
        "sender_worker": sender.name,
        "target_worker": target_name,
        "reason": normalized_reason,
        "question": body,
        "status": "pending",
        "created_at": _now_ts(),
        "updated_at": _now_ts(),
        "timeout_at": timeout_at,
        "round_index": rounds_used + 1,
        "max_rounds": int(settings["max_messages_per_pair_per_session"]),
        "response": {},
        "timeout_notified": False,
    }
    threads = _load_threads(team_id, workdir)
    threads.append(thread)
    _save_threads(team_id, workdir, threads)

    channel = TeamChannel.get_or_create(team_id, workdir)
    metadata = {
        "request_id": request_id,
        "reason": normalized_reason,
        "sender_worker": sender.name,
        "target_worker": target_name,
        "round_index": rounds_used + 1,
        "max_rounds": int(settings["max_messages_per_pair_per_session"]),
        "timeout_at": timeout_at,
        "manager_notified": True,
    }
    await channel.publish(
        source=f"worker:{sender.name}",
        target=f"worker:{target_name}",
        event_type="peer_question",
        content=body,
        metadata=metadata,
    )
    await channel.publish(
        source=f"worker:{sender.name}",
        target="manager",
        event_type="peer_question_copy",
        content=body,
        metadata=metadata,
    )

    target_notice = (
        "[Peer collaboration request]\n"
        f"request_id: {request_id}\n"
        f"from: {sender.name}\n"
        f"reason: {normalized_reason}\n"
        f"timeout_at: {int(timeout_at)}\n"
        "请只使用 `answer_peer_question` 回复一次结构化答复。\n"
        f"question:\n{body}"
    )
    manager_notice = (
        "[Peer collaboration opened]\n"
        f"{sender.name} -> {target_name}\n"
        f"request_id: {request_id}\n"
        f"reason: {normalized_reason}\n"
        f"round: {rounds_used + 1}/{settings['max_messages_per_pair_per_session']}\n"
        f"question:\n{body}"
    )
    await _append_system_message(target.session_id, target_notice)
    await _append_system_message(team_id, manager_notice)

    if target.status == "idle":
        task_key = f"{team_id}:{request_id}"
        task = asyncio.create_task(
            _auto_answer_question(
                team_id=team_id,
                target_session_id=target.session_id,
                request_id=request_id,
                workdir=workdir,
            )
        )
        _peer_tasks[task_key] = task

    return _thread_preview(thread)


async def answer_peer_question(
    *,
    responder_session_id: str,
    request_id: str,
    summary: str,
    confidence: float,
    next_action: str,
    blockers: list[str],
    workdir: str,
) -> dict[str, Any]:
    responder = WorkerRegistry.get(responder_session_id)
    if not responder:
        raise ValueError("仅 Worker 会话可以回答 peer collaboration")
    return await _finalize_peer_answer(
        team_id=responder.team_id,
        workdir=workdir,
        request_id=request_id,
        target_worker=responder.name,
        summary=summary,
        confidence=confidence,
        next_action=next_action,
        blockers=blockers,
    )
