"""Versioned public models for the external Harness capability snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CapabilityGrant(_Model):
    capability_id: str = Field(alias="capabilityId", min_length=1, max_length=120)
    status: Literal["active", "degraded", "unavailable"] = "active"
    operations: list[str] = Field(default_factory=list, max_length=32)
    limits: dict[str, Any] = Field(default_factory=dict, max_length=32)
    reason: str = Field(default="", max_length=1000)


class StagedSkill(_Model):
    skill_id: str = Field(alias="skillId", min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    version: str = Field(default="1", max_length=120)
    digest: str = Field(min_length=8, max_length=128)
    relative_path: str = Field(alias="relativePath", min_length=1, max_length=4096)
    source_kind: str = Field(default="workspace", alias="sourceKind", max_length=80)


class PlatformCapabilitySnapshot(_Model):
    schema_id: Literal["tcode.platform-capability-snapshot.v1"] = Field(
        default="tcode.platform-capability-snapshot.v1", alias="schema"
    )
    snapshot_id: str = Field(alias="snapshotId", min_length=1, max_length=120)
    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=200)
    user_id: str = Field(alias="userId", min_length=1, max_length=200)
    workspace_id: str = Field(alias="workspaceId", min_length=1, max_length=200)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=200)
    run_id: str = Field(alias="runId", min_length=1, max_length=200)
    runtime_id: str = Field(alias="runtimeId", min_length=1, max_length=200)
    model_id: str = Field(default="", alias="modelId", max_length=300)
    native_image_input: bool = Field(default=False, alias="nativeImageInput")
    capabilities: list[CapabilityGrant] = Field(default_factory=list, max_length=64)
    staged_skills: list[StagedSkill] = Field(default_factory=list, alias="stagedSkills", max_length=64)
    staging_root: str = Field(default="", alias="stagingRoot", max_length=4096)
    issued_at: str = Field(alias="issuedAt", max_length=80)
    expires_at: str = Field(alias="expiresAt", max_length=80)
    max_calls: int = Field(default=64, alias="maxCalls", ge=1, le=4096)
    audit_digest: str = Field(default="", alias="auditDigest", max_length=128)

    def active_capability_ids(self) -> list[str]:
        return [item.capability_id for item in self.capabilities if item.status == "active"]

    def has(self, capability_id: str) -> bool:
        return capability_id in self.active_capability_ids()

    def public_dict(self) -> dict[str, Any]:
        """Return the prompt/UI view without the owner identifiers."""

        data = self.model_dump(mode="json", by_alias=True)
        for key in ("tenantId", "userId"):
            data.pop(key, None)
        return data

    @staticmethod
    def timestamp(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = ["CapabilityGrant", "PlatformCapabilitySnapshot", "StagedSkill"]
