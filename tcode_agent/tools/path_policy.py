from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..runtime.sandbox.workspace_protection import (
    hidden_workspace_paths,
    read_only_workspace_paths,
    workspace_path_matches,
)
from .base import ToolContext


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class WorkspacePathDecision:
    raw_path: str
    host_path: Path
    display_path: str
    relative_path: str
    container_path: str
    guarded: bool
    blocked: bool = False
    reason: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def _workdir_from_context(context: Optional[ToolContext]) -> Path:
    extra = getattr(context, "extra", None) if context is not None else None
    if isinstance(extra, dict):
        workdir = str(extra.get("workdir", "") or "").strip()
        if workdir:
            return Path(workdir).expanduser().resolve(strict=False)
    return Path(os.environ.get("TCODE_WORKDIR") or os.getcwd()).expanduser().resolve(strict=False)


def _container_root() -> str:
    return str(os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace") or "/workspace").rstrip("/") or "/workspace"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def _guard_enabled(context: Optional[ToolContext]) -> bool:
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        return True
    from ..runtime.sandbox.base import strict_isolation_enabled

    if strict_isolation_enabled():
        return True
    raw = os.environ.get("TCODE_WORKSPACE_IO_GUARD")
    if raw is not None:
        value = raw.strip().lower()
        if value in _FALSE_VALUES or value in {"disabled", "off"}:
            return False
        if value in _TRUE_VALUES or value in {"always", "strict"}:
            return True

    try:
        from ..runtime.sandbox import sandbox_session_enabled

        return bool(sandbox_session_enabled(context))
    except Exception:
        return False


def _operation_allowed_outside_workspace(operation: str) -> bool:
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        return False
    op = str(operation or "").strip().lower()
    if op in {"read", "search", "list", "glob", "grep"}:
        return _env_bool("TCODE_WORKSPACE_IO_ALLOW_EXTERNAL_READ", default=False)
    if op in {"write", "edit", "delete"}:
        return _env_bool("TCODE_WORKSPACE_IO_ALLOW_EXTERNAL_WRITE", default=False)
    return False


def _read_only_workspace_roots(context: Optional[ToolContext]) -> list[Path]:
    extra = getattr(context, "extra", None) if context is not None else None
    resources = extra.get("resource_context") if isinstance(extra, dict) else None
    workspace = resources.get("_workspace") if isinstance(resources, dict) else None
    raw_paths = workspace.get("read_only_paths") if isinstance(workspace, dict) else None
    if not isinstance(raw_paths, list):
        return []
    roots: list[Path] = []
    for value in raw_paths:
        text = str(value or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve(strict=False))
    return roots


def _resolve_host_path(raw_path: str, workdir: Path) -> Path:
    raw = str(raw_path or "").strip()
    container_root = _container_root()
    if raw == container_root:
        return workdir
    if raw.startswith(f"{container_root}/"):
        rel = raw[len(container_root) + 1 :]
        return (workdir / rel).expanduser().resolve(strict=False)
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.expanduser().resolve(strict=False)
    return (workdir / candidate).expanduser().resolve(strict=False)


def _requested_workspace_relative_path(raw_path: str, workdir: Path) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    container_root = _container_root()
    normalized_container = posixpath.normpath(raw.replace("\\", "/"))
    if normalized_container == container_root:
        return ""
    if normalized_container.startswith(f"{container_root}/"):
        candidate = normalized_container[len(container_root) + 1 :]
    else:
        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded):
            try:
                candidate = (
                    Path(os.path.abspath(expanded))
                    .relative_to(workdir)
                    .as_posix()
                )
            except Exception:
                return ""
        else:
            candidate = posixpath.normpath(expanded.replace("\\", "/"))
    normalized = posixpath.normpath(candidate.replace("\\", "/"))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def workspace_platform_path_access(
    path: str | Path,
    context: Optional[ToolContext],
) -> str:
    if not _guard_enabled(context):
        return ""
    workdir = _workdir_from_context(context)
    raw = os.path.abspath(os.path.expanduser(str(path)))
    try:
        relative = Path(raw).relative_to(workdir).as_posix()
    except Exception:
        return ""
    if workspace_path_matches(relative, hidden_workspace_paths()):
        return "hidden"
    if workspace_path_matches(relative, read_only_workspace_paths()):
        return "read_only"
    return ""


