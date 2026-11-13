"""Symphony routing helpers for MCP tool calls."""

from __future__ import annotations

import json
import os
import copy
import uuid
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from ..runtime.shared_tracker import SharedTracker
from ..task.orchestration import OrchestrationManager, build_action_policy
from ..tools.base import ToolContext
from ..workspace_lifecycle import managed_workspace_generation_for_path
from .executor_config import resolve_executor_mcp_config
from ._utils import (
    _build_issue_fingerprint,
    _build_submit_workspace_hint,
    _choose_async_execution_channel,
    _context_control_workdir,
    _context_workdir,
    _context_workspace_claims,
    _direct_servers,
    _enforce_direct_whitelist,
    _env_truthy,
    _extract_parent_session_id,
    _extract_preview_ports,
    _find_duplicate_issue,
    _inject_async_target_identity,
    _inject_context_workspace_root_hint,
    _inject_workspace_root_hint,
    _load_issue_store,
    _never_route_servers,
    _normalize_routed_tool_args,
    _prune_redundant_issues_by_fingerprint,
    _routed_servers,
    _save_issue_store,
    _symphony_memory_issues_path,
    _tracker_db_path_text,
    _trigger_symphony_refresh,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _record_mcp_job_runtime(
    *,
    identifier: str,
    status: str,
    phase: str,
    server_name: str,
    tool_name: str,
    context: Optional[ToolContext],
    execution_channel: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """把 MCP 异步任务写入统一 TaskRuntime，并固化 Owner 三元组。"""

    try:
        from ..runtime.task_runtime import (
            TaskRuntimeStore,
            task_runtime_id_for_external_job,
        )

        owner = _context_workspace_claims(context)
        supports_resume = True
        if server_name.startswith('plugin-'):
            from ..plugins.async_targets import find_target
            target = find_target(_context_control_workdir(context), server_name,
                                 owner=f"{owner.get('tenant_id', '')}/{owner.get('user_id', '')}")
            supports_resume = bool(target and target['execution']['mode'] == 'remote-job')
        TaskRuntimeStore.record_event(
            runtime_id=task_runtime_id_for_external_job(identifier),
            runtime_kind="mcp_job",
            status=status,
            phase=phase,
            source="symphony_route",
            session_id=str(getattr(context, "session_id", "") or "").strip(),
            title=f"{server_name}.{tool_name}",
            external_id=identifier,
            tool_name=tool_name,
            metadata={
                "owner": {
                    "tenant_id": str(owner.get("tenant_id") or ""),
                    "user_id": str(owner.get("user_id") or ""),
                    "workspace_id": str(owner.get("workspace_id") or ""),
                },
                "identifier": identifier,
                "server_name": server_name,
                "tool_name": tool_name,
                "execution_channel": execution_channel,
                "supports_resume": supports_resume,
                "interrupt_behavior": "async_job",
                **dict(metadata or {}),
            },
            workdir=_context_control_workdir(context) or None,
        )
    except Exception:
        pass


def validate_routed_request(
    *,
    server_name: str,
    tool_name: str,
    normalized_args: dict,
    context: Optional[ToolContext],
) -> Optional[str]:
    """Preflight validation before enqueuing noisy routed tasks."""
    if server_name.lower() != "gnina-mcp" or tool_name != "gnina_mcp_server":
        return None

    payload = normalized_args
    if isinstance(payload.get("input_config"), dict):
        payload = payload["input_config"]

    if not isinstance(payload, dict):
        return "gnina 参数校验失败: input_config 必须是对象。"

    receptor = payload.get("receptor")
    ligand = payload.get("ligand")

    def _present(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(
                str(value.get("kind") or "").strip().lower() == "artifact"
                and str(value.get("artifact_id") or "").strip()
            )
        return bool(str(value or "").strip())

    if not _present(receptor) or not _present(ligand):
        return "gnina 参数校验失败: 缺少必填字段 receptor 或 ligand。"

    workdir = Path(_context_workdir(context) or os.getcwd())

    def _exists(value: Any) -> bool:
        # Portable ArtifactRefs belong to the Artifact service and are
        # materialized by Job Protocol / the target MCP.  They must never be
        # interpreted as paths in the submitting Agent's workspace.
        if isinstance(value, dict):
            return _present(value)
        value = str(value or "").strip()
        try:
            from ..workspace.async_protein_run import resolve_context_input_path

            resolved = resolve_context_input_path(context, value)
            if resolved is not None:
                return resolved.is_file()
        except Exception:
            pass
        path = Path(value)
        if not path.is_absolute():
            path = workdir / path
        return path.is_file()

    if not _exists(receptor):
        return f"gnina 参数校验失败: receptor 文件不存在: {receptor}"
    if not _exists(ligand):
        return f"gnina 参数校验失败: ligand 文件不存在: {ligand}"
    return None


def should_route_to_symphony(
    *, server_name: str, context: Optional[ToolContext] = None
) -> bool:
    server = str(server_name or "").strip().lower()
    from .manager_auto import MCPManager

    configured = next((entry for name, entry in MCPManager._server_configs.items()
                       if name.lower() == server), {})
    mode = str(configured.get("invocation_mode") or "auto").strip().lower()
    if mode == "direct":
        return False
    if mode == "async":
        return server not in _never_route_servers()
    direct = _direct_servers()

    if server in _never_route_servers():
        return False

    if _enforce_direct_whitelist() and direct and server not in direct:
        return True

    if context and getattr(context, "extra", None):
        forced = context.extra.get("route_symphony_on_mcp_tool")
        if isinstance(forced, bool):
            if not forced:
                return False
            if direct:
                return server not in direct
            return server in _routed_servers()

    if not _env_truthy("SYMPHONY_ROUTE_ON_MCP_TOOL", default=False):
        return False
    if direct:
        return server not in direct
    return server in _routed_servers()


def _coerce_timeout_seconds(value: Any, *, unit: str, default: int) -> int:
    try:
        explicit_zero = Decimal(str(value).strip()).is_zero()
    except (InvalidOperation, TypeError, ValueError):
        explicit_zero = False
    if unit == "ms":
        try:
            value = int(float(str(value).strip()) / 1000)
        except Exception:
            return default
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return default
    # Explicit zero disables the job's total waiting deadline, not connection checks.
    return 0 if explicit_zero else max(60, min(parsed, 24 * 60 * 60))


def _tool_timeout_from_mapping(
    mapping: Any,
    tool_name: str,
    *,
    unit: str,
    default: int,
) -> Optional[int]:
    if not isinstance(mapping, dict):
        return None
    tool = str(tool_name or "").strip()
    for candidate in (tool, tool.lower(), tool.replace("-", "_"), tool.replace("_", "-")):
        if candidate not in mapping:
            continue
        value = mapping.get(candidate)
        if isinstance(value, dict):
            for key, item_unit in (
                ("call_timeout_seconds", "seconds"),
                ("timeout_seconds", "seconds"),
                ("call_timeout", "ms"),
                ("timeout", "ms"),
                ("timeout_ms", "ms"),
            ):
                if key in value:
                    return _coerce_timeout_seconds(
                        value.get(key),
                        unit=item_unit,
                        default=default,
                    )
            return None
        return _coerce_timeout_seconds(value, unit=unit, default=default)
    return None


def _configured_routed_mcp_timeout_seconds(
    *,
    server_name: str,
    tool_name: str,
    mcp_config: Optional[Path],
    default: int,
) -> Optional[int]:
    if not mcp_config or not mcp_config.is_file():
        return None
    try:
        payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    server_cfg = payload.get(server_name)
    if not isinstance(server_cfg, dict):
        server_cfg = payload.get(str(server_name or "").strip().lower())
    if not isinstance(server_cfg, dict):
        return None

    for field, unit in (
        ("tool_timeout_seconds", "seconds"),
        ("tool_timeouts_seconds", "seconds"),
        ("tool_call_timeout_seconds", "seconds"),
        ("tool_timeouts", "ms"),
        ("tool_call_timeouts", "ms"),
    ):
        resolved = _tool_timeout_from_mapping(
            server_cfg.get(field),
            tool_name,
            unit=unit,
            default=default,
        )
        if resolved is not None:
            return resolved

    tools_cfg = server_cfg.get("tools")
    resolved = _tool_timeout_from_mapping(
        tools_cfg,
        tool_name,
        unit="ms",
        default=default,
    )
    if resolved is not None:
        return resolved

    for key, unit in (
        ("call_timeout_seconds", "seconds"),
        ("timeout_seconds", "seconds"),
        ("call_timeout", "ms"),
    ):
        if key in server_cfg:
            return _coerce_timeout_seconds(
                server_cfg.get(key),
                unit=unit,
                default=default,
            )
    return None


def _routed_mcp_timeout_seconds(
    *,
    server_name: str = "",
    tool_name: str = "",
    mcp_config: Optional[Path] = None,
) -> int:
    raw = os.environ.get("SYMPHONY_ROUTED_MCP_TIMEOUT_SECONDS", "0")
    default = _coerce_timeout_seconds(raw or "0", unit="seconds", default=0)
    configured = _configured_routed_mcp_timeout_seconds(
        server_name=server_name,
        tool_name=tool_name,
        mcp_config=mcp_config,
        default=default,
    )
    return configured if configured is not None else default


def build_job_protocol_submit_input(
    *,
    server_name: str,
    tool_name: str,
    tool_args: dict,
    context: Optional[ToolContext],
    issue_id: Optional[str] = None,
    issue_identifier: Optional[str] = None,
    description: str = "",
    input_schema: Optional[dict] = None,
    execution_workdir: str = "",
) -> dict[str, Any]:
    from .custom_targets import pinned_target
    from .dynamic_headers import snapshot
    business_args, call_context = snapshot(server_name, tool_args, context)
    local_gateway = (bool(os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"))
                     and _choose_async_execution_channel(server_name, context) == "bridge")
    normalized_args = (copy.deepcopy(business_args) if pinned_target(context, server_name) or local_gateway
                       else _normalize_routed_tool_args(business_args, tool_name))
    run_id = issue_id or f"local-{uuid.uuid4().hex[:10]}"
    identifier = issue_identifier or f"LOCAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    context_workdir = str(execution_workdir or _context_workdir(context) or "").strip()
    workdir = (
        Path(context_workdir).expanduser().resolve(strict=False)
        if context_workdir
        else PROJECT_ROOT
    )
    job_dir = (workdir / "runs" / "symphony_routed" / run_id).resolve(strict=False)
    owner_root_text = str(
        _context_control_workdir(context) or _context_workdir(context) or ""
    ).strip()
    owner_root = (
        Path(owner_root_text).expanduser().resolve(strict=False)
        if owner_root_text
        else workdir
    )
    try:
        execution_root_ref = workdir.relative_to(owner_root).as_posix() or "."
        job_dir_ref = job_dir.relative_to(owner_root).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            "异步执行目录必须位于所属 Workspace 内，不能把宿主机绝对路径交给 TMCP。"
        ) from exc
    configured_executor_mcp_config = str(
        os.environ.get("TCODE_EXECUTOR_MCP_CONFIG") or ""
    ).strip()
    executor_mcp_config = resolve_executor_mcp_config(
        server_name,
        current_config=configured_executor_mcp_config or None,
        workdir=workdir,
        config_root=os.environ.get("TCODE_CONFIG_ROOT") or PROJECT_ROOT,
    )

    routed_args = _inject_context_workspace_root_hint(
        server_name,
        normalized_args,
        context,
        tool_name,
    )
    # Demo tools have a strict, workspace-independent schema. The broker
    # envelope still carries the trusted workspace and run identity.
    if server_name != "simple-demo-mcp":
        routed_args = _inject_async_target_identity(
            routed_args,
            context,
            issue_id=run_id,
            issue_identifier=identifier,
        )
    input_snapshot = copy.deepcopy(routed_args)
    input_snapshot = _inject_workspace_root_hint(input_snapshot, execution_root_ref)
    # TMCP resolves the host path from workspace claims and execution_root.
    # Do not serialize a macOS/Linux-specific absolute path into the job input.
    if isinstance(routed_args.get("input_config"), dict):
        routed_args["input_config"].pop("workspace_root", None)
    else:
        routed_args.pop("workspace_root", None)

    if server_name.startswith('plugin-') or local_gateway:
        # Identity belongs to the broker envelope, not third-party tool args.
        routed_args = copy.deepcopy(business_args)
        input_snapshot = copy.deepcopy(business_args)

    def _portable_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: _portable_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_portable_value(item) for item in value]
        if not isinstance(value, str) or "://" in value:
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            return value
        try:
            return path.resolve(strict=False).relative_to(workdir).as_posix()
        except ValueError:
            return value

    if not server_name.startswith('plugin-') and not local_gateway:
        routed_args = _portable_value(routed_args)
    from .custom_targets import pinned_target
    if not pinned_target(context, server_name):
        input_snapshot = _portable_value(input_snapshot)
    timeout_seconds = _routed_mcp_timeout_seconds(
        server_name=server_name,
        tool_name=tool_name,
        mcp_config=executor_mcp_config,
    )
    claims = _context_workspace_claims(context)
    agent_id = str(getattr(context, "agent", "") or "tcode-agent").strip()

    return {
        "run_id": run_id,
        "tool": server_name,
        "task_type": tool_name,
        "target_server": server_name,
        "target_tool": tool_name,
        "arguments": routed_args,
        **({'call_context': call_context} if call_context else {}),
        "workdir": job_dir_ref,
        "execution_root": execution_root_ref,
        "expected_artifacts": ["mcp_result.json"],
        "issuer": "tcode",
        "agent_id": agent_id,
        "subject": claims.get("user_id", ""),
        "tenant_id": claims.get("tenant_id", ""),
        "user_id": claims.get("user_id", ""),
        "workspace_id": claims.get("workspace_id", ""),
        "capabilities": ["workspace:read", "workspace:write", "job:submit"],
        "workspace": _build_submit_workspace_hint(
            context=context,
            issue_identifier=identifier,
            run_id=run_id,
            target_server=server_name,
            target_tool=tool_name,
            job_dir=job_dir,
            expected_artifacts=["mcp_result.json"],
            preview_ports=_extract_preview_ports(normalized_args),
            input_snapshot=input_snapshot,
        ),
        "timeout_seconds": timeout_seconds,
        "retry_policy": {
            "max_retries": int(
                os.environ.get("SYMPHONY_ROUTED_MCP_MAX_RETRIES", "5")
            ),
            "backoff_seconds": int(
                os.environ.get("SYMPHONY_ROUTED_MCP_RETRY_BACKOFF_SECONDS", "2")
            ),
        },
    }


async def enqueue_symphony_local_issue(
    *,
    server_name: str,
    tool_name: str,
    normalized_args: dict,
    context: Optional[ToolContext],
    description: str = "",
    input_schema: Optional[dict] = None,
    admission=None,
) -> dict[str, Any]:
    from ..runtime.async_resume import assert_submission_allowed
    assert_submission_allowed(server_name, tool_name)
    execution_channel = _choose_async_execution_channel(server_name, context)
    target_version = ""
    if execution_channel == "bridge" and os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"):
        claims = _context_workspace_claims(context)
        if server_name.startswith("plugin-"):
            from ..plugins.async_targets import approved_target_version
            target_version = approved_target_version(
                _context_control_workdir(context), f"{claims['tenant_id']}/{claims['user_id']}", server_name,
            )
        else:
            from .manager_auto import MCPManager
            from ._proxy import _extract_contract_payload
            observed = await MCPManager.call_tool("job-protocol-mcp", "describe_target_tool", {
                "input": {"target_server": server_name, "target_tool": tool_name, "detail": "compact", "fields": [], **claims},
            })
            target_version = str(_extract_contract_payload(observed).get("target_version") or "")
        if not target_version:
            raise ValueError("无法确认异步目标的获批版本，未入队")
    workdir_text = _context_control_workdir(context)
    tracker_workdir = workdir_text or str(
        os.environ.get("TCODE_WORKDIR", "") or ""
    ).strip()
    issue_path = _symphony_memory_issues_path(tracker_workdir)
    store = _load_issue_store(issue_path)
    issues = store.get("issues", [])
    if not isinstance(issues, list):
        issues = []

    prune_refresh_triggered = False
    issues, removed = _prune_redundant_issues_by_fingerprint(issues)
    if removed > 0:
        store["issues"] = issues
        _save_issue_store(issue_path, store)
        prune_refresh_triggered = await _trigger_symphony_refresh()

    from .custom_targets import exact_fingerprint_arguments, pinned_target
    tool_args = (copy.deepcopy(normalized_args) if pinned_target(context, server_name)
                 else _normalize_routed_tool_args(normalized_args, tool_name))
    fingerprint_args = exact_fingerprint_arguments(tool_args) if pinned_target(context, server_name) else tool_args
    from .dynamic_headers import fingerprint_arguments, rules_for
    fingerprint_args = fingerprint_arguments(server_name, fingerprint_args, context, values_from=tool_args)
    if target_version:
        fingerprint_args = {"target_version": target_version, "arguments": fingerprint_args}
    if rules_for(server_name, context):
        from .dynamic_headers import ensure_broker_support
        await ensure_broker_support(server_name, tool_name, context)
    session_id = str(getattr(context, "session_id", "") or "")
    dedupe_fingerprint = _build_issue_fingerprint(
        server_name,
        tool_name,
        session_id,
        fingerprint_args,
    )
    duplicate = _find_duplicate_issue(
        issues,
        dedupe_fingerprint,
        failed_cooldown_seconds=int(
            os.environ.get("SYMPHONY_FAILED_DUP_COOLDOWN_SECONDS", "120")
        ),
    )
    if duplicate is not None:
        if admission is not None:
            async with admission():
                pass
        duplicate_identifier = str(duplicate.get("identifier", "") or "")
        duplicate_state = str(duplicate.get("state", ""))
        try:
            from ..workspace.async_protein_run import extract_link

            duplicate_protein_run = extract_link(duplicate)
        except Exception:
            duplicate_protein_run = {}
        if duplicate_identifier:
            _record_mcp_job_runtime(
                identifier=duplicate_identifier,
                status=(
                    "completed"
                    if duplicate_state.strip().lower() in {"done", "closed", "completed"}
                    else "waiting_external"
                ),
                phase="deduped",
                server_name=server_name,
                tool_name=tool_name,
                context=context,
                execution_channel=execution_channel,
                metadata={
                    "duplicate_state": duplicate_state,
                    "duplicate_result_summary": str(duplicate.get("result_summary", "")),
                    "dedupe_fingerprint": dedupe_fingerprint,
                },
            )
        return {
            "id": duplicate.get("id", ""),
            "identifier": duplicate.get("identifier", ""),
            "acceptance_id": duplicate.get("identifier", ""),
            "identifier_kind": "local_acceptance_id",
            "remote_job_id": "",
            "title": duplicate.get("title", ""),
            "status": "deduped",
            "duplicate_state": str(duplicate.get("state", "")),
            "duplicate_result_summary": str(duplicate.get("result_summary", "")),
            "tracker": "sqlite",
            "issue_store": _tracker_db_path_text(tracker_workdir),
            "refresh_triggered": prune_refresh_triggered,
            "routed_server": server_name,
            "routed_tool": tool_name,
            "route_mode": f"job-protocol-via-{execution_channel}",
            "execution_channel": execution_channel,
            "protein_run": duplicate_protein_run,
        }

    issue_id = f"local-{uuid.uuid4().hex[:10]}"
    identifier = f"LOCAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    now = datetime.utcnow().isoformat() + "Z"
    from ..workspace.async_protein_run import ensure_for_submission

    try:
        from ..workspace.input_resources import prepare_input_file_contract
        await prepare_input_file_contract(server_name, tool_name, tool_args, context)
        protein_run_link = ensure_for_submission(
            context=context,
            identifier=identifier,
            server_name=server_name,
            tool_name=tool_name,
            arguments=tool_args,
            execution_channel=execution_channel,
        )
    except Exception as exc:
        raise RuntimeError(
            "异步任务未入队：无法创建持久 Protein Run 输入快照："
            f"{exc}"
        ) from exc
    routed_tool_args = tool_args
    if protein_run_link:
        try:
            from ..workspace.async_protein_run import remap_submission_arguments

            routed_tool_args = remap_submission_arguments(
                protein_run_link,
                tool_args,
            )
        except Exception as exc:
            try:
                from ..workspace.async_protein_run import update_status

                update_status(
                    protein_run_link,
                    identifier=identifier,
                    status="failed",
                    phase="input_snapshot_failed",
                    target_server=server_name,
                    target_tool=tool_name,
                    error_summary=str(exc),
                )
            except Exception:
                pass
            raise RuntimeError(
                "异步任务未入队：输入文件未能映射到持久 Protein Run："
                f"{exc}"
            ) from exc
    durable_execution_workdir = str(
        (protein_run_link.get("run_root") or "") if isinstance(protein_run_link, dict) else ""
    ).strip()
    submit_input = build_job_protocol_submit_input(
        server_name=server_name,
        tool_name=tool_name,
        tool_args=routed_tool_args,
        context=context,
        issue_id=issue_id,
        issue_identifier=identifier,
        description=description,
        input_schema=input_schema,
        execution_workdir=durable_execution_workdir,
    )
    workspace_owner = _context_workspace_claims(context)
    workspace_generation = managed_workspace_generation_for_path(
        tracker_workdir or _context_control_workdir(context)
    )
    workspace_owner["workspace_generation"] = workspace_generation
    submit_workspace = (
        dict(submit_input.get("workspace"))
        if isinstance(submit_input.get("workspace"), dict)
        else {}
    )
    submit_workspace.update(workspace_owner)
    if execution_channel == "bridge" and os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"):
        submit_workspace["job_protocol_backend"] = "tcode-local"
        submit_input["target_version"] = target_version
    from ..workspace.input_resources import input_file_contract
    file_contract = input_file_contract(context, server_name, tool_name)
    if file_contract is not None:
        submit_workspace["input_file_contract"] = file_contract
    from .custom_targets import pinned_target
    custom_target = pinned_target(context, server_name)
    if custom_target:
        # Inputs are already durable ArtifactRefs/BundleRefs. Keep their exact
        # bindings and declaration for inspection, never reparse them on resume.
        submit_workspace['custom_mcp_input_transport'] = {
            'declaration': custom_target,
            'bindings': copy.deepcopy(getattr(context, '_custom_mcp_upload_bindings', {}).get((server_name, tool_name), [])),
        }
    if protein_run_link:
        submit_workspace["protein_run"] = protein_run_link
    submit_workspace["action_policy"] = build_action_policy(
        lifecycle_status="queued",
        job_id="",
        has_submit_input=True,
        resume_supported=bool(submit_workspace.get("resume_supported", False)),
        resume_command=str(submit_workspace.get("resume_command", "") or ""),
    )
    submit_input["workspace"] = submit_workspace

    orchestration_payload: dict[str, Any] = {}
    tool_contract: dict[str, Any] = {}
    try:
        context_extra = getattr(context, "extra", None)
        if not isinstance(context_extra, dict):
            context_extra = {}
        requested_plan_id = str(
            context_extra.get("orchestration_plan_id")
            or context_extra.get("plan_id")
            or ""
        ).strip()
        orchestration_info = OrchestrationManager.ensure_mcp_task_plan(
            identifier=identifier,
            issue_id=issue_id,
            title=f"{server_name}.{tool_name}",
            server_name=server_name,
            tool_name=tool_name,
            tool_args=tool_args,
            execution_channel=execution_channel,
            submit_input=submit_input,
            session_id=session_id,
            workdir=tracker_workdir,
            plan_id=requested_plan_id,
        )
        orchestration_payload = (
            orchestration_info.get("orchestration")
            if isinstance(orchestration_info.get("orchestration"), dict)
            else {}
        )
        tool_contract = (
            orchestration_info.get("tool_contract")
            if isinstance(orchestration_info.get("tool_contract"), dict)
            else {}
        )
        if orchestration_payload:
            submit_workspace["orchestration"] = orchestration_payload
        if tool_contract:
            submit_workspace["tool_contract"] = tool_contract
        submit_input["workspace"] = submit_workspace
    except Exception:
        orchestration_payload = {}
        tool_contract = {}

    orchestration_text = "Symphony" if execution_channel == "symphony" else "Bridge"
    trace_context = {}
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict) and isinstance(extra.get("tool_trace_context"), dict):
        trace_context = {
            str(key): str(value or "").strip()
            for key, value in extra.get("tool_trace_context", {}).items()
            if str(value or "").strip()
        }
    from ..runtime.async_completion import normalize_continuation_policy

    workflow_owned = bool(str(trace_context.get("workflow_run_id") or "").strip())
    continuation_policy = normalize_continuation_policy(
        extra.get("async_continuation_policy") if isinstance(extra, dict) else "",
        workflow_owned=workflow_owned,
    )
    origin_turn_id = ""
    if isinstance(extra, dict):
        origin_turn_id = str(extra.get("turn_id") or "").strip()
    if not origin_turn_id:
        for message in reversed(list(getattr(context, "messages", []) or [])):
            if str(getattr(message, "role", "") or "").strip().lower() != "user":
                continue
            origin_turn_id = str(getattr(message, "id", "") or "").strip()
            if origin_turn_id:
                break
    issue_description = (
        f"Auto-routed MCP request for {orchestration_text} orchestration via job-protocol.\n\n"
        "Only do this sequence and do not call the target MCP tool directly:\n"
        "1) mcp_job-protocol-mcp_submit_job\n"
        "2) mcp_job-protocol-mcp_get_job_status (poll to terminal state)\n"
        "3) mcp_job-protocol-mcp_collect_job_result\n\n"
        "IMPORTANT: mcp_job-protocol-mcp_get_job_status must use job_id/process_id returned by submit_job.\n"
        "Never use Symphony identifier (LOCAL-*) as job_id.\n"
        "If get_job_status returns 'job_id not found', keep the original submit_job IDs and retry polling.\n\n"
        "If submit_job/get_job_status/collect_job_result fails, stop and report failure. "
        "Do NOT call target MCP tool directly for verification.\n\n"
        f"target_server: {server_name}\n"
        f"target_tool: {tool_name}\n"
        f"session_id: {getattr(context, 'session_id', '')}\n"
        f"workdir: {getattr(context, 'workdir', '')}\n\n"
        "submit_job.input:\n"
        f"{json.dumps(submit_input, ensure_ascii=False, indent=2)}\n\n"
        "original_tool_arguments:\n"
        f"{json.dumps(tool_args, ensure_ascii=False, indent=2)}"
    )

    from ..runtime.async_resume import capture_task_context
    task_context = await capture_task_context(context)
    issue_record = {
        "id": issue_id,
        "identifier": identifier,
        "title": f"SMOKE: submit_job via {orchestration_text} - {server_name}.{tool_name}",
        "description": issue_description,
        "tool_server": server_name,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "resume_data": {
            "session_id": session_id,
            "agent_name": str(getattr(context, "agent", "") or "").strip(),
            "parent_session_id": _extract_parent_session_id(context),
            "target_server": server_name,
            "target_tool": tool_name,
            "origin_turn_id": origin_turn_id,
            "task_context": task_context,
            "origin_request_id": str(
                (extra.get("request_id") or "") if isinstance(extra, dict) else ""
            ).strip(),
            "continuation_policy": continuation_policy,
            "workflow_run_id": str(trace_context.get("workflow_run_id", "") or "").strip(),
            "workflow_stage_id": str(trace_context.get("workflow_stage_id", "") or "").strip(),
            "workflow_template_key": str(trace_context.get("workflow_template_key", "") or "").strip(),
            "workflow_owner_session_id": str(trace_context.get("workflow_owner_session_id", "") or "").strip(),
            "protein_run": protein_run_link,
        },
        "auto_resume_status": "pending",
        "fingerprint": dedupe_fingerprint,
        "execution_channel": execution_channel,
        "state": "Todo",
        "labels": [
            "local",
            execution_channel,
            "auto-routed",
            "job-protocol",
            server_name,
        ],
        "created_at": now,
        "updated_at": now,
        "workspace": submit_workspace,
        "metadata": {
            "orchestration": orchestration_payload,
            "tool_contract": tool_contract,
            "tool_trace_context": trace_context,
            "protein_run": protein_run_link,
        },
    }
    issue_record = SharedTracker.bind_workspace_owner(
        issue_record,
        tenant_id=workspace_owner["tenant_id"],
        user_id=workspace_owner["user_id"],
        workspace_id=workspace_owner["workspace_id"],
        workspace_generation=workspace_generation,
    )
    from ..runtime.asset_history import record_async_job

    await record_async_job(workdir_text, identifier, session=str(getattr(context, "session_id", "") or ""))
    try:
        async with admission() if admission is not None else nullcontext():
            SharedTracker.upsert_issue(
                issue_record,
                tracker_workdir,
                source="agent_integration",
                append_event=True,
            )
    except Exception as exc:
        await record_async_job(workdir_text, identifier, completed=True)
        if protein_run_link:
            try:
                from ..workspace.async_protein_run import update_status

                update_status(
                    protein_run_link,
                    identifier=identifier,
                    status="failed",
                    phase="enqueue_failed",
                    target_server=server_name,
                    target_tool=tool_name,
                    error_summary=str(exc),
                )
            except Exception:
                pass
        raise
    if orchestration_payload:
        try:
            task = SharedTracker.get_task(identifier=identifier, workdir=tracker_workdir)
            if task:
                OrchestrationManager.reconcile_from_task(task, tracker_workdir)
        except Exception:
            pass
    _record_mcp_job_runtime(
        identifier=identifier,
        status="waiting_external",
        phase="queued",
        server_name=server_name,
        tool_name=tool_name,
        context=context,
        execution_channel=execution_channel,
        metadata={
            "id": issue_id,
            "dedupe_fingerprint": dedupe_fingerprint,
            "issue_store": _tracker_db_path_text(tracker_workdir),
            "submit_input": submit_input,
            "protein_run": protein_run_link,
        },
    )
    refresh_triggered = (
        await _trigger_symphony_refresh() if execution_channel == "symphony" else False
    )
    refresh_triggered = refresh_triggered or prune_refresh_triggered

    return {
        "id": issue_id,
        "identifier": identifier,
        "acceptance_id": identifier,
        "identifier_kind": "local_acceptance_id",
        "remote_job_id": "",
        "title": issue_record["title"],
        "status": "queued",
        "tracker": "sqlite",
        "issue_store": _tracker_db_path_text(tracker_workdir),
        "refresh_triggered": refresh_triggered,
        "routed_server": server_name,
        "routed_tool": tool_name,
        "route_mode": f"job-protocol-via-{execution_channel}",
        "execution_channel": execution_channel,
        "submit_input": submit_input,
        "orchestration": orchestration_payload,
        "tool_contract": tool_contract,
        "protein_run": protein_run_link,
    }
