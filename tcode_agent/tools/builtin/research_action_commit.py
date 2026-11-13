"""Commit a previously previewed research action from the user's own latest text."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from ...research.action_service import ResearchActionCommitService
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class ResearchActionCommitParameters(ParameterSchema):
    action_id: str = Field(default="", max_length=180)


def _latest_user_message(messages: list[Any]) -> tuple[str, str]:
    for message in reversed(messages or []):
        if isinstance(message, dict):
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            message_id = str(message.get("id") or "").strip()
        else:
            role = str(getattr(message, "role", "") or "").strip()
            content = str(getattr(message, "content", "") or "").strip()
            message_id = str(getattr(message, "id", "") or "").strip()
        if role == "user" and content:
            return content, message_id
    return "", ""


class ResearchActionCommitTool(Tool):
    @property
    def id(self) -> str:
        return "research_action_commit"

    @property
    def description(self) -> str:
        return """Commit or cancel the current session's latest previewed research action.

Call this only when the user's latest message explicitly confirms or cancels a
Research Workspace proposal. The tool itself reads the latest authenticated
user message and decides whether it means confirm, cancel, or neither; there is
no model-controlled confirmed=true parameter. action_id is optional because the
server can resolve the latest pending action, but pass the exact action_id from
the preceding research_workspace result when available. A UI-generated hidden
action marker in the latest user message takes precedence and must match.

Never call this for an initial request such as "make this the formal topic" or
"download my pack"; first use research_workspace to produce the preview. Never
call it from a subagent. If the user's reply is ambiguous, this tool will leave
all research data unchanged and ask for an explicit “确认” or “取消”."""

    @property
    def parameters(self) -> type:
        return ResearchActionCommitParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            extra = dict(context.extra or {})
            if str(extra.get("parent_session_id") or "").strip():
                raise ValueError("subagents cannot commit user-facing research actions")
            user_text, user_message_id = _latest_user_message(context.messages)
            if not user_text:
                raise ValueError("research action commit requires a latest user message")
            parsed = ResearchActionCommitParameters(**args)
            result = ResearchActionCommitService(
                tenant_id=str(extra.get("tenant_id") or ""),
                user_id=str(extra.get("user_id") or ""),
                workspace_id=str(extra.get("workspace_id") or ""),
                session_id=str(context.session_id or ""),
                workdir=str(extra.get("workdir") or ""),
            ).commit(
                action_id=parsed.action_id,
                user_text=user_text,
                decision_message_id=user_message_id or str(context.message_id or ""),
            )
            model_result = result
            action = result.get("research_action") or {}
            saved = action.get("result") or {}
            if action.get("kind") == "research_step" and saved.get("subject_type") == "hypothesis" and saved.get("object_type"):
                model_result = {"状态": "已保存" if result.get("status") == "completed" else "尚未保存",
                    "操作": "关联假说与证据", "详情": action.get("details") or {}, "核对状态": "待核对",
                    "说明": "只保存关系判断，不批准假说，不启动计算。展示名称和中文状态，不输出字段名或内部编号。"}
            output = json.dumps(model_result, ensure_ascii=False, separators=(",", ":"))
            truncated = len(output) > 32_000
            if truncated:
                output = json.dumps(
                    {
                        "schema": result.get("schema"),
                        "action": "commit",
                        "status": result.get("status"),
                        "message": result.get("message"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            metadata: dict[str, Any] = {
                "action": "commit",
                "status": str(result.get("status") or ""),
                "truncated": truncated,
            }
            research_action = result.get("research_action")
            if isinstance(research_action, dict):
                metadata["research_action"] = research_action
            return ToolResult(
                title="Research action confirmation",
                output=output,
                metadata=metadata,
                truncated=truncated,
            )
        except (RuntimeError, ValueError) as exc:
            return ToolResult(
                title="Research action confirmation",
                output="",
                error=str(exc),
                metadata={"count": 0},
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


__all__ = ["ResearchActionCommitTool"]
