"""面向内建模板与社区插件的通用 Workflow 编排工具。"""

from __future__ import annotations

import asyncio
import json
import importlib
from typing import Any, Optional

import pydantic

from ...types import ToolResult
from ...workflow import (
    WorkflowRunManager,
    WorkflowTemplateRegistry,
    recover_stale_running_stage,
    recover_validating_stage,
)
from ...workflow.clarifications import (
    build_workflow_questions,
    serialize_answer,
)
from ...workflow.applicability import (
    WorkflowApplicabilityAssessment,
    assess_workflow_applicability,
    workflow_applicability_fingerprint,
    workflow_original_request,
)
from ...workflow.protein_design import resolve_approved_protein_design_brief
from ...workflow.input_bindings import bind_workflow_request, latest_user_request
from ..base import ParameterSchema, Tool, ToolContext
from .question import QuestionTool


ASYNC_WAITING_STATUSES = {"waiting_external_job", "review_pending", "collecting"}
PROTEIN_DESIGN_LOOP_TEMPLATE = "protein_design_loop"


def _direct_workflow_report(run: Any, final_report: str) -> str:
    """Opt-in delivery of an already validated report, not scientific attestation.

    This policy comes from the frozen template, never the model's tool args.
    Pending/failed runs and templates with human gates cannot use this path.
    """
    snapshot = dict((run.metadata or {}).get("template_snapshot") or {})
    presentation = dict((snapshot.get("metadata") or {}).get("presentation") or {})
    completion = dict(presentation.get("completion") or {})
    if (completion.get("delivery") != "direct"
            or completion.get("source") != "stage_output_field"
            or run.status != "completed" or not str(final_report or "").strip()):
        return ""
    stages = list(run.stage_states or [])
    if not stages or any(stage.status != "committed" for stage in stages):
        return ""
    for stage in snapshot.get("stages") or []:
        if stage.get("review_gate") or (stage.get("metadata") or {}).get("human_confirmation"):
            return ""
    if not any(stage.stage_id == completion.get("stage_id") for stage in stages):
        return ""
    from ...server.workflow_presenter import (
        workflow_original_user_request,
        workflow_user_answer_text,
    )

    if workflow_original_user_request(run):
        return workflow_user_answer_text(run, final_report)
    return final_report.strip()


def _workflow_runtime_status(status: str) -> str:
    """把 Workflow 状态映射为前端统一运行时状态。"""

    normalized = str(status or "").strip().lower()
    if normalized == "review_pending":
        return "waiting_review"
    if normalized in {"waiting_external_job", "collecting"}:
        return "waiting_external"
    if normalized == "blocked":
        return "blocked"
    if normalized in {"cancelled", "canceled", "terminated"}:
        return "cancelled"
    if normalized == "completed":
        return "completed"
    if normalized in {"failed", "error"}:
        return "failed"
    return "running"


class WorkflowRunParameters(ParameterSchema):
    """Workflow 工具参数，兼容旧领域字段并支持插件自定义输入。"""

    template_key: str = ""
    input_payload: dict[str, Any] = pydantic.Field(default_factory=dict)
    target_description: str | None = None
    receptor_path: str | None = None
    ligand_path: str | None = None
    autobox_ligand_path: str | None = None
    paper_paths: Any = None
    substrate_or_product: str | None = None
    design_goal: str | None = None
    constraints: Any = None
    success_metrics: Any = None
    brief_path: str | None = None
    max_loop_iterations: int | None = None
    run_id: str | None = None
    continue_existing: bool = True
    auto_continue: bool = True
    max_auto_stages: int = 6
    applicability_confirmation: str = pydantic.Field(default="", pattern=r"^(?:[a-f0-9]{64})?$",
        description="仅使用上次工具返回的 64 位确认指纹；不能填写用户确认的说明文字。与 applicability_request 原样配对。")
    applicability_request: str = pydantic.Field(default="",
        description="上次工具返回的完整原始用户请求；不是 Workflow 名称、确认问题或用户本轮的确认回答。")

    @pydantic.field_validator("max_auto_stages")
    @classmethod
    def _validate_max_auto_stages(cls, value: int) -> int:
        """把单次自动执行阶段数限制在非负范围内。"""

        return max(0, int(value))

    @pydantic.field_validator("max_loop_iterations")
    @classmethod
    def _validate_max_loop_iterations(cls, value: int | None) -> int | None:
        """限制 Protein 设计循环次数，避免无界执行。"""

        if value is None:
            return None
        return max(1, min(20, int(value)))

    @pydantic.field_validator("input_payload")
    @classmethod
    def _validate_input_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """规范插件输入键，拒绝空键并保留 JSON 兼容的任意值。"""

        normalized: dict[str, Any] = {}
        for raw_key, item in dict(value or {}).items():
            key = str(raw_key or "").strip()
            if not key:
                raise ValueError("input_payload contains an empty field name")
            normalized[key] = item
        return normalized


