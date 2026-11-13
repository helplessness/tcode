"""Generic, metadata-driven selection for permission-delegation recovery."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..agent.agents import BuiltinAgents
from ..delegation import read_permission_delegation_contract
from ..team.catalog import TeamCatalog


_FAILED_STATES = {"error", "failed", "cancelled", "canceled"}


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip()
        lowered = name.lower()
        if not name or lowered in seen:
            continue
        seen.add(lowered)
        result.append(name)
    return result


def _failed_call_snapshot(tool_call: dict[str, Any]) -> dict[str, Any]:
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return {
        "tool_name": str(tool_call.get("tool_name") or "").strip(),
        "call_id": str(tool_call.get("call_id") or "").strip(),
        "arguments": dict(arguments),
    }


def _delegation_id(failed_call: dict[str, Any]) -> str:
    call_id = str(failed_call.get("call_id") or "").strip()
    if call_id:
        return f"tool-call:{call_id}"
    encoded = json.dumps(
        failed_call,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "tool-call:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def build_permission_delegation_message(
    delegate: dict[str, Any],
    original_user_request: str,
) -> str:
    """Build a generic replay contract without tool- or agent-specific prose."""

    payload = {
        "schema": "permission-delegation/v1",
        "delegation_id": str(delegate.get("delegation_id") or ""),
        "original_user_request": str(original_user_request or ""),
        "failed_tool_call": dict(delegate.get("failed_call") or {}),
        "requirements": {
            "preserve_tool_name": True,
            "preserve_arguments": True,
            "use_assigned_agent_permissions": True,
            "report_actual_tool_result": True,
            "do_not_claim_submission_without_identifier": True,
        },
    }
    return (
        "执行以下结构化权限委派契约。失败调用的工具名与参数是权威输入；"
        "不要从自然语言重新推断或改写它们。若参数在当前运行环境不可用，"
        "请返回可验证的具体错误，不要声称任务已提交。\n\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        + "\n```"
    )


def _recovered_by_later_delegation(
    tool_calls_data: list[dict[str, Any]],
    denied_index: int,
    *,
    delegation_id: str,
    allowed_agents: list[str],
    delegate_tool: str,
    agent_parameter: str,
) -> bool:
    """Recognize a later successful delegation from structured fields only."""

    normalized_agents = {
        str(item or "").strip().lower()
        for item in allowed_agents
        if str(item or "").strip()
    }
    if not delegate_tool:
        return False
    for later in tool_calls_data[denied_index + 1 :]:
        if str(later.get("tool_name") or "").strip() != delegate_tool:
            continue
        later_state = str(later.get("state") or "").strip().lower()
        if str(later.get("error") or "").strip() or later_state in _FAILED_STATES:
            continue
        metadata = later.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        linked_id = str(
            metadata.get("delegation_id")
            or metadata.get("delegation_parent_id")
            or ""
        ).strip()
        if linked_id:
            if linked_id == delegation_id:
                return True
            continue
        arguments = later.get("arguments")
        if not isinstance(arguments, dict):
            continue
        requested_agent = str(arguments.get(agent_parameter) or "").strip().lower()
        if not requested_agent or requested_agent not in normalized_agents:
            continue
        claim_validation = metadata.get("delegated_claim_validation")
        if (
            isinstance(claim_validation, dict)
            and claim_validation.get("required") is True
            and claim_validation.get("valid") is True
        ):
            return True
        if metadata.get("accepted_async") is True:
            return True
    return False


def pick_permission_denied_delegate(
    current_agent_name: str,
    tool_calls_data: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Select a legal delegated runtime from a failed tool's metadata contract."""

    current = str(current_agent_name or "").strip().lower()
    for index, tool_call in enumerate(tool_calls_data):
        tool_name = str(tool_call.get("tool_name") or "").strip()
        error_text = str(tool_call.get("error") or "")
        state = str(tool_call.get("state") or "").strip().lower()
        failed = bool(error_text) or state in _FAILED_STATES
        metadata = tool_call.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        review_decision = str(metadata.get("review_decision") or "").strip().lower()
        retry_review = metadata.get("retry_review")
        if isinstance(retry_review, dict):
            review_decision = str(
                retry_review.get("decision", review_decision) or review_decision
            ).strip().lower()
        explicit_recovery = (
            failed
            and metadata.get("recovery_required") is True
            and review_decision in {"delegate", "revise"}
        )

        contract = read_permission_delegation_contract(metadata, failed=failed)
        if contract is None and not explicit_recovery:
            continue
        allowed_agents = _names(
            (contract or {}).get("allowed_agents") or metadata.get("allowed_agents")
        )
        if not allowed_agents:
            continue
        preferred = str((contract or {}).get("preferred_agent") or "").strip()
        ordered_agents = list(allowed_agents)
        if preferred in ordered_agents:
            ordered_agents.remove(preferred)
            ordered_agents.insert(0, preferred)

        failed_call = _failed_call_snapshot(tool_call)
        delegation_id = _delegation_id(failed_call)
        delegate_tool = str((contract or {}).get("tool") or "task").strip()
        agent_parameter = str(
            (contract or {}).get("agent_parameter") or "subagent_type"
        ).strip()
        if _recovered_by_later_delegation(
            tool_calls_data,
            index,
            delegation_id=delegation_id,
            allowed_agents=allowed_agents,
            delegate_tool=delegate_tool,
            agent_parameter=agent_parameter,
        ):
            continue

        for candidate in ordered_agents:
            entry = TeamCatalog.get_by_agent_name(candidate, available_only=True)
            runtime_agent = str(
                entry.runtime_agent if entry is not None else candidate
            ).strip()
            if not runtime_agent or runtime_agent.lower() == current:
                continue
            agent_info = BuiltinAgents.get(runtime_agent)
            if not agent_info:
                continue
            options = dict(getattr(agent_info, "options", {}) or {})
            return {
                "team_key": entry.key if entry is not None else "",
                "agent_name": runtime_agent,
                "display_name": (
                    entry.display_name
                    if entry is not None
                    else str(options.get("display_name") or runtime_agent)
                ),
                "slug": entry.slug if entry is not None else runtime_agent,
                "reason": (
                    f"工具 {tool_name} 触发恢复流程（{review_decision}），自动转交给可处理代理"
                    if explicit_recovery
                    else f"工具 {tool_name} 权限受限，自动转交给具备权限的代理处理"
                ),
                "route_rule": (
                    "tool_recovery_delegate"
                    if explicit_recovery
                    else "tool_permission_delegate"
                ),
                "route_source": (
                    "runtime_tool_recovery_metadata"
                    if explicit_recovery
                    else "runtime_permission_delegation_contract"
                ),
                "tool_name": tool_name,
                "allowed_agents": allowed_agents,
                "review_decision": review_decision,
                "delegation_contract": dict(contract or {}),
                "delegation_id": delegation_id,
                "failed_call": failed_call,
            }
    return None


_pick_permission_denied_delegate = pick_permission_denied_delegate
_pick_permission_denied_team_delegate = pick_permission_denied_delegate
