"""SQLite persistence for Topic Candidates, memberships, and Projects."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore
from .topic_models import (
    PROJECT_STATUSES,
    TOPIC_DECISION_VERSION,
    TOPIC_SCHEMA_VERSION,
    TOPIC_STATES,
    ResearchCandidateMembership,
    ResearchProject,
    ResearchTopicCandidate,
)


_TOPIC_LOCK = threading.RLock()


class ResearchTopicStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def create_candidate(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        candidate_id: str,
        suggested_title: str,
        goal_summary: str = "",
        keywords: list[str] | None = None,
        first_seen_at: str,
        last_seen_at: str,
        state: str = "emerging",
        title_source: str = "rule",
        split_from: list[str] | None = None,
        generation: int = 1,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchTopicCandidate, bool]:
        resolved = settings or load_research_settings()
        normalized_state = str(state or "emerging").strip().lower()
        if normalized_state not in TOPIC_STATES:
            raise ValueError(f"invalid topic candidate state: {normalized_state}")
        now = utc_now_iso()
        normalized_id = str(candidate_id or "").strip()[:160]
        if not normalized_id:
            raise ValueError("candidate_id is required")
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "candidate_id": normalized_id,
                }
            )
        )
        created = False
        with _TOPIC_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO research_topic_candidates(
                        candidate_id, schema_version, tenant_id, user_id, workspace_id,
                        suggested_title, title_source, goal_summary, keywords_json,
                        state, first_seen_at, last_seen_at, split_from_json, generation,
                        decision_version, created_at, updated_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_id,
                        TOPIC_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        str(suggested_title or "未命名研究课题").strip()[:160],
                        str(title_source or "rule").strip()[:40],
                        str(goal_summary or "").strip()[:2000],
                        canonical_json(list(dict.fromkeys(keywords or []))[:32]),
                        normalized_state,
                        str(first_seen_at or now),
                        str(last_seen_at or first_seen_at or now),
                        canonical_json(list(dict.fromkeys(split_from or []))[:32]),
                        max(1, int(generation or 1)),
                        TOPIC_DECISION_VERSION,
                        now,
                        now,
                        idempotency_key,
                    ),
                )
                created = cursor.rowcount > 0
                row = conn.execute(
                    "SELECT * FROM research_topic_candidates WHERE candidate_id = ?",
                    (normalized_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None or (
            str(row["tenant_id"]) != tenant_id
            or str(row["user_id"]) != user_id
            or str(row["workspace_id"]) != workspace_id
        ):
            raise ValueError("candidate does not belong to owner")
        return ResearchTopicCandidate.from_row(row), created

    @classmethod
    def update_candidate(
        cls,
        candidate_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
        **changes: Any,
    ) -> ResearchTopicCandidate:
        resolved = settings or load_research_settings()
        allowed = {
            "suggested_title",
            "title_source",
            "goal_summary",
            "keywords_json",
            "supporting_event_count",
            "session_count",
            "artifact_count",
            "tool_count",
            "explicit_goal",
            "stability_score",
            "state",
            "first_seen_at",
            "last_seen_at",
            "merged_into",
            "split_from_json",
            "confirmed_research_id",
            "generation",
            "decision_version",
        }
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "state":
                value = str(value or "").strip().lower()
                if value not in TOPIC_STATES:
                    raise ValueError(f"invalid topic candidate state: {value}")
            if key in {"keywords_json", "split_from_json"} and not isinstance(value, str):
                value = canonical_json(list(value or []))
            if key == "explicit_goal":
                value = 1 if value else 0
            normalized[key] = value
        normalized["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        params = [*normalized.values(), candidate_id, tenant_id, user_id, workspace_id]
        with _TOPIC_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                cursor = conn.execute(
                    f"UPDATE research_topic_candidates SET {assignments} "
                    "WHERE candidate_id = ? AND tenant_id = ? AND user_id = ? "
                    "AND workspace_id = ?",
                    params,
                )
                if cursor.rowcount == 0:
                    raise ValueError("candidate does not belong to owner")
                row = conn.execute(
                    "SELECT * FROM research_topic_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
            finally:
                conn.close()
        return ResearchTopicCandidate.from_row(row)

    @classmethod
    def get_candidate(
        cls,
        candidate_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchTopicCandidate | None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                """
                SELECT * FROM research_topic_candidates
                WHERE candidate_id = ? AND tenant_id = ? AND user_id = ?
                  AND workspace_id = ?
                """,
                (candidate_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchTopicCandidate.from_row(row) if row is not None else None

    @classmethod
    def list_candidates(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        states: list[str] | None = None,
        include_inactive: bool = False,
        limit: int = 200,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchTopicCandidate]:
        resolved = settings or load_research_settings()
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        normalized_states = [
            str(item or "").strip().lower()
            for item in (states or [])
            if str(item or "").strip().lower() in TOPIC_STATES
        ]
        if normalized_states:
            clauses.append(
                "state IN (" + ", ".join("?" for _ in normalized_states) + ")"
            )
            params.extend(normalized_states)
        elif not include_inactive:
            clauses.append("state NOT IN ('merged', 'split', 'dismissed')")
        params.append(max(1, min(int(limit or 200), 1000)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_topic_candidates WHERE "
                + " AND ".join(clauses)
                + " ORDER BY CASE state WHEN 'stable' THEN 0 WHEN 'confirmed' THEN 1 "
                "ELSE 2 END, stability_score DESC, last_seen_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchTopicCandidate.from_row(row) for row in rows]

    @classmethod
    def append_membership(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        event_id: str,
        candidate_id: str,
        state: str = "active",
        decision_source: str = "topic_rule",
        confidence: float = 0.9,
        reason_code: str = "topic_cluster",
        operation_id: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchCandidateMembership, bool]:
        resolved = settings or load_research_settings()
        normalized_state = str(state or "active").strip().lower()
        if normalized_state not in {"active", "removed", "moved"}:
            raise ValueError(f"invalid candidate membership state: {normalized_state}")
        semantic = sha256_text(
            canonical_json(
                {
                    "event_id": event_id,
                    "candidate_id": candidate_id,
                    "state": normalized_state,
                    "decision_source": decision_source,
                    "reason_code": reason_code,
                    "operation_id": operation_id,
                }
            )
        )
        with _TOPIC_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                event_owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id FROM research_events "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                candidate_owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id, state "
                    "FROM research_topic_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                expected = (tenant_id, user_id, workspace_id)
                if event_owner is None or tuple(event_owner) != expected:
                    raise ValueError("membership event does not belong to owner")
                if candidate_owner is None or tuple(candidate_owner[:3]) != expected:
                    raise ValueError("membership candidate does not belong to owner")
                if normalized_state == "active" and str(candidate_owner["state"]) in {
                    "merged",
                    "split",
                    "dismissed",
                }:
                    raise ValueError("cannot assign events to an inactive candidate")
                current = conn.execute(
                    "SELECT * FROM research_candidate_memberships "
                    "WHERE event_id = ? AND superseded_by = ''",
                    (event_id,),
                ).fetchone()
                if current is not None and str(current["semantic_fingerprint"]) == semantic:
                    conn.execute("COMMIT")
                    return ResearchCandidateMembership.from_row(current), False
                revision = int(current["revision"] or 0) + 1 if current is not None else 1
                membership_id = f"topic-member-{uuid.uuid4()}"
                if current is not None:
                    conn.execute(
                        "UPDATE research_candidate_memberships SET superseded_by = ? "
                        "WHERE membership_id = ?",
                        (membership_id, str(current["membership_id"])),
                    )
                decided_at = utc_now_iso()
                idempotency_key = sha256_text(
                    canonical_json(
                        {
                            "semantic_fingerprint": semantic,
                            "revision": revision,
                        }
                    )
                )
                conn.execute(
                    """
                    INSERT INTO research_candidate_memberships(
                        membership_id, schema_version, tenant_id, user_id, workspace_id,
                        event_id, candidate_id, state, decision_source, confidence,
                        reason_code, operation_id, revision, semantic_fingerprint,
                        idempotency_key, decided_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        membership_id,
                        TOPIC_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        event_id,
                        candidate_id,
                        normalized_state,
                        str(decision_source or "topic_rule")[:40],
                        max(0.0, min(float(confidence or 0.0), 1.0)),
                        str(reason_code or "topic_cluster")[:120],
                        str(operation_id or "")[:160],
                        revision,
                        semantic,
                        idempotency_key,
                        decided_at,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_candidate_memberships "
                    "WHERE membership_id = ?",
                    (membership_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        return ResearchCandidateMembership.from_row(row), True

    @classmethod
    def list_memberships(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        candidate_id: str = "",
        event_id: str = "",
        active_only: bool = True,
        limit: int = 5000,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchCandidateMembership]:
        resolved = settings or load_research_settings()
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        if candidate_id:
            clauses.append("candidate_id = ?")
            params.append(candidate_id)
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if active_only:
            clauses.extend(["superseded_by = ''", "state = 'active'"])
        params.append(max(1, min(int(limit or 5000), 10000)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_candidate_memberships WHERE "
                + " AND ".join(clauses)
                + " ORDER BY decided_at ASC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchCandidateMembership.from_row(row) for row in rows]

    @classmethod
    def list_candidate_events(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        candidate_id: str,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEvent]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                """
                SELECT e.* FROM research_events e
                JOIN research_candidate_memberships m ON m.event_id = e.event_id
                WHERE m.tenant_id = ? AND m.user_id = ? AND m.workspace_id = ?
                  AND m.candidate_id = ? AND m.state = 'active'
                  AND m.superseded_by = ''
                ORDER BY e.occurred_at ASC, e.ingested_at ASC LIMIT ?
                """,
                (
                    tenant_id,
                    user_id,
                    workspace_id,
                    candidate_id,
                    max(1, min(int(limit or 500), 5000)),
                ),
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEvent.from_row(row) for row in rows]

    @classmethod
    def record_operation(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        action: str,
        actor_id: str,
        source_candidate_ids: list[str] | None = None,
        target_candidate_ids: list[str] | None = None,
        event_ids: list[str] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str = "",
        idempotency_key: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        resolved = settings or load_research_settings()
        occurred_at = utc_now_iso()
        normalized_key = str(idempotency_key or "").strip()[:180] or sha256_text(
            canonical_json(
                {
                    "action": action,
                    "actor_id": actor_id,
                    "sources": source_candidate_ids or [],
                    "targets": target_candidate_ids or [],
                    "events": event_ids or [],
                    "occurred_at": occurred_at,
                }
            )
        )
        operation_id = f"topic-op-{uuid.uuid4()}"
        conn = cls._connect(workdir, resolved)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO research_topic_operations(
                    operation_id, tenant_id, user_id, workspace_id, action, actor_id,
                    source_candidate_ids_json, target_candidate_ids_json,
                    event_ids_json, before_json, after_json, reason, occurred_at,
                    idempotency_key
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    str(action or "unknown")[:80],
                    str(actor_id or user_id)[:160],
                    canonical_json(list(source_candidate_ids or [])[:100]),
                    canonical_json(list(target_candidate_ids or [])[:100]),
                    canonical_json(list(event_ids or [])[:5000]),
                    canonical_json(before or {}),
                    canonical_json(after or {}),
                    str(reason or "")[:1000],
                    occurred_at,
                    normalized_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_topic_operations WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
        finally:
            conn.close()
        return cls._operation_row(row)

    @staticmethod
    def _operation_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        for key in (
            "source_candidate_ids_json",
            "target_candidate_ids_json",
            "event_ids_json",
            "before_json",
            "after_json",
        ):
            try:
                item[key.removesuffix("_json")] = json.loads(str(item.pop(key) or "[]"))
            except Exception:
                item[key.removesuffix("_json")] = {}
        return item

    @classmethod
    def list_operations(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 200,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[dict[str, Any]]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_topic_operations WHERE tenant_id = ? "
                "AND user_id = ? AND workspace_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (
                    tenant_id,
                    user_id,
                    workspace_id,
                    max(1, min(int(limit or 200), 1000)),
                ),
            ).fetchall()
        finally:
            conn.close()
        return [cls._operation_row(row) for row in rows]

    @classmethod
    def ensure_project(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        title: str,
        origin_candidate_ids: list[str] | None = None,
        research_id: str = "",
        description: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchProject, bool]:
        resolved = settings or load_research_settings()
        normalized_origins = list(dict.fromkeys(origin_candidate_ids or []))[:100]
        normalized_id = str(research_id or "").strip()[:160] or f"research-{uuid.uuid4()}"
        now = utc_now_iso()
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "research_id": normalized_id,
                }
            )
        )
        with _TOPIC_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM research_projects WHERE research_id = ?",
                    (normalized_id,),
                ).fetchone()
                created = False
                if row is None:
                    project_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM research_projects "
                            "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? "
                            "AND status != 'archived'",
                            (tenant_id, user_id, workspace_id),
                        ).fetchone()[0]
                    )
                    if project_count >= 1:
                        raise ValueError(
                            "当前 Workspace 已存在正式课题；请先归档当前课题，"
                            "再确认新的研究候选"
                        )
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO research_projects(
                            research_id, schema_version, tenant_id, user_id, workspace_id,
                            title, description, status, origin_candidate_ids_json,
                            created_at, updated_at, idempotency_key
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                        """,
                        (
                            normalized_id,
                            TOPIC_SCHEMA_VERSION,
                            tenant_id,
                            user_id,
                            workspace_id,
                            str(title or "未命名研究课题").strip()[:160],
                            str(description or "").strip()[:4000],
                            canonical_json(normalized_origins),
                            now,
                            now,
                            idempotency_key,
                        ),
                    )
                    created = cursor.rowcount > 0
                    row = conn.execute(
                        "SELECT * FROM research_projects WHERE research_id = ?",
                        (normalized_id,),
                    ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        if row is None or (
            str(row["tenant_id"]) != tenant_id
            or str(row["user_id"]) != user_id
            or str(row["workspace_id"]) != workspace_id
        ):
            raise ValueError("project does not belong to owner")
        return ResearchProject.from_row(row), created

    @classmethod
    def list_projects(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        include_archived: bool = False,
        limit: int = 200,
        offset: int = 0,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchProject]:
        resolved = settings or load_research_settings()
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        if not include_archived:
            clauses.append("status != 'archived'")
        params.append(max(1, min(int(limit or 200), 1000)))
        params.append(max(0, int(offset)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_projects WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchProject.from_row(row) for row in rows]

    @classmethod
    def get_project(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchProject | None:
        projects = cls.list_projects(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
            include_archived=True,
            limit=1000,
            workdir=workdir,
            settings=settings,
        )
        return next((item for item in projects if item.research_id == research_id), None)

    @classmethod
    def set_project_status(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        status: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchProject:
        resolved = settings or load_research_settings()
        normalized = str(status or "").strip().lower()
        if normalized not in PROJECT_STATUSES:
            raise ValueError(f"invalid project status: {normalized}")
        now = utc_now_iso()
        conn = cls._connect(workdir, resolved)
        try:
            if normalized != "archived":
                conflict = conn.execute(
                    "SELECT research_id FROM research_projects "
                    "WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                    "AND status != 'archived' AND research_id != ? LIMIT 1",
                    (tenant_id, user_id, workspace_id, research_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("当前 Workspace 已有活跃的正式课题")
            cursor = conn.execute(
                "UPDATE research_projects SET status = ?, updated_at = ?, archived_at = ? "
                "WHERE research_id = ? AND tenant_id = ? AND user_id = ? "
                "AND workspace_id = ?",
                (
                    normalized,
                    now,
                    now if normalized == "archived" else "",
                    research_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("project does not belong to owner")
            row = conn.execute(
                "SELECT * FROM research_projects WHERE research_id = ?",
                (research_id,),
            ).fetchone()
        finally:
            conn.close()
        return ResearchProject.from_row(row)

    @classmethod
    def update_project_title(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        title: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchProject:
        resolved = settings or load_research_settings()
        normalized = str(title or "").strip()[:160]
        if not normalized:
            raise ValueError("project title is required")
        conn = cls._connect(workdir, resolved)
        try:
            cursor = conn.execute(
                "UPDATE research_projects SET title = ?, updated_at = ? "
                "WHERE research_id = ? AND tenant_id = ? AND user_id = ? "
                "AND workspace_id = ?",
                (
                    normalized,
                    utc_now_iso(),
                    research_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("project does not belong to owner")
            row = conn.execute(
                "SELECT * FROM research_projects WHERE research_id = ?",
                (research_id,),
            ).fetchone()
        finally:
            conn.close()
        return ResearchProject.from_row(row)

    @classmethod
    def start_run(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        trigger_kind: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> str:
        resolved = settings or load_research_settings()
        run_id = f"topic-run-{uuid.uuid4()}"
        conn = cls._connect(workdir, resolved)
        try:
            conn.execute(
                """
                INSERT INTO research_topic_runs(
                    run_id, tenant_id, user_id, workspace_id, trigger_kind,
                    decision_version, status, started_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    str(trigger_kind or "manual")[:80],
                    TOPIC_DECISION_VERSION,
                    utc_now_iso(),
                ),
            )
        finally:
            conn.close()
        return run_id

    @classmethod
    def finish_run(
        cls,
        run_id: str,
        report: dict[str, Any],
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            conn.execute(
                """
                UPDATE research_topic_runs SET status = ?, completed_at = ?,
                    event_count = ?, group_count = ?, candidate_created_count = ?,
                    membership_changed_count = ?, merge_suggestion_count = ?,
                    errors_json = ? WHERE run_id = ?
                """,
                (
                    str(report.get("status") or "completed")[:40],
                    utc_now_iso(),
                    max(0, int(report.get("event_count") or 0)),
                    max(0, int(report.get("group_count") or 0)),
                    max(0, int(report.get("candidate_created_count") or 0)),
                    max(0, int(report.get("membership_changed_count") or 0)),
                    max(0, int(report.get("merge_suggestion_count") or 0)),
                    canonical_json(list(report.get("errors") or [])[:100]),
                    run_id,
                ),
            )
        finally:
            conn.close()

    @classmethod
    def record_merge_suggestion(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        candidate_ids: list[str],
        score: float,
        reason_code: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> bool:
        resolved = settings or load_research_settings()
        ids = sorted(set(candidate_ids))
        key = sha256_text(
            canonical_json(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "candidate_ids": ids,
                    "decision_version": TOPIC_DECISION_VERSION,
                }
            )
        )
        now = utc_now_iso()
        conn = cls._connect(workdir, resolved)
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO research_topic_merge_suggestions(
                    suggestion_id, tenant_id, user_id, workspace_id,
                    candidate_ids_json, score, reason_code, created_at,
                    updated_at, idempotency_key
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"topic-merge-{uuid.uuid4()}",
                    tenant_id,
                    user_id,
                    workspace_id,
                    canonical_json(ids),
                    max(0.0, min(float(score or 0.0), 1.0)),
                    str(reason_code or "overlapping_candidates")[:120],
                    now,
                    now,
                    key,
                ),
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    @classmethod
    def health(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            candidates = conn.execute(
                "SELECT state, COUNT(*) AS count FROM research_topic_candidates "
                "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? GROUP BY state",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            project_count = conn.execute(
                "SELECT COUNT(*) AS count FROM research_projects WHERE tenant_id = ? "
                "AND user_id = ? AND workspace_id = ? AND status != 'archived'",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
            pending_count = conn.execute(
                "SELECT COUNT(*) AS count FROM research_event_links WHERE tenant_id = ? "
                "AND user_id = ? AND workspace_id = ? AND state = 'pending' "
                "AND relevance = 'research' AND superseded_by = ''",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
            suggestion_count = conn.execute(
                "SELECT COUNT(*) AS count FROM research_topic_merge_suggestions "
                "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? AND state = 'open'",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
            last_run = conn.execute(
                "SELECT status, started_at, completed_at FROM research_topic_runs "
                "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return {
            "states": {str(row["state"]): int(row["count"] or 0) for row in candidates},
            "active_project_count": int(project_count["count"] or 0),
            "pending_research_event_count": int(pending_count["count"] or 0),
            "open_merge_suggestion_count": int(suggestion_count["count"] or 0),
            "last_run": dict(last_run) if last_run is not None else None,
        }
