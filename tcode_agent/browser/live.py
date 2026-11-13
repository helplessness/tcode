"""Filesystem contract for a sandboxed live browser session.

The browser worker runs inside the persistent browser sandbox while the TCode
web process owns the authenticated viewer connection.  Both sides exchange
only files below the current workspace; no CDP or VNC port is exposed.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SESSION_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_browser_session_id(value: str) -> str:
    raw = str(value or "").strip() or "default"
    normalized = _SESSION_ID_PATTERN.sub("-", raw)[:80] or "default"
    return "default" if normalized in {".", ".."} else normalized


@dataclass(frozen=True)
class BrowserLivePaths:
    workspace: Path
    session_id: str
    root: Path
    requests: Path
    responses: Path
    state: Path
    frame: Path
    runtime: Path
    ready: Path
    observations: Path
    confirmation: Path


def browser_live_paths(workspace: str | Path, session_id: str) -> BrowserLivePaths:
    workspace_path = Path(workspace).expanduser().resolve(strict=False)
    normalized_session_id = safe_browser_session_id(session_id)
    root = workspace_path / ".tcode" / "browser" / "live" / normalized_session_id
    try:
        root.resolve(strict=False).relative_to(workspace_path)
    except ValueError as exc:  # pragma: no cover - guarded by normalization
        raise ValueError("browser live path escaped the workspace") from exc
    return BrowserLivePaths(
        workspace=workspace_path,
        session_id=normalized_session_id,
        root=root,
        requests=root / "requests",
        responses=root / "responses",
        state=root / "state.json",
        frame=root / "latest.jpg",
        runtime=root / "runtime.json",
        ready=root / "ready.json",
        observations=root / "observations",
        confirmation=root / "confirmation.json",
    )


def read_browser_live_state(
    workspace: str | Path,
    session_id: str,
) -> dict[str, Any]:
    paths = browser_live_paths(workspace, session_id)
    try:
        value = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def cleanup_browser_live_session(
    workspace: str | Path,
    session_id: str,
    *,
    terminal_status: str = "",
) -> bool:
    """Remove the live frame and redact sensitive page state for one session."""
    paths = browser_live_paths(workspace, session_id)
    changed = False
    for transient in (paths.frame, paths.confirmation):
        try:
            transient.unlink()
            changed = True
        except FileNotFoundError:
            pass
        except OSError:
            return changed
    if paths.observations.is_dir() and not paths.observations.is_symlink():
        for candidate in paths.observations.iterdir():
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                candidate.unlink()
                changed = True
            except FileNotFoundError:
                pass
            except OSError:
                continue
        try:
            paths.observations.rmdir()
        except OSError:
            pass

    state = read_browser_live_state(paths.workspace, paths.session_id)
    if not state:
        return changed
    sanitized = {
        **state,
        "url": "",
        "title": "",
        "refs": {},
        "error": "",
        "frame_error": "",
        "last_frame_at": "",
        "frame_available": False,
        "observation_screenshot_path": "",
        "observation_screenshot_available": False,
        "pending_confirmation": None,
        "control_mode": "agent",
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if terminal_status:
        sanitized["status"] = str(terminal_status).strip()
    paths.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.state.with_name(
        f".{paths.state.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, paths.state)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def cleanup_browser_live_workspace(workspace: str | Path) -> int:
    """Redact every live-browser session after its shared runtime stops."""
    workspace_path = Path(workspace).expanduser().resolve(strict=False)
    live_root = workspace_path / ".tcode" / "browser" / "live"
    if not live_root.is_dir():
        return 0
    cleaned = 0
    for candidate in live_root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            if cleanup_browser_live_session(
                workspace_path,
                candidate.name,
                terminal_status="stopped",
            ):
                cleaned += 1
        except (OSError, ValueError):
            continue
    return cleaned
