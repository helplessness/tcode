"""Per-connection WebSocket sending, including backpressure and disconnects."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..protocol.v2.adapters import TCodeWebSocketAdapter


def websocket_send_timeout_seconds() -> float:
    """Read the per-frame timeout, bounded to 1–300 seconds."""
    try:
        raw = float(os.environ.get("TCODE_WS_SEND_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        raw = 30.0
    return max(1.0, min(raw, 300.0))


class ChatTransport:
    """Keep send serialization and protocol routing local to one connection.

    Capture the original sender before the endpoint installs ``send_json``.
    The v2 emitter uses ``send_raw_json`` to avoid routing its own events back
    through the adapter.
    """

    def __init__(self, websocket: WebSocket):
        self._raw_send_json = websocket.send_json
        self._send_lock = asyncio.Lock()
        self.closed = False
        self.adapter: TCodeWebSocketAdapter | None = None

    async def send_raw_json(self, data: Any, mode: str = "text") -> bool:
        """Send one frame; only disconnects and slow consumers are suppressed."""
        if self.closed:
            return False
        async with self._send_lock:
            if self.closed:
                return False
            try:
                await asyncio.wait_for(
                    self._raw_send_json(data, mode=mode),
                    timeout=websocket_send_timeout_seconds(),
                )
                return True
            except (asyncio.TimeoutError, WebSocketDisconnect):
                self.closed = True
                return False
            except RuntimeError as exc:
                error_text = str(exc).lower()
                if (
                    "close message has been sent" in error_text
                    or "after sending 'websocket.close'" in error_text
                    or 'after sending "websocket.close"' in error_text
                ):
                    self.closed = True
                    return False
                raise

    async def send_json(self, data: Any, mode: str = "text") -> bool:
        if self.adapter is not None:
            return await self.adapter.send(data)
        return await self.send_raw_json(data, mode=mode)
