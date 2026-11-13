"""Shared child-to-parent question routing helpers.

The broker keeps child agents from answering on behalf of the user.  It first
offers the parent context resolver a chance to reuse already-known input, then
forwards unresolved questions through the parent session's interactive
callback.  Both ``task`` subagents and team Workers use this module.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional


QuestionCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]


def decorate_child_question(
    payload: dict[str, Any],
    *,
    child_session_id: str,
    child_agent_name: str,
    child_display_name: str = "",
    parent_session_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Attach stable source identity and a human-readable prefix."""

    forwarded = dict(payload or {})
    display_name = str(child_display_name or child_agent_name or "子代理").strip() or "子代理"
    prefix = f"{display_name} 需要补充信息："
    question_text = str(forwarded.get("question") or "").strip()
    display_text = str(forwarded.get("display") or "").strip()
    forwarded["question"] = (
        question_text
        if question_text.startswith(prefix)
        else f"{prefix}\n{question_text}" if question_text else display_text or prefix
    )
    forwarded["display"] = (
        display_text
        if display_text.startswith(prefix)
        else f"{prefix}\n{display_text}" if display_text else forwarded["question"]
    )
    forwarded["source_session_id"] = str(child_session_id or "").strip()
    forwarded["source_agent_name"] = str(child_agent_name or "").strip()
    forwarded["source_display_name"] = display_name
    forwarded["forwarded_to_parent_session_id"] = str(parent_session_id or "").strip()
    if task_id:
        forwarded["source_task_id"] = str(task_id).strip()
    return forwarded


async def _call_question_callback(
    callback: Optional[QuestionCallback],
    payload: dict[str, Any],
) -> Optional[str]:
    if not callable(callback):
        return None
    result = callback(payload)
    if hasattr(result, "__await__"):
        result = await result
    if result is None:
        return None
    text = str(result).strip()
    return text or None


def build_parent_question_bridge(
    *,
    child_session_id: str,
    child_agent_name: str,
    child_display_name: str = "",
    parent_session_id: str = "",
    task_id: str = "",
    parent_question_ask: Optional[QuestionCallback] = None,
    parent_question_resolve: Optional[QuestionCallback] = None,
    interaction_mode: str = "parent_first",
) -> QuestionCallback:
    """Build a question handler shared by task subagents and team Workers.

    ``parent_first`` never lets a child bypass the parent Agent and directly
    interrupt the human.  If deterministic parent context cannot answer, the
    empty result becomes an ``awaiting_input`` handoff.  ``direct_user`` is
    reserved for explicitly interactive child-chat surfaces.
    """

    normalized_mode = str(interaction_mode or "parent_first").strip().lower()

    async def bridge(payload: dict[str, Any]) -> str:
        forwarded = decorate_child_question(
            payload if isinstance(payload, dict) else {},
            child_session_id=child_session_id,
            child_agent_name=child_agent_name,
            child_display_name=child_display_name,
            parent_session_id=parent_session_id,
            task_id=task_id,
        )
        resolved = await _call_question_callback(parent_question_resolve, forwarded)
        if resolved is not None:
            return resolved
        if normalized_mode == "direct_user":
            answered = await _call_question_callback(parent_question_ask, forwarded)
            if answered is not None:
                return answered
            return str(forwarded.get("default_answer") or "").strip()
        if not bool(forwarded.get("required", True)):
            return str(forwarded.get("default_answer") or "").strip()
        return ""

    return bridge
