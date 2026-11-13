"""向 API v1 插件暴露的受控 Context。

公开视图包装内部 ``RuntimeContext``，避免受信任进程内插件意外依赖 Host
对象；恶意代码隔离仍属于后续独立 Worker 的职责。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional

from ..runtime.context import RuntimeContext, ServiceKey
from ..memory.service import memory_target
from ..runtime.service_providers import EVENTS, MEMORY, PROMPT, TOOLS
from .permissions import PluginPermission, PluginPermissionScope, PluginRuntime
from .embedding_api import PluginEmbeddingAPI
from .contributions import (
    ContributionBuffer,
    EventContributionAPI,
    PromptContributionAPI,
    ToolContributionAPI,
    VerifierContributionAPI,
)


PUBLIC_PLUGIN_SERVICE_KEYS = (EVENTS, MEMORY, PROMPT, TOOLS)


class PluginContextError(RuntimeError):
    """表示公开插件 Context 边界错误。"""

    code = "plugin.context"


class PluginContextClosedError(PluginContextError):
    """表示插件卸载后继续使用已关闭 Context。"""

    code = "plugin.context.closed"


class UndeclaredServiceAccessError(PluginContextError):
    """表示插件访问 Manifest 未声明的 Host Service。"""

    code = "plugin.context.service.undeclared"


class PluginServiceUnavailableError(PluginContextError):
    """表示已声明 Service 当前没有 Host Provider。"""

    code = "plugin.context.service.unavailable"


def _freeze(value: Any) -> Any:
    """递归冻结公开 Context 中的容器值。"""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class PluginInstance:
    """保存当前插件实例的只读公开身份。"""

    name: str
    version: str
    instance_id: str
    api_version: str
    requested_trust: str


@dataclass(frozen=True, slots=True)
class PluginScope:
    """保存插件 Scope 和不透明工作区标识。"""

    kind: str
    workspace_id: str


class PluginLogger:
    """不暴露 Handler 或 Root Logger 的轻量日志门面。"""

    __slots__ = ("__logger", "__extra")

    def __init__(self, name: str, instance_id: str) -> None:
        """为插件实例绑定固定日志来源字段。"""

        self.__logger = logging.getLogger(f"tcode_agent.plugins.{name}")
        self.__extra = {"plugin": name, "plugin_instance_id": instance_id}

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录调试级消息。"""

        self.__logger.debug(message, *args, extra=self.__extra, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录信息级消息。"""

        self.__logger.info(message, *args, extra=self.__extra, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录警告级消息。"""

        self.__logger.warning(message, *args, extra=self.__extra, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录错误级消息。"""

        self.__logger.error(message, *args, extra=self.__extra, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        """记录包含当前异常堆栈的错误消息。"""

        self.__logger.exception(message, *args, extra=self.__extra, **kwargs)


class _ServiceLease:
    """控制所有公开 Service Facade 的共同生命周期。"""

    __slots__ = ("active",)

    def __init__(self) -> None:
        """创建默认有效的 Context 租约。"""

        self.active = True

    def ensure_active(self) -> None:
        """确认租约有效，否则失败关闭。"""

        if not self.active:
            raise PluginContextClosedError("plugin service view is no longer active")

    def close(self) -> None:
        """永久关闭租约。"""

        self.active = False


class _ServiceFacade:
    """在每次调用前检查租约的内部 Service 包装器。"""

    __slots__ = ("__lease", "__target")

    def __init__(self, lease: _ServiceLease, target: Any) -> None:
        """绑定共同租约和内部 Service 目标。"""

        self.__lease = lease
        self.__target = target

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """检查生命周期后委派一个白名单方法。"""

        self.__lease.ensure_active()
        return getattr(self.__target, name)(*args, **kwargs)


class EventService(_ServiceFacade):
    """向插件提供只读事件目录。"""

    def catalog(self) -> list[dict[str, Any]]:
        """返回 Host 允许公开的事件目录。"""

        return self._call("catalog")


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """保存不含可执行对象的只读工具描述。"""

    id: str
    description: str
    parameters: Mapping[str, Any]


def _tool_descriptor(tool: Any) -> ToolDescriptor:
    """把内部 Tool 投影为只读公开描述。"""

    parameters = getattr(tool, "parameters", None)
    schema: dict[str, Any] = {}
    if parameters is not None:
        if hasattr(parameters, "model_json_schema"):
            schema = dict(parameters.model_json_schema())
        elif hasattr(parameters, "schema"):
            schema = dict(parameters.schema())
    return ToolDescriptor(
        id=str(getattr(tool, "id", "") or ""),
        description=str(getattr(tool, "description", "") or ""),
        parameters=_freeze(schema),
    )


class ToolService(_ServiceFacade):
    """向插件提供只读工具目录，不提供绕过 Policy 的执行入口。"""

    def get(self, tool_id: str) -> Optional[ToolDescriptor]:
        """按工具 ID 读取公开描述。"""

        tool = self._call("get", tool_id)
        return _tool_descriptor(tool) if tool is not None else None

    def list(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[ToolDescriptor]:
        """按 Agent 与执行配置列出公开工具描述。"""

        tools = self._call("list", agent, execution_profile=execution_profile)
        return [_tool_descriptor(tool) for tool in tools]

    def ids(
        self,
        agent: Any = None,
        *,
        execution_profile: Optional[str] = None,
    ) -> list[str]:
        """按 Agent 与执行配置列出公开工具 ID。"""

        return self._call(
            "ids",
            agent,
            execution_profile=execution_profile,
        )


class PromptService(_ServiceFacade):
    """提供不会修改生产 Prompt 的辅助校验能力。"""

    def validate_workpad_output(self, content: str) -> dict[str, Any]:
        """调用 Host 工作区输出校验器。"""

        return self._call("validate_workpad_output", content)


class MemoryService(_ServiceFacade):
    """向插件提供受 Manifest 权限约束的 Scope 化记忆操作。"""

    __slots__ = ("__runtime", "__source")

    def __init__(
        self,
        lease: _ServiceLease,
        target: Any,
        *,
        runtime: PluginRuntime,
        source: str,
    ) -> None:
        super().__init__(lease, target)
        self.__runtime = runtime
        self.__source = f"plugin:{source}"

    def _authorize(self, permission: PluginPermissionScope, scope: str, scope_key: str) -> str:
        target = memory_target(scope, scope_key)
        self.__runtime.ensure_permission(permission.value, target)
        return target

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
        self._authorize(PluginPermissionScope.MEMORY_READ, scope, scope_key)
        result = await self._call(
            "query",
            scope=scope,
            scope_key=scope_key,
            query=query,
            limit=limit,
            level=level,
            filters=dict(filters or {}),
            source=self.__source,
        )
        return _freeze(result)

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
        self._authorize(PluginPermissionScope.MEMORY_WRITE, scope, scope_key)
        result = await self._call(
            "put",
            scope=scope,
            scope_key=scope_key,
            content=content,
            tags=list(tags),
            structured=dict(structured or {}),
            source=self.__source,
            idempotency_key=idempotency_key,
        )
        return _freeze(result)

    async def delete(
        self,
        *,
        scope: str,
        scope_key: str = "",
        memory_id: str,
    ) -> Mapping[str, Any]:
        self._authorize(PluginPermissionScope.MEMORY_DELETE, scope, scope_key)
        result = await self._call(
            "delete",
            scope=scope,
            scope_key=scope_key,
            memory_id=memory_id,
            source=self.__source,
        )
        return _freeze(result)

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
        self._authorize(
            PluginPermissionScope.MEMORY_READ,
            source_scope,
            source_scope_key,
        )
        self._authorize(
            PluginPermissionScope.MEMORY_WRITE,
            target_scope,
            target_scope_key,
        )
        self._authorize(
            PluginPermissionScope.MEMORY_PROMOTE,
            source_scope,
            source_scope_key,
        )
        self._authorize(
            PluginPermissionScope.MEMORY_PROMOTE,
            target_scope,
            target_scope_key,
        )
        if move:
            self._authorize(
                PluginPermissionScope.MEMORY_DELETE,
                source_scope,
                source_scope_key,
            )
        result = await self._call(
            "promote",
            source_scope=source_scope,
            source_scope_key=source_scope_key,
            memory_id=memory_id,
            target_scope=target_scope,
            target_scope_key=target_scope_key,
            move=move,
            source=self.__source,
        )
        return _freeze(result)

    async def cleanup_scope(
        self,
        *,
        scope: str,
        scope_key: str = "",
    ) -> Mapping[str, Any]:
        self._authorize(PluginPermissionScope.MEMORY_MAINTENANCE, scope, scope_key)
        result = await self._call(
            "cleanup_scope",
            scope=scope,
            scope_key=scope_key,
            source=self.__source,
        )
        return _freeze(result)


class PluginWorkspaceAPI:
    """把 Manifest 文件权限投影为公开且不可逃逸的工作区接口。"""

    __slots__ = ("__lease", "__runtime")

    def __init__(self, lease: _ServiceLease, runtime: PluginRuntime) -> None:
        """绑定 Context 租约和内部权限执行器。

        参数:
            lease: 插件卸载后会关闭的生命周期租约。
            runtime: 只在 Host 内部持有的权限执行器。
        """

        self.__lease = lease
        self.__runtime = runtime

    @property
    def workdir(self) -> str:
        self.__lease.ensure_active()
        return str(self.__runtime.workdir)

    def resolve_read_path(self, path: str) -> str:
        """校验读权限并返回规范绝对路径。"""

        self.__lease.ensure_active()
        return str(
            self.__runtime.resolve_path(
                path,
                scope=PluginPermissionScope.FILESYSTEM_READ.value,
            )
        )

    def resolve_write_path(self, path: str) -> str:
        """校验写权限并返回规范绝对路径。"""

        self.__lease.ensure_active()
        return str(
            self.__runtime.resolve_path(
                path,
                scope=PluginPermissionScope.FILESYSTEM_WRITE.value,
            )
        )

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """在读权限边界内读取文本。"""

        self.__lease.ensure_active()
        return self.__runtime.read_text(path, encoding=encoding)

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> None:
        """在写权限边界内原子委派一次文本写入。"""

        self.__lease.ensure_active()
        self.__runtime.write_text(path, content, encoding=encoding)

    def list_dir(self, path: str) -> tuple[str, ...]:
        """在读权限边界内返回稳定排序的目录项。"""

        self.__lease.ensure_active()
        return tuple(self.__runtime.list_dir(path))


class PluginProcessAPI:
    """把 Manifest 进程权限投影为公开命令接口。"""

    __slots__ = ("__lease", "__runtime")

    def __init__(self, lease: _ServiceLease, runtime: PluginRuntime) -> None:
        """绑定 Context 租约和内部权限执行器。

        参数:
            lease: 插件卸载后会关闭的生命周期租约。
            runtime: 只在 Host 内部持有的权限执行器。
        """

        self.__lease = lease
        self.__runtime = runtime

    async def run(
        self,
        command: str,
        args: tuple[str, ...] = (),
        *,
        cwd: str | None = None,
        timeout: int = 60,
        profile: str | None = None,
    ) -> Mapping[str, Any]:
        """在进程、目录与 Sandbox 边界内运行一个命令。

        参数:
            command: Manifest 必须允许的可执行文件。
            args: 不经过 shell 展开的参数序列。
            cwd: 可选工作目录，同时必须命中文件读权限。
            timeout: 1 到 3600 秒的硬时限。
            profile: 可选逻辑 Sandbox Profile，必须命中 Manifest 授权。

        返回:
            冻结的退出码、标准输出、标准错误和执行环境元数据。
        """

        self.__lease.ensure_active()
        result = await self.__runtime.run_command(
            command,
            [str(item) for item in args],
            cwd=cwd,
            timeout=max(1, min(int(timeout), 3600)),
            sandbox_profile=profile,
        )
        return _freeze(result)


class PluginNetworkAPI:
    """把 Manifest 网络权限投影为固定 Egress Sandbox 接口。"""

    __slots__ = ("__lease", "__runtime")

    def __init__(self, lease: _ServiceLease, runtime: PluginRuntime) -> None:
        """绑定插件生命周期租约和 Host 权限执行器。"""

        self.__lease = lease
        self.__runtime = runtime

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        timeout: int = 30,
    ) -> Mapping[str, Any]:
        """通过受控 Egress 容器执行一次有界 HTTP 请求。"""

        self.__lease.ensure_active()
        result = await self.__runtime.request_network(
            method,
            url,
            headers={str(key): str(value) for key, value in dict(headers or {}).items()},
            body=body,
            timeout=max(1, min(int(timeout), 300)),
        )
        return _freeze(result)


def _service_facade(
    key: ServiceKey[Any],
    lease: _ServiceLease,
    target: Any,
    *,
    permission_runtime: PluginRuntime,
    plugin_name: str,
) -> Any:
    """为允许公开的 ServiceKey 建立受租约控制的门面。"""

    if key == EVENTS:
        return EventService(lease, target)
    if key == TOOLS:
        return ToolService(lease, target)
    if key == PROMPT:
        return PromptService(lease, target)
    if key == MEMORY:
        return MemoryService(
            lease,
            target,
            runtime=permission_runtime,
            source=plugin_name,
        )
    raise PluginServiceUnavailableError(f"service {key.name!r} is not public")


class ServiceView:
    """按 Manifest 声明过滤的只读 RuntimeContext Service 视图。"""

    __slots__ = ("__lease", "__required", "__optional", "__keys", "__services")

    def __init__(
        self,
        lease: _ServiceLease,
        *,
        required: tuple[str, ...],
        optional: tuple[str, ...],
        keys: Mapping[str, ServiceKey[Any]],
        services: Mapping[str, Any],
    ) -> None:
        """保存必需、可选 Service 与已经建立的门面。"""

        self.__lease = lease
        self.__required = frozenset(required)
        self.__optional = frozenset(optional)
        self.__keys = MappingProxyType(dict(keys))
        self.__services = MappingProxyType(dict(services))

    @property
    def declared(self) -> tuple[str, ...]:
        """返回全部已声明 Service 名称。"""

        return tuple(sorted(self.__required | self.__optional))

    @property
    def required(self) -> tuple[str, ...]:
        """返回必需 Service 名称。"""

        return tuple(sorted(self.__required))

    @property
    def optional_keys(self) -> tuple[str, ...]:
        """返回可选 Service 名称。"""

        return tuple(sorted(self.__optional))

    def _ensure_open(self) -> None:
        """确认 ServiceView 所属 Context 尚未卸载。"""

        self.__lease.ensure_active()

    def _resolve(self, key: str | ServiceKey[Any]) -> tuple[ServiceKey[Any], Any]:
        """解析已声明 Service，并拒绝未声明或不匹配的 Key。"""

        name = key.name if isinstance(key, ServiceKey) else str(key or "").strip()
        if name not in self.__required and name not in self.__optional:
            raise UndeclaredServiceAccessError(
                f"service {name!r} is not declared in Manifest requires/optional"
            )
        resolved = self.__keys.get(name)
        if resolved is None:
            raise PluginServiceUnavailableError(
                f"declared service {name!r} has no runtime provider"
            )
        if isinstance(key, ServiceKey) and key != resolved:
            raise PluginServiceUnavailableError(
                f"service definition for {name!r} does not match the host contract"
            )
        return resolved, self.__services.get(name)

    def require(self, key: str | ServiceKey[Any]) -> Any:
        """取得必需 Service；不存在时失败关闭。"""

        self._ensure_open()
        resolved, value = self._resolve(key)
        if value is None:
            raise PluginServiceUnavailableError(
                f"declared service {resolved.name!r} has no runtime provider"
            )
        return value

    def optional(self, key: str | ServiceKey[Any]) -> Optional[Any]:
        """取得可选 Service；没有 Provider 时返回空值。"""

        self._ensure_open()
        try:
            _resolved, value = self._resolve(key)
        except PluginServiceUnavailableError:
            return None
        return value

    def missing_required(self) -> tuple[str, ...]:
        """返回当前缺失的必需 Service。"""

        self._ensure_open()
        missing: list[str] = []
        for name in sorted(self.__required):
            if name not in self.__services:
                missing.append(name)
        return tuple(missing)

    def catalog(self) -> list[dict[str, Any]]:
        """返回不含内部对象的 Service 可用性目录。"""

        self._ensure_open()
        records: list[dict[str, Any]] = []
        for name in self.declared:
            key = self.__keys.get(name)
            available = key is not None and name in self.__services
            records.append(
                {
                    "key": name,
                    "required": name in self.__required,
                    "available": available,
                }
            )
        return records


@dataclass(frozen=True, slots=True)
class PluginContext:
    """保存插件生命周期内唯一的公开受控 Context。"""

    instance: PluginInstance
    scope: PluginScope
    services: ServiceView
    logger: PluginLogger
    config: Mapping[str, Any]
    tools: Any
    prompt: Any
    events: Any
    verifiers: Any
    memory: Any
    workspace: PluginWorkspaceAPI
    process: PluginProcessAPI
    network: PluginNetworkAPI
    embeddings: PluginEmbeddingAPI | None = None

    @classmethod
    def create(
        cls,
        *,
        runtime: RuntimeContext,
        name: str,
        version: str,
        instance_id: str,
        api_version: str,
        requested_trust: str,
        scope: str,
        workspace_id: str,
        config: Mapping[str, Any],
        required: tuple[str, ...],
        optional: tuple[str, ...],
        service_keys: Mapping[str, ServiceKey[Any]],
        contribution_buffer: Optional[ContributionBuffer] = None,
        workdir: str,
        permissions: tuple[PluginPermission, ...],
        agent_name: str = "",
    ) -> "PluginContext":
        """从已验证 Manifest、授权和内部 Runtime 构建公开 Context。"""

        lease = _ServiceLease()
        permission_runtime = PluginRuntime(
            workdir=workdir,
            permissions=list(permissions),
            agent_name=agent_name or name,
        )
        available_services: dict[str, Any] = {}
        for key_name, key in service_keys.items():
            target = runtime.optional(key)
            if target is not None:
                available_services[key_name] = _service_facade(
                    key,
                    lease,
                    target,
                    permission_runtime=permission_runtime,
                    plugin_name=name,
                )
        runtime.add_effect(
            lease.close,
            label=f"plugin-context-lease:{name}:{instance_id}",
        )
        buffer = contribution_buffer or ContributionBuffer(
            allowed=(),
            source=name,
            instance_id=instance_id,
            plugin_version=version,
        )
        declared = frozenset((*required, *optional))
        tool_reader = available_services.get(TOOLS.name) if TOOLS.name in declared else None
        prompt_reader = (
            available_services.get(PROMPT.name) if PROMPT.name in declared else None
        )
        event_reader = (
            available_services.get(EVENTS.name) if EVENTS.name in declared else None
        )
        memory_reader = (
            available_services.get(MEMORY.name) if MEMORY.name in declared else None
        )
        return cls(
            instance=PluginInstance(
                name=name,
                version=version,
                instance_id=instance_id,
                api_version=api_version,
                requested_trust=requested_trust,
            ),
            scope=PluginScope(kind=scope, workspace_id=workspace_id),
            services=ServiceView(
                lease,
                required=required,
                optional=optional,
                keys=service_keys,
                services=available_services,
            ),
            logger=PluginLogger(name, instance_id),
            config=_freeze(dict(config)),
            tools=ToolContributionAPI(buffer, tool_reader),
            prompt=PromptContributionAPI(buffer, prompt_reader),
            events=EventContributionAPI(buffer, event_reader),
            verifiers=VerifierContributionAPI(buffer),
            memory=memory_reader,
            workspace=PluginWorkspaceAPI(lease, permission_runtime),
            process=PluginProcessAPI(lease, permission_runtime),
            network=PluginNetworkAPI(lease, permission_runtime),
            embeddings=PluginEmbeddingAPI(workdir, permissions, lease.ensure_active)
            if any(p.scope == "model.embedding" for p in permissions) else None,
        )


__all__ = [
    "PluginContext",
    "PluginContextClosedError",
    "PluginContextError",
    "EventService",
    "PluginInstance",
    "PluginLogger",
    "MemoryService",
    "PluginNetworkAPI",
    "PluginScope",
    "PluginProcessAPI",
    "PluginWorkspaceAPI",
    "PluginServiceUnavailableError",
    "PUBLIC_PLUGIN_SERVICE_KEYS",
    "PromptService",
    "ServiceView",
    "ToolService",
    "ToolDescriptor",
    "UndeclaredServiceAccessError",
]
