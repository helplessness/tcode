"""Team coordinator - async task dispatch for Workers"""

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .registry import WorkerRegistry
from .channel import TeamChannel
from .catalog import TeamCatalog
from .worker_scoper import WorkerScoper
from .memory import TeamMemoryManager
from .worker_memory import WorkerMemoryManager
from .profile import score_worker_for_task
from ..session.manager import SessionManager
from ..subtask_handoff import (
    SubtaskHandoff,
    SubtaskHandoffError,
    build_subtask_handoff,
    build_subtask_tool_trace,
    completed_subtask_work,
    failed_subtask_work,
    merge_subtask_tool_traces,
    report_revision_evidence,
)
from ..providers import create_provider
from ..agent import Agent
from ..agent.agents import BuiltinAgents
from ..governance.biosafety import (
    BiosafetyGateResult,
    biosafety_human_review_arguments,
    biosafety_scope_hash,
    build_risk_review_contract,
    evaluate_worker_dispatch,
    record_risk_review,
)
from ..review import run_review_cycle
from ..runtime import (
    get_execution_profile_char_budget,
    normalize_execution_profile,
    RuntimeArtifactManager,
    RuntimeTaskManager,
    max_execution_retries,
    retry_decision_for_exception,
    run_postflight_checks,
    run_preflight_checks,
)
from ..runtime.question_broker import build_parent_question_bridge
from ..runtime.capacity import CapacityRejected, ExecutionCapacityController
from ..task.state_graph import TaskStateGraphManager
from ..types import Message
from ..workspace import AgentWorkspaceManager

# Global task store to prevent GC of background tasks
_active_tasks: Dict[str, asyncio.Task] = {}
_active_worker_agents: Dict[str, Agent] = {}
_pending_manager_async_summaries: Dict[str, list[dict[str, str]]] = {}
_manager_async_digest_tasks: Dict[str, asyncio.Task] = {}
_manager_goal_loop_tasks: Dict[str, asyncio.Task] = {}


def _message_role_value(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role) or "").strip().lower()


async def _team_biosafety_scope_text(team_id: str, fallback: str) -> str:
    """Use the root user request as the stable scope shared by all workers."""

    try:
        session = await SessionManager.get(team_id)
    except Exception:
        session = None
    for message in reversed(list(getattr(session, "messages", []) or [])):
        if _message_role_value(message) != "user":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return str(fallback or "").strip()


def _with_biosafety_resource_context(
    resource_context: dict[str, Any],
    *,
    team_id: str,
    scope_hash: str,
) -> dict[str, Any]:
    return {
        **dict(resource_context or {}),
        "_biosafety": {
            "team_id": str(team_id or "").strip(),
            "scope_hash": str(scope_hash or "").strip(),
        },
    }


def _biosafety_gate_message(result: BiosafetyGateResult) -> str:
    if result.action == "review_required":
        next_step = "请先调用 risk Worker（wait=true）完成同一用户目标的结构化风险审查。"
    elif result.action == "human_review_required":
        next_step = "该范围需要人工审查；在获得明确批准前不得继续设计或实验任务。"
    else:
        next_step = "该范围已被生物安全策略阻断，不得继续设计、实验或敏感工具调用。"
    return f"生物安全 Gate 阻断了任务：{result.reason}\n{next_step}"


def _record_completed_risk_review(
    *,
    workdir: str,
    team_id: str,
    scope_hash: str,
    scope_text: str,
    reviewer_session_id: str,
    handoff: SubtaskHandoff,
) -> BiosafetyGateResult:
    return record_risk_review(
        workdir=workdir,
        team_id=team_id,
        scope_hash=scope_hash,
        scope_text=scope_text,
        result_text=handoff.result_text,
        reviewer_session_id=reviewer_session_id,
    )


async def _enqueue_biosafety_human_review(
    *,
    workdir: str,
    team_id: str,
    scope_hash: str,
    result: BiosafetyGateResult,
) -> dict[str, Any]:
    decision = result.decision
    if decision is None or result.action != "human_review_required":
        return {}
    try:
        from ..mcp._review import enqueue_human_review_issue
        from ..tools.base import ToolContext

        review_context = ToolContext(
            session_id=team_id,
            message_id="biosafety-review",
            agent="protein_risk",
            abort=None,
            extra={
                "workdir": workdir,
                "resource_context": {
                    "_biosafety": {
                        "team_id": team_id,
                        "scope_hash": scope_hash,
                    }
                },
            },
        )
        return await enqueue_human_review_issue(
            server_name="biosafety-gate",
            tool_name="approve_scope",
            args=biosafety_human_review_arguments(
                team_id=team_id,
                scope_hash=scope_hash,
                decision_id=decision.decision_id,
            ),
            context=review_context,
            review_check={
                "reason": decision.reason or "该蛋白设计范围需要人工生物安全审查",
                "trigger": "biosafety_risk_worker_escalation",
                "check_type": "biosafety_scope",
            },
            review_data_extra={
                "biosafety_decision": decision.metadata(),
                "biosafety_scope_hash": scope_hash,
            },
        )
    except Exception:
        return {}


async def cancel_team_background_tasks(team_id: str) -> dict[str, list[str]]:
    """Best-effort cancel of coordinator-owned background tasks for a team session."""
    normalized_team_id = str(team_id or "").strip()
    cancelled_worker_session_ids: list[str] = []
    cancelled_auxiliary_tasks: list[str] = []
    if not normalized_team_id:
        return {
            "cancelled_worker_session_ids": cancelled_worker_session_ids,
            "cancelled_auxiliary_tasks": cancelled_auxiliary_tasks,
        }

    try:
        workers = list(WorkerRegistry.list_for_team(normalized_team_id) or [])
    except Exception:
        workers = []

    for worker in workers:
        worker_session_id = str(getattr(worker, "session_id", "") or "").strip()
        if not worker_session_id:
            continue
        if cancel_worker_task(worker_session_id):
            cancelled_worker_session_ids.append(worker_session_id)

    digest_task = _manager_async_digest_tasks.get(normalized_team_id)
    if digest_task and not digest_task.done():
        digest_task.cancel()
        cancelled_auxiliary_tasks.append("manager_async_digest")

    goal_loop_task = _manager_goal_loop_tasks.get(normalized_team_id)
    if goal_loop_task and not goal_loop_task.done():
        goal_loop_task.cancel()
        cancelled_auxiliary_tasks.append("manager_goal_loop")

    _pending_manager_async_summaries.pop(normalized_team_id, None)

    return {
        "cancelled_worker_session_ids": cancelled_worker_session_ids,
        "cancelled_auxiliary_tasks": cancelled_auxiliary_tasks,
    }


def cancel_worker_task(worker_session_id: str) -> bool:
    """Cancel one assign_task worker by its child session id."""
    normalized = str(worker_session_id or "").strip()
    if not normalized:
        return False
    agent = _active_worker_agents.get(normalized)
    if agent is not None:
        agent.stop()
    task = _active_tasks.get(normalized)
    if not task or task.done():
        return agent is not None
    task.cancel()
    return True


async def _wait_for_previous_worker_task(task: Optional[asyncio.Task]) -> None:
    if task and not task.done():
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)


def _clear_active_worker_task(worker_session_id: str, task: asyncio.Task) -> None:
    if _active_tasks.get(worker_session_id) is task:
        _active_tasks.pop(worker_session_id, None)


def worker_task_active(worker_session_id: str) -> bool:
    """Return whether the coordinator still owns a live task for this worker."""

    normalized = str(worker_session_id or "").strip()
    if not normalized:
        return False
    task = _active_tasks.get(normalized)
    if task is None:
        return False
    if task.done():
        _active_tasks.pop(normalized, None)
        return False
    return True


