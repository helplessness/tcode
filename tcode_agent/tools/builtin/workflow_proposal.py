"""Tool for preparing safe Level 3 workflow proposals."""

from __future__ import annotations

import json
from typing import Any

from ...types import ToolResult
from ...workflow.proposal import build_workflow_proposal
from ...workflow.discovery import discover_workflow
from ...workflow.router import detect_workflow_route
from ..base import ParameterSchema, Tool, ToolContext


class WorkflowProposalParameters(ParameterSchema):
    message: str | None = None
    composite_plan: Any = None
    include_disabled: bool = False


class WorkflowProposalTool(Tool):
    @property
    def id(self) -> str:
        return "workflow_proposal"

    @property
    def description(self) -> str:
        return """Prepare a safe Level 3 workflow proposal from the current composite skill plan.

Use this for multi-step requests in any domain. It discovers capabilities from the registered catalog, without a domain/keyword whitelist. When no fixed template fits, it proposes a temporary DAG that the user must generate, edit and confirm. This tool never creates or resumes a run. Call workflow_run only after the user explicitly confirms a fixed proposal."""

    @property
    def parameters(self) -> type:
        return WorkflowProposalParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WorkflowProposalParameters(**args)
        message = str(parsed.message or "").strip() or _latest_user_message(
            context.messages
        )
        extra = dict(context.extra or {})
        discovery = None
        if not detect_workflow_route(message).should_start:
            discovery = await discover_workflow(message, provider=extra.get("provider"))
        composite_plan = (
            dict(parsed.composite_plan)
            if isinstance(parsed.composite_plan, dict)
            else dict(extra.get("skill_composite_plan") or {})
        )
        proposal = build_workflow_proposal(
            message,
            composite_plan=composite_plan,
            resource_context=dict(extra.get("resource_context") or {}),
            requested_agent_name=str(context.agent or ""),
            workdir=str(extra.get("workdir") or "").strip() or None,
            include_disabled=bool(parsed.include_disabled),
            discovery=discovery,
        )
        payload = proposal.to_dict()
        return ToolResult(
            title="Workflow Proposal",
            output=_format_workflow_proposal(payload),
            metadata={"workflow_proposal": payload, "discovery": discovery.to_dict() if discovery else None},
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


def _latest_user_message(messages: list[Any]) -> str:
    for msg in reversed(messages or []):
        role = str(getattr(msg, "role", "") or "").strip()
        content = str(getattr(msg, "content", "") or "").strip()
        if not role and isinstance(msg, dict):
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "").strip()
        if role == "user" and content:
            return content
    return ""


def _format_workflow_proposal(payload: dict[str, Any]) -> str:
    if payload.get("decision") == "temporary_workflow_proposal":
        return "\n".join([
            "没有找到适合的固定 Workflow 模板。",
            "可以生成一张临时 DAG，用户编辑并确认后再运行。",
            f"原因：{payload.get('reason') or '当前目标需要定制执行计划。'}",
        ])
    if payload.get("decision") != "workflow_proposal":
        lines = [
            "未建议启动 Level 3 workflow。",
            f"原因：{payload.get('reason') or '未找到匹配 workflow 模板。'}",
        ]
        matched = payload.get("matched_templates") or []
        if matched:
            lines.append("最接近的模板：")
            for item in matched[:3]:
                lines.append(
                    "- "
                    f"{item.get('template_key')} "
                    f"({float(item.get('confidence') or 0.0):.2f})"
                )
        return "\n".join(lines)

    args = dict(payload.get("recommended_workflow_run_args") or {})
    lines = [
        "建议进入 Level 3 workflow proposal。",
        f"推荐模板：{payload.get('display_name')} (`{payload.get('template_key')}`)",
        f"置信度：{float(payload.get('confidence') or 0.0):.2f}",
        f"理由：{payload.get('reason') or 'Composite skill plan 与模板匹配。'}",
    ]
    missing = [
        str(item)
        for item in payload.get("missing_required") or []
        if str(item).strip()
    ]
    if missing:
        lines.append("需要补齐/确认：" + "、".join(missing))
    else:
        lines.append("必填输入：当前已满足模板启动要求。")
    lines.extend(
        [
            "安全策略：本工具只生成 proposal，不会创建或继续 workflow run。",
            "用户确认后，可调用 `workflow_run`，建议参数：",
            json.dumps(args, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )
    alternatives = [
        item
        for item in payload.get("matched_templates") or []
        if item.get("template_key") != payload.get("template_key")
    ]
    if alternatives:
        lines.append("备选模板：")
        for item in alternatives[:3]:
            lines.append(
                "- "
                f"{item.get('display_name') or item.get('template_key')} "
                f"(`{item.get('template_key')}`, "
                f"{float(item.get('confidence') or 0.0):.2f})"
            )
    return "\n".join(lines)
