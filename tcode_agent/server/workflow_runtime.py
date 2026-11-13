from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import HTTPException

from ..runtime.shared_tracker import SharedTracker
from ..workflow import (
    WorkflowCandidate,
    WorkflowRun,
    WorkflowRunManager,
    WorkflowStageTemplate,
    WorkflowTemplateRegistry,
    WorkflowVersionConflictError,
    ensure_workflow_confirmation_review,
    is_workflow_confirmation_review,
)


WORKFLOW_WAITING_STAGE_STATUSES = {"waiting_external_job", "review_pending", "collecting"}
WORKFLOW_COMPLETED_STATUSES = {"completed", "done", "closed"}
WORKFLOW_FAILED_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "canceled",
    "terminated",
    "timed_out",
}


def collect_workflow_tool_receipts(messages, executed_keys, trace_context):
    """Collect host tool metadata from this turn only, never from model prose."""
    trace = dict(trace_context or {})
    if not all(trace.get(key) for key in ("workflow_run_id", "workflow_stage_id", "workflow_stage_attempt")):
        return []
    latest = {}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            call_id = str(getattr(call, "call_id", "") or "")
            if not call_id or ("call_id", call_id) not in executed_keys:
                continue
            name = str(getattr(call, "tool_name", "") or "")
            meta = dict(getattr(call, "metadata", {}) or {})
            state = getattr(call, "state", "")
            completed = getattr(state, "value", state) == "completed"
            receipt = meta.get("stage_receipt")
            valid = completed and not getattr(call, "error", None) and meta.get("ok") is True
            if not valid or not isinstance(receipt, dict):
                receipt = None
            if receipt is not None:
                try:
                    encoded = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
                    receipt = json.loads(encoded) if len(encoded) <= 16000 else None
                except (TypeError, ValueError):
                    receipt = None
            artifact_refs = meta.get("artifact_refs")
            if not isinstance(artifact_refs, dict):
                artifact_refs = {}
            bounded_artifact_refs = {}
            for raw_name, raw_value in list(artifact_refs.items())[:32]:
                artifact_name = str(raw_name or "").strip()
                if not artifact_name:
                    continue
                if isinstance(raw_value, str):
                    artifact_value = raw_value.strip()
                elif isinstance(raw_value, dict):
                    artifact_value = dict(raw_value)
                else:
                    continue
                if artifact_value:
                    bounded_artifact_refs[artifact_name] = artifact_value
            entry = {
                "tool_name": name,
                "call_id": call_id,
                "receipt": receipt,
                "artifact_refs": bounded_artifact_refs,
                "run_id": trace["workflow_run_id"],
                "stage_id": trace["workflow_stage_id"],
                "attempt": trace["workflow_stage_attempt"],
            }
            # A later failed duplicate call must not erase an already captured
            # successful receipt from the same bounded turn.
            if receipt is not None or name not in latest:
                latest[name] = entry
    return list(latest.values())[-32:]


def declared_tool_stage_response(stage, result, *, run_id, attempt):
    """Use an explicitly declared tool receipt; normal stage validators still run."""
    source = (stage.metadata or {}).get("result_source")
    if not source:
        return None
    if (not isinstance(source, dict) or source.get("type") != "tool_receipt"
            or source.get("tool") not in stage.allowed_tools or stage.review_gate):
        raise ValueError("invalid workflow stage result_source declaration")
    matching = [r for r in result.get("workflow_tool_receipts", [])
                if isinstance(r, dict) and r.get("tool_name") == source["tool"]
                and r.get("run_id") == run_id and r.get("stage_id") == stage.stage_id
                and r.get("attempt") == attempt]
    receipt = matching[-1].get("receipt") if matching else None
    # The outer host receipt is already bound to the current workflow run,
    # stage and attempt above.  A plugin stage receipt may intentionally use
    # its own persisted run namespace (for example ``erd_*``) and must not be
    # required to equal the supervisor's ``wf-*`` identifier.
    receipt_run_id = receipt.get("run_id") if isinstance(receipt, dict) else None
    if not isinstance(receipt, dict) or not isinstance(receipt_run_id, str) or not receipt_run_id.strip():
        raise ValueError("declared workflow tool receipt missing or invalid for this attempt")
    return json.dumps(receipt, ensure_ascii=False, allow_nan=False)


def declared_tool_stage_artifact_refs(stage, result, *, run_id, attempt):
    """Return artifacts from the same authoritative receipt call only."""

    source = (stage.metadata or {}).get("result_source")
    if not source:
        return {}
    if (
        not isinstance(source, dict)
        or source.get("type") != "tool_receipt"
        or source.get("tool") not in stage.allowed_tools
        or stage.review_gate
    ):
        raise ValueError("invalid workflow stage result_source declaration")
    matching = [
        receipt
        for receipt in result.get("workflow_tool_receipts", [])
        if isinstance(receipt, dict)
        and receipt.get("tool_name") == source["tool"]
        and receipt.get("run_id") == run_id
        and receipt.get("stage_id") == stage.stage_id
        and receipt.get("attempt") == attempt
        and isinstance(receipt.get("receipt"), dict)
    ]
    if not matching:
        return {}
    artifact_refs = matching[-1].get("artifact_refs")
    return dict(artifact_refs) if isinstance(artifact_refs, dict) else {}


