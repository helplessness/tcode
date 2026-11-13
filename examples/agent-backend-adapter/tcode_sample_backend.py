"""A transport-independent TCode AgentBackend example."""

from __future__ import annotations

import uuid
from typing import AsyncIterator

from tcode_agent.protocol.v2 import RuntimeEventValue
from tcode_agent.runtime.agent_backend import NormalizedRunInput, RunContext


class SampleAgentBackend:
    backend_id = "sample-agent"

    async def get_capabilities(self) -> dict[str, object]:
        return {
            "text": True,
            "tools": False,
            "state": False,
            "cancel": True,
            "resume": False,
            "permissionAuthority": "tcode",
            "release": "experimental",
        }

    async def run(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]:
        message_id = f"msg_{uuid.uuid4().hex}"
        reply = f"Sample backend received: {run_input.latest_user_text()}"
        yield context.event("message.started", message_id=message_id, role="assistant")
        if not context.cancelled.is_set():
            yield context.event("message.delta", message_id=message_id, delta=reply)
            yield context.event(
                "message.finished",
                message={"message_id": message_id, "role": "assistant", "content": reply},
            )

    async def cancel(self, run_id: str, context: RunContext) -> None:
        context.cancelled.set()
