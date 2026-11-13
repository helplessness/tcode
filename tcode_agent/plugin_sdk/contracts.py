"""TCode Python Plugin SDK v1 的稳定公开契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol


PLUGIN_API_VERSION = "1.0"
PLUGIN_SDK_VERSION = "1.6.0"
PLUGIN_SDK_COMPATIBILITY = ">=1.0.0,<2.0.0"

ToolHandler = Callable[
    [Mapping[str, Any], "ToolCallContext"],
    "ToolOutput | Awaitable[ToolOutput]",
]
PromptRenderer = Callable[
    ["PromptRenderContext"],
    "str | Awaitable[str]",
]
EventHandler = Callable[["EventEnvelope"], "None | Awaitable[None]"]
ProviderHandler = Callable[
    ["ProviderRequest"],
    "ProviderResponse | Awaitable[ProviderResponse]",
]
VerifierHandler = Callable[[Mapping[str, Any]], "VerifierOutput"]


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """声明插件身份和展示元数据。"""

    name: str
    version: str
    description: str = ""
    author: str | None = None
    homepage: str | None = None
    plugin_type: tuple[str, ...] = ()


class PluginHealthStatus(str, Enum):
    """列出公开生命周期允许返回的健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class PluginHealth:
    """返回插件健康摘要和无业务秘密的诊断细节。"""

    status: PluginHealthStatus
    summary: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """声明一个始终进入 Host Policy 与 Permission 管线的工具贡献。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    version: str = "1.0.0"
    side_effect_level: Literal["none", "read", "write", "process"] = "none"
    result_budget_tokens: int = 2048
    on_demand: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    """向工具处理器暴露最小调用身份。"""

    session_id: str
    message_id: str
    agent: str
    call_id: str | None = None
    workspace: Any | None = field(default=None, repr=False, compare=False)
    ontology: Any | None = field(default=None, repr=False, compare=False)
    artifacts: ArtifactAPI | None = field(default=None, repr=False, compare=False)
    # Host-selected identifiers and paths only; never authorization callbacks or secrets.
    execution: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    request_input: Any | None = field(default=None, repr=False, compare=False)
    abort: Any | None = field(default=None, repr=False, compare=False)
    user_messages: tuple[str, ...] = ()


class ArtifactAPI(Protocol):
    """Per-call approved workspace text and create-only versioned snapshots.

    Paths from artifact_query must first be resolved through that existing tool.
    References are (path, sha256); no remote downloading or job submission.
    """

    async def read_text(self, path: str, *, sha256: str = "", snapshot: bool = False,
                        max_bytes: int = 192 * 1024) -> Mapping[str, Any]: ...

    async def publish_json(self, value: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """声明公开工具处理器返回的文本结果。"""

    title: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifierOutput:
    """返回一个有界验证事实；证明等级由 Host Registry 签发。"""

    status: Literal["pass", "fail", "uncertain", "error"]
    reason_code: str
    observations: tuple[str, ...] = ()
    artifact_refs: tuple[Mapping[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class VerifierDefinition:
    """声明一个由 Host 限时执行并限制证明强度的 Verifier。"""

    verifier_id: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    handler: VerifierHandler
    requested_max_proof_level: Literal["P0", "P1", "P2", "P3", "P4"] = "P1"
    timeout_seconds: float = 5.0
    required_capabilities: tuple[str, ...] = ()
    isolation_policy: Literal["controlled-host"] = "controlled-host"


@dataclass(frozen=True, slots=True)
class PromptContribution:
    """声明一段有顺序、有预算的工作区 Prompt 贡献。"""

    name: str
    text: str = ""
    renderer: PromptRenderer | None = None
    version: str = "1.0.0"
    scope: Literal["workspace"] = "workspace"
    order: int = 100
    token_budget: int = 1024
    required: bool = False
    agents: tuple[str, ...] = ()
    structured_output_safe: bool = False


@dataclass(frozen=True, slots=True)
class PromptRenderContext:
    """向动态 Prompt Renderer 提供不透明工作区和有界变量。"""

    workspace_id: str
    agent: str = ""
    variables: Mapping[str, Any] = field(default_factory=dict)


class PublicEvent(str, Enum):
    """API v1 暴露的带版本、只观察公共事件。"""

    CONFIG_LOADED_V1 = "tcode.event.config.loaded@1"
    SESSION_STARTED_V1 = "tcode.event.session.started@1"
    SESSION_BOOTSTRAPPED_V1 = "tcode.event.session.bootstrapped@1"
    MEMORY_RECALLED_V1 = "tcode.event.memory.recalled@1"
    MEMORY_WRITTEN_V1 = "tcode.event.memory.written@1"
    MEMORY_DELETED_V1 = "tcode.event.memory.deleted@1"
    MEMORY_PROMOTED_V1 = "tcode.event.memory.promoted@1"
    MEMORY_OPERATION_ERROR_V1 = "tcode.event.memory.operation-error@1"
    SESSION_COMPACTION_BEFORE_V1 = "tcode.event.session.compaction-before@1"
    SESSION_COMPACTION_AFTER_V1 = "tcode.event.session.compaction-after@1"
    SESSION_COMPACTION_ERROR_V1 = "tcode.event.session.compaction-error@1"
    SESSION_DELETE_BEFORE_V1 = "tcode.event.session.delete-before@1"
    SESSION_DELETE_AFTER_V1 = "tcode.event.session.delete-after@1"
    SESSION_DELETE_ERROR_V1 = "tcode.event.session.delete-error@1"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """向公共事件处理器投递不允许写回 Host 的事件快照。"""

    event: PublicEvent
    source: str
    plugin_instance_id: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """声明公开 Provider 收到的版本化请求。

    Host 不会向请求中放入数据库、Secret 或内部 Runtime
    对象；具体能力只由 ``operation`` 和可序列化输入表达。
    """

    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """声明公开 Provider 返回的有界结果。"""

    output: Mapping[str, Any] = field(default_factory=dict)
    status: Literal["ok", "error", "retry"] = "ok"
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """声明一个不直接获得 Host 权限的 Provider 能力。"""

    name: str
    version: str
    operations: tuple[str, ...]
    handler: ProviderHandler
    description: str = ""


class ProviderProtocol(Protocol):
    """定义 SDK v1 稳定 Provider 调用协议。

    该协议只冻结数据契约，不表示插件可以绕过宿主
    Policy、Permission 或 Sandbox 直接替换生产 Provider。
    """

    definition: ProviderDefinition

    async def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """在实现自身边界内处理一次 Provider 请求。"""

        ...


class ToolAPI(Protocol):
    """声明工具贡献注册和受控 Host 工具调用能力。"""

    def register(self, definition: ToolDefinition) -> None:
        """登记一个必须进入 Host 安全管线的工具。"""

        ...

    async def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        timeout: int = 300,
    ) -> Mapping[str, Any]:
        """通过 Host 权限、审批和 Sandbox 管线调用一个已授权工具。"""

        ...


class PromptAPI(Protocol):
    """声明生产 Prompt 贡献能力。"""

    def contribute(self, contribution: PromptContribution) -> None:
        """登记一个工作区级 Prompt Contribution。"""

        ...


class EventAPI(Protocol):
    """声明只观察公共事件订阅能力。"""

    def subscribe(self, event: PublicEvent, handler: EventHandler) -> None:
        """订阅一个明确带版本的公共事件。"""

        ...


class VerifierAPI(Protocol):
    """声明验证器贡献能力。"""

    def register(self, definition: VerifierDefinition) -> None:
        """登记验证器定义；最终证明上限由宿主策略决定。"""

        ...


class MemoryAPI(Protocol):
    """声明受 Manifest 权限与 Host Scope 约束的语义记忆能力。"""

    async def query(
        self,
        *,
        scope: str,
        scope_key: str = "",
        query: str = "",
        limit: int = 20,
        level: str = "l1",
        filters: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """召回指定 Scope 的记忆。"""
        ...

    async def put(
        self,
        *,
        scope: str,
        scope_key: str = "",
        content: str,
        tags: tuple[str, ...] | list[str] = (),
        structured: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> Mapping[str, Any]:
        """幂等或非幂等写入指定 Scope。"""
        ...

    async def delete(
        self,
        *,
        scope: str,
        scope_key: str = "",
        memory_id: str,
    ) -> Mapping[str, Any]:
        """删除指定 Scope 中的一条记忆。"""
        ...

    async def promote(
        self,
        *,
        source_scope: str,
        source_scope_key: str = "",
        memory_id: str,
        target_scope: str,
        target_scope_key: str = "",
        move: bool = False,
    ) -> Mapping[str, Any]:
        """把记忆复制或移动到另一个 Scope。"""
        ...

    async def cleanup_scope(
        self,
        *,
        scope: str,
        scope_key: str = "",
    ) -> Mapping[str, Any]:
        """清理完整 Scope；需要 maintenance 权限。"""
        ...


class WorkspaceAPI(Protocol):
    """声明受 Manifest 文件权限约束的工作区操作。"""

    @property
    def workdir(self) -> str: ...

    def resolve_read_path(self, path: str) -> str:
        """校验读权限并返回规范绝对路径。"""

        ...

    def resolve_write_path(self, path: str) -> str:
        """校验写权限并返回规范绝对路径。"""

        ...

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """在 Manifest 读权限内读取文本。"""

        ...

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> None:
        """在 Manifest 写权限内写入完整文本。"""

        ...

    def list_dir(self, path: str) -> tuple[str, ...]:
        """在 Manifest 读权限内列出目录项。"""

        ...


class ProcessAPI(Protocol):
    """声明受 Manifest 进程与目录权限约束的命令执行接口。"""

    async def run(
        self,
        command: str,
        args: tuple[str, ...] = (),
        *,
        cwd: str | None = None,
        timeout: int = 60,
        profile: str | None = None,
    ) -> Mapping[str, Any]:
        """在 Manifest 进程权限和 Host Sandbox Profile 内执行命令。"""

        ...


class NetworkAPI(Protocol):
    """声明只通过 Host 受控出口完成的 HTTP 请求接口。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        timeout: int = 30,
    ) -> Mapping[str, Any]:
        """按 Manifest 域名白名单请求网络资源并返回有界结果。"""

        ...


