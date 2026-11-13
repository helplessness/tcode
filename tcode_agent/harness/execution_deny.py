"""Monotonic execution-stage deny policy for visible tools.

The policy in this module is intentionally narrow: it can only turn an
otherwise executable tool call into a denial.  It cannot add tools, hide model
definitions, grant permissions, bypass biosafety, or select an execution body.
This makes it suitable for controlled preflight evidence and emergency runtime
blocks without introducing a second execution pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXECUTION_DENY_POLICY_CONTRACT_ID = "tcode.tool-execution-deny-policy.v1"
EXECUTION_DENY_POLICY_RELATIVE_PATH = Path("config/tool_execution_deny_policy.json")


@dataclass(frozen=True, slots=True)
class ExecutionDenyDecision:
    """Represent the payload-free result of the monotonic deny lookup.

    ``denied`` is true only when an exact tool entry matches or when a present
    policy file is invalid and must fail closed.  ``reason`` is safe to expose
    in a ToolResult, while ``metadata`` contains only contract and aggregate
    diagnostics—never the policy path, the full policy document, arguments, or
    session identifiers.
    """

    denied: bool
    reason: str = ""
    metadata: dict[str, Any] | None = None


def _policy_root_from_context(context: Any) -> Path:
    """Resolve the workspace root that owns the execution deny policy.

    The tool executor stores the authoritative control workspace in
    ``context.extra.control_workdir`` and the active agent workspace in
    ``context.extra.workdir``.  The function prefers the control workspace,
    falls back to the active workspace, and finally uses the current directory
    for direct unit-level calls.  It performs no filesystem mutation.
    """

    extra = getattr(context, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    root = str(extra.get("control_workdir") or extra.get("workdir") or ".").strip()
    return Path(root or ".").expanduser().resolve(strict=False)


def execution_deny_policy_path(context: Any) -> Path:
    """Return the single v1 policy path for a tool execution context.

    There is deliberately no legacy filename, environment-list fallback, or
    schema adapter.  Operators migrating from an older experiment must delete
    it and create the documented v1 file at this exact relative path.
    """

    return _policy_root_from_context(context) / EXECUTION_DENY_POLICY_RELATIVE_PATH


def _invalid_policy_decision(code: str) -> ExecutionDenyDecision:
    """Create a fail-closed decision for a malformed present policy file.

    The returned reason avoids echoing parser messages or file contents because
    those may contain local paths or operator data.  ``code`` is a bounded
    implementation-owned enum used by diagnostics and tests.
    """

    return ExecutionDenyDecision(
        denied=True,
        reason="Tool execution deny policy is invalid; execution was blocked fail-closed.",
        metadata={
            "contract_id": EXECUTION_DENY_POLICY_CONTRACT_ID,
            "policy_invalid": True,
            "policy_error_code": str(code or "invalid-policy")[:80],
        },
    )


def evaluate_execution_deny_policy(tool_name: str, context: Any) -> ExecutionDenyDecision:
    """Evaluate the single-schema execution-stage deny policy for one tool.

    Input is the exact model-emitted tool name plus its execution context.  A
    missing policy file means no additional denial.  A present file must be a
    JSON object with the exact v1 ``contract_id`` and a ``deny_tools`` array of
    objects shaped as ``{"tool": <exact-name>, "reason": <non-empty-text>}``.
    Wildcards, duplicate names, unknown root fields, and legacy list-only
    formats are rejected.  Invalid present configuration denies every tool so
    a broken safety file cannot silently become allow.  The function never
    mutates configuration or evaluates tool arguments.
    """

    normalized_tool = str(tool_name or "").strip()
    path = execution_deny_policy_path(context)
    if not path.is_file():
        return ExecutionDenyDecision(denied=False, metadata={})

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _invalid_policy_decision("unreadable-json")
    if not isinstance(document, dict):
        return _invalid_policy_decision("root-not-object")
    if set(document) != {"contract_id", "deny_tools"}:
        return _invalid_policy_decision("root-fields")
    if document.get("contract_id") != EXECUTION_DENY_POLICY_CONTRACT_ID:
        return _invalid_policy_decision("contract-id")

    entries = document.get("deny_tools")
    if not isinstance(entries, list):
        return _invalid_policy_decision("deny-tools-not-array")

    normalized_entries: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"tool", "reason"}:
            return _invalid_policy_decision("deny-entry-fields")
        entry_tool = str(entry.get("tool") or "").strip()
        entry_reason = str(entry.get("reason") or "").strip()
        if not entry_tool or not entry_reason:
            return _invalid_policy_decision("deny-entry-empty")
        if any(marker in entry_tool for marker in ("*", "?", "[", "]")):
            return _invalid_policy_decision("deny-entry-wildcard")
        if entry_tool in normalized_entries:
            return _invalid_policy_decision("deny-entry-duplicate")
        normalized_entries[entry_tool] = entry_reason

    reason = normalized_entries.get(normalized_tool)
    if reason is None:
        return ExecutionDenyDecision(
            denied=False,
            metadata={
                "contract_id": EXECUTION_DENY_POLICY_CONTRACT_ID,
                "deny_entry_count": len(normalized_entries),
            },
        )
    return ExecutionDenyDecision(
        denied=True,
        reason=reason,
        metadata={
            "contract_id": EXECUTION_DENY_POLICY_CONTRACT_ID,
            "deny_entry_count": len(normalized_entries),
            "execution_stage_deny": True,
        },
    )
