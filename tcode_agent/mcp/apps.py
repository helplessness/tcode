"""MCP Apps 描述符、资源校验和短期访问令牌。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from ..sqlite_runtime import connect as sqlite_connect

from ..file_lock import append_locked_text, state_thread_lock


MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APP_SCHEMA = "tcode.mcp-app.v2"
MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
_TOOL_VISIBILITY_VALUES = frozenset({"model", "app"})
_RESOURCE_PERMISSION_VALUES = frozenset(
    {"camera", "microphone", "geolocation", "clipboardWrite"}
)
_AUDIT_LOCK = threading.RLock()


def _gateway_db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (
        Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
        / ".tcode"
        / "agent-gateway.sqlite3"
    ).resolve(strict=False)


def validate_resource_uri(value: Any) -> str:
    uri = str(value or "").strip()
    parsed = urlparse(uri)
    if parsed.scheme != "ui" or not (parsed.netloc or parsed.path):
        raise ValueError("MCP App resource URI must use the ui:// scheme")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("MCP App resource URI contains unsupported authority data")
    if len(uri) > 2_000:
        raise ValueError("MCP App resource URI is too long")
    return uri


def resource_uri_from_metadata(
    tool_metadata: Any,
    result: Any,
) -> str:
    candidates: list[Any] = []
    for source in (tool_metadata, result):
        if not isinstance(source, dict):
            continue
        meta = source.get("_meta") if isinstance(source.get("_meta"), dict) else source
        ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
        candidates.extend(
            [
                ui.get("resourceUri"),
                ui.get("resource_uri"),
                meta.get("ui/resourceUri"),
                meta.get("ui.resourceUri"),
            ]
        )
        for block in source.get("content", []) if isinstance(source.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            resource = block.get("resource") if isinstance(block.get("resource"), dict) else {}
            mime = str(resource.get("mimeType") or resource.get("mime_type") or "").lower()
            if mime == MCP_APP_MIME_TYPE:
                candidates.append(resource.get("uri"))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return validate_resource_uri(candidate)
        except ValueError:
            continue
    return ""


def tool_visibility(tool_metadata: Any) -> frozenset[str]:
    """Return the MCP Apps visibility set, failing closed on malformed values."""

    if not isinstance(tool_metadata, dict):
        return frozenset({"model", "app"})
    meta = (
        tool_metadata.get("_meta")
        if isinstance(tool_metadata.get("_meta"), dict)
        else tool_metadata
    )
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    raw = ui.get("visibility")
    if raw is None:
        return frozenset({"model", "app"})
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        str(value).strip().lower()
        for value in raw
        if str(value).strip().lower() in _TOOL_VISIBILITY_VALUES
    )


def tool_is_visible_to_model(tool_metadata: Any) -> bool:
    return "model" in tool_visibility(tool_metadata)


def tool_is_visible_to_app(tool_metadata: Any) -> bool:
    return "app" in tool_visibility(tool_metadata)


def _valid_csp_source(value: Any, *, allow_websocket: bool = False) -> str:
    source = str(value or "").strip()
    if not source or len(source) > 500 or any(char in source for char in "\r\n; "):
        return ""
    candidate = source.replace("*.", "wildcard.", 1)
    parsed = urlparse(candidate)
    schemes = {"https"}
    if allow_websocket:
        schemes.add("wss")
    if parsed.scheme not in schemes or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return source


def sanitize_resource_ui_metadata(value: Any) -> dict[str, Any]:
    """Normalize the resource `_meta.ui` fields understood by the Host."""

    if not isinstance(value, dict):
        return {}
    meta = value.get("_meta") if isinstance(value.get("_meta"), dict) else value
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else meta
    if not isinstance(ui, dict):
        return {}
    normalized: dict[str, Any] = {}
    csp = ui.get("csp") if isinstance(ui.get("csp"), dict) else {}
    normalized_csp: dict[str, list[str]] = {}
    for key in (
        "connectDomains",
        "resourceDomains",
        "frameDomains",
        "baseUriDomains",
    ):
        raw = csp.get(key)
        if not isinstance(raw, list):
            continue
        sources: list[str] = []
        for item in raw[:32]:
            source = _valid_csp_source(
                item,
                allow_websocket=key == "connectDomains",
            )
            if source and source not in sources:
                sources.append(source)
        if sources:
            normalized_csp[key] = sources
    if normalized_csp:
        normalized["csp"] = normalized_csp

    permissions = ui.get("permissions")
    if isinstance(permissions, dict):
        normalized_permissions = {
            key: {}
            for key in _RESOURCE_PERMISSION_VALUES
            if key in permissions and isinstance(permissions.get(key), dict)
        }
        if normalized_permissions:
            normalized["permissions"] = normalized_permissions

    domain = str(ui.get("domain") or "").strip().lower()
    if domain and len(domain) <= 253 and re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,62}",
        domain,
    ):
        normalized["domain"] = domain
    if isinstance(ui.get("prefersBorder"), bool):
        normalized["prefersBorder"] = ui["prefersBorder"]
    return normalized


def _environment_allowlist(name: str) -> set[str]:
    return {
        item.strip()
        for item in str(os.environ.get(name) or "").split(",")
        if item.strip()
    }


def grant_resource_ui_metadata(value: Any) -> dict[str, Any]:
    """Intersect a resource request with explicit TCode host allowlists."""

    requested = sanitize_resource_ui_metadata(value)
    granted: dict[str, Any] = {}
    csp = requested.get("csp") if isinstance(requested.get("csp"), dict) else {}
    csp_env = {
        "connectDomains": "TCODE_MCP_APP_CONNECT_DOMAINS",
        "resourceDomains": "TCODE_MCP_APP_RESOURCE_DOMAINS",
        "frameDomains": "TCODE_MCP_APP_FRAME_DOMAINS",
        "baseUriDomains": "TCODE_MCP_APP_BASE_URI_DOMAINS",
    }
    granted_csp: dict[str, list[str]] = {}
    for key, env_name in csp_env.items():
        allowed = _environment_allowlist(env_name)
        values = [item for item in csp.get(key, []) if item in allowed]
        if values:
            granted_csp[key] = values
    if granted_csp:
        granted["csp"] = granted_csp

    # The first stable Host release intentionally grants no camera,
    # microphone or geolocation. Clipboard write can be enabled explicitly.
    allowed_permissions = _environment_allowlist(
        "TCODE_MCP_APP_ALLOWED_PERMISSIONS"
    ).intersection({"clipboardWrite"})
    requested_permissions = requested.get("permissions")
    if isinstance(requested_permissions, dict):
        permissions = {
            key: {}
            for key in requested_permissions
            if key in allowed_permissions
        }
        if permissions:
            granted["permissions"] = permissions
    if "prefersBorder" in requested:
        granted["prefersBorder"] = requested["prefersBorder"]
    return granted


def append_mcp_app_audit(
    workdir: str,
    *,
    event: str,
    session_id: str,
    server_name: str,
    app_id: str = "",
    resource_uri: str = "",
    tool_name: str = "",
    status: str = "authorized",
    details: dict[str, Any] | None = None,
) -> None:
    """Append a secret-safe MCP App authorization record."""

    root = Path(workdir).expanduser().resolve(strict=True)
    cursor = root
    for part in Path(".tcode/audit").parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise OSError("MCP Apps audit path cannot traverse a symbolic link")
    path = root / ".tcode" / "audit" / "mcp_apps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("MCP Apps audit file cannot be a symbolic link")
    safe_details = {
        str(key)[:120]: value
        for key, value in list((details or {}).items())[:32]
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    payload = {
        "schema": "tcode.mcp-app-audit.v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": str(event or "")[:120],
        "status": str(status or "")[:80],
        "session_id": str(session_id or "")[:200],
        "server_name": str(server_name or "")[:200],
        "app_id": str(app_id or "")[:200],
        "resource_uri": str(resource_uri or "")[:2_000],
        "tool_name": str(tool_name or "")[:300],
        "details": safe_details,
    }
    with state_thread_lock(_AUDIT_LOCK):
        append_locked_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def validate_resource_contents(
    contents: Any,
    expected_uri: str,
    *,
    fallback_metadata: Any = None,
) -> list[dict[str, Any]]:
    uri = validate_resource_uri(expected_uri)
    if not isinstance(contents, list) or not contents:
        raise ValueError("MCP App resource response has no contents")
    result: list[dict[str, Any]] = []
    total = 0
    for item in contents[:8]:
        if not isinstance(item, dict):
            continue
        item_uri = validate_resource_uri(item.get("uri"))
        if item_uri != uri:
            raise ValueError("MCP App resource response URI does not match the request")
        mime = str(item.get("mimeType") or item.get("mime_type") or "").strip().lower()
        if mime != MCP_APP_MIME_TYPE:
            raise ValueError("MCP App resource uses an unsupported MIME type")
        text = item.get("text")
        blob = item.get("blob")
        if not isinstance(text, str) and not isinstance(blob, str):
            raise ValueError("MCP App resource must contain text or blob")
        size = len((text if isinstance(text, str) else blob).encode("utf-8"))
        total += size
        if total > 2_000_000:
            raise ValueError("MCP App resource exceeds the 2 MB host limit")
        resource_meta = sanitize_resource_ui_metadata(item)
        if not resource_meta:
            resource_meta = sanitize_resource_ui_metadata(fallback_metadata)
        normalized_item = {
            "uri": item_uri,
            "mimeType": MCP_APP_MIME_TYPE,
            **({"text": text} if isinstance(text, str) else {"blob": blob}),
        }
        if resource_meta:
            normalized_item["_meta"] = {"ui": resource_meta}
        result.append(normalized_item)
    if not result:
        raise ValueError("MCP App resource response has no valid contents")
    return result


class MCPAppTokenStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _gateway_db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_app_tokens (
                    token_hash TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    server_name TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    resource_uri TEXT NOT NULL,
                    app_id TEXT NOT NULL DEFAULT '',
                    allowed_tools TEXT NOT NULL DEFAULT '[]',
                    allowed_resources TEXT NOT NULL DEFAULT '[]',
                    capability_digest TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(mcp_app_tokens)")
            }
            for name, definition in (
                ("app_id", "TEXT NOT NULL DEFAULT ''"),
                ("allowed_tools", "TEXT NOT NULL DEFAULT '[]'"),
                ("allowed_resources", "TEXT NOT NULL DEFAULT '[]'"),
                ("capability_digest", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE mcp_app_tokens ADD COLUMN {name} {definition}"
                    )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        session_id: str,
        server_name: str,
        tool_name: str,
        resource_uri: str,
        app_id: str = "",
        allowed_tools: list[str] | tuple[str, ...] = (),
        allowed_resources: list[str] | tuple[str, ...] = (),
        capability_digest: str = "",
        ttl_seconds: int = 600,
    ) -> str:
        uri = validate_resource_uri(resource_uri)
        if not session_id or not server_name:
            raise ValueError("session_id and server_name are required")
        token = "mcpapp_" + secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("DELETE FROM mcp_app_tokens WHERE expires_at<=?", (now,))
            connection.execute(
                """
                INSERT INTO mcp_app_tokens (
                    token_hash, session_id, server_name, tool_name,
                    resource_uri, app_id, allowed_tools, allowed_resources,
                    capability_digest, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._digest(token),
                    str(session_id)[:200],
                    str(server_name)[:200],
                    str(tool_name)[:300],
                    uri,
                    str(app_id)[:200],
                    json.dumps(sorted(set(allowed_tools)), ensure_ascii=True),
                    json.dumps(sorted(set(allowed_resources)), ensure_ascii=True),
                    str(capability_digest)[:128],
                    now + max(30, min(int(ttl_seconds), 3600)),
                    now,
                ),
            )
            connection.commit()
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        token = str(token or "").strip()
        if not token.startswith("mcpapp_"):
            return None
        digest = self._digest(token)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM mcp_app_tokens WHERE token_hash=? AND expires_at>?",
                (digest, time.time()),
            ).fetchone()
        if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
            return None
        record = dict(row)
        for key in ("allowed_tools", "allowed_resources"):
            try:
                parsed = json.loads(str(record.get(key) or "[]"))
            except Exception:
                parsed = []
            record[key] = [str(item) for item in parsed] if isinstance(parsed, list) else []
        return record


