"""Private review/publication. Only authenticated host handlers may call mutations."""
from __future__ import annotations
from .scientific import ENTITY_FIELDS
import json
import math
import hashlib
import sqlite3
import time
import re
from pathlib import Path

from .bundle import canonical, digest, file_digest, safe_file, writer_lock
from .extraction import LocalKnowledge, normalize_candidates
from .intake import DraftIntake, INITIALIZE_LOCK
from .sqlite_store import normalize_term
from .support import assess_claim, validate_claims
from .conflicts import ConflictGovernance
from .conditions import compare_conditions


class ConflictError(ValueError):
    pass


DDL = '''
CREATE TABLE IF NOT EXISTS meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision INTEGER NOT NULL,epoch INTEGER NOT NULL,workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review(item_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,decision TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_revision(item_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(item_id,revision));
CREATE TABLE IF NOT EXISTS entity(id TEXT PRIMARY KEY,type TEXT NOT NULL,label TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alias(term TEXT,entity_id TEXT REFERENCES entity(id),PRIMARY KEY(term,entity_id));
CREATE TABLE IF NOT EXISTS assertion(id TEXT PRIMARY KEY,subject TEXT REFERENCES entity(id),predicate TEXT,object TEXT REFERENCES entity(id),source_id TEXT,source_revision INTEGER,active INTEGER NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS assertion_subject ON assertion(subject,predicate,active,id);
CREATE INDEX IF NOT EXISTS assertion_object ON assertion(object,predicate,active,id);
CREATE INDEX IF NOT EXISTS assertion_source ON assertion(source_id,active,id);
CREATE TABLE IF NOT EXISTS publication(item_id TEXT PRIMARY KEY REFERENCES review(item_id),review_revision INTEGER NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_batch(item_id TEXT,review_revision INTEGER,result TEXT NOT NULL,selection TEXT NOT NULL,mapping TEXT NOT NULL,PRIMARY KEY(item_id,review_revision));
CREATE TABLE IF NOT EXISTS review_history(item_id TEXT,revision INTEGER,payload TEXT NOT NULL,PRIMARY KEY(item_id,revision));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS report(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflict_decision(case_id TEXT NOT NULL,version INTEGER NOT NULL,left_id TEXT NOT NULL,right_id TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(case_id,version));
CREATE INDEX IF NOT EXISTS conflict_decision_left ON conflict_decision(left_id,case_id);
CREATE INDEX IF NOT EXISTS conflict_decision_right ON conflict_decision(right_id,case_id);
CREATE TABLE IF NOT EXISTS report_check(report_id TEXT PRIMARY KEY REFERENCES report(id),payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_revision(version INTEGER PRIMARY KEY,parent_version INTEGER NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,created_at REAL NOT NULL);
'''


