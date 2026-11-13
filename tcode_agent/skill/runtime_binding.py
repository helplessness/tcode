"""Operator-controlled bindings between high-risk skills and dedicated agents."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .activation import SkillActivationStore


@dataclass(frozen=True)
class SkillRuntimeBinding:
    agent: str
    sandbox_profile: str = ""


def _parse_bindings(raw: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for item in str(raw or "").split(","):
        key, separator, value = item.strip().partition(":")
        if separator and key.strip() and value.strip():
            bindings[key.strip().lower()] = value.strip()
    return bindings


def skill_runtime_binding(skill_name: str) -> SkillRuntimeBinding | None:
    skill = str(skill_name or "").strip().lower()
    if not skill:
        return None
    agent_bindings = _parse_bindings(
        os.environ.get("TCODE_SKILL_AGENT_BINDINGS", "agent-browser:browser")
    )
    agent = agent_bindings.get(skill, "")
    if not agent:
        return None
    profile_bindings = _parse_bindings(
        os.environ.get(
            "TCODE_SKILL_SANDBOX_PROFILE_BINDINGS",
            "agent-browser:browser",
        )
    )
    return SkillRuntimeBinding(
        agent=agent,
        sandbox_profile=profile_bindings.get(skill, ""),
    )


def skill_allowed_for_agent(
    skill_name: str,
    agent_name: str,
    workdir: Optional[str] = None,
) -> bool:
    binding = skill_runtime_binding(skill_name)
    normalized_agent = str(agent_name or "").strip().lower()
    if binding is not None and binding.agent.lower() != normalized_agent:
        return False
    return SkillActivationStore.is_enabled(skill_name, normalized_agent, workdir)
