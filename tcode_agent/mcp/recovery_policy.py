"""Recovery policy helpers for MCP tool failures."""

from __future__ import annotations

from typing import Any


_JOB_PROTOCOL_TOOLS = {"submit_job", "get_job_status", "collect_job_result"}


def job_protocol_recovery_policy_error(
    *,
    server_name: str,
    tool_name: str,
    error: str,
) -> tuple[str, dict[str, Any]]:
    error_detail = str(error).strip() or "Unknown MCP error"
    message = f"MCP tool execution failed: {error_detail}"
    metadata: dict[str, Any] = {}
    if server_name.lower() == "job-protocol-mcp" and tool_name in _JOB_PROTOCOL_TOOLS:
        message += (
            "\nRecovery policy: this is a job-protocol task chain. First repair or retry "
            "the same submit/status/result flow, or report the failure with the original "
            "arguments. Do not silently switch to workflow_run or another execution mode; "
            "only switch when it is an explicit same-intent escalation and explain that "
            "rationale before invoking the alternative."
        )
        metadata = {
            "fallback_policy": "same_job_protocol_chain_first",
            "cross_mode_fallback_requires_rationale": True,
        }
    return message, metadata
