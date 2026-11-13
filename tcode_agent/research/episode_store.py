"""Owner-scoped persistence for research derivations and change history."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .episode_models import (
    CHECKPOINT_GENERATOR_VERSION,
    CONFLICT_STATUSES,
    CONFLICT_TYPES,
    DERIVATION_SCHEMA_VERSION,
    EPISODE_DERIVATION_VERSION,
    EPISODE_OUTCOMES,
    FINDING_STATUSES,
    PROVENANCE_TYPES,
    RESOLUTION_RESULTS,
    ResearchCheckpoint,
    ResearchConflict,
    ResearchEpisode,
    ResearchFinding,
    ResearchResolution,
)
from .models import ResearchEvent, canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore
from .topic_models import ResearchProject


_DERIVATION_LOCK = threading.RLock()


def _unique(values: list[str] | None, limit: int = 5000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        )
    )[:limit]


class ResearchDerivationStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

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
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_projects WHERE research_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                (research_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchProject.from_row(row) if row is not None else None

    @classmethod
    def list_project_events(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 5000,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEvent]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                """
                SELECT e.* FROM research_events e
                WHERE e.tenant_id = ? AND e.user_id = ? AND e.workspace_id = ?
                  AND (
                    EXISTS (
                      SELECT 1 FROM research_event_links l
                      WHERE l.event_id = e.event_id
                        AND l.tenant_id = e.tenant_id
                        AND l.user_id = e.user_id
                        AND l.workspace_id = e.workspace_id
                        AND l.research_id = ? AND l.state = 'included'
                        AND l.relevance = 'research' AND l.superseded_by = ''
                    )
                    OR EXISTS (
                      SELECT 1 FROM research_object_scopes s
                      WHERE s.object_type = 'event' AND s.object_id = e.event_id
                        AND s.tenant_id = e.tenant_id
                        AND s.user_id = e.user_id
                        AND s.workspace_id = e.workspace_id
                        AND s.research_id = ? AND s.state = 'active'
                        AND s.superseded_by = ''
                    )
                  )
                ORDER BY e.occurred_at ASC, e.ingested_at ASC LIMIT ?
                """,
                (
                    tenant_id,
                    user_id,
                    workspace_id,
                    research_id,
                    research_id,
                    max(1, min(int(limit or 5000), 10000)),
                ),
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEvent.from_row(row) for row in rows]

    @classmethod
    def list_episodes(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        include_inactive: bool = False,
        limit: int = 200,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEpisode]:
        resolved = settings or load_research_settings()
        clauses = [
            "tenant_id = ?",
            "user_id = ?",
            "workspace_id = ?",
            "research_id = ?",
        ]
        params: list[Any] = [tenant_id, user_id, workspace_id, research_id]
        if not include_inactive:
            clauses.append("state = 'active'")
        params.append(max(1, min(int(limit or 200), 1000)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_episodes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY started_at DESC, revision DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEpisode.from_row(row) for row in rows]

    @classmethod
    def get_episode(
        cls,
        episode_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchEpisode | None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_episodes WHERE episode_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                (episode_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchEpisode.from_row(row) if row is not None else None

    @classmethod
    def append_episode(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        episode_key: str,
        title: str,
        goal: dict[str, Any],
        summary: dict[str, Any],
        outcome_status: str,
        event_ids: list[str],
        session_ids: list[str],
        trace_ids: list[str],
        artifact_refs: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        failed_paths: list[dict[str, Any]],
        open_questions: list[dict[str, Any]],
        next_actions: list[dict[str, Any]],
        started_at: str,
        ended_at: str,
        boundary_reason: str,
        source_digest: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchEpisode, bool]:
        resolved = settings or load_research_settings()
        normalized_outcome = str(outcome_status or "unknown").strip().lower()
        if normalized_outcome not in EPISODE_OUTCOMES:
            normalized_outcome = "unknown"
        normalized_events = _unique(event_ids)
        if not normalized_events:
            raise ValueError("episode requires source events")
        now = utc_now_iso()
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "episode_key": episode_key,
                    "source_digest": source_digest,
                    "derivation_version": EPISODE_DERIVATION_VERSION,
                }
            )
        )
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id FROM research_projects "
                    "WHERE research_id = ?",
                    (research_id,),
                ).fetchone()
                if owner is None or tuple(owner) != (tenant_id, user_id, workspace_id):
                    raise ValueError("project does not belong to owner")
                current = conn.execute(
                    "SELECT * FROM research_episodes WHERE episode_key = ? "
                    "AND state = 'active'",
                    (episode_key,),
                ).fetchone()
                if current is not None and str(current["source_digest"]) == source_digest:
                    conn.execute("COMMIT")
                    return ResearchEpisode.from_row(current), False
                existing = conn.execute(
                    "SELECT * FROM research_episodes WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return ResearchEpisode.from_row(existing), False
                revision = int(current["revision"] or 0) + 1 if current else 1
                episode_id = f"episode-{uuid.uuid4()}"
                supersedes = str(current["episode_id"]) if current else ""
                if current is not None:
                    conn.execute(
                        "UPDATE research_episodes SET state = 'superseded' "
                        "WHERE episode_id = ?",
                        (supersedes,),
                    )
                conn.execute(
                    """
                    INSERT INTO research_episodes(
                        episode_id, episode_key, schema_version, tenant_id, user_id,
                        workspace_id, research_id, revision, title, goal_json,
                        summary_json, outcome_status, event_ids_json, session_ids_json,
                        trace_ids_json, artifact_refs_json, tool_calls_json,
                        decisions_json, failed_paths_json, open_questions_json,
                        next_actions_json, started_at, ended_at, boundary_reason,
                        derivation_version, source_digest, state,
                        supersedes_episode_id, created_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        episode_id,
                        episode_key,
                        DERIVATION_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        revision,
                        str(title or "研究进展")[:160],
                        canonical_json(goal),
                        canonical_json(summary),
                        normalized_outcome,
                        canonical_json(normalized_events),
                        canonical_json(_unique(session_ids, 1000)),
                        canonical_json(_unique(trace_ids, 1000)),
                        canonical_json(artifact_refs[:1000]),
                        canonical_json(tool_calls[:2000]),
                        canonical_json(decisions[:500]),
                        canonical_json(failed_paths[:500]),
                        canonical_json(open_questions[:500]),
                        canonical_json(next_actions[:500]),
                        started_at,
                        ended_at,
                        str(boundary_reason or "unknown")[:80],
                        EPISODE_DERIVATION_VERSION,
                        source_digest,
                        supersedes,
                        now,
                        idempotency_key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_episodes WHERE episode_id = ?",
                    (episode_id,),
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
        return ResearchEpisode.from_row(row), True

    @classmethod
    def mark_unmatched_episodes_stale(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        matched_episode_keys: set[str],
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> int:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT episode_id, episode_key FROM research_episodes "
                "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "AND research_id = ? AND state = 'active'",
                (tenant_id, user_id, workspace_id, research_id),
            ).fetchall()
            stale_ids = [
                str(row["episode_id"])
                for row in rows
                if str(row["episode_key"]) not in matched_episode_keys
            ]
            if stale_ids:
                conn.execute(
                    "UPDATE research_episodes SET state = 'stale' WHERE episode_id IN ("
                    + ", ".join("?" for _ in stale_ids)
                    + ")",
                    stale_ids,
                )
        finally:
            conn.close()
        return len(stale_ids)

    @classmethod
    def get_checkpoint(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        include_inactive: bool = False,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchCheckpoint | None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_checkpoints WHERE research_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                + ("" if include_inactive else "AND state = 'active' ")
                + "ORDER BY revision DESC LIMIT 1",
                (research_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchCheckpoint.from_row(row) if row is not None else None

    @classmethod
    def list_checkpoints(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 100,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchCheckpoint]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_checkpoints WHERE research_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "ORDER BY revision DESC LIMIT ?",
                (
                    research_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    max(1, min(int(limit or 100), 1000)),
                ),
            ).fetchall()
        finally:
            conn.close()
        return [ResearchCheckpoint.from_row(row) for row in rows]

    @classmethod
    def mark_active_checkpoint_stale(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> int:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            cursor = conn.execute(
                "UPDATE research_checkpoints SET state = 'stale' WHERE research_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "AND state = 'active'",
                (research_id, tenant_id, user_id, workspace_id),
            )
        finally:
            conn.close()
        return max(0, int(cursor.rowcount or 0))

    @classmethod
    def append_checkpoint(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        research_goal: str,
        current_state: str,
        confirmed_facts: list[dict[str, Any]],
        working_hypotheses: list[dict[str, Any]],
        recent_progress: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        failed_paths: list[dict[str, Any]],
        known_conflicts: list[dict[str, Any]],
        important_artifacts: list[dict[str, Any]],
        open_questions: list[dict[str, Any]],
        next_actions: list[dict[str, Any]],
        source_episode_ids: list[str],
        source_event_ids: list[str],
        source_digest: str,
        token_estimate: int,
        budget_status: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchCheckpoint, bool]:
        resolved = settings or load_research_settings()
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "research_id": research_id,
                    "source_digest": source_digest,
                    "generator_version": CHECKPOINT_GENERATOR_VERSION,
                }
            )
        )
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id FROM research_projects "
                    "WHERE research_id = ?",
                    (research_id,),
                ).fetchone()
                if owner is None or tuple(owner) != (tenant_id, user_id, workspace_id):
                    raise ValueError("project does not belong to owner")
                current = conn.execute(
                    "SELECT * FROM research_checkpoints WHERE research_id = ? "
                    "AND state = 'active'",
                    (research_id,),
                ).fetchone()
                if current is not None and str(current["source_digest"]) == source_digest:
                    conn.execute("COMMIT")
                    return ResearchCheckpoint.from_row(current), False
                existing = conn.execute(
                    "SELECT * FROM research_checkpoints WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return ResearchCheckpoint.from_row(existing), False
                revision = int(current["revision"] or 0) + 1 if current else 1
                checkpoint_id = f"checkpoint-{uuid.uuid4()}"
                supersedes = str(current["checkpoint_id"]) if current else ""
                if current is not None:
                    conn.execute(
                        "UPDATE research_checkpoints SET state = 'superseded' "
                        "WHERE checkpoint_id = ?",
                        (supersedes,),
                    )
                generated_at = utc_now_iso()
                conn.execute(
                    """
                    INSERT INTO research_checkpoints(
                        checkpoint_id, schema_version, tenant_id, user_id,
                        workspace_id, research_id, revision, research_goal,
                        current_state, confirmed_facts_json,
                        working_hypotheses_json, recent_progress_json,
                        decisions_json, failed_paths_json, known_conflicts_json,
                        important_artifacts_json, open_questions_json,
                        next_actions_json, source_episode_ids_json,
                        source_event_ids_json, generated_at, generator_version,
                        source_digest, state, supersedes_checkpoint_id,
                        token_estimate, budget_status, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        DERIVATION_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        revision,
                        str(research_goal or "")[:2000],
                        str(current_state or "")[:4000],
                        canonical_json(confirmed_facts[:500]),
                        canonical_json(working_hypotheses[:500]),
                        canonical_json(recent_progress[:100]),
                        canonical_json(decisions[:500]),
                        canonical_json(failed_paths[:500]),
                        canonical_json(known_conflicts[:500]),
                        canonical_json(important_artifacts[:1000]),
                        canonical_json(open_questions[:500]),
                        canonical_json(next_actions[:500]),
                        canonical_json(_unique(source_episode_ids, 1000)),
                        canonical_json(_unique(source_event_ids)),
                        generated_at,
                        CHECKPOINT_GENERATOR_VERSION,
                        source_digest,
                        supersedes,
                        max(0, int(token_estimate or 0)),
                        str(budget_status or "within_budget")[:40],
                        idempotency_key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
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
        return ResearchCheckpoint.from_row(row), True

    @classmethod
    def create_finding(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        statement: str,
        provenance: str,
        source_event_ids: list[str],
        actor_id: str,
        episode_id: str = "",
        scope: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchFinding, bool]:
        resolved = settings or load_research_settings()
        normalized_statement = " ".join(str(statement or "").split())[:4000]
        if not normalized_statement:
            raise ValueError("finding statement is required")
        normalized_provenance = str(provenance or "user_stated").strip().lower()
        if normalized_provenance not in PROVENANCE_TYPES:
            raise ValueError("invalid finding provenance")
        source_ids = _unique(source_event_ids)
        if not source_ids:
            raise ValueError("finding requires source events")
        normalized_scope = dict(scope or {})
        normalized_evidence = _unique(evidence_ids, 500)
        key = sha256_text(
            canonical_json(
                {
                    "owner": [tenant_id, user_id, workspace_id],
                    "research_id": research_id,
                    "statement": normalized_statement,
                    "scope": normalized_scope,
                    "source_event_ids": source_ids,
                }
            )
        )
        now = utc_now_iso()
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id FROM research_projects "
                    "WHERE research_id = ?",
                    (research_id,),
                ).fetchone()
                if owner is None or tuple(owner) != (tenant_id, user_id, workspace_id):
                    raise ValueError("project does not belong to owner")
                if episode_id:
                    episode = conn.execute(
                        "SELECT research_id, tenant_id, user_id, workspace_id "
                        "FROM research_episodes WHERE episode_id = ?",
                        (episode_id,),
                    ).fetchone()
                    if episode is None or tuple(episode) != (
                        research_id,
                        tenant_id,
                        user_id,
                        workspace_id,
                    ):
                        raise ValueError("episode does not belong to project")
                placeholders = ", ".join("?" for _ in source_ids)
                valid_count = conn.execute(
                    "SELECT COUNT(DISTINCT event_id) AS count FROM research_event_links "
                    "WHERE tenant_id = ? AND user_id = ? AND workspace_id = ? "
                    "AND research_id = ? AND state = 'included' AND relevance = 'research' "
                    "AND superseded_by = '' AND event_id IN (" + placeholders + ")",
                    [tenant_id, user_id, workspace_id, research_id, *source_ids],
                ).fetchone()
                if int(valid_count["count"] or 0) != len(source_ids):
                    raise ValueError("finding source events do not belong to project")
                existing = conn.execute(
                    "SELECT * FROM research_findings WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return ResearchFinding.from_row(existing), False
                finding_id = f"finding-{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO research_findings(
                        finding_id, schema_version, tenant_id, user_id, workspace_id,
                        research_id, episode_id, statement, scope_json, status,
                        provenance, evidence_ids_json, source_event_ids_json,
                        current_revision, created_by, created_at, updated_at,
                        idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        DERIVATION_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        str(episode_id or "")[:160],
                        normalized_statement,
                        canonical_json(normalized_scope),
                        normalized_provenance,
                        canonical_json(normalized_evidence),
                        canonical_json(source_ids),
                        str(actor_id or user_id)[:160],
                        now,
                        now,
                        key,
                    ),
                )
                version_key = sha256_text(f"{finding_id}:1:active")
                conn.execute(
                    """
                    INSERT INTO research_finding_versions(
                        version_id, finding_id, tenant_id, user_id, workspace_id,
                        research_id, revision, statement, scope_json, status,
                        provenance, evidence_ids_json, source_event_ids_json,
                        reason, changed_by, occurred_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"finding-version-{uuid.uuid4()}",
                        finding_id,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        normalized_statement,
                        canonical_json(normalized_scope),
                        normalized_provenance,
                        canonical_json(normalized_evidence),
                        canonical_json(source_ids),
                        "finding created",
                        str(actor_id or user_id)[:160],
                        now,
                        version_key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_findings WHERE finding_id = ?",
                    (finding_id,),
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
        return ResearchFinding.from_row(row), True

    @classmethod
    def get_finding(
        cls,
        finding_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchFinding | None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_findings WHERE finding_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                (finding_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchFinding.from_row(row) if row is not None else None

    @classmethod
    def list_findings(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        statuses: list[str] | None = None,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchFinding]:
        resolved = settings or load_research_settings()
        clauses = [
            "tenant_id = ?",
            "user_id = ?",
            "workspace_id = ?",
            "research_id = ?",
        ]
        params: list[Any] = [tenant_id, user_id, workspace_id, research_id]
        valid_statuses = [item for item in _unique(statuses, 20) if item in FINDING_STATUSES]
        if valid_statuses:
            clauses.append("status IN (" + ", ".join("?" for _ in valid_statuses) + ")")
            params.extend(valid_statuses)
        params.append(max(1, min(int(limit or 500), 1000)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_findings WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchFinding.from_row(row) for row in rows]

    @classmethod
    def list_finding_versions(
        cls,
        finding_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[dict[str, Any]]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_finding_versions WHERE finding_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "ORDER BY revision ASC",
                (finding_id, tenant_id, user_id, workspace_id),
            ).fetchall()
        finally:
            conn.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, default in (
                ("scope_json", {}),
                ("evidence_ids_json", []),
                ("source_event_ids_json", []),
            ):
                try:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key) or "")
                except Exception:
                    item[key.removesuffix("_json")] = default
            result.append(item)
        return result

    @classmethod
    def set_finding_status(
        cls,
        finding_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        status: str,
        actor_id: str,
        reason: str,
        evidence_ids: list[str] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchFinding:
        resolved = settings or load_research_settings()
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in FINDING_STATUSES:
            raise ValueError("invalid finding status")
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT * FROM research_findings WHERE finding_id = ? "
                    "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                    (finding_id, tenant_id, user_id, workspace_id),
                ).fetchone()
                if current is None:
                    raise ValueError("finding does not belong to owner")
                if str(current["status"]) == normalized_status:
                    conn.execute("COMMIT")
                    return ResearchFinding.from_row(current)
                revision = int(current["current_revision"] or 0) + 1
                evidence = _unique(evidence_ids, 500) or json.loads(
                    str(current["evidence_ids_json"] or "[]")
                )
                version_key = sha256_text(
                    canonical_json(
                        {
                            "finding_id": finding_id,
                            "revision": revision,
                            "status": normalized_status,
                        }
                    )
                )
                now = utc_now_iso()
                conn.execute(
                    "UPDATE research_findings SET status = ?, current_revision = ?, "
                    "evidence_ids_json = ?, updated_at = ? WHERE finding_id = ?",
                    (
                        normalized_status,
                        revision,
                        canonical_json(evidence),
                        now,
                        finding_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO research_finding_versions(
                        version_id, finding_id, tenant_id, user_id, workspace_id,
                        research_id, revision, statement, scope_json, status,
                        provenance, evidence_ids_json, source_event_ids_json,
                        reason, changed_by, occurred_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"finding-version-{uuid.uuid4()}",
                        finding_id,
                        tenant_id,
                        user_id,
                        workspace_id,
                        str(current["research_id"]),
                        revision,
                        str(current["statement"]),
                        str(current["scope_json"]),
                        normalized_status,
                        str(current["provenance"]),
                        canonical_json(evidence),
                        str(current["source_event_ids_json"]),
                        str(reason or "")[:2000],
                        str(actor_id or user_id)[:160],
                        now,
                        version_key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_findings WHERE finding_id = ?",
                    (finding_id,),
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
        return ResearchFinding.from_row(row)

    @classmethod
    def append_change(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        change_type: str,
        subject_type: str,
        subject_id: str,
        actor_id: str,
        reason: str = "",
        created_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        actor_type: str = "user",
        idempotency_key: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        resolved = settings or load_research_settings()
        now = utc_now_iso()
        normalized_key = str(idempotency_key or "").strip()[:180] or sha256_text(
            canonical_json(
                {
                    "research_id": research_id,
                    "change_type": change_type,
                    "subject_id": subject_id,
                    "created_ids": created_ids or [],
                    "now": now,
                }
            )
        )
        conn = cls._connect(workdir, resolved)
        try:
            owner = conn.execute(
                "SELECT tenant_id, user_id, workspace_id FROM research_projects "
                "WHERE research_id = ?",
                (research_id,),
            ).fetchone()
            if owner is None or tuple(owner) != (tenant_id, user_id, workspace_id):
                raise ValueError("project does not belong to owner")
            conn.execute(
                """
                INSERT OR IGNORE INTO research_change_events(
                    change_id, schema_version, tenant_id, user_id, workspace_id,
                    research_id, change_type, subject_type, subject_id,
                    created_ids_json, actor_type, actor_id, reason,
                    evidence_ids_json, occurred_at, idempotency_key
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"change-{uuid.uuid4()}",
                    DERIVATION_SCHEMA_VERSION,
                    tenant_id,
                    user_id,
                    workspace_id,
                    research_id,
                    str(change_type or "unknown")[:80],
                    str(subject_type or "unknown")[:80],
                    str(subject_id or "")[:160],
                    canonical_json(_unique(created_ids, 1000)),
                    str(actor_type or "user")[:40],
                    str(actor_id or user_id)[:160],
                    str(reason or "")[:2000],
                    canonical_json(_unique(evidence_ids, 1000)),
                    now,
                    normalized_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_change_events WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
        finally:
            conn.close()
        return cls._change_row(row)

    @staticmethod
    def _change_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        for key in ("created_ids_json", "evidence_ids_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(item.pop(key) or "[]")
            except Exception:
                item[key.removesuffix("_json")] = []
        return item

    @classmethod
    def list_changes(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[dict[str, Any]]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_change_events WHERE research_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "ORDER BY occurred_at DESC LIMIT ?",
                (
                    research_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    max(1, min(int(limit or 500), 1000)),
                ),
            ).fetchall()
        finally:
            conn.close()
        return [cls._change_row(row) for row in rows]

    @classmethod
    def create_conflict(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        conflict_type: str,
        original_refs: list[str],
        challenging_refs: list[str],
        summary: str,
        actor_id: str,
        original_evidence_ids: list[str] | None = None,
        challenging_evidence_ids: list[str] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchConflict, bool]:
        resolved = settings or load_research_settings()
        normalized_type = str(conflict_type or "interpretation_dispute").strip().lower()
        if normalized_type not in CONFLICT_TYPES:
            raise ValueError("invalid conflict type")
        originals = _unique(original_refs, 100)
        challengers = _unique(challenging_refs, 100)
        if not originals or not challengers or set(originals) & set(challengers):
            raise ValueError("conflict requires distinct original and challenging findings")
        normalized_summary = " ".join(str(summary or "").split())[:4000]
        if not normalized_summary:
            raise ValueError("conflict summary is required")
        key = sha256_text(
            canonical_json(
                {
                    "research_id": research_id,
                    "type": normalized_type,
                    "originals": sorted(originals),
                    "challengers": sorted(challengers),
                }
            )
        )
        now = utc_now_iso()
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                refs = originals + challengers
                rows = conn.execute(
                    "SELECT finding_id FROM research_findings WHERE tenant_id = ? "
                    "AND user_id = ? AND workspace_id = ? AND research_id = ? "
                    "AND finding_id IN (" + ", ".join("?" for _ in refs) + ")",
                    [tenant_id, user_id, workspace_id, research_id, *refs],
                ).fetchall()
                if {str(row["finding_id"]) for row in rows} != set(refs):
                    raise ValueError("conflict findings do not belong to project")
                existing = conn.execute(
                    "SELECT * FROM research_conflicts WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return ResearchConflict.from_row(existing), False
                conflict_id = f"conflict-{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO research_conflicts(
                        conflict_id, schema_version, tenant_id, user_id, workspace_id,
                        research_id, conflict_type, status, original_refs_json,
                        challenging_refs_json, original_evidence_ids_json,
                        challenging_evidence_ids_json, summary, detected_by,
                        detected_at, updated_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conflict_id,
                        DERIVATION_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        normalized_type,
                        canonical_json(originals),
                        canonical_json(challengers),
                        canonical_json(_unique(original_evidence_ids, 500)),
                        canonical_json(_unique(challenging_evidence_ids, 500)),
                        normalized_summary,
                        str(actor_id or user_id)[:160],
                        now,
                        now,
                        key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_conflicts WHERE conflict_id = ?",
                    (conflict_id,),
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
        return ResearchConflict.from_row(row), True

    @classmethod
    def get_conflict(
        cls,
        conflict_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchConflict | None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_conflicts WHERE conflict_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                (conflict_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchConflict.from_row(row) if row is not None else None

    @classmethod
    def list_conflicts(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        statuses: list[str] | None = None,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchConflict]:
        resolved = settings or load_research_settings()
        clauses = [
            "tenant_id = ?",
            "user_id = ?",
            "workspace_id = ?",
            "research_id = ?",
        ]
        params: list[Any] = [tenant_id, user_id, workspace_id, research_id]
        valid_statuses = [
            item for item in _unique(statuses, 20) if item in CONFLICT_STATUSES
        ]
        if valid_statuses:
            clauses.append("status IN (" + ", ".join("?" for _ in valid_statuses) + ")")
            params.extend(valid_statuses)
        params.append(max(1, min(int(limit or 500), 1000)))
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_conflicts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchConflict.from_row(row) for row in rows]

    @classmethod
    def update_conflict(
        cls,
        conflict_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        status: str,
        current_resolution_id: str | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchConflict:
        resolved = settings or load_research_settings()
        normalized = str(status or "").strip().lower()
        if normalized not in CONFLICT_STATUSES:
            raise ValueError("invalid conflict status")
        changes = ["status = ?", "updated_at = ?"]
        params: list[Any] = [normalized, utc_now_iso()]
        if current_resolution_id is not None:
            changes.append("current_resolution_id = ?")
            params.append(str(current_resolution_id or "")[:160])
        params.extend([conflict_id, tenant_id, user_id, workspace_id])
        conn = cls._connect(workdir, resolved)
        try:
            cursor = conn.execute(
                "UPDATE research_conflicts SET "
                + ", ".join(changes)
                + " WHERE conflict_id = ? AND tenant_id = ? AND user_id = ? "
                "AND workspace_id = ?",
                params,
            )
            if cursor.rowcount == 0:
                raise ValueError("conflict does not belong to owner")
            row = conn.execute(
                "SELECT * FROM research_conflicts WHERE conflict_id = ?",
                (conflict_id,),
            ).fetchone()
        finally:
            conn.close()
        return ResearchConflict.from_row(row)

    @classmethod
    def append_resolution(
        cls,
        conflict_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        result: str,
        actor_id: str,
        reason: str,
        evidence_ids: list[str],
        scope: dict[str, Any] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchResolution, bool]:
        resolved = settings or load_research_settings()
        normalized_result = str(result or "").strip().lower()
        if normalized_result not in RESOLUTION_RESULTS:
            raise ValueError("invalid resolution result")
        normalized_reason = " ".join(str(reason or "").split())[:4000]
        if not normalized_reason:
            raise ValueError("resolution reason is required")
        evidence = _unique(evidence_ids, 1000)
        if not evidence:
            raise ValueError("resolution requires evidence references")
        with _DERIVATION_LOCK:
            conn = cls._connect(workdir, resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conflict = conn.execute(
                    "SELECT * FROM research_conflicts WHERE conflict_id = ? "
                    "AND tenant_id = ? AND user_id = ? AND workspace_id = ?",
                    (conflict_id, tenant_id, user_id, workspace_id),
                ).fetchone()
                if conflict is None:
                    raise ValueError("conflict does not belong to owner")
                revision_row = conn.execute(
                    "SELECT MAX(revision) AS revision FROM research_resolutions "
                    "WHERE conflict_id = ?",
                    (conflict_id,),
                ).fetchone()
                revision = int(revision_row["revision"] or 0) + 1
                key = sha256_text(
                    canonical_json(
                        {
                            "conflict_id": conflict_id,
                            "revision": revision,
                            "result": normalized_result,
                            "reason": normalized_reason,
                            "evidence": evidence,
                        }
                    )
                )
                existing = conn.execute(
                    "SELECT * FROM research_resolutions WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return ResearchResolution.from_row(existing), False
                resolution_id = f"resolution-{uuid.uuid4()}"
                now = utc_now_iso()
                conn.execute(
                    """
                    INSERT INTO research_resolutions(
                        resolution_id, schema_version, tenant_id, user_id,
                        workspace_id, conflict_id, research_id, revision, result,
                        actor_id, reason, evidence_ids_json, scope_json,
                        resolved_at, idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolution_id,
                        DERIVATION_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        conflict_id,
                        str(conflict["research_id"]),
                        revision,
                        normalized_result,
                        str(actor_id or user_id)[:160],
                        normalized_reason,
                        canonical_json(evidence),
                        canonical_json(dict(scope or {})),
                        now,
                        key,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_resolutions WHERE resolution_id = ?",
                    (resolution_id,),
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
        return ResearchResolution.from_row(row), True

    @classmethod
    def list_resolutions(
        cls,
        conflict_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchResolution]:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_resolutions WHERE conflict_id = ? "
                "AND tenant_id = ? AND user_id = ? AND workspace_id = ? "
                "ORDER BY revision ASC",
                (conflict_id, tenant_id, user_id, workspace_id),
            ).fetchall()
        finally:
            conn.close()
        return [ResearchResolution.from_row(row) for row in rows]

    @classmethod
    def start_run(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str,
        trigger_kind: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> str:
        resolved = settings or load_research_settings()
        run_id = f"derivation-run-{uuid.uuid4()}"
        conn = cls._connect(workdir, resolved)
        try:
            conn.execute(
                """
                INSERT INTO research_derivation_runs(
                    run_id, tenant_id, user_id, workspace_id, research_id,
                    trigger_kind, derivation_version, status, started_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    str(research_id or "")[:160],
                    str(trigger_kind or "manual")[:80],
                    EPISODE_DERIVATION_VERSION,
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
                UPDATE research_derivation_runs SET status = ?, completed_at = ?,
                    project_count = ?, event_count = ?, episode_created_count = ?,
                    episode_stale_count = ?, checkpoint_created_count = ?,
                    errors_json = ? WHERE run_id = ?
                """,
                (
                    str(report.get("status") or "completed")[:40],
                    utc_now_iso(),
                    max(0, int(report.get("project_count") or 0)),
                    max(0, int(report.get("event_count") or 0)),
                    max(0, int(report.get("episode_created_count") or 0)),
                    max(0, int(report.get("episode_stale_count") or 0)),
                    max(0, int(report.get("checkpoint_created_count") or 0)),
                    canonical_json(list(report.get("errors") or [])[:100]),
                    run_id,
                ),
            )
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
            episode_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM research_episodes WHERE "
                "tenant_id = ? AND user_id = ? AND workspace_id = ? GROUP BY state",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            checkpoint_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM research_checkpoints WHERE "
                "tenant_id = ? AND user_id = ? AND workspace_id = ? GROUP BY state",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            finding_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM research_findings WHERE "
                "tenant_id = ? AND user_id = ? AND workspace_id = ? GROUP BY status",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            conflict_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM research_conflicts WHERE "
                "tenant_id = ? AND user_id = ? AND workspace_id = ? GROUP BY status",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            last_run = conn.execute(
                "SELECT status, started_at, completed_at, errors_json FROM "
                "research_derivation_runs WHERE tenant_id = ? AND user_id = ? "
                "AND workspace_id = ? ORDER BY started_at DESC LIMIT 1",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return {
            "episode_states": {
                str(row["state"]): int(row["count"] or 0) for row in episode_rows
            },
            "checkpoint_states": {
                str(row["state"]): int(row["count"] or 0) for row in checkpoint_rows
            },
            "finding_states": {
                str(row["status"]): int(row["count"] or 0) for row in finding_rows
            },
            "conflict_states": {
                str(row["status"]): int(row["count"] or 0) for row in conflict_rows
            },
            "last_run": dict(last_run) if last_run is not None else None,
        }
