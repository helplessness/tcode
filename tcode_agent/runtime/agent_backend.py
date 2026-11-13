"""公开 Agent Backend 合同与规范化运行输入。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..protocol.v2 import PROTOCOL_ID, PROTOCOL_VERSION, RuntimeEventValue, parse_runtime_event


class NormalizedMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=40)
    content: Any = ""


class NormalizedRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    parent_run_id: str = Field(default="", max_length=200)
    agent_id: str = Field(default="default", min_length=1, max_length=200)
    runtime_id: str = Field(default="tcode-native", min_length=1, max_length=200)
    model_id: str = Field(default="", max_length=300)
    messages: list[NormalizedMessage] = Field(default_factory=list, max_length=10_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    context: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    state: Any = None
    resume: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    forwarded_props: dict[str, Any] = Field(default_factory=dict, max_length=64)

    def latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.role != "user":
                continue
            if isinstance(message.content, str):
                return message.content
            if isinstance(message.content, list):
                parts = [
                    str(item.get("text") or "")
                    for item in message.content
                    if isinstance(item, dict) and str(item.get("type") or "") == "text"
                ]
                return "\n".join(part for part in parts if part)
        return ""


@dataclass
class RunContext:
    tenant_id: str
    user_id: str
    workspace_id: str
    workdir: str
    session_id: str
    run_id: str
    agent_id: str
    runtime_id: str
    request_id: str
    api_key_id: str = ""
    handoff_id: str = ""
    handoff_delivery_failed: bool = False
    # Server-owned Run state, never hydrated from model/tool arguments.
    async_resume_scope: dict[str, Any] | None = None
    async_origin: dict[str, Any] | None = None
    platform_snapshot: dict[str, Any] = field(default_factory=dict)
    platform_bridge_token: str = ""
    platform_bridge_socket: str = ""
    platform_content_blocks: list[dict[str, Any]] = field(default_factory=list)
    platform_artifacts: list[dict[str, Any]] = field(default_factory=list)
    platform_tool_state: dict[str, Any] = field(default_factory=dict)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    _sequence: int = 0

    def event(self, event_type: str, **payload: Any) -> RuntimeEventValue:
        """构造一个严格校验且在当前 run 内单调递增的 v2 事件。"""

        self._sequence += 1
        raw = {
            "protocol": PROTOCOL_ID,
            "protocol_version": PROTOCOL_VERSION,
            "type": event_type,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "sequence": self._sequence,
            "causation_id": "",
            "metadata": {
                "tcode": {
                    "request_id": self.request_id,
                    "runtime_id": self.runtime_id,
                    **(
                        {"platformCapabilities": self.platform_snapshot}
                        if self.platform_snapshot
                        else {}
                    ),
                    **({"handoff_id": self.handoff_id} if self.handoff_id else {}),
                    **(
                        {"handoff_delivery_failed": True}
                        if self.handoff_delivery_failed
                        else {}
                    ),
                }
            },
            **payload,
        }
        return parse_runtime_event(raw)

    def platform_environment(self) -> dict[str, str]:
        if not self.platform_bridge_token or not self.platform_bridge_socket:
            return {}
        return {
            "TCODE_PLATFORM_BRIDGE_TOKEN": self.platform_bridge_token,
            "TCODE_PLATFORM_BRIDGE_SOCKET": self.platform_bridge_socket,
            "TCODE_PLATFORM_CAPABILITY_SNAPSHOT": self.platform_snapshot.get(
                "snapshotId", ""
            ),
        }


@runtime_checkable
class AgentBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    async def get_capabilities(self) -> dict[str, Any]: ...

    async def run(
        self,
        run_input: NormalizedRunInput,
        context: RunContext,
    ) -> AsyncIterator[RuntimeEventValue]: ...

    async def cancel(self, run_id: str, context: RunContext) -> None: ...


__all__ = [
    "AgentBackend",
    "NormalizedMessage",
    "NormalizedRunInput",
    "RunContext",
]
