"""Structured orchestration plan, DAG, and MCP contract helpers.

This module is intentionally storage-light: plans are persisted as JSON under
the workspace, while executable async MCP tasks remain authoritative in
SharedTracker. That keeps the legacy Symphony/Bridge queue stable and gives
new code a machine-readable Plan -> Step -> TaskSpec -> Dependency model.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from .plan_schema import Plan, PlanStatus, PlanStep, StepCategory, StepStatus


ORCHESTRATION_SCHEMA_VERSION = "orchestration.plan/v1"
TASK_SPEC_VERSION = "task.spec/v1"
MCP_TOOL_CONTRACT_VERSION = "mcp.tool/v1"
_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TASK_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "duplicate"}


def _utc_path_workdir(workdir: Optional[str | Path] = None) -> Path:
    if workdir is None:
        return Path(os.environ.get("TCODE_WORKDIR") or os.getcwd())
    return Path(workdir)


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)].rstrip() + " ...[truncated]"


def _safe_plan_id(value: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        text = f"orch-{uuid.uuid4().hex[:12]}"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    if not text or not text[0].isalnum():
        text = f"orch-{uuid.uuid4().hex[:12]}"
    return text[:128]


def _normalize_plan_id(value: str) -> str:
    text = _safe_plan_id(value)
    if not _PLAN_ID_RE.fullmatch(text):
        raise ValueError("invalid orchestration plan_id")
    return text


def _normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _json_snapshot(value: Any) -> Any:
    """Return a detached JSON-safe snapshot so later mutations cannot create cycles."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        if isinstance(value, dict):
            return {str(key): _json_snapshot(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_snapshot(item) for item in value]
        return str(value)


def _task_status_to_step_status(status: Any) -> StepStatus:
    text = str(status or "").strip().lower()
    if text in {"completed", "duplicate"}:
        return StepStatus.COMPLETED
    if text in {"failed"}:
        return StepStatus.FAILED
    if text in {"cancelled", "canceled"}:
        return StepStatus.SKIPPED
    if text in {"claimed", "running", "retrying", "review_pending"}:
        return StepStatus.IN_PROGRESS
    return StepStatus.PENDING




def _task_orchestration(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    for source in (metadata, workspace):
        payload = source.get("orchestration") if isinstance(source, dict) else {}
        if isinstance(payload, dict) and payload:
            return dict(payload)
    return {}


def build_mcp_tool_contract(
    *,
    server_name: str,
    tool_name: str,
    tool_args: dict[str, Any],
    identifier: str,
    execution_channel: str,
    submit_input: dict[str, Any],
) -> dict[str, Any]:
    """Return the normalized submit/status/result/artifact/error contract."""

    submit_snapshot = _json_snapshot(submit_input if isinstance(submit_input, dict) else {})
    tool_args_snapshot = _json_snapshot(tool_args if isinstance(tool_args, dict) else {})
    workspace = (
        submit_snapshot.get("workspace")
        if isinstance(submit_snapshot.get("workspace"), dict)
        else {}
    )
    expected_artifacts = (
        submit_input.get("expected_artifacts")
        if isinstance(submit_input.get("expected_artifacts"), list)
        else []
    )
    return {
        "contract_version": MCP_TOOL_CONTRACT_VERSION,
        "kind": "mcp_tool",
        "server": str(server_name or "").strip(),
        "tool": str(tool_name or "").strip(),
        "execution_channel": str(execution_channel or "").strip(),
        "submit": {
            "transport": "job-protocol",
            "tool": "mcp_job-protocol-mcp_submit_job",
            "input_shape": "job_protocol.submit_job.v1",
            "target_server": str(server_name or "").strip(),
            "target_tool": str(tool_name or "").strip(),
            "target_args": tool_args_snapshot,
            "submit_input": submit_snapshot,
        },
        "status": {
            "tool": "mcp_job-protocol-mcp_get_job_status",
            "input": {"local_request_id": str(identifier or "").strip()},
            "states": ["queued", "running", "review_pending", "completed", "failed", "cancelled"],
            "terminal_states": ["completed", "failed", "cancelled"],
        },
        "result": {
            "tool": "mcp_job-protocol-mcp_collect_job_result",
            "input": {"local_request_id": str(identifier or "").strip()},
            "terminal_required": True,
        },
        "artifacts": {
            "expected": [str(item or "").strip() for item in expected_artifacts if str(item or "").strip()],
            "root": str(workspace.get("workdir") or submit_input.get("workdir") or "").strip(),
            "source": "workspace",
        },
        "error": {
            "summary_field": "error_summary",
            "detail_fields": ["error", "message", "stderr", "failure_reason", "exit_code"],
            "business_failure_fields": ["result_code=false", "success=false", "status=failed"],
        },
    }


def build_task_spec(
    *,
    task_id: str,
    step_id: str,
    title: str,
    server_name: str = "",
    tool_name: str = "",
    tool_args: Optional[dict[str, Any]] = None,
    execution_channel: str = "",
    depends_on: Optional[list[str]] = None,
    expected_artifacts: Optional[list[str]] = None,
    contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "task_spec_version": TASK_SPEC_VERSION,
        "task_id": str(task_id or "").strip(),
        "step_id": str(step_id or "").strip(),
        "title": str(title or "").strip() or "Orchestration task",
        "kind": "mcp_tool" if server_name and tool_name else "generic",
        "executor": {
            "type": "mcp" if server_name and tool_name else "manual",
            "server": str(server_name or "").strip(),
            "tool": str(tool_name or "").strip(),
            "execution_channel": str(execution_channel or "").strip(),
        },
        "input": dict(tool_args or {}),
        "depends_on": list(depends_on or []),
        "outputs": {
            "expected_artifacts": list(expected_artifacts or []),
        },
        "contract": dict(contract or {}),
    }


def build_action_policy(
    *,
    lifecycle_status: str,
    job_id: str = "",
    has_submit_input: bool = False,
    resume_supported: bool = False,
    resume_command: str = "",
    review_status: str = "",
    retry_reason: str = "",
    resume_reason: str = "",
) -> dict[str, Any]:
    """Normalize retry/resume/cancel/continue decisions for UI and agents."""

    status = str(lifecycle_status or "").strip().lower()
    terminal = status in {"completed", "done", "closed", "failed", "cancelled", "canceled", "terminated", "duplicate"}
    active = status in {"queued", "pending", "running", "claimed", "retrying", "review_pending", "reviewing", "in progress"}
    normalized_review = str(review_status or "").strip().lower()
    has_job = bool(str(job_id or "").strip())
    can_resume_capability = bool(resume_supported or str(resume_command or "").strip())

    retry_blocker = str(retry_reason or "").strip()
    if not retry_blocker:
        if not has_job:
            retry_blocker = "当前任务还没有真正生成 job_id，通常仍停留在 Todo 或人工审核阶段。"
        elif not terminal:
            retry_blocker = f"当前任务状态是 {status or 'unknown'}，只有终态任务才能重试。"
        elif not has_submit_input:
            retry_blocker = "当前任务缺少原始 submit_job 输入，无法自动重试。"

    resume_blocker = str(resume_reason or "").strip()
    if not resume_blocker:
        if not has_job:
            resume_blocker = "当前任务还没有真正生成 job_id，暂时不能恢复。"
        elif not terminal:
            resume_blocker = f"当前任务状态是 {status or 'unknown'}，只有终态任务才能恢复。"
        elif not can_resume_capability:
            resume_blocker = "当前执行层没有声明 resume 能力，所以只能重试，不能恢复。"
        elif not has_submit_input:
            resume_blocker = "当前任务缺少原始 submit_job 输入，无法自动恢复。"

    cancel_reason = ""
    if terminal:
        cancel_reason = "任务已经处于终态，不能再取消。"
    elif normalized_review == "pending_review" or status == "review_pending":
        cancel_reason = "任务正在等待人工审核，可通过审核面板驳回或删除审核项。"

    continue_reason = ""
    if status == "failed":
        continue_reason = "失败任务不会自动继续；请先重试或修改参数后重新提交。"
    elif status in {"cancelled", "canceled", "terminated"}:
        continue_reason = "取消或终止任务不会自动继续。"
    elif status not in {"completed", "done", "closed"}:
        continue_reason = "任务尚未完成。"

    return {
        "schema_version": "orchestration.action_policy/v1",
        "lifecycle_status": status,
        "terminal": terminal,
        "active": active,
        "retry": {"available": not bool(retry_blocker), "reason": retry_blocker},
        "resubmit": {"available": not bool(retry_blocker), "reason": retry_blocker},
        "resume": {"available": not bool(resume_blocker), "reason": resume_blocker},
        "cancel": {"available": not bool(cancel_reason), "reason": cancel_reason},
        "continue": {"available": not bool(continue_reason), "reason": continue_reason},
    }


def _plan_dag(plan: Plan) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    completed = plan.completed_step_ids()
    for step in plan.steps:
        task_spec = (
            step.metadata.get("task_spec")
            if isinstance(step.metadata.get("task_spec"), dict)
            else {}
        )
        blocked_by = [dep for dep in step.depends_on if dep not in completed]
        nodes.append(
            {
                "step_id": step.step_id,
                "task_id": str(task_spec.get("task_id", "") or ""),
                "title": step.content,
                "status": step.status.value,
                "depends_on": list(step.depends_on),
                "blocked_by": blocked_by,
                "ready": step.status == StepStatus.PENDING and not blocked_by,
            }
        )
        for dependency in step.depends_on:
            edges.append({"from": dependency, "to": step.step_id})
    return {
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "nodes": nodes,
        "edges": edges,
        "ready_step_ids": [node["step_id"] for node in nodes if node["ready"]],
        "blocked_step_ids": [node["step_id"] for node in nodes if node["blocked_by"]],
    }


class OrchestrationManager:
    """Persist structured plans and reconcile DAG state from SharedTracker."""

    @classmethod
    def _storage_root(cls, workdir: Optional[str | Path] = None) -> Path:
        return _utc_path_workdir(workdir) / ".tcode" / "orchestration" / "plans"

    @classmethod
    def _plan_path(cls, plan_id: str, workdir: Optional[str | Path] = None) -> Path:
        root = cls._storage_root(workdir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        normalized = _normalize_plan_id(plan_id)
        path = (root / f"{normalized}.json").resolve()
        try:
            path.relative_to(root)
        except Exception:
            raise ValueError("orchestration plan_id escapes storage root")
        return path

    @classmethod
    def load_plan(cls, plan_id: str, workdir: Optional[str | Path] = None) -> Optional[Plan]:
        path = cls._plan_path(plan_id, workdir)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        plan_payload = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        try:
            return Plan.from_dict(plan_payload)
        except Exception:
            return None

    @classmethod
    def save_plan(cls, plan: Plan, workdir: Optional[str | Path] = None) -> dict[str, Any]:
        plan.validate()
        plan.metadata["schema_version"] = ORCHESTRATION_SCHEMA_VERSION
        plan.metadata["dag"] = _plan_dag(plan)
        plan.touch()
        payload = {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "plan": plan.to_dict(),
            "dag": plan.metadata["dag"],
            "progress": plan.progress(),
        }
        path = cls._plan_path(plan.plan_id, workdir)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return payload

    @classmethod
    def list_plans(cls, workdir: Optional[str | Path] = None) -> list[dict[str, Any]]:
        root = cls._storage_root(workdir)
        if not root.exists():
            return []
        plans: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                plans.append(payload)
        return plans

    @classmethod
    def ensure_mcp_task_plan(
        cls,
        *,
        identifier: str,
        issue_id: str,
        title: str,
        server_name: str,
        tool_name: str,
        tool_args: dict[str, Any],
        execution_channel: str,
        submit_input: dict[str, Any],
        session_id: str = "",
        workdir: Optional[str | Path] = None,
        plan_id: str = "",
        depends_on: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create or update a structured plan for an async MCP task."""

        resolved_plan_id = _safe_plan_id(plan_id or f"orch-{identifier.lower()}")
        step_id = _safe_plan_id(f"step-{issue_id or identifier}")[:64]
        contract = build_mcp_tool_contract(
            server_name=server_name,
            tool_name=tool_name,
            tool_args=tool_args,
            identifier=identifier,
            execution_channel=execution_channel,
            submit_input=submit_input,
        )
        task_spec = build_task_spec(
            task_id=issue_id,
            step_id=step_id,
            title=title,
            server_name=server_name,
            tool_name=tool_name,
            tool_args=tool_args,
            execution_channel=execution_channel,
            depends_on=depends_on,
            expected_artifacts=contract.get("artifacts", {}).get("expected", []),
            contract=contract,
        )

        plan = cls.load_plan(resolved_plan_id, workdir) or Plan.create(
            goal=title or f"Run {server_name}.{tool_name}",
            session_id=session_id,
            steps=[],
            acceptance_criteria=["All orchestration task specs reach a terminal state."],
            context_summary=f"Structured orchestration plan for {server_name}.{tool_name}.",
        )
        plan.plan_id = resolved_plan_id
        plan.session_id = session_id or plan.session_id
        plan.status = PlanStatus.EXECUTING

        existing_index = next(
            (idx for idx, item in enumerate(plan.steps) if item.step_id == step_id),
            -1,
        )
        step = PlanStep(
            step_id=step_id,
            content=title or f"{server_name}.{tool_name}",
            category=StepCategory.IMPLEMENT,
            status=StepStatus.PENDING,
            depends_on=list(depends_on or []),
            acceptance_criteria=["Task reaches completed, failed, or cancelled with a recorded result contract."],
            assigned_to=str(execution_channel or "orchestrator"),
            metadata={
                "task_spec": task_spec,
                "tool_contract": contract,
            },
        )
        if existing_index >= 0:
            plan.steps[existing_index] = step
        else:
            plan.steps.append(step)
        plan.log_event(
            "task_spec_registered",
            summary=_short_text(title or f"{server_name}.{tool_name}"),
            payload={"step_id": step_id, "task_id": issue_id, "identifier": identifier},
        )
        saved = cls.save_plan(plan, workdir)
        orchestration = {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "plan_id": plan.plan_id,
            "step_id": step_id,
            "task_id": issue_id,
            "identifier": identifier,
            "dependencies": list(depends_on or []),
            "task_spec": task_spec,
            "plan_path": cls._plan_path(plan.plan_id, workdir).as_posix(),
        }
        return {
            "orchestration": orchestration,
            "tool_contract": contract,
            "plan": saved,
        }

    @classmethod
    def _related_tasks(cls, plan_id: str, workdir: Optional[str | Path]) -> list[dict[str, Any]]:
        from ..runtime.shared_tracker import SharedTracker

        tasks = SharedTracker.list_tasks(str(workdir) if workdir is not None else None, limit=5000)
        related = []
        for task in tasks:
            orchestration = _task_orchestration(task)
            if str(orchestration.get("plan_id", "") or "").strip() == plan_id:
                related.append(task)
        return related

    @classmethod
    def reconcile_plan(
        cls,
        plan_id: str,
        workdir: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        """Recompute plan DAG state from SharedTracker task statuses."""

        plan = cls.load_plan(plan_id, workdir)
        if plan is None:
            return {}

        related_tasks = cls._related_tasks(plan.plan_id, workdir)
        by_step: dict[str, dict[str, Any]] = {}
        by_task_id: dict[str, dict[str, Any]] = {}
        for task in related_tasks:
            orchestration = _task_orchestration(task)
            step_id = str(orchestration.get("step_id", "") or "").strip()
            task_id = str(orchestration.get("task_id", "") or task.get("task_id", "") or "").strip()
            if step_id:
                by_step[step_id] = task
            if task_id:
                by_task_id[task_id] = task

        for step in plan.steps:
            task_spec = (
                step.metadata.get("task_spec")
                if isinstance(step.metadata.get("task_spec"), dict)
                else {}
            )
            task = by_step.get(step.step_id) or by_task_id.get(str(task_spec.get("task_id", "") or ""))
            if not task:
                continue
            workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
            status = task.get("status")
            if (workspace.get("cancellation") or {}).get("request_id"):
                status = "failed"
            elif status == "completed" and (task.get("job_ref") or {}).get("job_id") and workspace.get("transfer_state") != "ready":
                status = "running"
            step.status = _task_status_to_step_status(status)
            if task.get("result_summary"):
                step.notes = _short_text(task.get("result_summary"), 1200)
            if task.get("error_summary"):
                step.notes = _short_text(task.get("error_summary"), 1200)
            workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
            artifacts = workspace.get("artifacts") if isinstance(workspace.get("artifacts"), list) else []
            if artifacts:
                step.artifact_refs = [str(item or "").strip() for item in artifacts if str(item or "").strip()]

        statuses = {step.status for step in plan.steps}
        if StepStatus.FAILED in statuses:
            plan.status = PlanStatus.FAILED
        elif plan.steps and statuses <= {StepStatus.COMPLETED, StepStatus.SKIPPED}:
            plan.status = PlanStatus.COMPLETED
        elif StepStatus.IN_PROGRESS in statuses or related_tasks:
            plan.status = PlanStatus.EXECUTING
        elif plan.status == PlanStatus.DRAFT:
            plan.status = PlanStatus.APPROVED

        dag = _plan_dag(plan)
        plan.metadata["dag"] = dag
        plan.metadata["related_task_count"] = len(related_tasks)
        plan.log_event(
            "dag_reconciled",
            summary=f"Ready={len(dag['ready_step_ids'])}, blocked={len(dag['blocked_step_ids'])}",
            payload={
                "ready_step_ids": dag["ready_step_ids"],
                "blocked_step_ids": dag["blocked_step_ids"],
                "progress": plan.progress(),
            },
        )
        return cls.save_plan(plan, workdir)

    @classmethod
    def reconcile_from_task(
        cls,
        task: dict[str, Any],
        workdir: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        orchestration = _task_orchestration(task)
        plan_id = str(orchestration.get("plan_id", "") or "").strip()
        if not plan_id:
            return {}
        return cls.reconcile_plan(plan_id, workdir)
