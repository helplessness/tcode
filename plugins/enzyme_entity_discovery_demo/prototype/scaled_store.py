"""Synthetic, disk-indexed ontology prototype. No biological facts are generated."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path


SCHEMA_VERSION = 1
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _version(value: str) -> str:
    if not _VERSION.fullmatch(value):
        raise ValueError("version must be a simple release name")
    return value


def shard_number(reaction_id: str, shards: int) -> int:
    return int(hashlib.sha256(reaction_id.encode()).hexdigest()[:8], 16) % shards


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection = sqlite3.connect(path)
    return connection


def _peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def _manifest(release_dir: Path, *, version: str, proteins: int, reactions: int, edges: int, shards: int, documents: int, curated_entities: int, curated_assertions: int) -> dict:
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(release_dir.glob("*.sqlite"))
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "synthetic": True,
        "proteins": proteins,
        "reactions": reactions,
        "assertions": edges,
        "document_metadata": documents,
        "curated_entities": curated_entities,
        "curated_assertions": curated_assertions,
        "shards": shards,
        "files": files,
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _publish(root: Path, staging: Path, version: str) -> None:
    target = root / "releases" / version
    if target.exists():
        raise FileExistsError(target)
    staging.rename(target)
    pointer = root / "active.json.tmp"
    pointer.write_text(json.dumps({"version": version}) + "\n")
    os.replace(pointer, root / "active.json")


def build(root: Path, *, version: str, proteins: int, reactions: int, edges: int, shards: int, documents: int = 30_000, curated_bundle: Path | None = None) -> dict:
    version = _version(version)
    if min(proteins, reactions, edges, shards) < 1 or documents < 0 or edges < reactions:
        raise ValueError("counts must be positive and edges >= reactions")
    root = root.resolve()
    (root / "releases").mkdir(parents=True, exist_ok=True)
    staging = root / "releases" / f".building-{version}"
    if staging.exists() or (root / "releases" / version).exists():
        raise FileExistsError(version)
    staging.mkdir()
    started = time.perf_counter()
    try:
        catalog = _connect(staging / "catalog.sqlite")
        curated_entities = curated_assertions = 0
        try:
            catalog.executescript("""
                CREATE TABLE entity(entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, label TEXT NOT NULL);
                CREATE VIRTUAL TABLE entity_fts USING fts5(entity_id UNINDEXED, label);
                CREATE TABLE document(document_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, local_path TEXT NOT NULL, extraction_status TEXT NOT NULL);
                CREATE TABLE curated_assertion(assertion_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, predicate TEXT NOT NULL, object_id TEXT NOT NULL, record_json TEXT NOT NULL);
                CREATE INDEX curated_by_object ON curated_assertion(object_id, predicate);
                CREATE TABLE curated_evidence(evidence_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
            """)
            for start in range(0, proteins, 5000):
                rows = [(f"P:{i:08d}", "Protein", f"Synthetic protein {i}") for i in range(start, min(start + 5000, proteins))]
                catalog.executemany("INSERT INTO entity VALUES (?,?,?)", rows)
                catalog.executemany("INSERT INTO entity_fts VALUES (?,?)", ((row[0], row[2]) for row in rows))
            for start in range(0, reactions, 5000):
                rows = [(f"R:{i:08d}", "Reaction", f"Synthetic reaction {i}") for i in range(start, min(start + 5000, reactions))]
                catalog.executemany("INSERT INTO entity VALUES (?,?,?)", rows)
                catalog.executemany("INSERT INTO entity_fts VALUES (?,?)", ((row[0], row[2]) for row in rows))
            for start in range(0, documents, 5000):
                catalog.executemany(
                    "INSERT INTO document VALUES (?,?,?,?)",
                    ((f"PDF:{i:08d}", hashlib.sha256(f"synthetic-pdf-{i}".encode()).hexdigest(), f"/synthetic/pdfs/{i:08d}.pdf", "not_extracted") for i in range(start, min(start + 5000, documents))),
                )
            if curated_bundle is not None:
                source = sqlite3.connect(curated_bundle)
                try:
                    for (payload,) in source.execute("SELECT payload FROM entity"):
                        row = json.loads(payload)
                        catalog.execute("INSERT INTO entity VALUES (?,?,?)", (row["entity_id"], row["entity_type"], row["label"]))
                        catalog.execute("INSERT INTO entity_fts VALUES (?,?)", (row["entity_id"], row["label"]))
                        curated_entities += 1
                    for (payload,) in source.execute("SELECT payload FROM assertion"):
                        row = json.loads(payload)
                        catalog.execute("INSERT INTO curated_assertion VALUES (?,?,?,?,?)", (row["assertion_id"], row["subject_id"], row["predicate"], row["object_id"], payload))
                        curated_assertions += 1
                    for (payload,) in source.execute("SELECT payload FROM evidence"):
                        row = json.loads(payload)
                        catalog.execute("INSERT INTO curated_evidence VALUES (?,?)", (row["evidence_id"], payload))
                finally:
                    source.close()
            catalog.commit()
        finally:
            catalog.close()

        connections = [_connect(staging / f"reaction-{i:03d}.sqlite") for i in range(shards)]
        try:
            for connection in connections:
                connection.executescript("""
                    CREATE TABLE assertion(
                        assertion_id TEXT PRIMARY KEY,
                        reaction_id TEXT NOT NULL,
                        protein_id TEXT NOT NULL,
                        predicate TEXT NOT NULL,
                        review_status TEXT NOT NULL,
                        epistemic_kind TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        source_ref TEXT NOT NULL
                    );
                    CREATE INDEX assertion_by_reaction ON assertion(reaction_id, review_status, assertion_id);
                    CREATE INDEX assertion_by_protein ON assertion(protein_id, reaction_id);
                    CREATE TABLE evidence(
                        evidence_id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        locator TEXT NOT NULL
                    );
                """)
            base, extra = divmod(edges, reactions)
            for reaction in range(reactions):
                reaction_id = f"R:{reaction:08d}"
                count = base + (reaction < extra)
                rows = [
                    (
                        f"A:{reaction:08d}:{j:06d}", reaction_id,
                        f"P:{(reaction * 7919 + j * 104729) % proteins:08d}",
                        "catalyzes_reported", "accepted" if j % 5 else "unreviewed",
                        "reported", f"E:{reaction:08d}:{j:06d}", "SYNTHETIC:benchmark",
                    )
                    for j in range(count)
                ]
                connection = connections[shard_number(reaction_id, shards)]
                connection.executemany(
                    "INSERT INTO assertion VALUES (?,?,?,?,?,?,?,?)", rows
                )
                connection.executemany(
                    "INSERT INTO evidence VALUES (?,?,?,?)",
                    ((row[6], "synthetic", row[7], row[0]) for row in rows),
                )
            for connection in connections:
                connection.commit()
        finally:
            for connection in connections:
                connection.close()
        manifest = _manifest(staging, version=version, proteins=proteins, reactions=reactions, edges=edges, shards=shards, documents=documents, curated_entities=curated_entities, curated_assertions=curated_assertions)
        _publish(root, staging, version)
        return {"build_seconds": round(time.perf_counter() - started, 3), "peak_rss_bytes": _peak_rss_bytes(), **manifest}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def active_release(root: Path, version: str | None = None) -> tuple[Path, dict]:
    root = root.resolve()
    version = _version(version or json.loads((root / "active.json").read_text())["version"])
    release = root / "releases" / version
    manifest = json.loads((release / "manifest.json").read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION or not manifest.get("synthetic") or manifest.get("version") != version:
        raise ValueError("unsupported or non-synthetic release")
    return release, manifest


def verify(root: Path) -> dict:
    release, manifest = active_release(root)
    for filename, expected in manifest["files"].items():
        path = release / filename
        if not path.is_file() or path.stat().st_size != expected["bytes"] or _sha256(path) != expected["sha256"]:
            raise ValueError(f"release file mismatch: {filename}")
    return {"version": manifest["version"], "files_verified": len(manifest["files"])}


def _resolve_in_release(release: Path, entity_id: str) -> dict | None:
    connection = _connect(release / "catalog.sqlite", readonly=True)
    try:
        row = connection.execute("SELECT entity_id, entity_type, label FROM entity WHERE entity_id=?", (entity_id,)).fetchone()
        return dict(zip(("entity_id", "entity_type", "label"), row)) if row else None
    finally:
        connection.close()


def resolve(root: Path, entity_id: str, *, version: str | None = None) -> dict | None:
    release, _ = active_release(root, version)
    return _resolve_in_release(release, entity_id)


def search(root: Path, text: str, *, limit: int = 20, version: str | None = None) -> list[dict]:
    if not text.strip() or not 1 <= limit <= 100:
        raise ValueError("provide a search term and limit between 1 and 100")
    release, _ = active_release(root, version)
    connection = _connect(release / "catalog.sqlite", readonly=True)
    try:
        phrase = '"' + text.strip().replace('"', '""') + '"'
        rows = connection.execute(
            """SELECT e.entity_id, e.entity_type, e.label
            FROM entity_fts f JOIN entity e ON e.entity_id=f.entity_id
            WHERE entity_fts MATCH ? LIMIT ?""",
            (phrase, limit),
        ).fetchall()
        return [dict(zip(("entity_id", "entity_type", "label"), row)) for row in rows]
    finally:
        connection.close()


def query_curated(root: Path, reaction_id: str, *, limit: int = 20, version: str | None = None) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    release, manifest = active_release(root, version)
    connection = _connect(release / "catalog.sqlite", readonly=True)
    try:
        rows = connection.execute(
            """SELECT record_json FROM curated_assertion
            WHERE object_id=? AND predicate='catalyzes_reported'
            ORDER BY assertion_id LIMIT ?""",
            (reaction_id, limit),
        ).fetchall()
        assertions = [json.loads(row[0]) for row in rows]
        evidence = {}
        for assertion in assertions:
            for evidence_id in assertion.get("evidence_ids", []):
                row = connection.execute("SELECT record_json FROM curated_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                if row:
                    evidence[evidence_id] = json.loads(row[0])
        return {"version": manifest["version"], "reaction_id": reaction_id, "assertions": assertions, "evidence": evidence, "synthetic": False}
    finally:
        connection.close()


def query(root: Path, reaction_id: str, *, workspace: Path | None = None, limit: int = 20, version: str | None = None) -> dict:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    release, manifest = active_release(root, version)
    if _resolve_in_release(release, reaction_id) is None:
        raise ValueError(f"unknown reaction: {reaction_id}")
    shard = release / f"reaction-{shard_number(reaction_id, manifest['shards']):03d}.sqlite"
    columns = ("assertion_id", "reaction_id", "protein_id", "predicate", "review_status", "epistemic_kind", "evidence_id", "source_ref")

    def read(path: Path) -> tuple[int, list[dict]]:
        connection = _connect(path, readonly=True)
        try:
            total = connection.execute("SELECT COUNT(*) FROM assertion WHERE reaction_id=?", (reaction_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM assertion WHERE reaction_id=? ORDER BY review_status, assertion_id LIMIT ?",
                (reaction_id, limit),
            ).fetchall()
            return total, [dict(zip(columns, row)) for row in rows]
        finally:
            connection.close()

    public_total, public = read(shard)
    private_total, private = (0, [])
    if workspace is not None and (workspace / "private.sqlite").is_file():
        private_total, private = read(workspace / "private.sqlite")
    return {
        "version": manifest["version"],
        "reaction_id": reaction_id,
        "public": {"total": public_total, "assertions": public},
        "private": {"total": private_total, "assertions": private},
        "synthetic": True,
    }


def read_evidence(root: Path, reaction_id: str, evidence_ids: list[str], *, workspace: Path | None = None, version: str | None = None) -> dict:
    if not evidence_ids or len(evidence_ids) > 12:
        raise ValueError("request between 1 and 12 evidence IDs")
    release, manifest = active_release(root, version)
    if _resolve_in_release(release, reaction_id) is None:
        raise ValueError(f"unknown reaction: {reaction_id}")
    shard = release / f"reaction-{shard_number(reaction_id, manifest['shards']):03d}.sqlite"
    rows = []
    for scope, path in (("public", shard), ("private", workspace / "private.sqlite" if workspace else None)):
        if path is None or not path.is_file():
            continue
        connection = _connect(path, readonly=True)
        try:
            for evidence_id in evidence_ids:
                row = connection.execute(
                    """SELECT e.evidence_id, e.source_type, e.source_ref, e.locator
                    FROM evidence e JOIN assertion a ON a.evidence_id=e.evidence_id
                    WHERE a.reaction_id=? AND e.evidence_id=?""",
                    (reaction_id, evidence_id),
                ).fetchone()
                if row:
                    rows.append({"scope": scope, **dict(zip(("evidence_id", "source_type", "source_ref", "locator"), row))})
        finally:
            connection.close()
    return {"version": manifest["version"], "evidence": rows, "synthetic": True}


def add_private(workspace: Path, *, reaction_id: str, protein_id: str, assertion_id: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(workspace / "private.sqlite")
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS assertion(
                assertion_id TEXT PRIMARY KEY, reaction_id TEXT NOT NULL,
                protein_id TEXT NOT NULL, predicate TEXT NOT NULL,
                review_status TEXT NOT NULL, epistemic_kind TEXT NOT NULL,
                evidence_id TEXT NOT NULL, source_ref TEXT NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS private_by_reaction ON assertion(reaction_id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS evidence(
                evidence_id TEXT PRIMARY KEY, source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL, locator TEXT NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO assertion VALUES (?,?,?,?,?,?,?,?)",
            (assertion_id, reaction_id, protein_id, "catalyzes_reported", "unreviewed", "reported", f"E:{assertion_id}", "SYNTHETIC:workspace"),
        )
        connection.execute(
            "INSERT INTO evidence VALUES (?,?,?,?)",
            (f"E:{assertion_id}", "synthetic", "SYNTHETIC:workspace", assertion_id),
        )
        connection.commit()
    finally:
        connection.close()


def apply_delta(root: Path, *, version: str, reaction_id: str, review_status: str) -> dict:
    version = _version(version)
    if review_status not in {"accepted", "unreviewed", "disputed", "rejected"}:
        raise ValueError("unsupported review status")
    old, manifest = active_release(root)
    if resolve(root, reaction_id) is None:
        raise ValueError(f"unknown reaction: {reaction_id}")
    staging = root.resolve() / "releases" / f".building-{version}"
    if staging.exists() or (root.resolve() / "releases" / version).exists():
        raise FileExistsError(version)
    staging.mkdir()
    changed = f"reaction-{shard_number(reaction_id, manifest['shards']):03d}.sqlite"
    started = time.perf_counter()
    try:
        for filename in manifest["files"]:
            if filename == changed:
                shutil.copy2(old / filename, staging / filename)
            else:
                os.link(old / filename, staging / filename)
        connection = _connect(staging / changed)
        try:
            cursor = connection.execute(
                "UPDATE assertion SET review_status=? WHERE assertion_id=? AND reaction_id=?",
                (review_status, f"A:{int(reaction_id[2:]):08d}:000000", reaction_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("delta target assertion missing")
            connection.commit()
        finally:
            connection.close()
        updated = _manifest(staging, version=version, proteins=manifest["proteins"], reactions=manifest["reactions"], edges=manifest["assertions"], shards=manifest["shards"], documents=manifest["document_metadata"], curated_entities=manifest["curated_entities"], curated_assertions=manifest["curated_assertions"])
        _publish(root.resolve(), staging, version)
        return {"delta_seconds": round(time.perf_counter() - started, 3), "changed_file": changed, **updated}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def benchmark(root: Path, *, samples: int = 200) -> dict:
    release, manifest = active_release(root)
    if samples < 1:
        raise ValueError("samples must be positive")
    times = []
    search_times = []
    for i in range(samples):
        reaction_id = f"R:{(i * 7919) % manifest['reactions']:08d}"
        start = time.perf_counter()
        query(root, reaction_id)
        times.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        search(root, f"Synthetic reaction {(i * 7919) % manifest['reactions']}")
        search_times.append((time.perf_counter() - start) * 1000)
    disk_bytes = sum(path.stat().st_size for path in release.glob("*.sqlite"))
    return {
        "version": manifest["version"],
        "samples": samples,
        "query_p50_ms": round(statistics.median(times), 3),
        "query_p95_ms": round(sorted(times)[max(0, int(samples * 0.95) - 1)], 3),
        "query_max_ms": round(max(times), 3),
        "search_p95_ms": round(sorted(search_times)[max(0, int(samples * 0.95) - 1)], 3),
        "index_bytes": disk_bytes,
        "peak_rss_bytes": _peak_rss_bytes(),
        "synthetic": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build")
    builder.add_argument("--version", default="v1")
    builder.add_argument("--proteins", type=int, default=100_000)
    builder.add_argument("--reactions", type=int, default=10_000)
    builder.add_argument("--assertions", type=int, default=1_000_000)
    builder.add_argument("--shards", type=int, default=16)
    builder.add_argument("--documents", type=int, default=30_000)
    builder.add_argument("--curated-bundle", type=Path)
    finder = sub.add_parser("query")
    finder.add_argument("reaction_id")
    finder.add_argument("--workspace", type=Path)
    finder.add_argument("--limit", type=int, default=20)
    finder.add_argument("--version")
    curated = sub.add_parser("curated")
    curated.add_argument("reaction_id")
    curated.add_argument("--version")
    curated.add_argument("--limit", type=int, default=20)
    name_search = sub.add_parser("search")
    name_search.add_argument("text")
    name_search.add_argument("--limit", type=int, default=20)
    name_search.add_argument("--version")
    evidence = sub.add_parser("evidence")
    evidence.add_argument("reaction_id")
    evidence.add_argument("evidence_ids", nargs="+")
    evidence.add_argument("--workspace", type=Path)
    evidence.add_argument("--version")
    delta = sub.add_parser("delta")
    delta.add_argument("--version", required=True)
    delta.add_argument("--reaction", required=True)
    delta.add_argument("--status", default="disputed")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--samples", type=int, default=200)
    sub.add_parser("verify")
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.root, version=args.version, proteins=args.proteins, reactions=args.reactions, edges=args.assertions, shards=args.shards, documents=args.documents, curated_bundle=args.curated_bundle)
    elif args.command == "query":
        result = query(args.root, args.reaction_id, workspace=args.workspace, limit=args.limit, version=args.version)
    elif args.command == "search":
        result = search(args.root, args.text, limit=args.limit, version=args.version)
    elif args.command == "curated":
        result = query_curated(args.root, args.reaction_id, limit=args.limit, version=args.version)
    elif args.command == "evidence":
        result = read_evidence(args.root, args.reaction_id, args.evidence_ids, workspace=args.workspace, version=args.version)
    elif args.command == "delta":
        result = apply_delta(args.root, version=args.version, reaction_id=args.reaction, review_status=args.status)
    elif args.command == "benchmark":
        result = benchmark(args.root, samples=args.samples)
    else:
        result = verify(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
