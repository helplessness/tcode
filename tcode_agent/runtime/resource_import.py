from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any


RESOURCE_PATH_KEYS = (
    "source_path",
    "path",
    "mount_target_path",
    "container_path",
    "relative_path",
)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def container_root() -> str:
    return str(os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace") or "/workspace").rstrip("/") or "/workspace"


def attachment_kind(path: Path) -> str:
    if path.is_file() and path.suffix.lower() == ".pdf":
        return "pdf"
    if path.is_file() and path.suffix.lower() == ".zip":
        return "zip"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def workspace_relative_path(path: Path, workdir: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(workdir.resolve(strict=False)).as_posix()
    except Exception:
        return ""


def container_path_for_workspace_relative(relative_path: str) -> str:
    rel = str(relative_path or "").strip().strip("/")
    root = container_root()
    return f"{root}/{rel}" if rel else root


def external_mount_relative_path(source: Path, *, session_id: str = "") -> str:
    session = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "session")).strip("._") or "session"
    return f".external/{session}/{safe_external_import_name(source)}"


def resolve_resource_source(raw_path: str | Path, workdir: str | Path) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("resource path is required")

    workspace = Path(workdir).expanduser().resolve(strict=False)
    root = container_root()
    if raw == root:
        return workspace
    if raw.startswith(f"{root}/"):
        rel = raw[len(root) + 1 :]
        return (workspace / rel).expanduser().resolve(strict=True)

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=True)
    return (workspace / candidate).expanduser().resolve(strict=True)


