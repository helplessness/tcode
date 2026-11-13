"""Plugin SDK v1 Contribution 的 Host 适配器。"""

from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match

from ..plugin_sdk import (
    EventEnvelope,
    PromptContribution,
    PublicEvent,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
    VerifierDefinition,
)
from ..types import ToolResult
from ..tools.base import ParameterSchema, Tool, ToolContext
from ..tools.capabilities import ToolCapability
from .permissions import (
    PluginPermission,
    PluginPermissionError,
    PluginPermissionScope,
    PluginRuntime,
)


_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERIFIER_ID_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,63}(?:\.[a-z0-9][a-z0-9_-]{0,63})+$"
)
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PUBLIC_EVENT_MAP = {
    PublicEvent.CONFIG_LOADED_V1: "config.loaded",
    PublicEvent.SESSION_STARTED_V1: "session.start",
    PublicEvent.SESSION_BOOTSTRAPPED_V1: "session.bootstrap",
    PublicEvent.MEMORY_RECALLED_V1: "memory.recall.after",
    PublicEvent.MEMORY_WRITTEN_V1: "memory.write.after",
    PublicEvent.MEMORY_DELETED_V1: "memory.delete.after",
    PublicEvent.MEMORY_PROMOTED_V1: "memory.promote.after",
    PublicEvent.MEMORY_OPERATION_ERROR_V1: "memory.operation.error",
    PublicEvent.SESSION_COMPACTION_BEFORE_V1: "session.compaction.before",
    PublicEvent.SESSION_COMPACTION_AFTER_V1: "session.compaction.after",
    PublicEvent.SESSION_COMPACTION_ERROR_V1: "session.compaction.error",
    PublicEvent.SESSION_DELETE_BEFORE_V1: "session.delete.before",
    PublicEvent.SESSION_DELETE_AFTER_V1: "session.delete.after",
    PublicEvent.SESSION_DELETE_ERROR_V1: "session.delete.error",
}
_PUBLIC_EVENT_PERMISSIONS: dict[PublicEvent, frozenset[str]] = {
    event: frozenset({"memory.read"})
    for event in PublicEvent
    if event is not PublicEvent.CONFIG_LOADED_V1
}
_PUBLIC_EVENT_PERMISSIONS[PublicEvent.SESSION_COMPACTION_BEFORE_V1] = frozenset(
    {"memory.read", "memory.write"}
)
_PUBLIC_EVENT_PERMISSIONS[PublicEvent.SESSION_STARTED_V1] = frozenset()
_PUBLIC_EVENT_PERMISSIONS[PublicEvent.SESSION_BOOTSTRAPPED_V1] = frozenset()


class _ToolCallWorkspaceAPI:
    """绑定到一次真实 Host 工具调用工作区的短生命周期文件能力。"""

    __slots__ = ("__active", "__runtime")

    def __init__(self, runtime: PluginRuntime) -> None:
        self.__active = True
        self.__runtime = runtime

    def _ensure_active(self) -> None:
        if not self.__active:
            raise RuntimeError("tool-call workspace capability is no longer active")

    def close(self) -> None:
        self.__active = False

    def resolve_read_path(self, path: str) -> str:
        self._ensure_active()
        return str(
            self.__runtime.resolve_path(
                path,
                scope=PluginPermissionScope.FILESYSTEM_READ.value,
            )
        )

    def resolve_write_path(self, path: str) -> str:
        self._ensure_active()
        return str(
            self.__runtime.resolve_path(
                path,
                scope=PluginPermissionScope.FILESYSTEM_WRITE.value,
            )
        )

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        self._ensure_active()
        return self.__runtime.read_text(path, encoding=encoding)

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> None:
        self._ensure_active()
        self.__runtime.write_text(path, content, encoding=encoding)

    def list_dir(self, path: str) -> tuple[str, ...]:
        self._ensure_active()
        return tuple(self.__runtime.list_dir(path))


