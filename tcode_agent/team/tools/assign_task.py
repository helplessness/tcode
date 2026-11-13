"""Tool: assign_task - dispatch a task to a Worker (async or sync)"""

import re
from typing import Any, Optional

from pydantic import model_validator

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...subtask_handoff import SubtaskHandoff, build_subtask_handoff
from ...types import ToolResult
from ...runtime.resource_import import normalize_resource_argument
from ..registry import WorkerRegistry
from ..coordinator import assign_worker_task, run_worker_task_inline
from ...workflow.protein_design import resolve_approved_protein_design_brief
from ...workflow.router import is_protein_design_brainstorm_mode


def protein_design_assign_gate_error(context: ToolContext) -> str:
    """Return a blocking reason when assign_task must wait for brainstorm approval."""

    agent_name = str(getattr(context, "agent", "") or "").strip().lower()
    if not is_protein_design_brainstorm_mode(agent_name):
        return ""

    workdir = ""
    if isinstance(getattr(context, "extra", None), dict):
        workdir = str(context.extra.get("workdir") or "").strip()
    _, error = resolve_approved_protein_design_brief(
        workdir=workdir or None,
        brief_path="protein_design_brief.md",
    )
    return str(error or "").strip()


class AssignTaskParameters(ParameterSchema):
    worker_name: str
    task: str
    wait: Optional[bool] = None  # True 则同步等待 Worker 完成并将结果直接返回给 Manager
    goal: Optional[str] = None
    deliverable: Optional[str] = None
    stop_when: Optional[str] = None
    allowed_tools: str | list[str] | None = None
    resources: dict[str, Any] | list[Any] | str | None = None
    token_budget: Optional[int] = None
    tool_call_budget: Optional[int] = None
    retry_budget: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def fill_missing_task_from_compatible_fields(cls, value: Any) -> Any:
        """Accept structured callers that put the task text in ``goal``.

        ``task`` remains required in the published schema so models are guided
        toward the canonical contract.  This pre-validation adapter prevents a
        recoverable manager formatting mistake from blocking subagent startup.
        """

        if not isinstance(value, dict) or str(value.get("task") or "").strip():
            return value
        normalized = dict(value)
        fallback = next(
            (
                str(normalized.get(field) or "").strip()
                for field in ("goal", "prompt", "description", "deliverable")
                if str(normalized.get(field) or "").strip()
            ),
            "",
        )
        if fallback:
            normalized["task"] = fallback
        return normalized


