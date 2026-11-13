"""Bounded source-asset inspection; content hashing runs only when requested."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from .presentation import evidence_image_entries


def asset_paths(record) -> list[str]:
    payload = record.to_dict()
    paths = [entry['path'] for entry in evidence_image_entries(payload)]
    if record.source_path:
        paths.append(record.source_path)
    for source in [record.metadata, record.artifact_refs, *record.citations, record.metadata.get('paper_anchor', {})]:
        if isinstance(source, dict):
            for key in ('pdf_path', 'source_pdf_path'):
                if source.get(key):
                    paths.append(str(source[key]))
    return list(dict.fromkeys(str(path) for path in paths if path))


def _expected_digest(record, raw: str) -> str:
    values = record.metadata.get('asset_sha256', {})
    if isinstance(values, dict) and values.get(raw):
        return str(values[raw])
    if raw == record.source_path:
        provenance = record.metadata.get('platform_provenance', {})
        if isinstance(provenance, dict):
            return str(provenance.get('source_digest') or '')
    return ''


def check_assets(record, *, workdir: str, verify: bool = False, verified_paths: set[str] | None = None) -> dict:
    from .store import EvidenceStore

    root = Path(workdir).expanduser().resolve()
    paths = asset_paths(record)
    try:
        limit = max(1, min(500, int(os.getenv('TCODE_EVIDENCE_ASSET_CHECK_LIMIT', '100'))))
    except ValueError:
        limit = 100
    issues = []
    checked = verified = 0
    for raw in paths[:limit]:
        label = Path(raw).name
        def issue(severity, code):
            issues.append({'severity': severity, 'code': code, 'message': code, 'evidence_id': record.evidence_id, 'metadata': {'file': label}})
        if urlparse(raw).scheme in {'http', 'https', 'data'}:
            issue('info', 'asset_remote_not_checked')
            continue
        target = Path(raw).expanduser()
        target = (root / target).resolve() if not target.is_absolute() else target.resolve()
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            issue('error', 'asset_outside_workspace')
            continue
        checked += 1
        try:
            before = target.stat()
            if not target.is_file():
                issue('error', 'asset_missing')
                continue
            # Opening, not os.access, verifies readability for the current worker.
            with target.open('rb') as stream:
                stream.read(1)
            with EvidenceStore._open(workdir) as connection:
                cached = connection.execute('SELECT * FROM evidence_asset_checks WHERE path=?', (relative,)).fetchone()
            current = bool(cached and cached['size'] == before.st_size and cached['mtime_ns'] == before.st_mtime_ns)
            if verify and (not current or verified_paths is None or relative not in verified_paths):
                digest = hashlib.sha256()
                with target.open('rb') as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                after = target.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    issue('warn', 'asset_changed_during_check')
                    continue
                observed = digest.hexdigest()
                baseline = cached['baseline'] if cached else _expected_digest(record, raw) or observed
                with EvidenceStore._open(workdir) as connection:
                    connection.execute('INSERT OR REPLACE INTO evidence_asset_checks VALUES(?,?,?,?,?,?)', (relative, baseline, observed, before.st_size, before.st_mtime_ns, time.time()))
                current = True
                if verified_paths is not None:
                    verified_paths.add(relative)
            elif current:
                observed, baseline = cached['observed'], cached['baseline']
            else:
                issue('info', 'asset_verification_pending' if cached or _expected_digest(record, raw) else 'asset_baseline_missing')
                continue
            verified += 1
            expected = _expected_digest(record, raw) or baseline
            if expected != observed:
                issue('error', 'asset_hash_mismatch')
        except FileNotFoundError:
            issue('error', 'asset_missing')
        except PermissionError:
            issue('error', 'asset_unreadable')
        except OSError:
            issue('warn', 'asset_unreadable')
    return {'file_count': len(paths), 'checked_file_count': checked, 'verified_file_count': verified,
            'truncated': len(paths) > limit, 'issues': issues}
