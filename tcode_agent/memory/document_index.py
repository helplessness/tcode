"""Persistent chunk index for large Markdown and structured research documents.

The index deliberately keeps lexical truth in SQLite and treats FAISS as a
rebuildable acceleration layer.  Document synchronization is deterministic:
unchanged chunks keep their IDs and embeddings even when earlier content moves.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from ..sqlite_runtime import connect as sqlite_connect
from ..atomic_storage import file_transaction

from .retriever import (
    TOKEN_RE,
    build_embedding_with_version,
    build_embeddings_with_version,
)
from .vector_store import FaissVectorStore
from .embedding_provider import EmbeddingProvider


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_SYNC_LOCKS: dict[str, threading.RLock] = {}
_SYNC_LOCKS_GUARD = threading.Lock()


def _sync_lock(path: Path) -> threading.RLock:
    key = path.expanduser().resolve(strict=False).as_posix()
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(key, threading.RLock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


@dataclass(frozen=True)
class DocumentSource:
    document_id: str
    source_type: str
    source_id: str
    title: str
    content: str
    format: str = "text"
    revision: str = ""
    immutable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return _sha256(self.content)


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True)
class _Block:
    content: str
    heading_path: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    atomic: bool = False


def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    if not lines:
        return [], []
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line.encode("utf-8"))
    return lines, offsets


def _block_from_lines(
    lines: Sequence[str],
    offsets: Sequence[int],
    start: int,
    end: int,
    *,
    heading_path: str,
    atomic: bool = False,
) -> _Block | None:
    content = "".join(lines[start : end + 1]).strip()
    if not content:
        return None
    last = lines[end]
    return _Block(
        content=content,
        heading_path=heading_path,
        line_start=start + 1,
        line_end=end + 1,
        byte_start=offsets[start],
        byte_end=offsets[end] + len(last.encode("utf-8")),
        atomic=atomic,
    )


def _markdown_blocks(text: str) -> list[_Block]:
    lines, offsets = _line_offsets(text)
    if not lines:
        return []
    headings: list[str] = []
    blocks: list[_Block] = []
    paragraph_start: int | None = None

    def heading_path() -> str:
        return " > ".join(value for value in headings if value)

    def flush_paragraph(end: int) -> None:
        nonlocal paragraph_start
        if paragraph_start is None:
            return
        block = _block_from_lines(
            lines,
            offsets,
            paragraph_start,
            max(paragraph_start, end),
            heading_path=heading_path(),
        )
        if block is not None:
            blocks.append(block)
        paragraph_start = None

    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\n")
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph(index - 1)
            level = len(heading.group(1))
            headings[level - 1 :] = [heading.group(2).strip()]
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph(index - 1)
            start = index
            marker = fence.group(1)[0]
            index += 1
            while index < len(lines):
                if lines[index].lstrip().startswith(marker * 3):
                    break
                index += 1
            end = min(index, len(lines) - 1)
            block = _block_from_lines(
                lines,
                offsets,
                start,
                end,
                heading_path=heading_path(),
                atomic=True,
            )
            if block is not None:
                blocks.append(block)
            index = end + 1
            continue

        if (
            "|" in line
            and index + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[index + 1].rstrip("\n"))
        ):
            flush_paragraph(index - 1)
            start = index
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                index += 1
            block = _block_from_lines(
                lines,
                offsets,
                start,
                index - 1,
                heading_path=heading_path(),
                atomic=True,
            )
            if block is not None:
                blocks.append(block)
            continue

        if not line.strip():
            flush_paragraph(index - 1)
        elif paragraph_start is None:
            paragraph_start = index
        index += 1
    flush_paragraph(len(lines) - 1)
    return blocks


def _plain_blocks(text: str) -> list[_Block]:
    lines, offsets = _line_offsets(text)
    if not lines:
        return []
    blocks: list[_Block] = []
    start: int | None = None
    for index, line in enumerate(lines):
        if not line.strip():
            if start is not None:
                block = _block_from_lines(lines, offsets, start, index - 1, heading_path="")
                if block is not None:
                    blocks.append(block)
                start = None
        elif start is None:
            start = index
    if start is not None:
        block = _block_from_lines(lines, offsets, start, len(lines) - 1, heading_path="")
        if block is not None:
            blocks.append(block)
    return blocks


def _split_large_block(block: _Block, chunk_size: int) -> list[_Block]:
    if len(block.content) <= chunk_size:
        return [block]
    lines = block.content.splitlines(keepends=True)
    if len(lines) <= 1:
        parts: list[_Block] = []
        for offset in range(0, len(block.content), chunk_size):
            content = block.content[offset : offset + chunk_size]
            byte_offset = len(block.content[:offset].encode("utf-8"))
            parts.append(
                _Block(
                    content=content,
                    heading_path=block.heading_path,
                    line_start=block.line_start,
                    line_end=block.line_end,
                    byte_start=block.byte_start + byte_offset,
                    byte_end=block.byte_start + byte_offset + len(content.encode("utf-8")),
                    atomic=block.atomic,
                )
            )
        return parts

    parts = []
    current: list[str] = []
    current_length = 0
    start_line = block.line_start
    line_cursor = block.line_start
    byte_cursor = block.byte_start
    part_byte_start = byte_cursor
    for line in lines:
        if current and current_length + len(line) > chunk_size:
            content = "".join(current).strip()
            parts.append(
                _Block(
                    content=content,
                    heading_path=block.heading_path,
                    line_start=start_line,
                    line_end=line_cursor - 1,
                    byte_start=part_byte_start,
                    byte_end=byte_cursor,
                    atomic=block.atomic,
                )
            )
            current = []
            current_length = 0
            start_line = line_cursor
            part_byte_start = byte_cursor
        current.append(line)
        current_length += len(line)
        byte_cursor += len(line.encode("utf-8"))
        line_cursor += 1
    if current:
        parts.append(
            _Block(
                content="".join(current).strip(),
                heading_path=block.heading_path,
                line_start=start_line,
                line_end=max(start_line, line_cursor - 1),
                byte_start=part_byte_start,
                byte_end=byte_cursor,
                atomic=block.atomic,
            )
        )
    return [part for part in parts if part.content]


def chunk_document(
    source: DocumentSource,
    *,
    chunk_size: int = 1600,
    chunk_overlap: int = 200,
) -> list[DocumentChunk]:
    """Split one document while preserving Markdown structure and source spans."""

    safe_size = max(256, min(int(chunk_size or 1600), 20_000))
    safe_overlap = max(0, min(int(chunk_overlap or 0), safe_size // 2))
    format_name = str(source.format or "text").strip().lower()
    raw_blocks = _markdown_blocks(source.content) if format_name in {"md", "markdown"} else _plain_blocks(source.content)
    blocks = [part for block in raw_blocks for part in _split_large_block(block, safe_size)]
    if not blocks and source.content.strip():
        blocks = _split_large_block(
            _Block(
                content=source.content.strip(),
                heading_path="",
                line_start=1,
                line_end=max(1, source.content.count("\n") + 1),
                byte_start=0,
                byte_end=len(source.content.encode("utf-8")),
            ),
            safe_size,
        )

    groups: list[list[_Block]] = []
    current: list[_Block] = []
    current_length = 0
    for block in blocks:
        addition = len(block.content) + (2 if current else 0)
        if current and current_length + addition > safe_size:
            groups.append(current)
            overlap: list[_Block] = []
            overlap_length = 0
            for previous in reversed(current):
                if overlap_length + len(previous.content) > safe_overlap:
                    break
                overlap.insert(0, previous)
                overlap_length += len(previous.content)
            current = overlap
            current_length = sum(len(item.content) + 2 for item in current)
        current.append(block)
        current_length += len(block.content) + (2 if len(current) > 1 else 0)
    if current:
        groups.append(current)

    occurrences: dict[str, int] = {}
    chunks: list[DocumentChunk] = []
    for ordinal, group in enumerate(groups):
        content = "\n\n".join(item.content for item in group).strip()
        headings = list(dict.fromkeys(item.heading_path for item in group if item.heading_path))
        heading = " | ".join(headings)
        content_hash = _sha256(content)
        identity_base = "\x00".join((source.document_id, heading, content_hash))
        occurrence = occurrences.get(identity_base, 0)
        occurrences[identity_base] = occurrence + 1
        chunk_id = f"chunk-{_sha256(f'{identity_base}\x00{occurrence}')[:32]}"
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                document_id=source.document_id,
                ordinal=ordinal,
                heading_path=heading,
                content=content,
                content_hash=content_hash,
                line_start=min(item.line_start for item in group),
                line_end=max(item.line_end for item in group),
                byte_start=min(item.byte_start for item in group),
                byte_end=max(item.byte_end for item in group),
            )
        )
    return chunks


class DocumentChunkIndex:
    """SQLite FTS5 + optional FAISS index for a bounded document collection."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        root: Path,
        workdir: Path,
        collection_id: str,
        chunk_size: int = 1600,
        chunk_overlap: int = 200,
        embedding_builder: Callable[..., tuple[list[list[float]], str]] = build_embeddings_with_version,
        query_embedding_builder: Callable[..., tuple[list[float], str]] = build_embedding_with_version,
        vector_store: type[FaissVectorStore] = FaissVectorStore,
        vector_available: Callable[[], bool] | None = None,
    ):
        self.root = Path(root)
        self.workdir = Path(workdir)
        self.collection_id = str(collection_id or "documents")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.db_path = self.root / "chunks.sqlite3"
        self.vector_base_path = self.root / "chunks"
        self.state_path = self.root / "state.json"
        self.embedding_builder = embedding_builder
        self.query_embedding_builder = query_embedding_builder
        self.vector_store = vector_store
        self.vector_available = vector_available or vector_store.available
        self._lock = _sync_lock(self.root)
        self._fts_available = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite_connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    format TEXT NOT NULL,
                    revision TEXT NOT NULL DEFAULT '',
                    immutable INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_document_source
                    ON documents(source_type, source_id);
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    heading_path TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    byte_start INTEGER NOT NULL,
                    byte_end INTEGER NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    embedding_version TEXT NOT NULL DEFAULT '',
                    UNIQUE(document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_document_ordinal
                    ON chunks(document_id, ordinal);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO index_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                    "chunk_id UNINDEXED, document_id UNINDEXED, title, heading_path, content)"
                )
                self._fts_available = True
            except sqlite3.DatabaseError:
                self._fts_available = False
            connection.commit()
        finally:
            connection.close()

    def state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {
                "status": "missing",
                "collection_id": self.collection_id,
                "fts_available": self._fts_available,
            }
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {"status": "invalid", "collection_id": self.collection_id}

    def _write_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = {
            "schema_version": self.SCHEMA_VERSION,
            "collection_id": self.collection_id,
            "fts_available": self._fts_available,
            **payload,
        }
        _atomic_json(self.state_path, state)
        return state

    def _existing_chunks(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        return {
            str(row["chunk_id"]): row
            for row in connection.execute("SELECT * FROM chunks").fetchall()
        }

    def _embed_changed(
        self,
        chunks: list[DocumentChunk],
        existing: dict[str, sqlite3.Row],
        *,
        force_embeddings: bool,
    ) -> tuple[dict[str, tuple[list[float], str]], int, dict[str, Any]]:
        if not self.vector_available():
            return {}, 0, {"status": "unavailable", "reason": "faiss_not_installed"}
        changed = [
            chunk
            for chunk in chunks
            if force_embeddings
            or chunk.chunk_id not in existing
            or not str(existing[chunk.chunk_id]["embedding_version"] or "")
            or not json.loads(str(existing[chunk.chunk_id]["embedding_json"] or "[]"))
        ]
        if not changed:
            return {}, 0, {"status": "unchanged"}
        texts = [chunk.content for chunk in changed]
        vectors, version = self.embedding_builder(texts, workdir=self.workdir.as_posix())
        if len(vectors) != len(changed):
            raise RuntimeError("document chunk embedding count mismatch")
        vectors = [EmbeddingProvider._parse_vector(vector) for vector in vectors]
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("Embedding batch dimensions must agree")
        stored_versions = {
            str(row["embedding_version"] or "")
            for row in existing.values()
            if str(row["embedding_version"] or "")
        }
        downgrade = str(version).startswith("hash-") and any(
            not stored.startswith("hash-") for stored in stored_versions
        )
        if stored_versions and stored_versions != {str(version)} and (not force_embeddings or downgrade):
            # A provider outage can make the embedding helper fall back to a
            # local hash model.  Mixing that version into an existing index (or
            # silently migrating every chunk) is both expensive and incorrect.
            # Keep the ready generation queryable and leave new chunks lexical-
            # only until an explicit force_embeddings rebuild authorizes the
            # migration.
            return {}, 0, {
                "status": "migration_required",
                "stored_versions": sorted(stored_versions),
                "candidate_version": str(version),
                "pending_chunk_count": len(changed),
            }
        if stored_versions == {str(version)}:
            old = next(row for row in existing.values() if str(row["embedding_version"] or "") == str(version))
            if len(json.loads(str(old["embedding_json"]))) != len(vectors[0]):
                raise ValueError("Embedding dimensions changed without a new model version")
        return {
            chunk.chunk_id: (vector, str(version))
            for chunk, vector in zip(changed, vectors)
        }, len(changed), {
            "status": "migrated" if force_embeddings and stored_versions else "updated",
            "embedding_version": str(version),
        }

    def sync_documents(
        self,
        documents: Iterable[DocumentSource],
        *,
        force_embeddings: bool = False,
    ) -> dict[str, Any]:
        with self._lock, file_transaction(self.root / "sync"):
            return self._sync_documents(
                documents,
                force_embeddings=force_embeddings,
            )

    def _sync_documents(
        self,
        documents: Iterable[DocumentSource],
        *,
        force_embeddings: bool = False,
    ) -> dict[str, Any]:
        sources = list(documents)
        if len({source.document_id for source in sources}) != len(sources):
            raise ValueError("document_id must be unique within one index sync")
        chunks_by_document = {
            source.document_id: chunk_document(
                source,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
            for source in sources
        }
        all_chunks = [chunk for source in sources for chunk in chunks_by_document[source.document_id]]
        connection = self._connect()
        try:
            existing = self._existing_chunks(connection)
        finally:
            connection.close()
        embeddings, embedded_count, embedding_update = self._embed_changed(
            all_chunks,
            existing,
            force_embeddings=force_embeddings,
        )

        source_ids = {source.document_id for source in sources}
        changed_documents = 0
        removed_documents = 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_documents = {
                str(row["document_id"]): row
                for row in connection.execute("SELECT * FROM documents").fetchall()
            }
            for stale_id in set(current_documents) - source_ids:
                if self._fts_available:
                    connection.execute("DELETE FROM chunk_fts WHERE document_id=?", (stale_id,))
                connection.execute("DELETE FROM chunks WHERE document_id=?", (stale_id,))
                connection.execute("DELETE FROM documents WHERE document_id=?", (stale_id,))
                removed_documents += 1

            for source in sources:
                chunks = chunks_by_document[source.document_id]
                metadata_json = _canonical(source.metadata)
                current = current_documents.get(source.document_id)
                document_changed = (
                    current is None
                    or str(current["content_hash"] or "") != source.content_hash
                    or str(current["title"] or "") != source.title
                    or str(current["revision"] or "") != str(source.revision or "")
                    or str(current["metadata_json"] or "{}") != metadata_json
                    or int(current["immutable"] or 0) != int(source.immutable)
                )
                if document_changed:
                    changed_documents += 1
                connection.execute(
                    "INSERT INTO documents(document_id,source_type,source_id,title,format,revision,immutable,"
                    "content_hash,metadata_json,chunk_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(document_id) DO UPDATE SET source_type=excluded.source_type,"
                    "source_id=excluded.source_id,title=excluded.title,format=excluded.format,"
                    "revision=excluded.revision,immutable=excluded.immutable,content_hash=excluded.content_hash,"
                    "metadata_json=excluded.metadata_json,chunk_count=excluded.chunk_count,updated_at=excluded.updated_at",
                    (
                        source.document_id,
                        source.source_type,
                        source.source_id,
                        source.title,
                        source.format,
                        str(source.revision or ""),
                        int(source.immutable),
                        source.content_hash,
                        metadata_json,
                        len(chunks),
                        _now(),
                    ),
                )
                # Stable chunk IDs deliberately survive insertions before a
                # chunk. Move old ordinals out of the non-negative namespace
                # before assigning their new positions, otherwise SQLite's
                # UNIQUE(document_id, ordinal) can reject a valid reorder.
                connection.execute(
                    "UPDATE chunks SET ordinal=-(ordinal+1) WHERE document_id=?",
                    (source.document_id,),
                )
                next_ids = {chunk.chunk_id for chunk in chunks}
                stale_chunks = connection.execute(
                    "SELECT chunk_id FROM chunks WHERE document_id=?",
                    (source.document_id,),
                ).fetchall()
                for row in stale_chunks:
                    chunk_id = str(row["chunk_id"])
                    if chunk_id not in next_ids:
                        connection.execute("DELETE FROM chunks WHERE chunk_id=?", (chunk_id,))
                if self._fts_available and document_changed:
                    connection.execute("DELETE FROM chunk_fts WHERE document_id=?", (source.document_id,))
                for chunk in chunks:
                    old = existing.get(chunk.chunk_id)
                    vector, version = embeddings.get(
                        chunk.chunk_id,
                        (
                            json.loads(str(old["embedding_json"] or "[]")) if old is not None else [],
                            str(old["embedding_version"] or "") if old is not None else "",
                        ),
                    )
                    connection.execute(
                        "INSERT INTO chunks(chunk_id,document_id,ordinal,heading_path,content,content_hash,"
                        "line_start,line_end,byte_start,byte_end,embedding_json,embedding_version) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET "
                        "document_id=excluded.document_id,ordinal=excluded.ordinal,heading_path=excluded.heading_path,"
                        "content=excluded.content,content_hash=excluded.content_hash,line_start=excluded.line_start,"
                        "line_end=excluded.line_end,byte_start=excluded.byte_start,byte_end=excluded.byte_end,"
                        "embedding_json=excluded.embedding_json,embedding_version=excluded.embedding_version",
                        (
                            chunk.chunk_id,
                            chunk.document_id,
                            chunk.ordinal,
                            chunk.heading_path,
                            chunk.content,
                            chunk.content_hash,
                            chunk.line_start,
                            chunk.line_end,
                            chunk.byte_start,
                            chunk.byte_end,
                            json.dumps(vector, separators=(",", ":")),
                            version,
                        ),
                    )
                    if self._fts_available and document_changed:
                        connection.execute(
                            "INSERT INTO chunk_fts(chunk_id,document_id,title,heading_path,content) VALUES(?,?,?,?,?)",
                            (chunk.chunk_id, source.document_id, source.title, chunk.heading_path, chunk.content),
                        )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        vector_status = self._rebuild_vector_store(embeddings)
        result = {
            "status": "ready",
            "document_count": len(sources),
            "chunk_count": len(all_chunks),
            "changed_document_count": changed_documents,
            "removed_document_count": removed_documents,
            "embedded_chunk_count": embedded_count,
            "embedding_update": embedding_update,
            "vector": vector_status,
            "built_at": _now(),
        }
        return self._write_state(result)

    def _rebuild_vector_store(self, changed_embeddings: dict[str, tuple[list[float], str]]) -> dict[str, Any]:
        if not self.vector_available():
            return {"status": "unavailable", "reason": "faiss_not_installed", "count": 0}
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT chunk_id,embedding_version FROM chunks "
                "WHERE embedding_version!='' ORDER BY document_id,ordinal"
            ).fetchall()
        finally:
            connection.close()
        versions = {str(row["embedding_version"]) for row in rows}
        if not rows:
            remove = getattr(self.vector_store, "remove", None)
            if remove:
                remove(self.vector_base_path)
            return {"status": "empty", "count": 0}
        if len(versions) != 1:
            return {"status": "version_mismatch", "count": len(rows), "versions": sorted(versions)}
        ids = [str(row["chunk_id"]) for row in rows]
        version = next(iter(versions))
        id_set = set(ids)
        update = getattr(self.vector_store, "update", None)
        mode = update(base_path=self.vector_base_path, ids=ids,
                      changed_vectors={key: value[0] for key, value in changed_embeddings.items() if key in id_set},
                      embedding_version=version) if update else "rebuild_required"
        if mode == "rebuild_required":
            connection = self._connect()
            try:
                vectors = [json.loads(str(row["embedding_json"])) for row in connection.execute(
                    "SELECT embedding_json FROM chunks WHERE embedding_version!='' ORDER BY document_id,ordinal"
                ).fetchall()]
            finally:
                connection.close()
            self.vector_store.write(base_path=self.vector_base_path, ids=ids, vectors=vectors, **(
                {"embedding_version": version} if update else {}
            ))
            mode = "rebuilt"
        return {"status": "ready", "count": len(ids), "embedding_version": version, "update_mode": mode}

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        tokens: list[str] = []
        for match in TOKEN_RE.finditer(str(query or "")):
            token = match.group(0)
            tokens.append(token)
            if re.fullmatch(r"[\u3400-\u9fff]{4,}", token):
                for size in (2, 3):
                    tokens.extend(
                        token[index : index + size]
                        for index in range(len(token) - size + 1)
                    )
            if len(tokens) >= 24:
                break
        return list(dict.fromkeys(tokens))[:24]

    @classmethod
    def _fts_query(cls, query: str) -> str:
        tokens = cls._query_tokens(query)
        return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)

    def _lexical_candidates(self, query: str, limit: int) -> list[tuple[str, float]]:
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = []
            fts_query = self._fts_query(query)
            if self._fts_available and fts_query:
                try:
                    rows = connection.execute(
                        "SELECT chunk_id,bm25(chunk_fts) AS rank FROM chunk_fts "
                        "WHERE chunk_fts MATCH ? ORDER BY rank LIMIT ?",
                        (fts_query, limit),
                    ).fetchall()
                except sqlite3.DatabaseError:
                    rows = []
            if not rows:
                terms = self._query_tokens(query)[:12]
                if not terms:
                    terms = [str(query or "").strip()]
                conditions: list[str] = []
                params: list[Any] = []
                for term in terms:
                    conditions.append(
                        "(c.content LIKE ? OR c.heading_path LIKE ? OR d.title LIKE ?)"
                    )
                    like = f"%{term}%"
                    params.extend((like, like, like))
                params.append(limit)
                rows = connection.execute(
                    "SELECT c.chunk_id,0 AS rank FROM chunks c "
                    "JOIN documents d ON d.document_id=c.document_id WHERE "
                    + " OR ".join(conditions)
                    + " ORDER BY c.document_id,c.ordinal LIMIT ?",
                    params,
                ).fetchall()
            return [
                (str(row["chunk_id"]), 1.0 - index / max(1, len(rows)))
                for index, row in enumerate(rows)
            ]
        finally:
            connection.close()

    def _vector_candidates(
        self,
        query: str,
        limit: int,
        *,
        policy: str = "configured",
    ) -> tuple[list[tuple[str, float]], dict[str, Any]]:
        state = self.state()
        vector = dict(state.get("vector") or {})
        if vector.get("status") != "ready" or not self.vector_available():
            return [], {**vector, "query_status": "unavailable"}
        normalized_policy = str(policy or "configured").strip().lower()
        if normalized_policy == "disabled":
            return [], {**vector, "query_status": "disabled"}
        if (
            normalized_policy == "local_only"
            and not str(vector.get("embedding_version") or "").startswith("hash-")
        ):
            return [], {**vector, "query_status": "skipped_remote_embedding_latency"}
        try:
            query_vector, version = self.query_embedding_builder(
                str(query or ""), workdir=self.workdir.as_posix(),
            )
        except Exception:
            return [], {**vector, "query_status": "embedding_unavailable", "fallback": "lexical"}
        if str(version) != str(vector.get("embedding_version") or ""):
            return [], {
                **vector,
                "query_status": "version_mismatch",
                "query_embedding_version": str(version),
                "fallback": "lexical",
            }
        try:
            hits = self.vector_store.search(
                base_path=self.vector_base_path, query_vector=query_vector, limit=limit,
            )
        except Exception:
            return [], {**vector, "query_status": "index_unavailable", "fallback": "lexical"}
        threshold = EmbeddingProvider.minimum_similarity()
        hits = [(identifier, score) for identifier, score in hits if score >= threshold]
        return hits, {**vector, "query_status": "ready", "minimum_similarity": threshold,
                      "retrieval_mode": "local_hash" if str(version).startswith("hash-") else "model"}

    def _chunk_rows(self, ids: Iterable[str]) -> dict[str, sqlite3.Row]:
        normalized = list(dict.fromkeys(str(value) for value in ids if str(value)))
        if not normalized:
            return {}
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = []
            for offset in range(0, len(normalized), 500):
                batch = normalized[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        "SELECT c.*,d.source_type,d.source_id,d.title,d.format,d.revision,d.immutable,"
                        "d.metadata_json FROM chunks c JOIN documents d ON d.document_id=c.document_id "
                        f"WHERE c.chunk_id IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            by_id = {str(row["chunk_id"]): row for row in rows}
            # Compatibility for callers/tests that still return a document ID from FAISS.
            missing = [value for value in normalized if value not in by_id]
            if missing:
                for offset in range(0, len(missing), 500):
                    batch = missing[offset : offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    fallback = connection.execute(
                        "SELECT c.*,d.source_type,d.source_id,d.title,d.format,d.revision,d.immutable,"
                        "d.metadata_json FROM chunks c JOIN documents d ON d.document_id=c.document_id "
                        f"WHERE c.document_id IN ({placeholders}) AND c.ordinal=0",
                        batch,
                    ).fetchall()
                    for row in fallback:
                        by_id[str(row["document_id"])] = row
            return by_id
        finally:
            connection.close()

    def _neighbors(self, document_id: str, ordinal: int, window: int) -> list[dict[str, Any]]:
        if window <= 0:
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT chunk_id,ordinal,heading_path,content,line_start,line_end,byte_start,byte_end "
                "FROM chunks WHERE document_id=? AND ordinal BETWEEN ? AND ? AND ordinal!=? ORDER BY ordinal",
                (document_id, max(0, ordinal - window), ordinal + window, ordinal),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        candidate_limit: int | None = None,
        allowed_document_ids: set[str] | None = None,
        lexical_weight: float = 0.45,
        vector_weight: float = 0.55,
        neighbor_window: int = 1,
        vector_query_policy: str = "configured",
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        if not text:
            raise ValueError("query is required")
        safe_limit = max(1, min(int(limit or 20), 100))
        candidates = max(safe_limit * 8, min(1000, int(candidate_limit or 100)))
        lexical = self._lexical_candidates(text, candidates)
        vector, vector_state = self._vector_candidates(
            text,
            candidates,
            policy=vector_query_policy,
        )
        ordered_ids = [identifier for identifier, _ in lexical] + [identifier for identifier, _ in vector]
        rows = self._chunk_rows(ordered_ids)
        allowed = set(allowed_document_ids or [])

        scores: dict[str, dict[str, float]] = {}
        for name, values, weight in (
            ("lexical", lexical, max(0.0, lexical_weight)),
            ("vector", vector, max(0.0, vector_weight)),
        ):
            if weight <= 0:
                continue
            for rank, (identifier, raw_score) in enumerate(values, start=1):
                row = rows.get(identifier)
                if row is None:
                    continue
                document_id = str(row["document_id"])
                if allowed_document_ids is not None and document_id not in allowed:
                    continue
                chunk_id = str(row["chunk_id"])
                item = scores.setdefault(chunk_id, {"score": 0.0, "lexical": 0.0, "vector": 0.0})
                normalized = max(0.0, min(1.0, float(raw_score)))
                item[name] = max(item[name], normalized)
                item["score"] += weight * (61.0 / (60.0 + rank))

        ranked = sorted(scores.items(), key=lambda item: (-item[1]["score"], item[0]))[:safe_limit]
        ranked_rows = self._chunk_rows(chunk_id for chunk_id, _ in ranked)
        hits: list[dict[str, Any]] = []
        for chunk_id, score in ranked:
            row = ranked_rows.get(chunk_id)
            if row is None:
                continue
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            hit = {
                "chunk_id": chunk_id,
                "document_id": str(row["document_id"]),
                "source_type": str(row["source_type"]),
                "source_id": str(row["source_id"]),
                "title": str(row["title"]),
                "format": str(row["format"]),
                "revision": str(row["revision"]),
                "immutable": bool(row["immutable"]),
                "heading_path": str(row["heading_path"]),
                "content": str(row["content"]),
                "line_start": int(row["line_start"]),
                "line_end": int(row["line_end"]),
                "byte_start": int(row["byte_start"]),
                "byte_end": int(row["byte_end"]),
                "ordinal": int(row["ordinal"]),
                "score": score["score"],
                "lexical_score": score["lexical"],
                "vector_score": score["vector"],
                "metadata": metadata,
                "neighbors": self._neighbors(
                    str(row["document_id"]),
                    int(row["ordinal"]),
                    max(0, min(2, int(neighbor_window))),
                ),
            }
            hits.append(hit)
        used_vector = any(value["vector"] > 0 for _, value in ranked)
        return {
            "query": text,
            "hits": hits,
            "total": len(hits),
            "search_mode": "hybrid" if used_vector and lexical else "vector" if used_vector else "fts",
            "index": self.state(),
            "vector": vector_state,
        }

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            document = connection.execute(
                "SELECT * FROM documents WHERE document_id=?",
                (str(document_id or ""),),
            ).fetchone()
            if document is None:
                return None
            chunks = connection.execute(
                "SELECT chunk_id,ordinal,heading_path,line_start,line_end,byte_start,byte_end "
                "FROM chunks WHERE document_id=? ORDER BY ordinal",
                (str(document_id),),
            ).fetchall()
        finally:
            connection.close()
        value = dict(document)
        value["immutable"] = bool(value.get("immutable"))
        value["metadata"] = json.loads(str(value.pop("metadata_json", "{}")))
        value["chunks"] = [dict(row) for row in chunks]
        return value


__all__ = [
    "DocumentChunk",
    "DocumentChunkIndex",
    "DocumentSource",
    "chunk_document",
]