class PluginContributionError(RuntimeError):
    """表示公开插件贡献违反 Host 契约。"""

    code = "plugin.contribution"


class UndeclaredContributionError(PluginContributionError):
    """表示插件贡献了 Manifest 未声明的类型。"""

    code = "plugin.contribution.undeclared"


class ContributionBufferClosedError(PluginContributionError):
    """表示插件在 ``start`` 完成后继续登记贡献。"""

    code = "plugin.contribution.closed"


def _safe_mapping(
    value: Any,
    *,
    depth: int = 0,
    string_limit: int = 4000,
) -> Any:
    """把插件元数据限制为有界、可序列化且不可变的值。"""

    if depth >= 4:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:string_limit]
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key)[:100]: _safe_mapping(
                    item,
                    depth=depth + 1,
                    string_limit=string_limit,
                )
                for key, item in list(value.items())[:64]
            }
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(
            _safe_mapping(item, depth=depth + 1, string_limit=string_limit)
            for item in list(value)[:64]
        )
    return f"<{type(value).__name__}>"


def _json_safe_copy(value: Any) -> Any:
    """把内部冻结视图复制成 ToolResult/事件可直接 JSON 序列化的值。"""

    if isinstance(value, Mapping):
        return {str(key): _json_safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(item) for item in value]
    return value


def _schema_type(name: str, schema: Mapping[str, Any]) -> type[ParameterSchema]:
    """把公开 JSON Schema 包装为内部 Tool 参数类型。"""

    frozen_schema = dict(schema)

    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """返回工具声明时冻结的 JSON Schema 副本。"""

        return dict(frozen_schema)

    return type(
        f"{name.title().replace('_', '')}Parameters",
        (ParameterSchema,),
        {"model_json_schema": classmethod(model_json_schema), "__module__": __name__},
    )


