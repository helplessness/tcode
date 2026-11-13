from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import HTTPException, Request, Response, WebSocket

from ..sqlite_runtime import SQLITE_CONNECTION_LOCK, connect as sqlite_connect
from . import workdirs as workdir_utils

AUTH_COOKIE_NAME = "tcode_auth_session"
_AUTH_LOCK = SQLITE_CONNECTION_LOCK
_SESSION_TTL_SECONDS = max(
    3600,
    int(os.environ.get("TCODE_AUTH_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 7)) or str(60 * 60 * 24 * 7)),
)
_UNSAFE_BOOTSTRAP_PASSWORDS = {
    "admin12345678",
    "password123456",
    "changeme12345",
}


@dataclass(frozen=True)
class CurrentUserContext:
    user_id: str
    tenant_id: str
    email: str
    name: str = ""
    role: str = "member"
    tenant_status: str = "active"


def auth_required() -> bool:
    raw = str(os.environ.get("TCODE_ENTERPRISE_MODE", os.environ.get("TCODE_AUTH_REQUIRED", "1")) or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _storage_base() -> Path:
    base = str(os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
    return Path(base) / ".tcode"


def _db_path() -> Path:
    return _storage_base() / "auth.sqlite"


def _now() -> float:
    return time.time()


def result_verification_settings(tenant_id: str, user_id: str, *, enabled: bool | None = None,
                                 mode: str | None = None) -> dict:
    """Persist the account's optional result-verification budget preference."""
    if not tenant_id or not user_id:
        return {"enabled": True, "mode": "standard"}
    if mode not in {None, "light", "standard", "strict"}:
        raise ValueError("invalid_verification_mode")
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS result_verification_preferences (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, enabled INTEGER NOT NULL,
            PRIMARY KEY (tenant_id, user_id), CHECK(enabled IN (0, 1))
        )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(result_verification_preferences)")}
        if "mode" not in columns:
            conn.execute("ALTER TABLE result_verification_preferences ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'")
        row = conn.execute("SELECT enabled, mode FROM result_verification_preferences WHERE tenant_id=? AND user_id=?",
                           (tenant_id, user_id)).fetchone()
        settings = {"enabled": bool(row["enabled"]) if row else True,
                    "mode": row["mode"] if row else "standard"}
        if enabled is not None or mode is not None:
            settings.update(enabled=settings["enabled"] if enabled is None else enabled,
                            mode=mode or settings["mode"])
            conn.execute("""INSERT INTO result_verification_preferences (tenant_id, user_id, enabled, mode) VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET enabled=excluded.enabled, mode=excluded.mode""",
                (tenant_id, user_id, int(settings["enabled"]), settings["mode"]))
    return settings


def result_verification_preference(tenant_id: str, user_id: str, *, enabled: bool | None = None) -> bool:
    return result_verification_settings(tenant_id, user_id, enabled=enabled)["enabled"]


async def session_result_verification_settings(session: Any) -> dict:
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(result_verification_settings,
                                  str(getattr(session, "tenant_id", "") or ""),
                                  str(getattr(session, "user_id", "") or ""))


async def session_result_verification_enabled(session: Any) -> bool:
    return (await session_result_verification_settings(session))["enabled"]


def intelligent_ui_preference(tenant_id: str, user_id: str, *, save: bool = False,
                              style: str | None = None, enabled: bool | None = None,
                              update_style: bool = True) -> dict:
    """Account preference in the existing auth store; None inherits deployment default."""
    from ..presentation.canvas import canvas_style, enabled as deployment_enabled
    if not tenant_id or not user_id:
        raise ValueError("owner_context_required")
    if style not in {None, "clay", "manual"}:
        raise ValueError("invalid_canvas_style")
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS intelligent_ui_preferences (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, style TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            PRIMARY KEY (tenant_id, user_id), CHECK(style IN ('clay', 'manual'))
        )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(intelligent_ui_preferences)")}
        if "enabled" not in columns:
            conn.execute("ALTER TABLE intelligent_ui_preferences ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))")
        if save:
            conn.execute("""INSERT INTO intelligent_ui_preferences (tenant_id, user_id, style, enabled)
                VALUES (?, ?, ?, COALESCE(?, 1))
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                    style=CASE WHEN ? THEN excluded.style ELSE intelligent_ui_preferences.style END,
                    enabled=COALESCE(?, intelligent_ui_preferences.enabled)""",
                (tenant_id, user_id, style, enabled, update_style, enabled))
        row = conn.execute("SELECT style, enabled FROM intelligent_ui_preferences WHERE tenant_id=? AND user_id=?",
                           (tenant_id, user_id)).fetchone()
    override = row["style"] if row else None
    default = canvas_style()
    personal_enabled = bool(row["enabled"]) if row else True
    return {"style": override, "default_style": default, "effective_style": override or default,
            "enabled": personal_enabled, "effective_enabled": personal_enabled and deployment_enabled()}


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield one auth connection and always close it before releasing the lock.

    ``sqlite3.Connection`` is a transaction context manager, but its
    ``__exit__`` does not close the connection.  The former ``with _connect()``
    call sites therefore left every request connection to the garbage
    collector, where it could be closed later from an unrelated worker thread.
    """

    with _AUTH_LOCK:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(path.as_posix(), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        260000,
    )
    return f"pbkdf2_sha256$260000${salt}${derived.hex()}"


def _verify_password(password: str, password_hash: str) -> bool:
    parts = str(password_hash or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    _scheme, rounds_text, salt, expected = parts
    try:
        rounds = int(rounds_text)
    except Exception:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        rounds,
    ).hex()
    return hmac.compare_digest(derived, expected)


def ensure_auth_store() -> None:
    with _AUTH_LOCK:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_login_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_memberships (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                )
                """
            )
            conn.commit()
            _ensure_bootstrap_user(conn)


def _ensure_bootstrap_user(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
    if int(row["count"] or 0) > 0:
        return
    tenant_id = workdir_utils.default_tenant_id()
    email = str(os.environ.get("TCODE_BOOTSTRAP_EMAIL") or "admin@example.com").strip().lower()
    password = str(os.environ.get("TCODE_BOOTSTRAP_PASSWORD") or "")
    if len(password) < 12:
        raise RuntimeError(
            "TCODE_BOOTSTRAP_PASSWORD must be explicitly set to at least 12 characters"
        )
    if password.strip().lower() in _UNSAFE_BOOTSTRAP_PASSWORDS:
        raise RuntimeError(
            "TCODE_BOOTSTRAP_PASSWORD must not use the bundled default or a common placeholder"
        )
    user_id = workdir_utils.sanitize_owner_id(
        os.environ.get("TCODE_BOOTSTRAP_USER_ID") or email.split("@", 1)[0] or workdir_utils.default_user_id(),
        workdir_utils.default_user_id(),
    )
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, slug, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
        (tenant_id, "Admin Tenant", tenant_id, now, now),
    )
    conn.execute(
        """
        INSERT INTO users (id, email, name, password_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (user_id, email, "Local Admin", _hash_password(password), now, now),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO tenant_memberships (tenant_id, user_id, role, status, created_at, updated_at)
        VALUES (?, ?, 'owner', 'active', ?, ?)
        """,
        (tenant_id, user_id, now, now),
    )
    conn.commit()


def create_user_account(
    *,
    tenant_id: str,
    email: str,
    password: str,
    name: str = "",
    role: str = "member",
    user_id: str = "",
) -> CurrentUserContext:
    ensure_auth_store()
    normalized_email = str(email or "").strip().lower()
    if "@" not in normalized_email or len(normalized_email) > 320:
        raise HTTPException(400, "Valid email is required")
    if len(str(password or "")) < 12:
        raise HTTPException(400, "Password must contain at least 12 characters")
    normalized_role = str(role or "member").strip().lower()
    if normalized_role not in {"member", "admin"}:
        raise HTTPException(400, "Role must be member or admin")
    normalized_tenant = workdir_utils.sanitize_owner_id(
        tenant_id,
        workdir_utils.default_tenant_id(),
    )
    normalized_user = workdir_utils.sanitize_owner_id(
        user_id or normalized_email.split("@", 1)[0],
        workdir_utils.default_user_id(),
    )
    now = _now()
    with _AUTH_LOCK:
        try:
            with _connect() as conn:
                tenant = conn.execute(
                    "SELECT id FROM tenants WHERE id = ? AND status = 'active'",
                    (normalized_tenant,),
                ).fetchone()
                if tenant is None:
                    raise HTTPException(404, "Tenant not found")
                conn.execute(
                    """
                    INSERT INTO users
                    (id, email, name, password_hash, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        normalized_user,
                        normalized_email,
                        str(name or "").strip()[:200],
                        _hash_password(password),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO tenant_memberships
                    (tenant_id, user_id, role, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        normalized_tenant,
                        normalized_user,
                        normalized_role,
                        now,
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "User id or email already exists") from exc
    return CurrentUserContext(
        user_id=normalized_user,
        tenant_id=normalized_tenant,
        email=normalized_email,
        name=str(name or "").strip()[:200],
        role=normalized_role,
    )


def authenticate(email: str, password: str) -> CurrentUserContext:
    ensure_auth_store()
    normalized_email = str(email or "").strip().lower()
    with _AUTH_LOCK:
        with _connect() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE email = ? AND status = 'active'",
                (normalized_email,),
            ).fetchone()
            if user is None or not _verify_password(password, str(user["password_hash"] or "")):
                raise HTTPException(401, "Invalid email or password")
            membership = conn.execute(
                """
                SELECT m.*, t.status AS tenant_status
                FROM tenant_memberships m
                JOIN tenants t ON t.id = m.tenant_id
                WHERE m.user_id = ? AND m.status = 'active'
                ORDER BY CASE t.status WHEN 'active' THEN 0 ELSE 1 END,
                         CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                         t.created_at ASC
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
            if membership is None:
                raise HTTPException(403, "User has no active tenant membership")
            conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (_now(), _now(), user["id"]),
            )
            conn.commit()
            return CurrentUserContext(
                user_id=str(user["id"]),
                tenant_id=str(membership["tenant_id"]),
                email=str(user["email"]),
                name=str(user["name"] or ""),
                role=str(membership["role"] or "member"),
                tenant_status=str(membership["tenant_status"] or "active"),
            )


def create_login_session(user: CurrentUserContext) -> str:
    ensure_auth_store()
    session_id = secrets.token_urlsafe(32)
    now = _now()
    with _AUTH_LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (id, user_id, tenant_id, expires_at, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user.user_id, user.tenant_id, now + _SESSION_TTL_SECONDS, now, now),
            )
            conn.commit()
    return session_id


def clear_login_session(session_id: str) -> None:
    if not session_id:
        return
    ensure_auth_store()
    with _AUTH_LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
            conn.commit()


def user_from_session_id(session_id: str) -> Optional[CurrentUserContext]:
    if not session_id:
        return None
    ensure_auth_store()
    now = _now()
    with _AUTH_LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT s.id AS session_id, s.expires_at, u.id AS user_id, u.email, u.name,
                       m.tenant_id, m.role, t.status AS tenant_status
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                JOIN tenant_memberships m ON m.user_id = u.id AND m.tenant_id = s.tenant_id
                JOIN tenants t ON t.id = m.tenant_id
                WHERE s.id = ? AND u.status = 'active' AND m.status = 'active'
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"] or 0) <= now:
                conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
                conn.commit()
                return None
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
            return CurrentUserContext(
                user_id=str(row["user_id"]),
                tenant_id=str(row["tenant_id"]),
                email=str(row["email"]),
                name=str(row["name"] or ""),
                role=str(row["role"] or "member"),
                tenant_status=str(row["tenant_status"] or "active"),
            )


def current_user_from_request(request: Request) -> Optional[CurrentUserContext]:
    # 本地关闭鉴权时必须完全忽略浏览器残留的登录 Cookie。Cookie 不区分端口，
    # 开发机访问另一套 TCode 服务后可能携带旧会话；若继续解析，会意外初始化
    # auth.sqlite，并在未配置 TCODE_BOOTSTRAP_PASSWORD 时把普通接口打成 500。
    if not auth_required():
        return None
    existing = getattr(request.state, "current_user", None)
    if isinstance(existing, CurrentUserContext):
        return existing
    user = user_from_session_id(str(request.cookies.get(AUTH_COOKIE_NAME) or ""))
    if user is not None:
        request.state.current_user = user
    return user


def current_user_from_websocket(websocket: WebSocket) -> Optional[CurrentUserContext]:
    # 与 HTTP 保持一致：关闭鉴权时不让陈旧 Cookie 影响本地 WebSocket 会话。
    if not auth_required():
        return None
    return user_from_session_id(str(websocket.cookies.get(AUTH_COOKIE_NAME) or ""))


def require_current_user(request: Request) -> CurrentUserContext:
    if not auth_required():
        return CurrentUserContext(
            user_id=workdir_utils.default_user_id(),
            tenant_id=workdir_utils.default_tenant_id(),
            email="local-user@example.local",
            name="Local User",
            role="owner",
            tenant_status="active",
        )
    user = current_user_from_request(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


def set_auth_cookie(request: Request, response: Response, session_id: str) -> None:
    secure = str(os.environ.get("TCODE_AUTH_COOKIE_SECURE", "") or "").strip().lower()
    secure_cookie = secure in {"1", "true", "yes", "on"} or str(request.url.scheme).lower() == "https"
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        max_age=_SESSION_TTL_SECONDS,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")


def serialize_user(user: CurrentUserContext) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "tenant_status": user.tenant_status,
    }


def list_user_tenants(user_id: str) -> list[dict[str, Any]]:
    ensure_auth_store()
    normalized_user = workdir_utils.sanitize_owner_id(
        user_id,
        workdir_utils.default_user_id(),
    )
    with _AUTH_LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id AS tenant_id, t.name, t.slug, t.status, t.created_at,
                   t.updated_at, m.role, m.status AS membership_status,
                   (
                       SELECT COUNT(*) FROM tenant_memberships members
                       WHERE members.tenant_id = t.id AND members.status = 'active'
                   ) AS member_count
            FROM tenant_memberships m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = ? AND m.status = 'active'
            ORDER BY CASE t.status WHEN 'active' THEN 0 ELSE 1 END,
                     lower(t.name), t.id
            """,
            (normalized_user,),
        ).fetchall()
    return [dict(row) for row in rows]


def _tenant_membership(
    conn: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.tenant_id, m.user_id, m.role, m.status AS membership_status,
               t.name, t.slug, t.status AS tenant_status
        FROM tenant_memberships m
        JOIN tenants t ON t.id = m.tenant_id
        WHERE m.tenant_id = ? AND m.user_id = ?
        """,
        (tenant_id, user_id),
    ).fetchone()


def _require_target_tenant_role(
    conn: sqlite3.Connection,
    actor_user_id: str,
    tenant_id: str,
    allowed_roles: set[str],
) -> sqlite3.Row:
    membership = _tenant_membership(conn, tenant_id, actor_user_id)
    if membership is None or str(membership["membership_status"] or "") != "active":
        raise HTTPException(403, "Active tenant membership is required")
    if str(membership["role"] or "member") not in allowed_roles:
        raise HTTPException(403, "Insufficient tenant role")
    return membership


def create_tenant(
    *,
    actor_user_id: str,
    tenant_id: str,
    name: str,
) -> dict[str, Any]:
    ensure_auth_store()
    raw_tenant_id = str(tenant_id or "").strip()
    normalized_tenant = workdir_utils.sanitize_owner_id(raw_tenant_id, "")
    if not raw_tenant_id or not normalized_tenant:
        raise HTTPException(400, "tenant_id is required")
    if normalized_tenant != raw_tenant_id:
        raise HTTPException(400, "tenant_id contains unsupported characters")
    normalized_name = str(name or "").strip()[:200]
    if not normalized_name:
        raise HTTPException(400, "Tenant name is required")
    normalized_user = workdir_utils.sanitize_owner_id(
        actor_user_id,
        workdir_utils.default_user_id(),
    )
    now = _now()
    with _AUTH_LOCK:
        try:
            with _connect() as conn:
                user = conn.execute(
                    "SELECT id FROM users WHERE id = ? AND status = 'active'",
                    (normalized_user,),
                ).fetchone()
                if user is None:
                    raise HTTPException(404, "User not found")
                conn.execute(
                    """
                    INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (normalized_tenant, normalized_name, normalized_tenant, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO tenant_memberships
                    (tenant_id, user_id, role, status, created_at, updated_at)
                    VALUES (?, ?, 'owner', 'active', ?, ?)
                    """,
                    (normalized_tenant, normalized_user, now, now),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Tenant id already exists") from exc
    return next(
        item for item in list_user_tenants(normalized_user)
        if item["tenant_id"] == normalized_tenant
    )


def update_tenant_name(
    *,
    actor_user_id: str,
    tenant_id: str,
    name: str,
) -> dict[str, Any]:
    normalized_name = str(name or "").strip()[:200]
    if not normalized_name:
        raise HTTPException(400, "Tenant name is required")
    now = _now()
    with _AUTH_LOCK, _connect() as conn:
        _require_target_tenant_role(conn, actor_user_id, tenant_id, {"owner", "admin"})
        conn.execute(
            "UPDATE tenants SET name = ?, updated_at = ? WHERE id = ?",
            (normalized_name, now, tenant_id),
        )
        conn.commit()
    return next(
        item for item in list_user_tenants(actor_user_id)
        if item["tenant_id"] == tenant_id
    )


def set_tenant_status(
    *,
    actor_user_id: str,
    tenant_id: str,
    status: str,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"active", "inactive"}:
        raise HTTPException(400, "Tenant status must be active or inactive")
    with _AUTH_LOCK, _connect() as conn:
        membership = _require_target_tenant_role(
            conn,
            actor_user_id,
            tenant_id,
            {"owner", "admin"},
        )
        if str(membership["tenant_status"] or "") != normalized_status:
            conn.execute(
                "UPDATE tenants SET status = ?, updated_at = ? WHERE id = ?",
                (normalized_status, _now(), tenant_id),
            )
            conn.commit()
    return next(
        item for item in list_user_tenants(actor_user_id)
        if item["tenant_id"] == tenant_id
    )


def switch_login_session_tenant(
    *,
    session_id: str,
    user_id: str,
    tenant_id: str,
) -> CurrentUserContext:
    ensure_auth_store()
    with _AUTH_LOCK, _connect() as conn:
        membership = _tenant_membership(conn, tenant_id, user_id)
        if membership is None or str(membership["membership_status"] or "") != "active":
            raise HTTPException(403, "Active tenant membership is required")
        if str(membership["tenant_status"] or "") != "active":
            raise HTTPException(409, "Tenant is inactive; restore it before switching")
        updated = conn.execute(
            """
            UPDATE auth_sessions SET tenant_id = ?, last_seen_at = ?
            WHERE id = ? AND user_id = ? AND expires_at > ?
            """,
            (tenant_id, _now(), session_id, user_id, _now()),
        )
        if int(updated.rowcount or 0) != 1:
            raise HTTPException(401, "Authentication session is no longer valid")
        user = conn.execute(
            "SELECT id, email, name FROM users WHERE id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        conn.commit()
    if user is None:
        raise HTTPException(401, "User is no longer active")
    return CurrentUserContext(
        user_id=str(user["id"]),
        tenant_id=str(membership["tenant_id"]),
        email=str(user["email"]),
        name=str(user["name"] or ""),
        role=str(membership["role"] or "member"),
        tenant_status=str(membership["tenant_status"] or "active"),
    )


def list_tenant_members(
    *,
    actor_user_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    ensure_auth_store()
    with _AUTH_LOCK, _connect() as conn:
        _require_target_tenant_role(conn, actor_user_id, tenant_id, {"owner", "admin"})
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.name, u.status AS user_status,
                   m.role, m.status AS membership_status, m.created_at, m.updated_at
            FROM tenant_memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = ?
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                     lower(u.name), lower(u.email)
            """,
            (tenant_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_shareable_tenant_members(
    *,
    actor_user_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """List active members visible to any active member for Wiki sharing.

    This intentionally exposes only the minimal identity fields needed by the
    share picker. Tenant administration details remain owner/admin-only.
    """

    ensure_auth_store()
    with _AUTH_LOCK, _connect() as conn:
        actor = _tenant_membership(conn, tenant_id, actor_user_id)
        if (
            actor is None
            or str(actor["membership_status"] or "") != "active"
            or str(actor["tenant_status"] or "") != "active"
        ):
            raise HTTPException(403, "Active tenant membership is required")
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.name, m.role
            FROM tenant_memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id=? AND m.status='active' AND u.status='active'
            ORDER BY lower(CASE WHEN u.name='' THEN u.email ELSE u.name END), lower(u.email)
            """,
            (tenant_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_active_tenant_user(*, tenant_id: str, user_id: str) -> CurrentUserContext:
    """Resolve a service-account impersonation target without creating a session."""

    ensure_auth_store()
    with _AUTH_LOCK, _connect() as conn:
        membership = _tenant_membership(conn, tenant_id, user_id)
        if (
            membership is None
            or str(membership["membership_status"] or "") != "active"
            or str(membership["tenant_status"] or "") != "active"
        ):
            raise HTTPException(403, "Active tenant membership is required")
        user = conn.execute(
            "SELECT id,email,name,status FROM users WHERE id=? AND status='active'",
            (user_id,),
        ).fetchone()
    if user is None:
        raise HTTPException(403, "Active user is required")
    return CurrentUserContext(
        user_id=str(user["id"]),
        tenant_id=str(membership["tenant_id"]),
        email=str(user["email"]),
        name=str(user["name"] or ""),
        role=str(membership["role"] or "member"),
        tenant_status=str(membership["tenant_status"] or "active"),
    )


def tenant_user_summaries(*, tenant_id: str, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return minimal same-tenant user labels for authorized share responses."""

    identifiers = sorted({str(item or "").strip() for item in user_ids if str(item or "").strip()})
    if not identifiers:
        return {}
    with _AUTH_LOCK, _connect() as conn:
        placeholders = ",".join("?" for _ in identifiers)
        rows = conn.execute(
            f"""
            SELECT u.id AS user_id,u.email,u.name,m.role,m.status AS membership_status
            FROM tenant_memberships m JOIN users u ON u.id=m.user_id
            WHERE m.tenant_id=? AND m.user_id IN ({placeholders})
            """,
            [tenant_id, *identifiers],
        ).fetchall()
    return {str(row["user_id"]): dict(row) for row in rows}


def add_tenant_member(
    *,
    actor_user_id: str,
    tenant_id: str,
    email: str,
    role: str = "member",
    password: str = "",
    name: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    ensure_auth_store()
    normalized_email = str(email or "").strip().lower()
    if "@" not in normalized_email or len(normalized_email) > 320:
        raise HTTPException(400, "Valid email is required")
    normalized_role = str(role or "member").strip().lower()
    if normalized_role not in {"member", "admin", "owner"}:
        raise HTTPException(400, "Role must be member, admin or owner")
    now = _now()
    with _AUTH_LOCK:
        with _connect() as conn:
            actor_membership = _require_target_tenant_role(
                conn,
                actor_user_id,
                tenant_id,
                {"owner", "admin"},
            )
            if str(actor_membership["tenant_status"] or "") != "active":
                raise HTTPException(409, "Restore the tenant before managing members")
            actor_role = str(actor_membership["role"] or "member")
            if normalized_role == "owner" and actor_role != "owner":
                raise HTTPException(403, "Only an owner can add another owner")
            user = conn.execute(
                "SELECT id, email, name FROM users WHERE email = ?",
                (normalized_email,),
            ).fetchone()
            if user is None:
                if len(str(password or "")) < 12:
                    raise HTTPException(400, "A new user password must contain at least 12 characters")
                normalized_user = workdir_utils.sanitize_owner_id(
                    user_id or normalized_email.split("@", 1)[0],
                    workdir_utils.default_user_id(),
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO users
                        (id, email, name, password_hash, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            normalized_user,
                            normalized_email,
                            str(name or "").strip()[:200],
                            _hash_password(password),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise HTTPException(409, "User id or email already exists") from exc
                user_id_value = normalized_user
            else:
                user_id_value = str(user["id"])
            try:
                conn.execute(
                    """
                    INSERT INTO tenant_memberships
                    (tenant_id, user_id, role, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                        role = excluded.role,
                        status = 'active',
                        updated_at = excluded.updated_at
                    """,
                    (tenant_id, user_id_value, normalized_role, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Unable to add tenant member") from exc
    return next(
        item for item in list_tenant_members(actor_user_id=actor_user_id, tenant_id=tenant_id)
        if item["user_id"] == user_id_value
    )


def update_tenant_member_role(
    *,
    actor_user_id: str,
    tenant_id: str,
    user_id: str,
    role: str,
) -> dict[str, Any]:
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in {"member", "admin", "owner"}:
        raise HTTPException(400, "Role must be member, admin or owner")
    with _AUTH_LOCK, _connect() as conn:
        actor_membership = _require_target_tenant_role(
            conn,
            actor_user_id,
            tenant_id,
            {"owner", "admin"},
        )
        target = _tenant_membership(conn, tenant_id, user_id)
        if target is None or str(target["membership_status"] or "") != "active":
            raise HTTPException(404, "Tenant member not found")
        actor_role = str(actor_membership["role"] or "member")
        target_role = str(target["role"] or "member")
        if actor_role != "owner" and (target_role == "owner" or normalized_role == "owner"):
            raise HTTPException(403, "Only an owner can manage owner roles")
        if target_role == "owner" and normalized_role != "owner":
            owners = conn.execute(
                """
                SELECT COUNT(*) AS count FROM tenant_memberships
                WHERE tenant_id = ? AND role = 'owner' AND status = 'active'
                """,
                (tenant_id,),
            ).fetchone()
            if int(owners["count"] or 0) <= 1:
                raise HTTPException(409, "A tenant must keep at least one active owner")
        conn.execute(
            "UPDATE tenant_memberships SET role = ?, updated_at = ? WHERE tenant_id = ? AND user_id = ?",
            (normalized_role, _now(), tenant_id, user_id),
        )
        conn.commit()
    return next(
        item for item in list_tenant_members(actor_user_id=actor_user_id, tenant_id=tenant_id)
        if item["user_id"] == user_id
    )


def remove_tenant_member(
    *,
    actor_user_id: str,
    tenant_id: str,
    user_id: str,
) -> None:
    if actor_user_id == user_id:
        raise HTTPException(409, "Use another owner to remove your own membership")
    with _AUTH_LOCK, _connect() as conn:
        actor_membership = _require_target_tenant_role(
            conn,
            actor_user_id,
            tenant_id,
            {"owner", "admin"},
        )
        target = _tenant_membership(conn, tenant_id, user_id)
        if target is None:
            raise HTTPException(404, "Tenant member not found")
        if str(target["role"] or "member") == "owner" and str(actor_membership["role"] or "") != "owner":
            raise HTTPException(403, "Only an owner can remove another owner")
        conn.execute(
            "DELETE FROM auth_sessions WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )
        conn.execute(
            "DELETE FROM tenant_memberships WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )
        conn.commit()


def validate_tenant_deletion(
    *,
    actor_user_id: str,
    current_tenant_id: str,
    tenant_id: str,
) -> None:
    with _AUTH_LOCK, _connect() as conn:
        membership = _require_target_tenant_role(conn, actor_user_id, tenant_id, {"owner"})
        if str(membership["tenant_status"] or "") != "inactive":
            raise HTTPException(409, "Disable the tenant before deleting it")
        if tenant_id == current_tenant_id:
            raise HTTPException(409, "Switch to another active tenant before deleting this tenant")


def delete_tenant_record(*, actor_user_id: str, tenant_id: str) -> None:
    with _AUTH_LOCK, _connect() as conn:
        _require_target_tenant_role(conn, actor_user_id, tenant_id, {"owner"})
        conn.execute("DELETE FROM auth_sessions WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenant_memberships WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        conn.commit()
