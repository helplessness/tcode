"""Low-level guards for managed workspace filesystem lifecycle.

This module deliberately has no dependency on ``tcode_agent.server`` so it
can be used by persistence layers while the server package is still importing.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from .sqlite_runtime import connect as sqlite_connect


HIDDEN_WORKSPACE_STATUSES = frozenset(
    {"deleting", "deleted", "purging", "purge_failed", "purged"}
)


class ManagedWorkspaceUnavailableError(RuntimeError):
    """Raised when code tries to recreate storage for a hidden workspace."""

    def __init__(self, status: str):
        self.status = str(status or "unavailable")
        super().__init__(f"workspace is {self.status}")


def _server_workdir(server_workdir: str | Path | None = None) -> Path | None:
    value = str(
        server_workdir or os.environ.get("TCODE_WORKDIR", "") or ""
    ).strip()
    if not value:
        return None
    return Path(value).expanduser().resolve(strict=False)


def managed_workspace_storage_root(
    server_workdir: str | Path | None = None,
) -> Path | None:
    """Return the root containing ``tenant/user/workspace`` directories."""

    server_root = _server_workdir(server_workdir)
    if server_root is None:
        return None
    configured = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
    root = Path(configured).expanduser() if configured else server_root / "workdirs"
    if not root.is_absolute():
        root = server_root / root
    return root.resolve(strict=False)


def managed_workspace_owner_from_path(
    path_value: Any,
    *,
    server_workdir: str | Path | None = None,
) -> tuple[str, str, str] | None:
    """Extract the owner tuple encoded by a managed workspace path."""

    raw_path = str(path_value or "").strip()
    storage_root = managed_workspace_storage_root(server_workdir)
    if not raw_path or storage_root is None:
        return None
    try:
        relative = (
            Path(raw_path)
            .expanduser()
            .resolve(strict=False)
            .relative_to(storage_root)
        )
    except (OSError, ValueError):
        return None
    if len(relative.parts) < 3 or relative.parts[0].startswith("."):
        return None
    return tuple(str(value) for value in relative.parts[:3])


def _workspace_state_db_path(server_workdir: str | Path | None = None) -> Path | None:
    server_root = _server_workdir(server_workdir)
    if server_root is None:
        return None
    configured = str(os.environ.get("TCODE_WORKSPACE_STATE_DB", "") or "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else server_root / ".tcode" / "workspaces.sqlite3"
    )
    if not path.is_absolute():
        path = server_root / path
    return path.resolve(strict=False)


def managed_workspace_lifecycle_for_path(
    path_value: Any,
    *,
    server_workdir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read lifecycle status/generation without creating lifecycle storage."""

    owner = managed_workspace_owner_from_path(
        path_value,
        server_workdir=server_workdir,
    )
    state_path = _workspace_state_db_path(server_workdir)
    if owner is None or state_path is None or not state_path.is_file():
        return None
    try:
        connection = sqlite_connect(
            f"{state_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            columns = {
                str(item[1] or "")
                for item in connection.execute(
                    "PRAGMA table_info(workspace_states)"
                ).fetchall()
            }
            generation_column = "generation" if "generation" in columns else "1"
            row = connection.execute(
                f"""
                SELECT status, {generation_column} AS generation
                FROM workspace_states
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                owner,
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    if not row:
        return None
    status = str(row[0] or "").strip().lower()
    return {
        "tenant_id": owner[0],
        "user_id": owner[1],
        "workspace_id": owner[2],
        "status": status or "active",
        "generation": max(1, int(row[1] or 1)),
    }


def managed_workspace_status_for_path(
    path_value: Any,
    *,
    server_workdir: str | Path | None = None,
) -> str | None:
    lifecycle = managed_workspace_lifecycle_for_path(
        path_value,
        server_workdir=server_workdir,
    )
    if lifecycle is None:
        return None
    return str(lifecycle.get("status") or "active")


def managed_workspace_generation_for_path(
    path_value: Any,
    *,
    server_workdir: str | Path | None = None,
) -> int:
    lifecycle = managed_workspace_lifecycle_for_path(
        path_value,
        server_workdir=server_workdir,
    )
    return max(1, int((lifecycle or {}).get("generation") or 1))


def assert_managed_workspace_creation_allowed(
    path_value: Any,
    *,
    server_workdir: str | Path | None = None,
) -> None:
    """Reject creation of new persistence files inside a hidden workspace."""

    status = managed_workspace_status_for_path(
        path_value,
        server_workdir=server_workdir,
    )
    if status in HIDDEN_WORKSPACE_STATUSES:
        raise ManagedWorkspaceUnavailableError(status)
