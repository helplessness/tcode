"""TCode Web v1 的权威传输模型。"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model
from ..presentation.canvas import CanvasContentBlock
from ..presentation.intelligent_page import PageContentBlock
from ..presentation.research_compare import ResearchCompareBlock


PROTOCOL_ID = "tcode.web.protocol"
PROTOCOL_VERSION = "1.0.0"


class ProtocolModel(BaseModel):
    """默认拒绝未声明字段，防止后端悄悄改变协议。"""

    model_config = ConfigDict(extra="forbid")


ToolState: TypeAlias = Literal[
    "pending", "running", "completed", "error", "skipped", "interrupted"
]


class TokenUsage(ProtocolModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ToolCall(ProtocolModel):
    tool_name: str
    arguments: dict[str, Any]
    call_id: Optional[str] = None
    state: Optional[ToolState] = None
    output: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    compacted_at: Optional[float] = None


class ToolResult(ProtocolModel):
    title: str
    output: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    output_path: Optional[str] = None
    error: Optional[str] = None


class GuidanceMessageMetadata(ProtocolModel):
    """追加指导在公开消息协议中的安全投影。"""

    id: str
    sequence: int
    mode: Literal["steer", "queue"]
    target_turn_id: str
    applied_safe_point: str
    contract_revision_id: str = ""


class SessionMessage(ProtocolModel):
    id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    created_at: Optional[float] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    parent_id: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    finish: Optional[str] = None
    usage: Optional[TokenUsage] = None
    cost: float = 0
    process_notes: list[dict[str, Any]] = Field(default_factory=list)
    presentation: Optional[str] = None
    content_blocks: list[dict[str, Any]] = Field(default_factory=list)
    research_grounding: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    input_images: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    summary: bool = False
    compacted_at: Optional[float] = None


class TaskRuntimeItem(ProtocolModel):
    runtime_id: str
    runtime_kind: str
    session_id: Optional[str] = None
    status: str
    phase: str
    source: Optional[str] = None
    title: Optional[str] = None
    parent_runtime_id: Optional[str] = None
    external_id: Optional[str] = None
    tool_name: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: float
    completed_at: Optional[float] = None
    event_count: Optional[int] = None
    last_event_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskRuntimeEvent(ProtocolModel):
    event_id: str
    runtime_id: str
    runtime_kind: str
    session_id: str = ""
    status: str
    phase: str
    source: str = ""
    previous_status: str = ""
    title: str = ""
    external_id: str = ""
    tool_name: str = ""
    created_at: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class LegacyRuntimeSummary(ProtocolModel):
    total: int
    counts: dict[str, int]
    tasks: list[dict[str, Any]]
    recent_events: list[dict[str, Any]]


class SessionTaskRuntimeSummary(ProtocolModel):
    session_id: str
    session_outcome_status: str = ""
    session_outcome_updated_at: float = 0
    total: int = 0
    active_total: int
    counts: dict[str, int]
    active_counts: dict[str, int]
    resolved_counts: dict[str, int] = Field(default_factory=dict)
    items: list[TaskRuntimeItem] = Field(default_factory=list)
    resolved_items: list[TaskRuntimeItem] = Field(default_factory=list)
    recent_events: list[TaskRuntimeEvent] = Field(default_factory=list)
    updated_at: float
    legacy_runtime: Optional[LegacyRuntimeSummary] = None


class SessionSummary(ProtocolModel):
    session_id: str
    parent_id: Optional[str] = None
    title: str
    agent_name: str
    status: str = ""
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    workdir: str = ""
    workdir_name: str = ""
    created_at: float
    updated_at: float
    message_count: int
    message_preview: str = ""
    async_completion_unread_count: int = 0
    usage: TokenUsage
    task_runtime: SessionTaskRuntimeSummary
    is_archived: bool = False
    archived_at: Optional[float] = None
    mode: str = "general"
    research_id: str = ""
    mode_source: str = "default"
    archive_cause: str = ""
    archive_locked: bool = False


class SessionDetail(ProtocolModel):
    session_id: str
    parent_id: Optional[str] = None
    title: str = ""
    agent_name: str = ""
    status: str = ""
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    workdir: str = ""
    workdir_name: str = ""
    created_at: float = 0
    updated_at: float = 0
    is_archived: bool = False
    archived_at: Optional[float] = None
    mode: str = "general"
    research_id: str = ""
    mode_source: str = "default"
    archive_cause: str = ""
    archive_locked: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[SessionMessage] = Field(default_factory=list)


class SessionUsageNode(ProtocolModel):
    session_id: str
    parent_id: str = ""
    title: str
    agent_name: str
    status: str = ""
    created_at: float
    updated_at: float
    message_count: int
    own_usage: TokenUsage
    total_usage: TokenUsage
    own_cost: float
    cost: float
    by_agent: dict[str, TokenUsage]
    children: list[SessionUsageNode] = Field(default_factory=list)


class SessionUsageSummary(ProtocolModel):
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    total_usage: TokenUsage
    cost: float
    by_agent: dict[str, TokenUsage]
    sessions: list[SessionUsageNode]
    verification_accounting: dict[str, Any] = Field(default_factory=dict)


class AsyncCompletionEvent(ProtocolModel):
    id: str
    kind: Literal["async_completion"]
    session_id: str
    origin_turn_id: Optional[str] = None
    origin_request_id: Optional[str] = None
    identifier: str
    job_id: Optional[str] = None
    target_server: Optional[str] = None
    target_tool: Optional[str] = None
    status: str
    issue_state: Optional[str] = None
    message: Optional[str] = None
    result_summary: Optional[str] = None
    error_summary: Optional[str] = None
    output_path: Optional[str] = None
    output_abs_path: Optional[str] = None
    artifacts: list[str] = Field(default_factory=list)
    can_continue: Optional[bool] = None
    continuation_policy: Optional[Literal["notify", "auto", "store_only"]] = None
    delivery_status: Optional[str] = None
    continuation_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    read_at: Optional[str] = None


class QuestionPayload(BaseModel):
    """问题工具允许业务侧扩展 metadata 等字段。"""

    model_config = ConfigDict(extra="allow")

    question: str
    type: str = "custom"
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    display: Optional[str] = None


class ToolUsingEvent(ProtocolModel):
    type: Literal["status"]
    status: Literal["using_tool"]
    session_id: str = ""
    tool_name: str
    tool_args: dict[str, Any]
    call_id: str = ""
    message_id: str = ""
    content: str = ""


class ToolResultEvent(ProtocolModel):
    type: Literal["tool_result"]
    session_id: str
    message_id: str = ""
    tool_call: ToolCall


class PermissionRequestEvent(ProtocolModel):
    type: Literal["permission"]
    request_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]


class QuestionRequestEvent(ProtocolModel):
    type: Literal["question"]
    request_id: str
    session_id: str
    question: QuestionPayload


class QuestionTimeoutEvent(ProtocolModel):
    type: Literal["question_timeout"]
    request_id: str
    session_id: str
    answer: str = ""


class UsageStatusEvent(ProtocolModel):
    type: Literal["status"]
    status: Literal["usage"]
    usage: TokenUsage


_ChatToolEventValue = Union[
    ToolUsingEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    QuestionRequestEvent,
    QuestionTimeoutEvent,
    UsageStatusEvent,
]


class ChatToolEvent(RootModel[_ChatToolEventValue]):
    pass


PROTOCOL_MODELS: tuple[type[BaseModel], ...] = (
    ResearchCompareBlock,
    CanvasContentBlock,
    PageContentBlock,
    TokenUsage,
    ToolCall,
    ToolResult,
    GuidanceMessageMetadata,
    SessionMessage,
    TaskRuntimeItem,
    TaskRuntimeEvent,
    LegacyRuntimeSummary,
    SessionTaskRuntimeSummary,
    SessionSummary,
    SessionDetail,
    SessionUsageNode,
    SessionUsageSummary,
    AsyncCompletionEvent,
    QuestionPayload,
    ToolUsingEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    QuestionRequestEvent,
    QuestionTimeoutEvent,
    UsageStatusEvent,
    ChatToolEvent,
)


def protocol_schema_document() -> dict[str, Any]:
    """返回包含所有公开模型的稳定 JSON Schema 文档。"""

    fields = {
        f"contract_{index}": (model, ...)
        for index, model in enumerate(PROTOCOL_MODELS)
    }
    root_model = create_model("TCodeWebProtocol", **fields)
    schema = root_model.model_json_schema(ref_template="#/$defs/{model}")
    schema["$id"] = f"https://tcode.local/schemas/web-protocol-{PROTOCOL_VERSION}.json"
    schema["x-contract-id"] = PROTOCOL_ID
    schema["x-protocol-version"] = PROTOCOL_VERSION
    schema["x-exported-models"] = [model.__name__ for model in PROTOCOL_MODELS]
    schema.pop("properties", None)
    schema.pop("required", None)
    schema["title"] = "TCode Web Protocol"
    return schema
