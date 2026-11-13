"""Team-scoped persistent memory.

Each team (identified by team_id = manager's session_id) has its own memory file:
  .tcode/teams/{team_id}_memory.json

This allows different Manager sessions to maintain isolated long-term memory
(decisions, worker results, project context) that persists across conversations.
"""

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..atomic_storage import atomic_write_json, file_transaction
from ..memory.filters import apply_memory_filters
from ..memory.budget import enforce_memory_budget
from ..memory.indexing import (
    ensure_vector_index,
    hydrate_embeddings,
    persistent_memory_record,
    rank_with_vector_index,
    records_contain_embeddings,
)
from ..memory.levels import build_l0_summary, build_l1_overview, build_l2_detail, choose_memory_level
from ..memory.retriever import build_search_text, cosine_similarity
from ..memory.schema import STRUCTURED_MEMORY_KEYS, normalize_structured_fields


@dataclass
class TeamMemoryEntry:
    id: str
    content: str
    tags: list
    created_at: str
    source: str = "manager"  # "manager" | "worker:<name>" | "auto"
    task_type: str = ""
    goal: str = ""
    constraints: list = None
    decision: str = ""
    outcome: str = ""
    confidence: float = 0.0
    toolchain: list = None
    failure_reason: str = ""
    embedding: list = None
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


