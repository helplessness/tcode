from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..agent import Agent
from ..agent.agents import BuiltinAgents
from ..config.manager import ConfigManager
from ..config.task_snapshot import TaskConfigSnapshotStore
from ..providers import create_provider
from ..providers.base import LLMProvider
from .plugin_warmup import trigger_plugin_event_background


def _same_resolved_workdir(left: str, right: str) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve(strict=False) == Path(right_text).expanduser().resolve(strict=False)
    except Exception:
        return left_text == right_text


class ServerState:
    sessions: Dict[str, Dict[str, Any]] = {}
    agents: Dict[str, Agent] = {}
    providers: Dict[str, LLMProvider] = {}
    session_event_subscribers: dict[str, dict[str, Callable]] = {}
    abort_events: Dict[str, asyncio.Event] = {}
    permission_futures: Dict[str, Dict[str, asyncio.Future]] = {}
    question_futures: Dict[str, Dict[str, asyncio.Future]] = {}
    question_requests: Dict[str, Dict[str, dict[str, Any]]] = {}
    _workdir: str = None
    _running: Dict[str, bool] = {}
    session_locks: Dict[str, asyncio.Lock] = {}
    guidance_tasks: Dict[str, asyncio.Task] = {}
    turn_tasks: Dict[str, asyncio.Task] = {}
    cancelled_session_ids: set[str] = set()
    active_chat_request_ids: Dict[str, str] = {}
    recent_chat_request_ids: Dict[str, list[str]] = {}
    @classmethod
    def get_workdir(cls) -> str:
        return cls._workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())

    @classmethod
    def get_session_tmp_dir(cls, session_id: str, create: bool = True) -> Path:
        # Frontend uploads are stored under upload/<session_id>.
        raw_id = str(session_id or "").strip()
        readable = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_id).strip(".-_")[:48]
        storage_key = f"{readable or 'session'}-{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}"
        runtime = cls.sessions.get(raw_id)
        session_workdir = (
            str(runtime.get("workdir") or "").strip()
            if isinstance(runtime, dict)
            else ""
        )
        upload_root = (Path(session_workdir or cls.get_workdir()) / "upload").resolve(
            strict=False
        )
        tmp_dir = (upload_root / storage_key).resolve(strict=False)
        try:
            tmp_dir.relative_to(upload_root)
        except ValueError as exc:  # defensive: storage_key must never escape
            raise ValueError("invalid session upload path") from exc
        if create:
            tmp_dir.mkdir(parents=True, exist_ok=True)
        return tmp_dir

    @classmethod
    def cleanup_session_tmp_dir(cls, session_id: str):
        tmp_dir = cls.get_session_tmp_dir(session_id, create=False)
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    @classmethod
    def get_or_create_session(
        cls,
        session_id: str,
        agent_name: Optional[str] = None,
        scheduler_mode: Optional[str] = None,
        execution_profile: Optional[str] = None,
        workdir: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        ConfigManager.load(cls.get_workdir())
        default_agent = ConfigManager.get_default_agent() or "build"
        if not BuiltinAgents.get(default_agent):
            default_agent = "build"

        normalized_scheduler = str(scheduler_mode or "supervisor").strip().lower()
        if normalized_scheduler not in {
            "supervisor",
            "chain",
            "swarm",
            "auto_swarm_builder",
        }:
            normalized_scheduler = "supervisor"
        try:
            from ..runtime.execution_profile import normalize_execution_profile

            normalized_execution_profile = normalize_execution_profile(execution_profile)
        except Exception:
            normalized_execution_profile = ""

        if session_id not in cls.sessions:
            cls.sessions[session_id] = {
                "session_id": session_id,
                "messages": [],
                "agent_name": agent_name or default_agent,
                "execution_profile": normalized_execution_profile or "standard",
                "scheduler_mode": normalized_scheduler,
                "workdir": str(workdir or cls.get_workdir()),
                **({"model": str(model).strip()} if str(model or "").strip() else {}),
            }
            try:
                session_workdir = str(
                    cls.sessions.get(session_id, {}).get("workdir") or cls.get_workdir()
                ).strip() or cls.get_workdir()
                trigger_plugin_event_background(
                    session_workdir,
                    "session.start",
                    {
                        "session_id": session_id,
                        "agent": cls.sessions[session_id]["agent_name"],
                        "workdir": session_workdir,
                    },
                    {},
                )
            except Exception:
                pass
        elif agent_name:
            cls.sessions[session_id]["agent_name"] = agent_name
        if scheduler_mode:
            cls.sessions[session_id]["scheduler_mode"] = normalized_scheduler
        if execution_profile and normalized_execution_profile:
            cls.sessions[session_id]["execution_profile"] = normalized_execution_profile
        if workdir:
            cls.sessions[session_id]["workdir"] = str(workdir)
        if model:
            normalized_model = str(model).strip()
            if str(cls.sessions[session_id].get("model") or "").strip() != normalized_model:
                # A task snapshot intentionally freezes runtime configuration.
                # A user-confirmed model switch must create a fresh snapshot;
                # otherwise the old snapshot silently wins over the request and
                # the cached provider continues using the prior model.
                cls.sessions[session_id]["config_snapshot_id"] = ""
            cls.sessions[session_id]["model"] = normalized_model
        return cls.sessions[session_id]

    @classmethod
    def get_or_create_agent(cls, session_id: str, *, hydrate: bool = True) -> Agent:
        session = cls.sessions.get(session_id, {})
        target_agent_name = session.get("agent_name", "build")
        session_workdir = str(session.get("workdir", "") or cls.get_workdir()).strip() or cls.get_workdir()
        execution_workdir = str(
            session.get("agent_execution_workdir", "") or session_workdir
        ).strip() or session_workdir
        from ..runtime.sandbox.base import strict_isolation_enabled

        if (
            strict_isolation_enabled()
            and not str(session.get("parent_id") or "").strip()
        ):
            from ..workspace.manager import AgentWorkspaceManager

            workspace = AgentWorkspaceManager.create(
                control_workdir=session_workdir,
                task_id=session_id,
                session_id=session_id,
                kind="main-agent",
                strategy="snapshot",
            )
            execution_workdir = workspace.execution_workdir
            session["agent_workspace_id"] = workspace.workspace_id
            session["agent_execution_workdir"] = execution_workdir
        requested_model_ref = str(session.get("model", "") or "").strip()
        snapshot_id = str(session.get("config_snapshot_id") or "")
        snapshot_overrides = (
            {"model": requested_model_ref} if requested_model_ref else None
        )
        snapshot = (
            TaskConfigSnapshotStore.capture(
                session_workdir,
                task_id=session_id,
                force=True,
                runtime_overrides=snapshot_overrides,
            )
            if requested_model_ref and not snapshot_id
            else TaskConfigSnapshotStore.for_task(
                session_id,
                session_workdir,
                snapshot_id=snapshot_id,
                runtime_overrides=snapshot_overrides,
            )
        )
        session_model_ref = str(
            snapshot.runtime_config.get("model") or requested_model_ref
        ).strip()
        session["model"] = session_model_ref
        session["config_snapshot_id"] = snapshot.snapshot_id
        session["config_snapshot_workdir"] = snapshot.workdir
        session["config_fingerprint"] = snapshot.config_fingerprint
        session["config_policy_fingerprint"] = snapshot.policy_fingerprint
        session["config_summary"] = dict(snapshot.summary)
        cached_agent = cls.agents.get(session_id)
        cached_name = str(
            getattr(getattr(cached_agent, "agent_info", None), "name", "") or ""
        ).strip()
        cached_workdir = str(getattr(cached_agent, "workdir", "") or "").strip()
        cached_model_ref = str(getattr(cached_agent, "_model_ref", "") or "").strip()
        should_recreate_agent = bool(
            cached_agent is not None
            and cached_name
            and (
                cached_name != target_agent_name
                or not _same_resolved_workdir(cached_workdir, execution_workdir)
                or cached_model_ref != session_model_ref
            )
        )
        if should_recreate_agent:
            try:
                print(
                    f"[ServerState] Recreating cached agent for session {session_id}: "
                    f"{cached_name}@{cached_workdir or '-'} -> {target_agent_name}@{execution_workdir}"
                )
            except Exception:
                pass
            provider = cls.providers.pop(session_id, None)
            cls.agents.pop(session_id, None)
            if provider is not None:
                close_fn = getattr(provider, "close", None)
                if close_fn:
                    try:
                        result = close_fn()
                        if hasattr(result, "__await__"):
                            asyncio.create_task(result)
                    except Exception:
                        pass
        if session_id not in cls.agents:
            with ConfigManager.bind_snapshot(
                snapshot.snapshot_id,
                snapshot.runtime_config,
                workdir=snapshot.workdir,
            ):
                provider = create_provider(
                    workdir=execution_workdir,
                    model_ref=session_model_ref or None,
                    session_id=session_id,
                )
                agent = Agent.create(
                    name=target_agent_name,
                    provider=provider,
                    session_id=session_id,
                    workdir=execution_workdir,
                )
            agent.bind_config_snapshot(snapshot)
            setattr(agent, "_model_ref", session_model_ref)
            cls.agents[session_id] = agent
            cls.providers[session_id] = provider
            
            # Hydrate agent with persisted messages from SessionManager
            try:
                import asyncio
                from ..session.manager import SessionManager
                
                # Use asyncio to load session if in async context
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                
                if loop and hydrate:
                    # We're in an async context but get_or_create_agent is sync
                    # Schedule the hydration as a task
                    async def _hydrate():
                        persisted = await SessionManager.get(session_id)
                        if persisted and getattr(persisted, "messages", None):
                            agent.messages = list(persisted.messages)
                            # Also sync to runtime session state for /api/sessions/{id}
                            if session_id in cls.sessions:
                                cls.sessions[session_id]["messages"] = list(persisted.messages)
                    
                    try:
                        asyncio.create_task(_hydrate())
                    except Exception:
                        pass
                elif hydrate:
                    # Fallback: try to load synchronously from store
                    from ..session.store_sqlite import SessionSQLiteStore
                    try:
                        SessionSQLiteStore.ensure_initialized()
                        persisted_data = SessionSQLiteStore.get(session_id)
                        if persisted_data and isinstance(persisted_data, dict):
                            messages_data = persisted_data.get("messages", [])
                            if messages_data:
                                from ..session.manager import SessionManager as SM
                                agent.messages = [
                                    SM._message_from_dict(m) if isinstance(m, dict) else m
                                    for m in messages_data
                                ]
                                # Sync to runtime state
                                if session_id in cls.sessions:
                                    cls.sessions[session_id]["messages"] = agent.messages
                    except Exception:
                        pass
            except Exception:
                pass
        
        cls.agents[session_id].bind_config_snapshot(snapshot)
        return cls.agents[session_id]

    @classmethod
    def is_running(cls, session_id: str) -> bool:
        return cls._running.get(session_id, False)

    @classmethod
    def set_running(cls, session_id: str, running: bool):
        cls._running[session_id] = running

    @classmethod
    def get_session_lock(cls, session_id: str) -> asyncio.Lock:
        lock = cls.session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            cls.session_locks[session_id] = lock
        return lock

    @classmethod
    def get_guidance_task(cls, session_id: str) -> Optional[asyncio.Task]:
        task = cls.guidance_tasks.get(session_id)
        if task and task.done():
            cls.guidance_tasks.pop(session_id, None)
            return None
        return task

    @classmethod
    def set_guidance_task(cls, session_id: str, task: Optional[asyncio.Task]) -> None:
        if task is None:
            cls.guidance_tasks.pop(session_id, None)
            return
        cls.guidance_tasks[session_id] = task

    @classmethod
    def get_turn_task(cls, session_id: str) -> Optional[asyncio.Task]:
        task = cls.turn_tasks.get(session_id)
        if task and task.done():
            cls.turn_tasks.pop(session_id, None)
            return None
        return task

    @classmethod
    def mark_session_cancelled(cls, session_id: str) -> None:
        normalized = str(session_id or "").strip()
        if normalized:
            cls.cancelled_session_ids.add(normalized)

    @classmethod
    def clear_session_cancelled(cls, session_id: str) -> None:
        cls.cancelled_session_ids.discard(str(session_id or "").strip())

    @classmethod
    def session_cancelled(cls, session_id: str) -> bool:
        return str(session_id or "").strip() in cls.cancelled_session_ids

    @classmethod
    def set_turn_task(cls, session_id: str, task: Optional[asyncio.Task]) -> None:
        if task is None:
            cls.turn_tasks.pop(session_id, None)
            return
        cls.turn_tasks[session_id] = task

    @classmethod
    def claim_chat_turn(
        cls,
        session_id: str,
        request_id: str,
        task: Optional[asyncio.Task],
    ) -> str:
        """Atomically claim a foreground chat turn within this server process."""

        normalized_session_id = str(session_id or "").strip()
        normalized_request_id = str(request_id or "").strip()[:160]
        if not normalized_session_id or task is None:
            return "invalid"

        current_task = cls.get_turn_task(normalized_session_id)
        if current_task is not None and not current_task.done():
            active_request_id = cls.active_chat_request_ids.get(normalized_session_id, "")
            if normalized_request_id and active_request_id == normalized_request_id:
                return "duplicate_active"
            return "session_busy"

        if normalized_request_id and normalized_request_id in cls.recent_chat_request_ids.get(
            normalized_session_id,
            [],
        ):
            return "duplicate_completed"

        cls.turn_tasks[normalized_session_id] = task
        cls._running[normalized_session_id] = True
        if normalized_request_id:
            cls.active_chat_request_ids[normalized_session_id] = normalized_request_id
        else:
            cls.active_chat_request_ids.pop(normalized_session_id, None)
        return "accepted"

    @classmethod
    def finish_chat_turn(
        cls,
        session_id: str,
        task: Optional[asyncio.Task],
    ) -> bool:
        """Release a chat claim without allowing an older task to clear a newer one."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return False
        current_task = cls.get_turn_task(normalized_session_id)
        if current_task is not task:
            return False

        request_id = cls.active_chat_request_ids.pop(normalized_session_id, "")
        cls.turn_tasks.pop(normalized_session_id, None)
        cls._running[normalized_session_id] = False
        if request_id:
            recent = cls.recent_chat_request_ids.setdefault(normalized_session_id, [])
            if request_id not in recent:
                recent.append(request_id)
            if len(recent) > 64:
                del recent[:-64]
            while len(cls.recent_chat_request_ids) > 2048:
                oldest_session_id = next(iter(cls.recent_chat_request_ids))
                cls.recent_chat_request_ids.pop(oldest_session_id, None)
        return True

    @classmethod
    async def cancel_session(cls, session_id: str, *, clear_guidance: bool = False):
        if session_id in cls.agents:
            agent = cls.agents[session_id]
            if hasattr(agent, "_abort_event"):
                agent._abort_event.set()
        runtime = cls.sessions.get(session_id)
        if isinstance(runtime, dict):
            workflow_task = runtime.pop("workflow_auto_continue_task", None)
            if workflow_task and hasattr(workflow_task, "done") and not workflow_task.done():
                workflow_task.cancel()
            workflow_tasks = runtime.pop("workflow_auto_continue_tasks", {})
            if isinstance(workflow_tasks, dict):
                for task in workflow_tasks.values():
                    if task and hasattr(task, "done") and not task.done():
                        task.cancel()
        turn_task = cls.get_turn_task(session_id)
        if turn_task and not turn_task.done():
            turn_task.cancel()
        guidance_task = cls.get_guidance_task(session_id)
        if guidance_task and not guidance_task.done():
            guidance_task.cancel()
        if clear_guidance:
            from ..session.manager import SessionManager
            from ._shared import _clear_session_guidance

            cls.guidance_tasks.pop(session_id, None)
            session = await SessionManager.get(session_id)
            if session is not None:
                await _clear_session_guidance(session)

    @classmethod
    async def cleanup_session(cls, session_id: str, cleanup_tmp_dir: bool = True):
        try:
            runtime = cls.sessions.get(session_id)
            runtime_data = runtime if isinstance(runtime, dict) else {}
            session_workdir = (
                str(runtime_data.get("workdir") or cls.get_workdir()).strip()
                or cls.get_workdir()
            )
            trigger_plugin_event_background(
                session_workdir,
                "session.end",
                {
                    "session_id": session_id,
                    "agent": str(runtime_data.get("agent_name") or ""),
                    "workdir": session_workdir,
                },
                {},
            )
        except Exception:
            pass

        cls._running[session_id] = False
        if session_id in cls.permission_futures:
            for future in cls.permission_futures[session_id].values():
                if not future.done():
                    future.set_result("reject")
            del cls.permission_futures[session_id]
        if session_id in cls.agents:
            if session_id in cls.providers:
                provider = cls.providers[session_id]
                close_fn = getattr(provider, "close", None)
                if close_fn:
                    result = close_fn()
                    if asyncio.iscoroutine(result):
                        await result
                del cls.providers[session_id]
            del cls.agents[session_id]
        if session_id in cls.question_futures:
            for future in cls.question_futures[session_id].values():
                if not future.done():
                    future.set_result("")
            del cls.question_futures[session_id]
        cls.question_requests.pop(session_id, None)
        if cleanup_tmp_dir:
            cls.cleanup_session_tmp_dir(session_id)
        if session_id in cls.sessions:
            runtime = cls.sessions[session_id]
            workflow_task = runtime.pop("workflow_auto_continue_task", None) if isinstance(runtime, dict) else None
            if workflow_task and hasattr(workflow_task, "done") and not workflow_task.done():
                workflow_task.cancel()
            workflow_tasks = runtime.pop("workflow_auto_continue_tasks", {}) if isinstance(runtime, dict) else {}
            if isinstance(workflow_tasks, dict):
                for task in workflow_tasks.values():
                    if task and hasattr(task, "done") and not task.done():
                        task.cancel()
            del cls.sessions[session_id]
        queued_task = cls.guidance_tasks.pop(session_id, None)
        if queued_task and not queued_task.done():
            queued_task.cancel()
        cls.turn_tasks.pop(session_id, None)
        cls.active_chat_request_ids.pop(session_id, None)
        cls.session_locks.pop(session_id, None)

    @classmethod
    def create_permission_future(
        cls, session_id: str, request_id: str
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if session_id not in cls.permission_futures:
            cls.permission_futures[session_id] = {}
        cls.permission_futures[session_id][request_id] = future
        return future

    @classmethod
    def resolve_permission_future(
        cls, session_id: str, request_id: str, response: str
    ) -> bool:
        session_map = cls.permission_futures.get(session_id, {})
        future = session_map.get(request_id)
        if not future:
            return False
        if not future.done():
            future.set_result(response)
        del session_map[request_id]
        if not session_map and session_id in cls.permission_futures:
            del cls.permission_futures[session_id]
        return True

    @classmethod
    def create_question_future(
        cls, session_id: str, request_id: str, payload: Optional[dict[str, Any]] = None,
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if session_id not in cls.question_futures:
            cls.question_futures[session_id] = {}
        cls.question_futures[session_id][request_id] = future
        if payload is not None:
            cls.question_requests.setdefault(session_id, {})[request_id] = {
                "type": "question", "session_id": session_id,
                "request_id": request_id, "question": deepcopy(payload),
            }
        return future

    @classmethod
    def pending_question_request(cls, session_id: str) -> Optional[dict[str, Any]]:
        return next(iter(cls.pending_question_requests(session_id)), None)

    @classmethod
    def pending_question_requests(cls, session_id: str) -> list[dict[str, Any]]:
        # Only live waits are recoverable; never revive a cancelled/restarted turn.
        futures = cls.question_futures.get(session_id, {})
        requests = []
        for request_id, request in cls.question_requests.get(session_id, {}).items():
            future = futures.get(request_id)
            if future is not None and not future.done():
                requests.append(deepcopy(request))
        return requests

    @classmethod
    def resolve_question_group(cls, session_id: str, answers: dict[str, str]) -> list[str]:
        """Validate the whole live group before waking any waiting coroutine."""
        from .goal_questions import parse_answer
        requests = cls.pending_question_requests(session_id)
        ids = {item['request_id'] for item in requests}
        if not requests or not set(answers).issubset(ids):
            raise LookupError('问题组已变化或已过期，请刷新后重试')
        required = {item['request_id'] for item in requests if item['question'].get('required', True)}
        if not required.issubset(answers):
            raise ValueError('请完成当前问题组的全部必填题，再统一提交')
        for item in requests:
            parse_answer({'payload': item['question']}, answers.get(item['request_id'], '').strip())
        # No awaits between validation and resolution: callbacks only run after
        # every Future has its answer, including omitted optional questions.
        for item in requests:
            cls.resolve_question_future(session_id, item['request_id'], answers.get(item['request_id'], '').strip())
        return [item['request_id'] for item in requests]

    @classmethod
    def resolve_question_future(
        cls, session_id: str, request_id: str, answer: str
    ) -> bool:
        requests = cls.question_requests.get(session_id, {})
        requests.pop(request_id, None)
        if not requests:
            cls.question_requests.pop(session_id, None)
        session_map = cls.question_futures.get(session_id, {})
        future = session_map.get(request_id)
        if not future:
            return False
        if not future.done():
            future.set_result(answer)
        del session_map[request_id]
        if not session_map and session_id in cls.question_futures:
            del cls.question_futures[session_id]
        return True

    @classmethod
    def set_session_event_sender(
        cls,
        session_id: str,
        sender: Optional[Callable[[Any], Awaitable[bool] | bool]],
    ) -> None:
        session = cls.sessions.get(session_id)
        if not isinstance(session, dict):
            return
        if sender is None:
            session.pop("event_sender", None)
            return
        session["event_sender"] = sender

    @classmethod
    def subscribe_session_events(cls, session_id: str, sender: Callable) -> Callable[[], None]:
        """Attach a run-scoped transport without replacing the Web connection."""
        token = uuid.uuid4().hex
        cls.session_event_subscribers.setdefault(session_id, {})[token] = sender

        def unsubscribe() -> None:
            subscribers = cls.session_event_subscribers.get(session_id, {})
            subscribers.pop(token, None)
            if not subscribers:
                cls.session_event_subscribers.pop(session_id, None)

        return unsubscribe

    @classmethod
    async def send_session_event(cls, session_id: str, payload: dict[str, Any]) -> bool:
        session = cls.sessions.get(session_id)
        sender = session.get("event_sender") if isinstance(session, dict) else None
        senders = ([sender] if callable(sender) else []) + list(
            cls.session_event_subscribers.get(session_id, {}).values()
        )
        delivered = False
        for callback in senders:
            try:
                result = callback(payload)
                if hasattr(result, "__await__"):
                    result = await result
                delivered = delivered or result is not False
            except Exception:
                continue
        return delivered
