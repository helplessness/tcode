"""SQLite authority for durable Guidance v2 state."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from ..session.store_sqlite import SessionSQLiteStore
from .guidance import (
    GuidanceClaimLost,
    GuidanceItem,
    GuidanceMode,
    GuidanceNotCancellable,
    GuidanceNotFound,
    GuidanceQueueFull,
    GuidanceSafePoint,
    GuidanceStaleTurn,
    GuidanceStatus,
    GuidanceSupersedeConflict,
    GuidanceVersionConflict,
    OwnerScope,
    TurnOwner,
)


_ACTIVE_STATUSES = (
    "classifying",
    "pending",
    "claimed",
    "deferred",
    "applying",
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        parsed = json.loads(str(value))
    except Exception:
        return default
    if isinstance(default, dict) and not isinstance(parsed, dict):
        return default
    if isinstance(default, list) and not isinstance(parsed, list):
        return default
    return parsed


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class GuidanceSQLiteStore:
    """Synchronous store. Async callers must use ``asyncio.to_thread``."""

    @classmethod
    @contextmanager
    def _transaction(cls, workdir: str) -> Iterator[sqlite3.Connection]:
        SessionSQLiteStore.ensure_initialized(workdir, force_migration=False)
        conn = SessionSQLiteStore._connect(workdir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _owner_tuple(owner: OwnerScope) -> tuple[str, str, str]:
        return (
            str(owner.tenant_id or "").strip(),
            str(owner.user_id or "").strip(),
            str(owner.workspace_id or "").strip(),
        )

    @classmethod
    def _item_from_row(cls, row: sqlite3.Row) -> GuidanceItem:
        return GuidanceItem(
            id=str(row["id"] or ""),
            session_id=str(row["session_id"] or ""),
            tenant_id=str(row["tenant_id"] or ""),
            user_id=str(row["user_id"] or ""),
            workspace_id=str(row["workspace_id"] or ""),
            client_request_id=str(row["client_request_id"] or ""),
            target_turn_id=str(row["target_turn_id"] or ""),
            sequence=int(row["sequence"] or 0),
            mode=GuidanceMode(str(row["mode"] or GuidanceMode.QUEUE.value)),
            status=GuidanceStatus(str(row["status"] or GuidanceStatus.FAILED.value)),
            priority=int(row["priority"] or 100),
            message=str(row["message"] or ""),
            file_refs=_json_value(row["file_refs_json"], []),
            output_format=_json_value(row["output_format_json"], None),
            outcome_contract=_json_value(row["outcome_contract_json"], None),
            contract_revision=_json_value(row["contract_revision_json"], None),
            apply_policy=str(row["apply_policy"] or "next_safe_point"),
            descendant_policy=str(row["descendant_policy"] or "preserve"),
            supersedes_guidance_id=str(row["supersedes_guidance_id"] or ""),
            received_phase=str(row["received_phase"] or ""),
            runtime_id=str(row["runtime_id"] or "tcode-native"),
            model_id=str(row["model_id"] or ""),
            harness_switch_version=int(row["harness_switch_version"] or 0),
            source_run_id=str(row["source_run_id"] or ""),
            claimed_safe_point=str(row["claimed_safe_point"] or ""),
            claim_owner=str(row["claim_owner"] or ""),
            claim_token=str(row["claim_token"] or ""),
            claim_lease_expires_at=(
                float(row["claim_lease_expires_at"])
                if row["claim_lease_expires_at"] is not None
                else None
            ),
            deferred_reason=str(row["deferred_reason"] or ""),
            effect=str(row["effect"] or ""),
            failure_code=str(row["failure_code"] or ""),
            failure_detail=str(row["failure_detail"] or ""),
            version=int(row["version"] or 1),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            claimed_at=(float(row["claimed_at"]) if row["claimed_at"] is not None else None),
            applied_at=(float(row["applied_at"]) if row["applied_at"] is not None else None),
        )

    @classmethod
    def _turn_owner_from_row(cls, row: sqlite3.Row) -> TurnOwner:
        return TurnOwner(
            session_id=str(row["session_id"] or ""),
            turn_id=str(row["turn_id"] or ""),
            owner_id=str(row["owner_id"] or ""),
            phase=str(row["phase"] or ""),
            stream_epoch=int(row["stream_epoch"] or 0),
            completion_fenced=bool(row["completion_fenced"]),
            lease_expires_at=float(row["lease_expires_at"] or 0),
            heartbeat_at=float(row["heartbeat_at"] or 0),
            version=int(row["version"] or 1),
            updated_at=float(row["updated_at"] or 0),
        )

    @classmethod
    def _session_row(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        owner: OwnerScope,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT id, tenant_id, user_id, workspace_id
            FROM sessions
            WHERE id = ? AND tenant_id = ? AND user_id = ? AND workspace_id = ?
            LIMIT 1
            """,
            (session_id, *cls._owner_tuple(owner)),
        ).fetchone()
        if row is None:
            raise GuidanceNotFound("session not found")
        return row

    @classmethod
    def _owned_item(
        cls,
        conn: sqlite3.Connection,
        guidance_id: str,
        owner: OwnerScope,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM guidance_items
            WHERE id = ? AND tenant_id = ? AND user_id = ? AND workspace_id = ?
            LIMIT 1
            """,
            (guidance_id, *cls._owner_tuple(owner)),
        ).fetchone()
        if row is None:
            raise GuidanceNotFound("guidance not found")
        return row

    @classmethod
    def _assert_version(cls, row: sqlite3.Row, expected_version: int) -> None:
        if int(row["version"] or 0) != int(expected_version):
            raise GuidanceVersionConflict("guidance version changed")

    @classmethod
    def _assert_claim(cls, row: sqlite3.Row, claim_token: str, now: float) -> None:
        if not claim_token or str(row["claim_token"] or "") != str(claim_token):
            raise GuidanceClaimLost("guidance claim token changed")
        lease = row["claim_lease_expires_at"]
        if lease is not None and float(lease) < now:
            raise GuidanceClaimLost("guidance claim expired")

    @classmethod
    def _outbox(
        cls,
        conn: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        event_type: str,
        status: Optional[str] = None,
    ) -> None:
        payload = {
            "guidance_id": str(row["id"] or ""),
            "session_id": str(row["session_id"] or ""),
            "target_turn_id": str(row["target_turn_id"] or ""),
            "status": str(status or row["status"] or ""),
            "sequence": int(row["sequence"] or 0),
            "version": int(row["version"] or 1),
            "mode": str(row["mode"] or ""),
            "runtime_id": str(row["runtime_id"] or "tcode-native"),
            "harness_switch_version": int(row["harness_switch_version"] or 0),
            "claimed_safe_point": str(row["claimed_safe_point"] or ""),
            "effect": str(row["effect"] or ""),
            "failure_code": str(row["failure_code"] or ""),
        }
        conn.execute(
            """
            INSERT INTO guidance_outbox(
                event_id, guidance_id, session_id, target_turn_id,
                event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                payload["guidance_id"],
                payload["session_id"],
                payload["target_turn_id"],
                event_type,
                _json_dump(payload),
                time.time(),
            ),
        )

    @classmethod
    def accept(
        cls,
        *,
        session_id: str,
        owner: OwnerScope,
        client_request_id: str,
        target_turn_id: str,
        mode: GuidanceMode,
        message: str,
        file_refs: list[dict[str, Any]],
        output_format: Optional[dict[str, Any]],
        outcome_contract: Optional[dict[str, Any]],
        contract_revision: Optional[dict[str, Any]] = None,
        apply_policy: str,
        descendant_policy: str,
        supersedes_guidance_id: str,
        received_phase: str,
        workdir: str,
        initial_effect: str = "",
        created_at: Optional[float] = None,
        runtime_id: str = "tcode-native",
        model_id: str = "",
        harness_switch_version: int = 0,
        source_run_id: str = "",
        allow_unbound_steer: bool = False,
    ) -> tuple[GuidanceItem, bool]:
        normalized_session_id = str(session_id or "").strip()
        normalized_request_id = str(client_request_id or "").strip()
        normalized_message = str(message or "").strip()
        if not normalized_session_id or not normalized_request_id or not normalized_message:
            raise ValueError("session_id, client_request_id and message are required")
        now = float(created_at or time.time())
        requested_mode = GuidanceMode(mode)
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, normalized_session_id, owner)
            duplicate = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE session_id = ? AND client_request_id = ?
                  AND tenant_id = ? AND user_id = ? AND workspace_id = ?
                LIMIT 1
                """,
                (
                    normalized_session_id,
                    normalized_request_id,
                    *cls._owner_tuple(owner),
                ),
            ).fetchone()
            if duplicate is not None:
                return cls._item_from_row(duplicate), True

            active_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM guidance_items
                    WHERE session_id = ? AND status IN (?, ?, ?, ?, ?)
                    """,
                    (normalized_session_id, *_ACTIVE_STATUSES),
                ).fetchone()["count"]
                or 0
            )
            workspace_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM guidance_items
                    WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                      AND status IN (?, ?, ?, ?, ?)
                    """,
                    (*cls._owner_tuple(owner), *_ACTIVE_STATUSES),
                ).fetchone()["count"]
                or 0
            )
            if active_count >= _bounded_int(
                "TCODE_GUIDANCE_QUEUE_MAX_PER_SESSION", 32, 1, 1024
            ) or workspace_count >= _bounded_int(
                "TCODE_GUIDANCE_QUEUE_MAX_GLOBAL", 512, 1, 100000
            ):
                raise GuidanceQueueFull("guidance queue full")

            accepted_mode = requested_mode
            bound_turn_id = str(target_turn_id or "").strip()
            effect = str(initial_effect or "").strip()
            if accepted_mode is GuidanceMode.STEER:
                owner_row = None
                if bound_turn_id:
                    owner_row = conn.execute(
                        """
                        SELECT * FROM guidance_turn_owners
                        WHERE session_id = ? AND turn_id = ?
                          AND completion_fenced = 0 AND lease_expires_at >= ?
                        LIMIT 1
                        """,
                        (normalized_session_id, bound_turn_id, time.time()),
                    ).fetchone()
                    if owner_row is None:
                        if allow_unbound_steer and str(source_run_id or "").strip():
                            bound_turn_id = str(source_run_id or "").strip()
                        else:
                            raise GuidanceStaleTurn("target turn is not active")
                else:
                    owner_row = conn.execute(
                        """
                        SELECT * FROM guidance_turn_owners
                        WHERE session_id = ? AND completion_fenced = 0
                          AND lease_expires_at >= ?
                        LIMIT 1
                        """,
                        (normalized_session_id, time.time()),
                    ).fetchone()
                    if owner_row is not None:
                        bound_turn_id = str(owner_row["turn_id"] or "")
                    elif allow_unbound_steer and str(source_run_id or "").strip():
                        bound_turn_id = str(source_run_id or "").strip()
                    else:
                        accepted_mode = GuidanceMode.QUEUE
                        bound_turn_id = ""
                        effect = "downgraded_no_active_turn"
            else:
                bound_turn_id = ""

            next_sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM guidance_items WHERE session_id = ?",
                    (normalized_session_id,),
                ).fetchone()["sequence"]
                or 1
            )

            supersedes_id = str(supersedes_guidance_id or "").strip()
            if supersedes_id:
                superseded = cls._owned_item(conn, supersedes_id, owner)
                if (
                    str(superseded["session_id"] or "") != normalized_session_id
                    or str(superseded["mode"] or "") != accepted_mode.value
                    or (
                        accepted_mode is GuidanceMode.STEER
                        and str(superseded["target_turn_id"] or "") != bound_turn_id
                    )
                    or str(superseded["status"] or "")
                    not in {"classifying", "pending", "deferred"}
                ):
                    raise GuidanceSupersedeConflict("guidance cannot be superseded")
                conn.execute(
                    """
                    UPDATE guidance_items
                    SET status = 'superseded', claim_owner = '', claim_token = '',
                        claim_lease_expires_at = NULL, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (time.time(), supersedes_id),
                )
                superseded = cls._owned_item(conn, supersedes_id, owner)
                cls._outbox(conn, row=superseded, event_type="guidance.superseded")

            guidance_id = str(uuid.uuid4())
            initial_status = (
                GuidanceStatus.CLASSIFYING.value
                if accepted_mode is GuidanceMode.STEER
                else GuidanceStatus.PENDING.value
            )
            conn.execute(
                """
                INSERT INTO guidance_items(
                    id, session_id, tenant_id, user_id, workspace_id,
                    client_request_id, target_turn_id, sequence, mode, status,
                    message, file_refs_json, output_format_json,
                    outcome_contract_json, contract_revision_json, apply_policy,
                    descendant_policy, supersedes_guidance_id, received_phase,
                    runtime_id, model_id, harness_switch_version, source_run_id,
                    effect, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guidance_id,
                    normalized_session_id,
                    *cls._owner_tuple(owner),
                    normalized_request_id,
                    bound_turn_id,
                    next_sequence,
                    accepted_mode.value,
                    initial_status,
                    normalized_message,
                    _json_dump(list(file_refs or [])),
                    _json_dump(output_format) if output_format is not None else "null",
                    _json_dump(outcome_contract) if outcome_contract is not None else "null",
                    _json_dump(contract_revision) if contract_revision is not None else "null",
                    str(apply_policy or "next_safe_point"),
                    str(descendant_policy or "preserve"),
                    supersedes_id,
                    str(received_phase or ""),
                    str(runtime_id or "tcode-native").strip() or "tcode-native",
                    str(model_id or "").strip(),
                    max(0, int(harness_switch_version or 0)),
                    str(source_run_id or "").strip(),
                    effect,
                    now,
                    time.time(),
                ),
            )
            inserted = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(conn, row=inserted, event_type="guidance.accepted")
            return cls._item_from_row(inserted), False

    @classmethod
    def claim_next_classification(
        cls,
        *,
        owner_id: str,
        lease_seconds: float,
        owner: OwnerScope,
        workdir: str,
    ) -> Optional[GuidanceItem]:
        now = time.time()
        with cls._transaction(workdir) as conn:
            row = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                  AND status = 'classifying'
                  AND (claim_token = '' OR claim_lease_expires_at IS NULL OR claim_lease_expires_at < ?)
                ORDER BY created_at, sequence LIMIT 1
                """,
                (*cls._owner_tuple(owner), now),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            conn.execute(
                """
                UPDATE guidance_items SET claim_owner = ?, claim_token = ?,
                    claim_lease_expires_at = ?, claimed_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    owner_id,
                    token,
                    now + max(1.0, float(lease_seconds)),
                    now,
                    now,
                    str(row["id"]),
                    int(row["version"]),
                ),
            )
            claimed = cls._owned_item(conn, str(row["id"]), owner)
            return cls._item_from_row(claimed)

    @classmethod
    def complete_classification(
        cls,
        *,
        guidance_id: str,
        claim_token: str,
        expected_version: int,
        contract_revision: Optional[dict[str, Any]],
        outcome_contract: Optional[dict[str, Any]],
        effect: str,
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        now = time.time()
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, guidance_id, owner)
            cls._assert_version(row, expected_version)
            cls._assert_claim(row, claim_token, now)
            if str(row["status"]) != GuidanceStatus.CLASSIFYING.value:
                raise GuidanceVersionConflict("guidance is no longer classifying")
            revision = dict(contract_revision or {})
            is_cancel = (
                str(revision.get("classification") or "").upper() == "CANCEL"
                and not bool(revision.get("needs_user_confirmation"))
            )
            next_status = "applying" if is_cancel else "pending"
            clear_claim = not is_cancel
            conn.execute(
                """
                UPDATE guidance_items SET status = ?, contract_revision_json = ?,
                    outcome_contract_json = ?, effect = ?, claim_owner = ?,
                    claim_token = ?, claim_lease_expires_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    next_status,
                    _json_dump(revision) if revision else "null",
                    _json_dump(outcome_contract) if outcome_contract is not None else "null",
                    str(effect or ""),
                    "" if clear_claim else str(row["claim_owner"] or ""),
                    "" if clear_claim else claim_token,
                    None if clear_claim else row["claim_lease_expires_at"],
                    now,
                    guidance_id,
                ),
            )
            updated = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(
                conn,
                row=updated,
                event_type="guidance.applying" if is_cancel else "guidance.pending",
            )
            return cls._item_from_row(updated)

    @classmethod
    def fail_classification(
        cls,
        *,
        guidance_id: str,
        claim_token: str,
        expected_version: int,
        failure_code: str,
        failure_detail: str,
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        now = time.time()
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, guidance_id, owner)
            cls._assert_version(row, expected_version)
            cls._assert_claim(row, claim_token, now)
            conn.execute(
                """
                UPDATE guidance_items SET status = 'failed', failure_code = ?,
                    failure_detail = ?, claim_owner = '', claim_token = '',
                    claim_lease_expires_at = NULL, updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'classifying'
                """,
                (str(failure_code)[:128], str(failure_detail)[:1000], now, guidance_id),
            )
            updated = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(conn, row=updated, event_type="guidance.failed")
            return cls._item_from_row(updated)

    @classmethod
    def claim_for_turn(
        cls,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        safe_point: GuidanceSafePoint,
        owner: OwnerScope,
        workdir: str,
        max_items: int = 8,
        lease_seconds: float = 30,
    ) -> list[GuidanceItem]:
        now = time.time()
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            turn_owner = conn.execute(
                """
                SELECT * FROM guidance_turn_owners
                WHERE session_id = ? AND turn_id = ? AND owner_id = ?
                  AND completion_fenced = 0 AND lease_expires_at >= ?
                LIMIT 1
                """,
                (session_id, turn_id, owner_id, now),
            ).fetchone()
            if turn_owner is None:
                raise GuidanceStaleTurn("turn ownership was lost")
            candidates = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE session_id = ? AND target_turn_id = ? AND mode = 'steer'
                  AND status IN ('classifying', 'pending')
                ORDER BY sequence
                """,
                (session_id, turn_id),
            ).fetchall()
            claimed: list[GuidanceItem] = []
            for row in candidates:
                if str(row["status"]) == "classifying":
                    break
                if (
                    str(row["apply_policy"] or "next_safe_point")
                    == "after_current_tool"
                    and GuidanceSafePoint(safe_point)
                    not in {
                        GuidanceSafePoint.AFTER_TOOL_RESULT,
                        GuidanceSafePoint.BEFORE_NEXT_TOOL,
                        GuidanceSafePoint.BEFORE_FINALIZE,
                    }
                ):
                    # Preserve sequence ordering: a later item may supersede or
                    # depend on this one, so do not leapfrog it.
                    break
                if len(claimed) >= max(1, min(int(max_items), 16)):
                    break
                token = str(uuid.uuid4())
                conn.execute(
                    """
                    UPDATE guidance_items SET status = 'claimed', claim_owner = ?,
                        claim_token = ?, claim_lease_expires_at = ?, claimed_at = ?,
                        claimed_safe_point = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND status = 'pending' AND version = ?
                    """,
                    (
                        owner_id,
                        token,
                        now + max(10.0, float(lease_seconds)),
                        now,
                        GuidanceSafePoint(safe_point).value,
                        now,
                        str(row["id"]),
                        int(row["version"]),
                    ),
                )
                updated = cls._owned_item(conn, str(row["id"]), owner)
                cls._outbox(conn, row=updated, event_type="guidance.claimed")
                claimed.append(cls._item_from_row(updated))
            return claimed

    @classmethod
    def claim_next_queue(
        cls,
        *,
        session_id: str,
        new_turn_id: str,
        owner_id: str,
        owner: OwnerScope,
        workdir: str,
        lease_seconds: float = 30,
    ) -> Optional[GuidanceItem]:
        now = time.time()
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            active_owner = conn.execute(
                """
                SELECT 1 FROM guidance_turn_owners
                WHERE session_id = ? AND completion_fenced = 0 AND lease_expires_at >= ?
                """,
                (session_id, now),
            ).fetchone()
            if active_owner is not None:
                return None
            row = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE session_id = ? AND mode = 'queue' AND status = 'pending'
                ORDER BY sequence LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            conn.execute(
                """
                UPDATE guidance_items SET status = 'claimed', target_turn_id = ?,
                    claim_owner = ?, claim_token = ?, claim_lease_expires_at = ?,
                    claimed_at = ?, claimed_safe_point = 'after_recovery',
                    updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'pending' AND version = ?
                """,
                (
                    new_turn_id,
                    owner_id,
                    token,
                    now + max(10.0, float(lease_seconds)),
                    now,
                    now,
                    str(row["id"]),
                    int(row["version"]),
                ),
            )
            updated = cls._owned_item(conn, str(row["id"]), owner)
            cls._outbox(conn, row=updated, event_type="guidance.claimed")
            return cls._item_from_row(updated)

    @classmethod
    def claim_next_restart_turn(
        cls,
        *,
        session_id: str,
        new_turn_id: str,
        owner_id: str,
        owner: OwnerScope,
        workdir: str,
        lease_seconds: float = 30,
    ) -> Optional[GuidanceItem]:
        """Claim external-Harness steer that must continue in a new turn.

        Native TCode steer is consumed inside the running engine at safe points.
        A runtime with ``restart_turn`` capability has no in-process safe point,
        so its durable steer is claimed after the original run has stopped.
        """

        now = time.time()
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            row = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE session_id = ? AND mode = 'steer' AND status = 'pending'
                  AND runtime_id != 'tcode-native' AND source_run_id != ''
                ORDER BY sequence LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            conn.execute(
                """
                UPDATE guidance_items SET status = 'claimed', target_turn_id = ?,
                    claim_owner = ?, claim_token = ?, claim_lease_expires_at = ?,
                    claimed_at = ?, claimed_safe_point = 'after_recovery',
                    updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'pending' AND version = ?
                """,
                (
                    new_turn_id,
                    owner_id,
                    token,
                    now + max(10.0, float(lease_seconds)),
                    now,
                    now,
                    str(row["id"]),
                    int(row["version"]),
                ),
            )
            updated = cls._owned_item(conn, str(row["id"]), owner)
            cls._outbox(conn, row=updated, event_type="guidance.claimed")
            return cls._item_from_row(updated)

    @classmethod
    def has_active_for_session(
        cls,
        *,
        session_id: str,
        owner: OwnerScope,
        workdir: str,
    ) -> bool:
        SessionSQLiteStore.ensure_initialized(workdir, force_migration=False)
        conn = SessionSQLiteStore._connect(workdir)
        try:
            row = conn.execute(
                """
                SELECT 1 FROM guidance_items
                WHERE session_id = ? AND tenant_id = ? AND user_id = ?
                  AND workspace_id = ? AND status IN (?, ?, ?, ?, ?)
                LIMIT 1
                """,
                (session_id, *cls._owner_tuple(owner), *_ACTIVE_STATUSES),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    @classmethod
    def mark_deferred(
        cls,
        *,
        guidance_id: str,
        claim_token: str,
        expected_version: int,
        reason: str,
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        now = time.time()
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, guidance_id, owner)
            cls._assert_version(row, expected_version)
            cls._assert_claim(row, claim_token, now)
            if str(row["status"]) != "claimed":
                raise GuidanceVersionConflict("only claimed guidance can be deferred")
            conn.execute(
                """
                UPDATE guidance_items SET status = 'deferred', deferred_reason = ?,
                    claim_owner = '', claim_token = '', claim_lease_expires_at = NULL,
                    updated_at = ?, version = version + 1 WHERE id = ?
                """,
                (str(reason or "")[:128], now, guidance_id),
            )
            updated = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(conn, row=updated, event_type="guidance.deferred")
            return cls._item_from_row(updated)

    @classmethod
    def release_deferred(
        cls,
        *,
        guidance_id: str,
        expected_version: int,
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, guidance_id, owner)
            cls._assert_version(row, expected_version)
            if str(row["status"]) != "deferred":
                return cls._item_from_row(row)
            conn.execute(
                """
                UPDATE guidance_items SET status = 'pending', deferred_reason = '',
                    updated_at = ?, version = version + 1 WHERE id = ?
                """,
                (time.time(), guidance_id),
            )
            updated = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(conn, row=updated, event_type="guidance.pending")
            return cls._item_from_row(updated)

    @classmethod
    def _transition_claimed_batch(
        cls,
        *,
        items: list[GuidanceItem],
        status: str,
        effect: str,
        owner: OwnerScope,
        workdir: str,
    ) -> list[GuidanceItem]:
        if not items:
            return []
        now = time.time()
        with cls._transaction(workdir) as conn:
            rows: list[sqlite3.Row] = []
            for item in items:
                row = cls._owned_item(conn, item.id, owner)
                cls._assert_version(row, item.version)
                cls._assert_claim(row, item.claim_token, now)
                allowed = {"claimed"} if status == "applying" else {"applying"}
                if str(row["status"]) not in allowed:
                    raise GuidanceVersionConflict(f"cannot transition guidance to {status}")
                rows.append(row)
            updated_items: list[GuidanceItem] = []
            for row in rows:
                terminal = status == "applied"
                conn.execute(
                    """
                    UPDATE guidance_items SET status = ?, effect = ?, applied_at = ?,
                        claim_owner = ?, claim_token = ?, claim_lease_expires_at = ?,
                        updated_at = ?, version = version + 1 WHERE id = ?
                    """,
                    (
                        status,
                        str(effect or row["effect"] or ""),
                        now if terminal else row["applied_at"],
                        "" if terminal else str(row["claim_owner"] or ""),
                        "" if terminal else str(row["claim_token"] or ""),
                        None if terminal else row["claim_lease_expires_at"],
                        now,
                        str(row["id"]),
                    ),
                )
                updated = cls._owned_item(conn, str(row["id"]), owner)
                cls._outbox(conn, row=updated, event_type=f"guidance.{status}")
                updated_items.append(cls._item_from_row(updated))
            return updated_items

    @classmethod
    def mark_applying(
        cls,
        *,
        items: list[GuidanceItem],
        owner: OwnerScope,
        workdir: str,
    ) -> list[GuidanceItem]:
        return cls._transition_claimed_batch(
            items=items,
            status="applying",
            effect="",
            owner=owner,
            workdir=workdir,
        )

    @classmethod
    def mark_applied(
        cls,
        *,
        items: list[GuidanceItem],
        effect: str,
        owner: OwnerScope,
        workdir: str,
    ) -> list[GuidanceItem]:
        return cls._transition_claimed_batch(
            items=items,
            status="applied",
            effect=effect,
            owner=owner,
            workdir=workdir,
        )

    @classmethod
    def mark_failed(
        cls,
        *,
        item: GuidanceItem,
        failure_code: str,
        failure_detail: str,
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        now = time.time()
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, item.id, owner)
            cls._assert_version(row, item.version)
            if str(row["status"]) in {"claimed", "applying", "classifying"}:
                cls._assert_claim(row, item.claim_token, now)
            conn.execute(
                """
                UPDATE guidance_items SET status = 'failed', failure_code = ?,
                    failure_detail = ?, claim_owner = '', claim_token = '',
                    claim_lease_expires_at = NULL, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (str(failure_code)[:128], str(failure_detail)[:1000], now, item.id),
            )
            updated = cls._owned_item(conn, item.id, owner)
            cls._outbox(conn, row=updated, event_type="guidance.failed")
            return cls._item_from_row(updated)

    @classmethod
    def cancel_pending(
        cls,
        *,
        guidance_id: str,
        expected_version: Optional[int],
        owner: OwnerScope,
        workdir: str,
    ) -> GuidanceItem:
        with cls._transaction(workdir) as conn:
            row = cls._owned_item(conn, guidance_id, owner)
            status = str(row["status"] or "")
            if status == "cancelled":
                return cls._item_from_row(row)
            if expected_version is not None:
                cls._assert_version(row, expected_version)
            if status not in {"classifying", "pending", "deferred"}:
                raise GuidanceNotCancellable("guidance has started applying")
            conn.execute(
                """
                UPDATE guidance_items SET status = 'cancelled', claim_owner = '',
                    claim_token = '', claim_lease_expires_at = NULL, updated_at = ?,
                    version = version + 1 WHERE id = ?
                """,
                (time.time(), guidance_id),
            )
            updated = cls._owned_item(conn, guidance_id, owner)
            cls._outbox(conn, row=updated, event_type="guidance.cancelled")
            return cls._item_from_row(updated)

    @classmethod
    def cancel_all_for_session(
        cls,
        *,
        session_id: str,
        owner: OwnerScope,
        workdir: str,
    ) -> list[GuidanceItem]:
        """Atomically fence every active guidance item after an explicit Stop."""

        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            rows = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE session_id = ? AND tenant_id = ? AND user_id = ?
                    AND workspace_id = ?
                    AND status IN ('classifying','pending','claimed','deferred','applying')
                ORDER BY sequence ASC
                """,
                (session_id, *cls._owner_tuple(owner)),
            ).fetchall()
            if not rows:
                return []
            now = time.time()
            ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE guidance_items SET status = 'cancelled', claim_owner = '',
                    claim_token = '', claim_lease_expires_at = NULL,
                    updated_at = ?, version = version + 1
                WHERE id IN ({placeholders})
                """,
                (now, *ids),
            )
            cancelled: list[GuidanceItem] = []
            for guidance_id in ids:
                updated = cls._owned_item(conn, guidance_id, owner)
                cls._outbox(conn, row=updated, event_type="guidance.cancelled")
                cancelled.append(cls._item_from_row(updated))
            return cancelled

    @classmethod
    def list_for_session(
        cls,
        *,
        session_id: str,
        owner: OwnerScope,
        workdir: str,
        statuses: Optional[Iterable[GuidanceStatus | str]] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[GuidanceItem]:
        SessionSQLiteStore.ensure_initialized(workdir, force_migration=False)
        conn = SessionSQLiteStore._connect(workdir)
        try:
            cls._session_row(conn, session_id, owner)
            params: list[Any] = [session_id, *cls._owner_tuple(owner), int(after_sequence)]
            status_values = [
                item.value if isinstance(item, GuidanceStatus) else str(item)
                for item in (statuses or [])
            ]
            status_sql = ""
            if status_values:
                placeholders = ",".join("?" for _ in status_values)
                status_sql = f" AND status IN ({placeholders})"
                params.extend(status_values)
            params.append(max(1, min(int(limit), 500)))
            rows = conn.execute(
                f"""
                SELECT * FROM guidance_items
                WHERE session_id = ? AND tenant_id = ? AND user_id = ?
                  AND workspace_id = ? AND sequence > ? {status_sql}
                ORDER BY sequence LIMIT ?
                """,
                params,
            ).fetchall()
            return [cls._item_from_row(row) for row in rows]
        finally:
            conn.close()

    @classmethod
    def reconcile_expired_claims(
        cls,
        *,
        owner: OwnerScope,
        workdir: str,
        now: Optional[float] = None,
        limit: int = 100,
    ) -> list[GuidanceItem]:
        current = float(now or time.time())
        with cls._transaction(workdir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM guidance_items
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                  AND status IN ('classifying', 'claimed', 'applying')
                  AND claim_token != '' AND claim_lease_expires_at < ?
                ORDER BY claim_lease_expires_at LIMIT ?
                """,
                (*cls._owner_tuple(owner), current, max(1, min(int(limit), 500))),
            ).fetchall()
            reconciled: list[GuidanceItem] = []
            for row in rows:
                message_exists = conn.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE session_id = ? AND guidance_id = ? LIMIT 1
                    """,
                    (str(row["session_id"]), str(row["id"])),
                ).fetchone()
                old_status = str(row["status"])
                if old_status == "applying" and message_exists is not None:
                    next_status = "applied"
                    applied_at = current
                    effect = str(row["effect"] or "reconciled_message")
                elif old_status == "classifying":
                    next_status = "classifying"
                    applied_at = row["applied_at"]
                    effect = str(row["effect"] or "")
                else:
                    next_status = "pending"
                    applied_at = row["applied_at"]
                    effect = str(row["effect"] or "")
                target_turn_id = str(row["target_turn_id"] or "")
                if str(row["mode"]) == "queue" and next_status == "pending":
                    target_turn_id = ""
                conn.execute(
                    """
                    UPDATE guidance_items SET status = ?, target_turn_id = ?, effect = ?,
                        applied_at = ?, claim_owner = '', claim_token = '',
                        claim_lease_expires_at = NULL, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        target_turn_id,
                        effect,
                        applied_at,
                        current,
                        str(row["id"]),
                    ),
                )
                updated = cls._owned_item(conn, str(row["id"]), owner)
                cls._outbox(
                    conn,
                    row=updated,
                    event_type=f"guidance.{next_status}",
                )
                reconciled.append(cls._item_from_row(updated))
            return reconciled

    @classmethod
    def register_turn_owner(
        cls,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        phase: str,
        lease_seconds: float,
        owner: OwnerScope,
        workdir: str,
    ) -> TurnOwner:
        now = time.time()
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            existing = conn.execute(
                "SELECT * FROM guidance_turn_owners WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if (
                existing is not None
                and str(existing["turn_id"]) != str(turn_id)
                and not bool(existing["completion_fenced"])
                and float(existing["lease_expires_at"] or 0) >= now
            ):
                raise GuidanceVersionConflict("another turn owns the session")
            version = int(existing["version"] or 0) + 1 if existing is not None else 1
            conn.execute(
                """
                INSERT INTO guidance_turn_owners(
                    session_id, turn_id, owner_id, phase, stream_epoch,
                    completion_fenced, lease_expires_at, heartbeat_at, version, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    turn_id=excluded.turn_id, owner_id=excluded.owner_id,
                    phase=excluded.phase, stream_epoch=excluded.stream_epoch,
                    completion_fenced=0, lease_expires_at=excluded.lease_expires_at,
                    heartbeat_at=excluded.heartbeat_at, version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    turn_id,
                    owner_id,
                    str(phase or ""),
                    now + max(10.0, float(lease_seconds)),
                    now,
                    version,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM guidance_turn_owners WHERE session_id = ?", (session_id,)
            ).fetchone()
            return cls._turn_owner_from_row(row)

    @classmethod
    def heartbeat_turn_owner(
        cls,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        phase: str,
        stream_epoch: int,
        lease_seconds: float,
        owner: OwnerScope,
        workdir: str,
    ) -> TurnOwner:
        now = time.time()
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            row = conn.execute(
                """
                SELECT * FROM guidance_turn_owners
                WHERE session_id = ? AND turn_id = ? AND owner_id = ?
                """,
                (session_id, turn_id, owner_id),
            ).fetchone()
            if row is None:
                raise GuidanceStaleTurn("turn owner not found")
            conn.execute(
                """
                UPDATE guidance_turn_owners SET phase = ?, stream_epoch = ?,
                    lease_expires_at = ?, heartbeat_at = ?, updated_at = ?,
                    version = version + 1 WHERE session_id = ?
                """,
                (
                    str(phase or ""),
                    max(0, int(stream_epoch)),
                    now + max(10.0, float(lease_seconds)),
                    now,
                    now,
                    session_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM guidance_turn_owners WHERE session_id = ?", (session_id,)
            ).fetchone()
            return cls._turn_owner_from_row(updated)

    @classmethod
    def fence_completion(
        cls,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        expected_version: Optional[int],
        owner: OwnerScope,
        workdir: str,
    ) -> str:
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            row = conn.execute(
                """
                SELECT * FROM guidance_turn_owners
                WHERE session_id = ? AND turn_id = ? AND owner_id = ?
                """,
                (session_id, turn_id, owner_id),
            ).fetchone()
            if row is None:
                raise GuidanceStaleTurn("turn owner not found")
            if expected_version is not None and int(row["version"]) != int(expected_version):
                raise GuidanceVersionConflict("turn owner version changed")
            blocker = conn.execute(
                """
                SELECT 1 FROM guidance_items
                WHERE session_id = ? AND target_turn_id = ? AND mode = 'steer'
                  AND status IN ('classifying', 'pending', 'claimed', 'deferred', 'applying')
                LIMIT 1
                """,
                (session_id, turn_id),
            ).fetchone()
            if blocker is not None:
                return "blocked_by_guidance"
            conn.execute(
                """
                UPDATE guidance_turn_owners SET completion_fenced = 1,
                    updated_at = ?, version = version + 1 WHERE session_id = ?
                """,
                (time.time(), session_id),
            )
            return "fenced"

    @classmethod
    def clear_turn_owner(
        cls,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        owner: OwnerScope,
        workdir: str,
    ) -> bool:
        with cls._transaction(workdir) as conn:
            cls._session_row(conn, session_id, owner)
            cursor = conn.execute(
                """
                DELETE FROM guidance_turn_owners
                WHERE session_id = ? AND turn_id = ? AND owner_id = ?
                """,
                (session_id, turn_id, owner_id),
            )
            return bool(cursor.rowcount)

    @classmethod
    def pending_counts(
        cls,
        *,
        session_id: str,
        owner: OwnerScope,
        workdir: str,
    ) -> dict[str, int]:
        SessionSQLiteStore.ensure_initialized(workdir, force_migration=False)
        conn = SessionSQLiteStore._connect(workdir)
        try:
            cls._session_row(conn, session_id, owner)
            rows = conn.execute(
                """
                SELECT mode, COUNT(*) AS count FROM guidance_items
                WHERE session_id = ? AND status IN ('classifying','pending','claimed','deferred','applying')
                GROUP BY mode
                """,
                (session_id,),
            ).fetchall()
            counts = {str(row["mode"]): int(row["count"] or 0) for row in rows}
            restart_row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM guidance_items
                WHERE session_id = ? AND mode = 'steer'
                  AND runtime_id != 'tcode-native' AND source_run_id != ''
                  AND status IN ('classifying','pending','claimed','deferred','applying')
                """,
                (session_id,),
            ).fetchone()
            return {
                "pending_steer_count": counts.get("steer", 0),
                "pending_queue_count": counts.get("queue", 0),
                "pending_restart_turn_count": int(restart_row["count"] or 0),
            }
        finally:
            conn.close()

    @classmethod
    def fetch_outbox(
        cls,
        *,
        workdir: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        SessionSQLiteStore.ensure_initialized(workdir, force_migration=False)
        conn = SessionSQLiteStore._connect(workdir)
        try:
            rows = conn.execute(
                """
                SELECT * FROM guidance_outbox
                WHERE published_at IS NULL AND next_attempt_at <= ?
                ORDER BY created_at LIMIT ?
                """,
                (time.time(), max(1, min(int(limit), 500))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @classmethod
    def mark_outbox_published(cls, *, event_id: str, workdir: str) -> None:
        with cls._transaction(workdir) as conn:
            conn.execute(
                "UPDATE guidance_outbox SET published_at = ?, last_error = '' WHERE event_id = ?",
                (time.time(), event_id),
            )

    @classmethod
    def mark_outbox_failed(cls, *, event_id: str, error: str, workdir: str) -> None:
        with cls._transaction(workdir) as conn:
            row = conn.execute(
                "SELECT attempts FROM guidance_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            attempts = int(row["attempts"] or 0) + 1 if row is not None else 1
            delay = min(30.0, 0.25 * (2 ** min(attempts, 7)))
            conn.execute(
                """
                UPDATE guidance_outbox SET attempts = ?, next_attempt_at = ?,
                    last_error = ? WHERE event_id = ?
                """,
                (attempts, time.time() + delay, str(error)[:500], event_id),
            )