class SDKToolAdapter(Tool):
    """把公开 SDK 工具定义适配到 Host 的统一工具注册表。"""

    def __init__(
        self,
        definition: ToolDefinition,
        *,
        source: str,
        instance_id: str,
        plugin_version: str,
        permission_runtime: PluginRuntime | None = None,
        permission_source_root: str = "",
        permission_config_root: str = "",
    ) -> None:
        """绑定工具定义及其精确插件实例所有权。"""

        self.definition = definition
        self.source = source
        self.instance_id = instance_id
        self.plugin_version = plugin_version
        self.permission_runtime = permission_runtime
        self.permission_source_root = str(permission_source_root or "").rstrip(os.sep)
        self.permission_config_root = str(permission_config_root or "").rstrip(os.sep)
        self._parameters = _schema_type(definition.name, definition.input_schema)
        self._validator = Draft202012Validator(dict(definition.input_schema))

    @property
    def id(self) -> str:
        """返回公开工具 ID。"""

        return self.definition.name

    @property
    def description(self) -> str:
        """返回公开工具描述。"""

        return self.definition.description

    @property
    def on_demand(self) -> bool:
        return self.definition.on_demand

    @property
    def parameters(self) -> type[ParameterSchema]:
        """返回由公开 JSON Schema 生成的参数模型。"""

        return self._parameters

    @property
    def capability(self) -> ToolCapability:
        """把插件声明的副作用等级投影为统一 Policy 能力。"""

        declared = self.definition.side_effect_level
        if declared in {"none", "read"}:
            side_effect_level = "none"
            tags = ("plugin", "read_only")
        elif declared == "write":
            side_effect_level = "high"
            tags = ("plugin", "write")
        else:
            side_effect_level = "high"
            tags = ("plugin", "shell", "sandbox_required")
        return ToolCapability(
            tool_id=self.id,
            side_effect_level=side_effect_level,
            concurrency_safe=declared in {"none", "read"},
            interrupt_behavior="cancel_process" if declared == "process" else "best_effort",
            result_budget_tokens=self.definition.result_budget_tokens,
            tags=tags,
        )

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        """再次校验参数、调用 SDK Handler 并限制结果预算。"""

        # 插件声明式选择加入；适用于进程内和隔离 worker，先恢复表示，再严格校验。
        format_repairs = []
        if self.definition.input_schema.get("x-tcode-argument-format") == "v1":
            from tcode_agent.plugin_sdk.argument_format import normalize_argument_format
            args = normalize_argument_format(args, self.definition.input_schema, repairs=format_repairs)
        errors = list(self._validator.iter_errors(args))
        if errors:
            selected = best_match(errors) or errors[0]
            location = ".".join(str(item) for item in selected.absolute_path)
            prefix = f"arguments.{location}: " if location else "arguments: "
            raise ValueError(prefix + selected.message)
        call_workspace: _ToolCallWorkspaceAPI | None = None
        if self.permission_runtime is not None:
            extra = context.extra if isinstance(context.extra, Mapping) else {}
            call_workdir = str(
                extra.get("workdir") or self.permission_runtime.workdir
            ).strip()
            startup_workdir = self.permission_runtime.workdir.rstrip(os.sep)
            source_root = self.permission_source_root
            config_root = self.permission_config_root
            call_permissions: list[PluginPermission] = []
            for permission in self.permission_runtime.permissions:
                patterns: list[str] = []
                for pattern in permission.patterns:
                    normalized = str(pattern or "").strip()
                    if not normalized:
                        continue
                    if source_root and (
                        normalized == source_root
                        or normalized.startswith(source_root + os.sep)
                    ):
                        patterns.append(normalized)
                    elif (
                        normalized == startup_workdir
                        or normalized.startswith(startup_workdir + os.sep)
                    ):
                        patterns.append(
                            call_workdir.rstrip(os.sep)
                            + normalized[len(startup_workdir):]
                        )
                    elif config_root and (
                        normalized == config_root
                        or normalized.startswith(config_root + os.sep)
                    ):
                        # Manifest/config 中相对配置根声明的工作区路径，在
                        # 多租户调用时必须随真实 call workdir 重绑定。插件
                        # 自身源码/知识目录已由上面的 source_root 分支固定，
                        # 不会被镜像到用户工作区而产生知识注入面。
                        patterns.append(
                            call_workdir.rstrip(os.sep)
                            + normalized[len(config_root):]
                        )
                    else:
                        patterns.append(normalized)
                call_permissions.append(
                    PluginPermission(
                        scope=permission.scope,
                        patterns=list(dict.fromkeys(patterns)),
                        description=permission.description,
                        required=permission.required,
                    )
                )
            call_workspace = _ToolCallWorkspaceAPI(
                PluginRuntime(
                    workdir=call_workdir,
                    permissions=call_permissions,
                    agent_name=self.permission_runtime.agent_name,
                    require_sandbox=self.permission_runtime.require_sandbox,
                )
            )
        ontology = None
        if call_workspace is not None and self.permission_runtime is not None:
            from ..ontology_host import read_capability
            try:
                self.permission_runtime.ensure_permission("ontology.read", "workspace")
            except PluginPermissionError:
                pass
            else:
                ontology = read_capability(call_workdir)
        from .artifact_api import PluginArtifacts
        artifacts = PluginArtifacts(context, call_workspace, self.definition.side_effect_level)
        input_active = True
        def require_input_active():
            if not input_active or (context.abort is not None and context.abort.is_set()):
                raise RuntimeError("plugin input request is no longer active")

        async def request_input(payload):
            require_input_active()
            extra = context.extra or {}
            for key in ("__question_resolve__", "__question_ask__"):
                callback = extra.get(key)
                if callable(callback):
                    require_input_active()
                    answer = callback(payload)
                    if inspect.isawaitable(answer):
                        answer = await answer
                    require_input_active()
                    if answer is not None and str(answer).strip():
                        return answer
            return None

        recent = []
        for message in reversed(getattr(context, "messages", []) or []):
            if str(getattr(message, "role", "") or "").strip().lower() == "user":
                text = str(getattr(message, "content", "") or "").strip()
                if text:
                    # Stop at an unknown oversized turn; never splice around it
                    # to recover older consent or truncate a refusal.
                    if len(text.encode("utf-8")) > 16384:
                        break
                    recent.append(text)
                    if len(recent) == 3:
                        break
        user_messages = tuple(reversed(recent))
        call_context = ToolCallContext(
            session_id=str(context.session_id or ""),
            message_id=str(context.message_id or ""),
            agent=str(context.agent or ""),
            call_id=str(context.call_id) if context.call_id is not None else None,
            execution=MappingProxyType({key: (context.extra or {})[key] for key in
                ("workdir", "tenant_id", "user_id", "workspace_id", "wiki_context", "resource_context") if key in (context.extra or {})}),
            request_input=request_input,
            abort=context.abort,
            user_messages=user_messages,
            workspace=call_workspace,
            ontology=ontology,
            artifacts=artifacts,
        )
        try:
            host_aware_handler = getattr(
                self.definition.handler,
                "__tcode_invoke_with_host_context__",
                None,
            )
            if callable(host_aware_handler):
                # 隔离 Worker 只通过此内部适配点取得 Host ToolContext；该对象不会
                # 被序列化给插件代码，只用于绑定反向能力请求的真实父调用身份。
                result = host_aware_handler(
                    MappingProxyType(dict(args)),
                    call_context,
                    context,
                )
            else:
                result = self.definition.handler(MappingProxyType(dict(args)), call_context)
            if inspect.isawaitable(result):
                result = await result
        finally:
            input_active = False
            artifacts.close()
            if ontology is not None:
                ontology.close()
            if call_workspace is not None:
                call_workspace.close()
        if not isinstance(result, ToolOutput):
            raise TypeError("Plugin SDK tool handler must return ToolOutput")
        if not isinstance(result.title, str) or not isinstance(result.content, str):
            raise TypeError("Plugin SDK ToolOutput title and content must be strings")
        if not isinstance(result.metadata, Mapping):
            raise TypeError("Plugin SDK ToolOutput metadata must be a mapping")
        max_chars = self.definition.result_budget_tokens * 4
        truncated = len(result.content) > max_chars
        output = result.content[:max_chars] if truncated else result.content
        # Ordinary plugin metadata remains compact. A workflow stage receipt is
        # an explicitly declared, schema-validated completion payload and the
        # workflow runtime already enforces a 16k encoded limit. Preserve that
        # payload up to the same boundary so long Markdown tables are not cut in
        # the middle before the workflow presenter reads ``user_summary``.
        raw_metadata = dict(result.metadata)
        if format_repairs:
            raw_metadata["argument_format_repairs"] = format_repairs[:20]
        metadata = _json_safe_copy(_safe_mapping(raw_metadata))
        if isinstance(raw_metadata.get("stage_receipt"), Mapping):
            metadata["stage_receipt"] = _json_safe_copy(
                _safe_mapping(raw_metadata["stage_receipt"], string_limit=16_000)
            )
        metadata.update(
            {
                "plugin": self.source,
                "plugin_instance_id": self.instance_id,
                "plugin_version": self.plugin_version,
                "tool_contract_version": self.definition.version,
            }
        )
        return ToolResult(
            title=str(result.title or self.definition.name)[:200],
            output=output,
            error=(metadata["error"].strip()[:2000] or None)
            if isinstance(metadata.get("error"), str) else None,
            metadata=metadata,
            truncated=truncated,
        )


