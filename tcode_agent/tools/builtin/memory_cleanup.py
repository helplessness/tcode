"""Memory cleanup tool — manual dedup, archive, and stats for global memory."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult


class MemoryCleanupParameters(ParameterSchema):
    action: str  # stats | deduplicate | archive
    archive_days: int | None = 90  # days since last update for archive action
    similarity_threshold: float | None = 0.85  # threshold for deduplicate action


class MemoryCleanupTool(Tool):
    @property
    def id(self) -> str:
        return "memory_cleanup"

    @property
    def description(self) -> str:
        return """Manage global memory storage (stats, deduplicate, archive).

Actions:
- stats: show memory count, size, age distribution, source breakdown
- deduplicate: merge near-duplicate entries (similarity > threshold)
- archive: move old untagged entries to memory_archive.json

The archive preserves knowledge without deleting it — archived entries
can be restored by moving them back to memory.json."""

    @property
    def parameters(self) -> type:
        return MemoryCleanupParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = MemoryCleanupParameters(**args)
        action = str(parsed.action or "").strip().lower()
        workdir = context.extra.get("workdir") if context.extra else None

        if action == "stats":
            return self._action_stats(workdir)
        if action == "deduplicate":
            return self._action_deduplicate(
                workdir,
                threshold=parsed.similarity_threshold or 0.85,
            )
        if action == "archive":
            return self._action_archive(
                workdir,
                archive_days=parsed.archive_days or 90,
            )
        return ToolResult(
            title="memory_cleanup",
            output="",
            error=f"Unsupported action: {parsed.action}. Use stats, deduplicate, or archive.",
        )

    @staticmethod
    def _memory_path(workdir: str) -> str:
        base = workdir or os.getcwd()
        return os.path.join(base, ".tcode", "memory.json")

    @staticmethod
    def _archive_path(workdir: str) -> str:
        base = workdir or os.getcwd()
        return os.path.join(base, ".tcode", "memory_archive.json")

    def _load_memories(self, workdir: str) -> list[dict]:
        path = self._memory_path(workdir)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def _save_memories(self, memories: list[dict], workdir: str) -> None:
        path = self._memory_path(workdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(memories, f, ensure_ascii=False, indent=2)

    def _calculate_similarity(self, a: dict, b: dict) -> float:
        """Simple token-overlap similarity for dedup detection."""
        text_a = f"{a.get('content', '')} {a.get('goal', '')} {a.get('task_type', '')}"
        text_b = f"{b.get('content', '')} {b.get('goal', '')} {b.get('task_type', '')}"
        tokens_a = set(text_a.lower().split())
        tokens_b = set(text_b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / max(len(union), 1)

    def _action_stats(self, workdir: str) -> ToolResult:
        memories = self._load_memories(workdir)
        if not memories:
            return ToolResult(
                title="Memory Stats",
                output="No memories found.",
                metadata={"count": 0},
            )

        path = self._memory_path(workdir)
        file_size = os.path.getsize(path) if os.path.exists(path) else 0

        sources: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        now = datetime.utcnow()

        for m in memories:
            src = str(m.get("source", "unknown")).split(",")[0].strip()
            sources[src] = sources.get(src, 0) + 1
            for tag in (m.get("tags") or []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

            updated = m.get("updated_at", "")
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age_days = (now - dt.replace(tzinfo=None)).days
            except Exception:
                age_days = 0
            age_bucket = (
                "last_week" if age_days <= 7
                else "last_month" if age_days <= 30
                else "last_quarter" if age_days <= 90
                else "older"
            )
            sources.setdefault(f"_age_{age_bucket}", 0)
            sources[f"_age_{age_bucket}"] = sources.get(f"_age_{age_bucket}", 0) + 1

        output_lines = [
            f"Total entries: {len(memories)}",
            f"File size: {file_size / 1024:.1f} KB",
            "",
            "Sources:",
        ]
        for src, count in sorted(sources.items(), key=lambda x: x[1], reverse=True):
            if not src.startswith("_age_"):
                output_lines.append(f"  {src}: {count}")
        output_lines.append("")
        output_lines.append("Age distribution:")
        for bucket in ["last_week", "last_month", "last_quarter", "older"]:
            count = sources.get(f"_age_{bucket}", 0)
            output_lines.append(f"  {bucket}: {count}")
        output_lines.append("")
        output_lines.append(f"Top tags: {', '.join(f'{t}({c})' for t, c in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10])}")

        return ToolResult(
            title="Memory Stats",
            output="\n".join(output_lines),
            metadata={
                "count": len(memories),
                "file_size_bytes": file_size,
                "sources": {k: v for k, v in sources.items() if not k.startswith("_age_")},
                "age_distribution": {k.replace("_age_", ""): sources.get(k, 0) for k in ["_age_last_week", "_age_last_month", "_age_last_quarter", "_age_older"]},
                "top_tags": dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
            },
        )

    def _action_deduplicate(
        self,
        workdir: str,
        threshold: float = 0.85,
    ) -> ToolResult:
        memories = self._load_memories(workdir)
        if len(memories) < 2:
            return ToolResult(
                title="Memory Dedup",
                output="Too few entries to deduplicate.",
                metadata={"merged": 0},
            )

        threshold = max(0.5, min(threshold, 0.99))
        merged_count = 0
        skipped = set()
        result: list[dict] = []

        for i, a in enumerate(memories):
            if i in skipped:
                continue
            best_j = -1
            best_sim = 0.0
            for j in range(i + 1, len(memories)):
                if j in skipped:
                    continue
                sim = self._calculate_similarity(a, memories[j])
                if sim > best_sim:
                    best_sim = sim
                    best_j = j

            if best_sim >= threshold and best_j >= 0:
                b = memories[best_j]
                merged = dict(a)
                merged["content"] = f"{a['content']}\n[dedup merged] {b['content']}"
                merged["tags"] = list(set((a.get("tags") or []) + (b.get("tags") or [])))
                merged["updated_at"] = datetime.utcnow().isoformat() + "Z"
                result.append(merged)
                skipped.add(best_j)
                merged_count += 1
            else:
                result.append(a)

        if merged_count > 0:
            self._save_memories(result, workdir)

        return ToolResult(
            title="Memory Dedup",
            output=f"Merged {merged_count} near-duplicate pairs (similarity >= {threshold:.0%}).\nBefore: {len(memories)} entries → After: {len(result)} entries.",
            metadata={"merged": merged_count, "before": len(memories), "after": len(result), "threshold": threshold},
        )

    def _action_archive(
        self,
        workdir: str,
        archive_days: int = 90,
    ) -> ToolResult:
        memories = self._load_memories(workdir)
        now = datetime.utcnow()
        archive_days = max(7, min(archive_days, 365))

        keep = []
        archive = []
        for m in memories:
            updated = m.get("updated_at", "")
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age_days = (now - dt.replace(tzinfo=None)).days
            except Exception:
                age_days = 0

            tags = m.get("tags") or []
            if age_days > archive_days and not tags:
                archive.append(m)
            else:
                keep.append(m)

        if not archive:
            return ToolResult(
                title="Memory Archive",
                output=f"No entries qualify for archive (>{archive_days} days old, no tags).",
                metadata={"archived": 0},
            )

        # Save archive
        archive_path = self._archive_path(workdir)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        existing = []
        if os.path.exists(archive_path):
            try:
                with open(archive_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        existing.extend(archive)
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        # Save trimmed memories
        self._save_memories(keep, workdir)

        return ToolResult(
            title="Memory Archive",
            output=f"Archived {len(archive)} entries (>{archive_days} days old, no tags).\nBefore: {len(memories)} entries → After: {len(keep)} entries.\nArchive saved to .tcode/memory_archive.json ({len(existing)} total archived).",
            metadata={
                "archived": len(archive),
                "before": len(memories),
                "after": len(keep),
                "archive_total": len(existing),
                "archive_path": str(Path(workdir) / ".tcode" / "memory_archive.json"),
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())