"""Portable locations shared with the sibling TMCP service."""

from __future__ import annotations

import os
import json
import urllib.request


def tmcp_job_protocol_api_base() -> str:
    return str(
        os.environ.get("TMCP_JOB_PROTOCOL_API_BASE", "http://127.0.0.1:10121")
        or "http://127.0.0.1:10121"
    ).rstrip("/")


def _job_protocol_api_token() -> str:
    return str(
        os.environ.get("TMCP_JOB_PROTOCOL_API_TOKEN")
        or os.environ.get("TMCP_SERVICE_API_TOKEN")
        or ""
    ).strip()


def load_tmcp_job_registry(timeout_seconds: float = 5.0) -> dict:
    """Read broker state through HTTP; never open its SQLite from the Agent host."""
    headers = {"Accept": "application/json"}
    token = _job_protocol_api_token()
    if token:
        headers["X-TMCP-Service-Token"] = token
    request = urllib.request.Request(
        f"{tmcp_job_protocol_api_base()}/api/v1/jobs", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
