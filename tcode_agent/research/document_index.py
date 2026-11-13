"""Chunk indexes for immutable ResearchPack parents and mutable overlays.

Parent and Overlay use independent SQLite/FAISS generations.  Updating a local
Overlay therefore cannot force the immutable Parent vectors to be loaded and
rewritten.  Chat retrieval uses :meth:`search_available`, which only reads a
ready generation and leaves refresh work to the background scheduler.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..memory.document_index import DocumentChunkIndex, DocumentSource
from ..memory.embedding_provider import EmbeddingProvider
from .config import ResearchSettings, load_research_settings
from .pack_asset_store import ResearchPackAssetStore


_LOGICAL_PATHS = {
    "research": "snapshot/researches.json",
    "checkpoint": "snapshot/checkpoints.jsonl",
    "finding": "snapshot/findings.jsonl",
    "activity": "timeline/activities.jsonl",
    "episode": "timeline/episodes.jsonl",
    "experience": "experience/items.jsonl",
    "conflict": "history/conflicts.jsonl",
    "resolution": "history/resolutions.jsonl",
    "knowledge_link": "history/knowledge-links.jsonl",
    "evidence": "evidence/records.jsonl",
    "event": "provenance/events.jsonl",
    "wiki_page": "knowledge/wiki/pages.jsonl",
    "artifact": "artifacts/manifest.json",
    "paper": "papers/manifest.json",
}
_PACK_LOCKS: dict[str, threading.RLock] = {}
_PACK_LOCKS_GUARD = threading.Lock()


def _pack_lock(path: Path) -> threading.RLock:
    key = path.expanduser().resolve(strict=False).as_posix()
    with _PACK_LOCKS_GUARD:
        return _PACK_LOCKS.setdefault(key, threading.RLock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _title(payload: dict[str, Any], object_type: str, fallback: str) -> str:
    for key in (
        "title",
        "statement",
        "summary",
        "name",
        "label",
        "current_state",
        "research_goal",
        "description",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:240]
    return f"{object_type or 'object'} · {fallback}"[:240]


def _document_content(title: str, object_type: str, payload: dict[str, Any]) -> str:
    body = dict(payload)
    markdown = ""
    for key in ("content", "statement", "summary", "description", "current_state"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            markdown = value.strip()
            if key == "content":
                body.pop(key, None)
            break
    sections = [f"# {title}", "", f"对象类型：`{object_type}`"]
    if markdown:
        sections.extend(["", "## 内容", "", markdown])
    if body:
        sections.extend(
            [
                "",
                "## 结构化字段",
                "",
                "```json",
                json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2),
                "```",
            ]
        )
    return "\n".join(sections).strip() + "\n"


class ResearchPackDocumentIndex:
    """Owner-scoped split index for one imported ResearchPack asset."""

    MAX_OBJECTS_PER_LAYER = 10_000
    LAYOUT = "parent-overlay-v1"

    def __init__(
        self,
        *,
        asset_id: str,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str,
        settings: ResearchSettings | None = None,
    ):
        self.asset_id = str(asset_id or "").strip()
        if not self.asset_id:
            raise ValueError("asset_id is required")
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if not all(self.owner.values()):
            raise ValueError("complete owner context is required")
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.settings = settings or load_research_settings()
        key = hashlib.sha256(self.asset_id.encode("utf-8")).hexdigest()[:32]
        self.root = self.workdir / ".tcode" / "research" / "pack-indexes" / key
        self.state_path = self.root / "pack-state.json"
        self._lock = _pack_lock(self.root)
        # Legacy combined index remains readable during a rolling migration.
        self.legacy_index = DocumentChunkIndex(
            root=self.root,
            workdir=self.workdir,
            collection_id=f"researchpack:{self.asset_id}",
            chunk_size=1800,
            chunk_overlap=220,
        )
        self.index = self.legacy_index
        self.parent_index = DocumentChunkIndex(
            root=self.root / "parent",
            workdir=self.workdir,
            collection_id=f"researchpack:{self.asset_id}:parent",
            chunk_size=1800,
            chunk_overlap=220,
        )
        self.overlay_index = DocumentChunkIndex(
            root=self.root / "overlay",
            workdir=self.workdir,
            collection_id=f"researchpack:{self.asset_id}:overlay",
            chunk_size=1800,
            chunk_overlap=220,
        )

    def state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        legacy = self.legacy_index.state()
        if legacy.get("status") == "ready":
            return {**legacy, "asset_id": self.asset_id, "layout": "legacy-combined"}
        parent = self.parent_index.state()
        overlay = self.overlay_index.state()
        if parent.get("status") == "ready" or overlay.get("status") == "ready":
            return self._aggregate_state(parent, overlay, write=False)
        return {
            "status": "missing",
            "asset_id": self.asset_id,
            "layout": self.LAYOUT,
            "layers": {"parent": parent, "overlay": overlay},
        }

    def _require_asset(self):
        asset = ResearchPackAssetStore.get_asset(
            self.asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if asset is None:
            raise ValueError("ResearchPack asset not found")
        return asset

    def _fingerprints(self) -> dict[str, str]:
        asset = self._require_asset()
        overlays = ResearchPackAssetStore.list_overlays(
            self.asset_id,
            **self.owner,
            status="",
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        parent = _digest(
            {
                "asset_id": self.asset_id,
                "source_digest": asset.source_digest,
                "archive_sha256": asset.archive_sha256,
            }
        )
        overlay = _digest(
            [
                {
                    "overlay_id": item.overlay_id,
                    "status": item.status,
                    "source_digest": item.source_digest,
                    "updated_at": item.updated_at,
                }
                for item in overlays
            ]
        )
        return {
            "parent": parent,
            "overlay": overlay,
            "combined": _digest({"parent": parent, "overlay": overlay}),
        }

    def _parent_sources(self) -> tuple[list[DocumentSource], dict[str, int | bool]]:
        self._require_asset()
        counts = ResearchPackAssetStore.object_counts(
            self.asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        objects = ResearchPackAssetStore.list_objects(
            self.asset_id,
            **self.owner,
            limit=self.MAX_OBJECTS_PER_LAYER,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        sources: list[DocumentSource] = []
        for item in objects:
            title = _title(item.payload, item.object_type, item.source_object_id)
            sources.append(
                DocumentSource(
                    document_id=f"parent:{item.object_id}",
                    source_type="researchpack_object",
                    source_id=item.source_object_id,
                    title=title,
                    content=_document_content(title, item.object_type, item.payload),
                    format="markdown",
                    revision=item.content_hash,
                    immutable=True,
                    metadata={
                        "asset_id": self.asset_id,
                        "layer": "parent",
                        "object_id": item.object_id,
                        "object_type": item.object_type,
                        "source_object_id": item.source_object_id,
                        "source_research_ids": item.source_research_ids,
                        "distribution": item.distribution,
                        "logical_path": _LOGICAL_PATHS.get(item.object_type, "manifest.json"),
                        "content_trust": "untrusted_data",
                    },
                )
            )
        expected = sum(int(value or 0) for value in counts.values())
        return sources, {
            "expected_objects": expected,
            "loaded_objects": len(objects),
            "complete": expected == len(objects),
        }

    def _overlay_sources(self) -> tuple[list[DocumentSource], dict[str, int | bool]]:
        self._require_asset()
        counts = ResearchPackAssetStore.overlay_object_counts(
            self.asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        objects = ResearchPackAssetStore.list_overlay_objects(
            self.asset_id,
            **self.owner,
            limit=self.MAX_OBJECTS_PER_LAYER,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        sources: list[DocumentSource] = []
        for item in objects:
            title = _title(item.payload, item.object_type, item.local_object_id)
            source_research_ids = list(
                dict.fromkeys(
                    str(ref.get("source_research_id") or "")
                    for ref in item.imported_scope_refs
                    if str(ref.get("source_research_id") or "")
                )
            )
            sources.append(
                DocumentSource(
                    document_id=f"overlay:{item.overlay_object_id}",
                    source_type="researchpack_overlay",
                    source_id=item.local_object_id,
                    title=title,
                    content=_document_content(title, item.object_type, item.payload),
                    format="markdown",
                    revision=str(item.revision),
                    immutable=False,
                    metadata={
                        "asset_id": self.asset_id,
                        "layer": "overlay",
                        "object_id": item.overlay_object_id,
                        "object_type": item.object_type,
                        "local_object_id": item.local_object_id,
                        "source_research_ids": source_research_ids,
                        "logical_path": "overlay/local-objects.jsonl",
                        "content_trust": "untrusted_data",
                    },
                )
            )
        expected = sum(int(value or 0) for value in counts.values())
        return sources, {
            "expected_objects": expected,
            "loaded_objects": len(objects),
            "complete": expected == len(objects),
        }

    @staticmethod
    def _layer_completeness(state: dict[str, Any]) -> dict[str, Any]:
        value = state.get("completeness")
        return dict(value) if isinstance(value, dict) else {}

    def _aggregate_state(
        self,
        parent: dict[str, Any],
        overlay: dict[str, Any],
        *,
        fingerprints: dict[str, str] | None = None,
        write: bool,
    ) -> dict[str, Any]:
        parent_complete = self._layer_completeness(parent)
        overlay_complete = self._layer_completeness(overlay)
        ready = parent.get("status") == "ready" and overlay.get("status") == "ready"
        completeness = {
            "expected_parent_objects": int(parent_complete.get("expected_objects") or 0),
            "loaded_parent_objects": int(parent_complete.get("loaded_objects") or 0),
            "expected_overlay_objects": int(overlay_complete.get("expected_objects") or 0),
            "loaded_overlay_objects": int(overlay_complete.get("loaded_objects") or 0),
            "complete": bool(parent_complete.get("complete", False))
            and bool(overlay_complete.get("complete", False)),
        }
        value = {
            "schema_version": 2,
            "status": "ready" if ready else "partial",
            "asset_id": self.asset_id,
            "layout": self.LAYOUT,
            "source_fingerprint": str((fingerprints or {}).get("combined") or ""),
            "fingerprints": dict(fingerprints or {}),
            "document_count": int(parent.get("document_count") or 0)
            + int(overlay.get("document_count") or 0),
            "chunk_count": int(parent.get("chunk_count") or 0)
            + int(overlay.get("chunk_count") or 0),
            "changed_document_count": int(parent.get("changed_document_count") or 0)
            + int(overlay.get("changed_document_count") or 0),
            "removed_document_count": int(parent.get("removed_document_count") or 0)
            + int(overlay.get("removed_document_count") or 0),
            "embedded_chunk_count": int(parent.get("embedded_chunk_count") or 0)
            + int(overlay.get("embedded_chunk_count") or 0),
            "fts_available": bool(parent.get("fts_available", False))
            and bool(overlay.get("fts_available", False)),
            "vector": {
                "status": "layered",
                "parent": dict(parent.get("vector") or {}),
                "overlay": dict(overlay.get("vector") or {}),
            },
            "completeness": completeness,
            "layers": {"parent": parent, "overlay": overlay},
            "built_at": max(
                str(parent.get("built_at") or ""),
                str(overlay.get("built_at") or ""),
            )
            or _now(),
        }
        if write:
            _atomic_json(self.state_path, value)
        return value

    def _sync_layer(
        self,
        layer: str,
        fingerprint: str,
        *,
        force_embeddings: bool,
    ) -> dict[str, Any]:
        if layer == "parent":
            index = self.parent_index
            sources, completeness = self._parent_sources()
        else:
            index = self.overlay_index
            sources, completeness = self._overlay_sources()
        result = index.sync_documents(sources, force_embeddings=force_embeddings)
        return index._write_state(
            {
                **result,
                "asset_id": self.asset_id,
                "layer": layer,
                "source_fingerprint": fingerprint,
                "completeness": completeness,
            }
        )

    def rebuild(self, *, force_embeddings: bool = False) -> dict[str, Any]:
        with self._lock:
            fingerprints = self._fingerprints()
            parent = self._sync_layer(
                "parent",
                fingerprints["parent"],
                force_embeddings=force_embeddings,
            )
            overlay = self._sync_layer(
                "overlay",
                fingerprints["overlay"],
                force_embeddings=force_embeddings,
            )
            return self._aggregate_state(
                parent,
                overlay,
                fingerprints=fingerprints,
                write=True,
            )

    def ensure(self) -> dict[str, Any]:
        with self._lock:
            fingerprints = self._fingerprints()
            parent = self.parent_index.state()
            overlay = self.overlay_index.state()
            if (
                parent.get("status") != "ready"
                or parent.get("source_fingerprint") != fingerprints["parent"]
            ):
                parent = self._sync_layer(
                    "parent",
                    fingerprints["parent"],
                    force_embeddings=False,
                )
            if (
                overlay.get("status") != "ready"
                or overlay.get("source_fingerprint") != fingerprints["overlay"]
            ):
                overlay = self._sync_layer(
                    "overlay",
                    fingerprints["overlay"],
                    force_embeddings=False,
                )
            return self._aggregate_state(
                parent,
                overlay,
                fingerprints=fingerprints,
                write=True,
            )

    def _allowed_documents(
        self,
        index: DocumentChunkIndex,
        source_research_id: str,
    ) -> set[str] | None:
        source_id = str(source_research_id or "").strip()
        if not source_id:
            return None
        connection = index._connect()
        try:
            rows = connection.execute(
                "SELECT document_id,metadata_json FROM documents"
            ).fetchall()
        finally:
            connection.close()
        return {
            str(row["document_id"])
            for row in rows
            if source_id
            in list(
                json.loads(str(row["metadata_json"] or "{}")).get(
                    "source_research_ids"
                )
                or []
            )
        }

    def _search_indexes(
        self,
        query: str,
        *,
        source_research_id: str,
        limit: int,
        indexes: list[tuple[str, DocumentChunkIndex]],
        vector_query_policy: str = "configured",
    ) -> dict[str, Any]:
        combined: list[dict[str, Any]] = []
        modes: set[str] = set()
        vectors: list[dict[str, Any]] = []
        timings: list[dict[str, Any]] = []
        per_layer_limit = min(100, max(8, int(limit or 20) * 3))
        for layer, index in indexes:
            started = time.perf_counter()
            result = index.search(
                query,
                limit=per_layer_limit,
                candidate_limit=max(100, per_layer_limit * 10),
                allowed_document_ids=self._allowed_documents(index, source_research_id),
                neighbor_window=1,
                vector_query_policy=vector_query_policy,
            )
            modes.add(str(result.get("search_mode") or "fts"))
            vectors.append(dict(result.get("vector") or {}))
            timings.append(
                {
                    "layer": layer,
                    "query_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            for rank, hit in enumerate(list(result.get("hits") or []), start=1):
                item = dict(hit)
                item["_layer"] = layer
                item["_cross_score"] = float(item.get("score") or 0.0) + 1.0 / (60 + rank)
                combined.append(item)
        combined.sort(
            key=lambda item: (
                -float(item.get("_cross_score") or 0.0),
                str(item.get("chunk_id") or ""),
            )
        )
        # Keep object diversity before allowing a second chunk from one object.
        diverse: list[dict[str, Any]] = []
        remainder: list[dict[str, Any]] = []
        seen_documents: set[str] = set()
        for hit in combined:
            document_id = str(hit.get("document_id") or "")
            if document_id and document_id not in seen_documents:
                seen_documents.add(document_id)
                diverse.append(hit)
            else:
                remainder.append(hit)
        selected = (diverse + remainder)[: max(1, min(int(limit or 20), 100))]
        return {
            "hits": selected,
            "search_mode": (
                "hybrid" if "hybrid" in modes else "vector" if "vector" in modes else "fts"
            ),
            "vector": {"layers": vectors},
            "query_ms": round(sum(item["query_ms"] for item in timings), 2),
            "timings": timings,
        }

    def search(
        self,
        query: str,
        *,
        source_research_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        state = self.ensure()
        return self._search_ready(
            query,
            source_research_id=source_research_id,
            limit=limit,
            index_state=state,
            index_stale=False,
            indexes=[("parent", self.parent_index), ("overlay", self.overlay_index)],
            vector_query_policy="configured",
        )

    def search_available(
        self,
        query: str,
        *,
        source_research_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search ready generations without rebuilding on the chat path."""

        text = str(query or "").strip()
        if not text:
            raise ValueError("query is required")
        state = self.state()
        indexes: list[tuple[str, DocumentChunkIndex]] = []
        if state.get("layout") == "legacy-combined" and state.get("status") == "ready":
            indexes = [("combined", self.legacy_index)]
        else:
            if self.parent_index.state().get("status") == "ready":
                indexes.append(("parent", self.parent_index))
            if self.overlay_index.state().get("status") == "ready":
                indexes.append(("overlay", self.overlay_index))
        stale = True
        try:
            stale = state.get("source_fingerprint") != self._fingerprints()["combined"]
        except Exception:
            stale = True
        if not indexes:
            return self._empty_result(text, source_research_id, state, index_stale=True)
        return self._search_ready(
            text,
            source_research_id=source_research_id,
            limit=limit,
            index_state=state,
            index_stale=stale,
            indexes=indexes,
            vector_query_policy="configured" if EmbeddingProvider.strategy() == "model_first" else "local_only",
        )

    def _empty_result(
        self,
        query: str,
        source_research_id: str,
        state: dict[str, Any],
        *,
        index_stale: bool,
    ) -> dict[str, Any]:
        return {
            "schema": "tcode.research-read.pack-search.v2",
            "query": query,
            "asset_id": self.asset_id,
            "source_research_id": str(source_research_id or "").strip(),
            "hits": [],
            "items": [],
            "loaded_count": 0,
            "total_count": 0,
            "total": 0,
            "page_complete": True,
            "next_cursor": "",
            "search_mode": "unavailable",
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
            "index": state,
            "index_stale": bool(index_stale),
        }

    def _search_ready(
        self,
        query: str,
        *,
        source_research_id: str,
        limit: int,
        index_state: dict[str, Any],
        index_stale: bool,
        indexes: list[tuple[str, DocumentChunkIndex]],
        vector_query_policy: str,
    ) -> dict[str, Any]:
        result = self._search_indexes(
            query,
            source_research_id=source_research_id,
            limit=limit,
            indexes=indexes,
            vector_query_policy=vector_query_policy,
        )
        hits: list[dict[str, Any]] = []
        for raw in list(result.get("hits") or []):
            hit = dict(raw)
            hit.pop("_cross_score", None)
            layer_hint = str(hit.pop("_layer", "") or "")
            metadata = dict(hit.get("metadata") or {})
            neighbors = [
                {**neighbor, "content": str(neighbor.get("content") or "")[:400]}
                for neighbor in list(hit.get("neighbors") or [])
            ]
            hits.append(
                {
                    **hit,
                    "content": str(hit.get("content") or "")[:1600],
                    "neighbors": neighbors,
                    "asset_id": self.asset_id,
                    "layer": metadata.get("layer", "parent" if layer_hint == "combined" else layer_hint),
                    "object_id": metadata.get("object_id", ""),
                    "object_type": metadata.get("object_type", ""),
                    "source_object_id": metadata.get("source_object_id", ""),
                    "local_object_id": metadata.get("local_object_id", ""),
                    "source_research_ids": metadata.get("source_research_ids", []),
                    "logical_path": metadata.get("logical_path", ""),
                    "content_trust": "untrusted_data",
                    "automatic_execution_allowed": False,
                }
            )
        items = [
            {
                "layer": hit["layer"],
                "immutable": hit["immutable"],
                "object_id": hit["object_id"],
                "object_type": hit["object_type"],
                "source_object_id": hit["source_object_id"],
                "local_object_id": hit["local_object_id"],
                "source_research_ids": hit["source_research_ids"],
                "chunk_id": hit["chunk_id"],
                "heading_path": hit["heading_path"],
                "line_start": hit["line_start"],
                "line_end": hit["line_end"],
                "logical_path": hit["logical_path"],
                "score": hit["score"],
                "payload": {"excerpt": hit["content"][:300]},
                "matched_terms": [],
            }
            for hit in hits
        ]
        return {
            **result,
            "schema": "tcode.research-read.pack-search.v2",
            "query": str(query or "").strip(),
            "asset_id": self.asset_id,
            "source_research_id": str(source_research_id or "").strip(),
            "hits": hits,
            "items": items,
            "loaded_count": len(items),
            "total_count": len(items),
            "total": len(hits),
            "page_complete": True,
            "next_cursor": "",
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
            "index": index_state,
            "index_stale": bool(index_stale),
        }


__all__ = ["ResearchPackDocumentIndex"]
