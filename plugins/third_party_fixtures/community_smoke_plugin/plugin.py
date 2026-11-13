"""使用 TCode Python Plugin SDK v1 编写的第三方冒烟测试插件。"""

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
    """提供两个无外部权限的公共 SDK Tool，供安装和 Workflow 验收使用。"""

    info = PluginMetadata(
        name="community-smoke-plugin",
        version="0.1.0",
        description="TCode 第三方社区插件冒烟测试 Fixture。",
        author="TCode Community Example",
        plugin_type=("tool", "workflow"),
    )

    def __init__(self) -> None:
        """初始化未绑定的插件上下文和安全的默认输出前缀。"""

        self._context: PluginContext | None = None
        self._prefix = "THIRD_PARTY"

    async def start(self, ctx: PluginContext, config: Mapping[str, Any]) -> None:
        """读取宿主已校验的配置，并只登记 Manifest 允许的 Tool。"""

        self._context = ctx
        self._prefix = str(config.get("prefix") or self._prefix)
        ctx.tools.register(
            ToolDefinition(
                name="community_smoke_echo",
                description="使用第三方插件配置的前缀回显输入文本。",
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
                name="community_smoke_count",
                description="统计输入文本的字符数、单词数或行数。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "maxLength": 2000,
                        },
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
        """报告插件是否已经完成公开 SDK Context 绑定。"""

        if self._context is None:
            return PluginHealth(PluginHealthStatus.UNHEALTHY, "插件尚未启动")
        return PluginHealth(PluginHealthStatus.HEALTHY, "第三方测试插件已就绪")

    async def stop(self, _reason: str = "unload") -> None:
        """卸载时释放宿主公开 Context，且允许重复调用。"""

        self._context = None

    async def _echo(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """返回带配置前缀的纯文本结果，不访问网络、进程或文件。"""

        text = str(arguments.get("text") or "")
        return ToolOutput(
            "第三方插件回显",
            f"{self._prefix}: {text}",
            {"prefix": self._prefix, "length": len(text)},
        )

    async def _count(
        self,
        arguments: Mapping[str, Any],
        _call: ToolCallContext,
    ) -> ToolOutput:
        """按调用方指定口径统计文本，并返回可机器校验的元数据。"""

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
            "第三方插件统计",
            str(count),
            {"mode": mode, "count": count},
        )
