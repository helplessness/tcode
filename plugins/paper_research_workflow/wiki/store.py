from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .text_safety_bridge import (
    contains_pdf_control_code,
    find_valid_mutations,
)

from .markdown_sync import (
    CLAIMS_END,
    CLAIMS_START,
    parse_materialized_markdown,
    semantic_projection,
)
from .review import approval_hash, build_review_model, derive_selected_operations
from .vector_index import PaperWikiVectorIndex


TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+")
SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")
ALLOWED_PAGE_STATUS = {
    "draft",
    "pending_review",
    "canonical",
    "stale",
    "conflicting",
    "archived",
    "rebuilding",
}
ALLOWED_SUPPORT_STATUS = {
    "supported",
    "partially_supported",
    "conflicting",
    "manual_unverified",
    "retracted",
    "stale",
}
ALLOWED_PROPOSAL_OPERATIONS = {"upsert_page", "archive_page", "restore_page"}
ALLOWED_EVIDENCE_RELATIONS = {"supports", "contradicts", "context_only"}
INLINE_EVIDENCE_MARKER_RE = re.compile(r"\s*\[evidence:[^\]]+\]")
LEGACY_USER_NOTES_HEADING_RE = re.compile(r"(?m)^##[ \t]+用户笔记[ \t]*$")


class WikiError(RuntimeError):
    pass


class WikiNotFoundError(WikiError):
    pass


class WikiConflictError(WikiError):
    pass


class WikiValidationError(WikiError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _split_legacy_user_notes(content: str) -> tuple[str, str]:
    text = str(content or "")
    match = LEGACY_USER_NOTES_HEADING_RE.search(text)
    if match is None:
        return text.strip(), ""
    before = text[: match.start()].rstrip()
    remainder = text[match.end() :].lstrip("\r\n")
    next_heading = re.search(r"(?m)^#{1,2}[ \t]+", remainder)
    if next_heading is None:
        notes = remainder.strip()
        after = ""
    else:
        notes = remainder[: next_heading.start()].strip()
        after = remainder[next_heading.start() :].strip()
    machine_content = "\n\n".join(part for part in (before, after) if part).strip()
    if notes == "暂无用户笔记。":
        notes = ""
    return machine_content, notes


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _safe_int(value: Any) -> int:
    """把外部字段安全转换为整数，非法值按零处理。"""

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in re.split(r"[,;\n]", value) if part.strip()]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _compiler_claim_matches_source_docs(
    claim: dict[str, Any],
    source_doc_ids: set[str],
) -> bool:
    if not source_doc_ids or str(claim.get("origin") or "") not in {
        "compiler",
        "paper_library",
    }:
        return False
    scope = dict(claim.get("scope") or {})
    scoped_doc_ids = _strings(
        [scope.get("source_doc_id"), *_strings(scope.get("source_doc_ids"))]
    )
    source_fact_id = str(scope.get("source_fact_id") or "").strip()
    evidence_ids = _strings(claim.get("evidence_ids"))
    return any(
        doc_id in scoped_doc_ids
        or source_fact_id == doc_id
        or source_fact_id.startswith(f"{doc_id}:")
        or any(evidence_id.startswith(f"paper:{doc_id}:") for evidence_id in evidence_ids)
        for doc_id in source_doc_ids
    )


