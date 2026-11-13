"""Protein-domain run workspace backend.

Scientific inputs are snapshotted when reasonably small and exposed as
read-only mounts otherwise.  Generated files live in a task-scoped run
directory and are indexed in a reproducibility manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..runtime.resource_import import (
    attachment_kind,
    container_path_for_workspace_relative,
    env_int,
    path_tree_size,
    resolve_resource_source,
)


# Prefer the original stable resource over a delegated agent's temporary
# worktree copy.  This matters for large async inputs that may be mounted
# instead of copied and must outlive the foreground subagent.
_INPUT_PATH_KEYS = ("source_path", "path", "mount_target_path", "container_path", "relative_path")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth >= 6:
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth=depth + 1) for item in list(value)[:500]]
    return str(value)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{id(payload)}.tmp"
    )
    temporary.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_input_name(source: Path) -> str:
    suffix = source.suffix if source.is_file() else ""
    stem = source.stem if source.is_file() else source.name
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "input"
    digest = hashlib.sha256(source.as_posix().encode("utf-8")).hexdigest()[:10]
    return f"{normalized[:80]}-{digest}{suffix}"


def _resource_dicts(value: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 6:
        return
    if isinstance(value, dict):
        if any(str(value.get(key) or "").strip() for key in _INPUT_PATH_KEYS):
            yield value
        for item in value.values():
            yield from _resource_dicts(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _resource_dicts(item, depth=depth + 1)


def _resolve_item_source(item: dict[str, Any], control_workdir: Path) -> Optional[Path]:
    for key in _INPUT_PATH_KEYS:
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        try:
            candidate = resolve_resource_source(raw, control_workdir)
        except Exception:
            continue
        if candidate.is_file() or candidate.is_dir():
            return candidate
    return None


def _input_copy_limits() -> tuple[int, int]:
    return (
        max(1, env_int("TCODE_PROTEIN_RUN_INPUT_COPY_MAX_BYTES", 64 * 1024 * 1024)),
        max(1, env_int("TCODE_PROTEIN_RUN_INPUT_COPY_MAX_FILES", 256)),
    )


def _make_writable(path: Path) -> None:
    try:
        path.chmod(0o755 if path.is_dir() else 0o644)
    except Exception:
        pass


def make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    _make_writable(root)
    try:
        for path in root.rglob("*"):
            _make_writable(path)
    except Exception:
        pass


def _protect_input_tree(root: Path) -> None:
    if not root.exists():
        return
    try:
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
    except Exception:
        pass


def _snapshot_or_mount(
    source: Path,
    inputs_dir: Path,
    *,
    durability_required: bool = False,
) -> dict[str, Any]:
    max_bytes, max_files = _input_copy_limits()
    durable_max_bytes = max_bytes
    durable_max_files = max_files
    if durability_required:
        durable_max_bytes = max(
            max_bytes,
            env_int(
                "TCODE_PROTEIN_RUN_DURABLE_INPUT_COPY_MAX_BYTES",
                2 * 1024 * 1024 * 1024,
            ),
        )
        durable_max_files = max(
            max_files,
            env_int("TCODE_PROTEIN_RUN_DURABLE_INPUT_COPY_MAX_FILES", 4096),
        )
    file_count, total_bytes = path_tree_size(
        source,
        max_files=(durable_max_files if durability_required else max_files) + 1,
    )
    if durability_required:
        if file_count > durable_max_files or total_bytes > durable_max_bytes:
            raise ValueError(
                "temporary async input exceeds durable snapshot limits: "
                f"{source} ({file_count} files, {total_bytes} bytes)"
            )
    should_copy = (
        durability_required
        or (source.is_file() and total_bytes <= max_bytes)
    ) or (
        source.is_dir() and file_count <= max_files and total_bytes <= max_bytes
    )
    name = _safe_input_name(source)
    target = (inputs_dir / name).resolve(strict=False)
    relative_path = f"inputs/{name}"
    container_path = container_path_for_workspace_relative(relative_path)

    if should_copy:
        if target.exists():
            make_tree_writable(target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        mode = "snapshot"
        host_path = target
        mounted = False
    else:
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        mode = "mount"
        host_path = source
        mounted = True

    if source.is_file():
        sha256 = _sha256_file(target if should_copy else source)
    elif should_copy:
        sha256 = _sha256_tree(target)
    else:
        sha256 = ""
    try:
        source_mtime_ns = int(source.stat().st_mtime_ns)
    except OSError:
        source_mtime_ns = 0
    return {
        "name": source.name,
        "kind": attachment_kind(source),
        "source_path": source.as_posix(),
        "path": host_path.as_posix(),
        "run_path": target.as_posix(),
        "relative_path": relative_path,
        "container_path": container_path,
        "mount_target_path": target.as_posix(),
        "mode": mode,
        "import_mode": mode,
        "snapshot": mode == "snapshot",
        "mounted": mounted,
        "read_only": True,
        "available_in_sandbox": True,
        "file_count": file_count,
        "size": total_bytes,
        "sha256": sha256,
        "source_mtime_ns": source_mtime_ns,
        "durability_required": bool(durability_required),
    }


def _resource_patch(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": record["path"],
        "source_path": record["source_path"],
        "relative_path": record["relative_path"],
        "container_path": record["container_path"],
        "mount_target_path": record["mount_target_path"],
        "available_in_sandbox": True,
        "imported": bool(record["snapshot"]),
        "mounted": bool(record["mounted"]),
        "read_only": True,
        "import_mode": record["mode"],
        "workspace_input": True,
        "sha256": record["sha256"],
        "size": record["size"],
    }


def _rewrite_resource_value(
    value: Any,
    *,
    control_workdir: Path,
    records_by_source: dict[str, dict[str, Any]],
    depth: int = 0,
) -> Any:
    if depth > 7:
        return copy.deepcopy(value)
    if isinstance(value, dict):
        mapped = {
            key: _rewrite_resource_value(
                item,
                control_workdir=control_workdir,
                records_by_source=records_by_source,
                depth=depth + 1,
            )
            for key, item in value.items()
        }
        source = _resolve_item_source(value, control_workdir)
        if source is not None:
            record = records_by_source.get(source.resolve(strict=False).as_posix())
            if record is not None:
                mapped.update(_resource_patch(record))
        return mapped
    if isinstance(value, list):
        return [
            _rewrite_resource_value(
                item,
                control_workdir=control_workdir,
                records_by_source=records_by_source,
                depth=depth + 1,
            )
            for item in value
        ]
    return copy.deepcopy(value)


def prepare_resource_context(
    *,
    control_workdir: Path,
    run_root: Path,
    resource_context: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    original = dict(resource_context or {}) if isinstance(resource_context, dict) else {}
    inputs_dir = run_root / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    make_tree_writable(inputs_dir)

    records_by_source: dict[str, dict[str, Any]] = {}
    for item in _resource_dicts(original):
        source = _resolve_item_source(item, control_workdir)
        if source is None:
            continue
        resolved = source.resolve(strict=False)
        try:
            resolved.relative_to(run_root.resolve(strict=False))
            continue
        except ValueError:
            pass
        key = resolved.as_posix()
        durability_required = bool(item.get("durability_required"))
        existing = records_by_source.get(key)
        if existing is None or (
            durability_required and str(existing.get("mode") or "") != "snapshot"
        ):
            records_by_source[key] = _snapshot_or_mount(
                resolved,
                inputs_dir,
                durability_required=durability_required,
            )

    records = list(records_by_source.values())
    mapped = _rewrite_resource_value(
        original,
        control_workdir=control_workdir,
        records_by_source=records_by_source,
    )
    existing_mounts = [
        item
        for item in list(mapped.get("external_mounts") or [])
        if isinstance(item, dict) and not bool(item.get("workspace_input"))
    ]
    mapped["external_mounts"] = [
        *existing_mounts,
        {
            "name": "protein-run-inputs",
            "kind": "directory",
            "source_path": inputs_dir.as_posix(),
            "mount_target_path": inputs_dir.as_posix(),
            "relative_path": "inputs",
            "container_path": container_path_for_workspace_relative("inputs"),
            "read_only": True,
            "workspace_input": True,
            "workspace_input_root": True,
        },
        *[
            {
                "name": record["name"],
                "kind": record["kind"],
                "source_path": record["source_path"],
                "mount_target_path": record["mount_target_path"],
                "relative_path": record["relative_path"],
                "container_path": record["container_path"],
                "read_only": True,
                "workspace_input": True,
            }
            for record in records
            if record["mounted"]
        ],
    ]
    mapped["_workspace"] = {
        "strategy": "protein_run",
        "run_root": run_root.as_posix(),
        "inputs_dir": inputs_dir.as_posix(),
        "outputs_dir": (run_root / "outputs").as_posix(),
        "logs_dir": (run_root / "logs").as_posix(),
        "manifest_path": (run_root / "manifest.json").as_posix(),
        "read_only_paths": [inputs_dir.as_posix()],
    }
    _protect_input_tree(inputs_dir)
    return mapped, records


def run_root_for(control_workdir: Path, workspace_id: str) -> Path:
    configured = str(os.environ.get("TCODE_PROTEIN_RUNS_ROOT", "") or "").strip()
    base = (
        Path(configured).expanduser()
        if configured
        else control_workdir / ".tcode" / "runs" / "protein"
    )
    if not base.is_absolute():
        base = control_workdir / base
    return (base.resolve(strict=False) / workspace_id).resolve(strict=False)


def initialize_run(
    *,
    workspace: Any,
    resource_context: Optional[dict[str, Any]],
) -> None:
    root = Path(workspace.workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("inputs", "outputs", "logs", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    mapped, inputs = prepare_resource_context(
        control_workdir=Path(workspace.control_workdir),
        run_root=root,
        resource_context=resource_context,
    )
    workspace.execution_workdir = root.as_posix()
    workspace.inputs_dir = (root / "inputs").as_posix()
    workspace.outputs_dir = (root / "outputs").as_posix()
    workspace.logs_dir = (root / "logs").as_posix()
    workspace.manifest_path = (root / "manifest.json").as_posix()
    workspace.resource_context = mapped
    workspace.resource_context["_workspace"]["workspace_id"] = workspace.workspace_id
    workspace.input_resources = inputs
    workspace.checkpoint_state = {
        "schema": "tcode.protein-run-checkpoint.v1",
        "status": "active",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "resume_count": 0,
        "pending_action": "",
        "completed_tools": [],
        "outputs": [],
    }
    write_manifest(workspace)
    write_checkpoint(workspace, status="active")


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _validate_input_record(record: dict[str, Any]) -> Optional[str]:
    mode = str(record.get("mode") or record.get("import_mode") or "snapshot")
    raw_path = str(
        record.get("path")
        if mode == "snapshot"
        else record.get("source_path") or record.get("path")
    ).strip()
    if not raw_path:
        return "input path is missing"
    path = Path(raw_path).expanduser().resolve(strict=False)
    if not path.exists():
        return f"input no longer exists: {path}"
    expected_sha = str(record.get("sha256") or "").strip()
    if path.is_file() and expected_sha:
        try:
            actual_sha = _sha256_file(path)
        except OSError as exc:
            return f"input cannot be hashed: {path}: {exc}"
        if actual_sha != expected_sha:
            return f"input checksum drift: {path}"
    if path.is_dir():
        try:
            expected_files = int(record.get("file_count") or 0)
            expected_size = int(record.get("size") or 0)
            actual_files, actual_size = path_tree_size(
                path,
                max_files=max(1, expected_files + 1),
            )
        except (OSError, TypeError, ValueError) as exc:
            return f"input directory cannot be inspected: {path}: {exc}"
        if expected_files and actual_files != expected_files:
            return f"input file-count drift: {path}"
        if expected_size and actual_size != expected_size:
            return f"input size drift: {path}"
        if expected_sha:
            try:
                actual_sha = _sha256_tree(path)
            except OSError as exc:
                return f"input directory cannot be hashed: {path}: {exc}"
            if actual_sha != expected_sha:
                return f"input checksum drift: {path}"
    return None


def validate_run_inputs(workspace: Any) -> list[str]:
    errors = []
    for record in list(getattr(workspace, "input_resources", []) or []):
        if not isinstance(record, dict):
            continue
        error = _validate_input_record(record)
        if error:
            errors.append(error)
    return errors


def resume_run(workspace: Any) -> None:
    root = Path(workspace.workspace_root).expanduser().resolve(strict=True)
    errors = validate_run_inputs(workspace)
    if errors:
        raise RuntimeError("; ".join(errors[:8]))
    workspace.execution_workdir = root.as_posix()
    workspace.inputs_dir = (root / "inputs").as_posix()
    workspace.outputs_dir = (root / "outputs").as_posix()
    workspace.logs_dir = (root / "logs").as_posix()
    workspace.manifest_path = (root / "manifest.json").as_posix()
    marker = workspace.resource_context.get("_workspace")
    if isinstance(marker, dict):
        marker["workspace_id"] = workspace.workspace_id
        marker["run_root"] = root.as_posix()
        marker["inputs_dir"] = workspace.inputs_dir
        marker["outputs_dir"] = workspace.outputs_dir
        marker["logs_dir"] = workspace.logs_dir
        marker["manifest_path"] = workspace.manifest_path
    workspace.resume_count = max(0, int(getattr(workspace, "resume_count", 0) or 0)) + 1
    workspace.checkpoint_state = _load_json_dict(root / "checkpoint.json")
    workspace.checkpoint_state["status"] = "resumed"
    workspace.checkpoint_state["resumed_at"] = _utc_now()
    workspace.checkpoint_state["resume_count"] = workspace.resume_count
    workspace.provenance = dict(
        _load_json_dict(root / "manifest.json").get("provenance") or workspace.provenance or {}
    )
    _protect_input_tree(root / "inputs")
    inspect_run(workspace)
    write_checkpoint(
        workspace,
        status="resumed",
        pending_action=str(workspace.checkpoint_state.get("pending_action") or ""),
    )


def _generated_files(workspace: Any) -> list[dict[str, Any]]:
    root = Path(workspace.workspace_root)
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if (
            relative in {"manifest.json", "candidates.json", "checkpoint.json"}
            or relative.startswith("inputs/")
            or relative.startswith("tmp/")
        ):
            continue
        try:
            size = int(path.stat().st_size)
            sha256 = _sha256_file(path)
        except OSError:
            continue
        records.append(
            {
                "path": path.as_posix(),
                "relative_path": relative,
                "container_path": container_path_for_workspace_relative(relative),
                "kind": attachment_kind(path),
                "size": size,
                "sha256": sha256,
            }
        )
    return sorted(records, key=lambda item: item["relative_path"])


def inspect_run(workspace: Any) -> tuple[bool, list[str], list[dict[str, Any]]]:
    from .protein_candidates import candidate_manifest_summary, sync_candidate_index

    outputs = _generated_files(workspace)
    candidate_index = sync_candidate_index(
        workspace.workspace_root,
        input_resources=list(workspace.input_resources or []),
    )
    workspace.candidate_registry = candidate_manifest_summary(
        workspace.workspace_root,
        candidate_index,
    )
    changed = [str(item["relative_path"]) for item in outputs]
    return bool(changed), changed, outputs


def write_manifest(workspace: Any, *, outputs: Optional[list[dict[str, Any]]] = None) -> None:
    from .protein_candidates import candidate_manifest_summary

    path = Path(workspace.manifest_path or Path(workspace.workspace_root) / "manifest.json")
    biosafety = {}
    resources = workspace.resource_context if isinstance(workspace.resource_context, dict) else {}
    marker = resources.get("_biosafety")
    if isinstance(marker, dict):
        biosafety = dict(marker)
    contract = workspace.task_contract if isinstance(workspace.task_contract, dict) else {}
    payload = {
        "schema_version": 1,
        "workspace_id": workspace.workspace_id,
        "strategy": "protein_run",
        "domain": workspace.domain or "protein",
        "task_id": workspace.task_id,
        "session_id": workspace.session_id,
        "kind": workspace.kind,
        "agent_name": workspace.agent_name,
        "worker_name": workspace.worker_name,
        "status": workspace.status,
        "outcome": workspace.outcome,
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
        "finalized_at": workspace.finalized_at,
        "control_workdir": workspace.control_workdir,
        "run_root": workspace.workspace_root,
        "directories": {
            "inputs": workspace.inputs_dir,
            "outputs": workspace.outputs_dir,
            "logs": workspace.logs_dir,
        },
        "contract": {
            key: contract.get(key)
            for key in ("goal", "deliverable", "stop_when", "allowed_tools")
            if key in contract
        },
        "biosafety": biosafety,
        "inputs": list(workspace.input_resources or []),
        "outputs": list(outputs or []),
        "candidate_registry": candidate_manifest_summary(workspace.workspace_root),
        "provenance": dict(getattr(workspace, "provenance", {}) or {}),
        "checkpoint": dict(getattr(workspace, "checkpoint_state", {}) or {}),
        "resume_count": max(0, int(getattr(workspace, "resume_count", 0) or 0)),
    }
    _atomic_json(path, payload)


def _safe_tool_provenance(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in list(tool_trace or [])[-500:]:
        if not isinstance(item, dict):
            continue
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        serialized_args = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        record = {
            "tool_name": str(item.get("tool_name") or ""),
            "call_id": str(item.get("call_id") or ""),
            "attempt": item.get("attempt"),
            "state": str(item.get("state") or ""),
            "arguments": _redact_sensitive(_jsonable(arguments)),
            "arguments_sha256": hashlib.sha256(serialized_args.encode("utf-8")).hexdigest(),
            "metadata": _redact_sensitive(_jsonable(item.get("metadata") or {})),
            "error": str(item.get("error_preview") or "")[:500],
        }
        records.append(record)
    return records


def _redact_sensitive(value: Any) -> Any:
    sensitive = re.compile(
        r"(?i)(?:api[_-]?key|authorization|cookie|password|secret|token)"
    )
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if sensitive.search(str(key))
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def record_provenance(
    workspace: Any,
    *,
    agent: Any = None,
    tool_trace: Optional[list[dict[str, Any]]] = None,
    attempt: int = 0,
    outcome: str = "",
) -> None:
    provenance = dict(getattr(workspace, "provenance", {}) or {})
    provenance.setdefault(
        "runtime",
        {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "tcode_version": "0.1.0",
        },
    )
    attempts = [
        dict(item)
        for item in list(provenance.get("attempts") or [])
        if isinstance(item, dict)
    ]
    if agent is not None:
        provider = getattr(agent, "provider", None)
        agent_info = getattr(agent, "agent_info", None)
        provider_name = str(getattr(provider, "name", "unknown") or "unknown")
        model = ""
        for attr in ("model", "_model"):
            value = getattr(provider, attr, None)
            if isinstance(value, str) and value.strip():
                model = value.strip()
                break
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for message in list(getattr(agent, "messages", []) or []):
            message_usage = getattr(message, "usage", None)
            for key in usage:
                try:
                    usage[key] += int(getattr(message_usage, key, 0) or 0)
                except (TypeError, ValueError):
                    pass
        invocation_id = f"{getattr(agent, 'session_id', workspace.session_id)}:{max(1, int(attempt or 1))}"
        attempt_record = {
            "invocation_id": invocation_id,
            "attempt": max(1, int(attempt or 1)),
            "agent_name": str(getattr(agent_info, "name", workspace.agent_name) or ""),
            "provider": provider_name,
            "model": model,
            "temperature": getattr(agent_info, "temperature", None),
            "top_p": getattr(agent_info, "top_p", None),
            "reasoning_effort": str(getattr(agent, "_reasoning_effort", "") or ""),
            "execution_profile": str(getattr(agent, "_execution_profile", "") or ""),
            "usage": usage,
            "message_count": len(list(getattr(agent, "messages", []) or [])),
            "outcome": str(outcome or ""),
            "recorded_at": _utc_now(),
        }
        attempts = [
            item for item in attempts if item.get("invocation_id") != invocation_id
        ]
        attempts.append(attempt_record)
    if tool_trace is not None:
        existing_tools = [
            dict(item)
            for item in list(provenance.get("tool_calls") or [])
            if isinstance(item, dict)
        ]
        incoming_tools = _safe_tool_provenance(tool_trace)
        merged_tools: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for index, item in enumerate([*existing_tools, *incoming_tools]):
            key = str(item.get("call_id") or "").strip()
            if not key:
                key = "|".join(
                    [
                        str(item.get("tool_name") or ""),
                        str(item.get("arguments_sha256") or ""),
                        str(index),
                    ]
                )
            if key not in merged_tools:
                order.append(key)
                merged_tools[key] = dict(item)
            else:
                previous = merged_tools[key]
                merged_tools[key] = {
                    **previous,
                    **{
                        name: value
                        for name, value in item.items()
                        if value not in (None, "", {}, [])
                    },
                }
        provenance["tool_calls"] = [merged_tools[key] for key in order][-500:]
        provenance["tool_call_count"] = len(provenance["tool_calls"])
        provenance["tools_used"] = sorted(
            {
                str(item.get("tool_name") or "")
                for item in provenance["tool_calls"]
                if str(item.get("tool_name") or "")
            }
        )
    provenance["attempts"] = attempts[-100:]
    provenance["updated_at"] = _utc_now()
    workspace.provenance = provenance
    write_manifest(workspace, outputs=list(getattr(workspace, "output_artifacts", []) or []))


def hydrate_persisted_state(workspace: Any) -> None:
    """Merge checkpoint/manifest state written by an async watcher.

    Delegated Task/Team code holds an in-memory workspace object while the
    asynchronous submission layer persists updates through another object.
    Hydration prevents the older object from overwriting external-job state
    during final settlement.
    """

    root = Path(workspace.workspace_root)
    checkpoint = _load_json_dict(root / "checkpoint.json")
    manifest = _load_json_dict(root / "manifest.json")
    if checkpoint:
        workspace.checkpoint_state = checkpoint
    provenance = manifest.get("provenance") if isinstance(manifest, dict) else None
    if isinstance(provenance, dict) and provenance:
        workspace.provenance = dict(provenance)
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
    if isinstance(outputs, list):
        workspace.output_artifacts = [
            dict(item) for item in outputs if isinstance(item, dict)
        ]


def external_jobs(workspace: Any) -> list[dict[str, Any]]:
    checkpoint = getattr(workspace, "checkpoint_state", {})
    if not isinstance(checkpoint, dict):
        return []
    records: list[dict[str, Any]] = []
    for item in list(checkpoint.get("external_jobs") or []):
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("identifier") or "").strip()
        if not identifier:
            continue
        record = dict(item)
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        record["attempt_id"] = str(
            record.get("attempt_id")
            or record.get("job_id")
            or identifier
        ).strip()
        record["issue_identifier"] = str(
            record.get("issue_identifier") or identifier
        ).strip()
        record["job_id"] = str(
            record.get("job_id") or metadata.get("job_id") or ""
        ).strip()
        records.append(record)
    return records


def _current_external_jobs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return attempts that have not been replaced by a retry.

    Historical failed attempts stay in the manifest for provenance, but must
    not force a successfully retried Run back into a failed outcome.
    """

    current = [
        dict(item)
        for item in records
        if not str(item.get("superseded_by") or "").strip()
    ]
    return current or ([dict(records[-1])] if records else [])


