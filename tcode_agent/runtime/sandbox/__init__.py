"""Sandbox runtime facade for tool execution."""

from __future__ import annotations

from .base import (
    apply_sandbox_config_overrides,
    SandboxConfig,
    SandboxJob,
    SandboxJobStatus,
    SandboxMount,
    SandboxResult,
    SandboxSpec,
    WorkspacePathMap,
    get_sandbox_runtime,
    load_sandbox_config,
    sandbox_enabled,
)
from .policy import build_tool_sandbox_spec
from .tool_policy import (
    SandboxToolDecision,
    matches_sandbox_pattern,
    sandbox_glob_patterns,
    sandbox_mode,
    sandbox_session_enabled,
    sandbox_tool_block_reason,
    sandbox_tool_decision,
    tool_should_run_in_sandbox,
)
from .pathmap import (
    assert_inside_workspace,
    container_to_host_path,
    host_to_container_path,
)

__all__ = [
    "SandboxConfig",
    "SandboxToolDecision",
    "SandboxJob",
    "SandboxJobStatus",
    "SandboxMount",
    "SandboxResult",
    "SandboxSpec",
    "WorkspacePathMap",
    "apply_sandbox_config_overrides",
    "assert_inside_workspace",
    "build_tool_sandbox_spec",
    "container_to_host_path",
    "get_sandbox_runtime",
    "host_to_container_path",
    "load_sandbox_config",
    "matches_sandbox_pattern",
    "sandbox_glob_patterns",
    "sandbox_enabled",
    "sandbox_mode",
    "sandbox_session_enabled",
    "sandbox_tool_block_reason",
    "sandbox_tool_decision",
    "tool_should_run_in_sandbox",
]
