"""Compatibility for the explicitly selected legacy protein-design agent.

This does not participate in discovery of new workflow templates.
"""
from __future__ import annotations
from dataclasses import replace
from typing import TYPE_CHECKING, Optional
from .protein_design import resolve_protein_design_execution_mode
from .registry import WorkflowTemplateRegistry

if TYPE_CHECKING:
    from .router import WorkflowRouteDecision

_PROTEIN_DESIGN_BRAINSTORM_AGENTS = {
    "protein_orchestrator",
    "protein-design",
}

_PROTEIN_DESIGN_RESUME_TERMS = (
    "继续",
    "接着",
    "恢复",
    "续跑",
    "继续执行",
    "开始执行",
    "执行方案",
    "执行简报",
    "按简报",
    "按计划",
    "查看状态",
    "查看进度",
    "当前状态",
    "当前进度",
    "resume",
    "continue",
    "go ahead",
    "proceed",
    "pick up",
    "status",
    "progress",
    "approved brief",
)

_PROTEIN_DESIGN_NEW_TASK_TERMS = (
    "新任务",
    "全新任务",
    "新需求",
    "全新需求",
    "另一个蛋白",
    "另一个设计",
    "new task",
    "new request",
    "another protein",
    "different protein",
)


def is_protein_design_brainstorm_mode(agent_name: str) -> bool:
    normalized = str(agent_name or "").strip().lower()
    return normalized in _PROTEIN_DESIGN_BRAINSTORM_AGENTS


def guard_protein_design_brainstorming_route(
    decision: "WorkflowRouteDecision",
    requested_agent_name: str,
    *,
    workdir: Optional[str] = None,
    message: str = "",
) -> "WorkflowRouteDecision":
    """Route new plugin requests to brainstorming and resume approved briefs."""

    if not is_protein_design_brainstorm_mode(requested_agent_name):
        return decision
    normalized_message = str(message or "").strip().lower()
    is_new_task = any(
        term in normalized_message for term in _PROTEIN_DESIGN_NEW_TASK_TERMS
    )
    has_resume_signal = any(
        term in normalized_message for term in _PROTEIN_DESIGN_RESUME_TERMS
    ) or bool((decision.signals or {}).get("follow_up_hits"))
    should_resume = bool(
        not is_new_task
        and (not normalized_message or has_resume_signal)
    )
    execution_mode = None
    brief_path = None
    if should_resume and str(workdir or "").strip():
        execution_mode, brief_path, _ = resolve_protein_design_execution_mode(
            workdir=workdir,
        )
    if execution_mode and brief_path is not None:
        input_payload = dict(decision.input_payload or {})
        signals = {
            **dict(decision.signals or {}),
            "protein_design_resume_approved": True,
            "protein_design_execution_mode": execution_mode,
            "protein_design_brief_path": str(brief_path),
        }
        common = {
            "missing_required": [],
            "recommended_agent": "protein_orchestrator",
            "recommended_scheduler_mode": "supervisor",
            "should_delegate_to_recommended_agent": False,
            "signals": signals,
            "reason": (
                "检测到已批准的 protein_design_brief.md，"
                f"按 execution_mode={execution_mode} 恢复"
            ),
        }
        if execution_mode == "workflow_loop":
            input_payload["brief_path"] = "protein_design_brief.md"
            if WorkflowTemplateRegistry.get("protein_design_loop") is not None:
                return replace(
                    decision,
                    should_start=True,
                    intent="protein_design_loop",
                    intent_family="workflow",
                    template_key="protein_design_loop",
                    input_payload=input_payload,
                    **common,
                )
            signals["workflow_template_unavailable"] = "protein_design_loop"
            return replace(
                decision,
                should_start=False,
                intent="protein_design_workflow_unavailable",
                intent_family="domain_task",
                template_key="",
                input_payload=input_payload,
                **{
                    **common,
                    "signals": signals,
                    "reason": "protein_design_loop 模板已禁用，不能恢复 workflow。",
                },
            )
        return replace(
            decision,
            should_start=False,
            intent=f"protein_design_{execution_mode}",
            intent_family=(
                "direct_execution" if execution_mode == "direct" else "agent_loop"
            ),
            template_key="",
            input_payload=input_payload,
            **common,
        )
    return replace(
        decision,
        should_start=False,
        intent="protein_design_brainstorm",
        intent_family="brainstorm",
        template_key="",
        missing_required=[],
        recommended_agent="protein_orchestrator",
        recommended_scheduler_mode="supervisor",
        should_delegate_to_recommended_agent=False,
    )
