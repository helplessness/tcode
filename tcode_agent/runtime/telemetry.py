"""统一遥测脱敏与有界、非阻塞导出通道。"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any, Optional


_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:access_?token|refresh_?token|token|secret|client_?secret|"
    r"password|passwd|authorization|cookie|api_?key|private_?key|credential)(?:$|_)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_INLINE_SECRET_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|token|secret|client[_-]?secret|"
    r"password|passwd|authorization|cookie|api[_-]?key|private[_-]?key|credential)"
    r"\b\s*[:=]\s*)([^\s,;&]+)"
)
_REDACTED = "<redacted>"


def _normalized_key(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _configured_secret_values() -> tuple[str, ...]:
    values: list[str] = []
    for key, value in os.environ.items():
        if _SENSITIVE_KEY_RE.search(_normalized_key(key)) and len(str(value or "")) >= 6:
            values.append(str(value))
    return tuple(sorted(set(values), key=len, reverse=True))


def redact_telemetry_text(value: Any, *, limit: int = 12_000) -> str:
    """脱敏字符串中的 Bearer、常见凭据赋值和当前进程已配置 Secret。"""

    text = str(value or "")
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _INLINE_SECRET_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    for secret in _configured_secret_values():
        text = text.replace(secret, _REDACTED)
    maximum = max(80, int(limit or 12_000))
    return text if len(text) <= maximum else text[: maximum - 24] + " ...[truncated]"


def redact_telemetry_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    text_limit: int = 12_000,
    list_limit: int = 100,
    dict_limit: int = 100,
    depth_limit: int = 6,
) -> Any:
    """递归脱敏并限制诊断数据规模；所有遥测出口共用这一套规则。"""

    if _SENSITIVE_KEY_RE.search(_normalized_key(key)):
        return _REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_telemetry_text(value, limit=text_limit)
    if depth >= max(1, depth_limit):
        return "<depth-limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())
        for item_key, item_value in items[: max(1, dict_limit)]:
            normalized_item_key = redact_telemetry_text(item_key, limit=160)
            result[normalized_item_key] = redact_telemetry_value(
                item_value,
                key=normalized_item_key,
                depth=depth + 1,
                text_limit=text_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
        if len(items) > dict_limit:
            result["__truncated_keys__"] = len(items) - dict_limit
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            redact_telemetry_value(
                item,
                depth=depth + 1,
                text_limit=text_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
            for item in items[: max(1, list_limit)]
        ]
        if len(items) > list_limit:
            result.append(f"...[{len(items) - list_limit} more items]")
        return result
    return redact_telemetry_text(repr(value), limit=min(text_limit, 1_024))


class TelemetryGateway:
    """进程内统一遥测出口；执行线程永不等待外部 exporter。"""

    _lock = threading.RLock()
    _recent: deque[dict[str, Any]] = deque(maxlen=200)
    _export_queue: Optional[queue.Queue[dict[str, Any] | None]] = None
    _exporter: Optional[Callable[[list[dict[str, Any]]], None]] = None
    _worker: Optional[threading.Thread] = None
    _batch_size = 20
    _accepted = 0
    _exported = 0
    _dropped = 0
    _export_failures = 0

    @classmethod
    def emit(
        cls,
        event_name: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        tenant_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
        session_id: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        event = {
            "event_id": f"tel-{uuid.uuid4()}",
            "event_name": str(event_name or "unknown")[:160],
            "created_at": time.time(),
            "tenant_id": str(tenant_id or "")[:128],
            "user_id": str(user_id or "")[:128],
            "workspace_id": str(workspace_id or "")[:128],
            "session_id": str(session_id or "")[:160],
            "request_id": str(request_id or "")[:160],
            "payload": redact_telemetry_value(dict(payload or {})),
        }
        with cls._lock:
            cls._accepted += 1
            cls._recent.append(event)
            export_queue = cls._export_queue
        if export_queue is not None:
            try:
                export_queue.put_nowait(event)
            except queue.Full:
                with cls._lock:
                    cls._dropped += 1
        return dict(event)

    @classmethod
    def configure_exporter(
        cls,
        exporter: Optional[Callable[[list[dict[str, Any]]], None]],
        *,
        queue_size: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        """配置可选 exporter；队列满时丢弃遥测，不拖慢业务执行。"""

        cls._stop_worker()
        if exporter is None:
            return
        configured_queue_size = queue_size or int(
            os.environ.get("TCODE_TELEMETRY_QUEUE_MAX", "1000") or "1000"
        )
        configured_batch_size = batch_size or int(
            os.environ.get("TCODE_TELEMETRY_BATCH_SIZE", "20") or "20"
        )
        with cls._lock:
            cls._exporter = exporter
            cls._batch_size = max(1, min(int(configured_batch_size), 200))
            cls._export_queue = queue.Queue(maxsize=max(1, int(configured_queue_size)))
            cls._worker = threading.Thread(
                target=cls._export_loop,
                name="tcode-telemetry-exporter",
                daemon=True,
            )
            cls._worker.start()

    @classmethod
    def _export_loop(cls) -> None:
        while True:
            with cls._lock:
                export_queue = cls._export_queue
                exporter = cls._exporter
                batch_size = cls._batch_size
            if export_queue is None or exporter is None:
                return
            first = export_queue.get()
            if first is None:
                export_queue.task_done()
                return
            batch = [first]
            while len(batch) < batch_size:
                try:
                    item = export_queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    export_queue.task_done()
                    break
                batch.append(item)
            try:
                exporter(batch)
                with cls._lock:
                    cls._exported += len(batch)
            except Exception:
                with cls._lock:
                    cls._export_failures += len(batch)
            finally:
                for _item in batch:
                    export_queue.task_done()

    @classmethod
    def _stop_worker(cls) -> None:
        with cls._lock:
            export_queue = cls._export_queue
            worker = cls._worker
            cls._export_queue = None
            cls._exporter = None
            cls._worker = None
        if export_queue is not None:
            try:
                export_queue.put_nowait(None)
            except queue.Full:
                pass
        if worker is not None and worker.is_alive():
            worker.join(timeout=0.2)

    @classmethod
    def snapshot(cls, *, include_recent: bool = False) -> dict[str, Any]:
        with cls._lock:
            result = {
                "accepted": cls._accepted,
                "exported": cls._exported,
                "dropped": cls._dropped,
                "export_failures": cls._export_failures,
                "queue_depth": cls._export_queue.qsize() if cls._export_queue else 0,
                "queue_limit": cls._export_queue.maxsize if cls._export_queue else 0,
            }
            if include_recent:
                result["recent"] = [dict(item) for item in cls._recent]
            return result

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._stop_worker()
        with cls._lock:
            cls._recent = deque(maxlen=200)
            cls._accepted = 0
            cls._exported = 0
            cls._dropped = 0
            cls._export_failures = 0


__all__ = [
    "TelemetryGateway",
    "redact_telemetry_text",
    "redact_telemetry_value",
]
