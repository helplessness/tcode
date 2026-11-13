"""MemoryRead tool - retrieve persisted memories."""

from typing import Any

from ...memory.scoped import query_memory_scope, resolve_memory_scope
from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult


class MemoryReadParameters(ParameterSchema):
    query: str | None = None
    limit: int | None = 20
    level: str | None = "l1"
    scope: str | None = "auto"
    expand: list[str] | None = None  # memory IDs to expand to L2 detail
    filters: dict[str, Any] | None = None


class MemoryReadTool(Tool):
    @property
    def id(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return """Read scoped memories saved from previous turns/sessions.

Parameters:
- query: optional keyword filter
- limit: max memories to return (default: 20)
- scope: auto | global | team | worker | session
- filters: structured field filters using eq/contains/in/gt/gte/lt/lte and all/any/not

Default scope:
- primary -> global
- manager -> team
- worker / helper -> worker
- subagent -> session"""

    @property
    def parameters(self) -> type:
        return MemoryReadParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = MemoryReadParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        try:
            resolved = await resolve_memory_scope(
                context,
                scope=parsed.scope,
                for_write=False,
            )
        except ValueError as exc:
            return ToolResult(title="Memory read", output="", error=str(exc))

        try:
            items = query_memory_scope(
                resolved,
                workdir=workdir,
                query=parsed.query,
                limit=parsed.limit or 20,
                level=parsed.level or "l1",
                filters=parsed.filters,
            )
        except ValueError as exc:
            return ToolResult(title="Memory read", output="", error=str(exc))

        expand_ids: set[str] = set()
        if parsed.expand:
            expand_ids = {str(e).strip() for e in parsed.expand if str(e).strip()}

        # Build retrieval trace for observability
        trace: dict[str, Any] = {
            "scope": resolved.resolved_scope,
            "requested_scope": resolved.requested_scope,
            "level": parsed.level or "l1",
            "query": parsed.query or "",
            "count": len(items),
            "filters": parsed.filters or {},
            "embedding_provider": "unknown",
        }
        try:
            from ...memory.embedding_provider import EmbeddingProvider
            trace["embedding_provider"] = "enabled" if EmbeddingProvider.is_enabled(workdir) else "hash_fallback"
        except Exception:
            pass
        try:
            from ...memory.vector_store import FaissVectorStore
            trace["vector_store"] = "faiss" if FaissVectorStore.available() else "unavailable"
        except Exception:
            trace["vector_store"] = "unavailable"

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
                    "trace": trace,
                },
            )

        lines = []
        item_metas = []
        for i, item in enumerate(items, 1):
            item_id = getattr(item, "id", "")
            tags = f" [tags: {', '.join(item.tags)}]" if item.tags else ""
            source = str(getattr(item, "source", "") or "").strip()
            source_txt = f" [{source}]" if source else ""
            lines.append(f"{i}. ({item_id}) {item.content}{tags}{source_txt}")

            meta = {
                "id": item_id,
                "content": item.content,
                "tags": item.tags,
                "created_at": getattr(item, "created_at", ""),
                "updated_at": getattr(item, "updated_at", ""),
                "source": getattr(item, "source", ""),
                "task_type": getattr(item, "task_type", ""),
                "goal": getattr(item, "goal", ""),
                "confidence": getattr(item, "confidence", 0.0),
                "toolchain": list(getattr(item, "toolchain", []) or []),
            }

            # Expand requested ids to L2 detail
            if item_id in expand_ids:
                try:
                    l2_content = getattr(item, "l2_detail", "") or ""
                    if l2_content:
                        lines.append(f"   └─ [L2] {l2_content[:500]}")
                        meta["l2_detail"] = l2_content
                except Exception:
                    pass

            item_metas.append(meta)

        return ToolResult(
            title=f"Memory read [{resolved.resolved_scope}]",
            output="\n".join(lines),
            metadata={
                "count": len(items),
                "scope": resolved.resolved_scope,
                "requested_scope": resolved.requested_scope,
                "scope_key": resolved.scope_key,
                "items": item_metas,
                "trace": trace,
                "expanded_ids": list(expand_ids),
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