def has_pending_external_jobs(workspace: Any) -> bool:
    terminal = {"completed", "failed", "cancelled"}
    return any(
        str(item.get("status") or "").strip().lower() not in terminal
        for item in _current_external_jobs(external_jobs(workspace))
    )


def external_jobs_outcome(workspace: Any) -> str:
    jobs = _current_external_jobs(external_jobs(workspace))
    if not jobs or has_pending_external_jobs(workspace):
        return ""
    statuses = {
        str(item.get("status") or "").strip().lower()
        for item in jobs
    }
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    return "completed"


def _merge_external_job_record(
    records: list[dict[str, Any]],
    incoming: dict[str, Any],
) -> list[dict[str, Any]]:
    attempt_id = str(
        incoming.get("attempt_id")
        or incoming.get("job_id")
        or incoming.get("identifier")
        or ""
    ).strip()
    merged: list[dict[str, Any]] = []
    found = False
    for item in records:
        item_attempt_id = str(
            item.get("attempt_id")
            or item.get("job_id")
            or item.get("identifier")
            or ""
        ).strip()
        if item_attempt_id != attempt_id:
            merged.append(dict(item))
            continue
        found = True
        merged.append(
            {
                **dict(item),
                **{
                    key: value
                    for key, value in incoming.items()
                    if value not in (None, "", {}, [])
                },
            }
        )
    if not found:
        merged.append(dict(incoming))
    return merged[-500:]


