from __future__ import annotations

"""Session management for agents"""

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from datetime import datetime

from ..presentation import normalize_content_blocks
from ..types import Message, ToolCall, ToolState, Usage
import os
from .store_sqlite import SessionSQLiteStore

ARCHIVED_AT_METADATA_KEY = "archived_at"
ARCHIVED_BY_USER_METADATA_KEY = "archived_by_user"
SESSION_MODE_METADATA_KEY = "mode"
RESEARCH_ID_METADATA_KEY = "research_id"
MODE_SOURCE_METADATA_KEY = "mode_source"
ARCHIVE_CAUSE_METADATA_KEY = "archive_cause"
ARCHIVE_LOCKED_METADATA_KEY = "archive_locked"
_DEFAULT_TRANSIENT_METADATA_KEYS = {
    "animation",
    "draft",
    "expanded",
    "loading",
    "selected_tab",
    "stream_state",
    "typing",
    "ui_state",
}


def _persistent_session_metadata(metadata: Any) -> dict[str, Any]:
    source = dict(metadata or {}) if isinstance(metadata, dict) else {}
    configured = {
        item.strip()
        for item in os.environ.get("TCODE_SESSION_TRANSIENT_METADATA_KEYS", "").split(",")
        if item.strip()
    }
    for key in _DEFAULT_TRANSIENT_METADATA_KEYS | configured:
        source.pop(key, None)
    return source
DEFAULT_TENANT_ID = "admin_tenant"
DEFAULT_USER_ID = "local-user"
DEFAULT_WORKSPACE_ID = "workspace-1"


def _append_research_session_event(session: "Session", event_type: str) -> None:
    """Best-effort Journal write; session lifecycle must never depend on it."""

    try:
        from ..research import ResearchEventInput, ResearchEventSink

        metadata = dict(getattr(session, "metadata", {}) or {})
        ResearchEventSink.append(
            ResearchEventInput(
                event_type=event_type,
                tenant_id=str(getattr(session, "tenant_id", "") or ""),
                user_id=str(getattr(session, "user_id", "") or ""),
                workspace_id=str(getattr(session, "workspace_id", "") or ""),
                session_id=str(getattr(session, "id", "") or ""),
                actor_type=(
                    "user"
                    if event_type in {"session.archived", "session.resumed"}
                    else "system"
                ),
                actor_id=(
                    str(getattr(session, "user_id", "") or "")
                    if event_type in {"session.archived", "session.resumed"}
                    else "session-manager"
                ),
                source_kind="session",
                source_id=str(getattr(session, "id", "") or ""),
                source_ref=f"session:{getattr(session, 'id', '')}",
                payload={
                    "title": str(getattr(session, "title", "") or ""),
                    "agent_name": str(getattr(session, "agent_name", "") or ""),
                    "status": str(getattr(session, "status", "") or ""),
                    "parent_id": str(getattr(session, "parent_id", "") or ""),
                    "archived_at": metadata.get(ARCHIVED_AT_METADATA_KEY),
                },
                privacy_state="private",
                stable_state_version=(
                    str(metadata.get(ARCHIVED_AT_METADATA_KEY) or "")
                    if event_type == "session.archived"
                    else event_type
                ),
            ),
            workdir=str(getattr(session, "workdir", "") or "") or None,
        )
        parent_id = str(getattr(session, "parent_id", "") or "").strip()
        raw_parent_context = metadata.get("research_parent_context") or {}
        parent_context = (
            dict(raw_parent_context) if isinstance(raw_parent_context, dict) else {}
        )
        if event_type == "session.created" and parent_id and parent_context:
            ResearchEventSink.append(
                ResearchEventInput(
                    event_type="agent.spawned",
                    tenant_id=str(getattr(session, "tenant_id", "") or ""),
                    user_id=str(getattr(session, "user_id", "") or ""),
                    workspace_id=str(getattr(session, "workspace_id", "") or ""),
                    session_id=parent_id,
                    turn_id=str(parent_context.get("turn_id") or ""),
                    trace_id=str(parent_context.get("trace_id") or ""),
                    actor_type="agent",
                    actor_id=str(parent_context.get("agent_name") or "parent-agent"),
                    source_kind="subagent",
                    source_id=str(getattr(session, "id", "") or ""),
                    source_ref=(
                        f"session:{parent_id}/subagent:{getattr(session, 'id', '')}"
                    ),
                    payload={
                        "subagent_session_id": str(
                            getattr(session, "id", "") or ""
                        ),
                        "subagent_name": str(
                            getattr(session, "agent_name", "") or ""
                        ),
                        "parent_session_id": parent_id,
                        "parent_tool_call_id": str(
                            parent_context.get("tool_call_id") or ""
                        ),
                    },
                    privacy_state="private",
                    stable_state_version=str(getattr(session, "id", "") or ""),
                ),
                workdir=str(getattr(session, "workdir", "") or "") or None,
            )
    except Exception:
        pass


