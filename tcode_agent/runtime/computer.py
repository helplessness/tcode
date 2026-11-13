"""Authenticated desktop computer-use broker client.

The launch credential is delivered on the existing private desktop stdin pipe
and kept in process memory.  It is never placed in argv, the environment, tool
arguments, model-visible results, or logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_client: "ComputerBrokerClient | None" = None

COMPUTER_MODEL_TOOLS = frozenset({
    "computer_discover", "computer_observe", "computer_element_action",
    "computer_pointer_action", "computer_type_text", "computer_press_key",
    "computer_scroll", "computer_control",
})
COMPUTER_TOOL_IDS = frozenset({"computer", "computer_input_action", *COMPUTER_MODEL_TOOLS})


def is_computer_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in COMPUTER_TOOL_IDS


def computer_operation(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    if tool_name == "computer_discover":
        return "list_apps"
    if tool_name in {"computer_type_text", "computer_press_key", "computer_scroll"}:
        return tool_name.removeprefix("computer_")
    return str((arguments or {}).get("operation") or "").strip()

# Protocol operation classes are deliberately explicit and exhaustive.  The
# desktop Host owns the matching DRIVER_CONTRACTS table; the cross-layer test
# fails whenever either side adds an operation without classifying it here.
COMPUTER_READ_OPERATIONS = frozenset({
    "capabilities", "list_apps", "list_windows", "observe", "status", "query_action",
})
COMPUTER_WRITE_OPERATIONS = frozenset({
    "click_element", "right_click", "open_menu", "select_option",
    "set_value", "click_at", "double_click", "scroll",
    "type_text", "press_key", "activate_app",
})
COMPUTER_CONTROL_OPERATIONS = frozenset({"release_control", "end_task"})
COMPUTER_APPROVAL_CAPABLE_OPERATIONS = frozenset({"observe", *COMPUTER_WRITE_OPERATIONS})
# Cross-layer protocol constant: Host-issued observation refs are exactly
# ``obs_`` plus 32 lowercase hexadecimal characters (36 characters total).
COMPUTER_OBSERVATION_REF_PATTERN = r"^obs_[a-f0-9]{32}$"


class ComputerBrokerError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = str(code or "driver_unavailable")


@dataclass(frozen=True)
class ComputerBrokerConfig:
    socket_path: str
    secret: str

    def __post_init__(self) -> None:
        if not self.socket_path or len(self.socket_path) > 4096:
            raise ValueError("invalid computer broker socket")
        if len(self.secret) != 64 or any(c not in "0123456789abcdef" for c in self.secret):
            raise ValueError("invalid computer broker credential")


class ComputerBrokerClient:
    MAX_FRAME = 4 * 1024 * 1024

    def __init__(self, config: ComputerBrokerConfig) -> None:
        self.config = config

    @property
    def capture_root(self) -> str:
        return str((Path(self.config.socket_path).parent / "captures").resolve())

    async def request(self, operation: str, payload: dict[str, Any], *, timeout: float = 35.0) -> dict[str, Any]:
        writer = None
        request_sent = False
        write_operation = operation in COMPUTER_WRITE_OPERATIONS
        # Treat the harness call id as the logical request id.  A transport
        # retry/replay of the same tool call must query or hit the broker's
        # existing ledger entry, never mint a second GUI action.  Requests
        # without a call id (internal/tests) retain a fresh nonce.
        logical_call_id = str(payload.get("call_id") or "")
        action_id = ""
        if write_operation:
            if logical_call_id:
                action_id = hmac.new(
                    bytes.fromhex(self.config.secret),
                    f"computer-action-v1:{payload.get('task_id', '')}:{logical_call_id}:{operation}".encode(),
                    hashlib.sha256,
                ).hexdigest()[:32]
            else:
                action_id = secrets.token_hex(16)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.config.socket_path), timeout=3
            )
            challenge = await self._read(reader)
            if challenge.get("type") != "challenge" or challenge.get("protocol") != 1:
                raise ComputerBrokerError("authentication_failed")
            nonce = str(challenge.get("nonce") or "")
            digest = hmac.new(
                bytes.fromhex(self.config.secret),
                f"1:{nonce}".encode(),
                hashlib.sha256,
            ).hexdigest()
            await self._write(writer, {"type": "authenticate", "protocol": 1, "digest": digest})
            welcome = await self._read(reader)
            if welcome.get("type") != "welcome":
                raise ComputerBrokerError("authentication_failed")
            request_id = secrets.token_hex(16)
            if isinstance(payload.get("task_intent"), dict):
                payload = dict(payload)
                payload["task_intent_proof"] = hmac.new(
                    bytes.fromhex(self.config.secret),
                    json.dumps(payload["task_intent"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
                    hashlib.sha256,
                ).hexdigest()
            await self._write(writer, {
                "type": "request",
                "request_id": request_id,
                "operation": operation,
                "payload": payload,
                "broker_session_id": welcome.get("broker_session_id"),
                "driver_generation": welcome.get("driver_generation"),
                "control_epoch": welcome.get("control_epoch"),
                **({"action_id": action_id} if action_id else {}),
            })
            request_sent = True
            # Approval waiting is broker-owned and does not count as a normal
            # tool timeout.  Revocation/expiry produces the final response.
            # Host approval can legitimately remain pending for its ten-minute
            # validity window. Do not turn a human wait into a transport
            # timeout; ordinary discovery/status reads retain the short bound.
            approval_capable = operation in COMPUTER_APPROVAL_CAPABLE_OPERATIONS
            response = await asyncio.wait_for(
                self._read(reader), timeout=max(timeout, 10 * 60 + 5) if approval_capable else timeout
            )
            if response.get("request_id") != request_id:
                raise ComputerBrokerError("protocol_error")
            return response
        except ComputerBrokerError as exc:
            if write_operation and request_sent:
                status = await self._recover_sent_action(payload, action_id)
                if status is not None:
                    return status
                error = ComputerBrokerError("outcome_unknown")
                error.action_id = action_id
                raise error from exc
            raise
        except asyncio.TimeoutError as exc:
            # The broker owns the side-effect ledger.  Never infer that a
            # dispatched GUI action failed merely because this client timed out.
            status = await self._recover_sent_action(payload, action_id) if request_sent else None
            if status is not None:
                return status
            code = "outcome_unknown" if write_operation and request_sent else "broker_timeout"
            error = ComputerBrokerError(code)
            error.action_id = action_id
            raise error from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # A broken response channel is not evidence that an already-sent
            # GUI action did not run. Consult the host ledger by the original
            # action id; if that is also unreachable, fail conservatively and
            # never manufacture a fresh id/replay.
            status = await self._recover_sent_action(payload, action_id) if request_sent else None
            if status is not None:
                return status
            if write_operation and request_sent:
                error = ComputerBrokerError("outcome_unknown")
                error.action_id = action_id
                raise error from exc
            raise ComputerBrokerError("driver_unavailable") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _recover_sent_action(self, payload: dict[str, Any], action_id: str) -> dict[str, Any] | None:
        if not action_id:
            return None
        try:
            status = await self.request(
                "query_action",
                {"task_id": str(payload.get("task_id") or ""), "action_id": action_id},
                timeout=3,
            )
            return status if status.get("error_code") != "action_not_found" else None
        except ComputerBrokerError:
            return None

    async def _read(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        raw = await reader.readline()
        if not raw or len(raw) > self.MAX_FRAME or not raw.endswith(b"\n"):
            raise ComputerBrokerError("protocol_error")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ComputerBrokerError("protocol_error")
        return value

    async def _write(self, writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(raw) > self.MAX_FRAME:
            raise ComputerBrokerError("protocol_error")
        writer.write(raw)
        await writer.drain()


def configure_computer_broker(payload: Any) -> None:
    global _client
    if payload is None:
        _client = None
        return
    if not isinstance(payload, dict):
        raise ValueError("invalid computer broker startup payload")
    _client = ComputerBrokerClient(ComputerBrokerConfig(
        socket_path=str(payload.get("socket_path") or ""),
        secret=str(payload.get("secret") or ""),
    ))


def computer_broker_client() -> ComputerBrokerClient | None:
    return _client


async def end_computer_task(task_id: str, terminal_id: str) -> bool:
    """Idempotently release one completed/cancelled turn's host grants."""
    client = computer_broker_client()
    if client is None or not task_id or not terminal_id:
        return False
    terminal_key = hashlib.sha256(str(terminal_id).encode("utf-8")).hexdigest()
    try:
        result = await client.request(
            "end_task", {"task_id": str(task_id), "terminal_id": terminal_key}, timeout=5
        )
        return result.get("status") == "completed_known"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Computer task cleanup failed for %s: %s",
            str(task_id), type(exc).__name__,
        )
        return False
