"""submit_job proxy helpers for MCP tool calls.

This module owns only the simplified `submit_job` adapter:
`target_server` + `target_tool` + target args -> Symphony/bridge LOCAL queue.
The actual LOCAL issue creation, submit payload construction, and dedupe live in
`_symphony.py` so submit_job proxy and direct routed MCP calls share one path.
"""

from __future__ import annotations

import difflib
import json
import copy
import os
from pathlib import Path
from typing import Any, Optional

from ..tools.base import ToolContext
from ..types import ToolResult
from ..delegation import build_permission_delegation_metadata
from ._utils import (
    _agents_allowed_to_route_server,
    _async_submit_should_end_turn,
    _check_human_review_required,
    _context_control_workdir,
    _extract_local_identifier_from_job_protocol_args,
    _find_issue_by_identifier,
    _get_agent_allowed_routed_servers,
    _get_turn_guard,
    _list_server_tool_names,
    _load_issue_store,
    _map_issue_state_to_job_status,
    _maybe_parse_json_object,
    _normalize_routed_tool_args,
    _set_turn_guard,
    _symphony_memory_issues_path,
    _turn_guard_submission_lock,
)


class _ProxyTool:
    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        description: str = "",
        input_schema: Optional[dict] = None,
    ):
        self.server_name = server_name
        self._tool_name = tool_name
        self._description = description
        self._input_schema = input_schema or {}

    @property
    def description(self) -> str:
        return self._description

    async def _enqueue_symphony_local_issue(
        self,
        *,
        target_server: str,
        target_tool: str,
        tool_args: dict,
        context: ToolContext,
    ) -> dict[str, Any]:
        from ._symphony import enqueue_symphony_local_issue

        return await enqueue_symphony_local_issue(
            server_name=target_server,
            tool_name=target_tool,
            normalized_args=tool_args,
            context=context,
            description=self.description,
            input_schema=self._input_schema,
        )


async def proxy_submit_job(
    *,
    server_name: str,
    tool_name: str,
    description: str,
    input_schema: dict,
    args: dict,
    context: ToolContext,
) -> Optional[ToolResult]:
    proxy = _ProxyTool(
        server_name=server_name,
        tool_name=tool_name,
        description=description,
        input_schema=input_schema,
    )
    return await _proxy_submit_job(proxy, args, context)


def proxy_local_issue_job_query(
    *,
    server_name: str,
    tool_name: str,
    args: dict,
    context: ToolContext,
) -> Optional[ToolResult]:
    proxy = _ProxyTool(server_name=server_name, tool_name=tool_name)
    return _proxy_local_issue_job_query(proxy, args, context)


def _same_path(left: str, right: str) -> bool:
    try:
        return (
            Path(left).expanduser().resolve(strict=False)
            == Path(right).expanduser().resolve(strict=False)
        )
    except Exception:
        return str(left or "").strip() == str(right or "").strip()


def _extract_contract_payload(result: Any) -> dict[str, Any]:
    """Reject MCP errors before unwrapping contract metadata."""
    if not isinstance(result, dict):
        return {}
    if result.get("isError", False) is not False:
        raise ValueError("MCP契约查询返回错误或错误标记无效，未接受声明或参数校验")
    for key in ("structuredContent", "result", "data"):
        nested = result.get(key)
        if isinstance(nested, dict):
            if set(nested) == {"result"} and isinstance(nested.get("result"), dict):
                return nested["result"]
            return nested
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            parsed = _maybe_parse_json_object(str(item.get("text") or ""))
            if isinstance(parsed, dict):
                return parsed
    return result


