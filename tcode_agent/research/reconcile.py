"""Best-effort reconciliation from canonical Session snapshots into the Journal."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .journal import ResearchEventSink
from .models import ResearchEventInput, sha256_text
from .trace import trace_id_for_turn


_READ_ONLY_ARTIFACT_TOOLS = {
    "read",
    "glob",
    "grep",
    "session_search",
    "memory_read",
    "evidence_search",
    "evidence_graph",
    "paper_library_lookup_pdf",
    "paper_library_query",
    "paper_library_stats",
    "paper_library_count",
    "paper_library_get_record",
}


@dataclass
class ResearchReconcileReport:
    sessions_scanned: int = 0
    messages_scanned: int = 0
    tool_calls_scanned: int = 0
    artifacts_seen: int = 0
    event_attempts: int = 0
    event_results: int = 0
    events_created: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class ResearchJournalReconciler:
    """Replays canonical facts using the same idempotency keys as live hooks."""

    def __init__(self, *, workdir: str | None = None):
        self.workdir = workdir
        self.report = ResearchReconcileReport()

    @staticmethod
    def _owner(session: Any) -> dict[str, str]:
        metadata = dict(getattr(session, "metadata", {}) or {})
        return {
            "tenant_id": str(
                getattr(session, "tenant_id", "")
                or metadata.get("tenant_id")
                or ""
            ),
            "user_id": str(
                getattr(session, "user_id", "")
                or metadata.get("user_id")
                or ""
            ),
            "workspace_id": str(
                getattr(session, "workspace_id", "")
                or metadata.get("workspace_id")
                or ""
            ),
        }

    def _append(self, event_input: ResearchEventInput, *, workdir: str | None) -> None:
        self.report.event_attempts += 1
        try:
            event, created = ResearchEventSink.append_with_status(
                event_input,
                workdir=workdir,
            )
            if event is not None:
                self.report.event_results += 1
            if created:
                self.report.events_created += 1
        except Exception:
            self.report.errors += 1

    @staticmethod
    def _mcp_identity(tool_call: Any) -> tuple[str, str]:
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        server = str(metadata.get("mcp_server") or "").strip()
        tool = str(metadata.get("mcp_tool_name") or "").strip()
        tool_name = str(getattr(tool_call, "tool_name", "") or "")
        if not server and tool_name.startswith("mcp_"):
            parts = tool_name.split("_", 2)
            if len(parts) == 3:
                server, tool = parts[1], parts[2]
        return server, tool

    @staticmethod
    def _artifact_paths(tool_call: Any) -> list[str]:
        tool_name = str(getattr(tool_call, "tool_name", "") or "").strip()
        if tool_name in _READ_ONLY_ARTIFACT_TOOLS:
            return []
        metadata = dict(getattr(tool_call, "metadata", {}) or {})
        values: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)

        collect(metadata.get("output_path"))
        collect(metadata.get("artifact_paths"))
        collect(metadata.get("artifacts"))
        return list(dict.fromkeys(values))[:32]

    def reconcile_session(self, session: Any) -> ResearchReconcileReport:
        self.report.sessions_scanned += 1
        owner = self._owner(session)
        session_id = str(getattr(session, "id", "") or "")
        workdir = str(getattr(session, "workdir", "") or self.workdir or "") or None
        self._append(
            ResearchEventInput(
                event_type="session.created",
                **owner,
                session_id=session_id,
                actor_type="system",
                actor_id="research-reconciler",
                source_kind="session",
                source_id=session_id,
                source_ref=f"session:{session_id}",
                payload={
                    "title": str(getattr(session, "title", "") or ""),
                    "agent_name": str(getattr(session, "agent_name", "") or ""),
                    "reconciled": True,
                },
                privacy_state="private",
                stable_state_version="session.created",
            ),
            workdir=workdir,
        )

        current_turn_id = ""
        current_trace_id = ""
        for message in list(getattr(session, "messages", []) or []):
            self.report.messages_scanned += 1
            role = str(getattr(message, "role", "") or "system").strip().lower()
            message_id = str(getattr(message, "id", "") or "")
            if role == "user" and not bool(getattr(message, "_internal", False)):
                current_turn_id = f"turn-{message_id}"
                current_trace_id = trace_id_for_turn(session_id, current_turn_id)
            if not current_turn_id:
                current_turn_id = f"turn-recovered-{message_id or 'unknown'}"
                current_trace_id = trace_id_for_turn(session_id, current_turn_id)

            actor_type = "agent" if role == "assistant" else role
            if actor_type not in {"user", "agent", "tool", "system"}:
                actor_type = "system"
            self._append(
                ResearchEventInput(
                    event_type="message.created",
                    **owner,
                    session_id=session_id,
                    turn_id=current_turn_id,
                    trace_id=current_trace_id,
                    actor_type=actor_type,
                    actor_id=(
                        str(getattr(session, "agent_name", "") or "")
                        if actor_type == "agent"
                        else owner["user_id"]
                    ),
                    source_kind="session_message",
                    source_id=message_id,
                    source_ref=f"session:{session_id}/message:{message_id}",
                    payload={
                        "role": role,
                        "content_preview": str(getattr(message, "content", "") or ""),
                        "parent_id": str(getattr(message, "parent_id", "") or ""),
                        "reconciled": True,
                    },
                    privacy_state="private",
                ),
                workdir=workdir,
            )
            if role == "user" and not bool(getattr(message, "_internal", False)):
                self._append(
                    ResearchEventInput(
                        event_type="turn.started",
                        **owner,
                        session_id=session_id,
                        turn_id=current_turn_id,
                        trace_id=current_trace_id,
                        actor_type="user",
                        actor_id=owner["user_id"],
                        source_kind="agent_turn",
                        source_id=current_turn_id,
                        source_ref=f"session:{session_id}/turn:{current_turn_id}",
                        payload={"reconciled": True},
                        privacy_state="private",
                        stable_state_version="turn.started",
                    ),
                    workdir=workdir,
                )

            for tool_call in list(getattr(message, "tool_calls", []) or []):
                self.report.tool_calls_scanned += 1
                call_id = str(getattr(tool_call, "call_id", "") or "")
                tool_name = str(getattr(tool_call, "tool_name", "") or "")
                state_obj = getattr(tool_call, "state", None)
                state = str(getattr(state_obj, "value", state_obj) or "").lower()
                server, mcp_tool = self._mcp_identity(tool_call)
                source_kind = "mcp_tool" if server else "tool_call"
                if getattr(tool_call, "start_time", None) is not None:
                    self._append(
                        ResearchEventInput(
                            event_type="mcp.called" if server else "tool.started",
                            **owner,
                            session_id=session_id,
                            turn_id=current_turn_id,
                            trace_id=current_trace_id,
                            actor_type="tool",
                            actor_id=server or tool_name,
                            source_kind=source_kind,
                            source_id=call_id,
                            source_ref=f"session:{session_id}/tool:{call_id}",
                            payload={
                                "tool_name": tool_name,
                                "call_id": call_id,
                                "arguments": dict(
                                    getattr(tool_call, "arguments", {}) or {}
                                ),
                                "state": "running",
                                "mcp_server": server,
                                "mcp_tool_name": mcp_tool,
                                "reconciled": True,
                            },
                            privacy_state="private",
                            stable_state_version=(
                                "mcp.called" if server else "tool.started"
                            ),
                        ),
                        workdir=workdir,
                    )
                if state in {"completed", "error"}:
                    failed = state == "error" or bool(
                        str(getattr(tool_call, "error", "") or "").strip()
                    )
                    event_type = (
                        ("mcp.failed" if failed else "mcp.completed")
                        if server
                        else ("tool.failed" if failed else "tool.completed")
                    )
                    self._append(
                        ResearchEventInput(
                            event_type=event_type,
                            **owner,
                            session_id=session_id,
                            turn_id=current_turn_id,
                            trace_id=current_trace_id,
                            actor_type="tool",
                            actor_id=server or tool_name,
                            source_kind=source_kind,
                            source_id=call_id,
                            source_ref=f"session:{session_id}/tool:{call_id}",
                            payload={
                                "tool_name": tool_name,
                                "call_id": call_id,
                                "state": state,
                                "output_preview": str(
                                    getattr(tool_call, "output", "") or ""
                                ),
                                "error_preview": str(
                                    getattr(tool_call, "error", "") or ""
                                ),
                                "mcp_server": server,
                                "mcp_tool_name": mcp_tool,
                                "reconciled": True,
                            },
                            privacy_state="private",
                            stable_state_version=event_type,
                        ),
                        workdir=workdir,
                    )

                for path in self._artifact_paths(tool_call):
                    self.report.artifacts_seen += 1
                    self._append(
                        ResearchEventInput(
                            event_type="artifact.created",
                            **owner,
                            session_id=session_id,
                            turn_id=current_turn_id,
                            trace_id=current_trace_id,
                            actor_type="tool",
                            actor_id=tool_name,
                            source_kind="tool_artifact",
                            source_id=f"{call_id}:{sha256_text(path)}",
                            source_ref=path,
                            payload={
                                "path": path,
                                "tool_name": tool_name,
                                "call_id": call_id,
                                "reconciled": True,
                            },
                            privacy_state="private",
                        ),
                        workdir=workdir,
                    )
        return self.report

    def reconcile(self, sessions: Iterable[Any]) -> ResearchReconcileReport:
        for session in sessions:
            try:
                self.reconcile_session(session)
            except Exception:
                self.report.errors += 1
        return self.report
