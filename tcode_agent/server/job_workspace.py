from __future__ import annotations

import asyncio
import aiohttp
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import shlex
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import HTTPException

from ..runtime.shared_tracker import SharedTracker
from ..task.orchestration import build_action_policy
from . import workdirs as workdir_utils
from .state import ServerState
from .tmcp_paths import load_tmcp_job_registry


_PREVIEW_TICKET_COOKIE = "tcode_job_preview_ticket"
_WINDOWS_PREVIEW_SECRET = secrets.token_bytes(32)
_PREVIEW_TICKET_TTL_SECONDS = max(
    60, int(os.environ.get("TCODE_JOB_PREVIEW_TTL_SECONDS", "600"))
)

_TEXT_PREVIEW_SUFFIXES = {
    ".txt",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".md",
    ".fa",
    ".fasta",
    ".pdb",
    ".cif",
    ".sdf",
    ".xml",
}
_HTML_PREVIEW_SUFFIXES = {".html", ".htm"}
_STRUCTURE_SUFFIXES = {".pdb", ".cif", ".sdf"}


def _preview_ticket_secret() -> bytes:
    raw = os.environ.get("TCODE_JOB_PREVIEW_SECRET") or os.environ.get("TCODE_PREVIEW_SECRET")
    if not raw:
        if os.name == "nt":
            return _WINDOWS_PREVIEW_SECRET
        raw = f"{ServerState.get_workdir()}:{os.getuid()}:job-preview"
    return hashlib.sha256(str(raw).encode("utf-8")).digest()


def _preview_cookie_path(job_id: str, port: int) -> str:
    return f"/api/job-preview/{job_id}/{port}"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _file_mtime_iso(path_like: Any) -> str:
    path = Path(str(path_like or ""))
    if not path.is_file():
        return ""
    try:
        return datetime.utcfromtimestamp(path.stat().st_mtime).isoformat() + "Z"
    except Exception:
        return ""


def _normalize_preview_ports_local(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    seen: set[int] = set()
    ports: list[int] = []
    for item in value:
        try:
            port = int(item)
        except Exception:
            continue
        if port <= 0 or port > 65535 or port in seen:
            continue
        ports.append(port)
        seen.add(port)
    return ports


def _iter_result_output_path_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            values.append(text)
        return values
    if isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_iter_result_output_path_values(item))
        return values
    if isinstance(value, dict):
        for item in value.values():
            values.extend(_iter_result_output_path_values(item))
    return values


def _extract_result_output_paths_from_payload(payload: Any) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for raw_key, value in node.items():
                key = str(raw_key or "").strip().lower()
                if key in {"output_path", "output_paths"}:
                    for candidate in _iter_result_output_path_values(value):
                        if candidate not in seen:
                            seen.add(candidate)
                            collected.append(candidate)
                else:
                    _walk(value)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item)

    _walk(payload)
    return collected


def _result_output_artifact_entries(workdir: str) -> list[dict[str, str]]:
    result_file = Path(workdir) / "mcp_result.json"
    if not result_file.is_file():
        return []

    try:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_path in _extract_result_output_paths_from_payload(payload):
        entry = _artifact_entry_from_raw_path(raw_path, workdir)
        if not entry:
            continue
        path = entry["path"]
        if path in seen:
            continue
        seen.add(path)
        entries.append(entry)
    return entries


