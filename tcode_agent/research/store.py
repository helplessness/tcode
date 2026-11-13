"""SQLite persistence for the append-only Research Journal."""

from __future__ import annotations

import sqlite3
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from .attribution_models import AttributionCorrectionInput, ResearchEventLink
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, canonical_json, sha256_text, utc_now_iso
from .schema import SCHEMA_VERSION as _SCHEMA_VERSION, initialize_schema
from ..sqlite_runtime import SQLiteConnection, connect as sqlite_connect
from ..workspace_lifecycle import assert_managed_workspace_creation_allowed


_LOCK = threading.RLock()
_INITIALIZATION_CACHE_LOCK = threading.RLock()
_INITIALIZED_DATABASES: dict[str, tuple[int, int, int, int]] = {}


class _ResearchSQLiteConnection(SQLiteConnection):
    """Track file identity after connections owned by this process close.

    SQLite may update the main database mtime while checkpointing WAL data.  By
    recording that expected change here, ``ensure_initialized`` can distinguish
    our ordinary writes from an externally replaced or downgraded database.
    """

    _research_path: Path | None = None

    def close(self) -> None:
        path = self._research_path
        # The shared connection base serializes SQLite's native teardown.
        super().close()
        if path is None:
            return
        cache_key = path.as_posix()
        # Never hold this cache lock while entering SQLite.  Keeping cache and
        # native lifecycle locks separate gives every caller the same lock
        # order and prevents startup reconciliation from deadlocking requests.
        with _INITIALIZATION_CACHE_LOCK:
            previous = _INITIALIZED_DATABASES.get(cache_key)
            if previous is None:
                return
            try:
                stat = path.stat()
                _INITIALIZED_DATABASES[cache_key] = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    previous[-1],
                )
            except OSError:
                _INITIALIZED_DATABASES.pop(cache_key, None)