async def reconcile_orphaned_worker_runtime_task(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Release a persisted busy worker when its in-process task is gone."""

    current = dict(record or {})
    status = str(current.get("status", "") or "").strip().lower()
    if status not in {"queued", "running", "reviewing", "revising", "recovering", "paused", "pause_requested"}:
        return current
    worker_session_id = str(current.get("worker_session_id", "") or "").strip()
    if not worker_session_id or worker_task_active(worker_session_id):
        return current

    worker = WorkerRegistry.get(worker_session_id)
    if worker is not None and str(getattr(worker, "status", "") or "").lower() in {
        "busy",
        "running",
    }:
        await WorkerRegistry.update(
            worker_session_id,
            status="idle",
            current_task=None,
        )

    task_id = str(current.get("task_id", "") or "").strip()
    if not task_id:
        return current
    reason = (
        "Delegated worker execution is no longer active; the server or parent "
        "turn ended before the persisted runtime task reached a terminal state."
    )
    try:
        return RuntimeTaskManager.transition(
            task_id,
            status="blocked",
            phase="interrupted_recovery",
            message="Recovered an orphaned delegated runtime task.",
            next_action="Retry or explicitly resume the delegated task.",
            last_error=reason,
        )
    except Exception:
        current["status"] = "blocked"
        current["phase"] = "interrupted_recovery"
        current["last_error"] = reason
        return current


def _int_from_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


WORKER_EXECUTION_PROMPT_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_EXECUTION_PROMPT_MAX_CHARS",
    18000,
    minimum=4000,
    maximum=60000,
)
WORKER_MEMORY_PROMPT_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_MEMORY_PROMPT_MAX_CHARS",
    5000,
    minimum=1000,
    maximum=20000,
)
WORKER_RESULT_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_RESULT_MAX_CHARS",
    12000,
    minimum=2000,
    maximum=40000,
)
WORKER_REVIEW_TASK_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_REVIEW_TASK_MAX_CHARS",
    9000,
    minimum=2000,
    maximum=30000,
)
WORKER_REVIEW_RESULT_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_REVIEW_RESULT_MAX_CHARS",
    12000,
    minimum=2000,
    maximum=40000,
)
WORKER_RETRY_RESULT_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_RETRY_RESULT_MAX_CHARS",
    8000,
    minimum=1500,
    maximum=30000,
)
WORKER_TASK_DONE_MAX_CHARS = _int_from_env(
    "TCODE_WORKER_TASK_DONE_MAX_CHARS",
    2000,
    minimum=400,
    maximum=12000,
)


def _budget_chars(
    execution_profile: Optional[str],
    budget_name: str,
    default: int,
    *,
    minimum: int = 200,
    maximum: Optional[int] = None,
) -> int:
    return get_execution_profile_char_budget(
        execution_profile,
        budget_name,
        default,
        minimum=minimum,
        maximum=maximum,
    )


def _truncate_for_prompt(text: str, max_chars: int, *, label: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    limit = max(200, int(max_chars or 0))
    if len(normalized) <= limit:
        return normalized
    marker = f"\n\n[... {label} 已截断，省略 {len(normalized) - limit} 个字符 ...]\n\n"
    if len(marker) >= limit:
        return normalized[:limit]
    available = limit - len(marker)
    head = max(120, int(available * 0.72))
    tail = max(60, available - head)
    omitted = max(0, len(normalized) - head - tail)
    marker = f"\n\n[... {label} 已截断，省略 {omitted} 个字符 ...]\n\n"
    available = limit - len(marker)
    if available <= 0:
        return normalized[:limit]
    head = max(80, int(available * 0.72))
    tail = max(40, available - head)
    return normalized[:head].rstrip() + marker + normalized[-tail:].lstrip()


def _truncate_tool_trace(tool_trace: list[Any] | None, *, limit: int = 24) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    source_evidence: list[str] = []
    source_seen: set[tuple[str, str]] = set()
    trace = list(tool_trace or [])
    revisions = report_revision_evidence([item for item in trace if isinstance(item, dict)])
    latest_reads = {}
    for item in trace:
        if isinstance(item, dict) and item.get('tool_name') == 'read' and not item.get('error_preview') and str(item.get('state', '')).lower() in {'completed', 'complete', 'success', 'succeeded'}:
            args = item.get('arguments') or {}
            latest_reads[str(args.get('filePath') or '')] = item.get('call_id')
    for item in trace:
        if isinstance(item, dict):
            item = dict(item)
            excerpts = item.pop("source_read_excerpts", [])
            if item.get("tool_name") != "read" or str(item.get("state", "")).lower() not in {"completed", "complete", "success", "succeeded"} or item.get("error_preview"):
                excerpts = []
            arguments = item.get("arguments") or {}
            path = str(arguments.get("filePath") or arguments.get("file_path") or "") if isinstance(arguments, dict) else ""
            if item.get('call_id') != latest_reads.get(path):
                excerpts = []  # Old report versions must not masquerade as final evidence.
            # One read is one evidence entry. Chunk-count limits must not let
            # early sources crowd out the final reports' complete saved text.
            excerpts = [''.join(str(part) for part in excerpts)[:9600]] if isinstance(excerpts, list) and excerpts else []
            for index, excerpt in enumerate(excerpts if isinstance(excerpts, list) else []):
                key = (path, str(excerpt))
                if len(source_evidence) >= min(16, max(0, limit - 2)) or key in source_seen:
                    continue
                source_seen.add(key)
                source_evidence.append(json.dumps({"source_path": path, "read_call_id": item.get("call_id"),
                    "source_read_excerpt": str(excerpt)[:9600], "excerpt_index": index,
                    "source_total_chars": item.get("source_read_total_chars")}, ensure_ascii=False))
            try:
                value = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                value = str(item or "")
        else:
            value = str(item or "")
        value = _truncate_for_prompt(
            value.strip(),
            2400,
            label="review tool trace item",
        )
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    prefix = [json.dumps(revisions, ensure_ascii=False)] if revisions['files'] and limit else []
    remaining = max(0, limit - len(source_evidence) - len(prefix))
    return prefix + (values[-remaining:] if remaining else []) + source_evidence


def _sanitize_worker_result_text(text: str, execution_profile: Optional[str] = None) -> str:
    return _truncate_for_prompt(
        text,
        _budget_chars(
            execution_profile,
            "worker_result_chars",
            WORKER_RESULT_MAX_CHARS,
            minimum=2000,
        ),
        label="worker result",
    )


def _normalize_allowed_tools(allowed_tools: Any) -> str | list[str]:
    from ..tools.registry import ToolRegistry

    return ToolRegistry.normalize_allowlist(allowed_tools)


def _normalize_resource_context(raw: Any) -> dict[str, Any]:
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {"items": raw}
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        except Exception:
            pass
    return {"notes": text}


def _merge_resource_context(
    inherited: Optional[dict[str, Any]],
    explicit: Optional[dict[str, Any]],
) -> dict[str, Any]:
    base = dict(inherited or {}) if isinstance(inherited, dict) else {}
    update = dict(explicit or {}) if isinstance(explicit, dict) else {}
    if not update:
        return base
    if not base:
        return update

    merged = dict(base)

    if "uploaded_files" in update:
        deduped_files: dict[str, dict[str, Any]] = {}
        for item in [*(base.get("uploaded_files") or []), *(update.get("uploaded_files") or [])]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            deduped_files[path] = dict(item)
        merged["uploaded_files"] = list(deduped_files.values())

    if "paper_inputs" in update and isinstance(update.get("paper_inputs"), dict):
        current_inputs = dict(base.get("paper_inputs") or {}) if isinstance(base.get("paper_inputs"), dict) else {}
        next_inputs = dict(update.get("paper_inputs") or {})
        merged_inputs = dict(current_inputs)
        for field in (
            "input_count",
            "pdf_count",
            "requires_ingest",
        ):
            if field in next_inputs:
                merged_inputs[field] = next_inputs[field]
        deduped_inputs: dict[str, dict[str, Any]] = {}
        for item in [*(current_inputs.get("inputs") or []), *(next_inputs.get("inputs") or [])]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            deduped_inputs[path] = dict(item)
        if deduped_inputs:
            merged_inputs["inputs"] = list(deduped_inputs.values())
        merged["paper_inputs"] = merged_inputs

    for key, value in update.items():
        if key in {"uploaded_files", "paper_inputs"}:
            continue
        merged[key] = value
    return merged


def _build_task_contract(task_prompt: str, overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    prompt = str(task_prompt or "").strip()
    payload = dict(overrides or {}) if isinstance(overrides, dict) else {}
    return {
        "goal": str(payload.get("goal") or prompt[:240]).strip(),
        "deliverable": str(payload.get("deliverable") or "给出该任务的最终结果和必要的关键信息。").strip(),
        "stop_when": str(payload.get("stop_when") or "结果已经足以交回 Manager，且无需继续补充关键步骤。").strip(),
        "allowed_tools": _normalize_allowed_tools(payload.get("allowed_tools", "all")),
    }


def _serialize_resource_context_for_prompt(resource_context: dict[str, Any]) -> str:
    if not resource_context:
        return ""

    lines: list[str] = []
    uploaded_files = resource_context.get("uploaded_files")
    if isinstance(uploaded_files, list) and uploaded_files:
        lines.append("uploaded_files:")
        for item in uploaded_files[:12]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            kind = str(item.get("kind") or "file").strip() or "file"
            lines.append(f"- {path} ({kind})")

    paper_inputs = resource_context.get("paper_inputs")
    if isinstance(paper_inputs, dict) and int(paper_inputs.get("input_count", 0) or 0) > 0:
        lines.append(
            f"paper_inputs: input_count={int(paper_inputs.get('input_count', 0) or 0)}, "
            f"pdf_count={int(paper_inputs.get('pdf_count', 0) or 0)}, "
            f"requires_ingest={bool(paper_inputs.get('requires_ingest', False))}"
        )
        for item in list(paper_inputs.get("inputs") or [])[:12]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            kind = str(item.get("kind") or "other").strip() or "other"
            pdf_count = int(item.get("pdf_count", 0) or 0)
            lines.append(f"  - {path} ({kind}, pdf_count={pdf_count})")

    paper_library_refs = resource_context.get("paper_library_refs")
    if isinstance(paper_library_refs, list) and paper_library_refs:
        lines.append("paper_library_refs (retrieve with Paper Library RAG; do not inline full text):")
        for item in paper_library_refs[:16]:
            if isinstance(item, dict) and str(item.get("doc_id") or "").strip():
                lines.append(f"- doc_id={str(item.get('doc_id')).strip()}")

    for key, value in resource_context.items():
        if key in {"uploaded_files", "paper_inputs", "paper_library_refs"}:
            continue
        if isinstance(value, (dict, list)):
            try:
                serialized = json.dumps(value, ensure_ascii=False)
            except Exception:
                serialized = str(value)
        else:
            serialized = str(value)
        if serialized.strip():
            lines.append(f"{key}: {serialized[:600]}")

    return "\n".join(lines).strip()


def _extract_pdf_paths(resource_context: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    uploaded_files = resource_context.get("uploaded_files")
    if isinstance(uploaded_files, list):
        for item in uploaded_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
            if path and (path.lower().endswith(".pdf") or kind == "pdf"):
                paths.append(path)

    paper_inputs = resource_context.get("paper_inputs")
    if isinstance(paper_inputs, dict):
        for item in list(paper_inputs.get("inputs") or []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
            if path and (path.lower().endswith(".pdf") or kind == "pdf"):
                paths.append(path)

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _build_execution_prompt(
    task_prompt: str,
    contract: dict[str, Any],
    resource_context: dict[str, Any],
    execution_profile: Optional[str] = None,
) -> str:
    execution_budget = _budget_chars(
        execution_profile,
        "worker_execution_prompt_chars",
        WORKER_EXECUTION_PROMPT_MAX_CHARS,
        minimum=4000,
    )
    allowed_tools = contract.get("allowed_tools", "all")
    tool_text = "all" if allowed_tools == "all" else ", ".join(allowed_tools)
    parts = [
        "# Task Contract",
        f"- goal: {contract.get('goal', '')}",
        f"- deliverable: {contract.get('deliverable', '')}",
        f"- stop_when: {contract.get('stop_when', '')}",
        f"- allowed_tools: {tool_text}",
        '- 事实边界：Task Contract 是任务分工，不是事实证据；其中的解释、预设结论和停止条件须与用户原始要求及来源分别核对，证据不足时明确局限，不把请求取消写成已确认取消。',
    ]
    resource_text = _serialize_resource_context_for_prompt(resource_context)
    if resource_text:
        parts.extend(["", "# Resource Context", resource_text])
    has_pdf_resource = False
    uploaded_files = resource_context.get("uploaded_files")
    if isinstance(uploaded_files, list):
        for item in uploaded_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip().lower()
            kind = str(item.get("kind") or "").strip().lower()
            if path.endswith(".pdf") or kind == "pdf":
                has_pdf_resource = True
                break
    if not has_pdf_resource:
        paper_inputs = resource_context.get("paper_inputs")
        if isinstance(paper_inputs, dict):
            has_pdf_resource = int(paper_inputs.get("pdf_count", 0) or 0) > 0
            if not has_pdf_resource:
                for item in list(paper_inputs.get("inputs") or []):
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip().lower()
                    kind = str(item.get("kind") or "").strip().lower()
                    if path.endswith(".pdf") or kind == "pdf":
                        has_pdf_resource = True
                        break
    if has_pdf_resource:
        pdf_paths = _extract_pdf_paths(resource_context)
        parts.extend(
            [
                "",
                "# PDF Execution Hints",
                "- 当前任务带有本地 PDF 资源。",
                "- 如果 PDF 是 workspace 外的宿主机路径，先调用 `import_resource` 获得受控的 `/workspace/.external/...` 路径，再交给 PDF skill；不要因路径隔离误判为文件不存在。",
                "- 不要直接 `read` 原始 `.pdf` 文件；先调用 `skill(name=\"pdf\")` 加载 PDF skill。",
                "- 不要使用 `webfetch` 访问 `file://` 本地路径；这是错误路径，本地 PDF 应直接用绝对路径交给脚本。",
                "- 必须通过 `skill_run(skill=\"pdf\", action=...)` 执行已声明 action；不要自写 Python，不要直接执行 skill 目录里的脚本。",
                "- 顺序为 `extract-text` -> `extract-tables`；如果 table_count=0 但正文出现 TABLE/表格标题，继续执行 `render-pages` 并对命中页执行 `ocr-image`/图片核对，不得转为外部搜索。",
                "- 不要猜测解释器、脚本名或宿主机路径；`skill_run` 负责 sandbox 挂载和路径映射。",
            ]
        )
        if pdf_paths:
            target_pdf = pdf_paths[0]
            parts.extend(
                [
                    "",
                    "# PDF Skill Target",
                    f"- target_pdf: {target_pdf}",
                    "- 输出必须写入当前 workspace 内的 artifacts 目录。",
                ]
            )
    parts.extend(
        [
            "",
            "# Original Task",
            _truncate_for_prompt(
                str(task_prompt or "").strip(),
                max(800, execution_budget // 2),
                label="original task",
            ),
        ]
    )
    return _truncate_for_prompt(
        "\n".join(parts).strip(),
        execution_budget,
        label="worker execution prompt",
    )


def _build_task_handoff(
    task_prompt: str,
    task_spec: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(task_spec or {}) if isinstance(task_spec, dict) else {}
    contract = _build_task_contract(task_prompt, payload.get("contract"))
    resource_context = _merge_resource_context(
        {},
        _normalize_resource_context(payload.get("resources")),
    )
    execution_profile = normalize_execution_profile(
        payload.get("execution_profile") or payload.get("profile")
    )
    execution_prompt = _build_execution_prompt(
        task_prompt,
        contract,
        resource_context,
        execution_profile=execution_profile,
    )
    return {
        "task_prompt": str(task_prompt or "").strip(),
        "contract": contract,
        "resources": resource_context,
        "execution_prompt": execution_prompt,
        "execution_profile": execution_profile,
    }


def _write_runtime_artifacts(
    *,
    session_id: str,
    task_id: str,
    workdir: str,
    contract: dict[str, Any],
    summary: str,
    result: dict[str, Any],
    tool_trace: list[dict[str, Any]],
) -> dict[str, str]:
    return RuntimeArtifactManager.write_bundle(
        session_id=session_id,
        task_id=task_id,
        workdir=workdir,
        contract=contract,
        summary=summary,
        result=result,
        tool_trace=tool_trace,
    )


def _attach_subtask_handoff_artifacts(
    *,
    team_id: str,
    runtime_task_id: str,
    workdir: str,
    contract: dict[str, Any],
    resource_context: dict[str, Any],
    handoff: SubtaskHandoff,
) -> None:
    handoff_text = handoff.render_for_parent()
    RuntimeTaskManager.attach_artifacts(
        runtime_task_id,
        _write_runtime_artifacts(
            session_id=team_id,
            task_id=runtime_task_id,
            workdir=workdir,
            contract={
                **contract,
                **({"resources": resource_context} if resource_context else {}),
            },
            summary=handoff_text[:1000],
            result={
                "status": handoff.outcome_status,
                "review_verdict": handoff.review_verdict,
                "next_action": handoff.next_action,
                "worker_name": handoff.agent_name,
                "subtask_handoff": handoff.to_dict(),
            },
            tool_trace=handoff.tool_trace,
        ),
    )


async def _append_manager_async_summary(
    *,
    team_id: str,
    worker_name: str,
    task_prompt: str,
    summary_text: str,
    status: str,
    review_verdict: str = "",
    recommended_next_step: str = "",
) -> None:
    team_session = await SessionManager.get(team_id)
    if not team_session:
        return

    normalized_status = str(status or "").strip().lower() or "completed"
    verdict_text = str(review_verdict or "").strip().upper()
    short_task = str(task_prompt or "").strip()[:140]
    short_summary = str(summary_text or "").strip()[:1200]
    short_next = str(recommended_next_step or "").strip()[:240]
    is_manager_digest = str(worker_name or "").strip().lower() == "manager"

    if is_manager_digest:
        header = "异步任务批量汇总"
        lines = [header]
    else:
        header = {
            "completed": "后台异步任务完成",
            "partial": "后台异步任务部分完成",
            "blocked": "后台异步任务受阻",
            "cancelled": "后台异步任务已取消",
        }.get(normalized_status, "后台异步任务失败")
        lines = [
            f"{header}：Worker `{worker_name}` 已返回结果。",
            f"任务：{short_task}",
        ]
    if verdict_text:
        lines.append(f"Review Verdict：{verdict_text}")
    if short_summary:
        lines.append(f"结果摘要：{short_summary}")
    if short_next:
        lines.append(f"建议下一步：{short_next}")
    lines.append("这是异步任务的自动回传汇总，后续可继续让我基于这个结果推进。")
    content = "\n".join(lines)

    existing_messages = list(getattr(team_session, "messages", []) or [])
    if existing_messages:
        last = existing_messages[-1]
        if getattr(last, "role", "") == "assistant" and str(getattr(last, "content", "") or "").strip() == content:
            return

    assistant_msg = Message(
        role="assistant",
        content=content,
        id=str(uuid.uuid4()),
    )
    next_messages = [*existing_messages, assistant_msg]
    await SessionManager.update(
        team_id,
        messages=next_messages,
        last_active_at=float(datetime.now().timestamp()),
    )

    try:
        from ..server.app import ServerState

        runtime_agent = ServerState.agents.get(team_id)
        if runtime_agent is not None:
            runtime_agent.messages = list(next_messages)

        runtime_session = ServerState.sessions.get(team_id)
        if runtime_session is not None:
            runtime_session["messages"] = list(next_messages)
    except Exception:
        pass


async def _schedule_manager_async_digest(
    *,
    team_id: str,
    worker_name: str,
    task_prompt: str,
    status: str,
    review_verdict: str = "",
    recommended_next_step: str = "",
) -> None:
    bucket = _pending_manager_async_summaries.setdefault(team_id, [])
    bucket.append(
        {
            "worker_name": str(worker_name or "").strip(),
            "task_prompt": str(task_prompt or "").strip()[:140],
            "status": str(status or "").strip().lower() or "completed",
            "review_verdict": str(review_verdict or "").strip().upper(),
            "recommended_next_step": str(recommended_next_step or "").strip()[:200],
        }
    )

    current_task = _manager_async_digest_tasks.get(team_id)
    if current_task and not current_task.done():
        return

    async def _emit_digest() -> None:
        try:
            await asyncio.sleep(2.5)
            items = list(_pending_manager_async_summaries.pop(team_id, []))
            if len(items) < 2:
                return

            completed = [item for item in items if item["status"] == "completed"]
            partial = [item for item in items if item["status"] == "partial"]
            failed = [
                item
                for item in items
                if item["status"] not in {"completed", "partial"}
            ]
            lines = [
                "异步任务阶段汇总：多条后台任务已完成一轮回传。",
                (
                    f"完成 {len(completed)} 项，部分完成 {len(partial)} 项，"
                    f"失败或受阻 {len(failed)} 项。"
                ),
            ]
            for item in items[:6]:
                segment = f"- `{item['worker_name']}` · {item['status']} · {item['task_prompt']}"
                if item["review_verdict"]:
                    segment += f" · verdict={item['review_verdict']}"
                lines.append(segment)

            next_steps: list[str] = []
            for item in items:
                step = item["recommended_next_step"]
                if step and step not in next_steps:
                    next_steps.append(step)
            if next_steps:
                lines.append("建议下一步：")
                for step in next_steps[:4]:
                    lines.append(f"- {step}")
            lines.append("如果需要，我现在可以基于这些异步结果继续统一规划下一步。")

            await _append_manager_async_summary(
                team_id=team_id,
                worker_name="manager",
                task_prompt="批量异步任务汇总",
                summary_text="\n".join(lines),
                status="completed",
                review_verdict="BATCH",
                recommended_next_step=next_steps[0] if next_steps else "",
            )
        finally:
            _manager_async_digest_tasks.pop(team_id, None)

    digest_task = asyncio.create_task(_emit_digest())
    _manager_async_digest_tasks[team_id] = digest_task


def _auto_reassign_enabled() -> bool:
    raw = str(os.environ.get("TCODE_AUTO_REASSIGN_ON_FAILURE", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _max_auto_reassign_hops() -> int:
    raw = str(os.environ.get("TCODE_AUTO_REASSIGN_MAX_HOPS", "1") or "1").strip()
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(0, min(3, value))


def _max_review_retries() -> int:
    raw = str(os.environ.get("TCODE_REVIEW_MAX_RETRIES", "1") or "1").strip()
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(0, min(5, value))


def _worker_review_timeout_seconds() -> float:
    raw = str(os.environ.get("TCODE_WORKER_REVIEW_TIMEOUT_SECONDS", "180") or "180").strip()
    try:
        value = float(raw)
    except Exception:
        value = 180.0
    return max(10.0, min(value, 1800.0))


def _worker_review_fail_open() -> bool:
    raw = str(os.environ.get("TCODE_WORKER_REVIEW_FAIL_OPEN", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _worker_requires_review(worker_name: str) -> bool:
    """Deterministic paper persistence tools already return auditable metadata."""

    return str(worker_name or "").strip().lower() not in {
        "paper_library_manager",
        "paper_synthesis",
    }


def _max_total_rework_retries() -> Optional[int]:
    raw = str(os.environ.get("TCODE_TOTAL_REWORK_MAX_RETRIES", "") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    if value < 0:
        return None
    return max(0, min(20, value))


def _current_total_rework_count(runtime_task_id: str) -> int:
    record = RuntimeTaskManager.get(runtime_task_id) or {}
    try:
        return max(0, int(record.get("total_rework_count", 0) or 0))
    except Exception:
        return 0


def _consume_total_rework_budget(
    runtime_task_id: str,
) -> tuple[bool, int, Optional[int]]:
    record = RuntimeTaskManager.get(runtime_task_id) or {}
    raw_limit = record.get("total_rework_limit")
    if raw_limit in (None, ""):
        limit = _max_total_rework_retries()
    else:
        try:
            parsed_limit = int(raw_limit)
        except Exception:
            parsed_limit = -1
        limit = parsed_limit if parsed_limit >= 0 else None
    current = _current_total_rework_count(runtime_task_id)
    if limit is not None and current >= limit:
        return False, current, limit
    next_count = current + 1
    RuntimeTaskManager.update_counters(
        runtime_task_id,
        total_rework_count=next_count,
    )
    return True, next_count, limit


def _total_rework_budget_message(current: int, limit: Optional[int]) -> str:
    if limit is None:
        return f"当前累计返工 {current} 次。"
    return f"当前累计返工 {current}/{limit} 次。"


def _guard_worker(worker, worker_name: str):
    """Return error string if worker cannot accept tasks, else None."""
    if not worker:
        return f"❌ Worker '{worker_name}' 不存在，请先用 create_worker 创建"
    catalog_worker = TeamCatalog.get_worker_by_session(
        str(getattr(worker, "session_id", "") or ""),
        str(getattr(worker, "name", "") or worker_name),
    )
    if catalog_worker is not None and not catalog_worker.enabled:
        return f"❌ Worker '{worker_name}' 已在团队配置中禁用，无法接受新任务"
    if worker.status == "terminated":
        return f"❌ Worker '{worker_name}' 已终止，无法接受新任务"
    if worker.status == "busy":
        return f"⚠️ Worker '{worker_name}' 正在执行任务，请等待完成后再分配"
    return None


def _worker_workspace_profile(worker: Any, session: Any) -> tuple[str, str]:
    """Return an explicit execution domain/strategy for a delegated worker.

    Worker display names are deliberately not inspected here.  A fixed or
    plugin team declares its execution environment in persisted session or
    catalog metadata, so aliases such as ``worker_*`` cannot accidentally
    switch a scientific worker to a Git worktree.
    """

    metadata = dict(getattr(session, "metadata", {}) or {})
    catalog_worker = TeamCatalog.get_worker_by_session(
        str(getattr(worker, "session_id", "") or ""),
        str(getattr(worker, "name", "") or ""),
    )
    catalog_metadata = dict(getattr(catalog_worker, "metadata", {}) or {})
    domain = str(
        metadata.get("execution_domain")
        or catalog_metadata.get("execution_domain")
        or catalog_metadata.get("domain")
        or ""
    ).strip()
    strategy = str(
        metadata.get("workspace_strategy")
        or catalog_metadata.get("workspace_strategy")
        or catalog_metadata.get("strategy")
        or "auto"
    ).strip() or "auto"
    return domain, strategy


def _make_runtime_task_id(worker_name: str) -> str:
    return f"{str(worker_name or 'worker').strip().lower()}-{uuid.uuid4().hex[:8]}"


def _task_control_metadata(
    runtime_task_id: str,
    contract: dict[str, Any],
    task_spec: Optional[dict[str, Any]],
    *,
    parent_task_id: str = "",
) -> dict[str, Any]:
    """Build the durable goal lineage and task budget without a second scheduler."""

    spec = dict(task_spec or {})
    parent_id = str(spec.get("parent_task_id") or parent_task_id or "").strip()
    parent = RuntimeTaskManager.get(parent_id) if parent_id else None
    inherited = list((parent or {}).get("goal_ancestry") or [])
    if parent_id and parent_id not in inherited:
        inherited.append(parent_id)
    raw_budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
    raw_limits = raw_budget.get("limits") if isinstance(raw_budget.get("limits"), dict) else raw_budget
    return {
        "goal_id": str(
            spec.get("goal_id")
            or (parent or {}).get("goal_id")
            or runtime_task_id
        ),
        "parent_task_id": parent_id,
        "goal_ancestry": inherited,
        "goal": str(spec.get("goal") or contract.get("goal") or ""),
        "wake_reason": "task_assigned",
        "budget": {
            "limits": {
                "tokens": int(raw_limits.get("tokens") or os.environ.get("TCODE_TASK_TOKEN_BUDGET", "0")),
                "tool_calls": int(raw_limits.get("tool_calls") or os.environ.get("TCODE_TASK_TOOL_CALL_BUDGET", "0")),
                "retries": int(raw_limits.get("retries") or os.environ.get("TCODE_TASK_RETRY_BUDGET", "0")),
            }
        },
    }


def _record_task_retries(
    runtime_task_id: str,
    handoff: SubtaskHandoff,
) -> None:
    recorded = (RuntimeTaskManager.get(runtime_task_id) or {}).get('budget', {}).get('usage', {}).get('retries', 0)
    retries = max(0, int(handoff.execution_attempts or 0) - 1) + max(0, int(handoff.review_attempts or 0) - 1)
    RuntimeTaskManager.record_usage(
        runtime_task_id,
        retries=max(0, retries - recorded),
    )


def _infer_task_type(task_prompt: str) -> str:
    text = str(task_prompt or "").lower()
    if any(word in text for word in ["review", "verify", "audit", "检查", "复核"]):
        return "review"
    if any(word in text for word in ["design", "protein", "结构", "序列"]):
        return "domain_design"
    if any(word in text for word in ["code", "implement", "fix", "编码", "实现", "修复"]):
        return "coding"
    if any(word in text for word in ["research", "analysis", "调研", "分析"]):
        return "analysis"
    return "general"


def _infer_review_level(task_prompt: str) -> str:
    text = str(task_prompt or "").strip().lower()
    if not text:
        return "medium"

    high_markers = [
        "执行",
        "运行",
        "修改代码",
        "改代码",
        "写文件",
        "删除",
        "部署",
        "搜索",
        "联网",
        "实验",
        "合规",
        "风险",
        "蛋白",
        "结构",
        "代码",
        "implement",
        "fix",
        "edit",
        "write file",
        "websearch",
        "search",
        "execute",
        "run command",
        "compliance",
        "safety",
        "protein",
    ]
    if any(marker in text for marker in high_markers):
        return "high"

    # Brevity is an output preference, not evidence of a low-risk task.
    simple_request = text.strip(" 。.!！?？").removeprefix("请")
    if simple_request in {
        "打个招呼", "问好", "打招呼", "hello world", "hello word",
        "输出一个hello", "输出 hello", "say hello", "greet",
    }:
        return "low"

    return "medium"


def _infer_constraints(task_prompt: str) -> list[str]:
    parts = re.split(r"[;；\n]", str(task_prompt or ""))
    return [part.strip() for part in parts if part.strip()][:6]




def _stringify_recovered_worker_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2).strip()
        except Exception:
            return str(value).strip()
    return str(value).strip()


def _normalize_recovered_worker_output(tool_name: str, text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    if tool_name == "team_memory_write":
        normalized = re.sub(r"^✅\s*已记忆（id=[^)]+）：", "", normalized).strip()
    return normalized


def _recover_worker_result_from_messages(messages: list[Any]) -> str:
    preferred_tool_arg_fields: dict[str, tuple[str, ...]] = {
        "team_memory_write": ("content", "outcome", "decision"),
        "paper_write_artifact": ("content",),
        "paper_save_cards": ("paper_cards",),
        "write": ("content",),
    }
    preferred_tool_message_order = (
        "team_memory_write",
        "paper_write_artifact",
        "paper_save_cards",
        "write",
        "bash",
    )

    for message in reversed(messages or []):
        if str(getattr(message, "role", "") or "").strip() != "assistant":
            continue
        for tool_call in reversed(getattr(message, "tool_calls", []) or []):
            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
            if not tool_name:
                continue
            arguments = getattr(tool_call, "arguments", {}) or {}
            if not isinstance(arguments, dict):
                continue
            for field_name in preferred_tool_arg_fields.get(tool_name, ()):
                recovered = _stringify_recovered_worker_payload(arguments.get(field_name))
                recovered = _normalize_recovered_worker_output(tool_name, recovered)
                if recovered:
                    return recovered

    for tool_name in preferred_tool_message_order:
        for message in reversed(messages or []):
            if str(getattr(message, "role", "") or "").strip() != "tool":
                continue
            tool_calls = getattr(message, "tool_calls", []) or []
            if not tool_calls:
                continue
            linked_tool_name = str(getattr(tool_calls[0], "tool_name", "") or "").strip()
            if linked_tool_name != tool_name:
                continue
            recovered = _normalize_recovered_worker_output(tool_name, getattr(message, "content", ""))
            if recovered:
                return recovered

    for message in reversed(messages or []):
        if str(getattr(message, "role", "") or "").strip() != "tool":
            continue
        tool_calls = getattr(message, "tool_calls", []) or []
        linked_tool_name = str(getattr(tool_calls[0], "tool_name", "") or "").strip() if tool_calls else ""
        recovered = _normalize_recovered_worker_output(linked_tool_name, getattr(message, "content", ""))
        if recovered:
            return recovered

    return ""


def _choose_best_worker(team_id: str, task_prompt: str) -> Optional[Any]:
    workers = [w for w in WorkerRegistry.list_for_team(team_id) if w.status != "terminated"]
    if not workers:
        return None
    ranked = sorted(
        workers,
        key=lambda w: score_worker_for_task(w, task_prompt=task_prompt),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _choose_alternate_worker(team_id: str, task_prompt: str, exclude_names: list[str]) -> Optional[Any]:
    excluded = {str(name or "").strip().lower() for name in exclude_names if str(name or "").strip()}
    workers = [
        w
        for w in WorkerRegistry.list_for_team(team_id)
        if w.status != "terminated" and w.name not in excluded
    ]
    if not workers:
        return None
    ranked = sorted(
        workers,
        key=lambda w: score_worker_for_task(w, task_prompt=task_prompt),
        reverse=True,
    )
    return ranked[0] if ranked else None


async def _schedule_goal_loop_check(team_id: str, workdir: str) -> None:
    current_task = _manager_goal_loop_tasks.get(team_id)
    if current_task and not current_task.done():
        return

    async def _emit_goal_loop() -> None:
        try:
            await asyncio.sleep(3.0)
            runtime_summary = RuntimeTaskManager.summary_for_session(team_id)
            counts = dict(runtime_summary.get("counts", {}) or {})
            active_count = sum(
                int(counts.get(name, 0) or 0)
                for name in ["queued", "running", "reviewing", "revising", "recovering"]
            )
            if active_count > 0:
                return

            graph = TaskStateGraphManager.summarize(team_id)
            blockers = list(graph.get("current_blockers", []) or [])
            next_actions = list(graph.get("next_actions", []) or [])
            incomplete_tasks = [
                task
                for task in runtime_summary.get("tasks", [])
                if str(task.get("status", ""))
                in {"partial", "blocked", "failed", "cancelled"}
            ]
            completed_tasks = [task for task in runtime_summary.get("tasks", []) if str(task.get("status", "")) == "completed"]

            if not completed_tasks and not incomplete_tasks:
                return

            if incomplete_tasks:
                goal_status = "需要继续推进"
                summary_lines = [
                    "目标推进检查：当前这一批异步任务已经收敛，但还有部分完成、受阻或失败分支需要处理。",
                    f"已完成 {len(completed_tasks)} 项，待接续 {len(incomplete_tasks)} 项。",
                ]
            elif next_actions or blockers:
                goal_status = "等待下一步"
                summary_lines = [
                    "目标推进检查：当前任务执行已收敛，需要进入下一步。",
                    f"已完成 {len(completed_tasks)} 项，当前没有运行中的后台任务。",
                ]
            else:
                goal_status = "阶段完成"
                summary_lines = [
                    "目标推进检查：当前这一阶段目标基本完成，没有检测到未处理的后台任务。",
                    f"已完成 {len(completed_tasks)} 项。",
                ]

            if blockers:
                summary_lines.append("当前阻塞：")
                for blocker in blockers[:4]:
                    summary_lines.append(f"- {blocker}")
            if next_actions:
                summary_lines.append("建议下一步：")
                for step in next_actions[:4]:
                    summary_lines.append(f"- {step}")
            elif incomplete_tasks:
                summary_lines.append("建议下一步：")
                for task in incomplete_tasks[:3]:
                    if str(task.get("next_action", "") or "").strip():
                        summary_lines.append(f"- {task.get('worker_name', 'worker')}: {task.get('next_action', '')}")
            summary_lines.append(f"阶段判断：{goal_status}。")

            await _append_manager_async_summary(
                team_id=team_id,
                worker_name="manager",
                task_prompt="goal_loop",
                summary_text="\n".join(summary_lines),
                status="completed",
                review_verdict="GOAL_LOOP",
                recommended_next_step=next_actions[0] if next_actions else "",
            )
            channel = TeamChannel.get_or_create(team_id, workdir)
            await channel.publish(
                source="manager",
                target="all",
                event_type="goal_loop",
                content="\n".join(summary_lines),
                metadata={
                    "completed": len(completed_tasks),
                    "incomplete": len(incomplete_tasks),
                },
            )
        finally:
            _manager_goal_loop_tasks.pop(team_id, None)

    loop_task = asyncio.create_task(_emit_goal_loop())
    _manager_goal_loop_tasks[team_id] = loop_task


async def _maybe_auto_reassign_failure(
    *,
    team_id: str,
    worker,
    task_prompt: str,
    workdir: str,
    channel,
    event_emitter: Optional[Callable],
    summary_text: str,
    recommended_next_step: str,
    runtime_task_id: str,
    reassign_depth: int,
    execution_profile: Optional[str] = None,
    question_policy: str = "ask",
) -> bool:
    if not _auto_reassign_enabled():
        return False
    if reassign_depth >= _max_auto_reassign_hops():
        return False

    alternate = _choose_alternate_worker(team_id, task_prompt, exclude_names=[worker.name])
    if not alternate:
        return False
    budget_ok, total_rework_count, total_rework_limit = _consume_total_rework_budget(runtime_task_id)
    if not budget_ok:
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="failed",
            phase="handoff",
            message=(
                "已达到总返工上限，不再继续自动改派。"
                f"{_total_rework_budget_message(total_rework_count, total_rework_limit)}"
            ),
            metadata={
                "total_rework_count": total_rework_count,
                "total_rework_limit": total_rework_limit,
            },
            next_action="请由 Manager 或用户决定是否手动继续。",
        )
        await channel.publish(
            source="manager",
            target="manager",
            event_type="task_retry_limit_reached",
            content=(
                "已达到总返工上限，不再继续自动改派。"
                f"{_total_rework_budget_message(total_rework_count, total_rework_limit)}"
            ),
            metadata={
                "runtime_task_id": runtime_task_id,
                "retry_kind": "total_rework_limit",
                "total_rework_count": total_rework_count,
                "total_rework_limit": total_rework_limit,
            },
        )
        return False

    handoff_prompt = (
        f"{task_prompt}\n\n"
        f"---\n"
        f"# 自动改派说明\n"
        f"上一位 Worker `{worker.name}` 未能完成该任务。\n\n"
        f"失败摘要：\n{summary_text[:1200]}\n\n"
        f"建议优先处理：{recommended_next_step or '先分析失败原因，再给出修正后的完整结果。'}\n"
        f"请你接手后直接输出新的最终结果。"
    )
    RuntimeTaskManager.transition(
        runtime_task_id,
        status="recovering",
        phase="handoff",
        message=f"任务自动改派给 Worker `{alternate.name}`。",
        metadata={"from_worker": worker.name, "to_worker": alternate.name, "reassign_depth": reassign_depth + 1},
        next_action=f"由 {alternate.name} 接手继续执行。",
    )
    await channel.publish(
        source="manager",
        target=f"worker:{alternate.name}",
        event_type="task_reassigned",
        content=handoff_prompt,
        metadata={
            "from_worker": worker.name,
            "to_worker": alternate.name,
            "reassign_depth": reassign_depth + 1,
            "previous_runtime_task_id": runtime_task_id,
            "total_rework_count": total_rework_count,
            "total_rework_limit": total_rework_limit,
        },
    )
    await _append_manager_async_summary(
        team_id=team_id,
        worker_name="manager",
        task_prompt=task_prompt,
        summary_text=f"检测到 `{worker.name}` 执行失败，已自动改派给 `{alternate.name}` 继续处理。",
        status="completed",
        review_verdict="REASSIGNED",
        recommended_next_step=f"等待 {alternate.name} 返回新结果。",
    )
    task_record = RuntimeTaskManager.get(runtime_task_id) or {}
    inherited_task_spec = {
            "contract": dict(task_record.get("task_contract") or {}) if isinstance(task_record.get("task_contract"), dict) else {},
            "resources": dict(task_record.get("resource_context") or {}) if isinstance(task_record.get("resource_context"), dict) else {},
            "total_rework_count": total_rework_count,
            "total_rework_limit": total_rework_limit,
            "execution_profile": execution_profile,
        }
    await assign_worker_task(
        team_id=team_id,
        worker_name=alternate.name,
        task_prompt=handoff_prompt,
        workdir=workdir,
        event_emitter=event_emitter,
        task_spec=inherited_task_spec,
        reassign_depth=reassign_depth + 1,
        origin_worker_name=worker.name,
        previous_runtime_task_id=runtime_task_id,
        question_policy=question_policy,
    )
    return True


def _inject_worker_memory(
    task_prompt: str,
    worker,
    workdir: str,
    execution_profile: Optional[str] = None,
) -> str:
    """Prepend the worker's persistent memory to the task prompt if available."""
    prompt_budget = _budget_chars(
        execution_profile,
        "worker_execution_prompt_chars",
        WORKER_EXECUTION_PROMPT_MAX_CHARS,
        minimum=4000,
    )
    if not worker.memory_key:
        return _truncate_for_prompt(task_prompt, prompt_budget, label="worker task prompt")
    try:
        memory = WorkerMemoryManager.render_context(worker.memory_key, workdir)
        if memory:
            safe_memory = _truncate_for_prompt(
                memory,
                _budget_chars(
                    execution_profile,
                    "worker_memory_prompt_chars",
                    WORKER_MEMORY_PROMPT_MAX_CHARS,
                    minimum=1000,
                ),
                label="worker memory",
            )
            return _truncate_for_prompt(
                (
                f"# 你的历史记忆（跨会话持久积累）\n\n{safe_memory}\n\n"
                f"---\n\n# 当前任务\n\n{task_prompt}"
                ),
                prompt_budget,
                label="worker task prompt",
            )
    except Exception:
        pass
    return _truncate_for_prompt(task_prompt, prompt_budget, label="worker task prompt")


def _fresh_worker_messages(previous_messages: list[Any], result_summary: str = "") -> list[Message]:
    if not previous_messages:
        return []
    summary = _truncate_for_prompt(result_summary, 480, label="previous worker summary")
    if not summary:
        return []
    return [
        Message(
            role="system",
            content=(
                "[Worker context reset]\n"
                "历史消息已归档，避免上下文累积过大。当前任务请只依据最新 Task Contract、Resource Context 和 Worker Memory。\n"
                f"上一任务摘要：{summary}"
            ),
        )
    ]


async def _reset_worker_session_for_new_task(session, worker) -> Any:
    existing_messages = list(getattr(session, "messages", []) or [])
    next_messages = _fresh_worker_messages(
        existing_messages,
        str(getattr(worker, "result_summary", "") or ""),
    )
    if existing_messages == next_messages:
        return session
    session.messages = list(next_messages)
    await SessionManager.update(
        worker.session_id,
        messages=list(next_messages),
        last_active_at=float(datetime.now().timestamp()),
    )
    return session


def _build_retry_prompt(
    *,
    original_task_prompt: str,
    result_text: str,
    review: dict[str, Any],
    attempt: int,
    execution_profile: Optional[str] = None,
    progress_trace: Optional[list[dict[str, Any]]] = None,
) -> str:
    critic_summary = str(review.get("critic", {}).get("summary", "") or "").strip()
    verifier_summary = str(review.get("verifier", {}).get("summary", "") or "").strip()
    critic_next = str(review.get("critic", {}).get("recommended_next_step", "") or "").strip()
    verifier_next = str(review.get("verifier", {}).get("recommended_next_step", "") or "").strip()
    critic_issues = [
        str(item or "").strip()
        for item in (review.get("critic", {}).get("issues") or [])
        if str(item or "").strip()
    ]
    verifier_missing = [
        str(item or "").strip()
        for item in (review.get("verifier", {}).get("missing_evidence") or [])
        if str(item or "").strip()
    ]
    retry_reasons: list[str] = []
    if critic_summary:
        retry_reasons.append(critic_summary)
    if verifier_summary and verifier_summary not in retry_reasons:
        retry_reasons.append(verifier_summary)
    for item in critic_issues[:2]:
        if item not in retry_reasons:
            retry_reasons.append(item)
    for item in verifier_missing[:2]:
        if item not in retry_reasons:
            retry_reasons.append(f"缺少证据：{item}")
    retry_reason_text = "；".join(retry_reasons[:3]).strip() or "上一版结果未满足审查要求。"
    safe_original_task_prompt = _truncate_for_prompt(
        original_task_prompt,
        _budget_chars(
            execution_profile,
            "worker_execution_prompt_chars",
            WORKER_EXECUTION_PROMPT_MAX_CHARS,
            minimum=4000,
        ),
        label="original task prompt",
    )
    safe_result_text = _truncate_for_prompt(
        result_text,
        _budget_chars(
            execution_profile,
            "worker_retry_result_chars",
            WORKER_RETRY_RESULT_MAX_CHARS,
            minimum=1500,
        ),
        label="previous result",
    )
    completed_progress = completed_subtask_work(
        list(progress_trace or []),
        max_items=8,
    )
    failed_progress = failed_subtask_work(
        list(progress_trace or []),
        max_items=6,
    )
    progress_lines: list[str] = []
    if completed_progress:
        progress_lines.append("### 已成功步骤（保留其结果，禁止重复有副作用操作）")
        for item in completed_progress:
            progress_lines.append(
                f"- {item.get('tool_name', 'unknown_tool')}: "
                f"{str(item.get('output_preview') or '').strip()}"
            )
    if failed_progress:
        progress_lines.append("### 尚未完成或失败步骤")
        for item in failed_progress:
            progress_lines.append(
                f"- {item.get('tool_name', 'unknown_tool')}: "
                f"{str(item.get('error_preview') or '').strip()}"
            )
    progress_checkpoint = _truncate_for_prompt(
        "\n".join(progress_lines).strip() or "尚无可复用的工具执行进度。",
        3000,
        label="cross-attempt progress checkpoint",
    )
    return (
        f"{safe_original_task_prompt}\n\n"
        f"---\n"
        f"# 自动返工说明\n"
        f"这是第 {attempt + 1} 次执行。\n"
        f"## 本次返工原因摘要\n{retry_reason_text}\n\n"
        f"上一版结果未通过审查，请基于以下反馈修正后重做。\n\n"
        f"## 上一版结果\n{safe_result_text}\n\n"
        f"## 跨轮执行检查点\n{progress_checkpoint}\n\n"
        f"## Critic 反馈\n{critic_summary or '无'}\n\n"
        f"## Verifier 反馈\n{verifier_summary or '无'}\n\n"
        f"## 必须优先处理的修正点\n"
        f"- {critic_next or '修复 critic 指出的问题'}\n"
        f"- {verifier_next or '补齐 verifier 要求的证据'}\n\n"
        f"请从未完成步骤继续，复用上述成功结果，不要重新执行已经成功且可能产生副作用的操作；"
        f"若修改本地报告及复核记录，先写最终报告并读回，再依据该版本写复核记录并读回；"
        f"不要在复核记录之后改报告。受限修订仍最多四次工具调用，优先按 write报告→read报告→write复核→read复核 排序，"
        f"复用已有来源证据，避免重复读取耗尽额度。"
        f"直接输出修正后的最终结果，不要只重复反馈。"
    )


async def _review_worker_result(
    *,
    team_id: str,
    workdir: str,
    task_prompt: str,
    result_text: str,
    tool_trace: list[Any] | None = None,
    execution_profile: Optional[str] = None,
) -> dict[str, Any]:
    from ..server.auth import session_result_verification_settings
    from ..runtime.verification import verification_policy

    # This is the coordinator's extra review, not the worker's assigned task.
    # Explicit critic/verifier delegation and Workflow review stages still run.
    session = await SessionManager.get(team_id)
    settings = await session_result_verification_settings(session)
    if not settings["enabled"]:
        summary = "结果验证已关闭，跳过系统追加的 Critic / Verifier 审查；结果未经此项复核。"
        return {
            "review": {
                "verdict": "SKIPPED",
                "review_skipped": True,
                "reason_code": "result_verification_disabled",
                "critic": {"summary": summary},
                "verifier": {"summary": summary, "confidence": 0.0},
            },
            "reviewed_result_text": result_text,
            "critic_summary": summary,
            "verifier_summary": summary,
            "review_level": "disabled",
        }
    mode = settings["mode"]
    policy = verification_policy(mode)
    review_level = mode
    safe_task_prompt = _truncate_for_prompt(
        task_prompt,
        _budget_chars(
            execution_profile,
            "worker_review_task_chars",
            WORKER_REVIEW_TASK_MAX_CHARS,
            minimum=2000,
        ),
        label="review task prompt",
    )
    safe_result_text = _truncate_for_prompt(
        result_text,
        _budget_chars(
            execution_profile,
            "worker_review_result_chars",
            WORKER_REVIEW_RESULT_MAX_CHARS,
            minimum=2000,
        ),
        label="review result",
    )
    safe_tool_trace = _truncate_tool_trace(tool_trace)
    try:
        review = await asyncio.wait_for(
            run_review_cycle(
                workdir=workdir,
                parent_session_id=team_id,
                task_prompt=safe_task_prompt,
                result_text=safe_result_text,
                tool_trace=safe_tool_trace,
                verification_mode=mode,
                max_repairs=policy["repairs"],
            ),
            # Policy timeout applies to each model call, not the whole cycle.
            timeout=_worker_review_timeout_seconds(),
        )
    except Exception as exc:
        fail_open = True  # Automatic review is always advisory, even on timeout.
        verdict = "REVISE"
        reason = "review timed out" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
        review = {
            "verdict": verdict,
            "critic": {
                "verdict": verdict,
                "summary": (
                    f"Critic/Verifier review skipped because the review pipeline failed: {reason}. "
                    "Worker result is returned without blocking the completed execution."
                    if fail_open
                    else f"Critic/Verifier review failed: {reason}"
                ),
                "issues": [] if fail_open else [reason],
                "missing_evidence": [],
                "confidence": 0.0,
                "recommended_next_step": (
                    "如需更严格质量门，请稍后重新复核该结果。"
                    if fail_open
                    else "先修复 review pipeline 再继续。"
                ),
            },
            "verifier": {
                "verdict": verdict,
                "summary": (
                    "Verifier skipped; fail-open policy returned the completed worker result."
                    if fail_open
                    else "Verifier skipped because review pipeline failed."
                ),
                "issues": [],
                "missing_evidence": [],
                "confidence": 0.0,
                "recommended_next_step": (
                    "不要重复执行已经完成的工具链；必要时单独触发复核。"
                    if fail_open
                    else "检查 review pipeline。"
                ),
            },
            "review_level": review_level,
            "review_failed_open": fail_open,
            "review_error": reason,
        }
    review["review_level"] = review_level
    review["advisory"] = True
    review["verification_mode"] = mode
    if review.get("revised_result_text"):
        safe_result_text = str(review["revised_result_text"])
    revisions = report_revision_evidence([item for item in tool_trace or [] if isinstance(item, dict)])
    review['report_revision_evidence'] = revisions
    if re.search(r'读回|read.?back', task_prompt, re.IGNORECASE):
        unread = revisions['unread_files']
        if unread:
            review['verdict'] = 'REVISE'
            review['verifier']['verdict'] = 'REVISE'
            review['verifier'].setdefault('missing_evidence', []).append('最终写入后缺少完整成功读回：' + ', '.join(unread))
            review['verifier']['recommended_next_step'] = '在原有额度内读回最终版本，再据此更新并读回复核记录；额度不足则保留部分结果。'

    critic_summary = str(review.get("critic", {}).get("summary", "") or "").strip()
    verifier_summary = str(review.get("verifier", {}).get("summary", "") or "").strip()
    reviewed_result_text = safe_result_text
    if critic_summary or verifier_summary:
        reviewed_result_text = (
            f"{safe_result_text}\n\n"
            f"## Critic\n{critic_summary or 'PASS'}\n\n"
            f"## Verifier\n{verifier_summary or 'PASS'}\n\n"
            f"## Review Verdict\n{review.get('verdict', 'REVISE')}"
        )
    reviewed_result_text = _truncate_for_prompt(
        reviewed_result_text,
        _budget_chars(
            execution_profile,
            "worker_result_chars",
            WORKER_RESULT_MAX_CHARS,
            minimum=2000,
        ),
        label="reviewed worker result",
    )
    return {
        "review": review,
        "reviewed_result_text": reviewed_result_text,
        "critic_summary": critic_summary,
        "verifier_summary": verifier_summary,
        "review_level": review_level,
    }


def _retry_reason_summary(review: dict[str, Any]) -> str:
    critic_summary = str(review.get("critic", {}).get("summary", "") or "").strip()
    verifier_summary = str(review.get("verifier", {}).get("summary", "") or "").strip()
    critic_issues = [
        str(item or "").strip()
        for item in (review.get("critic", {}).get("issues") or [])
        if str(item or "").strip()
    ]
    verifier_missing = [
        str(item or "").strip()
        for item in (review.get("verifier", {}).get("missing_evidence") or [])
        if str(item or "").strip()
    ]
    parts: list[str] = []
    if critic_summary:
        parts.append(critic_summary)
    if verifier_summary and verifier_summary not in parts:
        parts.append(verifier_summary)
    for item in critic_issues[:2]:
        if item not in parts:
            parts.append(item)
    for item in verifier_missing[:2]:
        marked = f"缺少证据：{item}"
        if marked not in parts:
            parts.append(marked)
    return "；".join(parts[:3]).strip() or "上一版结果未满足审查要求。"


def _apply_allowed_tools_to_worker_agent(agent_info, allowed_tools: Any):
    normalized = _normalize_allowed_tools(allowed_tools)
    if normalized == "all":
        return agent_info
    permission = {"*": "deny"}
    for tool_name in normalized:
        permission[str(tool_name)] = "allow"
    return replace(agent_info, permission=permission)


async def _build_worker_agent(
    worker,
    team_id: str,
    workdir: str,
    event_emitter,
    handoff: Optional[dict[str, Any]] = None,
    parent_question_ask: Optional[Callable] = None,
    parent_question_resolve: Optional[Callable] = None,
    runtime_task_id: str = "",
    execution_workdir: Optional[str] = None,
    workspace_metadata: Optional[dict[str, Any]] = None,
    question_policy: str = "ask",
):
    """Prepare session + Agent for a worker. Returns (session, agent)."""
    session = await SessionManager.get(worker.session_id)
    if not session:
        session = await SessionManager.create(
            session_id=worker.session_id,
            parent_id=team_id,
            agent_name=f"worker_{worker.name}",
            title=f"Worker: {worker.name} ({worker.role})",
            workdir=workdir,
        )
    else:
        session = await _reset_worker_session_for_new_task(session, worker)

    handoff_payload = dict(handoff or {}) if isinstance(handoff, dict) else {}
    if handoff_payload or workspace_metadata:
        metadata = dict(getattr(session, "metadata", {}) or {})
        if handoff_payload:
            metadata["handoff"] = handoff_payload
            if isinstance(handoff_payload.get("resources"), dict):
                metadata["workspace_resource_context"] = dict(
                    handoff_payload.get("resources") or {}
                )
        if workspace_metadata:
            metadata["agent_workspace"] = dict(workspace_metadata)
            metadata["control_workdir"] = workdir
        session.metadata = metadata
        await SessionManager.update(worker.session_id, metadata=metadata)

    scoper_info = BuiltinAgents.get(getattr(session, "agent_name", "") or "")
    if not scoper_info:
        scoper_info = WorkerScoper.build_worker_agent_info(
            worker.name,
            worker.role,
            worker.permission_preset,
            peer_whitelist=worker.peer_whitelist,
        )
        BuiltinAgents.register_custom(scoper_info, source="team")

    if handoff_payload:
        contract = dict(handoff_payload.get("contract") or {}) if isinstance(handoff_payload.get("contract"), dict) else {}
        scoper_info = _apply_allowed_tools_to_worker_agent(
            scoper_info,
            contract.get("allowed_tools", "all"),
        )

    provider = create_provider(workdir=workdir)
    agent = Agent(
        agent_info=scoper_info,
        provider=provider,
        session_id=worker.session_id,
        workdir=execution_workdir or workdir,
        existing_session=session,
        question_policy=question_policy,
    )
    agent.set_session_resource_context(handoff_payload.get("resources") if handoff_payload else {})
    agent.set_execution_profile(handoff_payload.get("execution_profile") if handoff_payload else None)
    if callable(parent_question_ask) or callable(parent_question_resolve):
        agent.set_question_handler(
            build_parent_question_bridge(
                child_session_id=worker.session_id,
                child_agent_name=worker.name,
                child_display_name=f"Worker: {worker.name}",
                parent_session_id=team_id,
                task_id=runtime_task_id,
                parent_question_ask=parent_question_ask,
                parent_question_resolve=parent_question_resolve,
            )
        )

    if event_emitter:
        _sid = worker.session_id
        _wname = worker.name
        _tid = team_id

        async def _emit(payload: dict[str, Any]) -> None:
            try:
                merged = {
                    **payload,
                    "parent_session_id": _tid,
                    "session_id": _sid,
                    "agent_name": f"worker_{_wname}",
                    "display_name": f"Worker: {_wname}",
                }
                r = event_emitter(merged)
                if hasattr(r, "__await__"):
                    await r
            except Exception:
                pass

        agent.set_agent_event_emitter(_emit)

    return session, agent


async def _persist_worker_session_messages(worker_session_id: str, agent: Agent, *,
    runtime_task_id: str = '', usage_cursor: Optional[dict] = None, retries: int = 0,
) -> None:
    await SessionManager.update(
        worker_session_id,
        messages=list(agent.messages),
        last_active_at=float(datetime.now().timestamp()),
    )
    if runtime_task_id and usage_cursor is not None:
        # Meter each observed response, including cancellation/failure finalization.
        # A new attempt has a fresh Agent counter; repeated persistence is idempotent.
        if usage_cursor.get('agent') is not agent:
            usage_cursor.update(agent=agent, tokens=0, calls=set())
        total = int(getattr(getattr(agent, 'total_usage', None), 'total_tokens', 0) or 0)
        # Proposed batch calls are not usage. Live execution reserves each call at
        # its gate; completion/cancellation flushes only charge actual executions.
        calls = {call.call_id for message in agent.messages for call in (message.tool_calls or [])
                 if str(getattr(call.state, 'value', call.state)) in {'completed', 'error'}}
        increments = dict(tokens=max(0,total-usage_cursor['tokens']),
            tool_calls=len(calls-usage_cursor['calls']), retries=max(0,retries-usage_cursor.get('retries',0)))
        if any(increments.values()):
            RuntimeTaskManager.record_usage(runtime_task_id, **increments)
        usage_cursor.update(tokens=max(total,usage_cursor['tokens']),calls=usage_cursor['calls']|calls,
            retries=max(retries,usage_cursor.get('retries',0)))


_worker_control_wakeups: dict[str, asyncio.Event] = {}


async def _wait_worker_execution(runtime_task_id: str) -> None:
    """Suspend the live continuation, never cancel/rebuild it to implement pause."""
    wakeup = _worker_control_wakeups.setdefault(runtime_task_id, asyncio.Event())
    try:
        while True:
            wakeup.clear()
            record = RuntimeTaskManager.control_execution(runtime_task_id)
            if record['status'] != 'paused':
                return
            await wakeup.wait()
    finally:
        if _worker_control_wakeups.get(runtime_task_id) is wakeup:
            _worker_control_wakeups.pop(runtime_task_id, None)


def notify_worker_control(runtime_task_id: str) -> None:
    wakeup = _worker_control_wakeups.get(runtime_task_id)
    if wakeup is not None:
        wakeup.set()


async def _settle_worker_workspace(
    *,
    workspace,
    runtime_task_id: str,
    worker_session_id: str,
    outcome: str,
    handoff: Optional[SubtaskHandoff] = None,
) -> None:
    """Persist execution-workspace lifecycle state without masking the worker outcome."""

    try:
        normalized = str(outcome or "failed").strip().lower() or "failed"
        tool_trace = list(getattr(handoff, "tool_trace", []) or []) if handoff else []
        AgentWorkspaceManager.record_provenance(
            workspace,
            tool_trace=tool_trace,
            outcome=normalized,
        )
        if normalized in {"completed", "failed", "cancelled", "blocked"}:
            AgentWorkspaceManager.finalize(workspace, outcome=normalized)
        else:
            AgentWorkspaceManager.checkpoint(
                workspace,
                status=normalized,
                tool_trace=tool_trace,
                pending_action=str(getattr(handoff, "next_action", "") or ""),
            )
        RuntimeTaskManager.update_metadata(
            runtime_task_id,
            agent_workspace=workspace.to_dict(),
        )
        session = await SessionManager.get(worker_session_id)
        if session is not None:
            metadata = dict(getattr(session, "metadata", {}) or {})
            metadata["agent_workspace"] = workspace.to_dict()
            metadata["control_workdir"] = workspace.control_workdir
            session.metadata = metadata
            await SessionManager.update(worker_session_id, metadata=metadata)
    except Exception:
        pass


def _worker_tool_call_event_key(tool_call: Any) -> tuple[str, str]:
    tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
    call_id = str(getattr(tool_call, "call_id", "") or "").strip()
    arguments = getattr(tool_call, "arguments", None) or {}
    if call_id:
        return tool_name, call_id
    try:
        args_key = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_key = str(arguments)
    return tool_name, args_key


async def _emit_worker_tool_call_events(
    *,
    agent: Agent,
    tool_calls: list[Any],
    seen_tool_calls: set[tuple[str, str]],
    tools_seen: set[str],
) -> None:
    emitter = getattr(agent, "_agent_event_emitter", None)
    if not callable(emitter):
        return

    message_count = len(getattr(agent, "messages", []) or [])
    tool_call_count = sum(
        len(getattr(message, "tool_calls", []) or [])
        for message in (getattr(agent, "messages", []) or [])
    )
    for tool_call in tool_calls or []:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        if not tool_name:
            continue
        key = _worker_tool_call_event_key(tool_call)
        if key in seen_tool_calls:
            continue
        seen_tool_calls.add(key)
        tools_seen.add(tool_name)
        payload = {
            "status": "running",
            "message": f"Using tool: {tool_name}",
            "tool_name": tool_name,
            "tool_args": getattr(tool_call, "arguments", None) or {},
            "tools_used": sorted(tools_seen),
            "context": {
                "message_count": message_count,
                "tool_call_count": tool_call_count,
            },
        }
        result = emitter(payload)
        if hasattr(result, "__await__"):
            await result


async def _finalize_worker_result(
    worker,
    team_id: str,
    workdir: str,
    channel,
    agent,
    result_text: str,
    task_prompt: str,
    started_at: float,
    review: dict[str, Any],
    reviewed_result_text: str,
    critic_summary: str,
    runtime_task_id: str,
    task_contract: Optional[dict[str, Any]] = None,
    resource_context: Optional[dict[str, Any]] = None,
    append_manager_summary: bool = False,
    execution_profile: Optional[str] = None,
    subtask_handoff: Optional[SubtaskHandoff] = None,
) -> None:
    """Persist session, update registry, publish task_done, save to team memory."""
    verdict = str(review.get("verdict", "REVISE") or "REVISE").upper()
    if subtask_handoff is None:
        subtask_handoff = build_subtask_handoff(
            result_text=reviewed_result_text,
            tool_trace=build_subtask_tool_trace(list(agent.messages)),
            child_outcome_status="completed" if verdict in {"PASS", "SKIPPED"} or review.get("advisory") else verdict,
            blocker="" if verdict in {"PASS", "SKIPPED"} or review.get("advisory") else _retry_reason_summary(review),
            next_action=str(
                review.get("verifier", {}).get("recommended_next_step", "")
                or review.get("critic", {}).get("recommended_next_step", "")
                or ""
            ),
            review_verdict=verdict,
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker.name,
            execution_attempts=1,
            review_attempts=1,
            request_text=task_prompt,
        )
    handoff_text = subtask_handoff.render_for_parent()
    handoff_payload = subtask_handoff.to_dict()
    outcome_status = subtask_handoff.outcome_status
    runtime_status = {
        "completed": "completed",
        "partial": "partial",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "awaiting_input": "waiting_for_user",
    }.get(outcome_status, "failed")
    awaiting_input = outcome_status == "awaiting_input"
    tool_trace = list(subtask_handoff.tool_trace or [])
    toolchain = list(
        dict.fromkeys(
            str(item.get("tool_name") or "").strip()
            for item in tool_trace
            if isinstance(item, dict) and str(item.get("tool_name") or "").strip()
        )
    )
    failure_reason = ""
    if outcome_status != "completed" and not awaiting_input:
        failure_reason = (
            subtask_handoff.blocker
            or subtask_handoff.next_action
            or _retry_reason_summary(review)
        )
    await SessionManager.update(worker.session_id, messages=list(agent.messages))
    await WorkerRegistry.update(
        worker.session_id,
        status="idle",
        current_task=None,
        result_summary=handoff_text[:500],
        last_task=worker.current_task,
        task_count=worker.task_count + 1,
    )
    await channel.publish(
        source=f"worker:{worker.name}",
        target="manager",
        event_type="task_waiting_for_user" if awaiting_input else "task_done",
        content=handoff_text[
            : _budget_chars(
                execution_profile,
                "worker_task_done_chars",
                WORKER_TASK_DONE_MAX_CHARS,
                minimum=400,
            )
        ],
        metadata={
            "session_id": worker.session_id,
            "review_verdict": verdict,
            "outcome_status": outcome_status,
            "child_outcome_status": subtask_handoff.child_outcome_status,
            "subtask_handoff": handoff_payload,
        },
    )
    # Auto-save result summary into persistent memory
    latency_sec = max(0.0, time.time() - started_at)
    task_type = _infer_task_type(task_prompt)
    constraints = _infer_constraints(task_prompt)

    try:
        summary = handoff_text[:800].strip()
        if summary and not awaiting_input:
            # Protein team workers save to their own fixed memory_key (cross-session)
            # Regular workers save to team memory (keyed by team_id = session_id)
            if worker.memory_key:
                WorkerMemoryManager.add_structured(
                    memory_key=worker.memory_key,
                    workdir=workdir,
                    task_type=task_type,
                    goal=task_prompt[:200],
                    constraints=constraints,
                    decision=critic_summary[:300],
                    outcome=summary,
                    confidence=float(review.get("verifier", {}).get("confidence", 0.0) or 0.0),
                    toolchain=toolchain,
                    failure_reason=failure_reason,
                    tags=["task_result"],
                    source=f"worker:{worker.name}",
                )
            else:
                TeamMemoryManager.add_structured(
                    team_id=team_id,
                    workdir=workdir,
                    task_type=task_type,
                    goal=task_prompt[:200],
                    constraints=constraints,
                    decision=critic_summary[:300],
                    outcome=summary,
                    confidence=float(review.get("verifier", {}).get("confidence", 0.0) or 0.0),
                    toolchain=toolchain,
                    failure_reason=failure_reason,
                    tags=["task_result"],
                    source=f"worker:{worker.name}",
                )
    except Exception:
        pass

    if not awaiting_input:
        await WorkerRegistry.record_execution(
            worker.session_id,
            success=outcome_status == "completed",
            latency_sec=latency_sec,
            tools_used=toolchain,
            skill_tags=[task_type],
            failure_reason=failure_reason,
        )

    TaskStateGraphManager.update_subtask(
        team_id,
        subtask_id=worker.name,
        title=task_prompt[:120],
        owner=worker.name,
        status=outcome_status,
        evidence=[
            f"worker:{worker.name}",
            f"review:{verdict}",
            f"completed_steps:{len(subtask_handoff.completed_work)}",
            f"failed_steps:{len(subtask_handoff.failed_work)}",
        ],
        next_action=(
            "等待父 Agent 或用户回答后恢复原任务。"
            if awaiting_input
            else str(review.get("verifier", {}).get("recommended_next_step", "") or "")
        ),
    )
    TaskStateGraphManager.set_next_actions(
        team_id,
        next_actions=[
            str(review.get("verifier", {}).get("recommended_next_step", "") or ""),
            str(review.get("critic", {}).get("recommended_next_step", "") or ""),
        ],
        blockers=[failure_reason] if failure_reason else [],
    )
    if not awaiting_input:
        postflight = run_postflight_checks(
            result_text=handoff_text,
            review=review,
        )
        RuntimeTaskManager.record_checks(
            runtime_task_id,
            stage="postflight",
            checks=postflight.get("checks", []),
            summary=str(postflight.get("summary", "") or ""),
        )
    RuntimeTaskManager.update_metadata(
        runtime_task_id,
        result_contract=handoff_payload,
        worker_session_id=worker.session_id,
    )
    RuntimeTaskManager.record_result(
        runtime_task_id,
        status=runtime_status,
        result_summary=handoff_text,
        review_verdict=verdict,
        recommended_next_step=(
            "等待父 Agent 或用户回答后恢复原任务。"
            if awaiting_input
            else (
                str(review.get("verifier", {}).get("recommended_next_step", "") or "")
                or str(review.get("critic", {}).get("recommended_next_step", "") or "")
            )
        ),
        last_error=failure_reason,
    )
    RuntimeTaskManager.attach_artifacts(
        runtime_task_id,
        _write_runtime_artifacts(
            session_id=team_id,
            task_id=runtime_task_id,
            workdir=workdir,
            contract={
                **_build_task_contract(task_prompt, task_contract),
                **({"resources": dict(resource_context or {})} if resource_context else {}),
            },
            summary=handoff_text[:1000],
            result={
                "status": outcome_status,
                "runtime_status": runtime_status,
                "review_verdict": verdict,
                "next_action": str(review.get("verifier", {}).get("recommended_next_step", "") or "") or str(review.get("critic", {}).get("recommended_next_step", "") or ""),
                "worker_name": worker.name,
                "subtask_handoff": handoff_payload,
            },
            tool_trace=tool_trace,
        ),
    )
    if append_manager_summary:
        await _append_manager_async_summary(
            team_id=team_id,
            worker_name=worker.name,
            task_prompt=task_prompt,
            summary_text=handoff_text,
            status=outcome_status,
            review_verdict=verdict,
            recommended_next_step=str(review.get("verifier", {}).get("recommended_next_step", "") or "") or str(review.get("critic", {}).get("recommended_next_step", "") or ""),
        )
        await _schedule_manager_async_digest(
            team_id=team_id,
            worker_name=worker.name,
            task_prompt=task_prompt,
            status=outcome_status,
            review_verdict=verdict,
            recommended_next_step=str(review.get("verifier", {}).get("recommended_next_step", "") or "") or str(review.get("critic", {}).get("recommended_next_step", "") or ""),
        )


async def _run_worker_with_review_retries(
    *,
    worker,
    team_id: str,
    workdir: str,
    channel,
    event_emitter: Optional[Callable],
    task_prompt: str,
    started_at: float,
    runtime_task_id: str,
    task_contract: Optional[dict[str, Any]] = None,
    resource_context: Optional[dict[str, Any]] = None,
    execution_prompt: Optional[str] = None,
    append_manager_summary: bool = False,
    execution_profile: Optional[str] = None,
    parent_question_ask: Optional[Callable] = None,
    parent_question_resolve: Optional[Callable] = None,
    execution_workdir: Optional[str] = None,
    workspace_metadata: Optional[dict[str, Any]] = None,
    agent_workspace: Any = None,
    question_policy: str = "ask",
) -> SubtaskHandoff:
    execution_handoff = {
        "contract": dict(task_contract or {}),
        "resources": dict(resource_context or {}),
        "execution_profile": normalize_execution_profile(execution_profile),
    }
    current_prompt = str(
        execution_prompt
        or _build_execution_prompt(
            task_prompt,
            _build_task_contract(task_prompt, task_contract),
            dict(resource_context or {}),
            execution_profile=execution_profile,
        )
    ).strip()
    last_result_text = "(no output)"
    last_review_payload: dict[str, Any] | None = None
    last_agent = None
    cumulative_tool_trace: list[dict[str, Any]] = []
    execution_attempts = 0
    review_attempts = 0
    usage_cursor: dict[str, Any] = {}

    def _make_terminal_handoff(
        review_payload: dict[str, Any],
    ) -> SubtaskHandoff:
        review = dict(review_payload.get("review") or {})
        verdict = str(review.get("verdict", "REVISE") or "REVISE").upper()
        next_action = str(
            review.get("verifier", {}).get("recommended_next_step", "")
            or review.get("critic", {}).get("recommended_next_step", "")
            or ""
        ).strip()
        reviewed_text = str(review_payload.get("reviewed_result_text") or "").strip()
        failed_step_errors = [
            str(item.get("error_preview") or "").strip()
            for item in cumulative_tool_trace
            if isinstance(item, dict) and str(item.get("error_preview") or "").strip()
        ]
        blocker = "\n".join(dict.fromkeys(failed_step_errors[-4:]))
        review_reason = _retry_reason_summary(review) if verdict not in {"PASS", "SKIPPED"} and not review.get("advisory") else ""
        if review_reason and review_reason not in blocker:
            blocker = "\n".join(part for part in (blocker, review_reason) if part)
        return build_subtask_handoff(
            result_text=reviewed_text,
            tool_trace=cumulative_tool_trace,
            child_outcome_status="completed" if verdict in {"PASS", "SKIPPED"} or review.get("advisory") else verdict,
            blocker="" if verdict in {"PASS", "SKIPPED"} or review.get("advisory") else (blocker or reviewed_text),
            next_action=next_action,
            review_verdict=verdict,
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker.name,
            execution_attempts=execution_attempts,
            review_attempts=review_attempts,
            request_text=task_prompt,
        )

    max_review_retries = _max_review_retries()
    max_exec_retries = max_execution_retries()
    execution_retry_count = 0
    for attempt in range(max_review_retries + 1):
        while True:
            await _wait_worker_execution(runtime_task_id)
            execution_attempts += 1
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="running",
                phase="execution",
                message=f"开始执行。execution_attempt={execution_retry_count + 1}，review_attempt={attempt + 1}",
                metadata={
                    "execution_attempt": execution_retry_count + 1,
                    "review_attempt": attempt + 1,
                },
                next_action="等待 Worker 返回结果。",
            )
            agent = None
            try:
                _, agent = await _build_worker_agent(
                    worker,
                    team_id,
                    workdir,
                    event_emitter,
                    handoff=execution_handoff,
                    parent_question_ask=parent_question_ask,
                    parent_question_resolve=parent_question_resolve,
                    question_policy=question_policy,
                    runtime_task_id=runtime_task_id,
                    execution_workdir=execution_workdir,
                    workspace_metadata=workspace_metadata,
                )
                _active_worker_agents[worker.session_id] = agent
                last_agent = agent
                observed_budget_limits = None

                async def execution_gate(tool_call=None):
                    nonlocal observed_budget_limits
                    await _persist_worker_session_messages(worker.session_id, agent,
                        runtime_task_id=runtime_task_id, usage_cursor=usage_cursor, retries=usage_cursor.get('retries', 0))
                    await _wait_worker_execution(runtime_task_id)
                    budget = (RuntimeTaskManager.get(runtime_task_id) or {}).get('budget', {})
                    limits = dict(budget.get('limits') or {})
                    if tool_call is None and limits != observed_budget_limits and any(limits.values()):
                        # A resumed model must not treat the parent prompt's old
                        # allowance as current and prematurely enter final repair.
                        agent.messages.append(Message(role='user',
                            id=f'worker-budget-{runtime_task_id}-{datetime.now().timestamp()}',
                            content=('[WORKER_EXECUTION_BUDGET] 宿主当前确认的预算：'
                                + json.dumps(budget, ensure_ascii=False)
                                + '。这是当前额度与累计用量，取代历史任务描述中的旧额度。'
                                  '宿主会在调用边界暂停；不要仅因旧额度而提前结束或把未完成写成完成。'
                                  '继续原任务尚未完成的步骤，保留已成功结果，不重复读写，不新增任务或自增额度。'),
                            metadata={'internal': True, 'purpose': 'worker_execution_budget', 'budget': budget}))
                        observed_budget_limits = limits
                    if tool_call is not None and tool_call.call_id not in usage_cursor['calls']:
                        # Reserve before executing, so a batch cannot spend its
                        # full set of proposed calls past the configured limit.
                        RuntimeTaskManager.record_usage(runtime_task_id, tool_calls=1)
                        usage_cursor['calls'].add(tool_call.call_id)

                agent._execution_gate = execution_gate
                enriched_prompt = _inject_worker_memory(
                    current_prompt,
                    worker,
                    workdir,
                    execution_profile=execution_profile,
                )
                result = None
                tools_seen: set[str] = set()
                tool_call_seen: set[tuple[str, str]] = set()
                async for item in agent.run_stream(enriched_prompt):
                    if isinstance(item, Message) and item.role == "assistant":
                        result = item
                        await _emit_worker_tool_call_events(
                            agent=agent,
                            tool_calls=list(getattr(item, "tool_calls", []) or []),
                            seen_tool_calls=tool_call_seen,
                            tools_seen=tools_seen,
                        )
                    await _persist_worker_session_messages(worker.session_id, agent,
                        runtime_task_id=runtime_task_id, usage_cursor=usage_cursor, retries=execution_attempts-1)

                await _persist_worker_session_messages(worker.session_id, agent,
                    runtime_task_id=runtime_task_id, usage_cursor=usage_cursor, retries=execution_attempts-1)
                cumulative_tool_trace = merge_subtask_tool_traces(
                    cumulative_tool_trace,
                    build_subtask_tool_trace(
                        list(getattr(agent, "messages", []) or []),
                        attempt=execution_attempts,
                    ),
                )
                raw_result_text = (result.content or "(no output)") if result else "(no output)"
                result_text = _sanitize_worker_result_text(
                    raw_result_text,
                    execution_profile=execution_profile,
                )
                last_result_text = result_text
                break
            except Exception as exc:
                decision = retry_decision_for_exception(exc, execution_retry_count)
                recovered_text = ""
                if agent is not None:
                    cumulative_tool_trace = merge_subtask_tool_traces(
                        cumulative_tool_trace,
                        build_subtask_tool_trace(
                            list(getattr(agent, "messages", []) or []),
                            attempt=execution_attempts,
                        ),
                    )
                    recovered_text = _recover_worker_result_from_messages(
                        list(getattr(agent, "messages", []) or [])
                    )
                if recovered_text and decision.retryable:
                    recovered_text = _sanitize_worker_result_text(
                        recovered_text,
                        execution_profile=execution_profile,
                    )
                    last_result_text = recovered_text
                    result_text = recovered_text
                    RuntimeTaskManager.transition(
                        runtime_task_id,
                        status="recovering",
                        phase="execution_recovered",
                        message=(
                            "执行阶段出现瞬时错误，但已从已有工具结果恢复可交付内容，"
                            "直接进入审查，不再从头重跑。"
                        ),
                        metadata={
                            "error": str(exc),
                            "recovered_from_tools": True,
                        },
                        next_action="直接进入审查，不再重复执行已有工具链。",
                        last_error=str(exc),
                    )
                    await channel.publish(
                        source=f"worker:{worker.name}",
                        target="manager",
                        event_type="task_recovered",
                        content="执行阶段超时，但已从已有工具结果恢复结果，直接进入审查。",
                        metadata={
                            "session_id": worker.session_id,
                            "error": str(exc),
                        },
                    )
                    break
                if decision.retryable and execution_retry_count < max_exec_retries:
                    execution_retry_count += 1
                    RuntimeTaskManager.update_counters(
                        runtime_task_id,
                        execution_retry_count=execution_retry_count,
                    )
                    RuntimeTaskManager.transition(
                        runtime_task_id,
                        status="recovering",
                        phase="execution_retry",
                        message=f"执行出现瞬时错误，准备第 {execution_retry_count} 次自动恢复重试：{exc}",
                        metadata={
                            "error": str(exc),
                            "delay_seconds": decision.delay_seconds,
                        },
                        next_action=decision.reason,
                        last_error=str(exc),
                    )
                    await channel.publish(
                        source=f"worker:{worker.name}",
                        target="manager",
                        event_type="task_retry",
                        content=f"执行阶段瞬时失败，准备自动恢复重试第 {execution_retry_count} 次：{exc}",
                        metadata={
                            "session_id": worker.session_id,
                            "retry_kind": "execution",
                            "attempt": execution_retry_count,
                        },
                    )
                    await asyncio.sleep(decision.delay_seconds)
                    continue
                failure_handoff = build_subtask_handoff(
                    result_text=str(exc),
                    tool_trace=cumulative_tool_trace,
                    child_outcome_status="failed",
                    blocker=str(exc),
                    next_action=(
                        decision.reason
                        or "由主 Agent 根据已完成步骤继续、恢复或改派 Worker。"
                    ),
                    review_verdict="FAILED",
                    task_id=runtime_task_id,
                    session_id=worker.session_id,
                    agent_name=worker.name,
                    execution_attempts=execution_attempts,
                    review_attempts=review_attempts,
                )
                raise SubtaskHandoffError(str(exc), failure_handoff) from exc
            finally:
                if agent is not None:
                    await _persist_worker_session_messages(worker.session_id, agent,
                        runtime_task_id=runtime_task_id, usage_cursor=usage_cursor, retries=execution_attempts-1)
                if agent_workspace is not None:
                    AgentWorkspaceManager.record_provenance(
                        agent_workspace,
                        agent=agent,
                        tool_trace=cumulative_tool_trace,
                        attempt=execution_attempts,
                        outcome=str(
                            getattr(agent, "_last_outcome_status", "") or ""
                        ),
                    )
                if _active_worker_agents.get(worker.session_id) is agent:
                    _active_worker_agents.pop(worker.session_id, None)

        checkpoint_handoff = build_subtask_handoff(
            result_text=result_text,
            tool_trace=cumulative_tool_trace,
            child_outcome_status=(
                getattr(agent, "_last_outcome_status", "completed")
                if agent is not None
                else "completed"
            ),
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker.name,
            execution_attempts=execution_attempts,
            review_attempts=review_attempts,
            request_text=task_prompt,
        )
        if checkpoint_handoff.outcome_status == "awaiting_input":
            next_step = "等待父 Agent 或用户回答后，携带既有进度恢复原任务。"
            waiting_review = {
                "verdict": "WAITING",
                "critic": {
                    "summary": "Worker 正在等待补充输入，暂不进入结果审查。",
                    "recommended_next_step": next_step,
                },
                "verifier": {
                    "summary": "检测到未决问题；这不是执行失败。",
                    "recommended_next_step": next_step,
                    "confidence": 1.0,
                },
            }
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="waiting_for_user",
                phase="awaiting_input",
                message="Worker 提出了未决问题，暂停审查并等待父 Agent 或用户输入。",
                metadata={"question_payload": checkpoint_handoff.question_payload},
                next_action=next_step,
                last_error="",
            )
            await _finalize_worker_result(
                worker,
                team_id,
                workdir,
                channel,
                agent,
                result_text,
                task_prompt,
                started_at,
                waiting_review,
                result_text,
                "Worker 正在等待补充输入。",
                runtime_task_id,
                task_contract=task_contract,
                resource_context=resource_context,
                append_manager_summary=append_manager_summary,
                execution_profile=execution_profile,
                subtask_handoff=checkpoint_handoff,
            )
            return checkpoint_handoff

        if not _worker_requires_review(worker.name):
            review_payload = {
                "review": {
                    "verdict": "PASS",
                    "critic": {
                        "summary": "文献库专用工具已返回可审计的结构化结果。",
                        "issues": [],
                        "recommended_next_step": "继续主任务。",
                    },
                    "verifier": {
                        "summary": "跳过重复的模型复核。",
                        "missing_evidence": [],
                        "recommended_next_step": "继续主任务。",
                        "confidence": 1.0,
                    },
                },
                "reviewed_result_text": result_text,
                "critic_summary": "文献库专用工具已返回可审计的结构化结果。",
                "verifier_summary": "跳过重复的模型复核。",
            }
            terminal_handoff = _make_terminal_handoff(review_payload)
            await _finalize_worker_result(
                worker,
                team_id,
                workdir,
                channel,
                agent,
                result_text,
                task_prompt,
                started_at,
                review_payload["review"],
                result_text,
                review_payload["critic_summary"],
                runtime_task_id,
                task_contract=task_contract,
                resource_context=resource_context,
                append_manager_summary=append_manager_summary,
                execution_profile=execution_profile,
                subtask_handoff=terminal_handoff,
            )
            return terminal_handoff

        await _wait_worker_execution(runtime_task_id)
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="reviewing",
            phase="review",
            message="Worker 已完成执行，进入 Critic / Verifier 审查。",
            next_action="等待 review verdict。",
        )
        if event_emitter:
            try:
                emitted = event_emitter(
                    {
                        "kind": "process_note",
                        "phase": "worker_review",
                        "status": "running",
                        "message": f"Worker `{worker.name}` 已完成执行，正在进行复核收尾。",
                        "session_id": worker.session_id,
                        "parent_session_id": team_id,
                        "agent_name": f"worker_{worker.name}",
                        "display_name": f"Worker: {worker.name}",
                    }
                )
                if hasattr(emitted, "__await__"):
                    await emitted
            except Exception:
                pass

        review_attempts += 1
        review_payload = await _review_worker_result(
            team_id=team_id,
            workdir=workdir,
            task_prompt=task_prompt,
            result_text=result_text,
            # Review must see the cumulative, outcome-bearing trace.  A retry may
            # intentionally reuse an earlier successful side effect and therefore
            # have no tool calls in the current Agent message list.
            tool_trace=cumulative_tool_trace,
            execution_profile=execution_profile,
        )
        if review_payload["review"].get("review_skipped"):
            review_attempts -= 1
        if agent_workspace is not None:
            AgentWorkspaceManager.record_review_provenance(
                agent_workspace,
                review=dict(review_payload.get("review") or {}),
                review_attempt=review_attempts,
            )
        last_review_payload = review_payload
        verdict = str(review_payload["review"].get("verdict", "REVISE") or "REVISE").upper()
        review_unavailable = bool(review_payload["review"].get("review_failed_open"))
        advisory = bool(review_payload["review"].get("advisory"))
        if advisory or verdict in {"PASS", "SKIPPED"} or review_unavailable or attempt >= max_review_retries:
            terminal_handoff = _make_terminal_handoff(review_payload)
            await _finalize_worker_result(
                worker,
                team_id,
                workdir,
                channel,
                agent,
                result_text,
                task_prompt,
                started_at,
                review_payload["review"],
                review_payload["reviewed_result_text"],
                review_payload["critic_summary"],
                runtime_task_id,
                task_contract=task_contract,
                resource_context=resource_context,
                append_manager_summary=append_manager_summary,
                execution_profile=execution_profile,
                subtask_handoff=terminal_handoff,
            )
            if not advisory and verdict not in {"PASS", "SKIPPED"} and not review_unavailable and append_manager_summary:
                reassigned = await _maybe_auto_reassign_failure(
                    team_id=team_id,
                    worker=worker,
                    task_prompt=task_prompt,
                    workdir=workdir,
                    channel=channel,
                    event_emitter=event_emitter,
                    summary_text=review_payload["reviewed_result_text"],
                    recommended_next_step=str(review_payload["review"].get("verifier", {}).get("recommended_next_step", "") or "") or str(review_payload["review"].get("critic", {}).get("recommended_next_step", "") or ""),
                    runtime_task_id=runtime_task_id,
                    reassign_depth=int(RuntimeTaskManager.get(runtime_task_id).get("reassign_depth", 0) or 0) if RuntimeTaskManager.get(runtime_task_id) else 0,
                    execution_profile=execution_profile,
                )
                if not reassigned:
                    await _schedule_goal_loop_check(team_id, workdir)
            return terminal_handoff

        budget_ok, total_rework_count, total_rework_limit = _consume_total_rework_budget(runtime_task_id)
        if not budget_ok:
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="failed",
                phase="review_retry_limit",
                message=(
                    "已达到总返工上限，不再继续自动返工。"
                    f"{_total_rework_budget_message(total_rework_count, total_rework_limit)}"
                ),
                metadata={
                    "review_verdict": verdict,
                    "total_rework_count": total_rework_count,
                    "total_rework_limit": total_rework_limit,
                },
                next_action="请由 Manager 或用户决定是否手动继续。",
            )
            await channel.publish(
                source=f"worker:{worker.name}",
                target="manager",
                event_type="task_retry_limit_reached",
                content=(
                    "已达到总返工上限，不再继续自动返工。"
                    f"{_total_rework_budget_message(total_rework_count, total_rework_limit)}"
                ),
                metadata={
                    "session_id": worker.session_id,
                    "retry_kind": "total_rework_limit",
                    "review_verdict": verdict,
                    "total_rework_count": total_rework_count,
                    "total_rework_limit": total_rework_limit,
                },
            )
            terminal_handoff = _make_terminal_handoff(review_payload)
            await _finalize_worker_result(
                worker,
                team_id,
                workdir,
                channel,
                agent,
                result_text,
                task_prompt,
                started_at,
                review_payload["review"],
                review_payload["reviewed_result_text"],
                review_payload["critic_summary"],
                runtime_task_id,
                task_contract=task_contract,
                resource_context=resource_context,
                append_manager_summary=append_manager_summary,
                execution_profile=execution_profile,
                subtask_handoff=terminal_handoff,
            )
            if append_manager_summary:
                await _schedule_goal_loop_check(team_id, workdir)
            return terminal_handoff

        retry_prompt = _build_retry_prompt(
            original_task_prompt=current_prompt,
            result_text=result_text,
            review=review_payload["review"],
            attempt=attempt,
            execution_profile=execution_profile,
            progress_trace=cumulative_tool_trace,
        )
        retry_reason = _retry_reason_summary(review_payload["review"])
        current_prompt = retry_prompt
        RuntimeTaskManager.update_counters(
            runtime_task_id,
            review_retry_count=attempt + 1,
            total_rework_count=total_rework_count,
        )
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="revising",
            phase="review_retry",
            message=f"Review verdict={verdict}，进入自动返工第 {attempt + 1} 次。原因：{retry_reason}",
            metadata={
                "review_verdict": verdict,
                "attempt": attempt + 1,
                "retry_reason": retry_reason,
                "total_rework_count": total_rework_count,
                "total_rework_limit": total_rework_limit,
            },
            next_action="根据 Critic / Verifier 反馈修正后重跑。",
        )
        await channel.publish(
            source=f"worker:{worker.name}",
            target="manager",
            event_type="task_retry",
            content=(
                f"自动重跑第 {attempt + 1} 次：上一版 verdict={verdict}。"
                f"返工原因：{retry_reason}"
            ),
            metadata={
                "session_id": worker.session_id,
                "attempt": attempt + 1,
                "review_verdict": verdict,
                "retry_reason": retry_reason,
                "critic_summary": review_payload["critic_summary"],
                "verifier_summary": review_payload["verifier_summary"],
                "total_rework_count": total_rework_count,
                "total_rework_limit": total_rework_limit,
            },
        )
        TaskStateGraphManager.update_subtask(
            team_id,
            subtask_id=worker.name,
            title=task_prompt[:120],
            owner=worker.name,
            status="running",
            evidence=[f"review:{verdict}", f"retry:{attempt + 1}", f"total_rework:{total_rework_count}"],
            next_action=(
                f"根据审查意见自动重跑第 {attempt + 1} 次。"
                f"{_total_rework_budget_message(total_rework_count, total_rework_limit)}"
            ),
        )

    if last_agent and last_review_payload:
        terminal_handoff = _make_terminal_handoff(last_review_payload)
        await _finalize_worker_result(
            worker,
            team_id,
            workdir,
            channel,
            last_agent,
            last_result_text,
            task_prompt,
            started_at,
            last_review_payload["review"],
            last_review_payload["reviewed_result_text"],
            last_review_payload["critic_summary"],
            runtime_task_id,
            task_contract=task_contract,
            resource_context=resource_context,
            append_manager_summary=append_manager_summary,
            execution_profile=execution_profile,
            subtask_handoff=terminal_handoff,
        )
        verdict = str(last_review_payload["review"].get("verdict", "REVISE") or "REVISE").upper()
        if verdict != "PASS" and append_manager_summary:
            reassigned = await _maybe_auto_reassign_failure(
                team_id=team_id,
                worker=worker,
                task_prompt=task_prompt,
                workdir=workdir,
                channel=channel,
                event_emitter=event_emitter,
                summary_text=last_review_payload["reviewed_result_text"],
                recommended_next_step=str(last_review_payload["review"].get("verifier", {}).get("recommended_next_step", "") or "") or str(last_review_payload["review"].get("critic", {}).get("recommended_next_step", "") or ""),
                runtime_task_id=runtime_task_id,
                reassign_depth=int(RuntimeTaskManager.get(runtime_task_id).get("reassign_depth", 0) or 0) if RuntimeTaskManager.get(runtime_task_id) else 0,
                execution_profile=execution_profile,
            )
            if not reassigned:
                await _schedule_goal_loop_check(team_id, workdir)
        return terminal_handoff
    return build_subtask_handoff(
        result_text=last_result_text,
        tool_trace=cumulative_tool_trace,
        child_outcome_status="failed",
        blocker=last_result_text,
        next_action="检查 Worker 执行状态并继续未完成步骤。",
        review_verdict="FAILED",
        task_id=runtime_task_id,
        session_id=worker.session_id,
        agent_name=worker.name,
        execution_attempts=execution_attempts,
        review_attempts=review_attempts,
        request_text=task_prompt,
    )


