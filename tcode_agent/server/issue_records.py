"""Pure Symphony issue identity, scope and timestamp helpers."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import workdirs as workdir_utils
from .job_workspace import _build_issue_workspace_summary


def _extract_submit_input_from_description(description: str) -> dict[str, Any]:
    marker = "submit_job.input:"
    idx = description.find(marker)
    if idx < 0:
        return {}
    text = description[idx + len(marker) :]
    start = text.find("{")
    if start < 0:
        return {}
    text = text[start:]
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return {}
    return {}


def _normalize_issue_state_text(state: Any) -> str:
    return str(state or "").strip().lower()


def _is_terminal_issue_state(state: Any) -> bool:
    return _normalize_issue_state_text(state) in {
        "done",
        "closed",
        "failed",
        "cancelled",
        "canceled",
        "duplicate",
    }


def _extract_issue_session_candidates(issue: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def _append(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        candidates.append(text)

    workspace = issue.get("workspace")
    if isinstance(workspace, dict):
        _append(workspace.get("session_id"))
        _append(workspace.get("parent_session_id"))

    resume_data = issue.get("resume_data")
    if isinstance(resume_data, dict):
        _append(resume_data.get("session_id"))
        _append(resume_data.get("parent_session_id"))

    description = str(issue.get("description", "") or "")
    for pattern in (
        r"^session_id:\s*(.+)$",
        r"^parent_session_id:\s*(.+)$",
    ):
        for match in re.finditer(pattern, description, re.MULTILINE):
            _append(match.group(1))

    return candidates


def _issue_matches_session(issue: dict[str, Any], session_id: str) -> bool:
    normalized = str(session_id or "").strip()
    if not normalized:
        return True
    candidates = [str(item or "").strip() for item in _extract_issue_session_candidates(issue)]
    if normalized in candidates:
        return True

    # 兼容 worker 会话命名（例如 protein-ec8c066a-0af-design）：
    # 当前端传父会话 UUID 时，允许按短 token 关联其 protein worker 子会话。
    short_token = normalized[:12].lower()
    if len(short_token) < 8:
        return False
    worker_prefix = f"protein-{short_token}-"
    for candidate in candidates:
        text = candidate.lower()
        if text.startswith(worker_prefix):
            return True
    return False


def _path_is_same_or_descendant(path_value: str, root_value: str) -> bool:
    path_text = str(path_value or "").strip()
    root_text = str(root_value or "").strip()
    if not path_text or not root_text:
        return False
    try:
        path = Path(path_text).expanduser().resolve()
        root = Path(root_text).expanduser().resolve()
        return path == root or root in path.parents
    except Exception:
        normalized_path = path_text.rstrip("/")
        normalized_root = root_text.rstrip("/")
        return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _issue_matches_workspace(issue: dict[str, Any], workspace_roots: list[str]) -> bool:
    roots = [str(item or "").strip() for item in workspace_roots if str(item or "").strip()]
    if not roots:
        return False

    workspace = issue.get("workspace")
    if not isinstance(workspace, dict):
        try:
            workspace = _build_issue_workspace_summary(issue)
        except Exception:
            workspace = {}

    candidate_paths: list[str] = []
    if isinstance(workspace, dict):
        for key in ("workdir", "output_root", "context_file", "log_file", "exitcode_file"):
            value = str(workspace.get(key, "") or "").strip()
            if value:
                candidate_paths.append(value)

        artifact_source = workspace.get("artifact_source")
        if isinstance(artifact_source, dict):
            value = str(artifact_source.get("root", "") or "").strip()
            if value:
                candidate_paths.append(value)

    if not candidate_paths:
        submit_input = _extract_submit_input_from_description(str(issue.get("description", "") or ""))
        value = str(submit_input.get("workdir", "") or "").strip()
        if value:
            candidate_paths.append(value)

    for candidate in candidate_paths:
        for root in roots:
            if _path_is_same_or_descendant(candidate, root):
                return True
    return False


def _issue_workspace_id(issue: dict[str, Any]) -> str:
    workspace = issue.get("workspace")
    if not isinstance(workspace, dict):
        try:
            workspace = _build_issue_workspace_summary(issue)
        except Exception:
            workspace = {}

    workspace_id = str(workspace.get("workspace_id", "") or "").strip()
    if workspace_id:
        return workspace_id

    workdir = str(workspace.get("workdir", "") or "").strip()
    if not workdir:
        return ""
    try:
        return Path(workdir).expanduser().resolve().name
    except Exception:
        return Path(workdir).name


def _issue_lookup_key(issue: dict[str, Any]) -> str:
    task_id = str(issue.get("id", "") or "").strip()
    if task_id:
        return f"id:{task_id}"
    identifier = str(issue.get("identifier", "") or "").strip()
    if identifier:
        return f"identifier:{identifier}"
    fingerprint = str(issue.get("fingerprint", "") or "").strip()
    if fingerprint:
        return f"fingerprint:{fingerprint}"
    return ""


def _issue_timestamp_value(issue: dict[str, Any]) -> float:
    for raw in (issue.get("updated_at"), issue.get("created_at")):
        text = str(raw or "").strip()
        if not text:
            continue
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except Exception:
            continue
    return 0.0


def _same_resolved_workdir(left: str, right: str) -> bool:
    return workdir_utils.same_resolved_path(str(left or "").strip(), str(right or "").strip())
