"""Tool capability metadata and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from ..runtime.computer import is_computer_tool


@dataclass(frozen=True)
class ToolCapability:
    tool_id: str
    side_effect_level: str = "medium"
    supports_async_submit: bool = False
    supports_resume: bool = False
    supports_review: bool = False
    artifact_contract: str = "none"
    concurrency_safe: bool = True
    interrupt_behavior: str = "best_effort"
    result_budget_tokens: int = 0
    preferred_profiles: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def readonly(self) -> bool:
        return self.side_effect_level == "none" or "read_only" in self.tags


# 内置工具能力元数据注册表。
# 为 agent 引擎/门控/调度/沙箱路由提供静态能力描述，无需实际调用工具即可判断其性质。
# 字段含义详见 docs/工具门控和调度_CAPABILITY_REFERENCE.md
# 注意：判断"是否为辅助工具"应使用 tags 而非 side_effect_level。
_CAPABILITY_MAP: dict[str, ToolCapability] = {
    "knowledge_search": ToolCapability("knowledge_search", side_effect_level="none", tags=("read_only", "memory")),
    "intelligent_page": ToolCapability("intelligent_page", side_effect_level="low", concurrency_safe=False, tags=("presentation", "local_fs")),
    "research_compare": ToolCapability("research_compare", side_effect_level="none", tags=("read_only", "research", "presentation", "local_fs")),
    "canvas": ToolCapability("canvas", side_effect_level="low", concurrency_safe=False,
                            tags=("presentation", "local_fs")),
    "structure_view": ToolCapability("structure_view", side_effect_level="low", concurrency_safe=False, tags=("structure", "presentation")),
    "read": ToolCapability("read", side_effect_level="none", tags=("read_only", "local_fs")),
    "inspect_file": ToolCapability(
        "inspect_file",
        side_effect_level="none",
        result_budget_tokens=4_000,
        tags=("read_only", "local_fs", "file_preflight"),
    ),
    "present_image": ToolCapability(
        "present_image",
        side_effect_level="none",
        artifact_contract="file",
        tags=("read_only", "local_fs", "presentation"),
    ),
    "look_at": ToolCapability(
        "look_at",
        side_effect_level="none",
        result_budget_tokens=4_000,
        tags=("read_only", "local_fs", "vision", "model_bridge"),
    ),
    "glob": ToolCapability("glob", side_effect_level="none", tags=("read_only", "local_fs")),
    "grep": ToolCapability("grep", side_effect_level="none", tags=("read_only", "local_fs")),
    "import_resource": ToolCapability(
        "import_resource",
        side_effect_level="high",
        artifact_contract="file",
        concurrency_safe=False,
        tags=("local_fs", "import", "sandbox_required"),
    ),
    "webfetch": ToolCapability("webfetch", side_effect_level="none", tags=("read_only", "web")),
    "websearch": ToolCapability("websearch", side_effect_level="none", tags=("read_only", "web")),
    "browser": ToolCapability(
        "browser",
        side_effect_level="high",
        artifact_contract="file",
        concurrency_safe=False,
        tags=("browser", "web", "ui", "sandbox_required"),
    ),
    "context_expand": ToolCapability("context_expand", side_effect_level="none", tags=("memory", "read_only")),
    "memory_read": ToolCapability("memory_read", side_effect_level="none", tags=("memory", "read_only")),
    "memory": ToolCapability("memory", side_effect_level="low", tags=("memory",)),
    "memory_write": ToolCapability("memory_write", side_effect_level="low", tags=("memory", "write")),
    "evidence_write": ToolCapability(
        "evidence_write",
        side_effect_level="low",
        artifact_contract="file",
        tags=("evidence", "memory", "write"),
    ),
    "evidence_search": ToolCapability(
        "evidence_search",
        side_effect_level="none",
        result_budget_tokens=6_000,
        tags=("evidence", "memory", "read_only"),
    ),
    "evidence_graph": ToolCapability(
        "evidence_graph",
        side_effect_level="low",
        artifact_contract="file",
        result_budget_tokens=8_000,
        tags=("evidence", "graph", "memory", "read_only"),
    ),
    "evidence_doctor": ToolCapability(
        "evidence_doctor",
        side_effect_level="low",
        artifact_contract="file",
        result_budget_tokens=8_000,
        tags=("evidence", "doctor", "memory", "read_only"),
    ),
    "research_read": ToolCapability(
        "research_read",
        side_effect_level="none",
        result_budget_tokens=8_000,
        preferred_profiles=("deep", "research-web", "paper-review"),
        tags=("research", "memory", "read_only", "progressive_read"),
    ),
    "research_workspace": ToolCapability(
        "research_workspace",
        side_effect_level="low",
        supports_review=True,
        result_budget_tokens=8_000,
        tags=("research", "control_plane", "review", "owner_scoped"),
    ),
    "research_action_commit": ToolCapability(
        "research_action_commit",
        side_effect_level="high",
        supports_review=True,
        concurrency_safe=False,
        result_budget_tokens=8_000,
        tags=("research", "control_plane", "write", "owner_scoped", "explicit_confirmation"),
    ),
    "protein_candidate": ToolCapability(
        "protein_candidate",
        side_effect_level="low",
        artifact_contract="file",
        supports_review=True,
        concurrency_safe=False,
        preferred_profiles=("protein-design", "orchestrated", "bio-safe"),
        tags=("protein", "artifact", "write"),
    ),
    "skill": ToolCapability("skill", side_effect_level="none", tags=("read_only", "skill")),
    "goal_checkpoint": ToolCapability('goal_checkpoint', side_effect_level='none', tags=('goal', 'checkpoint', 'progress', 'interaction')),
    "skill_run": ToolCapability(
        "skill_run",
        side_effect_level="high",
        artifact_contract="file",
        concurrency_safe=False,
        interrupt_behavior="cancel_process",
        tags=("skill", "shell", "sandbox_required"),
    ),
    "skill_install": ToolCapability(
        "skill_install",
        side_effect_level="high",
        concurrency_safe=False,
        tags=("skill", "network", "local_fs", "write", "install"),
    ),
    "skill_source": ToolCapability(
        "skill_source", side_effect_level="none", tags=("read_only", "skill", "network", "install"),
    ),
    "execute_code": ToolCapability(
        "execute_code",
        side_effect_level="low",
        concurrency_safe=False,
        preferred_profiles=("deep", "research-web", "paper-review"),
        tags=("read_only", "code_execution"),
    ),
    "question": ToolCapability(
        "question",
        side_effect_level="none",
        interrupt_behavior="human_review",
        tags=("interaction",),
    ),
    "plan_enter": ToolCapability("plan_enter", side_effect_level="none", tags=("planning",)),
    "plan_exit": ToolCapability("plan_exit", side_effect_level="none", tags=("planning",)),
    "todo": ToolCapability("todo", side_effect_level="low", tags=("planning", "write")),
    "todo_read": ToolCapability("todo_read", side_effect_level="none", tags=("planning", "read_only")),
    "todo_write": ToolCapability("todo_write", side_effect_level="low", tags=("planning", "write")),
    "task": ToolCapability(
        "task",
        side_effect_level="medium",
        supports_resume=True,
        concurrency_safe=False,
        preferred_profiles=("orchestrated", "manager-only"),
        tags=("orchestration",),
    ),
    "task_wait": ToolCapability(
        "task_wait",
        side_effect_level="none",
        concurrency_safe=True,
        preferred_profiles=("orchestrated", "manager-only"),
        tags=("orchestration", "read_only"),
    ),
    "subagent_guide": ToolCapability(
        "subagent_guide",
        side_effect_level="medium",
        concurrency_safe=False,
        preferred_profiles=("orchestrated", "manager-only"),
        tags=("orchestration",),
    ),
    "task_graph_read": ToolCapability(
        "task_graph_read",
        side_effect_level="none",
        tags=("team", "orchestration", "read_only"),
    ),
    "workflow_run": ToolCapability(
        "workflow_run",
        side_effect_level="high",
        supports_async_submit=True,
        supports_resume=True,
        supports_review=True,
        artifact_contract="job",
        concurrency_safe=False,
        interrupt_behavior="async_job",
        result_budget_tokens=12_000,
        preferred_profiles=("orchestrated",),
        tags=("workflow", "orchestration"),
    ),
    "workflow_list_templates": ToolCapability(
        "workflow_list_templates",
        side_effect_level="none",
        tags=("workflow", "orchestration", "read_only"),
    ),
    "write": ToolCapability("write", side_effect_level="high", artifact_contract="file", concurrency_safe=False, tags=("local_fs", "write")),
    "edit": ToolCapability("edit", side_effect_level="high", artifact_contract="file", concurrency_safe=False, tags=("local_fs", "write")),
    "bash": ToolCapability("bash", side_effect_level="high", artifact_contract="command", concurrency_safe=False, interrupt_behavior="cancel_process", result_budget_tokens=8_000, tags=("shell",)),
    "exec": ToolCapability("exec", side_effect_level="high", artifact_contract="command", concurrency_safe=False, interrupt_behavior="cancel_process", result_budget_tokens=8_000, tags=("shell",)),
    "process": ToolCapability("process", side_effect_level="high", artifact_contract="process", concurrency_safe=False, interrupt_behavior="cancel_process", result_budget_tokens=8_000, tags=("shell",)),
    "process_runtime": ToolCapability("process_runtime", side_effect_level="high", artifact_contract="process", concurrency_safe=False, interrupt_behavior="cancel_process", result_budget_tokens=8_000, tags=("shell",)),
    "result_item_read": ToolCapability("result_item_read", side_effect_level="none", tags=("artifact", "read_only")),
    "artifact_query": ToolCapability("artifact_query", side_effect_level="low", concurrency_safe=False, tags=("artifact", "local_fs")),
    "session_search": ToolCapability("session_search", side_effect_level="none", tags=("history", "read_only")),
    "ontology_query": ToolCapability("ontology_query", side_effect_level="none", tags=("knowledge", "read_only")),
    "computer": ToolCapability(
        "computer", side_effect_level="high", concurrency_safe=False,
        interrupt_behavior="human_review", result_budget_tokens=8_000,
        tags=("desktop", "ui", "explicit_confirmation"),
    ),
    "scheduled_task": ToolCapability(
        "scheduled_task",
        side_effect_level="medium",
        supports_async_submit=True,
        supports_resume=True,
        tags=("automation", "scheduling", "write"),
    ),
}


def get_tool_capability(tool_id: str) -> ToolCapability:
    normalized = str(tool_id or "").strip()
    if is_computer_tool(normalized):
        return _CAPABILITY_MAP["computer"]
    if normalized.startswith("mcp_"):
        return ToolCapability(
            tool_id=normalized,
            side_effect_level="high",
            supports_async_submit=True,
            supports_resume=True,
            supports_review=True,
            artifact_contract="job",
            concurrency_safe=False,
            interrupt_behavior="async_job",
            result_budget_tokens=12_000,
            preferred_profiles=("protein-design", "orchestrated"),
            tags=("mcp", "domain_job"),
        )
    mapped = _CAPABILITY_MAP.get(normalized)
    if mapped is not None:
        return mapped
    if normalized.startswith("team_") or normalized in {
        "create_worker",
        "assign_task",
        "list_teams",
        "list_workers",
        "terminate_worker",
        "ask_peer_question",
        "answer_peer_question",
        "team_memory_read",
        "team_memory_write",
        "task_graph",
        "task_graph_read",
    }:
        return ToolCapability(
            tool_id=normalized,
            side_effect_level="medium",
            supports_resume=True,
            concurrency_safe=False,
            preferred_profiles=("manager-only", "orchestrated"),
            tags=("team", "orchestration"),
        )
    return ToolCapability(tool_id=normalized, side_effect_level="medium", tags=("general",))
