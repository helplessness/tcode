"""FAISS-backed local vector store for memory retrieval."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
from collections import OrderedDict
from heapq import nlargest
from pathlib import Path
from typing import Any


def _import_faiss():
    import faiss  # type: ignore
    import numpy as np  # type: ignore

    return faiss, np


class FaissVectorStore:
    _cache: "OrderedDict[str, tuple[tuple[int, int, int, int], Any, list[str], dict[str, Any]]]" = OrderedDict()
    _cache_lock = threading.RLock()

    @classmethod
    def _index_path(cls, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss")

    @classmethod
    def _meta_path(cls, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss.meta.json")

    @classmethod
    def write(
        cls,
        *,
        base_path: Path,
        ids: list[str],
        vectors: list[list[float]],
        embedding_version: str = "",
        content_digest: str = "",
        record_digests: list[str] | None = None,
        configured_embedding_version: str = "",
    ) -> None:
        if len(ids) != len(vectors):
            raise ValueError("FAISS ids and vectors must have the same length")
        if record_digests is not None and len(record_digests) != len(ids):
            raise ValueError("FAISS record digests and ids must have the same length")
        cls.write_batches(base_path=base_path, count=len(ids), embedding_version=embedding_version,
                          content_digest=content_digest, configured_embedding_version=configured_embedding_version,
                          batches=((ids[offset:offset + 512], vectors[offset:offset + 512], (record_digests or [])[offset:offset + 512]) for offset in range(0, len(ids), 512)))

    @classmethod
    def write_batches(cls, *, base_path: Path, count: int, batches, embedding_version: str = '',
                      content_digest: str = '', configured_embedding_version: str = '') -> None:
        """Build in bounded vector batches, retaining only IDs/digests plus FAISS."""
        if not count:
            cls.remove(base_path)
            return
        faiss, np = _import_faiss()
        try:
            approximate_threshold = max(
                1_000,
                int(os.environ.get("TCODE_FAISS_HNSW_THRESHOLD", "10000")),
            )
        except Exception:
            approximate_threshold = 10_000
        index = None
        ids, record_digests = [], []
        index_type = 'flat_ip'
        dim = 0
        for batch_ids, vectors, digests in batches:
            if len(batch_ids) != len(vectors) or digests and len(digests) != len(batch_ids):
                raise ValueError('Invalid FAISS batch')
            if not vectors:
                continue
            if index is None:
                dim = len(vectors[0])
                if count >= approximate_threshold:
                    try:
                        index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
                        index.hnsw.efConstruction = 80
                        index.hnsw.efSearch = 64
                        index_type = 'hnsw_ip'
                    except Exception:
                        index = faiss.IndexFlatIP(dim)
                else:
                    index = faiss.IndexFlatIP(dim)
            if dim <= 0 or any(len(vector) != dim for vector in vectors):
                raise ValueError('FAISS vectors must have equal non-zero dimensions')
            arr = np.asarray(vectors, dtype='float32')
            if not np.isfinite(arr).all():
                raise ValueError('FAISS vectors must be finite')
            faiss.normalize_L2(arr)
            index.add(arr)
            ids.extend(batch_ids)
            record_digests.extend(digests)
        if len(ids) != count or index is None:
            raise ValueError('FAISS batch count does not match the snapshot')
        cls._persist(base_path, index, {
            "schema_version": 4, "ids": ids, "dim": dim, "index_type": index_type,
            "embedding_version": str(embedding_version or ""),
            "configured_embedding_version": configured_embedding_version,
            "content_digest": str(content_digest or ""), "record_digests": list(record_digests or []),
        })

    @classmethod
    def _persist(cls, base_path: Path, index, metadata: dict[str, Any]) -> None:
        faiss, _ = _import_faiss()
        with cls._cache_lock:
            index_path = cls._index_path(base_path)
            meta_path = cls._meta_path(base_path)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_index = tempfile.mkstemp(
                prefix=f".{index_path.name}.", dir=index_path.parent
            )
            os.close(descriptor)
            descriptor, temporary_meta = tempfile.mkstemp(
                prefix=f".{meta_path.name}.", dir=meta_path.parent
            )
            os.close(descriptor)
            try:
                faiss.write_index(index, temporary_index)
                metadata = {**metadata, "index_sha256": cls._file_digest(Path(temporary_index))}
                Path(temporary_meta).write_text(
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary_index, index_path)
                os.replace(temporary_meta, meta_path)
            finally:
                for temporary in (temporary_index, temporary_meta):
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            cls._cache.pop(base_path.expanduser().resolve(strict=False).as_posix(), None)

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def update(cls, *, base_path: Path, ids: list[str], changed_vectors: dict[str, list[float]],
               embedding_version: str) -> str:
        """复用未变化索引；Flat 支持删除/替换，HNSW 支持纯追加。"""
        if len(set(ids)) != len(ids):
            raise ValueError("FAISS ids must be unique")
        with cls._cache_lock:
            metadata = cls.metadata(base_path)
            if not cls.exists(base_path) or metadata.get("embedding_version") != embedding_version:
                return "rebuild_required"
            try:
                index, current_ids = cls._load_cached(base_path)
            except (OSError, ValueError, RuntimeError):
                return "rebuild_required"
            if index is None:
                return "rebuild_required"
            wanted, current = set(ids), set(current_ids)
            changed = wanted.intersection(changed_vectors)
            if wanted == current and not changed:
                return "reused"
            # 新 ID 必须有对应的新向量，不能复用其他位置的向量。
            if not wanted.difference(current).issubset(changed):
                return "rebuild_required"
            removed = [i for i, identifier in enumerate(current_ids) if identifier not in wanted or identifier in changed]
            if removed and metadata.get("index_type") != "flat_ip":
                return "rebuild_required"  # FAISS HNSW 不支持 remove_ids。
            faiss, np = _import_faiss()
            candidate = faiss.clone_index(index)  # 不修改正在被检索使用的缓存对象。
            removed_set = set(removed)
            remaining = [identifier for i, identifier in enumerate(current_ids) if i not in removed_set]
            if removed:
                candidate.remove_ids(np.asarray(removed, dtype="int64"))
            added = [identifier for identifier in ids if identifier in changed]
            if added:
                vectors = [changed_vectors[identifier] for identifier in added]
                if any(len(vector) != candidate.d for vector in vectors):
                    raise ValueError("FAISS incremental vector dimension mismatch")
                arr = np.asarray(vectors, dtype="float32")
                if not np.isfinite(arr).all():
                    raise ValueError("FAISS incremental vectors must be finite")
                faiss.normalize_L2(arr)
                candidate.add(arr)
            next_ids = remaining + added
            if candidate.ntotal != len(next_ids) or set(next_ids) != wanted:
                raise ValueError("FAISS incremental ID mapping mismatch")
            cls._persist(base_path, candidate, {
                "schema_version": 4, "ids": next_ids, "dim": candidate.d,
                "index_type": metadata["index_type"], "embedding_version": embedding_version,
            })
            return "incremental"

    @classmethod
    def _load_cached(cls, base_path: Path):
        index_path = cls._index_path(base_path)
        meta_path = cls._meta_path(base_path)
        cache_key = base_path.expanduser().resolve(strict=False).as_posix()
        with cls._cache_lock:
            signature = (
                index_path.stat().st_mtime_ns,
                index_path.stat().st_size,
                meta_path.stat().st_mtime_ns,
                meta_path.stat().st_size,
            )
            cached = cls._cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                cls._cache.move_to_end(cache_key)
                return cached[1], cached[2]
            faiss, _ = _import_faiss()
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ids = meta.get("ids", [])
            if not isinstance(ids, list) or not ids:
                return None, []
            if meta.get("index_sha256") and cls._file_digest(index_path) != meta["index_sha256"]:
                raise ValueError("FAISS index and ID metadata belong to different generations")
            index = faiss.read_index(str(index_path))
            if index.ntotal != len(ids) or len(set(ids)) != len(ids) or index.d != int(meta.get("dim") or 0):
                raise ValueError("FAISS index and ID metadata mismatch")
            try:
                max_cache_bytes = max(
                    0,
                    int(os.environ.get("TCODE_FAISS_CACHE_MAX_INDEX_BYTES", str(128 * 1024 * 1024))),
                )
            except Exception:
                max_cache_bytes = 128 * 1024 * 1024
            try:
                max_cache_entries = max(
                    0,
                    int(os.environ.get("TCODE_FAISS_CACHE_MAX_ENTRIES", "2")),
                )
            except Exception:
                max_cache_entries = 2
            if max_cache_entries and index_path.stat().st_size <= max_cache_bytes:
                cls._cache[cache_key] = (signature, index, [str(value) for value in ids], meta)
                cls._cache.move_to_end(cache_key)
                while len(cls._cache) > max_cache_entries:
                    cls._cache.popitem(last=False)
            return index, [str(value) for value in ids]

    @classmethod
    def search(
        cls,
        *,
        base_path: Path,
        query_vector: list[float],
        limit: int,
        expected_embedding_version: str = "",
        allowed_ids: set[str] | None = None,
        expected_digests: dict[str, str] | None = None,
    ) -> list[tuple[str, float]]:
        index_path = cls._index_path(base_path)
        meta_path = cls._meta_path(base_path)
        if not index_path.exists() or not meta_path.exists():
            return []
        if expected_embedding_version:
            metadata = cls.metadata(base_path)
            if (
                str(metadata.get("embedding_version") or "")
                != str(expected_embedding_version)
            ):
                return []

        faiss, np = _import_faiss()
        index, ids = cls._load_cached(base_path)
        if index is None or not ids:
            return []
        q = np.array([query_vector], dtype="float32")
        if q.shape[1] != index.d:
            return []
        if not np.isfinite(q).all():
            raise ValueError('FAISS query vector must be finite')
        faiss.normalize_L2(q)
        if allowed_ids is not None or expected_digests is not None:
            digests = cls.metadata(base_path).get("record_digests", [])
            positions = [i for i, identifier in enumerate(ids) if
                         (allowed_ids is None or identifier in allowed_ids) and
                         (expected_digests is None or i < len(digests) and expected_digests.get(identifier) == digests[i])]
            if not positions:
                return []
            if len(positions) < len(ids):
                eligible = set(positions)
                # Overfetch ANN candidates first. A selective filter falls back to
                # exact subset scoring, not an empty global-top-k post-filter.
                count = min(len(ids), max(limit * 4, 64))
                if len(positions) > 2048:
                    while True:
                        scores, found = index.search(q, count)
                        hits = [(ids[int(p)], float(s)) for s, p in zip(scores[0], found[0]) if int(p) in eligible]
                        if len(hits) >= limit or count >= min(len(ids), 2048):
                            if len(hits) >= limit:
                                return hits[:limit]
                            break
                        count = min(len(ids), count * 2, 2048)
                best: list[tuple[str, float]] = []
                for offset in range(0, len(positions), 512):
                    selected = positions[offset:offset + 512]
                    vectors = np.asarray([index.reconstruct(i) for i in selected], dtype="float32")
                    values = vectors @ q[0]
                    best = nlargest(max(1, limit), [*best, *((ids[i], float(s)) for i, s in zip(selected, values))], key=lambda hit: hit[1])
                return best
        scores, positions = index.search(q, max(1, limit))
        results: list[tuple[str, float]] = []
        for score, pos in zip(scores[0], positions[0]):
            if int(pos) < 0 or int(pos) >= len(ids):
                continue
            results.append((str(ids[int(pos)]), float(score)))
        return results

    @classmethod
    def read_vectors(cls, base_path: Path) -> tuple[list[str], list[list[float]]]:
        """Reconstruct stored vectors so unchanged records can be reused."""

        index, ids = cls._load_cached(base_path)
        if index is None or not ids:
            return [], []
        vectors: list[list[float]] = []
        for position in range(len(ids)):
            vector = index.reconstruct(position)
            vectors.append([float(value) for value in vector])
        return ids, vectors

    @classmethod
    def metadata(cls, base_path: Path) -> dict[str, Any]:
        """Read bounded sidecar metadata without loading the FAISS index."""

        path = cls._meta_path(base_path)
        if not path.exists():
            return {}
        key = base_path.expanduser().resolve(strict=False).as_posix()
        with cls._cache_lock:
            cached = cls._cache.get(key)
            index_path = cls._index_path(base_path)
            if cached and index_path.exists() and cached[0] == (index_path.stat().st_mtime_ns, index_path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_size):
                return cached[3]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def record_digest_map(cls, base_path: Path) -> dict[str, str]:
        metadata = cls.metadata(base_path)
        if '_digest_map' not in metadata:
            metadata['_digest_map'] = dict(zip(metadata.get('ids', []), metadata.get('record_digests', [])))
        return metadata['_digest_map']

    @classmethod
    def exists(cls, base_path: Path) -> bool:
        return cls._index_path(base_path).exists() and cls._meta_path(base_path).exists()

    @classmethod
    def remove(cls, base_path: Path) -> None:
        """Remove one index and invalidate its in-process cache."""

        for path in (cls._index_path(base_path), cls._meta_path(base_path)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        key = base_path.expanduser().resolve(strict=False).as_posix()
        with cls._cache_lock:
            cls._cache.pop(key, None)

    @classmethod
    def available(cls) -> bool:
        try:
            _import_faiss()
            return True
        except Exception:
            return False
