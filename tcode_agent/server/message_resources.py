"""Workspace file references, attachments and message resource context."""

import re
import zipfile
from pathlib import Path
from typing import Any, Optional

from ..runtime.resource_import import (
    attachment_kind as _attachment_kind,
    container_path_for_workspace_relative as _container_path_for_workspace_relative,
    resolve_resource_source as _resolve_resource_source,
    stage_resource_for_workspace as _stage_resource_for_workspace,
    workspace_relative_path as _workspace_relative_path,
)
from . import workdirs as workdir_utils
from .state import ServerState
from .workspace_files import is_user_visible_workspace_file


def _extract_attachment_paths(message: str) -> list[str]:
    text = str(message or "")
    if not text:
        return []
    pattern = re.compile(
        r"^\s*(?:#{1,6}\s*)?File:\s*(?P<name>.+?)\s*\n\s*Path:\s*(?P<path>[^\n]+)(?:\n|$)",
        re.M,
    )
    paths: list[str] = []
    for match in pattern.finditer(text):
        raw_path = str(match.group("path") or "").strip()
        if raw_path:
            paths.append(raw_path)
    return paths


_MAX_MESSAGE_FILE_REFS = 32
_PAPER_LIBRARY_REF_RE = re.compile(r"\[\[paper-library:(?P<doc_id>[A-Za-z0-9_.:-]{1,160})\]\]")


