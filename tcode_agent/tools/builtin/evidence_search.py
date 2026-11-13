"""EvidenceSearch tool - retrieve cited evidence records."""

from typing import Any
import asyncio

from ...evidence import EvidenceStore, format_evidence_records
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class EvidenceSearchParameters(ParameterSchema):
    query: str | None = ""
    limit: int | None = 10
    kind: str | None = ""
    tags: list[str] | None = None
    entities: list[str] | None = None
    source_type: str | None = ""
    stage_id: str | None = ""
    workflow_run_id: str | None = ""


class EvidenceSearchTool(Tool):
    @property
    def id(self) -> str:
        return "evidence_search"

    @property
    def description(self) -> str:
        return """Search workspace evidence records and return citation markers.

Use before synthesis, review, or workflow decisions when claims need traceable
support. Results include [evidence:<id>] markers that can be cited directly.
In user-facing answers use source titles and page locators, keeping the citation
marker for the View source button. Do not spell out internal IDs or tool names."""

    @property
    def parameters(self) -> type:
        return EvidenceSearchParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = EvidenceSearchParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        records = await asyncio.to_thread(EvidenceStore.search,
            query=parsed.query or "",
            workdir=workdir,
            limit=parsed.limit or 10,
            kind=parsed.kind or "",
            tags=parsed.tags or [],
            entities=parsed.entities or [],
            source_type=parsed.source_type or "",
            stage_id=parsed.stage_id or "",
            workflow_run_id=parsed.workflow_run_id or "",
        )
        from ...evidence.maintenance import public_status
        retrieval = await asyncio.to_thread(public_status, EvidenceStore._resolve_workdir(workdir))
        notice = '' if retrieval['semantic'] == 'ready' else '检索状态：语义检索未就绪或已降级，不能把未命中视为全库不存在相关证据。\n'
        if not retrieval['lexical_ready']:
            notice += '历史证据索引准备中，检索覆盖尚不完整。\n'
        if not records:
            return ToolResult(
                title="Evidence search",
                output=notice + "No evidence records found.",
                metadata={"count": 0, "items": [], 'retrieval': retrieval},
            )
        return ToolResult(
            title="Evidence search",
            output=notice + format_evidence_records(records),
            metadata={
                "count": len(records),
                'retrieval': retrieval,
                "items": [
                    {
                        "evidence_id": item.evidence_id,
                        "citation_marker": f"[evidence:{item.evidence_id}]",
                        "kind": item.kind,
                        "title": item.title,
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "source_path": item.source_path,
                        "locator": item.locator,
                        "claims": item.claims,
                        "entities": item.entities,
                        "tags": item.tags,
                        "confidence": item.confidence,
                        "workflow_run_id": item.workflow_run_id,
                        "stage_id": item.stage_id,
                    }
                    for item in records
                ],
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
