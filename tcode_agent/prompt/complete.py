"""完整生产 Prompt 的不可变终态契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CompletePrompt:
    """保存交给模型 Provider 的最终 Prompt 与无正文元数据。"""

    content: str = field(repr=False)
    provider_id: str
    provider_version: str
    content_digest: str
    char_count: int
    byte_count: int
    assembly_phases: tuple[str, ...]


class CompletePromptProvider(Protocol):
    """定义完整 Prompt Provider 的最小内部接口。"""

    provider_id: str
    provider_version: str

    def assemble(self, production_prompt: str) -> CompletePrompt:
        """把已预算组装的正文转换为最终 Prompt。"""


class BuiltinCompletePromptProvider:
    """保持 Prompt 字节不变的唯一内建终态 Provider。"""

    provider_id = "tcode.prompt.complete.builtin"
    provider_version = "1"
    assembly_phases = (
        "agent-prompt-builder",
        "plugin-contribution-pipeline",
        "prompt-composer-budget-assembly",
    )

    def assemble(self, production_prompt: str) -> CompletePrompt:
        """建立不规范化、不截断且不追加内容的终态 Prompt。

        参数:
            production_prompt: 已完成全部 Section、贡献与预算处理的正文。

        返回:
            正文与输入逐字节相同的不可变 Prompt。

        异常:
            TypeError: 输入不是字符串时抛出。
        """

        if not isinstance(production_prompt, str):
            raise TypeError("production_prompt 必须是字符串")
        encoded = production_prompt.encode("utf-8")
        return CompletePrompt(
            content=production_prompt,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            content_digest="sha256:"
            + hashlib.sha256(encoded).hexdigest(),
            char_count=len(production_prompt),
            byte_count=len(encoded),
            assembly_phases=self.assembly_phases,
        )


class CompletePromptRegistry:
    """保存唯一内建 CompletePrompt Provider 的只读注册点。"""

    _provider: CompletePromptProvider = BuiltinCompletePromptProvider()

    @classmethod
    def assemble(cls, production_prompt: str) -> CompletePrompt:
        """调用唯一 Provider 生成模型实际消费的 Prompt。

        参数:
            production_prompt: 已完成预算组装的正文。

        返回:
            Provider 生成的不可变终态 Prompt。
        """

        return cls._provider.assemble(production_prompt)

    @classmethod
    def provider_identity(cls) -> dict[str, str]:
        """返回不含 Prompt 正文的 Provider 身份。"""

        return {
            "provider_id": cls._provider.provider_id,
            "provider_version": cls._provider.provider_version,
        }


__all__ = [
    "BuiltinCompletePromptProvider",
    "CompletePrompt",
    "CompletePromptProvider",
    "CompletePromptRegistry",
]
