"""Grep tool - Search for patterns in files"""

import os
import re
from typing import Any

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ..path_policy import (
    display_path_for_host_path,
    resolve_workspace_io_path,
    workspace_platform_path_access,
)
from ...types import ToolResult


class GrepParameters(ParameterSchema):
    """Parameters for grep tool"""

    pattern: str = pydantic.Field(description="Literal, case-sensitive substring; not a regular expression. Use HETATM, not ^(HETATM|TER).")
    path: str | None = None
    include: str | None = None


class GrepTool(Tool):
    """Search for patterns in files"""

    @property
    def id(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for literal, case-sensitive text in files (not regular expressions). Returns matching lines with "
            "file path and line number. path accepts a single file or a directory. "
            "Text matches are not full structured-data analysis; use a discovered domain analyzer for large scoring tables."
        )

    @property
    def parameters(self) -> type:
        return GrepParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = GrepParameters(**args)
        pattern = parsed.pattern
        decision = resolve_workspace_io_path(
            parsed.path or ".",
            context,
            tool_id=self.id,
            operation="grep",
        )
        if decision.blocked:
            return ToolResult(
                title=f"Grep: {pattern}",
                output="",
                error=decision.reason,
                metadata=decision.metadata,
            )
        from ...runtime.sandbox.base import file_isolation_enabled

        if file_isolation_enabled():
            from ..sandbox_file_io import run_sandbox_file_io

            return await run_sandbox_file_io(
                operation="grep", decision=decision, context=context,
                arguments={"pattern": pattern, "include": parsed.include or "*"},
                title=f"Grep: {pattern}",
            )
        path = decision.host_path
        include = parsed.include or "*"

        results = []
        errors, scanned = [], 0
        include_pattern = include.replace("*", ".*")

        try:
            if not os.path.exists(path):
                raise FileNotFoundError("search path does not exist")
            entries = [(os.path.dirname(path), [], [os.path.basename(path)])] if os.path.isfile(path) else os.walk(path, onerror=lambda exc: errors.append(str(exc)))
            for root, dirs, files in entries:
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in {".git", "node_modules", "__pycache__", ".venv"}
                    and workspace_platform_path_access(
                        os.path.join(root, d),
                        context,
                    )
                    != "hidden"
                ]

                for file in files:
                    if not re.match(include_pattern, file):
                        continue

                    filepath = os.path.join(root, file)
                    if workspace_platform_path_access(filepath, context) == "hidden":
                        continue
                    try:
                        with open(
                            filepath, "r", encoding="utf-8", errors="ignore"
                        ) as f:
                            scanned += 1
                            for i, line in enumerate(f, 1):
                                if pattern in line:
                                    display_path = display_path_for_host_path(
                                        filepath,
                                        context,
                                    )
                                    results.append(f"{display_path}:{i}: {line.rstrip()}")
                                    if len(results) >= 1000:
                                        break
                    except OSError as exc:
                        if len(errors) < 20:
                            errors.append(str(exc))

                    if len(results) >= 1000:
                        break
                if len(results) >= 1000:
                    break

            output = "\n".join(results) or "No literal substring matches. Regex syntax is not interpreted; this does not establish absence of structured records."
            truncated = len(results) >= 1000 or len(output) > 128000
            return ToolResult(
                title=f"Grep: {pattern}",
                output=output[:128000],
                error=("search incomplete: " + "; ".join(errors)) if errors else None,
                metadata={"count": len(results), "scanned_files": scanned, "complete": not errors and not truncated,
                          "truncated": truncated, **decision.metadata},
            )
        except Exception as e:
            return ToolResult(
                title=f"Grep: {pattern}",
                output="",
                error=str(e),
                metadata=decision.metadata,
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
