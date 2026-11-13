"""Streaming migration and immutable releases. Management entry points are local/OS-trusted."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .sqlite_store import build_database, file_digest
from functools import lru_cache
from jsonschema import Draft202012Validator


@lru_cache(maxsize=16)
def contract(name):
    schema = json.loads((Path(__file__).parent / "schemas" / (name + ".schema.json")).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check(name, value):
    errors = list(contract(name).iter_errors(value))
    if errors:
        raise ValueError(f"invalid {name}: {errors[0].message}")



def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(canonical(value) + b'\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_file(root, relative):
    relative = Path(relative)
    root = Path(root).resolve()
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('unsafe relative path')
    path = root / relative
    if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
        raise ValueError('symlink in data path')
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f'missing or unsafe file: {relative}')
    return path


@contextmanager
def writer_lock(root):
    # Single local writer is intentional; multi-host governance is a later stage.
    from ..file_lock import lock_file, unlock_file
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.ontology-build.lock').open('a') as stream:
        lock_file(stream)
        try:
            yield
        finally:
            unlock_file(stream)


def jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path, rows):
    with Path(path).open('wb') as stream:
        for row in rows:
            stream.write(canonical(row) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def verify_manifest(root, manifest):
    payload = dict(manifest)
    recorded = payload.pop('bundle_digest', None)
    if digest(payload) != recorded:
        raise ValueError('manifest digest mismatch')
    if (manifest.get('schema_version'), manifest.get('storage_format')) not in {
        ('1.0', 'sqlite-v1'), ('2.0', 'sqlite-v2')
    }:
        raise ValueError('unsupported release format')
    seen = set()
    for entry in manifest['files']:
        name = entry['path']
        if name in seen:
            raise ValueError('duplicate manifest file')
        seen.add(name)
        if file_digest(safe_file(root, name)) != entry['sha256']:
            raise ValueError(f'checksum mismatch: {name}')
    for suffix in ('entities.sqlite', 'ontology.json'):
        if sum(name.endswith('/' + suffix) or name == suffix for name in seen) != 1:
            raise ValueError(f'exactly one {suffix} required')
    if manifest['storage_format'] == 'sqlite-v2':
        prefix = 'public/releases/' + manifest['release_id'] + '/'
        if any(not name.startswith(prefix) for name in seen):
            raise ValueError('release contains mutable external dependencies')
        if manifest['ontology_path'] not in seen or manifest['database_path'] not in seen:
            raise ValueError('release omits runtime dependencies')
    return manifest


def database_path(root, manifest):
    return safe_file(root, next(e['path'] for e in manifest['files'] if e['path'].endswith('/entities.sqlite')))


def ro_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return db


def rows(db, table):
    # table only comes from constant internal call sites.
    for (payload,) in db.execute(f'SELECT payload FROM {table} ORDER BY id'):
        yield json.loads(payload)


def logical_digest(db):
    h = hashlib.sha256()
    for table in ('entity', 'assertion', 'evidence'):
        h.update(table.encode() + b'\n')
        for row in rows(db, table):
            h.update(canonical(row) + b'\n')
    return h.hexdigest()


def validate_database(path, ontology, source_root=None):
    """SQL-backed reference checks; never materialize the full graph in Python."""
    db = ro_db(path)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database integrity failure')
        classes = {r['id'] for r in ontology['classes']}
        predicates = {r['id']: r for r in ontology['predicates']}
        for r in rows(db, 'entity'):
            if r['entity_type'] not in classes:
                raise ValueError('unknown entity type')
            for evidence_id in r.get('source_evidence_ids', []):
                if not db.execute('SELECT 1 FROM evidence WHERE id=?', (evidence_id,)).fetchone():
                    raise ValueError('missing entity evidence')
        for r in rows(db, 'assertion'):
            pred = predicates.get(r['predicate'])
            subject = db.execute('SELECT type FROM entity WHERE id=?', (r['subject_id'],)).fetchone()
            obj = db.execute('SELECT type FROM entity WHERE id=?', (r['object_id'],)).fetchone()
            if not pred or not subject or not obj or subject[0] not in pred['domains'] or obj[0] not in pred['ranges']:
                raise ValueError('invalid predicate endpoints')
            if r['polarity'] not in ('supports', 'opposes') or r['state'] != 'active':
                raise ValueError('unsupported assertion status')
            if not r.get('evidence_ids'):
                raise ValueError('assertion without evidence')
            for ev in r['evidence_ids']:
                if not db.execute('SELECT 1 FROM evidence WHERE id=?', (ev,)).fetchone():
                    raise ValueError('missing assertion evidence')
        v2 = db.execute("SELECT 1 FROM sqlite_master WHERE name='source_revision'").fetchone()
        for r in rows(db, 'evidence'):
            if hashlib.sha256(r['quote'].encode()).hexdigest() != r['quote_sha256']:
                raise ValueError('quote hash mismatch')
            if source_root is not None:
                if v2:
                    source = db.execute('SELECT object_path,object_sha256 FROM source_revision WHERE source_id=? AND revision=1', (r['card_id'],)).fetchone()
                    if not source:
                        raise ValueError('missing source revision')
                    file = safe_file(source_root, source[0])
                    if file_digest(file) != source[1] or source[1] != r['card_sha256']:
                        raise ValueError('source hash mismatch')
                else:
                    file = safe_file(source_root, 'wiki_snapshot/' + r['snapshot_path'])
                    if file_digest(file) != r['card_sha256']:
                        raise ValueError('source hash mismatch')
                # One source at a time, not the full corpus.
                lines = file.read_text(encoding='utf-8').splitlines()
                start, end = r['line_start'], r['line_end']
                if start < 1 or end < start or end > len(lines) or '\n'.join(lines[start - 1:end]) != r['quote']:
                    raise ValueError('evidence locator/quote mismatch')
        counts = {key: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table, key in [('entity', 'entities'), ('assertion', 'assertions'), ('evidence', 'evidence')]}
        return {**counts, 'logical_digest': logical_digest(db)}
    finally:
        db.close()


def migrate(knowledge_root, destination):
    """One-time legacy import; attests equivalence, never invents human review."""
    root, destination = Path(knowledge_root).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('migration destination already exists; use a fresh directory')
    manifest = verify_manifest(root, read_json(root / 'entity_manifest.json'))
    if manifest['storage_format'] != 'sqlite-v1':
        raise ValueError('migration requires a legacy v1 bundle')
    ontology = read_json(root / 'ontology.json')
    baseline = validate_database(database_path(root, manifest), ontology, root)
    source = read_json(root / 'curation_source.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.migration-', dir=destination.parent))
    try:
        (temporary / 'objects').mkdir()
        (temporary / 'changesets').mkdir()
        shutil.copyfile(database_path(root, manifest), temporary / 'baseline.sqlite')
        sources = []
        for card in source['source_cards']:
            original = safe_file(root, 'wiki_snapshot/' + card['snapshot_path'])
            sha = file_digest(original)
            if sha != card['sha256']:
                raise ValueError('source revision hash mismatch')
            relative = 'objects/' + sha
            shutil.copyfile(original, temporary / relative)
            sources.append({'source_id': card['card_id'], 'revision': 1, 'object_path': relative,
                            'object_sha256': sha, 'scope': 'public', 'workspace_id': None,
                            'media_type': 'text/markdown', 'original_name': original.name,
                            'legacy_metadata': card})
        write_jsonl(temporary / 'sources.jsonl', sources)
        write_json(temporary / 'ontology.json', ontology)
        shutil.copyfile(root / 'ontology.json', temporary / 'baseline-ontology.json')
        db = ro_db(database_path(root, manifest))
        try:
            spans = ({'span_id': 'span:' + r['evidence_id'], 'source_id': r['card_id'], 'revision': 1,
                      'line_start': r['line_start'], 'line_end': r['line_end'], 'text': r['quote'],
                      'text_sha256': r['quote_sha256'], 'extractor_version': 'legacy-lines-v1'}
                     for r in rows(db, 'evidence'))
            write_jsonl(temporary / 'spans.jsonl', spans)
            for table, key in [('entity', 'entity_id'), ('assertion', 'assertion_id'), ('evidence', 'evidence_id')]:
                write_jsonl(temporary / 'changesets' / (table + '.jsonl'),
                            ({'op': 'add_' + table, 'item_id': r[key], 'payload': r} for r in rows(db, table)))
            write_jsonl(temporary / 'id-map.jsonl',
                        ({'kind': table, 'old_id': r[key], 'new_id': r[key]}
                         for table, key in [('entity', 'entity_id'), ('assertion', 'assertion_id'), ('evidence', 'evidence_id')]
                         for r in rows(db, table)))
        finally:
            db.close()
        files = [{'path': str(p.relative_to(temporary)), 'sha256': file_digest(p)}
                 for p in sorted(temporary.rglob('*')) if p.is_file()]
        batch = {'schema_version': '2.0', 'batch_id': 'legacy-' + manifest['bundle_digest'][:20],
                 'scope': 'public', 'workspace_id': None, 'files': files,
                 'baseline': baseline, 'legacy_manifest': manifest,
                 'provenance': 'legacy_curated', 'decision': 'preserve_legacy',
                 'reviewer_user_id': None,
                 'review_note': '技术等价迁移，保留既有策展状态；未新增或伪造人类科学审核。'}
        batch['payload_hash'] = digest(batch)
        write_json(temporary / 'change-set.json', batch)
        write_json(temporary / 'migration-report.json', {'ok': True, 'baseline': baseline,
                   'source_cards': len(sources), 'identity_mapping': 'id-map.jsonl',
                   'new_scientific_review': False, 'legacy_bundle_digest': manifest['bundle_digest']})
        os.rename(temporary, destination)
        sync_dir(destination.parent)
        return read_json(destination / 'migration-report.json')
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


DDL = '''
CREATE TABLE meta(schema_version TEXT NOT NULL,scope TEXT NOT NULL CHECK(scope='public'),revision INTEGER NOT NULL);
INSERT INTO meta VALUES ('2.0','public',1);
CREATE TABLE source_revision(source_id TEXT,revision INTEGER,object_path TEXT NOT NULL,object_sha256 TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE span(span_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,FOREIGN KEY(source_id,revision) REFERENCES source_revision(source_id,revision));
CREATE INDEX span_source ON span(source_id,revision);
CREATE TABLE evidence_span(evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),span_id TEXT NOT NULL REFERENCES span(span_id));
CREATE TABLE assertion_evidence(assertion_id TEXT REFERENCES assertion(id),evidence_id TEXT REFERENCES evidence(id),PRIMARY KEY(assertion_id,evidence_id));
CREATE INDEX evidence_assertion ON assertion_evidence(evidence_id,assertion_id);
CREATE TABLE external_id(namespace TEXT,accession TEXT,entity_id TEXT REFERENCES entity(id),PRIMARY KEY(namespace,accession));
CREATE TABLE change_set(batch_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE review_decision(batch_id TEXT PRIMARY KEY REFERENCES change_set(batch_id),decision TEXT NOT NULL CHECK(decision='preserve_legacy'),reviewer_user_id TEXT,payload_hash TEXT NOT NULL);
CREATE TABLE audit_event(event_id TEXT PRIMARY KEY,action TEXT NOT NULL,actor_kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE assertion_revision(assertion_id TEXT REFERENCES assertion(id),revision INTEGER,review_state TEXT CHECK(review_state='legacy_curated'),conflict_state TEXT CHECK(conflict_state IN ('none','open','resolved')),payload TEXT NOT NULL,PRIMARY KEY(assertion_id,revision));
'''


def build(import_root, knowledge_root, *, fail_at=None):
    """Build a sealed legacy migration. General reviewed deltas arrive in stage 3."""
    imported, root = Path(import_root).resolve(), Path(knowledge_root).resolve()
    batch = read_json(safe_file(imported, 'change-set.json'))
    check('change-set', batch)
    body = dict(batch)
    recorded = body.pop('payload_hash', None)
    if recorded != digest(body) or batch['decision'] != 'preserve_legacy' or batch['provenance'] != 'legacy_curated':
        raise ValueError('unapproved or changed migration batch')
    seen = set()
    for item in batch['files']:
        if item['path'] in seen or file_digest(safe_file(imported, item['path'])) != item['sha256']:
            raise ValueError('changed or duplicate migration input')
        seen.add(item['path'])
    required = {'ontology.json', 'sources.jsonl', 'spans.jsonl', 'changesets/entity.jsonl', 'changesets/assertion.jsonl', 'changesets/evidence.jsonl'}
    if not required <= seen:
        raise ValueError('incomplete migration')
    legacy = batch['legacy_manifest']
    old_db_entry = next(e for e in legacy['files'] if e['path'].endswith('/entities.sqlite'))
    frozen_db = safe_file(imported, 'baseline.sqlite')
    if file_digest(frozen_db) != old_db_entry['sha256']:
        raise ValueError('frozen baseline database differs from legacy manifest')
    ontology = read_json(imported / 'ontology.json')
    old_ontology = next(e for e in legacy['files'] if e['path'] == 'ontology.json')
    # Legacy ontology uses different whitespace; compare against migration's canonical baseline
    # via an exact source copy, not a reformatted file's hash.
    legacy_ontology = safe_file(imported, 'baseline-ontology.json')
    if file_digest(legacy_ontology) != old_ontology['sha256'] or read_json(legacy_ontology) != ontology:
        raise ValueError('ontology differs from frozen legacy schema')
    if validate_database(frozen_db, ontology) != batch['baseline']:
        raise ValueError('baseline logical digest mismatch')
    def records(table):
        for row in jsonl(imported / 'changesets' / (table + '.jsonl')):
            if row['op'] != 'add_' + table or row['item_id'] != row['payload'][table + '_id']:
                raise ValueError('invalid operation identity')
            check(table, row['payload'])
            yield row['payload']
    with writer_lock(root):
        staging_root = root / 'staging'
        staging_root.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='release-', dir=staging_root))
        try:
            write_json(temporary / 'ontology.json', ontology)
            database = temporary / 'entities.sqlite'
            build_database(database, records('entity'), records('assertion'), records('evidence'))
            if fail_at == 'database':
                raise RuntimeError('injected after database build')
            db = sqlite3.connect(database)
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.executescript(DDL)
                db.execute('INSERT INTO change_set VALUES (?,?,?)', (batch['batch_id'], recorded, canonical(batch).decode()))
                db.execute('INSERT INTO review_decision VALUES (?,?,?,?)', (batch['batch_id'], 'preserve_legacy', None, recorded))
                db.execute('INSERT INTO audit_event VALUES (?,?,?,?)', (batch['batch_id'], 'legacy_migration', 'migration_tool', canonical(batch['baseline']).decode()))
                for row in jsonl(imported / 'sources.jsonl'):
                    check('source-document', row)
                    if (row['scope'] != 'public' or row['workspace_id'] is not None
                            or row['object_path'] not in seen or row['object_path'] != 'objects/' + row['object_sha256']):
                        raise ValueError('invalid source scope/path')
                    original = safe_file(imported, row['object_path'])
                    if file_digest(original) != row['object_sha256']:
                        raise ValueError('source object mismatch')
                    target = temporary / row['object_path']
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
                    db.execute('INSERT INTO source_revision VALUES (?,?,?,?,?)', (row['source_id'], row['revision'], row['object_path'], row['object_sha256'], canonical(row).decode()))
                for row in jsonl(imported / 'spans.jsonl'):
                    check('extracted-span', row)
                    db.execute('INSERT INTO span VALUES (?,?,?,?)', (row['span_id'], row['source_id'], row['revision'], canonical(row).decode()))
                for row in rows(db, 'evidence'):
                    span_id = 'span:' + row['evidence_id']
                    span_row = db.execute('SELECT payload FROM span WHERE span_id=?', (span_id,)).fetchone()
                    if not span_row:
                        raise ValueError('missing evidence span')
                    span = json.loads(span_row[0])
                    if (span['source_id'], span['text'], span['text_sha256'], span['line_start'], span['line_end']) != (row['card_id'], row['quote'], row['quote_sha256'], row['line_start'], row['line_end']):
                        raise ValueError('span differs from evidence')
                    db.execute('INSERT INTO evidence_span VALUES (?,?)', (row['evidence_id'], span_id))
                for row in rows(db, 'assertion'):
                    db.execute('INSERT INTO assertion_revision VALUES (?, ?, ?, ?, ?)', (row['assertion_id'], row['revision'], 'legacy_curated', 'none', canonical(row).decode()))
                    for ev in row['evidence_ids']:
                        db.execute('INSERT INTO assertion_evidence VALUES (?,?)', (row['assertion_id'], ev))
                for row in rows(db, 'entity'):
                    for namespace, accession in row.get('external_ids', {}).items():
                        db.execute('INSERT INTO external_id VALUES (?,?,?)', (namespace, accession, row['entity_id']))
                if db.execute('SELECT count(*) FROM source_revision').fetchone()[0] != batch['legacy_manifest']['statistics']['source_cards']:
                    raise ValueError('source count differs from baseline')
                db.commit()
            finally:
                db.close()
            actual = validate_database(database, ontology, temporary)
            if actual != batch['baseline']:
                raise ValueError('migration differs from frozen legacy baseline')
            # Bind the baseline to the supplied legacy manifest, not just input counts.
            if digest({k: v for k, v in batch['legacy_manifest'].items() if k != 'bundle_digest'}) != batch['legacy_manifest']['bundle_digest']:
                raise ValueError('invalid legacy provenance')
            files = [{'path': str(p.relative_to(temporary)), 'sha256': file_digest(p)} for p in sorted(temporary.rglob('*')) if p.is_file()]
            release_id = digest({'files': files, 'input': recorded})
            prefix = 'public/releases/' + release_id + '/'
            manifest = {**{k: batch['legacy_manifest'][k] for k in ('ontology_version', 'bundle_version', 'statistics')},
                        'schema_version': '2.0', 'storage_format': 'sqlite-v2', 'release_id': release_id,
                        'logical_digest': actual['logical_digest'], 'input_digest': recorded,
                        'database_path': prefix + 'entities.sqlite', 'ontology_path': prefix + 'ontology.json',
                        'files': [{**e, 'path': prefix + e['path']} for e in files]}
            manifest['bundle_digest'] = digest(manifest)
            check('release-manifest', manifest)
            write_json(temporary / 'manifest.json', manifest)
            if fail_at == 'validated':
                raise RuntimeError('injected after validation')
            destination = root / prefix
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if read_json(safe_file(destination, 'manifest.json')) != manifest:
                    raise ValueError('existing release manifest differs')
                verify_manifest(root, manifest)
            else:
                for path in temporary.rglob('*'):
                    if path.is_file():
                        with path.open('rb') as stream:
                            os.fsync(stream.fileno())
                for directory in sorted((p for p in temporary.rglob('*') if p.is_dir()), reverse=True):
                    sync_dir(directory)
                sync_dir(temporary)
                os.rename(temporary, destination)
                sync_dir(destination.parent)
            if fail_at == 'renamed':
                raise RuntimeError('injected before activation')
            # A single atomic pointer/manifest preserves the existing plugin entry path.
            write_json(root / 'entity_manifest.json', manifest)
            if fail_at == 'activated':
                raise RuntimeError('injected after activation')
            return {'ok': True, **actual, 'release_id': release_id, 'bundle_digest': manifest['bundle_digest']}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def validate(root):
    root = Path(root).resolve()
    manifest = verify_manifest(root, read_json(root / 'entity_manifest.json'))
    ontology = read_json(safe_file(root, manifest.get('ontology_path', 'ontology.json')))
    source_root = database_path(root, manifest).parent if manifest['storage_format'] == 'sqlite-v2' else root
    result = validate_database(database_path(root, manifest), ontology, source_root)
    if any(result[k] != manifest['statistics'][k] for k in ('entities', 'assertions', 'evidence')):
        raise ValueError('manifest count mismatch')
    return {'ok': True, 'source_cards': manifest['statistics']['source_cards'], **result,
            'database_sha256': file_digest(database_path(root, manifest))}


def activate(knowledge_root, manifest_path):
    """Stage-one recovery only: validate a retained complete bundle before switching."""
    root = Path(knowledge_root).resolve()
    with writer_lock(root):
        manifest = verify_manifest(root, read_json(manifest_path))
        ontology = read_json(safe_file(root, manifest.get('ontology_path', 'ontology.json')))
        source_root = database_path(root, manifest).parent if manifest['storage_format'] == 'sqlite-v2' else root
        result = validate_database(database_path(root, manifest), ontology, source_root)
        if any(result[k] != manifest['statistics'][k] for k in ('entities', 'assertions', 'evidence')):
            raise ValueError('manifest count mismatch')
        write_json(root / 'entity_manifest.json', manifest)
        return {'ok': True, 'bundle_digest': manifest['bundle_digest'], **result}
