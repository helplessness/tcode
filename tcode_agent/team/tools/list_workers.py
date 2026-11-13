"""Tool: list_workers - show all Workers in the current team"""

from typing import Any

from ...session.manager import SessionManager
from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..registry import WorkerRegistry
from ..profile import worker_profile_summary
from ..catalog import TeamCatalog


class ListWorkersParameters(ParameterSchema):
    pass


class ListWorkersTool(Tool):
    @property
    def id(self) -> str:
        return "list_workers"

    @property
    def description(self) -> str:
        return "列出当前团队中所有 Worker 的状态、角色和最近任务。"

    @property
    def parameters(self) -> type:
        return ListWorkersParameters

    @staticmethod
    def _status_icon(status: str) -> str:
        return {"idle": "✅", "busy": "⏳", "terminated": "💀"}.get(status, "❓")

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        team_id = context.session_id
        team_entry = TeamCatalog.get_by_agent_name(context.agent)
        extra = context.extra if isinstance(context.extra, dict) else {}
        owner_kwargs = {
            key: str(extra.get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
            if str(extra.get(key) or "").strip()
        }
        await WorkerRegistry.reconcile_team_sessions(
            team_id,
            owner_kwargs=owner_kwargs,
        )

        def _is_visible_worker(worker: Any) -> bool:
            catalog_worker = TeamCatalog.get_worker_by_session(
                str(getattr(worker, "session_id", "") or ""),
                str(getattr(worker, "name", "") or ""),
            )
            if catalog_worker is not None:
                return bool(catalog_worker.enabled and catalog_worker.visible)
            if team_entry is not None:
                return TeamCatalog.worker_is_visible(team_entry.key, getattr(worker, "name", ""))
            return True

        workers = [
            worker
            for worker in WorkerRegistry.list_for_team(team_id)
            if _is_visible_worker(worker)
        ]

        # 团队 Worker 可能属于任意下级编排会话；统一投影整棵受控会话树。
        child_sessions: list[Any] = []
        queue = list(await SessionManager.list(parent_id=team_id, **owner_kwargs))
        seen_sessions: set[str] = set()
        while queue:
            child = queue.pop(0)
            child_id = str(getattr(child, "id", "") or "").strip()
            if not child_id or child_id in seen_sessions:
                continue
            seen_sessions.add(child_id)
            child_sessions.append(child)
            queue.extend(
                await SessionManager.list(parent_id=child_id, **owner_kwargs)
            )

        team_children: list[tuple[Any, list[Any]]] = []
        for child in child_sessions:
            child_id = str(getattr(child, "id", "") or "").strip()
            await WorkerRegistry.reconcile_team_sessions(
                child_id,
                owner_kwargs=owner_kwargs,
            )
            child_workers = [
                worker
                for worker in WorkerRegistry.list_for_team(child_id)
                if _is_visible_worker(worker)
            ]
            if child_workers:
                team_children.append((child, child_workers))

        if not workers and not team_children:
            return ToolResult(
                title="list_workers",
                output=(
                    "当前受控会话树中没有 Worker。"
                    "\n- 固定团队会在团队会话启动时自动创建 Worker。"
                    "\n- 普通 manager 团队可以使用 create_worker 创建 Worker。"
                ),
            )

        lines = ["**受控会话树 Workers 状态：**", ""]

        def append_worker(lines: list[str], w: Any, *, indent: str = "") -> None:
            profile = worker_profile_summary(w)
            icon = self._status_icon(w.status)
            lines.append(f"{indent}{icon} **{w.name}** — {w.role}")
            lines.append(f"{indent}   状态: `{w.status}` | 权限: `{w.permission_preset}`")
            lines.append(f"{indent}   Session ID: `{w.session_id}` | Team ID: `{w.team_id}`")
            lines.append(
                f"{indent}   成功率: `{profile['success_rate']}` | 平均耗时: `{round(float(profile['avg_latency_sec'] or 0.0), 1)}s` | 任务数: `{w.task_count}`"
            )
            if w.skill_tags:
                lines.append(f"{indent}   能力标签: {', '.join(w.skill_tags[:8])}")
            if w.common_tools:
                lines.append(f"{indent}   常用工具: {', '.join(w.common_tools[:6])}")
            if w.peer_whitelist:
                lines.append(f"{indent}   Peer 白名单: {', '.join(w.peer_whitelist[:8])}")
            if w.current_task:
                snippet = w.current_task[:120]
                lines.append(f"{indent}   当前任务: {snippet}{'...' if len(w.current_task) > 120 else ''}")
            if w.result_summary:
                snippet = w.result_summary[:120]
                lines.append(f"{indent}   最近结果: {snippet}{'...' if len(w.result_summary) > 120 else ''}")
            if w.recent_failure_reason:
                lines.append(f"{indent}   最近失败原因: {w.recent_failure_reason[:120]}")
            lines.append("")

        if workers:
            lines.append(f"- 当前团队 `{team_id}` | workers: {len(workers)}")
            for worker in workers:
                append_worker(lines, worker, indent="  ")

        for child, child_workers in team_children:
            child_agent = str(getattr(child, "agent_name", "") or "")
            lines.append(
                f"- 下级团队 `{getattr(child, 'id', '')}` | agent: `{child_agent}` | workers: {len(child_workers)}"
            )
            for worker in child_workers:
                append_worker(lines, worker, indent="  ")

        return ToolResult(title="list_workers", output="\n".join(lines))

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
