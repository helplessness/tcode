from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from ..sqlite_runtime import connect as sqlite_connect

from fastapi import HTTPException

from ..workspace_lifecycle import managed_workspace_owner_from_path


DEFAULT_USER_WORKDIRS = ("workspace-1", "workspace-2", "workspace-3")
DEFAULT_TENANT_ID = "admin_tenant"
DEFAULT_USER_ID = "local-user"
DEFAULT_WORKSPACE_ID = "workspace-1"
DEFAULT_MAX_WORKSPACES_PER_USER = 5
_WORKSPACE_STATE_LOCK = threading.RLock()
_WORKSPACE_MUTATION_LOCK = threading.RLock()
_HIDDEN_WORKSPACE_STATUSES = {
    "deleting",
    "deleted",
    "purging",
    "purge_failed",
    "purged",
}
_WORKSPACE_STATUSES = {"active", *_HIDDEN_WORKSPACE_STATUSES}


@dataclass(frozen=True)
class WorkspaceContext:
    tenant_id: str
    user_id: str
    workspace_id: str


def workspace_state_db_path(server_workdir: str) -> Path:
    configured = str(os.environ.get("TCODE_WORKSPACE_STATE_DB", "") or "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else Path(server_workdir) / ".tcode" / "workspaces.sqlite3"
    )
    if not path.is_absolute():
        path = Path(server_workdir) / path
    return path.resolve(strict=False)


