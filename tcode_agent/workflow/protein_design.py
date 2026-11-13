"""Approved protein design brief parsing helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


_BRAINSTORM_APPROVAL_RE = re.compile(
    r"^\s*status\s*:\s*BRAINSTORM APPROVED\s*$",
    flags=re.IGNORECASE,
)
_EXECUTION_MODE_RE = re.compile(
    r"^\s*execution_mode\s*:\s*(direct|agent_loop|workflow_loop)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def resolve_approved_protein_design_brief(
    *,
    workdir: Optional[str],
    brief_path: Optional[str],
) -> tuple[Optional[Path], str]:
    """Resolve a workspace-local brief and verify its approval marker."""

    root = Path(workdir or os.getcwd()).expanduser().resolve()
    raw_path = str(brief_path or "protein_design_brief.md").strip()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, "brief_path must stay inside the workflow workdir"
    if not candidate.is_file():
        return None, (
            f"approved protein design brief not found: {candidate}; "
            "the first line must be 'status: BRAINSTORM APPROVED'"
        )
    try:
        first_line = candidate.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError, UnicodeError):
        return None, f"unable to read approved protein design brief: {candidate}"
    if not _BRAINSTORM_APPROVAL_RE.fullmatch(first_line):
        return None, (
            "protein design loop requires the first line "
            "'status: BRAINSTORM APPROVED'"
        )
    return candidate, ""


def resolve_protein_design_execution_mode(
    *,
    workdir: Optional[str],
    brief_path: Optional[str] = "protein_design_brief.md",
) -> tuple[Optional[str], Optional[Path], str]:
    """Return the approved brief execution mode used for route recovery."""

    brief, error = resolve_approved_protein_design_brief(
        workdir=workdir,
        brief_path=brief_path,
    )
    if brief is None:
        return None, None, error
    try:
        content = brief.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, brief, f"unable to read approved protein design brief: {brief}"
    match = _EXECUTION_MODE_RE.search(content)
    if match is None:
        return None, brief, (
            "approved protein design brief requires "
            "'execution_mode: direct | agent_loop | workflow_loop'"
        )
    return match.group(1).lower(), brief, ""