def build_app_id(server_name: str, tool_name: str, resource_uri: str) -> str:
    identity = f"{server_name}\0{tool_name}\0{resource_uri}".encode("utf-8")
    return "app_" + hashlib.sha256(identity).hexdigest()[:24]


def build_mcp_app_descriptor(
    *,
    session_id: str,
    server_name: str,
    tool_name: str,
    tool_input: Any = None,
    tool_metadata: Any,
    result: Any,
    token_store: MCPAppTokenStore | None = None,
) -> dict[str, Any] | None:
    uri = resource_uri_from_metadata(tool_metadata, result)
    if not uri or not session_id:
        return None
    # `token_store` remains accepted for source compatibility. Access tokens
    # are issued when the authenticated frontend resolves this stable block.
    del token_store
    safe_result = {
        key: result.get(key)
        for key in ("content", "structuredContent", "isError", "_meta")
        if isinstance(result, dict) and key in result
    }
    try:
        if len(json.dumps(safe_result, ensure_ascii=False, default=str).encode()) > 512_000:
            safe_result = {
                "content": [
                    {
                        "type": "text",
                        "text": "Tool result is too large for inline UI transfer",
                    }
                ],
                "isError": False,
            }
    except Exception:
        safe_result = {"content": [], "isError": False}
    return {
        "type": "mcp_app",
        "schema": MCP_APP_SCHEMA,
        "app_id": build_app_id(server_name, tool_name, uri),
        "session_id": session_id,
        "server_name": server_name,
        "tool_name": tool_name,
        "resource_uri": uri,
        "mime_type": MCP_APP_MIME_TYPE,
        "tool_input": tool_input if isinstance(tool_input, dict) else {},
        "tool_result": safe_result,
        "fallback": f"交互界面暂不可用：{server_name}.{tool_name}",
    }


__all__ = [
    "MCP_APP_MIME_TYPE",
    "MCP_APP_SCHEMA",
    "MCP_APPS_EXTENSION_ID",
    "MCPAppTokenStore",
    "append_mcp_app_audit",
    "build_app_id",
    "build_mcp_app_descriptor",
    "grant_resource_ui_metadata",
    "resource_uri_from_metadata",
    "sanitize_resource_ui_metadata",
    "tool_is_visible_to_app",
    "tool_is_visible_to_model",
    "tool_visibility",
    "validate_resource_contents",
    "validate_resource_uri",
]
