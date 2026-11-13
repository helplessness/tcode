"""Durable turn routing and multi-project scope models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


RESEARCH_ROUTING_SCHEMA_VERSION = 1
RESEARCH_ROUTE_ENVELOPE_SCHEMA = "tcode.research-routing.v1"
RESEARCH_ROUTE_ENVELOPE_TAG = "tcode-research-route"
RESEARCH_DURABILITIES = {"transient", "supporting", "durable"}
RESEARCH_ROUTING_STATES = {"resolved", "unresolved", "not_research"}
RESEARCH_ROUTING_DECISION_SOURCES = {
    "agent",
    "model_fallback",
    "deterministic",
    "user",
    "import",
}
RESEARCH_SCOPE_RELATIONS = {
    "primary",
    "supporting",
    "shared",
    "imported_continuation",
}
RESEARCH_SCOPE_STATES = {"active", "removed"}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except Exception:
            value = []
    return list(value) if isinstance(value, list) else []


def _strings(value: Any, *, limit: int = 100) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in _json_list(value)
            if str(item or "").strip()
        )
    )[: max(0, limit)]


def _objects(value: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    return [dict(item) for item in _json_list(value) if isinstance(item, dict)][: max(0, limit)]


@dataclass(frozen=True)
class ResearchTurnRoute:
    route_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    turn_id: str
    research_ids: list[str]
    imported_scope_refs: list[dict[str, Any]]
    durability: str
    confidence: float
    routing_state: str
    reason_code: str
    decision_source: str
    source_digest: str
    revision: int
    created_at: str
    superseded_by: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchTurnRoute":
        value = dict(row)
        value["research_ids"] = _strings(value.pop("research_ids_json", "[]"), limit=3)
        value["imported_scope_refs"] = _objects(
            value.pop("imported_scope_refs_json", "[]"),
            limit=100,
        )
        value["schema_version"] = int(value.get("schema_version") or 0)
        value["revision"] = int(value.get("revision") or 0)
        value["confidence"] = float(value.get("confidence") or 0.0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchObjectScope:
    scope_link_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    object_type: str
    object_id: str
    research_id: str
    relation: str
    confidence: float
    source_route_id: str
    state: str
    created_at: str
    superseded_by: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchObjectScope":
        value = dict(row)
        value["schema_version"] = int(value.get("schema_version") or 0)
        value["confidence"] = float(value.get("confidence") or 0.0)
        return cls(**value)


@dataclass(frozen=True)
class ResearchRouteProposal:
    research_ids: list[str]
    imported_scope_refs: list[dict[str, str]]
    durability: str
    confidence: float
    routing_state: str
    reason_code: str
    new_research_signal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchRouteExtraction:
    visible_content: str
    payload: dict[str, Any] | None
    found: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchRoutingContext:
    active: bool
    projects: list[dict[str, Any]]
    imported_scopes: list[dict[str, Any]]
    token_estimate: int
    token_budget: int
    local_token_budget: int
    imported_token_budget: int
    prompt_text: str

    def to_dict(self, *, include_prompt: bool = True) -> dict[str, Any]:
        value = asdict(self)
        if not include_prompt:
            value.pop("prompt_text", None)
        return value


__all__ = [
    "RESEARCH_DURABILITIES",
    "RESEARCH_ROUTING_DECISION_SOURCES",
    "RESEARCH_ROUTE_ENVELOPE_SCHEMA",
    "RESEARCH_ROUTE_ENVELOPE_TAG",
    "RESEARCH_ROUTING_SCHEMA_VERSION",
    "RESEARCH_ROUTING_STATES",
    "RESEARCH_SCOPE_RELATIONS",
    "RESEARCH_SCOPE_STATES",
    "ResearchObjectScope",
    "ResearchRouteExtraction",
    "ResearchRouteProposal",
    "ResearchRoutingContext",
    "ResearchTurnRoute",
]
