"""Tool: task_graph_read - inspect current team task graph."""

from typing import Any

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...task.state_graph import TaskStateGraphManager


class TaskGraphReadParameters(ParameterSchema):
    pass


class TaskGraphReadTool(Tool):
    @property
    def id(self) -> str:
        return "task_graph_read"

    @property
    def description(self) -> str:
        return "查看当前会话的显式任务状态图，包括子任务状态、证据和下一步动作。"

    @property
    def parameters(self) -> type:
        return TaskGraphReadParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        graph = TaskStateGraphManager.get(context.session_id)
        if not graph:
            return ToolResult(title="task_graph_read", output="当前没有任务状态图。")

        lines = [
            f"目标: {graph.goal or '（未设置）'}",
            f"整体状态: {graph.status}",
        ]
        if graph.current_blockers:
            lines.append(f"阻塞: {', '.join(graph.current_blockers)}")
        if graph.next_actions:
            lines.append(f"下一步: {', '.join(graph.next_actions)}")
        lines.append("")
        lines.append("子任务:")
        for node in graph.subtasks:
            lines.append(f"- {node.id} | {node.owner} | {node.status} | {node.title}")
            if node.depends_on:
                lines.append(f"  depends_on: {', '.join(node.depends_on)}")
            if node.evidence:
                lines.append(f"  evidence: {', '.join(node.evidence)}")
            if node.next_action:
                lines.append(f"  next: {node.next_action}")

        return ToolResult(title="task_graph_read", output="\n".join(lines))

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
