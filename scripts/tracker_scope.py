"""Tracker worker scope discovery and request metadata shared by both workers."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

try:
    from tracker_token import read_tracker_token
except ImportError:  # package-style imports in tests
    from scripts.tracker_token import read_tracker_token

TCODE_ROOT = Path(__file__).resolve().parent.parent
_UNAVAILABLE_TRACKER_SCOPES: dict[tuple[str, str, str], float] = {}

def _tracker_base() -> str:
    return str(os.environ.get("TCODE_TRACKER_API_BASE", "http://127.0.0.1:8080") or "").rstrip("/")


def _truthy_env(name: str, default: str = "") -> bool:
    raw = str(os.environ.get(name, default) or default).strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _sanitize_scope_part(value: str, default: str = "") -> str:
    import re

    text = re.sub(r"[^A-Za-z0-9._@-]+", "-", str(value or "").strip()).strip(".-_@")
    return (text or default)[:128]


def _configured_tracker_scope() -> dict[str, str]:
    tenant_id = _first_env("TCODE_TRACKER_TENANT_ID", "TCODE_WORKER_TENANT_ID", "TCODE_DEFAULT_TENANT_ID")
    user_id = _first_env("TCODE_TRACKER_USER_ID", "TCODE_WORKER_USER_ID", "TCODE_DEFAULT_USER_ID")
    workspace_id = _first_env("TCODE_TRACKER_WORKSPACE_ID", "TCODE_WORKER_WORKSPACE_ID", "TCODE_DEFAULT_WORKSPACE_ID")
    return {
        "tenant_id": _sanitize_scope_part(tenant_id, "local-tenant"),
        "user_id": _sanitize_scope_part(user_id, ""),
        "workspace_id": _sanitize_scope_part(workspace_id, "workspace-1"),
    }


def _parse_tracker_scope_list() -> list[dict[str, str]]:
    raw = _first_env("TCODE_TRACKER_SCOPES", "TCODE_WORKER_SCOPES")
    scopes: list[dict[str, str]] = []
    if not raw:
        return scopes
    default_tenant = _configured_tracker_scope().get("tenant_id") or "local-tenant"
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split(":")]
        if len(parts) == 2:
            tenant_id, user_id, workspace_id = default_tenant, parts[0], parts[1]
        elif len(parts) >= 3:
            tenant_id, user_id, workspace_id = parts[0], parts[1], parts[2]
        else:
            continue
        scopes.append(
            {
                "tenant_id": _sanitize_scope_part(tenant_id, "local-tenant"),
                "user_id": _sanitize_scope_part(user_id, ""),
                "workspace_id": _sanitize_scope_part(workspace_id, "workspace-1"),
            }
        )
    return scopes


def _looks_like_workspace_dir(path: Path) -> bool:
    if not path.is_dir() or path.name.startswith(".") or path.name in {"tmp", "runs", "upload"}:
        return False
    if path.name.startswith("workspace-"):
        return True
    return any(
        candidate.exists()
        for candidate in (
            path / "tmp" / "shared_tracker.sqlite",
            path / ".tcode",
            path / "upload",
            path / "runs",
        )
    )


def _discover_tracker_scopes() -> list[dict[str, str]]:
    if not _truthy_env("TCODE_WORKER_DISCOVER_WORKSPACES", "1"):
        return []
    root = Path(str(os.environ.get("TCODE_WORKDIR") or TCODE_ROOT.as_posix())).expanduser().resolve(strict=False)
    scopes: list[dict[str, str]] = []

    configured_root = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
    workdirs_root = Path(configured_root).expanduser() if configured_root else root / "workdirs"
    if not workdirs_root.is_absolute():
        workdirs_root = root / workdirs_root
    workdirs_root = workdirs_root.resolve(strict=False)
    if workdirs_root.is_dir():
        for tenant_dir in sorted(item for item in workdirs_root.iterdir() if item.is_dir()):
            tenant_id = _sanitize_scope_part(tenant_dir.name, "")
            if not tenant_id:
                continue
            for user_dir in sorted(item for item in tenant_dir.iterdir() if item.is_dir()):
                user_id = _sanitize_scope_part(user_dir.name, "")
                if not user_id:
                    continue
                for workspace in sorted(item for item in user_dir.iterdir() if _looks_like_workspace_dir(item)):
                    scopes.append(
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "workspace_id": _sanitize_scope_part(workspace.name, "workspace-1"),
                        }
                    )
    return scopes


def _tracker_scopes() -> list[dict[str, str]]:
    scopes = _parse_tracker_scope_list()
    configured = _configured_tracker_scope()
    if configured.get("user_id") or _first_env("TCODE_TRACKER_WORKSPACE_ID", "TCODE_WORKER_WORKSPACE_ID", "TCODE_DEFAULT_WORKSPACE_ID"):
        scopes.append(configured)
    scopes.extend(_discover_tracker_scopes())

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, str]] = []
    for scope in scopes:
        key = (
            str(scope.get("tenant_id", "") or ""),
            str(scope.get("user_id", "") or ""),
            str(scope.get("workspace_id", "") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(scope)
    if deduped:
        return deduped
    return [{"tenant_id": "", "user_id": "", "workspace_id": ""}]


def _tracker_headers(scope: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = read_tracker_token(os.environ)
    if token:
        headers["X-Tracker-Token"] = token
    scope = scope if isinstance(scope, dict) else {}
    tenant_id = str(scope.get("tenant_id", "") or "").strip()
    user_id = str(scope.get("user_id", "") or "").strip()
    workspace_id = str(scope.get("workspace_id", "") or "").strip()
    if tenant_id:
        headers["X-TCode-Tenant-ID"] = tenant_id
    if user_id:
        headers["X-TCode-User-ID"] = user_id
    if workspace_id:
        headers["X-TCode-Workspace-ID"] = workspace_id
    return headers


def _tracker_scope_key(scope: Optional[dict[str, str]]) -> tuple[str, str, str]:
    value = scope if isinstance(scope, dict) else {}
    return (
        str(value.get("tenant_id", "") or ""),
        str(value.get("user_id", "") or ""),
        str(value.get("workspace_id", "") or ""),
    )


def _tracker_scope_poll_suspended(scope: Optional[dict[str, str]]) -> bool:
    key = _tracker_scope_key(scope)
    resume_at = float(_UNAVAILABLE_TRACKER_SCOPES.get(key, 0.0) or 0.0)
    if resume_at <= time.monotonic():
        _UNAVAILABLE_TRACKER_SCOPES.pop(key, None)
        return False
    return True


def _suspend_tracker_scope(
    scope: Optional[dict[str, str]],
    retry_after_seconds: Any,
) -> None:
    try:
        delay = float(retry_after_seconds or 60)
    except (TypeError, ValueError):
        delay = 60.0
    _UNAVAILABLE_TRACKER_SCOPES[_tracker_scope_key(scope)] = (
        time.monotonic() + max(10.0, min(delay, 3600.0))
    )
