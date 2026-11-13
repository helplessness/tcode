"""
TCode Agent Python Package

A Python implementation of the TCode agent system with:
- Agent react loop
- Tool system with MCP integration
- Multiple LLM provider support
- Skill system for specialized tasks
"""

from .types import (
    AgentInfo,
    AgentMode,
    ToolCall,
    ToolResult,
    ToolState,
    Message,
    PermissionRule,
)

__version__ = "0.0.2"

__all__ = [
    "Agent",
    "AgentInfo",
    "AgentMode",
    "ToolCall",
    "ToolResult",
    "ToolState",
    "Message",
    "PermissionRule",
    "ToolRegistry",
    "MCPManager",
    "MCPStatus",
    "AnthropicProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "SkillManager",
    "SkillInfo",
    "TaskManager",
    "TaskInfo",
    "MemoryManager",
    "MemoryEntry",
]


def __getattr__(name: str):
    if name == "Agent":
        from .agent.agent import Agent

        return Agent
    if name == "ToolRegistry":
        from .tools.registry import ToolRegistry

        return ToolRegistry
    if name in {"MCPManager", "MCPStatus"}:
        from .mcp.manager import MCPManager, MCPStatus

        return {"MCPManager": MCPManager, "MCPStatus": MCPStatus}[name]
    if name in {"AnthropicProvider", "OpenAIProvider", "OllamaProvider"}:
        from . import providers

        return getattr(providers, name)
    if name in {"SkillManager", "SkillInfo"}:
        from .skill import SkillInfo, SkillManager

        return {"SkillManager": SkillManager, "SkillInfo": SkillInfo}[name]
    if name in {"TaskManager", "TaskInfo"}:
        from .task import TaskInfo, TaskManager

        return {"TaskManager": TaskManager, "TaskInfo": TaskInfo}[name]
    if name in {"MemoryManager", "MemoryEntry"}:
        from .memory import MemoryEntry, MemoryManager

        return {"MemoryManager": MemoryManager, "MemoryEntry": MemoryEntry}[name]
    raise AttributeError(name)
