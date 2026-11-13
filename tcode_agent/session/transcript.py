from __future__ import annotations

from typing import Any, Iterable


def _message_id(message: Any) -> str:
    return str(getattr(message, "id", "") or "").strip()


def _message_role(message: Any) -> str:
    return str(getattr(message, "role", "") or "").strip().lower()


def _message_content(message: Any) -> str:
    return str(getattr(message, "content", "") or "")


def _message_metadata(message: Any) -> dict[str, Any]:
    raw = getattr(message, "metadata", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def without_legacy_native_agui_user_echoes(
    messages: Iterable[Any],
    *,
    session_metadata: Any = None,
) -> list[Any]:
    """Remove user rows duplicated by the former AG-UI native persistence path.

    The old path let TCode persist the canonical user message, then appended
    the AG-UI source message after the assistant answer. Canonical completed
    turns are identified by assistant ``parent_id`` links, so two legitimate
    turns with identical text remain distinct.
    """

    items = list(messages or [])
    if len(items) < 3:
        return items

    answered_user_ids = {
        str(getattr(message, "parent_id", "") or "").strip()
        for message in items
        if _message_role(message) == "assistant"
        and str(getattr(message, "parent_id", "") or "").strip()
    }
    if not answered_user_ids:
        return items

    canonical_by_content: dict[str, list[str]] = {}
    positions = {_message_id(message): index for index, message in enumerate(items)}
    for message in items:
        message_id = _message_id(message)
        if _message_role(message) != "user" or message_id not in answered_user_ids:
            continue
        canonical_by_content.setdefault(_message_content(message), []).append(message_id)

    metadata = dict(session_metadata) if isinstance(session_metadata, dict) else {}
    last_turn = metadata.get("last_turn") or {}
    if not isinstance(last_turn, dict):
        last_turn = {}
    last_user_id = str(last_turn.get("user_message_id") or "").strip()
    last_assistant_id = str(
        last_turn.get("final_message_id")
        or last_turn.get("assistant_message_id")
        or ""
    ).strip()
    completed_agui_turn = (
        str(metadata.get("transport") or "").strip().lower() == "ag-ui"
        and str(last_turn.get("state") or "").strip().lower() == "completed"
        and last_user_id in answered_user_ids
        and last_user_id in positions
        and last_assistant_id in positions
    )

    cleaned: list[Any] = []
    for index, message in enumerate(items):
        if _message_role(message) != "user":
            cleaned.append(message)
            continue
        message_id = _message_id(message)
        if message_id in answered_user_ids:
            cleaned.append(message)
            continue
        # A failed or interrupted current turn can legitimately have no
        # assistant child yet. Its last_turn identity is still authoritative.
        if message_id and message_id == last_user_id:
            cleaned.append(message)
            continue
        canonical_ids = canonical_by_content.get(_message_content(message), [])
        preceding_canonical = any(positions.get(item_id, index) < index for item_id in canonical_ids)
        source_runtime_id = str(
            _message_metadata(message).get("source_runtime_id") or ""
        ).strip()
        marked_legacy_echo = source_runtime_id == "tcode-native" and preceding_canonical
        unmarked_trailing_echo = (
            completed_agui_turn
            and index == len(items) - 1
            and message_id != last_user_id
            and _message_content(message)
            == _message_content(next(
                item for item in items if _message_id(item) == last_user_id
            ))
            and index > positions[last_assistant_id]
        )
        if marked_legacy_echo or unmarked_trailing_echo:
            continue
        cleaned.append(message)
    return cleaned


__all__ = ["without_legacy_native_agui_user_echoes"]
