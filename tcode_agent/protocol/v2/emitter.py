"""运行时唯一 v2 事件出口及有界重放缓冲。"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .envelope import PROTOCOL_ID, PROTOCOL_VERSION, utc_now
from .events import RuntimeEventValue, parse_runtime_event
from .persistence import runtime_event_session_spec, should_persist_runtime_event
from .state_machine import RuntimeEventStateMachine


EventSink = Callable[[dict[str, Any]], Awaitable[Any]]
PROCESS_STREAM_ID = f"stream_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ReplayWindow:
    events: tuple[dict[str, Any], ...]
    oldest_sequence: int
    latest_sequence: int
    gap: bool


class RuntimeEventReplayBuffer:
    """进程内有界缓冲；服务重启后的恢复由持久 snapshot 负责。"""

    def __init__(self, max_events_per_session: int = 2_048) -> None:
        self.max_events_per_session = max(1, int(max_events_per_session))
        self._lock = threading.RLock()
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.max_events_per_session)
        )
        self._sequences: dict[str, int] = {}

    def next_sequence(self, session_id: str) -> int:
        with self._lock:
            value = self._sequences.get(session_id, 0) + 1
            self._sequences[session_id] = value
            return value

    def append(self, event: RuntimeEventValue) -> None:
        payload = event.model_dump(mode="json", by_alias=True)
        with self._lock:
            self._events[event.session_id].append(payload)
            self._sequences[event.session_id] = max(
                self._sequences.get(event.session_id, 0), event.sequence
            )

    def release_sequence(self, session_id: str, sequence: int) -> None:
        """仅回收尚未追加的最新序号，避免校验失败制造永久缺口。"""

        with self._lock:
            if self._sequences.get(session_id) != sequence:
                return
            buffered = self._events.get(session_id)
            buffered_latest = (
                int(buffered[-1].get("sequence", 0) or 0) if buffered else 0
            )
            if buffered_latest < sequence:
                self._sequences[session_id] = max(buffered_latest, sequence - 1)

    def after(
        self,
        session_id: str,
        after_sequence: int,
        *,
        limit: int = 2_048,
    ) -> ReplayWindow:
        normalized_after = max(0, int(after_sequence))
        normalized_limit = max(1, min(int(limit), self.max_events_per_session))
        with self._lock:
            buffered = list(self._events.get(session_id) or ())
            latest = self._sequences.get(session_id, 0)
        oldest = int(buffered[0].get("sequence", 0) or 0) if buffered else latest
        gap = bool(buffered and normalized_after < oldest - 1)
        selected = tuple(
            dict(item)
            for item in buffered
            if int(item.get("sequence", 0) or 0) > normalized_after
        )[:normalized_limit]
        return ReplayWindow(
            events=selected,
            oldest_sequence=oldest,
            latest_sequence=latest,
            gap=gap,
        )

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._sequences.clear()


def _default_buffer_size() -> int:
    try:
        return max(128, min(100_000, int(os.environ.get("TCODE_V2_REPLAY_EVENTS", "2048"))))
    except Exception:
        return 2_048


runtime_event_replay = RuntimeEventReplayBuffer(_default_buffer_size())


class RuntimeEventEmitter:
    """分配标识和序列、校验状态机、选择性持久化并发送事件。"""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        sink: EventSink,
        replay: RuntimeEventReplayBuffer = runtime_event_replay,
        owner: dict[str, Any] | None = None,
        workdir: str | None = None,
        persist: bool = True,
    ) -> None:
        self.session_id = str(session_id or "").strip()
        self.run_id = str(run_id or "").strip()
        if not self.session_id or not self.run_id:
            raise ValueError("session_id and run_id are required")
        self.sink = sink
        self.replay = replay
        self.owner = dict(owner or {})
        self.workdir = str(workdir or "").strip() or None
        self.persist = bool(persist)
        # sequence 由共享 replay registry 全局分配；另一 socket（例如只读回放）
        # 可能在本 emitter 两次发送之间占用序号，因此本地只检查单调与生命周期。
        self.state_machine = RuntimeEventStateMachine(
            session_id=self.session_id,
            require_contiguous=False,
        )
        self._emit_lock = asyncio.Lock()

    async def emit(
        self,
        event_type: str,
        *,
        causation_id: str = "",
        metadata: dict[str, Any] | None = None,
        **payload: Any,
    ) -> RuntimeEventValue | None:
        async with self._emit_lock:
            sequence = self.replay.next_sequence(self.session_id)
            normalized_metadata = dict(metadata or {})
            tcode_metadata = normalized_metadata.get("tcode")
            normalized_tcode_metadata = (
                dict(tcode_metadata) if isinstance(tcode_metadata, dict) else {}
            )
            normalized_tcode_metadata.setdefault("stream_id", PROCESS_STREAM_ID)
            normalized_metadata["tcode"] = normalized_tcode_metadata
            raw = {
                "protocol": PROTOCOL_ID,
                "protocol_version": PROTOCOL_VERSION,
                "type": event_type,
                "event_id": f"evt_{uuid.uuid4().hex}",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "sequence": sequence,
                "timestamp": utc_now(),
                "causation_id": str(causation_id or ""),
                "metadata": normalized_metadata,
                **payload,
            }
            try:
                event = parse_runtime_event(raw)
                if not self.state_machine.accept(event):
                    self.replay.release_sequence(self.session_id, sequence)
                    return None
            except Exception:
                self.replay.release_sequence(self.session_id, sequence)
                raise
            self.replay.append(event)
            if self.persist and self.workdir and should_persist_runtime_event(event):
                await asyncio.to_thread(self._persist, event)
            await self.sink(event.model_dump(mode="json", by_alias=True))
            return event

    def _persist(self, event: RuntimeEventValue) -> None:
        from ...session.events import SessionEventStore

        specification = runtime_event_session_spec(event)
        SessionEventStore.append(
            session_id=self.session_id,
            event_type=specification["event_type"],
            payload=specification["payload"],
            metadata=specification["metadata"],
            owner=self.owner,
            causation_id=specification["causation_id"],
            idempotency_key=specification["idempotency_key"],
            workdir=self.workdir,
        )

    async def replay_after(self, after_sequence: int, *, limit: int = 2_048) -> ReplayWindow:
        window = self.replay.after(
            self.session_id,
            after_sequence,
            limit=limit,
        )
        # 缓冲头之前已经丢失时，发送残缺尾部会制造看似连续但不完整的状态。
        # 调用方必须改发 messages/state snapshot。
        if not window.gap:
            for event in window.events:
                await self.sink(dict(event))
        return window


__all__ = [
    "ReplayWindow",
    "RuntimeEventEmitter",
    "RuntimeEventReplayBuffer",
    "runtime_event_replay",
]