def _normalized_owner_id(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = default
    return text[:128]


def _default_tenant_id() -> str:
    return _normalized_owner_id(os.environ.get("TCODE_DEFAULT_TENANT_ID"), DEFAULT_TENANT_ID)


def _default_user_id() -> str:
    return _normalized_owner_id(os.environ.get("TCODE_DEFAULT_USER_ID"), DEFAULT_USER_ID)


def _default_workspace_id() -> str:
    return _normalized_owner_id(
        os.environ.get("TCODE_DEFAULT_WORKSPACE_ID"),
        DEFAULT_WORKSPACE_ID,
    )


def _archived_at_from_metadata(metadata: Any) -> Optional[float]:
    if not isinstance(metadata, dict):
        return None
    raw_value = metadata.get(ARCHIVED_AT_METADATA_KEY)
    try:
        timestamp = float(raw_value or 0)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp > 0 else None


@dataclass
class Session:
    """Represents a conversation session"""

    id: str
    parent_id: Optional[str] = None
    title: str = ""
    agent_name: str = "general"
    status: str = "active"  # "active", "completed", "idle"
    tenant_id: str = field(default_factory=_default_tenant_id)
    user_id: str = field(default_factory=_default_user_id)
    workspace_id: str = field(default_factory=_default_workspace_id)
    workdir: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    last_active_at: float = field(default_factory=lambda: datetime.now().timestamp())
    messages: list = field(default_factory=list)
    # 完整前端会话历史。为空表示尚未发生上下文压缩，直接使用 messages。
    # messages 在压缩后只承担模型继续执行所需的“摘要 + 最近消息”。
    display_messages: list = field(default_factory=list)

    # Compaction metadata
    compaction_count: int = 0
    last_compacted_at: Optional[float] = None
    total_tokens_saved: int = 0
    current_revision: int = 0

    def add_message(self, role: str, content: str, **kwargs):
        """Add a message to the session"""
        from ..types import Message

        msg = Message(role=role, content=content, id=str(uuid.uuid4()), **kwargs)
        self.messages.append(msg)
        self.last_active_at = datetime.now().timestamp()
        return msg

    def get_messages(self) -> list:
        """Get all messages"""
        return self.messages

    def clear_messages(self):
        """Clear messages but keep session"""
        self.messages = []
        self.display_messages = []



    @classmethod
    async def restore_context(cls, session_id: str) -> str:
        """Build a recovery summary from the last session's state.

        Extracts: what was being worked on, completed steps, pending steps,
        unresolved LOCAL jobs, workspace path. Returns a string suitable
        for injection into a new session's system prompt.
        """
        session = await cls.get(session_id)
        if session is None:
            return ""

        lines = ["# Session Recovery Context", ""]

        messages = list(getattr(session, "messages", []) or [])
        metadata = dict(getattr(session, "metadata", {}) or {})

        # Extract last user message
        from ..message_context import is_real_user_message

        for msg in reversed(messages):
            if is_real_user_message(msg):
                content = str(getattr(msg, "content", "") or "").strip()
                if content:
                    lines.append(f"Last request: {content[:200]}")
                    break

        # Extract last assistant actions
        last_tools = []
        for msg in reversed(messages):
            for tc in (getattr(msg, "tool_calls", []) or []):
                name = str(getattr(tc, "tool_name", "") or "").strip()
                if name and name not in last_tools:
                    last_tools.append(name)
                    if len(last_tools) >= 8:
                        break
            if last_tools:
                break
        if last_tools:
            lines.append(f"Last tools used: {', '.join(last_tools)}")

        # Plan status
        plan = metadata.get("structured_plan")
        if isinstance(plan, dict):
            goal = str(plan.get("goal", "") or "").strip()
            if goal:
                lines.append(f"Plan goal: {goal[:200]}")
            steps = plan.get("steps", []) or []
            completed = [s for s in steps if s.get("status") == "completed"]
            pending = [s for s in steps if s.get("status") in ("pending", "in_progress")]
            if completed:
                lines.append(f"Completed steps ({len(completed)}):")
                for s in completed[-5:]:
                    lines.append(f"  ✅ {s.get('content', '')[:100]}")
            if pending:
                lines.append(f"Pending steps ({len(pending)}):")
                for s in pending[:5]:
                    lines.append(f"  ⬜ {s.get('content', '')[:100]}")

        # Workspace
        workdir = getattr(session, "workdir", "") or ""
        if workdir:
            lines.append(f"Workspace: {workdir}")

        # Active agent
        agent_name = getattr(session, "agent_name", "") or ""
        if agent_name:
            lines.append(f"Previous agent: {agent_name}")

        lines.append("")
        lines.append("Resume from this state. Do not repeat already-completed steps.")
        return "\n".join(lines)


class SessionManager:
    """Manages agent sessions"""

    _sessions: dict[str, Session] = {}
    _lock = None
    _loaded: bool = False

    @classmethod
    def _storage_path(cls) -> str:
        base = os.environ.get("TCODE_WORKDIR", os.getcwd())
        return os.path.join(base, ".tcode", "sessions.json")

    @classmethod
    def _sqlite_path(cls) -> str:
        return SessionSQLiteStore.db_path().as_posix()

    @classmethod
    def _ensure_storage(cls):
        path = cls._storage_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    @classmethod
    def _message_to_dict(cls, msg: Message) -> dict[str, Any]:
        return {
            "role": msg.role,
            "content": msg.content,
            "id": msg.id,
            "created_at": msg.created_at,
            "parent_id": msg.parent_id,
            "error": msg.error,
            "finish": msg.finish,
            "cost": msg.cost,
            "summary": msg.summary,
            "compacted_at": msg.compacted_at,
            "process_notes": list(getattr(msg, "process_notes", []) or []),
            "presentation": getattr(msg, "presentation", None),
            "content_blocks": normalize_content_blocks(
                getattr(msg, "content_blocks", [])
            ),
            "research_grounding": dict(
                getattr(msg, "research_grounding", {}) or {}
            ),
            "verification": dict(getattr(msg, "verification", {}) or {}),
            "input_images": [
                dict(item)
                for item in (getattr(msg, "input_images", []) or [])
                if isinstance(item, dict)
            ],
            "metadata": dict(getattr(msg, "metadata", {}) or {}),
            "usage": {
                "prompt_tokens": msg.usage.prompt_tokens,
                "completion_tokens": msg.usage.completion_tokens,
                "total_tokens": msg.usage.total_tokens,
            }
            if msg.usage
            else None,
            "tool_calls": [
                {
                    "tool_name": tc.tool_name,
                    "call_id": tc.call_id,
                    "arguments": tc.arguments,
                    "state": tc.state.value,
                    "output": tc.output,
                    "error": tc.error,
                    "metadata": tc.metadata,
                    "start_time": tc.start_time,
                    "end_time": tc.end_time,
                    "compacted_at": tc.compacted_at,
                }
                for tc in (msg.tool_calls or [])
            ],
        }

    @classmethod
    def _message_from_dict(cls, data: dict[str, Any]) -> Message:
        msg = Message(
            role=str(data.get("role", "assistant")),
            content=str(data.get("content", "")),
            id=str(data.get("id", "")),
            created_at=float(data.get("created_at") or 0),
            parent_id=data.get("parent_id"),
            error=data.get("error"),
            finish=data.get("finish"),
            cost=float(data.get("cost", 0) or 0),
            process_notes=[
                dict(item)
                for item in (data.get("process_notes", []) or [])
                if isinstance(item, dict)
            ],
            presentation=(
                str(data.get("presentation", "") or "").strip() or None
            ),
            content_blocks=normalize_content_blocks(data.get("content_blocks", [])),
            research_grounding=(
                dict(data.get("research_grounding") or {})
                if isinstance(data.get("research_grounding"), dict)
                else {}
            ),
            verification=(
                dict(data.get("verification") or {})
                if isinstance(data.get("verification"), dict)
                else {}
            ),
            input_images=[
                dict(item)
                for item in (data.get("input_images", []) or [])
                if isinstance(item, dict)
            ],
            metadata=(
                dict(data.get("metadata") or {})
                if isinstance(data.get("metadata"), dict)
                else {}
            ),
            summary=bool(data.get("summary", False)),
            compacted_at=data.get("compacted_at"),
            usage=Usage(**data["usage"]) if isinstance(data.get("usage"), dict) else None,
        )

        tool_calls = []
        for tc in data.get("tool_calls", []) or []:
            state_value = str(tc.get("state", "pending"))
            try:
                state = ToolState(state_value)
            except ValueError:
                state = ToolState.PENDING
            tool_calls.append(
                ToolCall(
                    tool_name=str(tc.get("tool_name", "")),
                    call_id=str(tc.get("call_id", "")),
                    arguments=tc.get("arguments", {}) or {},
                    state=state,
                    output=tc.get("output"),
                    error=tc.get("error"),
                    metadata=tc.get("metadata", {}) or {},
                    start_time=tc.get("start_time"),
                    end_time=tc.get("end_time"),
                    compacted_at=tc.get("compacted_at"),
                )
            )
        msg.tool_calls = tool_calls
        return msg

    @classmethod
    def _serialize_session(cls, session: Session) -> dict[str, Any]:
        from .notification_reads import apply_reads
        metadata = _persistent_session_metadata(session.metadata)
        if isinstance(metadata.get('async_completion_events'), list):
            metadata['async_completion_events'] = apply_reads(session.id, metadata['async_completion_events'])
        return {
            "id": session.id,
            "parent_id": session.parent_id,
            "title": session.title,
            "agent_name": session.agent_name,
            "status": session.status,
            "tenant_id": session.tenant_id,
            "user_id": session.user_id,
            "workspace_id": session.workspace_id,
            "workdir": session.workdir,
            "metadata": metadata,
            "created_at": session.created_at,
            "last_active_at": session.last_active_at,
            "compaction_count": session.compaction_count,
            "last_compacted_at": session.last_compacted_at,
            "total_tokens_saved": session.total_tokens_saved,
            "current_revision": session.current_revision,
            "messages": [cls._message_to_dict(m) for m in session.messages],
            "display_messages": [
                cls._message_to_dict(m)
                for m in list(getattr(session, "display_messages", []) or [])
            ],
        }

    @staticmethod
    def _is_compaction_context_message(message: Message) -> bool:
        content = str(getattr(message, "content", "") or "")
        return bool(getattr(message, "summary", False)) or content.startswith(
            "[COMPACTION MARKER"
        )

    @classmethod
    def visible_messages(
        cls,
        session: Session,
        current_messages: Optional[Iterable[Message]] = None,
    ) -> list[Message]:
        """Return the complete UI transcript without internal compaction messages."""

        preserved = list(getattr(session, "display_messages", []) or [])
        current = list(
            current_messages
            if current_messages is not None
            else getattr(session, "messages", []) or []
        )
        if not preserved and any(
            cls._is_compaction_context_message(message) for message in current
        ):
            try:
                from .event_archive import EventArchiveStore

                archived = EventArchiveStore.original_session_messages(
                    session.id,
                    owner={
                        "tenant_id": session.tenant_id,
                        "user_id": session.user_id,
                        "workspace_id": session.workspace_id,
                    },
                )
                preserved = [cls._message_from_dict(item) for item in archived]
                if preserved:
                    session.display_messages = list(preserved)
            except Exception:
                preserved = []
        if not preserved:
            return current

        merged = list(preserved)
        known_ids = {str(getattr(message, "id", "") or "") for message in merged}
        for message in current:
            if cls._is_compaction_context_message(message):
                continue
            message_id = str(getattr(message, "id", "") or "")
            if message_id and message_id in known_ids:
                continue
            merged.append(message)
            if message_id:
                known_ids.add(message_id)
        return merged

    @classmethod
    def _deserialize_session(cls, data: dict[str, Any]) -> Session:
        session = Session(
            id=str(data.get("id", str(uuid.uuid4()))),
            parent_id=data.get("parent_id"),
            title=str(data.get("title", "")),
            agent_name=str(data.get("agent_name", "general")),
            status=str(data.get("status", "active")),
            tenant_id=_normalized_owner_id(data.get("tenant_id"), _default_tenant_id()),
            user_id=_normalized_owner_id(data.get("user_id"), _default_user_id()),
            workspace_id=_normalized_owner_id(
                data.get("workspace_id"),
                _default_workspace_id(),
            ),
            workdir=data.get("workdir"),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", datetime.now().timestamp())),
            last_active_at=float(
                data.get("last_active_at", datetime.now().timestamp())
            ),
            messages=[
                cls._message_from_dict(item)
                for item in (data.get("messages", []) or [])
                if isinstance(item, dict)
            ],
            display_messages=[
                cls._message_from_dict(item)
                for item in (data.get("display_messages", []) or [])
                if isinstance(item, dict)
            ],
            compaction_count=int(data.get("compaction_count", 0) or 0),
            last_compacted_at=data.get("last_compacted_at"),
            total_tokens_saved=int(data.get("total_tokens_saved", 0) or 0),
            current_revision=int(data.get("current_revision", 0) or 0),
        )
        return session

    @classmethod
    def _matches_owner(
        cls,
        session: Optional[Session],
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> bool:
        if session is None:
            return False
        if tenant_id is not None and str(tenant_id).strip():
            if str(getattr(session, "tenant_id", "") or "").strip() != str(tenant_id).strip():
                return False
        if user_id is not None and str(user_id).strip():
            if str(getattr(session, "user_id", "") or "").strip() != str(user_id).strip():
                return False
        if workspace_id is not None and str(workspace_id).strip():
            if (
                str(getattr(session, "workspace_id", "") or "").strip()
                != str(workspace_id).strip()
            ):
                return False
        return True

    @classmethod
    def _load_from_disk(cls):
        """从 SQLite Projection 加载会话，并用 Canonical Event 补种或恢复。

        已存在的 SQLite 行在首次加载时写入一个幂等 Snapshot 事件；SQLite 为空
        时则从事件日志重放最近 Snapshot。整个过程不调用模型或外部服务。
        """

        if cls._loaded:
            return

        SessionSQLiteStore.ensure_initialized()
        path = cls._storage_path()
        if SessionSQLiteStore.is_empty() and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
            if isinstance(raw, dict) and raw:
                SessionSQLiteStore.import_legacy_sessions(raw)

        raw_sessions = SessionSQLiteStore.load_all()
        from .events import SessionEventStore

        cls._reconcile_legacy_delete_tombstones(raw_sessions)

        # Event Log 是权威来源：无论 SQLite 是否为空，都以可重放投影覆盖查询行。
        for session_id in SessionEventStore.session_ids():
            projection = SessionEventStore.project(session_id)
            if bool(projection.get("deleted")):
                raw_sessions.pop(session_id, None)
                SessionSQLiteStore.delete(session_id)
                continue
            snapshot = projection.get("session")
            if isinstance(snapshot, dict) and snapshot:
                raw_sessions[session_id] = dict(snapshot)
                SessionSQLiteStore.upsert(dict(snapshot))
        cls._sessions = {}
        for session_id, data in raw_sessions.items():
            if isinstance(data, dict):
                session = cls._deserialize_session(data)
                cls._sessions[session_id] = session
                snapshot = cls._serialize_session(session)
                SessionEventStore.record_snapshot(
                    session.id,
                    snapshot=snapshot,
                    owner={
                        "tenant_id": session.tenant_id,
                        "user_id": session.user_id,
                        "workspace_id": session.workspace_id,
                    },
                )
        cls._loaded = True

    @classmethod
    def _reconcile_legacy_delete_tombstones(
        cls,
        raw_sessions: dict[str, dict[str, Any]],
    ) -> list[str]:
        """把旧版本误写到执行工作区的删除墓碑迁回权威存储。

        Session 查询投影和权威事件都位于服务根目录；旧删除实现却把
        ``session.deleted`` 和 SQLite DELETE 定向到了 Session 的执行工作区。
        该错误会让 Session 暂时从内存消失，但在服务重启时从根快照复活。

        这里只检查根 SQLite 中仍存在 Session 对应的精确事件文件，并要求
        墓碑 Owner 与根快照 Owner 完全一致，避免扫描或误删其他工作区数据。
        """

        from .events import SessionEventStore

        reconciled: list[str] = []
        for session_id, data in list(raw_sessions.items()):
            if not isinstance(data, dict):
                continue
            execution_workdir = str(data.get("workdir") or "").strip()
            if not execution_workdir:
                continue
            try:
                canonical_path = SessionEventStore.path_for(session_id).resolve(
                    strict=False
                )
                legacy_path = SessionEventStore.path_for(
                    session_id,
                    workdir=execution_workdir,
                    create=False,
                ).resolve(strict=False)
                if legacy_path == canonical_path or not legacy_path.is_file():
                    continue
                legacy_events = SessionEventStore.list(
                    session_id,
                    workdir=execution_workdir,
                )
                latest_session_event = next(
                    (
                        event
                        for event in reversed(legacy_events)
                        if str(event.get("event_type") or "")
                        in {"session.snapshot", "session.patch", "session.deleted"}
                    ),
                    None,
                )
                if not isinstance(latest_session_event, dict) or str(
                    latest_session_event.get("event_type") or ""
                ) != "session.deleted":
                    continue
                owner = {
                    "tenant_id": _normalized_owner_id(
                        data.get("tenant_id"), _default_tenant_id()
                    ),
                    "user_id": _normalized_owner_id(
                        data.get("user_id"), _default_user_id()
                    ),
                    "workspace_id": _normalized_owner_id(
                        data.get("workspace_id"), _default_workspace_id()
                    ),
                }
                legacy_owner = dict(latest_session_event.get("owner") or {})
                if any(
                    str(legacy_owner.get(key) or "").strip() != value
                    for key, value in owner.items()
                ):
                    continue
                SessionEventStore.append(
                    session_id=session_id,
                    event_type="session.deleted",
                    payload={"source": "legacy_misdirected_tombstone"},
                    owner=owner,
                    idempotency_key=(
                        f"session.deleted:{session_id}:legacy-after:"
                        f"{int(SessionEventStore.project(session_id).get('last_sequence', 0) or 0)}"
                    ),
                )
                SessionSQLiteStore.delete(
                    session_id,
                    tenant_id=owner["tenant_id"],
                    user_id=owner["user_id"],
                    workspace_id=owner["workspace_id"],
                )
                raw_sessions.pop(session_id, None)
                reconciled.append(session_id)
            except Exception:
                # 单个旧事件文件损坏不能阻止服务加载其他 Session。原始记录仍
                # 保留在磁盘，可在修复完整性后再次自动迁移。
                continue
        return reconciled

    @classmethod
    def _save_to_disk(
        cls,
        session_ids: Optional[list[str]] = None,
        *,
        related_events_by_session: Optional[
            dict[str, list[dict[str, Any]]]
        ] = None,
    ):
        """先追加 Canonical Snapshot，再原子更新 SQLite 查询投影。

        若 SQLite 写入失败，事件仍可用于恢复；若相同状态重试，内容摘要幂等键
        会阻止重复事件。事件写入失败则停止本次投影更新，避免快照领先权威来源。
        """

        from .events import SessionEventStore

        SessionSQLiteStore.ensure_initialized()
        target_ids = session_ids or list(cls._sessions.keys())
        for session_id in target_ids:
            session = cls._sessions.get(session_id)
            if session is None:
                continue
            snapshot = cls._serialize_session(session)
            SessionEventStore.record_snapshot(
                session.id,
                snapshot=snapshot,
                owner={
                    "tenant_id": session.tenant_id,
                    "user_id": session.user_id,
                    "workspace_id": session.workspace_id,
                },
                related_events=list(
                    (related_events_by_session or {}).get(session.id) or []
                ),
            )
            SessionSQLiteStore.upsert(snapshot)
            persisted = SessionSQLiteStore.get(session_id)
            if persisted:
                session.current_revision = int(persisted.get("current_revision", 0) or 0)

    @classmethod
    async def _save_to_disk_async(
        cls,
        session_ids: Optional[list[str]] = None,
        *,
        related_events_by_session: Optional[
            dict[str, list[dict[str, Any]]]
        ] = None,
    ) -> None:
        """在线程中保存权威快照和 SQLite 投影，避免阻塞 Web 事件循环。

        调用方仍须持有 SessionManager 的异步锁，因此同一进程内的快照顺序、
        消息修订号和失败传播语义与同步实现一致。这里只迁移磁盘和 FTS 工作，
        不把写入改成最终一致，也不会在返回前提前释放 Session 锁。

        Args:
            session_ids: 本次需要保存的 Session 标识；为空时保存全部 Session。
            related_events_by_session: 与各 Session 快照原子关联的规范事件。

        Returns:
            无返回值；底层写入异常会原样传播给调用方。
        """

        save = asyncio.create_task(asyncio.to_thread(
            cls._save_to_disk,
            session_ids,
            related_events_by_session=related_events_by_session,
        ))
        try:
            await asyncio.shield(save)
        except asyncio.CancelledError as cancelled:
            # Thread cancellation cannot stop disk I/O. Keep the caller's session
            # guard until the entire transaction settles, even on repeated cancel.
            while not save.done():
                try:
                    await asyncio.shield(save)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                save.result()
            except Exception as error:
                raise cancelled from error
            raise

    @classmethod
    async def recover_from_events(
        cls,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        """按 Owner 约束从 Canonical Event 重建一个 SQLite Session Projection。"""

        from .events import SessionEventStore

        projection = SessionEventStore.project(session_id)
        snapshot = projection.get("session")
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        session = cls._deserialize_session(snapshot)
        if not cls._matches_owner(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            return None
        async with cls._get_lock():
            cls._sessions[session.id] = session
            SessionSQLiteStore.upsert(cls._serialize_session(session))
            persisted = SessionSQLiteStore.get(session.id)
            if persisted:
                session.current_revision = int(persisted.get("current_revision", 0) or 0)
        return session

    @classmethod
    def _get_lock(cls):
        """Get or create lock"""
        import asyncio

        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def archived_at(cls, session: Optional[Session]) -> Optional[float]:
        if session is None:
            return None
        return _archived_at_from_metadata(getattr(session, "metadata", {}) or {})

    @classmethod
    def is_archived_session(cls, session: Optional[Session]) -> bool:
        return cls.archived_at(session) is not None

    @classmethod
    async def create(
        cls,
        session_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        title: str = "",
        agent_name: str = "general",
        workdir: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Session:
        """Create a new session"""
        async with cls._get_lock():
            cls._load_from_disk()
            session_id = session_id or str(uuid.uuid4())
            resolved_tenant_id = _normalized_owner_id(tenant_id, _default_tenant_id())
            resolved_user_id = _normalized_owner_id(user_id, _default_user_id())
            resolved_workspace_id = _normalized_owner_id(
                workspace_id,
                _default_workspace_id(),
            )
            existing = cls._sessions.get(session_id)
            if existing is not None and not cls._matches_owner(
                existing,
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
                workspace_id=resolved_workspace_id,
            ):
                raise ValueError("session id already belongs to another owner")
            session_metadata = dict(metadata or {})
            parent = cls._sessions.get(str(parent_id or "")) if parent_id else None
            parent_metadata = dict(getattr(parent, "metadata", {}) or {})
            for key in (
                "config_snapshot_id",
                "config_snapshot_workdir",
                "config_fingerprint",
                "config_policy_fingerprint",
                "config_summary",
                SESSION_MODE_METADATA_KEY,
                RESEARCH_ID_METADATA_KEY,
                MODE_SOURCE_METADATA_KEY,
            ):
                if key in parent_metadata:
                    session_metadata.setdefault(key, parent_metadata[key])
            if not str(session_metadata.get("config_snapshot_id") or "").strip():
                try:
                    from ..config.task_snapshot import TaskConfigSnapshotStore

                    snapshot = TaskConfigSnapshotStore.for_task(
                        str(parent_id or session_id),
                        str(workdir or os.getcwd()),
                    )
                    session_metadata["config_snapshot_id"] = snapshot.snapshot_id
                    session_metadata["config_snapshot_workdir"] = snapshot.workdir
                    session_metadata["config_fingerprint"] = snapshot.config_fingerprint
                    session_metadata["config_policy_fingerprint"] = (
                        snapshot.policy_fingerprint
                    )
                    session_metadata["config_summary"] = dict(snapshot.summary)
                except Exception:
                    pass
            session_metadata.setdefault("tenant_id", resolved_tenant_id)
            session_metadata.setdefault("user_id", resolved_user_id)
            session_metadata.setdefault("workspace_id", resolved_workspace_id)
            session_metadata.setdefault(SESSION_MODE_METADATA_KEY, "general")
            session_metadata.setdefault(MODE_SOURCE_METADATA_KEY, "default")
            session = Session(
                id=session_id,
                parent_id=parent_id,
                title=title,
                agent_name=agent_name,
                tenant_id=resolved_tenant_id,
                user_id=resolved_user_id,
                workspace_id=resolved_workspace_id,
                workdir=workdir,
                metadata=session_metadata,
            )
            cls._sessions[session_id] = session
            await cls._save_to_disk_async([session_id])
            await asyncio.to_thread(
                _append_research_session_event,
                session,
                "session.created",
            )
            return session

    @classmethod
    async def bind_research_project(
        cls,
        session_id: str,
        *,
        research_id: str,
        mode_source: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        """Bind a Session to the Workspace's confirmed Research Project."""

        normalized_research_id = str(research_id or "").strip()[:160]
        if not normalized_research_id:
            raise ValueError("research_id is required")

        def apply(session: Session) -> Session:
            metadata = dict(getattr(session, "metadata", {}) or {})
            existing = str(metadata.get(RESEARCH_ID_METADATA_KEY) or "").strip()
            if existing and existing != normalized_research_id:
                raise ValueError("session already belongs to another research project")
            metadata[SESSION_MODE_METADATA_KEY] = "research"
            metadata[RESEARCH_ID_METADATA_KEY] = normalized_research_id
            metadata[MODE_SOURCE_METADATA_KEY] = (
                str(mode_source or "user_confirmed").strip()[:80]
                or "user_confirmed"
            )
            session.metadata = metadata
            return session

        return await cls.mutate(
            session_id,
            apply,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    @classmethod
    async def unbind_research_project(
        cls,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        """Return an unlocked Research Session to general mode."""

        def apply(session: Session) -> Session:
            metadata = dict(getattr(session, "metadata", {}) or {})
            if bool(metadata.get(ARCHIVE_LOCKED_METADATA_KEY)):
                raise ValueError("课题归档锁定的研究会话不能单独转换")
            previous_research_id = str(metadata.pop(RESEARCH_ID_METADATA_KEY, "") or "").strip()
            metadata[SESSION_MODE_METADATA_KEY] = "general"
            metadata[MODE_SOURCE_METADATA_KEY] = "user_unbound"
            if previous_research_id:
                metadata["research_unbound_from"] = previous_research_id
                metadata["research_unbound_at"] = datetime.now().timestamp()
            session.metadata = metadata
            return session

        return await cls.mutate(
            session_id,
            apply,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    @classmethod
    async def get(
        cls,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        """Get a session by ID"""
        cls._load_from_disk()
        session = cls._sessions.get(session_id)
        if not cls._matches_owner(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            return None
        return session

    @classmethod
    async def get_or_create(cls, session_id: str, **kwargs) -> Session:
        """Get existing session or create new one"""
        cls._load_from_disk()
        session = cls._sessions.get(session_id)
        owner_filter = {
            key: kwargs.get(key)
            for key in ("tenant_id", "user_id", "workspace_id")
            if kwargs.get(key) is not None
        }
        if session and owner_filter and not cls._matches_owner(session, **owner_filter):
            session = None
        if not session:
            session = await cls.create(session_id=session_id, **kwargs)
        return session

    @classmethod
    async def update(
        cls,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        related_events: Optional[list[dict[str, Any]]] = None,
        **kwargs,
    ):
        """更新 Session Projection，并在消息变化时保存同工作区恢复点。

        Owner 过滤失败时不产生写入；消息、元数据和 Checkpoint 都在同一 Session
        锁内按顺序提交，Canonical Snapshot 由 ``_save_to_disk`` 统一追加。
        """
        async with cls._get_lock():
            cls._load_from_disk()
            if session_id in cls._sessions and cls._matches_owner(
                cls._sessions.get(session_id),
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            ):
                if "metadata" in kwargs and isinstance(kwargs.get("metadata"), dict):
                    metadata = dict(kwargs.get("metadata") or {})
                    imports = dict((cls._sessions[session_id].metadata or {}).get("side_chat_imports") or {})
                    imports.update(metadata.get("side_chat_imports") or {})
                    if imports:
                        metadata["side_chat_imports"] = imports
                    metadata.setdefault("tenant_id", cls._sessions[session_id].tenant_id)
                    metadata.setdefault("user_id", cls._sessions[session_id].user_id)
                    metadata.setdefault(
                        "workspace_id",
                        cls._sessions[session_id].workspace_id,
                    )
                    kwargs["metadata"] = metadata
                if "messages" in kwargs:
                    incoming = list(kwargs["messages"])
                    session = cls._sessions[session_id]
                    has_compaction_context = any(
                        cls._is_compaction_context_message(message)
                        for message in incoming
                    )
                    if incoming and has_compaction_context:
                        if not session.display_messages:
                            session.display_messages = list(session.messages)
                        session.display_messages = cls.visible_messages(
                            session,
                            current_messages=incoming,
                        )
                    elif session.display_messages:
                        # Rewind/clear/full-history replacement intentionally
                        # starts a fresh uncompressed transcript projection.
                        session.display_messages = []
                    known = {getattr(m, "id", "") for m in incoming}
                    if incoming:
                        incoming.extend(m for m in cls._sessions[session_id].messages if getattr(m, "metadata", {}).get("event_type") == "result_selection" and m.id not in known)
                    # A running Agent can still be persisting its pre-injection
                    # snapshot. Pending transfers survive until its next safe point.
                    from ..message_context import side_chat_import_message
                    for record in (cls._sessions[session_id].metadata.get("side_chat_imports") or {}).values():
                        if record.get("pending") and record["id"] not in known:
                            incoming.append(side_chat_import_message(record))
                            known.add(record["id"])
                    kwargs["messages"] = incoming
                for key, value in kwargs.items():
                    if hasattr(cls._sessions[session_id], key):
                        setattr(cls._sessions[session_id], key, value)
                await cls._save_to_disk_async(
                    [session_id],
                    related_events_by_session={
                        session_id: list(related_events or [])
                    },
                )
                
                # Save only meaningful execution milestones. The transcript is
                # already durable; duplicating every persistence heartbeat as a
                # checkpoint creates noise without improving recovery.
                if "messages" in kwargs and kwargs.get("messages"):
                    try:
                        from ..checkpoint.milestone import maybe_save_session_milestone

                        await asyncio.to_thread(
                            maybe_save_session_milestone,
                            cls._sessions[session_id],
                        )
                    except Exception:
                        pass

    @classmethod
    async def mutate(
        cls,
        session_id: str,
        mutator: Callable[[Session], Any],
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Any:
        """Atomically mutate one persisted session and return the callback result.

        Background result delivery must not implement read/modify/write with separate
        ``get`` and ``update`` calls. A foreground chat turn can commit between those
        calls and otherwise lose completion metadata or messages.
        """

        async with cls._get_lock():
            cls._load_from_disk()
            session = cls._sessions.get(session_id)
            if not cls._matches_owner(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            ):
                return None

            before = copy.deepcopy(session)
            try:
                result = mutator(session)
                if isinstance(getattr(session, "metadata", None), dict):
                    session.metadata.setdefault("tenant_id", session.tenant_id)
                    session.metadata.setdefault("user_id", session.user_id)
                    session.metadata.setdefault("workspace_id", session.workspace_id)
                await cls._save_to_disk_async([session_id])
            except BaseException:
                # Failed writes/cancellation must not leave phantom success in
                # the shared in-memory projection seen by later requests.
                session.__dict__.clear()
                session.__dict__.update(before.__dict__)
                raise
            try:
                from ..checkpoint.milestone import maybe_save_session_milestone

                await asyncio.to_thread(maybe_save_session_milestone, session)
            except Exception:
                pass
            return result

    @classmethod
    async def delete(
        cls,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ):
        """按 Owner 删除 Session，在删除投影前清理其私有记忆并写 Tombstone。"""
        async with cls._get_lock():
            cls._load_from_disk()
            session = cls._sessions.get(session_id)
            if not cls._matches_owner(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            ):
                return
            if session is None:
                return
            resolved_session = session

        resolved_workdir = str(getattr(resolved_session, "workdir", "") or os.getcwd())
        owner = {
            "tenant_id": resolved_session.tenant_id,
            "user_id": resolved_session.user_id,
            "workspace_id": resolved_session.workspace_id,
        }
        payload = {
            "__workspace_path": os.path.realpath(os.path.abspath(resolved_workdir)),
            "session_id": resolved_session.id,
            "agent": resolved_session.agent_name,
            "workdir": resolved_workdir,
            "owner": owner,
        }
        cleanup_report: dict[str, Any] = {}

        try:
            from ..context.compression.contracts import Owner, Policy
            from ..context.compression.store import CapsuleStore
            capsule_owner = Owner(**owner, session_id=resolved_session.id)
            await asyncio.to_thread(CapsuleStore(resolved_workdir, capsule_owner, Policy.from_env(), create=False).purge)
        except Exception as exc:
            cleanup_report["context_capsules_error"] = type(exc).__name__

        try:
            from ..server.plugin_warmup import trigger_plugin_event

            async def emit_lifecycle(
                event: str,
                data: dict[str, Any],
                *,
                mode: str = "serial",
            ) -> None:
                try:
                    await trigger_plugin_event(
                        resolved_workdir,
                        event,
                        data,
                        {},
                        mode=mode,
                    )
                except Exception as hook_error:
                    cleanup_report.setdefault("hook_errors", []).append(
                        {
                            "event": event,
                            "error_type": type(hook_error).__name__,
                            "error": str(hook_error)[:500],
                        }
                    )

            # The explicit event is the new contract. The legacy event remains
            # for one compatibility cycle and is emitted at the same boundary.
            await emit_lifecycle(
                "session.delete.before",
                payload,
            )
            await emit_lifecycle(
                "session.delete",
                payload,
            )

            from ..agent.agents import BuiltinAgents
            from ..learning import LearningManager
            from ..memory.service import ScopedMemoryService
            from ..types import AgentMode

            memory = ScopedMemoryService(resolved_workdir)
            cleanup_report["session_memory"] = await memory.cleanup_scope(
                scope="session",
                scope_key=resolved_session.id,
                source="session.delete",
                allow_rejection=False,
            )
            agent_info = BuiltinAgents.get(resolved_session.agent_name)
            if getattr(agent_info, "mode", None) == AgentMode.MANAGER:
                cleanup_report["team_memory"] = await memory.cleanup_scope(
                    scope="team",
                    scope_key=resolved_session.id,
                    source="session.delete",
                    allow_rejection=False,
                )
            cleanup_report["learning_reflections_deleted"] = (
                LearningManager.delete_scope(
                    workdir=resolved_workdir,
                    scope_kind="session",
                    scope_key=resolved_session.id,
                )
            )
            from ..external_harness.handoff_store import HarnessHandoffStore
            from ..external_harness.session_binding import HarnessSessionBindingStore

            owner_key = (
                f"{resolved_session.tenant_id}:{resolved_session.user_id}"
            )
            await asyncio.to_thread(
                HarnessHandoffStore().delete_session,
                owner_key=owner_key,
                workspace_id=resolved_session.workspace_id,
                session_id=resolved_session.id,
            )
            await asyncio.to_thread(
                HarnessSessionBindingStore().delete_session,
                owner_key=owner_key,
                workspace_id=resolved_session.workspace_id,
                session_id=resolved_session.id,
            )

            async with cls._get_lock():
                cls._load_from_disk()
                current = cls._sessions.get(session_id)
                if not cls._matches_owner(
                    current,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                ):
                    return
                if current is None:
                    return
                from .events import SessionEventStore

                SessionEventStore.append(
                    session_id=current.id,
                    event_type="session.deleted",
                    owner=owner,
                    idempotency_key=(
                        f"session.deleted:{current.id}:after:"
                        f"{int(SessionEventStore.project(current.id).get('last_sequence', 0) or 0)}"
                    ),
                )
                del cls._sessions[session_id]
                await asyncio.to_thread(
                    SessionSQLiteStore.delete,
                    session_id,
                    tenant_id=resolved_session.tenant_id,
                    user_id=resolved_session.user_id,
                    workspace_id=resolved_session.workspace_id,
                )

            await emit_lifecycle(
                "session.delete.after",
                {**payload, "memory_cleanup": cleanup_report},
                mode="parallel",
            )
        except BaseException as exc:
            try:
                from ..server.plugin_warmup import trigger_plugin_event

                await trigger_plugin_event(
                    resolved_workdir,
                    "session.delete.error",
                    {
                        **payload,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "memory_cleanup": cleanup_report,
                    },
                    {},
                    mode="parallel",
                )
            except Exception:
                pass
            raise

    @classmethod
    async def list(
        cls,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_archived: Optional[bool] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[Session]:
        """List sessions, optionally filtered"""
        async with cls._get_lock():
            # The first load replays canonical logs and can span gigabytes.
            # Keep the event loop responsive while holding the session lock.
            await asyncio.to_thread(cls._load_from_disk)
            sessions = [
                s
                for s in cls._sessions.values()
                if cls._matches_owner(
                    s,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
            ]
            if status:
                sessions = [s for s in sessions if s.status == status]
            if parent_id:
                sessions = [s for s in sessions if s.parent_id == parent_id]
            if include_archived is not None:
                sessions = [
                    s
                    for s in sessions
                    if cls.is_archived_session(s) == include_archived
                ]
            return sorted(sessions, key=lambda x: x.created_at, reverse=True)

    @classmethod
    async def set_archived(
        cls,
        session_id: str,
        *,
        archived: bool,
        archived_by_user: bool = True,
        archive_cause: Optional[str] = None,
        archive_locked: Optional[bool] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        async with cls._get_lock():
            cls._load_from_disk()
            session = cls._sessions.get(session_id)
            if session is None or not cls._matches_owner(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            ):
                return None

            metadata = dict(getattr(session, "metadata", {}) or {})
            if archived:
                metadata[ARCHIVED_AT_METADATA_KEY] = datetime.now().timestamp()
                if archived_by_user:
                    metadata[ARCHIVED_BY_USER_METADATA_KEY] = True
                if archive_cause:
                    metadata[ARCHIVE_CAUSE_METADATA_KEY] = str(archive_cause)
                if archive_locked is not None:
                    metadata[ARCHIVE_LOCKED_METADATA_KEY] = bool(archive_locked)
            else:
                if bool(metadata.get(ARCHIVE_LOCKED_METADATA_KEY)):
                    raise ValueError("该研究会话已随历史课题锁定，请先恢复整个课题")
                metadata.pop(ARCHIVED_AT_METADATA_KEY, None)
                metadata.pop(ARCHIVED_BY_USER_METADATA_KEY, None)
                metadata.pop(ARCHIVE_CAUSE_METADATA_KEY, None)
                metadata.pop(ARCHIVE_LOCKED_METADATA_KEY, None)
                session.last_active_at = datetime.now().timestamp()

            session.metadata = metadata
            await cls._save_to_disk_async([session_id])
            await asyncio.to_thread(
                _append_research_session_event,
                session,
                "session.archived" if archived else "session.resumed",
            )
            return session

    @classmethod
    async def set_archive_locked(
        cls,
        session_id: str,
        locked: bool,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Session]:
        def apply(session: Session) -> Session:
            metadata = dict(session.metadata or {})
            if cls.is_archived_session(session):
                metadata[ARCHIVE_LOCKED_METADATA_KEY] = bool(locked)
            session.metadata = metadata
            return session

        return await cls.mutate(
            session_id,
            apply,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    @classmethod
    async def clear(
        cls,
        *,
        session_ids: Optional[Iterable[str]] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ):
        """持久删除匹配 Session，并为每条权威事件链写入终态墓碑。"""
        async with cls._get_lock():
            cls._load_from_disk()
            has_requested_ids = session_ids is not None
            raw_session_ids = (
                [session_ids]
                if isinstance(session_ids, str)
                else list(session_ids or [])
            )
            requested_ids = {
                str(session_id or "").strip()
                for session_id in raw_session_ids
                if str(session_id or "").strip()
            }
            owner_filter = any(
                str(value or "").strip()
                for value in (tenant_id, user_id, workspace_id)
            )
            target_ids = [
                session_id
                for session_id, session in cls._sessions.items()
                if (not has_requested_ids or session_id in requested_ids)
                and (
                    not owner_filter
                    or cls._matches_owner(
                        session,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                    )
                )
            ]

            from .events import SessionEventStore

            # 先把所有 Tombstone 可靠落盘，再删除查询投影。中途失败时仍
            # 保留全部 SQLite 行，避免出现查询投影已清空但事件尚可复活。
            for session_id in target_ids:
                session = cls._sessions.get(session_id)
                if session is None:
                    continue
                projection = SessionEventStore.project(session_id)
                SessionEventStore.append(
                    session_id=session_id,
                    event_type="session.deleted",
                    payload={"source": "session.clear"},
                    owner={
                        "tenant_id": session.tenant_id,
                        "user_id": session.user_id,
                        "workspace_id": session.workspace_id,
                    },
                    idempotency_key=(
                        f"session.deleted:{session_id}:clear-after:"
                        f"{int(projection.get('last_sequence', 0) or 0)}"
                    ),
                )

            for session_id in target_ids:
                session = cls._sessions.get(session_id)
                if session is None:
                    continue
                cls._sessions.pop(session_id, None)
                SessionSQLiteStore.delete(
                    session_id,
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    workspace_id=session.workspace_id,
                )

    @classmethod
    def get_all_sync(
        cls,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> dict[str, Session]:
        """Get all sessions (sync version)"""
        cls._load_from_disk()
        return {
            session_id: session
            for session_id, session in cls._sessions.items()
            if cls._matches_owner(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        }

    @classmethod
    async def search(
        cls,
        query: str,
        *,
        limit: int = 5,
        session_id: Optional[str] = None,
        workdir: Optional[str] = None,
        revision: Optional[int] = None,
        include_history: bool = False,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls._load_from_disk()
        return SessionSQLiteStore.search(
            query,
            limit=limit,
            session_id=session_id,
            workdir=workdir,
            revision=revision,
            include_history=include_history,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    @classmethod
    async def recent(
        cls,
        *,
        limit: int = 10,
        workdir: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls._load_from_disk()
        return SessionSQLiteStore.recent(
            limit=limit,
            workdir=workdir,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )

    @classmethod
    async def revisions(
        cls,
        session_id: str,
        *,
        limit: int = 20,
        workdir: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cls._load_from_disk()
        return SessionSQLiteStore.list_revisions(
            session_id,
            limit=limit,
            workdir=workdir,
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
