"""Memory provider abstractions for prompt recall and future backend expansion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MemoryPromptSection:
    title: str
    content: str
    guidance: str = ""
    provider: str = ""


@dataclass
class RecalledMemory:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryProvider(ABC):
    name: str = "memory"

    @abstractmethod
    def render_prompt_section(self, goal: str = "") -> Optional[MemoryPromptSection]:
        """Render a prompt section for the current agent context."""

    def prefetch(self, goal: str = "") -> list[RecalledMemory]:
        """Optional hook for future async prefetch behavior."""
        return []

    def sync(self, *args, **kwargs) -> None:
        """Optional hook for future sync behavior."""
        return None
