"""Routing headers for the official OpenCode Go inference endpoints."""

from hashlib import sha256
from urllib.parse import urlsplit
from uuid import uuid4


def opencode_go_headers(base_url: str | None, session_id: str | None = None) -> dict[str, str]:
    endpoint = urlsplit(base_url or "")
    path = endpoint.path.rstrip("/")
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "opencode.ai"
        or not (path == "/zen/go/v1" or path.startswith("/inference/go/"))
    ):
        return {}
    identity = sha256((session_id or uuid4().hex).encode()).hexdigest()
    return {"User-Agent": "TCode/1.0", "x-opencode-session": f"tcode-{identity}"}
