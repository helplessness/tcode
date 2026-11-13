"""Provider manager for global/team/shared memory prompt injection."""

from __future__ import annotations

from typing import Any

from .provider import MemoryPromptSection, MemoryProvider
from .manager import MemoryManager
from .retriever import build_embedding, cosine_similarity


def _is_top_match(
    item: Any,
    all_items: list[Any],
    goal: str,
    *,
    threshold: float = 0.3,
    top_n: int = 2,
) -> bool:
    """Check if an item is a top semantic match worth escalating to L2."""
    if not goal:
        return False
    goal_vec = build_embedding(goal)
    scored = []
    for it in all_items:
        item_vec = getattr(it, "embedding", None)
        if not item_vec:
            continue
        score = cosine_similarity(goal_vec, item_vec)
        scored.append((it, score))
    scored.sort(key=lambda p: p[1], reverse=True)
    top_set = {id(it) for it, _ in scored[:top_n]}
    return id(item) in top_set and (scored[0][1] if scored else 0) >= threshold


class GlobalMemoryProvider(MemoryProvider):
    name = "global"

    def __init__(self, workdir: str | None):
        self.workdir = workdir

    def render_prompt_section(self, goal: str = "") -> MemoryPromptSection | None:
        if goal:
            items = MemoryManager.query_related(
                workdir=self.workdir,
                goal=goal,
                query=goal,
                limit=8,
                level="l0",
            )
            content_lines = []
            for item in items:
                line = f"- [memory:{item.id[:8]}] {item.content}"
                if item.tags:
                    line += f" [tags: {', '.join(item.tags)}]"
                content_lines.append(line)
                # Escalate top semantic matches to L2 for richer context
                if getattr(item, "l2_detail", "") and _is_top_match(item, items, goal):
                    content_lines.append(f"  [detail] {item.l2_detail[:300]}")
            content = "\n".join(content_lines)
        else:
            content = MemoryManager.render_prompt_context(workdir=self.workdir)
        if not content:
            return None
        return MemoryPromptSection(
            title="Persistent Memory",
            content=content,
            guidance="Use this memory to personalize responses and avoid asking for known preferences again.",
            provider=self.name,
        )


class TeamMemoryProvider(MemoryProvider):
    name = "team"

    def __init__(self, team_id: str, workdir: str | None):
        self.team_id = team_id
        self.workdir = workdir

    def render_prompt_section(self, goal: str = "") -> MemoryPromptSection | None:
        from ..team.memory import TeamMemoryManager

        if goal:
            items = TeamMemoryManager.query_related(
                self.team_id,
                self.workdir,
                goal=goal,
                query=goal,
                limit=10,
                level="l0",
            )
            content_lines = []
            for item in items:
                line = f"[{item.source}][memory:{item.id[:8]}] {item.content}"
                if item.tags:
                    line += f" (tags: {', '.join(item.tags)})"
                content_lines.append(line)
                if getattr(item, "l2_detail", "") and _is_top_match(item, items, goal):
                    content_lines.append(f"  [detail] {item.l2_detail[:300]}")
            content = "\n".join(content_lines)
        else:
            content = TeamMemoryManager.render_context(self.team_id, self.workdir)
        if not content:
            return None
        return MemoryPromptSection(
            title="Team Long-term Memory",
            content=content,
            guidance="以上是本团队的历史记忆，包含 Worker 完成任务的结论、项目决策等。请参考这些信息避免重复工作。",
            provider=self.name,
        )


class SharedWorkerMemoryProvider(MemoryProvider):
    name = "shared_worker"

    def __init__(self, memory_key: str, workdir: str | None):
        self.memory_key = memory_key
        self.workdir = workdir

    def render_prompt_section(self, goal: str = "") -> MemoryPromptSection | None:
        from ..team.worker_memory import WorkerMemoryManager

        if goal:
            items = WorkerMemoryManager.query_related(
                self.memory_key,
                self.workdir,
                goal=goal,
                query=goal,
                limit=10,
                level="l0",
            )
            content_lines = []
            for item in items:
                line = f"[{item.source}][memory:{item.id[:8]}] {item.content}"
                if item.tags:
                    line += f" (tags: {', '.join(item.tags)})"
                content_lines.append(line)
                if getattr(item, "l2_detail", "") and _is_top_match(item, items, goal):
                    content_lines.append(f"  [detail] {item.l2_detail[:300]}")
            content = "\n".join(content_lines)
        else:
            content = WorkerMemoryManager.render_context(self.memory_key, self.workdir)
        if not content:
            return None
        return MemoryPromptSection(
            title="Shared Worker Memory",
            content=content,
            guidance="以上是当前执行链共享的长期记忆。你和临时 helper 要把它当成同一套工作记忆来读写，避免重复分析。",
            provider=self.name,
        )


class MemoryProviderManager:
    """Resolve applicable memory providers for an agent/session context."""

    def __init__(self, providers: list[MemoryProvider]):
        self.providers = providers

    @classmethod
    def from_agent(cls, agent: Any) -> "MemoryProviderManager":
        providers: list[MemoryProvider] = [GlobalMemoryProvider(agent.workdir)]
        from ..types import AgentMode

        if getattr(agent.agent_info, "mode", None) == AgentMode.MANAGER:
            providers.append(TeamMemoryProvider(agent.session_id, agent.workdir))

        shared_memory_key = str(getattr(agent, "_shared_memory_key", "") or "").strip()
        if shared_memory_key:
            providers.append(SharedWorkerMemoryProvider(shared_memory_key, agent.workdir))
        return cls(providers)

    def build_prompt_sections(self, *, goal: str = "") -> list[tuple[str, str, str]]:
        sections: list[tuple[str, str, str]] = []
        for provider in self.providers:
            section = provider.render_prompt_section(goal=goal)
            if not section or not str(section.content or "").strip():
                continue
            sections.append((section.title, section.content, section.guidance))
        return sections
