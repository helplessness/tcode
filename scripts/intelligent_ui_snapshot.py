"""Copy a consistent IntelligentUI SQLite snapshot without overwriting a destination.

Use the same command to restore a snapshot into a fresh workdir. This copies
canvas/page data only; a full runtime backup must also preserve sessions/research.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile


def copy_snapshot(source: Path, destination: Path) -> None:
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise ValueError("source_must_be_a_regular_database")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.intelligent-ui-', dir=destination.parent)
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    try:
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as original:
            with sqlite3.connect(temporary) as snapshot:
                original.backup(snapshot)
                if snapshot.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('snapshot_integrity_check_failed')
        # Publishing by link is atomic and fails if another process created the target.
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    arguments = parser.parse_args()
    copy_snapshot(arguments.source, arguments.destination)
    print(f'Verified snapshot: {arguments.destination}')
