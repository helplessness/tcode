"""Private review/publication. Only authenticated host handlers may call mutations."""
from __future__ import annotations
import json
import hashlib
import sqlite3
import time
from pathlib import Path

from .bundle import canonical, digest, file_digest, safe_file, writer_lock
from .extraction import LocalKnowledge, normalize_candidates
from .intake import DraftIntake, INITIALIZE_LOCK
from .sqlite_store import normalize_term


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
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS report(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_by TEXT NOT NULL);
'''


class PrivateKnowledge:
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
        return {'private_revision': row['revision'], 'revocation_epoch': row['epoch'], 'public_release_id': self.public.manifest.get('release_id', self.public.manifest['bundle_digest'])}

    def review(self, item_id):
        item = self.intake.db.execute("SELECT * FROM item WHERE id=? AND state='succeeded'", (item_id,)).fetchone()
        if not item:
            raise ValueError('completed draft not found')
        config = json.loads(self.intake.db.execute('SELECT config FROM job WHERE id=?', (item['job_id'],)).fetchone()[0])
        if config['bundle_digest'] != self.public.manifest['bundle_digest']:
            raise ConflictError('public knowledge changed; re-extract or rebase the draft')
        source = safe_file(self.root, 'sources/objects/' + item['sha256'][:2] + '/' + item['sha256'])
        if file_digest(source) != item['sha256']:
            raise ValueError('source hash mismatch')
        revision_row = self.db.execute('SELECT payload FROM draft_revision WHERE item_id=? ORDER BY revision DESC LIMIT 1', (item_id,)).fetchone()
        overrides = json.loads(revision_row[0]) if revision_row else {}
        operations, questions = [], []
        for record in self.intake.db.execute('SELECT payload,candidates FROM span WHERE item_id=? ORDER BY ordinal', (item_id,)):
            span, saved = json.loads(record['payload']), json.loads(record['candidates'])
            raw = overrides.get(span['span_id']) or {
                'entities': [{k: e[k] for k in ('key', 'type', 'label', 'quote', 'external_ids', 'identity_scope') if k in e} for e in saved['entities']],
                'assertions': [{k: a[k] for k in ('subject_key', 'object_key', 'predicate', 'polarity', 'kind', 'qualifiers', 'quote')} for a in saved['assertions']]}
            checked = normalize_candidates(raw, span, self.public, self.intake.namespace + '|' + item['source_id'])
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
        payload = {'item_id': item_id, 'source_id': item['source_id'], 'source_revision': item['revision'], 'object_sha256': item['sha256'], 'operations': operations, 'questions': questions}
        row = self.db.execute('SELECT revision,decision FROM review WHERE item_id=?', (item_id,)).fetchone()
        return {**payload, 'payload_hash': digest(payload), 'review_revision': row['revision'] if row else 0,
                'decision': row['decision'] if row else 'needs_review', 'published': self.db.execute('SELECT 1 FROM publication WHERE item_id=?', (item_id,)).fetchone() is not None}

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
                'entity': {'label', 'type', 'identity_scope'},
                'assertion': {'predicate', 'polarity', 'kind', 'qualifiers'},
            }
            removed = set()
            for edit in edits:
                if not isinstance(edit, dict) or edit.get('operation_id') not in operations:
                    raise ValueError('edit must reference a current operation')
                op = operations[edit['operation_id']]
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
                    raw['entities'].append({k: op['payload'][k] for k in ('key', 'type', 'label', 'quote', 'external_ids', 'identity_scope') if k in op['payload']})
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
            result.append({'item_id': row['id'], 'name': row['name'], 'decision': status['decision'] if status else 'needs_review',
                           'review_revision': status['revision'] if status else 0, 'published': bool(self.db.execute('SELECT 1 FROM publication WHERE item_id=?', (row['id'],)).fetchone())})
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
            available = {op['id'] for op in review['operations']}
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
            self._audit(actor, 'review.' + decision, {'item_id': item_id, 'review_revision': revision, 'payload_hash': payload_hash})
            return {'item_id': item_id, 'review_revision': revision, 'decision': decision}

    def publish(self, item_id, *, actor, expected_revision):
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            decision = self.db.execute('SELECT * FROM review WHERE item_id=?', (item_id,)).fetchone()
            if not decision or decision['revision'] != expected_revision or decision['decision'] != 'accepted':
                raise ConflictError('accepted current human decision required')
            prior = self.db.execute('SELECT result FROM publication WHERE item_id=?', (item_id,)).fetchone()
            if prior:
                return json.loads(prior['result'])
            current = self.review(item_id)
            if current['payload_hash'] != decision['payload_hash']:
                raise ConflictError('draft or conflicts changed after review')
            chosen = json.loads(decision['payload'])
            operations = [op for op in current['operations'] if op['id'] in chosen['selected_ids']]
            mapped = {}
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
                payload = {'entity_id': target, 'entity_type': entity['type'], 'label': entity['label'],
                           'identity_scope': entity.get('identity_scope', 'unresolved')}
                self.db.execute('INSERT OR IGNORE INTO entity VALUES(?,?,?,?)', (target, entity['type'], entity['label'], canonical(payload).decode()))
                self.db.execute('INSERT OR IGNORE INTO alias VALUES(?,?)', (normalize_term(entity['label']), target))
            ids = []
            for op in operations:
                if op['kind'] != 'assertion':
                    continue
                row = op['payload']
                if row['subject_id'] not in mapped or row['object_id'] not in mapped:
                    raise ValueError('dependent entity operations must be selected together')
                sid, oid = mapped[row['subject_id']], mapped[row['object_id']]
                mapped_conflicts = []
                for db, subject, obj, active in ((self.db, 'subject', 'object', 'active=1'), (self.public.db, 'subject_id', 'object_id', "state='active'")):
                    for old in db.execute(f'SELECT id,payload FROM assertion WHERE {subject}=? AND predicate=? AND {obj}=? AND {active}', (sid, row['predicate'], oid)):
                        if json.loads(old[1])['polarity'] != row['polarity']:
                            mapped_conflicts.append(old[0])
                if mapped_conflicts and not chosen['accept_conflicts']:
                    raise ConflictError('resolved identity has conflicting assertions; review and acknowledge: ' + ', '.join(mapped_conflicts[:20]))
                assertion_id = 'private-assertion:' + digest({'workspace': self.workspace_id, 'source': current['source_id'], 'revision': current['source_revision'], 'op': op['id'], 'subject': sid, 'object': oid})
                payload = {**row, 'assertion_id': assertion_id, 'subject_id': sid, 'object_id': oid, 'review_state': 'accepted',
                           'conflict_state': 'open' if current['questions'] or mapped_conflicts else 'none', 'source_id': current['source_id'], 'source_revision': current['source_revision'],
                           'object_sha256': current['object_sha256'], 'locator': op['span']['locator'], 'reviewer': decision['actor'], 'revision': 1}
                self.db.execute('INSERT OR IGNORE INTO assertion VALUES(?,?,?,?,?,?,1,1,?)', (assertion_id, sid, row['predicate'], oid, current['source_id'], current['source_revision'], canonical(payload).decode()))
                ids.append(assertion_id)
            self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            result = {'item_id': item_id, 'assertion_ids': ids, **self.revisions()}
            self.db.execute('INSERT INTO publication VALUES(?,?,?)', (item_id, expected_revision, canonical(result).decode()))
            self._audit(actor, 'publish', result)
            return result

    def withdraw(self, assertion_id, *, actor, expected_revision):
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
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

    def query(self, *, entity_id='', text='', predicate='', scope='both', limit=50, after=''):
        if scope not in ('public', 'private', 'both') or not 1 <= limit <= 100:
            raise ValueError('invalid query scope/limit')
        with self.db:
            self.db.execute('BEGIN')
            revisions = self.revisions()
            criteria = digest({'entity_id': entity_id, 'text': text, 'predicate': predicate, 'scope': scope})
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
            public_ids, private_ids = {entity_id} if entity_id else set(), {entity_id} if entity_id else set()
            if text:
                public_ids.update(r[0] for r in self.public.db.execute('SELECT entity_id FROM alias WHERE term=? LIMIT 101', (normalize_term(text),)))
                private_ids.update(r[0] for r in self.db.execute('SELECT entity_id FROM alias WHERE term=? LIMIT 101', (normalize_term(text),)))
                private_ids.update(public_ids)
            if len(public_ids) > 100 or len(private_ids) > 100:
                raise ValueError('ambiguous identity; narrow query')
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
                if row['scope'] == 'public':
                    row['evidence'] = [json.loads(r[0]) for ev in row.get('evidence_ids', []) for r in self.public.db.execute('SELECT payload FROM evidence WHERE id=?', (ev,))]
            conflicts = []
            conflicts_truncated = False
            if scope != 'public':
                for row in selected:
                    stores = [(self.db, 'subject', 'object', 'active=1')]
                    if row['scope'] == 'private' and scope == 'both':
                        stores.append((self.public.db, 'subject_id', 'object_id', "state='active'"))
                    for other_db, sub, obj, active in stores:
                        opposite_rows = list(other_db.execute(f'SELECT id,payload FROM assertion WHERE {sub}=? AND predicate=? AND {obj}=? AND {active} ORDER BY id LIMIT 101', (row['subject_id'], row['predicate'], row['object_id'])))
                        conflicts_truncated = conflicts_truncated or len(opposite_rows) > 100
                        for other in opposite_rows[:100]:
                            opposite = json.loads(other[1])
                            if opposite['polarity'] != row['polarity']:
                                conflicts.append({'left': row['id'], 'right': other[0], 'difference': 'polarity', 'same_conditions': row.get('qualifiers', {}) == opposite.get('qualifiers', {})})
            return {'public': [r for r in selected if r['scope'] == 'public'], 'private': [r for r in selected if r['scope'] == 'private'],
                    'workspace_conflicts': conflicts, 'conflicts_truncated': conflicts_truncated, 'next_cursor': next_cursor, 'completeness': 'page' if next_cursor else 'complete', **(revisions if scope != 'public' else {'public_release_id': revisions['public_release_id']})}

    def graph(self, *, text='', entity_id='', scope='both', limit=30, depth=2):
        """Return a bounded, read-only ontology neighborhood for visualization."""
        if scope not in ('public', 'private', 'both') or not 5 <= limit <= 100 or depth not in (1, 2):
            raise ValueError('invalid graph scope/limit/depth')

        rows = []
        if scope != 'private':
            rows.extend({**json.loads(row[1]), 'id': row[0], 'scope': 'public'} for row in self.public.db.execute(
                "SELECT id,payload FROM assertion WHERE state='active' ORDER BY id LIMIT 5001"
            ))
        if scope != 'public':
            rows.extend({**json.loads(row[1]), 'id': row[0], 'scope': 'private'} for row in self.db.execute(
                "SELECT id,payload FROM assertion WHERE active=1 ORDER BY id LIMIT 5001"
            ))
        truncated = len(rows) > 5000
        rows = rows[:5000]

        degree = {}
        adjacency = {}
        for row in rows:
            subject, obj = row['subject_id'], row['object_id']
            degree[subject] = degree.get(subject, 0) + 1
            degree[obj] = degree.get(obj, 0) + 1
            adjacency.setdefault(subject, set()).add(obj)
            adjacency.setdefault(obj, set()).add(subject)

        centers = {entity_id} if entity_id else set()
        normalized = normalize_term(text)
        if normalized:
            pattern = '%' + normalized + '%'
            if scope != 'private':
                centers.update(row[0] for row in self.public.db.execute(
                    "SELECT entity_id FROM alias WHERE term LIKE ? ORDER BY length(term),term LIMIT 20", (pattern,)
                ))
            if scope != 'public':
                centers.update(row[0] for row in self.db.execute(
                    "SELECT entity_id FROM alias WHERE term LIKE ? ORDER BY length(term),term LIMIT 20", (pattern,)
                ))
        centers = {item for item in centers if item in degree}
        center = max(centers, key=lambda item: (degree.get(item, 0), item), default='')

        if center:
            distances, frontier = {center: 0}, {center}
            for level in range(1, depth + 1):
                found = {neighbor for item in frontier for neighbor in adjacency.get(item, ()) if neighbor not in distances}
                for item in found:
                    distances[item] = level
                frontier = found
            selected = sorted(distances, key=lambda item: (distances[item], -degree.get(item, 0), item))[:limit]
        else:
            selected = sorted(degree, key=lambda item: (-degree[item], item))[:limit]
        selected_ids = set(selected)

        entities = {}
        for entity in selected:
            record = self.public.db.execute('SELECT type,label FROM entity WHERE id=?', (entity,)).fetchone()
            entity_scope = 'public'
            if record is None:
                record = self.db.execute('SELECT type,label FROM entity WHERE id=?', (entity,)).fetchone()
                entity_scope = 'private'
            if record:
                entities[entity] = {'id': entity, 'label': record[1], 'type': record[0], 'cluster': record[0],
                                    'scope': entity_scope, 'importance': degree.get(entity, 0),
                                    'distance': (distances.get(entity) if center else None)}

        conflict_keys = {}
        for row in rows:
            key = (row['subject_id'], row['predicate'], row['object_id'])
            conflict_keys.setdefault(key, set()).add(row.get('polarity', 'supports'))
        edges = []
        for row in rows:
            if row['subject_id'] not in selected_ids or row['object_id'] not in selected_ids:
                continue
            key = (row['subject_id'], row['predicate'], row['object_id'])
            edges.append({'id': row['id'], 'source': row['subject_id'], 'target': row['object_id'],
                          'predicate': row['predicate'], 'polarity': row.get('polarity', 'supports'),
                          'scope': row['scope'], 'conflict': len(conflict_keys[key]) > 1 or row.get('conflict_state') == 'open',
                          'source_id': row.get('source_id', ''), 'source_revision': row.get('source_revision'),
                          'quote': row.get('quote', ''), 'locator': row.get('locator', {}),
                          'evidence_ids': row.get('evidence_ids', [])})
        return {'nodes': list(entities.values()), 'edges': edges, 'center_id': center,
                'scope': scope, 'limit': limit, 'depth': depth, 'has_private': any(row['scope'] == 'private' for row in edges),
                'truncated': truncated, **self.revisions()}

    def read_facts(self, references):
        """Resolve a bounded set of private references in one current snapshot."""
        if not isinstance(references, list) or not 1 <= len(references) <= 100 or not all(isinstance(x, str) for x in references):
            raise ValueError('provide 1..100 private reference IDs')
        assertions, evidence, entities = {}, {}, {}
        with self.db:
            self.db.execute('BEGIN')
            for reference in references:
                identifier = reference.replace('private-evidence:', 'private-assertion:', 1) if reference.startswith('private-evidence:') else reference
                row = self.db.execute('SELECT payload FROM assertion WHERE id=? AND active=1', (identifier,)).fetchone()
                if not row:
                    raise ConflictError('private evidence withdrawn or inaccessible')
                fact = json.loads(row[0])
                eid = identifier.replace('private-assertion:', 'private-evidence:', 1)
                conflicts = []
                for db, sub, obj, active in ((self.db, 'subject', 'object', 'active=1'), (self.public.db, 'subject_id', 'object_id', "state='active'")):
                    for old in db.execute(f'SELECT id,payload FROM assertion WHERE {sub}=? AND predicate=? AND {obj}=? AND {active} LIMIT 101', (fact['subject_id'], fact['predicate'], fact['object_id'])):
                        if json.loads(old[1])['polarity'] != fact['polarity']:
                            conflicts.append(old[0])
                fact['conflict_state'] = 'open' if conflicts or fact.get('conflict_state') == 'open' else 'none'
                fact['conflicting_assertion_ids'] = conflicts
                locator = fact['locator']
                assertions[identifier] = {**fact, 'evidence_ids': [eid], 'epistemic_kind': fact['kind'],
                    'evidence_basis': 'private_source', 'attribution': 'unspecified', 'review_status': 'human_accepted', 'scope': 'private'}
                evidence[eid] = {'evidence_id': eid, 'wiki_ref': '', 'card_id': fact['source_id'],
                    'card_version': str(fact['source_revision']), 'card_sha256': fact['object_sha256'],
                    'heading_path': [], 'line_start': locator.get('line_start'), 'line_end': locator.get('line_end'),
                    'quote': fact['quote'], 'quote_sha256': hashlib.sha256(fact['quote'].encode()).hexdigest(),
                    'source_citations': [{'source_id': fact['source_id'], 'locator': locator}], 'source_scope': 'workspace_private',
                    'fragment_version': str(fact['source_revision']), 'snapshot_path': '', 'locator': locator,
                    'conflict_state': fact.get('conflict_state', 'none')}
                for entity_id in (fact['subject_id'], fact['object_id']):
                    entity = self.public.db.execute('SELECT payload FROM entity WHERE id=?', (entity_id,)).fetchone() or self.db.execute('SELECT payload FROM entity WHERE id=?', (entity_id,)).fetchone()
                    if not entity:
                        raise ConflictError('private assertion endpoint missing')
                    entities[entity_id] = {'identity_scope': 'workspace_private', **json.loads(entity[0])}
            return {'assertions': assertions, 'evidence': evidence, 'entities': entities, **self.revisions()}

    def validate_report(self, claims, *, actor=None, report_id=None):
        with self.db:
            # Linearization point relative to withdrawal; never rely on cached report validation.
            self.db.execute('BEGIN IMMEDIATE')
            if report_id:
                saved = self.db.execute('SELECT payload FROM report WHERE id=?', (report_id,)).fetchone()
                if not saved:
                    raise ValueError('report not found')
                claims = json.loads(saved[0])
            if not claims or len(claims) > 100:
                raise ValueError('bounded claims with references required')
            for claim in claims:
                if not claim.get('text') or not claim.get('assertion_ids') or len(claim['assertion_ids']) > 20:
                    raise ValueError('each claim needs explicit evidence references')
                for reference in claim['assertion_ids']:
                    if reference.startswith('private-assertion:'):
                        row = self.db.execute('SELECT active FROM assertion WHERE id=?', (reference,)).fetchone()
                        if not row or not row[0]:
                            raise ConflictError('report contains withdrawn or inaccessible evidence')
                    elif not self.public.db.execute("SELECT 1 FROM assertion WHERE id=? AND state='active'", (reference,)).fetchone():
                        raise ConflictError('report contains unknown public evidence')
            identifier = digest({'workspace': self.workspace_id, 'claims': claims})
            if actor:
                self.db.execute('INSERT OR IGNORE INTO report VALUES(?,?,?)', (identifier, canonical(claims).decode(), actor))
            return {'report_id': identifier, 'claims': claims, 'reference_status': 'current', 'scientific_correctness_verified': False, 'checked_at': time.time(), **self.revisions()}


# SDK compatibility: the host owns its runtime implementation, while this
# historical class keeps plugin-local injection points working for old callers.
# Only the exception identity must be shared across the boundary.
from tcode_agent.ontology_core.governance import ConflictError as ConflictError  # noqa: E402,F401
