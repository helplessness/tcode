"""Owner-scoped ResearchPack inspection, import audit, and ID provenance."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import canonical_json, sha256_text, utc_now_iso
from .pack_models import ResearchPackImport
from .store import ResearchEventStore


def _expired(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except Exception:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


class ResearchPackImportStore:
    @classmethod
    def _connect(
        cls,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> sqlite3.Connection:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        return ResearchEventStore._connect(workdir, settings=settings)

    @classmethod
    def create_inspection(
        cls,
        *,
        pack_id: str,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        schema_version: str,
        archive_name: str,
        staged_path: str,
        archive_sha256: str,
        archive_size: int,
        source_manifest: dict[str, Any],
        inspection_report: dict[str, Any],
        compatibility_report: dict[str, Any],
        expires_at: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackImport:
        resolved = settings or load_research_settings()
        now = utc_now_iso()
        import_id = f"import-{uuid.uuid4().hex}"
        inspection_id = f"inspection-{uuid.uuid4().hex}"
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "inspection_id": inspection_id,
                    "owner": [tenant_id, user_id, workspace_id],
                    "archive_sha256": archive_sha256,
                }
            )
        )
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute(
                "INSERT INTO research_pack_imports("
                "import_id,inspection_id,pack_id,tenant_id,user_id,workspace_id,"
                "schema_version,status,archive_name,staged_path,archive_sha256,"
                "archive_size,source_manifest_json,inspection_report_json,"
                "compatibility_report_json,created_at,inspected_at,expires_at,"
                "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    import_id,
                    inspection_id,
                    pack_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    schema_version,
                    "inspected",
                    Path(archive_name).name,
                    staged_path,
                    archive_sha256,
                    max(0, int(archive_size or 0)),
                    canonical_json(source_manifest),
                    canonical_json(inspection_report),
                    canonical_json(compatibility_report),
                    now,
                    now,
                    expires_at,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_pack_imports WHERE import_id=?",
                (import_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("ResearchPack inspection audit was not created")
        return ResearchPackImport.from_row(row)

    @classmethod
    def get(
        cls,
        inspection_or_import_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackImport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_imports WHERE "
                "(inspection_id=? OR import_id=?) AND tenant_id=? AND user_id=? "
                "AND workspace_id=?",
                (
                    inspection_or_import_id,
                    inspection_or_import_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                ),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackImport.from_row(row) if row is not None else None

    @classmethod
    def list_imports(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
        hidden: bool = False,
        offset: int = 0,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackImport]:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_imports WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND (hidden_at!='')=? ORDER BY created_at DESC,import_id DESC LIMIT ? OFFSET ?",
                (
                    tenant_id,
                    user_id,
                    workspace_id,
                    int(hidden),
                    max(1, min(int(limit or 50), 201)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackImport.from_row(row) for row in rows]

    @classmethod
    def set_hidden(cls, import_id: str, *, tenant_id: str, user_id: str, workspace_id: str,
                   hidden: bool, workdir: str | None = None, settings: ResearchSettings | None = None) -> bool:
        connection = cls._connect(workdir, settings or load_research_settings())
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT status FROM research_pack_imports WHERE import_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
                    (import_id, tenant_id, user_id, workspace_id)).fetchone()
                if row is None:
                    return False
                if row['status'] == 'importing':
                    raise ValueError('正在导入，完成后才能删除记录')
                connection.execute("UPDATE research_pack_imports SET hidden_at=? WHERE import_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
                    (utc_now_iso() if hidden else '', import_id, tenant_id, user_id, workspace_id))
            return True
        finally:
            connection.close()

    @classmethod
    def completed_for_pack(
        cls,
        pack_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackImport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_imports WHERE pack_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=? AND status='completed' "
                "ORDER BY imported_at DESC LIMIT 1",
                (pack_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackImport.from_row(row) if row is not None else None

    @classmethod
    def completed_for_research(
        cls,
        research_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPackImport | None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            row = connection.execute(
                "SELECT * FROM research_pack_imports WHERE local_research_id=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? AND status='completed' "
                "ORDER BY imported_at DESC LIMIT 1",
                (research_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        return ResearchPackImport.from_row(row) if row is not None else None

    @classmethod
    def change_mapping_metadata(
        cls,
        local_change_ids: list[str],
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not local_change_ids:
            return {}
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            result: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(local_change_ids), 500):
                batch = local_change_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT local_object_id,source_object_id,metadata_json FROM "
                    "research_pack_id_mappings WHERE object_type='change' AND "
                    f"local_object_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    try:
                        metadata = json.loads(str(row["metadata_json"] or "{}"))
                    except Exception:
                        metadata = {}
                    result[str(row["local_object_id"])] = {
                        "source_object_id": str(row["source_object_id"]),
                        **(metadata if isinstance(metadata, dict) else {}),
                    }
            return result
        finally:
            connection.close()

    @classmethod
    def fail(
        cls,
        import_id: str,
        error: str,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute(
                "UPDATE research_pack_imports SET status='failed',asset_id='',local_research_id='',"
                "staged_path='',error=? WHERE import_id=?",
                (str(error or "")[:4000], import_id),
            )
        finally:
            connection.close()

    @classmethod
    def list_recoverable(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchPackImport]:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            rows = connection.execute(
                "SELECT * FROM research_pack_imports WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND status IN ('inspected','importing') "
                "ORDER BY created_at ASC",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
        finally:
            connection.close()
        return [ResearchPackImport.from_row(row) for row in rows]

    @classmethod
    def mark_recovered(
        cls,
        import_id: str,
        *,
        status: str,
        error: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        resolved = settings or load_research_settings()
        connection = cls._connect(workdir, resolved)
        try:
            connection.execute(
                "UPDATE research_pack_imports SET status=?,staged_path='',error=? "
                "WHERE import_id=? AND status IN ('inspected','importing')",
                (str(status or "failed")[:40], str(error or "")[:4000], import_id),
            )
        finally:
            connection.close()

    @staticmethod
    def is_expired(item: ResearchPackImport) -> bool:
        return item.status == "inspected" and _expired(item.expires_at)

    @staticmethod
    def staged_file(
        item: ResearchPackImport,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> Path | None:
        if item.status not in {"inspected", "importing"} or not item.staged_path:
            return None
        resolved = settings or load_research_settings()
        root = resolved.research_pack_import_path(workdir).resolve(strict=False)
        raw = Path(item.staged_path).expanduser()
        if raw.is_symlink():
            return None
        path = raw.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return None
        return path if path.is_file() else None