def record_external_job(
    workspace: Any,
    *,
    identifier: str,
    target_server: str,
    target_tool: str,
    status: str,
    phase: str,
    attempt_id: str = "",
    job_id: str = "",
    supersedes_attempt_id: str = "",
    arguments: Optional[dict[str, Any]] = None,
    result_summary: str = "",
    error_summary: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Record one routed MCP job in Run provenance and checkpoint state."""

    hydrate_persisted_state(workspace)
    now = _utc_now()
    normalized_status = str(status or "waiting_external").strip().lower()
    aliases = {
        "todo": "waiting_external",
        "queued": "waiting_external",
        "open": "waiting_external",
        "pending": "waiting_external",
        "in progress": "running",
        "in_progress": "running",
        "doing": "running",
        "done": "completed",
        "closed": "completed",
        "canceled": "cancelled",
        "error": "failed",
    }
    normalized_status = aliases.get(normalized_status, normalized_status)
    if normalized_status not in {
        "waiting_external",
        "running",
        "collecting",
        "completed",
        "failed",
        "cancelled",
    }:
        normalized_status = "waiting_external"

    checkpoint = dict(getattr(workspace, "checkpoint_state", {}) or {})
    jobs = external_jobs(workspace)
    resolved_attempt_id = str(attempt_id or identifier or job_id).strip()
    if supersedes_attempt_id:
        jobs = [
            {
                **dict(item),
                **(
                    {
                        "superseded_by": resolved_attempt_id,
                        "superseded_at": now,
                    }
                    if str(item.get("attempt_id") or "").strip()
                    == str(supersedes_attempt_id).strip()
                    else {}
                ),
            }
            for item in jobs
        ]
    previous = next(
        (
            item
            for item in jobs
            if str(item.get("attempt_id") or "").strip() == resolved_attempt_id
        ),
        {},
    )
    safe_metadata = _redact_sensitive(_jsonable(dict(metadata or {})))
    resolved_job_id = str(
        job_id
        or previous.get("job_id")
        or (safe_metadata.get("job_id") if isinstance(safe_metadata, dict) else "")
        or ""
    ).strip()
    job_record = {
        **dict(previous),
        "attempt_id": resolved_attempt_id,
        "identifier": str(identifier or ""),
        "issue_identifier": str(identifier or previous.get("issue_identifier") or ""),
        "job_id": resolved_job_id,
        "target_server": str(target_server or previous.get("target_server") or ""),
        "target_tool": str(target_tool or previous.get("target_tool") or ""),
        "status": normalized_status,
        "phase": str(phase or normalized_status),
        "submitted_at": str(previous.get("submitted_at") or now),
        "updated_at": now,
        "result_summary": str(result_summary or previous.get("result_summary") or "")[:4000],
        "error_summary": str(error_summary or previous.get("error_summary") or "")[:4000],
        "metadata": {
            **(
                dict(previous.get("metadata") or {})
                if isinstance(previous.get("metadata"), dict)
                else {}
            ),
            **(safe_metadata if isinstance(safe_metadata, dict) else {}),
        },
    }
    if normalized_status in {"completed", "failed", "cancelled"}:
        job_record["finished_at"] = now
    jobs = _merge_external_job_record(jobs, job_record)
    checkpoint["external_jobs"] = jobs
    checkpoint["pending_action"] = (
        "Wait for external protein computation to finish."
        if any(
            str(item.get("status") or "").strip().lower()
            not in {"completed", "failed", "cancelled"}
            for item in jobs
        )
        else ""
    )
    workspace.checkpoint_state = checkpoint

    provenance = dict(getattr(workspace, "provenance", {}) or {})
    provenance_jobs = [
        dict(item)
        for item in list(provenance.get("external_jobs") or [])
        if isinstance(item, dict)
    ]
    if supersedes_attempt_id:
        provenance_jobs = [
            {
                **dict(item),
                **(
                    {
                        "superseded_by": resolved_attempt_id,
                        "superseded_at": now,
                    }
                    if str(
                        item.get("attempt_id")
                        or item.get("job_id")
                        or item.get("identifier")
                        or ""
                    ).strip()
                    == str(supersedes_attempt_id).strip()
                    else {}
                ),
            }
            for item in provenance_jobs
        ]
    provenance["external_jobs"] = _merge_external_job_record(
        provenance_jobs,
        job_record,
    )
    existing_tool_calls = [
        dict(item)
        for item in list(provenance.get("tool_calls") or [])
        if isinstance(item, dict)
    ]
    tool_name = "mcp_{}_{}".format(
        str(job_record.get("target_server") or ""),
        str(job_record.get("target_tool") or ""),
    ).strip("_")
    trace_record = {
        "tool_name": tool_name,
        "call_id": f"async:{resolved_attempt_id}",
        "state": (
            "completed"
            if normalized_status == "completed"
            else "error"
            if normalized_status in {"failed", "cancelled"}
            else normalized_status
        ),
        "arguments": dict(arguments or {}),
        "metadata": {
            "external_job": True,
            "attempt_id": resolved_attempt_id,
            "job_id": resolved_job_id,
            "identifier": str(identifier or ""),
            "target_server": str(job_record.get("target_server") or ""),
            "target_tool": str(job_record.get("target_tool") or ""),
            "status": normalized_status,
            "phase": str(phase or normalized_status),
            **(safe_metadata if isinstance(safe_metadata, dict) else {}),
        },
        "error_preview": str(error_summary or "")[:500],
    }
    safe_trace = _safe_tool_provenance([trace_record])[0]
    previous_tool = next(
        (
            item
            for item in existing_tool_calls
            if str(item.get("call_id") or "") == f"async:{resolved_attempt_id}"
        ),
        {},
    )
    if not arguments and previous_tool:
        safe_trace["arguments"] = previous_tool.get("arguments", {})
        safe_trace["arguments_sha256"] = previous_tool.get("arguments_sha256", "")
    merged_tools = [
        item
        for item in existing_tool_calls
        if str(item.get("call_id") or "") != f"async:{resolved_attempt_id}"
    ]
    merged_tools.append({**previous_tool, **safe_trace})
    provenance["tool_calls"] = merged_tools[-500:]
    provenance["tool_call_count"] = len(provenance["tool_calls"])
    provenance["tools_used"] = sorted(
        {
            str(item.get("tool_name") or "")
            for item in provenance["tool_calls"]
            if str(item.get("tool_name") or "")
        }
    )
    provenance["updated_at"] = now
    workspace.provenance = provenance

    if has_pending_external_jobs(workspace):
        workspace.status = "waiting_external"
        workspace.outcome = ""
        workspace.finalized_at = ""
        workspace.retained_reason = "external_job_pending"
    else:
        outcome = external_jobs_outcome(workspace) or "completed"
        workspace.status = outcome
        workspace.outcome = outcome
        workspace.finalized_at = now
        workspace.retained_reason = f"external_job_{outcome}"
    workspace.updated_at = now
    write_checkpoint(
        workspace,
        status=workspace.status,
        pending_action=str(checkpoint.get("pending_action") or ""),
    )
    write_manifest(
        workspace,
        outputs=list(getattr(workspace, "output_artifacts", []) or []),
    )


def begin_run_action(
    workspace: Any,
    *,
    action: str,
    attempt_id: str,
    idempotency_key: str = "",
    reason: str = "",
) -> tuple[dict[str, Any], bool]:
    """Persist an action intent before an external side effect is attempted."""

    now = _utc_now()
    normalized_key = str(idempotency_key or "").strip()[:200]
    checkpoint = dict(getattr(workspace, "checkpoint_state", {}) or {})
    actions = [
        dict(item)
        for item in list(checkpoint.get("actions") or [])
        if isinstance(item, dict)
    ]
    if normalized_key:
        existing = next(
            (
                item
                for item in actions
                if str(item.get("idempotency_key") or "") == normalized_key
                and str(item.get("action") or "") == str(action or "")
            ),
            None,
        )
        if existing is not None:
            return dict(existing), False
    busy = next(
        (
            item
            for item in actions
            if str(item.get("action") or "") == str(action or "")
            and str(item.get("attempt_id") or "") == str(attempt_id or "")
            and str(item.get("status") or "") in {"requested", "submitting"}
        ),
        None,
    )
    if busy is not None:
        return dict(busy), False
    action_id = f"run-action-{uuid.uuid4().hex}"
    record = {
        "action_id": action_id,
        "action": str(action or ""),
        "attempt_id": str(attempt_id or ""),
        "idempotency_key": normalized_key,
        "status": "submitting",
        "reason": str(reason or "")[:1000],
        "created_at": now,
        "updated_at": now,
    }
    actions.append(record)
    checkpoint["actions"] = actions[-200:]
    workspace.checkpoint_state = checkpoint
    write_checkpoint(
        workspace,
        status=str(getattr(workspace, "status", "") or "active"),
        pending_action=str(checkpoint.get("pending_action") or ""),
    )
    return dict(record), True


def finish_run_action(
    workspace: Any,
    *,
    action_id: str,
    status: str,
    result: Optional[dict[str, Any]] = None,
    error: str = "",
) -> dict[str, Any]:
    checkpoint = dict(getattr(workspace, "checkpoint_state", {}) or {})
    actions = [
        dict(item)
        for item in list(checkpoint.get("actions") or [])
        if isinstance(item, dict)
    ]
    updated: dict[str, Any] = {}
    now = _utc_now()
    for index, item in enumerate(actions):
        if str(item.get("action_id") or "") != str(action_id or ""):
            continue
        updated = {
            **item,
            "status": str(status or "failed"),
            "updated_at": now,
            "result": _redact_sensitive(_jsonable(dict(result or {}))),
            "error": str(error or "")[:2000],
        }
        actions[index] = updated
        break
    checkpoint["actions"] = actions[-200:]
    workspace.checkpoint_state = checkpoint
    write_checkpoint(
        workspace,
        status=str(getattr(workspace, "status", "") or "active"),
        pending_action=str(checkpoint.get("pending_action") or ""),
    )
    return dict(updated)


def record_review_provenance(
    workspace: Any,
    *,
    review: dict[str, Any],
    review_attempt: int,
) -> None:
    provenance = dict(getattr(workspace, "provenance", {}) or {})
    reviews = [
        dict(item)
        for item in list(provenance.get("reviews") or [])
        if isinstance(item, dict)
    ]
    models = [
        dict(item)
        for item in list(review.get("_provenance") or [])
        if isinstance(item, dict) and item
    ]
    record = {
        "review_attempt": max(1, int(review_attempt or 1)),
        "verdict": str(review.get("verdict") or ""),
        "review_level": str(review.get("review_level") or ""),
        "models": models,
        "recorded_at": _utc_now(),
    }
    reviews = [
        item
        for item in reviews
        if int(item.get("review_attempt") or 0) != record["review_attempt"]
    ]
    reviews.append(record)
    provenance["reviews"] = reviews[-100:]
    provenance["updated_at"] = _utc_now()
    workspace.provenance = provenance
    write_manifest(workspace, outputs=list(getattr(workspace, "output_artifacts", []) or []))


def write_checkpoint(
    workspace: Any,
    *,
    status: str,
    tool_trace: Optional[list[dict[str, Any]]] = None,
    pending_action: str = "",
) -> None:
    root = Path(workspace.workspace_root)
    existing = dict(getattr(workspace, "checkpoint_state", {}) or {})
    tools = _safe_tool_provenance(list(tool_trace or [])) if tool_trace is not None else list(
        existing.get("completed_tools") or []
    )
    payload = {
        **existing,
        "schema": "tcode.protein-run-checkpoint.v1",
        "workspace_id": workspace.workspace_id,
        "task_id": workspace.task_id,
        "session_id": workspace.session_id,
        "status": str(status or "paused"),
        "updated_at": _utc_now(),
        "resume_count": max(0, int(getattr(workspace, "resume_count", 0) or 0)),
        "pending_action": str(pending_action or existing.get("pending_action") or ""),
        "completed_tools": tools,
        "outputs": list(getattr(workspace, "output_artifacts", []) or []),
        "candidate_registry": dict(getattr(workspace, "candidate_registry", {}) or {}),
        "input_validation": {
            "valid": not bool(validate_run_inputs(workspace)),
            "checked_at": _utc_now(),
        },
    }
    workspace.checkpoint_state = payload
    _atomic_json(root / "checkpoint.json", payload)
    write_manifest(workspace, outputs=list(getattr(workspace, "output_artifacts", []) or []))


def execution_prompt_hint(workspace: Any) -> str:
    if str(getattr(workspace, "strategy", "")) != "protein_run":
        return ""
    lines = [
        "# Protein Run Workspace",
        f"- Run root: {workspace.execution_workdir}",
        f"- Inputs are read-only: {workspace.inputs_dir}",
        f"- Write generated artifacts under: {workspace.outputs_dir}",
        f"- Write execution logs under: {workspace.logs_dir}",
        f"- Reproducibility manifest: {workspace.manifest_path}",
        "- Prefer the remapped /workspace/inputs/... paths listed in Session Resources.",
        "- Never overwrite source uploads or files in inputs/.",
        "- Use protein_candidate to register metrics, compare candidates, and create evidence-gated promotions.",
    ]
    if workspace.input_resources:
        lines.append("Available inputs:")
        for item in list(workspace.input_resources)[:16]:
            lines.append(
                f"- {item.get('container_path')} ({item.get('kind')}, {item.get('mode')}, read-only)"
            )
    checkpoint = dict(getattr(workspace, "checkpoint_state", {}) or {})
    if int(getattr(workspace, "resume_count", 0) or 0) > 0:
        lines.extend(
            [
                f"- This run resumed from checkpoint (resume_count={workspace.resume_count}).",
                f"- Pending action: {checkpoint.get('pending_action') or 'continue from preserved outputs and tool history'}",
                "- Inspect existing outputs before running any step again.",
            ]
        )
    return "\n".join(lines)
