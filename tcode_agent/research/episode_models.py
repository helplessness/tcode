"""Versioned Episode, Checkpoint, Finding, and Conflict projections."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


DERIVATION_SCHEMA_VERSION = 1
EPISODE_DERIVATION_VERSION = "episode-readable-3"
CHECKPOINT_GENERATOR_VERSION = "checkpoint-readable-3"

EPISODE_STATES = {"active", "stale", "superseded"}
EPISODE_OUTCOMES = {"completed", "partial", "failed", "abandoned", "unknown"}
CHECKPOINT_STATES = {"active", "stale", "superseded"}
FINDING_STATUSES = {
    "active",
    "disputed",
    "superseded",
    "retracted",
    "context_limited",
}
PROVENANCE_TYPES = {"observed", "user_stated", "verified", "inferred"}
CONFLICT_TYPES = {
    "direct_contradiction",
    "reproduction_failure",
    "scope_mismatch",
    "methodology_difference",
    "evidence_invalidated",
    "data_version_drift",
    "tool_version_drift",
    "interpretation_dispute",
}
CONFLICT_STATUSES = {"open", "under_review", "resolved"}
RESOLUTION_RESULTS = {
    "original_upheld",
    "challenger_upheld",
    "context_dependent",
    "inconclusive",
    "invalidated",
}


def _json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if isinstance(value, type(default)) else default
    try:
        parsed = json.loads(value or "")
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


@dataclass(frozen=True)
class ResearchEpisode:
    episode_id: str
    episode_key: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    revision: int
    title: str
    goal: dict[str, Any]
    summary: dict[str, Any]
    outcome_status: str
    event_ids: list[str]
    session_ids: list[str]
    trace_ids: list[str]
    artifact_refs: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    failed_paths: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    next_actions: list[dict[str, Any]]
    started_at: str
    ended_at: str
    boundary_reason: str
    derivation_version: str
    source_digest: str
    state: str
    supersedes_episode_id: str
    created_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchEpisode":
        values = dict(row)
        for key in ("goal", "summary"):
            values[key] = _json(values.pop(f"{key}_json", "{}"), {})
        for key in (
            "event_ids",
            "session_ids",
            "trace_ids",
            "artifact_refs",
            "tool_calls",
            "decisions",
            "failed_paths",
            "open_questions",
            "next_actions",
        ):
            values[key] = _json(values.pop(f"{key}_json", "[]"), [])
        values["schema_version"] = int(values.get("schema_version") or 0)
        values["revision"] = int(values.get("revision") or 0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchCheckpoint:
    checkpoint_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    revision: int
    research_goal: str
    current_state: str
    confirmed_facts: list[dict[str, Any]]
    working_hypotheses: list[dict[str, Any]]
    recent_progress: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    failed_paths: list[dict[str, Any]]
    known_conflicts: list[dict[str, Any]]
    important_artifacts: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    next_actions: list[dict[str, Any]]
    source_episode_ids: list[str]
    source_event_ids: list[str]
    generated_at: str
    generator_version: str
    source_digest: str
    state: str
    supersedes_checkpoint_id: str
    token_estimate: int
    budget_status: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchCheckpoint":
        values = dict(row)
        for key in (
            "confirmed_facts",
            "working_hypotheses",
            "recent_progress",
            "decisions",
            "failed_paths",
            "known_conflicts",
            "important_artifacts",
            "open_questions",
            "next_actions",
            "source_episode_ids",
            "source_event_ids",
        ):
            values[key] = _json(values.pop(f"{key}_json", "[]"), [])
        for key in ("schema_version", "revision", "token_estimate"):
            values[key] = int(values.get(key) or 0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchFinding:
    finding_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    episode_id: str
    statement: str
    scope: dict[str, Any]
    status: str
    provenance: str
    evidence_ids: list[str]
    source_event_ids: list[str]
    current_revision: int
    created_by: str
    created_at: str
    updated_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchFinding":
        values = dict(row)
        values["scope"] = _json(values.pop("scope_json", "{}"), {})
        values["evidence_ids"] = _json(values.pop("evidence_ids_json", "[]"), [])
        values["source_event_ids"] = _json(
            values.pop("source_event_ids_json", "[]"), []
        )
        values["schema_version"] = int(values.get("schema_version") or 0)
        values["current_revision"] = int(values.get("current_revision") or 0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchConflict:
    conflict_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    conflict_type: str
    status: str
    original_refs: list[str]
    challenging_refs: list[str]
    original_evidence_ids: list[str]
    challenging_evidence_ids: list[str]
    summary: str
    detected_by: str
    detected_at: str
    current_resolution_id: str
    updated_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchConflict":
        values = dict(row)
        for key in (
            "original_refs",
            "challenging_refs",
            "original_evidence_ids",
            "challenging_evidence_ids",
        ):
            values[key] = _json(values.pop(f"{key}_json", "[]"), [])
        values["schema_version"] = int(values.get("schema_version") or 0)
        return cls(**values)


@dataclass(frozen=True)
class ResearchResolution:
    resolution_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    conflict_id: str
    research_id: str
    revision: int
    result: str
    actor_id: str
    reason: str
    evidence_ids: list[str]
    scope: dict[str, Any]
    resolved_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchResolution":
        values = dict(row)
        values["evidence_ids"] = _json(values.pop("evidence_ids_json", "[]"), [])
        values["scope"] = _json(values.pop("scope_json", "{}"), {})
        for key in ("schema_version", "revision"):
            values[key] = int(values.get(key) or 0)
        return cls(**values)


@dataclass
class DerivationRunReport:
    run_id: str
    status: str = "completed"
    project_count: int = 0
    event_count: int = 0
    episode_created_count: int = 0
    episode_stale_count: int = 0
    checkpoint_created_count: int = 0
    affected_research_ids: list[str] = field(default_factory=list)
    affected_episode_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
