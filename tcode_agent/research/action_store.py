"""SQLite store for short-lived natural-language research action tickets."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .action_models import (
    RESEARCH_PENDING_ACTION_SCHEMA_VERSION,
    RESEARCH_PENDING_ACTION_STATUSES,
    ResearchPendingAction,
)
from .config import ResearchSettings, load_research_settings
from .models import canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore


def _expires_at(minutes: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes or 1)))
    return value.isoformat().replace("+00:00", "Z")


class ResearchActionStore:
    """Persist one active confirmation target per owner-scoped chat session."""

    @staticmethod
    def _owner(tenant_id: str, user_id: str, workspace_id: str) -> tuple[str, str, str]:
        owner = tuple(
            str(value or "").strip()
            for value in (tenant_id, user_id, workspace_id)
        )
        if any(not value for value in owner):
            raise ValueError("research action requires complete owner context")
        return owner  # type: ignore[return-value]

    @classmethod
    def create_pending(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        proposal_message_id: str,
        kind: str,
        target_type: str,
        target_id: str,
        target_digest: str,
        payload: dict[str, Any],
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPendingAction:
        owner = cls._owner(tenant_id, user_id, workspace_id)
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            raise ValueError("research action requires a session_id")
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        now = utc_now_iso()
        expires = _expires_at(resolved.research_action_ttl_minutes)
        normalized_payload = dict(payload or {})
        idempotency_key = sha256_text(
            canonical_json(
                {
                    "owner": owner,
                    "session_id": normalized_session,
                    "proposal_message_id": str(proposal_message_id or "").strip(),
                    "kind": str(kind or "").strip(),
                    "target_type": str(target_type or "").strip(),
                    "target_id": str(target_id or "").strip(),
                    "target_digest": str(target_digest or "").strip(),
                }
            )
        )
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE research_pending_actions SET status='expired',updated_at=? "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=? AND session_id=? "
                "AND status='pending' AND expires_at<=?",
                (now, *owner, normalized_session, now),
            )
            existing = conn.execute(
                "SELECT * FROM research_pending_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None and str(existing["status"] or "") == "pending":
                conn.commit()
                return ResearchPendingAction.from_row(existing)
            if existing is not None:
                idempotency_key = sha256_text(
                    canonical_json({"previous": idempotency_key, "reissued_at": now})
                )
            conn.execute(
                "UPDATE research_pending_actions SET status='superseded',updated_at=? "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=? AND session_id=? "
                "AND status='pending'",
                (now, *owner, normalized_session),
            )
            action_id = f"research-action-{uuid.uuid4()}"
            conn.execute(
                """
                INSERT INTO research_pending_actions(
                    action_id,schema_version,tenant_id,user_id,workspace_id,
                    session_id,proposal_message_id,kind,status,target_type,target_id,
                    target_digest,payload_json,result_json,expires_at,
                    decision_message_id,completed_at,created_at,updated_at,idempotency_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    action_id,
                    RESEARCH_PENDING_ACTION_SCHEMA_VERSION,
                    *owner,
                    normalized_session,
                    str(proposal_message_id or "").strip(),
                    str(kind or "").strip(),
                    "pending",
                    str(target_type or "").strip(),
                    str(target_id or "").strip(),
                    str(target_digest or "").strip(),
                    canonical_json(normalized_payload),
                    "{}",
                    expires,
                    "",
                    "",
                    now,
                    now,
                    idempotency_key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_pending_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if row is None:  # pragma: no cover - guarded by the transaction
            raise RuntimeError("research action ticket was not persisted")
        return ResearchPendingAction.from_row(row)

    @classmethod
    def get(
        cls,
        action_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPendingAction | None:
        owner = cls._owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        now = utc_now_iso()
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE research_pending_actions SET status='expired',updated_at=? "
                "WHERE action_id=? AND tenant_id=? AND user_id=? AND workspace_id=? "
                "AND session_id=? AND status='pending' AND expires_at<=?",
                (
                    now,
                    str(action_id or "").strip(),
                    *owner,
                    str(session_id or "").strip(),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_pending_actions WHERE action_id=? "
                "AND tenant_id=? AND user_id=? AND workspace_id=? AND session_id=?",
                (str(action_id or "").strip(), *owner, str(session_id or "").strip()),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return ResearchPendingAction.from_row(row) if row is not None else None

    @classmethod
    def claim_pending(
        cls,
        action_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        decision_message_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPendingAction:
        owner = cls._owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        now = utc_now_iso()
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE research_pending_actions SET status='executing',decision_message_id=?,"
                "updated_at=? WHERE action_id=? AND tenant_id=? AND user_id=? AND workspace_id=? "
                "AND session_id=? AND status='pending' AND expires_at>?",
                (
                    str(decision_message_id or "").strip(),
                    now,
                    str(action_id or "").strip(),
                    *owner,
                    str(session_id or "").strip(),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_pending_actions WHERE action_id=? AND tenant_id=? "
                "AND user_id=? AND workspace_id=? AND session_id=?",
                (str(action_id or "").strip(), *owner, str(session_id or "").strip()),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if row is None:
            raise ValueError("research action was not found in the current session")
        record = ResearchPendingAction.from_row(row)
        if cursor.rowcount != 1:
            raise ValueError(
                f"该研究操作当前状态为 {record.status}，不能重复或并发执行。"
            )
        return record

    @classmethod
    def latest_pending(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPendingAction | None:
        owner = cls._owner(tenant_id, user_id, workspace_id)
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        now = utc_now_iso()
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE research_pending_actions SET status='expired',updated_at=? "
                "WHERE tenant_id=? AND user_id=? AND workspace_id=? AND session_id=? "
                "AND status='pending' AND expires_at<=?",
                (now, *owner, str(session_id or "").strip(), now),
            )
            row = conn.execute(
                "SELECT * FROM research_pending_actions WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND session_id=? AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (*owner, str(session_id or "").strip()),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return ResearchPendingAction.from_row(row) if row is not None else None

    @classmethod
    def update_status(
        cls,
        action_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        status: str,
        decision_message_id: str = "",
        result: dict[str, Any] | None = None,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchPendingAction:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in RESEARCH_PENDING_ACTION_STATUSES:
            raise ValueError(f"unsupported research action status: {normalized_status}")
        current = cls.get(
            action_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            workdir=workdir,
            settings=settings,
        )
        if current is None:
            raise ValueError("research action was not found in the current session")
        resolved = settings or load_research_settings()
        now = utc_now_iso()
        completed_at = now if normalized_status in {"completed", "cancelled", "stale", "failed"} else ""
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE research_pending_actions SET status=?,result_json=?,decision_message_id=?,"
                "completed_at=?,updated_at=? WHERE action_id=? AND tenant_id=? AND user_id=? "
                "AND workspace_id=? AND session_id=?",
                (
                    normalized_status,
                    canonical_json(dict(result or {})),
                    str(decision_message_id or "").strip(),
                    completed_at,
                    now,
                    current.action_id,
                    current.tenant_id,
                    current.user_id,
                    current.workspace_id,
                    current.session_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM research_pending_actions WHERE action_id=?",
                (current.action_id,),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if row is None:  # pragma: no cover
            raise RuntimeError("research action status update was lost")
        return ResearchPendingAction.from_row(row)


__all__ = ["ResearchActionStore"]
