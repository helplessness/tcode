"""In-process Unix-socket authority for external Harness bridge calls."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..platform_capabilities.models import PlatformCapabilitySnapshot
from ..platform_capabilities.tokens import (
    BridgeTokenRecord,
    BridgeTokenStore,
)
from .audit import append_bridge_audit
from .tools import BridgeToolError, BridgeTools, TOOL_CAPABILITIES, visible_tool_definitions


class PlatformBridgeRuntime:
    """Owns opaque tokens and performs every privileged operation in TCode."""

    def __init__(self) -> None:
        self.tokens = BridgeTokenStore()
        self.tools = BridgeTools()
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket_path = ""
        self._lock = asyncio.Lock()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    async def ensure_started(self) -> str:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._server is not None and self._loop is loop and self._socket_path:
                return self._socket_path
            if self._server is not None:
                self._server.close()
                self._server = None
            if self._socket_path:
                Path(self._socket_path).unlink(missing_ok=True)
            base = Path(tempfile.gettempdir())
            path = base / f"tcode-platform-bridge-{os.getpid()}-{id(loop):x}.sock"
            path.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=path.as_posix(),
                limit=2_100_000,
            )
            os.chmod(path, 0o600)
            self._loop = loop
            self._socket_path = path.as_posix()
            return self._socket_path

    async def issue(
        self,
        snapshot: PlatformCapabilitySnapshot,
        context: Any,
    ) -> tuple[str, str]:
        socket_path = await self.ensure_started()
        expires_at = _parse_timestamp(snapshot.expires_at)
        token, record = self.tokens.issue(
            snapshot,
            context,
            expires_at=expires_at,
            max_calls=snapshot.max_calls,
        )
        append_bridge_audit(
            context.workdir,
            event="token.issued",
            snapshot_id=snapshot.snapshot_id,
            token_id=record.token_id,
            runtime_id=snapshot.runtime_id,
            session_id=snapshot.session_id,
            run_id=snapshot.run_id,
            status="active",
            details={
                "capabilities": snapshot.active_capability_ids(),
                "expires_at": snapshot.expires_at,
                "max_calls": snapshot.max_calls,
            },
        )
        return token, socket_path

    def revoke(self, token: str, reason: str) -> bool:
        try:
            record = self.tokens.validate(token)
        except Exception:
            record = None
        changed = self.tokens.revoke(token, reason)
        if record is not None:
            append_bridge_audit(
                record.context.workdir,
                event="token.revoked",
                snapshot_id=record.snapshot.snapshot_id,
                token_id=record.token_id,
                runtime_id=record.snapshot.runtime_id,
                session_id=record.snapshot.session_id,
                run_id=record.snapshot.run_id,
                status="revoked",
                details={"reason": reason},
            )
        return changed

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        token = str(request.get("token") or "")
        action = str(request.get("action") or "")
        if action == "list":
            record = self.tokens.validate(token)
            active = set(record.snapshot.active_capability_ids())
            return {
                "snapshot": record.snapshot.public_dict(),
                "tools": visible_tool_definitions(active),
                "remaining_calls": record.remaining_calls,
            }
        if action != "call":
            raise BridgeToolError("Unknown bridge action", code="bridge_action_invalid")
        name = str(request.get("name") or "").strip()
        capability_id = TOOL_CAPABILITIES.get(name)
        if not capability_id:
            raise BridgeToolError("Bridge tool is not published", code="bridge_tool_not_found")
        record = self.tokens.consume(token, capability_id)
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise BridgeToolError("Tool arguments must be an object", code="bridge_arguments_invalid")
        append_bridge_audit(
            record.context.workdir,
            event="tool.started",
            snapshot_id=record.snapshot.snapshot_id,
            token_id=record.token_id,
            runtime_id=record.snapshot.runtime_id,
            session_id=record.snapshot.session_id,
            run_id=record.snapshot.run_id,
            capability_id=capability_id,
            tool_name=name,
            status="running",
            details={"argument_digest": _digest(arguments)},
        )
        try:
            result = await self.tools.call(name, arguments, record)
        except Exception as exc:
            append_bridge_audit(
                record.context.workdir,
                event="tool.finished",
                snapshot_id=record.snapshot.snapshot_id,
                token_id=record.token_id,
                runtime_id=record.snapshot.runtime_id,
                session_id=record.snapshot.session_id,
                run_id=record.snapshot.run_id,
                capability_id=capability_id,
                tool_name=name,
                status="error",
                details={"error_code": str(getattr(exc, "code", type(exc).__name__))},
            )
            raise
        append_bridge_audit(
            record.context.workdir,
            event="tool.finished",
            snapshot_id=record.snapshot.snapshot_id,
            token_id=record.token_id,
            runtime_id=record.snapshot.runtime_id,
            session_id=record.snapshot.session_id,
            run_id=record.snapshot.run_id,
            capability_id=capability_id,
            tool_name=name,
            status="completed",
            details={"result_digest": _digest(result)},
        )
        return {"result": result, "remaining_calls": record.remaining_calls}

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any]
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not line or len(line) > 2_000_000:
                raise BridgeToolError("Bridge request exceeds the size limit", code="bridge_request_too_large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise BridgeToolError("Bridge request must be an object", code="bridge_request_invalid")
            response = {"ok": True, **await self.dispatch(request)}
        except Exception as exc:
            response = {
                "ok": False,
                "error": {
                    "code": str(getattr(exc, "code", "bridge_request_failed")),
                    "message": str(exc)[:2000] or type(exc).__name__,
                },
            }
        try:
            writer.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _digest(value: Any) -> str:
    return "sha256:" + __import__("hashlib").sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _parse_timestamp(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


platform_bridge_runtime = PlatformBridgeRuntime()


__all__ = ["PlatformBridgeRuntime", "platform_bridge_runtime"]
