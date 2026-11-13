"""Bounded SQLite snapshots; audit events lack indexed immutable result lookup."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from ...sqlite_runtime import connect as sqlite_connect

from .contracts import Owner, Policy


class CapsuleStore:
    def __init__(self, workdir: str, owner: Owner, policy: Policy, *, create: bool = True):
        if not workdir:
            raise ValueError("workdir_required")
        self.owner = owner.key()
        self.policy = policy
        root = Path(workdir).resolve(strict=True)
        folder = root / ".tcode" / "context_capsules"
        for path in (root / ".tcode", folder):
            if path.is_symlink():
                raise ValueError("unsafe_capsule_path")
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
        self.path = folder / (hashlib.sha256(self.owner.encode()).hexdigest() + ".sqlite")

    @contextmanager
    def connect(self):
        if self.path.is_symlink() or self.path.parent.resolve() != self.path.parent:
            raise ValueError("unsafe_capsule_path")
        for suffix in ("-journal", "-wal", "-shm"):
            if Path(str(self.path) + suffix).is_symlink():
                raise ValueError("unsafe_capsule_path")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite_connect(self.path, timeout=self.policy.max_latency_ms / 1000)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS capsules (id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL, expires REAL NOT NULL, size INTEGER NOT NULL, UNIQUE(owner,digest,source))")
            db.execute("CREATE TABLE IF NOT EXISTS budgets (turn TEXT PRIMARY KEY, calls INTEGER NOT NULL, expires REAL NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def put(self, content: str, source: dict) -> str:
        raw = content.encode("utf-8")
        if len(raw) > self.policy.max_bytes:
            raise ValueError("source_too_large")
        digest = hashlib.sha256(raw).hexdigest()
        source_json = json.dumps(source, sort_keys=True, ensure_ascii=False)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM capsules WHERE expires <= ?", (now,))
            db.execute("DELETE FROM budgets WHERE expires <= ?", (now,))
            row = db.execute("SELECT id FROM capsules WHERE owner=? AND digest=? AND source=?", (self.owner, digest, source_json)).fetchone()
            if row:
                db.execute("UPDATE capsules SET expires=? WHERE id=?", (now + self.policy.ttl, row[0]))
                return row[0]
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM capsules").fetchone()[0]
            if used + len(raw) > self.policy.quota_bytes:
                raise ValueError("capsule_quota_exceeded")
            capsule_id = "ctx_" + uuid.uuid4().hex
            db.execute("INSERT INTO capsules VALUES (?,?,?,?,?,?,?)", (capsule_id, self.owner, digest, content, source_json, now + self.policy.ttl, len(raw)))
            return capsule_id

    def get(self, capsule_id: str, turn: str, *, with_source: bool = False) -> tuple[str, str] | tuple[str, str, dict]:
        if not re.fullmatch(r"ctx_[0-9a-f]{32}", capsule_id) or not turn:
            raise ValueError("invalid_reference_or_turn")
        if not self.path.exists():
            raise ValueError("source_unavailable")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT content,digest,source FROM capsules WHERE id=? AND owner=? AND expires>?", (capsule_id, self.owner, now)).fetchone()
            if not row:
                raise ValueError("source_unavailable")
            content, digest, source_json = row
            if hashlib.sha256(content.encode()).hexdigest() != digest:
                raise ValueError("source_integrity_error")
            db.execute("DELETE FROM budgets WHERE expires<=?", (now,))
            calls = db.execute("SELECT calls FROM budgets WHERE turn=?", (turn,)).fetchone()
            if calls and calls[0] >= self.policy.max_calls:
                raise ValueError("turn_retrieval_budget_exceeded")
            db.execute("INSERT INTO budgets VALUES (?,1,?) ON CONFLICT(turn) DO UPDATE SET calls=calls+1", (turn, now + self.policy.ttl))
            if with_source:
                source = json.loads(source_json)
                # Return only host provenance fields, never arbitrary source metadata.
                source = {key: source[key] for key in ('tool', 'tool_call_id', 'message_id', 'source_complete') if key in source}
                source['retrieval_calls_remaining'] = self.policy.max_calls - (calls[0] if calls else 0) - 1
                return content, digest, source
            return content, digest

    def load(self, capsule_id: str) -> str:
        """Host-only projection read; never exposed through a tool selector."""
        if not re.fullmatch(r"ctx_[0-9a-f]{32}", capsule_id):
            raise ValueError("invalid_reference")
        if not self.path.exists():
            raise ValueError("source_unavailable")
        with self.connect() as db:
            row = db.execute("SELECT content,digest FROM capsules WHERE id=? AND owner=? AND expires>?", (capsule_id, self.owner, time.time())).fetchone()
            if not row:
                raise ValueError("source_unavailable")
            if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                raise ValueError("source_integrity_error")
            return row[0]

    def purge(self) -> None:
        """Erase this owner's snapshots on session deletion; keep SQLite stable."""
        if not self.path.exists():
            return
        with self.connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM capsules")
            db.execute("DELETE FROM budgets")

    def cleanup(self) -> int:
        with self.connect() as db:
            count = db.execute("DELETE FROM capsules WHERE expires<=?", (time.time(),)).rowcount
            db.execute("DELETE FROM budgets WHERE expires<=?", (time.time(),))
        return count
