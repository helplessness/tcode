"""Bind routed protein MCP jobs to durable Protein Run workspaces.

The foreground agent only submits work through ``job-protocol-mcp``.  This
module keeps that asynchronous control-plane job and the scientific Run
workspace in one lifecycle without changing the target MCP server protocol.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from ..artifact_transport import materialize_output_bundle
from ..runtime.resource_import import resolve_resource_source
from ..tools.base import ToolContext
from .manager import AgentWorkspace, AgentWorkspaceManager
from .protein_run import run_root_for
from .input_resources import declared_resource_paths


_DEFAULT_PROTEIN_COMPUTE_SERVERS = {
    "boltz-mcp",
    "evolvepro-mcp",
    "gnina-mcp",
    "ligandmpnn-mcp",
    "mutation-scoring-mcp",
    "placer-mcp",
    "plip-mcp",
    "rfdiffusion-mcp",
    "simulation-mcp",
    "pythia-mcp",
    "foldx-mcp",
    "homology-mcp",
}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_OUTPUT_PATH_KEYS = {
    "cache_dir",
    "log_dir",
    "logs_dir",
    "output",
    "output_dir",
    "output_file",
    "output_path",
    "outputs_dir",
    "result_dir",
    "result_file",
    "result_path",
    "save_dir",
    "save_path",
    "tmp_dir",
    "workdir",
    "workspace_root",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.environ.get(name, default)).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _configured_servers() -> set[str]:
    raw = str(os.environ.get("TCODE_PROTEIN_RUN_ASYNC_SERVERS", "") or "").strip()
    if not raw:
        return set(_DEFAULT_PROTEIN_COMPUTE_SERVERS)
    return {
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }


def should_track_server(server_name: str) -> bool:
    return (
        _env_bool("TCODE_PROTEIN_RUN_ASYNC_TRACKING", True)
        and str(server_name or "").strip().lower() in _configured_servers()
    )


def _context_extra(context: Optional[ToolContext]) -> dict[str, Any]:
    extra = getattr(context, "extra", None) if context is not None else None
    return extra if isinstance(extra, dict) else {}


def _context_control_workdir(context: Optional[ToolContext]) -> str:
    extra = _context_extra(context)
    workspace = extra.get("agent_workspace")
    if isinstance(workspace, dict):
        value = str(workspace.get("control_workdir") or "").strip()
        if value:
            return value
    for key in ("control_workdir", "workdir"):
        value = str(extra.get(key) or "").strip()
        if value:
            return value
    return str(os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()


def _context_execution_workdir(context: Optional[ToolContext]) -> str:
    extra = _context_extra(context)
    workspace = extra.get("agent_workspace")
    if isinstance(workspace, dict):
        for key in ("execution_workdir", "workspace_root", "worktree_root"):
            value = str(workspace.get(key) or "").strip()
            if value:
                return value
    value = str(extra.get("workdir") or "").strip()
    return value or _context_control_workdir(context)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def resolve_context_input_path(
    context: Optional[ToolContext],
    value: Any,
) -> Optional[Path]:
    """Resolve host and ``/workspace`` input paths before async isolation.

    Container and relative paths belong to the submitting agent's execution
    workspace.  That exact file may contain changes which are not present in
    the control workspace, so resolve execution first and snapshot it before
    the asynchronous job is queued.  The control workspace is only a fallback
    for resources that were not materialized in the isolated workspace.
    """

    text = str(value or "").strip()
    if not text or "\n" in text or len(text) > 4096:
        return None
    control_text = _context_control_workdir(context)
    execution_text = _context_execution_workdir(context)
    roots: list[str] = []
    for candidate in (
        execution_text,
        control_text,
    ):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in roots:
            roots.append(candidate)
    for root in roots:
        try:
            resolved = resolve_resource_source(text, root)
        except PermissionError:
            raise  # Explicit denial must not fall back to a different root.
        except Exception:
            continue
        if resolved.is_file() or resolved.is_dir():
            return resolved.resolve(strict=False)
    return None


def _argument_value_may_be_path(value: str, key: str) -> bool:
    text = str(value or "").strip()
    if not text or "\n" in text or len(text) > 4096:
        return False
    if "://" in text or text.startswith(("data:", "urn:")):
        return False
    key_text = str(key or "").strip().lower()
    if key_text in _OUTPUT_PATH_KEYS:
        return False
    if text.startswith(("/", "./", "../", "~/")) or "/" in text or "\\" in text:
        return True
    if Path(text).suffix:
        return True
    # A model name alone is not a file. Explicit paths, file suffixes and
    # model_path/model_file/checkpoint fields still take the snapshot path.
    return any(
        token in key_text
        for token in (
            "checkpoint",
            "directory",
            "fasta",
            "file",
            "ligand",
            "path",
            "receptor",
            "structure",
            "template",
        )
    )


def _argument_path_bindings(
    arguments: dict[str, Any],
    context: Optional[ToolContext],
    *, server_name: str = "", tool_name: str = "",
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    declared = declared_resource_paths(server_name, tool_name, arguments, context=context)

    def _walk(value: Any, path: list[Any], key: str = "") -> None:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                _walk(nested_value, [*path, str(nested_key)], str(nested_key))
            return
        if isinstance(value, list):
            for index, nested_value in enumerate(value):
                _walk(nested_value, [*path, index], key)
            return
        if not isinstance(value, str):
            return
        if declared is not None:
            if tuple(path) not in declared:
                return
        elif not _argument_value_may_be_path(value, key):
            return
        resolved = resolve_context_input_path(context, value)
        if resolved is None:
            raise FileNotFoundError(
                "async input path could not be resolved before enqueue: "
                f"{'.'.join(str(token) for token in path)}={value}"
            )
        if declared is not None and declared[tuple(path)] == "file" and not resolved.is_file():
            raise ValueError(f"输入文件字段不能传入目录：{'.'.join(map(str, path))}")
        if declared is not None and declared[tuple(path)] == "directory" and not resolved.is_dir():
            raise ValueError(f"输入目录字段不能传入文件：{'.'.join(map(str, path))}")
        control = Path(_context_control_workdir(context)).expanduser().resolve(
            strict=False
        )
        execution = Path(_context_execution_workdir(context)).expanduser().resolve(
            strict=False
        )
        durability_required = (
            not _inside(resolved, control)
            or (execution != control and _inside(resolved, execution))
        )
        bindings.append(
            {
                "argument_path": path,
                "original_value": value,
                # Protein Run's resource importer snapshots/mounts this path
                # and rewrites it to the durable host-side input path.
                "path": resolved.as_posix(),
                # A target MCP can start after the submitting subagent has
                # already been cleaned. Such sources must be copied into the
                # async Run even when they exceed the normal auto-copy limit.
                "durability_required": durability_required,
            }
        )

    _walk(arguments, [])
    return bindings


def _existing_workspace(
    context: Optional[ToolContext],
    *,
    task_id: str,
) -> Optional[AgentWorkspace]:
    extra = _context_extra(context)
    payload = extra.get("agent_workspace")
    if not isinstance(payload, dict) or str(payload.get("strategy") or "") != "protein_run":
        return None
    workspace_id = str(payload.get("workspace_id") or "").strip()
    control_workdir = str(payload.get("control_workdir") or "").strip()
    if not workspace_id or not control_workdir:
        return None
    workspace = AgentWorkspaceManager.load(
        control_workdir=control_workdir,
        workspace_id=workspace_id,
    )
    if (
        workspace is None
        or workspace.strategy != "protein_run"
        or workspace.kind != "async-mcp"
        or str(workspace.task_id or "").strip() != str(task_id or "").strip()
    ):
        return None
    return workspace


def _resource_context_for_job(
    context: Optional[ToolContext],
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    extra = _context_extra(context)
    resources = extra.get("resource_context")
    mapped = dict(resources) if isinstance(resources, dict) else {}
    # Protein Run's resource importer recursively snapshots declared path-like
    # values.  Keeping the submission under a named object gives a standalone
    # one-tool Run the same immutable-input behavior as Task/Team runs.
    mapped["async_mcp_submission"] = {
        "server": str(server_name or ""),
        "tool": str(tool_name or ""),
        "path_bindings": _argument_path_bindings(arguments, context, server_name=server_name, tool_name=tool_name),
    }
    from .input_resources import input_file_contract
    contract = input_file_contract(context, server_name, tool_name)
    if contract is not None:
        mapped["async_mcp_submission"]["input_file_contract"] = contract
    if declared_resource_paths(server_name, tool_name, arguments, context=context) is None:
        mapped["async_mcp_submission"]["arguments"] = dict(arguments or {})
    # Audited calls import only path_bindings. Full arguments remain in the
    # external-job/provenance record, not in the importer's resource tree.
    return mapped


def _validated_submission_bindings(workspace: AgentWorkspace) -> list[dict[str, Any]]:
    resources = workspace.resource_context
    submission = resources.get("async_mcp_submission") if isinstance(resources, dict) else None
    bindings = submission.get("path_bindings") if isinstance(submission, dict) else None
    if not isinstance(bindings, list):
        raise RuntimeError("Run 的持久输入绑定缺失或损坏，已停止；不能重新推断输入")
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise RuntimeError("Run 的持久输入绑定损坏，已停止")
        path = binding.get("argument_path")
        host_path = binding.get("path")
        if (
            not isinstance(path, list) or not path
            or any(not ((type(p) is str and bool(p)) or (type(p) is int and p >= 0)) for p in path)
            or not isinstance(host_path, str) or not host_path.strip()
            or ("durability_required" in binding and type(binding["durability_required"]) is not bool)
        ):
            raise RuntimeError("Run 的持久输入绑定损坏，已停止")
        key = tuple(path)
        if key in seen:
            raise RuntimeError("Run 的持久输入绑定重复，已停止")
        seen.add(key)
    return bindings


def saved_submission_bindings(link: dict[str, Any]) -> list[dict[str, Any]]:
    """Read persisted bindings only; never rediscover inputs on queue recovery."""
    workspace = _load_workspace(link)
    if workspace is None:
        raise RuntimeError("durable async Protein Run could not be loaded")
    return copy.deepcopy(_validated_submission_bindings(workspace))


def _set_argument_path(payload: Any, path: list[Any], value: str) -> bool:
    current = payload
    for token in path[:-1]:
        if isinstance(token, int) and isinstance(current, list) and 0 <= token < len(current):
            current = current[token]
        elif isinstance(token, str) and isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return False
    if not path:
        return False
    final = path[-1]
    if isinstance(final, int) and isinstance(current, list) and 0 <= final < len(current):
        current[final] = value
        return True
    if isinstance(final, str) and isinstance(current, dict) and final in current:
        current[final] = value
        return True
    return False


def remap_submission_arguments(
    link: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite file inputs to durable Protein Run host paths.

    The target MCP is an HTTP service running outside the submitting agent's
    sandbox, so it must receive host paths.  ``/workspace/...`` paths are only
    meaningful inside that short-lived agent sandbox.
    """

    remapped = copy.deepcopy(dict(arguments or {}))
    workspace = _load_workspace(link)
    if workspace is None:
        raise RuntimeError("durable async Protein Run could not be loaded")
    bindings = _validated_submission_bindings(workspace)
    for binding in bindings:
        path = binding.get("argument_path")
        host_path = str(binding.get("path") or "").strip()
        candidate = Path(host_path).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(
                f"durable async input does not exist: {candidate}"
            )
        candidate = candidate.resolve(strict=False)
        if bool(binding.get("durability_required")) and not _inside(
            candidate,
            Path(workspace.inputs_dir),
        ):
            raise RuntimeError(
                "temporary async input was not snapshotted into the durable Run: "
                f"{candidate}"
            )
        if not _set_argument_path(remapped, path, candidate.as_posix()):
            raise RuntimeError(
                f"durable async input binding no longer matches arguments: {path}"
            )
    return remapped