def _target_contract_validation_enabled() -> bool:
    raw = str(os.environ.get("TCODE_TARGET_CONTRACT_VALIDATION", "true") or "true")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _validate_target_args_contract(
    *,
    target_server: str,
    target_tool: str,
    tool_args: dict[str, Any],
    context: Optional[ToolContext] = None,
) -> Optional[ToolResult]:
    """Validate final target arguments through the lightweight job contract tool."""
    if not _target_contract_validation_enabled():
        return None

    try:
        from .manager_auto import MCPManager
        from ._utils import _context_workspace_claims
        claims = _context_workspace_claims(context) if target_server.startswith('plugin-') else {}
        from .dynamic_headers import snapshot
        business_args, _ = snapshot(target_server, tool_args, context)
        validation_args = business_args
        if target_server.startswith('plugin-') and context is not None:
            from ..plugins.async_targets import find_target
            from ..workspace.async_protein_run import resolve_context_input_path
            from .custom_targets import pinned_target
            declaration = pinned_target(context, target_server) or find_target(_context_control_workdir(context), target_server,
                                      owner=f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}")
            if declaration is None:
                raise ValueError('plugin target is not active in this owner workspace')
            entry = next(t for t in declaration['tools'] if t['name'] == target_tool)
            validation_args = copy.deepcopy(business_args)
            # Goal review checks paths without uploading before approval.
            for field in ([] if declaration.get('origin') == 'mcp-config' else entry.get('files', [])):
                parts = [p.replace('~1', '/').replace('~0', '~') for p in field['pointer'][1:].split('/')]
                parent = validation_args
                try:
                    for part in parts[:-1]:
                        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
                    value = parent[key]
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if isinstance(value, str) or (isinstance(value, dict) and value.get('path')):
                    path = resolve_context_input_path(context, value.get('path') if isinstance(value, dict) else value)
                    if path is None or not (path.is_file() if field['kind'] == 'file' else path.is_dir()):
                        raise ValueError('declared input path is not accessible in the workspace')
                    parent[key] = ({'kind': 'artifact', 'artifact_id': 'art_validation'} if field['kind'] == 'file'
                                   else {'kind': 'artifact_bundle', 'bundle_id': 'bundle_validation'})
            if declaration.get('origin') == 'mcp-config':
                from .custom_targets import validation_arguments
                validation_args = validation_arguments(context, target_server, target_tool, business_args)

        if not MCPManager.is_initialized():
            await MCPManager.initialize()
        raw = await MCPManager.call_tool(
            "job-protocol-mcp",
            "describe_target_tool",
            {
                "input": {
                    "target_server": target_server,
                    "target_tool": target_tool,
                    "detail": "compact",
                    "fields": [],
                    "arguments": validation_args,
                    **claims,
                    **({'issuer': 'tcode', 'subject': claims.get('user_id', '')} if claims else {}),
                }
            },
        )
        payload = _extract_contract_payload(raw)
    except Exception as exc:
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output="",
            error=(
                "Target tool contract validation is unavailable; the async task was not queued. "
                f"Restart or check job-protocol-mcp: {exc}"
            ),
            metadata={
                "validation": "target_contract_unavailable",
                "target_server": target_server,
                "target_tool": target_tool,
            },
        )

    if payload.get('ok') and isinstance(payload.get('parameter_sources'), dict):
        from ._utils import _check_context_workspace_conflicts
        try:
            _check_context_workspace_conflicts(target_server, business_args, context,
                                              tool_name=target_tool, sources=payload['parameter_sources'])
        except ValueError as exc:
            return ToolResult(title=f'{target_server}.{target_tool}', output='', error=str(exc),
                              metadata={'validation': 'target_argument_conflict', 'retryable': False})

    if not payload.get("ok", False):
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output="",
            error=(
                "Target tool contract is unavailable or unknown; the async task was not queued: "
                f"{payload.get('message') or payload.get('error') or 'unknown contract error'}"
            ),
            metadata={
                "validation": "target_contract_error",
                "target_server": target_server,
                "target_tool": target_tool,
                "contract": payload,
            },
        )

    validation = payload.get("validation")
    if not isinstance(validation, dict):
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output="",
            error="Target tool contract returned no validation result; the async task was not queued.",
            metadata={
                "validation": "target_contract_malformed",
                "target_server": target_server,
                "target_tool": target_tool,
                "contract_schema_hash": payload.get("schema_hash", ""),
            },
        )
    if validation.get("valid") is True:
        from ..workspace.input_resources import PUBLISHED_INPUT_TARGETS, pin_input_file_contract
        if (target_server, target_tool) in PUBLISHED_INPUT_TARGETS:
            try:
                pin_input_file_contract(context, target_server, target_tool, payload)
            except ValueError as exc:
                return ToolResult(title="计算输入声明不可用", output="", error=str(exc))
        return None

    errors = validation.get("errors") if isinstance(validation.get("errors"), list) else []
    summaries: list[str] = []
    for item in errors[:6]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "arguments")
        message = str(item.get("message") or item.get("type") or "invalid value")
        summaries.append(f"{field}: {message}")
    return ToolResult(
        title=f"{target_server}.{target_tool}",
        output="",
        error=(
            "Target arguments failed pre-submit validation; the async task was not queued. "
            + ("; ".join(summaries) or "Invalid target arguments.")
        ),
        metadata={
            "validation": "target_arguments_invalid",
            "submission_state": "submission_not_sent",
            "target_server": target_server,
            "target_tool": target_tool,
            "contract_schema_hash": payload.get("schema_hash", ""),
            "errors": errors,
        },
    )


