"""Offline administrator backup/restore; no user-selected paths in HTTP tools."""
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .bundle import file_digest, safe_file, sync_dir, write_json, writer_lock
from .governance import PrivateKnowledge


def backup(data_root, destination, workspace_id, knowledge_root):
    root, target = Path(data_root).resolve(), Path(destination).absolute()
    if not (root / 'private.sqlite').is_file():
        raise ValueError('private store does not exist')
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(root):
        raise ValueError('backup requires a new destination outside the live store')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.ontology-backup-', dir=target.parent))
    try:
        with writer_lock(root):
            store = PrivateKnowledge(root, workspace_id, knowledge_root)
            try:
                for source in root.rglob('*'):
                    if source.is_symlink():
                        raise ValueError('symlink in private store')
                    relative = source.relative_to(root)
                    if not source.is_file() or relative.name in ('.ontology-build.lock', 'private.sqlite', 'drafts.sqlite') or relative.name.endswith(('-wal', '-shm')):
                        continue
                    dest = staging / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, dest)
                for name, connection in [('private.sqlite', store.db), ('drafts.sqlite', store.intake.db)]:
                    with sqlite3.connect(staging / name) as saved:
                        connection.backup(saved)
                manifest = {'format': 'private-backup-1', 'workspace_id': workspace_id, **store.revisions(), 'files': {str(p.relative_to(staging)): file_digest(p) for p in staging.rglob('*') if p.is_file()}}
                write_json(staging / 'backup.json', manifest)
            finally:
                store.close()
        for file in staging.rglob('*'):
            if file.is_file():
                with file.open('rb') as stream:
                    os.fsync(stream.fileno())
        sync_dir(staging)
        staging.rename(target)
        sync_dir(target.parent)
        return {'backup': str(target), 'files': len(manifest['files']), 'workspace_id': workspace_id}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore(backup_root, destination, workspace_id, knowledge_root):
    source, target = Path(backup_root).resolve(), Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('restore requires a new empty destination; never overwrite a live store')
    manifest = json.loads(safe_file(source, 'backup.json').read_text())
    if manifest.get('format') != 'private-backup-1' or manifest['workspace_id'] != workspace_id:
        raise ValueError('backup scope/format mismatch')
    if not {'private.sqlite', 'drafts.sqlite'} <= set(manifest['files']):
        raise ValueError('backup databases missing')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.ontology-restore-', dir=target.parent))
    try:
        for relative, sha in manifest['files'].items():
            original = safe_file(source, relative)
            if file_digest(original) != sha:
                raise ValueError('backup hash mismatch: ' + relative)
            dest = staging / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, dest)
        for name in ('private.sqlite', 'drafts.sqlite'):
            with sqlite3.connect(staging / name) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or list(db.execute('PRAGMA foreign_key_check')):
                    raise ValueError('invalid backup database')
        store = PrivateKnowledge(staging, workspace_id, knowledge_root)
        try:
            if store.revisions() != {key: manifest[key] for key in store.revisions()}:
                raise ValueError('backup release/revision mismatch; restore matching public release first')
        finally:
            store.close()
        for file in staging.rglob('*'):
            if file.is_file():
                with file.open('rb') as stream:
                    os.fsync(stream.fileno())
        sync_dir(staging)
        staging.rename(target)
        sync_dir(target.parent)
        return {'restored': str(target), 'workspace_id': workspace_id}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
