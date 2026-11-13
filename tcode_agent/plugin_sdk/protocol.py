"""插件 Worker v2 使用的有界双向 JSON 行协议。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


WORKER_PROTOCOL = "tcode.plugin-worker.v2"
MAX_PROTOCOL_LINE_BYTES = 512 * 1024


class PluginWorkerProtocolError(RuntimeError):
    """表示 Worker 消息不符合版本、大小或结构约束。"""


def encode_message(message: Mapping[str, Any]) -> bytes:
    """编码一条协议消息，并在写出前执行硬大小限制。"""

    payload = json.dumps(
        dict(message),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(payload) > MAX_PROTOCOL_LINE_BYTES:
        raise PluginWorkerProtocolError("plugin worker message exceeds protocol limit")
    return payload


def decode_message(payload: bytes | str) -> dict[str, Any]:
    """解码并校验一条请求、响应或事件消息。"""

    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > MAX_PROTOCOL_LINE_BYTES:
        raise PluginWorkerProtocolError("plugin worker message exceeds protocol limit")
    try:
        message = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PluginWorkerProtocolError("plugin worker message is not valid JSON") from exc
    if not isinstance(message, dict):
        raise PluginWorkerProtocolError("plugin worker message must be an object")
    if message.get("protocol") != WORKER_PROTOCOL:
        raise PluginWorkerProtocolError("unsupported plugin worker protocol")
    message_type = str(message.get("type") or "")
    if message_type not in {"request", "response", "event", "cancel"}:
        raise PluginWorkerProtocolError("unsupported plugin worker message type")
    if message_type in {"request", "response", "cancel"} and not str(
        message.get("id") or ""
    ):
        raise PluginWorkerProtocolError("plugin worker message id is required")
    if message_type == "request" and not str(message.get("method") or ""):
        raise PluginWorkerProtocolError("plugin worker request method is required")
    return message


def request_message(
    request_id: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    parent_id: str = "",
) -> dict[str, Any]:
    """建立一条可由 Host 或 Worker 发起的请求消息。"""

    message: dict[str, Any] = {
        "protocol": WORKER_PROTOCOL,
        "type": "request",
        "id": str(request_id),
        "method": str(method),
        "params": dict(params or {}),
    }
    if parent_id:
        message["parent_id"] = str(parent_id)
    return message


def response_message(
    request_id: str,
    *,
    result: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """建立一条成功或失败响应，保证两种结果不会同时出现。"""

    message: dict[str, Any] = {
        "protocol": WORKER_PROTOCOL,
        "type": "response",
        "id": str(request_id),
        "ok": error is None,
    }
    if error is None:
        message["result"] = dict(result or {})
    else:
        message["error"] = dict(error)
    return message


__all__ = [
    "MAX_PROTOCOL_LINE_BYTES",
    "PluginWorkerProtocolError",
    "WORKER_PROTOCOL",
    "decode_message",
    "encode_message",
    "request_message",
    "response_message",
]
