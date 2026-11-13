"""Stable identifiers used to connect one turn with its tools and artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def trace_id_for_turn(session_id: str, turn_id: str) -> str:
    source = f"{str(session_id or '').strip()}\x00{str(turn_id or '').strip()}"
    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"trace-{digest}"


@dataclass(frozen=True)
class ResearchTraceContext:
    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    trace_id: str
    parent_event_id: str = ""
    research_id: str = ""
    candidate_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "parent_event_id": self.parent_event_id,
            "research_id": self.research_id,
            "candidate_id": self.candidate_id,
        }

    @classmethod
    def from_runtime(
        cls,
        *,
        session: Any,
        turn_id: str,
        trace_id: str = "",
    ) -> "ResearchTraceContext":
        session_id = str(getattr(session, "id", "") or "")
        metadata = dict(getattr(session, "metadata", {}) or {})
        return cls(
            tenant_id=str(
                getattr(session, "tenant_id", "")
                or metadata.get("tenant_id")
                or "admin_tenant"
            ),
            user_id=str(
                getattr(session, "user_id", "")
                or metadata.get("user_id")
                or "local-user"
            ),
            workspace_id=str(
                getattr(session, "workspace_id", "")
                or metadata.get("workspace_id")
                or "workspace-1"
            ),
            session_id=session_id,
            turn_id=str(turn_id or ""),
            trace_id=str(trace_id or trace_id_for_turn(session_id, turn_id)),
            parent_event_id="",
            research_id=str(metadata.get("active_research_id") or ""),
            candidate_id=str(metadata.get("active_candidate_id") or ""),
        )
