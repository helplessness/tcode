"""Task tools for synchronous and background sub-agent execution.

The default remains synchronous for backward compatibility. Supervisor calls may
opt into background execution with ``wait=false`` and collect results through
``task_wait``.
"""

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...agent.agents import BuiltinAgents
from ...governance.biosafety import biosafety_scope_hash, evaluate_worker_dispatch
from ...mcp.route_permissions import _agents_allowed_to_route_server
from ...runtime import RuntimeArtifactManager
from ...runtime.question_broker import build_parent_question_bridge
from ...session.manager import SessionManager
from ...subtask_handoff import (
    build_subtask_handoff,
    build_subtask_tool_trace,
    compact_subtask_tool_metadata,
    completed_subtask_work,
    json_safe_handoff_value,
    normalize_subtask_outcome,
    validate_async_submission_claim,
)
from ...team.registry import WorkerRegistry
from ...providers import create_provider, clone_provider
from ...workspace import AgentWorkspaceManager
from ...workflow.protein_design import resolve_approved_protein_design_brief
from ...workflow.router import is_protein_design_brainstorm_mode


def protein_design_task_gate_error(context: ToolContext) -> str:
    """Return a blocking reason when protein-design task execution must wait for brainstorm approval."""

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


class TaskParameters(ParameterSchema):
    """Parameters for task tool"""

    description: str
    prompt: str
    subagent_type: str | None = None
    task_id: str | None = None
    goal: str | None = None
    deliverable: str | None = None
    stop_when: str | None = None
    allowed_tools: str | list[str] | None = None
    swarm_peer_communication: bool | None = None
    swarm_communication_rounds: int | None = None
    wait: bool = True


class TaskWaitParameters(ParameterSchema):
    task_ids: str | list[str]
    timeout_seconds: float = 0


_BACKGROUND_SUBAGENT_TASKS: dict[str, dict[str, Any]] = {}