@dataclass(frozen=True, slots=True)
class EventSubscription:
    """保存一个带插件实例所有权的公共事件订阅。"""

    event: PublicEvent
    handler: Any
    name: str


class ContributionBuffer:
    """在插件预热成功前暂存贡献，并校验声明与授权边界。"""

    def __init__(
        self,
        *,
        allowed: tuple[str, ...],
        source: str,
        instance_id: str,
        plugin_version: str,
        allowed_side_effects: tuple[str, ...] = ("none",),
        permission_scopes: tuple[str, ...] = (),
        permission_runtime: Any = None,
        permission_source_root: str = "",
        permission_config_root: str = "",
    ) -> None:
        """初始化贡献类型、副作用上限与实例所有权。"""

        self.allowed = frozenset(allowed)
        self.source = source
        self.instance_id = instance_id
        self.plugin_version = plugin_version
        self.allowed_side_effects = frozenset(allowed_side_effects) | {"none"}
        self.permission_scopes = frozenset(permission_scopes)
        self.permission_runtime = permission_runtime
        self.permission_source_root = str(permission_source_root or "")
        self.permission_config_root = str(permission_config_root or "")
        self.tools: list[ToolDefinition] = []
        self.prompts: list[PromptContribution] = []
        self.events: list[EventSubscription] = []
        self.verifiers: list[VerifierDefinition] = []
        self._open = True

    def _ensure(self, kind: str) -> None:
        """确认缓冲区仍开放且 Manifest 声明了贡献类型。"""

        if not self._open:
            raise ContributionBufferClosedError(
                "plugin contributions are sealed after start()"
            )
        if kind not in self.allowed:
            raise UndeclaredContributionError(
                f"Manifest contributes must declare {kind!r}"
            )

    def add_tool(self, definition: ToolDefinition) -> None:
        """校验并暂存一个工具贡献。"""

        self._ensure("tools")
        if not isinstance(definition, ToolDefinition):
            raise TypeError("tools.register requires ToolDefinition")
        if not _NAME_PATTERN.fullmatch(definition.name):
            raise PluginContributionError("tool name has an invalid format")
        if not _VERSION_PATTERN.fullmatch(definition.version):
            raise PluginContributionError("tool version must be a semantic version")
        if not str(definition.description or "").strip():
            raise PluginContributionError("tool description must not be empty")
        if any(item.name == definition.name for item in self.tools):
            raise PluginContributionError(f"duplicate tool contribution: {definition.name}")
        if definition.side_effect_level not in self.allowed_side_effects:
            raise PluginContributionError(
                "tool side_effect_level exceeds granted Manifest permissions: "
                f"{definition.side_effect_level}"
            )
        if not callable(definition.handler):
            raise PluginContributionError("tool handler must be callable")
        if not 1 <= definition.result_budget_tokens <= 32768:
            raise PluginContributionError("tool result_budget_tokens must be 1..32768")
        if not isinstance(definition.on_demand, bool):
            raise PluginContributionError("tool on_demand must be a boolean")
        if not isinstance(definition.input_schema, Mapping):
            raise PluginContributionError("tool input_schema must be a mapping")
        schema = dict(definition.input_schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise PluginContributionError(
                f"invalid Draft 2020-12 tool input schema: {exc.message}"
            ) from exc
        if schema.get("type") != "object":
            raise PluginContributionError("tool input schema root type must be object")
        self.tools.append(definition)

    def add_prompt(self, contribution: PromptContribution) -> None:
        """校验并暂存一个 Prompt Contribution。"""

        self._ensure("prompt")
        if not isinstance(contribution, PromptContribution):
            raise TypeError("prompt.contribute requires PromptContribution")
        if not _NAME_PATTERN.fullmatch(contribution.name):
            raise PluginContributionError("prompt contribution name has an invalid format")
        if not _VERSION_PATTERN.fullmatch(contribution.version):
            raise PluginContributionError("prompt version must be a semantic version")
        if contribution.scope != "workspace":
            raise PluginContributionError(
                "Plugin API v1 prompt scope must be 'workspace'"
            )
        if not isinstance(contribution.structured_output_safe, bool):
            raise PluginContributionError("structured_output_safe must be a boolean")
        if (not isinstance(contribution.agents, (tuple, list)) or len(contribution.agents) > 64
                or any(not isinstance(agent, str) or not _NAME_PATTERN.fullmatch(agent) for agent in contribution.agents)):
            raise PluginContributionError("prompt agents must contain at most 64 valid names")
        if any(item.name == contribution.name for item in self.prompts):
            raise PluginContributionError(
                f"duplicate prompt contribution: {contribution.name}"
            )
        if bool(contribution.text) == bool(contribution.renderer):
            raise PluginContributionError(
                "prompt contribution must define exactly one of text or renderer"
            )
        if contribution.renderer is not None and not callable(contribution.renderer):
            raise PluginContributionError("prompt renderer must be callable")
        if not 1 <= contribution.token_budget <= 16384:
            raise PluginContributionError("prompt token_budget must be 1..16384")
        if not -10000 <= contribution.order <= 10000:
            raise PluginContributionError("prompt order must be -10000..10000")
        self.prompts.append(contribution)

    def add_event(self, event: PublicEvent, handler: Any) -> None:
        """校验并暂存一个公共事件订阅。"""

        self._ensure("events")
        if not isinstance(event, PublicEvent):
            raise TypeError("events.subscribe requires a PublicEvent enum value")
        if event not in _PUBLIC_EVENT_MAP:
            raise PluginContributionError(f"public event is not supported: {event.value}")
        missing_permissions = sorted(
            _PUBLIC_EVENT_PERMISSIONS.get(event, frozenset()) - self.permission_scopes
        )
        if missing_permissions:
            raise PluginContributionError(
                f"public event {event.value} requires Manifest permissions: "
                + ", ".join(missing_permissions)
            )
        if not callable(handler):
            raise TypeError("event handler must be callable")
        name = str(getattr(handler, "__name__", "observer"))[:100]
        self.events.append(EventSubscription(event, handler, name))

    def add_verifier(self, definition: VerifierDefinition) -> None:
        """校验并暂存一个受 Host 证明强度约束的 Verifier。"""

        self._ensure("verifiers")
        if not isinstance(definition, VerifierDefinition):
            raise TypeError("verifiers.register requires VerifierDefinition")
        verifier_id = str(definition.verifier_id or "").strip().lower()
        if not _VERIFIER_ID_PATTERN.fullmatch(verifier_id):
            raise PluginContributionError(
                "verifier_id must be a lowercase dotted identifier"
            )
        expected_namespace = f"{self.source.lower()}."
        if not verifier_id.startswith(expected_namespace):
            raise PluginContributionError(
                f"verifier_id must use the {expected_namespace!r} namespace"
            )
        if not _VERSION_PATTERN.fullmatch(definition.version):
            raise PluginContributionError("verifier version must be a semantic version")
        if not str(definition.description or "").strip():
            raise PluginContributionError("verifier description must not be empty")
        if any(item.verifier_id == verifier_id for item in self.verifiers):
            raise PluginContributionError(
                f"duplicate verifier contribution: {verifier_id}"
            )
        if not callable(definition.handler):
            raise PluginContributionError("verifier handler must be callable")
        if definition.requested_max_proof_level not in {"P0", "P1", "P2", "P3", "P4"}:
            raise PluginContributionError(
                "verifier requested_max_proof_level must be P0..P4"
            )
        if not 0.01 <= float(definition.timeout_seconds) <= 30.0:
            raise PluginContributionError("verifier timeout_seconds must be 0.01..30")
        if definition.isolation_policy != "controlled-host":
            raise PluginContributionError(
                "verifier isolation_policy must be controlled-host"
            )
        missing_capabilities = sorted(
            set(definition.required_capabilities) - self.permission_scopes
        )
        if missing_capabilities:
            raise PluginContributionError(
                "verifier requires ungranted Manifest permissions: "
                + ", ".join(missing_capabilities)
            )
        for label, schema in (
            ("input", definition.input_schema),
            ("output", definition.output_schema),
        ):
            if not isinstance(schema, Mapping):
                raise PluginContributionError(
                    f"verifier {label}_schema must be a mapping"
                )
            try:
                Draft202012Validator.check_schema(dict(schema))
            except SchemaError as exc:
                raise PluginContributionError(
                    f"invalid Draft 2020-12 verifier {label} schema: {exc.message}"
                ) from exc
            if schema.get("type") != "object":
                raise PluginContributionError(
                    f"verifier {label} schema root type must be object"
                )
        self.verifiers.append(definition)

    def seal(self) -> None:
        """关闭缓冲区，阻止插件在预热后追加贡献。"""

        self._open = False

    def tool_adapters(self) -> list[SDKToolAdapter]:
        """为已经封存的工具贡献建立 Host Adapter。"""

        if self._open:
            raise ContributionBufferClosedError("contributions must be sealed first")
        return [
            SDKToolAdapter(
                item,
                source=self.source,
                instance_id=self.instance_id,
                plugin_version=self.plugin_version,
                permission_runtime=self.permission_runtime,
                permission_source_root=self.permission_source_root,
                permission_config_root=self.permission_config_root,
            )
            for item in self.tools
        ]

    @staticmethod
    def internal_event(event: PublicEvent) -> str:
        """把公开事件版本映射到唯一内部 Hook 名称。"""

        return _PUBLIC_EVENT_MAP[event]

    def event_handler(self, subscription: EventSubscription):
        """为只观察事件建立不允许写回 Host 的处理器。"""

        async def observer(
            _input_data: dict[str, Any],
            output_data: dict[str, Any],
        ) -> None:
            """投递内容受限的事件 Envelope，并丢弃 Handler 返回值。"""

            if subscription.event in {PublicEvent.SESSION_STARTED_V1, PublicEvent.SESSION_BOOTSTRAPPED_V1}:
                if self.permission_runtime is None or str(_input_data.get("__workspace_path") or _input_data.get("workdir") or "") != self.permission_runtime.workdir:
                    return

            required_permissions = _PUBLIC_EVENT_PERMISSIONS.get(
                subscription.event,
                frozenset(),
            )
            if required_permissions:
                runtime = self.permission_runtime
                if runtime is None:
                    return
                event_workspace = str(
                    _input_data.get("__workspace_path") or ""
                ).strip()
                if not event_workspace or event_workspace != runtime.workdir:
                    return
                if subscription.event.value.startswith("tcode.event.memory."):
                    scope = str(_input_data.get("scope") or "").strip().lower()
                    scope_key = str(_input_data.get("scope_key") or "").strip()
                    if scope not in {"global", "team", "worker", "session"}:
                        return
                    if (scope == "global" and scope_key) or (
                        scope != "global" and not scope_key
                    ):
                        return
                    target = "global" if scope == "global" else f"{scope}:{scope_key}"
                else:
                    session_id = str(_input_data.get("session_id") or "").strip()
                    target = f"session:{session_id}"
                if not target or target.endswith(":"):
                    return
                try:
                    for permission in required_permissions:
                        runtime.ensure_permission(permission, target)
                except PermissionError:
                    return

            plugins = output_data.get("plugins")
            errors = output_data.get("errors")
            event_data = (
                {}
                if subscription.event is PublicEvent.CONFIG_LOADED_V1
                else dict(
                    _safe_mapping(
                        {
                            key: value
                            for key, value in _input_data.items()
                            if not str(key).startswith("__")
                        }
                    )
                )
            )
            if subscription.event is PublicEvent.CONFIG_LOADED_V1:
                event_data.update(
                    {
                        "loaded_plugins": tuple(
                            str(item) for item in plugins
                        ) if isinstance(plugins, list) else (),
                        "error_count": len(errors) if isinstance(errors, Mapping) else 0,
                    }
                )
            envelope = EventEnvelope(
                event=subscription.event,
                source="tcode",
                plugin_instance_id=self.instance_id,
                data=MappingProxyType(event_data),
            )
            result = subscription.handler(envelope)
            if inspect.isawaitable(result):
                await result

        return observer


class ToolContributionAPI:
    """合并工具贡献写入口和可选只读目录。"""

    def __init__(self, buffer: ContributionBuffer, reader: Any) -> None:
        """绑定当前贡献缓冲区与只读工具目录。"""

        self._buffer = buffer
        self._reader = reader

    def register(self, definition: ToolDefinition) -> None:
        """登记一个工具贡献。"""

        self._buffer.add_tool(definition)

    def get(self, tool_id: str) -> Any:
        """读取一个 Host 工具描述。"""

        return self._reader.get(tool_id) if self._reader is not None else None

    def list(self, agent: Any = None, *, execution_profile: str | None = None) -> list[Any]:
        """列出当前 Agent 可见的 Host 工具描述。"""

        if self._reader is None:
            return []
        return self._reader.list(agent, execution_profile=execution_profile)

    def ids(self, agent: Any = None, *, execution_profile: str | None = None) -> list[str]:
        """列出当前 Agent 可见的 Host 工具 ID。"""

        if self._reader is None:
            return []
        return self._reader.ids(agent, execution_profile=execution_profile)

    async def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        timeout: int = 300,
    ) -> Mapping[str, Any]:
        """拒绝脱离 Agent 调用链直接执行 Host 工具。"""

        raise PermissionError(
            "tools.invoke is available only during an isolated plugin tool call"
        )


