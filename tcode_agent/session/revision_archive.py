"""Compressed, searchable cold storage for pruned session revisions."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class RevisionArchiveStore:
    @classmethod
    def _root(cls, workdir: Optional[str] = None) -> Path:
        root = Path(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()) / ".tcode" / "session_revisions"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _connect(cls, workdir: Optional[str] = None) -> sqlite3.Connection:
        connection = sqlite3.connect(cls._root(workdir) / "index.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT '',
                archived_at REAL NOT NULL,
                message_count INTEGER NOT NULL,
                archive_path TEXT NOT NULL,
                checksum TEXT NOT NULL,
                search_text TEXT NOT NULL,
                UNIQUE(session_id, revision)
            );
            CREATE INDEX IF NOT EXISTS revisions_session ON revisions(session_id, revision DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS revisions_fts
                USING fts5(search_text, content='revisions', content_rowid='id');
            """
        )
        return connection

    @staticmethod
    def _safe(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:180] or "unknown"

    @classmethod
    def archive(
        cls,
        *,
        session: dict[str, Any],
        revision: int,
        messages: list[dict[str, Any]],
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        session_id = str(session.get("id") or "")
        payload = {
            "schema_version": 1,
            "session": session,
            "revision": int(revision),
            "messages": messages,
            "archived_at": time.time(),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        folder = cls._root(workdir) / "archive" / cls._safe(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"revision-{int(revision)}.json.gz"
        temporary = path.with_suffix(".gz.tmp")
        temporary.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))
        if hashlib.sha256(gzip.decompress(temporary.read_bytes())).hexdigest() != checksum:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("session revision archive verification failed")
        os.replace(temporary, path)
        relative_path = path.relative_to(cls._root(workdir)).as_posix()
        search_text = "\n".join(
            str(message.get("search_text") or message.get("content") or message.get("message_json") or "")
            for message in messages
        )
        connection = cls._connect(workdir)
        try:
            old = connection.execute(
                "SELECT id FROM revisions WHERE session_id = ? AND revision = ?",
                (session_id, int(revision)),
            ).fetchone()
            if old:
                connection.execute("DELETE FROM revisions_fts WHERE rowid = ?", (int(old["id"]),))
            connection.execute(
                """INSERT INTO revisions(
                       session_id, revision, tenant_id, user_id, workspace_id,
                       archived_at, message_count, archive_path, checksum, search_text
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, revision) DO UPDATE SET
                       tenant_id=excluded.tenant_id, user_id=excluded.user_id,
                       workspace_id=excluded.workspace_id, archived_at=excluded.archived_at,
                       message_count=excluded.message_count, archive_path=excluded.archive_path,
                       checksum=excluded.checksum, search_text=excluded.search_text""",
                (
                    session_id,
                    int(revision),
                    str(session.get("tenant_id") or ""),
                    str(session.get("user_id") or ""),
                    str(session.get("workspace_id") or ""),
                    float(payload["archived_at"]),
                    len(messages),
                    relative_path,
                    checksum,
                    search_text,
                ),
            )
            row_id = int(connection.execute(
                "SELECT id FROM revisions WHERE session_id = ? AND revision = ?",
                (session_id, int(revision)),
            ).fetchone()["id"])
            connection.execute(
                "INSERT INTO revisions_fts(rowid, search_text) VALUES (?, ?)",
                (row_id, search_text),
            )
            connection.commit()
        finally:
            connection.close()
        return {"revision": int(revision), "message_count": len(messages), "archive_path": relative_path}

    @classmethod
    def search(
        cls,
        session_id: str,
        *,
        query: str = "",
        owner: Optional[dict[str, Any]] = None,
        limit: Optional[int] = 50,
        workdir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not (cls._root(workdir) / "index.sqlite3").exists():
            return []
        clauses = ["r.session_id = ?"]
        parameters: list[Any] = [str(session_id)]
        join = ""
        query = str(query or "").strip()
        if query:
            join = " JOIN revisions_fts f ON f.rowid = r.id"
            clauses.append("f.search_text MATCH ?")
            parameters.append('"' + query.replace('"', '""') + '"')
        for field in ("tenant_id", "user_id", "workspace_id"):
            value = str((owner or {}).get(field) or "").strip()
            if value:
                clauses.append(f"r.{field} = ?")
                parameters.append(value)
        if limit is not None:
            parameters.append(min(200, max(1, int(limit))))
        connection = cls._connect(workdir)
        try:
            rows = connection.execute(
                f"""SELECT r.id, r.session_id, r.revision, r.archived_at,
                           r.message_count, r.archive_path, r.search_text
                    FROM revisions r{join} WHERE {' AND '.join(clauses)}
                    ORDER BY r.revision DESC {"LIMIT ?" if limit is not None else ""}""",
                parameters,
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "archive_revision_id": int(row["id"]),
                "session_id": str(row["session_id"]),
                "revision": int(row["revision"]),
                "archived_at": float(row["archived_at"]),
                "message_count": int(row["message_count"]),
                "archive_path": str(row["archive_path"]),
                "snippet": str(row["search_text"])[:240],
            }
            for row in rows
        ]

    @classmethod
    def read(
        cls,
        *,
        session_id: str,
        revision: int,
        owner: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not (cls._root(workdir) / "index.sqlite3").exists():
            return None
        clauses = ["session_id = ?", "revision = ?"]
        parameters: list[Any] = [str(session_id), int(revision)]
        for field in ("tenant_id", "user_id", "workspace_id"):
            value = str((owner or {}).get(field) or "").strip()
            if value:
                clauses.append(f"{field} = ?")
                parameters.append(value)
        connection = cls._connect(workdir)
        try:
            row = connection.execute(
                f"SELECT * FROM revisions WHERE {' AND '.join(clauses)}", parameters
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        root = cls._root(workdir).resolve()
        path = (root / str(row["archive_path"])).resolve()
        if root not in path.parents:
            raise RuntimeError("invalid session revision archive path")
        raw = gzip.decompress(path.read_bytes())
        if hashlib.sha256(raw).hexdigest() != str(row["checksum"]):
            raise RuntimeError("session revision archive checksum mismatch")
        payload = json.loads(raw)
        if str(payload.get("session", {}).get("id") or "") != str(session_id) or int(payload.get("revision", 0)) != int(revision):
            raise RuntimeError("session revision archive identity mismatch")
        return payload

    @classmethod
    def cleanup_expired(
        cls,
        *,
        ttl_days: float,
        apply: bool = False,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """预览或清理超过冷存储 TTL 的 revision；默认不执行删除。"""

        cutoff = time.time() - max(0.0, float(ttl_days)) * 86400
        index_path = cls._root(workdir) / "index.sqlite3"
        if not index_path.exists():
            return {"apply": bool(apply), "ttl_days": ttl_days, "candidate_count": 0, "candidate_bytes": 0}
        connection = cls._connect(workdir)
        try:
            rows = connection.execute(
                "SELECT id, archive_path FROM revisions WHERE archived_at < ?", (cutoff,)
            ).fetchall()
            paths = [(cls._root(workdir) / str(row["archive_path"]), int(row["id"])) for row in rows]
            report = {
                "apply": bool(apply),
                "ttl_days": ttl_days,
                "candidate_count": len(paths),
                "candidate_bytes": sum(path.stat().st_size for path, _ in paths if path.exists()),
            }
            if apply:
                connection.executemany("DELETE FROM revisions_fts WHERE rowid = ?", [(row_id,) for _, row_id in paths])
                connection.executemany("DELETE FROM revisions WHERE id = ?", [(row_id,) for _, row_id in paths])
                connection.commit()
                for path, _ in paths:
                    path.unlink(missing_ok=True)
            return report
        finally:
            connection.close()
