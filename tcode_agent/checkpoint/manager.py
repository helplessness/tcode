"""Unified checkpoint & resume framework supporting supervisor/team/swarm modes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from ..file_lock import lock_state_file as lock_file, state_thread_lock, unlock_file


class Checkpoint:
    """Unified checkpoint snapshot for session/task recovery."""

    def __init__(
        self,
        task_id: str,
        session_id: str,
        round_index: int = 0,
        step_name: Literal[
            "init",
            "retrieve",
            "execute",
            "review",
            "finalize",
            "completed",
            "resumed",
        ] = "init",
        message_cursor: int = 0,
        state_snapshot: Optional[dict[str, Any]] = None,
        contract_snapshot: Optional[dict[str, Any]] = None,
        artifacts: Optional[dict[str, Any]] = None,
        pending_action: str = "",
        resume_point: str = "",
        metadata: Optional[dict[str, Any]] = None,
        schema_version: str = "tcode.checkpoint/v1",
        milestone: str = "",
        evidence_refs: Optional[list[dict[str, Any]]] = None,
        unresolved_questions: Optional[list[str]] = None,
    ):
        self.id = f"ckpt-{datetime.now().timestamp()}"
        self.task_id = task_id
        self.session_id = session_id
        self.round_index = round_index
        self.step_name = step_name
        self.message_cursor = message_cursor
        self.state_snapshot = state_snapshot or {}
        self.contract_snapshot = contract_snapshot or {}
        self.artifacts = artifacts or {}
        self.pending_action = pending_action
        self.resume_point = resume_point
        self.metadata = metadata or {}
        self.schema_version = str(schema_version or "tcode.checkpoint/v1")
        self.milestone = str(milestone or "")
        self.evidence_refs = list(evidence_refs or [])
        self.unresolved_questions = list(unresolved_questions or [])
        self.timestamp = datetime.now().timestamp()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "round_index": self.round_index,
            "step_name": self.step_name,
            "message_cursor": self.message_cursor,
            "state_snapshot": self.state_snapshot,
            "contract_snapshot": self.contract_snapshot,
            "artifacts": self.artifacts,
            "pending_action": self.pending_action,
            "resume_point": self.resume_point,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
            "milestone": self.milestone,
            "evidence_refs": self.evidence_refs,
            "unresolved_questions": self.unresolved_questions,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        """Deserialize from dict."""
        state_snapshot = data.get("state_snapshot")
        contract_snapshot = data.get("contract_snapshot")
        artifacts = data.get("artifacts")
        metadata = data.get("metadata")
        checkpoint = cls(
            task_id=str(data.get("task_id", "")),
            session_id=str(data.get("session_id", "")),
            round_index=int(data.get("round_index", 0)),
            step_name=str(data.get("step_name", "init")),
            message_cursor=int(data.get("message_cursor", 0)),
            state_snapshot=(
                dict(state_snapshot) if isinstance(state_snapshot, dict) else {}
            ),
            contract_snapshot=(
                dict(contract_snapshot) if isinstance(contract_snapshot, dict) else {}
            ),
            artifacts=dict(artifacts) if isinstance(artifacts, dict) else {},
            pending_action=str(data.get("pending_action", "")),
            resume_point=str(data.get("resume_point", "")),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            schema_version=str(data.get("schema_version") or "tcode.checkpoint/v1"),
            milestone=str(data.get("milestone") or ""),
            evidence_refs=[
                dict(item)
                for item in (data.get("evidence_refs") or [])
                if isinstance(item, dict)
            ],
            unresolved_questions=[
                str(item)
                for item in (data.get("unresolved_questions") or [])
                if str(item or "").strip()
            ],
        )
        checkpoint.id = str(data.get("id") or checkpoint.id)
        try:
            checkpoint.timestamp = float(
                data.get("timestamp") or checkpoint.timestamp
            )
        except (TypeError, ValueError):
            pass
        return checkpoint


class CheckpointManager:
    """Manage checkpoint persistence and recovery."""

    _thread_locks_guard = threading.Lock()
    _thread_locks: dict[str, threading.RLock] = {}
    _thread_lock_users: dict[str, int] = {}
    _milestone_priority = {
        "manual": 10,
        "context_pressure": 20,
        "goal_changed": 40,
        "tool_batch": 50,
        "waiting_for_input": 70,
        "async_completed": 80,
        "turn_completed": 90,
    }

    @classmethod
    def _checkpoint_dir(cls, workdir: Optional[str] = None) -> Path:
        """Get checkpoint storage directory."""
        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        checkpoint_dir = Path(base) / ".tcode" / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        return checkpoint_dir

    @staticmethod
    def _safe_task_stem(task_id: str) -> str:
        """Return a filesystem-safe name without changing normal session IDs."""

        raw = str(task_id or "task")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", raw):
            return raw
        prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")[:80]
        digest = hashlib.sha256(
            raw.encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        return f"{prefix or 'task'}-{digest}"

    @classmethod
    @contextmanager
    def _thread_guard(cls, key: str) -> Iterator[None]:
        with cls._thread_locks_guard:
            lock = cls._thread_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._thread_locks[key] = lock
            cls._thread_lock_users[key] = cls._thread_lock_users.get(key, 0) + 1
        try:
            with state_thread_lock(lock):
                yield
        finally:
            with cls._thread_locks_guard:
                users = cls._thread_lock_users.get(key, 1) - 1
                if users <= 0:
                    cls._thread_lock_users.pop(key, None)
                    cls._thread_locks.pop(key, None)
                else:
                    cls._thread_lock_users[key] = users

    @classmethod
    @contextmanager
    def _task_guard(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
    ) -> Iterator[tuple[Path, str]]:
        checkpoint_dir = cls._checkpoint_dir(workdir)
        task_stem = cls._safe_task_stem(task_id)
        lock_key = str((checkpoint_dir / task_stem).resolve())
        with cls._thread_guard(lock_key):
            lock_path = checkpoint_dir / f".{task_stem}.lock"
            with open(lock_path, "a+", encoding="utf-8") as lock_handle:
                lock_file(lock_handle)
                try:
                    yield checkpoint_dir, task_stem
                finally:
                    unlock_file(lock_handle)

    @classmethod
    def _load_unlocked(
        cls,
        checkpoint_dir: Path,
        task_stem: str,
    ) -> Optional[Checkpoint]:
        checkpoint_file = checkpoint_dir / f"{task_stem}.json"
        if not checkpoint_file.exists():
            return None
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Checkpoint.from_dict(data) if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _content_projection(checkpoint: Checkpoint) -> dict[str, Any]:
        payload = checkpoint.to_dict()
        payload.pop("id", None)
        payload.pop("timestamp", None)
        return payload

    @classmethod
    def _is_superseded(
        cls,
        checkpoint: Checkpoint,
        existing: Optional[Checkpoint],
    ) -> bool:
        """Prevent a delayed writer from replacing a newer/final projection."""

        if existing is None:
            return False
        if checkpoint.message_cursor < existing.message_cursor:
            return True
        if checkpoint.message_cursor > existing.message_cursor:
            return False
        if existing.step_name == "completed" and checkpoint.step_name != "completed":
            return True
        if checkpoint.step_name == "completed" and existing.step_name != "completed":
            return False
        new_priority = cls._milestone_priority.get(checkpoint.milestone, 0)
        existing_priority = cls._milestone_priority.get(existing.milestone, 0)
        return new_priority < existing_priority

    @classmethod
    def save(
        cls,
        checkpoint: Checkpoint,
        workdir: Optional[str] = None,
    ) -> str:
        """Save checkpoint to disk. Returns checkpoint ID."""
        with cls._task_guard(checkpoint.task_id, workdir) as (
            checkpoint_dir,
            task_stem,
        ):
            checkpoint_file = checkpoint_dir / f"{task_stem}.json"
            existing = cls._load_unlocked(checkpoint_dir, task_stem)
            if existing is not None and cls._content_projection(
                checkpoint
            ) == cls._content_projection(existing):
                return existing.id
            if cls._is_superseded(checkpoint, existing):
                return existing.id if existing is not None else checkpoint.id

            # Keep append-only checkpoint history for audit and recovery. The
            # task lock makes each JSONL record atomic across threads/processes.
            history_file = checkpoint_dir / f"{task_stem}_history.jsonl"
            try:
                with open(history_file, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(checkpoint.to_dict(), ensure_ascii=False) + "\n"
                    )
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    retention = max(
                        1,
                        int(os.environ.get("TCODE_CHECKPOINT_HISTORY_RETENTION", "50")),
                    )
                except ValueError:
                    retention = 50
                recent = deque(maxlen=retention)
                with history_file.open("r", encoding="utf-8") as handle:
                    recent.extend(line for line in handle if line.strip())
                temporary_history = history_file.with_suffix(".jsonl.tmp")
                with temporary_history.open("w", encoding="utf-8") as handle:
                    handle.writelines(recent)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_history, history_file)
            except Exception:
                pass

            # Replace the current projection atomically. The append-only
            # history remains the audit source if the process exits mid-write.
            payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{task_stem}.",
                suffix=".tmp",
                dir=str(checkpoint_dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary_name, checkpoint_file)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

            return checkpoint.id

    @classmethod
    def load(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
    ) -> Optional[Checkpoint]:
        """Load most recent checkpoint for a task."""
        checkpoint_dir = cls._checkpoint_dir(workdir)
        task_stem = cls._safe_task_stem(task_id)
        if not (checkpoint_dir / f"{task_stem}.json").exists():
            return None
        with cls._task_guard(task_id, workdir) as (checkpoint_dir, task_stem):
            return cls._load_unlocked(checkpoint_dir, task_stem)

    @classmethod
    def list_history(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
        limit: int = 10,
    ) -> list[Checkpoint]:
        """List checkpoint history for a task."""
        safe_limit = max(1, int(limit or 10))
        checkpoints = deque(maxlen=safe_limit)
        with cls._task_guard(task_id, workdir) as (checkpoint_dir, task_stem):
            history_file = checkpoint_dir / f"{task_stem}_history.jsonl"
            if not history_file.exists():
                return []
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            if isinstance(data, dict):
                                checkpoints.append(Checkpoint.from_dict(data))
                        except Exception:
                            pass
            except Exception:
                pass
        
        # Return newest first while respecting the requested bound.
        return list(reversed(checkpoints))

    @classmethod
    def delete(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
    ) -> None:
        """删除一个任务的当前 checkpoint 与历史记录。

        参数:
            task_id: 与会话或任务一致的 checkpoint 标识。
            workdir: checkpoint 根目录；为空时沿用运行时默认目录。
        """

        with cls._task_guard(task_id, workdir) as (checkpoint_dir, task_stem):
            checkpoint_file = checkpoint_dir / f"{task_stem}.json"
            history_file = checkpoint_dir / f"{task_stem}_history.jsonl"
            for path in (checkpoint_file, history_file):
                if path.exists():
                    path.unlink()

    @classmethod
    def should_resume_from_checkpoint(
        cls,
        checkpoint: Optional[Checkpoint],
        current_step: str,
    ) -> bool:
        """Decide if should resume from checkpoint."""
        if not checkpoint:
            return False
        
        # Don't resume if already completed
        if checkpoint.step_name == "completed":
            return False
        
        # Resume if checkpoint is newer than current step
        # or if we're retrying from an earlier step
        step_order = ["init", "retrieve", "execute", "review", "finalize", "completed"]
        checkpoint_idx = (
            step_order.index(checkpoint.step_name)
            if checkpoint.step_name in step_order
            else 0
        )
        current_idx = step_order.index(current_step) if current_step in step_order else 0
        
        return checkpoint_idx > current_idx or current_step == "init"
