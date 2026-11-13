"""Server orchestration for durable Guidance v2."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..runtime.guidance import (
    GuidanceController,
    GuidanceItem,
    GuidanceMode,
    GuidanceStatus,
    OwnerScope,
)
from ..runtime.guidance_notifier import (
    InProcessGuidanceNotifier,
    RedisGuidanceNotifier,
    workspace_notification_key,
)
from ..runtime.guidance_store import GuidanceSQLiteStore
from ..runtime.telemetry import TelemetryGateway
from .state import ServerState


Classifier = Callable[
    [GuidanceItem],
    Awaitable[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], str]],
]
CancelTurn = Callable[[GuidanceItem], Awaitable[None]]
PostClassify = Callable[[GuidanceItem], Awaitable[Any]]


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class GuidanceRuntime:
    def __init__(self) -> None:
        backend = str(os.environ.get("TCODE_GUIDANCE_NOTIFY_BACKEND", "auto") or "auto")
        redis_url = str(os.environ.get("TCODE_GUIDANCE_REDIS_URL", "") or "").strip()
        if backend == "redis" or (backend == "auto" and redis_url):
            self.notifier = RedisGuidanceNotifier(redis_url)
        else:
            self.notifier = InProcessGuidanceNotifier()
        self._outbox_tasks: dict[str, asyncio.Task] = {}
        self._classification_tasks: dict[tuple[str, OwnerScope], asyncio.Task] = {}
        self._closed = False

    def start(self) -> None:
        self._closed = False

    def controller(self, *, workdir: str, owner: OwnerScope) -> GuidanceController:
        return GuidanceController(
            workdir=workdir,
            owner=owner,
            notifier=self.notifier,
            poll_interval_seconds=_env_float(
                "TCODE_GUIDANCE_POLL_INTERVAL_MS", 250, 100, 2000
            )
            / 1000,
            batch_max_items=_env_int("TCODE_GUIDANCE_BATCH_MAX_ITEMS", 8, 1, 16),
            claim_lease_seconds=_env_float(
                "TCODE_GUIDANCE_CLAIM_LEASE_SECONDS", 30, 10, 300
            ),
        )

    def ensure_workdir(self, workdir: str) -> None:
        normalized = Path(workdir).resolve(strict=False).as_posix()
        existing = self._outbox_tasks.get(normalized)
        if existing is not None and not existing.done():
            return
        self._outbox_tasks[normalized] = asyncio.create_task(
            self._outbox_loop(normalized),
            name=f"guidance-outbox:{workspace_notification_key(normalized)}",
        )

    async def _outbox_loop(self, workdir: str) -> None:
        while not self._closed:
            try:
                rows = await asyncio.to_thread(
                    GuidanceSQLiteStore.fetch_outbox,
                    workdir=workdir,
                    limit=100,
                )
                if not rows:
                    await asyncio.sleep(0.1)
                    continue
                for row in rows:
                    try:
                        payload = json.loads(str(row.get("payload_json") or "{}"))
                        session_id = str(payload.get("session_id") or row.get("session_id") or "")
                        turn_id = str(payload.get("target_turn_id") or row.get("target_turn_id") or "")
                        guidance_id = str(payload.get("guidance_id") or row.get("guidance_id") or "")
                        await self.notifier.publish(
                            workspace_key=workspace_notification_key(workdir),
                            session_id=session_id,
                            turn_id=turn_id,
                            guidance_id=guidance_id,
                        )
                        await ServerState.send_session_event(
                            session_id,
                            {
                                "type": "agent_event",
                                "kind": "guidance.lifecycle",
                                "session_id": session_id,
                                "guidance_id": guidance_id,
                                "sequence": int(payload.get("sequence") or 0),
                                "status": str(payload.get("status") or ""),
                                "apply_state": str(payload.get("status") or ""),
                                "target_turn_id": turn_id,
                                "mode": str(payload.get("mode") or ""),
                                "safe_point": str(
                                    payload.get("claimed_safe_point") or ""
                                ),
                                "effect": str(payload.get("effect") or ""),
                                "failure_code": str(
                                    payload.get("failure_code") or ""
                                ),
                                "version": int(payload.get("version") or 0),
                            },
                        )
                        await asyncio.to_thread(
                            GuidanceSQLiteStore.mark_outbox_published,
                            event_id=str(row.get("event_id") or ""),
                            workdir=workdir,
                        )
                    except Exception as exc:
                        await asyncio.to_thread(
                            GuidanceSQLiteStore.mark_outbox_failed,
                            event_id=str(row.get("event_id") or ""),
                            error=str(exc),
                            workdir=workdir,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.25)

    def schedule_classification(
        self,
        *,
        workdir: str,
        owner: OwnerScope,
        classifier: Classifier,
        cancel_turn: CancelTurn,
        post_classify: Optional[PostClassify] = None,
    ) -> None:
        self.ensure_workdir(workdir)
        key = (Path(workdir).resolve(strict=False).as_posix(), owner)
        current = self._classification_tasks.get(key)
        if current is not None and not current.done():
            return
        self._classification_tasks[key] = asyncio.create_task(
            self._classification_loop(
                workdir=key[0],
                owner=owner,
                classifier=classifier,
                cancel_turn=cancel_turn,
                post_classify=post_classify,
            ),
            name=f"guidance-classifier:{workspace_notification_key(key[0])}:{owner.user_id}",
        )

    async def _classification_loop(
        self,
        *,
        workdir: str,
        owner: OwnerScope,
        classifier: Classifier,
        cancel_turn: CancelTurn,
        post_classify: Optional[PostClassify],
    ) -> None:
        classifier_id = f"classifier:{uuid.uuid4()}"
        while not self._closed:
            item = await asyncio.to_thread(
                GuidanceSQLiteStore.claim_next_classification,
                owner_id=classifier_id,
                lease_seconds=max(
                    30,
                    _env_float("TCODE_GUIDANCE_CLASSIFY_TIMEOUT_SECONDS", 12, 2, 60)
                    + 5,
                ),
                owner=owner,
                workdir=workdir,
            )
            if item is None:
                return
            started_at = asyncio.get_running_loop().time()
            try:
                timeout = _env_float(
                    "TCODE_GUIDANCE_CLASSIFY_TIMEOUT_SECONDS", 12, 2, 60
                )
                try:
                    revision, outcome_contract, effect = await asyncio.wait_for(
                        classifier(item), timeout=timeout
                    )
                    classification_result = "classified"
                except (asyncio.TimeoutError, OSError, ConnectionError):
                    revision, outcome_contract, effect = (
                        {"classification": "REFINEMENT", "degraded": True},
                        item.outcome_contract,
                        "classification_degraded",
                    )
                    classification_result = "degraded"
                classified = await asyncio.to_thread(
                    GuidanceSQLiteStore.complete_classification,
                    guidance_id=item.id,
                    claim_token=item.claim_token,
                    expected_version=item.version,
                    contract_revision=revision,
                    outcome_contract=outcome_contract,
                    effect=effect,
                    owner=owner,
                    workdir=workdir,
                )
                if post_classify is not None:
                    try:
                        await post_classify(classified)
                    except Exception:
                        # Descendant cancellation is conservative and optional;
                        # failure preserves children and must not lose guidance.
                        pass
                if classified.status is GuidanceStatus.APPLYING:
                    try:
                        await cancel_turn(classified)
                    except Exception as exc:
                        await asyncio.to_thread(
                            GuidanceSQLiteStore.mark_failed,
                            item=classified,
                            failure_code="turn_cancel_failed",
                            failure_detail=str(exc),
                            owner=owner,
                            workdir=workdir,
                        )
                    else:
                        await asyncio.to_thread(
                            GuidanceSQLiteStore.mark_applied,
                            items=[classified],
                            effect="turn_cancelled",
                            owner=owner,
                            workdir=workdir,
                        )
                TelemetryGateway.emit(
                    "guidance.classification",
                    {
                        "result": classification_result,
                        "effect": effect,
                        "latency_ms": int(
                            (asyncio.get_running_loop().time() - started_at) * 1000
                        ),
                    },
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    workspace_id=owner.workspace_id,
                    session_id=item.session_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await asyncio.to_thread(
                        GuidanceSQLiteStore.fail_classification,
                        guidance_id=item.id,
                        claim_token=item.claim_token,
                        expected_version=item.version,
                        failure_code="classification_failed",
                        failure_detail=str(exc),
                        owner=owner,
                        workdir=workdir,
                    )
                except Exception:
                    pass
                TelemetryGateway.emit(
                    "guidance.classification",
                    {
                        "result": "failed",
                        "error_type": type(exc).__name__,
                        "latency_ms": int(
                            (asyncio.get_running_loop().time() - started_at) * 1000
                        ),
                    },
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    workspace_id=owner.workspace_id,
                    session_id=item.session_id,
                )

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._outbox_tasks.values(), *self._classification_tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._outbox_tasks.clear()
        self._classification_tasks.clear()
        close = getattr(self.notifier, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


guidance_runtime = GuidanceRuntime()
