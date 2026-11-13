"""定义宿主内部 Provider API v1 的窄合同与安全边界。

本模块不会通过 ``PluginContext`` 暴露；生产 Consumer 和第三方插件均不能
据此替换宿主 Provider。它只为宿主内部调用固定请求、响应、Owner 和权限语义。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


PROVIDER_API_INTERNAL_VERSION = "1.0-internal"
_TRUSTED_LEVELS = frozenset({"builtin", "trusted"})
_OWNER_FIELDS = ("tenant_id", "user_id", "workspace_id")


class ProviderAccessError(PermissionError):
    """表示 Provider 调用未通过信任、Owner、权限或预算校验。"""

    def __init__(self, code: str, detail: str) -> None:
        """保存稳定错误码和可公开的拒绝原因。"""

        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"[{self.code}] {self.detail}")


class ProviderInvocationError(RuntimeError):
    """表示 Provider 调用失败，且不泄露上游异常消息。"""

    def __init__(
        self,
        code: str,
        *,
        provider: str,
        exception_type: str,
        retryable: bool,
    ) -> None:
        """保存 Provider、异常类型与可重试标记。"""

        self.code = str(code)
        self.provider = str(provider or "unknown")
        self.exception_type = str(exception_type or "Exception")
        self.retryable = bool(retryable)
        super().__init__(
            f"[{self.code}] provider {self.provider!r} failed "
            f"with {self.exception_type}"
        )


@dataclass(frozen=True, slots=True)
class ProviderInvocationContext:
    """保存由宿主提供的 Owner、信任、权限和输出预算。"""

    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str = ""
    request_id: str = ""
    trust: str = "trusted"
    permissions: frozenset[str] = frozenset()
    max_output_tokens: int = 0


def require_provider_access(
    context: ProviderInvocationContext,
    *,
    permissions: Sequence[str],
    require_owner: bool = True,
) -> None:
    """在调用 Provider 前统一校验信任、Owner 和所需权限。"""

    trust = str(context.trust or "").strip().lower()
    if trust not in _TRUSTED_LEVELS:
        raise ProviderAccessError(
            "provider.access.trust",
            "provider API accepts only builtin or trusted host contexts",
        )
    if require_owner:
        missing_owner = [
            field
            for field in _OWNER_FIELDS
            if not str(getattr(context, field, "") or "").strip()
        ]
        if missing_owner:
            raise ProviderAccessError(
                "provider.access.owner",
                "provider owner context is incomplete: " + ", ".join(missing_owner),
            )
    granted = frozenset(str(item or "").strip() for item in context.permissions)
    missing_permissions = [item for item in permissions if item not in granted]
    if missing_permissions:
        raise ProviderAccessError(
            "provider.access.permission",
            "missing provider permissions: " + ", ".join(missing_permissions),
        )


@dataclass(frozen=True, slots=True)
class LLMProviderMessage:
    """描述不含宿主私有对象的 LLM 消息。"""

    role: str
    content: str
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    tool_call_id: str = ""


@dataclass(frozen=True, slots=True)
class LLMProviderTool:
    """描述发送给模型的工具名、说明和参数 Schema。"""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LLMProviderRequest:
    """描述一次受宿主预算约束的 LLM 请求。"""

    messages: tuple[LLMProviderMessage, ...]
    tools: tuple[LLMProviderTool, ...] = ()
    model: str = ""
    temperature: float | None = None
    max_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMProviderResult:
    """描述经过标准化的 LLM 文本、工具调用和用量结果。"""

    content: str
    tool_calls: tuple[Mapping[str, Any], ...]
    finish_reason: str
    usage: Mapping[str, int]


@runtime_checkable
class LLMProviderContract(Protocol):
    """约束宿主内部 LLM 完整与流式调用接口。"""

    @property
    def name(self) -> str:
        """返回 Provider 的稳定名称。"""
        ...

    async def complete(
        self,
        request: LLMProviderRequest,
        context: ProviderInvocationContext,
    ) -> LLMProviderResult:
        """执行一次完整响应调用。"""
        ...

    def stream(
        self,
        request: LLMProviderRequest,
        context: ProviderInvocationContext,
    ) -> AsyncIterator[LLMProviderResult]:
        """返回标准化的异步流式结果。"""
        ...


class LLMProviderAdapter:
    """把当前 LLMProvider 映射为宿主内部的文本调用合同。"""

    def __init__(self, provider: Any) -> None:
        """绑定当前宿主 LLMProvider 实例。"""

        self.__provider = provider

    @property
    def name(self) -> str:
        """返回底层 Provider 名称，并在缺失时使用稳定回退值。"""

        return str(getattr(self.__provider, "name", "llm-provider") or "llm-provider")

    @staticmethod
    def _validate_request(
        request: LLMProviderRequest,
        context: ProviderInvocationContext,
    ) -> None:
        """校验消息、工具、温度以及宿主输出预算上限。"""

        if not request.messages or len(request.messages) > 256:
            raise ValueError("LLM provider request must contain 1..256 messages")
        allowed_roles = {"system", "user", "assistant", "tool"}
        total_chars = 0
        for message in request.messages:
            if message.role not in allowed_roles:
                raise ValueError(f"unsupported LLM message role: {message.role!r}")
            total_chars += len(message.content)
        if total_chars > 1_000_000:
            raise ValueError("LLM provider request exceeds the text character limit")
        if len(request.tools) > 128:
            raise ValueError("LLM provider request exceeds the tool limit")
        if request.temperature is not None and not 0 <= request.temperature <= 2:
            raise ValueError("LLM provider temperature must be between 0 and 2")
        if request.max_tokens <= 0:
            raise ProviderAccessError(
                "provider.access.budget",
                "LLM provider requires an explicit positive max_tokens budget",
            )
        if context.max_output_tokens <= 0 or request.max_tokens > context.max_output_tokens:
            raise ProviderAccessError(
                "provider.access.budget",
                "LLM provider request exceeds the host output-token budget",
            )

    @staticmethod
    def _provider_request(
        request: LLMProviderRequest,
    ) -> tuple[list[Any], list[Any] | None]:
        """把内部 DTO 转换为当前 Provider 接受的请求对象。"""

        from ..providers.base import ChatMessage, ToolDefinition

        messages = [
            ChatMessage(
                role=item.role,
                content=item.content,
                tool_calls=[dict(value) for value in item.tool_calls] or None,
                tool_call_id=item.tool_call_id or None,
            )
            for item in request.messages
        ]
        tools = [
            ToolDefinition(
                name=item.name,
                description=item.description,
                parameters=dict(item.parameters),
            )
            for item in request.tools
        ]
        return messages, tools or None

    @staticmethod
    def _result(response: Any) -> LLMProviderResult:
        """把底层响应收敛为不含私有对象的标准结果。"""

        raw_usage = getattr(response, "usage", None)
        usage = {
            str(key): max(0, int(value or 0))
            for key, value in (
                dict(raw_usage).items() if isinstance(raw_usage, Mapping) else ()
            )
        }
        raw_tool_calls = getattr(response, "tool_calls", None)
        tool_calls = tuple(
            dict(item)
            for item in (raw_tool_calls or [])
            if isinstance(item, Mapping)
        )
        return LLMProviderResult(
            content=str(getattr(response, "content", "") or ""),
            tool_calls=tool_calls,
            finish_reason=str(getattr(response, "finish_reason", "") or ""),
            usage=usage,
        )

    def _invocation_error(self, exc: Exception) -> ProviderInvocationError:
        """把上游异常映射为不泄露详情的稳定错误分类。"""

        timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
        return ProviderInvocationError(
            "provider.llm.timeout" if timed_out else "provider.llm.failure",
            provider=self.name,
            exception_type=type(exc).__name__,
            retryable=timed_out,
        )

    async def complete(
        self,
        request: LLMProviderRequest,
        context: ProviderInvocationContext,
    ) -> LLMProviderResult:
        """校验边界后调用底层完整响应接口。"""

        require_provider_access(
            context,
            permissions=("llm.invoke", "secret.use.model-provider"),
        )
        self._validate_request(request, context)
        messages, tools = self._provider_request(request)
        try:
            response = await self.__provider.chat(
                messages,
                tools=tools,
                model=request.model or None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            return self._result(response)
        except Exception as exc:
            raise self._invocation_error(exc) from exc

    async def stream(
        self,
        request: LLMProviderRequest,
        context: ProviderInvocationContext,
    ) -> AsyncIterator[LLMProviderResult]:
        """校验边界后转发并标准化底层流式响应。"""

        require_provider_access(
            context,
            permissions=("llm.invoke", "secret.use.model-provider"),
        )
        self._validate_request(request, context)
        messages, tools = self._provider_request(request)
        try:
            stream = self.__provider.chat_stream(
                messages,
                tools=tools,
                model=request.model or None,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            async for response in stream:
                yield self._result(response)
        except Exception as exc:
            raise self._invocation_error(exc) from exc


@dataclass(frozen=True, slots=True)
class SessionProjectionRecord:
    """保存不含消息内容、元数据和工作目录的 Session 投影。"""

    session_id: str
    parent_id: str
    title: str
    agent_name: str
    status: str
    created_at: float
    last_active_at: float
    message_count: int
    current_revision: int
    archived: bool

    def to_dict(self) -> dict[str, Any]:
        """返回可序列化的投影字典。"""

        return asdict(self)


@runtime_checkable
class SessionProjectionContract(Protocol):
    """约束带 Owner 过滤的 Session 只读查询接口。"""

    async def get(
        self,
        session_id: str,
        context: ProviderInvocationContext,
    ) -> SessionProjectionRecord | None:
        """读取当前 Owner 范围内的单个 Session 投影。"""
        ...

    async def list(
        self,
        context: ProviderInvocationContext,
        *,
        status: str = "",
        parent_id: str = "",
        include_archived: bool | None = None,
        limit: int = 100,
    ) -> tuple[SessionProjectionRecord, ...]:
        """按条件列出当前 Owner 范围内的 Session 投影。"""
        ...


class SessionProjectionAdapter:
    """提供基于当前 SessionManager 的 Owner 过滤、无内容只读投影。"""

    def __init__(self, manager: Any = None) -> None:
        """绑定可选 SessionManager，缺省时延迟加载宿主实现。"""

        self.__manager = manager

    def _manager(self) -> Any:
        """返回注入的 Manager 或当前宿主 SessionManager。"""

        if self.__manager is not None:
            return self.__manager
        from ..session.manager import SessionManager

        return SessionManager

    @staticmethod
    def _record(session: Any, manager: Any) -> SessionProjectionRecord:
        """把 Session 转换为内容安全的只读投影。"""

        return SessionProjectionRecord(
            session_id=str(getattr(session, "id", "") or ""),
            parent_id=str(getattr(session, "parent_id", "") or ""),
            title=str(getattr(session, "title", "") or "")[:500],
            agent_name=str(getattr(session, "agent_name", "") or "")[:100],
            status=str(getattr(session, "status", "") or "")[:50],
            created_at=float(getattr(session, "created_at", 0.0) or 0.0),
            last_active_at=float(getattr(session, "last_active_at", 0.0) or 0.0),
            message_count=len(list(getattr(session, "messages", []) or [])),
            current_revision=max(
                0,
                int(getattr(session, "current_revision", 0) or 0),
            ),
            archived=bool(manager.is_archived_session(session)),
        )

    @staticmethod
    def _require(context: ProviderInvocationContext) -> None:
        """要求完整 Owner 和 session.read 权限。"""

        require_provider_access(context, permissions=("session.read",))

    async def get(
        self,
        session_id: str,
        context: ProviderInvocationContext,
    ) -> SessionProjectionRecord | None:
        """执行带 Owner 过滤的单 Session 查询。"""

        self._require(context)
        manager = self._manager()
        session = await manager.get(
            str(session_id or "").strip(),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        return self._record(session, manager) if session is not None else None

    async def list(
        self,
        context: ProviderInvocationContext,
        *,
        status: str = "",
        parent_id: str = "",
        include_archived: bool | None = None,
        limit: int = 100,
    ) -> tuple[SessionProjectionRecord, ...]:
        """执行带 Owner 过滤和硬上限的 Session 列表查询。"""

        self._require(context)
        wanted_limit = max(1, min(int(limit), 200))
        manager = self._manager()
        sessions = await manager.list(
            status=status or None,
            parent_id=parent_id or None,
            include_archived=include_archived,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        return tuple(
            self._record(session, manager) for session in sessions[:wanted_limit]
        )


class TelemetryAdapter:
    """只允许受信宿主把已经统一脱敏的事件送入非阻塞遥测网关。"""

    def export(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: ProviderInvocationContext,
    ) -> Mapping[str, Any]:
        require_provider_access(context, permissions=("telemetry.export",))
        from .telemetry import TelemetryGateway

        return TelemetryGateway.emit(
            event_name,
            dict(payload or {}),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            request_id=context.request_id,
        )


@dataclass(frozen=True, slots=True)
class ProviderOperationSpec:
    """描述 Provider 单项操作的风险、权限和 Owner 要求。"""

    name: str
    risk: str
    permission: str
    owner_required: bool = True
    additional_permissions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 友好的操作规格。"""

        payload = asdict(self)
        payload["additional_permissions"] = list(self.additional_permissions)
        return payload


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySpec:
    """描述一种 Provider 能力的暴露策略与内核不变量。"""

    id: str
    service_key: str
    phase: str
    disposition: str
    exposure: str
    allowed_trust: tuple[str, ...]
    adapter_status: str
    operations: tuple[ProviderOperationSpec, ...]
    kernel_invariants: tuple[str, ...]
    blockers: tuple[str, ...]
    source_anchors: tuple[str, ...]
    plugin_context_exposed: bool = False
    production_consumer_switched: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 友好的完整能力规格。"""

        payload = asdict(self)
        payload["operations"] = [item.to_dict() for item in self.operations]
        payload["allowed_trust"] = list(self.allowed_trust)
        payload["kernel_invariants"] = list(self.kernel_invariants)
        payload["blockers"] = list(self.blockers)
        payload["source_anchors"] = list(self.source_anchors)
        return payload


_CAPABILITIES = (
    ProviderCapabilitySpec(
        id="approval",
        service_key="tcode.approval@1",
        phase="DSH-406",
        disposition="internal-only",
        exposure="blocked",
        allowed_trust=("builtin",),
        adapter_status="not-started",
        operations=(
            ProviderOperationSpec("ask", "control", "approval.ask"),
            ProviderOperationSpec("resolve", "control", "approval.resolve"),
        ),
        kernel_invariants=(
            "hard deny cannot be overridden",
            "timeout or missing approver fails closed",
            "approver identity and decision remain auditable",
        ),
        blockers=("authenticated approver contract", "durable wait/resolve state"),
        source_anchors=("tcode_agent/harness/policy.py", "tcode_agent/server/state.py"),
    ),
    ProviderCapabilitySpec(
        id="host",
        service_key="tcode.host@1",
        phase="DSH-406",
        disposition="internal-only",
        exposure="blocked",
        allowed_trust=("builtin",),
        adapter_status="boundary-evaluated",
        operations=(ProviderOperationSpec("compose", "control", "host.compose"),),
        kernel_invariants=(
            "application and server objects are never plugin values",
            "complete service graph is validated before host startup",
        ),
        blockers=("stable CLI/Web/Worker composition input",),
        source_anchors=("tcode_agent/server/app.py", "scripts/chat_cli.py"),
    ),
    ProviderCapabilitySpec(
        id="jobs",
        service_key="tcode.jobs@1",
        phase="DSH-BATCH3",
        disposition="experimental",
        exposure="adapter-only",
        allowed_trust=("builtin", "trusted"),
        adapter_status="task-runtime-v1-implemented",
        operations=(
            ProviderOperationSpec("submit", "mutate", "job.submit"),
            ProviderOperationSpec("read", "read", "job.read"),
            ProviderOperationSpec("cancel", "control", "job.cancel"),
        ),
        kernel_invariants=(
            "job owner and workspace visibility are enforced by the kernel",
            "backend state mapping is lossless",
            "workflow stage state is not replaced by job state",
        ),
        blockers=(),
        source_anchors=("tcode_agent/runtime/jobs.py", "tcode_agent/server/job_runtime_api.py"),
    ),
    ProviderCapabilitySpec(
        id="llm",
        service_key="tcode.llm@1",
        phase="DSH-307",
        disposition="experimental",
        exposure="adapter-only",
        allowed_trust=("builtin", "trusted"),
        adapter_status="host-adapter-v1-implemented",
        operations=(
            ProviderOperationSpec(
                "complete",
                "external-side-effect",
                "llm.invoke",
                additional_permissions=("secret.use.model-provider",),
            ),
            ProviderOperationSpec(
                "stream",
                "external-side-effect",
                "llm.invoke",
                additional_permissions=("secret.use.model-provider",),
            ),
        ),
        kernel_invariants=(
            "request owner identity and output budget are host supplied",
            "provider secrets are not exposed through the contract",
            "session persistence is not performed by the provider",
            "cancellation propagates to the current provider call",
        ),
        blockers=("host composition injection", "stable provider error taxonomy", "image request contract"),
        source_anchors=("tcode_agent/providers/base.py", "tcode_agent/runtime/provider_api.py"),
    ),
    ProviderCapabilitySpec(
        id="sandbox",
        service_key="tcode.sandbox@1",
        phase="DSH-406",
        disposition="internal-only",
        exposure="blocked",
        allowed_trust=("builtin",),
        adapter_status="existing-internal-protocol",
        operations=(ProviderOperationSpec("execute", "external-side-effect", "sandbox.execute"),),
        kernel_invariants=(
            "routing policy and permission ceiling remain in the kernel",
            "provider cannot self-attest isolation",
            "owner path and network policy are independently verified",
        ),
        blockers=("isolation attestation", "capability-safe file/network contract"),
        source_anchors=("tcode_agent/runtime/sandbox/base.py", "tcode_agent/harness/policy.py"),
    ),
    ProviderCapabilitySpec(
        id="session_persistence",
        service_key="tcode.session.persistence@1",
        phase="DSH-BATCH3",
        disposition="internal-only",
        exposure="blocked",
        allowed_trust=("builtin",),
        adapter_status="canonical-event-v1-implemented",
        operations=(
            ProviderOperationSpec("append", "mutate", "session.persist"),
            ProviderOperationSpec("snapshot", "mutate", "session.persist"),
        ),
        kernel_invariants=(
            "sequence version and owner checks are mandatory",
            "tool call and result pairing is preserved",
            "recovery remains deterministic",
        ),
        blockers=(),
        source_anchors=("tcode_agent/session/events.py", "tcode_agent/session/store_sqlite.py"),
    ),
    ProviderCapabilitySpec(
        id="session_projection",
        service_key="tcode.session.projection@1",
        phase="DSH-BATCH3",
        disposition="experimental",
        exposure="read-only-candidate",
        allowed_trust=("builtin", "trusted"),
        adapter_status="event-projection-v1-implemented",
        operations=(
            ProviderOperationSpec("get", "read", "session.read"),
            ProviderOperationSpec("list", "read", "session.read"),
        ),
        kernel_invariants=(
            "tenant user and workspace filters are mandatory",
            "message content metadata workdir and private reasoning are excluded",
            "projection access cannot mutate persistence",
        ),
        blockers=(),
        source_anchors=("tcode_agent/session/manager.py", "tcode_agent/runtime/provider_api.py"),
    ),
    ProviderCapabilitySpec(
        id="telemetry",
        service_key="tcode.telemetry@1",
        phase="DSH-406",
        disposition="experimental",
        exposure="adapter-only",
        allowed_trust=("builtin", "trusted"),
        adapter_status="redaction-gateway-v1-implemented",
        operations=(ProviderOperationSpec("export", "external-side-effect", "telemetry.export"),),
        kernel_invariants=(
            "redaction happens before exporter invocation",
            "core correlation fields cannot be replaced",
            "exporter backpressure cannot block execution",
        ),
        blockers=(),
        source_anchors=("tcode_agent/runtime/telemetry.py", "tcode_agent/runtime/artifacts.py"),
    ),
)


def provider_api_catalog() -> list[dict[str, Any]]:
    """按能力标识稳定排序并返回 Provider 目录。"""

    return [item.to_dict() for item in sorted(_CAPABILITIES, key=lambda item: item.id)]


def provider_api_catalog_document() -> dict[str, Any]:
    """返回可生成权威内部合同的 Provider API 文档。"""

    return {
        "schema_version": 1,
        "contract_id": "tcode.provider-api.internal.v1",
        "api_version": PROVIDER_API_INTERNAL_VERSION,
        "status": "internal-evaluation",
        "compatibility_commitment": "none",
        "plugin_context_exposed": False,
        "production_consumer_switched": False,
        "default_plugin_invocation": "deny",
        "capabilities": provider_api_catalog(),
    }


__all__ = [
    "LLMProviderMessage",
    "LLMProviderContract",
    "LLMProviderRequest",
    "LLMProviderResult",
    "LLMProviderTool",
    "LLMProviderAdapter",
    "PROVIDER_API_INTERNAL_VERSION",
    "ProviderAccessError",
    "ProviderCapabilitySpec",
    "ProviderInvocationContext",
    "ProviderInvocationError",
    "ProviderOperationSpec",
    "SessionProjectionAdapter",
    "SessionProjectionContract",
    "SessionProjectionRecord",
    "TelemetryAdapter",
    "provider_api_catalog",
    "provider_api_catalog_document",
    "require_provider_access",
]
