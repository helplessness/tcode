"""同步与流式入口共用的 Turn 驱动和等价投影合同。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class TurnProjection:
    """描述一次 Turn 对调用方可观察的稳定终态。

    投影只保存消息角色、正文、工具身份和最终结果状态，不保存随机消息 ID、
    流式分片时序，因而可直接比较两次独立同步/流式执行的业务终态。
    """

    messages: tuple[dict[str, Any], ...]
    final_content: str
    outcome_status: str

    def to_dict(self) -> dict[str, Any]:
        """把不可变投影转换为便于合同测试和诊断输出的字典。"""

        payload = asdict(self)
        payload["messages"] = [dict(item) for item in self.messages]
        return payload


class TurnDriver:
    """以一个异步消息驱动同时服务同步和流式 Agent 入口。

    ``stream_factory`` 是唯一真正推进模型、工具、检查点与最终收尾的状态机。
    同步入口只消费同一消息序列并返回最后一条 Assistant 消息，不再维护第二
    份 Turn 循环。
    """

    def __init__(
        self,
        stream_factory: Callable[..., AsyncIterator[Any]],
    ) -> None:
        """绑定唯一 Turn 状态机工厂。

        参数:
            stream_factory: 接收用户输入和可选合同、返回消息异步迭代器的函数。
        """

        self._stream_factory = stream_factory

    async def stream(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator[Any]:
        """逐条转发唯一状态机产生的过程消息和最终消息。"""

        async for message in self._stream_factory(
            user_message,
            output_format,
            outcome_contract,
        ):
            yield message

    async def complete(
        self,
        user_message: str,
        output_format: Optional[dict[str, Any]] = None,
        outcome_contract: Optional[dict[str, Any]] = None,
    ) -> Any:
        """消费同一状态机并返回最终 Assistant 消息。

        状态机若未产生 Assistant 消息说明违反 Turn 合同，此时明确失败，避免
        把工具或内部预加载消息误当成最终答复。
        """

        final_message: Any = None
        async for message in self.stream(
            user_message,
            output_format,
            outcome_contract,
        ):
            if str(getattr(message, "role", "") or "") == "assistant":
                final_message = message
        if final_message is None:
            raise RuntimeError("Turn driver completed without an assistant message")
        return final_message

    @staticmethod
    def project(
        messages: list[Any] | tuple[Any, ...],
        *,
        outcome_status: str = "",
    ) -> TurnProjection:
        """把一次 Turn 的消息序列规范化为同步/流式等价投影。"""

        projected: list[dict[str, Any]] = []
        final_message: Any = None
        for message in messages:
            role = str(getattr(message, "role", "") or "")
            if role == "assistant":
                final_message = message
            tool_calls = []
            for tool_call in list(getattr(message, "tool_calls", []) or []):
                state_value = getattr(getattr(tool_call, "state", None), "value", None)
                tool_calls.append(
                    {
                        "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
                        "call_id": str(getattr(tool_call, "call_id", "") or ""),
                        "state": str(state_value or ""),
                    }
                )
            projected.append(
                {
                    "role": role,
                    "content": str(getattr(message, "content", "") or ""),
                    "tool_calls": tool_calls,
                }
            )
        return TurnProjection(
            messages=tuple(projected),
            final_content=str(getattr(final_message, "content", "") or ""),
            outcome_status=str(outcome_status or ""),
        )


def turn_driver_contract_document() -> dict[str, Any]:
    """返回第三批 Turn 单轨合同的无业务载荷诊断文档。"""

    return {
        "schema_version": 1,
        "contract_id": "tcode.turn-driver.v1",
        "single_driver": True,
        "sync_behavior": "consume-canonical-stream",
        "stream_behavior": "project-canonical-stream",
        "projection_ignores": [
            "generated_message_ids",
            "delta_timing",
            "transport_chunk_boundaries",
        ],
        "terminal_states": [
            "completed",
            "partial",
            "blocked",
            "failed",
            "interrupted",
            "cancelled",
        ],
    }


__all__ = ["TurnDriver", "TurnProjection", "turn_driver_contract_document"]
