"""Harness-level progress ledger and handoff protocol for delegated work.

Delegation boundaries must preserve successful side effects even when a child
agent later becomes blocked, fails review, or exhausts its retry budget.  This
module is intentionally independent of ``task`` and ``assign_task`` so every
delegation implementation can expose the same parent-facing contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


_SUCCESS_STATES = {"completed", "complete", "success", "succeeded"}
_FAILED_STATES = {"error", "failed", "failure", "blocked", "cancelled", "canceled"}

_ASYNC_SUBMISSION_REQUEST_PATTERNS = (
    re.compile(
        r"(?i)(?<!已)(?:重新)?(?:提交|投递|入队|发起).{0,32}"
        r"(?:异步|任务|作业|请求|job|task|request|workflow|mcp)"
    ),
    re.compile(
        r"(?i)(?:异步|任务|作业|请求|job|task|request|workflow|mcp)"
        r".{0,20}(?<!已)(?:提交|投递|入队|发起)"
    ),
    re.compile(r"(?i)(?:启动|运行).{0,16}异步(?:任务|作业|请求)"),
    re.compile(
        r"(?i)\b(?:submit|enqueue|dispatch|launch)\b.{0,40}"
        r"\b(?:async(?:hronous)?|job|task|request|workflow|mcp)\b"
    ),
    re.compile(
        r"(?i)\b(?:async(?:hronous)?|job|task|request|workflow|mcp)\b.{0,24}"
        r"\b(?:submit|enqueue|dispatch|launch)\b"
    ),
)
_ASYNC_SUBMISSION_SUCCESS_PATTERNS = (
    re.compile(r"(?i)(?:成功提交|提交成功|已成功投递|投递成功|已入队|成功入队)"),
    re.compile(r"(?i)(?:任务|作业|请求).{0,12}(?:已提交|已投递|已受理|已入队)"),
    re.compile(r"(?i)\baccepted\s+async(?:hronous)?\b"),
    re.compile(r"(?i)\b(?:successfully\s+submitted|submitted\s+successfully)\b"),
    re.compile(r"(?i)\b(?:job|task|request|workflow)\b.{0,16}\b(?:submitted|accepted|queued)\b"),
)
_LOCAL_IDENTIFIER_PATTERN = re.compile(r"(?i)\bLOCAL-[A-Z0-9][A-Z0-9._:-]*")
_LABELED_IDENTIFIER_PATTERN = re.compile(
    r"(?i)(?:受理\s*ID|identifier|local[_ -]?(?:request[_ -]?)?id|"
    r"request[_ -]?id|job[_ -]?id)\s*[:：=]\s*`?([A-Z0-9][A-Z0-9._:-]{3,})"
)
_SUBMISSION_METADATA_IDENTIFIER_KEYS = {
    "identifier",
    "symphony_identifier",
    "local_identifier",
    "local_request_id",
    "request_id",
    "job_id",
    "process_id",
    "run_id",
    "workflow_run_id",
}


def _submission_identifiers(value: Any) -> list[str]:
    """Extract stable async identifiers from text or structured metadata."""

    identifiers: list[str] = []

    def _append(candidate: Any) -> None:
        normalized = str(candidate or "").strip().strip("`'\".,;，。；")
        if normalized and normalized not in identifiers:
            identifiers.append(normalized)

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key or "").strip().lower() in _SUBMISSION_METADATA_IDENTIFIER_KEYS:
                _append(item)
            if isinstance(item, (dict, list, tuple)):
                for identifier in _submission_identifiers(item):
                    _append(identifier)
    elif isinstance(value, (list, tuple)):
        for item in value:
            for identifier in _submission_identifiers(item):
                _append(identifier)
    else:
        text = str(value or "")
        for match in _LOCAL_IDENTIFIER_PATTERN.finditer(text):
            _append(match.group(0))
        for match in _LABELED_IDENTIFIER_PATTERN.finditer(text):
            _append(match.group(1))
    return identifiers


def validate_async_submission_claim(
    *,
    request_text: Any,
    result_text: Any,
    tool_trace: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Verify delegated async-submission claims against successful tool evidence.

    The check is target-agnostic: any delegated request that asks to submit an
    asynchronous job must show a successful ``submit_job``-style call (or an
    explicit ``accepted_async`` result).  When the child reports identifiers,
    those exact identifiers must also occur in the successful tool evidence.
    """

    request = str(request_text or "").strip()
    result = str(result_text or "").strip()
    has_submission_intent = any(
        pattern.search(request) for pattern in _ASYNC_SUBMISSION_REQUEST_PATTERNS
    )
    claims_submission_success = any(
        pattern.search(result) for pattern in _ASYNC_SUBMISSION_SUCCESS_PATTERNS
    )
    claimed_identifiers = _submission_identifiers(result)

    base = {
        "schema": "tcode.delegated-claim-validation.v1",
        "claim_type": "async_submission",
        "required": bool(has_submission_intent and claims_submission_success),
        "valid": True,
        "status": "not_applicable",
        "claimed_identifiers": claimed_identifiers,
        "evidence_identifiers": [],
        "evidence_tools": [],
        "reason": "",
    }
    if not base["required"]:
        return base

    evidence_identifiers: list[str] = []
    evidence_tools: list[str] = []
    for item in tool_trace or []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        error = str(item.get("error_preview") or "").strip()
        if error or (state and state not in _SUCCESS_STATES):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        tool_name_lower = tool_name.lower()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        route_mode = str(metadata.get("route_mode") or "").strip().lower()
        is_submission_evidence = bool(
            "submit_job" in tool_name_lower
            or metadata.get("accepted_async") is True
            or route_mode.startswith("job-protocol")
            or route_mode.endswith("-queue")
        )
        if not is_submission_evidence:
            continue
        if tool_name and tool_name not in evidence_tools:
            evidence_tools.append(tool_name)
        for source in (metadata, item.get("output_preview") or ""):
            for identifier in _submission_identifiers(source):
                if identifier not in evidence_identifiers:
                    evidence_identifiers.append(identifier)

    base["evidence_identifiers"] = evidence_identifiers
    base["evidence_tools"] = evidence_tools
    if not evidence_tools:
        base.update(
            {
                "valid": False,
                "status": "missing_submission_evidence",
                "reason": "子任务没有成功的 submit_job 或 accepted_async 工具证据。",
            }
        )
        return base

    missing_identifiers = [
        identifier
        for identifier in claimed_identifiers
        if identifier not in evidence_identifiers
    ]
    if missing_identifiers:
        base.update(
            {
                "valid": False,
                "status": "identifier_mismatch",
                "missing_identifiers": missing_identifiers,
                "reason": (
                    "子任务声称的受理 ID 与成功工具证据不一致："
                    + ", ".join(missing_identifiers)
                    + "。"
                ),
            }
        )
        return base

    base.update({"status": "verified", "reason": "异步提交声明已由工具轨迹验证。"})
    return base


