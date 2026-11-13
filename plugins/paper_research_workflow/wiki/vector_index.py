from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from tcode_agent.memory.document_index import DocumentChunkIndex, DocumentSource
from tcode_agent.memory.retriever import (
    build_embedding_with_version,
    build_embeddings_with_version,
)
from tcode_agent.memory.vector_store import FaissVectorStore


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PaperWikiVectorIndex:
    """Chunk-level semantic index for canonical Wiki pages.

    The public state/search contract remains page-oriented for compatibility,
    while every vector and search citation now points at a bounded source chunk.
    """

    def __init__(self, *, index_root: Path, workdir: Path):
        self.index_root = Path(index_root)
        self.workdir = Path(workdir)
        self.base_path = self.index_root / "pages"
        self.state_path = self.index_root / "pages.state.json"
        self.chunk_index = DocumentChunkIndex(
            root=self.index_root / "page-chunks",
            workdir=self.workdir,
            collection_id="paper-wiki-pages",
            chunk_size=1600,
            chunk_overlap=200,
            embedding_builder=lambda texts, *, workdir: build_embeddings_with_version(
                texts,
                workdir=workdir,
            ),
            query_embedding_builder=lambda text, *, workdir: build_embedding_with_version(
                text,
                workdir=workdir,
            ),
            vector_store=FaissVectorStore,
            vector_available=lambda: self.available(),
        )

    def available(self) -> bool:
        return FaissVectorStore.available()

    def state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def write_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload or {})
        _atomic_json(self.state_path, state)
        return state

    def requires_rebuild(self) -> bool:
        state = self.state()
        if state.get("status") != "ready":
            return False
        chunk_state = self.chunk_index.state()
        return (
            not isinstance(state.get("document_index"), dict)
            or chunk_state.get("status") != "ready"
        )

    def rebuild(self, items: list[dict[str, Any]], *, force_embeddings: bool = False) -> dict[str, Any]:
        documents = [
            DocumentSource(
                document_id=str(item["page_id"]),
                source_type="wiki_page",
                source_id=str(item["page_id"]),
                title=str(item.get("title") or item["page_id"]),
                content=str(item.get("search_text") or ""),
                format="markdown",
                revision=str(item.get("revision") or ""),
                immutable=False,
                metadata={
                    "page_id": str(item["page_id"]),
                    "content_hash": str(item.get("content_hash") or ""),
                },
            )
            for item in items
        ]
        result = self.chunk_index.sync_documents(documents, force_embeddings=force_embeddings)
        vector = dict(result.get("vector") or {})
        if not documents:
            status = "empty"
        elif vector.get("status") == "unavailable":
            status = "unavailable"
        elif vector.get("status") in {"ready", "empty"}:
            status = "ready"
        else:
            status = str(vector.get("status") or "error")
        state = {
            "status": status,
            "count": len(documents),
            "chunk_count": int(result.get("chunk_count") or 0),
            "embedding_version": str(vector.get("embedding_version") or ""),
            "built_at": str(result.get("built_at") or ""),
            "embedded_chunk_count": int(result.get("embedded_chunk_count") or 0),
            "content_hashes": {
                str(item["page_id"]): str(item.get("content_hash") or "") for item in items
            },
            "document_index": result,
        }
        _atomic_json(self.state_path, state)
        return state

    def search(self, query: str, *, limit: int) -> dict[str, Any]:
        state = self.state()
        if state.get("status") != "ready":
            return {"status": "missing", "hits": [], "state": state}
        result = self.chunk_index.search(
            str(query or ""),
            limit=max(1, int(limit)) * 3,
            candidate_limit=max(50, int(limit) * 8),
            lexical_weight=0.0,
            vector_weight=1.0,
            neighbor_window=1,
        )
        vector = dict(result.get("vector") or {})
        if vector.get("query_status") == "version_mismatch":
            return {
                "status": "version_mismatch",
                "hits": [],
                "state": state,
                "query_embedding_version": vector.get("query_embedding_version"),
            }
        if vector.get("query_status") != "ready":
            return {"status": "missing", "hits": [], "state": state}

        pages: dict[str, dict[str, Any]] = {}
        for hit in result.get("hits") or []:
            page_id = str(hit.get("document_id") or hit.get("source_id") or "")
            if not page_id:
                continue
            raw_score = max(-1.0, min(1.0, float(hit.get("vector_score") or 0.0)))
            chunk_score = (raw_score + 1.0) / 2.0
            chunk = {
                "chunk_id": hit.get("chunk_id"),
                "heading_path": hit.get("heading_path"),
                "content": hit.get("content"),
                "line_start": hit.get("line_start"),
                "line_end": hit.get("line_end"),
                "byte_start": hit.get("byte_start"),
                "byte_end": hit.get("byte_end"),
                "score": chunk_score,
            }
            # Preserve the historical page-level contract: Store converts the
            # raw FAISS cosine score from [-1, 1] to [0, 1].
            current = pages.setdefault(
                page_id,
                {"page_id": page_id, "score": raw_score, "chunks": []},
            )
            current["score"] = max(float(current["score"]), raw_score)
            if len(current["chunks"]) < 3:
                current["chunks"].append(chunk)
        hits = sorted(pages.values(), key=lambda item: (-float(item["score"]), item["page_id"]))
        return {"status": "ready", "hits": hits[: max(1, int(limit))], "state": state}
