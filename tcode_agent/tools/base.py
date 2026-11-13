"""Tool system base classes"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Type

import pydantic

from ..types import ToolResult
from .capabilities import ToolCapability, get_tool_capability


class ParameterSchema(pydantic.BaseModel):
    """Base class for tool parameter schemas"""

    pass


@dataclass
class ToolContext:
    """Context passed to tool execution"""

    session_id: str
    message_id: str
    agent: str
    abort: Any  # asyncio.Event
    call_id: Optional[str] = None
    extra: Optional[dict[str, Any]] = None
    messages: list = field(default_factory=list)
    progress_callback: Any = None

    def metadata(self, title: Optional[str] = None, metadata: Optional[dict] = None):
        """Set metadata for the tool call"""
        pass

    async def ask(self, permission: str, patterns: list[str]) -> bool:
        """Ask user for permission"""
        if self.extra:
            callback = self.extra.get("__permission_ask__")
            if callback:
                result = callback(permission, patterns)
                if hasattr(result, "__await__"):
                    return await result
                return bool(result)
        return True

    async def report_progress(self, event: Any) -> None:
        """Report semantic progress without creating another model turn."""
        callback = self.progress_callback
        if not callable(callback) and self.extra:
            callback = self.extra.get("__tool_progress_report__")
        if not callable(callback):
            return
        result = callback(event)
        if hasattr(result, "__await__"):
            await result


class Tool(ABC):
    """Base class for all tools"""

    @property
    @abstractmethod
    def id(self) -> str:
        """Tool identifier"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description for LLM"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> Type[ParameterSchema]:
        """Pydantic model for parameters"""
        pass

    @property
    def capability(self) -> ToolCapability:
        """Capability metadata used by execution profiles and policy layers."""
        return get_tool_capability(self.id)

    @property
    def permission_dependencies(self) -> tuple[str, ...]:
        """Other tool permissions required for this tool to work end to end."""
        return ()

    def available_to_agent(self, agent_name: str) -> bool:
        """Definition admission; runtime permission checks remain authoritative."""
        return True

    @abstractmethod
    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool"""
        pass


def create_schema(fields: dict[str, tuple[type, Any]]) -> Type[ParameterSchema]:
    """Create a parameter schema from field definitions"""
    annotations = {}
    defaults = {}

    for name, (field_type, default) in fields.items():
        annotations[name] = Optional[field_type]
        if default is not None:
            defaults[name] = default

    return pydantic.create_model(
        "ToolParameters", **defaults, __annotations__=annotations
    )
