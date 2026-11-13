"""Tool: ask_peer_question - open a formal single-round peer collaboration request."""

from typing import Any

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..peer_comm import ask_peer_question


class AskPeerQuestionParameters(ParameterSchema):
    target_worker: str
    reason: str
    question: str


class AskPeerQuestionTool(Tool):
    @property
    def id(self) -> str:
        return "ask_peer_question"

    @property
    def description(self) -> str:
        return """向白名单内的其他 Worker 发起一轮正式协作请求。

协议：
- 只能发起单轮问题
- 目标 Worker 只能用 answer_peer_question 回复一次结构化答复
- 所有请求会自动抄送给 Manager
- 会受到白名单、reason、轮次和超时限制
"""

    @property
    def parameters(self) -> type:
        return AskPeerQuestionParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = AskPeerQuestionParameters(**args)
        thread = await ask_peer_question(
            sender_session_id=context.session_id,
            target_worker_name=parsed.target_worker,
            question=parsed.question,
            reason=parsed.reason,
            workdir=(context.extra or {}).get("workdir", "") or "",
        )
        return ToolResult(
            title=f"ask_peer_question → {thread['target_worker']}",
            output=(
                "已发起正式协作请求，并已自动抄送 Manager。\n"
                f"request_id: {thread['request_id']}\n"
                f"from: {thread['sender_worker']}\n"
                f"to: {thread['target_worker']}\n"
                f"reason: {thread['reason']}\n"
                f"status: {thread['status']}\n"
                f"round: {thread['round_index']}/{thread['max_rounds']}"
            ),
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry

        ToolRegistry.register(cls())