class TaskTool(Tool):
    _MCP_SERVER_PATTERN = re.compile(
        r"(?i)(?<![A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]*-mcp)"
        r"(?![A-Za-z0-9_-])"
    )

    @staticmethod
    def _control_workdir(context: Optional[ToolContext]) -> Optional[str]:
        extra = context.extra if context and isinstance(context.extra, dict) else {}
        value = str(
            extra.get("side_chat_workdir")
            or extra.get("control_workdir")
            or extra.get("workdir")
            or os.environ.get("TCODE_WORKDIR")
            or ""
        ).strip()
        return value or None

    def _emit_task_hook(self, event: str, context, data: dict[str, Any]) -> None:
        """Emit a task-level plugin hook (fire-and-forget)."""
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_running_loop()

            async def _emit():
                try:
                    from ...plugins.manager import PluginManager
                    workdir = str((context.extra or {}).get("workdir", "") or "").strip() if context and hasattr(context, 'extra') else ""
                    manager = await PluginManager.get_global(workdir or os.getcwd())
                    await manager.trigger(event, data, {})
                except Exception:
                    pass

            loop.create_task(_emit())
        except Exception:
            pass

    """Launch a sub-agent synchronously by default, or in the background on request.

    1. Creates a new session for the subagent
    2. Runs the subagent with the prompt
    3. Waits for completion
    4. Returns the result directly (includes <task_result> and task_id)

    You can pass task_id to resume a previous task session.
    """

    @property
    def id(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        agents = BuiltinAgents.list()
        accessible = [a for a in agents if a.mode.value != "primary"]
        accessible.sort(key=lambda item: (item.name != "browser", item.name))

        agent_list = "\n".join(
            [
                (
                    f"- **agent-browser** (browser): {a.description}"
                    if a.name == "browser"
                    else f"- **{a.name}**: {a.description or 'Subagent for specific tasks'}"
                )
                for a in accessible[:8]
            ]
        )

        return f"""Launch a subagent to handle a task. The agent will run and return results directly.

            Available sub-agents:
            {agent_list}

            Parameters:
            - description: A short description of the task
            - prompt: The task for the agent to perform  
            - subagent_type: Which subagent to use (default: general)
            - task_id: Optional - resume a previous task session instead of creating new one
            - goal: Optional - concise goal contract for the subagent
            - deliverable: Optional - required deliverable or result format
            - stop_when: Optional - explicit stopping condition
            - allowed_tools: Optional - "all" or exact registered tool IDs. Unknown
              names are rejected. Skill actions are not standalone tools; allow `skill`
              and its declared execution dependency (`skill_run`).
              For third-party packages, arbitrary Python, or long-running/background
              computation, allow both `exec` and `process`; `execute_code` alone is only
              a restricted read-only glue environment without imports or asyncio.
            - swarm_peer_communication: Optional - enable/disable inter-agent communication in swarm mode
            - swarm_communication_rounds: Optional - communication rounds in swarm mode (default: 1, max: 3)
            - wait: Optional - true waits for completion (default); false starts a
              background subagent in supervisor mode. Use task_wait with the
              returned task_id to inspect or collect its result.

            If an existing subagent needs correction, continuation, or a new
            direction, use subagent_guide instead of launching a duplicate task.

            Browser rule: real browser work must use this task tool with
            subagent_type="agent-browser". The parent agent must not call the browser tool directly.

            Returns: task_id (for resuming) and <task_result> (the agent's response)"""

    @property
    def parameters(self) -> type:
        return TaskParameters

    def _resolve_scheduler_mode(self, context: ToolContext) -> str:
        mode = "supervisor"
        if context.extra:
            mode = str(context.extra.get("scheduler_mode") or "supervisor")
        mode = mode.strip().lower()
        if mode not in {"supervisor", "chain", "swarm", "auto_swarm_builder"}:
            return "supervisor"
        return mode

    def _resolve_swarm_topology(self, context: Optional[ToolContext]) -> str:
        topology = "concurrent"
        if context and context.extra:
            topology = str(context.extra.get("swarm_topology") or "concurrent")
        topology = topology.strip().lower()
        if topology not in {"concurrent", "sequential", "voting", "rearrange"}:
            return "concurrent"
        return topology

    def _resolve_swarm_peer_communication(
        self,
        context: Optional[ToolContext],
        parsed: Optional[TaskParameters] = None,
    ) -> bool:
        if parsed and parsed.swarm_peer_communication is not None:
            return bool(parsed.swarm_peer_communication)

        if context and context.extra and isinstance(
            context.extra.get("swarm_peer_communication"), bool
        ):
            return bool(context.extra.get("swarm_peer_communication"))

        value = os.environ.get("TCODE_SWARM_PEER_COMMUNICATION", "1").strip().lower()
        return value not in {"0", "false", "off", "no"}

    def _resolve_swarm_communication_rounds(
        self,
        context: Optional[ToolContext],
        parsed: Optional[TaskParameters] = None,
    ) -> int:
        raw: Any = 1
        if parsed and parsed.swarm_communication_rounds is not None:
            raw = parsed.swarm_communication_rounds
        elif context and context.extra and context.extra.get("swarm_communication_rounds") is not None:
            raw = context.extra.get("swarm_communication_rounds")
        else:
            raw = os.environ.get("TCODE_SWARM_COMMUNICATION_ROUNDS", "1")

        try:
            value = int(raw)
        except Exception:
            value = 1
        return max(0, min(3, value))

    def _is_explicit_external_swarms(self, context: Optional[ToolContext]) -> bool:
        return bool(
            context
            and context.extra
            and isinstance(context.extra.get("use_swarms"), bool)
            and context.extra.get("use_swarms") is True
        )

    def _use_swarms_enabled(self, context: Optional[ToolContext]) -> bool:
        if context and context.extra and isinstance(context.extra.get("use_swarms"), bool):
            return bool(context.extra.get("use_swarms"))
        use_swarms = os.environ.get("TCODE_USE_SWARMS", "1").strip().lower()
        return use_swarms not in {"0", "false", "off", "no"}

    def _subagent_turn_timeout_seconds(self) -> float:
        try:
            raw = float(
                os.environ.get("TCODE_SUBAGENT_TURN_TIMEOUT_SECONDS", "1800")
            )
        except Exception:
            raw = 1800.0
        if raw <= 0:
            return 0.0
        return max(1.0, min(raw, 24 * 3600.0))

    def _subagent_provider_close_timeout_seconds(self) -> float:
        try:
            raw = float(
                os.environ.get("TCODE_SUBAGENT_PROVIDER_CLOSE_TIMEOUT_SECONDS", "5")
            )
        except Exception:
            raw = 5.0
        if raw <= 0:
            return 0.0
        return max(0.1, min(raw, 60.0))

    async def _close_subagent_provider(self, provider: Any) -> None:
        close = getattr(provider, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if not hasattr(result, "__await__"):
                return
            timeout = self._subagent_provider_close_timeout_seconds()
            if timeout > 0:
                await asyncio.wait_for(result, timeout=timeout)
            else:
                await result
        except Exception:
            pass

    async def _emit_agent_event(self, context: ToolContext, payload: dict[str, Any]):
        if not context.extra:
            return
        emitter = context.extra.get("__agent_event_emit__")
        if not emitter:
            return
        try:
            result = emitter(payload)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            pass

    async def _forward_subagent_browser_session_event(
        self,
        *,
        event_context: ToolContext,
        session: Any,
        payload: dict[str, Any],
    ) -> None:
        """Forward a child's live-browser lifecycle into the parent event stream."""
        event = dict(payload or {})
        if str(event.get("kind") or "").strip().lower() != "browser_session":
            return
        event.setdefault(
            "parent_session_id",
            str(
                getattr(session, "parent_id", "")
                or getattr(event_context, "session_id", "")
                or ""
            ),
        )
        event.setdefault("session_id", str(getattr(session, "id", "") or ""))
        event.setdefault(
            "workspace_id",
            str(getattr(session, "workspace_id", "") or ""),
        )
        await self._emit_agent_event(event_context, event)

    async def _session_owner_kwargs(
        self,
        context: Optional[ToolContext],
        *,
        include_workspace: bool = True,
    ) -> dict[str, str]:
        extra = context.extra if context and isinstance(context.extra, dict) else {}
        tenant_id = str(extra.get("tenant_id") or "").strip()
        user_id = str(extra.get("user_id") or "").strip()
        workspace_id = str(extra.get("workspace_id") or "").strip()
        enterprise_mode = str(
            os.environ.get("TCODE_ENTERPRISE_MODE", "") or ""
        ).strip().lower() not in {"", "0", "false", "no", "off"}

        if context and (not tenant_id or not user_id or (include_workspace and not workspace_id)):
            if enterprise_mode:
                raise ValueError(
                    "enterprise task context requires tenant_id, user_id and workspace_id"
                )
            try:
                lookup_owner = {
                    key: value
                    for key, value in {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "workspace_id": workspace_id if include_workspace else "",
                    }.items()
                    if value
                }
                parent = await SessionManager.get(context.session_id, **lookup_owner)
            except Exception:
                parent = None
            if parent is not None:
                tenant_id = tenant_id or str(getattr(parent, "tenant_id", "") or "").strip()
                user_id = user_id or str(getattr(parent, "user_id", "") or "").strip()
                workspace_id = workspace_id or str(getattr(parent, "workspace_id", "") or "").strip()

        payload: dict[str, str] = {}
        if tenant_id:
            payload["tenant_id"] = tenant_id
        if user_id:
            payload["user_id"] = user_id
        if include_workspace and workspace_id:
            payload["workspace_id"] = workspace_id
        return payload

    def _subagent_display_name(
        self,
        index: int,
        total: int,
        agent_name: Optional[str] = None,
    ) -> str:
        if agent_name:
            info = BuiltinAgents.get(agent_name)
            if info:
                options = info.options if isinstance(info.options, dict) else {}
                explicit_display = str(
                    options.get("display_name")
                    or options.get("title")
                    or ""
                ).strip()
                if explicit_display:
                    return explicit_display

                configured_name = str(info.name or "").strip()
                if configured_name:
                    return configured_name

            raw_name = str(agent_name).strip()
            if raw_name:
                return raw_name

        base_roles = [
            "规划师",
            "行动师",
            "分析师",
            "审阅师",
            "整合师",
            "执行师",
        ]
        if index < len(base_roles):
            return base_roles[index]
        return f"协作师{index + 1}"

    def _extract_tools_used(self, messages: list[Any]) -> list[str]:
        tool_names: list[str] = []
        seen: set[str] = set()
        for msg in messages or []:
            for tc in (getattr(msg, "tool_calls", None) or []):
                name = str(getattr(tc, "tool_name", "") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                tool_names.append(name)
        return tool_names

    def _truthy(self, value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}

    def _object_value(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {}

    def _text_value(self, *values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _json_safe_value(self, value: Any, *, depth: int = 0) -> Any:
        return json_safe_handoff_value(value, depth=depth)

    def _compact_tool_metadata(self, metadata: Any) -> dict[str, Any]:
        return compact_subtask_tool_metadata(metadata)

    def _build_tool_trace(self, messages: list[Any]) -> list[dict[str, Any]]:
        return build_subtask_tool_trace(messages)

    def _metadata_has_sandbox_signal(self, metadata: dict[str, Any]) -> bool:
        execution = self._object_value(metadata.get("execution_environment"))
        sandbox_meta = self._object_value(metadata.get("sandbox"))
        location = str(execution.get("location") or "").strip().lower()
        if location == "sandbox":
            return True
        if self._truthy(execution.get("sandboxed")):
            return True
        for value in (
            metadata.get("sandbox_executed"),
            metadata.get("sandbox_worker"),
            sandbox_meta.get("enabled"),
            sandbox_meta.get("sandbox_worker"),
        ):
            if self._truthy(value):
                return True
        return bool(
            self._text_value(
                metadata.get("sandbox_job_id"),
                metadata.get("container_id"),
                metadata.get("container_name"),
                sandbox_meta.get("sandbox_job_id"),
                sandbox_meta.get("container_id"),
                sandbox_meta.get("container_name"),
                execution.get("sandbox_job_id"),
                execution.get("container_id"),
                execution.get("container_name"),
            )
        )

    def _extract_delegated_execution_environment(
        self,
        tool_trace: list[dict[str, Any]],
        *,
        session_id: str = "",
        agent_name: str = "",
    ) -> dict[str, Any]:
        sandbox_items: list[dict[str, Any]] = []
        for item in tool_trace or []:
            metadata = self._object_value(item.get("metadata"))
            if not metadata or not self._metadata_has_sandbox_signal(metadata):
                continue
            execution = self._object_value(metadata.get("execution_environment"))
            sandbox_meta = self._object_value(metadata.get("sandbox"))
            env = dict(execution)
            env["location"] = "sandbox"
            env["sandboxed"] = True
            env["delegated"] = True
            env["control_plane_location"] = "host"
            env["source_tool_name"] = str(item.get("tool_name") or "").strip()
            env["source_call_id"] = str(item.get("call_id") or "").strip()
            if session_id:
                env["source_session_id"] = session_id
            if agent_name:
                env["source_agent_name"] = agent_name
            for key, values in {
                "sandbox_tool": (metadata.get("sandbox_tool"), metadata.get("sandbox_tool_name"), metadata.get("runtime_tool")),
                "runner_url": (metadata.get("sandbox_runner_url"), sandbox_meta.get("runner_url")),
                "image": (metadata.get("sandbox_image"), sandbox_meta.get("image")),
                "runtime": (metadata.get("sandbox_runtime"), metadata.get("runtime"), sandbox_meta.get("runtime")),
                "backend": (metadata.get("sandbox_backend"), metadata.get("backend"), sandbox_meta.get("backend")),
                "network": (metadata.get("sandbox_network"), metadata.get("network_policy"), sandbox_meta.get("network")),
                "container_id": (metadata.get("container_id"), sandbox_meta.get("container_id")),
                "container_name": (metadata.get("container_name"), sandbox_meta.get("container_name")),
                "sandbox_job_id": (metadata.get("sandbox_job_id"), sandbox_meta.get("sandbox_job_id")),
                "status": (metadata.get("sandbox_status"), metadata.get("status"), sandbox_meta.get("status")),
                "exit_code": (metadata.get("exit_code"), metadata.get("returncode"), metadata.get("exitCode"), sandbox_meta.get("exit_code")),
                "container_cwd": (metadata.get("container_cwd"),),
                "profile": (metadata.get("sandbox_profile"), sandbox_meta.get("sandbox_profile")),
                "worker_input": (metadata.get("worker_input"), sandbox_meta.get("input_path")),
                "worker_output": (metadata.get("worker_output"), sandbox_meta.get("output_path")),
            }.items():
                if key not in env or not self._text_value(env.get(key)):
                    text = self._text_value(*values)
                    if text:
                        env[key] = text
            sandbox_items.append(self._json_safe_value(env))

        if not sandbox_items:
            return {}
        primary = dict(sandbox_items[-1])
        primary["sandbox_tool_count"] = len(sandbox_items)
        primary["sandbox_tools"] = [
            {
                "tool_name": item.get("source_tool_name", ""),
                "sandbox_job_id": item.get("sandbox_job_id", ""),
                "container_name": item.get("container_name", ""),
            }
            for item in sandbox_items
        ]
        return primary

    def _task_metadata_with_runtime(
        self,
        metadata: dict[str, Any],
        delegated_environment: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        env = self._object_value(delegated_environment)
        if not env:
            return metadata
        return {
            **metadata,
            "delegated_execution": True,
            "sandbox_executed": True,
            "delegated_execution_environment": env,
            "child_execution_environment": env,
        }

    def _trim_text(self, text: str, max_len: int = 1000) -> str:
        value = str(text or "")
        if len(value) <= max_len:
            return value
        return value[:max_len] + "..."

    @staticmethod
    def _large_result_threshold_chars() -> int:
        try:
            raw = int(os.environ.get("TCODE_TASK_INLINE_RESULT_MAX_CHARS", "12000"))
        except Exception:
            raw = 12000
        return max(2000, min(raw, 200000))

    @staticmethod
    def _large_result_preview_chars() -> int:
        try:
            raw = int(os.environ.get("TCODE_TASK_RESULT_PREVIEW_CHARS", "4000"))
        except Exception:
            raw = 4000
        return max(500, min(raw, 20000))

    @staticmethod
    def _normalize_subagent_outcome_status(raw_status: Any) -> tuple[str, bool]:
        return normalize_subtask_outcome(raw_status)

    def _completed_subtask_work(
        self,
        tool_trace: list[dict[str, Any]],
        *,
        max_items: int = 8,
    ) -> list[dict[str, Any]]:
        """Return bounded, de-duplicated completed work for parent recovery."""
        return completed_subtask_work(tool_trace, max_items=max_items)

    @staticmethod
    def _clean_subtask_blocker(result_content: str) -> str:
        lines = str(result_content or "").strip().splitlines()
        while lines and lines[0].strip() in {"当前任务未完成。", "当前任务未完成"}:
            lines.pop(0)
        return "\n".join(lines).strip()

    def _build_recoverable_subtask_handoff(
        self,
        *,
        result_content: str,
        tool_trace: list[dict[str, Any]],
        outcome_status: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Expose child progress and blocker without terminating the parent turn."""
        handoff = build_subtask_handoff(
            result_text=result_content,
            tool_trace=tool_trace,
            child_outcome_status=outcome_status,
        )
        return handoff.render_for_parent(), handoff.completed_work

    def _format_task_result_for_parent(
        self,
        result_content: str,
        artifact_paths: dict[str, str],
    ) -> tuple[str, dict[str, Any]]:
        content = str(result_content or "")
        full_result_path = str(artifact_paths.get("full_result_path") or "").strip()
        threshold = self._large_result_threshold_chars()
        if len(content) <= threshold or not full_result_path:
            return content, {
                "large_result": False,
                "result_chars": len(content),
                "full_result_path": full_result_path,
            }
        preview = self._trim_text(content, self._large_result_preview_chars())
        return (
            "大型结果已自动保存，聊天中仅显示预览。\n"
            f"- full_result_path: {full_result_path}\n"
            f"- result_chars: {len(content)}\n"
            "- preview:\n"
            f"{preview}",
            {
                "large_result": True,
                "result_chars": len(content),
                "full_result_path": full_result_path,
                "preview_chars": len(preview),
            },
        )

    @staticmethod
    def _declared_full_result_path(result_content: str) -> str:
        match = re.search(
            r"full_result_path[\"']?\s*[:=]\s*[`\"']?([^\s`\"',}\]]+)",
            str(result_content or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        candidate = Path(match.group(1)).expanduser()
        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            return ""
        return str(resolved) if resolved.is_file() else ""

    def _large_deliverable_prompt(
        self,
        prompt: str,
        *,
        context: ToolContext,
        session_id: str,
    ) -> str:
        text = str(prompt or "")
        likely_large = len(text) >= 8000 or bool(
            re.search(
                r"(?:严格\s*JSON|strict\s+json|所有(?:记录|条目|数据)|每条\s*record|全量(?:抽取|导出))",
                text,
                flags=re.IGNORECASE,
            )
        )
        if not likely_large:
            return text
        workdir = context.extra.get("workdir") if context.extra else None
        task_dir = RuntimeArtifactManager.task_dir(context.session_id, session_id, workdir)
        suffix = ".json" if re.search(r"\bjson\b|JSON", text) else ".md"
        deliverable_path = (task_dir / f"agent_deliverable{suffix}").resolve()
        policy = (
            "# Large Deliverable Policy\n"
            "本任务预计会产生大型结果。不要把完整大型结果一次性塞进最终聊天回复。\n"
            "如果完整结果可能超过 12000 字符，并且你有 write/bash 等本地写入能力：\n"
            f"1. 将完整结果写入 `{deliverable_path}`；结构化数据必须保持完整、有效。\n"
            "2. 优先用确定性本地脚本分批构造文件，避免在一次 LLM 回复或单个工具参数中生成全部重复记录。\n"
            "3. 最终回复不超过 4000 字符，只返回完成状态、记录数量、少量预览和 `full_result_path`。\n"
            "4. 这条大型交付策略优先于“把完整结果直接输出到聊天”的格式要求；完整格式要求应用于文件内容。\n"
        )
        return f"{text}\n\n{policy}"

    def _normalize_allowed_tools(self, allowed_tools: Any) -> str | list[str]:
        from ..registry import ToolRegistry

        return ToolRegistry.normalize_allowlist(allowed_tools)

    @staticmethod
    def _unknown_allowed_tools(allowed_tools: str | list[str]) -> list[str]:
        if allowed_tools == "all":
            return []
        from ..registry import ToolRegistry

        return ToolRegistry.unknown_allowlist_ids(allowed_tools)

    @staticmethod
    def _invalid_allowlist_result(
        *,
        title: str,
        unknown_tools: list[str],
        resolved_tools: list[str],
    ) -> ToolResult:
        unknown_text = ", ".join(unknown_tools)
        return ToolResult(
            title=title or "Task",
            output="",
            error=(
                f"Unknown allowed_tools entries: {unknown_text}. "
                "Use exact registered tool IDs; names of scripts or skill actions are "
                "not standalone tools. To run an executable skill, allow `skill` and "
                "`skill_run`, load the skill, then invoke its declared action through "
                "`skill_run`."
            ),
            metadata={
                "invalid_allowed_tools": unknown_tools,
                "resolved_allowed_tools": resolved_tools,
                "retryable": True,
            },
        )

    def _build_contract(self, parsed: TaskParameters) -> dict[str, Any]:
        goal = str(parsed.goal or parsed.description or "").strip()
        deliverable = str(parsed.deliverable or "给出完成任务所需的最终结果。").strip()
        stop_when = str(parsed.stop_when or "当 deliverable 已完成且无需继续探索时停止。").strip()
        allowed_tools = self._normalize_allowed_tools(parsed.allowed_tools)
        return {
            "goal": goal,
            "deliverable": deliverable,
            "stop_when": stop_when,
            "allowed_tools": allowed_tools,
        }

    def _normalize_resource_context(self, raw: Any) -> dict[str, Any]:
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

    def _serialize_resource_context(self, resource_context: dict[str, Any]) -> str:
        if not resource_context:
            return ""
        lines: list[str] = []
        uploaded_files = resource_context.get("uploaded_files")
        if isinstance(uploaded_files, list) and uploaded_files:
            lines.append("uploaded_files:")
            for item in uploaded_files[:12]:
                if not isinstance(item, dict):
                    continue
                path = str(
                    item.get("container_path")
                    or item.get("relative_path")
                    or item.get("path")
                    or ""
                ).strip()
                if not path:
                    continue
                kind = str(item.get("kind") or "file").strip() or "file"
                lines.append(f"- {path} ({kind})")
        paper_inputs = resource_context.get("paper_inputs")
        if isinstance(paper_inputs, dict) and int(paper_inputs.get("input_count", 0) or 0) > 0:
            lines.append(
                f"paper_inputs: input_count={int(paper_inputs.get('input_count', 0) or 0)}, "
                f"pdf_count={int(paper_inputs.get('pdf_count', 0) or 0)}"
            )
            for item in list(paper_inputs.get("inputs") or [])[:12]:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                kind = str(item.get("kind") or "other").strip() or "other"
                lines.append(f"  - {path} ({kind})")
        for key, value in resource_context.items():
            if key in {"uploaded_files", "paper_inputs"}:
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

    def _build_contract_prompt(
        self,
        contract: dict[str, Any],
        prompt: str,
        resource_context: Optional[dict[str, Any]] = None,
    ) -> str:
        allowed_tools = contract.get("allowed_tools", "all")
        tool_text = "all" if allowed_tools == "all" else ", ".join(allowed_tools)
        resource_text = self._serialize_resource_context(dict(resource_context or {}))
        prompt_text = (
            "# Task Contract\n"
            f"- goal: {contract.get('goal', '')}\n"
            f"- deliverable: {contract.get('deliverable', '')}\n"
            f"- stop_when: {contract.get('stop_when', '')}\n"
            f"- allowed_tools: {tool_text}\n\n"
        )
        if resource_text:
            prompt_text += "# Resource Context\n" f"{resource_text}\n\n"
        prompt_text += "# Original Task\n" f"{prompt}"
        return (
            prompt_text
        )

    def _is_strict_input_contract_prompt(self, prompt: str) -> bool:
        text = str(prompt or "").strip()
        if not text.startswith("INPUT_CONTRACT:"):
            return False
        remainder = text[len("INPUT_CONTRACT:"):].strip()
        return remainder.startswith("```json") or remainder.startswith("{")

    def _apply_allowed_tools_to_agent(self, agent_info, allowed_tools: str | list[str]):
        if allowed_tools == "all":
            return agent_info
        permission = {"*": "deny"}
        for tool_name in allowed_tools:
            permission[str(tool_name)] = "allow"
        return replace(agent_info, permission=permission)

    def _build_context_snapshot(self, messages: list[Any]) -> dict[str, Any]:
        tool_call_count = 0
        last_user = ""
        last_assistant = ""

        for msg in messages or []:
            role = str(getattr(msg, "role", "") or "")
            content = str(getattr(msg, "content", "") or "")
            if role == "user" and content.strip():
                last_user = content
            elif role == "assistant" and content.strip():
                last_assistant = content

            tool_call_count += len(getattr(msg, "tool_calls", None) or [])

        return {
            "message_count": len(messages or []),
            "tool_call_count": tool_call_count,
            "last_user": self._trim_text(last_user, 240),
            "last_assistant": self._trim_text(last_assistant, 240),
        }

    def _resolve_shared_memory_key(self, context: Optional[ToolContext]) -> str:
        if not context or not context.extra:
            return ""
        for key in ("shared_memory_key", "worker_memory_key"):
            value = str(context.extra.get(key) or "").strip()
            if value:
                return value
        return ""

    def _resolve_task_child_policy(self, context: Optional[ToolContext]) -> str:
        if context and context.extra:
            value = str(context.extra.get("task_child_policy") or "").strip().lower()
            if value in {"persistent", "ephemeral"}:
                return value
            if bool(context.extra.get("resident_generalist")) and self._resolve_shared_memory_key(context):
                return "ephemeral"
        return "persistent"

    def _uses_ephemeral_helpers(self, context: Optional[ToolContext]) -> bool:
        return self._resolve_task_child_policy(context) == "ephemeral"

    def _build_subagent_metadata(
        self,
        *,
        context: ToolContext,
        contract: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        contract_payload = dict(contract or {})
        metadata: dict[str, Any] = {"contract": contract_payload}
        shared_memory_key = self._resolve_shared_memory_key(context)
        if shared_memory_key:
            metadata["shared_memory_key"] = shared_memory_key
            metadata["shared_memory_source"] = str(
                (context.extra or {}).get("shared_memory_source") or ""
            ).strip()
        metadata["task_child_policy"] = self._resolve_task_child_policy(context)
        resource_context = self._normalize_resource_context(
            (context.extra or {}).get("resource_context") if context.extra else {}
        )
        if resource_context:
            metadata["resource_context"] = resource_context
        context_extra = dict(context.extra or {}) if isinstance(context.extra, dict) else {}
        for key in (
            "side_chat_workdir",
            "request_denied_tools",
            "config_snapshot_id",
            "config_snapshot_workdir",
            "config_fingerprint",
            "config_policy_fingerprint",
            "config_summary",
        ):
            if key in context_extra:
                metadata[key] = context_extra[key]
        control_workdir = self._control_workdir(context)
        if control_workdir:
            metadata["control_workdir"] = control_workdir
        parent_execution_workdir = str(context_extra.get("workdir") or "").strip()
        if parent_execution_workdir:
            metadata["parent_execution_workdir"] = parent_execution_workdir
        parent_turn_id = str(context_extra.get("turn_id") or "").strip()
        parent_trace_id = str(context_extra.get("trace_id") or "").strip()
        if parent_turn_id or parent_trace_id:
            metadata["research_parent_context"] = {
                "session_id": str(context.session_id or ""),
                "turn_id": parent_turn_id,
                "trace_id": parent_trace_id,
                "tool_call_id": str(context.call_id or ""),
                "agent_name": str(context.agent or ""),
            }
        scope_summary = str(
            contract_payload.get("goal")
            or contract_payload.get("deliverable")
            or contract_payload.get("title")
            or ""
        ).strip()[:1200]
        fingerprint_seed = json.dumps(
            {
                "parent_session_id": str(context.session_id or ""),
                "parent_turn_id": parent_turn_id,
                "contract_id": str(contract_payload.get("contract_id") or ""),
                "contract_version": int(contract_payload.get("version") or 0),
                "origin_tool_call_id": str(context.call_id or ""),
                "scope_summary": scope_summary,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_paths: list[str] = []
        for deliverable in list(contract_payload.get("deliverables") or []):
            if not isinstance(deliverable, dict):
                continue
            path = str(deliverable.get("path") or deliverable.get("artifact_path") or "").strip()
            if path and path not in artifact_paths:
                artifact_paths.append(path)
        metadata["delegation_scope"] = {
            "parent_session_id": str(context.session_id or ""),
            "parent_turn_id": parent_turn_id,
            "parent_contract_id": str(contract_payload.get("contract_id") or ""),
            "parent_contract_version": int(contract_payload.get("version") or 0),
            "origin_tool_call_id": str(context.call_id or ""),
            "goal_fingerprint": hashlib.sha256(
                fingerprint_seed.encode("utf-8")
            ).hexdigest(),
            "scope_summary": scope_summary,
            "artifact_paths": artifact_paths[:32],
            "created_at": time.time(),
        }
        return metadata

    def _write_task_artifacts(
        self,
        *,
        session_id: str,
        task_id: str,
        workdir: Optional[str],
        contract: dict[str, Any],
        result_summary: str,
        result_payload: dict[str, Any],
        tool_trace: list[dict[str, Any]],
        full_result: Optional[str] = None,
    ) -> dict[str, str]:
        return RuntimeArtifactManager.write_bundle(
            session_id=session_id,
            task_id=task_id,
            workdir=workdir,
            contract=contract,
            summary=result_summary,
            result=result_payload,
            tool_trace=tool_trace,
            full_result=full_result,
        )

    async def _get_or_create_subagent_session(
        self,
        *,
        context: ToolContext,
        agent_name: str,
        title: str,
        contract: Optional[dict[str, Any]] = None,
        reuse_existing: bool = True,
        metadata_extra: Optional[dict[str, Any]] = None,
    ) -> tuple[Any, bool]:
        """Reuse the latest subagent session under the same parent when possible."""
        parent_id = context.session_id
        owner_kwargs = await self._session_owner_kwargs(context)
        if reuse_existing:
            existing_children = await SessionManager.list(parent_id=parent_id, **owner_kwargs)
            for child in existing_children:
                if str(getattr(child, "agent_name", "") or "") == agent_name:
                    try:
                        await SessionManager.update(
                            child.id,
                            **owner_kwargs,
                            last_active_at=datetime.now().timestamp(),
                        )
                    except Exception:
                        pass
                    if contract or metadata_extra:
                        metadata = dict(getattr(child, "metadata", {}) or {})
                        if contract:
                            metadata["contract"] = contract
                        if metadata_extra:
                            metadata.update(metadata_extra)
                        await SessionManager.update(child.id, **owner_kwargs, metadata=metadata)
                    return child, True

        session_metadata = {"contract": contract or {}}
        if metadata_extra:
            session_metadata.update(metadata_extra)

        session = await SessionManager.create(
            parent_id=parent_id,
            title=title,
            agent_name=agent_name,
            workdir=self._control_workdir(context),
            **owner_kwargs,
            metadata=session_metadata,
        )
        return session, False

    async def _cleanup_subagent_session(
        self,
        *,
        session_id: str,
        context: Optional[ToolContext] = None,
        agent_name: str = "",
        scheduler_mode: str = "supervisor",
    ) -> None:
        if context:
            self._emit_task_hook(
                "task.cancel",
                context,
                {
                    "task_id": session_id,
                    "agent_name": agent_name,
                    "scheduler_mode": scheduler_mode,
                    "reason": "cleanup",
                },
            )
        owner_kwargs = await self._session_owner_kwargs(context)
        await SessionManager.delete(session_id, **owner_kwargs)
        await WorkerRegistry.delete(session_id)
        if context:
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": scheduler_mode,
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "status": "cleaned_up",
                    "message": "临时 helper 已自动清理",
                    "conversation_with": ["parent_agent"],
                },
            )

    def _resolve_swarms_model_name(self, provider) -> str:
        raw_model = (
            os.environ.get("SWARMS_MODEL")
            or getattr(provider, "_model", None)
            or "gpt-4o-mini"
        )
        model_name = str(raw_model).strip()
        if not model_name:
            return "openai/gpt-4o-mini"
        if "/" in model_name:
            return model_name

        provider_name = str(getattr(provider, "name", "") or "").strip().lower()
        if provider_name == "ollama":
            return f"ollama/{model_name}"
        if provider_name == "anthropic":
            return f"anthropic/{model_name}"

        return f"openai/{model_name}"

    def _normalize_swarms_history_files(self, context: Optional[ToolContext]):
        try:
            workdir = ""
            if context and context.extra:
                workdir = str(context.extra.get("workdir") or "").strip()
            base_dir = Path(workdir or os.getcwd())
            swarms_root = base_dir / "agent_workspace" / "swarms"
            if not swarms_root.exists():
                return

            for history_path in swarms_root.rglob("conversation_history.json"):
                try:
                    raw = history_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    history_path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except Exception:
                    continue
        except Exception:
            pass

    def _prepare_swarms_runtime(self, provider, model_name: str) -> bool:
        provider_name = str(getattr(provider, "name", "") or "").strip().lower()

        if model_name.startswith("openai/"):
            api_key = str(getattr(provider, "_api_key", "") or "").strip()
            base_url = str(getattr(provider, "_base_url", "") or "").strip()

            # For OpenAI-compatible local gateways, litellm still requires a non-empty key.
            if not api_key and base_url:
                api_key = "sk-local-placeholder"

            if not api_key:
                return False

            os.environ["OPENAI_API_KEY"] = api_key
            if base_url:
                os.environ["OPENAI_API_BASE"] = base_url
                os.environ["OPENAI_BASE_URL"] = base_url
            return True

        if model_name.startswith("anthropic/"):
            api_key = str(getattr(provider, "_api_key", "") or "").strip()
            if not api_key:
                return False
            os.environ["ANTHROPIC_API_KEY"] = api_key
            base_url = str(getattr(provider, "_base_url", "") or "").strip()
            if base_url:
                os.environ["ANTHROPIC_API_BASE"] = base_url
                os.environ["ANTHROPIC_BASE_URL"] = base_url
            return True

        if model_name.startswith("ollama/") or provider_name == "ollama":
            base_url = str(getattr(provider, "_base_url", "") or "").strip()
            if base_url:
                os.environ["OLLAMA_API_BASE"] = base_url
            return True

        return True

    def _default_subagents_for_context(self, context: ToolContext) -> list[str]:
        # 1. Prefer explicitly declared allowed_subagents from agent JSON
        declared = BuiltinAgents.allowed_subagents_for(context.agent)
        if declared:
            return declared

        # 2. Fallback: hardcoded context-specific defaults
        if context.agent == "protein_orchestrator":
            return [
                name
                for name in BuiltinAgents.protein_design_team_names()
                if name != "protein_orchestrator"
            ]
        return ["general"]

    def _infer_subagents_from_text(self, text: str) -> list[str]:
        raw_text = str(text or "")
        if not raw_text.strip():
            return []

        lower = raw_text.lower()
        explicit_matches: list[str] = []
        if re.search(r"(?<![A-Za-z0-9_.-])agent[-_]browser(?![A-Za-z0-9_.-])", lower):
            explicit_matches.append("browser")
        for agent in BuiltinAgents.list():
            if getattr(agent.mode, "value", "") == "primary":
                continue
            name = str(getattr(agent, "name", "") or "").strip()
            if not name:
                continue
            escaped = re.escape(name.lower())
            if re.search(rf"(?<![A-Za-z0-9_.-])@?{escaped}(?![A-Za-z0-9_.-])", lower):
                explicit_matches.append(name)

        if explicit_matches:
            deduped_explicit: list[str] = []
            seen_explicit: set[str] = set()
            for name in explicit_matches:
                if name in seen_explicit:
                    continue
                seen_explicit.add(name)
                deduped_explicit.append(name)
            return deduped_explicit

        groups: list[tuple[str, list[str]]] = [
            (
                "explore",
                [
                    "explore",
                    "expore",
                    "code exploration",
                    "browse code",
                    "scan code",
                    "探索",
                    "浏览代码",
                    "代码结构",
                ],
            ),
            (
                "general",
                [
                    "general",
                    "architecture",
                    "analyze architecture",
                    "project function",
                    "通用",
                    "分析项目功能",
                    "架构设计",
                    "功能和架构",
                ],
            ),
            (
                "plan",
                [
                    "plan",
                    "improvement",
                    "improve",
                    "assessment",
                    "evaluate",
                    "规划",
                    "改进建议",
                    "评估",
                    "项目状态",
                ],
            ),
        ]

        matched: list[str] = []
        for agent_name, patterns in groups:
            for pattern in patterns:
                candidate = pattern.lower()
                if re.search(rf"\b{re.escape(candidate)}\b", lower) or candidate in lower:
                    matched.append(agent_name)
                    break

        deduped: list[str] = []
        seen: set[str] = set()
        for name in matched:
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def _infer_subagents_from_allowed_tools(self, allowed_tools: Any) -> list[str]:
        if allowed_tools in (None, "", "all"):
            return []
        if isinstance(allowed_tools, str):
            items = [part.strip() for part in allowed_tools.split(",") if part.strip()]
        elif isinstance(allowed_tools, list):
            items = [str(part or "").strip() for part in allowed_tools if str(part or "").strip()]
        else:
            return []
        tool_names = set(items)
        if not any(name.startswith("paper_library_") for name in tool_names):
            return []

        extraction_tools = {"skill", "skill_run", "read", "bash", "glob"}
        if tool_names & extraction_tools and BuiltinAgents.get("paper_generalist"):
            return ["paper_generalist"]
        if BuiltinAgents.get("paper_library_manager"):
            return ["paper_library_manager"]
        if BuiltinAgents.get("paper_generalist"):
            return ["paper_generalist"]
        return []

    def _resolve_subagent_list(
        self,
        subagent_type: Optional[str],
        context: ToolContext,
        description: Optional[str] = None,
        prompt: Optional[str] = None,
        allowed_tools: Any = None,
    ) -> list[str]:
        def _normalize_candidate(name: str) -> str:
            raw_name = str(name or "").strip()
            if not raw_name:
                return ""
            if BuiltinAgents.get(raw_name):
                return raw_name

            lowered = raw_name.lower()
            alias_to_builtin = {
                "agent-browser": "browser",
                "agent_browser": "browser",
            }
            mapped = alias_to_builtin.get(lowered)
            if mapped and BuiltinAgents.get(mapped):
                return mapped

            for prefix in ("agent-", "subagent-", "agent_", "subagent_"):
                if lowered.startswith(prefix):
                    stripped = lowered[len(prefix) :]
                    if BuiltinAgents.get(stripped):
                        return stripped

            hyphen_name = lowered.replace("_", "-")
            underscore_name = lowered.replace("-", "_")
            if BuiltinAgents.get(hyphen_name):
                return hyphen_name
            if BuiltinAgents.get(underscore_name):
                return underscore_name

            return raw_name

        def _filter_allowed(candidates: list[str]) -> list[str]:
            if not candidates:
                return []
            allowed = BuiltinAgents.allowed_subagents_for(context.agent)
            if allowed:
                return [name for name in candidates if name in allowed]
            return candidates

        raw = (subagent_type or "").strip()
        raw_lower = raw.lower()
        if not raw or raw_lower == "general":
            inferred = self._infer_subagents_from_text(
                f"{description or ''}\n{prompt or ''}"
            )
            if inferred:
                inferred = _filter_allowed(inferred)
                if inferred:
                    return inferred
            inferred = _filter_allowed(self._infer_subagents_from_allowed_tools(allowed_tools))
            if inferred:
                return inferred
            if raw_lower == "general":
                return self._default_subagents_for_context(context)
            return self._default_subagents_for_context(context)

        if raw in {"protein_team", "protein_design_team"}:
            return [
                name
                for name in BuiltinAgents.protein_design_team_names()
                if name != "protein_orchestrator"
            ]

        candidates = [_normalize_candidate(part) for part in raw.split(",") if part.strip()]
        if not candidates:
            return self._default_subagents_for_context(context)
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            deduped.append(candidate)
        return deduped

    @classmethod
    def _requested_mcp_target_servers(cls, text: Any) -> list[str]:
        targets: list[str] = []
        for match in cls._MCP_SERVER_PATTERN.finditer(str(text or "")):
            server = str(match.group(1) or "").strip().lower()
            if server in {"job-protocol-mcp", "paper-search-mcp", "paper-download-mcp"}:
                continue
            if server and server not in targets:
                targets.append(server)
        return targets

    def _route_subagents_for_mcp_targets(
        self,
        *,
        candidates: list[str],
        request_text: str,
        context: ToolContext,
    ) -> tuple[list[str], list[str], bool]:
        """Keep MCP submissions on an agent explicitly authorized for every target."""
        targets = self._requested_mcp_target_servers(request_text)
        if not targets:
            return list(candidates), [], False

        workdir = self._control_workdir(context)
        allowed_sets = [
            set(_agents_allowed_to_route_server(target, workdir))
            for target in targets
        ]
        common = set.intersection(*allowed_sets) if allowed_sets else set()
        authorized_requested = [name for name in candidates if name in common]
        if authorized_requested:
            return authorized_requested, targets, False

        ordered = _agents_allowed_to_route_server(targets[0], workdir)
        authorized_fallback = [name for name in ordered if name in common]
        return authorized_fallback[:1], targets, bool(authorized_fallback)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TaskParameters(**args)
        gate_error = protein_design_task_gate_error(context)
        if gate_error:
            return ToolResult(
                title=parsed.description or "Task",
                output=(
                    "蛋白设计模式在 BRAINSTORM 通过前禁止调用 task。\n"
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

        # Routing is a one-time decision over caller-authored input.  Contract
        # text, resource hints, recovery checkpoints, and scheduler wrappers
        # must never be allowed to silently reclassify the chosen subagent.
        original_prompt = parsed.prompt
        contract = self._build_contract(parsed)
        allowed_tools = contract.get("allowed_tools", "all")
        unknown_tools = self._unknown_allowed_tools(allowed_tools)
        if unknown_tools:
            return self._invalid_allowlist_result(
                title=parsed.description,
                unknown_tools=unknown_tools,
                resolved_tools=(
                    list(allowed_tools) if isinstance(allowed_tools, list) else []
                ),
            )
        requested_subagents = self._resolve_subagent_list(
            parsed.subagent_type,
            context,
            description=parsed.description,
            prompt=original_prompt,
            allowed_tools=allowed_tools,
        )
        if "browser" in requested_subagents:
            from ...runtime.sandbox.tool_policy import sandbox_tool_block_reason
            unavailable = sandbox_tool_block_reason("browser", context)
            if unavailable:
                return ToolResult(
                    title=parsed.description or "Browser task", output="", error=unavailable,
                    metadata={"browser_unavailable": True, "execution_skipped": True,
                              "retryable": False, "should_end_turn": True},
                )
        requested_subagents, mcp_route_targets, mcp_route_reassigned = (
            self._route_subagents_for_mcp_targets(
                candidates=requested_subagents,
                request_text=f"{parsed.description or ''}\n{original_prompt}",
                context=context,
            )
        )
        if mcp_route_targets and not requested_subagents:
            return ToolResult(
                title=parsed.description or "Task",
                output="",
                error=(
                    "No configured subagent is allowed to route all requested MCP targets: "
                    + ", ".join(mcp_route_targets)
                ),
                metadata={
                    "validation": "no_authorized_mcp_route_agent",
                    "target_servers": mcp_route_targets,
                    "retryable": False,
                    "should_end_turn": False,
                },
            )
        latest_user_scope = ""
        for message in reversed(list(getattr(context, "messages", []) or [])):
            role = getattr(message, "role", "")
            if str(getattr(role, "value", role) or "").strip().lower() != "user":
                continue
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                latest_user_scope = content.strip()
                break
        scope_hash = biosafety_scope_hash(latest_user_scope or parsed.prompt)
        workdir = str((context.extra or {}).get("workdir") or "").strip()
        for subagent_name in requested_subagents:
            biosafety_gate = evaluate_worker_dispatch(
                workdir=workdir,
                team_id=context.session_id,
                worker_name=subagent_name,
                scope_hash=scope_hash,
            )
            if not biosafety_gate.allowed:
                return ToolResult(
                    title=parsed.description or "Task",
                    output=(
                        f"Biosafety Gate blocked subagent `{subagent_name}`: "
                        f"{biosafety_gate.reason}\n"
                        "请通过蛋白团队的 assign_task(worker_name=\"risk\", wait=true) "
                        "完成审查，再启动 design/experiment。"
                    ),
                    metadata={
                        "biosafety_blocked": True,
                        "biosafety_action": biosafety_gate.action,
                        "required_worker": "risk",
                        "scope_hash": scope_hash,
                        **dict(biosafety_gate.metadata or {}),
                    },
                )

        resource_context = self._normalize_resource_context(
            (context.extra or {}).get("resource_context") if context.extra else {}
        )
        if resource_context:
            contract = {
                **contract,
                "resources": resource_context,
            }
        if not self._is_strict_input_contract_prompt(parsed.prompt):
            parsed.prompt = self._build_contract_prompt(
                contract,
                parsed.prompt,
                resource_context,
            )
        if mcp_route_reassigned:
            parsed.prompt += (
                "\n\n路由约束：该任务已改派给具有目标 MCP 权限的 agent。"
                "必须通过 job-protocol 提交；不得直接调用目标 MCP，也不得用 exec "
                "伪造或绕过提交。"
            )
        scheduler_mode = self._resolve_scheduler_mode(context)

        # Get provider from context
        provider = context.extra.get("provider") if context.extra else None
        if not provider:
            provider = create_provider(
                workdir=context.extra.get("workdir") if context.extra else None
            )

        # Supervisor mode keeps original behavior for backward compatibility.
        if scheduler_mode == "supervisor":
            # Check for checkpoint recovery before running
            if parsed.task_id:
                try:
                    from ...checkpoint import RecoveryHelper
                    workdir = context.extra.get("workdir") if context.extra else None
                    should_resume, checkpoint = RecoveryHelper.should_resume_task(parsed.task_id, workdir=workdir)
                    if should_resume and checkpoint:
                        RecoveryHelper.record_recovery(parsed.task_id, checkpoint, workdir=workdir)
                        # Build resume context from checkpoint
                        resume_prompt = RecoveryHelper.apply_checkpoint_context(checkpoint, parsed.prompt)
                        parsed.prompt = resume_prompt
                except Exception:
                    pass  # If checkpoint recovery fails, just continue normally
            
            if not parsed.wait:
                return await self._start_background_supervisor(
                    parsed,
                    context,
                    provider,
                    contract,
                    selected_subagents=requested_subagents,
                )
            return await self._execute_supervisor(
                parsed,
                context,
                provider,
                contract,
                selected_subagents=requested_subagents,
            )

        if not parsed.wait:
            return ToolResult(
                title=parsed.description or "Task",
                output="",
                error="wait=false is only supported in supervisor mode",
                metadata={"retryable": False, "should_end_turn": False},
            )

        if scheduler_mode == "auto_swarm_builder":
            return await self._execute_auto_swarm_builder(
                parsed,
                context,
                provider,
                contract,
                selected_subagents=requested_subagents,
            )

        return await self._execute_chain_or_swarm(
            parsed=parsed,
            context=context,
            provider=provider,
            scheduler_mode=scheduler_mode,
            contract=contract,
            selected_subagents=requested_subagents,
        )

    def _background_limit(self) -> int:
        try:
            return max(
                1,
                min(
                    16,
                    int(os.environ.get("TCODE_SUBAGENT_BACKGROUND_LIMIT", "4") or "4"),
                ),
            )
        except (TypeError, ValueError):
            return 4

    async def _start_background_supervisor(
        self,
        parsed: TaskParameters,
        context: ToolContext,
        provider: Any,
        contract: dict[str, Any],
        *,
        selected_subagents: Optional[list[str]] = None,
    ) -> ToolResult:
        parent_session_id = str(context.session_id or "").strip()
        active = sum(
            1
            for record in _BACKGROUND_SUBAGENT_TASKS.values()
            if record.get("parent_session_id") == parent_session_id
            and not record["task"].done()
        )
        limit = self._background_limit()
        if active >= limit:
            return ToolResult(
                title=parsed.description or "Task",
                output="",
                error=f"background subagent limit reached ({active}/{limit})",
                metadata={
                    "status": "blocked",
                    "retryable": True,
                    "should_end_turn": False,
                    "background_limit": limit,
                },
            )

        background_task_id = f"subagent-{uuid.uuid4()}"
        started = asyncio.Event()
        record: dict[str, Any] = {
            "task_id": background_task_id,
            "parent_session_id": parent_session_id,
            "session_id": "",
            "description": parsed.description,
            "created_at": time.time(),
            "started": started,
        }

        async def _run() -> ToolResult:
            try:
                return await self._execute_supervisor(
                    parsed,
                    context,
                    provider,
                    contract,
                    selected_subagents=selected_subagents,
                    background_task_id=background_task_id,
                )
            finally:
                started.set()

        task = asyncio.create_task(_run(), name=background_task_id)
        # Retrieve failures eagerly so a completed, uncollected handle does not
        # produce an asyncio "exception was never retrieved" warning. Calling
        # result() later from task_wait remains valid.
        def _consume_exception(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            try:
                completed.exception()
            except Exception:
                pass

        task.add_done_callback(_consume_exception)
        record["task"] = task
        _BACKGROUND_SUBAGENT_TASKS[background_task_id] = record
        # Wait only for the child Session to be allocated, never for its turn.
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        # Keep a bounded in-process history of finished handles.
        completed = sorted(
            (
                item
                for item in _BACKGROUND_SUBAGENT_TASKS.values()
                if item["task"].done()
            ),
            key=lambda item: float(item.get("created_at") or 0),
        )
        for item in completed[:-128]:
            _BACKGROUND_SUBAGENT_TASKS.pop(str(item.get("task_id") or ""), None)

        session_id = str(record.get("session_id") or "")
        return ToolResult(
            title=parsed.description or "Task",
            output=(
                f"Background subagent started. task_id={background_task_id}"
                + (f", session_id={session_id}" if session_id else "")
                + ". Use task_wait to inspect or collect the result."
            ),
            metadata={
                "accepted_async": True,
                "status": "running",
                "task_id": background_task_id,
                "session_id": session_id,
                "scheduler_mode": "supervisor",
                "background": True,
                "background_limit": limit,
                "should_end_turn": False,
            },
        )

    async def _execute_auto_swarm_builder(
        self,
        parsed: TaskParameters,
        context: ToolContext,
        provider,
        contract: dict[str, Any],
        selected_subagents: Optional[list[str]] = None,
    ) -> ToolResult:
        if parsed.task_id:
            return ToolResult(
                title=parsed.description,
                output="",
                error="task_id resume is only supported in supervisor mode",
            )

        self._last_auto_swarm_error = ""
        if contract.get("allowed_tools", "all") != "all":
            self._last_auto_swarm_error = "自动构建引擎不支持本任务的工具权限约束，已改用受控蜂群。"
            result = None
        else:
            result = await self._run_auto_swarm_builder(
                prompt=parsed.prompt,
                provider=provider,
                context=context,
            )
        if result is not None:
            # Keep the roster returned by the executing builder, not the
            # requested roster or an instance attribute shared by concurrent calls.
            participant_metadata = {}
            try:
                payload = json.loads(result)
                members = payload.get("agents") if isinstance(payload, dict) else None
                if isinstance(members, list) and all(isinstance(name, str) and name.strip() for name in members):
                    participant_metadata = {
                        "participant_count": len(set(members)),
                        "subagents": list(dict.fromkeys(members)),
                    }
            except (TypeError, ValueError):
                pass
            claim_validation = validate_async_submission_claim(
                request_text=parsed.prompt,
                result_text=result,
                tool_trace=[],
            )
            if claim_validation.get("required") and not claim_validation.get("valid"):
                return ToolResult(
                    title=parsed.description or "Task",
                    output="",
                    error=str(claim_validation.get("reason") or "异步提交声明缺少工具证据。"),
                    metadata={
                        "scheduler_mode": "auto_swarm_builder",
                        "engine": "swarms_auto_swarm_builder",
                        "status": "failed",
                        "retryable": True,
                        "terminal_tool_error": False,
                        "should_end_turn": False,
                        "delegated_claim_validation": claim_validation,
                    },
                )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "auto_swarm_builder",
                    "session_id": "",
                    "agent_name": "auto_swarm_builder",
                    "status": "completed",
                    "message": "AutoSwarmBuilder completed",
                },
            )
            output_lines = [
                "scheduler_mode: auto_swarm_builder",
                "subagents: auto-generated",
                *([f"participant_count: {participant_metadata['participant_count']}"] if participant_metadata else []),
                "",
                "<task_result agent=\"auto_swarm_builder\">",
                result,
                "</task_result>",
            ]
            return ToolResult(
                title=parsed.description or "Task",
                output="\n".join(output_lines),
                metadata={
                    "scheduler_mode": "auto_swarm_builder",
                    "engine": "swarms_auto_swarm_builder",
                    "status": "completed",
                    **participant_metadata,
                },
            )

        reason = self._last_auto_swarm_error or "自动构建引擎未能返回结果。"
        fallback = await self._execute_chain_or_swarm(
            parsed=parsed,
            context=context,
            provider=provider,
            scheduler_mode="swarm",
            contract=contract,
            selected_subagents=selected_subagents,
        )
        fallback.metadata = {
            **(fallback.metadata or {}),
            "scheduler_mode": "auto_swarm_builder",
            "effective_scheduler_mode": "swarm",
            "fallback_reason": reason,
        }
        fallback.output = f"自动群体构建已回退为普通蜂群：{reason}\n\n{fallback.output}"
        return fallback

    @staticmethod
    def _validate_delegated_run_results(
        *,
        request_text: str,
        run_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Apply the delegated-claim guard uniformly to every scheduler engine."""

        for item in run_results or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            tool_trace = (
                item.get("tool_trace")
                if isinstance(item.get("tool_trace"), list)
                else []
            )
            validation = validate_async_submission_claim(
                request_text=request_text,
                result_text=content,
                tool_trace=tool_trace,
            )
            item["delegated_claim_validation"] = validation
            if not validation.get("required") or validation.get("valid"):
                continue
            handoff = build_subtask_handoff(
                request_text=request_text,
                result_text=content,
                tool_trace=tool_trace,
                child_outcome_status="failed",
                session_id=item.get("session_id") or "",
                agent_name=item.get("agent") or "",
            )
            if not content.startswith(
                "子任务的异步提交声明未通过工具证据校验。"
            ):
                item["content"] = handoff.result_text
            item["status"] = handoff.child_outcome_status
            item["error"] = str(validation.get("reason") or handoff.blocker)
            item["subtask_handoff"] = handoff.to_dict()
        return run_results

    async def _run_auto_swarm_builder(
        self,
        prompt: str,
        provider,
        context: Optional[ToolContext] = None,
    ) -> Optional[str]:
        from ...swarm.capabilities import auto_swarm_builder_availability

        availability = auto_swarm_builder_availability()
        if not self._use_swarms_enabled(context) or not availability["available"]:
            self._last_auto_swarm_error = str(availability["reason"] or "本任务已关闭扩展蜂群引擎。")
            return None

        os.environ.setdefault("SWARMS_TELEMETRY_ON", "false")
        try:
            auto_builder_module = importlib.import_module(
                "swarms.structs.auto_swarm_builder"
            )
            AutoSwarmBuilder = getattr(auto_builder_module, "AutoSwarmBuilder")
        except Exception:
            self._last_auto_swarm_error = "AutoSwarmBuilder 模块加载失败，请检查服务环境中的 Swarms 依赖。"
            return None

        model_name = self._resolve_swarms_model_name(provider)
        if not self._prepare_swarms_runtime(provider, model_name):
            self._last_auto_swarm_error = "自动构建引擎的模型连接配置不可用。"
            return None

        try:
            parameters = inspect.signature(AutoSwarmBuilder).parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            options = dict(name="tcode-auto-swarm", description="Auto-generated swarm for tcode task",
                           verbose=False, max_loops=1, model_name=model_name,
                           auto_execute=True, return_agents=False)
            if not accepts_kwargs and "model_name" not in parameters:
                raise TypeError("Builder cannot preserve the selected model")
            # Generated team specs must not override the user's provider/model.
            if hasattr(AutoSwarmBuilder, "create_agents_from_specs"):
                base_url = str(getattr(provider, "_base_url", "") or "").strip()

                class ConfiguredBuilder(AutoSwarmBuilder):
                    def build_llm_agent(self, config):
                        model = super().build_llm_agent(config)
                        if model_name.startswith("anthropic/"):
                            # Forced tool_choice conflicts with Anthropic thinking.
                            # The builder still validates JSON against this schema.
                            model.response_format = None
                            model.system_prompt += "\nReturn ONLY JSON matching this schema:\n" + json.dumps(config.model_json_schema())
                            model.messages = [{"role": "system", "content": model.system_prompt}]
                        return model

                    def create_agents_from_specs(self, specs):
                        items = specs.get("agents", []) if isinstance(specs, dict) else specs.agents
                        agents = []
                        for item in items:
                            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
                            data["model_name"] = model_name
                            if not data.get("agent_name") or not data.get("system_prompt"):
                                raise ValueError("Generated agent requires name and system prompt")
                            data["agent_description"] = data.pop("description", "")
                            # Explicit LLM bypasses Swarms' model-name allowlist,
                            # which cannot recognize configured gateway aliases.
                            data["llm"] = auto_builder_module.LiteLLM(
                                model_name=model_name, base_url=base_url or None,
                                api_key=getattr(provider, "_api_key", None),
                                system_prompt=data["system_prompt"],
                                max_tokens=data.get("max_tokens") or 8192,
                            )
                            agents.append(auto_builder_module.Agent(**data))
                        return agents

                builder_class = ConfiguredBuilder
            else:
                builder_class = AutoSwarmBuilder
            builder = builder_class(**{k: v for k, v in options.items() if accepts_kwargs or k in parameters})
        except Exception:
            self._last_auto_swarm_error = "AutoSwarmBuilder 初始化失败，请检查 Swarms 版本兼容性。"
            return None

        def _run_builder() -> Any:
            return builder.run(prompt)

        try:
            result = await asyncio.to_thread(_run_builder)
        except Exception:
            self._last_auto_swarm_error = "AutoSwarmBuilder 执行失败，已改用受控蜂群。"
            return None

        self._normalize_swarms_history_files(context)

        if result is None or not str(result).strip():
            self._last_auto_swarm_error = "自动构建引擎返回了空结果。"
            return None
        return json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)

    async def _execute_supervisor(
        self,
        parsed: TaskParameters,
        context: ToolContext,
        provider,
        contract: dict[str, Any],
        selected_subagents: Optional[list[str]] = None,
        background_task_id: str = "",
    ) -> ToolResult:
        ephemeral_helpers = self._uses_ephemeral_helpers(context)
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )
        if parsed.task_id and ephemeral_helpers:
            return ToolResult(
                title=parsed.description,
                output="",
                error="当前任务使用临时 helper 模式，不支持通过 task_id 恢复旧子会话。",
            )
        # Resume existing session or create new one
        session = None
        is_resume = False
        owner_kwargs = await self._session_owner_kwargs(context)

        if parsed.task_id:
            session = await SessionManager.get(parsed.task_id, **owner_kwargs)
            if session and str(getattr(session, "parent_id", "") or "") == context.session_id:
                is_resume = True
            else:
                # Session not found, will create new one
                session = None
                parsed.task_id = None

        # Create new session if needed
        if not session:
            subagent_candidates = (
                list(selected_subagents)
                if selected_subagents is not None
                else self._resolve_subagent_list(
                    parsed.subagent_type,
                    context,
                    parsed.description,
                    parsed.prompt,
                    contract.get("allowed_tools", parsed.allowed_tools),
                )
            )
            subagent_type = subagent_candidates[0] if subagent_candidates else "general"
            display_name = self._subagent_display_name(0, 1, subagent_type)
            agent_info = BuiltinAgents.get(subagent_type)
            if not agent_info:
                return ToolResult(
                    title=parsed.description,
                    output="",
                    error=f"Unknown subagent type: {subagent_type}",
                )

            session = await SessionManager.create(
                parent_id=context.session_id,
                title=f"{display_name} · {parsed.description} (@{subagent_type} subagent)",
                agent_name=subagent_type,
                workdir=self._control_workdir(context),
                **owner_kwargs,
                metadata=session_metadata,
            )
        else:
            metadata = dict(getattr(session, "metadata", {}) or {})
            metadata.update(session_metadata)
            await SessionManager.update(session.id, **owner_kwargs, metadata=metadata)

        if background_task_id:
            record = _BACKGROUND_SUBAGENT_TASKS.get(background_task_id)
            if record is not None:
                record["session_id"] = session.id
                started = record.get("started")
                if isinstance(started, asyncio.Event):
                    started.set()

        display_name = self._subagent_display_name(
            1 if is_resume else 0,
            1,
            session.agent_name,
        )

        # Run the subagent (synchronously - waits for completion)
        await self._emit_agent_event(
            context,
            {
                "kind": "subagent",
                "scheduler_mode": "supervisor",
                "session_id": session.id,
                "agent_name": session.agent_name,
                "display_name": display_name,
                "status": "running",
                "message": f"{session.agent_name} started",
                "conversation_with": ["parent_agent"],
            },
        )
        subagent_prompt = self._large_deliverable_prompt(
            parsed.prompt,
            context=context,
            session_id=session.id,
        )
        run_info = await self._run_subagent(
            session=session,
            prompt=subagent_prompt,
            provider=provider,
            contract=contract,
            is_resume=is_resume,
            event_context=context,
            event_payload={
                "kind": "subagent",
                "scheduler_mode": "supervisor",
                "session_id": session.id,
                "agent_name": session.agent_name,
                "display_name": display_name,
                "conversation_with": ["parent_agent"],
                "contract": contract,
            },
        )
        result_content = run_info["content"]
        raw_outcome_status, _ = self._normalize_subagent_outcome_status(
            run_info.get("outcome_status")
        )
        structured_handoff = build_subtask_handoff(
            request_text=parsed.prompt,
            result_text=result_content,
            tool_trace=run_info.get("tool_trace") or [],
            child_outcome_status=raw_outcome_status,
            task_id=session.id,
            session_id=session.id,
            agent_name=session.agent_name,
        )
        outcome_status = (
            "awaiting_input"
            if structured_handoff.outcome_status == "awaiting_input"
            else raw_outcome_status
        )
        child_outcome_status = structured_handoff.child_outcome_status
        child_terminal_error = structured_handoff.child_terminal_error
        task_status = outcome_status
        handoff_content = structured_handoff.render_for_parent()
        completed_subtask_work = structured_handoff.completed_work
        await self._emit_agent_event(
            context,
            {
                "kind": "subagent",
                "scheduler_mode": "supervisor",
                "session_id": session.id,
                "agent_name": session.agent_name,
                "display_name": display_name,
                "status": "running",
                "phase": "integrating",
                "message": (
                    "Subagent is waiting for parent or user input"
                    if outcome_status == "awaiting_input"
                    else (
                    "Subagent returned a partial checkpoint; integrating available result"
                    if outcome_status == "partial"
                    else "Subagent completed; integrating result"
                    )
                ),
                "tools_used": run_info["tools_used"],
                "conversation_with": ["parent_agent"],
                "last_response": self._trim_text(result_content),
                "context": run_info["context"],
                "contract": contract,
            },
        )

        # Auto-memory: capture subagent result to team memory
        try:
            import os as _os
            _am_enabled = str(_os.environ.get("TCODE_AUTO_MEMORY_ENABLED", "1") or "1").strip().lower()
            if _am_enabled not in {"0", "false", "no", "off"}:
                from ...memory.manager import MemoryManager
                from ...task.summary_schema import SubagentSummaryValidator
                validation = SubagentSummaryValidator.validate_raw_output(result_content)
                if validation.get("valid"):
                    import json as _json
                    try:
                        data = _json.loads(result_content.strip())
                        if isinstance(data, dict):
                            summary = data.get("summary", result_content[:200])
                            verdict = data.get("verdict", "pass")
                            conf = float(data.get("confidence", 0.5) or 0.5)
                            MemoryManager.add_structured(
                                workdir=context.extra.get("workdir") if context.extra else None,
                                source=f"subagent:{session.agent_name}",
                                tags=["subagent_result", str(session.agent_name or "")],
                                task_type="subagent",
                                goal=str(contract.get("goal", "") or parsed.description or "")[:200],
                                outcome=str(summary or "")[:500],
                                confidence=conf,
                                decision=str(verdict or ""),
                            )
                    except Exception:
                        pass
        except Exception:
            pass

        artifact_paths = self._write_task_artifacts(
            session_id=context.session_id,
            task_id=session.id,
            workdir=context.extra.get("workdir") if context.extra else None,
            contract=contract,
            result_summary=handoff_content[:1000],
            result_payload={
                "status": task_status,
                "outcome_status": outcome_status,
                "handoff_outcome_status": structured_handoff.outcome_status,
                "child_outcome_status": child_outcome_status,
                "subtask_terminal_error": child_terminal_error,
                "completed_subtask_steps": len(completed_subtask_work),
                "failed_subtask_steps": len(structured_handoff.failed_work),
                "review_verdict": "",
                "next_action": contract.get("stop_when", ""),
                "subagent": session.agent_name,
                "scheduler_mode": "supervisor",
                "delegated_execution_environment": run_info.get("delegated_execution_environment") or {},
                "agent_workspace": run_info.get("agent_workspace") or {},
                "result_chars": len(handoff_content),
            },
            tool_trace=run_info.get("tool_trace") or [
                {"tool_name": name}
                for name in run_info.get("tools_used", []) or []
            ],
            full_result=handoff_content,
        )
        parent_result_content, large_result_metadata = self._format_task_result_for_parent(
            handoff_content,
            artifact_paths,
        )
        declared_result_path = self._declared_full_result_path(result_content)
        result_output_path = declared_result_path or str(
            artifact_paths.get("full_result_path") or ""
        )
        if declared_result_path:
            large_result_metadata["agent_deliverable_path"] = declared_result_path
        if ephemeral_helpers:
            await self._cleanup_subagent_session(
                session_id=session.id,
                context=context,
                agent_name=session.agent_name,
                scheduler_mode="supervisor",
            )

        output_lines = [
            (
                f"task_id: {session.id} (临时 helper，结果已归档并自动清理)"
                if ephemeral_helpers
                else f"task_id: {session.id} (for resuming to continue this task if needed)"
            ),
            "",
            "<task_result>",
            parent_result_content,
            "</task_result>",
        ]

        # Attempt structured summary extraction
        try:
            from ...task.summary_schema import SubagentSummary, SubagentSummaryValidator

            validation = SubagentSummaryValidator.validate_raw_output(result_content)
            if validation.get("valid"):
                try:
                    data = json.loads(result_content.strip())
                    if isinstance(data, dict):
                        summary_obj = SubagentSummary.from_dict(data)
                        output_lines.append("")
                        output_lines.append("<summary_verdict>")
                        output_lines.append(f"verdict: {summary_obj.verdict.value}")
                        output_lines.append(f"evidence: {summary_obj.evidence_level.value}")
                        output_lines.append(f"confidence: {summary_obj.confidence:.0%}")
                        if summary_obj.next_actions:
                            output_lines.append(f"next_actions: {', '.join(summary_obj.next_actions[:3])}")
                        output_lines.append("</summary_verdict>")
                except Exception:
                    pass
        except Exception:
            pass

        metadata = self._task_metadata_with_runtime(
            {
                "task_id": session.id,
                "session_id": session.id,
                "status": task_status,
                "outcome_status": outcome_status,
                "handoff_outcome_status": structured_handoff.outcome_status,
                "child_outcome_status": child_outcome_status,
                "subagent": session.agent_name,
                "scheduler_mode": "supervisor",
                "contract": contract,
                "artifacts": artifact_paths,
                "agent_workspace": run_info.get("agent_workspace") or {},
                "ephemeral_helper": ephemeral_helpers,
                "tool_trace": run_info.get("tool_trace") or [],
                **large_result_metadata,
                # A child may stop at its own permission or capability boundary,
                # but that terminal signal must not cross the task boundary and
                # terminate the parent Agent before it can recover or re-delegate.
                "retryable": child_terminal_error,
                "terminal_tool_error": False,
                "should_end_turn": False,
                "subtask_terminal_error": child_terminal_error,
                "termination_scope": "subagent" if child_terminal_error else "none",
                "recovery_required": structured_handoff.recovery_required,
                "completed_subtask_steps": len(completed_subtask_work),
                "completed_subtask_work": completed_subtask_work,
                "failed_subtask_steps": len(structured_handoff.failed_work),
                "failed_subtask_work": structured_handoff.failed_work,
                "subtask_handoff": structured_handoff.to_dict(),
                "delegated_claim_validation": (
                    run_info.get("delegated_claim_validation") or {}
                ),
            },
            run_info.get("delegated_execution_environment"),
        )

        await self._emit_agent_event(
            context,
            {
                "kind": "subagent",
                "scheduler_mode": "supervisor",
                "session_id": session.id,
                "agent_name": session.agent_name,
                "display_name": display_name,
                "status": (
                    "awaiting_input"
                    if outcome_status == "awaiting_input"
                    else ("completed" if not child_terminal_error else task_status)
                ),
                "phase": (
                    "awaiting_input"
                    if outcome_status == "awaiting_input"
                    else ("completed" if not child_terminal_error else task_status)
                ),
                "outcome_status": outcome_status,
                "message": (
                    "Waiting for parent or user input"
                    if outcome_status == "awaiting_input"
                    else (
                    "Partial checkpoint integrated"
                    if outcome_status == "partial"
                    else (
                        "Result integrated"
                        if task_status == "completed"
                        else "Subagent ended before completing the task"
                    )
                    )
                ),
                "tools_used": run_info["tools_used"],
                "conversation_with": ["parent_agent"],
                "last_response": self._trim_text(result_content),
                "context": run_info["context"],
                "contract": contract,
            },
        )

        return ToolResult(
            title=parsed.description or "Task",
            output="\n".join(output_lines),
            # Preserve the child failure as a non-terminal tool error. The
            # parent Harness records it, exposes the handoff output, and then
            # gives the parent Agent another reasoning turn.
            error=result_content if child_terminal_error else "",
            metadata=metadata,
            output_path=result_output_path or None,
        )

    async def _execute_chain_or_swarm(
        self,
        parsed: TaskParameters,
        context: ToolContext,
        provider,
        scheduler_mode: str,
        contract: dict[str, Any],
        selected_subagents: Optional[list[str]] = None,
    ) -> ToolResult:
        if parsed.task_id:
            return ToolResult(
                title=parsed.description,
                output="",
                error="task_id resume is only supported in supervisor mode",
            )

        subagents = (
            list(selected_subagents)
            if selected_subagents is not None
            else self._resolve_subagent_list(
                parsed.subagent_type,
                context,
                parsed.description,
                parsed.prompt,
                contract.get("allowed_tools", parsed.allowed_tools),
            )
        )

        invalid = [name for name in subagents if not BuiltinAgents.get(name)]
        if invalid:
            return ToolResult(
                title=parsed.description,
                output="",
                error=f"Unknown subagent type(s): {', '.join(invalid)}",
            )

        if scheduler_mode == "chain":
            run_results = await self._run_chain(
                description=parsed.description,
                prompt=parsed.prompt,
                subagents=subagents,
                context=context,
                provider=provider,
                contract=contract,
            )
        else:
            run_results = await self._run_swarm(
                description=parsed.description,
                prompt=parsed.prompt,
                subagents=subagents,
                context=context,
                provider=provider,
                parsed=parsed,
                contract=contract,
            )
        run_results = self._validate_delegated_run_results(
            request_text=parsed.prompt,
            run_results=run_results,
        )
        participant_count = len({
            str(item["session_id"]) for item in run_results if item.get("session_id")
        })

        failed_results = [
            item
            for item in run_results
            if str(item.get("status") or "completed").strip().lower()
            in {"failed", "error", "blocked", "cancelled", "canceled"}
        ]
        waiting_results = [
            item
            for item in run_results
            if str(item.get("status") or "").strip().lower()
            in {
                "awaiting_input",
                "waiting_for_user",
                "needs_parent_input",
                "needs_user_input",
            }
        ]
        partial_results = [
            item
            for item in run_results
            if str(item.get("status") or "").strip().lower() == "partial"
        ]
        result_status = (
            "partial"
            if failed_results or partial_results
            else ("awaiting_input" if waiting_results else "completed")
        )

        output_lines = [
            f"scheduler_mode: {scheduler_mode}",
            f"subagents: {', '.join(subagents)}",
            f"participant_count: {participant_count}",
            "",
        ]

        engine = ""
        topology = ""
        if scheduler_mode == "swarm":
            topology = self._resolve_swarm_topology(context)
            engines = {
                str(item.get("engine", "")).strip()
                for item in run_results
                if str(item.get("engine", "")).strip()
            }
            if len(engines) == 1:
                engine = next(iter(engines))
            elif len(engines) > 1:
                engine = "mixed"

        if engine:
            output_lines.insert(2, f"engine: {engine}")
        if topology:
            output_lines.insert(3 if engine else 2, f"topology: {topology}")

        if scheduler_mode == "swarm":
            peer_comm = self._resolve_swarm_peer_communication(context, parsed)
            rounds = self._resolve_swarm_communication_rounds(context, parsed)
            output_lines.insert(
                4 if (engine and topology) else (3 if (engine or topology) else 2),
                f"peer_communication: {'enabled' if peer_comm and rounds > 0 else 'disabled'}",
            )
            output_lines.insert(
                5 if (engine and topology) else (4 if (engine or topology) else 3),
                f"communication_rounds: {rounds if peer_comm else 0}",
            )

        for item in run_results:
            output_lines.extend(
                [
                    f"task_id[{item['agent']}]: {item['session_id']}",
                    *(["tool_evidence: " + json.dumps(item["tool_trace"], ensure_ascii=False, default=str)] if item.get("tool_trace") else []),
                    f"<task_result agent=\"{item['agent']}\">",
                    item["content"],
                    "</task_result>",
                    "",
                ]
            )

        if failed_results:
            output_lines.extend(
                [
                    "<task_warnings>",
                    *[
                        f"- {item.get('agent', 'subagent')}: "
                        f"{str(item.get('error') or item.get('content') or 'failed')[:500]}"
                        for item in failed_results
                    ],
                    "</task_warnings>",
                    "",
                ]
            )

        # Auto-memory: capture subagent result to team memory
        try:
            import os as _os
            _am_enabled = str(_os.environ.get("TCODE_AUTO_MEMORY_ENABLED", "1") or "1").strip().lower()
            if _am_enabled not in {"0", "false", "no", "off"}:
                from ...memory.manager import MemoryManager
                from ...task.summary_schema import SubagentSummaryValidator
                import json as _json
                for item in run_results:
                    result_content = str(item.get("content") or "")
                    validation = SubagentSummaryValidator.validate_raw_output(
                        result_content
                    )
                    if not validation.get("valid"):
                        continue
                    try:
                        data = _json.loads(result_content.strip())
                        if not isinstance(data, dict):
                            continue
                        summary = data.get("summary", result_content[:200])
                        verdict = data.get("verdict", "pass")
                        conf = float(data.get("confidence", 0.5) or 0.5)
                        agent_name = str(item.get("agent") or "subagent")
                        MemoryManager.add_structured(
                            workdir=context.extra.get("workdir") if context.extra else None,
                            source=f"subagent:{agent_name}",
                            tags=["subagent_result", agent_name],
                            task_type="subagent",
                            goal=str(contract.get("goal", "") or parsed.description or "")[:200],
                            outcome=str(summary or "")[:500],
                            confidence=conf,
                            decision=str(verdict or ""),
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        delegated_environment = next(
            (
                self._object_value(item.get("delegated_execution_environment"))
                for item in reversed(run_results)
                if self._object_value(item.get("delegated_execution_environment"))
            ),
            {},
        )
        combined_result_content = "\n".join(output_lines).strip()

        artifact_paths = self._write_task_artifacts(
            session_id=context.session_id,
            task_id=f"task-{context.message_id}",
            workdir=context.extra.get("workdir") if context.extra else None,
            contract=contract,
            result_summary="\n\n".join(
                [f"[{item['agent']}]\n{item['content'][:600]}" for item in run_results[:4]]
            ),
            result_payload={
                "status": result_status,
                "review_verdict": "",
                "next_action": contract.get("stop_when", ""),
                "scheduler_mode": scheduler_mode,
                "subagents": [item["agent"] for item in run_results],
                "delegated_execution_environment": delegated_environment,
                "agent_workspaces": [
                    item.get("agent_workspace") or {}
                    for item in run_results
                    if item.get("agent_workspace")
                ],
            },
            tool_trace=[
                {
                    "agent": item["agent"],
                    "session_id": item["session_id"],
                    "engine": item.get("engine", ""),
                    "tools_used": item.get("tools_used") or [],
                    "tool_trace": item.get("tool_trace") or [],
                    "delegated_execution_environment": item.get("delegated_execution_environment") or {},
                }
                for item in run_results
            ],
            full_result=combined_result_content,
        )
        parent_result_content, large_result_metadata = self._format_task_result_for_parent(
            combined_result_content,
            artifact_paths,
        )

        metadata = self._task_metadata_with_runtime(
            {
                "scheduler_mode": scheduler_mode,
                **({"engine": engine} if engine else {}),
                **({"topology": topology} if topology else {}),
                **(
                    {
                        "peer_communication": self._resolve_swarm_peer_communication(
                            context, parsed
                        ),
                        "communication_rounds": self._resolve_swarm_communication_rounds(
                            context, parsed
                        ),
                    }
                    if scheduler_mode == "swarm"
                    else {}
                ),
                "subagents": subagents,
                "participant_count": participant_count,
                "tasks": run_results,
                "tool_trace": [
                    {**entry, "agent": item["agent"], "session_id": item["session_id"]}
                    for item in run_results for entry in item.get("tool_trace", [])
                ],
                "agent_workspaces": [
                    item.get("agent_workspace") or {}
                    for item in run_results
                    if item.get("agent_workspace")
                ],
                "status": result_status,
                "result_status": result_status,
                "failed_subagents": [
                    str(item.get("agent") or "") for item in failed_results
                ],
                "partial_subagents": [
                    str(item.get("agent") or "") for item in partial_results
                ],
                "contract": contract,
                "artifacts": artifact_paths,
                "waiting_subagents": [
                    str(item.get("agent") or "") for item in waiting_results
                ],
                "retryable": bool(failed_results),
                "terminal_tool_error": False,
                "should_end_turn": False,
                "recovery_required": result_status != "completed",
                **large_result_metadata,
            },
            delegated_environment,
        )

        return ToolResult(
            title=parsed.description or "Task",
            output=parent_result_content,
            metadata=metadata,
            output_path=str(artifact_paths.get("full_result_path") or "") or None,
        )

    async def _run_chain(
        self,
        description: str,
        prompt: str,
        subagents: list[str],
        context: ToolContext,
        provider,
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        chained_prompt = prompt
        reuse_existing = not self._uses_ephemeral_helpers(context)
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )

        total = len(subagents)
        for index, agent_name in enumerate(subagents):
            display_name = self._subagent_display_name(index, total, agent_name)
            session, is_resume = await self._get_or_create_subagent_session(
                context=context,
                agent_name=agent_name,
                title=f"{display_name} · {description} (@{agent_name} chain step {index + 1})",
                contract=contract,
                reuse_existing=reuse_existing,
                metadata_extra=session_metadata,
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "chain",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "status": "running",
                    "message": (
                        f"{agent_name} resumed for chain step {index + 1}"
                        if is_resume
                        else f"{agent_name} is processing chain step {index + 1}"
                    ),
                    "conversation_with": ["parent_agent"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=chained_prompt,
                provider=provider,
                contract=contract,
                is_resume=is_resume,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "chain",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "conversation_with": ["parent_agent"],
                },
            )
            content = run_info["content"]
            results.append(
                {
                    "agent": agent_name,
                    "display_name": display_name,
                    "session_id": session.id,
                    "content": content,
                    "status": run_info.get("outcome_status") or "completed",
                    "tools_used": run_info.get("tools_used") or [],
                    "tool_trace": run_info.get("tool_trace") or [],
                    "delegated_execution_environment": run_info.get("delegated_execution_environment") or {},
                    "agent_workspace": run_info.get("agent_workspace") or {},
                }
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "chain",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "status": "running",
                    "phase": "integrating",
                    "message": "Subagent completed; integrating result",
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            if self._uses_ephemeral_helpers(context):
                await self._cleanup_subagent_session(
                    session_id=session.id,
                    context=context,
                    agent_name=agent_name,
                    scheduler_mode="chain",
                )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "chain",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "status": run_info.get("outcome_status") or "completed",
                    "phase": run_info.get("outcome_status") or "completed",
                    "message": (
                        "Waiting for parent or user input"
                        if run_info.get("outcome_status") == "awaiting_input"
                        else "Result integrated"
                    ),
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            if run_info.get("outcome_status") == "awaiting_input":
                break
            chained_prompt = (
                f"Original task:\n{prompt}\n\n"
                f"Previous step output ({agent_name}):\n{content}\n\n"
                "Continue from this context with your specialty and produce the next-step result."
            )

        return results

    async def _run_swarm(
        self,
        description: str,
        prompt: str,
        subagents: list[str],
        context: ToolContext,
        provider,
        parsed: Optional[TaskParameters] = None,
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        topology = self._resolve_swarm_topology(context)
        first_round = await self._run_swarm_first_round(
            description=description,
            prompt=prompt,
            subagents=subagents,
            context=context,
            provider=provider,
            topology=topology,
            contract=contract,
        )

        peer_communication = self._resolve_swarm_peer_communication(context, parsed)
        communication_rounds = self._resolve_swarm_communication_rounds(context, parsed)
        if peer_communication and communication_rounds > 0 and len(first_round) > 1:
            first_round = await self._run_swarm_inter_agent_communication(
                prompt=prompt,
                context=context,
                provider=provider,
                round_results=first_round,
                rounds=communication_rounds,
            )

        first_metrics = first_round[0].get("metrics") if first_round else {}
        external_topology_handled = (
            isinstance(first_metrics, dict)
            and str(first_round[0].get("engine", "") or "") == "external_swarm_runtime"
            and isinstance(first_metrics.get("topology_metrics"), dict)
            and str(first_metrics.get("topology", "") or "") in {"voting", "rearrange"}
        )

        if topology == "sequential":  #### 智能体一次执行
            return first_round
        if topology == "voting":   ### 投票机制，评估彼此的输出，一次表决
            if external_topology_handled:
                return first_round
            return await self._run_swarm_voting(
                prompt=prompt,
                context=context,
                provider=provider,
                first_round=first_round,
                contract=contract,
            )
        if topology == "rearrange":  ##### 根据第一轮输出结果重新排序
            if external_topology_handled:
                return first_round
            return await self._run_swarm_rearrange(
                prompt=prompt,
                context=context,
                provider=provider,
                first_round=first_round,
                contract=contract,
            )

        return await self._run_swarm_peer_refine(
            prompt=prompt,
            context=context,
            provider=provider,
            first_round=first_round,
        )

    def _extract_tagged_block(self, text: str, tag: str) -> str:
        content = str(text or "")
        pattern = rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>"
        match = re.search(pattern, content, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return match.group(1).strip()

    def _parse_p2p_messages(
        self,
        text: str,
        sender: str,
        candidates: list[str],
    ) -> list[dict[str, str]]:
        """Parse strict point-to-point messages in form: TO:<agent> MESSAGE:<text>."""
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        allowed = set(candidates)
        routed: list[dict[str, str]] = []

        for line in lines:
            # Strict P2P format: TO:<agent_name> MESSAGE:<content>
            match = re.match(
                r"^TO\s*:\s*([a-zA-Z0-9_\-]+)\s+MESSAGE\s*:\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            target = match.group(1).strip()
            content = match.group(2).strip()
            if not content:
                continue

            if target.lower() == "all":
                for candidate in candidates:
                    if candidate == sender:
                        continue
                    routed.append(
                        {
                            "from": sender,
                            "to": candidate,
                            "message": content,
                        }
                    )
                continue

            if target in allowed and target != sender:
                routed.append(
                    {
                        "from": sender,
                        "to": target,
                        "message": content,
                    }
                )

        return routed

    def _parse_swarm_inbox_messages(self, prompt_text: str) -> list[dict[str, str]]:
        """Parse inbox messages from swarm turn prompt text.

        Expected line blocks:
        FROM:<agent>
        MESSAGE:<content>
        """
        text = str(prompt_text or "")
        messages: list[dict[str, str]] = []
        lines = [line.strip() for line in text.splitlines()]
        current_from = ""

        for line in lines:
            from_match = re.match(r"^FROM\s*:\s*(.+)$", line, flags=re.IGNORECASE)
            if from_match:
                current_from = from_match.group(1).strip()
                continue

            message_match = re.match(r"^MESSAGE\s*:\s*(.+)$", line, flags=re.IGNORECASE)
            if message_match and current_from:
                msg = message_match.group(1).strip()
                if msg:
                    messages.append({"from": current_from, "message": msg})
                current_from = ""

        return messages

    async def _run_swarm_inter_agent_communication(
        self,
        prompt: str,
        context: ToolContext,
        provider,
        round_results: list[dict[str, str]],
        rounds: int,
    ) -> list[dict[str, str]]:
        updated = [dict(item) for item in round_results]
        display_names = {
            item["agent"]: item.get("display_name") or item["agent"]
            for item in updated
        }
        agent_names = [item["agent"] for item in updated]

        inbox_by_agent: dict[str, list[dict[str, str]]] = {
            name: [] for name in agent_names
        }
        # Seed first inbox from initial outputs so communication can start immediately.
        for sender_item in updated:
            sender = sender_item["agent"]
            seed = self._trim_text(sender_item.get("content", ""), 1200)
            if not seed:
                continue
            for receiver in agent_names:
                if receiver == sender:
                    continue
                inbox_by_agent[receiver].append(
                    {
                        "from": sender,
                        "to": receiver,
                        "message": seed,
                    }
                )

        for round_index in range(rounds):
            next_inbox: dict[str, list[dict[str, str]]] = {
                name: [] for name in agent_names
            }
            owner_kwargs = await self._session_owner_kwargs(context)

            async def _communicate(item: dict[str, str]) -> dict[str, str]:
                if str(item.get("status") or "").strip().lower() in {
                    "awaiting_input",
                    "waiting_for_user",
                }:
                    return item
                agent_name = item["agent"]
                session = await SessionManager.get(item["session_id"], **owner_kwargs)
                if not session:
                    return item

                inbox_messages = inbox_by_agent.get(agent_name) or []
                if not inbox_messages:
                    return item

                inbox_lines = [
                    f"FROM:{msg['from']}\nMESSAGE:{msg['message']}"
                    for msg in inbox_messages
                ]

                comm_prompt = (
                    f"Original task:\n{prompt}\n\n"
                    f"Round: {round_index + 1}/{rounds}\n"
                    f"Your latest answer:\n{item.get('content', '')}\n\n"
                    "Messages delivered to you (strict routed inbox):\n"
                    + "\n\n".join(inbox_lines)
                    + "\n\n"
                    "Reply in this strict format:\n"
                    "<peer_messages>\n"
                    "TO:<agent_name> MESSAGE:<one concise message>\n"
                    "TO:<agent_name> MESSAGE:<one concise message>\n"
                    "</peer_messages>\n"
                    "<updated_answer>Your improved answer for the task after considering peer messages.</updated_answer>"
                )

                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": session.id,
                        "agent_name": agent_name,
                        "display_name": display_names.get(agent_name, agent_name),
                        "engine": item.get("engine", "internal_swarm"),
                        "status": "running",
                        "message": f"Inter-agent communication round {round_index + 1}",
                        "conversation_with": ["peer_agents"],
                    },
                )
                run_info = await self._run_subagent(
                    session=session,
                    prompt=comm_prompt,
                    provider=provider,
                    is_resume=True,
                    event_context=context,
                    event_payload={
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": session.id,
                        "agent_name": agent_name,
                        "display_name": display_names.get(agent_name, agent_name),
                        "conversation_with": ["peer_agents"],
                    },
                )
                raw = run_info.get("content", "")
                peer_messages_block = self._extract_tagged_block(raw, "peer_messages")
                updated_answer = self._extract_tagged_block(raw, "updated_answer")
                next_content = updated_answer or raw
                routed = self._parse_p2p_messages(
                    text=peer_messages_block,
                    sender=agent_name,
                    candidates=agent_names,
                )

                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": session.id,
                        "agent_name": agent_name,
                        "display_name": display_names.get(agent_name, agent_name),
                        "engine": item.get("engine", "internal_swarm"),
                        "status": "completed",
                        "message": f"Inter-agent communication round {round_index + 1} completed",
                        "conversation_with": ["peer_agents"],
                        "tools_used": run_info.get("tools_used") or [],
                        "routed_messages": len(routed),
                        "last_response": self._trim_text(next_content),
                        "context": run_info.get("context") or {},
                    },
                )

                for route_item in routed:
                    receiver = route_item.get("to", "")
                    if receiver in next_inbox:
                        next_inbox[receiver].append(route_item)

                return {
                    **item,
                    "display_name": display_names.get(agent_name, agent_name),
                    "content": next_content,
                    "status": run_info.get("outcome_status") or item.get("status") or "completed",
                    "peer_messages": [
                        f"TO:{msg['to']} MESSAGE:{msg['message']}"
                        for msg in routed
                    ],
                }

            updated = await asyncio.gather(*[_communicate(item) for item in updated])
            inbox_by_agent = next_inbox

        return updated


    @staticmethod
    @staticmethod
    def _build_swarm_trace(results, scheduler_mode, topology, peer_comm, rounds):
        """Build observability trace for swarm execution.

        Returns a dict suitable for inclusion in result metadata,
        providing visibility into swarm agent participation and configuration.
        """
        trace = {
            "scheduler_mode": scheduler_mode,
            "topology": topology,
            "peer_communication": peer_comm,
            "max_rounds": rounds,
            "agent_count": len(results) if results else 0,
            "agents": [],
        }
        for item in (results or []):
            agent_info = {
                "agent": item.get("agent", ""),
                "session_id": item.get("session_id", ""),
                "engine": item.get("engine", "unknown"),
                "output_length": len(str(item.get("content", ""))),
            }
            # Include external swarm metrics if available
            if isinstance(item.get("metrics"), dict):
                agent_info["metrics"] = {
                    "rounds": item["metrics"].get("rounds"),
                    "pheromone_keys": item["metrics"].get("pheromone_keys"),
                    "bus_backend": item["metrics"].get("bus_backend"),
                }
            trace["agents"].append(agent_info)
        return trace

    async def _run_swarm_first_round(
        self,
        description: str,
        prompt: str,
        subagents: list[str],
        context: ToolContext,
        provider,
        topology: str,
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        swarm_engine = "internal_swarm"
        self._last_swarms_error = ""
        swarms_results = await self._run_swarm_via_swarms_router(
            description=description,
            prompt=prompt,
            subagents=subagents,
            context=context,
            provider=provider,
            topology=topology,
            contract=contract,
        )
        display_names = {
            name: self._subagent_display_name(idx, len(subagents), name)
            for idx, name in enumerate(subagents)
        }

        if swarms_results is not None:
            swarm_engine = "swarms_router"
            for item in swarms_results:
                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": item["session_id"],
                        "agent_name": item["agent"],
                        "display_name": display_names.get(item["agent"], item["agent"]),
                        "engine": item.get("engine", swarm_engine),
                        "status": "completed",
                        "message": "Round 1 completed via swarms router",
                        "conversation_with": ["parent_agent", "peer_agents"],
                        "last_response": self._trim_text(item.get("content", "")),
                    },
                )
            return swarms_results

        if self._is_explicit_external_swarms(context):
            reason = str(getattr(self, "_last_swarms_error", "") or "Unknown swarms runtime error")
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": "",
                    "agent_name": "swarm_router",
                    "display_name": "SwarmsRouter",
                    "engine": "swarms_router",
                    "status": "failed",
                    "message": f"External swarms failed: {reason}",
                },
            )
            raise RuntimeError(
                f"External swarms is enabled but unavailable: {reason}. "
                "Disable 'Use external swarms' to use internal swarm fallback."
            )

        if topology == "sequential":
            return await self._run_swarm_internal_sequential(
                description=description,
                prompt=prompt,
                subagents=subagents,
                context=context,
                provider=provider,
                contract=contract,
            )

        reuse_existing = not self._uses_ephemeral_helpers(context)
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )

        async def _run_one(agent_name: str) -> dict[str, str]:
            display_name = display_names.get(agent_name, agent_name)
            session, is_resume = await self._get_or_create_subagent_session(
                context=context,
                agent_name=agent_name,
                title=f"{display_name} · {description} (@{agent_name} swarm)",
                contract=contract,
                reuse_existing=reuse_existing,
                metadata_extra=session_metadata,
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": swarm_engine,
                    "status": "running",
                    "message": (
                        f"{agent_name} resumed in parallel"
                        if is_resume
                        else f"{agent_name} started in parallel"
                    ),
                    "conversation_with": ["parent_agent"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=prompt,
                provider=provider,
                contract=contract,
                is_resume=is_resume,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "conversation_with": ["parent_agent"],
                },
            )
            content = run_info["content"]
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": swarm_engine,
                    "status": "running",
                    "phase": "integrating",
                    "message": "Subagent completed; integrating result",
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            if self._uses_ephemeral_helpers(context):
                await self._cleanup_subagent_session(
                    session_id=session.id,
                    context=context,
                    agent_name=agent_name,
                    scheduler_mode="swarm",
                )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": swarm_engine,
                    "status": run_info.get("outcome_status") or "completed",
                    "phase": run_info.get("outcome_status") or "completed",
                    "message": (
                        "Waiting for parent or user input"
                        if run_info.get("outcome_status") == "awaiting_input"
                        else "Result integrated"
                    ),
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            return {
                "agent": agent_name,
                "display_name": display_name,
                "session_id": session.id,
                "content": content,
                "engine": swarm_engine,
                "status": run_info.get("outcome_status") or "completed",
                "tools_used": run_info.get("tools_used") or [],
                "tool_trace": run_info.get("tool_trace") or [],
                "delegated_execution_environment": run_info.get("delegated_execution_environment") or {},
                "agent_workspace": run_info.get("agent_workspace") or {},
            }

        async def _run_one_guarded(agent_name: str) -> dict[str, Any]:
            try:
                return await _run_one(agent_name)
            except Exception as exc:
                display_name = display_names.get(agent_name, agent_name)
                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": "",
                        "agent_name": agent_name,
                        "display_name": display_name,
                        "engine": swarm_engine,
                        "status": "failed",
                        "phase": "failed",
                        "message": str(exc)[:1000],
                        "conversation_with": ["parent_agent"],
                    },
                )
                return {
                    "agent": agent_name,
                    "display_name": display_name,
                    "session_id": "",
                    "content": f"Subagent failed: {exc}",
                    "engine": swarm_engine,
                    "status": "failed",
                    "error": str(exc),
                    "tools_used": [],
                    "tool_trace": [],
                    "delegated_execution_environment": {},
                }

        return await asyncio.gather(
            *[_run_one_guarded(name) for name in subagents]
        )

    async def _run_swarm_internal_sequential(
        self,
        description: str,
        prompt: str,
        subagents: list[str],
        context: ToolContext,
        provider,
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        chained_prompt = prompt
        reuse_existing = not self._uses_ephemeral_helpers(context)
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )
        for index, agent_name in enumerate(subagents):
            display_name = self._subagent_display_name(index, len(subagents), agent_name)
            session, is_resume = await self._get_or_create_subagent_session(
                context=context,
                agent_name=agent_name,
                title=f"{display_name} · {description} (@{agent_name} swarm/sequential)",
                contract=contract,
                reuse_existing=reuse_existing,
                metadata_extra=session_metadata,
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "internal_swarm",
                    "status": "running",
                    "message": (
                        f"{agent_name} resumed in sequence"
                        if is_resume
                        else f"{agent_name} started in sequence"
                    ),
                    "conversation_with": ["parent_agent"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=chained_prompt,
                provider=provider,
                contract=contract,
                is_resume=is_resume,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "conversation_with": ["parent_agent"],
                },
            )
            content = run_info["content"]
            results.append(
                {
                    "agent": agent_name,
                    "display_name": display_name,
                    "session_id": session.id,
                    "content": content,
                    "engine": "internal_swarm",
                    "status": run_info.get("outcome_status") or "completed",
                    "tools_used": run_info.get("tools_used") or [],
                    "tool_trace": run_info.get("tool_trace") or [],
                    "delegated_execution_environment": run_info.get("delegated_execution_environment") or {},
                    "agent_workspace": run_info.get("agent_workspace") or {},
                }
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "internal_swarm",
                    "status": "running",
                    "phase": "integrating",
                    "message": "Sequential step completed; integrating result",
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            if self._uses_ephemeral_helpers(context):
                await self._cleanup_subagent_session(
                    session_id=session.id,
                    context=context,
                    agent_name=agent_name,
                    scheduler_mode="swarm",
                )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "internal_swarm",
                    "status": run_info.get("outcome_status") or "completed",
                    "phase": run_info.get("outcome_status") or "completed",
                    "message": (
                        "Waiting for parent or user input"
                        if run_info.get("outcome_status") == "awaiting_input"
                        else "Result integrated"
                    ),
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["parent_agent"],
                    "last_response": self._trim_text(content),
                    "context": run_info["context"],
                },
            )
            if run_info.get("outcome_status") == "awaiting_input":
                break
            chained_prompt = (
                f"Original task:\n{prompt}\n\n"
                f"Previous step output ({agent_name}):\n{content}\n\n"
                "Continue from this context with your specialty and produce the next-step result."
            )
        return results

    async def _run_swarm_peer_refine(
        self,
        prompt: str,
        context: ToolContext,
        provider,
        first_round: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        peer_sync = os.environ.get("TCODE_SWARM_PEER_SYNC", "1").strip().lower()
        if peer_sync in {"0", "false", "off", "no"} or len(first_round) <= 1:
            return first_round

        display_names = {
            item["agent"]: item.get("display_name") or item["agent"]
            for item in first_round
        }
        results_by_agent = {item["agent"]: item["content"] for item in first_round}
        owner_kwargs = await self._session_owner_kwargs(context)

        async def _refine_with_peer_context(item: dict[str, str]) -> dict[str, str]:
            if str(item.get("status") or "").strip().lower() in {
                "awaiting_input",
                "waiting_for_user",
            }:
                return item
            agent_name = item["agent"]
            session = await SessionManager.get(item["session_id"], **owner_kwargs)
            if not session:
                return item

            peer_lines = []
            for other_name, other_output in results_by_agent.items():
                if other_name == agent_name:
                    continue
                peer_lines.append(f"[{other_name}]\n{other_output}")

            if not peer_lines:
                return item

            peer_prompt = (
                f"Original task:\n{prompt}\n\n"
                f"Your previous output:\n{item['content']}\n\n"
                "Peer outputs from other agents:\n"
                + "\n\n".join(peer_lines)
                + "\n\nPlease refine your answer by considering peer insights while keeping your specialty focus."
            )

            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_names.get(agent_name, agent_name),
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "running",
                    "message": "Peer communication round",
                    "conversation_with": ["peer_agents"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=peer_prompt,
                provider=provider,
                is_resume=True,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_names.get(agent_name, agent_name),
                    "conversation_with": ["peer_agents"],
                },
            )
            refined = run_info["content"]
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_names.get(agent_name, agent_name),
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "completed",
                    "message": "Peer communication completed",
                    "tools_used": run_info["tools_used"],
                    "conversation_with": ["peer_agents"],
                    "last_response": self._trim_text(refined),
                    "context": run_info["context"],
                },
            )
            return {
                **item,
                "display_name": display_names.get(agent_name, agent_name),
                "content": refined,
                "status": run_info.get("outcome_status") or item.get("status") or "completed",
            }

        return await asyncio.gather(*[_refine_with_peer_context(item) for item in first_round])

    def _extract_vote_target(self, text: str, candidates: list[str]) -> Optional[str]:
        content = str(text or "")
        match = re.search(r"VOTE\s*[:：]\s*([a-zA-Z0-9_\-]+)", content)
        if match:
            voted = match.group(1).strip()
            if voted in candidates:
                return voted
        lowered = content.lower()
        for candidate in candidates:
            if re.search(rf"\b{re.escape(candidate.lower())}\b", lowered):
                return candidate
        return None

    async def _run_swarm_voting(
        self,
        prompt: str,
        context: ToolContext,
        provider,
        first_round: list[dict[str, str]],
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        if len(first_round) <= 1:
            return first_round

        candidates = [item["agent"] for item in first_round]
        candidate_text = "\n\n".join(
            [f"[{item['agent']}]\n{self._trim_text(item['content'], 1800)}" for item in first_round]
        )
        votes: dict[str, int] = {name: 0 for name in candidates}
        owner_kwargs = await self._session_owner_kwargs(context)

        for item in first_round:
            session = await SessionManager.get(item["session_id"], **owner_kwargs)
            if not session:
                continue

            vote_prompt = (
                f"Original task:\n{prompt}\n\n"
                "You are in a majority-voting topology. Choose the best candidate answer among peers.\n"
                "Reply with the first line exactly as: VOTE:<agent_name>\n"
                "Then briefly explain why.\n\n"
                f"Candidates:\n{candidate_text}"
            )
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": item["agent"],
                    "display_name": item.get("display_name") or item["agent"],
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "running",
                    "message": "Voting round",
                    "conversation_with": ["peer_agents"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=vote_prompt,
                provider=provider,
                is_resume=True,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": item["agent"],
                    "display_name": item.get("display_name") or item["agent"],
                    "conversation_with": ["peer_agents"],
                },
            )
            vote_target = self._extract_vote_target(run_info.get("content", ""), candidates)
            if vote_target:
                votes[vote_target] = votes.get(vote_target, 0) + 1

            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": item["agent"],
                    "display_name": item.get("display_name") or item["agent"],
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "completed",
                    "message": f"Voting completed ({vote_target or 'no vote parsed'})",
                    "conversation_with": ["peer_agents"],
                    "tools_used": run_info.get("tools_used") or [],
                    "context": run_info.get("context") or {},
                },
            )

        winner = max(candidates, key=lambda name: votes.get(name, 0))
        vote_report = "\n".join([f"- {name}: {votes.get(name, 0)}" for name in candidates])
        winner_content = next((item["content"] for item in first_round if item["agent"] == winner), "")

        merge_prompt = (
            f"Original task:\n{prompt}\n\n"
            "You are the voting aggregator in a swarm topology.\n"
            f"Winner by majority: {winner}\n"
            f"Vote tally:\n{vote_report}\n\n"
            f"Winner output:\n{winner_content}\n\n"
            f"Other outputs:\n{candidate_text}\n\n"
            "Produce the final merged answer."
        )
        merged = await self._run_meta_subagent(
            context=context,
            provider=provider,
            title="Swarm Voting Aggregator",
            agent_name="plan",
            prompt=merge_prompt,
            contract=contract,
        )

        await self._emit_agent_event(
            context,
            {
                "kind": "subagent",
                "scheduler_mode": "swarm",
                "session_id": merged["session_id"],
                "agent_name": "swarm_vote",
                "display_name": "SwarmVote",
                "engine": merged.get("engine", "internal_swarm"),
                "status": "completed",
                "message": f"Voting winner: {winner}",
                "last_response": self._trim_text(merged.get("content", "")),
                "conversation_with": ["peer_agents", "parent_agent"],
            },
        )

        return [
            *first_round,
            {
                "agent": "swarm_vote",
                "display_name": "SwarmVote",
                "session_id": merged["session_id"],
                "content": (
                    f"Voting winner: {winner}\n\n"
                    f"Vote tally:\n{vote_report}\n\n"
                    f"Merged result:\n{merged.get('content', '')}"
                ),
                "engine": merged.get("engine", "internal_swarm"),
            },
        ]

    def _extract_rearrange_order(
        self,
        text: str,
        candidates: list[str],
    ) -> list[str]:
        content = str(text or "")
        match = re.search(r"ORDER\s*[:：]\s*([a-zA-Z0-9_,\-\s]+)", content)
        if not match:
            return candidates

        ordered: list[str] = []
        seen: set[str] = set()
        for part in match.group(1).split(","):
            name = part.strip()
            if name in candidates and name not in seen:
                ordered.append(name)
                seen.add(name)

        for candidate in candidates:
            if candidate not in seen:
                ordered.append(candidate)
        return ordered if ordered else candidates

    async def _run_swarm_rearrange(
        self,
        prompt: str,
        context: ToolContext,
        provider,
        first_round: list[dict[str, str]],
        contract: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        if len(first_round) <= 1:
            return first_round

        candidates = [item["agent"] for item in first_round]
        candidate_text = "\n\n".join(
            [f"[{item['agent']}]\n{self._trim_text(item['content'], 1800)}" for item in first_round]
        )
        order_prompt = (
            f"Original task:\n{prompt}\n\n"
            "You are arranging agent execution order for a SwarmRearrange topology.\n"
            "Reply first line as ORDER:a,b,c using only these candidates.\n"
            f"Candidates: {', '.join(candidates)}\n\n"
            f"Current outputs:\n{candidate_text}"
        )
        order_result = await self._run_meta_subagent(
            context=context,
            provider=provider,
            title="Swarm Rearrange Planner",
            agent_name="plan",
            prompt=order_prompt,
            contract=contract,
        )
        ordered_agents = self._extract_rearrange_order(order_result.get("content", ""), candidates)

        by_name = {item["agent"]: item for item in first_round}
        refined_outputs: dict[str, str] = {}
        refined_items: list[dict[str, str]] = []

        for agent_name in ordered_agents:
            item = by_name.get(agent_name)
            if not item:
                continue
            owner_kwargs = await self._session_owner_kwargs(context)
            session = await SessionManager.get(item["session_id"], **owner_kwargs)
            if not session:
                refined_items.append(item)
                continue

            peer_context = "\n\n".join(
                [f"[{name}]\n{value}" for name, value in refined_outputs.items()]
            )
            rearrange_prompt = (
                f"Original task:\n{prompt}\n\n"
                f"Your previous output:\n{item['content']}\n\n"
                + (f"Prioritized peer outputs (rearranged order):\n{peer_context}\n\n" if peer_context else "")
                + "Refine your answer considering the current rearranged execution order."
            )

            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": item.get("display_name") or agent_name,
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "running",
                    "message": f"Rearrange round ({agent_name})",
                    "conversation_with": ["peer_agents"],
                },
            )
            run_info = await self._run_subagent(
                session=session,
                prompt=rearrange_prompt,
                provider=provider,
                is_resume=True,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": item.get("display_name") or agent_name,
                    "conversation_with": ["peer_agents"],
                },
            )
            refined_content = run_info.get("content", "")
            refined_outputs[agent_name] = refined_content

            updated = {
                **item,
                "content": refined_content,
            }
            refined_items.append(updated)
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": item.get("display_name") or agent_name,
                    "engine": item.get("engine", "internal_swarm"),
                    "status": "completed",
                    "message": "Rearrange refinement completed",
                    "tools_used": run_info.get("tools_used") or [],
                    "conversation_with": ["peer_agents"],
                    "last_response": self._trim_text(refined_content),
                    "context": run_info.get("context") or {},
                },
            )

        return refined_items if refined_items else first_round

    async def _run_meta_subagent(
        self,
        context: ToolContext,
        provider,
        title: str,
        prompt: str,
        agent_name: str = "plan",
        contract: Optional[dict[str, Any]] = None,
    ) -> dict[str, str]:
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )
        owner_kwargs = await self._session_owner_kwargs(context)
        session = await SessionManager.create(
            parent_id=context.session_id,
            title=title,
            agent_name=agent_name,
            workdir=self._control_workdir(context),
            **owner_kwargs,
            metadata=session_metadata,
        )
        run_info = await self._run_subagent(
            session=session,
            prompt=prompt,
            provider=provider,
            contract=contract,
            is_resume=False,
            event_context=context,
            event_payload={
                "kind": "subagent",
                "scheduler_mode": "swarm",
                "session_id": session.id,
                "agent_name": agent_name,
                "display_name": title,
                "conversation_with": ["peer_agents", "parent_agent"],
            },
        )
        if self._uses_ephemeral_helpers(context):
            await self._cleanup_subagent_session(
                session_id=session.id,
                context=context,
                agent_name=agent_name,
                scheduler_mode="swarm",
            )
        return {
            "session_id": session.id,
            "content": run_info.get("content", ""),
            "engine": "internal_swarm",
        }

    async def _run_swarm_via_swarms_router(
        self,
        description: str,
        prompt: str,
        subagents: list[str],
        context: ToolContext,
        provider,
        topology: str = "concurrent",
        contract: Optional[dict[str, Any]] = None,
    ) -> Optional[list[dict[str, str]]]:
        self._last_swarms_error = ""
        if not self._use_swarms_enabled(context):
            self._last_swarms_error = "external swarms disabled by configuration"
            return None

        model_name = self._resolve_swarms_model_name(provider)
        if not self._prepare_swarms_runtime(provider, model_name):
            self._last_swarms_error = "provider auth/runtime setup failed"
            return None

        session_map: dict[str, str] = {}
        agent_specs: list[dict[str, str]] = []
        reuse_existing = not self._uses_ephemeral_helpers(context)
        session_metadata = self._build_subagent_metadata(
            context=context,
            contract=contract,
        )

        total = len(subagents)
        for idx, agent_name in enumerate(subagents):
            display_name = self._subagent_display_name(idx, total, agent_name)
            session, is_resume = await self._get_or_create_subagent_session(
                context=context,
                agent_name=agent_name,
                title=f"{display_name} · {description} (@{agent_name} swarm/swarms)",
                contract=contract,
                reuse_existing=reuse_existing,
                metadata_extra=session_metadata,
            )
            session_map[agent_name] = session.id

            info = BuiltinAgents.get(agent_name)
            system_prompt = (
                info.prompt
                if info and info.prompt
                else f"You are {agent_name}. Complete the task precisely and concisely."
            )
            agent_specs.append(
                {
                    "name": agent_name,
                    "session_id": session.id,
                    "description": info.description if info else f"{agent_name} specialist",
                    "system_prompt": system_prompt,
                    "display_name": display_name,
                }
            )

            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session.id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "external_swarm_runtime",
                    "status": "running",
                    "message": (
                        f"{agent_name} resumed in external swarm"
                        if is_resume
                        else f"{agent_name} joined external swarm"
                    ),
                    "conversation_with": ["peer_agents", "parent_agent"],
                },
            )

        external_runtime_results = await self._run_swarm_via_external_runtime(
            description=description,
            prompt=prompt,
            context=context,
            provider=provider,
            model_name=model_name,
            topology=topology,
            subagents=subagents,
            session_map=session_map,
            agent_specs=agent_specs,
        )
        if external_runtime_results is not None:
            if self._uses_ephemeral_helpers(context):
                for agent_name, session_id in session_map.items():
                    await self._cleanup_subagent_session(
                        session_id=session_id,
                        context=context,
                        agent_name=agent_name,
                        scheduler_mode="swarm",
                    )
            return external_runtime_results

        # External runtime failed but fallback is allowed: publish explicit reason for UI.
        if str(getattr(self, "_last_swarms_error", "") or "").strip():
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": "",
                    "agent_name": "external_swarm_runtime",
                    "display_name": "ExternalSwarmRuntime",
                    "engine": "external_swarm_runtime",
                    "status": "failed",
                    "message": (
                        f"External swarm runtime unavailable: {self._last_swarms_error}. "
                        "Falling back to swarms router/internal swarm."
                    ),
                    "conversation_with": ["parent_agent"],
                },
            )

        try:
            swarms_module = importlib.import_module("swarms")
            SwarmsAgent = getattr(swarms_module, "Agent")
            swarm_router_module = importlib.import_module("swarms.structs.swarm_router")
            SwarmRouter = getattr(swarm_router_module, "SwarmRouter")
        except Exception as exc:
            for spec in agent_specs:
                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": spec.get("session_id", ""),
                        "agent_name": spec.get("name", "subagent"),
                        "display_name": spec.get("display_name") or spec.get("name", "subagent"),
                        "engine": "external_swarm_runtime",
                        "status": "failed",
                        "message": (
                            "External swarm unavailable "
                            f"({exc}), switched to internal fallback"
                        ),
                        "conversation_with": ["parent_agent"],
                    },
                )
            self._last_swarms_error = f"import failed: {exc}"
            return None

        agents: list[Any] = []
        for spec in agent_specs:
            agent = SwarmsAgent(
                agent_name=spec["name"],
                agent_description=spec["description"],
                system_prompt=spec["system_prompt"],
                model_name=model_name,
                max_loops=1,
            )
            agents.append(agent)

        swarm_type = "ConcurrentWorkflow"
        if topology == "sequential":
            swarm_type = "SequentialWorkflow"

        router = SwarmRouter(
            name=f"tcode-{context.session_id[:8]}-swarm",
            description=description or "TCode task swarm",
            agents=agents,
            swarm_type=swarm_type,
            max_loops=1,
            output_type="dict",
            multi_agent_collab_prompt=True,
        )

        try:
            raw_output = await asyncio.to_thread(router.run, prompt)
        except Exception as exc:
            for spec in agent_specs:
                await self._emit_agent_event(
                    context,
                    {
                        "kind": "subagent",
                        "scheduler_mode": "swarm",
                        "session_id": spec.get("session_id", ""),
                        "agent_name": spec.get("name", "subagent"),
                        "display_name": spec.get("display_name") or spec.get("name", "subagent"),
                        "engine": "swarms_router",
                        "status": "failed",
                        "message": (
                            "Swarms router failed "
                            f"({exc}), switched to internal fallback"
                        ),
                        "conversation_with": ["parent_agent"],
                    },
                )
            self._last_swarms_error = f"router.run failed: {exc}"
            return None

        self._normalize_swarms_history_files(context)

        normalized = self._normalize_swarms_output(raw_output, subagents)
        results = [
            {
                "agent": name,
                "session_id": session_map.get(name, ""),
                "content": normalized.get(name, ""),
                "engine": "swarms_router",
            }
            for name in subagents
        ]
        if self._uses_ephemeral_helpers(context):
            for agent_name, session_id in session_map.items():
                await self._cleanup_subagent_session(
                    session_id=session_id,
                    context=context,
                    agent_name=agent_name,
                    scheduler_mode="swarm",
                )
        return results

    async def _run_swarm_via_external_runtime(
        self,
        description: str,
        prompt: str,
        context: ToolContext,
        provider,
        model_name: str,
        topology: str,
        subagents: list[str],
        session_map: dict[str, str],
        agent_specs: list[dict[str, str]],
    ) -> Optional[list[dict[str, str]]]:
        try:
            from ...swarm import ExternalSwarmRuntime, ExternalSwarmRuntimeError
        except Exception as exc:
            self._last_swarms_error = f"external runtime import failed: {exc}"
            return None

        workdir = context.extra.get("workdir") if context.extra else None
        runtime_rounds_raw = "3"
        if context.extra and context.extra.get("external_swarm_rounds") is not None:
            runtime_rounds_raw = str(context.extra.get("external_swarm_rounds"))
        else:
            runtime_rounds_raw = os.environ.get("TCODE_EXTERNAL_SWARM_ROUNDS", "3")

        try:
            runtime_rounds = int(runtime_rounds_raw)
        except Exception:
            runtime_rounds = 3

        bus_backend = "memory"
        if context.extra and context.extra.get("external_swarm_bus") is not None:
            bus_backend = str(context.extra.get("external_swarm_bus") or "memory")
        else:
            bus_backend = str(os.environ.get("TCODE_EXTERNAL_SWARM_BUS", "memory") or "memory")

        communication_mode = "hybrid"
        if context.extra and context.extra.get("external_swarm_communication_mode") is not None:
            communication_mode = str(
                context.extra.get("external_swarm_communication_mode") or "hybrid"
            )
        else:
            communication_mode = str(
                os.environ.get("TCODE_EXTERNAL_SWARM_COMMUNICATION_MODE", "hybrid")
                or "hybrid"
            )

        timeout_raw = "90"
        if context.extra and context.extra.get("external_swarm_agent_timeout_seconds") is not None:
            timeout_raw = str(context.extra.get("external_swarm_agent_timeout_seconds"))
        else:
            timeout_raw = str(os.environ.get("TCODE_EXTERNAL_SWARM_AGENT_TIMEOUT_SECONDS", "90"))
        try:
            agent_timeout_seconds = float(timeout_raw)
        except Exception:
            agent_timeout_seconds = 90.0

        retries_raw = "2"
        if context.extra and context.extra.get("external_swarm_max_retries") is not None:
            retries_raw = str(context.extra.get("external_swarm_max_retries"))
        else:
            retries_raw = str(os.environ.get("TCODE_EXTERNAL_SWARM_MAX_RETRIES", "2"))
        try:
            max_retries = int(retries_raw)
        except Exception:
            max_retries = 2

        fallback_raw = "1"
        if context.extra and context.extra.get("external_swarm_fallback_on_error") is not None:
            fallback_raw = str(context.extra.get("external_swarm_fallback_on_error"))
        else:
            fallback_raw = str(os.environ.get("TCODE_EXTERNAL_SWARM_FALLBACK_ON_ERROR", "1"))
        fallback_on_error = fallback_raw.strip().lower() not in {"0", "false", "off", "no"}

        display_name_by_agent = {
            str(spec.get("name", "")): str(spec.get("display_name") or spec.get("name") or "subagent")
            for spec in agent_specs
        }
        owner_kwargs = await self._session_owner_kwargs(context)

        # Invocation-local evidence survives text-only communication/refinement
        # rounds without borrowing calls from a reused session's earlier tasks.
        initial_call_ids: dict[str, set[str]] = {}
        agent_evidence: dict[str, dict[str, Any]] = {}

        async def _execute_agent_turn(
            agent_name: str,
            session_id: str,
            turn_prompt: str,
            round_index: int,
        ) -> dict[str, Any]:
            session = await SessionManager.get(session_id, **owner_kwargs)
            if not session:
                raise RuntimeError(f"Subagent session not found: {session_id}")
            if agent_name not in initial_call_ids:
                initial_call_ids[agent_name] = {
                    str(item.get("call_id") or "")
                    for item in self._build_tool_trace(session.messages)
                }

            display_name = display_name_by_agent.get(agent_name, agent_name)
            inbox_messages = self._parse_swarm_inbox_messages(turn_prompt)
            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "external_swarm_runtime",
                    "status": "running",
                    "message": f"External swarm execution round {round_index}",
                    "conversation_with": ["peer_agents", "parent_agent"],
                    "received_messages": inbox_messages,
                },
            )

            run_info = await self._run_subagent(
                session=session,
                prompt=turn_prompt,
                provider=provider,
                is_resume=True,
                event_context=context,
                event_payload={
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "conversation_with": ["peer_agents", "parent_agent"],
                },
            )

            previous = agent_evidence.get(agent_name, {})
            traces = {
                str(item["call_id"]): item
                for item in [*previous.get("tool_trace", []), *run_info.get("tool_trace", [])]
                if item.get("call_id") and str(item["call_id"]) not in initial_call_ids[agent_name]
            }
            agent_evidence[agent_name] = {
                **run_info,
                "tool_trace": list(traces.values()),
                "tools_used": list(dict.fromkeys(
                    str(item["tool_name"]) for item in traces.values() if item.get("tool_name")
                )),
                "status": run_info.get("outcome_status") or "completed",
            }

            await self._emit_agent_event(
                context,
                {
                    "kind": "subagent",
                    "scheduler_mode": "swarm",
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "display_name": display_name,
                    "engine": "external_swarm_runtime",
                    "status": "completed",
                    "message": f"External swarm execution round {round_index} completed",
                    "tools_used": run_info.get("tools_used") or [],
                    "conversation_with": ["peer_agents", "parent_agent"],
                    "last_response": self._trim_text(str(run_info.get("content", ""))),
                    "context": run_info.get("context") or {},
                    "received_messages": inbox_messages,
                    "returned_messages": self._parse_p2p_messages(
                        text=self._extract_tagged_block(str(run_info.get("content", "")), "peer_messages"),
                        sender=agent_name,
                        candidates=subagents,
                    ),
                },
            )

            return run_info

        loop = asyncio.get_running_loop()
        runtime = ExternalSwarmRuntime(
            run_id=f"{context.session_id[:8]}-{int(loop.time())}",
            model_name=model_name,
            topology=topology,
            max_rounds=runtime_rounds,
            bus_backend=bus_backend,
            agent_timeout_seconds=agent_timeout_seconds,
            max_retries=max_retries,
            fallback_on_error=fallback_on_error,
            execute_agent_turn=_execute_agent_turn,
            session_id=context.session_id,
        )

        try:
            run_result = await runtime.run(
                task=prompt,
                description=description,
                agent_specs=agent_specs,
                workdir=workdir or os.getcwd(),
                constraints={
                    "strict_p2p": "TO:<agent> MESSAGE:<content>",
                    "topology": topology,
                    "communication_mode": communication_mode,
                },
            )
        except ExternalSwarmRuntimeError as exc:
            from ...swarm.observability import SwarmTraceStore
            SwarmTraceStore.finish(
                runtime.run_id,
                status="failed",
                metrics={},
                error=str(exc),
                workdir=workdir or os.getcwd(),
            )
            self._last_swarms_error = f"external runtime failed: {exc}"
            return None
        except Exception as exc:
            from ...swarm.observability import SwarmTraceStore
            SwarmTraceStore.finish(
                runtime.run_id,
                status="failed",
                metrics={},
                error=str(exc),
                workdir=workdir or os.getcwd(),
            )
            self._last_swarms_error = f"external runtime exception: {exc}"
            return None

        outputs = run_result.get("outputs") or {}
        metrics = run_result.get("metrics") or {}
        topology_metrics = metrics.get("topology_metrics") if isinstance(metrics, dict) else None

        if isinstance(topology_metrics, dict):
            first_agent = subagents[0] if subagents else ""
            first_session = session_map.get(first_agent, "")
            if first_session:
                if topology == "voting" and topology_metrics.get("winner"):
                    winner = str(topology_metrics.get("winner") or "")
                    tally = topology_metrics.get("tally")
                    tally_text = ""
                    if isinstance(tally, dict):
                        lines = [f"{k}:{v}" for k, v in tally.items()]
                        tally_text = "; ".join(lines)
                    await self._emit_agent_event(
                        context,
                        {
                            "kind": "subagent",
                            "scheduler_mode": "swarm",
                            "session_id": first_session,
                            "agent_name": first_agent,
                            "display_name": display_name_by_agent.get(first_agent, first_agent),
                            "engine": "external_swarm_runtime",
                            "status": "completed",
                            "message": f"External voting winner: {winner}" + (f" ({tally_text})" if tally_text else ""),
                            "conversation_with": ["peer_agents", "parent_agent"],
                        },
                    )

                if topology == "rearrange" and isinstance(topology_metrics.get("order"), list):
                    order = [str(item) for item in topology_metrics.get("order") or [] if str(item)]
                    if order:
                        await self._emit_agent_event(
                            context,
                            {
                                "kind": "subagent",
                                "scheduler_mode": "swarm",
                                "session_id": first_session,
                                "agent_name": first_agent,
                                "display_name": display_name_by_agent.get(first_agent, first_agent),
                                "engine": "external_swarm_runtime",
                                "status": "completed",
                                "message": f"External rearrange order: {' -> '.join(order)}",
                                "conversation_with": ["peer_agents", "parent_agent"],
                            },
                        )

        return [
            {
                **agent_evidence.get(name, {}),
                "agent": name,
                "session_id": session_map.get(name, ""),
                "content": str(outputs.get(name, "")),
                "engine": "external_swarm_runtime",
                "metrics": metrics,
            }
            for name in subagents
        ]

    def _normalize_swarms_output(
        self,
        output: Any,
        subagents: list[str],
    ) -> dict[str, str]:
        if isinstance(output, dict):
            normalized: dict[str, str] = {}
            for agent_name in subagents:
                value = output.get(agent_name)
                if value is None:
                    lowered = {str(k).lower(): v for k, v in output.items()}
                    value = lowered.get(agent_name.lower())
                normalized[agent_name] = "" if value is None else str(value)
            return normalized

        if isinstance(output, list):
            normalized = {}
            for idx, agent_name in enumerate(subagents):
                value = output[idx] if idx < len(output) else ""
                normalized[agent_name] = "" if value is None else str(value)
            return normalized

        return {agent_name: str(output) for agent_name in subagents}

    async def _run_subagent(
        self,
        session,
        prompt: str,
        provider,
        contract: Optional[dict[str, Any]] = None,
        is_resume: bool = False,
        event_context: Optional[ToolContext] = None,
        event_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run one child turn in its own cancellable task."""
        from ...server.state import ServerState
        from ...runtime.capacity import ExecutionCapacityController

        request_id = ""
        if event_context and isinstance(getattr(event_context, "extra", None), dict):
            request_id = str(
                event_context.extra.get("request_id")
                or event_context.extra.get("parent_request_id")
                or ""
            ).strip()
        lease = await ExecutionCapacityController.current().acquire(
            kind="subagent",
            tenant_id=str(getattr(session, "tenant_id", "") or ""),
            user_id=str(getattr(session, "user_id", "") or ""),
            session_id=str(getattr(session, "id", "") or ""),
            request_id=request_id or str(getattr(session, "id", "") or ""),
        )

        child_task = asyncio.create_task(
            self._run_subagent_body(
                session=session,
                prompt=prompt,
                provider=provider,
                contract=contract,
                is_resume=is_resume,
                event_context=event_context,
                event_payload=event_payload,
            ),
            name=f"subagent-turn-{session.id}",
        )
        ServerState.set_running(session.id, True)
        ServerState.set_turn_task(session.id, child_task)
        try:
            timeout_seconds = self._subagent_turn_timeout_seconds()
            if timeout_seconds > 0:
                done, _ = await asyncio.wait(
                    {child_task},
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if child_task in done:
                    # Await the completed child directly so an inner timeout
                    # (for example the harness LLM timeout) keeps its original
                    # error instead of being mislabeled as the outer turn cap.
                    return await child_task
                child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
                raise TimeoutError(
                    f"subagent turn timed out after {timeout_seconds:g}s: {session.id}"
                )
            return await child_task
        except asyncio.CancelledError as exc:
            if not child_task.done():
                child_task.cancel()
            await asyncio.gather(child_task, return_exceptions=True)
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            raise RuntimeError(f"subagent run cancelled: {session.id}") from exc
        finally:
            registered = ServerState.get_turn_task(session.id)
            if registered is child_task:
                ServerState.set_turn_task(session.id, None)
            ServerState.set_running(session.id, False)
            lease.release()

    async def _run_subagent_body(
        self,
        session,
        prompt: str,
        provider,
        contract: Optional[dict[str, Any]] = None,
        is_resume: bool = False,
        event_context: Optional[ToolContext] = None,
        event_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run the subagent synchronously (waits for completion)"""
        from ...agent.agent import Agent

        # Resolve the agent contract before allocating filesystem resources.
        agent_info = BuiltinAgents.get(session.agent_name)
        if not agent_info:
            raise Exception(f"Unknown agent: {session.agent_name}")
        from ...team.session_bootstrap import ensure_session_team_workers

        await ensure_session_team_workers(
            session_id=str(session.id),
            agent_name=str(session.agent_name or ""),
            workdir=str(
                getattr(session, "workdir", "")
                or self._control_workdir(event_context)
            ),
            owner={
                key: str(getattr(session, key, "") or "").strip()
                for key in ("tenant_id", "user_id", "workspace_id")
                if str(getattr(session, key, "") or "").strip()
            },
        )
        if not contract:
            maybe_contract = getattr(session, "metadata", {}) or {}
            if isinstance(maybe_contract, dict):
                contract = maybe_contract.get("contract") if isinstance(maybe_contract.get("contract"), dict) else {}
        if contract:
            agent_info = self._apply_allowed_tools_to_agent(
                agent_info,
                contract.get("allowed_tools", "all"),
            )

        persisted_metadata = dict(getattr(session, "metadata", {}) or {})
        original_resource_context = {}
        if isinstance(contract, dict) and isinstance(contract.get("resources"), dict):
            original_resource_context = dict(contract.get("resources") or {})
        elif isinstance(persisted_metadata.get("resource_context"), dict):
            original_resource_context = dict(
                persisted_metadata.get("resource_context") or {}
            )

        control_workdir = str(
            (getattr(session, "metadata", {}) or {}).get("control_workdir")
            or getattr(session, "workdir", "")
            or os.environ.get("TCODE_WORKDIR")
            or os.getcwd()
        ).strip()
        agent_workspace = AgentWorkspaceManager.create(
            control_workdir=control_workdir,
            task_id=str(getattr(session, "id", "") or ""),
            session_id=str(getattr(session, "id", "") or ""),
            kind="task-subagent",
            strategy=str(persisted_metadata.get("workspace_strategy") or "auto"),
            domain=str(persisted_metadata.get("domain") or ""),
            agent_name=str(getattr(session, "agent_name", "") or ""),
            resource_context=original_resource_context,
            task_contract=contract,
        )
        execution_resource_context = dict(
            agent_workspace.resource_context or original_resource_context
        )
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        session_metadata["agent_workspace"] = agent_workspace.to_dict()
        session_metadata["control_workdir"] = control_workdir
        session_metadata["workspace_resource_context"] = execution_resource_context
        session.metadata = session_metadata
        await SessionManager.update(
            session.id,
            tenant_id=str(getattr(session, "tenant_id", "") or ""),
            user_id=str(getattr(session, "user_id", "") or ""),
            workspace_id=str(getattr(session, "workspace_id", "") or ""),
            metadata=session_metadata,
        )

        subagent_provider = None
        agent = None
        workspace_outcome = "failed"
        workspace_checkpoint = ""
        run_result: Optional[dict[str, Any]] = None
        try:
            # Provider/session state stays in the control workspace; only tool
            # execution is redirected to the selected isolated workspace.
            subagent_provider = clone_provider(provider)
            agent = Agent(
                agent_info=agent_info,
                provider=subagent_provider,
                session_id=session.id,
                workdir=agent_workspace.execution_workdir,
                existing_session=session,
                question_policy=(event_context.extra or {}).get("__question_policy__", "ask")
                if event_context else "ask",
            )
            if event_context:
                async def forward_browser_session_event(
                    payload: dict[str, Any],
                ) -> None:
                    """Forward live-browser lifecycle events during child execution.

                    TaskTool otherwise observes a child's tool calls only when
                    ``run_stream`` yields the assistant item.  Browser live view
                    must be announced while the browser tool is actually
                    running so the parent chat can connect before the action
                    finishes.
                    """
                    await self._forward_subagent_browser_session_event(
                        event_context=event_context,
                        session=session,
                        payload=payload,
                    )

                agent.set_agent_event_emitter(forward_browser_session_event)
            if event_context and isinstance(getattr(event_context, "extra", None), dict):
                agent.set_execution_profile(event_context.extra.get("execution_profile"))
            if execution_resource_context:
                agent.set_session_resource_context(execution_resource_context)

            parent_question_ask = None
            parent_question_resolve = None
            if event_context and isinstance(getattr(event_context, "extra", None), dict):
                parent_question_ask = event_context.extra.get("__question_ask__")
                parent_question_resolve = event_context.extra.get("__question_resolve__")
            if callable(parent_question_ask) or callable(parent_question_resolve):
                display_name = ""
                if isinstance(event_payload, dict):
                    display_name = str(event_payload.get("display_name") or "").strip()
                agent.set_question_handler(
                    build_parent_question_bridge(
                        child_session_id=session.id,
                        child_agent_name=session.agent_name,
                        child_display_name=display_name,
                        parent_session_id=str(getattr(session, "parent_id", "") or ""),
                        task_id=session.id,
                        parent_question_ask=parent_question_ask,
                        parent_question_resolve=parent_question_resolve,
                    )
                )

            tools_seen: set[str] = set()
            tool_call_seen: set[tuple[str, str]] = set()
            final_content = ""

            workspace_hint = AgentWorkspaceManager.execution_prompt_hint(agent_workspace)
            execution_prompt = (
                f"{prompt}\n\n{workspace_hint}" if workspace_hint else prompt
            )
            async for item in agent.run_stream(execution_prompt):
                if item.role == "assistant":
                    final_content = item.content or final_content

                    for tc in (item.tool_calls or []):
                        tool_name = str(getattr(tc, "tool_name", "") or "").strip()
                        tool_args = getattr(tc, "arguments", None) or {}
                        key = (tool_name, str(tool_args))
                        if not tool_name or key in tool_call_seen:
                            continue
                        tool_call_seen.add(key)
                        tools_seen.add(tool_name)
                        if event_context and event_payload:
                            await self._emit_agent_event(
                                event_context,
                                {
                                    **event_payload,
                                    "contract": contract or {},
                                    "status": "running",
                                    "message": f"Using tool: {tool_name}",
                                    "tool_name": tool_name,
                                    "tool_args": tool_args,
                                    "tools_used": sorted(tools_seen),
                                    "context": self._build_context_snapshot(agent.messages),
                                },
                            )

            if not final_content:
                for msg in reversed(agent.messages):
                    if getattr(msg, "role", "") == "assistant" and getattr(msg, "content", ""):
                        final_content = msg.content
                        break

            await SessionManager.update(
                session.id,
                tenant_id=str(getattr(session, "tenant_id", "") or ""),
                user_id=str(getattr(session, "user_id", "") or ""),
                workspace_id=str(getattr(session, "workspace_id", "") or ""),
                messages=list(agent.messages),
            )

            tool_trace = self._build_tool_trace(agent.messages)
            delegated_environment = self._extract_delegated_execution_environment(
                tool_trace,
                session_id=str(getattr(session, "id", "") or ""),
                agent_name=str(getattr(session, "agent_name", "") or ""),
            )
            outcome_status = str(getattr(agent, "_last_outcome_status", "") or "completed").strip().lower() or "completed"
            checkpoint_handoff = build_subtask_handoff(
                result_text=final_content,
                tool_trace=tool_trace,
                child_outcome_status=outcome_status,
                task_id=str(getattr(session, "id", "") or ""),
                session_id=str(getattr(session, "id", "") or ""),
                agent_name=str(getattr(session, "agent_name", "") or ""),
                request_text=prompt,
            )
            claim_validation = dict(checkpoint_handoff.claim_validation or {})
            if claim_validation.get("required") and not claim_validation.get("valid"):
                outcome_status = checkpoint_handoff.child_outcome_status
                final_content = checkpoint_handoff.result_text
            if checkpoint_handoff.outcome_status == "awaiting_input":
                outcome_status = "awaiting_input"
                final_content = checkpoint_handoff.render_for_parent()

            if outcome_status in {"completed", "failed", "cancelled", "blocked"}:
                workspace_outcome = outcome_status
            else:
                workspace_outcome = ""
                workspace_checkpoint = outcome_status or "paused"

            # Return content and tool metadata
            run_result = {
                "content": final_content,
                "tools_used": self._extract_tools_used(agent.messages),
                "tool_trace": tool_trace,
                "delegated_execution_environment": delegated_environment,
                "context": self._build_context_snapshot(agent.messages),
                "contract": contract or {},
                "outcome_status": outcome_status,
                "subtask_handoff": checkpoint_handoff.to_dict(),
                "delegated_claim_validation": claim_validation,
            }
        except asyncio.CancelledError:
            workspace_outcome = "cancelled"
            raise
        finally:
            tool_trace_for_provenance = (
                list(run_result.get("tool_trace") or [])
                if isinstance(run_result, dict)
                else (
                    self._build_tool_trace(agent.messages)
                    if agent is not None
                    else []
                )
            )
            AgentWorkspaceManager.record_provenance(
                agent_workspace,
                agent=agent,
                tool_trace=tool_trace_for_provenance,
                attempt=1,
                outcome=workspace_outcome or workspace_checkpoint,
            )
            if workspace_outcome:
                AgentWorkspaceManager.finalize(
                    agent_workspace,
                    outcome=workspace_outcome,
                )
            else:
                AgentWorkspaceManager.checkpoint(
                    agent_workspace,
                    status=workspace_checkpoint or "paused",
                    tool_trace=tool_trace_for_provenance,
                    pending_action=(
                        str(
                            (run_result or {})
                            .get("subtask_handoff", {})
                            .get("next_action", "")
                        )
                        if isinstance((run_result or {}).get("subtask_handoff"), dict)
                        else ""
                    ),
                )
            latest_metadata = dict(getattr(session, "metadata", {}) or {})
            latest_metadata["agent_workspace"] = agent_workspace.to_dict()
            session.metadata = latest_metadata
            try:
                await SessionManager.update(
                    session.id,
                    tenant_id=str(getattr(session, "tenant_id", "") or ""),
                    user_id=str(getattr(session, "user_id", "") or ""),
                    workspace_id=str(getattr(session, "workspace_id", "") or ""),
                    metadata=latest_metadata,
                )
            except Exception:
                pass
            # Always close the provider to prevent resource leaks.
            await self._close_subagent_provider(subagent_provider)

        if run_result is None:
            raise RuntimeError(f"subagent produced no result: {session.id}")
        run_result["agent_workspace"] = agent_workspace.to_dict()
        return run_result

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


class TaskWaitTool(Tool):
    """Inspect or wait for ordinary background supervisor tasks."""

    @property
    def id(self) -> str:
        return "task_wait"

    @property
    def description(self) -> str:
        return (
            "Inspect or wait for one or more background task calls created with "
            "task(wait=false). timeout_seconds=0 returns an immediate snapshot."
        )

    @property
    def parameters(self) -> type:
        return TaskWaitParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TaskWaitParameters(**args)
        raw_ids = (
            parsed.task_ids
            if isinstance(parsed.task_ids, list)
            else [item.strip() for item in str(parsed.task_ids or "").split(",")]
        )
        task_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in raw_ids
                if str(item or "").strip()
            )
        )
        if not task_ids:
            return ToolResult(title="Task wait", output="", error="task_ids is required")
        if len(task_ids) > 16:
            return ToolResult(title="Task wait", output="", error="at most 16 task_ids are allowed")

        records: list[dict[str, Any]] = []
        for task_id in task_ids:
            record = _BACKGROUND_SUBAGENT_TASKS.get(task_id)
            if record is None or str(record.get("parent_session_id") or "") != str(
                context.session_id or ""
            ):
                return ToolResult(
                    title="Task wait",
                    output="",
                    error=f"background task not found: {task_id}",
                )
            records.append(record)

        try:
            timeout = max(0.0, min(float(parsed.timeout_seconds or 0), 3600.0))
        except (TypeError, ValueError):
            timeout = 0.0
        pending = [record["task"] for record in records if not record["task"].done()]
        if pending and timeout > 0:
            await asyncio.wait(pending, timeout=timeout)

        snapshots: list[dict[str, Any]] = []
        output_blocks: list[str] = []
        for record in records:
            task_id = str(record.get("task_id") or "")
            task: asyncio.Task = record["task"]
            session_id = str(record.get("session_id") or "")
            if not task.done():
                status = "running"
                result = None
                error = ""
            elif task.cancelled():
                status = "cancelled"
                result = None
                error = ""
            else:
                try:
                    result = task.result()
                    error = str(result.error or "")
                    status = (
                        "failed"
                        if error
                        else str((result.metadata or {}).get("status") or "completed")
                    )
                except Exception as exc:
                    result = None
                    status = "failed"
                    error = str(exc)
            snapshot = {
                "task_id": task_id,
                "session_id": session_id,
                "status": status,
                "done": task.done(),
                "error": error[:1000],
            }
            snapshots.append(snapshot)
            if result is not None:
                output_blocks.append(
                    f'<task_result task_id="{task_id}" session_id="{session_id}">\n'
                    f"{result.output}\n</task_result>"
                )
            else:
                output_blocks.append(
                    f"task_id={task_id} session_id={session_id or '-'} status={status}"
                    + (f" error={error[:500]}" if error else "")
                )

        return ToolResult(
            title="Task wait",
            output="\n\n".join(output_blocks),
            metadata={
                "tasks": snapshots,
                "all_done": all(item["done"] for item in snapshots),
                "should_end_turn": False,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
