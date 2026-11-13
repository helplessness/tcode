"""第三方 AgentBackend 可复用的一致性校验器。"""

from __future__ import annotations

from typing import Any

from ..protocol.v2 import RuntimeEventStateMachine
from ..runtime.agent_backend import AgentBackend, NormalizedRunInput, RunContext


async def collect_backend_events(
    backend: AgentBackend,
    run_input: NormalizedRunInput,
    context: RunContext,
) -> list[dict[str, Any]]:
    machine = RuntimeEventStateMachine(require_contiguous=True)
    events: list[dict[str, Any]] = []
    async for event in backend.run(run_input, context):
        machine.accept(event)
        events.append(event.model_dump(mode="json", by_alias=True))
    return events


__all__ = ["collect_backend_events"]
