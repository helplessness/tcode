"""AG-UI HTTP 输入模型与 TCode 扩展边界。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..runtime.agent_backend import NormalizedMessage, NormalizedRunInput


class ClientCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mcp_apps: bool = Field(default=False, alias="mcpApps")
    iframe: bool = False
    files: bool = True
    images: bool = True


class TCodeForwardedProps(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace_id: str = Field(default="workspace-1", alias="workspaceId", min_length=1, max_length=128)
    runtime_id: str = Field(default="tcode-native", alias="runtimeId", min_length=1, max_length=200)
    model_id: str = Field(default="", alias="modelId", max_length=300)
    execution_profile: Literal["fast", "standard", "deep", "orchestrated"] | None = Field(default=None, alias="executionProfile")
    scheduler_mode: Literal["supervisor", "chain", "swarm", "auto_swarm_builder"] | None = Field(default=None, alias="schedulerMode")
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = Field(default=None, alias="reasoningEffort")
    disconnect_policy: Literal["cancel", "detach"] = Field(default="cancel", alias="disconnectPolicy")
    client_request_id: str = Field(default="", alias="clientRequestId", max_length=200)
    client_capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities, alias="clientCapabilities")

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("workspaceId must be a public workspace identifier")
        return value


class ForwardedProps(BaseModel):
    model_config = ConfigDict(extra="allow")

    tcode: TCodeForwardedProps = Field(default_factory=TCodeForwardedProps)


class RunAgentInput(BaseModel):
    """匹配 @ag-ui/core 0.0.59，未知标准字段向前兼容。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1, max_length=200)
    run_id: str = Field(alias="runId", min_length=1, max_length=200)
    parent_run_id: str = Field(default="", alias="parentRunId", max_length=200)
    state: Any = None
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    context: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    forwarded_props: ForwardedProps = Field(default_factory=ForwardedProps, alias="forwardedProps")
    resume: list[dict[str, Any]] = Field(default_factory=list, max_length=128)

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("messages must contain objects")
            message_id = str(message.get("id") or "").strip()
            role = str(message.get("role") or "").strip()
            if not message_id or len(message_id) > 200:
                raise ValueError("every message requires a bounded id")
            if role not in {"developer", "system", "user", "assistant", "tool", "reasoning", "activity"}:
                raise ValueError("unsupported message role")
        return messages

    def normalized(self, agent_id: str) -> NormalizedRunInput:
        normalized_messages = [
            NormalizedMessage(
                id=str(item.get("id") or ""),
                role=str(item.get("role") or ""),
                content=item.get("content", ""),
            )
            for item in self.messages
            if str(item.get("role") or "") in {"user", "assistant", "system", "developer", "tool"}
        ]
        tcode = self.forwarded_props.tcode
        return NormalizedRunInput(
            thread_id=self.thread_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
            agent_id=agent_id,
            runtime_id=tcode.runtime_id,
            model_id=tcode.model_id,
            messages=normalized_messages,
            tools=self.tools,
            context=self.context,
            state=self.state,
            resume=self.resume,
            forwarded_props=self.forwarded_props.model_dump(mode="json", by_alias=True),
        )


__all__ = ["ClientCapabilities", "ForwardedProps", "RunAgentInput", "TCodeForwardedProps"]
