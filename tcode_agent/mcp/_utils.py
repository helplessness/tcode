"""Shared MCP utilities — path rewriting, routing helpers, issue store, fingerprinting.

Extracted from agent_integration.py to reduce monolith size.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

from ..runtime.shared_tracker import SharedTracker
if TYPE_CHECKING:
    from ..tools.base import ToolContext
else:
    ToolContext = Any
from .sandbox_boundary import (
    mcp_sandbox_boundary_error as _mcp_sandbox_boundary_error,
    mcp_sandbox_boundary_metadata as _mcp_sandbox_boundary_metadata,
)


_TURN_SUBMIT_GUARD: dict[str, dict[str, Any]] = {}
_TURN_SUBMIT_GUARD_LOCK = threading.RLock()
_TURN_SUBMIT_LOCKS: dict[str, dict[str, Any]] = {}
_PAPER_WORKSPACE_SERVERS = {
    "paper-download-mcp",
}
_TMCP_WORKSPACE_SERVERS = _PAPER_WORKSPACE_SERVERS | {
    "boltz-mcp",
    "gnina-mcp",
    "ligandmpnn-mcp",
    "simulation-mcp",
    "placer-mcp",
    "plip-mcp",
    "rfdiffusion-mcp",
}


def _tracker_db_path_text(workdir: Optional[str]) -> str:
    return SharedTracker.db_path(workdir).as_posix()


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _extract_parent_session_id(context: Optional[ToolContext]) -> str:
    if context is None:
        return ""
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        value = str(extra.get("parent_session_id", "") or "").strip()
        if value:
            return value
        trace = extra.get("tool_trace_context")
        if isinstance(trace, dict):
            value = str(trace.get("parent_session_id", "") or "").strip()
            if value:
                return value
    return ""


def _extract_tool_trace_context(context: Optional[ToolContext]) -> dict[str, str]:
    if context is None:
        return {}
    extra = getattr(context, "extra", None)
    if not isinstance(extra, dict):
        return {}
    trace = extra.get("tool_trace_context")
    if not isinstance(trace, dict):
        return {}
    allowed_keys = (
        "workflow_run_id",
        "workflow_stage_id",
        "workflow_template_key",
        "workflow_owner_session_id",
        "workflow_actor_type",
        "parent_session_id",
        "subagent_session_id",
        "worker_name",
    )
    return {
        key: str(trace.get(key, "") or "").strip()
        for key in allowed_keys
        if str(trace.get(key, "") or "").strip()
    }


def _context_workdir(context: Optional[ToolContext]) -> str:
    """Return the directory where the current agent executes tools and resolves files."""
    if context is None:
        return str(os.environ.get("TCODE_WORKDIR") or "").strip()
    value = str(getattr(context, "workdir", "") or "").strip()
    if value:
        return value
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        value = str(extra.get("workdir", "") or "").strip()
        if value:
            return value
    return str(os.environ.get("TCODE_WORKDIR") or "").strip()


def _context_control_workdir(context: Optional[ToolContext]) -> str:
    """Return the stable workspace used by control-plane state.

    Task/team subagents can execute in isolated worktrees.  Their async queue,
    review, runtime, and watcher records must still live in the owning workspace
    so the parent agent and task board can observe them.
    """
    extra = getattr(context, "extra", None) if context is not None else None
    if isinstance(extra, dict):
        workspace = extra.get("agent_workspace")
        if isinstance(workspace, dict):
            value = str(workspace.get("control_workdir") or "").strip()
            if value:
                return value
        value = str(extra.get("control_workdir") or "").strip()
        if value:
            return value
    return _context_workdir(context)


def _workspace_owner_from_workdir(workdir: str) -> dict[str, str]:
    text = str(workdir or "").strip()
    if not text:
        return {}
    try:
        parts = Path(text).expanduser().resolve(strict=False).parts
    except Exception:
        parts = Path(text).parts

    for index in range(len(parts) - 4, -1, -1):
        if (
            parts[index] == "workdirs"
            and parts[index + 1]
            and parts[index + 2]
            and parts[index + 3]
        ):
            return {
                "tenant_id": parts[index + 1],
                "user_id": parts[index + 2],
                "workspace_id": parts[index + 3],
            }

    return {}


def _context_workspace_claims(context: Optional[ToolContext]) -> dict[str, str]:
    extra = getattr(context, "extra", None)
    if not isinstance(extra, dict):
        extra = {}

    claims = {
        "tenant_id": str(extra.get("tenant_id", "") or "").strip(),
        "user_id": str(extra.get("user_id", "") or "").strip(),
        "workspace_id": str(extra.get("workspace_id", "") or "").strip(),
    }
    inferred = _workspace_owner_from_workdir(_context_control_workdir(context))

    inferred_workspace = str(inferred.get("workspace_id", "") or "").strip()
    if inferred_workspace and claims.get("workspace_id") != inferred_workspace:
        claims["workspace_id"] = inferred_workspace
        inferred_user = str(inferred.get("user_id", "") or "").strip()
        if inferred_user:
            claims["user_id"] = inferred_user
    elif not claims.get("workspace_id") and inferred_workspace:
        claims["workspace_id"] = inferred_workspace

    if not claims.get("user_id"):
        inferred_user = str(inferred.get("user_id", "") or "").strip()
        if inferred_user:
            claims["user_id"] = inferred_user

    if not claims.get("tenant_id"):
        inferred_tenant = str(inferred.get("tenant_id", "") or "").strip()
        if inferred_tenant:
            claims["tenant_id"] = inferred_tenant

    return claims


def _inject_workspace_root_hint(payload: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    text = str(workspace_root or "").strip()
    if not text:
        return dict(payload or {})

    hinted = dict(payload or {})
    if isinstance(hinted.get("input_config"), dict):
        inner = dict(hinted["input_config"])
        inner["workspace_root"] = text
        hinted["input_config"] = inner
        return hinted

    hinted["workspace_root"] = text
    return hinted


def _inject_context_workspace_claims(
    payload: dict[str, Any],
    context: Optional[ToolContext],
) -> dict[str, Any]:
    hinted = dict(payload or {})
    claims = _context_workspace_claims(context)
    workdir = _context_workdir(context)

    if isinstance(hinted.get("input_config"), dict):
        inner = dict(hinted["input_config"])
        for key, value in claims.items():
            if value:
                inner[key] = value
        if workdir:
            inner["workspace_root"] = workdir
        hinted["input_config"] = inner
        return hinted

    for key, value in claims.items():
        if value:
            hinted[key] = value
    if workdir:
        hinted["workspace_root"] = workdir
    return hinted


def _inject_context_workspace_root_hint(
    server_name: str,
    payload: dict[str, Any],
    context: Optional[ToolContext],
    tool_name: str = "",
) -> dict[str, Any]:
    server = str(server_name or "").strip().lower()
    if not _workspace_managed_server(server, tool_name):
        return dict(payload or {})
    if server in _PAPER_WORKSPACE_SERVERS:
        hinted = dict(payload or {})
        if str(tool_name or "").strip() not in {"download_paper", "batch_download_papers"}:
            return hinted
        workdir = _context_workdir(context)
        if workdir:
            hinted["workspace_root"] = workdir
        return hinted
    return _inject_context_workspace_claims(payload, context)


def _workspace_managed_server(server: str, tool_name: str = '', schema=None) -> bool:
    if server in _TMCP_WORKSPACE_SERVERS:
        return True
    from .parameter_contract import workspace_aware
    if schema is None:
        from .manager_auto import MCPManager
        schema = (MCPManager.get_server_tool(server, tool_name) or {}).get('inputSchema')
    return workspace_aware(schema)


def _check_context_workspace_conflicts(server, arguments, context, *, tool_name='', schema=None, sources=None):
    """Check NEW model calls only; persisted worker/recovery payloads keep their binding."""
    declared = {name for name, source in (sources or {}).items() if source in {'runtime', 'deployment'}}
    if not declared and not _workspace_managed_server(str(server).lower(), tool_name, schema):
        return
    expected = {**_context_workspace_claims(context), 'workspace_root': _context_workdir(context)}
    layers = [arguments]
    layers.extend(arguments[name] for name in ('input_config', 'input', 'payload') if isinstance(arguments.get(name), dict))
    for layer in layers:
        for raw_name, value in layer.items():
            name = raw_name.removeprefix('__')
            trusted = expected.get(name)
            if value in (None, ''):
                continue
            if raw_name in {'job_protocol_token', '__job_protocol_token'} or (name in declared and not trusted):
                raise ValueError(f"Parameter '{raw_name}' is system-managed; omit it and let the host supply it.")
            if not trusted:
                continue
            equal = str(value).strip() == trusted
            if name == 'workspace_root':
                try:
                    equal = Path(str(value)).expanduser().resolve() == Path(trusted).expanduser().resolve()
                except (ValueError, OSError):
                    equal = False
            if not equal:
                raise ValueError(f"Parameter '{raw_name}' is managed by the current session and conflicts with trusted context; omit it.")


def _inject_async_target_identity(
    payload: dict[str, Any],
    context: Optional[ToolContext],
    *,
    issue_id: str,
    issue_identifier: str,
) -> dict[str, Any]:
    """Inject the TMCP user/project/task/run hierarchy from trusted context.

    Target MCPs build scientific outputs below
    ``runs/tmcp/<user>/<project>/<task>/software/...``. These fields are
    control-plane identity, not model-selected scientific parameters.
    """

    hinted = _inject_context_workspace_claims(payload, context)
    extra = getattr(context, "extra", None) if context is not None else None
    extra = extra if isinstance(extra, dict) else {}
    claims = _context_workspace_claims(context)
    trace = _extract_tool_trace_context(context)
    project_id = str(
        extra.get("project_id")
        or trace.get("workflow_run_id")
        or claims.get("workspace_id")
        or getattr(context, "session_id", "")
        or "workspace"
    ).strip()

    def _safe_identity(value: Any, fallback: str) -> str:
        text = re.sub(r"[^A-Za-z0-9._@-]+", "-", str(value or "").strip())
        return (text.strip(".-_@") or fallback)[:128]

    project_id = _safe_identity(project_id, "workspace")
    task_id = _safe_identity(issue_identifier or issue_id, "async-task")
    run_id = _safe_identity(issue_id or issue_identifier, "async-run")

    target = hinted
    wrapped = isinstance(hinted.get("input_config"), dict)
    if wrapped:
        target = dict(hinted["input_config"])
    target["project_id"] = project_id
    target["task_id"] = task_id
    target["run_id"] = run_id
    if wrapped:
        hinted["input_config"] = target
    return hinted


def _extract_preview_ports(tool_args: Any) -> list[int]:
    if not isinstance(tool_args, dict):
        return []
    values = tool_args.get("preview_ports")
    if not isinstance(values, list):
        return []
    ports: list[int] = []
    seen: set[int] = set()
    for item in values:
        try:
            port = int(item)
        except Exception:
            continue
        if port <= 0 or port > 65535 or port in seen:
            continue
        ports.append(port)
        seen.add(port)
    return ports


def _review_canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _review_canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_review_canonicalize(v) for v in value]
    if isinstance(value, str):
        return _canonicalize_path_like_value(value)
    return value


def _review_args_equal(left: Any, right: Any) -> bool:
    return _review_canonicalize(left) == _review_canonicalize(right)


def _review_arg_match_candidates(args: Any, tool_name: str) -> list[Any]:
    if not isinstance(args, dict):
        return [args]

    candidates: list[Any] = []

    def _add(value: Any) -> None:
        marker = _review_canonicalize(value)
        if any(_review_canonicalize(existing) == marker for existing in candidates):
            return
        candidates.append(value)

    raw = dict(args)
    _add(raw)
    try:
        _add(_normalize_routed_tool_args(raw, tool_name))
    except Exception:
        pass

    if isinstance(raw.get("input_config"), dict):
        _add(dict(raw["input_config"]))

    return candidates


def _review_tool_args_match(left: Any, right: Any, tool_name: str) -> bool:
    left_candidates = _review_arg_match_candidates(left, tool_name)
    right_candidates = _review_arg_match_candidates(right, tool_name)
    return any(
        _review_args_equal(left_candidate, right_candidate)
        for left_candidate in left_candidates
        for right_candidate in right_candidates
    )


def _extract_review_workdir_hints(
    tool_args: Any,
    context: Optional[ToolContext],
) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        try:
            resolved = Path(text).expanduser().resolve(strict=False).as_posix()
        except Exception:
            resolved = text
        if resolved in seen:
            return
        seen.add(resolved)
        hints.append(text)

    _add(_context_control_workdir(context))
    _add(_context_workdir(context))
    _add(os.environ.get("TCODE_WORKDIR") or os.getcwd())
    configured_workspace_root = str(
        os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or ""
    ).strip()
    if configured_workspace_root:
        configured_path = Path(configured_workspace_root).expanduser()
        if not configured_path.is_absolute():
            configured_path = (
                Path(os.environ.get("TCODE_WORKDIR") or os.getcwd())
                / configured_path
            )
        _add(configured_path)

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key) in {
                    "workdir",
                    "workspace_root",
                    "output_root",
                    "root",
                }:
                    _add(nested)
                else:
                    _walk(nested)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(tool_args)

    roots = list(hints)
    try:
        configured_resolved = (
            configured_path.resolve(strict=False)
            if configured_workspace_root
            else None
        )
    except Exception:
        configured_resolved = None
    for root_text in roots:
        root = Path(root_text).expanduser().resolve(strict=False)
        patterns = (
            ("*/*/*",)
            if configured_resolved is not None and root == configured_resolved
            else ("workdirs/*/*/*",)
        )
        for pattern in patterns:
            try:
                for path in root.glob(pattern):
                    if path.is_dir():
                        _add(path)
            except Exception:
                continue

    return hints


def _find_approved_review_for_call_local(
    *,
    tool_server: str,
    tool_name: str,
    tool_args: dict,
    context: Optional[ToolContext],
) -> Optional[dict[str, Any]]:
    server = str(tool_server or "").strip().lower()
    name = str(tool_name or "").strip()
    if not server or not name:
        return None

    for workdir in _extract_review_workdir_hints(tool_args, context):
        tracker_path = SharedTracker.db_path(workdir)
        legacy_path = Path(workdir) / "tmp" / "human_review_queue.json"
        if not tracker_path.is_file() and not legacy_path.is_file():
            continue
        try:
            store = SharedTracker.load_review_store(workdir)
        except Exception:
            continue
        reviews = store.get("reviews", []) if isinstance(store, dict) else []
        if not isinstance(reviews, list):
            continue
        for review in reversed(reviews):
            if not isinstance(review, dict):
                continue
            if str(review.get("status", "")).strip().lower() != "approved":
                continue
            if (
                str(review.get("execution_disposition", "")).strip().lower()
                == "revise_before_execute"
            ):
                continue
            if str(review.get("tool_server", "")).strip().lower() != server:
                continue
            if str(review.get("tool_name", "")).strip() != name:
                continue
            approved_args = review.get("tool_args", {})
            if _review_tool_args_match(approved_args, tool_args, name):
                return review

    return None


def _build_submit_workspace_hint(
    *,
    context: Optional[ToolContext],
    issue_identifier: str,
    run_id: str,
    target_server: str,
    target_tool: str,
    job_dir: Path,
    expected_artifacts: list[str],
    preview_ports: Optional[list[int]] = None,
    input_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "job_id": "",
        "process_id": "",
        "run_id": run_id,
        "tool": target_server,
        "task_type": target_tool,
        "workdir": str(job_dir),
        "log_file": "",
        "exitcode_file": "",
        "expected_artifacts": expected_artifacts,
        "context_file": str(job_dir / "job_context.json"),
        "session_id": str(getattr(context, "session_id", "") or "").strip(),
        "parent_session_id": _extract_parent_session_id(context),
        "issue_identifier": issue_identifier,
        "preview_ports": preview_ports or [],
        "preview_urls": [],
        "input_snapshot": input_snapshot if isinstance(input_snapshot, dict) else {},
        "artifact_source": {},
        "resume_supported": False,
        "resume_command": "",
        "retry_of": "",
        "resume_of": "",
        "review_lineage": {},
        "timeline_ref": {},
    }


def _build_review_workspace_hint(
    *,
    context: Optional[ToolContext],
    target_server: str,
    target_tool: str,
    preview_ports: Optional[list[int]] = None,
    input_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    workdir = _context_control_workdir(context)
    trace_context = _extract_tool_trace_context(context)
    hint = {
        "job_id": "",
        "process_id": "",
        "run_id": "",
        "tool": target_server,
        "task_type": target_tool,
        "workdir": workdir,
        "log_file": "",
        "exitcode_file": "",
        "expected_artifacts": [],
        "context_file": "",
        "session_id": str(getattr(context, "session_id", "") or "").strip() if context else "",
        "parent_session_id": _extract_parent_session_id(context),
        "issue_identifier": "",
        "preview_ports": preview_ports or [],
        "preview_urls": [],
        "input_snapshot": input_snapshot if isinstance(input_snapshot, dict) else {},
        "artifact_source": {},
        "resume_supported": False,
        "resume_command": "",
        "retry_of": "",
        "resume_of": "",
        "review_lineage": {},
        "timeline_ref": {},
    }
    if trace_context:
        hint["tool_trace_context"] = trace_context
        for key in (
            "workflow_run_id",
            "workflow_stage_id",
            "workflow_template_key",
            "workflow_owner_session_id",
        ):
            if trace_context.get(key):
                hint[key] = trace_context[key]
    return hint

def _async_submit_should_end_turn() -> bool:
    """Whether async submit acceptance should force-end the current turn.

    Default is False so one user request can enqueue multiple async jobs.
    """
    return _env_truthy("TCODE_ASYNC_ACCEPT_END_TURN", default=False)


def _mcp_path_rewrite_enabled() -> bool:
    return _env_truthy(
        "TCODE_MCP_REWRITE_WORKSPACE_PATHS",
        default=_env_truthy("TCODE_SANDBOX_ENABLED", default=False),
    )


def _mcp_reject_host_paths_enabled() -> bool:
    return _env_truthy(
        "TCODE_MCP_REJECT_HOST_PATH_RESULTS",
        default=_env_truthy("TCODE_SANDBOX_ENABLED", default=False),
    )


def _context_host_workdir(context: Optional[ToolContext]) -> str:
    return _context_workdir(context)


def _container_workspace_root() -> str:
    return str(os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace") or "/workspace").rstrip("/") or "/workspace"


def _looks_like_sensitive_host_path(text: str) -> bool:
    return bool(re.match(r"^/(Users|home|srv|tmp|var|etc|root|opt)(/|$)", text))


def _rewrite_mcp_path_value(value: str, *, host_workdir: Path, container_root: str) -> str:
    text = str(value or "")
    if not text or text.startswith("artifact://") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return text
    if text == container_root or text.startswith(f"{container_root}/"):
        return text

    try:
        path = Path(text)
        if path.is_absolute():
            resolved = path.expanduser().resolve(strict=False)
            root = host_workdir.expanduser().resolve(strict=False)
            try:
                rel = resolved.relative_to(root)
                return container_root if not rel.parts else f"{container_root}/{rel.as_posix()}"
            except Exception:
                if _mcp_reject_host_paths_enabled() and _looks_like_sensitive_host_path(text):
                    return "[blocked host path outside workspace]"
    except Exception:
        return text
    return text


def _rewrite_mcp_paths_in_payload(payload: Any, *, host_workdir: Path, container_root: str) -> Any:
    if isinstance(payload, dict):
        return {
            key: _rewrite_mcp_paths_in_payload(value, host_workdir=host_workdir, container_root=container_root)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [
            _rewrite_mcp_paths_in_payload(item, host_workdir=host_workdir, container_root=container_root)
            for item in payload
        ]
    if isinstance(payload, str):
        return _rewrite_mcp_path_value(payload, host_workdir=host_workdir, container_root=container_root)
    return payload


def _rewrite_mcp_result_paths(text: str, context: Optional[ToolContext]) -> str:
    if not _mcp_path_rewrite_enabled():
        return text
    host_workdir_text = _context_host_workdir(context)
    if not host_workdir_text:
        return text
    host_workdir = Path(host_workdir_text)
    container_root = _container_workspace_root()

    try:
        payload = json.loads(text)
    except Exception:
        root = host_workdir.expanduser().resolve(strict=False).as_posix()
        if root and root in text:
            return text.replace(root, container_root)
        return text

    try:
        rewritten = _rewrite_mcp_paths_in_payload(
            payload,
            host_workdir=host_workdir,
            container_root=container_root,
        )
        return json.dumps(rewritten, ensure_ascii=False)
    except Exception:
        return text


def _routed_servers() -> set[str]:
    raw = os.environ.get("SYMPHONY_ROUTED_MCP_SERVERS", "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _direct_servers() -> set[str]:
    raw = os.environ.get("SYMPHONY_DIRECT_MCP_SERVERS", "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _never_route_servers() -> set[str]:
    # Hard safety guard to avoid recursive queueing loops.
    raw = os.environ.get("SYMPHONY_NEVER_ROUTE_MCP_SERVERS", "job-protocol-mcp").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# Keep the historical imports stable while the implementation lives in a
# dependency-light module.  In particular, builtin TaskTool can import route
# permissions without recursively importing this module through tools.base.
from .route_permissions import (  # noqa: E402
    agents_allowed_to_route_server as _agents_allowed_to_route_server,
    get_agent_allowed_routed_servers as _get_agent_allowed_routed_servers,
    normalize_allowed_routed_servers as _normalize_allowed_routed_servers,
)


async def _check_human_review_required(
    tool_server: str,
    tool_name: str,
    tool_args: dict,
    user_message: str = "",
    tool_output: str = "",
    context: Optional[ToolContext] = None,
) -> dict:
    """Check if a tool call requires human review via API"""
    if callable((getattr(context, 'extra', None) or {}).get('__goal_checkpoint__')):
        from ..session.manager import SessionManager
        from ..server.goal_mode import public_goal
        import time
        goal = public_goal(await SessionManager.get(context.session_id))
        if (goal and goal.get('auto_approve_reviews') and goal['status'] == 'running'
                and str((context.extra or {}).get('request_id') or '') in goal.get('run_ids', [])
                and time.time() < goal['deadline'] and goal['rounds_used'] <= goal['max_rounds']):
            # Compute submissions are separately forced through one-shot grants
            # in the proxy; this only skips ordinary tool review waits.
            return {'requires_review': False, 'reason': 'goal_auto_review_policy'}
    def _local_review_fallback() -> dict:
        def _check_input_params_local(args: dict, conditions: dict) -> tuple[bool, str]:
            for param, condition in conditions.items():
                if param not in args:
                    continue
                op = condition.get("operator", "eq")
                threshold = condition.get("value")
                value = args[param]

                if op == "gt" and isinstance(value, (int, float)) and value > threshold:
                    return True, f"parameter_{param}_exceeds_threshold"
                if op == "lt" and isinstance(value, (int, float)) and value < threshold:
                    return True, f"parameter_{param}_below_threshold"
                if op == "gte" and isinstance(value, (int, float)) and value >= threshold:
                    return True, f"parameter_{param}_gte_threshold"
                if op == "lte" and isinstance(value, (int, float)) and value <= threshold:
                    return True, f"parameter_{param}_lte_threshold"
                if op == "eq" and str(value) == str(threshold):
                    return True, f"parameter_{param}_equals_{threshold}"
                if op == "neq" and str(value) != str(threshold):
                    return True, f"parameter_{param}_not_equals_{threshold}"
            return False, ""

        try:
            enabled = str(os.environ.get("HUMAN_REVIEW_ENABLED", "true")).strip().lower()
            if enabled in {"0", "false", "no", "off"}:
                return {"requires_review": False, "reason": "human_review_disabled"}

            approved = _find_approved_review_for_call_local(
                tool_server=tool_server,
                tool_name=tool_name,
                tool_args=tool_args,
                context=context,
            )
            if approved is not None:
                approved_override = approved.get("approved_tool_args_override")
                if not isinstance(approved_override, dict):
                    approved_override = None
                return {
                    "requires_review": False,
                    "reason": "approved_review_reused",
                    "trigger": "approved_review_match_local",
                    "approved_review_identifier": str(
                        approved.get("identifier", "") or ""
                    ),
                    "approved_comments": str(approved.get("comments", "") or ""),
                    "approved_tool_args_override": approved_override,
                }

            config_path = os.environ.get("HUMAN_REVIEW_CONFIG_PATH", "").strip()
            if not config_path:
                base = (
                    os.environ.get("TCODE_CONFIG_ROOT")
                    or os.environ.get("TCODE_WORKDIR")
                    or os.getcwd()
                )
                config_path = str(Path(base) / "human_review.json")
            cfg = Path(config_path)
            if not cfg.is_file():
                return {"requires_review": False, "reason": "check_unavailable"}

            config = json.loads(cfg.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or not config.get("enabled", False):
                return {"requires_review": False, "reason": "human_review_disabled"}

            required_tools = config.get("required_review_tools", {})
            server = str(tool_server or "").strip().lower()
            name = str(tool_name or "").strip()
            if isinstance(required_tools, dict):
                server_cfg = required_tools.get(server)
                if isinstance(server_cfg, dict):
                    tools = server_cfg.get("tools", [])
                    if isinstance(tools, list) and name in tools:
                        auto_conditions = server_cfg.get("auto_review_conditions", {})
                        if isinstance(auto_conditions, dict) and auto_conditions:
                            match, trigger = _check_input_params_local(
                                tool_args, auto_conditions
                            )
                            if match:
                                return {
                                    "requires_review": True,
                                    "reason": str(server_cfg.get("reason", "需要人工确认") or "需要人工确认"),
                                    "trigger": trigger,
                                    "check_type": "input_param",
                                }
                        else:
                            return {
                                "requires_review": True,
                                "reason": str(server_cfg.get("reason", "需要人工确认") or "需要人工确认"),
                                "trigger": "local_config_fallback",
                                "check_type": "input_param",
                            }
        except Exception:
            pass
        return {"requires_review": False, "reason": "check_unavailable"}

    try:
        api_base = os.environ.get("TCODE_HUMAN_REVIEW_API_BASE")
        url = f"{api_base}/api/human-review/check-required"

        payload = {
            "tool_server": tool_server.lower(),
            "tool_name": tool_name,
            "tool_args": tool_args,
            "user_message": user_message,
            "tool_output": tool_output,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        return _local_review_fallback()

    return _local_review_fallback()


async def _build_review_session_data(context: Optional[ToolContext]) -> dict[str, str]:
    session_id = str(getattr(context, "session_id", "") or "").strip()
    workdir = _context_control_workdir(context)
    parent_session_id = ""
    trace_context = _extract_tool_trace_context(context)

    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        parent_session_id = str(extra.get("parent_session_id", "") or "").strip()
        if not workdir:
            workdir = str(extra.get("workdir", "") or "").strip()
    if not parent_session_id:
        parent_session_id = str(trace_context.get("parent_session_id", "") or "").strip()

    if not parent_session_id and session_id:
        try:
            from ..session.manager import SessionManager

            session = await SessionManager.get(session_id)
            parent_session_id = str(getattr(session, "parent_id", "") or "").strip()
        except Exception:
            parent_session_id = ""

    review_data = {
        "session_id": session_id,
        "workdir": workdir,
        "origin_request_id": str((extra.get("request_id") or "") if isinstance(extra, dict) else "").strip(),
    }
    if parent_session_id:
        review_data["parent_session_id"] = parent_session_id
    if trace_context:
        review_data["tool_trace_context"] = trace_context
        for key in (
            "workflow_run_id",
            "workflow_stage_id",
            "workflow_template_key",
            "workflow_owner_session_id",
        ):
            if trace_context.get(key):
                review_data[key] = trace_context[key]
    return review_data


def _enforce_direct_whitelist() -> bool:
    # When enabled, MCP servers outside direct whitelist are not allowed to run directly.
    return _env_truthy("SYMPHONY_ENFORCE_DIRECT_WHITELIST", default=False)


def _maybe_parse_json_object(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:
                return value
    return value


def _normalize_routed_tool_args(args: dict, tool_name: str) -> dict:
    normalized = dict(args or {})
    call_values = normalized.pop('_tcode_context', None)
    keep_input_config_wrapper = str(tool_name or "").endswith("_mcp_server")

    for key in ("input", "input_config", "payload"):
        if key in normalized:
            normalized[key] = _maybe_parse_json_object(normalized[key])

    # For non-job-protocol tools, a single input_config wrapper usually means
    # the target tool expects direct named arguments.
    if (
        tool_name not in {"submit_job", "get_job_status", "collect_job_result", "describe_target_tool"}
        and set(normalized.keys()) == {"input_config"}
        and isinstance(normalized.get("input_config"), dict)
    ):
        if keep_input_config_wrapper:
            normalized = {"input_config": dict(normalized["input_config"])}
        else:
            normalized = dict(normalized["input_config"])

    # For *_mcp_server tools, enforce a stable input shape:
    # even if caller provides flat args, wrap them into input_config.
    if keep_input_config_wrapper and "input_config" not in normalized:
        normalized = {"input_config": dict(normalized)}

    # Remove submit-job routing metadata when invoking non-job-protocol target tools.
    # These keys are control-plane fields and should never be forwarded to the target MCP tool.
    # describe_target_tool also consumes routing fields; they are query arguments,
    # not submit metadata to strip from a domain tool's payload.
    if tool_name not in {"submit_job", "get_job_status", "collect_job_result", "describe_target_tool"}:
        control_keys = {
            "target_server",
            "target_tool",
            "server_name",
            "tool_name",
            "server",
            "tool",
        }
        if keep_input_config_wrapper and isinstance(
            normalized.get("input_config"), dict
        ):
            inner = dict(normalized["input_config"])
            for key in control_keys:
                inner.pop(key, None)
            normalized["input_config"] = inner
        else:
            for key in control_keys:
                normalized.pop(key, None)

    if call_values is not None:
        normalized['_tcode_context'] = call_values
    return normalized


async def _list_server_tool_names(server_name: str) -> set[str]:
    """Return connected MCP tool names for a given server."""
    from .manager_auto import MCPManager

    if not MCPManager.is_initialized():
        await MCPManager.initialize()

    tools = await MCPManager.get_tools()
    names: set[str] = set()
    wanted = (server_name or "").strip().lower()

    for tool_info in tools.values():
        if not isinstance(tool_info, dict):
            continue
        srv = str(tool_info.get("server") or "").strip().lower()
        name = str(tool_info.get("tool") or "").strip()
        if srv == wanted and name:
            names.add(name)

    return names


def _symphony_memory_issues_path(workdir: Optional[str]) -> Path:
    configured = os.environ.get("SYMPHONY_MEMORY_ISSUES_FILE", "").strip()
    if configured:
        return Path(configured)
    base = workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()
    return Path(base) / "tmp" / "symphony_memory_issues.json"


def _is_symphony_issue_store_path(path: Path) -> bool:
    if path.name == "symphony_memory_issues.json" and path.parent.name == "tmp":
        return True
    try:
        expected = _symphony_memory_issues_path(None).resolve(strict=False)
        actual = path.resolve(strict=False)
        return actual == expected
    except Exception:
        return path.name == "symphony_memory_issues.json"


def _async_execution_mode() -> str:
    """返回进程级异步执行通道，并对旧值安全地收敛到 Symphony。

    输入来自 ``TCODE_ASYNC_EXECUTION_MODE``。最新配置只允许显式的
    ``symphony`` 或运维专用的 ``bridge``；已经删除的 ``hybrid``、空值和
    其他未知值都不会再触发按服务名单分流，而是统一返回 ``symphony``。
    该函数只决定执行通道，不修改请求、任务或持久化状态。
    """

    mode = str(
        os.environ.get("TCODE_ASYNC_EXECUTION_MODE", "symphony") or ""
    ).strip().lower()
    return mode if mode in {"bridge", "symphony"} else "symphony"


def _choose_async_execution_channel(
    server_name: str, context: Optional[ToolContext] = None
) -> str:
    """为一次异步 MCP 提交选择唯一执行通道。

    ``context.extra.async_execution_channel`` 是内部调度器对单次请求的显式
    运维覆盖，只接受 ``bridge`` 或 ``symphony``。没有覆盖时使用进程级单轨
    配置。``server_name`` 仅为保持调用接口稳定而保留，不再参与旧 hybrid
    服务名单判断，因此同一部署不会因 MCP 服务名不同而隐式走两套系统。
    """

    del server_name
    if context and getattr(context, "extra", None):
        extra = context.extra
        if isinstance(extra, dict):
            forced = str(extra.get("async_execution_channel", "") or "").strip().lower()
            if forced in {"bridge", "symphony"}:
                return forced
    return _async_execution_mode()


def _extract_local_identifier_from_job_protocol_args(args: dict) -> str:
    raw = args.get("input") or args.get("input_config") or args
    if isinstance(raw, str):
        raw = _maybe_parse_json_object(raw)
    if not isinstance(raw, dict):
        return ""

    candidates = [
        raw.get("job_id"),
        raw.get("process_id"),
        raw.get("identifier"),
        raw.get("local_request_id"),
        raw.get("local_identifier"),
        raw.get("request_id"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text.upper().startswith("LOCAL-"):
            return text
    return ""


def _find_issue_by_identifier(
    issues: list[dict[str, Any]], identifier: str
) -> Optional[dict[str, Any]]:
    want = str(identifier or "").strip()
    if not want:
        return None
    for issue in reversed(issues):
        if not isinstance(issue, dict):
            continue
        got = str(issue.get("identifier", "")).strip()
        if got == want:
            return issue
    return None


def _map_issue_state_to_job_status(state: str) -> str:
    s = str(state or "").strip().lower()
    if s in {"todo", "queued"}:
        return "queued"
    if s in {"in progress", "running"}:
        return "running"
    if s in {"done", "closed"}:
        return "completed"
    if s in {"failed", "error"}:
        return "failed"
    if s in {"cancelled", "canceled"}:
        return "cancelled"
    return "queued"


def _load_issue_store(path: Path) -> dict[str, Any]:
    if _is_symphony_issue_store_path(path):
        try:
            workdir = str(path.parent.parent)
            return SharedTracker.load_issue_store(workdir)
        except Exception:
            return {"issues": []}
    if not path.exists():
        return {"issues": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"issues": []}
        issues = data.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        return {"issues": issues}
    except Exception:
        return {"issues": []}


def _save_issue_store(path: Path, store: dict[str, Any]) -> None:
    if _is_symphony_issue_store_path(path):
        workdir = str(path.parent.parent)
        SharedTracker.sync_issue_store(store, workdir)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _stable_json(data: Any) -> str:
    try:
        return json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except Exception:
        return str(data)


def _canonicalize_path_like_value(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    # Skip URLs or obvious non-path tokens.
    if "://" in text:
        return text

    p = Path(text)
    if p.is_absolute():
        try:
            return str(p.resolve(strict=False))
        except Exception:
            return str(p)

    base = os.environ.get("TCODE_WORKDIR") or os.getcwd()
    try:
        return str((Path(base) / p).resolve(strict=False))
    except Exception:
        return str(p)


def _canonicalize_fingerprint_args(data: Any) -> Any:
    if isinstance(data, dict):
        return {str(k): _canonicalize_fingerprint_args(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_canonicalize_fingerprint_args(v) for v in data]
    if isinstance(data, str):
        return _canonicalize_path_like_value(data)
    return data


def _compact_fingerprint_args(server: str, tool: str, tool_args: dict) -> dict:
    compact = _canonicalize_fingerprint_args(tool_args or {})
    server_l = str(server or "").strip().lower()
    tool_s = str(tool or "").strip()

    # gnina often receives semantically identical requests with only path style
    # differences or redundant autobox_ligand. Collapse those to one fingerprint.
    if server_l == "gnina-mcp" and tool_s == "gnina_mcp_server":
        payload = compact
        if isinstance(payload, dict) and isinstance(payload.get("input_config"), dict):
            payload = dict(payload["input_config"])
        if isinstance(payload, dict):
            ligand = str(payload.get("ligand") or "").strip()
            autobox = str(payload.get("autobox_ligand") or "").strip()
            if ligand and autobox and ligand == autobox:
                payload.pop("autobox_ligand", None)
            if isinstance(compact, dict) and "input_config" in compact:
                return {"input_config": payload}
            return payload

    return compact


def _issue_state_normalized(issue: dict[str, Any]) -> str:
    return str(issue.get("state", "")).strip().lower()


def _is_active_issue_state(state: str) -> bool:
    return state in {"todo", "queued", "in progress"}


def _parse_iso_utc(ts: str) -> Optional[datetime]:
    text = str(ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _is_recent_issue(issue: dict[str, Any], seconds: int) -> bool:
    dt = _parse_iso_utc(issue.get("updated_at") or issue.get("created_at") or "")
    if not dt:
        return False
    try:
        now = datetime.now(dt.tzinfo)
    except Exception:
        return False
    return (now - dt).total_seconds() <= max(1, int(seconds))


def _build_issue_fingerprint(
    server: str, tool: str, session_id: str, tool_args: dict
) -> str:
    compact_args = _compact_fingerprint_args(server, tool, tool_args)
    raw = "|".join(
        [
            str(server or "").strip().lower(),
            str(tool or "").strip(),
            str(session_id or "").strip(),
            _stable_json(compact_args),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _turn_guard_key(
    context: Optional[ToolContext],
    server: str,
    tool: str,
    tool_args: Optional[dict[str, Any]] = None,
) -> str:
    session_id = str(getattr(context, "session_id", "") or "")
    message_id = str(getattr(context, "message_id", "") or "")
    if not session_id or not message_id:
        return ""
    owner_claims = _context_workspace_claims(context)
    normalized_args = _compact_fingerprint_args(
        str(server or ""),
        str(tool or ""),
        dict(tool_args or {}),
    )
    encoded_args = json.dumps(
        normalized_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    args_digest = hashlib.sha256(encoded_args.encode("utf-8")).hexdigest()[:20]
    return "|".join(
        [
            str(owner_claims.get("tenant_id") or ""),
            str(owner_claims.get("user_id") or ""),
            str(owner_claims.get("workspace_id") or ""),
            session_id,
            message_id,
            str(server or "").strip().lower(),
            str(tool or "").strip(),
            args_digest,
        ]
    )


def _get_turn_guard(
    context: Optional[ToolContext],
    server: str,
    tool: str,
    tool_args: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    key = _turn_guard_key(context, server, tool, tool_args)
    if not key.strip("|"):
        return None
    now = time.time()
    with _TURN_SUBMIT_GUARD_LOCK:
        _purge_turn_guard_locked(now)
        info = _TURN_SUBMIT_GUARD.get(key)
        return dict(info) if isinstance(info, dict) else None


def _set_turn_guard(
    context: Optional[ToolContext],
    server: str,
    tool: str,
    info: dict[str, Any],
    tool_args: Optional[dict[str, Any]] = None,
) -> None:
    key = _turn_guard_key(context, server, tool, tool_args)
    if not key.strip("|"):
        return
    payload = dict(info or {})
    now = time.time()
    payload["ts"] = now
    with _TURN_SUBMIT_GUARD_LOCK:
        _purge_turn_guard_locked(now)
        _TURN_SUBMIT_GUARD[key] = payload
        max_entries = _turn_guard_env_int(
            "SYMPHONY_PROXY_TURN_GUARD_MAX_ENTRIES",
            4096,
            minimum=64,
        )
        overflow = len(_TURN_SUBMIT_GUARD) - max_entries
        if overflow > 0:
            oldest = sorted(
                _TURN_SUBMIT_GUARD,
                key=lambda item: float(
                    _TURN_SUBMIT_GUARD[item].get("ts", 0.0) or 0.0
                ),
            )
            for stale_key in oldest[:overflow]:
                _TURN_SUBMIT_GUARD.pop(stale_key, None)


def _turn_guard_env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, value)


def _purge_turn_guard_locked(now: float) -> None:
    ttl_seconds = _turn_guard_env_int(
        "SYMPHONY_PROXY_TURN_GUARD_SECONDS",
        900,
        minimum=30,
    )
    stale_keys: list[str] = []
    for key, info in _TURN_SUBMIT_GUARD.items():
        try:
            timestamp = float(info.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            timestamp = 0.0
        if timestamp <= 0 or (now - timestamp) > ttl_seconds:
            stale_keys.append(key)
    for key in stale_keys:
        _TURN_SUBMIT_GUARD.pop(key, None)


@asynccontextmanager
async def _turn_guard_submission_lock(
    context: Optional[ToolContext],
    server: str,
    tool: str,
    tool_args: Optional[dict[str, Any]] = None,
):
    """Serialize same-turn submission checks for an identical routed call."""

    key = _turn_guard_key(context, server, tool, tool_args)
    if not key.strip("|"):
        yield
        return
    with _TURN_SUBMIT_GUARD_LOCK:
        entry = _TURN_SUBMIT_LOCKS.get(key)
        if entry is None:
            entry = {"lock": asyncio.Lock(), "users": 0}
            _TURN_SUBMIT_LOCKS[key] = entry
        entry["users"] = int(entry.get("users", 0) or 0) + 1
        lock = entry["lock"]
    try:
        async with lock:
            yield
    finally:
        with _TURN_SUBMIT_GUARD_LOCK:
            current = _TURN_SUBMIT_LOCKS.get(key)
            if current is entry:
                current["users"] = max(0, int(current.get("users", 1) or 1) - 1)
                if current["users"] == 0:
                    _TURN_SUBMIT_LOCKS.pop(key, None)


def _find_duplicate_issue(
    issues: list[dict[str, Any]],
    fingerprint: str,
    failed_cooldown_seconds: int,
) -> Optional[dict[str, Any]]:
    for issue in reversed(issues):
        if not isinstance(issue, dict):
            continue
        if str(issue.get("fingerprint", "")).strip() != fingerprint:
            continue
        state = _issue_state_normalized(issue)
        if _is_active_issue_state(state):
            return issue
        if state in {"failed", "error", "cancelled", "canceled"} and _is_recent_issue(
            issue, failed_cooldown_seconds
        ):
            return issue
    return None


def _issue_rank_for_keep(state: str) -> int:
    if _is_active_issue_state(state):
        return 3
    if state in {"done", "closed"}:
        return 2
    if state in {"failed", "error", "cancelled", "canceled"}:
        return 1
    return 0


def _issue_time_for_sort(issue: dict[str, Any]) -> float:
    dt = _parse_iso_utc(issue.get("updated_at") or issue.get("created_at") or "")
    if dt is None:
        return 0.0
    return dt.timestamp()


def _prune_redundant_issues_by_fingerprint(
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep only one most valuable issue per fingerprint to reduce queue noise.

    Keep priority:
    1) active (Todo/Queued/In Progress)
    2) Done/Closed
    3) Failed/Cancelled
    4) any other state
    Tie-breaker: newer updated_at/created_at wins.
    """
    if not issues:
        return issues, 0

    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        fp = str(issue.get("fingerprint", "")).strip()
        if not fp:
            passthrough.append(issue)
            continue
        grouped.setdefault(fp, []).append(issue)

    kept_ids: set[str] = set()
    for fp, group in grouped.items():
        if len(group) == 1:
            iid = str(group[0].get("id", "")).strip()
            if iid:
                kept_ids.add(iid)
            continue

        scored = sorted(
            group,
            key=lambda x: (
                _issue_rank_for_keep(_issue_state_normalized(x)),
                _issue_time_for_sort(x),
            ),
            reverse=True,
        )
        keep = scored[0]
        iid = str(keep.get("id", "")).strip()
        if iid:
            kept_ids.add(iid)

    new_issues: list[dict[str, Any]] = []
    removed = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        fp = str(issue.get("fingerprint", "")).strip()
        if not fp:
            new_issues.append(issue)
            continue
        iid = str(issue.get("id", "")).strip()
        if iid in kept_ids:
            new_issues.append(issue)
        else:
            removed += 1

    return new_issues, removed


async def _trigger_symphony_refresh() -> bool:
    base_url = os.environ.get("SYMPHONY_API_BASE", "http://127.0.0.1:7890").rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base_url}/api/v1/refresh", json={}) as resp:
                return resp.status < 400
    except Exception:
        return False
