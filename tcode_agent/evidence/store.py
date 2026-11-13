"""Workspace-local evidence registry inspired by gbrain's cited brain layer.

Evidence is an immutable-ish source ledger, while Paper Wiki owns editable
claims and their semantic relationships.  SQLite provides atomic concurrent
writes; the original ``records.json`` file is imported automatically once and
kept in place as a migration backup.  Workspaces that briefly used the old
``.opencode/evidence`` location are imported into the canonical ``.tcode``
store when the canonical store is still empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Optional
from ..sqlite_runtime import connect as sqlite_connect

from ..memory.retriever import (
    build_embedding_with_version,
    build_embeddings_with_version,
    build_search_text,
    semantic_rank,
)


EVIDENCE_ID_RE = re.compile(
    r"(?:\[evidence:([A-Za-z0-9_.:-]+)\]|evidence[_ -]?id\s*[:=]\s*([A-Za-z0-9_.:-]+))",
    re.IGNORECASE,
)
METRIC_ENTITY_RE = re.compile(
    r"^(?:km|kcat|kcat/km|tm|pH|activity|yield|conversion|selectivity|enantioselectivity|kinetics)$",
    re.IGNORECASE,
)
MUTATION_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][a-z]{2}|[A-Z])\d{1,4}(?:[A-Z][a-z]{2}|[A-Z])\b"
)
INCREASE_RE = re.compile(
    r"\b(?:increase[sd]?|increasing|improve[sd]?|improvement|enhance[sd]?|higher|greater|"
    r"elevated|activated|up-?regulated|gain(?:ed)?|boost(?:ed)?)\b",
    re.IGNORECASE,
)
DECREASE_RE = re.compile(
    r"\b(?:decrease[sd]?|decreasing|reduce[sd]?|reduction|lower|less|diminish(?:ed)?|"
    r"inhibit(?:ed|ion)?|impaired|lost|loss|down-?regulated)\b",
    re.IGNORECASE,
)
NO_CHANGE_RE = re.compile(
    r"\b(?:no\s+(?:significant\s+)?(?:change|effect|difference)|unchanged|not\s+(?:improved|increased|reduced))\b",
    re.IGNORECASE,
)
GENERIC_ENTITY_TERMS = {
    "activity",
    "assay",
    "condition",
    "enzyme",
    "evidence",
    "fact",
    "figure",
    "kinetics",
    "metric",
    "page",
    "paper",
    "protein",
    "substrate",
    "table",
    "variant",
}
EVIDENCE_SCHEMA_VERSION = 2
SEARCH_INDEX_VERSION = 1
EVIDENCE_STATUSES = {"active", "stale", "retracted", "superseded"}


def _search_tokens(text: str) -> list[str]:
    # 中文按双字切分，避免 unicode61 把整句中文当作一个词。
    tokens: list[str] = []
    for term in re.findall(r"[\u4e00-\u9fff]+|[^\W_]+", str(text).lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            tokens.extend(term[i:i + 2] for i in range(max(1, len(term) - 1)))
        else:
            tokens.append(term)
    return tokens


def _search_candidate_limit(limit: int) -> int:
    # 候选数限制仅控制重排内存；数据库仍在全部授权记录中召回。
    try:
        configured = int(os.environ.get("TCODE_EVIDENCE_SEARCH_CANDIDATES", "256"))
    except ValueError:
        configured = 256
    return max(limit, min(2048, max(64, configured)))


class EvidenceStoreCorruptionError(RuntimeError):
    """Raised when legacy evidence cannot be migrated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [part.strip() for part in re.split(r"[,;\n]", values) if part.strip()]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        text = _clean_string(value)
        if text and text not in result:
            result.append(text)
    return result


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _clean_citations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            cleaned = {str(k): v for k, v in item.items() if str(k).strip()}
            if cleaned:
                result.append(cleaned)
        elif str(item or "").strip():
            result.append({"text": str(item).strip()})
    return result


def _fingerprint(value: Any) -> str:
    text = _clean_string(value)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _graph_node_id(node_type: str, label: Any) -> str:
    return f"{node_type}:{_fingerprint(label)}"


def _classify_entity(value: str) -> str:
    text = _clean_string(value)
    if MUTATION_ENTITY_RE.search(text):
        return "mutation"
    if METRIC_ENTITY_RE.search(text):
        return "metric"
    return "entity"


def _normalize_entity(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_string(value)).casefold()


def _claim_polarity(value: Any) -> str:
    text = _clean_string(value)
    if not text:
        return ""
    has_no_change = bool(NO_CHANGE_RE.search(text))
    has_increase = bool(INCREASE_RE.search(text))
    has_decrease = bool(DECREASE_RE.search(text))
    if has_no_change:
        return "no_change"
    if has_increase and not has_decrease:
        return "increase"
    if has_decrease and not has_increase:
        return "decrease"
    return ""


