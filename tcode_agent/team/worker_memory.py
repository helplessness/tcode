"""Worker-scoped persistent memory stored outside manager team memory."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..atomic_storage import file_transaction
from .memory import TeamMemoryEntry, TeamMemoryManager


class WorkerMemoryManager(TeamMemoryManager):
    """Persistent memory for fixed workers with a dedicated storage directory.

    New location:
      .tcode/workers/{memory_key}_memory.json

    Legacy worker memories that previously lived under `.tcode/teams/`
    are migrated lazily on first access.
    """

    MEMORY_SCOPE_KIND = "worker"
    DEFAULT_MAX_ENTRIES = 1000

    @classmethod
    def _legacy_path(cls, memory_key: str, workdir: str) -> Path:
        base = workdir or os.getcwd()
        safe_key = str(memory_key or "").replace(":", "__")
        return Path(base) / ".tcode" / "teams" / f"{safe_key}_memory.json"

    @classmethod
    def _path(cls, team_id: str, workdir: str) -> str:
        base = workdir or os.getcwd()
        safe_key = str(team_id or "").replace(":", "__")
        return os.path.join(base, ".tcode", "workers", f"{safe_key}_memory.json")

    @classmethod
    def _ensure(cls, team_id: str, workdir: str) -> None:
        path = Path(cls._path(team_id, workdir))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Use the same sidecar lock as inherited load/save, including creation.
        # Locking only the new data file lets another process read it half-written.
        with file_transaction(path):
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return
            with os.fdopen(descriptor, "w", encoding="utf-8") as target_file:
                legacy = cls._legacy_path(team_id, workdir)
                if legacy.exists():
                    try:
                        target_file.write(legacy.read_text(encoding="utf-8"))
                        for suffix in (".faiss", ".faiss.meta.json"):
                            current_sidecar = legacy.with_suffix(legacy.suffix + suffix)
                            old_sidecar = legacy.with_suffix(suffix)
                            source = current_sidecar if current_sidecar.exists() else old_sidecar
                            if source.exists():
                                target = path.with_suffix(path.suffix + suffix)
                                shutil.copyfile(source, target)
                    except Exception:
                        target_file.seek(0)
                        target_file.truncate()
                        json.dump([], target_file, ensure_ascii=False, indent=2)
                else:
                    json.dump([], target_file, ensure_ascii=False, indent=2)

    @classmethod
    def render_context(cls, memory_key: str, workdir: str, limit: int = 20) -> str:
        return super().render_context(memory_key, workdir, limit=limit)

    @classmethod
    def load(cls, memory_key: str, workdir: str) -> list[TeamMemoryEntry]:
        return super().load(memory_key, workdir)

    @classmethod
    def add_structured(
        cls,
        *,
        memory_key: str,
        workdir: str,
        source: str = "worker",
        tags: list[str] | None = None,
        **fields,
    ) -> TeamMemoryEntry:
        return super().add_structured(
            team_id=memory_key,
            workdir=workdir,
            source=source,
            tags=tags,
            **fields,
        )
