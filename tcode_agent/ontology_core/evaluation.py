"""Offline retrieval baseline. Gold labels are supplied by people, never by the query itself."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

from .governance import DDL, PrivateKnowledge
from .sqlite_store import build_database


@contextmanager
def fixture_store(dataset):
    """Replay a small labelled corpus in disposable databases using production queries."""
    with tempfile.TemporaryDirectory(prefix='ontology-eval-') as root:
        path = Path(root) / 'public.sqlite'
        build_database(path, dataset['entities'], dataset['assertions'], dataset.get('evidence', []))
        public = sqlite3.connect(path)
        private = sqlite3.connect(':memory:')
        private.row_factory = sqlite3.Row
        private.executescript(DDL)
        private.execute("INSERT INTO meta VALUES(1,0,0,'evaluation')")
        private.commit()
        store = PrivateKnowledge.__new__(PrivateKnowledge)
        store.root = Path(root)
        store.db = private
        store.workspace_id = 'evaluation'
        store.public = SimpleNamespace(db=public, manifest={'bundle_digest': 'evaluation'})
        try:
            yield store
        finally:
            private.close()
            public.close()


def evaluate(store, cases):
    if not cases:
        raise ValueError('at least one independently labelled case is required')
    results = []
    for case in cases:
        if case['operation'] not in ('query', 'graph'):
            raise ValueError('only read-only query and graph cases are supported')
        expected = set(case['expected_ids'])
        started = time.perf_counter()
        response = getattr(store, case['operation'])(**case.get('params', {}))
        duration = (time.perf_counter() - started) * 1000
        if case['operation'] == 'graph':
            actual = {node['id'] for node in response['nodes']}
        else:
            actual = {row['id'] for visibility in ('public', 'private') for row in response[visibility]}
        hits = len(expected & actual)
        precision = hits / len(actual) if actual else float(not expected)
        recall = hits / len(expected) if expected else 1.0
        checks = {key: response.get(key) == value for key, value in case.get('expected_fields', {}).items()}
        passed = actual == expected and all(checks.values())
        results.append({'id': case['id'], 'passed': passed, 'precision': precision, 'recall': recall,
                        'missing_ids': sorted(expected - actual), 'unexpected_ids': sorted(actual - expected),
                        'field_checks': checks, 'elapsed_ms': round(duration, 3)})
    times = sorted(row['elapsed_ms'] for row in results)
    return {'cases': results, 'case_count': len(results), 'passed': sum(row['passed'] for row in results),
            'macro_precision': sum(row['precision'] for row in results) / len(results),
            'macro_recall': sum(row['recall'] for row in results) / len(results),
            'latency_p50_ms': times[len(times) // 2], 'latency_max_ms': max(times),
            'scope': 'retrieval_regression', 'scientific_correctness_verified': False}


def evaluate_support(store, cases):
    if not cases:
        raise ValueError('at least one independently labelled support case is required')
    results = []
    false_supported = 0
    for case in cases:
        started = time.perf_counter()
        report = store.validate_report(case['claims'])
        actual = [row['status'] for row in report['assessments']]
        expected = case['expected_statuses']
        reasons = {reason for row in report['assessments'] for reason in row['reasons']}
        reasons.update(reason for row in report['assessments'] for check in row['evidence_checks'] for reason in check['reasons'])
        false_supported += sum(status == 'supported' and (index >= len(expected) or expected[index] != 'supported') for index, status in enumerate(actual))
        results.append({'id': case['id'], 'expected_statuses': expected, 'actual_statuses': actual,
                        'passed': actual == expected and set(case.get('expected_reasons', [])) <= reasons,
                        'elapsed_ms': round((time.perf_counter()-started)*1000, 3)})
    return {'scope': 'structured_support_regression', 'cases': results, 'case_count': len(results),
            'passed': sum(row['passed'] for row in results), 'false_supported': false_supported,
            'scientific_correctness_verified': False, 'text_consistency_checked': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--support', action='store_true', help='Evaluate structured support cases instead of retrieval cases')
    parser.add_argument('--server-root', type=Path, help='Evaluate an existing managed workspace instead of replaying fixture facts')
    parser.add_argument('--tenant')
    parser.add_argument('--user')
    parser.add_argument('--workspace')
    args = parser.parse_args()
    if args.server_root and not all((args.tenant, args.user, args.workspace)):
        parser.error('existing-workspace evaluation requires --tenant, --user and --workspace')
    raw = args.dataset.read_bytes()
    dataset = json.loads(raw)
    evaluator = evaluate_support if args.support else evaluate
    cases = dataset['support_cases' if args.support else 'cases']
    if args.server_root:
        from ..ontology_host import open_store
        store = open_store(args.server_root, (args.tenant, args.user, args.workspace))
        try:
            report = evaluator(store, cases)
            report['revisions'] = store.revisions()
        finally:
            store.close()
    else:
        with fixture_store(dataset) as store:
            report = evaluator(store, cases)
    report.update(dataset_id=dataset['id'], dataset_sha256=hashlib.sha256(raw).hexdigest(),
                  annotation=dataset.get('annotation', {}), mode='workspace' if args.server_root else 'fixture',
                  measured_at=datetime.now(timezone.utc).isoformat(), python_version=sys.version.split()[0],
                  fixture_counts=({'entities': len(dataset['entities']), 'assertions': len(dataset['assertions'])}
                                  if not args.server_root else None))
    output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end='')
    return 0 if report['passed'] == report['case_count'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