def iter_resource_descriptors(value: Any, *, depth: int = 0):
    """Yield path-bearing resource records from an arbitrary context tree."""

    if depth > 8:
        return
    if isinstance(value, dict):
        if any(str(value.get(key) or "").strip() for key in RESOURCE_PATH_KEYS):
            yield value
        for item in value.values():
            yield from iter_resource_descriptors(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_resource_descriptors(item, depth=depth + 1)


def resource_descriptor_for_reference(
    reference: str | Path,
    *,
    workdir: str | Path,
    inherited_context: Any = None,
) -> dict[str, Any]:
    """Resolve one caller-visible reference into a portable resource record.

    Container paths are capabilities scoped to the caller's execution
    workspace.  Prefer the caller's existing descriptor (which retains the
    authorized source) and otherwise resolve the path against that execution
    workspace.  A missing reference remains descriptive and will fail closed
    when a child workspace attempts to materialize it.
    """

    raw = str(reference or "").strip()
    if not raw:
        return {}
    for item in iter_resource_descriptors(inherited_context):
        if any(str(item.get(key) or "").strip() == raw for key in RESOURCE_PATH_KEYS):
            return dict(item)

    descriptor: dict[str, Any] = {"path": raw}
    if raw == container_root() or raw.startswith(f"{container_root()}/"):
        descriptor["container_path"] = raw
    try:
        source = resolve_resource_source(raw, workdir)
    except Exception:
        return descriptor
    if not source.is_file() and not source.is_dir():
        return descriptor
    relative = workspace_relative_path(source, Path(workdir))
    descriptor.update(
        {
            "name": source.name,
            "kind": attachment_kind(source),
            "source_path": source.as_posix(),
            "path": source.as_posix(),
            "relative_path": relative,
            "available_in_sandbox": bool(relative),
        }
    )
    if relative:
        descriptor["container_path"] = container_path_for_workspace_relative(relative)
    return descriptor


def normalize_resource_argument(
    value: Any,
    *,
    workdir: str | Path,
    inherited_context: Any = None,
) -> dict[str, Any]:
    """Normalize public tool resource arguments without assuming a domain."""

    if value in (None, "", {}):
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                items.append(dict(item))
            elif str(item or "").strip():
                items.append(
                    resource_descriptor_for_reference(
                        str(item),
                        workdir=workdir,
                        inherited_context=inherited_context,
                    )
                )
        return {"items": [item for item in items if item]}
    text = str(value or "").strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            import json

            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return normalize_resource_argument(
                parsed,
                workdir=workdir,
                inherited_context=inherited_context,
            )
    descriptor = resource_descriptor_for_reference(
        text,
        workdir=workdir,
        inherited_context=inherited_context,
    )
    return {"items": [descriptor]} if descriptor else {}


def path_is_inside_workspace(path: Path, workdir: str | Path) -> bool:
    workspace = Path(workdir).expanduser().resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(workspace)
        return True
    except Exception:
        return False


def safe_external_import_name(source: Path) -> str:
    digest = hashlib.sha256(source.as_posix().encode("utf-8", errors="ignore")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name).strip("._") or "external_input"
    if source.is_file() and source.suffix:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._") or "external_input"
        return f"{stem}-{digest}{source.suffix}"
    return f"{name}-{digest}"


def path_tree_size(path: Path, *, max_files: int) -> tuple[int, int]:
    if path.is_file():
        return 1, int(path.stat().st_size)
    files = 0
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        files += 1
        if files > max_files:
            break
        try:
            total += int(item.stat().st_size)
        except Exception:
            pass
    return files, total


def should_mount_resource(source: Path) -> bool:
    """Choose mount for resources where copying is likely expensive."""
    if source.is_dir():
        raw = os.environ.get("TCODE_IMPORT_RESOURCE_AUTO_COPY_DIRS")
        if raw is not None and raw.strip().lower() in {"1", "true", "yes", "on"}:
            return False
        return True
    if not source.is_file():
        return False
    threshold = max(1, env_int("TCODE_IMPORT_RESOURCE_AUTO_MOUNT_BYTES", 64 * 1024 * 1024))
    try:
        return int(source.stat().st_size) > threshold
    except Exception:
        return False


def mount_resource_for_workspace(
    source: str | Path,
    *,
    session_id: str = "",
    workdir: str = "",
) -> dict[str, Any]:
    root = Path(workdir or "").expanduser().resolve(strict=False)
    if not str(workdir or "").strip():
        raw_source = Path(source).expanduser()
        return {
            "name": raw_source.name,
            "path": raw_source.as_posix(),
            "kind": "other",
            "available_in_sandbox": False,
            "mounted": False,
            "import_error": "external path was not mounted because workdir is unavailable",
        }

    raw_source = Path(source).expanduser()
    try:
        resolved_source = raw_source.resolve(strict=True)
    except Exception as exc:
        return {
            "name": raw_source.name,
            "path": raw_source.as_posix(),
            "kind": "other",
            "available_in_sandbox": False,
            "mounted": False,
            "import_error": f"external input does not exist or is inaccessible: {exc}",
        }

    kind = attachment_kind(resolved_source)
    if not resolved_source.is_file() and not resolved_source.is_dir():
        return {
            "name": resolved_source.name,
            "path": resolved_source.as_posix(),
            "source_path": resolved_source.as_posix(),
            "kind": kind,
            "available_in_sandbox": False,
            "mounted": False,
            "import_error": "external input is not a regular file or directory",
        }

    rel = external_mount_relative_path(resolved_source, session_id=session_id)
    mountpoint = (root / rel).resolve(strict=False)
    try:
        mountpoint.relative_to(root)
    except Exception:
        return {
            "name": resolved_source.name,
            "path": resolved_source.as_posix(),
            "source_path": resolved_source.as_posix(),
            "kind": kind,
            "available_in_sandbox": False,
            "mounted": False,
            "import_error": "external mount destination escapes workspace",
        }

    try:
        if resolved_source.is_dir():
            mountpoint.mkdir(parents=True, exist_ok=True)
        else:
            mountpoint.parent.mkdir(parents=True, exist_ok=True)
            if mountpoint.exists() and mountpoint.is_dir():
                shutil.rmtree(mountpoint)
            mountpoint.touch(exist_ok=True)
    except Exception as exc:
        return {
            "name": resolved_source.name,
            "path": mountpoint.as_posix(),
            "source_path": resolved_source.as_posix(),
            "kind": kind,
            "available_in_sandbox": False,
            "mounted": False,
            "import_error": f"failed to prepare external mountpoint: {exc}",
        }

    container_path = container_path_for_workspace_relative(rel)
    return {
        "name": resolved_source.name,
        "path": mountpoint.as_posix(),
        "source_path": resolved_source.as_posix(),
        "relative_path": rel,
        "container_path": container_path,
        "mount_target_path": mountpoint.as_posix(),
        "kind": kind,
        "available_in_sandbox": True,
        "imported": False,
        "mounted": True,
        "read_only": True,
        "import_mode": "mount",
    }


def stage_resource_for_workspace(
    source: str | Path,
    *,
    session_id: str = "",
    workdir: str = "",
) -> dict[str, Any]:
    root = Path(workdir or "").expanduser().resolve(strict=False)
    if not str(workdir or "").strip():
        raw_source = Path(source).expanduser()
        return {
            "name": raw_source.name,
            "path": raw_source.as_posix(),
            "kind": "other",
            "available_in_sandbox": False,
            "imported": False,
            "import_error": "external path was not imported because workdir is unavailable",
        }

    raw_source = Path(source).expanduser()
    try:
        resolved_source = raw_source.resolve(strict=True)
    except Exception as exc:
        return {
            "name": raw_source.name,
            "path": raw_source.as_posix(),
            "kind": "other",
            "available_in_sandbox": False,
            "imported": False,
            "import_error": f"external input does not exist or is inaccessible: {exc}",
        }

    kind = attachment_kind(resolved_source)
    if not resolved_source.is_file() and not resolved_source.is_dir():
        return {
            "name": resolved_source.name,
            "path": resolved_source.as_posix(),
            "source_path": resolved_source.as_posix(),
            "kind": kind,
            "available_in_sandbox": False,
            "imported": False,
            "import_error": "external input is not a regular file or directory",
        }

    try:
        resolved_source.relative_to(root)
        staged_path = resolved_source
        imported = False
    except Exception:
        if not str(session_id or "").strip():
            return {
                "name": resolved_source.name,
                "path": resolved_source.as_posix(),
                "source_path": resolved_source.as_posix(),
                "kind": kind,
                "available_in_sandbox": False,
                "imported": False,
                "import_error": "external path was not imported because session_id is unavailable",
            }

        max_bytes = max(1, env_int("TCODE_EXTERNAL_INPUT_IMPORT_MAX_BYTES", 500 * 1024 * 1024))
        max_files = max(1, env_int("TCODE_EXTERNAL_INPUT_IMPORT_MAX_FILES", 2048))
        file_count, total_bytes = path_tree_size(resolved_source, max_files=max_files)
        if file_count > max_files:
            return {
                "name": resolved_source.name,
                "path": resolved_source.as_posix(),
                "source_path": resolved_source.as_posix(),
                "kind": kind,
                "available_in_sandbox": False,
                "imported": False,
                "import_error": f"external input has too many files ({file_count} > {max_files})",
            }
        if total_bytes > max_bytes:
            return {
                "name": resolved_source.name,
                "path": resolved_source.as_posix(),
                "source_path": resolved_source.as_posix(),
                "kind": kind,
                "available_in_sandbox": False,
                "imported": False,
                "import_error": f"external input is too large ({total_bytes} > {max_bytes} bytes)",
            }

        external_root = (root / "upload" / str(session_id).strip() / "external").resolve()
        staged_path = (external_root / safe_external_import_name(resolved_source)).resolve()
        try:
            staged_path.relative_to(external_root)
        except Exception:
            return {
                "name": resolved_source.name,
                "path": resolved_source.as_posix(),
                "source_path": resolved_source.as_posix(),
                "kind": kind,
                "available_in_sandbox": False,
                "imported": False,
                "import_error": "external import destination escapes upload root",
            }

        try:
            external_root.mkdir(parents=True, exist_ok=True)
            if resolved_source.is_dir():
                if staged_path.exists():
                    shutil.rmtree(staged_path)
                shutil.copytree(resolved_source, staged_path, symlinks=False)
            else:
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved_source, staged_path)
        except Exception as exc:
            return {
                "name": resolved_source.name,
                "path": resolved_source.as_posix(),
                "source_path": resolved_source.as_posix(),
                "kind": kind,
                "available_in_sandbox": False,
                "imported": False,
                "import_error": f"failed to import external input: {exc}",
            }
        imported = True

    rel = workspace_relative_path(staged_path, root)
    return {
        "name": resolved_source.name,
        "path": staged_path.as_posix(),
        "source_path": resolved_source.as_posix(),
        "relative_path": rel,
        "container_path": container_path_for_workspace_relative(rel),
        "kind": kind,
        "available_in_sandbox": bool(rel),
        "imported": imported,
        "mounted": False,
        "read_only": False,
        "import_mode": "copy" if imported else "workspace",
    }
