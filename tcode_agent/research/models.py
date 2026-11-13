"""Versioned ResearchEvent schema and sanitisation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import ResearchSettings


RESEARCH_EVENT_SCHEMA_VERSION = 1
RESEARCH_EVENT_INGEST_VERSION = "research-journal-1"

_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(
        r"(?i)\b(?:[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"password|passwd|secret|private[_-]?key|credential)[A-Za-z0-9_]*)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\b(?:authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"),
)
_REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "set_cookie",
    "password",
    "passwd",
    "passphrase",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "access_key",
    "secret_access_key",
    "private_key",
    "credential",
    "credentials",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_password",
    "_passwd",
    "_passphrase",
    "_secret",
    "_access_token",
    "_refresh_token",
    "_api_key",
    "_private_key",
    "_credential",
    "_credentials",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES):
        return True
    compact = normalized.replace("_", "")
    return compact in {
        "authorization",
        "proxyauthorization",
        "setcookie",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "accesskey",
        "secretaccesskey",
        "privatekey",
    }


def _redact_string(value: str, *, max_chars: int) -> str:
    cleaned = str(value or "")
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", cleaned, re.IGNORECASE):
        return _REDACTED
    for pattern in _INLINE_SECRET_PATTERNS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    if max_chars >= 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def sanitize_value(
    value: Any,
    *,
    max_string_chars: int,
    store_content_preview: bool = True,
    _depth: int = 0,
) -> Any:
    """Return a bounded, JSON-compatible value with credential material removed."""

    if _depth > 8:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value, max_chars=max_string_chars)
    if isinstance(value, bytes):
        return f"[BYTES:{len(value)}]"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 128:
                sanitized["__truncated_items__"] = len(value) - index
                break
            key = str(raw_key or "")[:160]
            if _is_sensitive_key(key):
                sanitized[key] = _REDACTED
                continue
            if not store_content_preview and key.lower() in {
                "content",
                "content_preview",
                "output",
                "output_preview",
                "error",
                "error_preview",
                "message",
                "prompt",
            }:
                sanitized[key] = "[PREVIEW_DISABLED]"
                continue
            sanitized[key] = sanitize_value(
                item,
                max_string_chars=max_string_chars,
                store_content_preview=store_content_preview,
                _depth=_depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            sanitize_value(
                item,
                max_string_chars=max_string_chars,
                store_content_preview=store_content_preview,
                _depth=_depth + 1,
            )
            for item in items[:128]
        ]
        if len(items) > 128:
            result.append({"__truncated_items__": len(items) - 128})
        return result
    return _redact_string(str(value), max_chars=max_string_chars)


def _bounded_payload(payload: dict[str, Any], settings: ResearchSettings) -> dict[str, Any]:
    sanitized = sanitize_value(
        payload,
        max_string_chars=settings.content_preview_chars,
        store_content_preview=settings.store_content_preview,
    )
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    serialized = canonical_json(sanitized)
    if len(serialized.encode("utf-8")) <= settings.max_payload_bytes:
        return sanitized
    return {
        "truncated": True,
        "original_sanitized_bytes": len(serialized.encode("utf-8")),
        "preview": _redact_string(
            serialized,
            max_chars=max(0, settings.max_payload_bytes // 4),
        ),
    }


def _owner(value: Any, env_name: str, default: str) -> str:
    normalized = str(value or os.environ.get(env_name) or default).strip() or default
    return normalized[:128]


@dataclass(frozen=True)
class ResearchEventInput:
    event_type: str
    session_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    turn_id: str = ""
    trace_id: str = ""
    parent_event_id: str = ""
    actor_type: str = "system"
    actor_id: str = ""
    source_kind: str = "runtime"
    source_id: str = ""
    source_ref: str = ""
    occurred_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    privacy_state: str = "private"
    stable_state_version: str = ""
    idempotency_key: str = ""

    def build(self, settings: ResearchSettings) -> "ResearchEvent":
        event_type = str(self.event_type or "").strip().lower()
        if not event_type:
            raise ValueError("event_type is required")

        payload = _bounded_payload(dict(self.payload or {}), settings)
        source_kind = str(self.source_kind or "runtime").strip()[:80] or "runtime"
        source_id = str(self.source_id or self.session_id or "unknown").strip()[:240]
        state_version = str(self.stable_state_version or "").strip()[:160]
        idempotency_key = str(self.idempotency_key or "").strip()
        if not idempotency_key:
            idempotency_key = sha256_text(
                canonical_json(
                    {
                        "source_kind": source_kind,
                        "source_id": source_id,
                        "event_type": event_type,
                        "stable_state_version": state_version,
                    }
                )
            )

        content_hash = sha256_text(canonical_json(payload))
        return ResearchEvent(
            event_id=f"rev-{uuid.uuid4()}",
            schema_version=RESEARCH_EVENT_SCHEMA_VERSION,
            tenant_id=_owner(self.tenant_id, "TCODE_DEFAULT_TENANT_ID", "admin_tenant"),
            user_id=_owner(self.user_id, "TCODE_DEFAULT_USER_ID", "local-user"),
            workspace_id=_owner(
                self.workspace_id,
                "TCODE_DEFAULT_WORKSPACE_ID",
                "workspace-1",
            ),
            session_id=str(self.session_id or "").strip()[:240],
            turn_id=str(self.turn_id or "").strip()[:240],
            trace_id=str(self.trace_id or "").strip()[:240],
            parent_event_id=str(self.parent_event_id or "").strip()[:240],
            actor_type=str(self.actor_type or "system").strip().lower()[:40],
            actor_id=str(self.actor_id or "").strip()[:160],
            event_type=event_type,
            source_kind=source_kind,
            source_id=source_id,
            source_ref=_redact_string(str(self.source_ref or ""), max_chars=1000),
            occurred_at=str(self.occurred_at or utc_now_iso()),
            ingested_at=utc_now_iso(),
            payload=payload,
            content_hash=content_hash,
            privacy_state=str(self.privacy_state or "private").strip().lower()[:40],
            ingest_version=RESEARCH_EVENT_INGEST_VERSION,
            idempotency_key=idempotency_key[:180],
        )


@dataclass(frozen=True)
class ResearchEvent:
    event_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    trace_id: str
    parent_event_id: str
    actor_type: str
    actor_id: str
    event_type: str
    source_kind: str
    source_id: str
    source_ref: str
    occurred_at: str
    ingested_at: str
    payload: dict[str, Any]
    content_hash: str
    privacy_state: str
    ingest_version: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchEvent":
        values = dict(row)
        try:
            payload = json.loads(str(values.pop("payload_json", "{}") or "{}"))
        except Exception:
            payload = {}
        values["payload"] = payload if isinstance(payload, dict) else {}
        return cls(**values)
