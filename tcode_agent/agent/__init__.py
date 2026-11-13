"""Agent 公共导出，并避免包初始化阶段加载完整运行时。"""

from __future__ import annotations

from importlib import import_module

__all__ = ["Agent", "BuiltinAgents"]


_PUBLIC_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": (".agent", "Agent"),
    "BuiltinAgents": (".agents", "BuiltinAgents"),
}


def __getattr__(name: str):
    """按需解析 Agent 包公开对象并缓存结果。

    参数:
        name: 调用方通过 ``tcode_agent.agent`` 请求的属性名。

    返回:
        对应子模块中的公开对象。首次解析后会写入包全局变量，后续访问
        保持普通模块属性的对象身份与访问成本。

    异常:
        AttributeError: 请求的名称不属于公开导出。

    该惰性边界允许插件管理器只加载 ``BuiltinAgents``，而不会在包初始化
    期间提前加载会反向依赖插件管理器的完整 ``Agent`` 运行时。
    """

    target = _PUBLIC_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
