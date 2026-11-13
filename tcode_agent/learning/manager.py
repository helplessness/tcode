"""Lightweight agent-learning runtime.

This module intentionally keeps learning artifacts outside the main memory store.
That gives us three benefits:
1. We can add guardrail-specific metadata without disturbing existing memory schemas.
2. Draft skills stay out of the normal skill scan path until a human promotes them.
3. Adaptive behavior remains low-risk because it is derived from a bounded store.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..config.manager import ConfigManager
from ..atomic_storage import atomic_write_json, file_transaction
from ..memory.indexing import (
    ensure_vector_index,
    persistent_memory_record,
    rank_with_vector_index,
    records_contain_embeddings,
)
from ..memory.retriever import build_search_text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _clean_text(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip()


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-").lower()
    return slug or fallback


def _extract_paths(text: str, *, limit: int = 8) -> list[str]:
    found = re.findall(r"(?:^|[\s\"'`])((?:/|~/)[^\s\"'`]+)", str(text or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in found:
        path = str(item or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
        if len(result) >= limit:
            break
    return result


@dataclass
class LearningConfig:
    enabled: bool = True
    reflection_enabled: bool = True
    reflection_ttl_days: int = 30
    reflection_max_per_scope: int = 400
    reflection_min_tool_calls: int = 1
    reflection_min_response_chars: int = 80
    flush_enabled: bool = True
    flush_ttl_days: int = 21
    skill_draft_enabled: bool = True
    skill_draft_min_success_count: int = 2
    skill_draft_min_tool_count: int = 2
    adaptive_enabled: bool = True
    adaptive_min_confidence: float = 0.55
    adaptive_max_reflections: int = 2
    adaptive_max_hints: int = 3
    adaptive_window_days: int = 45


@dataclass
class ReflectionEntry:
    id: str
    fingerprint: str
    created_at: str
    updated_at: str
    expires_at: str
    source_kind: str
    scope_kind: str
    scope_key: str
    session_id: str
    agent_name: str
    task_type: str
    goal: str
    success: bool
    confidence: float
    toolchain: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    outcome: str = ""
    next_advice: str = ""
    failure_reason: str = ""
    artifact_paths: list[str] = field(default_factory=list)
    search_text: str = ""
    embedding: list[float] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    repeat_count: int = 1
    learning_status: str = "unknown"
    verification: dict[str, Any] = field(default_factory=dict)
    verification_ref: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillDraftIndexEntry:
    id: str
    created_at: str
    updated_at: str
    scope_kind: str
    scope_key: str
    agent_name: str
    task_type: str
    fingerprint: str
    file_path: str
    success_count: int
    source_reflection_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    findings: list[dict[str, str]] = field(default_factory=list)


class LearningManager:
    """Reflection capture, compaction flush and low-risk adaptation."""

    @staticmethod
    def _scan_text(content: str, *, source: str):
        from ..prompt.security import scan_prompt_text

        return scan_prompt_text(content, source=source)

    @classmethod
    def _config(cls, workdir: Optional[str]) -> LearningConfig:
        try:
            ConfigManager.load(workdir or os.getcwd())
            raw = dict(ConfigManager.get_config_copy().get("learning", {}) or {})
        except Exception:
            raw = {}
        reflection = dict(raw.get("reflection", {}) or {})
        flush = dict(raw.get("compaction_flush", {}) or {})
        skill_drafts = dict(raw.get("skill_drafts", {}) or {})
        adaptive = dict(raw.get("adaptive_hints", {}) or {})

        def _env_bool(name: str, default: bool) -> bool:
            value = os.environ.get(name)
            if value is None:
                return default
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
            value = os.environ.get(name)
            if value is None:
                return default
            try:
                parsed = int(str(value).strip())
            except Exception:
                return default
            return max(minimum, min(maximum, parsed))

        def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
            value = os.environ.get(name)
            if value is None:
                return default
            try:
                parsed = float(str(value).strip())
            except Exception:
                return default
            return max(minimum, min(maximum, parsed))

        return LearningConfig(
            enabled=_env_bool("TCODE_LEARNING_ENABLED", bool(raw.get("enabled", True))),
            reflection_enabled=_env_bool(
                "TCODE_REFLECTION_ENABLED",
                bool(reflection.get("enabled", True)),
            ),
            reflection_ttl_days=_env_int(
                "TCODE_REFLECTION_TTL_DAYS",
                int(reflection.get("ttl_days", 30) or 30),
                1,
                365,
            ),
            reflection_max_per_scope=_env_int(
                "TCODE_REFLECTION_MAX_PER_SCOPE",
                int(reflection.get("max_per_scope", 400) or 400),
                20,
                5000,
            ),
            reflection_min_tool_calls=_env_int(
                "TCODE_REFLECTION_MIN_TOOL_CALLS",
                int(reflection.get("min_tool_calls", 1) or 1),
                0,
                20,
            ),
            reflection_min_response_chars=_env_int(
                "TCODE_REFLECTION_MIN_RESPONSE_CHARS",
                int(reflection.get("min_response_chars", 80) or 80),
                0,
                4000,
            ),
            flush_enabled=_env_bool(
                "TCODE_COMPACTION_FLUSH_ENABLED",
                bool(flush.get("enabled", True)),
            ),
            flush_ttl_days=_env_int(
                "TCODE_COMPACTION_FLUSH_TTL_DAYS",
                int(flush.get("ttl_days", 21) or 21),
                1,
                365,
            ),
            skill_draft_enabled=_env_bool(
                "TCODE_SKILL_DRAFT_ENABLED",
                bool(skill_drafts.get("enabled", True)),
            ),
            skill_draft_min_success_count=_env_int(
                "TCODE_SKILL_DRAFT_MIN_SUCCESS_COUNT",
                int(skill_drafts.get("min_success_count", 2) or 2),
                1,
                20,
            ),
            skill_draft_min_tool_count=_env_int(
                "TCODE_SKILL_DRAFT_MIN_TOOL_COUNT",
                int(skill_drafts.get("min_tool_count", 2) or 2),
                1,
                20,
            ),
            adaptive_enabled=_env_bool(
                "TCODE_ADAPTIVE_HINTS_ENABLED",
                bool(adaptive.get("enabled", True)),
            ),
            adaptive_min_confidence=_env_float(
                "TCODE_ADAPTIVE_MIN_CONFIDENCE",
                float(adaptive.get("min_confidence", 0.55) or 0.55),
                0.0,
                1.0,
            ),
            adaptive_max_reflections=_env_int(
                "TCODE_ADAPTIVE_MAX_REFLECTIONS",
                int(adaptive.get("max_reflections", 2) or 2),
                1,
                10,
            ),
            adaptive_max_hints=_env_int(
                "TCODE_ADAPTIVE_MAX_HINTS",
                int(adaptive.get("max_hints", 3) or 3),
                1,
                10,
            ),
            adaptive_window_days=_env_int(
                "TCODE_ADAPTIVE_WINDOW_DAYS",
                int(adaptive.get("window_days", 45) or 45),
                1,
                365,
            ),
        )

    @classmethod
    def _base_dir(cls, workdir: Optional[str]) -> Path:
        base = Path(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd())
        path = base / ".tcode" / "learning"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _reflections_path(cls, workdir: Optional[str]) -> Path:
        return cls._base_dir(workdir) / "reflections.json"

    @classmethod
    def _reflection_vector_base_path(cls, workdir: Optional[str]) -> Path:
        return cls._reflections_path(workdir)

    @classmethod
    def _skill_drafts_dir(cls, workdir: Optional[str]) -> Path:
        path = cls._base_dir(workdir) / "skill_drafts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _skill_draft_index_path(cls, workdir: Optional[str]) -> Path:
        return cls._base_dir(workdir) / "skill_drafts.index.json"

    @classmethod
    def _load_reflections(cls, workdir: Optional[str], *, locked: bool = False) -> list[ReflectionEntry]:
        if not locked:
            with file_transaction(cls._reflections_path(workdir)):
                return cls._load_reflections(workdir, locked=True)
        path = cls._reflections_path(workdir)
        if not path.exists():
            ensure_vector_index(
                [],
                base_path=cls._reflection_vector_base_path(workdir),
                workdir=workdir,
                id_getter=lambda item: item.id,
                text_getter=lambda item: item.search_text or item.summary,
            )
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("Reflection store is damaged; refusing to overwrite existing data")
        records = raw
        if records_contain_embeddings(records):
            raw = [persistent_memory_record(item) for item in records]
            atomic_write_json(path, raw)
        entries: list[ReflectionEntry] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    ReflectionEntry(
                        id=str(item.get("id", "")),
                        fingerprint=str(item.get("fingerprint", "")),
                        created_at=str(item.get("created_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                        expires_at=str(item.get("expires_at", "")),
                        source_kind=str(item.get("source_kind", "")),
                        scope_kind=str(item.get("scope_kind", "")),
                        scope_key=str(item.get("scope_key", "")),
                        session_id=str(item.get("session_id", "")),
                        agent_name=str(item.get("agent_name", "")),
                        task_type=str(item.get("task_type", "")),
                        goal=str(item.get("goal", "")),
                        success=bool(item.get("success", False)),
                        confidence=float(item.get("confidence", 0.0) or 0.0),
                        toolchain=list(item.get("toolchain", []) or []),
                        tags=list(item.get("tags", []) or []),
                        summary=str(item.get("summary", "")),
                        outcome=str(item.get("outcome", "")),
                        next_advice=str(item.get("next_advice", "")),
                        failure_reason=str(item.get("failure_reason", "")),
                        artifact_paths=list(item.get("artifact_paths", []) or []),
                        search_text=str(item.get("search_text", "")),
                        embedding=[],
                        success_count=int(item.get("success_count", 0) or 0),
                        failure_count=int(item.get("failure_count", 0) or 0),
                        repeat_count=int(item.get("repeat_count", 1) or 1),
                        learning_status=str(item.get("learning_status") or "unknown"),
                        verification=dict(item.get("verification") or {}),
                        verification_ref=dict(item.get("verification_ref") or {}),
                    )
                )
            except Exception as exc:
                raise ValueError("Invalid reflection record; refusing to overwrite existing data") from exc
        cls._refresh_verifications(entries, workdir)
        ensure_vector_index(
            entries,
            base_path=cls._reflection_vector_base_path(workdir),
            workdir=workdir,
            id_getter=lambda item: item.id,
            text_getter=lambda item: item.search_text or item.summary,
        )
        for item in entries:
            item.embedding = []
        return entries

    @classmethod
    def _save_reflections(cls, entries: list[ReflectionEntry], workdir: Optional[str], *, locked: bool = False) -> None:
        path = cls._reflections_path(workdir)
        with nullcontext() if locked else file_transaction(path):
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("Reflection store is damaged; refusing to overwrite existing data")
            atomic_write_json(path, [persistent_memory_record(item) for item in entries])
        ensure_vector_index(
            entries,
            base_path=cls._reflection_vector_base_path(workdir),
            workdir=workdir,
            id_getter=lambda item: item.id,
            text_getter=lambda item: item.search_text or item.summary,
        )
        for item in entries:
            item.embedding = []

    @classmethod
    def _store_reflection(cls, entry, workdir, max_per_scope):
        with file_transaction(cls._reflections_path(workdir)):
            entries = cls._load_reflections(workdir, locked=True)
            entries, stored = cls._upsert_reflection(entries, entry, workdir=workdir)
            entries = cls._prune_expired(entries, max_per_scope=max_per_scope)
            cls._save_reflections(entries, workdir, locked=True)
        return entries, stored

    @classmethod
    def _load_skill_draft_index(cls, workdir: Optional[str]) -> list[SkillDraftIndexEntry]:
        path = cls._skill_draft_index_path(workdir)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries: list[SkillDraftIndexEntry] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    SkillDraftIndexEntry(
                        id=str(item.get("id", "")),
                        created_at=str(item.get("created_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                        scope_kind=str(item.get("scope_kind", "")),
                        scope_key=str(item.get("scope_key", "")),
                        agent_name=str(item.get("agent_name", "")),
                        task_type=str(item.get("task_type", "")),
                        fingerprint=str(item.get("fingerprint", "")),
                        file_path=str(item.get("file_path", "")),
                        success_count=int(item.get("success_count", 0) or 0),
                        source_reflection_ids=list(item.get("source_reflection_ids", []) or []),
                        blocked=bool(item.get("blocked", False)),
                        findings=list(item.get("findings", []) or []),
                    )
                )
            except Exception:
                continue
        reflections = {item.id: item for item in cls._load_reflections(workdir)}
        for entry in entries:
            sources = [reflections.get(key) for key in entry.source_reflection_ids]
            if not sources or any(item is None or not item.success for item in sources):
                entry.blocked = True
                entry.success_count = 0
                entry.findings.append({"kind": "verification", "detail": "来源经验缺少当前有效验收；草稿不可作为已验证经验使用。"})
        return entries

    @classmethod
    def _save_skill_draft_index(
        cls,
        entries: list[SkillDraftIndexEntry],
        workdir: Optional[str],
    ) -> None:
        path = cls._skill_draft_index_path(workdir)
        path.write_text(
            json.dumps([asdict(item) for item in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def _prune_expired(
        cls,
        entries: list[ReflectionEntry],
        *,
        max_per_scope: int,
    ) -> list[ReflectionEntry]:
        now = _utc_now()
        alive = []
        for item in entries:
            expires_at = _parse_iso(item.expires_at)
            if expires_at and expires_at < now:
                continue
            alive.append(item)
        alive.sort(key=lambda item: item.updated_at, reverse=True)
        grouped: dict[tuple[str, str], list[ReflectionEntry]] = {}
        for item in alive:
            grouped.setdefault((item.scope_kind, item.scope_key), []).append(item)
        result: list[ReflectionEntry] = []
        for bucket in grouped.values():
            result.extend(bucket[:max_per_scope])
        result.sort(key=lambda item: item.updated_at)
        return result

    @classmethod
    def _scope_from_agent(cls, agent: Any) -> tuple[str, str]:
        shared_memory_key = str(getattr(agent, "_shared_memory_key", "") or "").strip()
        if shared_memory_key:
            return "worker", shared_memory_key
        mode = getattr(getattr(agent, "agent_info", None), "mode", None)
        mode_value = getattr(mode, "value", str(mode or "")).strip().lower()
        if mode_value == "manager":
            return "team", str(getattr(agent, "session_id", "") or "").strip()
        if mode_value == "worker":
            return "session", str(getattr(agent, "session_id", "") or "").strip()
        return "global", "global"

    @classmethod
    def _scope_matches(
        cls,
        entry: ReflectionEntry,
        *,
        scope_kind: str,
        scope_key: str,
    ) -> bool:
        if entry.scope_kind == scope_kind and entry.scope_key == scope_key:
            return True
        return entry.scope_kind == "global" and scope_kind != "global"

    @classmethod
    def _collect_toolchain(cls, messages: list[Any]) -> list[str]:
        tools: list[str] = []
        seen: set[str] = set()
        for msg in messages or []:
            for tc in getattr(msg, "tool_calls", []) or []:
                tool_name = str(getattr(tc, "tool_name", "") or "").strip()
                if not tool_name or tool_name in seen:
                    continue
                seen.add(tool_name)
                tools.append(tool_name)
        return tools

    @classmethod
    def _collect_artifact_paths(cls, messages: list[Any]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for msg in messages or []:
            for item in _extract_paths(getattr(msg, "content", "")):
                if item in seen:
                    continue
                seen.add(item)
                paths.append(item)
        return paths[:12]

    @classmethod
    def _infer_task_type(cls, *, agent_name: str, goal: str, toolchain: list[str]) -> str:
        text = f"{agent_name}\n{goal}".lower()
        if any(token in text for token in ("pdf", "论文", "表格", "图片", "ocr", "figure", "table")):
            return "paper_pdf"
        if any(token in text for token in ("paper", "pubmed", "arxiv", "文献", "检索", "综述")):
            return "paper_research"
        if any(token in text for token in ("protein", "ligand", "dock", "gnina", "rfdiffusion", "蛋白", "配体", "对接")):
            return "protein_pipeline"
        if any(token in text for token in ("bug", "test", "refactor", "代码", "文件", "脚本")):
            return "coding"
        if "bash" in toolchain or "read" in toolchain or "skill" in toolchain:
            return "execution"
        return "general"

    @classmethod
    def _build_summary(
        cls,
        *,
        task_type: str,
        goal: str,
        toolchain: list[str],
        success: bool,
        confidence: float,
        outcome: str,
        failure_reason: str,
        learning_status: str = "unknown",
    ) -> tuple[str, str]:
        tool_text = ", ".join(toolchain[:6]) if toolchain else "未调用工具"
        summary = (
            f"task_type={task_type} | status={learning_status} | "
            f"confidence={round(confidence, 3)} | tools={tool_text} | goal={goal[:120]}"
        )
        if success:
            advice = f"类似任务优先复用 {tool_text} 的顺序，尽量沿用这次的短路径。"
        elif learning_status == "failed":
            fail_text = failure_reason[:120] if failure_reason else "这次路径不稳定"
            advice = f"类似任务先规避本次失败点：{fail_text}。必要时减少探索型工具试探。"
        else:
            advice = "本次经验尚未通过当前有效验收，不作为成功路径复用。"
        return summary, advice

    @classmethod
    def _apply_verification(cls, entry: ReflectionEntry, verification: dict[str, Any]) -> None:
        """Consume host verification only; text/tool success cannot establish an outcome."""
        from ..guard.outcome_guard import public_outcome_projection
        from ..guard.proof_levels import proof_level_satisfies

        ref = entry.verification_ref
        version = ref.get("contract_version")
        bound = bool(ref.get("message_id") and ref.get("contract_id")) and type(version) is int and version > 0
        bound = bound and verification.get("contract_id") == ref.get("contract_id")
        bound = bound and verification.get("contract_version") == ref.get("contract_version")
        value = public_outcome_projection(verification) if bound else {}
        state = value.get("confirmation_state")
        status = "unknown"
        if value.get("superseded"):
            reason = "来源验收已被新契约替代"
        elif value.get("criteria_failed", 0) > 0 or state == "objective_failure":
            status, reason = "failed", "当前验收存在未通过条件"
        elif state in {"pending", "modification_requested"} or (
            value.get("needs_user_confirmation") and not value.get("user_confirmed")
        ):
            status, reason = "pending", "等待用户确认或修改后重新验收"
        elif (
            value.get("verification_status") == "VERIFIED"
            and value.get("validator") in {"deterministic", "semantic"}
            and not value.get("criteria_unverified", 0)
            and state != "repair_exhausted"
            and value.get("required_proof_level")
            and value.get("achieved_proof_level")
            and proof_level_satisfies(value["achieved_proof_level"], value["required_proof_level"])
        ):
            status, reason = "success", ""
        else:
            reason = "缺少当前有效验收，或验收尚未完成"
        entry.verification = value
        entry.learning_status = status
        entry.success = status == "success"
        try:
            confidence = float(verification.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 0.0
        entry.confidence = confidence if entry.success else 0.0
        # One bound result is one observation, even after repeated compaction/replay.
        entry.success_count = int(entry.success)
        entry.failure_count = int(status == "failed")
        entry.failure_reason = reason
        entry.summary, entry.next_advice = cls._build_summary(
            task_type=entry.task_type, goal=entry.goal, toolchain=entry.toolchain,
            success=entry.success, confidence=entry.confidence, outcome=entry.outcome,
            failure_reason=reason, learning_status=status,
        )
        if entry.success:
            entry.summary += f" | proof={value['achieved_proof_level']}/{value['required_proof_level']}"
        entry.search_text = cls._reflection_search_text(entry)

    @classmethod
    def _refresh_verifications(cls, entries: list[ReflectionEntry], workdir: Optional[str]) -> None:
        """Re-read canonical messages so later confirmation/revision takes effect locally."""
        from ..session.store_sqlite import SessionSQLiteStore

        sessions: dict[tuple, dict[str, Any]] = {}
        for entry in entries:
            ref = entry.verification_ref
            root = str(ref.get("storage_root") or "")
            owner = dict(ref.get("owner") or {})
            key = (root, entry.session_id, tuple(sorted(owner.items())))
            if key not in sessions:
                snapshot = None
                try:
                    if root and SessionSQLiteStore.db_path(root).is_file():
                        snapshot = SessionSQLiteStore.get(entry.session_id, root, **owner)
                except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
                    pass  # Unreadable validation must never preserve a success label.
                sessions[key] = snapshot or {}
            snapshot = sessions[key]
            verification = {}
            if snapshot and Path(snapshot.get("workdir") or root).resolve() == Path(workdir or os.getcwd()).resolve():
                message = next((item for item in snapshot.get("messages", [])
                                if item.get("id") == ref.get("message_id")), None)
                if message and message.get("role") == "assistant" and not message.get("error") and hashlib.sha256(
                    str(message.get("content") or "").encode("utf-8")
                ).hexdigest() == ref.get("content_sha256"):
                    verification = dict(message.get("verification") or {})
            cls._apply_verification(entry, verification)

    @classmethod
    def _build_fingerprint(
        cls,
        *,
        scope_kind: str,
        scope_key: str,
        agent_name: str,
        task_type: str,
        goal: str,
        toolchain: list[str],
    ) -> str:
        normalized_goal = re.sub(r"\s+", " ", goal.lower()).strip()[:240]
        payload = "|".join(
            [
                scope_kind,
                scope_key,
                agent_name,
                task_type,
                normalized_goal,
                ",".join(sorted(toolchain)),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _reflection_search_text(cls, entry: ReflectionEntry) -> str:
        return build_search_text(
            content="\n".join(
                part
                for part in [
                    entry.goal,
                    entry.summary,
                    entry.outcome,
                    entry.next_advice,
                    " ".join(entry.artifact_paths),
                ]
                if part
            ),
            tags=entry.tags,
            fields={
                "task_type": entry.task_type,
                "agent_name": entry.agent_name,
                "toolchain": entry.toolchain,
                "success": str(entry.success).lower(),
                "failure_reason": entry.failure_reason,
            },
        )

    @classmethod
    def _build_reflection_entry(
        cls,
        *,
        workdir: Optional[str],
        scope_kind: str,
        scope_key: str,
        session_id: str,
        agent_name: str,
        goal: str,
        final_output: str,
        toolchain: list[str],
        source_kind: str,
        tags: Optional[list[str]] = None,
        ttl_days: int,
        messages: list[Any],
        final_message: Any = None,
        source_session: Any = None,
    ) -> Optional[ReflectionEntry]:
        task_type = cls._infer_task_type(
            agent_name=agent_name,
            goal=goal,
            toolchain=toolchain,
        )
        artifact_paths = cls._collect_artifact_paths(messages)
        from ..session.store_sqlite import SessionSQLiteStore

        verification = dict(getattr(final_message, "verification", {}) or {})
        if getattr(final_message, "role", "") != "assistant" or getattr(final_message, "error", None):
            verification = {}
        verification_ref = {
            "message_id": str(getattr(final_message, "id", "") or ""),
            "content_sha256": hashlib.sha256(str(getattr(final_message, "content", "") or "").encode("utf-8")).hexdigest(),
            "contract_id": str(verification.get("contract_id") or ""),
            "contract_version": verification.get("contract_version"),
            "storage_root": str(SessionSQLiteStore.db_path().resolve().parent.parent),
            "owner": {key: str(getattr(source_session, key)) for key in ("tenant_id", "user_id", "workspace_id")
                      if getattr(source_session, key, None)},
        }
        fingerprint = cls._build_fingerprint(
            scope_kind=scope_kind,
            scope_key=scope_key,
            agent_name=agent_name,
            task_type=task_type,
            goal=goal,
            toolchain=toolchain,
        )
        now = _utc_now()
        entry = ReflectionEntry(
            id=str(uuid.uuid4()),
            fingerprint=fingerprint,
            created_at=_to_iso(now),
            updated_at=_to_iso(now),
            expires_at=_to_iso(now + timedelta(days=max(1, ttl_days))),
            source_kind=source_kind,
            scope_kind=scope_kind,
            scope_key=scope_key,
            session_id=session_id,
            agent_name=agent_name,
            task_type=task_type,
            goal=_clean_text(goal, limit=400),
            success=False,
            confidence=0.0,
            toolchain=list(toolchain),
            tags=list(tags or []),
            outcome=_clean_text(final_output, limit=800),
            artifact_paths=artifact_paths,
            verification_ref=verification_ref,
        )
        cls._apply_verification(entry, verification)
        entry.embedding = []

        scan = cls._scan_text(
            "\n".join(
                part
                for part in [entry.goal, entry.summary, entry.outcome, entry.next_advice]
                if part
            ),
            source=f"learning:{source_kind}:{agent_name}",
        )
        if scan.blocked:
            return None
        return entry

    @classmethod
    def _substantial_enough(
        cls,
        *,
        final_output: str,
        toolchain: list[str],
        min_tool_calls: int,
        min_response_chars: int,
        source_kind: str,
    ) -> bool:
        if len(toolchain) >= max(0, min_tool_calls):
            return True
        if len(str(final_output or "")) >= max(0, min_response_chars):
            return True
        return source_kind == "compaction_flush"

    @classmethod
    def _upsert_reflection(
        cls,
        entries: list[ReflectionEntry],
        entry: ReflectionEntry,
        *,
        workdir: Optional[str],
    ) -> tuple[list[ReflectionEntry], ReflectionEntry]:
        now = _utc_now_iso()
        for index, existing in enumerate(entries):
            if not entry.verification_ref.get("message_id") or (
                existing.session_id != entry.session_id
                or existing.verification_ref.get("message_id") != entry.verification_ref.get("message_id")
                or existing.verification_ref.get("storage_root") != entry.verification_ref.get("storage_root")
            ):
                continue
            entry.id = existing.id
            entry.created_at = existing.created_at
            entry.updated_at = now
            entry.repeat_count = existing.repeat_count + 1
            # A compaction flush must not change a result's original memory scope.
            entry.scope_kind, entry.scope_key = existing.scope_kind, existing.scope_key
            entries[index] = entry
            return entries, entry

        entries.append(entry)
        return entries, entry

    @classmethod
    def capture_turn(
        cls,
        *,
        agent: Any,
        user_message: str,
        final_message: Any,
        turn_messages: list[Any],
        source_kind: str = "turn_complete",
    ) -> Optional[ReflectionEntry]:
        config = cls._config(getattr(agent, "workdir", None))
        if not (config.enabled and config.reflection_enabled):
            return None

        final_output = _clean_text(getattr(final_message, "content", ""))
        toolchain = cls._collect_toolchain(turn_messages)
        if not cls._substantial_enough(
            final_output=final_output,
            toolchain=toolchain,
            min_tool_calls=config.reflection_min_tool_calls,
            min_response_chars=config.reflection_min_response_chars,
            source_kind=source_kind,
        ):
            return None

        scope_kind, scope_key = cls._scope_from_agent(agent)
        workdir = getattr(agent, "workdir", None)
        entry = cls._build_reflection_entry(
            workdir=workdir,
            scope_kind=scope_kind,
            scope_key=scope_key,
            session_id=str(getattr(agent, "session_id", "") or "").strip(),
            agent_name=str(getattr(getattr(agent, "agent_info", None), "name", "") or "").strip() or "unknown",
            goal=_clean_text(user_message, limit=500),
            final_output=final_output,
            toolchain=toolchain,
            source_kind=source_kind,
            tags=[scope_kind, "reflection"],
            ttl_days=config.reflection_ttl_days,
            messages=turn_messages,
            final_message=final_message,
            source_session=getattr(agent, "_existing_session", None),
        )
        if not entry:
            return None

        entries, stored = cls._store_reflection(entry, workdir, config.reflection_max_per_scope)
        cls._maybe_generate_skill_draft(
            workdir=workdir,
            config=config,
            entry=stored,
            reflections=entries,
        )
        return stored

    @classmethod
    def flush_before_compaction(
        cls,
        *,
        session: Any,
        messages: list[Any],
    ) -> Optional[ReflectionEntry]:
        workdir = getattr(session, "workdir", None)
        config = cls._config(workdir)
        if not (config.enabled and config.flush_enabled):
            return None

        from ..message_context import is_real_user_message

        start = next((i for i in range(len(messages) - 1, -1, -1) if is_real_user_message(messages[i])), None)
        if start is None:
            return None
        turn_messages = messages[start:]
        final_message = next((msg for msg in reversed(turn_messages)
                              if getattr(msg, "role", "") == "assistant"
                              and not getattr(msg, "tool_calls", [])
                              and not getattr(msg, "summary", False)), None)
        if final_message is None:
            return None
        goal = getattr(messages[start], "content", "")
        final_output = getattr(final_message, "content", "")
        toolchain = cls._collect_toolchain(turn_messages)
        if not cls._substantial_enough(
            final_output=final_output,
            toolchain=toolchain,
            min_tool_calls=1,
            min_response_chars=120,
            source_kind="compaction_flush",
        ):
            return None

        shared_memory_key = str(
            getattr(session, "metadata", {}).get("shared_memory_key", "") if isinstance(getattr(session, "metadata", {}), dict) else ""
        ).strip()
        if shared_memory_key:
            scope_kind, scope_key = "worker", shared_memory_key
        else:
            scope_kind, scope_key = "session", str(getattr(session, "id", "") or "").strip()

        entry = cls._build_reflection_entry(
            workdir=workdir,
            scope_kind=scope_kind,
            scope_key=scope_key,
            session_id=str(getattr(session, "id", "") or "").strip(),
            agent_name=str(getattr(session, "agent_name", "") or "").strip() or "unknown",
            goal=_clean_text(goal, limit=500),
            final_output=_clean_text(final_output, limit=800),
            toolchain=toolchain,
            source_kind="compaction_flush",
            tags=[scope_kind, "reflection", "flush"],
            ttl_days=config.flush_ttl_days,
            messages=turn_messages,
            final_message=final_message,
            source_session=session,
        )
        if not entry:
            return None

        entries, stored = cls._store_reflection(entry, workdir, config.reflection_max_per_scope)
        cls._maybe_generate_skill_draft(
            workdir=workdir,
            config=config,
            entry=stored,
            reflections=entries,
        )
        return stored

    @classmethod
    def delete_scope(
        cls,
        *,
        workdir: Optional[str],
        scope_kind: str,
        scope_key: str,
    ) -> int:
        """删除一个精确 Learning Scope，不影响跨 Session 的 Worker 反思。"""

        normalized_kind = str(scope_kind or "").strip().lower()
        normalized_key = str(scope_key or "").strip()
        if not normalized_kind or not normalized_key:
            return 0
        with file_transaction(cls._reflections_path(workdir)):
            entries = cls._load_reflections(workdir, locked=True)
            retained = [
                item for item in entries
                if not (item.scope_kind == normalized_kind and item.scope_key == normalized_key)
            ]
            deleted = len(entries) - len(retained)
            if deleted:
                cls._save_reflections(retained, workdir, locked=True)
        return deleted

    @classmethod
    def _recent_reflections_for_scope(
        cls,
        *,
        workdir: Optional[str],
        scope_kind: str,
        scope_key: str,
        goal: str,
        max_items: int,
        min_confidence: float,
        window_days: int,
    ) -> list[ReflectionEntry]:
        entries = cls._load_reflections(workdir)
        cutoff = _utc_now() - timedelta(days=max(1, window_days))
        candidates = [
            item
            for item in entries
            if item.success
            and item.confidence >= min_confidence
            and cls._scope_matches(item, scope_kind=scope_kind, scope_key=scope_key)
            and (_parse_iso(item.updated_at) or _utc_now()) >= cutoff
        ]
        if not candidates:
            return []
        ranked = rank_with_vector_index(
            candidates,
            query=goal or "general execution",
            base_path=cls._reflection_vector_base_path(workdir),
            workdir=workdir,
            id_getter=lambda item: item.id,
            text_getter=lambda item: item.search_text or item.summary,
        )
        return ranked[:max_items]

    @classmethod
    def _derive_adaptive_hints(
        cls,
        reflections: list[ReflectionEntry],
        *,
        max_hints: int,
    ) -> list[str]:
        hints: list[str] = []
        preferred_tools: list[str] = []
        avoided_tools: list[str] = []

        for item in reflections:
            for tool in item.toolchain:
                if tool not in preferred_tools:
                    preferred_tools.append(tool)
            if item.failure_reason:
                if "webfetch" in item.toolchain and "file://" in item.failure_reason.lower():
                    avoided_tools.append("webfetch(file://...)")

        if preferred_tools:
            hints.append(f"优先复用近期成功路径里的工具顺序：{', '.join(preferred_tools[:6])}。")
        if avoided_tools:
            hints.append(f"尽量规避近期失败过的探索路径：{', '.join(dict.fromkeys(avoided_tools))}。")

        for item in reflections:
            if item.next_advice and item.next_advice not in hints:
                hints.append(item.next_advice)
            if len(hints) >= max_hints:
                break

        deduped: list[str] = []
        seen: set[str] = set()
        for hint in hints:
            normalized = hint.strip()
            if not normalized or normalized in seen:
                continue
            scan = cls._scan_text(normalized, source="learning:adaptive_hint")
            if scan.blocked:
                continue
            seen.add(normalized)
            deduped.append(normalized)
            if len(deduped) >= max_hints:
                break
        return deduped

    @classmethod
    def build_prompt_sections(cls, agent: Any, goal: str) -> list[tuple[str, str, str]]:
        config = cls._config(getattr(agent, "workdir", None))
        if not (config.enabled and config.adaptive_enabled):
            return []

        scope_kind, scope_key = cls._scope_from_agent(agent)
        reflections = cls._recent_reflections_for_scope(
            workdir=getattr(agent, "workdir", None),
            scope_kind=scope_kind,
            scope_key=scope_key,
            goal=goal,
            max_items=config.adaptive_max_reflections,
            min_confidence=config.adaptive_min_confidence,
            window_days=config.adaptive_window_days,
        )
        if not reflections:
            return []

        sections: list[tuple[str, str, str]] = []
        reflection_lines = [
            f"- [{item.agent_name}/{item.task_type}] {item.summary}"
            for item in reflections
            if item.summary
        ]
        if reflection_lines:
            content = "\n".join(reflection_lines[: config.adaptive_max_reflections])
            sections.append(
                (
                    "Reflection Memory",
                    content,
                    "这些经验仅通过原任务契约的验收，不代表科学结论成立或工具顺序普适。可参考，不作为硬约束。",
                )
            )

        hints = cls._derive_adaptive_hints(
            reflections,
            max_hints=config.adaptive_max_hints,
        )
        if hints:
            sections.append(
                (
                    "Adaptive Hints",
                    "\n".join(f"- {item}" for item in hints),
                    "这些提示来自近期高置信反思，只影响工具优先级和执行顺序，不会改变权限边界。",
                )
            )
        return sections

    @classmethod
    def _skill_draft_bucket(
        cls,
        *,
        reflections: list[ReflectionEntry],
        entry: ReflectionEntry,
    ) -> list[ReflectionEntry]:
        matched = [
            item
            for item in reflections
            if item.scope_kind == entry.scope_kind
            and item.scope_key == entry.scope_key
            and item.agent_name == entry.agent_name
            and item.task_type == entry.task_type
            and item.success and item.learning_status == "success"
        ]
        matched.sort(key=lambda item: item.updated_at, reverse=True)
        return matched

    @classmethod
    def _build_skill_draft_content(
        cls,
        *,
        entry: ReflectionEntry,
        matched: list[ReflectionEntry],
    ) -> str:
        toolchain = ", ".join(entry.toolchain[:8]) or "按最短路径选择确定性工具"
        example_goal = entry.goal or "处理相似的执行型任务"
        advice = "\n".join(
            f"- {item.next_advice}"
            for item in matched[:3]
            if item.next_advice
        ) or "- 优先沿用近期成功的短路径，不要无谓试探。"
        return (
            "---\n"
            f"name: auto-draft-{_slugify(entry.task_type or entry.agent_name, 'draft')}\n"
            "description: 自动沉淀的草稿技能，默认不启用，需人工审核后再转正。\n"
            "status: draft\n"
            "auto_generated: true\n"
            f"agent: {entry.agent_name}\n"
            f"task_type: {entry.task_type}\n"
            "---\n\n"
            "# 何时使用\n\n"
            f"- 任务与以下目标相似：`{example_goal}`\n"
            f"- 当前常驻执行者是 `{entry.agent_name}`，且需要复用近期稳定流程。\n\n"
            "# 稳定流程\n\n"
            f"1. 先识别目标与产物路径。\n"
            f"2. 优先使用这条近期成功工具链：`{toolchain}`。\n"
            "3. 一旦产物已经足够回答，立即收束，不要继续探索型调用。\n\n"
            "# 护栏\n\n"
            "- 这是草稿技能，不要把它当成硬规则覆盖现有系统 prompt。\n"
            "- 不改变权限，不擅自扩大工具范围。\n"
            "- 成功仅限来源任务的验收范围，不代表科研结论成立或该工具链已被独立验证。\n"
            "- 若当前输入与草稿上下文不匹配，应回退到正常执行逻辑。\n\n"
            "# 近期反思摘要\n\n"
            f"{advice}\n"
        )

    @classmethod
    def _maybe_generate_skill_draft(
        cls,
        *,
        workdir: Optional[str],
        config: LearningConfig,
        entry: ReflectionEntry,
        reflections: list[ReflectionEntry],
    ) -> Optional[SkillDraftIndexEntry]:
        if not (config.enabled and config.skill_draft_enabled):
            return None
        if not entry.success or entry.learning_status != "success":
            return None
        if len(entry.toolchain) < config.skill_draft_min_tool_count:
            return None

        matched = cls._skill_draft_bucket(reflections=reflections, entry=entry)
        success_total = len({(item.verification_ref.get("storage_root"), item.session_id,
                              item.verification_ref.get("message_id")) for item in matched})
        if success_total < config.skill_draft_min_success_count:
            return None

        draft_content = cls._build_skill_draft_content(entry=entry, matched=matched)
        scan = cls._scan_text(draft_content, source=f"learning:skill_draft:{entry.agent_name}")
        index_entries = cls._load_skill_draft_index(workdir)

        fingerprint = entry.fingerprint
        existing = next((item for item in index_entries if item.fingerprint == fingerprint), None)
        now = _utc_now_iso()
        draft_dir = cls._skill_drafts_dir(workdir) / _slugify(entry.agent_name, "agent") / _slugify(entry.task_type, "task")
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_path = draft_dir / "SKILL.md"

        if not scan.blocked:
            draft_path.write_text(scan.sanitized_content or draft_content, encoding="utf-8")

        source_ids = [item.id for item in matched[:6]]
        if existing:
            existing.updated_at = now
            existing.success_count = success_total
            existing.source_reflection_ids = source_ids
            existing.blocked = scan.blocked
            existing.findings = [
                {"kind": finding.kind, "detail": finding.detail}
                for finding in scan.findings
            ]
            existing.file_path = draft_path.as_posix()
            cls._save_skill_draft_index(index_entries, workdir)
            return existing

        record = SkillDraftIndexEntry(
            id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            scope_kind=entry.scope_kind,
            scope_key=entry.scope_key,
            agent_name=entry.agent_name,
            task_type=entry.task_type,
            fingerprint=fingerprint,
            file_path=draft_path.as_posix(),
            success_count=success_total,
            source_reflection_ids=source_ids,
            blocked=scan.blocked,
            findings=[
                {"kind": finding.kind, "detail": finding.detail}
                for finding in scan.findings
            ],
        )
        index_entries.append(record)
        cls._save_skill_draft_index(index_entries, workdir)
        return record
