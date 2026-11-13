"""Owner-scoped ResearchPack export audit and archive lookup."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import canonical_json, utc_now_iso
from .pack_models import (
    RESEARCH_PACK_GENERATOR_VERSION,
    RESEARCH_PACK_SCHEMA_VERSION,
    ResearchPackExport,
)
from .store import ResearchEventStore


class ResearchPackStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def latest_completed(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE research_id=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? "
                "AND status='completed' ORDER BY completed_at DESC, pack_id DESC LIMIT 1",
                (research_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackExport.from_row(row) if row is not None else None

    @classmethod
    def latest_completed_scope(
        cls,
        scope: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport | None:
        resolved = settings or load_research_settings()
        normalized_scope = canonical_json(scope)
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE scope_json=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? "
                "AND status='completed' ORDER BY completed_at DESC,pack_id DESC LIMIT 1",
                (normalized_scope, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackExport.from_row(row) if row is not None else None

    @classmethod
    def get_by_idempotency_key(
        cls,
        idempotency_key: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE idempotency_key=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=?",
                (idempotency_key, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackExport.from_row(row) if row is not None else None

    @classmethod
    def start_export(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        privacy_profile: str,
        attribution_mode: str,
        source_digest: str,
        idempotency_key: str,
        scope: dict[str, Any] | None = None,
        source_research_ids: list[str] | None = None,
        schema_version: str = RESEARCH_PACK_SCHEMA_VERSION,
        generator_version: str = RESEARCH_PACK_GENERATOR_VERSION,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport:
        resolved = settings or load_research_settings()
        now = utc_now_iso()
        pack_id = f"pack-{uuid.uuid4().hex}"
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            normalized_scope = dict(scope or {})
            scope_mode = str(normalized_scope.get("mode") or "project").strip()
            normalized_research_ids = list(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in (source_research_ids or ([research_id] if research_id else []))
                    if str(item or "").strip()
                )
            )[:100]
            if scope_mode == "project":
                continuation_asset_id = str(
                    normalized_scope.get("continuation_asset_id") or ""
                ).strip()
                if continuation_asset_id:
                    owner = connection.execute(
                        "SELECT tenant_id,user_id,workspace_id FROM research_pack_assets "
                        "WHERE asset_id=? AND status='active'",
                        (continuation_asset_id,),
                    ).fetchone()
                    if owner is None or tuple(owner) != (
                        tenant_id,
                        user_id,
                        workspace_id,
                    ):
                        raise ValueError("ResearchPack asset not found for owner")
                else:
                    owner = connection.execute(
                        "SELECT tenant_id,user_id,workspace_id FROM research_projects WHERE research_id=?",
                        (research_id,),
                    ).fetchone()
                    if owner is None or tuple(owner) != (
                        tenant_id,
                        user_id,
                        workspace_id,
                    ):
                        raise ValueError("research project not found for owner")
            elif scope_mode != "workspace_period":
                raise ValueError("unsupported ResearchPack scope mode")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO research_pack_exports("
                "pack_id,tenant_id,user_id,workspace_id,research_id,scope_mode,scope_json,"
                "started_at,ended_at,source_research_ids_json,schema_version,"
                "generator_version,status,privacy_profile,attribution_mode,source_digest,"
                "created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pack_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    research_id,
                    scope_mode,
                    canonical_json(normalized_scope),
                    str(normalized_scope.get("started_at") or ""),
                    str(normalized_scope.get("ended_at") or ""),
                    canonical_json(normalized_research_ids),
                    schema_version,
                    generator_version,
                    "building",
                    privacy_profile,
                    attribution_mode,
                    source_digest,
                    now,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if cursor.rowcount == 0 and row is not None:
                if str(row["status"]) in {"failed", "completed"}:
                    connection.execute(
                        "UPDATE research_pack_exports SET status='building',created_at=?,"
                        "completed_at='',error='' WHERE pack_id=?",
                        (now, str(row["pack_id"])),
                    )
                    row = connection.execute(
                        "SELECT * FROM research_pack_exports WHERE pack_id=?",
                        (str(row["pack_id"]),),
                    ).fetchone()
                elif str(row["status"]) == "building":
                    raise RuntimeError("ResearchPack export is already in progress")
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
            raise RuntimeError("ResearchPack export audit was not created")
        return ResearchPackExport.from_row(row)

    @classmethod
    def finish_export(
        cls,
        pack_id: str,
        *,
        manifest: dict[str, Any],
        privacy_report: dict[str, Any],
        compatibility_report: dict[str, Any],
        archive_name: str,
        archive_path: str,
        archive_sha256: str,
        archive_size: int,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute(
                "UPDATE research_pack_exports SET status='completed',manifest_json=?,"
                "privacy_report_json=?,compatibility_report_json=?,archive_name=?,"
                "archive_path=?,archive_sha256=?,archive_size=?,completed_at=?,error='' "
                "WHERE pack_id=?",
                (
                    canonical_json(manifest),
                    canonical_json(privacy_report),
                    canonical_json(compatibility_report),
                    archive_name,
                    archive_path,
                    archive_sha256,
                    max(0, int(archive_size or 0)),
                    utc_now_iso(),
                    pack_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE pack_id=?",
                (pack_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("ResearchPack export audit was not found")
        return ResearchPackExport.from_row(row)

    @classmethod
    def fail_export(
        cls,
        pack_id: str,
        error: str,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute(
                "UPDATE research_pack_exports SET status='failed',error=?,completed_at=? "
                "WHERE pack_id=?",
                (str(error or "")[:4000], utc_now_iso(), pack_id),
            )
        finally:
            connection.close()

    @classmethod
    def list_exports(
        cls,
        research_id: str = "",
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackExport]:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            clauses = ["tenant_id=?", "user_id=?", "workspace_id=?"]
            params: list[Any] = [tenant_id, user_id, workspace_id]
            if str(research_id or "").strip():
                clauses.append("research_id=?")
                params.append(str(research_id).strip())
            params.append(max(1, min(int(limit or 50), 200)))
            rows = connection.execute(
                "SELECT * FROM research_pack_exports WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackExport.from_row(row) for row in rows]

    @classmethod
    def get_export(
        cls,
        pack_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackExport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_exports WHERE pack_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=?",
                (pack_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackExport.from_row(row) if row is not None else None

    @staticmethod
    def archive_file(
        export: ResearchPackExport,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> Path | None:
        if export.status != "completed" or not export.archive_path:
            return None
        raw_path = Path(export.archive_path).expanduser()
        if raw_path.is_symlink():
            return None
        path = raw_path.resolve(strict=False)
        if path.name != f"{export.pack_id}.researchpack.zip" or path.name != export.archive_name:
            return None
        if workdir is not None or settings is not None:
            root = (settings or load_research_settings()).research_pack_root(workdir)
            try:
                path.relative_to(root)
            except ValueError:
                return None
        return path if path.is_file() else None
