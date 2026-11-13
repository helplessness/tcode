"""Bound active JSON memory while retaining original records on disk."""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

from ..atomic_storage import atomic_write_json
from .indexing import persistent_memory_record


def enforce_memory_budget(entries, base_path, scope="global"):
    name = "TCODE_MEMORY_MAX_BYTES" if scope == "global" else f"TCODE_{scope.upper()}_MEMORY_MAX_BYTES"
    defaults = {"global": 4_194_304, "team": 2_097_152, "worker": 4_194_304, "session": 524_288}
    try:
        limit = max(4096, int(os.environ.get(name, defaults[scope])))
    except (ValueError, TypeError):
        limit = defaults[scope]
    result = list(entries)

    def size():
        return len(json.dumps([persistent_memory_record(e) for e in result], ensure_ascii=False, indent=2).encode("utf-8"))

    def archive(entry):
        record = persistent_memory_record(entry)
        digest = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        directory = Path(str(base_path) + ".archive")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}.json"
        if not target.exists():
            atomic_write_json(target, record)
        return digest

    # ponytail: deterministic excerpts; semantic summaries can replace these if needed.
    for index in sorted(range(len(result)), key=lambda i: result[i].created_at):
        if size() <= limit:
            break
        original = result[index]
        digest = archive(original)
        changes = {}
        for key, value in persistent_memory_record(original).items():
            if key in {"id", "created_at", "updated_at"}:
                continue
            if isinstance(value, str):
                changes[key] = value[:160]
            elif isinstance(value, list):
                changes[key] = [str(item)[:80] for item in value[:8]]
        preview = original.content[:320] + f"\n[archived:{digest}]"
        changes.update(content=preview, search_text=preview, l2_detail=preview, embedding=[])
        result[index] = replace(original, **changes)
    while size() > limit and len(result) > 1:
        oldest = min(range(len(result)), key=lambda i: result[i].created_at)
        archive(result[oldest])
        result.pop(oldest)
    if size() > limit:
        raise ValueError("Memory metadata exceeds the active byte budget")
    return result
