"""Durable in-turn user guidance domain model and runtime port."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Protocol


class GuidanceMode(str, Enum):
    STEER = "steer"
    QUEUE = "queue"


class GuidanceStatus(str, Enum):
    CLASSIFYING = "classifying"
    PENDING = "pending"
    CLAIMED = "claimed"
    DEFERRED = "deferred"
    APPLYING = "applying"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GuidanceSafePoint(str, Enum):
    AFTER_RECOVERY = "after_recovery"
    BEFORE_COMPACTION = "before_compaction"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    BEFORE_FIRST_TOOL = "before_first_tool"
    AFTER_TOOL_RESULT = "after_tool_result"
    BEFORE_NEXT_TOOL = "before_next_tool"
    BEFORE_OUTCOME_REPAIR = "before_outcome_repair"
    BEFORE_FINALIZE = "before_finalize"


TERMINAL_GUIDANCE_STATUSES = frozenset(
    {
        GuidanceStatus.APPLIED,
        GuidanceStatus.SUPERSEDED,
        GuidanceStatus.CANCELLED,
        GuidanceStatus.FAILED,
    }
)


@dataclass(frozen=True)
class OwnerScope:
    tenant_id: str
    user_id: str
    workspace_id: str


@dataclass
class GuidanceItem:
    id: str
    session_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    client_request_id: str
    target_turn_id: str
    sequence: int
    mode: GuidanceMode
    status: GuidanceStatus
    priority: int
    message: str
    file_refs: list[dict[str, Any]] = field(default_factory=list)
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None
    contract_revision: Optional[dict[str, Any]] = None
    apply_policy: str = "next_safe_point"
    descendant_policy: str = "preserve"
    supersedes_guidance_id: str = ""
    received_phase: str = ""
    runtime_id: str = "tcode-native"
    model_id: str = ""
    harness_switch_version: int = 0
    source_run_id: str = ""
    claimed_safe_point: str = ""
    claim_owner: str = ""
    claim_token: str = ""
    claim_lease_expires_at: Optional[float] = None
    deferred_reason: str = ""
    effect: str = ""
    failure_code: str = ""
    failure_detail: str = ""
    version: int = 1
    created_at: float = 0
    updated_at: float = 0
    claimed_at: Optional[float] = None
    applied_at: Optional[float] = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_GUIDANCE_STATUSES

    def public_projection(self, *, include_preview: bool = True) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "id": self.id,
            "sequence": self.sequence,
            "mode": self.mode.value,
            "status": self.status.value,
            "target_turn_id": self.target_turn_id,
            "received_phase": self.received_phase,
            "runtime_id": self.runtime_id,
            "model_id": self.model_id,
            "harness_switch_version": self.harness_switch_version,
            "claimed_safe_point": self.claimed_safe_point,
            "deferred_reason": self.deferred_reason,
            "effect": self.effect,
            "failure_code": self.failure_code,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "applied_at": self.applied_at,
        }
        if include_preview:
            projection["message_preview"] = self.message[:240]
        return projection


@dataclass
class TurnOwner:
    session_id: str
    turn_id: str
    owner_id: str
    phase: str
    stream_epoch: int
    completion_fenced: bool
    lease_expires_at: float
    heartbeat_at: float
    version: int
    updated_at: float


@dataclass
class GuidanceApplyResult:
    applied_ids: list[str] = field(default_factory=list)
    replan_required: bool = False
    contract_changed: bool = False
    skipped_tool_call_ids: list[str] = field(default_factory=list)


class GuidanceError(RuntimeError):
    code = "guidance_error"


class GuidanceNotFound(GuidanceError):
    code = "guidance_not_found"


class GuidanceVersionConflict(GuidanceError):
    code = "guidance_version_conflict"


class GuidanceClaimLost(GuidanceError):
    code = "guidance_claim_lost"


class GuidanceQueueFull(GuidanceError):
    code = "guidance_queue_full"


class GuidanceStaleTurn(GuidanceError):
    code = "stale_turn"


class GuidanceNotCancellable(GuidanceError):
    code = "guidance_not_cancellable"


class GuidanceSupersedeConflict(GuidanceError):
    code = "supersede_conflict"


class ModelCallSupersededByGuidance(Exception):
    def __init__(self, guidance_ids: list[str]):
        super().__init__("model output superseded by guidance")
        self.guidance_ids = list(guidance_ids)


class GuidancePort(Protocol):
    async def wait_for_steer(
        self,
        *,
        session_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> bool: ...

    async def claim_for_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        safe_point: GuidanceSafePoint,
    ) -> list[GuidanceItem]: ...

    async def mark_deferred(
        self, *, item: GuidanceItem, reason: str
    ) -> GuidanceItem: ...

    async def mark_applying(
        self, *, items: list[GuidanceItem]
    ) -> list[GuidanceItem]: ...

    async def mark_applied(
        self, *, items: list[GuidanceItem], effect: str
    ) -> list[GuidanceItem]: ...

    async def mark_failed(
        self,
        *,
        item: GuidanceItem,
        failure_code: str,
        failure_detail: str,
    ) -> GuidanceItem: ...

    async def fence_completion(
        self, *, session_id: str, turn_id: str, owner_id: str
    ) -> Literal["fenced", "blocked_by_guidance"]: ...


class GuidanceController(GuidancePort):
    """Async adapter binding a workdir/owner scope to the SQLite authority."""

    def __init__(
        self,
        *,
        workdir: str,
        owner: OwnerScope,
        notifier: Any,
        poll_interval_seconds: float = 0.25,
        batch_max_items: int = 8,
        claim_lease_seconds: float = 30,
    ) -> None:
        self.workdir = str(workdir)
        self.owner = owner
        self.notifier = notifier
        self.poll_interval_seconds = max(0.1, min(2.0, float(poll_interval_seconds)))
        self.batch_max_items = max(1, min(16, int(batch_max_items)))
        self.claim_lease_seconds = max(10.0, min(300.0, float(claim_lease_seconds)))
        from .guidance_notifier import workspace_notification_key

        self.workspace_key = workspace_notification_key(self.workdir)
        self._reconciled = False
        self._reconcile_lock: Any = None

    async def _to_thread(self, method_name: str, **kwargs: Any) -> Any:
        import asyncio

        from .guidance_store import GuidanceSQLiteStore

        method = getattr(GuidanceSQLiteStore, method_name)
        return await asyncio.to_thread(
            method,
            owner=self.owner,
            workdir=self.workdir,
            **kwargs,
        )

    def _telemetry(
        self,
        event_name: str,
        payload: dict[str, Any],
        *,
        session_id: str = "",
    ) -> None:
        from .telemetry import TelemetryGateway

        TelemetryGateway.emit(
            event_name,
            payload,
            tenant_id=self.owner.tenant_id,
            user_id=self.owner.user_id,
            workspace_id=self.owner.workspace_id,
            session_id=session_id,
        )

    async def _ensure_reconciled(self) -> None:
        if self._reconciled:
            return
        import asyncio

        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()
        async with self._reconcile_lock:
            if self._reconciled:
                return
            recovered = await self._to_thread("reconcile_expired_claims")
            self._reconciled = True
            if recovered:
                from .telemetry import TelemetryGateway

                TelemetryGateway.emit(
                    "guidance.recovery",
                    {
                        "phase": "expired_claims",
                        "result": "reconciled",
                        "count": len(recovered),
                    },
                    tenant_id=self.owner.tenant_id,
                    user_id=self.owner.user_id,
                    workspace_id=self.owner.workspace_id,
                )

    async def wait_for_steer(
        self,
        *,
        session_id: str,
        turn_id: str,
        timeout_seconds: float,
    ) -> bool:
        import asyncio

        await self._ensure_reconciled()
        deadline = asyncio.get_running_loop().time() + max(0.01, timeout_seconds)
        while True:
            items = await self._to_thread(
                "list_for_session",
                session_id=session_id,
                statuses=[GuidanceStatus.PENDING],
                after_sequence=0,
                limit=self.batch_max_items,
            )
            if any(
                item.mode is GuidanceMode.STEER
                and item.target_turn_id == turn_id
                and item.apply_policy == "next_safe_point"
                for item in items
            ):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await self.notifier.wait(
                workspace_key=self.workspace_key,
                session_id=session_id,
                turn_id=turn_id,
                timeout_seconds=min(self.poll_interval_seconds, remaining),
            )

    async def claim_for_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        safe_point: GuidanceSafePoint,
    ) -> list[GuidanceItem]:
        await self._ensure_reconciled()
        claimed = await self._to_thread(
            "claim_for_turn",
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
            safe_point=safe_point,
            max_items=self.batch_max_items,
            lease_seconds=self.claim_lease_seconds,
        )
        self._telemetry(
            "guidance.claim",
            {
                "safe_point": safe_point.value,
                "result": "claimed" if claimed else "empty",
                "count": len(claimed),
            },
            session_id=session_id,
        )
        return claimed

    async def mark_deferred(
        self, *, item: GuidanceItem, reason: str
    ) -> GuidanceItem:
        return await self._to_thread(
            "mark_deferred",
            guidance_id=item.id,
            claim_token=item.claim_token,
            expected_version=item.version,
            reason=reason,
        )

    async def mark_applying(
        self, *, items: list[GuidanceItem]
    ) -> list[GuidanceItem]:
        return await self._to_thread("mark_applying", items=items)

    async def mark_applied(
        self, *, items: list[GuidanceItem], effect: str
    ) -> list[GuidanceItem]:
        applied = await self._to_thread("mark_applied", items=items, effect=effect)
        self._telemetry(
            "guidance.apply",
            {
                "result": "applied",
                "effect": effect,
                "count": len(applied),
            },
            session_id=applied[0].session_id if applied else "",
        )
        return applied

    async def mark_failed(
        self,
        *,
        item: GuidanceItem,
        failure_code: str,
        failure_detail: str,
    ) -> GuidanceItem:
        failed = await self._to_thread(
            "mark_failed",
            item=item,
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
        self._telemetry(
            "guidance.apply",
            {"result": "failed", "failure_code": failure_code, "count": 1},
            session_id=failed.session_id,
        )
        return failed

    async def fence_completion(
        self, *, session_id: str, turn_id: str, owner_id: str
    ) -> Literal["fenced", "blocked_by_guidance"]:
        result = await self._to_thread(
            "fence_completion",
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
            expected_version=None,
        )
        return result

    async def register_turn_owner(
        self,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        phase: str,
    ) -> TurnOwner:
        await self._ensure_reconciled()
        return await self._to_thread(
            "register_turn_owner",
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
            phase=phase,
            lease_seconds=self.claim_lease_seconds,
        )

    async def heartbeat_turn_owner(
        self,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        phase: str,
        stream_epoch: int,
    ) -> TurnOwner:
        return await self._to_thread(
            "heartbeat_turn_owner",
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
            phase=phase,
            stream_epoch=stream_epoch,
            lease_seconds=self.claim_lease_seconds,
        )

    async def clear_turn_owner(
        self, *, session_id: str, turn_id: str, owner_id: str
    ) -> bool:
        return await self._to_thread(
            "clear_turn_owner",
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
        )

    async def defer_pending_for_wait(
        self,
        *,
        session_id: str,
        turn_id: str,
        owner_id: str,
        reason: str,
    ) -> list[GuidanceItem]:
        items = await self.claim_for_turn(
            session_id=session_id,
            turn_id=turn_id,
            owner_id=owner_id,
            safe_point=GuidanceSafePoint.BEFORE_NEXT_TOOL,
        )
        deferred: list[GuidanceItem] = []
        for item in items:
            deferred.append(await self.mark_deferred(item=item, reason=reason))
        return deferred

    async def release_deferred_for_turn(
        self, *, session_id: str, turn_id: str
    ) -> list[GuidanceItem]:
        items = await self._to_thread(
            "list_for_session",
            session_id=session_id,
            statuses=[GuidanceStatus.DEFERRED],
            after_sequence=0,
            limit=500,
        )
        released: list[GuidanceItem] = []
        for item in items:
            if item.target_turn_id != turn_id:
                continue
            released.append(
                await self._to_thread(
                    "release_deferred",
                    guidance_id=item.id,
                    expected_version=item.version,
                )
            )
        return released
