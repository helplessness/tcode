"""Append-only persistence for Stage 5 research knowledge links and lint runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .knowledge_models import (
    KNOWLEDGE_OBJECT_TYPES,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_SCHEMA_VERSION,
    KNOWLEDGE_SIDES,
    KNOWLEDGE_STATES,
    KNOWLEDGE_SUBJECT_TYPES,
    ResearchKnowledgeLink,
    ResearchKnowledgeLintReport,
)
from .models import canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore


_KNOWLEDGE_LOCK = threading.RLock()


def _clean(value: Any, *, maximum: int = 500) -> str:
    return str(value or "").strip()[:maximum]


class ResearchKnowledgeStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def _project_exists(
        cls,
        connection: sqlite3.Connection,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> bool:
        return connection.execute(
            "SELECT 1 FROM research_projects WHERE research_id=? AND tenant_id=? "
            "AND user_id=? AND workspace_id=?",
            (research_id, tenant_id, user_id, workspace_id),
        ).fetchone() is not None

    @classmethod
    def upsert_link(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        relation: str,
        side: str = "neutral",
        state: str = "active",
        source_revision: int = 0,
        source_hash: str = "",
        metadata: dict[str, Any] | None = None,
        discovered_by: str = "system",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchKnowledgeLink, bool]:
        resolved = settings or load_research_settings()
        subject_type = _clean(subject_type, maximum=40)
        subject_id = _clean(subject_id, maximum=240)
        object_type = _clean(object_type, maximum=40)
        object_id = _clean(object_id, maximum=500)
        relation = _clean(relation, maximum=40)
        side = _clean(side, maximum=40) or "neutral"
        state = _clean(state, maximum=40) or "active"
        discovered_by = _clean(discovered_by, maximum=40) or "system"
        if subject_type not in KNOWLEDGE_SUBJECT_TYPES:
            raise ValueError(f"unsupported knowledge subject_type: {subject_type}")
        if object_type not in KNOWLEDGE_OBJECT_TYPES:
            raise ValueError(f"unsupported knowledge object_type: {object_type}")
        if relation not in KNOWLEDGE_RELATIONS:
            raise ValueError(f"unsupported knowledge relation: {relation}")
        if side not in KNOWLEDGE_SIDES:
            raise ValueError(f"unsupported knowledge side: {side}")
        if state not in KNOWLEDGE_STATES:
            raise ValueError(f"unsupported knowledge state: {state}")
        if not subject_id or not object_id:
            raise ValueError("knowledge subject_id and object_id are required")
        normalized_metadata = dict(metadata or {})
        link_key = sha256_text(
            canonical_json(
                {
                    "owner": [tenant_id, user_id, workspace_id, research_id],
                    "subject": [subject_type, subject_id],
                    "object": [object_type, object_id],
                    "side": side,
                }
            )
        )
        semantic = {
            "relation": relation,
            "state": state,
            "source_revision": max(0, int(source_revision or 0)),
            "source_hash": _clean(source_hash, maximum=128),
            "metadata": normalized_metadata,
            "discovered_by": discovered_by,
        }
        semantic_digest = sha256_text(canonical_json(semantic))
        now = utc_now_iso()
        with _KNOWLEDGE_LOCK:
            connection = cls._connect(workdir, resolved)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not cls._project_exists(
                    connection,
                    research_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                ):
                    raise ValueError("research project not found for owner")
                current_row = connection.execute(
                    "SELECT * FROM research_knowledge_links "
                    "WHERE link_key=? AND superseded_by=''",
                    (link_key,),
                ).fetchone()
                current = (
                    ResearchKnowledgeLink.from_row(current_row)
                    if current_row is not None
                    else None
                )
                if current is not None and current.discovered_by == "user" and discovered_by != "user":
                    connection.execute("COMMIT")
                    return current, False
                if current is not None:
                    current_semantic = {
                        "relation": current.relation,
                        "state": current.state,
                        "source_revision": current.source_revision,
                        "source_hash": current.source_hash,
                        "metadata": current.metadata,
                        "discovered_by": current.discovered_by,
                    }
                    if sha256_text(canonical_json(current_semantic)) == semantic_digest:
                        connection.execute("COMMIT")
                        return current, False
                revision = (current.revision + 1) if current is not None else 1
                link_id = f"knowledge-{uuid.uuid4().hex}"
                idempotency_key = f"knowledge:{link_key}:{revision}:{semantic_digest}"
                if current is not None:
                    connection.execute(
                        "UPDATE research_knowledge_links SET superseded_by=? WHERE link_id=?",
                        (link_id, current.link_id),
                    )
                connection.execute(
                    "INSERT INTO research_knowledge_links("
                    "link_id,link_key,schema_version,tenant_id,user_id,workspace_id,research_id,"
                    "subject_type,subject_id,object_type,object_id,relation,side,state,"
                    "source_revision,source_hash,metadata_json,discovered_by,revision,"
                    "created_at,updated_at,supersedes_link_id,superseded_by,idempotency_key"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        link_id,
                        link_key,
                        KNOWLEDGE_SCHEMA_VERSION,
                        tenant_id,
                        user_id,
                        workspace_id,
                        research_id,
                        subject_type,
                        subject_id,
                        object_type,
                        object_id,
                        relation,
                        side,
                        state,
                        semantic["source_revision"],
                        semantic["source_hash"],
                        canonical_json(normalized_metadata),
                        discovered_by,
                        revision,
                        current.created_at if current is not None else now,
                        now,
                        current.link_id if current is not None else "",
                        "",
                        idempotency_key,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM research_knowledge_links WHERE link_id=?",
                    (link_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                connection.close()
        if row is None:
            raise RuntimeError("knowledge link did not persist")
        return ResearchKnowledgeLink.from_row(row), True

    @classmethod
    def list_links(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        include_history: bool = False,
        limit: int = 5000,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchKnowledgeLink]:
        resolved = settings or load_research_settings()
        clauses = [
            "research_id=?",
            "tenant_id=?",
            "user_id=?",
            "workspace_id=?",
        ]
        values: list[Any] = [research_id, tenant_id, user_id, workspace_id]
        if not include_history:
            clauses.append("superseded_by=''")
        if object_type:
            clauses.append("object_type=?")
            values.append(object_type)
        values.append(max(1, min(int(limit or 5000), 10000)))
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_knowledge_links WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, link_id DESC LIMIT ?",
                values,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchKnowledgeLink.from_row(row) for row in rows]

    @classmethod
    def get_link(
        cls,
        link_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchKnowledgeLink | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_knowledge_links WHERE link_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=?",
                (link_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchKnowledgeLink.from_row(row) if row is not None else None

    @classmethod
    def save_lint_report(
        cls,
        report: ResearchKnowledgeLintReport,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchKnowledgeLintReport:
        resolved = settings or load_research_settings()
        with _KNOWLEDGE_LOCK:
            connection = cls._connect(workdir, resolved)
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO research_knowledge_lint_runs("
                    "run_id,tenant_id,user_id,workspace_id,research_id,status,export_ready,"
                    "checked_link_count,counts_json,issues_json,source_digest,started_at,"
                    "completed_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.run_id,
                        report.tenant_id,
                        report.user_id,
                        report.workspace_id,
                        report.research_id,
                        report.status,
                        1 if report.export_ready else 0,
                        report.checked_link_count,
                        canonical_json(report.counts),
                        canonical_json(report.issues),
                        report.source_digest,
                        report.started_at,
                        report.completed_at,
                        report.idempotency_key,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM research_knowledge_lint_runs WHERE idempotency_key=?",
                    (report.idempotency_key,),
                ).fetchone()
            finally:
                connection.close()
        if row is None:
            raise RuntimeError("knowledge lint report did not persist")
        return ResearchKnowledgeLintReport.from_row(row)

    @classmethod
    def latest_lint_report(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchKnowledgeLintReport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_knowledge_lint_runs WHERE research_id=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? "
                "ORDER BY completed_at DESC, run_id DESC LIMIT 1",
                (research_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchKnowledgeLintReport.from_row(row) if row is not None else None

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
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT object_type,state,COUNT(*) AS count FROM research_knowledge_links "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=? AND superseded_by='' "
                "GROUP BY object_type,state",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            latest = connection.execute(
                "SELECT status,export_ready,completed_at,research_id FROM research_knowledge_lint_runs "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                "ORDER BY completed_at DESC LIMIT 1",
                (tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        by_type: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for row in rows:
            by_type[str(row["object_type"])] = by_type.get(str(row["object_type"]), 0) + int(row["count"])
            by_state[str(row["state"])] = by_state.get(str(row["state"]), 0) + int(row["count"])
        return {
            "object_types": by_type,
            "states": by_state,
            "latest_lint": dict(latest) if latest is not None else None,
        }
