from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ...tools.base import ToolContext


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_GROUPS: dict[str, tuple[str, ...]] = {
    "group:all": ("*",),
    "group:runtime": ("bash", "exec", "process", "process_runtime", "skill_run"),
    "group:fs": ("read", "write", "edit", "glob", "grep", "import_resource"),
    "group:web": ("webfetch", "websearch"),
    "group:ui": ("browser", "canvas", "intelligent_page", "mcp_*browser*", "mcp_*playwright*", "mcp_*chrome*", "mcp_*cdp*"),
    "group:mcp": ("mcp_*",),
    "group:plugins": ("plugin_runtime", "plugin_network", "plugin:*"),
    "group:orchestration": (
        "task",
        "subagent_guide",
        "create_worker",
        "assign_task",
        "list_workers",
        "list_teams",
        "terminate_worker",
        "ask_peer_question",
        "answer_peer_question",
        "team_memory_read",
        "team_memory_write",
        "task_graph",
    ),
}


@dataclass(frozen=True)
class SandboxToolDecision:
    tool_id: str
    sandbox_enabled: bool
    run_in_sandbox: bool
    blocked: bool = False
    reason: str = ""
    mode: str = "off"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = str(os.environ.get(name, default) or "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def sandbox_mode() -> str:
    """Return the OpenClaw-like sandbox mode for tcode.

    `TCODE_SANDBOX_ENABLED=true` is kept as the legacy switch. When enabled and
    no mode is provided, the default is `runtime`: command-like tools are run in
    the sandbox while pure control-plane tools stay on the host.
    """
    from .base import strict_isolation_enabled

    if strict_isolation_enabled():
        return "runtime"
    enabled_raw = os.environ.get("TCODE_SANDBOX_ENABLED")
    if enabled_raw is not None and enabled_raw.strip().lower() in _FALSE_VALUES:
        return "off"

    raw = str(os.environ.get("TCODE_SANDBOX_MODE", "") or "").strip().lower()
    if not raw:
        return "runtime" if _env_bool("TCODE_SANDBOX_ENABLED", default=False) else "off"
    if raw in {"0", "false", "no", "off", "disabled"}:
        return "off"
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return "runtime"
    if raw in {"runtime", "all", "non-main"}:
        return raw
    return "runtime" if _env_bool("TCODE_SANDBOX_ENABLED", default=False) else "off"


def sandbox_glob_patterns(name: str, default: str = "") -> tuple[str, ...]:
    return _env_list(name, default)


def _expanded_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for pattern in patterns:
        key = pattern.strip().lower()
        if not key:
            continue
        group_patterns = _GROUPS.get(key)
        if group_patterns:
            expanded.extend(group_patterns)
        elif key.startswith("group:custom:"):
            # group:custom:label=name1,name2,name3 — user-defined grouping via env var
            # e.g. "group:custom:memory_tools=memory_read,memory_write,memory_cleanup"
            custom_tools = key[len("group:custom:"):]
            if "=" in custom_tools:
                _, tools = custom_tools.split("=", 1)
                expanded.extend(t.strip() for t in tools.split(",") if t.strip())
            else:
                expanded.append(pattern)
        else:
            expanded.append(pattern)
    return tuple(expanded)


def matches_sandbox_pattern(value: str, patterns: tuple[str, ...]) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    normalized_l = normalized.lower()
    for pattern in _expanded_patterns(patterns):
        pattern_l = str(pattern or "").strip().lower()
        if not pattern_l:
            continue
        if pattern_l == normalized_l:
            return True
        if "*" in pattern_l or "?" in pattern_l:
            if fnmatch.fnmatch(normalized_l, pattern_l):
                return True
    return False


def _is_non_main_context(context: Optional[ToolContext]) -> bool:
    if context is None:
        return False
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        if str(extra.get("parent_session_id", "") or "").strip():
            return True
        trace = extra.get("tool_trace_context")
        if isinstance(trace, dict) and str(trace.get("parent_session_id", "") or "").strip():
            return True
    agent_name = str(getattr(context, "agent", "") or "").strip().lower()
    if not agent_name:
        return False
    return agent_name not in {"main", "default", "assistant", "manager"}


def sandbox_session_enabled(context: Optional[ToolContext] = None) -> bool:
    mode = sandbox_mode()
    if mode == "off":
        return False
    if mode in {"runtime", "all"}:
        return True
    if mode == "non-main":
        return _is_non_main_context(context)
    return False


def sandbox_tool_block_reason(tool_id: str, context: Optional[ToolContext] = None) -> str:
    """Return a hard-block reason when sandbox tool policy denies this tool."""
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        desktop_browser = tool_id == "browser" and os.environ.get("TCODE_DESKTOP_BROWSER_READY") == "1"
        # canvas is platform-owned SVG data, not a host UI/script runner.
        if tool_id not in {"canvas", "intelligent_page"} and not desktop_browser and matches_sandbox_pattern(tool_id, ("group:plugins", "group:ui", "import_resource")):
            return "此工具尚未接入桌面原生隔离；请用文件界面导入本地输入，计算任务使用远程 MCP。"
        if matches_sandbox_pattern(tool_id, ("group:runtime",)) and os.environ.get("TCODE_DESKTOP_NATIVE_READY") != "1":
            return "桌面本地隔离自检未通过；远程 MCP 无需本机 Sandbox Runner。"
    if not sandbox_session_enabled(context):
        return ""

    denied = sandbox_glob_patterns("TCODE_SANDBOX_TOOL_DENY")
    if matches_sandbox_pattern(tool_id, denied):
        return f"Tool '{tool_id}' is denied by TCODE_SANDBOX_TOOL_DENY."

    allowed = sandbox_glob_patterns("TCODE_SANDBOX_TOOL_ALLOW")
    if allowed and not matches_sandbox_pattern(tool_id, allowed):
        return f"Tool '{tool_id}' is not allowed by TCODE_SANDBOX_TOOL_ALLOW."

    return ""


def tool_should_run_in_sandbox(tool_id: str, context: Optional[ToolContext] = None) -> bool:
    # Even stale/direct tool calls must enter the rejecting desktop runtime,
    # never interpret a policy denial as permission to execute on the host.
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1" and matches_sandbox_pattern(tool_id, ("group:runtime", "group:plugins")):
        return True
    if not sandbox_session_enabled(context):
        return False
    if sandbox_tool_block_reason(tool_id, context):
        return False

    mode = sandbox_mode()
    from .base import strict_isolation_enabled

    if strict_isolation_enabled() and matches_sandbox_pattern(
        tool_id, ("group:runtime", "group:plugins", "group:fs")
    ):
        return True
    default_run_groups = "group:runtime,group:plugins" if mode != "all" else "group:all"
    run_groups = sandbox_glob_patterns("TCODE_SANDBOX_TOOL_RUN_GROUPS", default_run_groups)
    return matches_sandbox_pattern(tool_id, run_groups)


def sandbox_tool_decision(tool_id: str, context: Optional[ToolContext] = None) -> SandboxToolDecision:
    mode = sandbox_mode()
    enabled = sandbox_session_enabled(context)
    reason = sandbox_tool_block_reason(tool_id, context)
    return SandboxToolDecision(
        tool_id=tool_id,
        sandbox_enabled=enabled,
        run_in_sandbox=tool_should_run_in_sandbox(tool_id, context),
        blocked=bool(reason),
        reason=reason,
        mode=mode,
    )