def _candidate_local_issue_workdirs(context: ToolContext) -> list[str]:
    candidates: list[str] = []

    def _append(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if any(_same_path(existing, text) for existing in candidates):
            return
        candidates.append(text)

    extra = getattr(context, "extra", None)
    if not isinstance(extra, dict):
        extra = {}

    _append(extra.get("control_workdir"))
    workspace = extra.get("agent_workspace")
    if isinstance(workspace, dict):
        _append(workspace.get("control_workdir"))
    _append(extra.get("workspace_root"))
    _append(extra.get("workdir"))

    server_workdir = str(os.environ.get("TCODE_WORKDIR") or "").strip()
    tenant_id = str(extra.get("tenant_id", "") or "").strip()
    user_id = str(extra.get("user_id", "") or "").strip()
    workspace_id = str(extra.get("workspace_id", "") or "").strip()
    if server_workdir and tenant_id and user_id and workspace_id:
        configured_root = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
        workspace_root = Path(configured_root) if configured_root else Path(server_workdir) / "workdirs"
        if not workspace_root.is_absolute():
            workspace_root = Path(server_workdir) / workspace_root
        _append(workspace_root / tenant_id / user_id / workspace_id)

    workdir = _context_control_workdir(context)
    _append(workdir)

    # Workspace-scoped tracker DBs are often under
    # <server-workdir>/workdirs/<tenant>/<user>/<workspace>. Also check the server
    # workdir because older/local flows can materialize tasks there.
    if workdir:
        try:
            current = Path(workdir).expanduser().resolve(strict=False)
            for parent in (current, *current.parents):
                if parent.name == "workdirs":
                    _append(parent.parent.as_posix())
                    break
        except Exception:
            pass

    _append(server_workdir)
    try:
        _append(Path(__file__).resolve().parents[2].as_posix())
    except Exception:
        pass
    return candidates


def _find_local_issue_for_job_query(
    identifier: str,
    context: ToolContext,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    checked: list[str] = []
    for workdir in _candidate_local_issue_workdirs(context):
        issue_path = _symphony_memory_issues_path(workdir)
        checked.append(str(issue_path))
        store = _load_issue_store(issue_path)
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            continue
        issue = _find_issue_by_identifier(issues, identifier)
        if issue is not None:
            return issue, checked
    return None, checked


def _simplified_submit_args(args: dict) -> tuple[Optional[dict], Optional[ToolResult]]:
    raw_args = dict(args or {})
    # Some providers wrap the entire call in one JSON-encoded arguments field.
    # Unwrap only that unambiguous envelope; target-level arguments stay intact.
    if set(raw_args) == {'arguments'}:
        envelope = raw_args['arguments']
        for _ in range(2):
            if not isinstance(envelope, str):
                break
            try:
                envelope = json.loads(envelope)
            except (ValueError, TypeError):
                break
        if isinstance(envelope, dict):
            raw_args = envelope
    for key in ("input", "input_config", "payload"):
        if key in raw_args:
            raw_args[key] = _maybe_parse_json_object(raw_args[key])

    raw = (
        raw_args.get("input")
        or raw_args.get("input_config")
        or raw_args.get("payload")
        or raw_args
    )
    if isinstance(raw, str):
        raw = _maybe_parse_json_object(raw)
    if not isinstance(raw, dict):
        return None, None

    # Accept the legacy split shape produced by older skills/docs:
    # {"input_config": {"target_server": "...", "target_tool": "..."}, "input": {...args...}}
    # and normalize it to the simplified flat shape.
    if isinstance(raw_args.get("input_config"), dict) and isinstance(raw_args.get("input"), dict):
        input_config = dict(raw_args["input_config"])
        input_payload = dict(raw_args["input"])
        merged = dict(input_payload)
        for key in (
            "target_server",
            "target_tool",
            "server_name",
            "tool_name",
            "server",
            "tool",
            "run_id",
            "workdir",
            "task_type",
            "expected_artifacts",
            "timeout_seconds",
            "retry_policy",
            "continuation_policy",
        ):
            if key in input_config:
                merged[key] = input_config[key]
        raw = merged

    route_keys = {
        "target_server",
        "target_tool",
        "server_name",
        "tool_name",
        "server",
        "tool",
    }
    nested_keys = ("input", "input_config", "payload")
    route_in_raw = any(key in raw for key in route_keys)
    nested_key = next(
        (key for key in nested_keys if isinstance(raw.get(key), dict)),
        "",
    )
    if nested_key:
        nested = dict(raw[nested_key])
        route_in_nested = any(key in nested for key in route_keys)
        if route_in_raw and not route_in_nested:
            merged = dict(nested)
            for key, value in raw.items():
                if key not in nested_keys:
                    merged[key] = value
            raw = merged
        elif not route_in_raw and route_in_nested:
            raw = nested

    if "command" in raw:
        if os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"):
            return None, ToolResult(title="异步提交", output="", error="内置 Job Protocol 不接受 command 字段，未执行任务。")
        # Legacy external job-protocol payloads keep their existing path.
        return None, None

    work = dict(raw)
    target_server = ""
    target_tool = ""
    for key in ("target_server", "server_name", "server"):
        if key in work:
            target_server = str(work.pop(key) or "").strip()
            break
    for key in ("target_tool", "tool_name", "tool"):
        if key in work:
            target_tool = str(work.pop(key) or "").strip()
            break

    if not target_server or not target_tool:
        return None, ToolResult(
            title="submit_job",
            output="",
            error=(
                "Simplified submit_job requires 'target_server' and 'target_tool'. "
                'Example: {"target_server": "simple-demo-mcp", "target_tool": "echo_text", '
                '"text": "hello", "prefix": "demo"}'
            ),
        )

    continuation_policy = str(work.pop("continuation_policy", "") or "").strip().lower()
    if continuation_policy not in {"", "notify", "auto", "store_only"}:
        return None, ToolResult(
            title="submit_job",
            output="",
            error=(
                "continuation_policy must be one of: notify, auto, store_only"
            ),
        )

    # Remove submit_job schema fields that are not target tool arguments.
    for key in (
        "run_id",
        "workdir",
        "task_type",
        "expected_artifacts",
        "timeout_seconds",
        "retry_policy",
    ):
        work.pop(key, None)

    call_values = work.pop('_tcode_context', None)
    # These built-in demo tools have no business parameter named arguments.
    demo_envelope = target_server == 'simple-demo-mcp' and target_tool in {'echo_text', 'sleep_then_echo'}
    if demo_envelope and 'arguments' in work and set(work) <= {'arguments', 'workspace_id'}:
        envelope = _maybe_parse_json_object(work['arguments'])
        siblings = {key: value for key, value in work.items() if key != 'arguments'}
        if isinstance(envelope, dict) and not siblings.keys() & envelope.keys():
            work = {**envelope, **siblings}
    elif target_server.startswith('plugin-') and set(work) == {'arguments'} and isinstance(work['arguments'], dict):
        work = work['arguments']
    if call_values is not None:
        work = {**work, '_tcode_context': call_values}
    return {
        "target_server": target_server,
        "target_tool": target_tool,
        "target_args": work,
        "continuation_policy": continuation_policy,
    }, None


async def _validate_target_tool(
    *,
    target_server: str,
    target_tool: str,
) -> Optional[ToolResult]:
    try:
        available_tools = await _list_server_tool_names(target_server)
    except Exception:
        available_tools = set()

    if not available_tools or target_tool in available_tools:
        return None

    suggestions = difflib.get_close_matches(
        target_tool, sorted(available_tools), n=3, cutoff=0.6
    )
    hint = f" 你可能想用: {', '.join(suggestions)}" if suggestions else ""
    return ToolResult(
        title="submit_job",
        output="",
        error=(
            f"Unknown target_tool '{target_tool}' for server '{target_server}'."
            f" Available tools: {', '.join(sorted(available_tools))}.{hint}"
        ),
        metadata={
            "validation": "target_tool_not_found",
            "target_server": target_server,
            "target_tool": target_tool,
            "available_tools": sorted(available_tools),
            "suggestions": suggestions,
        },
    )


async def _register_async_completion_watcher(
    *,
    identifier: str,
    target_server: str,
    target_tool: str,
    context: ToolContext,
    protein_run: Optional[dict[str, Any]] = None,
    continuation_policy: str = "notify",
    submission_arguments: Optional[dict[str, Any]] = None,
) -> None:
    if not identifier:
        return
    try:
        from ..runtime.async_watcher import (
            is_injected,
            register as watcher_register,
            start_watcher,
        )

        if is_injected(identifier):
            return
        if protein_run:
            try:
                from ..workspace.async_protein_run import needs_terminal_recovery

                if not needs_terminal_recovery(protein_run, identifier):
                    return
            except Exception:
                pass
        workdir = _context_control_workdir(context)
        context_extra = (
            dict(context.extra)
            if isinstance(getattr(context, "extra", None), dict)
            else {}
        )
        parent_session_id = str(context_extra.get("parent_session_id") or "").strip()
        origin_turn_id = str(context_extra.get("turn_id") or "").strip()
        if not origin_turn_id:
            for message in reversed(list(getattr(context, "messages", []) or [])):
                if str(getattr(message, "role", "") or "").strip().lower() != "user":
                    continue
                origin_turn_id = str(getattr(message, "id", "") or "").strip()
                if origin_turn_id:
                    break
        from ..runtime.async_resume import capture_task_context
        task_context = await capture_task_context(context)
        if submission_arguments is not None:
            task_context["submission_arguments"] = copy.deepcopy(submission_arguments)
        watcher_register(
            job_identifier=identifier,
            session_id=(
                parent_session_id
                or str(getattr(context, "session_id", "") or "").strip()
            ),
            target_server=str(target_server or "").strip(),
            target_tool=str(target_tool or "").strip(),
            workdir=workdir,
            protein_run=dict(protein_run or {}),
            origin_turn_id=origin_turn_id,
            origin_request_id=str(
                context_extra.get("origin_request_id")
                or context_extra.get("request_id")
                or ""
            ).strip(),
            continuation_policy=continuation_policy,
            task_context=task_context,
        )
        await start_watcher(
            workdir=workdir,
            agent_event_emitter=(context.extra or {}).get("__agent_event_emit__")
            if getattr(context, "extra", None)
            else None,
        )
    except Exception:
        pass


async def _proxy_submit_job(
    self, args: dict, context: ToolContext
) -> Optional[ToolResult]:
    """Queue simplified submit_job requests through the single LOCAL issue path."""
    parsed, error = _simplified_submit_args(args)
    if error is not None:
        return error
    if parsed is None:
        if callable((getattr(context, "extra", None) or {}).get("__goal_checkpoint__")):
            return ToolResult(title="目标计算申请", output="请提供明确的 target_server、target_tool 和参数后申请审核；未执行计算。", error="goal_review_requires_target")
        return None

    target_server = str(parsed["target_server"])
    target_tool = str(parsed["target_tool"])
    from ..runtime.async_resume import assert_submission_allowed
    try:
        assert_submission_allowed(target_server, target_tool)
    except PermissionError as exc:
        return ToolResult(title="恢复已有结果", output="", error=str(exc), metadata={"async_resume_replay_blocked": True})
    requested_registry_id = target_server.startswith('plugin-')
    from .custom_targets import configured_alias
    target_server_key = configured_alias(target_server, context).lower()
    try:
        assert_submission_allowed(target_server_key, target_tool)
    except PermissionError as exc:
        return ToolResult(title="恢复已有结果", output="", error=str(exc), metadata={"async_resume_replay_blocked": True})
    from ..runtime.async_completion import normalize_continuation_policy

    context_extra = (
        context.extra
        if isinstance(getattr(context, "extra", None), dict)
        else {}
    )
    trace_context = (
        context_extra.get("tool_trace_context")
        if isinstance(context_extra.get("tool_trace_context"), dict)
        else {}
    )
    continuation_policy = normalize_continuation_policy(
        parsed.get("continuation_policy")
        or context_extra.get("async_continuation_policy"),
        workflow_owned=bool(str(trace_context.get("workflow_run_id") or "").strip()),
    )
    context_extra["async_continuation_policy"] = continuation_policy
    context.extra = context_extra

    allowed_servers = _get_agent_allowed_routed_servers(context)
    if allowed_servers is not None and target_server_key not in allowed_servers:
        allowed_text = ", ".join(sorted(allowed_servers)) if allowed_servers else "none"
        route_agents = _agents_allowed_to_route_server(
            target_server,
            _context_control_workdir(context),
        )
        preferred_delegate = route_agents[0] if route_agents else ""
        return ToolResult(
            title="submit_job",
            output="",
            error=(
                f"Agent '{getattr(context, 'agent', '')}' is not allowed to route MCP server "
                f"'{target_server}'. Allowed servers: {allowed_text}."
                + (
                    f" Delegate through one of: {', '.join(route_agents)}."
                    if route_agents
                    else ""
                )
            ),
            metadata={
                "validation": "target_server_not_allowed",
                "agent": getattr(context, "agent", ""),
                "target_server": target_server,
                "allowed_servers": sorted(allowed_servers),
                **build_permission_delegation_metadata(
                    allowed_agents=route_agents,
                    preferred_agent=preferred_delegate,
                    match_tokens=(target_server,),
                ),
                "retryable": False,
                "should_end_turn": False,
            },
        )

    # Check permission against the configured, user-visible alias FIRST. The
    # opaque immutable registry identity is never an extra routing permission.
    from .custom_targets import resolve as resolve_custom_target
    try:
        target_server = resolve_custom_target(target_server, context)
    except ValueError as exc:
        return ToolResult(title="输入文件传输", output=str(exc), error="custom_mcp_transfer_unavailable")

    tool_validation = await _validate_target_tool(
        target_server=target_server,
        target_tool=target_tool,
    )
    if tool_validation is not None:
        return tool_validation

    tool_args = _normalize_routed_tool_args(
        dict(parsed.get("target_args") or {}),
        target_tool,
    )
    from ._utils import _check_context_workspace_conflicts
    try:
        _check_context_workspace_conflicts(target_server, tool_args, context, tool_name=target_tool)
    except ValueError as exc:
        return ToolResult(title='MCP 参数归属', output='', error=str(exc),
                          metadata={'validation': 'runtime_context_conflict', 'retryable': False})
    if target_server.startswith('plugin-'):
        tool_args = dict(parsed.get('target_args') or {})
        from .custom_targets import pinned_target
        custom = pinned_target(context, target_server)
        # The legacy parser already unwraps registry IDs. Only a configured
        # alias still has the submit envelope, not the tool's own arguments field.
        from .dynamic_headers import split
        business, values = split(tool_args)
        if custom and not requested_registry_id and set(business) == {'arguments'} and isinstance(business['arguments'], dict):
            tool_args = {**business['arguments'], **({'_tcode_context': values} if values else {})}
        if custom and target_tool not in {t['name'] for t in custom['tools']}:
            return ToolResult(title="输入文件传输", output="此工具尚未在文件传输配置中启用，请先选择工具并保存。", error="custom_mcp_tool_not_configured")
    user_message = ""
    from .dynamic_headers import rules_for, snapshot
    try:
        # The demo tools have no business field named _tcode_context.
        if (target_server == 'simple-demo-mcp'
                and target_tool in {'echo_text', 'sleep_then_echo'}
                and '_tcode_context' in tool_args
                and not rules_for(target_server, context)):
            tool_args = {key: value for key, value in tool_args.items() if key != '_tcode_context'}
        snapshot(target_server, tool_args, context)
    except ValueError as exc:
        return ToolResult(title='MCP 调用上下文', output='', error=str(exc))
    if context and getattr(context, "extra", None):
        user_message = str(context.extra.get("user_message", "") or "")

    from .custom_targets import authorize_inputs
    if not await authorize_inputs(context, target_server, target_tool, tool_args):
        return ToolResult(title="输入文件传输", output="未获准读取输入文件，未上传或提交计算。", error="input_read_denied")

    prepared = dict(self=self, context=context, target_server=target_server, target_tool=target_tool,
                    target_server_key=target_server_key, tool_args=tool_args, user_message=user_message,
                    continuation_policy=continuation_policy)
    from .custom_targets import pinned_target, submission_guard_arguments
    if not pinned_target(context, target_server):
        return await _submit_authorized_job(**prepared)
    # Reuse the existing same-turn lock/cache, but key it BEFORE upload assigns
    # fresh Artifact IDs. This lock also covers parallel calls and review enqueue.
    stable_args = submission_guard_arguments(context, tool_args)
    from .dynamic_headers import fingerprint_arguments
    stable_args = fingerprint_arguments(target_server, stable_args, context, values_from=tool_args)
    guard_server = 'custom-input:' + target_server  # Separate from the post-upload guard key space.
    async with _turn_guard_submission_lock(context, guard_server, target_tool, stable_args):
        previous = _get_turn_guard(context, guard_server, target_tool, stable_args)
        if previous is not None:
            return ToolResult(title='异步请求已受理', output=previous['output'],
                              metadata={**previous['metadata'], 'route_mode': 'turn-guard-deduped'})
        result = await _submit_authorized_job(**prepared)
        if not result.error and (result.metadata.get('accepted_async') or result.metadata.get('human_review_required')
                                 or result.metadata.get('submit_proxy') == 'queued'):
            _set_turn_guard(context, guard_server, target_tool,
                            {'output': result.output, 'metadata': copy.deepcopy(result.metadata)}, stable_args)
        return result


async def _submit_authorized_job(self, context, target_server, target_tool, target_server_key,
                                 tool_args, user_message, continuation_policy):
    from .custom_targets import authorize_inputs

    goal_review = None
    if callable((getattr(context, "extra", None) or {}).get("__goal_checkpoint__")):
        from ..session.manager import SessionManager
        from ..server.goal_mode import public_goal
        session = await SessionManager.get(context.session_id)
        goal = public_goal(session)
        origin = str((context.extra or {}).get("request_id") or "")
        if not goal or goal['status'] != 'running' or origin not in goal.get('run_ids', []):
            return ToolResult(title="目标计算申请", output="目标已暂停、结束或请求已过期，未提交计算。", error="goal_review_stale")
        invalid = await _validate_target_args_contract(target_server=target_server, target_tool=target_tool, tool_args=tool_args, context=context)
        if invalid is not None:
            return invalid
        goal_review = {"goal_id": goal['id'], "max_submissions": 1}

    review_check = ({"requires_review": True, "reason": "目标模式计算申请：仅批准所列参数的一次提交；重跑或变更参数须重新审核，资源成本未知时请先核对。", "trigger": "goal_computation_request"} if goal_review else await _check_human_review_required(
        target_server_key,
        target_tool,
        tool_args,
        user_message,
        context=context,
    ))
    review_decision = {
        "requires_review": bool(review_check.get("requires_review", False)),
        "reason": str(review_check.get("reason", "") or ""),
        "trigger": str(review_check.get("trigger", "") or ""),
        "check_type": str(review_check.get("check_type", "") or ""),
    }
    approved_override = review_check.get("approved_tool_args_override")
    if isinstance(approved_override, dict) and approved_override:
        tool_args = dict(approved_override)
        review_decision["approved_override_applied"] = True
    else:
        review_decision["approved_override_applied"] = False

    if review_decision['approved_override_applied'] and not await authorize_inputs(context, target_server, target_tool, tool_args):
        return ToolResult(title="输入文件传输", output="变更后的输入未获读取授权，未上传或提交。", error="input_read_denied")

    if review_check.get("requires_review", False):
        from ._review import enqueue_human_review_issue

        review_issue = await enqueue_human_review_issue(
            server_name=target_server,
            tool_name=target_tool,
            args=tool_args,
            context=context,
            review_check=review_check,
            review_data_extra={
                "review_decision": review_decision,
                "proxy_source": "job_protocol_submit",
                **({"custom_mcp_read_grants": getattr(context, '_custom_mcp_read_grants', []),
                    "custom_mcp_read_bindings": copy.deepcopy(getattr(context, '_custom_mcp_read_bindings', []))}
                   if getattr(context, '_custom_mcp_targets', {}).get(target_server) else {}),
                **({"goal_computation": goal_review} if goal_review else {}),
            },
        )
        if goal_review:
            from ..runtime.shared_tracker import SharedTracker
            from ..server.goal_computation import auto_approve_computation
            saved = SharedTracker.get_review(review_id=review_issue.get('id', ''), workdir=session.workdir)
            if saved and await auto_approve_computation(saved, session):
                return ToolResult(title="目标计算申请已自动批准", output="已按当前目标设置自动批准所列参数的一次提交，等待现有异步机制执行与回传；不要重复提交。", metadata={**review_issue, "status": "approved", "human_review_required": False, "should_end_turn": True, "auto_approved": True})
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output=(
                f"Human review required: {review_issue.get('identifier', 'created')} - "
                f"{review_check.get('reason', '需要人工确认')} "
                f"(trigger: {review_check.get('trigger', 'unknown')})"
            ),
            metadata={
                **review_issue,
                "human_review_required": True,
                "should_end_turn": True,
            },
        )

    from .artifact_boundary import prepare_mcp_artifact_arguments


    tool_args, lazy_artifact_uploads = await prepare_mcp_artifact_arguments(
        target_server,
        target_tool,
        tool_args,
        context,
    )
    from .custom_targets import pinned_target
    if pinned_target(context, target_server):
        context._custom_mcp_upload_bindings = {(target_server, target_tool): copy.deepcopy(lazy_artifact_uploads)}

    from ._symphony import validate_routed_request

    validation_error = validate_routed_request(
        server_name=target_server,
        tool_name=target_tool,
        normalized_args=tool_args,
        context=context,
    )
    if validation_error:
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output="",
            error=validation_error,
        )

    contract_error = await _validate_target_args_contract(
        target_server=target_server,
        target_tool=target_tool,
        tool_args=tool_args,
        context=context,
    )
    if contract_error is not None:
        return contract_error

    from .custom_targets import exact_fingerprint_arguments
    guard_args = exact_fingerprint_arguments(tool_args) if pinned_target(context, target_server) else tool_args
    from .dynamic_headers import fingerprint_arguments
    guard_args = fingerprint_arguments(target_server, guard_args, context, values_from=tool_args)
    async with _turn_guard_submission_lock(
        context,
        target_server,
        target_tool,
        guard_args,
    ):
        guarded = _get_turn_guard(
            context,
            target_server,
            target_tool,
            guard_args,
        )
        if guarded is not None:
            existing_identifier = str(
                guarded.get("symphony_identifier")
                or guarded.get("identifier")
                or "existing"
            )
            return ToolResult(
                title=f"{target_server}.{target_tool}",
                output=(
                    "Reused existing submission in same turn: "
                    f"{existing_identifier} (guarded)"
                ),
                metadata={"route_mode": "turn-guard-deduped", **guarded},
            )

        local_issue = await self._enqueue_symphony_local_issue(
            target_server=target_server,
            target_tool=target_tool,
            tool_args=tool_args,
            context=context,
        )
        if lazy_artifact_uploads:
            local_issue["lazy_artifact_uploads"] = lazy_artifact_uploads
        sym_identifier = str(local_issue.get("identifier", "") or "").strip()
        execution_channel = str(
            local_issue.get("execution_channel", "symphony") or "symphony"
        )
        _set_turn_guard(
            context,
            target_server,
            target_tool,
            {
                "symphony_identifier": sym_identifier,
                "status": local_issue.get("status", "queued"),
                "execution_channel": execution_channel,
            },
            guard_args,
        )

    await _register_async_completion_watcher(
        identifier=sym_identifier,
        submission_arguments=tool_args,
        target_server=target_server,
        target_tool=target_tool,
        context=context,
        protein_run=(
            dict(local_issue.get("protein_run") or {})
            if isinstance(local_issue.get("protein_run"), dict)
            else {}
        ),
        continuation_policy=continuation_policy,
    )

    if local_issue.get("status") == "deduped":
        duplicate_state = str(local_issue.get("duplicate_state", "")).strip().lower()
        duplicate_summary = str(local_issue.get("duplicate_result_summary", "")).strip()
        if duplicate_state in {"done", "closed"} and duplicate_summary:
            return ToolResult(
                title=f"{target_server}.{target_tool}",
                output=duplicate_summary,
                metadata={
                    "route_mode": f"{execution_channel}-deduped",
                    "submit_proxy": "queued",
                    **local_issue,
                },
            )
        return ToolResult(
            title=f"{target_server}.{target_tool}",
            output=(
                f"异步请求已由 {execution_channel} 队列受理；本地受理 ID: "
                f"{sym_identifier or 'existing'}（已去重）。"
                "该 LOCAL-* 编号不是目标 MCP 的 Job ID；后台执行器后续会通过 "
                "job-protocol 提交并记录真实 job_id。"
                "本轮不要调用 get_job_status/collect_job_result；请把本地受理 ID "
                "返回给用户，然后结束本轮或继续其他工作。"
            ),
            metadata={
                "route_mode": f"{execution_channel}-deduped",
                "accepted_async": True,
                "should_end_turn": _async_submit_should_end_turn(),
                "submit_proxy": "queued",
                **local_issue,
            },
        )

    return ToolResult(
        title=f"{target_server}.{target_tool}",
        output=(
            f"异步请求已由 {execution_channel} 队列受理；本地受理 ID: "
            f"{sym_identifier or 'created'}。"
            "该 LOCAL-* 编号不是目标 MCP 的 Job ID，也不表示目标 MCP 已经收到请求；"
            "后台执行器后续会通过 job-protocol 提交并记录真实 job_id。"
            f"队列刷新状态: {'已触发' if local_issue.get('refresh_triggered') else '等待刷新'}。"
            "本轮不要调用 get_job_status/collect_job_result；请把本地受理 ID "
            "返回给用户，然后结束本轮或继续其他工作。"
        ),
        metadata={
            "route_mode": f"{execution_channel}-queue",
            "accepted_async": True,
            "should_end_turn": _async_submit_should_end_turn(),
            "submit_proxy": "queued",
            **local_issue,
        },
    )


