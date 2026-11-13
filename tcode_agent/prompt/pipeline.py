"""内建 Section 与插件 Prompt Contribution 的单轨组装器。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from ..plugin_sdk import PromptRenderContext
from ..runtime.plugin_admission import public_workspace_id
from .registry import PromptRegistry


@dataclass(frozen=True, slots=True)
class PromptContributionReceipt:
    """保存一次生产组装的无正文回执。"""

    source: str
    name: str
    content_digest: str
    char_count: int
    truncated: bool
    required: bool


class PromptContributionPipeline:
    """把已发布的插件贡献合并到唯一生产 Prompt。"""

    def __init__(self, *, render_timeout_seconds: float = 0.5) -> None:
        """初始化有界渲染时限。

        参数:
            render_timeout_seconds: 所有插件 Prompt 本轮允许的总渲染时间。
        """

        self.render_timeout_seconds = max(
            0.01,
            min(float(render_timeout_seconds), 5.0),
        )

    async def compose(
        self,
        base_prompt: str,
        *,
        workdir: str,
        agent: str = "",
        variables: Mapping[str, Any] | None = None,
        structured_output: bool = False,
    ) -> tuple[str, tuple[PromptContributionReceipt, ...]]:
        """渲染并按稳定顺序追加当前工作区的插件贡献。

        参数:
            base_prompt: 已组装内建 Section 的生产 Prompt。
            workdir: 用于派生不透明工作区标识的控制目录。
            agent: 当前 Agent 名称，只在本次渲染上下文中使用。
            variables: 可供公开 Renderer 读取的有界变量。

        返回:
            最终 Prompt 与不含正文的贡献回执。没有贡献时逐字节返回输入。

        异常:
            TimeoutError: 渲染超过硬时限时失败关闭，避免半组装 Prompt。
            PromptContributionError: 必需贡献失败或返回非法内容时抛出。
        """

        context = PromptRenderContext(
            workspace_id=public_workspace_id(workdir),
            agent=str(agent or ""),
            variables=dict(variables or {}),
        )
        rendered = await asyncio.wait_for(
            PromptRegistry.render(context, structured_output=structured_output),
            timeout=self.render_timeout_seconds,
        )
        if not rendered:
            return base_prompt, ()
        sections = [base_prompt]
        receipts: list[PromptContributionReceipt] = []
        for item in rendered:
            sections.append(
                "\n\n# Plugin Prompt Contribution: "
                f"{item.source}/{item.name}\n\n{item.content}"
            )
            receipts.append(
                PromptContributionReceipt(
                    source=item.source,
                    name=item.name,
                    content_digest="sha256:"
                    + hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    char_count=len(item.content),
                    truncated=item.truncated,
                    required=item.required,
                )
            )
        return "".join(sections), tuple(receipts)


__all__ = ["PromptContributionPipeline", "PromptContributionReceipt"]