class EmbeddingAPI(Protocol):
    """宿主固定配置的 embedding 模型；不接受插件指定 URL、模型或密钥。"""

    async def describe(self) -> Mapping[str, Any]: ...

    async def embed(self, texts: list[str], *, expected_signature: str,
                    kind: str = "document") -> Mapping[str, Any]: ...


class PluginInstance(Protocol):
    """声明当前加载实例的只读身份字段。"""

    name: str
    version: str
    instance_id: str
    api_version: str
    requested_trust: str


class PluginScope(Protocol):
    """声明当前插件的工作区 Scope。"""

    kind: str
    workspace_id: str


class PluginLogger(Protocol):
    """声明不会暴露 Host Logger 对象的日志门面。"""

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录调试级消息。"""

        ...

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录信息级消息。"""

        ...

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录警告级消息。"""

        ...

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录错误级消息。"""

        ...


class PluginContext(Protocol):
    """声明插件 ``start`` 生命周期可使用的完整公开 Context。"""

    instance: PluginInstance
    scope: PluginScope
    logger: PluginLogger
    tools: ToolAPI
    prompt: PromptAPI
    events: EventAPI
    verifiers: VerifierAPI
    memory: MemoryAPI | None
    workspace: WorkspaceAPI
    process: ProcessAPI
    network: NetworkAPI
    embeddings: EmbeddingAPI | None
    config: Mapping[str, Any]


__all__ = [
    "ArtifactAPI",
    "EmbeddingAPI",
    "EventAPI",
    "EventEnvelope",
    "EventHandler",
    "PLUGIN_API_VERSION",
    "PLUGIN_SDK_COMPATIBILITY",
    "PLUGIN_SDK_VERSION",
    "NetworkAPI",
    "MemoryAPI",
    "PluginContext",
    "PluginHealth",
    "PluginHealthStatus",
    "PluginInstance",
    "PluginLogger",
    "PluginMetadata",
    "PluginScope",
    "ProcessAPI",
    "ProviderDefinition",
    "ProviderHandler",
    "ProviderProtocol",
    "ProviderRequest",
    "ProviderResponse",
    "PromptAPI",
    "PromptContribution",
    "PromptRenderContext",
    "PublicEvent",
    "ToolAPI",
    "ToolCallContext",
    "ToolDefinition",
    "ToolOutput",
    "VerifierAPI",
    "VerifierDefinition",
    "VerifierHandler",
    "VerifierOutput",
    "WorkspaceAPI",
]