def _proxy_local_issue_job_query(
    self, args: dict, context: ToolContext
) -> Optional[ToolResult]:
    identifier = _extract_local_identifier_from_job_protocol_args(args)
    if not identifier:
        return None

    issue, checked_paths = _find_local_issue_for_job_query(identifier, context)
    if issue is None:
        return ToolResult(
            title=f"job-protocol-mcp.{self._tool_name}",
            output=(
                f"Async Symphony issue not found: {identifier}. "
                "Refresh the SharedTracker-backed local issue list. "
                f"Checked issue stores: {', '.join(checked_paths) or 'none'}"
            ),
            error="LOCAL issue not found",
        )

    state = str(issue.get("state", "")).strip()
    mapped = _map_issue_state_to_job_status(state)
    result_summary = str(issue.get("result_summary", "") or "").strip()
    error_summary = str(issue.get("error_summary", "") or "").strip()
    workspace = issue.get("workspace") if isinstance(issue.get("workspace"), dict) else {}
    job_ref = workspace.get("job_ref") if isinstance(workspace.get("job_ref"), dict) else {}
    artifacts = workspace.get("artifacts") if isinstance(workspace.get("artifacts"), list) else []
    orchestration = workspace.get("orchestration") if isinstance(workspace.get("orchestration"), dict) else {}
    tool_contract = workspace.get("tool_contract") if isinstance(workspace.get("tool_contract"), dict) else {}
    output_path = str(
        workspace.get("output_abs_path")
        or workspace.get("output_path")
        or workspace.get("output_root")
        or ""
    ).strip()
    payload = {
        "status": mapped,
        "job_id": identifier,
        "process_id": identifier,
        "real_job_id": str(job_ref.get("job_id", "") or ""),
        "real_process_id": str(job_ref.get("process_id", "") or ""),
        "job_ref": job_ref,
        "tracker": "symphony-local-issue",
        "tracker_status": str(issue.get("tracker_status", "") or "").strip(),
        "state": state,
        "identifier": identifier,
        "title": str(issue.get("title", "") or ""),
        "updated_at": str(issue.get("updated_at", "") or ""),
        "result_summary": result_summary,
        "output_path": output_path,
        "artifacts": artifacts,
        "can_continue": mapped == "completed",
        "orchestration": orchestration,
        "tool_contract": tool_contract,
    }
    if error_summary:
        payload["error_summary"] = error_summary

    if self._tool_name == "collect_job_result":
        if mapped != "completed":
            return ToolResult(
                title=f"job-protocol-mcp.{self._tool_name}",
                output=json.dumps(payload, ensure_ascii=False),
                error=None,
                metadata={"accepted_async": True, **payload},
            )
        if result_summary:
            return ToolResult(
                title=f"job-protocol-mcp.{self._tool_name}",
                output=result_summary,
                metadata={"accepted_async": True, **payload},
            )
        return ToolResult(
            title=f"job-protocol-mcp.{self._tool_name}",
            output=json.dumps(payload, ensure_ascii=False),
            metadata={"accepted_async": True, **payload},
        )

    return ToolResult(
        title=f"job-protocol-mcp.{self._tool_name}",
        output=json.dumps(payload, ensure_ascii=False),
        metadata={"accepted_async": True, **payload},
    )
