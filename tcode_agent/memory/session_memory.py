"""Session-scoped persistent memory stored separately from global/team memory."""

from __future__ import annotations

import os

from ..team.memory import TeamMemoryEntry, TeamMemoryManager


class SessionMemoryManager(TeamMemoryManager):
    """Persistent memory scoped to a single session id.

    Storage location:
      .tcode/sessions/{session_id}_memory.json
    """

    MEMORY_SCOPE_KIND = "session"
    DEFAULT_MAX_ENTRIES = 200

    @classmethod
    def _path(cls, team_id: str, workdir: str) -> str:
        base = workdir or os.getcwd()
        safe_key = str(team_id or "").replace(":", "__")
        return os.path.join(base, ".tcode", "sessions", f"{safe_key}_memory.json")

    @classmethod
    def render_context(cls, session_id: str, workdir: str, limit: int = 20) -> str:
        return super().render_context(session_id, workdir, limit=limit)

    @classmethod
    def load(cls, session_id: str, workdir: str) -> list[TeamMemoryEntry]:
        return super().load(session_id, workdir)