def _platform_workspace_path_block(
    raw_path: str,
    workdir: Path,
    *,
    guarded: bool,
    operation: str,
) -> WorkspacePathDecision | None:
    requested_rel = _requested_workspace_relative_path(raw_path, workdir)
    if not guarded or not requested_rel:
        return None

    hidden_root = workspace_path_matches(
        requested_rel,
        hidden_workspace_paths(),
    )
    read_only_root = workspace_path_matches(
        requested_rel,
        read_only_workspace_paths(),
    )
    operation_name = operation.strip().lower()
    if not hidden_root and not (
        read_only_root and operation_name in {"write", "edit", "delete"}
    ):
        return None

    host_path = _resolve_host_path(raw_path, workdir)
    rel, container = _relative_and_container(host_path, workdir)
    display_path = container or host_path.as_posix()
    if hidden_root:
        root = hidden_root
        access = "hidden"
        reason = (
            f"Workspace IO blocked: {display_path} belongs to platform "
            "control data and is not visible to Agent tools."
        )
    else:
        root = read_only_root
        access = "read_only"
        reason = (
            f"Workspace IO blocked: {display_path} is platform "
            "configuration and is read-only inside Agent sessions."
        )
    return WorkspacePathDecision(
        raw_path=str(raw_path or ""),
        host_path=host_path,
        display_path=display_path,
        relative_path=rel,
        container_path=container,
        guarded=guarded,
        blocked=True,
        reason=reason,
        metadata={
            "workspace_io_guarded": True,
            "workspace_io_blocked": True,
            "workspace": workdir.as_posix(),
            "host_path": host_path.as_posix(),
            "container_path": container,
            "platform_control_path": root,
            "platform_path_access": access,
        },
    )


def _relative_and_container(host_path: Path, workdir: Path) -> tuple[str, str]:
    try:
        rel = host_path.relative_to(workdir).as_posix()
    except Exception:
        return "", ""
    container_root = _container_root()
    container = container_root if not rel else f"{container_root}/{rel}"
    return rel, container


def _external_mount_entries(context: Optional[ToolContext]) -> list[dict[str, object]]:
    extra = getattr(context, "extra", None) if context is not None else None
    resource_context = extra.get("resource_context") if isinstance(extra, dict) else None
    if not isinstance(resource_context, dict):
        return []
    mounts = resource_context.get("external_mounts")
    if not isinstance(mounts, list):
        return []
    entries = [dict(item) for item in mounts if isinstance(item, dict)]
    return sorted(
        entries,
        key=lambda item: len(str(item.get("container_path") or "").rstrip("/")),
        reverse=True,
    )


def _container_candidate_for_raw_path(raw_path: str) -> str:
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    root = _container_root()
    if raw == root or raw.startswith(f"{root}/"):
        return raw
    if raw.startswith("./.external/"):
        return f"{root}/{raw[2:]}"
    if raw.startswith(".external/"):
        return f"{root}/{raw}"
    return ""


def _external_mount_for_path(
    raw_path: str,
    context: Optional[ToolContext],
) -> tuple[Path, str, dict[str, object]] | None:
    container_candidate = _container_candidate_for_raw_path(raw_path)
    raw = str(raw_path or "").strip()
    host_candidate: Path | None = None
    if raw:
        candidate = Path(raw)
        if candidate.is_absolute() and not container_candidate:
            host_candidate = candidate.expanduser().resolve(strict=False)

    for item in _external_mount_entries(context):
        source_text = str(item.get("source_path") or "").strip()
        container_path = str(item.get("container_path") or "").strip().rstrip("/")
        if not source_text or not container_path:
            continue
        source = Path(source_text).expanduser().resolve(strict=False)

        if container_candidate:
            if container_candidate == container_path:
                return source, container_path, item
            prefix = f"{container_path}/"
            if container_candidate.startswith(prefix):
                rel = container_candidate[len(prefix) :]
                return (source / rel).resolve(strict=False), container_candidate, item

        if host_candidate is not None:
            try:
                rel_path = host_candidate.relative_to(source)
            except Exception:
                continue
            rel = "" if not rel_path.parts else rel_path.as_posix()
            mapped_container = container_path if not rel else f"{container_path}/{rel}"
            return host_candidate, mapped_container, item

    return None


def _external_container_for_host_path(
    host_path: Path,
    context: Optional[ToolContext],
) -> str:
    target = Path(host_path).expanduser().resolve(strict=False)
    for item in _external_mount_entries(context):
        source_text = str(item.get("source_path") or "").strip()
        container_path = str(item.get("container_path") or "").strip().rstrip("/")
        if not source_text or not container_path:
            continue
        source = Path(source_text).expanduser().resolve(strict=False)
        try:
            rel_path = target.relative_to(source)
        except Exception:
            continue
        rel = "" if not rel_path.parts else rel_path.as_posix()
        return container_path if not rel else f"{container_path}/{rel}"
    return ""


