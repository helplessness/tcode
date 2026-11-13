"""把会话运行时对象整理成稳定的 Web 协议。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from ..message_context import is_real_user_message
from ..protocol import SessionSummary
from ..protocol.v2 import runtime_message_from_legacy
from ..runtime import TaskRuntimeStore
from ..runtime.async_completion import unread_completion_count
from ..session.manager import Session, SessionManager
from ..session.transcript import without_legacy_native_agui_user_echoes
from ..session.usage import session_own_usage


_PUBLIC_PROCESS_TOOLS = {
    "read", "write", "edit", "grep", "glob", "bash", "exec", "computer",
    "browser", "websearch", "web_search", "webfetch", "web_fetch",
    "todo_read", "todoread", "todo_write", "todowrite", "task", "assign_task",
    "list_workers", "paper_literature_search", "paper_library_lookup_pdf",
    "paper_library_ingest_pdf", "paper_library_get_record", "import_resource",
}
TRUSTED_PUBLIC_COMMENTARY = object()


def public_process_note(note: Any) -> dict[str, Any]:
    """Project a structured process note to its safe public representation."""
    raw = dict(note or {}) if isinstance(note, dict) else {}
    phase = str(raw.get("phase") or "").strip().lower()
    status = str(raw.get("status") or "").strip().lower()
    raw_tool = str(raw.get("tool_name") or "").strip()
    tool = raw_tool if raw_tool.lower() in _PUBLIC_PROCESS_TOOLS else "工具"

    trusted_commentary = raw.get("_trusted_public_commentary") is TRUSTED_PUBLIC_COMMENTARY
    if phase == "public_commentary" and trusted_commentary:
        message = str(raw.get("message") or "").strip()
    elif phase == "turn_start":
        message = "我先对齐这次请求和当前会话上下文。"
    elif phase == "preparing":
        message = "我在检查可用能力和已有上下文。"
    elif phase == "context_check":
        message = "我在校准上下文，准备继续下一步。"
    elif phase == "tool_preparation":
        message = "我在整理可用工具，准备选择合适入口。"
    elif phase == "model_wait":
        message = "工具已经就绪，我在判断下一步行动。"
    elif phase == "tool_selection":
        message = "下一步需要调用工具补充可核对的信息。"
    elif phase == "final_response":
        message = "现有信息已经够用，我开始整理最终回复。"
    elif phase == "tool_start":
        message = f"我正在调用 {tool}，完成当前阶段。"
    elif phase == "tool_progress":
        message = f"{tool} 仍在运行，我会继续同步状态。"
    elif phase == "tool_result":
        if status in {"error", "failed"}:
            message = f"{tool} 返回错误，我会保留失败状态并调整下一步。"
        elif status in {"cancelled", "canceled", "interrupted"}:
            message = f"{tool} 已中断，没有把这一步记为完成。"
        else:
            message = f"{tool} 已返回；我会先核对结果，再决定下一步。"
    elif "approval" in phase or "approval" in status:
        message = "当前操作正在等待用户审批。"
    elif status in {"cancelled", "canceled", "interrupted"}:
        message = "当前步骤已中断。"
    elif status in {"error", "failed"}:
        message = "当前步骤失败，我会保留失败状态并调整下一步。"
    else:
        message = "任务状态已有更新。"

    safe_call_id = ""
    raw_call_id = str(raw.get("call_id") or "").strip()
    if raw_call_id:
        safe_call_id = "call-" + hashlib.sha256(raw_call_id.encode()).hexdigest()[:16]
    identity = json.dumps(
        [phase, status, tool, message, safe_call_id, raw.get("seq")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    public = {
        key: raw.get(key)
        for key in ("phase", "status", "elapsed_seconds", "current", "total", "seq", "timestamp")
        if raw.get(key) not in (None, "")
    }
    public["id"] = "process-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    if safe_call_id:
        public["call_id"] = safe_call_id
    public["phase"] = phase or None
    public["status"] = status or None
    public["message"] = message
    public["tool_name"] = None if trusted_commentary or not raw_tool else tool
    return public


def serialize_runtime_messages(session: Session) -> list[dict[str, Any]]:
    """Build a v2 snapshot, skipping legacy messages the protocol cannot decode."""
    messages = []
    for message in list(getattr(session, "messages", []) or []):
        try:
            payload = runtime_message_from_legacy(message).model_dump(mode="json", by_alias=True)
            if payload.get("process_notes"):
                payload["process_notes"] = [public_process_note(note) for note in payload["process_notes"]]
            messages.append(payload)
        except (TypeError, ValueError):
            continue
    return messages


def serialize_session_summary(
    session: Session,
    *,
    default_workdir: str,
    session_context: Any,
    workdir_name: Callable[[str, Any], str],
) -> dict[str, Any]:
    archived_at = SessionManager.archived_at(session)
    visible_messages = without_legacy_native_agui_user_echoes(
        SessionManager.visible_messages(session),
        session_metadata=getattr(session, "metadata", {}),
    )
    message_preview = ""
    if visible_messages:
        first_message = visible_messages[0]
        message_preview = str(getattr(first_message, "content", "") or "").strip()
    if not message_preview:
        message_preview = str(getattr(session, "title", "") or "").strip()
    message_preview = message_preview[:240]

    session_workdir = str(getattr(session, "workdir", "") or "")
    task_runtime = TaskRuntimeStore.summary_for_session(
        session.id,
        workdir=session_workdir or default_workdir,
        item_limit=0,
        event_limit=0,
        session_metadata=dict(getattr(session, "metadata", {}) or {}),
    )
    payload = {
        "session_id": session.id,
        "parent_id": session.parent_id,
        "title": session.title,
        "agent_name": session.agent_name,
        "status": session.status,
        "tenant_id": str(getattr(session, "tenant_id", "") or ""),
        "user_id": str(getattr(session, "user_id", "") or ""),
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
        "workdir": session_workdir,
        "workdir_name": workdir_name(session_workdir, session_context),
        "created_at": session.created_at,
        "updated_at": session.last_active_at,
        "message_count": len(visible_messages),
        "message_preview": message_preview,
        "async_completion_unread_count": unread_completion_count(session),
        "usage": session_own_usage(session),
        "task_runtime": {
            "session_id": task_runtime["session_id"],
            "session_outcome_status": task_runtime["session_outcome_status"],
            "session_outcome_updated_at": task_runtime["session_outcome_updated_at"],
            "active_total": task_runtime["active_total"],
            "counts": task_runtime["counts"],
            "active_counts": task_runtime["active_counts"],
            "resolved_counts": task_runtime["resolved_counts"],
            "updated_at": task_runtime["updated_at"],
        },
        "is_archived": archived_at is not None,
        "archived_at": archived_at,
        "mode": str((getattr(session, "metadata", {}) or {}).get("mode") or "general"),
        "research_id": str((getattr(session, "metadata", {}) or {}).get("research_id") or ""),
        "mode_source": str((getattr(session, "metadata", {}) or {}).get("mode_source") or "default"),
        "archive_cause": str((getattr(session, "metadata", {}) or {}).get("archive_cause") or ""),
        "archive_locked": bool((getattr(session, "metadata", {}) or {}).get("archive_locked")),
    }
    # exclude_unset 保留原接口字段数量，同时让模型负责字段名和类型校验。
    return SessionSummary.model_validate(payload).model_dump(
        mode="json",
        exclude_unset=True,
    )


def _serialize_frontend_tool_call(tool_call: Any) -> dict[str, Any]:
    state = getattr(tool_call, "state", None)
    state_value = getattr(state, "value", state)
    output = getattr(tool_call, "output", None)
    error = getattr(tool_call, "error", None)
    metadata = getattr(tool_call, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
        "arguments": getattr(tool_call, "arguments", {}) or {},
        "call_id": str(getattr(tool_call, "call_id", "") or ""),
        "state": str(state_value or ""),
        "output": None if output is None else str(output),
        "error": None if error is None else str(error),
        "start_time": getattr(tool_call, "start_time", None),
        "metadata": dict(metadata or {}),
    }


def _public_frontend_message_metadata(message: Any) -> dict[str, Any]:
    """Return only guidance metadata that is safe and useful to the client."""

    raw = getattr(message, "metadata", None)
    if not isinstance(raw, dict):
        return {}
    public: dict[str, Any] = {}
    guidance = raw.get("guidance")
    if isinstance(guidance, dict):
        allowed = {
            key: guidance.get(key)
            for key in (
                "id",
                "sequence",
                "mode",
                "target_turn_id",
                "applied_safe_point",
                "contract_revision_id",
            )
            if guidance.get(key) not in (None, "")
        }
        if allowed:
            public["guidance"] = allowed
    if bool(raw.get("superseded_by_guidance")):
        public["superseded_by_guidance"] = True
    stream_epoch = raw.get("stream_epoch")
    if isinstance(stream_epoch, int) and stream_epoch >= 0:
        public["stream_epoch"] = stream_epoch
    completed_at = raw.get("completed_at")
    if isinstance(completed_at, (int, float)) and completed_at > 0:
        public["completed_at"] = completed_at
    source_runtime_id = str(raw.get("source_runtime_id") or "").strip()
    if source_runtime_id:
        public["source_runtime_id"] = source_runtime_id[:200]
    return public


def _subagent_integration_message(
    display_name: str,
    agent_name: str,
    user_message: str,
    response: str,
    tools_used: list[str],
    context: dict[str, Any],
) -> str:
    tools_text = ", ".join(tools_used) if tools_used else "none"
    return (
        "[Subagent Feedback Integrated]\n"
        f"subagent: {display_name} ({agent_name})\n"
        f"instruction: {user_message}\n"
        f"tools_used: {tools_text}\n"
        f"context: messages={context.get('message_count', 0)}, tools={context.get('tool_call_count', 0)}\n"
        "result:\n"
        f"{response}"
    )


def _extract_subagent_display_name(session) -> str:
    metadata = getattr(session, "metadata", {}) or {}
    if isinstance(metadata, dict):
        explicit = str(metadata.get("display_name", "") or "").strip()
        if explicit:
            return explicit
    title = str(getattr(session, "title", "") or "")
    if "·" in title:
        maybe = title.split("·", 1)[0].strip()
        if maybe:
            return maybe
    return str(getattr(session, "agent_name", "subagent") or "subagent")


def _extract_subagent_details(session) -> dict[str, Any]:
    tools_used: list[str] = []
    async_tool_refs: list[dict[str, Any]] = []
    artifact_refs: dict[str, Any] = {}
    artifact_paths: list[str] = []
    seen_tools: set[str] = set()
    seen_async_refs: set[str] = set()
    conversation_with: set[str] = set()
    last_response = ""

    def _add_artifact_path(value: Any) -> None:
        """Collect bounded, explicit tool artifact paths without guessing from prose."""

        text = str(value or "").strip()
        if text and text not in artifact_paths and len(artifact_paths) < 32:
            artifact_paths.append(text)

    def _collect_artifacts(metadata: dict[str, Any]) -> None:
        raw_refs = metadata.get("artifact_refs")
        if isinstance(raw_refs, dict):
            for raw_name, raw_value in raw_refs.items():
                name = str(raw_name or "").strip()
                if not name or len(artifact_refs) >= 32:
                    continue
                artifact_refs[name] = raw_value
                if isinstance(raw_value, dict):
                    _add_artifact_path(
                        raw_value.get("path")
                        or raw_value.get("artifact_path")
                        or raw_value.get("output_path")
                    )
                elif isinstance(raw_value, str):
                    _add_artifact_path(raw_value)

        raw_artifacts = metadata.get("artifacts")
        if isinstance(raw_artifacts, dict):
            raw_artifacts = [raw_artifacts]
        if isinstance(raw_artifacts, (list, tuple)):
            for index, item in enumerate(raw_artifacts, start=1):
                if isinstance(item, dict):
                    path = str(
                        item.get("path")
                        or item.get("artifact_path")
                        or item.get("output_path")
                        or ""
                    ).strip()
                    name = str(item.get("name") or f"artifact_{index}").strip()
                    if name and path and len(artifact_refs) < 32:
                        artifact_refs.setdefault(name, path)
                    _add_artifact_path(path)
                elif isinstance(item, str):
                    _add_artifact_path(item)

    for msg in getattr(session, "messages", None) or []:
        role = str(getattr(msg, "role", "") or "")
        content = str(getattr(msg, "content", "") or "")

        if role == "user":
            lowered = content.lower()
            if "peer outputs from other agents" in lowered:
                conversation_with.add("peer_agents")
            elif "original task:" in lowered:
                conversation_with.add("parent_agent")
            else:
                conversation_with.add("human")
        elif role == "assistant" and content.strip():
            last_response = content

        for tc in getattr(msg, "tool_calls", None) or []:
            name = str(getattr(tc, "tool_name", "") or "").strip()
            if not name:
                continue
            if name not in seen_tools:
                seen_tools.add(name)
                tools_used.append(name)
            metadata = dict(getattr(tc, "metadata", {}) or {})
            _collect_artifacts(metadata)
            output = str(getattr(tc, "output", "") or "")
            is_async_ref = bool(metadata.get("accepted_async")) or bool(
                metadata.get("human_review_required")
            )
            if not is_async_ref and re.search(r"\bAccepted async\b", output, re.IGNORECASE):
                is_async_ref = True
            if is_async_ref:
                ref_key = "|".join(
                    [
                        name,
                        str(metadata.get("identifier") or metadata.get("issue_identifier") or ""),
                        str(metadata.get("job_id") or ""),
                        output[:160],
                    ]
                )
                if ref_key in seen_async_refs:
                    continue
                seen_async_refs.add(ref_key)
                async_tool_refs.append(
                    {
                        "tool_name": name,
                        "output": output[:1200],
                        "metadata": metadata,
                        "identifier": str(
                            metadata.get("identifier")
                            or metadata.get("issue_identifier")
                            or metadata.get("symphony_identifier")
                            or ""
                        ).strip(),
                        "issue_id": str(
                            metadata.get("id")
                            or metadata.get("issue_id")
                            or metadata.get("proxy_issue_id")
                            or ""
                        ).strip(),
                        "job_id": str(metadata.get("job_id") or "").strip(),
                        "execution_channel": str(metadata.get("execution_channel") or "").strip(),
                        "route_mode": str(metadata.get("route_mode") or "").strip(),
                        "routed_server": str(metadata.get("routed_server") or "").strip(),
                        "routed_tool": str(metadata.get("routed_tool") or "").strip(),
                        "human_review_required": bool(metadata.get("human_review_required")),
                    }
                )

    if len(last_response) > 1200:
        last_response = last_response[:1200] + "..."

    context = {
        "message_count": len(getattr(session, "messages", None) or []),
        "tool_call_count": len(tools_used),
        "last_user": "",
        "last_assistant": last_response,
    }
    for msg in reversed(getattr(session, "messages", None) or []):
        if is_real_user_message(msg):
            context["last_user"] = str(getattr(msg, "content", "") or "")[:240]
            break

    metadata = getattr(session, "metadata", {}) or {}
    contract = metadata.get("contract", {}) if isinstance(metadata, dict) else {}

    return {
        "display_name": _extract_subagent_display_name(session),
        "tools_used": tools_used,
        "async_tool_refs": async_tool_refs,
        "artifact_refs": artifact_refs,
        "artifact_paths": artifact_paths,
        "conversation_with": sorted(conversation_with),
        "last_response": last_response,
        "context": context,
        "contract": {
            "goal": str(contract.get("goal", "") or ""),
            "deliverable": str(contract.get("deliverable", "") or ""),
            "stop_when": str(contract.get("stop_when", "") or ""),
            "allowed_tools": contract.get("allowed_tools", "all"),
        },
    }
