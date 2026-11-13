"""Unified todo tool - read/write todos with one entrypoint."""

from typing import Any

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from .todowrite import TodoItem, TodoWriteParameters, TodoManager, _infer_goal


class TodoParameters(ParameterSchema):
    action: str
    todos: list[TodoItem] | None = None


class TodoTool(Tool):
    @property
    def id(self) -> str:
        return "todo"

    @property
    def description(self) -> str:
        return """Manage session todo list with one tool.
Actions:
- read: read current todo list
- write: replace todo list with provided todos"""

    @property
    def parameters(self) -> type:
        return TodoParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TodoParameters(**args)
        action = str(parsed.action or "").strip().lower()

        if action == "read":
            if not await context.ask(permission="todo_read", patterns=["*"]):
                return ToolResult(
                    title="Todo List",
                    output="",
                    error="Permission denied for todo_read",
                )
            todos = TodoManager.get_todos(
                context.session_id,
                workdir=(context.extra or {}).get("workdir", "") or None,
            )
            if not todos:
                return ToolResult(
                    title="Todo List",
                    output="No todos found.",
                    metadata={"todos": [], "count": 0},
                )
            lines = ["Todo List:"]
            for i, todo in enumerate(todos, 1):
                status_icon = {
                    "pending": "PENDING",
                    "in_progress": "IN_PROGRESS",
                    "completed": "COMPLETED",
                    "cancelled": "CANCELLED",
                }.get(todo.get("status", "pending"), "PENDING")
                lines.append(f"  {i}. [{status_icon}] {todo.get('content', '')}")
            return ToolResult(
                title="Todo List",
                output="\n".join(lines),
                metadata={"todos": todos, "count": len(todos)},
            )

        if action == "write":
            if not await context.ask(permission="todo_write", patterns=["*"]):
                return ToolResult(
                    title="Todo List Updated",
                    output="",
                    error="Permission denied for todo_write",
                )
            if parsed.todos is None:
                return ToolResult(
                    title="Todo List Updated",
                    output="",
                    error="todos is required when action=write",
                )
            write_args = TodoWriteParameters(todos=parsed.todos)
            todos = [{"content": t.content, "status": t.status.value} for t in write_args.todos]
            todos = await TodoManager.persist(
                context.session_id,
                todos,
                goal=_infer_goal(context),
                owner=context.agent,
            )
            lines = ["Todo List:"]
            for i, todo in enumerate(todos, 1):
                lines.append(f"  {i}. [{todo['status']}] {todo['content']}")
            return ToolResult(
                title="Todo List Updated",
                output="\n".join(lines),
                metadata={"todos": todos, "count": len(todos)},
            )

        return ToolResult(
            title="Todo",
            output="",
            error=f"Unsupported action: {parsed.action}",
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