class ResearchEventStore:
    """Workspace-local, owner-scoped ResearchEvent store."""

    @classmethod
    def db_path(
        cls,
        workdir: str | None = None,
        *,
        settings: ResearchSettings | None = None,
    ) -> Path:
        return (settings or load_research_settings()).database_path(workdir)

    @classmethod
    def _connect(
        cls,
        workdir: str | None = None,
        *,
        settings: ResearchSettings | None = None,
    ) -> sqlite3.Connection:
        resolved = settings or load_research_settings()
        path = resolved.database_path(workdir)
        if not path.exists():
            assert_managed_workspace_creation_allowed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        conn = sqlite_connect(
            path.as_posix(),
            timeout=max(0.1, resolved.database_busy_timeout_ms / 1000),
            isolation_level=None,
            check_same_thread=False,
            factory=_ResearchSQLiteConnection,
        )
        conn._research_path = path
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={resolved.database_busy_timeout_ms}")
        return conn

    @classmethod
    def delete_workspace_data(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        """Delete owner-scoped Research rows and managed external pack files."""

        owner = tuple(
            str(value or "").strip()
            for value in (tenant_id, user_id, workspace_id)
        )
        if any(not value for value in owner):
            raise ValueError("tenant_id, user_id and workspace_id are required")
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        conn = cls._connect(workdir, settings=resolved)
        deleted: dict[str, int] = {}
        removed_paths: list[str] = []
        try:
            export_rows = conn.execute(
                """
                SELECT archive_path FROM research_pack_exports
                WHERE tenant_id=? AND user_id=? AND workspace_id=?
                """,
                owner,
            ).fetchall()
            import_rows = conn.execute(
                """
                SELECT staged_path FROM research_pack_imports
                WHERE tenant_id=? AND user_id=? AND workspace_id=?
                """,
                owner,
            ).fetchall()
            managed_roots = {
                resolved.research_pack_root(workdir).resolve(strict=False),
                resolved.research_pack_import_path(workdir).resolve(strict=False),
            }
            for raw_path in [
                *[str(row["archive_path"] or "") for row in export_rows],
                *[str(row["staged_path"] or "") for row in import_rows],
            ]:
                if not raw_path:
                    continue
                candidate = Path(raw_path).expanduser().resolve(strict=False)
                allowed = False
                for root in managed_roots:
                    try:
                        candidate.relative_to(root)
                        allowed = candidate != root
                    except ValueError:
                        continue
                    if allowed:
                        break
                if not allowed or not candidate.exists():
                    continue
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
                removed_paths.append(candidate.as_posix())

            import_ids = [
                str(row["import_id"])
                for row in conn.execute(
                    """
                    SELECT import_id FROM research_pack_imports
                    WHERE tenant_id=? AND user_id=? AND workspace_id=?
                    """,
                    owner,
                ).fetchall()
            ]
            conn.execute("BEGIN IMMEDIATE")
            if import_ids:
                placeholders = ",".join("?" for _ in import_ids)
                cursor = conn.execute(
                    f"DELETE FROM research_pack_id_mappings WHERE import_id IN ({placeholders})",
                    tuple(import_ids),
                )
                deleted["research_pack_id_mappings"] = int(cursor.rowcount or 0)

            tables = (
                "research_snapshots",
                "research_pending_actions",
                "research_pack_overlay_objects",
                "research_pack_objects",
                "research_pack_object_mappings",
                "research_pack_overlays",
                "research_pack_scopes",
                "research_pack_assets",
                "research_experience_items",
                "research_object_scopes",
                "research_turn_routes",
                "research_context_audits",
                "research_activities",
                "research_pack_imports",
                "research_pack_exports",
                "research_knowledge_lint_runs",
                "research_knowledge_links",
                "research_derivation_runs",
                "research_resolutions",
                "research_conflicts",
                "research_change_events",
                "research_finding_versions",
                "research_findings",
                "research_checkpoints",
                "research_episodes",
                "research_topic_merge_suggestions",
                "research_topic_runs",
                "research_topic_operations",
                "research_project_deletions",
                "research_candidate_memberships",
                "research_topic_candidates",
                "research_attribution_corrections",
                "research_attribution_runs",
                "research_event_links",
                "research_projects",
                "research_events",
            )
            for table in tables:
                columns = {
                    str(row["name"] or "")
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not {"tenant_id", "user_id", "workspace_id"}.issubset(columns):
                    continue
                cursor = conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE tenant_id=? AND user_id=? AND workspace_id=?
                    """,
                    owner,
                )
                deleted[table] = int(cursor.rowcount or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "records": deleted,
            "removed_paths": sorted(set(removed_paths)),
        }

    @classmethod
    def ensure_initialized(
        cls,
        workdir: str | None = None,
        *,
        settings: ResearchSettings | None = None,
    ) -> Path:
        resolved = settings or load_research_settings()
        path = resolved.database_path(workdir)
        cache_key = path.as_posix()
        with _LOCK:
            try:
                stat = path.stat()
                identity = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    _SCHEMA_VERSION,
                )
            except OSError:
                identity = None
            with _INITIALIZATION_CACHE_LOCK:
                cached_identity = _INITIALIZED_DATABASES.get(cache_key)
            if identity is not None and cached_identity == identity:
                return path

            conn = cls._connect(workdir, settings=resolved)
            try:
                # WAL is a persistent database setting.  Reapplying it on every
                # read/write connection can acquire locks and stall the Web loop.
                conn.execute("PRAGMA journal_mode=WAL")
                initialize_schema(conn)
            finally:
                conn.close()
            try:
                path.chmod(0o600)
            except OSError:
                pass
            try:
                stat = path.stat()
                with _INITIALIZATION_CACHE_LOCK:
                    _INITIALIZED_DATABASES[cache_key] = (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_mtime_ns,
                        _SCHEMA_VERSION,
                    )
            except OSError:
                with _INITIALIZATION_CACHE_LOCK:
                    _INITIALIZED_DATABASES.pop(cache_key, None)
        return path

    @classmethod
    def append(
        cls,
        event: ResearchEvent,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
        return_status: bool = False,
    ) -> ResearchEvent | tuple[ResearchEvent, bool]:
        persisted, created = cls._append_with_status(
            event,
            workdir=workdir,
            settings=settings,
        )
        return (persisted, created) if return_status else persisted

    @classmethod
    def append_with_status(
        cls,
        event: ResearchEvent,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchEvent, bool]:
        """Append one event and report whether this call inserted a new row."""

        return cls._append_with_status(
            event,
            workdir=workdir,
            settings=settings,
        )

    @classmethod
    def _append_with_status(
        cls,
        event: ResearchEvent,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchEvent, bool]:

        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        values = event.to_dict()
        payload_json = canonical_json(values.pop("payload"))
        columns = [*values.keys(), "payload_json"]
        params = [*values.values(), payload_json]
        placeholders = ", ".join("?" for _ in columns)
        with _LOCK:
            conn = cls._connect(workdir, settings=resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    f"INSERT OR IGNORE INTO research_events({', '.join(columns)}) "
                    f"VALUES({placeholders})",
                    params,
                )
                created = int(cursor.rowcount or 0) > 0
                row = conn.execute(
                    "SELECT * FROM research_events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if created and resolved.routing_active:
                    conn.execute(
                        "INSERT OR IGNORE INTO research_routing_outbox(event_id) VALUES(?)",
                        (event.event_id,),
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        if row is None:
            raise RuntimeError("research event append did not return a persisted row")
        return ResearchEvent.from_row(row), created

    @classmethod
    def list_events_for_attribution(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        event_ids: list[str] | None = None,
        limit: int = 100,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEvent]:
        """Return owner-scoped events in stable order for deterministic replay."""

        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        normalized_ids = [
            str(item or "").strip() for item in (event_ids or []) if str(item or "").strip()
        ][:1000]
        if normalized_ids:
            clauses.append(
                "event_id IN (" + ", ".join("?" for _ in normalized_ids) + ")"
            )
            params.extend(normalized_ids)
        bounded_limit = max(1, min(int(limit or 100), 5000))
        params.append(bounded_limit)
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at DESC, ingested_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEvent.from_row(row) for row in reversed(rows)]

    @classmethod
    def append_attribution_link(
        cls,
        link: ResearchEventLink,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchEventLink, bool]:
        """Append one decision and supersede the previous active projection."""

        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        values = link.to_dict()
        reason_detail_json = canonical_json(values.pop("reason_detail"))
        values["shadow"] = 1 if values.get("shadow") else 0
        columns = [*values.keys(), "reason_detail_json"]
        params = [*values.values(), reason_detail_json]
        placeholders = ", ".join("?" for _ in columns)
        with _LOCK:
            conn = cls._connect(workdir, settings=resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                event_owner = conn.execute(
                    """
                    SELECT tenant_id, user_id, workspace_id
                    FROM research_events WHERE event_id = ?
                    """,
                    (link.event_id,),
                ).fetchone()
                if event_owner is None or (
                    str(event_owner["tenant_id"]) != link.tenant_id
                    or str(event_owner["user_id"]) != link.user_id
                    or str(event_owner["workspace_id"]) != link.workspace_id
                ):
                    raise ValueError("attribution event does not belong to owner")
                active = conn.execute(
                    """
                    SELECT * FROM research_event_links
                    WHERE event_id = ? AND tenant_id = ? AND user_id = ?
                      AND workspace_id = ? AND superseded_by = ''
                    """,
                    (
                        link.event_id,
                        link.tenant_id,
                        link.user_id,
                        link.workspace_id,
                    ),
                ).fetchone()
                if active is not None and str(active["semantic_fingerprint"]) == str(
                    link.semantic_fingerprint
                ):
                    conn.execute("COMMIT")
                    return ResearchEventLink.from_row(active), False
                if active is not None:
                    conn.execute(
                        "UPDATE research_event_links SET superseded_by = ? WHERE link_id = ?",
                        (link.link_id, str(active["link_id"])),
                    )
                conn.execute(
                    f"INSERT OR IGNORE INTO research_event_links({', '.join(columns)}) "
                    f"VALUES({placeholders})",
                    params,
                )
                row = conn.execute(
                    "SELECT * FROM research_event_links WHERE idempotency_key = ?",
                    (link.idempotency_key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("attribution link append did not persist a row")
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()
        return ResearchEventLink.from_row(row), True

    @classmethod
    def list_attribution_links(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        event_id: str = "",
        state: str = "",
        relevance: str = "",
        active_only: bool = True,
        limit: int = 200,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEventLink]:
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        for column, value in (
            ("event_id", event_id),
            ("state", state),
            ("relevance", relevance),
        ):
            normalized = str(value or "").strip().lower() if column != "event_id" else str(value or "").strip()
            if normalized:
                clauses.append(f"{column} = ?")
                params.append(normalized)
        if active_only:
            clauses.append("superseded_by = ''")
        params.append(max(1, min(int(limit or 200), 5000)))
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_event_links WHERE "
                + " AND ".join(clauses)
                + " ORDER BY decided_at ASC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEventLink.from_row(row) for row in rows]

    @classmethod
    def start_attribution_run(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        trigger_kind: str,
        decision_version: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> str:
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        run_id = f"attr-run-{uuid.uuid4()}"
        conn = cls._connect(workdir, settings=resolved)
        try:
            conn.execute(
                """
                INSERT INTO research_attribution_runs(
                    run_id, tenant_id, user_id, workspace_id, mode, trigger_kind,
                    decision_version, status, started_at
                ) VALUES(?, ?, ?, ?, 'shadow', ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    tenant_id,
                    user_id,
                    workspace_id,
                    str(trigger_kind or "manual")[:80],
                    str(decision_version or "attribution-shadow-1")[:80],
                    utc_now_iso(),
                ),
            )
        finally:
            conn.close()
        return run_id

    @classmethod
    def finish_attribution_run(
        cls,
        run_id: str,
        *,
        status: str,
        scanned_count: int,
        decision_count: int,
        changed_count: int,
        counts: dict[str, Any],
        errors: list[str],
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        resolved = settings or load_research_settings()
        conn = cls._connect(workdir, settings=resolved)
        try:
            conn.execute(
                """
                UPDATE research_attribution_runs
                SET status = ?, completed_at = ?, scanned_count = ?,
                    decision_count = ?, changed_count = ?, counts_json = ?,
                    errors_json = ?
                WHERE run_id = ?
                """,
                (
                    str(status or "completed")[:40],
                    utc_now_iso(),
                    max(0, int(scanned_count or 0)),
                    max(0, int(decision_count or 0)),
                    max(0, int(changed_count or 0)),
                    canonical_json(counts or {}),
                    canonical_json(list(errors or [])[:100]),
                    run_id,
                ),
            )
        finally:
            conn.close()

    @classmethod
    def list_attribution_runs(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        limit: int = 50,
        settings: ResearchSettings | None = None,
    ) -> list[dict[str, Any]]:
        import json

        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                """
                SELECT * FROM research_attribution_runs
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (
                    tenant_id,
                    user_id,
                    workspace_id,
                    max(1, min(int(limit or 50), 500)),
                ),
            ).fetchall()
        finally:
            conn.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("counts_json", "errors_json"):
                try:
                    item[key.removesuffix("_json")] = json.loads(str(item.pop(key) or "{}"))
                except Exception:
                    item[key.removesuffix("_json")] = {} if key == "counts_json" else []
            result.append(item)
        return result

    @classmethod
    def append_attribution_correction(
        cls,
        correction: AttributionCorrectionInput,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        action = correction.normalized_action()
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        occurred_at = utc_now_iso()
        correction_id = f"corr-{uuid.uuid4()}"
        idempotency_key = str(correction.idempotency_key or "").strip() or sha256_text(
            canonical_json(
                {
                    "event_id": correction.event_id,
                    "action": action,
                    "candidate_id": correction.candidate_id,
                    "research_id": correction.research_id,
                    "actor_id": correction.actor_id,
                    "occurred_at": occurred_at,
                }
            )
        )
        with _LOCK:
            conn = cls._connect(workdir, settings=resolved)
            try:
                conn.execute("BEGIN IMMEDIATE")
                event_owner = conn.execute(
                    "SELECT tenant_id, user_id, workspace_id FROM research_events WHERE event_id = ?",
                    (correction.event_id,),
                ).fetchone()
                if event_owner is None or (
                    str(event_owner["tenant_id"]) != correction.tenant_id
                    or str(event_owner["user_id"]) != correction.user_id
                    or str(event_owner["workspace_id"]) != correction.workspace_id
                ):
                    raise ValueError("correction event does not belong to owner")
                active = conn.execute(
                    "SELECT * FROM research_event_links WHERE event_id = ? AND superseded_by = ''",
                    (correction.event_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO research_attribution_corrections(
                        correction_id, tenant_id, user_id, workspace_id, event_id,
                        action, candidate_id, research_id, actor_id, reason,
                        previous_link_id, shadow_state, shadow_relevance,
                        shadow_candidate_id, shadow_research_id, occurred_at,
                        idempotency_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        correction_id,
                        correction.tenant_id,
                        correction.user_id,
                        correction.workspace_id,
                        correction.event_id,
                        action,
                        str(correction.candidate_id or "")[:160],
                        str(correction.research_id or "")[:160],
                        str(correction.actor_id or correction.user_id)[:160],
                        str(correction.reason or "")[:1000],
                        str(active["link_id"] if active is not None else ""),
                        str(active["state"] if active is not None else ""),
                        str(active["relevance"] if active is not None else ""),
                        str(active["candidate_id"] if active is not None else ""),
                        str(active["research_id"] if active is not None else ""),
                        occurred_at,
                        idempotency_key[:180],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM research_attribution_corrections WHERE idempotency_key = ?",
                    (idempotency_key[:180],),
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
        if row is None:
            raise RuntimeError("attribution correction append did not persist a row")
        return dict(row)

    @classmethod
    def latest_attribution_corrections(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        event_ids: list[str] | None = None,
        limit: int = 5000,
        settings: ResearchSettings | None = None,
    ) -> dict[str, dict[str, Any]]:
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        normalized_ids = [
            str(item or "").strip() for item in (event_ids or []) if str(item or "").strip()
        ][:1000]
        if normalized_ids:
            clauses.append("event_id IN (" + ", ".join("?" for _ in normalized_ids) + ")")
            params.extend(normalized_ids)
        params.append(max(1, min(int(limit or 5000), 10000)))
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_attribution_corrections WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at ASC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return {str(row["event_id"]): dict(row) for row in rows}

    @classmethod
    def list_events(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        session_id: str = "",
        turn_id: str = "",
        trace_id: str = "",
        event_type: str = "",
        limit: int = 200,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEvent]:
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        clauses = ["tenant_id = ?", "user_id = ?", "workspace_id = ?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        for column, value in (
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("trace_id", trace_id),
            ("event_type", event_type),
        ):
            normalized = str(value or "").strip()
            if normalized:
                clauses.append(f"{column} = ?")
                params.append(normalized)
        bounded_limit = max(1, min(int(limit or 200), 5000))
        params.append(bounded_limit)
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at ASC, ingested_at ASC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchEvent.from_row(row) for row in rows]

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
        path = cls.ensure_initialized(workdir, settings=resolved)
        conn = cls._connect(workdir, settings=resolved)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COUNT(DISTINCT session_id) AS session_count,
                       COUNT(DISTINCT trace_id) AS trace_count,
                       MAX(ingested_at) AS last_ingested_at
                FROM research_events
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return {
            "database_path": path.as_posix(),
            "schema_version": _SCHEMA_VERSION,
            "event_count": int(row["event_count"] or 0),
            "session_count": int(row["session_count"] or 0),
            "trace_count": int(row["trace_count"] or 0),
            "last_ingested_at": str(row["last_ingested_at"] or ""),
        }

    @classmethod
    def attribution_health(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM research_event_links
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                  AND superseded_by = ''
                GROUP BY state
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchall()
            summary = conn.execute(
                """
                SELECT COUNT(*) AS active_link_count,
                       SUM(CASE WHEN expires_at != '' AND expires_at < ?
                                AND state = 'pending' THEN 1 ELSE 0 END)
                           AS overdue_pending_count,
                       MAX(decided_at) AS last_decided_at
                FROM research_event_links
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                  AND superseded_by = ''
                """,
                (utc_now_iso(), tenant_id, user_id, workspace_id),
            ).fetchone()
            run = conn.execute(
                """
                SELECT status, started_at, completed_at
                FROM research_attribution_runs
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchone()
            correction_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM research_attribution_corrections
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        states = {str(row["state"]): int(row["count"] or 0) for row in rows}
        return {
            "active_link_count": int(summary["active_link_count"] or 0),
            "states": states,
            "overdue_pending_count": int(summary["overdue_pending_count"] or 0),
            "last_decided_at": str(summary["last_decided_at"] or ""),
            "correction_count": int(correction_count["count"] or 0),
            "last_run": (
                {
                    "status": str(run["status"] or ""),
                    "started_at": str(run["started_at"] or ""),
                    "completed_at": str(run["completed_at"] or ""),
                }
                if run is not None
                else None
            ),
        }

    @classmethod
    def attribution_evaluation(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        """Compare pre-correction shadow output with user-labelled corrections."""

        resolved = settings or load_research_settings()
        cls.ensure_initialized(workdir, settings=resolved)
        conn = cls._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                """
                SELECT action, candidate_id, research_id, shadow_state,
                       shadow_relevance, shadow_candidate_id, shadow_research_id
                FROM research_attribution_corrections
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                  AND action IN ('include', 'exclude') AND shadow_state != ''
                ORDER BY occurred_at ASC
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchall()
        finally:
            conn.close()
        labelled = len(rows)
        correct = 0
        false_includes = 0
        false_excludes = 0
        predicted_includes = 0
        correct_includes = 0
        for row in rows:
            action = str(row["action"] or "")
            shadow_state = str(row["shadow_state"] or "")
            shadow_relevance = str(row["shadow_relevance"] or "")
            expected_include = action == "include"
            predicted_include = shadow_state == "included" or (
                shadow_state == "pending" and shadow_relevance == "research"
            )
            if predicted_include:
                predicted_includes += 1
            target_matches = True
            if expected_include and (row["candidate_id"] or row["research_id"]):
                target_matches = (
                    str(row["candidate_id"] or "")
                    == str(row["shadow_candidate_id"] or "")
                    and str(row["research_id"] or "")
                    == str(row["shadow_research_id"] or "")
                )
            if predicted_include == expected_include and target_matches:
                correct += 1
                if predicted_include:
                    correct_includes += 1
            elif predicted_include and not expected_include:
                false_includes += 1
            elif expected_include and not predicted_include:
                false_excludes += 1
        return {
            "labelled_count": labelled,
            "correct_count": correct,
            "accuracy": (correct / labelled) if labelled else None,
            "auto_include_precision": (
                correct_includes / predicted_includes if predicted_includes else None
            ),
            "false_include_count": false_includes,
            "false_exclude_count": false_excludes,
            "ready_for_threshold_calibration": labelled >= 100,
        }
