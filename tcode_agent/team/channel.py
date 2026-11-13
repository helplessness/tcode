"""Team channel - persistent JSONL event log with asyncio subscribers"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class TeamChannelEvent:
    """A single observable event in the team channel"""

    event_id: str
    team_id: str
    source: str        # "manager" | "worker:alice" | "human"
    target: str        # "all" | "worker:bob" | "manager"
    event_type: str    # worker_created | task_assigned | task_done | task_failed | worker_terminated | message
    content: str
    metadata: dict
    timestamp: float


class TeamChannel:
    """
    Per-team event log.  
    Persists to a JSONL file and pushes live events to asyncio.Queue subscribers.
    Equivalent to a Matrix Room in HiClaw, but implemented locally.
    """

    _channels: dict[str, "TeamChannel"] = {}

    def __init__(self, team_id: str, workdir: str) -> None:
        self.team_id = team_id
        self._path = Path(workdir) / ".tcode" / "teams" / f"{team_id}_events.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._subscribers: list[asyncio.Queue] = []

    @classmethod
    def get_or_create(cls, team_id: str, workdir: str) -> "TeamChannel":
        if team_id not in cls._channels:
            cls._channels[team_id] = cls(team_id, workdir)
        return cls._channels[team_id]

    async def publish(
        self,
        source: str,
        target: str,
        event_type: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> TeamChannelEvent:
        from ..runtime.telemetry import redact_telemetry_text, redact_telemetry_value

        evt = TeamChannelEvent(
            event_id=uuid.uuid4().hex,
            team_id=self.team_id,
            source=source,
            target=target,
            event_type=event_type,
            content=str(content or ""),
            metadata=dict(metadata or {}),
            timestamp=time.time(),
        )
        # Persist to JSONL
        try:
            persisted = asdict(evt)
            persisted["content"] = redact_telemetry_text(persisted["content"])
            persisted["metadata"] = redact_telemetry_value(persisted["metadata"])
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(persisted, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # Push to live subscribers
        dead: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                dead.append(q)
        for d in dead:
            try:
                self._subscribers.remove(d)
            except ValueError:
                pass
        return evt

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def history(self, limit: int = 200) -> list[TeamChannelEvent]:
        """Read persisted events (newest last)."""
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").strip().splitlines()
            events: list[TeamChannelEvent] = []
            for line in lines[-limit:]:
                try:
                    d = json.loads(line)
                    events.append(TeamChannelEvent(**d))
                except Exception:
                    pass
            return events
        except Exception:
            return []

    def clear(self) -> None:
        try:
            if self._path.exists():
                self._path.unlink()
        except Exception:
            pass
