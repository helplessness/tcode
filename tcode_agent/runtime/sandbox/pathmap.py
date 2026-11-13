from __future__ import annotations

from pathlib import Path

from .base import WorkspacePathMap


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def assert_inside_workspace(path: Path | str, workspace: Path | str) -> Path:
    target = _resolve(Path(path))
    root = _resolve(Path(workspace))
    if not _inside(target, root):
        raise ValueError(f"path escapes workspace: {target}")
    return target


def host_to_container_path(path: Path | str, pathmap: WorkspacePathMap) -> str:
    host_path = assert_inside_workspace(Path(path), pathmap.host_workspace)
    rel = host_path.relative_to(pathmap.host_workspace)
    if not rel.parts:
        return pathmap.container_workspace
    return f"{pathmap.container_workspace}/{rel.as_posix()}"


def container_to_host_path(path: str, pathmap: WorkspacePathMap) -> Path:
    text = str(path or "").strip()
    container_root = pathmap.container_workspace.rstrip("/") or "/workspace"
    if text == container_root:
        return pathmap.host_workspace
    prefix = f"{container_root}/"
    if not text.startswith(prefix):
        raise ValueError(f"container path is outside workspace: {text}")
    rel = text[len(prefix) :]
    return assert_inside_workspace(pathmap.host_workspace / rel, pathmap.host_workspace)


def normalize_container_path(path: str, pathmap: WorkspacePathMap) -> str:
    host_path = container_to_host_path(path, pathmap)
    return host_to_container_path(host_path, pathmap)