class WorkflowRunTool(Tool):
    """创建或续跑声明式 Workflow，并统一处理输入、阶段执行和运行时记录。"""

    @property
    def id(self) -> str:
        """返回工具注册标识。"""

        return "workflow_run"

    @property
    def description(self) -> str:
        """返回提供给模型的通用 Workflow 使用说明。"""

        return """Create or resume a template-driven workflow run.

Use this only when the user explicitly selected a workflow template or an existing workflow run should continue. Every new run performs an applicability preflight, including explicitly selected templates. Pass applicability_confirmation and applicability_request only as the bound pair returned by a prior mismatch warning after the user explicitly accepted it. Put plugin-defined custom fields in input_payload. The template_key must match one of the JSON templates listed by the workflow template registry. protein_design_loop requires an approved protein_design_brief.md, then runs a bounded maker-checker loop until verified success or human escalation."""

    @property
    def parameters(self) -> type:
        """返回经过 Pydantic 校验的工具参数模型。"""

        return WorkflowRunParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        """创建或续跑 Workflow，并返回可持久化的状态与阶段摘要。"""

        parsed = WorkflowRunParameters(**args)
        workdir = str((context.extra or {}).get("workdir") or "").strip() or None
        template_key = str(parsed.template_key or "").strip()
        if not template_key and parsed.run_id:
            existing_run = WorkflowRunManager.get(parsed.run_id, workdir=workdir)
            if existing_run is not None:
                template_key = existing_run.template_key
        if not template_key:
            return ToolResult(
                title="Workflow Run",
                output="template_key is required. Choose one workflow template from /api/workflow/templates or pass an existing run_id.",
                metadata={"error": "template_key_required"},
            )
        template = WorkflowTemplateRegistry.get(template_key)
        if template is None:
            return ToolResult(
                title="Workflow Run",
                output=f"workflow template not found: {template_key}",
                metadata={"error": "template_not_found", "template_key": template_key},
            )

        extra = context.extra or {}
        selected = str(extra.get("selected_workflow_key") or "")
        owner = str((template.metadata or {}).get("owner_user_id") or "")
        private = bool((template.metadata or {}).get("private"))
        if (not template.enabled or (private and owner and owner != str(extra.get("user_id") or ""))
                or (selected and selected != template_key and not parsed.run_id)):
            return ToolResult(title="Workflow Run", output="Workflow template is disabled, inaccessible, or differs from the user's explicit selection.",
                error="workflow_template_not_allowed", metadata={"error": "workflow_template_not_allowed", "should_end_turn": True})

        resource_context = dict((context.extra or {}).get("resource_context") or {})
        input_payload = self._build_input_payload(parsed)
        existing_run = self._find_existing_run(
            context=context,
            parsed=parsed,
            template_key=template_key,
            workdir=workdir,
        )
        original_user_request = (
            str(parsed.applicability_request or "").strip()
            or latest_user_request(context.messages)
            or workflow_original_request(context.messages, input_payload)
        )
        applicability: dict[str, Any] = dict(
            (getattr(existing_run, "metadata", {}) or {}).get("applicability") or {}
        )
        if existing_run is None:
            # Bind before applicability approval, never parse domain fields.
            input_payload = bind_workflow_request(
                input_payload, template.input_contract,
                str(parsed.applicability_request or "") or latest_user_request(context.messages),
            )
            if parsed.applicability_confirmation or parsed.applicability_request:
                expected = workflow_applicability_fingerprint(template_key, original_user_request, input_payload)
                if (not parsed.applicability_request.strip()
                        or parsed.applicability_confirmation != expected):
                    message = (
                        "启动确认参数不匹配，未启动 Workflow，也未重新调用适用性模型。"
                        "请原样使用上次工具输出的 recommended_workflow_run_args；"
                        "不得用确认说明、Workflow 标题或改写稿替代原请求和指纹。"
                        "如果用户改了任务或输入，应移除确认参数，按新请求重新检查。"
                    )
                    return ToolResult(title="Workflow 启动确认参数无效", output=message, error=message,
                        metadata={"error": "invalid_applicability_confirmation", "status": "not_started",
                                  "workflow_run_not_started": True, "retryable": True})
            assessment = await assess_workflow_applicability(
                template,
                user_request=original_user_request,
                input_payload=input_payload,
                provider=(context.extra or {}).get("provider"),
            )
            applicability = assessment.to_dict()
            preconfirmed = bool(
                str(parsed.applicability_confirmation or "").strip()
                and str(parsed.applicability_confirmation or "").strip()
                == assessment.fingerprint
            )
            if assessment.requires_confirmation and not preconfirmed:
                confirmation = await self._confirm_applicability(
                    assessment=assessment,
                    template=template,
                    original_user_request=original_user_request,
                    context=context,
                )
                confirmation_metadata = dict(confirmation.metadata or {})
                if confirmation_metadata.get("awaiting_input"):
                    retry_args = {**dict(args or {}),
                        "applicability_confirmation": assessment.fingerprint,
                        "applicability_request": original_user_request}
                    return ToolResult(
                        title="Workflow 适用性确认",
                        output=confirmation.output + "\n\n仅供 Agent 使用，不向用户展示：用户明确同意后，"
                        "原样使用以下续跑参数，不重写原请求或指纹。\n"
                        + json.dumps({"recommended_workflow_run_args": retry_args}, ensure_ascii=False),
                        metadata={
                            **confirmation_metadata,
                            "status": "awaiting_confirmation",
                            "template_key": template_key,
                            "workflow_applicability": applicability,
                            "recommended_workflow_run_args": retry_args,
                            "should_end_turn": True,
                        },
                    )
                if not self._applicability_confirmation_accepted(confirmation):
                    return ToolResult(
                        title="Workflow 未启动",
                        output=(
                            f"没有启动「{template.display_name or template_key}」。"
                            "当前请求会返回主对话，由 Agent 重新选择更合适的处理方式。"
                        ),
                        metadata={
                            "status": "not_started",
                            "template_key": template_key,
                            "workflow_applicability": applicability,
                            "workflow_run_not_started": True,
                            "should_end_turn": False,
                        },
                    )
                applicability["confirmation"] = {
                    "confirmed": True,
                    "source": "auto_confirmation" if confirmation_metadata.get("auto_confirmed") else "question",
                    "auto_confirmed": bool(confirmation_metadata.get("auto_confirmed")),
                }
            elif preconfirmed:
                applicability["confirmation"] = {
                    "confirmed": True,
                    "source": "bound_follow_up",
                }
            else:
                applicability["confirmation"] = {
                    "confirmed": False,
                    "required": False,
                }
        if template_key == PROTEIN_DESIGN_LOOP_TEMPLATE:
            approved_brief, brief_error = resolve_approved_protein_design_brief(
                workdir=workdir,
                brief_path=parsed.brief_path,
            )
            if brief_error:
                return ToolResult(
                    title="Protein Design Loop",
                    output=brief_error,
                    metadata={
                        "error": "brainstorm_approval_required",
                        "template_key": template_key,
                    },
                )
            input_payload["brief_path"] = str(approved_brief)
            if parsed.max_loop_iterations is None:
                input_payload["max_loop_iterations"] = max(
                    1,
                    int((template.metadata or {}).get("max_loop_iterations", 3) or 3),
                )
        run = self._select_or_create_run(
            context=context,
            parsed=parsed,
            template_key=template_key,
            input_payload=input_payload,
            workdir=workdir,
            existing_run=existing_run,
            creation_metadata={
                "intent_snapshot": {
                    "original_user_request": original_user_request,
                    "selected_template_key": template_key,
                    "selection_source": "workflow_run",
                },
                "applicability": applicability,
            },
        )
        if existing_run is None:
            run = WorkflowRunManager.append_event(
                run.run_id,
                kind="workflow_preflight_evaluated",
                source="workflow_run",
                summary=(
                    "Workflow applicability accepted"
                    if not applicability.get("confirmation", {}).get("confirmed")
                    else "Workflow applicability accepted after user confirmation"
                ),
                payload={
                    "verdict": applicability.get("verdict"),
                    "confidence": applicability.get("confidence"),
                    "reason": applicability.get("reason"),
                    "source": applicability.get("source"),
                    "confirmation": dict(applicability.get("confirmation") or {}),
                },
                workdir=workdir,
            )

        run = WorkflowRunManager.resume_legacy_applicability_pause(
            run.run_id,
            workdir=workdir,
        )

        try:
            run = await self._collect_missing_inputs(
                run_id=run.run_id,
                input_payload=run.input_payload,
                resource_context=resource_context,
                context=context,
                workdir=workdir,
                template_key=template_key,
                input_contract=template.input_contract,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._cancel_interrupted_run(
                run_id=run.run_id,
                context=context,
                workdir=workdir,
                template_key=template_key,
            )
            raise
        run, unresolved_required = self._finalize_clarifications(
            run_id=run.run_id,
            input_payload=run.input_payload,
            resource_context=resource_context,
            workdir=workdir,
            template_key=template_key,
            input_contract=template.input_contract,
        )
        if str(run.status or "").strip().lower() == "validating":
            run = recover_validating_stage(run, workdir=str(workdir or ""))
        if str(run.status or "").strip().lower() == "running":
            run = recover_stale_running_stage(run, workdir=str(workdir or ""))

        executed_stages: list[str] = []
        if parsed.auto_continue and not unresolved_required:
            try:
                run, executed_stages = await self._auto_continue_run(
                    run_id=run.run_id,
                    workdir=workdir,
                    max_auto_stages=parsed.max_auto_stages,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._cancel_interrupted_run(
                    run_id=run.run_id,
                    context=context,
                    workdir=workdir,
                    template_key=template_key,
                )
                raise

        current_stage = run.current_stage_id or run.next_pending_stage_id() or ""
        output_lines = [
            f"workflow run: {run.run_id}",
            f"template: {template_key}",
            f"status: {run.status}",
        ]
        if executed_stages:
            output_lines.append(f"executed_stages: {', '.join(executed_stages)}")
        if current_stage:
            output_lines.append(f"next_stage: {current_stage}")
        if run.status in {"blocked", "failed", "cancelled", "terminated"}:
            failed_stage = next(
                (stage for stage in run.stage_states if stage.stage_id == current_stage),
                None,
            )
            reason = str(getattr(failed_stage, "last_error", "") or run.status)
            output_lines.append(f"stopped_reason: {reason}")
            output_lines.append("当前流程已停止；相同参数续跑不会推进。先处理原因，不要重复调用或自动要求人工审核。")
        if unresolved_required:
            output_lines.append(
                f"blocked_on: {', '.join(unresolved_required)}"
            )
        final_report = self._final_report_summary(run)
        if run.status == "completed" and final_report and not _direct_workflow_report(run, final_report):
            output_lines.append(
                "回答组织：请把 original_user_request 当作最终回答目标，并把 final_report 当作"
                "工作流证据来组织自然语言回复；先直接回答用户的问题，再给关键结果、依据、"
                "限制和下一步。不要只复述或原样返回 final_report。"
                "由宿主继续语义验收和证据检查。"
                "如需用户验收，只在该回复的综合评分区域显示确认按钮；"
                "不要调用 question 询问是否验收、满意或归档，也不要提前宣称验收通过。"
                "验收本身不授权写入记忆或归档。"
            )
        if final_report:
            output_lines.extend(
                [
                    "final_report:",
                    final_report,
                ]
            )
        async_stage_refs = self._async_stage_refs(run)
        if async_stage_refs:
            output_lines.append("async_stage:")
            output_lines.append(
                f"{run.current_stage_id or current_stage} 正在等待异步任务或审核；"
                "workflow run 已持久化，刷新页面或重新进入会话后可在流程工作区继续查看。"
            )

        self._record_workflow_runtime(
            run=run,
            context=context,
            workdir=workdir,
            template_key=template_key,
            current_stage=current_stage,
            executed_stages=executed_stages,
            unresolved_required=unresolved_required,
            async_stage_refs=async_stage_refs,
            final_report=final_report,
        )

        return ToolResult(
            title="Workflow Run",
            output="\n".join(output_lines),
            metadata={
                "run_id": run.run_id,
                "template_key": template_key,
                "status": run.status,
                "current_stage_id": current_stage,
                "executed_stages": executed_stages,
                "blocked_on": unresolved_required,
                "input_payload": dict(run.input_payload or {}),
                "original_user_request": str(
                    dict((run.metadata or {}).get("intent_snapshot") or {}).get(
                        "original_user_request"
                    )
                    or ""
                ),
                "workflow_applicability": dict(
                    (run.metadata or {}).get("applicability") or {}
                ),
                "final_report": final_report,
                "partial_result": {
                    "completed_stages": [{
                        "label": stage.label,
                        "summary": stage.result_summary,
                        "validated_result": json.dumps(stage.committed_result, ensure_ascii=False)[:4000],
                        "artifact_refs": stage.artifact_refs,
                    } for stage in run.stage_states if stage.status == "committed"],
                    "remaining_stages": [stage.label for stage in run.stage_states if stage.status != "committed"],
                } if run.status in {"blocked", "failed"} else {},
                "workflow_direct_response": _direct_workflow_report(run, final_report),
                "async_stage_refs": async_stage_refs,
                "should_end_turn": run.status in {
                    *ASYNC_WAITING_STATUSES, "blocked", "failed", "cancelled", "terminated",
                },
            },
        )

    def _record_workflow_runtime(
        self,
        *,
        run,
        context: ToolContext,
        workdir: Optional[str],
        template_key: str,
        current_stage: str,
        executed_stages: list[str],
        unresolved_required: list[str],
        async_stage_refs: list[dict[str, Any]],
        final_report: str,
    ) -> None:
        """把 Workflow 的最新生命周期投影到统一 Task Runtime 索引。"""

        try:
            from ...runtime.task_runtime import (
                TaskRuntimeStore,
                task_runtime_id_for_workflow,
            )

            TaskRuntimeStore.record_event(
                runtime_id=task_runtime_id_for_workflow(run.run_id),
                runtime_kind="workflow_run",
                status=_workflow_runtime_status(run.status),
                phase=str(run.status or "running").strip().lower() or "running",
                source="workflow_run",
                session_id=str(getattr(context, "session_id", "") or "").strip(),
                title=f"workflow:{template_key}",
                external_id=run.run_id,
                tool_name="workflow_run",
                metadata={
                    "run_id": run.run_id,
                    "template_key": template_key,
                    "workflow_status": run.status,
                    "current_stage_id": current_stage,
                    "executed_stages": executed_stages,
                    "blocked_on": unresolved_required,
                    "async_stage_refs": async_stage_refs,
                    "final_report": final_report,
                    "supports_resume": True,
                    "interrupt_behavior": "async_job",
                },
                workdir=workdir,
            )
        except Exception:
            pass

    def _build_input_payload(self, parsed: WorkflowRunParameters) -> dict[str, Any]:
        """合并通用插件输入与旧领域参数，显式领域参数具有更高优先级。"""

        payload: dict[str, Any] = dict(parsed.input_payload or {})
        for field in (
            "target_description",
            "receptor_path",
            "ligand_path",
            "autobox_ligand_path",
            "paper_paths",
            "substrate_or_product",
            "design_goal",
            "constraints",
            "success_metrics",
            "brief_path",
            "max_loop_iterations",
        ):
            value = getattr(parsed, field)
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                payload[field] = normalized
                continue
            payload[field] = value
        return payload

    def _find_existing_run(
        self,
        *,
        context: ToolContext,
        parsed: WorkflowRunParameters,
        template_key: str,
        workdir: Optional[str],
    ):
        """Resolve a resumable run before deciding whether a new-run preflight is needed."""

        if parsed.run_id:
            run = WorkflowRunManager.get(parsed.run_id, workdir=workdir)
            if run is None:
                raise KeyError(f"workflow run not found: {parsed.run_id}")
            return run
        if not parsed.continue_existing:
            return None
        terminal_statuses = {"completed", "cancelled", "terminated"}
        for run in WorkflowRunManager.list_runs(
            session_id=context.session_id,
            template_key=template_key,
            workdir=workdir,
        ):
            if str(run.status or "").strip().lower() not in terminal_statuses:
                return run
        return None

    async def _confirm_applicability(
        self,
        *,
        assessment: WorkflowApplicabilityAssessment,
        template,
        original_user_request: str,
        context: ToolContext,
    ) -> ToolResult:
        """Explain the boundary and ask once before creating a mismatched run."""

        fit_text = (
            "当前请求看起来不适合这个 Workflow"
            if assessment.verdict == "incompatible"
            else "当前请求与这个 Workflow 的适用范围不完全明确"
        )
        lines = [
            f"{fit_text}。",
            "",
            f"Workflow：{template.display_name or template.template_key}",
            f"用途：{assessment.workflow_summary or template.description or '按模板分阶段执行任务。'}",
        ]
        if original_user_request:
            lines.append(f"你的问题：{original_user_request[:800]}")
        if assessment.reason:
            lines.append(f"判断原因：{assessment.reason}")
        if assessment.do_not_use_when:
            lines.append("不适用场景：" + "；".join(assessment.do_not_use_when[:3]))
        if assessment.gaps:
            lines.append("已知限制：" + "；".join(assessment.gaps[:4]))
        lines.extend([
            "",
            "启动后会按既定步骤执行，不再因适用性问题暂停确认；结果中的限制会在最终回答中说明。",
            "是否仍然运行这个 Workflow？",
        ])
        yes = "仍然运行这个 Workflow"
        no = "不运行，返回普通对话处理"
        return await QuestionTool().execute(
            {
                "question": "\n".join(lines),
                "type": "single",
                "options": [yes, no],
                "confirmation_option": yes,
                "allow_custom": False,
                "default_answer": no,
                "timeout_seconds": 300,
                "goal": "确认所选 Workflow 是否继续运行",
                "metadata": {
                    "kind": "workflow_applicability_confirmation",
                    "template_key": template.template_key,
                    "workflow_applicability": assessment.to_dict(),
                },
            },
            context,
        )

    @staticmethod
    def _applicability_confirmation_accepted(result: ToolResult) -> bool:
        metadata = dict(result.metadata or {})
        selected = str(metadata.get("selected_option") or "").strip()
        answer = str(metadata.get("answer") or "").strip()
        return selected == "仍然运行这个 Workflow" or answer in {
            "1",
            "仍然运行这个 Workflow",
        }

    def _preserve_chat_request(
        self,
        payload: dict[str, Any],
        *,
        context: ToolContext,
        input_contract: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Only opt-in templates receive the verbatim request, never a model paraphrase."""

        return bind_workflow_request(payload, input_contract, latest_user_request(context.messages))

    def _cancel_interrupted_run(
        self,
        *,
        run_id: str,
        context: ToolContext,
        workdir: Optional[str],
        template_key: str,
    ) -> None:
        """在前台调用被取消时终止未完成 Workflow，并关闭统一运行时记录。"""

        reason = "workflow_run was interrupted before a durable result was returned"
        try:
            run = WorkflowRunManager.get(run_id, workdir=workdir)
            if run is None:
                return
            current_stage = next(
                (
                    stage
                    for stage in run.stage_states
                    if stage.stage_id == run.current_stage_id
                ),
                None,
            )
            if current_stage is not None and str(current_stage.status or "").lower() in {
                "running",
                "validating",
            }:
                run = WorkflowRunManager.mark_stage_failed(
                    run_id,
                    current_stage.stage_id,
                    error=reason,
                    metadata={"interrupted": True},
                    workdir=workdir,
                )
            run = WorkflowRunManager.update_status(
                run_id,
                status="cancelled",
                current_stage_id=run.current_stage_id,
                metadata={
                    "interrupted": True,
                    "interruption_reason": reason,
                },
                workdir=workdir,
            )
            self._record_workflow_runtime(
                run=run,
                context=context,
                workdir=workdir,
                template_key=template_key,
                current_stage=run.current_stage_id,
                executed_stages=[],
                unresolved_required=[],
                async_stage_refs=[],
                final_report="",
            )
        except Exception:
            return

    def _select_or_create_run(
        self,
        *,
        context: ToolContext,
        parsed: WorkflowRunParameters,
        template_key: str,
        input_payload: dict[str, Any],
        workdir: Optional[str],
        existing_run=None,
        creation_metadata: Optional[dict[str, Any]] = None,
    ):
        """选择可续跑实例或创建新实例，并把本轮输入合并进去。"""

        if existing_run is not None:
            run = existing_run
            return WorkflowRunManager.merge_input_payload(
                run.run_id,
                input_payload=input_payload,
                workdir=workdir,
            )

        # Already bound before preflight. Do not replace the agreed request
        # with a confirmation response here.
        return WorkflowRunManager.create_run(
            template_key=template_key,
            session_id=context.session_id,
            input_payload=input_payload,
            metadata={
                "created_by": "workflow_run_tool",
                **dict(creation_metadata or {}),
                **{
                    key: str((context.extra or {}).get(key) or "").strip()
                    for key in (
                        "tenant_id",
                        "user_id",
                        "workspace_id",
                        "turn_id",
                        "trace_id",
                    )
                    if str((context.extra or {}).get(key) or "").strip()
                },
            },
            workdir=workdir,
        )

    async def _collect_missing_inputs(
        self,
        *,
        run_id: str,
        input_payload: dict[str, Any],
        resource_context: dict[str, Any],
        context: ToolContext,
        workdir: Optional[str],
        template_key: str,
        input_contract: Optional[dict[str, Any]],
    ):
        """只对缺失的必填字段和显式要求询问的可选字段发起澄清。"""

        question_tool = QuestionTool()
        resolved_inputs, questions, autofilled = build_workflow_questions(
            input_payload,
            resource_context,
            input_contract=input_contract,
            template_key=template_key,
        )
        run = WorkflowRunManager.merge_input_payload(
            run_id,
            input_payload=resolved_inputs,
            metadata={
                "workflow_run_tool": True,
                "autofilled_inputs": dict(autofilled),
            },
            workdir=workdir,
        )

        for question in questions:
            metadata = dict(question.get("metadata") or {})
            metadata["workflow_run_id"] = run_id
            metadata["workflow_inputs"] = dict(run.input_payload or {})
            question["metadata"] = metadata
            result = await question_tool.execute(question, context)
            answer = serialize_answer((result.metadata or {}).get("answer"))
            field = str(metadata.get("workflow_field") or "").strip()
            if not field:
                continue
            run = WorkflowRunManager.merge_input_payload(
                run_id,
                input_payload={field: answer} if answer else {},
                metadata={"last_clarified_field": field},
                workdir=workdir,
            )
        return run

    def _finalize_clarifications(
        self,
        *,
        run_id: str,
        input_payload: dict[str, Any],
        resource_context: dict[str, Any],
        workdir: Optional[str],
        template_key: str,
        input_contract: Optional[dict[str, Any]],
    ):
        """重新计算缺失字段，并把 Workflow 切换到 blocked 或 queued。"""

        resolved_inputs, questions, autofilled = build_workflow_questions(
            input_payload,
            resource_context,
            input_contract=input_contract,
            template_key=template_key,
        )
        run = WorkflowRunManager.merge_input_payload(
            run_id,
            input_payload=resolved_inputs,
            metadata={"autofilled_inputs": dict(autofilled)},
            workdir=workdir,
        )
        unresolved_required = [
            str(dict(item.get("metadata") or {}).get("workflow_field") or "").strip()
            for item in questions
            if item.get("required")
        ]
        unresolved_required = [item for item in unresolved_required if item]
        if unresolved_required:
            run = WorkflowRunManager.update_status(
                run_id,
                status="blocked",
                metadata={"clarification_pending_fields": unresolved_required},
                current_stage_id=run.current_stage_id or run.next_pending_stage_id(),
                workdir=workdir,
            )
        elif (
            run.status == "blocked"
            and bool((run.metadata or {}).get("clarification_pending_fields"))
        ):
            run = WorkflowRunManager.update_status(
                run_id,
                status="queued",
                metadata={"clarification_pending_fields": []},
                current_stage_id=run.current_stage_id or run.next_pending_stage_id(),
                workdir=workdir,
            )
        return run, unresolved_required

    async def _auto_continue_run(
        self,
        *,
        run_id: str,
        workdir: Optional[str],
        max_auto_stages: int,
    ):
        """从当前阶段开始有界续跑，遇到等待、阻塞或终态立即停止。"""

        appmod = importlib.import_module("tcode_agent.server.app")
        run = WorkflowRunManager.get(run_id, workdir=workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        executed_stages: list[str] = []
        remaining = max_auto_stages

        while remaining > 0:
            if run.status in {"blocked", "failed", "completed", "cancelled", "terminated", *ASYNC_WAITING_STATUSES}:
                break
            stage_id = run.current_stage_id or run.next_pending_stage_id()
            if not stage_id:
                break
            try:
                run = await appmod._execute_workflow_stage_with_protein_worker(
                    run_id,
                    stage_id,
                    workdir=str(workdir or ""),
                )
            except Exception:
                # The stage API can raise after durably marking the run failed.
                # Return that record (and its valid partial artifacts), rather
                # than losing it through the generic tool-exception boundary.
                stopped = WorkflowRunManager.get(run_id, workdir=workdir)
                if stopped is None or stopped.status not in {"blocked", "failed"}:
                    raise
                run = stopped
            executed_stages.append(stage_id)
            remaining -= 1
            if run.status in {"blocked", *ASYNC_WAITING_STATUSES}:
                break
        return run, executed_stages

    def _async_stage_refs(self, run) -> list[dict[str, Any]]:
        """收集正在等待外部任务或人工审核的阶段引用。"""

        refs: list[dict[str, Any]] = []
        for stage in getattr(run, "stage_states", []) or []:
            if str(getattr(stage, "status", "") or "") not in ASYNC_WAITING_STATUSES:
                continue
            metadata = dict(getattr(stage, "metadata", {}) or {})
            for ref in metadata.get("async_tool_refs") or []:
                if isinstance(ref, dict):
                    refs.append(
                        {
                            "stage_id": stage.stage_id,
                            "stage_status": stage.status,
                            **dict(ref),
                        }
                    )
        return refs

    def _final_report_summary(self, run) -> str:
        """Use the same user-facing completion projection as the workflow UI."""

        from ...server.workflow_presenter import workflow_final_report_text

        return workflow_final_report_text(run)

    @classmethod
    def register(cls):
        """把工具实例注册到宿主工具目录。"""

        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
