"""Subagent guidance tool - steer an existing child agent without changing runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import model_validator

from ..base import ParameterSchema, Tool, ToolContext
from ...runtime import RuntimeTaskManager
from ...session.manager import Session, SessionManager
from ...team.registry import WorkerRegistry
from ...types import ToolResult


class SubagentGuideParameters(ParameterSchema):
    """Parameters for subagent_guide tool."""

    message: str
    target: str | None = None
    subagent_session_id: str | None = None
    task_id: str | None = None
    worker_name: str | None = None
    agent_name: str | None = None
    mode: str | None = "queue"
    run_if_idle: bool | None = True
    cancel_descendants: bool | None = True
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def accept_prompt_as_message(cls, value: Any) -> Any:
        """Keep guidance calls compatible with task-style prompt payloads."""

        if not isinstance(value, dict) or str(value.get("message") or "").strip():
            return value
        normalized = dict(value)
        prompt = str(normalized.get("prompt") or "").strip()
        if prompt:
            normalized["message"] = prompt
        return normalized


@dataclass
class _ResolvedTarget:
    session: Session
    label: str
    matched_by: str


class SubagentGuideTool(Tool):
    """Queue or interrupt guidance for an existing subagent session."""

    @property
    def id(self) -> str:
        return "subagent_guide"

    @property
    def description(self) -> str:
        return """Guide an existing subagent or team worker.

Use this when the user wants to steer, correct, or continue a subagent that
already exists under the current session. Do not create a new task just to
change direction of an existing subagent.

Targeting:
- Prefer subagent_session_id or task_id when you have it from a prior task result.
- Use worker_name for team workers.
- Use agent_name only when there is exactly one matching child agent.
- target can be a session id, worker name, agent name, or unambiguous title text.

Modes:
- queue: default. Append guidance and let the subagent continue after its current step.
- interrupt: stop the subagent's current step, clear older queued guidance, and continue with this guidance.