def _link(workspace: AgentWorkspace, *, created: bool) -> dict[str, Any]:
    return {
        "schema": "tcode.protein-run-link.v1",
        "workspace_id": workspace.workspace_id,
        "run_id": workspace.workspace_id,
        "control_workdir": workspace.control_workdir,
        "run_root": workspace.workspace_root,
        "created_for_async_job": bool(created),
    }


def extract_link(*payloads: Any) -> dict[str, Any]:
    """Extract a Protein Run link from tracker, issue, or watcher payloads."""

    deleted_run_ids: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        tombstones = [
            payload.get("protein_run_deleted"),
            (payload.get("workspace") or {}).get("protein_run_deleted")
            if isinstance(payload.get("workspace"), dict)
            else None,
            (payload.get("resume_data") or {}).get("protein_run_deleted")
            if isinstance(payload.get("resume_data"), dict)
            else None,
            (payload.get("metadata") or {}).get("protein_run_deleted")
            if isinstance(payload.get("metadata"), dict)
            else None,
        ]
        for tombstone in tombstones:
            if isinstance(tombstone, dict):
                deleted_id = str(
                    tombstone.get("workspace_id") or tombstone.get("run_id") or ""
                ).strip()
            else:
                deleted_id = str(tombstone or "").strip()
            if deleted_id:
                deleted_run_ids.add(deleted_id)

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = [
            payload,
            payload.get("protein_run"),
            (payload.get("workspace") or {}).get("protein_run")
            if isinstance(payload.get("workspace"), dict)
            else None,
            (payload.get("resume_data") or {}).get("protein_run")
            if isinstance(payload.get("resume_data"), dict)
            else None,
            (payload.get("metadata") or {}).get("protein_run")
            if isinstance(payload.get("metadata"), dict)
            else None,
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            workspace_id = str(
                candidate.get("workspace_id") or candidate.get("run_id") or ""
            ).strip()
            control_workdir = str(candidate.get("control_workdir") or "").strip()
            if workspace_id and control_workdir and workspace_id not in deleted_run_ids:
                return {**candidate, "workspace_id": workspace_id, "run_id": workspace_id}
    return {}


def ensure_for_submission(
    *,
    context: Optional[ToolContext],
    identifier: str,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    execution_channel: str = "",
) -> dict[str, Any]:
    """Create a job-scoped durable Protein Run and mark it as queued.

    A foreground protein subagent Run is deliberately not reused.  Its files
    and terminal cleanup belong to the foreground task, while the target MCP
    may start much later.  Each routed job therefore owns an independent Run
    whose input snapshots survive the submitting agent.
    """

    if not should_track_server(server_name):
        return {}
    workspace = _existing_workspace(context, task_id=identifier)
    created = workspace is None
    if workspace is None:
        control_workdir = _context_control_workdir(context)
        workspace = AgentWorkspaceManager.create(
            control_workdir=control_workdir,
            task_id=str(identifier or "async-mcp-job"),
            session_id=str(getattr(context, "session_id", "") or ""),
            kind="async-mcp",
            strategy="protein_run",
            domain="protein",
            agent_name=str(getattr(context, "agent", "") or "protein_orchestrator"),
            resource_context=_resource_context_for_job(
                context,
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
            ),
            task_contract={
                "goal": f"Run {server_name}.{tool_name}",
                "deliverable": "Collected MCP result and registered output artifacts",
                "stop_when": "The external job reaches a terminal state",
                "allowed_tools": ["job-protocol-mcp", server_name],
            },
        )
    link = _link(workspace, created=created)
    AgentWorkspaceManager.record_external_job(
        workspace,
        identifier=identifier,
        target_server=server_name,
        target_tool=tool_name,
        status="waiting_external",
        phase="queued",
        arguments=dict(arguments or {}),
        metadata={"execution_channel": str(execution_channel or "")},
    )
    return link


def _load_workspace(link: dict[str, Any]) -> Optional[AgentWorkspace]:
    resolved = extract_link(link)
    if not resolved:
        return None
    return AgentWorkspaceManager.load(
        control_workdir=resolved["control_workdir"],
        workspace_id=resolved["workspace_id"],
    )


def needs_terminal_recovery(link: dict[str, Any], identifier: str) -> bool:
    """Return whether a terminal tracker task still needs Run settlement."""

    workspace = _load_workspace(link)
    if workspace is None:
        return False
    jobs = workspace.checkpoint_state.get("external_jobs")
    if not isinstance(jobs, list):
        return True
    matching = next(
        (
            item
            for item in jobs
            if isinstance(item, dict)
            and str(item.get("identifier") or "").strip()
            == str(identifier or "").strip()
        ),
        None,
    )
    if matching is None:
        return True
    return str(matching.get("status") or "").strip().lower() not in _TERMINAL_STATUSES


def discover_pending_runs(
    search_root: str | Path,
    *,
    limit: Optional[int] = 500,
) -> list[dict[str, Any]]:
    """Discover pending Run links after a server restart.

    Tracker databases may be scoped to an individual delegated Run.  The
    in-memory watcher registry cannot know those paths after restart, so scan
    only the managed Protein Run roots (never the whole filesystem).
    """

    root = Path(search_root or os.environ.get("TCODE_WORKDIR") or os.getcwd())
    root = root.expanduser().resolve(strict=False)
    run_roots: list[Path] = []

    def _add_run_root(value: Path) -> None:
        resolved = value.expanduser().resolve(strict=False)
        if resolved.is_dir() and resolved not in run_roots:
            run_roots.append(resolved)

    _add_run_root(run_root_for(root, "__discovery_probe__").parent)

    user_roots: list[Path] = []
    configured_users = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
    if configured_users:
        users_path = Path(configured_users).expanduser()
        if not users_path.is_absolute():
            users_path = root / users_path
        user_roots.append(users_path.resolve(strict=False))
    user_roots.append((root / "workdirs").resolve(strict=False))
    for users_root in user_roots:
        if not users_root.is_dir():
            continue
        for workspace_dir in users_root.glob("*/*/*"):
            if workspace_dir.is_dir() and not workspace_dir.is_symlink():
                _add_run_root(
                    run_root_for(workspace_dir, "__discovery_probe__").parent
                )

    found: list[dict[str, Any]] = []
    for runs_root in run_roots:
        for run_dir in runs_root.iterdir():
            if limit is not None and len(found) >= max(1, int(limit)):
                return found
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(manifest, dict) or not isinstance(checkpoint, dict):
                continue
            if str(manifest.get("strategy") or "") != "protein_run":
                continue
            control_workdir = str(manifest.get("control_workdir") or "").strip()
            workspace_id = str(manifest.get("workspace_id") or run_dir.name).strip()
            session_id = str(manifest.get("session_id") or "").strip()
            if not control_workdir or not workspace_id or not session_id:
                continue
            link = {
                "schema": "tcode.protein-run-link.v1",
                "workspace_id": workspace_id,
                "run_id": workspace_id,
                "control_workdir": control_workdir,
                "run_root": run_dir.as_posix(),
                "created_for_async_job": str(manifest.get("kind") or "") == "async-mcp",
            }
            tracker_workdir = (
                control_workdir
                if link["created_for_async_job"]
                else run_dir.as_posix()
            )
            jobs = checkpoint.get("external_jobs")
            if not isinstance(jobs, list):
                continue
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                identifier = str(job.get("identifier") or "").strip()
                status = str(job.get("status") or "").strip().lower()
                if not identifier or status in _TERMINAL_STATUSES:
                    continue
                found.append(
                    {
                        "identifier": identifier,
                        "session_id": session_id,
                        "target_server": str(job.get("target_server") or ""),
                        "target_tool": str(job.get("target_tool") or ""),
                        "workdir": tracker_workdir,
                        "protein_run": link,
                    }
                )
                if limit is not None and len(found) >= max(1, int(limit)):
                    return found
    return found


def update_status(
    link: dict[str, Any],
    *,
    identifier: str,
    status: str,
    phase: str,
    target_server: str = "",
    target_tool: str = "",
    result_summary: str = "",
    error_summary: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    workspace = _load_workspace(link)
    if workspace is None:
        return False
    safe_metadata = dict(metadata or {})
    AgentWorkspaceManager.record_external_job(
        workspace,
        identifier=identifier,
        target_server=target_server,
        target_tool=target_tool,
        status=status,
        phase=phase,
        result_summary=result_summary,
        error_summary=error_summary,
        attempt_id=str(safe_metadata.get("attempt_id") or identifier),
        job_id=str(safe_metadata.get("job_id") or ""),
        metadata=safe_metadata,
    )
    return True


def _safe_segment(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", str(value or "")).strip("-.")[:96]
    return normalized or hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _write_collected_result(destination: Path, result_output: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    limit = _env_int("TCODE_PROTEIN_RUN_ASYNC_RESULT_MAX_BYTES", 8 * 1024 * 1024)
    raw = str(result_output or "")
    encoded = raw.encode("utf-8", errors="replace")
    truncated = len(encoded) > limit
    if truncated:
        encoded = encoded[:limit]
        raw = encoded.decode("utf-8", errors="ignore") + "\n...[result truncated]\n"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None and not truncated:
        path = destination / "collected_result.json"
        path.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        path = destination / "collected_result.txt"
        path.write_text(raw, encoding="utf-8")
    return path


def materialize_completed_generic_result(control: Path, output_root: Path, result_output: str) -> Optional[Path]:
    """Reuse the collected-result writer for non-Protein-Run receipts.

    Only complete, successful generic protocol payloads become readable files;
    incomplete previews or failure receipts never qualify as result evidence.
    """
    from ..runtime.sandbox.macos_native import publish_file
    try:
        payload = json.loads(result_output)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get('isError'):
        return None
    if isinstance(payload.get('structuredContent'), dict):
        payload = payload['structuredContent']
    elif isinstance(payload.get('content'), list):
        try:
            payload = json.loads('\n'.join(str(item.get('text') or '') for item in payload['content'] if isinstance(item, dict)))
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    if (payload.get('result_type') != 'generic' or payload.get('validated') is not True
            or payload.get('status') != 'completed'):
        return None
    target_result = payload.get('target_result')
    if not isinstance(target_result, dict) or target_result.get('isError'):
        return None
    control = control.resolve()
    if not output_root.is_absolute():
        output_root = control / output_root
    if not output_root.is_relative_to(control) or output_root == control or '..' in output_root.parts:
        raise ValueError('Collected result output escapes workspace')
    with tempfile.TemporaryDirectory(prefix='tcode-collected-result-') as directory:
        staged = _write_collected_result(Path(directory), json.dumps(payload, ensure_ascii=False))
        if staged.suffix != '.json':
            raise ValueError('Collected result exceeds complete-evidence size limit')
        data = staged.read_bytes()
    if len(data) > _env_int('TCODE_PROTEIN_RUN_ASYNC_RESULT_MAX_BYTES', 8 * 1024 * 1024):
        raise ValueError('Collected result exceeds complete-evidence size limit')
    target = output_root / 'mcp_result.json'
    publish_file(control, target.relative_to(control).as_posix(), data,
                 expected=hashlib.sha256(data).hexdigest() if target.exists() else None)
    return target


def _source_candidates(
    paths: Iterable[Any],
    *,
    roots: Iterable[Any],
) -> list[Path]:
    resolved_roots: list[Path] = []
    for root_value in roots:
        text = str(root_value or "").strip()
        if not text:
            continue
        root = Path(text).expanduser().resolve(strict=False)
        if root not in resolved_roots:
            resolved_roots.append(root)
    candidates: list[Path] = []
    for value in paths:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        variants = [path] if path.is_absolute() else [root / path for root in resolved_roots]
        for variant in variants:
            resolved = variant.resolve(strict=False)
            if resolved_roots and not any(
                resolved != root and _inside(resolved, root)
                for root in resolved_roots
            ):
                continue
            if resolved.exists() and resolved not in candidates:
                candidates.append(resolved)
                break
    return candidates


def _copy_registered_artifacts(
    *,
    workspace: AgentWorkspace,
    destination: Path,
    source_paths: Iterable[Any],
    source_roots: Iterable[Any],
) -> tuple[list[str], list[dict[str, str]]]:
    max_files = _env_int("TCODE_PROTEIN_RUN_ASYNC_ARTIFACT_MAX_FILES", 512)
    max_bytes = _env_int(
        "TCODE_PROTEIN_RUN_ASYNC_ARTIFACT_MAX_BYTES",
        256 * 1024 * 1024,
    )
    copied: list[str] = []
    skipped: list[dict[str, str]] = []
    file_count = 0
    byte_count = 0
    copied_sources: set[Path] = set()
    run_root = Path(workspace.workspace_root).resolve(strict=False)

    def _copy_file(source: Path, relative_hint: Path) -> bool:
        nonlocal file_count, byte_count
        if source.is_symlink() or not source.is_file():
            return False
        source_key = source.resolve(strict=False)
        if source_key in copied_sources:
            return True
        try:
            size = int(source.stat().st_size)
        except OSError:
            return False
        if file_count + 1 > max_files or byte_count + size > max_bytes:
            return False
        relative_parts = [
            _safe_segment(part)
            for part in relative_hint.parts
            if part not in {"", ".", ".."}
        ]
        relative = Path(*relative_parts) if relative_parts else Path(_safe_segment(source.name))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            digest = hashlib.sha256(source.as_posix().encode("utf-8")).hexdigest()[:10]
            target = target.with_name(f"{target.stem}-{digest}{target.suffix}")
        shutil.copy2(source, target, follow_symlinks=False)
        file_count += 1
        byte_count += size
        copied_sources.add(source_key)
        copied.append(target.relative_to(run_root).as_posix())
        return True

    for source in _source_candidates(source_paths, roots=source_roots):
        if _inside(source, run_root):
            continue
        if source.is_file():
            if not _copy_file(source, Path(_safe_segment(source.name))):
                skipped.append({"name": source.name, "reason": "copy_limit_or_unsafe"})
            continue
        if not source.is_dir() or source.is_symlink():
            skipped.append({"name": source.name, "reason": "unsupported_artifact"})
            continue
        copied_from_dir = False
        visited = 0
        for item in source.rglob("*"):
            visited += 1
            if visited > max_files * 4:
                skipped.append({"name": source.name, "reason": "scan_limit"})
                break
            if item.is_symlink() or not item.is_file():
                continue
            if not _copy_file(item, Path(_safe_segment(source.name)) / item.relative_to(source)):
                skipped.append({"name": source.name, "reason": "copy_limit_or_unsafe"})
                break
            copied_from_dir = True
        if not copied_from_dir and not any(row.get("name") == source.name for row in skipped):
            skipped.append({"name": source.name, "reason": "empty_artifact"})
    return copied, skipped


def settle_result(
    link: dict[str, Any],
    *,
    identifier: str,
    status: str,
    target_server: str,
    target_tool: str,
    result_output: str,
    result_summary: str = "",
    error_summary: str = "",
    artifact_paths: Optional[list[Any]] = None,
    source_roots: Optional[list[Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    settlement_out: Optional[dict[str, Any]] = None,
) -> bool:
    """Archive the collected result and settle the linked Run."""

    workspace = _load_workspace(link)
    if workspace is None:
        return False
    safe_metadata = dict(metadata or {})
    attempt_id = str(
        safe_metadata.get("attempt_id")
        or safe_metadata.get("job_id")
        or identifier
    ).strip()
    result_segment = _safe_segment(identifier)
    if attempt_id and attempt_id != identifier:
        result_segment = f"{result_segment}--{_safe_segment(attempt_id)}"
    destination = (
        Path(workspace.outputs_dir)
        / "async_jobs"
        / result_segment
    )
    collected_path = _write_collected_result(destination, result_output)
    materialized = materialize_output_bundle(
        result_output,
        destination / "artifacts" / "bundles",
        automatic=True,
    )
    copied, skipped = _copy_registered_artifacts(
        workspace=workspace,
        destination=destination / "artifacts",
        source_paths=list(artifact_paths or []),
        source_roots=list(source_roots or []),
    )
    AgentWorkspaceManager.inspect(workspace)
    downloaded: list[str] = []
    agent_artifacts: list[str] = []
    agent_output_root = ""
    if materialized:
        run_root = Path(workspace.workspace_root).resolve(strict=False)
        control_root = Path(workspace.control_workdir).resolve(strict=False)
        materialized_root = Path(str(materialized.get("root") or "")).resolve(
            strict=False
        )
        try:
            agent_output_root = materialized_root.relative_to(control_root).as_posix()
        except ValueError:
            agent_output_root = materialized_root.as_posix()
        for item in list(materialized.get("files") or []):
            path = Path(str(item.get("path") or "")).resolve(strict=False)
            try:
                downloaded.append(path.relative_to(run_root).as_posix())
            except ValueError:
                downloaded.append(path.as_posix())
            try:
                agent_artifacts.append(path.relative_to(control_root).as_posix())
            except ValueError:
                agent_artifacts.append(path.as_posix())
        materialized = {
            **materialized,
            "files": [
                {
                    **dict(item),
                    "path": downloaded[index],
                }
                for index, item in enumerate(list(materialized.get("files") or []))
            ],
        }
    merged_metadata = {
        **safe_metadata,
        "collected_result": collected_path.relative_to(
            Path(workspace.workspace_root)
        ).as_posix(),
        "copied_artifacts": copied,
        "downloaded_artifacts": downloaded,
        "skipped_artifacts": skipped,
    }
    if materialized:
        merged_metadata["artifact_materialization"] = materialized
    if settlement_out is not None:
        settlement_out.update(
            {
                "collected_result": merged_metadata["collected_result"],
                "copied_artifacts": list(copied),
                "downloaded_artifacts": list(downloaded),
                "agent_artifacts": list(agent_artifacts),
                "agent_output_root": agent_output_root,
                "artifact_materialization": dict(materialized or {}),
            }
        )
    AgentWorkspaceManager.record_external_job(
        workspace,
        identifier=identifier,
        target_server=target_server,
        target_tool=target_tool,
        status=status if status in _TERMINAL_STATUSES else "completed",
        phase="collected",
        attempt_id=str(safe_metadata.get("attempt_id") or identifier),
        job_id=str(safe_metadata.get("job_id") or ""),
        result_summary=result_summary,
        error_summary=error_summary,
        metadata=merged_metadata,
    )
    return True