def trim_handoff_text(value: Any, max_len: int = 1000) -> str:
    text = str(value or "")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def json_safe_handoff_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return trim_handoff_text(value, 1800)
    if depth >= 4:
        return trim_handoff_text(repr(value), 500)
    if isinstance(value, dict):
        payload: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                payload["__truncated_keys__"] = len(value) - index
                break
            payload[str(key)] = json_safe_handoff_value(item, depth=depth + 1)
        return payload
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        payload = [
            json_safe_handoff_value(item, depth=depth + 1)
            for item in values[:40]
        ]
        if len(values) > 40:
            payload.append(f"...[{len(values) - 40} more items]")
        return payload
    return trim_handoff_text(repr(value), 800)


def compact_subtask_tool_metadata(metadata: Any) -> dict[str, Any]:
    raw = metadata if isinstance(metadata, dict) else {}
    if not raw:
        return {}
    keys = {
        "execution_environment",
        "sandbox",
        "sandbox_executed",
        "sandbox_worker",
        "sandbox_job_id",
        "sandbox_tool",
        "sandbox_tool_name",
        "sandbox_backend",
        "sandbox_image",
        "sandbox_runtime",
        "sandbox_runner_url",
        "sandbox_network",
        "sandbox_profile",
        "sandbox_status",
        "container_id",
        "container_name",
        "container_cwd",
        "worker_input",
        "worker_output",
        "runtime_tool",
        "backend",
        "runtime",
        "network_policy",
        "returncode",
        "exit_code",
        "exitCode",
        "status",
        "accepted_async",
        "identifier",
        "symphony_identifier",
        "local_identifier",
        "local_request_id",
        "request_id",
        "job_id",
        "process_id",
        "run_id",
        "workflow_run_id",
        "route_mode",
        "execution_channel",
        "routed_server",
        "routed_tool",
        "tracker",
        "workdir",
        "permission_denied",
        "denied_tool",
        "policy_source",
        "terminal_tool_error",
        "should_end_turn",
        "termination_scope",
        "output_path",
        "full_result_path",
        "artifact_paths",
        "artifacts",
        "doc_id",
        "record_path",
        "l0_delivery",
        "awaiting_input",
        "waiting_for_user",
        "question_permission_denied",
        "question_payload",
        "path_resolution",
        "pdf_path",
        "sha256",
        "reused",
        "existing",
        "truncated",
        "prefixOmitted",
        "hasMore",
    }
    return {
        key: json_safe_handoff_value(raw[key])
        for key in keys
        if key in raw
    }


