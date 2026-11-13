from __future__ import annotations

import json
import os
from typing import Any

from pydantic import Field

from ...scheduled_tasks import (
    ScheduleValidationError,
    compute_next_run,
    get_scheduled_task_service,
    get_scheduled_task_store,
    normalize_task_spec,
)
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class ScheduledTaskParameters(ParameterSchema):
    action: str
    task_id: str | None = None
    confirmation_token: str | None = None
    confirmed: bool = False
    name: str | None = None
    instruction: str | None = None
    schedule_type: str | None = None
    schedule: dict[str, Any] | None = None
    timezone: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


def _owner(context: ToolContext) -> tuple[str, str, str]:
    extra = dict(context.extra or {})
    tenant_id = str(
        extra.get("tenant_id")
        or os.environ.get("TCODE_DEFAULT_TENANT_ID")
        or "admin_tenant"
    ).strip()
    user_id = str(extra.get("user_id") or "local-user").strip()
    workspace_id = str(extra.get("workspace_id") or "workspace-1").strip()
    return tenant_id, user_id, workspace_id


def _task_line(task: dict[str, Any]) -> str:
    return (
        f"- {task.get('name')} (id={task.get('id')}, status={task.get('status')}, "
        f"type={task.get('schedule_type')}, next_run_at={task.get('next_run_at_local') or task.get('next_run_at')})"
    )