async def assign_worker_task(
    *,
    team_id: str,
    worker_name: str,
    task_prompt: str,
    workdir: str,
    event_emitter: Optional[Callable] = None,
    task_spec: Optional[dict[str, Any]] = None,
    reassign_depth: int = 0,
    origin_worker_name: Optional[str] = None,
    previous_runtime_task_id: Optional[str] = None,
    parent_question_ask: Optional[Callable] = None,
    parent_question_resolve: Optional[Callable] = None,
    invocation_id: str = "",
    parent_turn_id: str = "",
    parent_request_id: str = "",
    question_policy: str = "ask",
) -> str:
    """
    Dispatch a task to a named Worker asynchronously.
    Returns immediately with a status message.
    The Worker runs in background; results appear in TeamChannel.
    """
    if worker_name in {"auto", "*", ""}:
        selected = _choose_best_worker(team_id, task_prompt)
        if not selected:
            return "❌ 当前团队没有可用 Worker。"
        worker_name = selected.name

    worker = WorkerRegistry.get_by_name(team_id, worker_name)
    err = _guard_worker(worker, worker_name)
    if err:
        return err

    handoff = _build_task_handoff(task_prompt, task_spec)
    contract = dict(handoff.get("contract") or {})
    resource_context = dict(handoff.get("resources") or {})
    execution_prompt = str(handoff.get("execution_prompt") or task_prompt).strip()
    execution_profile = normalize_execution_profile(handoff.get("execution_profile"))
    biosafety_scope_text = await _team_biosafety_scope_text(team_id, task_prompt)
    execution_prompt += ("\n\n# Parent User Request\n"
        + _truncate_for_prompt(biosafety_scope_text, 4000, label="parent user request")
        + "\n上文保留父会话原始要求，仅用于核对分工范围，不扩大工具授权。任务契约中的额外解释不构成事实证据；未被来源支持的效果不得写成已经发生。")
    biosafety_scope = biosafety_scope_hash(biosafety_scope_text)
    resource_context = _with_biosafety_resource_context(
        resource_context,
        team_id=team_id,
        scope_hash=biosafety_scope,
    )
    if worker_name == "risk":
        execution_prompt += build_risk_review_contract(
            scope_hash=biosafety_scope,
            scope_text=biosafety_scope_text,
        )
    else:
        biosafety_gate = evaluate_worker_dispatch(
            workdir=workdir,
            team_id=team_id,
            worker_name=worker_name,
            scope_hash=biosafety_scope,
        )
        if not biosafety_gate.allowed:
            return "❌ " + _biosafety_gate_message(biosafety_gate)
    inherited_total_rework_count = 0
    inherited_total_rework_limit = _max_total_rework_retries()
    if isinstance(task_spec, dict):
        try:
            inherited_total_rework_count = max(0, int(task_spec.get("total_rework_count", 0) or 0))
        except Exception:
            inherited_total_rework_count = 0
        raw_limit = task_spec.get("total_rework_limit")
        if raw_limit not in (None, ""):
            try:
                inherited_total_rework_limit = max(0, int(raw_limit))
            except Exception:
                inherited_total_rework_limit = _max_total_rework_retries()

    runtime_task_id = _make_runtime_task_id(worker_name)
    RuntimeTaskManager.ensure_task(
        task_id=runtime_task_id,
        session_id=team_id,
        worker_name=worker_name,
        task_prompt=task_prompt,
        mode="async",
        metadata={
            **_task_control_metadata(
                runtime_task_id,
                contract,
                task_spec,
                parent_task_id=previous_runtime_task_id or "",
            ),
            "reassign_depth": reassign_depth,
            "origin_worker_name": origin_worker_name or "",
            "previous_runtime_task_id": previous_runtime_task_id or "",
            "task_contract": contract,
            "resource_context": resource_context,
            "execution_profile": execution_profile,
            "invocation_id": str(invocation_id or ""),
            "parent_turn_id": str(parent_turn_id or ""),
            "parent_request_id": str(parent_request_id or ""),
            "tool_name": "assign_task",
            "worker_session_id": str(worker.session_id or ""),
            "total_rework_count": inherited_total_rework_count,
            "total_rework_limit": inherited_total_rework_limit,
        },
    )
    RuntimeTaskManager.update_counters(
        runtime_task_id,
        total_rework_count=inherited_total_rework_count,
    )
    RuntimeTaskManager.attach_artifacts(
        runtime_task_id,
        _write_runtime_artifacts(
            session_id=team_id,
            task_id=runtime_task_id,
            workdir=workdir,
            contract={
                **contract,
                **({"resources": resource_context} if resource_context else {}),
            },
            summary=task_prompt[:600],
            result={"status": "queued", "review_verdict": "", "next_action": "执行 preflight。"},
            tool_trace=[],
        ),
    )
    RuntimeTaskManager.transition(
        runtime_task_id,
        status="queued",
        phase="queued",
        message="任务进入异步队列，等待预检。",
        next_action="执行 preflight。",
    )
    preflight = run_preflight_checks(
        worker_name=worker_name,
        task_prompt=task_prompt,
        workdir=workdir,
        resource_context=resource_context,
    )
    RuntimeTaskManager.record_checks(
        runtime_task_id,
        stage="preflight",
        checks=preflight.get("checks", []),
        summary=str(preflight.get("summary", "") or ""),
    )
    if str(preflight.get("status", "")).lower() == "blocked":
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="preflight",
            message="预检未通过，任务被阻塞。",
            next_action=str(preflight.get("next_action", "") or ""),
            last_error="preflight_blocked",
        )
        return "❌ 任务预检失败，已阻塞。请先修复工作目录、provider 或任务输入后重试。"

    workspace_session = await SessionManager.get(worker.session_id)
    workspace_agent_name = str(
        getattr(workspace_session, "agent_name", "") or f"worker_{worker.name}"
    )
    workspace_domain, workspace_strategy = _worker_workspace_profile(
        worker,
        workspace_session,
    )
    try:
        agent_workspace = AgentWorkspaceManager.create(
            control_workdir=workdir,
            task_id=runtime_task_id,
            session_id=worker.session_id,
            kind="team-worker",
            strategy=workspace_strategy,
            domain=workspace_domain,
            agent_name=workspace_agent_name,
            worker_name=worker.name,
            resource_context=resource_context,
            task_contract=contract,
        )
    except Exception as exc:
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="workspace_isolation",
            message="无法创建 Worker 隔离工作区，任务已阻塞。",
            next_action="检查科研 Run Workspace 或 Git worktree 状态后重试。",
            last_error=str(exc),
        )
        return f"❌ 无法创建 Worker 隔离工作区：{exc}"
    RuntimeTaskManager.update_metadata(
        runtime_task_id,
        agent_workspace=agent_workspace.to_dict(),
    )
    execution_resource_context = dict(
        agent_workspace.resource_context or resource_context
    )
    workspace_hint = AgentWorkspaceManager.execution_prompt_hint(agent_workspace)
    workspace_execution_prompt = (
        f"{execution_prompt}\n\n{workspace_hint}"
        if workspace_hint
        else execution_prompt
    )

    previous_task = _active_tasks.get(worker.session_id)

    channel = TeamChannel.get_or_create(team_id, workdir)
    await channel.publish(
        source="manager",
        target=f"worker:{worker_name}",
        event_type="task_assigned",
        content=task_prompt,
        metadata={"worker_session_id": worker.session_id},
    )
    waits_for_previous = bool(previous_task and not previous_task.done())
    RuntimeTaskManager.transition(
        runtime_task_id,
        status="queued" if waits_for_previous else "running",
        phase="worker_wait" if waits_for_previous else "dispatch",
        message=(
            f"Worker `{worker_name}` 正在完成上一项任务，本任务已排队。"
            if waits_for_previous
            else f"任务已分派给 Worker `{worker_name}`，开始后台执行。"
        ),
        metadata={"worker_session_id": worker.session_id},
        next_action=(
            "上一项任务结束后自动开始。"
            if waits_for_previous
            else "等待 Worker 执行。"
        ),
    )
    TaskStateGraphManager.ensure_graph(team_id, task_prompt[:200])
    TaskStateGraphManager.update_subtask(
        team_id,
        subtask_id=worker.name,
        title=task_prompt[:120],
        owner=worker.name,
        status="queued" if waits_for_previous else "running",
        next_action=(
            "等待上一项任务结束后自动开始"
            if waits_for_previous
            else "等待 Worker 完成并进入 critic/verifier 审查"
        ),
    )

    _worker = worker  # capture for closure

    async def _run() -> None:
        started_at = time.time()
        workspace_outcome = "failed"
        settlement_handoff: Optional[SubtaskHandoff] = None
        worker_lease = None
        claim_token = ""
        heartbeat_task: Optional[asyncio.Task] = None
        try:
            if previous_task and not previous_task.done():
                RuntimeTaskManager.transition(
                    runtime_task_id,
                    status="queued",
                    phase="worker_wait",
                    message=f"Worker `{_worker.name}` 正在完成上一项任务，本任务已排队。",
                    next_action="上一项任务结束后自动开始。",
                )
                await _wait_for_previous_worker_task(previous_task)
            claim_token = RuntimeTaskManager.try_claim(
                runtime_task_id,
                assignee_session_id=_worker.session_id,
            ) or ""
            if not claim_token:
                RuntimeTaskManager.transition(
                    runtime_task_id,
                    status="blocked",
                    phase="claim_conflict",
                    message="任务执行权已被其他执行器领取。",
                    next_action="等待当前租约结束或由恢复流程重新派发。",
                    last_error="claim_conflict",
                )
                return

            async def _heartbeat_claim() -> None:
                while True:
                    await asyncio.sleep(30)
                    if not RuntimeTaskManager.heartbeat(
                        runtime_task_id,
                        claim_token=claim_token,
                    ):
                        return

            heartbeat_task = asyncio.create_task(_heartbeat_claim())
            await WorkerRegistry.update(
                _worker.session_id,
                status="busy",
                current_task=task_prompt,
            )
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="queued",
                phase="capacity_wait",
                message="Worker 正在等待可用执行名额。",
                next_action="获得执行名额后自动开始。",
            )
            worker_lease = await ExecutionCapacityController.current().acquire(
                kind="worker",
                tenant_id=str(getattr(workspace_session, "tenant_id", "") or ""),
                user_id=str(getattr(workspace_session, "user_id", "") or ""),
                session_id=_worker.session_id,
                request_id=runtime_task_id,
            )
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="running",
                phase="dispatch",
                message=f"Worker `{_worker.name}` 已获得执行名额。",
                next_action="执行任务并进入 review。",
            )
            final_handoff = await _run_worker_with_review_retries(
                worker=_worker,
                team_id=team_id,
                workdir=workdir,
                channel=channel,
                event_emitter=event_emitter,
                task_prompt=task_prompt,
                started_at=started_at,
                runtime_task_id=runtime_task_id,
                task_contract=contract,
                resource_context=execution_resource_context,
                execution_prompt=workspace_execution_prompt,
                append_manager_summary=True,
                execution_profile=execution_profile,
                parent_question_ask=parent_question_ask,
                parent_question_resolve=parent_question_resolve,
                question_policy=question_policy,
                execution_workdir=agent_workspace.execution_workdir,
                workspace_metadata=agent_workspace.to_dict(),
                agent_workspace=agent_workspace,
            )
            settlement_handoff = final_handoff
            workspace_outcome = final_handoff.outcome_status or "completed"
            _record_task_retries(runtime_task_id, final_handoff)
            if _worker.name == "risk":
                risk_record = _record_completed_risk_review(
                    workdir=workdir,
                    team_id=team_id,
                    scope_hash=biosafety_scope,
                    scope_text=biosafety_scope_text,
                    reviewer_session_id=_worker.session_id,
                    handoff=final_handoff,
                )
                human_review_issue = await _enqueue_biosafety_human_review(
                    workdir=workdir,
                    team_id=team_id,
                    scope_hash=biosafety_scope,
                    result=risk_record,
                )
                if human_review_issue:
                    risk_record_message = (
                        "生物安全审查已记录，等待人工批准："
                        + str(human_review_issue.get("identifier") or "已加入审核队列")
                    )
                elif risk_record.decision is not None:
                    risk_record_message = "生物安全审查已记录。"
                else:
                    risk_record_message = _biosafety_gate_message(risk_record)
                await channel.publish(
                    source=f"worker:{_worker.name}",
                    target="manager",
                    event_type="biosafety_review_recorded",
                    content=risk_record_message,
                    metadata={
                        "biosafety_action": risk_record.action,
                        "human_review": human_review_issue,
                        **dict(risk_record.metadata or {}),
                    },
                )
            await _schedule_goal_loop_check(team_id, workdir)
        except CapacityRejected as exc:
            workspace_outcome = "blocked"
            capacity_handoff = build_subtask_handoff(
                result_text=exc.detail,
                tool_trace=[],
                child_outcome_status="blocked",
                blocker=f"{exc.code}: {exc.detail}",
                next_action="稍后重试，或由管理员调整并发和等待队列上限。",
                task_id=runtime_task_id,
                session_id=_worker.session_id,
                agent_name=_worker.name,
            )
            settlement_handoff = capacity_handoff
            await WorkerRegistry.update(_worker.session_id, status="idle", current_task=None)
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="blocked",
                phase="capacity_rejected",
                message="Worker 因执行容量不足未启动。",
                next_action="稍后重试，或检查容量监控。",
                last_error=exc.code,
            )
            TaskStateGraphManager.update_subtask(
                team_id,
                subtask_id=_worker.name,
                title=task_prompt[:120],
                owner=_worker.name,
                status="blocked",
                evidence=[exc.code],
                next_action="稍后重试或调整执行容量",
            )
            await channel.publish(
                source=f"worker:{_worker.name}",
                target="manager",
                event_type="task_blocked",
                content=capacity_handoff.render_for_parent(),
                metadata={"session_id": _worker.session_id, "code": exc.code},
            )
        except asyncio.CancelledError:
            workspace_outcome = "cancelled"
            session = await SessionManager.get(_worker.session_id)
            cancel_handoff = build_subtask_handoff(
                result_text="任务在后台执行过程中被取消。",
                tool_trace=build_subtask_tool_trace(
                    list(getattr(session, "messages", []) or [])
                    if session
                    else [],
                    attempt=1,
                ),
                child_outcome_status="cancelled",
                blocker="任务在后台执行过程中被取消。",
                next_action="由 Manager 基于已有进度继续或重新分配该任务。",
                review_verdict="CANCELLED",
                task_id=runtime_task_id,
                session_id=_worker.session_id,
                agent_name=_worker.name,
                execution_attempts=1,
            )
            cancel_text = cancel_handoff.render_for_parent()
            settlement_handoff = cancel_handoff
            cancel_status = cancel_handoff.outcome_status
            await WorkerRegistry.update(_worker.session_id, status="idle", current_task=None)
            await WorkerRegistry.record_execution(
                _worker.session_id,
                success=False,
                latency_sec=max(0.0, time.time() - started_at),
                failure_reason="task_cancelled",
            )
            TaskStateGraphManager.update_subtask(
                team_id,
                subtask_id=_worker.name,
                title=task_prompt[:120],
                owner=_worker.name,
                status=cancel_status,
                evidence=[
                    "task_cancelled",
                    f"completed_steps:{len(cancel_handoff.completed_work)}",
                ],
                next_action="由 Manager 重新分配或终止",
            )
            await channel.publish(
                source=f"worker:{_worker.name}",
                target="manager",
                event_type="task_failed",
                content=cancel_text,
                metadata={
                    "session_id": _worker.session_id,
                    "outcome_status": cancel_status,
                    "child_outcome_status": cancel_handoff.child_outcome_status,
                    "subtask_handoff": cancel_handoff.to_dict(),
                },
            )
            RuntimeTaskManager.record_result(
                runtime_task_id,
                status=cancel_status,
                result_summary=cancel_text,
                review_verdict="CANCELLED",
                recommended_next_step="由 Manager 决定是否重新分配该任务。",
                last_error="task_cancelled",
            )
            _attach_subtask_handoff_artifacts(
                team_id=team_id,
                runtime_task_id=runtime_task_id,
                workdir=workdir,
                contract=contract,
                resource_context=resource_context,
                handoff=cancel_handoff,
            )
            await _append_manager_async_summary(
                team_id=team_id,
                worker_name=_worker.name,
                task_prompt=task_prompt,
                summary_text=cancel_text,
                status=cancel_status,
                review_verdict="CANCELLED",
                recommended_next_step="由 Manager 决定是否重新分配该任务。",
            )
            await _schedule_manager_async_digest(
                team_id=team_id,
                worker_name=_worker.name,
                task_prompt=task_prompt,
                status=cancel_status,
                review_verdict="CANCELLED",
                recommended_next_step="由 Manager 决定是否重新分配该任务。",
            )
            await _schedule_goal_loop_check(team_id, workdir)
        except Exception as exc:
            workspace_outcome = "failed"
            if isinstance(exc, SubtaskHandoffError):
                failure_handoff = exc.handoff
            else:
                session = await SessionManager.get(_worker.session_id)
                failure_handoff = build_subtask_handoff(
                    result_text=str(exc),
                    tool_trace=build_subtask_tool_trace(
                        list(getattr(session, "messages", []) or [])
                        if session
                        else [],
                        attempt=1,
                    ),
                    child_outcome_status="failed",
                    blocker=str(exc),
                    next_action="检查失败原因，基于已有进度继续或改派 Worker。",
                    review_verdict="FAILED",
                    task_id=runtime_task_id,
                    session_id=_worker.session_id,
                    agent_name=_worker.name,
                    execution_attempts=1,
                )
            failure_text = failure_handoff.render_for_parent()
            settlement_handoff = failure_handoff
            failure_status = failure_handoff.outcome_status
            await WorkerRegistry.update(_worker.session_id, status="idle", current_task=None)
            await WorkerRegistry.record_execution(
                _worker.session_id,
                success=False,
                latency_sec=max(0.0, time.time() - started_at),
                failure_reason=failure_handoff.blocker or str(exc),
            )
            TaskStateGraphManager.update_subtask(
                team_id,
                subtask_id=_worker.name,
                title=task_prompt[:120],
                owner=_worker.name,
                status=failure_status,
                evidence=[
                    str(exc),
                    f"completed_steps:{len(failure_handoff.completed_work)}",
                    f"failed_steps:{len(failure_handoff.failed_work)}",
                ],
                next_action="检查失败原因并决定是否重跑",
            )
            await channel.publish(
                source=f"worker:{_worker.name}",
                target="manager",
                event_type="task_failed",
                content=failure_text,
                metadata={
                    "session_id": _worker.session_id,
                    "outcome_status": failure_status,
                    "child_outcome_status": failure_handoff.child_outcome_status,
                    "subtask_handoff": failure_handoff.to_dict(),
                },
            )
            RuntimeTaskManager.record_result(
                runtime_task_id,
                status=failure_status,
                result_summary=failure_text,
                review_verdict="FAILED",
                recommended_next_step="检查失败原因，必要时改派 Worker 或缩小任务范围。",
                last_error=failure_handoff.blocker or str(exc),
            )
            _attach_subtask_handoff_artifacts(
                team_id=team_id,
                runtime_task_id=runtime_task_id,
                workdir=workdir,
                contract=contract,
                resource_context=resource_context,
                handoff=failure_handoff,
            )
            await _append_manager_async_summary(
                team_id=team_id,
                worker_name=_worker.name,
                task_prompt=task_prompt,
                summary_text=failure_text,
                status=failure_status,
                review_verdict="FAILED",
                recommended_next_step="检查失败原因，必要时改派 Worker 或缩小任务范围。",
            )
            await _schedule_manager_async_digest(
                team_id=team_id,
                worker_name=_worker.name,
                task_prompt=task_prompt,
                status=failure_status,
                review_verdict="FAILED",
                recommended_next_step="检查失败原因，必要时改派 Worker 或缩小任务范围。",
            )
            reassigned = await _maybe_auto_reassign_failure(
                team_id=team_id,
                worker=_worker,
                task_prompt=task_prompt,
                workdir=workdir,
                channel=channel,
                event_emitter=event_emitter,
                summary_text=failure_text,
                recommended_next_step="检查失败原因，必要时改派 Worker 或缩小任务范围。",
                runtime_task_id=runtime_task_id,
                reassign_depth=reassign_depth,
                execution_profile=execution_profile,
                question_policy=question_policy,
            )
            if not reassigned:
                await _schedule_goal_loop_check(team_id, workdir)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if claim_token:
                RuntimeTaskManager.release_claim(runtime_task_id, claim_token=claim_token)
            if worker_lease is not None:
                worker_lease.release()
            await _settle_worker_workspace(
                workspace=agent_workspace,
                runtime_task_id=runtime_task_id,
                worker_session_id=_worker.session_id,
                outcome=workspace_outcome,
                handoff=settlement_handoff,
            )
            current_task = asyncio.current_task()
            if current_task is not None:
                _clear_active_worker_task(_worker.session_id, current_task)

    task = asyncio.create_task(_run())
    _active_tasks[worker.session_id] = task
    if waits_for_previous:
        return f"✅ 任务已加入 Worker **{worker_name}** 的队列，将在上一项任务结束后自动执行。"
    return f"✅ 任务已分派给 Worker **{worker_name}**，正在后台执行。使用 list_workers 查看进度。"


