from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable

from .schedule import compute_next_run
from .store import ScheduledTaskStore, get_scheduled_task_store


TaskExecutor = Callable[[dict[str, Any]], Awaitable[str | dict[str, Any]]]
TaskCompletionNotifier = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


class ScheduledTaskService:
    def __init__(
        self,
        store: ScheduledTaskStore | None = None,
        *,
        poll_interval: float | None = None,
        max_concurrency: int | None = None,
    ):
        self.store = store or get_scheduled_task_store()
        configured_poll = os.environ.get("TCODE_SCHEDULE_POLL_SECONDS", "1")
        configured_concurrency = os.environ.get("TCODE_SCHEDULE_MAX_CONCURRENCY", "2")
        self.poll_interval = max(
            0.1,
            float(configured_poll if poll_interval is None else poll_interval),
        )
        self.max_concurrency = max(
            1,
            int(configured_concurrency if max_concurrency is None else max_concurrency),
        )
        self._executor: TaskExecutor | None = None
        self._completion_notifier: TaskCompletionNotifier | None = None
        self._loop_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._stopping = False

    def set_executor(self, executor: TaskExecutor) -> None:
        self._executor = executor

    def set_completion_notifier(self, notifier: TaskCompletionNotifier) -> None:
        self._completion_notifier = notifier

    @property
    def running(self) -> bool:
        return bool(self._loop_task and not self._loop_task.done())

    async def start(self) -> None:
        if self.running:
            return
        self.store.ensure_initialized()
        self.store.recover_interrupted()
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="tcode-scheduled-task-loop"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        inflight = list(self._inflight)
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        self._inflight.clear()

    async def _run_loop(self) -> None:
        while not self._stopping:
            await self.run_due_once()
            await asyncio.sleep(self.poll_interval)

    def _track(self, task: asyncio.Task) -> None:
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def run_due_once(self, *, now_ts: float | None = None) -> list[str]:
        claims = self.store.claim_due(now_ts=now_ts, limit=self.max_concurrency * 4)
        run_ids: list[str] = []
        for claim in claims:
            run_ids.append(str(claim["run_id"]))
            task = asyncio.create_task(
                self._execute_claim(claim),
                name=f"scheduled-run-{claim['run_id']}",
            )
            self._track(task)
        return run_ids

    async def dispatch_now(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str | None,
        trigger: str = "manual",
    ) -> str:
        claim = self.store.claim_now(
            task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            trigger=trigger,
        )
        task = asyncio.create_task(
            self._execute_claim(claim),
            name=f"scheduled-run-{claim['run_id']}",
        )
        self._track(task)
        return str(claim["run_id"])

    async def execute_claim_and_wait(self, claim: dict[str, Any]) -> dict[str, Any]:
        """Synchronous execution hook used by tests and maintenance code."""
        return await self._execute_claim(claim)

    async def _execute_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        run_id = str(claim["run_id"])
        task = dict(claim["task"])
        trigger = str(claim.get("trigger") or "scheduled")
        preserve_schedule = trigger != "scheduled"
        async with self._semaphore:
            if self._executor is None:
                error = "定时任务执行器尚未初始化"
                return await self._finish_and_notify(
                    task=task,
                    run_id=run_id,
                    trigger=trigger,
                    success=False,
                    error=error,
                    preserve_schedule=preserve_schedule,
                )
            try:
                result = await self._executor(task)
                if isinstance(result, dict):
                    if str(result.get("status") or "").lower() in {"failed", "error"}:
                        raise RuntimeError(str(result.get("error") or "任务执行失败"))
                    output = str(
                        result.get("output")
                        or result.get("response")
                        or result.get("content")
                        or ""
                    )
                else:
                    output = str(result or "")
                next_run_at = None
                if task.get("schedule_type") not in {"at", "delay"} and not preserve_schedule:
                    # Calculate from the current wall clock so service recovery replays only
                    # the most recent missed recurring occurrence.
                    next_run_at = compute_next_run(
                        str(task.get("schedule_type") or ""),
                        dict(task.get("schedule") or {}),
                        str(task.get("timezone") or "Asia/Shanghai"),
                        after_ts=time.time(),
                    )
                return await self._finish_and_notify(
                    task=task,
                    run_id=run_id,
                    trigger=trigger,
                    success=True,
                    output=output,
                    next_run_at=next_run_at,
                    preserve_schedule=preserve_schedule,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                next_run_at = None
                if task.get("schedule_type") not in {"at", "delay"} and not preserve_schedule:
                    try:
                        next_run_at = compute_next_run(
                            str(task.get("schedule_type") or ""),
                            dict(task.get("schedule") or {}),
                            str(task.get("timezone") or "Asia/Shanghai"),
                            after_ts=time.time(),
                        )
                    except Exception:
                        next_run_at = None
                return await self._finish_and_notify(
                    task=task,
                    run_id=run_id,
                    trigger=trigger,
                    success=False,
                    error=str(exc) or "任务执行失败",
                    next_run_at=next_run_at,
                    preserve_schedule=preserve_schedule,
                )

    async def _finish_and_notify(
        self,
        *,
        task: dict[str, Any],
        run_id: str,
        trigger: str,
        success: bool,
        output: str = "",
        error: str = "",
        next_run_at: float | None = None,
        preserve_schedule: bool = False,
    ) -> dict[str, Any]:
        finished = self.store.finish_run(
            run_id,
            success=success,
            output=output,
            error=error,
            next_run_at=next_run_at,
            preserve_schedule=preserve_schedule,
        )
        payload = {
            **dict(finished or {}),
            "run_id": run_id,
            "trigger": trigger,
            "success": bool(success),
            "output": str(output or ""),
            "error": str(error or ""),
        }
        if self._completion_notifier is not None:
            try:
                await self._completion_notifier(dict(task), payload)
            except Exception:
                # Notification delivery must not turn a completed run into a failure.
                pass
        return finished


_SERVICE: ScheduledTaskService | None = None
_SERVICE_KEY = ""


def get_scheduled_task_service() -> ScheduledTaskService:
    global _SERVICE, _SERVICE_KEY
    key = str(os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
    if _SERVICE is None or _SERVICE_KEY != key:
        _SERVICE = ScheduledTaskService(get_scheduled_task_store())
        _SERVICE_KEY = key
    return _SERVICE
