"""Human-readable Research Activity projection over immutable ResearchEvent logs."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .attribution import _event_text, _parse_time
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent, canonical_json, sha256_text, utc_now_iso
from .store import ResearchEventStore
from .topic_store import ResearchTopicStore


ACTIVITY_SCHEMA_VERSION = 5
ACTIVITY_PROJECTION_VERSION = "research-activity-9-markdown-progress"
_EVENT_FILE_SCAN_MAX_VALUES = 256
_EVENT_FILE_SCAN_MAX_CHARS = 32_768
_SHA256_PATTERN = re.compile(r"(?i)\b[a-f0-9]{64}\b")
# Match one path-like token, then let ``_normalize_file_ref`` decide whether it
# is a supported file reference.  Do not reintroduce a repeated group whose
# body can also consume path separators: that shape caused exponential
# backtracking on long Artifact result paths without an ``/upload/`` segment.
_PATH_PATTERN = re.compile(
    r"(?i)(?:[a-z]:)?[/\\][^\s\"'<>]+"
)
_PAPER_TITLE_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:paper[ _-]?title|document[ _-]?title|title|标题|论文标题|题目)"
    r"(?:\*\*)?\s*[:：]\s*(.{8,240}?)\s*$"
)
_MARKDOWN_PAPER_HEADING_PATTERN = re.compile(
    r"(?m)^\s*\*\*(.{20,240}?)\*\*\s*$"
)
_PAPER_DOC_ID_PATTERN = re.compile(
    r"(?im)(?:\*\*)?doc_id(?:\*\*)?\s*[:=：]\s*`?([a-z0-9._-]{4,160})"
)
_PAPER_PATH_DOC_ID_PATTERN = re.compile(r"(?i)/(paper-[a-z0-9._-]+)/")
_PAPER_VERSION_PATTERN = re.compile(r"(?i)/versions/([^/]+)")
_PAPER_RESULT_TOOL_NAMES = {
    "paper_library_ingest_pdf",
    "paper_library_lookup_pdf",
    "paper_library_get_record",
}
_PAPER_PRIMARY_ARTIFACTS = {
    "source.pdf": ("source_pdf", "原始论文"),
    "record.json": ("record", "文献库记录"),
    "full_text.md": ("full_text", "全文"),
    "l0_summary.json": ("l0_summary", "L0 摘要"),
    "l1_passages.json": ("l1_passages", "L1 段落"),
    "l2_facts.json": ("l2_facts", "L2 事实"),
    "evidence_anchors.json": ("evidence", "证据锚点"),
    "tables_extract.json": ("tables", "表格提取"),
    "visual_manifest.json": ("visuals", "图片清单"),
}
_FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".tsv", ".json",
    ".xlsx", ".xls", ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".svg",
    ".pdb", ".sdf", ".mol", ".mol2", ".fasta", ".fa", ".py", ".ipynb",
}
_CONTROL_EVENT_TYPES = {
    "session.created", "session.archived", "session.resumed",
    "turn.started", "turn.completed", "turn.failed", "turn.cancelled",
    "message.compacted", "agent.spawned", "agent.started", "agent.completed",
    "agent.partial", "agent.waiting", "agent.failed", "agent.cancelled", "tool.started", "mcp.started",
    "workflow.started",
}
_INLINE_TECH_REF_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:session|trace|event|turn|call|episode|checkpoint|run)"
    r"\s*[:#-]\s*[a-z0-9._:-]+"
)


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return default
    return value if isinstance(value, type(default)) else default


def _walk(value: Any, *, depth: int = 0) -> Iterable[tuple[str, Any]]:
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in list(value.items())[:100]:
            yield str(key), item
            yield from _walk(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in list(value)[:100]:
            yield from _walk(item, depth=depth + 1)


def _strings(value: Any, *, limit: int | None = None) -> list[str]:
    output: list[str] = []
    maximum = None if limit is None else max(0, int(limit))
    if maximum == 0:
        return output
    if isinstance(value, str) and value.strip():
        output.append(value.strip())
        if maximum is not None and len(output) >= maximum:
            return output
    for _, item in _walk(value):
        if isinstance(item, str) and item.strip():
            output.append(item.strip())
            if maximum is not None and len(output) >= maximum:
                break
    return output


def _bounded_file_scan_text(value: Any) -> str:
    """Bound regex work while retaining useful references at both edges."""

    text = str(value or "")
    if len(text) <= _EVENT_FILE_SCAN_MAX_CHARS:
        return text
    separator = "\n[...event file scan truncated...]\n"
    edge = max(1, (_EVENT_FILE_SCAN_MAX_CHARS - len(separator)) // 2)
    return text[:edge] + separator + text[-edge:]


def _normalize_file_ref(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("`.,;:)]}")
    if not text:
        return ""
    upload_match = re.search(
        r"(?i)(?:^|[/\s:=])uploads?/(.+?\.(?:pdf|docx?|txt|md|csv|tsv|json|xlsx?|pptx?|png|jpe?g|svg|pdb|sdf|mol2?|fasta|fa|py|ipynb))(?=$|[\s`\"'<>])",
        text,
    )
    if upload_match:
        return ("upload/" + upload_match.group(1)).rstrip("`.,;:)]}")[:1000]
    lowered = text.lower()
    marker_positions = [
        (lowered.rfind(marker), marker)
        for marker in ("/upload/", "/uploads/")
    ]
    position, marker = max(marker_positions, key=lambda item: item[0])
    if position >= 0:
        text = "upload/" + text[position + len(marker) :].lstrip("/")
    elif lowered.startswith(("upload/", "uploads/")):
        text = "upload/" + text.split("/", 1)[1]
    else:
        suffix = PurePosixPath(text).suffix.lower()
        if suffix not in _FILE_EXTENSIONS or (
            "/" not in text and "\\" not in text and bool(re.search(r"\s", text))
        ):
            return ""
    return text[:1000]


def _event_file_keys(event: ResearchEvent) -> set[str]:
    keys: set[str] = set()
    values = [event.source_ref]
    values.extend(
        _strings(
            event.payload,
            limit=max(0, _EVENT_FILE_SCAN_MAX_VALUES - 1),
        )
    )
    for raw_value in values[:_EVENT_FILE_SCAN_MAX_VALUES]:
        value = _bounded_file_scan_text(raw_value)
        normalized = _normalize_file_ref(value)
        if normalized:
            keys.add(f"path:{normalized.lower()}")
        for match in _PATH_PATTERN.findall(value):
            upload_token = str(match or "").replace("\\", "/").lower()
            if "/upload/" not in upload_token and "/uploads/" not in upload_token:
                continue
            normalized = _normalize_file_ref(match)
            if normalized:
                keys.add(f"path:{normalized.lower()}")
        for digest in _SHA256_PATTERN.findall(value):
            keys.add(f"sha256:{digest.lower()}")
    return keys


def _group_file_keys(event: ResearchEvent) -> set[str]:
    """Only actual inputs join actions; returned historical records are references."""
    from types import SimpleNamespace
    if _is_file(event) or _is_artifact(event):
        return _event_file_keys(event)
    if event.source_kind in {"tool_call", "mcp_tool"}:
        payload = {"arguments": event.payload.get("arguments") or {}}
    elif event.event_type == "message.created" and event.actor_type == "user":
        payload = {"attachments": event.payload.get("attachments") or []}
    else:
        return set()
    return _event_file_keys(SimpleNamespace(source_ref="", payload=payload))


def _call_ids(event: ResearchEvent) -> set[str]:
    values: set[str] = set()
    if event.source_kind in {"tool_call", "mcp_tool"} and event.source_id:
        values.add(event.source_id)
    for key, item in _walk(event.payload):
        if key.lower() in {"call_id", "parent_tool_call_id"} and isinstance(item, str):
            if item.strip():
                values.add(item.strip())
    return values


def _child_session_ids(event: ResearchEvent) -> set[str]:
    values: set[str] = set()
    for key, item in _walk(event.payload):
        if key.lower() in {"subagent_session_id", "child_session_id"} and isinstance(item, str):
            if item.strip():
                values.add(item.strip())
    if event.event_type == "session.created" and event.source_id and event.payload.get("parent_id"):
        values.add(event.source_id)
    return values


def _is_file(event: ResearchEvent) -> bool:
    return event.event_type.startswith("file.") or event.source_kind in {
        "file_upload", "file_change",
    }


def _is_artifact(event: ResearchEvent) -> bool:
    return (
        event.event_type.startswith(("artifact.", "document."))
        or event.source_kind in {"artifact", "tool_artifact", "document"}
    )


def _artifact_has_user_semantics(event: ResearchEvent) -> bool:
    """Return whether an Artifact says what it is, rather than only where it lives."""

    payload = event.payload or {}
    for key in (
        "paper_title",
        "document_title",
        "artifact_title",
        "display_name",
        "summary",
        "summary_text",
        "statement",
        "description",
        "content_preview",
        "output_preview",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return True

    title = str(payload.get("title") or "").strip()
    if title and title.lower() not in {
        "artifact", "file", "output", "result", "task", "tool artifact",
    }:
        return True

    # A user-created document/file with a name is understandable. A Tool Artifact
    # whose only metadata is ``path + tool_name`` is an implementation output and
    # must stay in the audit layer even if its path happens to end in ``.md``.
    filename = str(payload.get("filename") or "").strip()
    return bool(filename and event.source_kind != "tool_artifact")


def _is_control(event: ResearchEvent) -> bool:
    if event.event_type in _CONTROL_EVENT_TYPES:
        return True
    if _is_artifact(event) and not _artifact_has_user_semantics(event):
        return True
    content = str(event.payload.get("content_preview") or "").strip()
    if event.event_type == "message.created":
        if content.startswith("# Task Contract"):
            return True
        tool_calls = event.payload.get("tool_calls")
        if event.actor_type == "agent" and not content and isinstance(tool_calls, list) and tool_calls:
            return True
        if event.payload.get("reconciled") and str(event.payload.get("role") or "") == "tool":
            return True
    return False


def _category(event: ResearchEvent) -> str:
    event_type = event.event_type.lower()
    kind = event.source_kind.lower()
    if _is_file(event):
        return "file"
    if _is_artifact(event):
        return "artifact"
    if event_type.startswith("mcp.") or kind == "mcp_tool":
        return "mcp"
    if event_type.startswith("workflow.") or kind == "workflow":
        return "workflow"
    if event_type.startswith("agent.") or kind == "subagent":
        return "agent"
    if event_type.startswith("tool.") or kind == "tool_call":
        return "tool"
    if event_type.startswith("message.") or kind == "session_message":
        return "message"
    return "other"


def _clean_text(value: Any, limit: int = 1000) -> str:
    text = _INLINE_TECH_REF_PATTERN.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-—:：")
    return text[:limit]


def _filename(file_ref: str) -> str:
    return PurePosixPath(str(file_ref or "")).name[:240]


def _portable_id(prefix: str, value: str) -> str:
    """Build a stable opaque projection ID without exposing raw Session IDs."""

    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return f"{prefix}-{sha256_text(f'{prefix}:{normalized}').split(':', 1)[-1][:24]}"


def _agent_run_id(session_id: str) -> str:
    return _portable_id("agent-run", session_id)


def _file_id(path: str) -> str:
    return _portable_id("file", path)


def _artifact_id(ref: str) -> str:
    return _portable_id("artifact", ref)


@dataclass(frozen=True)
class ResearchActivity:
    activity_id: str
    schema_version: int
    tenant_id: str
    user_id: str
    workspace_id: str
    activity_key: str
    activity_type: str
    title: str
    summary: str
    status: str
    review_state: str
    recommendation: str
    confidence: float
    reason_code: str
    candidate_id: str
    research_id: str
    event_ids: list[str]
    semantic_event_ids: list[str]
    control_event_ids: list[str]
    session_ids: list[str]
    trace_ids: list[str]
    tool_calls: list[dict[str, Any]]
    agent_calls: list[dict[str, Any]]
    workflow_calls: list[dict[str, Any]]
    agent_runs: list[dict[str, Any]]
    file_refs: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    artifact_groups: list[dict[str, Any]]
    category_counts: dict[str, int]
    started_at: str
    ended_at: str
    source_digest: str
    rebuilt_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "ResearchActivity":
        values = dict(row)
        for source, target, default in (
            ("event_ids_json", "event_ids", []),
            ("semantic_event_ids_json", "semantic_event_ids", []),
            ("control_event_ids_json", "control_event_ids", []),
            ("session_ids_json", "session_ids", []),
            ("trace_ids_json", "trace_ids", []),
            ("tool_calls_json", "tool_calls", []),
            ("agent_calls_json", "agent_calls", []),
            ("workflow_calls_json", "workflow_calls", []),
            ("agent_runs_json", "agent_runs", []),
            ("file_refs_json", "file_refs", []),
            ("artifact_refs_json", "artifact_refs", []),
            ("artifact_groups_json", "artifact_groups", []),
            ("category_counts_json", "category_counts", {}),
        ):
            values[target] = _json_value(values.pop(source, default), default)
        values["schema_version"] = int(values.get("schema_version") or 0)
        values["confidence"] = float(values.get("confidence") or 0)
        return cls(**values)


@dataclass
class ResearchActivityApplyReport:
    """Bounded counters for one full or incremental projection pass."""

    mode: str
    input_event_count: int = 0
    context_event_count: int = 0
    activity_count: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    removed_count: int = 0
    affected_activity_ids: list[str] = field(default_factory=list)
    affected_research_ids: list[str] = field(default_factory=list)
    removed_activity_ids: list[str] = field(default_factory=list)
    removed_activity_keys: list[str] = field(default_factory=list)
    activities: list[ResearchActivity] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["activities"] = [item.to_dict() for item in self.activities]
        return value


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


class ResearchActivityStore:
    _JSON_FIELDS = (
        "event_ids",
        "semantic_event_ids",
        "control_event_ids",
        "session_ids",
        "trace_ids",
        "tool_calls",
        "agent_calls",
        "workflow_calls",
        "agent_runs",
        "file_refs",
        "artifact_refs",
        "artifact_groups",
        "category_counts",
    )

    @classmethod
    def _serialized_values(cls, activity: ResearchActivity) -> dict[str, Any]:
        values = activity.to_dict()
        for key in cls._JSON_FIELDS:
            values[f"{key}_json"] = canonical_json(values.pop(key))
        return values

    @classmethod
    def health(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> dict[str, Any]:
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                """
                SELECT review_state, status, COUNT(*) AS count
                FROM research_activities
                WHERE tenant_id=? AND user_id=? AND workspace_id=?
                GROUP BY review_state, status
                """,
                (tenant_id, user_id, workspace_id),
            ).fetchall()
        finally:
            conn.close()
        review_states: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        for row in rows:
            count = int(row["count"] or 0)
            review_states[str(row["review_state"] or "unknown")] += count
            statuses[str(row["status"] or "unknown")] += count
        return {
            "count": sum(review_states.values()),
            "review_states": dict(review_states),
            "statuses": dict(statuses),
        }

    @classmethod
    def replace_owner(
        cls,
        activities: list[ResearchActivity],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> None:
        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        conn = ResearchEventStore._connect(workdir, settings=settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM research_activities WHERE tenant_id=? AND user_id=? AND workspace_id=?",
                (tenant_id, user_id, workspace_id),
            )
            for activity in activities:
                values = cls._serialized_values(activity)
                columns = list(values)
                conn.execute(
                    f"INSERT INTO research_activities({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)})",
                    [values[key] for key in columns],
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @classmethod
    def upsert_many(
        cls,
        activities: list[ResearchActivity],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> tuple[list[ResearchActivity], dict[str, int]]:
        """Upsert only affected Activity keys; never delete unrelated rows."""

        ResearchEventStore.ensure_initialized(workdir, settings=settings)
        conn = ResearchEventStore._connect(workdir, settings=settings)
        persisted: list[ResearchActivity] = []
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        try:
            conn.execute("BEGIN IMMEDIATE")
            for activity in activities:
                current = conn.execute(
                    "SELECT * FROM research_activities WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND activity_key=? LIMIT 1",
                    (tenant_id, user_id, workspace_id, activity.activity_key),
                ).fetchone()
                if current is not None and str(current["source_digest"] or "") == activity.source_digest:
                    persisted.append(ResearchActivity.from_row(current))
                    counts["unchanged"] += 1
                    continue
                values = cls._serialized_values(activity)
                if current is None:
                    columns = list(values)
                    conn.execute(
                        f"INSERT INTO research_activities({', '.join(columns)}) "
                        f"VALUES({', '.join('?' for _ in columns)})",
                        [values[key] for key in columns],
                    )
                    counts["created"] += 1
                else:
                    activity_id = str(current["activity_id"])
                    values["activity_id"] = activity_id
                    columns = [key for key in values if key != "activity_id"]
                    conn.execute(
                        "UPDATE research_activities SET "
                        + ", ".join(f"{key}=?" for key in columns)
                        + " WHERE activity_id=?",
                        [*[values[key] for key in columns], activity_id],
                    )
                    counts["updated"] += 1
                row = conn.execute(
                    "SELECT * FROM research_activities WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND activity_key=? LIMIT 1",
                    (tenant_id, user_id, workspace_id, activity.activity_key),
                ).fetchone()
                if row is None:
                    raise RuntimeError("research activity upsert did not persist a row")
                persisted.append(ResearchActivity.from_row(row))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return persisted, counts

    @classmethod
    def delete_keys(
        cls,
        activity_keys: list[str],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None,
        settings: ResearchSettings,
    ) -> int:
        normalized = list(
            dict.fromkeys(str(item or "").strip() for item in activity_keys if str(item or "").strip())
        )[:2000]
        if not normalized:
            return 0
        conn = ResearchEventStore._connect(workdir, settings=settings)
        try:
            cursor = conn.execute(
                "DELETE FROM research_activities WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                "AND activity_key IN (" + ",".join("?" for _ in normalized) + ")",
                (tenant_id, user_id, workspace_id, *normalized),
            )
            return max(0, int(cursor.rowcount or 0))
        finally:
            conn.close()

    @classmethod
    def list(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        review_state: str = "",
        limit: int = 500,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> list[ResearchActivity]:
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        clauses = ["tenant_id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [tenant_id, user_id, workspace_id]
        if review_state:
            clauses.append("review_state=?")
            params.append(review_state)
        params.append(max(1, min(int(limit or 500), 2000)))
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            rows = conn.execute(
                "SELECT * FROM research_activities WHERE " + " AND ".join(clauses)
                + " ORDER BY started_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [ResearchActivity.from_row(row) for row in rows]

    @classmethod
    def get(
        cls,
        activity_id: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> ResearchActivity | None:
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        conn = ResearchEventStore._connect(workdir, settings=resolved)
        try:
            row = conn.execute(
                "SELECT * FROM research_activities WHERE activity_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
                (activity_id, tenant_id, user_id, workspace_id),
            ).fetchone()
        finally:
            conn.close()
        return ResearchActivity.from_row(row) if row is not None else None


class ResearchActivityBuilder:
    """Build a replaceable, owner-scoped activity projection without mutating events."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ):
        self.owner = {"tenant_id": tenant_id, "user_id": user_id, "workspace_id": workspace_id}
        self.workdir = workdir
        self.settings = settings or load_research_settings()

    def rebuild(self, *, limit: int = 5000) -> list[ResearchActivity]:
        """Compatibility alias for the explicit maintenance-only full rebuild."""
        return self.rebuild_owner(limit=limit)

    def rebuild_owner(self, *, limit: int = 5000) -> list[ResearchActivity]:
        """Rebuild the complete Owner projection for startup/manual repair only."""
        if not self.settings.activities_active:
            return []
        events = ResearchEventStore.list_events_for_attribution(
            **self.owner, workdir=self.workdir, settings=self.settings, limit=min(5000, max(1, limit))
        )
        links = ResearchEventStore.list_attribution_links(
            **self.owner, workdir=self.workdir, settings=self.settings,
            active_only=True, limit=5000,
        )
        link_by_event = {link.event_id: link for link in links}
        activities = self._build(events, link_by_event)
        previous = ResearchActivityStore.list(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            limit=2000,
        )
        ResearchActivityStore.replace_owner(
            activities, **self.owner, workdir=self.workdir, settings=self.settings
        )
        active_keys = {item.activity_key for item in activities}
        stale_keys: list[str] = []
        for old in previous:
            if old.activity_key not in active_keys:
                stale_keys.append(old.activity_key)
                self._synchronize_activity_scopes(old.activity_id, [])
        for activity in activities:
            self._synchronize_activity_scopes(
                activity.activity_id,
                self._activity_research_ids(activity),
            )
        if self.settings.experience_projection_active:
            from .experience import ResearchExperienceProjector
            from .experience_store import ResearchExperienceStore

            stale_keys.extend(
                item.experience_key.split(":", 1)[1]
                for item in ResearchExperienceStore.list_items(
                    **self.owner,
                    limit=5000,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                if item.experience_key.startswith("activity:")
                and item.experience_key.split(":", 1)[1] not in active_keys
            )
            stale_keys = list(dict.fromkeys(stale_keys))

            experience_projector = ResearchExperienceProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            experience_projector.remove_activity_keys(stale_keys)
            experience_projector.apply_activities(
                [activity.activity_id for activity in activities]
            )
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            projector = ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            for activity in activities:
                projector.project_activity(activity)
        except Exception:
            # Overlay is rebuilt from local Activity/Event state and therefore
            # cannot make the primary Activity projection fail.
            pass
        return activities

    def apply_events(self, event_ids: list[str]) -> ResearchActivityApplyReport:
        """Incrementally rebuild only components connected to the supplied Events."""

        normalized = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in event_ids
                if str(item or "").strip()
            )
        )[:1000]
        report = ResearchActivityApplyReport(
            mode="incremental",
            input_event_count=len(normalized),
        )
        if not self.settings.activities_active or not normalized:
            return report
        events = self._load_incremental_context(normalized)
        report.context_event_count = len(events)
        if not events:
            return report
        context_ids = {event.event_id for event in events}
        previous = [
            item
            for item in ResearchActivityStore.list(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                limit=2000,
            )
            if context_ids.intersection(item.event_ids)
        ]
        affected_research: set[str] = set()
        for item in previous:
            if item.research_id:
                affected_research.add(item.research_id)
            affected_research.update(self._activity_scope_ids(item.activity_id))
        links = self._links_for_events(list(context_ids))
        activities = self._build(events, links)
        new_keys = {item.activity_key for item in activities}
        stale = [item for item in previous if item.activity_key not in new_keys]
        for item in stale:
            self._synchronize_activity_scopes(item.activity_id, [])
        report.removed_activity_ids = [item.activity_id for item in stale]
        report.removed_activity_keys = [item.activity_key for item in stale]
        report.removed_count = ResearchActivityStore.delete_keys(
            [item.activity_key for item in stale],
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        persisted, counts = ResearchActivityStore.upsert_many(
            activities,
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        for activity in persisted:
            research_ids = self._activity_research_ids(activity)
            affected_research.update(research_ids)
            self._synchronize_activity_scopes(activity.activity_id, research_ids)
        report.activities = persisted
        report.activity_count = len(persisted)
        report.created_count = counts["created"]
        report.updated_count = counts["updated"]
        report.unchanged_count = counts["unchanged"]
        report.affected_activity_ids = [item.activity_id for item in persisted]
        report.affected_research_ids = sorted(affected_research)[:3]
        try:
            from .pack_overlay import ResearchPackOverlayProjector

            projector = ResearchPackOverlayProjector(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            for activity in persisted:
                projector.project_activity(activity)
        except Exception:
            # Overlay is a repairable copy and must not mask committed Activity
            # changes or add latency through retries/model calls.
            pass
        return report

    def compare_projection(self, *, limit: int = 5000) -> dict[str, Any]:
        """Compare persisted incremental state with a maintenance rebuild in memory."""

        events = ResearchEventStore.list_events_for_attribution(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            limit=min(5000, max(1, limit)),
        )
        links = ResearchEventStore.list_attribution_links(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
            active_only=True,
            limit=5000,
        )
        expected = {
            item.activity_key: item.source_digest
            for item in self._build(events, {link.event_id: link for link in links})
        }
        actual = {
            item.activity_key: item.source_digest
            for item in ResearchActivityStore.list(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                limit=2000,
            )
        }
        return {
            "equal": expected == actual,
            "expected_count": len(expected),
            "actual_count": len(actual),
            "missing_keys": sorted(set(expected) - set(actual)),
            "stale_keys": sorted(set(actual) - set(expected)),
            "different_keys": sorted(
                key for key in set(expected) & set(actual) if expected[key] != actual[key]
            ),
        }

    def _load_incremental_context(self, event_ids: list[str]) -> list[ResearchEvent]:
        max_events = int(self.settings.activity_incremental_max_events)
        ResearchEventStore.ensure_initialized(self.workdir, settings=self.settings)
        conn = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            known_ids = set(event_ids)
            for activity in ResearchActivityStore.list(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
                limit=2000,
            ):
                if known_ids.intersection(activity.event_ids):
                    known_ids.update(activity.event_ids)
            events: dict[str, ResearchEvent] = {}

            def load_ids(values: set[str]) -> None:
                remaining = max_events - len(events)
                if remaining <= 0:
                    return
                normalized_values = list(values)[:remaining]
                for start in range(0, len(normalized_values), 400):
                    chunk = normalized_values[start : start + 400]
                    if not chunk:
                        continue
                    rows = conn.execute(
                        "SELECT * FROM research_events WHERE tenant_id=? AND user_id=? "
                        "AND workspace_id=? AND event_id IN ("
                        + ",".join("?" for _ in chunk)
                        + ")",
                        (*self.owner.values(), *chunk),
                    ).fetchall()
                    for row in rows:
                        event = ResearchEvent.from_row(row)
                        events[event.event_id] = event
                        if len(events) >= max_events:
                            return

            load_ids(known_ids)
            for _ in range(4):
                if not events or len(events) >= max_events:
                    break
                current = list(events.values())
                turns = list(dict.fromkeys(event.turn_id for event in current if event.turn_id))[:200]
                traces = list(dict.fromkeys(event.trace_id for event in current if event.trace_id))[:200]
                sources = list(dict.fromkeys(event.source_id for event in current if event.source_id))[:200]
                source_refs = list(dict.fromkeys(event.source_ref for event in current if event.source_ref))[:100]
                parent_ids = list(
                    dict.fromkeys(
                        value
                        for event in current
                        for value in (
                            event.event_id,
                            event.source_id,
                            event.parent_event_id,
                            str(event.payload.get("parent_id") or ""),
                        )
                        if value
                    )
                )[:400]
                child_sessions = list(
                    dict.fromkeys(
                        value for event in current for value in _child_session_ids(event) if value
                    )
                )[:100]
                clauses: list[str] = []
                params: list[Any] = [*self.owner.values()]

                def add(column: str, values: list[str]) -> None:
                    if not values:
                        return
                    clauses.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                    params.extend(values)

                add("turn_id", turns)
                add("trace_id", traces)
                add("source_id", sources)
                add("source_ref", source_refs)
                add("parent_event_id", parent_ids)
                add("session_id", child_sessions)
                if not clauses:
                    break
                params.append(max_events - len(events))
                rows = conn.execute(
                    "SELECT * FROM research_events WHERE tenant_id=? AND user_id=? AND workspace_id=? AND ("
                    + " OR ".join(clauses)
                    + ") ORDER BY occurred_at,ingested_at LIMIT ?",
                    params,
                ).fetchall()
                before = len(events)
                for row in rows:
                    event = ResearchEvent.from_row(row)
                    events[event.event_id] = event
                    if len(events) >= max_events:
                        break
                if len(events) == before:
                    break
        finally:
            conn.close()
        return sorted(
            events.values(),
            key=lambda item: (item.occurred_at, item.ingested_at, item.event_id),
        )

    def _links_for_events(self, event_ids: list[str]) -> dict[str, Any]:
        from .attribution_models import ResearchEventLink

        result: dict[str, Any] = {}
        conn = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            for start in range(0, len(event_ids), 400):
                chunk = event_ids[start : start + 400]
                if not chunk:
                    continue
                rows = conn.execute(
                    "SELECT * FROM research_event_links WHERE tenant_id=? AND user_id=? "
                    "AND workspace_id=? AND superseded_by='' AND event_id IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    (*self.owner.values(), *chunk),
                ).fetchall()
                for row in rows:
                    link = ResearchEventLink.from_row(row)
                    result[link.event_id] = link
        finally:
            conn.close()
        return result

    def _activity_research_ids(self, activity: ResearchActivity) -> list[str]:
        result = {str(activity.research_id or "").strip()} if activity.research_id else set()
        conn = ResearchEventStore._connect(self.workdir, settings=self.settings)
        try:
            for start in range(0, len(activity.event_ids), 400):
                chunk = activity.event_ids[start : start + 400]
                if not chunk:
                    continue
                rows = conn.execute(
                    "SELECT DISTINCT research_id FROM research_object_scopes WHERE tenant_id=? "
                    "AND user_id=? AND workspace_id=? AND object_type='event' AND state='active' "
                    "AND superseded_by='' AND object_id IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    (*self.owner.values(), *chunk),
                ).fetchall()
                result.update(str(row["research_id"] or "").strip() for row in rows)
        finally:
            conn.close()
        if activity.candidate_id:
            candidate = ResearchTopicStore.get_candidate(
                activity.candidate_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if candidate is not None and candidate.confirmed_research_id:
                result.add(candidate.confirmed_research_id)
        return sorted(item for item in result if item)[:3]

    def _activity_scope_ids(self, activity_id: str) -> list[str]:
        from .routing_store import ResearchRoutingStore

        return sorted(
            {
                scope.research_id
                for scope in ResearchRoutingStore.list_object_scopes(
                    **self.owner,
                    object_type="activity",
                    object_id=activity_id,
                    workdir=self.workdir,
                    settings=self.settings,
                    limit=20,
                )
                if scope.state == "active"
            }
        )[:3]

    def _synchronize_activity_scopes(
        self,
        activity_id: str,
        research_ids: list[str],
    ) -> None:
        from .routing_store import ResearchRoutingStore

        desired = set(research_ids)
        existing = ResearchRoutingStore.list_object_scopes(
            **self.owner,
            object_type="activity",
            object_id=activity_id,
            workdir=self.workdir,
            settings=self.settings,
            limit=20,
        )
        relation = "shared" if len(desired) > 1 else "primary"
        for research_id in sorted(desired):
            ResearchRoutingStore.link_object(
                **self.owner,
                object_type="activity",
                object_id=activity_id,
                research_id=research_id,
                relation=relation,
                confidence=1.0,
                state="active",
                workdir=self.workdir,
                settings=self.settings,
            )
        for scope in existing:
            if scope.research_id in desired or scope.state == "removed":
                continue
            ResearchRoutingStore.link_object(
                **self.owner,
                object_type="activity",
                object_id=activity_id,
                research_id=scope.research_id,
                relation=scope.relation,
                confidence=scope.confidence,
                state="removed",
                workdir=self.workdir,
                settings=self.settings,
            )

    def _build(self, events: list[ResearchEvent], link_by_event: dict[str, Any]) -> list[ResearchActivity]:
        if not events:
            return []
        union = _UnionFind(len(events))
        key_first: dict[str, int] = {}
        child_sessions = {item for event in events for item in _child_session_ids(event)}
        source_first: dict[str, int] = {}
        for index, event in enumerate(events):
            source_first.setdefault(event.event_id, index)
            if event.source_id:
                source_first.setdefault(event.source_id, index)
        for index, event in enumerate(events):
            keys: set[str] = set()
            if event.trace_id:
                keys.add(f"trace:{event.trace_id}")
            keys.update(f"call:{item}" for item in _call_ids(event))
            # A reused filename is not evidence that different research topics
            # performed one action. Explicit call/trace/child links still join.
            link = link_by_event.get(event.event_id)
            file_scope = str(link.research_id or "") if link else ""
            keys.update(f"file:{file_scope}:{item}" for item in _group_file_keys(event))
            if event.session_id in child_sessions:
                keys.add(f"child-session:{event.session_id}")
            keys.update(f"child-session:{item}" for item in _child_session_ids(event))
            parent_id = str(event.parent_event_id or event.payload.get("parent_id") or "").strip()
            if parent_id and parent_id in source_first:
                union.union(index, source_first[parent_id])
            for key in keys:
                previous = key_first.setdefault(key, index)
                union.union(index, previous)

        grouped: dict[int, list[ResearchEvent]] = defaultdict(list)
        for index, event in enumerate(events):
            grouped[union.find(index)].append(event)
        result: list[ResearchActivity] = []
        for group in grouped.values():
            group.sort(key=lambda item: (item.occurred_at, item.ingested_at, item.event_id))
            relevant = [
                event for event in group
                if (link := link_by_event.get(event.event_id)) is not None
                and link.relevance == "research" and link.state in {"pending", "included"}
            ]
            if not relevant:
                continue
            control = [event for event in group if _is_control(event)]
            semantic = [event for event in group if event not in control and self._semantic(event)]
            if not semantic:
                continue
            result.append(self._activity(group, semantic, control, link_by_event))
        return sorted(result, key=lambda item: (item.started_at, item.activity_id), reverse=True)

    @staticmethod
    def _semantic(event: ResearchEvent) -> bool:
        if _is_file(event) or _is_artifact(event):
            return True
        if event.event_type.startswith(("tool.completed", "tool.failed", "mcp.completed", "mcp.failed")):
            return True
        if event.event_type == "message.created":
            return bool(_event_text(event).strip())
        return event.actor_type == "user" and bool(_event_text(event).strip())

    def _activity(
        self,
        group: list[ResearchEvent],
        semantic: list[ResearchEvent],
        control: list[ResearchEvent],
        link_by_event: dict[str, Any],
    ) -> ResearchActivity:
        agent_calls = self._agent_calls(group)
        agent_by_session = {
            str(item.get("session_id") or ""): item
            for item in agent_calls
            if str(item.get("session_id") or "")
        }
        file_refs = self._file_refs(group, agent_by_session)
        tool_calls = self._tool_calls(group, agent_by_session)
        workflow_calls = self._workflow_calls(group, agent_by_session)
        artifact_refs = self._artifact_refs(group, agent_by_session)
        known_artifact_refs = {str(item.get("ref") or "") for item in artifact_refs}
        for workflow in workflow_calls:
            for ref in workflow.get("artifact_refs") or []:
                normalized_ref = str(ref or "").strip()
                if not normalized_ref or normalized_ref in known_artifact_refs:
                    continue
                known_artifact_refs.add(normalized_ref)
                event_ids = list(workflow.get("event_ids") or [])
                artifact_refs.append(
                    {
                        "artifact_id": _artifact_id(normalized_ref),
                        "ref": normalized_ref[:1000],
                        "event_id": event_ids[0] if event_ids else "",
                        "event_ids": event_ids,
                        "kind": "workflow_artifact",
                        "producer_agent_run_id": str(
                            workflow.get("producer_agent_run_id") or ""
                        ),
                        "producer_session_id": str(
                            workflow.get("producer_session_id") or ""
                        ),
                        "producer_tool_call_id": "",
                        "parent_assignment_id": str(
                            workflow.get("parent_assignment_id") or ""
                        ),
                        "workflow_run_id": str(workflow.get("run_id") or ""),
                    }
                )
        artifact_refs.sort(key=lambda item: str(item.get("ref") or ""))
        agent_runs = self._agent_runs(
            group,
            agent_calls=agent_calls,
            tool_calls=tool_calls,
            workflow_calls=workflow_calls,
            file_refs=file_refs,
            artifact_refs=artifact_refs,
        )
        artifact_groups = self._artifact_groups(
            group,
            artifact_refs,
            workdir=self.workdir,
        )
        candidate_ids = sorted({
            str(link_by_event[event.event_id].candidate_id)
            for event in group if event.event_id in link_by_event
            and str(link_by_event[event.event_id].candidate_id or "")
        })
        research_ids = sorted({
            str(link_by_event[event.event_id].research_id)
            for event in group if event.event_id in link_by_event
            and str(link_by_event[event.event_id].research_id or "")
        })
        pending_semantic = [
            event for event in semantic
            if (link := link_by_event.get(event.event_id)) is not None
            and link.relevance == "research" and link.state == "pending"
        ]
        included_semantic = [
            event for event in semantic
            if (link := link_by_event.get(event.event_id)) is not None
            and link.relevance == "research" and link.state == "included"
        ]
        review_state = "pending" if pending_semantic else "included" if included_semantic else "observed"
        status = self._status(group, tool_calls, agent_calls)
        activity_type = self._activity_type(file_refs, tool_calls, group)
        title = self._title(activity_type, group, semantic, file_refs, tool_calls)
        summary = self._summary(group, semantic, title)
        recommendation, confidence, reason_code = self._recommendation(
            status, candidate_ids, research_ids, file_refs, semantic
        )
        event_ids = [event.event_id for event in group]
        source_digest = sha256_text(canonical_json({
            "version": ACTIVITY_PROJECTION_VERSION,
            "events": [(event.event_id, event.content_hash) for event in group],
            "attribution": [
                (
                    event.event_id,
                    str(link.state or ""),
                    str(link.relevance or ""),
                    str(link.candidate_id or ""),
                    str(link.research_id or ""),
                    str(link.semantic_fingerprint or ""),
                )
                for event in group
                if (link := link_by_event.get(event.event_id)) is not None
            ],
        }))
        anchor = (
            next((item["sha256"] for item in file_refs if item.get("sha256")), "")
            or next((item["path"] for item in file_refs if item.get("path")), "")
            or next((event.trace_id for event in group if event.trace_id), "")
            or semantic[0].event_id
        )
        activity_key = sha256_text(canonical_json({**self.owner, "anchor": anchor}))
        activity_id = f"activity-{activity_key.split(':', 1)[-1][:24]}"
        now = utc_now_iso()
        return ResearchActivity(
            activity_id=activity_id,
            schema_version=ACTIVITY_SCHEMA_VERSION,
            **self.owner,
            activity_key=activity_key,
            activity_type=activity_type,
            title=title,
            summary=summary,
            status=status,
            review_state=review_state,
            recommendation=recommendation,
            confidence=confidence,
            reason_code=reason_code,
            candidate_id=candidate_ids[0] if len(candidate_ids) == 1 else "",
            research_id=research_ids[0] if len(research_ids) == 1 else "",
            event_ids=event_ids,
            semantic_event_ids=[event.event_id for event in semantic],
            control_event_ids=[event.event_id for event in control],
            session_ids=sorted({event.session_id for event in group if event.session_id}),
            trace_ids=sorted({event.trace_id for event in group if event.trace_id}),
            tool_calls=tool_calls,
            agent_calls=agent_calls,
            workflow_calls=workflow_calls,
            agent_runs=agent_runs,
            file_refs=file_refs,
            artifact_refs=artifact_refs,
            artifact_groups=artifact_groups,
            category_counts=self._category_counts(
                group, semantic, tool_calls, agent_calls, file_refs, artifact_groups
            ),
            started_at=group[0].occurred_at,
            ended_at=group[-1].occurred_at,
            source_digest=source_digest,
            rebuilt_at=now,
            idempotency_key=sha256_text(canonical_json({"activity_key": activity_key, "source_digest": source_digest})),
        )

    def _status(
        self,
        group: list[ResearchEvent],
        tool_calls: list[dict[str, Any]],
        agent_calls: list[dict[str, Any]],
    ) -> str:
        failed_tools = [item for item in tool_calls if item["state"] == "failed"]
        failed_agents = [
            item for item in agent_calls if item["state"] in {"failed", "partial"}
        ]
        issue_times = [
            _parse_time(str(item.get("ended_at") or item.get("started_at") or ""))
            for item in [*failed_tools, *failed_agents]
            if str(item.get("ended_at") or item.get("started_at") or "").strip()
        ]
        completed_times = [
            _parse_time(str(item.get("ended_at") or item.get("started_at") or ""))
            for item in [*tool_calls, *agent_calls]
            if item.get("state") == "completed"
            and str(item.get("ended_at") or item.get("started_at") or "").strip()
        ]
        answer_times = [
            _parse_time(event.occurred_at)
            for event in group
            if event.actor_type == "agent"
            and event.source_kind == "session_message"
            and _event_text(event).strip()
        ]
        recovered = bool(
            issue_times
            and completed_times
            and answer_times
            and max(completed_times) > max(issue_times)
            and max(answer_times) >= max(completed_times)
        )
        if failed_tools and not recovered:
            if (
                any(item["state"] == "partial" for item in agent_calls)
                and all(item.get("name") in {"assign_task", "task"} for item in failed_tools)
            ):
                return "partial"
            return "failed"
        if any(item["state"] == "failed" for item in agent_calls) and not recovered:
            return "failed"
        if any(item["state"] == "partial" for item in agent_calls) and not recovered:
            return "partial"
        if any(item["state"] == "waiting" for item in agent_calls):
            return "waiting"
        if any(item["state"] == "running" for item in [*tool_calls, *agent_calls]):
            latest = max(_parse_time(event.occurred_at) for event in group)
            age = (datetime.now(timezone.utc) - latest).total_seconds() / 60
            return "incomplete" if age >= self.settings.activity_incomplete_minutes else "running"
        if tool_calls or agent_calls:
            return "completed"
        return "observed"

    @staticmethod
    def _activity_type(file_refs: list[dict[str, Any]], tool_calls: list[dict[str, Any]], group: list[ResearchEvent]) -> str:
        names = " ".join(item.get("name", "") for item in tool_calls).lower()
        if "paper_library" in names or any(str(item.get("path", "")).lower().endswith(".pdf") for item in file_refs):
            return "paper_ingest" if "ingest" in names or "入库" in " ".join(_event_text(e) for e in group) else "paper_research"
        if file_refs:
            return "file_research"
        if any(event.source_kind == "subagent" or event.event_type.startswith("agent.") for event in group):
            return "subagent_task"
        if tool_calls:
            return "tool_run"
        return "research_conversation"

    @staticmethod
    def _paper_title(group: list[ResearchEvent]) -> str:
        titles: list[str] = []
        has_paper_result = any(
            str(event.payload.get("tool_name") or "").strip().lower()
            in _PAPER_RESULT_TOOL_NAMES
            and event.event_type in {
                "tool.completed", "mcp.completed", "tool.failed", "mcp.failed",
            }
            for event in group
        )
        for event in group:
            for key, value in _walk(event.payload):
                if key.lower() in {"paper_title", "document_title"} and isinstance(value, str):
                    title = _clean_text(value, 160)
                    if title:
                        titles.append(title)
            text = "\n".join(
                str(event.payload.get(key) or "")
                for key in ("output_preview", "content_preview")
            )
            titles.extend(
                _clean_text(match.group(1), 160)
                for match in _PAPER_TITLE_PATTERN.finditer(text)
                if _clean_text(match.group(1), 160)
            )
            # 完成论文工具后，Agent 最终答复常把规范题名写成独占一行的
            # Markdown 粗体标题，而不是 ``title:`` 字段。只在已经存在可信论文
            # 结果时读取这种格式，避免把普通聊天中的强调文字误当作论文题名。
            if has_paper_result and event.actor_type == "agent":
                titles.extend(
                    _clean_text(re.sub(r"[*_]+", "", match.group(1)), 160)
                    for match in _MARKDOWN_PAPER_HEADING_PATTERN.finditer(text)
                    if _clean_text(re.sub(r"[*_]+", "", match.group(1)), 160)
                )
            if _is_file(event) or _is_artifact(event):
                title = event.payload.get("title")
                if isinstance(title, str) and _clean_text(title):
                    titles.append(_clean_text(title, 160))
        distinct: list[str] = []
        for title in sorted(set(titles), key=len, reverse=True):
            lowered = title.casefold()
            if any(
                lowered in current.casefold() or current.casefold() in lowered
                for current in distinct
            ):
                continue
            distinct.append(title)
        return distinct[0] if len(distinct) == 1 else ""

    def _title(
        self,
        activity_type: str,
        group: list[ResearchEvent],
        semantic: list[ResearchEvent],
        file_refs: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> str:
        paper_title = self._paper_title(group)
        if paper_title:
            return paper_title
        file_name = next((_filename(item.get("path", "")) for item in file_refs if item.get("path")), "")
        if file_name and activity_type == "paper_ingest":
            return f"解析并入库 {file_name}"[:160]
        if file_name and activity_type == "paper_research":
            return f"研究论文 {file_name}"[:160]
        user_text = next(
            (_event_text(event) for event in semantic if event.actor_type == "user" and not _is_control(event)),
            "",
        )
        if user_text:
            first = user_text.splitlines()[0]
            first = re.sub(r"^(?:请|麻烦)?(?:帮我|协助我)?", "", first).strip()
            return _clean_text(first, 160) or "未命名研究活动"
        if file_name:
            return f"处理文件 {file_name}"[:160]
        substantive = next(
            (
                _event_text(event)
                for event in reversed(semantic)
                if event.source_kind not in {"tool_call", "mcp_tool"}
                and not event.event_type.startswith(("tool.", "mcp."))
                and _event_text(event).strip()
            ),
            "",
        )
        if substantive:
            return _clean_text(substantive.splitlines()[0], 160)
        names = [item.get("name", "") for item in tool_calls if item.get("name")]
        return ("执行研究工具" if names else "未命名研究活动")

    @staticmethod
    def _summary(group: list[ResearchEvent], semantic: list[ResearchEvent], title: str) -> str:
        user_text = next(
            (
                _event_text(event)
                for event in semantic
                if event.actor_type == "user"
                and event.source_kind == "session_message"
                and _event_text(event).strip()
            ),
            "",
        ) or next(
            (
                _event_text(event)
                for event in semantic
                if event.actor_type == "user" and _event_text(event).strip()
            ),
            "",
        )
        final_text = next(
            (str(event.payload.get("content_preview") or _event_text(event)) for event in reversed(semantic) if event.actor_type == "agent" and _event_text(event).strip()),
            "",
        )
        value = final_text or user_text
        if _clean_text(value) == title: return ""
        text = _INLINE_TECH_REF_PATTERN.sub(" ", str(value or "")).strip()
        if len(text) > 6000:
            boundary = text.rfind("\n", 0, 6000)
            text = text[:boundary if boundary > 0 else 6000] + "\n\n摘要过长，请到原始会话查看完整内容。"
        return text

    @staticmethod
    def _recommendation(
        status: str,
        candidate_ids: list[str],
        research_ids: list[str],
        file_refs: list[dict[str, Any]],
        semantic: list[ResearchEvent],
    ) -> tuple[str, float, str]:
        if status == "incomplete":
            return "review", 0.99, "incomplete_tool_record"
        if len(candidate_ids) == 1 or len(research_ids) == 1:
            return "include", 0.98, "single_existing_research_target"
        if file_refs and any(event.actor_type == "user" for event in semantic):
            return "include", 0.95, "user_file_research_activity"
        return "review", 0.75, "ambiguous_research_activity"

    @staticmethod
    def _producer_fields(
        event: ResearchEvent,
        agent_by_session: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        session_id = str(event.session_id or "").strip()
        agent = agent_by_session.get(session_id, {})
        return {
            "producer_session_id": session_id,
            "producer_agent_run_id": _agent_run_id(session_id),
            "parent_assignment_id": str(agent.get("assignment_id") or ""),
        }

    @classmethod
    def _file_refs(
        cls,
        group: list[ResearchEvent],
        agent_by_session: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for event in group:
            keys = _event_file_keys(event)
            direct_sha = str(event.payload.get("sha256") or "").strip().lower()
            direct_size = int(event.payload.get("size") or 0) if str(event.payload.get("size") or "").isdigit() else 0
            producer = cls._producer_fields(event, agent_by_session)
            call_id = str(event.payload.get("call_id") or "").strip()
            for key in keys:
                if key.startswith("path:"):
                    path = key.split(":", 1)[1]
                    # file_refs 是用户输入资料，不重复收录 Paper Library 和
                    # Task Runtime 的内部产物；完整路径仍在 artifact_refs 中。
                    if path.startswith(".tcode/") or "/.tcode/" in path:
                        continue
                    item = values.setdefault(
                        path,
                        {
                            "file_id": _file_id(path),
                            "path": path,
                            "filename": _filename(path),
                            "sha256": "",
                            "size": 0,
                            "operations": [],
                            "event_ids": [],
                            "producer_agent_run_ids": [],
                            "producer_session_ids": [],
                            "producer_tool_call_ids": [],
                        },
                    )
                    if direct_sha:
                        item["sha256"] = direct_sha
                    if direct_size:
                        item["size"] = direct_size
                    operation = str(event.payload.get("operation") or event.event_type or "").strip()
                    if operation and operation not in item["operations"]:
                        item["operations"].append(operation)
                    if event.event_id not in item["event_ids"]:
                        item["event_ids"].append(event.event_id)
                    for target, source in (
                        ("producer_agent_run_ids", producer["producer_agent_run_id"]),
                        ("producer_session_ids", producer["producer_session_id"]),
                        ("producer_tool_call_ids", call_id),
                    ):
                        if source and source not in item[target]:
                            item[target].append(source)
            if direct_sha and not keys:
                key = f"sha256:{direct_sha}"
                values.setdefault(
                    key,
                    {
                        "file_id": _file_id(key),
                        "path": "",
                        "filename": "",
                        "sha256": direct_sha,
                        "size": direct_size,
                        "operations": [],
                        "event_ids": [event.event_id],
                        "producer_agent_run_ids": [producer["producer_agent_run_id"]]
                        if producer["producer_agent_run_id"] else [],
                        "producer_session_ids": [producer["producer_session_id"]]
                        if producer["producer_session_id"] else [],
                        "producer_tool_call_ids": [call_id] if call_id else [],
                    },
                )
        # The same upload is commonly present as ``filename``, host path and
        # ``/workspace/upload`` path. Collapse those aliases into one visible file.
        merged: list[dict[str, Any]] = []
        ordered = sorted(
            values.values(),
            key=lambda item: (
                not str(item.get("path") or "").startswith("upload/"),
                -len(str(item.get("path") or "")),
            ),
        )
        for item in ordered:
            match = next(
                (
                    current
                    for current in merged
                    if (
                        item.get("sha256")
                        and current.get("sha256") == item.get("sha256")
                    )
                    or (
                        item.get("filename")
                        and current.get("filename") == item.get("filename")
                    )
                ),
                None,
            )
            if match is None:
                merged.append(dict(item))
                continue
            if len(str(item.get("path") or "")) > len(str(match.get("path") or "")):
                match["path"] = item.get("path") or match.get("path")
            match["filename"] = match.get("filename") or item.get("filename")
            match["sha256"] = match.get("sha256") or item.get("sha256")
            match["size"] = max(int(match.get("size") or 0), int(item.get("size") or 0))
            for key in (
                "operations",
                "event_ids",
                "producer_agent_run_ids",
                "producer_session_ids",
                "producer_tool_call_ids",
            ):
                match[key] = list(dict.fromkeys([*(match.get(key) or []), *(item.get(key) or [])]))
        return sorted(merged, key=lambda item: (item.get("path", ""), item.get("sha256", "")))

    @classmethod
    def _tool_calls(
        cls,
        group: list[ResearchEvent],
        agent_by_session: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        for event in group:
            if event.source_kind not in {"tool_call", "mcp_tool"} and not event.event_type.startswith(("tool.", "mcp.")):
                continue
            call_id = str(event.payload.get("call_id") or event.source_id or event.event_id)
            name = str(event.payload.get("tool_name") or event.payload.get("mcp_tool_name") or "")[:160]
            producer = cls._producer_fields(event, agent_by_session)
            item = calls.setdefault(
                call_id,
                {
                    "call_id": call_id,
                    "name": name,
                    "state": "running",
                    "event_ids": [],
                    "started_at": "",
                    "ended_at": "",
                    **producer,
                },
            )
            item["event_ids"].append(event.event_id)
            item["name"] = item["name"] or name
            for key, value in producer.items():
                item[key] = item.get(key) or value
            if event.event_type.endswith("failed") or event.payload.get("error_preview"):
                item["state"] = "failed"
                item["ended_at"] = event.occurred_at
            elif event.event_type.endswith("completed") or str(event.payload.get("state") or "") == "completed":
                item["state"] = "completed"
                item["ended_at"] = event.occurred_at
            elif event.event_type.endswith("started"):
                item["started_at"] = event.occurred_at
        return sorted(calls.values(), key=lambda item: (item.get("started_at", ""), item["call_id"]))

    @staticmethod
    def _agent_calls(group: list[ResearchEvent]) -> list[dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        assignment_context: dict[str, dict[str, str]] = {}
        for event in group:
            payload = event.payload or {}
            tool_name = str(payload.get("tool_name") or "").strip()
            if tool_name not in {"task", "assign_task"}:
                continue
            arguments = payload.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            call_id = str(payload.get("call_id") or event.source_id or "").strip()
            if not call_id:
                continue
            assignment_context[call_id] = {
                "name": str(
                    arguments.get("worker_name")
                    or arguments.get("subagent_type")
                    or arguments.get("agent")
                    or ""
                ).strip(),
                "task": str(
                    arguments.get("task")
                    or arguments.get("prompt")
                    or arguments.get("description")
                    or ""
                ).strip()[:1600],
            }
        for event in group:
            if event.source_kind != "subagent" and not event.event_type.startswith("agent."):
                continue
            payload = event.payload or {}
            session_id = str(payload.get("subagent_session_id") or "").strip()
            assignment_id = str(
                payload.get("assignment_id")
                or payload.get("task_id")
                or payload.get("parent_tool_call_id")
                or event.source_id
                or event.event_id
            ).strip()
            # ``agent.spawned`` commonly identifies the assignment by the
            # parent tool call while terminal events use the child task/session
            # id.  The child session is the stable lifecycle identity; otherwise
            # one delegation is projected as separate running/completed calls.
            identity = f"session:{session_id}" if session_id else f"assignment:{assignment_id}"
            parent_call_id = str(payload.get("parent_tool_call_id") or "").strip()
            parent_session_id = str(payload.get("parent_session_id") or event.session_id or "").strip()
            if parent_session_id == session_id:
                parent_session_id = ""
            context = assignment_context.get(parent_call_id, {})
            name = str(
                payload.get("subagent_name")
                or context.get("name")
                or event.actor_id
                or ""
            ).strip()
            if name == "subagent":
                name = ""
            item = calls.setdefault(
                identity,
                {
                    "assignment_id": assignment_id,
                    "session_id": session_id,
                    "agent_run_id": _agent_run_id(session_id or assignment_id),
                    "parent_session_id": parent_session_id,
                    "parent_agent_run_id": _agent_run_id(parent_session_id),
                    "parent_tool_call_id": parent_call_id,
                    "name": name,
                    "task": str(
                        payload.get("task") or context.get("task") or ""
                    ).strip()[:1600],
                    "state": "running",
                    "event_ids": [],
                    "started_at": "",
                    "ended_at": "",
                    "error": "",
                    "next_action": "",
                    "result_summary": str(payload.get("result_summary") or "")[:2000],
                    "completed_work": list(payload.get("completed_work") or [])[:64]
                    if isinstance(payload.get("completed_work"), list) else [],
                    "failed_work": list(payload.get("failed_work") or [])[:64]
                    if isinstance(payload.get("failed_work"), list) else [],
                    "review_verdict": str(payload.get("review_verdict") or "")[:1200],
                    "recovery_required": bool(payload.get("recovery_required")),
                },
            )
            item["event_ids"].append(event.event_id)
            if parent_call_id and str(item.get("assignment_id") or "") == session_id:
                item["assignment_id"] = parent_call_id
            item["session_id"] = item["session_id"] or session_id
            item["agent_run_id"] = item.get("agent_run_id") or _agent_run_id(session_id or assignment_id)
            item["parent_session_id"] = item.get("parent_session_id") or parent_session_id
            item["parent_agent_run_id"] = item.get("parent_agent_run_id") or _agent_run_id(parent_session_id)
            item["parent_tool_call_id"] = item.get("parent_tool_call_id") or parent_call_id
            item["name"] = item["name"] or name
            item["task"] = item["task"] or str(payload.get("task") or "").strip()[:1600]
            event_type = event.event_type.lower()
            payload_status = str(payload.get("status") or "").strip().lower()
            if event_type.endswith(("started", "spawned")):
                item["started_at"] = item["started_at"] or event.occurred_at
            elif event_type.endswith("completed"):
                item["state"] = "completed"
                item["ended_at"] = event.occurred_at
            elif event_type.endswith("partial") or payload_status == "partial":
                item["state"] = "partial"
                item["ended_at"] = event.occurred_at
            elif event_type.endswith("waiting") or payload_status in {
                "awaiting_input",
                "waiting_for_user",
                "needs_parent_input",
                "needs_user_input",
            }:
                item["state"] = "waiting"
            elif event_type.endswith("cancelled"):
                item["state"] = "cancelled"
                item["ended_at"] = event.occurred_at
            elif event_type.endswith("failed"):
                item["state"] = "failed"
                item["ended_at"] = event.occurred_at
            item["error"] = item["error"] or str(
                payload.get("error_preview") or payload.get("blocker") or ""
            ).strip()[:1200]
            item["next_action"] = item["next_action"] or str(
                payload.get("next_action") or ""
            ).strip()[:1200]
            item["result_summary"] = item.get("result_summary") or str(
                payload.get("result_summary") or ""
            ).strip()[:2000]
            if isinstance(payload.get("completed_work"), list):
                item["completed_work"] = list(payload.get("completed_work") or [])[:64]
            if isinstance(payload.get("failed_work"), list):
                item["failed_work"] = list(payload.get("failed_work") or [])[:64]
            item["review_verdict"] = item.get("review_verdict") or str(
                payload.get("review_verdict") or ""
            ).strip()[:1200]
            item["recovery_required"] = bool(
                item.get("recovery_required") or payload.get("recovery_required")
            )
        return sorted(
            calls.values(),
            key=lambda item: (item.get("started_at", ""), item["assignment_id"]),
        )

    @classmethod
    def _workflow_calls(
        cls,
        group: list[ResearchEvent],
        agent_by_session: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        for event in group:
            if event.source_kind not in {"workflow_run", "workflow_stage"} and not event.event_type.startswith("workflow."):
                continue
            payload = event.payload or {}
            run_id = str(payload.get("run_id") or event.source_id or "").split(":", 1)[0].strip()
            if not run_id:
                continue
            producer = cls._producer_fields(event, agent_by_session)
            item = calls.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "template_key": str(payload.get("template_key") or "")[:160],
                    "state": "running",
                    "event_ids": [],
                    "stages": [],
                    "artifact_refs": [],
                    "started_at": "",
                    "ended_at": "",
                    **producer,
                },
            )
            item["event_ids"].append(event.event_id)
            item["template_key"] = item["template_key"] or str(payload.get("template_key") or "")[:160]
            for key, value in producer.items():
                item[key] = item.get(key) or value
            event_type = event.event_type.lower()
            status = str(payload.get("status") or "").strip().lower()
            if event_type == "workflow.started":
                item["started_at"] = item["started_at"] or event.occurred_at
            elif event_type == "workflow.completed" or status == "completed":
                item["state"] = "completed"
                item["ended_at"] = event.occurred_at
            elif event_type == "workflow.failed" or status in {"failed", "cancelled", "terminated"}:
                item["state"] = "failed"
                item["ended_at"] = event.occurred_at
            if event.source_kind == "workflow_stage" or event_type.startswith("workflow.stage."):
                stage_id = str(payload.get("stage_id") or event.source_id or event.event_id).strip()
                attempt = max(0, int(payload.get("attempt") or 0))
                stage_key = f"{stage_id}:{attempt}"
                stage = next(
                    (candidate for candidate in item["stages"] if candidate.get("stage_key") == stage_key),
                    None,
                )
                if stage is None:
                    stage_state = (
                        "completed"
                        if event_type.endswith(".completed") or status == "committed"
                        else "failed"
                        if event_type.endswith(".failed") or status == "failed"
                        else "running"
                        if event_type.endswith(".started") or status == "running"
                        else status or event_type.rsplit(".", 1)[-1]
                    )
                    stage = {
                        "stage_key": stage_key,
                        "stage_id": stage_id,
                        "attempt": attempt,
                        "name": str(event.actor_id or "workflow-stage")[:160],
                        "state": stage_state,
                        "summary": str(payload.get("result_summary") or "")[:2000],
                        "error": str(payload.get("last_error") or "")[:1200],
                        "event_ids": [],
                        "artifact_refs": [],
                    }
                    item["stages"].append(stage)
                stage["event_ids"].append(event.event_id)
                refs = payload.get("artifact_refs") or {}
                ref_values = _strings(refs)
                stage["artifact_refs"] = list(dict.fromkeys([*stage["artifact_refs"], *ref_values]))[:200]
                item["artifact_refs"] = list(dict.fromkeys([*item["artifact_refs"], *ref_values]))[:500]
        for item in calls.values():
            item["stages"] = sorted(
                item["stages"],
                key=lambda stage: (stage.get("attempt", 0), stage.get("stage_id", "")),
            )
        return sorted(calls.values(), key=lambda item: (item.get("started_at", ""), item["run_id"]))

    @classmethod
    def _artifact_refs(
        cls,
        group: list[ResearchEvent],
        agent_by_session: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refs: dict[str, dict[str, Any]] = {}
        for event in group:
            if not _is_artifact(event):
                continue
            ref = str(event.source_ref or event.payload.get("path") or "").strip()
            if ref:
                producer = cls._producer_fields(event, agent_by_session)
                item = refs.setdefault(
                    ref,
                    {
                        "artifact_id": _artifact_id(ref),
                        "ref": ref[:1000],
                        "event_id": event.event_id,
                        "event_ids": [],
                        "kind": event.source_kind,
                        "producer_agent_run_id": producer["producer_agent_run_id"],
                        "producer_session_id": producer["producer_session_id"],
                        "producer_tool_call_id": str(event.payload.get("call_id") or ""),
                        "parent_assignment_id": producer["parent_assignment_id"],
                    },
                )
                item["event_ids"].append(event.event_id)
        return sorted(refs.values(), key=lambda item: item["ref"])

    @staticmethod
    def _agent_runs(
        group: list[ResearchEvent],
        *,
        agent_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        workflow_calls: list[dict[str, Any]],
        file_refs: list[dict[str, Any]],
        artifact_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        child_sessions = {
            str(item.get("session_id") or "")
            for item in agent_calls
            if str(item.get("session_id") or "")
        }
        sessions = sorted({str(event.session_id or "") for event in group if str(event.session_id or "")})
        calls_by_session = {
            str(item.get("session_id") or ""): item
            for item in agent_calls
            if str(item.get("session_id") or "")
        }
        runs: dict[str, dict[str, Any]] = {}
        for session_id in sessions:
            call = calls_by_session.get(session_id, {})
            parent_session_id = str(call.get("parent_session_id") or "")
            related = [event for event in group if event.session_id == session_id]
            session_event = next(
                (event for event in related if event.source_kind == "session"),
                None,
            )
            agent_name = str(call.get("name") or "").strip()
            if not agent_name and session_event is not None:
                agent_name = str(session_event.payload.get("agent_name") or "").strip()
            if not agent_name:
                agent_name = next(
                    (
                        str(event.actor_id or "").strip()
                        for event in related
                        if event.actor_type == "agent"
                        and event.source_kind not in {"subagent", "workflow_stage"}
                        and str(event.actor_id or "").strip()
                    ),
                    "",
                )
            if call:
                run_state = str(call.get("state") or "running")
            elif any(event.event_type in {"turn.failed", "turn.cancelled"} for event in related):
                run_state = "failed"
            elif any(event.event_type == "turn.started" for event in related) and not any(
                event.event_type == "turn.completed" for event in related
            ):
                run_state = "running"
            else:
                run_state = "completed"
            started_at = min((event.occurred_at for event in related), default="")
            ended_at = max((event.occurred_at for event in related), default="")
            run_id = _agent_run_id(session_id)
            runs[run_id] = {
                "agent_run_id": run_id,
                "session_id": session_id,
                "parent_agent_run_id": _agent_run_id(parent_session_id),
                "parent_session_id": parent_session_id,
                "assignment_id": str(call.get("assignment_id") or ""),
                "parent_tool_call_id": str(call.get("parent_tool_call_id") or ""),
                "name": agent_name or ("子 Agent" if session_id in child_sessions else "主 Agent"),
                "task": str(call.get("task") or "")[:1600],
                "state": run_state,
                "is_root": not bool(parent_session_id),
                "event_ids": [event.event_id for event in related],
                "child_agent_run_ids": [],
                "tool_call_ids": [],
                "workflow_run_ids": [],
                "file_ids": [],
                "artifact_ids": [],
                "started_at": str(call.get("started_at") or started_at),
                "ended_at": str(call.get("ended_at") or ended_at),
                "error": str(call.get("error") or "")[:1200],
                "next_action": str(call.get("next_action") or "")[:1200],
                "result_summary": str(call.get("result_summary") or "")[:2000],
                "completed_work": list(call.get("completed_work") or [])[:64],
                "failed_work": list(call.get("failed_work") or [])[:64],
                "review_verdict": str(call.get("review_verdict") or "")[:1200],
                "recovery_required": bool(call.get("recovery_required")),
            }
        # A lifecycle event can exist before the child emits its first own event.
        for call in agent_calls:
            run_id = str(call.get("agent_run_id") or "")
            if not run_id or run_id in runs:
                continue
            parent_session_id = str(call.get("parent_session_id") or "")
            runs[run_id] = {
                "agent_run_id": run_id,
                "session_id": str(call.get("session_id") or ""),
                "parent_agent_run_id": str(call.get("parent_agent_run_id") or ""),
                "parent_session_id": parent_session_id,
                "assignment_id": str(call.get("assignment_id") or ""),
                "parent_tool_call_id": str(call.get("parent_tool_call_id") or ""),
                "name": str(call.get("name") or "子 Agent"),
                "task": str(call.get("task") or "")[:1600],
                "state": str(call.get("state") or "running"),
                "is_root": False,
                "event_ids": list(call.get("event_ids") or []),
                "child_agent_run_ids": [],
                "tool_call_ids": [],
                "workflow_run_ids": [],
                "file_ids": [],
                "artifact_ids": [],
                "started_at": str(call.get("started_at") or ""),
                "ended_at": str(call.get("ended_at") or ""),
                "error": str(call.get("error") or "")[:1200],
                "next_action": str(call.get("next_action") or "")[:1200],
                "result_summary": str(call.get("result_summary") or "")[:2000],
                "completed_work": list(call.get("completed_work") or [])[:64],
                "failed_work": list(call.get("failed_work") or [])[:64],
                "review_verdict": str(call.get("review_verdict") or "")[:1200],
                "recovery_required": bool(call.get("recovery_required")),
            }
        for run in runs.values():
            parent = runs.get(str(run.get("parent_agent_run_id") or ""))
            if parent is not None:
                parent["child_agent_run_ids"].append(run["agent_run_id"])
        for item, id_key, list_key in (
            *[(item, "call_id", "tool_call_ids") for item in tool_calls],
            *[(item, "run_id", "workflow_run_ids") for item in workflow_calls],
        ):
            run = runs.get(str(item.get("producer_agent_run_id") or ""))
            value = str(item.get(id_key) or "")
            if run is not None and value and value not in run[list_key]:
                run[list_key].append(value)
        for item in file_refs:
            for run_id in item.get("producer_agent_run_ids") or []:
                run = runs.get(str(run_id or ""))
                value = str(item.get("file_id") or "")
                if run is not None and value and value not in run["file_ids"]:
                    run["file_ids"].append(value)
        for item in artifact_refs:
            run = runs.get(str(item.get("producer_agent_run_id") or ""))
            value = str(item.get("artifact_id") or "")
            if run is not None and value and value not in run["artifact_ids"]:
                run["artifact_ids"].append(value)
        return sorted(
            runs.values(),
            key=lambda item: (item.get("started_at", ""), item["agent_run_id"]),
        )

    @staticmethod
    def _artifact_groups(
        group: list[ResearchEvent],
        artifact_refs: list[dict[str, Any]],
        *,
        workdir: str | None = None,
    ) -> list[dict[str, Any]]:
        """按论文身份把多项实现产物投影为用户可读的文献产品。"""

        texts = [
            "\n".join(
                str(event.payload.get(key) or "")
                for key in ("output_preview", "content_preview")
            )
            for event in group
        ]
        combined_text = "\n".join(texts)
        doc_ids = {
            match.group(1).strip("`., ")
            for match in _PAPER_DOC_ID_PATTERN.finditer(combined_text)
        }
        doc_ids.update(
            match.group(1)
            for item in artifact_refs
            for match in _PAPER_PATH_DOC_ID_PATTERN.finditer(str(item.get("ref") or ""))
        )
        paper_tool_events = [
            event
            for event in group
            if str(event.payload.get("tool_name") or "").strip().lower()
            in _PAPER_RESULT_TOOL_NAMES
            and event.event_type in {"tool.completed", "mcp.completed", "tool.failed", "mcp.failed"}
        ]
        if not doc_ids and not paper_tool_events:
            return []

        def metrics_for_text(source_text: str) -> dict[str, int]:
            """只从同一论文的结构化输出中提取计数，避免跨论文串值。"""

            def max_metric(*patterns: str) -> int:
                """返回严格标签模式匹配到的最大非负整数。"""

                values: list[int] = []
                for pattern in patterns:
                    values.extend(
                        int(match.group(1))
                        for match in re.finditer(pattern, source_text, re.IGNORECASE)
                    )
                return max(values, default=0)

            return {
                "pages": max_metric(
                    r"page_count\s*[:：]\s*(\d+)",
                    r"(?:页数|页面数)\s*[:：]\s*(\d+)",
                ),
                "tables": max_metric(
                    r"table_count\s*[:：]\s*(\d+)",
                    r"表格数\s*[:：]\s*(\d+)",
                ),
                "page_images": max_metric(r"page_images?\s*[:：]\s*(\d+)"),
                "table_images": max_metric(r"table_images?\s*[:：]\s*(\d+)"),
                "figure_images": max_metric(r"figure_images?\s*[:：]\s*(\d+)"),
                "l1_passages": max_metric(
                    r"l1_passage_count\s*[:：]\s*(\d+)",
                    r"(\d+)\s*(?:个\s*)?(?:l1\s*)?passages?",
                    r"l1\s*(?:段落|passages?)\s*[:：]\s*(\d+)",
                ),
                "l2_facts": max_metric(
                    r"l2_fact_count\s*[:：]\s*(\d+)",
                    r"(\d+)\s*(?:个\s*)?(?:l2\s*)?facts?",
                    r"l2\s*(?:事实|facts?)\s*[:：]\s*(\d+)",
                ),
                "evidence_records": max_metric(
                    r"evidence_records?\s*[:：]\s*(\d+)",
                    r"(\d+)\s*(?:条\s*)?evidence records?",
                    r"证据记录\s*[:：]\s*(\d+)",
                ),
            }

        def identifiers_for_text(source_text: str) -> tuple[list[str], str]:
            """提取单篇论文输出中的 DOI 与 PMID。"""

            doi_matches = [
                match.group(0).rstrip(".,;")
                for match in re.finditer(
                    r"(?i)\b10\.\d{4,9}/[-._;()/:a-z0-9]+",
                    source_text,
                )
            ]
            pmid_match = re.search(
                r"(?im)(?:\*\*)?pmid(?:\*\*)?\s*[:：]\s*`?([0-9]+)",
                source_text,
            )
            return doi_matches, pmid_match.group(1) if pmid_match else ""

        def record_counts_for_refs(refs: list[dict[str, Any]]) -> dict[str, int]:
            """从工作区内已登记的 record.json 补齐被事件预览截断的计数。"""

            if not workdir:
                return {}
            workspace_root = Path(workdir).expanduser().resolve()
            record_refs = sorted(
                (
                    str(item.get("ref") or "")
                    for item in refs
                    if PurePosixPath(str(item.get("ref") or "")).name.lower()
                    == "record.json"
                ),
                reverse=True,
            )
            for ref in record_refs:
                candidate = Path(ref).expanduser()
                if not candidate.is_absolute():
                    candidate = workspace_root / candidate
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(workspace_root)
                except (OSError, ValueError):
                    continue
                if not resolved.is_file():
                    continue
                try:
                    payload = json.loads(resolved.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                metadata = dict(payload.get("metadata") or {})
                evidence_brain = dict(payload.get("evidence_brain") or {})
                return {
                    "pages": int(metadata.get("page_count") or 0),
                    "tables": int(metadata.get("table_count") or 0),
                    "page_images": int(metadata.get("page_image_count") or 0),
                    "table_images": int(metadata.get("table_image_count") or 0),
                    "figure_images": int(metadata.get("figure_image_count") or 0),
                    "l1_passages": len(list(payload.get("l1_passage_summaries") or [])),
                    "l2_facts": len(list(payload.get("l2_facts") or [])),
                    "evidence_records": int(evidence_brain.get("record_count") or 0),
                }
            return {}

        event_by_id = {event.event_id: event for event in group}
        results: list[dict[str, Any]] = []
        targets = sorted(doc_ids) or [""]
        for doc_id in targets:
            matched_refs = [
                item for item in artifact_refs
                if not doc_id or doc_id.lower() in str(item.get("ref") or "").lower()
            ]
            if not matched_refs and doc_id:
                continue
            doc_paper_tool_events = [
                event
                for event in paper_tool_events
                if not doc_id or doc_id.lower() in _event_text(event).lower()
            ]
            doc_context_events = [
                event
                for event in group
                if not doc_id or doc_id.lower() in _event_text(event).lower()
            ]
            metric_events = doc_paper_tool_events or doc_context_events
            metric_text = "\n".join(_event_text(event) for event in metric_events)
            if not metric_text and len(targets) == 1:
                metric_text = combined_text
            counts = metrics_for_text(metric_text)
            record_counts = record_counts_for_refs(matched_refs)
            counts = {
                key: value or int(record_counts.get(key) or 0)
                for key, value in counts.items()
            }
            identity_text = combined_text if len(targets) == 1 else metric_text
            doi_values, pmid = identifiers_for_text(identity_text)
            title = ResearchActivityBuilder._paper_title(
                group if len(targets) == 1 else (doc_context_events or group)
            )
            versions = sorted({
                match.group(1)
                for item in matched_refs
                for match in _PAPER_VERSION_PATTERN.finditer(str(item.get("ref") or ""))
            })
            latest_version = versions[-1] if versions else ""
            primary: dict[str, dict[str, Any]] = {}
            for item in matched_refs:
                ref = str(item.get("ref") or "")
                filename = PurePosixPath(ref).name.lower()
                definition = _PAPER_PRIMARY_ARTIFACTS.get(filename)
                if definition is None:
                    continue
                kind, label = definition
                current = primary.get(kind)
                item_version = next(
                    (match.group(1) for match in _PAPER_VERSION_PATTERN.finditer(ref)), ""
                )
                current_version = str((current or {}).get("version") or "")
                if current is None or item_version >= current_version:
                    primary[kind] = {
                        "kind": kind,
                        "label": label,
                        "ref": ref,
                        "event_id": item.get("event_id") or "",
                        "version": item_version,
                    }
            status = "failed" if any(
                event.event_type.endswith("failed")
                for event in doc_paper_tool_events
            ) else "completed"
            summary_parts = []
            if counts["l1_passages"]:
                summary_parts.append(f'{counts["l1_passages"]} 个 L1 段落')
            if counts["l2_facts"]:
                summary_parts.append(f'{counts["l2_facts"]} 个 L2 事实')
            if counts["evidence_records"]:
                summary_parts.append(f'{counts["evidence_records"]} 条证据记录')
            if counts["tables"]:
                summary_parts.append(f'{counts["tables"]} 张表格')
            image_count = counts["figure_images"] + counts["table_images"]
            if image_count:
                summary_parts.append(f"{image_count} 张研究图片")
            relevant_event_ids = [
                item.get("event_id") or ""
                for item in matched_refs
                if item.get("event_id") in event_by_id
            ]
            relevant_event_ids.extend(event.event_id for event in doc_paper_tool_events)
            results.append({
                "group_id": "artifact-group-" + sha256_text(
                    canonical_json({"type": "paper_record", "doc_id": doc_id or title})
                ).split(":", 1)[-1][:20],
                "group_type": "paper_record",
                "title": title or doc_id or "已入库论文",
                "summary": (
                    "已生成可复用的文献库记录"
                    + (f"，包含{'、'.join(summary_parts)}" if summary_parts else "")
                    + "。"
                ),
                "status": status,
                "doc_id": doc_id,
                "doi": doi_values[0] if doi_values else "",
                "pmid": pmid,
                "version_count": max(1, len(versions)),
                "latest_version": latest_version,
                "artifact_count": len(matched_refs),
                "audit_artifact_count": max(0, len(matched_refs) - len(primary)),
                "counts": counts,
                "primary_artifacts": sorted(primary.values(), key=lambda item: item["label"]),
                "event_ids": sorted(set(relevant_event_ids)),
                "updated_at": max(
                    (
                        event_by_id[event_id].occurred_at
                        for event_id in relevant_event_ids
                        if event_id in event_by_id
                    ),
                    default=group[-1].occurred_at,
                ),
            })
        return results

    @staticmethod
    def _category_counts(
        group: list[ResearchEvent],
        semantic: list[ResearchEvent],
        tool_calls: list[dict[str, Any]],
        agent_calls: list[dict[str, Any]],
        file_refs: list[dict[str, Any]],
        artifact_groups: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts = Counter(_category(event) for event in semantic)
        counts["tool"] = len(tool_calls)
        counts["file"] = len(file_refs)
        if artifact_groups:
            counts["artifact"] = len(artifact_groups)
        if agent_calls:
            counts["agent"] = len(agent_calls)
        return {key: int(value) for key, value in counts.items() if value > 0}