def build_subtask_tool_trace(
    messages: list[Any],
    *,
    attempt: int | None = None,
) -> list[dict[str, Any]]:
    """Build a bounded, JSON-safe trace containing outcomes, not just names."""

    trace: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages or []:
        message_id = str(getattr(message, "id", "") or "").strip()
        for tool_call in getattr(message, "tool_calls", None) or []:
            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
            call_id = str(getattr(tool_call, "call_id", "") or "").strip()
            arguments = getattr(tool_call, "arguments", None) or {}
            key = (message_id, call_id, tool_name)
            if not tool_name or key in seen:
                continue
            seen.add(key)
            state = getattr(tool_call, "state", "")
            state_value = getattr(state, "value", state)
            entry: dict[str, Any] = {"tool_name": tool_name}
            if call_id:
                entry["call_id"] = call_id
            if state_value:
                entry["state"] = str(state_value)
            if attempt is not None:
                entry["attempt"] = max(1, int(attempt))
            if isinstance(arguments, dict) and arguments:
                entry["arguments"] = json_safe_handoff_value(arguments, depth=1)
            raw_metadata = getattr(tool_call, "metadata", None) or {}
            metadata = compact_subtask_tool_metadata(raw_metadata)
            if metadata:
                entry["metadata"] = metadata
            output = str(getattr(tool_call, "output", "") or "").strip()
            error = str(getattr(tool_call, "error", "") or "").strip()
            if output:
                entry["output_preview"] = trim_handoff_text(output, 900)
                path = str(arguments.get("filePath") or arguments.get("file_path") or "") if isinstance(arguments, dict) else ""
                if (tool_name == "read" and str(state_value).lower() in _SUCCESS_STATES
                        and not error and path.lower().endswith((".md", ".txt", ".rst"))
                        and len(output) > 900):
                    # Review needs the actual source beyond the short activity
                    # preview. Preserve bounded chunks of this approved read,
                    # never reopen a file that may have changed in the meantime.
                    entry["source_read_excerpts"] = [output[i:i + 1200] for i in range(0, min(len(output), 9600), 1200)]
                    entry["source_read_total_chars"] = len(output)
            if error:
                entry["error_preview"] = trim_handoff_text(error, 900)
            trace.append(entry)

            # Delegating tools expose the child's bounded trace in metadata.
            # Flatten it so evidence survives more than one delegation boundary
            # instead of degrading to a bare `task`/`assign_task` call.
            nested_trace = (
                raw_metadata.get("tool_trace")
                if isinstance(raw_metadata, dict)
                else None
            )
            if not isinstance(nested_trace, list):
                continue
            for nested_raw in nested_trace[:40]:
                if not isinstance(nested_raw, dict):
                    continue
                nested = json_safe_handoff_value(nested_raw)
                if not isinstance(nested, dict):
                    continue
                nested_tool = str(nested.get("tool_name") or "").strip()
                nested_call_id = str(nested.get("call_id") or "").strip()
                nested_key = (message_id, nested_call_id, nested_tool)
                if not nested_tool or nested_key in seen:
                    continue
                seen.add(nested_key)
                if attempt is not None and nested.get("attempt") in (None, ""):
                    nested["attempt"] = max(1, int(attempt))
                trace.append(nested)
    return trace


