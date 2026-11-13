"""Versioned models for shadow research-attribution decisions."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import canonical_json, sha256_text, utc_now_iso


ATTRIBUTION_SCHEMA_VERSION = 1
ATTRIBUTION_DECISION_VERSION = "attribution-shadow-1"
ATTRIBUTION_STATES = {"unassigned", "pending", "included", "excluded", "frozen"}
ATTRIBUTION_RELEVANCE = {"research", "private_only", "uncertain"}


@dataclass(frozen=True)
class ResearchEventLinkInput:
    tenant_id: str
    user_id: str
    workspace_id: str
    event_id: str
    state: str
    relevance: str
    decision_source: str
    confidence: float
    reason_code: str
    run_id: str
    candidate_id: str = ""
    research_id: str = ""
    reason_detail: dict[str, Any] = field(default_factory=dict)
    expires_at: str = ""
    correction_id: str = ""
    decision_version: str = ATTRIBUTION_DECISION_VERSION

    def build(self) -> "ResearchEventLink":
        state = str(self.state or "unassigned").strip().lower()
        relevance = str(self.relevance or "uncertain").strip().lower()
        if state not in ATTRIBUTION_STATES:
            raise ValueError(f"invalid attribution state: {state}")
        if relevance not in ATTRIBUTION_RELEVANCE:
            raise ValueError(f"invalid attribution relevance: {relevance}")
        candidate_id = str(self.candidate_id or "").strip()[:160]
        research_id = str(self.research_id or "").strip()[:160]
        if state == "included" and not (candidate_id or research_id):
            raise ValueError("included attribution requires candidate_id or research_id")
        confidence = max(0.0, min(float(self.confidence or 0.0), 1.0))
        decision_version = (
            str(self.decision_version or ATTRIBUTION_DECISION_VERSION).strip()[:80]
            or ATTRIBUTION_DECISION_VERSION
        )
        decided_at = utc_now_iso()
        semantic_fingerprint = sha256_text(
            canonical_json(
                {
                    "event_id": str(self.event_id or "").strip(),
                    "state": state,
                    "relevance": relevance,
                    "candidate_id": candidate_id,
                    "research_id": research_id,
                    "decision_source": str(self.decision_source or "rule").strip(),
                    "reason_code": str(self.reason_code or "unspecified").strip(),
                    "decision_version": decision_version,
                    "correction_id": str(self.correction_id or "").strip(),
                }
            )
        )
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "semantic_fingerprint": semantic_fingerprint,
                    "run_id": str(self.run_id or "").strip(),
                }
            )
        )
        return ResearchEventLink(
            link_id=f"rel-{uuid.uuid4()}",
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            tenant_id=str(self.tenant_id or "").strip()[:128],
            user_id=str(self.user_id or "").strip()[:128],
            workspace_id=str(self.workspace_id or "").strip()[:128],
            event_id=str(self.event_id or "").strip()[:160],
            candidate_id=candidate_id,
            research_id=research_id,
            state=state,
            relevance=relevance,
            decision_source=str(self.decision_source or "rule").strip()[:40],
            confidence=confidence,
            reason_code=str(self.reason_code or "unspecified").strip()[:120],
            reason_detail=dict(self.reason_detail or {}),
            shadow=True,
            decision_version=decision_version,
            run_id=str(self.run_id or "").strip()[:160],
            correction_id=str(self.correction_id or "").strip()[:160],
            semantic_fingerprint=semantic_fingerprint,
            idempotency_key=idempotency_key,
            decided_at=decided_at,
            expires_at=str(self.expires_at or "").strip()[:40],
            superseded_by="",
        )


@dataclass(frozen=True)
class ResearchEventLink:
    link_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    event_id: str
    candidate_id: str
    research_id: str
    state: str
    relevance: str
    decision_source: str
    confidence: float
    reason_code: str
    reason_detail: dict[str, Any]
    shadow: bool
    decision_version: str
    run_id: str
    correction_id: str
    semantic_fingerprint: str
    idempotency_key: str
    decided_at: str
    expires_at: str
    superseded_by: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchEventLink":
        import json

        values = dict(row)
        try:
            reason_detail = json.loads(
                str(values.pop("reason_detail_json", "{}") or "{}")
            )
        except Exception:
            reason_detail = {}
        values["reason_detail"] = (
            reason_detail if isinstance(reason_detail, dict) else {}
        )
        values["shadow"] = bool(values.get("shadow"))
        values["confidence"] = float(values.get("confidence") or 0.0)
        return cls(**values)


@dataclass(frozen=True)
class AttributionCorrectionInput:
    tenant_id: str
    user_id: str
    workspace_id: str
    event_id: str
    action: str
    actor_id: str
    candidate_id: str = ""
    research_id: str = ""
    reason: str = ""
    idempotency_key: str = ""

    def normalized_action(self) -> str:
        value = str(self.action or "").strip().lower()
        if value not in {"include", "exclude", "pending", "restore_frozen"}:
            raise ValueError(
                "correction action must be include, exclude, pending, or restore_frozen"
            )
        return value