def recover_declared_tool_stage_receipt_from_log(
    stage,
    *,
    run_id: str,
    attempt: int,
    workdir: str,
    log_paths: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover a successful host receipt lost from the in-memory turn result."""

    source = (stage.metadata or {}).get("result_source")
    tool_name = source.get("tool") if isinstance(source, dict) else ""
    raw_path = str((log_paths or {}).get("workflow_stage_log_jsonl") or "").strip()
    if not tool_name or not raw_path:
        return None
    root = Path(workdir or ".").expanduser().resolve()
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None

    recovered_artifacts: dict[str, Any] = {}
    declared_entry: dict[str, Any] | None = None
    for raw_line in reversed(deque(path.open("r", encoding="utf-8"), maxlen=256)):
        try:
            entry = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        receipt = metadata.get("stage_receipt")
        same_stage_success = (
            entry.get("workflow_run_id") == run_id
            and entry.get("workflow_stage_id") == stage.stage_id
            and entry.get("state") == "completed"
            and not result.get("error")
            and metadata.get("ok") is True
        )
        if same_stage_success:
            for name, value in dict(metadata.get("artifact_refs") or {}).items():
                recovered_artifacts.setdefault(str(name), value)
        if (
            entry.get("tool_name") == tool_name
            and same_stage_success
            and isinstance(receipt, dict)
            and str(receipt.get("run_id") or "").strip()
            and declared_entry is None
        ):
            declared_entry = {
                "tool_name": tool_name,
                "call_id": str(entry.get("call_id") or ""),
                "receipt": dict(receipt),
                "run_id": run_id,
                "stage_id": stage.stage_id,
                "attempt": attempt,
            }
    if declared_entry is None:
        return None
    declared_entry["artifact_refs"] = recovered_artifacts
    return declared_entry


def terminal_tool_stage_response(stage, result, *, run_id, attempt):
    """Recover a non-completed turn with an authoritative stage receipt.

This does not commit/pass the stage: Schema and file validators still decide.
Never recover cancellation, explicit host errors, or asynchronous work.  A
generic PARTIAL/UNVERIFIED/BLOCKED outcome is presentation state only after a
declared commit tool has returned an authoritative receipt; the Workflow's own
schema, artifact and scientific validators remain the acceptance boundary.
"""
    status = str(result.get("status") or "").strip().lower()
    raw_response = str(result.get("response") or result.get("last_response") or "")
    explicit_failure = re.search(
        r"(?:^|\n)\s*(?:[#>*-]+\s*)?"
        r"(?:BLOCKED|FAILED|ERROR|FAILURE|阻塞|失败|执行失败)(?:\b|\s*[:：])",
        raw_response,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if (
        status not in {"partial", "blocked"}
        or result.get("error")
        or result.get("async_tool_refs")
        or explicit_failure
    ):
        return None
    try:
        return declared_tool_stage_response(stage, result, run_id=run_id, attempt=attempt)
    except (ValueError, TypeError):
        return None


def _review_workflow_context(review: dict[str, Any]) -> tuple[str, str]:
    review_data = review.get("review_data") if isinstance(review.get("review_data"), dict) else {}
    workspace = (
        review_data.get("workspace")
        if isinstance(review_data.get("workspace"), dict)
        else {}
    )
    sources = (
        review_data.get("tool_trace_context"),
        workspace.get("tool_trace_context"),
        review_data,
        workspace,
    )
    run_id = ""
    stage_id = ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        if not run_id:
            run_id = str(source.get("workflow_run_id", "") or "").strip()
        if not stage_id:
            stage_id = str(source.get("workflow_stage_id", "") or "").strip()
        if run_id and stage_id:
            break
    return run_id, stage_id


def _approved_action_from_review(review: dict[str, Any]) -> dict[str, Any]:
    resume_result = (
        review.get("resume_result")
        if isinstance(review.get("resume_result"), dict)
        else {}
    )
    approved_action = (
        resume_result.get("approved_action")
        if isinstance(resume_result.get("approved_action"), dict)
        else review.get("approved_action")
    )
    return dict(approved_action) if isinstance(approved_action, dict) else {}


def _approved_action_async_ref(action: dict[str, Any]) -> dict[str, Any]:
    identifier = str(
        action.get("identifier")
        or action.get("issue_identifier")
        or action.get("symphony_identifier")
        or ""
    ).strip()
    if not identifier:
        return {}
    ref = dict(action)
    ref["identifier"] = identifier
    ref["human_review_required"] = False
    return ref


def _merge_async_refs(
    refs: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _key(ref: dict[str, Any]) -> str:
        return str(
            ref.get("identifier")
            or ref.get("issue_identifier")
            or ref.get("job_id")
            or ref.get("id")
            or json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        )

    for ref in [*refs, *additions]:
        if not isinstance(ref, dict) or not ref:
            continue
        key = _key(ref)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(ref))
    return merged

# workflow 阶段上下文 JSON 的默认总字符预算。
# 超出该预算时，会按既定缩减策略逐步降低上下文细节。
WORKFLOW_STAGE_CONTEXT_MAX_CHARS_DEFAULT = 60_000
# 单个字符串字段的最大保留长度，超过后会被截断。
WORKFLOW_STAGE_STRING_MAX_LEN = 1_600
# 递归裁剪上下文时，每个列表最多保留的元素数量。
WORKFLOW_STAGE_LIST_MAX_ITEMS = 16
# 递归裁剪上下文时，每个字典最多保留的键数量。
WORKFLOW_STAGE_DICT_MAX_KEYS = 40
# 保留原始嵌套结构的最大递归深度；超过后会折叠为简化内容。
WORKFLOW_STAGE_MAX_DEPTH = 6
# stage context 的硬上限，避免环境变量配置过大导致请求超限。
WORKFLOW_STAGE_CONTEXT_HARD_CAP_CHARS_DEFAULT = 80_000


@dataclass(frozen=True)
class WorkflowAsyncDeps:
    load_job_registry: Callable[[], dict[str, Any]]
    map_job_runtime_status: Callable[[dict[str, Any]], str]
    get_job_workspace: Callable[[str], dict[str, Any]]
    build_job_workspace_summary: Callable[[str, dict[str, Any]], dict[str, Any]]
    hydrate_workspace_from_job_files: Callable[[dict[str, Any]], dict[str, Any]]
    utc_now_iso: Callable[[], str]


def workflow_stage_template(run: WorkflowRun, stage_id: str):
    snapshot = (run.metadata or {}).get("template_snapshot")
    if not isinstance(snapshot, dict):
        raise HTTPException(
            409,
            "workflow run uses an unsupported legacy format without template_snapshot",
        )
    template = WorkflowTemplateRegistry.validate_payload(snapshot)
    if template is None:
        raise HTTPException(404, f"workflow template not found: {run.template_key}")
    normalized_stage_id = str(stage_id or "").strip()
    for stage in template.stages:
        if stage.stage_id == normalized_stage_id:
            return template, stage
    raise HTTPException(404, f"workflow stage not found: {normalized_stage_id}")


def workflow_prior_stage_summaries(
    run: WorkflowRun,
    current_stage_id: str,
) -> list[dict[str, Any]]:
    stage_by_id = {stage.stage_id: stage for stage in run.stage_states}
    current = stage_by_id.get(str(current_stage_id or "").strip())
    required_stage_ids: set[str] = set()
    pending = list((current.metadata or {}).get("depends_on", []) or []) if current else []
    while pending:
        dependency_id = str(pending.pop() or "").strip()
        if not dependency_id or dependency_id in required_stage_ids:
            continue
        required_stage_ids.add(dependency_id)
        dependency = stage_by_id.get(dependency_id)
        if dependency is not None:
            pending.extend((dependency.metadata or {}).get("depends_on", []) or [])

    summaries: list[dict[str, Any]] = []
    for stage in run.stage_states:
        if stage.stage_id not in required_stage_ids or stage.status != "committed":
            continue
        confirmation_state = (stage.metadata or {}).get("human_confirmation_state")
        confirmation_summary: dict[str, Any] = {}
        if isinstance(confirmation_state, dict):
            confirmation_summary = {
                key: confirmation_state.get(key)
                for key in (
                    "kind",
                    "status",
                    "review_id",
                    "review_identifier",
                    "resolved_by",
                    "resolved_at",
                    "reason",
                )
                if confirmation_state.get(key) not in (None, "")
            }
        summaries.append(
            {
                "stage_id": stage.stage_id,
                "label": stage.label,
                "status": stage.status,
                "summary": str(stage.result_summary or stage.last_error or "")[:1200],
                "artifact_refs": dict(stage.artifact_refs or {}),
                **(
                    {"human_confirmation": confirmation_summary}
                    if confirmation_summary
                    else {}
                ),
            }
        )
    return summaries


def workflow_stage_template_instructions(stage: WorkflowStageTemplate) -> list[str]:
    metadata = dict(stage.metadata or {})
    raw = (
        metadata.get("output_instructions")
        or metadata.get("instructions")
        or metadata.get("prompt_instructions")
    )
    if isinstance(raw, str):
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if isinstance(raw, list):
        return [str(item or "").strip() for item in raw if str(item or "").strip()]
    return []


def workflow_stage_prompt(stage: WorkflowStageTemplate) -> tuple[str, str]:
    metadata = dict(stage.metadata or {})
    raw = metadata.get("prompt_markdown") or metadata.get("prompt")
    prompt = str(raw or "").strip()
    source = str(
        metadata.get("prompt_source")
        or metadata.get("prompt_file")
        or ""
    ).strip()
    return prompt, source


def workflow_stage_context_dict(stage: WorkflowStageTemplate) -> dict[str, Any]:
    data = stage.to_dict(include_prompt_content=False)
    metadata = dict(data.get("metadata") or {})
    if metadata:
        data["metadata"] = metadata
    else:
        data.pop("metadata", None)
    return data


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except Exception:
        return max(minimum, default)


def _bounded_stage_context_budget(
    stage: WorkflowStageTemplate | None = None,
) -> int:
    configured_budget = _env_int(
        "TCODE_WORKFLOW_STAGE_CONTEXT_MAX_CHARS",
        WORKFLOW_STAGE_CONTEXT_MAX_CHARS_DEFAULT,
        minimum=8_000,
    )
    hard_cap = _env_int(
        "TCODE_WORKFLOW_STAGE_CONTEXT_HARD_CAP_CHARS",
        WORKFLOW_STAGE_CONTEXT_HARD_CAP_CHARS_DEFAULT,
        minimum=20_000,
    )
    global_budget = min(configured_budget, hard_cap)
    stage_budget = (getattr(stage, "metadata", {}) or {}).get("context_max_chars")
    if stage_budget is None:
        return global_budget
    try:
        return min(global_budget, max(8_000, int(stage_budget)))
    except (TypeError, ValueError):
        return global_budget


def _shorten_text(value: Any, *, limit: int = WORKFLOW_STAGE_STRING_MAX_LEN) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + " ...[truncated]"


def _trim_for_workflow_context(value: Any, *, depth: int = 0) -> Any:
    if depth >= WORKFLOW_STAGE_MAX_DEPTH:
        if isinstance(value, (dict, list, tuple)):
            return "[truncated-depth]"
        return _shorten_text(value, limit=256)

    if isinstance(value, str):
        return _shorten_text(value)

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    if isinstance(value, list):
        trimmed = [
            _trim_for_workflow_context(item, depth=depth + 1)
            for item in value[:WORKFLOW_STAGE_LIST_MAX_ITEMS]
        ]
        if len(value) > WORKFLOW_STAGE_LIST_MAX_ITEMS:
            trimmed.append(
                f"...[{len(value) - WORKFLOW_STAGE_LIST_MAX_ITEMS} items truncated]"
            )
        return trimmed

    if isinstance(value, tuple):
        return _trim_for_workflow_context(list(value), depth=depth)

    if isinstance(value, dict):
        trimmed: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:WORKFLOW_STAGE_DICT_MAX_KEYS]:
            trimmed[str(key)] = _trim_for_workflow_context(item, depth=depth + 1)
        if len(items) > WORKFLOW_STAGE_DICT_MAX_KEYS:
            trimmed["_truncated_keys"] = len(items) - WORKFLOW_STAGE_DICT_MAX_KEYS
        return trimmed

    return _shorten_text(value)


def _minify_candidate_for_context(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.get("candidate_id", "") or "").strip(),
        "status": str(candidate.get("status", "") or "").strip(),
        "rank": candidate.get("rank"),
        "summary": _shorten_text(candidate.get("summary", ""), limit=1000),
        "score_card": _trim_for_workflow_context(candidate.get("score_card", {}), depth=0),
    }


def _minify_prior_stage_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": str(item.get("stage_id", "") or "").strip(),
        "label": _shorten_text(item.get("label", ""), limit=120),
        "status": str(item.get("status", "") or "").strip(),
        "summary": _shorten_text(item.get("summary", ""), limit=1000),
    }


def _workflow_context_json_with_budget(
    *,
    payload: dict[str, Any],
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    context_json = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(context_json) <= max_chars:
        return context_json, payload

    shrunk = dict(payload)
    shrunk["current_candidates"] = [
        _minify_candidate_for_context(item)
        for item in (payload.get("current_candidates", []) or [])
        if isinstance(item, dict)
    ]
    shrunk["prior_stage_summaries"] = [
        _minify_prior_stage_summary(item)
        for item in (payload.get("prior_stage_summaries", []) or [])
        if isinstance(item, dict)
    ]
    shrunk["input_payload"] = _trim_for_workflow_context(payload.get("input_payload", {}), depth=0)
    shrunk["output_contract"] = _trim_for_workflow_context(payload.get("output_contract", {}), depth=0)
    context_json = json.dumps(shrunk, ensure_ascii=False, indent=2)
    if len(context_json) <= max_chars:
        return context_json, shrunk

    shrunk["current_candidates"] = shrunk.get("current_candidates", [])[:6]
    shrunk["prior_stage_summaries"] = shrunk.get("prior_stage_summaries", [])[:6]
    context_json = json.dumps(shrunk, ensure_ascii=False, indent=2)
    if len(context_json) <= max_chars:
        return context_json, shrunk

    shrunk["output_contract"] = {
        "_truncated": True,
        "keys": sorted(str(k) for k in (payload.get("output_contract", {}) or {}).keys())[:40],
    }
    shrunk["input_payload"] = {
        "_truncated": True,
        "keys": sorted(str(k) for k in (payload.get("input_payload", {}) or {}).keys())[:40],
    }
    shrunk["current_candidates"] = shrunk.get("current_candidates", [])[:3]
    shrunk["prior_stage_summaries"] = shrunk.get("prior_stage_summaries", [])[:4]
    context_json = json.dumps(shrunk, ensure_ascii=False, indent=2)
    if len(context_json) <= max_chars:
        return context_json, shrunk

    shrunk["current_candidates"] = []
    shrunk["prior_stage_summaries"] = []
    shrunk["context_note"] = (
        "Workflow context was heavily truncated to satisfy LLM input budget. "
        "Use available summaries and request explicit artifacts when needed."
    )
    context_json = json.dumps(shrunk, ensure_ascii=False, indent=2)
    if len(context_json) <= max_chars:
        return context_json, shrunk

    hard = {
        "run_id": str(payload.get("run_id", "") or ""),
        "template_key": str(payload.get("template_key", "") or ""),
        "stage": {
            "stage_id": str((payload.get("stage") or {}).get("stage_id", "") or ""),
            "label": str((payload.get("stage") or {}).get("label", "") or ""),
            "owner": str((payload.get("stage") or {}).get("owner", "") or ""),
            "call_type": str((payload.get("stage") or {}).get("call_type", "") or ""),
        },
        "context_note": "Minimal context due to strict prompt budget.",
    }
    return json.dumps(hard, ensure_ascii=False, indent=2), hard


def build_workflow_stage_worker_message(
    run: WorkflowRun,
    stage_id: str,
) -> tuple[str, str]:
    normalized_stage_id = str(stage_id or "").strip()
    template, stage = workflow_stage_template(run, normalized_stage_id)
    worker_name = str(stage.owner or "").strip() or "generalist"
    call_type = str(stage.call_type or "").strip()
    call_type_normalized = call_type.lower()
    executor_kind = (
        "agent"
        if call_type_normalized in {"agent", "subagent", "workflow_agent", "plugin_agent"}
        or call_type_normalized.startswith("agent:")
        else "worker"
    )
    stage_prompt, stage_prompt_source = workflow_stage_prompt(stage)
    stage_state = next(
        (item for item in run.stage_states if item.stage_id == normalized_stage_id),
        None,
    )
    next_attempt = max(1, int(getattr(stage_state, "attempt", 0) or 0) + 1)
    prior_stage_summaries = workflow_prior_stage_summaries(
        run,
        normalized_stage_id,
    )
    upstream_stage_ids = {
        str(item.get("stage_id") or "").strip()
        for item in prior_stage_summaries
        if str(item.get("stage_id") or "").strip()
    }
    context_payload = {
        "run_id": run.run_id,
        "template_key": run.template_key,
        "template_description": template.description,
        "stage": workflow_stage_context_dict(stage),
        "stage_execution": {
            "attempt": next_attempt,
            "idempotency_key": f"{run.run_id}:{normalized_stage_id}:attempt:{next_attempt}",
        },
        "input_payload": dict(run.input_payload or {}),
        "prior_stage_summaries": prior_stage_summaries,
        "current_candidates": [
            item.to_dict()
            for item in run.candidates
            if item.source_stage_id in upstream_stage_ids
        ],
        "output_contract": dict(stage.output_contract or {}),
        "goal_contract": {
            "original_user_request": str(
                dict((run.metadata or {}).get("intent_snapshot") or {}).get(
                    "original_user_request"
                )
                or (run.metadata or {}).get("auto_route_last_message")
                or ""
            )[:6000],
            "applicability_check_phase": "before_start_only",
        },
    }
    context_payload = _trim_for_workflow_context(context_payload, depth=0)
    context_budget = _bounded_stage_context_budget(stage)
    context_json, context_payload = _workflow_context_json_with_budget(
        payload=context_payload,
        max_chars=context_budget,
    )
    lines = [
        "你正在执行一个持久化 protein workflow 的单个阶段。",
        f"阶段 ID：{stage.stage_id}",
        f"阶段名称：{stage.label}",
        f"执行对象：{worker_name}",
        f"执行类型：{executor_kind}",
        f"调用类型：{call_type}",
        f"阶段说明：{stage.description or '(无)'}",
        "优先使用当前执行对象可用且合适的工具；若指定工具不可用，请明确说明限制，并给出基于现有证据的最佳结果。",
        "首次调用工具前，用一至三句公开行动说明交代当前目标和调用目的，并在同一轮发出工具调用；"
        "获得关键证据、遇到失败或调整方向时，继续说明已知发现和下一步。"
        "下述 JSON/Markdown 输出约束仅适用于最终阶段结果，不限制带工具调用的行动说明；"
        "不要在最终 JSON 中混入旁白，也不要输出内部思维链。",
        "输出应简洁、面向决策，并适合作为该阶段的运行摘要持久化。",
        "如果完整阶段结果可能超过单次模型输出预算，不要把大数据拆成多段聊天正文交给下游；"
        "应先用本阶段允许的工具写入 Artifact/文件，并在阶段输出中返回简洁摘要和 artifact_refs。"
        "只有完成续写、通过校验并提交的阶段结果才会进入下游；未完整输出不得声明完成。",
        ("完成输出必须符合本阶段 output_contract：仅返回一个 JSON 对象或 JSON 代码块，不另写 Markdown 表格或阶段总结。"
         if stage.output_contract.get("type") == "object" or any(v.get("type") == "json_schema" for v in stage.validators)
         else "默认输出格式：Markdown。"),
        "除 workflow marker 与 JSON 键名外，所有自然语言内容必须使用简体中文。",
    ]
    if stage.allowed_tools:
        lines.append(
            "Suggested tools for this stage: "
            + ", ".join(stage.allowed_tools)
            + "（本阶段建议工具）"
        )
        lines.append(
            "仅使用本阶段允许的工具；不得改用外部工具绕过阶段范围。"
        )
    required_skills = list(stage.required_skills or [])
    if required_skills:
        lines.append(
            "本阶段必须先调用 `skill` 工具加载这些 skill："
            + ", ".join(required_skills)
            + "。加载后按 skill 文档执行；如果缺少 FASTA/sequence/accession、数据库或运行环境，请明确标记为 BLOCKED/阻塞项，不要只输出泛泛计划。"
        )
    if stage.expected_artifacts:
        lines.append("期望产物：" + ", ".join(stage.expected_artifacts))
    if stage.success_checks:
        lines.append(
            "业务验收要求（以本阶段声明的 validators 与 review_gate 为准）："
            + ", ".join(stage.success_checks)
        )
    if stage.validators:
        lines.append(
            "提交前强制校验器："
            + ", ".join(str(item.get("type") or "unknown") for item in stage.validators)
        )
    if stage.fallback_stage_ids:
        lines.append(
            "当结论为 REVISE 时可回退到阶段："
            + ", ".join(stage.fallback_stage_ids)
        )
    if stage.review_gate:
        lines.append(
            "该阶段为评审闸门；如果证据不足，请明确给出 REVISE 决策和所需返工目标。"
        )
    if stage_prompt:
        source_text = f"（来源：{stage_prompt_source}）" if stage_prompt_source else ""
        lines.extend(
            [
                f"阶段专属 Markdown Prompt{source_text}：",
                "请优先遵循下面的阶段专属 prompt；若它与通用 workflow marker、上下文完整性或安全约束冲突，以通用 workflow 约束为准。",
                "```markdown",
                stage_prompt,
                "```",
            ]
        )
    lines.extend(
        [
            "当你发现候选或更新在落地前需要审查时，请单独一行输出 workflow marker：",
            "[workflow:candidate]{\"candidate_id\":\"...\",\"summary\":\"...\",\"status\":\"draft\",\"rank\":1,\"score_card\":{},\"artifact_refs\":{},\"metadata\":{}}[/workflow:candidate]",
            "[workflow:revision]{\"summary\":\"...\",\"reason\":\"...\",\"target_stage_id\":\"...\"}[/workflow:revision]",
            "[workflow:final_report]{\"summary\":\"...\",\"final_report\":\"...\"}[/workflow:final_report]",
            "final_report 必须以 goal_contract.original_user_request 为回答目标，形成可直接展示给用户的"
            "完整答复：结论优先，并整合关键结果、依据、限制和下一步；不要把阶段日志或内部"
            "Workflow 报告原样拼接后交给用户。",
            "Workflow 适用性仅在启动前检查。运行中不再判断或询问整个 Workflow 或某一步是否适用，"
            "不要因适用范围重新要求用户确认。按既定模板执行本阶段；如结果存在限制或未覆盖的"
            "目标，请如实写入阶段结果和最终答复，不得臆造证据、跳过必需步骤或宣称未完成的任务已完成。"
            "必要输入、工具错误和结果校验仍按本阶段既有规则处理。",
        ]
    )

    template_instructions = workflow_stage_template_instructions(stage)
    if template_instructions:
        lines.extend(template_instructions)
    elif stage_prompt:
        lines.append("请按阶段专属 Markdown Prompt 完成本阶段，并返回 Markdown 结果。")
    elif normalized_stage_id == "workflow_bootstrap":
        lines.extend(
            [
                "请返回 Markdown，并包含这些小节：规范化目标、输入检查、下一步说明。",
                "如果缺少必需输入，请显式列出缺失项。",
            ]
        )
    elif normalized_stage_id == "requirements_freeze":
        lines.extend(
            [
                "请返回 Markdown，并包含这些小节：功能目标、约束条件、成功指标、停止准则、开放问题。",
                "语言要明确、可直接执行。",
            ]
        )
    elif normalized_stage_id == "candidate_generation":
        lines.extend(
            [
                "请基于冻结后的需求生成 2 到 4 个候选设计方案。",
                "请返回 Markdown（可含简短标题），并在 ```json 代码块中给出候选 payload，结构如下：",
                "{\"candidates\": [{\"candidate_id\": \"...\", \"summary\": \"...\", \"status\": \"draft\", \"rank\": 1, \"score_card\": {\"confidence_score\": 0.0}, \"artifact_refs\": {\"design_notes\": \"...\"}, \"metadata\": {\"rationale\": \"...\"}}]}",
            ]
        )
    elif normalized_stage_id in {"scoring", "candidate_aggregation"}:
        lines.extend(
            [
                "请尽可能更新候选卡片。",
                "若更新候选，请返回 Markdown，并包含 ```json 代码块；其中 candidates 数组需包含 candidate_id、summary、status、rank、score_card、artifact_refs、metadata。",
                "若无法更新候选，请用 Markdown 说明证据、限制和下一步动作。",
            ]
        )
    elif normalized_stage_id == "review_gate":
        lines.extend(
            [
                "请作为独立评审代理，质疑关键假设，并核查候选短名单是否有阶段证据支撑。",
                "请返回 Markdown，包含：决策（PASS 或 REVISE）、关键证据、风险、所需修订、批准短名单。",
            ]
        )
    elif normalized_stage_id == "decision_report":
        lines.extend(
            [
                "请产出面向用户的最终决策报告。",
                "请返回 Markdown，包含：推荐候选、推荐理由、主要风险、建议的后续实验、开放问题。",
            ]
        )
    else:
        lines.append("请返回 Markdown，包含：结果、产物、风险、下一步行动。")

    lines.extend(
        [
            "Workflow 运行时上下文：",
            "```json",
            context_json,
            "```",
        ]
    )
    lines.append(
        "若发现上下文被截断，请优先用简洁工具调用补齐必要产物后再下最终结论。"
    )
    return worker_name, "\n".join(lines)


def workflow_stage_outcome_contract(stage: WorkflowStageTemplate) -> dict[str, Any] | None:
    """Compile natural-language stage acceptance checks for the independent validator."""

    checks = [str(item).strip() for item in stage.success_checks if str(item).strip()]
    if not checks:
        return None
    retry_policy = dict(stage.retry_policy or {})
    try:
        max_repairs = max(0, min(2, int(retry_policy.get("max_attempts", 2)) - 1))
    except (TypeError, ValueError):
        max_repairs = 1
    risk_level = str((stage.metadata or {}).get("risk_level") or "low").strip().lower()
    if risk_level not in {"low", "medium", "high", "critical"}:
        risk_level = "low"
    stage_tool_names = [
        str(item).strip()
        for item in (stage.allowed_tools or [])
        if str(item).strip() and str(item).strip().lower() != "skill"
    ]
    verifier_refs = ["semantic"]
    verifier_parameters: dict[str, Any] = {}
    if risk_level in {"high", "critical"} and stage_tool_names:
        # Outcome v2 requires every high-risk criterion to carry a criterion-
        # bound non-semantic P2 fact.  Workflow stages already execute declared
        # tools and are followed by the workflow's own deterministic validators,
        # so bind successful declared-tool evidence to each semantic criterion.
        # This preserves high-risk proof gating without asking for a human
        # confirmation between the model response and artifact validation.
        verifier_refs.append("tool_success")
        verifier_parameters = {
            "tool_names": stage_tool_names,
            "require_all": True,
        }
    stage_goal = (
        f"完成 Workflow 阶段 {stage.label}（{stage.stage_id}）"
        + (f"：{stage.description}" if str(stage.description or "").strip() else "。")
    )
    return {
        "schema_version": "tcode.outcome/v2",
        "goal": stage_goal,
        "title": f"Workflow stage acceptance: {stage.label}",
        "intent": stage.description or stage.label,
        "risk_level": risk_level,
        "required_proof_level": "P1",
        "semantic_validation": "always",
        "max_repair_attempts": max_repairs,
        # Intermediate workflow stages must be judged against the stage contract,
        # not OutcomeContract.compile()'s default whole-user-request criteria.
        "acceptance_criteria": checks,
        "deliverables": [
            {
                "name": "stage_result",
                "kind": "message",
                "required": True,
            }
        ],
        "success_criteria": [
            {
                "description": check,
                "required": True,
                "verifier_refs": list(verifier_refs),
                **(
                    {"parameters": dict(verifier_parameters)}
                    if verifier_parameters
                    else {}
                ),
            }
            for check in checks
        ],
        "metadata": {
            "planning": {
                "source": "workflow",
                "reason_code": "workflow_natural_language_success_checks",
                "level": "V3" if risk_level in {"high", "critical"} else "V2",
                "stage_id": stage.stage_id,
            }
        },
    }


def extract_json_payload_from_text(raw_text: str) -> Any:
    text = str(raw_text or "").strip()
    if not text:
        return None

    fenced_blocks = re.findall(
        r"```(?:json)?\s*([\s\S]*?)```",
        text,
        flags=re.IGNORECASE,
    )
    for block in fenced_blocks:
        candidate = str(block or "").strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
            return payload
        except Exception:
            continue
    return None


def extract_workflow_candidates_from_response(
    run: WorkflowRun,
    stage_id: str,
    response: str,
    *,
    allow_fallback: bool = True,
) -> list[WorkflowCandidate]:
    payload = extract_json_payload_from_text(response)
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_candidates = payload.get("candidates")
        if isinstance(raw_candidates, list):
            items = [item for item in raw_candidates if isinstance(item, dict)]
        elif isinstance(raw_candidates, dict):
            items = [raw_candidates]
        elif payload.get("candidate_id"):
            items = [payload]
    elif isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]

    normalized: list[WorkflowCandidate] = []
    existing_count = len(run.candidates or [])
    for index, item in enumerate(items, start=1):
        candidate_id = (
            str(item.get("candidate_id") or "").strip()
            or f"{run.run_id}-cand-{existing_count + index}"
        )
        summary = str(item.get("summary") or item.get("description") or "").strip()
        status = str(item.get("status") or "draft").strip() or "draft"
        rank_value = item.get("rank")
        try:
            rank = int(rank_value) if rank_value is not None else index
        except Exception:
            rank = index
        score_card = (
            dict(item.get("score_card") or {})
            if isinstance(item.get("score_card"), dict)
            else {}
        )
        artifact_refs = (
            dict(item.get("artifact_refs") or {})
            if isinstance(item.get("artifact_refs"), dict)
            else {}
        )
        metadata = (
            dict(item.get("metadata") or {})
            if isinstance(item.get("metadata"), dict)
            else {}
        )
        normalized.append(
            WorkflowCandidate(
                candidate_id=candidate_id,
                source_stage_id=stage_id,
                status=status,
                rank=rank,
                score_card=score_card,
                artifact_refs=artifact_refs,
                summary=summary,
                metadata=metadata,
            )
        )

    if normalized:
        return normalized

    if not allow_fallback:
        return []

    fallback_summary = str(response or "").strip()
    if not fallback_summary:
        return []
    return [
        WorkflowCandidate(
            candidate_id=f"{run.run_id}-cand-{existing_count + 1}",
            source_stage_id=stage_id,
            status="draft",
            rank=1,
            summary=fallback_summary[:800],
            metadata={"generated_from_response": True},
        )
    ]


def workflow_candidate_result_summary(candidates: list[WorkflowCandidate]) -> str:
    if not candidates:
        return "candidate generation completed"
    lines = [f"Generated {len(candidates)} candidate(s) via design worker."]
    for candidate in candidates[:3]:
        line = f"- {candidate.candidate_id}"
        if candidate.summary:
            line = f"{line}: {candidate.summary[:180]}"
        lines.append(line)
    return "\n".join(lines)


def workflow_marker_summary(marker_type: str, payload: dict[str, Any]) -> str:
    if marker_type == "candidate":
        candidate = (
            payload.get("candidate")
            if isinstance(payload.get("candidate"), dict)
            else payload
        )
        return str(
            candidate.get("summary")
            or candidate.get("description")
            or candidate.get("candidate_id")
            or "candidate proposal"
        )[:1200]
    if marker_type == "revision":
        return str(
            payload.get("summary")
            or payload.get("reason")
            or payload.get("required_revision")
            or "revision proposal"
        )[:1200]
    return str(
        payload.get("summary")
        or payload.get("final_report")
        or payload.get("report")
        or "final report proposal"
    )[:1200]


def workflow_review_decision(text: str) -> str:
    """解析复核结论，并排除“无需 REVISE”这类否定表达。"""

    raw = str(text or "")
    normalized = raw.strip().lower()
    if not normalized:
        return ""
    first_line = next(
        (line.strip() for line in raw.splitlines() if line.strip()),
        "",
    )
    if re.match(r"^ESCALATE\b", first_line, flags=re.IGNORECASE):
        return "escalate"
    if re.match(r"^REVISE\b", first_line, flags=re.IGNORECASE):
        return "revise"
    if re.match(r"^PASS\b", first_line, flags=re.IGNORECASE):
        return "pass"
    decision_text = re.sub(
        r"(?:无需|不需要|不需|不必|不建议)\s*(?:[`*_]+\s*)?"
        r"(?:REVISE\b|返工|修订)",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    normalized_decision = decision_text.strip().lower()
    if (
        re.search(r"\bESCALATE\b", decision_text, flags=re.IGNORECASE)
        or "升级人工" in normalized_decision
        or "需要人工" in normalized_decision
    ):
        return "escalate"
    if (
        re.search(r"\bREVISE\b", decision_text, flags=re.IGNORECASE)
        or "需要返工" in normalized_decision
        or "需返工" in normalized_decision
        or "未通过" in normalized_decision
        or "不通过" in normalized_decision
    ):
        return "revise"
    if (
        re.search(r"\bPASS\b", decision_text, flags=re.IGNORECASE)
        or "通过" in normalized_decision
    ):
        return "pass"
    return ""


def workflow_review_revision_payload(
    stage: WorkflowStageTemplate,
    response: str,
) -> dict[str, Any]:
    fallback_ids = [
        str(item or "").strip()
        for item in (stage.fallback_stage_ids or [])
        if str(item or "").strip()
    ]
    target_stage_id = fallback_ids[0] if fallback_ids else stage.stage_id
    summary = str(response or "").strip()
    return {
        "summary": summary[:1200] or "Review gate requested revision",
        "reason": summary[:1200] or "Review gate returned REVISE",
        "target_stage_id": target_stage_id,
        "fallback_stage_ids": fallback_ids,
    }


def declared_tool_revision_payload(stage, response, *, allowed_stage_ids):
    """显式启用的工具回执修订目标；不改变审批策略、循环预算或其他 Workflow。"""
    field = (stage.metadata or {}).get("revision_request_field")
    if not field:
        return None
    source = (stage.metadata or {}).get("result_source") or {}
    if source.get("type") != "tool_receipt" or source.get("tool") not in stage.allowed_tools:
        raise ValueError("revision request requires an authoritative tool receipt")
    payload = extract_json_payload_from_text(response)
    revision_decisions = (stage.metadata or {}).get("revision_decisions", ["REVISE"])
    if not isinstance(revision_decisions, list) or any(not isinstance(value, str) for value in revision_decisions):
        raise ValueError("revision_decisions must be a declared string list")
    if not isinstance(payload, dict) or payload.get("review_decision") not in revision_decisions:
        return None
    request = payload
    for part in str(field).split("."):
        request = request.get(part) if isinstance(request, dict) else None
    if not isinstance(request, dict) or request.get("target_stage_id") is None:
        return None  # 纯交付故障：终止探索，不循环重做化学。
    target = request["target_stage_id"]
    declared = (stage.metadata or {}).get("revision_target_stage_ids", [])
    if not isinstance(target, str) or target not in declared or target not in allowed_stage_ids:
        raise ValueError("revision target must be explicitly declared and belong to this Workflow")
    reason = str(request.get("reason") or "定向修订")[:1200]
    return {"summary": reason, "reason": reason, "target_stage_id": target,
            "revision_tasks": request.get("tasks", [])}


def workflow_async_stage_status(async_refs: list[dict[str, Any]]) -> str:
    if any(bool(ref.get("human_review_required")) for ref in async_refs):
        return "review_pending"
    return "waiting_external_job"


def workflow_async_stage_summary(status: str, async_refs: list[dict[str, Any]]) -> str:
    label = "等待人工审核" if status == "review_pending" else "等待异步任务完成"
    identifiers = [
        str(ref.get("identifier") or ref.get("job_id") or ref.get("issue_id") or "").strip()
        for ref in async_refs
    ]
    identifiers = [item for item in identifiers if item]
    if identifiers:
        return f"{label}: {', '.join(identifiers[:3])}"
    return label


def workflow_async_ref_task(ref: dict[str, Any], workdir: str) -> Optional[dict[str, Any]]:
    import os
    from pathlib import Path

    def _append_workdir(candidates: list[str], value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        normalized = str(Path(text))
        if normalized not in candidates:
            candidates.append(normalized)

    def _infer_issue_store_workdir(ref_item: dict[str, Any]) -> str:
        # 优先使用 submit_job 返回中的 issue_store，避免依赖 TCODE_WORKDIR。
        metadata = ref_item.get("metadata") if isinstance(ref_item.get("metadata"), dict) else {}
        issue_store = str(
            ref_item.get("issue_store")
            or metadata.get("issue_store")
            or ""
        ).strip()
        if not issue_store:
            return ""
        db_path = Path(issue_store)
        if db_path.name != "shared_tracker.sqlite":
            return ""
        return str(db_path.parent.parent)

    candidate_workdirs: list[str] = []
    _append_workdir(candidate_workdirs, workdir)
    _append_workdir(candidate_workdirs, _infer_issue_store_workdir(ref))
    
    identifier = str(
        ref.get("identifier")
        or ref.get("issue_identifier")
        or ref.get("symphony_identifier")
        or ""
    ).strip()
    task_id = str(ref.get("issue_id") or ref.get("id") or "").strip()
    if identifier or task_id:
        for candidate in candidate_workdirs:
            task = SharedTracker.get_task(
                task_id=task_id,
                identifier=identifier,
                workdir=candidate,
            )
            if task:
                return task
        
        # Fallback: 如果在工作区 workdir 里没找到，尝试主 TCODE_WORKDIR
        # （e.g. workflow 的 workspace-2 DB 是空的，但 issue 写入了 tcode 主 workdir）
        main_workdir = str(os.environ.get("TCODE_WORKDIR", "") or "").strip()
        if main_workdir and Path(main_workdir).is_dir() and main_workdir != workdir:
            normalized_main = str(Path(main_workdir))
            if normalized_main not in candidate_workdirs:
                task = SharedTracker.get_task(
                    task_id=task_id,
                    identifier=identifier,
                    workdir=normalized_main,
                )
                if task:
                    return task

    job_id = str(ref.get("job_id") or "").strip()
    if not job_id:
        return None
    for candidate in candidate_workdirs:
        for task in reversed(SharedTracker.list_tasks(workdir=candidate, limit=200)):
            workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
            job_ref = task.get("job_ref") if isinstance(task.get("job_ref"), dict) else {}
            workspace_job_ref = (
                workspace.get("job_ref")
                if isinstance(workspace.get("job_ref"), dict)
                else {}
            )
            if job_id in {
                str(workspace.get("job_id") or "").strip(),
                str(job_ref.get("job_id") or "").strip(),
                str(workspace_job_ref.get("job_id") or "").strip(),
            }:
                return task
    
    # Fallback for job_id: 同样尝试主 TCODE_WORKDIR
    main_workdir = str(os.environ.get("TCODE_WORKDIR", "") or "").strip()
    if main_workdir and Path(main_workdir).is_dir() and main_workdir != workdir:
        normalized_main = str(Path(main_workdir))
        if normalized_main in candidate_workdirs:
            return None
        for task in reversed(SharedTracker.list_tasks(workdir=normalized_main, limit=200)):
            workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
            job_ref = task.get("job_ref") if isinstance(task.get("job_ref"), dict) else {}
            workspace_job_ref = (
                workspace.get("job_ref")
                if isinstance(workspace.get("job_ref"), dict)
                else {}
            )
            if job_id in {
                str(workspace.get("job_id") or "").strip(),
                str(job_ref.get("job_id") or "").strip(),
                str(workspace_job_ref.get("job_id") or "").strip(),
            }:
                return task
    return None


def workflow_async_ref_status(
    ref: dict[str, Any],
    *,
    workdir: str,
    deps: WorkflowAsyncDeps,
) -> tuple[str, dict[str, Any]]:
    task = workflow_async_ref_task(ref, workdir)
    if task:
        workspace = task.get("workspace") or {}
        if (workspace.get("cancellation") or {}).get("request_id"):
            return "cancelled", {"task": task}
        if (task.get("job_ref") or {}).get("job_id") and str(task.get("status") or "").lower() == "completed" and workspace.get("transfer_state") != "ready":
            return "collecting", {"task": task}
        # 优先用 state（issue 业务状态：Done/In Progress/...），
        # tracker_status 是 Symphony 执行通道内部状态（claimed/running/...），
        # 不能作为业务完成判断依据，降为兜底。
        status = str(
            task.get("state")
            or task.get("status")
            or task.get("tracker_status")
            or ""
        ).strip().lower()
        return status, {"task": task}

    identifier = str(ref.get("identifier") or "").strip()
    review = SharedTracker.get_review(identifier=identifier, workdir=workdir) if identifier else None
    if review:
        status = str(review.get("status", "") or "").strip().lower()
        return status, {"review": review}

    job_id = str(ref.get("job_id") or "").strip()
    if job_id:
        registry = deps.load_job_registry()
        job = registry.get(job_id)
        if isinstance(job, dict):
            return deps.map_job_runtime_status(job), {"job": job, "job_id": job_id}

    return "", {}


def workflow_async_artifact_refs(
    details: dict[str, Any],
    ref: dict[str, Any],
    *,
    deps: WorkflowAsyncDeps,
) -> dict[str, Any]:
    task = details.get("task") if isinstance(details.get("task"), dict) else {}
    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    job = details.get("job") if isinstance(details.get("job"), dict) else {}
    job_id = str(
        ref.get("job_id")
        or workspace.get("job_id")
        or (workspace.get("job_ref") or {}).get("job_id")
        or (task.get("job_ref") or {}).get("job_id")
        or details.get("job_id")
        or ""
    ).strip()
    hydrated_workspace: dict[str, Any] = {}
    if job_id:
        try:
            hydrated_workspace = deps.get_job_workspace(job_id)
        except Exception:
            hydrated_workspace = {}
    if not hydrated_workspace and job:
        hydrated_workspace = deps.build_job_workspace_summary(job_id, job) if job_id else {}
    if not hydrated_workspace and workspace:
        hydrated_workspace = deps.hydrate_workspace_from_job_files(dict(workspace))

    artifact_refs: dict[str, Any] = {}
    issue_identifier = str(
        ref.get("identifier")
        or task.get("identifier")
        or workspace.get("issue_identifier")
        or ""
    ).strip()
    if issue_identifier:
        artifact_refs["issue"] = issue_identifier
    if job_id:
        artifact_refs["job"] = job_id
    workdir_ref = str(
        hydrated_workspace.get("workdir") or workspace.get("workdir") or ""
    ).strip()
    if workdir_ref:
        artifact_refs["workspace"] = workdir_ref
    output_root = str(hydrated_workspace.get("output_root") or "").strip()
    if output_root:
        artifact_refs["output_root"] = output_root
    artifacts = hydrated_workspace.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        artifact_refs["artifacts"] = [str(item) for item in artifacts if str(item).strip()]
    return artifact_refs


def sync_workflow_human_review_decision(
    review: dict[str, Any],
    workdir: str,
    *,
    decision: str = "approved",
    deps: WorkflowAsyncDeps,
) -> Optional[WorkflowRun]:
    """Reflect an approved/rejected human review back onto a waiting workflow stage."""

    run_id, stage_id = _review_workflow_context(review)
    if not run_id or not stage_id:
        return None
    run = WorkflowRunManager.get(run_id, workdir=workdir)
    if run is None:
        return None
    stage = next((item for item in run.stage_states if item.stage_id == stage_id), None)
    if stage is None or str(stage.status or "").strip() not in WORKFLOW_WAITING_STAGE_STATUSES:
        return run

    normalized_decision = str(decision or review.get("status") or "").strip().lower()
    if is_workflow_confirmation_review(review):
        if normalized_decision not in {"approved", "rejected"}:
            return run
        reason = str(
            review.get("rejection_reason")
            or review.get("comments")
            or review.get("resume_instruction")
            or ""
        ).strip()
        return WorkflowRunManager.resolve_stage_human_confirmation(
            run_id,
            stage_id,
            decision=normalized_decision,
            actor=str(review.get("reviewer") or "human").strip() or "human",
            reason=reason,
            review_id=str(review.get("id") or "").strip(),
            review_identifier=str(review.get("identifier") or "").strip(),
            resolved_at=str(review.get("reviewed_at") or deps.utc_now_iso()),
            workdir=workdir,
        )

    if normalized_decision == "rejected":
        reason = str(
            review.get("rejection_reason")
            or review.get("comments")
            or "人工审核拒绝"
        ).strip()
        return WorkflowRunManager.mark_stage_failed(
            run_id,
            stage_id,
            error=reason,
            metadata={
                "human_review_synced_at": deps.utc_now_iso(),
                "human_review_status": "rejected",
                "human_review_identifier": str(review.get("identifier", "") or ""),
            },
            workdir=workdir,
        )

    if normalized_decision != "approved":
        return run

    action = _approved_action_from_review(review)
    action_status = str(action.get("status", "") or "").strip().lower()
    if action_status in {"failed", "error"}:
        return WorkflowRunManager.mark_stage_failed(
            run_id,
            stage_id,
            error=str(action.get("error") or "人工审核通过后提交异步任务失败"),
            metadata={
                "human_review_synced_at": deps.utc_now_iso(),
                "human_review_status": "approved",
                "approved_action": action,
            },
            workdir=workdir,
        )
    if action_status == "revision_requested":
        return WorkflowRunManager.update_status(
            run_id,
            status="blocked",
            current_stage_id=stage_id,
            metadata={
                "human_review_synced_at": deps.utc_now_iso(),
                "human_review_status": "approved",
                "human_review_revision_requested": True,
                "human_review_resume_instruction": str(
                    review.get("resume_instruction") or review.get("comments") or ""
                )[:1200],
            },
            workdir=workdir,
        )

    action_ref = _approved_action_async_ref(action)
    if not action_ref:
        return run

    metadata = dict(stage.metadata or {})
    refs = [
        dict(item)
        for item in metadata.get("async_tool_refs", []) or []
        if isinstance(item, dict) and not bool(item.get("human_review_required"))
    ]
    refs = _merge_async_refs(refs, [action_ref])
    identifier = str(action_ref.get("identifier") or "").strip()
    return WorkflowRunManager.mark_stage_waiting(
        run_id,
        stage_id,
        status="waiting_external_job",
        result_summary=(
            f"人工审核已通过，异步任务已提交: {identifier}"
            if identifier
            else "人工审核已通过，异步任务已提交"
        ),
        metadata={
            **metadata,
            "async_tool_refs": refs,
            "async_ref_count": len(refs),
            "human_review_synced_at": deps.utc_now_iso(),
            "human_review_status": "approved",
            "human_review_identifier": str(review.get("identifier", "") or ""),
            "approved_action": action,
        },
        workdir=workdir,
    )


def sync_workflow_async_waiting_run(
    run: WorkflowRun,
    workdir: str,
    *,
    deps: WorkflowAsyncDeps,
) -> WorkflowRun:
    ensure_workflow_confirmation_review(run, workdir=workdir)
    current = run
    for stage in list(current.stage_states):
        if str(stage.status or "").strip() not in WORKFLOW_WAITING_STAGE_STATUSES:
            continue
        metadata = dict(stage.metadata or {})
        refs = [
            dict(item)
            for item in metadata.get("async_tool_refs", []) or []
            if isinstance(item, dict)
        ]
        if not refs:
            continue

        statuses: list[str] = []
        detail_items: list[dict[str, Any]] = []
        approved_action_refs: list[dict[str, Any]] = []
        approved_action_failures: list[str] = []
        for ref in refs:
            status, details = workflow_async_ref_status(
                ref,
                workdir=workdir,
                deps=deps,
            )
            statuses.append(status)
            detail_items.append({"ref": ref, "status": status, "details": details})
            review = details.get("review") if isinstance(details.get("review"), dict) else {}
            if str(review.get("status", "") or "").strip().lower() == "approved":
                action = _approved_action_from_review(review)
                action_status = str(action.get("status", "") or "").strip().lower()
                if action_status in {"failed", "error"}:
                    approved_action_failures.append(
                        str(action.get("error") or "人工审核通过后提交异步任务失败")
                    )
                else:
                    action_ref = _approved_action_async_ref(action)
                    if action_ref:
                        approved_action_refs.append(action_ref)

        if approved_action_failures:
            current = WorkflowRunManager.mark_stage_failed(
                current.run_id,
                stage.stage_id,
                error=approved_action_failures[0],
                metadata={
                    "async_synced_at": deps.utc_now_iso(),
                    "human_review_action_failed": True,
                },
                workdir=workdir,
            )
            continue

        if approved_action_refs:
            refs_without_review = [
                dict(item)
                for item in refs
                if isinstance(item, dict) and not bool(item.get("human_review_required"))
            ]
            merged_refs = _merge_async_refs(refs_without_review, approved_action_refs)
            current = WorkflowRunManager.mark_stage_waiting(
                current.run_id,
                stage.stage_id,
                status="waiting_external_job",
                result_summary=workflow_async_stage_summary(
                    "waiting_external_job",
                    approved_action_refs,
                ),
                metadata={
                    **metadata,
                    "async_tool_refs": merged_refs,
                    "async_ref_count": len(merged_refs),
                    "human_review_synced_at": deps.utc_now_iso(),
                    "human_review_status": "approved",
                },
                workdir=workdir,
            )
            continue

        if any(status in WORKFLOW_FAILED_STATUSES for status in statuses):
            failed = next(
                item for item in detail_items if item["status"] in WORKFLOW_FAILED_STATUSES
            )
            details = failed.get("details") if isinstance(failed.get("details"), dict) else {}
            task = details.get("task") if isinstance(details.get("task"), dict) else {}
            error_summary = str(
                task.get("error_summary")
                or task.get("result_summary")
                or f"异步任务失败: {failed.get('status')}"
            )
            current = WorkflowRunManager.mark_stage_failed(
                current.run_id,
                stage.stage_id,
                error=error_summary,
                metadata={
                    "async_synced_at": deps.utc_now_iso(),
                    "async_terminal_statuses": statuses,
                },
                workdir=workdir,
            )
            continue

        if statuses and all(status in WORKFLOW_COMPLETED_STATUSES for status in statuses):
            summaries: list[str] = []
            artifact_refs: dict[str, Any] = {}
            for item in detail_items:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                task = details.get("task") if isinstance(details.get("task"), dict) else {}
                ref = item.get("ref") if isinstance(item.get("ref"), dict) else {}
                summary = str(task.get("result_summary") or "").strip()
                if summary:
                    summaries.append(summary)
                artifact_refs.update(workflow_async_artifact_refs(details, ref, deps=deps))
            result_summary = (
                "\n".join(summaries).strip()
                or stage.result_summary
                or "异步任务已完成"
            )
            _, stage_template = workflow_stage_template(current, stage.stage_id)
            from ..workflow.executor import validate_and_commit_stage

            commit_result = validate_and_commit_stage(
                run_id=current.run_id,
                stage=stage_template,
                response=result_summary,
                artifact_refs=artifact_refs,
                metadata={
                    "async_synced_at": deps.utc_now_iso(),
                    "async_terminal_statuses": statuses,
                    "async_completed_refs": refs,
                },
                draft_metadata={"async_refs": refs},
                workdir=workdir,
            )
            current = commit_result.run
    return current


def assert_workflow_session_owner(run: WorkflowRun, session_id: str) -> None:
    normalized = str(session_id or "").strip()
    if normalized and run.session_id != normalized:
        raise HTTPException(403, "workflow run does not belong to this session")


def raise_workflow_version_conflict(exc: WorkflowVersionConflictError) -> None:
    raise HTTPException(
        409,
        {
            "error": "workflow_version_conflict",
            "expected_version": exc.expected_version,
            "actual_version": exc.actual_version,
        },
    )


def get_workflow_run_or_404(
    run_id: str,
    *,
    workdir: str,
    session_id: str = "",
) -> WorkflowRun:
    try:
        run = WorkflowRunManager.get(run_id, workdir=workdir)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if run is None:
        raise HTTPException(404, f"workflow run not found: {run_id}")
    assert_workflow_session_owner(run, session_id)
    return run
