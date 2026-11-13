"""Read-only browser API for the workspace-scoped Paper Library."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ._shared import ServerState, workdir_utils
from .wiki_service import _paper_wiki_plugin_config
from .workspace_scope import _resolve_user_workdir_path, _workspace_context_from_request


router = APIRouter()
_DOC_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class _PaperLibraryReader:
    """Read library manifests without importing the PDF ingestion runtime."""

    def __init__(self, library_root: Path):
        self.library_root = library_root.resolve(strict=False)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_registry(self) -> dict[str, Any]:
        return self._read_json(self.library_root / "registry.json")

    def get_record(self, doc_id: str, *, sync_evidence: bool = False) -> dict[str, Any]:
        del sync_evidence
        entry = next(
            (
                item for item in self._load_registry().get("documents", [])
                if isinstance(item, dict) and str(item.get("doc_id") or "") == doc_id
            ),
            None,
        )
        if entry is None:
            raise FileNotFoundError(doc_id)
        raw_path = str((entry.get("artifacts") or {}).get("record_path") or entry.get("record_path") or "")
        path = Path(raw_path).expanduser() if raw_path else self.library_root / "docs" / doc_id / "record.json"
        if not path.is_absolute():
            path = self.library_root / path
        path = path.resolve(strict=False)
        try:
            path.relative_to(self.library_root)
        except ValueError as exc:
            raise FileNotFoundError(doc_id) from exc
        record = self._read_json(path)
        if not record:
            raise FileNotFoundError(doc_id)
        return record


def _store_for_request(request: Request, workspace_id: str):
    context = _workspace_context_from_request(request, workspace_id=workspace_id)
    workdir_utils.assert_workspace_active(ServerState.get_workdir(), context)
    workdir = _resolve_user_workdir_path(context.workspace_id, context).resolve()
    config = _paper_wiki_plugin_config(workdir)
    store = _PaperLibraryReader(
        workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
    )
    return store, workdir, bool(config.get("__plugin_enabled__", False))


def _safe_doc_id(doc_id: str) -> str:
    value = str(doc_id or "").strip()
    if not _DOC_ID.fullmatch(value):
        raise HTTPException(400, "Invalid paper document id")
    return value


def _relative_source_path(raw_path: str, workdir: Path) -> str:
    if not raw_path:
        return ""
    try:
        return Path(raw_path).expanduser().resolve(strict=False).relative_to(workdir).as_posix()
    except (OSError, ValueError):
        return ""


def _list_documents(store: Any, workdir: Path, query: str, offset: int, limit: int) -> dict[str, Any]:
    registry = store._load_registry()
    needle = str(query or "").strip().casefold()
    documents: list[dict[str, Any]] = []
    for raw in registry.get("documents", []):
        if not isinstance(raw, dict) or str(raw.get("status") or "ready").lower() != "ready":
            continue
        doc_id = str(raw.get("doc_id") or "")
        try:
            record = store.get_record(doc_id, sync_evidence=False) if doc_id else {}
        except (FileNotFoundError, ValueError):
            record = {}
        metadata = dict(record.get("metadata") or {})
        summary = dict(record.get("l0_summary") or raw.get("l0_summary_index") or {})
        identifiers = dict(metadata.get("identifiers") or raw.get("identifiers") or {})
        authors = metadata.get("authors") or metadata.get("author") or []
        if isinstance(authors, str):
            authors = [authors]
        item = {
            "doc_id": doc_id,
            "title": str(metadata.get("title") or raw.get("title") or doc_id),
            "authors": [str(value) for value in authors if str(value).strip()][:30],
            "year": metadata.get("year") or metadata.get("publication_year") or "",
            "journal": str(metadata.get("journal") or metadata.get("venue") or ""),
            "identifiers": identifiers,
            "summary": str(summary.get("summary_text") or ""),
            "keywords": [str(value) for value in list(summary.get("keywords") or [])[:30]],
            "page_span": list(summary.get("page_span") or raw.get("page_span") or []),
            "passage_count": int(raw.get("passage_count") or len(record.get("l1_passage_summaries") or [])),
            "fact_count": sum(int(value or 0) for value in dict(raw.get("fact_counts") or {}).values()),
            "visual_counts": dict(raw.get("visual_counts") or {}),
            "source_path": _relative_source_path(str(raw.get("pdf_path") or record.get("pdf_path") or ""), workdir),
            "pdf_available": Path(str(raw.get("pdf_path") or record.get("pdf_path") or "")).expanduser().is_file(),
            "reference_only": bool(record.get("imported_reference_only")),
            "updated_at": str(raw.get("updated_at") or record.get("updated_at") or ""),
        }
        haystack = " ".join([
            item["title"], " ".join(item["authors"]), item["journal"], item["summary"],
            " ".join(item["keywords"]), " ".join(str(value) for value in identifiers.values()),
        ]).casefold()
        if not needle or all(token in haystack for token in needle.split()):
            documents.append(item)
    documents.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"items": documents[offset:offset + limit], "total": len(documents), "offset": offset, "limit": limit}


@router.get("/api/paper-library")
async def list_paper_library(
    request: Request,
    workspace_id: str = Query(default=""),
    q: str = Query(default="", max_length=300),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    store, workdir, enabled = _store_for_request(request, workspace_id)
    result = await asyncio.to_thread(_list_documents, store, workdir, q, offset, limit)
    return {**result, "enabled": enabled, "workspace_id": workspace_id}


@router.get("/api/paper-library/{doc_id}")
async def get_paper_library_document(doc_id: str, request: Request, workspace_id: str = Query(default="")):
    store, workdir, enabled = _store_for_request(request, workspace_id)
    try:
        record = await asyncio.to_thread(store.get_record, _safe_doc_id(doc_id), sync_evidence=False)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Paper was not found") from exc
    metadata = dict(record.get("metadata") or {})
    summary = dict(record.get("l0_summary") or {})
    raw_path = str(record.get("pdf_path") or "")
    return {
        "enabled": enabled,
        "document": {
            "doc_id": str(record.get("doc_id") or doc_id),
            "title": str(metadata.get("title") or record.get("title") or doc_id),
            "metadata": metadata,
            "summary": summary,
            "passages": list(record.get("l1_passage_summaries") or [])[:100],
            "facts": list(record.get("l2_facts") or [])[:200],
            "available_layers": list(record.get("available_layers") or []),
            "missing_layers": list(record.get("missing_layers") or []),
            "source_path": _relative_source_path(raw_path, workdir),
            "pdf_available": Path(raw_path).expanduser().is_file() if raw_path else False,
            "reference_only": bool(record.get("imported_reference_only")),
            "updated_at": str(record.get("updated_at") or ""),
        },
    }


@router.get("/api/paper-library/{doc_id}/pdf")
async def get_paper_library_pdf(doc_id: str, request: Request, workspace_id: str = Query(default="")):
    store, workdir, _enabled = _store_for_request(request, workspace_id)
    try:
        record = await asyncio.to_thread(store.get_record, _safe_doc_id(doc_id), sync_evidence=False)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Paper was not found") from exc
    path = Path(str(record.get("pdf_path") or "")).expanduser().resolve(strict=False)
    library_root = Path(store.library_root).resolve(strict=False)
    try:
        path.relative_to(workdir)
    except ValueError:
        try:
            path.relative_to(library_root)
        except ValueError as exc:
            raise HTTPException(403, "Paper PDF is outside the workspace") from exc
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "Paper PDF is unavailable")
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")
