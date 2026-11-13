"""Indexed read-only ontology facts; no workspace copy or full-graph preload."""

from __future__ import annotations

from collections import ChainMap
import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .entity_validation import BundleValidationError, validate_packaged_bundle
from .sqlite_bundle import normalize_term


class _Rows(Mapping[str, dict[str, Any]]):
    def __init__(self, db: sqlite3.Connection, table: str) -> None:
        self.db, self.table = db, table

    def __getitem__(self, key: str) -> dict[str, Any]:
        row = self.db.execute(f"SELECT payload FROM {self.table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __iter__(self) -> Iterator[str]:
        for row in self.db.execute(f"SELECT id FROM {self.table} ORDER BY id"):
            yield row[0]

    def __len__(self) -> int:
        return self.db.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.db.execute(f"SELECT 1 FROM {self.table} WHERE id=?", (key,)).fetchone() is not None


class _Aliases(Mapping[str, set[str]]):
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __getitem__(self, key: str) -> set[str]:
        result = {r[0] for r in self.db.execute("SELECT entity_id FROM alias WHERE term=?", (key,))}
        if not result:
            raise KeyError(key)
        return result

    def __iter__(self) -> Iterator[str]:
        for row in self.db.execute("SELECT DISTINCT term FROM alias ORDER BY term"):
            yield row[0]

    def __len__(self) -> int:
        return self.db.execute("SELECT count(DISTINCT term) FROM alias").fetchone()[0]


class _Edges(Mapping[str, tuple[dict[str, Any], ...]]):
    def __init__(self, db: sqlite3.Connection, column: str) -> None:
        self.db, self.column = db, column

    def __getitem__(self, key: str) -> tuple[dict[str, Any], ...]:
        rows = tuple(json.loads(r[0]) for r in self.db.execute(
            f"SELECT payload FROM assertion WHERE {self.column}=? ORDER BY id LIMIT 5001", (key,)
        ))
        if len(rows) > 5000:
            raise ValueError(f"entity {key} has more than 5000 relations; narrow the query")
        if not rows:
            raise KeyError(key)
        return rows

    def __iter__(self) -> Iterator[str]:
        for row in self.db.execute(f"SELECT DISTINCT {self.column} FROM assertion ORDER BY {self.column}"):
            yield row[0]

    def __len__(self) -> int:
        return self.db.execute(f"SELECT count(DISTINCT {self.column}) FROM assertion").fetchone()[0]


class _WikiRefs(Mapping[str, str]):
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __getitem__(self, key: str) -> str:
        row = self.db.execute("SELECT id FROM evidence WHERE wiki_ref=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    def __iter__(self) -> Iterator[str]:
        for row in self.db.execute("SELECT wiki_ref FROM evidence ORDER BY wiki_ref"):
            yield row[0]

    def __len__(self) -> int:
        return self.db.execute("SELECT count(*) FROM evidence").fetchone()[0]


class EntityStore:
    def __init__(self, workspace: Any, plugin_root: str | Path, *, workspace_dir: str,
                 expected_bundle_digest: str | None = None, knowledge_format: str = "sqlite") -> None:
        if knowledge_format != "sqlite":
            raise ValueError("knowledge_format must be sqlite")
        self.workspace = workspace
        self.plugin_root = Path(plugin_root).resolve()
        self.workspace_dir = workspace_dir
        self.expected_bundle_digest = str(expected_bundle_digest or "")
        self.knowledge_format = knowledge_format
        self.manifest: dict[str, Any] | None = None
        self.snapshot_root = ""
        self.db: sqlite3.Connection | None = None
        self.entities: Mapping[str, dict[str, Any]] = {}
        self.assertions: Mapping[str, dict[str, Any]] = {}
        self.evidence: Mapping[str, dict[str, Any]] = {}
        self.alias_index: Mapping[str, set[str]] = {}
        self.outgoing: Mapping[str, tuple[dict[str, Any], ...]] = {}
        self.incoming: Mapping[str, tuple[dict[str, Any], ...]] = {}
        self.evidence_by_wiki_ref: Mapping[str, str] = {}
        self.predicates: dict[str, dict[str, Any]] = {}

    @property
    def bundle_digest(self) -> str:
        if self.manifest is None:
            raise RuntimeError("entity store is not loaded")
        return str(self.manifest["bundle_digest"])

    def load(self) -> dict[str, Any]:
        if self.db is not None:
            return self.summary()
        manifest, _ = validate_packaged_bundle(self.workspace, self.plugin_root)
        if self.expected_bundle_digest and manifest["bundle_digest"] != self.expected_bundle_digest:
            raise BundleValidationError("configured expected_bundle_digest does not match the packaged bundle")
        database_relative = next(entry["path"] for entry in manifest["files"] if entry["path"].startswith("public/releases/") and entry["path"].endswith("/entities.sqlite"))
        path = Path(self.workspace.resolve_read_path(str(self.plugin_root / "knowledge" / database_relative)))
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
        try:
            db.execute("PRAGMA query_only=ON")
            for table, expected in (("entity", "entities"), ("assertion", "assertions"), ("evidence", "evidence")):
                actual = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if actual != manifest["statistics"][expected]:
                    raise BundleValidationError(f"{table} count mismatch")
            ontology = json.loads(self.workspace.read_text(str(self.plugin_root / "knowledge" / manifest.get("ontology_path", "ontology.json"))))
            self.predicates = {row["id"]: row for row in ontology["predicates"]}
        except BaseException:
            db.close()
            raise
        self.db = db
        self.manifest = manifest
        self.snapshot_root = str(path)
        self.entities = _Rows(db, "entity")
        self.assertions = _Rows(db, "assertion")
        self.evidence = _Rows(db, "evidence")
        self.alias_index = _Aliases(db)
        self.outgoing = _Edges(db, "subject_id")
        self.incoming = _Edges(db, "object_id")
        self.evidence_by_wiki_ref = _WikiRefs(db)
        return self.summary()

    def attach_private_facts(self, facts):
        # Per-call overlay only: never mutate the public database or shared mappings.
        self.entities = ChainMap(self.entities, facts['entities'])
        self.assertions = ChainMap(self.assertions, facts['assertions'])
        self.evidence = ChainMap(self.evidence, facts['evidence'])

    def summary(self) -> dict[str, Any]:
        if self.manifest is None:
            raise RuntimeError("entity store is not loaded")
        return {"bundle_digest": self.bundle_digest, "bundle_version": self.manifest["bundle_version"],
                **self.manifest["statistics"], "knowledge_format": "sqlite", "snapshot_root": self.snapshot_root}

    def entity_summary(self, entity_id: str) -> dict[str, Any]:
        entity = self.entities[entity_id]
        return {"entity_id": entity["entity_id"], "entity_type": entity["entity_type"],
                "label": entity["label"], "aliases": list(entity.get("aliases") or []),
                "identity_scope": entity["identity_scope"], "external_ids": dict(entity.get("external_ids") or {}),
                "organism": entity.get("organism"), "source_evidence_ids": list(entity.get("source_evidence_ids") or [])}

    def entity_types(self) -> set[str]:
        assert self.db is not None
        return {r[0] for r in self.db.execute("SELECT DISTINCT type FROM entity")}

    def candidate_reactions(self, feature_ids: list[str], predicates: list[str]) -> list[str]:
        assert self.db is not None
        if not feature_ids:
            return []
        placeholders = ",".join("?" for _ in feature_ids)
        kinds = ",".join("?" for _ in predicates)
        return [r[0] for r in self.db.execute(
            f"SELECT DISTINCT subject_id FROM assertion WHERE object_id IN ({placeholders}) AND predicate IN ({kinds}) AND state='active' AND polarity='supports' ORDER BY subject_id LIMIT 1001",
            (*feature_ids, *predicates),
        )]

    def fragment_reactions(self, query: dict[str, list[str]]) -> list[str]:
        direct = self.candidate_reactions(
            [*query["participant"], *query["transformation"], *query["capability"], *query["intermediate"]],
            ["has_participant", "exhibits_transformation", "requires_capability", "has_intermediate"],
        )
        found = set(direct)
        assert self.db is not None
        for cofactor in query["cofactor"]:
            for (reaction_id,) in self.db.execute(
                "SELECT DISTINCT c.object_id FROM assertion u JOIN assertion c ON c.subject_id=u.subject_id "
                "WHERE u.object_id=? AND u.predicate='uses_cofactor' AND c.predicate='catalyzes_reported' "
                "AND u.state='active' AND c.state='active' AND u.polarity='supports' AND c.polarity='supports' LIMIT 1001", (cofactor,)
            ):
                found.add(reaction_id)
        for term in query["term"]:
            ids = self.suggest_ids(term, 200)
            found.update(self.candidate_reactions(ids, ["has_participant", "exhibits_transformation", "requires_capability", "has_intermediate"]))
            for item in ids:
                if self.entities[item]["entity_type"] == "Reaction":
                    found.add(item)
                for (reaction_id,) in self.db.execute(
                    "SELECT object_id FROM assertion WHERE subject_id=? AND predicate='catalyzes_reported' AND state='active' AND polarity='supports' LIMIT 1001", (item,)
                ):
                    found.add(reaction_id)
        if len(found) > 1000:
            raise ValueError("mechanism fragment search has too many reactions; narrow the query")
        return sorted(found)

    def suggest_ids(self, query: str, limit: int = 500) -> list[str]:
        assert self.db is not None
        tokens = normalize_term(query).split()
        if not tokens:
            return []
        placeholders = ",".join("?" for _ in tokens)
        return [r[0] for r in self.db.execute(
            f"SELECT entity_id FROM alias_token WHERE token IN ({placeholders}) GROUP BY entity_id ORDER BY count(*) DESC, entity_id LIMIT ?",
            (*tokens, limit),
        )]

    def active_assertions(self, entity_id: str, *, direction: str) -> tuple[dict[str, Any], ...]:
        rows = {}
        if direction in {"outgoing", "both"}:
            rows.update((r["assertion_id"], r) for r in self.outgoing.get(entity_id, ()))
        if direction in {"incoming", "both"}:
            rows.update((r["assertion_id"], r) for r in self.incoming.get(entity_id, ()))
        return tuple(r for _, r in sorted(rows.items()) if r.get("state") == "active")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
        self.db = None
        self.manifest = None
