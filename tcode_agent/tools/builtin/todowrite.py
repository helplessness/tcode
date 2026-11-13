"""Todo state management for the unified todo tool."""

from enum import Enum
from typing import Optional
from datetime import datetime

import pydantic

from ..base import ToolContext, ParameterSchema
from ...session.manager import SessionManager
from ...task.state_graph import TaskStateGraphManager
from ...task.plan_freshness import update_task_fingerprint


class TodoStatus(str, Enum):
    """Todo status"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoItem(pydantic.BaseModel):
    """A single todo item"""

    content: str
    status: TodoStatus = TodoStatus.PENDING


class TodoWriteParameters(ParameterSchema):
    """Parameters for todo tool"""

    todos: list[TodoItem]


class TodoManager:
    """Manages todo lists per session"""

    _todos: dict[str, list[dict]] = {}

    @staticmethod
    def _normalize(todos: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for item in todos or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            status = str(item.get("status", TodoStatus.PENDING.value)).strip().lower()
            if status not in {
                TodoStatus.PENDING.value,
                TodoStatus.IN_PROGRESS.value,
                TodoStatus.COMPLETED.value,
                TodoStatus.CANCELLED.value,
            }:
                status = TodoStatus.PENDING.value
            normalized.append({"content": content, "status": status})
        return normalized

    @classmethod
    def set_todos(cls, session_id: str, todos: list[dict]):
        cls._todos[session_id] = cls._normalize(todos)

    @classmethod
    def get_todos(cls, session_id: str, workdir: Optional[str] = None) -> list[dict]:
        cached = cls._todos.get(session_id)
        if cached is not None:
            return [dict(item) for item in cached]

        sessions = SessionManager.get_all_sync()
        session = sessions.get(session_id)
        metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
        stored = metadata.get("todos")
        if isinstance(stored, list):
            normalized = cls._normalize(stored)
            cls._todos[session_id] = normalized
            return [dict(item) for item in normalized]
        return []

    @classmethod
    def clear(cls, session_id: str):
        if session_id in cls._todos:
            del cls._todos[session_id]

    @classmethod
    async def persist(
        cls,
        session_id: str,
        todos: list[dict],
        *,
        goal: str = "",
        owner: str = "planner",
    ) -> list[dict]:
        normalized = cls._normalize(todos)
        cls._todos[session_id] = normalized

        session = await SessionManager.get(session_id)
        metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
        metadata["todos"] = normalized
        metadata["todos_owner"] = str(owner or "planner").strip() or "planner"

        graph = TaskStateGraphManager.get(session_id)
        goal_text = (
            str(goal or "").strip()
            or str(metadata.get("task_goal", "") or "").strip()
            or str(getattr(session, "title", "") or "").strip()
            or str(getattr(graph, "goal", "") or "").strip()
            or "Current task plan"
        )
        metadata["task_goal"] = goal_text
        metadata, _fingerprint = update_task_fingerprint(
            metadata,
            goal=goal_text,
            todos=normalized,
        )

        if session:
            await SessionManager.update(
                session_id,
                metadata=metadata,
                last_active_at=datetime.now().timestamp(),
            )

        TaskStateGraphManager.sync_from_todos(
            session_id,
            goal=goal_text,
            todos=normalized,
            owner=owner or "planner",
        )
        return normalized

    @classmethod
    async def ensure_graph(
        cls,
        session_id: str,
        *,
        goal: str = "",
        owner: str = "planner",
        workdir: Optional[str] = None,
    ) -> None:
        todos = cls.get_todos(session_id, workdir=workdir)
        await cls.persist(session_id, todos, goal=goal, owner=owner)


def _infer_goal(context: ToolContext) -> str:
    for msg in reversed(context.messages or []):
        if getattr(msg, "role", "") == "user":
            text = str(getattr(msg, "content", "") or "").strip()
            if text:
                return text

    session = SessionManager.get_all_sync().get(context.session_id)
    if session:
        metadata = dict(getattr(session, "metadata", {}) or {})
        for candidate in (
            metadata.get("task_goal"),
            getattr(session, "title", ""),
        ):
            text = str(candidate or "").strip()
            if text:
                return text
    return "Current task plan"
