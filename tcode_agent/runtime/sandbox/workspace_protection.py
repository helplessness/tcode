from __future__ import annotations

import os
import posixpath
from pathlib import PurePosixPath


DEFAULT_HIDDEN_WORKSPACE_PATHS = (
    ".tcode",
    ".tcode-local",
    ".env",
    "config/local.env",
    "config/sandbox_production.env",
    "config/mcp_host.env",
)

DEFAULT_READ_ONLY_WORKSPACE_PATHS = (
    ".git",
    "tcode.json",
    "plugins.json",
    "mcp.json",
    "sandbox_runner_tool_policy.json",
    "mcp_job_protocol_only.json",
)


def normalize_workspace_relative_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError("workspace protection path cannot be empty")
    if PurePosixPath(raw).is_absolute():
        raise ValueError("workspace protection path must be relative")

    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("workspace protection path must stay inside the workspace")
    return normalized


def _configured_paths(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    values = defaults if raw is None or not raw.strip() else tuple(raw.split(","))
    normalized = {
        normalize_workspace_relative_path(value)
        for value in values
        if str(value or "").strip()
    }
    return tuple(sorted(normalized))


def hidden_workspace_paths() -> tuple[str, ...]:
    return _configured_paths(
        "TCODE_SANDBOX_WORKSPACE_HIDDEN_PATHS",
        DEFAULT_HIDDEN_WORKSPACE_PATHS,
    )


def read_only_workspace_paths() -> tuple[str, ...]:
    hidden = set(hidden_workspace_paths())
    return tuple(
        path
        for path in _configured_paths(
            "TCODE_SANDBOX_WORKSPACE_READ_ONLY_PATHS",
            DEFAULT_READ_ONLY_WORKSPACE_PATHS,
        )
        if path not in hidden
    )


def workspace_path_matches(relative_path: str, roots: tuple[str, ...]) -> str:
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    for root in roots:
        if normalized == root or normalized.startswith(f"{root}/"):
            return root
    return ""


def workspace_protection_contract() -> dict[str, tuple[str, ...]]:
    return {
        "hidden": hidden_workspace_paths(),
        "read_only": read_only_workspace_paths(),
    }
