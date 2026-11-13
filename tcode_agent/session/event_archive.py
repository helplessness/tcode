"""Search index and verified reader for compressed session event logs."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional


class EventArchiveStore:
    @classmethod
    def _root(cls, workdir: Optional[str] = None) -> Path:
        root = Path(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()) / ".tcode" / "session_events"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _connect(cls, workdir: Optional[str] = None) -> sqlite3.Connection:
        connection = sqlite3.connect(cls._root(workdir) / "archive_index.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, archive_path TEXT NOT NULL, line_number INTEGER NOT NULL,
                session_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
                created_at REAL NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '', workspace_id TEXT NOT NULL DEFAULT '',
                checksum TEXT NOT NULL, search_text TEXT NOT NULL,
                UNIQUE(archive_path, line_number)
            );
            CREATE INDEX IF NOT EXISTS events_session_time ON events(session_id, created_at DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
                USING fts5(search_text, content='events', content_rowid='id');
            """
        )
        return connection

    @staticmethod
    def _checksum(event: dict[str, Any]) -> str:
        payload = {key: value for key, value in event.items() if key != "checksum"}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _relative(cls, path: Path, workdir: Optional[str]) -> str:
        return path.resolve().relative_to(cls._root(workdir).resolve()).as_posix()

    @classmethod
    def index(cls, path: Path, *, workdir: Optional[str] = None) -> int:
        relative = cls._relative(path, workdir)
        rows = []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("checksum") != cls._checksum(event):
                    raise RuntimeError("session event archive checksum mismatch")
                owner = event.get("owner") if isinstance(event.get("owner"), dict) else {}
                searchable = json.dumps(
                    {key: event.get(key) for key in ("event_type", "causation_id", "idempotency_key", "payload", "metadata")},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                rows.append((
                    relative, line_number, str(event.get("session_id") or ""),
                    int(event.get("sequence", 0) or 0), str(event.get("event_type") or "unknown"),
                    float(event.get("created_at", 0) or 0), str(owner.get("tenant_id") or ""),
                    str(owner.get("user_id") or ""), str(owner.get("workspace_id") or ""),
                    str(event.get("checksum") or ""), searchable,
                ))
        connection = cls._connect(workdir)
        try:
            old = connection.execute("SELECT id FROM events WHERE archive_path = ?", (relative,)).fetchall()
            connection.executemany("DELETE FROM events_fts WHERE rowid = ?", [(int(row["id"]),) for row in old])
            connection.execute("DELETE FROM events WHERE archive_path = ?", (relative,))
            for row in rows:
                cursor = connection.execute(
                    """INSERT INTO events(archive_path,line_number,session_id,sequence,event_type,
                       created_at,tenant_id,user_id,workspace_id,checksum,search_text)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""", row,
                )
                connection.execute("INSERT INTO events_fts(rowid,search_text) VALUES (?,?)", (cursor.lastrowid, row[-1]))
            connection.commit()
        finally:
            connection.close()
        return len(rows)

    @classmethod
    def remove(cls, path: Path, *, workdir: Optional[str] = None) -> None:
        index_path = cls._root(workdir) / "archive_index.sqlite3"
        if not index_path.exists():
            return
        relative = cls._relative(path, workdir)
        connection = cls._connect(workdir)
        try:
            ids = connection.execute("SELECT id FROM events WHERE archive_path = ?", (relative,)).fetchall()
            connection.executemany("DELETE FROM events_fts WHERE rowid = ?", [(int(row["id"]),) for row in ids])
            connection.execute("DELETE FROM events WHERE archive_path = ?", (relative,))
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def rebuild(cls, *, workdir: Optional[str] = None) -> dict[str, int]:
        archives = sorted((cls._root(workdir) / "archive").glob("*/*.jsonl.gz"))
        count = sum(cls.index(path, workdir=workdir) for path in archives)
        return {"indexed_archive_count": len(archives), "indexed_event_count": count}

    @classmethod
    def status(cls, *, workdir: Optional[str] = None) -> dict[str, int]:
        paths = {cls._relative(path, workdir) for path in (cls._root(workdir) / "archive").glob("*/*.jsonl.gz")}
        indexed = set()
        if (cls._root(workdir) / "archive_index.sqlite3").exists():
            connection = cls._connect(workdir)
            try:
                indexed = {str(row[0]) for row in connection.execute("SELECT DISTINCT archive_path FROM events")}
            finally:
                connection.close()
        return {"archive_count": len(paths), "indexed_archive_count": len(paths & indexed), "unindexed_archive_count": len(paths - indexed)}

    @classmethod
    def search(cls, session_id: str, *, query: str = "", event_type: str = "", owner: Optional[dict[str, Any]] = None,
               created_from: Optional[float] = None, created_to: Optional[float] = None, limit: int = 50,
               workdir: Optional[str] = None) -> list[dict[str, Any]]:
        if not (cls._root(workdir) / "archive_index.sqlite3").exists():
            return []
        clauses, params, join = ["e.session_id = ?"], [session_id], ""
        query = str(query or "").strip()
        if query:
            join, clauses = " JOIN events_fts f ON f.rowid=e.id", clauses + ["f.search_text MATCH ?"]
            params.append('"' + query.replace('"', '""') + '"')
        if event_type:
            clauses.append("e.event_type = ?"); params.append(event_type)
        if created_from is not None:
            clauses.append("e.created_at >= ?"); params.append(float(created_from))
        if created_to is not None:
            clauses.append("e.created_at <= ?"); params.append(float(created_to))
        for field in ("tenant_id", "user_id", "workspace_id"):
            value = str((owner or {}).get(field) or "").strip()
            if value:
                clauses.append(f"e.{field} = ?"); params.append(value)
        params.append(min(200, max(1, int(limit))))
        connection = cls._connect(workdir)
        try:
            rows = connection.execute(
                f"SELECT e.* FROM events e{join} WHERE {' AND '.join(clauses)} ORDER BY e.created_at DESC,e.sequence DESC LIMIT ?", params
            ).fetchall()
        finally:
            connection.close()
        return [{"archive_event_id": int(row["id"]), "session_id": row["session_id"], "sequence": int(row["sequence"]),
                 "event_type": row["event_type"], "created_at": float(row["created_at"]), "snippet": str(row["search_text"])[:240]}
                for row in rows]

    @classmethod
    def read(cls, archive_event_id: int, *, session_id: str, owner: Optional[dict[str, Any]] = None,
             workdir: Optional[str] = None) -> Optional[dict[str, Any]]:
        if not (cls._root(workdir) / "archive_index.sqlite3").exists():
            return None
        clauses, params = ["id = ?", "session_id = ?"], [int(archive_event_id), session_id]
        for field in ("tenant_id", "user_id", "workspace_id"):
            value = str((owner or {}).get(field) or "").strip()
            if value:
                clauses.append(f"{field} = ?"); params.append(value)
        connection = cls._connect(workdir)
        try:
            row = connection.execute(f"SELECT * FROM events WHERE {' AND '.join(clauses)}", params).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        root = cls._root(workdir).resolve(); path = (root / str(row["archive_path"])).resolve()
        if root not in path.parents:
            raise RuntimeError("invalid session event archive path")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            event = json.loads(next(line for number, line in enumerate(handle, 1) if number == int(row["line_number"])))
        if event.get("checksum") != row["checksum"] or event.get("checksum") != cls._checksum(event):
            raise RuntimeError("session event archive checksum mismatch")
        return event

    @classmethod
    def original_session_messages(
        cls,
        session_id: str,
        *,
        owner: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Recover the transcript immediately before the first physical compaction."""

        index_path = cls._root(workdir) / "archive_index.sqlite3"
        if not index_path.exists():
            return []
        clauses, params = ["session_id = ?"], [session_id]
        for field in ("tenant_id", "user_id", "workspace_id"):
            value = str((owner or {}).get(field) or "").strip()
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        connection = cls._connect(workdir)
        try:
            row = connection.execute(
                f"""
                SELECT archive_path, MIN(created_at) AS first_event_at
                FROM events
                WHERE {' AND '.join(clauses)}
                GROUP BY archive_path
                ORDER BY first_event_at ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return []

        root = cls._root(workdir).resolve()
        path = (root / str(row["archive_path"])).resolve()
        if root not in path.parents:
            raise RuntimeError("invalid session event archive path")

        messages: list[dict[str, Any]] = []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("checksum") != cls._checksum(event):
                    raise RuntimeError("session event archive checksum mismatch")
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                event_type = str(event.get("event_type") or "")
                if event_type == "session.snapshot":
                    snapshot = payload.get("session")
                    if isinstance(snapshot, dict):
                        messages = list(snapshot.get("messages") or [])
                elif event_type == "session.baseline":
                    projection = payload.get("projection")
                    snapshot = projection.get("session") if isinstance(projection, dict) else None
                    if isinstance(snapshot, dict):
                        messages = list(snapshot.get("messages") or [])
                elif event_type == "session.patch" and "messages_from" in payload:
                    start = max(0, int(payload.get("messages_from", 0) or 0))
                    tail = payload.get("messages")
                    if isinstance(tail, list):
                        messages = [*messages[:start], *tail]
        return [message for message in messages if isinstance(message, dict)]
