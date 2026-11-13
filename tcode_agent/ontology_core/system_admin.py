"""OS-trusted administration for the platform-owned public ontology."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .bundle import (canonical, check, database_path, digest, file_digest, read_json,
                     ro_db, rows, safe_file, sync_dir, validate_database, verify_manifest,
                     write_json, writer_lock)
from .sqlite_store import normalize_term


KINDS = {'entity': ('entity_id', 'entity'), 'assertion': ('assertion_id', 'assertion'),
         'evidence': ('evidence_id', 'evidence')}


def active(root):
    root = Path(root).resolve()
    manifest = verify_manifest(root, read_json(root / 'entity_manifest.json'))
    return root, manifest


def status(root):
    root, manifest = active(root)
    return {'ok': True, 'root': str(root), 'release_id': manifest['release_id'],
            'ontology_version': manifest.get('ontology_version'),
            'statistics': manifest['statistics'], 'retained_releases': len(list((root / 'public/releases').glob('*')))}


def schema(root):
    root, manifest = active(root)
    return read_json(safe_file(root, manifest['ontology_path']))


def list_records(root, kind, limit=100):
    if kind not in KINDS:
        raise ValueError('kind must be entity, assertion, or evidence')
    root, manifest = active(root)
    db = ro_db(database_path(root, manifest))
    try:
        return {'release_id': manifest['release_id'], 'kind': kind,
                'items': list(rows(db, KINDS[kind][1]))[:limit]}
    finally:
        db.close()


def show(root, kind, item_id):
    if kind not in KINDS:
        raise ValueError('kind must be entity, assertion, or evidence')
    root, manifest = active(root)
    db = ro_db(database_path(root, manifest))
    try:
        row = db.execute(f'SELECT payload FROM {KINDS[kind][1]} WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError(f'{kind} not found: {item_id}')
        return json.loads(row[0])
    finally:
        db.close()


def diff(root, left_release, right_release):
    root = Path(root).resolve()
    def ids(release, table):
        manifest = read_json(safe_file(root, f'public/releases/{release}/manifest.json'))
        db = ro_db(database_path(root, manifest))
        try:
            return {row[0] for row in db.execute(f'SELECT id FROM {table}')}
        finally:
            db.close()
    result = {'left_release_id': left_release, 'right_release_id': right_release}
    for kind, (_, table) in KINDS.items():
        left, right = ids(left_release, table), ids(right_release, table)
        result[kind] = {'added': sorted(right - left), 'removed': sorted(left - right)}
    return result


def _payloads(db, table):
    return {item_id: json.loads(payload) for item_id, payload in db.execute(f'SELECT id,payload FROM {table}')}


def _apply(items, kind, operation):
    key, _ = KINDS[kind]
    action = operation['op']
    item_id = operation.get('id') or (operation.get('payload') or {}).get(key)
    if not item_id:
        raise ValueError(f'{action} requires id')
    if action == f'add_{kind}':
        payload = operation['payload']
        check(kind, payload)
        if item_id in items and items[item_id] != payload:
            raise ValueError(f'{kind} already exists: {item_id}')
        items[item_id] = payload
    elif action == f'update_{kind}':
        if item_id not in items:
            raise ValueError(f'{kind} not found: {item_id}')
        payload = operation.get('payload') or {**items[item_id], **operation.get('changes', {})}
        if payload.get(key) != item_id:
            raise ValueError(f'{kind} identity cannot change')
        check(kind, payload)
        items[item_id] = payload
    elif action == f'remove_{kind}':
        if item_id not in items:
            return
        del items[item_id]
    else:
        raise ValueError(f'unsupported operation: {action}')


def _replace(db, table, items):
    db.execute(f'DELETE FROM {table}')
    if table == 'entity':
        db.executemany('INSERT INTO entity VALUES(?,?,?,?)',
                       ((r['entity_id'], r['entity_type'], r['label'], canonical(r).decode()) for r in items.values()))
    elif table == 'assertion':
        db.executemany('INSERT INTO assertion VALUES(?,?,?,?,?,?,?)',
                       ((r['assertion_id'], r['subject_id'], r['predicate'], r['object_id'], r['state'], r['polarity'], canonical(r).decode()) for r in items.values()))
    else:
        db.executemany('INSERT INTO evidence VALUES(?,?,?)',
                       ((r['evidence_id'], r['wiki_ref'], canonical(r).decode()) for r in items.values()))


def _ensure_indexes(db, entities, assertions):
    db.execute('DELETE FROM alias'); db.execute('DELETE FROM alias_token')
    aliases = set()
    for row in entities.values():
        terms = [row['entity_id'], row['label'], *(row.get('aliases') or []), *((row.get('external_ids') or {}).values())]
        aliases.update((term, row['entity_id']) for raw in terms if (term := normalize_term(raw)))
    db.executemany('INSERT INTO alias VALUES(?,?)', sorted(aliases))
    db.executemany('INSERT INTO alias_token VALUES(?,?)', sorted({(token, entity_id) for term, entity_id in aliases for token in term.split()}))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='assertion_evidence'").fetchone():
        db.execute('DELETE FROM assertion_evidence')
        db.executemany('INSERT INTO assertion_evidence VALUES(?,?)',
                       ((row['assertion_id'], evidence_id) for row in assertions.values() for evidence_id in row['evidence_ids']))


def apply_change_set(root, change_set, *, publish=False, package_root=None):
    """Validate a JSON change set and optionally publish one immutable release."""
    root, current = active(root)
    expected = change_set.get('expected_release_id')
    if not expected:
        raise ValueError('expected_release_id is required')
    if expected != current['release_id']:
        raise ValueError(f'release conflict: expected {expected}, active {current["release_id"]}')
    operations = change_set.get('operations')
    if not isinstance(operations, list) or not operations:
        raise ValueError('operations must be a non-empty list')
    input_digest = digest(change_set)
    if current.get('input_digest') == input_digest:
        return {'ok': True, 'published': True, 'idempotent': True, 'release_id': current['release_id']}
    ontology = change_set.get('ontology') or schema(root)
    source_release = database_path(root, current).parent
    with writer_lock(root):
        staging = root / 'staging'; staging.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='admin-', dir=staging))
        try:
            for path in source_release.rglob('*'):
                if path.is_file() and path.name != 'manifest.json':
                    target = temporary / path.relative_to(source_release)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            write_json(temporary / 'ontology.json', ontology)
            db_path = temporary / 'entities.sqlite'
            db = sqlite3.connect(db_path)
            try:
                db.execute('PRAGMA foreign_keys=OFF')
                entities, assertions, evidence = (_payloads(db, name) for name in ('entity', 'assertion', 'evidence'))
                if package_root is not None:
                    package_root, package_manifest = active(package_root)
                    package_release = database_path(package_root, package_manifest).parent
                    package_db = ro_db(database_path(package_root, package_manifest))
                    try:
                        for kind, target in (('entity', entities), ('assertion', assertions), ('evidence', evidence)):
                            for item_id, payload in _payloads(package_db, kind).items():
                                if item_id in target and target[item_id] != payload:
                                    raise ValueError(f'package conflict for {kind}: {item_id}')
                                target[item_id] = payload
                        _merge_auxiliary(db, package_db)
                    finally:
                        package_db.close()
                    for path in (package_release / 'objects').glob('*'):
                        target = temporary / 'objects' / path.name
                        target.parent.mkdir(exist_ok=True)
                        if target.exists() and file_digest(target) != file_digest(path):
                            raise ValueError(f'package object conflict: {path.name}')
                        if not target.exists():
                            shutil.copyfile(path, target)
                if any(op.get('op') == 'clear' for op in operations):
                    if len(operations) != 1:
                        raise ValueError('clear must be the only operation')
                    entities.clear(); assertions.clear(); evidence.clear()
                    for table in ('assertion_revision', 'assertion_evidence', 'evidence_span', 'span', 'source_revision', 'external_id'):
                        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            db.execute(f'DELETE FROM {table}')
                    shutil.rmtree(temporary / 'objects', ignore_errors=True)
                else:
                    for op in operations:
                        if op.get('op') == 'package_contribution' and package_root is not None:
                            continue
                        if op.get('op') == 'update_schema':
                            if not change_set.get('ontology'):
                                raise ValueError('update_schema requires explicit ontology')
                            continue
                        matched = next((kind for kind in KINDS if op.get('op', '').endswith('_' + kind)), None)
                        if not matched:
                            raise ValueError(f'unsupported operation: {op.get("op")}')
                        _apply({'entity': entities, 'assertion': assertions, 'evidence': evidence}[matched], matched, op)
                _replace(db, 'assertion', {})
                _replace(db, 'evidence', evidence)
                _replace(db, 'entity', entities)
                _replace(db, 'assertion', assertions)
                _ensure_indexes(db, entities, assertions)
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_event'").fetchone():
                    db.execute('INSERT OR IGNORE INTO audit_event VALUES(?,?,?,?)',
                               ('admin:' + input_digest, 'system_change_set', 'local_admin_cli', canonical(change_set).decode()))
                db.commit()
            finally:
                db.close()
            actual = validate_database(db_path, ontology, temporary)
            files = [{'path': str(path.relative_to(temporary)), 'sha256': file_digest(path)}
                     for path in sorted(temporary.rglob('*')) if path.is_file()]
            release_id = digest({'files': files, 'input': input_digest})
            result = {'ok': True, 'published': False, 'release_id': release_id,
                      'previous_release_id': current['release_id'], **actual}
            if not publish:
                return result
            prefix = f'public/releases/{release_id}/'
            manifest = {'schema_version': '2.0', 'storage_format': 'sqlite-v2', 'release_id': release_id,
                        'bundle_version': release_id[:12], 'ontology_version': ontology.get('ontology_version', '1.0.0'),
                        'logical_digest': actual['logical_digest'], 'input_digest': input_digest,
                        'database_path': prefix + 'entities.sqlite', 'ontology_path': prefix + 'ontology.json',
                        'statistics': {**{k: actual[k] for k in ('entities', 'assertions', 'evidence')},
                                       'source_cards': _source_count(db_path)},
                        'files': [{**entry, 'path': prefix + entry['path']} for entry in files]}
            manifest['bundle_digest'] = digest(manifest)
            write_json(temporary / 'manifest.json', manifest)
            destination = root / 'public/releases' / release_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary.rename(destination); sync_dir(destination.parent)
            write_json(root / 'entity_manifest.json', manifest)
            return {**result, 'published': True, 'bundle_digest': manifest['bundle_digest']}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _source_count(path):
    db = sqlite3.connect(path)
    try:
        return db.execute('SELECT count(*) FROM source_revision').fetchone()[0]
    finally:
        db.close()


def _merge_auxiliary(target, source):
    """Merge provenance tables without same-key/different-value overwrites."""
    for table in ('source_revision', 'span', 'evidence_span'):
        if not target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        columns = [row[1] for row in target.execute(f'PRAGMA table_info({table})')]
        if columns != [row[1] for row in source.execute(f'PRAGMA table_info({table})')]:
            raise ValueError(f'package table shape conflict: {table}')
        placeholders = ','.join('?' for _ in columns)
        for row in source.execute(f'SELECT * FROM {table}'):
            try:
                target.execute(f'INSERT INTO {table} VALUES({placeholders})', tuple(row))
            except sqlite3.IntegrityError:
                keys = [item[1] for item in target.execute(f'PRAGMA table_info({table})') if item[5]]
                where = ' AND '.join(f'{name}=?' for name in keys)
                values = tuple(row[columns.index(name)] for name in keys)
                existing = target.execute(f'SELECT * FROM {table} WHERE {where}', values).fetchone()
                if existing != tuple(row):
                    raise ValueError(f'package provenance conflict: {table}:{values}')


def _merge_ontology(left, right):
    result = {'ontology_version': left.get('ontology_version', '1.0.0')}
    for collection in ('classes', 'predicates'):
        items = {item['id']: item for item in left[collection]}
        for item in right[collection]:
            existing = items.get(item['id'])
            if existing is not None and existing != item:
                # The empty platform schema deliberately declares only minimal
                # IDs; a package may enrich that declaration, never contradict it.
                if not all(item.get(key) == value for key, value in existing.items()):
                    raise ValueError(f'package ontology conflict: {collection}:{item["id"]}')
            items[item['id']] = item
        result[collection] = list(items.values())
    return result


def contribute_package(root, provider_id, package_root, *, publish=True):
    """Import one immutable plugin bundle into a host-owned release."""
    root, current = active(root)
    package_root, package = active(package_root)
    contribution = {'provider_id': provider_id, 'package_release_id': package['release_id'],
                    'package_bundle_digest': package['bundle_digest']}
    db = ro_db(database_path(root, current))
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'").fetchone():
            for (payload,) in db.execute("SELECT payload FROM audit_event WHERE action='system_change_set'"):
                if json.loads(payload).get('contribution') == contribution:
                    return {'ok': True, 'published': True, 'idempotent': True,
                            'release_id': current['release_id'], 'contribution': contribution}
    finally:
        db.close()
    change_set = {'expected_release_id': current['release_id'],
                  'operations': [{'op': 'package_contribution'}], 'contribution': contribution,
                  'ontology': _merge_ontology(schema(root), schema(package_root))}
    result = apply_change_set(root, change_set, publish=publish, package_root=package_root)
    return {**result, 'contribution': contribution}


def rollback(root, release_id, *, publish=False):
    root = Path(root).resolve()
    manifest_path = root / 'public/releases' / release_id / 'manifest.json'
    if not manifest_path.is_file():
        raise ValueError(f'release not found: {release_id}')
    manifest = verify_manifest(root, read_json(manifest_path))
    if publish:
        write_json(root / 'entity_manifest.json', manifest)
    return {'ok': True, 'published': publish, 'release_id': release_id}
