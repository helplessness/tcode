"""Session compaction - compress conversation history while preserving context

Implements the same logic as tcode's compaction.ts:
1. Check if context is overflowing (auto compaction trigger)
2. Prune old tool outputs (remove irrelevant tool results)
3. Generate summary using LLM (preserve key information)
4. Create compaction markers (for sliding window)
"""

import asyncio
from copy import deepcopy
import json
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Optional

from ..learning import LearningManager
from ..types import Message, Usage
from ..session.manager import SessionManager, Session


@dataclass
class CompactionConfig:
    """Configuration for session compaction"""

    auto: bool = True  # Enable auto compaction
    prune: bool = True  # Enable pruning of old tool outputs
    reserved: int = 20_000  # Reserved tokens for new context
    buffer: int = 20_000  # Buffer before triggering compaction

    # Pruning thresholds
    prune_minimum: int = 20_000  # Minimum tokens to prune
    prune_protect: int = 40_000  # Protect recent tool outputs
    protected_tools: list[str] = field(default_factory=lambda: ["skill", "task"])
    summary_context_tokens: int = 48_000
    summary_per_message_tokens: int = 1_000
    recent_keep_messages: int = 6
    recent_message_tokens: int = 4_000

    @classmethod
    def _env_bool(cls, name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _env_int(cls, name: str, default: int, minimum: int = 0) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(minimum, value)

    @classmethod
    def from_env(cls) -> "CompactionConfig":
        protected_tools_raw = str(
            os.environ.get("TCODE_COMPACTION_PROTECTED_TOOLS", "skill,task") or ""
        ).strip()
        protected_tools = [
            item.strip()
            for item in protected_tools_raw.split(",")
            if item.strip()
        ] or ["skill", "task"]

        return cls(
            auto=cls._env_bool("TCODE_COMPACTION_AUTO", True),
            prune=cls._env_bool("TCODE_COMPACTION_PRUNE", True),
            reserved=cls._env_int("TCODE_COMPACTION_RESERVED_TOKENS", 20_000),
            buffer=cls._env_int("TCODE_COMPACTION_BUFFER_TOKENS", 20_000),
            prune_minimum=cls._env_int("TCODE_COMPACTION_PRUNE_MINIMUM_TOKENS", 20_000),
            prune_protect=cls._env_int("TCODE_COMPACTION_PRUNE_PROTECT_TOKENS", 40_000),
            protected_tools=protected_tools,
            summary_context_tokens=cls._env_int(
                "TCODE_COMPACTION_SUMMARY_CONTEXT_TOKENS",
                48_000,
                minimum=2_000,
            ),
            summary_per_message_tokens=cls._env_int(
                "TCODE_COMPACTION_SUMMARY_PER_MESSAGE_TOKENS",
                1_000,
                minimum=200,
            ),
            recent_keep_messages=cls._env_int(
                "TCODE_COMPACTION_RECENT_KEEP_MESSAGES",
                6,
                minimum=2,
            ),
            recent_message_tokens=cls._env_int(
                "TCODE_COMPACTION_RECENT_MESSAGE_TOKENS",
                4_000,
                minimum=500,
            ),
        )

    @classmethod
    def model_context_limit_from_env(cls, default: int = 128_000) -> int:
        return cls._env_int("TCODE_COMPACTION_MODEL_CONTEXT_LIMIT", default, minimum=1)


@dataclass
class CompactionResult:
    """Result from compaction process"""

    status: Literal["success", "failed", "skipped"]
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    summary: str = ""
    pruned_messages: int = 0
    pruned_tool_outputs: int = 0


class SessionCompaction:
    """Session compaction manager"""

    DEFAULT_COMPACT_PROMPT = """Provide a detailed prompt for continuing our conversation above.
Focus on information that would be helpful for continuing the conversation, including what we did, what we're doing, which files we're working on, and what we're going to do next.
The summary that you construct will be used so that another agent can read it and continue the work.

When constructing the summary, try to stick to this template:
---
## Goal

[What goal(s) is the user trying to accomplish?]

## Instructions

- [What important instructions did the user give you that are relevant]
- [If there is a plan or spec, include information about it so that next agent can continue using it]

## Discoveries

[What notable things were learned during this conversation that would be useful for the next agent to know when continuing the work]

## Accomplished

[What work has been completed, what work is still in progress, and what work is left?]

## Relevant files / directories

[Construct a structured list of relevant files that have been read, edited, or created that pertain to the task at hand. If all the files in a directory are relevant, include the path to the directory.]
---"""

    CONTINUE_PROMPT = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."

    @classmethod
    def _lifecycle_messages(cls, messages: list[Message]) -> list[dict[str, str]]:
        """为插件事件建立有界消息快照，避免把整个 Session 对象暴露出去。"""

        if len(messages) <= 32:
            selected = list(messages)
        else:
            selected = [*messages[:4], *messages[-28:]]
        remaining = 32_000
        payload: list[dict[str, str]] = []
        for message in selected:
            if remaining <= 0:
                break
            content = str(getattr(message, "content", "") or "")[: min(4000, remaining)]
            remaining -= len(content)
            payload.append(
                {
                    "id": str(getattr(message, "id", "") or ""),
                    "role": str(getattr(message, "role", "") or "unknown"),
                    "content": content,
                }
            )
        return payload

    @classmethod
    async def _emit_lifecycle(
        cls,
        session: Session,
        event: str,
        payload: dict[str, Any],
        *,
        mode: str = "serial",
    ) -> None:
        """有界等待生命周期插件；插件故障不应阻止上下文自救。"""

        try:
            from ..server.plugin_warmup import trigger_plugin_event

            resolved_workdir = os.path.realpath(
                os.path.abspath(
                    str(getattr(session, "workdir", "") or os.getcwd())
                )
            )
            await trigger_plugin_event(
                resolved_workdir,
                event,
                {"__workspace_path": resolved_workdir, **payload},
                {},
                mode=mode,
            )
        except Exception:
            pass

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Estimate token count (rough: 4 chars per token)"""
        return len(text) // 4

    @classmethod
    def _estimate_message_tokens(cls, msg: Message) -> int:
        """估算单条消息在上下文中占用的 token（不叠加历史 usage）。"""
        total = cls._estimate_tokens(msg.content or "")
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += cls._estimate_tokens(str(tc.tool_name or ""))
                if tc.arguments:
                    try:
                        args_text = json.dumps(tc.arguments, ensure_ascii=False)
                    except Exception:
                        args_text = str(tc.arguments)
                    total += cls._estimate_tokens(args_text)
        return total

    @classmethod
    def _shorten_text(cls, value: Any, *, token_limit: int) -> str:
        text = str(value or "")
        char_limit = max(1, int(token_limit) * 4)
        if len(text) <= char_limit:
            return text
        return text[: max(0, char_limit - 24)] + " ...[truncated]"

    @classmethod
    def _message_for_summary(cls, msg: Message, *, token_limit: int) -> str:
        role_label = str(getattr(msg, "role", "") or "unknown").upper()
        parts = [cls._shorten_text(getattr(msg, "content", ""), token_limit=token_limit)]
        content_blocks = getattr(msg, "content_blocks", None) or []
        image_lines: list[str] = []
        for block in content_blocks[:12]:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            label = str(block.get("caption") or block.get("alt") or "图片").strip()
            path = str(block.get("path") or "").strip().lstrip("/")
            if path:
                image_lines.append(f"- {label}: /workspace/{path}")
        if image_lines:
            parts.append("presented_images:\n" + "\n".join(image_lines))
        if getattr(msg, "tool_calls", None):
            tool_lines: list[str] = []
            for tc in (msg.tool_calls or [])[:4]:
                tool_name = str(getattr(tc, "tool_name", "") or "").strip()
                if not tool_name:
                    continue
                args = getattr(tc, "arguments", {}) or {}
                try:
                    args_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
                except Exception:
                    args_text = str(args)
                tool_lines.append(
                    f"- {tool_name}: {cls._shorten_text(args_text, token_limit=120)}"
                )
            if tool_lines:
                parts.append("tool_calls:\n" + "\n".join(tool_lines))
        content = "\n".join(part for part in parts if part)
        return f"[{role_label}]: {content}"

    @classmethod
    def _bounded_summary_context(
        cls,
        messages: list[Message],
        *,
        config: CompactionConfig,
        max_summary_tokens: Optional[int] = None,
    ) -> tuple[str, int]:
        max_tokens = max(
            1,
            min(
                int(max_summary_tokens or config.summary_context_tokens),
                int(config.summary_context_tokens),
            ),
        )
        remaining_chars = max_tokens * 4
        per_message_tokens = max(1, int(config.summary_per_message_tokens))
        selected: list[str] = []
        included = 0

        for msg in reversed(messages):
            chunk = cls._message_for_summary(msg, token_limit=per_message_tokens)
            chunk_len = len(chunk) + 2
            if selected and chunk_len > remaining_chars:
                break
            if chunk_len > remaining_chars:
                chunk = cls._shorten_text(chunk, token_limit=max_tokens)
                chunk_len = len(chunk) + 2
            selected.append(chunk)
            included += 1
            remaining_chars -= chunk_len
            if remaining_chars <= 0:
                break

        selected.reverse()
        omitted = max(0, len(messages) - included)
        if omitted:
            selected.insert(
                0,
                (
                    f"[SYSTEM]: {omitted} older messages were omitted from the "
                    "summarization request to stay within the compaction budget."
                ),
            )
        return "\n\n".join(selected), omitted

    @classmethod
    def _trim_recent_messages(
        cls,
        messages: list[Message],
        *,
        config: CompactionConfig,
    ) -> list[Message]:
        trimmed: list[Message] = []
        for msg in messages:
            content = cls._shorten_text(
                getattr(msg, "content", ""),
                token_limit=config.recent_message_tokens,
            )
            tool_calls = []
            for tc in getattr(msg, "tool_calls", []) or []:
                if not hasattr(tc, "__dataclass_fields__"):
                    tool_calls.append(tc)
                    continue
                new_tc = replace(tc)
                if new_tc.output:
                    new_tc.output = cls._shorten_text(
                        new_tc.output,
                        token_limit=max(250, config.recent_message_tokens // 2),
                    )
                    if new_tc.output != tc.output:
                        new_tc.compacted_at = datetime.now().timestamp()
                tool_calls.append(new_tc)
            trimmed.append(
                replace(
                    msg,
                    content=content,
                    tool_calls=tool_calls,
                )
            )
        return trimmed

    @classmethod
    def _deterministic_summary(
        cls,
        messages: list[Message],
        *,
        config: CompactionConfig,
        reason: str = "",
    ) -> str:
        sample: list[Message] = []
        if len(messages) <= 12:
            sample = list(messages)
        else:
            sample = [*messages[:4], *messages[-8:]]

        lines = [
            "自动压缩摘要：由于会话历史过长或模型摘要失败，系统已用确定性方式压缩早期上下文。",
            "最近消息仍保留在摘要后方；如需完整细节，应查询 session history 或相关 artifact。",
        ]
        if reason:
            lines.append(f"压缩原因：{cls._shorten_text(reason, token_limit=120)}")
        lines.append("")
        lines.append("历史片段：")
        for msg in sample:
            role = str(getattr(msg, "role", "") or "unknown")
            content = cls._shorten_text(getattr(msg, "content", ""), token_limit=220)
            if content:
                lines.append(f"- [{role}] {content}")
        if not sample:
            lines.append("- 无可用历史片段。")
        return "\n".join(lines)

    @classmethod
    def _apply_compacted_messages(
        cls,
        session: Session,
        *,
        summary: str,
        original_tokens: int,
        config: CompactionConfig,
    ) -> CompactionResult:
        guidance_refs: list[dict[str, object]] = []
        for message in session.messages:
            metadata = dict(getattr(message, "metadata", {}) or {})
            guidance = metadata.get("guidance")
            if not isinstance(guidance, dict) or not guidance.get("id"):
                continue
            guidance_refs.append(
                {
                    "id": str(guidance.get("id") or ""),
                    "sequence": int(guidance.get("sequence") or 0),
                    "mode": str(guidance.get("mode") or ""),
                    "target_turn_id": str(guidance.get("target_turn_id") or ""),
                    "applied_safe_point": str(
                        guidance.get("applied_safe_point") or ""
                    ),
                    "contract_revision_id": str(
                        guidance.get("contract_revision_id") or ""
                    ),
                    "requirement_summary": cls._shorten_text(
                        getattr(message, "content", ""),
                        token_limit=160,
                    ),
                }
            )
        summary_message = Message(
            role="assistant",
            content=f"[SESSION SUMMARY]\n\n{summary}",
            id=f"summary-{datetime.now().timestamp()}",
            summary=True,
            metadata={"compacted_guidance_refs": guidance_refs[-64:]},
        )
        compaction_marker = Message(
            role="user",
            content="[COMPACTION MARKER - earlier messages were summarized]",
            id=f"compaction-{datetime.now().timestamp()}",
        )
        keep_count = max(2, int(config.recent_keep_messages))
        messages_to_keep = cls._trim_recent_messages(
            session.messages[-keep_count:],
            config=config,
        )
        from ..presentation.result_selection import retained_selection_messages
        selections_to_keep = retained_selection_messages(session.messages, messages_to_keep)
        if not getattr(session, "display_messages", None):
            session.display_messages = list(session.messages)
        else:
            session.display_messages = SessionManager.visible_messages(session)
        session.messages = [summary_message, compaction_marker, *selections_to_keep, *messages_to_keep]
        compressed_tokens = sum(
            cls._estimate_message_tokens(msg) for msg in session.messages
        )
        return CompactionResult(
            status="success",
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            tokens_saved=original_tokens - compressed_tokens,
            summary=summary,
        )

    @classmethod
    async def is_overflow(
        cls,
        session: Session,
        model_context_limit: int,
        config: Optional[CompactionConfig] = None,
    ) -> bool:
        """Check if session context is overflowing

        Returns True if compaction should be triggered
        """
        if config is None:
            config = CompactionConfig()

        if not config.auto:
            return False

        if model_context_limit == 0:
            return False

        # 只按当前上下文内容估算，不叠加历史请求 usage。
        total_tokens = sum(cls._estimate_message_tokens(msg) for msg in session.messages)

        # Calculate usable context
        reserved = config.reserved
        usable = model_context_limit - reserved

        # Trigger if we're in the buffer zone
        threshold = usable - config.buffer
        return total_tokens >= threshold

    @classmethod
    async def prune(
        cls,
        session: Session,
        config: Optional[CompactionConfig] = None,
    ) -> int:
        """Prune old tool outputs

        Goes backwards through messages and removes old tool outputs
        that are no longer relevant (protecting recent ones).

        Returns number of tool outputs pruned
        """
        if config is None:
            config = CompactionConfig()

        if not config.prune:
            return 0

        pruned_count = 0
        total_tool_tokens = 0
        to_clear = []
        turns = 0

        # Go backwards through messages
        for msg in reversed(session.messages):
            if msg.role == "user":
                turns += 1

            # Skip first 2 turns (most recent work)
            if turns < 2:
                continue

            # Stop at summary marker
            if hasattr(msg, "summary") and msg.summary:
                break

            # Check tool outputs
            if msg.role == "tool" and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.tool_name in config.protected_tools:
                        continue

                    # Check if already compacted
                    if hasattr(tc, "compacted_at") and tc.compacted_at:
                        break

                    # Estimate tokens
                    output_tokens = cls._estimate_tokens(tc.output or "")
                    total_tool_tokens += output_tokens

                    if total_tool_tokens > config.prune_protect:
                        to_clear.append(tc)
                        pruned_count += output_tokens

        # Actually prune
        if pruned_count > config.prune_minimum:
            for tc in to_clear:
                tc.output = "[pruned - output removed during compaction]"
                tc.compacted_at = datetime.now().timestamp()

            return len(to_clear)

        return 0

    @classmethod
    async def process(
        cls,
        session: Session,
        provider,
        agent_name: str = "compaction",
        auto: bool = True,
        config: Optional[CompactionConfig] = None,
        max_summary_tokens: Optional[int] = None,
        raise_on_provider_error: bool = False,
    ) -> CompactionResult:
        """Process compaction - generate summary using LLM

        Args:
            session: Session to compact
            provider: LLM provider to use
            agent_name: Agent to use for compaction
            auto: Whether this is auto-triggered

        Returns:
            CompactionResult with status and statistics
        """
        if config is None:
            config = CompactionConfig.from_env()

        from ..agent.agents import BuiltinAgents

        # Get compaction agent
        agent_info = BuiltinAgents.get(agent_name)
        if not agent_info:
            # Fallback to general agent
            agent_info = BuiltinAgents.get("general")

        # Calculate original tokens
        original_tokens = sum(cls._estimate_message_tokens(msg) for msg in session.messages)

        # Prepare messages for summarization
        # Include all messages up to the compaction point
        messages_to_summarize = []
        for msg in session.messages:
            if hasattr(msg, "summary") and msg.summary:
                # Found previous summary, start from here
                messages_to_summarize = [msg]
            else:
                messages_to_summarize.append(msg)

        if len(messages_to_summarize) < 2:
            # Not enough to summarize
            return CompactionResult(
                status="skipped",
                original_tokens=original_tokens,
            )

        operation_id = uuid.uuid4().hex
        lifecycle_payload = {
            "operation_id": operation_id,
            "session_id": str(getattr(session, "id", "") or ""),
            "agent": str(getattr(session, "agent_name", "") or ""),
            "trigger": "pressure" if auto else "manual",
            "message_count": len(messages_to_summarize),
            "messages": cls._lifecycle_messages(messages_to_summarize),
            "owner": {
                "tenant_id": str(getattr(session, "tenant_id", "") or ""),
                "user_id": str(getattr(session, "user_id", "") or ""),
                "workspace_id": str(getattr(session, "workspace_id", "") or ""),
            },
        }
        await cls._emit_lifecycle(
            session,
            "session.compaction.before",
            lifecycle_payload,
        )

        try:
            LearningManager.flush_before_compaction(
                session=session,
                messages=messages_to_summarize,
            )
        except Exception as exc:
            print(f"[LEARNING] compaction flush failed: {exc}")

        context_text, omitted_messages = cls._bounded_summary_context(
            messages_to_summarize,
            config=config,
            max_summary_tokens=max_summary_tokens,
        )

        # Create summarization prompt
        summarization_prompt = f"""
Previous conversation context:
---
{context_text}
---

Note: {omitted_messages} older message(s) may have been omitted from the raw context
because the summarization request itself must remain inside the model input budget.

{cls.DEFAULT_COMPACT_PROMPT}
"""

        response = None
        fallback_reason = ""
        try:
            # Call LLM to generate summary
            from ..providers.base import ChatMessage

            response = await provider.chat(
                messages=[ChatMessage(role="user", content=summarization_prompt)],
                temperature=0.3,  # Lower temperature for more focused summary
                max_tokens=2000,
            )

            summary = response.content
            if raise_on_provider_error and not str(summary or '').strip():
                raise ValueError('Compaction provider returned an empty summary')
        except Exception as e:
            if raise_on_provider_error:
                raise
            fallback_reason = f"LLM compaction failed: {str(e)}"
            summary = cls._deterministic_summary(
                messages_to_summarize,
                config=config,
                reason=fallback_reason,
            )

        try:
            result = cls._apply_compacted_messages(
                session,
                summary=summary,
                original_tokens=original_tokens,
                config=config,
            )
            if response is not None and response.usage and session.messages:
                usage_obj = None
                if isinstance(response.usage, dict):
                    usage_obj = Usage(
                        prompt_tokens=response.usage.get("prompt_tokens", 0),
                        completion_tokens=response.usage.get("completion_tokens", 0),
                        total_tokens=response.usage.get("total_tokens", 0),
                    )
                else:
                    usage_obj = response.usage
                session.messages[0].usage = usage_obj
            await cls._emit_lifecycle(
                session,
                "session.compaction.after",
                {
                    **lifecycle_payload,
                    "messages": [],
                    "status": result.status,
                    "summary": result.summary[:8000],
                    "original_tokens": result.original_tokens,
                    "compressed_tokens": result.compressed_tokens,
                    "tokens_saved": result.tokens_saved,
                    "fallback": bool(fallback_reason),
                },
                mode="parallel",
            )
            return result
        except BaseException as exc:
            await cls._emit_lifecycle(
                session,
                "session.compaction.error",
                {
                    **lifecycle_payload,
                    "messages": [],
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                mode="parallel",
            )
            raise

    @classmethod
    async def auto_compact(
        cls,
        session: Session,
        provider,
        model_context_limit: int,
        config: Optional[CompactionConfig] = None,
        on_start: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> Optional[CompactionResult]:
        """在每次模型请求前检查上下文溢出，并按需剪枝或压缩。

        成功压缩后同时更新 Session 查询投影并追加 Canonical Event 检查点；
        检查点不删除原始审计链，因此仍可验证压缩前后的恢复路径。
        """
        if config is None:
            config = CompactionConfig()

        # Check if overflow
        if not await cls.is_overflow(session, model_context_limit, config):
            return None

        if on_start is not None:
            estimated_tokens = sum(
                cls._estimate_message_tokens(msg) for msg in session.messages
            )
            try:
                await on_start(
                    {
                        "estimated_tokens": estimated_tokens,
                        "model_context_limit": model_context_limit,
                        "threshold_tokens": max(
                            0,
                            model_context_limit - config.reserved - config.buffer,
                        ),
                        "message_count": len(session.messages),
                    }
                )
            except Exception:
                pass

        # First try pruning (lighter operation)
        pruned = 0
        if config.prune:
            if not getattr(session, "display_messages", None):
                session.display_messages = deepcopy(session.messages)
            pruned = await cls.prune(session, config)
            if pruned > 0:
                # Save pruned session
                if hasattr(session, "messages"):
                    await SessionManager.update(
                        session.id,
                        messages=session.messages,
                        display_messages=session.display_messages,
                    )

        summary_budget = max(
            2_000,
            min(
                config.summary_context_tokens,
                max(2_000, model_context_limit - config.reserved - config.buffer),
            ),
        )

        # Then full compaction
        result = await cls.process(
            session,
            provider,
            auto=True,
            config=config,
            max_summary_tokens=summary_budget,
        )
        result.pruned_tool_outputs = pruned

        # Save compacted session
        if result.status == "success":
            await SessionManager.update(session.id, messages=session.messages)
            from .events import SessionEventStore

            SessionEventStore.compact(
                session.id,
                snapshot=SessionManager._serialize_session(session),
                owner={
                    "tenant_id": session.tenant_id,
                    "user_id": session.user_id,
                    "workspace_id": session.workspace_id,
                },
            )

        return result


# Default config instance
DEFAULT_CONFIG = CompactionConfig()
