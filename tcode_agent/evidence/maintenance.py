"""Resumable, workspace-local maintenance of derived evidence indexes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..file_lock import lock_file
from ..memory.embedding_provider import EmbeddingProvider
from ..memory.retriever import build_embeddings_with_version
from ..memory.vector_store import FaissVectorStore

log = logging.getLogger(__name__)
_workers: dict[str, threading.Thread] = {}
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_local = threading.local()


@contextmanager
def suspend_background():
    previous = getattr(_local, 'active', False)
    _local.active = True
    try:
        yield
    finally:
        _local.active = previous


def batch_size() -> int:
    try:
        return max(10, min(500, int(os.getenv("TCODE_EVIDENCE_INDEX_BATCH_SIZE", "100"))))
    except ValueError:
        return 100


def meta(connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM evidence_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def put_meta(connection, key: str, value) -> None:
    connection.execute("INSERT OR REPLACE INTO evidence_meta VALUES(?,?)", (key, str(value)))


def tables(generation: str = "") -> tuple[str, str, str]:
    if generation and (len(generation) != 12 or any(c not in "0123456789abcdef" for c in generation)):
        raise ValueError("Invalid evidence index generation")
    suffix = f"_{generation}" if generation else ""
    return tuple(f"evidence_{name}{suffix}" for name in ("fts", "facets", "graph_members"))


def active_tables(connection) -> tuple[str, str, str]:
    return tables(meta(connection, "active_generation"))


def create_projection(connection, generation: str = "") -> None:
    fts, facets, graph = tables(generation)
    connection.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5(search_terms, tokenize='unicode61')")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {facets}(evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id) ON DELETE CASCADE, facet TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(evidence_id,facet,value))")
    connection.execute(f"CREATE INDEX IF NOT EXISTS idx_{facets} ON {facets}(facet,value,evidence_id)")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {graph}(evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id) ON DELETE CASCADE, node_id TEXT NOT NULL, PRIMARY KEY(evidence_id,node_id))")
    connection.execute(f"CREATE INDEX IF NOT EXISTS idx_{graph} ON {graph}(node_id,evidence_id)")
    connection.execute(f"CREATE TRIGGER IF NOT EXISTS delete_{fts} AFTER DELETE ON evidence_records BEGIN DELETE FROM {fts} WHERE rowid=old.rowid; END")


def state(connection) -> dict:
    return json.loads(meta(connection, "maintenance", "{}"))


def save_state(connection, job: dict) -> None:
    job['total'] = max(int(job.get('total', 0)), int(job.get('processed', 0)))
    job["updated_at"] = time.time()
    put_meta(connection, "maintenance", json.dumps(job))


def request(*, workdir: str, rebuild: bool = True, verify_assets: bool = False, background: bool = True) -> dict:
    from .store import EvidenceStore

    with EvidenceStore._open(workdir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            job = state(connection)
            if job.get("status") in {"queued", "running", "failed"}:
                was_failed = job.get('status') == 'failed'
                if not was_failed and ((verify_assets and not job.get('verify_assets')) or (rebuild and not job.get('generation'))):
                    raise ValueError('A different maintenance task is already in progress')
                # A failed job retains its checkpoints. New requests join it.
                job.update(status='queued' if was_failed else job['status'], retry_after=0)
                job["verify_assets"] = bool(job.get("verify_assets") or verify_assets)
                if rebuild and not job.get("generation"):
                    if not was_failed and job.get('phase') != 'lexical':
                        raise ValueError('A different maintenance task is already in progress')
                    job["generation"] = uuid.uuid4().hex[:12]
                    job["lexical_done"] = False
                    job["last_id"] = ""
                    job["processed"] = 0
                    job['update_vectors'] = True
                    create_projection(connection, job["generation"])
            else:
                job = {"job_id": uuid.uuid4().hex, "generation": uuid.uuid4().hex[:12] if rebuild else "",
                       "status": "queued", "phase": "lexical" if rebuild else "assets",
                       "processed": 0, "total": connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0],
                       "last_id": "", "lexical_done": not rebuild, "verify_assets": verify_assets, "asset_cursor": "", 'update_vectors': rebuild or not verify_assets}
                if rebuild:
                    create_projection(connection, job["generation"])
            save_state(connection, job)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    if background:
        schedule(workdir)
    return public_status(workdir)


def schedule_if_needed(workdir: str, connection) -> None:
    if getattr(_local, 'active', False):
        return
    job = state(connection)
    pending = job.get("status") in {"queued", "running", "failed"}
    revision = meta(connection, "revision", "0")
    if not pending and os.getenv("TCODE_EVIDENCE_SEMANTIC_SEARCH", "1") != "0" and EmbeddingProvider.is_enabled(workdir) and FaissVectorStore.available():
        pending = meta(connection, "vector_revision", "-1") != revision or meta(connection, "vector_version") != EmbeddingProvider.version(workdir)
    if pending and float(job.get("retry_after", 0)) <= time.time():
        schedule(workdir)


def schedule(workdir: str) -> None:
    if os.getenv("TCODE_EVIDENCE_BACKGROUND_INDEX", "1") == "0":
        return
    key = str(Path(workdir).resolve())
    with _lock:
        if key in _workers and _workers[key].is_alive():
            return
        if not _slots.acquire(blocking=False):
            return
        def target():
            try:
                run(workdir=key)
            except Exception:
                log.exception("Evidence maintenance failed")
            finally:
                with _lock:
                    _workers.pop(key, None)
                _slots.release()
        thread = threading.Thread(target=target, name="evidence-maintenance", daemon=True)
        _workers[key] = thread
        try:
            thread.start()
        except Exception:
            _workers.pop(key, None)
            _slots.release()
            raise


def public_status(workdir: str) -> dict:
    from .store import EvidenceStore

    with EvidenceStore._open(workdir) as connection:
        job = state(connection)
        version = meta(connection, "vector_version")
        vector_ready = bool(meta(connection, "vector_path")) or version and meta(connection, 'vector_revision') == meta(connection, 'revision', '0')
        semantic = "disabled" if os.getenv("TCODE_EVIDENCE_SEMANTIC_SEARCH", "1") == "0" else "unconfigured" if not EmbeddingProvider.is_enabled(workdir) else "ready" if vector_ready and version == EmbeddingProvider.version(workdir) else "pending"
        if semantic not in {'disabled', 'unconfigured'} and not FaissVectorStore.available():
            semantic = 'unavailable'
        if semantic == "ready" and meta(connection, "vector_revision") != meta(connection, "revision", "0"):
            semantic = "updating"
        if job.get("vector_error") and semantic not in {'disabled', 'unconfigured'}:
            semantic = "degraded"
        if semantic not in {'disabled', 'unconfigured'} and float(meta(connection, 'semantic_query_error_until', '0')) > time.time():
            semantic = 'degraded'
        return {"status": job.get("status", "idle"), "phase": job.get("phase", ""),
                "processed": job.get("processed", 0), "total": job.get("total", 0),
                "semantic": semantic, "retryable": job.get("status") == "failed",
                "lexical_ready": bool(meta(connection, 'search_index_version')),
                "updated_at": job.get("updated_at", 0)}


def run(*, workdir: str, max_batches: int | None = None, wait: bool = False) -> dict:
    with suspend_background():
        return _run(workdir=workdir, max_batches=max_batches, wait=wait)


def _run(*, workdir: str, max_batches: int | None = None, wait: bool = False) -> dict:
    """A file lock fences workers across processes; SQLite checkpoints survive exits."""
    from .store import EvidenceStore, SEARCH_INDEX_VERSION

    root = EvidenceStore.evidence_path(workdir).parent
    root.mkdir(parents=True, exist_ok=True)
    with (root / "maintenance.lock").open("a") as lock:
        try:
            lock_file(lock, blocking=wait)
        except BlockingIOError:
            return public_status(workdir)
        try:
            with EvidenceStore._open(workdir) as connection:
                job = state(connection)
            if wait and job.get('status') == 'completed':
                return public_status(workdir)
            if not job or job.get("status") == "completed":
                request(workdir=workdir, rebuild=False, background=False)
            batches = 0
            while True:
                with EvidenceStore._open(workdir) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        job = state(connection)
                        job.update(status="running", retry_after=0)
                        if job.get("lexical_done"):
                            save_state(connection, job)
                            connection.commit()
                            break
                        job["phase"] = "lexical"
                        rows = connection.execute("SELECT rowid,evidence_id,payload_json FROM evidence_records WHERE evidence_id>? ORDER BY evidence_id LIMIT ?", (job["last_id"], batch_size())).fetchall()
                        for row in rows:
                            value = EvidenceStore._record_from_row(row)
                            EvidenceStore._index_projection(connection, value, int(row["rowid"]), tables(job["generation"]))
                            connection.execute("UPDATE evidence_records SET workflow_run_id=?,stage_id=? WHERE evidence_id=?", (value.workflow_run_id, value.stage_id, value.evidence_id))
                        if rows:
                            job["last_id"] = rows[-1]["evidence_id"]
                            job["processed"] += len(rows)
                        else:
                            # Writes dual-update the shadow generation until this atomic switch.
                            put_meta(connection, "active_generation", job["generation"])
                            put_meta(connection, "search_index_version", SEARCH_INDEX_VERSION)
                            job["lexical_done"] = True
                        save_state(connection, job)
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                batches += 1
                if max_batches is not None and batches >= max_batches:
                    return public_status(workdir)
                time.sleep(0.005)
            _vectors(workdir)
            _assets(workdir)
            with EvidenceStore._open(workdir) as connection:
                job = state(connection)
                job.update(status="completed", phase="done", processed=connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
                job["total"] = job["processed"]
                save_state(connection, job)
            # Keep the previous generation for readers that resolved it just before publication.
            _cleanup(workdir)
            return public_status(workdir)
        except Exception:
            with EvidenceStore._open(workdir) as connection:
                job = state(connection)
                job.update(status="failed", retry_after=time.time() + 60)
                save_state(connection, job)
            raise


def _vectors(workdir: str) -> None:
    from .store import EvidenceStore

    if os.getenv("TCODE_EVIDENCE_SEMANTIC_SEARCH", "1") == "0" or not EmbeddingProvider.is_enabled(workdir) or not FaissVectorStore.available():
        return
    version = EmbeddingProvider.version(workdir)
    with EvidenceStore._open(workdir) as connection:
        job = state(connection)
        if not job.get('update_vectors', True):
            return
        job.update(phase="semantic", processed=0)
        if job.get('vector_version') != version:
            job.update(vector_cursor='', vector_version=version)
        save_state(connection, job)
    while True:
        with EvidenceStore._open(workdir) as connection:
            job = state(connection)
            rows = connection.execute("SELECT e.evidence_id,e.payload_json,e.updated_at,e.content_hash,v.vector AS cached_vector,v.version AS cached_version FROM evidence_records e LEFT JOIN evidence_vector_cache v ON e.evidence_id=v.evidence_id WHERE e.evidence_id>? AND (v.evidence_id IS NULL OR v.version<>? OR v.updated_at<>e.updated_at OR v.content_hash<>e.content_hash) ORDER BY e.evidence_id LIMIT ?", (job.get('vector_cursor', ''), version, batch_size())).fetchall()
        if not rows:
            break
        records = [EvidenceStore._record_from_row(row) for row in rows]
        texts = [r.search_text or r.content for r in records]
        text_digests = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
        vectors = [None] * len(rows)
        missing = []
        for index, row in enumerate(rows):
            cached = json.loads(row['cached_vector']) if row['cached_vector'] else {}
            if isinstance(cached, dict) and row['cached_version'] == version and cached.get('text_digest') == text_digests[index]:
                vectors[index] = cached.get('vector')
            if not vectors[index]:
                missing.append(index)
        actual = version
        if missing:
            fresh, actual = build_embeddings_with_version([texts[i] for i in missing], workdir=workdir)
            if len(fresh) != len(missing):
                fresh, actual = [], ''
            for index, vector in zip(missing, fresh):
                vectors[index] = vector
        if actual != version or len(vectors) != len(rows) or any(not vector for vector in vectors):
            with EvidenceStore._open(workdir) as connection:
                job = state(connection)
                job.update(vector_error=True, retry_after=time.time() + 60)
                save_state(connection, job)
            return
        with EvidenceStore._open(workdir) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for row, vector, text_digest in zip(rows, vectors, text_digests):
                    connection.execute("INSERT OR REPLACE INTO evidence_vector_cache(evidence_id,updated_at,content_hash,version,vector) SELECT evidence_id,updated_at,content_hash,?,? FROM evidence_records WHERE evidence_id=? AND updated_at=? AND content_hash=?", (version, json.dumps({'vector': vector, 'text_digest': text_digest}), row["evidence_id"], row["updated_at"], row["content_hash"]))
                job = state(connection)
                job["processed"] += len(rows)
                job['vector_cursor'] = rows[-1]['evidence_id']
                save_state(connection, job)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        time.sleep(0.005)
    base = EvidenceStore.evidence_path(workdir).parent / "vectors" / uuid.uuid4().hex
    with EvidenceStore._open(workdir) as connection:
        connection.execute('BEGIN')
        revision = meta(connection, 'revision', '0')
        selection = ' FROM evidence_vector_cache v JOIN evidence_records e ON e.evidence_id=v.evidence_id AND e.updated_at=v.updated_at AND e.content_hash=v.content_hash WHERE v.version=?'
        count = int(connection.execute('SELECT COUNT(*)' + selection, (version,)).fetchone()[0])
        total = int(connection.execute('SELECT COUNT(*) FROM evidence_records').fetchone()[0])
        cursor = connection.execute('SELECT v.*' + selection + ' ORDER BY v.evidence_id', (version,))
        def batches():
            while rows := cursor.fetchmany(batch_size()):
                cached = [json.loads(r['vector']) for r in rows]
                yield ([r['evidence_id'] for r in rows], [value['vector'] if isinstance(value, dict) else value for value in cached],
                       [hashlib.sha256((r['updated_at'] + r['content_hash']).encode()).hexdigest() for r in rows])
        FaissVectorStore.write_batches(base_path=base, count=count, batches=batches(), embedding_version=version)
    # Immutable filenames plus a transactional pointer prevent mixed index/metadata pairs.
    with EvidenceStore._open(workdir) as connection:
        connection.execute("BEGIN IMMEDIATE")
        put_meta(connection, "vector_path", str(base) if count else "")
        put_meta(connection, "vector_version", version)
        put_meta(connection, "vector_revision", revision if count == total else '-1')
        put_meta(connection, 'semantic_query_error_until', 0)
        job = state(connection)
        job.pop("vector_error", None)
        save_state(connection, job)
        connection.commit()


def _assets(workdir: str) -> None:
    from .store import EvidenceStore
    from .assets import check_assets

    verified_paths: set[str] = set()
    while True:
        with EvidenceStore._open(workdir) as connection:
            job = state(connection)
            if not job.get("verify_assets"):
                return
            rows = connection.execute("SELECT evidence_id,payload_json FROM evidence_records WHERE evidence_id>? ORDER BY evidence_id LIMIT ?", (job.get("asset_cursor", ""), batch_size())).fetchall()
        if not rows:
            return
        for row in rows:
            check_assets(EvidenceStore._record_from_row(row), workdir=workdir, verify=True, verified_paths=verified_paths)
        with EvidenceStore._open(workdir) as connection:
            job = state(connection)
            job.update(phase="assets", asset_cursor=rows[-1]["evidence_id"], processed=job.get("asset_processed", 0) + len(rows))
            job["asset_processed"] = job["processed"]
            save_state(connection, job)
        time.sleep(0.005)


def _cleanup(workdir: str) -> None:
    from .store import EvidenceStore

    with EvidenceStore._open(workdir) as connection:
        active = meta(connection, "active_generation")
        previous = meta(connection, "previous_generation")
        keep = {active, previous, ""}
        names = [str(r[0]) for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'evidence_facets_*'")]
        for name in names:
            generation = name.removeprefix("evidence_facets_")
            if generation not in keep:
                for table in tables(generation):
                    connection.execute(f"DROP TRIGGER IF EXISTS delete_{table}")
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
        put_meta(connection, "previous_generation", active)
        current = meta(connection, "vector_path")
    # Previous vector files stay usable by in-flight searches; prune only old files.
    for path in (EvidenceStore.evidence_path(workdir).parent / "vectors").glob("*"):
        if current and str(path).startswith(current) or time.time() - path.stat().st_mtime < 86400:
            continue
        path.unlink(missing_ok=True)
