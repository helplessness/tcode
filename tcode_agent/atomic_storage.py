"""Cross-process locking and crash-safe JSON publication for local stores."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .file_lock import lock_state_file as lock_file, unlock_file


@contextmanager
def file_transaction(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a", encoding="utf-8") as lock:
        lock_file(lock)
        try:
            yield
        finally:
            unlock_file(lock)


def atomic_write_json(path, data):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
