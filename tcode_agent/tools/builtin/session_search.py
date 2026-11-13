"""Session search tool backed by SQLite FTS."""

from __future__ import annotations

from typing import Any

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...session.manager import SessionManager


class SessionSearchParameters(ParameterSchema):
    query: str | None = None
    limit: int | None = 5
    session_id: str | None = None
    revision: int | None = None
    include_history: bool | None = False
    list_revisions: bool | None = False


class SessionSearchTool(Tool):
    @property
    def id(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return """Search persisted session history across past conversations.

Use this when:
- 你怀疑以前已经讨论过类似问题
- 需要找回某次任务里的路径、参数、失败原因或结论
- 需要浏览最近会话，而不是把这些信息写入长期 memory
- 需要查看某个 session 的历史 revision，或只搜索某个旧 revision
"""

    @property
    def parameters(self) -> type:
        return SessionSearchParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = SessionSearchParameters(**args)
        workdir = context.extra.get("workdir") if context.extra else None
        limit = max(1, min(int(parsed.limit or 5), 20))
        target_session_id = str(parsed.session_id or "").strip() or None
        effective_session_id = target_session_id or (str(context.session_id or "").strip() or None)

        if parsed.list_revisions:
            if not effective_session_id:
                return ToolResult(
                    title="Session revisions",
                    output="session_id is required to list revisions.",
                    error="session_id is required",
                )
            revisions = await SessionManager.revisions(
                effective_session_id,
                limit=limit,
                workdir=workdir,
            )
            if not revisions:
                return ToolResult(
                    title="Session revisions",
                    output="No revisions found.",
                    metadata={"count": 0, "items": []},
                )

            lines = []
            for idx, item in enumerate(revisions, 1):
                marker = " current" if bool(item.get("is_current")) else ""
                preview = str(item.get("last_message_preview") or "").strip()
                role = str(item.get("last_message_role") or "").strip()
                lines.append(
                    f"{idx}. rev={item.get('revision')}{marker} messages={item.get('message_count')} ({role}) {preview}"
                )
            return ToolResult(
                title="Session revisions",
                output="\n".join(lines),
                metadata={"count": len(revisions), "items": revisions},
            )

        if str(parsed.query or "").strip():
            results = await SessionManager.search(
                str(parsed.query),
                limit=limit,
                session_id=target_session_id,
                workdir=workdir,
                revision=parsed.revision,
                include_history=bool(parsed.include_history),
            )
            if not results:
                return ToolResult(
                    title="Session search",
                    output="No matching sessions found.",
                    metadata={"count": 0, "items": []},
                )

            lines: list[str] = []
            for idx, item in enumerate(results, 1):
                title = str(item.get("session_title") or item.get("session_id") or "")
                snippet = str(item.get("snippet") or "").strip()
                role = str(item.get("role") or "")
                revision = int(item.get("revision") or 0)
                current_flag = " current" if bool(item.get("is_current_revision")) else ""
                lines.append(f"{idx}. [{title}] rev={revision}{current_flag} ({role}) {snippet}")
            return ToolResult(
                title="Session search",
                output="\n".join(lines),
                metadata={"count": len(results), "items": results},
            )

        if parsed.revision is not None and effective_session_id:
            revisions = await SessionManager.revisions(
                effective_session_id,
                limit=max(limit, 20),
                workdir=workdir,
            )
            target = next(
                (item for item in revisions if int(item.get("revision") or 0) == int(parsed.revision)),
                None,
            )
            if target is None:
                return ToolResult(
                    title="Session revision",
                    output=f"Revision {parsed.revision} not found.",
                    metadata={"count": 0, "items": []},
                )
            marker = " current" if bool(target.get("is_current")) else ""
            preview = str(target.get("last_message_preview") or "").strip()
            role = str(target.get("last_message_role") or "").strip()
            return ToolResult(
                title="Session revision",
                output=(
                    f"rev={target.get('revision')}{marker} "
                    f"messages={target.get('message_count')} ({role}) {preview}"
                ),
                metadata={"count": 1, "items": [target]},
            )

        recent = await SessionManager.recent(limit=limit, workdir=workdir)
        if not recent:
            return ToolResult(
                title="Recent sessions",
                output="No recent sessions found.",
                metadata={"count": 0, "items": []},
            )

        lines = []
        for idx, item in enumerate(recent, 1):
            title = str(item.get("title") or item.get("id") or "")
            agent_name = str(item.get("agent_name") or "")
            preview = str(item.get("last_message_preview") or "").strip()
            lines.append(f"{idx}. [{title}] agent={agent_name} {preview}")
        return ToolResult(
            title="Recent sessions",
            output="\n".join(lines),
            metadata={"count": len(recent), "items": recent},
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
