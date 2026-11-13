"""User-visible workspace file discovery.

The workspace is also a runtime boundary: it contains copied configuration,
skill/plugin bundles, databases, logs, and other implementation details.  The
``@`` picker must therefore use provenance and public output conventions rather
than exposing the workspace as a generic file tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import workdirs


_SCAN_LIMIT = 20_000
_PUBLIC_OUTPUT_DIRS = {
    "artifact",
    "artifacts",
    "deliverable",
    "deliverables",
    "download",
    "downloads",
    "export",
    "exports",
    "figure",
    "figures",
    "output",
    "outputs",
    "report",
    "reports",
    "result",
    "results",
}
_INTERNAL_TOP_LEVEL_DIRS = {
    "__pycache__",
    "cache",
    "caches",
    "log",
    "logs",
    "node_modules",
    "plugin",
    "plugins",
    "run",
    "runs",
    "skill",
    "skills",
    "temp",
    "tmp",
    "venv",
    "workflow_runs",
}
_INTERNAL_FILE_NAMES = {
    "agents.md",
    "biosafety_policy.json",
    "claude.md",
    "mcp.json",
    "mcp_agent_config.json",
    "mcp_agent_config.example.json",
    "tcode.json",
    "plugins.json",
    "skill.md",
}
_INTERNAL_SUFFIXES = {
    ".db",
    ".db3",
    ".duckdb",
    ".lock",
    ".log",
    ".pid",
    ".sqlite",
    ".sqlite3",
}
_EVENT_TYPES = ("file.uploaded", "file.written", "artifact.created")


@dataclass(frozen=True)
class WorkspaceFileCandidate:
    path: Path
    relative_path: str
    origin: str


def _template_paths(server_workdir: str) -> set[str]:
    if not str(server_workdir or "").strip():
        return set()
    template_root = workdirs.workdir_template_root(server_workdir)
    if not template_root.is_dir():
        return set()
    result: set[str] = set()
    try:
        for item in template_root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                result.add(item.relative_to(template_root).as_posix().casefold())
    except OSError:
        return result
    return result


def _is_internal_path(relative_path: str, template_paths: set[str]) -> bool:
    relative = Path(relative_path)
    parts = relative.parts
    if not parts:
        return True
    folded_parts = tuple(part.casefold() for part in parts)
    if any(part.startswith(".") for part in parts):
        return True
    if folded_parts[0] in _INTERNAL_TOP_LEVEL_DIRS:
        return True
    if relative_path.casefold() in template_paths:
        return True
    name = folded_parts[-1]
    if name in _INTERNAL_FILE_NAMES:
        return True
    suffix = Path(name).suffix.casefold()
    if suffix in _INTERNAL_SUFFIXES or name.endswith(("-shm", "-wal")):
        return True
    if suffix == ".json" and name.startswith(("mcp", "tcode", "plugins")):
        return True
    return False


def _is_external_runtime_stage(relative_path: str) -> bool:
    parts = tuple(part.casefold() for part in Path(relative_path).parts)
    return len(parts) >= 3 and parts[0] == "upload" and parts[2] == "external"


def _resolve_event_path(raw_path: Any, workspace_root: Path) -> Path | None:
    text = str(raw_path or "").strip().strip('"').strip("'")
    if not text or text.startswith(("session:", "tool:", "agent:")):
        return None
    if text == "/workspace":
        candidate = workspace_root
    elif text.startswith("/workspace/"):
        candidate = workspace_root / text.removeprefix("/workspace/")
    else:
        raw = Path(text).expanduser()
        candidate = raw if raw.is_absolute() else workspace_root / raw
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError):
        return None
    return resolved


def _payload_path_values(payload: Any) -> Iterable[Any]:
    if isinstance(payload, dict):
        for raw_key, value in payload.items():
            key = str(raw_key or "").casefold()
            if "path" in key:
                if isinstance(value, (str, Path)):
                    yield value
                elif isinstance(value, (list, tuple, set)):
                    for item in value:
                        if isinstance(item, (str, Path)):
                            yield item
                        elif isinstance(item, dict):
                            yield from _payload_path_values(item)
            elif isinstance(value, (dict, list, tuple, set)):
                yield from _payload_path_values(value)
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            yield from _payload_path_values(item)


def _event_provenance(
    workspace_root: Path,
    context: workdirs.WorkspaceContext | None,
) -> dict[Path, str]:
    """Read existing research events without creating a journal as a side effect."""

    if context is None:
        return {}
    try:
        from ..research import ResearchEventStore, load_research_settings

        settings = load_research_settings()
        if not settings.database_path(workspace_root.as_posix()).is_file():
            return {}
        provenance: dict[Path, str] = {}
        for event_type in _EVENT_TYPES:
            events = ResearchEventStore.list_events(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                workdir=workspace_root.as_posix(),
                event_type=event_type,
                limit=5000,
                settings=settings,
            )
            origin = "uploaded" if event_type == "file.uploaded" else "generated"
            for event in events:
                values = [event.source_ref, *_payload_path_values(event.payload)]
                for value in values:
                    resolved = _resolve_event_path(value, workspace_root)
                    if resolved is not None:
                        provenance[resolved] = origin
        return provenance
    except Exception:
        # The picker must keep working when research recording is disabled or a
        # legacy journal cannot be read.  Directory conventions remain safe.
        return {}


def _walk_files(root: Path, *, budget: list[int]) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return
    for current_root, dir_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        if any(name.casefold() == "skill.md" for name in file_names):
            dir_names[:] = []
            continue
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not name.startswith(".")
            and name.casefold() not in _INTERNAL_TOP_LEVEL_DIRS
            and not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            budget[0] += 1
            if budget[0] > _SCAN_LIMIT:
                return
            path = current / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            yield path


def collect_user_visible_workspace_files(
    workspace_root: Path,
    *,
    server_workdir: str = "",
    context: workdirs.WorkspaceContext | None = None,
) -> tuple[list[WorkspaceFileCandidate], int, bool]:
    """Return only uploaded files and public/generated workspace artifacts."""

    root = workspace_root.expanduser().resolve(strict=False)
    if not root.is_dir():
        return [], 0, False
    if context is None and str(server_workdir or "").strip():
        context = workdirs.workspace_context_from_managed_path(server_workdir, root)

    template_paths = _template_paths(server_workdir)
    provenance = _event_provenance(root, context)
    provenance_files: dict[Path, str] = {}
    budget = [0]

    for source, origin in provenance.items():
        if source.is_file() and not source.is_symlink():
            provenance_files[source.resolve(strict=False)] = origin
        elif source.is_dir() and not source.is_symlink():
            for item in _walk_files(source, budget=budget):
                provenance_files[item.resolve(strict=False)] = origin

    candidates: dict[str, WorkspaceFileCandidate] = {}

    def add_file(path: Path, origin: str) -> None:
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            return
        if resolved.is_symlink() or not resolved.is_file():
            return
        if _is_internal_path(relative, template_paths):
            return
        if _is_external_runtime_stage(relative):
            proven_origin = provenance_files.get(resolved)
            if proven_origin != "uploaded":
                return
            origin = proven_origin
        candidates[relative] = WorkspaceFileCandidate(resolved, relative, origin)

    # Explicit upload/tool provenance may point to nested files outside the
    # conventional public directories.
    for path, origin in provenance_files.items():
        add_file(path, origin)

    # Files at workspace root are generated deliverables.  Template and control
    # files are excluded above, so a Bash-created workbook/report remains usable
    # even when the tool did not emit artifact metadata.
    try:
        root_items = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        root_items = []
    for item in root_items:
        budget[0] += 1
        if budget[0] > _SCAN_LIMIT:
            break
        if item.is_file() and not item.is_symlink():
            add_file(item, "generated")

    # Direct browser uploads are public.  Session ``external`` staging is only
    # public when an exact upload provenance record exists, preventing copied
    # skills and plugins from leaking into the picker.
    upload_root = root / "upload"
    for item in _walk_files(upload_root, budget=budget):
        if budget[0] > _SCAN_LIMIT:
            break
        add_file(item, "uploaded")

    for dirname in sorted(_PUBLIC_OUTPUT_DIRS):
        public_root = root / dirname
        for item in _walk_files(public_root, budget=budget):
            if budget[0] > _SCAN_LIMIT:
                break
            add_file(item, "generated")

    return (
        sorted(candidates.values(), key=lambda item: item.relative_path.casefold()),
        budget[0],
        budget[0] > _SCAN_LIMIT,
    )


def is_user_visible_workspace_file(
    path: Path,
    *,
    workspace_root: Path,
    server_workdir: str = "",
    context: workdirs.WorkspaceContext | None = None,
) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return False
    candidates, _scanned, _truncated = collect_user_visible_workspace_files(
        workspace_root,
        server_workdir=server_workdir,
        context=context,
    )
    return any(item.path == resolved for item in candidates)