def _artifact_entry_from_raw_path(raw: Any, workdir: str) -> Optional[dict[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return None

    path = Path(text)
    if not path.is_absolute():
        path = Path(workdir) / path

    try:
        if not path.exists():
            return None
        if path.is_dir():
            kind = "directory"
        elif path.is_file():
            kind = "file"
        else:
            return None
    except Exception:
        return None

    return {"path": path.as_posix(), "type": kind}


def _resolve_workspace_artifact_entries(
    expected_artifacts: list[Any],
    workdir: str,
    extra_paths: Optional[list[Any]] = None,
) -> list[dict[str, str]]:
    if not workdir:
        return []

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in list(extra_paths or []) + list(expected_artifacts or []):
        entry = _artifact_entry_from_raw_path(raw, workdir)
        if not entry:
            continue
        path = entry["path"]
        if path in seen:
            continue
        seen.add(path)
        entries.append(entry)
    return entries


def _resolve_workspace_artifacts(
    expected_artifacts: list[Any], workdir: str, extra_paths: Optional[list[Any]] = None
) -> list[str]:
    return [
        entry["path"]
        for entry in _resolve_workspace_artifact_entries(
            expected_artifacts, workdir, extra_paths=extra_paths
        )
    ]


def _path_within_root(path: str, root: str) -> bool:
    try:
        target = Path(path).resolve(strict=False)
        base = Path(root).resolve(strict=False)
        target.relative_to(base)
        return True
    except Exception:
        return False


def _artifact_source_root(
    artifact_entries: list[dict[str, str]], workdir: str
) -> str:
    if artifact_entries:
        first = artifact_entries[0]
        first_path = str(first.get("path", "") or "").strip()
        if first_path:
            if str(first.get("type", "") or "").strip() == "directory":
                return first_path
            return str(Path(first_path).parent)
    return str(workdir or "").strip()


def _build_workspace_artifact_source(
    workspace: dict[str, Any],
    artifact_entries: list[dict[str, str]],
) -> dict[str, Any]:
    source = workspace.get("artifact_source")
    if isinstance(source, dict):
        normalized = dict(source)
    else:
        normalized = {}

    workdir = str(workspace.get("workdir", "") or "").strip()
    root = str(normalized.get("root", "") or "").strip()
    if not root:
        root = _artifact_source_root(artifact_entries, workdir)
    remote_base_url = str(
        normalized.get("remote_base_url")
        or os.environ.get("TCODE_REMOTE_ARTIFACT_BASE_URL")
        or ""
    ).strip()

    kind = str(normalized.get("kind", "") or "").strip()
    if not kind:
        if remote_base_url:
            kind = "remote_http"
        elif root and workdir and _path_within_root(root, workdir):
            kind = "workdir"
        elif root:
            kind = "external_path"
        else:
            kind = "unavailable"

    host = str(
        normalized.get("host")
        or workspace.get("host")
        or workspace.get("execution_host")
        or socket.gethostname()
    )
    browse_api = str(normalized.get("browse_api", "") or "job_artifacts")
    available = bool(normalized.get("available", False))
    if kind == "remote_http" and remote_base_url:
        available = True
    elif root and kind != "remote_http":
        try:
            available = Path(root).exists()
        except Exception:
            available = False

    return {
        "kind": kind,
        "root": root,
        "host": host,
        "browse_api": browse_api,
        "available": available,
        "remote_base_url": remote_base_url,
    }


def _preview_entry_kind_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _HTML_PREVIEW_SUFFIXES:
        return "html"
    if suffix in _STRUCTURE_SUFFIXES:
        return "structure_file"
    if suffix in _TEXT_PREVIEW_SUFFIXES:
        return "text_file"
    return "file"


def _relative_artifact_path(path: str, root: str) -> str:
    text = str(path or "").strip()
    root_text = str(root or "").strip()
    if not text:
        return ""
    if not root_text:
        return text
    try:
        rel = str(Path(text).resolve(strict=False).relative_to(Path(root_text).resolve(strict=False)))
        return "." if rel == "" else rel
    except Exception:
        return text


def _build_workspace_preview_entries(
    workspace: dict[str, Any],
    artifact_entries: list[dict[str, str]],
    artifact_source: dict[str, Any],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    job_id = str(workspace.get("job_id", "") or "").strip()
    artifact_root = str(artifact_source.get("root", "") or "").strip()

    for port in _normalize_preview_ports_local(workspace.get("preview_ports")):
        key = f"service:{port}"
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "label": f"服务预览 {port}",
                "type": "service",
                "port": port,
                "path": "",
                "open_url": f"/api/job-preview/{job_id}/{port}" if job_id else "",
            }
        )

    for artifact in artifact_entries:
        path = str(artifact.get("path", "") or "").strip()
        if not path or str(artifact.get("type", "") or "") != "file":
            continue
        kind = _preview_entry_kind_for_path(path)
        if kind == "file":
            continue
        key = f"{kind}:{path}"
        if key in seen:
            continue
        seen.add(key)
        query = urlencode({"path": _relative_artifact_path(path, artifact_root), "root": artifact_root, "inline": "1"})
        entries.append(
            {
                "label": Path(path).name,
                "type": kind,
                "port": None,
                "path": path,
                "open_url": f"/api/jobs/{job_id}/artifacts/download?{query}" if job_id else "",
            }
        )

    return entries


def _preview_base_urls(job_id: str, preview_ports: list[int]) -> list[str]:
    return [f"/api/job-preview/{job_id}/{port}" for port in preview_ports]


def _extract_submit_job_input_from_description(description: str) -> dict[str, Any]:
    marker = "submit_job.input:"
    idx = str(description or "").find(marker)
    if idx < 0:
        return {}
    payload = _extract_first_json_object(description[idx + len(marker) :])
    return payload if isinstance(payload, dict) else {}


def _extract_original_tool_args_from_description(description: str) -> dict[str, Any]:
    marker = "original_tool_arguments:"
    idx = str(description or "").find(marker)
    if idx < 0:
        return {}
    payload = _extract_first_json_object(description[idx + len(marker) :])
    return payload if isinstance(payload, dict) else {}


def _extract_first_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        decoder = json.JSONDecoder()
        payload, _ = decoder.raw_decode(raw[start:])
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _build_workspace_from_submit_input(
    submit_input: dict[str, Any],
    *,
    issue_identifier: str = "",
    session_id: str = "",
    parent_session_id: str = "",
) -> dict[str, Any]:
    workdir = str(submit_input.get("workdir", "") or "").strip()
    workspace_input = submit_input.get("workspace")
    if not isinstance(workspace_input, dict):
        workspace_input = {}

    context_file = ""
    if workdir:
        context_file = str(Path(workdir) / "job_context.json")

    return {
        "job_id": str(workspace_input.get("job_id", "") or ""),
        "process_id": str(workspace_input.get("process_id", "") or ""),
        "run_id": str(submit_input.get("run_id", "") or ""),
        "tool": str(submit_input.get("tool", "") or ""),
        "task_type": str(submit_input.get("task_type", "") or ""),
        "workdir": workdir,
        "log_file": str(workspace_input.get("log_file", "") or ""),
        "exitcode_file": str(workspace_input.get("exitcode_file", "") or ""),
        "expected_artifacts": submit_input.get("expected_artifacts", [])
        if isinstance(submit_input.get("expected_artifacts"), list)
        else [],
        "context_file": str(workspace_input.get("context_file", "") or context_file),
        "session_id": str(workspace_input.get("session_id", "") or session_id),
        "parent_session_id": str(
            workspace_input.get("parent_session_id", "") or parent_session_id
        ),
        "issue_identifier": str(
            workspace_input.get("issue_identifier", "") or issue_identifier
        ),
        "preview_ports": _normalize_preview_ports_local(
            workspace_input.get("preview_ports")
        ),
        "preview_urls": [],
        "preview_entries": workspace_input.get("preview_entries")
        if isinstance(workspace_input.get("preview_entries"), list)
        else [],
        "artifact_source": workspace_input.get("artifact_source")
        if isinstance(workspace_input.get("artifact_source"), dict)
        else {},
        "host": str(
            workspace_input.get("host")
            or workspace_input.get("execution_host")
            or socket.gethostname()
        ),
        "execution_host": str(
            workspace_input.get("execution_host") or socket.gethostname()
        ),
        "machine_label": str(
            workspace_input.get("machine_label") or socket.gethostname()
        ),
        "input_snapshot": workspace_input.get("input_snapshot")
        if isinstance(workspace_input.get("input_snapshot"), dict)
        else {},
        "input_summary": {},
        "resume_supported": bool(workspace_input.get("resume_supported", False)),
        "resume_command": str(workspace_input.get("resume_command", "") or ""),
        "retry_of": str(workspace_input.get("retry_of", "") or ""),
        "resume_of": str(workspace_input.get("resume_of", "") or ""),
        "review_lineage": workspace_input.get("review_lineage")
        if isinstance(workspace_input.get("review_lineage"), dict)
        else {},
        "orchestration": workspace_input.get("orchestration")
        if isinstance(workspace_input.get("orchestration"), dict)
        else {},
        "tool_contract": workspace_input.get("tool_contract")
        if isinstance(workspace_input.get("tool_contract"), dict)
        else {},
        "action_policy": workspace_input.get("action_policy")
        if isinstance(workspace_input.get("action_policy"), dict)
        else {},
        "timeline_ref": workspace_input.get("timeline_ref")
        if isinstance(workspace_input.get("timeline_ref"), dict)
        else {},
        "submitted_at": str(workspace_input.get("submitted_at", "") or ""),
        "started_at": str(workspace_input.get("started_at", "") or ""),
        "completed_at": str(workspace_input.get("completed_at", "") or ""),
        "cancelled_at": str(workspace_input.get("cancelled_at", "") or ""),
    }


def _hydrate_workspace_from_job_files(
    workspace: dict[str, Any]
) -> dict[str, Any]:
    hydrated = dict(workspace)
    workdir = str(hydrated.get("workdir", "") or "").strip()
    if not workdir:
        hydrated.setdefault("expected_artifacts", [])
        hydrated.setdefault("preview_ports", [])
        hydrated.setdefault("preview_urls", [])
        hydrated.setdefault("preview_entries", [])
        hydrated.setdefault("artifacts", [])
        hydrated.setdefault("artifact_entries", [])
        hydrated.setdefault("artifact_source", {})
        return hydrated

    manifest = _read_json_file(Path(workdir) / "job_manifest.json")
    context_path = Path(str(hydrated.get("context_file", "") or ""))
    if not context_path:
        context_path = Path(workdir) / "job_context.json"
    context_data = _read_json_file(context_path)

    manifest_workspace = manifest.get("workspace")
    if isinstance(manifest_workspace, dict):
        hydrated.update(manifest_workspace)

    context_workspace = context_data.get("workspace")
    if isinstance(context_workspace, dict):
        hydrated.update(context_workspace)

    hydrated["job_id"] = str(
        hydrated.get("job_id") or manifest.get("job_id") or context_data.get("job_id") or ""
    )
    hydrated["process_id"] = str(
        hydrated.get("process_id")
        or manifest.get("process_id")
        or context_data.get("process_id")
        or ""
    )
    hydrated["run_id"] = str(
        hydrated.get("run_id") or manifest.get("run_id") or ""
    )
    hydrated["tool"] = str(
        hydrated.get("tool") or manifest.get("tool") or ""
    )
    hydrated["task_type"] = str(
        hydrated.get("task_type") or manifest.get("task_type") or ""
    )
    hydrated["workdir"] = workdir
    hydrated["log_file"] = str(
        hydrated.get("log_file") or manifest.get("log_file") or ""
    )
    hydrated["exitcode_file"] = str(
        hydrated.get("exitcode_file") or manifest.get("exitcode_file") or ""
    )
    hydrated["context_file"] = str(
        hydrated.get("context_file") or (Path(workdir) / "job_context.json")
    )
    hydrated["host"] = str(
        hydrated.get("host")
        or hydrated.get("execution_host")
        or manifest.get("execution_host")
        or context_data.get("execution_host")
        or socket.gethostname()
    )
    hydrated["execution_host"] = str(
        hydrated.get("execution_host")
        or manifest.get("execution_host")
        or context_data.get("execution_host")
        or socket.gethostname()
    )
    hydrated["machine_label"] = str(
        hydrated.get("machine_label")
        or manifest.get("machine_label")
        or context_data.get("machine_label")
        or hydrated.get("execution_host")
        or socket.gethostname()
    )
    hydrated["expected_artifacts"] = (
        hydrated.get("expected_artifacts")
        if isinstance(hydrated.get("expected_artifacts"), list)
        else manifest.get("expected_artifacts", [])
        if isinstance(manifest.get("expected_artifacts"), list)
        else []
    )
    hydrated["input_summary"] = (
        context_data.get("input_summary")
        if isinstance(context_data.get("input_summary"), dict)
        else hydrated.get("input_summary")
        if isinstance(hydrated.get("input_summary"), dict)
        else {}
    )
    hydrated["input_snapshot"] = (
        hydrated.get("input_snapshot")
        if isinstance(hydrated.get("input_snapshot"), dict)
        else context_workspace.get("input_snapshot")
        if isinstance(context_workspace, dict)
        and isinstance(context_workspace.get("input_snapshot"), dict)
        else {}
    )
    hydrated["resume_supported"] = bool(hydrated.get("resume_supported", False))
    hydrated["resume_command"] = str(hydrated.get("resume_command", "") or "")
    hydrated["retry_of"] = str(hydrated.get("retry_of", "") or "")
    hydrated["resume_of"] = str(hydrated.get("resume_of", "") or "")
    hydrated["review_lineage"] = (
        hydrated.get("review_lineage")
        if isinstance(hydrated.get("review_lineage"), dict)
        else {}
    )
    hydrated["timeline_ref"] = (
        hydrated.get("timeline_ref")
        if isinstance(hydrated.get("timeline_ref"), dict)
        else {}
    )
    hydrated["submitted_at"] = str(
        hydrated.get("submitted_at")
        or manifest.get("submitted_at")
        or context_data.get("submitted_at")
        or (context_data.get("audit") or {}).get("created_at")
        or ""
    )
    hydrated["started_at"] = str(
        hydrated.get("started_at")
        or manifest.get("started_at")
        or hydrated.get("submitted_at")
        or ""
    )
    hydrated["completed_at"] = str(
        hydrated.get("completed_at")
        or manifest.get("completed_at")
        or _file_mtime_iso(hydrated.get("exitcode_file"))
        or ""
    )
    hydrated["cancelled_at"] = str(
        hydrated.get("cancelled_at")
        or manifest.get("terminated_at")
        or ""
    )
    hydrated["preview_ports"] = _normalize_preview_ports_local(
        hydrated.get("preview_ports")
    )
    hydrated["preview_urls"] = _preview_base_urls(
        str(hydrated.get("job_id", "") or ""), hydrated["preview_ports"]
    ) if str(hydrated.get("job_id", "") or "").strip() else []
    result_output_entries = _result_output_artifact_entries(workdir)
    artifact_entries = _resolve_workspace_artifact_entries(
        hydrated.get("expected_artifacts", []),
        workdir,
        extra_paths=[entry["path"] for entry in result_output_entries],
    )
    hydrated["artifact_entries"] = artifact_entries
    hydrated["artifacts"] = [entry["path"] for entry in artifact_entries]
    # Explicit recovery has its own validated download root. The old job
    # directory may be empty after timeout; scanning it must not erase that list.
    if (workspace.get("artifact_recovery") and workspace.get("transfer_state") == "ready"
            and isinstance(workspace.get("artifacts"), list)):
        for key in ("artifacts", "output_root", "output_abs_path", "transfer_state", "artifact_recovery"):
            if key in workspace:
                hydrated[key] = workspace[key]
    hydrated["artifact_source"] = _build_workspace_artifact_source(
        hydrated, artifact_entries
    )
    hydrated["output_root"] = str(
        hydrated.get("output_root")
        or (hydrated.get("artifact_source") or {}).get("root")
        or ""
    )
    hydrated["preview_entries"] = _build_workspace_preview_entries(
        hydrated, artifact_entries, hydrated.get("artifact_source") or {}
    )
    return hydrated


def _map_job_runtime_status(job: dict[str, Any]) -> str:
    # Import lazily because _shared imports this module at import time.
    from ._shared import _map_job_runtime_status as _shared_map_job_runtime_status

    return _shared_map_job_runtime_status(job)


def _load_symphony_memory_issue_store(workdir: Optional[str] = None) -> dict[str, Any]:
    # Import lazily because _shared imports this module at import time.
    from ._shared import (
        _load_symphony_memory_issue_store as _shared_load_symphony_memory_issue_store,
    )

    return _shared_load_symphony_memory_issue_store(workdir)


def _build_job_workspace_summary(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    workdir = str(job.get("workdir", "") or "").strip()
    workspace = {}
    if isinstance(job.get("workspace"), dict):
        workspace.update(job.get("workspace"))
    if not isinstance(workspace.get("owner"), dict) and isinstance(job.get("owner"), dict):
        workspace["owner"] = dict(job.get("owner"))
    workspace.update(
        {
            "job_id": job_id,
            "process_id": str(job.get("process_id", "") or ""),
            "run_id": str(job.get("run_id", "") or ""),
            "tool": str(job.get("tool", "") or ""),
            "task_type": str(job.get("task_type", "") or ""),
            "workdir": workdir,
            "log_file": str(job.get("log_file", "") or ""),
            "exitcode_file": str(job.get("exitcode_file", "") or ""),
            "expected_artifacts": job.get("expected_artifacts", [])
            if isinstance(job.get("expected_artifacts"), list)
            else [],
            "context_file": str(
                (job.get("workspace") or {}).get("context_file", "")
                if isinstance(job.get("workspace"), dict)
                else ""
            )
            or (str(Path(workdir) / "job_context.json") if workdir else ""),
        }
    )
    workspace = _hydrate_workspace_from_job_files(workspace)
    if not isinstance(workspace.get("review_lineage"), dict) or not workspace.get("review_lineage"):
        workspace["review_lineage"] = _latest_review_lineage_for_workspace(workspace)
    workspace["status"] = _map_job_runtime_status(job)
    workspace["action_policy"] = build_action_policy(
        lifecycle_status=workspace.get("status", ""),
        job_id=job_id,
        has_submit_input=bool(job),
        resume_supported=bool(workspace.get("resume_supported", False)),
        resume_command=str(workspace.get("resume_command", "") or ""),
        review_status=str((workspace.get("review_lineage") or {}).get("status", "") or "")
        if isinstance(workspace.get("review_lineage"), dict)
        else "",
    )
    return workspace


def _build_issue_workspace_summary(issue: dict[str, Any]) -> dict[str, Any]:
    existing = issue.get("workspace")
    workspace = dict(existing) if isinstance(existing, dict) else {}
    submit_input = _extract_submit_job_input_from_description(
        str(issue.get("description", "") or "")
    )
    resume_data = issue.get("resume_data")
    if not isinstance(resume_data, dict):
        resume_data = {}

    if submit_input:
        workspace = {
            **_build_workspace_from_submit_input(
                submit_input,
                issue_identifier=str(issue.get("identifier", "") or ""),
                session_id=str(resume_data.get("session_id", "") or ""),
                parent_session_id=str(resume_data.get("parent_session_id", "") or ""),
            ),
            **workspace,
        }
    if not isinstance(workspace.get("input_snapshot"), dict) or not workspace.get("input_snapshot"):
        original_args = _extract_original_tool_args_from_description(
            str(issue.get("description", "") or "")
        )
        if original_args:
            workspace["input_snapshot"] = original_args

    workspace = _hydrate_workspace_from_job_files(workspace)
    if not workspace.get("issue_identifier"):
        workspace["issue_identifier"] = str(issue.get("identifier", "") or "")
    workspace["status"] = str(issue.get("state", "") or workspace.get("status", "") or "")
    workspace["cancel_actions"] = (
        issue.get("cancel_actions")
        if isinstance(issue.get("cancel_actions"), list)
        else []
    )
    if workspace.get("job_id"):
        workspace["preview_urls"] = _preview_base_urls(
            str(workspace.get("job_id", "") or ""),
            _normalize_preview_ports_local(workspace.get("preview_ports")),
        )
    if not isinstance(workspace.get("review_lineage"), dict) or not workspace.get("review_lineage"):
        workspace["review_lineage"] = _latest_review_lineage_for_workspace(workspace)
    workspace["action_policy"] = build_action_policy(
        lifecycle_status=workspace.get("status", ""),
        job_id=str(workspace.get("job_id", "") or ""),
        has_submit_input=bool(submit_input),
        resume_supported=bool(workspace.get("resume_supported", False)),
        resume_command=str(workspace.get("resume_command", "") or ""),
        review_status=str((workspace.get("review_lineage") or {}).get("status", "") or "")
        if isinstance(workspace.get("review_lineage"), dict)
        else "",
    )
    if not workspace.get("job_id") and (workspace.get("recovery") or {}).get("state") == "submission_unknown":
        workspace["action_policy"]["cancel"] = {
            "available": False,
            "reason": "远程提交状态待核对，尚无可验证的远程任务编号；不能确认远程停止。",
        }
    return workspace


def _normalize_review_workspace(
    review: dict[str, Any], review_data: dict[str, Any]
) -> dict[str, Any]:
    workspace = review_data.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}

    if not workspace:
        workspace = {
            "job_id": "",
            "process_id": "",
            "run_id": "",
            "tool": str(review.get("tool_server", "") or ""),
            "task_type": str(review.get("tool_name", "") or ""),
            "workdir": str(review_data.get("workdir", "") or ""),
            "log_file": "",
            "exitcode_file": "",
            "expected_artifacts": [],
            "context_file": "",
            "session_id": str(review_data.get("session_id", "") or ""),
            "parent_session_id": str(review_data.get("parent_session_id", "") or ""),
            "issue_identifier": str(review.get("identifier", "") or ""),
            "preview_ports": [],
            "preview_urls": [],
        }

    workspace = _hydrate_workspace_from_job_files(workspace)
    workspace["status"] = str(review.get("status", "") or "")
    workspace["issue_identifier"] = str(
        workspace.get("issue_identifier") or review.get("identifier") or ""
    )
    workspace["review_lineage"] = {
        "identifier": str(review.get("identifier", "") or ""),
        "status": str(review.get("status", "") or ""),
        "reason": str(review.get("reason", "") or ""),
        "reviewed_at": str(review.get("reviewed_at", "") or ""),
        "resume_status": str(review.get("resume_status", "") or ""),
    }
    if not isinstance(workspace.get("timeline_ref"), dict):
        workspace["timeline_ref"] = {}
    workspace["timeline_ref"] = {
        **(workspace.get("timeline_ref") or {}),
        "review_identifier": str(review.get("identifier", "") or ""),
        "review_status": str(review.get("status", "") or ""),
    }
    return workspace


def _reconcile_human_review_queue(store: dict[str, Any]) -> bool:
    reviews = store.get("reviews", [])
    if not isinstance(reviews, list):
        return False
    changed = False
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_data = review.get("review_data")
        if not isinstance(review_data, dict):
            review_data = {}
            review["review_data"] = review_data
            changed = True
        workspace = _normalize_review_workspace(review, review_data)
        if review_data.get("workspace") != workspace:
            review_data["workspace"] = workspace
            review["review_data"] = review_data
            changed = True
    return changed


def _human_review_config_path() -> Path:
    explicit = str(os.environ.get("HUMAN_REVIEW_CONFIG_PATH", "") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path(ServerState.get_workdir()) / path
        return path.resolve(strict=False)
    config_root = str(os.environ.get("TCODE_CONFIG_ROOT", "") or "").strip()
    base = Path(config_root).expanduser() if config_root else Path(ServerState.get_workdir())
    return (base / "human_review.json").resolve(strict=False)


def _load_human_review_config() -> dict[str, Any]:
    path = _human_review_config_path()
    if not path.exists():
        return {"enabled": False, "mode": "disabled"}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        return config
    except Exception:
        return {"enabled": False, "mode": "disabled"}


def _human_review_store_path() -> Path:
    workdir = ServerState.get_workdir()
    return Path(workdir) / "tmp" / "human_review_queue.json"


def _candidate_human_review_store_workdirs() -> list[str]:
    """Return known workdirs that may hold human review tracker rows."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(path: Any) -> None:
        text = str(path or "").strip()
        if not text:
            return
        try:
            resolved = Path(text).expanduser().resolve(strict=False).as_posix()
        except Exception:
            resolved = text
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(text)

    server_workdir = ServerState.get_workdir()
    _add(server_workdir)

    for session in ServerState.sessions.values():
        if isinstance(session, dict):
            _add(session.get("workdir"))

    workspace_root = workdir_utils.workspace_storage_root(server_workdir)
    try:
        for tenant_dir in workspace_root.iterdir():
            if not tenant_dir.is_dir():
                continue
            for user_dir in tenant_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                for path in user_dir.iterdir():
                    if path.is_dir():
                        _add(path)
    except Exception:
        pass

    return candidates


def _load_human_review_queue_for_workdir(workdir: str) -> dict[str, Any]:
    try:
        data = SharedTracker.load_review_store(workdir)
        if not isinstance(data, dict):
            data = {"reviews": [], "history": []}
        data.setdefault("reviews", [])
        data.setdefault("history", [])
        if _reconcile_human_review_queue(data):
            SharedTracker.sync_review_store(data, workdir)
        return data
    except Exception:
        return {"reviews": [], "history": []}


def _load_human_review_queue() -> dict[str, Any]:
    return _load_human_review_queue_for_workdir(ServerState.get_workdir())


def _load_aggregated_human_review_queue() -> dict[str, Any]:
    reviews_by_key: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []

    for workdir in _candidate_human_review_store_workdirs():
        tracker_path = SharedTracker.db_path(workdir)
        legacy_path = Path(workdir) / "tmp" / "human_review_queue.json"
        if not tracker_path.is_file() and not legacy_path.is_file():
            continue
        store = _load_human_review_queue_for_workdir(workdir)
        reviews = store.get("reviews", [])
        if not isinstance(reviews, list):
            continue
        for raw_review in reviews:
            if not isinstance(raw_review, dict):
                continue
            review = dict(raw_review)
            review.setdefault("review_data", {})
            if isinstance(review.get("review_data"), dict):
                review["review_data"] = dict(review["review_data"])
                review["review_data"].setdefault("store_workdir", workdir)
            review_key = (
                str(review.get("id") or "").strip()
                or str(review.get("identifier") or "").strip()
                or f"{workdir}:{len(reviews_by_key)}"
            )
            existing = reviews_by_key.get(review_key)
            existing_ts = str(
                (existing or {}).get("updated_at")
                or (existing or {}).get("reviewed_at")
                or (existing or {}).get("created_at")
                or ""
            )
            review_ts = str(
                review.get("updated_at")
                or review.get("reviewed_at")
                or review.get("created_at")
                or ""
            )
            if existing is None or review_ts >= existing_ts:
                reviews_by_key[review_key] = review

        raw_history = store.get("history", [])
        if isinstance(raw_history, list):
            history.extend(item for item in raw_history if isinstance(item, dict))

    ordered_reviews = sorted(
        reviews_by_key.values(),
        key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""),
    )
    return {"reviews": ordered_reviews, "history": history}


def _save_human_review_queue(store: dict[str, Any]):
    SharedTracker.sync_review_store(store, ServerState.get_workdir())


def _save_human_review_queue_for_workdir(store: dict[str, Any], workdir: str):
    SharedTracker.sync_review_store(store, workdir or ServerState.get_workdir())


def _find_human_review_store_for_update(
    *,
    issue_id: str = "",
    issue_identifier: str = "",
) -> tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]] | None:
    want_id = str(issue_id or "").strip()
    want_identifier = str(issue_identifier or "").strip()
    if not want_id and not want_identifier:
        return None

    for workdir in _candidate_human_review_store_workdirs():
        tracker_path = SharedTracker.db_path(workdir)
        legacy_path = Path(workdir) / "tmp" / "human_review_queue.json"
        if not tracker_path.is_file() and not legacy_path.is_file():
            continue
        store = _load_human_review_queue_for_workdir(workdir)
        reviews = store.get("reviews", [])
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if want_id and str(review.get("id", "")).strip() == want_id:
                return workdir, store, reviews, review
            if (
                want_identifier
                and str(review.get("identifier", "")).strip() == want_identifier
            ):
                return workdir, store, reviews, review

    return None


def _canonicalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonicalize_json(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonicalize_json(v) for v in value]
    return value


def _tool_args_match(left: Any, right: Any) -> bool:
    return _canonicalize_json(left) == _canonicalize_json(right)


def _review_arg_match_candidates(args: Any, tool_name: str) -> list[Any]:
    if not isinstance(args, dict):
        return [args]

    candidates: list[Any] = []

    def _add(value: Any) -> None:
        marker = _canonicalize_json(value)
        if any(_canonicalize_json(existing) == marker for existing in candidates):
            return
        candidates.append(value)

    raw = dict(args)
    _add(raw)

    if str(tool_name or "").endswith("_mcp_server"):
        control_keys = {
            "target_server",
            "target_tool",
            "server_name",
            "tool_name",
            "server",
            "tool",
        }
        if isinstance(raw.get("input_config"), dict):
            inner = dict(raw["input_config"])
            for key in control_keys:
                inner.pop(key, None)
            _add({"input_config": inner})
            _add(inner)
        else:
            inner = dict(raw)
            for key in control_keys:
                inner.pop(key, None)
            _add({"input_config": inner})

    return candidates


def _review_tool_args_match(left: Any, right: Any, tool_name: str) -> bool:
    left_candidates = _review_arg_match_candidates(left, tool_name)
    right_candidates = _review_arg_match_candidates(right, tool_name)
    return any(
        _tool_args_match(left_candidate, right_candidate)
        for left_candidate in left_candidates
        for right_candidate in right_candidates
    )


def _has_approved_review_for_call(
    tool_server: str,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    allowed_workdir_root: str = "",
) -> Optional[dict[str, Any]]:
    store = _load_aggregated_human_review_queue()
    reviews = store.get("reviews", [])
    if not isinstance(reviews, list):
        return None

    for review in reversed(reviews):
        if not isinstance(review, dict):
            continue
        if allowed_workdir_root:
            review_data = (
                review.get("review_data")
                if isinstance(review.get("review_data"), dict)
                else {}
            )
            workspace = (
                review_data.get("workspace")
                if isinstance(review_data.get("workspace"), dict)
                else {}
            )
            review_workdir = str(
                review_data.get("store_workdir")
                or review_data.get("workdir")
                or workspace.get("workdir")
                or ""
            ).strip()
            try:
                Path(review_workdir).expanduser().resolve(strict=False).relative_to(
                    Path(allowed_workdir_root).expanduser().resolve(strict=False)
                )
            except Exception:
                continue
        status = str(review.get("status", "")).strip().lower()
        if status != "approved":
            continue
        if (
            str(review.get("execution_disposition", "")).strip().lower()
            == "revise_before_execute"
        ):
            continue
        server = str(review.get("tool_server", "")).strip().lower()
        name = str(review.get("tool_name", "")).strip()
        if server != tool_server or name != tool_name:
            continue
        approved_args = review.get("tool_args", {})
        if _review_tool_args_match(approved_args, tool_args, tool_name):
            return review

    return None


def _find_matching_reviews_for_workspace(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    tool_server = str(workspace.get("tool", "") or "").strip().lower()
    tool_name = str(workspace.get("task_type", "") or "").strip()
    input_snapshot = workspace.get("input_snapshot")
    if not tool_server or not tool_name or not isinstance(input_snapshot, dict):
        return []

    store = _load_human_review_queue()
    candidates: list[dict[str, Any]] = []
    for bucket_name in ("reviews", "history"):
        bucket = store.get(bucket_name, [])
        if not isinstance(bucket, list):
            continue
        for review in bucket:
            if not isinstance(review, dict):
                continue
            server = str(review.get("tool_server", "") or "").strip().lower()
            name = str(review.get("tool_name", "") or "").strip()
            if server != tool_server or name != tool_name:
                continue
            tool_args = review.get("tool_args")
            if not isinstance(tool_args, dict):
                continue
            if _tool_args_match(tool_args, input_snapshot):
                candidates.append(review)
    return candidates


def _latest_review_lineage_for_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    matches = _find_matching_reviews_for_workspace(workspace)
    if not matches:
        return {}
    latest = sorted(
        matches,
        key=lambda item: str(item.get("updated_at") or item.get("reviewed_at") or item.get("created_at") or ""),
    )[-1]
    return {
        "identifier": str(latest.get("identifier", "") or ""),
        "status": str(latest.get("status", "") or ""),
        "reason": str(latest.get("reason", "") or ""),
        "reviewed_at": str(latest.get("reviewed_at", "") or ""),
        "resume_status": str(latest.get("resume_status", "") or ""),
    }


def _extract_mcp_payload_dict(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    nested = result.get("result")
    if isinstance(nested, dict):
        if isinstance(nested.get("structuredContent"), dict):
            return nested.get("structuredContent")
        if isinstance(nested.get("data"), dict):
            return nested.get("data")
        return nested
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _build_submit_input_from_job_and_issue(
    job_id: str, job: dict[str, Any], issue: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    if issue:
        submit_input = _extract_submit_job_input_from_description(
            str(issue.get("description", "") or "")
        )
        if submit_input:
            return submit_input

    submission = job.get("submission")
    if isinstance(submission, dict) and submission:
        return dict(submission)

    workspace = job.get("workspace") if isinstance(job.get("workspace"), dict) else {}
    return {
        "run_id": str(job.get("run_id", "") or ""),
        "tool": str(job.get("tool", "") or ""),
        "task_type": str(job.get("task_type", "") or ""),
        "workdir": str(job.get("workdir", "") or ""),
        "command": str(job.get("command", "") or ""),
        "expected_artifacts": list(job.get("expected_artifacts", []) or []),
        "timeout_seconds": 0 if job.get("timeout_seconds") in (None, "") else job["timeout_seconds"],
        "retry_policy": dict(job.get("retry_policy", {}) or {}),
        "workspace": dict(workspace),
    }


def _snapshot_job_state(
    *,
    job_id: str,
    workspace: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
    reason: str = "retry",
) -> str:
    if str(workspace.get("storage_mode") or "").strip() == "broker_owned":
        return ""
    workdir_text = str(workspace.get("workdir", "") or "").strip()
    if not workdir_text:
        return ""
    workdir = Path(workdir_text)
    snapshot_dir = (
        workdir
        / ".job_snapshots"
        / f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{reason}_{job_id}"
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    tracked_paths: list[Path] = []
    for candidate in [
        workdir / "job_manifest.json",
        workdir / "job_context.json",
        workdir / "mcp_result.json",
    ]:
        if candidate.exists():
            tracked_paths.append(candidate)
    for pattern in ("*.log", "*.exitcode"):
        tracked_paths.extend(sorted(workdir.glob(pattern)))

    moved: list[str] = []
    for path in tracked_paths:
        target = snapshot_dir / path.name
        try:
            shutil.copy2(path.as_posix(), target.as_posix())
            moved.append(target.as_posix())
        except Exception:
            continue

    snapshot_meta = {
        "job_id": job_id,
        "reason": reason,
        "created_at": _utc_now_iso(),
        "workspace": workspace,
        "issue": issue or {},
        "snapshot_files": moved,
    }
    (snapshot_dir / "snapshot_meta.json").write_text(
        json.dumps(snapshot_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot_dir.as_posix()


def _build_resubmission_input(
    *,
    submit_input: dict[str, Any],
    source_workspace: dict[str, Any],
    lineage_key: str,
    lineage_value: str,
) -> dict[str, Any]:
    payload = dict(submit_input or {})
    base_run_id = str(payload.get("run_id", "") or source_workspace.get("run_id", "") or "job")
    new_run_id = f"{base_run_id}_{lineage_key}_{datetime.utcnow().strftime('%H%M%S%f')}"
    original_workdir = str(payload.get("workdir", "") or source_workspace.get("workdir", "") or "").strip()
    new_workdir = ""
    if original_workdir:
        new_workdir = (Path(original_workdir).parent / new_run_id).as_posix()

    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}
    workspace = dict(workspace)
    workspace["job_id"] = ""
    workspace["process_id"] = ""
    workspace["log_file"] = ""
    workspace["exitcode_file"] = ""
    workspace["context_file"] = ""
    workspace["preview_urls"] = []
    workspace["preview_entries"] = []
    workspace["artifacts"] = []
    workspace["artifact_entries"] = []
    workspace["artifact_source"] = {}
    workspace["output_root"] = ""
    workspace["completed_at"] = ""
    workspace["cancelled_at"] = ""
    workspace["submitted_at"] = ""
    workspace["started_at"] = ""
    workspace[lineage_key] = lineage_value
    if lineage_key == "retry_of":
        workspace.setdefault("resume_of", "")
    if lineage_key == "resume_of":
        workspace.setdefault("retry_of", str(source_workspace.get("retry_of", "") or ""))
    payload["workspace"] = workspace

    payload["run_id"] = new_run_id
    if new_workdir:
        payload["workdir"] = new_workdir

    command = str(payload.get("command", "") or "")
    if new_workdir and "mcp_tool_job_runner.py" in command and "--output-file" in command:
        output_file = (Path(new_workdir) / "mcp_result.json").as_posix()

        def _replace_output_file(match: re.Match[str]) -> str:
            return f"{match.group(1)}{shlex.quote(output_file)}"

        payload["command"] = re.sub(
            r"(--output-file(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)",
            _replace_output_file,
            command,
            count=1,
        )
    return payload


def _normalize_lifecycle_status(
    workspace: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
    reviews: Optional[list[dict[str, Any]]] = None,
) -> str:
    review_items = reviews if isinstance(reviews, list) else _find_matching_reviews_for_workspace(workspace)
    latest_review_status = ""
    if review_items:
        latest_review = sorted(
            review_items,
            key=lambda item: str(
                item.get("updated_at")
                or item.get("reviewed_at")
                or item.get("created_at")
                or ""
            ),
        )[-1]
        latest_review_status = str(latest_review.get("status", "") or "").strip().lower()
        if latest_review_status == "pending_review":
            return "review_pending"

    state = str(
        workspace.get("status")
        or (issue.get("state") if isinstance(issue, dict) else "")
        or workspace.get("issue_state")
        or ""
    ).strip().lower()

    if state in {"done", "closed", "completed"}:
        return "completed"
    if state == "failed":
        return "failed"
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state == "terminated":
        return "terminated"
    if state in {"in progress", "running"}:
        if str(workspace.get("retry_of", "") or "").strip() or str(workspace.get("resume_of", "") or "").strip():
            return "retrying"
        return "running"
    return "queued"


def _append_timeline_event(
    events: list[dict[str, Any]],
    *,
    kind: str,
    at: str,
    title: str,
    source: str,
    details: str = "",
    metadata: Optional[dict[str, Any]] = None,
):
    when = str(at or "").strip()
    if not when:
        return
    events.append(
        {
            "kind": kind,
            "at": when,
            "title": title,
            "source": source,
            "details": details,
            "metadata": metadata or {},
        }
    )


def _build_job_timeline(
    job_id: str,
    workspace: dict[str, Any],
    issue: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    issue_obj = issue if isinstance(issue, dict) else None
    reviews = _find_matching_reviews_for_workspace(workspace)
    events: list[dict[str, Any]] = []

    submitted_at = str(
        workspace.get("submitted_at")
        or (issue_obj.get("created_at") if issue_obj else "")
        or ""
    )
    retry_of = str(workspace.get("retry_of", "") or "").strip()
    resume_of = str(workspace.get("resume_of", "") or "").strip()
    title = "任务已提交"
    details = ""
    if retry_of:
        title = "已安排重试"
        details = f"来源任务: {retry_of}"
    elif resume_of:
        title = "已安排恢复"
        details = f"来源任务: {resume_of}"
    _append_timeline_event(
        events,
        kind="submitted" if not (retry_of or resume_of) else "retry_scheduled",
        at=submitted_at,
        title=title,
        source="job_protocol",
        details=details,
        metadata={"job_id": job_id},
    )

    for review in sorted(
        reviews,
        key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""),
    ):
        identifier = str(review.get("identifier", "") or "")
        reason = str(review.get("reason", "") or "")
        created_at = str(review.get("created_at", "") or "")
        status = str(review.get("status", "") or "").strip().lower()
        _append_timeline_event(
            events,
            kind="review_pending",
            at=created_at,
            title="进入人工审核",
            source="human_review",
            details=reason or identifier,
            metadata={"review_identifier": identifier, "status": review.get("status")},
        )
        if status == "approved":
            _append_timeline_event(
                events,
                kind="review_approved",
                at=str(review.get("reviewed_at") or review.get("updated_at") or created_at),
                title="人工审核已通过",
                source="human_review",
                details=str(review.get("comments", "") or ""),
                metadata={"review_identifier": identifier},
            )
        elif status == "rejected":
            _append_timeline_event(
                events,
                kind="review_rejected",
                at=str(review.get("reviewed_at") or review.get("updated_at") or created_at),
                title="人工审核已驳回",
                source="human_review",
                details=str(review.get("rejection_reason", "") or review.get("comments", "") or ""),
                metadata={"review_identifier": identifier},
            )

    _append_timeline_event(
        events,
        kind="started",
        at=str(workspace.get("started_at", "") or ""),
        title="任务开始执行",
        source="job_protocol",
        details=str(workspace.get("execution_host", "") or workspace.get("host", "") or ""),
    )

    preview_entries = workspace.get("preview_entries")
    if isinstance(preview_entries, list) and preview_entries:
        _append_timeline_event(
            events,
            kind="preview_available",
            at=str(
                workspace.get("started_at")
                or workspace.get("submitted_at")
                or (issue_obj.get("updated_at") if issue_obj else "")
                or ""
            ),
            title="预览入口已就绪",
            source="workspace",
            details=" / ".join(
                [
                    str(item.get("label", "") or "")
                    for item in preview_entries
                    if isinstance(item, dict) and str(item.get("label", "") or "").strip()
                ][:3]
            ),
        )

    cancelled_at = str(
        workspace.get("cancelled_at")
        or (issue_obj.get("cancelled_at") if issue_obj else "")
        or ""
    )
    if cancelled_at:
        _append_timeline_event(
            events,
            kind="cancelled",
            at=cancelled_at,
            title="任务已取消",
            source="workspace",
            details="; ".join(
                [
                    str(action.get("reason", "") or "")
                    for action in (workspace.get("cancel_actions") or [])
                    if isinstance(action, dict) and str(action.get("reason", "") or "").strip()
                ][:3]
            ),
        )

    lifecycle_status = _normalize_lifecycle_status(workspace, issue_obj, reviews)
    if lifecycle_status == "completed":
        _append_timeline_event(
            events,
            kind="completed",
            at=str(
                workspace.get("completed_at")
                or (issue_obj.get("updated_at") if issue_obj else "")
                or ""
            ),
            title="任务执行完成",
            source="job_protocol",
            details=str(workspace.get("result_summary", "") or ""),
        )
    elif lifecycle_status == "failed":
        _append_timeline_event(
            events,
            kind="failed",
            at=str(
                workspace.get("completed_at")
                or (issue_obj.get("updated_at") if issue_obj else "")
                or ""
            ),
            title="任务执行失败",
            source="job_protocol",
            details=str(
                workspace.get("result_summary")
                or (issue_obj.get("error_summary") if issue_obj else "")
                or ""
            ),
        )
    elif lifecycle_status == "terminated":
        _append_timeline_event(
            events,
            kind="terminated",
            at=str(
                workspace.get("cancelled_at")
                or workspace.get("completed_at")
                or (issue_obj.get("updated_at") if issue_obj else "")
                or ""
            ),
            title="任务已终止",
            source="job_protocol",
        )

    if issue_obj and str(issue_obj.get("result_summary", "") or "").strip():
        _append_timeline_event(
            events,
            kind="result_collected",
            at=str(issue_obj.get("updated_at", "") or issue_obj.get("created_at", "") or ""),
            title="结果已汇总到 Symphony",
            source="symphony",
            details=str(issue_obj.get("result_summary", "") or "")[:240],
        )

    task_id = str(
        workspace.get("task_id")
        or (issue_obj.get("id") if issue_obj else "")
        or ""
    ).strip()
    if task_id:
        try:
            tracker_events = SharedTracker.list_task_events(
                task_id, ServerState.get_workdir()
            )
        except Exception:
            tracker_events = []
        title_map = {
            "claimed": "任务已被执行器认领",
            "started": "任务进入执行阶段",
            "completed": "任务已完成",
            "failed": "任务执行失败",
            "cancelled": "任务已取消",
            "duplicate": "任务被判定为重复",
            "created": "任务已登记到 Shared Tracker",
            "updated": "任务状态已更新",
        }
        for tracker_event in tracker_events:
            if not isinstance(tracker_event, dict):
                continue
            kind = str(tracker_event.get("kind", "") or "").strip()
            at = str(tracker_event.get("created_at", "") or "").strip()
            if not kind or not at:
                continue
            payload = (
                tracker_event.get("payload")
                if isinstance(tracker_event.get("payload"), dict)
                else {}
            )
            details = ""
            if kind == "claimed":
                details = str(payload.get("worker_id", "") or "")
            elif kind in {"completed", "failed", "cancelled"}:
                details = str(
                    payload.get("result_summary")
                    or payload.get("error_summary")
                    or ""
                )
            _append_timeline_event(
                events,
                kind=kind,
                at=at,
                title=title_map.get(kind, f"Tracker 事件: {kind}"),
                source=str(tracker_event.get("source", "") or "tracker"),
                details=details,
                metadata=payload,
            )

    events.sort(key=lambda item: (str(item.get("at", "") or ""), str(item.get("kind", "") or "")))
    return events


def _preview_ticket_signature(payload: str) -> str:
    # Legacy tickets used a timezone-dependent expiry. Do not accept them after upgrade.
    signing_key = hmac.digest(_preview_ticket_secret(), b"tcode-job-preview:v2", "sha256")
    return hmac.new(signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_preview_ticket(ticket: str, job_id: str, port: int) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(ticket.encode("ascii")).decode("utf-8")
        parsed_job_id, parsed_port, parsed_exp, parsed_sig = decoded.rsplit(":", 3)
        expected = _preview_ticket_signature(f"{parsed_job_id}:{parsed_port}:{parsed_exp}")
        if not hmac.compare_digest(parsed_sig, expected):
            return False
        if parsed_job_id != job_id or int(parsed_port) != int(port):
            return False
        if int(parsed_exp) < int(time.time()):
            return False
        return True
    except Exception:
        return False


def _sign_preview_ticket(job_id: str, port: int, expires_at: int) -> str:
    payload = f"{job_id}:{port}:{expires_at}"
    sig = _preview_ticket_signature(payload)
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _rewrite_preview_location(location: str, job_id: str, port: int) -> str:
    text = str(location or "").strip()
    if not text:
        return ""
    local_prefixes = [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    ]
    for prefix in local_prefixes:
        if text.startswith(prefix):
            suffix = text[len(prefix) :]
            return f"/api/job-preview/{job_id}/{port}{suffix}"
    if text.startswith("/"):
        return f"/api/job-preview/{job_id}/{port}{text}"
    return text


def _get_job_workspace_or_404(job_id: str) -> dict[str, Any]:
    registry = _load_job_registry()
    job = registry.get(job_id)
    if not isinstance(job, dict):
        raise HTTPException(404, f"job not found: {job_id}")
    return _build_job_workspace_summary(job_id, job)


def _find_related_symphony_issue(workspace: dict[str, Any]) -> Optional[dict[str, Any]]:
    job_id = str(workspace.get("job_id", "") or "").strip()
    workdir = str(workspace.get("workdir", "") or "").strip()
    store = _load_symphony_memory_issue_store()
    issues = store.get("issues", [])
    if not isinstance(issues, list):
        return None
    for issue in reversed(issues):
        if not isinstance(issue, dict):
            continue
        issue_workspace = issue.get("workspace")
        if isinstance(issue_workspace, dict):
            if job_id and str(issue_workspace.get("job_id", "") or "").strip() == job_id:
                return issue
            if workdir and str(issue_workspace.get("workdir", "") or "").strip() == workdir:
                return issue
        desc_workspace = _build_issue_workspace_summary(issue)
        if job_id and str(desc_workspace.get("job_id", "") or "").strip() == job_id:
            return issue
        if workdir and str(desc_workspace.get("workdir", "") or "").strip() == workdir:
            return issue
    return None


def _load_job_registry() -> dict[str, Any]:
    return load_tmcp_job_registry()


async def _proxy_remote_artifact_json(
    remote_base_url: str,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if not remote_base_url:
        raise HTTPException(502, "Remote artifact base URL is not configured")
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{remote_base_url.rstrip('/')}{endpoint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=filtered) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = data.get("detail") if isinstance(data, dict) else None
                    raise HTTPException(resp.status, str(detail or "Remote artifact request failed"))
                return data if isinstance(data, dict) else {}
    except HTTPException:
        raise
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Remote artifact request failed: {exc}")


async def _proxy_remote_artifact_download(
    remote_base_url: str,
    endpoint: str,
    params: dict[str, Any],
) -> Any:
    if not remote_base_url:
        raise HTTPException(502, "Remote artifact base URL is not configured")
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{remote_base_url.rstrip('/')}{endpoint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=filtered) as resp:
                body = await resp.read()
                headers: dict[str, str] = {}
                for header_name in ("Content-Type", "Content-Disposition"):
                    value = resp.headers.get(header_name)
                    if value:
                        headers[header_name.lower()] = value
                if resp.status >= 400:
                    detail = body.decode("utf-8", errors="replace")[:400]
                    raise HTTPException(resp.status, detail or "Remote artifact download failed")
                from fastapi import Response

                return Response(content=body, status_code=resp.status, headers=headers)
    except HTTPException:
        raise
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Remote artifact download failed: {exc}")
