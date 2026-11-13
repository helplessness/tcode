"""Shared classification helpers for durable conversation messages."""

from __future__ import annotations

from typing import Any
import re


_INTERNAL_USER_PREFIXES = (
    "[SIDE CHAT CONTEXT IMPORT]",
    "[OUTCOME VALIDATION REPAIR]",
    "[OUTCOME REVISION CONFIRMATION]",
    "[INTERNAL TERMINAL SUMMARY REQUEST]",
    "Async Symphony issue finished:",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_real_user_message(message: Any) -> bool:
    """Return whether a user-role message expresses an actual user goal.

    Runtime repair/guidance messages remain part of the durable transcript and
    can still be sent to the model, but they must not replace the goal used for
    checkpointing, memory retrieval, or tool routing.
    """

    if str(getattr(message, "role", "") or "") != "user":
        return False
    content = str(getattr(message, "content", "") or "").strip()
    if not content or content.startswith("[COMPACTION MARKER"):
        return False
    if bool(getattr(message, "_internal", False)) or bool(
        getattr(message, "_hide_from_frontend", False)
    ):
        return False
    metadata = getattr(message, "metadata", {})
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    if _truthy(metadata.get("internal")) or metadata.get("turn_interruption"):
        return False
    guidance = metadata.get("guidance")
    if isinstance(guidance, dict):
        # Queue messages (including Feishu) start real user turns. Only inline
        # runtime steering must be excluded from goal selection.
        if guidance.get("mode") != "queue" and guidance.get("applied_safe_point") != "after_recovery":
            return False
    return not content.startswith(_INTERNAL_USER_PREFIXES)


def latest_user_goal(messages: list[Any]) -> str:
    """Return the latest real user goal, excluding runtime control messages."""

    for message in reversed(messages or []):
        if is_real_user_message(message):
            return str(getattr(message, "content", "") or "").strip()
    return ""


def applied_turn_guidance(messages: list[Any], turn_id: str) -> tuple[str, ...]:
    """Select only runtime-applied user steering for this exact turn."""
    if not turn_id:
        return ()
    directives = []
    for message in messages or []:
        if getattr(message, "role", "") != "user":
            continue
        metadata = getattr(message, "metadata", {}) or {}
        guidance = metadata.get("guidance")
        if (
            not metadata.get("internal")
            and isinstance(guidance, dict)
            and guidance.get("mode") == "steer"
            and guidance.get("target_turn_id") == turn_id
            and guidance.get("applied_safe_point")
        ):
            directives.append(str(getattr(message, "content", "") or ""))
    return tuple(text.strip() for text in directives if text.strip())


def pending_guidance_handoff(messages: list[Any]) -> list[Any]:
    """Applied guidance that has not yet received a fresh model response."""
    result = []
    for message in reversed(messages):
        metadata = getattr(message, "metadata", {}) or {}
        if getattr(message, "role", "") == "assistant" and not metadata.get("superseded_by_guidance"):
            break
        if getattr(message, "role", "") == "user" and isinstance(metadata.get("guidance_handoff"), dict):
            result.append(message)
        elif is_real_user_message(message):
            break
    return list(reversed(result))


def request_denied_tools(messages: list[Any]) -> list[str]:
    """Enforce explicit shell bans across repair turns, not tool/PDF text.

    This narrow guard is not a general natural-language permission compiler.
    Other permissions still go through the existing policy/contract boundary.
    """
    goal = latest_user_goal(messages)
    shell_ban = re.search(
        r"(?:不要|禁止|不得|不允许|不)(?:调用|使用|执行)[^。；;\n]{0,80}\bShell\b"
        r"|\b(?:no shell|do not (?:use|call|execute) (?:the )?shell)\b",
        goal, re.IGNORECASE,
    )
    return ["exec", "bash", "process"] if shell_ban else []


def side_chat_import_message(record):
    from .types import Message
    return Message(id=record["id"], role="user", content=record["content"], metadata={
        "internal": True, "event_type": "side_chat_import", "side_session_id": record["side_session_id"],
        "user_directives": record.get("user_directives", []),
    })


def consume_pending_side_chat_imports(agent):
    """Attach at a model-request safe point, never in the middle of a tool pair."""
    session = getattr(agent, "_existing_session", None)
    imports = (getattr(session, "metadata", None) or {}).get("side_chat_imports") or {}
    known = {message.id for message in agent.messages}
    for record in imports.values():
        if record.get("pending"):
            if record["id"] not in known:
                agent.messages.append(side_chat_import_message(record))
                known.add(record["id"])
            else:
                for message in agent.messages:
                    if message.id == record["id"]:
                        message.metadata.update(side_chat_import_message(record).metadata)
            record["pending"] = False


__all__ = ["is_real_user_message", "latest_user_goal"]
