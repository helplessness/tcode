"""机器可读的 Harness conformance 报告。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConformanceCase(_Model):
    case_id: str = Field(alias="caseId")
    passed: bool
    message: str = ""


class HarnessConformanceReport(_Model):
    schema_id: str = Field(
        default="tcode.harness-conformance.v1", alias="schema"
    )
    runtime_id: str = Field(alias="runtimeId")
    adapter_version: str = Field(default="", alias="adapterVersion")
    definition_digest: str = Field(default="", alias="definitionDigest")
    mode: str
    passed: bool
    capabilities_verified: list[str] = Field(
        default_factory=list, alias="capabilitiesVerified"
    )
    capabilities_rejected: list[str] = Field(
        default_factory=list, alias="capabilitiesRejected"
    )
    cases: list[ConformanceCase] = Field(default_factory=list)


__all__ = ["ConformanceCase", "HarnessConformanceReport"]