def _extract_paper_library_refs(message: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    refs: list[dict[str, str]] = []
    for match in _PAPER_LIBRARY_REF_RE.finditer(str(message or "")):
        doc_id = str(match.group("doc_id") or "").strip()
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            refs.append({"doc_id": doc_id, "retrieval": "paper_library_rag"})
    return refs[:16]


def _extract_structured_file_ref_paths(file_refs: Any) -> list[str]:
    """Normalize the explicit workspace-file contract used by the composer."""

    if file_refs in (None, []):
        return []
    if not isinstance(file_refs, list):
        raise ValueError("file_refs must be a list")
    if len(file_refs) > _MAX_MESSAGE_FILE_REFS:
        raise ValueError(
            f"at most {_MAX_MESSAGE_FILE_REFS} workspace files may be referenced"
        )

    paths: list[str] = []
    seen: set[str] = set()
    for item in file_refs:
        if not isinstance(item, dict):
            raise ValueError("each file_refs item must be an object")
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise ValueError("each file_refs item requires path")
        if len(raw_path) > 2048:
            raise ValueError("workspace file reference path is too long")
        if raw_path in seen:
            continue
        seen.add(raw_path)
        paths.append(raw_path)
    return paths


def _normalize_attachment_path(path_text: str, *, workdir: str = "") -> Optional[Path]:
    raw = str(path_text or "").strip()
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
        root = Path(workdir or ServerState.get_workdir()).resolve()
        try:
            resolved.relative_to(root)
        except Exception:
            return None
        if not resolved.exists():
            return None
        return resolved
    except Exception:
        return None


def _resolve_existing_attachment_path(
    path_text: str,
    *,
    workdir: str = "",
) -> Optional[Path]:
    """Resolve host and ``/workspace`` attachment paths consistently.

    Uploaded attachments are rendered in chat with sandbox-style paths.  They
    must be translated at the message boundary; otherwise delegated tasks lose
    the upload from their resource context before isolation is even created.
    """

    raw = str(path_text or "").strip()
    if not raw:
        return None
    try:
        resolved = _resolve_resource_source(
            raw,
            workdir or ServerState.get_workdir(),
        )
        if not resolved.exists():
            return None
        return resolved
    except Exception:
        return None


def _resolve_workspace_file_reference(
    path_text: str,
    *,
    workdir: str,
) -> Path:
    """Resolve a structured reference and require it to stay in its workspace."""

    if not str(workdir or "").strip():
        raise ValueError("workspace file reference requires a session workdir")
    path = _resolve_existing_attachment_path(path_text, workdir=workdir)
    if path is None or not path.is_file():
        raise ValueError(f"workspace file reference does not exist: {path_text}")
    workspace_root = Path(workdir).expanduser().resolve(strict=False)
    try:
        path.resolve(strict=True).relative_to(workspace_root)
    except Exception as exc:
        raise ValueError(
            f"workspace file reference escapes current workspace: {path_text}"
        ) from exc
    server_workdir = ServerState.get_workdir()
    workspace_context = workdir_utils.workspace_context_from_managed_path(
        server_workdir,
        workspace_root,
    )
    if not is_user_visible_workspace_file(
        path,
        workspace_root=workspace_root,
        server_workdir=server_workdir,
        context=workspace_context,
    ):
        raise ValueError(
            f"workspace file reference is not user-visible: {path_text}"
        )
    return path


def _stage_attachment_for_workspace(
    source: Path,
    *,
    session_id: str = "",
    workdir: str = "",
) -> dict[str, Any]:
    return _stage_resource_for_workspace(
        source,
        session_id=session_id,
        workdir=workdir or ServerState.get_workdir(),
    )


def _count_pdfs_in_directory(path: Path, limit: int = 512) -> int:
    count = 0
    try:
        for item in path.rglob("*"):
            if item.is_file() and item.suffix.lower() == ".pdf":
                count += 1
                if count >= limit:
                    break
    except Exception:
        return count
    return count


def _count_pdfs_in_zip(path: Path, limit: int = 512) -> int:
    count = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if str(name or "").lower().endswith(".pdf"):
                    count += 1
                    if count >= limit:
                        break
    except Exception:
        return count
    return count


def _inspect_local_paper_inputs(
    message: str,
    *,
    attachment_items: Optional[list[dict[str, Any]]] = None,
    workdir: str = "",
) -> dict[str, Any]:
    attachment_paths = _extract_attachment_paths(message)
    inputs: list[dict[str, Any]] = []
    pdf_count = 0
    requires_ingest = False

    items = list(attachment_items or [])
    if not items:
        for raw_path in attachment_paths:
            path = _normalize_attachment_path(raw_path, workdir=workdir)
            if path is not None:
                items.append({"path": path.as_posix(), "kind": _attachment_kind(path)})

    for item in items:
        if not isinstance(item, dict):
            continue
        path = _resolve_existing_attachment_path(
            str(item.get("path") or ""),
            workdir=workdir,
        )
        if path is None:
            continue
        kind = "other"
        found = 0
        if path.is_file() and path.suffix.lower() == ".pdf":
            kind = "pdf"
            found = 1
        elif path.is_file() and path.suffix.lower() == ".zip":
            kind = "zip"
            requires_ingest = True
            found = _count_pdfs_in_zip(path)
        elif path.is_dir():
            kind = "directory"
            requires_ingest = True
            found = _count_pdfs_in_directory(path)

        pdf_count += found
        rel = str(item.get("relative_path") or "").strip()
        if not rel and workdir:
            rel = _workspace_relative_path(path, Path(workdir).resolve())
        inputs.append(
            {
                "path": path.as_posix(),
                "source_path": str(item.get("source_path") or "").strip(),
                "relative_path": rel,
                "container_path": str(
                    item.get("container_path")
                    or (_container_path_for_workspace_relative(rel) if rel else "")
                ).strip(),
                "kind": kind,
                "pdf_count": found,
                "available_in_sandbox": bool(item.get("available_in_sandbox", False) or rel),
                "imported": bool(item.get("imported", False)),
            }
        )

    return {
        "input_count": len(inputs),
        "pdf_count": pdf_count,
        "requires_ingest": requires_ingest,
        "inputs": inputs,
    }


def _build_message_resource_context(
    message: str,
    *,
    session_id: str = "",
    workdir: str = "",
    file_refs: Any = None,
) -> dict[str, Any]:
    attachment_paths = _extract_attachment_paths(message)
    uploaded_files: list[dict[str, Any]] = []
    staged_paths: set[str] = set()

    for raw_path in attachment_paths:
        path = _resolve_existing_attachment_path(raw_path, workdir=workdir)
        if path is None:
            continue
        canonical = path.resolve(strict=False).as_posix()
        if canonical in staged_paths:
            continue
        staged_paths.add(canonical)
        uploaded_files.append(
            _stage_attachment_for_workspace(
                path,
                session_id=session_id,
                workdir=workdir,
            )
        )

    for raw_path in _extract_structured_file_ref_paths(file_refs):
        path = _resolve_workspace_file_reference(raw_path, workdir=workdir)
        canonical = path.resolve(strict=False).as_posix()
        if canonical in staged_paths:
            continue
        staged_paths.add(canonical)
        uploaded_files.append(
            _stage_attachment_for_workspace(
                path,
                session_id=session_id,
                workdir=workdir,
            )
        )

    payload: dict[str, Any] = {}
    paper_library_refs = _extract_paper_library_refs(message)
    if paper_library_refs:
        payload["paper_library_refs"] = paper_library_refs
    if uploaded_files:
        payload["uploaded_files"] = uploaded_files

    paper_inputs = _inspect_local_paper_inputs(
        message,
        attachment_items=[
            item for item in uploaded_files if bool(item.get("available_in_sandbox", False))
        ],
        workdir=workdir,
    )
    if int(paper_inputs.get("input_count", 0) or 0) > 0:
        payload["paper_inputs"] = paper_inputs

    return payload
