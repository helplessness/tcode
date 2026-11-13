"""Task Manager - Track and manage background tasks"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime

from .state_graph import TaskStateGraphManager


@dataclass
class TaskInfo:
    """Information about a background task"""

    task_id: str
    description: str
    prompt: str
    agent_name: str
    status: str  # "running", "completed", "failed"
    result: str = ""
    workdir: Optional[str] = None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    parent_session_id: Optional[str] = None


class TaskManager:
    """Manages background tasks"""

    _tasks: dict[str, TaskInfo] = {}
    _lock = asyncio.Lock()
    _loaded: bool = False

    @classmethod
    def _storage_path(cls) -> str:
        base = os.environ.get("TCODE_WORKDIR", os.getcwd())
        return os.path.join(base, ".tcode", "tasks.json")

    @classmethod
    def _ensure_storage(cls):
        path = cls._storage_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    @classmethod
    def _load_from_disk(cls):
        if cls._loaded:
            return

        cls._ensure_storage()
        path = cls._storage_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}

        cls._tasks = {}
        if isinstance(raw, dict):
            for task_id, item in raw.items():
                if isinstance(item, dict):
                    cls._tasks[task_id] = TaskInfo(
                        task_id=str(item.get("task_id", task_id)),
                        description=str(item.get("description", "")),
                        prompt=str(item.get("prompt", "")),
                        agent_name=str(item.get("agent_name", "general")),
                        status=str(item.get("status", "pending")),
                        result=str(item.get("result", "")),
                        workdir=item.get("workdir"),
                        created_at=float(
                            item.get("created_at", datetime.now().timestamp())
                        ),
                        started_at=item.get("started_at"),
                        completed_at=item.get("completed_at"),
                        parent_session_id=item.get("parent_session_id"),
                    )
        cls._loaded = True

    @classmethod
    def _save_to_disk(cls):
        cls._ensure_storage()
        path = cls._storage_path()
        payload = {
            task_id: {
                "task_id": info.task_id,
                "description": info.description,
                "prompt": info.prompt,
                "agent_name": info.agent_name,
                "status": info.status,
                "result": info.result,
                "workdir": info.workdir,
                "created_at": info.created_at,
                "started_at": info.started_at,
                "completed_at": info.completed_at,
                "parent_session_id": info.parent_session_id,
            }
            for task_id, info in cls._tasks.items()
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    async def create(
        cls,
        description: str,
        prompt: str,
        agent_name: str,
        workdir: Optional[str] = None,
        parent_session_id: Optional[str] = None,
    ) -> str:
        """Create a new task"""
        async with cls._lock:
            cls._load_from_disk()
            task_id = str(uuid.uuid4())
            cls._tasks[task_id] = TaskInfo(
                task_id=task_id,
                description=description,
                prompt=prompt,
                agent_name=agent_name,
                status="pending",
                workdir=workdir,
                parent_session_id=parent_session_id,
            )
            cls._save_to_disk()
            TaskStateGraphManager.ensure_graph(task_id, description or prompt[:200])
            return task_id

    @classmethod
    async def start(cls, task_id: str):
        """Mark task as started"""
        async with cls._lock:
            cls._load_from_disk()
            if task_id in cls._tasks:
                cls._tasks[task_id].status = "running"
                cls._tasks[task_id].started_at = datetime.now().timestamp()
                cls._save_to_disk()
                TaskStateGraphManager.set_next_actions(task_id, ["任务运行中"], [])

    @classmethod
    async def complete(cls, task_id: str, result: str):
        """Mark task as completed"""
        async with cls._lock:
            cls._load_from_disk()
            if task_id in cls._tasks:
                cls._tasks[task_id].status = "completed"
                cls._tasks[task_id].result = result
                cls._tasks[task_id].completed_at = datetime.now().timestamp()
                cls._save_to_disk()
                TaskStateGraphManager.set_next_actions(task_id, ["任务已完成"], [])

    @classmethod
    async def fail(cls, task_id: str, error: str):
        """Mark task as failed"""
        async with cls._lock:
            cls._load_from_disk()
            if task_id in cls._tasks:
                cls._tasks[task_id].status = "failed"
                cls._tasks[task_id].result = f"Error: {error}"
                cls._tasks[task_id].completed_at = datetime.now().timestamp()
                cls._save_to_disk()
                TaskStateGraphManager.set_next_actions(task_id, ["排查失败原因并决定是否重试"], [error])

    @classmethod
    def get(cls, task_id: str) -> Optional[TaskInfo]:
        """Get task info"""
        cls._load_from_disk()
        return cls._tasks.get(task_id)

    @classmethod
    def list(cls, status: Optional[str] = None) -> list[TaskInfo]:
        """List tasks, optionally filtered by status"""
        cls._load_from_disk()
        tasks = list(cls._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda x: x.created_at, reverse=True)

    @classmethod
    def running(cls) -> "list[TaskInfo]":
        """Get all running tasks"""
        return cls.list("running")

    @classmethod
    def completed(cls) -> "list[TaskInfo]":
        """Get all completed tasks"""
        return cls.list("completed")

    @classmethod
    def status_summary(cls) -> str:
        """Get a summary of all tasks"""
        running = cls.running()
        completed = cls.completed()
        failed = cls.list("failed")

        lines = []

        if running:
            lines.append("🔄 Running Tasks:")
            for t in running:
                duration = ""
                if t.started_at:
                    duration = f" ({int(datetime.now().timestamp() - t.started_at)}s)"
                lines.append(
                    f"  • {t.task_id[:8]}... - {t.description} [{t.agent_name}]{duration}"
                )

        if completed:
            lines.append("✅ Completed Tasks:")
            for t in completed[:5]:
                lines.append(
                    f"  • {t.task_id[:8]}... - {t.description} [{t.agent_name}]"
                )
            if len(completed) > 5:
                lines.append(f"  ... and {len(completed) - 5} more")

        if failed:
            lines.append("❌ Failed Tasks:")
            for t in failed[:5]:
                lines.append(
                    f"  • {t.task_id[:8]}... - {t.description} [{t.agent_name}]"
                )

        if not lines:
            return "No background tasks"

        return "\n".join(lines)

    @classmethod
    def clear(cls):
        """Clear all tasks"""
        cls._load_from_disk()
        cls._tasks.clear()
        cls._save_to_disk()
