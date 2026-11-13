"""Fail-open facade for writing ResearchEvents from runtime hooks."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import fields
from typing import Iterable

from ..sqlite_runtime import connect as sqlite_connect
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, ResearchEventInput, sanitize_value, utc_now_iso
from .store import ResearchEventStore


_DEAD_LETTER_LOCK = threading.RLock()
_ROUTING_RETRY_LOCK = threading.RLock()
_ROUTING_RETRY_WORKDIRS: dict[str, int] = {}


class ResearchEventSink:
    @classmethod
    def append(
        cls,
        event_input: ResearchEventInput,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchEvent | None:
        persisted, _created = cls.append_with_status(
            event_input,
            workdir=workdir,
            settings=settings,
        )
        return persisted

    @classmethod
    def append_with_status(
        cls,
        event_input: ResearchEventInput,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> tuple[ResearchEvent | None, bool]:
        """Append one event and avoid replaying projections for duplicates."""

        resolved = settings or load_research_settings()
        if not resolved.journal_active:
            return None, False
        try:
            event = event_input.build(resolved)
            persisted, created = ResearchEventStore.append(
                event,
                workdir=workdir,
                settings=resolved,
                return_status=True,
            )
            if (
                created
                and resolved.attribution_shadow_active
                and resolved.attribution_auto_dispatch
            ):
                try:
                    from .attribution import AttributionShadowDispatcher

                    AttributionShadowDispatcher.submit(
                        persisted,
                        workdir=workdir,
                        settings=resolved,
                    )
                except Exception:
                    pass
            if resolved.routing_active:
                cls._remember_routing_workdir(workdir)
                try:
                    cls.retry_pending_routing(
                        workdir=workdir, settings=resolved,
                        owner=(persisted.tenant_id, persisted.user_id, persisted.workspace_id),
                    )
                except Exception:
                    # The transaction already saved both the event and its
                    # outbox row. A retry/storage outage must not hide that fact.
                    import logging
                    logging.getLogger(__name__).exception("Research routing retry deferred")
            return persisted, created
        except Exception as exc:
            cls._write_dead_letter(
                event_input,
                exc,
                workdir=workdir,
                settings=resolved,
            )
            if not resolved.write_fail_open:
                raise
            return None, False

    @classmethod
    def _remember_routing_workdir(cls, workdir):
        root = os.path.abspath(workdir or os.getcwd())
        with _ROUTING_RETRY_LOCK:
            _ROUTING_RETRY_WORKDIRS[root] = _ROUTING_RETRY_WORKDIRS.get(root, 0) + 1
        return root

    @classmethod
    def discover_routing_workspaces(cls, workdirs, *, settings=None) -> int:
        """Restore the retry registry from durable outboxes without creating stores."""
        import sqlite3
        resolved = settings or load_research_settings()
        if not resolved.routing_active:
            return 0
        found = 0
        for root in dict.fromkeys(str(path) for path in workdirs if path):
            path = resolved.database_path(root)
            if not path.is_file():
                continue
            try:
                conn = sqlite_connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
                try:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_routing_outbox'").fetchone()
                    pending = exists and conn.execute("SELECT 1 FROM research_routing_outbox LIMIT 1").fetchone()
                finally:
                    conn.close()
                if pending:
                    cls._remember_routing_workdir(root)
                    found += 1
            except sqlite3.Error:
                # Retain this existing database for a later retry after a lock
                # or transient read failure, rather than silently forgetting it.
                cls._remember_routing_workdir(root)
        return found

    @classmethod
    def retry_routing_workspaces(cls, *, settings=None):
        """Bounded background pass over workspaces with durable pending work."""
        from pathlib import Path
        resolved = settings or load_research_settings()
        with _ROUTING_RETRY_LOCK:
            roots = list(_ROUTING_RETRY_WORKDIRS)[:100]
            # Rotate a bounded pass so persistent failures cannot starve later workspaces.
            for root in roots:
                _ROUTING_RETRY_WORKDIRS[root] = _ROUTING_RETRY_WORKDIRS.pop(root)
        for root in roots:
            try:
                if not Path(resolved.database_path(root)).is_file():
                    with _ROUTING_RETRY_LOCK:
                        _ROUTING_RETRY_WORKDIRS.pop(root, None)
                    continue
                cls.retry_pending_routing(workdir=root, settings=resolved)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Research routing retry failed for workspace")

    @classmethod
    def retry_pending_routing(cls, *, workdir=None, settings=None, owner=None, limit=20) -> int:
        """Retry durable projection work; duplicate Journal delivery is harmless."""
        resolved = settings or load_research_settings()
        if not resolved.routing_active:
            return 0
        root = cls._remember_routing_workdir(workdir)
        with _ROUTING_RETRY_LOCK:
            generation = _ROUTING_RETRY_WORKDIRS[root]
        from .routing_store import ResearchRoutingStore
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        completed = 0
        try:
            where = "WHERE e.tenant_id=? AND e.user_id=? AND e.workspace_id=?" if owner else ""
            rows = conn.execute(
                "SELECT e.* FROM research_routing_outbox o JOIN research_events e USING(event_id) "
                + where + " ORDER BY o.attempts, e.ingested_at LIMIT ?",
                (*tuple(owner or ()), max(1, min(limit, 100))),
            ).fetchall()
            for row in rows:
                event = ResearchEvent.from_row(row)
                try:
                    ResearchRoutingStore.inherit_event(event, workdir=workdir, settings=resolved)
                except Exception as exc:
                    conn.execute("UPDATE research_routing_outbox SET attempts=attempts+1, last_error=? WHERE event_id=?", (str(exc)[:1000], event.event_id))
                else:
                    conn.execute("DELETE FROM research_routing_outbox WHERE event_id=?", (event.event_id,))
                    completed += 1
            remaining = conn.execute("SELECT 1 FROM research_routing_outbox LIMIT 1").fetchone()
            if remaining is None:
                with _ROUTING_RETRY_LOCK:
                    if _ROUTING_RETRY_WORKDIRS.get(root) == generation:
                        _ROUTING_RETRY_WORKDIRS.pop(root, None)
        finally:
            conn.close()
        return completed

    @classmethod
    def append_many(
        cls,
        event_inputs: Iterable[ResearchEventInput],
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchEvent]:
        resolved = settings or load_research_settings()
        result: list[ResearchEvent] = []
        for event_input in event_inputs:
            event = cls.append(event_input, workdir=workdir, settings=resolved)
            if event is not None:
                result.append(event)
        return result

    @classmethod
    def _write_dead_letter(
        cls,
        event_input: ResearchEventInput,
        error: BaseException,
        *,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> None:
        try:
            path = settings.dead_letter_path(workdir)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = sanitize_value(
                {
                    "failed_at": utc_now_iso(),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "event_input": event_input.__dict__,
                },
                max_string_chars=settings.content_preview_chars,
                store_content_preview=settings.store_content_preview,
            )
            with _DEAD_LETTER_LOCK:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                    )
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            pass


class ResearchDeadLetterReplayer:
    """Replay only sanitized, owner-scoped facts; never execute stored content."""

    @classmethod
    def health(
        cls,
        *,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, int | bool]:
        resolved = settings or load_research_settings()
        path = resolved.dead_letter_path(workdir)
        if not path.exists():
            return {"exists": False, "line_count": 0, "size_bytes": 0}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            return {
                "exists": True,
                "line_count": len(lines),
                "size_bytes": int(path.stat().st_size),
            }
        except OSError:
            return {"exists": True, "line_count": -1, "size_bytes": -1}

    @classmethod
    def delete_workspace_data(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> int:
        """Remove dead-letter entries belonging to one exact workspace owner."""

        resolved = settings or load_research_settings()
        path = resolved.dead_letter_path(workdir)
        if not path.exists():
            return 0
        owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if any(not value for value in owner.values()):
            raise ValueError("tenant_id, user_id and workspace_id are required")
        removed = 0
        retained: list[str] = []
        with _DEAD_LETTER_LOCK:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(line)
                except Exception:
                    retained.append(line)
                    continue
                event_input = (
                    payload.get("event_input")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("event_input"), dict)
                    else {}
                )
                if all(
                    str(event_input.get(key) or "").strip() == value
                    for key, value in owner.items()
                ):
                    removed += 1
                else:
                    retained.append(line)
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            temporary.write_text(
                "".join(f"{line}\n" for line in retained),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(path)
        return removed

    @classmethod
    def replay(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        limit: int = 100,
        settings: ResearchSettings | None = None,
    ) -> dict[str, int]:
        resolved = settings or load_research_settings()
        path = resolved.dead_letter_path(workdir)
        report = {
            "scanned": 0,
            "replayed": 0,
            "retained": 0,
            "invalid": 0,
            "foreign_owner": 0,
            "failed": 0,
        }
        if not path.exists():
            return report
        allowed_fields = {item.name for item in fields(ResearchEventInput)}
        retained: list[str] = []
        bounded_limit = max(1, min(int(limit or 100), 5000))
        with _DEAD_LETTER_LOCK:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
            for raw_line in raw_lines:
                if report["scanned"] >= bounded_limit:
                    retained.append(raw_line)
                    continue
                report["scanned"] += 1
                try:
                    envelope = json.loads(raw_line)
                    raw_input = envelope.get("event_input")
                    if not isinstance(raw_input, dict):
                        raise ValueError("missing event_input")
                    values = {
                        key: value
                        for key, value in raw_input.items()
                        if key in allowed_fields
                    }
                    event_input = ResearchEventInput(**values)
                except Exception:
                    report["invalid"] += 1
                    retained.append(raw_line)
                    continue
                try:
                    built_event = event_input.build(resolved)
                except Exception:
                    report["invalid"] += 1
                    retained.append(raw_line)
                    continue
                owner = (
                    built_event.tenant_id,
                    built_event.user_id,
                    built_event.workspace_id,
                )
                if owner != (tenant_id, user_id, workspace_id):
                    report["foreign_owner"] += 1
                    retained.append(raw_line)
                    continue
                try:
                    event = ResearchEventStore.append(
                        built_event,
                        workdir=workdir,
                        settings=resolved,
                    )
                    report["replayed"] += 1
                    if resolved.attribution_shadow_active:
                        try:
                            from .attribution import AttributionShadowDispatcher

                            AttributionShadowDispatcher.submit(
                                event,
                                workdir=workdir,
                                settings=resolved,
                            )
                        except Exception:
                            pass
                except Exception:
                    report["failed"] += 1
                    retained.append(raw_line)
            report["retained"] = len(retained)
            temporary = path.with_name(f".{path.name}.replay.tmp")
            temporary.write_text(
                "".join(f"{line}\n" for line in retained),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return report
