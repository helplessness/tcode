"""Write tool - Write content to a file"""

from typing import Any

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ..path_policy import resolve_workspace_io_path
from ...types import ToolResult


class WriteParameters(ParameterSchema):
    """Parameters for write tool"""

    content: str
    filePath: str


class WriteTool(Tool):
    """Write content to a file"""

    @property
    def id(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return """Write content to a file. Creates the file if it doesn't exist, or overwrites existing content.

Use this for:
- Creating new files
- Writing complete file content
- Overwriting files when needed

IMPORTANT:
- Use relative paths: "src/main.py" NOT "workdir/src/main.py"
- In sandbox mode, writes must stay inside the active workspace; use /workspace/... for sandbox-visible files
- Parent directories are created automatically
- For editing existing files, prefer the Edit tool
- NEVER use for file operations that should use bash (cat <<EOF, echo >)

Code Style:
- Do NOT add comments unless explicitly requested
- Follow existing code conventions in the project
- Use appropriate file extensions (.py, .ts, .js, .md, etc.)

Examples:
✓ Create Python module: filePath="src/utils.py"
✓ Create config: filePath="config.json"
✓ Create documentation: filePath="docs/guide.md"
✗ Don't use for: appending to logs, modifying existing code (use edit instead)
"""

    @property
    def parameters(self) -> type:
        return WriteParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WriteParameters(**args)
        decision = resolve_workspace_io_path(
            parsed.filePath,
            context,
            tool_id=self.id,
            operation="write",
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
            parsed.content, repairs = repair_knowledge_delimiters(parsed.content, context.messages)
            if repairs:
                decision.metadata['citation_format_repairs'] = repairs
        from ...runtime.sandbox.base import file_isolation_enabled

        if file_isolation_enabled():
            from ..sandbox_file_io import run_sandbox_file_io

            return await run_sandbox_file_io(
                operation="write",
                decision=decision,
                context=context,
                arguments={"content": parsed.content},
            )
        filepath = decision.host_path

        try:
            parent_dir = filepath.parent
            if parent_dir:
                parent_dir.mkdir(parents=True, exist_ok=True)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(parsed.content)

            return ToolResult(
                title=decision.display_path,
                output=f"File written successfully: {decision.display_path}",
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
