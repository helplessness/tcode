"""Unified memory tool - read/write persistent memory."""

from typing import Any

from ...memory.scoped import query_memory_scope, resolve_memory_scope, write_memory_scope
from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult


class MemoryParameters(ParameterSchema):
    action: str
    content: str | None = None
    tags: list[str] | None = None
    query: str | None = None
    limit: int | None = 20
    level: str | None = "l1"
    scope: str | None = "auto"
    expand: list[str] | None = None  # memory IDs to expand to L2 detail


class MemoryTool(Tool):
    @property
    def id(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return """Read/write scoped memory with one tool.
Actions:
- read: query memories (query/limit)
- write: save memory (content/tags)

Parameters:
- scope: auto | global | team | worker | session"""

    @property
    def parameters(self) -> type:
        return MemoryParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = MemoryParameters(**args)
        action = str(parsed.action or "").strip().lower()
        workdir = context.extra.get("workdir") if context.extra else None

        if action == "read":
            try:
                resolved = await resolve_memory_scope(
                    context,
                    scope=parsed.scope,
                    for_write=False,
                )
            except ValueError as exc:
                return ToolResult(title="Memory read", output="", error=str(exc))
            items = query_memory_scope(
                resolved,
                workdir=workdir,
                query=parsed.query,
                limit=parsed.limit or 20,
                level=parsed.level or "l1",
            )
            expand_ids: set[str] = set()
            if parsed.expand:
                expand_ids = {str(e).strip() for e in parsed.expand if str(e).strip()}

            if not items:
                return ToolResult(
                    title=f"Memory read [{resolved.resolved_scope}]",
                    output="No memories found.",
                    metadata={
                        "count": 0,
                        "items": [],
                        "scope": resolved.resolved_scope,
                        "requested_scope": resolved.requested_scope,
                        "scope_key": resolved.scope_key,
                    },
                )
            lines = []
            for i, item in enumerate(items, 1):
                tags = f" [tags: {', '.join(item.tags)}]" if item.tags else ""
                source = str(getattr(item, "source", "") or "").strip()
                source_txt = f" [{source}]" if source else ""
                lines.append(f"{i}. ({item.id}) {item.content}{tags}{source_txt}")
                if item.id in expand_ids:
                    l2 = getattr(item, "l2_detail", "") or ""
                    if l2:
                        lines.append(f"   └─ [L2] {l2[:500]}")
            return ToolResult(
                title=f"Memory read [{resolved.resolved_scope}]",
                output="\n".join(lines),
                metadata={
                    "count": len(items),
                    "expanded_ids": list(expand_ids),
                    "scope": resolved.resolved_scope,
                    "requested_scope": resolved.requested_scope,
                    "scope_key": resolved.scope_key,
                    "items": [
                        {
                            "id": item.id,
                            "content": item.content,
                            "tags": item.tags,
                            "created_at": getattr(item, "created_at", ""),
                            "updated_at": getattr(item, "updated_at", ""),
                            "source": getattr(item, "source", ""),
                        }
                        for item in items
                    ],
                },
            )

        if action == "write":
            text = str(parsed.content or "").strip()
            if not text:
                return ToolResult(
                    title="Memory write",
                    output="",
                    error="content is required when action=write",
                )
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
                content=text,
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

        return ToolResult(
            title="Memory",
            output="",
            error=f"Unsupported action: {parsed.action}",
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