def _opposite_polarity(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    if {left, right} == {"increase", "decrease"}:
        return True
    return "no_change" in {left, right}


def _citation_locator(citation: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, label in (
        ("page", "page"),
        ("table_id", "table"),
        ("figure_id", "figure"),
        ("row_index", "row"),
        ("chunk_id", "chunk"),
    ):
        value = citation.get(key)
        if value:
            parts.append(f"{label} {value}")
    image_path = _clean_string(citation.get("image_path"))
    if image_path:
        parts.append(f"image {Path(image_path).name}")
    anchor_ids = citation.get("anchor_ids")
    if isinstance(anchor_ids, list) and anchor_ids:
        parts.append("anchors " + ",".join(str(item) for item in anchor_ids[:3]))
    text = _clean_string(citation.get("text"))
    if text:
        parts.append(text[:80])
    return ", ".join(parts)


def _has_citation_locator(record: "EvidenceRecord") -> bool:
    if record.locator:
        return True
    for citation in record.citations:
        if _citation_locator(citation):
            return True
    return False


def _iter_artifact_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_artifact_refs(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_artifact_refs(item)
        return
    text = _clean_string(value)
    if not text:
        return
    if "/" in text or "\\" in text or re.search(r"\.(?:pdf|png|jpg|jpeg|svg|json|csv|tsv|txt|md)$", text, re.IGNORECASE):
        yield text


@dataclass(slots=True)
class EvidenceRecord:
    evidence_id: str
    kind: str
    title: str
    content: str
    source_type: str = ""
    source_id: str = ""
    source_path: str = ""
    locator: str = ""
    claims: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    workflow_run_id: str = ""
    stage_id: str = ""
    artifact_refs: dict[str, Any] = field(default_factory=dict)
    source_agent: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    search_text: str = ""
    embedding: list[float] = field(default_factory=list)
    embedding_version: str = ""
    content_hash: str = ""
    status: str = "active"
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvidenceRecord":
        data = dict(payload or {})
        confidence = data.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence or 0.0)))
        except Exception:
            confidence = 0.0
        return cls(
            evidence_id=_clean_string(data.get("evidence_id") or data.get("id")),
            kind=_clean_string(data.get("kind") or "note"),
            title=_clean_string(data.get("title")),
            content=_clean_string(data.get("content")),
            source_type=_clean_string(data.get("source_type")),
            source_id=_clean_string(data.get("source_id")),
            source_path=_clean_string(data.get("source_path")),
            locator=_clean_string(data.get("locator")),
            claims=_clean_list(data.get("claims")),
            entities=_clean_list(data.get("entities")),
            citations=_clean_citations(data.get("citations")),
            tags=_clean_list(data.get("tags")),
            confidence=confidence,
            workflow_run_id=_clean_string(data.get("workflow_run_id")),
            stage_id=_clean_string(data.get("stage_id")),
            artifact_refs=_clean_dict(data.get("artifact_refs")),
            source_agent=_clean_string(data.get("source_agent")),
            metadata=_clean_dict(data.get("metadata")),
            created_at=_clean_string(data.get("created_at")),
            updated_at=_clean_string(data.get("updated_at")),
            search_text=_clean_string(data.get("search_text")),
            embedding=[float(x) for x in data.get("embedding", []) if isinstance(x, (int, float))],
            embedding_version=_clean_string(data.get("embedding_version")),
            content_hash=_clean_string(data.get("content_hash")),
            status=(
                _clean_string(data.get("status")).lower()
                if _clean_string(data.get("status")).lower() in EVIDENCE_STATUSES
                else "active"
            ),
            schema_version=int(data.get("schema_version") or 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_evidence_record(
    *,
    content: str,
    kind: str = "note",
    title: str = "",
    source_type: str = "",
    source_id: str = "",
    source_path: str = "",
    locator: str = "",
    claims: Iterable[str] | None = None,
    entities: Iterable[str] | None = None,
    citations: list[dict[str, Any]] | None = None,
    tags: Iterable[str] | None = None,
    confidence: float = 0.0,
    workflow_run_id: str = "",
    stage_id: str = "",
    artifact_refs: dict[str, Any] | None = None,
    source_agent: str = "",
    metadata: dict[str, Any] | None = None,
    evidence_id: str = "",
    workdir: Optional[str] = None,
    defer_embedding: bool = False,
) -> EvidenceRecord:
    now = _utc_now()
    normalized_claims = _clean_list(list(claims or []))
    normalized_entities = _clean_list(list(entities or []))
    normalized_tags = _clean_list(list(tags or []))
    try:
        normalized_confidence = max(0.0, min(1.0, float(confidence or 0.0)))
    except Exception:
        normalized_confidence = 0.0
    record = EvidenceRecord(
        evidence_id=_clean_string(evidence_id) or f"ev-{uuid.uuid4().hex[:12]}",
        kind=_clean_string(kind) or "note",
        title=_clean_string(title),
        content=_clean_string(content),
        source_type=_clean_string(source_type),
        source_id=_clean_string(source_id),
        source_path=_clean_string(source_path),
        locator=_clean_string(locator),
        claims=normalized_claims,
        entities=normalized_entities,
        citations=_clean_citations(citations or []),
        tags=normalized_tags,
        confidence=normalized_confidence,
        workflow_run_id=_clean_string(workflow_run_id),
        stage_id=_clean_string(stage_id),
        artifact_refs=_clean_dict(artifact_refs),
        source_agent=_clean_string(source_agent),
        metadata=_clean_dict(metadata),
        created_at=now,
        updated_at=now,
        content_hash=hashlib.sha256(_clean_string(content).encode("utf-8")).hexdigest(),
        status="active",
        schema_version=EVIDENCE_SCHEMA_VERSION,
    )
    record.search_text = build_search_text(
        content="\n".join(
            part
            for part in [
                record.title,
                record.content,
                "\n".join(record.claims),
                " ".join(record.entities),
                record.source_id,
                record.source_path,
                record.locator,
            ]
            if part
        ),
        tags=record.tags,
        fields={
            "kind": record.kind,
            "source_type": record.source_type,
            "workflow_run_id": record.workflow_run_id,
            "stage_id": record.stage_id,
        },
    )
    if not defer_embedding:
        record.embedding, record.embedding_version = build_embedding_with_version(
            record.search_text,
            workdir=workdir,
        )
    return record


def extract_evidence_ids(text: str) -> list[str]:
    ids: list[str] = []
    for match in EVIDENCE_ID_RE.finditer(str(text or "")):
        value = _clean_string(match.group(1) or match.group(2))
        if value and value not in ids:
            ids.append(value)
    return ids


def _matches_all(record_values: list[str], required: Iterable[str]) -> bool:
    lower_values = {value.lower() for value in record_values}
    return all(str(item or "").strip().lower() in lower_values for item in required)


def _claim_observations(records: Iterable[EvidenceRecord]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for record in records:
        claims = record.claims or ([record.content] if record.content else [])
        normalized_entities = [
            _normalize_entity(entity)
            for entity in record.entities
            if _normalize_entity(entity) and _normalize_entity(entity) not in GENERIC_ENTITY_TERMS
        ]
        for claim in claims:
            claim_text = _clean_string(claim)
            polarity = _claim_polarity(claim_text)
            if not polarity:
                continue
            claim_lower = claim_text.casefold()
            claim_entities = [
                entity
                for entity in normalized_entities
                if entity and (entity in claim_lower or len(normalized_entities) <= 3)
            ] or normalized_entities
            if not claim_entities:
                continue
            observations.append(
                {
                    "evidence_id": record.evidence_id,
                    "claim": claim_text,
                    "claim_node_id": _graph_node_id("claim", claim_text),
                    "polarity": polarity,
                    "entities": sorted(set(claim_entities)),
                    "source_id": record.source_id,
                    "locator": record.locator,
                }
            )
    return observations


def _detect_conflicts(records: Iterable[EvidenceRecord]) -> list[dict[str, Any]]:
    observations = _claim_observations(records)
    conflicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for left, right in combinations(observations, 2):
        left_id = str(left.get("evidence_id") or "")
        right_id = str(right.get("evidence_id") or "")
        if not left_id or not right_id or left_id == right_id:
            continue
        if not _opposite_polarity(str(left.get("polarity") or ""), str(right.get("polarity") or "")):
            continue
        shared_entities = sorted(set(left.get("entities") or []) & set(right.get("entities") or []))
        if not shared_entities:
            continue
        if len(shared_entities) < 2 and not any(
            METRIC_ENTITY_RE.search(entity) or MUTATION_ENTITY_RE.search(entity)
            for entity in shared_entities
        ):
            continue
        key = "|".join(sorted([left_id, right_id]) + shared_entities)
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(
            {
                "left_evidence_id": left_id,
                "right_evidence_id": right_id,
                "left_claim": str(left.get("claim") or ""),
                "right_claim": str(right.get("claim") or ""),
                "left_polarity": str(left.get("polarity") or ""),
                "right_polarity": str(right.get("polarity") or ""),
                "shared_entities": shared_entities,
                "left_source_id": str(left.get("source_id") or ""),
                "right_source_id": str(right.get("source_id") or ""),
                "left_locator": str(left.get("locator") or ""),
                "right_locator": str(right.get("locator") or ""),
            }
        )
    return conflicts


class EvidenceStore:
    """Transactional workspace-local evidence registry."""

    _initialization_lock = threading.RLock()
    _initialized_paths: set[str] = set()

    @classmethod
    def _resolve_workdir(cls, workdir: Optional[str]) -> str:
        return workdir or os.getcwd()

    @classmethod
    def evidence_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".tcode" / "evidence" / "evidence.sqlite3"

    @classmethod
    def legacy_evidence_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".tcode" / "evidence" / "records.json"

    @classmethod
    def previous_evidence_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".opencode" / "evidence" / "evidence.sqlite3"

    @classmethod
    def previous_legacy_evidence_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".opencode" / "evidence" / "records.json"

    @classmethod
    def graph_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".tcode" / "evidence" / "graph.json"

    @classmethod
    def doctor_path(cls, workdir: Optional[str] = None) -> Path:
        return Path(cls._resolve_workdir(workdir)) / ".tcode" / "evidence" / "doctor_report.json"

    @classmethod
    def _ensure_store(cls, workdir: Optional[str] = None) -> Path:
        path = cls.evidence_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with cls._open(workdir):
            pass
        return path

    @classmethod
    @contextmanager
    def _open(cls, workdir: Optional[str] = None) -> Iterable[sqlite3.Connection]:
        connection = cls._connect(workdir)
        try:
            yield connection
        finally:
            connection.close()

    @classmethod
    def _connect(cls, workdir: Optional[str] = None) -> sqlite3.Connection:
        path = cls.evidence_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path = str(path.resolve())
        connection = sqlite_connect(
            path.as_posix(),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            # Set the busy handler before any PRAGMA that may need a database
            # lock. Concurrent first-open journal mode changes can otherwise
            # fail immediately on some SQLite builds.
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")

            with cls._initialization_lock:
                if normalized_path in cls._initialized_paths:
                    schema_exists = connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'evidence_meta'"
                    ).fetchone()
                    if schema_exists is None:
                        cls._initialized_paths.discard(normalized_path)
                if normalized_path not in cls._initialized_paths:
                    cls._initialize_database(connection, workdir)
                    cls._initialized_paths.add(normalized_path)
            from .maintenance import schedule_if_needed
            schedule_if_needed(cls._resolve_workdir(workdir), connection)
            return connection
        except Exception:
            connection.close()
            raise

    @classmethod
    def _initialize_database(
        cls,
        connection: sqlite3.Connection,
        workdir: Optional[str],
    ) -> None:
        """Initialize one evidence database once per process with lock retries."""

        for attempt in range(5):
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS evidence_records (
                        evidence_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        source_type TEXT NOT NULL DEFAULT '',
                        source_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        content_hash TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_evidence_source
                        ON evidence_records(source_type, source_id);
                    CREATE INDEX IF NOT EXISTS idx_evidence_status
                        ON evidence_records(status, updated_at);
                    CREATE INDEX IF NOT EXISTS idx_evidence_content_hash
                        ON evidence_records(content_hash);
                    """
                )
                cls._migrate_legacy_json(connection, workdir)
                cls._migrate_previous_opencode_sources(connection, workdir)
                cls._initialize_search_index(connection)
                return
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))

    @classmethod
    def _initialize_search_index(cls, connection: sqlite3.Connection, *, force: bool = False) -> None:
        from .maintenance import active_tables, batch_size, create_projection, put_meta, save_state

        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE INDEX IF NOT EXISTS idx_evidence_browse ON evidence_records(status, created_at, evidence_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_created ON evidence_records(created_at, evidence_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_kind_filter ON evidence_records(lower(kind), status);
            CREATE INDEX IF NOT EXISTS idx_evidence_type_filter ON evidence_records(lower(source_type), status);
            CREATE TABLE IF NOT EXISTS evidence_vector_cache (
                evidence_id TEXT PRIMARY KEY REFERENCES evidence_records(evidence_id) ON DELETE CASCADE,
                updated_at TEXT NOT NULL, content_hash TEXT NOT NULL, version TEXT NOT NULL, vector TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_asset_checks (
                path TEXT PRIMARY KEY, baseline TEXT NOT NULL, observed TEXT NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, checked_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO evidence_meta VALUES('revision','0');
            CREATE TRIGGER IF NOT EXISTS evidence_revision_insert AFTER INSERT ON evidence_records BEGIN
                UPDATE evidence_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision';
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_revision_update AFTER UPDATE OF payload_json ON evidence_records BEGIN
                UPDATE evidence_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision';
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_revision_delete AFTER DELETE ON evidence_records BEGIN
                UPDATE evidence_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision';
            END;
        """)
        try:
            create_projection(connection, active_tables(connection)[0].removeprefix('evidence_fts').lstrip('_'))
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(evidence_records)")}
            for column in ("workflow_run_id", "stage_id"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE evidence_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_run_stage ON evidence_records(workflow_run_id, stage_id, status)")
            version = connection.execute("SELECT value FROM evidence_meta WHERE key='search_index_version'").fetchone()
            if force or version is None or str(version[0]) != str(SEARCH_INDEX_VERSION):
                count = int(connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
                if count <= batch_size():
                    for table in active_tables(connection):
                        connection.execute(f"DELETE FROM {table}")
                    for row in connection.execute("SELECT rowid,evidence_id,payload_json FROM evidence_records").fetchall():
                        cls._index_record(connection, cls._record_from_row(row), int(row["rowid"]))
                    put_meta(connection, 'search_index_version', SEARCH_INDEX_VERSION)
                else:
                    # 大库首次回填进入后台；请求线程只准备空索引和持久化断点。
                    from .maintenance import state
                    existing_job = state(connection)
                    if not existing_job or existing_job.get('status') == 'completed':
                        generation = uuid.uuid4().hex[:12]
                        create_projection(connection, generation)
                        save_state(connection, {'job_id': uuid.uuid4().hex, 'generation': generation, 'status': 'queued', 'phase': 'lexical', 'processed': 0, 'total': count, 'last_id': '', 'lexical_done': False})
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @classmethod
    def rebuild_search_index(cls, *, workdir: Optional[str] = None) -> dict[str, int]:
        """CLI-compatible blocking runner, with resumable short transactions."""
        from .maintenance import request, run, suspend_background
        resolved = cls._resolve_workdir(workdir)
        with suspend_background():
            request(workdir=resolved, background=False)
            run(workdir=resolved, wait=True)
            with cls._open(workdir) as connection:
                count = int(connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
        return {"indexed_record_count": count, "search_index_version": SEARCH_INDEX_VERSION}

    @classmethod
    def _index_record(cls, connection: sqlite3.Connection, record: EvidenceRecord, rowid: int) -> None:
        from .maintenance import active_tables, state, tables
        connection.execute("UPDATE evidence_records SET workflow_run_id=?,stage_id=? WHERE evidence_id=?", (record.workflow_run_id, record.stage_id, record.evidence_id))
        active = active_tables(connection)
        cls._index_projection(connection, record, rowid, active)
        job = state(connection)
        if job.get('generation') and not job.get('lexical_done'):
            shadow = tables(job['generation'])
            if shadow != active:
                cls._index_projection(connection, record, rowid, shadow)

    @classmethod
    def _index_projection(cls, connection: sqlite3.Connection, record: EvidenceRecord, rowid: int, projection: tuple[str, str, str]) -> None:
        fts, facets, graph_table = projection
        connection.execute(f"DELETE FROM {fts} WHERE rowid=?", (rowid,))
        text = record.search_text or "\n".join([record.title, record.content, *record.claims, *record.entities, *record.tags, record.source_id, record.locator])
        connection.execute(f"INSERT INTO {fts}(rowid,search_terms) VALUES(?,?)", (rowid, " ".join(_search_tokens(text))))
        connection.execute(f"DELETE FROM {facets} WHERE evidence_id=?", (record.evidence_id,))
        connection.executemany(f"INSERT OR IGNORE INTO {facets}(evidence_id,facet,value) VALUES(?,?,?)", [(record.evidence_id, facet, value) for facet, values in (("tag", record.tags), ("entity", record.entities)) for value in values])
        graph = cls._graph_from_records([record])
        connection.execute(f"DELETE FROM {graph_table} WHERE evidence_id=?", (record.evidence_id,))
        connection.executemany(f"INSERT INTO {graph_table}(evidence_id,node_id) VALUES(?,?)", [(record.evidence_id, node["node_id"]) for node in graph["nodes"]])

    @classmethod
    def _migrate_legacy_json(
        cls,
        connection: sqlite3.Connection,
        workdir: Optional[str],
    ) -> None:
        """Import the original JSON ledger once without deleting it."""

        migrated = connection.execute(
            "SELECT value FROM evidence_meta WHERE key = 'legacy_json_migrated'"
        ).fetchone()
        if migrated is not None:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated = connection.execute(
                "SELECT value FROM evidence_meta WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            if migrated is not None:
                connection.execute("COMMIT")
                return

            existing_count = int(
                connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
            )
            legacy_path = cls.legacy_evidence_path(workdir)
            migrated_count = 0
            if existing_count == 0 and legacy_path.exists():
                try:
                    raw = json.loads(legacy_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise EvidenceStoreCorruptionError(
                        f"Legacy evidence JSON is unreadable: {legacy_path}: {exc}"
                    ) from exc
                if not isinstance(raw, list):
                    raise EvidenceStoreCorruptionError(
                        f"Legacy evidence JSON must contain a list: {legacy_path}"
                    )
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    record = EvidenceRecord.from_dict(item)
                    if not record.evidence_id or not record.content:
                        continue
                    cls._normalize_record(record)
                    cls._write_record(connection, record)
                    migrated_count += 1

            connection.execute(
                "INSERT INTO evidence_meta(key, value) VALUES(?, ?)",
                ("legacy_json_migrated", json.dumps({"at": _utc_now(), "count": migrated_count})),
            )
            connection.execute(
                "INSERT OR REPLACE INTO evidence_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(EVIDENCE_SCHEMA_VERSION)),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @classmethod
    def _legacy_json_records(cls, legacy_path: Path) -> list[dict[str, Any]]:
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise EvidenceStoreCorruptionError(
                f"Legacy evidence JSON is unreadable: {legacy_path}: {exc}"
            ) from exc
        if not isinstance(raw, list):
            raise EvidenceStoreCorruptionError(
                f"Legacy evidence JSON must contain a list: {legacy_path}"
            )
        return [dict(item) for item in raw if isinstance(item, dict)]

    @classmethod
    def _legacy_sqlite_records(cls, legacy_path: Path) -> list[dict[str, Any]]:
        try:
            legacy = sqlite_connect(
                f"file:{legacy_path.as_posix()}?mode=ro",
                uri=True,
                timeout=30.0,
            )
            legacy.row_factory = sqlite3.Row
        except Exception as exc:
            raise EvidenceStoreCorruptionError(
                f"Legacy evidence SQLite is unreadable: {legacy_path}: {exc}"
            ) from exc
        try:
            table = legacy.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_records'"
            ).fetchone()
            if table is None:
                return []
            rows = legacy.execute(
                "SELECT payload_json FROM evidence_records ORDER BY created_at, evidence_id"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EvidenceStoreCorruptionError(
                f"Legacy evidence SQLite is invalid: {legacy_path}: {exc}"
            ) from exc
        finally:
            legacy.close()

        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or ""))
            except Exception as exc:
                raise EvidenceStoreCorruptionError(
                    f"Legacy evidence SQLite row is invalid: {legacy_path}: {exc}"
                ) from exc
            if isinstance(payload, dict):
                records.append(payload)
        return records

    @classmethod
    def _import_legacy_record_payloads(
        cls,
        connection: sqlite3.Connection,
        payloads: Iterable[dict[str, Any]],
    ) -> int:
        migrated_count = 0
        for item in payloads:
            record = EvidenceRecord.from_dict(item)
            if not record.evidence_id or not record.content:
                continue
            cls._normalize_record(record)
            cls._write_record(connection, record)
            migrated_count += 1
        return migrated_count

    @classmethod
    def _migrate_previous_opencode_sources(
        cls,
        connection: sqlite3.Connection,
        workdir: Optional[str],
    ) -> None:
        """Import evidence from the temporary old .opencode location once."""

        meta_key = "previous_opencode_evidence_migrated"
        migrated = connection.execute(
            "SELECT value FROM evidence_meta WHERE key = ?",
            (meta_key,),
        ).fetchone()
        if migrated is not None:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated = connection.execute(
                "SELECT value FROM evidence_meta WHERE key = ?",
                (meta_key,),
            ).fetchone()
            if migrated is not None:
                connection.execute("COMMIT")
                return

            existing_count = int(
                connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
            )
            migrated_count = 0
            source = ""
            if existing_count == 0:
                for kind, path in (
                    ("sqlite", cls.previous_evidence_path(workdir)),
                    ("json", cls.previous_legacy_evidence_path(workdir)),
                ):
                    if not path.exists():
                        continue
                    payloads = (
                        cls._legacy_sqlite_records(path)
                        if kind == "sqlite"
                        else cls._legacy_json_records(path)
                    )
                    if not payloads and kind == "sqlite":
                        continue
                    migrated_count = cls._import_legacy_record_payloads(
                        connection,
                        payloads,
                    )
                    source = path.as_posix()
                    break

            connection.execute(
                "INSERT INTO evidence_meta(key, value) VALUES(?, ?)",
                (
                    meta_key,
                    json.dumps(
                        {
                            "at": _utc_now(),
                            "count": migrated_count,
                            "source": source,
                            "skipped_existing_records": existing_count if existing_count else 0,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @classmethod
    def _normalize_record(cls, record: EvidenceRecord) -> EvidenceRecord:
        record.status = record.status if record.status in EVIDENCE_STATUSES else "active"
        record.schema_version = EVIDENCE_SCHEMA_VERSION
        record.content_hash = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
        if not record.search_text:
            record.search_text = build_search_text(
                content="\n".join(
                    part
                    for part in [
                        record.title,
                        record.content,
                        "\n".join(record.claims),
                        " ".join(record.entities),
                        record.source_id,
                        record.source_path,
                        record.locator,
                    ]
                    if part
                ),
                tags=record.tags,
                fields={
                    "kind": record.kind,
                    "source_type": record.source_type,
                    "workflow_run_id": record.workflow_run_id,
                    "stage_id": record.stage_id,
                },
            )
        return record

    @classmethod
    def _prepare_records(
        cls,
        records: Iterable[EvidenceRecord],
        workdir: Optional[str],
    ) -> list[EvidenceRecord]:
        candidates = [cls._normalize_record(record) for record in records]
        missing = [record for record in candidates if not record.embedding or not record.embedding_version]
        if missing:
            vectors, version = build_embeddings_with_version(
                [record.search_text for record in missing],
                workdir=workdir,
            )
            for record, vector in zip(missing, vectors):
                record.embedding = vector
                record.embedding_version = version
        return candidates

    @classmethod
    def _write_record(cls, connection: sqlite3.Connection, record: EvidenceRecord) -> None:
        payload = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO evidence_records(
                evidence_id, kind, source_type, source_id, status, content_hash,
                created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(evidence_id) DO UPDATE SET
                kind=excluded.kind,
                source_type=excluded.source_type,
                source_id=excluded.source_id,
                status=excluded.status,
                content_hash=excluded.content_hash,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                record.evidence_id,
                record.kind,
                record.source_type,
                record.source_id,
                record.status,
                record.content_hash,
                record.created_at,
                record.updated_at,
                payload,
            ),
        )
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_fts'").fetchone():
            rowid = connection.execute("SELECT rowid FROM evidence_records WHERE evidence_id=?", (record.evidence_id,)).fetchone()[0]
            cls._index_record(connection, record, int(rowid))

    @classmethod
    def _record_from_row(cls, row: sqlite3.Row) -> EvidenceRecord:
        try:
            payload = json.loads(str(row["payload_json"] or ""))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            return EvidenceRecord.from_dict(payload)
        except Exception as exc:
            raise EvidenceStoreCorruptionError(
                f"Evidence record {row['evidence_id']} has an invalid payload: {exc}"
            ) from exc

    @classmethod
    def load(cls, workdir: Optional[str] = None) -> list[EvidenceRecord]:
        with cls._open(workdir) as connection:
            rows = connection.execute(
                "SELECT evidence_id, payload_json FROM evidence_records "
                "ORDER BY created_at, evidence_id"
            ).fetchall()
        return [cls._record_from_row(row) for row in rows]

    @classmethod
    def save(cls, records: list[EvidenceRecord], workdir: Optional[str] = None) -> None:
        candidates = cls._prepare_records(
            [record for record in records if record.evidence_id and record.content],
            workdir,
        )
        connection = cls._connect(workdir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM evidence_records")
            for record in candidates:
                cls._write_record(connection, record)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @classmethod
    def add(cls, record: EvidenceRecord, workdir: Optional[str] = None) -> EvidenceRecord:
        record = cls._prepare_records([record], workdir)[0]
        now = _utc_now()
        record.created_at = record.created_at or now
        record.updated_at = now
        connection = cls._connect(workdir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            while connection.execute(
                "SELECT 1 FROM evidence_records WHERE evidence_id = ?",
                (record.evidence_id,),
            ).fetchone():
                record.evidence_id = f"ev-{uuid.uuid4().hex[:12]}"
            cls._write_record(connection, record)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @classmethod
    def upsert(cls, record: EvidenceRecord, workdir: Optional[str] = None) -> EvidenceRecord:
        record = cls._prepare_records([record], workdir)[0]
        now = _utc_now()
        connection = cls._connect(workdir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT created_at FROM evidence_records WHERE evidence_id = ?",
                (record.evidence_id,),
            ).fetchone()
            record.created_at = str(row["created_at"] or "") if row else (record.created_at or now)
            record.updated_at = now
            cls._write_record(connection, record)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @classmethod
    def upsert_many(
        cls,
        new_records: list[EvidenceRecord],
        workdir: Optional[str] = None,
    ) -> list[EvidenceRecord]:
        """Prepare embeddings in one batch and upsert in one transaction."""
        candidates = [
            record for record in new_records if record.evidence_id and record.content
        ]
        if not candidates:
            return []
        candidates = cls._prepare_records(candidates, workdir)
        now = _utc_now()
        connection = cls._connect(workdir)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in candidates:
                row = connection.execute(
                    "SELECT created_at FROM evidence_records WHERE evidence_id = ?",
                    (record.evidence_id,),
                ).fetchone()
                record.created_at = str(row["created_at"] or "") if row else (record.created_at or now)
                record.updated_at = now
                cls._write_record(connection, record)
            connection.execute("COMMIT")
            return candidates
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @classmethod
    def get(cls, evidence_id: str, workdir: Optional[str] = None) -> EvidenceRecord | None:
        normalized = _clean_string(evidence_id)
        if not normalized:
            return None
        with cls._open(workdir) as connection:
            row = connection.execute(
                "SELECT evidence_id, payload_json FROM evidence_records WHERE evidence_id = ?",
                (normalized,),
            ).fetchone()
        return cls._record_from_row(row) if row else None

    @classmethod
    def get_many(
        cls,
        evidence_ids: Iterable[str],
        *,
        workdir: Optional[str] = None,
    ) -> dict[str, EvidenceRecord]:
        """Fetch evidence records in bounded SQL batches, keyed by stable ID."""

        identifiers = _clean_list(list(evidence_ids))
        if not identifiers:
            return {}
        result: dict[str, EvidenceRecord] = {}
        with cls._open(workdir) as connection:
            for offset in range(0, len(identifiers), 500):
                batch = identifiers[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT evidence_id,payload_json FROM evidence_records "
                    f"WHERE evidence_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    record = cls._record_from_row(row)
                    result[record.evidence_id] = record
        return result

    @classmethod
    def exists(cls, evidence_id: str, workdir: Optional[str] = None) -> bool:
        normalized = _clean_string(evidence_id)
        if not normalized:
            return False
        with cls._open(workdir) as connection:
            return connection.execute(
                "SELECT 1 FROM evidence_records WHERE evidence_id = ?",
                (normalized,),
            ).fetchone() is not None

    @classmethod
    def set_status(
        cls,
        evidence_id: str,
        status: str,
        *,
        workdir: Optional[str] = None,
    ) -> EvidenceRecord | None:
        """Update lifecycle state without changing the stable Evidence ID."""

        normalized_status = _clean_string(status).lower()
        if normalized_status not in EVIDENCE_STATUSES:
            raise ValueError(f"Unsupported evidence status: {status}")
        record = cls.get(evidence_id, workdir=workdir)
        if record is None:
            return None
        record.status = normalized_status
        return cls.upsert(record, workdir=workdir)

    @classmethod
    def _add_graph_node(
        cls,
        nodes: dict[str, dict[str, Any]],
        *,
        node_id: str,
        node_type: str,
        label: str,
        evidence_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not node_id or not label:
            return
        node = nodes.setdefault(
            node_id,
            {
                "node_id": node_id,
                "type": node_type,
                "label": label,
                "evidence_ids": [],
                "metadata": {},
            },
        )
        if evidence_id and evidence_id not in node["evidence_ids"]:
            node["evidence_ids"].append(evidence_id)
        if metadata:
            node["metadata"].update(metadata)

    @classmethod
    def _add_graph_edge(
        cls,
        edges: dict[str, dict[str, Any]],
        *,
        source: str,
        target: str,
        edge_type: str,
        evidence_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not source or not target or not edge_type:
            return
        edge_id = f"{edge_type}:{source}->{target}"
        edge = edges.setdefault(
            edge_id,
            {
                "edge_id": edge_id,
                "source": source,
                "target": target,
                "type": edge_type,
                "evidence_ids": [],
                "metadata": {},
            },
        )
        if evidence_id and evidence_id not in edge["evidence_ids"]:
            edge["evidence_ids"].append(evidence_id)
        if metadata:
            edge["metadata"].update(metadata)

    @classmethod
    def build_graph(
        cls,
        workdir: Optional[str] = None,
        *,
        persist: bool = True,
        include_semantic_hints: bool = False,
    ) -> dict[str, Any]:
        """Build a provenance graph; legacy claim/entity hints are opt-in.

        Paper Wiki is the owner of the semantic knowledge graph.  Keeping this
        graph provenance-first prevents two independently derived claim graphs
        from drifting apart.
        """
        graph = cls._graph_from_records(cls.load(workdir), include_semantic_hints=include_semantic_hints)
        if persist:
            path = cls.graph_path(workdir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return graph

    @classmethod
    def _graph_from_records(
        cls, records: list[EvidenceRecord], *, include_semantic_hints: bool = False,
    ) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}

        for record in records:
            evidence_node_id = f"evidence:{record.evidence_id}"
            cls._add_graph_node(
                nodes,
                node_id=evidence_node_id,
                node_type="evidence",
                label=record.title or record.evidence_id,
                evidence_id=record.evidence_id,
                metadata={
                    "kind": record.kind,
                    "confidence": record.confidence,
                    "source_type": record.source_type,
                    "status": record.status,
                    "content_hash": record.content_hash,
                },
            )

            source_label = record.source_id or record.source_path
            if source_label:
                source_node_id = _graph_node_id("source", f"{record.source_type}:{source_label}")
                cls._add_graph_node(
                    nodes,
                    node_id=source_node_id,
                    node_type="source",
                    label=source_label,
                    evidence_id=record.evidence_id,
                    metadata={"source_type": record.source_type},
                )
                cls._add_graph_edge(
                    edges,
                    source=source_node_id,
                    target=evidence_node_id,
                    edge_type="contains_evidence",
                    evidence_id=record.evidence_id,
                )

            if record.source_path:
                artifact_node_id = _graph_node_id("artifact", record.source_path)
                cls._add_graph_node(
                    nodes,
                    node_id=artifact_node_id,
                    node_type="artifact",
                    label=record.source_path,
                    evidence_id=record.evidence_id,
                )
                cls._add_graph_edge(
                    edges,
                    source=evidence_node_id,
                    target=artifact_node_id,
                    edge_type="points_to_artifact",
                    evidence_id=record.evidence_id,
                )

            for artifact_ref in sorted(set(_iter_artifact_refs(record.artifact_refs))):
                if artifact_ref == record.source_path:
                    continue
                artifact_node_id = _graph_node_id("artifact", artifact_ref)
                cls._add_graph_node(
                    nodes,
                    node_id=artifact_node_id,
                    node_type="artifact",
                    label=artifact_ref,
                    evidence_id=record.evidence_id,
                    metadata={"basename": Path(artifact_ref).name},
                )
                cls._add_graph_edge(
                    edges,
                    source=evidence_node_id,
                    target=artifact_node_id,
                    edge_type="has_artifact",
                    evidence_id=record.evidence_id,
                )

            if record.workflow_run_id:
                run_node_id = _graph_node_id("workflow_run", record.workflow_run_id)
                cls._add_graph_node(
                    nodes,
                    node_id=run_node_id,
                    node_type="workflow_run",
                    label=record.workflow_run_id,
                    evidence_id=record.evidence_id,
                )
                cls._add_graph_edge(
                    edges,
                    source=run_node_id,
                    target=evidence_node_id,
                    edge_type="produced_evidence",
                    evidence_id=record.evidence_id,
                )

            if record.stage_id:
                stage_node_id = _graph_node_id("stage", record.stage_id)
                cls._add_graph_node(
                    nodes,
                    node_id=stage_node_id,
                    node_type="stage",
                    label=record.stage_id,
                    evidence_id=record.evidence_id,
                )
                cls._add_graph_edge(
                    edges,
                    source=stage_node_id,
                    target=evidence_node_id,
                    edge_type="produced_evidence",
                    evidence_id=record.evidence_id,
                )

            entity_nodes: list[str] = []
            for entity in record.entities if include_semantic_hints else []:
                entity_type = _classify_entity(entity)
                entity_node_id = _graph_node_id(entity_type, entity)
                entity_nodes.append(entity_node_id)
                cls._add_graph_node(
                    nodes,
                    node_id=entity_node_id,
                    node_type=entity_type,
                    label=entity,
                    evidence_id=record.evidence_id,
                )
                cls._add_graph_edge(
                    edges,
                    source=evidence_node_id,
                    target=entity_node_id,
                    edge_type="mentions_entity",
                    evidence_id=record.evidence_id,
                )

            for claim in record.claims if include_semantic_hints else []:
                claim_node_id = _graph_node_id("claim", claim)
                cls._add_graph_node(
                    nodes,
                    node_id=claim_node_id,
                    node_type="claim",
                    label=claim,
                    evidence_id=record.evidence_id,
                )
                cls._add_graph_edge(
                    edges,
                    source=evidence_node_id,
                    target=claim_node_id,
                    edge_type="supports_claim",
                    evidence_id=record.evidence_id,
                )
                claim_lower = claim.lower()
                for entity_node_id in entity_nodes:
                    entity_label = str(nodes.get(entity_node_id, {}).get("label") or "")
                    if entity_label and entity_label.lower() in claim_lower:
                        cls._add_graph_edge(
                            edges,
                            source=claim_node_id,
                            target=entity_node_id,
                            edge_type="about_entity",
                            evidence_id=record.evidence_id,
                        )

            for citation in record.citations:
                locator = _citation_locator(citation)
                if not locator:
                    continue
                anchor_node_id = _graph_node_id(
                    "anchor",
                    f"{record.evidence_id}:{locator}",
                )
                cls._add_graph_node(
                    nodes,
                    node_id=anchor_node_id,
                    node_type="anchor",
                    label=locator,
                    evidence_id=record.evidence_id,
                    metadata={
                        key: value
                        for key, value in citation.items()
                        if key in {"doc_id", "fact_id", "page", "table_id", "row_index", "chunk_id"}
                    },
                )
                cls._add_graph_edge(
                    edges,
                    source=evidence_node_id,
                    target=anchor_node_id,
                    edge_type="cites_anchor",
                    evidence_id=record.evidence_id,
                )

            paper_fact = dict(record.metadata.get("paper_fact") or {})
            fact_id = _clean_string(paper_fact.get("fact_id"))
            if fact_id:
                fact_node_id = _graph_node_id("paper_fact", fact_id)
                fact_label = " / ".join(
                    part
                    for part in [
                        _clean_string(paper_fact.get("fact_type")),
                        _clean_string(paper_fact.get("label")),
                        _clean_string(paper_fact.get("value")),
                    ]
                    if part
                ) or fact_id
                cls._add_graph_node(
                    nodes,
                    node_id=fact_node_id,
                    node_type="paper_fact",
                    label=fact_label,
                    evidence_id=record.evidence_id,
                    metadata={"fact_id": fact_id},
                )
                cls._add_graph_edge(
                    edges,
                    source=fact_node_id,
                    target=evidence_node_id,
                    edge_type="materialized_as_evidence",
                    evidence_id=record.evidence_id,
                )

            paper_chunk = dict(record.metadata.get("paper_chunk") or {})
            chunk_id = _clean_string(paper_chunk.get("chunk_id"))
            if chunk_id:
                chunk_node_id = _graph_node_id("paper_chunk", chunk_id)
                chunk_label = " / ".join(
                    part
                    for part in [
                        _clean_string(paper_chunk.get("kind")),
                        _clean_string(paper_chunk.get("page") and f"page {paper_chunk.get('page')}"),
                        chunk_id,
                    ]
                    if part
                ) or chunk_id
                cls._add_graph_node(
                    nodes,
                    node_id=chunk_node_id,
                    node_type="paper_chunk",
                    label=chunk_label,
                    evidence_id=record.evidence_id,
                    metadata={"chunk_id": chunk_id},
                )
                cls._add_graph_edge(
                    edges,
                    source=chunk_node_id,
                    target=evidence_node_id,
                    edge_type="materialized_as_evidence",
                    evidence_id=record.evidence_id,
                )

            paper_visual = dict(record.metadata.get("paper_visual_artifact") or {})
            visual_id = _clean_string(
                paper_visual.get("figure_id")
                or paper_visual.get("table_id")
                or paper_visual.get("page")
                or paper_visual.get("image_path")
            )
            if visual_id:
                visual_node_id = _graph_node_id("paper_visual", f"{record.evidence_id}:{visual_id}")
                visual_label = " / ".join(
                    part
                    for part in [
                        _clean_string(paper_visual.get("kind")),
                        _clean_string(paper_visual.get("page") and f"page {paper_visual.get('page')}"),
                        _clean_string(paper_visual.get("table_id") and f"table {paper_visual.get('table_id')}"),
                        _clean_string(paper_visual.get("figure_id") and f"figure {paper_visual.get('figure_id')}"),
                    ]
                    if part
                ) or visual_id
                cls._add_graph_node(
                    nodes,
                    node_id=visual_node_id,
                    node_type="paper_visual",
                    label=visual_label,
                    evidence_id=record.evidence_id,
                    metadata={key: value for key, value in paper_visual.items() if key != "image_path"},
                )
                cls._add_graph_edge(
                    edges,
                    source=visual_node_id,
                    target=evidence_node_id,
                    edge_type="materialized_as_evidence",
                    evidence_id=record.evidence_id,
                )

        conflicts = _detect_conflicts(records) if include_semantic_hints else []
        for conflict in conflicts:
            left_id = str(conflict.get("left_evidence_id") or "")
            right_id = str(conflict.get("right_evidence_id") or "")
            if not left_id or not right_id:
                continue
            cls._add_graph_edge(
                edges,
                source=f"evidence:{left_id}",
                target=f"evidence:{right_id}",
                edge_type="contradicts",
                evidence_id=left_id,
                metadata=conflict,
            )

        graph = {
            "schema_version": 2,
            "graph_kind": "provenance_with_semantic_hints" if include_semantic_hints else "provenance",
            "updated_at": _utc_now(),
            "record_count": len(records),
            "conflict_count": len(conflicts),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": sorted(nodes.values(), key=lambda item: (str(item.get("type")), str(item.get("label")))),
            "edges": sorted(edges.values(), key=lambda item: (str(item.get("type")), str(item.get("edge_id")))),
        }
        return graph

    @classmethod
    def graph_neighborhood(
        cls,
        evidence_id: str,
        *,
        workdir: Optional[str] = None,
        depth: int = 1,
        limit: int = 60,
        include_semantic_hints: bool = False,
    ) -> dict[str, Any]:
        try:
            safe_limit = max(5, min(200, int(limit or 60)))
        except (ValueError, TypeError):
            safe_limit = 60
        try:
            safe_depth = max(1, min(3, int(depth or 1)))
        except (ValueError, TypeError):
            safe_depth = 1
        if include_semantic_hints:
            graph = cls.build_graph(workdir=workdir, persist=False, include_semantic_hints=True)
        else:
            # 从索引查找共享来源的记录；单条详情不读取或构建全库图谱。
            identifiers = {_clean_string(evidence_id)} if evidence_id else set()
            frontier_ids = set(identifiers)
            with cls._open(workdir) as connection:
                from .maintenance import active_tables
                connection.execute('BEGIN')
                graph_table = active_tables(connection)[2]
                total = int(connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
                for _ in range(safe_depth - 1):
                    if not frontier_ids:
                        break
                    placeholders = ",".join("?" for _ in frontier_ids)
                    rows = connection.execute(
                        f"SELECT DISTINCT other.evidence_id FROM {graph_table} AS seed "
                        f"JOIN {graph_table} AS other ON other.node_id=seed.node_id "
                        f"WHERE seed.evidence_id IN ({placeholders}) ORDER BY other.evidence_id LIMIT ?",
                        [*sorted(frontier_ids), safe_limit],
                    ).fetchall()
                    discovered = {str(row[0]) for row in rows} - identifiers
                    frontier_ids = set(sorted(discovered)[:max(0, safe_limit - len(identifiers))])
                    identifiers.update(frontier_ids)
            graph = cls._graph_from_records(list(cls.get_many(sorted(identifiers), workdir=workdir).values()))
            graph["record_count"] = total
        start = f"evidence:{_clean_string(evidence_id)}"
        if not evidence_id or start not in {str(node.get("node_id")) for node in graph.get("nodes", [])}:
            return {
                **graph,
                "nodes": [],
                "edges": [],
                "conflict_count": 0,
                "start_node_id": start,
                "depth": depth,
            }

        adjacency: dict[str, set[str]] = {}
        edges_by_node: dict[str, list[dict[str, Any]]] = {}
        for edge in graph.get("edges", []):
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if not source or not target:
                continue
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)
            edges_by_node.setdefault(source, []).append(edge)
            edges_by_node.setdefault(target, []).append(edge)

        visited = {start}
        frontier = {start}
        selected_edges: dict[str, dict[str, Any]] = {}
        for _ in range(safe_depth):
            next_frontier: set[str] = set()
            for node_id in sorted(frontier):
                for edge in edges_by_node.get(node_id, []):
                    selected_edges[str(edge.get("edge_id") or "")] = edge
                for neighbor in sorted(adjacency.get(node_id, set())):
                    if len(visited) >= safe_limit:
                        break
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier or len(visited) >= safe_limit:
                break

        nodes = [
            node
            for node in graph.get("nodes", [])
            if str(node.get("node_id") or "") in visited
        ][:safe_limit]
        node_ids = {str(node.get("node_id") or "") for node in nodes}
        edges = [
            edge
            for edge in selected_edges.values()
            if str(edge.get("source") or "") in node_ids and str(edge.get("target") or "") in node_ids
        ][:safe_limit]
        return {
            "schema_version": graph.get("schema_version", 1),
            "updated_at": graph.get("updated_at", ""),
            "record_count": graph.get("record_count", 0),
            "conflict_count": sum(1 for edge in edges if str(edge.get("type") or "") == "contradicts"),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "start_node_id": start,
            "depth": safe_depth,
            "nodes": nodes,
            "edges": edges,
            "truncated": len(visited) >= safe_limit or len(selected_edges) > len(edges),
        }

    @classmethod
    def doctor(
        cls,
        *,
        workdir: Optional[str] = None,
        evidence_id: str = "",
        text: str = "",
        persist: bool = True,
        include_semantic_hints: bool = False,
        record_limit: int | None = None,
        verify_assets: bool = False,
    ) -> dict[str, Any]:
        target_id = _clean_string(evidence_id)
        referenced_ids = extract_evidence_ids(text)
        scoped = bool(target_id or record_limit is not None)
        known_reference_ids: set[str] = set()
        references_truncated = False
        try:
            with cls._open(workdir) as connection:
                total_records = int(connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
                if referenced_ids and scoped:
                    for start in range(0, len(referenced_ids), 400):
                        batch = referenced_ids[start:start + 400]
                        placeholders = ','.join('?' for _ in batch)
                        known_reference_ids.update(str(row[0]) for row in connection.execute(f"SELECT evidence_id FROM evidence_records WHERE evidence_id IN ({placeholders})", batch))
                if not scoped:
                    integrity_messages = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
                    if integrity_messages != ["ok"]:
                        raise EvidenceStoreCorruptionError("; ".join(integrity_messages))
            if target_id or (referenced_ids and record_limit is not None):
                requested_ids = list(dict.fromkeys([item for item in [target_id, *referenced_ids] if item]))
                read_limit = max(1, min(500, int(record_limit))) if record_limit is not None else 500
                references_truncated = len(requested_ids) > read_limit
                records = list(cls.get_many(requested_ids[:read_limit], workdir=workdir).values())
            elif record_limit is not None:
                safe_record_limit = max(1, min(500, int(record_limit)))
                with cls._open(workdir) as connection:
                    rows = connection.execute("SELECT evidence_id,payload_json FROM evidence_records ORDER BY created_at,evidence_id LIMIT ?", (safe_record_limit,)).fetchall()
                records = [cls._record_from_row(row) for row in rows]
            else:
                records = cls.load(workdir)
        except (EvidenceStoreCorruptionError, sqlite3.DatabaseError) as exc:
            report = {
                "schema_version": 2,
                "updated_at": _utc_now(),
                "status": "fail",
                "record_count": 0,
                "checked_record_count": 0,
                "graph_node_count": 0,
                "graph_edge_count": 0,
                "conflict_count": 0,
                "issue_count": 1,
                "counts": {"error": 1, "warn": 0, "info": 0},
                "storage_integrity": "failed",
                "issues": [
                    {
                        "severity": "error",
                        "code": "storage_corruption",
                        "message": f"Evidence ledger integrity check failed: {exc}",
                        "evidence_id": "",
                        "metadata": {},
                    }
                ],
            }
            if persist and not scoped:
                path = cls.doctor_path(workdir)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return report
        record_map = {record.evidence_id: record for record in records}
        target_id = _clean_string(evidence_id)
        target_records = ([record_map[target_id]] if target_id in record_map else []) if target_id else records
        issues: list[dict[str, Any]] = []

        def add_issue(
            severity: str,
            code: str,
            message: str,
            *,
            record: EvidenceRecord | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            issues.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message,
                    "evidence_id": record.evidence_id if record else "",
                    "metadata": metadata or {},
                }
            )

        if target_id and target_id not in record_map:
            add_issue(
                "error",
                "unknown_evidence_id",
                f"Evidence id does not exist: {target_id}",
            )

        asset_counts = {'file_count': 0, 'checked_file_count': 0, 'verified_file_count': 0}
        assets_truncated = False
        verified_paths: set[str] = set()
        for record in target_records:
            from .assets import check_assets
            asset_report = check_assets(record, workdir=cls._resolve_workdir(workdir), verify=verify_assets, verified_paths=verified_paths)
            for key in asset_counts:
                asset_counts[key] += asset_report[key]
            issues.extend(asset_report['issues'])
            if asset_report['truncated']:
                assets_truncated = True
                add_issue('warn', 'asset_checks_truncated', 'Only a bounded subset of source files was checked.', record=record)
            if not (record.source_id or record.source_path):
                add_issue(
                    "warn",
                    "missing_source",
                    "Evidence has neither source_id nor source_path.",
                    record=record,
                )
            if record.source_type.lower() == "paper" and not _has_citation_locator(record):
                add_issue(
                    "warn",
                    "missing_locator",
                    "Paper evidence has no page/table/chunk locator.",
                    record=record,
                )
            if record.source_type.lower() == "paper" and not record.citations and not record.metadata.get("paper_anchor"):
                add_issue(
                    "warn",
                    "missing_anchor",
                    "Paper evidence has no citation anchor metadata.",
                    record=record,
                )
            if include_semantic_hints and not record.claims:
                add_issue(
                    "warn",
                    "missing_claims",
                    "Evidence has no normalized claims.",
                    record=record,
                )
            if include_semantic_hints and not record.entities:
                add_issue(
                    "info",
                    "missing_entities",
                    "Evidence has no extracted entities.",
                    record=record,
                )
            if not record.search_text:
                add_issue(
                    "warn",
                    "missing_search_text",
                    "Evidence search_text is missing and should be rebuilt.",
                    record=record,
                )
            if not record.embedding:
                add_issue(
                    "info",
                    "missing_embedding",
                    "Evidence embedding is missing; search may fall back to lexical matching.",
                    record=record,
                )
            if record.embedding and not record.embedding_version:
                add_issue(
                    "info",
                    "unknown_embedding_version",
                    "Evidence embedding has no version and will use lexical ranking.",
                    record=record,
                )
            expected_hash = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            if not record.content_hash or record.content_hash != expected_hash:
                add_issue(
                    "error",
                    "content_hash_mismatch",
                    "Evidence content hash is missing or does not match its payload.",
                    record=record,
                )
            if record.status != "active":
                add_issue(
                    "warn",
                    f"evidence_{record.status}",
                    f"Evidence lifecycle status is {record.status}.",
                    record=record,
                )

        content_signatures: dict[str, list[str]] = {}
        for record in target_records:
            signature_text = re.sub(r"\s+", " ", record.content).strip().lower()
            if not signature_text:
                continue
            signature = _fingerprint(signature_text)
            content_signatures.setdefault(signature, []).append(record.evidence_id)
        for evidence_ids in content_signatures.values():
            if len(evidence_ids) > 1:
                add_issue(
                    "warn",
                    "duplicate_content",
                    "Multiple evidence records have the same normalized content.",
                    metadata={"evidence_ids": evidence_ids},
                )

        graph = cls._graph_from_records(target_records)
        edge_node_ids = {
            str(edge.get("source") or "")
            for edge in graph.get("edges", [])
        } | {
            str(edge.get("target") or "")
            for edge in graph.get("edges", [])
        }
        for record in target_records:
            node_id = f"evidence:{record.evidence_id}"
            if node_id not in edge_node_ids:
                add_issue(
                    "warn",
                    "orphan_evidence_node",
                    "Evidence node has no graph relationships beyond itself.",
                    record=record,
                    metadata={"node_id": node_id},
                )

        conflicts = _detect_conflicts(records) if include_semantic_hints else []
        reported_conflicts: list[dict[str, Any]] = []
        target_ids = {record.evidence_id for record in target_records}
        for conflict in conflicts:
            left_id = str(conflict.get("left_evidence_id") or "")
            right_id = str(conflict.get("right_evidence_id") or "")
            if target_id and target_id not in {left_id, right_id}:
                continue
            reported_conflicts.append(conflict)
            issue_record = record_map.get(left_id if left_id in target_ids else right_id)
            add_issue(
                "warn",
                "conflicting_evidence",
                "Potential contradiction between evidence claims sharing entities.",
                record=issue_record,
                metadata=conflict,
            )

        unknown_refs: list[str] = []
        for ref_id in extract_evidence_ids(text):
            if ref_id not in record_map and ref_id not in known_reference_ids and ref_id not in unknown_refs:
                unknown_refs.append(ref_id)
                add_issue(
                    "error",
                    "unknown_evidence_ref",
                    f"Text references unknown evidence id: {ref_id}",
                    metadata={"evidence_id": ref_id},
                )

        severity_rank = {"error": 3, "warn": 2, "info": 1}
        highest = max((severity_rank.get(issue["severity"], 0) for issue in issues), default=0)
        status = "fail" if highest >= 3 else "warn" if highest >= 2 else "pass"
        counts = {
            "error": sum(1 for issue in issues if issue["severity"] == "error"),
            "warn": sum(1 for issue in issues if issue["severity"] == "warn"),
            "info": sum(1 for issue in issues if issue["severity"] == "info"),
        }
        report = {
            "schema_version": 2,
            "updated_at": _utc_now(),
            "status": status,
            "record_count": total_records,
            "checked_record_count": len(target_records),
            "graph_node_count": int(graph.get("node_count") or 0),
            "graph_edge_count": int(graph.get("edge_count") or 0),
            "conflict_count": len(reported_conflicts),
            "issue_count": len(issues),
            "storage_integrity": "not_checked" if scoped else "ok",
            "scope": "record" if target_id else "references" if referenced_ids and record_limit is not None else "workspace_sample" if record_limit is not None else "workspace",
            "truncated": assets_truncated or references_truncated or (scoped and not (target_id or referenced_ids) and len(records) < total_records),
            "counts": counts,
            "issues": issues,
            "asset_verification": "full" if verify_assets else "lightweight",
            "asset_checks": asset_counts,
        }
        if persist and not scoped:
            path = cls.doctor_path(workdir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report

    @classmethod
    def search(
        cls,
        *,
        query: str = "",
        workdir: Optional[str] = None,
        limit: int = 10,
        kind: str = "",
        tags: Iterable[str] | None = None,
        entities: Iterable[str] | None = None,
        source_type: str = "",
        stage_id: str = "",
        workflow_run_id: str = "",
        include_inactive: bool = False,
        allowed_ids: Iterable[str] | None = None,
    ) -> list[EvidenceRecord]:
        try:
            safe_limit = max(1, min(100, int(limit or 10)))
        except (ValueError, TypeError):
            safe_limit = 10
        query_text = _clean_string(query)[:1000]
        tokens = list(dict.fromkeys(_search_tokens(query_text)))[:32]
        if query_text and not tokens:
            return []
        required_tags = _clean_list(list(tags or []))
        required_entities = _clean_list(list(entities or []))
        where: list[str] = []
        parameters: list[Any] = []
        if not include_inactive:
            where.append("e.status='active'")
        for column, value in (("kind", kind), ("source_type", source_type), ("stage_id", stage_id), ("workflow_run_id", workflow_run_id)):
            normalized = _clean_string(value)
            if normalized:
                if column in {"kind", "source_type"}:
                    where.append(f"lower(e.{column})=?")
                    normalized = normalized.lower()
                else:
                    where.append(f"e.{column}=?")
                parameters.append(normalized)
        for facet, values in (("tag", required_tags), ("entity", required_entities)):
            for value in values:
                where.append("EXISTS(SELECT 1 FROM _FACETS_ f WHERE f.evidence_id=e.evidence_id AND f.facet=? AND f.value=?)")
                parameters.extend([facet, value])
        with cls._open(workdir) as connection:
            from .maintenance import active_tables, meta
            connection.execute('BEGIN')
            fts, facets, _ = active_tables(connection)
            where = [clause.replace('_FACETS_', facets) for clause in where]
            if allowed_ids is not None:
                allowed = _clean_list(list(allowed_ids))
                if not allowed:
                    return []
                # 大量授权 ID 使用临时索引，避免超出 SQLite 参数数量上限。
                connection.execute("CREATE TEMP TABLE allowed_evidence(id TEXT PRIMARY KEY)")
                connection.executemany("INSERT INTO allowed_evidence VALUES(?)", [(item,) for item in allowed])
                where.append("e.evidence_id IN (SELECT id FROM allowed_evidence)")
            eligible_where = list(where)
            eligible_parameters = list(parameters)
            vector_path = meta(connection, 'vector_path')
            vector_version = meta(connection, 'vector_version')
            join = ""
            order = "e.created_at,e.evidence_id"
            candidate_limit = safe_limit
            if tokens:
                join = f" JOIN {fts} ON {fts}.rowid=e.rowid"
                where.append(f"{fts} MATCH ?")
                parameters.append(" OR ".join('"' + token.replace('"', '""') + '"' for token in tokens))
                order = f"{fts}.rank"
                candidate_limit = _search_candidate_limit(safe_limit)
            predicate = " AND ".join(where) or "1"
            rows = connection.execute(
                "SELECT e.evidence_id,e.payload_json FROM evidence_records e" + join
                + " WHERE " + predicate + " ORDER BY " + order + " LIMIT ?",
                [*parameters, candidate_limit],
            ).fetchall()
            selective = bool(required_tags or required_entities or kind or source_type or stage_id or workflow_run_id or allowed_ids is not None)
            eligible = None
            if selective and query_text and vector_path and os.getenv('TCODE_EVIDENCE_SEMANTIC_SEARCH', '1') != '0':
                eligible = connection.execute("SELECT e.evidence_id,e.updated_at,e.content_hash FROM evidence_records e WHERE " + (' AND '.join(eligible_where) or '1'), eligible_parameters).fetchall()
        filtered = [cls._record_from_row(row) for row in rows]
        if not query_text:
            return filtered
        semantic_ids: list[str] = []
        semantic_digests: dict[str, str] = {}
        if vector_path and os.getenv('TCODE_EVIDENCE_SEMANTIC_SEARCH', '1') != '0' and (eligible is None or eligible):
            from ..memory.embedding_provider import EmbeddingProvider
            from ..memory.vector_store import FaissVectorStore
            if EmbeddingProvider.is_enabled(workdir) and FaissVectorStore.available():
                vector, actual = build_embedding_with_version(query_text, workdir=workdir)
                if actual == vector_version:
                    try:
                        budget = _search_candidate_limit(safe_limit)
                        base = Path(vector_path)
                        if not FaissVectorStore.exists(base):
                            raise OSError('Evidence semantic index is missing')
                        if eligible is None:
                            hits = FaissVectorStore.search(base_path=base, query_vector=vector, limit=budget, expected_embedding_version=actual)
                            index_digests = FaissVectorStore.record_digest_map(base)
                            def valid_hits(hits):
                                valid = set()
                                with cls._open(workdir) as current:
                                    for offset in range(0, len(hits), 400):
                                        ids = [identifier for identifier, _ in hits[offset:offset + 400]]
                                        placeholders = ','.join('?' for _ in ids)
                                        if not ids:
                                            continue
                                        for row in current.execute(f'SELECT evidence_id,updated_at,content_hash,status FROM evidence_records WHERE evidence_id IN ({placeholders})', ids):
                                            digest = hashlib.sha256((row['updated_at'] + row['content_hash']).encode()).hexdigest()
                                            if (include_inactive or row['status'] == 'active') and index_digests.get(row['evidence_id']) == digest:
                                                valid.add(row['evidence_id'])
                                return [hit for hit in hits if hit[0] in valid]
                            raw_count = len(hits)
                            hits = valid_hits(hits)
                            if len(hits) < safe_limit and raw_count >= budget:
                                # Rare heavily withdrawn/stale neighborhoods use an exact
                                # eligible subset; normal queries only inspect ANN candidates.
                                with cls._open(workdir) as current:
                                    eligible = current.execute('SELECT evidence_id,updated_at,content_hash FROM evidence_records' + ('' if include_inactive else " WHERE status='active'")).fetchall()
                        if eligible is not None:
                            digests = {str(r['evidence_id']): hashlib.sha256((r['updated_at'] + r['content_hash']).encode()).hexdigest() for r in eligible}
                            hits = FaissVectorStore.search(base_path=base, query_vector=vector, limit=budget, expected_embedding_version=actual, allowed_ids=set(digests), expected_digests=digests)
                        try:
                            minimum = max(-1.0, min(1.0, float(os.getenv('TCODE_EVIDENCE_SEMANTIC_MIN_SCORE', '0.25'))))
                        except ValueError:
                            minimum = 0.25
                        semantic_ids = [identifier for identifier, score in hits if score >= minimum]
                        source_digests = digests if eligible is not None else index_digests
                        semantic_digests = {identifier: source_digests.get(identifier, '') for identifier in semantic_ids}
                    except (OSError, RuntimeError, ValueError):
                        semantic_ids = []
                        with cls._open(workdir) as connection:
                            from .maintenance import put_meta
                            put_meta(connection, 'vector_revision', '-1')
                            put_meta(connection, 'semantic_query_error_until', time.time() + 60)
                elif actual.startswith('hash-'):
                    with cls._open(workdir) as connection:
                        from .maintenance import put_meta
                        put_meta(connection, 'semantic_query_error_until', time.time() + 60)
        if semantic_ids:
            candidates = cls.get_many([*[r.evidence_id for r in filtered], *semantic_ids], workdir=workdir)
            # Reciprocal rank fusion avoids comparing BM25 with cosine scales.
            scores: dict[str, float] = {}
            for ranking in ([r.evidence_id for r in filtered], semantic_ids):
                for position, identifier in enumerate(ranking, 1):
                    scores[identifier] = scores.get(identifier, 0) + 1 / (60 + position)
            # Revalidate mutable lifecycle after vector search, before returning records.
            def matches(record):
                return ((include_inactive or record.status == 'active')
                        and (not _clean_string(kind) or record.kind.lower() == _clean_string(kind).lower())
                        and (not _clean_string(source_type) or record.source_type.lower() == _clean_string(source_type).lower())
                        and (not _clean_string(stage_id) or record.stage_id == _clean_string(stage_id))
                        and (not _clean_string(workflow_run_id) or record.workflow_run_id == _clean_string(workflow_run_id))
                        and set(required_tags).issubset(record.tags)
                        and set(required_entities).issubset(record.entities)
                        and (allowed_ids is None or record.evidence_id in allowed)
                        and (record.evidence_id not in semantic_digests or hashlib.sha256((record.updated_at + record.content_hash).encode()).hexdigest() == semantic_digests[record.evidence_id]))
            return [candidates[i] for i in sorted(scores, key=lambda i: (-scores[i], i)) if i in candidates and matches(candidates[i])][:safe_limit]
        if not filtered:
            return []
        ranked = semantic_rank(
            query=query_text,
            items=filtered,
            text_getter=lambda item: item.search_text or item.content,
            embedding_getter=lambda item: item.embedding,
            embedding_version_getter=lambda item: item.embedding_version,
            workdir=workdir,
        )
        return ranked[:safe_limit]


def format_evidence_records(records: Iterable[EvidenceRecord], *, content_chars: int = 300) -> str:
    lines: list[str] = []
    for index, record in enumerate(records, 1):
        marker = f"[evidence:{record.evidence_id}]"
        title = f" {record.title}" if record.title else ""
        source = " / ".join(
            part
            for part in [record.source_type, record.source_id, record.locator or record.source_path]
            if part
        )
        source_text = f" ({source})" if source else ""
        content = record.content.replace("\n", " ").strip()
        if len(content) > content_chars:
            content = content[: max(0, content_chars - 3)].rstrip() + "..."
        lines.append(f"{index}. {marker} {record.kind}{title}{source_text}: {content}")
        if record.claims:
            claims = "; ".join(record.claims[:3])
            lines.append(f"   claims: {claims}")
        if record.entities:
            lines.append(f"   entities: {', '.join(record.entities[:8])}")
    return "\n".join(lines)


def format_evidence_graph(graph: dict[str, Any], *, max_nodes: int = 20, max_edges: int = 30) -> str:
    lines = [
        (
            f"Evidence graph: {int(graph.get('record_count') or 0)} records, "
            f"{int(graph.get('node_count') or 0)} nodes, {int(graph.get('edge_count') or 0)} edges, "
            f"{int(graph.get('conflict_count') or 0)} conflicts"
        )
    ]
    nodes = list(graph.get("nodes") or [])[:max_nodes]
    edges = list(graph.get("edges") or [])[:max_edges]
    if nodes:
        lines.append("Nodes:")
        for node in nodes:
            evidence_ids = list(node.get("evidence_ids") or [])
            refs = f" evidence={','.join(evidence_ids[:3])}" if evidence_ids else ""
            lines.append(
                f"- {node.get('type')}: {node.get('label')} ({node.get('node_id')}){refs}"
            )
    if edges:
        label_by_id = {
            str(node.get("node_id") or ""): str(node.get("label") or "")
            for node in graph.get("nodes") or []
            if isinstance(node, dict)
        }
        lines.append("Edges:")
        for edge in edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            source_label = label_by_id.get(source, source)
            target_label = label_by_id.get(target, target)
            lines.append(f"- {source_label} --{edge.get('type')}--> {target_label}")
    return "\n".join(lines)


def format_evidence_doctor_report(report: dict[str, Any], *, max_issues: int = 30) -> str:
    counts = dict(report.get("counts") or {})
    lines = [
        (
            f"Evidence doctor: {str(report.get('status') or 'unknown').upper()} "
            f"({int(report.get('issue_count') or 0)} issues; "
            f"errors={int(counts.get('error') or 0)}, "
            f"warnings={int(counts.get('warn') or 0)}, "
            f"info={int(counts.get('info') or 0)})"
        ),
        (
            f"Checked {int(report.get('checked_record_count') or 0)} of "
            f"{int(report.get('record_count') or 0)} records; graph has "
            f"{int(report.get('graph_node_count') or 0)} nodes and "
            f"{int(report.get('graph_edge_count') or 0)} edges; "
            f"{int(report.get('conflict_count') or 0)} potential conflicts."
        ),
    ]
    issues = list(report.get("issues") or [])[:max_issues]
    if issues:
        lines.append("Issues:")
        for issue in issues:
            evidence_id = str(issue.get("evidence_id") or "")
            suffix = f" [{evidence_id}]" if evidence_id else ""
            lines.append(
                f"- {str(issue.get('severity') or '').upper()} "
                f"{issue.get('code')}: {issue.get('message')}{suffix}"
            )
    return "\n".join(lines)
