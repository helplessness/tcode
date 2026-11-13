"""Tool: create_worker - create a new Worker Agent in the current team"""

import uuid
from typing import Any

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..registry import WorkerRegistry
from ..channel import TeamChannel
from ..worker_scoper import WorkerScoper
from ...session.manager import SessionManager
from ...agent.agents import BuiltinAgents


class CreateWorkerParameters(ParameterSchema):
    name: str
    role: str
    permission_preset: str = "developer"
    skill_tags: str = ""
    peer_whitelist: str = ""


class CreateWorkerTool(Tool):
    @property
    def id(self) -> str:
        return "create_worker"

    @property
    def description(self) -> str:
        return """创建一个专职 Worker Agent，加入当前 Manager 团队。

参数：
- name: Worker 名称（英文小写，如 alice/bob/frontend）
- role: 角色描述（如 "前端工程师"、"测试工程师"、"数据分析师"）
- permission_preset: 权限级别
  - "readonly"：只能读取文件，不能修改（适合分析/调研 Worker）
  - "developer"（默认）：可以读写文件和执行命令
  - "full"：完整权限
- skill_tags: 可选，逗号分隔的能力标签（如 "frontend,react,test"）
- peer_whitelist: 可选，逗号分隔。允许该 Worker 正式联系的其他 Worker 名称

创建后使用 assign_task 给 Worker 分配任务，list_workers 查看所有 Worker 状态。"""

    @property
    def parameters(self) -> type:
        return CreateWorkerParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = CreateWorkerParameters(**args)
        name = parsed.name.strip().lower().replace(" ", "_").replace("-", "_")
        role = parsed.role.strip()
        preset = parsed.permission_preset.strip().lower()
        skill_tags = [tag.strip() for tag in (parsed.skill_tags or "").split(",") if tag.strip()]
        peer_whitelist = [
            tag.strip().lower()
            for tag in (parsed.peer_whitelist or "").split(",")
            if tag.strip()
        ]
        if preset not in WorkerScoper.VALID_PRESETS:
            preset = "developer"

        team_id = context.session_id
        workdir = (context.extra or {}).get("workdir", "") or ""
        extra = context.extra if isinstance(context.extra, dict) else {}
        tenant_id = str(extra.get("tenant_id") or "").strip()
        user_id = str(extra.get("user_id") or "").strip()
        workspace_id = str(extra.get("workspace_id") or "").strip()
        if not tenant_id or not user_id or not workspace_id:
            try:
                parent_session = await SessionManager.get(team_id)
            except Exception:
                parent_session = None
            if parent_session is not None:
                tenant_id = tenant_id or str(getattr(parent_session, "tenant_id", "") or "").strip()
                user_id = user_id or str(getattr(parent_session, "user_id", "") or "").strip()
                workspace_id = workspace_id or str(getattr(parent_session, "workspace_id", "") or "").strip()
        owner_kwargs = {
            key: value
            for key, value in {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
            }.items()
            if value
        }

        # Check for duplicate
        if WorkerRegistry.get_by_name(team_id, name):
            return ToolResult(
                title=f"create_worker: {name}",
                output=f"⚠️ Worker **{name}** 已在团队中，无需重复创建。使用 list_workers 查看当前状态。",
            )

        # Create a backing session as child of Manager session
        worker_session_id = f"worker-{team_id[:8]}-{name}-{uuid.uuid4().hex[:8]}"
        await SessionManager.create(
            session_id=worker_session_id,
            parent_id=team_id,
            agent_name=f"worker_{name}",
            title=f"Worker: {name} ({role})",
            workdir=workdir or None,
            metadata={
                "team_worker_registration": {
                    "name": name,
                    "role": role,
                    "permission_preset": preset,
                    "deletable": True,
                    "skill_tags": skill_tags,
                    "peer_whitelist": peer_whitelist,
                }
            },
            **owner_kwargs,
        )

        worker = await WorkerRegistry.create_worker(
            team_id=team_id,
            name=name,
            role=role,
            session_id=worker_session_id,
            permission_preset=preset,
            skill_tags=skill_tags,
            peer_whitelist=peer_whitelist,
        )
        if worker.session_id != worker_session_id:
            # Another process won the same (team_id, name) creation race.
            # Remove only the redundant session created by this attempt and use
            # the durable winner instead of leaving an invisible orphan branch.
            await SessionManager.delete(worker_session_id, **owner_kwargs)
            return ToolResult(
                title=f"create_worker: {name}",
                output=(
                    f"⚠️ Worker **{name}** 已由并发请求创建；已复用现有 Worker。\n\n"
                    f"- Session ID：{worker.session_id}\n"
                    f"- Team ID：{worker.team_id}\n"
                    "现在可以直接使用 assign_task 分派任务。"
                ),
                metadata={
                    "worker_session_id": worker.session_id,
                    "team_id": worker.team_id,
                    "deduplicated": True,
                },
            )

        # Pre-register the worker's AgentInfo so it can be looked up
        scoper_info = WorkerScoper.build_worker_agent_info(
            name,
            role,
            preset,
            peer_whitelist=peer_whitelist,
        )
        BuiltinAgents.register_custom(scoper_info, source="team")

        # Write to TeamChannel
        if workdir:
            channel = TeamChannel.get_or_create(team_id, workdir)
            await channel.publish(
                source="manager",
                target="all",
                event_type="worker_created",
                content=f"Worker **{name}** ({role}) 加入团队",
                metadata={
                    "worker_id": worker.worker_id,
                    "permission_preset": preset,
                    "session_id": worker_session_id,
                },
            )

        return ToolResult(
            title=f"create_worker: {name}",
            output=f"""✅ Worker **{name}** 创建成功！

- 角色：{role}
- 权限：{preset}
- peer whitelist：{", ".join(peer_whitelist) if peer_whitelist else "（未配置）"}
- Session ID：{worker_session_id}

现在可以用 `assign_task` 给 **{name}** 分派任务了。""",
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
