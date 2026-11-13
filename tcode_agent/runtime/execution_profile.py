"""Execution profile definitions and tool gating."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..tools.capabilities import ToolCapability


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    description: str
    prompt_modifier: str = ""
    prompt_budget_tokens: int = 16_000
    input_char_multiplier: float = 1.0
    retry_char_multiplier: float = 1.0
    worker_prompt_multiplier: float = 1.0
    worker_result_multiplier: float = 1.0
    worker_review_multiplier: float = 1.0
    task_done_multiplier: float = 1.0
    allowed_tags: tuple[str, ...] = field(default_factory=tuple)
    blocked_tags: tuple[str, ...] = field(default_factory=tuple)
    blocked_tools: tuple[str, ...] = field(default_factory=tuple)
    allow_all: bool = False


_PROFILES: dict[str, ExecutionProfile] = {
    "fast": ExecutionProfile(
        name="fast",
        description="快速执行配置，收紧上下文预算，尽量减少额外探索与扩展能力加载。",
        prompt_modifier=(
            "# Fast Mode\n\n"
            "You are in fast execution mode. Prioritize the shortest reliable path.\n"
            "- Keep exploration and planning minimal.\n"
            "- Prefer direct file reads/searches over broad scans.\n"
            "- Use concise answers and avoid non-essential background context.\n"
            "- Skip todo creation unless the task clearly has many independent steps."
        ),
        prompt_budget_tokens=8_000,
        input_char_multiplier=0.55,
        retry_char_multiplier=0.60,
        worker_prompt_multiplier=0.55,
        worker_result_multiplier=0.55,
        worker_review_multiplier=0.55,
        task_done_multiplier=0.65,
        allow_all=True,
    ),
    "standard": ExecutionProfile(
        name="standard",
        description="标准执行配置，使用默认上下文与截断预算。",
        prompt_modifier=(
            "# Standard Mode\n\n"
            "You are in standard execution mode. Balance speed, context gathering, and verification.\n"
            "- Gather enough context to avoid obvious mistakes.\n"
            "- Keep plans lightweight unless the task is multi-step.\n"
            "- Verify changes with targeted checks when practical."
        ),
        prompt_budget_tokens=16_000,
        allow_all=True,
    ),
    "deep": ExecutionProfile(
        name="deep",
        description="深度分析配置，放宽上下文与 worker 截断预算，强调验证与完整性。",
        prompt_modifier=(
            "# Deep Mode\n\n"
            "You are in deep execution mode. Prioritize correctness and complete context.\n"
            "- Verify assumptions before making changes.\n"
            "- Inspect adjacent code paths when behavior may cross module boundaries.\n"
            "- Use a structured plan for substantial work.\n"
            "- Run targeted validation and summarize residual risk."
        ),
        prompt_budget_tokens=32_000,
        input_char_multiplier=1.25,
        retry_char_multiplier=1.35,
        worker_prompt_multiplier=1.45,
        worker_result_multiplier=1.35,
        worker_review_multiplier=1.35,
        task_done_multiplier=1.25,
        allow_all=True,
    ),
    "orchestrated": ExecutionProfile(
        name="orchestrated",
        description="编排型配置，放宽团队/worker 上下文预算，偏向多步任务与委派协作。",
        prompt_modifier=(
            "# Orchestrated Mode\n\n"
            "You are in orchestrated execution mode. Prefer explicit coordination for multi-step work.\n"
            "- Break complex tasks into clear worker-sized handoffs when delegation helps.\n"
            "- Preserve task contracts, resources, and acceptance criteria in handoffs.\n"
            "- Track worker outputs, review evidence, and converge results before finalizing.\n"
            "- Avoid delegation for trivial tasks that are faster to complete directly."
        ),
        prompt_budget_tokens=40_000,
        input_char_multiplier=1.35,
        retry_char_multiplier=1.50,
        worker_prompt_multiplier=1.60,
        worker_result_multiplier=1.50,
        worker_review_multiplier=1.50,
        task_done_multiplier=1.35,
        allow_all=True,
    ),
    "bio-safe": ExecutionProfile(
        name="bio-safe",
        description="生物领域安全只读配置，禁止高副作用工具。",
        allowed_tags=("read_only", "web", "memory", "history", "planning", "interaction", "import"),
        blocked_tags=("shell", "domain_job", "write", "orchestration", "team"),
    ),
    "paper-review": ExecutionProfile(
        name="paper-review",
        description="文献审阅配置，仅开放检索、阅读、历史与记忆能力。",
        allowed_tags=("read_only", "web", "history", "memory", "interaction", "import"),
        blocked_tags=("shell", "domain_job", "write", "orchestration", "team"),
    ),
    "research-web": ExecutionProfile(
        name="research-web",
        description="研究检索配置，适合网页检索与本地只读分析。",
        allowed_tags=("read_only", "web", "history", "memory", "planning", "interaction", "import"),
        blocked_tags=("domain_job", "team"),
    ),
    "protein-design": ExecutionProfile(
        name="protein-design",
        description="蛋白设计配置，允许领域任务与工件工作流。",
        allow_all=True,
    ),
    "manager-only": ExecutionProfile(
        name="manager-only",
        description="Manager 编排配置，强调团队与任务调度。",
        blocked_tags=("domain_job",),
    ),
}


_PROFILE_ALIASES: dict[str, str] = {
    "quick": "fast",
    "lite": "fast",
    "light": "fast",
    "speed": "fast",
    "speedy": "fast",
    "normal": "standard",
    "default": "standard",
    "balanced": "standard",
    "balance": "standard",
    "safe": "standard",
    "thorough": "deep",
    "full": "deep",
    "heavy": "deep",
    "high": "deep",
    "orchestrate": "orchestrated",
    "orchestration": "orchestrated",
    "team": "orchestrated",
    "teamwork": "orchestrated",
    "manager": "orchestrated",
}


_BUDGET_MULTIPLIERS: dict[str, str] = {
    "max_input_chars": "input_char_multiplier",
    "emergency_message_chars": "retry_char_multiplier",
    "retry_total_chars": "retry_char_multiplier",
    "retry_per_message_chars": "retry_char_multiplier",
    "worker_execution_prompt_chars": "worker_prompt_multiplier",
    "worker_memory_prompt_chars": "worker_prompt_multiplier",
    "worker_result_chars": "worker_result_multiplier",
    "worker_review_task_chars": "worker_review_multiplier",
    "worker_review_result_chars": "worker_review_multiplier",
    "worker_retry_result_chars": "worker_result_multiplier",
    "worker_task_done_chars": "task_done_multiplier",
}


def _normalize_profile_key(profile: str | None) -> str:
    normalized = str(profile or "").strip().lower().replace("_", "-").replace(" ", "-")
    return "-".join(part for part in normalized.split("-") if part)


def normalize_execution_profile(profile: str | None) -> str:
    normalized = _normalize_profile_key(profile)
    if normalized in _PROFILES:
        return normalized
    return _PROFILE_ALIASES.get(normalized, "")


def is_known_execution_profile(profile: str | None) -> bool:
    return bool(normalize_execution_profile(profile))


def list_execution_profiles() -> list[str]:
    return list(_PROFILES.keys())


def get_execution_profile(profile: str | None) -> ExecutionProfile | None:
    normalized = normalize_execution_profile(profile)
    return _PROFILES.get(normalized) if normalized else None


def get_execution_profile_prompt_budget_tokens(profile: str | None, default: int = 16_000) -> int:
    profile_info = get_execution_profile(profile)
    if not profile_info:
        return max(1, int(default or 1))
    try:
        return max(1, int(profile_info.prompt_budget_tokens or default))
    except Exception:
        return max(1, int(default or 1))


def get_execution_profile_char_budget(
    profile: str | None,
    budget_name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    """Scale a character budget by the active execution profile.

    Existing env/provider values remain the baseline. Profiles only adjust that
    baseline, so custom deployments keep their configured limits while fast/deep
    modes still have a visible effect.
    """
    base = max(minimum, int(default or minimum))
    profile_info = get_execution_profile(profile)
    attr = _BUDGET_MULTIPLIERS.get(str(budget_name or ""))
    if not profile_info or not attr:
        value = base
    else:
        try:
            multiplier = float(getattr(profile_info, attr, 1.0) or 1.0)
        except Exception:
            multiplier = 1.0
        value = max(minimum, int(round(base * multiplier)))
    if maximum is not None:
        value = min(value, int(maximum))
    return value


def profile_allows_tool(
    profile: str | None,
    tool_id: str,
    capability: ToolCapability | None = None,
) -> bool:
    normalized = normalize_execution_profile(profile)
    if not normalized:
        return True
    profile_info = _PROFILES[normalized]
    if profile_info.allow_all:
        return True
    tool_name = str(tool_id or "").strip()
    if capability is None:
        from ..tools.capabilities import get_tool_capability

        capability = get_tool_capability(tool_name)
    if tool_name in profile_info.blocked_tools:
        return False
    if profile_info.blocked_tags and any(tag in capability.tags for tag in profile_info.blocked_tags):
        return False
    if profile_info.allowed_tags:
        return any(tag in capability.tags for tag in profile_info.allowed_tags)
    return True