class ScheduledTaskTool(Tool):
    @property
    def id(self) -> str:
        return "scheduled_task"

    @property
    def description(self) -> str:
        return """Create and manage persistent scheduled agent tasks.

Use this tool when the user asks for a future, delayed, or recurring action. It supports one-time `at`, relative `delay`, `daily`, `weekly`, `monthly`, `weekdays`, and 5/6-field `cron` schedules.

Creation is deliberately two-step:
1. Resolve every ambiguity first. Ask the user when the time, timezone, recurrence, deliverable, or required execution details are unclear. A date without a clock defaults to 09:00 in the user's timezone; the default timezone is Asia/Shanghai.
2. Call action=`prepare` only after details are complete. Show the returned preview and ask the user to confirm. Do not call action=`confirm` until the user explicitly confirms; then pass the exact confirmation_token.

For modifying an existing task, first describe the exact proposed change and ask the user to confirm. Call action=`update` with confirmed=true only after explicit confirmation.

Other actions: `list`, `get`, `history`, `pause`, `resume`, `run`, `retry`, and `delete`. Never schedule unsupported, dangerous, or authorization-dependent work. A scheduled run can only use capabilities already available to the current project."""

    @property
    def parameters(self) -> type:
        return ScheduledTaskParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ScheduledTaskParameters(**args)
        action = str(parsed.action or "").strip().lower()
        tenant_id, user_id, workspace_id = _owner(context)
        store = get_scheduled_task_store()

        try:
            if action == "prepare":
                timezone_name = str(parsed.timezone or "").strip()
                if not timezone_name:
                    timezone_name = str(
                        store.get_preferences(tenant_id=tenant_id, user_id=user_id).get(
                            "timezone"
                        )
                        or "Asia/Shanghai"
                    )
                spec = normalize_task_spec(
                    {
                        "name": parsed.name,
                        "instruction": parsed.instruction,
                        "schedule_type": parsed.schedule_type,
                        "schedule": parsed.schedule or {},
                        "timezone": timezone_name,
                        "source_session_id": context.session_id,
                        "agent_name": context.agent or "build",
                        "workdir": str((context.extra or {}).get("workdir") or ""),
                        "created_via": "natural_language",
                    }
                )
                draft = store.create_draft(
                    spec,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                preview = dict(spec)
                output = (
                    "任务尚未创建，请向用户展示以下预览并等待明确确认：\n"
                    f"- 名称：{spec['name']}\n"
                    f"- 指令：{spec['instruction']}\n"
                    f"- 类型：{spec['schedule_type']}\n"
                    f"- 规则：{json.dumps(spec['schedule'], ensure_ascii=False)}\n"
                    f"- 时区：{spec['timezone']}\n"
                    f"- 首次执行时间戳：{spec['next_run_at']}\n"
                    f"确认令牌：{draft['token']}"
                )
                return ToolResult(
                    title="Scheduled Task Preview",
                    output=output,
                    metadata={
                        "requires_confirmation": True,
                        "confirmation_token": draft["token"],
                        "confirmation_expires_at": draft["expires_at"],
                        "preview": preview,
                    },
                )

            if action == "confirm":
                if not parsed.confirmation_token:
                    raise ScheduleValidationError("confirm 操作缺少 confirmation_token")
                task = store.confirm_draft(
                    parsed.confirmation_token,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                return ToolResult(
                    title="Scheduled Task Created",
                    output=f"定时任务已创建。\n{_task_line(task)}",
                    metadata={"task": task, "created": True},
                )

            if action == "list":
                tasks = store.list_tasks(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    limit=parsed.limit,
                )
                output = "当前没有定时任务。" if not tasks else "定时任务：\n" + "\n".join(
                    _task_line(task) for task in tasks
                )
                return ToolResult(
                    title="Scheduled Tasks",
                    output=output,
                    metadata={"tasks": tasks, "count": len(tasks)},
                )

            if not parsed.task_id:
                raise ScheduleValidationError(f"{action} 操作缺少 task_id")
            task = store.get_task(
                parsed.task_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if task is None:
                raise KeyError("任务不存在")

            if action == "get":
                return ToolResult(
                    title="Scheduled Task",
                    output=json.dumps(task, ensure_ascii=False, indent=2),
                    metadata={"task": task},
                )
            if action == "history":
                runs = store.list_runs(
                    parsed.task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    limit=parsed.limit,
                )
                return ToolResult(
                    title="Scheduled Task History",
                    output=json.dumps(runs, ensure_ascii=False, indent=2),
                    metadata={"runs": runs, "count": len(runs)},
                )
            if action == "pause":
                updated = store.set_status(
                    parsed.task_id,
                    "paused",
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                return ToolResult("Scheduled Task Paused", _task_line(updated), {"task": updated})
            if action == "resume":
                next_run = compute_next_run(
                    str(task["schedule_type"]),
                    dict(task["schedule"]),
                    str(task["timezone"]),
                )
                updated = store.set_status(
                    parsed.task_id,
                    "active",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    next_run_at=next_run,
                )
                return ToolResult("Scheduled Task Resumed", _task_line(updated), {"task": updated})
            if action == "delete":
                store.delete_task(
                    parsed.task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                return ToolResult(
                    title="Scheduled Task Deleted",
                    output=f"定时任务 {parsed.task_id} 已删除。",
                    metadata={"task_id": parsed.task_id, "deleted": True},
                )
            if action in {"run", "retry"}:
                run_id = await get_scheduled_task_service().dispatch_now(
                    parsed.task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    trigger="retry" if action == "retry" else "manual",
                )
                return ToolResult(
                    title="Scheduled Task Run Accepted",
                    output=f"任务已进入执行队列，run_id={run_id}",
                    metadata={"task_id": parsed.task_id, "run_id": run_id, "accepted": True},
                )
            if action == "update":
                if not parsed.confirmed:
                    raise ScheduleValidationError("修改任务前必须获得用户明确确认并设置 confirmed=true")
                combined = {
                    "name": parsed.name if parsed.name is not None else task["name"],
                    "instruction": parsed.instruction if parsed.instruction is not None else task["instruction"],
                    "schedule_type": parsed.schedule_type if parsed.schedule_type is not None else task["schedule_type"],
                    "schedule": parsed.schedule if parsed.schedule is not None else task["schedule"],
                    "timezone": parsed.timezone or task["timezone"],
                    "source_session_id": task.get("source_session_id"),
                    "agent_name": task.get("agent_name"),
                    "workdir": task.get("workdir"),
                    "created_via": task.get("created_via"),
                }
                spec = normalize_task_spec(combined)
                updated = store.update_task(
                    parsed.task_id,
                    spec,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                return ToolResult(
                    title="Scheduled Task Updated",
                    output=f"定时任务已更新。\n{_task_line(updated)}",
                    metadata={"task": updated, "updated": True},
                )
            raise ScheduleValidationError(f"不支持的 scheduled_task action：{action}")
        except Exception as exc:
            return ToolResult(
                title="Scheduled Task",
                output="",
                error=str(exc).strip("'") or "定时任务操作失败",
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