Set run_if_idle=false to only queue guidance without starting an idle subagent."""

    @property
    def parameters(self) -> type:
        return SubagentGuideParameters

    @staticmethod
    def _owner_kwargs(context: ToolContext) -> dict[str, str]:
        extra = context.extra if isinstance(context.extra, dict) else {}
        return {
            key: value
            for key, value in {
                "tenant_id": str(extra.get("tenant_id") or "").strip(),
                "user_id": str(extra.get("user_id") or "").strip(),
                "workspace_id": str(extra.get("workspace_id") or "").strip(),
            }.items()
            if value
        }

    @staticmethod
    def _session_label(session: Session) -> str:
        title = str(getattr(session, "title", "") or "").strip()
        agent = str(getattr(session, "agent_name", "") or "").strip()
        session_id = str(getattr(session, "id", "") or "").strip()
        parts = [part for part in (agent, title, session_id) if part]
        return " | ".join(parts)

    @staticmethod
    def _candidate_summary(children: list[Session]) -> str:
        if not children:
            return "No child subagents are available for this session."
        lines = ["Available child subagents:"]
        for child in children[:12]:
            lines.append(f"- {SubagentGuideTool._session_label(child)}")
        if len(children) > 12:
            lines.append(f"- ... {len(children) - 12} more")
        return "\n".join(lines)

    async def _resolve_by_session_id(
        self,
        session_id: str,
        *,
        parent_session_id: str,
        owner: dict[str, str],
    ) -> Optional[_ResolvedTarget]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        session = await SessionManager.get(normalized, **owner)
        if not session:
            return None
        if str(getattr(session, "parent_id", "") or "") != parent_session_id:
            return None
        return _ResolvedTarget(
            session=session,
            label=self._session_label(session),
            matched_by="session_id",
        )

    async def _resolve_explicit_session_id(
        self,
        field_name: str,
        session_id: str,
        *,
        parent_session_id: str,
        owner: dict[str, str],
    ) -> tuple[Optional[_ResolvedTarget], str]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None, ""
        session = await SessionManager.get(normalized, **owner)
        if not session:
            return None, f"{field_name} not found: {normalized}"
        if str(getattr(session, "parent_id", "") or "") != parent_session_id:
            return (
                None,
                f"{field_name} does not belong to the current parent session: {normalized}",
            )
        return (
            _ResolvedTarget(
                session=session,
                label=self._session_label(session),
                matched_by=field_name,
            ),
            "",
        )

    async def _resolve_runtime_task_id(
        self,
        task_id: str,
        *,
        parent_session_id: str,
        owner: dict[str, str],
    ) -> tuple[Optional[_ResolvedTarget], str]:
        """Resolve a durable runtime task id to its executing child session."""

        normalized = str(task_id or "").strip()
        if not normalized:
            return None, ""
        record = RuntimeTaskManager.get(normalized)
        if not isinstance(record, dict):
            # Backward compatibility: older callers passed a session id in the
            # task_id field before runtime task ids were persisted.
            return await self._resolve_explicit_session_id(
                "task_id",
                normalized,
                parent_session_id=parent_session_id,
                owner=owner,
            )
        if str(record.get("session_id") or "").strip() != parent_session_id:
            return (
                None,
                f"task_id does not belong to the current parent session: {normalized}",
            )

        candidate_session_ids: list[str] = []

        def _append_candidate(raw: Any) -> None:
            value = str(raw or "").strip()
            if value and value not in candidate_session_ids:
                candidate_session_ids.append(value)

        _append_candidate(record.get("worker_session_id"))
        workspace = record.get("agent_workspace")
        if isinstance(workspace, dict):
            _append_candidate(workspace.get("session_id"))
        for event in reversed(list(record.get("events") or [])):
            if not isinstance(event, dict):
                continue
            metadata = event.get("metadata")
            if isinstance(metadata, dict):
                _append_candidate(metadata.get("worker_session_id"))

        worker_name = str(record.get("worker_name") or "").strip().lower()
        if worker_name:
            worker = WorkerRegistry.get_by_name(parent_session_id, worker_name)
            if worker is not None:
                _append_candidate(getattr(worker, "session_id", ""))

        for session_id in candidate_session_ids:
            resolved = await self._resolve_by_session_id(
                session_id,
                parent_session_id=parent_session_id,
                owner=owner,
            )
            if resolved is not None:
                resolved.matched_by = "task_id"
                return resolved, ""
        return None, f"task_id has no available child session: {normalized}"

    async def _resolve_target(
        self,
        parsed: SubagentGuideParameters,
        context: ToolContext,
        owner: dict[str, str],
    ) -> tuple[Optional[_ResolvedTarget], str]:
        parent_session_id = str(context.session_id or "").strip()
        if not parent_session_id:
            return None, "subagent_guide requires a current parent session_id"

        parent = await SessionManager.get(parent_session_id, **owner)
        if not parent:
            return None, "Current parent session was not found"

        if parsed.subagent_session_id:
            resolved, error = await self._resolve_explicit_session_id(
                "subagent_session_id",
                str(parsed.subagent_session_id or "").strip(),
                parent_session_id=parent_session_id,
                owner=owner,
            )
            if error or resolved:
                return resolved, error

        if parsed.task_id:
            resolved, error = await self._resolve_runtime_task_id(
                str(parsed.task_id or "").strip(),
                parent_session_id=parent_session_id,
                owner=owner,
            )
            if error or resolved:
                return resolved, error

        target_text = str(parsed.target or "").strip()
        if target_text:
            target_session = await SessionManager.get(target_text, **owner)
            if target_session and str(getattr(target_session, "parent_id", "") or "") != parent_session_id:
                return None, f"target session does not belong to the current parent session: {target_text}"
            resolved = await self._resolve_by_session_id(
                target_text,
                parent_session_id=parent_session_id,
                owner=owner,
            )
            if resolved is not None:
                return resolved, ""

        worker_lookup = str(parsed.worker_name or parsed.target or "").strip().lower()
        if worker_lookup:
            worker = WorkerRegistry.get_by_name(parent_session_id, worker_lookup)
            if worker:
                resolved = await self._resolve_by_session_id(
                    worker.session_id,
                    parent_session_id=parent_session_id,
                    owner=owner,
                )
                if resolved:
                    resolved.matched_by = "worker_name"
                    return resolved, ""

        children = await SessionManager.list(parent_id=parent_session_id, **owner)
        if not children:
            return None, "No child subagents are available for this session."

        agent_lookup = str(parsed.agent_name or "").strip()
        target_lookup = str(parsed.target or "").strip()

        match_groups: list[tuple[str, list[Session]]] = []
        if agent_lookup:
            match_groups.append(
                (
                    "agent_name",
                    [
                        child
                        for child in children
                        if str(getattr(child, "agent_name", "") or "").strip() == agent_lookup
                    ],
                )
            )

        if target_lookup:
            lowered = target_lookup.lower()
            match_groups.extend(
                [
                    (
                        "agent_name",
                        [
                            child
                            for child in children
                            if str(getattr(child, "agent_name", "") or "").strip().lower()
                            == lowered
                        ],
                    ),
                    (
                        "title",
                        [
                            child
                            for child in children
                            if lowered
                            and lowered
                            in str(getattr(child, "title", "") or "").strip().lower()
                        ],
                    ),
                ]
            )

        for matched_by, matches in match_groups:
            unique = {str(child.id): child for child in matches}
            values = list(unique.values())
            if len(values) == 1:
                child = values[0]
                return (
                    _ResolvedTarget(
                        session=child,
                        label=self._session_label(child),
                        matched_by=matched_by,
                    ),
                    "",
                )
            if len(values) > 1:
                return (
                    None,
                    "Target matched multiple child subagents. "
                    "Use subagent_session_id or task_id.\n"
                    + self._candidate_summary(values),
                )

        return (
            None,
            "Could not resolve the target subagent. "
            "Use subagent_session_id, task_id, worker_name, or an exact agent_name.\n"
            + self._candidate_summary(children),
        )

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = SubagentGuideParameters(**args)
        message = str(parsed.message or "").strip()
        if not message:
            return ToolResult(
                title="Subagent guidance",
                output="",
                error="message is required",
            )

        mode = str(parsed.mode or "queue").strip().lower()
        if mode in {"continue", "append", "enqueue"}:
            mode = "queue"
        if mode not in {"queue", "interrupt"}:
            return ToolResult(
                title="Subagent guidance",
                output="",
                error='mode must be "queue" or "interrupt"',
            )

        owner = self._owner_kwargs(context)
        resolved, error = await self._resolve_target(parsed, context, owner)
        if error or resolved is None:
            return ToolResult(
                title="Subagent guidance",
                output="",
                error=error or "Target subagent not found",
            )

        # Import server runtime helpers lazily to avoid making builtin tool
        # registration depend on the web server module graph.
        from ...server._shared import (
            _cancel_runtime_subtree_for_interrupt_guidance,
            _chat_runtime_status_payload,
            _ensure_session_guidance_drain,
            _hydrate_session_guidance_queue,
            _persist_session_guidance_queue,
        )
        from ...server.state import GuidanceQueueFull, ServerState

        target_session_id = str(resolved.session.id)
        await _hydrate_session_guidance_queue(target_session_id, owner_kwargs=owner)

        cancelled_subtree: dict[str, Any] = {}
        if mode == "interrupt":
            ServerState.clear_guidance_queue(target_session_id)

        try:
            item = ServerState.enqueue_guidance(
                target_session_id,
                message=message,
                output_format=parsed.output_format,
                outcome_contract=parsed.outcome_contract,
                interrupt=mode == "interrupt",
                source=f"agent:{context.session_id}",
                prepend=mode == "interrupt",
            )
        except GuidanceQueueFull as exc:
            return ToolResult(
                title="Subagent guidance",
                output="",
                error=f"[{exc.code}] {exc.detail}",
            )
        await _persist_session_guidance_queue(target_session_id, owner_kwargs=owner)

        drain_after_current_turn = False
        turn_task = ServerState.get_turn_task(target_session_id)
        if bool(parsed.run_if_idle) and turn_task is not None and not turn_task.done():
            loop = asyncio.get_running_loop()

            def _drain_when_turn_finishes(_task: asyncio.Task) -> None:
                loop.call_soon(_ensure_session_guidance_drain, target_session_id)

            turn_task.add_done_callback(_drain_when_turn_finishes)
            drain_after_current_turn = True

        if mode == "interrupt":
            if bool(parsed.cancel_descendants):
                cancelled_subtree = await _cancel_runtime_subtree_for_interrupt_guidance(
                    target_session_id,
                    clear_guidance=True,
                    owner_kwargs=owner,
                )
            await ServerState.cancel_session(target_session_id)

        if (
            bool(parsed.run_if_idle)
            and not drain_after_current_turn
            and not ServerState.is_running(target_session_id)
        ):
            _ensure_session_guidance_drain(target_session_id)

        runtime = _chat_runtime_status_payload(target_session_id)
        pending_count = int(runtime.get("pending_guidance_count", 0) or 0)
        status_text = (
            "interrupt guidance queued"
            if mode == "interrupt"
            else "guidance queued"
        )
        if bool(parsed.run_if_idle) and runtime.get("guidance_worker_active"):
            status_text += " and drain started"

        metadata = {
            "target_session_id": target_session_id,
            "target_agent_name": str(getattr(resolved.session, "agent_name", "") or ""),
            "matched_by": resolved.matched_by,
            "mode": mode,
            "guidance_id": item["id"],
            "pending_guidance_count": pending_count,
            "guidance_worker_active": bool(runtime.get("guidance_worker_active")),
            "drain_after_current_turn": drain_after_current_turn,
            **cancelled_subtree,
        }
        return ToolResult(
            title=f"Subagent guidance: {resolved.label}",
            output=(
                f"{status_text}\n"
                f"target_session_id: {target_session_id}\n"
                f"guidance_id: {item['id']}\n"
                f"pending_guidance_count: {pending_count}"
            ),
            metadata=metadata,
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
