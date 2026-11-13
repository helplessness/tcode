"""Versioned links between research state and durable knowledge objects."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_SUBJECT_TYPES = {"project", "episode", "finding", "conflict", "wiki_claim", "hypothesis"}
KNOWLEDGE_OBJECT_TYPES = {
    "paper",
    "passage",
    "evidence",
    "wiki_page",
    "wiki_claim",
    "finding",
}
KNOWLEDGE_RELATIONS = {
    "references",
    "supports",
    "contradicts",
    "context_only",
    "corresponds_to",
    "challenges",
    "refines",
    "supersedes",
}
KNOWLEDGE_SIDES = {"neutral", "original", "challenging", "resolution"}
KNOWLEDGE_STATES = {"active", "stale", "missing", "retracted", "conflicting", "removed"}


def _json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if isinstance(value, type(default)) else default
    try:
        parsed = json.loads(value or "")
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


@dataclass(frozen=True)
class ResearchKnowledgeLink:
    link_id: str
    link_key: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    subject_type: str
    subject_id: str
    object_type: str
    object_id: str
    relation: str
    side: str
    state: str
    source_revision: int
    source_hash: str
    metadata: dict[str, Any]
    discovered_by: str
    revision: int
    created_at: str
    updated_at: str
    supersedes_link_id: str
    superseded_by: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchKnowledgeLink":
        values = dict(row)
        values["metadata"] = _json(values.pop("metadata_json", "{}"), {})
        for key in ("schema_version", "source_revision", "revision"):
            values[key] = int(values.get(key) or 0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchKnowledgeLintReport:
    run_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    status: str
    export_ready: bool
    checked_link_count: int
    counts: dict[str, int]
    issues: list[dict[str, Any]]
    source_digest: str
    started_at: str
    completed_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchKnowledgeLintReport":
        values = dict(row)
        values["export_ready"] = bool(values.get("export_ready"))
        values["checked_link_count"] = int(values.get("checked_link_count") or 0)
        values["counts"] = _json(values.pop("counts_json", "{}"), {})
        values["issues"] = _json(values.pop("issues_json", "[]"), [])
        return cls(**values)
