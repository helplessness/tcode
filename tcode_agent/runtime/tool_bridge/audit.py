"""Secret-safe append-only audit records for platform bridge calls."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_SECRET_KEYS = {"token", "authorization", "cookie", "password", "secret", "api_key", "headers"}


def _redact(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if any(marker in key.lower() for marker in _SECRET_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key)[:200]: _redact(item, key=str(item_key), depth=depth + 1)
            for item_key, item in list(value.items())[:80]
        }
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:80]]
    if isinstance(value, str):
        text = value[:4000]
        if text.startswith(("http://", "https://")):
            return "[endpoint-redacted]"
        return text
    return value


def append_bridge_audit(
    workdir: str,
    *,
    event: str,
    snapshot_id: str,
    token_id: str,
    runtime_id: str,
    session_id: str,
    run_id: str,
    capability_id: str = "",
    tool_name: str = "",
    status: str = "",
    details: dict[str, Any] | None = None,
) -> str:
    sanitized = _redact(details or {})
    digest = "sha256:" + hashlib.sha256(
        json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    payload = {
        "schema": "tcode.platform-bridge-audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": str(event or "")[:120],
        "snapshot_id": str(snapshot_id or "")[:120],
        "token_id": str(token_id or "")[:120],
        "runtime_id": str(runtime_id or "")[:200],
        "session_id": str(session_id or "")[:200],
        "run_id": str(run_id or "")[:200],
        "capability_id": str(capability_id or "")[:120],
        "tool_name": str(tool_name or "")[:200],
        "status": str(status or "")[:80],
        "details_digest": digest,
        "details": sanitized,
    }
    root = Path(workdir).expanduser().resolve(strict=True)
    cursor = root
    for part in Path(".tcode/audit").parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise OSError("Platform Bridge audit path cannot traverse a symbolic link")
    path = root / ".tcode" / "audit" / "platform_bridge.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Platform Bridge audit file cannot be a symbolic link")
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return digest


__all__ = ["append_bridge_audit"]
