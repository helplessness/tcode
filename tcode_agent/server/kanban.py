from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from ..sqlite_runtime import connect as sqlite_connect

from ..runtime.shared_tracker import SharedTracker


_LOCK = threading.RLock()
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_STATUS_PRIORITY = {
    "in_progress": 5,
    "failed": 4,
    "todo": 3,
    "cancelled": 2,
    "completed": 1,
}
_SOURCE_PRIORITY = {"async": 3, "subagent": 2, "todo": 1}


def kanban_poll_interval_seconds() -> int:
    try:
        value = int(float(os.environ.get("TCODE_KANBAN_POLL_INTERVAL_SECONDS", "8")))
    except (TypeError, ValueError):
        value = 8
    return max(2, min(value, 300))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_timestamp(value: Any) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _normalized_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def _short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _card_id(source: str, *parts: str) -> str:
    raw = "|".join([source, *[str(part or "") for part in parts]])
    return f"{source}:{_short_hash(raw, 24)}"


def _status_from_todo(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "in_progress":
        return "in_progress"
    if normalized == "completed":
        return "completed"
    if normalized == "cancelled":
        return "cancelled"
    return "todo"


def _status_from_tracker(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"claimed", "running", "retrying"}:
        return "in_progress"
    if normalized == "completed":
        return "completed"
    if normalized == "failed":
        return "failed"
    if normalized in {"cancelled", "duplicate"}:
        return "cancelled"
    return "todo"


def _status_from_subagent(
    session: Any,
    worker: Any,
    *,
    running: bool,
) -> str:
    if running or str(getattr(worker, "status", "") or "").strip().lower() == "busy":
        return "in_progress"

    persisted = str(getattr(session, "status", "") or "").strip().lower()
    if persisted == "failed":
        return "failed"
    if persisted in {"cancelled", "terminated"}:
        return "cancelled"
    if persisted == "completed":
        return "completed"

    messages = list(getattr(session, "messages", []) or [])
    has_assistant_result = any(
        str(getattr(message, "role", "") or "") == "assistant"
        and str(getattr(message, "content", "") or "").strip()
        for message in messages
    )
    worker_result = str(getattr(worker, "result_summary", "") or "").strip()
    worker_last_task = str(getattr(worker, "last_task", "") or "").strip()
    if has_assistant_result or worker_result or worker_last_task:
        return "completed"
    return "todo"


def _progress_for_status(status: str) -> tuple[int, str]:
    if status == "completed":
        return 100, "已完成 1/1"
    if status in {"failed", "cancelled"}:
        return 100, "已结束 1/1"
    if status == "in_progress":
        return 50, "已完成 0/1"
    return 0, "已完成 0/1"


def _root_session_id(session: Any, sessions_by_id: dict[str, Any]) -> str:
    current = session
    seen: set[str] = set()
    while current is not None:
        current_id = str(getattr(current, "id", "") or "")
        if not current_id or current_id in seen:
            return current_id
        seen.add(current_id)
        parent_id = str(getattr(current, "parent_id", "") or "")
        if not parent_id:
            return current_id
        current = sessions_by_id.get(parent_id)
    return str(getattr(session, "id", "") or "")


def _last_assistant_text(session: Any) -> str:
    for message in reversed(list(getattr(session, "messages", []) or [])):
        if str(getattr(message, "role", "") or "") != "assistant":
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if content:
            return content[:1200]
    return ""


def _subagent_task_title(session: Any, worker: Any) -> str:
    metadata = dict(getattr(session, "metadata", {}) or {})
    handoff = metadata.get("handoff") if isinstance(metadata.get("handoff"), dict) else {}
    contract = metadata.get("contract") if isinstance(metadata.get("contract"), dict) else {}
    candidates = [
        getattr(worker, "current_task", ""),
        handoff.get("task_prompt"),
        contract.get("goal"),
        getattr(worker, "last_task", ""),
        getattr(session, "title", ""),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _subagent_has_task(session: Any, worker: Any, title: str) -> bool:
    metadata = dict(getattr(session, "metadata", {}) or {})
    if metadata.get("handoff") or metadata.get("contract"):
        return True
    if worker is not None and any(
        str(getattr(worker, key, "") or "").strip()
        for key in ("current_task", "last_task", "result_summary")
    ):
        return True
    if list(getattr(session, "messages", []) or []):
        return True
    return bool(title and not title.lower().startswith("worker:"))


def _is_fixed_worker_shell(session: Any, worker: Any) -> bool:
    metadata = dict(getattr(session, "metadata", {}) or {})
    title = str(getattr(session, "title", "") or "").strip()
    if worker is not None and not bool(getattr(worker, "deletable", True)):
        return True
    return bool(
        title.startswith("[固定]")
        or metadata.get("team_worker_name")
        or metadata.get("resident_worker")
        or metadata.get("team_key")
    )


def _fixed_worker_is_active(worker: Any, *, running: bool) -> bool:
    if running:
        return True
    if worker is None:
        return False
    if str(getattr(worker, "status", "") or "").strip().lower() == "busy":
        return True
    return bool(str(getattr(worker, "current_task", "") or "").strip())


def _review_match_keys(review: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    task_id = str(review.get("task_id") or "").strip()
    if task_id:
        keys.add(f"task:{task_id}")
    review_data = (
        review.get("review_data") if isinstance(review.get("review_data"), dict) else {}
    )
    workspace = (
        review_data.get("workspace")
        if isinstance(review_data.get("workspace"), dict)
        else {}
    )
    workspace_task_id = str(workspace.get("task_id") or "").strip()
    if workspace_task_id:
        keys.add(f"task:{workspace_task_id}")
    issue_identifier = str(workspace.get("issue_identifier") or "").strip()
    if issue_identifier:
        keys.add(f"issue:{issue_identifier}")
    return keys


def _task_match_keys(task: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    if task_id:
        keys.add(f"task:{task_id}")
    identifier = str(task.get("identifier") or "").strip()
    if identifier:
        keys.add(f"issue:{identifier}")
    workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    workspace_task_id = str(workspace.get("task_id") or "").strip()
    if workspace_task_id:
        keys.add(f"task:{workspace_task_id}")
    issue_identifier = str(workspace.get("issue_identifier") or "").strip()
    if issue_identifier:
        keys.add(f"issue:{issue_identifier}")
    return keys


def _load_latest_reviews(workdirs: Iterable[str]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()
    for raw_path in workdirs:
        path = str(raw_path or "").strip()
        if not path:
            continue
        resolved = Path(path).expanduser().resolve(strict=False).as_posix()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        tracker_path = SharedTracker.db_path(path)
        legacy_path = Path(path) / "tmp" / "human_review_queue.json"
        if not tracker_path.is_file() and not legacy_path.is_file():
            continue
        try:
            store = SharedTracker.load_review_store(path)
        except Exception:
            continue
        reviews = store.get("reviews", []) if isinstance(store, dict) else []
        if not isinstance(reviews, list):
            continue
        for raw_review in reviews:
            if not isinstance(raw_review, dict):
                continue
            review = dict(raw_review)
            review_timestamp = str(
                review.get("updated_at")
                or review.get("reviewed_at")
                or review.get("created_at")
                or ""
            )
            for key in _review_match_keys(review):
                existing = latest.get(key)
                existing_timestamp = str(
                    (existing or {}).get("updated_at")
                    or (existing or {}).get("reviewed_at")
                    or (existing or {}).get("created_at")
                    or ""
                )
                if existing is None or review_timestamp >= existing_timestamp:
                    latest[key] = review
    return latest


def _review_for_task(
    task: dict[str, Any],
    reviews_by_key: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    task_workspace = (
        task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
    )
    task_sessions = {
        str(task.get("session_id") or "").strip(),
        str(task.get("parent_session_id") or "").strip(),
        str(task_workspace.get("session_id") or "").strip(),
        str(task_workspace.get("parent_session_id") or "").strip(),
    } - {""}
    candidates: list[dict[str, Any]] = []
    for key in _task_match_keys(task):
        review = reviews_by_key.get(key)
        if not review:
            continue
        review_data = (
            review.get("review_data")
            if isinstance(review.get("review_data"), dict)
            else {}
        )
        review_workspace = (
            review_data.get("workspace")
            if isinstance(review_data.get("workspace"), dict)
            else {}
        )
        scope_mismatch = any(
            str(task_workspace.get(scope_key) or "").strip()
            and str(review_workspace.get(scope_key) or "").strip()
            and str(task_workspace.get(scope_key) or "").strip()
            != str(review_workspace.get(scope_key) or "").strip()
            for scope_key in ("tenant_id", "user_id", "workspace_id")
        )
        if scope_mismatch:
            continue
        review_sessions = {
            str(review_data.get("session_id") or "").strip(),
            str(review_data.get("parent_session_id") or "").strip(),
            str(review_workspace.get("session_id") or "").strip(),
            str(review_workspace.get("parent_session_id") or "").strip(),
        } - {""}
        if task_sessions and review_sessions and task_sessions.isdisjoint(review_sessions):
            continue
        candidates.append(review)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: str(
            item.get("updated_at")
            or item.get("reviewed_at")
            or item.get("created_at")
            or ""
        ),
    )


def _dedupe_key(card: dict[str, Any]) -> str:
    title = _normalized_text(card.get("title"))
    session_scope = str(card.get("root_session_id") or card.get("session_id") or "")
    if not session_scope:
        return "|".join(
            [
                str(card.get("workspace_id") or ""),
                "unlinked",
                str(card.get("id") or ""),
            ]
        )
    return "|".join(
        [
            str(card.get("workspace_id") or ""),
            session_scope,
            title or str(card.get("id") or ""),
        ]
    )


def _merge_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in cards:
        card = dict(raw)
        key = _dedupe_key(card)
        card["dedupe_key"] = key
        card["source_ids"] = [str(card.get("id") or "")]
        existing = merged.get(key)
        if existing is None:
            merged[key] = card
            continue

        existing_source = str(existing.get("source_type") or "")
        next_source = str(card.get("source_type") or "")
        replace_primary = _SOURCE_PRIORITY.get(next_source, 0) > _SOURCE_PRIORITY.get(
            existing_source, 0
        )
        primary = card if replace_primary else existing
        secondary = existing if replace_primary else card
        combined = dict(primary)
        combined["source_ids"] = sorted(
            {
                *[str(item) for item in existing.get("source_ids", []) if str(item)],
                *[str(item) for item in card.get("source_ids", []) if str(item)],
            }
        )
        combined["sources"] = sorted(
            {
                str(existing.get("source_type") or ""),
                str(card.get("source_type") or ""),
            }
            - {""}
        )
        if _STATUS_PRIORITY.get(str(secondary.get("status") or ""), 0) > _STATUS_PRIORITY.get(
            str(combined.get("status") or ""), 0
        ):
            combined["status"] = secondary.get("status")
            combined["current_step"] = secondary.get("current_step")
            combined["progress_percent"] = secondary.get("progress_percent")
            combined["progress_label"] = secondary.get("progress_label")
        combined["agent"] = " / ".join(
            dict.fromkeys(
                [
                    str(primary.get("agent") or "").strip(),
                    str(secondary.get("agent") or "").strip(),
                ]
            )
        ).strip(" /")
        combined["result_summary"] = str(
            primary.get("result_summary") or secondary.get("result_summary") or ""
        )
        combined["error_summary"] = str(
            primary.get("error_summary") or secondary.get("error_summary") or ""
        )
        for field in (
            "workspace",
            "human_review",
            "tracker_status",
            "issue_identifier",
            "async_task_id",
        ):
            if not combined.get(field) and secondary.get(field):
                combined[field] = secondary.get(field)
        combined["created_at"] = min(
            [value for value in [primary.get("created_at"), secondary.get("created_at")] if value]
            or [""]
        )
        combined["updated_at"] = max(
            [value for value in [primary.get("updated_at"), secondary.get("updated_at")] if value]
            or [""]
        )
        combined["dedupe_key"] = key
        merged[key] = combined

    return sorted(
        merged.values(),
        key=lambda item: (
            _STATUS_PRIORITY.get(str(item.get("status") or ""), 0),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )


def build_kanban_cards(
    *,
    sessions: Iterable[Any],
    workdirs: Iterable[dict[str, Any]],
    review_workdirs: Optional[Iterable[str]] = None,
    worker_lookup: Optional[Callable[[str], Any]] = None,
    running_session_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    session_list = list(sessions)
    sessions_by_id = {
        str(getattr(session, "id", "") or ""): session
        for session in session_list
        if str(getattr(session, "id", "") or "")
    }
    running_ids = running_session_ids or set()
    cards: list[dict[str, Any]] = []
    workdir_list = list(workdirs)
    review_paths = [
        str(item.get("path") or "").strip()
        for item in workdir_list
        if str(item.get("path") or "").strip()
    ]
    review_paths.extend(str(item or "").strip() for item in (review_workdirs or []))
    reviews_by_key = _load_latest_reviews(review_paths)

    for session in session_list:
        session_id = str(getattr(session, "id", "") or "")
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        metadata = dict(getattr(session, "metadata", {}) or {})
        todos = metadata.get("todos") if isinstance(metadata.get("todos"), list) else []
        todo_owner = str(metadata.get("todos_owner") or getattr(session, "agent_name", "") or "")
        completed_count = sum(
            1 for item in todos if str((item or {}).get("status") or "").lower() == "completed"
        )
        total_count = len([item for item in todos if isinstance(item, dict)])
        current_todo = next(
            (
                str(item.get("content") or "").strip()
                for item in todos
                if isinstance(item, dict)
                and str(item.get("status") or "").lower() == "in_progress"
                and str(item.get("content") or "").strip()
            ),
            "",
        )
        occurrences: dict[str, int] = {}
        for item in todos:
            if not isinstance(item, dict):
                continue
            title = str(item.get("content") or "").strip()
            if not title:
                continue
            normalized_title = _normalized_text(title)
            occurrences[normalized_title] = occurrences.get(normalized_title, 0) + 1
            status = _status_from_todo(item.get("status"))
            progress = int(round((completed_count / max(1, total_count)) * 100))
            cards.append(
                {
                    "id": _card_id(
                        "todo",
                        session_id,
                        normalized_title,
                        str(occurrences[normalized_title]),
                    ),
                    "source_type": "todo",
                    "sources": ["todo"],
                    "title": title,
                    "status": status,
                    "agent": todo_owner or "agent",
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "root_session_id": _root_session_id(session, sessions_by_id),
                    "current_step": current_todo or title,
                    "progress_percent": progress,
                    "progress_label": f"已完成 {completed_count}/{max(1, total_count)}",
                    "created_at": _iso_from_timestamp(getattr(session, "created_at", 0)),
                    "updated_at": _iso_from_timestamp(getattr(session, "last_active_at", 0)),
                    "result_summary": "该步骤已完成" if status == "completed" else "",
                    "error_summary": "",
                }
            )

        parent_id = str(getattr(session, "parent_id", "") or "")
        if not parent_id:
            continue
        worker = worker_lookup(session_id) if worker_lookup else None
        if _is_fixed_worker_shell(session, worker) and not _fixed_worker_is_active(
            worker,
            running=session_id in running_ids,
        ):
            continue
        title = _subagent_task_title(session, worker)
        if not _subagent_has_task(session, worker, title):
            continue
        status = _status_from_subagent(
            session,
            worker,
            running=session_id in running_ids,
        )
        progress_percent, progress_label = _progress_for_status(status)
        metadata_contract = (
            metadata.get("contract") if isinstance(metadata.get("contract"), dict) else {}
        )
        current_step = str(
            getattr(worker, "current_task", "")
            or metadata_contract.get("stop_when")
            or title
        ).strip()
        cards.append(
            {
                "id": _card_id("subagent", session_id),
                "source_type": "subagent",
                "sources": ["subagent"],
                "title": title or "子 Agent 任务",
                "status": status,
                "agent": str(
                    getattr(worker, "name", "")
                    or getattr(session, "agent_name", "")
                    or "subagent"
                ),
                "workspace_id": workspace_id,
                "session_id": session_id,
                "root_session_id": _root_session_id(session, sessions_by_id),
                "current_step": current_step,
                "progress_percent": progress_percent,
                "progress_label": progress_label,
                "created_at": _iso_from_timestamp(getattr(session, "created_at", 0)),
                "updated_at": _iso_from_timestamp(getattr(session, "last_active_at", 0)),
                "result_summary": str(
                    getattr(worker, "result_summary", "") or _last_assistant_text(session)
                )[:1200],
                "error_summary": str(getattr(worker, "recent_failure_reason", "") or "")[:1200]
                if status == "failed"
                else "",
            }
        )

    for workdir in workdir_list:
        path = str(workdir.get("path") or "").strip()
        workspace_id = str(
            workdir.get("workspace_id") or workdir.get("name") or ""
        ).strip()
        if not path or not Path(path).is_dir():
            continue
        tracker_path = SharedTracker.db_path(path)
        legacy_path = Path(path) / "tmp" / "symphony_memory_issues.json"
        if not tracker_path.is_file() and not legacy_path.is_file():
            continue
        try:
            tasks = SharedTracker.list_tasks(path, limit=1000)
        except Exception:
            tasks = []
        for task in tasks:
            tracker_status = str(task.get("status") or "").strip().lower()
            review = _review_for_task(task, reviews_by_key)
            review_status = str((review or {}).get("status") or "").strip().lower()
            status = _status_from_tracker(
                "review_pending" if review_status == "pending_review" else tracker_status
            )
            progress_percent, progress_label = _progress_for_status(status)
            session_id = str(task.get("session_id") or "")
            parent_session_id = str(task.get("parent_session_id") or "")
            linked_session = sessions_by_id.get(session_id) or sessions_by_id.get(parent_session_id)
            root_session_id = (
                _root_session_id(linked_session, sessions_by_id) if linked_session else ""
            )
            agent = str(
                task.get("claim_owner")
                or task.get("tool_name")
                or task.get("tool_server")
                or "async-agent"
            )
            current_step = (
                "等待人工审核"
                if review_status == "pending_review"
                else {
                    "todo": "等待执行",
                    "in_progress": "后台任务执行中",
                    "completed": "任务已完成",
                    "failed": "任务执行失败",
                    "cancelled": "任务已取消",
                }.get(status, "")
            )
            workspace = (
                dict(task.get("workspace"))
                if isinstance(task.get("workspace"), dict)
                else {}
            )
            review_data = (
                review.get("review_data")
                if isinstance((review or {}).get("review_data"), dict)
                else {}
            )
            review_workspace = (
                review_data.get("workspace")
                if isinstance(review_data.get("workspace"), dict)
                else {}
            )
            workspace = {**review_workspace, **workspace}
            workspace.setdefault("workspace_id", workspace_id)
            workspace.setdefault("session_id", session_id)
            workspace.setdefault("parent_session_id", parent_session_id)
            workspace.setdefault("task_id", str(task.get("task_id") or ""))
            workspace.setdefault(
                "issue_identifier", str(task.get("identifier") or "")
            )
            if review:
                workspace["review_lineage"] = {
                    **(
                        workspace.get("review_lineage")
                        if isinstance(workspace.get("review_lineage"), dict)
                        else {}
                    ),
                    "id": str(review.get("id") or ""),
                    "identifier": str(review.get("identifier") or ""),
                    "status": str(review.get("status") or ""),
                }
                if review_status == "pending_review":
                    workspace["lifecycle_status"] = "review_pending"
            cards.append(
                {
                    "id": _card_id("async", workspace_id, str(task.get("task_id") or "")),
                    "source_type": "async",
                    "sources": ["async"],
                    "title": str(task.get("title") or task.get("description") or "异步任务")[
                        :300
                    ],
                    "status": status,
                    "agent": agent,
                    "workspace_id": workspace_id,
                    "session_id": session_id or parent_session_id,
                    "root_session_id": root_session_id,
                    "async_task_id": str(task.get("task_id") or ""),
                    "issue_identifier": str(task.get("identifier") or ""),
                    "tracker_status": tracker_status,
                    "execution_channel": str(
                        task.get("execution_channel")
                        or workspace.get("execution_channel")
                        or ""
                    ),
                    "claim_owner": str(
                        task.get("claim_owner")
                        or workspace.get("claim_owner")
                        or ""
                    ),
                    "workspace": workspace,
                    "human_review": review if review_status == "pending_review" else None,
                    "current_step": current_step,
                    "progress_percent": progress_percent,
                    "progress_label": progress_label,
                    "created_at": str(task.get("created_at") or ""),
                    "updated_at": str(task.get("updated_at") or ""),
                    "result_summary": str(task.get("result_summary") or "")[:1200],
                    "error_summary": str(task.get("error_summary") or "")[:1200],
                }
            )

    return _merge_cards(cards)


class KanbanHiddenStore:
    @classmethod
    def _db_path(cls, server_workdir: str) -> Path:
        return Path(server_workdir) / "tmp" / "kanban.sqlite"

    @classmethod
    def _connect(cls, server_workdir: str) -> sqlite3.Connection:
        path = cls._db_path(server_workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(path.as_posix(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kanban_hidden_cards (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                card_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL DEFAULT '',
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                hidden_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, card_id)
            )
            """
        )
        columns = {
            str(row["name"] or "")
            for row in conn.execute("PRAGMA table_info(kanban_hidden_cards)").fetchall()
        }
        if "workspace_id" not in columns:
            conn.execute(
                "ALTER TABLE kanban_hidden_cards ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
        return conn

    @classmethod
    def hidden_refs(
        cls, server_workdir: str, *, tenant_id: str, user_id: str
    ) -> set[str]:
        with _LOCK:
            conn = cls._connect(server_workdir)
            try:
                rows = conn.execute(
                    """
                    SELECT card_id, source_ids_json
                    FROM kanban_hidden_cards
                    WHERE tenant_id = ? AND user_id = ?
                    """,
                    (tenant_id, user_id),
                ).fetchall()
            finally:
                conn.close()
        ids: set[str] = set()
        for row in rows:
            ids.add(str(row["card_id"] or ""))
            try:
                source_ids = json.loads(str(row["source_ids_json"] or "[]"))
            except Exception:
                source_ids = []
            if isinstance(source_ids, list):
                ids.update(str(item) for item in source_ids if str(item))
        return ids

    @classmethod
    def hide(
        cls,
        server_workdir: str,
        *,
        tenant_id: str,
        user_id: str,
        card: dict[str, Any],
    ) -> None:
        status = str(card.get("status") or "")
        if status not in _TERMINAL_STATUSES:
            raise ValueError("只有已完成、失败或已取消的任务可以删除")
        with _LOCK:
            conn = cls._connect(server_workdir)
            try:
                conn.execute(
                    """
                    INSERT INTO kanban_hidden_cards (
                        tenant_id, user_id, workspace_id, card_id, dedupe_key,
                        source_ids_json, status, hidden_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, user_id, card_id) DO UPDATE SET
                        workspace_id = excluded.workspace_id,
                        dedupe_key = excluded.dedupe_key,
                        source_ids_json = excluded.source_ids_json,
                        status = excluded.status,
                        hidden_at = excluded.hidden_at
                    """,
                    (
                        tenant_id,
                        user_id,
                        str(card.get("workspace_id") or "").strip(),
                        str(card.get("id") or ""),
                        str(card.get("dedupe_key") or ""),
                        json.dumps(card.get("source_ids") or [], ensure_ascii=False),
                        status,
                        _utc_now_iso(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def delete_workspace_data(
        cls,
        server_workdir: str,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> int:
        with _LOCK:
            conn = cls._connect(server_workdir)
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM kanban_hidden_cards
                    WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                    """,
                    (
                        str(tenant_id or "").strip(),
                        str(user_id or "").strip(),
                        str(workspace_id or "").strip(),
                    ),
                )
                conn.commit()
                return int(cursor.rowcount or 0)
            finally:
                conn.close()

    @classmethod
    def delete_tenant_data(cls, server_workdir: str, *, tenant_id: str) -> int:
        with _LOCK:
            conn = cls._connect(server_workdir)
            try:
                cursor = conn.execute(
                    "DELETE FROM kanban_hidden_cards WHERE tenant_id = ?",
                    (str(tenant_id or "").strip(),),
                )
                conn.commit()
                return int(cursor.rowcount or 0)
            finally:
                conn.close()


def filter_hidden_cards(
    cards: Iterable[dict[str, Any]],
    *,
    hidden_ids: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for card in cards:
        source_ids = {str(item) for item in card.get("source_ids", []) if str(item)}
        if source_ids and source_ids.issubset(hidden_ids):
            continue
        if not source_ids and str(card.get("id") or "") in hidden_ids:
            continue
        result.append(card)
    return result


def filter_kanban_cards(
    cards: Iterable[dict[str, Any]],
    *,
    workspace_id: str = "",
    session_id: str = "",
) -> list[dict[str, Any]]:
    normalized_workspace = str(workspace_id or "").strip()
    normalized_session = str(session_id or "").strip()
    result: list[dict[str, Any]] = []
    for card in cards:
        if normalized_workspace and str(card.get("workspace_id") or "").strip() != normalized_workspace:
            continue
        if normalized_session:
            session_candidates = {
                str(card.get("session_id") or "").strip(),
                str(card.get("root_session_id") or "").strip(),
            }
            workspace = (
                card.get("workspace")
                if isinstance(card.get("workspace"), dict)
                else {}
            )
            session_candidates.update(
                {
                    str(workspace.get("session_id") or "").strip(),
                    str(workspace.get("parent_session_id") or "").strip(),
                }
            )
            if normalized_session not in session_candidates:
                continue
        result.append(card)
    return result


def kanban_counts(cards: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "todo": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
    }
    for card in cards:
        status = str(card.get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts
