"""Glob tool - Find files by pattern"""

import glob as glob_module
from typing import Any

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ..path_policy import (
    display_path_for_host_path,
    resolve_workspace_io_path,
    workspace_platform_path_access,
)
from ...types import ToolResult


class GlobParameters(ParameterSchema):
    """Parameters for glob tool"""

    pattern: str
    path: str | None = None


class GlobTool(Tool):
    """Find files by glob pattern"""

    @property
    def id(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern. Use patterns like "
            "'**/*.py' for all Python files recursively."
        )

    @property
    def parameters(self) -> type:
        return GlobParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = GlobParameters(**args)
        pattern = parsed.pattern
        decision = resolve_workspace_io_path(
            parsed.path or ".",
            context,
            tool_id=self.id,
            operation="glob",
        )
        if decision.blocked:
            return ToolResult(
                title=f"Glob: {pattern}",
                output="",
                error=decision.reason,
                metadata=decision.metadata,
            )
        from ...runtime.sandbox.base import file_isolation_enabled

        if file_isolation_enabled():
            from ..sandbox_file_io import run_sandbox_file_io

            return await run_sandbox_file_io(
                operation="glob", decision=decision, context=context,
                arguments={"pattern": pattern}, title=f"Glob: {pattern}",
            )
        path = decision.host_path

        try:
            full_pattern = str(path / pattern)
            matches = glob_module.glob(full_pattern, recursive=True)
            matches = [
                match
                for match in sorted(matches)
                if workspace_platform_path_access(match, context) != "hidden"
            ]
            display_matches = [
                display_path_for_host_path(match, context)
                for match in matches
            ]

            return ToolResult(
                title=f"Glob: {pattern}",
                output="\n".join(display_matches),
                metadata={"count": len(matches), **decision.metadata},
            )
        except Exception as e:
            return ToolResult(
                title=f"Glob: {pattern}",
                output="",
                error=str(e),
                metadata=decision.metadata,
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
