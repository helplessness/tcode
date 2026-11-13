"""Agent 运行主循环。

这层只负责一轮对话如何推进，不关心具体工具细节。
"""

from __future__ import annotations

import json
import hashlib
import os
import asyncio
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

if TYPE_CHECKING:
    from ..task.outcome_contract import OutcomeContract

from ..learning import LearningManager
from ..presentation import collect_presented_image_blocks, normalize_content_blocks
from ..session.usage import normalize_usage
from ..tools.capabilities import get_tool_capability
from ..runtime.computer import computer_operation, is_computer_tool
from ..types import Message, ToolCall, ToolResult, ToolState, Usage
from .turn_driver import TurnDriver
from ..runtime.asset_history import record_asset_turn

TURN_STATE_METADATA_KEY = "active_turn"
LAST_TURN_METADATA_KEY = "last_turn"
TERMINAL_TURN_STATES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "interrupted",
    "cancelled",
}


def _observe_read_progress(tool_name, result, seen, repeats):
    """Bound repeated successful observations, never cache or invent a result."""
    metadata = dict(result.metadata or {})
    capability = dict(metadata.get("policy") or {}).get("capability") or {}
    if not (capability.get("readonly") or metadata.get("semantic_readonly")) or result.error:
        seen.clear()  # A mutation/failure may change what a subsequent read observes.
        return 0
    semantic = str(metadata.get("result_fingerprint") or "")
    signature = (tool_name, semantic or hashlib.sha256(str(result.output or "").encode()).hexdigest())
    repeated = signature in seen
    seen.add(signature)
    return repeats + 1 if repeated else 0


def _observe_computer_action_progress(result, state):
    """Track verified GUI state changes separately from repeated-read liveness.

    A successful dispatch is not evidence that the application goal changed.
    Only a later, fresh ``observe`` with the Host's stable semantic fingerprint
    can confirm either a change or a bounded no-effect action. Missing semantic
    evidence remains unknown and neither advances nor resets the guard.
    """
    metadata = dict(result.metadata or {})
    audit = dict(metadata.get("computer_audit") or {})
    operation = str(audit.get("operation") or "")
    status = str(audit.get("status") or "")
    public = dict(metadata.get("computer") or {})
    target_ref = str(public.get("target_ref") or audit.get("target_ref") or "")
    fingerprints = state.setdefault("fingerprints", {})

    def same_region(previous, current):
        if previous is None or current is None:
            return previous is current
        return len(previous) == len(current) == 2 and all(abs(a - b) <= 16 for a, b in zip(previous, current))

    def unverified_repeat(pending):
        signature = str(pending.get("signature") or "")
        region = pending.get("region")
        key = (str(pending.get("target_ref") or ""), signature)
        if not signature:
            return int(state.get("unverified", 0)), "unknown"
        if state.get("last_unverified_key") == key and same_region(state.get("last_unverified_region"), region):
            state["unverified"] = int(state.get("unverified", 0)) + 1
        else:
            state["last_unverified_key"] = key
            state["unverified"] = 1
        state["last_unverified_region"] = region
        return state["unverified"], "unverified"

    if operation == "observe" and status == "completed_known" and not result.error:
        fingerprint = str(metadata.get("result_fingerprint") or "")
        evidence = dict(metadata.get("computer_progress_evidence") or {})
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.get("target_ref") == target_ref:
            state.pop("pending", None)
        if (not fingerprint or not target_ref
                or evidence.get("kind") != "ax_target_semantics"
                or evidence.get("semantic_coverage") != "complete"):
            if isinstance(pending, dict) and pending.get("target_ref") == target_ref:
                state["suppress_passive_repeat_once"] = True
                return unverified_repeat(pending)
            return int(state.get("unchanged", 0)), "unknown"
        previous = str(fingerprints.get(target_ref) or "")
        fingerprints[target_ref] = fingerprint
        if not isinstance(pending, dict) or pending.get("target_ref") != target_ref:
            if state.pop("suppress_passive_repeat_once", False):
                state["last_passive_observe"] = (target_ref, fingerprint)
                state["passive_observe_repeats"] = 0
                return 0, "observed"
            passive_key = (target_ref, fingerprint)
            if previous == fingerprint and state.get("last_passive_observe") == passive_key:
                state["passive_observe_repeats"] = int(state.get("passive_observe_repeats", 0)) + 1
            elif previous == fingerprint:
                state["last_passive_observe"] = passive_key
                state["passive_observe_repeats"] = 1
            else:
                state["last_passive_observe"] = passive_key
                state["passive_observe_repeats"] = 0
            if previous == fingerprint:
                return int(state.get("passive_observe_repeats", 0)), "repeated_observe"
            return 0, "observed"
        state["passive_observe_repeats"] = 0
        state.pop("last_passive_observe", None)
        if not pending.get("verifiable"):
            return unverified_repeat(pending)
        baseline = str(pending.get("fingerprint") or previous)
        if not baseline:
            return int(state.get("unchanged", 0)), "unknown"
        if fingerprint == baseline:
            state["unverified"] = 0
            unchanged_key = (
                target_ref,
                str(pending.get("signature") or ""),
            )
            region = pending.get("region")
            if state.get("last_unchanged_key") == unchanged_key and same_region(state.get("last_unchanged_region"), region):
                state["unchanged"] = int(state.get("unchanged", 0)) + 1
            else:
                state["last_unchanged_key"] = unchanged_key
                state["unchanged"] = 1
            state["last_unchanged_region"] = region
            return state["unchanged"], "unchanged"
        state["unchanged"] = 0
        state["unverified"] = 0
        state.pop("last_unchanged_key", None)
        return 0, "changed"

    if operation in {
        "click_element", "right_click", "open_menu", "select_option",
        "set_value", "click_at", "double_click", "scroll", "type_text",
        "press_key", "activate_app",
    } and status == "completed_known" and not result.error and target_ref:
        state["pending"] = {
            "target_ref": target_ref,
            "fingerprint": str(fingerprints.get(target_ref) or ""),
            "signature": str(metadata.get("computer_action_signature") or ""),
            "region": metadata.get("computer_action_region"),
            "verifiable": bool(metadata.get("computer_progress_binding")),
        }
        return int(state.get("unchanged", 0)), (
            "action_pending" if metadata.get("computer_progress_binding") else "verification_pending"
        )

    return int(state.get("unchanged", 0)), "ignored"


def _model_output_budget_partial(response: Any) -> bool:
    return str(getattr(response, "finish_reason", "") or "").strip().lower() == "partial_output_budget"


def _workflow_stage_uses_its_own_validation(agent: Any) -> bool:
    """Return whether the active turn is owned by a Workflow stage.

    Workflow stages are accepted or rejected after the worker returns by the
    template's deterministic validators (schema, required artifacts, files,
    review decision, and any plugin validators).  Running the generic Outcome
    semantic judge before those validators duplicates the acceptance boundary
    and can hide the actionable stage error behind a generic PARTIAL result.
    """

    trace_context = getattr(agent, "_active_tool_trace_context", None)
    if not isinstance(trace_context, dict):
        return False
    return bool(
        str(trace_context.get("workflow_run_id") or "").strip()
        and str(trace_context.get("workflow_stage_id") or "").strip()
        and str(trace_context.get("workflow_template_key") or "").strip()
    )


def _workflow_stage_min_iterations(agent: Any) -> int:
    """Give Workflow workers enough turns to finish their declared commit.

    Agent JSON files intentionally use small ``steps`` values for ordinary
    subagent conversations.  A Workflow stage is different: it must often
    read several bounded evidence batches, recover a validation error, and
    then call one authoritative commit tool.  Letting the ordinary step cap
    terminate that sequence turns a recoverable run into a misleading PARTIAL
    outcome.  Keep the agent's configured value as the lower bound for
    ordinary turns, while applying a bounded Workflow-only floor that can be
    tuned by operators.
    """
    if not _workflow_stage_uses_its_own_validation(agent):
        return 0
    raw = str(os.environ.get("TCODE_WORKFLOW_STAGE_MIN_STEPS", "24") or "24").strip()
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        configured = 24
    floor = max(8, min(configured, 100))
    current = getattr(getattr(agent, "agent_info", None), "steps", None)
    try:
        current_steps = int(current) if current is not None else 0
    except (TypeError, ValueError):
        current_steps = 0
    # ``steps=None`` already means the engine default (100); preserve that
    # behavior rather than reducing it to the Workflow floor.
    return max(floor, current_steps) if current_steps > 0 else 100


def _completed_workflow_report(tool_call: Any, result: ToolResult, *, pending_tools: bool) -> str:
    """Only the core workflow tool may directly deliver a committed report."""
    metadata = dict(result.metadata or {})
    if (pending_tools or str(getattr(tool_call, "tool_name", "")) != "workflow_run"
            or result.error or metadata.get("status") != "completed"
            or metadata.get("blocked_on") or metadata.get("async_stage_refs")):
        return ""
    return str(metadata.get("workflow_direct_response") or "").strip()


