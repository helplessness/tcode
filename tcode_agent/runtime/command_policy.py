"""Shared command classification, authorization, audit and replay for Bash/Exec."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from ..runtime.execution_profile import profile_allows_tool
from ..runtime.telemetry import TelemetryGateway
from ..types import ToolResult

from ..file_lock import append_locked_text, state_thread_lock

_WINDOWS = os.name == "nt"

if TYPE_CHECKING:
    from ..tools.base import ToolContext


_WRITE_COMMANDS = {
    "chmod", "chown", "chgrp", "cp", "dd", "install", "ln", "mkdir", "mv",
    "patch", "rm", "rmdir", "tar", "tee", "touch", "truncate", "unzip",
}
_NETWORK_COMMANDS = {
    "curl", "ftp", "git", "nc", "ncat", "ping", "rsync", "scp", "sftp",
    "ssh", "telnet", "wget",
}
_SYSTEM_COMMANDS = {
    "chroot", "crontab", "diskutil", "docker", "fdisk", "halt", "iptables", "kill",
    "killall", "launchctl", "losetup", "mount", "pfctl", "pkill", "poweroff",
    "podman", "reboot", "service", "shutdown", "su", "sudo", "systemctl", "umount",
    "useradd", "userdel", "usermod",
}
_PACKAGE_COMMANDS = {
    "apt", "apt-get", "brew", "conda", "dnf", "gem", "mamba", "pip",
    "pip3", "poetry", "uv", "yum",
}
_PACKAGE_SUBCOMMANDS = {
    "add", "install", "remove", "uninstall", "update", "upgrade", "sync",
}
_JS_PACKAGE_COMMANDS = {"bun", "npm", "pnpm", "yarn"}
_GIT_WRITE_SUBCOMMANDS = {
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
    "commit", "merge", "mv", "rebase", "reset", "restore", "revert", "rm",
    "stash", "switch", "tag",
}
_GIT_NETWORK_SUBCOMMANDS = {
    "clone", "fetch", "ls-remote", "pull", "push", "remote", "submodule",
}
_DYNAMIC_CODE_COMMANDS = {
    "bash", "node", "perl", "php", "python", "python3", "ruby", "sh", "zsh",
}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WRITE_REDIRECT_TOKENS = {">", ">>", "&>", ">&", ">|"}
_NON_PERSISTENT_REDIRECT_TARGETS = {"/dev/null"}


@dataclass(frozen=True, slots=True)
class CommandPolicyDecision:
    allowed: bool
    code: str
    reason: str
    mode: str
    categories: tuple[str, ...]
    executables: tuple[str, ...]
    command_fingerprint: str
    workdir: str
    snapshot_id: str = ""
    policy_fingerprint: str = ""
    violations: tuple[str, ...] = ()
    path_checks: tuple[dict[str, Any], ...] = ()
    approval_granted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["categories"] = list(self.categories)
        payload["executables"] = list(self.executables)
        payload["violations"] = list(self.violations)
        payload["path_checks"] = [dict(item) for item in self.path_checks]
        payload["approval_granted"] = list(self.approval_granted)
        return payload


def _command_fingerprint(command: str) -> str:
    return hashlib.sha256(str(command or "").encode("utf-8")).hexdigest()


def _segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(
            str(command or ""),
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        raise ValueError("命令引号或转义不完整，无法进行安全判断。")
    result: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(character in ";&|" for character in token):
            if current:
                result.append(current)
                current = []
            continue
        current.append(token)
    if current:
        result.append(current)
    return result


def _redirection_targets(segments: list[list[str]]) -> list[str]:
    targets: list[str] = []
    for tokens in segments:
        for index, token in enumerate(tokens[:-1]):
            if token in _WRITE_REDIRECT_TOKENS:
                target = tokens[index + 1]
                if target not in _NON_PERSISTENT_REDIRECT_TARGETS:
                    targets.append(target)
    return targets


def _executable_and_args(tokens: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    if index >= len(tokens):
        return "", []
    executable = Path(tokens[index]).name.lower()
    return executable, tokens[index + 1 :]


def _first_subcommand(args: list[str]) -> str:
    for item in args:
        if not item.startswith("-"):
            return item.strip().lower()
    return ""


def _classify(command: str, *, background: bool) -> tuple[set[str], list[str], list[list[str]]]:
    parsed_segments = _segments(command)
    categories: set[str] = {"read_only"}
    executables: list[str] = []
    if background or re.search(r"(?:^|\s)&\s*$", command) or "nohup" in command:
        categories.add("background_process")
    if _redirection_targets(parsed_segments):
        categories.add("workspace_write")
    if "$(" in command or "$((" in command or "`" in command:
        categories.add("dynamic_shell")

    for tokens in parsed_segments:
        executable, args = _executable_and_args(tokens)
        if not executable:
            continue
        executables.append(executable)
        subcommand = _first_subcommand(args)
        if executable in _SYSTEM_COMMANDS:
            categories.add("system_operation")
        if executable in _WRITE_COMMANDS:
            categories.add("workspace_write")
        if executable in _NETWORK_COMMANDS and executable != "git":
            categories.add("network")
        if executable == "git":
            if subcommand in _GIT_WRITE_SUBCOMMANDS:
                categories.add("workspace_write")
            if subcommand in _GIT_NETWORK_SUBCOMMANDS:
                categories.add("network")
        if executable in _PACKAGE_COMMANDS and subcommand in _PACKAGE_SUBCOMMANDS:
            categories.update({"dependency_install", "network", "workspace_write"})
        if executable in _JS_PACKAGE_COMMANDS:
            if subcommand in _PACKAGE_SUBCOMMANDS or not subcommand:
                categories.update({"dependency_install", "network", "workspace_write"})
        if executable in _DYNAMIC_CODE_COMMANDS:
            categories.add("dynamic_code")
        if executable in {"python", "python3"}:
            lowered_args = [item.lower() for item in args]
            if "-m" in lowered_args and "pip" in lowered_args and any(
                item in _PACKAGE_SUBCOMMANDS for item in lowered_args
            ):
                categories.update({"dependency_install", "network", "workspace_write"})
        if executable == "uv" and subcommand == "pip" and any(
            item.lower() in _PACKAGE_SUBCOMMANDS for item in args
        ):
            categories.update({"dependency_install", "network", "workspace_write"})
        if executable in {"cargo", "go"} and subcommand in {"install", "get"}:
            categories.update({"dependency_install", "network", "workspace_write"})
        if executable == "sed" and any(
            item == "-i" or item.startswith("-i") for item in args
        ):
            categories.add("workspace_write")
        if executable == "find" and any(
            item in {"-delete", "-exec", "-execdir"} for item in args
        ):
            categories.add("workspace_write")
    if len(categories) > 1:
        categories.discard("read_only")
    return categories, executables, parsed_segments


def _path_tokens(
    segments: list[list[str]],
    categories: set[str],
) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    for target in _redirection_targets(segments):
        checks.append((target, "write"))
    for tokens in segments:
        executable, args = _executable_and_args(tokens)
        path_candidates = [
            item
            for item in args
            if item
            and not item.startswith("-")
            and item not in _NON_PERSISTENT_REDIRECT_TARGETS
            and not _URL_RE.match(item)
            and (item.startswith(("/", "./", "../", "~")) or "/" in item)
        ]
        if executable == "cd" and args:
            checks.append((args[0], "execute"))
            continue
        if "workspace_write" not in categories:
            checks.extend((item, "read") for item in path_candidates)
            continue
        if executable in {"cp", "install", "ln", "mv"} and path_candidates:
            checks.extend((item, "read") for item in path_candidates[:-1])
            checks.append((path_candidates[-1], "write"))
        elif executable in _WRITE_COMMANDS or executable in {"sed", "find"}:
            checks.extend((item, "write") for item in path_candidates)
    deduped: dict[tuple[str, str], None] = {}
    for item in checks:
        deduped[item] = None
    return list(deduped)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def _policy_path(
    raw_path: str,
    *,
    workspace: Path,
    cwd: Path,
    sandboxed: bool,
) -> Path:
    """Resolve a command path in the same namespace where it will execute."""

    text = str(raw_path or "").strip()
    if sandboxed:
        container_root = str(
            os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace")
            or "/workspace"
        ).rstrip("/") or "/workspace"
        if text == container_root:
            return workspace
        prefix = f"{container_root}/"
        if text.startswith(prefix):
            relative = text[len(prefix) :]
            return (workspace / relative).resolve(strict=False)

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _permission_action(context: ToolContext, name: str) -> str:
    extra = dict(context.extra or {})
    resolver = extra.get("__permission_resolve__")
    if callable(resolver):
        try:
            value = str(resolver(name) or "allow").strip().lower()
            if value in {"allow", "ask", "deny"}:
                return value
        except Exception:
            pass
    permissions = extra.get("agent_permission")
    if isinstance(permissions, dict):
        value = permissions.get(name, permissions.get("*", "allow"))
        if isinstance(value, str) and value.strip().lower() in {"allow", "ask", "deny"}:
            return value.strip().lower()
    return "allow"


def _effective_network(context: ToolContext, tool_name: str) -> str:
    try:
        from .sandbox import tool_should_run_in_sandbox
        from .sandbox.policy import resolve_sandbox_config

        if not tool_should_run_in_sandbox(tool_name, context):
            return "host"
        configured = {
            item.strip()
            for item in str(
                os.environ.get("TCODE_SANDBOX_NETWORK_ALLOWED_TOOLS", "") or ""
            ).split(",")
            if item.strip()
        }
        if tool_name in configured:
            return str(
                os.environ.get("TCODE_SANDBOX_ALLOWED_NETWORK", "egress") or "egress"
            ).strip().lower()
        config, _ = resolve_sandbox_config(context=context, tool_name=tool_name)
        return str(config.network or "none").strip().lower()
    except Exception:
        return "unknown"


def evaluate_command_policy(
    command: str,
    *,
    context: ToolContext,
    tool_name: str,
    requested_workdir: Optional[str] = None,
    background: bool = False,
    policy_override: Optional[dict[str, Any]] = None,
    approval_granted: tuple[str, ...] = (),
) -> CommandPolicyDecision:
    """Classify and decide without executing or prompting."""

    extra = dict(context.extra or {})
    policy = dict(policy_override or extra.get("command_policy") or {})
    mode = str(policy.get("mode") or "enforce").strip().lower()
    if mode not in {"enforce", "audit", "disabled"}:
        mode = "enforce"
    snapshot_id = str(extra.get("config_snapshot_id") or "")
    policy_fingerprint = str(extra.get("config_policy_fingerprint") or "")
    workspace = Path(
        str(extra.get("workdir") or os.getcwd())
    ).expanduser().resolve(strict=False)
    try:
        from .sandbox import tool_should_run_in_sandbox

        sandboxed = tool_should_run_in_sandbox(tool_name, context)
    except Exception:
        sandboxed = False
    cwd = _policy_path(
        str(requested_workdir or workspace),
        workspace=workspace,
        cwd=workspace,
        sandboxed=sandboxed,
    )
    fingerprint = _command_fingerprint(command)
    if _WINDOWS and not sandboxed:
        # A POSIX lexer cannot approve cmd.exe expansion, redirection or
        # executable resolution. Job Objects do not enforce those permissions.
        return CommandPolicyDecision(
            allowed=mode != "enforce",
            code=("command.denied.windows_host_shell" if mode == "enforce"
                  else f"command.{mode}.windows_host_shell_unclassified"),
            reason=("当前强制审批策略无法安全判断 Windows 本地 shell 命令，未执行。"
                    "请使用文件工具、已配置的 MCP 或受支持的隔离环境。"
                    if mode == "enforce" else
                    "Windows 本地 shell 命令未经过安全分类；当前策略不拦截。"),
            mode=mode,
            categories=("unclassified",),
            executables=(),
            command_fingerprint=fingerprint,
            workdir=cwd.as_posix(),
            snapshot_id=snapshot_id,
            policy_fingerprint=policy_fingerprint,
            violations=("windows_host_shell",),
        )
    if len(str(command or "")) > 20_000:
        return CommandPolicyDecision(
            allowed=mode != "enforce",
            code=(
                "command.denied.too_long"
                if mode == "enforce"
                else "command.audit.too_long"
            ),
            reason="命令超过 20,000 字符的安全判断上限，未执行。",
            mode=mode,
            categories=("unclassified",),
            executables=(),
            command_fingerprint=fingerprint,
            workdir=cwd.as_posix(),
            snapshot_id=snapshot_id,
            policy_fingerprint=policy_fingerprint,
            violations=("too_long",),
        )

    try:
        categories, executables, parsed_segments = _classify(
            command,
            background=background,
        )
    except ValueError as exc:
        return CommandPolicyDecision(
            allowed=mode != "enforce",
            code="command.denied.parse" if mode == "enforce" else "command.audit.parse",
            reason=str(exc),
            mode=mode,
            categories=("unclassified",),
            executables=(),
            command_fingerprint=fingerprint,
            workdir=cwd.as_posix(),
            snapshot_id=snapshot_id,
            policy_fingerprint=policy_fingerprint,
            violations=("parse",),
        )

    violations: list[str] = []
    path_checks: list[dict[str, Any]] = []
    if not _inside(cwd, workspace):
        violations.append("external_workdir")
        path_checks.append(
            {
                "path_fingerprint": hashlib.sha256(cwd.as_posix().encode()).hexdigest(),
                "operation": "execute",
                "inside_workspace": False,
            }
        )

    for raw_path, operation in _path_tokens(parsed_segments, categories):
        resolved = _policy_path(
            raw_path,
            workspace=workspace,
            cwd=cwd,
            sandboxed=sandboxed,
        )
        inside = _inside(resolved, workspace)
        path_checks.append(
            {
                "path_fingerprint": hashlib.sha256(
                    str(raw_path).encode("utf-8")
                ).hexdigest(),
                "operation": operation,
                "inside_workspace": inside,
            }
        )
        if inside:
            try:
                from .sandbox.workspace_protection import (
                    hidden_workspace_paths,
                    read_only_workspace_paths,
                    workspace_path_matches,
                )

                relative = resolved.relative_to(workspace).as_posix()
                if workspace_path_matches(relative, hidden_workspace_paths()):
                    violations.append("hidden_workspace_path")
                if operation == "write" and workspace_path_matches(
                    relative, read_only_workspace_paths()
                ):
                    violations.append("read_only_workspace_path")
            except Exception:
                pass
        elif operation == "write" and not bool(policy.get("allow_external_write", False)):
            violations.append("external_write")
        elif operation in {"read", "execute"} and not bool(
            policy.get("allow_external_read", False)
        ):
            violations.append("external_read")

    write_action = _permission_action(context, "write")
    network_action = _permission_action(context, "network")
    execution_profile = str(extra.get("execution_profile") or "").strip()
    if "workspace_write" in categories:
        if not bool(policy.get("allow_workspace_write", True)):
            violations.append("workspace_write_policy")
        if write_action == "deny" or not profile_allows_tool(execution_profile, "write"):
            violations.append("write_permission")
        if write_action == "ask" and "write" not in approval_granted:
            violations.append("write_approval")
    if "network" in categories:
        if not bool(policy.get("allow_network", True)):
            violations.append("network_policy")
        if network_action == "deny":
            violations.append("network_permission")
        if network_action == "ask" and "network" not in approval_granted:
            violations.append("network_approval")
        if _effective_network(context, tool_name) == "none":
            violations.append("sandbox_network_none")
    if "dependency_install" in categories and not bool(
        policy.get("allow_dependency_install", True)
    ):
        violations.append("dependency_install_policy")
    if "system_operation" in categories and not bool(
        policy.get("allow_system_operations", False)
    ):
        violations.append("system_operation_policy")
    if "background_process" in categories and not bool(
        policy.get("allow_background_process", True)
    ):
        violations.append("background_process_policy")

    # Arbitrary inline code can otherwise become an obvious permission bypass.
    if "dynamic_code" in categories:
        if write_action != "allow" and "write" not in approval_granted:
            violations.append("dynamic_code_write_capability")
        if network_action != "allow" and "network" not in approval_granted:
            violations.append("dynamic_code_network_capability")

    unique_violations = tuple(sorted(set(violations)))
    enforced = mode == "enforce" and bool(unique_violations)
    if enforced:
        primary = unique_violations[0]
        code = f"command.denied.{primary}"
        reason = (
            "命令未执行：当前任务的命令策略不允许该操作。"
            f" 拒绝规则：{', '.join(unique_violations)}。"
        )
    elif unique_violations and mode == "audit":
        code = "command.audit.allowed"
        reason = "命令处于审计模式；发现策略风险但未拦截。"
    elif mode == "disabled":
        code = "command.disabled.allowed"
        reason = "命令策略已关闭；本次只保留最小审计记录。"
    else:
        code = "command.allowed"
        reason = "命令符合当前任务固定的权限和沙盒策略。"
    return CommandPolicyDecision(
        allowed=not enforced,
        code=code,
        reason=reason,
        mode=mode,
        categories=tuple(sorted(categories)),
        executables=tuple(executables),
        command_fingerprint=fingerprint,
        workdir=cwd.as_posix(),
        snapshot_id=snapshot_id,
        policy_fingerprint=policy_fingerprint,
        violations=unique_violations,
        path_checks=tuple(path_checks),
        approval_granted=tuple(sorted(set(approval_granted))),
    )


class CommandPolicyAudit:
    _lock = threading.RLock()

    @classmethod
    def record(cls, decision: CommandPolicyDecision, context: ToolContext, tool_name: str) -> None:
        payload = decision.to_dict()
        payload["tool_name"] = str(tool_name or "")
        payload["session_id"] = str(context.session_id or "")
        payload["recorded_at"] = time.time()
        # Never write the command itself: fingerprint + executable/category are
        # enough to correlate a decision without persisting user secrets.
        workspace = Path(
            str((context.extra or {}).get("control_workdir") or (context.extra or {}).get("workdir") or os.getcwd())
        ).expanduser().resolve(strict=False)
        path = workspace / ".tcode" / "audit" / "command_policy.jsonl"
        try:
            with state_thread_lock(cls._lock):
                path.parent.mkdir(parents=True, exist_ok=True)
                append_locked_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass
        TelemetryGateway.emit(
            "command.policy.decision",
            payload,
            session_id=str(context.session_id or ""),
            request_id=str((context.extra or {}).get("request_id") or ""),
            tenant_id=str((context.extra or {}).get("tenant_id") or ""),
            user_id=str((context.extra or {}).get("user_id") or ""),
            workspace_id=str((context.extra or {}).get("workspace_id") or ""),
        )


async def authorize_command(
    command: str,
    *,
    context: ToolContext,
    tool_name: str,
    requested_workdir: Optional[str] = None,
    background: bool = False,
) -> CommandPolicyDecision:
    decision = evaluate_command_policy(
        command,
        context=context,
        tool_name=tool_name,
        requested_workdir=requested_workdir,
        background=background,
    )
    approvals: list[str] = []
    if decision.mode == "enforce":
        ask_callback = (context.extra or {}).get("__permission_ask__")
        for permission, marker in (
            ("write", "write_approval"),
            ("network", "network_approval"),
        ):
            if marker not in decision.violations:
                continue
            if not callable(ask_callback):
                continue
            approved = await context.ask(
                permission,
                [f"{tool_name}: {', '.join(decision.categories)}"],
            )
            if approved:
                approvals.append(permission)
        if approvals:
            decision = evaluate_command_policy(
                command,
                context=context,
                tool_name=tool_name,
                requested_workdir=requested_workdir,
                background=background,
                approval_granted=tuple(approvals),
            )
    CommandPolicyAudit.record(decision, context, tool_name)
    return decision


def denied_command_result(
    decision: CommandPolicyDecision,
    *,
    title: str,
) -> ToolResult:
    compatibility_message = ""
    if "write_permission" in decision.violations:
        compatibility_message = (
            " Current runtime does not grant write permission."
        )
    recoverable_read_denial = (
        set(decision.categories).issubset({"read_only"})
        and bool(decision.violations)
        and set(decision.violations).issubset(
            {"external_read", "external_workdir", "hidden_workspace_path"}
        )
    )
    return ToolResult(
        title=title,
        output="",
        error=f"[{decision.code}] {decision.reason}{compatibility_message}",
        metadata={
            "execution_skipped": True,
            # A denied read remains denied, but the Agent can safely retry with
            # an allowed workspace path.  Treating it as terminal incorrectly
            # changed otherwise successful async tasks to "blocked".
            "terminal_tool_error": not recoverable_read_denial,
            "retryable": recoverable_read_denial,
            "should_end_turn": not recoverable_read_denial,
            "blocked_by_write_permission": (
                "write_permission" in decision.violations
            ),
            "denied_tool": (
                "write" if "write_permission" in decision.violations else "command"
            ),
            "command_policy": decision.to_dict(),
            "execution_environment": {
                "location": "not_executed",
                "label": "Not executed",
                "sandboxed": False,
            },
        },
    )


def replay_command_policy(
    snapshot: Any,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replay classification/decisions against a historical snapshot, never execute."""

    results: list[dict[str, Any]] = []
    for index, raw_case in enumerate(cases[:200]):
        case = raw_case if isinstance(raw_case, dict) else {"command": str(raw_case)}
        context = SimpleNamespace(
            session_id=f"replay:{getattr(snapshot, 'snapshot_id', '')}",
            message_id=f"case:{index}",
            agent="replay",
            abort=None,
            extra={
                "workdir": getattr(snapshot, "workdir", os.getcwd()),
                "control_workdir": getattr(snapshot, "workdir", os.getcwd()),
                "execution_profile": str(case.get("execution_profile") or "standard"),
                "agent_permission": dict(case.get("permission") or {}),
                "command_policy": dict(getattr(snapshot, "command_policy", {}) or {}),
                "config_snapshot_id": getattr(snapshot, "snapshot_id", ""),
                "config_policy_fingerprint": getattr(snapshot, "policy_fingerprint", ""),
            },
        )
        decision = evaluate_command_policy(
            str(case.get("command") or ""),
            context=context,
            tool_name=str(case.get("tool_name") or "bash"),
            requested_workdir=case.get("workdir"),
            background=bool(case.get("background", False)),
            policy_override=dict(getattr(snapshot, "command_policy", {}) or {}),
        )
        results.append({"case": index, **decision.to_dict()})
    return results


__all__ = [
    "CommandPolicyAudit",
    "CommandPolicyDecision",
    "authorize_command",
    "denied_command_result",
    "evaluate_command_policy",
    "replay_command_policy",
]