class PromptContributionAPI:
    """合并 Prompt 贡献入口和可选辅助校验。"""

    def __init__(self, buffer: ContributionBuffer, reader: Any) -> None:
        """绑定当前贡献缓冲区与只读 Prompt Service。"""

        self._buffer = buffer
        self._reader = reader

    def contribute(self, contribution: PromptContribution) -> None:
        """登记一个 Prompt Contribution。"""

        self._buffer.add_prompt(contribution)

    def validate_workpad_output(self, content: str) -> dict[str, Any]:
        """调用可选 Host 工作区输出校验器。"""

        if self._reader is None:
            raise RuntimeError("prompt validation service is unavailable")
        return self._reader.validate_workpad_output(content)


class EventContributionAPI:
    """合并公共事件订阅入口和可选只读目录。"""

    def __init__(self, buffer: ContributionBuffer, reader: Any) -> None:
        """绑定当前贡献缓冲区与只读事件目录。"""

        self._buffer = buffer
        self._reader = reader

    def subscribe(self, event: PublicEvent, handler: Any) -> None:
        """登记一个公共事件订阅。"""

        self._buffer.add_event(event, handler)

    def catalog(self) -> list[dict[str, Any]]:
        """返回 Host 允许公开的事件目录。"""

        return self._reader.catalog() if self._reader is not None else []


class VerifierContributionAPI:
    """只暴露 Verifier 定义登记，不暴露 Host 裁决器。"""

    def __init__(self, buffer: ContributionBuffer) -> None:
        self._buffer = buffer

    def register(self, definition: VerifierDefinition) -> None:
        """暂存 Verifier，待插件预热成功后由 Host 原子发布。"""

        self._buffer.add_verifier(definition)


__all__ = [
    "ContributionBuffer",
    "ContributionBufferClosedError",
    "EventContributionAPI",
    "EventSubscription",
    "PluginContributionError",
    "PromptContributionAPI",
    "SDKToolAdapter",
    "ToolContributionAPI",
    "UndeclaredContributionError",
    "VerifierContributionAPI",
]
