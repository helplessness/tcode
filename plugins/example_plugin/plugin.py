"""使用 TCode Python Plugin SDK v1 实现的官方最小参考插件。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tcode_agent.plugin_sdk import (
    EventEnvelope,
    PluginContext,
    PluginHealth,
    PluginHealthStatus,
    PluginMetadata,
    PromptContribution,
    ProviderDefinition,
    PublicEvent,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)


class Plugin:
    """演示 Tool、Prompt、Event、Memory、配置和日志公开接口。"""

    info = PluginMetadata(
        name="example-plugin",
        version="1.2.0",
        description="TCode Plugin SDK v1 官方最小参考插件。",
        author="TCode",
        plugin_type=("tool", "prompt", "event", "workflow"),
    )

    def __init__(self) -> None:
        """初始化默认前缀、事件快照和未绑定的公开上下文。"""

        self._context: PluginContext | None = None
        self._memory_available = False
        self._default_prefix = "Echo"
        self.last_event: EventEnvelope | None = None

    async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
        """读取已校验配置并一次性登记全部公开贡献。"""

        self._context = ctx
        self._memory_available = ctx.memory is not None
        self._default_prefix = str(config.get("default_prefix") or "Echo")
        ctx.tools.register(
            ToolDefinition(
                name="example_echo",
                description="使用插件默认前缀或调用参数回显文本。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "echo_text": {"type": "string"},
                        "prefix": {"type": "string"},
                    },
                    "required": ["echo_text"],
                    "additionalProperties": False,
                },
                handler=self._echo,
            )
        )
        ctx.tools.register(
            ToolDefinition(
                name="example_count",
                description="统计输入文本的字符、单词或行数。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "count_type": {
                            "type": "string",
                            "enum": ["chars", "words", "lines"],
                            "default": "chars",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._count,
            )
        )
        ctx.prompt.contribute(
            PromptContribution(
                name="example_guidance",
                text="调用示例工具时，优先返回简洁、可验证的结果。",
                order=200,
                token_budget=64,
            )
        )
        ctx.events.subscribe(PublicEvent.CONFIG_LOADED_V1, self._on_config_loaded)
        ctx.logger.info("example-plugin 已通过公开 SDK 完成初始化")

    async def health(self) -> PluginHealth:
        """报告插件是否已成功绑定公开上下文。"""

        if self._context is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        return PluginHealth(PluginHealthStatus.HEALTHY, "插件已就绪")

    async def stop(self, _reason: str = "unload") -> None:
        """卸载时释放上下文和最近事件，不保留宿主对象。"""

        self._context = None
        self._memory_available = False
        self.last_event = None

    async def _echo(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """返回带显式或默认前缀的输入文本。"""

        text = str(arguments.get("echo_text") or "")
        prefix = str(arguments.get("prefix") or self._default_prefix)
        return ToolOutput(
            "示例回显",
            f"{prefix}: {text}",
            {"prefix": prefix, "length": len(text)},
        )

    async def _count(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """按调用方选择的口径统计文本长度。"""

        text = str(arguments.get("text") or "")
        count_type = str(arguments.get("count_type") or "chars")
        if count_type == "words":
            count = len(text.split())
        elif count_type == "lines":
            count = len(text.splitlines())
        else:
            count_type = "chars"
            count = len(text)
        return ToolOutput(
            "示例统计",
            str(count),
            {"count_type": count_type, "count": count},
        )

    async def _on_config_loaded(self, envelope: EventEnvelope) -> None:
        """只保存经过宿主裁剪的公共配置加载事件。"""

        self.last_event = envelope


# 此类型仅用于让参考包编译期覆盖稳定 Provider 数据合同；SDK v1
# 当前不授权插件替换生产 Provider，因此不会向宿主注册该定义。
_PROVIDER_CONTRACT_TYPE = ProviderDefinition