class TeamMemoryManager:
    """Persistent memory scoped to a specific team (manager session).

    For protein team workers, use a stable memory_key (e.g. 'protein_orchestrator:requirements')
    so each worker accumulates their own knowledge across all sessions.
    """

    MEMORY_SCOPE_KIND = "team"
    DEFAULT_MAX_ENTRIES = 500

    @classmethod
    def _max_entries(cls) -> int:
        name = f"TCODE_{cls.MEMORY_SCOPE_KIND.upper()}_MEMORY_MAX_ENTRIES"
        try:
            return max(2, min(100_000, int(os.environ.get(name, cls.DEFAULT_MAX_ENTRIES))))
        except Exception:
            return cls.DEFAULT_MAX_ENTRIES

    @classmethod
    def _auto_compact_enabled(cls) -> bool:
        default = os.environ.get("TCODE_SCOPED_MEMORY_AUTO_COMPACT", "1")
        name = f"TCODE_{cls.MEMORY_SCOPE_KIND.upper()}_MEMORY_AUTO_COMPACT"
        raw = str(os.environ.get(name, default) or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    @classmethod
    def _path(cls, team_id: str, workdir: str) -> str:
        base = workdir or os.getcwd()
        # Colons in key are replaced with '__' to avoid filesystem issues
        safe_key = team_id.replace(":", "__")
        return os.path.join(base, ".tcode", "teams", f"{safe_key}_memory.json")

    @classmethod
    def _vector_base_path(cls, team_id: str, workdir: str) -> Path:
        return Path(cls._path(team_id, workdir))

    @classmethod
    def _ensure(cls, team_id: str, workdir: str) -> None:
        path = cls._path(team_id, workdir)
        with file_transaction(path):
            if not os.path.exists(path):
                atomic_write_json(path, [])

    @staticmethod
    def _read_records(path: str) -> list:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, list):
            raise ValueError(f"Invalid memory store: {path}")
        return raw

    @classmethod
    def load(cls, team_id: str, workdir: str) -> list:
        cls._ensure(team_id, workdir)
        path = cls._path(team_id, workdir)
        with file_transaction(path):
            raw = cls._read_records(path)
            parsed = cls._deserialize(raw)
            if len(parsed) != len(raw):
                raise ValueError("Invalid memory record; refusing to overwrite existing data")
            original = [persistent_memory_record(item) for item in parsed]
            final = enforce_memory_budget(parsed, path, cls.MEMORY_SCOPE_KIND)
            bounded = [persistent_memory_record(item) for item in final]
            if bounded != original:
                atomic_write_json(path, bounded)
            elif records_contain_embeddings(raw):
                atomic_write_json(path, [persistent_memory_record(item) for item in raw])
        cls._rebuild_vector_index(final, team_id, workdir)
        return final

    @classmethod
    def _deserialize(cls, raw: list) -> list[TeamMemoryEntry]:
        result: list[TeamMemoryEntry] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                result.append(TeamMemoryEntry(
                    id=str(item.get("id", "")),
                    content=str(item.get("content", "")).strip(),
                    tags=list(item.get("tags", [])),
                    created_at=str(item.get("created_at", "")),
                    source=str(item.get("source", "manager")),
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
                ))
            except Exception:
                continue
        return [x for x in result if x.id and x.content]

    @classmethod
    def _save(cls, entries: list, team_id: str, workdir: str) -> list:
        cls._ensure(team_id, workdir)
        persisted = cls._compact_to_limit(entries, workdir=workdir)
        path = cls._path(team_id, workdir)
        with file_transaction(path):
            persisted = enforce_memory_budget(persisted, path, cls.MEMORY_SCOPE_KIND)
            atomic_write_json(path, [persistent_memory_record(e) for e in persisted])
        cls._rebuild_vector_index(persisted, team_id, workdir)
        return persisted

    @classmethod
    def _rebuild_vector_index(
        cls,
        entries: list,
        team_id: str,
        workdir: str,
        *,
        force: bool = False,
    ) -> None:
        try:
            ensure_vector_index(
                entries,
                base_path=cls._vector_base_path(team_id, workdir),
                workdir=workdir,
                id_getter=lambda item: item.id,
                text_getter=lambda item: item.search_text or item.content,
                force=force,
            )
        except Exception:
            pass
        finally:
            for item in entries:
                item.embedding = []

    @staticmethod
    def _merge_text(left: str, right: str) -> str:
        values = [value.strip() for value in (left, right) if str(value or "").strip()]
        return "\n".join(dict.fromkeys(values))

    @classmethod
    def _merge_pair(
        cls,
        left: TeamMemoryEntry,
        right: TeamMemoryEntry,
        *,
        workdir: str,
    ) -> TeamMemoryEntry:
        content = (
            f"{left.content}\n"
            f"[auto-merged {left.id[:8]} + {right.id[:8]}]\n"
            f"{right.content}"
        )
        tags = list(dict.fromkeys([*left.tags, *right.tags, "auto-compacted"]))
        constraints = list(dict.fromkeys([*left.constraints, *right.constraints]))
        toolchain = list(dict.fromkeys([*left.toolchain, *right.toolchain]))
        task_type = cls._merge_text(left.task_type, right.task_type)
        goal = cls._merge_text(left.goal, right.goal)
        decision = cls._merge_text(left.decision, right.decision)
        outcome = cls._merge_text(left.outcome, right.outcome)
        failure_reason = cls._merge_text(left.failure_reason, right.failure_reason)
        source = "auto_compact"
        fields = {
            "task_type": task_type,
            "goal": goal,
            "constraints": constraints,
            "decision": decision,
            "outcome": outcome,
            "toolchain": toolchain,
            "failure_reason": failure_reason,
        }
        search_text = build_search_text(content=content, tags=tags, fields=fields)
        return TeamMemoryEntry(
            id=str(uuid.uuid4()),
            content=content,
            tags=tags,
            created_at=min(left.created_at, right.created_at),
            source=source,
            task_type=task_type,
            goal=goal,
            constraints=constraints,
            decision=decision,
            outcome=outcome,
            confidence=max(left.confidence, right.confidence),
            toolchain=toolchain,
            failure_reason=failure_reason,
            embedding=[],
            search_text=search_text,
            l0_summary=build_l0_summary(
                task_type=task_type,
                goal=goal,
                outcome=outcome or content,
                confidence=max(left.confidence, right.confidence),
                failure_reason=failure_reason,
            ),
            l1_overview=build_l1_overview(
                constraints=constraints,
                decision=decision,
                toolchain=toolchain,
                tags=tags,
                source=source,
            ),
            l2_detail=build_l2_detail(content=content, outcome=outcome or content),
        )

    @classmethod
    def _compact_to_limit(cls, entries: list, *, workdir: str) -> list:
        result = list(entries)
        if not cls._auto_compact_enabled():
            return result
        limit = cls._max_entries()
        while len(result) > limit:
            # Protect the newest entry and merge within a bounded window of the
            # oldest records.  This makes write cost predictable for long-lived scopes.
            candidate_count = min(64, max(2, len(result) - 1))
            candidates = result[:candidate_count]
            hydrate_embeddings(
                candidates,
                workdir=workdir,
                text_getter=lambda item: item.search_text or item.content,
            )
            best_pair = (0, 1)
            best_score = float("-inf")
            for left_index in range(len(candidates)):
                for right_index in range(left_index + 1, len(candidates)):
                    score = cosine_similarity(
                        candidates[left_index].embedding,
                        candidates[right_index].embedding,
                    )
                    if score > best_score:
                        best_score = score
                        best_pair = (left_index, right_index)
            left_index, right_index = best_pair
            merged = cls._merge_pair(
                result[left_index],
                result[right_index],
                workdir=workdir,
            )
            result = [
                item
                for index, item in enumerate(result)
                if index not in {left_index, right_index}
            ]
            result.insert(left_index, merged)
        return result

    @classmethod
    def add(
        cls,
        team_id: str,
        workdir: str,
        content: str,
        tags: Optional[list] = None,
        source: str = "manager",
        structured: Optional[dict] = None,
    ) -> TeamMemoryEntry:
        text = (content or "").strip()
        if not text:
            raise ValueError("Memory content cannot be empty")
        normalized = normalize_structured_fields(structured or {})
        search_text = build_search_text(content=text, tags=tags or [], fields=normalized)
        entry = TeamMemoryEntry(
            id=str(uuid.uuid4()),
            content=text,
            tags=[t.strip() for t in (tags or []) if str(t).strip()],
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
        cls._ensure(team_id, workdir)
        path = cls._path(team_id, workdir)
        with file_transaction(path):
            entries = cls._deserialize(cls._read_records(path))
            entries.append(entry)
            entries = cls._compact_to_limit(entries, workdir=workdir)
            entries = enforce_memory_budget(entries, path, cls.MEMORY_SCOPE_KIND)
            atomic_write_json(path, [persistent_memory_record(item) for item in entries])
        cls._rebuild_vector_index(entries, team_id, workdir)
        return next((item for item in entries if item.id == entry.id), entry)

    @classmethod
    def add_structured(
        cls,
        *,
        team_id: str,
        workdir: str,
        source: str = "manager",
        tags: Optional[list] = None,
        **fields,
    ) -> TeamMemoryEntry:
        normalized = normalize_structured_fields(fields)
        content = normalized["outcome"] or normalized["decision"] or normalized["goal"]
        return cls.add(
            team_id=team_id,
            workdir=workdir,
            content=content,
            tags=tags or [],
            source=source,
            structured=normalized,
        )

    @classmethod
    def query(cls, team_id: str, workdir: str, query: Optional[str] = None, limit: int = 30, level: str = "l1", filters: Optional[dict] = None) -> list:
        entries = cls.load(team_id, workdir)
        entries = apply_memory_filters(entries, filters)
        entries.reverse()  # newest first
        if query:
            entries = cls._query_semantic(team_id, workdir, entries, query)
        return cls._apply_level(entries[:max(1, min(limit, 200))], level)

    @classmethod
    def query_related(
        cls,
        team_id: str,
        workdir: str,
        *,
        task_type: Optional[str] = None,
        goal: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 10,
        level: str = "l1",
        filters: Optional[dict] = None,
    ) -> list:
        entries = cls.load(team_id, workdir)
        if task_type:
            entries = [
                e for e in entries
                if not e.task_type or e.task_type == task_type or task_type.lower() in e.task_type.lower()
            ]
        entries = apply_memory_filters(entries, filters)
        query_text = " ".join(part for part in [task_type or "", goal or "", query or ""] if part).strip()
        entries = cls._query_semantic(team_id, workdir, entries, query_text)
        return cls._apply_level(entries[:max(1, min(limit, 200))], level)

    @classmethod
    def _apply_level(cls, entries: list, level: str) -> list:
        normalized = str(level or "l1").strip().lower()
        if normalized not in {"l0", "l1", "l2"}:
            normalized = "l1"
        result = []
        for item in entries:
            clone = TeamMemoryEntry(**asdict(item))
            clone.content = choose_memory_level(normalized, item)
            result.append(clone)
        return result

    @classmethod
    def _query_semantic(cls, team_id: str, workdir: str, entries: list, query: str) -> list:
        return rank_with_vector_index(
            entries,
            query=query,
            base_path=cls._vector_base_path(team_id, workdir),
            workdir=workdir,
            id_getter=lambda item: item.id,
            text_getter=lambda item: item.search_text or item.content,
        )

    @classmethod
    def delete(cls, team_id: str, workdir: str, memory_id: str) -> bool:
        cls._ensure(team_id, workdir)
        path = cls._path(team_id, workdir)
        with file_transaction(path):
            entries = cls._deserialize(cls._read_records(path))
            before = len(entries)
            entries = [entry for entry in entries if entry.id != memory_id]
            if len(entries) == before:
                return False
            atomic_write_json(path, [persistent_memory_record(item) for item in entries])
        cls._rebuild_vector_index(entries, team_id, workdir)
        return True

    @classmethod
    def get(cls, team_id: str, workdir: str, memory_id: str, level: str = "l2") -> Optional[TeamMemoryEntry]:
        for item in cls.load(team_id, workdir):
            if item.id != memory_id:
                continue
            return cls._apply_level([item], level)[0]
        return None

    @classmethod
    def render_context(cls, team_id: str, workdir: str, limit: int = 20) -> str:
        """Render team memories as a plain-text block for system prompt injection."""
        items = cls.query(team_id, workdir, limit=limit, level="l0")
        if not items:
            return ""
        lines = []
        for e in items:
            extra_bits = []
            for key in STRUCTURED_MEMORY_KEYS:
                value = getattr(e, key, None)
                if isinstance(value, list) and value:
                    extra_bits.append(f"{key}={', '.join(str(v) for v in value)}")
                elif isinstance(value, (str, float)) and value not in {"", 0.0}:
                    extra_bits.append(f"{key}={value}")
            tag_text = f"  (tags: {', '.join(e.tags)})" if e.tags else ""
            structured_text = f" | {'; '.join(extra_bits)}" if extra_bits else ""
            lines.append(f"[{e.source}] {e.content}{tag_text}{structured_text}")
        return "\n".join(lines)
