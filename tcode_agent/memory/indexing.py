"""Vector-sidecar helpers for JSON-backed semantic memory.

Memory JSON is the canonical text/metadata store. Embeddings are transient
runtime values and are persisted only in the FAISS sidecar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar

from .retriever import (
    build_embedding_with_version,
    build_embeddings_with_version,
    semantic_rank,
)
from .vector_store import FaissVectorStore
from .embedding_provider import EmbeddingProvider
from .retriever import HASH_EMBEDDING_VERSION


T = TypeVar("T")
_TRANSIENT_VECTOR_FIELDS = frozenset({"embedding", "embedding_version"})


def _content_digest(ids: list[str], texts: list[str]) -> str:
    payload = json.dumps(
        list(zip(ids, texts)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_digests(ids: list[str], texts: list[str]) -> list[str]:
    return [
        hashlib.sha256(f"{item_id}\0{text}".encode("utf-8")).hexdigest()
        for item_id, text in zip(ids, texts)
    ]


def persistent_memory_record(value: Any) -> dict[str, Any]:
    """Project a memory object to JSON without transient vector fields."""

    if is_dataclass(value):
        raw = asdict(value)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = dict(vars(value))
    for field in _TRANSIENT_VECTOR_FIELDS:
        raw.pop(field, None)
    return raw


def records_contain_embeddings(records: Iterable[Any]) -> bool:
    """Return whether a legacy JSON collection still embeds vectors."""

    return any(
        isinstance(item, Mapping)
        and any(field in item for field in _TRANSIENT_VECTOR_FIELDS)
        for item in records
    )


def hydrate_embeddings(
    entries: list[T],
    *,
    workdir: str | None,
    text_getter: Callable[[T], str],
) -> str:
    """Compute one compatible embedding batch and attach it transiently."""

    if not entries:
        return ""
    vectors, version = build_embeddings_with_version(
        [str(text_getter(item) or "") for item in entries],
        workdir=workdir,
    )
    for item, vector in zip(entries, vectors):
        setattr(item, "embedding", list(vector))
        if hasattr(item, "embedding_version"):
            setattr(item, "embedding_version", version)
    return version


def ensure_vector_index(
    entries: list[T],
    *,
    base_path: Path,
    workdir: str | None,
    id_getter: Callable[[T], str],
    text_getter: Callable[[T], str],
    force: bool = False,
) -> dict[str, Any]:
    """Create or repair a FAISS sidecar when it is missing or stale."""

    ids = [str(id_getter(item) or "") for item in entries]
    texts = [str(text_getter(item) or "") for item in entries]
    if not entries:
        try:
            FaissVectorStore.remove(base_path)
        except Exception:
            return {"status": "failed", "count": 0, "embedding_version": ""}
        return {"status": "empty", "count": 0, "embedding_version": ""}
    if not FaissVectorStore.available():
        return {"status": "unavailable", "count": len(entries), "embedding_version": ""}

    content_digest = _content_digest(ids, texts)
    record_digests = _record_digests(ids, texts)
    metadata = FaissVectorStore.metadata(base_path)
    configured_version = (
        EmbeddingProvider.version(workdir)
        if EmbeddingProvider.is_enabled(workdir)
        else HASH_EMBEDDING_VERSION
    )
    configuration_changed = metadata.get("configured_embedding_version") != configured_version
    if (
        not force
        and not configuration_changed
        and metadata.get("ids") == ids
        and metadata.get("content_digest") == content_digest
        and metadata.get("record_digests") == record_digests
        and str(metadata.get("embedding_version") or "").strip()
        and FaissVectorStore.exists(base_path)
    ):
        return {
            "status": "ready",
            "count": len(ids),
            "embedding_version": str(metadata.get("embedding_version") or ""),
        }

    vectors: list[list[float]] = [[] for _item in entries]
    version = ""
    reused = False
    old_version = str(metadata.get("embedding_version") or "").strip()
    old_ids = metadata.get("ids")
    old_record_digests = metadata.get("record_digests")
    if (
        not force
        and not configuration_changed
        and old_version
        and isinstance(old_ids, list)
        and isinstance(old_record_digests, list)
        and len(old_ids) == len(old_record_digests)
        and FaissVectorStore.exists(base_path)
    ):
        try:
            stored_ids, stored_vectors = FaissVectorStore.read_vectors(base_path)
            reusable = {
                (str(item_id), str(record_digest)): list(vector)
                for item_id, record_digest, vector in zip(
                    stored_ids,
                    old_record_digests,
                    stored_vectors,
                )
            }
            for index, key in enumerate(zip(ids, record_digests)):
                vector = reusable.get(key)
                if vector:
                    vectors[index] = vector
                    reused = True
            version = old_version
        except Exception:
            vectors = [[] for _item in entries]
            reused = False
            version = ""

    missing_indices = [index for index, vector in enumerate(vectors) if not vector]
    if missing_indices:
        missing_vectors, generated_version = build_embeddings_with_version(
            [texts[index] for index in missing_indices],
            workdir=workdir,
        )
        if reused and generated_version != old_version:
            # A provider/model switch makes every stored vector incompatible.
            vectors, version = build_embeddings_with_version(texts, workdir=workdir)
        else:
            for index, vector in zip(missing_indices, missing_vectors):
                vectors[index] = list(vector)
            version = generated_version
    elif not version:
        vectors, version = build_embeddings_with_version(texts, workdir=workdir)

    if reused and len({len(vector) for vector in vectors if vector}) > 1:
        vectors, version = build_embeddings_with_version(texts, workdir=workdir)

    for item, vector in zip(entries, vectors):
        setattr(item, "embedding", list(vector))
    if not vectors or any(not vector for vector in vectors):
        return {"status": "failed", "count": len(ids), "embedding_version": version}
    try:
        FaissVectorStore.write(
            base_path=base_path,
            ids=ids,
            vectors=vectors,
            embedding_version=version,
            content_digest=content_digest,
            record_digests=record_digests,
            configured_embedding_version=configured_version,
        )
    except Exception:
        return {"status": "failed", "count": len(ids), "embedding_version": version}
    return {"status": "rebuilt", "count": len(ids), "embedding_version": version}


def rank_with_vector_index(
    entries: list[T],
    *,
    query: str,
    base_path: Path,
    workdir: str | None,
    id_getter: Callable[[T], str],
    text_getter: Callable[[T], str],
) -> list[T]:
    """Rank with a compatible sidecar, falling back to on-demand vectors."""

    query_text = str(query or "").strip()
    if not query_text or not entries:
        return list(entries)
    if FaissVectorStore.available():
        try:
            query_vector, version = build_embedding_with_version(
                query_text,
                workdir=workdir,
            )
            metadata = FaissVectorStore.metadata(base_path)
            results = FaissVectorStore.search(
                base_path=base_path,
                query_vector=query_vector,
                limit=max(10, len(metadata.get("ids") or []), len(entries)),
                expected_embedding_version=version,
            )
            if results:
                by_id = {str(id_getter(item) or ""): item for item in entries}
                ranked = [
                    by_id[item_id]
                    for item_id, _score in results
                    if item_id in by_id
                ]
                if ranked:
                    ranked_ids = {str(id_getter(item) or "") for item in ranked}
                    ranked.extend(
                        item
                        for item in entries
                        if str(id_getter(item) or "") not in ranked_ids
                    )
                    return ranked
        except Exception:
            pass
    return semantic_rank(
        query=query_text,
        items=entries,
        text_getter=text_getter,
        embedding_getter=lambda item: getattr(item, "embedding", None),
        workdir=workdir,
    )


__all__ = [
    "ensure_vector_index",
    "hydrate_embeddings",
    "persistent_memory_record",
    "rank_with_vector_index",
    "records_contain_embeddings",
]
