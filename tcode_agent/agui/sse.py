"""AG-UI SSE 编码和有界生产队列。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable


def encode_sse_data(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n".encode("utf-8")


async def bounded_sse_stream(
    source: AsyncIterator[dict[str, Any]],
    *,
    disconnect_policy: str = "cancel",
    on_disconnect: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    queue_size = max(8, min(int(os.environ.get("TCODE_AGUI_SSE_QUEUE_SIZE", "128")), 4096))
    heartbeat = max(1.0, min(float(os.environ.get("TCODE_AGUI_HEARTBEAT_SECONDS", "15")), 60.0))
    queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue(queue_size)

    async def produce() -> None:
        try:
            async for event in source:
                await queue.put(event)
        except BaseException as exc:
            await queue.put(exc)
        finally:
            await queue.put(None)

    producer = asyncio.create_task(produce(), name="agui-sse-producer")
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                if isinstance(item, asyncio.CancelledError):
                    raise item
                raise item
            yield encode_sse_data(item)
    finally:
        if not producer.done() and disconnect_policy == "cancel":
            producer.cancel()
            if on_disconnect is not None:
                await on_disconnect()
        await asyncio.gather(producer, return_exceptions=True)


__all__ = ["bounded_sse_stream", "encode_sse_data"]
