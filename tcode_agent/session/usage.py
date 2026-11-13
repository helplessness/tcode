"""Session token usage aggregation helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        "prompt_tokens": int(left.get("prompt_tokens", 0) or 0) + int(right.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(left.get("completion_tokens", 0) or 0) + int(right.get("completion_tokens", 0) or 0),
        "total_tokens": int(left.get("total_tokens", 0) or 0) + int(right.get("total_tokens", 0) or 0),
    }


def normalize_usage(raw: Any) -> dict[str, int]:
    if raw is None:
        return empty_usage()

    if isinstance(raw, dict):
        prompt_tokens = int(raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0)
        completion_tokens = int(raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0)
        total_tokens = int(raw.get("total_tokens", 0) or 0)
    else:
        prompt_tokens = int(
            getattr(raw, "prompt_tokens", getattr(raw, "input_tokens", 0)) or 0
        )
        completion_tokens = int(
            getattr(raw, "completion_tokens", getattr(raw, "output_tokens", 0)) or 0
        )
        total_tokens = int(getattr(raw, "total_tokens", 0) or 0)

    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": max(0, prompt_tokens),
        "completion_tokens": max(0, completion_tokens),
        "total_tokens": max(0, total_tokens),
    }


def usage_cost(usage: dict[str, int]) -> float:
    return (
        int(usage.get("prompt_tokens", 0) or 0) * 0.0015
        + int(usage.get("completion_tokens", 0) or 0) * 0.015
    ) / 1000


def session_own_usage(session: Any) -> dict[str, int]:
    total = empty_usage()
    for message in getattr(session, "messages", []) or []:
        total = add_usage(total, normalize_usage(getattr(message, "usage", None)))
    return total


def build_session_usage_tree(sessions: list[Any]) -> dict[str, Any]:
    verification_accounting = {"known_tokens": 0, "additional_tokens": 0, "unknown_calls": 0, "unavailable_sessions": 0, "recorded_calls": 0}
    by_id = {str(getattr(session, "id", "") or ""): session for session in sessions}
    by_id.pop("", None)

    children_by_parent: dict[str, list[Any]] = defaultdict(list)
    roots: list[Any] = []
    for session in by_id.values():
        parent_id = str(getattr(session, "parent_id", "") or "").strip()
        if parent_id and parent_id in by_id:
            children_by_parent[parent_id].append(session)
        else:
            roots.append(session)

    def node_for(session: Any) -> dict[str, Any]:
        session_id = str(getattr(session, "id", "") or "")
        own = session_own_usage(session)
        main_usage = own
        additional = empty_usage()
        if getattr(session, "workdir", ""):
            from ..runtime.verification import verification_usage_summary
            try:
                verification = verification_usage_summary(session)
                additional = verification["additional_known_usage"]
                own = add_usage(own, additional)
                verification_accounting["known_tokens"] += verification["known_usage"]["total_tokens"]
                verification_accounting["additional_tokens"] += additional["total_tokens"]
                verification_accounting["unknown_calls"] += verification["unknown_calls"]
                verification_accounting["unavailable_sessions"] += int(bool(verification["recording_errors"]))
                verification_accounting["recorded_calls"] += len(verification["calls"])
            except Exception:
                verification_accounting["unavailable_sessions"] += 1
        children = [node_for(child) for child in children_by_parent.get(session_id, [])]
        total = own
        by_agent: dict[str, dict[str, int]] = {}
        agent_name = str(getattr(session, "agent_name", "") or "unknown") or "unknown"
        by_agent[agent_name] = add_usage(by_agent.get(agent_name, empty_usage()), main_usage)
        if additional["total_tokens"]:
            by_agent["verification_models"] = additional

        for child in children:
            total = add_usage(total, child["total_usage"])
            for name, usage in child.get("by_agent", {}).items():
                by_agent[name] = add_usage(by_agent.get(name, empty_usage()), usage)

        return {
            "session_id": session_id,
            "parent_id": str(getattr(session, "parent_id", "") or ""),
            "title": str(getattr(session, "title", "") or ""),
            "agent_name": agent_name,
            "status": str(getattr(session, "status", "") or ""),
            "created_at": float(getattr(session, "created_at", 0) or 0),
            "updated_at": float(getattr(session, "last_active_at", 0) or 0),
            "message_count": len(getattr(session, "messages", []) or []),
            "own_usage": own,
            "total_usage": total,
            "cost": usage_cost(total),
            "own_cost": usage_cost(own),
            "by_agent": by_agent,
            "children": children,
        }

    tree = [node_for(session) for session in sorted(
        roots,
        key=lambda item: float(getattr(item, "last_active_at", 0) or getattr(item, "created_at", 0) or 0),
        reverse=True,
    )]
    total = empty_usage()
    by_agent: dict[str, dict[str, int]] = {}
    for root in tree:
        total = add_usage(total, root["total_usage"])
        for name, usage in root.get("by_agent", {}).items():
            by_agent[name] = add_usage(by_agent.get(name, empty_usage()), usage)

    return {
        "total_usage": total,
        "cost": usage_cost(total),
        "by_agent": by_agent,
        "sessions": tree,
        "verification_accounting": verification_accounting,
    }
