"""主 Agent、Worker 共用的有界并发预算与等待队列。"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional

from .telemetry import TelemetryGateway


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or str(default)))
    except Exception:
        return max(minimum, default)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or str(default)))
    except Exception:
        return max(minimum, default)


class CapacityRejected(RuntimeError):
    """容量不足或等待超时时返回稳定、可展示的拒绝。"""

    def __init__(self, code: str, detail: str, *, retry_after_seconds: float = 1.0):
        self.code = str(code)
        self.detail = str(detail)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(f"[{self.code}] {self.detail}")


@dataclass(slots=True)
class _Waiter:
    ticket_id: str
    kind: str
    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    created_at: float
    event: asyncio.Event
    loop: asyncio.AbstractEventLoop
    granted: bool = False


class ExecutionLease:
    """一次并发预算占用；释放操作幂等。"""

    def __init__(self, controller: "ExecutionCapacityController", waiter: _Waiter):
        self._controller = controller
        self._waiter = waiter
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._controller._release(self._waiter)

    async def __aenter__(self) -> "ExecutionLease":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class ExecutionCapacityController:
    """进程级 FIFO 容量控制器，统一核算主会话和 Worker。"""

    _instance: Optional["ExecutionCapacityController"] = None
    _instance_lock = threading.RLock()

    def __init__(self) -> None:
        self.max_global = _env_int("TCODE_EXECUTION_MAX_CONCURRENT_GLOBAL", 16, minimum=1)
        self.max_per_user = _env_int("TCODE_EXECUTION_MAX_CONCURRENT_PER_USER", 4, minimum=1)
        self.max_per_session = _env_int("TCODE_EXECUTION_MAX_CONCURRENT_PER_SESSION", 1, minimum=1)
        self.queue_max_global = _env_int("TCODE_EXECUTION_QUEUE_MAX_GLOBAL", 64)
        self.queue_max_per_user = _env_int("TCODE_EXECUTION_QUEUE_MAX_PER_USER", 12)
        self.queue_max_per_session = _env_int("TCODE_EXECUTION_QUEUE_MAX_PER_SESSION", 1)
        self.default_wait_seconds = _env_float(
            "TCODE_EXECUTION_QUEUE_WAIT_SECONDS", 30.0
        )
        self._lock = threading.RLock()
        self._waiters: deque[_Waiter] = deque()
        self._active_global = 0
        self._active_by_user: Counter[tuple[str, str]] = Counter()
        self._active_by_session: Counter[str] = Counter()
        self._active_by_kind: Counter[str] = Counter()
        self._queued_by_user: Counter[tuple[str, str]] = Counter()
        self._queued_by_session: Counter[str] = Counter()
        self._queued_by_kind: Counter[str] = Counter()
        self._accepted = 0
        self._rejected = 0
        self._rejected_by_code: Counter[str] = Counter()
        self._timed_out = 0
        self._wait_seconds_total = 0.0
        self._wait_seconds_max = 0.0

    @classmethod
    def current(cls) -> "ExecutionCapacityController":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> "ExecutionCapacityController":
        with cls._instance_lock:
            cls._instance = cls()
            return cls._instance

    @staticmethod
    def _user_key(waiter: _Waiter) -> tuple[str, str]:
        return waiter.tenant_id, waiter.user_id

    def _fits_locked(self, waiter: _Waiter) -> bool:
        return bool(
            self._active_global < self.max_global
            and self._active_by_user[self._user_key(waiter)] < self.max_per_user
            and self._active_by_session[waiter.session_id] < self.max_per_session
        )

    def _grant_locked(self, waiter: _Waiter) -> None:
        waiter.granted = True
        self._active_global += 1
        self._active_by_user[self._user_key(waiter)] += 1
        self._active_by_session[waiter.session_id] += 1
        self._active_by_kind[waiter.kind] += 1
        self._accepted += 1
        waited = max(0.0, time.monotonic() - waiter.created_at)
        self._wait_seconds_total += waited
        self._wait_seconds_max = max(self._wait_seconds_max, waited)

    def _remove_queued_locked(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        self._queued_by_user[self._user_key(waiter)] -= 1
        self._queued_by_session[waiter.session_id] -= 1
        self._queued_by_kind[waiter.kind] -= 1

    def _promote_waiters_locked(self) -> None:
        while self._waiters:
            waiter = self._waiters[0]
            if not self._fits_locked(waiter):
                break
            self._waiters.popleft()
            self._queued_by_user[self._user_key(waiter)] -= 1
            self._queued_by_session[waiter.session_id] -= 1
            self._queued_by_kind[waiter.kind] -= 1
            self._grant_locked(waiter)
            try:
                waiter.loop.call_soon_threadsafe(waiter.event.set)
            except RuntimeError:
                self._release_locked(waiter)

    def _queue_has_room_locked(self, waiter: _Waiter) -> bool:
        return bool(
            len(self._waiters) < self.queue_max_global
            and self._queued_by_user[self._user_key(waiter)] < self.queue_max_per_user
            and self._queued_by_session[waiter.session_id] < self.queue_max_per_session
        )

    async def acquire(
        self,
        *,
        kind: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        request_id: str = "",
        wait_timeout_seconds: Optional[float] = None,
    ) -> ExecutionLease:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            ticket_id=f"cap-{uuid.uuid4()}",
            kind=str(kind or "unknown")[:64],
            tenant_id=str(tenant_id or "anonymous")[:128],
            user_id=str(user_id or "anonymous")[:128],
            session_id=str(session_id or request_id or uuid.uuid4())[:160],
            request_id=str(request_id or "")[:160],
            created_at=time.monotonic(),
            event=asyncio.Event(),
            loop=loop,
        )
        timeout = (
            self.default_wait_seconds
            if wait_timeout_seconds is None
            else max(0.0, float(wait_timeout_seconds))
        )
        rejection: Optional[CapacityRejected] = None
        with self._lock:
            if not self._waiters and self._fits_locked(waiter):
                self._grant_locked(waiter)
            elif timeout <= 0:
                self._rejected += 1
                self._rejected_by_code["capacity.busy"] += 1
                rejection = CapacityRejected(
                    "capacity.busy",
                    "当前任务并发已满，请稍后重试。",
                    retry_after_seconds=1.0,
                )
            elif not self._queue_has_room_locked(waiter):
                self._rejected += 1
                self._rejected_by_code["capacity.queue_full"] += 1
                rejection = CapacityRejected(
                    "capacity.queue_full",
                    "当前等待队列已满，请稍后重试。",
                    retry_after_seconds=min(max(timeout, 1.0), 30.0),
                )
            else:
                self._waiters.append(waiter)
                self._queued_by_user[self._user_key(waiter)] += 1
                self._queued_by_session[waiter.session_id] += 1
                self._queued_by_kind[waiter.kind] += 1
        if rejection is not None:
            self._emit("capacity.rejected", waiter, {"code": rejection.code})
            raise rejection
        if waiter.granted:
            self._emit("capacity.acquired", waiter, {"wait_seconds": 0.0})
            return ExecutionLease(self, waiter)

        self._emit("capacity.queued", waiter, {"timeout_seconds": timeout})
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            with self._lock:
                if waiter.granted:
                    return ExecutionLease(self, waiter)
                self._remove_queued_locked(waiter)
                self._timed_out += 1
                self._rejected += 1
                self._rejected_by_code["capacity.wait_timeout"] += 1
            self._emit("capacity.rejected", waiter, {"code": "capacity.wait_timeout"})
            raise CapacityRejected(
                "capacity.wait_timeout",
                "等待可用执行名额超时，请稍后重试。",
                retry_after_seconds=1.0,
            ) from exc
        except asyncio.CancelledError:
            with self._lock:
                if waiter.granted:
                    self._release_locked(waiter)
                    self._promote_waiters_locked()
                else:
                    self._remove_queued_locked(waiter)
            raise
        self._emit(
            "capacity.acquired",
            waiter,
            {"wait_seconds": max(0.0, time.monotonic() - waiter.created_at)},
        )
        return ExecutionLease(self, waiter)

    def _release_locked(self, waiter: _Waiter) -> None:
        if not waiter.granted:
            return
        waiter.granted = False
        self._active_global = max(0, self._active_global - 1)
        self._active_by_user[self._user_key(waiter)] -= 1
        self._active_by_session[waiter.session_id] -= 1
        self._active_by_kind[waiter.kind] -= 1

    def _release(self, waiter: _Waiter) -> None:
        with self._lock:
            was_granted = waiter.granted
            self._release_locked(waiter)
            self._promote_waiters_locked()
        if was_granted:
            self._emit("capacity.released", waiter, {})

    @staticmethod
    def _emit(event_name: str, waiter: _Waiter, payload: dict[str, object]) -> None:
        TelemetryGateway.emit(
            event_name,
            {"kind": waiter.kind, **payload},
            tenant_id=waiter.tenant_id,
            user_id=waiter.user_id,
            session_id=waiter.session_id,
            request_id=waiter.request_id,
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "active": self._active_global,
                "queued": len(self._waiters),
                "accepted": self._accepted,
                "rejected": self._rejected,
                "rejected_by_code": dict(sorted(self._rejected_by_code.items())),
                "timed_out": self._timed_out,
                "wait_seconds_total": round(self._wait_seconds_total, 6),
                "wait_seconds_max": round(self._wait_seconds_max, 6),
                "active_by_kind": {
                    key: value for key, value in self._active_by_kind.items() if value > 0
                },
                "queued_by_kind": {
                    key: value for key, value in self._queued_by_kind.items() if value > 0
                },
                "limits": {
                    "concurrent_global": self.max_global,
                    "concurrent_per_user": self.max_per_user,
                    "concurrent_per_session": self.max_per_session,
                    "queue_global": self.queue_max_global,
                    "queue_per_user": self.queue_max_per_user,
                    "queue_per_session": self.queue_max_per_session,
                    "wait_seconds": self.default_wait_seconds,
                },
            }


__all__ = [
    "CapacityRejected",
    "ExecutionCapacityController",
    "ExecutionLease",
]