async def run_worker_task_inline(
    *,
    team_id: str,
    worker_name: str,
    task_prompt: str,
    workdir: str,
    event_emitter: Optional[Callable] = None,
    task_spec: Optional[dict[str, Any]] = None,
    parent_question_ask: Optional[Callable] = None,
    parent_question_resolve: Optional[Callable] = None,
    invocation_id: str = "",
    parent_turn_id: str = "",
    parent_request_id: str = "",
    question_policy: str = "ask",
) -> SubtaskHandoff:
    """
    Run a Worker task synchronously (blocks until completed).
    Returns the full result text directly into the caller's (Manager's) context.
    """
    if worker_name in {"auto", "*", ""}:
        selected = _choose_best_worker(team_id, task_prompt)
        if not selected:
            return build_subtask_handoff(
                result_text="当前团队没有可用 Worker。",
                tool_trace=[],
                child_outcome_status="blocked",
                blocker="当前团队没有可用 Worker。",
                next_action="创建合适的 Worker 或由主 Agent 直接继续。",
                agent_name=worker_name,
            )
        worker_name = selected.name

    worker = WorkerRegistry.get_by_name(team_id, worker_name)
    err = _guard_worker(worker, worker_name)
    if err:
        return build_subtask_handoff(
            result_text=err,
            tool_trace=[],
            child_outcome_status="blocked",
            blocker=err,
            next_action="检查 Worker 状态或改派其他 Worker。",
            agent_name=worker_name,
        )

    handoff = _build_task_handoff(task_prompt, task_spec)
    contract = dict(handoff.get("contract") or {})
    resource_context = dict(handoff.get("resources") or {})
    execution_prompt = str(handoff.get("execution_prompt") or task_prompt).strip()
    execution_profile = normalize_execution_profile(handoff.get("execution_profile"))
    biosafety_scope_text = await _team_biosafety_scope_text(team_id, task_prompt)
    execution_prompt += ("\n\n# Parent User Request\n"
        + _truncate_for_prompt(biosafety_scope_text, 4000, label="parent user request")
        + "\n上文保留父会话原始要求，仅用于核对分工范围，不扩大工具授权。任务契约中的额外解释不构成事实证据；未被来源支持的效果不得写成已经发生。")
    biosafety_scope = biosafety_scope_hash(biosafety_scope_text)
    resource_context = _with_biosafety_resource_context(
        resource_context,
        team_id=team_id,
        scope_hash=biosafety_scope,
    )
    if worker_name == "risk":
        execution_prompt += build_risk_review_contract(
            scope_hash=biosafety_scope,
            scope_text=biosafety_scope_text,
        )
    else:
        biosafety_gate = evaluate_worker_dispatch(
            workdir=workdir,
            team_id=team_id,
            worker_name=worker_name,
            scope_hash=biosafety_scope,
        )
        if not biosafety_gate.allowed:
            blocker = _biosafety_gate_message(biosafety_gate)
            return build_subtask_handoff(
                result_text=blocker,
                tool_trace=[],
                child_outcome_status="blocked",
                blocker=blocker,
                next_action=(
                    "先完成 risk Worker 审查后重试。"
                    if biosafety_gate.action == "review_required"
                    else "停止该任务或按生物安全审查要求处理。"
                ),
                agent_name=worker_name,
            )
    inherited_total_rework_count = 0
    inherited_total_rework_limit = _max_total_rework_retries()
    if isinstance(task_spec, dict):
        try:
            inherited_total_rework_count = max(0, int(task_spec.get("total_rework_count", 0) or 0))
        except Exception:
            inherited_total_rework_count = 0
        raw_limit = task_spec.get("total_rework_limit")
        if raw_limit not in (None, ""):
            try:
                inherited_total_rework_limit = max(0, int(raw_limit))
            except Exception:
                inherited_total_rework_limit = _max_total_rework_retries()

    runtime_task_id = _make_runtime_task_id(worker_name)
    RuntimeTaskManager.ensure_task(
        task_id=runtime_task_id,
        session_id=team_id,
        worker_name=worker_name,
        task_prompt=task_prompt,
        mode="sync",
        metadata={
            **_task_control_metadata(runtime_task_id, contract, task_spec),
            "task_contract": contract,
            "resource_context": resource_context,
            "execution_profile": execution_profile,
            "invocation_id": str(invocation_id or ""),
            "parent_turn_id": str(parent_turn_id or ""),
            "parent_request_id": str(parent_request_id or ""),
            "tool_name": "assign_task",
            "worker_session_id": str(worker.session_id or ""),
            "total_rework_count": inherited_total_rework_count,
            "total_rework_limit": inherited_total_rework_limit,
        },
    )
    RuntimeTaskManager.update_counters(
        runtime_task_id,
        total_rework_count=inherited_total_rework_count,
    )
    RuntimeTaskManager.attach_artifacts(
        runtime_task_id,
        _write_runtime_artifacts(
            session_id=team_id,
            task_id=runtime_task_id,
            workdir=workdir,
            contract={
                **contract,
                **({"resources": resource_context} if resource_context else {}),
            },
            summary=task_prompt[:600],
            result={"status": "queued", "review_verdict": "", "next_action": "执行 preflight。"},
            tool_trace=[],
        ),
    )
    RuntimeTaskManager.transition(
        runtime_task_id,
        status="queued",
        phase="queued",
        message="同步任务已创建，等待预检。",
        next_action="执行 preflight。",
    )
    preflight = run_preflight_checks(
        worker_name=worker_name,
        task_prompt=task_prompt,
        workdir=workdir,
        resource_context=resource_context,
    )
    RuntimeTaskManager.record_checks(
        runtime_task_id,
        stage="preflight",
        checks=preflight.get("checks", []),
        summary=str(preflight.get("summary", "") or ""),
    )
    if str(preflight.get("status", "")).lower() == "blocked":
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="preflight",
            message="同步任务预检未通过。",
            next_action=str(preflight.get("next_action", "") or ""),
            last_error="preflight_blocked",
        )
        blocker = "任务预检失败，已阻塞。请先修复工作目录、provider 或任务输入后重试。"
        return build_subtask_handoff(
            result_text=blocker,
            tool_trace=[],
            child_outcome_status="blocked",
            blocker=blocker,
            next_action=str(preflight.get("next_action", "") or "修复预检问题后继续。"),
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker_name,
        )

    workspace_session = await SessionManager.get(worker.session_id)
    workspace_agent_name = str(
        getattr(workspace_session, "agent_name", "") or f"worker_{worker.name}"
    )
    workspace_domain, workspace_strategy = _worker_workspace_profile(
        worker,
        workspace_session,
    )
    try:
        agent_workspace = AgentWorkspaceManager.create(
            control_workdir=workdir,
            task_id=runtime_task_id,
            session_id=worker.session_id,
            kind="team-worker",
            strategy=workspace_strategy,
            domain=workspace_domain,
            agent_name=workspace_agent_name,
            worker_name=worker.name,
            resource_context=resource_context,
            task_contract=contract,
        )
    except Exception as exc:
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="workspace_isolation",
            message="无法创建 Worker 隔离工作区，同步任务已阻塞。",
            next_action="检查科研 Run Workspace 或 Git worktree 状态后重试。",
            last_error=str(exc),
        )
        blocker = f"无法创建 Worker 隔离工作区：{exc}"
        return build_subtask_handoff(
            result_text=blocker,
            tool_trace=[],
            child_outcome_status="blocked",
            blocker=blocker,
            next_action="检查科研 Run Workspace 或 Git worktree 状态后重试。",
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker_name,
        )
    RuntimeTaskManager.update_metadata(
        runtime_task_id,
        agent_workspace=agent_workspace.to_dict(),
    )
    execution_resource_context = dict(
        agent_workspace.resource_context or resource_context
    )
    workspace_hint = AgentWorkspaceManager.execution_prompt_hint(agent_workspace)
    workspace_execution_prompt = (
        f"{execution_prompt}\n\n{workspace_hint}"
        if workspace_hint
        else execution_prompt
    )

    claim_token = RuntimeTaskManager.try_claim(
        runtime_task_id,
        assignee_session_id=worker.session_id,
    ) or ""
    if not claim_token:
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="claim_conflict",
            message="同步任务执行权已被其他执行器领取。",
            next_action="等待当前租约结束后重试。",
            last_error="claim_conflict",
        )
        return build_subtask_handoff(
            result_text="任务领取冲突，未重复执行。",
            tool_trace=[],
            child_outcome_status="blocked",
            blocker="claim_conflict",
            next_action="等待当前租约结束后重试。",
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker_name,
        )

    channel = TeamChannel.get_or_create(team_id, workdir)
    await channel.publish(
        source="manager",
        target=f"worker:{worker_name}",
        event_type="task_assigned",
        content=task_prompt,
        metadata={"worker_session_id": worker.session_id, "mode": "sync"},
    )
    await WorkerRegistry.update(worker.session_id, status="busy", current_task=task_prompt)
    RuntimeTaskManager.transition(
        runtime_task_id,
        status="running",
        phase="dispatch",
        message=f"同步任务已分派给 Worker `{worker_name}`。",
        metadata={"worker_session_id": worker.session_id},
        next_action="等待 Worker 执行并完成 review。",
    )
    TaskStateGraphManager.ensure_graph(team_id, task_prompt[:200])
    TaskStateGraphManager.update_subtask(
        team_id,
        subtask_id=worker.name,
        title=task_prompt[:120],
        owner=worker.name,
        status="running",
        next_action="同步执行中，完成后进入 critic/verifier 审查",
    )

    started_at = time.time()

    async def _run_inline_worker() -> SubtaskHandoff:
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="queued",
            phase="capacity_wait",
            message="同步 Worker 正在等待可用执行名额。",
            next_action="获得执行名额后自动开始。",
        )
        lease = await ExecutionCapacityController.current().acquire(
            kind="worker",
            tenant_id=str(getattr(workspace_session, "tenant_id", "") or ""),
            user_id=str(getattr(workspace_session, "user_id", "") or ""),
            session_id=worker.session_id,
            request_id=runtime_task_id,
        )
        async with lease:
            RuntimeTaskManager.transition(
                runtime_task_id,
                status="running",
                phase="dispatch",
                message=f"同步 Worker `{worker_name}` 已获得执行名额。",
                next_action="执行任务并进入 review。",
            )
            return await _run_worker_with_review_retries(
                worker=worker,
                team_id=team_id,
                workdir=workdir,
                channel=channel,
                event_emitter=event_emitter,
                task_prompt=task_prompt,
                started_at=started_at,
                runtime_task_id=runtime_task_id,
                task_contract=contract,
                resource_context=execution_resource_context,
                execution_prompt=workspace_execution_prompt,
                append_manager_summary=False,
                execution_profile=execution_profile,
                parent_question_ask=parent_question_ask,
                parent_question_resolve=parent_question_resolve,
                question_policy=question_policy,
                execution_workdir=agent_workspace.execution_workdir,
                workspace_metadata=agent_workspace.to_dict(),
                agent_workspace=agent_workspace,
            )

    inline_task = asyncio.create_task(
        _run_inline_worker(),
        name=f"assign-task-inline-{worker.session_id}",
    )
    _active_tasks[worker.session_id] = inline_task
    workspace_outcome = "failed"
    settlement_handoff: Optional[SubtaskHandoff] = None
    try:
        final_handoff = await inline_task
        settlement_handoff = final_handoff
        workspace_outcome = final_handoff.outcome_status or "completed"
        _record_task_retries(runtime_task_id, final_handoff)
        if worker.name == "risk":
            risk_record = _record_completed_risk_review(
                workdir=workdir,
                team_id=team_id,
                scope_hash=biosafety_scope,
                scope_text=biosafety_scope_text,
                reviewer_session_id=worker.session_id,
                handoff=final_handoff,
            )
            human_review_issue = await _enqueue_biosafety_human_review(
                workdir=workdir,
                team_id=team_id,
                scope_hash=biosafety_scope,
                result=risk_record,
            )
            if risk_record.decision is None:
                blocker = _biosafety_gate_message(risk_record)
                return build_subtask_handoff(
                    result_text=f"{final_handoff.result_text}\n\n{blocker}",
                    tool_trace=final_handoff.tool_trace,
                    child_outcome_status="blocked",
                    blocker=blocker,
                    next_action="让 risk Worker 按结构化 Schema 重新提交审查。",
                    review_verdict="REVISE",
                    task_id=final_handoff.task_id,
                    session_id=final_handoff.session_id,
                    agent_name=final_handoff.agent_name,
                    execution_attempts=final_handoff.execution_attempts,
                    review_attempts=final_handoff.review_attempts,
                )
            if human_review_issue:
                final_handoff.result_text = (
                    f"{final_handoff.result_text}\n\n"
                    "生物安全人工审查已加入队列："
                    f"{human_review_issue.get('identifier') or 'pending_review'}。"
                )
        return final_handoff
    except CapacityRejected as exc:
        workspace_outcome = "blocked"
        capacity_handoff = build_subtask_handoff(
            result_text=exc.detail,
            tool_trace=[],
            child_outcome_status="blocked",
            blocker=f"{exc.code}: {exc.detail}",
            next_action="稍后重试，或由管理员调整并发和等待队列上限。",
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker_name,
        )
        settlement_handoff = capacity_handoff
        await WorkerRegistry.update(worker.session_id, status="idle", current_task=None)
        RuntimeTaskManager.transition(
            runtime_task_id,
            status="blocked",
            phase="capacity_rejected",
            message="同步 Worker 因执行容量不足未启动。",
            next_action="稍后重试，或检查容量监控。",
            last_error=exc.code,
        )
        return capacity_handoff
    except asyncio.CancelledError:
        workspace_outcome = "cancelled"
        current_task = asyncio.current_task()
        parent_cancelled = bool(current_task is not None and current_task.cancelling())
        session = await SessionManager.get(worker.session_id)
        cancel_handoff = build_subtask_handoff(
            result_text=f"Worker '{worker_name}' 任务已中止。",
            tool_trace=build_subtask_tool_trace(
                list(getattr(session, "messages", []) or []) if session else [],
                attempt=1,
            ),
            child_outcome_status="cancelled",
            blocker="任务在同步执行过程中被用户中止。",
            next_action="由主 Agent 决定是否基于已有进度继续或重新分配。",
            review_verdict="CANCELLED",
            task_id=runtime_task_id,
            session_id=worker.session_id,
            agent_name=worker_name,
            execution_attempts=1,
        )
        cancel_text = cancel_handoff.render_for_parent()
        settlement_handoff = cancel_handoff
        cancel_status = cancel_handoff.outcome_status
        await WorkerRegistry.update(worker.session_id, status="idle", current_task=None)
        await WorkerRegistry.record_execution(
            worker.session_id,
            success=False,
            latency_sec=max(0.0, time.time() - started_at),
            failure_reason="task_cancelled",
        )
        TaskStateGraphManager.update_subtask(
            team_id,
            subtask_id=worker.name,
            title=task_prompt[:120],
            owner=worker.name,
            status=cancel_status,
            evidence=[
                "task_cancelled",
                f"completed_steps:{len(cancel_handoff.completed_work)}",
            ],
            next_action="由 Manager 决定是否重新分配",
        )
        await channel.publish(
            source=f"worker:{worker_name}",
            target="manager",
            event_type="task_cancelled",
            content=cancel_text,
            metadata={
                "session_id": worker.session_id,
                "outcome_status": cancel_status,
                "child_outcome_status": cancel_handoff.child_outcome_status,
                "subtask_handoff": cancel_handoff.to_dict(),
            },
        )
        RuntimeTaskManager.record_result(
            runtime_task_id,
            status=cancel_status,
            result_summary=cancel_text,
            review_verdict="CANCELLED",
            recommended_next_step="由 Manager 决定是否重新分配该任务。",
            last_error="task_cancelled",
        )
        _attach_subtask_handoff_artifacts(
            team_id=team_id,
            runtime_task_id=runtime_task_id,
            workdir=workdir,
            contract=contract,
            resource_context=resource_context,
            handoff=cancel_handoff,
        )
        if parent_cancelled:
            raise
        return cancel_handoff
    except Exception as exc:
        workspace_outcome = "failed"
        if isinstance(exc, SubtaskHandoffError):
            failure_handoff = exc.handoff
        else:
            session = await SessionManager.get(worker.session_id)
            failure_handoff = build_subtask_handoff(
                result_text=f"Worker '{worker_name}' 任务失败：{exc}",
                tool_trace=build_subtask_tool_trace(
                    list(getattr(session, "messages", []) or [])
                    if session
                    else [],
                    attempt=1,
                ),
                child_outcome_status="failed",
                blocker=str(exc),
                next_action="由主 Agent 根据已完成步骤继续、恢复或改派 Worker。",
                review_verdict="FAILED",
                task_id=runtime_task_id,
                session_id=worker.session_id,
                agent_name=worker_name,
                execution_attempts=1,
            )
        failure_text = failure_handoff.render_for_parent()
        settlement_handoff = failure_handoff
        failure_status = failure_handoff.outcome_status
        await WorkerRegistry.update(worker.session_id, status="idle", current_task=None)
        await WorkerRegistry.record_execution(
            worker.session_id,
            success=False,
            latency_sec=0.0,
            failure_reason=failure_handoff.blocker or str(exc),
        )
        TaskStateGraphManager.update_subtask(
            team_id,
            subtask_id=worker.name,
            title=task_prompt[:120],
            owner=worker.name,
            status=failure_status,
            evidence=[
                str(exc),
                f"completed_steps:{len(failure_handoff.completed_work)}",
                f"failed_steps:{len(failure_handoff.failed_work)}",
            ],
            next_action="检查失败原因并决定是否重跑",
        )
        await channel.publish(
            source=f"worker:{worker_name}",
            target="manager",
            event_type="task_failed",
            content=failure_text,
            metadata={
                "session_id": worker.session_id,
                "outcome_status": failure_status,
                "child_outcome_status": failure_handoff.child_outcome_status,
                "subtask_handoff": failure_handoff.to_dict(),
            },
        )
        RuntimeTaskManager.record_result(
            runtime_task_id,
            status=failure_status,
            result_summary=failure_text,
            review_verdict="FAILED",
            recommended_next_step="检查失败原因并决定是否重跑。",
            last_error=failure_handoff.blocker or str(exc),
        )
        _attach_subtask_handoff_artifacts(
            team_id=team_id,
            runtime_task_id=runtime_task_id,
            workdir=workdir,
            contract=contract,
            resource_context=resource_context,
            handoff=failure_handoff,
        )
        return failure_handoff
    finally:
        RuntimeTaskManager.release_claim(runtime_task_id, claim_token=claim_token)
        await _settle_worker_workspace(
            workspace=agent_workspace,
            runtime_task_id=runtime_task_id,
            worker_session_id=worker.session_id,
            outcome=workspace_outcome,
            handoff=settlement_handoff,
        )
        if _active_tasks.get(worker.session_id) is inline_task:
            _active_tasks.pop(worker.session_id, None)
