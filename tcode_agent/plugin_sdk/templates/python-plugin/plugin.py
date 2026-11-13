"""{{plugin_name}} 的 TCode Python Plugin SDK v1 入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tcode_agent.plugin_sdk import (
    PluginContext,
    PluginHealth,
    PluginHealthStatus,
    PluginMetadata,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)


class Plugin:
    """提供一个无副作用的 SDK v1 示例工具。"""

    info = PluginMetadata(
        name="{{plugin_name}}",
        version="0.1.0",
        description="TCode Python SDK v1 插件。",
        plugin_type=("tool",),
    )

    def __init__(self) -> None:
        """初始化默认配置和未绑定的公开 Context。"""

        self._context: PluginContext | None = None
        self._prefix = "{{plugin_name}}"

    async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
        """读取已校验配置并登记示例工具。"""

        self._context = ctx
        self._prefix = str(config.get("prefix") or self._prefix)
        ctx.tools.register(
            ToolDefinition(
                name="{{tool_name}}",
                description="返回带插件前缀的文本。",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._echo,
            )
        )

    async def health(self) -> PluginHealth:
        """报告插件是否已完成 Context 绑定。"""

        if self._context is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        return PluginHealth(PluginHealthStatus.HEALTHY, "插件已就绪")

    async def stop(self, _reason: str = "unload") -> None:
        """卸载时释放对公开 Context 的引用。"""

        self._context = None

    async def _echo(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """生成一个不读写外部资源的纯计算结果。"""

        text = str(arguments.get("text") or "")
        return ToolOutput("{{plugin_name}}", f"{self._prefix}: {text}")
