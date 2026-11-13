"""Helpers for resolving scoped memory namespaces for agents and tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from ..agent.agents import BuiltinAgents
from ..session.manager import SessionManager
from ..team.memory import TeamMemoryManager
from ..team.registry import WorkerRegistry
from ..team.worker_memory import WorkerMemoryManager
from ..types import AgentMode
from .manager import MemoryManager
from .session_memory import SessionMemoryManager

VALID_MEMORY_SCOPES = frozenset({"auto", "global", "team", "worker", "session"})

# ── Global memory write policy ──────────────────────────────────

def _global_write_mode() -> str:
    raw = str(os.environ.get("TCODE_GLOBAL_MEMORY_WRITE_MODE", "primary-only") or "primary-only").strip().lower()
    if raw not in {"all", "primary-only", "none"}:
        return "primary-only"
    return raw


def _global_write_allowed_agents() -> set[str]:
    raw = str(os.environ.get("TCODE_GLOBAL_MEMORY_WRITE_ALLOW_AGENTS", "") or "").strip()
    if not raw:
        return set()
    return {name.strip().lower() for name in raw.replace("，", ",").split(",") if name.strip()}


def _agent_may_write_global(agent_name: str) -> tuple[bool, str]:
    """Check if an agent is allowed to write to global memory scope.

    Returns (allowed, reason).
    """
    mode = _global_write_mode()
    agent_name_lower = str(agent_name or "").strip().lower()

    # Mode: all — everyone can write
    if mode == "all":
        return True, ""

    # Mode: none — nobody can write
    if mode == "none":
        return False, "global scope writes are disabled (TCODE_GLOBAL_MEMORY_WRITE_MODE=none)"

    # Mode: primary-only — only primary agents, plus explicit allowlist
    info = BuiltinAgents.get(agent_name_lower)
    agent_mode = getattr(info, "mode", AgentMode.PRIMARY) if info else AgentMode.PRIMARY

    # Explicit allowlist wins (case-insensitive)
    allowed_set = _global_write_allowed_agents()
    if agent_name_lower in allowed_set:
        return True, ""

    if agent_mode == AgentMode.PRIMARY:
        return True, ""

    return False, (
        "global scope write is restricted for this agent; "
        "use scope=\"session\", \"worker\", or \"team\" instead"
    )


@dataclass(frozen=True)
class ResolvedMemoryScope:
    requested_scope: str
    resolved_scope: str
    scope_key: str = ""
    source: str = ""


def normalize_memory_scope(
    scope: Optional[str],
    *,
    allow_auto: bool = True,
) -> str:
    normalized = str(scope or "auto").strip().lower() or "auto"
    allowed = set(VALID_MEMORY_SCOPES)
    if not allow_auto:
        allowed.discard("auto")
    if normalized not in allowed:
        raise ValueError(
            "scope must be one of: "
            + ", ".join(sorted(allowed))
        )
    return normalized


def _agent_mode(agent_name: str) -> AgentMode:
    info = BuiltinAgents.get(str(agent_name or "").strip())
    if info and getattr(info, "mode", None):
        return info.mode
    return AgentMode.PRIMARY


def _context_workdir(context: Any) -> Optional[str]:
    extra = getattr(context, "extra", None) or {}
    return extra.get("workdir")


def _shared_memory_binding(context: Any) -> tuple[str, str]:
    extra = getattr(context, "extra", None) or {}
    shared_memory_key = str(
        extra.get("shared_memory_key") or extra.get("worker_memory_key") or ""
    ).strip()
    if not shared_memory_key:
        return "", ""
    source = str(extra.get("shared_memory_source") or "").strip()
    if not source:
        source = f"worker:{getattr(context, 'agent', 'agent')}"
    return shared_memory_key, source


def _derive_worker_scope_key(worker: Any) -> str:
    explicit = str(getattr(worker, "memory_key", "") or "").strip()
    if explicit:
        return explicit
    team_id = str(getattr(worker, "team_id", "") or "").strip()
    name = str(getattr(worker, "name", "") or "").strip().lower()
    if team_id and name:
        return f"{team_id}:{name}"
    session_id = str(getattr(worker, "session_id", "") or "").strip()
    if session_id:
        return f"worker:{session_id}"
    return ""


async def _resolve_worker_scope(context: Any) -> tuple[str, str]:
    shared_memory_key, shared_source = _shared_memory_binding(context)
    if shared_memory_key:
        return shared_memory_key, shared_source

    session_id = str(getattr(context, "session_id", "") or "").strip()
    worker = WorkerRegistry.get(session_id) if session_id else None
    if worker:
        source = f"worker:{str(getattr(worker, 'name', '') or '').strip().lower()}"
        return _derive_worker_scope_key(worker), source

    session = await SessionManager.get(session_id) if session_id else None
    parent_id = str(getattr(session, "parent_id", "") or "").strip() if session else ""
    if parent_id:
        parent_worker = WorkerRegistry.get(parent_id)
        if parent_worker:
            source = f"worker:{str(getattr(parent_worker, 'name', '') or '').strip().lower()}"
            return _derive_worker_scope_key(parent_worker), source

    return "", ""


async def _resolve_team_scope_key(context: Any) -> str:
    session_id = str(getattr(context, "session_id", "") or "").strip()
    if not session_id:
        return ""

    worker = WorkerRegistry.get(session_id)
    if worker:
        return str(getattr(worker, "team_id", "") or "").strip()

    session = await SessionManager.get(session_id)
    if session:
        parent_id = str(getattr(session, "parent_id", "") or "").strip()
        if parent_id:
            parent_worker = WorkerRegistry.get(parent_id)
            if parent_worker:
                return str(getattr(parent_worker, "team_id", "") or "").strip()

            parent_session = await SessionManager.get(parent_id)
            if parent_session:
                parent_mode = _agent_mode(str(getattr(parent_session, "agent_name", "") or ""))
                if parent_mode == AgentMode.MANAGER:
                    return str(getattr(parent_session, "id", "") or parent_id).strip()

        current_mode = _agent_mode(str(getattr(session, "agent_name", "") or ""))
        if current_mode == AgentMode.MANAGER:
            return str(getattr(session, "id", "") or session_id).strip()

    if _agent_mode(str(getattr(context, "agent", "") or "")) == AgentMode.MANAGER:
        return session_id
    return ""


def default_memory_scope(context: Any, *, for_write: bool) -> str:
    mode = _agent_mode(str(getattr(context, "agent", "") or ""))
    worker_key, _ = _shared_memory_binding(context)
    if worker_key or mode == AgentMode.WORKER:
        return "worker"
    if mode == AgentMode.MANAGER:
        return "team"
    if mode == AgentMode.PRIMARY:
        return "global"
    return "session"


async def resolve_memory_scope(
    context: Any,
    *,
    scope: Optional[str],
    for_write: bool,
) -> ResolvedMemoryScope:
    requested_scope = normalize_memory_scope(scope)
    target_scope = (
        default_memory_scope(context, for_write=for_write)
        if requested_scope == "auto"
        else requested_scope
    )

    if target_scope == "global":
        if for_write:
            allowed, reason = _agent_may_write_global(
                str(getattr(context, "agent", "") or "").strip()
            )
            if not allowed:
                raise ValueError(reason)
        return ResolvedMemoryScope(
            requested_scope=requested_scope,
            resolved_scope="global",
        )

    if target_scope == "session":
        return ResolvedMemoryScope(
            requested_scope=requested_scope,
            resolved_scope="session",
            scope_key=str(getattr(context, "session_id", "") or "").strip(),
            source=str(getattr(context, "agent", "") or "").strip(),
        )

    if target_scope == "worker":
        scope_key, source = await _resolve_worker_scope(context)
        if scope_key:
            return ResolvedMemoryScope(
                requested_scope=requested_scope,
                resolved_scope="worker",
                scope_key=scope_key,
                source=source or str(getattr(context, "agent", "") or "").strip(),
            )
        if requested_scope != "auto":
            raise ValueError("worker scope is unavailable in this session")
        return await resolve_memory_scope(
            context,
            scope="session",
            for_write=for_write,
        )

    if target_scope == "team":
        scope_key = await _resolve_team_scope_key(context)
        if scope_key:
            return ResolvedMemoryScope(
                requested_scope=requested_scope,
                resolved_scope="team",
                scope_key=scope_key,
                source=str(getattr(context, "agent", "") or "").strip(),
            )
        if requested_scope != "auto":
            raise ValueError("team scope is unavailable in this session")
        fallback_scope = "global" if _agent_mode(str(getattr(context, "agent", "") or "")) == AgentMode.PRIMARY else "session"
        return await resolve_memory_scope(
            context,
            scope=fallback_scope,
            for_write=for_write,
        )

    raise ValueError(f"unsupported scope: {target_scope}")


def query_memory_scope(
    resolved: ResolvedMemoryScope,
    *,
    workdir: Optional[str],
    query: Optional[str],
    limit: int,
    level: str,
    filters: Optional[dict[str, Any]] = None,
) -> list[Any]:
    if resolved.resolved_scope == "global":
        return MemoryManager.query(
            workdir=workdir,
            query=query,
            limit=limit,
            level=level,
            filters=filters,
        )
    manager = {
        "team": TeamMemoryManager,
        "worker": WorkerMemoryManager,
        "session": SessionMemoryManager,
    }[resolved.resolved_scope]
    return manager.query(
        resolved.scope_key,
        workdir or "",
        query=query,
        limit=limit,
        level=level,
        filters=filters,
    )


def write_memory_scope(
    resolved: ResolvedMemoryScope,
    *,
    workdir: Optional[str],
    content: str,
    tags: Optional[list[str]],
    source: str,
) -> Any:
    if resolved.resolved_scope == "global":
        return MemoryManager.add(
            content=content,
            tags=tags,
            workdir=workdir,
            source=source,
        )
    manager = {
        "team": TeamMemoryManager,
        "worker": WorkerMemoryManager,
        "session": SessionMemoryManager,
    }[resolved.resolved_scope]
    return manager.add(
        resolved.scope_key,
        workdir or "",
        content=content,
        tags=tags,
        source=resolved.source or source,
    )
