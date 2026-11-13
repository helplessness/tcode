"""Read-only workflow template discovery tool."""

from __future__ import annotations

from typing import Any
from pydantic import Field

from ...types import ToolResult
from ...workflow import WorkflowTemplateRegistry
from ...workflow.models import WorkflowStageTemplate, WorkflowTemplate
from ...workflow.router import workflow_routing_config
from ...workflow.discovery import discover_workflow
from ..base import ParameterSchema, Tool, ToolContext


class WorkflowListTemplatesParameters(ParameterSchema):
    """Parameters for listing registered workflow templates."""

    include_disabled: bool = False
    include_stages: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    query: str = Field(default="", max_length=12000)


def _contract_items(contract: dict[str, Any], key: str) -> list[str]:
    raw_items = (contract or {}).get(key, [])
    if not isinstance(raw_items, list):
        return []
    items: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            value = item.get("field") or item.get("name") or item.get("key")
        else:
            value = item
        normalized = str(value or "").strip()
        if normalized:
            items.append(normalized)
    return items


def _stage_summary(stage: WorkflowStageTemplate) -> dict[str, Any]:
    return {
        "stage_id": stage.stage_id,
        "label": stage.label,
        "owner": stage.owner,
        "call_type": stage.call_type,
        "description": stage.description,
        "depends_on": list(stage.depends_on or []),
        "required_skills": list(stage.required_skills or []),
        "review_gate": bool(stage.review_gate),
        "expected_artifacts": list(stage.expected_artifacts or []),
        "validators": [dict(item) for item in stage.validators or []],
        "retry_policy": dict(stage.retry_policy or {}),
        "failure_policy": dict(stage.failure_policy or {}),
        "fallback_stage_ids": list(stage.fallback_stage_ids or []),
    }


def _template_summary(
    template: WorkflowTemplate,
    *,
    include_stages: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "template_key": template.template_key,
        "display_name": template.display_name,
        "domain": template.domain,
        "description": template.description[:500],
        "enabled": bool(template.enabled),
        "stage_count": len(template.stages or []),
        "required_inputs": _contract_items(template.input_contract, "required"),
        "optional_inputs": _contract_items(template.input_contract, "optional"),
        "required_outputs": _contract_items(template.output_contract, "required"),
        "optional_outputs": _contract_items(template.output_contract, "optional"),
        "input_contract": dict(template.input_contract),
        "use_when": workflow_routing_config(template)["use_when"],
        "do_not_use_when": workflow_routing_config(template)["do_not_use_when"],
    }
    if include_stages:
        summary["stages"] = [_stage_summary(stage) for stage in template.stages or []]
    return summary


def _render_templates(
    summaries: list[dict[str, Any]],
    *,
    include_disabled: bool,
    include_stages: bool,
) -> str:
    if not summaries:
        if include_disabled:
            return "当前平台没有注册任何 workflow 模板。"
        return "当前平台没有启用中的 workflow 模板；可以用 include_disabled=true 查看被禁用的模板。"

    scope = "注册的 workflow 模板（含禁用）" if include_disabled else "可调用 workflow 模板"
    lines = [f"当前平台发现 {len(summaries)} 个{scope}："]
    for index, item in enumerate(summaries, start=1):
        status = "启用" if item.get("enabled") else "禁用"
        lines.append(
            f"{index}. {item.get('display_name') or item.get('template_key')} "
            f"(`{item.get('template_key')}`) - {item.get('domain') or 'unknown'}，"
            f"{status}，{item.get('stage_count', 0)} 个阶段"
        )
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"   说明：{description}")
        if item.get("use_when"):
            lines.append("   适用：" + "；".join(item["use_when"]))
        if item.get("do_not_use_when"):
            lines.append("   不适用：" + "；".join(item["do_not_use_when"]))
        if item.get("input_contract"):
            import json
            lines.append("   输入声明：" + json.dumps(item["input_contract"], ensure_ascii=False))
        required_inputs = item.get("required_inputs") or []
        if required_inputs:
            lines.append(f"   必需输入：{', '.join(required_inputs)}")
        required_outputs = item.get("required_outputs") or []
        if required_outputs:
            lines.append(f"   主要输出：{', '.join(required_outputs)}")
        if include_stages and item.get("stages"):
            stage_items = [
                f"{stage.get('stage_id')}({stage.get('owner')}/{stage.get('call_type')})"
                for stage in item.get("stages", [])
                if stage.get("stage_id")
            ]
            if stage_items:
                lines.append(f"   阶段：{'; '.join(stage_items)}")
    return "\n".join(lines)


class WorkflowListTemplatesTool(Tool):
    """List registered workflow templates."""

    @property
    def id(self) -> str:
        return "workflow_list_templates"

    @property
    def description(self) -> str:
        return """List registered workflow JSON templates from the current platform.

Use this for any task domain to discover registered capabilities. query optionally asks the configured model for one bounded semantic selection, without keyword filtering or starting a run. Results are suggestions, not approval. The catalog is paginated with offset/limit and next_offset; follow pages when has_more=true. By default only enabled templates and compact descriptions are returned. include_stages=true includes stage details; include_disabled=true is for inspecting disabled templates."""

    @property
    def parameters(self) -> type:
        return WorkflowListTemplatesParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = WorkflowListTemplatesParameters(**args)
        templates = WorkflowTemplateRegistry.list_templates(
            include_disabled=bool(parsed.include_disabled)
        )
        templates = sorted(templates, key=lambda item: item.template_key)
        total = len(templates)
        page = templates[parsed.offset:parsed.offset + parsed.limit]
        summaries = [
            _template_summary(template, include_stages=bool(parsed.include_stages))
            for template in page
        ]
        discovery = await discover_workflow(parsed.query, provider=(context.extra or {}).get("provider")) if parsed.query else None
        if discovery and discovery.template_key:
            selected = WorkflowTemplateRegistry.get(discovery.template_key)
            if selected and not any(item["template_key"] == selected.template_key for item in summaries):
                summaries.append(_template_summary(selected, include_stages=False))
        next_offset = parsed.offset + len(page)
        has_more = next_offset < total
        import json
        diagnostic = ("\n语义发现：" + json.dumps(discovery.to_dict(), ensure_ascii=False)) if discovery else ""
        return ToolResult(
            title="Workflow Templates",
            output=_render_templates(
                summaries,
                include_disabled=bool(parsed.include_disabled),
                include_stages=bool(parsed.include_stages),
            ) + f"\n目录总数：{total}；has_more={str(has_more).lower()}；next_offset={next_offset if has_more else 'null'}。"
            + diagnostic,
            metadata={
                "count": len(summaries),
                "include_disabled": bool(parsed.include_disabled),
                "include_stages": bool(parsed.include_stages),
                "templates": summaries,
                "total": total, "offset": parsed.offset, "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "discovery": discovery.to_dict() if discovery else None,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
