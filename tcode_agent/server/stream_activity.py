"""Helpers for detecting genuinely idle agent streams.

The websocket message iterator is only one of several activity channels.  Tool
progress and subagent lifecycle events are emitted out-of-band, so a watchdog
must observe all of them instead of treating a quiet message iterator as a
stalled turn.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Optional


class StreamActivityTracker:
    """Track the most recent meaningful activity for one agent turn."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.started_at = clock()
        self.last_activity_at = self.started_at
        self.last_activity_kind = "turn_started"

    def touch(self, kind: str) -> None:
        self.last_activity_at = self._clock()
        self.last_activity_kind = str(kind or "activity").strip() or "activity"

    def idle_seconds(self) -> float:
        return max(0.0, self._clock() - self.last_activity_at)

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self.started_at)


async def cancel_pending_stream_item(
    task: Optional[asyncio.Task],
    stream_iter: AsyncIterator[Any],
) -> None:
    """Cancel and reap a pending ``__anext__`` call, then close its iterator."""

    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    close = getattr(stream_iter, "aclose", None)
    if callable(close):
        try:
            await close()
        except (asyncio.CancelledError, RuntimeError):
            # A cancelled generator may already be closed, or still be unwinding.
            pass

