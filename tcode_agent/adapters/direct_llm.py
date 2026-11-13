"""最小的、无 Provider 依赖的参考 Backend。"""

from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

from ..protocol.v2 import RuntimeEventValue
from ..runtime.agent_backend import NormalizedRunInput, RunContext


class DirectEchoBackend:
    """公开 Adapter 示例和离线互操作测试 Backend。"""

    backend_id = "direct-echo"

    async def get_capabilities(self) -> dict[str, object]:
        return {
            "text": True,
            "tools": False,
            "state": True,
            "cancel": True,
            "resume": False,
            "guidanceMode": "unsupported",
            "permissionAuthority": "tcode",
            "release": "stable",
        }

    async def run(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        text = run_input.latest_user_text()
        reply = f"Echo: {text}" if text else "Echo"
        message_id = f"msg_{uuid.uuid4().hex}"
        yield context.event("message.started", message_id=message_id, role="assistant")
        for index in range(0, len(reply), 16):
            if context.cancelled.is_set():
                return
            delta = reply[index : index + 16]
            yield context.event("message.delta", message_id=message_id, delta=delta)
            await asyncio.sleep(0)
        yield context.event(
            "message.finished",
            message={"message_id": message_id, "role": "assistant", "content": reply},
        )

    async def cancel(self, run_id: str, context: RunContext) -> None:
        context.cancelled.set()


__all__ = ["DirectEchoBackend"]
