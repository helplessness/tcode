"""EvidenceDoctor tool - run health checks over cited evidence."""

from typing import Any
import asyncio

from ...evidence import EvidenceStore, format_evidence_doctor_report
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class EvidenceDoctorParameters(ParameterSchema):
    evidence_id: str | None = ""
    text: str | None = ""
    persist: bool | None = True
    include_semantic_hints: bool | None = False


class EvidenceDoctorTool(Tool):
    @property
    def id(self) -> str:
        return "evidence_doctor"

    @property
    def description(self) -> str:
        return """Check evidence health and citation integrity.

Use this to find missing sources and locators, unknown evidence refs, invalid
content hashes, inactive evidence, duplicate records, and broken provenance.
Paper Wiki lint owns claim support and semantic conflict checks."""

    @property
    def parameters(self) -> type:
        return EvidenceDoctorParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = EvidenceDoctorParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        report = await asyncio.to_thread(EvidenceStore.doctor,
            workdir=workdir,
            evidence_id=parsed.evidence_id or "",
            text=parsed.text or "",
            persist=bool(parsed.persist),
            include_semantic_hints=bool(parsed.include_semantic_hints),
        )
        return ToolResult(
            title="Evidence doctor",
            output=format_evidence_doctor_report(report),
            metadata=report,
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
