"""Versioned, read-only SQLite facts for the enzyme entity plugin."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from itertools import islice
from pathlib import Path
from typing import Any, Iterable


def normalize_term(value: Any) -> str:
    import re
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", text).split())


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _batches(rows: Iterable[dict], size: int = 1000) -> Iterable[list[dict]]:
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch


def build_database(path: Path, entities: Iterable[dict], assertions: Iterable[dict], evidence: Iterable[dict]) -> None:
    """Build a complete database beside the destination, then atomically publish it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".entity-", suffix=".sqlite", dir=path.parent)
    os.close(handle)
    try:
        db = sqlite3.connect(temporary)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE entity(id TEXT PRIMARY KEY, type TEXT NOT NULL, label TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE alias(term TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entity(id), PRIMARY KEY(term, entity_id));
                CREATE INDEX alias_entity ON alias(entity_id);
                CREATE TABLE alias_token(token TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entity(id), PRIMARY KEY(token, entity_id));
                CREATE TABLE assertion(id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entity(id), predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL REFERENCES entity(id), state TEXT NOT NULL, polarity TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX assertion_subject ON assertion(subject_id, predicate, state, polarity);
                CREATE INDEX assertion_object ON assertion(object_id, predicate, state, polarity);
                CREATE TABLE evidence(id TEXT PRIMARY KEY, wiki_ref TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
            """)
            for batch in _batches(entities):
                db.executemany("INSERT INTO entity VALUES (?,?,?,?)", ((r["entity_id"], r["entity_type"], r["label"], json.dumps(r, ensure_ascii=False)) for r in batch))
                aliases = set()
                for row in batch:
                    terms = [row["entity_id"], row["label"], *(row.get("aliases") or []), *((row.get("external_ids") or {}).values())]
                    aliases.update((normal, row["entity_id"]) for term in terms if (normal := normalize_term(term)))
                db.executemany("INSERT OR IGNORE INTO alias VALUES (?,?)", sorted(aliases))
                db.executemany("INSERT OR IGNORE INTO alias_token VALUES (?,?)", sorted({(token, entity_id) for term, entity_id in aliases for token in term.split()}))
            for batch in _batches(assertions):
                db.executemany("INSERT INTO assertion VALUES (?,?,?,?,?,?,?)", ((r["assertion_id"], r["subject_id"], r["predicate"], r["object_id"], r["state"], r["polarity"], json.dumps(r, ensure_ascii=False)) for r in batch))
            for batch in _batches(evidence):
                db.executemany("INSERT INTO evidence VALUES (?,?,?)", ((r["evidence_id"], r["wiki_ref"], json.dumps(r, ensure_ascii=False)) for r in batch))
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("entity database contains broken references")
            db.commit()
        finally:
            db.close()
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
