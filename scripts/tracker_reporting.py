"""Small stdlib-only client for reporting Symphony execution to SharedTracker."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from tracker_token import read_tracker_token
except ImportError:  # pragma: no cover - package-style test imports
    from scripts.tracker_token import read_tracker_token


class TrackerReportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0):
        self.status_code = int(status_code or 0)
        super().__init__(message)


def extract_submit_input_from_prompt(prompt: str) -> dict[str, Any]:
    """Decode the first routed ``submit_job.input`` object from a prompt."""

    text = str(prompt or "")
    for marker in ("submit_job.input:", "参数："):
        marker_index = text.find(marker)
        if marker_index < 0:
            continue
        payload = text[marker_index + len(marker) :]
        object_index = payload.find("{")
        if object_index < 0:
            continue
        try:
            decoded, _ = json.JSONDecoder().raw_decode(payload[object_index:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue
        nested = decoded.get("input")
        if marker == "参数：" and isinstance(nested, dict):
            return nested
        return decoded
    return {}


def tracker_scope_from_submit_input(
    submit_input: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(submit_input or {})
    workspace = (
        payload.get("workspace")
        if isinstance(payload.get("workspace"), dict)
        else {}
    )
    scope: dict[str, Any] = {}
    for key in ("tenant_id", "user_id", "workspace_id"):
        value = str(workspace.get(key) or payload.get(key) or "").strip()
        if value:
            scope[key] = value
    try:
        generation = int(
            workspace.get("workspace_generation")
            or payload.get("workspace_generation")
            or 0
        )
    except (TypeError, ValueError):
        generation = 0
    if generation > 0:
        scope["workspace_generation"] = generation
    return scope


def report_execution(
    *,
    identifier: str,
    status: str,
    submit_input: Mapping[str, Any] | None = None,
    result_summary: str = "",
    error_summary: str = "",
    workspace_patch: Mapping[str, Any] | None = None,
    metadata_patch: Mapping[str, Any] | None = None,
    job_ref: Mapping[str, Any] | None = None,
    source: str = "symphony_adapter",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report execution state through the authenticated Tracker API."""

    issue_identifier = str(identifier or "").strip()
    if not issue_identifier:
        raise TrackerReportError("tracker identifier is required")
    env = environment if environment is not None else os.environ
    scope = tracker_scope_from_submit_input(submit_input)
    required = ("tenant_id", "user_id", "workspace_id")
    if any(not str(scope.get(key) or "").strip() for key in required):
        raise TrackerReportError("tracker owner scope is missing from submit_job.input")

    base = str(env.get("TCODE_TRACKER_API_BASE", "http://127.0.0.1:8080") or "").rstrip("/")
    url = (
        f"{base}/api/tracker/tasks/by-identifier/"
        f"{quote(issue_identifier, safe='')}/report-execution"
    )
    body = {
        "status": str(status or "").strip().lower(),
        "result_summary": str(result_summary or ""),
        "error_summary": str(error_summary or ""),
        "workspace_patch": dict(workspace_patch or {}),
        "metadata_patch": dict(metadata_patch or {}),
        "job_ref": dict(job_ref or {}),
        "source": str(source or "symphony_adapter").strip(),
    }
    headers = {
        "Content-Type": "application/json",
        "X-TCode-Tenant-ID": str(scope["tenant_id"]),
        "X-TCode-User-ID": str(scope["user_id"]),
        "X-TCode-Workspace-ID": str(scope["workspace_id"]),
    }
    token = read_tracker_token(env)
    if token:
        headers["X-Tracker-Token"] = token
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        auth_hint = ""
        if exc.code == 401:
            auth_hint = (
                " Tracker authentication failed; ensure Web and Symphony use "
                "the same TCODE_TRACKER_API_TOKEN or TCODE_TRACKER_API_TOKEN_FILE."
            )
        raise TrackerReportError(
            f"tracker report rejected ({exc.code}): {detail or exc.reason}{auth_hint}",
            status_code=exc.code,
        ) from exc
    except (OSError, URLError) as exc:
        raise TrackerReportError(f"tracker report unavailable: {exc}") from exc
    try:
        decoded = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise TrackerReportError("tracker report returned invalid JSON") from exc
    return decoded if isinstance(decoded, dict) else {}
