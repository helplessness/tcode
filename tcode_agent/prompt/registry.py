"""按插件实例持有的生产 Prompt Contribution 注册表。"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Collection, Mapping

from ..plugin_sdk import PromptContribution, PromptRenderContext
from ..runtime.registrations import RegistrationDisposer, RegistrationToken


class PromptContributionError(RuntimeError):
    """表示插件 Prompt 无法形成合法生产输入。"""

    pass


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """保存一条工作区级插件 Prompt 注册记录。"""

    key: str
    workspace_id: str
    source: str
    instance_id: str
    contribution: PromptContribution
    digest: str


@dataclass(frozen=True, slots=True)
class RenderedPromptContribution:
    """保存经过预算限制的 Prompt Contribution 正文。"""

    name: str
    source: str
    content: str
    truncated: bool
    required: bool


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class PromptRegistry(metaclass=WorkspaceRegistryMeta):

    """为唯一生产 Prompt 管线提供实例级注册、渲染与精确卸载。"""
    _workspace_fields = ('_records', '_registrations')

    _records: dict[str, PromptRecord] = {}
    _registrations: dict[str, RegistrationToken] = {}

    @staticmethod
    def _key(workspace_id: str, name: str) -> str:
        """生成不会跨工作区冲突的注册键。"""

        return f"{workspace_id}:{name}"

    @classmethod
    def register(
        cls,
        contribution: PromptContribution,
        *,
        workspace_id: str,
        source: str,
        instance_id: str,
        replace: bool = False,
    ) -> RegistrationDisposer:
        """注册一条贡献，并返回只移除本次注册的 disposer。"""

        key = cls._key(workspace_id, contribution.name)
        if not replace and key in cls._records:
            current = cls._records[key]
            raise ValueError(
                f"prompt contribution conflict: {contribution.name!r} already "
                f"registered from {current.source}"
            )
        digest = hashlib.sha256(
            (
                contribution.name
                + "\0"
                + contribution.version
                + "\0"
                + contribution.text
            ).encode("utf-8")
        ).hexdigest()
        token = RegistrationToken.create(
            registry="prompts",
            key=key,
            source=source,
            instance_id=instance_id,
        )
        cls._records[key] = PromptRecord(
            key=key,
            workspace_id=workspace_id,
            source=source,
            instance_id=instance_id,
            contribution=contribution,
            digest=digest,
        )
        cls._registrations[key] = token
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        """仅在 token 与当前所有者完全一致时移除贡献。"""

        if cls._registrations.get(token.key) != token:
            return False
        cls._records.pop(token.key, None)
        cls._registrations.pop(token.key, None)
        return True

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        """移除一个插件实例拥有的全部 Prompt 贡献。"""

        removed: list[str] = []
        for key, token in list(cls._registrations.items()):
            if token.instance_id == instance_id and cls.unregister_exact(token):
                removed.append(key)
        return removed

    @classmethod
    def unregister_by_source(
        cls, source: str, *, legacy_only: bool = False
    ) -> list[str]:
        """按来源清理贡献；可限制为没有 token 的旧注册。"""

        removed: list[str] = []
        for key, record in list(cls._records.items()):
            if record.source != source:
                continue
            token = cls._registrations.get(key)
            if legacy_only and token is not None:
                continue
            if token is not None:
                if cls.unregister_exact(token):
                    removed.append(key)
            else:
                cls._records.pop(key, None)
                removed.append(key)
        return removed

    @classmethod
    def catalog(cls, workspace_id: str | None = None) -> list[Mapping[str, Any]]:
        """返回不含正文、按生产顺序排列的注册目录。"""

        records = [
            record
            for record in cls._records.values()
            if workspace_id is None or record.workspace_id == workspace_id
        ]
        records.sort(
            key=lambda item: (
                item.contribution.order,
                item.source,
                item.contribution.name,
            )
        )
        return [
            MappingProxyType(
                {
                    "name": item.contribution.name,
                    "version": item.contribution.version,
                    "scope": item.contribution.scope,
                    "order": item.contribution.order,
                    "token_budget": item.contribution.token_budget,
                    "required": item.contribution.required,
                    "source": item.source,
                    "instance_id": item.instance_id,
                    "digest": item.digest,
                }
            )
            for item in records
        ]

    @classmethod
    async def render(
        cls,
        context: PromptRenderContext,
        *,
        sources: Collection[str] | None = None,
        structured_output: bool = False,
    ) -> tuple[RenderedPromptContribution, ...]:
        """按稳定顺序渲染贡献并执行逐项字符预算。"""

        selected_sources = frozenset(sources) if sources is not None else None
        output: list[RenderedPromptContribution] = []
        records = [
            item
            for item in cls._records.values()
            if item.workspace_id == context.workspace_id
            and (selected_sources is None or item.source in selected_sources)
        ]
        records.sort(
            key=lambda item: (
                item.contribution.order,
                item.source,
                item.contribution.name,
            )
        )
        for record in records:
            contribution = record.contribution
            if structured_output and not contribution.structured_output_safe:
                continue
            if contribution.agents and context.agent not in contribution.agents:
                continue
            content: Any = contribution.text
            if contribution.renderer is not None:
                content = contribution.renderer(context)
                if inspect.isawaitable(content):
                    content = await content
            if not isinstance(content, str):
                raise PromptContributionError(
                    f"prompt renderer {contribution.name!r} must return a string"
                )
            max_chars = contribution.token_budget * 4
            truncated = len(content) > max_chars
            if truncated and contribution.required:
                raise PromptContributionError(
                    f"required prompt {contribution.name!r} exceeded its token budget"
                )
            if truncated:
                content = content[:max_chars]
            output.append(
                RenderedPromptContribution(
                    name=contribution.name,
                    source=record.source,
                    content=content,
                    truncated=truncated,
                    required=contribution.required,
                )
            )
        return tuple(output)

    @classmethod
    def clear(cls) -> None:
        """清空进程级注册表，仅供测试与进程关闭使用。"""

        cls._records.clear()
        cls._registrations.clear()


__all__ = [
    "PromptContributionError",
    "PromptRecord",
    "PromptRegistry",
    "RenderedPromptContribution",
]
