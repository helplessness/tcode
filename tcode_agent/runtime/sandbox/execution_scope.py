"""Platform-owned execution registrations, outside task-writable directories."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .workspace_snapshot import _parent_fd


def scope_path(owner_workspace: Path, execution_id: str) -> Path:
    key = hashlib.sha256(execution_id.encode()).hexdigest()
    return owner_workspace / ".tcode" / "execution-scopes" / f"{key}.json"


def register_workspace(workspace) -> None:
    from .base import strict_isolation_enabled

    if not strict_isolation_enabled() or not workspace.isolated:
        return
    control = Path(workspace.control_workdir).resolve(strict=True)
    owner_workspace = control
    for name in ("TCODE_USER_WORKDIRS_ROOT", "TCODE_SANDBOX_WORKSPACE_ROOT"):
        value = os.environ.get(name)
        if not value:
            continue
        root = Path(value).resolve(strict=True)
        if control.is_relative_to(root):
            parts = control.relative_to(root).parts
            offset = 1 if parts and parts[0] == "workdirs" else 0
            if len(parts) >= offset + 3:
                owner_workspace = root.joinpath(*parts[:offset + 3])
                break
    execution_id = str(workspace.task_id or workspace.session_id)
    path = scope_path(owner_workspace, execution_id)
    payload = {
        "execution_id": execution_id,
        "session_id": workspace.session_id,
        "execution_workdir": str(Path(workspace.execution_workdir).resolve(strict=False)),
        "active": workspace.status != "cleaned",
    }
    root_fd = os.open(owner_workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with _parent_fd(root_fd, path.relative_to(owner_workspace).as_posix(), create=True) as (parent, name):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                fd = None
            if fd is not None:
                with os.fdopen(fd) as stream:
                    existing = json.load(stream)
                if existing["execution_workdir"] != payload["execution_workdir"] or existing["session_id"] != payload["session_id"]:
                    raise ValueError("execution identity is already bound to another directory or session")
            temp = f".scope-{uuid.uuid4().hex}"
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, "w") as stream:
                    json.dump(payload, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass
    finally:
        os.close(root_fd)


def validate_execution(owner_workspace: Path, execution_workspace: Path, metadata: dict) -> None:
    path = scope_path(owner_workspace, str(metadata.get("execution_id") or ""))
    try:
        root_fd = os.open(owner_workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with _parent_fd(root_fd, path.relative_to(owner_workspace).as_posix()) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(fd) as stream:
                    grant = json.load(stream)
        finally:
            os.close(root_fd)
    except (OSError, ValueError) as exc:
        raise ValueError("execution has no platform workspace registration") from exc
    if (
        grant.get("active") is not True
        or grant.get("execution_id") != metadata.get("execution_id")
        or grant.get("session_id") != metadata.get("session_id")
        or grant.get("execution_workdir") != str(execution_workspace)
    ):
        raise ValueError("execution identity does not match its registered workspace")
