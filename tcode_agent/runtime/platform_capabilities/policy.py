"""Fail-closed admission policy for the external Harness platform bridge."""

from __future__ import annotations

import os
from typing import Any


DEFAULT_CAPABILITY_ALLOWLIST = {
    "image.inspect",
    "evidence.read",
    "evidence.write",
    "wiki.read",
    "wiki.propose",
    "research.read",
    "skill.read",
    "artifact.publish",
    "job.describe",
    "job.submit",
}

DEFAULT_JOB_TARGETS = {
    "boltz-mcp",
    "gnina-mcp",
    "ligandmpnn-mcp",
    "placer-mcp",
    "plip-mcp",
    "rfdiffusion-mcp",
    "evolvepro-mcp",
    "mutation-scoring-mcp",
    "simulation-mcp",
    "simple-demo-mcp",
}

DEFAULT_JOB_TARGET_TOOLS = {
    "boltz-mcp/boltz_mcp_server",
    "evolvepro-mcp/evolvepro_single_round_mcp_server",
    "gnina-mcp/gnina_mcp_server",
    "ligandmpnn-mcp/ligandmpnn_run_mcp_server",
    "mutation-scoring-mcp/multi_model_sequence_mutation_scoring_mcp_server",
    "plip-mcp/plip_mcp_server",
    "simple-demo-mcp/echo_text",
    "simple-demo-mcp/sleep_then_echo",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: set[str]) -> set[str]:
    raw = os.environ.get(name)
    if raw is None:
        return set(default)
    return {item.strip() for item in raw.split(",") if item.strip()}


class PlatformCapabilityPolicy:
    """Server-owned policy; Harness declarations can only reduce this set."""

    def __init__(self) -> None:
        self.enabled = _env_bool("TCODE_PLATFORM_BRIDGE_ENABLED", True)
        self.allowed_capabilities = _csv(
            "TCODE_PLATFORM_BRIDGE_CAPABILITIES", DEFAULT_CAPABILITY_ALLOWLIST
        )
        self.allowed_runtime_ids = _csv(
            "TCODE_PLATFORM_BRIDGE_ALLOWED_RUNTIME_IDS", set()
        )
        self.allowed_job_targets = {
            item.lower()
            for item in _csv("TCODE_PLATFORM_BRIDGE_JOB_TARGETS", DEFAULT_JOB_TARGETS)
        }
        self.allowed_job_tools = {
            item.lower()
            for item in _csv(
                "TCODE_PLATFORM_BRIDGE_JOB_TOOLS", DEFAULT_JOB_TARGET_TOOLS
            )
        }
        self.allowed_skills = _csv("TCODE_PLATFORM_BRIDGE_ALLOWED_SKILLS", set())

    def allows_runtime(self, runtime_id: str, capabilities: dict[str, Any]) -> bool:
        normalized = str(runtime_id or "").strip()
        if not self.enabled or normalized in {"", "tcode-native", "direct-echo"}:
            return False
        if self.allowed_runtime_ids and normalized not in self.allowed_runtime_ids:
            return False
        return capabilities.get("tools") is True

    def allows(self, capability_id: str) -> bool:
        return self.enabled and capability_id in self.allowed_capabilities

    def allows_job_target(self, server_name: str) -> bool:
        return str(server_name or "").strip().lower() in self.allowed_job_targets

    def allows_job_tool(self, server_name: str, tool_name: str) -> bool:
        server = str(server_name or "").strip().lower()
        tool = str(tool_name or "").strip().lower()
        return self.allows_job_target(server) and bool(
            f"{server}/{tool}" in self.allowed_job_tools
            or f"{server}/*" in self.allowed_job_tools
        )

    def skill_allowed(self, name: str, skill_id: str = "") -> bool:
        if not self.allowed_skills:
            return False
        return "*" in self.allowed_skills or bool(
            {str(name or "").strip(), str(skill_id or "").strip()} & self.allowed_skills
        )


def runtime_platform_capability_summary(
    runtime_id: str, capabilities: dict[str, Any]
) -> dict[str, Any]:
    policy = PlatformCapabilityPolicy()
    enabled = policy.allows_runtime(runtime_id, capabilities)
    admitted = sorted(policy.allowed_capabilities) if enabled else []
    return {
        "schema": "tcode.platform-bridge-summary.v1",
        "enabled": enabled,
        "status": "available" if enabled else "disabled",
        "capabilities": admitted,
        "transport": (
            "acp-stdio-mcp"
            if enabled and str(capabilities.get("mcpInjection") or "") == "acp"
            else "environment-cli" if enabled else "none"
        ),
        "defaultDeny": True,
    }


__all__ = [
    "DEFAULT_CAPABILITY_ALLOWLIST",
    "DEFAULT_JOB_TARGETS",
    "DEFAULT_JOB_TARGET_TOOLS",
    "PlatformCapabilityPolicy",
    "runtime_platform_capability_summary",
]
