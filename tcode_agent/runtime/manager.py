"""Persistent runtime task state and event timeline."""

from __future__ import annotations

import json
import os
import secrets
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from ..atomic_storage import atomic_write_json, file_transaction


_FINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "blocked"}


def _lifecycle_status(status: str, phase: str = "") -> str:
    """Project detailed runtime phases onto one stable UI/API lifecycle."""

    normalized = str(status or "queued").strip().lower()
    normalized_phase = str(phase or "").strip().lower()
    if normalized in {"completed", "partial"}:
        return "completed"
    if normalized in {"failed", "cancelled"}:
        return "failed"
    if normalized == "blocked":
        return "blocked"
    if normalized in {"waiting_for_user", "awaiting_input", "paused", "pause_requested"}:
        return "paused"
    if normalized in {"reviewing", "revising"}:
        return "reporting"
    if normalized in {"running", "recovering"}:
        return "running"
    if normalized_phase in {"capacity_wait", "worker_wait"}:
        return "assigned"
    return "assigned" if normalized == "queued" else "available"


def _normalized_budget(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    source = dict(value or {})
    limits = source.get("limits") if isinstance(source.get("limits"), dict) else {}
    usage = source.get("usage") if isinstance(source.get("usage"), dict) else {}
    return {
        "limits": {
            "tokens": max(0, int(limits.get("tokens", 0) or 0)),
            "tool_calls": max(0, int(limits.get("tool_calls", 0) or 0)),
            "retries": max(0, int(limits.get("retries", 0) or 0)),
        },
        "usage": {
            "tokens": max(0, int(usage.get("tokens", 0) or 0)),
            "tool_calls": max(0, int(usage.get("tool_calls", 0) or 0)),
            "retries": max(0, int(usage.get("retries", 0) or 0)),
        },
        "state": str(source.get("state") or "ok"),
    }


def _runtime_locked(method):
    @wraps(method)
    def wrapped(cls, *args, **kwargs):
        with cls._LOCK, file_transaction(cls._storage_path()):
            return method(cls, *args, **kwargs)

    return wrapped


class RuntimeTaskManager:
    _LOCK = threading.RLock()

    @classmethod
    def _storage_path(cls) -> Path:
        base = os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "runtime" / "tasks.json"

    @classmethod
    def _load_all(cls) -> dict[str, dict[str, Any]]:
        with cls._LOCK:
            path = cls._storage_path()
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            if not isinstance(raw, dict) or any(not isinstance(record, dict) for record in raw.values()):
                raise ValueError(f"Runtime task store is damaged; refusing to overwrite: {path}")
            return raw

    @classmethod
    def _save_all(cls, tasks: dict[str, dict[str, Any]]) -> None:
        with cls._LOCK:
            path = cls._storage_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, tasks)

    @classmethod
    @_runtime_locked
    def ensure_task(
        cls,
        *,
        task_id: str,
        session_id: str,
        worker_name: str,
        task_prompt: str,
        mode: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        existing = tasks.get(task_id)
        if existing is None:
            now = datetime.now().timestamp()
            existing = {
                "task_id": task_id,
                "session_id": session_id,
                "worker_name": worker_name,
                "task_prompt": task_prompt,
                "mode": mode,
                "status": "queued",
                "phase": "queued",
                "lifecycle_status": "assigned",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "execution_retry_count": 0,
                "review_retry_count": 0,
                "total_rework_count": 0,
                "review_verdict": "",
                "last_error": "",
                "next_action": "",
                "result_summary": "",
                "artifact_paths": {},
                "preflight_checks": [],
                "postflight_checks": [],
                "events": [],
                "goal_id": task_id,
                "parent_task_id": "",
                "goal_ancestry": [],
                "assignee_session_id": "",
                "claim_token": "",
                "lease_expires_at": None,
                "heartbeat_at": None,
                "wake_reason": "task_created",
                "budget": _normalized_budget(None),
            }
            if metadata:
                orchestration_fields = {
                    "goal_id",
                    "parent_task_id",
                    "goal_ancestry",
                    "assignee_session_id",
                    "wake_reason",
                    "budget",
                }
                for key, value in metadata.items():
                    if key not in existing or key in orchestration_fields:
                        existing[key] = _normalized_budget(value) if key == "budget" else value
            tasks[task_id] = existing
            cls._save_all(tasks)
        return existing

    @classmethod
    def _append_event(
        cls,
        record: dict[str, Any],
        *,
        status: str,
        phase: str,
        message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        events = list(record.get("events", []) or [])
        events.append(
            {
                "timestamp": datetime.now().timestamp(),
                "status": status,
                "phase": phase,
                "message": message,
                "metadata": metadata or {},
            }
        )
        record["events"] = events[-80:]

    @classmethod
    @_runtime_locked
    def transition(
        cls,
        task_id: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        message: str = "",
        metadata: Optional[dict[str, Any]] = None,
        next_action: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")

        next_status = str(status or record.get("status", "queued"))
        next_phase = str(phase or record.get("phase", next_status))
        record["status"] = next_status
        record["phase"] = next_phase
        record["lifecycle_status"] = _lifecycle_status(next_status, next_phase)
        record["updated_at"] = datetime.now().timestamp()

        if next_status in {"running", "reviewing", "revising", "recovering"} and not record.get("started_at"):
            record["started_at"] = record["updated_at"]
        if next_status in _FINAL_STATUSES:
            record["completed_at"] = record["updated_at"]
            record["claim_token"] = ""
            record["lease_expires_at"] = None
        if next_action is not None:
            record["next_action"] = str(next_action or "")
        if last_error is not None:
            record["last_error"] = str(last_error or "")

        if message:
            cls._append_event(
                record,
                status=next_status,
                phase=next_phase,
                message=message,
                metadata=metadata,
            )

        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def try_claim(
        cls,
        task_id: str,
        *,
        assignee_session_id: str,
        lease_seconds: float = 120.0,
    ) -> Optional[str]:
        """Atomically claim a queued task, or reclaim it after its lease expires."""

        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        now = datetime.now().timestamp()
        current_token = str(record.get("claim_token") or "")
        lease_expires_at = float(record.get("lease_expires_at") or 0.0)
        if current_token and lease_expires_at > now:
            return None
        if str(record.get("status") or "") in _FINAL_STATUSES:
            return None
        token = secrets.token_urlsafe(24)
        record["assignee_session_id"] = str(assignee_session_id or "")
        record["claim_token"] = token
        record["heartbeat_at"] = now
        record["lease_expires_at"] = now + max(1.0, float(lease_seconds))
        record["updated_at"] = now
        record["lifecycle_status"] = "running"
        cls._append_event(
            record,
            status=str(record.get("status") or "queued"),
            phase="claimed",
            message="任务执行权已被原子领取。",
            metadata={"assignee_session_id": record["assignee_session_id"]},
        )
        tasks[task_id] = record
        cls._save_all(tasks)
        return token

    @classmethod
    @_runtime_locked
    def heartbeat(
        cls,
        task_id: str,
        *,
        claim_token: str,
        lease_seconds: float = 120.0,
    ) -> bool:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None or not secrets.compare_digest(
            str(record.get("claim_token") or ""), str(claim_token or "")
        ):
            return False
        now = datetime.now().timestamp()
        record["heartbeat_at"] = now
        record["lease_expires_at"] = now + max(1.0, float(lease_seconds))
        record["updated_at"] = now
        tasks[task_id] = record
        cls._save_all(tasks)
        return True

    @classmethod
    @_runtime_locked
    def release_claim(cls, task_id: str, *, claim_token: str) -> bool:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None or not secrets.compare_digest(
            str(record.get("claim_token") or ""), str(claim_token or "")
        ):
            return False
        record["claim_token"] = ""
        record["lease_expires_at"] = None
        record["updated_at"] = datetime.now().timestamp()
        tasks[task_id] = record
        cls._save_all(tasks)
        return True

    @classmethod
    @_runtime_locked
    def wake(
        cls,
        task_id: str,
        *,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Persist a wake trigger so recovery and UI share the same reason."""

        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        record["wake_reason"] = str(reason or "manual")
        record["updated_at"] = datetime.now().timestamp()
        cls._append_event(
            record,
            status=str(record.get("status") or "queued"),
            phase="wake",
            message=f"任务被事件唤醒：{record['wake_reason']}",
            metadata=metadata,
        )
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def record_usage(
        cls,
        task_id: str,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
        retries: int = 0,
    ) -> dict[str, Any]:
        """Accumulate task usage and compute ok/warning/exhausted budget state."""

        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        budget = _normalized_budget(record.get("budget"))
        increments = {"tokens": tokens, "tool_calls": tool_calls, "retries": retries}
        for key, amount in increments.items():
            budget["usage"][key] += max(0, int(amount or 0))
        ratios = [
            budget["usage"][key] / limit
            for key, limit in budget["limits"].items()
            if limit > 0
        ]
        maximum = max(ratios, default=0.0)
        budget["state"] = "exhausted" if maximum >= 1 else "warning" if maximum >= 0.8 else "ok"
        record["budget"] = budget
        record["updated_at"] = datetime.now().timestamp()
        if budget["state"] != "ok":
            cls._append_event(
                record,
                status=str(record.get("status") or "queued"),
                phase="budget",
                message="任务预算已耗尽。" if budget["state"] == "exhausted" else "任务预算使用达到 80%。",
                metadata={"budget": budget},
            )
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def control_execution(cls, task_id: str, *, action: str = "checkpoint",
                          limits: Optional[dict[str, int]] = None) -> dict[str, Any]:
        """Pause at safe boundaries; resume the same task without resetting usage."""
        tasks = cls._load_all()
        record = tasks[task_id]
        previous_status, previous_budget = record['status'], record.get('budget')
        if record["status"] in _FINAL_STATUSES:
            raise ValueError("任务已结束，不能恢复原执行现场。")
        if action not in {"checkpoint", "pause", "resume"}:
            raise ValueError("未知控制操作。")
        budget = _normalized_budget(record.get("budget"))
        for key, value in (limits or {}).items():
            if (action != "resume" or key not in {"tokens", "tool_calls"}
                    or type(value) is not int or value <= budget["limits"][key]
                    or budget["limits"][key] == 0):
                raise ValueError("只能显式增加现有 token 或工具额度，不能修改重试上限。")
            budget["limits"][key] = value
        maximum = max((budget["usage"][key] / limit
                       for key, limit in budget["limits"].items() if limit > 0), default=0)
        budget["state"] = "exhausted" if maximum >= 1 else "warning" if maximum >= .8 else "ok"
        if action == "resume" and budget["state"] == "exhausted":
            raise ValueError("预算已耗尽，请明确增加相应额度后再恢复；用量不会清零。")
        control = dict(record.get("execution_control") or {})
        before = dict(control)
        if action == "pause":
            control["pause_requested"] = True
        elif action == "resume":
            control["pause_requested"] = False
        held = control.get("pause_requested") or budget["state"] == "exhausted"
        if held:
            if record["status"] not in {"paused", "pause_requested"}:
                control.update(previous_status=record["status"], previous_phase=record["phase"])
            control["reason"] = "budget_exhausted" if budget["state"] == "exhausted" else "user_pause"
            status = "paused" if action == "checkpoint" or record["status"] == "paused" else "pause_requested"
            record.update(status=status, lifecycle_status="paused",
                          next_action="预算已耗尽，请增加额度后恢复。" if control["reason"] == "budget_exhausted"
                          else "进度保留；到达安全边界后暂停，可恢复原任务。")
        elif record["status"] in {"paused", "pause_requested"}:
            record.update(status=control.get("previous_status", "running"),
                          phase=control.get("previous_phase", "execution"), next_action="继续原任务。")
            record["lifecycle_status"] = _lifecycle_status(record["status"], record["phase"])
            control["reason"] = ""
        record["budget"] = budget
        record["execution_control"] = control
        if (control != before or record['status'] != previous_status
                or budget != previous_budget or action != "checkpoint"):
            record["updated_at"] = datetime.now().timestamp()
            cls._append_event(record, status=record["status"], phase="execution_control",
                              message=record.get("next_action", ""))
            cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def record_checks(
        cls,
        task_id: str,
        *,
        stage: str,
        checks: list[dict[str, Any]],
        summary: str = "",
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        normalized_stage = "postflight" if str(stage).strip().lower() == "postflight" else "preflight"
        record[f"{normalized_stage}_checks"] = list(checks or [])
        if summary:
            cls._append_event(
                record,
                status=str(record.get("status", "queued")),
                phase=normalized_stage,
                message=summary,
                metadata={"check_count": len(checks or [])},
            )
        record["updated_at"] = datetime.now().timestamp()
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def update_counters(
        cls,
        task_id: str,
        *,
        execution_retry_count: Optional[int] = None,
        review_retry_count: Optional[int] = None,
        total_rework_count: Optional[int] = None,
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        if execution_retry_count is not None:
            record["execution_retry_count"] = max(0, int(execution_retry_count))
        if review_retry_count is not None:
            record["review_retry_count"] = max(0, int(review_retry_count))
        if total_rework_count is not None:
            record["total_rework_count"] = max(0, int(total_rework_count))
        record["updated_at"] = datetime.now().timestamp()
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def update_metadata(
        cls,
        task_id: str,
        **metadata: Any,
    ) -> dict[str, Any]:
        """Attach durable orchestration metadata without changing task status."""

        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        for key, value in metadata.items():
            if str(key or "").strip():
                record[str(key)] = value
        record["updated_at"] = datetime.now().timestamp()
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def record_result(
        cls,
        task_id: str,
        *,
        status: str,
        result_summary: str = "",
        review_verdict: str = "",
        recommended_next_step: str = "",
        last_error: str = "",
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        waiting_for_user = status in {"waiting_for_user", "awaiting_input"}
        record["status"] = "waiting_for_user" if waiting_for_user else status
        record["phase"] = (
            "awaiting_input"
            if waiting_for_user
            else ("completed" if status == "completed" else "terminal")
        )
        record["lifecycle_status"] = _lifecycle_status(record["status"], record["phase"])
        record["result_summary"] = str(result_summary or "")[:1200]
        record["review_verdict"] = str(review_verdict or "")
        record["next_action"] = str(recommended_next_step or "")
        record["last_error"] = str(last_error or "")
        record["updated_at"] = datetime.now().timestamp()
        record["completed_at"] = None if waiting_for_user else record["updated_at"]
        if not waiting_for_user:
            record["claim_token"] = ""
            record["lease_expires_at"] = None
        cls._append_event(
            record,
            status=record["status"],
            phase=record["phase"],
            message=(
                "任务正在等待父 Agent 或用户输入。"
                if waiting_for_user
                else "任务运行结束。"
            ),
            metadata={
                "review_verdict": record["review_verdict"],
                "next_action": record["next_action"],
            },
        )
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    @_runtime_locked
    def attach_artifacts(
        cls,
        task_id: str,
        artifact_paths: dict[str, Any],
    ) -> dict[str, Any]:
        tasks = cls._load_all()
        record = tasks.get(task_id)
        if record is None:
            raise KeyError(f"runtime task not found: {task_id}")
        record["artifact_paths"] = dict(artifact_paths or {})
        record["updated_at"] = datetime.now().timestamp()
        tasks[task_id] = record
        cls._save_all(tasks)
        return record

    @classmethod
    def get(cls, task_id: str) -> Optional[dict[str, Any]]:
        return cls._load_all().get(task_id)

    @classmethod
    def find_by_invocation(
        cls,
        invocation_id: str,
        *,
        session_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """Find the newest durable task created by one tool invocation."""

        normalized = str(invocation_id or "").strip()
        if not normalized:
            return None
        normalized_session = str(session_id or "").strip()
        matches = [
            task
            for task in cls._load_all().values()
            if str(task.get("invocation_id") or "").strip() == normalized
            and (
                not normalized_session
                or str(task.get("session_id") or "").strip() == normalized_session
            )
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: float(item.get("updated_at", 0.0) or 0.0))

    @classmethod
    def list_for_session(cls, session_id: str) -> list[dict[str, Any]]:
        tasks = cls._load_all()
        items = [task for task in tasks.values() if str(task.get("session_id", "")) == str(session_id)]
        return sorted(items, key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)

    @classmethod
    def summary_for_session(cls, session_id: str) -> dict[str, Any]:
        tasks = cls.list_for_session(session_id)
        counts = {
            "queued": 0,
            "running": 0,
            "reviewing": 0,
            "revising": 0,
            "recovering": 0,
            "waiting_for_user": 0,
            "blocked": 0,
            "completed": 0,
            "partial": 0,
            "failed": 0,
            "cancelled": 0,
        }
        recent_events: list[dict[str, Any]] = []
        for task in tasks:
            status = str(task.get("status", "") or "")
            if status in counts:
                counts[status] += 1
            for event in list(task.get("events", []) or [])[-4:]:
                recent_events.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "worker_name": task.get("worker_name", ""),
                        **event,
                    }
                )
        recent_events.sort(key=lambda item: float(item.get("timestamp", 0.0)), reverse=True)
        return {
            "total": len(tasks),
            "counts": counts,
            "tasks": tasks[:12],
            "recent_events": recent_events[:20],
        }
