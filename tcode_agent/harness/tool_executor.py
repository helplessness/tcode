"""工具执行器。

这层负责把一次 ToolCall 落到真正的执行路径上，
包含权限、上下文组装、内建工具与 MCP 工具桥接，
以及工具执行结果的自动结构化记忆采集。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from ..mcp.agent_integration import MCPTool
from ..mcp.manager_auto import MCPManager
from ..runtime import RuntimeArtifactManager
from ..runtime.output_budget import bound_text, project_tool_output
from ..runtime.tool_arguments import validate_tool_arguments
from ..runtime.task_runtime import (
    TaskRuntimeStore,
    compact_runtime_text,
    task_runtime_id_for_tool_call,
    task_runtime_status_for_tool_result,
)
from ..runtime.sandbox import load_sandbox_config
from ..runtime.tool_pipeline import (
    ToolExecutionPipeline,
    ToolPipelineStage,
    advance_tool_pipeline_stage,
)
from ..runtime.tool_progress import coerce_tool_progress_event
from ..tools.base import ToolContext
from ..tools.capabilities import get_tool_capability
from ..tools.registry import ToolRegistry
from ..types import ToolCall, ToolResult, ToolState
from .policy import ToolPolicyDecision, ToolPolicyEngine


_TOOLS_WITH_NATIVE_SANDBOX_HANDLING = {
    "bash",
    "exec",
    "import_resource",
    "process",
    "browser",
    "skill_run",
}

# 这些工具只把既有文件路径作为导航信息；如果误判为新产物，一次读取会被
# 放大为多条研究事件，并无意义地重建 Topic、Activity 和 Episode 投影。
_RESEARCH_READ_ONLY_ARTIFACT_TOOLS = {
    "read",
    "inspect_file",
    "glob",
    "grep",
    "session_search",
    "result_item_read",
    "memory_read",
    "evidence_search",
    "evidence_graph",
    "paper_library_lookup_pdf",
    "paper_library_query",
    "paper_library_stats",
    "paper_library_count",
    "paper_library_get_record",
}

# 只有这些受信任文档工具可以把语义身份提升到研究日志；普通工具标题仍然
# 只是实现细节。
_RESEARCH_DOCUMENT_METADATA_TOOLS = {
    "paper_library_ingest_pdf",
    "paper_library_lookup_pdf",
    "paper_library_get_record",
}

_PLUGIN_DELEGATED_METADATA_KEYS = {
    "artifact",
    "artifacts",
    "artifact_ref",
    "artifact_refs",
    "content_type",
    "count",
    "encoding",
    "mime_type",
    "partial",
    "retryable",
    "schema_version",
    "status",
    "truncated",
    "warning",
    "warnings",
}
_PLUGIN_DELEGATED_FORBIDDEN_KEY_PARTS = {
    "authorization",
    "command",
    "container",
    "cookie",
    "credential",
    "env",
    "host",
    "password",
    "path",
    "secret",
    "token",
    "url",
}


def public_tool_error(tool_name: str, error: str, metadata: Any = None) -> str:
    """Convert internal diagnostics into a stable, user-facing error."""

    details = dict(metadata or {}) if isinstance(metadata, dict) else {}
    if user_error := str(details.get("user_error") or "").strip():
        return user_error[:2000]
    raw = str(error or "").strip()
    lowered = raw.lower()
    if "repository not found" in lowered or (
        "github" in lowered and "http 404" in lowered
    ):
        return "找不到远程代码仓库。请检查仓库地址是否正确，以及当前账号是否有访问权限。"
    if "remote branch" in lowered or "couldn't find remote ref" in lowered:
        return "远程仓库中不存在指定的分支或标签，请检查地址中的版本信息。"
    if "permission denied" in lowered or "access denied" in lowered:
        return "工具没有完成操作所需的权限，请检查文件、账号或执行权限。"
    if "timed out" in lowered or "timeout" in lowered:
        return "工具执行超时。请稍后重试，或缩小本次操作范围。"
    if "connection refused" in lowered or "could not resolve host" in lowered:
        return "无法连接到目标服务。请检查网络、服务地址或服务状态。"
    internal = (
        "traceback (most recent call last)" in lowered
        or "cloning into '" in lowered
        or bool(re.search(r"/(?:private/)?var/folders/|/tmp/", raw))
    )
    if internal:
        label = str(tool_name or "工具").strip() or "工具"
        return f"{label} 执行失败。详细诊断已记录在本次任务日志中，请检查输入或运行环境后重试。"
    return raw[:2000]


@dataclass(frozen=True)
class ToolInterruptResult:
    """Result of a tool-specific interruption request."""

    outcome: str
    confirmed_stopped: bool = False
    side_effect_state: str = "possible"
    detail: str = ""


def _plugin_delegated_value(value: Any, *, depth: int = 0) -> Any:
    """把委派工具元数据压缩为不含宿主路径和凭据的 JSON 视图。"""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if depth >= 5:
        return "<depth-limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key or "")[:128]
            lowered = key.lower()
            if any(part in lowered for part in _PLUGIN_DELEGATED_FORBIDDEN_KEY_PARTS):
                continue
            result[key] = _plugin_delegated_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _plugin_delegated_value(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    return str(value)[:1000]


def _plugin_delegated_metadata(metadata: Any) -> dict[str, Any]:
    """只向未知插件返回状态和 Artifact 类的最小工具元数据。"""

    if not isinstance(metadata, dict):
        return {}
    safe: dict[str, Any] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key or "")[:128]
        lowered = key.lower()
        if lowered not in _PLUGIN_DELEGATED_METADATA_KEYS and "artifact" not in lowered:
            continue
        if any(part in lowered for part in _PLUGIN_DELEGATED_FORBIDDEN_KEY_PARTS):
            continue
        safe[key] = _plugin_delegated_value(value)
    return safe


def _plugin_delegated_error(error: Any, *, workdir: str) -> str | None:
    """把委派错误压缩并删除工作区、用户目录、Runner 地址和 Token。"""

    if error is None:
        return None
    message = str(error)
    replacements = {
        str(workdir or "").strip(): "<workspace>",
        str(Path.home()): "<home>",
        str(os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or "").strip(): "<runner>",
        str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN", "") or "").strip(): "<redacted>",
    }
    for value, replacement in replacements.items():
        if len(value) > 1:
            message = message.replace(value, replacement)
    return message[:4000]


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _inner_timeout_message(exc: BaseException) -> str:
    text = str(exc or "").strip()
    lowered = text.lower()
    if text and (
        "llm request timed out" in lowered
        or "subagent turn timed out" in lowered
        or "provider request timed out" in lowered
    ):
        return text
    return ""


def _text_value(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _compact_metadata(value: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if item is None:
            continue
        if isinstance(item, str) and not item.strip():
            continue
        compacted[key] = item
    return compacted


def _merge_resource_items(
    current: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in [*(current or []), *(incoming or [])]:
        if not isinstance(item, dict):
            continue
        key = ""
        for field in key_fields:
            key = str(item.get(field) or "").strip()
            if key:
                break
        if not key:
            continue
        deduped[key] = dict(item)
    return list(deduped.values())


def _merge_resource_context_patch(
    existing: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        return dict(existing or {})

    merged = dict(existing or {})
    if isinstance(patch.get("uploaded_files"), list):
        merged["uploaded_files"] = _merge_resource_items(
            list(merged.get("uploaded_files") or []),
            list(patch.get("uploaded_files") or []),
            key_fields=("container_path", "path", "source_path"),
        )
    if isinstance(patch.get("external_mounts"), list):
        merged["external_mounts"] = _merge_resource_items(
            list(merged.get("external_mounts") or []),
            list(patch.get("external_mounts") or []),
            key_fields=("container_path", "source_path"),
        )

    for key, value in patch.items():
        if key in {"uploaded_files", "external_mounts"}:
            continue
        merged[key] = value
    return merged


def _auto_memory_enabled() -> bool:
    raw = str(os.environ.get("TCODE_AUTO_MEMORY_ENABLED", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _auto_memory_min_confidence() -> float:
    try:
        return max(0.0, min(float(os.environ.get("TCODE_AUTO_MEMORY_MIN_CONFIDENCE", "0.5") or "0.5"), 1.0))
    except Exception:
        return 0.5


class AgentToolExecutor:
    """给 Agent 提供一个更清晰的工具执行边界。"""

    def __init__(self, agent: Any):
        """绑定 Agent 与唯一生产工具管线。

        参数:
            agent: 提供上下文、权限、Hook 和工具注册表的 Agent。
        """

        self.agent = agent
        self._pipeline = ToolExecutionPipeline(agent)
        self._active_calls: dict[str, asyncio.Task[ToolResult]] = {}

    def _builtin_tool_timeout_seconds(self, tool_name: str) -> float:
        """Return a hard wall-clock limit for one builtin tool call.

        Subagent tools get a larger budget because they emit their own progress
        events, while ordinary builtins should fail before the websocket's idle
        watchdog turns the whole turn into an opaque stream-stalled error.
        A value <= 0 disables the hard limit.
        """

        normalized = str(tool_name or "").strip().lower()
        if normalized in {"task", "assign_task"}:
            env_name = "TCODE_SUBAGENT_TOOL_TIMEOUT_SECONDS"
            default = 1800.0
        elif normalized == "workflow_run":
            # One call may execute several model-driven stages. Keep its
            # bounded budget separate from short ordinary builtin calls.
            env_name = "TCODE_WORKFLOW_TOOL_TIMEOUT_SECONDS"
            default = 1800.0
        elif normalized == "paper_library_ingest_pdf":
            env_name = "TCODE_PAPER_LIBRARY_INGEST_TIMEOUT_SECONDS"
            default = 3600.0
        elif normalized == "read":
            env_name = "TCODE_READ_TIMEOUT_SECONDS"
            default = 30.0
        elif normalized in {
            "computer", "computer_discover", "computer_observe",
            "computer_element_action", "computer_pointer_action",
            "computer_input_action", "computer_control",
            "computer_type_text", "computer_press_key", "computer_scroll",
        }:
            # Broker approvals expire at ten minutes and own cancellation.
            # Keep the outer tool wall-clock beyond the broker/client deadline
            # so normal expiry can return a terminal response instead of
            # abandoning a still-live Host approval.
            env_name = "TCODE_COMPUTER_TOOL_TIMEOUT_SECONDS"
            default = 660.0
        else:
            env_name = "TCODE_BUILTIN_TOOL_TIMEOUT_SECONDS"
            default = 540.0
        try:
            raw = float(os.environ.get(env_name, str(default)))
        except Exception:
            raw = default
        if raw <= 0:
            return 0.0
        return max(1.0, min(raw, 24 * 3600.0))

    @staticmethod
    def _serialize_frontend_tool_call(tool_call: ToolCall) -> dict[str, Any]:
        state_obj = getattr(tool_call, "state", None)
        state = getattr(state_obj, "value", state_obj)
        return {
            "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
            "arguments": dict(getattr(tool_call, "arguments", {}) or {}),
            "call_id": str(getattr(tool_call, "call_id", "") or ""),
            "state": str(state or "").strip().lower(),
            "output": getattr(tool_call, "output", None),
            "error": getattr(tool_call, "error", None),
            "metadata": dict(getattr(tool_call, "metadata", {}) or {}),
        }

    @staticmethod
    def _policy_metadata(decision: ToolPolicyDecision) -> dict[str, Any]:
        metadata = {
            "action": decision.action,
            "source": decision.source,
        }
        if decision.reason:
            metadata["reason"] = decision.reason
        for key in (
            "capability",
            "execution_profile",
            "sandbox_mode",
            "sandbox_policy_blocked",
            "goal_review_id",
        ):
            if key in decision.metadata:
                metadata[key] = decision.metadata[key]
        return metadata

    def _set_tool_policy_metadata(
        self,
        tool_call: ToolCall,
        decision: ToolPolicyDecision,
    ) -> dict[str, Any]:
        policy_metadata = self._policy_metadata(decision)
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        metadata["policy"] = policy_metadata
        tool_call.metadata = metadata
        return policy_metadata

    def _task_runtime_workdir(self, context: ToolContext | None = None) -> str:
        extra = getattr(context, "extra", None) if context is not None else None
        return str(
            (extra or {}).get("control_workdir", "")
            or (extra or {}).get("workdir", "")
            if isinstance(extra, dict)
            else ""
        ).strip() or str(getattr(self.agent, "workdir", "") or "").strip()

    def _research_trace_payload(self, context: ToolContext) -> dict[str, str]:
        extra = dict(context.extra or {}) if isinstance(context.extra, dict) else {}
        session = getattr(self.agent, "_existing_session", None)
        turn_id = str(
            extra.get("turn_id")
            or getattr(self.agent, "_active_research_turn_id", "")
            or ""
        )
        trace_id = str(
            extra.get("trace_id")
            or getattr(self.agent, "_active_research_trace_id", "")
            or ""
        )
        if turn_id and not trace_id:
            try:
                from ..research import trace_id_for_turn

                trace_id = trace_id_for_turn(
                    str(getattr(self.agent, "session_id", "") or ""),
                    turn_id,
                )
            except Exception:
                trace_id = ""
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        return {
            "tenant_id": str(
                extra.get("tenant_id")
                or getattr(session, "tenant_id", "")
                or ""
            ),
            "user_id": str(
                extra.get("user_id")
                or getattr(session, "user_id", "")
                or ""
            ),
            "workspace_id": str(
                extra.get("workspace_id")
                or getattr(session, "workspace_id", "")
                or ""
            ),
            "session_id": str(getattr(self.agent, "session_id", "") or ""),
            "turn_id": turn_id,
            "trace_id": trace_id,
            "research_id": str(session_metadata.get("active_research_id") or ""),
            "candidate_id": str(session_metadata.get("active_candidate_id") or ""),
        }

    def _append_research_tool_event(
        self,
        *,
        tool_call: ToolCall,
        context: ToolContext,
        event_type: str,
        source: str,
        result: ToolResult | None = None,
        mcp_server: str = "",
        mcp_tool_name: str = "",
    ) -> None:
        try:
            from ..research import ResearchEventInput, ResearchEventSink

            trace = self._research_trace_payload(context)
            result_metadata = dict(getattr(result, "metadata", {}) or {}) if result else {}
            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip().lower()
            selected_metadata = {
                key: result_metadata.get(key)
                for key in (
                    "identifier",
                    "job_id",
                    "process_id",
                    "run_id",
                    "workflow_run_id",
                    "accepted_async",
                    "human_review_required",
                    "output_path",
                    "truncated",
                    "execution_environment",
                    "policy",
                )
                if result_metadata.get(key) not in (None, "", {}, [])
            }
            paper_title = ""
            if tool_name in _RESEARCH_DOCUMENT_METADATA_TOOLS:
                paper_title = str(
                    result_metadata.get("paper_title")
                    or result_metadata.get("document_title")
                    or result_metadata.get("title")
                    or ""
                ).strip()[:300]
                for key in ("doc_id", "doi", "pmid"):
                    value = result_metadata.get(key)
                    if value not in (None, "", {}, []):
                        selected_metadata[key] = value
                if paper_title:
                    selected_metadata["title"] = paper_title
            state_obj = getattr(tool_call, "state", None)
            payload = {
                "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
                "call_id": str(getattr(tool_call, "call_id", "") or ""),
                "state": str(getattr(state_obj, "value", state_obj) or ""),
                "source": source,
                "mcp_server": mcp_server,
                "mcp_tool_name": mcp_tool_name,
                "arguments": dict(getattr(tool_call, "arguments", {}) or {}),
                "start_time": getattr(tool_call, "start_time", None),
                "end_time": getattr(tool_call, "end_time", None),
                "research_id": trace.get("research_id", ""),
                "candidate_id": trace.get("candidate_id", ""),
            }
            if result is not None:
                payload.update(
                    {
                        "title": str(getattr(result, "title", "") or ""),
                        "output_preview": str(getattr(result, "output", "") or ""),
                        "error_preview": str(
                            getattr(result, "error", "")
                            or getattr(tool_call, "error", "")
                            or ""
                        ),
                        "output_path": str(getattr(result, "output_path", "") or ""),
                        "metadata": selected_metadata,
                    }
                )
                if paper_title:
                    payload["paper_title"] = paper_title
            source_id = str(
                getattr(tool_call, "call_id", "")
                or f"{trace.get('turn_id', '')}:{getattr(tool_call, 'tool_name', '')}"
            )
            ResearchEventSink.append(
                ResearchEventInput(
                    event_type=event_type,
                    tenant_id=trace["tenant_id"],
                    user_id=trace["user_id"],
                    workspace_id=trace["workspace_id"],
                    session_id=trace["session_id"],
                    turn_id=trace["turn_id"],
                    trace_id=trace["trace_id"],
                    actor_type="tool",
                    actor_id=(mcp_server or str(getattr(tool_call, "tool_name", "") or "")),
                    source_kind="mcp_tool" if source == "mcp" else "tool_call",
                    source_id=source_id,
                    source_ref=(
                        f"session:{trace['session_id']}/tool:{source_id}"
                    ),
                    payload=payload,
                    privacy_state="private",
                    stable_state_version=event_type,
                ),
                workdir=self._task_runtime_workdir(context) or None,
            )
        except Exception:
            pass

    def _append_research_artifact_events(
        self,
        *,
        tool_call: ToolCall,
        context: ToolContext,
        result: ToolResult,
    ) -> None:
        try:
            from ..research import ResearchEventInput, ResearchEventSink
            from ..research.models import sha256_text

            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
            if tool_name in _RESEARCH_READ_ONLY_ARTIFACT_TOOLS:
                return
            trace = self._research_trace_payload(context)
            metadata = dict(getattr(result, "metadata", {}) or {})
            candidates: list[str] = []

            def add_paths(value: Any) -> None:
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        candidates.append(text)
                elif isinstance(value, dict):
                    for item in value.values():
                        add_paths(item)
                elif isinstance(value, (list, tuple, set)):
                    for item in value:
                        add_paths(item)

            add_paths(getattr(result, "output_path", ""))
            add_paths(metadata.get("output_path"))
            add_paths(metadata.get("artifact_paths"))
            add_paths(metadata.get("artifacts"))
            seen: set[str] = set()
            call_id = str(getattr(tool_call, "call_id", "") or "")
            for path in candidates:
                if path in seen or len(seen) >= 32:
                    continue
                seen.add(path)
                ResearchEventSink.append(
                    ResearchEventInput(
                        event_type="artifact.created",
                        tenant_id=trace["tenant_id"],
                        user_id=trace["user_id"],
                        workspace_id=trace["workspace_id"],
                        session_id=trace["session_id"],
                        turn_id=trace["turn_id"],
                        trace_id=trace["trace_id"],
                        actor_type="tool",
                        actor_id=tool_name,
                        source_kind="tool_artifact",
                        source_id=f"{call_id}:{sha256_text(path)}",
                        source_ref=path,
                        payload={
                            "path": path,
                            "tool_name": tool_name,
                            "call_id": call_id,
                        },
                        privacy_state="private",
                    ),
                    workdir=self._task_runtime_workdir(context) or None,
                )
        except Exception:
            pass

    def _append_research_file_events(
        self,
        *,
        tool_call: ToolCall,
        context: ToolContext,
        result: ToolResult,
    ) -> None:
        try:
            from ..research import ResearchEventInput, ResearchEventSink
            from ..research.models import sha256_text

            trace = self._research_trace_payload(context)
            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
            arguments = dict(getattr(tool_call, "arguments", {}) or {})
            metadata = dict(getattr(result, "metadata", {}) or {})
            call_id = str(getattr(tool_call, "call_id", "") or "")
            records: list[tuple[str, str, dict[str, Any]]] = []

            if tool_name in {"read", "write", "edit"}:
                path = str(
                    arguments.get("path")
                    or arguments.get("file_path")
                    or arguments.get("file")
                    or ""
                ).strip()
                if path:
                    event_type = "file.read" if tool_name == "read" else "file.written"
                    records.append(
                        (
                            event_type,
                            path,
                            {
                                "path": path,
                                "operation": tool_name,
                                "call_id": call_id,
                                "content_chars": len(
                                    str(arguments.get("content") or "")
                                ),
                            },
                        )
                    )

            if tool_name == "import_resource":
                for item in list(metadata.get("imported_resources") or []):
                    if not isinstance(item, dict):
                        continue
                    path = str(
                        item.get("path")
                        or item.get("container_path")
                        or item.get("mount_target_path")
                        or ""
                    ).strip()
                    if not path:
                        continue
                    records.append(
                        (
                            "file.uploaded",
                            path,
                            {
                                "path": path,
                                "container_path": str(
                                    item.get("container_path") or ""
                                ),
                                "name": str(item.get("name") or ""),
                                "kind": str(item.get("kind") or ""),
                                "mounted": bool(item.get("mounted")),
                                "imported": bool(item.get("imported")),
                                "call_id": call_id,
                            },
                        )
                    )

            seen: set[tuple[str, str]] = set()
            for event_type, path, payload in records[:32]:
                key = (event_type, path)
                if key in seen:
                    continue
                seen.add(key)
                ResearchEventSink.append(
                    ResearchEventInput(
                        event_type=event_type,
                        tenant_id=trace["tenant_id"],
                        user_id=trace["user_id"],
                        workspace_id=trace["workspace_id"],
                        session_id=trace["session_id"],
                        turn_id=trace["turn_id"],
                        trace_id=trace["trace_id"],
                        actor_type="tool",
                        actor_id=tool_name,
                        source_kind="file_tool",
                        source_id=f"{call_id}:{sha256_text(path)}",
                        source_ref=path,
                        payload=payload,
                        privacy_state="private",
                    ),
                    workdir=self._task_runtime_workdir(context) or None,
                )
        except Exception:
            pass

    def _append_research_subagent_events(
        self,
        *,
        tool_call: ToolCall,
        context: ToolContext,
        result: ToolResult,
    ) -> None:
        if str(getattr(tool_call, "tool_name", "") or "") not in {
            "task",
            "assign_task",
        }:
            return
        try:
            from ..research import ResearchEventInput, ResearchEventSink

            trace = self._research_trace_payload(context)
            metadata = dict(getattr(result, "metadata", {}) or {})
            arguments = dict(getattr(tool_call, "arguments", {}) or {})
            default_agent = str(
                metadata.get("subagent")
                or metadata.get("worker_name")
                or arguments.get("worker_name")
                or arguments.get("subagent_type")
                or arguments.get("agent")
                or ""
            ).strip()
            default_task = str(
                arguments.get("task")
                or arguments.get("prompt")
                or arguments.get("description")
                or ""
            ).strip()
            default_handoff = (
                dict(metadata.get("subtask_handoff") or {})
                if isinstance(metadata.get("subtask_handoff"), dict)
                else {}
            )

            def work_items(value: Any) -> list[Any]:
                return list(value)[:64] if isinstance(value, (list, tuple)) else []
            candidates: list[dict[str, Any]] = []
            if str(metadata.get("session_id") or metadata.get("task_id") or "").strip():
                candidates.append(
                    {
                        "session_id": str(
                            metadata.get("session_id") or metadata.get("task_id") or ""
                        ),
                        "agent": default_agent,
                        "task_id": str(
                            metadata.get("task_id")
                            or getattr(tool_call, "call_id", "")
                            or ""
                        ),
                        "task": default_task,
                        "status": str(
                            metadata.get("outcome_status")
                            or metadata.get("child_outcome_status")
                            or metadata.get("status")
                            or "completed"
                        ),
                        "handoff": default_handoff,
                        "completed_work": work_items(
                            metadata.get("completed_subtask_work")
                        ),
                        "failed_work": work_items(
                            metadata.get("failed_subtask_work")
                        ),
                    }
                )
            for item in list(metadata.get("tasks") or []):
                if isinstance(item, dict):
                    candidates.append(
                        {
                            "session_id": str(item.get("session_id") or ""),
                            "agent": str(
                                item.get("agent")
                                or item.get("worker_name")
                                or default_agent
                                or ""
                            ),
                            "task_id": str(
                                item.get("task_id")
                                or item.get("id")
                                or getattr(tool_call, "call_id", "")
                                or ""
                            ),
                            "task": str(
                                item.get("task")
                                or item.get("prompt")
                                or default_task
                                or ""
                            ),
                            "status": str(
                                item.get("outcome_status")
                                or item.get("child_outcome_status")
                                or item.get("status")
                                or "completed"
                            ),
                            "handoff": (
                                dict(item.get("subtask_handoff") or {})
                                if isinstance(item.get("subtask_handoff"), dict)
                                else {}
                            ),
                            "completed_work": work_items(
                                item.get("completed_subtask_work")
                            ),
                            "failed_work": work_items(
                                item.get("failed_subtask_work")
                            ),
                        }
                    )
            seen: set[tuple[str, str]] = set()
            for item in candidates[:64]:
                child_session_id = item["session_id"].strip()
                assignment_id = str(
                    item.get("task_id")
                    or getattr(tool_call, "call_id", "")
                    or child_session_id
                ).strip()
                identity = (child_session_id, assignment_id)
                if not child_session_id or identity in seen:
                    continue
                seen.add(identity)
                status = item["status"].strip().lower()
                has_error = bool(str(getattr(result, "error", "") or "").strip())
                if status == "partial":
                    terminal_event_type = "agent.partial"
                elif status in {
                    "awaiting_input",
                    "waiting_for_user",
                    "needs_parent_input",
                    "needs_user_input",
                }:
                    terminal_event_type = "agent.waiting"
                elif status in {"cancelled", "canceled"}:
                    terminal_event_type = "agent.cancelled"
                elif has_error or status in {"failed", "error", "blocked"}:
                    terminal_event_type = "agent.failed"
                else:
                    terminal_event_type = "agent.completed"
                source_id = assignment_id or child_session_id
                handoff = (
                    dict(item.get("handoff") or {})
                    if isinstance(item.get("handoff"), dict)
                    else {}
                )
                base_payload = {
                    "assignment_id": assignment_id,
                    "task_id": assignment_id,
                    "task": str(item.get("task") or "")[:1600],
                    "subagent_session_id": child_session_id,
                    "subagent_name": str(item.get("agent") or ""),
                    "status": status,
                    "parent_session_id": trace["session_id"],
                    "parent_tool_call_id": str(
                        getattr(tool_call, "call_id", "") or ""
                    ),
                    "scheduler_mode": str(metadata.get("scheduler_mode") or ""),
                    "result_summary": str(
                        handoff.get("result_text")
                        or metadata.get("result_summary")
                        or ""
                    )[:2000],
                    "completed_work": work_items(
                        handoff.get("completed_work")
                        or item.get("completed_work")
                    ),
                    "failed_work": work_items(
                        handoff.get("failed_work")
                        or item.get("failed_work")
                    ),
                    "review_verdict": str(
                        handoff.get("review_verdict") or ""
                    )[:1200],
                    "recovery_required": bool(
                        handoff.get("recovery_required")
                        or metadata.get("recovery_required")
                    ),
                }
                for event_type in ("agent.started", terminal_event_type):
                    payload = dict(base_payload)
                    if event_type != "agent.started":
                        payload["error_preview"] = str(
                            getattr(result, "error", "") or ""
                        )[:1200]
                        payload["blocker"] = str(metadata.get("blocker") or "")[:1200]
                        payload["next_action"] = str(
                            metadata.get("next_action") or ""
                        )[:1200]
                    ResearchEventSink.append(
                        ResearchEventInput(
                            event_type=event_type,
                            tenant_id=trace["tenant_id"],
                            user_id=trace["user_id"],
                            workspace_id=trace["workspace_id"],
                            session_id=trace["session_id"],
                            turn_id=trace["turn_id"],
                            trace_id=trace["trace_id"],
                            actor_type="agent",
                            actor_id=str(item.get("agent") or "").strip()
                            or "subagent",
                            source_kind="subagent",
                            source_id=source_id,
                            source_ref=(
                                f"session:{trace['session_id']}/assignment:{assignment_id}"
                                f"/subagent:{child_session_id}"
                            ),
                            payload=payload,
                            privacy_state="private",
                            stable_state_version=f"{event_type}:{status}",
                        ),
                        workdir=self._task_runtime_workdir(context) or None,
                    )
        except Exception:
            pass

    def _tool_runtime_metadata(
        self,
        *,
        tool_call: ToolCall,
        result: ToolResult | None = None,
        source: str = "",
        mcp_server: str = "",
        mcp_tool_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_metadata = dict(getattr(tool_call, "metadata", {}) or {})
        result_metadata = dict(getattr(result, "metadata", {}) or {}) if result else {}
        policy = dict(tool_metadata.get("policy") or result_metadata.get("policy") or {})
        capability = dict(policy.get("capability") or {})
        result_budget_tokens = int(capability.get("result_budget_tokens") or 0)
        output_preview = ""
        error_preview = ""
        if result is not None:
            output_preview = compact_runtime_text(
                getattr(result, "output", "") or "",
                result_budget_tokens=result_budget_tokens,
            )
            error_preview = compact_runtime_text(
                getattr(result, "error", "") or "",
                default_chars=2000,
            )

        external_refs = {
            key: value
            for key, value in {
                "identifier": result_metadata.get("identifier"),
                "job_id": result_metadata.get("job_id"),
                "process_id": result_metadata.get("process_id"),
                "sandbox_job_id": result_metadata.get("sandbox_job_id"),
                "workflow_run_id": result_metadata.get("run_id")
                or result_metadata.get("workflow_run_id"),
                "review_id": result_metadata.get("id"),
            }.items()
            if str(value or "").strip()
        }
        return {
            "tool_name": str(tool_call.tool_name or ""),
            "call_id": str(tool_call.call_id or ""),
            "source": source,
            "mcp_server": mcp_server,
            "mcp_tool_name": mcp_tool_name,
            "arguments": dict(tool_call.arguments or {}),
            "policy": policy,
            "capability_contract": {
                "supports_async_submit": bool(capability.get("supports_async_submit")),
                "supports_resume": bool(capability.get("supports_resume")),
                "interrupt_behavior": str(capability.get("interrupt_behavior") or ""),
                "result_budget_tokens": result_budget_tokens,
            },
            "external_refs": external_refs,
            "result_flags": {
                "accepted_async": bool(result_metadata.get("accepted_async")),
                "human_review_required": bool(result_metadata.get("human_review_required")),
                "should_end_turn": bool(result_metadata.get("should_end_turn")),
                "sandbox_executed": bool(result_metadata.get("sandbox_executed")),
            },
            "result_metadata": result_metadata,
            "output_preview": output_preview,
            "error_preview": error_preview,
            **dict(extra or {}),
        }

    def _record_tool_runtime(
        self,
        *,
        tool_call: ToolCall,
        context: ToolContext,
        status: str,
        phase: str,
        source: str,
        result: ToolResult | None = None,
        mcp_server: str = "",
        mcp_tool_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            TaskRuntimeStore.record_event(
                runtime_id=task_runtime_id_for_tool_call(
                    self.agent.session_id,
                    tool_call.call_id or tool_call.tool_name,
                ),
                runtime_kind="agent_tool",
                status=status,
                phase=phase,
                source=source,
                session_id=str(getattr(self.agent, "session_id", "") or ""),
                title=str(tool_call.tool_name or ""),
                external_id=str(tool_call.call_id or ""),
                tool_name=str(tool_call.tool_name or ""),
                metadata=self._tool_runtime_metadata(
                    tool_call=tool_call,
                    result=result,
                    source=source,
                    mcp_server=mcp_server,
                    mcp_tool_name=mcp_tool_name,
                    extra=extra,
                ),
                workdir=self._task_runtime_workdir(context),
            )
        except Exception:
            pass

    async def _emit_realtime_tool_progress(
        self,
        *,
        tool_call: ToolCall,
        status: str,
    ) -> None:
        # 流式模式本身已由 websocket 主循环逐步发送，避免重复推送。
        if bool(getattr(self.agent, "_frontend_stream", True)):
            return

        emitter = getattr(self.agent, "_agent_event_emitter", None)
        if not callable(emitter):
            return

        payload = {
            "kind": "tool_progress",
            "status": str(status or "").strip().lower(),
            "session_id": str(getattr(self.agent, "session_id", "") or ""),
            "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
            "tool_args": dict(getattr(tool_call, "arguments", {}) or {}),
            "call_id": str(getattr(tool_call, "call_id", "") or ""),
            "tool_call": self._serialize_frontend_tool_call(tool_call),
        }

        try:
            result = emitter(payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            pass

    def _tool_progress_min_interval_seconds(self) -> float:
        try:
            raw = float(os.environ.get("TCODE_TOOL_PROGRESS_MIN_INTERVAL_SECONDS", "0.75"))
        except Exception:
            raw = 0.75
        return max(0.0, min(raw, 10.0))

    async def _report_tool_runtime_progress(
        self,
        tool_call: ToolCall,
        value: Any,
    ) -> None:
        """Project a tool's semantic progress into the existing public event stream."""

        event = coerce_tool_progress_event(value)
        if event is None:
            return

        now = asyncio.get_running_loop().time()
        setattr(tool_call, "_last_progress_monotonic", now)
        signature = (
            event.message,
            event.current,
            event.total,
            event.unit,
            event.status,
        )
        if signature == getattr(tool_call, "_last_progress_signature", None):
            return

        last_emitted = float(getattr(tool_call, "_last_progress_emitted_at", 0.0) or 0.0)
        min_interval = self._tool_progress_min_interval_seconds()
        terminal_progress = (
            event.current is not None
            and event.total is not None
            and event.current >= event.total
        ) or event.status in {"completed", "failed", "error", "cancelled", "canceled"}
        if last_emitted and now - last_emitted < min_interval and not terminal_progress:
            return
        setattr(tool_call, "_last_progress_signature", signature)
        setattr(tool_call, "_last_progress_emitted_at", now)

        emitter = getattr(self.agent, "_agent_event_emitter", None)
        if not callable(emitter):
            return

        payload = {
            "kind": "tool_runtime_progress",
            "phase": "tool_progress",
            "status": event.status or "running",
            "session_id": str(getattr(self.agent, "session_id", "") or ""),
            "agent_name": str(
                getattr(getattr(self.agent, "agent_info", None), "name", "") or ""
            ),
            "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
            "call_id": str(getattr(tool_call, "call_id", "") or ""),
            "message": event.message,
            **event.to_dict(),
        }
        try:
            emitted = emitter(payload)
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:
            pass

    def _build_tool_context(self, tool_call: ToolCall) -> ToolContext:
        # 这里统一拼上下文，避免内建工具和 MCP 工具各自塞一遍运行时字段。
        active_turn_id = str(
            getattr(self.agent, "_active_research_turn_id", "") or ""
        )
        active_trace_id = str(
            getattr(self.agent, "_active_research_trace_id", "") or ""
        )
        async def report_progress(value: Any) -> None:
            await self._report_tool_runtime_progress(tool_call, value)

        async def emit_agent_event(value: Any) -> None:
            payload = dict(value) if isinstance(value, dict) else {}
            if str(tool_call.tool_name or "") in {"task", "assign_task"}:
                child_session_id = str(payload.get("session_id") or "").strip()
                if child_session_id and child_session_id != str(
                    getattr(self.agent, "session_id", "") or ""
                ):
                    metadata = dict(getattr(tool_call, "metadata", {}) or {})
                    metadata["child_session_id"] = child_session_id
                    tool_call.metadata = metadata
            emitter = getattr(self.agent, "_agent_event_emitter", None)
            if not callable(emitter):
                return
            emitted = emitter(value)
            if hasattr(emitted, "__await__"):
                await emitted

        delegated_call_count = 0

        async def invoke_plugin_tool(
            delegated_tool_id: str,
            delegated_arguments: dict[str, Any],
            *,
            source_plugin: str,
            parent_call_id: str,
        ) -> dict[str, Any]:
            """让隔离插件重新进入当前 Agent 的完整工具执行管线。"""

            nonlocal delegated_call_count
            delegated_call_count += 1
            maximum_calls = max(
                1,
                min(
                    int(os.environ.get("TCODE_PLUGIN_MAX_DELEGATED_CALLS", "16") or 16),
                    128,
                ),
            )
            if delegated_call_count > maximum_calls:
                raise PermissionError("plugin delegated tool call limit exceeded")
            parent_metadata = dict(getattr(tool_call, "metadata", {}) or {})
            depth = int(parent_metadata.get("plugin_delegation_depth") or 0) + 1
            maximum_depth = max(
                1,
                min(
                    int(os.environ.get("TCODE_PLUGIN_MAX_DELEGATION_DEPTH", "3") or 3),
                    8,
                ),
            )
            if depth > maximum_depth:
                raise PermissionError("plugin delegated tool depth exceeded")
            nested = ToolCall(
                tool_name=str(delegated_tool_id),
                call_id=(
                    f"{str(parent_call_id or tool_call.call_id or 'plugin')}:"
                    f"nested:{delegated_call_count}"
                )[:240],
                arguments=dict(delegated_arguments or {}),
                metadata={
                    "plugin_delegated": True,
                    "plugin_source": str(source_plugin),
                    "plugin_parent_call_id": str(parent_call_id or tool_call.call_id or ""),
                    "plugin_delegation_depth": depth,
                },
            )
            result = await self.execute(nested)
            return {
                "title": result.title,
                "output": result.output,
                "error": _plugin_delegated_error(
                    result.error,
                    workdir=str(getattr(self.agent, "workdir", "") or ""),
                ),
                "metadata": _plugin_delegated_metadata(result.metadata),
                "truncated": bool(result.truncated),
            }

        session_metadata = dict(
            getattr(self.agent._existing_session, "metadata", {}) or {}
        )
        config_snapshot = getattr(self.agent, "_config_snapshot", None)
        workspace_metadata = session_metadata.get("agent_workspace")
        workspace_metadata = (
            dict(workspace_metadata) if isinstance(workspace_metadata, dict) else {}
        )
        control_workdir = str(
            workspace_metadata.get("control_workdir")
            or session_metadata.get("control_workdir")
            or self.agent.workdir
            or ""
        ).strip()
        from ..message_context import request_denied_tools
        denied_tools = sorted(set(session_metadata.get("request_denied_tools") or [])
            | set(request_denied_tools(self.agent.messages)))

        from ..runtime.async_resume import active_resume
        restore_resources = dict(((active_resume.get() or {}).get("task_context") or {}).get("resource_context") or {})
        effective_resources = _merge_resource_context_patch(
            dict(self.agent._session_resource_context or {}),
            {key: value for key, value in restore_resources.items() if key in {"uploaded_files", "external_mounts"}},
        )
        return ToolContext(
            session_id=self.agent.session_id,
            message_id=self.agent.messages[-1].id,
            agent=self.agent.agent_info.name,
            abort=self.agent._abort_event,
            call_id=tool_call.call_id,
            messages=self.agent.messages,
            progress_callback=report_progress,
            extra={
                "computer_task_intent": getattr(self.agent, "_computer_task_intent", None),
                "workdir": self.agent.workdir,
                "config_snapshot_id": str(
                    getattr(config_snapshot, "snapshot_id", "") or ""
                ),
                "config_snapshot_workdir": str(
                    getattr(config_snapshot, "workdir", "") or ""
                ),
                "config_fingerprint": str(
                    getattr(config_snapshot, "config_fingerprint", "") or ""
                ),
                "config_policy_fingerprint": str(
                    getattr(config_snapshot, "policy_fingerprint", "") or ""
                ),
                "config_summary": dict(
                    getattr(config_snapshot, "summary", {}) or {}
                ),
                "command_policy": dict(
                    getattr(config_snapshot, "command_policy", {}) or {}
                ),
                "control_workdir": control_workdir,
                # Descendants share the side's private scope, not the main
                # workspace that owns its sandbox registration/configuration.
                "side_chat_workdir": str(session_metadata.get("side_chat_workdir")
                    or (self.agent.workdir if session_metadata.get("side_question") else "")),
                "request_denied_tools": denied_tools,
                "agent_workspace": workspace_metadata,
                "provider": self.agent.provider,
                "parent_session_id": str(
                    getattr(self.agent._existing_session, "parent_id", "") or ""
                ),
                "tenant_id": str(getattr(self.agent._existing_session, "tenant_id", "") or ""),
                "user_id": str(getattr(self.agent._existing_session, "user_id", "") or ""),
                "workspace_id": str(getattr(self.agent._existing_session, "workspace_id", "") or ""),
                "wiki_context": (
                    dict(session_metadata.get("wiki_context") or {})
                    if isinstance(session_metadata.get("wiki_context"), dict)
                    else {}
                ),
                "turn_id": active_turn_id,
                "trace_id": active_trace_id,
                "request_id": str(
                    getattr(self.agent, "_active_client_request_id", "") or ""
                ),
                "resource_context": effective_resources,
                "model_context_snapshot": dict(getattr(self.agent, "_async_submission_context", {}) or {}),
                "execution_profile": getattr(self.agent, "_execution_profile", None),
                "__tool_discovery_catalog__": getattr(self.agent, "_discoverable_tool_definitions", None),
                "__tool_discovery_activate__": getattr(self.agent, "_activate_discovered_tools", None),
                "__tool_discovery_status__": getattr(self.agent, "_tool_discovery_status", None),
                "selected_workflow_key": self.agent._selected_workflow_key(),
                "current_tool_ids": list(
                    dict(getattr(self.agent, "_last_tool_routing", {}) or {}).get(
                        "selected_tools", []
                    )
                ),
                "scheduler_mode": self.agent._scheduler_mode,
                "use_swarms": self.agent._use_swarms,
                "swarm_topology": self.agent._swarm_topology,
                "external_swarm_communication_mode": self.agent._external_swarm_communication_mode,
                "external_swarm_bus": self.agent._external_swarm_bus,
                "external_swarm_rounds": self.agent._external_swarm_rounds,
                "external_swarm_agent_timeout_seconds": self.agent._external_swarm_agent_timeout_seconds,
                "external_swarm_max_retries": self.agent._external_swarm_max_retries,
                "external_swarm_fallback_on_error": self.agent._external_swarm_fallback_on_error,
                "route_symphony_on_mcp_tool": self.agent._route_symphony_on_mcp_tool,
                "__agent_event_emit__": emit_agent_event,
                "__tool_progress_report__": report_progress,
                "__permission_ask__": self.agent._ask_permission_from_context,
                "__goal_checkpoint__": getattr(self.agent, '_goal_checkpoint_callback', None),
                "__goal_question__": getattr(self.agent, '_goal_question_callback', None),
                "__goal_authorization_blocked__": getattr(self.agent, '_goal_authorization_blocked_callback', None),
                "__plugin_tool_invoke__": invoke_plugin_tool,
                "__permission_prompt__": getattr(
                    self.agent, "_request_permission_decision", None
                ),
                "__permission_remember__": getattr(
                    self.agent, "_remember_permission_patterns", None
                ),
                "__permission_resolve__": self.agent._resolve_permission_action,
                "__question_ask__": self.agent._ask_question_from_context,
                "__question_resolve__": self.agent._resolve_question_from_context,
                "__question_policy__": getattr(self.agent, "_question_policy", "ask"),
                "__set_plan_mode__": self.agent._set_plan_mode,
                "agent_permission": dict(self.agent.agent_info.permission or {}),
                "shared_memory_key": getattr(self.agent, "_shared_memory_key", ""),
                "shared_memory_source": getattr(
                    self.agent, "_shared_memory_source", ""
                ),
                "task_child_policy": getattr(
                    self.agent, "_task_child_policy", "persistent"
                ),
                "resident_generalist": bool(
                    getattr(self.agent, "_resident_generalist", False)
                ),
                "direct_execution_mode": bool(
                    getattr(self.agent, "_direct_execution_mode", False)
                ),
                "direct_execution_scope": str(
                    getattr(self.agent, "_direct_execution_scope", "") or ""
                ),
                "default_skill_enabled": bool(
                    getattr(self.agent, "_default_skill_enabled", lambda: False)()
                ),
                "default_skill_roots": [
                    str(item)
                    for item in getattr(
                        self.agent, "_candidate_default_skill_roots", lambda: []
                    )()
                ],
                "default_skill_patterns": list(
                    getattr(self.agent, "_default_skill_patterns", lambda: [])()
                ),
                "tool_trace_context": dict(
                    getattr(self.agent, "_active_tool_trace_context", {}) or {}
                ),
                "skill_routing_trace": dict(
                    getattr(self.agent, "_skill_routing_trace", {}) or {}
                ),
                "skill_composite_plan": dict(
                    getattr(self.agent, "_skill_composite_plan", {}) or {}
                ),
            },
        )

    def _sandbox_config_snapshot(self) -> dict[str, str]:
        try:
            cfg = load_sandbox_config()
        except Exception:
            return {}
        return {
            "backend": str(cfg.backend or ""),
            "docker_runtime": str(cfg.docker_runtime or ""),
            "image": str(cfg.image or ""),
            "runner_url": str(cfg.runner_url or ""),
            "network": str(cfg.network or ""),
            "container_workspace": str(cfg.container_workspace or ""),
            "profile": str(cfg.profile_name or ""),
        }

    def _attach_execution_environment(
        self,
        tool_call: ToolCall,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(metadata or {})
        existing = _dict_value(metadata.get("execution_environment"))
        sandbox_meta = _dict_value(metadata.get("sandbox"))
        existing_location = str(existing.get("location") or "").strip().lower()
        if tool_call.tool_name == "browser" and metadata.get("desktop_browser") is True:
            metadata["execution_environment"] = {**existing, "tool_name": "browser"}
            return metadata
        if _truthy(metadata.get("execution_skipped")) or existing_location in {
            "not_executed",
            "blocked",
        }:
            metadata["execution_environment"] = _compact_metadata(
                {
                    **existing,
                    "location": "not_executed",
                    "label": "Not executed",
                    "sandboxed": False,
                    "tool_name": str(tool_call.tool_name or ""),
                }
            )
            metadata["execution_skipped"] = True
            metadata.setdefault("sandbox_executed", False)
            return metadata
        sandbox_signal = (
            _truthy(existing.get("sandboxed"))
            or str(existing.get("location") or "").strip().lower() == "sandbox"
            or _truthy(metadata.get("sandbox"))
            or _truthy(metadata.get("sandbox_executed"))
            or _truthy(metadata.get("sandbox_worker"))
            or _truthy(sandbox_meta.get("enabled"))
            or _truthy(sandbox_meta.get("sandbox_worker"))
            or bool(
                _text_value(
                    metadata.get("sandbox_job_id"),
                    metadata.get("container_id"),
                    metadata.get("container_name"),
                    sandbox_meta.get("sandbox_job_id"),
                    sandbox_meta.get("container_id"),
                    sandbox_meta.get("container_name"),
                )
            )
        )

        if sandbox_signal:
            cfg = self._sandbox_config_snapshot()
            env = {
                **existing,
                "location": "sandbox",
                "label": "Sandbox",
                "sandboxed": True,
                "tool_name": str(tool_call.tool_name or ""),
                "sandbox_tool": _text_value(
                    existing.get("sandbox_tool"),
                    metadata.get("sandbox_tool"),
                    metadata.get("sandbox_tool_name"),
                    metadata.get("runtime_tool"),
                    "plugin_runtime" if metadata.get("plugin_tool_id") else "",
                ),
                "runner_url": _text_value(existing.get("runner_url"), metadata.get("sandbox_runner_url"), cfg.get("runner_url")),
                "backend": _text_value(existing.get("backend"), metadata.get("sandbox_backend"), sandbox_meta.get("backend"), metadata.get("backend"), cfg.get("backend")),
                "runtime": _text_value(existing.get("runtime"), metadata.get("sandbox_runtime"), metadata.get("docker_runtime"), sandbox_meta.get("runtime"), cfg.get("docker_runtime")),
                "image": _text_value(existing.get("image"), metadata.get("sandbox_image"), sandbox_meta.get("image"), metadata.get("image"), cfg.get("image")),
                "network": _text_value(existing.get("network"), metadata.get("sandbox_network"), sandbox_meta.get("network"), metadata.get("network_policy"), metadata.get("network"), cfg.get("network")),
                "profile": _text_value(existing.get("profile"), metadata.get("sandbox_profile"), sandbox_meta.get("sandbox_profile"), metadata.get("profile"), cfg.get("profile")),
                "status": _text_value(existing.get("status"), metadata.get("sandbox_status"), metadata.get("status"), sandbox_meta.get("status")),
                "exit_code": _text_value(existing.get("exit_code"), metadata.get("exit_code"), metadata.get("returncode"), sandbox_meta.get("exit_code")),
                "sandbox_job_id": _text_value(existing.get("sandbox_job_id"), metadata.get("sandbox_job_id"), sandbox_meta.get("sandbox_job_id")),
                "container_id": _text_value(existing.get("container_id"), metadata.get("container_id"), sandbox_meta.get("container_id")),
                "container_name": _text_value(existing.get("container_name"), metadata.get("container_name"), sandbox_meta.get("container_name")),
                "worker_input": _text_value(existing.get("worker_input"), metadata.get("worker_input"), sandbox_meta.get("input_path")),
                "worker_output": _text_value(existing.get("worker_output"), metadata.get("worker_output"), sandbox_meta.get("output_path")),
            }
            metadata["execution_environment"] = _compact_metadata(env)
            metadata.setdefault("sandbox_executed", True)
            for field in ("sandbox_job_id", "container_id", "container_name"):
                value = metadata["execution_environment"].get(field)
                if value:
                    metadata.setdefault(field, value)
            metadata.setdefault("sandbox_backend", metadata["execution_environment"].get("backend", ""))
            metadata.setdefault("sandbox_runtime", metadata["execution_environment"].get("runtime", ""))
            metadata.setdefault("sandbox_image", metadata["execution_environment"].get("image", ""))
            metadata.setdefault("sandbox_runner_url", metadata["execution_environment"].get("runner_url", ""))
            return metadata

        env = {
            **existing,
            "location": "host",
            "label": "Host",
            "sandboxed": False,
            "tool_name": str(tool_call.tool_name or ""),
            "process_id": str(os.getpid()),
            "workdir": str(getattr(self.agent, "workdir", "") or ""),
        }
        metadata["execution_environment"] = _compact_metadata(env)
        metadata.setdefault("sandbox_executed", False)
        if "sandbox" not in metadata:
            metadata["sandbox"] = False
        return metadata

    async def _finalize_tool_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        advance_tool_pipeline_stage(ToolPipelineStage.AFTER_HOOK_ENTERED)
        final_result = await self.agent._apply_plugin_after_tool_execute(
            tool_call,
            result,
        )
        advance_tool_pipeline_stage(ToolPipelineStage.AFTER_HOOK_COMPLETED)
        metadata = dict(final_result.metadata or {})

        try:
            global_limit = int(os.environ.get("TCODE_TOOL_RESULT_MAX_CHARS", "120000"))
        except Exception:
            global_limit = 120000
        global_limit = max(1000, min(global_limit, 2_000_000))
        capability = get_tool_capability(str(tool_call.tool_name or ""))
        capability_limit = max(0, int(capability.result_budget_tokens or 0)) * 4
        result_limit = min(global_limit, capability_limit) if capability_limit else global_limit
        # Preserve full observations before the pre-existing hard output cap.
        # Enforce snapshots only explicitly admitted tools. Shadow may inspect
        # unknown/new tools, but stores only aggregate metrics and never content.
        metadata.pop("context_source_id", None)
        metadata.pop("context_source_complete", None)
        metadata.pop("context_compression_shadow", None)
        from ..context.compression.contracts import Owner, Policy

        policy = Policy.from_env().restricted(
            (getattr(getattr(self.agent, "_existing_session", None), "metadata", {}) or {}).get("context_compression"),
            (getattr(getattr(self.agent, "agent_info", None), "options", {}) or {}).get("context_compression"),
        )
        compression_safe = (
            policy.admits(tool_call.tool_name)
            and metadata.get("compression_risk") not in {"high", "critical"}
            and not metadata.get("compression_disabled")
        )
        if policy.mode == "shadow" and compression_safe:
            from ..context.compression.service import shadow_measurement

            try:
                prompt_limit = int(
                    os.environ.get("TCODE_LLM_TOOL_RESULT_MAX_CHARS", "40000")
                )
            except Exception:
                prompt_limit = 40000
            prompt_limit = max(1000, min(prompt_limit, 2_000_000))
            measured = await asyncio.to_thread(
                shadow_measurement,
                final_result.output,
                tool_call.tool_name,
                policy,
                prompt_limit,
            )
            if measured is not None:
                measured["source_complete"] = not final_result.truncated
                metadata["context_compression_shadow"] = measured

        if len(final_result.output) > result_limit and policy.mode == "enforce":
            from ..context.compression.store import CapsuleStore

            if compression_safe:
                try:
                    session = self.agent._existing_session
                    owner = Owner(*(str(getattr(session, k, "") or "") for k in ("tenant_id", "user_id", "workspace_id")), self.agent.session_id)
                    def capture():
                        return CapsuleStore(self.agent.workdir, owner, policy).put(
                            final_result.output, {"tool_call_id": tool_call.call_id, "tool": tool_call.tool_name,
                                                  "source_complete": not final_result.truncated})
                    metadata["context_source_id"] = await asyncio.to_thread(capture)
                    metadata["context_source_complete"] = not final_result.truncated
                except Exception as exc:
                    metadata["context_capture_error"] = type(exc).__name__
        projected = project_tool_output(final_result.output, max_chars=result_limit)
        if projected.truncated:
            metadata.update(
                {
                    "toolResultHardLimited": True,
                    "toolResultOriginalChars": projected.original_chars,
                    "toolResultShownChars": projected.shown_chars,
                    "toolResultLimitChars": result_limit,
                }
            )
            final_result.output = projected.text
            final_result.truncated = True

        error_projection = bound_text(
            final_result.error or "",
            max_chars=min(16_000, result_limit),
            label="tool error truncated",
        )
        if error_projection.truncated:
            final_result.error = error_projection.text
            metadata.update(
                {
                    "toolErrorHardLimited": True,
                    "toolErrorOriginalChars": error_projection.original_chars,
                }
            )

        existing_tool_metadata = dict(getattr(tool_call, "metadata", {}) or {})
        if "policy" in existing_tool_metadata and "policy" not in metadata:
            metadata["policy"] = existing_tool_metadata["policy"]
        patch = metadata.get("resource_context_patch")
        if isinstance(patch, dict) and patch:
            merged_context = _merge_resource_context_patch(
                dict(getattr(self.agent, "_session_resource_context", {}) or {}),
                patch,
            )
            self.agent._session_resource_context = merged_context
            metadata["resource_context"] = dict(merged_context)
        metadata = self._attach_execution_environment(tool_call, metadata)
        if "argument_transport_envelope" in existing_tool_metadata:
            metadata["argument_transport_envelope"] = existing_tool_metadata["argument_transport_envelope"]
        if final_result.output_path:
            metadata.setdefault("output_path", str(final_result.output_path))
        if final_result.truncated:
            metadata["truncated"] = True
        final_result.metadata = metadata
        tool_call.output = final_result.output
        if final_result.error:
            tool_call.error = str(final_result.error)
        tool_call.metadata = dict(final_result.metadata or {})
        return final_result

    async def _record_tool_execution(
        self,
        *,
        tool_call: ToolCall,
        result: ToolResult,
        context: ToolContext,
        source: str,
        mcp_server: str = "",
        mcp_tool_name: str = "",
    ) -> ToolResult:
        trace_context = {}
        if isinstance(context.extra, dict):
            trace_context = dict(context.extra.get("tool_trace_context") or {})
        try:
            paths = RuntimeArtifactManager.append_tool_execution_log(
                session_id=self.agent.session_id,
                agent_name=self.agent.agent_info.name,
                tool_name=tool_call.tool_name,
                call_id=tool_call.call_id,
                arguments=dict(tool_call.arguments or {}),
                result={
                    "title": result.title,
                    "output": result.output,
                    "error": result.error,
                    "output_path": result.output_path,
                    "metadata": dict(result.metadata or {}),
                },
                state=getattr(tool_call.state, "value", tool_call.state) or "",
                source=source,
                workdir=getattr(self.agent, "workdir", "") or None,
                workflow_run_id=str(trace_context.get("workflow_run_id") or "").strip(),
                workflow_stage_id=str(trace_context.get("workflow_stage_id") or "").strip(),
                parent_session_id=str(trace_context.get("parent_session_id") or "").strip(),
                subagent_session_id=str(trace_context.get("subagent_session_id") or "").strip(),
                workflow_template_key=str(trace_context.get("workflow_template_key") or "").strip(),
                workflow_owner_session_id=str(trace_context.get("workflow_owner_session_id") or "").strip(),
                worker_name=str(trace_context.get("worker_name") or "").strip(),
                mcp_server=mcp_server,
                mcp_tool_name=mcp_tool_name,
                turn_message_id=context.message_id,
                start_time=tool_call.start_time,
                end_time=tool_call.end_time,
            )
        except Exception:
            return result

        metadata = dict(result.metadata or {})
        metadata.setdefault("tool_execution_log_paths", paths)
        result.metadata = metadata
        tool_call.metadata = dict(tool_call.metadata or {})
        tool_call.metadata.setdefault("tool_execution_log_paths", paths)
        return result

    async def _finalize_and_record(
        self,
        *,
        tool_call: ToolCall,
        result: ToolResult,
        context: ToolContext,
        source: str,
        mcp_server: str = "",
        mcp_tool_name: str = "",
    ) -> ToolResult:
        advance_tool_pipeline_stage(ToolPipelineStage.FINALIZE_ENTERED, route=source)
        final_result = await self._finalize_tool_result(tool_call, result)
        recorded = await self._record_tool_execution(
            tool_call=tool_call,
            result=final_result,
            context=context,
            source=source,
            mcp_server=mcp_server,
            mcp_tool_name=mcp_tool_name,
        )

        # The execution log above keeps the original diagnostic for debugging;
        # everything after this boundary receives only the public projection.
        if str(recorded.error or "").strip():
            recorded.error = public_tool_error(
                tool_call.tool_name,
                str(recorded.error),
                recorded.metadata,
            )
            metadata = dict(recorded.metadata or {})
            metadata["error_projected_for_user"] = True
            recorded.metadata = metadata
            tool_call.error = recorded.error
            tool_call.metadata = dict(metadata)

        failed = bool(
            str(getattr(recorded, "error", "") or "").strip()
            or str(getattr(tool_call, "error", "") or "").strip()
        )
        if (tool_call.metadata.get('policy') or {}).get('goal_review_id'):
            from ..server.goal_tool_review import finish_review
            try:
                await finish_review(tool_call, recorded, context)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Failed to record goal tool review outcome')
        if source == "mcp":
            journal_event_type = "mcp.failed" if failed else "mcp.completed"
        else:
            journal_event_type = "tool.failed" if failed else "tool.completed"
        self._append_research_tool_event(
            tool_call=tool_call,
            context=context,
            event_type=journal_event_type,
            source=source,
            result=recorded,
            mcp_server=mcp_server,
            mcp_tool_name=mcp_tool_name,
        )
        self._append_research_subagent_events(
            tool_call=tool_call,
            context=context,
            result=recorded,
        )
        if not failed:
            self._append_research_artifact_events(
                tool_call=tool_call,
                context=context,
                result=recorded,
            )
            self._append_research_file_events(
                tool_call=tool_call,
                context=context,
                result=recorded,
            )

        # 自动记忆：把结构化结果写入持久记忆。
        if _auto_memory_enabled():
            await self._maybe_auto_memory(
                tool_call=tool_call,
                result=final_result,
                context=context,
                source=source,
                mcp_server=mcp_server,
            )

        self._record_tool_runtime(
            tool_call=tool_call,
            context=context,
            status=task_runtime_status_for_tool_result(recorded),
            phase="finalize",
            source=source,
            result=recorded,
            mcp_server=mcp_server,
            mcp_tool_name=mcp_tool_name,
        )

        final_status = "error" if str(getattr(tool_call, "error", "") or "").strip() else "completed"
        await self._emit_realtime_tool_progress(
            tool_call=tool_call,
            status=final_status,
        )

        advance_tool_pipeline_stage(
            ToolPipelineStage.FINALIZE_COMPLETED,
            route=source,
            outcome=final_status,
        )
        return recorded

    async def _maybe_auto_memory(
        self,
        *,
        tool_call: ToolCall,
        result: ToolResult,
        context: ToolContext,
        source: str,
        mcp_server: str = "",
    ) -> None:
        """Try to extract structured knowledge from tool results and write to memory."""
        if not result.output:
            return

        # 跳过只读工具和记忆工具，避免递归写入。
        if tool_call.tool_name in {
            "read", "glob", "grep", "todo", "todo_read", "task_graph_read",
            "memory", "memory_read", "memory_write", "memory_cleanup",
            "list_workers", "list_teams",
        }:
            return

        try:
            from ..task.summary_schema import SubagentSummaryValidator

            # 尝试提取结构化摘要。
            validation = SubagentSummaryValidator.validate_raw_output(result.output)
            if not validation.get("valid"):
                return

            # 解析摘要。
            try:
                data = json.loads(result.output.strip())
            except (json.JSONDecodeError, TypeError):
                return

            if not isinstance(data, dict):
                return

            # 检查置信度阈值。
            confidence = float(data.get("confidence", 0.0) or 0.0)
            if confidence < _auto_memory_min_confidence():
                return

            verdict = str(data.get("verdict", "") or "").strip()
            summary_text = str(data.get("summary", "") or "").strip()
            if not summary_text:
                return

            # 从工具名称建立任务类型。
            task_type = mcp_server or tool_call.tool_name

            # 从当前上下文建立目标。
            goal = ""
            for msg in getattr(self.agent, "messages", []) or []:
                if getattr(msg, "role", "") == "user":
                    goal = str(getattr(msg, "content", "") or "")[:200]
                    break

            from ..memory.manager import MemoryManager

            MemoryManager.add_structured(
                workdir=getattr(self.agent, "workdir", "") or None,
                source=f"auto:{tool_call.tool_name}",
                tags=["auto_collected", tool_call.tool_name],
                task_type=task_type,
                goal=goal,
                outcome=summary_text,
                confidence=confidence,
                decision=verdict,
                failure_reason="" if verdict not in {"fail", "blocked"} else summary_text[:200],
            )

        except Exception:
            pass  # Best-effort — never block tool execution on memory write failure

    async def record_synthetic_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        # A rejected child call still owns a request/trace. In particular, it
        # must not finalize an inherited workflow_run trace from its parent.
        async def finalize_rejection(call: ToolCall) -> ToolResult:
            context = self._build_tool_context(call)
            advance_tool_pipeline_stage(ToolPipelineStage.CONTEXT_BUILT)
            return await self._finalize_and_record(
                tool_call=call,
                result=result,
                context=context,
                source="synthetic",
            )

        return await self._pipeline.execute(tool_call, finalize_rejection)

    async def _execute_in_sandbox(
        self,
        tool_call: ToolCall,
        context: ToolContext,
    ) -> Optional[ToolResult]:
        """Execute a command-like tool in the sandbox and fail closed on errors."""
        body_entered = False
        body_completed = False
        try:
            from ..runtime.sandbox import (
                build_tool_sandbox_spec,
                get_sandbox_runtime,
            )

            command = self._extract_tool_command(tool_call)
            if not command:
                return None

            spec = build_tool_sandbox_spec(
                tool_name=tool_call.tool_name,
                command=command,
                requested_workdir=getattr(self.agent, "workdir", "") or None,
                timeout_seconds=None,
                context=context,
            )

            runtime = get_sandbox_runtime()
            tool_call.state = ToolState.RUNNING
            tool_call.start_time = self.agent._current_time()
            self._append_research_tool_event(
                tool_call=tool_call,
                context=context,
                event_type="tool.started",
                source="sandbox",
            )
            self._record_tool_runtime(
                tool_call=tool_call,
                context=context,
                status="running",
                phase="sandbox",
                source="sandbox",
            )
            await self._emit_realtime_tool_progress(
                tool_call=tool_call,
                status="running",
            )

            body_entered = True
            advance_tool_pipeline_stage(
                ToolPipelineStage.TOOL_BODY_ENTERED,
                route="sandbox",
            )
            result = await runtime.run_once(spec)
            body_completed = True
            advance_tool_pipeline_stage(
                ToolPipelineStage.TOOL_BODY_COMPLETED,
                route="sandbox",
                outcome=str(result.status or "unknown"),
            )
            tool_call.end_time = self.agent._current_time()

            if result.status == "completed":
                tool_call.state = ToolState.COMPLETED
                tool_call.output = result.stdout
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title=f"{tool_call.tool_name} [sandbox]",
                        output=result.stdout,
                        error=result.stderr if result.stderr else None,
                        metadata={
                            "sandbox_executed": True,
                            "sandbox_job_id": result.sandbox_job_id,
                            "container_id": result.container_id,
                            "container_name": result.container_name,
                            "exit_code": result.exit_code,
                            **dict(result.metadata or {}),
                        },
                    ),
                    context=context,
                    source="sandbox",
                )
            else:
                tool_call.state = ToolState.ERROR
                tool_call.error = f"sandbox {result.status}: {result.stderr or result.stdout}"
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title=f"{tool_call.tool_name} [sandbox]",
                        output=result.stdout,
                        error=f"sandbox {result.status}: {result.stderr or result.stdout}",
                        metadata={
                            "sandbox_executed": True,
                            "sandbox_job_id": result.sandbox_job_id,
                            "container_id": result.container_id,
                            "container_name": result.container_name,
                            "exit_code": result.exit_code,
                            "sandbox_status": result.status,
                            **dict(result.metadata or {}),
                        },
                    ),
                    context=context,
                    source="sandbox",
                )
        except Exception as exc:
            if body_entered and not body_completed:
                advance_tool_pipeline_stage(
                    ToolPipelineStage.TOOL_BODY_COMPLETED,
                    route="sandbox",
                    outcome="error",
                    failure_type=type(exc).__name__,
                )
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            detail = str(exc or "").strip() or "no exception message"
            tool_call.error = (
                "Sandbox execution failed: "
                f"{type(exc).__name__}: {detail}"
            )
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title=f"{tool_call.tool_name} [sandbox]",
                    output="",
                    error=tool_call.error,
                    metadata={
                        "sandbox_executed": False,
                        "sandbox_fail_closed": True,
                        "exception_type": type(exc).__name__,
                    },
                ),
                context=context,
                source="sandbox",
            )

    @staticmethod
    def _extract_tool_command(tool_call: ToolCall) -> str:
        """Extract the shell command from a tool call for sandbox execution."""
        # Only these tools define a shell-command contract. A similarly named
        # field on a read, glue-code or MCP tool is data, not permission to run it.
        if tool_call.tool_name not in {"bash", "exec"}:
            return ""
        args = dict(tool_call.arguments or {})
        cmd = str(args.get("command", "") or "").strip()
        if cmd:
            return cmd
        cmd = str(args.get("cmd", "") or "").strip()
        if cmd:
            return cmd
        return ""

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """通过唯一生产管线执行工具调用。

        参数:
            tool_call: 当前未经隐式参数写回的原始工具调用。

        返回:
            完成参数校验、安全策略、路由、工具体和最终化后的结果。
        """

        call_id = str(tool_call.call_id or "").strip()
        body_task = asyncio.create_task(
            self._pipeline.execute(tool_call, self._execute_pipeline)
        )
        if call_id:
            self._active_calls[call_id] = body_task
        try:
            return await body_task
        finally:
            if call_id and self._active_calls.get(call_id) is body_task:
                self._active_calls.pop(call_id, None)

    async def interrupt_tool_call(
        self,
        tool_call: ToolCall,
        *,
        reason: str,
    ) -> ToolInterruptResult:
        """Interrupt one active call through its declared capability contract.

        The executor owns cancellation of the tool body. Shell tools already
        implement ``CancelledError`` cleanup that terminates their subprocess
        or managed sandbox job, so the hook waits for that cleanup before it
        confirms the interruption to the Harness.
        """

        capability = get_tool_capability(str(tool_call.tool_name or ""))
        behavior = str(capability.interrupt_behavior or "best_effort")
        if behavior == "async_job":
            return ToolInterruptResult(
                outcome="not_supported",
                confirmed_stopped=False,
                side_effect_state="confirmed",
                detail="The external job remains owned by its durable runtime.",
            )
        if behavior != "cancel_process":
            return ToolInterruptResult(
                outcome="not_supported",
                detail=f"interrupt_behavior={behavior}",
            )

        call_id = str(tool_call.call_id or "").strip()
        active = self._active_calls.get(call_id)
        if active is None or active.done():
            return ToolInterruptResult(
                outcome="completed_before_interrupt",
                confirmed_stopped=True,
                side_effect_state="possible",
            )

        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        metadata.update(
            {
                "interrupt_requested": True,
                "interrupt_reason": str(reason or "")[:500],
            }
        )
        tool_call.metadata = metadata
        active.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(active), timeout=10.0)
        except asyncio.CancelledError:
            return ToolInterruptResult(
                outcome="cancelled_side_effect_possible",
                confirmed_stopped=True,
                side_effect_state="possible",
                detail="Tool cleanup completed after cancellation.",
            )
        except asyncio.TimeoutError:
            return ToolInterruptResult(
                outcome="failed",
                confirmed_stopped=False,
                side_effect_state="possible",
                detail="Tool cleanup did not finish within 10 seconds.",
            )
        except Exception as exc:
            return ToolInterruptResult(
                outcome="failed",
                confirmed_stopped=False,
                side_effect_state="possible",
                detail=str(exc)[:500],
            )
        return ToolInterruptResult(
            outcome="completed_before_interrupt",
            confirmed_stopped=True,
            side_effect_state="possible",
        )

    async def _execute_pipeline(self, tool_call: ToolCall) -> ToolResult:
        """按单轨顺序执行工具的全部生产阶段。

        参数:
            tool_call: 当前工具调用，before hook 仍可按旧规则原地更新
            其参数。

        返回:
            Policy、Approval、Sandbox、Builtin 或 MCP 生成的最终结果。
        """

        mcp_tool = None
        mcp_admitted = True
        if tool_call.tool_name.startswith("mcp_"):
            # Resolve exact published definitions, never guess a server/tool
            # from a name or trust an old process-global MCP registration.
            from ..mcp.agent_specific_mcp import AgentSpecificMCPManager
            try:
                mcp_info = (await MCPManager.get_tools()).get(tool_call.tool_name)
            except Exception:
                mcp_info = None
            if mcp_info:
                mcp_tool = MCPTool(
                    server_name=mcp_info["server"], tool_name=mcp_info["tool"],
                    description=mcp_info.get("description", ""),
                    input_schema=mcp_info.get("inputSchema") or {},
                    metadata=mcp_info.get("_meta") or {},
                )
            mcp_admitted = bool(mcp_tool and mcp_tool.id == tool_call.tool_name
                                and AgentSpecificMCPManager()._filter_tools_by_agent(self.agent.agent_info, [mcp_tool]))
        if mcp_admitted and mcp_tool is not None and set(tool_call.arguments or {}) == {"arguments"}:
            self.agent._unwrap_tool_argument_envelope(tool_call, mcp_tool)

        context = self._build_tool_context(tool_call)
        advance_tool_pipeline_stage(ToolPipelineStage.CONTEXT_BUILT)

        argument_parse_error = dict(
            (getattr(tool_call, "metadata", {}) or {}).get(
                "argument_parse_error"
            )
            or {}
        )
        if argument_parse_error:
            advance_tool_pipeline_stage(
                ToolPipelineStage.ARGUMENT_VALIDATION_REJECTED,
                outcome="rejected",
            )
            line = int(argument_parse_error.get("line", 0) or 0)
            column = int(argument_parse_error.get("column", 0) or 0)
            location = (
                f"（第 {line} 行，第 {column} 列）"
                if line and column
                else ""
            )
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            tool_call.error = (
                "模型生成的工具参数不是完整有效的 JSON"
                f"{location}，为避免误执行已阻止本次调用。"
                "请重新生成完整参数；不要复用被截断或缺少分隔符的片段。"
            )
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title=f"{tool_call.tool_name} [参数校验未通过]",
                    output="",
                    error=tool_call.error,
                    metadata={
                        "argument_parse_error": argument_parse_error,
                        "execution_skipped": True,
                        "retryable": True,
                        "terminal_tool_error": False,
                        "should_end_turn": False,
                        "execution_environment": {
                            "location": "not_executed",
                            "label": "Not executed",
                            "sandboxed": False,
                        },
                    },
                ),
                context=context,
                source="llm_argument_validation",
            )

        advance_tool_pipeline_stage(ToolPipelineStage.BEFORE_HOOK_ENTERED)
        await self.agent._apply_plugin_before_tool_execute(tool_call)
        advance_tool_pipeline_stage(ToolPipelineStage.BEFORE_HOOK_COMPLETED)

        local_tool = None if tool_call.tool_name.startswith("mcp_") else ToolRegistry.get(tool_call.tool_name)
        validation = validate_tool_arguments(local_tool or mcp_tool, tool_call.arguments)
        if not validation.accepted:
            advance_tool_pipeline_stage(
                ToolPipelineStage.ARGUMENT_VALIDATION_REJECTED,
                outcome="rejected",
            )
            error = (
                f"工具参数未通过 {validation.validator} 校验："
                f"{validation.location}: {validation.reason}"
            )
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            tool_call.error = error
            if tool_call.tool_name in {"computer_input_action", "computer_type_text", "computer_press_key", "computer_scroll"}:
                from ..tools.builtin.computer import computer_input_argument_rejection

                rejected = computer_input_argument_rejection(tool_call.tool_name)
                rejected.metadata["argument_validation"] = {
                    "validator": validation.validator, "location": validation.location,
                    "accepted": False, "arguments_written_back": False,
                }
                tool_call.error = rejected.error
                return await self._finalize_and_record(
                    tool_call=tool_call, result=rejected, context=context,
                    source="argument-validation",
                )
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title=f"{tool_call.tool_name} [参数校验未通过]",
                    output="",
                    error=error,
                    metadata={
                        "argument_validation": {
                            "validator": validation.validator,
                            "location": validation.location,
                            "accepted": False,
                            "arguments_written_back": False,
                        },
                        "execution_skipped": True,
                        "retryable": True,
                        "terminal_tool_error": False,
                        "should_end_turn": False,
                        "execution_environment": {
                            "location": "not_executed",
                            "label": "Not executed",
                            "sandboxed": False,
                        },
                    },
                ),
                context=context,
                source="argument-validation",
            )
        advance_tool_pipeline_stage(
            ToolPipelineStage.ARGUMENT_VALIDATION_PASSED,
            outcome="skipped" if validation.skipped else "accepted",
        )

        permission_action = self.agent._resolve_permission_action(tool_call.tool_name)
        always_allowed = tool_call.tool_name in getattr(
            self.agent,
            "_always_allowed_tools",
            set(),
        )
        policy = ToolPolicyEngine.evaluate(
            tool_name=tool_call.tool_name,
            context=context,
            execution_profile=getattr(self.agent, "_execution_profile", None),
            permission_action=permission_action,
            native_sandbox_tools=set(_TOOLS_WITH_NATIVE_SANDBOX_HANDLING),
            always_allowed=always_allowed,
            tool_arguments=tool_call.arguments,
            resolved_tool=local_tool or mcp_tool,
        )
        if not mcp_admitted:
            policy = ToolPolicyDecision(
                action="deny", source="mcp-admission",
                reason=f"MCP tool is unavailable or not admitted for this Agent: {tool_call.tool_name}",
                capability=policy.capability, metadata=policy.metadata,
            )
        goal_risk_review = getattr(self.agent, '_goal_risk_reviewer', None)
        if policy.source == 'biosafety-policy' and callable(goal_risk_review):
            await goal_risk_review(tool_call, context)
            # A review is evidence, not permission. Re-run the complete existing
            # policy chain, including explicit denies, sandbox and approvals.
            policy = ToolPolicyEngine.evaluate(
                tool_name=tool_call.tool_name, context=context,
                execution_profile=getattr(self.agent, '_execution_profile', None),
                permission_action=permission_action,
                native_sandbox_tools=set(_TOOLS_WITH_NATIVE_SANDBOX_HANDLING),
                always_allowed=always_allowed, tool_arguments=tool_call.arguments,
                resolved_tool=local_tool or mcp_tool,
            )
        if local_tool is not None and mcp_tool is None and callable((context.extra or {}).get('__goal_checkpoint__')):
            from ..server.goal_tool_review import review_tool_call
            reviewed = await review_tool_call(tool_call, context, policy,
                                             native_sandbox_tools=set(_TOOLS_WITH_NATIVE_SANDBOX_HANDLING))
            if isinstance(reviewed, ToolResult):
                tool_call.state = ToolState.COMPLETED
                tool_call.end_time = self.agent._current_time()
                return await self._finalize_and_record(tool_call=tool_call, result=reviewed,
                                                       context=context, source='human-review')
            policy = reviewed
        advance_tool_pipeline_stage(
            ToolPipelineStage.POLICY_EVALUATED,
            route=policy.action,
        )
        self._set_tool_policy_metadata(tool_call, policy)
        self._record_tool_runtime(
            tool_call=tool_call,
            context=context,
            status="submitted",
            phase="policy",
            source="policy",
            extra={"policy_action": policy.action, "policy_source": policy.source},
        )

        if tool_call.tool_name in getattr(self.agent, "_rate_limited_tools", set()):
            self.agent._emit_agent_hook("tool.rate.limit", {
                "agent": self.agent.agent_info.name,
                "session_id": self.agent.session_id,
                "tool": tool_call.tool_name,
            })
        if policy.action == "deny":
            advance_tool_pipeline_stage(
                ToolPipelineStage.ROUTE_SELECTED,
                route="deny",
                outcome="blocked",
            )
            parent_session_id = str(
                getattr(getattr(self.agent, "_existing_session", None), "parent_id", "")
                or ""
            ).strip()
            if (
                str(tool_call.tool_name or "").strip() == "question"
                and parent_session_id
            ):
                # 被拒绝的子 Agent 提问属于可恢复交互边界，不是执行失败；保留
                # Policy 决定，同时把所需输入交还父任务。
                tool_call.state = ToolState.COMPLETED
                tool_call.end_time = self.agent._current_time()
                metadata = dict(policy.metadata or {})
                metadata.update(
                    {
                        "awaiting_input": True,
                        "waiting_for_user": True,
                        "question_permission_denied": True,
                        "question_payload": dict(tool_call.arguments or {}),
                        "denied_tool": "question",
                        "policy_source": policy.source,
                        "retryable": True,
                        "should_end_turn": True,
                        "termination_scope": "agent",
                        "execution_environment": {
                            "location": "not_executed",
                            "label": "Not executed",
                            "sandboxed": False,
                        },
                    }
                )
                question_text = str((tool_call.arguments or {}).get("question") or "").strip()
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title="Awaiting Parent Input",
                        output=(
                            "当前子任务需要父会话补充输入，但该 Agent 无权直接询问用户。\n"
                            f"- question: {question_text or '未提供具体问题'}\n"
                            "- next_action: 由父 Agent 根据已有上下文回答，或通过前端询问用户后恢复任务。"
                        ),
                        metadata=metadata,
                    ),
                    context=context,
                    source="builtin",
                )
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            tool_call.error = policy.reason
            metadata = dict(policy.metadata or {})
            if metadata.get("goal_action_confirmation_required"):
                notify = (context.extra or {}).get("__goal_authorization_blocked__")
                if callable(notify):
                    notify()
            metadata.update(
                {
                    "retryable": False,
                    # 被拒绝的操作本身不可重试，但 Agent 仍需保留一次推理边界，
                    # 用于说明已完成工作、选择允许的替代方案或在最终回答中报告阻塞。
                    "terminal_tool_error": bool(metadata.get("goal_action_confirmation_required")),
                    "permission_denied": True,
                    "denied_tool": str(tool_call.tool_name or ""),
                    "policy_source": policy.source,
                    "should_end_turn": False,
                    "requires_final_summary": True,
                    "termination_scope": "agent",
                    "execution_environment": {
                        "location": "not_executed",
                        "label": "Not executed",
                        "sandboxed": False,
                    },
                }
            )
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title=tool_call.tool_name,
                    output="",
                    error=policy.reason,
                    metadata=metadata,
                ),
                context=context,
                source=(
                    "sandbox-policy"
                    if policy.source == "sandbox-policy"
                    else "builtin"
                ),
            )

        if policy.action == "ask":
            advance_tool_pipeline_stage(
                ToolPipelineStage.APPROVAL_REQUESTED,
                route="ask",
            )
            approved = await self.agent._ask_tool_permission(
                tool_call.tool_name, tool_call.arguments
            )
            advance_tool_pipeline_stage(
                ToolPipelineStage.APPROVAL_RESOLVED,
                route="ask",
                outcome="approved" if approved else "rejected",
            )
            if not approved:
                tool_call.state = ToolState.ERROR
                tool_call.end_time = self.agent._current_time()
                tool_call.error = (
                    f"Permission rejected for tool: {tool_call.tool_name}"
                )
                metadata = dict(policy.metadata or {})
                metadata.update(
                    {
                        "retryable": False,
                        "terminal_tool_error": False,
                        "permission_denied": True,
                        "denied_tool": str(tool_call.tool_name or ""),
                        "policy_source": policy.source,
                        "should_end_turn": False,
                        "requires_final_summary": True,
                        "termination_scope": "agent",
                        "execution_environment": {
                            "location": "not_executed",
                            "label": "Not executed",
                            "sandboxed": False,
                        },
                    }
                )
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title=tool_call.tool_name,
                        output="",
                        error=f"Permission rejected for tool: {tool_call.tool_name}",
                        metadata=metadata,
                    ),
                    context=context,
                    source="builtin",
                )

            sandbox = policy.sandbox_decision
            route_sandbox = bool(sandbox and sandbox.run_in_sandbox
                                 and tool_call.tool_name not in _TOOLS_WITH_NATIVE_SANDBOX_HANDLING)
            policy = replace(policy, action="route_sandbox" if route_sandbox else "allow")
            self._set_tool_policy_metadata(tool_call, policy)

        # Approval must precede every sandbox body, including generic routing.
        if policy.action == "route_sandbox":
            # Non-command tools retain their own validated execution semantics.
            if self._extract_tool_command(tool_call):
                advance_tool_pipeline_stage(ToolPipelineStage.ROUTE_SELECTED, route="sandbox")
                sandbox_result = await self._execute_in_sandbox(tool_call, context)
                if sandbox_result is not None:
                    return sandbox_result
            policy = replace(policy, action="allow", source="policy",
                             metadata={**policy.metadata, "native_tool_handling": True})
            self._set_tool_policy_metadata(tool_call, policy)

        self.agent._emit_agent_hook("tool.validate.before", {
            "agent": self.agent.agent_info.name,
            "session_id": self.agent.session_id,
            "tool": tool_call.tool_name,
            "call_id": tool_call.call_id,
        })

        tool = local_tool
        if mcp_tool is not None:
            advance_tool_pipeline_stage(ToolPipelineStage.ROUTE_SELECTED, route="mcp")
            return await self._execute_mcp_tool(
                tool_call=tool_call, server_name=mcp_tool.server_name,
                mcp_tool_name=mcp_tool._tool_name, description=mcp_tool.description,
                input_schema=mcp_tool._input_schema, metadata=mcp_tool._metadata,
                context=context,
            )

        if not tool:
            advance_tool_pipeline_stage(
                ToolPipelineStage.ROUTE_SELECTED,
                route="missing",
                outcome="not-found",
            )
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title="",
                    output="",
                    error=f"Tool not found: {tool_call.tool_name}",
                ),
                context=context,
                source="missing",
            )

        advance_tool_pipeline_stage(
            ToolPipelineStage.ROUTE_SELECTED,
            route="builtin",
        )
        body_entered = False
        body_completed = False
        try:
            tool_call.state = ToolState.RUNNING
            tool_call.start_time = self.agent._current_time()
            self._append_research_tool_event(
                tool_call=tool_call,
                context=context,
                event_type="tool.started",
                source="builtin",
            )
            self._record_tool_runtime(
                tool_call=tool_call,
                context=context,
                status="running",
                phase="execute",
                source="builtin",
            )
            await self._emit_realtime_tool_progress(
                tool_call=tool_call,
                status="running",
            )
            timeout_seconds = self._builtin_tool_timeout_seconds(tool_call.tool_name)
            try:
                advance_tool_pipeline_stage(
                    ToolPipelineStage.TOOL_BODY_ENTERED,
                    route="builtin",
                )
                body_entered = True
                if timeout_seconds > 0:
                    result = await asyncio.wait_for(
                        tool.execute(tool_call.arguments, context),
                        timeout=timeout_seconds,
                    )
                else:
                    result = await tool.execute(tool_call.arguments, context)
            except asyncio.TimeoutError as exc:
                inner_timeout_text = _inner_timeout_message(exc)
                timeout_text = inner_timeout_text or (
                    f"Tool execution timed out after {timeout_seconds:g}s: "
                    f"{tool_call.tool_name}"
                )
                tool_call.state = ToolState.ERROR
                tool_call.error = timeout_text
                tool_call.end_time = self.agent._current_time()
                metadata = {
                    "tool_timeout": not bool(inner_timeout_text),
                    "timeout_seconds": timeout_seconds,
                    "terminal_tool_error": True,
                    "retryable": bool(inner_timeout_text),
                    "should_end_turn": True,
                }
                if inner_timeout_text:
                    metadata["upstream_timeout"] = True
                body_completed = True
                advance_tool_pipeline_stage(
                    ToolPipelineStage.TOOL_BODY_COMPLETED,
                    route="builtin",
                    outcome="timeout",
                    failure_type=type(exc).__name__,
                )
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title=tool_call.tool_name,
                        output="",
                        error=timeout_text,
                        metadata=metadata,
                    ),
                    context=context,
                    source="builtin",
                )
            body_completed = True
            advance_tool_pipeline_stage(
                ToolPipelineStage.TOOL_BODY_COMPLETED,
                route="builtin",
                outcome="error" if result.error else "completed",
            )
            if result.error:
                tool_call.state = ToolState.ERROR
                tool_call.error = str(result.error)
            else:
                tool_call.state = ToolState.COMPLETED
            tool_call.output = result.output
            tool_call.end_time = self.agent._current_time()
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=result,
                context=context,
                source="builtin",
            )
        except Exception as exc:
            if body_entered and not body_completed:
                advance_tool_pipeline_stage(
                    ToolPipelineStage.TOOL_BODY_COMPLETED,
                    route="builtin",
                    outcome="error",
                    failure_type=type(exc).__name__,
                )
            inner_timeout_text = _inner_timeout_message(exc)
            tool_call.state = ToolState.ERROR
            tool_call.error = inner_timeout_text or str(exc)
            tool_call.end_time = self.agent._current_time()
            metadata = None
            if inner_timeout_text:
                metadata = {
                    "tool_timeout": False,
                    "upstream_timeout": True,
                    "terminal_tool_error": True,
                    "retryable": True,
                    "should_end_turn": True,
                }
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title="",
                    output="",
                    error=inner_timeout_text or str(exc),
                    metadata=metadata,
                ),
                context=context,
                source="builtin",
            )

    async def _execute_mcp_tool(
        self,
        *,
        tool_call: ToolCall,
        server_name: str,
        mcp_tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        metadata: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        # Approval can await user input. Re-read admission/schema at the final
        # execution boundary instead of trusting the pre-approval snapshot.
        from ..mcp.agent_specific_mcp import AgentSpecificMCPManager
        try:
            info = (await MCPManager.get_tools()).get(tool_call.tool_name)
        except Exception:
            info = None
        current = (MCPTool(info["server"], info["tool"], info.get("description", ""),
                           info.get("inputSchema") or {}, info.get("_meta") or {}) if info else None)
        rejection = ""
        if (current is None or current.id != tool_call.tool_name
                or not AgentSpecificMCPManager()._filter_tools_by_agent(self.agent.agent_info, [current])):
            rejection = "MCP tool admission changed or the tool is unavailable."
        else:
            validation = validate_tool_arguments(current, tool_call.arguments)
            if not validation.accepted:
                rejection = f"MCP argument validation failed: {validation.location}: {validation.reason}"
        if rejection:
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            tool_call.error = rejection
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(title=tool_call.tool_name, output="", error=rejection,
                                  metadata={"execution_skipped": True, "retryable": False}),
                context=context, source="mcp-admission",
            )
        server_name, mcp_tool_name = current.server_name, current._tool_name
        body_entered = False
        body_completed = False
        try:
            tool_call.state = ToolState.RUNNING
            tool_call.start_time = self.agent._current_time()
            self._append_research_tool_event(
                tool_call=tool_call,
                context=context,
                event_type="mcp.called",
                source="mcp",
                mcp_server=server_name,
                mcp_tool_name=mcp_tool_name,
            )
            self._record_tool_runtime(
                tool_call=tool_call,
                context=context,
                status="running",
                phase="mcp",
                source="mcp",
                mcp_server=server_name,
                mcp_tool_name=mcp_tool_name,
            )
            await self._emit_realtime_tool_progress(
                tool_call=tool_call,
                status="running",
            )

            # Reuse the freshly admitted adapter. Its host schema already has
            # dynamic context; constructing it again would treat that as a
            # remote reserved-field collision. Keep the raw-schema guard intact.
            mcp_tool = current
            advance_tool_pipeline_stage(
                ToolPipelineStage.TOOL_BODY_ENTERED,
                route="mcp",
            )
            body_entered = True
            result = await mcp_tool.execute(tool_call.arguments, context)
            body_completed = True
            advance_tool_pipeline_stage(
                ToolPipelineStage.TOOL_BODY_COMPLETED,
                route="mcp",
                outcome="error" if result.error else "completed",
            )

            tool_call.state = ToolState.COMPLETED
            tool_call.end_time = self.agent._current_time()

            if result.error:
                error_msg = str(result.error)
                tool_call.state = ToolState.ERROR
                tool_call.error = error_msg
                return await self._finalize_and_record(
                    tool_call=tool_call,
                    result=ToolResult(
                        title=tool_call.tool_name,
                        output="",
                        error=error_msg,
                        metadata=dict(result.metadata or {}),
                    ),
                    context=context,
                    source="mcp",
                    mcp_server=server_name,
                    mcp_tool_name=mcp_tool_name,
                )

            tool_call.output = result.output
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=result,
                context=context,
                source="mcp",
                mcp_server=server_name,
                mcp_tool_name=mcp_tool_name,
            )
        except Exception as exc:
            if body_entered and not body_completed:
                advance_tool_pipeline_stage(
                    ToolPipelineStage.TOOL_BODY_COMPLETED,
                    route="mcp",
                    outcome="error",
                    failure_type=type(exc).__name__,
                )
            tool_call.state = ToolState.ERROR
            tool_call.end_time = self.agent._current_time()
            tool_call.error = str(exc)
            return await self._finalize_and_record(
                tool_call=tool_call,
                result=ToolResult(
                    title=tool_call.tool_name,
                    output="",
                    error=str(exc),
                ),
                context=context,
                source="mcp",
                mcp_server=server_name,
                mcp_tool_name=mcp_tool_name,
            )