def resolve_workspace_io_path(
    raw_path: str,
    context: Optional[ToolContext],
    *,
    tool_id: str,
    operation: str,
) -> WorkspacePathDecision:
    workdir = _workdir_from_context(context)
    guarded = _guard_enabled(context)
    platform_block = _platform_workspace_path_block(
        str(raw_path or ""),
        workdir,
        guarded=guarded,
        operation=operation,
    )
    if platform_block is not None:
        return platform_block

    external_mount = None if os.environ.get("TCODE_DESKTOP_MANAGED") == "1" else _external_mount_for_path(str(raw_path or ""), context)
    if external_mount is not None:
        host_path, container, item = external_mount
        metadata: dict[str, object] = {
            "workspace_io_guarded": guarded,
            "workspace": workdir.as_posix(),
            "host_path": host_path.as_posix(),
            "container_path": container,
            "external_mount": True,
            "source_path": str(item.get("source_path") or ""),
            "read_only": bool(item.get("read_only", True)),
        }
        if bool(item.get("workspace_input")):
            metadata["read_only_scientific_input"] = True
        if operation.strip().lower() in {"write", "edit", "delete"} and bool(item.get("read_only", True)):
            reason = (
                f"Workspace IO blocked: {container} is a read-only scientific input. "
                "Write derived artifacts under the run workspace outputs directory."
                if bool(item.get("workspace_input"))
                else (
                    f"Workspace IO blocked: {container} is a read-only external mount. "
                    "Copy it into the workspace first if it must be modified."
                )
            )
            return WorkspacePathDecision(
                raw_path=str(raw_path or ""),
                host_path=host_path,
                display_path=container,
                relative_path="",
                container_path=container,
                guarded=guarded,
                blocked=True,
                reason=reason,
                metadata={**metadata, "workspace_io_blocked": True},
            )
        return WorkspacePathDecision(
            raw_path=str(raw_path or ""),
            host_path=host_path,
            display_path=container,
            relative_path="",
            container_path=container,
            guarded=guarded,
            metadata=metadata,
        )

    host_path = _resolve_host_path(raw_path, workdir)
    inside = _inside(host_path, workdir)
    rel, container = _relative_and_container(host_path, workdir)
    display_path = container if guarded and container else host_path.as_posix()

    operation_name = operation.strip().lower()
    if operation_name in {"write", "edit", "delete"}:
        for read_only_root in _read_only_workspace_roots(context):
            if not _inside(host_path, read_only_root):
                continue
            reason = (
                f"Workspace IO blocked: {display_path} is a read-only scientific input. "
                "Write derived artifacts under the run workspace outputs directory."
            )
            return WorkspacePathDecision(
                raw_path=str(raw_path or ""),
                host_path=host_path,
                display_path=display_path,
                relative_path=rel,
                container_path=container,
                guarded=guarded,
                blocked=True,
                reason=reason,
                metadata={
                    "workspace_io_guarded": guarded,
                    "workspace_io_blocked": True,
                    "workspace": workdir.as_posix(),
                    "host_path": host_path.as_posix(),
                    "container_path": container,
                    "read_only_scientific_input": True,
                },
            )

    metadata: dict[str, object] = {
        "workspace_io_guarded": guarded,
        "workspace": workdir.as_posix(),
        "host_path": host_path.as_posix(),
    }
    if rel:
        metadata["relative_path"] = rel
    if container:
        metadata["container_path"] = container

    if guarded and not inside and not _operation_allowed_outside_workspace(operation):
        reason = (
            f"Workspace IO blocked: tool '{tool_id}' cannot {operation} outside "
            f"the active workspace while sandbox mode is enabled. Call import_resource "
            f"for external inputs first, then use the returned /workspace/... path."
        )
        return WorkspacePathDecision(
            raw_path=str(raw_path or ""),
            host_path=host_path,
            display_path=host_path.as_posix(),
            relative_path="",
            container_path="",
            guarded=guarded,
            blocked=True,
            reason=reason,
            metadata={**metadata, "workspace_io_blocked": True},
        )

    return WorkspacePathDecision(
        raw_path=str(raw_path or ""),
        host_path=host_path,
        display_path=display_path,
        relative_path=rel,
        container_path=container,
        guarded=guarded,
        metadata=metadata,
    )


def display_path_for_host_path(path: str | Path, context: Optional[ToolContext]) -> str:
    workdir = _workdir_from_context(context)
    host_path = Path(path).expanduser().resolve(strict=False)
    external_container = _external_container_for_host_path(host_path, context)
    if external_container:
        return external_container
    guarded = _guard_enabled(context)
    _, container = _relative_and_container(host_path, workdir)
    if guarded and container:
        return container
    return host_path.as_posix()
