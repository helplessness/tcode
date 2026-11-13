"""Harness 核心组件的轻量公开导出。

这里放 Agent 的最小执行骨架，尽量不掺杂团队、插件或领域工作流。
公开对象使用惰性导入，避免轻量 Policy/Guard 契约反向触发 Agent、Tool
和 PluginManager 的注册链。
"""

from __future__ import annotations

__all__ = [
    "AgentHarnessEngine",
    "AgentPromptBuilder",
    "AgentToolExecutor",
    "TurnDriver",
    "TurnProjection",
]


def __getattr__(name: str):
    """Resolve one public Harness class without eager package side effects.

    Args:
        name: Attribute requested through ``tcode_agent.harness``.

    Returns:
        The requested Harness class loaded from its owning module.

    Raises:
        AttributeError: If the name is not part of the documented public set.
    """

    if name == "AgentHarnessEngine":
        from .engine import AgentHarnessEngine

        return AgentHarnessEngine
    if name == "AgentPromptBuilder":
        from .prompt_builder import AgentPromptBuilder

        return AgentPromptBuilder
    if name == "AgentToolExecutor":
        from .tool_executor import AgentToolExecutor

        return AgentToolExecutor
    if name in {"TurnDriver", "TurnProjection"}:
        from .turn_driver import TurnDriver, TurnProjection

        return {"TurnDriver": TurnDriver, "TurnProjection": TurnProjection}[name]
    raise AttributeError(name)
