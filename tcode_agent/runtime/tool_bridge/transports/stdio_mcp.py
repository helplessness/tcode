"""Unix-socket client used by the unprivileged stdio MCP forwarder."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def forward_bridge_request(
    socket_path: str,
    token: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    if not socket_path or not token:
        raise RuntimeError("TCode platform bridge environment is incomplete")
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2_100_000)
    try:
        payload = {**request, "token": token}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > 2_000_000:
            raise RuntimeError("TCode platform bridge request exceeds 2 MB")
        writer.write(encoded + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=900)
        if not line or len(line) > 2_000_000:
            raise RuntimeError("TCode platform bridge response is invalid")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise RuntimeError("TCode platform bridge response is not an object")
        return response
    finally:
        writer.close()
        await writer.wait_closed()


__all__ = ["forward_bridge_request"]
