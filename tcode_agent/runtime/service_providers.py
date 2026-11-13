"""把现有宿主能力注册为类型化运行时服务。

本模块是运行时服务的唯一原生入口。它只向 ``RuntimeContext`` 发布窄接口，
不会把底层 Registry、Manager 或宿主对象直接暴露给公开插件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from .context import (
    RuntimeContext,
    RuntimeIdentity,
    ServiceKey,
    apply_service_providers,
)


@runtime_checkable
class EventServiceContract(Protocol):
    """约束事件查询、分发与受 Scope 管理的订阅能力。"""

    async def dispatch(
        self,
        event: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        *,
        mode: str = "serial",
    ) -> dict[str, Any]:
        """按指定模式分发事件并返回累积输出。"""
        ...

    def subscribe(
        self,
        event: str,
        name: str,
        handler: Any,
        *,
        source: str,
        priority: int = 0,
        instance_id: Optional[str] = None,
    ) -> Any:
        """注册事件处理器，并返回可精确释放的 disposer。"""
        ...

    def catalog(self) -> list[dict[str, Any]]:
        """返回当前允许公开的事件目录。"""
        ...


@runtime_checkable
class ToolServiceContract(Protocol):
    """约束只读工具目录能力，不提供绕过策略的执行入口。"""

    def get(self, tool_id: str) -> Any:
        """按工具标识读取工具定义。"""
        ...

    def list(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[Any]:
        """按 Agent 与执行配置列出工具定义。"""
        ...

    def ids(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[str]:
        """按 Agent 与执行配置列出工具标识。"""
        ...


@runtime_checkable
class SessionProjectionServiceContract(Protocol):
    """约束带 Owner 过滤的 Session 只读投影能力。"""

    async def get(
        self,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Any:
        """按标识和 Owner 范围读取一个 Session。"""
        ...

    async def list(
        self,
        *,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_archived: Optional[bool] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[Any]:
        """按状态、层级和 Owner 范围列出 Session。"""
        ...


@runtime_checkable
class PromptServiceContract(Protocol):
    """约束 Prompt 组装与 Workpad 输出校验能力。"""

    def create_project_assembler(self, agent: Any) -> Any:
        """为指定 Agent 创建项目 Prompt 组装器。"""
        ...

    def assemble(self, **kwargs: Any) -> str:
        """按预算组装最终 Prompt。"""
        ...

    def validate_workpad_output(self, content: str) -> dict[str, Any]:
        """校验 Workpad 输出格式并返回结构化结果。"""
        ...


@runtime_checkable
class JobServiceContract(Protocol):
    """约束 Job 服务可用性探测和失败关闭行为。"""

    @property
    def available(self) -> bool:
        """返回当前是否存在可用的统一 Job Provider。"""
        ...

    @property
    def reason(self) -> str:
        """返回服务不可用的稳定原因。"""
        ...

    def require_available(self) -> None:
        """要求服务可用，否则抛出类型化异常。"""
        ...


@runtime_checkable
class MemoryServiceContract(Protocol):
    """约束插件可用的 Scope 化语义记忆能力。"""

    async def query(self, **kwargs: Any) -> dict[str, Any]:
        """召回一个受控 Scope 内的记忆。"""
        ...

    async def put(self, **kwargs: Any) -> dict[str, Any]:
        """写入一个受控 Scope 内的记忆。"""
        ...

    async def delete(self, **kwargs: Any) -> dict[str, Any]:
        """按 ID 删除一个受控 Scope 内的记忆。"""
        ...

    async def promote(self, **kwargs: Any) -> dict[str, Any]:
        """复制或移动记忆到另一个受控 Scope。"""
        ...

    async def cleanup_scope(self, **kwargs: Any) -> dict[str, Any]:
        """清理一个完整记忆 Scope 及其派生索引。"""
        ...


EVENTS = ServiceKey("tcode.events@1", EventServiceContract)
TOOLS = ServiceKey("tcode.tools@1", ToolServiceContract)
SESSION_PROJECTION = ServiceKey(
    "tcode.session.projection@1",
    SessionProjectionServiceContract,
)
PROMPT = ServiceKey("tcode.prompt@1", PromptServiceContract)
JOBS = ServiceKey("tcode.jobs@1", JobServiceContract)
MEMORY = ServiceKey("tcode.memory@1", MemoryServiceContract)
RUNTIME_SERVICE_KEYS = (EVENTS, TOOLS, SESSION_PROJECTION, PROMPT, JOBS, MEMORY)


class RuntimeServiceUnavailableError(RuntimeError):
    """表示运行时服务已声明但当前没有可用实现。"""

    def __init__(self, service_key: str, reason: str) -> None:
        """保存服务键和失败原因，并生成稳定错误消息。"""

        self.service_key = service_key
        self.reason = reason
        super().__init__(f"runtime service {service_key!r} is unavailable: {reason}")


class RuntimeEventService:
    """通过 HookRegistry 实现运行时事件服务。"""

    def __init__(self, context: RuntimeContext) -> None:
        """绑定拥有订阅副作用的运行时 Context。"""

        self._context = context

    async def dispatch(
        self,
        event: str,
        input_data: Optional[dict[str, Any]] = None,
        output_data: Optional[dict[str, Any]] = None,
        *,
        mode: str = "serial",
    ) -> dict[str, Any]:
        """委派 HookRegistry 分发事件。"""

        from ..plugins.hooks import HookRegistry

        return await HookRegistry.dispatch(
            event,
            input_data=input_data,
            output_data=output_data,
            mode=mode,
        )

    def subscribe(
        self,
        event: str,
        name: str,
        handler: Any,
        *,
        source: str,
        priority: int = 0,
        instance_id: Optional[str] = None,
    ) -> Any:
        """注册处理器，并把释放操作纳入当前 Context 生命周期。"""

        from ..plugins.hooks import HookRegistry

        disposer = HookRegistry.register(
            event,
            name=name,
            handler=handler,
            source=source,
            priority=priority,
            instance_id=instance_id,
        )
        try:
            self._context.add_effect(
                disposer,
                label=f"event:{disposer.token.event}:{disposer.token.registration_id}",
            )
        except BaseException:
            disposer()
            raise
        return disposer

    def catalog(self) -> list[dict[str, Any]]:
        """返回 HookRegistry 的只读事件目录。"""

        from ..plugins.hooks import HookRegistry

        return HookRegistry.catalog()


class RuntimeToolService:
    """提供 ToolRegistry 的只读运行时视图。"""

    def get(self, tool_id: str) -> Any:
        """按工具标识读取当前注册项。"""

        from ..tools.registry import ToolRegistry

        return ToolRegistry.get(tool_id)

    def list(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[Any]:
        """按 Agent 与执行配置读取当前工具列表。"""

        from ..tools.registry import ToolRegistry

        return ToolRegistry.all(agent, execution_profile=execution_profile)

    def ids(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[str]:
        """按 Agent 与执行配置读取当前工具标识。"""

        from ..tools.registry import ToolRegistry

        return ToolRegistry.ids(agent, execution_profile=execution_profile)


class RuntimeSessionProjectionService:
    """提供带 Owner 过滤的 Session 只读投影。"""

    async def get(
        self,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Any:
        """把单个 Session 查询委派给 SessionManager。"""

        from ..session.manager import SessionManager

        return await SessionManager.get(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    async def list(
        self,
        *,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_archived: Optional[bool] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[Any]:
        """把 Session 列表查询委派给 SessionManager。"""

        from ..session.manager import SessionManager

        return await SessionManager.list(
            status=status,
            parent_id=parent_id,
            include_archived=include_archived,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )


class RuntimePromptService:
    """提供当前唯一 Prompt 组装和输出校验入口。"""

    def create_project_assembler(self, agent: Any) -> Any:
        """创建项目级 Prompt 组装器。"""

        from ..prompt.builder import ProjectPromptAssembler

        return ProjectPromptAssembler(agent)

    def assemble(self, **kwargs: Any) -> str:
        """使用预算感知的唯一 Prompt 组装实现。"""

        from ..prompt.composer import PromptComposer

        return PromptComposer.budget_aware_assemble(**kwargs)

    def validate_workpad_output(self, content: str) -> dict[str, Any]:
        """使用统一规则校验 Workpad 输出。"""

        from ..prompt.composer import PromptComposer

        return PromptComposer.validate_workpad_output(content)


@dataclass(frozen=True)
class UnavailableJobService:
    """在 Job Provider 尚未注入当前 Context 时执行失败关闭。"""

    reason: str = "the current runtime context has no canonical Job provider"

    @property
    def available(self) -> bool:
        """明确返回服务当前不可用。"""

        return False

    def require_available(self) -> None:
        """抛出包含服务键和原因的类型化异常。"""

        raise RuntimeServiceUnavailableError(JOBS.name, self.reason)


class RuntimeServiceProvider:
    """一次性向 RuntimeContext 发布宿主服务窄接口。"""

    name = "tcode.runtime-services@1"
    provides = RUNTIME_SERVICE_KEYS
    dependencies: tuple[ServiceKey[Any], ...] = ()
    shadows: tuple[ServiceKey[Any], ...] = ()

    async def apply(self, context: RuntimeContext) -> None:
        """以固定来源注册服务，并由 Context 负责生命周期释放。"""

        context.provide(EVENTS, RuntimeEventService(context), source=self.name)
        context.provide(TOOLS, RuntimeToolService(), source=self.name)
        context.provide(
            SESSION_PROJECTION,
            RuntimeSessionProjectionService(),
            source=self.name,
        )
        context.provide(PROMPT, RuntimePromptService(), source=self.name)
        context.provide(JOBS, UnavailableJobService(), source=self.name)
        from ..memory.service import ScopedMemoryService

        context.provide(
            MEMORY,
            ScopedMemoryService(context.identity.workspace_id or "."),
            source=self.name,
        )


async def create_runtime_context(
    identity: Optional[RuntimeIdentity] = None,
    *,
    name: str = "runtime-services",
) -> RuntimeContext:
    """创建并完整装配原生运行时 Context，失败时自动释放。"""

    context = RuntimeContext(identity, name=name)
    try:
        await apply_service_providers(context, [RuntimeServiceProvider()])
    except BaseException:
        await context.close()
        raise
    return context


__all__ = [
    "EVENTS",
    "JOBS",
    "MEMORY",
    "PROMPT",
    "RUNTIME_SERVICE_KEYS",
    "SESSION_PROJECTION",
    "TOOLS",
    "EventServiceContract",
    "JobServiceContract",
    "MemoryServiceContract",
    "PromptServiceContract",
    "RuntimeEventService",
    "RuntimePromptService",
    "RuntimeServiceProvider",
    "RuntimeServiceUnavailableError",
    "RuntimeSessionProjectionService",
    "RuntimeToolService",
    "SessionProjectionServiceContract",
    "ToolServiceContract",
    "UnavailableJobService",
    "create_runtime_context",
]
