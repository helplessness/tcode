"""Helpers for assembling prompt context from rules and memory providers."""

from __future__ import annotations

from typing import Any

from ..learning import LearningManager
from ..message_context import latest_user_goal
from ..memory.provider_manager import MemoryProviderManager
from ..rule import RuleManager


class ProjectPromptAssembler:
    """Assemble reusable prompt sections for the runtime."""

    def __init__(self, agent: Any):
        self.agent = agent

    def build_project_rules_section(self) -> str:
        RuleManager.load(workdir=self.agent.workdir)
        return RuleManager.get_rules()

    def build_memory_sections(self, goal: str) -> list[tuple[str, str, str]]:
        manager = MemoryProviderManager.from_agent(self.agent)
        return manager.build_prompt_sections(goal=goal)

    def build_learning_sections(self, goal: str) -> list[tuple[str, str, str]]:
        return LearningManager.build_prompt_sections(self.agent, goal)
