"""Owner-scoped persistence for turn routing and multi-project scope links."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import canonical_json, sha256_text, utc_now_iso
from .routing_models import (
    RESEARCH_DURABILITIES,
    RESEARCH_ROUTING_DECISION_SOURCES,
    RESEARCH_ROUTING_SCHEMA_VERSION,
    RESEARCH_ROUTING_STATES,
    RESEARCH_SCOPE_RELATIONS,
    RESEARCH_SCOPE_STATES,
    ResearchObjectScope,
    ResearchTurnRoute,
)
from .store import ResearchEventStore


def _owner(tenant_id: str, user_id: str, workspace_id: str) -> tuple[str, str, str]:
    value = tuple(str(item or "").strip() for item in (tenant_id, user_id, workspace_id))
    if any(not item for item in value):
        raise ValueError("tenant_id, user_id and workspace_id are required")
    return value  # type: ignore[return-value]


def _research_ids(values: list[str] | None) -> list[str]:
    normalized = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        )
    )
    if len(normalized) > 3:
        raise ValueError("a turn route can reference at most three local research projects")
    return normalized


def _scope_refs(values: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        asset_id = str(raw.get("asset_id") or "").strip()[:240]
        scope_id = str(raw.get("scope_id") or "").strip()[:240]
        pack_id = str(raw.get("pack_id") or "").strip()[:160]
        source_research_id = str(raw.get("source_research_id") or "").strip()[:160]
        if not (asset_id or pack_id) or not (scope_id or source_research_id):
            continue
        key = (asset_id, scope_id, pack_id, source_research_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "asset_id": asset_id,
                "scope_id": scope_id,
                "pack_id": pack_id,
                "source_research_id": source_research_id,
            }
        )
        if len(result) >= 100:
            break
    return result


class ResearchRoutingStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def append_route(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        turn_id: str,
        research_ids: list[str] | None = None,
        imported_scope_refs: list[dict[str, Any]] | None = None,
        durability: str = "transient",
        confidence: float = 0.0,
        routing_state: str = "unresolved",
        reason_code: str = "",
        decision_source: str = "deterministic",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchTurnRoute, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_turn = str(turn_id or "").strip()[:240]
        if not normalized_turn:
            raise ValueError("turn_id is required")
        normalized_research = _research_ids(research_ids)
        normalized_imported = _scope_refs(imported_scope_refs)
        normalized_durability = str(durability or "").strip().lower()
        normalized_state = str(routing_state or "").strip().lower()
        normalized_source = str(decision_source or "").strip().lower()
        if normalized_durability not in RESEARCH_DURABILITIES:
            raise ValueError("unsupported research durability")
        if normalized_state not in RESEARCH_ROUTING_STATES:
            raise ValueError("unsupported research routing state")
        if normalized_source not in RESEARCH_ROUTING_DECISION_SOURCES:
            raise ValueError("unsupported research routing decision source")
        if normalized_state == "not_research":
            if normalized_research or normalized_imported:
                raise ValueError("not_research route cannot reference a research scope")
            normalized_durability = "transient"
        if normalized_state == "resolved" and not normalized_research and not normalized_imported:
            raise ValueError("resolved route requires a local or imported research scope")
        normalized_confidence = max(0.0, min(float(confidence or 0.0), 1.0))
        semantic = {
            "turn_id": normalized_turn,
            "research_ids": normalized_research,
            "imported_scope_refs": normalized_imported,
            "durability": normalized_durability,
            "confidence": round(normalized_confidence, 6),
            "routing_state": normalized_state,
            "reason_code": str(reason_code or "").strip()[:160],
            "decision_source": normalized_source,
        }
        source_digest = sha256_text(canonical_json(semantic))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM research_turn_routes WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND turn_id=? AND superseded_by='' LIMIT 1",
                (*owner, normalized_turn),
            ).fetchone()
            if current is not None and str(current["source_digest"] or "") == source_digest:
                connection.execute("COMMIT")
                return ResearchTurnRoute.from_row(current), False
            revision = int(current["revision"] or 0) + 1 if current is not None else 1
            route_id = f"route-{uuid.uuid4()}"
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "turn_id": normalized_turn, "revision": revision, "digest": source_digest})
            )
            if current is not None:
                connection.execute(
                    "UPDATE research_turn_routes SET superseded_by=? WHERE route_id=?",
                    (route_id, str(current["route_id"])),
                )
            connection.execute(
                "INSERT INTO research_turn_routes(route_id,schema_version,tenant_id,user_id,workspace_id,"
                "turn_id,research_ids_json,imported_scope_refs_json,durability,confidence,routing_state,"
                "reason_code,decision_source,source_digest,revision,created_at,superseded_by,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    route_id,
                    RESEARCH_ROUTING_SCHEMA_VERSION,
                    *owner,
                    normalized_turn,
                    canonical_json(normalized_research),
                    canonical_json(normalized_imported),
                    normalized_durability,
                    normalized_confidence,
                    normalized_state,
                    semantic["reason_code"],
                    normalized_source,
                    source_digest,
                    revision,
                    now,
                    "",
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_turn_routes WHERE route_id=?",
                (route_id,),
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
            raise RuntimeError("research turn route was not created")
        return ResearchTurnRoute.from_row(row), True

    @classmethod
    def get_active_route(
        cls,
        turn_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchTurnRoute | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_turn_routes WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND turn_id=? AND superseded_by='' LIMIT 1",
                (*owner, str(turn_id or "").strip()),
            ).fetchone()
        finally:
            connection.close()
        return ResearchTurnRoute.from_row(row) if row is not None else None

    @classmethod
    def list_routes(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        include_history: bool = False,
        limit: int = 200,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchTurnRoute]:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            sql = (
                "SELECT * FROM research_turn_routes WHERE tenant_id=? AND user_id=? AND workspace_id=?"
                + ("" if include_history else " AND superseded_by=''")
                + " ORDER BY created_at DESC, revision DESC LIMIT ?"
            )
            rows = connection.execute(
                sql,
                (*owner, max(1, min(int(limit or 200), 5000))),
            ).fetchall()
        finally:
            connection.close()
        return [ResearchTurnRoute.from_row(row) for row in rows]

    @classmethod
    def link_object(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str,
        object_id: str,
        research_id: str,
        relation: str = "primary",
        confidence: float = 1.0,
        source_route_id: str = "",
        state: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchObjectScope, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_type = str(object_type or "").strip().lower()[:80]
        normalized_object = str(object_id or "").strip()[:240]
        normalized_research = str(research_id or "").strip()[:160]
        normalized_relation = str(relation or "").strip().lower()
        normalized_state = str(state or "").strip().lower()
        if not normalized_type or not normalized_object or not normalized_research:
            raise ValueError("object_type, object_id and research_id are required")
        if normalized_relation not in RESEARCH_SCOPE_RELATIONS:
            raise ValueError("unsupported research scope relation")
        if normalized_state not in RESEARCH_SCOPE_STATES:
            raise ValueError("unsupported research scope state")
        normalized_confidence = max(0.0, min(float(confidence or 0.0), 1.0))
        semantic = {
            "object_type": normalized_type,
            "object_id": normalized_object,
            "research_id": normalized_research,
            "relation": normalized_relation,
            "confidence": round(normalized_confidence, 6),
            "source_route_id": str(source_route_id or "").strip()[:160],
            "state": normalized_state,
        }
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND object_type=? AND object_id=? AND research_id=? "
                "AND superseded_by='' LIMIT 1",
                (*owner, normalized_type, normalized_object, normalized_research),
            ).fetchone()
            if current is not None:
                current_semantic = {
                    "object_type": str(current["object_type"]),
                    "object_id": str(current["object_id"]),
                    "research_id": str(current["research_id"]),
                    "relation": str(current["relation"]),
                    "confidence": round(float(current["confidence"] or 0.0), 6),
                    "source_route_id": str(current["source_route_id"] or ""),
                    "state": str(current["state"]),
                }
                if current_semantic == semantic:
                    connection.execute("COMMIT")
                    return ResearchObjectScope.from_row(current), False
            scope_link_id = f"scope-link-{uuid.uuid4()}"
            if current is not None:
                connection.execute(
                    "UPDATE research_object_scopes SET superseded_by=? WHERE scope_link_id=?",
                    (scope_link_id, str(current["scope_link_id"])),
                )
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "scope_link_id": scope_link_id, **semantic})
            )
            connection.execute(
                "INSERT INTO research_object_scopes(scope_link_id,schema_version,tenant_id,user_id,workspace_id,"
                "object_type,object_id,research_id,relation,confidence,source_route_id,state,created_at,"
                "superseded_by,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope_link_id,
                    RESEARCH_ROUTING_SCHEMA_VERSION,
                    *owner,
                    normalized_type,
                    normalized_object,
                    normalized_research,
                    normalized_relation,
                    normalized_confidence,
                    semantic["source_route_id"],
                    normalized_state,
                    now,
                    "",
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_object_scopes WHERE scope_link_id=?",
                (scope_link_id,),
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
            raise RuntimeError("research object scope was not created")
        return ResearchObjectScope.from_row(row), True

    @classmethod
    def list_object_scopes(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        object_id: str = "",
        research_id: str = "",
        include_history: bool = False,
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchObjectScope]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [*owner]
        for column, raw in (
            ("object_type", object_type),
            ("object_id", object_id),
            ("research_id", research_id),
        ):
            value = str(raw or "").strip()
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if not include_history:
            clauses.append("superseded_by=''")
        params.append(max(1, min(int(limit or 500), 5000)))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_object_scopes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchObjectScope.from_row(row) for row in rows]

    @classmethod
    def resolve_route_for_event(
        cls,
        event: Any,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchTurnRoute | None:
        owner = _owner(
            str(getattr(event, "tenant_id", "") or ""),
            str(getattr(event, "user_id", "") or ""),
            str(getattr(event, "workspace_id", "") or ""),
        )
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            turn_candidates: list[str] = []

            def add_turn(value: Any) -> None:
                normalized = str(value or "").strip()
                if normalized and normalized not in turn_candidates:
                    turn_candidates.append(normalized)

            add_turn(getattr(event, "turn_id", ""))
            parent_event_id = str(getattr(event, "parent_event_id", "") or "").strip()
            if parent_event_id:
                parent = connection.execute(
                    "SELECT turn_id FROM research_events WHERE event_id=? AND tenant_id=? "
                    "AND user_id=? AND workspace_id=? LIMIT 1",
                    (parent_event_id, *owner),
                ).fetchone()
                if parent is not None:
                    add_turn(parent["turn_id"])
            trace_id = str(getattr(event, "trace_id", "") or "").strip()
            if trace_id:
                rows = connection.execute(
                    "SELECT DISTINCT turn_id FROM research_events WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND trace_id=? AND turn_id!='' ORDER BY occurred_at DESC LIMIT 5",
                    (*owner, trace_id),
                ).fetchall()
                for row in rows:
                    add_turn(row["turn_id"])
            source_kind = str(getattr(event, "source_kind", "") or "").strip()
            source_id = str(getattr(event, "source_id", "") or "").strip()
            if source_kind and source_id:
                rows = connection.execute(
                    "SELECT DISTINCT turn_id FROM research_events WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND source_kind=? AND source_id=? AND turn_id!='' "
                    "ORDER BY occurred_at DESC LIMIT 5",
                    (*owner, source_kind, source_id),
                ).fetchall()
                for row in rows:
                    add_turn(row["turn_id"])

            for turn_id in turn_candidates:
                row = connection.execute(
                    "SELECT * FROM research_turn_routes WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND turn_id=? AND superseded_by='' LIMIT 1",
                    (*owner, turn_id),
                ).fetchone()
                if row is not None:
                    return ResearchTurnRoute.from_row(row)
        finally:
            connection.close()
        return None

    @classmethod
    def _synchronize_event_scopes(
        cls,
        event: Any,
        route: ResearchTurnRoute,
        *,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> int:
        event_id = str(getattr(event, "event_id", "") or "").strip()
        if not event_id:
            return 0
        owner = _owner(route.tenant_id, route.user_id, route.workspace_id)
        desired = set(route.research_ids)
        relation = "shared" if len(desired) > 1 else "primary"
        connection = cls._connect(workdir, settings)
        changed = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM research_object_scopes WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND object_type='event' AND object_id=? "
                "AND superseded_by=''",
                (*owner, event_id),
            ).fetchall()
            current_by_research = {str(row["research_id"]): row for row in rows}
            all_research_ids = list(dict.fromkeys([*current_by_research, *route.research_ids]))
            now = utc_now_iso()
            for research_id in all_research_ids:
                current = current_by_research.get(research_id)
                target_state = "active" if research_id in desired else "removed"
                target_relation = (
                    relation
                    if target_state == "active"
                    else str(current["relation"] if current is not None else "primary")
                )
                target_confidence = (
                    float(route.confidence)
                    if target_state == "active"
                    else float(current["confidence"] if current is not None else 0.0)
                )
                if current is not None and (
                    str(current["relation"]) == target_relation
                    and round(float(current["confidence"] or 0.0), 6)
                    == round(target_confidence, 6)
                    and str(current["source_route_id"] or "") == route.route_id
                    and str(current["state"]) == target_state
                ):
                    continue
                scope_link_id = f"scope-link-{uuid.uuid4()}"
                if current is not None:
                    connection.execute(
                        "UPDATE research_object_scopes SET superseded_by=? WHERE scope_link_id=?",
                        (scope_link_id, str(current["scope_link_id"])),
                    )
                semantic = {
                    "owner": owner,
                    "object_type": "event",
                    "object_id": event_id,
                    "research_id": research_id,
                    "relation": target_relation,
                    "confidence": round(target_confidence, 6),
                    "source_route_id": route.route_id,
                    "state": target_state,
                    "scope_link_id": scope_link_id,
                }
                connection.execute(
                    "INSERT INTO research_object_scopes(scope_link_id,schema_version,tenant_id,user_id,"
                    "workspace_id,object_type,object_id,research_id,relation,confidence,source_route_id,"
                    "state,created_at,superseded_by,idempotency_key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope_link_id,
                        RESEARCH_ROUTING_SCHEMA_VERSION,
                        *owner,
                        "event",
                        event_id,
                        research_id,
                        target_relation,
                        target_confidence,
                        route.route_id,
                        target_state,
                        now,
                        "",
                        sha256_text(canonical_json(semantic)),
                    ),
                )
                changed += 1
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        if route.imported_scope_refs and route.durability != "transient":
            try:
                from .pack_overlay import ResearchPackOverlayProjector

                ResearchPackOverlayProjector(
                    tenant_id=route.tenant_id,
                    user_id=route.user_id,
                    workspace_id=route.workspace_id,
                    workdir=workdir,
                    settings=settings,
                ).project_event(event, route)
            except Exception:
                # Overlay is a repairable continuation projection.  A failure
                # must not roll back an already committed route/scope update.
                pass
        return changed

    @classmethod
    def inherit_event(
        cls,
        event: Any,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchTurnRoute | None:
        resolved = settings or load_research_settings()
        if not resolved.routing_active:
            return None
        route = cls.resolve_route_for_event(
            event,
            workdir=workdir,
            settings=resolved,
        )
        if route is None:
            return None
        cls._synchronize_event_scopes(
            event,
            route,
            workdir=workdir,
            settings=resolved,
        )
        return route

    @classmethod
    def synchronize_turn_events(
        cls,
        route: ResearchTurnRoute,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> int:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_events WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                "AND turn_id=? ORDER BY occurred_at,event_id",
                (
                    route.tenant_id,
                    route.user_id,
                    route.workspace_id,
                    route.turn_id,
                ),
            ).fetchall()
        finally:
            connection.close()
        from .models import ResearchEvent

        return sum(
            cls._synchronize_event_scopes(
                ResearchEvent.from_row(row),
                route,
                workdir=workdir,
                settings=resolved,
            )
            for row in rows
        )

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
                "SELECT routing_state,decision_source,COUNT(*) AS count "
                "FROM research_turn_routes WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                "AND superseded_by='' GROUP BY routing_state,decision_source",
                owner,
            ).fetchall()
        finally:
            connection.close()
        by_state: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for row in rows:
            count = int(row["count"] or 0)
            state = str(row["routing_state"] or "unknown")
            source = str(row["decision_source"] or "unknown")
            by_state[state] = by_state.get(state, 0) + count
            by_source[source] = by_source.get(source, 0) + count
        return {
            "count": sum(by_state.values()),
            "by_state": by_state,
            "by_decision_source": by_source,
        }


__all__ = ["ResearchRoutingStore"]