def _workspace_state_connection(server_workdir: str) -> sqlite3.Connection:
    path = workspace_state_db_path(server_workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite_connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_states (
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            status TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            purge_stage TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            delete_started_at REAL NOT NULL DEFAULT 0,
            deleted_at REAL NOT NULL DEFAULT 0,
            purge_started_at REAL NOT NULL DEFAULT 0,
            purged_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, user_id, workspace_id)
        )
        """
    )
    columns = {
        str(row["name"] or "")
        for row in connection.execute("PRAGMA table_info(workspace_states)").fetchall()
    }
    if "purge_started_at" not in columns:
        connection.execute(
            "ALTER TABLE workspace_states ADD COLUMN purge_started_at REAL NOT NULL DEFAULT 0"
        )
    if "purged_at" not in columns:
        connection.execute(
            "ALTER TABLE workspace_states ADD COLUMN purged_at REAL NOT NULL DEFAULT 0"
        )
    if "generation" not in columns:
        connection.execute(
            "ALTER TABLE workspace_states ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
        )
    if "purge_stage" not in columns:
        connection.execute(
            "ALTER TABLE workspace_states ADD COLUMN purge_stage TEXT NOT NULL DEFAULT ''"
        )
    connection.commit()
    return connection


def workspace_state(
    server_workdir: str,
    context: WorkspaceContext,
) -> dict[str, Any] | None:
    with _WORKSPACE_STATE_LOCK, closing(
        _workspace_state_connection(server_workdir)
    ) as connection:
        row = connection.execute(
            """
            SELECT tenant_id, user_id, workspace_id, status, generation,
                   purge_stage, last_error,
                   delete_started_at, deleted_at, purge_started_at,
                   purged_at, updated_at
            FROM workspace_states
            WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
            """,
            (context.tenant_id, context.user_id, context.workspace_id),
        ).fetchone()
    return dict(row) if row is not None else None


def set_workspace_state(
    server_workdir: str,
    context: WorkspaceContext,
    status: str,
    *,
    last_error: str = "",
    purge_stage: str | None = None,
) -> dict[str, Any]:
    normalized = str(status or "").strip().lower()
    if normalized not in _WORKSPACE_STATUSES:
        raise ValueError("invalid workspace status")
    now = time.time()
    existing = workspace_state(server_workdir, context) or {}
    previous_status = str(existing.get("status") or "").strip().lower()
    generation = max(1, int(existing.get("generation") or 1))
    if normalized == "purging" and previous_status != "purging":
        generation += 1
    next_purge_stage = (
        str(purge_stage or "").strip()
        if purge_stage is not None
        else ""
        if normalized == "active"
        else str(existing.get("purge_stage") or "").strip()
    )
    delete_started_at = (
        now if normalized == "deleting" else float(existing.get("delete_started_at") or 0)
    )
    deleted_at = (
        0.0
        if normalized == "active"
        else now
        if normalized == "deleted"
        else float(existing.get("deleted_at") or 0)
    )
    purge_started_at = (
        0.0
        if normalized == "active"
        else now
        if normalized == "purging"
        and str(existing.get("status") or "") != "purging"
        else float(existing.get("purge_started_at") or 0)
    )
    purged_at = (
        0.0
        if normalized == "active"
        else now
        if normalized == "purged"
        else float(existing.get("purged_at") or 0)
    )
    with _WORKSPACE_STATE_LOCK, closing(
        _workspace_state_connection(server_workdir)
    ) as connection:
        connection.execute(
            """
            INSERT INTO workspace_states (
                tenant_id, user_id, workspace_id, status, generation,
                purge_stage, last_error,
                delete_started_at, deleted_at, purge_started_at,
                purged_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, user_id, workspace_id) DO UPDATE SET
                status = excluded.status,
                generation = excluded.generation,
                purge_stage = excluded.purge_stage,
                last_error = excluded.last_error,
                delete_started_at = excluded.delete_started_at,
                deleted_at = excluded.deleted_at,
                purge_started_at = excluded.purge_started_at,
                purged_at = excluded.purged_at,
                updated_at = excluded.updated_at
            """,
            (
                context.tenant_id,
                context.user_id,
                context.workspace_id,
                normalized,
                generation,
                next_purge_stage,
                str(last_error or "")[:4000],
                delete_started_at,
                deleted_at,
                purge_started_at,
                purged_at,
                now,
            ),
        )
        connection.commit()
    return workspace_state(server_workdir, context) or {}


def list_workspace_states(
    server_workdir: str,
    *,
    tenant_id: str,
    user_id: str,
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    normalized_statuses = {
        str(status or "").strip().lower()
        for status in (statuses or set())
        if str(status or "").strip().lower() in _WORKSPACE_STATUSES
    }
    clauses = ["tenant_id = ?", "user_id = ?"]
    params: list[Any] = [safe_tenant_id, safe_user_id]
    if normalized_statuses:
        placeholders = ",".join("?" for _ in normalized_statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(normalized_statuses))
    with _WORKSPACE_STATE_LOCK, closing(
        _workspace_state_connection(server_workdir)
    ) as connection:
        rows = connection.execute(
            f"""
            SELECT tenant_id, user_id, workspace_id, status, generation,
                   purge_stage, last_error,
                   delete_started_at, deleted_at, purge_started_at,
                   purged_at, updated_at
            FROM workspace_states
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_workspace_states(
    server_workdir: str,
    *,
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """List persisted workspace lifecycle rows across every owner.

    This is intentionally an internal recovery primitive. User-facing callers
    must keep using :func:`list_workspace_states` so tenant isolation remains
    enforced at the API boundary.
    """

    normalized_statuses = {
        str(status or "").strip().lower()
        for status in (statuses or set())
        if str(status or "").strip().lower() in _WORKSPACE_STATUSES
    }
    params: list[Any] = []
    where = ""
    if normalized_statuses:
        placeholders = ",".join("?" for _ in normalized_statuses)
        where = f"WHERE status IN ({placeholders})"
        params.extend(sorted(normalized_statuses))
    with _WORKSPACE_STATE_LOCK, closing(
        _workspace_state_connection(server_workdir)
    ) as connection:
        rows = connection.execute(
            f"""
            SELECT tenant_id, user_id, workspace_id, status, generation,
                   purge_stage, last_error, delete_started_at, deleted_at,
                   purge_started_at, purged_at, updated_at
            FROM workspace_states
            {where}
            ORDER BY updated_at ASC
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def workspace_generation(server_workdir: str, context: WorkspaceContext) -> int:
    state = workspace_state(server_workdir, context) or {}
    return max(1, int(state.get("generation") or 1))


def workspace_is_hidden(server_workdir: str, context: WorkspaceContext) -> bool:
    state = workspace_state(server_workdir, context)
    return str((state or {}).get("status") or "active") in _HIDDEN_WORKSPACE_STATUSES


def assert_workspace_active(server_workdir: str, context: WorkspaceContext) -> None:
    state = workspace_state(server_workdir, context)
    status = str((state or {}).get("status") or "active")
    if status in _HIDDEN_WORKSPACE_STATUSES:
        raise HTTPException(410, f"workspace is {status}")


def sanitize_owner_id(raw_value: Any, default: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._@-]+", "-", str(raw_value or "").strip())
    text = text.strip(".-_@")
    if not text:
        text = default
    return text[:128]


def default_tenant_id() -> str:
    return sanitize_owner_id(
        os.environ.get("TCODE_DEFAULT_TENANT_ID"),
        DEFAULT_TENANT_ID,
    )


def default_user_id() -> str:
    return sanitize_owner_id(
        os.environ.get("TCODE_DEFAULT_USER_ID"),
        DEFAULT_USER_ID,
    )


def default_workspace_id() -> str:
    return sanitize_workdir_name(
        os.environ.get("TCODE_DEFAULT_WORKSPACE_ID") or DEFAULT_WORKSPACE_ID
    ) or DEFAULT_WORKSPACE_ID


def max_workspaces_per_user() -> int:
    """Return the configured per-user workspace limit.

    Invalid values fall back to the safe product default instead of disabling
    the limit accidentally.
    """

    raw_value = str(
        os.environ.get(
            "TCODE_MAX_WORKSPACES_PER_USER",
            DEFAULT_MAX_WORKSPACES_PER_USER,
        )
        or ""
    ).strip()
    try:
        configured = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKSPACES_PER_USER
    if configured < 1:
        return DEFAULT_MAX_WORKSPACES_PER_USER
    return configured


def _lookup(mapping: Any, *names: str) -> str:
    if mapping is None:
        return ""
    for name in names:
        try:
            value = mapping.get(name)
        except Exception:
            value = None
        if value not in (None, ""):
            return str(value)
    return ""


def resolve_workspace_context(
    *,
    headers: Any = None,
    query_params: Any = None,
    tenant_id: str = "",
    user_id: str = "",
    workspace_id: str = "",
    workdir_name: str = "",
) -> WorkspaceContext:
    tenant = (
        tenant_id
        or _lookup(headers, "x-tcode-tenant-id", "x-tenant-id")
        or _lookup(query_params, "tenant_id")
        or default_tenant_id()
    )
    user = (
        user_id
        or _lookup(headers, "x-tcode-user-id", "x-user-id")
        or _lookup(query_params, "user_id")
        or default_user_id()
    )
    workspace = (
        workspace_id
        or _lookup(headers, "x-tcode-workspace-id", "x-workspace-id")
        or _lookup(query_params, "workspace_id")
        or workdir_name
        or _lookup(query_params, "workdir_name")
        or default_workspace_id()
    )
    return WorkspaceContext(
        tenant_id=sanitize_owner_id(tenant, default_tenant_id()),
        user_id=sanitize_owner_id(user, default_user_id()),
        workspace_id=sanitize_workdir_name(workspace) or default_workspace_id(),
    )


def workspace_storage_root(server_workdir: str) -> Path:
    """Return the directory that directly contains tenant directories."""

    configured = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            root = Path(server_workdir) / root
    else:
        root = Path(server_workdir) / "workdirs"
    return root.resolve()


def workspace_context_from_managed_path(
    server_workdir: str,
    path_text: Any,
) -> WorkspaceContext | None:
    """Return the actual workspace encoded by a managed filesystem path."""

    owner = managed_workspace_owner_from_path(
        path_text,
        server_workdir=server_workdir,
    )
    if owner is None:
        return None
    tenant_id, user_id, workspace_id = owner
    safe_workspace_id = sanitize_workdir_name(workspace_id)
    if not safe_workspace_id:
        return None
    return WorkspaceContext(
        tenant_id=sanitize_owner_id(tenant_id, default_tenant_id()),
        user_id=sanitize_owner_id(user_id, default_user_id()),
        workspace_id=safe_workspace_id,
    )


def workspace_purge_path(server_workdir: str, context: WorkspaceContext) -> Path:
    """Return the deterministic quarantine path used by permanent deletion."""

    root = workspace_storage_root(server_workdir) / ".tcode-purge"
    if root.is_symlink():
        raise ValueError("workspace purge root must not be a symlink")
    safe_tenant = sanitize_owner_id(context.tenant_id, default_tenant_id())
    safe_user = sanitize_owner_id(context.user_id, default_user_id())
    safe_workspace = sanitize_workdir_name(context.workspace_id)
    if not safe_workspace:
        raise ValueError("invalid workspace id")
    quarantine = (root / safe_tenant / safe_user / safe_workspace).resolve(strict=False)
    try:
        quarantine.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("workspace purge path escapes quarantine root") from exc
    return quarantine


def tenant_workdirs_root(server_workdir: str, *, tenant_id: str = "") -> Path:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    return (workspace_storage_root(server_workdir) / safe_tenant_id).resolve()


def user_workdirs_root(
    server_workdir: str,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> Path:
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    return (
        tenant_workdirs_root(server_workdir, tenant_id=tenant_id) / safe_user_id
    ).resolve()


def delete_tenant_workspace_states(server_workdir: str, tenant_id: str) -> None:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    with _WORKSPACE_STATE_LOCK, closing(
        _workspace_state_connection(server_workdir)
    ) as connection:
        connection.execute(
            "DELETE FROM workspace_states WHERE tenant_id = ?",
            (safe_tenant_id,),
        )
        connection.commit()


def workdir_template_root(server_workdir: str) -> Path:
    configured = str(os.environ.get("TCODE_WORKDIR_TEMPLATE_DIR", "") or "").strip()
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            root = Path(server_workdir) / root
    else:
        root = Path(server_workdir) / "config" / "workdir_template"
    return root.resolve()


def sanitize_workdir_name(raw_name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw_name or "").strip())
    text = text.strip(".-_")
    return text[:64]


def resolve_user_workdir_path(
    server_workdir: str,
    workdir_name: str,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> Path:
    root = user_workdirs_root(
        server_workdir,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    safe_name = sanitize_workdir_name(workdir_name)
    if not safe_name:
        raise HTTPException(400, "invalid workdir name")
    target = (root / safe_name).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise HTTPException(403, "workdir escapes allowed root")
    return target


def workdir_name_from_path(
    server_workdir: str,
    path_text: str,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> str:
    root = user_workdirs_root(
        server_workdir,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    path = Path(str(path_text or "").strip())
    try:
        rel = path.resolve().relative_to(root)
    except Exception:
        return ""
    parts = rel.parts
    return parts[0] if parts else ""


def copy_template_to_workdir(server_workdir: str, target: Path) -> str:
    template = workdir_template_root(server_workdir)
    warning = ""
    if template.is_dir():
        try:
            for entry in template.iterdir():
                dst = target / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, dst)
        except Exception as exc:
            warning = f"template copy failed: {exc}; created empty workspace"
    elif template.exists():
        warning = "template path is not a directory; created empty workspace"
    return warning


def ensure_default_user_workdirs(
    server_workdir: str,
    default_names: tuple[str, ...] = DEFAULT_USER_WORKDIRS,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> None:
    with _WORKSPACE_MUTATION_LOCK:
        root = user_workdirs_root(
            server_workdir,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        root.mkdir(parents=True, exist_ok=True)
        existing_dirs = [item for item in root.iterdir() if item.is_dir()]
        if existing_dirs:
            return
        default_limit = max_workspaces_per_user()
        for name in default_names[:default_limit]:
            context = WorkspaceContext(
                tenant_id=sanitize_owner_id(tenant_id, default_tenant_id()),
                user_id=sanitize_owner_id(user_id, default_user_id()),
                workspace_id=sanitize_workdir_name(name),
            )
            if workspace_is_hidden(server_workdir, context):
                continue
            target = resolve_user_workdir_path(
                server_workdir,
                name,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            target.mkdir(parents=True, exist_ok=True)
            copy_template_to_workdir(server_workdir, target)


def active_workspace_count(
    server_workdir: str,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> int:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    root = user_workdirs_root(
        server_workdir,
        tenant_id=safe_tenant_id,
        user_id=safe_user_id,
    )
    if not root.is_dir():
        return 0
    count = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        context = WorkspaceContext(
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
            workspace_id=entry.name,
        )
        if not workspace_is_hidden(server_workdir, context):
            count += 1
    return count


def assert_workspace_activation_allowed(
    server_workdir: str,
    context: WorkspaceContext,
) -> None:
    """Reject restoring a hidden workspace when the user is already at quota."""

    if not workspace_is_hidden(server_workdir, context):
        return
    limit = max_workspaces_per_user()
    current = active_workspace_count(
        server_workdir,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    if current >= limit:
        raise HTTPException(
            409,
            f"workspace limit reached: user already has {current} of {limit} active workspaces",
        )


def list_user_workdirs(
    server_workdir: str,
    default_names: tuple[str, ...] = DEFAULT_USER_WORKDIRS,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> list[dict[str, Any]]:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    root = user_workdirs_root(
        server_workdir,
        tenant_id=safe_tenant_id,
        user_id=safe_user_id,
    )
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        context = WorkspaceContext(
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
            workspace_id=entry.name,
        )
        if workspace_is_hidden(server_workdir, context):
            continue
        try:
            stat = entry.stat()
            rows.append(
                {
                    "name": entry.name,
                    "workspace_id": entry.name,
                    "tenant_id": safe_tenant_id,
                    "owner_user_id": safe_user_id,
                    "path": entry.as_posix(),
                    "created_at": float(stat.st_ctime),
                    "updated_at": float(stat.st_mtime),
                }
            )
        except Exception:
            rows.append(
                {
                    "name": entry.name,
                    "workspace_id": entry.name,
                    "tenant_id": safe_tenant_id,
                    "owner_user_id": safe_user_id,
                    "path": entry.as_posix(),
                    "created_at": 0.0,
                    "updated_at": 0.0,
                }
            )
    return rows


def create_user_workdir(
    server_workdir: str,
    name: str,
    default_names: tuple[str, ...] = DEFAULT_USER_WORKDIRS,
    *,
    tenant_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    with _WORKSPACE_MUTATION_LOCK:
        user_workdirs_root(
            server_workdir,
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
        ).mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_workdir_name(name)
        if not safe_name:
            raise HTTPException(400, "workdir name is required")
        target = resolve_user_workdir_path(
            server_workdir,
            safe_name,
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
        )
        context = WorkspaceContext(
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
            workspace_id=safe_name,
        )
        state = workspace_state(server_workdir, context) or {}
        status = str(state.get("status") or "active")
        recovery_target: Path | None = None
        if status in _HIDDEN_WORKSPACE_STATUSES and status != "purged":
            if target.exists():
                raise HTTPException(
                    409,
                    "workdir is soft-deleted; restore it before reuse",
                )
            raise HTTPException(
                409,
                f"workdir cleanup is incomplete ({status}); retry purge or restore it",
            )
        if target.exists() and status != "purged":
            raise HTTPException(409, "workdir already exists")

        limit = max_workspaces_per_user()
        current = active_workspace_count(
            server_workdir,
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
        )
        if current >= limit:
            raise HTTPException(
                409,
                f"workspace limit reached: user already has {current} of {limit} active workspaces",
            )

        if target.exists() and status == "purged":
            # A purged workspace id is explicitly reusable.  If a crashed
            # create attempt or a workspace-scoped service recreated the
            # directory after purge, never adopt those residual files as
            # the new workspace.  Park them outside the user-visible root,
            # build a clean workspace, and restore them only if creation
            # fails before activation.
            recovery_root = (
                workspace_storage_root(server_workdir)
                / ".tcode-recreate"
                / safe_tenant_id
                / safe_user_id
            ).resolve(strict=False)
            storage_root = workspace_storage_root(server_workdir).resolve(strict=False)
            try:
                recovery_root.relative_to(storage_root)
            except ValueError as exc:
                raise HTTPException(500, "invalid workspace recovery path") from exc
            recovery_root.mkdir(parents=True, exist_ok=True)
            recovery_target = (
                recovery_root / f"{safe_name}-{time.time_ns()}"
            ).resolve(strict=False)
            try:
                recovery_target.relative_to(recovery_root)
            except ValueError as exc:
                raise HTTPException(500, "invalid workspace recovery target") from exc
            os.replace(target, recovery_target)

        try:
            target.mkdir(parents=True, exist_ok=False)
            warning = copy_template_to_workdir(server_workdir, target)
            set_workspace_state(server_workdir, context, "active")
            stat = target.stat()
        except Exception:
            # Do not leave a directory with a hidden/purged state: the next
            # create would otherwise report the misleading soft-delete error.
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if recovery_target is not None and recovery_target.exists():
                os.replace(recovery_target, target)
            raise
        if recovery_target is not None and recovery_target.exists():
            try:
                if recovery_target.is_dir():
                    shutil.rmtree(recovery_target)
                else:
                    recovery_target.unlink()
            except Exception as exc:
                cleanup_warning = f"purged workspace residual cleanup failed: {exc}"
                warning = f"{warning}; {cleanup_warning}" if warning else cleanup_warning
    return {
        "name": target.name,
        "workspace_id": target.name,
        "tenant_id": safe_tenant_id,
        "owner_user_id": safe_user_id,
        "path": target.as_posix(),
        "created_at": float(stat.st_ctime),
        "updated_at": float(stat.st_mtime),
        "warning": warning,
    }


def resolve_workdir_from_name_or_path(
    server_workdir: str,
    *,
    workdir_name: str = "",
    workdir_path: str = "",
    default_names: tuple[str, ...] = DEFAULT_USER_WORKDIRS,
    tenant_id: str = "",
    user_id: str = "",
) -> str:
    safe_tenant_id = sanitize_owner_id(tenant_id, default_tenant_id())
    safe_user_id = sanitize_owner_id(user_id, default_user_id())
    name_text = str(workdir_name or "").strip()
    path_text = str(workdir_path or "").strip()

    if name_text:
        target = resolve_user_workdir_path(
            server_workdir,
            name_text,
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
        )
        if not target.is_dir():
            raise HTTPException(404, "workdir not found")
        assert_workspace_active(
            server_workdir,
            resolve_workspace_context(
                tenant_id=safe_tenant_id,
                user_id=safe_user_id,
                workspace_id=target.name,
            ),
        )
        return target.as_posix()

    if path_text:
        resolved = Path(path_text).expanduser().resolve()
        root = user_workdirs_root(
            server_workdir,
            tenant_id=safe_tenant_id,
            user_id=safe_user_id,
        )
        try:
            resolved.relative_to(root)
        except Exception:
            raise HTTPException(403, "workdir is outside user root")
        if not resolved.is_dir():
            raise HTTPException(404, "workdir not found")
        assert_workspace_active(
            server_workdir,
            resolve_workspace_context(
                tenant_id=safe_tenant_id,
                user_id=safe_user_id,
                workspace_id=workdir_name_from_path(
                    server_workdir,
                    resolved.as_posix(),
                    tenant_id=safe_tenant_id,
                    user_id=safe_user_id,
                ),
            ),
        )
        return resolved.as_posix()

    raise HTTPException(400, "workdir_name is required")


def same_resolved_path(left: str, right: str) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return (
            Path(left_text).expanduser().resolve()
            == Path(right_text).expanduser().resolve()
        )
    except Exception:
        return left_text == right_text


def resolve_requested_workdir_for_session(
    server_workdir: str,
    *,
    workdir_name: str = "",
    workdir_path: str = "",
    existing_workdir: str = "",
    default_names: tuple[str, ...] = DEFAULT_USER_WORKDIRS,
    tenant_id: str = "",
    user_id: str = "",
) -> str:
    name_text = str(workdir_name or "").strip()
    path_text = str(workdir_path or "").strip()
    if not name_text and not path_text:
        return ""

    existing_text = str(existing_workdir or "").strip()
    if existing_text and path_text and same_resolved_path(existing_text, path_text):
        try:
            existing_path = Path(existing_text).expanduser().resolve()
        except Exception:
            return existing_text
        if existing_path.is_dir():
            assert_workspace_active(
                server_workdir,
                resolve_workspace_context(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workdir_name_from_path(
                        server_workdir,
                        existing_path.as_posix(),
                        tenant_id=tenant_id,
                        user_id=user_id,
                    ),
                ),
            )
            return existing_path.as_posix()
        return existing_text

    return resolve_workdir_from_name_or_path(
        server_workdir,
        workdir_name=name_text,
        workdir_path=path_text,
        default_names=default_names,
        tenant_id=tenant_id,
        user_id=user_id,
    )
