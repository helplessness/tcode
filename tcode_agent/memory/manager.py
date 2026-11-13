"""Persistent memory manager.

Stores long-term memory in `.tcode/memory.json` under the workspace.

This version supports both:
- legacy append-only text memories
- structured memories with lightweight embedding retrieval
"""

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from ..atomic_storage import atomic_write_json, file_transaction

from .filters import apply_memory_filters
from .budget import enforce_memory_budget
from .indexing import (
    ensure_vector_index,
    hydrate_embeddings,
    persistent_memory_record,
    rank_with_vector_index,
    records_contain_embeddings,
)
from .levels import build_l0_summary, build_l1_overview, build_l2_detail, choose_memory_level
from .retriever import build_search_text
from .schema import STRUCTURED_MEMORY_KEYS, normalize_structured_fields


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str]
    created_at: str
    updated_at: str
    source: str = "agent"
    task_type: str = ""
    goal: str = ""
    constraints: list[str] = None
    decision: str = ""
    outcome: str = ""
    confidence: float = 0.0
    toolchain: list[str] = None
    failure_reason: str = ""
    embedding: list[float] = None
    search_text: str = ""
    l0_summary: str = ""
    l1_overview: str = ""
    l2_detail: str = ""

    def __post_init__(self):
        if self.constraints is None:
            self.constraints = []
        if self.toolchain is None:
            self.toolchain = []
        if self.embedding is None:
            self.embedding = []




def cosine_similarity_local(left: list[float], right: list[float]) -> float:
    import math
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(float(left[i]) * float(right[i]) for i in range(size))
    na = math.sqrt(sum(v * v for v in left))
    nb = math.sqrt(sum(v * v for v in right))
    return dot / (na * nb) if na * nb else 0.0


def _memory_max_entries() -> int:
    try:
        return max(100, int(os.environ.get("TCODE_MEMORY_MAX_ENTRIES", "1000") or "1000"))
    except Exception:
        return 1000


