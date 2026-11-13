"""Workspace-scoped conflict comparisons and append-only human decisions.

Decisions describe a pair; they never certify a claim or erase counterevidence.
"""
import json
import time

from .bundle import canonical, digest, writer_lock
from .conditions import compare_conditions


class ConflictGovernance:
    def _conflict_analysis(self, fact, scopes, cache=None):
        key = (fact['subject_id'], fact['predicate'], fact['object_id'])
        cache = {} if cache is None else cache
        cache_key = (key, tuple(sorted(scopes)))
        if cache_key not in cache:
            candidates, truncated = [], set()
            for scope in sorted(scopes):
                db = self.db if scope == 'private' else self.public.db
                subject, obj, active = ('subject', 'object', 'active=1') if scope == 'private' else ('subject_id', 'object_id', "state='active'")
                for polarity in ('supports', 'opposes'):
                    rows = list(db.execute(f"SELECT id,payload FROM assertion WHERE {active} AND {subject}=? AND predicate=? AND {obj}=? AND json_extract(payload,'$.polarity')=? ORDER BY id LIMIT 101", (*key, polarity)))
                    if len(rows) > 100:
                        truncated.add(polarity)
                    candidates.extend(self._support_fact(db, row, scope) for row in rows[:100])
            cache[cache_key] = candidates, truncated
        candidates, truncated = cache[cache_key]
        categories, counterparts = set(), []
        for other in candidates:
            if {fact.get('polarity', 'supports'), other.get('polarity', 'supports')} != {'supports', 'opposes'}:
                continue
            relation = compare_conditions(fact.get('qualifiers') or {}, other.get('qualifiers') or {})
            categories.add(relation)
            counterparts.append({**other, 'condition_relation': relation})
        opposite = 'opposes' if fact.get('polarity', 'supports') == 'supports' else 'supports'
        incomplete = opposite in truncated
        if incomplete:
            categories.add('unchecked')
        if fact.get('conflict_state') == 'open':
            categories.add('source_dispute')
        priority = {'same_conditions': 0, 'unknown_conditions': 1, 'different_conditions': 2}
        counterparts.sort(key=lambda row: (priority[row['condition_relation']], row['scope'], row['id']))
        return {'conflict': 'same_conditions' in categories, 'conflict_categories': sorted(categories),
                'comparison_truncated': incomplete, 'counterparts': counterparts}

    def _conflict_fact(self, identifier):
        scope = 'private' if identifier.startswith('private-assertion:') else 'public'
        db = self.db if scope == 'private' else self.public.db
        row = db.execute('SELECT id,payload FROM assertion WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('conflict assertion missing or inaccessible')
        fact = self._support_fact(db, row, scope)
        state = db.execute('SELECT active,revision FROM assertion WHERE id=?' if scope == 'private' else "SELECT state,0 FROM assertion WHERE id=?", (identifier,)).fetchone()
        fact['active'] = bool(state[0]) if scope == 'private' else state[0] == 'active'
        fact['record_revision'] = state[1]
        for field in ('subject', 'object'):
            entity = self.public.db.execute('SELECT label FROM entity WHERE id=?', (fact[field + '_id'],)).fetchone() or self.db.execute('SELECT label FROM entity WHERE id=?', (fact[field + '_id'],)).fetchone()
            fact[field + '_label'] = entity[0] if entity else fact[field + '_id']
        return fact

    def _conflict_case(self, left_id, right_id):
        if any(not isinstance(value,str) or not 1 <= len(value) <= 500 for value in (left_id,right_id)):
            raise ValueError('provide bounded assertion identifiers')
        if left_id == right_id:
            raise ValueError('provide two distinct assertions')
        facts = [self._conflict_fact(identifier) for identifier in sorted((left_id, right_id))]
        left, right = facts
        if any(left.get(k) != right.get(k) for k in ('subject_id', 'predicate', 'object_id')) or {left.get('polarity'), right.get('polarity')} != {'supports', 'opposes'}:
            raise ValueError('assertions must express opposite polarities of the same relation')
        case_id = digest({'workspace': self.workspace_id, 'references': [row['id'] for row in facts]})
        snapshot = digest({'facts': facts, 'public_release': self.revisions()['public_release_id'] if any(row['scope'] == 'public' for row in facts) else None})
        events = [{**json.loads(row[0]), 'actor': row[1], 'created_at': row[2]} for row in self.db.execute('SELECT payload,actor,created_at FROM conflict_decision WHERE case_id=? ORDER BY version DESC LIMIT 51', (case_id,))]
        latest = events[0] if events else None
        relation = compare_conditions(left.get('qualifiers') or {}, right.get('qualifiers') or {})
        return {'case_id': case_id, 'facts': facts, 'snapshot': snapshot, 'version': latest['version'] if latest else 0,
                'condition_relation': relation, 'active': all(row['active'] for row in facts),
                'decision_stale': bool(latest and latest['snapshot_after'] != snapshot),
                'latest_decision': latest, 'history': events[:50], 'history_truncated': len(events) > 50}

    def _conflict_impacts(self, case, after='', limit=20):
        left, right = case['facts']
        sql = """FROM report r WHERE EXISTS (SELECT 1 FROM json_each(r.payload) c
            WHERE EXISTS (SELECT 1 FROM json_each(c.value,'$.assertion_ids') ref WHERE ref.value IN (?,?))
               OR (json_extract(c.value,'$.statement.subject_id')=? AND json_extract(c.value,'$.statement.predicate')=? AND json_extract(c.value,'$.statement.object_id')=?)) AND r.id>? ORDER BY r.id LIMIT ?"""
        rows = list(self.db.execute('SELECT r.id,r.payload ' + sql, (left['id'], right['id'], left['subject_id'], left['predicate'], left['object_id'], after, limit + 1)))
        reports = []
        versions = self.revisions()
        for row in rows[:limit]:
            check = self.db.execute('SELECT payload FROM report_check WHERE report_id=?', (row[0],)).fetchone()
            last_check = json.loads(check[0]) if check else None
            reports.append({'report_id': row[0], 'claims': json.loads(row[1]), 'last_check': last_check,
                            'needs_recheck': not last_check or last_check['knowledge_version'] != versions})
        return {'reports': reports, 'reports_truncated': len(rows) > limit,
                'next_cursor': rows[limit-1][0] if len(rows) > limit else None}

    def conflict_case(self, left_id, right_id, *, after=''):
        with self.db:
            self.db.execute('BEGIN')
            case = self._conflict_case(left_id, right_id)
            return {**case, 'impacts': self._conflict_impacts(case, after), **self.revisions()}

    def list_conflict_cases(self, *, after='', limit=30):
        if not 1 <= limit <= 100:
            raise ValueError('invalid conflict history limit')
        with self.db:
            self.db.execute('BEGIN')
            rows = list(self.db.execute('SELECT case_id,left_id,right_id FROM conflict_decision WHERE case_id>? GROUP BY case_id ORDER BY case_id LIMIT ?', (after, limit+1)))
            cases = []
            for row in rows[:limit]:
                try:
                    case = self._conflict_case(row[1], row[2])
                    cases.append({key: case[key] for key in ('case_id','facts','version','condition_relation','active','decision_stale','latest_decision')})
                except ValueError:
                    event = self.db.execute('SELECT payload,actor,created_at FROM conflict_decision WHERE case_id=? ORDER BY version DESC LIMIT 1',(row[0],)).fetchone()
                    latest = {**json.loads(event[0]),'actor':event[1],'created_at':event[2]}
                    cases.append({'case_id':row[0], 'left_id':row[1], 'right_id':row[2], 'unavailable':True, 'decision_stale':True,'latest_decision':latest})
            return {'cases':cases, 'next_cursor':rows[limit-1][0] if len(rows)>limit else None}

    def record_conflict_decision(self, *, actor, left_id, right_id, action, reason, expected_snapshot, expected_version, withdraw_id=''):
        from .governance import ConflictError
        if not actor or action not in ('different_conditions','insufficient_information','retain_dispute','withdraw_incorrect','reopen') or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 4000:
            raise ValueError('provide an authenticated decision with a bounded reason')
        with writer_lock(self.root), self.db:
            self.db.execute('BEGIN IMMEDIATE')
            case = self._conflict_case(left_id, right_id)
            if case['snapshot'] != expected_snapshot or case['version'] != expected_version:
                raise ConflictError('conflict evidence or decision changed; refresh before deciding')
            if not case['active']:
                raise ConflictError('one assertion is withdrawn; review the current evidence instead')
            if action == 'different_conditions' and case['condition_relation'] != 'different_conditions':
                raise ValueError('recorded conditions do not establish a difference; retain dispute or request information')
            if action != 'withdraw_incorrect' and withdraw_id:
                raise ValueError('withdraw_id requires withdraw_incorrect')
            if action == 'withdraw_incorrect':
                fact = next((row for row in case['facts'] if row['id'] == withdraw_id and row['scope']=='private'), None)
                if not fact:
                    raise ValueError('only an explicitly selected workspace-private assertion may be withdrawn')
                self._withdraw_assertion(withdraw_id, actor=actor, expected_revision=fact['record_revision'])
            else:
                self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            snapshot_after = self._conflict_case(left_id, right_id)['snapshot']
            event = {'version':case['version']+1, 'action':action, 'reason':reason.strip(), 'withdraw_id':withdraw_id,
                     'snapshot_before':case['snapshot'], 'snapshot_after':snapshot_after}
            self.db.execute('INSERT INTO conflict_decision VALUES(?,?,?,?,?,?,?,?)', (case['case_id'], event['version'], *sorted((left_id,right_id)), action, canonical(event).decode(), actor, time.time()))
            self._audit(actor, 'conflict.decision', {'case_id':case['case_id'], **event})
            result = self._conflict_case(left_id, right_id)
            return {**result, 'impacts':self._conflict_impacts(result), **self.revisions()}

    def _save_report_check(self, identifier, result):
        summary = {'knowledge_version':self.revisions(), 'checked_at':time.time(),
                   'delivery_status':result['delivery_status'], 'reference_status':result['reference_status'],
                   'statuses':[row['status'] for row in result.get('assessments',[])]}
        self.db.execute('INSERT OR REPLACE INTO report_check VALUES(?,?)', (identifier, canonical(summary).decode()))

    def recheck_report(self, identifier, *, actor):
        from .governance import ConflictError
        if not actor:
            raise ValueError('authenticated actor required')
        try:
            return self.validate_report(None, actor=actor, report_id=identifier)
        except ConflictError:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                result = {'report_id':identifier, 'reference_status':'invalid_references', 'delivery_status':'needs_review',
                          'requires_human_review':True, 'text_consistency_checked':False, 'scientific_correctness_verified':False, 'assessments':[], **self.revisions()}
                self._save_report_check(identifier, result)
                self._audit(actor, 'report.recheck', {'report_id':identifier,'reference_status':'invalid_references'})
                return result
