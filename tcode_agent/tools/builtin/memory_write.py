"""MemoryWrite tool - persist important memories across sessions."""

from typing import Any

from ...memory.scoped import resolve_memory_scope, write_memory_scope
from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult


class MemoryWriteParameters(ParameterSchema):
    content: str
    tags: list[str] | None = None
    scope: str | None = "auto"


class MemoryWriteTool(Tool):
    @property
    def id(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return """Store a memory in the appropriate namespace.
Use global only for durable user/project preferences or explicit long-term decisions.

Parameters:
- content: memory text to store
- tags: optional list of tags (e.g. [\"preference\", \"project\"])
- scope: auto | global | team | worker | session

Default scope:
- primary -> global
- manager -> team
- worker / helper -> worker
- subagent -> session"""

    @property
    def parameters(self) -> type:
        return MemoryWriteParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = MemoryWriteParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        try:
            resolved = await resolve_memory_scope(
                context,
                scope=parsed.scope,
                for_write=True,
            )
        except ValueError as exc:
            return ToolResult(title="Memory write", output="", error=str(exc))

        entry = write_memory_scope(
            resolved,
            workdir=workdir,
            content=parsed.content,
            tags=parsed.tags,
            source=context.agent,
        )

        return ToolResult(
            title=f"Memory saved [{resolved.resolved_scope}]",
            output=f"Saved {resolved.resolved_scope} memory {entry.id}: {entry.content}",
            metadata={
                "id": entry.id,
                "content": entry.content,
                "tags": entry.tags,
                "created_at": entry.created_at,
                "updated_at": getattr(entry, "updated_at", ""),
                "source": getattr(entry, "source", ""),
                "scope": resolved.resolved_scope,
                "requested_scope": resolved.requested_scope,
                "scope_key": resolved.scope_key,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
