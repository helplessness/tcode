"""可撤销、只存哈希的 AG-UI API Key。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from ..sqlite_runtime import connect as sqlite_connect

if TYPE_CHECKING:
    from ..server.auth import CurrentUserContext


DEFAULT_SCOPES = {
    "agents:read",
    "runtimes:read",
    "runtimes:execute",
    "runs:create",
    "runs:cancel",
    "threads:read",
}
KNOWN_SCOPES = DEFAULT_SCOPES | {"runs:detach", "mcp_apps:invoke"}


def _db_path() -> Path:
    configured = str(os.environ.get("TCODE_AGENT_GATEWAY_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd())) / ".tcode" / "agent-gateway.sqlite3").resolve(strict=False)


def _digest(token: str) -> str:
    pepper = str(os.environ.get("TCODE_API_KEY_PEPPER") or "")
    return hashlib.sha256(f"{pepper}\0{token}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class APIKeyPrincipal:
    key_id: str
    user: "CurrentUserContext"
    scopes: frozenset[str]
    workspace_ids: frozenset[str]
    allowed_runtime_ids: frozenset[str]
    expires_at: float

    def permits(self, scope: str) -> bool:
        return scope in self.scopes


class APIKeyStore:
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or _db_path()).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_api_keys (
                    key_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'member',
                    scopes_json TEXT NOT NULL,
                    workspace_ids_json TEXT NOT NULL,
                    runtime_ids_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            connection.commit()

    def create(
        self,
        user: "CurrentUserContext",
        *,
        scopes: set[str] | None = None,
        workspace_ids: set[str] | None = None,
        allowed_runtime_ids: set[str] | None = None,
        ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> tuple[str, dict[str, Any]]:
        selected_scopes = set(scopes or DEFAULT_SCOPES)
        unknown = selected_scopes - KNOWN_SCOPES
        if unknown:
            raise ValueError("unknown API key scopes: " + ", ".join(sorted(unknown)))
        key_id = "key_" + uuid.uuid4().hex
        token = f"tcode_{key_id}_{secrets.token_urlsafe(32)}"
        now = time.time()
        expires_at = now + max(60, min(int(ttl_seconds), 365 * 24 * 60 * 60))
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO agui_api_keys (
                    key_id, token_hash, tenant_id, user_id, email, name, role,
                    scopes_json, workspace_ids_json, runtime_ids_json,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    _digest(token),
                    user.tenant_id,
                    user.user_id,
                    user.email,
                    user.name,
                    user.role,
                    json.dumps(sorted(selected_scopes)),
                    json.dumps(sorted(workspace_ids or set())),
                    json.dumps(sorted(allowed_runtime_ids or set())),
                    expires_at,
                    now,
                ),
            )
            connection.commit()
        return token, {
            "key_id": key_id,
            "scopes": sorted(selected_scopes),
            "workspace_ids": sorted(workspace_ids or set()),
            "allowed_runtime_ids": sorted(allowed_runtime_ids or set()),
            "expires_at": expires_at,
        }

    def authenticate(self, token: str) -> APIKeyPrincipal | None:
        from ..server.auth import CurrentUserContext

        token = str(token or "").strip()
        if not token.startswith("tcode_key_"):
            return None
        parts = token.split("_", 3)
        if len(parts) != 4:
            return None
        key_id = f"key_{parts[2]}"
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM agui_api_keys WHERE key_id=?", (key_id,)
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            if float(record.get("revoked_at") or 0) > 0 or float(record.get("expires_at") or 0) <= now:
                return None
            if not hmac.compare_digest(str(record.get("token_hash") or ""), _digest(token)):
                return None
            connection.execute(
                "UPDATE agui_api_keys SET last_used_at=? WHERE key_id=?", (now, key_id)
            )
            connection.commit()
        return APIKeyPrincipal(
            key_id=key_id,
            user=CurrentUserContext(
                user_id=str(record["user_id"]),
                tenant_id=str(record["tenant_id"]),
                email=str(record["email"]),
                name=str(record.get("name") or ""),
                role=str(record.get("role") or "member"),
            ),
            scopes=frozenset(json.loads(str(record.get("scopes_json") or "[]"))),
            workspace_ids=frozenset(json.loads(str(record.get("workspace_ids_json") or "[]"))),
            allowed_runtime_ids=frozenset(json.loads(str(record.get("runtime_ids_json") or "[]"))),
            expires_at=float(record["expires_at"]),
        )

    def revoke(self, key_id: str, *, tenant_id: str, user_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE agui_api_keys SET revoked_at=?
                WHERE key_id=? AND tenant_id=? AND user_id=? AND revoked_at=0
                """,
                (time.time(), key_id, tenant_id, user_id),
            )
            connection.commit()
        return cursor.rowcount > 0


def bearer_token(authorization: str) -> str:
    value = str(authorization or "").strip()
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


__all__ = [
    "APIKeyPrincipal",
    "APIKeyStore",
    "DEFAULT_SCOPES",
    "KNOWN_SCOPES",
    "bearer_token",
]
