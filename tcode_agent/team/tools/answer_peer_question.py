"""Tool: answer_peer_question - close a formal peer collaboration round with one structured response."""

from typing import Any

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..peer_comm import answer_peer_question


class AnswerPeerQuestionParameters(ParameterSchema):
    request_id: str
    summary: str
    confidence: float = 0.5
    next_action: str = ""
    blockers: list[str] = []


class AnswerPeerQuestionTool(Tool):
    @property
    def id(self) -> str:
        return "answer_peer_question"

    @property
    def description(self) -> str:
        return """回复一条正式 peer collaboration 请求。

约束：
- 只能由目标 Worker 回复
- 每个 request_id 只能回复一次
- 回复必须是结构化答复
- 回复后该轮自动结束，并抄送给 Manager 与发起方
"""

    @property
    def parameters(self) -> type:
        return AnswerPeerQuestionParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = AnswerPeerQuestionParameters(**args)
        thread = await answer_peer_question(
            responder_session_id=context.session_id,
            request_id=parsed.request_id,
            summary=parsed.summary,
            confidence=parsed.confidence,
            next_action=parsed.next_action,
            blockers=parsed.blockers,
            workdir=(context.extra or {}).get("workdir", "") or "",
        )
        response = thread.get("response") or {}
        return ToolResult(
            title=f"answer_peer_question → {thread['sender_worker']}",
            output=(
                "已完成正式协作答复，本轮已关闭。\n"
                f"request_id: {thread['request_id']}\n"
                f"status: {thread['status']}\n"
                f"summary: {response.get('summary', '')}\n"
                f"confidence: {response.get('confidence', 0)}\n"
                f"next_action: {response.get('next_action', '') or '-'}"
            ),
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry

        ToolRegistry.register(cls())