def _slug(value: Any, fallback: str = "page") -> str:
    raw = str(value or "").strip().lower().replace(" ", "-")
    slug = SLUG_RE.sub("-", raw).strip(".-_")
    return slug[:96] or fallback


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PaperWikiStore:
    """Transactional Wiki state for one TCode workspace."""

    SCHEMA_VERSION = 4

    def __init__(
        self,
        *,
        library_root: Path,
        workdir: str,
        vector_search_enabled: bool = False,
        vector_index_min_pages: int = 1,
        vector_search_weight: float = 0.35,
    ):
        self.library_root = Path(library_root).expanduser().resolve(strict=False)
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.root = self.library_root / "wiki"
        self.state_root = self.root / "state"
        self.pages_root = self.root / "pages"
        self.proposals_root = self.root / "proposals"
        self.revisions_root = self.root / "revisions"
        self.archive_root = self.root / "archive"
        self.exports_root = self.root / "exports"
        self.reports_root = self.root / "reports"
        self.index_root = self.root / "index"
        self.db_path = self.state_root / "wiki.sqlite3"
        self.vector_search_enabled = bool(vector_search_enabled)
        self.vector_index_min_pages = max(1, int(vector_index_min_pages))
        self.vector_search_weight = max(0.0, min(1.0, float(vector_search_weight)))
        self.vector_index = PaperWikiVectorIndex(
            index_root=self.index_root,
            workdir=self.workdir,
        )
        self._fts_available = False
        self._initialize()

    def _library_document_exists(self, doc_id: str) -> bool:
        """Resolve both legacy and versioned Paper Library record layouts."""

        normalized_doc_id = str(doc_id or "").strip()
        if not normalized_doc_id:
            return False
        legacy_record = self.library_root / "docs" / normalized_doc_id / "record.json"
        if legacy_record.is_file():
            return True

        registry_path = self.library_root / "registry.json"
        try:
            registry = _loads(registry_path.read_text(encoding="utf-8"), {})
        except OSError:
            return False
        documents = registry.get("documents") if isinstance(registry, dict) else []
        for item in documents if isinstance(documents, list) else []:
            if not isinstance(item, dict) or str(item.get("doc_id") or "") != normalized_doc_id:
                continue
            artifacts = item.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            raw_record_path = str(
                artifacts.get("record_path")
                or item.get("record_path")
                or ""
            ).strip()
            if not raw_record_path:
                return False
            raw_path = Path(raw_record_path).expanduser()
            candidates = [raw_path] if raw_path.is_absolute() else []
            if raw_record_path == "/workspace" or raw_record_path.startswith("/workspace/"):
                relative = raw_record_path[len("/workspace") :].lstrip("/")
                candidates.append(self.workdir / relative)
            elif not raw_path.is_absolute():
                candidates.extend((self.library_root / raw_path, self.workdir / raw_path))
            return any(path.resolve(strict=False).is_file() for path in candidates)
        return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        for path in (
            self.state_root,
            self.pages_root,
            self.proposals_root,
            self.revisions_root,
            self.archive_root,
            self.exports_root,
            self.reports_root,
            self.index_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    page_id TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL UNIQUE,
                    page_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'canonical',
                    revision INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    source_doc_ids_json TEXT NOT NULL DEFAULT '[]',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    origin TEXT NOT NULL DEFAULT 'user',
                    created_by TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    markdown_path TEXT NOT NULL DEFAULT '',
                    materialized_hash TEXT NOT NULL DEFAULT '',
                    materialized_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_pages_status_type
                    ON pages(status, page_type, updated_at DESC);
                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
                    statement TEXT NOT NULL,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    support_status TEXT NOT NULL DEFAULT 'manual_unverified',
                    confidence REAL NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT 'user',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_claims_page ON claims(page_id, position);
                CREATE TABLE IF NOT EXISTS page_user_notes (
                    page_id TEXT PRIMARY KEY REFERENCES pages(page_id) ON DELETE CASCADE,
                    content TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'supports',
                    support_strength REAL NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL DEFAULT '',
                    verified_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(claim_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_claim_evidence_id
                    ON claim_evidence(evidence_id, relation);
                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_revision INTEGER,
                    snapshot_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(page_id, revision)
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    trigger_doc_ids_json TEXT NOT NULL DEFAULT '[]',
                    base_revisions_json TEXT NOT NULL DEFAULT '{}',
                    operations_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    proposal_hash TEXT NOT NULL,
                    compiler_version TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT,
                    review_comment TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    applied_at TEXT,
                    review_selection_json TEXT NOT NULL DEFAULT '{}',
                    applied_operations_json TEXT NOT NULL DEFAULT '[]',
                    approval_hash TEXT NOT NULL DEFAULT '',
                    parent_proposal_id TEXT NOT NULL DEFAULT '',
                    superseded_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_proposals_status
                    ON proposals(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    page_id TEXT,
                    proposal_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wiki_events_page
                    ON events(page_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS dependencies (
                    page_id TEXT NOT NULL,
                    dependency_type TEXT NOT NULL,
                    dependency_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(page_id, dependency_type, dependency_id)
                );
                CREATE TABLE IF NOT EXISTS entity_types (
                    type_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    parent_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'approved',
                    schema_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_drafts (
                    page_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    content TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(page_id, actor)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_schema_columns(connection)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5("
                    "page_id UNINDEXED, title, aliases, summary, content)"
                )
                self._fts_available = True
            except sqlite3.DatabaseError:
                self._fts_available = False
            self._seed_entity_types(connection)
            self._backfill_claim_evidence(connection)
        finally:
            connection.close()
        self._migrate_v4_user_notes()

    def _ensure_schema_columns(self, connection: sqlite3.Connection) -> None:
        migrations = {
            "pages": {
                "materialized_hash": "TEXT NOT NULL DEFAULT ''",
                "materialized_at": "TEXT NOT NULL DEFAULT ''",
            },
            "proposals": {
                "review_selection_json": "TEXT NOT NULL DEFAULT '{}'",
                "applied_operations_json": "TEXT NOT NULL DEFAULT '[]'",
                "approval_hash": "TEXT NOT NULL DEFAULT ''",
                "parent_proposal_id": "TEXT NOT NULL DEFAULT ''",
                "superseded_by": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in migrations.items():
            existing = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column not in existing:
                    try:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    except sqlite3.OperationalError as exc:
                        # Store objects are created per request. Two first requests
                        # may race on the same additive migration; re-check before
                        # treating the duplicate-column result as a real failure.
                        refreshed = {
                            str(row["name"])
                            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                        }
                        if column not in refreshed:
                            raise exc

    def _backfill_claim_evidence(self, connection: sqlite3.Connection) -> None:
        """Materialize typed support links for claims created by Wiki v1."""

        if connection.execute(
            "SELECT 1 FROM meta WHERE key='claim_evidence_backfilled'"
        ).fetchone():
            return
        now = utc_now()
        rows = connection.execute(
            "SELECT claim_id,evidence_ids_json FROM claims"
        ).fetchall()
        for row in rows:
            for evidence_id in _strings(_loads(row["evidence_ids_json"], [])):
                connection.execute(
                    "INSERT OR IGNORE INTO claim_evidence("
                    "claim_id,evidence_id,relation,support_strength,verified_at) "
                    "VALUES(?,?,?,?,?)",
                    (row["claim_id"], evidence_id, "supports", 1.0, now),
                )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('claim_evidence_backfilled', ?)",
            (now,),
        )

    def _migrate_v4_user_notes(self) -> None:
        """Move legacy note sections out of Agent content exactly once."""

        changed_page_ids: list[str] = []
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM meta WHERE key='page_user_notes_v4_migrated'"
            ).fetchone():
                return
            rows = connection.execute("SELECT * FROM pages ORDER BY page_id").fetchall()
            for row in rows:
                page = self._row_page(row) or {}
                page_id = str(page.get("page_id") or "")
                original_content = str(page.get("content") or "")
                machine_content, legacy_notes = _split_legacy_user_notes(original_content)
                machine_content = INLINE_EVIDENCE_MARKER_RE.sub("", machine_content).strip()
                now = utc_now()
                if legacy_notes:
                    connection.execute(
                        "INSERT OR IGNORE INTO page_user_notes("
                        "page_id,content,revision,updated_by,updated_at) VALUES(?,?,?,?,?)",
                        (page_id, legacy_notes, 1, "schema-v4-migration", now),
                    )
                if machine_content == original_content.strip():
                    continue
                claims = self._claims_for(connection, page_id)
                revision = int(page.get("revision") or 0) + 1
                fingerprint = _digest(
                    {
                        "page_type": page.get("page_type"),
                        "title": page.get("title"),
                        "summary": page.get("summary"),
                        "content": machine_content,
                        "status": page.get("status"),
                        "aliases": page.get("aliases") or [],
                        "source_doc_ids": page.get("source_doc_ids") or [],
                        "evidence_ids": page.get("evidence_ids") or [],
                        "claims": claims,
                    }
                )
                connection.execute(
                    "UPDATE pages SET content=?,revision=?,content_hash=?,updated_by=?,"
                    "updated_at=?,materialized_hash='',materialized_at='' WHERE page_id=?",
                    (
                        machine_content,
                        revision,
                        fingerprint,
                        "schema-v4-migration",
                        now,
                        page_id,
                    ),
                )
                migrated_page = self._page_in_connection(connection, page_id) or {}
                connection.execute(
                    "INSERT INTO revisions(revision_id,page_id,revision,parent_revision,"
                    "snapshot_json,actor,reason,origin,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        f"revision-{uuid.uuid4().hex}",
                        page_id,
                        revision,
                        revision - 1 or None,
                        _json(migrated_page),
                        "schema-v4-migration",
                        "separate_user_notes_from_agent_content",
                        "migration",
                        now,
                    ),
                )
                self._event(
                    connection,
                    "page_updated",
                    "schema-v4-migration",
                    page_id=page_id,
                    payload={
                        "revision": revision,
                        "reason": "separate_user_notes_from_agent_content",
                        "origin": "migration",
                    },
                )
                self._sync_fts(connection, migrated_page)
                self._mark_vector_dirty(connection)
                changed_page_ids.append(page_id)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES("
                "'page_user_notes_v4_migrated', ?)",
                (utc_now(),),
            )
        for page_id in changed_page_ids:
            self._materialize_page(self.get_page(page_id))
        if changed_page_ids:
            self._materialize_indexes()

    def _seed_entity_types(self, connection: sqlite3.Connection) -> None:
        now = utc_now()
        types = {
            "concept": "概念",
            "topic": "主题",
            "paper": "论文",
            "gene": "基因",
            "protein": "蛋白",
            "mutation": "突变",
            "compound": "化合物",
            "substrate": "底物",
            "organism": "物种",
            "method": "方法",
            "condition": "条件",
            "metric": "指标",
            "dataset": "数据集",
            "comparison": "对比",
            "open_question": "开放问题",
        }
        for type_id, display_name in types.items():
            connection.execute(
                "INSERT OR IGNORE INTO entity_types("
                "type_id, display_name, created_at, updated_at) VALUES(?,?,?,?)",
                (type_id, display_name, now, now),
            )

    def _row_page(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["aliases"] = _loads(payload.pop("aliases_json", "[]"), [])
        payload["source_doc_ids"] = _loads(payload.pop("source_doc_ids_json", "[]"), [])
        payload["evidence_ids"] = _loads(payload.pop("evidence_ids_json", "[]"), [])
        return payload

    def _row_claim(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["scope"] = _loads(payload.pop("scope_json", "{}"), {})
        payload["evidence_ids"] = _loads(payload.pop("evidence_ids_json", "[]"), [])
        return payload

    def _evidence_links_for_claims(
        self,
        connection: sqlite3.Connection,
        claim_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not claim_ids:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for offset in range(0, len(claim_ids), 500):
            batch = claim_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT * FROM claim_evidence WHERE claim_id IN ({placeholders}) "
                "ORDER BY claim_id,evidence_id",
                batch,
            ).fetchall()
            for row in rows:
                payload = dict(row)
                result.setdefault(str(payload["claim_id"]), []).append(payload)
        return result

    def _claims_for(self, connection: sqlite3.Connection, page_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM claims WHERE page_id=? ORDER BY position, created_at",
            (page_id,),
        ).fetchall()
        claims = [self._row_claim(row) for row in rows]
        links_by_claim = self._evidence_links_for_claims(
            connection,
            [str(claim["claim_id"]) for claim in claims],
        )
        for claim in claims:
            links = links_by_claim.get(str(claim["claim_id"]), [])
            if not links:
                links = [
                    {
                        "claim_id": claim["claim_id"],
                        "evidence_id": evidence_id,
                        "relation": "supports",
                        "support_strength": 1.0,
                        "note": "",
                        "source_hash": "",
                        "verified_at": "",
                    }
                    for evidence_id in claim.get("evidence_ids") or []
                ]
            claim["evidence_links"] = links
        return claims

    def _page_in_connection(
        self,
        connection: sqlite3.Connection,
        page_id: str,
        *,
        include_claims: bool = True,
    ) -> dict[str, Any] | None:
        page = self._row_page(connection.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone())
        if page is not None and include_claims:
            page["claims"] = self._claims_for(connection, page_id)
        return page

    def _normalize_claims(self, claims: Any, *, page_id: str, now: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not isinstance(claims, list):
            return result
        if len(claims) > 500:
            raise WikiValidationError("Wiki page cannot contain more than 500 claims")
        seen_claim_ids: set[str] = set()
        for position, raw in enumerate(claims):
            if not isinstance(raw, dict):
                continue
            statement = str(raw.get("statement") or raw.get("value") or "").strip()
            if not statement:
                continue
            support_status = str(raw.get("support_status") or "manual_unverified").strip()
            if support_status not in ALLOWED_SUPPORT_STATUS:
                support_status = "manual_unverified"
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
            except Exception:
                confidence = 0.0
            claim_id = str(raw.get("claim_id") or f"claim-{uuid.uuid4().hex[:12]}")
            if not SAFE_ID_RE.fullmatch(claim_id):
                raise WikiValidationError(f"invalid Wiki claim_id: {claim_id}")
            if claim_id in seen_claim_ids:
                raise WikiValidationError(f"duplicate Wiki claim_id: {claim_id}")
            seen_claim_ids.add(claim_id)
            evidence_ids = _strings(raw.get("evidence_ids"))
            has_explicit_ids = "evidence_ids" in raw
            evidence_links: list[dict[str, Any]] = []
            seen_evidence_ids: set[str] = set()
            for link in raw.get("evidence_links") or []:
                if not isinstance(link, dict):
                    continue
                evidence_id = str(link.get("evidence_id") or "").strip()
                if not evidence_id or not SAFE_ID_RE.fullmatch(evidence_id):
                    continue
                if has_explicit_ids and evidence_id not in evidence_ids:
                    continue
                if evidence_id in seen_evidence_ids:
                    continue
                relation = str(link.get("relation") or "supports").strip()
                if relation not in ALLOWED_EVIDENCE_RELATIONS:
                    relation = "supports"
                try:
                    strength = max(0.0, min(1.0, float(link.get("support_strength", 1.0))))
                except Exception:
                    strength = 1.0
                evidence_links.append(
                    {
                        "claim_id": claim_id,
                        "evidence_id": evidence_id,
                        "relation": relation,
                        "support_strength": strength,
                        "note": str(link.get("note") or "").strip()[:2000],
                        "source_hash": str(link.get("source_hash") or "").strip(),
                        "verified_at": str(link.get("verified_at") or "").strip(),
                    }
                )
                seen_evidence_ids.add(evidence_id)
                if evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
            for evidence_id in evidence_ids:
                if evidence_id in seen_evidence_ids or not SAFE_ID_RE.fullmatch(evidence_id):
                    continue
                evidence_links.append(
                    {
                        "claim_id": claim_id,
                        "evidence_id": evidence_id,
                        "relation": "supports",
                        "support_strength": 1.0,
                        "note": "",
                        "source_hash": "",
                        "verified_at": "",
                    }
                )
                seen_evidence_ids.add(evidence_id)
            result.append(
                {
                    "claim_id": claim_id,
                    "page_id": page_id,
                    "statement": statement,
                    "scope": dict(raw.get("scope") or {}) if isinstance(raw.get("scope"), dict) else {},
                    "evidence_ids": [link["evidence_id"] for link in evidence_links],
                    "evidence_links": evidence_links,
                    "support_status": support_status,
                    "confidence": confidence,
                    "origin": str(raw.get("origin") or "user").strip() or "user",
                    "position": position,
                    "created_at": str(raw.get("created_at") or now),
                    "updated_at": now,
                }
            )
        return result

    def _default_content(self, summary: str, claims: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        if summary:
            lines.extend([summary.strip(), ""])
        if claims:
            lines.extend(["## 综合结论", ""])
            for claim in claims:
                lines.append(f"- {claim['statement']}")
        return "\n".join(lines).strip()

    def _snapshot_evidence_links(self, claims: list[dict[str, Any]], *, now: str) -> None:
        """Capture evidence hashes so Wiki lint can detect later source drift."""

        evidence_ids = [
            str(link.get("evidence_id") or "")
            for claim in claims
            for link in claim.get("evidence_links") or []
            if str(link.get("evidence_id") or "")
        ]
        if not evidence_ids:
            return
        try:
            from tcode_agent.evidence import EvidenceStore
        except ImportError:
            return
        records = EvidenceStore.get_many(evidence_ids, workdir=self.workdir.as_posix())
        for claim in claims:
            for link in claim.get("evidence_links") or []:
                record = records.get(str(link.get("evidence_id") or ""))
                if record is None or link.get("source_hash"):
                    continue
                link["source_hash"] = record.content_hash
                link["verified_at"] = now

    def _sync_fts(self, connection: sqlite3.Connection, page: dict[str, Any]) -> None:
        if not self._fts_available:
            return
        try:
            claims = self._claims_for(connection, str(page["page_id"]))
            content = "\n".join(
                [str(page.get("content") or ""), *[str(item.get("statement") or "") for item in claims]]
            )
            connection.execute("DELETE FROM page_fts WHERE page_id=?", (page["page_id"],))
            connection.execute(
                "INSERT INTO page_fts(page_id,title,aliases,summary,content) VALUES(?,?,?,?,?)",
                (
                    page["page_id"],
                    page["title"],
                    " ".join(page.get("aliases") or []),
                    page.get("summary") or "",
                    content,
                ),
            )
        except sqlite3.DatabaseError:
            self._fts_available = False

    def _mark_vector_dirty(self, connection: sqlite3.Connection) -> None:
        if not self.vector_search_enabled:
            return
        row = connection.execute(
            "SELECT value FROM meta WHERE key='vector_index_generation'"
        ).fetchone()
        try:
            generation = int(row["value"] if row is not None else 0) + 1
        except Exception:
            generation = 1
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('vector_index_dirty','1')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('vector_index_generation',?)",
            (str(generation),),
        )

    def _mark_vector_clean_if_generation(self, expected_generation: int) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='vector_index_generation'"
            ).fetchone()
            try:
                current_generation = int(row["value"] if row is not None else 0)
            except Exception:
                current_generation = -1
            if current_generation != int(expected_generation):
                return False
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('vector_index_dirty','0')"
            )
            return True

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        actor: str,
        *,
        page_id: str = "",
        proposal_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(event_id,event_type,actor,page_id,proposal_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"event-{uuid.uuid4().hex}",
                event_type,
                actor,
                page_id or None,
                proposal_id or None,
                _json(payload or {}),
                utc_now(),
            ),
        )

    def _write_page_in_connection(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        *,
        actor: str,
        reason: str,
        origin: str,
        expected_revision: int | None = None,
    ) -> str:
        requested_id = str(payload.get("page_id") or "").strip()
        if requested_id and not SAFE_ID_RE.fullmatch(requested_id):
            raise WikiValidationError(f"invalid Wiki page_id: {requested_id}")
        page_type = _slug(payload.get("page_type") or "concept", "concept")
        title = str(payload.get("title") or "").strip()
        if not title and requested_id:
            existing_by_id = self._page_in_connection(connection, requested_id)
            title = str((existing_by_id or {}).get("title") or "").strip()
            page_type = str((existing_by_id or {}).get("page_type") or page_type)
        if not title:
            raise WikiValidationError("Wiki page title is required")
        if len(title) > 500:
            raise WikiValidationError("Wiki page title cannot exceed 500 characters")
        slug = _slug(payload.get("slug") or title, requested_id or "page")
        requested_canonical_key = str(payload.get("canonical_key") or "").strip()
        canonical_key = requested_canonical_key or f"{page_type}:{slug}"
        row = None
        if requested_id:
            row = connection.execute("SELECT * FROM pages WHERE page_id=?", (requested_id,)).fetchone()
        if row is None:
            row = connection.execute("SELECT * FROM pages WHERE canonical_key=?", (canonical_key,)).fetchone()
        existing = self._row_page(row)
        entity_type = connection.execute("SELECT status FROM entity_types WHERE type_id=?", (page_type,)).fetchone()
        if (existing is None or existing["page_type"] != page_type) and (entity_type is None or entity_type["status"] != "approved"):
            raise WikiValidationError("新页面的实体类型必须先在实体类型管理中申请并批准")
        if existing is not None and not requested_canonical_key:
            canonical_key = str(existing["canonical_key"])
        page_id = str((existing or {}).get("page_id") or requested_id or f"wiki-{uuid.uuid4().hex[:16]}")
        canonical_owner = connection.execute(
            "SELECT page_id FROM pages WHERE canonical_key=?",
            (canonical_key,),
        ).fetchone()
        if canonical_owner is not None and str(canonical_owner["page_id"]) != page_id:
            raise WikiConflictError(f"Wiki canonical_key already exists: {canonical_key}")
        current_revision = int((existing or {}).get("revision") or 0)
        if expected_revision is not None and current_revision != int(expected_revision):
            raise WikiConflictError(
                f"page revision conflict: expected {expected_revision}, current {current_revision}"
            )
        now = utc_now()
        existing_claims = self._claims_for(connection, page_id) if existing else []
        claims = (
            self._normalize_claims(payload.get("claims"), page_id=page_id, now=now)
            if "claims" in payload
            else existing_claims
        )
        self._snapshot_evidence_links(claims, now=now)
        for claim in claims:
            owner = connection.execute(
                "SELECT page_id FROM claims WHERE claim_id=?",
                (claim["claim_id"],),
            ).fetchone()
            if owner is not None and str(owner["page_id"]) != page_id:
                claim["claim_id"] = (
                    "claim-"
                    + hashlib.sha256(
                        f"{page_id}:{claim['claim_id']}".encode("utf-8")
                    ).hexdigest()[:24]
                )
                for link in claim.get("evidence_links") or []:
                    link["claim_id"] = claim["claim_id"]
        if origin == "user" and "claims" in payload:
            previous_claims = {item["claim_id"]: item for item in existing_claims}
            for claim in claims:
                previous = previous_claims.get(claim["claim_id"])
                meaningful = {
                    "statement": claim["statement"],
                    "scope": claim["scope"],
                    "evidence_ids": claim["evidence_ids"],
                    "evidence_links": claim["evidence_links"],
                    "support_status": claim["support_status"],
                    "confidence": claim["confidence"],
                }
                previous_meaningful = (
                    {
                        "statement": previous["statement"],
                        "scope": previous["scope"],
                        "evidence_ids": previous["evidence_ids"],
                        "evidence_links": previous.get("evidence_links") or [],
                        "support_status": previous["support_status"],
                        "confidence": previous["confidence"],
                    }
                    if previous
                    else None
                )
                if meaningful != previous_meaningful:
                    claim["origin"] = "user_note"
                    claim["support_status"] = "manual_unverified"
        summary = str(payload.get("summary", (existing or {}).get("summary") or "")).strip()
        content = str(payload.get("content", (existing or {}).get("content") or "")).strip()
        if len(summary) > 20_000:
            raise WikiValidationError("Wiki page summary cannot exceed 20000 characters")
        if len(content) > 2_000_000:
            raise WikiValidationError("Wiki page content cannot exceed 2000000 characters")
        if not content:
            content = self._default_content(summary, claims)
        aliases = _strings(payload.get("aliases", (existing or {}).get("aliases") or []))
        source_doc_ids = _strings(
            payload.get("source_doc_ids", (existing or {}).get("source_doc_ids") or [])
        )
        evidence_ids = _strings(
            payload.get("evidence_ids", (existing or {}).get("evidence_ids") or [])
        )
        for claim in claims:
            for evidence_id in claim.get("evidence_ids", []):
                if evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
        status = str(payload.get("status", (existing or {}).get("status") or "canonical")).strip()
        if status not in ALLOWED_PAGE_STATUS:
            raise WikiValidationError(f"unsupported Wiki page status: {status}")
        revision = current_revision + 1
        markdown_path = str((existing or {}).get("markdown_path") or "").strip()
        if not markdown_path:
            markdown_file = self.pages_root / page_type / f"{slug}--{page_id[-8:]}.md"
            try:
                markdown_path = markdown_file.relative_to(self.workdir).as_posix()
            except ValueError:
                markdown_path = markdown_file.as_posix()
        created_at = str((existing or {}).get("created_at") or now)
        created_by = str((existing or {}).get("created_by") or actor)
        archived_at = now if status == "archived" else None
        fingerprint = _digest(
            {
                "page_type": page_type,
                "title": title,
                "summary": summary,
                "content": content,
                "status": status,
                "aliases": aliases,
                "source_doc_ids": source_doc_ids,
                "evidence_ids": evidence_ids,
                "claims": claims,
            }
        )
        connection.execute(
            """
            INSERT INTO pages(
                page_id,canonical_key,page_type,title,slug,summary,content,status,revision,
                content_hash,aliases_json,source_doc_ids_json,evidence_ids_json,origin,
                created_by,updated_by,markdown_path,created_at,updated_at,archived_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(page_id) DO UPDATE SET
                canonical_key=excluded.canonical_key,page_type=excluded.page_type,
                title=excluded.title,slug=excluded.slug,summary=excluded.summary,
                content=excluded.content,status=excluded.status,revision=excluded.revision,
                content_hash=excluded.content_hash,aliases_json=excluded.aliases_json,
                source_doc_ids_json=excluded.source_doc_ids_json,
                evidence_ids_json=excluded.evidence_ids_json,origin=excluded.origin,
                updated_by=excluded.updated_by,markdown_path=excluded.markdown_path,
                updated_at=excluded.updated_at,archived_at=excluded.archived_at
            """,
            (
                page_id,
                canonical_key,
                page_type,
                title,
                slug,
                summary,
                content,
                status,
                revision,
                fingerprint,
                _json(aliases),
                _json(source_doc_ids),
                _json(evidence_ids),
                origin,
                created_by,
                actor,
                markdown_path,
                created_at,
                now,
                archived_at,
            ),
        )
        connection.execute("DELETE FROM claims WHERE page_id=?", (page_id,))
        for claim in claims:
            connection.execute(
                "INSERT INTO claims(claim_id,page_id,statement,scope_json,evidence_ids_json,"
                "support_status,confidence,origin,position,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    claim["claim_id"],
                    page_id,
                    claim["statement"],
                    _json(claim["scope"]),
                    _json(claim["evidence_ids"]),
                    claim["support_status"],
                    claim["confidence"],
                    claim["origin"],
                    claim["position"],
                    claim["created_at"],
                    claim["updated_at"],
                ),
            )
            for link in claim.get("evidence_links") or []:
                connection.execute(
                    "INSERT INTO claim_evidence("
                    "claim_id,evidence_id,relation,support_strength,note,source_hash,verified_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        claim["claim_id"],
                        link["evidence_id"],
                        link["relation"],
                        link["support_strength"],
                        link["note"],
                        link["source_hash"],
                        link["verified_at"],
                    ),
                )
        connection.execute("DELETE FROM dependencies WHERE page_id=?", (page_id,))
        for dependency_type, values in (("paper", source_doc_ids), ("evidence", evidence_ids)):
            for dependency_id in values:
                connection.execute(
                    "INSERT INTO dependencies(page_id,dependency_type,dependency_id,created_at) "
                    "VALUES(?,?,?,?)",
                    (page_id, dependency_type, dependency_id, now),
                )
        page = self._page_in_connection(connection, page_id) or {}
        snapshot = dict(page)
        revision_id = f"revision-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO revisions(revision_id,page_id,revision,parent_revision,snapshot_json,"
            "actor,reason,origin,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                page_id,
                revision,
                current_revision or None,
                _json(snapshot),
                actor,
                reason,
                origin,
                now,
            ),
        )
        self._event(
            connection,
            "page_created" if current_revision == 0 else "page_updated",
            actor,
            page_id=page_id,
            payload={"revision": revision, "reason": reason, "origin": origin},
        )
        self._sync_fts(connection, page)
        self._mark_vector_dirty(connection)
        return page_id

    def save_page(
        self,
        payload: dict[str, Any],
        *,
        actor: str,
        reason: str = "user_edit",
        origin: str = "user",
        expected_revision: int | None = None,
        refresh_indexes: bool = True,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            page_id = self._write_page_in_connection(
                connection,
                dict(payload or {}),
                actor=actor,
                reason=reason,
                origin=origin,
                expected_revision=expected_revision,
            )
        page = self.get_page(page_id)
        self._materialize_page(page)
        if refresh_indexes:
            self._materialize_indexes()
        return page

    def get_page(self, page_id: str, *, include_history: bool = False) -> dict[str, Any]:
        connection = self._connect()
        try:
            page = self._page_in_connection(connection, str(page_id or "").strip())
            if page is None:
                raise WikiNotFoundError(f"Wiki page not found: {page_id}")
            if include_history:
                rows = connection.execute(
                    "SELECT revision_id,revision,parent_revision,actor,reason,origin,created_at "
                    "FROM revisions WHERE page_id=? ORDER BY revision DESC LIMIT 100",
                    (page_id,),
                ).fetchall()
                page["history"] = [dict(row) for row in rows]
            return page
        finally:
            connection.close()

    def get_page_user_notes(self, page_id: str) -> dict[str, Any]:
        normalized_page_id = str(page_id or "").strip()
        connection = self._connect()
        try:
            if connection.execute(
                "SELECT 1 FROM pages WHERE page_id=?",
                (normalized_page_id,),
            ).fetchone() is None:
                raise WikiNotFoundError(f"Wiki page not found: {page_id}")
            row = connection.execute(
                "SELECT content,revision,updated_by,updated_at "
                "FROM page_user_notes WHERE page_id=?",
                (normalized_page_id,),
            ).fetchone()
            return {
                "page_id": normalized_page_id,
                "user_notes": str(row["content"] or "") if row is not None else "",
                "user_notes_revision": int(row["revision"] or 0) if row is not None else 0,
                "user_notes_updated_by": str(row["updated_by"] or "") if row is not None else "",
                "user_notes_updated_at": str(row["updated_at"] or "") if row is not None else "",
            }
        finally:
            connection.close()

    def save_page_user_notes(
        self,
        page_id: str,
        content: str,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized_page_id = str(page_id or "").strip()
        notes = str(content or "")
        if len(notes) > 500_000:
            raise WikiValidationError("Wiki user notes cannot exceed 500000 characters")
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM pages WHERE page_id=?",
                (normalized_page_id,),
            ).fetchone() is None:
                raise WikiNotFoundError(f"Wiki page not found: {page_id}")
            row = connection.execute(
                "SELECT revision FROM page_user_notes WHERE page_id=?",
                (normalized_page_id,),
            ).fetchone()
            current_revision = int(row["revision"] or 0) if row is not None else 0
            if expected_revision is not None and current_revision != int(expected_revision):
                raise WikiConflictError(
                    "user notes revision conflict: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            revision = current_revision + 1
            now = utc_now()
            connection.execute(
                "INSERT INTO page_user_notes(page_id,content,revision,updated_by,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET "
                "content=excluded.content,revision=excluded.revision,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (normalized_page_id, notes, revision, str(actor or ""), now),
            )
            self._event(
                connection,
                "page_user_notes_updated",
                str(actor or ""),
                page_id=normalized_page_id,
                payload={"notes_revision": revision},
            )
        return self.get_page_user_notes(normalized_page_id)

    def list_pages(
        self,
        *,
        status: str = "",
        page_type: str = "",
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
        allowed_page_ids: Iterable[str] | None = None,
        allowed_page_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        values: list[Any] = []
        if status:
            conditions.append("status=?")
            values.append(status)
        elif not include_archived:
            conditions.append("status!='archived'")
        if page_type:
            conditions.append("page_type=?")
            values.append(page_type)
        scope_condition, scope_values = self._page_scope_condition(
            allowed_page_ids,
            allowed_page_types,
        )
        if scope_condition:
            conditions.append(scope_condition)
            values.extend(scope_values)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        safe_limit = max(1, min(200, int(limit)))
        safe_offset = max(0, int(offset))
        connection = self._connect()
        try:
            total = int(connection.execute(f"SELECT COUNT(*) FROM pages{where}", values).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM pages{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                [*values, safe_limit, safe_offset],
            ).fetchall()
            return {
                "total": total,
                "limit": safe_limit,
                "offset": safe_offset,
                "pages": [
                    {key: value for key, value in (self._row_page(row) or {}).items() if key != "content"}
                    for row in rows
                ],
            }
        finally:
            connection.close()

    @staticmethod
    def _page_scope_condition(
        allowed_page_ids: Iterable[str] | None,
        allowed_page_types: Iterable[str] | None,
        *,
        alias: str = "",
    ) -> tuple[str, list[str]]:
        if allowed_page_ids is None and allowed_page_types is None:
            return "", []
        page_ids = sorted({str(item).strip() for item in (allowed_page_ids or []) if str(item).strip()})
        page_types = sorted(
            {str(item).strip() for item in (allowed_page_types or []) if str(item).strip()}
        )
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        values: list[str] = []
        if page_ids:
            clauses.append(f"{prefix}page_id IN ({','.join('?' for _ in page_ids)})")
            values.extend(page_ids)
        if page_types:
            clauses.append(f"{prefix}page_type IN ({','.join('?' for _ in page_types)})")
            values.extend(page_types)
        return (f"({' OR '.join(clauses)})" if clauses else "0"), values

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        status: str = "",
        include_archived: bool = False,
        allowed_page_ids: Iterable[str] | None = None,
        allowed_page_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        normalized_status = str(status or "").strip()
        if not text:
            listed = self.list_pages(
                status=normalized_status,
                include_archived=include_archived,
                limit=limit,
                allowed_page_ids=allowed_page_ids,
                allowed_page_types=allowed_page_types,
            )
            return {
                "query": text,
                "hits": listed["pages"],
                "total": listed["total"],
                "search_mode": "recent",
                "vector": self.vector_status(),
            }
        safe_limit = max(1, min(50, int(limit)))
        candidate_limit = max(safe_limit, min(200, safe_limit * 4))
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = []
            tokens = [match.group(0) for match in TOKEN_RE.finditer(text)][:12]
            if self._fts_available and tokens:
                fts_query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
                if normalized_status:
                    archived_clause = " AND p.status=?"
                    status_values = [normalized_status]
                else:
                    archived_clause = "" if include_archived else " AND p.status!='archived'"
                    status_values = []
                scope_condition, scope_values = self._page_scope_condition(
                    allowed_page_ids,
                    allowed_page_types,
                    alias="p",
                )
                scope_clause = f" AND {scope_condition}" if scope_condition else ""
                try:
                    rows = connection.execute(
                        "SELECT p.*, bm25(page_fts) AS rank FROM page_fts "
                        "JOIN pages p ON p.page_id=page_fts.page_id "
                        f"WHERE page_fts MATCH ?{archived_clause}{scope_clause} ORDER BY rank LIMIT ?",
                        [fts_query, *status_values, *scope_values, candidate_limit],
                    ).fetchall()
                except sqlite3.DatabaseError:
                    rows = []
            if not rows:
                like = f"%{text}%"
                if normalized_status:
                    archived_clause = " AND status=?"
                    status_values = [normalized_status]
                else:
                    archived_clause = "" if include_archived else " AND status!='archived'"
                    status_values = []
                scope_condition, scope_values = self._page_scope_condition(
                    allowed_page_ids,
                    allowed_page_types,
                )
                scope_clause = f" AND {scope_condition}" if scope_condition else ""
                rows = connection.execute(
                    "SELECT *, 0 AS rank FROM pages WHERE "
                    "(title LIKE ? OR summary LIKE ? OR content LIKE ? OR aliases_json LIKE ?)"
                    f"{archived_clause}{scope_clause} ORDER BY updated_at DESC LIMIT ?",
                    [like, like, like, like, *status_values, *scope_values, candidate_limit],
                ).fetchall()
            pages_by_id = {
                str(row["page_id"]): self._row_page(row) or {} for row in rows
            }
            lexical_scores = {
                str(row["page_id"]): 1.0 - (index / max(1, len(rows)))
                for index, row in enumerate(rows)
            }
            vector_status = self.vector_status()
            vector_scores: dict[str, float] = {}
            vector_chunks: dict[str, list[dict[str, Any]]] = {}
            vector_result: dict[str, Any] = {"status": "disabled", "hits": []}
            if (
                self.vector_search_enabled
                and vector_status["available"]
                and not vector_status["dirty"]
                and normalized_status != "archived"
            ):
                try:
                    vector_result = self.vector_index.search(text, limit=candidate_limit)
                    if vector_result.get("status") == "ready":
                        vector_scores = {
                            str(item["page_id"]): max(
                                0.0,
                                min(1.0, (float(item.get("score") or 0.0) + 1.0) / 2.0),
                            )
                            for item in vector_result.get("hits") or []
                        }
                        vector_chunks = {
                            str(item["page_id"]): [
                                dict(chunk)
                                for chunk in list(item.get("chunks") or [])[:3]
                                if isinstance(chunk, dict)
                            ]
                            for item in vector_result.get("hits") or []
                        }
                    elif vector_result.get("status") == "version_mismatch":
                        # 服务失联导致查询暂时降级哈希，不是已有模型索引损坏。
                        # 不因每次查询重复排队重建同一个健康索引。
                        if not str(vector_result.get("query_embedding_version") or "").startswith("hash-"):
                            self._mark_vector_dirty(connection)
                            vector_status["dirty"] = True
                except Exception as exc:
                    vector_result = {
                        "status": "error",
                        "hits": [],
                        "error_type": type(exc).__name__,
                    }
            missing_ids = [page_id for page_id in vector_scores if page_id not in pages_by_id]
            if missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                if normalized_status:
                    archived_clause = " AND status=?"
                    status_values = [normalized_status]
                else:
                    archived_clause = "" if include_archived else " AND status!='archived'"
                    status_values = []
                scope_condition, scope_values = self._page_scope_condition(
                    allowed_page_ids,
                    allowed_page_types,
                )
                scope_clause = f" AND {scope_condition}" if scope_condition else ""
                vector_rows = connection.execute(
                    f"SELECT * FROM pages WHERE page_id IN ({placeholders}){archived_clause}{scope_clause}",
                    [*missing_ids, *status_values, *scope_values],
                ).fetchall()
                for row in vector_rows:
                    pages_by_id[str(row["page_id"])] = self._row_page(row) or {}
            vector_scores = {
                page_id: score for page_id, score in vector_scores.items() if page_id in pages_by_id
            }

            ranked: list[tuple[str, float]] = []
            all_ids = set(lexical_scores) | set(vector_scores)
            for page_id in all_ids:
                lexical = lexical_scores.get(page_id, 0.0)
                vector = vector_scores.get(page_id, 0.0)
                if page_id in lexical_scores and page_id in vector_scores:
                    score = (1.0 - self.vector_search_weight) * lexical + self.vector_search_weight * vector
                elif page_id in vector_scores:
                    score = vector * self.vector_search_weight
                else:
                    score = lexical * (1.0 - self.vector_search_weight)
                ranked.append((page_id, score))
            ranked.sort(key=lambda item: item[1], reverse=True)
            hits: list[dict[str, Any]] = []
            lowered = text.casefold()
            for page_id, hybrid_score in ranked[:safe_limit]:
                page = dict(pages_by_id.get(page_id) or {})
                if not page:
                    continue
                page.pop("content", None)
                claims = self._claims_for(connection, page_id)
                matched = [
                    claim
                    for claim in claims
                    if lowered in str(claim.get("statement") or "").casefold()
                    or any(token.casefold() in str(claim.get("statement") or "").casefold() for token in tokens)
                ][:8]
                page["matched_claims"] = matched
                page["lexical_score"] = lexical_scores.get(page_id, 0.0)
                page["vector_score"] = vector_scores.get(page_id, 0.0)
                page["score"] = hybrid_score
                page["matched_chunks"] = vector_chunks.get(page_id, [])
                hits.append(page)
            used_vector = bool(vector_scores)
            return {
                "query": text,
                "hits": hits,
                "total": len(hits),
                "search_mode": "hybrid" if used_vector else "fts",
                "vector": {
                    **vector_status,
                    "query_status": vector_result.get("status"),
                },
            }
        finally:
            connection.close()

    def archive_page(self, page_id: str, *, actor: str, expected_revision: int | None = None) -> dict[str, Any]:
        page = self.get_page(page_id)
        page["status"] = "archived"
        updated = self.save_page(
            page,
            actor=actor,
            reason="soft_delete",
            origin="user",
            expected_revision=expected_revision,
        )
        _atomic_write(
            self.archive_root / f"{page_id}.json",
            _pretty_json(
                {
                    "page_id": page_id,
                    "archived_at": updated.get("archived_at"),
                    "archived_by": actor,
                    "revision": updated.get("revision"),
                }
            ),
        )
        return updated

    def restore_page(self, page_id: str, *, actor: str, expected_revision: int | None = None) -> dict[str, Any]:
        page = self.get_page(page_id)
        page["status"] = "canonical"
        updated = self.save_page(
            page,
            actor=actor,
            reason="restore",
            origin="user",
            expected_revision=expected_revision,
        )
        tombstone = self.archive_root / f"{page_id}.json"
        if tombstone.exists():
            tombstone.unlink()
        return updated

    @staticmethod
    def _proposal_operation_matches_page(
        operation: Any,
        *,
        page_id: str,
        canonical_key: str,
    ) -> bool:
        if not isinstance(operation, dict):
            return False
        candidates = [operation]
        for key in ("page", "before"):
            value = operation.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        return any(
            str(candidate.get("page_id") or "").strip() == page_id
            or (
                bool(canonical_key)
                and str(candidate.get("canonical_key") or "").strip() == canonical_key
            )
            for candidate in candidates
        )

    def _redact_page_from_proposals(
        self,
        connection: sqlite3.Connection,
        *,
        page_id: str,
        canonical_key: str,
    ) -> list[str]:
        """Remove deleted page snapshots while retaining a minimal review audit."""

        affected: list[str] = []
        rows = connection.execute("SELECT * FROM proposals ORDER BY proposal_id").fetchall()
        for row in rows:
            proposal = self._row_proposal(row) or {}
            operations = list(proposal.get("operations") or [])
            applied_operations = list(proposal.get("applied_operations") or [])
            matching_operation_indexes = {
                index
                for index, operation in enumerate(operations)
                if self._proposal_operation_matches_page(
                    operation,
                    page_id=page_id,
                    canonical_key=canonical_key,
                )
            }
            matching_applied_indexes = {
                index
                for index, operation in enumerate(applied_operations)
                if self._proposal_operation_matches_page(
                    operation,
                    page_id=page_id,
                    canonical_key=canonical_key,
                )
            }
            if not matching_operation_indexes and not matching_applied_indexes:
                continue

            redacted = {
                "type": "purged_page",
                "purged": True,
                "message": "This Wiki page was permanently deleted.",
            }
            operations = [
                dict(redacted) if index in matching_operation_indexes else operation
                for index, operation in enumerate(operations)
            ]
            applied_operations = [
                dict(redacted) if index in matching_applied_indexes else operation
                for index, operation in enumerate(applied_operations)
            ]
            base_revisions = dict(proposal.get("base_revisions") or {})
            base_revisions.pop(page_id, None)
            validation = dict(proposal.get("validation") or {})
            warnings = [
                str(item)
                for item in list(validation.get("warnings") or [])
                if page_id not in str(item) and canonical_key not in str(item)
            ]
            warnings.append("A referenced Wiki page was permanently deleted and redacted.")
            validation["warnings"] = warnings
            status = str(proposal.get("status") or "")
            if status in {"pending_review", "draft"}:
                status = "superseded"
                validation["valid"] = False
                validation["errors"] = [
                    "This proposal can no longer be applied because a referenced page was permanently deleted."
                ]
            connection.execute(
                "UPDATE proposals SET status=?,base_revisions_json=?,operations_json=?,"
                "validation_json=?,applied_operations_json=?,updated_at=? WHERE proposal_id=?",
                (
                    status,
                    _json(base_revisions),
                    _json(operations),
                    _json(validation),
                    _json(applied_operations),
                    utc_now(),
                    proposal["proposal_id"],
                ),
            )
            affected.append(str(proposal["proposal_id"]))
        return affected

    def purge_page(
        self,
        page_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Permanently delete one archived Wiki page and all page-owned history.

        Paper Library documents and EvidenceStore records are intentionally not
        deleted because they are independent sources that may support other pages.
        """

        normalized_page_id = str(page_id or "").strip()
        if not normalized_page_id or not SAFE_ID_RE.fullmatch(normalized_page_id):
            raise WikiValidationError("invalid Wiki page_id")

        proposal_ids: list[str] = []
        markdown_path = Path()
        deleted: dict[str, int] = {}
        with self._transaction() as connection:
            page = self._page_in_connection(connection, normalized_page_id)
            if page is None:
                raise WikiNotFoundError(f"Wiki page not found: {normalized_page_id}")
            if str(page.get("status") or "") != "archived":
                raise WikiValidationError(
                    "Only archived Wiki pages can be permanently deleted"
                )
            current_revision = int(page.get("revision") or 0)
            if expected_revision is not None and current_revision != int(expected_revision):
                raise WikiConflictError(
                    "page revision conflict: "
                    f"expected {expected_revision}, current {current_revision}"
                )

            markdown_path = Path(str(page.get("markdown_path") or ""))
            if markdown_path and not markdown_path.is_absolute():
                markdown_path = self.workdir / markdown_path
            deleted = {
                "claims": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM claims WHERE page_id=?",
                        (normalized_page_id,),
                    ).fetchone()[0]
                ),
                "evidence_links": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM claim_evidence WHERE claim_id IN "
                        "(SELECT claim_id FROM claims WHERE page_id=?)",
                        (normalized_page_id,),
                    ).fetchone()[0]
                ),
                "user_notes": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM page_user_notes WHERE page_id=?",
                        (normalized_page_id,),
                    ).fetchone()[0]
                ),
                "revisions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM revisions WHERE page_id=?",
                        (normalized_page_id,),
                    ).fetchone()[0]
                ),
                "dependencies": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM dependencies WHERE page_id=?",
                        (normalized_page_id,),
                    ).fetchone()[0]
                ),
            }
            proposal_ids = self._redact_page_from_proposals(
                connection,
                page_id=normalized_page_id,
                canonical_key=str(page.get("canonical_key") or ""),
            )
            deleted["redacted_proposals"] = len(proposal_ids)

            connection.execute("DELETE FROM revisions WHERE page_id=?", (normalized_page_id,))
            connection.execute("DELETE FROM note_drafts WHERE page_id=?", (normalized_page_id,))
            connection.execute("DELETE FROM dependencies WHERE page_id=?", (normalized_page_id,))
            connection.execute("DELETE FROM events WHERE page_id=?", (normalized_page_id,))
            try:
                connection.execute("DELETE FROM page_fts WHERE page_id=?", (normalized_page_id,))
            except sqlite3.DatabaseError:
                self._fts_available = False
            connection.execute("DELETE FROM pages WHERE page_id=?", (normalized_page_id,))
            self._event(
                connection,
                "page_purged",
                actor,
                payload={
                    "page_id": normalized_page_id,
                    "deleted": deleted,
                    "source_records_preserved": True,
                },
            )
            self._mark_vector_dirty(connection)

        cleanup_warnings: list[str] = []
        removed_files = 0
        pages_root = self.pages_root.resolve(strict=False)
        if str(markdown_path):
            resolved_markdown = markdown_path.resolve(strict=False)
            try:
                resolved_markdown.relative_to(pages_root)
            except ValueError:
                cleanup_warnings.append("Skipped an unsafe materialized Markdown path")
            else:
                try:
                    if resolved_markdown.is_file() or resolved_markdown.is_symlink():
                        resolved_markdown.unlink()
                        removed_files += 1
                    parent = resolved_markdown.parent
                    if parent != pages_root and parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError as exc:
                    cleanup_warnings.append(
                        f"Failed to remove materialized Markdown: {type(exc).__name__}"
                    )

        revision_dir = self.revisions_root / normalized_page_id
        try:
            if revision_dir.exists():
                shutil.rmtree(revision_dir)
                removed_files += 1
        except OSError as exc:
            cleanup_warnings.append(
                f"Failed to remove revision snapshots: {type(exc).__name__}"
            )

        archive_marker = self.archive_root / f"{normalized_page_id}.json"
        try:
            if archive_marker.is_file() or archive_marker.is_symlink():
                archive_marker.unlink()
                removed_files += 1
        except OSError as exc:
            cleanup_warnings.append(
                f"Failed to remove archive marker: {type(exc).__name__}"
            )

        for proposal_id in proposal_ids:
            try:
                self._materialize_proposal(self.get_proposal(proposal_id))
            except Exception as exc:
                cleanup_warnings.append(
                    f"Failed to redact proposal materialization: {type(exc).__name__}"
                )
        try:
            self._materialize_indexes()
        except Exception as exc:
            cleanup_warnings.append(
                f"Failed to refresh Wiki materialized indexes: {type(exc).__name__}"
            )
        return {
            "status": "purged",
            "page_id": normalized_page_id,
            "deleted": deleted,
            "removed_file_groups": removed_files,
            "cleanup_warnings": cleanup_warnings,
            "source_records_preserved": True,
        }

    def rollback_page(self, page_id: str, revision: int, *, actor: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT snapshot_json FROM revisions WHERE page_id=? AND revision=?",
                (page_id, int(revision)),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise WikiNotFoundError(f"Wiki revision not found: {page_id}@{revision}")
        snapshot = _loads(row["snapshot_json"], {})
        current = self.get_page(page_id)
        snapshot["page_id"] = page_id
        return self.save_page(
            snapshot,
            actor=actor,
            reason=f"rollback_to_revision_{revision}",
            origin="rollback",
            expected_revision=int(current["revision"]),
        )

    def _validate_operations(self, operations: Any, *, require_evidence: bool) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(operations, list) or not operations:
            errors.append("proposal operations must be a non-empty list")
            operations = []
        elif len(operations) > 100:
            errors.append("proposal cannot contain more than 100 operations")
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                errors.append(f"operation[{index}] must be an object")
                continue
            op_type = str(operation.get("type") or "")
            if op_type not in ALLOWED_PROPOSAL_OPERATIONS:
                errors.append(f"operation[{index}] has unsupported type: {op_type}")
                continue
            if op_type == "upsert_page":
                page = operation.get("page")
                if not isinstance(page, dict) or not str(page.get("title") or "").strip():
                    errors.append(f"operation[{index}] requires page.title")
                    continue
                for claim_index, claim in enumerate(page.get("claims") or []):
                    if not isinstance(claim, dict) or not str(claim.get("statement") or "").strip():
                        errors.append(f"operation[{index}].claims[{claim_index}] requires statement")
                        continue
                    evidence_ids = _strings(claim.get("evidence_ids"))
                    links = [
                        item for item in claim.get("evidence_links") or [] if isinstance(item, dict)
                    ]
                    for link_index, link in enumerate(links):
                        relation = str(link.get("relation") or "supports")
                        if relation not in ALLOWED_EVIDENCE_RELATIONS:
                            errors.append(
                                f"operation[{index}].claims[{claim_index}]."
                                f"evidence_links[{link_index}] has unsupported relation: {relation}"
                            )
                        evidence_id = str(link.get("evidence_id") or "").strip()
                        if evidence_id and evidence_id not in evidence_ids:
                            evidence_ids.append(evidence_id)
                    if require_evidence and not evidence_ids:
                        warnings.append(
                            f"operation[{index}].claims[{claim_index}] has no evidence and will be unverified"
                        )
            elif not str(operation.get("page_id") or "").strip():
                errors.append(f"operation[{index}] requires page_id")
            elif not SAFE_ID_RE.fullmatch(str(operation.get("page_id") or "").strip()):
                errors.append(f"operation[{index}] has invalid page_id")
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def stage_proposal(
        self,
        *,
        operations: list[dict[str, Any]],
        trigger_kind: str,
        trigger_doc_ids: list[str] | None,
        created_by: str,
        compiler_version: str = "paper-wiki-1",
        require_evidence: bool = True,
        parent_proposal_id: str = "",
    ) -> dict[str, Any]:
        prepared_operations: list[dict[str, Any]] = []
        connection = self._connect()
        try:
            base_revisions: dict[str, int] = {}
            for raw_operation in operations:
                operation = dict(raw_operation or {})
                if str(operation.get("type") or "") == "upsert_page":
                    page_payload = dict(operation.get("page") or {})
                    replace_claim_docs = set(
                        _strings(page_payload.pop("_replace_compiler_claims_for_source_doc_ids", []))
                    )
                    refresh_generated_content = bool(
                        page_payload.pop("_refresh_generated_content", False)
                    )
                    page_id = str(page_payload.get("page_id") or "").strip()
                    row = None
                    if page_id:
                        row = connection.execute(
                            "SELECT * FROM pages WHERE page_id=?", (page_id,)
                        ).fetchone()
                    canonical_key = str(page_payload.get("canonical_key") or "").strip()
                    if row is None and canonical_key:
                        row = connection.execute(
                            "SELECT * FROM pages WHERE canonical_key=?", (canonical_key,)
                        ).fetchone()
                    existing = self._row_page(row)
                    if existing is not None:
                        page_id = str(existing["page_id"])
                        page_payload["page_id"] = page_id
                        before = self._page_in_connection(connection, page_id) or existing
                        operation["before"] = before
                        if (
                            str(trigger_kind or "") == "paper_ingest"
                            and str(page_payload.get("page_type") or existing.get("page_type")) != "paper"
                        ):
                            for field in ("aliases", "source_doc_ids", "evidence_ids"):
                                page_payload[field] = _strings(
                                    [*(before.get(field) or []), *(page_payload.get(field) or [])]
                                )
                            merged_claims = {
                                str(item.get("claim_id") or ""): dict(item)
                                for item in before.get("claims") or []
                                if str(item.get("claim_id") or "")
                                and not _compiler_claim_matches_source_docs(
                                    dict(item), replace_claim_docs
                                )
                            }
                            for item in page_payload.get("claims") or []:
                                if not isinstance(item, dict):
                                    continue
                                key = str(item.get("claim_id") or "")
                                if not key:
                                    key = "claim-" + _digest(
                                        {
                                            "statement": item.get("statement"),
                                            "evidence_ids": item.get("evidence_ids"),
                                        }
                                    )[:24]
                                    item = {**item, "claim_id": key}
                                merged_claims[key] = dict(item)
                            page_payload["claims"] = list(merged_claims.values())
                    canonical_identity = str(
                        page_payload.get("canonical_key")
                        or page_payload.get("page_id")
                        or page_payload.get("title")
                        or "wiki-page"
                    )
                    prepared_claims: list[dict[str, Any]] = []
                    for claim in page_payload.get("claims") or []:
                        if not isinstance(claim, dict):
                            continue
                        prepared_claim = dict(claim)
                        if not str(prepared_claim.get("claim_id") or "").strip():
                            prepared_claim["claim_id"] = "claim-" + _digest(
                                {
                                    "page": canonical_identity,
                                    "statement": prepared_claim.get("statement"),
                                    "evidence_ids": prepared_claim.get("evidence_ids"),
                                }
                            )[:24]
                        prepared_claims.append(prepared_claim)
                    if "claims" in page_payload:
                        page_payload["claims"] = prepared_claims
                    if refresh_generated_content and (
                        existing is None or str(existing.get("origin") or "") == "compiler"
                    ):
                        page_payload["content"] = self._default_content(
                            str(page_payload.get("summary") or ""),
                            prepared_claims,
                        )
                    operation["page"] = page_payload
                else:
                    page_id = str(operation.get("page_id") or "").strip()
                if not page_id:
                    prepared_operations.append(operation)
                    continue
                revision_row = connection.execute(
                    "SELECT revision FROM pages WHERE page_id=?", (page_id,)
                ).fetchone()
                base_revisions[page_id] = int(revision_row["revision"] if revision_row else 0)
                prepared_operations.append(operation)
        finally:
            connection.close()
        operations = prepared_operations
        validation = self._validate_operations(operations, require_evidence=require_evidence)
        proposal_id = f"wiki-proposal-{uuid.uuid4().hex[:16]}"
        trigger_docs = _strings(trigger_doc_ids)
        proposal_hash = _digest(
            {
                "operations": operations,
                "base_revisions": base_revisions,
                "trigger_doc_ids": trigger_docs,
            }
        )
        status = "pending_review" if validation["valid"] else "draft"
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO proposals(proposal_id,status,trigger_kind,trigger_doc_ids_json,"
                "base_revisions_json,operations_json,validation_json,proposal_hash,compiler_version,"
                "created_by,created_at,updated_at,parent_proposal_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    status,
                    str(trigger_kind or "manual"),
                    _json(trigger_docs),
                    _json(base_revisions),
                    _json(operations),
                    _json(validation),
                    proposal_hash,
                    compiler_version,
                    created_by,
                    now,
                    now,
                    str(parent_proposal_id or ""),
                ),
            )
            self._event(
                connection,
                "proposal_staged",
                created_by,
                proposal_id=proposal_id,
                payload={"status": status, "trigger_kind": trigger_kind},
            )
        proposal = self.get_proposal(proposal_id)
        self._materialize_proposal(proposal)
        return proposal

    def _row_proposal(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["trigger_doc_ids"] = _loads(payload.pop("trigger_doc_ids_json", "[]"), [])
        payload["base_revisions"] = _loads(payload.pop("base_revisions_json", "{}"), {})
        payload["operations"] = _loads(payload.pop("operations_json", "[]"), [])
        payload["validation"] = _loads(payload.pop("validation_json", "{}"), {})
        payload["review_selection"] = _loads(payload.pop("review_selection_json", "{}"), {})
        payload["applied_operations"] = _loads(payload.pop("applied_operations_json", "[]"), [])
        return payload

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            proposal = self._row_proposal(
                connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            )
        finally:
            connection.close()
        if proposal is None:
            raise WikiNotFoundError(f"Wiki proposal not found: {proposal_id}")
        proposal["review_model"] = build_review_model(list(proposal.get("operations") or []))
        return proposal

    def list_proposals(self, *, status: str = "", limit: int = 50) -> dict[str, Any]:
        safe_limit = max(1, min(200, int(limit)))
        connection = self._connect()
        try:
            if status:
                rows = connection.execute(
                    "SELECT * FROM proposals WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM proposals ORDER BY updated_at DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
            proposals: list[dict[str, Any]] = []
            for row in rows:
                proposal = self._row_proposal(row) or {}
                proposal["operation_count"] = len(proposal.get("operations") or [])
                proposal.pop("operations", None)
                proposals.append(proposal)
            return {"proposals": proposals, "count": len(rows)}
        finally:
            connection.close()

    def approve_and_apply_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        expected_hash: str,
        comment: str = "",
        selection: dict[str, Any] | None = None,
        require_new_pages_only: bool = False,
    ) -> dict[str, Any]:
        touched: list[str] = []
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            proposal = self._row_proposal(row)
            if proposal is None:
                raise WikiNotFoundError(f"Wiki proposal not found: {proposal_id}")
            if proposal["status"] != "pending_review":
                raise WikiConflictError(f"proposal is not pending: {proposal['status']}")
            if str(expected_hash or "") != str(proposal["proposal_hash"]):
                raise WikiConflictError("proposal content changed after it was opened for review")
            if require_new_pages_only:
                if str(proposal.get("trigger_kind") or "") != "paper_ingest":
                    raise WikiValidationError("automatic approval is limited to paper ingest")
                operations = list(proposal.get("operations") or [])
                if not operations:
                    raise WikiValidationError("automatic approval requires at least one new Wiki page")
                for operation in operations:
                    if str(operation.get("type") or "") != "upsert_page":
                        raise WikiValidationError("automatic approval only supports new Wiki pages")
                    page_payload = dict(operation.get("page") or {})
                    if str(page_payload.get("page_id") or "").strip() or operation.get("before"):
                        raise WikiConflictError("automatic approval cannot modify an existing Wiki page")
                    page_type = _slug(page_payload.get("page_type") or "concept", "concept")
                    title = str(page_payload.get("title") or "").strip()
                    slug = _slug(page_payload.get("slug") or title, "page")
                    canonical_key = str(page_payload.get("canonical_key") or "").strip()
                    canonical_key = canonical_key or f"{page_type}:{slug}"
                    existing = connection.execute(
                        "SELECT page_id FROM pages WHERE canonical_key=?",
                        (canonical_key,),
                    ).fetchone()
                    if existing is not None:
                        raise WikiConflictError(
                            f"automatic approval cannot replace existing Wiki page: {canonical_key}"
                        )
            try:
                applied_operations, normalized_selection, _ = derive_selected_operations(
                    list(proposal.get("operations") or []),
                    selection,
                )
            except ValueError as exc:
                raise WikiValidationError(str(exc)) from exc
            if not applied_operations:
                raise WikiValidationError("at least one Wiki change must be selected")
            approved_hash = approval_hash(
                proposal_hash=str(proposal["proposal_hash"]),
                selection=normalized_selection,
                applied_operations=applied_operations,
            )
            selected_page_ids: set[str] = set()
            for operation in applied_operations:
                if str(operation.get("type") or "") == "upsert_page":
                    page_id = str((operation.get("page") or {}).get("page_id") or "")
                else:
                    page_id = str(operation.get("page_id") or "")
                if page_id:
                    selected_page_ids.add(page_id)
            for page_id, expected in dict(proposal["base_revisions"]).items():
                if page_id not in selected_page_ids:
                    continue
                row = connection.execute("SELECT revision FROM pages WHERE page_id=?", (page_id,)).fetchone()
                current = int(row["revision"] if row else 0)
                if current != int(expected):
                    raise WikiConflictError(
                        f"proposal is stale for {page_id}: expected revision {expected}, current {current}"
                    )
            connection.execute(
                "UPDATE proposals SET status='approved',reviewed_by=?,review_comment=?,reviewed_at=?,"
                "updated_at=?,review_selection_json=?,applied_operations_json=?,approval_hash=? "
                "WHERE proposal_id=?",
                (
                    actor,
                    comment,
                    utc_now(),
                    utc_now(),
                    _json(normalized_selection),
                    _json(applied_operations),
                    approved_hash,
                    proposal_id,
                ),
            )
            for operation in applied_operations:
                op_type = str(operation.get("type") or "")
                if op_type == "upsert_page":
                    page_payload = dict(operation.get("page") or {})
                    page_id = self._write_page_in_connection(
                        connection,
                        page_payload,
                        actor=actor,
                        reason=f"apply_proposal:{proposal_id}",
                        origin="compiler",
                        expected_revision=(
                            int(proposal["base_revisions"].get(page_payload.get("page_id"), 0))
                            if page_payload.get("page_id")
                            else None
                        ),
                    )
                    touched.append(page_id)
                elif op_type in {"archive_page", "restore_page"}:
                    page_id = str(operation.get("page_id") or "")
                    page = self._page_in_connection(connection, page_id)
                    if page is None:
                        raise WikiNotFoundError(f"Wiki page not found: {page_id}")
                    page["status"] = "archived" if op_type == "archive_page" else "canonical"
                    self._write_page_in_connection(
                        connection,
                        page,
                        actor=actor,
                        reason=f"apply_proposal:{proposal_id}",
                        origin="compiler",
                        expected_revision=int(proposal["base_revisions"].get(page_id, page["revision"])),
                    )
                    touched.append(page_id)
            now = utc_now()
            connection.execute(
                "UPDATE proposals SET status='applied',applied_at=?,updated_at=? WHERE proposal_id=?",
                (now, now, proposal_id),
            )
            self._event(
                connection,
                "proposal_applied",
                actor,
                proposal_id=proposal_id,
                payload={
                    "page_ids": touched,
                    "comment": comment,
                    "approval_hash": approved_hash,
                    "selection": normalized_selection,
                },
            )
        for page_id in touched:
            self._materialize_page(self.get_page(page_id))
        self._materialize_indexes()
        applied = self.get_proposal(proposal_id)
        self._materialize_proposal(applied)
        applied["applied_page_ids"] = touched
        applied["approval_hash"] = approved_hash
        return applied

    def supersede_proposal(
        self,
        proposal_id: str,
        *,
        replacement_proposal_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise WikiNotFoundError(f"Wiki proposal not found: {proposal_id}")
            if str(row["status"]) != "pending_review":
                raise WikiConflictError(f"proposal is not pending: {row['status']}")
            replacement = connection.execute(
                "SELECT proposal_id FROM proposals WHERE proposal_id=?",
                (replacement_proposal_id,),
            ).fetchone()
            if replacement is None:
                raise WikiNotFoundError(
                    f"Replacement Wiki proposal not found: {replacement_proposal_id}"
                )
            now = utc_now()
            connection.execute(
                "UPDATE proposals SET status='superseded',superseded_by=?,reviewed_by=?,"
                "review_comment=?,reviewed_at=?,updated_at=? WHERE proposal_id=?",
                (replacement_proposal_id, actor, reason, now, now, proposal_id),
            )
            self._event(
                connection,
                "proposal_superseded",
                actor,
                proposal_id=proposal_id,
                payload={
                    "replacement_proposal_id": replacement_proposal_id,
                    "reason": reason,
                },
            )
        proposal = self.get_proposal(proposal_id)
        self._materialize_proposal(proposal)
        return proposal

    def reject_proposal(self, proposal_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                raise WikiNotFoundError(f"Wiki proposal not found: {proposal_id}")
            if str(row["status"]) != "pending_review":
                raise WikiConflictError(f"proposal is not pending: {row['status']}")
            now = utc_now()
            connection.execute(
                "UPDATE proposals SET status='rejected',reviewed_by=?,review_comment=?,reviewed_at=?,updated_at=? "
                "WHERE proposal_id=?",
                (actor, reason, now, now, proposal_id),
            )
            self._event(
                connection,
                "proposal_rejected",
                actor,
                proposal_id=proposal_id,
                payload={"reason": reason},
            )
        proposal = self.get_proposal(proposal_id)
        self._materialize_proposal(proposal)
        return proposal

    def claim_evidence(self, claim_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
            link_rows = connection.execute(
                "SELECT * FROM claim_evidence WHERE claim_id=? ORDER BY evidence_id",
                (claim_id,),
            ).fetchall()
        finally:
            connection.close()
        if row is None:
            raise WikiNotFoundError(f"Wiki claim not found: {claim_id}")
        claim = self._row_claim(row)
        links = [dict(item) for item in link_rows]
        if not links:
            links = [
                {
                    "claim_id": claim_id,
                    "evidence_id": evidence_id,
                    "relation": "supports",
                    "support_strength": 1.0,
                    "note": "",
                    "source_hash": "",
                    "verified_at": "",
                }
                for evidence_id in claim["evidence_ids"]
            ]
        claim["evidence_links"] = links
        link_by_id = {str(link["evidence_id"]): link for link in links}
        requested_evidence_ids = list(
            dict.fromkeys(
                str(link.get("evidence_id") or "").strip()
                for link in links
                if str(link.get("evidence_id") or "").strip()
            )
        )
        evidence = self.evidence_records(requested_evidence_ids)
        for record in evidence:
            link = link_by_id.get(str(record.get("evidence_id") or ""), {})
            record["wiki_relation"] = link.get("relation", "supports")
            record["wiki_support_strength"] = link.get("support_strength", 1.0)
            record["wiki_note"] = link.get("note", "")
        found_evidence_ids = {
            str(record.get("evidence_id") or "").strip()
            for record in evidence
            if str(record.get("evidence_id") or "").strip()
        }
        missing_evidence_ids = [
            evidence_id
            for evidence_id in requested_evidence_ids
            if evidence_id not in found_evidence_ids
        ]
        if not requested_evidence_ids:
            evidence_state = "none"
        elif not missing_evidence_ids:
            evidence_state = "complete"
        elif found_evidence_ids:
            evidence_state = "partial"
        else:
            evidence_state = "missing"
        return {
            "claim": claim,
            "links": links,
            "requested_evidence_ids": requested_evidence_ids,
            "missing_evidence_ids": missing_evidence_ids,
            "evidence_state": evidence_state,
            "evidence": evidence,
        }

    def evidence_records(self, evidence_ids: Any, *, limit: int = 50) -> list[dict[str, Any]]:
        identifiers = _strings(evidence_ids)[: max(1, min(100, int(limit)))]
        from tcode_agent.evidence import EvidenceStore

        records = EvidenceStore.get_many(identifiers, workdir=self.workdir.as_posix())
        return [records[evidence_id].to_dict() for evidence_id in identifiers if evidence_id in records]

    def stats(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            page_count = int(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
            active_count = int(
                connection.execute("SELECT COUNT(*) FROM pages WHERE status!='archived'").fetchone()[0]
            )
            claim_count = int(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
            pending_count = int(
                connection.execute("SELECT COUNT(*) FROM proposals WHERE status='pending_review'").fetchone()[0]
            )
            active_job_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
            )
            type_rows = connection.execute(
                "SELECT page_type,COUNT(*) AS count FROM pages WHERE status!='archived' GROUP BY page_type"
            ).fetchall()
            return {
                "enabled": True,
                "schema_version": self.SCHEMA_VERSION,
                "page_count": page_count,
                "active_page_count": active_count,
                "archived_page_count": page_count - active_count,
                "claim_count": claim_count,
                "pending_proposal_count": pending_count,
                "active_job_count": active_job_count,
                "page_types": {row["page_type"]: int(row["count"]) for row in type_rows},
                "fts_available": self._fts_available,
                "vector": self.vector_status(),
            }
        finally:
            connection.close()

    def stats_for_scope(
        self,
        *,
        allowed_page_ids: Iterable[str] | None,
        allowed_page_types: Iterable[str] | None,
    ) -> dict[str, Any]:
        scope, values = self._page_scope_condition(
            allowed_page_ids,
            allowed_page_types,
            alias="p",
        )
        scope = scope or "0"
        connection = self._connect()
        try:
            page_count = int(
                connection.execute(f"SELECT COUNT(*) FROM pages p WHERE {scope}", values).fetchone()[0]
            )
            active_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM pages p WHERE ({scope}) AND p.status!='archived'",
                    values,
                ).fetchone()[0]
            )
            claim_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM claims c JOIN pages p ON p.page_id=c.page_id WHERE {scope}",
                    values,
                ).fetchone()[0]
            )
            type_rows = connection.execute(
                f"""
                SELECT p.page_type,COUNT(*) AS count FROM pages p
                WHERE ({scope}) AND p.status!='archived' GROUP BY p.page_type
                """,
                values,
            ).fetchall()
            return {
                "enabled": True,
                "schema_version": self.SCHEMA_VERSION,
                "page_count": page_count,
                "active_page_count": active_count,
                "archived_page_count": page_count - active_count,
                "claim_count": claim_count,
                "pending_proposal_count": 0,
                "active_job_count": 0,
                "page_types": {row["page_type"]: int(row["count"]) for row in type_rows},
                "fts_available": self._fts_available,
                "vector": self.vector_status(),
                "read_only": True,
            }
        finally:
            connection.close()

    def allowed_evidence_ids(
        self,
        *,
        allowed_page_ids: Iterable[str] | None,
        allowed_page_types: Iterable[str] | None,
    ) -> set[str]:
        scope, values = self._page_scope_condition(
            allowed_page_ids,
            allowed_page_types,
            alias="p",
        )
        if not scope:
            return set()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT DISTINCT d.dependency_id AS evidence_id
                FROM dependencies d JOIN pages p ON p.page_id=d.page_id
                WHERE d.dependency_type='evidence' AND ({scope})
                UNION
                SELECT DISTINCT ce.evidence_id AS evidence_id
                FROM claim_evidence ce
                JOIN claims c ON c.claim_id=ce.claim_id
                JOIN pages p ON p.page_id=c.page_id
                WHERE {scope}
                """,
                [*values, *values],
            ).fetchall()
            return {str(row["evidence_id"]) for row in rows if str(row["evidence_id"] or "")}
        finally:
            connection.close()

    def list_entity_types_for_scope(
        self,
        *,
        allowed_page_ids: Iterable[str] | None,
        allowed_page_types: Iterable[str] | None,
    ) -> dict[str, Any]:
        scope, values = self._page_scope_condition(
            allowed_page_ids,
            allowed_page_types,
            alias="p",
        )
        if not scope:
            return {"count": 0, "types": []}
        connection = self._connect()
        try:
            page_type_rows = connection.execute(
                f"SELECT DISTINCT p.page_type FROM pages p WHERE {scope}", values
            ).fetchall()
            type_ids = sorted(
                {str(row["page_type"]) for row in page_type_rows}
                | {str(item) for item in (allowed_page_types or []) if str(item)}
            )
            if not type_ids:
                return {"count": 0, "types": []}
            rows = connection.execute(
                "SELECT type_id,display_name,parent_type,status,schema_json,created_at,updated_at "
                f"FROM entity_types WHERE status='approved' AND type_id IN ({','.join('?' for _ in type_ids)}) "
                "ORDER BY type_id",
                type_ids,
            ).fetchall()
        finally:
            connection.close()
        types: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["schema"] = _loads(item.pop("schema_json", "{}"), {})
            types.append(item)
        return {"count": len(types), "types": types}

    def list_entity_types(self, include_all: bool = False) -> dict[str, Any]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT type_id,display_name,parent_type,status,schema_json,created_at,updated_at "
                "FROM entity_types " + ("" if include_all else "WHERE status='approved' ") + "ORDER BY type_id"
            ).fetchall()
        finally:
            connection.close()
        types: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["schema"] = _loads(item.pop("schema_json", "{}"), {})
            types.append(item)
        return {"count": len(types), "types": types}

    def save_entity_type(self, type_id: str, display_name: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", type_id):
            raise WikiValidationError("类型 ID 必须是小写字母开头的字母、数字或下划线，最多 64 字符")
        if not display_name.strip() or len(display_name) > 100:
            raise WikiValidationError("请输入 1–100 字符的类型名称")
        connection = self._connect()
        try:
            with connection:
                if connection.execute("SELECT 1 FROM entity_types WHERE type_id=?", (type_id,)).fetchone():
                    raise WikiConflictError("该类型 ID 已存在")
                now = utc_now()
                connection.execute("INSERT INTO entity_types VALUES(?,?,?,'pending',?,?,?)",
                                   (type_id, display_name.strip(), "", _json({"requested_by": actor}), now, now))
                self._event(connection, "entity_type_requested", actor, payload={"type_id": type_id})
        finally:
            connection.close()
        return self.list_entity_types(include_all=True)

    def review_entity_type(self, type_id: str, status: str, *, actor: str, reason: str = "", expected_status: str) -> dict[str, Any]:
        transitions = {"pending": {"approved", "rejected"}, "approved": {"disabled"}, "disabled": {"approved"}, "rejected": {"pending"}}
        if status not in transitions.get(expected_status, set()):
            raise WikiValidationError("不允许的类型状态变更")
        if status == "rejected" and not reason.strip():
            raise WikiValidationError("驳回原因必填")
        connection = self._connect()
        try:
            with connection:
                row = connection.execute("SELECT * FROM entity_types WHERE type_id=?", (type_id,)).fetchone()
                if row is None:
                    raise WikiNotFoundError("类型不存在")
                metadata = _loads(row["schema_json"], {})
                if "requested_by" not in metadata:
                    raise WikiValidationError("内置实体类型不能停用或审核")
                metadata.update(reviewed_by=actor, review_reason=reason[:1000])
                cursor = connection.execute("UPDATE entity_types SET status=?,schema_json=?,updated_at=? WHERE type_id=? AND status=?",
                                            (status, _json(metadata), utc_now(), type_id, expected_status))
                if cursor.rowcount != 1:
                    raise WikiConflictError("类型状态已变化，请刷新")
                self._event(connection, "entity_type_reviewed", actor, payload={"type_id": type_id, "status": status, "reason": reason[:1000]})
        finally:
            connection.close()
        return self.list_entity_types(include_all=True)

    def note_draft(self, page_id: str, *, actor: str, content: str | None = None, base_revision: int = 0, expected_revision: int = 0, delete: bool = False) -> dict[str, Any]:
        self.get_page(page_id)
        connection = self._connect()
        try:
            with connection:
                if content is not None or delete:
                    connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM note_drafts WHERE page_id=? AND actor=?", (page_id, actor)).fetchone()
                if content is not None or delete:
                    if (row["revision"] if row else 0) != expected_revision:
                        raise WikiConflictError("草稿已被另一个窗口修改，请重新打开笔记")
                    if delete:
                        connection.execute("DELETE FROM note_drafts WHERE page_id=? AND actor=?", (page_id, actor))
                        return {"draft": None}
                    if len(content) > 200000:
                        raise WikiValidationError("草稿超过 200000 字符")
                    connection.execute("INSERT OR REPLACE INTO note_drafts VALUES(?,?,?,?,?,?)",
                                       (page_id, actor, content, base_revision, expected_revision + 1, utc_now()))
                    row = connection.execute("SELECT * FROM note_drafts WHERE page_id=? AND actor=?", (page_id, actor)).fetchone()
                return {"draft": dict(row) if row else None}
        finally:
            connection.close()

    def vector_status(self) -> dict[str, Any]:
        state = self.vector_index.state()
        dirty = (
            self._meta_value("vector_index_dirty", "1") == "1"
            or self.vector_index.requires_rebuild()
        )
        return {
            "enabled": self.vector_search_enabled,
            "available": self.vector_index.available(),
            "dirty": dirty,
            "min_pages": self.vector_index_min_pages,
            "weight": self.vector_search_weight,
            "state": state,
        }

    def rebuild_vector_index(self, *, force: bool = False, force_embeddings: bool = False) -> dict[str, Any]:
        if not self.vector_search_enabled:
            return {"status": "disabled", "count": 0}
        status = self.vector_status()
        if not force and not force_embeddings and not status["dirty"] and status["state"].get("status") == "ready":
            return {**status["state"], "reused": True}
        try:
            generation = int(self._meta_value("vector_index_generation", "0") or 0)
        except Exception:
            generation = 0
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM pages WHERE status!='archived' ORDER BY page_id"
            ).fetchall()
            if len(rows) < self.vector_index_min_pages:
                result = {
                    "status": "below_threshold",
                    "count": len(rows),
                    "min_pages": self.vector_index_min_pages,
                    "built_at": utc_now(),
                }
                self.vector_index.write_state(result)
                result["clean"] = self._mark_vector_clean_if_generation(generation)
                return result
            items: list[dict[str, Any]] = []
            for row in rows:
                page = self._row_page(row) or {}
                claims = self._claims_for(connection, str(page.get("page_id") or ""))
                search_text = "\n".join(
                    [
                        str(page.get("title") or ""),
                        " ".join(page.get("aliases") or []),
                        str(page.get("summary") or ""),
                        str(page.get("content") or ""),
                        *[str(claim.get("statement") or "") for claim in claims],
                    ]
                )
                items.append(
                    {
                        "page_id": page["page_id"],
                        "title": page.get("title"),
                        "revision": page.get("revision"),
                        "content_hash": page.get("content_hash"),
                        "search_text": search_text,
                    }
                )
        finally:
            connection.close()
        result = self.vector_index.rebuild(items, force_embeddings=True) if force_embeddings else self.vector_index.rebuild(items)
        if result.get("status") in {"ready", "empty"}:
            result["clean"] = self._mark_vector_clean_if_generation(generation)
            if result["clean"]:
                self._set_meta_value("vector_index_built_at", result.get("built_at") or utc_now())
        return result

    def _meta_value(self, key: str, default: str = "") -> str:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"] if row is not None else default)
        finally:
            connection.close()

    def _set_meta_value(self, key: str, value: Any) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                (key, str(value)),
            )
        finally:
            connection.close()

    def reconcile_external_edits(
        self,
        *,
        actor: str,
        force: bool = False,
        dry_run: bool = False,
        max_pages: int = 2000,
        settle_seconds: float = 1.5,
    ) -> dict[str, Any]:
        started = time.time()
        last_epoch = float(self._meta_value("markdown_reconcile_epoch", "0") or 0)
        if not force and started - last_epoch < 2.0:
            return {
                "status": "throttled",
                "changed_count": 0,
                "changed_pages": [],
                "rejected_count": 0,
                "rejected_pages": [],
                "issues": [],
                "scanned_count": 0,
            }
        safe_limit = max(1, min(10_000, int(max_pages)))
        cursor = "" if force else self._meta_value("markdown_reconcile_cursor", "")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM pages WHERE page_id>? ORDER BY page_id LIMIT ?",
                (cursor, safe_limit),
            ).fetchall()
            if not rows and cursor:
                rows = connection.execute(
                    "SELECT * FROM pages ORDER BY page_id LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        finally:
            connection.close()

        changed_pages: list[dict[str, Any]] = []
        rejected_pages: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        baseline_count = 0
        latest_cursor = cursor
        for row in rows:
            page = self._row_page(row) or {}
            page_id = str(page.get("page_id") or "")
            latest_cursor = page_id
            markdown_path = Path(str(page.get("markdown_path") or ""))
            if not markdown_path.is_absolute():
                markdown_path = self.workdir / markdown_path
            if not markdown_path.is_file():
                issues.append(
                    {
                        "code": "markdown_missing",
                        "page_id": page_id,
                        "markdown_path": str(page.get("markdown_path") or ""),
                    }
                )
                continue
            try:
                stat = markdown_path.stat()
                text = markdown_path.read_text(encoding="utf-8")
            except Exception as exc:
                issues.append(
                    {
                        "code": "markdown_read_failed",
                        "page_id": page_id,
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if file_hash == str(page.get("materialized_hash") or ""):
                continue
            if not force and time.time() - float(stat.st_mtime) < max(0.0, settle_seconds):
                issues.append({"code": "markdown_edit_settling", "page_id": page_id})
                continue
            try:
                current = self.get_page(page_id)
                candidate = parse_materialized_markdown(text, existing_page=current)
            except Exception as exc:
                issues.append(
                    {
                        "code": "markdown_parse_failed",
                        "page_id": page_id,
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            if (
                not str(page.get("materialized_hash") or "")
                and semantic_projection(candidate) == semantic_projection(current)
            ):
                if not dry_run:
                    self._materialize_page(current)
                baseline_count += 1
                continue
            if semantic_projection(candidate) == semantic_projection(current):
                if not dry_run:
                    self._materialize_page(current)
                baseline_count += 1
                continue
            rejected = {
                "page_id": page_id,
                "title": current.get("title"),
                "revision": current.get("revision"),
                "dry_run": bool(dry_run),
            }
            rejected_pages.append(rejected)
            issues.append(
                {
                    "code": "machine_markdown_edit_rejected",
                    "page_id": page_id,
                    "message": "Agent-derived Wiki Markdown is read-only; edit user notes instead.",
                }
            )
            if not dry_run:
                latest_text = markdown_path.read_text(encoding="utf-8")
                if hashlib.sha256(latest_text.encode("utf-8")).hexdigest() != file_hash:
                    issues.append({"code": "markdown_changed_during_sync", "page_id": page_id})
                    continue
                self._materialize_page(current)
        if changed_pages and not dry_run:
            self._materialize_indexes()
        self._set_meta_value("markdown_reconcile_epoch", started)
        if latest_cursor:
            self._set_meta_value("markdown_reconcile_cursor", latest_cursor)
        return {
            "status": "ok" if not issues else "warning",
            "changed_count": len(changed_pages),
            "changed_pages": changed_pages,
            "rejected_count": len(rejected_pages),
            "rejected_pages": rejected_pages,
            "baseline_count": baseline_count,
            "issues": issues,
            "scanned_count": len(rows),
            "cursor": latest_cursor,
            "duration_ms": int((time.time() - started) * 1000),
        }

    def create_job(
        self,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        job_id = f"wiki-job-{uuid.uuid4().hex[:16]}"
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(job_id,job_type,status,progress,message,payload_json,result_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    str(job_type or "wiki_job"),
                    "queued",
                    0.0,
                    str(message or ""),
                    _json(payload or {}),
                    _json({}),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        progress: float,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip()
        if normalized_status not in {"queued", "running", "completed", "failed"}:
            raise WikiValidationError(f"unsupported Wiki job status: {normalized_status}")
        with self._transaction() as connection:
            current = connection.execute("SELECT job_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if current is None:
                raise WikiNotFoundError(f"Wiki job not found: {job_id}")
            connection.execute(
                "UPDATE jobs SET status=?,progress=?,message=?,result_json=?,updated_at=? WHERE job_id=?",
                (
                    normalized_status,
                    max(0.0, min(1.0, float(progress))),
                    str(message or ""),
                    _json(result or {}),
                    utc_now(),
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise WikiNotFoundError(f"Wiki job not found: {job_id}")
        job = dict(row)
        job["payload"] = _loads(job.pop("payload_json", "{}"), {})
        job["result"] = _loads(job.pop("result_json", "{}"), {})
        return job

    def list_jobs(self, *, status: str = "", limit: int = 30) -> dict[str, Any]:
        safe_limit = max(1, min(100, int(limit)))
        connection = self._connect()
        try:
            if status:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        finally:
            connection.close()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            job = dict(row)
            job["payload"] = _loads(job.pop("payload_json", "{}"), {})
            job["result"] = _loads(job.pop("result_json", "{}"), {})
            jobs.append(job)
        return {"count": len(jobs), "jobs": jobs}

    def lint(self, *, persist: bool = True) -> dict[str, Any]:
        """检查 Wiki 的证据完整性、实体合法性与表格列语义一致性。"""

        issues: list[dict[str, Any]] = []
        from tcode_agent.evidence import EvidenceStore

        connection = self._connect()
        try:
            pages = [
                self._row_page(row) or {}
                for row in connection.execute("SELECT * FROM pages WHERE status!='archived'").fetchall()
            ]
            claims_by_page = {
                str(page["page_id"]): self._claims_for(connection, str(page["page_id"]))
                for page in pages
            }
            evidence_map = EvidenceStore.get_many(
                [
                    str(link.get("evidence_id") or "")
                    for claims in claims_by_page.values()
                    for claim in claims
                    for link in claim.get("evidence_links") or []
                    if str(link.get("evidence_id") or "")
                ],
                workdir=self.workdir.as_posix(),
            )
            for page in pages:
                claims = claims_by_page[str(page["page_id"])]
                page_text = "\n".join(
                    str(page.get(key) or "")
                    for key in ("title", "summary", "content", "canonical_key")
                )
                if contains_pdf_control_code(page_text):
                    issues.append(
                        {
                            "severity": "error",
                            "code": "pdf_control_code_leak",
                            "page_id": page["page_id"],
                        }
                    )
                if (
                    str(page.get("page_type") or "").strip().lower() == "mutation"
                    and not find_valid_mutations(str(page.get("title") or ""))
                ):
                    issues.append(
                        {
                            "severity": "error",
                            "code": "invalid_mutation_entity",
                            "page_id": page["page_id"],
                        }
                    )
                if not claims and not str(page.get("content") or "").strip():
                    issues.append(
                        {"severity": "warning", "code": "empty_page", "page_id": page["page_id"]}
                    )
                for claim in claims:
                    claim_scope = dict(claim.get("scope") or {})
                    claim_fact_type = str(
                        claim_scope.get("fact_type") or ""
                    ).strip().lower()
                    if contains_pdf_control_code(str(claim.get("statement") or "")):
                        issues.append(
                            {
                                "severity": "error",
                                "code": "pdf_control_code_leak",
                                "page_id": page["page_id"],
                                "claim_id": claim["claim_id"],
                            }
                        )
                    if (
                        claim_fact_type
                        in {"table_measurement", "kinetic_metric", "assay_result"}
                        and str(claim_scope.get("table_id") or "").strip()
                    ):
                        alignment_status = str(
                            claim_scope.get("table_alignment_status") or ""
                        ).strip().lower()
                        if alignment_status != "aligned":
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "unverified_table_alignment",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                }
                            )
                        expected_columns = _safe_int(
                            claim_scope.get("expected_column_count") or 0
                        )
                        actual_columns = _safe_int(
                            claim_scope.get("actual_column_count") or 0
                        )
                        if (
                            alignment_status == "aligned"
                            and expected_columns
                            and actual_columns
                            and expected_columns != actual_columns
                        ):
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "table_column_count_mismatch",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                }
                            )
                        fact_label = str(claim_scope.get("fact_label") or "").strip()
                        table_header = str(claim_scope.get("header") or "").strip()
                        if (
                            alignment_status == "aligned"
                            and fact_label
                            and table_header
                            and fact_label.casefold() != table_header.casefold()
                        ):
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "table_metric_label_mismatch",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                }
                            )
                    if not claim["evidence_ids"] and claim["origin"] != "user_note":
                        issues.append(
                            {
                                "severity": "warning",
                                "code": "claim_without_evidence",
                                "page_id": page["page_id"],
                                "claim_id": claim["claim_id"],
                            }
                        )
                    links = list(claim.get("evidence_links") or [])
                    supporting_links = [
                        link for link in links if link.get("relation") == "supports"
                    ]
                    if links and not supporting_links and claim["origin"] != "user_note":
                        issues.append(
                            {
                                "severity": "warning",
                                "code": "claim_without_supporting_evidence",
                                "page_id": page["page_id"],
                                "claim_id": claim["claim_id"],
                            }
                        )
                    active_support_count = 0
                    for link in links:
                        evidence_id = str(link.get("evidence_id") or "")
                        record = evidence_map.get(evidence_id)
                        if record is None:
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "unknown_evidence_id",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                    "evidence_id": evidence_id,
                                    "relation": link.get("relation"),
                                }
                            )
                            continue
                        evidence_semantic_text = "\n".join(
                            [
                                str(record.content or ""),
                                json.dumps(record.metadata or {}, ensure_ascii=False),
                            ]
                        )
                        if contains_pdf_control_code(evidence_semantic_text):
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "evidence_pdf_control_code_leak",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                    "evidence_id": evidence_id,
                                }
                            )
                        paper_fact = dict((record.metadata or {}).get("paper_fact") or {})
                        paper_attributes = dict(paper_fact.get("attributes") or {})
                        paper_fact_type = str(
                            paper_fact.get("fact_type") or ""
                        ).strip().lower()
                        if (
                            paper_fact_type
                            in {"table_measurement", "kinetic_metric", "assay_result"}
                            and str(paper_attributes.get("table_id") or "").strip()
                            and str(
                                paper_attributes.get("table_alignment_status") or ""
                            ).strip().lower()
                            != "aligned"
                        ):
                            issues.append(
                                {
                                    "severity": "error",
                                    "code": "evidence_unverified_table_alignment",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                    "evidence_id": evidence_id,
                                }
                            )
                        if link.get("relation") == "supports" and record.status == "active":
                            active_support_count += 1
                        if record.status != "active":
                            issues.append(
                                {
                                    "severity": "error" if record.status == "retracted" else "warning",
                                    "code": f"claim_evidence_{record.status}",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                    "evidence_id": evidence_id,
                                }
                            )
                        source_hash = str(link.get("source_hash") or "")
                        if source_hash and source_hash != record.content_hash:
                            issues.append(
                                {
                                    "severity": "warning",
                                    "code": "claim_evidence_changed",
                                    "page_id": page["page_id"],
                                    "claim_id": claim["claim_id"],
                                    "evidence_id": evidence_id,
                                }
                            )
                    if supporting_links and active_support_count == 0:
                        issues.append(
                            {
                                "severity": "warning",
                                "code": "claim_without_active_support",
                                "page_id": page["page_id"],
                                "claim_id": claim["claim_id"],
                            }
                        )
                for doc_id in page.get("source_doc_ids") or []:
                    if not self._library_document_exists(str(doc_id)):
                        issues.append(
                            {
                                "severity": "error",
                                "code": "missing_source_document",
                                "page_id": page["page_id"],
                                "doc_id": doc_id,
                            }
                        )
            dependency_rows = connection.execute(
                "SELECT d.page_id,d.dependency_id FROM dependencies d "
                "LEFT JOIN pages p ON p.page_id=d.page_id WHERE p.page_id IS NULL"
            ).fetchall()
            for row in dependency_rows:
                issues.append(
                    {
                        "severity": "error",
                        "code": "orphan_dependency",
                        "page_id": row["page_id"],
                        "dependency_id": row["dependency_id"],
                    }
                )
        finally:
            connection.close()
        report = {
            "status": "error" if any(item["severity"] == "error" for item in issues) else "ok",
            "generated_at": utc_now(),
            "issue_count": len(issues),
            "issues": issues,
        }
        if persist:
            _atomic_write(self.reports_root / "lint_report.json", _pretty_json(report))
        return report

    def _materialize_page(self, page: dict[str, Any]) -> None:
        claims = list(page.get("claims") or [])
        aliases = _json(page.get("aliases") or [])
        source_docs = _json(page.get("source_doc_ids") or [])
        evidence_ids = _json(page.get("evidence_ids") or [])
        lines = [
            "---",
            f"page_id: {page['page_id']}",
            f"page_type: {page['page_type']}",
            f"status: {page['status']}",
            f"revision: {page['revision']}",
            f"content_hash: {page['content_hash']}",
            f"summary: {_json(str(page.get('summary') or ''))}",
            f"aliases: {aliases}",
            f"source_doc_ids: {source_docs}",
            f"evidence_ids: {evidence_ids}",
            f"updated_at: {page['updated_at']}",
            "---",
            "",
            f"# {page['title']}",
            "",
            str(page.get("content") or "").strip(),
            "",
        ]
        lines.extend([CLAIMS_START, "", "## 结构化结论", ""])
        for claim in claims:
            markers = " ".join(f"[evidence:{item}]" for item in claim.get("evidence_ids", []))
            lines.append(
                f"- <!-- paper-wiki:claim:{claim['claim_id']} --> "
                f"`{claim['support_status']}` {claim['statement']}"
                + (f" {markers}" if markers else "")
            )
        lines.extend(["", CLAIMS_END, ""])
        markdown_path = Path(page["markdown_path"])
        if not markdown_path.is_absolute():
            markdown_path = self.workdir / markdown_path
        materialized = "\n".join(lines).rstrip() + "\n"
        _atomic_write(markdown_path, materialized)
        materialized_hash = hashlib.sha256(materialized.encode("utf-8")).hexdigest()
        materialized_at = utc_now()
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE pages SET materialized_hash=?,materialized_at=? "
                "WHERE page_id=? AND revision=?",
                (materialized_hash, materialized_at, page["page_id"], int(page["revision"])),
            )
        finally:
            connection.close()
        page["materialized_hash"] = materialized_hash
        page["materialized_at"] = materialized_at
        snapshot_path = self.revisions_root / page["page_id"] / f"revision-{page['revision']}.json"
        _atomic_write(snapshot_path, _pretty_json(page))

    def _materialize_proposal(self, proposal: dict[str, Any]) -> None:
        _atomic_write(
            self.proposals_root / f"{proposal['proposal_id']}.json",
            _pretty_json(proposal),
        )

    def _materialize_indexes(self) -> None:
        pages: list[dict[str, Any]] = []
        offset = 0
        while True:
            listed = self.list_pages(include_archived=False, limit=200, offset=offset)
            batch = list(listed.get("pages") or [])
            pages.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(listed.get("total") or 0):
                break
        lines = ["# Paper Wiki Index", ""]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for page in pages:
            by_type.setdefault(str(page.get("page_type") or "concept"), []).append(page)
        for page_type in sorted(by_type):
            lines.extend([f"## {page_type}", ""])
            for page in sorted(by_type[page_type], key=lambda item: str(item.get("title") or "")):
                path = Path(page["markdown_path"])
                if not path.is_absolute():
                    path = self.workdir / path
                try:
                    relative = path.relative_to(self.exports_root.parent)
                except ValueError:
                    relative = path
                lines.append(
                    f"- [{page['title']}](../{relative.as_posix()}) · {page['status']} · r{page['revision']}"
                )
            lines.append("")
        _atomic_write(self.exports_root / "INDEX.md", "\n".join(lines).rstrip() + "\n")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT event_type,actor,page_id,proposal_id,payload_json,created_at "
                "FROM events ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        finally:
            connection.close()
        log_lines = ["# Paper Wiki Change Log", ""]
        for row in rows:
            target = row["page_id"] or row["proposal_id"] or "wiki"
            log_lines.append(f"- {row['created_at']} · {row['event_type']} · {target} · {row['actor']}")
        _atomic_write(self.exports_root / "LOG.md", "\n".join(log_lines).rstrip() + "\n")
