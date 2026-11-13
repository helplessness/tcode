"""Host-owned ontology retrieval for ordinary agents."""

import json
from typing import Any, Literal
from pydantic import Field

from ...ontology_host import read_capability
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class OntologyQueryParameters(ParameterSchema):
    operation: Literal['query', 'check_claims'] = 'query'
    claims: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=100)
    text: str | None = ""
    entity_id: str | None = ""
    predicate: str | None = ""
    scope: Literal["public", "private", "both"] = "both"
    evidence_basis: Literal['', 'experimental', 'computational', 'literature_report', 'hypothesis', 'unspecified'] = ''
    polarity: Literal['', 'supports', 'opposes'] = ''
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    after: str = ''
    limit: int = Field(default=20, ge=1, le=100)


class OntologyQueryTool(Tool):
    @property
    def id(self) -> str:
        return "ontology_query"

    @property
    def description(self) -> str:
        return (
            "Search ontology facts visible to the current user: the read-only system "
            "ontology plus that user's current workspace ontology. Use this before "
            "answering questions about known entities, relations, papers, or their evidence. "
            "Search using a short entity name or identifier, not the whole question. "
            "Translate the user's question into an entity and explicit predicate, evidence_basis, polarity "
            "and exact qualifiers filters (for example {\"pH\":7}); never invent conditions or silently drop them. "
            "When requires_selection is true, compare candidate names and types, then query the appropriate "
            "entity_id; ask the user if identity remains ambiguous. Do not merge candidates. "
            "Use next_cursor as after with unchanged filters to read further pages. Unknown evidence basis "
            "is unspecified, not experimental. Qualifiers are exact typed values, not numeric ranges. "
            "Results include evidence quotes, source locators and polarity; cite these "
            "with source_id and locator. These published evidence snapshots do not require "
            "a plugin run_id or plugin evidence tool. Do not infer unsupported variant details, "
            "conditions or causal roles; absence is unknown, not false."
            " Before presenting a conclusion, use operation=check_claims with claims: "
            "[{text,assertion_ids,statement:{subject_id,predicate,object_id,polarity,qualifiers,kind},required_evidence_basis}]. "
            "statement.kind is reported or hypothesis; required_evidence_basis is any, experimental, computational, "
            "literature_report or hypothesis. Conditions exclude evidence_basis/primary_reference. "
            "The host checks exact identity, conditions, evidence type and opposing facts within enabled sources. "
            "Respect supported/partial_support/conflict/undetermined assessments and all reasons. "
            "Supported applies only to the declared structured statement; prose consistency and scientific "
            "truth are NOT verified. Do not promote computational results, analogies or hypotheses to experiments."
        )

    @property
    def parameters(self) -> type:
        return OntologyQueryParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = OntologyQueryParameters(**args)
        extra = context.extra or {}
        workdir = str(extra.get("workdir") or "")
        capability = read_capability(workdir)
        if capability is None:
            return ToolResult(
                title="Ontology query",
                output="No ontology is available for the current managed workspace.",
                metadata={"items": [], "available": False},
            )
        try:
            if parsed.operation == 'check_claims':
                if not parsed.claims:
                    raise ValueError('check_claims requires claims with explicit assertion references')
                assessment = capability.validate_report(parsed.claims)
                return ToolResult(title='Ontology evidence support check',
                                  output=json.dumps(assessment, ensure_ascii=False), metadata=assessment)
            result = capability.query(
                text=parsed.text or "",
                entity_id=parsed.entity_id or "",
                predicate=parsed.predicate or "",
                scope=parsed.scope,
                limit=parsed.limit,
                after=parsed.after,
                evidence_basis=parsed.evidence_basis,
                polarity=parsed.polarity,
                qualifiers=parsed.qualifiers,
            )
        finally:
            capability.close()
        items = [
            {**item, "scope": scope}
            for scope in ("public", "private")
            for item in result.get(scope, [])
        ]
        if result.get('unavailable_reason') == 'no_enabled_ontology_sources':
            return ToolResult(
                title="Ontology query",
                output="No ontology knowledge source is enabled for this workspace. Enable the public or private ontology in 本体知识 before requesting ontology evidence.",
                metadata={**result, "items": [], "available": False},
            )
        # Put the scientific statement before opaque IDs in bounded prompt previews.
        # Keep all snapshot/provenance fields; this is presentation, not a new fact.
        visible_items = [
            {**{key: item[key] for key in (
                "subject_label", "predicate", "object_label", "polarity", "kind",
                "qualifiers", "resolved_evidence_basis", "quote", "scope", "source_id", "locator",
            ) if key in item}, **item}
            for item in items
        ]
        return ToolResult(
            title="Ontology query",
            output=json.dumps({
                "facts": visible_items, "candidates": result.get('candidates', []),
                "requires_selection": result.get('requires_selection', False),
                "candidates_truncated": result.get('candidates_truncated', False),
                "match_type": result.get('match_type'), "next_cursor": result.get('next_cursor'),
                "applied_filters": result.get('applied_filters', {}),
                "message": ('Choose an entity_id before retrieving facts.' if result.get('requires_selection') else
                            '' if items else 'No matching ontology facts found. Absence is unknown, not evidence of falsity.'),
            }, ensure_ascii=False),
            metadata={**result, "items": items, "available": True},
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry
        ToolRegistry.register(cls())
