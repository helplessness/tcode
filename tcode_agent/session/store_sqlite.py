"""SQLite-backed session store with basic full-text search."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
import time
from pathlib import Path
from typing import Any, Optional

from ..sqlite_runtime import SQLITE_CONNECTION_LOCK, connect as sqlite_connect
from .revision_archive import RevisionArchiveStore

_LOCK = SQLITE_CONNECTION_LOCK
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_SEARCH_TEXT_MAX_CHARS = 4000
_SEARCH_TERM_MAX_COUNT = 256
_SEARCH_CORPUS_MAX_CHARS = 100000
_SEARCH_CHUNK_SIZE = 1200
_SEARCH_CHUNK_OVERLAP = 200
_SEARCH_CHUNK_MAX_COUNT = 256
_DEFAULT_TENANT_ID = "admin_tenant"
_DEFAULT_USER_ID = "local-user"
_DEFAULT_WORKSPACE_ID = "workspace-1"
_DEFAULT_REVISION_RETENTION = 20
_DEFAULT_REVISION_PRUNE_BATCH = 1


def _revision_retention_limit(value: Any = None) -> int:
    raw = (
        os.environ.get("TCODE_SESSION_REVISION_RETENTION")
        if value is None
        else value
    )
    if raw in (None, ""):
        return _DEFAULT_REVISION_RETENTION
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REVISION_RETENTION
    if parsed <= 0:
        return 0
    return max(1, min(parsed, 10_000))


def _revision_prune_batch() -> int:
    raw = os.environ.get("TCODE_SESSION_REVISION_PRUNE_BATCH")
    if raw in (None, ""):
        return _DEFAULT_REVISION_PRUNE_BATCH
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REVISION_PRUNE_BATCH
    return max(1, min(parsed, 100))


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except Exception:
        return default
    if isinstance(default, dict) and not isinstance(parsed, dict):
        return default
    if isinstance(default, list) and not isinstance(parsed, list):
        return default
    return parsed


def _normalized_owner_id(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = default
    return text[:128]


def _default_tenant_id() -> str:
    return _normalized_owner_id(
        os.environ.get("TCODE_DEFAULT_TENANT_ID"),
        _DEFAULT_TENANT_ID,
    )


def _default_user_id() -> str:
    return _normalized_owner_id(
        os.environ.get("TCODE_DEFAULT_USER_ID"),
        _DEFAULT_USER_ID,
    )


def _default_workspace_id() -> str:
    return _normalized_owner_id(
        os.environ.get("TCODE_DEFAULT_WORKSPACE_ID"),
        _DEFAULT_WORKSPACE_ID,
    )


def _storage_base(workdir: Optional[str] = None) -> Path:
    base = str(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
    return Path(base)


def _workspace_storage_root(workdir: Optional[str] = None) -> Path:
    base = _storage_base(workdir)
    configured = str(os.environ.get("TCODE_USER_WORKDIRS_ROOT", "") or "").strip()
    root = Path(configured).expanduser() if configured else base / "workdirs"
    if not root.is_absolute():
        root = base / root
    return root.resolve(strict=False)


def _workspace_owner_from_workdir(
    path_text: Any,
    *,
    storage_workdir: Optional[str] = None,
) -> Optional[tuple[str, str, str]]:
    """Return the scoped owner encoded by a managed workspace path.

    Only paths below ``<workspace-root>/<tenant>/<user>/<workspace>`` qualify.
    Arbitrary external workdirs must not be treated as ownership metadata.
    """

    raw_path = str(path_text or "").strip()
    if not raw_path:
        return None
    try:
        rel = Path(raw_path).expanduser().resolve(strict=False).relative_to(
            _workspace_storage_root(storage_workdir)
        )
    except Exception:
        return None
    if len(rel.parts) < 3:
        return None
    return (
        _normalized_owner_id(rel.parts[0], _default_tenant_id()),
        _normalized_owner_id(rel.parts[1], _default_user_id()),
        _normalized_owner_id(rel.parts[2], _default_workspace_id()),
    )


def _infer_workspace_id_from_workdir(
    path_text: Any,
    *,
    storage_workdir: Optional[str] = None,
) -> str:
    raw_path = str(path_text or "").strip()
    if not raw_path:
        return _default_workspace_id()

    try:
        path = Path(raw_path).expanduser().resolve()
    except Exception:
        path = Path(raw_path)

    scoped_owner = _workspace_owner_from_workdir(
        path,
        storage_workdir=storage_workdir,
    )
    if scoped_owner is not None:
        return scoped_owner[2]

    name = path.name.strip()
    return _normalized_owner_id(name, _default_workspace_id())


def _session_owner_values(
    session_data: dict[str, Any],
    *,
    existing_row: Optional[sqlite3.Row] = None,
    storage_workdir: Optional[str] = None,
) -> tuple[str, str, str]:
    metadata = session_data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = _json_loads(session_data.get("metadata_json"), {})
    if not isinstance(metadata, dict):
        metadata = {}

    existing_tenant_id = ""
    existing_user_id = ""
    existing_workspace_id = ""
    if existing_row is not None:
        try:
            existing_tenant_id = str(existing_row["tenant_id"] or "").strip()
            existing_user_id = str(existing_row["user_id"] or "").strip()
            existing_workspace_id = str(existing_row["workspace_id"] or "").strip()
        except Exception:
            pass

    tenant_id = (
        existing_tenant_id
        or session_data.get("tenant_id")
        or metadata.get("tenant_id")
        or _default_tenant_id()
    )
    user_id = (
        existing_user_id
        or session_data.get("user_id")
        or metadata.get("user_id")
        or metadata.get("owner_user_id")
        or _default_user_id()
    )
    normalized_tenant_id = _normalized_owner_id(tenant_id, _default_tenant_id())
    normalized_user_id = _normalized_owner_id(user_id, _default_user_id())
    scoped_owner = _workspace_owner_from_workdir(
        session_data.get("workdir"),
        storage_workdir=storage_workdir,
    )
    scoped_workspace_id = (
        scoped_owner[2]
        if scoped_owner is not None
        and scoped_owner[:2] == (normalized_tenant_id, normalized_user_id)
        else ""
    )
    workspace_id = (
        scoped_workspace_id
        or existing_workspace_id
        or session_data.get("workspace_id")
        or metadata.get("workspace_id")
        or metadata.get("workdir_name")
        or _infer_workspace_id_from_workdir(
            session_data.get("workdir"),
            storage_workdir=storage_workdir,
        )
    )
    return (
        normalized_tenant_id,
        normalized_user_id,
        _normalized_owner_id(workspace_id, _default_workspace_id()),
    )


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2EBEF
    )


def _iter_cjk_runs(text: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in text:
        if _is_cjk(char):
            current.append(char)
            continue
        if current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _dedupe_preserve(values: list[str], *, limit: int = _SEARCH_TERM_MAX_COUNT) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _query_terms_for_cjk(run: str) -> list[str]:
    size = len(run)
    if size <= 1:
        return [run] if run else []
    if size == 2:
        return [run]
    result: list[str] = []
    index = 0
    while index + 1 < size:
        result.append(run[index : index + 2])
        index += 2
    tail = run[-2:]
    if tail not in result:
        result.append(tail)
    return result


def _index_terms_for_cjk(run: str) -> list[str]:
    if not run:
        return []
    result = list(run)
    if len(run) >= 2:
        result.extend(run[idx : idx + 2] for idx in range(len(run) - 1))
    return result


def _extract_search_terms(text: str, *, for_query: bool) -> list[str]:
    normalized = _normalize_text(text).lower()
    if not normalized:
        return []

    terms: list[str] = []
    terms.extend(match.group(0) for match in _ASCII_TOKEN_RE.finditer(normalized))
    for run in _iter_cjk_runs(normalized):
        if for_query:
            terms.extend(_query_terms_for_cjk(run))
        else:
            terms.extend(_index_terms_for_cjk(run))
    return _dedupe_preserve(terms)


def _flatten_scalar_strings(
    value: Any,
    *,
    max_items: int = 48,
    max_chars: int = 2000,
) -> list[str]:
    result: list[str] = []
    consumed = 0

    def visit(item: Any):
        nonlocal consumed
        if len(result) >= max_items or consumed >= max_chars:
            return
        if item is None:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(key)
                visit(nested)
                if len(result) >= max_items or consumed >= max_chars:
                    return
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
                if len(result) >= max_items or consumed >= max_chars:
                    return
            return
        if isinstance(item, (str, int, float, bool)):
            text = _normalize_text(item)
            if not text:
                return
            remaining = max_chars - consumed
            if remaining <= 0:
                return
            if len(text) > remaining:
                text = text[:remaining]
            result.append(text)
            consumed += len(text)

    visit(value)
    return _dedupe_preserve(result, limit=max_items)


def _chunk_text(
    text: str,
    *,
    chunk_size: int = _SEARCH_CHUNK_SIZE,
    overlap: int = _SEARCH_CHUNK_OVERLAP,
    max_chunks: int = _SEARCH_CHUNK_MAX_COUNT,
) -> list[str]:
    source = _normalize_text(text)
    if not source:
        return []
    if len(source) <= chunk_size:
        return [source]

    step = max(1, chunk_size - overlap)
    chunks: list[str] = []
    index = 0
    while index < len(source) and len(chunks) < max_chunks:
        chunks.append(source[index : index + chunk_size])
        index += step
    return _dedupe_preserve(chunks, limit=max_chunks)


def _build_message_search_document(message: dict[str, Any]) -> tuple[str, str, list[str]]:
    preview_parts: list[str] = []
    corpus_fragments: list[str] = []
    corpus_chars = 0

    def add_preview(value: Any, *, max_chars: Optional[int] = None):
        text = _normalize_text(value)
        if not text:
            return
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        preview_parts.append(text)

    def add_corpus(value: Any, *, max_chars: Optional[int] = None):
        nonlocal corpus_chars
        text = _normalize_text(value)
        if not text:
            return
        remaining = _SEARCH_CORPUS_MAX_CHARS - corpus_chars
        if remaining <= 0:
            return
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        if len(text) > remaining:
            text = text[:remaining]
        corpus_fragments.append(text)
        corpus_chars += len(text)

    add_preview(message.get("content"))
    add_preview(message.get("role"))
    add_corpus(message.get("content"))
    add_corpus(message.get("role"))

    for fragment in _flatten_scalar_strings(message.get("error"), max_items=8, max_chars=600):
        add_preview(fragment, max_chars=200)
        add_corpus(fragment)

    for tool_call in message.get("tool_calls", []) or []:
        if not isinstance(tool_call, dict):
            continue
        add_preview(tool_call.get("tool_name"))
        add_preview(tool_call.get("call_id"))
        add_corpus(tool_call.get("tool_name"))
        add_corpus(tool_call.get("call_id"))

        for fragment in _flatten_scalar_strings(
            tool_call.get("arguments"), max_items=48, max_chars=6000
        ):
            add_preview(fragment, max_chars=160)
            add_corpus(fragment)

        error_text = _normalize_text(tool_call.get("error"))
        if error_text:
            add_preview(error_text, max_chars=240)
            add_corpus(error_text)

        output = _normalize_text(tool_call.get("output"))
        if output:
            add_preview(output, max_chars=400)
            add_corpus(output)

    raw_text = "\n".join(_dedupe_preserve(preview_parts, limit=128))
    if len(raw_text) > _SEARCH_TEXT_MAX_CHARS:
        raw_text = raw_text[:_SEARCH_TEXT_MAX_CHARS]

    corpus_fragments = _dedupe_preserve(corpus_fragments, limit=512)
    search_terms = " ".join(
        _dedupe_preserve(
            [
                term
                for fragment in corpus_fragments
                for term in _extract_search_terms(fragment, for_query=False)
            ],
            limit=max(_SEARCH_TERM_MAX_COUNT * 8, _SEARCH_TERM_MAX_COUNT),
        )
    )
    search_chunks = _dedupe_preserve(
        [
            chunk
            for fragment in corpus_fragments
            for chunk in _chunk_text(fragment)
        ],
        limit=_SEARCH_CHUNK_MAX_COUNT,
    )
    return raw_text, search_terms, search_chunks


def _escape_fts_term(term: str) -> str:
    return '"' + str(term or "").replace('"', '""') + '"'


def _prepare_search_query(query: str) -> tuple[str, str]:
    raw = _normalize_text(query).lower()
    if not raw:
        return "", ""
    terms = _extract_search_terms(raw, for_query=True)
    if not terms:
        return raw, ""
    return raw, " AND ".join(_escape_fts_term(term) for term in terms)


def _build_snippet(text: str, query: str, *, width: int = 180) -> str:
    source = str(text or "").strip()
    raw = _normalize_text(query)
    if not source:
        return ""
    if not raw:
        return source[:width]

    lowered_source = source.lower()
    lowered_query = raw.lower()
    position = lowered_source.find(lowered_query)
    if position < 0:
        return source[:width]

    end = position + len(raw)
    start = max(0, position - width // 3)
    stop = min(len(source), end + width // 2)
    prefix = "..." if start > 0 else ""
    suffix = "..." if stop < len(source) else ""
    highlighted = (
        source[start:position] + "[" + source[position:end] + "]" + source[end:stop]
    )
    return prefix + highlighted + suffix


def _pick_snippet_source(
    content: str,
    search_text: str,
    query: str,
    chunk_text: str = "",
) -> str:
    raw = _normalize_text(query).lower()
    plain_content = str(content or "")
    plain_search = str(search_text or "")
    plain_chunk = str(chunk_text or "")
    if raw and raw in plain_content.lower():
        return plain_content
    if raw and raw in plain_chunk.lower():
        return plain_chunk
    if raw and raw in plain_search.lower():
        return plain_search
    return plain_content or plain_chunk or plain_search


class SessionSQLiteStore:
    """Persistent session store with message-level FTS search."""

    _initialized: dict[Path, tuple[int, int, int]] = {}

    @classmethod
    def db_path(cls, workdir: Optional[str] = None) -> Path:
        return _storage_base(workdir) / ".tcode" / "sessions.sqlite"

    @classmethod
    def legacy_json_path(cls, workdir: Optional[str] = None) -> Path:
        return _storage_base(workdir) / ".tcode" / "sessions.json"

    @classmethod
    def _connect(cls, workdir: Optional[str] = None) -> sqlite3.Connection:
        path = cls.db_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(
            path.as_posix(),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @classmethod
    def ensure_initialized(
        cls, workdir: Optional[str] = None, *, force_migration: bool = True,
    ) -> Path:
        """Explicit initialization repairs legacy rows; routine queries reuse it."""
        path = cls.db_path(workdir).resolve()
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino,
                            int(conn.execute("PRAGMA schema_version").fetchone()[0]))
                if not force_migration and cls._initialized.get(path) == identity:
                    return path
                cls._create_schema(conn, workdir=workdir)
                cls._initialized[path] = (
                    stat.st_dev, stat.st_ino,
                    int(conn.execute("PRAGMA schema_version").fetchone()[0]),
                )
            finally:
                conn.close()
        return path

    @classmethod
    def _create_schema(
        cls,
        conn: sqlite3.Connection,
        *,
        workdir: Optional[str] = None,
    ) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'active',
                tenant_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT '',
                workdir TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                display_messages_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL DEFAULT 0,
                last_active_at REAL NOT NULL DEFAULT 0,
                compaction_count INTEGER NOT NULL DEFAULT 0,
                last_compacted_at REAL,
                total_tokens_saved INTEGER NOT NULL DEFAULT 0,
                current_revision INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                session_revision INTEGER NOT NULL DEFAULT 0,
                message_order INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                parent_id TEXT NOT NULL DEFAULT '',
                message_json TEXT NOT NULL DEFAULT '{}',
                search_text TEXT NOT NULL DEFAULT '',
                search_terms TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS message_search_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                session_revision INTEGER NOT NULL DEFAULT 0,
                chunk_order INTEGER NOT NULL DEFAULT 0,
                chunk_text TEXT NOT NULL DEFAULT '',
                search_terms TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS guidance_items (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT '',
                client_request_id TEXT NOT NULL,
                target_turn_id TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('steer', 'queue')),
                status TEXT NOT NULL CHECK(status IN (
                    'classifying', 'pending', 'claimed', 'deferred', 'applying',
                    'applied', 'superseded', 'cancelled', 'failed'
                )),
                priority INTEGER NOT NULL DEFAULT 100,
                message TEXT NOT NULL,
                file_refs_json TEXT NOT NULL DEFAULT '[]',
                output_format_json TEXT NOT NULL DEFAULT 'null',
                outcome_contract_json TEXT NOT NULL DEFAULT 'null',
                contract_revision_json TEXT NOT NULL DEFAULT 'null',
                apply_policy TEXT NOT NULL DEFAULT 'next_safe_point',
                descendant_policy TEXT NOT NULL DEFAULT 'preserve',
                supersedes_guidance_id TEXT NOT NULL DEFAULT '',
                received_phase TEXT NOT NULL DEFAULT '',
                runtime_id TEXT NOT NULL DEFAULT 'tcode-native',
                model_id TEXT NOT NULL DEFAULT '',
                harness_switch_version INTEGER NOT NULL DEFAULT 0,
                source_run_id TEXT NOT NULL DEFAULT '',
                claimed_safe_point TEXT NOT NULL DEFAULT '',
                claim_owner TEXT NOT NULL DEFAULT '',
                claim_token TEXT NOT NULL DEFAULT '',
                claim_lease_expires_at REAL,
                deferred_reason TEXT NOT NULL DEFAULT '',
                effect TEXT NOT NULL DEFAULT '',
                failure_code TEXT NOT NULL DEFAULT '',
                failure_detail TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                claimed_at REAL,
                applied_at REAL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                UNIQUE(session_id, client_request_id),
                UNIQUE(session_id, sequence)
            );

            CREATE TABLE IF NOT EXISTS guidance_outbox (
                event_id TEXT PRIMARY KEY,
                guidance_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                target_turn_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                published_at REAL,
                last_error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(guidance_id) REFERENCES guidance_items(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS guidance_turn_owners (
                session_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT '',
                stream_epoch INTEGER NOT NULL DEFAULT 0,
                completion_fenced INTEGER NOT NULL DEFAULT 0,
                lease_expires_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_revision ON sessions(current_revision);
            CREATE INDEX IF NOT EXISTS idx_messages_session_order
                ON messages(session_id, session_revision, message_order);
            CREATE INDEX IF NOT EXISTS idx_message_search_chunks_session
                ON message_search_chunks(session_id, session_revision, chunk_order);
            CREATE INDEX IF NOT EXISTS idx_message_search_chunks_message
                ON message_search_chunks(message_id);
            """
        )
        schema_changed = False
        schema_changed |= cls._ensure_column(
            conn, "sessions", "current_revision INTEGER NOT NULL DEFAULT 0"
        )
        schema_changed |= cls._ensure_column(
            conn, "sessions", "tenant_id TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "sessions", "user_id TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "sessions", "workspace_id TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "sessions", "display_messages_json TEXT NOT NULL DEFAULT '[]'"
        )
        schema_changed |= cls._ensure_column(
            conn, "messages", "session_revision INTEGER NOT NULL DEFAULT 0"
        )
        schema_changed |= cls._ensure_column(
            conn, "messages", "search_text TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "messages", "search_terms TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "messages", "guidance_id TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "guidance_items", "runtime_id TEXT NOT NULL DEFAULT 'tcode-native'"
        )
        schema_changed |= cls._ensure_column(
            conn, "guidance_items", "model_id TEXT NOT NULL DEFAULT ''"
        )
        schema_changed |= cls._ensure_column(
            conn, "guidance_items", "harness_switch_version INTEGER NOT NULL DEFAULT 0"
        )
        schema_changed |= cls._ensure_column(
            conn, "guidance_items", "source_run_id TEXT NOT NULL DEFAULT ''"
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_sessions_tenant_user
                ON sessions(tenant_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_workspace
                ON sessions(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_tenant_user_last_active
                ON sessions(tenant_id, user_id, last_active_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_guidance_latest
                ON messages(session_id, session_revision, guidance_id);
            CREATE INDEX IF NOT EXISTS idx_guidance_session_status_sequence
                ON guidance_items(session_id, status, sequence);
            CREATE INDEX IF NOT EXISTS idx_guidance_turn_status_sequence
                ON guidance_items(session_id, target_turn_id, status, sequence);
            CREATE INDEX IF NOT EXISTS idx_guidance_claim_lease
                ON guidance_items(status, claim_lease_expires_at);
            CREATE INDEX IF NOT EXISTS idx_guidance_owner_scope
                ON guidance_items(tenant_id, user_id, workspace_id, session_id);
            CREATE INDEX IF NOT EXISTS idx_guidance_outbox_unpublished
                ON guidance_outbox(published_at, next_attempt_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_guidance_owner_lease
                ON guidance_turn_owners(lease_expires_at);
            """
        )

        migrated = cls._migrate_existing_rows_locked(conn, workdir=workdir)
        fts_changed = cls._ensure_fts_schema_locked(conn)
        if schema_changed or migrated or fts_changed:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.execute(
                "INSERT INTO message_search_chunks_fts(message_search_chunks_fts) VALUES('rebuild')"
            )

    @classmethod
    def _ensure_column(
        cls,
        conn: sqlite3.Connection,
        table_name: str,
        column_definition: str,
    ) -> bool:
        column_name = column_definition.split()[0]
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name in existing:
            return False
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")
        return True

    @classmethod
    def _migrate_existing_rows_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        workdir: Optional[str] = None,
    ) -> bool:
        changed = False
        owner_rows = conn.execute(
            """
            SELECT id, workdir, metadata_json, tenant_id, user_id, workspace_id
            FROM sessions
            WHERE tenant_id = '' OR user_id = '' OR workspace_id = ''
            """
        ).fetchall()
        for row in owner_rows:
            metadata = _json_loads(row["metadata_json"], {})
            session_data = {
                "id": str(row["id"] or ""),
                "workdir": str(row["workdir"] or ""),
                "metadata": metadata if isinstance(metadata, dict) else {},
                "tenant_id": str(row["tenant_id"] or ""),
                "user_id": str(row["user_id"] or ""),
                "workspace_id": str(row["workspace_id"] or ""),
            }
            tenant_id, user_id, workspace_id = _session_owner_values(
                session_data,
                storage_workdir=workdir,
            )
            conn.execute(
                """
                UPDATE sessions
                SET tenant_id = ?, user_id = ?, workspace_id = ?
                WHERE id = ?
                """,
                (tenant_id, user_id, workspace_id, str(row["id"] or "")),
            )
            changed = True

        scoped_rows = conn.execute(
            """
            SELECT id, workdir, metadata_json, tenant_id, user_id, workspace_id
            FROM sessions
            WHERE workdir != ''
            """
        ).fetchall()
        for row in scoped_rows:
            scoped_owner = _workspace_owner_from_workdir(
                row["workdir"],
                storage_workdir=workdir,
            )
            if scoped_owner is None:
                continue
            tenant_id, user_id, workspace_id = scoped_owner
            if (
                str(row["tenant_id"] or "").strip() != tenant_id
                or str(row["user_id"] or "").strip() != user_id
                or str(row["workspace_id"] or "").strip() == workspace_id
            ):
                continue
            metadata = _json_loads(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["tenant_id"] = tenant_id
            metadata["user_id"] = user_id
            metadata["workspace_id"] = workspace_id
            conn.execute(
                """
                UPDATE sessions
                SET workspace_id = ?, metadata_json = ?
                WHERE id = ?
                """,
                (
                    workspace_id,
                    _json_dumps(metadata),
                    str(row["id"] or ""),
                ),
            )
            changed = True

        session_rows = conn.execute(
            """
            UPDATE sessions
            SET current_revision = 1
            WHERE current_revision = 0
              AND EXISTS (
                SELECT 1 FROM messages WHERE messages.session_id = sessions.id
              )
            """
        )
        changed |= int(session_rows.rowcount or 0) > 0

        message_rows = conn.execute(
            """
            UPDATE messages
            SET session_revision = COALESCE(
                (
                    SELECT CASE
                        WHEN sessions.current_revision > 0 THEN sessions.current_revision
                        ELSE 1
                    END
                    FROM sessions
                    WHERE sessions.id = messages.session_id
                ),
                1
            )
            WHERE session_revision = 0
            """
        )
        changed |= int(message_rows.rowcount or 0) > 0

        stale_rows = conn.execute(
            """
            SELECT id, content, message_json
            FROM messages
            WHERE search_text = '' OR search_terms = ''
            """
        ).fetchall()
        for row in stale_rows:
            payload = _json_loads(row["message_json"], {})
            if not isinstance(payload, dict):
                payload = {"content": str(row["content"] or "")}
            search_text, search_terms, _ = _build_message_search_document(payload)
            conn.execute(
                """
                UPDATE messages
                SET search_text = ?, search_terms = ?
                WHERE id = ?
                """,
                (search_text, search_terms, int(row["id"])),
            )
            changed = True

        stale_chunk_rows = conn.execute(
            """
            SELECT messages.id, messages.session_id, messages.session_revision, messages.message_json
            FROM messages
            LEFT JOIN message_search_chunks
              ON message_search_chunks.message_id = messages.id
            WHERE message_search_chunks.id IS NULL
            """
        ).fetchall()
        for row in stale_chunk_rows:
            payload = _json_loads(row["message_json"], {})
            if not isinstance(payload, dict):
                payload = {"content": ""}
            _, _, search_chunks = _build_message_search_document(payload)
            cls._insert_message_chunks_locked(
                conn,
                message_id=int(row["id"]),
                session_id=str(row["session_id"] or ""),
                session_revision=int(row["session_revision"] or 0),
                search_chunks=search_chunks,
            )
            changed = True
        return changed

    @classmethod
    def _owner_filter_clause(
        cls,
        *,
        table_alias: str = "",
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        prefix = f"{table_alias}." if table_alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None and str(tenant_id).strip():
            clauses.append(f"{prefix}tenant_id = ?")
            params.append(str(tenant_id).strip())
        if user_id is not None and str(user_id).strip():
            clauses.append(f"{prefix}user_id = ?")
            params.append(str(user_id).strip())
        if workspace_id is not None and str(workspace_id).strip():
            clauses.append(f"{prefix}workspace_id = ?")
            params.append(str(workspace_id).strip())
        return " AND ".join(clauses), params

    @classmethod
    def _ensure_fts_schema_locked(cls, conn: sqlite3.Connection) -> bool:
        current_sql_row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'messages_fts'
            """
        ).fetchone()
        current_sql = str(current_sql_row["sql"] or "") if current_sql_row else ""
        desired_columns = ("content", "search_text", "search_terms")
        needs_recreate = not current_sql or any(
            column not in current_sql for column in desired_columns
        )
        chunk_sql_row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'message_search_chunks_fts'
            """
        ).fetchone()
        chunk_sql = str(chunk_sql_row["sql"] or "") if chunk_sql_row else ""
        chunk_columns = ("chunk_text", "search_terms")
        chunk_needs_recreate = not chunk_sql or any(
            column not in chunk_sql for column in chunk_columns
        )

        if needs_recreate:
            conn.executescript(
                """
                DROP TRIGGER IF EXISTS messages_ai;
                DROP TRIGGER IF EXISTS messages_ad;
                DROP TRIGGER IF EXISTS messages_au;
                DROP TABLE IF EXISTS messages_fts;
                """
            )
        if chunk_needs_recreate:
            conn.executescript(
                """
                DROP TRIGGER IF EXISTS message_search_chunks_ai;
                DROP TRIGGER IF EXISTS message_search_chunks_ad;
                DROP TRIGGER IF EXISTS message_search_chunks_au;
                DROP TABLE IF EXISTS message_search_chunks_fts;
                """
            )

        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                search_text,
                search_terms,
                content='messages',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, search_text, search_terms)
                VALUES (new.id, new.content, new.search_text, new.search_terms);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, search_text, search_terms)
                VALUES('delete', old.id, old.content, old.search_text, old.search_terms);
            END;

            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, search_text, search_terms)
                VALUES('delete', old.id, old.content, old.search_text, old.search_terms);
                INSERT INTO messages_fts(rowid, content, search_text, search_terms)
                VALUES (new.id, new.content, new.search_text, new.search_terms);
            END;

            CREATE VIRTUAL TABLE IF NOT EXISTS message_search_chunks_fts USING fts5(
                chunk_text,
                search_terms,
                content='message_search_chunks',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS message_search_chunks_ai
            AFTER INSERT ON message_search_chunks BEGIN
                INSERT INTO message_search_chunks_fts(rowid, chunk_text, search_terms)
                VALUES (new.id, new.chunk_text, new.search_terms);
            END;

            CREATE TRIGGER IF NOT EXISTS message_search_chunks_ad
            AFTER DELETE ON message_search_chunks BEGIN
                INSERT INTO message_search_chunks_fts(
                    message_search_chunks_fts, rowid, chunk_text, search_terms
                )
                VALUES('delete', old.id, old.chunk_text, old.search_terms);
            END;

            CREATE TRIGGER IF NOT EXISTS message_search_chunks_au
            AFTER UPDATE ON message_search_chunks BEGIN
                INSERT INTO message_search_chunks_fts(
                    message_search_chunks_fts, rowid, chunk_text, search_terms
                )
                VALUES('delete', old.id, old.chunk_text, old.search_terms);
                INSERT INTO message_search_chunks_fts(rowid, chunk_text, search_terms)
                VALUES (new.id, new.chunk_text, new.search_terms);
            END;
            """
        )
        return needs_recreate or chunk_needs_recreate

    @classmethod
    def is_empty(cls, workdir: Optional[str] = None) -> bool:
        cls.ensure_initialized(workdir, force_migration=False)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                row = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
                return int((row or {"count": 0})["count"]) == 0
            finally:
                conn.close()

    @classmethod
    def import_legacy_sessions(
        cls,
        payload: dict[str, Any],
        workdir: Optional[str] = None,
    ) -> None:
        if not isinstance(payload, dict) or not payload:
            return
        cls.ensure_initialized(workdir, force_migration=False)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                cls._create_schema(conn, workdir=workdir)
                for session_id, data in payload.items():
                    if not isinstance(data, dict):
                        continue
                    session_payload = dict(data)
                    session_payload["id"] = str(session_payload.get("id") or session_id)
                    cls._upsert_locked(
                        conn,
                        session_payload,
                        storage_workdir=workdir,
                    )
            finally:
                conn.close()

    @classmethod
    def load_all(
        cls,
        workdir: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> dict[str, dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        where = f"WHERE {owner_clause}" if owner_clause else ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(
                    f"SELECT * FROM sessions {where} ORDER BY created_at DESC",
                    tuple(owner_params),
                ).fetchall()
                result: dict[str, dict[str, Any]] = {}
                for row in rows:
                    data = cls._session_row_to_dict(row)
                    data["messages"] = cls._load_messages_locked(conn, data["id"])
                    result[data["id"]] = data
                return result
            finally:
                conn.close()

    @classmethod
    def get(
        cls,
        session_id: str,
        workdir: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        suffix = f" AND {owner_clause}" if owner_clause else ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                row = conn.execute(
                    f"SELECT * FROM sessions WHERE id = ?{suffix} LIMIT 1",
                    (session_id, *owner_params),
                ).fetchone()
                if row is None:
                    return None
                data = cls._session_row_to_dict(row)
                data["messages"] = cls._load_messages_locked(conn, data["id"])
                return data
            finally:
                conn.close()

    @classmethod
    def upsert(cls, session_data: dict[str, Any], workdir: Optional[str] = None) -> None:
        cls.ensure_initialized(workdir, force_migration=False)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                cls._upsert_locked(conn, session_data, storage_workdir=workdir)
            finally:
                conn.close()

    @classmethod
    def _upsert_locked(
        cls,
        conn: sqlite3.Connection,
        session_data: dict[str, Any],
        *,
        storage_workdir: Optional[str] = None,
    ) -> None:
        session_id = str(session_data.get("id") or "").strip()
        if not session_id:
            return
        messages = [
            message
            for message in (session_data.get("messages", []) or [])
            if isinstance(message, dict)
        ]
        current_row = conn.execute(
            """
            SELECT current_revision, tenant_id, user_id, workspace_id
            FROM sessions
            WHERE id = ?
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        current_revision = (
            int(current_row["current_revision"] or 0) if current_row is not None else 0
        )
        tenant_id, user_id, workspace_id = _session_owner_values(
            session_data,
            existing_row=current_row,
            storage_workdir=storage_workdir,
        )
        messages_changed = cls._messages_changed_locked(
            conn,
            session_id,
            current_revision,
            messages,
        )
        next_revision = current_revision + 1 if messages_changed else current_revision
        conn.execute(
            """
            INSERT INTO sessions(
                id, parent_id, title, agent_name, status, tenant_id, user_id,
                workspace_id, workdir, metadata_json, display_messages_json,
                created_at, last_active_at,
                compaction_count, last_compacted_at, total_tokens_saved,
                current_revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id,
                title=excluded.title,
                agent_name=excluded.agent_name,
                status=excluded.status,
                tenant_id=excluded.tenant_id,
                user_id=excluded.user_id,
                workspace_id=excluded.workspace_id,
                workdir=excluded.workdir,
                metadata_json=excluded.metadata_json,
                display_messages_json=excluded.display_messages_json,
                created_at=excluded.created_at,
                last_active_at=excluded.last_active_at,
                compaction_count=excluded.compaction_count,
                last_compacted_at=excluded.last_compacted_at,
                total_tokens_saved=excluded.total_tokens_saved,
                current_revision=excluded.current_revision
            """,
            (
                session_id,
                str(session_data.get("parent_id") or ""),
                str(session_data.get("title") or ""),
                str(session_data.get("agent_name") or "general"),
                str(session_data.get("status") or "active"),
                tenant_id,
                user_id,
                workspace_id,
                str(session_data.get("workdir") or ""),
                _json_dumps(session_data.get("metadata") or {}),
                _json_dumps(session_data.get("display_messages") or []),
                float(session_data.get("created_at", 0) or 0),
                float(session_data.get("last_active_at", 0) or 0),
                int(session_data.get("compaction_count", 0) or 0),
                session_data.get("last_compacted_at"),
                int(session_data.get("total_tokens_saved", 0) or 0),
                next_revision,
            ),
        )
        if not messages_changed:
            return

        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND session_revision = ?",
            (session_id, next_revision),
        )
        for idx, message in enumerate(messages):
            search_text, search_terms, search_chunks = _build_message_search_document(message)
            cursor = conn.execute(
                """
                INSERT INTO messages(
                    session_id,
                    session_revision,
                    message_order,
                    role,
                    content,
                    parent_id,
                    message_json,
                    search_text,
                    search_terms,
                    guidance_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    next_revision,
                    idx,
                    str(message.get("role") or ""),
                    str(message.get("content") or ""),
                    str(message.get("parent_id") or ""),
                    _json_dumps(message),
                    search_text,
                    search_terms,
                    str(
                        (
                            (message.get("metadata") or {}).get("guidance") or {}
                        ).get("id")
                        or ""
                    )
                    if isinstance(message.get("metadata"), dict)
                    else "",
                ),
            )
            cls._insert_message_chunks_locked(
                conn,
                message_id=int(cursor.lastrowid or 0),
                session_id=session_id,
                session_revision=next_revision,
                search_chunks=search_chunks,
            )

        retention = _revision_retention_limit()
        if retention > 0:
            cls._prune_session_revisions_locked(
                conn,
                session_id=session_id,
                current_revision=next_revision,
                keep=retention,
                max_revisions=_revision_prune_batch(),
                workdir=storage_workdir,
            )

    @classmethod
    def _historical_revision_candidates_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        current_revision: int,
        keep: int,
    ) -> list[int]:
        rows = conn.execute(
            """
            SELECT DISTINCT session_revision
            FROM messages
            WHERE session_id = ?
            ORDER BY session_revision DESC
            """,
            (session_id,),
        ).fetchall()
        revisions = {
            int(row["session_revision"] or 0)
            for row in rows
            if int(row["session_revision"] or 0) > 0
        }
        if current_revision > 0:
            # An empty current revision has no messages row but still counts
            # toward the retention window.
            revisions.add(int(current_revision))
        newest = sorted(revisions, reverse=True)
        retained = set(newest[: max(1, int(keep))])
        return sorted(
            revision
            for revision in revisions
            if revision not in retained and revision != current_revision
        )

    @classmethod
    def _delete_session_revisions_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        revisions: list[int],
        workdir: Optional[str] = None,
    ) -> dict[str, int]:
        normalized = sorted({int(item) for item in revisions if int(item) > 0})
        if not normalized:
            return {"revision_count": 0, "message_count": 0, "message_bytes": 0}
        placeholders = ",".join("?" for _ in normalized)
        params: tuple[Any, ...] = (session_id, *normalized)
        stats = conn.execute(
            f"""
            SELECT
                COUNT(*) AS message_count,
                COALESCE(SUM(
                    LENGTH(content) + LENGTH(message_json) +
                    LENGTH(search_text) + LENGTH(search_terms)
                ), 0) AS message_bytes
            FROM messages
            WHERE session_id = ? AND session_revision IN ({placeholders})
            """,
            params,
        ).fetchone()
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
        if session_row is None:
            raise RuntimeError("session disappeared before revision archival")
        session_data = dict(session_row)
        for revision in normalized:
            message_rows = conn.execute(
                """SELECT * FROM messages
                   WHERE session_id = ? AND session_revision = ?
                   ORDER BY message_order""",
                (session_id, revision),
            ).fetchall()
            RevisionArchiveStore.archive(
                session=session_data,
                revision=revision,
                messages=[dict(row) for row in message_rows],
                workdir=workdir,
            )
        conn.execute(
            f"""
            DELETE FROM messages
            WHERE session_id = ? AND session_revision IN ({placeholders})
            """,
            params,
        )
        return {
            "revision_count": len(normalized),
            "message_count": int(stats["message_count"] or 0) if stats else 0,
            "message_bytes": int(stats["message_bytes"] or 0) if stats else 0,
        }

    @classmethod
    def _prune_session_revisions_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        current_revision: int,
        keep: int,
        max_revisions: Optional[int] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, int]:
        candidates = cls._historical_revision_candidates_locked(
            conn,
            session_id=session_id,
            current_revision=current_revision,
            keep=keep,
        )
        if max_revisions is not None:
            candidates = candidates[: max(0, int(max_revisions))]
        return cls._delete_session_revisions_locked(
            conn,
            session_id=session_id,
            revisions=candidates,
            workdir=workdir,
        )

    @classmethod
    def prune_historical_revisions(
        cls,
        workdir: Optional[str] = None,
        *,
        keep: Optional[int] = None,
        session_id: Optional[str] = None,
        apply: bool = False,
        max_delete_revisions: Optional[int] = None,
        idle_days: Optional[float] = None,
        idle_keep: Optional[int] = None,
    ) -> dict[str, Any]:
        """Preview or delete old snapshots; preview mode is the safe default."""

        cls.ensure_initialized(workdir, force_migration=False)
        retention = _revision_retention_limit(keep)
        if retention <= 0:
            raise ValueError("keep must be greater than zero")
        remaining = (
            None
            if max_delete_revisions is None or int(max_delete_revisions) <= 0
            else int(max_delete_revisions)
        )
        database_path = cls.db_path(workdir)
        report: dict[str, Any] = {
            "database": database_path.as_posix(),
            "apply": bool(apply),
            "keep": retention,
            "candidate_revision_count": 0,
            "candidate_message_count": 0,
            "candidate_message_bytes": 0,
            "sessions": [],
        }
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                clauses = "WHERE id = ?" if session_id else ""
                params = (str(session_id),) if session_id else ()
                session_rows = conn.execute(
                    f"""
                    SELECT id, current_revision, last_active_at, status
                    FROM sessions
                    {clauses}
                    ORDER BY id
                    """,
                    params,
                ).fetchall()
                for row in session_rows:
                    if remaining == 0:
                        break
                    target_session_id = str(row["id"] or "")
                    current_revision = int(row["current_revision"] or 0)
                    session_keep = retention
                    is_idle = False
                    if idle_days is not None and float(idle_days) >= 0:
                        cutoff = time.time() - float(idle_days) * 86400
                        is_idle = (
                            float(row["last_active_at"] or 0) <= cutoff
                            or str(row["status"] or "").lower() in {"idle", "completed", "archived"}
                        )
                    if is_idle and idle_keep is not None:
                        session_keep = max(1, int(idle_keep))
                    candidates = cls._historical_revision_candidates_locked(
                        conn,
                        session_id=target_session_id,
                        current_revision=current_revision,
                        keep=session_keep,
                    )
                    if remaining is not None:
                        candidates = candidates[:remaining]
                    if not candidates:
                        continue
                    stats = cls._delete_session_revisions_locked(
                        conn,
                        session_id=target_session_id,
                        revisions=candidates if apply else [],
                        workdir=workdir,
                    )
                    if not apply:
                        placeholders = ",".join("?" for _ in candidates)
                        preview = conn.execute(
                            f"""
                            SELECT
                                COUNT(*) AS message_count,
                                COALESCE(SUM(
                                    LENGTH(content) + LENGTH(message_json) +
                                    LENGTH(search_text) + LENGTH(search_terms)
                                ), 0) AS message_bytes
                            FROM messages
                            WHERE session_id = ?
                              AND session_revision IN ({placeholders})
                            """,
                            (target_session_id, *candidates),
                        ).fetchone()
                        stats = {
                            "revision_count": len(candidates),
                            "message_count": int(preview["message_count"] or 0),
                            "message_bytes": int(preview["message_bytes"] or 0),
                        }
                    session_report = {
                        "session_id": target_session_id,
                        "current_revision": current_revision,
                        "keep": session_keep,
                        "idle": is_idle,
                        "revisions": candidates,
                        **stats,
                    }
                    report["sessions"].append(session_report)
                    report["candidate_revision_count"] += stats["revision_count"]
                    report["candidate_message_count"] += stats["message_count"]
                    report["candidate_message_bytes"] += stats["message_bytes"]
                    if remaining is not None:
                        remaining -= len(candidates)
                report["freelist_pages"] = int(
                    conn.execute("PRAGMA freelist_count").fetchone()[0]
                )
                report["page_size"] = int(conn.execute("PRAGMA page_size").fetchone()[0])
            finally:
                conn.close()
        report["database_bytes"] = (
            database_path.stat().st_size if database_path.exists() else 0
        )
        return report

    @classmethod
    def compact_database(cls, workdir: Optional[str] = None) -> dict[str, int]:
        """Run an explicitly requested VACUUM after historical pruning."""

        cls.ensure_initialized(workdir, force_migration=False)
        database_path = cls.db_path(workdir)
        before = database_path.stat().st_size if database_path.exists() else 0
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            finally:
                conn.close()
        after = database_path.stat().st_size if database_path.exists() else 0
        return {"database_bytes_before": before, "database_bytes_after": after}

    @classmethod
    def _messages_changed_locked(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        current_revision: int,
        messages: list[dict[str, Any]],
    ) -> bool:
        if current_revision <= 0:
            return bool(messages)

        existing_rows = conn.execute(
            """
            SELECT message_json
            FROM messages
            WHERE session_id = ? AND session_revision = ?
            ORDER BY message_order ASC
            """,
            (session_id, current_revision),
        ).fetchall()
        existing_payloads = [
            str(row["message_json"] or "")
            for row in existing_rows
            if row["message_json"] not in (None, "")
        ]
        new_payloads = [_json_dumps(message) for message in messages]
        return existing_payloads != new_payloads

    @classmethod
    def _insert_message_chunks_locked(
        cls,
        conn: sqlite3.Connection,
        *,
        message_id: int,
        session_id: str,
        session_revision: int,
        search_chunks: list[str],
    ) -> None:
        if message_id <= 0:
            return
        conn.execute(
            "DELETE FROM message_search_chunks WHERE message_id = ?",
            (message_id,),
        )
        for idx, chunk_text in enumerate(search_chunks):
            normalized = _normalize_text(chunk_text)
            if not normalized:
                continue
            chunk_terms = " ".join(_extract_search_terms(normalized, for_query=False))
            conn.execute(
                """
                INSERT INTO message_search_chunks(
                    message_id,
                    session_id,
                    session_revision,
                    chunk_order,
                    chunk_text,
                    search_terms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    session_revision,
                    idx,
                    normalized,
                    chunk_terms,
                ),
            )

    @classmethod
    def delete(
        cls,
        session_id: str,
        workdir: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> None:
        cls.ensure_initialized(workdir, force_migration=False)
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        suffix = f" AND {owner_clause}" if owner_clause else ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute(
                    f"DELETE FROM sessions WHERE id = ?{suffix}",
                    (session_id, *owner_params),
                )
            finally:
                conn.close()

    @classmethod
    def clear(
        cls,
        workdir: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> None:
        cls.ensure_initialized(workdir, force_migration=False)
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                if owner_clause:
                    conn.execute(
                        f"DELETE FROM sessions WHERE {owner_clause}",
                        tuple(owner_params),
                    )
                else:
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM sessions")
            finally:
                conn.close()

    @classmethod
    def list(
        cls,
        workdir: Optional[str] = None,
        *,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if parent_id:
            clauses.append("parent_id = ?")
            params.append(str(parent_id))
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if owner_clause:
            clauses.append(owner_clause)
            params.extend(owner_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM sessions {where} ORDER BY created_at DESC"
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(query, tuple(params)).fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    data = cls._session_row_to_dict(row)
                    data["messages"] = cls._load_messages_locked(conn, data["id"])
                    result.append(data)
                return result
            finally:
                conn.close()

    @classmethod
    def list_workspace_metadata(cls, workdir: Optional[str] = None) -> list[dict[str, str]]:
        """List workspace owners without materializing messages or session snapshots."""
        cls.ensure_initialized(workdir, force_migration=False)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(
                    "SELECT workdir, tenant_id, user_id, workspace_id FROM sessions"
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    @classmethod
    def list_pending_continuation_ids(cls, workdir: Optional[str] = None) -> list[str]:
        """Find pending cards from session metadata without loading transcripts."""
        if not cls.db_path(workdir).is_file():
            return []
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(
                    "SELECT id, metadata_json FROM sessions "
                    "WHERE instr(metadata_json, 'pending_continuations') > 0"
                ).fetchall()
                pending_ids = []
                for row in rows:
                    metadata = _json_loads(row["metadata_json"], {})
                    items = metadata.get("pending_continuations", [])
                    if isinstance(items, list) and any(
                        isinstance(item, dict)
                        and str(item.get("delivery_status") or "").strip() != "delivered"
                        for item in items
                    ):
                        pending_ids.append(str(row["id"]))
                return pending_ids
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                return []
            finally:
                conn.close()

    @classmethod
    def recent(
        cls,
        workdir: Optional[str] = None,
        limit: int = 10,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        safe_limit = max(1, min(int(limit or 10), 100))
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        where = f"WHERE {owner_clause}" if owner_clause else ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(
                    f"""
                    SELECT * FROM sessions
                    {where}
                    ORDER BY last_active_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (*owner_params, safe_limit),
                ).fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    data = cls._session_row_to_dict(row)
                    last_message = conn.execute(
                        """
                        SELECT role, content FROM messages
                        WHERE session_id = ? AND session_revision = ?
                        ORDER BY message_order DESC
                        LIMIT 1
                        """,
                        (data["id"], data["current_revision"]),
                    ).fetchone()
                    if last_message:
                        data["last_message_role"] = str(last_message["role"] or "")
                        data["last_message_preview"] = str(last_message["content"] or "")[:240]
                    else:
                        data["last_message_role"] = ""
                        data["last_message_preview"] = ""
                    result.append(data)
                return result
            finally:
                conn.close()

    @classmethod
    def list_revisions(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
        limit: int = 20,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        safe_limit = max(1, min(int(limit or 20), 100))
        owner_clause, owner_params = cls._owner_filter_clause(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        suffix = f" AND {owner_clause}" if owner_clause else ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                session_row = conn.execute(
                    """
                    SELECT id, title, agent_name, current_revision, last_active_at
                    FROM sessions
                    WHERE id = ?
                    """ + suffix + """
                    LIMIT 1
                    """,
                    (str(session_id), *owner_params),
                ).fetchone()
                if session_row is None:
                    return []

                rows = conn.execute(
                    """
                    SELECT
                        messages.session_revision AS revision,
                        COUNT(*) AS message_count,
                        MAX(messages.id) AS latest_message_id
                    FROM messages
                    WHERE messages.session_id = ?
                    GROUP BY messages.session_revision
                    ORDER BY messages.session_revision DESC
                    LIMIT ?
                    """,
                    (str(session_id), safe_limit),
                ).fetchall()

                result: list[dict[str, Any]] = []
                for row in rows:
                    revision = int(row["revision"] or 0)
                    last_message = conn.execute(
                        """
                        SELECT role, content
                        FROM messages
                        WHERE session_id = ? AND session_revision = ?
                        ORDER BY message_order DESC
                        LIMIT 1
                        """,
                        (str(session_id), revision),
                    ).fetchone()
                    result.append(
                        {
                            "session_id": str(session_row["id"]),
                            "session_title": str(session_row["title"] or ""),
                            "agent_name": str(session_row["agent_name"] or ""),
                            "revision": revision,
                            "current_revision": int(session_row["current_revision"] or 0),
                            "is_current": revision == int(session_row["current_revision"] or 0),
                            "message_count": int(row["message_count"] or 0),
                            "last_active_at": float(session_row["last_active_at"] or 0),
                            "last_message_role": str(last_message["role"] or "") if last_message else "",
                            "last_message_preview": str(last_message["content"] or "")[:240]
                            if last_message
                            else "",
                        }
                    )
                return result
            finally:
                conn.close()

    @classmethod
    def restore_archived_revision(
        cls,
        session_id: str,
        revision: int,
        *,
        workdir: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """把一个冷归档 revision 恢复为可查询的历史 revision。"""

        owner = {
            "tenant_id": tenant_id or "",
            "user_id": user_id or "",
            "workspace_id": workspace_id or "",
        }
        payload = RevisionArchiveStore.read(
            session_id=session_id,
            revision=revision,
            owner=owner,
            workdir=workdir,
        )
        if payload is None:
            raise KeyError("archived session revision not found")
        cls.ensure_initialized(workdir, force_migration=False)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                owner_clause, owner_params = cls._owner_filter_clause(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                suffix = f" AND {owner_clause}" if owner_clause else ""
                session_row = conn.execute(
                    "SELECT current_revision FROM sessions WHERE id = ?" + suffix,
                    (session_id, *owner_params),
                ).fetchone()
                if session_row is None:
                    raise KeyError("session not found")
                if int(revision) == int(session_row["current_revision"] or 0):
                    raise ValueError("current revision cannot be restored from archive")
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ? AND session_revision = ?",
                    (session_id, int(revision)),
                )
                for row in payload.get("messages", []):
                    cursor = conn.execute(
                        """INSERT INTO messages(
                               session_id, session_revision, message_order, role, content,
                               parent_id, message_json, search_text, search_terms, guidance_id
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            session_id,
                            int(revision),
                            int(row.get("message_order", 0) or 0),
                            str(row.get("role") or ""),
                            str(row.get("content") or ""),
                            str(row.get("parent_id") or ""),
                            str(row.get("message_json") or "{}"),
                            str(row.get("search_text") or ""),
                            str(row.get("search_terms") or ""),
                            str(row.get("guidance_id") or ""),
                        ),
                    )
                    message = _json_loads(row.get("message_json"), {})
                    _, _, chunks = _build_message_search_document(message)
                    cls._insert_message_chunks_locked(
                        conn,
                        message_id=int(cursor.lastrowid or 0),
                        session_id=session_id,
                        session_revision=int(revision),
                        search_chunks=chunks,
                    )
                return {
                    "session_id": session_id,
                    "revision": int(revision),
                    "message_count": len(payload.get("messages", [])),
                    "restored": True,
                }
            finally:
                conn.close()

    @classmethod
    def _revision_filter(
        cls,
        *,
        table_alias: str,
        session_alias: str,
        revision: Optional[int],
        include_history: bool,
    ) -> tuple[str, list[Any]]:
        if revision is not None:
            return f"AND {table_alias}.session_revision = ?", [int(revision)]
        if include_history:
            return "", []
        return f"AND {table_alias}.session_revision = {session_alias}.current_revision", []

    @classmethod
    def search(
        cls,
        query: str,
        *,
        workdir: Optional[str] = None,
        limit: int = 5,
        session_id: Optional[str] = None,
        revision: Optional[int] = None,
        include_history: bool = False,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir, force_migration=False)
        raw_query, fts_query = _prepare_search_query(query)
        if not raw_query:
            return []
        safe_limit = max(1, min(int(limit or 5), 50))
        message_revision_clause, message_revision_params = cls._revision_filter(
            table_alias="messages",
            session_alias="sessions",
            revision=revision,
            include_history=include_history,
        )
        chunk_revision_clause, chunk_revision_params = cls._revision_filter(
            table_alias="message_search_chunks",
            session_alias="sessions",
            revision=revision,
            include_history=include_history,
        )
        session_clause = ""
        chunk_session_clause = ""
        exact_params: list[Any] = [raw_query, raw_query]
        chunk_exact_params: list[Any] = [raw_query]
        fts_params: list[Any] = []
        chunk_fts_params: list[Any] = []
        exact_params.extend(message_revision_params)
        chunk_exact_params.extend(chunk_revision_params)
        owner_clause, owner_params = cls._owner_filter_clause(
            table_alias="sessions",
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        owner_sql = f"AND {owner_clause}" if owner_clause else ""
        if session_id:
            session_clause = "AND messages.session_id = ?"
            chunk_session_clause = "AND message_search_chunks.session_id = ?"
            exact_params.append(str(session_id))
            chunk_exact_params.append(str(session_id))
        exact_params.extend(owner_params)
        chunk_exact_params.extend(owner_params)
        exact_params.append(safe_limit * 6)
        chunk_exact_params.append(safe_limit * 6)
        exact_sql = f"""
            SELECT
                messages.id AS message_rowid,
                messages.session_id AS session_id,
                messages.session_revision AS session_revision,
                sessions.title AS session_title,
                sessions.agent_name AS agent_name,
                sessions.current_revision AS current_revision,
                sessions.last_active_at AS last_active_at,
                messages.role AS role,
                messages.message_order AS message_order,
                messages.content AS content,
                messages.search_text AS search_text,
                '' AS chunk_text
            FROM messages
            JOIN sessions ON sessions.id = messages.session_id
            WHERE (
                instr(lower(messages.content), ?) > 0
                OR instr(lower(messages.search_text), ?) > 0
              )
              {message_revision_clause}
              {session_clause}
              {owner_sql}
            ORDER BY sessions.last_active_at DESC, messages.message_order DESC
            LIMIT ?
        """
        chunk_exact_sql = f"""
            SELECT
                messages.id AS message_rowid,
                messages.session_id AS session_id,
                messages.session_revision AS session_revision,
                sessions.title AS session_title,
                sessions.agent_name AS agent_name,
                sessions.current_revision AS current_revision,
                sessions.last_active_at AS last_active_at,
                messages.role AS role,
                messages.message_order AS message_order,
                messages.content AS content,
                messages.search_text AS search_text,
                message_search_chunks.chunk_text AS chunk_text
            FROM message_search_chunks
            JOIN messages ON messages.id = message_search_chunks.message_id
            JOIN sessions ON sessions.id = message_search_chunks.session_id
            WHERE instr(lower(message_search_chunks.chunk_text), ?) > 0
              {chunk_revision_clause}
              {chunk_session_clause}
              {owner_sql}
            ORDER BY sessions.last_active_at DESC, messages.message_order DESC
            LIMIT ?
        """

        fts_sql = f"""
            SELECT
                messages.id AS message_rowid,
                messages.session_id AS session_id,
                messages.session_revision AS session_revision,
                sessions.title AS session_title,
                sessions.agent_name AS agent_name,
                sessions.current_revision AS current_revision,
                sessions.last_active_at AS last_active_at,
                messages.role AS role,
                messages.message_order AS message_order,
                messages.content AS content,
                messages.search_text AS search_text,
                '' AS chunk_text,
                snippet(messages_fts, 0, '[', ']', ' ... ', 14) AS snippet,
                bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages ON messages_fts.rowid = messages.id
            JOIN sessions ON sessions.id = messages.session_id
            WHERE messages_fts MATCH ?
              {message_revision_clause}
              {session_clause}
              {owner_sql}
            ORDER BY score, sessions.last_active_at DESC
            LIMIT ?
        """
        chunk_fts_sql = f"""
            SELECT
                messages.id AS message_rowid,
                messages.session_id AS session_id,
                messages.session_revision AS session_revision,
                sessions.title AS session_title,
                sessions.agent_name AS agent_name,
                sessions.current_revision AS current_revision,
                sessions.last_active_at AS last_active_at,
                messages.role AS role,
                messages.message_order AS message_order,
                messages.content AS content,
                messages.search_text AS search_text,
                message_search_chunks.chunk_text AS chunk_text,
                snippet(message_search_chunks_fts, 0, '[', ']', ' ... ', 14) AS snippet,
                bm25(message_search_chunks_fts) AS score
            FROM message_search_chunks_fts
            JOIN message_search_chunks
              ON message_search_chunks_fts.rowid = message_search_chunks.id
            JOIN messages ON messages.id = message_search_chunks.message_id
            JOIN sessions ON sessions.id = message_search_chunks.session_id
            WHERE message_search_chunks_fts MATCH ?
              {chunk_revision_clause}
              {chunk_session_clause}
              {owner_sql}
            ORDER BY score, sessions.last_active_at DESC
            LIMIT ?
        """
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                merged: dict[int, dict[str, Any]] = {}

                def merge_row(
                    row: sqlite3.Row,
                    *,
                    snippet: str,
                    score: float,
                    exact_match: bool,
                ) -> None:
                    key = int(row["message_rowid"] or 0)
                    payload = {
                        "session_id": str(row["session_id"]),
                        "session_title": str(row["session_title"] or ""),
                        "agent_name": str(row["agent_name"] or ""),
                        "last_active_at": float(row["last_active_at"] or 0),
                        "role": str(row["role"] or ""),
                        "message_order": int(row["message_order"] or 0),
                        "snippet": snippet,
                        "score": score,
                        "exact_match": exact_match,
                        "revision": int(row["session_revision"] or 0),
                        "current_revision": int(row["current_revision"] or 0),
                        "is_current_revision": int(row["session_revision"] or 0)
                        == int(row["current_revision"] or 0),
                    }
                    existing = merged.get(key)
                    if not existing:
                        merged[key] = payload
                        return
                    existing["score"] = min(float(existing.get("score", 0.0)), float(score))
                    existing["exact_match"] = bool(existing.get("exact_match")) or exact_match
                    if not existing.get("snippet"):
                        existing["snippet"] = snippet
                    if not existing.get("is_current_revision") and payload["is_current_revision"]:
                        existing["is_current_revision"] = True
                    existing["current_revision"] = max(
                        int(existing.get("current_revision", 0)),
                        int(payload["current_revision"]),
                    )

                exact_rows = conn.execute(exact_sql, tuple(exact_params)).fetchall()
                for row in exact_rows:
                    snippet_source = _pick_snippet_source(
                        str(row["content"] or ""),
                        str(row["search_text"] or ""),
                        raw_query,
                    )
                    merge_row(
                        row,
                        snippet=_build_snippet(snippet_source, raw_query),
                        score=-1.0,
                        exact_match=True,
                    )

                chunk_exact_rows = conn.execute(
                    chunk_exact_sql, tuple(chunk_exact_params)
                ).fetchall()
                for row in chunk_exact_rows:
                    snippet_source = _pick_snippet_source(
                        str(row["content"] or ""),
                        str(row["search_text"] or ""),
                        raw_query,
                        str(row["chunk_text"] or ""),
                    )
                    merge_row(
                        row,
                        snippet=_build_snippet(snippet_source, raw_query),
                        score=-1.0,
                        exact_match=True,
                    )

                if fts_query:
                    fts_params.append(fts_query)
                    fts_params.extend(message_revision_params)
                    if session_id:
                        fts_params.append(str(session_id))
                    fts_params.extend(owner_params)
                    fts_params.append(safe_limit * 6)
                    fts_rows = conn.execute(fts_sql, tuple(fts_params)).fetchall()
                    for row in fts_rows:
                        snippet = str(row["snippet"] or "").strip()
                        if not snippet:
                            snippet_source = _pick_snippet_source(
                                str(row["content"] or ""),
                                str(row["search_text"] or ""),
                                raw_query,
                            )
                            snippet = _build_snippet(snippet_source, raw_query)
                        merge_row(
                            row,
                            snippet=snippet,
                            score=float(row["score"] or 0.0),
                            exact_match=False,
                        )

                    chunk_fts_params.append(fts_query)
                    chunk_fts_params.extend(chunk_revision_params)
                    if session_id:
                        chunk_fts_params.append(str(session_id))
                    chunk_fts_params.extend(owner_params)
                    chunk_fts_params.append(safe_limit * 6)
                    chunk_fts_rows = conn.execute(
                        chunk_fts_sql, tuple(chunk_fts_params)
                    ).fetchall()
                    for row in chunk_fts_rows:
                        snippet = str(row["snippet"] or "").strip()
                        if not snippet:
                            snippet_source = _pick_snippet_source(
                                str(row["content"] or ""),
                                str(row["search_text"] or ""),
                                raw_query,
                                str(row["chunk_text"] or ""),
                            )
                            snippet = _build_snippet(snippet_source, raw_query)
                        merge_row(
                            row,
                            snippet=snippet,
                            score=float(row["score"] or 0.0),
                            exact_match=False,
                        )

                result = sorted(
                    merged.values(),
                    key=lambda item: (
                        0 if item.get("exact_match") else 1,
                        float(item.get("score", 0.0)),
                        -int(item.get("revision", 0)),
                        -float(item.get("last_active_at", 0)),
                    ),
                )
                return result[:safe_limit]
            finally:
                conn.close()

    @classmethod
    def _session_row_to_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "parent_id": str(row["parent_id"] or ""),
            "title": str(row["title"] or ""),
            "agent_name": str(row["agent_name"] or "general"),
            "status": str(row["status"] or "active"),
            "tenant_id": str(row["tenant_id"] or ""),
            "user_id": str(row["user_id"] or ""),
            "workspace_id": str(row["workspace_id"] or ""),
            "workdir": str(row["workdir"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "display_messages": _json_loads(row["display_messages_json"], []),
            "created_at": float(row["created_at"] or 0),
            "last_active_at": float(row["last_active_at"] or 0),
            "compaction_count": int(row["compaction_count"] or 0),
            "last_compacted_at": row["last_compacted_at"],
            "total_tokens_saved": int(row["total_tokens_saved"] or 0),
            "current_revision": int(row["current_revision"] or 0),
        }

    @classmethod
    def _load_messages_locked(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        revision: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        effective_revision = revision
        if effective_revision is None:
            row = conn.execute(
                "SELECT current_revision FROM sessions WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            effective_revision = int(row["current_revision"] or 0) if row else 0
        rows = conn.execute(
            """
            SELECT message_json
            FROM messages
            WHERE session_id = ? AND session_revision = ?
            ORDER BY message_order ASC
            """,
            (session_id, effective_revision),
        ).fetchall()
        return [
            _json_loads(row["message_json"], {})
            for row in rows
            if row["message_json"] not in (None, "")
        ]