class AssignTaskTool(Tool):
    def _task_has_local_file_reference(self, task: str) -> bool:
        text = str(task or "").strip()
        if not text:
            return False
        if re.search(r"(?i)(?:^|[\s\"'`])(?:/|~\/)[^\n\r\"'`]+?\.(?:pdf|zip|json|csv|md|txt)\b", text):
            return True
        return bool(re.search(r"(?im)^\s*Path:\s*(?:/|~/)[^\n\r]+", text))

    def _task_requests_background_execution(self, task: str) -> bool:
        text = str(task or "").strip().lower()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "后台",
                "异步",
                "不用等",
                "稍后",
                "background",
                "async",
                "asynchronous",
                "don't wait",
                "do not wait",
            )
        )

    def _has_file_backed_execution_context(
        self,
        task: str,
        resource_context: dict[str, Any],
    ) -> bool:
        return (
            self._resource_file_count(resource_context) > 0
            or self._task_has_local_file_reference(task)
        )

    def _looks_like_execution_task(self, task: str, resource_context: dict[str, Any]) -> bool:
        if self._resource_file_count(resource_context) > 0:
            return True

        text = str(task or "").lower()
        return any(
            keyword in text
            for keyword in (
                "parse",
                "extract",
                "transform",
                "convert",
                "normalize",
                "clean",
                "cleanup",
                "format",
                "rewrite",
                "markdown",
                "json",
                "csv",
                "doi",
                "url",
                "evidence",
                "ocr",
                "table",
                "figure",
                "image",
                "pdf",
                "解析",
                "提取",
                "抽取",
                "整理",
                "转换",
                "清洗",
                "格式",
                "改写",
                "markdown",
                "表格",
                "图片",
                "图像",
                "证据",
                "原文",
                "链接",
                "附件",
                "本地",
                "落盘",
                "位点",
                "突变",
                "活性",
                "动力学",
                "底物",
            )
        )

    def _infer_allowed_tools(
        self,
        *,
        parsed: AssignTaskParameters,
        resource_context: dict[str, Any],
        raw_args: dict[str, Any],
    ) -> str | list[str]:
        if "allowed_tools" in (raw_args or {}) and parsed.allowed_tools not in (None, ""):
            return parsed.allowed_tools

        worker_name = str(parsed.worker_name or "").strip().lower()
        has_local_files = self._has_file_backed_execution_context(
            parsed.task,
            resource_context,
        )
        if (
            worker_name == "paper_generalist"
            and has_local_files
            and self._looks_like_pdf_or_file_task(parsed.task, resource_context)
        ):
            # 对 paper_generalist 的本地 PDF/文件执行任务，自动收窄到确定性的本地工具集合。
            # 这样它会更像 build：直接用 skill + bash 完成本地执行，而不是绕去 webfetch(file://...)。
            return [
                "skill",
                "skill_run",
                "question",
                "bash",
                "read",
                "memory_read",
                "team_memory_read",
                "team_memory_write",
                "paper_prepare_workspace",
                "paper_collect_helper_results",
                "paper_library_lookup_pdf",
                "paper_library_ingest_pdf",
                "paper_library_get_record",
                "paper_save_cards",
                "paper_write_artifact",
            ]
        if worker_name == "paper_generalist" and has_local_files:
            return [
                "skill",
                "skill_run",
                "question",
                "bash",
                "read",
                "glob",
                "memory_read",
                "team_memory_read",
                "team_memory_write",
                "paper_prepare_workspace",
                "paper_collect_helper_results",
                "paper_library_lookup_pdf",
                "paper_library_ingest_pdf",
                "paper_library_get_record",
                "paper_save_cards",
                "paper_write_artifact",
            ]
        if worker_name == "paper_generalist" and self._looks_like_execution_task(parsed.task, resource_context):
            # 杂活默认走直执行模式：保留必要工具，但仍以执行落地为主，而不是团队规划。
            return [
                "skill",
                "skill_run",
                "question",
                "bash",
                "read",
                "glob",
                "webfetch",
                "memory_read",
                "team_memory_read",
                "team_memory_write",
                "paper_prepare_workspace",
                "paper_collect_helper_results",
                "paper_library_lookup_pdf",
                "paper_library_ingest_pdf",
                "paper_library_get_record",
                "paper_save_cards",
                "paper_write_artifact",
            ]
        if worker_name == "paper_library_manager":
            return [
                "memory_read",
                "team_memory_read",
                "team_memory_write",
                "paper_library_lookup_pdf",
                "paper_library_ingest_pdf",
                "paper_library_get_record",
            ]
        if worker_name == "paper_library_retriever":
            return [
                "read",
                "glob",
                "memory_read",
                "team_memory_read",
                "paper_library_query",
                "paper_library_get_record",
            ]
        return "all"

    @staticmethod
    def _resolve_allowed_tools(raw: Any) -> str | list[str]:
        from ...tools.registry import ToolRegistry

        return ToolRegistry.normalize_allowlist(raw)

    def _resource_file_count(self, resource_context: dict[str, Any]) -> int:
        count = 0
        uploaded_files = resource_context.get("uploaded_files")
        if isinstance(uploaded_files, list):
            count += sum(
                1
                for item in uploaded_files
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            )
        paper_inputs = resource_context.get("paper_inputs")
        if isinstance(paper_inputs, dict):
            count += int(paper_inputs.get("input_count", 0) or 0)
        return count

    def _looks_like_pdf_or_file_task(self, task: str, resource_context: dict[str, Any]) -> bool:
        text = str(task or "").lower()
        if any(
            keyword in text
            for keyword in (
                "pdf",
                "论文",
                "表格",
                "图片",
                "原文",
                "突变",
                "活性",
                "动力学",
                "底物",
                "文件",
                "附件",
            )
        ):
            return True

        for item in list(resource_context.get("uploaded_files") or []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip().lower()
            kind = str(item.get("kind") or "").strip().lower()
            if path.endswith(".pdf") or kind == "pdf":
                return True

        paper_inputs = resource_context.get("paper_inputs")
        if isinstance(paper_inputs, dict):
            if int(paper_inputs.get("pdf_count", 0) or 0) > 0:
                return True
            for item in list(paper_inputs.get("inputs") or []):
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip().lower()
                kind = str(item.get("kind") or "").strip().lower()
                if path.endswith(".pdf") or kind == "pdf":
                    return True
        return False

    def _infer_wait_mode(
        self,
        *,
        context: ToolContext,
        parsed: AssignTaskParameters,
        resource_context: dict[str, Any],
        raw_args: dict[str, Any],
    ) -> bool:
        if "wait" in (raw_args or {}):
            return bool(parsed.wait)
        if not self._has_file_backed_execution_context(parsed.task, resource_context):
            return False
        if self._task_requests_background_execution(parsed.task):
            return False
        if not (
            self._looks_like_pdf_or_file_task(parsed.task, resource_context)
            or self._looks_like_execution_task(parsed.task, resource_context)
        ):
            return False
        # 对携带本地资源的执行任务默认同步等待，避免结果停留在后台。
        # 如果 manager 明确传 wait=false 或任务文本明确要求后台执行，则尊重该决策。
        return True

    def _normalize_resources(
        self,
        raw: Any,
        *,
        workdir: str = "",
        inherited_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return normalize_resource_argument(
            raw,
            workdir=workdir,
            inherited_context=inherited_context,
        )

    def _merge_resources(self, inherited: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any]:
        if not inherited:
            return dict(explicit or {})
        if not explicit:
            return dict(inherited or {})

        merged = dict(inherited)

        for key, value in explicit.items():
            if key == "uploaded_files":
                current = list(merged.get("uploaded_files") or [])
                merged_files: dict[str, dict[str, Any]] = {}
                for item in [*current, *(value or [])]:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip()
                    if not path:
                        continue
                    merged_files[path] = dict(item)
                merged["uploaded_files"] = list(merged_files.values())
                continue
            if key == "paper_inputs" and isinstance(value, dict):
                current_inputs = dict(merged.get("paper_inputs") or {}) if isinstance(merged.get("paper_inputs"), dict) else {}
                next_inputs = dict(value)
                merged_inputs = dict(current_inputs)
                for field in (
                    "input_count",
                    "pdf_count",
                    "requires_ingest",
                ):
                    if field in next_inputs:
                        merged_inputs[field] = next_inputs[field]
                current_list = list(current_inputs.get("inputs") or [])
                next_list = list(next_inputs.get("inputs") or [])
                deduped_inputs: dict[str, dict[str, Any]] = {}
                for item in [*current_list, *next_list]:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip()
                    if not path:
                        continue
                    deduped_inputs[path] = dict(item)
                if deduped_inputs:
                    merged_inputs["inputs"] = list(deduped_inputs.values())
                merged["paper_inputs"] = merged_inputs
                continue
            merged[key] = value
        return merged

    @property
    def id(self) -> str:
        return "assign_task"

    @property
    def description(self) -> str:
        return """给指定 Worker 分派任务。

转交时保留用户原始要求和来源边界，只拆分职责，不补入未经来源支持的结论或新的验收要求。
预算用量是告警记录，不能擅自把“预算耗尽”增加为用户任务的完成条件。

参数：
- worker_name: Worker 名称（与 create_worker 时相同，小写）；传 `auto` 可自动选择最合适的 Worker
- task: 必填，详细的任务描述（越详细越好）。标准调用必须显式传入；为兼容旧调用，缺失时会依次尝试使用 goal、prompt、description 或 deliverable。
- wait: (true/false) 默认 false（异步后台执行）；但携带本地资源且属于抽取、解析、整理、转换类执行任务时，默认会同步等待。
  设为 true 则**同步等待 Worker 完成**，将完整结果直接返回到当前对话（Manager 可立即感知并利用结果）。
  如果你判断任务适合后台执行，可以显式传 wait=false；如果任务需要在本轮整合结果，请传 wait=true。
- goal / deliverable / stop_when: 可选，结构化任务契约
- allowed_tools: 可选，限制该次任务允许使用的工具（"all" 或列表）
- resources: 可选，显式传递文件路径、artifact 路径、候选列表等资源；若省略，会自动继承当前会话中的资源上下文

异步模式：立即返回，任务后台运行；用 list_workers 查看进度。
同步模式：阻塞直到 Worker 完成，结果会经过 Critic/Verifier 审查后返回。"""

    @property
    def parameters(self) -> type:
        return AssignTaskParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = AssignTaskParameters(**args)
        gate_error = protein_design_assign_gate_error(context)
        if gate_error:
            return ToolResult(
                title=f"assign_task → {parsed.worker_name.strip().lower() or 'worker'}",
                output=(
                    "蛋白设计模式在 BRAINSTORM 通过前禁止调用 assign_task。\n"
                    f"{gate_error}\n"
                    "请先完成头脑风暴并写入 protein_design_brief.md，且第一行为 "
                    "'status: BRAINSTORM APPROVED'。"
                ),
                metadata={
                    "error": "brainstorm_approval_required",
                    "required_file": "protein_design_brief.md",
                    "required_marker": "status: BRAINSTORM APPROVED",
                },
            )

        team_id = context.session_id
        workdir = (context.extra or {}).get("workdir", "") or ""
        event_emitter = (context.extra or {}).get("__agent_event_emit__")
        parent_question_ask = (context.extra or {}).get("__question_ask__")
        parent_question_resolve = (context.extra or {}).get("__question_resolve__")
        worker_name = parsed.worker_name.strip().lower()
        task_prompt = parsed.task.strip()
        owner_kwargs = {
            key: str((context.extra or {}).get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
            if str((context.extra or {}).get(key) or "").strip()
        }
        resolved_worker, worker_candidates = await WorkerRegistry.resolve_for_session_tree(
            team_id,
            worker_name,
            owner_kwargs=owner_kwargs,
        )
        if resolved_worker is not None:
            team_id = resolved_worker.team_id
            worker_name = resolved_worker.name
        elif len(worker_candidates) > 1:
            candidate_text = ", ".join(
                f"{worker.name} ({worker.session_id}, team={worker.team_id})"
                for worker in worker_candidates[:12]
            )
            return ToolResult(
                title=f"assign_task → {worker_name}",
                output="",
                error=(
                    f"Worker 标识 '{worker_name}' 在当前会话树中不唯一："
                    f"{candidate_text}。请改用准确的 worker session_id。"
                ),
                metadata={
                    "worker_resolution": "ambiguous",
                    "candidate_session_ids": [
                        worker.session_id for worker in worker_candidates
                    ],
                    "retryable": True,
                },
            )
        inherited_resources = self._normalize_resources(
            (context.extra or {}).get("resource_context"),
            workdir=workdir,
        )
        explicit_resources = self._normalize_resources(
            parsed.resources,
            workdir=workdir,
            inherited_context=inherited_resources,
        )
        merged_resources = self._merge_resources(inherited_resources, explicit_resources)
        should_wait = self._infer_wait_mode(
            context=context,
            parsed=parsed,
            resource_context=merged_resources,
            raw_args=args,
        )
        allowed_tools = self._resolve_allowed_tools(
            self._infer_allowed_tools(
                parsed=parsed,
                resource_context=merged_resources,
                raw_args=args,
            )
        )
        if isinstance(allowed_tools, list):
            from ...tools.registry import ToolRegistry

            unknown_tools = ToolRegistry.unknown_allowlist_ids(allowed_tools)
            if unknown_tools:
                return ToolResult(
                    title=f"assign_task → {worker_name}",
                    output="",
                    error=(
                        "Unknown allowed_tools entries: "
                        + ", ".join(unknown_tools)
                        + ". Use exact registered tool IDs. Executable skill actions "
                        "must be invoked through `skill` and `skill_run`."
                    ),
                    metadata={
                        "invalid_allowed_tools": unknown_tools,
                        "resolved_allowed_tools": allowed_tools,
                        "retryable": True,
                    },
                )
        task_spec = {
            "contract": {
                "goal": str(parsed.goal or "").strip(),
                "deliverable": str(parsed.deliverable or "").strip(),
                "stop_when": str(parsed.stop_when or "").strip(),
                "allowed_tools": allowed_tools,
            },
            "resources": merged_resources,
            "execution_profile": (context.extra or {}).get("execution_profile"),
            "budget": {
                "tokens": max(0, int(parsed.token_budget or 0)),
                "tool_calls": max(0, int(parsed.tool_call_budget or 0)),
                "retries": max(0, int(parsed.retry_budget or 0)),
            },
        }

        if should_wait:
            subtask_handoff = await run_worker_task_inline(
                team_id=team_id,
                worker_name=worker_name,
                task_prompt=task_prompt,
                workdir=workdir,
                event_emitter=event_emitter,
                task_spec=task_spec,
                parent_question_ask=parent_question_ask,
                parent_question_resolve=parent_question_resolve,
                question_policy=(context.extra or {}).get("__question_policy__", "ask"),
                invocation_id=str(context.call_id or ""),
                parent_turn_id=str((context.extra or {}).get("turn_id") or ""),
                parent_request_id=str(
                    (context.extra or {}).get("request_id") or ""
                ),
            )
            if not isinstance(subtask_handoff, SubtaskHandoff):
                # Compatibility for integrations that still monkeypatch the
                # coordinator with the pre-v1 plain-string return contract.
                subtask_handoff = build_subtask_handoff(
                    result_text=subtask_handoff,
                    tool_trace=[],
                    child_outcome_status="completed",
                    agent_name=worker_name,
                )
            output = subtask_handoff.render_for_parent()
            child_terminal_error = subtask_handoff.child_terminal_error
            return ToolResult(
                title=f"assign_task(同步) → {worker_name}",
                output=output,
                error=(
                    subtask_handoff.blocker or subtask_handoff.result_text
                    if child_terminal_error
                    else ""
                ),
                metadata={
                    "status": subtask_handoff.outcome_status,
                    "outcome_status": subtask_handoff.outcome_status,
                    "child_outcome_status": subtask_handoff.child_outcome_status,
                    "worker_name": worker_name,
                    "task_id": subtask_handoff.task_id,
                    "session_id": subtask_handoff.session_id,
                    "tool_trace": subtask_handoff.tool_trace,
                    "completed_subtask_steps": len(subtask_handoff.completed_work),
                    "completed_subtask_work": subtask_handoff.completed_work,
                    "failed_subtask_steps": len(subtask_handoff.failed_work),
                    "failed_subtask_work": subtask_handoff.failed_work,
                    "subtask_handoff": subtask_handoff.to_dict(),
                    # A child-scoped failure is recoverable by the Manager and
                    # must never terminate the parent user's turn.
                    "retryable": child_terminal_error,
                    "terminal_tool_error": False,
                    "should_end_turn": False,
                    "subtask_terminal_error": child_terminal_error,
                    "termination_scope": (
                        "subagent" if child_terminal_error else "none"
                    ),
                    "recovery_required": subtask_handoff.recovery_required,
                },
            )

        msg = await assign_worker_task(
            team_id=team_id,
            worker_name=worker_name,
            task_prompt=task_prompt,
            workdir=workdir,
            event_emitter=event_emitter,
            task_spec=task_spec,
            parent_question_ask=parent_question_ask,
            parent_question_resolve=parent_question_resolve,
            question_policy=(context.extra or {}).get("__question_policy__", "ask"),
            invocation_id=str(context.call_id or ""),
            parent_turn_id=str((context.extra or {}).get("turn_id") or ""),
            parent_request_id=str((context.extra or {}).get("request_id") or ""),
        )
        return ToolResult(title=f"assign_task → {worker_name}", output=msg)

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
