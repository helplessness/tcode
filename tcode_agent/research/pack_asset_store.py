"""Persistence for imported ResearchPack assets, scopes, overlays, and mappings."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import canonical_json, sha256_text, utc_now_iso
from .pack_asset_models import (
    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
    RESEARCH_PACK_ASSET_STATES,
    RESEARCH_PACK_OVERLAY_OBJECT_STATES,
    RESEARCH_PACK_OVERLAY_STATES,
    RESEARCH_PACK_SCOPE_STATES,
    ResearchPackAsset,
    ResearchPackObjectMapping,
    ResearchPackObjectSnapshot,
    ResearchPackOverlay,
    ResearchPackOverlayObject,
    ResearchPackSourceScope,
)
from .pack_v2_models import RESEARCH_PACK_V2_SCOPE_MODES, ResearchPackV2Scope
from .store import ResearchEventStore


def _owner(tenant_id: str, user_id: str, workspace_id: str) -> tuple[str, str, str]:
    value = tuple(str(item or "").strip() for item in (tenant_id, user_id, workspace_id))
    if any(not item for item in value):
        raise ValueError("tenant_id, user_id and workspace_id are required")
    return value  # type: ignore[return-value]


def _strings(values: list[str] | None, *, limit: int = 1000) -> list[str]:
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        )
    )[: max(0, limit)]


def _scope_refs(values: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        value = {
            key: str(raw.get(key) or "").strip()[:240]
            for key in ("asset_id", "scope_id", "pack_id", "source_research_id")
        }
        key = (
            value["asset_id"],
            value["scope_id"],
            value["pack_id"],
            value["source_research_id"],
        )
        if not (value["asset_id"] or value["pack_id"]):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= 100:
            break
    return result


class ResearchPackAssetStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def create_asset(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        pack_id: str,
        pack_schema_version: str,
        title: str,
        scope_mode: str,
        source_research_ids: list[str] | None,
        manifest: dict[str, Any],
        summary: dict[str, Any] | None = None,
        lineage: dict[str, Any] | None = None,
        source_digest: str = "",
        archive_sha256: str = "",
        started_at: str = "",
        ended_at: str = "",
        status: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchPackAsset, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_pack = str(pack_id or "").strip()[:160]
        normalized_status = str(status or "").strip().lower()
        normalized_mode = str(scope_mode or "").strip().lower()
        if not normalized_pack:
            raise ValueError("pack_id is required")
        if normalized_status not in RESEARCH_PACK_ASSET_STATES:
            raise ValueError("unsupported ResearchPack asset state")
        if normalized_mode not in RESEARCH_PACK_V2_SCOPE_MODES:
            raise ValueError("unsupported ResearchPack scope mode")
        researches = _strings(source_research_ids, limit=100)
        ResearchPackV2Scope(
            mode=normalized_mode,
            research_ids=researches,
            started_at=str(started_at or "").strip()[:48],
            ended_at=str(ended_at or "").strip()[:48],
        )
        normalized_manifest = dict(manifest or {})
        normalized_summary = dict(summary or {})
        normalized_lineage = dict(lineage or {})
        semantic_digest = str(source_digest or "").strip() or sha256_text(
            canonical_json(
                {
                    "pack_schema_version": str(pack_schema_version or "").strip()[:40],
                    "title": str(title or "导入的研究资料包").strip()[:500],
                    "manifest": normalized_manifest,
                    "summary": normalized_summary,
                    "lineage": normalized_lineage,
                    "scope_mode": normalized_mode,
                    "source_research_ids": researches,
                    "started_at": str(started_at or "").strip()[:48],
                    "ended_at": str(ended_at or "").strip()[:48],
                    "archive_sha256": str(archive_sha256 or "").strip()[:160],
                }
            )
        )
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM research_pack_assets WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND pack_id=? LIMIT 1",
                (*owner, normalized_pack),
            ).fetchone()
            if current is not None:
                if str(current["source_digest"] or "") != semantic_digest:
                    raise ValueError("ResearchPack asset pack_id already exists with different content")
                connection.execute("COMMIT")
                return ResearchPackAsset.from_row(current), False
            asset_id = f"pack-asset-{uuid.uuid4()}"
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "pack_id": normalized_pack, "source_digest": semantic_digest})
            )
            connection.execute(
                "INSERT INTO research_pack_assets(asset_id,schema_version,tenant_id,user_id,workspace_id,pack_id,"
                "pack_schema_version,title,status,scope_mode,started_at,ended_at,source_research_ids_json,"
                "manifest_json,summary_json,lineage_json,source_digest,archive_sha256,imported_at,created_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    *owner,
                    normalized_pack,
                    str(pack_schema_version or "").strip()[:40],
                    str(title or "导入的研究资料包").strip()[:500],
                    normalized_status,
                    normalized_mode,
                    str(started_at or "").strip()[:48],
                    str(ended_at or "").strip()[:48],
                    canonical_json(researches),
                    canonical_json(normalized_manifest),
                    canonical_json(normalized_summary),
                    canonical_json(normalized_lineage),
                    semantic_digest,
                    str(archive_sha256 or "").strip()[:160],
                    now,
                    now,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_assets WHERE asset_id=?",
                (asset_id,),
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
            raise RuntimeError("ResearchPack asset was not created")
        return ResearchPackAsset.from_row(row), True

    @classmethod
    def get_asset(
        cls,
        asset_or_pack_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        include_archived: bool = False,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackAsset | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        value = str(asset_or_pack_id or "").strip()
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_assets WHERE (asset_id=? OR pack_id=?) AND tenant_id=? "
                "AND user_id=? AND workspace_id=? AND (status!='archived' OR ?) "
                "ORDER BY CASE WHEN asset_id=? THEN 0 ELSE 1 END, imported_at DESC LIMIT 1",
                (value, value, *owner, int(include_archived), value),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackAsset.from_row(row) if row is not None else None

    @classmethod
    def set_archived(cls, asset_id: str, *, tenant_id: str, user_id: str, workspace_id: str,
                     archived: bool, workdir: str | None = None, settings: ResearchSettings | None = None) -> bool:
        owner = _owner(tenant_id, user_id, workspace_id)
        connection = cls._connect(workdir, settings or load_research_settings())
        try:
            with connection:
                result = connection.execute("UPDATE research_pack_assets SET status=? WHERE asset_id=? AND tenant_id=? AND user_id=? AND workspace_id=? AND status IN ('active','archived')",
                    ('archived' if archived else 'active', asset_id, *owner))
            return result.rowcount > 0
        finally:
            connection.close()

    @classmethod
    def list_assets(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackAsset]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [*owner]
        if status:
            normalized_status = str(status).strip().lower()
            if normalized_status not in RESEARCH_PACK_ASSET_STATES:
                raise ValueError("unsupported ResearchPack asset state")
            clauses.append("status=?")
            params.append(normalized_status)
        params.append(max(1, min(int(limit or 100), 1000)))
        params.append(max(0, int(offset)))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_assets WHERE "
                + " AND ".join(clauses)
                + " ORDER BY imported_at DESC,asset_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackAsset.from_row(row) for row in rows]

    @classmethod
    def add_scope(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        source_research_id: str,
        title: str,
        scope_type: str = "project",
        summary: str = "",
        keywords: list[str] | None = None,
        state: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchPackSourceScope, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_type = str(scope_type or "project").strip().lower()
        normalized_source = str(source_research_id or "").strip()[:160]
        normalized_state = str(state or "").strip().lower()
        if normalized_type not in {"project", "unassigned"}:
            raise ValueError("unsupported ResearchPack source scope type")
        if normalized_type == "project" and not normalized_source:
            raise ValueError("source_research_id is required")
        if normalized_type == "unassigned":
            normalized_source = ""
        if normalized_state not in RESEARCH_PACK_SCOPE_STATES:
            raise ValueError("unsupported ResearchPack source scope state")
        normalized_keywords = _strings(keywords, limit=100)
        semantic = {
            "title": str(title or "未命名来源项目").strip()[:500],
            "summary": str(summary or "").strip()[:12000],
            "keywords": normalized_keywords,
            "state": normalized_state,
        }
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                "SELECT 1 FROM research_pack_assets WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=?",
                (asset_id, *owner),
            ).fetchone()
            if asset is None:
                raise ValueError("ResearchPack asset does not belong to owner")
            current = connection.execute(
                "SELECT * FROM research_pack_scopes WHERE asset_id=? AND source_research_id=? LIMIT 1",
                (asset_id, normalized_source),
            ).fetchone()
            if current is not None:
                current_semantic = {
                    "title": str(current["title"]),
                    "summary": str(current["summary"]),
                    "keywords": json.loads(str(current["keywords_json"] or "[]")),
                    "state": str(current["state"]),
                }
                if str(current["scope_type"] or "project") != normalized_type:
                    raise ValueError("ResearchPack source scope already exists with different type")
                if current_semantic != semantic:
                    raise ValueError("ResearchPack source scope already exists with different content")
                connection.execute("COMMIT")
                return ResearchPackSourceScope.from_row(current), False
            scope_id = f"pack-scope-{uuid.uuid4()}"
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "asset_id": asset_id, "source_research_id": normalized_source})
            )
            connection.execute(
                "INSERT INTO research_pack_scopes(scope_id,schema_version,asset_id,tenant_id,user_id,workspace_id,"
                "scope_type,source_research_id,title,summary,keywords_json,state,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    asset_id,
                    *owner,
                    normalized_type,
                    normalized_source,
                    semantic["title"],
                    semantic["summary"],
                    canonical_json(normalized_keywords),
                    normalized_state,
                    now,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_scopes WHERE scope_id=?",
                (scope_id,),
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
            raise RuntimeError("ResearchPack source scope was not created")
        return ResearchPackSourceScope.from_row(row), True

    @classmethod
    def list_scopes(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackSourceScope]:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_scopes WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=? ORDER BY created_at,source_research_id",
                (asset_id, *owner),
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackSourceScope.from_row(row) for row in rows]

    @classmethod
    def insert_import_snapshot(
        cls,
        connection: sqlite3.Connection,
        *,
        asset_id: str,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        pack_id: str,
        pack_schema_version: str,
        title: str,
        scope_mode: str,
        started_at: str,
        ended_at: str,
        source_research_ids: list[str],
        manifest: dict[str, Any],
        summary: dict[str, Any],
        lineage: dict[str, Any],
        source_digest: str,
        archive_sha256: str,
        scopes: list[dict[str, Any]],
        objects: list[dict[str, Any]],
        parent_pack_ids: list[str],
        imported_at: str,
    ) -> tuple[ResearchPackAsset, ResearchPackOverlay, dict[str, dict[str, str]]]:
        """Insert one immutable imported Asset using the caller's transaction."""

        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_asset = str(asset_id or "").strip()
        normalized_pack = str(pack_id or "").strip()[:160]
        normalized_mode = str(scope_mode or "").strip().lower()
        if not normalized_asset or not normalized_pack:
            raise ValueError("asset_id and pack_id are required")
        if normalized_mode not in RESEARCH_PACK_V2_SCOPE_MODES:
            raise ValueError("unsupported ResearchPack scope mode")
        researches = _strings(source_research_ids, limit=100)
        ResearchPackV2Scope(
            mode=normalized_mode,
            research_ids=researches,
            started_at=str(started_at or "").strip()[:48],
            ended_at=str(ended_at or "").strip()[:48],
        )
        now = str(imported_at or utc_now_iso())
        asset_key = sha256_text(
            canonical_json({"owner": owner, "asset_id": normalized_asset})
        )
        connection.execute(
            "INSERT INTO research_pack_assets(asset_id,schema_version,tenant_id,user_id,workspace_id,pack_id,"
            "pack_schema_version,title,status,scope_mode,started_at,ended_at,source_research_ids_json,"
            "manifest_json,summary_json,lineage_json,source_digest,archive_sha256,imported_at,created_at,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                normalized_asset,
                RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                *owner,
                normalized_pack,
                str(pack_schema_version or "").strip()[:40],
                str(title or "导入的研究资料包").strip()[:500],
                "active",
                normalized_mode,
                str(started_at or "").strip()[:48],
                str(ended_at or "").strip()[:48],
                canonical_json(researches),
                canonical_json(dict(manifest or {})),
                canonical_json(dict(summary or {})),
                canonical_json(dict(lineage or {})),
                str(source_digest or "").strip(),
                str(archive_sha256 or "").strip()[:160],
                now,
                now,
                asset_key,
            ),
        )

        for raw_scope in scopes:
            scope_type = str(raw_scope.get("scope_type") or "project").strip().lower()
            source_id = str(raw_scope.get("source_research_id") or "").strip()[:160]
            if scope_type not in {"project", "unassigned"}:
                raise ValueError("unsupported ResearchPack source scope type")
            if scope_type == "project" and not source_id:
                raise ValueError("project source scope requires source_research_id")
            if scope_type == "unassigned":
                source_id = ""
            scope_id = f"pack-scope-{uuid.uuid4()}"
            scope_key = sha256_text(
                canonical_json(
                    {
                        "owner": owner,
                        "asset_id": normalized_asset,
                        "scope_type": scope_type,
                        "source_research_id": source_id,
                    }
                )
            )
            connection.execute(
                "INSERT INTO research_pack_scopes(scope_id,schema_version,asset_id,tenant_id,user_id,workspace_id,"
                "scope_type,source_research_id,title,summary,keywords_json,state,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    normalized_asset,
                    *owner,
                    scope_type,
                    source_id,
                    str(raw_scope.get("title") or "未命名来源项目").strip()[:500],
                    str(raw_scope.get("summary") or "").strip()[:12000],
                    canonical_json(_strings(raw_scope.get("keywords"), limit=100)),
                    "active",
                    now,
                    scope_key,
                ),
            )

        object_mappings: dict[str, dict[str, str]] = {}
        for raw_object in objects:
            object_type = str(raw_object.get("object_type") or "").strip().lower()[:80]
            source_id = str(raw_object.get("source_object_id") or "").strip()[:240]
            payload = dict(raw_object.get("payload") or {})
            if not object_type or not source_id:
                raise ValueError("imported ResearchPack object type and source ID are required")
            object_id = str(raw_object.get("object_id") or f"pack-object-{uuid.uuid4()}")
            content_hash = str(raw_object.get("content_hash") or "").strip() or sha256_text(
                canonical_json(payload)
            )
            distribution = str(raw_object.get("distribution") or "snapshot").strip()[:80]
            available_layers = _strings(raw_object.get("available_layers"), limit=100)
            missing_layers = _strings(raw_object.get("missing_layers"), limit=100)
            object_key = sha256_text(
                canonical_json(
                    {
                        "owner": owner,
                        "asset_id": normalized_asset,
                        "object_type": object_type,
                        "source_object_id": source_id,
                    }
                )
            )
            connection.execute(
                "INSERT INTO research_pack_objects(object_id,schema_version,asset_id,tenant_id,user_id,workspace_id,"
                "object_type,source_object_id,source_research_ids_json,content_hash,immutable,distribution,"
                "available_layers_json,missing_layers_json,payload_json,created_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    object_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    normalized_asset,
                    *owner,
                    object_type,
                    source_id,
                    canonical_json(_strings(raw_object.get("source_research_ids"), limit=100)),
                    content_hash[:160],
                    1,
                    distribution,
                    canonical_json(available_layers),
                    canonical_json(missing_layers),
                    canonical_json(payload),
                    now,
                    object_key,
                ),
            )
            mapping_id = f"pack-map-{uuid.uuid4()}"
            mapping_key = sha256_text(
                canonical_json(
                    {
                        "owner": owner,
                        "asset_id": normalized_asset,
                        "type": object_type,
                        "source": source_id,
                    }
                )
            )
            mapping_metadata = {
                "origin_pack_id": normalized_pack,
                "immutable": True,
                "distribution": distribution,
                "materialized_local_id": str(
                    raw_object.get("materialized_local_id") or ""
                ),
            }
            connection.execute(
                "INSERT INTO research_pack_object_mappings(mapping_id,schema_version,asset_id,tenant_id,user_id,"
                "workspace_id,object_type,source_object_id,local_object_id,content_hash,metadata_json,created_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mapping_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    normalized_asset,
                    *owner,
                    object_type,
                    source_id,
                    object_id,
                    content_hash[:160],
                    canonical_json(mapping_metadata),
                    now,
                    mapping_key,
                ),
            )
            object_mappings.setdefault(object_type, {})[source_id] = object_id

        overlay_id = f"pack-overlay-{uuid.uuid4()}"
        parents = _strings(
            [normalized_pack, *list(parent_pack_ids or [])], limit=100
        )
        overlay_digest = sha256_text(
            canonical_json(
                {
                    "asset_id": normalized_asset,
                    "parent_pack_ids": parents,
                    "objects": object_mappings,
                }
            )
        )
        overlay_key = sha256_text(
            canonical_json({"owner": owner, "asset_id": normalized_asset, "overlay": "initial"})
        )
        connection.execute(
            "INSERT INTO research_pack_overlays(overlay_id,schema_version,asset_id,tenant_id,user_id,"
            "workspace_id,parent_pack_ids_json,status,source_digest,created_at,updated_at,idempotency_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                overlay_id,
                RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                normalized_asset,
                *owner,
                canonical_json(parents),
                "active",
                overlay_digest,
                now,
                now,
                overlay_key,
            ),
        )
        asset_row = connection.execute(
            "SELECT * FROM research_pack_assets WHERE asset_id=?", (normalized_asset,)
        ).fetchone()
        overlay_row = connection.execute(
            "SELECT * FROM research_pack_overlays WHERE overlay_id=?", (overlay_id,)
        ).fetchone()
        if asset_row is None or overlay_row is None:
            raise RuntimeError("ResearchPack import snapshot was not created")
        return (
            ResearchPackAsset.from_row(asset_row),
            ResearchPackOverlay.from_row(overlay_row),
            object_mappings,
        )

    @classmethod
    def get_object(
        cls,
        asset_id: str,
        object_or_source_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackObjectSnapshot | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = [
            "asset_id=?",
            "(object_id=? OR source_object_id=?)",
            "tenant_id=?",
            "user_id=?",
            "workspace_id=?",
        ]
        params: list[Any] = [asset_id, object_or_source_id, object_or_source_id, *owner]
        if object_type:
            clauses.append("object_type=?")
            params.append(str(object_type).strip().lower())
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_objects WHERE "
                + " AND ".join(clauses)
                + " LIMIT 1",
                params,
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackObjectSnapshot.from_row(row) if row is not None else None

    @classmethod
    def list_objects(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        limit: int = 100,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackObjectSnapshot]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["asset_id=?", "tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [asset_id, *owner]
        if object_type:
            clauses.append("object_type=?")
            params.append(str(object_type).strip().lower())
        params.append(max(1, min(int(limit or 100), 10000)))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_objects WHERE "
                + " AND ".join(clauses)
                + " ORDER BY object_type,created_at,object_id LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackObjectSnapshot.from_row(row) for row in rows]

    @classmethod
    def object_counts(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, int]:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT object_type,COUNT(*) AS total FROM research_pack_objects "
                "WHERE asset_id=? AND tenant_id=? AND user_id=? AND workspace_id=? "
                "GROUP BY object_type ORDER BY object_type",
                (asset_id, *owner),
            ).fetchall()
        finally:
            connection.close()
        return {str(row["object_type"]): int(row["total"] or 0) for row in rows}

    @classmethod
    def routing_index(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        asset_limit: int = 12,
        scope_limit: int = 48,
        pinned_asset_ids: list[str] | tuple[str, ...] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[dict[str, Any]]:
        """Return a bounded, metadata-only imported Scope index for routing."""

        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        pinned = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in list(pinned_asset_ids or [])
                if str(value or "").strip()
            )
        )[:20]
        pinned_clause = ""
        pinned_order = ""
        if pinned:
            pinned_clause = " OR a.asset_id IN (" + ",".join("?" for _ in pinned) + ")"
            pinned_order = (
                "CASE WHEN a.asset_id IN ("
                + ",".join("?" for _ in pinned)
                + ") THEN 0 ELSE 1 END,"
            )
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT a.asset_id,a.pack_id,a.title AS asset_title,a.started_at,a.ended_at,"
                "a.summary_json,a.lineage_json,o.status AS overlay_status,o.updated_at AS overlay_updated_at,"
                "s.scope_id,s.scope_type,s.source_research_id,s.title,s.summary,s.keywords_json "
                "FROM research_pack_assets a "
                "JOIN research_pack_scopes s ON s.asset_id=a.asset_id AND s.state='active' "
                "LEFT JOIN research_pack_overlays o ON o.asset_id=a.asset_id AND o.status='active' "
                "WHERE a.tenant_id=? AND a.user_id=? AND a.workspace_id=? AND a.status='active' "
                "AND (a.asset_id IN (SELECT asset_id FROM research_pack_assets WHERE tenant_id=? "
                "AND user_id=? AND workspace_id=? AND status='active' ORDER BY imported_at DESC LIMIT ?) "
                + pinned_clause
                + ") "
                "ORDER BY " + pinned_order + "a.imported_at DESC,s.scope_type,s.title,s.scope_id LIMIT ?",
                (*owner, *owner, max(1, min(int(asset_limit or 12), 100)), *pinned, *pinned,
                 max(1, min(int(scope_limit or 48), 500))),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                summary = json.loads(str(row["summary_json"] or "{}"))
            except Exception:
                summary = {}
            try:
                lineage = json.loads(str(row["lineage_json"] or "{}"))
            except Exception:
                lineage = {}
            try:
                keywords = json.loads(str(row["keywords_json"] or "[]"))
            except Exception:
                keywords = []
            gate = dict(summary.get("conflict_gate") or {}) if isinstance(summary, dict) else {}
            availability = (
                dict(summary.get("lineage_availability") or {})
                if isinstance(summary, dict)
                else {}
            )
            result.append(
                {
                    "asset_id": str(row["asset_id"]),
                    "scope_id": str(row["scope_id"]),
                    "pack_id": str(row["pack_id"]),
                    "source_research_id": str(row["source_research_id"] or ""),
                    "scope_type": str(row["scope_type"] or "project"),
                    "title": str(row["title"] or "")[:500],
                    "summary": str(row["summary"] or "")[:1200],
                    "keywords": _strings(keywords if isinstance(keywords, list) else [], limit=20),
                    "asset_title": str(row["asset_title"] or "")[:500],
                    "started_at": str(row["started_at"] or ""),
                    "ended_at": str(row["ended_at"] or ""),
                    "overlay_status": str(row["overlay_status"] or ""),
                    "overlay_updated_at": str(row["overlay_updated_at"] or ""),
                    "continuation_blocked": bool(gate.get("continuation_blocked")),
                    "continuation_warning": bool(availability.get("continuation_warning")),
                    "generation": int(lineage.get("generation") or 1),
                }
            )
        return result

    @classmethod
    def append_overlay_object(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str,
        object_key: str,
        local_object_id: str,
        payload: dict[str, Any],
        imported_scope_refs: list[dict[str, Any]],
        origin_pack_ids: list[str] | None = None,
        state: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchPackOverlayObject, bool]:
        """Append or revise one local Overlay object without mutating its Parent."""

        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_asset = str(asset_id or "").strip()[:240]
        normalized_type = str(object_type or "").strip().lower()[:80]
        normalized_key = str(object_key or "").strip()[:500]
        normalized_local = str(local_object_id or "").strip()[:500]
        normalized_state = str(state or "").strip().lower()
        if not normalized_asset or not normalized_type or not normalized_key or not normalized_local:
            raise ValueError("asset_id, object_type, object_key and local_object_id are required")
        if normalized_state not in RESEARCH_PACK_OVERLAY_OBJECT_STATES:
            raise ValueError("unsupported ResearchPack Overlay object state")
        normalized_payload = dict(payload or {})
        normalized_refs = _scope_refs(imported_scope_refs)
        if not normalized_refs:
            raise ValueError("Overlay object requires at least one imported Scope reference")
        normalized_origins = _strings(origin_pack_ids, limit=100)
        content_hash = sha256_text(canonical_json(normalized_payload))
        semantic = {
            "object_type": normalized_type,
            "object_key": normalized_key,
            "local_object_id": normalized_local,
            "imported_scope_refs": normalized_refs,
            "origin_pack_ids": normalized_origins,
            "payload": normalized_payload,
            "content_hash": content_hash,
            "state": normalized_state,
        }
        source_digest = sha256_text(canonical_json(semantic))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                "SELECT * FROM research_pack_assets WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=? AND status='active'",
                (normalized_asset, *owner),
            ).fetchone()
            if asset is None:
                raise ValueError("ResearchPack asset does not belong to owner")
            try:
                asset_summary = json.loads(str(asset["summary_json"] or "{}"))
            except Exception:
                asset_summary = {}
            gate = dict(asset_summary.get("conflict_gate") or {}) if isinstance(asset_summary, dict) else {}
            if gate.get("continuation_blocked"):
                raise ValueError("ResearchPack continuation is blocked by unresolved conflicts")
            overlay = connection.execute(
                "SELECT * FROM research_pack_overlays WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=? AND status='active' LIMIT 1",
                (normalized_asset, *owner),
            ).fetchone()
            if overlay is None:
                raise ValueError("active ResearchPack Overlay was not found")
            scope_rows = connection.execute(
                "SELECT scope_id,source_research_id FROM research_pack_scopes WHERE asset_id=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? AND state='active'",
                (normalized_asset, *owner),
            ).fetchall()
            scope_by_id = {str(row["scope_id"]): str(row["source_research_id"] or "") for row in scope_rows}
            source_ids = set(scope_by_id.values())
            canonical_refs: list[dict[str, str]] = []
            for ref in normalized_refs:
                if ref.get("asset_id") and ref["asset_id"] != normalized_asset:
                    continue
                scope_id = ref.get("scope_id", "")
                source_id = ref.get("source_research_id", "")
                if scope_id:
                    if scope_id not in scope_by_id:
                        continue
                    source_id = scope_by_id[scope_id]
                elif source_id not in source_ids:
                    continue
                canonical_refs.append(
                    {
                        "asset_id": normalized_asset,
                        "scope_id": scope_id,
                        "pack_id": str(asset["pack_id"]),
                        "source_research_id": source_id,
                    }
                )
            canonical_refs = _scope_refs(canonical_refs)
            if not canonical_refs:
                raise ValueError("Overlay object references a Scope outside the target Asset")
            semantic["imported_scope_refs"] = canonical_refs
            semantic["origin_pack_ids"] = normalized_origins or [str(asset["pack_id"])]
            source_digest = sha256_text(canonical_json(semantic))
            current = connection.execute(
                "SELECT * FROM research_pack_overlay_objects WHERE overlay_id=? AND object_type=? "
                "AND object_key=? AND superseded_by='' LIMIT 1",
                (str(overlay["overlay_id"]), normalized_type, normalized_key),
            ).fetchone()
            if current is not None and str(current["source_digest"] or "") == source_digest:
                connection.execute("COMMIT")
                return ResearchPackOverlayObject.from_row(current), False
            revision = int(current["revision"] or 0) + 1 if current is not None else 1
            overlay_object_id = f"pack-overlay-object-{uuid.uuid4()}"
            now = utc_now_iso()
            if current is not None:
                connection.execute(
                    "UPDATE research_pack_overlay_objects SET superseded_by=? WHERE overlay_object_id=?",
                    (overlay_object_id, str(current["overlay_object_id"])),
                )
            idempotency_key = sha256_text(
                canonical_json(
                    {
                        "owner": owner,
                        "overlay_id": str(overlay["overlay_id"]),
                        "object_type": normalized_type,
                        "object_key": normalized_key,
                        "revision": revision,
                        "source_digest": source_digest,
                    }
                )
            )
            connection.execute(
                "INSERT INTO research_pack_overlay_objects(overlay_object_id,schema_version,overlay_id,asset_id,"
                "tenant_id,user_id,workspace_id,object_type,object_key,local_object_id,"
                "imported_scope_refs_json,origin_pack_ids_json,payload_json,content_hash,revision,state,"
                "source_digest,supersedes_overlay_object_id,superseded_by,created_at,updated_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    overlay_object_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    str(overlay["overlay_id"]),
                    normalized_asset,
                    *owner,
                    normalized_type,
                    normalized_key,
                    normalized_local,
                    canonical_json(canonical_refs),
                    canonical_json(semantic["origin_pack_ids"]),
                    canonical_json(normalized_payload),
                    content_hash,
                    revision,
                    normalized_state,
                    source_digest,
                    str(current["overlay_object_id"] if current is not None else ""),
                    "",
                    now,
                    now,
                    idempotency_key,
                ),
            )
            overlay_digest = sha256_text(
                canonical_json(
                    {
                        "previous": str(overlay["source_digest"] or ""),
                        "object": source_digest,
                        "revision": revision,
                    }
                )
            )
            connection.execute(
                "UPDATE research_pack_overlays SET source_digest=?,updated_at=? WHERE overlay_id=?",
                (overlay_digest, now, str(overlay["overlay_id"])),
            )
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM research_pack_overlay_objects WHERE asset_id=? "
                    "AND superseded_by='' AND state='active'",
                    (normalized_asset,),
                ).fetchone()[0]
            )
            if not isinstance(asset_summary, dict):
                asset_summary = {}
            asset_summary["overlay"] = {
                "state": "active" if active_count else "empty",
                "local_object_count": active_count,
                "updated_at": now,
            }
            connection.execute(
                "UPDATE research_pack_assets SET summary_json=? WHERE asset_id=?",
                (canonical_json(asset_summary), normalized_asset),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_overlay_objects WHERE overlay_object_id=?",
                (overlay_object_id,),
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
            raise RuntimeError("ResearchPack Overlay object was not created")
        return ResearchPackOverlayObject.from_row(row), True

    @classmethod
    def get_overlay_object(
        cls,
        asset_id: str,
        object_or_local_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        include_history: bool = False,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackOverlayObject | None:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = [
            "asset_id=?",
            "(overlay_object_id=? OR local_object_id=? OR object_key=?)",
            "tenant_id=?",
            "user_id=?",
            "workspace_id=?",
        ]
        params: list[Any] = [asset_id, object_or_local_id, object_or_local_id, object_or_local_id, *owner]
        if object_type:
            clauses.append("object_type=?")
            params.append(str(object_type).strip().lower())
        if not include_history:
            clauses.append("superseded_by='' AND state='active'")
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_overlay_objects WHERE " + " AND ".join(clauses)
                + " ORDER BY revision DESC LIMIT 1",
                params,
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackOverlayObject.from_row(row) if row is not None else None

    @classmethod
    def list_overlay_objects(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        include_history: bool = False,
        limit: int = 1000,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackOverlayObject]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["asset_id=?", "tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [asset_id, *owner]
        if object_type:
            clauses.append("object_type=?")
            params.append(str(object_type).strip().lower())
        if not include_history:
            clauses.append("superseded_by='' AND state='active'")
        params.append(max(1, min(int(limit or 1000), 10000)))
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_overlay_objects WHERE " + " AND ".join(clauses)
                + " ORDER BY object_type,updated_at,overlay_object_id LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackOverlayObject.from_row(row) for row in rows]

    @classmethod
    def overlay_object_counts(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, int]:
        owner = _owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT object_type,COUNT(*) AS total FROM research_pack_overlay_objects "
                "WHERE asset_id=? AND tenant_id=? AND user_id=? AND workspace_id=? "
                "AND superseded_by='' AND state='active' GROUP BY object_type ORDER BY object_type",
                (asset_id, *owner),
            ).fetchall()
        finally:
            connection.close()
        return {str(row["object_type"]): int(row["total"] or 0) for row in rows}

    @classmethod
    def create_overlay(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        parent_pack_ids: list[str] | None = None,
        source_digest: str = "",
        status: str = "active",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchPackOverlay, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in RESEARCH_PACK_OVERLAY_STATES:
            raise ValueError("unsupported ResearchPack overlay state")
        parents = _strings(parent_pack_ids, limit=100)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                "SELECT pack_id FROM research_pack_assets WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=?",
                (asset_id, *owner),
            ).fetchone()
            if asset is None:
                raise ValueError("ResearchPack asset does not belong to owner")
            effective_parents = parents or [str(asset["pack_id"])]
            digest = str(source_digest or "").strip() or sha256_text(
                canonical_json(
                    {"asset_id": asset_id, "parent_pack_ids": effective_parents}
                )
            )
            current = connection.execute(
                "SELECT * FROM research_pack_overlays WHERE asset_id=? AND status='active' LIMIT 1",
                (asset_id,),
            ).fetchone()
            if current is not None and normalized_status == "active":
                current_parents = json.loads(
                    str(current["parent_pack_ids_json"] or "[]")
                )
                if (
                    current_parents != effective_parents
                    or str(current["source_digest"] or "") != digest
                ):
                    raise ValueError(
                        "active ResearchPack overlay already exists with different content"
                    )
                connection.execute("COMMIT")
                return ResearchPackOverlay.from_row(current), False
            overlay_id = f"pack-overlay-{uuid.uuid4()}"
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json({"owner": owner, "asset_id": asset_id, "overlay_id": overlay_id})
            )
            connection.execute(
                "INSERT INTO research_pack_overlays(overlay_id,schema_version,asset_id,tenant_id,user_id,"
                "workspace_id,parent_pack_ids_json,status,source_digest,created_at,updated_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    overlay_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    asset_id,
                    *owner,
                    canonical_json(effective_parents),
                    normalized_status,
                    digest,
                    now,
                    now,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_overlays WHERE overlay_id=?",
                (overlay_id,),
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
            raise RuntimeError("ResearchPack overlay was not created")
        return ResearchPackOverlay.from_row(row), True

    @classmethod
    def put_mapping(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str,
        source_object_id: str,
        local_object_id: str,
        content_hash: str = "",
        metadata: dict[str, Any] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchPackObjectMapping, bool]:
        owner = _owner(tenant_id, user_id, workspace_id)
        normalized_type = str(object_type or "").strip().lower()[:80]
        normalized_source = str(source_object_id or "").strip()[:240]
        normalized_local = str(local_object_id or "").strip()[:240]
        if not normalized_type or not normalized_source or not normalized_local:
            raise ValueError("object_type, source_object_id and local_object_id are required")
        normalized_metadata = dict(metadata or {})
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                "SELECT 1 FROM research_pack_assets WHERE asset_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=?",
                (asset_id, *owner),
            ).fetchone()
            if asset is None:
                raise ValueError("ResearchPack asset does not belong to owner")
            current = connection.execute(
                "SELECT * FROM research_pack_object_mappings WHERE asset_id=? AND object_type=? "
                "AND source_object_id=? LIMIT 1",
                (asset_id, normalized_type, normalized_source),
            ).fetchone()
            if current is not None:
                current_metadata = json.loads(str(current["metadata_json"] or "{}"))
                if (
                    str(current["local_object_id"]) != normalized_local
                    or str(current["content_hash"] or "") != str(content_hash or "").strip()
                    or current_metadata != normalized_metadata
                ):
                    raise ValueError("ResearchPack object mapping already exists with different content")
                connection.execute("COMMIT")
                return ResearchPackObjectMapping.from_row(current), False
            mapping_id = f"pack-map-{uuid.uuid4()}"
            now = utc_now_iso()
            idempotency_key = sha256_text(
                canonical_json(
                    {
                        "owner": owner,
                        "asset_id": asset_id,
                        "type": normalized_type,
                        "source": normalized_source,
                    }
                )
            )
            connection.execute(
                "INSERT INTO research_pack_object_mappings(mapping_id,schema_version,asset_id,tenant_id,user_id,"
                "workspace_id,object_type,source_object_id,local_object_id,content_hash,metadata_json,created_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mapping_id,
                    RESEARCH_PACK_ASSET_SCHEMA_VERSION,
                    asset_id,
                    *owner,
                    normalized_type,
                    normalized_source,
                    normalized_local,
                    str(content_hash or "").strip()[:160],
                    canonical_json(normalized_metadata),
                    now,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_object_mappings WHERE mapping_id=?",
                (mapping_id,),
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
            raise RuntimeError("ResearchPack object mapping was not created")
        return ResearchPackObjectMapping.from_row(row), True

    @classmethod
    def list_overlays(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        status: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackOverlay]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["asset_id=?", "tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [asset_id, *owner]
        if status:
            normalized_status = str(status or "").strip().lower()
            if normalized_status not in RESEARCH_PACK_OVERLAY_STATES:
                raise ValueError("unsupported ResearchPack overlay state")
            clauses.append("status=?")
            params.append(normalized_status)
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_overlays WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,overlay_id",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackOverlay.from_row(row) for row in rows]

    @classmethod
    def list_mappings(
        cls,
        asset_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        object_type: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackObjectMapping]:
        owner = _owner(tenant_id, user_id, workspace_id)
        clauses = ["asset_id=?", "tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [asset_id, *owner]
        if object_type:
            clauses.append("object_type=?")
            params.append(str(object_type).strip().lower())
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_object_mappings WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,mapping_id",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackObjectMapping.from_row(row) for row in rows]


__all__ = ["ResearchPackAssetStore"]
