"""ResearchPack v2 unified scope and manifest contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .pack_models import RESEARCH_PACK_FORMAT
from .routing_models import RESEARCH_DURABILITIES, RESEARCH_ROUTING_STATES


RESEARCH_PACK_V2_SCHEMA_VERSION = "2.0.0"
RESEARCH_PACK_V2_SCOPE_MODES = {"project", "workspace_period"}


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))


@dataclass(frozen=True)
class ResearchPackV2Scope:
    mode: str
    research_ids: list[str] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""

    def __post_init__(self) -> None:
        mode = str(self.mode or "").strip()
        research_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in self.research_ids
                if str(item or "").strip()
            )
        )[:100]
        if mode not in RESEARCH_PACK_V2_SCOPE_MODES:
            raise ValueError("unsupported ResearchPack v2 scope mode")
        if mode == "project" and len(research_ids) != 1:
            raise ValueError("project scope requires exactly one research_id")
        if mode == "project" and bool(self.started_at) != bool(self.ended_at):
            raise ValueError("project scope time range requires both boundaries")
        if mode == "workspace_period":
            if not self.started_at or not self.ended_at:
                raise ValueError("workspace_period scope requires started_at and ended_at")
            try:
                started = _timestamp(self.started_at)
                ended = _timestamp(self.ended_at)
            except Exception as exc:
                raise ValueError("ResearchPack v2 scope timestamps are invalid") from exc
            if started >= ended:
                raise ValueError("ResearchPack v2 scope requires started_at < ended_at")
        elif self.started_at and self.ended_at:
            try:
                started = _timestamp(self.started_at)
                ended = _timestamp(self.ended_at)
            except Exception as exc:
                raise ValueError("ResearchPack v2 scope timestamps are invalid") from exc
            if started >= ended:
                raise ValueError("ResearchPack v2 scope requires started_at < ended_at")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "research_ids", research_ids)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_research_ids"] = value.pop("research_ids")
        return value


@dataclass(frozen=True)
class ResearchPackV2Activity:
    """Portable Activity view used by the v2 protocol.

    The live Activity projection is intentionally left unchanged until E3; this
    model freezes the cross-pack fields without switching the current read path.
    """

    activity_id: str
    title: str
    summary: str
    status: str
    source_research_ids: list[str] = field(default_factory=list)
    durability: str = "durable"
    routing_state: str = "unresolved"
    source_turn_ids: list[str] = field(default_factory=list)
    origin_pack_ids: list[str] = field(default_factory=list)
    continuation_of_ids: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    agent_calls: list[dict[str, Any]] = field(default_factory=list)
    workflow_calls: list[dict[str, Any]] = field(default_factory=list)
    agent_runs: list[dict[str, Any]] = field(default_factory=list)
    file_refs: list[dict[str, Any]] = field(default_factory=list)
    execution_graph_version: str = "agent-run-v1"
    included_as_dependency: bool = False
    included_reason: str = "scope_time"
    source_digest: str = ""
    started_at: str = ""
    ended_at: str = ""

    def __post_init__(self) -> None:
        if not str(self.activity_id or "").strip():
            raise ValueError("activity_id is required")
        if self.durability not in RESEARCH_DURABILITIES:
            raise ValueError("unsupported ResearchPack v2 Activity durability")
        if self.routing_state not in RESEARCH_ROUTING_STATES:
            raise ValueError("unsupported ResearchPack v2 Activity routing state")
        for field_name in (
            "source_research_ids",
            "source_turn_ids",
            "origin_pack_ids",
            "continuation_of_ids",
            "source_event_ids",
        ):
            normalized = list(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in getattr(self, field_name)
                    if str(item or "").strip()
                )
            )[:1000]
            object.__setattr__(self, field_name, normalized)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchPackV2Manifest:
    pack_id: str
    created_at: str
    scope: ResearchPackV2Scope
    researches: list[dict[str, Any]] = field(default_factory=list)
    lineage: dict[str, Any] = field(default_factory=dict)
    contents: dict[str, int] = field(default_factory=dict)
    format: str = RESEARCH_PACK_FORMAT
    schema_version: str = RESEARCH_PACK_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not str(self.pack_id or "").strip():
            raise ValueError("pack_id is required")
        if self.format != RESEARCH_PACK_FORMAT:
            raise ValueError("unsupported ResearchPack format")
        if self.schema_version != RESEARCH_PACK_V2_SCHEMA_VERSION:
            raise ValueError("unsupported ResearchPack v2 schema version")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["scope"] = self.scope.to_dict()
        return value


__all__ = [
    "RESEARCH_PACK_V2_SCHEMA_VERSION",
    "RESEARCH_PACK_V2_SCOPE_MODES",
    "ResearchPackV2Activity",
    "ResearchPackV2Manifest",
    "ResearchPackV2Scope",
]