def _memory_auto_compact_enabled() -> bool:
    raw = str(os.environ.get("TCODE_MEMORY_AUTO_COMPACT", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class MemoryManager:
    """Simple persistent memory storage."""

    @classmethod
    def _resolve_workdir(cls, workdir: Optional[str]) -> str:
        return workdir or os.getcwd()

    @classmethod
    def _memory_path(cls, workdir: Optional[str]) -> str:
        base = cls._resolve_workdir(workdir)
        return os.path.join(base, ".tcode", "memory.json")

    @classmethod
    def _vector_base_path(cls, workdir: Optional[str]) -> Path:
        return Path(cls._memory_path(workdir))

    @classmethod
    def _ensure_store(cls, workdir: Optional[str]):
        path = cls._memory_path(workdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with file_transaction(path):
            if not os.path.exists(path):
                atomic_write_json(path, [])

    @classmethod
    def _locked_load(cls, workdir: Optional[str]) -> list:
        cls._ensure_store(workdir)
        path = cls._memory_path(workdir)
        with file_transaction(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("Memory store is damaged; refusing to overwrite existing data")
            records = raw
            if records_contain_embeddings(records):
                records = [persistent_memory_record(item) for item in records]
                atomic_write_json(path, records)
            return records

    @classmethod
    def _locked_save(cls, memories: list[MemoryEntry], workdir: Optional[str]):
        cls._ensure_store(workdir)
        path = cls._memory_path(workdir)
        with file_transaction(path):
            memories = enforce_memory_budget(memories, path)
            data = [persistent_memory_record(m) for m in memories]
            atomic_write_json(path, data)
        return memories

    @classmethod
    def _maybe_repair_index(cls, memories: list[MemoryEntry], workdir: Optional[str]):
        try:
            return ensure_vector_index(
                memories,
                base_path=cls._vector_base_path(workdir),
                workdir=workdir,
                id_getter=lambda item: item.id,
                text_getter=lambda item: item.search_text or item.content,
            )
        finally:
            for item in memories:
                item.embedding = []

    @classmethod
    def _deserialize(cls, raw: list) -> list[MemoryEntry]:
        result: list[MemoryEntry] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                result.append(
                    MemoryEntry(
                        id=str(item.get("id", "")),
                        content=str(item.get("content", "")).strip(),
                        tags=list(item.get("tags", [])) if isinstance(item.get("tags", []), list) else [],
                        created_at=str(item.get("created_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                        source=str(item.get("source", "agent")),
                        task_type=str(item.get("task_type", "")),
                        goal=str(item.get("goal", "")),
                        constraints=list(item.get("constraints", [])) if isinstance(item.get("constraints", []), list) else [],
                        decision=str(item.get("decision", "")),
                        outcome=str(item.get("outcome", "")),
                        confidence=float(item.get("confidence", 0.0) or 0.0),
                        toolchain=list(item.get("toolchain", [])) if isinstance(item.get("toolchain", []), list) else [],
                        failure_reason=str(item.get("failure_reason", "")),
                        embedding=[],
                        search_text=str(item.get("search_text", "")),
                        l0_summary=str(item.get("l0_summary", "")),
                        l1_overview=str(item.get("l1_overview", "")),
                        l2_detail=str(item.get("l2_detail", "")),
                    )
                )
            except Exception:
                continue
        return [x for x in result if x.id and x.content]

    @classmethod
    def load(cls, workdir: Optional[str] = None) -> list[MemoryEntry]:
        cls._ensure_store(workdir)
        path = cls._memory_path(workdir)
        with file_transaction(path):
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("Memory store is damaged; refusing to overwrite existing data")
            parsed = cls._deserialize(raw)
            if len(parsed) != len(raw):
                raise ValueError("Invalid memory record; refusing to overwrite existing data")
            original = [persistent_memory_record(item) for item in parsed]
            final = enforce_memory_budget(parsed, path)
            bounded = [persistent_memory_record(item) for item in final]
            if bounded != original:
                atomic_write_json(path, bounded)
            elif records_contain_embeddings(raw):
                atomic_write_json(path, [persistent_memory_record(item) for item in raw])
        cls._maybe_repair_index(final, workdir)
        return final

    @classmethod
    def save(cls, memories: list[MemoryEntry], workdir: Optional[str] = None):
        # Compact if over limit (auto-merge similar entries, no data loss)
        if _memory_auto_compact_enabled():
            while len(memories) > _memory_max_entries():
                compacted = cls._maybe_compact(memories, workdir)
                if len(compacted) >= len(memories):
                    break
                memories = compacted
        memories = cls._locked_save(memories, workdir)
        cls._rebuild_vector_index(memories, workdir)

    @classmethod
    def _maybe_compact(
        cls,
        memories: list[MemoryEntry],
        workdir: Optional[str] = None,
    ) -> list[MemoryEntry]:
        max_entries = _memory_max_entries()
        if len(memories) <= max_entries:
            return memories

        hydrate_embeddings(
            memories,
            workdir=workdir,
            text_getter=lambda item: item.search_text or item.content,
        )

        # Always retain a deterministic fallback pair so the configured cap is
        # enforced even when all vectors are orthogonal or unavailable.
        best_sim = float("-inf")
        best_pair: tuple[int, int] = (0, 1)
        for i in range(len(memories)):
            if not memories[i].embedding:
                continue
            for j in range(i + 1, len(memories)):
                if not memories[j].embedding:
                    continue
                sim = cosine_similarity_local(
                    memories[i].embedding,
                    memories[j].embedding,
                )
                if sim > best_sim:
                    best_sim = sim
                    best_pair = (i, j)

        # Merge the pair
        a, b = best_pair[0], best_pair[1]
        merged_content = (
            f"{memories[a].content}\n"
            f"[merged from {memories[a].id[:8]} + {memories[b].id[:8]}] "
            f"{memories[b].content}"
        )
        merged_tags = list(set(memories[a].tags + memories[b].tags))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        search_text = build_search_text(
            content=merged_content,
            tags=merged_tags,
            fields={
                "task_type": memories[a].task_type or memories[b].task_type,
                "goal": memories[a].goal or memories[b].goal,
                "source": f"{memories[a].source},{memories[b].source}",
            },
        )
        merged = MemoryEntry(
            id=str(uuid.uuid4()),
            content=merged_content,
            tags=merged_tags,
            created_at=memories[a].created_at,
            updated_at=now,
            source=f"{memories[a].source},{memories[b].source}",
            task_type=memories[a].task_type or memories[b].task_type,
            goal=memories[a].goal or memories[b].goal,
            constraints=list(dict.fromkeys(memories[a].constraints + memories[b].constraints)),
            decision="\n".join(dict.fromkeys(filter(None, [memories[a].decision, memories[b].decision]))),
            outcome="\n".join(dict.fromkeys(filter(None, [memories[a].outcome, memories[b].outcome]))),
            confidence=min(memories[a].confidence, memories[b].confidence),
            toolchain=list(dict.fromkeys(memories[a].toolchain + memories[b].toolchain)),
            failure_reason="\n".join(dict.fromkeys(filter(None, [memories[a].failure_reason, memories[b].failure_reason]))),
            embedding=[],
            search_text=search_text,
            l0_summary=build_l0_summary(
                task_type=memories[a].task_type or memories[b].task_type,
                goal=memories[a].goal or memories[b].goal,
                outcome=merged_content[:200],
                confidence=min(memories[a].confidence, memories[b].confidence),
                failure_reason="",
            ),
            l1_overview="\n".join(filter(None, [memories[a].l1_overview, memories[b].l1_overview])),
            l2_detail=merged_content,
        )
        merged.search_text = build_search_text(content=merged.content, tags=merged.tags, fields=asdict(merged))
        merged.l1_overview = build_l1_overview(
            constraints=merged.constraints, decision=merged.decision,
            toolchain=merged.toolchain, tags=merged.tags, source=merged.source,
        )

        # Replace the pair with merged entry
        result = [m for i, m in enumerate(memories) if i not in best_pair]
        result.append(merged)
        return result

    @classmethod
    def _compact_now(cls, workdir: Optional[str] = None) -> int:
        """Force a compaction cycle. Returns how many entries were merged."""
        memories = cls.load(workdir)
        before = len(memories)
        compacted = cls._maybe_compact(memories, workdir)
        if len(compacted) < before:
            compacted = cls._locked_save(compacted, workdir)
            cls._rebuild_vector_index(compacted, workdir)
        return before - len(compacted)

    @classmethod
    def _rebuild_vector_index(
        cls,
        memories: list[MemoryEntry],
        workdir: Optional[str] = None,
        *,
        force: bool = False,
    ):
        try:
            return ensure_vector_index(
                memories,
                base_path=cls._vector_base_path(workdir),
                workdir=workdir,
                id_getter=lambda item: item.id,
                text_getter=lambda item: item.search_text or item.content,
                force=force,
            )
        finally:
            for item in memories:
                item.embedding = []

    @classmethod
    def add(
        cls,
        content: str,
        tags: Optional[list[str]] = None,
        workdir: Optional[str] = None,
        source: str = "agent",
        structured: Optional[dict] = None,
    ) -> MemoryEntry:
        text = (content or "").strip()
        if not text:
            raise ValueError("Memory content cannot be empty")

        normalized = normalize_structured_fields(structured or {})
        search_text = build_search_text(content=text, tags=tags or [], fields=normalized)

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=text,
            tags=[t.strip() for t in (tags or []) if str(t).strip()],
            created_at=now,
            updated_at=now,
            source=source,
            task_type=normalized["task_type"],
            goal=normalized["goal"],
            constraints=normalized["constraints"],
            decision=normalized["decision"],
            outcome=normalized["outcome"],
            confidence=normalized["confidence"],
            toolchain=normalized["toolchain"],
            failure_reason=normalized["failure_reason"],
            embedding=[],
            search_text=search_text,
            l0_summary=build_l0_summary(
                task_type=normalized["task_type"],
                goal=normalized["goal"],
                outcome=normalized["outcome"] or text,
                confidence=normalized["confidence"],
                failure_reason=normalized["failure_reason"],
            ),
            l1_overview=build_l1_overview(
                constraints=normalized["constraints"],
                decision=normalized["decision"],
                toolchain=normalized["toolchain"],
                tags=[t.strip() for t in (tags or []) if str(t).strip()],
                source=source,
            ),
            l2_detail=build_l2_detail(content=text, outcome=normalized["outcome"] or text),
        )

        cls._ensure_store(workdir)
        path = cls._memory_path(workdir)
        with file_transaction(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("Memory store is damaged; refusing to overwrite existing data")
            records = raw
            memories = cls._deserialize(records)
            memories.append(entry)
            if _memory_auto_compact_enabled():
                while len(memories) > _memory_max_entries():
                    compacted = cls._maybe_compact(memories, workdir)
                    if len(compacted) >= len(memories):
                        break
                    memories = compacted
            memories = enforce_memory_budget(memories, path)
            atomic_write_json(
                path,
                [persistent_memory_record(item) for item in memories],
            )
        cls._rebuild_vector_index(memories, workdir)
        return next((item for item in memories if item.id == entry.id), entry)

    @classmethod
    def add_structured(
        cls,
        *,
        workdir: Optional[str] = None,
        source: str = "agent",
        tags: Optional[list[str]] = None,
        **fields,
    ) -> MemoryEntry:
        normalized = normalize_structured_fields(fields)
        content = normalized["outcome"] or normalized["decision"] or normalized["goal"]
        return cls.add(
            content=content,
            tags=tags or [],
            workdir=workdir,
            source=source,
            structured=normalized,
        )

    @classmethod
    def query(
        cls,
        workdir: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 20,
        level: str = "l1",
        filters: Optional[dict] = None,
    ) -> list[MemoryEntry]:
        memories = cls.load(workdir)
        memories = apply_memory_filters(memories, filters)
        memories.reverse()  # newest first

        if query:
            q = query.lower().strip()
            memories = cls._query_semantic(memories, q, workdir=workdir)
        else:
            memories.sort(key=lambda m: (m.updated_at, m.created_at), reverse=True)

        safe_limit = max(1, min(limit or 20, 200))
        return cls._apply_level(memories[:safe_limit], level)

    @classmethod
    def query_related(
        cls,
        *,
        workdir: Optional[str] = None,
        task_type: Optional[str] = None,
        goal: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 10,
        level: str = "l1",
        filters: Optional[dict] = None,
    ) -> list[MemoryEntry]:
        memories = cls.load(workdir)
        if task_type:
            memories = [
                m for m in memories
                if not m.task_type or m.task_type == task_type or task_type.lower() in m.task_type.lower()
            ]
        memories = apply_memory_filters(memories, filters)
        query_text = " ".join([part for part in [task_type or "", goal or "", query or ""] if part]).strip()
        ranked = cls._query_semantic(memories, query_text, workdir=workdir)
        safe_limit = max(1, min(limit or 10, 200))
        return cls._apply_level(ranked[:safe_limit], level)

    @classmethod
    def _apply_level(cls, memories: list[MemoryEntry], level: str) -> list[MemoryEntry]:
        normalized = str(level or "l1").strip().lower()
        if normalized not in {"l0", "l1", "l2"}:
            normalized = "l1"
        result: list[MemoryEntry] = []
        for item in memories:
            clone = MemoryEntry(**asdict(item))
            clone.content = choose_memory_level(normalized, item)
            result.append(clone)
        return result

    @classmethod
    def _query_semantic(
        cls,
        memories: list[MemoryEntry],
        query: str,
        *,
        workdir: Optional[str] = None,
    ) -> list[MemoryEntry]:
        return rank_with_vector_index(
            memories,
            query=query,
            base_path=cls._vector_base_path(workdir),
            workdir=workdir,
            id_getter=lambda item: item.id,
            text_getter=lambda item: item.search_text or item.content,
        )

    @classmethod
    def delete(cls, memory_id: str, workdir: Optional[str] = None) -> bool:
        cls._ensure_store(workdir)
        path = cls._memory_path(workdir)
        with file_transaction(path):
            with open(path, encoding="utf-8") as stream:
                raw = json.load(stream)
            if not isinstance(raw, list):
                raise ValueError("Memory store is damaged; refusing to overwrite existing data")
            before = len(raw)
            raw = [item for item in raw if str(item.get("id", "")) != memory_id]
            if len(raw) == before:
                return False
            atomic_write_json(path, [persistent_memory_record(item) for item in raw])
        cls._rebuild_vector_index(cls.load(workdir), workdir)
        return True

    @classmethod
    def get(
        cls,
        memory_id: str,
        *,
        workdir: Optional[str] = None,
        level: str = "l2",
    ) -> Optional[MemoryEntry]:
        for item in cls.load(workdir):
            if item.id != memory_id:
                continue
            return cls._apply_level([item], level)[0]
        return None

    @classmethod
    def render_prompt_context(cls, workdir: Optional[str] = None, limit: int = 10) -> str:
        items = cls.query(workdir=workdir, limit=limit, level="l0")
        if not items:
            return ""

        lines = []
        for item in items:
            tag_text = f" [tags: {', '.join(item.tags)}]" if item.tags else ""
            structured_bits = []
            for key in STRUCTURED_MEMORY_KEYS:
                value = getattr(item, key, None)
                if isinstance(value, list) and value:
                    structured_bits.append(f"{key}={', '.join(str(v) for v in value)}")
                elif isinstance(value, (str, float)) and value not in {"", 0.0}:
                    structured_bits.append(f"{key}={value}")
            suffix = f" | {'; '.join(structured_bits)}" if structured_bits else ""
            lines.append(f"- {item.content}{tag_text}{suffix}")
        return "\n".join(lines)
