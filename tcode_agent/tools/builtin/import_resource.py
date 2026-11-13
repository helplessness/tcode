"""Import external files or directories into the active workspace."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..base import ParameterSchema, Tool, ToolContext
from ...runtime.resource_import import (
    mount_resource_for_workspace,
    path_is_inside_workspace,
    resolve_resource_source,
    should_mount_resource,
    stage_resource_for_workspace,
)
from ...types import ToolResult


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class ImportResourceParameters(ParameterSchema):
    """Parameters for import_resource tool."""

    path: str | None = None
    paths: list[str] | None = None
    mode: str | None = "auto"
    reason: str | None = None


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


def _workdir_from_context(context: ToolContext) -> str:
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        workdir = str(extra.get("workdir", "") or "").strip()
        if workdir:
            return workdir
    return os.environ.get("TCODE_WORKDIR") or os.getcwd()


def _normalize_requested_paths(parsed: ImportResourceParameters) -> list[str]:
    requested: list[str] = []
    if parsed.path:
        requested.append(str(parsed.path))
    for item in parsed.paths or []:
        if item:
            requested.append(str(item))

    seen: set[str] = set()
    deduped: list[str] = []
    for item in requested:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _normalize_mode(mode: str | None) -> str:
    normalized = str(mode or "auto").strip().lower()
    if normalized in {"copy", "mount", "auto"}:
        return normalized
    return "auto"


async def _request_external_import_permission(
    context: ToolContext,
    sources: list[Path],
    *,
    reason: str = "",
) -> bool:
    if not _env_bool("TCODE_IMPORT_RESOURCE_REQUIRE_PERMISSION", True):
        return True

    patterns = [source.as_posix() for source in sources]
    extra = getattr(context, "extra", None)
    prompt = extra.get("__permission_prompt__") if isinstance(extra, dict) else None
    if callable(prompt):
        decision = prompt(
            "import_resource",
            {
                "operation": "import_external_resource",
                "patterns": patterns,
                "paths": patterns,
                "reason": reason,
            },
        )
        if hasattr(decision, "__await__"):
            decision = await decision
        if isinstance(decision, bool):
            return decision

        normalized = str(decision or "").strip().lower()
        if normalized in {"always", "allow_always"}:
            remember = extra.get("__permission_remember__") if isinstance(extra, dict) else None
            if callable(remember):
                remember("import_resource", patterns)
            return True
        return normalized in {"allow", "once", "y", "yes", "true"}

    return await context.ask("import_resource", patterns)


class ImportResourceTool(Tool):
    """Import external resources into the current workspace."""

    @property
    def id(self) -> str:
        return "import_resource"

    @property
    def description(self) -> str:
        return """Import an external file or directory into the active workspace so sandboxed tools can access it.

Use this when a user or prior command references a host path outside the current workspace and later steps need read, grep, bash, exec, browser, or MCP tools to see that content.

IMPORTANT:
- mode="auto" copies small external files and uses read-only mounts for directories or large files
- mode="copy" copies outside-workspace inputs into workspace/upload/<session>/external/
- mode="mount" registers outside-workspace inputs as read-only Docker bind mounts without copying
- External paths require user permission before import
- After import, use the returned /workspace/... path for all follow-up tool calls
- Do not keep using the original host path after import

Examples:
- path="/path/to/input.pdb", mode="auto"
- path="/data/big-dataset", mode="mount", reason="score external dataset in sandbox"
"""

    @property
    def parameters(self) -> type:
        return ImportResourceParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ImportResourceParameters(**args)
        mode = _normalize_mode(parsed.mode)
        requested_paths = _normalize_requested_paths(parsed)
        if not requested_paths:
            return ToolResult(
                title="Import resource",
                output="",
                error="No resource path was provided. Pass path or paths.",
            )

        workdir = _workdir_from_context(context)
        resolved: list[Path] = []
        resolve_errors: list[str] = []
        for raw_path in requested_paths:
            try:
                resolved.append(resolve_resource_source(raw_path, workdir))
            except Exception as exc:
                resolve_errors.append(f"{raw_path}: {exc}")

        if resolve_errors and not resolved:
            return ToolResult(
                title="Import resource",
                output="",
                error="\n".join(resolve_errors),
                metadata={"workspace_io_import": True, "resolve_errors": resolve_errors},
            )

        external_sources = [
            source for source in resolved if not path_is_inside_workspace(source, workdir)
        ]
        if external_sources:
            approved = await _request_external_import_permission(
                context,
                external_sources,
                reason=f"{str(parsed.reason or '').strip()} mode={mode}".strip(),
            )
            if not approved:
                return ToolResult(
                    title="Import resource",
                    output="",
                    error="Permission rejected for importing external resources.",
                    metadata={
                        "workspace_io_import": True,
                        "permission_rejected": True,
                        "requested_paths": [source.as_posix() for source in external_sources],
                    },
                )

        resources: list[dict[str, Any]] = []
        external_mounts: list[dict[str, Any]] = []
        import_errors = list(resolve_errors)
        for source in resolved:
            inside_workspace = path_is_inside_workspace(source, workdir)
            use_mount = False
            if not inside_workspace:
                use_mount = mode == "mount" or (mode == "auto" and should_mount_resource(source))

            if use_mount:
                item = mount_resource_for_workspace(
                    source,
                    session_id=context.session_id,
                    workdir=workdir,
                )
            else:
                item = stage_resource_for_workspace(
                    source,
                    session_id=context.session_id,
                    workdir=workdir,
                )
            if item.get("import_error"):
                import_errors.append(f"{source.as_posix()}: {item.get('import_error')}")
                continue
            resources.append(item)
            if item.get("mounted"):
                external_mounts.append(
                    {
                        "name": item.get("name"),
                        "source_path": item.get("source_path"),
                        "container_path": item.get("container_path"),
                        "mount_target_path": item.get("mount_target_path") or item.get("path"),
                        "kind": item.get("kind"),
                        "read_only": True,
                    }
                )

        lines: list[str] = []
        for item in resources:
            source_path = str(item.get("source_path") or item.get("path") or "")
            container_path = str(item.get("container_path") or item.get("path") or "")
            if item.get("mounted"):
                verb = "Mounted read-only"
            elif item.get("imported"):
                verb = "Imported"
            else:
                verb = "Already in workspace"
            lines.append(f"{verb}: {source_path} -> {container_path}")

        resource_context_patch: dict[str, Any] = {}
        if resources:
            resource_context_patch["uploaded_files"] = resources
        if external_mounts:
            resource_context_patch["external_mounts"] = external_mounts

        metadata = {
            "workspace_io_import": True,
            "imported_resources": resources,
            "external_mounts": external_mounts,
            "import_mode": mode,
            "permission_requested": bool(external_sources),
            "import_errors": import_errors,
            "resource_context_patch": resource_context_patch,
        }
        return ToolResult(
            title="Import resource",
            output="\n".join(lines),
            error="\n".join(import_errors) if import_errors else None,
            metadata=metadata,
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
