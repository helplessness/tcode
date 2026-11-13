"""Main Agent implementation with React loop"""

import asyncio
import fnmatch
import json
import logging
import os
import platform
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from copy import deepcopy
from typing import Any, AsyncIterator, Optional, Awaitable, Callable, Mapping

from ..prompt.templates import load_prompt_template
from ..config.manager import ConfigManager
from ..config.task_snapshot import TaskConfigSnapshot, TaskConfigSnapshotStore
from ..harness import AgentHarnessEngine, AgentPromptBuilder, AgentToolExecutor
from ..context.engine import get_default_context_engine
from ..runtime.execution_profile import (
    get_execution_profile,
    normalize_execution_profile,
    profile_allows_tool,
)
from ..runtime.question_policy import normalize_question_policy
from ..types import (
    AgentInfo,
    AgentMode,
    Message,
    ToolCall,
    ToolResult,
    ToolState,
    Usage,
)
from ..tools.registry import ToolRegistry
from ..mcp.manager_auto import MCPManager
from ..mcp.agent_specific_mcp import get_agent_specific_mcp_manager
from ..plugins.manager import PluginManager
from ..providers.base import LLMProvider, ChatMessage, ToolDefinition


class Agent:
    """Main agent class implementing the react loop"""

    # 用途：初始化 Agent 的会话、模型、权限、编排、记忆与工具执行状态。
    def __init__(
        self,
        agent_info: AgentInfo,
        provider: LLMProvider,
        session_id: str,
        workdir: Optional[str] = None,
        existing_session: Optional[Any] = None,  # Existing Session to continue
        *,
        question_policy: str = "ask",
    ):
        self.agent_info = agent_info
        self.provider = provider
        self.session_id = session_id
        self.workdir = workdir
        self._existing_session = existing_session
        # Runtime-only opt-in: reopening a saved session must not inherit consent.
        self._question_policy = normalize_question_policy(question_policy)

        # Use existing session's messages if provided
        if existing_session and hasattr(existing_session, "messages"):
            self.messages = list(existing_session.messages)
        else:
            self.messages = []
        self._abort_event = asyncio.Event()  ###中止事件标志
        self._plugin_hook_tasks: set[asyncio.Task[None]] = set()

        # Track usage
        self.total_usage = Usage()  ### token流量追踪

        # Runtime permission integration (e.g., web frontend prompt)
        ### 运行时权限处理
        self._permission_handler: Optional[
            Callable[[str, dict[str, Any]], Awaitable[str | bool]]
        ] = None
        ### 交互式问题处理
        self._question_handler: Optional[Callable[[dict[str, Any]], Awaitable[str]]] = (
            None
        )
        ### 使用工具
        self._always_allowed_tools: set[str] = set()
        self._always_allowed_permission_patterns: dict[str, set[str]] = {}
        self._output_format: Optional[dict[str, Any]] = None
        self._last_outcome_status: str = ""
        self._last_outcome_validation: dict[str, Any] = {}
        self._last_outcome_contract: dict[str, Any] = {}
        self._last_outcome_plan: dict[str, Any] = {}
        self._last_proof_pack: dict[str, Any] = {}
        self._active_outcome_contract: Any = None
        self._active_outcome_plan: Any = None
        self._superseded_outcome_contracts: dict[
            tuple[str, int], dict[str, Any]
        ] = {}
        self._scheduler_mode: str = "supervisor"
        self._reasoning_effort: Optional[str] = None
        self._execution_profile: Optional[str] = None
        self._use_swarms: Optional[bool] = None
        self._swarm_topology: str = "concurrent"  ##拓扑结构，顺序执行，并行，投票，重排序
        self._external_swarm_communication_mode: Optional[str] = None
        self._external_swarm_bus: Optional[str] = None
        self._external_swarm_rounds: Optional[int] = None
        self._external_swarm_agent_timeout_seconds: Optional[float] = None
        self._external_swarm_max_retries: Optional[int] = None
        self._external_swarm_fallback_on_error: Optional[bool] = None
        self._route_symphony_on_mcp_tool: Optional[bool] = None
        self._agent_event_emitter: Optional[
            Callable[[dict[str, Any]], Awaitable[None]]
        ] = None
        self._llm_text_delta_handler: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self._recommended_skills: list[dict[str, Any]] = []
        self._skill_routing_trace: dict[str, Any] = {}
        self._skill_composite_plan: dict[str, Any] = {}
        self._skill_routing_message_id: str = ""
        # 稳定 Turn/Trace 用于研究账本；阶段 8 的受控上下文由 PromptBuilder
        # 按 Owner、课题相关性、预算和 Conflict Gate 单独选择。
        self._active_research_turn_id: str = ""
        self._active_research_trace_id: str = ""
        self._active_client_request_id: str = ""
        self._research_context_cache_key: str = ""
        self._research_context_cache: Any = None
        self._last_research_continuation_context: dict[str, Any] = {}
        self._research_route_prompt_expected_turn_id: str = ""
        self._pending_research_route_envelope: dict[str, Any] = {}
        self._research_route_committed_turn_id: str = ""
        self._last_research_turn_route: dict[str, Any] = {}
        self._research_route_model_fallback_calls: dict[str, int] = {}
        self._last_research_routing_context: dict[str, Any] = {}
        self._research_pack_context_cache_key: str = ""
        self._research_pack_context_cache: Any = None
        self._last_research_grounding: dict[str, Any] = {}
        self._context_engine = get_default_context_engine()
        self._last_context_assembly: dict[str, Any] = {}
        self._last_tool_routing: dict[str, Any] = {}
        self._session_resource_context: dict[str, Any] = {}
        self._active_structure_context: dict[str, Any] | None = None
        self._next_user_input_images: list[dict[str, Any]] = []
        self._next_user_message_metadata: dict[str, Any] = {}
        self._next_user_message_id: str = ""
        self._guidance_port: Any = None
        self._guidance_owner_id: str = ""
        self._guidance_stream_epoch: int = 0
        session_metadata = (
            dict(getattr(existing_session, "metadata", {}) or {})
            if existing_session
            else {}
        )
        self._config_snapshot: Optional[TaskConfigSnapshot] = None
        snapshot_id = str(session_metadata.get("config_snapshot_id") or "").strip()
        if snapshot_id:
            snapshot_workdir = str(
                session_metadata.get("config_snapshot_workdir")
                or session_metadata.get("control_workdir")
                or workdir
                or os.getcwd()
            ).strip()
            try:
                self._config_snapshot = TaskConfigSnapshotStore.get(
                    snapshot_id,
                    snapshot_workdir,
                )
            except Exception:
                self._config_snapshot = None
        if isinstance(session_metadata.get("resource_context"), dict):
            self._session_resource_context = dict(
                session_metadata.get("resource_context") or {}
            )
        shared_memory_key = str(session_metadata.get("shared_memory_key") or "").strip()
        self._shared_memory_source = str(
            session_metadata.get("shared_memory_source") or ""
        ).strip()
        try:
            from ..team.registry import WorkerRegistry

            worker = WorkerRegistry.get(session_id)
        except Exception:
            worker = None
        if not shared_memory_key and worker:
            shared_memory_key = str(getattr(worker, "memory_key", "") or "").strip()
            if not shared_memory_key:
                derived_team_id = str(getattr(worker, "team_id", "") or "").strip()
                derived_name = str(getattr(worker, "name", "") or "").strip().lower()
                if derived_team_id and derived_name:
                    shared_memory_key = f"{derived_team_id}:{derived_name}"
                else:
                    derived_session_id = str(getattr(worker, "session_id", "") or "").strip()
                    if derived_session_id:
                        shared_memory_key = f"worker:{derived_session_id}"
        if not self._shared_memory_source and worker and getattr(worker, "name", None):
            self._shared_memory_source = f"worker:{worker.name}"
        self._shared_memory_key = shared_memory_key
        self._resident_generalist = bool(
            session_metadata.get("resident_generalist", False)
        )
        self._direct_execution_mode = bool(
            session_metadata.get("direct_execution_mode", False)
        )
        self._direct_execution_scope = str(
            session_metadata.get("direct_execution_scope") or ""
        ).strip()
        task_child_policy = str(
            session_metadata.get("task_child_policy") or ""
        ).strip().lower()
        if task_child_policy not in {"persistent", "ephemeral"}:
            if self._resident_generalist and self._shared_memory_key:
                task_child_policy = "ephemeral"
            else:
                task_child_policy = "persistent"
        self._task_child_policy = task_child_policy
        self._plan_mode: bool = bool(session_metadata.get("plan_mode", False))
        self._plan_mode_source: str = str(
            session_metadata.get("plan_mode_source")
            or agent_info.name
            or "build"
        ).strip() or "build"
        self._prompt_builder = AgentPromptBuilder(self)
        self._tool_executor = AgentToolExecutor(self)
        self._engine = AgentHarnessEngine(self)

        # Hook: agent.create
        self._emit_agent_hook("agent.create", {
            "agent": self.agent_info.name,
            "mode": self.agent_info.mode.value,
            "session_id": self.session_id,
        })

    # 用途：注入运行时权限确认回调，处理需要用户授权的工具调用。
    def set_permission_handler(
        self,
        handler: Optional[Callable[[str, dict[str, Any]], Awaitable[str | bool]]],
    ):
        """用途：设置运行时权限处理器，用于 ask-mode 工具调用
            参数：handler - 接收工具名和参数的异步回调函数"""
        self._permission_handler = handler

    # 用途：注入交互式问题回调，让 Agent 能等待并接收用户回答。
    def set_question_handler(
        self,
        handler: Optional[Callable[[dict[str, Any]], Awaitable[str]]],
    ):
        """
        用途：设置交互式问题处理器
        参数：handler - 接收问题载荷的异步回调函数
        """
        self._question_handler = handler

    def set_question_policy(self, policy: str) -> None:
        """Set Question automation for this Agent only; permissions are unchanged."""
        self._question_policy = normalize_question_policy(policy)

    # 用途：设置子任务采用主管、链式、群组或自动群组等调度模式。
    def set_scheduler_mode(self, mode: Optional[str]):
        """设置子代理编排的任务调度模式
        有效值：supervisor, chain, swarm, auto_swarm_builder"""
        normalized = str(mode or "supervisor").strip().lower()
        if normalized not in {"supervisor", "chain", "swarm", "auto_swarm_builder"}:
            normalized = "supervisor"
        self._scheduler_mode = normalized

    # 用途：规范并保存模型推理强度，非法值会恢复为未指定状态。
    def set_reasoning_effort(self, effort: Optional[str]):
        normalized = str(effort or "").strip().lower()
        if normalized not in {"minimal", "low", "medium", "high"}:
            self._reasoning_effort = None
            return
        self._reasoning_effort = normalized

    # 用途：设置当前 Agent 的执行配置档，用于约束可用工具和运行策略。
    def set_execution_profile(self, profile: Optional[str]):
        normalized = normalize_execution_profile(profile)
        if not normalized:
            self._execution_profile = None
            return
        self._execution_profile = normalized

    # 用途：更新会话资源上下文，供文件识别、问题补全和工作流路由使用。
    def set_session_resource_context(self, resource_context: Optional[dict[str, Any]]):
        if isinstance(resource_context, dict):
            self._session_resource_context = dict(resource_context)
        else:
            self._session_resource_context = {}

    def bind_config_snapshot(self, snapshot: TaskConfigSnapshot) -> None:
        """Bind this Agent, its tools and inherited async workers to one config."""

        self._config_snapshot = snapshot

    def set_next_user_input_images(self, images: Optional[list[dict[str, Any]]]) -> None:
        """Attach a bounded image set to the next user message only."""
        self._next_user_input_images = [
            dict(item)
            for item in (images or [])[:2]
            if isinstance(item, dict)
        ]

    def consume_next_user_input_images(self) -> list[dict[str, Any]]:
        images = list(self._next_user_input_images)
        self._next_user_input_images = []
        return images

    def set_next_user_message_context(
        self,
        *,
        metadata: Optional[dict[str, Any]] = None,
        message_id: str = "",
    ) -> None:
        """Attach durable control metadata and an optional ID to the next user turn."""

        self._next_user_message_metadata = dict(metadata or {})
        self._next_user_message_id = str(message_id or "").strip()

    def consume_next_user_message_context(self) -> tuple[dict[str, Any], str]:
        metadata = dict(self._next_user_message_metadata)
        message_id = self._next_user_message_id
        self._next_user_message_metadata = {}
        self._next_user_message_id = ""
        return metadata, message_id

    def set_guidance_port(self, port: Any, *, owner_id: str = "") -> None:
        self._guidance_port = port
        self._guidance_owner_id = str(owner_id or "").strip()

    # 用途：显式开启、关闭或清除外部 Swarm 运行时配置。
    def set_use_swarms(self, use_swarms: Optional[bool]):
        """设置是否使用外部 swarm 运行时"""
        if isinstance(use_swarms, bool):
            self._use_swarms = use_swarms
        else:
            self._use_swarms = None

    # 用途：设置 Swarm 的并行、顺序、投票或重排拓扑。
    def set_swarm_topology(self, topology: Optional[str]):
        """设置 swarm 运行时的拓扑结构
        concurrent, sequential, voting, rearrange"""
        normalized = str(topology or "concurrent").strip().lower()
        if normalized not in {"concurrent", "sequential", "voting", "rearrange"}:
            normalized = "concurrent"
        self._swarm_topology = normalized

    # 用途：设置外部 Swarm 的点对点、广播、交接或混合通信方式。
    def set_external_swarm_communication_mode(self, mode: Optional[str]):
        normalized = str(mode or "").strip().lower()
        if normalized == "all":
            normalized = "hybrid"
        allowed = {"hybrid", "p2p", "broadcast", "handoff"}
        self._external_swarm_communication_mode = normalized if normalized in allowed else None

    # 用途：选择外部 Swarm 使用内存、Redis 或 Kafka 作为通信总线。
    def set_external_swarm_bus(self, bus: Optional[str]):
        """设置外部 swarm 运行时的通信总线"""
        normalized = str(bus or "").strip().lower()
        allowed = {"memory", "redis", "kafka"}
        self._external_swarm_bus = normalized if normalized in allowed else None

    # 用途：设置并限制外部 Swarm 的协作轮数。
    def set_external_swarm_rounds(self, rounds: Optional[int]):
        """设置外部 swarm 运行时的迭代轮数，建议 1-8 轮"""
        if isinstance(rounds, int):
            self._external_swarm_rounds = max(1, min(8, rounds))
        else:
            self._external_swarm_rounds = None

    # 用途：设置并限制外部 Swarm 中单个 Agent 的执行超时时间。
    def set_external_swarm_agent_timeout_seconds(self, seconds: Optional[float]):
        """设置外部 swarm 运行时单个子代理的超时时间，建议 30-120 秒"""
        if isinstance(seconds, (int, float)):
            self._external_swarm_agent_timeout_seconds = max(5.0, min(600.0, float(seconds)))
        else:
            self._external_swarm_agent_timeout_seconds = None

    # 用途：设置并限制外部 Swarm 任务失败后的最大重试次数。
    def set_external_swarm_max_retries(self, retries: Optional[int]):
        """设置外部 swarm 运行时的最大重试次数，建议 0-5 次"""
        if isinstance(retries, int):
            self._external_swarm_max_retries = max(0, min(5, retries))
        else:
            self._external_swarm_max_retries = None

    # 用途：设置外部 Swarm 出错时是否回退到本地执行路径。
    def set_external_swarm_fallback_on_error(self, fallback: Optional[bool]):
        """设置外部 swarm 运行时出错时的回退行为"""
        if isinstance(fallback, bool):
            self._external_swarm_fallback_on_error = fallback
        else:
            self._external_swarm_fallback_on_error = None

    # 用途：控制长耗时 MCP 调用是否转交 Symphony 异步队列。
    def set_route_symphony_on_mcp_tool(self, route: Optional[bool]):
        """设置是否将长任务 MCP 工具路由到 Symphony 队列"""
        if isinstance(route, bool):
            self._route_symphony_on_mcp_tool = route
        else:
            self._route_symphony_on_mcp_tool = None

    # 用途：注入 Agent 事件发送器，将运行状态实时推送给前端或外部系统。
    def set_agent_event_emitter(
        self,
        emitter: Optional[Callable[[dict[str, Any]], Awaitable[None]]],
    ):
        """设置代理事件发射器，用于向外部系统（如前端界面）发送实时事件更新"""
        self._agent_event_emitter = emitter

    # 用途：根据计划/执行模式覆盖指定工具的允许或拒绝状态。
    def _mode_tool_override(self, tool_name: str) -> Optional[str]:
        normalized = str(tool_name or "").strip()
        if not normalized:
            return None

        if self._plan_mode:
            if normalized == "plan_enter":
                return "deny"
            if normalized == "plan_exit":
                return "allow"
            if normalized.startswith("mcp_"):
                return "deny"

            allowed_in_plan = {
                "plan_exit",
                "plan_validate",
                "plan_review",
                "read",
                "grep",
                "glob",
                "webfetch",
                "websearch",
                "question",
                "skill",
                "todo",
                "todo_read",
                "todo_write",
                "task_graph_read",
                "memory_read",
                "team_memory_read",
                "list_workers",
            }
            return "allow" if normalized in allowed_in_plan else "deny"

        if normalized == "plan_exit":
            return "deny"
        if normalized == "todo":
            return "deny"
        return None

    # 用途：切换计划模式并同步会话元数据及任务图状态。
    async def _set_plan_mode(
        self,
        *,
        enabled: bool,
        goal: str = "",
        reason: str = "",
        source_agent: str = "",
    ) -> dict[str, Any]:
        from ..session.manager import SessionManager
        from ..tools.builtin.todowrite import TodoManager

        if enabled and not self._plan_mode:
            candidate = str(source_agent or self.agent_info.name or "").strip()
            if candidate and candidate != "plan":
                self._plan_mode_source = candidate

        self._plan_mode = bool(enabled)

        session = await SessionManager.get(self.session_id)
        active_session = self._existing_session or session
        metadata = dict(getattr(active_session, "metadata", {}) or {})
        metadata["plan_mode"] = self._plan_mode
        metadata["plan_mode_source"] = self._plan_mode_source or "build"
        if goal:
            metadata["task_goal"] = str(goal).strip()
        if reason:
            metadata["plan_mode_last_reason"] = str(reason).strip()

        if active_session:
            active_session.metadata = metadata
            active_session.last_active_at = time.time()
        try:
            if session or active_session:
                await SessionManager.update(
                    self.session_id,
                    metadata=metadata,
                    last_active_at=time.time(),
                )
        except Exception:
            pass

        await TodoManager.ensure_graph(
            self.session_id,
            goal=str(goal or metadata.get("task_goal", "")).strip(),
            owner=self.agent_info.name,
            workdir=self.workdir,
        )

        return {
            "plan_mode": self._plan_mode,
            "mode": "plan" if self._plan_mode else "execute",
            "source_agent": self._plan_mode_source or "build",
        }

    # 用途：判断 Agent 当前是否尚未收到中止信号。
    @property
    def is_running(self) -> bool:
        """Check if the agent is currently running"""
        return not self._abort_event.is_set()

    # 用途：以异步且不阻塞主流程的方式触发插件生命周期事件。
    def _emit_agent_hook(self, event: str, data: dict[str, Any]) -> None:
        """把 Agent 生命周期事件交给唯一 HookRegistry 管线异步派发。

        参数:
            event: 已声明或自定义的事件名称。
            data: 只在当前派发任务中使用的事件输入。
        """

        try:
            import asyncio as _asyncio
            loop = _asyncio.get_running_loop()

            # 用途：获取插件管理器并实际派发当前 Agent 钩子事件。
            async def _emit():
                """在受 Agent 所有权约束的任务中执行一次事件派发。"""

                try:
                    from ..plugins.manager import PluginManager
                    manager = await PluginManager.get_global(self.workdir or os.getcwd())
                    await manager.trigger(event, data, {})
                except _asyncio.CancelledError:
                    raise
                except Exception:
                    return

            task = loop.create_task(_emit())
            self._plugin_hook_tasks.add(task)
            task.add_done_callback(self._plugin_hook_tasks.discard)
        except Exception:
            pass

    async def _cancel_plugin_hook_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._plugin_hook_tasks
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._plugin_hook_tasks.difference_update(tasks)

    # 用途：设置中止标志并广播 Agent 停止事件。
    def stop(self):
        """Stop"""
        self._abort_event.set()
        self._emit_agent_hook("agent.stop", {
            "agent": self.agent_info.name,
            "session_id": self.session_id,
        })

    # 用途：关闭 MCP 与模型提供方连接，释放 Agent 占用的运行资源。
    async def close(self):
        """关闭代理，清理资源"""
        # Always attempt both cleanup steps
        mcp_error = None
        await self._cancel_plugin_hook_tasks()
        try:
            await MCPManager.close()
        except Exception as e:
            mcp_error = e

        close_method = getattr(self.provider, "close", None)
        if close_method:
            maybe_coro = close_method()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro

        if mcp_error:
            raise mcp_error


    ##### 工具方法
    # 用途：按照字符数粗略估算文本占用的 Token 数量。
    def _estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数量，粗略估计每 4 个字符约等于 1 个 token"""
        # Average ~4 characters per token
        return len(text) // 4

    # 用途：为权限检查生成工具名及其兼容别名候选。
    @staticmethod
    def _permission_alias_candidates(tool_name: str) -> list[str]:
        normalized = str(tool_name or "").strip()
        if not normalized:
            return []

        alias_map = {
            "write": "edit",
            "todo_write": "todowrite",
            "todo_read": "todoread",
            "todowrite": "todo_write",
            "todoread": "todo_read",
        }
        candidates = [normalized]
        alias = alias_map.get(normalized)
        if alias and alias not in candidates:
            candidates.append(alias)
        return candidates

    # 用途：判断工具名是否符合通配符权限规则。
    @staticmethod
    def _matches_permission_pattern(pattern: str, candidate: str) -> bool:
        if pattern == "*":
            return True
        return fnmatch.fnmatch(candidate, pattern)

    # 用途：解析带参数子规则的权限配置并合并为 allow、deny 或 ask。
    @classmethod
    def _resolve_nested_permission_action(
        cls,
        nested_rules: Any,
        patterns: list[str] | None,
    ) -> str:
        if not isinstance(nested_rules, dict):
            return "ask"

        normalized_patterns = [
            str(item or "").strip()
            for item in (patterns or [])
            if str(item or "").strip()
        ]
        if not normalized_patterns:
            normalized_patterns = ["*"]

        saw_ask = False
        for candidate in normalized_patterns:
            matched_action: Optional[str] = None
            for pattern, action in nested_rules.items():
                if not isinstance(action, str):
                    continue
                if cls._matches_permission_pattern(str(pattern), candidate):
                    matched_action = action

            normalized_action = str(matched_action or "ask").strip().lower()
            if normalized_action == "deny":
                return "deny"
            if normalized_action == "ask":
                saw_ask = True

        return "ask" if saw_ask else "allow"

    # 用途：清洗权限参数列表，并在空输入时回退为全匹配规则。
    @staticmethod
    def _normalize_permission_patterns(patterns: list[str] | None) -> list[str]:
        normalized = [
            str(item or "").strip()
            for item in (patterns or [])
            if str(item or "").strip()
        ]
        return normalized or ["*"]

    # 用途：检查一组权限参数是否已被用户设置为始终允许。
    def _permission_patterns_always_allowed(
        self,
        permission: str,
        patterns: list[str] | None,
    ) -> bool:
        cached = self._always_allowed_permission_patterns.get(
            str(permission or "").strip(),
            set(),
        )
        if not cached:
            return False
        if "*" in cached:
            return True

        requested = self._normalize_permission_patterns(patterns)
        return all(item in cached for item in requested)

    # 用途：缓存用户选择“始终允许”的具体权限参数。
    def _remember_permission_patterns(
        self,
        permission: str,
        patterns: list[str] | None,
    ) -> None:
        normalized_permission = str(permission or "").strip()
        if not normalized_permission:
            return
        cached = self._always_allowed_permission_patterns.setdefault(
            normalized_permission,
            set(),
        )
        cached.update(self._normalize_permission_patterns(patterns))

    # 用途：综合模式覆盖和 Agent 权限配置判断工具是否可见可用。
    def _is_tool_enabled_for_agent(self, tool_name: str) -> bool:
        mode_override = self._mode_tool_override(tool_name)
        if mode_override == "deny":
            return False
        if mode_override == "allow":
            return True

        permissions = dict(self.agent_info.permission or {})
        action: Optional[str] = None
        for pattern, value in permissions.items():
            if isinstance(value, dict):
                continue
            if pattern == "*":
                action = str(value)
                continue
            if "*" in pattern or "?" in pattern:
                if fnmatch.fnmatch(tool_name, pattern):
                    action = str(value)
                continue
            if pattern == tool_name:
                action = str(value)
        return action != "deny"

    # 用途：判断当前 Agent 是否通过权限配置明确禁用了全部工具。
    def _agent_declares_no_tools(self) -> bool:
        permissions = dict(self.agent_info.permission or {})
        if str(permissions.get("*", "") or "").strip().lower() != "deny":
            return False
        for pattern, value in permissions.items():
            if pattern == "*":
                continue
            if isinstance(value, dict):
                return False
            if str(value or "").strip().lower() != "deny":
                return False
        return True

    # 用途：判断当前执行配置是否允许进行 Skill 推荐。
    def _skill_recommendation_enabled(self) -> bool:
        return self._execution_profile not in {"", None, "fast"}

    # 用途：把字符串或列表统一清洗成非空字符串列表。
    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
        if not isinstance(value, list):
            return []
        return [str(item or "").strip() for item in value if str(item or "").strip()]

    # 用途：安全判断一个路径是否位于指定根目录之内。
    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except Exception:
            return False

    # 用途：根据 Agent 权限判断是否启用默认 Skill 自动加载能力。
    def _default_skill_enabled(self) -> bool:
        permission = dict(self.agent_info.permission or {})
        return str(permission.get("default_skill", "") or "").strip().lower() == "allow"

    # 用途：解析当前 Agent 定义文件所在目录，供相对配置定位使用。
    def _definition_dir(self) -> Optional[Path]:
        options = dict(self.agent_info.options or {})
        raw = str(
            options.get("__definition_dir__")
            or options.get("definition_dir")
            or ""
        ).strip()
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve()
        except Exception:
            return None

    # 用途：汇总默认 Skill 的名称与通配符匹配规则。
    def _default_skill_patterns(self) -> list[str]:
        options = dict(self.agent_info.options or {})
        patterns = self._normalize_string_list(options.get("default_skill_patterns"))
        for name in self._normalize_string_list(options.get("default_skill_names")):
            if name and name not in patterns:
                patterns.append(name)
        return patterns

    # 用途：获取当前工作目录及其全部父目录，避免重复扫描同一目录链。
    def _current_workdir_lineage(self) -> set[Path]:
        try:
            base = Path(self.workdir or os.getcwd()).expanduser().resolve()
        except Exception:
            return set()
        if base.is_file():
            base = base.parent
        return {base, *base.parents}

    # 用途：从配置根或 Agent 定义位置自动推断默认 Skill 根目录。
    def _auto_default_skill_root(self) -> Optional[Path]:
        from ..skill.roots import default_skill_roots
        roots = default_skill_roots(self.workdir, {'__definition_dir__': str(self._definition_dir() or '')})
        return roots[0] if roots else None

    # 用途：解析、去重并返回 Agent 配置声明的默认 Skill 根目录。
    def _configured_default_skill_roots(self) -> list[Path]:
        from ..skill.roots import default_skill_roots
        return default_skill_roots(self.workdir, self.agent_info.options)

    # 用途：筛出不在当前工作目录继承链中的额外 Skill 根目录。
    def _candidate_default_skill_roots(self) -> list[Path]:
        lineage = self._current_workdir_lineage()
        return [
            root
            for root in self._configured_default_skill_roots()
            if root not in lineage
        ]

    # 用途：将额外 Skill 根目录展开成所有受支持的 Skill 子目录。
    def _candidate_default_skill_dirs(self) -> list[Path]:
        from ..skill import SkillManager

        directories: list[Path] = []
        for root in self._candidate_default_skill_roots():
            for rel in SkillManager.DEFAULT_SKILL_DIRS:
                directories.append((root / rel).resolve())
        return directories

    # 用途：将全部配置根目录展开成实际需要扫描的 Skill 子目录。
    def _configured_default_skill_dirs(self) -> list[Path]:
        from ..skill import SkillManager

        directories: list[Path] = []
        for root in self._configured_default_skill_roots():
            for rel in SkillManager.DEFAULT_SKILL_DIRS:
                directories.append((root / rel).resolve())
        return directories

    # 用途：判断给定 Skill 是否来自额外配置的默认 Skill 根目录。
    def _is_default_root_skill(self, skill: Any) -> bool:
        if getattr(skill, 'source_kind', '') == 'project':
            return False
        directories = self._candidate_default_skill_dirs()
        if not directories:
            return False
        try:
            skill_path = Path(str(getattr(skill, "location", "") or "")).expanduser().resolve()
        except Exception:
            return False
        return any(self._path_is_within(skill_path, directory) for directory in directories)

    # 用途：综合专属绑定、动态启停、默认规则和权限判断 Skill 对当前 Agent 是否可见。
    def _is_skill_visible_to_agent(self, skill: Any) -> bool:
        skill_name = str(getattr(skill, "name", "") or "").strip()
        if not skill_name:
            return False
        from ..skill.runtime_binding import skill_allowed_for_agent

        if not skill_allowed_for_agent(
            skill_name,
            self.agent_info.name,
            self.workdir,
        ):
            return False
        if not self._is_default_root_skill(skill):
            return self._resolve_permission_action("skill", [skill_name]) != "deny"
        if not self._default_skill_enabled():
            return False
        patterns = self._default_skill_patterns()
        if not patterns:
            return self._resolve_permission_action("skill", [skill_name]) != "deny"
        if not any(fnmatch.fnmatch(skill_name, pattern) for pattern in patterns):
            return False
        return self._resolve_permission_action("skill", [skill_name]) != "deny"

    # 用途：注册插件 Skill，并确保工作区及额外目录的 Skill 目录已加载。
    async def _ensure_skill_catalog_loaded(self) -> None:
        from ..skill import SkillManager

        await self._ensure_plugin_skills_registered()
        if not SkillManager._loaded:
            await SkillManager.load(workdir=self.workdir)
        else:
            await SkillManager.load(workdir=self.workdir)

        explicit_roots = [str(root) for root in self._candidate_default_skill_roots()]
        if explicit_roots:
            await SkillManager.load_explicit_roots(explicit_roots)

    # 用途：查找最近一条用户消息的 ID，作为推荐和追踪关联键。
    def _latest_user_message_id(self) -> str:
        for msg in reversed(self.messages):
            if getattr(msg, "role", "") == "user" and str(getattr(msg, "id", "") or "").strip():
                return str(getattr(msg, "id", "") or "").strip()
        return self.messages[-1].id if self.messages else self._generate_id()

    # 用途：把当前 Skill 路由结果整理成注入系统提示词的推荐说明。
    def _build_skill_recommendation_prompt(self) -> str:
        composite_plan = (
            self._skill_composite_plan
            if isinstance(self._skill_composite_plan, dict)
            else {}
        )
        composite_steps = (
            composite_plan.get("steps")
            if composite_plan.get("decision") == "composite"
            and bool(composite_plan.get("expose_to_agent", False))
            and isinstance(composite_plan.get("steps"), list)
            else []
        )
        if not self._recommended_skills and not composite_steps:
            return ""
        lines = ["# Recommended Skills"]
        if self._recommended_skills:
            lines.append(
                "The following skills look relevant for this request. Use the `skill` tool to load the full skill when a skill's `when_to_use` matches the task."
            )
            route_decision = str(self._recommended_skills[0].get("route_decision", "") or "")
            if route_decision == "clarify":
                lines.append(
                    "The top candidates are close. Do not auto-load one by guess; compare their boundaries and clarify only when the task cannot proceed safely."
                )
            for item in self._recommended_skills[:3]:
                marker = " [primary]" if bool(item.get("selected")) else ""
                summary = f"- {item.get('name', 'unknown')}{marker}: {item.get('reason', item.get('description', ''))}"
                when_to_use = str(item.get("when_to_use", "") or "").strip()
                if when_to_use:
                    summary += f" | when_to_use: {when_to_use}"
                lines.append(summary)
        if composite_steps:
            lines.extend(
                [
                    "",
                    "# Composite Skill Plan",
                    "This request appears to contain multiple routable sub-tasks. Treat this as a planning hint, not as permission to execute high-cost jobs automatically. Load only the skill needed for the current step, and use workflow/human-review/async-job controls when a step requires them.",
                ]
            )
            if bool(composite_plan.get("level3_candidate", False)):
                lines.append(
                    "This plan looks like a candidate for a durable workflow because it contains async domain-compute steps. Prefer `workflow_proposal` first; do not call `workflow_run` until the user confirms a proposal or explicitly selects a workflow template."
                )
            for raw_step in composite_steps[:6]:
                if not isinstance(raw_step, dict):
                    continue
                step_id = str(raw_step.get("step_id") or "?")
                subtask = str(raw_step.get("subtask") or "").strip()
                selected = str(raw_step.get("selected_skill") or "").strip()
                confidence = raw_step.get("confidence")
                execution_hint = str(raw_step.get("execution_hint") or "instructional")
                depends_on = raw_step.get("depends_on") if isinstance(raw_step.get("depends_on"), list) else []
                suffix = f" -> `{selected}`" if selected else " -> no routed skill"
                if isinstance(confidence, (int, float)) and selected:
                    suffix += f" ({confidence:.2f})"
                if depends_on:
                    suffix += f"; depends_on={','.join(str(item) for item in depends_on)}"
                suffix += f"; execution={execution_hint}"
                lines.append(f"- {step_id}: {subtask}{suffix}")
        return "\n".join(lines)

    # 用途：识别问候、确认等低信息量输入，避免无意义的 Skill 路由。
    def _is_low_signal_query(self, query: str) -> bool:
        text = str(query or "").strip().lower()
        if not text:
            return True
        # 短问候语不触发重型检索，避免流式首包被慢路径阻塞。
        if len(text) <= 4:
            return True
        low_signal_set = {
            "hi",
            "hello",
            "hey",
            "你好",
            "在吗",
            "嗨",
            "test",
            "测试",
            "1",
            "?",
            "？",
        }
        return text in low_signal_set

    # 用途：结合用户输入、记忆召回和可见目录选择推荐 Skill。
    def _select_recommended_skills(self, *, use_vector: bool = True) -> list[dict[str, Any]]:
        if not self._skill_recommendation_enabled():
            self._recommended_skills = []
            return []
        latest_user_id = ""
        try:
            from ..skill import SkillManager
            from ..context.router import route_context
            from ..skill.routing_config import load_skill_routing_config

            last_user = ""
            for msg in reversed(self.messages):
                if getattr(msg, "role", "") == "user":
                    last_user = str(getattr(msg, "content", "") or "").strip()
                    if last_user:
                        break

            latest_user_id = self._latest_user_message_id()
            if self._skill_routing_message_id == latest_user_id and self._skill_routing_trace:
                return self._recommended_skills

            recall_texts: list[str] = []
            include_recall = bool(load_skill_routing_config(self.workdir).get('routing', {}).get('include_recall_context', False))
            if use_vector and include_recall and last_user and not self._is_low_signal_query(last_user):
                routed = route_context(
                    session_id=self.session_id,
                    workdir=self.workdir or os.getcwd(),
                    query=last_user,
                    team_mode=self.agent_info.name in {"manager", "protein_orchestrator", "paper_auto_coordinator"},
                    limit=4,
                )
                for item in routed.get("memory_recall", [])[:2]:
                    recall_texts.append(
                        str(getattr(item, "l0_summary", "") or getattr(item, "content", ""))
                    )
                for item in routed.get("team_memory_recall", [])[:2]:
                    recall_texts.append(
                        str(getattr(item, "l0_summary", "") or getattr(item, "content", ""))
                    )

            visible_skills = [
                skill
                for skill in SkillManager.all()
                if self._is_skill_visible_to_agent(skill)
            ]
            route_result = SkillManager.route(
                query=last_user,
                agent_name=self.agent_info.name,
                recall_texts=recall_texts,
                skills=visible_skills,
                limit=3,
                enabled=True,
                workdir=self.workdir,
                use_vector=use_vector,
            )
            composite_plan = SkillManager.composite_route(
                query=last_user,
                agent_name=self.agent_info.name,
                recall_texts=recall_texts,
                skills=visible_skills,
                enabled=True,
                workdir=self.workdir,
                use_vector=use_vector,
            )
            if latest_user_id != self._latest_user_message_id():
                return []
            self._skill_routing_trace = dict(route_result.trace or {})
            self._skill_routing_message_id = latest_user_id
            self._recommended_skills = list(route_result.recommendations)[:3]
            self._skill_composite_plan = composite_plan.to_dict()
            return self._recommended_skills
        except Exception:
            if latest_user_id and latest_user_id != self._latest_user_message_id():
                return []
            self._recommended_skills = []
            self._skill_routing_trace = {}
            self._skill_composite_plan = {}
            self._skill_routing_message_id = ""
            return []

    async def _recommend_skills_without_blocking(self) -> None:
        """Optional recommendations share one job per question and a short wait budget."""
        question_id = self._latest_user_message_id()
        if getattr(self, '_skill_routing_message_id', '') == question_id and getattr(self, '_skill_routing_trace', {}):
            return
        task = getattr(self, '_skill_recommendation_task', None)
        if getattr(self, '_skill_recommendation_task_question', '') != question_id or task is None:
            if task is not None and not task.done():
                task.cancel()
            task = asyncio.create_task(asyncio.to_thread(self._select_recommended_skills))
            self._skill_recommendation_task = task
            self._skill_recommendation_task_question = question_id
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except asyncio.TimeoutError:
            await asyncio.to_thread(self._select_recommended_skills, use_vector=False)

    # 用途：判断环境与执行配置是否共同允许自动预加载推荐 Skill。
    def _skill_auto_preload_enabled(self) -> bool:
        if not self._env_skill_auto_load_enabled():
            return False
        raw = str(os.environ.get("TCODE_AUTO_PRELOAD_RECOMMENDED_SKILLS", "0") or "0").strip().lower()
        return self._skill_recommendation_enabled() and raw not in {"0", "false", "no", "off"}

    # 用途：初始化插件管理器，使插件贡献的 Skill 被注册到目录。
    async def _ensure_plugin_skills_registered(self):
        try:
            await PluginManager.get_global(self.workdir or os.getcwd())
        except Exception:
            return

    # 用途：按 Agent 默认规则预加载尚未加载且允许使用的 Skill。
    async def _maybe_preload_default_skills(
        self,
        *,
        parent_msg_id: str,
    ) -> list[Message]:
        if not self._default_skill_enabled():
            return []

        patterns = self._default_skill_patterns()
        if not patterns:
            return []

        from ..skill import SkillManager
        from ..skill.runtime_binding import skill_allowed_for_agent
        from ..skill.tool import SkillTool

        await self._ensure_skill_catalog_loaded()

        loaded_names = self._recently_loaded_skill_names()
        options = dict(self.agent_info.options or {})
        try:
            limit = int(options.get("default_skill_limit", 8) or 8)
        except Exception:
            limit = 8
        limit = max(1, min(limit, 20))

        matched = []
        for skill in SkillManager.all():
            if not skill_allowed_for_agent(
                skill.name,
                self.agent_info.name,
                self.workdir,
            ):
                continue
            try:
                skill_path = Path(str(getattr(skill, "location", "") or "")).expanduser().resolve()
            except Exception:
                continue
            if not any(
                self._path_is_within(skill_path, directory)
                for directory in self._configured_default_skill_dirs()
            ):
                continue
            if skill.name in loaded_names:
                continue
            if not self._default_skill_enabled():
                continue
            if not any(fnmatch.fnmatch(skill.name, pattern) for pattern in patterns):
                continue
            matched.append(skill)

        matched.sort(key=lambda item: item.name)
        created: list[Message] = []
        for skill in matched[:limit]:
            content, _ = SkillTool.render_skill_content(skill)
            msg = Message(
                role="tool",
                content=content,
                id=self._generate_id(),
                parent_id=parent_msg_id,
            )
            self.messages.append(msg)
            created.append(msg)
        return created

    # 用途：从近期工具消息中提取已加载的 Skill 名称，防止重复注入。
    def _recently_loaded_skill_names(self) -> set[str]:
        loaded: set[str] = set()
        for msg in self.messages[-20:]:
            for tool_call in getattr(msg, "tool_calls", []) or []:
                if str(getattr(tool_call, "tool_name", "") or "") != "skill":
                    continue
                arguments = getattr(tool_call, "arguments", {}) or {}
                name = str(arguments.get("name", "") or "").strip()
                if name:
                    loaded.add(name)
            content = str(getattr(msg, "content", "") or "")
            marker = '<skill_content name="'
            idx = content.find(marker)
            if idx >= 0:
                start = idx + len(marker)
                end = content.find('"', start)
                if end > start:
                    loaded.add(content[start:end])
        return loaded

    
    # 用途：读取自动加载 Skill 所需的最低路由分数。
    def _env_skill_min_score(self) -> float:
        try:
            return float(os.environ.get("TCODE_SKILL_AUTO_LOAD_MIN_SCORE", "5.0") or "5.0")
        except Exception:
            return 5.0

    # 用途：从环境变量读取是否允许自动加载推荐 Skill。
    def _env_skill_auto_load_enabled(self) -> bool:
        raw = str(os.environ.get("TCODE_SKILL_AUTO_LOAD_ENABLED", "1") or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    # 用途：从推荐结果中选出满足阈值且尚未加载的自动预加载候选。
    def _select_auto_preload_skill(self) -> Optional[dict[str, Any]]:
        if not self._skill_auto_preload_enabled() or not self._recommended_skills:
            return None
        if not self._is_tool_enabled_for_agent("skill"):
            return None

        loaded_names = self._recently_loaded_skill_names()

        # P1 路由结果只允许自动加载唯一主 Skill；clarify/no-skill 均不预加载。
        routed_candidates = [
            item
            for item in self._recommended_skills
            if "route_decision" in item
        ]
        if routed_candidates:
            for item in routed_candidates:
                name = str(item.get("name", "") or "").strip()
                if not name or name in loaded_names:
                    continue
                if bool(item.get("selected")) and bool(item.get("auto_load_eligible")):
                    return item
            return None

        # 兼容关闭新路由器时的旧推荐结果。
        last_user = ""
        for msg in reversed(self.messages):
            if getattr(msg, "role", "") == "user":
                last_user = str(getattr(msg, "content", "") or "").strip().lower()
                if last_user:
                    break

        if last_user:
            for item in self._recommended_skills:
                name = str(item.get("name", "") or "").strip()
                if not name or name in loaded_names:
                    continue
                trigger_keywords = item.get("trigger_keywords", []) or []
                if not isinstance(trigger_keywords, list):
                    trigger_keywords = []
                if trigger_keywords:
                    if any(kw.lower() in last_user for kw in trigger_keywords):
                        return item

        auto_candidates: list[dict[str, Any]] = []
        for item in self._recommended_skills:
            name = str(item.get("name", "") or "").strip()
            if not name or name in loaded_names:
                continue
            when_to_use = str(item.get("when_to_use", "") or "").strip()
            explicit_auto_load = bool(item.get("auto_load", False))
            if not explicit_auto_load and not when_to_use:
                continue
            auto_candidates.append(item)

        if not auto_candidates:
            return None

        # 用途：安全读取推荐项分数，无法解析时返回零分。
        def _score(item: dict[str, Any]) -> float:
            try:
                return float(item.get("score", 0) or 0)
            except Exception:
                return 0.0

        runners = [
            item
            for item in auto_candidates
            if str(item.get("skill_role", "") or "").strip().lower() == "runner"
        ]
        pool = runners or auto_candidates
        pool.sort(key=_score, reverse=True)

        best = pool[0]
        explicit_auto_load = bool(best.get("auto_load", False))
        overlap_tokens = best.get("overlap_tokens") if isinstance(best.get("overlap_tokens"), list) else []
        when_to_use = str(best.get("when_to_use", "") or "").strip()
        if explicit_auto_load:
            return best
        if not when_to_use:
            return None
        if not overlap_tokens and _score(best) < 6.0:
            return None
        min_score = self._env_skill_min_score()
        if _score(best) < min_score:
            return None
        return best

    async def _preload_selected_skills(self, *, parent_msg_id: str) -> list[Message]:
        """Load explicitly selected skills through the same tool permission boundary."""
        names = list(getattr(self, '_next_selected_skills', []) or [])
        self._next_selected_skills = []
        if not names:
            return []
        await self._ensure_skill_catalog_loaded()
        loaded: list[Message] = []
        for name in names:
            call = ToolCall(tool_name='skill', call_id=self._generate_id(), arguments={'name': name})
            result = await self._execute_tool(call)
            if result.error:
                raise ValueError(f'无法加载所选技能 {name}: {result.error}')
            message = Message(role='tool', content=result.output or '', id=self._generate_id(), parent_id=parent_msg_id,
                metadata={'explicit_skill': name})
            message.tool_calls = [call]
            self.messages.append(message)
            loaded.append(message)
        return loaded

    # 用途：在执行前自动加载最佳推荐 Skill，并把结果写入会话消息。
    async def _maybe_preload_recommended_skill(
        self,
        *,
        parent_msg_id: str,
    ) -> Optional[Message]:
        from ..skill import SkillManager

        if not self._skill_auto_preload_enabled():
            return None
        await self._ensure_skill_catalog_loaded()
        await self._recommend_skills_without_blocking()

        target = self._select_auto_preload_skill()
        if not target:
            return None

        skill_name = str(target.get("name", "") or "").strip()
        if not skill_name:
            return None

        preload_call = ToolCall(
            tool_name="skill",
            call_id=self._generate_id(),
            arguments={"name": skill_name},
        )
        result = await self._execute_tool(preload_call)
        if result.error:
            return None

        preload_msg = Message(
            role="tool",
            content=result.output or "",
            id=self._generate_id(),
            parent_id=parent_msg_id,
        )
        preload_msg.tool_calls = [preload_call]
        self.messages.append(preload_msg)
        return preload_msg

    # 用途：估算当前消息上下文的 Token 使用量和占用比例。
    def _get_context_usage(self) -> dict:
        """用途：计算当前上下文使用情况
        返回：包含 messages 数量、chars 字符数、estimated_tokens 估算 token 数"""
        total_chars = 0
        for msg in self.messages:
            total_chars += len(msg.content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.arguments:
                        total_chars += len(json.dumps(tc.arguments))

        # Estimate tokens (rough: 4 chars per token)
        estimated_tokens = total_chars // 4

        return {
            "messages": len(self.messages),
            "chars": total_chars,
            "estimated_tokens": estimated_tokens,
        }

    # 用途：在上下文接近模型限制时触发会话自动压缩。
    async def _check_auto_compact(self):
        """检查是否需要自动压缩会话上下文以适应模型限制"""
        if not self._existing_session:
            return

        if hasattr(self._existing_session, "messages"):
            self._existing_session.messages = list(self.messages)

        compaction_started = False

        async def emit_compaction_event(status: str, **details: Any) -> None:
            emitter = getattr(self, "_agent_event_emitter", None)
            if not callable(emitter):
                return
            payload = {
                "kind": "context_compaction",
                "status": str(status or "").strip().lower(),
                "session_id": str(getattr(self, "session_id", "") or ""),
                "agent_name": str(getattr(self.agent_info, "name", "") or ""),
                **details,
            }
            try:
                emitted = emitter(payload)
                if hasattr(emitted, "__await__"):
                    await emitted
            except Exception:
                pass

        async def on_compaction_start(details: dict[str, Any]) -> None:
            nonlocal compaction_started
            compaction_started = True
            await emit_compaction_event("running", **details)

        try:
            result = await self._context_engine.maybe_compact(
                session=self._existing_session,
                provider=self.provider,
                on_start=on_compaction_start,
            )
        except Exception as exc:
            if compaction_started:
                await emit_compaction_event("error", error=str(exc)[:1000])
            raise

        if result:
            compacted_messages = list(
                getattr(self._existing_session, "messages", []) or []
            )
            if compacted_messages and (
                result.status == "success"
                or getattr(result, "pruned_tool_outputs", 0) > 0
            ):
                self.messages = compacted_messages
            if result.status == "success":
                try:
                    compaction_count = int(
                        getattr(self._existing_session, "compaction_count", 0) or 0
                    ) + 1
                    compacted_at = time.time()
                    total_tokens_saved = int(
                        getattr(self._existing_session, "total_tokens_saved", 0) or 0
                    ) + max(0, int(result.tokens_saved or 0))
                    self._existing_session.compaction_count = compaction_count
                    self._existing_session.last_compacted_at = compacted_at
                    self._existing_session.total_tokens_saved = total_tokens_saved
                    from ..session.manager import SessionManager

                    owner = {
                        "tenant_id": str(
                            getattr(self._existing_session, "tenant_id", "") or ""
                        ),
                        "user_id": str(
                            getattr(self._existing_session, "user_id", "") or ""
                        ),
                        "workspace_id": str(
                            getattr(self._existing_session, "workspace_id", "") or ""
                        ),
                    }
                    await SessionManager.update(
                        self.session_id,
                        **owner,
                        compaction_count=compaction_count,
                        last_compacted_at=compacted_at,
                        total_tokens_saved=total_tokens_saved,
                    )
                    from ..research import ResearchEventInput, ResearchEventSink

                    ResearchEventSink.append(
                        ResearchEventInput(
                            event_type="message.compacted",
                            **owner,
                            session_id=self.session_id,
                            turn_id=str(self._active_research_turn_id or ""),
                            trace_id=str(self._active_research_trace_id or ""),
                            actor_type="system",
                            actor_id="session-compaction",
                            source_kind="session_compaction",
                            source_id=f"{self.session_id}:{compaction_count}",
                            source_ref=f"session:{self.session_id}/compaction:{compaction_count}",
                            payload={
                                "compaction_count": compaction_count,
                                "original_tokens": int(result.original_tokens or 0),
                                "compressed_tokens": int(result.compressed_tokens or 0),
                                "tokens_saved": int(result.tokens_saved or 0),
                                "pruned_tool_outputs": int(
                                    result.pruned_tool_outputs or 0
                                ),
                                "summary_message_id": str(
                                    getattr(
                                        (self.messages or [None])[0],
                                        "id",
                                        "",
                                    )
                                    or ""
                                ),
                            },
                            privacy_state="private",
                            stable_state_version=str(compaction_count),
                        ),
                        workdir=self.workdir,
                    )
                except Exception:
                    pass
                print(
                    f"\n📦 Session compacted: {result.original_tokens} → {result.compressed_tokens} tokens (saved {result.tokens_saved})"
                )
                if result.pruned_tool_outputs > 0:
                    print(f"   Pruned {result.pruned_tool_outputs} old tool outputs")
            elif result.status == "failed":
                print(f"\n⚠️  Compaction failed: {result.summary}")
            if compaction_started:
                await emit_compaction_event(
                    "completed" if result.status == "success" else result.status,
                    original_tokens=result.original_tokens,
                    compressed_tokens=result.compressed_tokens,
                    tokens_saved=result.tokens_saved,
                    pruned_tool_outputs=result.pruned_tool_outputs,
                    summary=result.summary[:1000] if result.status == "failed" else "",
                )
        return result

    # 用途：识别已受理的 Symphony 异步任务，并尽力执行后续结果收集。
    async def _try_async_followup_collect(
        self,
        result: ToolResult,
        parent_msg_id: str,
    ) -> tuple[bool, list[Message]]:
        """Optional inline collection for an explicitly orchestrated Workflow.

        Returns (collected, generated_tool_messages).
        - collected=True: collect succeeded in current turn, caller should continue next LLM round.
        - collected=False: keep accepted-async early return behavior.
        """
        metadata = result.metadata or {}
        if not bool(metadata.get("accepted_async")):
            return False, []

        # Ordinary chat releases its foreground turn after acceptance. The
        # watcher owns collection and the explicit continuation card, even if
        # a legacy deployment enabled inline polling globally.
        if not dict(getattr(self, "_active_tool_trace_context", {}) or {}).get("workflow_run_id"):
            return False, []

        enable_followup = str(
            os.environ.get("TCODE_ASYNC_POSTPROCESS_ON_ACCEPT", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not enable_followup:
            return False, []

        identifier = str(
            metadata.get("identifier")
            or metadata.get("symphony_identifier")
            or ""
        ).strip()
        if not identifier:
            return False, []

        try:
            timeout_seconds = float(
                os.environ.get("TCODE_ASYNC_POSTPROCESS_TIMEOUT_SECONDS", "12")
            )
        except Exception:
            timeout_seconds = 12.0
        if timeout_seconds <= 0:
            return False, []
        timeout_seconds = min(timeout_seconds, 60.0)
        try:
            poll_interval = float(
                os.environ.get("TCODE_ASYNC_POSTPROCESS_POLL_INTERVAL_SECONDS", "1")
            )
        except Exception:
            poll_interval = 1.0
        poll_interval = max(0.2, min(poll_interval, 100.0))
        deadline = time.time() + timeout_seconds

        created_msgs: list[Message] = []
        terminal_states = {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "terminated",
            "timed_out",
            "error",
        }

        while time.time() < deadline:
            status_call = ToolCall(
                tool_name="mcp_job-protocol-mcp_get_job_status",
                call_id=self._generate_id(),
                arguments={"input": {"job_id": identifier}},
            )
            status_result = await self._execute_tool(status_call)

            status_msg = Message(
                role="tool",
                content=status_result.output or status_result.error or "",
                id=self._generate_id(),
                parent_id=parent_msg_id,
            )
            status_msg.tool_calls = [status_call]
            self.messages.append(status_msg)
            created_msgs.append(status_msg)

            status = ""
            try:
                parsed = json.loads(status_result.output or "{}")
                status = str(parsed.get("status", "")).strip().lower()
            except Exception:
                status = ""

            if status in terminal_states:
                if status == "completed":
                    collect_call = ToolCall(
                        tool_name="mcp_job-protocol-mcp_collect_job_result",
                        call_id=self._generate_id(),
                        arguments={"input": {"job_id": identifier}},
                    )
                    collect_result = await self._execute_tool(collect_call)
                    collect_msg = Message(
                        role="tool",
                        content=collect_result.output or collect_result.error or "",
                        id=self._generate_id(),
                        parent_id=parent_msg_id,
                    )
                    collect_msg.tool_calls = [collect_call]
                    self.messages.append(collect_msg)
                    created_msgs.append(collect_msg)
                    return True, created_msgs
                return False, created_msgs

            await asyncio.sleep(poll_interval)

        return False, created_msgs


    ####。核心运算方法
    # 用途：以非流式方式处理一条用户消息并返回最终响应。
    async def run(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> Message:
        """Run the agent with a user message (non-streaming)"""
        snapshot = self._config_snapshot or TaskConfigSnapshotStore.for_task(
            self.session_id,
            self.workdir or os.getcwd(),
        )
        self._config_snapshot = snapshot
        with ConfigManager.bind_snapshot(
            snapshot.snapshot_id,
            snapshot.runtime_config,
            workdir=snapshot.workdir,
        ):
            self._emit_agent_hook("agent.create.complete", {
                "agent": self.agent_info.name,
                "session_id": self.session_id,
                "config_snapshot_id": snapshot.snapshot_id,
            })
            return await self._engine.run(
                user_message,
                output_format,
                outcome_contract,
            )

    # 用途：以异步迭代器方式运行 Agent，持续产出流式消息事件。
    async def run_stream(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[Message]:
        """Run the agent with streaming responses"""
        snapshot = self._config_snapshot or TaskConfigSnapshotStore.for_task(
            self.session_id,
            self.workdir or os.getcwd(),
        )
        self._config_snapshot = snapshot
        stream = self._engine.run_stream(
            user_message,
            output_format,
            outcome_contract,
        )
        while True:
            # The websocket consumes each __anext__ in a fresh asyncio Task.
            # Bind per step so the ContextVar is never carried across a yield or
            # reset from a different Task.
            with ConfigManager.bind_snapshot(
                snapshot.snapshot_id,
                snapshot.runtime_config,
                workdir=snapshot.workdir,
            ):
                try:
                    msg = await stream.__anext__()
                except StopAsyncIteration:
                    return
            yield msg

    ### 工具相关方法
    # 用途：让插件在工具暴露给模型前修改其名称、描述和参数结构。
    async def _apply_plugin_tool_definition_hook(
        self, definition: ToolDefinition
    ) -> ToolDefinition:
        from ..plugins.hooks import HookRegistry

        if not HookRegistry._hooks.get("tool.definition"):
            return definition
        try:
            manager = await PluginManager.get_global(self.workdir or os.getcwd())
            updated = await manager.trigger(
                "tool.definition",
                {
                    "tool_id": definition.name,
                    "session_id": self.session_id,
                    "agent": self.agent_info.name,
                },
                {
                    "description": definition.description,
                    "parameters": definition.parameters,
                },
            )
            description = str(updated.get("description", definition.description))
            parameters = updated.get("parameters", definition.parameters)
            if not isinstance(parameters, dict):
                parameters = definition.parameters
            return ToolDefinition(
                name=definition.name,
                description=description,
                parameters=parameters,
                provider_id=definition.provider_id,
                provider_instance_id=definition.provider_instance_id,
                provider_version=definition.provider_version,
                definition_version=definition.definition_version,
            )
        except Exception:
            return definition

    # 用途：在工具执行前触发插件钩子，并允许插件改写调用参数。
    async def _apply_plugin_before_tool_execute(self, tool_call: ToolCall) -> bool:
        """执行现有 before hook，并告诉只观察链路 Hook 是否成功。

        参数:
            tool_call: 允许现有插件原地更新参数的工具调用。

        返回:
            Hook 调度完成时返回 ``True``；兼容性吞掉异常时返回
            ``False``。返回值只用于 Shadow 分类，不改变原执行语义。
        """

        try:
            manager = await PluginManager.get_global(self.workdir or os.getcwd())
            updated = await manager.trigger(
                "tool.execute.before",
                {
                    "tool": tool_call.tool_name,
                    "session_id": self.session_id,
                    "call_id": tool_call.call_id,
                    "agent": self.agent_info.name,
                },
                {
                    "args": dict(tool_call.arguments or {}),
                },
            )
            args = updated.get("args")
            if isinstance(args, dict):
                tool_call.arguments = args
            return True
        except Exception:
            return False

    # 用途：在工具执行后触发插件钩子，并允许插件改写工具结果。
    async def _apply_plugin_after_tool_execute(
        self, tool_call: ToolCall, result: ToolResult
    ) -> ToolResult:
        try:
            manager = await PluginManager.get_global(self.workdir or os.getcwd())
            updated = await manager.trigger(
                "tool.execute.after",
                {
                    "tool": tool_call.tool_name,
                    "session_id": self.session_id,
                    "call_id": tool_call.call_id,
                    "agent": self.agent_info.name,
                    "args": dict(tool_call.arguments or {}),
                },
                {
                    "title": result.title,
                    "output": result.output,
                    "metadata": dict(result.metadata or {}),
                    "error": result.error,
                },
            )
            metadata = updated.get("metadata", result.metadata)
            if not isinstance(metadata, dict):
                metadata = result.metadata
            return ToolResult(
                title=str(updated.get("title", result.title)),
                output=str(updated.get("output", result.output)),
                metadata=metadata,
                truncated=result.truncated,
                output_path=result.output_path,
                error=updated.get("error", result.error),
            )
        except Exception:
            return result

    # 用途：汇总内置、MCP 和插件工具，应用权限与执行配置后生成模型工具定义。
    def _discovered_tool_ids(self) -> list[str]:
        session = getattr(self, "_existing_session", None)
        metadata = dict(getattr(session, "metadata", {}) or {})
        # ponytail: sessions without a task fingerprint retain at most 32 IDs;
        # use explicit task IDs if independent tasks must share one session.
        state = metadata.get("tool_discovery") or getattr(self, "_tool_discovery", {})
        if not isinstance(state, dict):
            return []
        if state.get("task_goal_hash", "") != metadata.get("task_goal_hash", ""):
            return []
        ids = state.get("tool_ids", [])
        return [name for name in ids if isinstance(name, str)][:32] if isinstance(ids, list) else []

    def _selected_workflow_key(self) -> str:
        """Host routing of the latest real request; discovery is not authorization."""
        from ..workflow.router import detect_workflow_route
        from ..workflow.registry import WorkflowTemplateRegistry
        from ..message_context import is_real_user_message
        for message in reversed(getattr(self, "messages", []) or []):
            if not is_real_user_message(message):
                continue
            decision = detect_workflow_route(str(message.content or ""), requested_agent_name=self.agent_info.name)
            template = WorkflowTemplateRegistry.get(decision.template_key) if decision.should_start else None
            if template is None:
                return ""
            metadata = dict(template.metadata or {})
            owner = str(metadata.get("owner_user_id") or "")
            user = str(getattr(getattr(self, "_existing_session", None), "user_id", "") or "")
            return "" if metadata.get("private") and owner and owner != user else template.template_key
        return ""

    def _tool_discovery_status(self, name: str) -> dict[str, Any]:
        """Explain exact-name rejection without publishing hidden schemas."""
        if not self._is_tool_enabled_for_agent(name) or self._agent_declares_no_tools():
            reason = "permission_denied"
        elif not profile_allows_tool(self._execution_profile, name):
            reason = "execution_profile_blocked"
        elif name in {tool.name for tool in self._discoverable_tool_definitions()}:
            return {"available": True, "reason": "admitted", "next_action": "activate"}
        elif ToolRegistry.get(name) is None:
            reason = "not_registered_or_unavailable"
        else:
            reason = "not_admitted_for_current_agent"
        return {"available": False, "reason": reason, "next_action": "stop_discovery"}

    def _discoverable_tool_definitions(self) -> list[ToolDefinition]:
        if self._agent_declares_no_tools():
            return []
        local_ids = {tool.id for tool in ToolRegistry.all(
            self.agent_info, execution_profile=self._execution_profile,
            explicit_workflow=bool(self._selected_workflow_key()),
            discoverable=True,
        )}
        from ..mcp.agent_specific_mcp import AgentSpecificMCPManager
        mcp_ids = {tool.id for tool in AgentSpecificMCPManager()._filter_tools_by_agent(
            self.agent_info, getattr(self, "_mcp_discovery_tools", []),
        )}
        return [tool for tool in getattr(self, "_tool_definition_candidates", [])
                if (tool.name in local_ids or tool.name in mcp_ids)
                and self._is_tool_enabled_for_agent(tool.name)
                and profile_allows_tool(self._execution_profile, tool.name)]

    async def _activate_discovered_tools(self, tool_ids: list[str], *, reset: bool = False) -> dict[str, Any]:
        if not hasattr(self, "_tool_discovery_lock"):
            self._tool_discovery_lock = asyncio.Lock()
        async with self._tool_discovery_lock:
            return await self._update_discovered_tools(tool_ids, reset=reset)

    async def _update_discovered_tools(self, tool_ids: list[str], *, reset: bool = False) -> dict[str, Any]:
        allowed = {tool.name for tool in self._discoverable_tool_definitions()}
        requested = list(dict.fromkeys(tool_ids))
        session = getattr(self, "_existing_session", None)

        def update(current):
            metadata = dict(current.metadata or {})
            old = metadata.get("tool_discovery") or {}
            old = old if isinstance(old, dict) else {}
            previous = old.get("tool_ids", [])
            if reset or not isinstance(previous, list) or old.get("task_goal_hash", "") != metadata.get("task_goal_hash", ""):
                previous = []
            merged = list(dict.fromkeys([*(name for name in previous if isinstance(name, str) and name in allowed),
                                         *(name for name in requested if name in allowed)]))
            active = merged[:32]
            metadata["tool_discovery"] = {"task_goal_hash": metadata.get("task_goal_hash", ""), "tool_ids": active}
            current.metadata = metadata
            return {"activated": [name for name in requested if name in active],
                    "rejected": [name for name in requested if name not in active],
                    "rejection_reasons": {name: (self._tool_discovery_status(name) if name not in allowed else
                        {"available": False, "reason": "activation_limit", "next_action": "reset_or_reduce_selection"})
                        for name in requested if name not in active},
                    "active_tool_ids": active, "effective_on": "next_model_call"}, current

        if session is not None:
            from ..session.manager import SessionManager
            scope = {key: getattr(session, key, None)
                     for key in ("tenant_id", "user_id", "workspace_id")}
            updated = await SessionManager.mutate(session.id, update, **scope)
            if updated is None:
                raise RuntimeError("Tool activation unavailable: session ownership or existence changed")
            payload, self._existing_session = updated
            self._tool_discovery = self._existing_session.metadata["tool_discovery"]
            return payload
        from types import SimpleNamespace
        payload, current = update(SimpleNamespace(metadata={"tool_discovery": getattr(self, "_tool_discovery", {})}))
        self._tool_discovery = current.metadata["tool_discovery"]
        return payload

    def _state_preloaded_tool_ids(self) -> list[str]:
        """Read owner-scoped runtime state; this only selects admitted definitions."""
        selected = set()
        if self._selected_workflow_key():
            selected.add("workflow_run")
        session = getattr(self, "_existing_session", None)
        if session is None:
            return sorted(selected)
        from ..runtime.structure_view import bridge
        owner = tuple(str(getattr(session, key, "") or "")
                      for key in ("tenant_id", "user_id", "workspace_id", "id"))
        if all(owner) and bridge.current_context(owner):
            selected.add("structure_view")
        from ..runtime.task_runtime import TaskRuntimeStore
        metadata = dict(getattr(session, "metadata", {}) or {})
        workspace = metadata.get("agent_workspace") or {}
        workdir = str(workspace.get("control_workdir") or metadata.get("control_workdir")
                      or getattr(self, "workdir", "") or "")
        # Avoid creating runtime directories just to inspect absent state.
        runtime_root = Path(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd())
        if (runtime_root / ".tcode/runtime/task_runtime/runtimes.json").is_file():
            for item in TaskRuntimeStore.list(session_id=session.id, workdir=str(runtime_root)):
                if item.get("status") not in {"queued", "running", "waiting_external", "waiting_review"}:
                    continue
                kind = item.get("runtime_kind")
                if kind in {"subagent", "subagent_task"}:
                    selected.add("task_wait")
                elif kind in {"process", "sandbox_job"}:
                    selected.add("process")
                elif kind == "mcp_job":
                    selected.update(tool.id for tool in getattr(self, "_mcp_discovery_tools", [])
                                    if tool.server_name == "job-protocol-mcp"
                                    and any(word in tool._tool_name.lower() for word in ("status", "result", "cancel", "get_job")))
                elif kind == "agent_tool" and item.get("tool_name") in {"task", "process", "exec"}:
                    selected.add("task_wait" if item.get("tool_name") == "task" else "process")
        return sorted(selected)

    async def _get_core_tool_definitions(self) -> list[ToolDefinition]:
        """Local-only fallback; apply the same admission and definition hooks."""
        if self._agent_declares_no_tools():
            return []
        core = {"capability_catalog", "read", "glob", "grep", "inspect_file", "question", "task_wait", "process"}
        from ..tools.builtin.goal_checkpoint import goal_checkpoint_active
        if goal_checkpoint_active.get():
            core.add("goal_checkpoint")
        explicit_workflow = bool(self._selected_workflow_key())
        if explicit_workflow:
            core.add("workflow_run")
        result = []
        for tool in ToolRegistry.all(self.agent_info, execution_profile=self._execution_profile,
                                     explicit_workflow=explicit_workflow):
            if tool.id not in core or not self._is_tool_enabled_for_agent(tool.id):
                continue
            definition = ToolDefinition(name=tool.id, description=tool.description,
                                        parameters=self._get_tool_schema(tool),
                                        **ToolRegistry.definition_provenance(tool))
            result.append(await self._apply_plugin_tool_definition_hook(definition))
        self._tool_definition_candidates = self._deduplicate_tool_definitions(result)
        return self._tool_definition_candidates

    async def _get_tool_definitions(self) -> list[ToolDefinition]:
        """构建当前 model step 的工具定义与参数 Schema 来源。

        返回:
            已应用 execution profile、definition hook 和重名规则的工具
            定义列表。当 DSH-203D2 未启用 build 上下文时，Schema
            来源记录为零操作。
        """

        from ..presentation.canvas import enabled as deployment_ui_enabled
        session = getattr(self, "_existing_session", None)
        tenant_id = getattr(session, "tenant_id", "")
        user_id = getattr(session, "user_id", "")
        intelligent_ui_enabled = deployment_ui_enabled()
        if intelligent_ui_enabled and tenant_id and user_id:
            from ..server.auth import intelligent_ui_preference
            preference = await asyncio.to_thread(intelligent_ui_preference, tenant_id, user_id)
            intelligent_ui_enabled = preference["effective_enabled"]
        self._intelligent_ui_generation_disabled = not intelligent_ui_enabled

        if self._agent_declares_no_tools():
            return []

        # Strong text-only intent can bypass skill/MCP discovery entirely. All
        # ambiguous requests keep the normal fail-open catalog path.
        from ..tools.builtin.goal_checkpoint import goal_checkpoint_active
        goal_routing = (str(getattr(self, '_goal_tool_routing_prompt', '') or '')
                        if goal_checkpoint_active.get() else '')
        try:
            from ..context.tool_router import ToolDefinitionRouter

            early_router = ToolDefinitionRouter()
            if not goal_routing and early_router.should_skip_catalog(messages=list(self.messages or [])):
                self._last_tool_routing = {
                    "schema": "tcode.tool-definition-routing/v1",
                    "applied": True,
                    "categories": ["text"],
                    "original_count": None,
                    "selected_count": 0,
                    "reason": "text_only_preflight_skipped_catalog",
                    "selected_tools": [],
                }
                return []
        except Exception:
            pass

        ### 获取技能列表
        from ..skill import SkillManager

        engine = getattr(self, '_engine', None)
        if engine is not None:
            await engine._emit_llm_progress('loading_skills', iteration=0)
        await self._ensure_skill_catalog_loaded()
        await self._maybe_preload_default_skills(
            parent_msg_id=self._latest_user_message_id(),
        )
        if engine is not None:
            await engine._emit_llm_progress('matching_skills', iteration=0)
        await self._recommend_skills_without_blocking()

        from ..tools import ensure_team_tools_registered

        ensure_team_tools_registered()

        #  获取可用的技能
        initial_local_ids = {tool.id for tool in ToolRegistry.all(
            self.agent_info,
            execution_profile=self._execution_profile,
            explicit_workflow=bool(self._selected_workflow_key()),
        )}
        regular_tools = ToolRegistry.all(
            self.agent_info,
            execution_profile=self._execution_profile,
            explicit_workflow=bool(self._selected_workflow_key()),
            discoverable=True,
        )
        deferred_local_ids = {tool.id for tool in regular_tools} - initial_local_ids

        result = []
        for tool in regular_tools:
            if not self._is_tool_enabled_for_agent(tool.id if hasattr(tool, "id") else tool.name):
                continue
            description = tool.description
            tool_name = tool.id if hasattr(tool, "id") else tool.name
            if not intelligent_ui_enabled and tool_name in {"canvas", "intelligent_page"}:
                continue
            if tool_name == "skill":
                visible_skills = [
                    skill for skill in SkillManager.all() if self._is_skill_visible_to_agent(skill)
                ]
                if self._recommended_skills:
                    lines = [
                        "Load a specialized skill that provides domain-specific instructions and workflows.",
                        "",
                        "Recommended skills for this request:",
                    ]
                    for item in self._recommended_skills[:3]:
                        lines.append(
                            f"- **{item.get('name', 'unknown')}**: {item.get('description', '') or item.get('reason', '')} ({item.get('reason', '相关')})"
                        )
                    lines.append("")
                    lines.append("Only load a full skill when you need its detailed workflow.")
                    description = "\n".join(lines)
                elif visible_skills:
                    skill_list = "\n".join(
                        [f"- **{s.name}**: {s.description}" for s in visible_skills[:10]]
                    )
                    description = (
                        "Load a specialized skill that provides domain-specific instructions and workflows.\n\n"
                        "Available skills:\n"
                        f"{skill_list}\n\n"
                        "Use this tool when you recognize that a task matches one of the available skills."
                    )
                elif not self._skill_recommendation_enabled():
                    description = "Load a specialized skill when the task clearly requires domain-specific instructions. Fast mode usually avoids loading skills unless necessary."
            provenance = ToolRegistry.definition_provenance(tool)
            model_schema_before_hook = self._get_tool_schema(tool)
            if not intelligent_ui_enabled and tool_name == "research_workspace":
                from ..tools.builtin.research_workspace import INTELLIGENT_UI_ACTIONS
                action = model_schema_before_hook["properties"]["action"]
                action["enum"] = [value for value in action["enum"] if value not in INTELLIGENT_UI_ACTIONS]
                description = description.split("Use show_hypothesis_map", 1)[0]
            definition = ToolDefinition(
                name=tool_name,
                description=description,
                parameters=model_schema_before_hook,
                **provenance,
            )
            result.append(await self._apply_plugin_tool_definition_hook(definition))

        # Preserve completed local definitions if subsequent remote discovery times out.
        self._tool_definition_candidates = self._deduplicate_tool_definitions(result)

        # 获取MCP
        try:
            mcp_manager = await get_agent_specific_mcp_manager()
            agent_tools = await mcp_manager.setup_agent_mcp_tools(self.agent_info)
            self._mcp_discovery_tools = list(agent_tools or [])

            if agent_tools:
                # Add agent-specific MCP tools
                for tool in agent_tools:
                    if not self._is_tool_enabled_for_agent(tool.id):
                        continue
                    if not profile_allows_tool(self._execution_profile, tool.id):
                        continue
                    server_name = str(getattr(tool, "server_name", "") or "unknown")
                    model_schema_before_hook = self._get_tool_schema(tool)
                    provider_id = f"mcp:{server_name}"
                    definition = ToolDefinition(
                        name=tool.id,
                        description=tool.description,
                        parameters=model_schema_before_hook,
                        provider_id=provider_id,
                        provider_instance_id=str(
                            getattr(tool, "instance_id", "") or ""
                        ),
                        provider_version="unversioned",
                        definition_version="unversioned",
                    )
                    result.append(
                        await self._apply_plugin_tool_definition_hook(definition)
                    )
        except Exception as e:
            # Fallback to original method if agent-specific MCP fails
            print(f"Warning: Could not load agent-specific MCP tools: {e}")
            from ..mcp.agent_integration import MCPTool
            from ..mcp.agent_specific_mcp import AgentSpecificMCPManager
            mcp_tools_dict = await MCPManager.get_tools()
            adapters = [MCPTool(str(info.get("server") or ""), str(info.get("tool") or ""),
                               str(info.get("description") or ""), info.get("inputSchema") or {}, info.get("_meta") or {})
                        for info in mcp_tools_dict.values()]
            self._mcp_discovery_tools = AgentSpecificMCPManager()._filter_tools_by_agent(self.agent_info, adapters)
            admitted_mcp_ids = {tool.id for tool in self._mcp_discovery_tools}
            for tool_id, tool_info in mcp_tools_dict.items():
                if tool_id not in admitted_mcp_ids:
                    continue
                if not self._is_tool_enabled_for_agent(tool_id):
                    continue
                if not profile_allows_tool(self._execution_profile, tool_id):
                    continue
                server_name = str(tool_info.get("server") or "unknown")
                adapter = next(item for item in adapters if item.id == tool_id)
                input_schema = adapter._input_schema
                definition = ToolDefinition(
                    name=tool_id,
                    description=adapter.description,
                    parameters=input_schema,
                    provider_id=f"mcp:{server_name}",
                    provider_instance_id="",
                    provider_version="unversioned",
                    definition_version="unversioned",
                )
                result.append(await self._apply_plugin_tool_definition_hook(definition))

        definitions = sorted(self._deduplicate_tool_definitions(result), key=lambda tool: tool.name)
        self._tool_definition_candidates = definitions
        # 目录保留完整候选，但专用工具只有激活后才送入模型，避免首轮膨胀。
        activated = self._discovered_tool_ids()
        if goal_routing:
            activated = list(dict.fromkeys([*activated, "goal_checkpoint"]))
        routing_definitions = [tool for tool in definitions
                               if tool.name not in deferred_local_ids or tool.name in activated]
        try:
            from ..context.tool_router import ToolDefinitionRouter

            try:
                state_tools = self._state_preloaded_tool_ids()
            except Exception as exc:
                # Broken optional UI/runtime state must not disable schema budgeting.
                logging.getLogger(__name__).warning("Tool state preload unavailable: %s", type(exc).__name__)
                state_tools = []
            routing = ToolDefinitionRouter().route(
                routing_definitions,
                messages=list(self.messages or []),
                goal=goal_routing,
                activated_tool_ids=activated,
                state_tool_ids=state_tools,
            )
            self._last_tool_routing = routing.telemetry()
            self._last_tool_routing["state_preloaded_tools"] = state_tools
            return routing.tools
        except Exception as exc:
            self._last_tool_routing = {
                "schema": "tcode.tool-definition-routing/v1",
                "applied": False,
                "original_count": len(routing_definitions),
                "selected_count": len(routing_definitions),
                "reason": f"routing_error:{type(exc).__name__}",
            }
            return routing_definitions

    # 用途：模型接口要求单次请求内工具名唯一；插件/MCP 全局注册可能让同一工具从多个来源出现。
    @staticmethod
    def _deduplicate_tool_definitions(
        tools: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        seen: set[str] = set()
        deduped: list[ToolDefinition] = []
        duplicates: list[str] = []
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if name in seen:
                duplicates.append(name)
                continue
            seen.add(name)
            deduped.append(tool)
        if duplicates:
            duplicate_names = ", ".join(sorted(set(duplicates)))
            print(f"[Agent] Skipped duplicate tool definitions: {duplicate_names}")
        return deduped

    # 用途：从 Pydantic 参数模型或工具对象中提取 JSON Schema。
    def _get_tool_schema(self, tool) -> dict:
        """从工具参数获取 JSON Schema
            Pydantic model_json_schema() → schema 属性 → 默认空对象"""
        if isinstance(getattr(tool, "_input_schema", None), dict):
            return deepcopy(tool._input_schema)
        try:
            # Try pydantic model
            if hasattr(tool.parameters, "model_json_schema"):
                model = tool.parameters
                cache = getattr(self, "_tool_parameter_schema_cache", None)
                if cache is None:
                    cache = self._tool_parameter_schema_cache = {}
                if model not in cache:
                    if len(cache) >= 128:
                        cache.clear()
                    cache[model] = model.model_json_schema()
                return deepcopy(cache[model])
            elif hasattr(tool.parameters, "schema"):
                return tool.parameters.schema
        except:
            pass
        return {"type": "object", "properties": {}}

    # 用途：读取调试开关，判断是否需要把实际提示词载荷写入磁盘。
    def _prompt_dump_enabled(self) -> bool:
        raw = str(os.environ.get("TCODE_DEBUG_PROMPT_DUMP", "false") or "false").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    # 用途：解析提示词调试快照的输出目录。
    def _prompt_dump_dir(self) -> Path:
        base = self.workdir or os.getcwd()
        configured = str(os.environ.get("TCODE_DEBUG_PROMPT_DIR", "") or "").strip()
        if configured:
            return Path(configured)
        return Path(base) / ".tcode" / "debug" / "prompts"

    # 用途：把提供方消息对象转换成可写入 JSON 的调试数据。
    def _serialize_messages_for_dump(self, messages_for_api: list[ChatMessage]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for msg in messages_for_api:
            payload.append(
                {
                    "role": msg.role,
                    "content": msg.content,
                    "tool_calls": msg.tool_calls,
                    "tool_call_id": msg.tool_call_id,
                }
            )
        return payload

    # 用途：把工具定义转换成可写入 JSON 的调试数据。
    def _serialize_tools_for_dump(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for tool in tools:
            payload.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
        return payload

    # 用途：将系统提示词、消息、工具和模型信息保存为调试快照。
    def _dump_prompt_payload(
        self,
        *,
        messages_for_api: list[ChatMessage],
        tools: list[ToolDefinition],
    ) -> None:
        if not self._prompt_dump_enabled():
            return
        try:
            dump_dir = self._prompt_dump_dir() / self.session_id
            dump_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time() * 1000)
            payload = {
                "session_id": self.session_id,
                "agent_name": self.agent_info.name,
                "reasoning_effort": self._reasoning_effort,
                "execution_profile": self._execution_profile,
                "scheduler_mode": self._scheduler_mode,
                "tool_count": len(tools or []),
                "message_count": len(messages_for_api or []),
                "provider": getattr(self.provider, "name", "unknown"),
                "model": self._get_provider_model_id(),
                "context_assembly": dict(self._last_context_assembly or {}),
                "tool_routing": dict(self._last_tool_routing or {}),
                "messages": self._serialize_messages_for_dump(messages_for_api),
                "tools": self._serialize_tools_for_dump(tools or []),
            }
            path = dump_dir / f"{timestamp}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[PROMPT_DUMP] saved prompt payload to {path}")
        except Exception as exc:
            print(f"[PROMPT_DUMP] failed: {exc}")

    
    # 用途：把模型返回的原始工具调用字典解析为内部 ToolCall 对象。
    def _parse_tool_call(self, tc: dict) -> ToolCall:
        """从 LLM 响应解析工具调用
        返回：ToolCall 对象，包含 tool_name, call_id, arguments"""
        tool_call = ToolCall(
            tool_name=tc.get("function", {}).get("name", ""),
            call_id=tc.get("id", ""),
            arguments=tc.get("function", {}).get("arguments", {}),
        )
        parse_error = tc.get("_tcode_argument_parse_error")
        if isinstance(parse_error, dict) and parse_error:
            tool_call.metadata["argument_parse_error"] = dict(parse_error)
        if not tool_call.tool_name.startswith("mcp_"):
            self._unwrap_tool_argument_envelope(tool_call, ToolRegistry.get(tool_call.tool_name))
        return tool_call

    def _unwrap_tool_argument_envelope(self, tool_call: ToolCall, tool) -> None:
        if tool_call.metadata.get("argument_parse_error"):
            return
        # Some providers emit the function-call transport envelope as the
        # parameter object. Only unwrap a serialized, schema-valid object;
        # never coerce values, infer missing fields, or rewrite hook arguments.
        arguments = tool_call.arguments
        if tool is not None and isinstance(arguments, dict) and set(arguments) == {"arguments"} and isinstance(arguments["arguments"], str):
            from ..runtime.tool_arguments import validate_tool_arguments
            schema = self._get_tool_schema(tool)
            if schema.get("properties") and "arguments" not in schema["properties"]:
                candidate = arguments["arguments"]
                try:
                    for _ in range(2):
                        if not isinstance(candidate, str):
                            break
                        candidate = json.loads(candidate)
                except (ValueError, TypeError):
                    candidate = None
                if isinstance(candidate, dict) and validate_tool_arguments(tool, candidate).accepted:
                    tool_call.metadata["argument_transport_envelope"] = arguments
                    tool_call.arguments = candidate
                else:
                    # Optional-parameter schemas may accept unknown keys, but
                    # an invalid transport envelope must not execute defaults.
                    tool_call.metadata["argument_parse_error"] = {"reason": "invalid_argument_transport_envelope"}

    # 用途：把单次工具调用交给统一工具执行器处理。
    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """执行一次工具调用。具体逻辑委托给 harness.tool_executor。"""
        gate = getattr(self, "_execution_gate", None)
        if gate is not None:
            await gate(tool_call)
        return await self._tool_executor.execute(tool_call)

    ### llm 交互方法
    # 用途：通过提示词构建器组装上下文并调用模型提供方。
    async def _call_llm(
        self, tools: list[ToolDefinition]
    ) -> tuple[str, Optional[list[dict]], Any]:
        """组装 prompt 并调用 LLM。具体逻辑委托给 harness.prompt_builder。"""
        await self._ensure_plugin_skills_registered()
        return await self._prompt_builder.call_llm(tools)


    ## 输出验证方法
    # 用途：验证模型输出是否符合目标 Schema，必要时自动修复。
    async def _ensure_structured_output(self, content: str) -> str:
        """用途：确保输出符合 JSON Schema
        验证 → 失败则修复 → 再验证 → 返回结果或错误信息
        """
        if not (
            isinstance(self._output_format, dict)
            and self._output_format.get("type") == "json_schema"
            and isinstance(self._output_format.get("schema"), dict)
        ):
            return content

        schema = self._output_format["schema"]
        ok, errors = self._validate_json_schema_text(content, schema)
        if ok:
            return content

        repaired = await self._repair_structured_output(content, schema, errors)
        ok_repaired, repaired_errors = self._validate_json_schema_text(repaired, schema)
        if ok_repaired:
            return repaired

        return json.dumps(
            {
                "error": "structured_output_validation_failed",
                "details": repaired_errors[:5],
                "raw": content,
            },
            ensure_ascii=False,
        )

    # 用途：提示模型根据校验错误修复不合规的结构化输出。
    async def _repair_structured_output(
        self, content: str, schema: dict[str, Any], errors: list[str]
    ) -> str:
        """用途：使用 LLM 修复无效的 JSON 输出"""
        try:
            prompt = (
                "Your previous output failed JSON schema validation. "
                "Return ONLY corrected JSON, no markdown, no commentary.\n\n"
                f"Schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Validation errors:\n{json.dumps(errors[:10], ensure_ascii=False)}\n\n"
                f"Previous output:\n{content}"
            )
            try:
                timeout_seconds = float(
                    os.environ.get(
                        "TCODE_STRUCTURED_OUTPUT_REPAIR_TIMEOUT_SECONDS",
                        "60",
                    )
                )
            except Exception:
                timeout_seconds = 60.0
            timeout_seconds = max(5.0, min(timeout_seconds, 300.0))
            repair = await asyncio.wait_for(
                self.provider.chat(
                    messages=[
                        ChatMessage(
                            role="system",
                            content="You are a strict JSON formatter.",
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    tools=None,
                    temperature=0,
                ),
                timeout=timeout_seconds,
            )
            return repair.content
        except Exception:
            return content

    # 用途：解析 JSON 文本并返回其是否满足指定 Schema 及错误列表。
    def _validate_json_schema_text(
        self, content: str, schema: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """验证文本是否符合 JSON Schema
        返回：(是否有效，错误列表)
        """
        try:
            data = json.loads(content)
        except Exception:
            return False, ["Output is not valid JSON"]

        errors: list[str] = []
        self._validate_schema_node(data, schema, "$", errors)
        return len(errors) == 0, errors

    # 用途：递归校验一个 JSON 节点的类型、必填字段和子节点结构。
    def _validate_schema_node(
        self, value: Any, schema: dict[str, Any], path: str, errors: list[str]
    ):
        """
        递归验证 JSON 节点是否符合 Schema
        """
        schema_type = schema.get("type")

        # 用途：判断当前 Python 值是否符合 Schema 声明的基础类型。
        def type_ok(v: Any, expected: str) -> bool:
            if expected == "object":
                return isinstance(v, dict)
            if expected == "array":
                return isinstance(v, list)
            if expected == "string":
                return isinstance(v, str)
            if expected == "number":
                return isinstance(v, (int, float)) and not isinstance(v, bool)
            if expected == "integer":
                return isinstance(v, int) and not isinstance(v, bool)
            if expected == "boolean":
                return isinstance(v, bool)
            if expected == "null":
                return v is None
            return True

        if isinstance(schema_type, str):
            if not type_ok(value, schema_type):
                errors.append(f"{path}: expected {schema_type}")
                return

        if schema_type == "object":
            required = schema.get("required", []) or []
            for key in required:
                if key not in value:
                    errors.append(f"{path}: missing required field '{key}'")

            props = schema.get("properties", {}) or {}
            for key, sub_schema in props.items():
                if key in value and isinstance(sub_schema, dict):
                    self._validate_schema_node(
                        value[key], sub_schema, f"{path}.{key}", errors
                    )

        if schema_type == "array":
            items_schema = schema.get("items")
            if isinstance(items_schema, dict):
                for idx, item in enumerate(value):
                    self._validate_schema_node(
                        item, items_schema, f"{path}[{idx}]", errors
                    )
    
    #### 权限处理方法
    # 用途：按模式覆盖、工具别名和权限规则解析最终授权动作。
    def _resolve_permission_action(
        self, tool_name: str, patterns: list[str] | None = None,
    ) -> str:
        action = self._resolve_permission_action_base(tool_name, patterns)
        if (getattr(self, '_review_read_permissions', False)
                and tool_name == 'read' and action != 'deny'):
            return 'ask'
        return action

    def _resolve_permission_action_base(
        self,
        tool_name: str,
        patterns: list[str] | None = None,
    ) -> str:

        """
        根据代理权限规则解析工具操作
        返回：allow, deny, 或 ask
        """
        rules = self.agent_info.permission or {}

        mode_override = self._mode_tool_override(tool_name)
        if mode_override == "deny":
            return mode_override
        if mode_override == "allow":
            return mode_override

        alias_candidates = self._permission_alias_candidates(tool_name)

        # Safe presentation tools are globally available even for narrowly
        # scoped agents that use `"*": "deny"`. A specific tool/wildcard rule
        # can still explicitly deny them, and mode overrides above still win.
        if str(tool_name or "").strip() == "present_image":
            explicit_action = None
            for pattern, action in rules.items():
                if str(pattern) == "*" or not isinstance(action, str):
                    continue
                if any(
                    self._matches_permission_pattern(str(pattern), candidate)
                    for candidate in alias_candidates
                ):
                    explicit_action = action
            resolved_explicit = str(explicit_action or "allow").strip().lower()
            return (
                resolved_explicit
                if resolved_explicit in {"allow", "deny", "ask"}
                else "allow"
            )

        matched_action = None
        for pattern, action in rules.items():
            for candidate in alias_candidates:
                if not self._matches_permission_pattern(str(pattern), candidate):
                    continue
                if isinstance(action, dict) and patterns is not None:
                    matched_action = self._resolve_nested_permission_action(
                        action,
                        patterns,
                    )
                    break
                if isinstance(action, str):
                    matched_action = action
                    break

        resolved = str(matched_action or mode_override or "allow").strip().lower()
        return resolved if resolved in {"allow", "deny", "ask"} else "allow"

    # 用途：在工具上下文中执行权限检查，并处理单次或永久授权结果。
    async def _ask_permission_from_context(
        self, permission: str, patterns: list[str]
    ) -> bool:
        review_read = permission == 'read' and getattr(self, '_review_read_permissions', False)
        if not review_read and permission in self._always_allowed_tools:
            return True
        if not review_read and self._permission_patterns_always_allowed(permission, patterns):
            return True

        action = self._resolve_permission_action(permission, patterns)
        if action == "deny":
            return False
        if action == "ask":
            decision = await self._request_permission_decision(
                permission,
                {"patterns": patterns},
            )
            if isinstance(decision, bool):
                return decision

            normalized = str(decision).strip().lower()
            if normalized in {"always", "allow_always"}:
                self._remember_permission_patterns(permission, patterns)
                return True
            return normalized in {"allow", "once", "y", "yes", "true"}
        return True

    # 用途：通过前端回调或终端提示请求用户作出工具权限决定。
    async def _request_permission_decision(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | bool:
        engine = getattr(self, "_engine", None)
        if engine is not None and hasattr(engine, "mark_waiting_for_user"):
            try:
                await engine.mark_waiting_for_user(
                    "permission",
                    {"tool_name": tool_name, "arguments": dict(arguments or {})},
                )
            except Exception:
                pass
        try:
            if self._permission_handler:
                decision = await self._permission_handler(tool_name, arguments)
                if isinstance(decision, bool):
                    return decision
                return str(decision).strip().lower()

            if not sys.stdin or not sys.stdin.isatty():
                return False

            prompt = (
                f"\n⚠️ Permission required for tool '{tool_name}'\n"
                f"Arguments: {json.dumps(arguments, ensure_ascii=False)[:300]}\n"
                "Allow this call? [y/N]: "
            )

            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, input, prompt)
            return answer.strip().lower()
        finally:
            if engine is not None and hasattr(engine, "mark_waiting_for_user_done"):
                try:
                    await engine.mark_waiting_for_user_done()
                except Exception:
                    pass

    # 用途：请求某次工具调用授权，并缓存用户选择的始终允许工具。
    async def _ask_tool_permission(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> bool:
        decision = await self._request_permission_decision(tool_name, arguments)
        if isinstance(decision, bool):
            return decision

        normalized = str(decision).strip().lower()
        if normalized in {"always", "allow_always"}:
            self._always_allowed_tools.add(tool_name)
            return True
        return normalized in {"allow", "once", "y", "yes", "true"}

    # 用途：把结构化问题交给交互处理器，并维护等待用户的运行状态。
    async def _ask_question_from_context(self, payload: dict[str, Any]) -> str:
        engine = getattr(self, "_engine", None)
        if engine is not None and hasattr(engine, "mark_waiting_for_user"):
            try:
                await engine.mark_waiting_for_user(
                    "question",
                    {"payload": dict(payload or {})},
                )
            except Exception:
                pass
        try:
            if self._question_handler:
                result = await self._question_handler(payload)
                return "" if result is None else str(result)
            return ""
        finally:
            if engine is not None and hasattr(engine, "mark_waiting_for_user_done"):
                try:
                    await engine.mark_waiting_for_user_done()
                except Exception:
                    pass

    # 用途：尝试利用会话资源上下文自动推断问题答案，减少重复询问。
    async def _resolve_question_from_context(
        self,
        payload: dict[str, Any],
    ) -> Optional[str]:
        try:
            from ..workflow.clarifications import infer_answer_from_context

            answer = infer_answer_from_context(
                payload,
                self._session_resource_context,
            )
            return None if answer is None else str(answer)
        except Exception:
            return None

    # 用途：把工具参数中的长值压短，避免兜底摘要看起来像原始日志。
    @staticmethod
    def _summary_value(value: Any, *, limit: int = 90) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    # 用途：从常见参数名里挑一个适合展示的路径或标识。
    @classmethod
    def _summary_argument(
        cls,
        arguments: dict[str, Any],
        *keys: str,
        limit: int = 90,
    ) -> str:
        for key in keys:
            value = arguments.get(key)
            if value:
                return cls._summary_value(value, limit=limit)
        return ""

    # 用途：为没有最终自然语言回复的工具调用生成更像人说话的兜底摘要。
    def _tool_summary_sentence(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        normalized = str(tool_name or "").strip().lower()
        path = self._summary_argument(arguments, "filePath", "path", "file", "target")

        if normalized == "write":
            return (
                f"我已经把结果写到 `{path}`。"
                if path
                else "我已经把结果写入文件。"
            )
        if normalized == "edit":
            return f"我已经更新 `{path}`。" if path else "我已经完成文件修改。"
        if normalized == "read":
            return (
                f"我读过 `{path}`，并把关键信息带回上下文。"
                if path
                else "我已经读取相关文件，并把关键信息带回上下文。"
            )
        if normalized in {"grep", "glob"}:
            pattern = self._summary_argument(
                arguments,
                "pattern",
                "query",
                "glob",
                "include",
                limit=70,
            )
            if pattern:
                return f"我在项目里查过 `{pattern}`，用匹配结果缩小了范围。"
            return "我在项目里查过相关线索，并把结果带回上下文。"
        if normalized == "bash":
            command = self._summary_argument(arguments, "command", limit=100)
            return (
                f"我跑了 `{command}`，命令结果已经带回上下文。"
                if command
                else "我已经运行本地命令，结果已经带回上下文。"
            )
        if normalized == "exec":
            command = self._summary_argument(arguments, "command", "cmd", limit=100)
            return (
                f"我启动了 `{command}`，执行结果已经带回上下文。"
                if command
                else "我已经执行本地任务，结果已经带回上下文。"
            )
        if normalized in {"websearch", "web_search"}:
            query = self._summary_argument(arguments, "query", "q", limit=80)
            return (
                f"我查过公开资料里的 `{query}`，把可用线索带回来了。"
                if query
                else "我查过公开资料，并把可用线索带回来了。"
            )
        if normalized in {"webfetch", "web_fetch"}:
            url = self._summary_argument(arguments, "url", "href", limit=100)
            return (
                f"我打开过 `{url}`，把页面里的关键信息带回上下文。"
                if url
                else "我打开过指定网页，并把关键信息带回上下文。"
            )
        if normalized in {"todo_write", "todowrite"}:
            return "我已经更新任务清单，让后续步骤更好追踪。"
        if normalized in {"todo_read", "todoread"}:
            return "我已经读取任务清单，确认当前进度。"
        if normalized == "browser":
            return "我已经用浏览器检查过页面状态，并把结果带回上下文。"

        display_name = str(tool_name or "工具").strip() or "工具"
        return f"我调用了 `{display_name}`，结果已经回到上下文里。"

    # 用途：从最近一次工具调用生成简短、可直接展示的执行摘要。
    def _build_tool_summary_response(self) -> str:
        summaries: list[str] = []

        for msg in reversed(self.messages):
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_name = str(tc.tool_name or "").strip()
                    arguments = tc.arguments if isinstance(tc.arguments, dict) else {}

                    if tool_name in {"assign_task", "task"}:
                        for tool_msg in reversed(self.messages):
                            if tool_msg.role != "tool" or not tool_msg.tool_calls:
                                continue
                            linked = tool_msg.tool_calls[0]
                            same_call_id = (
                                str(getattr(linked, "call_id", "") or "").strip()
                                and str(getattr(linked, "call_id", "") or "").strip()
                                == str(getattr(tc, "call_id", "") or "").strip()
                            )
                            same_tool_fallback = (
                                not str(getattr(tc, "call_id", "") or "").strip()
                                and str(getattr(linked, "tool_name", "") or "").strip()
                                == tool_name
                            )
                            if same_call_id or same_tool_fallback:
                                content = str(getattr(tool_msg, "content", "") or "").strip()
                                if content:
                                    return content

                    summaries.append(
                        self._tool_summary_sentence(tool_name, arguments)
                    )
                break

        if not summaries:
            return ""

        return "\n".join(summaries)

    # 用途：生成新的 UUID 字符串作为消息或调用标识。
    def _generate_id(self) -> str:
        return str(uuid.uuid4())

    # 用途：返回当前 Unix 时间的毫秒值。
    def _current_time(self) -> int:
        return int(time.time() * 1000)

    # 用途：按模型类型选择基础系统提示词，并追加执行配置与 Skill 推荐信息。
    def _get_system_prompt(self) -> str:
        """Get system prompt based on model type (TCode-like routing)."""
        model_id = self._get_provider_model_id().lower()

        # Mirror tcode session/system.ts provider selection order:
        # 1) gpt-5 -> codex style
        # 2) gpt-/o1/o3 -> codex style (closest local equivalent)
        # 3) gemini -> gemini style
        # 4) claude -> anthropic style
        # 5) trinity -> default style
        # 6) fallback -> default style (qwen-like)
        if "gpt-5" in model_id:
            base_prompt = self._get_codex_prompt()
        elif "gpt-" in model_id or "o1" in model_id or "o3" in model_id:
            base_prompt = self._get_codex_prompt()
        elif "gemini" in model_id:
            base_prompt = self._get_gemini_prompt()
        elif "claude" in model_id:
            base_prompt = self._get_anthropic_prompt()
        elif "trinity" in model_id:
            base_prompt = self._get_default_prompt()
        else:
            base_prompt = self._get_default_prompt()

        extra_sections: list[str] = []
        if self._execution_profile:
            profile = get_execution_profile(self._execution_profile)
            if profile:
                extra_sections.append(
                    "# Execution Profile\n"
                    f"Current execution profile: {profile.name}\n"
                    f"{profile.description}"
                )
            else:
                extra_sections.append(
                    f"# Execution Profile\nCurrent execution profile: {self._execution_profile}"
                )
        skill_prompt = self._build_skill_recommendation_prompt()
        if skill_prompt:
            extra_sections.append(skill_prompt)
        extra_sections.append(load_prompt_template("system.md"))
        if self._active_structure_context:
            extra_sections.append(
                load_prompt_template("current-mol-viewer.md")
                + json.dumps(self._active_structure_context, ensure_ascii=False)
            )
        else:
            extra_sections.append(load_prompt_template("structure-reference.md"))
        try:
            available_tool_ids = set(
                ToolRegistry.ids(
                    self.agent_info,
                    execution_profile=self._execution_profile,
                )
            )
        except Exception:
            available_tool_ids = set()
        if available_tool_ids.intersection({
            "computer_discover", "computer_observe", "computer_element_action",
            "computer_pointer_action", "computer_input_action", "computer_control",
            "computer_type_text", "computer_press_key", "computer_scroll",
        }):
            extra_sections.append(load_prompt_template("desktop-computer-use.md"))
        if "workflow_list_templates" in available_tool_ids:
            extra_sections.append(load_prompt_template("workflow-discovery.md"))
        if "workflow_proposal" in available_tool_ids:
            extra_sections.append(load_prompt_template("workflow-proposal.md"))
        if "scheduled_task" in available_tool_ids:
            extra_sections.append(load_prompt_template("scheduled-tasks.md"))
        if extra_sections:
            return base_prompt + "\n\n" + "\n\n".join(extra_sections)
        return base_prompt

    # 用途：从模型提供方对象中尽力提取当前模型 ID。
    def _get_provider_model_id(self) -> str:
        """Best-effort extraction of current provider model id."""
        for attr in ("model", "_model"):
            value = getattr(self.provider, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    # 用途：构建包含工作目录、平台、日期、模型和执行模式的环境提示词。
    def _get_environment_prompt(self) -> str:
        """Build TCode-like runtime environment context for system prompt."""
        model_id = self._get_provider_model_id() or "unknown"
        workdir = self.workdir or os.getcwd()
        is_git_repo = os.path.isdir(os.path.join(workdir, ".git"))
        current_mode = "plan" if self._plan_mode else "execute"

        return "\n".join(
            [
                "Here is useful information about the environment you are running in:",
                "<env>",
                f"Working directory: {workdir}",
                f"Is directory a git repo: {'yes' if is_git_repo else 'no'}",
                f"Platform: {platform.system().lower()}",
                f"OS Version: {platform.platform()}",
                f"Today's date: {date.today().isoformat()}",
                f"You are powered by model: {model_id}",
                f"Execution mode: {current_mode}",
                (
                    "Plan mode guidance: inspect context, maintain `todo` and `task_graph_read`, "
                    "and use `plan_exit` before execution."
                    if self._plan_mode
                    else "Plan mode guidance: use `plan_enter` before drafting a structured plan."
                ),
                "</env>",
            ]
        )

    # 用途：返回适用于 Qwen 等通用模型的默认系统提示词。
    def _get_default_prompt(self) -> str:
        """Default system prompt for Qwen and similar models"""
        return load_prompt_template("default.md")

    # 用途：返回针对 Claude/Anthropic 模型优化的系统提示词。
    def _get_anthropic_prompt(self) -> str:
        """System prompt optimized for Claude models"""
        return load_prompt_template("anthropic.md")

    # 用途：返回针对 Gemini 模型优化的系统提示词。
    def _get_gemini_prompt(self) -> str:
        """System prompt optimized for Gemini models"""
        return load_prompt_template("gemini.md")

    # 用途：返回针对 GPT/Codex 模型优化的系统提示词。
    def _get_codex_prompt(self) -> str:
        """System prompt optimized for GPT/Codex models"""
        return load_prompt_template("codex.md")

    # 用途：按名称解析配置并创建可运行的 Agent 实例，必要时回退到 build。
    @classmethod
    def create(
        cls,
        name: str,
        provider: LLMProvider,
        session_id: str,
        workdir: Optional[str] = None,
    ) -> "Agent":
        """Create an agent by name"""
        from .agents import BuiltinAgents
        from ..config.manager import ConfigManager

        ConfigManager.load(workdir or None)

        requested_name = (name or "").strip()
        default_name = ConfigManager.get_default_agent() or "build"
        resolved_name = requested_name or default_name
        initial_plan_mode = resolved_name == "plan"
        if initial_plan_mode:
            resolved_name = default_name if default_name != "plan" else "build"

        agent_info = BuiltinAgents.get(resolved_name)
        if not agent_info and resolved_name != "build":
            resolved_name = "build"
            agent_info = BuiltinAgents.get(resolved_name)

        if not agent_info:
            raise ValueError(f"Unknown agent: {resolved_name}")

        agent_config = ConfigManager.get_agent(resolved_name)
        mcp_agent_config = BuiltinAgents.get_agent_mcp_config(resolved_name, workdir)
        merged_agent_config: dict[str, Any] = {}
        if isinstance(agent_config, dict):
            merged_agent_config.update(agent_config)
        if isinstance(mcp_agent_config, dict):
            merged_agent_config.update(mcp_agent_config)
        if merged_agent_config:
            agent_info = BuiltinAgents.apply_config(agent_info, merged_agent_config)

        agent = cls(agent_info, provider, session_id, workdir)
        if initial_plan_mode:
            agent._plan_mode = True
            agent._plan_mode_source = resolved_name or "build"
        return agent
