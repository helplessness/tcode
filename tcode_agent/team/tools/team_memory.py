"""Tools: team_memory_write + team_memory_read for Manager long-term memory."""

from typing import Any, Optional

from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..memory import TeamMemoryManager
from ..worker_memory import WorkerMemoryManager


def _resolve_memory_scope(context: ToolContext) -> tuple[str, str, str]:
    extra = context.extra or {}
    shared_memory_key = str(
        extra.get("shared_memory_key") or extra.get("worker_memory_key") or ""
    ).strip()
    if shared_memory_key:
        source = str(extra.get("shared_memory_source") or "").strip()
        if not source:
            source = f"worker:{context.agent}"
        return "worker", shared_memory_key, source
    return "team", context.session_id, "manager"


# ── Write ──────────────────────────────────────────────────────────────────

class TeamMemoryWriteParameters(ParameterSchema):
    content: str
    tags: Optional[str] = None  # comma-separated
    task_type: Optional[str] = None
    goal: Optional[str] = None
    constraints: Optional[str] = None
    decision: Optional[str] = None
    outcome: Optional[str] = None
    confidence: Optional[float] = None
    toolchain: Optional[str] = None
    failure_reason: Optional[str] = None


class TeamMemoryWriteTool(Tool):
    @property
    def id(self) -> str:
        return "team_memory_write"

    @property
    def description(self) -> str:
        return """将重要信息写入团队长期记忆（持久化存储，跨会话可见）。

参数：
- content: 要记忆的内容（决策、Worker 结论、项目背景等）
- tags: 可选，逗号分隔的标签（如 "worker_result,researcher"）
- 也支持结构化字段：task_type / goal / constraints / decision / outcome / confidence / toolchain / failure_reason

用途示例：
- 记录 Worker 完成任务的关键结论
- 保存项目关键决策
- 记录用户偏好和约束条件"""

    @property
    def parameters(self) -> type:
        return TeamMemoryWriteParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TeamMemoryWriteParameters(**args)
        workdir = (context.extra or {}).get("workdir", "") or ""
        scope_kind, scope_key, source = _resolve_memory_scope(context)
        tags = [t.strip() for t in (parsed.tags or "").split(",") if t.strip()]
        constraints = [t.strip() for t in (parsed.constraints or "").split(",") if t.strip()]
        toolchain = [t.strip() for t in (parsed.toolchain or "").split(",") if t.strip()]

        if scope_kind == "worker":
            entry = WorkerMemoryManager.add_structured(
                memory_key=scope_key,
                workdir=workdir,
                goal=parsed.goal or parsed.content.strip(),
                task_type=parsed.task_type or "",
                constraints=constraints,
                decision=parsed.decision or "",
                outcome=parsed.outcome or parsed.content.strip(),
                confidence=float(parsed.confidence or 0.0),
                toolchain=toolchain,
                failure_reason=parsed.failure_reason or "",
                tags=tags,
                source=source,
            )
        else:
            entry = TeamMemoryManager.add_structured(
                team_id=scope_key,
                workdir=workdir,
                goal=parsed.goal or parsed.content.strip(),
                task_type=parsed.task_type or "",
                constraints=constraints,
                decision=parsed.decision or "",
                outcome=parsed.outcome or parsed.content.strip(),
                confidence=float(parsed.confidence or 0.0),
                toolchain=toolchain,
                failure_reason=parsed.failure_reason or "",
                tags=tags,
                source=source,
            )
        return ToolResult(
            title="team_memory_write",
            output=f"✅ 已记忆（id={entry.id[:8]}）：{parsed.content[:120]}",
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())


# ── Read ───────────────────────────────────────────────────────────────────

class TeamMemoryReadParameters(ParameterSchema):
    query: Optional[str] = None
    limit: Optional[int] = 20
    task_type: Optional[str] = None
    goal: Optional[str] = None
    level: Optional[str] = "l1"
    filters: Optional[dict[str, Any]] = None


class TeamMemoryReadTool(Tool):
    @property
    def id(self) -> str:
        return "team_memory_read"

    @property
    def description(self) -> str:
        return """读取团队长期记忆。

参数：
- query: 可选搜索关键词，留空则返回最新记录
- task_type: 可选任务类型过滤
- goal: 可选目标文本，用于相似召回
- limit: 返回条数（默认 20）"""

    @property
    def parameters(self) -> type:
        return TeamMemoryReadParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = TeamMemoryReadParameters(**args)
        workdir = (context.extra or {}).get("workdir", "") or ""
        scope_kind, scope_key, _ = _resolve_memory_scope(context)
        manager = WorkerMemoryManager if scope_kind == "worker" else TeamMemoryManager

        if parsed.query or parsed.task_type or parsed.goal:
            items = manager.query_related(
                team_id=scope_key,
                workdir=workdir,
                task_type=parsed.task_type,
                goal=parsed.goal,
                query=parsed.query,
                limit=parsed.limit or 20,
                level=parsed.level or "l1",
                filters=parsed.filters,
            )
        else:
            items = manager.query(
                team_id=scope_key,
                workdir=workdir,
                query=parsed.query,
                limit=parsed.limit or 20,
                level=parsed.level or "l1",
                filters=parsed.filters,
            )
        if not items:
            return ToolResult(title="team_memory_read", output="（暂无团队记忆）")

        lines = []
        for e in items:
            tag_txt = f"  [tags: {', '.join(e.tags)}]" if e.tags else ""
            structured = []
            if e.task_type:
                structured.append(f"task_type={e.task_type}")
            if e.goal:
                structured.append(f"goal={e.goal[:80]}")
            if e.confidence:
                structured.append(f"confidence={e.confidence}")
            if e.toolchain:
                structured.append(f"toolchain={','.join(e.toolchain[:5])}")
            structured_txt = f"  [{'; '.join(structured)}]" if structured else ""
            lines.append(f"• [{e.source}] {e.content}{tag_txt}{structured_txt}")
        return ToolResult(
            title=f"team_memory_read ({len(items)} 条)",
            output="\n".join(lines),
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry
        ToolRegistry.register(cls())