def _trace_identity(item: dict[str, Any]) -> str:
    attempt = str(item.get("attempt") or "").strip()
    attempt_prefix = f"attempt:{attempt}:" if attempt else ""
    call_id = str(item.get("call_id") or "").strip()
    if call_id:
        return f"{attempt_prefix}call:{call_id}"
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    try:
        args_key = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_key = str(arguments)
    return (
        f"{attempt_prefix}"
        f"tool:{str(item.get('tool_name') or '').strip()}:{args_key}"
    )


def _trace_richness(item: dict[str, Any]) -> tuple[int, int]:
    terminal = int(bool(item.get("output_preview") or item.get("error_preview")))
    return terminal, len(json.dumps(item, ensure_ascii=False, default=str))


def merge_subtask_tool_traces(
    *traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge snapshots across execution/review attempts without losing history."""

    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for trace in traces:
        for raw_item in trace or []:
            if not isinstance(raw_item, dict):
                continue
            item = json_safe_handoff_value(raw_item)
            identity = _trace_identity(item)
            position = positions.get(identity)
            if position is None:
                positions[identity] = len(merged)
                merged.append(item)
                continue
            if _trace_richness(item) > _trace_richness(merged[position]):
                merged[position] = item
    return merged


def report_revision_evidence(tool_trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed local write/read order, never inferred from the model's claims.

    Call IDs identify observed revisions, not filesystem hashes. External edits
    and the semantic correctness of a review still require independent review.
    """
    files: dict[str, dict[str, Any]] = {}
    reads: dict[str, dict[str, str]] = {}
    for item in merge_subtask_tool_traces(tool_trace):
        if item.get('error_preview') or str(item.get('state', '')).lower() not in _SUCCESS_STATES:
            continue
        name = item.get('tool_name')
        args = item.get('arguments') or {}
        raw = str(args.get('filePath') or '')
        if name not in {'read', 'write', 'edit'} or not raw.lower().endswith(('.md', '.txt', '.rst')) or '://' in raw:
            continue
        path = raw.removeprefix('/workspace/').removeprefix('./')
        call_id = str(item.get('call_id') or '')
        if not call_id:
            continue
        state = files.setdefault(path, {'path': path, 'last_write_call_id': '', 'last_read_call_id': ''})
        if name == 'read':
            metadata = item.get('metadata') or {}
            if metadata.get('truncated') or metadata.get('prefixOmitted') or metadata.get('hasMore') or int(args.get('offset') or 1) > 1 or int(args.get('columnOffset') or 0) > 0:
                continue
            state['last_read_call_id'] = call_id
            reads[path] = {'read_call_id': call_id, 'observed_write_call_id': state['last_write_call_id']}
        else:
            state['last_write_call_id'] = call_id
            state['inputs_at_write'] = {key: dict(value) for key, value in reads.items() if key != path}
    written = [value for value in files.values() if value['last_write_call_id']]
    for state in written:
        observed = reads.get(state['path'], {})
        state['read_after_last_write'] = observed.get('observed_write_call_id') == state['last_write_call_id']
        state['inputs_at_write'] = [
            {'path': path, **receipt, 'latest_write_call_id': files[path]['last_write_call_id'],
             'matches_latest_write': receipt['observed_write_call_id'] == files[path]['last_write_call_id']}
            for path, receipt in state.get('inputs_at_write', {}).items() if files[path]['last_write_call_id']
        ][:10]
    unread = [state['path'] for state in written if not state['read_after_last_write']]
    return {'kind': 'report_revision_call_order', 'files': written[-10:],
            'unread_file_count': len(unread), 'unread_files': unread[:10],
            'omitted_files': max(0, len(written) - 10)}


def normalize_subtask_outcome(raw_status: Any) -> tuple[str, bool]:
    status = str(raw_status or "completed").strip().lower() or "completed"
    if status in {"completed", "complete", "pass", "passed", "success", "succeeded"}:
        return "completed", False
    if status == "partial":
        return "partial", False
    if status in {
        "awaiting_input",
        "waiting_for_user",
        "needs_parent_input",
        "needs_user_input",
    }:
        return "waiting_for_user", False
    if status in {"blocked", "revise", "needs_revision"}:
        return "blocked", True
    if status in {"cancelled", "canceled"}:
        return "cancelled", True
    return "failed", True


def completed_subtask_work(
    tool_trace: list[dict[str, Any]],
    *,
    max_items: int = 12,
) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tool_trace or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        state = str(item.get("state") or "").strip().lower()
        output_preview = str(item.get("output_preview") or "").strip()
        error_preview = str(item.get("error_preview") or "").strip()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if bool(metadata.get("awaiting_input") or metadata.get("waiting_for_user")):
            continue
        if not tool_name or error_preview:
            continue
        if state and state not in _SUCCESS_STATES:
            continue
        if not output_preview:
            if metadata:
                output_preview = json.dumps(metadata, ensure_ascii=False, default=str)
            elif state in _SUCCESS_STATES:
                output_preview = "工具调用成功（未提供输出摘要）。"
            else:
                continue
        identity = _trace_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        work: dict[str, Any] = {
            "tool_name": tool_name,
            "call_id": str(item.get("call_id") or "").strip(),
            "output_preview": trim_handoff_text(output_preview, 1000),
        }
        for key in ("attempt", "arguments", "metadata"):
            if item.get(key) not in (None, "", {}, []):
                work[key] = json_safe_handoff_value(item[key])
        completed.append(work)
        if len(completed) >= max(1, max_items):
            break
    return completed


def failed_subtask_work(
    tool_trace: list[dict[str, Any]],
    *,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in reversed(tool_trace or []):
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        state = str(item.get("state") or "").strip().lower()
        error_preview = str(item.get("error_preview") or "").strip()
        if not tool_name or (not error_preview and state not in _FAILED_STATES):
            continue
        identity = _trace_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        failure: dict[str, Any] = {
            "tool_name": tool_name,
            "call_id": str(item.get("call_id") or "").strip(),
            "error_preview": trim_handoff_text(
                error_preview or str(item.get("output_preview") or state),
                1000,
            ),
        }
        for key in ("attempt", "arguments", "metadata"):
            if item.get(key) not in (None, "", {}, []):
                failure[key] = json_safe_handoff_value(item[key])
        failed.append(failure)
        if len(failed) >= max(1, max_items):
            break
    return list(reversed(failed))


def pending_subtask_question(
    tool_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the newest unresolved child question carried by tool metadata."""

    for item in reversed(tool_trace or []):
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if not bool(metadata.get("awaiting_input") or metadata.get("waiting_for_user")):
            continue
        payload = metadata.get("question_payload")
        question = dict(payload) if isinstance(payload, dict) else {}
        if not question:
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            question = dict(arguments)
        question.setdefault("source_tool", str(item.get("tool_name") or "question"))
        question.setdefault("source_call_id", str(item.get("call_id") or ""))
        question["permission_denied"] = bool(metadata.get("question_permission_denied"))
        return json_safe_handoff_value(question)
    return {}


def pending_subtask_question_from_result(result_text: Any) -> dict[str, Any]:
    """Read an explicit ``needs_parent_input`` result contract from child text."""

    text = str(result_text or "").strip()
    if not text:
        return {}
    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(candidate)
    except Exception:
        decoded = None
    if isinstance(decoded, dict):
        status = str(
            decoded.get("outcome_status")
            or decoded.get("status")
            or decoded.get("state")
            or ""
        ).strip().lower()
        if status in {
            "awaiting_input",
            "waiting_for_user",
            "needs_parent_input",
            "needs_user_input",
        }:
            nested = decoded.get("question_payload")
            payload = dict(nested) if isinstance(nested, dict) else {}
            payload.setdefault(
                "question",
                str(decoded.get("question") or decoded.get("prompt") or "").strip(),
            )
            if isinstance(decoded.get("options"), list):
                payload.setdefault("options", list(decoded["options"]))
    if not payload:
        lowered = text.lower()
        markers = (
            "needs_parent_input",
            "needs_user_input",
            "outcome_status: awaiting_input",
            "status: awaiting_input",
            "正在等待用户输入",
        )
        if not any(marker in lowered for marker in markers):
            return {}
        question = ""
        for line in text.splitlines():
            stripped = line.strip().lstrip("-* ")
            lowered_line = stripped.lower()
            for prefix in ("question:", "question：", "问题:", "问题："):
                if lowered_line.startswith(prefix.lower()):
                    question = stripped[len(prefix) :].strip()
                    break
            if question:
                break
        payload = {"question": question}
    if not str(payload.get("question") or "").strip():
        payload["question"] = "子任务需要父 Agent 补充输入后才能继续。"
    payload.setdefault("source", "child_result_contract")
    return json_safe_handoff_value(payload)


def clean_subtask_blocker(value: Any) -> str:
    lines = str(value or "").strip().splitlines()
    while lines and lines[0].strip() in {"当前任务未完成。", "当前任务未完成"}:
        lines.pop(0)
    return "\n".join(lines).strip()


@dataclass
class SubtaskHandoff:
    """Structured result passed across a child-to-parent Harness boundary."""

    outcome_status: str
    child_outcome_status: str
    result_text: str = ""
    blocker: str = ""
    next_action: str = ""
    review_verdict: str = ""
    completed_work: list[dict[str, Any]] = field(default_factory=list)
    failed_work: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    task_id: str = ""
    session_id: str = ""
    agent_name: str = ""
    execution_attempts: int = 0
    review_attempts: int = 0
    question_payload: dict[str, Any] = field(default_factory=dict)
    claim_validation: dict[str, Any] = field(default_factory=dict)

    @property
    def child_terminal_error(self) -> bool:
        return self.child_outcome_status in {"blocked", "failed", "cancelled"}

    @property
    def recovery_required(self) -> bool:
        return self.outcome_status != "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "tcode.subtask-handoff.v1",
            "outcome_status": self.outcome_status,
            "child_outcome_status": self.child_outcome_status,
            "result_text": self.result_text,
            "blocker": self.blocker,
            "next_action": self.next_action,
            "review_verdict": self.review_verdict,
            "completed_work": self.completed_work,
            "failed_work": self.failed_work,
            "tool_trace": self.tool_trace,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "execution_attempts": self.execution_attempts,
            "review_attempts": self.review_attempts,
            "question_payload": self.question_payload,
            "claim_validation": self.claim_validation,
            "parent_can_continue": True,
            "should_end_parent_turn": False,
            "recovery_required": self.recovery_required,
        }

    def render_for_parent(self) -> str:
        if self.outcome_status == "completed":
            return str(self.result_text or "")

        if self.outcome_status == "awaiting_input":
            lines = [
                "子任务正在等待补充输入；这不是执行失败。",
                "- handoff_outcome_status: awaiting_input",
                f"- child_outcome_status: {self.child_outcome_status}",
            ]
            if self.task_id:
                lines.append(f"- task_id: {self.task_id}")
            if self.session_id and self.session_id != self.task_id:
                lines.append(f"- child_session_id: {self.session_id}")
            question = str(self.question_payload.get("question") or "").strip()
            if question:
                lines.extend(["", "## 待回答问题", question])
            options = self.question_payload.get("options")
            if isinstance(options, list) and options:
                lines.extend(["", "## 可选答案"])
                lines.extend(f"- {str(item)}" for item in options)
            if self.completed_work:
                lines.extend(["", "## 已完成工作"])
                for index, item in enumerate(self.completed_work, 1):
                    lines.append(f"### {index}. {item.get('tool_name', 'unknown_tool')}")
                    lines.append(str(item.get("output_preview") or "已完成。"))
            lines.extend(
                [
                    "",
                    "## 接续要求",
                    "请父 Agent 先复用已有上下文；仍无法回答时通过前端询问用户，随后恢复原任务。不要重复已经成功且有副作用的步骤。",
                ]
            )
            return "\n".join(lines)

        lines = [
            "子任务未完全完成；主 Agent 可依据以下结构化交接继续处理。",
            f"- handoff_outcome_status: {self.outcome_status}",
            f"- child_outcome_status: {self.child_outcome_status}",
        ]
        if self.review_verdict:
            lines.append(f"- review_verdict: {self.review_verdict}")
        if self.execution_attempts or self.review_attempts:
            lines.append(
                "- attempts: "
                f"execution={self.execution_attempts}, review={self.review_attempts}"
            )

        if self.completed_work:
            lines.extend(["", "## 已完成工作"])
            for index, item in enumerate(self.completed_work, 1):
                lines.append(f"### {index}. {item.get('tool_name', 'unknown_tool')}")
                lines.append(
                    str(item.get("output_preview") or "已完成，但未提供输出摘要。")
                )

        if self.failed_work:
            lines.extend(["", "## 未完成或失败步骤"])
            for index, item in enumerate(self.failed_work, 1):
                lines.append(f"### {index}. {item.get('tool_name', 'unknown_tool')}")
                lines.append(
                    str(item.get("error_preview") or "失败，但未提供错误摘要。")
                )

        lines.extend(["", "## 阻塞位置"])
        lines.append(self.blocker or "子任务未提供具体阻塞原因。")
        if self.next_action:
            lines.extend(["", "## 建议下一步", self.next_action])
        lines.extend(
            [
                "",
                "## 接续要求",
                "请主 Agent 根据原始用户目标直接汇总已有结果、继续未完成步骤、恢复该子任务或改派其他 Agent；不要把子任务局部失败直接当作用户任务失败，也不要重复已经成功且有副作用的步骤。",
            ]
        )
        return "\n".join(lines)


class SubtaskHandoffError(RuntimeError):
    """Execution error that carries the progress ledger across call layers."""

    def __init__(self, message: str, handoff: SubtaskHandoff) -> None:
        super().__init__(message)
        self.handoff = handoff


def build_subtask_handoff(
    *,
    result_text: Any,
    tool_trace: list[dict[str, Any]] | None,
    child_outcome_status: Any,
    blocker: Any = "",
    next_action: Any = "",
    review_verdict: Any = "",
    task_id: Any = "",
    session_id: Any = "",
    agent_name: Any = "",
    execution_attempts: int = 0,
    review_attempts: int = 0,
    request_text: Any = "",
) -> SubtaskHandoff:
    trace = merge_subtask_tool_traces(list(tool_trace or []))
    result = str(result_text or "").strip()
    claim_validation = validate_async_submission_claim(
        request_text=request_text,
        result_text=result,
        tool_trace=trace,
    )
    if claim_validation.get("required") and not claim_validation.get("valid"):
        result = (
            "子任务的异步提交声明未通过工具证据校验。\n"
            f"{claim_validation.get('reason') or '缺少可验证的提交证据。'}"
            "\n未验证的受理 ID 与成功描述已从父 Agent 可见结果中隔离，"
            "不得据此报告任务状态。"
        )
        child_outcome_status = "failed"
        if not str(blocker or "").strip():
            blocker = str(claim_validation.get("reason") or result).strip()
        if not str(next_action or "").strip():
            next_action = (
                "重新调用异步提交工具，并仅返回工具输出中的真实受理 ID。"
            )
    reported_child_status, _ = normalize_subtask_outcome(child_outcome_status)
    result_question_payload = pending_subtask_question_from_result(result)
    # A successfully resumed child keeps its previous tool history.  Do not
    # resurrect an old waiting checkpoint after the new turn has completed.
    question_payload = (
        result_question_payload
        or (
            {}
            if reported_child_status == "completed"
            else pending_subtask_question(trace)
        )
    )
    normalized_child_status, _ = normalize_subtask_outcome(
        "waiting_for_user" if question_payload else reported_child_status
    )
    completed = completed_subtask_work(trace)
    failed = failed_subtask_work(trace)
    if question_payload:
        outcome_status = "awaiting_input"
    elif normalized_child_status == "completed":
        outcome_status = "completed"
    elif normalized_child_status == "partial" or completed:
        outcome_status = "partial"
    else:
        outcome_status = normalized_child_status

    blocker_text = clean_subtask_blocker(blocker)
    if outcome_status == "awaiting_input":
        blocker_text = ""
    elif outcome_status != "completed" and not blocker_text:
        blocker_text = clean_subtask_blocker(result)
    return SubtaskHandoff(
        outcome_status=outcome_status,
        child_outcome_status=normalized_child_status,
        result_text=result,
        blocker=blocker_text,
        next_action=str(next_action or "").strip(),
        review_verdict=str(review_verdict or "").strip(),
        completed_work=completed,
        failed_work=failed,
        tool_trace=trace,
        task_id=str(task_id or "").strip(),
        session_id=str(session_id or "").strip(),
        agent_name=str(agent_name or "").strip(),
        execution_attempts=max(0, int(execution_attempts or 0)),
        review_attempts=max(0, int(review_attempts or 0)),
        question_payload=question_payload,
        claim_validation=claim_validation,
    )
