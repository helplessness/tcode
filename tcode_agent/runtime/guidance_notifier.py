"""Wakeup backends for Guidance v2.

Notifications never contain user content and are only hints to re-query SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Protocol


class GuidanceNotifier(Protocol):
    async def publish(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        guidance_id: str,
    ) -> None: ...

    async def wait(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> bool: ...


def workspace_notification_key(workdir: str) -> str:
    normalized = Path(str(workdir or ".")).resolve(strict=False).as_posix()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class InProcessGuidanceNotifier:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str, str], asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def _event(self, key: tuple[str, str, str]) -> asyncio.Event:
        async with self._lock:
            return self._events.setdefault(key, asyncio.Event())

    async def publish(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        guidance_id: str,
    ) -> None:
        del guidance_id
        event = await self._event((workspace_key, session_id, turn_id))
        event.set()

    async def wait(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> bool:
        event = await self._event((workspace_key, session_id, turn_id))
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.01, timeout_seconds))
        except asyncio.TimeoutError:
            return False
        event.clear()
        return True


class RedisGuidanceNotifier:
    def __init__(self, redis_url: str) -> None:
        try:
            from redis import asyncio as redis_asyncio
        except ImportError as exc:  # pragma: no cover - depends on optional deployment
            raise RuntimeError("redis package is required for Redis guidance notifications") from exc
        self._client = redis_asyncio.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _channel(workspace_key: str, session_id: str) -> str:
        return f"tcode:guidance:{workspace_key}:{session_id}"

    async def publish(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        guidance_id: str,
    ) -> None:
        await self._client.publish(
            self._channel(workspace_key, session_id),
            json.dumps(
                {"turn_id": turn_id, "guidance_id": guidance_id},
                separators=(",", ":"),
            ),
        )

    async def wait(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> bool:
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self._channel(workspace_key, session_id))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.01, timeout_seconds)
        try:
            while loop.time() < deadline:
                message = await pubsub.get_message(
                    timeout=min(0.25, max(0.01, deadline - loop.time()))
                )
                if not isinstance(message, dict):
                    continue
                try:
                    payload = json.loads(str(message.get("data") or "{}"))
                except Exception:
                    payload = {}
                if not turn_id or str(payload.get("turn_id") or "") in {"", turn_id}:
                    return True
            return False
        finally:
            await pubsub.unsubscribe(self._channel(workspace_key, session_id))
            await pubsub.aclose()

    async def close(self) -> None:
        await self._client.aclose()