class AgentHarnessEngine:
    """承载唯一 Turn 状态机，并由 TurnDriver 投影同步与流式入口。"""

    def __init__(self, agent: Any):
        """初始化单个 Agent 的 turn loop。

        参数:
            agent: 提供会话、工作目录、模型与工具执行能力的 Agent。
        """

        self.agent = agent

    @staticmethod
    def _attach_presented_content_blocks(
        final_message: Message,
        turn_messages: list[Message],
    ) -> None:
        final_message.content_blocks = normalize_content_blocks(
            [
                *list(getattr(final_message, "content_blocks", []) or []),
                *collect_presented_image_blocks(turn_messages),
            ]
        )

    def _recommended_skill_preload_timeout_seconds(self) -> float:
        try:
            raw = float(
                os.environ.get("TCODE_RECOMMENDED_SKILL_PRELOAD_TIMEOUT_SECONDS", "4")
            )
        except Exception:
            raw = 4.0
        return max(0.5, min(raw, 30.0))

    def _tool_definitions_timeout_seconds(self) -> float:
        try:
            raw = float(
                os.environ.get("TCODE_TOOL_DEFINITIONS_TIMEOUT_SECONDS", "12")
            )
        except Exception:
            raw = 12.0
        return max(1.0, min(raw, 300.0))

    def _session_persistence_timeout_seconds(self) -> float:
        try:
            raw = float(
                os.environ.get("TCODE_SESSION_PERSIST_TIMEOUT_SECONDS", "15")
            )
        except Exception:
            raw = 15.0
        if raw <= 0:
            return 0.0
        return max(0.5, min(raw, 120.0))

    def _tool_heartbeat_interval_seconds(self) -> float:
        try:
            raw = float(os.environ.get("TCODE_TOOL_HEARTBEAT_SECONDS", "12"))
        except Exception:
            raw = 12.0
        if raw <= 0:
            return 0.0
        return max(3.0, min(raw, 120.0))

    @staticmethod
    def _guidance_batch_window_seconds() -> float:
        try:
            milliseconds = float(
                os.environ.get("TCODE_GUIDANCE_BATCH_WINDOW_MS", "100") or 100
            )
        except (TypeError, ValueError):
            milliseconds = 100.0
        return max(0.0, min(0.5, milliseconds / 1000.0))

    @staticmethod
    def _guidance_model_cancel_timeout_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "TCODE_GUIDANCE_MODEL_CANCEL_TIMEOUT_SECONDS", "2"
                )
                or 2
            )
        except (TypeError, ValueError):
            seconds = 2.0
        return max(0.5, min(10.0, seconds))

    def _format_elapsed(self, seconds: float) -> str:
        total = max(0, int(seconds or 0))
        if total < 60:
            return f"{total} 秒"
        minutes, remainder = divmod(total, 60)
        if minutes < 60:
            return f"{minutes} 分 {remainder} 秒" if remainder else f"{minutes} 分钟"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"

    def _llm_call_timeout_seconds(self, *, iteration: int) -> float:
        provider_timeout = getattr(self.agent.provider, "_timeout_seconds", None)
        raw = provider_timeout
        if raw in (None, ""):
            raw = os.environ.get(
                "TCODE_HARNESS_LLM_TIMEOUT_SECONDS",
                os.environ.get("TCODE_LLM_TIMEOUT", "300"),
            )
        try:
            parsed = float(raw)
        except Exception:
            parsed = 300.0
        if iteration > 1:
            try:
                final_response_timeout = float(
                    os.environ.get(
                        "TCODE_FINAL_RESPONSE_LLM_TIMEOUT_SECONDS",
                        "180",
                    )
                )
            except Exception:
                final_response_timeout = 180.0
            if final_response_timeout > 0:
                parsed = min(parsed, final_response_timeout)
        if parsed <= 0:
            return 0.0
        return max(5.0, min(parsed, 3600.0))

    async def _emit_llm_progress(self, status: str, *, iteration: int) -> None:
        emitter = getattr(self.agent, "_agent_event_emitter", None)
        if not callable(emitter):
            return
        normalized_status = str(status or "").strip().lower()
        final_response = iteration > 1
        preparation_messages = {
            'preparing': '正在分析你的请求。',
            'preparing_tools': '正在检查可用工具和权限。',
            'loading_skills': '正在加载功能目录。',
            'matching_skills': '正在选择本次任务需要的功能。',
        }
        if normalized_status in preparation_messages:
            progress_message = preparation_messages[normalized_status]
        elif normalized_status == "completed":
            progress_message = (
                "最终回答已经生成，我继续做最后整理。"
                if final_response
                else "响应已经回来，我来判断是直接回答还是继续取证。"
            )
        elif normalized_status == "timeout":
            progress_message = (
                "最终整理超时了，我会尽量用已经完成的工具结果收尾。"
                if final_response
                else "模型响应超时了，我会尝试用已有上下文恢复。"
            )
        elif normalized_status == "error":
            progress_message = (
                "最终整理失败了，我会保留上下文并尝试恢复。"
                if final_response
                else "模型响应失败了，我会保留现场继续处理。"
            )
        else:
            progress_message = (
                "工具结果已经回来，我在把它们收拢成最终回答。"
                if final_response
                else "我在判断下一步怎么走。"
            )
        try:
            emitted = emitter(
                {
                    "kind": "llm_progress",
                    "status": normalized_status,
                    "phase": normalized_status if normalized_status in preparation_messages else "final_response" if final_response else "initial_response",
                    "session_id": str(getattr(self.agent, "session_id", "") or ""),
                    "agent_name": str(
                        getattr(getattr(self.agent, "agent_info", None), "name", "")
                        or ""
                    ),
                    "iteration": iteration,
                    "message": progress_message,
                }
            )
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:
            pass
    async def _emit_model_output_event(
        self,
        *,
        status: str,
        iteration: int,
        message_id: str,
        delta: str = "",
        content: str = "",
        presentation: str = "model_output",
    ) -> None:
        """Project public model text without exposing private reasoning."""
        emitter = getattr(self.agent, "_agent_event_emitter", None)
        if not callable(emitter):
            return
        payload = {
            "kind": "model_output",
            "status": str(status or "streaming").strip().lower(),
            "session_id": str(getattr(self.agent, "session_id", "") or ""),
            "agent_name": str(
                getattr(getattr(self.agent, "agent_info", None), "name", "") or ""
            ),
            "iteration": iteration,
            "message_id": str(message_id or ""),
            "stream_epoch": int(
                getattr(self.agent, "_guidance_stream_epoch", 0) or 0
            ),
            "presentation": str(presentation or "model_output"),
            "visibility": "public_summary",
        }
        if delta:
            payload["delta"] = str(delta)
        if content:
            payload["content"] = str(content)
        try:
            emitted = emitter(payload)
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:
            pass

    def _tool_call_names_preview(self, tool_calls: list[Any], *, limit: int = 4) -> str:
        names: list[str] = []
        for tool_call in tool_calls[: max(0, limit)]:
            name = str(getattr(tool_call, "tool_name", "") or "").strip()
            if name:
                names.append(name)
        if len(tool_calls) > limit:
            names.append(f"等 {len(tool_calls)} 个")
        return "、".join(names)

    @staticmethod
    def _short_public_text(value: Any, *, limit: int = 72) -> str:
        text = str(value or "").strip().replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    @classmethod
    def _first_public_arg(
        cls,
        arguments: dict[str, Any],
        *keys: str,
        limit: int = 72,
    ) -> str:
        for key in keys:
            value = arguments.get(key)
            if value:
                return cls._short_public_text(value, limit=limit)
        return ""

    def _request_focus_label(self, user_message: str) -> str:
        text = str(user_message or "").lower()
        if any(marker in text for marker in ("论文", "文献", "pdf", "pmid", "doi")):
            return "文献证据"
        if any(marker in text for marker in ("显卡", "gpu", "cuda", "驱动", "环境")):
            return "环境排查"
        if any(marker in text for marker in ("测试", "test", "pytest", "vitest")):
            return "验证"
        if any(marker in text for marker in ("代码", "修改", "改造", "修复", "实现")):
            return "代码改造"
        return "目标"

    def _turn_start_process_message(self, user_message: str = "") -> str:
        focus = self._request_focus_label(user_message)
        if focus == "目标":
            return "我先把这次请求、当前工作区和会话上下文对齐一下。"
        return f"我先把这次{focus}目标、当前工作区和会话上下文对齐一下。"

    def _preparing_process_message(self) -> str:
        return "我在扫一眼可用技能、运行状态和已有上下文。"

    def _context_check_process_message(self, *, iteration: int) -> str:
        if iteration <= 1:
            return "先看看上下文有没有需要压缩、补齐或避开的地方。"
        return "上一轮结果已经进来，我重新校准一下上下文再继续。"

    def _tool_preparation_process_message(self) -> str:
        return "我在整理可用工具，保证下一步能选对入口。"

    def _model_wait_process_message(self, *, tool_count: int) -> str:
        if tool_count > 0:
            return (
                f"可用工具已经整理好（{tool_count} 个），"
                "我来判断下一步该直接回答还是先取证。"
            )
        return "当前没有可用工具，我先让模型基于已有上下文判断。"

    def _tool_selection_process_message(self, tool_calls: list[Any]) -> str:
        count = len(tool_calls)
        preview = self._tool_call_names_preview(tool_calls)
        if count <= 0:
            return "我看下来不需要额外工具，可以直接整理回答。"
        if count == 1:
            return f"我看下来还需要先取一块关键信息：{preview or '一个工具'}。"
        return f"我看下来还需要先补几块信息：{preview or f'{count} 个工具'}。"

    def _final_response_process_message(self) -> str:
        return "现有信息已经够用了，我开始把结果收拢成回复。"

    def _tool_public_action(self, tool_call: ToolCall) -> str:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        normalized = tool_name.lower()
        arguments = getattr(tool_call, "arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}

        exact_actions = {
            "paper_library_lookup_pdf": "我先检查本地文献库里是否已有这篇论文。",
            "paper_library_ingest_pdf": (
                "我把 PDF 写入本地文献库，方便后续按证据层读取。"
            ),
            "paper_library_get_record": "我读取已入库论文的结构化记录和证据层。",
            "paper_literature_search": "我用论文标识符检索候选文献和可用 PDF。",
            "paper_prepare_workspace": "我先准备这次文献任务的工作区。",
            "paper_collect_helper_results": "我收集团队成员已经完成的阶段结果。",
            "paper_save_cards": "我把可复用的文献证据保存成卡片。",
            "paper_write_artifact": "我把整理出的中间结果写成可复用文件。",
            "import_resource": "我尝试把用户给的本地资源导入当前工作区。",
            "read": "我读取相关文件内容，先确认已有上下文。",
            "write": "我把结果写入文件，方便后续复用和检查。",
            "edit": "我修改相关文件，尽量让改动贴着这次需求。",
            "grep": "我在项目里搜索相关线索。",
            "glob": "我先定位可能相关的文件。",
            "bash": "我运行本地命令获取确定性结果。",
            "exec": "我运行本地命令获取确定性结果。",
            "websearch": "我通过搜索补充公开资料线索。",
            "web_search": "我通过搜索补充公开资料线索。",
            "webfetch": "我打开网页读取指定来源内容。",
            "web_fetch": "我打开网页读取指定来源内容。",
            "browser": "我用隔离浏览器检查页面或下载入口。",
            "computer": "我读取当前应用状态，再执行获准的界面操作。",
            "todo_write": "我更新任务清单，保持后续步骤可追踪。",
            "todowrite": "我更新任务清单，保持后续步骤可追踪。",
            "todo_read": "我读取当前任务清单，确认进度状态。",
            "todoread": "我读取当前任务清单，确认进度状态。",
            "list_workers": "我查看当前团队里有哪些可用成员。",
            "assign_task": "我把这一步交给合适的团队成员同步处理。",
            "task": "我把这部分拆给子代理处理，再把结果合并回来。",
        }
        if is_computer_tool(normalized):
            operation = computer_operation(normalized, arguments).lower()
            return {
                "list_apps": "我先确认目标应用是否可用。",
                "get_window_state": "我读取目标窗口的当前状态，确认下一步操作位置。",
                "observe": "我读取目标窗口的当前状态，确认下一步操作位置。",
                "activate_app": "我把目标应用切到前台，准备继续操作。",
                "click": "我执行一次获准的界面点击，再核对页面变化。",
                "set_value": "我填写获准的界面字段，再核对输入结果。",
                "select": "我选择获准的界面选项，再核对页面状态。",
                "scroll": "我滚动目标界面，继续定位需要操作的内容。",
                "open_context_menu": "我打开目标项的上下文菜单，确认可用操作。",
            }.get(operation, "我读取当前应用状态，再执行获准的界面操作。")
        if normalized in exact_actions:
            return exact_actions[normalized]

        if normalized.startswith("paper_library_"):
            return "我调用本地文献库能力，补齐论文记录或证据定位。"
        if normalized.startswith("paper_"):
            return "我调用文献工作流工具，推进检索、入库或证据整理。"
        if normalized.startswith("mcp_"):
            return "我调用外部 MCP 能力，获取当前步骤需要的结构化结果。"
        return f"我调用 {tool_name or '工具'} 获取下一步需要的信息。"

    def _tool_start_process_message(
        self,
        tool_call: ToolCall,
        *,
        last_completed_tool_name: str = "",
    ) -> str:
        action = self._tool_public_action(tool_call)
        previous = str(last_completed_tool_name or "").strip()
        if previous:
            return f"拿到 {previous} 的结果后，{action}"
        return action

    def _tool_progress_process_message(
        self,
        tool_call: ToolCall,
        *,
        elapsed_seconds: float,
    ) -> str:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        normalized = tool_name.lower()
        arguments = getattr(tool_call, "arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        elapsed = self._format_elapsed(elapsed_seconds)

        if normalized == "assign_task":
            worker = str(arguments.get("worker_name") or "worker").strip()
            task = str(arguments.get("task") or "").strip()
            subject = f"“{task[:48]}”" if task else "子任务"
            return (
                f"{worker or 'worker'} 还在处理 {subject}，"
                f"已等待 {elapsed}；我继续同步进度。"
            )
        if normalized == "task":
            task = str(
                arguments.get("description") or arguments.get("prompt") or ""
            ).strip()
            subject = f"“{task[:48]}”" if task else "子任务"
            return (
                f"子代理还在处理 {subject}，已等待 {elapsed}；"
                "我继续等它把结果带回来。"
            )
        if normalized == "paper_library_ingest_pdf":
            force_reindex = bool(arguments.get("force_reindex"))
            if force_reindex:
                return (
                    "文献库还在重建这篇 PDF 的文本、表格和图片索引，"
                    f"已运行 {elapsed}。"
                )
            return (
                f"文献库还在入库这篇 PDF，已运行 {elapsed}；"
                "我继续等证据索引完成。"
            )
        if normalized in {"bash", "exec", "skill_run", "browser"}:
            return f"{tool_name} 还在跑，已等待 {elapsed}；我继续等结果。"
        return (
            f"{tool_name or '工具'} 还在执行，已等待 {elapsed}；"
            "我继续盯着进度。"
        )

    async def _execute_tool_with_heartbeat(
        self,
        tool_call: ToolCall,
        *,
        iteration: int,
    ) -> ToolResult:
        from ..runtime.guidance import GuidanceSafePoint

        interval = self._tool_heartbeat_interval_seconds()
        started_at = asyncio.get_running_loop().time()
        setattr(tool_call, "_last_progress_monotonic", started_at)
        heartbeat_task: Optional[asyncio.Task[Any]] = None

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                now = asyncio.get_running_loop().time()
                last_progress = float(
                    getattr(tool_call, "_last_progress_monotonic", started_at)
                    or started_at
                )
                if now - last_progress < interval:
                    continue
                elapsed = now - started_at
                port = getattr(self.agent, "_guidance_port", None)
                owner_id = str(
                    getattr(self.agent, "_guidance_owner_id", "") or ""
                )
                active_turn_id = str(
                    getattr(self, "_active_guidance_turn_id", "") or ""
                )
                if port is not None and owner_id and active_turn_id:
                    try:
                        await port.heartbeat_turn_owner(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=active_turn_id,
                            owner_id=owner_id,
                            phase="executing_tool",
                            stream_epoch=int(
                                getattr(self.agent, "_guidance_stream_epoch", 0) or 0
                            ),
                        )
                    except Exception:
                        pass

        if interval > 0:
            heartbeat_task = asyncio.create_task(heartbeat_loop())
        steer_task: Optional[asyncio.Task[bool]] = None
        port = getattr(self.agent, "_guidance_port", None)
        owner_id = str(getattr(self.agent, "_guidance_owner_id", "") or "")
        turn_id = str(getattr(self, "_active_guidance_turn_id", "") or "")
        interrupt_enabled = str(
            os.environ.get("TCODE_STEER_TOOL_INTERRUPT_ENABLED", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        tool_name = str(tool_call.tool_name or "").strip()
        behavior = str(
            get_tool_capability(tool_name).interrupt_behavior
            or "best_effort"
        )
        detach_capable = behavior == "async_job" or tool_name in {
            "task",
            "assign_task",
        }
        execution_call = tool_call
        if detach_capable and interrupt_enabled:
            # A detached operation must be allowed to finish its own durable
            # submission/child lifecycle without mutating the synthetic result
            # that closes the parent's provider tool-call pair.
            execution_call = ToolCall(
                tool_name=tool_call.tool_name,
                call_id=tool_call.call_id,
                arguments=dict(tool_call.arguments or {}),
                state=tool_call.state,
                output=tool_call.output,
                error=tool_call.error,
                metadata=dict(tool_call.metadata or {}),
                start_time=tool_call.start_time,
                end_time=tool_call.end_time,
                compacted_at=tool_call.compacted_at,
            )
        execution_task = asyncio.create_task(self.agent._execute_tool(execution_call))
        detached = False

        def copy_execution_state() -> None:
            if execution_call is tool_call:
                return
            for field in (
                "state",
                "output",
                "error",
                "metadata",
                "start_time",
                "end_time",
                "compacted_at",
            ):
                value = getattr(execution_call, field)
                if field == "metadata":
                    value = dict(value or {})
                setattr(tool_call, field, value)

        if (
            interrupt_enabled
            and (behavior == "cancel_process" or detach_capable)
            and port is not None
            and owner_id
            and turn_id
        ):
            steer_task = asyncio.create_task(
                port.wait_for_steer(
                    session_id=str(getattr(self.agent, "session_id", "") or ""),
                    turn_id=turn_id,
                    timeout_seconds=24 * 3600,
                )
            )
        try:
            while steer_task is not None:
                done, _ = await asyncio.wait(
                    {execution_task, steer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution_task in done:
                    result = execution_task.result()
                    copy_execution_state()
                    return result
                steer_ready = False
                try:
                    steer_ready = bool(steer_task.result())
                except Exception:
                    pass
                if not steer_ready:
                    steer_task = asyncio.create_task(
                        port.wait_for_steer(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=turn_id,
                            timeout_seconds=24 * 3600,
                        )
                    )
                    continue
                claimed = await self._claim_pending_guidance(
                    turn_id=turn_id,
                    safe_point=GuidanceSafePoint.AFTER_TOOL_RESULT,
                )
                if not claimed:
                    steer_task = asyncio.create_task(
                        port.wait_for_steer(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=turn_id,
                            timeout_seconds=24 * 3600,
                        )
                    )
                    continue
                if detach_capable:
                    reference = await self._detached_tool_reference(execution_call)
                    guidance_ids = [item.id for item in claimed]
                    setattr(tool_call, "_guidance_claimed_items", claimed)
                    metadata = dict(getattr(tool_call, "metadata", {}) or {})
                    metadata.update(
                        {
                            "synthetic": True,
                            "detached": True,
                            "detached_async_job": True,
                            "interrupt_requested": False,
                            "side_effect_state": "external_work_preserved",
                            "guidance_ids": guidance_ids,
                            **reference,
                        }
                    )
                    tool_call.state = ToolState.COMPLETED
                    tool_call.error = None
                    tool_call.output = (
                        "The operation was detached from the parent wait and continues "
                        "in the background."
                    )
                    tool_call.end_time = self.agent._current_time()
                    tool_call.metadata = metadata
                    detached = True
                    background_tasks = getattr(
                        self, "_detached_guidance_tool_tasks", None
                    )
                    if background_tasks is None:
                        background_tasks = set()
                        self._detached_guidance_tool_tasks = background_tasks
                    background_tasks.add(execution_task)

                    def consume_background_result(task: asyncio.Task[Any]) -> None:
                        background_tasks.discard(task)
                        with suppress(asyncio.CancelledError, Exception):
                            task.result()

                    execution_task.add_done_callback(consume_background_result)
                    from ..runtime.telemetry import TelemetryGateway

                    TelemetryGateway.emit(
                        "guidance.tool_interrupt",
                        {
                            "tool": tool_name,
                            "behavior": (
                                "orchestration_detach"
                                if tool_name in {"task", "assign_task"}
                                else behavior
                            ),
                            "result": "detached_async_job",
                        },
                        session_id=str(
                            getattr(self.agent, "session_id", "") or ""
                        ),
                    )
                    return ToolResult(
                        title=f"{tool_call.tool_name} detached",
                        output=tool_call.output,
                        metadata=metadata,
                    )
                interrupt = await self.agent._tool_executor.interrupt_tool_call(
                    tool_call,
                    reason="Superseded by newer user guidance.",
                )
                from ..runtime.telemetry import TelemetryGateway

                TelemetryGateway.emit(
                    "guidance.tool_interrupt",
                    {
                        "tool": tool_name,
                        "behavior": behavior,
                        "result": interrupt.outcome,
                        "confirmed_stopped": interrupt.confirmed_stopped,
                        "side_effect_state": interrupt.side_effect_state,
                    },
                    session_id=str(getattr(self.agent, "session_id", "") or ""),
                )
                if interrupt.outcome.startswith("cancelled_"):
                    with suppress(asyncio.CancelledError):
                        await execution_task
                    guidance_ids = [item.id for item in claimed]
                    setattr(tool_call, "_guidance_claimed_items", claimed)
                    tool_call.state = ToolState.INTERRUPTED
                    tool_call.error = None
                    tool_call.output = ""
                    tool_call.end_time = self.agent._current_time()
                    metadata = dict(getattr(tool_call, "metadata", {}) or {})
                    metadata.update(
                        {
                            "synthetic": True,
                            "interrupted": True,
                            "interrupt_requested": True,
                            "interrupt_confirmed": interrupt.confirmed_stopped,
                            "side_effect_state": interrupt.side_effect_state,
                            "guidance_ids": guidance_ids,
                            "superseded_by_guidance": True,
                            "semantic_disposition": "superseded_neutral",
                        }
                    )
                    tool_call.metadata = metadata
                    self._close_interrupted_tool_runtime(
                        tool_call,
                        reason="Superseded by newer user guidance.",
                    )
                    return ToolResult(
                        title=f"{tool_call.tool_name} interrupted",
                        output=(
                            "Execution was interrupted by newer user guidance. "
                            f"Termination confirmed: {interrupt.confirmed_stopped}. "
                            f"Side effects: {interrupt.side_effect_state}. "
                            "Inspect the current state before rerunning."
                        ),
                        metadata=metadata,
                    )

                # The body either completed first or could not be safely
                # interrupted. Put the claimed batch back behind the current
                # atomic tool action and consume it at after_tool_result.
                for item in claimed:
                    try:
                        await port.mark_deferred(
                            item=item,
                            reason="waiting_current_tool",
                        )
                    except Exception:
                        pass
                setattr(tool_call, "_release_deferred_guidance", True)
                steer_task = None
            result = await execution_task
            copy_execution_state()
            return result
        finally:
            if steer_task is not None and not steer_task.done():
                steer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await steer_task
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            if not detached and not execution_task.done():
                execution_task.cancel()
                with suppress(asyncio.CancelledError):
                    await execution_task

    def _collect_public_result_paths(self, value: Any, *, depth: int = 0) -> list[str]:
        if depth > 4:
            return []
        paths: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if len(paths) >= 4:
                    break
                key_text = str(key or "").lower()
                if (
                    isinstance(item, str)
                    and item.strip()
                    and any(
                        marker in key_text
                        for marker in ("path", "file", "artifact")
                    )
                ):
                    paths.append(self._short_public_text(item, limit=96))
                elif isinstance(item, (dict, list)):
                    paths.extend(
                        self._collect_public_result_paths(item, depth=depth + 1)
                    )
        elif isinstance(value, list):
            for item in value[:8]:
                if len(paths) >= 4:
                    break
                paths.extend(self._collect_public_result_paths(item, depth=depth + 1))
        return [
            path
            for index, path in enumerate(paths)
            if path and path not in paths[:index]
        ]

    def _result_public_artifact(self, result: ToolResult, output_text: str) -> str:
        direct = self._short_public_text(
            getattr(result, "output_path", "") or "",
            limit=96,
        )
        if direct:
            return direct
        paths = self._collect_public_result_paths(getattr(result, "metadata", {}) or {})
        if paths:
            return paths[0]
        text = str(output_text or "")
        if text.strip().startswith(("{", "[")):
            with suppress(Exception):
                parsed = json.loads(text)
                paths = self._collect_public_result_paths(parsed)
                if paths:
                    return paths[0]
        match = re.search(
            (
                r"(/[^ \n\r\t'\"<>]+(?:\.json|\.csv|\.md|\.txt|\.pdf|"
                r"\.png|\.jpg|\.xlsx|\.py|\.ts|\.tsx))"
            ),
            text,
        )
        return self._short_public_text(match.group(1), limit=96) if match else ""

    def _result_public_count(self, result: ToolResult, output_text: str) -> str:
        candidates: list[tuple[str, Any]] = []

        def collect(value: Any, *, depth: int = 0) -> None:
            if depth > 3 or len(candidates) >= 4:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    key_text = str(key or "").lower()
                    if isinstance(item, (int, float)) and any(
                        marker in key_text
                        for marker in ("count", "total", "matched", "downloaded")
                    ):
                        candidates.append((key_text, item))
                    elif isinstance(item, (dict, list)):
                        collect(item, depth=depth + 1)
            elif isinstance(value, list):
                for item in value[:8]:
                    collect(item, depth=depth + 1)

        collect(getattr(result, "metadata", {}) or {})
        text = str(output_text or "")
        if text.strip().startswith(("{", "[")):
            with suppress(Exception):
                collect(json.loads(text))
        if not candidates:
            return ""
        key, value = candidates[0]
        label = "候选" if "candidate" in key else "结果"
        return f"{label} {int(value)} 个"

    def _tool_result_process_message(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        *,
        failed: bool,
    ) -> str:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        normalized = tool_name.lower()
        error_text = str(getattr(result, "error", "") or "").strip()
        output_text = str(getattr(result, "output", "") or "").strip()
        combined = f"{error_text}\n{output_text}"[:5000].lower()
        if failed:
            if any(
                marker in combined
                for marker in (
                    "无权直接使用",
                    "permission",
                    "allowed_agents",
                    "not authorized",
                )
            ):
                return "这个工具需要换到有权限的 agent 执行，我会按权限提示调整路径。"
            if any(
                marker in combined
                for marker in (
                    "no such file",
                    "no such directory",
                    "没有那个文件",
                    "找不到",
                    "errno 2",
                    "errn 2",
                )
            ):
                return "当前环境访问不到这个路径，我会改用工作区路径或论文标识符继续。"
            if any(
                marker in combined
                for marker in (
                    "invalid api key",
                    "incorrect api key",
                    "401",
                    "鉴权",
                    "认证",
                )
            ):
                return "模型服务返回鉴权错误，当前步骤被环境配置挡住了。"
            if "timeout" in combined or "timed out" in combined or "超时" in combined:
                return "这个工具超时了，我会保留失败原因并换更稳的路径继续。"
            failed_tool_name = (
                tool_name
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", tool_name)
                else "工具"
            )
            return f"{failed_tool_name} 返回错误，我会保留失败状态并调整下一步。"

        public_tool_name = (
            tool_name
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", tool_name)
            else "工具"
        )
        if is_computer_tool(normalized):
            return "这次界面操作已返回状态；我会先核对实际变化，再决定下一步。"
        if normalized in {"read", "grep", "glob"}:
            return "文件读取步骤已返回；我会根据可核对的信息继续判断。"
        if normalized in {"write", "edit"}:
            return "文件操作已返回；我会继续检查结果是否符合要求。"
        if normalized in {"bash", "exec"}:
            return "本地命令已返回；我会根据退出状态和输出继续判断。"
        if normalized in {"task", "assign_task"}:
            return "执行者返回了阶段状态；我会先核对再合并到当前任务。"
        return f"{public_tool_name} 已返回；我会先核对结果，再决定下一步。"

    async def _emit_outcome_progress(
        self,
        status: str,
        *,
        validation: Optional[dict[str, Any]] = None,
        repair_attempt: int = 0,
    ) -> None:
        expose = str(
            os.environ.get("TCODE_EXPOSE_OUTCOME_VALIDATION", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not expose:
            return
        emitter = getattr(self.agent, "_agent_event_emitter", None)
        if not callable(emitter):
            return
        try:
            emitted = emitter(
                {
                    "kind": "outcome_validation",
                    "status": str(status or "").strip().lower(),
                    "phase": "validating" if status == "running" else status,
                    "session_id": str(getattr(self.agent, "session_id", "") or ""),
                    "agent_name": str(
                        getattr(getattr(self.agent, "agent_info", None), "name", "")
                        or ""
                    ),
                    "repair_attempt": repair_attempt,
                    "validation": dict(validation or {}),
                }
            )
            if hasattr(emitted, "__await__"):
                await emitted
        except Exception:
            pass

    async def _validate_outcome_candidate(
        self,
        content: str,
        *,
        contract: Any,
        turn_start_index: int,
        repair_attempt: int,
    ) -> Any:
        from ..guard import (
            deterministic_verifier_enabled,
            outcome_guard_enabled,
            validate_outcome,
            validate_outcome_deterministic,
        )
        from ..guard.outcome_guard import OutcomeValidationResult
        from ..runtime.evidence_ledger import EvidenceLedger
        from ..runtime.proof_pack import proof_pack_enabled

        from ..server.auth import session_result_verification_settings
        settings = await session_result_verification_settings(await self._turn_session())
        if not settings["enabled"]:
            from ..guard.outcome_guard import disabled_outcome_validation
            validation = disabled_outcome_validation()
            self.agent._last_outcome_status = validation.verdict
            self.agent._last_outcome_validation = validation.to_dict()
            self.agent._last_outcome_contract = contract.to_dict()
            return validation

        plan = getattr(self.agent, "_active_outcome_plan", None)
        is_async_continuation = (
            str(getattr(plan, "reason_code", "") or "")
            == "async_result_continuation"
        )
        async_receipt = dict(
            getattr(self.agent, "_active_async_completion_receipt", {}) or {}
        )
        if is_async_continuation:
            criterion_id = str(
                getattr(
                    (list(getattr(contract, "success_criteria", []) or []) or [None])[0],
                    "criterion_id",
                    "async_completion_receipt",
                )
                or "async_completion_receipt"
            )
            if async_receipt.get("validated") is True:
                validation = OutcomeValidationResult(
                    verdict="pass",
                    validator="deterministic",
                    criterion_results=[
                        {
                            "criterion_id": criterion_id,
                            "status": "pass",
                            "reason_code": "trusted_async_completion_receipt",
                            "evidence_refs": [
                                str(async_receipt.get("event_id") or "")
                            ],
                            "artifact_refs": [
                                str(async_receipt.get("bundle_id") or "")
                            ] if str(async_receipt.get("bundle_id") or "") else [],
                            "observations": [
                                "host_recorded_completion",
                                "job_protocol_result_validated",
                            ],
                        }
                    ],
                    goal_status="SATISFIED",
                    proof_level="P1",
                    required_proof_level="P1",
                    achieved_proof_level="P1",
                    verification_status="VERIFIED",
                    confidence=1.0,
                    confidence_components={
                        "objective_checks": 1.0,
                        "evidence_coverage": 1.0,
                        "independent_validator": 1.0,
                        "agreement": 1.0,
                    },
                    validator_confidence=1.0,
                )
            else:
                validation = OutcomeValidationResult(
                    verdict="blocked",
                    failed_criteria=[criterion_id],
                    warnings=[
                        "The continuation prompt did not match an available host-owned async completion event."
                    ],
                    validator="deterministic",
                    criterion_results=[
                        {
                            "criterion_id": criterion_id,
                            "status": "fail",
                            "reason_code": "missing_trusted_async_completion_receipt",
                            "evidence_refs": [],
                            "artifact_refs": [],
                            "observations": [],
                        }
                    ],
                    goal_status="BLOCKED",
                    proof_level="P1",
                    required_proof_level="P1",
                    achieved_proof_level="P0",
                    verification_status="UNVERIFIED",
                    confidence=0.0,
                    validator_confidence=1.0,
                )
            from ..guard.outcome_guard import advisory_outcome_validation
            advisory_outcome_validation(validation, settings["mode"])
            payload = validation.to_dict()
            self.agent._last_outcome_status = validation.verdict
            self.agent._last_outcome_validation = payload
            self.agent._last_outcome_contract = contract.to_dict()
            return validation

        if _workflow_stage_uses_its_own_validation(self.agent):
            # The Workflow executor validates the response and committed
            # artifacts immediately after this turn.  Do not insert a second,
            # model-based completion judge between the worker and that
            # authoritative validation boundary.
            self.agent._last_outcome_status = ""
            self.agent._last_outcome_validation = {}
            return OutcomeValidationResult(verdict="pass", validator="disabled")

        guard_is_enabled = outcome_guard_enabled()
        pack_is_enabled = proof_pack_enabled()
        if not guard_is_enabled and not pack_is_enabled:
            return OutcomeValidationResult(verdict="pass", validator="disabled")

        ledger = EvidenceLedger.from_messages(
            self.agent.messages[turn_start_index:]
        )
        enforce_v2 = deterministic_verifier_enabled()
        if guard_is_enabled:
            await self._emit_outcome_progress(
                "running",
                repair_attempt=repair_attempt,
            )
            self._append_session_event(
                "outcome.goal.verification_requested",
                payload={
                    "contract_id": contract.contract_id,
                    "goal_id": contract.goal_id,
                    "repair_attempt": repair_attempt,
                },
            )
            validation = await validate_outcome(
                content,
                contract,
                ledger,
                provider=self.agent.provider,
                schema_validator=self.agent._validate_json_schema_text,
                workspace_root=str(getattr(self.agent, "workdir", "") or ""),
                enforce_v2=enforce_v2,
                verification_mode=settings["mode"],
                usage_session=await self._turn_session(),
            )
        else:
            shadow = validate_outcome_deterministic(
                content,
                contract,
                ledger=ledger,
                schema_validator=self.agent._validate_json_schema_text,
                workspace_root=str(getattr(self.agent, "workdir", "") or ""),
                enforce_v2=enforce_v2,
            )
            validation = OutcomeValidationResult(
                verdict="pass",
                failed_criteria=list(shadow.failed_criteria),
                unsupported_claims=list(shadow.unsupported_claims),
                missing_deliverables=list(shadow.missing_deliverables),
                warnings=[
                    "Outcome Guard is disabled; this Proof Pack is observational only."
                ],
                validator="disabled",
                criterion_results=list(shadow.criterion_results),
                goal_status="UNVERIFIED",
                proof_level=contract.required_proof_level,
                required_proof_level=contract.required_proof_level,
                achieved_proof_level=shadow.achieved_proof_level,
            )
        if guard_is_enabled and repair_attempt:
            unread = self._report_repair_readback(turn_start_index)
            if unread:
                validation.verdict = 'partial'
                validation.verification_status = 'PARTIAL'
                validation.goal_status = 'ACTIVE'
                validation.confidence = min(validation.confidence, 0.69)
                validation.failed_criteria.append('最终版本缺少完整读回：' + ', '.join(sorted(unread)))
        if guard_is_enabled:
            from ..guard.outcome_guard import advisory_outcome_validation, verification_repair_limit
            advisory_outcome_validation(validation, settings["mode"])
            contract.max_repair_attempts = min(contract.max_repair_attempts, verification_repair_limit(settings["mode"]))
        payload = validation.to_dict()
        payload["repair_attempt"] = repair_attempt
        if pack_is_enabled:
            proof_payload = self._store_proof_pack(
                content=content,
                contract=contract,
                validation=validation,
                ledger=ledger,
            )
            payload["proof_pack"] = proof_payload
        self.agent._last_outcome_status = validation.verdict
        self.agent._last_outcome_validation = payload
        self.agent._last_outcome_contract = contract.to_dict()
        for criterion_result in validation.criterion_results:
            self._append_session_event(
                "outcome.criterion.verification_completed",
                payload={
                    "contract_id": contract.contract_id,
                    "goal_id": contract.goal_id,
                    "criterion_result": dict(criterion_result),
                    "required_proof_level": validation.required_proof_level,
                    "achieved_proof_level": validation.achieved_proof_level,
                    "repair_attempt": repair_attempt,
                },
                metadata={
                    "criterion_id": str(criterion_result.get("criterion_id") or ""),
                    "status": str(criterion_result.get("status") or ""),
                },
            )
        self._append_session_event(
            "turn.outcome_validation",
            payload={
                "validation": payload,
                "contract": contract.to_dict(),
                "evidence": ledger.compact(max_entries=20),
                "repair_attempt": repair_attempt,
            },
            metadata={"verdict": validation.verdict},
        )
        if guard_is_enabled:
            self._append_session_event(
                (
                    "outcome.goal.satisfied"
                    if validation.goal_status == "SATISFIED"
                    else "outcome.goal.verification_failed"
                ),
                payload={
                    "contract_id": contract.contract_id,
                    "goal_id": contract.goal_id,
                    "goal_status": validation.goal_status,
                    "verdict": validation.verdict,
                    "required_proof_level": validation.required_proof_level,
                    "achieved_proof_level": validation.achieved_proof_level,
                    "repair_attempt": repair_attempt,
                },
                metadata={"goal_status": validation.goal_status},
            )
            await self._emit_outcome_progress(
                validation.verdict,
                validation=payload,
                repair_attempt=repair_attempt,
            )
        return validation

    def _store_proof_pack(
        self,
        *,
        content: str,
        contract: Any,
        validation: Any,
        ledger: Any,
    ) -> dict[str, Any]:
        """Build one redacted Proof Pack projection and append its canonical event."""

        from ..runtime.proof_pack import ProofPack

        proof_pack = ProofPack.build(
            contract=contract,
            validation=validation,
            ledger=ledger,
            candidate=content,
            workspace_root=str(getattr(self.agent, "workdir", "") or "") or None,
        )
        proof_payload = {
            "json": proof_pack.to_dict(),
            "markdown": proof_pack.to_markdown(),
        }
        self.agent._last_proof_pack = proof_payload
        self._append_session_event(
            "outcome.proof_pack.generated",
            payload={
                "contract_id": contract.contract_id,
                "goal_id": contract.goal_id,
                "proof_pack": proof_payload["json"],
            },
            metadata={
                "proof_pack_id": proof_payload["json"].get("proof_pack_id", ""),
                "goal_status": proof_payload["json"].get("outcome", {}).get(
                    "status", ""
                ),
                "achieved_proof_level": proof_payload["json"].get("outcome", {}).get(
                    "achieved_proof_level", "P0"
                ),
            },
        )
        return proof_payload

    @staticmethod
    def _outcome_repair_prompt(contract: Any, validation: Any) -> str:
        payload = {
            "goal": contract.goal,
            "failed_criteria": validation.failed_criteria,
            "unsupported_claims": validation.unsupported_claims,
            "missing_deliverables": validation.missing_deliverables,
            "repair_instructions": validation.repair_instructions,
            "criteria": [
                {"id": item.criterion_id, "description": item.description}
                for item in getattr(contract, 'success_criteria', [])
                if item.criterion_id in validation.failed_criteria
            ],
        }
        return (
            "[OUTCOME VALIDATION REPAIR]\n"
            "The candidate response is not yet an acceptable final deliverable. "
            "Revise it to satisfy the original user request and the validation report. "
            "Do not describe progress as completion. Reuse existing tool evidence; call "
            "additional tools only when required evidence is genuinely missing. Return the "
            "actual requested deliverable, not a plan or an offer to continue. "
            "Do not call question to ask for acceptance of the result: deliver the revised "
            "candidate for host validation; any outcome confirmation belongs to its result card.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _local_report_repair_paths(self, turn_start_index: int) -> dict[str, set[str]]:
        """Repair only local text reports already written in this turn, with existing sources."""
        if not getattr(self.agent, 'workdir', None):
            return {'read': set(), 'write': set()}
        root = Path(self.agent.workdir).resolve()
        paths: dict[str, set[str]] = {'read': set(), 'write': set()}
        for message in self.agent.messages[turn_start_index:]:
            for call in message.tool_calls or []:
                if call.tool_name not in paths or self._tool_call_failed(call):
                    continue
                if getattr(call.state, 'value', call.state) != 'completed':
                    continue
                raw = str((call.arguments or {}).get('filePath') or '')
                path = self._local_report_repair_path(raw)
                if path and path.suffix.lower() in {'.md', '.txt', '.rst'} and path.is_file() and path.is_relative_to(root):
                    paths['read'].add(str(path))
                    if call.tool_name == 'write':
                        paths['write'].add(str(path))
        return paths if paths['write'] else {'read': set(), 'write': set()}

    @staticmethod
    def _repair_protocol_fallback(content: str, previous_candidate: str) -> Optional[str]:
        if not re.search(r"<[｜|]+DSML[｜|]+\s*(?:calls|invoke|function_calls)\b", content or ""):
            return None
        return (previous_candidate.strip() + '\n\n修订未执行：模型返回了非正式工具片段，未据此写入文件。已有文件保留，当前结果仍需完成审查。').strip()

    def _local_report_repair_path(self, raw: str) -> Optional[Path]:
        if not raw or '://' in raw:
            return None
        root = Path(self.agent.workdir).resolve()
        if raw.startswith('/workspace/'):
            raw = raw[len('/workspace/'):]
        try:
            return (root / raw).resolve()
        except (OSError, ValueError):
            return None

    def _report_repair_calls_allowed(self, calls, paths, remaining: int, *, pending_reads=None, written=None) -> bool:
        if not calls or len(calls) > remaining:
            return False
        pending = set(pending_reads or ())
        changed = set(written or ())
        has_write = False
        reads: set[str] = set()
        writes: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                return False
            function = call.get('function') or {}
            arguments = function.get('arguments')
            if not isinstance(arguments, dict):
                return False
            name = function.get('name')
            path = self._local_report_repair_path(str(arguments.get('filePath') or ''))
            if path is None or str(path) not in paths.get(name, set()):
                return False
            if name == 'write':
                if str(path) in changed:
                    return False
                changed.add(str(path))
                pending.add(str(path))
                writes.add(str(path))
                has_write = True
            elif name == 'read':
                reads.add(str(path))
        # Reserve actual future read calls; proposed parallel reads cannot prove a
        # preceding write was observed. Do not spend the final slot on a write.
        if reads & writes or (has_write and len(calls) + len(pending) > remaining):
            return False
        return True

    def _report_repair_readback(self, turn_start_index: int) -> set[str]:
        from ..subtask_handoff import build_subtask_tool_trace, report_revision_evidence
        by_path: dict[str, list] = {}
        for entry in build_subtask_tool_trace(self.agent.messages[turn_start_index:]):
            path = self._local_report_repair_path(str((entry.get('arguments') or {}).get('filePath') or ''))
            if path is not None:
                by_path.setdefault(str(path), []).append({**entry, 'arguments': {'filePath': str(path),
                    **{k: v for k, v in entry.get('arguments', {}).items() if k != 'filePath'}}})
        # Check each file independently; the shared receipt's display cap must
        # not hide outstanding reads when more than ten reports were written.
        return {path for path, trace in by_path.items() if report_revision_evidence(trace)['unread_file_count']}

    async def _call_llm_with_timeout(
        self,
        tools: list[Any],
        *,
        iteration: int,
    ) -> tuple[str, Optional[list[dict]], Any]:
        # A paused Worker keeps its live turn; waiting must not consume model timeout.
        gate = getattr(self.agent, "_execution_gate", None)
        if gate is not None:
            await gate()
        await self._emit_llm_progress("running", iteration=iteration)
        timeout_seconds = self._llm_call_timeout_seconds(iteration=iteration)
        try:
            call = self.agent._call_llm(tools)
            if timeout_seconds > 0:
                result = await asyncio.wait_for(call, timeout=timeout_seconds)
            else:
                result = await call
        except asyncio.TimeoutError as exc:
            await self._emit_llm_progress("timeout", iteration=iteration)
            raise TimeoutError(
                f"LLM request timed out after {timeout_seconds:g}s in harness"
            ) from exc
        except Exception:
            await self._emit_llm_progress("error", iteration=iteration)
            raise
        await self._emit_llm_progress("completed", iteration=iteration)
        return result

    async def _call_llm_with_guidance_race(
        self,
        tools: list[Any],
        *,
        iteration: int,
        turn_id: str,
        stream_message_id: str,
    ) -> tuple[Optional[tuple[str, Optional[list[dict]], Any]], list[Any]]:
        port = getattr(self.agent, "_guidance_port", None)
        interrupt_enabled = str(
            os.environ.get("TCODE_STEER_MODEL_INTERRUPT_ENABLED", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        cancel_mode = str(
            getattr(self.agent.provider, "stream_cancel_mode", "not_supported")
            or "not_supported"
        )
        if port is None or not interrupt_enabled or cancel_mode == "not_supported":
            return await self._call_llm_with_timeout(tools, iteration=iteration), []

        from ..runtime.guidance import GuidanceSafePoint

        llm_task = asyncio.create_task(
            self._call_llm_with_timeout(tools, iteration=iteration)
        )
        steer_task = asyncio.create_task(
            port.wait_for_steer(
                session_id=str(getattr(self.agent, "session_id", "") or ""),
                turn_id=turn_id,
                timeout_seconds=self._llm_call_timeout_seconds(iteration=iteration),
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {llm_task, steer_task},
                    timeout=5.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    try:
                        await port.heartbeat_turn_owner(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=turn_id,
                            owner_id=str(
                                getattr(self.agent, "_guidance_owner_id", "") or ""
                            ),
                            phase="calling_llm",
                            stream_epoch=int(
                                getattr(self.agent, "_guidance_stream_epoch", 0) or 0
                            ),
                        )
                    except Exception:
                        pass
                    continue
                if llm_task in done:
                    if not steer_task.done():
                        steer_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await steer_task
                    return llm_task.result(), []

                steer_ready = False
                try:
                    steer_ready = bool(steer_task.result())
                except Exception:
                    steer_ready = False
                if not steer_ready:
                    steer_task = asyncio.create_task(
                        port.wait_for_steer(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=turn_id,
                            timeout_seconds=self._llm_call_timeout_seconds(
                                iteration=iteration
                            ),
                        )
                    )
                    continue
                await asyncio.sleep(self._guidance_batch_window_seconds())
                claimed = await self._claim_pending_guidance(
                    turn_id=turn_id,
                    safe_point=GuidanceSafePoint.AFTER_MODEL_CALL,
                )
                if not claimed:
                    steer_task = asyncio.create_task(
                        port.wait_for_steer(
                            session_id=str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            turn_id=turn_id,
                            timeout_seconds=self._llm_call_timeout_seconds(
                                iteration=iteration
                            ),
                        )
                    )
                    continue
                llm_task.cancel()
                cancel_timeout = self._guidance_model_cancel_timeout_seconds()
                cancel_result = "completed_before_cancel"
                try:
                    await asyncio.wait_for(llm_task, timeout=cancel_timeout)
                except asyncio.CancelledError:
                    cancel_result = "cancelled"
                except asyncio.TimeoutError:
                    cancel_result = "timeout"
                from ..runtime.telemetry import TelemetryGateway

                TelemetryGateway.emit(
                    "guidance.model_cancel",
                    {
                        "provider": type(self.agent.provider).__name__,
                        "cancel_mode": cancel_mode,
                        "result": cancel_result,
                    },
                    session_id=str(getattr(self.agent, "session_id", "") or ""),
                )
                self.agent._guidance_stream_epoch = int(
                    getattr(self.agent, "_guidance_stream_epoch", 0) or 0
                ) + 1
                emitter = getattr(self.agent, "_agent_event_emitter", None)
                if callable(emitter):
                    event = {
                        "type": "agent_event",
                        "kind": "model_output.superseded",
                        "session_id": str(
                            getattr(self.agent, "session_id", "") or ""
                        ),
                        "turn_id": turn_id,
                        "message_id": stream_message_id,
                        "stream_epoch": self.agent._guidance_stream_epoch,
                        "superseded_by_guidance_ids": [item.id for item in claimed],
                    }
                    result = emitter(event)
                    if hasattr(result, "__await__"):
                        await result
                return None, claimed
        finally:
            for task in (llm_task, steer_task):
                if not task.done():
                    task.cancel()

    @staticmethod
    def _is_llm_timeout_error(exc: BaseException) -> bool:
        text = str(exc or "").strip().lower()
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or (
            "llm" in text and ("timed out" in text or "timeout" in text)
        )

    @staticmethod
    def _unwrap_task_result(content: str) -> str:
        text = str(content or "").strip()
        matches = re.findall(
            r"<task_result(?:\s+[^>]*)?>\s*(.*?)\s*</task_result>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if len(matches) == 1:
            text = matches[0].strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return fenced.group(1).strip() if fenced else text

    def _latest_successful_task_result(self, *, turn_start_index: int) -> str:
        for msg in reversed(self.agent.messages[turn_start_index:]):
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            tool_calls = list(getattr(msg, "tool_calls", []) or [])
            if not tool_calls:
                continue
            tool_call = tool_calls[0]
            tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
            if tool_name not in {"task", "assign_task"}:
                continue
            if self._tool_call_failed(tool_call):
                continue
            content = self._unwrap_task_result(getattr(msg, "content", "") or "")
            if content:
                return content
        return ""

    def _latest_successful_tool_checkpoint(self, *, turn_start_index: int) -> str:
        completed_tools: list[str] = []
        artifact_paths: list[str] = []
        latest_preview = ""
        preferred_path_keys = {
            "output_path",
            "host_path",
            "record_path",
            "tables_extract_path",
            "full_text_md_path",
            "visual_manifest_path",
            "pdf_path",
        }

        def _collect_paths(value: Any, *, depth: int = 0) -> None:
            if depth > 4 or len(artifact_paths) >= 8:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    if len(artifact_paths) >= 8:
                        return
                    if str(key) in preferred_path_keys and isinstance(item, str):
                        path = item.strip()
                        if path and path not in artifact_paths:
                            artifact_paths.append(path)
                    elif isinstance(item, (dict, list)):
                        _collect_paths(item, depth=depth + 1)
            elif isinstance(value, list):
                for item in value[:20]:
                    _collect_paths(item, depth=depth + 1)

        for msg in reversed(self.agent.messages[turn_start_index:]):
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            tool_calls = list(getattr(msg, "tool_calls", []) or [])
            for tool_call in tool_calls:
                if self._tool_call_failed(tool_call):
                    continue
                tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
                if tool_name and tool_name not in completed_tools:
                    completed_tools.append(tool_name)
                _collect_paths(dict(getattr(tool_call, "metadata", {}) or {}))
            if not latest_preview:
                content = str(getattr(msg, "content", "") or "").strip()
                if content:
                    latest_preview = (
                        content
                        if len(content) <= 2400
                        else content[:2400] + " ...[preview truncated]"
                    )
            if len(completed_tools) >= 6 and (artifact_paths or latest_preview):
                break

        if not completed_tools or (not artifact_paths and not latest_preview):
            return ""
        lines = [
            "子代理已完成工具步骤，但最终模型整理响应超时。以下为可恢复的部分结果。",
            "- status: partial",
            f"- completed_tools: {', '.join(reversed(completed_tools))}",
        ]
        if artifact_paths:
            lines.append("- artifact_paths:")
            lines.extend(f"  - {path}" for path in artifact_paths)
        if latest_preview:
            lines.extend(["- latest_tool_preview:", latest_preview])
        return "\n".join(lines)

    def _task_result_timeout_fallback_block_reason(self, *, turn_start_index: int) -> str:
        validation = getattr(self.agent, "_last_outcome_validation", {}) or {}
        if isinstance(validation, dict) and validation.get("verdict") == "repair":
            return "outcome_validation_repair"
        for msg in reversed(self.agent.messages[turn_start_index:]):
            if str(getattr(msg, "role", "") or "") != "user":
                continue
            content = str(getattr(msg, "content", "") or "").lstrip()
            if content.startswith("[OUTCOME VALIDATION REPAIR]"):
                return "outcome_repair_prompt"
        return ""

    def _contract_compatible_timeout_fallback(
        self,
        *,
        content: str,
        source: str,
        error: BaseException,
        contract: Any,
    ) -> tuple[str, Any]:
        """Return a recoverable partial result without violating the output contract.

        Plain-message contracts can use the completed tool/task result directly. For a
        JSON-only contract without a formal schema, preserve that result in a generic
        partial-result envelope. A declared JSON schema remains authoritative: if the
        envelope does not validate, the caller must fail instead of inventing fields.
        """
        from ..guard import validate_outcome_deterministic

        schema_validator = getattr(
            self.agent,
            "_validate_json_schema_text",
            None,
        )
        validation = validate_outcome_deterministic(
            content,
            contract,
            schema_validator=schema_validator,
        )
        if validation.verdict == "pass":
            return content, validation

        partial_policy = str(
            getattr(contract, "partial_result_policy", "") or ""
        ).strip().lower()
        if partial_policy != "return_partial_with_warnings":
            return "", validation

        required_deliverables = [
            item
            for item in (getattr(contract, "deliverables", []) or [])
            if bool(getattr(item, "required", True))
        ]
        requires_json = any(
            str(getattr(item, "kind", "") or "").strip().lower() == "json"
            for item in required_deliverables
        )
        if not requires_json:
            return "", validation

        partial_content = json.dumps(
            {
                "status": "partial",
                "outcome_status": "partial",
                "source": f"{source}_partial_envelope",
                "completed_result": content,
                "warnings": [
                    "Final model synthesis timed out; this response preserves the latest "
                    "completed task or tool result.",
                    str(error)[:1000],
                ],
            },
            ensure_ascii=False,
        )
        partial_validation = validate_outcome_deterministic(
            partial_content,
            contract,
            schema_validator=schema_validator,
        )
        if partial_validation.verdict != "pass":
            return "", validation
        return partial_content, partial_validation

    async def _finalize_from_task_result_after_llm_timeout(
        self,
        *,
        user_message: str,
        turn_start_index: int,
        turn_id: str,
        user_message_id: str,
        parent_id: str,
        error: BaseException,
        contract: Any = None,
    ) -> Optional[Message]:
        if not self._is_llm_timeout_error(error):
            return None
        block_reason = self._task_result_timeout_fallback_block_reason(
            turn_start_index=turn_start_index,
        )
        if block_reason:
            self._append_session_event(
                "turn.llm_timeout_fallback_skipped",
                payload={
                    "turn_id": turn_id,
                    "error": str(error)[:1000],
                    "reason": block_reason,
                },
                metadata={"recovered": False},
            )
            return None
        content = self._latest_successful_task_result(
            turn_start_index=turn_start_index,
        )
        source = "latest_successful_task_result"
        if not content:
            content = self._latest_successful_tool_checkpoint(
                turn_start_index=turn_start_index,
            )
            source = "latest_successful_tool_checkpoint"
        if not content:
            return None
        if contract is not None:
            compatible_content, validation = self._contract_compatible_timeout_fallback(
                content=content,
                source=source,
                error=error,
                contract=contract,
            )
            if not compatible_content:
                self._append_session_event(
                    "turn.llm_timeout_fallback_skipped",
                    payload={
                        "turn_id": turn_id,
                        "error": str(error)[:1000],
                        "reason": "fallback_candidate_failed_contract",
                        "validation": validation.to_dict(),
                    },
                    metadata={"recovered": False},
                )
                return None
            if compatible_content != content:
                source = f"{source}_partial_envelope"
                content = compatible_content
        final_msg = Message(
            role="assistant",
            content=content,
            id=self.agent._generate_id(),
            parent_id=parent_id,
        )
        self.agent.messages.append(final_msg)
        self._append_session_event(
            "turn.llm_timeout_fallback",
            payload={
                "turn_id": turn_id,
                "error": str(error)[:1000],
                "source": source,
            },
            metadata={"recovered": True},
        )
        self.agent._last_outcome_status = "partial"
        self.agent._last_outcome_validation = {
            "verdict": "partial",
            "failed_criteria": ["final model synthesis timed out"],
            "warnings": [str(error)[:1000]],
            "validator": "timeout_fallback",
        }
        return await self._finalize_turn(
            user_message=user_message,
            final_message=final_msg,
            turn_messages=self.agent.messages[turn_start_index:],
            turn_id=turn_id,
            user_message_id=user_message_id,
            completion_state="partial",
        )

    def _max_tool_calls_per_turn(self) -> int:
        options = dict(getattr(self.agent.agent_info, "options", {}) or {})
        try:
            raw = int(
                options.get(
                    "max_tool_calls_per_turn",
                    os.environ.get("TCODE_MAX_TOOL_CALLS_PER_TURN", "20"),
                )
            )
        except Exception:
            raw = 20
        return max(1, min(raw, 200))

    def _max_consecutive_tool_failures(self) -> int:
        try:
            raw = int(os.environ.get("TCODE_MAX_CONSECUTIVE_TOOL_FAILURES", "5"))
        except Exception:
            raw = 5
        return max(1, min(raw, 20))

    async def _safe_preload_recommended_skill(self, *, parent_msg_id: str) -> Optional[Message]:
        """对推荐技能预加载加超时护栏，避免首包前被慢路径阻塞。"""
        timeout_seconds = self._recommended_skill_preload_timeout_seconds()
        try:
            return await asyncio.wait_for(
                self.agent._maybe_preload_recommended_skill(parent_msg_id=parent_msg_id),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            print(
                f"[Agent] Recommended skill preload timed out after {timeout_seconds:.1f}s; continue without preload"
            )
            return None
        except Exception as exc:
            print(f"[Agent] Recommended skill preload failed: {exc}")
            return None

    async def _safe_get_tool_definitions(self) -> list[Any]:
        """在超时护栏内构建本轮唯一模型工具定义列表。

        返回:
            成功时返回工具定义列表；失败时保留已构建且仍准入的本地核心工具。
        """

        await self._emit_llm_progress("preparing_tools", iteration=0)
        timeout_seconds = self._tool_definitions_timeout_seconds()
        try:
            return await asyncio.wait_for(
                self.agent._get_tool_definitions(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            print(
                f"[Agent] Tool definitions timed out after {timeout_seconds:.1f}s; using local fallback"
            )
        except Exception as exc:
            print(f"[Agent] Tool definitions failed: {exc}")
        core = {"capability_catalog", "knowledge_search", "read", "glob", "grep", "inspect_file", "question", "task_wait", "process"}
        from ..tools.builtin.goal_checkpoint import goal_checkpoint_active
        if goal_checkpoint_active.get():
            core.add("goal_checkpoint")
        getter = getattr(self.agent, "_discoverable_tool_definitions", None)
        tools = []
        if callable(getter):
            try:
                tools = [tool for tool in getter() if tool.name in core]
            except Exception as exc:
                print(f"[Agent] Cached tool fallback failed: {type(exc).__name__}")
        if not tools:
            fallback = getattr(self.agent, "_get_core_tool_definitions", None)
            if callable(fallback):
                try:
                    tools = await asyncio.wait_for(fallback(), timeout=2.0)
                except Exception:
                    tools = []
        self.agent._last_tool_routing = {"selected_tools": [tool.name for tool in tools],
                                         "reason": "definition_loading_failed_local_fallback"}
        self.agent.messages.append(Message(
            role="system", content="[TOOL_DISCOVERY_DEGRADED] Tool loading failed. Only the listed local tools are available; do not claim unavailable actions were executed.",
            id=self.agent._generate_id(),
        ))
        return tools

    def _tool_call_fingerprint(self, tool_call: Any) -> str:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        arguments = getattr(tool_call, "arguments", {}) or {}
        if tool_name == "task" and isinstance(arguments, dict):
            # Description/contract wording may change while retrying the same work.
            arguments = {
                "prompt": " ".join(str(arguments.get("prompt") or "").split()),
                "subagent_type": str(arguments.get("subagent_type") or "general").strip(),
                "task_id": str(arguments.get("task_id") or "").strip(),
            }
        try:
            args_text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        except Exception:
            args_text = str(arguments)
        return f"{tool_name}::{args_text}"

    def _tool_call_failed(self, tool_call: Any) -> bool:
        if str(getattr(tool_call, "error", "") or "").strip():
            return True
        if str(getattr(tool_call, "tool_name", "") or "") == "task":
            metadata = dict(getattr(tool_call, "metadata", {}) or {})
            if str(metadata.get("outcome_status") or metadata.get("status") or "").lower() in {"failed", "blocked", "cancelled"}:
                return True
        state = getattr(tool_call, "state", None)
        state_value = getattr(state, "value", state)
        return str(state_value or "").strip().lower() == ToolState.ERROR.value

    def _matching_failed_tool_calls(
        self,
        tool_call: ToolCall,
        *,
        turn_start_index: int,
    ) -> list[ToolCall]:
        fingerprint = self._tool_call_fingerprint(tool_call)
        matches: list[ToolCall] = []
        for msg in (getattr(self.agent, "messages", None) or [])[turn_start_index:]:
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            for existing in getattr(msg, "tool_calls", []) or []:
                if self._tool_call_fingerprint(existing) != fingerprint:
                    continue
                if self._tool_call_failed(existing):
                    matches.append(existing)
        return matches

    def _computer_observed_after_failure(
        self,
        failure: ToolCall,
        *,
        turn_start_index: int,
    ) -> bool:
        """A required observe changes the state behind an otherwise identical retry."""
        seen_failure = False
        failed_target = str((getattr(failure, "arguments", {}) or {}).get("target_ref") or "")
        for msg in (getattr(self.agent, "messages", None) or [])[turn_start_index:]:
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            for existing in getattr(msg, "tool_calls", []) or []:
                if existing is failure:
                    seen_failure = True
                    continue
                arguments = getattr(existing, "arguments", {}) or {}
                if (
                    seen_failure
                    and is_computer_tool(getattr(existing, "tool_name", ""))
                    and computer_operation(getattr(existing, "tool_name", ""), arguments) == "observe"
                    and str(arguments.get("target_ref") or "") == failed_target
                    and not self._tool_call_failed(existing)
                ):
                    return True
        return False

    def _tool_trace_for_review(self, *, turn_start_index: int) -> list[str]:
        trace: list[str] = []
        for msg in self.agent.messages[turn_start_index:]:
            for tc in getattr(msg, "tool_calls", []) or []:
                name = str(getattr(tc, "tool_name", "") or "").strip()
                if name:
                    trace.append(name)
        return trace

    def _apply_synthetic_tool_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        tool_call.state = (
            ToolState.ERROR
            if str(result.error or "").strip()
            else ToolState.COMPLETED
        )
        tool_call.output = result.output
        tool_call.error = result.error
        tool_call.metadata = dict(result.metadata or {})
        now = self.agent._current_time()
        if tool_call.start_time is None:
            tool_call.start_time = now
        tool_call.end_time = now
        return result

    def _build_terminal_turn_response(self, result: ToolResult) -> str:
        """Public terminal message; diagnostic details stay in tool records."""
        metadata = dict(result.metadata or {})
        if metadata.get("accepted_async"):
            return result.output or "任务已提交，结果出来后会继续处理。"
        if metadata.get("human_review_required"):
            return "这一步需要审核，审核完成后才能继续。"
        if str(metadata.get("status") or "") in {"blocked", "failed", "terminated"} or result.error or any(metadata.get(key) for key in (
            "browser_agent_required", "tool_call_limit_exceeded",
            "consecutive_tool_failure_limit_exceeded", "duplicate_retry_blocked",
        )):
            return self._build_human_terminal_fallback(result)
        return result.output or "抱歉，处理时遇到了一点问题，暂时没能完成。"

    def _build_terminal_turn_diagnostic(self, result: ToolResult) -> str:
        metadata = dict(result.metadata or {})
        if bool(metadata.get("accepted_async")):
            return result.output or "任务已异步受理，请使用返回编号后续查询。"

        if bool(metadata.get("browser_agent_required")):
            return "\n".join(
                [
                    "当前任务未完成。",
                    "阻塞原因: 构建模式不能直接调用 browser，本次浏览器并未执行。",
                    "下一步建议: 使用 task 工具并设置 subagent_type=agent-browser，"
                    "由专用 Browser Agent 在 Sandbox 中执行。",
                ]
            )

        if bool(metadata.get("tool_call_limit_exceeded")):
            limit = int(metadata.get("tool_call_limit", 0) or 0)
            return "\n".join(
                [
                    "当前任务未完成。",
                    f"阻塞原因: 本回合已达到工具调用上限（{limit} 次）。",
                    "下一步建议: 请检查前述错误后重新规划，避免继续试错。",
                ]
            )

        if bool(metadata.get("consecutive_tool_failure_limit_exceeded")):
            count = int(metadata.get("consecutive_tool_failures", 0) or 0)
            if tool_name := str(metadata.get("failed_task_tool_name") or ""):
                return (
                    f"抱歉，当前平台暂时无法完成这项任务。{tool_name} 已失败 {count} 次，"
                    "为避免反复报错，系统已停止自动尝试。请稍后重试；"
                    "如果问题持续存在，请联系管理员并提供本次任务记录。"
                )
            lines = [
                "当前任务未完成。",
                f"阻塞原因: 已连续发生 {count} 次工具调用失败，系统已停止继续试错。",
            ]
            if str(result.error or "").strip():
                lines.append(f"最后错误: {str(result.error or '').strip()[:1200]}")
            lines.append("下一步建议: 请调整方案、改用合适的子代理，或修复环境配置后重试。")
            return "\n".join(lines)

        review = metadata.get("retry_review")
        if bool(metadata.get("duplicate_retry_blocked")):
            summary = (
                str(review.get("summary", "") or "").strip()
                if isinstance(review, dict)
                else ""
            )
            next_step = (
                str(review.get("recommended_next_step", "") or "").strip()
                if isinstance(review, dict)
                else ""
            )
            lines = ["当前任务未完成。"]
            if summary:
                lines.append(f"阻塞原因: {summary}")
            elif str(result.error or "").strip():
                lines.append(f"阻塞原因: {str(result.error or '').strip()[:1200]}")
            if next_step:
                lines.append(f"下一步建议: {next_step}")
            else:
                lines.append("下一步建议: 停止原样重试，改为重新规划或明确报告当前阻塞。")
            return "\n".join(lines)

        if bool(metadata.get("human_review_required")):
            lines = ["当前任务未完成。", "原因: 当前操作需要人工审核后才能继续。"]
            if str(result.output or "").strip():
                lines.append(f"详情: {str(result.output or '').strip()[:1200]}")
            return "\n".join(lines)

        if str(result.error or "").strip():
            lines = ["当前任务未完成。"]
            lines.append(f"阻塞原因: {str(result.error or '').strip()[:1200]}")
            if str(result.output or "").strip() and str(result.output or "").strip() != str(result.error or "").strip():
                lines.append(f"详情: {str(result.output or '').strip()[:1200]}")
            return "\n".join(lines)

        return result.output or "任务已异步受理，请使用返回编号后续查询。"

    def _queue_terminal_summary_request(
        self,
        result: ToolResult,
        *,
        parent_id: str,
    ) -> Message:
        """Ask the main model for one final, tool-free user-facing summary."""

        diagnostic = self._build_terminal_turn_diagnostic(result)
        message = Message(
            role="user",
            content=(
                "[INTERNAL TERMINAL SUMMARY REQUEST]\n"
                "工具执行已经停止，本回合不得再调用任何工具。请结合原始用户请求、"
                "已经完成的步骤和下面的内部诊断，直接给用户一段自然、简洁、可操作的"
                "最终回复。说明实际完成了什么、什么没有完成、错误造成的影响，以及一个"
                "最合适的下一步。不要照抄内部诊断，不要使用“当前任务未完成 / 阻塞原因 / "
                "最后错误 / 下一步建议”这套运行时模板，也不要声称未发生的成功。\n\n"
                "不要展示工具名、错误次数、参数、Schema、英文异常或内部路径。"
                "遇到技术故障只需简短致歉并说明暂未完成；具体诊断仅供开发者使用。\n\n"
                "如果已有已验收的阶段产物，仍须交付其中能回答用户的有效结果，"
                "明确缺失部分；不要只返回错误或计划。未验收的推断不能当作已完成结果。\n"
                "可用的部分结果：" + json.dumps((result.metadata or {}).get("partial_result") or {}, ensure_ascii=False)[:14000] + "\n\n"
                f"内部诊断：\n{diagnostic[:2400]}"
            ),
            id=self.agent._generate_id(),
            parent_id=parent_id,
            metadata={"internal": True, "purpose": "terminal_summary"},
        )
        setattr(message, "_internal", True)
        setattr(message, "_hide_from_frontend", True)
        self.agent.messages.append(message)
        return message

    @staticmethod
    def _build_human_terminal_fallback(result: ToolResult) -> str:
        """Do not leak internal exceptions through the deterministic fallback."""
        partial = (result.metadata or {}).get("partial_result") or {}
        completed = partial.get("completed_stages") or []
        if completed:
            progress = "、".join(str(stage.get("label") or "已完成步骤") for stage in completed)
            return f"已完成：{progress}。已有结果已保留。\n\n抱歉，后续处理遇到了一点问题，剩余结果暂时未能生成。"
        return "抱歉，处理时遇到了一点问题，暂时没能完成。你可以稍后再试。"

    async def _finalize_terminal_summary_fallback(
        self,
        *,
        result: ToolResult,
        user_message: str,
        turn_start_index: int,
        turn_id: str,
        user_message_id: str,
        parent_id: str,
        contract: Any,
        synthesis_error: BaseException | None = None,
    ) -> Message:
        content = self._append_downloaded_pdf_paths(
            self._build_human_terminal_fallback(result),
            turn_start_index=turn_start_index,
            contract=contract,
        )
        final_msg = Message(
            role="assistant",
            content=content,
            id=self.agent._generate_id(),
            parent_id=parent_id,
        )
        self.agent.messages.append(final_msg)
        self.agent._last_outcome_status = "blocked"
        self.agent._last_outcome_validation = {
            "verdict": "blocked",
            "failed_criteria": ["tool execution stopped before completion"],
            "warnings": (
                [str(synthesis_error)[:1000]] if synthesis_error is not None else []
            ),
            "validator": "terminal_summary_fallback",
        }
        return await self._finalize_turn(
            user_message=user_message,
            final_message=final_msg,
            turn_messages=self.agent.messages[turn_start_index:],
            turn_id=turn_id,
            user_message_id=user_message_id,
            completion_state="blocked",
        )

    @staticmethod
    def _tool_result_console_line(result: ToolResult) -> str:
        """Show actionable validation errors even when a tool produced no output."""
        error = str(result.error or "").strip()
        label, text, limit = ("错误", error, 800) if error else ("结果", str(result.output or ""), 100)
        preview = text[:limit] + ("..." if len(text) > limit else "")
        return f"{label}: {preview}"

    def _tool_call_limit_result(self, tool_call: ToolCall, *, limit: int) -> ToolResult:
        content = (
            f"本回合工具调用已达到 {limit} 次上限，已阻止继续调用 "
            f"`{tool_call.tool_name}`。"
        )
        return self._apply_synthetic_tool_result(
            tool_call,
            ToolResult(
                title=tool_call.tool_name,
                output=content,
                error=content,
                metadata={
                    "tool_call_limit_exceeded": True,
                    "tool_call_limit": limit,
                    "should_end_turn": True,
                    "execution_skipped": True,
                    "execution_environment": {
                        "location": "not_executed",
                        "label": "Not executed",
                        "sandboxed": False,
                    },
                },
            ),
        )

    def _apply_tool_loop_policy(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        *,
        consecutive_failures: int,
        turn_start_index: int = 0,
    ) -> ToolResult:
        metadata = dict(result.metadata or {})
        # 工具可显式选择按错误签名进行有限纠正；未声明的工具保持原有策略。
        if result.error and metadata.get("retryable") is True and not metadata.get("terminal_tool_error"):
            from ..runtime.tool_arguments import _tool_schema, allow_tool_correction
            from ..tools.registry import ToolRegistry
            schema = _tool_schema(ToolRegistry.get(tool_call.tool_name)) or {}
            if schema.get("x-tcode-correction-policy") == "distinct-errors-v1":
                states = getattr(self, "_tool_correction_states", {})
                counts = states.setdefault(turn_start_index, {})
                self._tool_correction_states = {turn_start_index: counts}
                if not allow_tool_correction(counts, tool_call.tool_name,
                                             str((tool_call.arguments or {}).get("slot") or ""), str(result.error)):
                    metadata.update(should_end_turn=True, tool_loop_terminal=True,
                                    correction_budget_exhausted=True)
        deterministic_browser_failure = bool(
            metadata.get("browser_agent_required")
            or metadata.get("runner_required")
            or (
                tool_call.tool_name == "browser"
                and metadata.get("sandbox_required")
            )
        )
        explicitly_non_retryable = (
            (
                metadata.get("retryable") is False
                and not bool(metadata.get("permission_denied"))
            )
            or bool(metadata.get("terminal_tool_error"))
        )
        if bool(metadata.get("permission_denied")):
            # A denied tool never executes, but the failed action must be
            # returned to the model so it can summarize prior progress or use
            # an allowed alternative.  The consecutive-failure guard below
            # still prevents repeated denied calls from looping forever.
            metadata["should_end_turn"] = False
            metadata["tool_loop_terminal"] = False
        if str(result.error or "").strip() and (
            deterministic_browser_failure
            or explicitly_non_retryable
            or bool(metadata.get("duplicate_retry_blocked"))
        ):
            metadata["should_end_turn"] = True
            metadata["tool_loop_terminal"] = True

        recovery = dict(metadata.get("recovery") or {})
        recovery_limit = int(recovery.get("max_attempts") or 0)
        if is_computer_tool(tool_call.tool_name):
            chains = getattr(self, "_computer_recovery_chains", None)
            if not isinstance(chains, dict):
                chains = {}
                self._computer_recovery_chains = chains
            chain = dict(chains.get(turn_start_index) or {"attempts": 0})
            operation = computer_operation(tool_call.tool_name, tool_call.arguments)
            if str(result.error or "").strip() and recovery.get("action") == "observe_then_replan":
                observation_refusal = str(result.error or "") == "recovery_observe_required" or (
                    bool(metadata.get("computer_argument_rejected")) and bool(metadata.get("execution_skipped"))
                )
                # Host refused before dispatch: allow one correction to the
                # required read, not a second write attempt or an automatic replay.
                counter = "observation_refusals" if observation_refusal else "attempts"
                chain[counter] = int(chain.get(counter) or 0) + 1
                chain["awaiting_observe"] = True
                chain["target_ref"] = str((tool_call.arguments or {}).get("target_ref") or "")
                if bool(metadata.get("known_action_receipt")):
                    validation_key = str(metadata.get("computer_action_signature") or "")
                    if not validation_key:
                        action_args = {key: value for key, value in (tool_call.arguments or {}).items()
                            if key not in {"observation_ref", "authorization"}}
                        validation_key = hashlib.sha256(json.dumps(action_args, sort_keys=True).encode()).hexdigest()
                    if chain.get("validation_key") != validation_key:
                        chain["validation_rounds"] = 0
                    chain["validation_key"] = validation_key
                    chain["validation_rounds"] = int(chain.get("validation_rounds") or 0) + 1
                chains[turn_start_index] = chain
                metadata["computer_recovery_attempt"] = chain["attempts"]
                validation_limit = int(recovery.get("max_validation_rounds") or 0)
                validation_exhausted = bool(
                    validation_limit
                    and int(chain.get("validation_rounds") or 0) >= validation_limit
                )
                if (recovery_limit and chain["attempts"] >= recovery_limit) or validation_exhausted or int(chain.get("observation_refusals") or 0) >= 2:
                    metadata["should_end_turn"] = True
                    metadata["tool_loop_terminal"] = True
                    metadata["computer_recovery_limit_exceeded"] = True
            elif not str(result.error or "").strip() and operation == "observe":
                observed = dict(metadata.get("computer") or {})
                target_ref = str((tool_call.arguments or {}).get("target_ref") or "")
                observation_ref = str(observed.get("observation_ref") or "")
                fresh_for_target = bool(
                    chain.get("awaiting_observe")
                    and target_ref
                    and target_ref == str(chain.get("target_ref") or "")
                    and observation_ref
                    and observation_ref != str(chain.get("observation_ref") or "")
                )
                chain["awaiting_observe"] = not fresh_for_target
                if fresh_for_target:
                    chain["attempts"] = 0
                    chain["observation_refusals"] = 0
                    chain["observation_ref"] = observation_ref
                chains[turn_start_index] = chain
                metadata["computer_recovery_attempt"] = int(chain.get("attempts") or 0)
            elif not str(result.error or "").strip():
                chains.pop(turn_start_index, None)

        failure_limit = self._max_consecutive_tool_failures()
        task_failed = tool_call.tool_name == "task" and (
            bool(str(result.error or "").strip())
            or str(metadata.get("outcome_status") or metadata.get("status") or "").lower()
            in {"failed", "blocked", "cancelled"}
        )
        task_failures = (
            len(self._matching_failed_tool_calls(tool_call, turn_start_index=turn_start_index)) + 1
            if task_failed else 0
        )
        if (task_failed and task_failures >= failure_limit) or (
            tool_call.tool_name != "task"
            and str(result.error or "").strip()
            and consecutive_failures >= failure_limit
        ):
            metadata["should_end_turn"] = True
            metadata["tool_loop_terminal"] = True
            metadata["consecutive_tool_failure_limit_exceeded"] = True
            metadata["consecutive_tool_failures"] = task_failures if task_failed else consecutive_failures
            metadata["consecutive_tool_failure_limit"] = failure_limit
            if task_failed:
                metadata["failed_task_tool_name"] = "同一子任务"
                metadata["termination_scope"] = "agent"

        result.metadata = metadata
        tool_call.metadata = dict(metadata)
        return result

    @staticmethod
    def _should_end_turn_for_tool_result(
        tool_call: ToolCall,
        result: ToolResult,
    ) -> bool:
        """Keep child-scoped termination from escaping a delegation boundary."""

        metadata = dict(result.metadata or {})
        if not bool(metadata.get("should_end_turn")):
            return False
        termination_scope = str(metadata.get("termination_scope") or "").strip().lower()
        delegated_tool = str(tool_call.tool_name or "").strip() in {
            "task",
            "assign_task",
        }
        if delegated_tool and termination_scope in {"child", "subagent"}:
            return False
        return True

    def _tool_call_snapshot(self, tool_call: Any) -> dict[str, Any]:
        state_obj = getattr(tool_call, "state", None)
        state = getattr(state_obj, "value", state_obj)
        return {
            "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
            "call_id": str(getattr(tool_call, "call_id", "") or ""),
            "arguments": dict(getattr(tool_call, "arguments", {}) or {}),
            "state": str(state or "").strip().lower(),
            "start_time": getattr(tool_call, "start_time", None),
            "end_time": getattr(tool_call, "end_time", None),
        }

    def _tool_call_from_snapshot(self, snapshot: dict[str, Any]) -> ToolCall:
        state_value = str(snapshot.get("state", "") or "").strip().lower()
        try:
            state = ToolState(state_value) if state_value else ToolState.ERROR
        except ValueError:
            state = ToolState.ERROR
        return ToolCall(
            tool_name=str(snapshot.get("tool_name", "") or ""),
            call_id=str(snapshot.get("call_id", "") or ""),
            arguments=dict(snapshot.get("arguments", {}) or {}),
            state=state,
            start_time=snapshot.get("start_time"),
            end_time=snapshot.get("end_time"),
        )

    async def _turn_session(self) -> Optional[Any]:
        existing_session = getattr(self.agent, "_existing_session", None)
        if existing_session is not None:
            if (
                not getattr(self.agent, "messages", None)
                and getattr(existing_session, "messages", None)
            ):
                self.agent.messages = list(existing_session.messages)
            return existing_session
        try:
            from ..session.manager import SessionManager

            session = await SessionManager.get(self.agent.session_id)
            if session is not None:
                self.agent._existing_session = session
                if (
                    not getattr(self.agent, "messages", None)
                    and getattr(session, "messages", None)
                ):
                    self.agent.messages = list(session.messages)
            return session
        except Exception:
            return None

    def _event_workdir(self, session: Optional[Any] = None) -> str:
        return str(
            getattr(session, "workdir", "")
            or getattr(self.agent, "workdir", "")
            or os.environ.get("TCODE_WORKDIR", "")
            or os.getcwd()
        )

    def _research_message_by_id(self, message_id: str) -> Optional[Any]:
        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        for message in reversed(list(getattr(self.agent, "messages", []) or [])):
            if str(getattr(message, "id", "") or "").strip() == normalized:
                return message
        return None

    def _consume_research_route_metadata(self, content: str, *, turn_id: str) -> str:
        """Strip the internal route block before any Message can be created."""
        expected = str(
            getattr(self.agent, "_research_route_prompt_expected_turn_id", "") or ""
        ).strip()
        if not expected or expected != str(turn_id or "").strip():
            return str(content or "")
        try:
            from ..research import ResearchRouteEnvelopeParser

            extraction = ResearchRouteEnvelopeParser.extract(content)
            self.agent._pending_research_route_envelope = {
                "turn_id": expected,
                "payload": extraction.payload,
                "found": extraction.found,
                "error": extraction.error,
            }
            return extraction.visible_content
        except Exception:
            # If parsing itself fails, remove a dangling internal block rather
            # than risk displaying it. The route will fall back separately.
            marker = "<tcode-research-route>"
            text = str(content or "")
            position = text.lower().find(marker)
            self.agent._pending_research_route_envelope = {
                "turn_id": expected,
                "payload": None,
                "found": position >= 0,
                "error": "route_parser_failed",
            }
            return text[:position].rstrip() if position >= 0 else text

    def _record_research_fallback_usage(self, response: Any) -> None:
        usage_value = getattr(response, "usage", None)
        total = getattr(self.agent, "total_usage", None)
        if not usage_value or total is None:
            return
        try:
            usage = Usage(**normalize_usage(usage_value))
            total.prompt_tokens += usage.prompt_tokens
            total.completion_tokens += usage.completion_tokens
            total.total_tokens += usage.total_tokens
        except Exception:
            pass

    async def _capture_research_turn_route(
        self,
        *,
        user_message: str,
        turn_id: str,
        allow_model_fallback: bool = True,
    ) -> Optional[Any]:
        """Persist one route per Turn without allowing failures to affect chat."""
        normalized_turn = str(turn_id or "").strip()
        if not normalized_turn or normalized_turn == str(
            getattr(self.agent, "_research_route_committed_turn_id", "") or ""
        ):
            return None
        try:
            session = await self._turn_session()
            if session is None or str(getattr(session, "parent_id", "") or "").strip():
                return None
            metadata = dict(getattr(session, "metadata", {}) or {})
            owner = {
                "tenant_id": str(
                    getattr(session, "tenant_id", "")
                    or metadata.get("tenant_id")
                    or ""
                ).strip(),
                "user_id": str(
                    getattr(session, "user_id", "") or metadata.get("user_id") or ""
                ).strip(),
                "workspace_id": str(
                    getattr(session, "workspace_id", "")
                    or metadata.get("workspace_id")
                    or ""
                ).strip(),
            }
            if not all(owner.values()):
                return None
            workdir = self._event_workdir(session)
            from ..providers.base import ChatMessage
            from ..research import (
                ResearchRouteEnvelopeParser,
                ResearchTurnRouter,
                load_research_settings,
                selected_asset_ids,
            )

            settings = load_research_settings()
            if not settings.routing_active:
                return None
            router = ResearchTurnRouter(
                **owner,
                workdir=workdir,
                settings=settings,
                pinned_asset_ids=selected_asset_ids(
                    metadata.get("research_context")
                ),
            )
            pending = dict(
                getattr(self.agent, "_pending_research_route_envelope", {}) or {}
            )
            payload = (
                pending.get("payload")
                if str(pending.get("turn_id") or "") == normalized_turn
                and isinstance(pending.get("payload"), dict)
                else None
            )
            decision_source = "deterministic"
            if payload is not None:
                try:
                    proposal = router.proposal_from_payload(payload)
                    decision_source = "agent"
                except Exception:
                    proposal = router.deterministic_proposal(user_message)
            else:
                proposal = router.deterministic_proposal(user_message)

            if (
                allow_model_fallback
                and router.should_model_fallback(proposal)
            ):
                calls = dict(
                    getattr(self.agent, "_research_route_model_fallback_calls", {}) or {}
                )
                if int(calls.get(normalized_turn, 0) or 0) < 1:
                    calls[normalized_turn] = 1
                    self.agent._research_route_model_fallback_calls = calls
                    try:
                        fallback_messages = [
                            ChatMessage(role=item["role"], content=item["content"])
                            for item in router.fallback_prompt(user_message)
                        ]
                        response = await asyncio.wait_for(
                            self.agent.provider.chat(
                                messages=fallback_messages,
                                tools=None,
                                temperature=0,
                                max_tokens=300,
                            ),
                            timeout=float(settings.routing_fallback_timeout_seconds),
                        )
                        self._record_research_fallback_usage(response)
                        fallback_payload = ResearchRouteEnvelopeParser.parse_bare_json(
                            str(getattr(response, "content", "") or "")
                        )
                        if fallback_payload is None:
                            raise ValueError("model fallback did not return JSON")
                        proposal = router.proposal_from_payload(fallback_payload)
                        decision_source = "model_fallback"
                    except Exception:
                        proposal = router.fallback_failure_proposal(
                            "model_fallback_failed",
                            base=proposal,
                        )
                        decision_source = "model_fallback"

            route, created = router.persist(
                normalized_turn,
                proposal,
                decision_source=decision_source,
            )
            self.agent._research_route_committed_turn_id = normalized_turn
            self.agent._last_research_turn_route = route.to_dict()
            self.agent._pending_research_route_envelope = {}
            self._append_session_event(
                "research.route",
                payload={
                    "turn_id": normalized_turn,
                    "route_id": route.route_id,
                    "routing_state": route.routing_state,
                    "durability": route.durability,
                    "research_ids": route.research_ids,
                    "imported_scope_ref_count": len(route.imported_scope_refs),
                    "decision_source": route.decision_source,
                    "confidence": route.confidence,
                    "reason_code": route.reason_code,
                    "created": created,
                    "model_fallback_calls": int(
                        getattr(
                            self.agent,
                            "_research_route_model_fallback_calls",
                            {},
                        ).get(normalized_turn, 0)
                    ),
                },
                metadata={"internal": True},
                session=session,
            )
            return route
        except Exception:
            return None

    def _append_research_message_event(
        self,
        message_id: str,
        *,
        turn_id: str,
        trace_id: str,
        session: Any,
        parent_event_id: str = "",
    ) -> Optional[Any]:
        message = self._research_message_by_id(message_id)
        if message is None:
            return None
        try:
            from ..research import ResearchEventInput, ResearchEventSink

            role = str(getattr(message, "role", "") or "system").strip().lower()
            actor_type = "agent" if role == "assistant" else role
            if actor_type not in {"user", "agent", "tool", "system"}:
                actor_type = "system"
            tool_calls = []
            for tool_call in list(getattr(message, "tool_calls", []) or []):
                state_obj = getattr(tool_call, "state", None)
                tool_calls.append(
                    {
                        "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
                        "call_id": str(getattr(tool_call, "call_id", "") or ""),
                        "state": str(getattr(state_obj, "value", state_obj) or ""),
                    }
                )
            return ResearchEventSink.append(
                ResearchEventInput(
                    event_type="message.created",
                    tenant_id=str(getattr(session, "tenant_id", "") or ""),
                    user_id=str(getattr(session, "user_id", "") or ""),
                    workspace_id=str(getattr(session, "workspace_id", "") or ""),
                    session_id=str(getattr(session, "id", "") or ""),
                    turn_id=turn_id,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    actor_type=actor_type,
                    actor_id=(
                        str(getattr(self.agent.agent_info, "name", "") or "")
                        if actor_type == "agent"
                        else str(getattr(session, "user_id", "") or "")
                    ),
                    source_kind="session_message",
                    source_id=str(getattr(message, "id", "") or ""),
                    source_ref=(
                        f"session:{getattr(session, 'id', '')}/message:"
                        f"{getattr(message, 'id', '')}"
                    ),
                    payload={
                        "role": role,
                        "content_preview": str(getattr(message, "content", "") or ""),
                        "parent_id": str(getattr(message, "parent_id", "") or ""),
                        "finish": getattr(message, "finish", None),
                        "error_preview": str(getattr(message, "error", "") or ""),
                        "tool_calls": tool_calls,
                        "internal": bool(getattr(message, "_internal", False)),
                    },
                    privacy_state="private",
                ),
                workdir=self._event_workdir(session),
            )
        except Exception:
            return None

    def _append_research_turn_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        terminal: bool,
        session: Any,
    ) -> None:
        try:
            from ..research import (
                ResearchEventInput,
                ResearchEventSink,
                trace_id_for_turn,
            )

            turn_id = str(snapshot.get("turn_id", "") or "")
            session_id = str(getattr(session, "id", "") or "")
            trace_id = str(
                getattr(self.agent, "_active_research_trace_id", "")
                or trace_id_for_turn(session_id, turn_id)
            )
            user_event = self._append_research_message_event(
                str(snapshot.get("user_message_id", "") or ""),
                turn_id=turn_id,
                trace_id=trace_id,
                session=session,
            )
            parent_event_id = str(getattr(user_event, "event_id", "") or "")
            assistant_ids = [
                str(snapshot.get("assistant_message_id", "") or ""),
                str(snapshot.get("final_message_id", "") or ""),
            ]
            seen_message_ids: set[str] = set()
            for message_id in assistant_ids:
                if not message_id or message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                self._append_research_message_event(
                    message_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    session=session,
                    parent_event_id=parent_event_id,
                )

            state = str(snapshot.get("state", "") or "").strip().lower()
            event_type = ""
            if state == "preparing":
                event_type = "turn.started"
            elif terminal:
                if state in {"completed", "partial", "blocked"}:
                    event_type = "turn.completed"
                elif state == "cancelled":
                    event_type = "turn.cancelled"
                else:
                    event_type = "turn.failed"
            if not event_type:
                return

            ResearchEventSink.append(
                ResearchEventInput(
                    event_type=event_type,
                    tenant_id=str(getattr(session, "tenant_id", "") or ""),
                    user_id=str(getattr(session, "user_id", "") or ""),
                    workspace_id=str(getattr(session, "workspace_id", "") or ""),
                    session_id=session_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    parent_event_id=parent_event_id,
                    actor_type="agent" if terminal else "user",
                    actor_id=(
                        str(getattr(self.agent.agent_info, "name", "") or "")
                        if terminal
                        else str(getattr(session, "user_id", "") or "")
                    ),
                    source_kind="agent_turn",
                    source_id=turn_id,
                    source_ref=f"session:{session_id}/turn:{turn_id}",
                    payload=snapshot,
                    privacy_state="private",
                    stable_state_version=event_type,
                ),
                workdir=self._event_workdir(session),
            )
        except Exception:
            pass

    def _append_session_event(
        self,
        event_type: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        session: Optional[Any] = None,
    ) -> None:
        """向中央 Canonical Session Log 追加一条 Owner 约束事件。"""

        try:
            from ..session.events import SessionEventStore

            SessionEventStore.append(
                session_id=str(
                    getattr(self.agent, "session_id", "")
                    or getattr(session, "id", "")
                    or ""
                ),
                event_type=event_type,
                payload=payload or {},
                metadata=metadata or {},
                owner={
                    "tenant_id": str(getattr(session, "tenant_id", "") or ""),
                    "user_id": str(getattr(session, "user_id", "") or ""),
                    "workspace_id": str(getattr(session, "workspace_id", "") or ""),
                },
                causation_id=str((payload or {}).get("turn_id") or ""),
            )
        except Exception:
            pass

    async def _persist_turn_state(
        self,
        *,
        turn_id: str,
        state: str,
        user_message_id: str = "",
        assistant_message_id: str = "",
        final_message_id: str = "",
        iteration: Optional[int] = None,
        pending_tool_call: Optional[ToolCall | dict[str, Any]] = None,
        pending_tool_calls: Optional[list[ToolCall | dict[str, Any]]] = None,
        extra: Optional[dict[str, Any]] = None,
        error: str = "",
        clear_active: bool = False,
    ) -> None:
        session = await self._turn_session()
        if session is None:
            return

        metadata = dict(getattr(session, "metadata", {}) or {})
        normalized_state = str(state or "").strip().lower()
        self._guidance_lease_phase = normalized_state
        if turn_id:
            try:
                from ..research import trace_id_for_turn

                self.agent._active_research_turn_id = str(turn_id)
                self.agent._active_research_trace_id = trace_id_for_turn(
                    str(getattr(session, "id", "") or ""),
                    str(turn_id),
                )
            except Exception:
                pass

        existing_active = metadata.get(TURN_STATE_METADATA_KEY)
        if isinstance(existing_active, dict):
            user_message_id = user_message_id or str(
                existing_active.get("user_message_id", "") or ""
            )
            assistant_message_id = assistant_message_id or str(
                existing_active.get("assistant_message_id", "") or ""
            )
            if iteration is None and existing_active.get("iteration") is not None:
                try:
                    iteration = int(existing_active.get("iteration"))
                except Exception:
                    iteration = None
            preserve_pending = normalized_state.startswith("waiting_")
            if preserve_pending:
                if pending_tool_call is None and isinstance(
                    existing_active.get("pending_tool_call"), dict
                ):
                    pending_tool_call = dict(existing_active.get("pending_tool_call") or {})
                if pending_tool_calls is None and isinstance(
                    existing_active.get("pending_tool_calls"), list
                ):
                    pending_tool_calls = list(existing_active.get("pending_tool_calls") or [])

        snapshot: dict[str, Any] = {
            "turn_id": str(turn_id or ""),
            "state": normalized_state,
            "agent": str(getattr(self.agent.agent_info, "name", "") or ""),
            "session_id": str(getattr(self.agent, "session_id", "") or ""),
            "user_message_id": str(user_message_id or ""),
            "assistant_message_id": str(assistant_message_id or ""),
            "final_message_id": str(final_message_id or ""),
            "iteration": iteration,
            "message_count": len(getattr(self.agent, "messages", []) or []),
            "updated_at": self.agent._current_time(),
        }
        if pending_tool_call is not None:
            snapshot["pending_tool_call"] = (
                dict(pending_tool_call)
                if isinstance(pending_tool_call, dict)
                else self._tool_call_snapshot(pending_tool_call)
            )
        if pending_tool_calls is not None:
            snapshot["pending_tool_calls"] = [
                dict(item) if isinstance(item, dict) else self._tool_call_snapshot(item)
                for item in pending_tool_calls
            ]
        if extra:
            snapshot["extra"] = dict(extra)
        if error:
            snapshot["error"] = str(error)[:2000]

        terminal = snapshot["state"] in TERMINAL_TURN_STATES or clear_active
        if normalized_state in {"interrupted", "cancelled"} and user_message_id:
            for message in self.agent.messages:
                if message.id == user_message_id and message.role == "user":
                    message.metadata = {**(message.metadata or {}), "turn_interruption": {
                        "turn_id": turn_id, "state": normalized_state,
                    }}
                    break
        if terminal:
            metadata[LAST_TURN_METADATA_KEY] = snapshot
            metadata.pop(TURN_STATE_METADATA_KEY, None)
        else:
            metadata[TURN_STATE_METADATA_KEY] = snapshot

        try:
            session.metadata = metadata
            session.messages = list(getattr(self.agent, "messages", []) or [])
            from ..session.manager import SessionManager

            update_coro = SessionManager.update(
                session.id,
                metadata=metadata,
                messages=list(getattr(self.agent, "messages", []) or []),
                related_events=[
                    {
                        "event_type": "turn.state",
                        "payload": snapshot,
                        "metadata": {"terminal": terminal},
                        "causation_id": str(turn_id or ""),
                        "idempotency_key": (
                            "turn-state:"
                            + hashlib.sha256(
                                json.dumps(
                                    snapshot,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                        ),
                    }
                ],
            )
            timeout_seconds = self._session_persistence_timeout_seconds()
            if timeout_seconds > 0:
                await asyncio.wait_for(update_coro, timeout=timeout_seconds)
            else:
                await update_coro
            self._append_research_turn_snapshot(
                snapshot,
                terminal=terminal,
                session=session,
            )
            if terminal:
                self.agent._active_research_turn_id = ""
                self.agent._active_research_trace_id = ""
        except Exception:
            pass

        port = getattr(self.agent, "_guidance_port", None)
        owner_id = str(getattr(self.agent, "_guidance_owner_id", "") or "")
        if port is not None and owner_id and turn_id:
            try:
                if terminal:
                    await port.clear_turn_owner(
                        session_id=str(getattr(self.agent, "session_id", "") or ""),
                        turn_id=turn_id,
                        owner_id=owner_id,
                    )
                    self._active_guidance_turn_id = ""
                else:
                    await port.heartbeat_turn_owner(
                        session_id=str(getattr(self.agent, "session_id", "") or ""),
                        turn_id=turn_id,
                        owner_id=owner_id,
                        phase=normalized_state,
                        stream_epoch=int(
                            getattr(self.agent, "_guidance_stream_epoch", 0) or 0
                        ),
                    )
            except Exception:
                pass

    async def _persist_completion_fence(self, *, turn_id: str) -> None:
        """Mirror the durable SQLite fence into Session metadata for recovery."""

        session = await self._turn_session()
        if session is None:
            return
        metadata = dict(getattr(session, "metadata", {}) or {})
        active = dict(metadata.get(TURN_STATE_METADATA_KEY) or {})
        if str(active.get("turn_id") or "") != str(turn_id or ""):
            return
        active["completion_fenced"] = True
        active["updated_at"] = self.agent._current_time()
        metadata[TURN_STATE_METADATA_KEY] = active
        try:
            from ..session.manager import SessionManager

            session.metadata = metadata
            await SessionManager.update(session.id, metadata=metadata)
        except Exception:
            pass

    def _has_tool_result_for_call(self, call_id: str) -> bool:
        normalized = str(call_id or "").strip()
        if not normalized:
            return False
        for msg in getattr(self.agent, "messages", []) or []:
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            for tc in getattr(msg, "tool_calls", []) or []:
                if str(getattr(tc, "call_id", "") or "").strip() == normalized:
                    return True
        return False

    def _find_tool_call_in_history(self, call_id: str) -> Optional[ToolCall]:
        normalized = str(call_id or "").strip()
        if not normalized:
            return None
        for msg in reversed(getattr(self.agent, "messages", []) or []):
            for tc in getattr(msg, "tool_calls", []) or []:
                if str(getattr(tc, "call_id", "") or "").strip() == normalized:
                    return tc
        return None

    def _missing_tool_call_snapshots(
        self,
        active_turn: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if isinstance(active_turn.get("pending_tool_calls"), list):
            candidates.extend(
                item
                for item in active_turn.get("pending_tool_calls") or []
                if isinstance(item, dict)
            )
        if isinstance(active_turn.get("pending_tool_call"), dict):
            candidates.append(dict(active_turn.get("pending_tool_call") or {}))

        missing: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            call_id = str(item.get("call_id", "") or "").strip()
            if not call_id or call_id in seen:
                continue
            seen.add(call_id)
            if not self._has_tool_result_for_call(call_id):
                missing.append(dict(item))
        return missing

    def _append_interrupted_tool_result(
        self,
        tool_call: ToolCall,
        *,
        parent_id: str,
        reason: str,
    ) -> Message:
        """写入被中断工具的合成结果，并关闭对应的统一运行时记录。"""

        tool_call.state = ToolState.INTERRUPTED
        tool_call.error = None
        tool_call.output = ""
        tool_call.end_time = self.agent._current_time()
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        metadata.update(
            {
                "synthetic": True,
                "interrupted": True,
                "side_effect_state": str(
                    metadata.get("side_effect_state") or "possible"
                ),
            }
        )
        if metadata.get("guidance_ids") or metadata.get("interrupt_requested"):
            metadata.update(
                {
                    "superseded_by_guidance": True,
                    "semantic_disposition": "superseded_neutral",
                }
            )
        tool_call.metadata = metadata

        tool_result_msg = Message(
            role="tool",
            content=reason,
            id=self.agent._generate_id(),
            parent_id=parent_id,
        )
        tool_result_msg.tool_calls = [tool_call]
        self.agent.messages.append(tool_result_msg)
        self._close_interrupted_tool_runtime(tool_call, reason=reason)
        self._append_session_event(
            "tool.result.synthetic",
            payload={
                "reason": reason,
                "parent_id": parent_id,
                "tool_result_message_id": tool_result_msg.id,
                "tool_call": self._tool_call_snapshot(tool_call),
            },
            metadata={"interrupted": True},
        )
        return tool_result_msg

    def _append_skipped_tool_result(
        self,
        tool_call: ToolCall,
        *,
        parent_id: str,
        guidance_ids: list[str],
        boundary_reason: str = "",
    ) -> Message:
        reason = boundary_reason or "Skipped because newer user guidance superseded this unstarted tool call."
        tool_call.state = ToolState.SKIPPED
        tool_call.output = ""
        tool_call.error = None
        tool_call.end_time = self.agent._current_time()
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        metadata.update(
            {
                "synthetic": True,
                "skipped": True,
                "reason": "superseded_by_guidance",
                "guidance_ids": list(guidance_ids),
                "superseded_by_guidance": True,
                "semantic_disposition": "superseded_neutral",
            }
        )
        tool_call.metadata = metadata
        if boundary_reason:
            for key in ("guidance_ids", "superseded_by_guidance", "semantic_disposition"):
                metadata.pop(key, None)
            metadata.update({"reason": "computer_verification_boundary", "execution_skipped": True})
        result_message = Message(
            role="tool",
            content=reason,
            id=self.agent._generate_id(),
            parent_id=parent_id,
        )
        result_message.tool_calls = [tool_call]
        self.agent.messages.append(result_message)
        self._close_interrupted_tool_runtime(tool_call, reason=reason)
        self._append_session_event(
            "tool.result.synthetic",
            payload={
                "reason": reason,
                "parent_id": parent_id,
                "tool_result_message_id": result_message.id,
                "tool_call": self._tool_call_snapshot(tool_call),
                "guidance_ids": list(guidance_ids),
            },
            metadata={"skipped": True},
        )
        return result_message

    def _neutralize_superseded_turn_evidence(
        self,
        *,
        user_message_id: str,
        guidance_ids: list[str],
    ) -> int:
        """Keep old-goal tool history auditable without counting it for a replacement."""

        started = not bool(str(user_message_id or "").strip())
        changed = 0
        for message in list(getattr(self.agent, "messages", []) or []):
            if str(getattr(message, "id", "") or "") == str(user_message_id or ""):
                started = True
                continue
            if not started:
                continue
            for tool_call in list(getattr(message, "tool_calls", []) or []):
                metadata = dict(getattr(tool_call, "metadata", {}) or {})
                existing_guidance_ids = [
                    str(value or "").strip()
                    for value in list(metadata.get("guidance_ids") or [])
                    if str(value or "").strip()
                ]
                metadata.update(
                    {
                        "superseded_by_guidance": True,
                        "semantic_disposition": "superseded_neutral",
                        "guidance_ids": list(
                            dict.fromkeys(existing_guidance_ids + list(guidance_ids))
                        ),
                    }
                )
                tool_call.metadata = metadata
                changed += 1
        return changed

    async def _claim_pending_guidance(
        self,
        *,
        turn_id: str,
        safe_point: Any,
    ) -> list[Any]:
        port = getattr(self.agent, "_guidance_port", None)
        owner_id = str(getattr(self.agent, "_guidance_owner_id", "") or "")
        if port is None or not owner_id:
            return []
        return await port.claim_for_turn(
            session_id=str(getattr(self.agent, "session_id", "") or ""),
            turn_id=turn_id,
            owner_id=owner_id,
            safe_point=safe_point,
        )

    async def _apply_claimed_guidance(
        self,
        *,
        items: list[Any],
        turn_id: str,
        user_message_id: str,
        iteration: int,
        contract: Any,
        pending_tool_calls: Optional[list[ToolCall]] = None,
        assistant_message_id: str = "",
    ) -> tuple[Any, list[Message]]:
        if not items:
            return contract, []
        port = getattr(self.agent, "_guidance_port", None)
        if port is None:
            return contract, []
        applying = await port.mark_applying(items=items)
        guidance_ids = [item.id for item in applying]
        emitted: list[Message] = []
        try:
            for tool_call in list(pending_tool_calls or []):
                if getattr(tool_call, "state", None) is not ToolState.PENDING:
                    continue
                if self._has_tool_result_for_call(tool_call.call_id):
                    continue
                emitted.append(
                    self._append_skipped_tool_result(
                        tool_call,
                        parent_id=assistant_message_id,
                        guidance_ids=guidance_ids,
                    )
                )

            from ..task.outcome_contract import OutcomeContract
            from ..task.outcome_planner import OutcomePlan
            from ..task.outcome_revision import invalidate_superseded_message_verifications

            revised_contract = contract
            revised_plan = getattr(self.agent, "_active_outcome_plan", None)
            applied_revisions: list[dict[str, Any]] = []
            for item in applying:
                if isinstance(item.outcome_contract, dict):
                    old_contract = revised_contract
                    revision_payload = dict(item.contract_revision or {})
                    revised_contract = OutcomeContract.compile(
                        item.message,
                        supplied=item.outcome_contract,
                    )
                    relationship = str(
                        revision_payload.get("relationship") or ""
                    ).strip().lower()
                    classification = str(
                        revision_payload.get("classification") or ""
                    ).strip().upper()
                    if (
                        relationship in {"replace", "weaken"}
                        or classification == "GOAL_REPLACEMENT"
                    ):
                        self._neutralize_superseded_turn_evidence(
                            user_message_id=user_message_id,
                            guidance_ids=[item.id],
                        )
                    if old_contract is not None:
                        invalidate_superseded_message_verifications(
                            self.agent.messages,
                            contract_id=str(getattr(old_contract, "contract_id", "") or ""),
                            contract_version=int(getattr(old_contract, "version", 0) or 0),
                            superseding_contract=revised_contract,
                        )
                        superseded = dict(
                            getattr(self.agent, "_superseded_outcome_contracts", {}) or {}
                        )
                        superseded[
                            (
                                str(getattr(old_contract, "contract_id", "") or ""),
                                int(getattr(old_contract, "version", 0) or 0),
                            )
                        ] = {
                            "contract_version": revised_contract.version,
                            "contract_id": revised_contract.contract_id,
                            "goal_id": revised_contract.goal_id,
                            "classification": str(
                                (item.contract_revision or {}).get("classification") or ""
                            ),
                        }
                        self.agent._superseded_outcome_contracts = dict(
                            list(superseded.items())[-64:]
                        )
                    proof_rank = {
                        "P0": 0,
                        "P1": 1,
                        "P2": 2,
                        "P3": 3,
                        "P4": 4,
                    }.get(revised_contract.required_proof_level, 0)
                    level = (
                        "V3"
                        if revised_contract.risk_level in {"high", "critical"}
                        or proof_rank >= 3
                        else "V2"
                        if proof_rank >= 2
                        else "V1"
                    )
                    expected_targets = tuple(
                        str(artifact.get("path") or "").strip()
                        for criterion in revised_contract.success_criteria
                        for artifact in criterion.expected_artifacts
                        if str(artifact.get("path") or "").strip()
                    )
                    try:
                        revision_confidence = max(
                            0.0,
                            min(
                                1.0,
                                float(revision_payload.get("confidence") or 0.0),
                            ),
                        )
                    except (TypeError, ValueError):
                        revision_confidence = 0.0
                    revised_plan = OutcomePlan(
                        contract=revised_contract,
                        level=level,
                        source="runtime_guidance",
                        reason_code=str(
                            revision_payload.get("reason")
                            or "runtime_guidance_contract_revision"
                        ),
                        formal_verification=(
                            revised_contract.compatibility_mode == "native_v2"
                        ),
                        confidence=revision_confidence,
                        targets=expected_targets,
                    )
                    applied_revisions.append(
                        {
                            "guidance_id": item.id,
                            "base_contract_id": str(
                                getattr(old_contract, "contract_id", "") or ""
                            ),
                            "base_contract_version": int(
                                getattr(old_contract, "version", 0) or 0
                            ),
                            "contract_id": revised_contract.contract_id,
                            "contract_version": revised_contract.version,
                            "goal_id": revised_contract.goal_id,
                            "classification": str(
                                revision_payload.get("classification") or ""
                            ),
                        }
                    )
                guidance_message = Message(
                    role="user",
                    content=item.message,
                    id=self.agent._generate_id(),
                    metadata={
                        "guidance": {
                            "id": item.id,
                            "sequence": item.sequence,
                            "mode": item.mode.value,
                            "target_turn_id": turn_id,
                            "applied_safe_point": item.claimed_safe_point,
                            "contract_revision_id": str(
                                (item.contract_revision or {}).get("revision_id") or ""
                            ),
                        }
                    },
                )
                self.agent.messages.append(guidance_message)
                emitted.append(guidance_message)

            self.agent._active_outcome_contract = revised_contract
            self.agent._last_outcome_contract = revised_contract.to_dict()
            if revised_plan is not None:
                self.agent._active_outcome_plan = revised_plan
                self.agent._last_outcome_plan = revised_plan.to_dict()
            await self._persist_turn_state(
                turn_id=turn_id,
                state="guidance_applying",
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                iteration=iteration,
                pending_tool_calls=pending_tool_calls,
                extra={
                    "guidance_ids": guidance_ids,
                    "last_applied_guidance_sequence": max(
                        item.sequence for item in applying
                    ),
                    "outcome_contract_revisions": applied_revisions,
                },
            )
            await port.mark_applied(items=applying, effect="replanned")
            return revised_contract, emitted
        except Exception as exc:
            for item in applying:
                try:
                    await port.mark_failed(
                        item=item,
                        failure_code="apply_failed",
                        failure_detail=str(exc),
                    )
                except Exception:
                    pass
            raise

    async def _apply_pending_guidance(
        self,
        *,
        turn_id: str,
        user_message_id: str,
        safe_point: Any,
        iteration: int,
        contract: Any,
        pending_tool_calls: Optional[list[ToolCall]] = None,
        assistant_message_id: str = "",
    ) -> tuple[Any, list[Message]]:
        items = await self._claim_pending_guidance(
            turn_id=turn_id,
            safe_point=safe_point,
        )
        return await self._apply_claimed_guidance(
            items=items,
            turn_id=turn_id,
            user_message_id=user_message_id,
            iteration=iteration,
            contract=contract,
            pending_tool_calls=pending_tool_calls,
            assistant_message_id=assistant_message_id,
        )

    def _close_interrupted_tool_runtime(
        self,
        tool_call: ToolCall,
        *,
        reason: str,
    ) -> None:
        """把未返回结果的 Agent Tool Runtime 转为 cancelled，防止前端残留运行中。"""

        try:
            from ..runtime.task_runtime import (
                TaskRuntimeStore,
                task_runtime_id_for_tool_call,
            )

            session_id = str(getattr(self.agent, "session_id", "") or "").strip()
            call_id = str(getattr(tool_call, "call_id", "") or "").strip()
            if not session_id or not call_id:
                return
            tool_metadata = dict(getattr(tool_call, "metadata", {}) or {})
            skipped = bool(tool_metadata.get("skipped"))
            TaskRuntimeStore.record_event(
                runtime_id=task_runtime_id_for_tool_call(session_id, call_id),
                runtime_kind="agent_tool",
                status="cancelled",
                phase="skipped" if skipped else "interrupted",
                source="guidance" if tool_metadata.get("guidance_ids") else "turn_recovery",
                session_id=session_id,
                title=str(getattr(tool_call, "tool_name", "") or ""),
                external_id=call_id,
                tool_name=str(getattr(tool_call, "tool_name", "") or ""),
                metadata={
                    "interrupted": not skipped,
                    "skipped": skipped,
                    "reason": (
                        "guidance_superseded" if skipped else "guidance_interrupted"
                    ),
                    "interruption_reason": str(reason or "")[:1200],
                    "side_effect_state": str(
                        tool_metadata.get("side_effect_state")
                        or ("none" if skipped else "possible")
                    ),
                    "guidance_ids": list(tool_metadata.get("guidance_ids") or []),
                    "superseded_by_guidance": bool(
                        tool_metadata.get("superseded_by_guidance")
                    ),
                    "semantic_disposition": str(
                        tool_metadata.get("semantic_disposition") or ""
                    ),
                },
                workdir=str(getattr(self.agent, "workdir", "") or "").strip() or None,
            )
        except Exception:
            return

    async def _runtime_task_for_tool_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Resolve a durable runtime task created by a pending tool call."""

        call_id = str(snapshot.get("call_id", "") or "").strip()
        if not call_id:
            return None
        try:
            from ..runtime import RuntimeTaskManager

            record = RuntimeTaskManager.find_by_invocation(
                call_id,
                session_id=str(getattr(self.agent, "session_id", "") or ""),
            )
            if record is None:
                return None
            if str(record.get("worker_session_id", "") or "").strip():
                from ..team.coordinator import reconcile_orphaned_worker_runtime_task

                record = await reconcile_orphaned_worker_runtime_task(record)
            return record
        except Exception:
            return None

    async def _detached_tool_reference(
        self,
        tool_call: ToolCall,
    ) -> dict[str, Any]:
        """Resolve the durable job or child identity before closing a parent wait."""

        call_id = str(getattr(tool_call, "call_id", "") or "").strip()
        session_id = str(getattr(self.agent, "session_id", "") or "").strip()
        owner = getattr(self.agent, "_existing_session", None)
        owner_kwargs = {
            key: str(getattr(owner, key, "") or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
            if str(getattr(owner, key, "") or "").strip()
        }
        for attempt in range(21):
            runtime_task = await self._runtime_task_for_tool_snapshot(
                self._tool_call_snapshot(tool_call)
            )
            if runtime_task:
                task_id = str(
                    runtime_task.get("task_id")
                    or runtime_task.get("runtime_id")
                    or ""
                ).strip()
                child_session_id = str(
                    runtime_task.get("worker_session_id")
                    or runtime_task.get("child_session_id")
                    or ""
                ).strip()
                return {
                    **({"runtime_task_id": task_id} if task_id else {}),
                    **(
                        {"child_session_id": child_session_id}
                        if child_session_id
                        else {}
                    ),
                }

            if call_id and session_id:
                try:
                    from ..session.manager import SessionManager

                    children = await SessionManager.list(
                        parent_id=session_id,
                        **owner_kwargs,
                    )
                    for child in children:
                        child_metadata = dict(
                            getattr(child, "metadata", {}) or {}
                        )
                        scope = child_metadata.get("delegation_scope")
                        if not isinstance(scope, dict):
                            continue
                        if str(scope.get("origin_tool_call_id") or "") != call_id:
                            continue
                        return {
                            "child_session_id": str(
                                getattr(child, "id", "") or ""
                            )
                        }
                except Exception:
                    pass

            for key in (
                "child_session_id",
                "runtime_task_id",
                "task_id",
                "job_id",
                "external_id",
            ):
                value = str(
                    (getattr(tool_call, "metadata", {}) or {}).get(key) or ""
                ).strip()
                if value:
                    return {
                        (
                            "child_session_id"
                            if key == "child_session_id"
                            else "runtime_task_id"
                        ): value
                    }
            if attempt < 20:
                await asyncio.sleep(0.05)
        return {"origin_tool_call_id": call_id} if call_id else {}

    def _append_recovered_runtime_tool_result(
        self,
        tool_call: ToolCall,
        *,
        parent_id: str,
        runtime_task: dict[str, Any],
    ) -> Message:
        """Close an interrupted call with its persisted task state/result."""

        status = str(runtime_task.get("status", "") or "unknown").strip().lower()
        task_id = str(runtime_task.get("task_id", "") or "").strip()
        terminal_error = status in {"failed", "cancelled", "blocked"}
        summary = str(runtime_task.get("result_summary", "") or "").strip()
        if not summary and terminal_error:
            summary = str(runtime_task.get("last_error", "") or "").strip()
        if not summary:
            if status in {"queued", "running", "reviewing", "revising", "recovering"}:
                summary = (
                    f"Delegated runtime task {task_id or '(unknown)'} is still "
                    f"{status}; its persisted execution was recovered."
                )
            elif status in {"waiting_for_user", "awaiting_input"}:
                summary = (
                    f"Delegated runtime task {task_id or '(unknown)'} is waiting "
                    "for parent or user input."
                )
            else:
                summary = (
                    f"Recovered delegated runtime task {task_id or '(unknown)'} "
                    f"with status {status}."
                )

        tool_call.state = ToolState.ERROR if terminal_error else ToolState.COMPLETED
        tool_call.output = "" if terminal_error else summary
        tool_call.error = summary if terminal_error else None
        tool_call.end_time = self.agent._current_time()
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        metadata.update(
            {
                "runtime_recovered": True,
                "runtime_task_id": task_id,
                "runtime_task_status": status,
                "retryable": status in {
                    "queued",
                    "running",
                    "reviewing",
                    "revising",
                    "recovering",
                    "waiting_for_user",
                    "awaiting_input",
                },
            }
        )
        result_contract = runtime_task.get("result_contract")
        if isinstance(result_contract, dict):
            metadata["subtask_handoff"] = dict(result_contract)
        tool_call.metadata = metadata

        tool_result_msg = Message(
            role="tool",
            content=summary,
            id=self.agent._generate_id(),
            parent_id=parent_id,
        )
        tool_result_msg.tool_calls = [tool_call]
        self.agent.messages.append(tool_result_msg)
        self._append_session_event(
            "tool.result.recovered",
            payload={
                "parent_id": parent_id,
                "tool_result_message_id": tool_result_msg.id,
                "runtime_task_id": task_id,
                "runtime_task_status": status,
                "tool_call": self._tool_call_snapshot(tool_call),
            },
            metadata={"runtime_recovered": True},
        )
        return tool_result_msg

    async def _recover_incomplete_turn_if_needed(self) -> None:
        session = await self._turn_session()
        if session is None:
            return
        active_turn = dict(
            (getattr(session, "metadata", {}) or {}).get(TURN_STATE_METADATA_KEY) or {}
        )
        if not active_turn:
            return

        state = str(active_turn.get("state", "") or "").strip().lower()
        if state in TERMINAL_TURN_STATES:
            await self._persist_turn_state(
                turn_id=str(active_turn.get("turn_id", "") or ""),
                state=state,
                clear_active=True,
            )
            return

        reason = (
            "Previous turn was interrupted before this tool call produced a "
            "recorded result."
        )
        if (
            state in {"assistant_ready", "executing_tool", "tool_result_recorded"}
            or state.startswith("waiting_")
        ):
            parent_id = str(active_turn.get("assistant_message_id", "") or "")
            for snapshot in self._missing_tool_call_snapshots(active_turn):
                call_id = str(snapshot.get("call_id", "") or "").strip()
                tool_call = self._find_tool_call_in_history(call_id)
                if tool_call is None:
                    tool_call = self._tool_call_from_snapshot(snapshot)
                runtime_task = await self._runtime_task_for_tool_snapshot(snapshot)
                if runtime_task is not None:
                    self._append_recovered_runtime_tool_result(
                        tool_call,
                        parent_id=parent_id,
                        runtime_task=runtime_task,
                    )
                else:
                    self._append_interrupted_tool_result(
                        tool_call,
                        parent_id=parent_id,
                        reason=reason,
                    )

        await self._persist_turn_state(
            turn_id=str(active_turn.get("turn_id", "") or ""),
            state="interrupted",
            user_message_id=str(active_turn.get("user_message_id", "") or ""),
            assistant_message_id=str(active_turn.get("assistant_message_id", "") or ""),
            iteration=active_turn.get("iteration"),
            error=f"Recovered incomplete turn from state: {state or 'unknown'}",
            clear_active=True,
        )

    async def mark_waiting_for_user(
        self,
        wait_kind: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        session = await self._turn_session()
        if session is None:
            return
        active_turn = dict(
            (getattr(session, "metadata", {}) or {}).get(TURN_STATE_METADATA_KEY) or {}
        )
        if not active_turn:
            return
        self._waiting_previous_turn_state = active_turn
        wait_label = str(wait_kind or "user").strip().lower() or "user"
        await self._persist_turn_state(
            turn_id=str(active_turn.get("turn_id", "") or ""),
            state=f"waiting_{wait_label}",
            user_message_id=str(active_turn.get("user_message_id", "") or ""),
            assistant_message_id=str(active_turn.get("assistant_message_id", "") or ""),
            iteration=active_turn.get("iteration"),
            pending_tool_call=active_turn.get("pending_tool_call"),
            pending_tool_calls=active_turn.get("pending_tool_calls"),
            extra={"wait_kind": wait_label, "payload": dict(payload or {})},
        )
        port = getattr(self.agent, "_guidance_port", None)
        owner_id = str(getattr(self.agent, "_guidance_owner_id", "") or "")
        if port is not None and owner_id:
            try:
                await port.defer_pending_for_wait(
                    session_id=str(getattr(self.agent, "session_id", "") or ""),
                    turn_id=str(active_turn.get("turn_id", "") or ""),
                    owner_id=owner_id,
                    reason=wait_label,
                )
            except Exception:
                pass

    async def mark_waiting_for_user_done(self) -> None:
        previous = getattr(self, "_waiting_previous_turn_state", None)
        if not isinstance(previous, dict) or not previous:
            return
        self._waiting_previous_turn_state = None
        await self._persist_turn_state(
            turn_id=str(previous.get("turn_id", "") or ""),
            state=str(previous.get("state", "") or "executing_tool"),
            user_message_id=str(previous.get("user_message_id", "") or ""),
            assistant_message_id=str(previous.get("assistant_message_id", "") or ""),
            iteration=previous.get("iteration"),
            pending_tool_call=previous.get("pending_tool_call"),
            pending_tool_calls=previous.get("pending_tool_calls"),
        )
        port = getattr(self.agent, "_guidance_port", None)
        if port is not None:
            try:
                await port.release_deferred_for_turn(
                    session_id=str(getattr(self.agent, "session_id", "") or ""),
                    turn_id=str(previous.get("turn_id", "") or ""),
                )
            except Exception:
                pass

    def _compaction_state_extra(self) -> dict[str, Any]:
        session = getattr(self.agent, "_existing_session", None)
        return {
            "pre_compaction_revision": int(
                getattr(session, "current_revision", 0) or 0
            ),
            "pre_compaction_message_count": len(
                getattr(self.agent, "messages", []) or []
            ),
        }

    def _collect_downloaded_pdf_paths(self, *, turn_start_index: int) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for msg in self.agent.messages[turn_start_index:]:
            if str(getattr(msg, "role", "") or "") != "tool":
                continue
            for tool_call in getattr(msg, "tool_calls", []) or []:
                metadata = dict(getattr(tool_call, "metadata", {}) or {})
                raw_paths = metadata.get("downloaded_pdf_paths")
                if isinstance(raw_paths, str):
                    candidates = [raw_paths]
                elif isinstance(raw_paths, list):
                    candidates = raw_paths
                else:
                    candidates = []
                for item in candidates:
                    normalized = str(item or "").strip()
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    paths.append(normalized)
        return paths

    @staticmethod
    def _contract_requires_json(contract: Any = None) -> bool:
        for deliverable in getattr(contract, "deliverables", []) or []:
            if bool(getattr(deliverable, "required", True)) and str(
                getattr(deliverable, "kind", "") or ""
            ).strip().lower() == "json":
                return True
        return False

    def _append_downloaded_pdf_paths(
        self,
        content: str,
        *,
        turn_start_index: int,
        contract: Any = None,
    ) -> str:
        if (
            isinstance(self.agent._output_format, dict)
            and self.agent._output_format.get("type") == "json_schema"
        ):
            return content
        if self._contract_requires_json(contract):
            return str(content or "").strip()

        downloaded_paths = self._collect_downloaded_pdf_paths(
            turn_start_index=turn_start_index,
        )
        if not downloaded_paths:
            return content

        base = str(content or "").strip()
        missing_paths = [path for path in downloaded_paths if path not in base]
        if not missing_paths:
            return base

        path_lines: list[str] = []
        if len(missing_paths) == 1:
            path_lines.append(f"下载完成的 PDF 绝对路径: {missing_paths[0]}")
        else:
            path_lines.append("下载完成的 PDF 绝对路径:")
            for path in missing_paths[:10]:
                path_lines.append(f"- {path}")
            if len(missing_paths) > 10:
                path_lines.append(
                    f"- 其余 {len(missing_paths) - 10} 个路径见 candidate_papers.json"
                )

        if not base:
            return "\n".join(path_lines)
        return f"{base}\n\n" + "\n".join(path_lines)

    async def _maybe_intercept_duplicate_tool_retry(
        self,
        *,
        user_message: str,
        tool_call: ToolCall,
        turn_start_index: int,
    ) -> Optional[ToolResult]:
        if tool_call.tool_name == "task":
            return None  # Task retries use the per-task limit below.
        previous_failures = self._matching_failed_tool_calls(
            tool_call,
            turn_start_index=turn_start_index,
        )
        if not previous_failures:
            return None

        latest_failure = previous_failures[-1]
        latest_metadata = dict(getattr(latest_failure, "metadata", {}) or {})
        recovery = dict(latest_metadata.get("recovery") or {})
        if (
            is_computer_tool(tool_call.tool_name)
            and recovery.get("action") == "observe_then_replan"
            and self._computer_observed_after_failure(
                latest_failure,
                turn_start_index=turn_start_index,
            )
        ):
            return None
        review = latest_metadata.get("retry_review")
        decision = (
            str(review.get("decision", "") or "").strip().lower()
            if isinstance(review, dict)
            else ""
        )
        if not decision:
            from ..review.loop import run_tool_retry_review
            from ..server.auth import session_result_verification_settings

            retry_settings = await session_result_verification_settings(await self._turn_session())

            review = await run_tool_retry_review(
                use_model=retry_settings["enabled"],
                verification_mode=retry_settings["mode"],
                workdir=self.agent.workdir or "",
                parent_session_id=self.agent.session_id,
                user_message=user_message,
                tool_name=tool_call.tool_name,
                tool_arguments=dict(tool_call.arguments or {}),
                attempt_count=len(previous_failures) + 1,
                previous_error=str(getattr(latest_failure, "error", "") or ""),
                previous_output=str(getattr(latest_failure, "output", "") or ""),
                previous_metadata=latest_metadata,
                tool_trace=self._tool_trace_for_review(
                    turn_start_index=turn_start_index,
                ),
            )
            decision = str(review.get("decision", "") or "").strip().lower()

        from ..review.loop import enforce_tool_retry_policy

        review = enforce_tool_retry_policy(
            review if isinstance(review, dict) else {},
            tool_name=tool_call.tool_name,
            previous_error=str(getattr(latest_failure, "error", "") or ""),
            previous_output=str(getattr(latest_failure, "output", "") or ""),
        )
        decision = str(review.get("decision", "") or "").strip().lower()

        if decision == "retry":
            return None

        allowed_agents = [
            str(item or "").strip()
            for item in (latest_metadata.get("allowed_agents") or [])
            if str(item or "").strip()
        ]
        summary = str((review or {}).get("summary") or "").strip()
        next_step = str((review or {}).get("recommended_next_step") or "").strip()
        previous_error = str(getattr(latest_failure, "error", "") or "").strip()
        lines = [
            (
                f"检测到 `{tool_call.tool_name}` 已用相同参数失败 "
                f"{len(previous_failures)} 次，已阻止再次原样调用。"
            )
        ]
        if summary:
            lines.append(f"审查结论: {summary}")
        if next_step:
            lines.append(f"建议动作: {next_step}")
        if previous_error:
            lines.append(f"上一次错误: {previous_error[:800]}")
        if allowed_agents:
            lines.append(f"allowed_agents: {', '.join(allowed_agents)}")
        content = "\n".join(lines)
        metadata = {
            "duplicate_retry_blocked": True,
            "retry_review": dict(review or {}),
            "review_decision": decision or "revise",
            "previous_attempt_count": len(previous_failures),
            "should_end_turn": True,
            "recovery_required": (decision or "revise") in {"delegate", "revise"},
            "execution_skipped": True,
            "execution_environment": {
                "location": "not_executed",
                "label": "Not executed",
                "sandboxed": False,
            },
        }
        if allowed_agents:
            metadata["allowed_agents"] = allowed_agents
        return self._apply_synthetic_tool_result(
            tool_call,
            ToolResult(
                title=tool_call.tool_name,
                output=content,
                error=content,
                metadata=metadata,
            ),
        )

    async def _finalize_turn(
        self,
        *,
        user_message: str,
        final_message: Message,
        turn_messages: list[Message],
        turn_id: str = "",
        user_message_id: str = "",
        completion_state: str = "completed",
    ) -> Message:
        from ..research.citations import repair_knowledge_delimiters
        final_message.content, citation_repairs = repair_knowledge_delimiters(final_message.content, self.agent.messages)
        if citation_repairs:
            final_message.metadata['citation_format_repairs'] = citation_repairs
        normalized_completion = str(completion_state or "completed").strip().lower()
        if normalized_completion not in TERMINAL_TURN_STATES:
            normalized_completion = "completed"
        self.agent._last_outcome_status = normalized_completion
        if turn_id:
            await self._capture_research_turn_route(
                user_message=user_message,
                turn_id=turn_id,
                allow_model_fallback=False,
            )
        grounding = dict(
            getattr(self.agent, "_last_research_grounding", {}) or {}
        )
        grounding_sources = list(grounding.get("sources") or [])
        for message in turn_messages:
            for tool_call in list(getattr(message, "tool_calls", []) or []):
                metadata = dict(getattr(tool_call, "metadata", {}) or {})
                tool_grounding = metadata.get("research_grounding")
                if isinstance(tool_grounding, dict):
                    grounding_sources.extend(list(tool_grounding.get("sources") or []))
        if grounding or grounding_sources:
            deduped_sources: list[dict[str, Any]] = []
            seen_sources: set[tuple[str, str, str]] = set()
            for source in grounding_sources:
                if not isinstance(source, dict):
                    continue
                marker = (
                    str(source.get("asset_id") or ""),
                    str(source.get("object_id") or ""),
                    str(source.get("chunk_id") or ""),
                )
                if marker in seen_sources:
                    continue
                seen_sources.add(marker)
                deduped_sources.append(dict(source))
            grounding["schema"] = "tcode.research-grounding.v1"
            grounding["sources"] = deduped_sources[:24]
            final_message.research_grounding = grounding
        self._attach_presented_content_blocks(final_message, turn_messages)

        from ..runtime.evidence_ledger import EvidenceLedger
        from ..runtime.proof_pack import proof_pack_enabled

        active_contract = getattr(self.agent, "_active_outcome_contract", None)
        if (
            proof_pack_enabled()
            and active_contract is not None
            and not dict(getattr(self.agent, "_last_proof_pack", {}) or {})
        ):
            validation_payload = dict(
                getattr(self.agent, "_last_outcome_validation", {}) or {}
            )
            validation_payload.setdefault(
                "verdict",
                {
                    "completed": "pass",
                    "partial": "partial",
                    "blocked": "blocked",
                    "failed": "partial",
                    "cancelled": "blocked",
                }.get(normalized_completion, "partial"),
            )
            validation_payload.setdefault("validator", "terminal_fallback")
            proof_payload = self._store_proof_pack(
                content=str(getattr(final_message, "content", "") or ""),
                contract=active_contract,
                validation=validation_payload,
                ledger=EvidenceLedger.from_messages(turn_messages),
            )
            validation_payload["proof_pack"] = proof_payload
            self.agent._last_outcome_validation = validation_payload

        from ..guard import public_outcome_projection

        validation_payload = dict(
            getattr(self.agent, "_last_outcome_validation", {}) or {}
        )
        if not validation_payload:
            plan = getattr(self.agent, "_active_outcome_plan", None)
            validation_payload = {
                "verdict": (
                    "pass" if normalized_completion == "completed" else normalized_completion
                ),
                "verification_status": (
                    "NOT_REQUIRED"
                    if plan is not None
                    and not bool(getattr(plan, "formal_verification", False))
                    else (
                        "PARTIAL"
                        if normalized_completion in {"partial", "failed"}
                        else "UNVERIFIED"
                    )
                ),
                "confidence": 0.5 if normalized_completion == "completed" else 0.0,
            }
            self.agent._last_outcome_validation = validation_payload
        public_verification = public_outcome_projection(validation_payload)
        if active_contract is not None:
            public_verification.update(
                {
                    "contract_id": str(
                        getattr(active_contract, "contract_id", "") or ""
                    ),
                    "contract_version": int(
                        getattr(active_contract, "version", 0) or 0
                    ),
                }
            )
            superseded_by = dict(
                getattr(self.agent, "_superseded_outcome_contracts", {}) or {}
            ).get(
                (
                    public_verification["contract_id"],
                    public_verification["contract_version"],
                )
            )
            if isinstance(superseded_by, dict):
                superseding_contract = dict(superseded_by)
                public_verification.update(
                    {
                        "verification_status": "UNVERIFIED",
                        "confidence": 0.0,
                        "superseded": True,
                        "superseded_by_contract_version": int(
                            superseding_contract.get("contract_version", 0) or 0
                        ),
                    }
                )
                if superseding_contract.get("contract_id"):
                    public_verification["superseded_by_contract_id"] = str(
                        superseding_contract["contract_id"]
                    )
                if superseding_contract.get("goal_id"):
                    public_verification["superseded_by_goal_id"] = str(
                        superseding_contract["goal_id"]
                    )
        final_message.verification = public_verification

        # Hook: agent.response
        response_content = str(getattr(final_message, "content", "") or "")[:500]
        self.agent._emit_agent_hook("agent.response", {
            "agent": self.agent.agent_info.name,
            "session_id": self.agent.session_id,
            "response_preview": response_content,
            "turn_count": len(turn_messages),
        })

        try:
            LearningManager.capture_turn(
                agent=self.agent,
                user_message=user_message,
                final_message=final_message,
                turn_messages=turn_messages,
            )
        except Exception as exc:
            print(f"[LEARNING] capture_turn failed: {exc}")

        if self.agent._existing_session:
            self.agent._existing_session.messages = list(self.agent.messages)
        if turn_id:
            await self._persist_turn_state(
                turn_id=turn_id,
                state=normalized_completion,
                user_message_id=user_message_id,
                final_message_id=final_message.id,
                extra={
                    "outcome_status": str(
                        getattr(self.agent, "_last_outcome_status", "") or ""
                    ),
                    "outcome_validation": dict(
                        getattr(self.agent, "_last_outcome_validation", {}) or {}
                    ),
                    "outcome_contract": dict(
                        getattr(self.agent, "_last_outcome_contract", {}) or {}
                    ),
                    "outcome_plan": dict(
                        getattr(self.agent, "_last_outcome_plan", {}) or {}
                    ),
                    "proof_pack": dict(
                        getattr(self.agent, "_last_proof_pack", {}) or {}
                    ),
                    "research_route": dict(
                        getattr(self.agent, "_last_research_turn_route", {}) or {}
                    ),
                    "research_grounding": dict(
                        getattr(final_message, "research_grounding", {}) or {}
                    ),
                    "research_route_model_fallback_calls": int(
                        getattr(
                            self.agent,
                            "_research_route_model_fallback_calls",
                            {},
                        ).get(turn_id, 0)
                    ),
                },
                clear_active=True,
            )
        return final_message

    async def run(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> Message:
        """通过唯一 Turn 状态机完成非流式调用并返回最终消息。

        非流式入口只负责消费 canonical message stream，不再复制模型、工具、
        Checkpoint 或 Finalize 循环。
        """

        driver = TurnDriver(self._drive_turn_with_lease)
        return await driver.complete(
            user_message,
            output_format,
            outcome_contract,
        )

    async def run_stream(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[Message]:
        """通过唯一 Turn 状态机逐条投影流式消息。"""

        driver = TurnDriver(self._drive_turn_with_lease)
        async for message in driver.stream(
            user_message,
            output_format,
            outcome_contract,
        ):
            yield message

    async def _drive_turn_with_lease(self, *args, **kwargs) -> AsyncIterator[Message]:
        """Keep an owned turn alive across preparation and provider/tool waits."""
        from contextlib import aclosing
        await self._emit_llm_progress("preparing", iteration=0)
        port = getattr(self.agent, "_guidance_port", None)
        owner_id = str(getattr(self.agent, "_guidance_owner_id", "") or "")
        async def renew():
            while True:
                await asyncio.sleep(5)
                turn_id = str(getattr(self, "_active_guidance_turn_id", "") or "")
                if not turn_id:
                    continue
                try:
                    await port.heartbeat_turn_owner(
                        session_id=str(getattr(self.agent, "session_id", "") or ""),
                        turn_id=turn_id, owner_id=owner_id,
                        phase=str(getattr(self, "_guidance_lease_phase", "preparing")),
                        stream_epoch=int(getattr(self.agent, "_guidance_stream_epoch", 0) or 0),
                    )
                except Exception:
                    pass  # Ownership and completion checks still reject lost turns.
        heartbeat = asyncio.create_task(renew()) if port is not None and owner_id else None
        try:
            async with aclosing(self._drive_turn(*args, **kwargs)) as stream:
                async for message in stream:
                    yield message
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            # Preparation/stream closure can exit before terminal persistence.
            # Release only this turn's exact owner; never clear another run.
            turn_id = str(getattr(self, "_active_guidance_turn_id", "") or "")
            if port is not None and owner_id and turn_id:
                try:
                    await port.clear_turn_owner(
                        session_id=str(getattr(self.agent, "session_id", "") or ""),
                        turn_id=turn_id, owner_id=owner_id,
                    )
                    self._active_guidance_turn_id = ""
                except Exception:
                    pass  # The existing lease expiry remains the fallback.

    async def _prepare_turn(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]],
        outcome_contract: Optional[dict[str, Any]],
    ) -> tuple[Message, int, OutcomeContract]:
        """Reset turn state, recover prior work and persist the new user message.

        Agent fields remain the public runtime projection used by tools and
        guidance. This phase owns their initial values for the new turn.
        """
        from ..task.outcome_planner import OutcomeContractPlanner
        from ..runtime.workspace_registry import bind_plugin_workspace
        # Lightweight adapters and tests may not own a workspace.  Binding an
        # empty scope is valid and keeps process-global registries isolated.
        bind_plugin_workspace(getattr(self.agent, "workdir", ""))

        self.agent._output_format = output_format
        next_user_metadata: dict[str, Any] = {}
        next_user_message_id = ""
        consume_user_context = getattr(
            self.agent, "consume_next_user_message_context", None
        )
        if callable(consume_user_context):
            next_user_metadata, next_user_message_id = consume_user_context()
        self.agent._active_async_completion_receipt = dict(
            next_user_metadata.get("async_completion_receipt") or {}
        )
        from ..server.auth import session_result_verification_settings
        verification_settings = await session_result_verification_settings(await self._turn_session())
        outcome_plan = await OutcomeContractPlanner.plan_turn(
            user_message,
            output_format=output_format,
            supplied=outcome_contract,
            workspace_root=str(getattr(self.agent, "workdir", "") or ""),
            provider=self.agent.provider if verification_settings["mode"] != "light" else None,
            verification_enabled=verification_settings["enabled"],
            usage_session=await self._turn_session(),
        )
        contract = outcome_plan.contract
        contract.metadata["verification_mode"] = verification_settings["mode"]
        from ..guard.outcome_guard import verification_repair_limit
        contract.max_repair_attempts = min(contract.max_repair_attempts, verification_repair_limit(verification_settings["mode"]))
        self.agent._active_outcome_contract = contract
        self.agent._active_outcome_plan = outcome_plan
        self.agent._last_outcome_status = ""
        self.agent._last_outcome_validation = {}
        self.agent._last_outcome_contract = contract.to_dict()
        self.agent._last_outcome_plan = outcome_plan.to_dict()
        self.agent._last_proof_pack = {}
        if self.agent._abort_event.is_set():
            self.agent._abort_event.clear()
        await self._recover_incomplete_turn_if_needed()
        turn_start_index = len(self.agent.messages)

        user_msg = Message(
            role="user",
            content=user_message,
            id=next_user_message_id or self.agent._generate_id(),
            input_images=(
                self.agent.consume_next_user_input_images()
                if callable(getattr(self.agent, "consume_next_user_input_images", None))
                else []
            ),
            metadata=next_user_metadata,
        )
        turn_id = f"turn-{user_msg.id}"
        computer_task_text = getattr(self.agent, "_next_computer_task_text", "")
        self.agent._next_computer_task_text = ""
        self.agent._computer_task_intent = (
            {"task_id": self.agent.session_id, "turn_id": turn_id,
             "user_message_id": user_msg.id, "text": user_message}
            if computer_task_text and computer_task_text == user_message else None
        )
        self._active_guidance_turn_id = turn_id
        self.agent._research_route_prompt_expected_turn_id = ""
        self.agent._pending_research_route_envelope = {}
        self.agent._research_route_committed_turn_id = ""
        self.agent._last_research_turn_route = {}
        self.agent._last_research_grounding = {}
        self.agent._research_route_model_fallback_calls = {turn_id: 0}
        self.agent.messages.append(user_msg)
        guidance_port = getattr(self.agent, "_guidance_port", None)
        guidance_owner_id = str(
            getattr(self.agent, "_guidance_owner_id", "") or ""
        )
        if guidance_port is not None and guidance_owner_id:
            await guidance_port.register_turn_owner(
                session_id=str(getattr(self.agent, "session_id", "") or ""),
                turn_id=turn_id,
                owner_id=guidance_owner_id,
                phase="preparing",
            )
        from ..guard import deterministic_verifier_enabled, outcome_guard_enabled
        from ..runtime.proof_pack import proof_pack_enabled

        if (
            outcome_guard_enabled()
            or deterministic_verifier_enabled()
            or proof_pack_enabled()
        ):
            self._append_session_event(
                "outcome.contract.created",
                payload={
                    "turn_id": turn_id,
                    "contract": contract.to_v2_dict(),
                    "plan": outcome_plan.to_dict(),
                },
                metadata={
                    "contract_id": contract.contract_id,
                    "goal_id": contract.goal_id,
                    "proof_level": contract.proof_level,
                    "required_proof_level": contract.required_proof_level,
                },
            )
        await self._persist_turn_state(
            turn_id=turn_id,
            state="preparing",
            user_message_id=user_msg.id,
            extra={
                "outcome_contract": contract.to_dict(),
                "outcome_plan": outcome_plan.to_dict(),
            },
        )
        return user_msg, turn_start_index, contract

    async def _compact_turn_context(
        self, *, turn_id: str, user_message_id: str, iteration: int,
    ) -> None:
        """Compact context and persist the terminal checkpoint if it fails."""
        await self._persist_turn_state(
            turn_id=turn_id,
            state="compacting",
            user_message_id=user_message_id,
            iteration=iteration,
            extra=self._compaction_state_extra(),
        )
        try:
            await self.agent._check_auto_compact()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self._persist_turn_state(
                turn_id=turn_id,
                state="interrupted",
                user_message_id=user_message_id,
                iteration=iteration,
                error="Interrupted while compacting context.",
                clear_active=True,
            )
            raise
        except Exception as exc:
            await self._persist_turn_state(
                turn_id=turn_id,
                state="failed",
                user_message_id=user_message_id,
                iteration=iteration,
                error=str(exc),
                clear_active=True,
            )
            raise

    async def _record_assistant_response(
        self, *, response_content: str, tool_calls: Any, model_response: Any,
        stream_message_id: str, streamed: bool, turn_id: str,
        user_message_id: str, iteration: int,
    ) -> Message:
        """Project model output, advance usage counters and persist the message."""
        parsed_tool_calls = []
        if tool_calls:
            parsed_tool_calls = [
                self.agent._parse_tool_call(tc) for tc in tool_calls
            ]
            if not str(response_content or "").strip():
                actions = [self._tool_public_action(tool_call) for tool_call in parsed_tool_calls]
                response_content = "\n\n".join(actions)
        presentation = (
            "commentary"
            if parsed_tool_calls and str(response_content or "").strip()
            else "final"
        )
        if streamed or presentation == "commentary":
            await self._emit_model_output_event(
                status="completed",
                iteration=iteration,
                message_id=stream_message_id,
                content=response_content,
                presentation=presentation,
            )

        # 统一入口允许最小合同测试桩省略 Usage；真实 Agent 仍使用累计值。
        total_usage = getattr(self.agent, "total_usage", None) or Usage()
        turn_usage = Usage(
            prompt_tokens=total_usage.prompt_tokens
            - getattr(self.agent, "_prev_prompt_tokens", 0),
            completion_tokens=total_usage.completion_tokens
            - getattr(self.agent, "_prev_completion_tokens", 0),
            total_tokens=total_usage.total_tokens
            - getattr(self.agent, "_prev_total_tokens", 0),
        )

        assistant_msg = Message(
            role="assistant",
            content=response_content,
            tool_calls=parsed_tool_calls,
            id=stream_message_id,
            parent_id=user_message_id,
            usage=turn_usage if turn_usage.total_tokens > 0 else None,
            presentation=(
                "commentary"
                if parsed_tool_calls and str(response_content or "").strip()
                else None
            ),
            metadata={
                "stream_epoch": int(
                    getattr(self.agent, "_guidance_stream_epoch", 0) or 0
                ),
                **({"reasoning_content": model_response.reasoning_content}
                   if getattr(model_response, "reasoning_content", None) is not None else {}),
                **({"reasoning_blocks": model_response.reasoning_blocks}
                   if getattr(model_response, "reasoning_blocks", None) else {}),
            },
        )

        self.agent._prev_prompt_tokens = total_usage.prompt_tokens
        self.agent._prev_completion_tokens = (
            total_usage.completion_tokens
        )
        self.agent._prev_total_tokens = total_usage.total_tokens

        self.agent.messages.append(assistant_msg)
        await self._persist_turn_state(
            turn_id=turn_id,
            state="assistant_ready" if tool_calls else "finalizing",
            user_message_id=user_message_id,
            assistant_message_id=assistant_msg.id,
            iteration=iteration,
            pending_tool_calls=assistant_msg.tool_calls if tool_calls else None,
        )
        return assistant_msg

    async def _append_public_tool_progress(
        self, tool_call: ToolCall, *, assistant_msg: Message,
        turn_start_index: int, turn_id: str, user_message_id: str, iteration: int,
    ) -> bool:
        """Fill missing public commentary from explicit facts in the same message.

        Workflow owns its cumulative reply. Ordinary model commentary remains
        primary; unknown outputs are not summarized or sent to another model.
        """
        if dict(getattr(self.agent, "_active_tool_trace_context", {}) or {}).get("workflow_run_id"):
            return False
        from ..presentation.public_progress import tool_public_progress
        updates = tool_public_progress(tool_call)
        known = [entry for message in self.agent.messages[turn_start_index:]
                 for entry in dict(getattr(message, "metadata", {}) or {}).get("public_commentary", [])
                 if isinstance(entry, dict)]
        updates = [entry for entry in updates if not any(prior.get("topic") == entry.get("topic") for prior in known)]
        if not updates or len(known) >= 6:
            return False
        updates = updates[:2]
        commentary_key = json.dumps(
            [assistant_msg.id, str(tool_call.call_id or ""), [entry.get("topic") for entry in updates]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        commentary = Message(
            role="assistant",
            content="\n\n".join(entry["text"] for entry in updates),
            id="commentary-" + hashlib.sha256(commentary_key.encode()).hexdigest()[:24],
            parent_id=assistant_msg.id,
            presentation="commentary",
            metadata={"public_commentary": updates},
        )
        self.agent.messages.append(commentary)
        await self._persist_turn_state(
            turn_id=turn_id, state="tool_result_recorded", user_message_id=user_message_id,
            assistant_message_id=commentary.id, iteration=iteration,
        )
        await self._emit_model_output_event(
            status="completed", iteration=iteration, message_id=commentary.id,
            content=commentary.content, presentation="commentary",
        )
        return True

    async def _execute_turn_tool(
        self, tool_call: ToolCall, *, assistant_msg: Message,
        turn_id: str, user_message_id: str, iteration: int,
    ) -> ToolResult:
        """Execute a tool and record interruption before propagating cancellation."""
        if tool_call.start_time is None:
            tool_call.start_time = self.agent._current_time()
        tool_call.state = ToolState.RUNNING
        await self._persist_turn_state(
            turn_id=turn_id,
            state="executing_tool",
            user_message_id=user_message_id,
            assistant_message_id=assistant_msg.id,
            iteration=iteration,
            pending_tool_call=tool_call,
            pending_tool_calls=assistant_msg.tool_calls,
        )
        try:
            result = await self._execute_tool_with_heartbeat(
                tool_call,
                iteration=iteration,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            reason = "Tool execution was interrupted before a result was recorded."
            if not self._has_tool_result_for_call(tool_call.call_id):
                self._append_interrupted_tool_result(
                    tool_call,
                    parent_id=assistant_msg.id,
                    reason=reason,
                )
            await self._persist_turn_state(
                turn_id=turn_id,
                state="interrupted",
                user_message_id=user_message_id,
                assistant_message_id=assistant_msg.id,
                iteration=iteration,
                pending_tool_call=tool_call,
                pending_tool_calls=assistant_msg.tool_calls,
                error=reason,
                clear_active=True,
            )
            raise
        return result

    async def _check_final_answer_guards(
        self, assistant_msg: Message, *, turn_start_index: int,
        turn_id: str, user_message_id: str, iteration: int,
    ) -> Optional[Message]:
        """Record missing-evidence warnings without delaying final delivery."""
        from ..guard import (
            check_thin_trace, check_hallucinated_tool_refs,
            check_incomplete_paper_json, guard_enabled,
        )
        from ..tools.capabilities import _CAPABILITY_MAP

        if not guard_enabled():
            return None
        turn_msgs = self.agent.messages[turn_start_index:]
        should_block, hint = check_thin_trace(turn_msgs, _CAPABILITY_MAP)
        if not should_block:
            should_block, hint = check_incomplete_paper_json(
                assistant_msg.content, turn_msgs,
            )
        if should_block:
            # Missing result evidence is advisory; execution permissions and input
            # validation remain enforced at their own boundaries.
            assistant_msg.metadata = {**(assistant_msg.metadata or {}), "validation_warnings": [hint]}
            self._append_session_event("turn.final_answer_warning", payload={"warning": hint})
            return None
        warning = check_hallucinated_tool_refs(
            final_content=assistant_msg.content, turn_messages=turn_msgs,
            all_tool_ids=set(_CAPABILITY_MAP.keys()),
        )
        if warning:
            if (
                isinstance(self.agent._output_format, dict)
                and self.agent._output_format.get("type") == "json_schema"
            ):
                self._append_session_event(
                    "turn.hallucinated_tool_warning", payload={"warning": warning},
                )
            else:
                assistant_msg.content = f"{assistant_msg.content}\n\n{warning}"
        return None

    @record_asset_turn
    async def _drive_turn(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[Message]:
        """执行一个流式 Agent turn，按生成顺序交付过程和结果消息。

        参数:
            user_message: 当前用户输入。
            output_format: 可选的结构化输出要求。
            outcome_contract: 可选的任务结果契约。

        生成:
            commentary、tool result 与最终 Message；中止和异常仍按原语义传播。
        """

        from ..runtime.guidance import GuidanceSafePoint

        user_msg, turn_start_index, contract = await self._prepare_turn(
            user_message, output_format, outcome_contract,
        )
        turn_id = f"turn-{user_msg.id}"
        selected_skill_loader = getattr(self.agent, '_preload_selected_skills', None)
        if callable(selected_skill_loader):
            for selected_skill_message in await selected_skill_loader(parent_msg_id=user_msg.id):
                yield selected_skill_message
        preloaded_skill_msg = await self._safe_preload_recommended_skill(
            parent_msg_id=user_msg.id
        )
        if preloaded_skill_msg is not None:
            await self._persist_turn_state(
                turn_id=turn_id,
                state="preparing",
                user_message_id=user_msg.id,
            )
            yield preloaded_skill_msg

        contract, guidance_messages = await self._apply_pending_guidance(
            turn_id=turn_id,
            user_message_id=user_msg.id,
            safe_point=GuidanceSafePoint.AFTER_RECOVERY,
            iteration=0,
            contract=contract,
        )
        for guidance_message in guidance_messages:
            yield guidance_message

        max_iterations = self.agent.agent_info.steps or 100
        workflow_min_iterations = _workflow_stage_min_iterations(self.agent)
        if workflow_min_iterations:
            max_iterations = max(max_iterations, workflow_min_iterations)
        iterations = 0
        tool_call_count = 0
        last_completed_tool_name = ""
        consecutive_tool_failures = 0
        max_tool_calls = self._max_tool_calls_per_turn()
        tool_budget_notice_sent = False
        outcome_repair_attempts = 0
        previous_outcome_candidate = ""
        report_repair_paths = {'read': set(), 'write': set()}
        report_repair_calls = 0
        report_repair_written: set[str] = set()
        completion_state = "partial"
        terminal_summary_mode = False
        terminal_summary_result: Optional[ToolResult] = None
        seen_read_results: set[tuple[str, str]] = set()
        repeated_read_results = 0
        read_progress_synthesis = False
        computer_action_progress: dict[str, Any] = {}

        while iterations < max_iterations and not self.agent._abort_event.is_set():
            iterations += 1
            if not tool_budget_notice_sent and tool_call_count > 0 and max_tool_calls - tool_call_count <= 5:
                budget_notice = Message(
                    role='user', id=self.agent._generate_id(), parent_id=user_msg.id,
                    content=(f'[TOOL_BUDGET_BOUNDARY] 本回合剩余 {max(0, max_tool_calls - tool_call_count)} 次工具调用，额度不会自动增加。'
                             '停止无关目录探索和重复读取；已有成功写入不要重写，优先完成必要的最终读回并返回证据支持的交付结论。'
                             '系统自动验收由现有运行时机制执行，无需为此搜索或启动额外 Workflow；用户明确指定的审查步骤仍须执行。'
                             '未完成或未审查的部分如实标明，不能自行宣称通过；此提示不改变权限、审查标准或重试次数。'),
                    metadata={'internal': True, 'purpose': 'tool_budget_boundary'},
                )
                setattr(budget_notice, '_internal', True)
                setattr(budget_notice, '_hide_from_frontend', True)
                self.agent.messages.append(budget_notice)
                tool_budget_notice_sent = True
            if iterations == max(1, max_iterations - 2):
                budget_notice = Message(
                    role='user', id=self.agent._generate_id(), parent_id=user_msg.id,
                    content=(f'[TURN_BUDGET_BOUNDARY] 本回合只剩 {max_iterations - iterations + 1} 次模型调用（含本次），不会自动延长。'
                             '停止无关浏览和重复扫描，优先按用户要求用获准工具保存已有证据支持的交付物，然后汇报结果。'
                             '证据不足的部分明确写为未知或未完成，不得编造引句、来源或成功。此提示不扩大权限或增加预算。'),
                    metadata={'internal': True, 'purpose': 'turn_budget_boundary'},
                )
                setattr(budget_notice, '_internal', True)
                setattr(budget_notice, '_hide_from_frontend', True)
                self.agent.messages.append(budget_notice)
            contract, guidance_messages = await self._apply_pending_guidance(
                turn_id=turn_id,
                user_message_id=user_msg.id,
                safe_point=GuidanceSafePoint.BEFORE_COMPACTION,
                iteration=iterations,
                contract=contract,
            )
            for guidance_message in guidance_messages:
                yield guidance_message
            await self._compact_turn_context(
                turn_id=turn_id, user_message_id=user_msg.id, iteration=iterations,
            )
            show_tool_init_logs = os.getenv(
                "TCODE_CLI_SHOW_TOOL_INIT_LOGS", "1"
            ).strip().lower() not in {"0", "false", "no", "off", "quiet"}
            if show_tool_init_logs:
                print(
                    f"[Agent] Preparing tools: session={self.agent.session_id}, "
                    f"iteration={iterations}"
                )
            tools = (
                []
                if terminal_summary_mode or read_progress_synthesis
                else await self._safe_get_tool_definitions()
            )
            if outcome_repair_attempts > 0:
                tools = [tool for tool in tools if report_repair_calls < 4 and report_repair_paths.get(tool.name)]
            read_progress_synthesis = False
            contract, guidance_messages = await self._apply_pending_guidance(
                turn_id=turn_id,
                user_message_id=user_msg.id,
                safe_point=GuidanceSafePoint.BEFORE_MODEL_CALL,
                iteration=iterations,
                contract=contract,
            )
            if guidance_messages:
                for guidance_message in guidance_messages:
                    yield guidance_message
                continue
            provider_name = str(
                getattr(self.agent.provider, "name", "")
                or self.agent.provider.__class__.__name__
            )
            model_name = str(getattr(self.agent.provider, "_model", "") or "default")
            print(
                f"[Agent] Calling LLM: session={self.agent.session_id}, "
                f"iteration={iterations}, provider={provider_name}, "
                f"model={model_name}, tools={len(tools)}"
            )
            await self._persist_turn_state(
                turn_id=turn_id,
                state="calling_llm",
                user_message_id=user_msg.id,
                iteration=iterations,
            )
            stream_message_id = self.agent._generate_id()
            streamed_text_parts: list[str] = []
            previous_text_delta_handler = getattr(
                self.agent, "_llm_text_delta_handler", None
            )

            async def emit_text_delta(delta: str) -> None:
                text = str(delta or "")
                if not text:
                    return
                streamed_text_parts.append(text)
                await self._emit_model_output_event(
                    status="streaming",
                    iteration=iterations,
                    message_id=stream_message_id,
                    delta=text,
                )

            stream_to_frontend = bool(
                getattr(self.agent, "_frontend_stream", False)
            ) and callable(getattr(self.agent, "_agent_event_emitter", None))
            if stream_to_frontend:
                self.agent._llm_text_delta_handler = emit_text_delta
            try:
                claimed_during_model: list[Any] = []
                try:
                    from contextlib import nullcontext
                    from ..runtime.verification import VerificationCall
                    role_name = str(getattr(self.agent.agent_info, "name", ""))
                    accounting = (VerificationCall(self.agent.provider,
                        source="automatic_outcome_repair" if outcome_repair_attempts else "explicit_review",
                        session=await self._turn_session(), included_in_session_usage=True,
                        mode=str(contract.metadata.get("verification_mode", "")))
                        if outcome_repair_attempts or role_name in {"critic", "verifier"} else nullcontext())
                    async with accounting as call:
                        llm_result, claimed_during_model = await self._call_llm_with_guidance_race(
                            tools,
                            iteration=iterations,
                            turn_id=turn_id,
                            stream_message_id=stream_message_id,
                        )
                        if call is not None and llm_result is not None:
                            call.response = llm_result[2]
                finally:
                    self.agent._llm_text_delta_handler = previous_text_delta_handler
                if claimed_during_model:
                    contract, guidance_messages = await self._apply_claimed_guidance(
                        items=claimed_during_model,
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        iteration=iterations,
                        contract=contract,
                    )
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    continue
                if llm_result is None:
                    continue
                response_content, tool_calls, model_response = llm_result
                output_budget_partial = _model_output_budget_partial(model_response)
                contract, guidance_messages = await self._apply_pending_guidance(
                    turn_id=turn_id,
                    user_message_id=user_msg.id,
                    safe_point=GuidanceSafePoint.AFTER_MODEL_CALL,
                    iteration=iterations,
                    contract=contract,
                )
                if guidance_messages:
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    continue
                response_content = self._consume_research_route_metadata(
                    response_content,
                    turn_id=turn_id,
                )
                await self._capture_research_turn_route(
                    user_message=user_message,
                    turn_id=turn_id,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._persist_turn_state(
                    turn_id=turn_id,
                    state="interrupted",
                    user_message_id=user_msg.id,
                    iteration=iterations,
                    error="Interrupted while waiting for the model response.",
                    clear_active=True,
                )
                raise
            except Exception as exc:
                if terminal_summary_mode and terminal_summary_result is not None:
                    fallback = await self._finalize_terminal_summary_fallback(
                        result=terminal_summary_result,
                        user_message=user_message,
                        turn_start_index=turn_start_index,
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        parent_id=user_msg.id,
                        contract=contract,
                        synthesis_error=exc,
                    )
                    yield fallback
                    return
                fallback = await self._finalize_from_task_result_after_llm_timeout(
                    user_message=user_message,
                    turn_start_index=turn_start_index,
                    turn_id=turn_id,
                    user_message_id=user_msg.id,
                    parent_id=user_msg.id,
                    error=exc,
                    contract=contract,
                )
                if fallback is not None:
                    yield fallback
                    return
                await self._persist_turn_state(
                    turn_id=turn_id,
                    state="failed",
                    user_message_id=user_msg.id,
                    iteration=iterations,
                    error=str(exc),
                    clear_active=True,
                )
                raise
            print(
                f"[Agent] LLM response received: session={self.agent.session_id}, "
                f"iteration={iterations}"
            )

            if outcome_repair_attempts > 0 and tool_calls:
                if self._report_repair_calls_allowed(tool_calls, report_repair_paths, 4-report_repair_calls,
                        pending_reads=self._report_repair_readback(turn_start_index), written=report_repair_written):
                    report_repair_calls += len(tool_calls)
                    report_repair_written.update(str(self._local_report_repair_path(call['function']['arguments']['filePath']))
                        for call in tool_calls if call['function']['name'] == 'write')
                else:
                    # Reject the entire batch; repair never grants new execution or file targets.
                    response_content = '修订超出已有本地报告范围、重复写入或未预留最终读回额度，未执行该批调用；保留已有结果，仍需修订。'
                    tool_calls = None

            if outcome_repair_attempts > 0 and not tool_calls:
                protocol_fallback = self._repair_protocol_fallback(response_content, previous_outcome_candidate)
                if protocol_fallback is not None:
                    self._append_session_event("turn.repair_protocol_rejected", payload={
                        "reason": "unexecuted_dsml", "repair_attempt": outcome_repair_attempts,
                        "raw_response_sha256": hashlib.sha256((response_content or '').encode()).hexdigest(),
                    })
                    response_content = protocol_fallback
                    terminal_summary_mode = True

            if (
                terminal_summary_mode
                and terminal_summary_result is not None
                and tool_calls
            ):
                response_content = self._build_human_terminal_fallback(
                    terminal_summary_result
                )
                tool_calls = None

            assistant_msg = await self._record_assistant_response(
                response_content=response_content,
                tool_calls=tool_calls,
                model_response=model_response,
                stream_message_id=stream_message_id,
                streamed=bool(streamed_text_parts),
                turn_id=turn_id,
                user_message_id=user_msg.id,
                iteration=iterations,
            )
            if output_budget_partial:
                assistant_msg.metadata["output_budget_partial"] = True
            yield assistant_msg

            if tool_calls:
                contract, guidance_messages = await self._apply_pending_guidance(
                    turn_id=turn_id,
                    user_message_id=user_msg.id,
                    safe_point=GuidanceSafePoint.BEFORE_FIRST_TOOL,
                    iteration=iterations,
                    contract=contract,
                    pending_tool_calls=assistant_msg.tool_calls,
                    assistant_message_id=assistant_msg.id,
                )
                if guidance_messages:
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    continue

            if not tool_calls:
                previous_content = str(assistant_msg.content or "")
                assistant_msg.content = await self.agent._ensure_structured_output(
                    assistant_msg.content
                )
                if not (assistant_msg.content or "").strip():
                    fallback = self.agent._build_tool_summary_response()
                    if fallback:
                        assistant_msg.content = fallback
                assistant_msg.content = self._append_downloaded_pdf_paths(
                    assistant_msg.content,
                    turn_start_index=turn_start_index,
                    contract=contract,
                )

                if not terminal_summary_mode:
                    block_msg = await self._check_final_answer_guards(
                        assistant_msg, turn_start_index=turn_start_index,
                        turn_id=turn_id, user_message_id=user_msg.id,
                        iteration=iterations,
                    )
                    if block_msg is not None:
                        yield block_msg
                        continue

                from ..guard import normalize_outcome_candidate

                assistant_msg.content = normalize_outcome_candidate(
                    assistant_msg.content,
                    contract,
                )

                validation = await self._validate_outcome_candidate(
                    assistant_msg.content,
                    contract=contract,
                    turn_start_index=turn_start_index,
                    repair_attempt=outcome_repair_attempts,
                )
                delivery_warnings = (assistant_msg.metadata or {}).get("validation_warnings", [])
                if delivery_warnings and getattr(validation, "advisory", False):
                    validation.failed_criteria.extend(delivery_warnings)
                    validation.verification_status = "PARTIAL"
                    validation.confidence = min(validation.confidence, 0.69)
                    self.agent._last_outcome_validation.update(validation.to_dict())
                if (
                    not terminal_summary_mode
                    and validation.verdict == "repair"
                    and outcome_repair_attempts < contract.max_repair_attempts
                ):
                    contract, guidance_messages = await self._apply_pending_guidance(
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        safe_point=GuidanceSafePoint.BEFORE_OUTCOME_REPAIR,
                        iteration=iterations,
                        contract=contract,
                        assistant_message_id=assistant_msg.id,
                    )
                    if guidance_messages:
                        assistant_msg.metadata["superseded_by_guidance"] = True
                        for guidance_message in guidance_messages:
                            yield guidance_message
                        continue
                    outcome_repair_attempts += 1
                    previous_outcome_candidate = assistant_msg.content or ""
                    report_repair_paths = self._local_report_repair_paths(turn_start_index)
                    repair_msg = Message(
                        role="user",
                        content=self._outcome_repair_prompt(contract, validation) +
                            '\n仅修复验收指出的具体缺口，复用已有成功写入及完整读回证据；已成功文件不得为刷新记录而重写。若仅缺最终回复，直接修订回复；若仅缺最终版本读回，只 read 缺失文件，不重写。只有文件内容确有验收缺陷才使用正式 write 修正本轮已有本地文本报告，同一文件修复阶段最多写一次。总计仍最多四次工具调用，每次必要写入必须预留一次后续完整 read；写与读分批执行，必须实际读回成功后才能声称核验。涉及报告与复核记录时，按 write最终报告→read最终报告→write复核记录→read复核记录 顺序；复核记录依据刚读回的报告版本，之后不再改报告。额度不足时保留成功文件，明确未完成，不得以旧版本读回代替最终版本核验。禁止 Shell、网络、委派、新计算、新文件或外部写入；不输出伪工具调用文本。允许文件：' +
                            json.dumps({key: sorted(value) for key,value in report_repair_paths.items()},ensure_ascii=False),
                        id=self.agent._generate_id(),
                        parent_id=assistant_msg.id,
                        metadata={"internal": True, "purpose": "outcome_repair"},
                    )
                    setattr(repair_msg, "_internal", True)
                    setattr(repair_msg, "_hide_from_frontend", True)
                    self.agent.messages.append(repair_msg)
                    await self._persist_turn_state(
                        turn_id=turn_id,
                        state="repairing_outcome",
                        user_message_id=user_msg.id,
                        assistant_message_id=assistant_msg.id,
                        iteration=iterations,
                        extra={
                            "outcome_validation": validation.to_dict(),
                            "repair_attempt": outcome_repair_attempts,
                        },
                    )
                    continue

                completion_state = (
                    "partial"
                    if output_budget_partial
                    else (
                        ("partial" if terminal_summary_result is None else "blocked")
                        if terminal_summary_mode
                        else (
                            validation.verdict
                            if validation.verdict in {"partial", "blocked"}
                            else (
                                "partial"
                                if validation.verdict == "repair"
                                else "completed"
                            )
                        )
                    )
                )
                if getattr(validation, "advisory", False) and not terminal_summary_mode and not output_budget_partial:
                    completion_state = "partial" if validation.verification_status in {"PARTIAL", "UNVERIFIED"} else "completed"

                if assistant_msg.content != previous_content:
                    await self._persist_turn_state(
                        turn_id=turn_id,
                        state="finalizing",
                        user_message_id=user_msg.id,
                        assistant_message_id=assistant_msg.id,
                        iteration=iterations,
                    )
                    yield assistant_msg
                contract, guidance_messages = await self._apply_pending_guidance(
                    turn_id=turn_id,
                    user_message_id=user_msg.id,
                    safe_point=GuidanceSafePoint.BEFORE_FINALIZE,
                    iteration=iterations,
                    contract=contract,
                    assistant_message_id=assistant_msg.id,
                )
                if guidance_messages:
                    assistant_msg.metadata["superseded_by_guidance"] = True
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    continue
                guidance_port = getattr(self.agent, "_guidance_port", None)
                guidance_owner_id = str(
                    getattr(self.agent, "_guidance_owner_id", "") or ""
                )
                if guidance_port is not None and guidance_owner_id:
                    fence_result = await guidance_port.fence_completion(
                        session_id=str(
                            getattr(self.agent, "session_id", "") or ""
                        ),
                        turn_id=turn_id,
                        owner_id=guidance_owner_id,
                    )
                    if fence_result == "blocked_by_guidance":
                        assistant_msg.metadata["superseded_by_guidance"] = True
                        await asyncio.sleep(0.05)
                        continue
                    await self._persist_completion_fence(turn_id=turn_id)
                break

            print(f"\n🔧 第 {iterations} 轮工具调用:")
            guidance_replan = False
            for tool_index, tool_call in enumerate(assistant_msg.tool_calls):
                contract, guidance_messages = await self._apply_pending_guidance(
                    turn_id=turn_id,
                    user_message_id=user_msg.id,
                    safe_point=(
                        GuidanceSafePoint.BEFORE_FIRST_TOOL
                        if tool_index == 0
                        else GuidanceSafePoint.BEFORE_NEXT_TOOL
                    ),
                    iteration=iterations,
                    contract=contract,
                    pending_tool_calls=assistant_msg.tool_calls[tool_index:],
                    assistant_message_id=assistant_msg.id,
                )
                if guidance_messages:
                    guidance_replan = True
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    break
                tool_call_count += 1
                print(f"└─ 工具: {tool_call.tool_name}")
                print(
                    f"参数: {json.dumps(tool_call.arguments, ensure_ascii=False)[:100]}..."
                )
                intercepted = (
                    self._tool_call_limit_result(tool_call, limit=max_tool_calls)
                    if tool_call_count > max_tool_calls
                    else await self._maybe_intercept_duplicate_tool_retry(
                        user_message=user_message,
                        tool_call=tool_call,
                        turn_start_index=turn_start_index,
                    )
                )
                if intercepted is not None:
                    result = await self.agent._tool_executor.record_synthetic_result(
                        tool_call,
                        intercepted,
                    )
                else:
                    result = await self._execute_turn_tool(
                        tool_call, assistant_msg=assistant_msg, turn_id=turn_id,
                        user_message_id=user_msg.id, iteration=iterations,
                    )
                if tool_call.tool_name == "task":
                    consecutive_tool_failures = 0
                elif str(result.error or "").strip():
                    consecutive_tool_failures += 1
                else:
                    consecutive_tool_failures = 0
                result = self._apply_tool_loop_policy(
                    tool_call,
                    result,
                    consecutive_failures=consecutive_tool_failures,
                    turn_start_index=turn_start_index,
                )
                tool_failed = bool(str(result.error or "").strip())
                last_completed_tool_name = str(tool_call.tool_name or "")
                print(self._tool_result_console_line(result))

                tool_result_msg = Message(
                    role="tool",
                    content=result.output or result.error or "",
                    id=self.agent._generate_id(),
                    parent_id=assistant_msg.id,
                )
                tool_result_msg.tool_calls = [tool_call]
                self.agent.messages.append(tool_result_msg)
                await self._persist_turn_state(
                    turn_id=turn_id,
                    state="tool_result_recorded",
                    user_message_id=user_msg.id,
                    assistant_message_id=assistant_msg.id,
                    iteration=iterations,
                    pending_tool_calls=assistant_msg.tool_calls,
                )
                yield tool_result_msg
                repeated_read_results = _observe_read_progress(
                    tool_call.tool_name, result, seen_read_results, repeated_read_results,
                )
                unchanged_actions, computer_progress = _observe_computer_action_progress(
                    result, computer_action_progress,
                )
                if repeated_read_results >= 6 and tool_index + 1 == len(assistant_msg.tool_calls):
                    # One normal synthesis call: guards and outcome validation still run.
                    # No synthetic tool success/error and no cached evidence is substituted.
                    read_progress_synthesis = True
                    max_iterations = max(max_iterations, iterations + 1)
                    repeated_read_results = 0
                    synthesis_request = Message(
                        role="user",
                        content=("[READ_PROGRESS_BOUNDARY] Six consecutive successful read observations "
                                 "repeat the same semantic state already obtained in this turn. Stop repeating retrieval. "
                                 "For discovery, select a returned candidate, narrow with query, or follow next_cursor; otherwise "
                                 "summarize the evidence actually returned, with its citations and scope. "
                                 "Distinguish supported facts from unknowns; do not claim missing work succeeded. "
                                 "This next call is synthesis-only; normal acceptance checks still apply."),
                        id=self.agent._generate_id(), parent_id=tool_result_msg.id,
                        metadata={"internal": True, "purpose": "read_progress_synthesis"},
                    )
                    setattr(synthesis_request, "_internal", True)
                    setattr(synthesis_request, "_hide_from_frontend", True)
                    self.agent.messages.append(synthesis_request)
                elif (unchanged_actions >= 2 and computer_progress == "repeated_observe"
                      and tool_index + 1 == len(assistant_msg.tool_calls)):
                    read_progress_synthesis = True
                    max_iterations = max(max_iterations, iterations + 1)
                    computer_action_progress["passive_observe_repeats"] = 0
                    synthesis_request = Message(
                        role="user",
                        content=("[COMPUTER_OBSERVE_BOUNDARY] Three successful observations of the same "
                                 "GUI target returned the same complete semantic state without an intervening "
                                 "action. Stop requesting approval for the same observation. Use the exact "
                                 "target_ref, observation_ref, and element tokens already returned to perform "
                                 "the requested action, or report the concrete reason no safe action is supported. "
                                 "This next call is synthesis-only; do not claim the application changed."),
                        id=self.agent._generate_id(), parent_id=tool_result_msg.id,
                        metadata={"internal": True, "purpose": "computer_observe_synthesis"},
                    )
                    setattr(synthesis_request, "_internal", True)
                    setattr(synthesis_request, "_hide_from_frontend", True)
                    self.agent.messages.append(synthesis_request)
                elif (unchanged_actions >= 3 and computer_progress == "unchanged"
                      and tool_index + 1 == len(assistant_msg.tool_calls)):
                    # Keep this guard independent from repeated-read discovery:
                    # these are completed actions followed by fresh observations,
                    # but the application semantics did not change.
                    read_progress_synthesis = True
                    max_iterations = max(max_iterations, iterations + 1)
                    computer_action_progress["unchanged"] = 0
                    synthesis_request = Message(
                        role="user",
                        content=("[COMPUTER_NO_PROGRESS_BOUNDARY] Three completed GUI actions, each followed "
                                 "by a fresh observation, produced no verified semantic state change. "
                                 "Stop blind repetition. Report that the intended application change is not "
                                 "verified and distinguish it from successful tool delivery. Do not claim goal "
                                 "success; this next call is synthesis-only and normal acceptance checks apply."),
                        id=self.agent._generate_id(), parent_id=tool_result_msg.id,
                        metadata={"internal": True, "purpose": "computer_no_progress_synthesis"},
                    )
                    setattr(synthesis_request, "_internal", True)
                    setattr(synthesis_request, "_hide_from_frontend", True)
                    self.agent.messages.append(synthesis_request)
                elif (unchanged_actions >= 3 and computer_progress == "unverified"
                      and tool_index + 1 == len(assistant_msg.tool_calls)):
                    max_iterations = max(max_iterations, iterations + 1)
                    computer_action_progress["unverified"] = 0
                    synthesis_request = Message(
                        role="user",
                        content=("[COMPUTER_REPLAN_BOUNDARY] The same GUI action family/target/region was "
                                 "repeated three times without verified target-state progress. Fresh screenshots are "
                                 "available to compare, but semantic coverage is insufficient, so the outcome "
                                 "is unknown rather than success or no-progress. Do not repeat that identical "
                                 "action again. Inspect the newest screenshot and report what is verified and "
                                 "what remains uncertain. This next call is synthesis-only; no further actions are allowed in this turn."),
                        id=self.agent._generate_id(), parent_id=tool_result_msg.id,
                        metadata={"internal": True, "purpose": "computer_visual_replan"},
                    )
                    setattr(synthesis_request, "_internal", True)
                    setattr(synthesis_request, "_hide_from_frontend", True)
                    self.agent.messages.append(synthesis_request)
                if unchanged_actions >= 3 and computer_progress in {"unchanged", "unverified"}:
                    terminal_summary_mode = True
                    max_iterations = max(max_iterations, iterations + 1)
                    terminal_summary_result = ToolResult(
                        title="Computer verification boundary",
                        output="Repeated actions have no verified target-state progress. Inspect the latest evidence and report what is verified and what remains uncertain. Do not perform more actions or claim that delivery proves task success.",
                        error="computer_verification_limit_exceeded",
                        metadata={"verification_required": True, "should_end_turn": True},
                    )
                if await self._append_public_tool_progress(
                    tool_call, assistant_msg=assistant_msg, turn_start_index=turn_start_index,
                    turn_id=turn_id, user_message_id=user_msg.id, iteration=iterations,
                ):
                    yield assistant_msg

                if (terminal_summary_mode and terminal_summary_result is not None
                        and terminal_summary_result.error == "computer_verification_limit_exceeded"):
                    for pending_call in assistant_msg.tool_calls[tool_index + 1:]:
                        yield self._append_skipped_tool_result(
                            pending_call, parent_id=assistant_msg.id, guidance_ids=[],
                            boundary_reason="Skipped because repeated GUI actions have no verified target-state progress. Report the latest evidence before performing further actions.",
                        )
                    await self._persist_turn_state(
                        turn_id=turn_id, state="summarizing_verification_boundary",
                        user_message_id=user_msg.id, assistant_message_id=assistant_msg.id,
                        iteration=iterations,
                    )
                    break

                guidance_port = getattr(self.agent, "_guidance_port", None)
                if bool(getattr(tool_call, "_release_deferred_guidance", False)):
                    setattr(tool_call, "_release_deferred_guidance", False)
                    if guidance_port is not None:
                        try:
                            await guidance_port.release_deferred_for_turn(
                                session_id=str(
                                    getattr(self.agent, "session_id", "") or ""
                                ),
                                turn_id=turn_id,
                            )
                        except Exception:
                            pass
                preclaimed_guidance = list(
                    getattr(tool_call, "_guidance_claimed_items", []) or []
                )
                setattr(tool_call, "_guidance_claimed_items", [])
                if preclaimed_guidance:
                    contract, guidance_messages = await self._apply_claimed_guidance(
                        items=preclaimed_guidance,
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        iteration=iterations,
                        contract=contract,
                        pending_tool_calls=assistant_msg.tool_calls[tool_index + 1 :],
                        assistant_message_id=assistant_msg.id,
                    )
                else:
                    contract, guidance_messages = await self._apply_pending_guidance(
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        safe_point=GuidanceSafePoint.AFTER_TOOL_RESULT,
                        iteration=iterations,
                        contract=contract,
                        pending_tool_calls=assistant_msg.tool_calls[tool_index + 1 :],
                        assistant_message_id=assistant_msg.id,
                    )
                if guidance_messages:
                    guidance_replan = True
                    for guidance_message in guidance_messages:
                        yield guidance_message
                    break

                workflow_report = _completed_workflow_report(
                    tool_call, result,
                    pending_tools=tool_index + 1 < len(assistant_msg.tool_calls),
                )
                if workflow_report:
                    # The declared stage report is the deliverable. A second
                    # parent-model rewrite/semantic judge can invent a human
                    # acceptance gate after the Workflow has already completed.
                    # Do not award a generic VERIFIED/P2 badge for scientific
                    # hypotheses; keep the Workflow's own checks and caveats.
                    self.agent._last_outcome_status = ""
                    self.agent._last_outcome_validation = {}
                    final_msg = Message(
                        role="assistant", content=workflow_report,
                        id=self.agent._generate_id(), parent_id=user_msg.id,
                        metadata={"completion_source": "workflow_validated_report",
                                  "workflow_run_id": (result.metadata or {}).get("run_id")},
                    )
                    self.agent.messages.append(final_msg)
                    await self._finalize_turn(
                        user_message=user_message, final_message=final_msg,
                        turn_messages=self.agent.messages[turn_start_index:],
                        turn_id=turn_id, user_message_id=user_msg.id,
                        completion_state="completed",
                    )
                    yield final_msg
                    return

                should_end_turn = self._should_end_turn_for_tool_result(
                    tool_call,
                    result,
                )
                if should_end_turn:
                    collected, extra_msgs = (
                        await self.agent._try_async_followup_collect(
                            result,
                            assistant_msg.id,
                        )
                    )
                    for msg in extra_msgs:
                        yield msg
                    if collected:
                        break
                    terminal_metadata = dict(result.metadata or {})
                    deterministic_control_stop = bool(
                        terminal_metadata.get("tool_call_limit_exceeded")
                        or terminal_metadata.get(
                            "consecutive_tool_failure_limit_exceeded"
                        )
                    )
                    # Loop guards finalize immediately; other terminal results
                    # still pass through synthesis and the guidance fence.
                    if not deterministic_control_stop:
                        if (
                            (str(result.error or "").strip() or str(terminal_metadata.get("status") or "") in {"blocked", "failed", "terminated"})
                            and not bool(
                                dict(result.metadata or {}).get("human_review_required")
                            )
                        ):
                            terminal_summary_mode = True
                            terminal_summary_result = result
                            max_iterations = max(max_iterations, iterations + 1)
                            summary_request = self._queue_terminal_summary_request(
                                result,
                                parent_id=tool_result_msg.id,
                            )
                            await self._persist_turn_state(
                                turn_id=turn_id,
                                state="summarizing_terminal_error",
                                user_message_id=user_msg.id,
                                assistant_message_id=assistant_msg.id,
                                iteration=iterations,
                                extra={
                                    "terminal_summary_request_id": summary_request.id,
                                    "terminal_error": str(result.error or "")[:1200],
                                },
                            )
                            break
                        contract, guidance_messages = await self._apply_pending_guidance(
                            turn_id=turn_id,
                            user_message_id=user_msg.id,
                            safe_point=GuidanceSafePoint.BEFORE_FINALIZE,
                            iteration=iterations,
                            contract=contract,
                            pending_tool_calls=assistant_msg.tool_calls[tool_index + 1 :],
                            assistant_message_id=assistant_msg.id,
                        )
                        if guidance_messages:
                            guidance_replan = True
                            for guidance_message in guidance_messages:
                                yield guidance_message
                            break
                        guidance_port = getattr(self.agent, "_guidance_port", None)
                        guidance_owner_id = str(
                            getattr(self.agent, "_guidance_owner_id", "") or ""
                        )
                        if guidance_port is not None and guidance_owner_id:
                            fence_result = await guidance_port.fence_completion(
                                session_id=str(
                                    getattr(self.agent, "session_id", "") or ""
                                ),
                                turn_id=turn_id,
                                owner_id=guidance_owner_id,
                            )
                            if fence_result == "blocked_by_guidance":
                                guidance_replan = True
                                break
                            await self._persist_completion_fence(turn_id=turn_id)
                    final_msg = Message(
                        role="assistant",
                        content=self._append_downloaded_pdf_paths(
                            self._build_terminal_turn_response(result),
                            turn_start_index=turn_start_index,
                            contract=contract,
                        ),
                        id=self.agent._generate_id(),
                        parent_id=user_msg.id,
                    )
                    self.agent.messages.append(final_msg)
                    await self._finalize_turn(
                        user_message=user_message,
                        final_message=final_msg,
                        turn_messages=self.agent.messages[turn_start_index:],
                        turn_id=turn_id,
                        user_message_id=user_msg.id,
                        completion_state=("completed" if tool_call.tool_name == "goal_checkpoint"
                                          and not result.error and terminal_metadata.get("goal_checkpoint")
                                          else "blocked"),
                    )
                    yield final_msg
                    return
            else:
                continue
            if guidance_replan:
                continue

        last_assistant = None
        for msg in reversed(self.agent.messages):
            if msg.role == "assistant":
                last_assistant = msg
                break
        self.agent._emit_agent_hook("message.stream.end", {
            "agent": self.agent.agent_info.name,
            "session_id": self.agent.session_id,
        })

        if last_assistant:
            await self._finalize_turn(
                user_message=user_message,
                final_message=last_assistant,
                turn_messages=self.agent.messages[turn_start_index:],
                turn_id=turn_id,
                user_message_id=user_msg.id,
                completion_state=completion_state,
            )
