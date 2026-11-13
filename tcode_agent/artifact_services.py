"""Trusted Artifact service routing; references contain IDs, never URLs or secrets."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

_SERVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _mapping(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    try:
        value = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def artifact_service(service_id: str = "", *, target_server: str = "",
                     default_url: str = "", default_token: str = "", default_service_id: str = "") -> tuple[str, str, dict[str, str]]:
    """Resolve a configured endpoint and its own credentials, failing closed."""
    services = _mapping("TMCP_ARTIFACT_SERVICES")
    routes = _mapping("TMCP_ARTIFACT_TARGET_SERVICES")
    default_id = os.environ.get("TMCP_ARTIFACT_SERVICE_ID", default_service_id).strip()
    if not isinstance(service_id, str):
        raise ValueError("Artifact service_id must be a string")
    routed = routes.get(target_server, "")
    if not isinstance(routed, str):
        raise ValueError('Artifact target route must be a service ID')
    selected = service_id or routed or default_id
    if not isinstance(selected, str) or (selected and not _SERVICE_ID.fullmatch(selected)):
        raise ValueError("Artifact service_id is invalid")
    if default_id and not _SERVICE_ID.fullmatch(default_id):
        raise ValueError("TMCP_ARTIFACT_SERVICE_ID is invalid")
    if selected in services:
        entry = services[selected]
        if not isinstance(entry, dict):
            raise ValueError(f"Artifact service '{selected}' must be an object")
        url = entry.get("url", "")
        token_name = entry.get("token_env", "")
        if not isinstance(token_name, str) or (token_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_name)):
            raise ValueError(f"Artifact service '{selected}' token_env is invalid")
        token = os.environ.get(token_name, "") if token_name else ""
        if token_name and not token:
            raise ValueError(f"Artifact service '{selected}' credential is not configured")
    elif selected and selected != default_id:
        raise ValueError(f"Unknown Artifact service_id: {selected}")
    else:
        url = default_url
        token = default_token
    if not isinstance(url, str):
        raise ValueError("Artifact service URL must be a string")
    try:
        parsed = urlsplit(url)
        parsed.port
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"Artifact service '{selected or 'default'}' URL is invalid")
    return selected, url.rstrip("/"), {"X-Artifact-Token": token} if token else {}


def bind_artifact_source(payload: dict, service_id: str) -> dict:
    """Stamp old servers' references without accepting conflicting identities."""
    if not isinstance(payload, dict):
        raise ValueError('Artifact service returned a non-object response')
    ref = payload.get('ref')
    if not isinstance(ref, dict) and service_id:
        if payload.get('bundle_id'):
            ref = {'kind': 'artifact_bundle', 'bundle_id': payload['bundle_id']}
        elif payload.get('artifact_id'):
            ref = {'kind': 'artifact', 'artifact_id': payload['artifact_id']}
    if isinstance(ref, dict) and service_id:
        if ref.get('service_id') and ref['service_id'] != service_id:
            raise ValueError('Artifact service identity mismatch')
        payload = {**payload, 'ref': {**ref, 'service_id': service_id}}
    return payload

