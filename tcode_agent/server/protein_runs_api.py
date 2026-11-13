"""Owner-scoped, read-only API for protein run workspaces."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..workspace import AgentWorkspaceManager
from ..workspace.protein_run import (
    run_root_for,
    validate_run_inputs,
)
from . import workdirs as workdir_utils
from .auth import auth_required, current_user_from_request
from .state import ServerState


router = APIRouter(prefix="/api/protein-runs", tags=["protein-runs"])

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token)",
    re.IGNORECASE,
)
_MAX_JSON_BYTES = 8 * 1024 * 1024
_SAFE_USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}
_ACTIVE_ATTEMPT_STATUSES = {"waiting_external", "queued", "pending", "running", "collecting"}
_RETRYABLE_ATTEMPT_STATUSES = {"failed", "cancelled", "canceled"}
_DELETABLE_RUN_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "blocked",
    "retained",
    "paused",
    "awaiting_input",
}
_UNSAFE_INLINE_SUFFIXES = {".html", ".htm", ".svg"}
_UNSAFE_INLINE_MEDIA_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}


class ProteinRunActionRequest(BaseModel):
    idempotency_key: str = ""
    reason: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _stale_after_seconds() -> int:
    try:
        return max(30, min(86400, int(os.environ.get("TCODE_ASYNC_STALE_SECONDS", "300") or "300")))
    except (TypeError, ValueError):
        return 300


def _watcher_health() -> dict[str, Any]:
    from ..runtime.async_watcher import health_snapshot

    return health_snapshot()


def _workspace_context(request: Request) -> workdir_utils.WorkspaceContext:
    user = current_user_from_request(request) if auth_required() else None
    return workdir_utils.resolve_workspace_context(
        headers=None if user is not None else request.headers,
        query_params=request.query_params,
        tenant_id=user.tenant_id if user is not None else "",
        user_id=user.user_id if user is not None else "",
    )


def _workspace_path(context: workdir_utils.WorkspaceContext) -> Path:
    return workdir_utils.resolve_user_workdir_path(
        ServerState.get_workdir(),
        context.workspace_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    ).resolve(strict=False)


def _runs_root(workspace_path: Path) -> Path:
    # run_root_for also honours TCODE_PROTEIN_RUNS_ROOT.  A configured shared
    # root is safe because every manifest is filtered by control_workdir below.
    return run_root_for(workspace_path, "__run_probe__").parent


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return str(value)[:500]
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if (
                    str(key).lower() not in _SAFE_USAGE_KEYS
                    and _SENSITIVE_KEY_PATTERN.search(str(key))
                )
                else _redact(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:500]
        }
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:1000]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:2000]


def _same_path(left: Any, right: Path) -> bool:
    try:
        return Path(str(left or "")).expanduser().resolve(strict=False) == right
    except (OSError, RuntimeError, ValueError):
        return False


def _manifest_belongs_to_workspace(
    manifest: dict[str, Any],
    *,
    run_dir: Path,
    runs_root: Path,
    workspace_path: Path,
) -> bool:
    if str(manifest.get("strategy") or "") != "protein_run":
        return False
    control_workdir = str(manifest.get("control_workdir") or "").strip()
    if control_workdir:
        return _same_path(control_workdir, workspace_path)
    try:
        run_dir.relative_to(runs_root)
        runs_root.relative_to(workspace_path)
        return True
    except ValueError:
        return False


def _safe_run_dir(
    run_id: str,
    *,
    runs_root: Path,
    workspace_path: Path,
) -> tuple[Path, dict[str, Any]]:
    normalized = str(run_id or "").strip()
    if not _RUN_ID_PATTERN.fullmatch(normalized) or normalized in {".", ".."}:
        raise HTTPException(404, "protein run not found")
    candidate = runs_root / normalized
    if candidate.is_symlink():
        raise HTTPException(404, "protein run not found")
    target = candidate.resolve(strict=False)
    try:
        target.relative_to(runs_root.resolve(strict=False))
    except ValueError as exc:
        raise HTTPException(404, "protein run not found") from exc
    if not target.is_dir() or target.is_symlink():
        raise HTTPException(404, "protein run not found")
    manifest = _read_json_object(target / "manifest.json")
    if not manifest or not _manifest_belongs_to_workspace(
        manifest,
        run_dir=target,
        runs_root=runs_root,
        workspace_path=workspace_path,
    ):
        raise HTTPException(404, "protein run not found")
    return target, manifest


def _lifecycle_status(manifest: dict[str, Any], metadata: Any = None) -> str:
    outcome = str(manifest.get("outcome") or getattr(metadata, "outcome", "") or "").strip().lower()
    if outcome in {"completed", "failed", "cancelled", "blocked"}:
        return outcome
    status = str(manifest.get("status") or getattr(metadata, "status", "") or "unknown").strip().lower()
    return status or "unknown"


def _summary(
    manifest: dict[str, Any],
    *,
    run_id: str,
    workspace_path: Path,
) -> dict[str, Any]:
    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    tool_calls = [
        item for item in list(provenance.get("tool_calls") or []) if isinstance(item, dict)
    ]
    contract = manifest.get("contract") if isinstance(manifest.get("contract"), dict) else {}
    inputs = [item for item in list(manifest.get("inputs") or []) if isinstance(item, dict)]
    outputs = [item for item in list(manifest.get("outputs") or []) if isinstance(item, dict)]
    metadata = AgentWorkspaceManager.load(
        control_workdir=workspace_path,
        workspace_id=run_id,
    )
    checkpoint = (
        dict(getattr(metadata, "checkpoint_state", {}) or {})
        if metadata is not None
        else {}
    )
    attempts = _attempt_records(manifest, checkpoint)
    active_attempt = _active_attempt(attempts)
    goal = str(contract.get("goal") or "").strip()
    title = goal.splitlines()[0][:180] if goal else ""
    if not title:
        title = str(manifest.get("task_id") or manifest.get("agent_name") or run_id).strip()[:180]
    delete_policy = _delete_policy(
        lifecycle_status=_lifecycle_status(manifest, metadata),
        attempts=attempts,
    )
    return {
        "run_id": run_id,
        "title": title,
        "task_id": str(manifest.get("task_id") or ""),
        "session_id": str(manifest.get("session_id") or ""),
        "kind": str(manifest.get("kind") or ""),
        "domain": str(manifest.get("domain") or "protein"),
        "agent_name": str(manifest.get("agent_name") or ""),
        "worker_name": str(manifest.get("worker_name") or ""),
        "status": str(manifest.get("status") or getattr(metadata, "status", "") or "unknown"),
        "lifecycle_status": _lifecycle_status(manifest, metadata),
        "outcome": str(manifest.get("outcome") or getattr(metadata, "outcome", "") or ""),
        "created_at": str(manifest.get("created_at") or ""),
        "updated_at": str(manifest.get("updated_at") or ""),
        "finalized_at": str(manifest.get("finalized_at") or ""),
        "input_count": len(inputs),
        "output_count": len(outputs),
        "tool_call_count": int(provenance.get("tool_call_count") or len(tool_calls)),
        "resume_count": max(0, int(manifest.get("resume_count") or 0)),
        "retained_reason": str(getattr(metadata, "retained_reason", "") or ""),
        "error": str(getattr(metadata, "error", "") or "")[:500],
        "sync_status": str(active_attempt.get("sync_status") or ""),
        "stale": bool(active_attempt.get("stale", False)),
        "deletable": bool(delete_policy["available"]),
        "delete_block_reason": str(delete_policy["reason"]),
    }


def _input_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _redact(item.get(key))
        for key in (
            "name",
            "kind",
            "relative_path",
            "container_path",
            "mode",
            "import_mode",
            "snapshot",
            "mounted",
            "read_only",
            "file_count",
            "size",
            "sha256",
        )
        if key in item
    }


def _safe_relative_path(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip().lstrip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _is_unsafe_inline_file(filename: str, media_type: str) -> bool:
    suffix = Path(str(filename or "")).suffix.lower()
    content_type = str(media_type or "").split(";", 1)[0].strip().lower()
    return suffix in _UNSAFE_INLINE_SUFFIXES or content_type in _UNSAFE_INLINE_MEDIA_TYPES


def _output_view(item: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    relative_path = _safe_relative_path(item.get("relative_path"))
    result = {
        key: _redact(item.get(key))
        for key in ("kind", "relative_path", "container_path", "size", "sha256")
        if key in item and key != "relative_path"
    }
    result["relative_path"] = relative_path
    if relative_path:
        encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(relative_path).parts)
        result["artifact_url"] = f"/api/protein-runs/{quote(run_id, safe='')}/artifacts/{encoded_path}"
    return result


def _recovery_view(
    manifest: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    summary: dict[str, Any],
) -> dict[str, Any]:
    validation = checkpoint.get("input_validation") if isinstance(checkpoint.get("input_validation"), dict) else {}
    input_valid = validation.get("valid") if isinstance(validation.get("valid"), bool) else None
    lifecycle = str(summary.get("lifecycle_status") or "unknown")
    retained_reason = str(summary.get("retained_reason") or "")
    has_checkpoint = bool(checkpoint)
    if input_valid is False or retained_reason == "input_drift":
        state = "blocked_input_drift"
        can_resume = False
    elif lifecycle == "completed":
        state = "not_needed"
        can_resume = False
    elif lifecycle in {"active", "resumed", "waiting_external", "running", "collecting"}:
        state = "in_progress_or_interrupted"
        can_resume = False
    elif has_checkpoint and lifecycle in {"paused", "awaiting_input", "failed", "cancelled", "blocked", "retained"}:
        state = "checkpoint_available"
        can_resume = True
    elif has_checkpoint:
        state = "checkpoint_present"
        can_resume = False
    else:
        state = "unavailable"
        can_resume = False
    return {
        "state": state,
        "can_resume": can_resume,
        "requires_revalidation": bool(can_resume),
        "checkpoint_status": str(checkpoint.get("status") or ""),
        "checkpoint_created_at": str(checkpoint.get("created_at") or ""),
        "checkpoint_updated_at": str(checkpoint.get("updated_at") or ""),
        "last_input_validation": {
            "valid": input_valid,
            "checked_at": str(validation.get("checked_at") or ""),
        },
        "pending_action": (
            "" if state == "not_needed" else str(checkpoint.get("pending_action") or "")
        ),
        "resume_count": max(0, int(checkpoint.get("resume_count") or summary.get("resume_count") or 0)),
        "completed_step_count": max(
            len(
                [item for item in list(checkpoint.get("completed_tools") or []) if isinstance(item, dict)]
            ),
            int(summary.get("tool_call_count") or 0) if lifecycle == "completed" else 0,
        ),
    }


def _attempt_records(
    manifest: dict[str, Any],
    checkpoint: dict[str, Any],
) -> list[dict[str, Any]]:
    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    raw_records = checkpoint.get("external_jobs")
    if not isinstance(raw_records, list) or not raw_records:
        raw_records = provenance.get("external_jobs")
    records: list[dict[str, Any]] = []
    for raw in list(raw_records or []):
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("issue_identifier") or raw.get("identifier") or "").strip()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        job_id = str(raw.get("job_id") or metadata.get("job_id") or "").strip()
        attempt_id = str(raw.get("attempt_id") or identifier or job_id).strip()
        if not attempt_id or not identifier:
            continue
        records.append(
            {
                "attempt_id": attempt_id,
                "issue_identifier": identifier,
                "job_id": job_id,
                "target_server": str(raw.get("target_server") or ""),
                "target_tool": str(raw.get("target_tool") or ""),
                "status": str(raw.get("status") or "unknown").strip().lower(),
                "phase": str(raw.get("phase") or ""),
                "submitted_at": str(raw.get("submitted_at") or ""),
                "updated_at": str(raw.get("updated_at") or ""),
                "finished_at": str(raw.get("finished_at") or ""),
                # Detail responses need a complete watcher summary so the UI can
                # parse structured JSON and keep the raw payload collapsible.
                # Watcher persistence already caps these fields at 4,000 chars.
                "result_summary": str(raw.get("result_summary") or "")[:4000],
                "error_summary": str(raw.get("error_summary") or "")[:4000],
                "superseded_by": str(raw.get("superseded_by") or ""),
                "superseded_at": str(raw.get("superseded_at") or ""),
                "metadata": {
                    key: _redact(metadata.get(key))
                    for key in (
                        "execution_channel",
                        "issue_state",
                        "delivery_status",
                        "action",
                        "source_attempt_id",
                        "sync_status",
                        "sync_retry_count",
                        "sync_last_error_at",
                        "sync_next_retry_at",
                        "last_heartbeat_at",
                    )
                    if key in metadata
                },
            }
        )
    records.sort(key=lambda item: str(item.get("submitted_at") or item.get("updated_at") or ""))
    now = datetime.now(timezone.utc).timestamp()
    stale_after = _stale_after_seconds()
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        status = str(record.get("status") or "").strip().lower()
        updated_at = str(
            metadata.get("last_heartbeat_at")
            or record.get("updated_at")
            or record.get("submitted_at")
            or ""
        )
        age = max(0, int(now - _timestamp(updated_at))) if _timestamp(updated_at) > 0 else 0
        record["sync_status"] = str(
            metadata.get("sync_status")
            or ("sync_failed" if str(record.get("phase") or "").lower() == "sync_failed" else "")
        )
        record["sync_retry_count"] = max(0, int(metadata.get("sync_retry_count") or 0))
        record["sync_last_error_at"] = str(metadata.get("sync_last_error_at") or "")
        record["sync_next_retry_at"] = str(metadata.get("sync_next_retry_at") or "")
        record["stale_seconds"] = age
        record["last_heartbeat_at"] = str(metadata.get("last_heartbeat_at") or "")
        record["stale"] = bool(record["last_heartbeat_at"]) and status in _ACTIVE_ATTEMPT_STATUSES and age >= stale_after
    return records


def _active_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    current = [item for item in attempts if not str(item.get("superseded_by") or "").strip()]
    candidates = current or attempts
    if not candidates:
        return {}
    return dict(
        sorted(
            candidates,
            key=lambda item: str(item.get("updated_at") or item.get("submitted_at") or ""),
        )[-1]
    )


def _delete_policy(
    *,
    lifecycle_status: str,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    lifecycle = str(lifecycle_status or "unknown").strip().lower()
    active = _active_attempt(attempts)
    attempt_status = str(active.get("status") or "").strip().lower()
    sync_status = str(active.get("sync_status") or "").strip().lower()
    if attempt_status in _ACTIVE_ATTEMPT_STATUSES and sync_status != "sync_failed":
        return {
            "available": False,
            "reason": "当前 Run 仍有关联计算在运行，请先取消并等待任务进入终态。",
        }
    if lifecycle not in _DELETABLE_RUN_STATUSES and sync_status != "sync_failed":
        return {
            "available": False,
            "reason": f"当前 Run 状态为 {lifecycle or 'unknown'}，暂不允许删除。",
        }
    return {"available": True, "reason": ""}


def _run_action_policy(
    attempts: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    active = _active_attempt(attempts)
    status = str(active.get("status") or "unknown").strip().lower()
    phase = str(active.get("phase") or "").strip().lower()
    input_validation = checkpoint.get("input_validation") if isinstance(checkpoint.get("input_validation"), dict) else {}
    input_valid = input_validation.get("valid")

    cancel_reason = ""
    if not active:
        cancel_reason = "当前 Run 没有关联的异步计算任务。"
    elif phase == "cancel_requested":
        cancel_reason = "取消请求已经发送，正在等待执行端确认。"
    elif status not in _ACTIVE_ATTEMPT_STATUSES:
        cancel_reason = f"当前 attempt 状态是 {status or 'unknown'}，无需取消。"
    elif not str(active.get("issue_identifier") or "").strip():
        cancel_reason = "当前 attempt 缺少 Symphony identifier，无法安全取消。"

    retry_reason = ""
    if not active:
        retry_reason = "当前 Run 没有关联的异步计算任务。"
    elif str(active.get("superseded_by") or "").strip():
        retry_reason = "该 attempt 已被后续重试替代。"
    elif status not in _RETRYABLE_ATTEMPT_STATUSES:
        retry_reason = f"当前 attempt 状态是 {status or 'unknown'}，只有失败或取消的任务才能重试。"
    elif not str(active.get("job_id") or "").strip():
        retry_reason = "当前 attempt 尚未记录真实 job_id，无法按原参数重试。"
    elif input_valid is False:
        retry_reason = "Run 输入已经变化，禁止直接重试。"

    return {
        "schema_version": "protein_run.action_policy/v1",
        "attempt_id": str(active.get("attempt_id") or ""),
        "cancel": {"available": not bool(cancel_reason), "reason": cancel_reason},
        "retry": {
            "available": not bool(retry_reason),
            "reason": retry_reason,
            "requires_revalidation": not bool(retry_reason),
            "requires_biosafety_gate": not bool(retry_reason),
        },
        "resume": {
            "available": False,
            "reason": "本批次只开放外部计算重试；Agent Run 检查点恢复仍保持只读。",
        },
    }


def _attempt_arguments(
    manifest: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    attempt_id = str(attempt.get("attempt_id") or "").strip()
    identifier = str(attempt.get("issue_identifier") or "").strip()
    for call in list(provenance.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
        call_attempt = str(metadata.get("attempt_id") or metadata.get("identifier") or "").strip()
        if call_attempt not in {attempt_id, identifier}:
            continue
        arguments = call.get("arguments")
        return dict(arguments) if isinstance(arguments, dict) else {}
    return {}


def _find_attempt(
    attempts: list[dict[str, Any]],
    attempt_id: str,
) -> dict[str, Any]:
    normalized = str(attempt_id or "").strip()
    match = next(
        (item for item in attempts if str(item.get("attempt_id") or "") == normalized),
        None,
    )
    if match is None:
        raise HTTPException(404, "protein run attempt not found")
    return dict(match)


@router.get("")
async def list_protein_runs(
    request: Request,
    status: str = Query(""),
    session_id: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
    q: str = "",
):
    context = _workspace_context(request)
    workspace_path = _workspace_path(context)
    runs_root = _runs_root(workspace_path)
    normalized_status = str(status or "").strip().lower()
    normalized_session = str(session_id or "").strip()
    tokens = str(q or "")[:200].casefold().split()
    rows: list[dict[str, Any]] = []
    if runs_root.is_dir():
        for entry in runs_root.iterdir():
            if not entry.is_dir() or entry.is_symlink() or not _RUN_ID_PATTERN.fullmatch(entry.name):
                continue
            manifest = _read_json_object(entry / "manifest.json")
            if manifest.get("kind") == "side_chat":
                continue
            if not manifest or not _manifest_belongs_to_workspace(
                manifest,
                run_dir=entry.resolve(strict=False),
                runs_root=runs_root,
                workspace_path=workspace_path,
            ):
                continue
            row = _summary(manifest, run_id=entry.name, workspace_path=workspace_path)
            searchable = " ".join(str(row.get(key) or "") for key in
                                  ("run_id", "title", "kind", "status", "session_id", "agent_name")).casefold()
            if tokens and not all(token in searchable for token in tokens):
                continue
            if normalized_status and normalized_status not in {
                str(row.get("status") or "").lower(),
                str(row.get("lifecycle_status") or "").lower(),
            }:
                continue
            if normalized_session and str(row.get("session_id") or "") != normalized_session:
                continue
            rows.append(row)
    rows.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return {
        "workspace_id": context.workspace_id,
        "count": min(len(rows), limit),
        "total": len(rows),
        "runs": rows[:limit],
        "generated_at": _utc_now(),
        "watcher": _watcher_health(),
    }


@router.get("/{run_id}")
async def get_protein_run(run_id: str, request: Request):
    context = _workspace_context(request)
    workspace_path = _workspace_path(context)
    runs_root = _runs_root(workspace_path)
    run_dir, manifest = _safe_run_dir(
        run_id,
        runs_root=runs_root,
        workspace_path=workspace_path,
    )
    checkpoint = _read_json_object(run_dir / "checkpoint.json")
    summary = _summary(manifest, run_id=run_dir.name, workspace_path=workspace_path)
    provenance = manifest.get("provenance") if isinstance(manifest.get("provenance"), dict) else {}
    attempts = _attempt_records(manifest, checkpoint)
    tool_calls = [
        _redact(item)
        for item in list(provenance.get("tool_calls") or checkpoint.get("completed_tools") or [])
        if isinstance(item, dict)
    ]
    return {
        "run": summary,
        "contract": _redact(manifest.get("contract") or {}),
        "biosafety": _redact(manifest.get("biosafety") or {}),
        "inputs": [
            _input_view(item)
            for item in list(manifest.get("inputs") or [])
            if isinstance(item, dict)
        ],
        "outputs": [
            _output_view(item, run_id=run_dir.name)
            for item in list(manifest.get("outputs") or [])
            if isinstance(item, dict) and _safe_relative_path(item.get("relative_path"))
        ],
        "tool_calls": tool_calls,
        "model_attempts": [
            _redact(item)
            for item in list(provenance.get("attempts") or [])
            if isinstance(item, dict)
        ],
        "reviews": [
            _redact(item)
            for item in list(provenance.get("reviews") or [])
            if isinstance(item, dict)
        ],
        "runtime": _redact(provenance.get("runtime") or {}),
        "recovery": _recovery_view(manifest, checkpoint, summary=summary),
        "attempts": attempts,
        "active_attempt": _active_attempt(attempts),
        "action_policy": _run_action_policy(attempts, checkpoint),
        "watcher": _watcher_health(),
    }


def _workspace_for_action(
    run_id: str,
    request: Request,
) -> tuple[Any, Path, dict[str, Any], dict[str, Any]]:
    context = _workspace_context(request)
    workspace_path = _workspace_path(context)
    run_dir, manifest = _safe_run_dir(
        run_id,
        runs_root=_runs_root(workspace_path),
        workspace_path=workspace_path,
    )
    workspace = AgentWorkspaceManager.load(
        control_workdir=workspace_path,
        workspace_id=run_dir.name,
    )
    if workspace is None or workspace.strategy != "protein_run":
        raise HTTPException(409, "protein run workspace metadata is unavailable")
    checkpoint = _read_json_object(run_dir / "checkpoint.json")
    return workspace, run_dir, manifest, checkpoint


def _dismiss_deleted_run_tracking(
    *,
    workspace: Any,
    attempts: list[dict[str, Any]],
    deleted_at: str,
) -> None:
    identifiers = {
        str(attempt.get("issue_identifier") or "").strip()
        for attempt in attempts
        if str(attempt.get("issue_identifier") or "").strip()
    }
    if not identifiers:
        return

    from ..runtime.async_watcher import unregister
    from ..runtime.shared_tracker import SharedTracker

    workdirs = []
    for candidate in (workspace.control_workdir, ServerState.get_workdir()):
        normalized = str(candidate or "").strip()
        if normalized and normalized not in workdirs:
            workdirs.append(normalized)
    tombstone = {
        "workspace_id": workspace.workspace_id,
        "run_id": workspace.workspace_id,
        "deleted_at": deleted_at,
    }
    watcher_state = {
        "sync_status": "dismissed",
        "run_settled": True,
        "run_deleted_at": deleted_at,
        "updated_at": deleted_at,
    }
    for identifier in identifiers:
        for tracker_workdir in workdirs:
            existing = SharedTracker.get_task(
                identifier=identifier,
                workdir=tracker_workdir,
            )
            if existing is None:
                continue
            updated = SharedTracker.patch_task(
                identifier=identifier,
                workspace_patch={
                    "protein_run_deleted": tombstone,
                    "async_watcher": watcher_state,
                },
                metadata_patch={
                    "protein_run_deleted": tombstone,
                    "async_watcher": watcher_state,
                },
                event_kind="protein_run_deleted",
                source="protein_runs_api",
                workdir=tracker_workdir,
            )
            if updated is None:
                raise RuntimeError(
                    f"failed to dismiss tracker link for {identifier}"
                )
        unregister(identifier)


@router.delete("/{run_id}")
async def delete_protein_run(run_id: str, request: Request):
    workspace, _, manifest, checkpoint = _workspace_for_action(run_id, request)
    attempts = _attempt_records(manifest, checkpoint)
    summary = _summary(
        manifest,
        run_id=workspace.workspace_id,
        workspace_path=Path(workspace.control_workdir),
    )
    policy = _delete_policy(
        lifecycle_status=str(summary.get("lifecycle_status") or ""),
        attempts=attempts,
    )
    if not policy["available"]:
        raise HTTPException(409, str(policy["reason"]))

    deleted_at = _utc_now()
    try:
        _dismiss_deleted_run_tracking(
            workspace=workspace,
            attempts=attempts,
            deleted_at=deleted_at,
        )
        cleanup = AgentWorkspaceManager.cleanup(workspace, force=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"删除 Protein Run 失败：{str(exc)[:500]}") from exc
    if not cleanup.removed:
        raise HTTPException(
            500,
            f"删除 Protein Run 失败：{cleanup.reason or 'workspace cleanup failed'}",
        )
    return {
        "status": "deleted",
        "run_id": workspace.workspace_id,
        "deleted_at": deleted_at,
        "removed_output_count": len(
            [
                item
                for item in list(manifest.get("outputs") or [])
                if isinstance(item, dict)
            ]
        ),
        "removed_attempt_count": len(attempts),
    }


def _replay_action_response(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status") or "").strip()
    if status == "succeeded" and isinstance(record.get("result"), dict):
        return {
            "status": "ok",
            "idempotent_replay": True,
            **dict(record.get("result") or {}),
        }
    if status in {"requested", "submitting"}:
        raise HTTPException(409, "相同 Run attempt 已有操作正在执行。")
    raise HTTPException(
        409,
        str(record.get("error") or "相同幂等键对应的操作此前未成功。"),
    )


def _existing_action(
    checkpoint: dict[str, Any],
    *,
    action: str,
    idempotency_key: str,
) -> dict[str, Any]:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        return {}
    for item in list(checkpoint.get("actions") or []):
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("action") or "") == str(action or "")
            and str(item.get("idempotency_key") or "") == normalized
        ):
            return dict(item)
    return {}


def _retry_gate(
    *,
    workspace: Any,
    manifest: dict[str, Any],
    attempt: dict[str, Any],
    action_id: str,
) -> None:
    from ..governance.biosafety import evaluate_tool_call
    from ..tools.base import ToolContext

    arguments = _attempt_arguments(manifest, attempt)
    nested = arguments.get("input_config") if isinstance(arguments.get("input_config"), dict) else None
    input_config: dict[str, Any] = dict(nested or arguments)
    input_config.update(
        {
            "target_server": str(attempt.get("target_server") or ""),
            "target_tool": str(attempt.get("target_tool") or ""),
        }
    )
    resources = dict(getattr(workspace, "resource_context", {}) or {})
    biosafety = manifest.get("biosafety") if isinstance(manifest.get("biosafety"), dict) else {}
    if biosafety:
        resources["_biosafety"] = dict(biosafety)
    context = ToolContext(
        session_id=str(manifest.get("session_id") or ""),
        message_id=action_id,
        call_id=action_id,
        agent=str(manifest.get("agent_name") or "protein_orchestrator"),
        abort=None,
        messages=[],
        extra={
            "workdir": workspace.control_workdir,
            "control_workdir": workspace.control_workdir,
            "resource_context": resources,
        },
    )
    decision = evaluate_tool_call(
        tool_name="mcp_job-protocol-mcp_submit_job",
        arguments={"input_config": input_config},
        context=context,
    )
    if not decision.allowed:
        raise HTTPException(
            403,
            f"生物安全 Gate 未通过，未提交重试: {decision.reason or decision.action}",
        )


@router.post("/{run_id}/attempts/{attempt_id}/retry")
async def retry_protein_run_attempt(
    run_id: str,
    attempt_id: str,
    payload: ProteinRunActionRequest,
    request: Request,
):
    workspace, _, manifest, checkpoint = _workspace_for_action(run_id, request)
    previous_action = _existing_action(
        checkpoint,
        action="retry",
        idempotency_key=payload.idempotency_key,
    )
    if previous_action:
        return _replay_action_response(previous_action)
    attempts = _attempt_records(manifest, checkpoint)
    attempt = _find_attempt(attempts, attempt_id)
    active = _active_attempt(attempts)
    if str(active.get("attempt_id") or "") != str(attempt.get("attempt_id") or ""):
        raise HTTPException(409, "只能重试当前有效 attempt。")
    policy = _run_action_policy(attempts, checkpoint).get("retry") or {}
    if not bool(policy.get("available")):
        raise HTTPException(409, str(policy.get("reason") or "当前 attempt 不可重试。"))

    action_record, created = AgentWorkspaceManager.begin_run_action(
        workspace,
        action="retry",
        attempt_id=str(attempt.get("attempt_id") or ""),
        idempotency_key=str(payload.idempotency_key or ""),
        reason=str(payload.reason or ""),
    )
    if not created:
        return _replay_action_response(action_record)
    action_id = str(action_record.get("action_id") or "")
    submission: dict[str, Any] = {}
    try:
        input_errors = validate_run_inputs(workspace)
        if input_errors:
            raise HTTPException(
                409,
                "Run 输入校验失败，未提交重试: " + "; ".join(input_errors[:8]),
            )
        _retry_gate(
            workspace=workspace,
            manifest=manifest,
            attempt=attempt,
            action_id=action_id,
        )

        # Import lazily because _shared owns the FastAPI app that includes this router.
        from ._shared import _submit_job_action

        submission = await _submit_job_action(
            job_id=str(attempt.get("job_id") or ""),
            action="retry",
            reuse_issue=False,
            reason=str(payload.reason or "").strip(),
            expected_protein_run_id=workspace.workspace_id,
            expected_control_workdir=workspace.control_workdir,
        )
        new_job_id = str(submission.get("job_id") or "").strip()
        new_issue = submission.get("issue") if isinstance(submission.get("issue"), dict) else {}
        new_identifier = str(new_issue.get("identifier") or "").strip()
        if not new_job_id or not new_identifier:
            raise RuntimeError("重试已提交，但没有返回可追踪的 job_id 或 issue identifier")

        source_arguments = _attempt_arguments(manifest, attempt)
        AgentWorkspaceManager.record_external_job(
            workspace,
            identifier=new_identifier,
            attempt_id=new_identifier,
            job_id=new_job_id,
            supersedes_attempt_id=str(attempt.get("attempt_id") or ""),
            target_server=str(attempt.get("target_server") or ""),
            target_tool=str(attempt.get("target_tool") or ""),
            status="waiting_external",
            phase="retry_submitted",
            arguments=source_arguments,
            metadata={
                "action": "retry",
                "source_attempt_id": str(attempt.get("attempt_id") or ""),
                "job_id": new_job_id,
            },
        )

        run_link = {
            "schema": "tcode.protein-run-link.v1",
            "workspace_id": workspace.workspace_id,
            "run_id": workspace.workspace_id,
            "control_workdir": workspace.control_workdir,
            "run_root": workspace.workspace_root,
            "created_for_async_job": str(workspace.kind or "") == "async-mcp",
        }
        try:
            from ..runtime.async_watcher import register, start_watcher

            register(
                new_identifier,
                str(workspace.session_id or manifest.get("session_id") or ""),
                job_id=new_job_id,
                target_server=str(attempt.get("target_server") or ""),
                target_tool=str(attempt.get("target_tool") or ""),
                workdir=workspace.control_workdir,
                protein_run=run_link,
            )
            await start_watcher(workdir=workspace.control_workdir)
        except Exception:
            # The persisted Run link lets restart recovery register the watcher.
            pass

        result = {
            "run_id": workspace.workspace_id,
            "action": "retry",
            "source_attempt_id": str(attempt.get("attempt_id") or ""),
            "attempt": {
                "attempt_id": new_identifier,
                "issue_identifier": new_identifier,
                "job_id": new_job_id,
                "status": "waiting_external",
                "phase": "retry_submitted",
            },
        }
        AgentWorkspaceManager.finish_run_action(
            workspace,
            action_id=action_id,
            status="succeeded",
            result=result,
        )
        return {"status": "ok", "idempotent_replay": False, **result}
    except Exception as exc:
        partial = {
            "job_id": str(submission.get("job_id") or ""),
            "source_attempt_id": str(attempt.get("attempt_id") or ""),
        }
        AgentWorkspaceManager.finish_run_action(
            workspace,
            action_id=action_id,
            status="failed",
            result=partial,
            error=str(getattr(exc, "detail", "") or exc),
        )
        raise


@router.post("/{run_id}/attempts/{attempt_id}/cancel")
async def cancel_protein_run_attempt(
    run_id: str,
    attempt_id: str,
    payload: ProteinRunActionRequest,
    request: Request,
):
    workspace, _, manifest, checkpoint = _workspace_for_action(run_id, request)
    previous_action = _existing_action(
        checkpoint,
        action="cancel",
        idempotency_key=payload.idempotency_key,
    )
    if previous_action:
        return _replay_action_response(previous_action)
    attempts = _attempt_records(manifest, checkpoint)
    attempt = _find_attempt(attempts, attempt_id)
    active = _active_attempt(attempts)
    if str(active.get("attempt_id") or "") != str(attempt.get("attempt_id") or ""):
        raise HTTPException(409, "只能取消当前有效 attempt。")
    policy = _run_action_policy(attempts, checkpoint).get("cancel") or {}
    if not bool(policy.get("available")):
        raise HTTPException(409, str(policy.get("reason") or "当前 attempt 不可取消。"))

    action_record, created = AgentWorkspaceManager.begin_run_action(
        workspace,
        action="cancel",
        attempt_id=str(attempt.get("attempt_id") or ""),
        idempotency_key=str(payload.idempotency_key or ""),
        reason=str(payload.reason or ""),
    )
    if not created:
        return _replay_action_response(action_record)
    action_id = str(action_record.get("action_id") or "")
    try:
        from ._shared import _cancel_symphony_issue_by_identifier

        cancellation = await _cancel_symphony_issue_by_identifier(
            str(attempt.get("issue_identifier") or ""),
            session_id=str(workspace.session_id or manifest.get("session_id") or ""),
            expected_protein_run_id=workspace.workspace_id,
            expected_control_workdir=workspace.control_workdir,
        )
        AgentWorkspaceManager.record_external_job(
            workspace,
            identifier=str(attempt.get("issue_identifier") or ""),
            attempt_id=str(attempt.get("attempt_id") or ""),
            job_id=str(attempt.get("job_id") or ""),
            target_server=str(attempt.get("target_server") or ""),
            target_tool=str(attempt.get("target_tool") or ""),
            status=str(attempt.get("status") or "waiting_external"),
            phase="cancel_requested",
            metadata={
                "action": "cancel",
                "job_id": str(attempt.get("job_id") or ""),
            },
        )
        result = {
            "run_id": workspace.workspace_id,
            "action": "cancel",
            "attempt_id": str(attempt.get("attempt_id") or ""),
            "cancel_status": "requested",
            "cancellation": _redact(cancellation.get("cancellation") or {}),
        }
        AgentWorkspaceManager.finish_run_action(
            workspace,
            action_id=action_id,
            status="succeeded",
            result=result,
        )
        return {"status": "ok", "idempotent_replay": False, **result}
    except Exception as exc:
        AgentWorkspaceManager.finish_run_action(
            workspace,
            action_id=action_id,
            status="failed",
            error=str(getattr(exc, "detail", "") or exc),
        )
        raise


@router.post("/{run_id}/attempts/{attempt_id}/reconcile")
async def reconcile_protein_run_attempt(
    run_id: str,
    attempt_id: str,
    request: Request,
):
    workspace, _, manifest, checkpoint = _workspace_for_action(run_id, request)
    attempts = _attempt_records(manifest, checkpoint)
    attempt = _find_attempt(attempts, attempt_id)
    active = _active_attempt(attempts)
    if str(active.get("attempt_id") or "") != str(attempt.get("attempt_id") or ""):
        raise HTTPException(409, "只能重新同步当前有效 attempt。")
    identifier = str(attempt.get("issue_identifier") or "").strip()
    if not identifier:
        raise HTTPException(409, "当前 attempt 缺少 Symphony identifier，无法重新同步。")

    from ..runtime.async_watcher import reconcile_job, start_watcher

    run_link = {
        "schema": "tcode.protein-run-link.v1",
        "workspace_id": workspace.workspace_id,
        "run_id": workspace.workspace_id,
        "control_workdir": workspace.control_workdir,
        "run_root": workspace.workspace_root,
    }
    result = await reconcile_job(
        identifier,
        session_id=str(workspace.session_id or manifest.get("session_id") or ""),
        job_id=str(attempt.get("job_id") or ""),
        target_server=str(attempt.get("target_server") or ""),
        target_tool=str(attempt.get("target_tool") or ""),
        workdir=workspace.control_workdir,
        protein_run=run_link,
    )
    await start_watcher(workdir=workspace.control_workdir)
    if result.get("status") == "not_found":
        raise HTTPException(409, str(result.get("error") or "Tracker 中未找到对应任务。"))
    return {
        "run_id": workspace.workspace_id,
        "attempt_id": str(attempt.get("attempt_id") or ""),
        "action": "reconcile",
        **result,
    }


@router.get("/{run_id}/artifacts/{artifact_path:path}")
async def get_protein_run_artifact(run_id: str, artifact_path: str, request: Request):
    context = _workspace_context(request)
    workspace_path = _workspace_path(context)
    runs_root = _runs_root(workspace_path)
    run_dir, manifest = _safe_run_dir(
        run_id,
        runs_root=runs_root,
        workspace_path=workspace_path,
    )
    relative_path = _safe_relative_path(artifact_path)
    allowed_paths = {
        _safe_relative_path(item.get("relative_path"))
        for item in list(manifest.get("outputs") or [])
        if isinstance(item, dict)
    }
    if not relative_path or relative_path not in allowed_paths:
        raise HTTPException(404, "protein run artifact not found")
    candidate = run_dir / relative_path
    if candidate.is_symlink():
        raise HTTPException(404, "protein run artifact not found")
    target = candidate.resolve(strict=False)
    try:
        target.relative_to(run_dir)
    except ValueError as exc:
        raise HTTPException(404, "protein run artifact not found") from exc
    if not target.is_file() or target.is_symlink():
        raise HTTPException(404, "protein run artifact not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    safe_inline = not _is_unsafe_inline_file(target.name, media_type)
    response = FileResponse(
        path=target,
        filename=target.name,
        media_type=media_type if safe_inline else "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline" if safe_inline else "attachment",
    )
    return response
