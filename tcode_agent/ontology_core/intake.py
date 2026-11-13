"""Local draft intake with durable per-document/per-span checkpoints.

This is an OS-trusted CLI backend, not an authenticated multi-user API.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .bundle import canonical, digest, file_digest, jsonl, safe_file, sync_dir, write_json, writer_lock
from .document_parser import PARSER_VERSION
from .scientific import ENTITY_FIELDS
from .provenance import adjacent_window
from .extraction import LocalKnowledge, PROMPT_VERSION, ModelRequestError, model_candidates, normalize_candidates


# ponytail: one server process; serialize schema/WAL bootstrap only, not queries.
INITIALIZE_LOCK = threading.RLock()


DDL = '''
CREATE TABLE IF NOT EXISTS scope(scope TEXT NOT NULL,workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_revision(source_id TEXT,revision INTEGER,sha256 TEXT NOT NULL,media TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE IF NOT EXISTS job(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,payload_hash TEXT NOT NULL,config TEXT NOT NULL,state TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,deleted_at REAL,deleted_by TEXT);
CREATE TABLE IF NOT EXISTS item(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES job(id),source_id TEXT,revision INTEGER,sha256 TEXT,media TEXT,name TEXT,state TEXT NOT NULL,error TEXT,attempt INTEGER NOT NULL DEFAULT 0,report TEXT,FOREIGN KEY(source_id,revision) REFERENCES source_revision(source_id,revision));
CREATE INDEX IF NOT EXISTS item_job ON item(job_id,state,id);
CREATE TABLE IF NOT EXISTS span(item_id TEXT REFERENCES item(id),ordinal INTEGER,payload TEXT NOT NULL,candidates TEXT,PRIMARY KEY(item_id,ordinal));
CREATE TABLE IF NOT EXISTS draft_assertion(item_id TEXT REFERENCES item(id),ordinal INTEGER,position INTEGER,subject TEXT,predicate TEXT,object TEXT,polarity TEXT,qualifiers TEXT,PRIMARY KEY(item_id,ordinal,position));
CREATE INDEX IF NOT EXISTS draft_conflict ON draft_assertion(item_id,subject,predicate,object,polarity);
CREATE TABLE IF NOT EXISTS extraction_attempt(item_id TEXT REFERENCES item(id),attempt INTEGER,state TEXT NOT NULL,error TEXT,started_at REAL NOT NULL,finished_at REAL,versions TEXT NOT NULL,PRIMARY KEY(item_id,attempt));
CREATE TABLE IF NOT EXISTS span_attempt(item_id TEXT,attempt INTEGER,ordinal INTEGER,state TEXT NOT NULL,error TEXT,PRIMARY KEY(item_id,attempt,ordinal),FOREIGN KEY(item_id,attempt) REFERENCES extraction_attempt(item_id,attempt));
CREATE TABLE IF NOT EXISTS extraction_pass(item_id TEXT,attempt INTEGER,ordinal INTEGER,pass INTEGER,state TEXT NOT NULL,error TEXT,result_digest TEXT,PRIMARY KEY(item_id,attempt,ordinal,pass),FOREIGN KEY(item_id,attempt) REFERENCES extraction_attempt(item_id,attempt));
CREATE TABLE IF NOT EXISTS glean_retry(item_id TEXT,ordinal INTEGER,attempt INTEGER,state TEXT NOT NULL,error TEXT,candidates TEXT,PRIMARY KEY(item_id,ordinal,attempt));
'''


class DraftIntake:
    def __init__(self, root, *, scope='workspace', workspace_id):
        if scope not in ('workspace', 'public') or (scope == 'workspace' and not workspace_id) or (scope == 'public' and workspace_id):
            raise ValueError('invalid scope binding')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ('sources/objects', 'changesets', 'staging'):
            path = self.root / directory
            path.mkdir(parents=True, exist_ok=True)
            if not path.resolve().is_relative_to(self.root) or path.is_symlink():
                raise ValueError('unsafe data directory')
        path = self.root / 'drafts.sqlite'
        if path.is_symlink():
            raise ValueError('unsafe database path')
        with INITIALIZE_LOCK:
            self.db = sqlite3.connect(path, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript(DDL)
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(job)')}
            if 'deleted_at' not in columns:
                self.db.execute('ALTER TABLE job ADD COLUMN deleted_at REAL')
            if 'deleted_by' not in columns:
                self.db.execute('ALTER TABLE job ADD COLUMN deleted_by TEXT')
            try:
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    row = self.db.execute('SELECT scope,workspace_id FROM scope').fetchone()
                    if row and tuple(row) != (scope, workspace_id):
                        raise ValueError('data root belongs to another scope/workspace')
                    if not row:
                        self.db.execute('INSERT INTO scope VALUES (?,?)', (scope, workspace_id))
            except BaseException:
                self.db.close()
                raise
        self.namespace = scope + ':' + workspace_id

    def close(self):
        self.db.close()

    def _ingest(self, filename, source_id, base_revision, max_bytes):
        path = Path(filename)
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing_or_symlink_source')
        media = path.suffix.lower().lstrip('.')
        if media not in ('txt', 'md', 'pdf'):
            raise ValueError('unsupported_document_type')
        fd, tmp = tempfile.mkstemp(dir=self.root / 'staging')
        try:
            sha, size = hashlib.sha256(), 0
            with os.fdopen(fd, 'wb') as target, path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError('file_size_budget_exceeded')
                    sha.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            value = sha.hexdigest()
            relative = 'sources/objects/' + value[:2] + '/' + value
            target = self.root / relative
            target.parent.mkdir(exist_ok=True)
            if target.parent.is_symlink() or target.is_symlink():
                raise ValueError('unsafe_object_path')
            if target.exists():
                if file_digest(target) != value:
                    raise ValueError('corrupt_existing_object')
            else:
                os.replace(tmp, target)
                sync_dir(target.parent)
            sid = source_id or 'source:' + value
            current = self.db.execute('SELECT revision,sha256 FROM source_revision WHERE source_id=? ORDER BY revision DESC LIMIT 1', (sid,)).fetchone()
            if current and current['sha256'] == value:
                revision = current['revision']
            else:
                if current and base_revision != current['revision']:
                    raise ValueError('source_revision_conflict')
                if not current and base_revision not in (None, 0):
                    raise ValueError('source_revision_conflict')
                revision = current['revision'] + 1 if current else 1
                self.db.execute('INSERT INTO source_revision VALUES (?,?,?,?,?)', (sid, revision, value, media, path.name))
            return {'source_id': sid, 'revision': revision, 'sha256': value, 'media': media, 'name': path.name, 'state': 'queued', 'error': None}
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def submit(self, files, *, knowledge_root, mode='mentions', model=None, model_ref=None, base_url='http://127.0.0.1:11434/v1', idempotency_key=None, source_id=None, base_revision=None, max_file_bytes=100 * 1024 * 1024, allowed_model_endpoints=None, model_timeout_seconds=60, model_reasoning_effort=None):
        knowledge_root = Path(knowledge_root).resolve()
        if self.root == knowledge_root or self.root.is_relative_to(knowledge_root) or knowledge_root.is_relative_to(self.root):
            raise ValueError('draft and published data roots must be separate')
        if max_file_bytes <= 0 or not files or (source_id and len(files) != 1) or mode not in ('mentions', 'model') or (mode == 'model' and not model):
            raise ValueError('invalid intake configuration')
        knowledge = LocalKnowledge(knowledge_root)
        config = {'knowledge_root': str(knowledge_root), 'bundle_digest': knowledge.manifest['bundle_digest'],
                  'logical_digest': knowledge.manifest.get('logical_digest'),
                  'mode': mode, 'model': model, 'model_ref': model_ref, 'base_url': base_url, 'allowed_model_endpoints': list(allowed_model_endpoints or []), 'model_timeout_seconds': model_timeout_seconds, 'model_reasoning_effort': model_reasoning_effort, 'parser_version': PARSER_VERSION, 'evidence_window_version': 1, 'prompt_version': PROMPT_VERSION}
        knowledge.close()
        with writer_lock(self.root), self.db:
            prepared = []
            for file in files:
                try:
                    prepared.append(self._ingest(file, source_id, base_revision, max_file_bytes))
                except (OSError, ValueError) as exc:
                    prepared.append({'source_id': None, 'revision': None, 'sha256': None, 'media': None, 'name': Path(file).name, 'state': 'failed', 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
            payload_hash = digest({'items': prepared, 'config': config})
            key = idempotency_key or payload_hash
            existing = self.db.execute('SELECT id,payload_hash FROM job WHERE idempotency_key=?', (key,)).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    raise ValueError('idempotency_conflict')
                return existing['id']
            job_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO job(id,idempotency_key,payload_hash,config,state) VALUES (?,?,?,?,?)', (job_id, key, payload_hash, canonical(config).decode(), 'queued'))
            for row in prepared:
                self.db.execute('INSERT INTO item(id,job_id,source_id,revision,sha256,media,name,state,error) VALUES (?,?,?,?,?,?,?,?,?)',
                                (uuid.uuid4().hex, job_id, *[row[k] for k in ('source_id', 'revision', 'sha256', 'media', 'name', 'state', 'error')]))
            return job_id

    def list_jobs(self, *, include_deleted=False, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError('page limit must be between 1 and 500')
        where = '' if include_deleted else 'WHERE deleted_at IS NULL'
        return [dict(row) for row in self.db.execute(
            f"SELECT id,state,deleted_at,deleted_by,json_extract(config,'$.model_ref') AS model_ref, (SELECT error FROM item WHERE job_id=job.id AND error IS NOT NULL LIMIT 1) AS error FROM job {where} ORDER BY rowid DESC LIMIT ?", (limit,)
        )]

    def inspect(self, job_id, *, limit=100, cursor=None, include_deleted=False):
        job = self.db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
        if not job or (job['deleted_at'] is not None and not include_deleted):
            raise ValueError('unknown_job')
        counts = {r['state']: r['n'] for r in self.db.execute('SELECT state,count(*) n FROM item WHERE job_id=? GROUP BY state', (job_id,))}
        if not 1 <= limit <= 500:
            raise ValueError('page limit must be between 1 and 500')
        if cursor and not self.db.execute('SELECT 1 FROM item WHERE job_id=? AND id=?', (job_id, cursor)).fetchone():
            raise ValueError('cursor does not belong to job')
        page = list(self.db.execute('SELECT id,source_id,revision,name,state,error,attempt,report FROM item WHERE job_id=? AND id>? ORDER BY id LIMIT ?', (job_id, cursor or '', limit + 1)))
        items = []
        for row in page[:limit]:
            value = dict(row)
            value['report'] = json.loads(value['report']) if value['report'] else None
            total, complete = self.db.execute(
                'SELECT count(*),sum(candidates IS NOT NULL) FROM span WHERE item_id=?', (row['id'],)
            ).fetchone()
            candidate_counts = {'entities': 0, 'assertions': 0, 'schema_gaps': 0}
            for ordinal, encoded in self.db.execute('SELECT ordinal,candidates FROM span WHERE item_id=? AND candidates IS NOT NULL', (row['id'],)):
                candidate = self.effective_candidates(row['id'], ordinal, encoded)
                candidate_counts['entities'] += len(candidate.get('entities', []))
                candidate_counts['assertions'] += len(candidate.get('assertions', []))
                candidate_counts['schema_gaps'] += sum(q.get('code') == 'schema_gap' for q in candidate.get('questions', []))
            nonempty = candidate_counts['entities'] + candidate_counts['assertions'] > 0
            glean_failed = bool(self.db.execute("SELECT 1 FROM extraction_pass p WHERE item_id=? AND pass=1 AND state='failed' AND NOT EXISTS (SELECT 1 FROM glean_retry g WHERE g.item_id=p.item_id AND g.ordinal=p.ordinal AND g.state='succeeded') LIMIT 1", (row['id'],)).fetchone())
            model_requests = self.db.execute('SELECT count(*) FROM extraction_pass WHERE item_id=?', (row['id'],)).fetchone()[0] + self.db.execute('SELECT count(*) FROM glean_retry WHERE item_id=?', (row['id'],)).fetchone()[0]
            glean_budget = sum(self._gleanable(json.loads(r[0])) for r in self.db.execute('SELECT payload FROM span WHERE item_id=?', (row['id'],)))
            value['progress'] = {
                'completed_spans': complete or 0,
                'total_spans': total or 0,
                'draft_assertions': self.db.execute(
                    'SELECT count(*) FROM draft_assertion WHERE item_id=?', (row['id'],)
                ).fetchone()[0],
                **candidate_counts,
                'model_requests_recorded': model_requests,
                'gleaning_retries': [dict(r) for r in self.db.execute('SELECT ordinal,attempt,state,error FROM glean_retry WHERE item_id=? ORDER BY ordinal,attempt', (row['id'],))],
                'request_budget': {'initial': total or 0, 'gleaning': glean_budget,
                                   'hard_max_with_one_correction_per_span': 2 * (total or 0) + glean_budget},
                'outcome': ('partial_review' if glean_failed else 'complete' if nonempty else 'complete_empty') if value['state'] == 'succeeded' else 'incomplete',
                'reviewable': value['state'] == 'succeeded' and (complete or 0) == (total or 0) and nonempty,
            }
            items.append(value)
        return {'job_id': job_id, 'model_ref': json.loads(job['config']).get('model_ref'), 'state': job['state'], 'counts': counts, 'cancel_requested': bool(job['cancel_requested']),
                'deleted_at': job['deleted_at'], 'deleted_by': job['deleted_by'],
                'items': items, 'next_cursor': items[-1]['id'] if len(page) > limit else None}

    def soft_delete(self, job_id, *, actor):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.db.execute('SELECT state,deleted_at FROM job WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise ValueError('unknown_job')
            if job['deleted_at'] is not None:
                return self.inspect(job_id, include_deleted=True)
            active = self.db.execute(
                "SELECT 1 FROM item WHERE job_id=? AND state IN ('queued','running') LIMIT 1", (job_id,)
            ).fetchone()
            if job['state'] in ('queued', 'running') or active:
                raise ValueError('active_job_must_be_cancelled_before_delete')
            if self.db.execute("SELECT 1 FROM glean_retry g JOIN item i ON i.id=g.item_id WHERE i.job_id=? AND g.state='running'", (job_id,)).fetchone():
                raise ValueError('active_job_must_be_cancelled_before_delete')
            self.db.execute('UPDATE job SET deleted_at=?,deleted_by=? WHERE id=?', (time.time(), actor, job_id))
        return self.inspect(job_id, include_deleted=True)

    def restore(self, job_id, *, actor):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.db.execute('SELECT deleted_at FROM job WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise ValueError('unknown_job')
            if job['deleted_at'] is not None:
                self.db.execute('UPDATE job SET deleted_at=NULL,deleted_by=NULL WHERE id=?', (job_id,))
        return self.inspect(job_id)

    def request_retry(self, job_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.db.execute('SELECT state,deleted_at FROM job WHERE id=?', (job_id,)).fetchone()
            if not job or job['deleted_at'] is not None:
                raise ValueError('unknown_job')
            if job['state'] in ('queued', 'running'):
                raise ValueError('job_already_active')
            self.db.execute("UPDATE job SET state='queued',cancel_requested=0 WHERE id=?", (job_id,))
        return {'job_id': job_id, 'state': 'queued'}

    def effective_candidates(self, item_id, ordinal, encoded):
        candidate = json.loads(encoded)
        row = self.db.execute("SELECT candidates FROM glean_retry WHERE item_id=? AND ordinal=? AND state='succeeded' ORDER BY attempt DESC LIMIT 1", (item_id, ordinal)).fetchone()
        if row:
            candidate['questions'] = [q for q in candidate['questions'] if q.get('code') != 'gleaning_incomplete']
            candidate = self._merge_candidates(candidate, json.loads(row[0]))
        return candidate

    def retry_failed_gleaning(self, job_id, *, runtime_config=None):
        self.inspect(job_id)
        job = self.db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
        config = {**json.loads(job['config']), **(runtime_config or {})}
        if job['state'] != 'succeeded' or config['mode'] != 'model':
            raise ValueError('completed_model_job_required')
        knowledge = LocalKnowledge(Path(config['knowledge_root']))
        try:
            if knowledge.manifest['bundle_digest'] != config['bundle_digest']:
                raise ValueError('public_bundle_changed')
            for item in self.db.execute("SELECT * FROM item WHERE job_id=? AND state='succeeded'", (job_id,)).fetchall():
                source = safe_file(self.root, 'sources/objects/' + item['sha256'][:2] + '/' + item['sha256'])
                if file_digest(source) != item['sha256']:
                    raise ValueError('source_hash_mismatch')
                rows = self.db.execute('SELECT * FROM span WHERE item_id=? ORDER BY ordinal', (item['id'],)).fetchall()
                budget = 2 * len(rows) + sum(self._gleanable(json.loads(r['payload'])) for r in rows)
                for row in rows:
                    if self._cancelled(job_id):
                        raise InterruptedError('cancelled')
                    with self.db:
                        self.db.execute('BEGIN IMMEDIATE')
                        current_job = self.db.execute('SELECT deleted_at,cancel_requested FROM job WHERE id=?', (job_id,)).fetchone()
                        if current_job['deleted_at'] is not None or current_job['cancel_requested']:
                            raise ValueError('job_deleted_or_cancelled')
                        failed = self.db.execute("SELECT 1 FROM extraction_pass p WHERE item_id=? AND ordinal=? AND pass=1 AND state='failed' AND NOT EXISTS (SELECT 1 FROM extraction_pass s WHERE s.item_id=p.item_id AND s.ordinal=p.ordinal AND s.pass=1 AND s.state='succeeded')", (item['id'], row['ordinal'])).fetchone()
                        done = self.db.execute("SELECT 1 FROM glean_retry WHERE item_id=? AND ordinal=? AND state IN ('running','succeeded')", (item['id'], row['ordinal'])).fetchone()
                        if not failed or done:
                            continue
                        used = self.db.execute('SELECT count(*) FROM extraction_pass WHERE item_id=?', (item['id'],)).fetchone()[0] + self.db.execute('SELECT count(*) FROM glean_retry WHERE item_id=?', (item['id'],)).fetchone()[0]
                        if used >= budget:
                            raise ValueError('model_request_budget_exhausted')
                        attempt = self.db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM glean_retry WHERE item_id=? AND ordinal=?', (item['id'], row['ordinal'])).fetchone()[0]
                        self.db.execute("INSERT INTO glean_retry VALUES(?,?,?,'running',NULL,NULL)", (item['id'], row['ordinal'], attempt))
                    try:
                        span = json.loads(row['payload'])
                        saved = json.loads(row['candidates'])
                        declared = self._declared_entities(saved)
                        raw = model_candidates(span, knowledge, config, correction={'kind': 'failed_gleaning_retry', 'declared_entities': declared, 'instruction': 'Return only missing supported facts. Assertion endpoints may reference the declared entity keys. Do not redefine them. Zero additions is valid.'})
                        extra = self._normalize_gleaning(raw, saved, span, knowledge, self.namespace + '|' + item['source_id'])
                        with self.db:
                            self.db.execute("UPDATE glean_retry SET state='succeeded',candidates=? WHERE item_id=? AND ordinal=? AND attempt=?", (canonical(extra).decode(), item['id'], row['ordinal'], attempt))
                    except Exception as exc:
                        with self.db:
                            self.db.execute("UPDATE glean_retry SET state='failed',error=? WHERE item_id=? AND ordinal=? AND attempt=?", (str(exc)[:200], item['id'], row['ordinal'], attempt))
        finally:
            knowledge.close()
        return self.inspect(job_id)

    def cancel(self, job_id):
        self.inspect(job_id)
        with self.db:
            self.db.execute("UPDATE job SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE id=?", (job_id,))
            self.db.execute("UPDATE item SET state='cancelled',error='cancelled' WHERE job_id=? AND state='queued'", (job_id,))
        return self.inspect(job_id)

    def _cancelled(self, job_id):
        return bool(self.db.execute('SELECT cancel_requested FROM job WHERE id=?', (job_id,)).fetchone()[0])

    def _parse(self, item):
        source = safe_file(self.root, 'sources/objects/' + item['sha256'][:2] + '/' + item['sha256'])
        if file_digest(source) != item['sha256']:
            raise ValueError('source_hash_mismatch')
        if self.db.execute('SELECT 1 FROM span WHERE item_id=? LIMIT 1', (item['id'],)).fetchone():
            return
        folder = Path(tempfile.mkdtemp(dir=self.root / 'staging'))
        try:
            output = folder / 'spans.jsonl'
            env = dict(os.environ)
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
            result = subprocess.run([sys.executable, '-m', 'tcode_agent.ontology_core.document_parser', str(source), str(output), item['media']],
                                    env=env, capture_output=True, text=True, timeout=60)
            if result.returncode:
                # Parser messages are controlled and never include document contents.
                raise ValueError('parse_failed:' + result.stderr.strip()[-160:])
            report = json.loads(Path(str(output) + '.report.json').read_text())
            with self.db:
                previous = None
                for ordinal, span in enumerate(jsonl(output)):
                    span['span_id'] = 'span:' + digest({'source': item['source_id'], 'revision': item['revision'], 'locator': span['locator'], 'text': span['text_sha256']})
                    span['source_id'], span['source_revision'] = item['source_id'], item['revision']
                    span['object_sha256'], span['source_ordinal'] = item['sha256'], ordinal
                    window = adjacent_window(span, previous)
                    self.db.execute('INSERT INTO span VALUES (?,?,?,NULL)', (item['id'], ordinal, canonical(window).decode()))
                    previous = span
                self.db.execute('UPDATE item SET report=? WHERE id=?', (canonical(report).decode(), item['id']))
        finally:
            shutil.rmtree(folder)

    @staticmethod
    def reviewable_candidates(candidate):
        """Preserve distinct evidence and definitions in legacy duplicate-key drafts."""
        entities, endpoints, seen = [], {}, set()
        questions = list(candidate['questions'])
        for entity in candidate['entities']:
            fingerprint = digest(entity)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            original = entity['key']
            key = original if not any(row['key'] == original for row in entities) else original + ':' + fingerprint[:16]
            entities.append({**entity, 'key': key})
            endpoints.setdefault((original, entity.get('entity_id')), key)
            definitions = {(row['type'], row['label'], row.get('identity_scope'), canonical(row.get('external_ids', {})), canonical(row.get('aliases', [])), canonical(row.get('mutations', [])))
                           for row in candidate['entities'] if row['key'] == original}
            if len(definitions) > 1:
                questions.append({'code': 'duplicate_entity_key_conflict', 'entity_key': original,
                                  'message': '不同实体定义分别保留，需核对身份；所有原文证据均保留。'})
        assertions = []
        for assertion in candidate['assertions']:
            row = dict(assertion)
            for side in ('subject', 'object'):
                endpoint = endpoints.get((row[side + '_key'], row.get(side + '_id')))
                if endpoint is None:
                    raise ValueError('merged_assertion_endpoint_ambiguous')
                row[side + '_key'] = endpoint
            assertions.append(row)
        return {**candidate, 'entities': entities, 'assertions': assertions,
                'questions': list({digest(row): row for row in questions}.values())}

    @staticmethod
    def _declared_entities(saved):
        return [{k: e[k] for k in ENTITY_FIELDS if k in e}
                for e in DraftIntake.reviewable_candidates(saved)['entities']]

    @staticmethod
    def _gleaning_context(saved):
        """Coverage is based on accepted drafts, never a rejected model response."""
        checked = DraftIntake.reviewable_candidates(saved)
        fields = ('subject_key', 'predicate', 'object_key', 'polarity', 'kind', 'qualifiers', 'quote')
        return {'entities': DraftIntake._declared_entities(checked),
                'assertions': [{k: row[k] for k in fields} for row in checked['assertions']],
                'schema_gaps': []}

    @staticmethod
    def _normalize_gleaning(raw, saved, span, knowledge, namespace, *, quarantine_invalid_relations=False):
        # Reuse only server-validated definitions from this very source span.
        declared = DraftIntake._declared_entities(saved)
        prior_by_key = {e['key']: e for e in declared}
        conflicts = []
        for entity in raw['entities']:
            prior = prior_by_key.get(entity['key'])
            if prior and any(entity.get(k, {} if k == 'external_ids' else [] if k in ('aliases', 'mutations') else None) != prior.get(k, {} if k == 'external_ids' else [] if k in ('aliases', 'mutations') else None)
                             for k in ('type', 'label', 'external_ids', 'identity_scope', 'aliases', 'mutations')):
                if not quarantine_invalid_relations:
                    raise ValueError('declared_endpoint_identity_changed')
                conflicts.append(entity)
        conflicting_keys = {e['key'] for e in conflicts}
        rejected = [a for a in raw['assertions'] if {a['subject_key'], a['object_key']} & conflicting_keys]
        raw = {**raw, 'entities': [e for e in raw['entities'] if e['key'] not in conflicting_keys],
               'assertions': [a for a in raw['assertions'] if not {a['subject_key'], a['object_key']} & conflicting_keys]}
        keys = {e['key'] for e in raw['entities']}
        combined = {**raw, 'entities': [*raw['entities'], *(e for e in declared if e['key'] not in keys)]}
        checked = normalize_candidates(combined, span, knowledge, namespace, quarantine_invalid_relations=quarantine_invalid_relations)
        checked['questions'].extend({'code': 'invalid_entity_quarantined', 'reason': 'declared_endpoint_identity_changed',
                                     'raw_entity': e} for e in conflicts)
        checked['questions'].extend({'code': 'invalid_relation_quarantined', 'reason': 'redefined_endpoint',
                                     'raw_assertion': a} for a in rejected)
        checked['rejected_assertions'].extend(rejected)
        return checked

    @staticmethod
    def _merge_candidates(first, second):
        """Union independently validated passes; never replace a fact because text is longer."""
        entities = {digest(row): row for row in first['entities']}
        assertions = {digest(row): row for row in first['assertions']}
        added_entities = sum(digest(row) not in entities for row in second['entities'])
        added_assertions = sum(digest(row) not in assertions for row in second['assertions'])
        entities.update({digest(row): row for row in second['entities']})
        assertions.update({digest(row): row for row in second['assertions']})
        return DraftIntake.reviewable_candidates({
            'entities': list(entities.values()),
            'assertions': list(assertions.values()),
            'questions': list({digest(row): row for row in [*first['questions'], *second['questions']]}.values()),
            'rejected_assertions': list({digest(row): row for row in [*first['rejected_assertions'], *second['rejected_assertions']]}.values()),
            'status': 'draft', 'warning': first['warning'],
            'gleaning': {'passes': 2, 'added_entities': added_entities,
                         'added_assertions': added_assertions, 'stopped': 'bounded_one_pass'},
        })

    @staticmethod
    def _resolve_deferred_references(saved, span, knowledge, namespace):
        """Late declarations may resolve a rejected pronoun, never a named owner."""
        declared = DraftIntake._declared_entities(saved)
        known = {e['key'] for e in declared}
        conflicting = {q['entity_key'] for q in saved['questions'] if q['code'] == 'duplicate_entity_key_conflict'}
        pending = {}
        for q in saved['questions']:
            if q.get('reason') != 'referential_route_antecedent_required' or 'raw_entity' not in q:
                continue
            e = {k: v for k, v in q['raw_entity'].items() if k in ENTITY_FIELDS}
            if e['key'] not in known:
                pending.setdefault(e['key'], {})[digest(e)] = e
        pending = {key: next(iter(rows.values())) for key, rows in pending.items() if len(rows) == 1}
        claims = [a for a in saved['rejected_assertions']
                  if a['subject_key'] in pending and a['object_key'] in known and a['object_key'] not in conflicting
                  and a['predicate'] == 'has_observation']
        if not claims:
            return saved
        checked = normalize_candidates({'entities': [*declared, *pending.values()], 'assertions': claims, 'schema_gaps': []},
                                       span, knowledge, namespace, quarantine_invalid_relations=True)
        if not checked['assertions']:
            return saved
        merged = DraftIntake._merge_candidates(saved, checked)
        merged['questions'].append({'code': 'resolved_deferred_references', 'count': len(checked['assertions']),
                                    'message': '后续具名实体已使原先的代词主张通过唯一先行对象及原文校验；原拒绝记录保留。'})
        return merged

    @staticmethod
    def _gleanable(span):
        lines = [line.strip() for line in span['text'].splitlines() if line.strip()]
        references = str(span.get('locator', {}).get('heading') or '').lower() == 'references'
        bibliography = bool(lines) and sum(bool(__import__('re').match(r'^\d+\s*[.)]', line)) for line in lines) / len(lines) > .5
        return not references and not bibliography

    def _extract(self, item, knowledge, config, extractor, attempt):
        rows = list(self.db.execute('SELECT ordinal,payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item['id'],)))
        glean_budget = sum(self._gleanable(json.loads(row['payload'])) for row in rows)
        request_budget = 2 * len(rows) + glean_budget

        def reserve(ordinal, pass_number):
            with self.db:
                used = self.db.execute('SELECT count(*) FROM extraction_pass WHERE item_id=?', (item['id'],)).fetchone()[0]
                if used >= request_budget:
                    raise ValueError('model_request_budget_exhausted')
                self.db.execute('INSERT OR REPLACE INTO extraction_pass VALUES (?,?,?,?,?,?,?)',
                                (item['id'], attempt, ordinal, pass_number, 'running', None, None))

        for index, span_row in enumerate(rows):
            if self._cancelled(item['job_id']):
                raise InterruptedError('cancelled')
            if span_row['candidates'] is not None:
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO span_attempt VALUES (?,?,?,?,?)', (item['id'], attempt, span_row['ordinal'], 'reused', None))
                continue
            span = json.loads(span_row['payload'])
            span['context_before'] = json.loads(rows[index - 1]['payload'])['text'][-800:] if index else ''
            span['context_after'] = json.loads(rows[index + 1]['payload'])['text'][:800] if index + 1 < len(rows) else ''
            # Bounded identity hints from earlier validated spans of this file only.
            # They are context, never evidence for activity, mutations or measurements.
            hints = {}
            for prior in rows[max(0, index - 24):index]:
                current = self.db.execute('SELECT candidates FROM span WHERE item_id=? AND ordinal=?', (item['id'], prior['ordinal'])).fetchone()
                if not current or not current[0]:
                    continue
                for entity in json.loads(current[0])['entities']:
                    if entity['type'] == 'EnzymeVariant':
                        hints[entity['label']] = {'label': entity['label'], 'type': entity['type'],
                                                  'aliases': entity.get('aliases', [])[:10]}
            span['entity_context'] = list(hints.values())[-24:]
            namespace = self.namespace + '|' + item['source_id']
            raw = None
            try:
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO span_attempt VALUES (?,?,?,?,?)', (item['id'], attempt, span_row['ordinal'], 'running', None))
                if extractor:
                    raw = extractor(span, knowledge, config)
                elif config['mode'] == 'mentions':
                    raw = knowledge.mentions(span['text'])
                else:
                    reserve(span_row['ordinal'], 0)
                    raw = model_candidates(span, knowledge, config)
                candidate = normalize_candidates(raw, span, knowledge, namespace)
                if not extractor and config['mode'] == 'model':
                    with self.db:
                        self.db.execute('UPDATE extraction_pass SET state=?,result_digest=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=0', ('succeeded', digest(candidate), item['id'], attempt, span_row['ordinal']))
            except ValueError as exc:
                if extractor or config['mode'] != 'model':
                    with self.db:
                        self.db.execute('UPDATE span_attempt SET state=?,error=? WHERE item_id=? AND attempt=? AND ordinal=?', ('failed', str(exc)[:200], item['id'], attempt, span_row['ordinal']))
                    raise
                if self._cancelled(item['job_id']):
                    raise InterruptedError('cancelled')
                # Keep independently valid facts when correcting another item in the batch.
                retained = None
                if raw is not None:
                    try:
                        retained = normalize_candidates(raw, span, knowledge, namespace, quarantine_invalid_relations=True)
                    except ValueError:
                        pass  # Invalid root/evidence cannot be salvaged.
                # One bounded model correction; never relax validation or repair evidence in code.
                try:
                    with self.db:
                        self.db.execute('UPDATE extraction_pass SET state=?,error=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=0', ('failed', str(exc)[:200], item['id'], attempt, span_row['ordinal']))
                    reserve(span_row['ordinal'], 2)
                    raw = model_candidates(span, knowledge, config, correction={'previous_output': getattr(exc, 'model_output', raw), 'validation_error': str(exc),
                        'validation_issues': retained['questions'] if retained is not None else [],
                        'instruction': 'Return one compact complete corrected object. Check ALL validation_issues, not only the first validation_error. validated_reference_candidates are source-checked antecedents; correct the endpoint only with that evidence, otherwise leave unresolved. validated_strategy_candidates are source-proven strategy names: declare a NEW Pathway key for those outcomes, never keep the contradicted named endpoint or redefine it. Keep independently valid facts. Copy textual values and detail fields literally from source lines, including typographic spelling; do not paraphrase them. Use a directly source-named subject or the explicit reference protocol for observations. Check all line ranges and constraints. Omit unsupported candidates; use schema_gaps only for important facts the schema cannot express.'})
                    candidate = normalize_candidates(raw, span, knowledge, namespace, quarantine_invalid_relations=True)
                    if retained is not None:
                        candidate = self._merge_candidates(retained, candidate)
                    candidate['model_correction_attempts'] = 1
                    with self.db:
                        self.db.execute('UPDATE extraction_pass SET state=?,result_digest=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=2', ('succeeded', digest(candidate), item['id'], attempt, span_row['ordinal']))
                except ValueError as repair:
                    with self.db:
                        self.db.execute('UPDATE span_attempt SET state=?,error=? WHERE item_id=? AND attempt=? AND ordinal=?', ('failed', str(repair)[:200], item['id'], attempt, span_row['ordinal']))
                        self.db.execute('UPDATE extraction_pass SET state=?,error=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=2', ('failed', str(repair)[:200], item['id'], attempt, span_row['ordinal']))
                    raise
            if not extractor and config['mode'] == 'model' and self._gleanable(span):
                try:
                    reserve(span_row['ordinal'], 1)
                    extra_raw = model_candidates(span, knowledge, config, correction={
                        'kind': 'neutral_gleaning', 'previous_output': self._gleaning_context(candidate),
                        'rejected_candidates': [q for q in candidate['questions'] if q.get('code') in {'invalid_relation_quarantined', 'invalid_entity_quarantined'}],
                        'declared_entities': self._declared_entities(candidate),
                        'uncovered_source_quotes': list(dict.fromkeys(q['quote'] for q in candidate['questions'] if q.get('code') == 'schema_gap')),
                        'instruction': 'Audit source coverage before returning additions: explicit negative/detection findings for every named tested subject; cofactor dependence and engineered-construct naming; full-route outcomes with source-named inputs/products and alternate units; current-versus-historical route comparisons with separate comparator fields. has_observation can represent negative, procedural and quantitative findings; do not claim missing schema for these. uncovered_source_quotes are source passages with unresolved coverage, not covered facts. Recheck them against the actual schema: an explicit total product count needs no individual product names or per-product yields. For aggregate counts and route comparisons, include procedure_strategy copied from the exact source noun phrase naming the whole strategy; do not invent a combined display name. Keep any genuinely unresolved gap. previous_output contains only validated drafts. Rejected candidates are not covered facts; correct them only with source evidence. validated_reference_candidates provide a unique source-checked antecedent when available; do not retain a contradicted endpoint. validated_strategy_candidates name a source-proven whole strategy: declare a NEW Pathway key, never redefine a different validated route. You may add a fully qualified observation for an incomplete finding. Where stated, retain method/structure_id, role/reference_state, operation/participants/after_phase/timing, and background separately from structure_template; copy literal source wording. For observation binding, a paraphrased entity label needs an exact-source alias naming the SAME entity, never another module or a family. An explicitly selected anonymous individual needs its own unresolved Protein/EnzymeVariant with the exact source selector as label; its unknown sequence is not a schema gap. Attach separate measurements to that key, not the family, a later mutant or an observation. For an explicit single-construct engineering/naming narrative, check whether the named construct member_of the stated EnzymeFamily annotation is missing; use unspecified evidence and the complete naming passage, never lineage inheritance. Preserve scientific uncertainty. For a direct qualitative activity statement, check supports_capability with a CatalyticCapability named by the exact source activity phrase. Select the sentence naming the actual construct; never transfer an evolved capability to the parent or bypass an ambiguous observation reference. Reuse declared endpoint keys without re-declaring or changing aliases. Return only additional supported facts or genuine unresolved gaps. Zero additions is valid. Do not repeat validated complete items, redefine existing entities or infer measurements from context.'})
                    extra = self._normalize_gleaning(extra_raw, candidate, span, knowledge, namespace, quarantine_invalid_relations=True)
                    candidate = self._merge_candidates(candidate, extra)
                    with self.db:
                        self.db.execute('UPDATE extraction_pass SET state=?,result_digest=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=1', ('succeeded', digest(extra), item['id'], attempt, span_row['ordinal']))
                except ValueError as glean_error:
                    candidate['questions'].append({'code': 'gleaning_incomplete', 'message': str(glean_error)[:200]})
                    with self.db:
                        self.db.execute('UPDATE extraction_pass SET state=?,error=? WHERE item_id=? AND attempt=? AND ordinal=? AND pass=1', ('failed', str(glean_error)[:200], item['id'], attempt, span_row['ordinal']))
            candidate = self._resolve_deferred_references(candidate, span, knowledge, namespace)
            with self.db:
                for position, assertion in enumerate(candidate['assertions']):
                    qualifiers = canonical(assertion['qualifiers']).decode()
                    for other in self.db.execute('SELECT ordinal,position,qualifiers FROM draft_assertion WHERE item_id=? AND subject=? AND predicate=? AND object=? AND polarity<>?', (item['id'], assertion['subject_id'], assertion['predicate'], assertion['object_id'], assertion['polarity'])):
                        candidate['questions'].append({'code': 'possible_conflict' if other['qualifiers'] == qualifiers else 'condition_difference', 'candidate': position, 'other_span_ordinal': other['ordinal'], 'other_position': other['position']})
                    self.db.execute('INSERT INTO draft_assertion VALUES (?,?,?,?,?,?,?,?)', (item['id'], span_row['ordinal'], position, assertion['subject_id'], assertion['predicate'], assertion['object_id'], assertion['polarity'], qualifiers))
                self.db.execute('UPDATE span SET candidates=? WHERE item_id=? AND ordinal=?', (canonical(candidate).decode(), item['id'], span_row['ordinal']))
                self.db.execute('UPDATE span_attempt SET state=? WHERE item_id=? AND attempt=? AND ordinal=?', ('succeeded', item['id'], attempt, span_row['ordinal']))

    def _export(self, item, config):
        folder = self.root / 'changesets' / item['id']
        folder.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=folder)
        counts = {'entities': 0, 'assertions': 0, 'questions': 0, 'rejected_assertions': 0}
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.root):
            raise ValueError('unsafe export path')
        try:
            with os.fdopen(fd, 'wb') as out:
                for span in self.db.execute('SELECT payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item['id'],)):
                    if span['candidates'] is None:
                        raise ValueError('incomplete_candidates')
                    value = json.loads(span['candidates'])
                    for key in counts:
                        counts[key] += len(value[key])
                    out.write(canonical({'span': json.loads(span['payload']), 'candidates': value}) + b'\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, folder / 'candidates.jsonl')
            op_fd, op_tmp = tempfile.mkstemp(dir=folder)
            try:
                with os.fdopen(op_fd, 'wb') as operations:
                    for record in jsonl(folder / 'candidates.jsonl'):
                        span_id = record['span']['span_id']
                        for entity in record['candidates']['entities']:
                            op = {'op': 'reference_entity' if entity['identity_state'] == 'matched' else 'propose_entity',
                                  'item_id': digest({'span': span_id, 'entity': entity}), 'review_state': 'draft', 'payload': entity}
                            operations.write(canonical(op) + b'\n')
                        for assertion in record['candidates']['assertions']:
                            op = {'op': 'propose_assertion', 'item_id': digest({'source': item['source_id'], 'revision': item['revision'], 'assertion': assertion}),
                                  'review_state': 'draft', 'payload': assertion}
                            operations.write(canonical(op) + b'\n')
                    operations.flush()
                    os.fsync(operations.fileno())
                os.replace(op_tmp, folder / 'operations.jsonl')
            finally:
                if os.path.exists(op_tmp):
                    os.unlink(op_tmp)
            write_json(folder / 'change-set.json', {'schema_version': 'draft-1', 'state': 'draft', 'job_id': item['job_id'], 'item_id': item['id'],
                       'source_id': item['source_id'], 'source_revision': item['revision'], 'object_sha256': item['sha256'], 'scope_binding': self.namespace,
                       'operations_file': 'operations.jsonl', 'operations_sha256': file_digest(folder / 'operations.jsonl'),
                       'candidate_file': 'candidates.jsonl', 'candidate_sha256': file_digest(folder / 'candidates.jsonl'), 'counts': counts,
                       'explicit_change_list_required': item['revision'] > 1,
                       # Runtime transport and credentials never belong in an exported draft.
                       'config': json.loads(self.db.execute('SELECT config FROM job WHERE id=?', (item['job_id'],)).fetchone()[0]),
                       'notes': ['No publication. No automatic withdrawal from text omissions.', 'mentions mode extracts names only; model mode requires scientific review.']})
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def run(self, job_id, *, retry=False, extractor=None, runtime_config=None):
        self.inspect(job_id)
        with writer_lock(self.root):
            job = self.db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
            if job['state'] == 'succeeded' or (job['cancel_requested'] and not retry):
                return self.inspect(job_id)
            config = json.loads(job['config'])
            if runtime_config:
                config.update(runtime_config)
            knowledge = LocalKnowledge(Path(config['knowledge_root']))
            try:
                if config['parser_version'] != PARSER_VERSION or config['prompt_version'] != PROMPT_VERSION:
                    raise ValueError('extractor_version_changed: submit a new job')
                if knowledge.manifest['bundle_digest'] != config['bundle_digest']:
                    raise ValueError('knowledge_version_changed: submit a new job')
                with self.db:
                    if retry:
                        self.db.execute('UPDATE job SET cancel_requested=0 WHERE id=?', (job_id,))
                        self.db.execute("UPDATE item SET state='queued',error=NULL WHERE job_id=? AND state IN ('failed','cancelled') AND sha256 IS NOT NULL", (job_id,))
                    self.db.execute("UPDATE item SET state='queued' WHERE job_id=? AND state='running'", (job_id,))
                    self.db.execute("UPDATE job SET state='running' WHERE id=?", (job_id,))
                for item in self.db.execute("SELECT * FROM item WHERE job_id=? AND state='queued' ORDER BY id", (job_id,)):
                    if self._cancelled(job_id):
                        break
                    with self.db:
                        self.db.execute("UPDATE item SET state='running',attempt=attempt+1 WHERE id=?", (item['id'],))
                        attempt = self.db.execute('SELECT attempt FROM item WHERE id=?', (item['id'],)).fetchone()[0]
                        versions = canonical({'parser': config['parser_version'], 'prompt': config['prompt_version'], 'model': config.get('model'), 'bundle': config['bundle_digest']}).decode()
                        self.db.execute('INSERT INTO extraction_attempt VALUES (?,?,?,?,?,?,?)', (item['id'], attempt, 'running', None, time.time(), None, versions))
                    try:
                        self._parse(item)
                        self._extract(item, knowledge, config, extractor, attempt)
                        if self._cancelled(job_id):
                            raise InterruptedError('cancelled')
                        self._export(item, config)
                        with self.db:
                            self.db.execute("UPDATE item SET state='succeeded',error=NULL WHERE id=?", (item['id'],))
                            self.db.execute('UPDATE extraction_attempt SET state=?,finished_at=? WHERE item_id=? AND attempt=?', ('succeeded', time.time(), item['id'], attempt))
                    except Exception as exc:
                        state = 'cancelled' if isinstance(exc, InterruptedError) else 'failed'
                        # Provider exceptions can contain request text; only controlled ValueErrors are disclosed.
                        error = str(exc)[:200] if isinstance(exc, (ValueError, InterruptedError, ModelRequestError)) else type(exc).__name__
                        with self.db:
                            self.db.execute('UPDATE item SET state=?,error=? WHERE id=?', (state, error, item['id']))
                            self.db.execute('UPDATE extraction_attempt SET state=?,error=?,finished_at=? WHERE item_id=? AND attempt=?', (state, error, time.time(), item['id'], attempt))
                counts = self.inspect(job_id)['counts']
                state = 'cancelled' if self._cancelled(job_id) else ('succeeded' if set(counts) == {'succeeded'} else 'partial' if counts.get('succeeded') else 'failed')
                with self.db:
                    self.db.execute('UPDATE job SET state=? WHERE id=?', (state, job_id))
                return self.inspect(job_id)
            finally:
                knowledge.close()
