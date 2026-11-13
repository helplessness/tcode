"""Tool: terminate_worker - gracefully stop a Worker Agent"""

from typing import Any

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..registry import WorkerRegistry
from ..channel import TeamChannel


class TerminateWorkerParameters(ParameterSchema):
    worker_name: str


class TerminateWorkerTool(Tool):
    @property
    def id(self) -> str:
        return "terminate_worker"

    @property
    def description(self) -> str:
        return "终止一个 Worker Agent，标记为已终止状态。终止后的 Worker 不再接受新任务（但历史记录保留）。"

    @property
    def parameters(self) -> type:
        return TerminateWorkerParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TerminateWorkerParameters(**args)
        team_id = context.session_id
        workdir = (context.extra or {}).get("workdir", "") or ""
        name = parsed.worker_name.strip().lower()

        worker = WorkerRegistry.get_by_name(team_id, name)
        if not worker:
            return ToolResult(
                title="terminate_worker",
                output=f"❌ Worker '{name}' 不存在",
            )
        if not worker.deletable:
            return ToolResult(
                title="terminate_worker",
                output=f"❌ Worker '{name}' 是团队固定成员，不允许终止。",
            )
        if worker.status == "terminated":
            return ToolResult(
                title="terminate_worker",
                output=f"⚠️ Worker '{name}' 已经处于终止状态",
            )

        await WorkerRegistry.update(worker.session_id, status="terminated", current_task=None)

        if workdir:
            channel = TeamChannel.get_or_create(team_id, workdir)
            await channel.publish(
                source="manager",
                target="all",
                event_type="worker_terminated",
                content=f"Worker **{name}** 已终止",
                metadata={"worker_id": worker.worker_id, "session_id": worker.session_id},
            )

        return ToolResult(
            title="terminate_worker",
            output=f"✅ Worker **{name}** 已终止。",
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
