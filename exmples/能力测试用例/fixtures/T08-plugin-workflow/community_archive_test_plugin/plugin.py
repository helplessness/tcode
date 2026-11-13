"""从 ZIP 安装的 TCode 第三方插件测试入口。"""

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
    """提供回显和统计工具，用于证明压缩包中的代码被真实加载。"""

    info = PluginMetadata(
        name="community-archive-test-plugin",
        version="0.1.0",
        description="第三方插件 ZIP 安装与 Workflow 冒烟 Fixture。",
        author="TCode Community Fixture",
        plugin_type=("tool", "workflow"),
    )

    def __init__(self) -> None:
        """初始化未绑定的公共上下文和默认回显前缀。"""

        self._context: PluginContext | None = None
        self._prefix = "ARCHIVE_TEST"

    async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
        """读取宿主校验后的配置并登记两个无副作用工具。"""

        self._context = ctx
        self._prefix = str(config.get("prefix") or self._prefix)
        ctx.tools.register(
            ToolDefinition(
                name="community_archive_echo",
                description="回显 ZIP 插件收到的文本，用于确认真实插件调用。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                        }
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._echo,
                side_effect_level="none",
            )
        )
        ctx.tools.register(
            ToolDefinition(
                name="community_archive_count",
                description="统计 ZIP 插件输入文本的字符、单词或行数。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "maxLength": 2000},
                        "mode": {
                            "type": "string",
                            "enum": ["chars", "words", "lines"],
                            "default": "chars",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=self._count,
                side_effect_level="none",
            )
        )

    async def health(self) -> PluginHealth:
        """报告 ZIP 插件是否已经完成公共 Context 绑定。"""

        if self._context is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        return PluginHealth(PluginHealthStatus.HEALTHY, "ZIP 测试插件已就绪")

    async def stop(self, _reason: str = "unload") -> None:
        """卸载时释放公共 Context，且不留下外部资源。"""

        self._context = None

    async def _echo(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """返回带配置前缀的确定性回显结果。"""

        text = str(arguments.get("text") or "")
        return ToolOutput(
            "ZIP 插件回显",
            f"{self._prefix}: {text}",
            {"prefix": self._prefix, "length": len(text)},
        )

    async def _count(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """按指定模式统计文本并返回结构化计数元数据。"""

        text = str(arguments.get("text") or "")
        mode = str(arguments.get("mode") or "chars")
        if mode == "words":
            count = len(text.split())
        elif mode == "lines":
            count = len(text.splitlines())
        else:
            mode = "chars"
            count = len(text)
        return ToolOutput(
            "ZIP 插件统计",
            str(count),
            {"mode": mode, "count": count},
        )
