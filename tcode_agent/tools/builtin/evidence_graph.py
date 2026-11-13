"""EvidenceGraph tool - inspect relationships between evidence records."""

from typing import Any
import asyncio

from ...evidence import EvidenceStore, format_evidence_graph
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class EvidenceGraphParameters(ParameterSchema):
    evidence_id: str | None = ""
    query: str | None = ""
    depth: int | None = 1
    limit: int | None = 60
    persist: bool | None = True
    include_semantic_hints: bool | None = False


class EvidenceGraphTool(Tool):
    @property
    def id(self) -> str:
        return "evidence_graph"

    @property
    def description(self) -> str:
        return """Build or inspect the workspace evidence provenance graph.

Use this to see how evidence records connect to sources, anchors, workflow
stages, and artifacts. Paper Wiki owns claims and semantic relationships;
include_semantic_hints is a compatibility/debug option only."""

    @property
    def parameters(self) -> type:
        return EvidenceGraphParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = EvidenceGraphParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        if parsed.evidence_id:
            graph = await asyncio.to_thread(EvidenceStore.graph_neighborhood,
                parsed.evidence_id,
                workdir=workdir,
                depth=parsed.depth or 1,
                limit=parsed.limit or 60,
                include_semantic_hints=bool(parsed.include_semantic_hints),
            )
        else:
            graph = await asyncio.to_thread(EvidenceStore.build_graph,
                workdir=workdir,
                persist=bool(parsed.persist),
                include_semantic_hints=bool(parsed.include_semantic_hints),
            )
            query = str(parsed.query or "").strip().lower()
            if query:
                matching_nodes = [
                    node
                    for node in list(graph.get("nodes") or [])
                    if query in str(node.get("label") or "").lower()
                    or query in str(node.get("type") or "").lower()
                ][: max(1, min(100, int(parsed.limit or 60)))]
                node_ids = {str(node.get("node_id") or "") for node in matching_nodes}
                matching_edges = [
                    edge
                    for edge in list(graph.get("edges") or [])
                    if str(edge.get("source") or "") in node_ids
                    or str(edge.get("target") or "") in node_ids
                ][: max(1, min(100, int(parsed.limit or 60)))]
                connected_ids = node_ids | {
                    str(edge.get("source") or "") for edge in matching_edges
                } | {
                    str(edge.get("target") or "") for edge in matching_edges
                }
                nodes = [
                    node
                    for node in list(graph.get("nodes") or [])
                    if str(node.get("node_id") or "") in connected_ids
                ]
                graph = {
                    **graph,
                    "node_count": len(nodes),
                    "edge_count": len(matching_edges),
                    "nodes": nodes,
                    "edges": matching_edges,
                    "query": query,
                }

        return ToolResult(
            title="Evidence graph",
            output=format_evidence_graph(graph),
            metadata=graph,
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
