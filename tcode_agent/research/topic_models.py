"""Versioned Topic Candidate and Research Project projections."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import canonical_json, sha256_text


TOPIC_SCHEMA_VERSION = 1
TOPIC_DECISION_VERSION = "topic-candidate-rule-3"
TOPIC_STATES = {"emerging", "stable", "merged", "split", "dismissed", "confirmed"}
PROJECT_STATUSES = {"active", "paused", "archived"}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except Exception:
            value = []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


@dataclass(frozen=True)
class ResearchTopicCandidate:
    candidate_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    suggested_title: str
    title_source: str
    goal_summary: str
    keywords: list[str]
    supporting_event_count: int
    session_count: int
    artifact_count: int
    tool_count: int
    explicit_goal: bool
    stability_score: float
    state: str
    first_seen_at: str
    last_seen_at: str
    merged_into: str
    split_from: list[str]
    confirmed_research_id: str
    generation: int
    decision_version: str
    created_at: str
    updated_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchTopicCandidate":
        values = dict(row)
        values["keywords"] = _json_list(values.pop("keywords_json", "[]"))
        values["split_from"] = _json_list(values.pop("split_from_json", "[]"))
        values["explicit_goal"] = bool(values.get("explicit_goal"))
        values["stability_score"] = float(values.get("stability_score") or 0.0)
        for key in (
            "supporting_event_count",
            "session_count",
            "artifact_count",
            "tool_count",
            "generation",
            "schema_version",
        ):
            values[key] = int(values.get(key) or 0)
        return cls(**values)


def research_candidate_digest(candidate: ResearchTopicCandidate) -> str:
    """Return the optimistic-concurrency token for a candidate review snapshot."""

    return sha256_text(
        canonical_json(
            {
                "candidate_id": candidate.candidate_id,
                "suggested_title": candidate.suggested_title,
                "title_source": candidate.title_source,
                "goal_summary": candidate.goal_summary,
                "keywords": candidate.keywords,
                "supporting_event_count": candidate.supporting_event_count,
                "session_count": candidate.session_count,
                "artifact_count": candidate.artifact_count,
                "tool_count": candidate.tool_count,
                "explicit_goal": candidate.explicit_goal,
                "stability_score": candidate.stability_score,
                "state": candidate.state,
                "last_seen_at": candidate.last_seen_at,
                "confirmed_research_id": candidate.confirmed_research_id,
                "generation": candidate.generation,
            }
        )
    )


@dataclass(frozen=True)
class ResearchCandidateMembership:
    membership_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    event_id: str
    candidate_id: str
    state: str
    decision_source: str
    confidence: float
    reason_code: str
    operation_id: str
    revision: int
    semantic_fingerprint: str
    idempotency_key: str
    decided_at: str
    superseded_by: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchCandidateMembership":
        values = dict(row)
        values["schema_version"] = int(values.get("schema_version") or 0)
        values["revision"] = int(values.get("revision") or 0)
        values["confidence"] = float(values.get("confidence") or 0.0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchProject:
    research_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    title: str
    description: str
    status: str
    origin_candidate_ids: list[str]
    created_at: str
    updated_at: str
    archived_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchProject":
        values = dict(row)
        values["origin_candidate_ids"] = _json_list(
            values.pop("origin_candidate_ids_json", "[]")
        )
        values["schema_version"] = int(values.get("schema_version") or 0)
        return cls(**values)


@dataclass
class TopicRunReport:
    run_id: str
    status: str = "completed"
    event_count: int = 0
    group_count: int = 0
    candidate_created_count: int = 0
    membership_changed_count: int = 0
    merge_suggestion_count: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