class PrivateKnowledge(ConflictGovernance):
    def __init__(self, root, workspace_id, knowledge_root):
        self.intake = DraftIntake(root, workspace_id=workspace_id)
        self.root = self.intake.root
        self.public = LocalKnowledge(Path(knowledge_root))
        path = self.root / 'private.sqlite'
        if path.is_symlink():
            self.close()
            raise ValueError('unsafe private database')
        with INITIALIZE_LOCK:
            self.db = sqlite3.connect(path, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript(DDL)
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO meta VALUES(1,0,0,?)', (workspace_id,))
                self.db.execute("INSERT OR IGNORE INTO publication_batch SELECT p.item_id,p.review_revision,p.result,json_extract(r.payload,'$.selected_ids'),'{}' FROM publication p JOIN review r ON r.item_id=p.item_id")
                for legacy in self.db.execute("SELECT b.item_id,b.review_revision,r.payload FROM publication_batch b JOIN review r ON r.item_id=b.item_id WHERE b.mapping='{}'").fetchall():
                    saved = json.loads(legacy['payload'])
                    mapping = {}
                    for op in saved.get('review', {}).get('operations', []):
                        if op['kind'] == 'entity' and op['id'] in saved['selected_ids']:
                            proposed = op['payload']['entity_id']
                            target = saved['identity_choices'].get(proposed, proposed)
                            mapping[proposed] = 'private:' + digest({'workspace': workspace_id, 'draft': target}) if target.startswith('draft:') else target
                    self.db.execute('UPDATE publication_batch SET mapping=? WHERE item_id=? AND review_revision=?', (canonical(mapping).decode(), legacy['item_id'], legacy['review_revision']))
            if self.db.execute('SELECT workspace_id FROM meta WHERE singleton=1').fetchone()[0] != workspace_id:
                self.close()
                raise ValueError('private database scope mismatch')
        self.workspace_id = workspace_id

    def close(self):
        for obj in (getattr(self, 'db', None), getattr(self, 'public', None), getattr(self, 'intake', None)):
            if obj is not None:
                obj.close()

    def _audit(self, actor, action, payload):
        if not actor:
            raise ValueError('authenticated actor required')
        self.db.execute('INSERT INTO audit(actor,action,payload,created_at) VALUES(?,?,?,?)', (actor, action, canonical(payload).decode(), time.time()))

    def revisions(self):
        row = self.db.execute('SELECT revision,epoch FROM meta WHERE singleton=1').fetchone()
        schema = self.db.execute('SELECT COALESCE(MAX(version),0) FROM schema_revision').fetchone()[0]
        return {'private_revision': row['revision'], 'revocation_epoch': row['epoch'], 'workspace_schema_version': schema,
                'public_release_id': self.public.manifest.get('release_id', self.public.manifest['bundle_digest'])}

    def schema(self, version=None):
        """Return the immutable system schema plus one workspace schema snapshot."""
        if version is None:
            row = self.db.execute('SELECT version,payload FROM schema_revision ORDER BY version DESC LIMIT 1').fetchone()
        else:
            row = self.db.execute('SELECT version,payload FROM schema_revision WHERE version=?', (version,)).fetchone()
            if version and row is None:
                raise ValueError('workspace schema version not found')
        private = json.loads(row['payload']) if row else {'classes': [], 'predicates': []}
        return {'system': self.public.ontology, 'workspace': private, 'workspace_schema_version': row['version'] if row else 0,
                'public_release_id': self.public.manifest.get('release_id', self.public.manifest['bundle_digest'])}

    def revise_schema(self, *, actor, expected_version, classes, predicates):
        """Append a workspace-only schema snapshot; system definitions cannot be shadowed."""
        if not actor or not isinstance(classes, list) or not isinstance(predicates, list) or len(classes) > 200 or len(predicates) > 500:
            raise ValueError('invalid workspace schema')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.schema()
            if current['workspace_schema_version'] != expected_version:
                raise ConflictError('workspace schema version changed')
            identifier = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$')
            system_classes = {row['id'] for row in current['system']['classes']}
            system_predicates = {row['id'] for row in current['system']['predicates']}
            class_ids, predicate_ids = set(), set()
            normalized_classes = []
            for row in classes:
                if not isinstance(row, dict) or set(row) - {'id', 'label', 'description'} or not identifier.fullmatch(str(row.get('id', ''))):
                    raise ValueError('invalid workspace class definition')
                if row['id'] in system_classes or row['id'] in class_ids:
                    raise ValueError('workspace class cannot shadow or duplicate a definition')
                class_ids.add(row['id'])
                normalized_classes.append({k: str(row[k]).strip() for k in ('id', 'label', 'description') if str(row.get(k, '')).strip()})
            known_classes = system_classes | class_ids
            normalized_predicates = []
            for row in predicates:
                if (not isinstance(row, dict) or set(row) - {'id', 'label', 'description', 'domains', 'ranges'}
                        or not identifier.fullmatch(str(row.get('id', ''))) or not isinstance(row.get('domains'), list)
                        or not isinstance(row.get('ranges'), list) or not row['domains'] or not row['ranges']):
                    raise ValueError('invalid workspace predicate definition')
                domains, ranges = list(dict.fromkeys(row['domains'])), list(dict.fromkeys(row['ranges']))
                if row['id'] in system_predicates or row['id'] in predicate_ids or not set(domains + ranges) <= known_classes:
                    raise ValueError('workspace predicate cannot shadow definitions and must reference known classes')
                predicate_ids.add(row['id'])
                normalized_predicates.append({**{k: str(row[k]).strip() for k in ('id', 'label', 'description') if str(row.get(k, '')).strip()},
                                              'domains': domains, 'ranges': ranges})
            payload = {'classes': normalized_classes, 'predicates': normalized_predicates}
            version = expected_version + 1
            self.db.execute('INSERT INTO schema_revision VALUES(?,?,?,?,?)', (version, expected_version, canonical(payload).decode(), actor, time.time()))
            self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            self._audit(actor, 'schema.revise', {'workspace_schema_version': version, 'schema_digest': digest(payload)})
            return self.schema(version)

    def create_facts(self, *, actor, expected_schema_version, entities, assertions, source_id='workspace-manual'):
        """Create workspace facts validated against one immutable schema snapshot."""
        if not actor or not isinstance(entities, list) or not isinstance(assertions, list) or not 1 <= len(entities) <= 200 or len(assertions) > 500:
            raise ValueError('invalid workspace facts')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            schema = self.schema(expected_schema_version)
            if schema['workspace_schema_version'] != self.schema()['workspace_schema_version']:
                raise ConflictError('create facts against the current workspace schema')
            types = {row['id'] for row in schema['system']['classes']} | {row['id'] for row in schema['workspace']['classes']}
            predicates = {row['id']: row for row in [*schema['system']['predicates'], *schema['workspace']['predicates']]}
            by_key, ids = {}, []
            for row in entities:
                if (not isinstance(row, dict) or set(row) - {'key', 'type', 'label'} or not str(row.get('key', '')).strip()
                        or not str(row.get('label', '')).strip() or row.get('type') not in types or row['key'] in by_key):
                    raise ValueError('invalid workspace entity')
                entity_id = 'private:' + digest({'workspace': self.workspace_id, 'key': row['key']})
                payload = {'entity_id': entity_id, 'entity_type': row['type'], 'label': row['label'], 'identity_scope': 'workspace_private',
                           'schema_version': expected_schema_version}
                existing = self.db.execute('SELECT type,label FROM entity WHERE id=?', (entity_id,)).fetchone()
                if existing and (existing['type'] != row['type'] or existing['label'] != row['label']):
                    raise ConflictError('workspace entity key already has a different definition')
                self.db.execute('INSERT OR IGNORE INTO entity VALUES(?,?,?,?)', (entity_id, row['type'], row['label'], canonical(payload).decode()))
                self.db.execute('INSERT OR IGNORE INTO alias VALUES(?,?)', (normalize_term(row['label']), entity_id))
                by_key[row['key']] = payload
                ids.append(entity_id)
            assertion_ids = []
            schema_digest = digest(schema['workspace'])
            for index, row in enumerate(assertions):
                if not isinstance(row, dict) or set(row) - {'subject_key', 'predicate', 'object_key', 'polarity', 'kind', 'qualifiers', 'evidence'}:
                    raise ValueError('invalid workspace assertion')
                subject, obj, predicate = by_key.get(row.get('subject_key')), by_key.get(row.get('object_key')), predicates.get(row.get('predicate'))
                if not subject or not obj or not predicate or subject['entity_type'] not in predicate['domains'] or obj['entity_type'] not in predicate['ranges']:
                    raise ValueError('workspace assertion violates schema domain/range')
                if row.get('polarity') not in ('supports', 'opposes') or row.get('kind') not in ('reported', 'annotation', 'analogy', 'hypothesis') or not str(row.get('evidence', '')).strip():
                    raise ValueError('workspace assertion requires polarity, kind, and evidence')
                assertion_id = 'private-assertion:' + digest({'workspace': self.workspace_id, 'schema': expected_schema_version, 'source': source_id, 'index': index, 'row': row})
                payload = {'assertion_id': assertion_id, 'subject_id': subject['entity_id'], 'predicate': row['predicate'], 'object_id': obj['entity_id'],
                           'polarity': row['polarity'], 'kind': row['kind'], 'qualifiers': row.get('qualifiers') or {}, 'quote': row['evidence'],
                           'source_id': source_id, 'source_revision': 1, 'object_sha256': hashlib.sha256(row['evidence'].encode()).hexdigest(),
                           'locator': {'kind': 'workspace_manual'}, 'review_state': 'accepted', 'conflict_state': 'none', 'reviewer': actor,
                           'revision': 1, 'schema_version': expected_schema_version, 'schema_digest': schema_digest}
                self.db.execute('INSERT OR IGNORE INTO assertion VALUES(?,?,?,?,?,?,1,1,?)',
                                (assertion_id, subject['entity_id'], row['predicate'], obj['entity_id'], source_id, 1, canonical(payload).decode()))
                assertion_ids.append(assertion_id)
            self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            result = {'entity_ids': ids, 'assertion_ids': assertion_ids, 'workspace_schema_version': expected_schema_version, 'schema_digest': schema_digest}
            self._audit(actor, 'facts.create', result)
            return {**result, **self.revisions()}

    def review(self, item_id):
        item = self.intake.db.execute("SELECT * FROM item WHERE id=? AND state='succeeded'", (item_id,)).fetchone()
        if not item:
            raise ValueError('completed draft not found')
        if not any(
            json.loads(row[0]).get('entities') or json.loads(row[0]).get('assertions')
            for row in self.intake.db.execute('SELECT candidates FROM span WHERE item_id=? AND candidates IS NOT NULL', (item_id,))
        ):
            raise ValueError('completed extraction contains no reviewable candidates')
        config = json.loads(self.intake.db.execute('SELECT config FROM job WHERE id=?', (item['job_id'],)).fetchone()[0])
        if not self._public_binding(config)['compatible']:
            raise ConflictError('public knowledge changed; re-extract or rebase the draft')
        source = safe_file(self.root, 'sources/objects/' + item['sha256'][:2] + '/' + item['sha256'])
        if file_digest(source) != item['sha256']:
            raise ValueError('source hash mismatch')
        revision_row = self.db.execute('SELECT payload FROM draft_revision WHERE item_id=? ORDER BY revision DESC LIMIT 1', (item_id,)).fetchone()
        overrides = json.loads(revision_row[0]) if revision_row else {}
        operations, questions = [], []
        for record in self.intake.db.execute('SELECT ordinal,payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item_id,)):
            span, saved = json.loads(record['payload']), self.intake.effective_candidates(item_id, record['ordinal'], record['candidates'])
            # Older completed drafts may contain the same validated entity key from
            # both the initial and gleaning passes. Collapse those at read time so
            # the immutable extraction ledger remains untouched and reviewable.
            saved = DraftIntake.reviewable_candidates(saved)
            raw = overrides.get(span['span_id']) or {
                'entities': [{k: e[k] for k in ENTITY_FIELDS if k in e} for e in saved['entities']],
                'assertions': [{k: a[k] for k in ('subject_key', 'object_key', 'predicate', 'polarity', 'kind', 'qualifiers', 'quote')} for a in saved['assertions']]}
            checked = normalize_candidates(raw, span, self.public, self.intake.namespace + '|' + item['source_id'], quarantine_invalid_relations=True)
            questions.extend(saved['questions'])
            questions.extend(checked['questions'])
            for entity in checked['entities']:
                operations.append({'id': digest({'span': span['span_id'], 'entity': entity}), 'kind': 'entity', 'payload': entity, 'span': span})
            for assertion in checked['assertions']:
                operations.append({'id': digest({'span': span['span_id'], 'assertion': assertion}), 'kind': 'assertion', 'payload': assertion, 'span': span})
                for (old_payload,) in self.db.execute('SELECT payload FROM assertion WHERE subject=? AND predicate=? AND object=? AND active=1', (assertion['subject_id'], assertion['predicate'], assertion['object_id'])):
                    old = json.loads(old_payload)
                    if old['polarity'] != assertion['polarity']:
                        questions.append({'code': 'possible_conflict' if old['qualifiers'] == assertion['qualifiers'] else 'condition_difference', 'existing_assertion_id': old['assertion_id']})
        questions = list({digest(q): q for q in questions}.values())
        for question in questions:
            existing_id = question.get('existing_assertion_id')
            if existing_id:
                old = self.db.execute('SELECT payload FROM assertion WHERE id=?', (existing_id,)).fetchone() or self.public.db.execute('SELECT payload FROM assertion WHERE id=?', (existing_id,)).fetchone()
                if old:
                    fact = json.loads(old[0])
                    question['existing_fact'] = fact
                    question['existing_evidence'] = [json.loads(r[0]) for ev in fact.get('evidence_ids', []) for r in self.public.db.execute('SELECT payload FROM evidence WHERE id=?', (ev,))]
        published_ids = {op for record in self.db.execute('SELECT selection FROM publication_batch WHERE item_id=?', (item_id,)) for op in json.loads(record[0])}
        for op in operations:
            op['published'] = op['id'] in published_ids
        payload = {'item_id': item_id, 'source_id': item['source_id'], 'source_revision': item['revision'], 'object_sha256': item['sha256'], 'operations': operations, 'questions': questions}
        row = self.db.execute('SELECT revision,decision FROM review WHERE item_id=?', (item_id,)).fetchone()
        return {**payload, 'payload_hash': digest(payload), 'review_revision': row['revision'] if row else 0,
                'decision': row['decision'] if row else 'needs_review', 'published': bool(operations) and all(op['published'] for op in operations), 'pending_count': sum(not op['published'] for op in operations)}

    def _public_binding(self, config):
        current = self.public.manifest.get('logical_digest') or self.public.manifest['bundle_digest']
        saved = config.get('logical_digest')
        # Legacy jobs predate logical digests. Recover the digest only from the
        # immutable release whose bundle hash they recorded; never infer it from
        # a path, version label, or the current manifest.
        if not saved:
            root = Path(str(config.get('knowledge_root') or ''))
            manifests = [root / 'entity_manifest.json', *root.glob('public/releases/*/manifest.json')] if root.is_dir() else []
            for path in manifests:
                try:
                    manifest = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if manifest.get('bundle_digest') == config.get('bundle_digest') and manifest.get('logical_digest'):
                    saved = manifest['logical_digest']
                    break
        return {'compatible': saved == current, 'saved_logical_digest': saved,
                'current_logical_digest': current,
                'current_release_id': self.public.manifest.get('release_id', self.public.manifest['bundle_digest'])}

    def rebase(self, item_id, *, actor='', apply=False):
        """Preview or apply a content-safe draft binding update.

        Candidate text and immutable extraction rows are never rewritten. Every
        effective candidate (including human draft revisions) is normalized
        against the current public schema before the binding may be updated.
        """
        item = self.intake.db.execute("SELECT * FROM item WHERE id=? AND state='succeeded'", (item_id,)).fetchone()
        if not item:
            raise ValueError('completed draft not found')
        job = self.intake.db.execute('SELECT config FROM job WHERE id=?', (item['job_id'],)).fetchone()
        config = json.loads(job['config'])
        binding = self._public_binding(config)
        revision_row = self.db.execute('SELECT payload FROM draft_revision WHERE item_id=? ORDER BY revision DESC LIMIT 1', (item_id,)).fetchone()
        overrides = json.loads(revision_row[0]) if revision_row else {}
        blockers, validated = [], 0
        for record in self.intake.db.execute('SELECT ordinal,payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item_id,)):
            span = json.loads(record['payload'])
            saved = DraftIntake.reviewable_candidates(self.intake.effective_candidates(item_id, record['ordinal'], record['candidates']))
            raw = overrides.get(span['span_id']) or {
                'entities': [{k: e[k] for k in ENTITY_FIELDS if k in e} for e in saved['entities']],
                'assertions': [{k: a[k] for k in ('subject_key', 'object_key', 'predicate', 'polarity', 'kind', 'qualifiers', 'quote')} for a in saved['assertions']]}
            try:
                normalize_candidates(raw, span, self.public, self.intake.namespace + '|' + item['source_id'])
                validated += 1
            except (KeyError, ValueError) as exc:
                blockers.append({'span_id': span['span_id'], 'locator': span.get('locator', {}), 'reason': str(exc)})
        preview = {**binding, 'item_id': item_id, 'validated_spans': validated,
                   'blockers': blockers, 'human_edits_preserved': bool(revision_row),
                   'can_apply': not blockers and bool(binding['saved_logical_digest'])}
        if not apply:
            return preview
        if not actor:
            raise ValueError('authenticated actor required')
        if blockers:
            raise ConflictError('draft has schema or identity conflicts; resolve listed blockers before rebasing')
        if not binding['saved_logical_digest']:
            raise ConflictError('original public content digest is unavailable; re-extraction required')
        with writer_lock(self.root), self.intake.db, self.db:
            current_config = json.loads(self.intake.db.execute('SELECT config FROM job WHERE id=?', (item['job_id'],)).fetchone()[0])
            current_binding = self._public_binding(current_config)
            if current_binding['compatible']:
                return {**preview, 'applied': False, 'already_current': True}
            current_config['rebased_from'] = {'bundle_digest': current_config.get('bundle_digest'),
                                               'logical_digest': current_binding['saved_logical_digest']}
            current_config['bundle_digest'] = self.public.manifest['bundle_digest']
            current_config['logical_digest'] = binding['current_logical_digest']
            current_config['knowledge_root'] = str(self.public.root)
            self.intake.db.execute('UPDATE job SET config=? WHERE id=?', (canonical(current_config).decode(), item['job_id']))
            self._audit(actor, 'draft.rebase', {'item_id': item_id, 'from': current_config['rebased_from'],
                                                'to_release_id': binding['current_release_id'], 'validated_spans': validated})
        return {**preview, 'applied': True, 'already_current': False}

    def revise(self, item_id, *, actor, expected_revision, payload_hash, edits):
        """Create an immutable draft revision and re-run schema/domain/range checks."""
        if not actor or not isinstance(edits, list) or len(edits) > 200:
            raise ValueError('invalid draft revision')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.review(item_id)
            if current['published']:
                raise ConflictError('published changesets are immutable; withdraw facts explicitly')
            if current['review_revision'] != expected_revision or current['payload_hash'] != payload_hash:
                raise ConflictError('review payload or revision changed')
            operations = {op['id']: op for op in current['operations']}
            allowed = {
                'entity': {'label', 'type', 'identity_scope', 'aliases', 'mutations'},
                'assertion': {'predicate', 'polarity', 'kind', 'qualifiers'},
            }
            removed = set()
            for edit in edits:
                if not isinstance(edit, dict) or edit.get('operation_id') not in operations:
                    raise ValueError('edit must reference a current operation')
                op = operations[edit['operation_id']]
                if op['published']:
                    raise ConflictError('published operations are immutable')
                if edit.get('remove') is True:
                    removed.add(op['id'])
                    continue
                changes = edit.get('changes')
                if not isinstance(changes, dict) or not changes or not set(changes) <= allowed[op['kind']]:
                    raise ValueError('unsupported draft fields')
                op['payload'] = {**op['payload'], **changes}
            grouped = {}
            for op in operations.values():
                if op['id'] in removed:
                    continue
                span_id = op['span']['span_id']
                raw = grouped.setdefault(span_id, {'entities': [], 'assertions': []})
                if op['kind'] == 'entity':
                    raw['entities'].append({k: op['payload'][k] for k in ENTITY_FIELDS if k in op['payload']})
                else:
                    raw['assertions'].append({k: op['payload'][k] for k in ('subject_key', 'object_key', 'predicate', 'polarity', 'kind', 'qualifiers', 'quote')})
            # Validate every span before persisting; references to removed entities fail closed.
            span_ids = {json.loads(r[0])['span_id'] for r in self.intake.db.execute('SELECT payload FROM span WHERE item_id=?', (item_id,))}
            for span_id in span_ids:
                span = json.loads(self.intake.db.execute('SELECT payload FROM span WHERE item_id=? AND json_extract(payload,\'$.span_id\')=?', (item_id, span_id)).fetchone()[0])
                normalize_candidates(grouped.get(span_id, {'entities': [], 'assertions': []}), span, self.public, self.intake.namespace + '|' + current['source_id'])
            revision = expected_revision + 1
            encoded = canonical(grouped).decode()
            self.db.execute('INSERT INTO draft_revision VALUES(?,?,?,?,?)', (item_id, revision, encoded, actor, time.time()))
            self.db.execute('INSERT INTO review VALUES(?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,payload_hash=excluded.payload_hash,decision=excluded.decision,actor=excluded.actor,payload=excluded.payload',
                            (item_id, revision, '', 'needs_review', actor, '{}'))
            self._audit(actor, 'draft.revise', {'item_id': item_id, 'review_revision': revision, 'edit_count': len(edits)})
            return self.review(item_id)

    def list_reviews(self, *, after='', limit=50):
        if not 1 <= limit <= 100:
            raise ValueError('invalid review page size')
        rows = list(self.intake.db.execute("SELECT id,name FROM item WHERE state='succeeded' AND id>? ORDER BY id LIMIT ?", (after, limit + 1)))
        result = []
        for row in rows[:limit]:
            status = self.db.execute('SELECT revision,decision FROM review WHERE item_id=?', (row['id'],)).fetchone()
            job_config = self.intake.db.execute('SELECT j.config FROM job j JOIN item i ON i.job_id=j.id WHERE i.id=?', (row['id'],)).fetchone()
            binding = self._public_binding(json.loads(job_config[0]))
            result.append({'item_id': row['id'], 'name': row['name'], 'decision': status['decision'] if status else 'needs_review',
                           'review_revision': status['revision'] if status else 0,
                           'published': self.review(row['id'])['published'] if binding['compatible'] else False,
                           'needs_rebase': not binding['compatible']})
        return {'items': result, 'next_cursor': rows[limit-1]['id'] if len(rows) > limit else None, **self.revisions()}

    def decide(self, item_id, *, actor, expected_revision, payload_hash, selected_ids, identity_choices=None, accept_conflicts=False, decision='accepted'):
        if decision not in ('accepted', 'rejected') or not actor:
            raise ValueError('invalid human decision')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            review = self.review(item_id)
            if review['published']:
                raise ConflictError('published decisions are immutable; withdraw facts explicitly')
            if review['review_revision'] != expected_revision or review['payload_hash'] != payload_hash:
                raise ConflictError('review payload or revision changed')
            choices = identity_choices or {}
            entities = {op['payload']['entity_id']: op['payload'] for op in review['operations'] if op['kind'] == 'entity'}
            for proposed, target in choices.items():
                entity = entities.get(proposed)
                if not entity or target not in [proposed, *entity['suggestions']]:
                    raise ValueError('identity choice must come from the reviewed suggestions')
            selected = set(selected_ids)
            available = {op['id'] for op in review['operations'] if not op['published']}
            if not selected <= available or (decision == 'accepted' and not selected):
                raise ValueError('select explicit operations from the current preview')
            if decision == 'accepted':
                if review['questions'] and not accept_conflicts:
                    raise ConflictError('explicit acknowledgement of unresolved questions required')
                for op in review['operations']:
                    if op['id'] in selected and op['kind'] == 'entity' and op['payload']['identity_state'] == 'needs_review' and op['payload']['entity_id'] not in choices:
                        raise ConflictError('identity choice required')
            stored = {'selected_ids': sorted(selected), 'identity_choices': choices, 'accept_conflicts': accept_conflicts, 'review': review}
            revision = expected_revision + 1
            self.db.execute('INSERT INTO review VALUES(?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,payload_hash=excluded.payload_hash,decision=excluded.decision,actor=excluded.actor,payload=excluded.payload',
                            (item_id, revision, payload_hash, decision, actor, canonical(stored).decode()))
            self.db.execute('INSERT INTO review_history VALUES(?,?,?)', (item_id, revision, canonical(stored).decode()))
            self._audit(actor, 'review.' + decision, {'item_id': item_id, 'review_revision': revision, 'payload_hash': payload_hash})
            return {'item_id': item_id, 'review_revision': revision, 'decision': decision}

    def publish(self, item_id, *, actor, expected_revision):
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute('SELECT result FROM publication_batch WHERE item_id=? AND review_revision=?', (item_id, expected_revision)).fetchone()
            if prior:
                return json.loads(prior['result'])
            item = self.intake.db.execute('SELECT job_id FROM item WHERE id=?', (item_id,)).fetchone()
            progress = next((row for row in self.intake.inspect(item[0])['items'] if row['id'] == item_id), None) if item else None
            if not progress or not progress['progress']['reviewable']:
                raise ConflictError('only a fully extracted item may be published')
            decision = self.db.execute('SELECT * FROM review WHERE item_id=?', (item_id,)).fetchone()
            if not decision or decision['revision'] != expected_revision or decision['decision'] != 'accepted':
                raise ConflictError('accepted current human decision required')
            current = self.review(item_id)
            if current['payload_hash'] != decision['payload_hash']:
                raise ConflictError('draft or conflicts changed after review')
            chosen = json.loads(decision['payload'])
            operations = [op for op in current['operations'] if op['id'] in chosen['selected_ids']]
            mapped = {}
            for batch in self.db.execute('SELECT mapping FROM publication_batch WHERE item_id=?', (item_id,)):
                mapped.update(json.loads(batch[0]))
            for op in operations:
                if op['kind'] != 'entity':
                    continue
                entity = op['payload']
                proposed = entity['entity_id']
                target = chosen['identity_choices'].get(proposed, proposed)
                if target.startswith('draft:'):
                    if target != proposed:
                        raise ValueError('cannot select another unreviewed draft identity')
                    target = 'private:' + digest({'workspace': self.workspace_id, 'draft': target})
                public_entity = self.public.db.execute('SELECT type,payload FROM entity WHERE id=?', (target,)).fetchone()
                existing = self.db.execute('SELECT type,payload FROM entity WHERE id=?', (target,)).fetchone()
                known = public_entity or existing
                if known and known[0] != entity['type']:
                    raise ValueError('identity type mismatch')
                if not known and not target.startswith('private:'):
                    raise ValueError('unknown identity selection')
                # Only the generated private identity can be new; arbitrary private IDs are rejected.
                if not known and target != 'private:' + digest({'workspace': self.workspace_id, 'draft': proposed}):
                    raise ValueError('invalid new private identity')
                mapped[proposed] = target
                prior_payload = json.loads(known[1]) if known else {}
                incoming_mutations = entity.get('mutations', [])
                if prior_payload.get('mutations') and incoming_mutations and set(prior_payload['mutations']) != set(incoming_mutations):
                    raise ConflictError('variant mutation definition differs; select a distinct identity or revise the draft')
                canonical_label = prior_payload.get('label') or entity['label']
                aliases = sorted((set(prior_payload.get('aliases', [])) | set(entity.get('aliases', [])) | {entity['label']}) - {canonical_label})
                payload = {**prior_payload, 'entity_id': target, 'entity_type': entity['type'], 'label': canonical_label,
                           'identity_scope': entity.get('identity_scope', 'unresolved'),
                           'aliases': aliases, 'mutations': incoming_mutations or prior_payload.get('mutations', [])}
                self.db.execute('INSERT INTO entity VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                                (target, entity['type'], canonical_label, canonical(payload).decode()))
                for alias in [entity['label'], *aliases]:
                    self.db.execute('INSERT OR IGNORE INTO alias VALUES(?,?)', (normalize_term(alias), target))
            ids = []
            for op in operations:
                if op['kind'] != 'assertion':
                    continue
                row = op['payload']
                if row['subject_id'] not in mapped or row['object_id'] not in mapped:
                    raise ValueError('dependent entity operations must be selected together')
                sid, oid = mapped[row['subject_id']], mapped[row['object_id']]
                mapped_conflicts, mapped_comparisons = [], []
                for db, subject, obj, active in ((self.db, 'subject', 'object', 'active=1'), (self.public.db, 'subject_id', 'object_id', "state='active'")):
                    for old in db.execute(f'SELECT id,payload FROM assertion WHERE {subject}=? AND predicate=? AND {obj}=? AND {active}', (sid, row['predicate'], oid)):
                        if json.loads(old[1])['polarity'] != row['polarity']:
                            mapped_conflicts.append(old[0])
                            mapped_comparisons.append({'assertion_id':old[0], 'condition_relation':compare_conditions(row.get('qualifiers') or {}, json.loads(old[1]).get('qualifiers') or {})})
                if mapped_conflicts and not chosen['accept_conflicts']:
                    raise ConflictError('resolved identity has opposing assertions; review their recorded conditions and acknowledge: ' + ', '.join(mapped_conflicts[:20]))
                assertion_id = 'private-assertion:' + digest({'workspace': self.workspace_id, 'source': current['source_id'], 'revision': current['source_revision'], 'op': op['id'], 'subject': sid, 'object': oid})
                payload = {**row, 'assertion_id': assertion_id, 'subject_id': sid, 'object_id': oid, 'review_state': 'accepted',
                           'conflict_state': 'open' if current['questions'] or mapped_conflicts else 'none',
                           'publication_conflict_comparisons': mapped_comparisons, 'source_id': current['source_id'], 'source_revision': current['source_revision'],
                           'object_sha256': current['object_sha256'], 'locator': op['span']['locator'], 'reviewer': decision['actor'], 'revision': 1}
                self.db.execute('INSERT OR IGNORE INTO assertion VALUES(?,?,?,?,?,?,1,1,?)', (assertion_id, sid, row['predicate'], oid, current['source_id'], current['source_revision'], canonical(payload).decode()))
                ids.append(assertion_id)
            self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            result = {'item_id': item_id, 'assertion_ids': ids, **self.revisions()}
            self.db.execute('INSERT OR IGNORE INTO publication VALUES(?,?,?)', (item_id, expected_revision, canonical(result).decode()))
            self.db.execute('INSERT INTO publication_batch VALUES(?,?,?,?,?)', (item_id, expected_revision, canonical(result).decode(), canonical(chosen['selected_ids']).decode(), canonical(mapped).decode()))
            self._audit(actor, 'publish', result)
            return result

    def withdraw(self, assertion_id, *, actor, expected_revision):
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return self._withdraw_assertion(assertion_id, actor=actor, expected_revision=expected_revision)

    def _withdraw_assertion(self, assertion_id, *, actor, expected_revision):
        if not actor:
            raise ValueError('authenticated actor required')
        row = self.db.execute('SELECT revision,active FROM assertion WHERE id=?', (assertion_id,)).fetchone()
        if not row:
            raise ValueError('private assertion not found')
        if row['revision'] != expected_revision:
            raise ConflictError('assertion revision changed')
        if row['active']:
            self.db.execute('UPDATE assertion SET active=0,revision=revision+1 WHERE id=?', (assertion_id,))
            self.db.execute('UPDATE meta SET revision=revision+1,epoch=epoch+1 WHERE singleton=1')
            self._audit(actor, 'withdraw', {'assertion_id': assertion_id})
        return self.revisions()

    def withdraw_source(self, source_id, *, actor, expected_revision):
        if not actor:
            raise ValueError('authenticated actor required')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.revisions()['private_revision'] != expected_revision:
                raise ConflictError('private revision changed; refresh before withdrawing source')
            if not self.intake.db.execute('SELECT 1 FROM source_revision WHERE source_id=?', (source_id,)).fetchone():
                raise ValueError('source not found')
            count = self.db.execute('UPDATE assertion SET active=0,revision=revision+1 WHERE source_id=? AND active=1', (source_id,)).rowcount
            if count:
                self.db.execute('UPDATE meta SET revision=revision+1,epoch=epoch+1 WHERE singleton=1')
                self._audit(actor, 'withdraw_source', {'source_id': source_id, 'assertions': count})
            return {'source_id': source_id, 'withdrawn_assertions': count, **self.revisions()}

    def purge_assertions(self, assertion_ids, *, actor, expected_revision):
        """Physically remove an exact, revision-bound set of private facts."""
        if not actor:
            raise ValueError('authenticated actor required')
        ids = list(dict.fromkeys(assertion_ids or []))
        if not ids or len(ids) > 100 or len(ids) != len(assertion_ids or []):
            raise ValueError('provide 1..100 unique private assertion IDs')
        if any(not isinstance(item, str) or not item.startswith('private-assertion:') for item in ids):
            raise ValueError('invalid private assertion ID')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.revisions()['private_revision'] != expected_revision:
                raise ConflictError('private revision changed; refresh before purging facts')
            found = {row[0] for row in self.db.execute(
                f"SELECT id FROM assertion WHERE id IN ({','.join('?' for _ in ids)})", ids
            )}
            missing = sorted(set(ids) - found)
            if missing:
                raise ConflictError('private assertions changed or missing: ' + ', '.join(missing[:20]))
            self.db.executemany('DELETE FROM assertion WHERE id=?', [(item,) for item in ids])
            self.db.execute('UPDATE meta SET revision=revision+1,epoch=epoch+1 WHERE singleton=1')
            self._audit(actor, 'purge_assertions', {'assertion_ids': sorted(ids), 'count': len(ids)})
            return {'purged_assertion_ids': sorted(ids), 'purged_assertions': len(ids), **self.revisions()}

    def _query_candidates(self, *, text, entity_id, scope):
        """Resolve identities without merging same-name entities or leaking disabled scopes."""
        term = normalize_term(text)
        candidates = {}
        stores = [(self.public.db, 'public')]
        if scope != 'public':
            stores.append((self.db, 'private'))
        match = 'selected' if entity_id else 'exact'
        modes = ['selected'] if entity_id else (['exact', 'partial', 'tokens'] if term else [])
        for mode in modes:
            for db, visibility in stores:
                if entity_id:
                    sql, args = 'SELECT id,type,label FROM entity WHERE id=?', [entity_id]
                else:
                    sql = 'SELECT DISTINCT e.id,e.type,e.label FROM alias a JOIN entity e ON e.id=a.entity_id WHERE '
                    if mode == 'exact':
                        sql += 'a.term=?'
                        args = [term]
                    elif mode == 'partial':
                        if len(term) < 3:
                            continue
                        sql += 'instr(a.term,?)>0'
                        args = [term]
                    else:
                        tokens = list(dict.fromkeys(term.split()))
                        if len(tokens) < 2 or len(tokens) > 12:
                            continue
                        sql += ' AND '.join('instr(a.term,?)>0' for _ in tokens)
                        args = tokens
                    sql += ' ORDER BY length(e.label),e.label,e.id'
                for identifier, kind, label in db.execute(sql, args):
                    if scope == 'private' and visibility == 'public':
                        if not self.db.execute('SELECT 1 FROM assertion WHERE active=1 AND (subject=? OR object=?) LIMIT 1', (identifier, identifier)).fetchone():
                            continue
                    candidates.setdefault(identifier, {'id': identifier, 'label': label, 'type': kind, 'scope': visibility})
                    if len(candidates) > 100:
                        break
            if candidates:
                match = mode
                break
        values = sorted(candidates.values(), key=lambda row: (len(row['label']), row['label'], row['id']))
        return values[:100], match if values else 'not_found', len(values) > 100

    def query(self, *, entity_id='', text='', predicate='', scope='both', limit=50, after='', evidence_basis='', polarity='', qualifiers=None):
        if scope not in ('public', 'private', 'both') or not 1 <= limit <= 100:
            raise ValueError('invalid query scope/limit')
        qualifiers = {} if qualifiers is None else qualifiers
        if evidence_basis not in ('', 'experimental', 'computational', 'literature_report', 'hypothesis', 'unspecified') or polarity not in ('', 'supports', 'opposes'):
            raise ValueError('invalid evidence_basis/polarity filter')
        if not isinstance(qualifiers, dict) or len(qualifiers) > 20:
            raise ValueError('provide at most 20 exact qualifier conditions')
        for key, value in qualifiers.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 100 or not isinstance(value, (str, int, float, bool)) or len(str(value)) > 1000:
                raise ValueError('qualifier conditions require bounded scalar values')
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('qualifier numbers must be finite')
        if len(text) > 500 or len(entity_id) > 500 or len(predicate) > 200:
            raise ValueError('query text or identifier too long')
        with self.db:
            self.db.execute('BEGIN')
            revisions = self.revisions()
            criteria = digest({'entity_id': entity_id, 'text': text, 'predicate': predicate, 'scope': scope,
                               'evidence_basis': evidence_basis, 'polarity': polarity, 'qualifiers': qualifiers})
            last = {'public': '', 'private': ''}
            if after:
                if len(after) > 8192:
                    raise ValueError('invalid cursor')
                cursor = json.loads(after)
                if not isinstance(cursor, dict) or not isinstance(cursor.get('last'), dict) or set(cursor['last']) != {'public', 'private'} or not all(isinstance(v, str) for v in cursor['last'].values()):
                    raise ValueError('invalid cursor')
                expected_version = revisions if scope != 'public' else {'public_release_id': revisions['public_release_id']}
                if cursor.get('criteria') != criteria or cursor.get('version') != expected_version:
                    raise ConflictError('cursor_stale')
                last = cursor['last']
            candidates, match_type, candidates_truncated = self._query_candidates(text=text, entity_id=entity_id, scope=scope)
            requires_selection = len(candidates) > 1 or candidates_truncated
            ids = {row['id'] for row in candidates} if not requires_selection else set()
            public_ids = {row['id'] for row in candidates if row['scope'] == 'public'} & ids
            private_ids = ids
            def retrieve(db, public, ids):
                if not ids:
                    return []
                placeholders = ','.join('?' for _ in ids)
                subject, obj = ('subject_id', 'object_id') if public else ('subject', 'object')
                active = "state='active'" if public else 'active=1'
                sql = f'SELECT id,payload FROM assertion WHERE {active} AND id>? AND ({subject} IN ({placeholders}) OR {obj} IN ({placeholders}))'
                args = [last['public' if public else 'private'], *sorted(ids), *sorted(ids)]
                if predicate:
                    sql += ' AND predicate=?'
                    args.append(predicate)
                if polarity:
                    sql += " AND json_extract(payload,'$.polarity')=?"
                    args.append(polarity)
                if evidence_basis:
                    basis_sql = "COALESCE(json_extract(payload,'$.qualifiers.evidence_basis'),json_extract(payload,'$.evidence_basis'),'unspecified')"
                    sql += f" AND CASE WHEN {basis_sql} IN ('experimental','computational','literature_report','hypothesis') THEN {basis_sql} ELSE 'unspecified' END=?"
                    args.append(evidence_basis)
                for key, value in qualifiers.items():
                    value_type = 'true' if value is True else 'false' if value is False else 'text' if isinstance(value, str) else 'number'
                    sql += " AND EXISTS (SELECT 1 FROM json_each(assertion.payload,'$.qualifiers') q WHERE q.key=? AND "
                    sql += "q.type IN ('integer','real')" if value_type == 'number' else 'q.type=?'
                    args.append(key)
                    if value_type != 'number':
                        args.append(value_type)
                    sql += ' AND q.value=?)'
                    args.append(value)
                sql += ' ORDER BY id LIMIT ?'
                args.append(limit + 1)
                return [{'id': r[0], 'scope': 'public' if public else 'private', **json.loads(r[1]), **({} if public else {'evidence_ids': [r[0].replace('private-assertion:', 'private-evidence:', 1)]})} for r in db.execute(sql, args)]
            public = retrieve(self.public.db, True, public_ids) if scope != 'private' else []
            private = retrieve(self.db, False, private_ids) if scope != 'public' else []
            selected = public[:limit] + private[:limit]
            more = len(public) > limit or len(private) > limit
            positions = {'public': public[min(limit, len(public))-1]['id'] if public else last['public'],
                         'private': private[min(limit, len(private))-1]['id'] if private else last['private']}
            next_cursor = canonical({'criteria': criteria, 'version': revisions if scope != 'public' else {'public_release_id': revisions['public_release_id']}, 'last': positions}).decode() if more else None
            for row in selected:
                basis = row.get('qualifiers', {}).get('evidence_basis', row.get('evidence_basis', 'unspecified'))
                row['resolved_evidence_basis'] = basis if basis in ('experimental', 'computational', 'literature_report', 'hypothesis') else 'unspecified'
                entity_db = self.public.db if row['scope'] == 'public' else self.db
                for side in ('subject', 'object'):
                    entity = entity_db.execute('SELECT payload FROM entity WHERE id=?', (row[side + '_id'],)).fetchone()
                    if entity is None and row['scope'] == 'private':
                        entity = self.public.db.execute('SELECT payload FROM entity WHERE id=?', (row[side + '_id'],)).fetchone()
                    if entity:
                        definition = json.loads(entity[0])
                        row[side + '_label'] = definition.get('label', row[side + '_id'])
                        row[side + '_aliases'] = definition.get('aliases', [])
                        row[side + '_mutations'] = definition.get('mutations', [])
                if row['scope'] == 'public':
                    row['evidence'] = [json.loads(r[0]) for ev in row.get('evidence_ids', []) for r in self.public.db.execute('SELECT payload FROM evidence WHERE id=?', (ev,))]
            conflicts, conflicts_truncated, conflict_cache = [], False, {}
            scopes = {'public','private'} if scope == 'both' else {scope}
            seen_pairs = set()
            for row in selected:
                analysis = self._conflict_analysis(row, scopes, conflict_cache)
                row.update({key:analysis[key] for key in ('conflict','conflict_categories','comparison_truncated')})
                conflicts_truncated = conflicts_truncated or analysis['comparison_truncated']
                for other in analysis['counterparts']:
                    pair = tuple(sorted((row['id'],other['id'])))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    conflicts.append({'left':row['id'], 'right':other['id'], 'difference':'polarity',
                                      'condition_relation':other['condition_relation'],
                                      'same_conditions':other['condition_relation']=='same_conditions',
                                      'left_scope':row['scope'], 'right_scope':other['scope']})
            return {'public': [r for r in selected if r['scope'] == 'public'], 'private': [r for r in selected if r['scope'] == 'private'],
                    'candidates': candidates, 'match_type': match_type, 'requires_selection': requires_selection, 'candidates_truncated': candidates_truncated,
                    'applied_filters': {'predicate': predicate, 'evidence_basis': evidence_basis, 'polarity': polarity, 'qualifiers': qualifiers},
                    'workspace_conflicts': conflicts, 'conflicts_truncated': conflicts_truncated, 'next_cursor': next_cursor, 'completeness': 'selection_required' if requires_selection else ('page' if next_cursor else 'complete'), **(revisions if scope != 'public' else {'public_release_id': revisions['public_release_id']})}

    def graph(self, *, text='', entity_id='', scope='both', limit=30, depth=2, ranking='coverage'):
        """Return a bounded, read-only ontology neighborhood for visualization."""
        if scope not in ('public', 'private', 'both') or not 5 <= limit <= 100 or depth not in (1, 2) or ranking not in ('coverage', 'connectivity'):
            raise ValueError('invalid graph scope/limit/depth')

        with self.db:
            self.db.execute('BEGIN')
            return self._graph_snapshot(text=text, entity_id=entity_id, scope=scope, limit=limit, depth=depth, ranking=ranking)

    def _graph_snapshot(self, *, text, entity_id, scope, limit, depth, ranking):
        stores = []
        if scope != 'private':
            stores.append((self.public.db, 'public', 'subject_id', 'object_id', "state='active'"))
        if scope != 'public':
            stores.append((self.db, 'private', 'subject', 'object', 'active=1'))

        from .graph_analysis import graph_statistics, compare_conditions
        degree, neighbors, sources = graph_statistics(stores)
        def rank(identifier):
            if ranking == 'connectivity':
                return (-degree.get(identifier, 0), identifier)
            return (-len(sources.get(identifier, ())), -len(neighbors.get(identifier, ())),
                    -degree.get(identifier, 0), identifier)
        centers = {entity_id} if entity_id else set()
        normalized = normalize_term(text)
        if normalized and not entity_id:
            for db, *_ in stores:
                centers.update(row[0] for row in db.execute('SELECT entity_id FROM alias WHERE term=?', (normalized,)))
            if not centers:
                escaped = normalized.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                for db, *_ in stores:
                    centers.update(row[0] for row in db.execute("SELECT entity_id FROM alias WHERE term LIKE ? ESCAPE '\\'", ('%' + escaped + '%',)))
        # Entities with no relations are still valid search results.
        centers = {identifier for identifier in centers if any(db.execute('SELECT 1 FROM entity WHERE id=?', (identifier,)).fetchone() for db, *_ in stores)}
        center = min(centers, key=rank, default='')
        searched = bool(text.strip() or entity_id)
        truncated = False
        distances = {}
        if center:
            distances, frontier = {center: 0}, {center}
            for level in range(1, depth + 1):
                found = set()
                marks = ','.join('?' for _ in frontier)
                if not marks:
                    break
                for db, _, subject, obj, active in stores:
                    for a, b in db.execute(f'SELECT {subject},{obj} FROM assertion WHERE {active} AND ({subject} IN ({marks}) OR {obj} IN ({marks}))', [*sorted(frontier), *sorted(frontier)]):
                        found.update((a, b))
                found.difference_update(distances)
                remaining = limit - len(distances)
                ranked = sorted(found, key=rank)
                truncated = truncated or len(ranked) > remaining
                frontier = set(ranked[:remaining])
                distances.update({identifier: level for identifier in frontier})
                if len(ranked) > remaining:
                    break  # All remaining deeper nodes rank after this omitted level.
            selected = sorted(distances, key=lambda identifier: (distances[identifier], rank(identifier)))
        elif searched:
            selected = []
        else:
            selected = sorted(degree, key=rank)[:limit]
            truncated = len(degree) > limit
        selected_ids = set(selected)
        rows = []
        edge_limit = 2000
        edges_truncated = False
        if selected:
            marks = ','.join('?' for _ in selected)
            for db, visibility, subject, obj, active in stores:
                facts = list(db.execute(f'SELECT id,payload FROM assertion WHERE {active} AND {subject} IN ({marks}) AND {obj} IN ({marks}) ORDER BY id LIMIT ?', [*selected, *selected, edge_limit + 1]))
                edges_truncated = edges_truncated or len(facts) > edge_limit
                rows.extend(self._support_fact(db, row, visibility) for row in facts[:edge_limit])

        entities = {}
        for entity in selected:
            record = self.public.db.execute('SELECT type,label,payload FROM entity WHERE id=?', (entity,)).fetchone()
            entity_scope = 'public'
            if record is None:
                record = self.db.execute('SELECT type,label,payload FROM entity WHERE id=?', (entity,)).fetchone()
                entity_scope = 'private'
            if record:
                entity_payload = json.loads(record[2])
                entities[entity] = {'id': entity, 'label': record[1], 'type': record[0], 'cluster': record[0],
                                    'scope': entity_scope, 'importance': degree.get(entity, 0),
                                    'neighbor_count': len(neighbors.get(entity, ())), 'source_count': len(sources.get(entity, ())),
                                    'distance': (distances.get(entity) if center else None),
                                    'description': entity_payload.get('description', '')}

        conflict_cache = {}
        scopes = {store[1] for store in stores}
        edges = []
        for row in rows:
            if row['subject_id'] not in selected_ids or row['object_id'] not in selected_ids:
                continue
            key = (row['subject_id'], row['predicate'], row['object_id'])
            analysis = self._conflict_analysis(row, scopes, conflict_cache)
            counterparts = [{key:other.get(key) for key in ('id','scope','polarity','qualifiers','condition_relation','source_id','quote','evidence_ids')}
                            for other in analysis['counterparts'][:20]]
            edges.append({'id': row['id'], 'source': row['subject_id'], 'target': row['object_id'],
                          'predicate': row['predicate'], 'polarity': row.get('polarity', 'supports'),
                          'scope': row['scope'], **{key:analysis[key] for key in ('conflict','conflict_categories','comparison_truncated')},
                          'counterparts': counterparts, 'qualifiers': row.get('qualifiers', {}),
                          'source_id': row.get('source_id', ''), 'source_revision': row.get('source_revision'),
                          'quote': row.get('quote', ''), 'locator': row.get('locator', {}),
                          'evidence_ids': row.get('evidence_ids', [])})
        return {'nodes': list(entities.values()), 'edges': edges, 'center_id': center,
                'scope': scope, 'limit': limit, 'depth': depth, 'ranking': ranking, 'has_private': any(row['scope'] == 'private' for row in edges),
                'truncated': truncated or edges_truncated, 'nodes_truncated': truncated, 'edges_truncated': edges_truncated,
                'status': 'not_found' if searched and not center else ('truncated' if truncated or edges_truncated else 'complete'),
                'mode': 'neighborhood' if searched else 'overview', 'edge_limit_per_scope': edge_limit, **self.revisions()}

    def read_facts(self, references, *, allowed_scopes=None):
        """Resolve a bounded set of private references in one current snapshot."""
        if not isinstance(references, list) or not 1 <= len(references) <= 100 or not all(isinstance(x, str) for x in references):
            raise ValueError('provide 1..100 private reference IDs')
        scopes = {'public','private'} if allowed_scopes is None else set(allowed_scopes)
        if 'private' not in scopes or not scopes <= {'public','private'}:
            raise PermissionError('private ontology evidence source is disabled')
        assertions, evidence, entities, conflict_cache = {}, {}, {}, {}
        with self.db:
            self.db.execute('BEGIN')
            for reference in references:
                identifier = reference.replace('private-evidence:', 'private-assertion:', 1) if reference.startswith('private-evidence:') else reference
                row = self.db.execute('SELECT payload FROM assertion WHERE id=? AND active=1', (identifier,)).fetchone()
                if not row:
                    raise ConflictError('private evidence withdrawn or inaccessible')
                fact = json.loads(row[0])
                eid = identifier.replace('private-assertion:', 'private-evidence:', 1)
                analysis = self._conflict_analysis(fact, scopes, conflict_cache)
                fact.update({key:analysis[key] for key in ('conflict','conflict_categories','comparison_truncated')})
                fact['source_conflict_state'] = fact.get('conflict_state','none')
                fact['conflict_state'] = 'open' if set(analysis['conflict_categories']) & {'same_conditions','unknown_conditions','source_dispute','unchecked'} else 'none'
                fact['conflicting_assertion_ids'] = [row['id'] for row in analysis['counterparts']]
                fact['counterparts'] = analysis['counterparts'][:20]
                locator = fact['locator']
                assertions[identifier] = {**fact, 'evidence_ids': [eid], 'epistemic_kind': fact['kind'],
                    'evidence_basis': 'private_source', 'attribution': 'unspecified', 'review_status': 'human_accepted', 'scope': 'private'}
                evidence[eid] = {'evidence_id': eid, 'wiki_ref': '', 'card_id': fact['source_id'],
                    'card_version': str(fact['source_revision']), 'card_sha256': fact['object_sha256'],
                    'heading_path': [], 'line_start': locator.get('line_start'), 'line_end': locator.get('line_end'),
                    'quote': fact['quote'], 'quote_sha256': hashlib.sha256(fact['quote'].encode()).hexdigest(),
                    'source_citations': [{'source_id': fact['source_id'], 'locator': locator}], 'source_scope': 'workspace_private',
                    'fragment_version': str(fact['source_revision']), 'snapshot_path': '', 'locator': locator,
                    'conflict_state': fact.get('conflict_state', 'none'),
                    **{key:analysis[key] for key in ('conflict','conflict_categories','comparison_truncated')}}
                for entity_id in (fact['subject_id'], fact['object_id']):
                    entity = self.public.db.execute('SELECT payload FROM entity WHERE id=?', (entity_id,)).fetchone() or self.db.execute('SELECT payload FROM entity WHERE id=?', (entity_id,)).fetchone()
                    if not entity:
                        raise ConflictError('private assertion endpoint missing')
                    entities[entity_id] = {'identity_scope': 'workspace_private', **json.loads(entity[0])}
            return {'assertions': assertions, 'evidence': evidence, 'entities': entities, **self.revisions()}

    def _support_fact(self, db, record, scope):
        fact = {**json.loads(record[1]), 'id': record[0], 'scope': scope}
        if scope == 'private':
            fact['evidence_ids'] = [record[0].replace('private-assertion:', 'private-evidence:', 1)]
        else:
            sources = [json.loads(row[0]) for reference in fact.get('evidence_ids', [])
                       for row in db.execute('SELECT payload FROM evidence WHERE id=?', (reference,))]
            usable = [source for source in sources if source.get('quote')]
            if usable:
                source = usable[0]
                fact['quote'] = source['quote']
                fact['locator'] = source.get('locator') or {key: source[key] for key in ('page', 'line_start', 'line_end', 'wiki_ref') if source.get(key) is not None}
                fact['source_id'] = source.get('card_id', source.get('wiki_ref', ''))
                fact['source_revision'] = source.get('card_version')
            fact['evidence_sources'] = sources
        return fact

    def validate_report(self, claims, *, actor=None, report_id=None, allowed_scopes=None):
        scopes = {'public', 'private'} if allowed_scopes is None else set(allowed_scopes)
        if not scopes <= {'public', 'private'}:
            raise ValueError('invalid evidence scope')
        with self.db:
            # Linearization point relative to withdrawal; no cached green assessments.
            self.db.execute('BEGIN IMMEDIATE')
            if report_id:
                saved = self.db.execute('SELECT payload FROM report WHERE id=?', (report_id,)).fetchone()
                if not saved:
                    raise ValueError('report not found')
                claims = json.loads(saved[0])
            validate_claims(claims)
            evidence = {}
            assessments = []
            conflict_cache = {}
            for index, claim in enumerate(claims):
                for reference in claim['assertion_ids']:
                    scope = 'private' if reference.startswith('private-assertion:') else 'public'
                    if scope not in scopes:
                        raise PermissionError('ontology evidence source is disabled')
                    db = self.db if scope == 'private' else self.public.db
                    active = 'active=1' if scope == 'private' else "state='active'"
                    record = db.execute(f'SELECT id,payload FROM assertion WHERE id=? AND {active}', (reference,)).fetchone()
                    if not record:
                        raise ConflictError('report contains withdrawn, unknown or inaccessible evidence')
                    evidence[reference] = self._support_fact(db, record, scope)
                related, truncated = [], False
                statement = claim.get('statement')
                if statement:
                    analysis = self._conflict_analysis(statement, scopes, conflict_cache)
                    related, truncated = analysis['counterparts'], analysis['comparison_truncated']
                assessment = assess_claim(claim, [evidence[reference] for reference in dict.fromkeys(claim['assertion_ids'])], related, truncated)
                assessments.append({'claim_index': index, **assessment})
            identifier = digest({'workspace': self.workspace_id, 'claims': claims})
            if actor:
                self.db.execute('INSERT OR IGNORE INTO report VALUES(?,?,?)', (identifier, canonical(claims).decode(), actor))
            result = {'report_id': identifier, 'claims': claims, 'reference_status': 'current',
                    'assessments': assessments, 'assessment_version': 'structured-support-v2',
                    'checked_scopes': sorted(scopes), 'conflict_scan_limit_per_scope': 100,
                    'requires_human_review': True, 'text_consistency_checked': False,
                    'delivery_status': 'ready_for_human_review' if all(row['status'] == 'supported' for row in assessments) else 'needs_review',
                    'scientific_correctness_verified': False, 'checked_at': time.time(), **self.revisions()}
            if actor:
                self._save_report_check(identifier, result)
                if report_id:
                    self._audit(actor, 'report.recheck', {'report_id':identifier,'delivery_status':result['delivery_status']})
            return result
