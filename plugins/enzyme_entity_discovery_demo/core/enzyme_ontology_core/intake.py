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
import uuid
from pathlib import Path

from .bundle import canonical, digest, file_digest, jsonl, safe_file, sync_dir, write_json, writer_lock
from .document_parser import PARSER_VERSION
from .extraction import LocalKnowledge, PROMPT_VERSION, model_candidates, normalize_candidates


# ponytail: one server process; serialize schema/WAL bootstrap only, not queries.
INITIALIZE_LOCK = threading.RLock()


DDL = '''
CREATE TABLE IF NOT EXISTS scope(scope TEXT NOT NULL,workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_revision(source_id TEXT,revision INTEGER,sha256 TEXT NOT NULL,media TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE IF NOT EXISTS job(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,payload_hash TEXT NOT NULL,config TEXT NOT NULL,state TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS item(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES job(id),source_id TEXT,revision INTEGER,sha256 TEXT,media TEXT,name TEXT,state TEXT NOT NULL,error TEXT,attempt INTEGER NOT NULL DEFAULT 0,report TEXT,FOREIGN KEY(source_id,revision) REFERENCES source_revision(source_id,revision));
CREATE INDEX IF NOT EXISTS item_job ON item(job_id,state,id);
CREATE TABLE IF NOT EXISTS span(item_id TEXT REFERENCES item(id),ordinal INTEGER,payload TEXT NOT NULL,candidates TEXT,PRIMARY KEY(item_id,ordinal));
CREATE TABLE IF NOT EXISTS draft_assertion(item_id TEXT REFERENCES item(id),ordinal INTEGER,position INTEGER,subject TEXT,predicate TEXT,object TEXT,polarity TEXT,qualifiers TEXT,PRIMARY KEY(item_id,ordinal,position));
CREATE INDEX IF NOT EXISTS draft_conflict ON draft_assertion(item_id,subject,predicate,object,polarity);
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

    def submit(self, files, *, knowledge_root, mode='mentions', model=None, base_url='http://127.0.0.1:11434/v1', idempotency_key=None, source_id=None, base_revision=None, max_file_bytes=100 * 1024 * 1024, allowed_model_endpoints=None, model_timeout_seconds=60, model_reasoning_effort=None):
        knowledge_root = Path(knowledge_root).resolve()
        if self.root == knowledge_root or self.root.is_relative_to(knowledge_root) or knowledge_root.is_relative_to(self.root):
            raise ValueError('draft and published data roots must be separate')
        if max_file_bytes <= 0 or not files or (source_id and len(files) != 1) or mode not in ('mentions', 'model') or (mode == 'model' and not model):
            raise ValueError('invalid intake configuration')
        knowledge = LocalKnowledge(knowledge_root)
        config = {'knowledge_root': str(knowledge_root), 'bundle_digest': knowledge.manifest['bundle_digest'],
                  'mode': mode, 'model': model, 'base_url': base_url, 'allowed_model_endpoints': list(allowed_model_endpoints or []), 'model_timeout_seconds': model_timeout_seconds, 'model_reasoning_effort': model_reasoning_effort, 'parser_version': PARSER_VERSION, 'prompt_version': PROMPT_VERSION}
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

    def inspect(self, job_id, *, limit=100, cursor=None):
        job = self.db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
        if not job:
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
            items.append(value)
        return {'job_id': job_id, 'state': job['state'], 'counts': counts, 'cancel_requested': bool(job['cancel_requested']),
                'items': items, 'next_cursor': items[-1]['id'] if len(page) > limit else None}

    def cancel(self, job_id):
        self.inspect(job_id)
        with self.db:
            self.db.execute("UPDATE job SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE id=?", (job_id,))
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
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
            result = subprocess.run([sys.executable, '-m', 'enzyme_ontology_core.document_parser', str(source), str(output), item['media']],
                                    env=env, capture_output=True, text=True, timeout=60)
            if result.returncode:
                # Parser messages are controlled and never include document contents.
                raise ValueError('parse_failed:' + result.stderr.strip()[-160:])
            report = json.loads(Path(str(output) + '.report.json').read_text())
            with self.db:
                for ordinal, span in enumerate(jsonl(output)):
                    span['span_id'] = 'span:' + digest({'source': item['source_id'], 'revision': item['revision'], 'locator': span['locator'], 'text': span['text_sha256']})
                    span['source_id'], span['source_revision'] = item['source_id'], item['revision']
                    self.db.execute('INSERT INTO span VALUES (?,?,?,NULL)', (item['id'], ordinal, canonical(span).decode()))
                self.db.execute('UPDATE item SET report=? WHERE id=?', (canonical(report).decode(), item['id']))
        finally:
            shutil.rmtree(folder)

    def _extract(self, item, knowledge, config, extractor):
        for span_row in self.db.execute('SELECT ordinal,payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item['id'],)):
            if self._cancelled(item['job_id']):
                raise InterruptedError('cancelled')
            if span_row['candidates'] is not None:
                continue
            span = json.loads(span_row['payload'])
            namespace = self.namespace + '|' + item['source_id']
            raw = None
            try:
                if extractor:
                    raw = extractor(span, knowledge, config)
                elif config['mode'] == 'mentions':
                    raw = knowledge.mentions(span['text'])
                else:
                    raw = model_candidates(span, knowledge, config)
                candidate = normalize_candidates(raw, span, knowledge, namespace)
            except ValueError as exc:
                if extractor or config['mode'] != 'model':
                    raise
                if self._cancelled(item['job_id']):
                    raise InterruptedError('cancelled')
                # One bounded model correction; never relax validation or repair evidence in code.
                raw = model_candidates(span, knowledge, config, correction={'previous_output': getattr(exc, 'model_output', raw), 'validation_error': str(exc),
                    'instruction': 'Return a complete corrected object. Check ALL quotes and domain/range constraints. Omit unsupported candidates; do not invent evidence.'})
                candidate = normalize_candidates(raw, span, knowledge, namespace, quarantine_invalid_relations=True)
                candidate['model_correction_attempts'] = 1
            with self.db:
                for position, assertion in enumerate(candidate['assertions']):
                    qualifiers = canonical(assertion['qualifiers']).decode()
                    for other in self.db.execute('SELECT ordinal,position,qualifiers FROM draft_assertion WHERE item_id=? AND subject=? AND predicate=? AND object=? AND polarity<>?', (item['id'], assertion['subject_id'], assertion['predicate'], assertion['object_id'], assertion['polarity'])):
                        candidate['questions'].append({'code': 'possible_conflict' if other['qualifiers'] == qualifiers else 'condition_difference', 'candidate': position, 'other_span_ordinal': other['ordinal'], 'other_position': other['position']})
                    self.db.execute('INSERT INTO draft_assertion VALUES (?,?,?,?,?,?,?,?)', (item['id'], span_row['ordinal'], position, assertion['subject_id'], assertion['predicate'], assertion['object_id'], assertion['polarity'], qualifiers))
                self.db.execute('UPDATE span SET candidates=? WHERE item_id=? AND ordinal=?', (canonical(candidate).decode(), item['id'], span_row['ordinal']))

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
                       'explicit_change_list_required': item['revision'] > 1, 'config': config,
                       'notes': ['No publication. No automatic withdrawal from text omissions.', 'mentions mode extracts names only; model mode requires scientific review.']})
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def run(self, job_id, *, retry=False, extractor=None):
        self.inspect(job_id)
        with writer_lock(self.root):
            job = self.db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
            if job['state'] == 'succeeded' or (job['cancel_requested'] and not retry):
                return self.inspect(job_id)
            config = json.loads(job['config'])
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
                    try:
                        self._parse(item)
                        self._extract(item, knowledge, config, extractor)
                        if self._cancelled(job_id):
                            raise InterruptedError('cancelled')
                        self._export(item, config)
                        with self.db:
                            self.db.execute("UPDATE item SET state='succeeded',error=NULL WHERE id=?", (item['id'],))
                    except Exception as exc:
                        state = 'cancelled' if isinstance(exc, InterruptedError) else 'failed'
                        # Provider exceptions can contain request text; only controlled ValueErrors are disclosed.
                        error = str(exc)[:200] if isinstance(exc, (ValueError, InterruptedError)) else type(exc).__name__
                        with self.db:
                            self.db.execute('UPDATE item SET state=?,error=? WHERE id=?', (state, error, item['id']))
                counts = self.inspect(job_id)['counts']
                state = 'cancelled' if self._cancelled(job_id) else ('succeeded' if set(counts) == {'succeeded'} else 'partial' if counts.get('succeeded') else 'failed')
                with self.db:
                    self.db.execute('UPDATE job SET state=? WHERE id=?', (state, job_id))
                return self.inspect(job_id)
            finally:
                knowledge.close()
