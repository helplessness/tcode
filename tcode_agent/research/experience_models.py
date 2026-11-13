"""Persistent, revisioned research experience projection models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


RESEARCH_EXPERIENCE_SCHEMA_VERSION = 1
RESEARCH_EXPERIENCE_KINDS = {
    "attempt",
    "result",
    "decision",
    "failed_path",
    "method",
    "lesson",
    "limitation",
    "next_action",
}
RESEARCH_EXPERIENCE_PROVENANCE = {
    "observed",
    "user_stated",
    "verified",
    "inferred",
    "imported",
}
RESEARCH_EXPERIENCE_STATES = {"active", "superseded", "removed"}
RESEARCH_EXPERIENCE_SUMMARY_STATES = {"complete", "incomplete"}
RESEARCH_EXPERIENCE_ROUTING_STATES = {
    "resolved",
    "unresolved",
    "unassigned",
    "not_research",
}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except Exception:
            value = []
    return list(value) if isinstance(value, list) else []


def _strings(value: Any, *, limit: int = 5000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in _json_list(value)
            if str(item or "").strip()
        )
    )[: max(0, limit)]


@dataclass(frozen=True)
class ResearchExperienceItem:
    experience_id: str
    experience_key: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    kind: str
    title: str
    summary: str
    summary_state: str
    durability: str
    outcome_status: str
    routing_state: str
    source_research_ids: list[str]
    source_activity_ids: list[str]
    source_episode_ids: list[str]
    source_event_ids: list[str]
    artifact_refs: list[dict[str, Any]]
    evidence_ids: list[str]
    origin_pack_ids: list[str]
    provenance: str
    occurred_at: str
    revision: int
    state: str
    source_digest: str
    supersedes_experience_id: str
    superseded_by: str
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchExperienceItem":
        value = dict(row)
        for key in (
            "source_research_ids",
            "source_activity_ids",
            "source_episode_ids",
            "source_event_ids",
            "evidence_ids",
            "origin_pack_ids",
        ):
            value[key] = _strings(value.pop(f"{key}_json", "[]"))
        value["artifact_refs"] = [
            dict(item)
            for item in _json_list(value.pop("artifact_refs_json", "[]"))
            if isinstance(item, dict)
        ][:1000]
        value["schema_version"] = int(value.get("schema_version") or 0)
        value["revision"] = int(value.get("revision") or 0)
        return cls(**value)


__all__ = [
    "RESEARCH_EXPERIENCE_KINDS",
    "RESEARCH_EXPERIENCE_PROVENANCE",
    "RESEARCH_EXPERIENCE_ROUTING_STATES",
    "RESEARCH_EXPERIENCE_SCHEMA_VERSION",
    "RESEARCH_EXPERIENCE_STATES",
    "RESEARCH_EXPERIENCE_SUMMARY_STATES",
    "ResearchExperienceItem",
]
