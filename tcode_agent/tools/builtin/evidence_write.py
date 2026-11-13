"""EvidenceWrite tool - persist cited evidence for workflows and reports."""

from typing import Any

from ...evidence import EvidenceStore, build_evidence_record
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class EvidenceWriteParameters(ParameterSchema):
    content: str
    kind: str | None = "note"
    title: str | None = ""
    source_type: str | None = ""
    source_id: str | None = ""
    source_path: str | None = ""
    locator: str | None = ""
    claims: list[str] | None = None
    entities: list[str] | None = None
    citations: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    confidence: float | None = 0.0
    workflow_run_id: str | None = ""
    stage_id: str | None = ""
    artifact_refs: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class EvidenceWriteTool(Tool):
    @property
    def id(self) -> str:
        return "evidence_write"

    @property
    def description(self) -> str:
        return """Store a reusable evidence record with a stable citation marker.

Use this for paper facts, table rows, figure observations, workflow artifacts,
MCP job outputs, and review findings that downstream stages should cite.
The returned marker has the form [evidence:<id>] and can be used in reports or
workflow validation."""

    @property
    def parameters(self) -> type:
        return EvidenceWriteParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = EvidenceWriteParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        record = build_evidence_record(
            content=parsed.content,
            kind=parsed.kind or "note",
            title=parsed.title or "",
            source_type=parsed.source_type or "",
            source_id=parsed.source_id or "",
            source_path=parsed.source_path or "",
            locator=parsed.locator or "",
            claims=parsed.claims or [],
            entities=parsed.entities or [],
            citations=parsed.citations or [],
            tags=parsed.tags or [],
            confidence=parsed.confidence or 0.0,
            workflow_run_id=parsed.workflow_run_id or "",
            stage_id=parsed.stage_id or "",
            artifact_refs=parsed.artifact_refs or {},
            source_agent=context.agent,
            metadata=parsed.metadata or {},
            workdir=workdir,
        )
        saved = EvidenceStore.add(record, workdir=workdir)
        marker = f"[evidence:{saved.evidence_id}]"
        return ToolResult(
            title="Evidence saved",
            output=f"Saved evidence {saved.evidence_id}. Cite it as {marker}",
            metadata={
                "evidence_id": saved.evidence_id,
                "citation_marker": marker,
                "kind": saved.kind,
                "title": saved.title,
                "source_type": saved.source_type,
                "source_id": saved.source_id,
                "source_path": saved.source_path,
                "locator": saved.locator,
                "claims": saved.claims,
                "entities": saved.entities,
                "tags": saved.tags,
                "confidence": saved.confidence,
                "workflow_run_id": saved.workflow_run_id,
                "stage_id": saved.stage_id,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
