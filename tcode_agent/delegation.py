"""Machine-readable delegation contracts shared by tools and runtimes."""

from __future__ import annotations

from typing import Any, Iterable


DELEGATION_CONTRACT_VERSION = 1
_SUPPORTED_DELEGATION_CONTRACT_VERSIONS = {0, DELEGATION_CONTRACT_VERSION}
_SUPPORTED_DELEGATION_ACTIONS = {
    "create_subagent",
    "create_or_reuse_subagent",
    "delegate",
}
_SUPPORTED_PERMISSION_MODES = {"inherit"}


def _names(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        lowered = value.lower()
        if not value or lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result


def build_permission_delegation_metadata(
    *,
    allowed_agents: Iterable[str],
    preferred_agent: str = "",
    match_tokens: Iterable[str] = (),
    delegate_tool: str = "task",
    delegate_agent_parameter: str = "subagent_type",
) -> dict[str, Any]:
    """Build the common contract for a denied action that another agent may run."""

    agents = _names(list(allowed_agents))
    preferred = str(preferred_agent or "").strip()
    if not preferred or preferred not in agents:
        preferred = agents[0] if agents else ""
    tokens = _names(list(match_tokens))
    agent_parameter = (
        str(delegate_agent_parameter or "subagent_type").strip()
        or "subagent_type"
    )
    return {
        "permission_denied": True,
        "allowed_agents": agents,
        "delegate_tool": str(delegate_tool or "task").strip() or "task",
        "delegate_agent_parameter": agent_parameter,
        "delegate_subagent_type": preferred,
        "delegate_allowed_tools": "inherit",
        "delegation": {
            "version": DELEGATION_CONTRACT_VERSION,
            "action": "create_or_reuse_subagent",
            "tool": str(delegate_tool or "task").strip() or "task",
            "agent_parameter": agent_parameter,
            "allowed_agents": agents,
            "preferred_agent": preferred,
            "permission_mode": "inherit",
            "match_tokens": tokens,
        },
    }


def read_permission_delegation_contract(
    metadata: Any,
    *,
    failed: bool,
) -> dict[str, Any] | None:
    """Read the common contract, accepting flat v0 fields for compatibility."""

    # Automatic delegation is a recovery path.  A successful call carrying
    # stale or forged delegation metadata must never launch another agent.
    if not failed or not isinstance(metadata, dict):
        return None
    nested = metadata.get("delegation")
    contract = dict(nested) if isinstance(nested, dict) else {}
    agents = _names(contract.get("allowed_agents") or metadata.get("allowed_agents"))
    if not agents:
        return None

    explicitly_denied = metadata.get("permission_denied") is True
    action = str(contract.get("action") or "").strip()
    has_contract = bool(contract) and action in _SUPPORTED_DELEGATION_ACTIONS
    if not explicitly_denied or (contract and not has_contract):
        return None

    raw_version = contract.get("version", 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError, OverflowError):
        return None
    if version not in _SUPPORTED_DELEGATION_CONTRACT_VERSIONS:
        return None

    permission_mode = str(
        contract.get("permission_mode")
        or metadata.get("delegate_allowed_tools")
        or "inherit"
    ).strip().lower()
    if permission_mode not in _SUPPORTED_PERMISSION_MODES:
        return None

    preferred = str(
        contract.get("preferred_agent")
        or metadata.get("delegate_subagent_type")
        or ""
    ).strip()
    if preferred not in agents:
        preferred = agents[0]
    tokens = _names(contract.get("match_tokens"))
    return {
        "version": version,
        "action": action or "create_or_reuse_subagent",
        "tool": str(contract.get("tool") or metadata.get("delegate_tool") or "task"),
        "agent_parameter": str(
            contract.get("agent_parameter")
            or metadata.get("delegate_agent_parameter")
            or "subagent_type"
        ).strip(),
        "allowed_agents": agents,
        "preferred_agent": preferred,
        "permission_mode": permission_mode,
        "match_tokens": tokens,
    }
