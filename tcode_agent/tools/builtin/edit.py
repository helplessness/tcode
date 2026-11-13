"""Edit tool - Edit a file with specific changes"""

from typing import Any

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ..path_policy import resolve_workspace_io_path
from ...types import ToolResult


class EditParameters(ParameterSchema):
    """Parameters for edit tool"""

    filePath: str
    oldString: str
    newString: str


class EditTool(Tool):
    """Edit a file with specific string replacement"""

    @property
    def id(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """Edit a file by replacing exact text.

Usage:
- Read the file FIRST using the Read tool
- Provide exact oldString to find (including whitespace/indentation)
- Provide newString to replace it with

Important:
- Use relative paths: "src/main.py" NOT "workdir/src/main.py"
- In sandbox mode, edits must stay inside the active workspace; use /workspace/... for sandbox-visible files
- Preserve exact indentation (tabs/spaces) from the file
- Include enough context to make oldString unique
- Include trailing newlines in the replacement if needed

Line Number Format:
When reading files, line numbers are prefixed as "1: content"
Do NOT include line numbers in oldString/newString - only the actual file content.

Examples:
✓ Correct: oldString="def hello():\n    return 'hi'"
✗ Wrong: oldString="1: def hello():"

If oldString appears multiple times:
- Include more surrounding context to make it unique
- Or use multiple edit calls for each occurrence

NEVER:
- Edit without reading first
- Guess at indentation
- Use line numbers in strings
"""

    @property
    def parameters(self) -> type:
        return EditParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = EditParameters(**args)
        decision = resolve_workspace_io_path(
            parsed.filePath,
            context,
            tool_id=self.id,
            operation="edit",
        )
        if decision.blocked:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=decision.reason,
                metadata=decision.metadata,
            )
        if decision.host_path.suffix.lower() in {'.md', '.txt'}:
            from ...research.citations import repair_knowledge_delimiters
            parsed.newString, repairs = repair_knowledge_delimiters(parsed.newString, context.messages)
            if repairs:
                decision.metadata['citation_format_repairs'] = repairs
        from ...runtime.sandbox.base import file_isolation_enabled

        if file_isolation_enabled():
            from ..sandbox_file_io import run_sandbox_file_io

            return await run_sandbox_file_io(
                operation="edit",
                decision=decision,
                context=context,
                arguments={"oldString": parsed.oldString, "newString": parsed.newString},
            )
        filepath = decision.host_path

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if not parsed.oldString:
                return ToolResult(
                    title=decision.display_path,
                    output="",
                    error="oldString must not be empty.",
                    metadata=decision.metadata,
                )

            match_count = content.count(parsed.oldString)
            if match_count == 0:
                return ToolResult(
                    title=decision.display_path,
                    output="",
                    error="oldString not found in file. Make sure the text matches exactly.",
                    metadata=decision.metadata,
                )
            if match_count > 1:
                return ToolResult(
                    title=decision.display_path,
                    output="",
                    error=(
                        "oldString appears multiple times in file. "
                        "Include more surrounding context to make it unique."
                    ),
                    metadata=decision.metadata,
                )

            new_content = content.replace(parsed.oldString, parsed.newString, 1)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)

            return ToolResult(
                title=decision.display_path,
                output=f"File edited successfully: {decision.display_path}",
                metadata=decision.metadata,
            )
        except FileNotFoundError:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=f"File not found: {decision.display_path}",
                metadata=decision.metadata,
            )
        except Exception as e:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=str(e),
                metadata=decision.metadata,
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
