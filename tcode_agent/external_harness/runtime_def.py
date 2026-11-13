"""公开的 Harness Runtime v2 合同与发现状态。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeDefModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExecutableDef(RuntimeDefModel):
    primary: str = Field(min_length=1, max_length=200)
    fallbacks: list[str] = Field(default_factory=list, max_length=8)


class ProbeDef(RuntimeDefModel):
    version_args: list[str] = Field(default_factory=lambda: ["--version"], alias="versionArgs", max_length=16)
    auth_strategy: str = Field(default="unknown", alias="authStrategy", max_length=80)
    model_strategy: str = Field(default="none", alias="modelStrategy", max_length=80)
    timeout_seconds: float = Field(default=3.0, alias="timeoutSeconds", ge=0.1, le=30)


class InvocationDef(RuntimeDefModel):
    args: list[str] = Field(default_factory=list, max_length=64)
    prompt_mode: Literal["stdin", "jsonl", "file", "argv", "acp"] = Field(default="stdin", alias="promptMode")
    stream_format: Literal["acp-json-rpc", "codex-jsonl", "claude-stream-json", "ndjson", "plain"] = Field(alias="streamFormat")
    stderr_mode: Literal["diagnostic", "ignore"] = Field(default="diagnostic", alias="stderrMode")

    @field_validator("args")
    @classmethod
    def reject_shell_syntax(cls, values: list[str]) -> list[str]:
        allowed_placeholders = {
            "TCODE_MODEL_ID",
            "TCODE_RESOLVED_WORKSPACE",
        }
        for value in values:
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("argv templates cannot contain control characters")
            if "`" in value or "$(" in value or "${{" in value or "{{" in value:
                raise ValueError("argv templates cannot contain dynamic expressions")
            placeholders = set(re.findall(r"\$\{([^{}]+)\}", value))
            unknown = placeholders - allowed_placeholders
            if unknown:
                raise ValueError(
                    "unknown argv placeholder: " + ", ".join(sorted(unknown))
                )
        return values


class SessionDef(RuntimeDefModel):
    strategy: Literal["none", "native", "acp"] = "none"
    resume_strategy: str = Field(default="none", alias="resumeStrategy", max_length=80)


class HarnessCapabilities(RuntimeDefModel):
    text: bool = True
    images: bool = False
    tools: bool = False
    permission_authority: Literal["tcode", "shared", "runtime", "none"] = Field(default="none", alias="permissionAuthority")
    resume: bool = False
    cancel: bool = True
    mcp_injection: str = Field(default="none", alias="mcpInjection", max_length=80)
    negotiated: bool = False
    guidance_mode: Literal[
        "in_turn", "restart_turn", "queue_only", "unsupported"
    ] = Field(default="queue_only", alias="guidanceMode")


class HarnessSecurity(RuntimeDefModel):
    workspace_mode: Literal["configured"] = Field(default="configured", alias="workspaceMode")
    inherit_environment: Literal["allowlist"] = Field(default="allowlist", alias="inheritEnvironment")
    public_server: bool = Field(default=False, alias="publicServer")


class HarnessCompatibility(RuntimeDefModel):
    adapter_api_version: Literal["1"] = Field(
        default="1", alias="adapterApiVersion"
    )
    tcode_version: str = Field(
        default=">=0.0.1,<1",
        alias="tcodeVersion",
        min_length=1,
        max_length=120,
    )


class HarnessRuntimeDef(RuntimeDefModel):
    schema_id: Literal[
        "tcode.harness-runtime.v1", "tcode.harness-runtime.v2"
    ] = Field(default="tcode.harness-runtime.v2", alias="schema")
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    kind: Literal["local_cli"] = "local_cli"
    release: Literal["experimental", "beta", "stable"] = "beta"
    executables: ExecutableDef
    probes: ProbeDef = Field(default_factory=ProbeDef)
    invocation: InvocationDef
    session: SessionDef = Field(default_factory=SessionDef)
    capabilities: HarnessCapabilities = Field(default_factory=HarnessCapabilities)
    security: HarnessSecurity = Field(default_factory=HarnessSecurity)
    compatibility: HarnessCompatibility = Field(default_factory=HarnessCompatibility)


class RuntimeProbeResult(RuntimeDefModel):
    runtime_id: str = Field(alias="runtimeId")
    display_name: str = Field(alias="displayName")
    release: str
    available: bool
    enabled: bool
    executable: str = ""
    version: str = ""
    auth: str = "unknown"
    models: list[str] = Field(default_factory=list, max_length=5000)
    capabilities: dict[str, object] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list, max_length=32)
    source_type: Literal["builtin", "profile", "entrypoint"] = Field(
        default="builtin", alias="sourceType"
    )
    source_name: str = Field(default="", alias="sourceName", max_length=500)
    distribution_name: str = Field(
        default="", alias="distributionName", max_length=300
    )
    distribution_version: str = Field(
        default="", alias="distributionVersion", max_length=120
    )
    definition_digest: str = Field(
        default="", alias="definitionDigest", max_length=100
    )
    trust_state: Literal[
        "builtin", "declarative", "trusted", "untrusted", "rejected"
    ] = Field(default="builtin", alias="trustState")
    compatibility_state: Literal[
        "compatible", "incompatible", "unknown", "error", "conflict"
    ] = Field(default="compatible", alias="compatibilityState")
    conformance_state: Literal[
        "builtin", "unverified", "passed", "failed", "stale"
    ] = Field(default="builtin", alias="conformanceState")
    capabilities_verified: list[str] = Field(
        default_factory=list, alias="capabilitiesVerified", max_length=64
    )


class HarnessRuntimeRecord(RuntimeDefModel):
    runtime_id: str = Field(alias="runtimeId", min_length=1, max_length=200)
    display_name: str = Field(alias="displayName", min_length=1, max_length=200)
    source_type: Literal["builtin", "profile", "entrypoint"] = Field(
        alias="sourceType"
    )
    source_name: str = Field(alias="sourceName", min_length=1, max_length=500)
    distribution_name: str = Field(
        default="", alias="distributionName", max_length=300
    )
    distribution_version: str = Field(
        default="", alias="distributionVersion", max_length=120
    )
    definition_digest: str = Field(
        default="", alias="definitionDigest", max_length=100
    )
    trust_state: Literal[
        "builtin", "declarative", "trusted", "untrusted", "rejected"
    ] = Field(alias="trustState")
    enabled: bool = False
    compatibility_state: Literal[
        "compatible", "incompatible", "unknown", "error", "conflict"
    ] = Field(alias="compatibilityState")
    diagnostics: list[str] = Field(default_factory=list, max_length=32)
    conformance_state: Literal[
        "builtin", "unverified", "passed", "failed", "stale"
    ] = Field(default="unverified", alias="conformanceState")
    capabilities_verified: list[str] = Field(
        default_factory=list, alias="capabilitiesVerified", max_length=64
    )
    definition: HarnessRuntimeDef | None = None


__all__ = [
    "HarnessCompatibility",
    "HarnessRuntimeDef",
    "HarnessRuntimeRecord",
    "RuntimeProbeResult",
]
