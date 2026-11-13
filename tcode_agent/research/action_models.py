"""Durable, owner-scoped pending actions for natural-language research control."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


RESEARCH_PENDING_ACTION_SCHEMA_VERSION = 1
RESEARCH_PENDING_ACTION_STATUSES = {
    "pending",
    "executing",
    "completed",
    "cancelled",
    "expired",
    "stale",
    "failed",
    "superseded",
}


@dataclass(frozen=True)
class ResearchPendingAction:
    action_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str
    proposal_message_id: str
    kind: str
    status: str
    target_type: str
    target_id: str
    target_digest: str
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    expires_at: str = ""
    decision_message_id: str = ""
    completed_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    idempotency_key: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "ResearchPendingAction":
        def decode(name: str) -> dict[str, Any]:
            try:
                value = json.loads(str(row[name] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            return value if isinstance(value, dict) else {}

        return cls(
            action_id=str(row["action_id"] or ""),
            schema_version=int(row["schema_version"] or 1),
            tenant_id=str(row["tenant_id"] or ""),
            user_id=str(row["user_id"] or ""),
            workspace_id=str(row["workspace_id"] or ""),
            session_id=str(row["session_id"] or ""),
            proposal_message_id=str(row["proposal_message_id"] or ""),
            kind=str(row["kind"] or ""),
            status=str(row["status"] or ""),
            target_type=str(row["target_type"] or ""),
            target_id=str(row["target_id"] or ""),
            target_digest=str(row["target_digest"] or ""),
            payload=decode("payload_json"),
            result=decode("result_json"),
            expires_at=str(row["expires_at"] or ""),
            decision_message_id=str(row["decision_message_id"] or ""),
            completed_at=str(row["completed_at"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
        )

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "action_id": self.action_id,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "status": self.status,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_digest": self.target_digest,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }
        if include_payload:
            value["payload"] = dict(self.payload)
            value["result"] = dict(self.result)
        return value


__all__ = [
    "RESEARCH_PENDING_ACTION_SCHEMA_VERSION",
    "RESEARCH_PENDING_ACTION_STATUSES",
    "ResearchPendingAction",
]
