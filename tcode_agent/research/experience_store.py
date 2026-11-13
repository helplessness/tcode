"""Owner-scoped persistence for revisioned research experience items."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .experience_models import (
    RESEARCH_EXPERIENCE_KINDS,
    RESEARCH_EXPERIENCE_PROVENANCE,
    RESEARCH_EXPERIENCE_ROUTING_STATES,
    RESEARCH_EXPERIENCE_SCHEMA_VERSION,
    RESEARCH_EXPERIENCE_STATES,
    RESEARCH_EXPERIENCE_SUMMARY_STATES,
    ResearchExperienceItem,
)
from .models import canonical_json, sha256_text, utc_now_iso
from .routing_models import RESEARCH_DURABILITIES
from .store import ResearchEventStore


def _owner(tenant_id: str, user_id: str, workspace_id: str) -> tuple[str, str, str]:
    value = tuple(str(item or "").strip() for item in (tenant_id, user_id, workspace_id))
    if any(not item for item in value):
        raise ValueError("tenant_id, user_id and workspace_id are required")
    return value  # type: ignore[return-value]


def _strings(values: list[str] | None, *, limit: int = 5000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        )
    )[: max(0, limit)]


def _artifacts(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (values or []) if isinstance(item, dict)][:1000]


class ResearchExperienceStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def append(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        kind: str,
        title: str,
        summary: str,
        occurred_at: str,
        experience_key: str = "",
        summary_state: str = "complete",
        durability: str = "durable",
        outcome_status: str = "unknown",
        routing_state: str = "unassigned",
        source_research_ids: list[str] | None = None,
        source_activity_ids: list[str] | None = None,
        source_episode_ids: list[str] | None = None,
        source_event_ids: list[str] | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
        evidence_ids: list[str] | None = None,
        origin_pack_ids: list[str] | None = None,
        provenance: str = "observed",
        state: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
        expected_revision: int | None = None,
    ) -> tuple[ResearchExperienceItem, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_kind = str(kind or "").strip().lower()
        normalized_summary_state = str(summary_state or "").strip().lower()
        normalized_durability = str(durability or "").strip().lower()
        normalized_routing = str(routing_state or "").strip().lower()
        normalized_provenance = str(provenance or "").strip().lower()
        normalized_state = str(state or "").strip().lower()
        if normalized_kind not in RESEARCH_EXPERIENCE_KINDS:
            raise ValueError("unsupported research experience kind")
        if normalized_summary_state not in RESEARCH_EXPERIENCE_SUMMARY_STATES:
            raise ValueError("unsupported research experience summary state")
        if normalized_durability not in RESEARCH_DURABILITIES:
            raise ValueError("unsupported research experience durability")
        if normalized_routing not in RESEARCH_EXPERIENCE_ROUTING_STATES:
            raise ValueError("unsupported research experience routing state")
        if normalized_provenance not in RESEARCH_EXPERIENCE_PROVENANCE:
            raise ValueError("unsupported research experience provenance")
        if normalized_state not in RESEARCH_EXPERIENCE_STATES:
            raise ValueError("unsupported research experience state")
        normalized_title = str(title or "").strip()[:500]
        normalized_summary = str(summary or "").strip()[:12000]
        normalized_time = str(occurred_at or "").strip()[:48]
        if not normalized_title or not normalized_time:
            raise ValueError("research experience title and occurred_at are required")
        researches = _strings(source_research_ids, limit=100)
        activities = _strings(source_activity_ids)
        episodes = _strings(source_episode_ids)
        events = _strings(source_event_ids)
        artifacts = _artifacts(artifact_refs)
        evidence = _strings(evidence_ids)
        origin_packs = _strings(origin_pack_ids, limit=100)
        normalized_key = str(experience_key or "").strip()[:240]
        if not normalized_key:
            normalized_key = sha256_text(
                canonical_json(
                    {
                        "kind": normalized_kind,
                        "activities": activities,
                        "episodes": episodes,
                        "events": events,
                        "occurred_at": normalized_time,
                        "title": normalized_title,
                    }
                )
            )
        semantic = {
            "experience_key": normalized_key,
            "kind": normalized_kind,
            "title": normalized_title,
            "summary": normalized_summary,
            "summary_state": normalized_summary_state,
            "durability": normalized_durability,
            "outcome_status": str(outcome_status or "unknown").strip().lower()[:40],
            "routing_state": normalized_routing,
            "source_research_ids": researches,
            "source_activity_ids": activities,
            "source_episode_ids": episodes,
            "source_event_ids": events,
            "artifact_refs": artifacts,
            "evidence_ids": evidence,
            "origin_pack_ids": origin_packs,
            "provenance": normalized_provenance,
            "occurred_at": normalized_time,
            "state": normalized_state,
        }
        source_digest = sha256_text(canonical_json(semantic))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM research_experience_items WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND experience_key=? AND superseded_by='' LIMIT 1",
                (*owner, normalized_key),
            ).fetchone()
            if current is not None and str(current["source_digest"] or "") == source_digest:
                connection.execute("COMMIT")
                return ResearchExperienceItem.from_row(current), False
            if expected_revision is not None and expected_revision != (int(current["revision"]) if current else 0):
                raise ValueError("research_decision_changed_reload_required")
            revision = int(current["revision"] or 0) + 1 if current is not None else 1
            experience_id = f"experience-{uuid.uuid4()}"
            now = utc_now_iso()
            if current is not None:
                connection.execute(
                    "UPDATE research_experience_items SET state='superseded',superseded_by=? "
                    "WHERE experience_id=?",
                    (experience_id, str(current["experience_id"])),
                )
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "key": normalized_key, "revision": revision, "digest": source_digest})
            )
            connection.execute(
                "INSERT INTO research_experience_items(experience_id,experience_key,schema_version,tenant_id,"
                "user_id,workspace_id,kind,title,summary,summary_state,durability,outcome_status,routing_state,"
                "source_research_ids_json,source_activity_ids_json,source_episode_ids_json,source_event_ids_json,"
                "artifact_refs_json,evidence_ids_json,origin_pack_ids_json,provenance,occurred_at,revision,state,"
                "source_digest,supersedes_experience_id,superseded_by,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experience_id,
                    normalized_key,
                    RESEARCH_EXPERIENCE_SCHEMA_VERSION,
                    *owner,
                    normalized_kind,
                    normalized_title,
                    normalized_summary,
                    normalized_summary_state,
                    normalized_durability,
                    semantic["outcome_status"],
                    normalized_routing,
                    canonical_json(researches),
                    canonical_json(activities),
                    canonical_json(episodes),
                    canonical_json(events),
                    canonical_json(artifacts),
                    canonical_json(evidence),
                    canonical_json(origin_packs),
                    normalized_provenance,
                    normalized_time,
                    revision,
                    normalized_state,
                    source_digest,
                    str(current["experience_id"] or "") if current is not None else "",
                    "",
                    now,
                    idempotency_key,
                ),
            )
            relation = "shared" if len(researches) > 1 else "primary"
            for research_id in researches:
                scope_link_id = f"scope-link-{uuid.uuid4()}"
                connection.execute(
                    "INSERT INTO research_object_scopes(scope_link_id,schema_version,tenant_id,user_id,workspace_id,"
                    "object_type,object_id,research_id,relation,confidence,source_route_id,state,created_at,"
                    "superseded_by,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope_link_id,
                        RESEARCH_EXPERIENCE_SCHEMA_VERSION,
                        *owner,
                        "experience",
                        experience_id,
                        research_id,
                        relation,
                        1.0,
                        "",
                        "active",
                        now,
                        "",
                        sha256_text(canonical_json({"owner": owner, "scope_link_id": scope_link_id})),
                    ),
                )
            row = connection.execute(
                "SELECT * FROM research_experience_items WHERE experience_id=?",
                (experience_id,),
            ).fetchone()
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("research experience item was not created")
        return ResearchExperienceItem.from_row(row), True

    @classmethod
    def get(
        cls,
        experience_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchExperienceItem | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_experience_items WHERE experience_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=?",
                (str(experience_id or "").strip(), *owner),
            ).fetchone()
        finally:
            connection.close()
        return ResearchExperienceItem.from_row(row) if row is not None else None

    @classmethod
    def get_active_by_key(
        cls,
        experience_key: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchExperienceItem | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_experience_items WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND experience_key=? AND superseded_by='' LIMIT 1",
                (*owner, str(experience_key or "").strip()),
            ).fetchone()
        finally:
            connection.close()
        return ResearchExperienceItem.from_row(row) if row is not None else None

    @classmethod
    def list_active_for_episode_key(
        cls,
        episode_key: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchExperienceItem]:
        """Return current Episode-derived items for one logical Episode only."""

        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT DISTINCT x.* FROM research_experience_items x "
                "WHERE x.tenant_id=? AND x.user_id=? AND x.workspace_id=? "
                "AND x.experience_key LIKE 'episode:%' "
                "AND x.superseded_by='' AND x.state='active' AND EXISTS ("
                "SELECT 1 FROM json_each(x.source_episode_ids_json) ref "
                "JOIN research_episodes ep ON ep.episode_id=ref.value "
                "WHERE ep.tenant_id=? AND ep.user_id=? AND ep.workspace_id=? "
                "AND ep.episode_key=?)",
                (*owner, *owner, str(episode_key or "").strip()),
            ).fetchall()
        finally:
            connection.close()
        return [ResearchExperienceItem.from_row(row) for row in rows]

    @classmethod
    def list_items(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        research_id: str = "",
        unassigned_only: bool = False,
        durability: str = "",
        started_at: str = "",
        ended_at: str = "",
        include_history: bool = False,
        experience_key: str = "",
        before_revision: int = 0,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchExperienceItem]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["e.tenant_id=?", "e.user_id=?", "e.workspace_id=?"]
        params: list[Any] = [*owner]
        joins = ""
        if experience_key:
            clauses.append("e.experience_key=?")
            params.append(experience_key)
        if before_revision:
            clauses.append("e.revision<?")
            params.append(before_revision)
        normalized_research = str(research_id or "").strip()
        if normalized_research:
            joins = (
                " JOIN research_object_scopes s ON s.object_type='experience' "
                "AND s.object_id=e.experience_id AND s.superseded_by='' AND s.state='active'"
            )
            clauses.append("s.research_id=?")
            params.append(normalized_research)
        elif unassigned_only:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM research_object_scopes s WHERE s.object_type='experience' "
                "AND s.object_id=e.experience_id AND s.superseded_by='' AND s.state='active')"
            )
        if durability:
            normalized_durability = str(durability or "").strip().lower()
            if normalized_durability not in RESEARCH_DURABILITIES:
                raise ValueError("unsupported research experience durability")
            clauses.append("e.durability=?")
            params.append(normalized_durability)
        if started_at:
            clauses.append("e.occurred_at>=?")
            params.append(str(started_at))
        if ended_at:
            clauses.append("e.occurred_at<?")
            params.append(str(ended_at))
        if not include_history:
            clauses.append("e.superseded_by=''")
            clauses.append("e.state='active'")
        params.append(max(1, min(int(limit or 500), 5000)))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT DISTINCT e.* FROM research_experience_items e"
                + joins
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY e.occurred_at DESC,e.revision DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchExperienceItem.from_row(row) for row in rows]

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
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT durability,routing_state,COUNT(*) AS count "
                "FROM research_experience_items WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND superseded_by='' AND state='active' "
                "GROUP BY durability,routing_state",
                owner,
            ).fetchall()
        finally:
            connection.close()
        by_durability: dict[str, int] = {}
        by_routing_state: dict[str, int] = {}
        for row in rows:
            count = int(row["count"] or 0)
            durability = str(row["durability"] or "unknown")
            routing_state = str(row["routing_state"] or "unknown")
            by_durability[durability] = by_durability.get(durability, 0) + count
            by_routing_state[routing_state] = by_routing_state.get(routing_state, 0) + count
        return {
            "count": sum(by_durability.values()),
            "by_durability": by_durability,
            "by_routing_state": by_routing_state,
        }


__all__ = ["ResearchExperienceStore"]
