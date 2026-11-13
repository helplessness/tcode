"""Shared Tracker backed by SQLite for async control-plane state."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from ..sqlite_runtime import connect as sqlite_connect

from ..workspace_lifecycle import assert_managed_workspace_creation_allowed


_LOCK = threading.RLock()

_TASK_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "duplicate"}
_TASK_STATUSES = {
    "queued",
    "review_pending",
    "claimed",
    "running",
    "completed",
    "failed",
    "cancelled",
    "duplicate",
    "retrying",
}
_WORKSPACE_OWNER_KEYS = (
    "tenant_id",
    "user_id",
    "workspace_id",
    "workspace_generation",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_json_text(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _parse_json_text(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
        if isinstance(default, dict) and not isinstance(parsed, dict):
            return default
        if isinstance(default, list) and not isinstance(parsed, list):
            return default
        return parsed
    except Exception:
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically replace a disposable JSON projection."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_workspace(
    current: Any,
    patch: Any,
) -> dict[str, Any]:
    workspace = dict(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return workspace
    for key, value in patch.items():
        if key in _WORKSPACE_OWNER_KEYS and str(workspace.get(key, "") or "").strip():
            continue
        workspace[key] = value
    return workspace


def tracker_status_to_issue_state(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in {"queued", "review_pending"}:
        return "Todo"
    if text in {"claimed", "running", "retrying"}:
        return "In Progress"
    if text == "completed":
        return "Done"
    if text == "failed":
        return "Failed"
    if text == "cancelled":
        return "Cancelled"
    if text == "duplicate":
        return "Duplicate"
    return "Todo"


def issue_state_to_tracker_status(state: Any) -> str:
    text = str(state or "").strip().lower()
    if text in {"todo", "queued"}:
        return "queued"
    if text in {"in progress", "running"}:
        return "running"
    if text in {"done", "closed", "completed"}:
        return "completed"
    if text in {"failed", "error"}:
        return "failed"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text == "duplicate":
        return "duplicate"
    return "queued"


def _preserve_terminal_status(existing_status: Any, next_status: Any, state_status: Any) -> str:
    current = str(existing_status or "").strip().lower()
    candidate = str(next_status or "").strip().lower()
    state_candidate = str(state_status or "").strip().lower()

    if state_candidate in _TASK_TERMINAL_STATUSES and state_candidate != candidate:
        return state_candidate
    if current in _TASK_TERMINAL_STATUSES and candidate not in _TASK_TERMINAL_STATUSES:
        return current
    return candidate


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _reconcile_orchestration_task(task: dict[str, Any], workdir: Optional[str]) -> None:
    try:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        orchestration = (
            metadata.get("orchestration")
            if isinstance(metadata.get("orchestration"), dict)
            else workspace.get("orchestration")
            if isinstance(workspace.get("orchestration"), dict)
            else {}
        )
        plan_id = str(orchestration.get("plan_id", "") or "").strip() if isinstance(orchestration, dict) else ""
        if not plan_id:
            return
        from ..task.orchestration import OrchestrationManager

        OrchestrationManager.reconcile_plan(plan_id, workdir)
    except Exception:
        pass


class SharedTracker:
    """SQLite-backed shared tracker for async control-plane state."""

    @staticmethod
    def _require_matching_claim_token(task: dict[str, Any], claim_token: str) -> None:
        provided = str(claim_token or "").strip()
        expected = str(task.get("claim_token", "") or "").strip()
        if not provided:
            raise ValueError("claim token is required")
        if not expected or not hmac.compare_digest(provided, expected):
            raise ValueError("claim token mismatch")

    @classmethod
    def bind_workspace_owner(
        cls,
        task: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workspace_generation: int | None = None,
    ) -> dict[str, Any]:
        payload = dict(task or {})
        workspace = (
            dict(payload.get("workspace"))
            if isinstance(payload.get("workspace"), dict)
            else {}
        )
        workspace.update(
            {
                "tenant_id": str(tenant_id or "").strip(),
                "user_id": str(user_id or "").strip(),
                "workspace_id": str(workspace_id or "").strip(),
            }
        )
        if workspace_generation is not None:
            workspace["workspace_generation"] = max(
                1, int(workspace_generation or 1)
            )
        payload["workspace"] = workspace
        return payload

    @classmethod
    def db_path(cls, workdir: Optional[str] = None) -> Path:
        configured = str(os.environ.get("TCODE_SHARED_TRACKER_DB_PATH", "") or "").strip()
        if configured:
            configured_path = Path(configured)
            server_root_text = str(
                os.environ.get("TCODE_WORKDIR", "") or ""
            ).strip()
            if workdir and server_root_text:
                try:
                    server_root = Path(server_root_text).expanduser().resolve(
                        strict=False
                    )
                    scoped_workdir = Path(workdir).expanduser().resolve(strict=False)
                    if scoped_workdir != server_root:
                        scoped_workdir.relative_to(server_root)
                        digest = hashlib.sha256(
                            scoped_workdir.as_posix().encode("utf-8")
                        ).hexdigest()[:16]
                        suffix = configured_path.suffix or ".sqlite"
                        stem = configured_path.stem or "shared_tracker"
                        return configured_path.with_name(f"{stem}-{digest}{suffix}")
                except ValueError:
                    pass
            return configured_path
        base = workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd()
        return Path(base) / "tmp" / "shared_tracker.sqlite"

    @classmethod
    def _connect(cls, workdir: Optional[str] = None) -> sqlite3.Connection:
        path = cls.db_path(workdir)
        if not path.exists():
            assert_managed_workspace_creation_allowed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(
            path.as_posix(),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @classmethod
    def ensure_initialized(cls, workdir: Optional[str] = None) -> Path:
        """初始化唯一 SQLite 权威存储，不再读取旧 JSON 数据。"""

        with _LOCK:
            conn = cls._connect(workdir)
            try:
                cls._create_schema(conn)
            finally:
                conn.close()
        return cls.db_path(workdir)

    @classmethod
    def delete_tenant_data(
        cls,
        tenant_id: str,
        *,
        workdir: Optional[str] = None,
    ) -> dict[str, int]:
        """Permanently remove central tracker rows belonging to one tenant."""

        normalized_tenant = str(tenant_id or "").strip()
        if not normalized_tenant:
            raise ValueError("tenant_id is required")
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                cls._create_schema(conn)
                task_rows = conn.execute(
                    "SELECT task_id, workspace_json FROM tracker_tasks"
                ).fetchall()
                task_ids = [
                    str(row["task_id"])
                    for row in task_rows
                    if str(
                        _parse_json_text(row["workspace_json"], {}).get(
                            "tenant_id", ""
                        )
                        or ""
                    ).strip()
                    == normalized_tenant
                ]
                review_rows = conn.execute(
                    "SELECT review_id, task_id, review_data_json FROM tracker_reviews"
                ).fetchall()
                review_ids = []
                task_id_set = set(task_ids)
                for row in review_rows:
                    review_data = _parse_json_text(row["review_data_json"], {})
                    workspace = (
                        review_data.get("workspace")
                        if isinstance(review_data.get("workspace"), dict)
                        else {}
                    )
                    review_tenant = str(
                        review_data.get("tenant_id")
                        or workspace.get("tenant_id")
                        or ""
                    ).strip()
                    if review_tenant == normalized_tenant or str(row["task_id"] or "") in task_id_set:
                        review_ids.append(str(row["review_id"]))

                conn.execute("BEGIN IMMEDIATE")
                deleted_reviews = 0
                if review_ids:
                    placeholders = ",".join("?" for _ in review_ids)
                    cursor = conn.execute(
                        f"DELETE FROM tracker_reviews WHERE review_id IN ({placeholders})",
                        tuple(review_ids),
                    )
                    deleted_reviews = int(cursor.rowcount or 0)
                deleted_tasks = 0
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    cursor = conn.execute(
                        f"DELETE FROM tracker_tasks WHERE task_id IN ({placeholders})",
                        tuple(task_ids),
                    )
                    deleted_tasks = int(cursor.rowcount or 0)
                conn.commit()
                return {
                    "tracker_tasks": deleted_tasks,
                    "tracker_reviews": deleted_reviews,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @classmethod
    def delete_workspace_data(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: Optional[str] = None,
    ) -> dict[str, int]:
        """Permanently remove tracker rows for one exact workspace owner."""

        owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if any(not value for value in owner.values()):
            raise ValueError("tenant_id, user_id and workspace_id are required")

        def matches(value: Any) -> bool:
            payload = _parse_json_text(value, {})
            workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else payload
            return all(
                str(workspace.get(key) or payload.get(key) or "").strip() == expected
                for key, expected in owner.items()
            )

        with _LOCK:
            conn = cls._connect(workdir)
            try:
                cls._create_schema(conn)
                task_rows = conn.execute(
                    "SELECT task_id, workspace_json FROM tracker_tasks"
                ).fetchall()
                task_ids = [
                    str(row["task_id"])
                    for row in task_rows
                    if matches(row["workspace_json"])
                ]
                task_id_set = set(task_ids)
                review_rows = conn.execute(
                    "SELECT review_id, task_id, review_data_json FROM tracker_reviews"
                ).fetchall()
                review_ids = [
                    str(row["review_id"])
                    for row in review_rows
                    if str(row["task_id"] or "") in task_id_set
                    or matches(row["review_data_json"])
                ]

                conn.execute("BEGIN IMMEDIATE")
                deleted_reviews = 0
                if review_ids:
                    placeholders = ",".join("?" for _ in review_ids)
                    cursor = conn.execute(
                        f"DELETE FROM tracker_reviews WHERE review_id IN ({placeholders})",
                        tuple(review_ids),
                    )
                    deleted_reviews = int(cursor.rowcount or 0)
                deleted_tasks = 0
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    cursor = conn.execute(
                        f"DELETE FROM tracker_tasks WHERE task_id IN ({placeholders})",
                        tuple(task_ids),
                    )
                    deleted_tasks = int(cursor.rowcount or 0)
                conn.commit()
                return {
                    "tracker_tasks": deleted_tasks,
                    "tracker_reviews": deleted_reviews,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @classmethod
    def _create_schema(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracker_tasks (
                task_id TEXT PRIMARY KEY,
                identifier TEXT UNIQUE,
                kind TEXT NOT NULL DEFAULT 'symphony_local_issue',
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                labels_json TEXT NOT NULL DEFAULT '[]',
                fingerprint TEXT NOT NULL DEFAULT '',
                tool_server TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                tool_args_json TEXT NOT NULL DEFAULT '{}',
                submit_input_json TEXT NOT NULL DEFAULT '{}',
                resume_data_json TEXT NOT NULL DEFAULT '{}',
                session_id TEXT NOT NULL DEFAULT '',
                parent_session_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                execution_channel TEXT NOT NULL DEFAULT 'symphony',
                claim_owner TEXT NOT NULL DEFAULT '',
                claim_token TEXT NOT NULL DEFAULT '',
                claim_expires_at TEXT NOT NULL DEFAULT '',
                claim_heartbeat_at TEXT NOT NULL DEFAULT '',
                workspace_json TEXT NOT NULL DEFAULT '{}',
                job_ref_json TEXT NOT NULL DEFAULT '{}',
                result_summary TEXT NOT NULL DEFAULT '',
                error_summary TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS tracker_task_events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tracker_tasks(task_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tracker_reviews (
                review_id TEXT PRIMARY KEY,
                identifier TEXT UNIQUE,
                task_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending_review',
                tool_server TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                tool_args_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL DEFAULT '',
                review_data_json TEXT NOT NULL DEFAULT '{}',
                approved_override_json TEXT NOT NULL DEFAULT '{}',
                resume_instruction TEXT NOT NULL DEFAULT '',
                execution_disposition TEXT NOT NULL DEFAULT '',
                comments TEXT NOT NULL DEFAULT '',
                rejection_reason TEXT NOT NULL DEFAULT '',
                resume_status TEXT NOT NULL DEFAULT '',
                resume_result_json TEXT NOT NULL DEFAULT '{}',
                reviewer TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS tracker_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tracker_tasks_status ON tracker_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tracker_tasks_channel_status ON tracker_tasks(execution_channel, status);
            CREATE INDEX IF NOT EXISTS idx_tracker_tasks_identifier ON tracker_tasks(identifier);
            CREATE INDEX IF NOT EXISTS idx_tracker_tasks_jobref ON tracker_tasks(task_id);
            CREATE INDEX IF NOT EXISTS idx_tracker_tasks_kind ON tracker_tasks(kind);
            CREATE INDEX IF NOT EXISTS idx_tracker_events_task ON tracker_task_events(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_tracker_reviews_status ON tracker_reviews(status);
            CREATE INDEX IF NOT EXISTS idx_tracker_reviews_task_id ON tracker_reviews(task_id);
            """
        )
        review_columns = {
            str(row["name"] or "")
            for row in conn.execute("PRAGMA table_info(tracker_reviews)").fetchall()
        }
        if "resume_instruction" not in review_columns:
            conn.execute(
                "ALTER TABLE tracker_reviews ADD COLUMN resume_instruction TEXT NOT NULL DEFAULT ''"
            )
        if "execution_disposition" not in review_columns:
            conn.execute(
                "ALTER TABLE tracker_reviews ADD COLUMN execution_disposition TEXT NOT NULL DEFAULT ''"
            )

    @classmethod
    def _projection_paths(cls, workdir: Optional[str]) -> dict[str, Path]:
        """返回供 Symphony 适配器消费的可丢弃 JSON 投影路径。"""

        base = Path(workdir or os.environ.get("TCODE_WORKDIR") or os.getcwd())
        return {
            "issues": base / "tmp" / "symphony_memory_issues.json",
            "reviews": base / "tmp" / "human_review_queue.json",
        }

    @classmethod
    def _extract_submit_input_from_description(cls, description: str) -> dict[str, Any]:
        marker = "submit_job.input:"
        idx = str(description or "").find(marker)
        if idx < 0:
            return {}
        text = str(description or "")[idx + len(marker) :]
        start = text.find("{")
        if start < 0:
            return {}
        text = text[start:]
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return {}
        return {}

    @classmethod
    def _derive_issue_fields(cls, issue: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """从当前 Issue 描述和工作区字段推导工具与提交参数。"""

        tool_server = ""
        tool_name = ""
        submit_input = cls._extract_submit_input_from_description(str(issue.get("description", "") or ""))
        if isinstance(submit_input, dict):
            tool_server = str(submit_input.get("tool") or submit_input.get("target_server") or "").strip()
            tool_name = str(submit_input.get("task_type") or submit_input.get("target_tool") or "").strip()
        workspace = issue.get("workspace")
        if isinstance(workspace, dict):
            tool_server = tool_server or str(workspace.get("tool", "") or "").strip()
            tool_name = tool_name or str(workspace.get("task_type", "") or "").strip()
        return tool_server, tool_name, submit_input

    @classmethod
    def _materialize_issue_store_conn(
        cls,
        conn: sqlite3.Connection,
        workdir: Optional[str],
        *,
        kind: str = "symphony_local_issue",
    ) -> None:
        """从 SQLite 生成供 Symphony 消费的单向、可重建 Issue 投影。"""

        path = cls._projection_paths(workdir)["issues"]
        rows = conn.execute(
            """
            SELECT * FROM tracker_tasks
            WHERE deleted_at = '' AND kind = ?
            ORDER BY created_at ASC
            """,
            (kind,),
        ).fetchall()
        issues = [cls._task_row_to_issue(_row_to_dict(row)) for row in rows]
        _atomic_write_json(
            path,
            {
                "_projection": {
                    "source": "shared_tracker.sqlite",
                    "version": 1,
                    "generated_at": _utc_now_iso(),
                    "authoritative": False,
                },
                "issues": issues,
            },
        )

    @classmethod
    def _task_row_to_issue(cls, row: dict[str, Any]) -> dict[str, Any]:
        workspace = _parse_json_text(row.get("workspace_json"), {})
        if not isinstance(workspace, dict):
            workspace = {}
        job_ref = _parse_json_text(row.get("job_ref_json"), {})
        resume_data = _parse_json_text(row.get("resume_data_json"), {})
        labels = _parse_json_text(row.get("labels_json"), [])
        metadata = _parse_json_text(row.get("metadata_json"), {})
        workspace.setdefault("task_id", str(row.get("task_id", "") or ""))
        workspace["execution_channel"] = str(row.get("execution_channel", "") or workspace.get("execution_channel", "") or "")
        workspace["claim_owner"] = str(row.get("claim_owner", "") or workspace.get("claim_owner", "") or "")
        workspace["job_ref"] = job_ref if isinstance(job_ref, dict) else {}
        workspace["tracker_status"] = str(row.get("status", "") or "")
        issue = {
            "id": str(row.get("task_id", "") or ""),
            "identifier": str(row.get("identifier", "") or ""),
            "title": str(row.get("title", "") or ""),
            "description": str(row.get("description", "") or ""),
            "state": tracker_status_to_issue_state(row.get("status")),
            "tracker_status": str(row.get("status", "") or ""),
            "labels": labels if isinstance(labels, list) else [],
            "created_at": str(row.get("created_at", "") or ""),
            "updated_at": str(row.get("updated_at", "") or ""),
            "workspace": workspace,
            "result_summary": str(row.get("result_summary", "") or ""),
            "error_summary": str(row.get("error_summary", "") or ""),
            "execution_channel": str(row.get("execution_channel", "") or ""),
            "claim_owner": str(row.get("claim_owner", "") or ""),
            "claim_expires_at": str(row.get("claim_expires_at", "") or ""),
            "claim_heartbeat_at": str(row.get("claim_heartbeat_at", "") or ""),
            "resume_data": resume_data if isinstance(resume_data, dict) else {},
            "fingerprint": str(row.get("fingerprint", "") or ""),
        }
        if isinstance(metadata, dict):
            for key in (
                "execution_pid",
                "cancel_actions",
                "last_turn_id",
                "auto_resume_status",
                "auto_resume_result",
                "auto_resume_token",
                "auto_resume_updated_at",
                "execution_started_at",
            ):
                if key in metadata:
                    issue[key] = metadata[key]
        return issue

    @classmethod
    def _task_row_to_task(cls, row: dict[str, Any]) -> dict[str, Any]:
        workspace = _parse_json_text(row.get("workspace_json"), {})
        if not isinstance(workspace, dict):
            workspace = {}
        job_ref = _parse_json_text(row.get("job_ref_json"), {})
        if not isinstance(job_ref, dict):
            job_ref = {}
        task = {
            "task_id": str(row.get("task_id", "") or ""),
            "id": str(row.get("task_id", "") or ""),
            "identifier": str(row.get("identifier", "") or ""),
            "kind": str(row.get("kind", "") or ""),
            "title": str(row.get("title", "") or ""),
            "description": str(row.get("description", "") or ""),
            "labels": _parse_json_text(row.get("labels_json"), []),
            "fingerprint": str(row.get("fingerprint", "") or ""),
            "tool_server": str(row.get("tool_server", "") or ""),
            "tool_name": str(row.get("tool_name", "") or ""),
            "tool_args": _parse_json_text(row.get("tool_args_json"), {}),
            "submit_input": _parse_json_text(row.get("submit_input_json"), {}),
            "resume_data": _parse_json_text(row.get("resume_data_json"), {}),
            "session_id": str(row.get("session_id", "") or ""),
            "parent_session_id": str(row.get("parent_session_id", "") or ""),
            "status": str(row.get("status", "") or ""),
            "tracker_status": str(row.get("status", "") or ""),
            "state": tracker_status_to_issue_state(row.get("status")),
            "execution_channel": str(row.get("execution_channel", "") or ""),
            "claim_owner": str(row.get("claim_owner", "") or ""),
            "claim_token": str(row.get("claim_token", "") or ""),
            "claim_expires_at": str(row.get("claim_expires_at", "") or ""),
            "claim_heartbeat_at": str(row.get("claim_heartbeat_at", "") or ""),
            "workspace": workspace,
            "job_ref": job_ref,
            "result_summary": str(row.get("result_summary", "") or ""),
            "error_summary": str(row.get("error_summary", "") or ""),
            "metadata": _parse_json_text(row.get("metadata_json"), {}),
            "created_at": str(row.get("created_at", "") or ""),
            "updated_at": str(row.get("updated_at", "") or ""),
            "deleted_at": str(row.get("deleted_at", "") or ""),
        }
        task["workspace"].setdefault("task_id", task["task_id"])
        task["workspace"]["execution_channel"] = str(
            task["execution_channel"] or task["workspace"].get("execution_channel") or ""
        )
        task["workspace"]["claim_owner"] = str(
            task["claim_owner"] or task["workspace"].get("claim_owner") or ""
        )
        task["workspace"]["job_ref"] = job_ref
        task["workspace"]["tracker_status"] = task["tracker_status"]
        return task

    @classmethod
    def _review_row_to_review(cls, row: dict[str, Any]) -> dict[str, Any]:
        review_data = _parse_json_text(row.get("review_data_json"), {})
        if not isinstance(review_data, dict):
            review_data = {}
        approved_override = _parse_json_text(row.get("approved_override_json"), {})
        if not isinstance(approved_override, dict):
            approved_override = {}
        resume_result = _parse_json_text(row.get("resume_result_json"), {})
        if not isinstance(resume_result, dict):
            resume_result = {}
        review = {
            "id": str(row.get("review_id", "") or ""),
            "identifier": str(row.get("identifier", "") or ""),
            "task_id": str(row.get("task_id", "") or ""),
            "status": str(row.get("status", "") or ""),
            "tool_server": str(row.get("tool_server", "") or ""),
            "tool_name": str(row.get("tool_name", "") or ""),
            "tool_args": _parse_json_text(row.get("tool_args_json"), {}),
            "reason": str(row.get("reason", "") or ""),
            "review_data": review_data,
            "approved_tool_args_override": approved_override if approved_override else None,
            "resume_instruction": str(row.get("resume_instruction", "") or ""),
            "execution_disposition": str(row.get("execution_disposition", "") or ""),
            "comments": str(row.get("comments", "") or ""),
            "rejection_reason": str(row.get("rejection_reason", "") or ""),
            "resume_status": str(row.get("resume_status", "") or ""),
            "resume_result": resume_result if resume_result else None,
            "reviewer": str(row.get("reviewer", "") or ""),
            "created_at": str(row.get("created_at", "") or ""),
            "updated_at": str(row.get("updated_at", "") or ""),
            "reviewed_at": str(row.get("reviewed_at", "") or ""),
        }
        return review

    @classmethod
    def _issue_to_task_record(cls, issue: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        existing = existing or {}
        existing_workspace = (
            existing.get("workspace")
            if isinstance(existing.get("workspace"), dict)
            else {}
        )
        incoming_workspace = (
            issue.get("workspace")
            if isinstance(issue.get("workspace"), dict)
            else {}
        )
        workspace = _merge_workspace(existing_workspace, incoming_workspace)
        resume_data = issue.get("resume_data")
        if not isinstance(resume_data, dict):
            resume_data = existing.get("resume_data") if isinstance(existing.get("resume_data"), dict) else {}
        metadata = dict(existing.get("metadata")) if isinstance(existing.get("metadata"), dict) else {}
        incoming_metadata = issue.get("metadata")
        if isinstance(incoming_metadata, dict) and incoming_metadata:
            metadata.update(incoming_metadata)
        for key in (
            "execution_pid",
            "cancel_actions",
            "last_turn_id",
            "auto_resume_status",
            "auto_resume_result",
            "auto_resume_token",
            "auto_resume_updated_at",
            "execution_started_at",
        ):
            if key in issue:
                metadata[key] = issue.get(key)
        submit_input = cls._extract_submit_input_from_description(str(issue.get("description", "") or ""))
        if not submit_input and isinstance(existing.get("submit_input"), dict):
            submit_input = existing["submit_input"]
        tool_server = str(issue.get("tool_server", "") or existing.get("tool_server", "") or "").strip()
        tool_name = str(issue.get("tool_name", "") or existing.get("tool_name", "") or "").strip()
        if not tool_server or not tool_name:
            derived_server, derived_tool, derived_submit = cls._derive_issue_fields(issue)
            tool_server = tool_server or derived_server
            tool_name = tool_name or derived_tool
            if not submit_input:
                submit_input = derived_submit
        state_status = issue_state_to_tracker_status(issue.get("state"))
        status = str(issue.get("tracker_status") or issue.get("status") or "").strip().lower()
        if status not in _TASK_STATUSES:
            status = state_status
        status = _preserve_terminal_status(existing.get("status"), status, state_status)
        execution_channel = str(
            issue.get("execution_channel")
            or workspace.get("execution_channel")
            or existing.get("execution_channel")
            or "symphony"
        ).strip().lower() or "symphony"
        if execution_channel not in {"symphony", "bridge"}:
            execution_channel = "symphony"
        task = {
            "task_id": str(
                issue.get("id", "")
                or issue.get("task_id", "")
                or existing.get("task_id")
                or f"local-{uuid.uuid4().hex[:10]}"
            ),
            "identifier": str(issue.get("identifier", "") or existing.get("identifier") or ""),
            "kind": str(issue.get("kind", "") or existing.get("kind") or "symphony_local_issue"),
            "title": str(issue.get("title", "") or existing.get("title") or ""),
            "description": str(issue.get("description", "") or existing.get("description") or ""),
            "labels": issue.get("labels") if isinstance(issue.get("labels"), list) else existing.get("labels") or [],
            "fingerprint": str(issue.get("fingerprint", "") or existing.get("fingerprint") or ""),
            "tool_server": tool_server,
            "tool_name": tool_name,
            "tool_args": issue.get("tool_args") if isinstance(issue.get("tool_args"), dict) else existing.get("tool_args") or workspace.get("input_snapshot") if isinstance(workspace.get("input_snapshot"), dict) else {},
            "submit_input": submit_input,
            "resume_data": resume_data,
            "session_id": str(
                issue.get("session_id")
                or resume_data.get("session_id")
                or workspace.get("session_id")
                or existing.get("session_id")
                or ""
            ),
            "parent_session_id": str(
                issue.get("parent_session_id")
                or resume_data.get("parent_session_id")
                or workspace.get("parent_session_id")
                or existing.get("parent_session_id")
                or ""
            ),
            "status": status,
            "execution_channel": execution_channel,
            "workspace": workspace,
            "job_ref": workspace.get("job_ref") if isinstance(workspace.get("job_ref"), dict) else existing.get("job_ref") or {},
            "result_summary": str(issue.get("result_summary", "") or existing.get("result_summary") or ""),
            "error_summary": str(issue.get("error_summary", "") or existing.get("error_summary") or ""),
            "metadata": metadata,
            "created_at": str(issue.get("created_at", "") or existing.get("created_at") or _utc_now_iso()),
            "updated_at": str(issue.get("updated_at", "") or _utc_now_iso()),
            "claim_owner": str(issue.get("claim_owner", "") or existing.get("claim_owner") or ""),
            "claim_token": str(existing.get("claim_token", "") or ""),
            "claim_expires_at": str(issue.get("claim_expires_at", "") or existing.get("claim_expires_at") or ""),
            "claim_heartbeat_at": str(existing.get("claim_heartbeat_at", "") or ""),
            "deleted_at": str(existing.get("deleted_at", "") or ""),
        }
        return task

    @classmethod
    def _review_to_record(cls, review: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        existing = existing or {}
        return {
            "review_id": str(review.get("id", "") or existing.get("review_id") or f"review-{uuid.uuid4().hex[:10]}"),
            "identifier": str(review.get("identifier", "") or existing.get("identifier") or ""),
            "task_id": str(review.get("task_id", "") or existing.get("task_id") or ""),
            "status": str(review.get("status", "") or existing.get("status") or "pending_review"),
            "tool_server": str(review.get("tool_server", "") or existing.get("tool_server") or ""),
            "tool_name": str(review.get("tool_name", "") or existing.get("tool_name") or ""),
            "tool_args": review.get("tool_args") if isinstance(review.get("tool_args"), dict) else existing.get("tool_args") or {},
            "reason": str(review.get("reason", "") or existing.get("reason") or ""),
            "review_data": review.get("review_data") if isinstance(review.get("review_data"), dict) else existing.get("review_data") or {},
            "approved_override": review.get("approved_tool_args_override") if isinstance(review.get("approved_tool_args_override"), dict) else existing.get("approved_override") or {},
            "resume_instruction": str(review.get("resume_instruction", "") or existing.get("resume_instruction") or ""),
            "execution_disposition": str(review.get("execution_disposition", "") or existing.get("execution_disposition") or ""),
            "comments": str(review.get("comments", "") or existing.get("comments") or ""),
            "rejection_reason": str(review.get("rejection_reason", "") or existing.get("rejection_reason") or ""),
            "resume_status": str(review.get("resume_status", "") or existing.get("resume_status") or ""),
            "resume_result": review.get("resume_result") if isinstance(review.get("resume_result"), dict) else existing.get("resume_result") or {},
            "reviewer": str(review.get("reviewer", "") or existing.get("reviewer") or ""),
            "created_at": str(review.get("created_at", "") or existing.get("created_at") or _utc_now_iso()),
            "updated_at": str(review.get("updated_at", "") or _utc_now_iso()),
            "reviewed_at": str(review.get("reviewed_at", "") or existing.get("reviewed_at") or ""),
        }

    @classmethod
    def _get_existing_task(cls, conn: sqlite3.Connection, task_id: str = "", identifier: str = "") -> dict[str, Any]:
        row = None
        if task_id:
            row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
        if row is None and identifier:
            row = conn.execute("SELECT * FROM tracker_tasks WHERE identifier = ? LIMIT 1", (identifier,)).fetchone()
        payload = _row_to_dict(row)
        if not payload:
            return {}
        payload["labels"] = _parse_json_text(payload.get("labels_json"), [])
        payload["tool_args"] = _parse_json_text(payload.get("tool_args_json"), {})
        payload["submit_input"] = _parse_json_text(payload.get("submit_input_json"), {})
        payload["resume_data"] = _parse_json_text(payload.get("resume_data_json"), {})
        payload["workspace"] = _parse_json_text(payload.get("workspace_json"), {})
        payload["job_ref"] = _parse_json_text(payload.get("job_ref_json"), {})
        payload["metadata"] = _parse_json_text(payload.get("metadata_json"), {})
        return payload

    @classmethod
    def _get_existing_review(cls, conn: sqlite3.Connection, review_id: str = "", identifier: str = "") -> dict[str, Any]:
        row = None
        if review_id:
            row = conn.execute("SELECT * FROM tracker_reviews WHERE review_id = ? LIMIT 1", (review_id,)).fetchone()
        if row is None and identifier:
            row = conn.execute("SELECT * FROM tracker_reviews WHERE identifier = ? LIMIT 1", (identifier,)).fetchone()
        payload = _row_to_dict(row)
        if not payload:
            return {}
        payload["tool_args"] = _parse_json_text(payload.get("tool_args_json"), {})
        payload["review_data"] = _parse_json_text(payload.get("review_data_json"), {})
        payload["approved_override"] = _parse_json_text(payload.get("approved_override_json"), {})
        payload["resume_result"] = _parse_json_text(payload.get("resume_result_json"), {})
        return payload

    @classmethod
    def _upsert_task(
        cls,
        conn: sqlite3.Connection,
        task: dict[str, Any],
        *,
        source: str = "app",
        append_event: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        existing = cls._get_existing_task(
            conn, task_id=str(task.get("task_id", "") or ""), identifier=str(task.get("identifier", "") or "")
        )
        created = not existing
        record = cls._issue_to_task_record(task, existing=existing)
        record["updated_at"] = str(task.get("updated_at", "") or now)
        if created:
            record["created_at"] = str(task.get("created_at", "") or now)

        conn.execute(
            """
            INSERT INTO tracker_tasks (
                task_id, identifier, kind, title, description, labels_json, fingerprint,
                tool_server, tool_name, tool_args_json, submit_input_json, resume_data_json,
                session_id, parent_session_id, status, execution_channel,
                claim_owner, claim_token, claim_expires_at, claim_heartbeat_at,
                workspace_json, job_ref_json, result_summary, error_summary, metadata_json,
                created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                identifier=excluded.identifier,
                kind=excluded.kind,
                title=excluded.title,
                description=excluded.description,
                labels_json=excluded.labels_json,
                fingerprint=excluded.fingerprint,
                tool_server=excluded.tool_server,
                tool_name=excluded.tool_name,
                tool_args_json=excluded.tool_args_json,
                submit_input_json=excluded.submit_input_json,
                resume_data_json=excluded.resume_data_json,
                session_id=excluded.session_id,
                parent_session_id=excluded.parent_session_id,
                status=excluded.status,
                execution_channel=excluded.execution_channel,
                claim_owner=CASE WHEN excluded.claim_owner != '' THEN excluded.claim_owner ELSE tracker_tasks.claim_owner END,
                claim_token=CASE WHEN excluded.claim_token != '' THEN excluded.claim_token ELSE tracker_tasks.claim_token END,
                claim_expires_at=CASE WHEN excluded.claim_expires_at != '' THEN excluded.claim_expires_at ELSE tracker_tasks.claim_expires_at END,
                claim_heartbeat_at=CASE WHEN excluded.claim_heartbeat_at != '' THEN excluded.claim_heartbeat_at ELSE tracker_tasks.claim_heartbeat_at END,
                workspace_json=excluded.workspace_json,
                job_ref_json=excluded.job_ref_json,
                result_summary=excluded.result_summary,
                error_summary=excluded.error_summary,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at,
                deleted_at=excluded.deleted_at
            """,
            (
                record["task_id"],
                record["identifier"],
                record["kind"],
                record["title"],
                record["description"],
                _normalize_json_text(record["labels"]),
                record["fingerprint"],
                record["tool_server"],
                record["tool_name"],
                _normalize_json_text(record["tool_args"]),
                _normalize_json_text(record["submit_input"]),
                _normalize_json_text(record["resume_data"]),
                record["session_id"],
                record["parent_session_id"],
                record["status"],
                record["execution_channel"],
                record["claim_owner"],
                record["claim_token"],
                record["claim_expires_at"],
                record["claim_heartbeat_at"],
                _normalize_json_text(record["workspace"]),
                _normalize_json_text(record["job_ref"]),
                record["result_summary"],
                record["error_summary"],
                _normalize_json_text(record["metadata"]),
                record["created_at"],
                record["updated_at"],
                record["deleted_at"],
            ),
        )
        if append_event:
            cls._append_event_conn(
                conn,
                task_id=record["task_id"],
                kind="created" if created else "updated",
                source=source,
                payload={
                    "status": record["status"],
                    "execution_channel": record["execution_channel"],
                    "identifier": record["identifier"],
                },
            )
        return record

    @classmethod
    def _upsert_review(cls, conn: sqlite3.Connection, review: dict[str, Any]) -> dict[str, Any]:
        existing = cls._get_existing_review(
            conn, review_id=str(review.get("review_id", "") or ""), identifier=str(review.get("identifier", "") or "")
        )
        record = cls._review_to_record(review, existing=existing)
        conn.execute(
            """
            INSERT INTO tracker_reviews (
                review_id, identifier, task_id, status, tool_server, tool_name, tool_args_json,
                reason, review_data_json, approved_override_json, resume_instruction,
                execution_disposition, comments, rejection_reason, resume_status,
                resume_result_json, reviewer, created_at, updated_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_id) DO UPDATE SET
                identifier=excluded.identifier,
                task_id=excluded.task_id,
                status=excluded.status,
                tool_server=excluded.tool_server,
                tool_name=excluded.tool_name,
                tool_args_json=excluded.tool_args_json,
                reason=excluded.reason,
                review_data_json=excluded.review_data_json,
                approved_override_json=excluded.approved_override_json,
                resume_instruction=excluded.resume_instruction,
                execution_disposition=excluded.execution_disposition,
                comments=excluded.comments,
                rejection_reason=excluded.rejection_reason,
                resume_status=excluded.resume_status,
                resume_result_json=excluded.resume_result_json,
                reviewer=excluded.reviewer,
                updated_at=excluded.updated_at,
                reviewed_at=excluded.reviewed_at
            """,
            (
                record["review_id"],
                record["identifier"],
                record["task_id"],
                record["status"],
                record["tool_server"],
                record["tool_name"],
                _normalize_json_text(record["tool_args"]),
                record["reason"],
                _normalize_json_text(record["review_data"]),
                _normalize_json_text(record["approved_override"]),
                record["resume_instruction"],
                record["execution_disposition"],
                record["comments"],
                record["rejection_reason"],
                record["resume_status"],
                _normalize_json_text(record["resume_result"]),
                record["reviewer"],
                record["created_at"],
                record["updated_at"],
                record["reviewed_at"],
            ),
        )
        return record

    @classmethod
    def _append_event_conn(
        cls,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        kind: str,
        source: str,
        payload: Optional[dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> dict[str, Any]:
        event_id = f"event-{uuid.uuid4().hex[:12]}"
        when = str(created_at or _utc_now_iso())
        conn.execute(
            """
            INSERT INTO tracker_task_events (
                event_id, task_id, kind, source, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                str(kind or "").strip(),
                str(source or "").strip() or "tracker",
                _normalize_json_text(payload or {}),
                when,
            ),
        )
        return {
            "event_id": event_id,
            "task_id": task_id,
            "kind": kind,
            "source": source,
            "payload": payload or {},
            "created_at": when,
        }

    @classmethod
    def load_issue_store(
        cls,
        workdir: Optional[str] = None,
        *,
        kinds: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                where = ["deleted_at = ''"]
                params: list[Any] = []
                kind_list = kinds or ["symphony_local_issue"]
                if kind_list:
                    placeholders = ",".join("?" for _ in kind_list)
                    where.append(f"kind IN ({placeholders})")
                    params.extend(kind_list)
                rows = conn.execute(
                    f"""
                    SELECT * FROM tracker_tasks
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at ASC
                    """,
                    params,
                ).fetchall()
                issues = [cls._task_row_to_issue(_row_to_dict(row)) for row in rows]
                return {"issues": issues}
            finally:
                conn.close()

    @classmethod
    def sync_issue_store(
        cls,
        store: dict[str, Any],
        workdir: Optional[str] = None,
        *,
        kind: str = "symphony_local_issue",
    ) -> None:
        cls.ensure_initialized(workdir)
        issues = store.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                current_rows = conn.execute(
                    "SELECT task_id, identifier, status FROM tracker_tasks WHERE deleted_at = '' AND kind = ?",
                    (kind,),
                ).fetchall()
                current_tasks = {
                    str(row["task_id"] or ""): str(row["status"] or "").strip().lower()
                    for row in current_rows
                }
                current_ids = set(current_tasks)
                keep_ids: set[str] = set()
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    issue["kind"] = kind
                    existing = cls._get_existing_task(
                        conn,
                        task_id=str(issue.get("id", "") or ""),
                        identifier=str(issue.get("identifier", "") or ""),
                    )
                    if str(existing.get("deleted_at", "") or "").strip():
                        keep_ids.add(str(existing.get("task_id", "") or ""))
                        continue
                    task = cls._issue_to_task_record(issue, existing=existing)
                    task["kind"] = kind
                    cls._upsert_task(conn, task, source="sync_issue_store", append_event=False)
                    keep_ids.add(task["task_id"])
                for task_id in current_ids - keep_ids:
                    if current_tasks.get(task_id) in _TASK_TERMINAL_STATUSES:
                        continue
                    now_iso = _utc_now_iso()
                    conn.execute(
                        """
                        UPDATE tracker_tasks
                        SET status = 'cancelled', claim_owner = '', claim_token = '',
                            claim_expires_at = '', claim_heartbeat_at = '',
                            updated_at = ?, deleted_at = ?
                        WHERE task_id = ? AND kind = ? AND deleted_at = ''
                        """,
                        (now_iso, now_iso, task_id, kind),
                    )
                    cls._append_event_conn(
                        conn,
                        task_id=task_id,
                        kind="deleted",
                        source="sync_issue_store",
                        payload={"reason": "missing from issue store sync"},
                    )
                cls._materialize_issue_store_conn(conn, workdir, kind=kind)
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def get_issue(
        cls,
        *,
        task_id: str = "",
        identifier: str = "",
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                existing = cls._get_existing_task(conn, task_id=task_id, identifier=identifier)
                if not existing or str(existing.get("deleted_at", "") or "").strip():
                    return None
                row = conn.execute(
                    "SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1",
                    (existing["task_id"],),
                ).fetchone()
                if row is None:
                    return None
                return cls._task_row_to_issue(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def list_tasks(
        cls,
        workdir: Optional[str] = None,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        include_deleted: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                where: list[str] = []
                params: list[Any] = []
                if not include_deleted:
                    where.append("deleted_at = ''")
                if kind:
                    where.append("kind = ?")
                    params.append(str(kind).strip())
                if status:
                    where.append("LOWER(status) = ?")
                    params.append(str(status).strip().lower())
                if channel:
                    where.append("LOWER(execution_channel) = ?")
                    params.append(str(channel).strip().lower())
                sql = "SELECT * FROM tracker_tasks"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY created_at ASC"
                if limit is not None:
                    sql += f" LIMIT {max(1, int(limit))}"
                    if int(offset or 0) > 0:
                        sql += f" OFFSET {max(0, int(offset))}"
                rows = conn.execute(sql, params).fetchall()
                return [cls._task_row_to_task(_row_to_dict(row)) for row in rows]
            finally:
                conn.close()

    @classmethod
    def get_task(
        cls,
        *,
        task_id: str = "",
        identifier: str = "",
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                existing = cls._get_existing_task(conn, task_id=task_id, identifier=identifier)
                if not existing or str(existing.get("deleted_at", "") or "").strip():
                    return None
                row = conn.execute(
                    "SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1",
                    (existing["task_id"],),
                ).fetchone()
                if row is None:
                    return None
                return cls._task_row_to_task(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def upsert_issue(
        cls,
        issue: dict[str, Any],
        workdir: Optional[str] = None,
        *,
        source: str = "app",
        append_event: bool = True,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                task = cls._upsert_task(conn, issue, source=source, append_event=append_event)
                cls._materialize_issue_store_conn(conn, workdir, kind=str(task.get("kind", "") or "symphony_local_issue"))
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
                return cls._task_row_to_issue(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def delete_issue(
        cls,
        *,
        task_id: str = "",
        identifier: str = "",
        workdir: Optional[str] = None,
        forget_unresolved_submission: bool = False,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                if forget_unresolved_submission:
                    conn.execute("BEGIN IMMEDIATE")
                existing = cls._get_existing_task(conn, task_id=task_id, identifier=identifier)
                if not existing:
                    return None
                row = conn.execute(
                    "SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1",
                    (existing["task_id"],),
                ).fetchone()
                issue = cls._task_row_to_issue(_row_to_dict(row)) if row is not None else None
                if issue is None:
                    return None
                if str(existing.get("deleted_at", "") or "").strip():
                    return issue
                if forget_unresolved_submission:
                    workspace = existing.get("workspace") or {}
                    references = [existing.get("job_ref") or {}, workspace.get("job_ref") or {}, workspace]
                    if (workspace.get("recovery", {}).get("state") != "submission_unknown"
                            or any(str(ref.get("job_id") or ref.get("process_id") or "").strip() for ref in references)):
                        raise ValueError("任务状态已变化或已有远程编号，请刷新后使用正常停止／删除流程。")
                    expires = str(existing.get("claim_expires_at") or "").strip()
                    if expires:
                        try:
                            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            active = expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) > datetime.now(timezone.utc)
                        except (TypeError, ValueError):
                            active = True
                        if active:
                            raise ValueError("提交执行器仍持有任务，请等待提交结束后再删除本地记录。")
                now_iso = _utc_now_iso()
                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?, claim_owner = '', claim_token = '',
                        claim_expires_at = '', claim_heartbeat_at = '',
                        updated_at = ?, deleted_at = ?
                    WHERE task_id = ?
                    """,
                    (existing["status"] if forget_unresolved_submission else "cancelled", now_iso, now_iso, issue["id"]),
                )
                cls._append_event_conn(
                    conn,
                    task_id=str(issue["id"] or ""),
                    kind="deleted",
                    source="app",
                    payload={"identifier": str(issue.get("identifier", "") or ""),
                             "local_only": forget_unresolved_submission},
                )
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                return issue
            finally:
                conn.close()

    @classmethod
    def load_review_store(
        cls,
        workdir: Optional[str] = None,
        *,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                where = []
                params: list[Any] = []
                if status:
                    where.append("LOWER(status) = ?")
                    params.append(str(status).strip().lower())
                sql = "SELECT * FROM tracker_reviews"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY created_at ASC"
                rows = conn.execute(sql, params).fetchall()
                reviews = [cls._review_row_to_review(_row_to_dict(row)) for row in rows]
                return {"reviews": reviews, "history": []}
            finally:
                conn.close()

    @classmethod
    def sync_review_store(cls, store: dict[str, Any], workdir: Optional[str] = None) -> None:
        cls.ensure_initialized(workdir)
        reviews = store.get("reviews", [])
        if not isinstance(reviews, list):
            reviews = []
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                current_rows = conn.execute("SELECT review_id FROM tracker_reviews").fetchall()
                current_ids = {str(row["review_id"] or "") for row in current_rows}
                keep_ids: set[str] = set()
                for review in reviews:
                    if not isinstance(review, dict):
                        continue
                    record = cls._upsert_review(conn, review)
                    keep_ids.add(record["review_id"])
                for review_id in current_ids - keep_ids:
                    conn.execute("DELETE FROM tracker_reviews WHERE review_id = ?", (review_id,))
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def get_review(
        cls,
        *,
        review_id: str = "",
        identifier: str = "",
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                existing = cls._get_existing_review(conn, review_id=review_id, identifier=identifier)
                if not existing:
                    return None
                row = conn.execute(
                    "SELECT * FROM tracker_reviews WHERE review_id = ? LIMIT 1",
                    (existing["review_id"],),
                ).fetchone()
                return cls._review_row_to_review(_row_to_dict(row)) if row else None
            finally:
                conn.close()

    @classmethod
    def upsert_review(
        cls,
        review: dict[str, Any],
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                record = cls._upsert_review(conn, review)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_reviews WHERE review_id = ?", (record["review_id"],)).fetchone()
                return cls._review_row_to_review(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def delete_review(
        cls,
        *,
        review_id: str = "",
        identifier: str = "",
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                review = cls.get_review(review_id=review_id, identifier=identifier, workdir=workdir)
                if review is None:
                    return None
                conn.execute("DELETE FROM tracker_reviews WHERE review_id = ?", (review["id"],))
                conn.commit()
                return review
            finally:
                conn.close()

    @classmethod
    def list_task_events(cls, task_id: str, workdir: Optional[str] = None) -> list[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                rows = conn.execute(
                    "SELECT * FROM tracker_task_events WHERE task_id = ? ORDER BY created_at ASC",
                    (task_id,),
                ).fetchall()
                events: list[dict[str, Any]] = []
                for row in rows:
                    payload = _row_to_dict(row)
                    payload["payload"] = _parse_json_text(payload.get("payload_json"), {})
                    events.append(
                        {
                            "event_id": str(payload.get("event_id", "") or ""),
                            "task_id": str(payload.get("task_id", "") or ""),
                            "kind": str(payload.get("kind", "") or ""),
                            "source": str(payload.get("source", "") or ""),
                            "payload": payload.get("payload", {}),
                            "created_at": str(payload.get("created_at", "") or ""),
                        }
                    )
                return events
            finally:
                conn.close()

    @classmethod
    def append_event(
        cls,
        *,
        task_id: str,
        kind: str,
        source: str,
        payload: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                event = cls._append_event_conn(conn, task_id=task_id, kind=kind, source=source, payload=payload)
                conn.commit()
                return event
            finally:
                conn.close()

    @classmethod
    def request_cancellation(cls, *, task_id: str, workdir: Optional[str] = None) -> dict[str, Any]:
        """Persist intent before I/O, atomically fencing a never-claimed submission."""
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = cls._get_existing_task(conn, task_id=task_id)
                if not task or task.get("deleted_at"):
                    raise ValueError("task not found")
                workspace = task["workspace"]
                cancellation = dict(workspace.get("cancellation") or {})
                status = task["status"]
                if status not in _TASK_TERMINAL_STATUSES:
                    cancellation.setdefault("request_id", uuid.uuid4().hex)
                    cancellation.setdefault("requested_at", _utc_now_iso())
                    cancellation["state"] = "pending"
                    if status == "queued" and not task.get("claim_owner") and not task.get("job_ref") and not workspace.get("job_id") and not task["metadata"].get("execution_pid"):
                        status = "cancelled"
                        cancellation["state"] = "confirmed"
                    workspace["cancellation"] = cancellation
                    # Repeated clicks keep an in-flight recovery lease intact.
                    recovery = dict(workspace.get("recovery") or {})
                    recovery["next_retry_at"] = 0
                    workspace["recovery"] = recovery
                    conn.execute("UPDATE tracker_tasks SET status=?, workspace_json=?, updated_at=? WHERE task_id=?", (status, _normalize_json_text(workspace), _utc_now_iso(), task_id))
                    cls._append_event_conn(conn, task_id=task_id, kind="cancel_requested", source="app", payload={"request_id": cancellation["request_id"]})
                    cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id=?", (task_id,)).fetchone()
                return cls._task_row_to_task(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def claim_recovery(cls, *, task_id: str, now: float, workdir: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Short SQLite lease shared by HTTP cancellation and background recovery."""
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = cls._get_existing_task(conn, task_id=task_id)
                if not task or task.get("deleted_at"):
                    conn.commit()
                    return None
                workspace = task["workspace"]
                recovery = dict(workspace.get("recovery") or {})
                if max(float(recovery.get("lease_until") or 0), float(recovery.get("next_retry_at") or 0)) > now:
                    conn.commit()
                    return None
                recovery.update(lease_until=now + 90, attempt=int(recovery.get("attempt") or 0) + 1)
                workspace["recovery"] = recovery
                conn.execute("UPDATE tracker_tasks SET workspace_json=? WHERE task_id=?", (_normalize_json_text(workspace), task_id))
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id=?", (task_id,)).fetchone()
                return cls._task_row_to_task(_row_to_dict(row))
            finally:
                conn.close()

    @classmethod
    def cancel_unclaimed(cls, *, task_id: str, workdir: Optional[str] = None) -> bool:
        """Serialize cancellation with claiming: only never-claimed jobs are safe locally."""
        cls.ensure_initialized(workdir)
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = cls._get_existing_task(conn, task_id=task_id)
                if (not task or task.get("status") != "queued" or task.get("claim_owner")
                        or task.get("job_ref") or task.get("workspace", {}).get("job_id")
                        or task.get("metadata", {}).get("execution_pid")):
                    conn.commit()
                    return False
                conn.execute("UPDATE tracker_tasks SET status='cancelled', updated_at=? WHERE task_id=?", (_utc_now_iso(), task_id))
                cls._append_event_conn(conn, task_id=task_id, kind="cancelled", source="app", payload={"reason": "cancelled_before_claim"})
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                return True
            finally:
                conn.close()

    @classmethod
    def patch_task(
        cls,
        *,
        task_id: str = "",
        identifier: str = "",
        status: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
        result_summary: str = "",
        error_summary: str = "",
        event_kind: str = "updated",
        source: str = "system",
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Patch task control-plane fields without requiring a worker lease."""

        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in _TASK_STATUSES:
            raise ValueError("invalid task status")

        cls.ensure_initialized(workdir)
        now_iso = _utc_now_iso()
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = cls._get_existing_task(
                    conn,
                    task_id=str(task_id or "").strip(),
                    identifier=str(identifier or "").strip(),
                )
                if not existing:
                    conn.commit()
                    return None

                current_status = str(existing.get("status", "") or "").strip().lower()
                next_status = normalized_status or current_status
                if current_status in _TASK_TERMINAL_STATUSES and normalized_status not in _TASK_TERMINAL_STATUSES:
                    next_status = current_status

                workspace = (
                    dict(existing.get("workspace"))
                    if isinstance(existing.get("workspace"), dict)
                    else {}
                )
                if isinstance(workspace_patch, dict) and workspace_patch:
                    workspace = _merge_workspace(workspace, workspace_patch)

                metadata = (
                    dict(existing.get("metadata"))
                    if isinstance(existing.get("metadata"), dict)
                    else {}
                )
                if isinstance(metadata_patch, dict) and metadata_patch:
                    metadata.update(metadata_patch)

                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?,
                        workspace_json = ?,
                        result_summary = ?,
                        error_summary = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        next_status,
                        _normalize_json_text(workspace),
                        result_summary or str(existing.get("result_summary", "") or ""),
                        error_summary or str(existing.get("error_summary", "") or ""),
                        _normalize_json_text(metadata),
                        now_iso,
                        str(existing.get("task_id", "") or ""),
                    ),
                )
                cls._append_event_conn(
                    conn,
                    task_id=str(existing.get("task_id", "") or ""),
                    kind=str(event_kind or "updated").strip() or "updated",
                    source=source,
                    payload={
                        "identifier": str(existing.get("identifier", "") or identifier or ""),
                        "status": next_status,
                        "workspace_patch": workspace_patch if isinstance(workspace_patch, dict) else {},
                        "metadata_patch": metadata_patch if isinstance(metadata_patch, dict) else {},
                    },
                )
                cls._materialize_issue_store_conn(
                    conn,
                    workdir,
                    kind=str(existing.get("kind", "") or "symphony_local_issue"),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM tracker_tasks WHERE task_id = ?",
                    (str(existing.get("task_id", "") or ""),),
                ).fetchone()
                updated = cls._task_row_to_task(_row_to_dict(row))
                _reconcile_orchestration_task(updated, workdir)
                return updated
            finally:
                conn.close()

    @classmethod
    def record_result(
        cls,
        *,
        task_id: str = "",
        identifier: str = "",
        status: str,
        source: str = "system",
        result_summary: str = "",
        error_summary: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        job_ref: Optional[dict[str, Any]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Record an externally observed task result without requiring a worker lease.

        Bridge/Symphony workers normally finish tasks with a claim token. The
        async watcher is a reconciliation process: it may see final output after
        the worker lease has expired or after process restart, so it needs a
        claim-free, idempotent way to enrich the authoritative tracker record.
        """

        normalized_status = str(status or "").strip().lower()
        if normalized_status not in _TASK_STATUSES:
            raise ValueError("invalid result status")

        cls.ensure_initialized(workdir)
        now_iso = _utc_now_iso()
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = cls._get_existing_task(
                    conn,
                    task_id=str(task_id or "").strip(),
                    identifier=str(identifier or "").strip(),
                )
                if not existing:
                    conn.commit()
                    return None

                current_status = str(existing.get("status", "") or "").strip().lower()
                next_status = normalized_status
                if current_status in _TASK_TERMINAL_STATUSES and normalized_status not in _TASK_TERMINAL_STATUSES:
                    next_status = current_status

                workspace = (
                    dict(existing.get("workspace"))
                    if isinstance(existing.get("workspace"), dict)
                    else {}
                )
                if isinstance(workspace_patch, dict) and workspace_patch:
                    workspace = _merge_workspace(workspace, workspace_patch)

                existing_job_ref = (
                    dict(existing.get("job_ref"))
                    if isinstance(existing.get("job_ref"), dict)
                    else {}
                )
                if isinstance(job_ref, dict) and job_ref:
                    next_job_ref = {**existing_job_ref, **job_ref}
                else:
                    next_job_ref = existing_job_ref
                if next_job_ref:
                    workspace["job_ref"] = dict(next_job_ref)

                metadata = (
                    dict(existing.get("metadata"))
                    if isinstance(existing.get("metadata"), dict)
                    else {}
                )
                if isinstance(metadata_patch, dict) and metadata_patch:
                    metadata.update(metadata_patch)

                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?,
                        workspace_json = ?,
                        job_ref_json = ?,
                        result_summary = ?,
                        error_summary = ?,
                        metadata_json = ?,
                        claim_owner = CASE
                            WHEN ? IN ('completed', 'failed', 'cancelled', 'duplicate') THEN ''
                            ELSE claim_owner
                        END,
                        claim_token = CASE
                            WHEN ? IN ('completed', 'failed', 'cancelled', 'duplicate') THEN ''
                            ELSE claim_token
                        END,
                        claim_expires_at = CASE
                            WHEN ? IN ('completed', 'failed', 'cancelled', 'duplicate') THEN ''
                            ELSE claim_expires_at
                        END,
                        claim_heartbeat_at = CASE
                            WHEN ? IN ('completed', 'failed', 'cancelled', 'duplicate') THEN ''
                            ELSE claim_heartbeat_at
                        END,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        next_status,
                        _normalize_json_text(workspace),
                        _normalize_json_text(next_job_ref),
                        result_summary or str(existing.get("result_summary", "") or ""),
                        error_summary or str(existing.get("error_summary", "") or ""),
                        _normalize_json_text(metadata),
                        next_status,
                        next_status,
                        next_status,
                        next_status,
                        now_iso,
                        str(existing.get("task_id", "") or ""),
                    ),
                )
                event_kind = {
                    "completed": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "duplicate": "duplicate",
                }.get(next_status, "updated")
                cls._append_event_conn(
                    conn,
                    task_id=str(existing.get("task_id", "") or ""),
                    kind=event_kind,
                    source=source,
                    payload={
                        "identifier": str(existing.get("identifier", "") or identifier or ""),
                        "status": next_status,
                        "result_summary": result_summary,
                        "error_summary": error_summary,
                        "job_ref": next_job_ref,
                        "metadata_patch": metadata_patch if isinstance(metadata_patch, dict) else {},
                    },
                )
                cls._materialize_issue_store_conn(
                    conn,
                    workdir,
                    kind=str(existing.get("kind", "") or "symphony_local_issue"),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM tracker_tasks WHERE task_id = ?",
                    (str(existing.get("task_id", "") or ""),),
                ).fetchone()
                updated = cls._task_row_to_task(_row_to_dict(row))
                _reconcile_orchestration_task(updated, workdir)
                return updated
            finally:
                conn.close()

    @classmethod
    def claim_next(
        cls,
        *,
        worker_id: str,
        channel: str,
        lease_seconds: int,
        allowed_servers: list[str],
        workdir: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        cls.ensure_initialized(workdir)
        normalized_channel = str(channel or "").strip().lower()
        if normalized_channel not in {"bridge", "symphony"}:
            raise ValueError("channel must be bridge or symphony")
        allow_all = not allowed_servers
        allowed = [str(item or "").strip().lower() for item in allowed_servers if str(item or "").strip()]
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=max(10, int(lease_seconds or 30)))).isoformat()

        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                sql = (
                    "SELECT * FROM tracker_tasks "
                    "WHERE deleted_at = '' AND kind = 'symphony_local_issue' "
                    "AND COALESCE(json_extract(workspace_json, '$.cancellation.state'), '') != 'pending' "
                    "AND execution_channel = ? "
                    "AND status = 'queued' AND claim_owner = '' "
                    "AND COALESCE(json_extract(job_ref_json, '$.job_id'), '') = '' "
                    "AND COALESCE(json_extract(workspace_json, '$.job_id'), '') = '' "
                )
                # Expired executions are observed by recovery, never re-submitted.
                params: list[Any] = [normalized_channel]
                if not allow_all:
                    placeholders = ",".join("?" for _ in allowed)
                    sql += f"AND LOWER(tool_server) IN ({placeholders}) "
                    params.extend(allowed)
                sql += "ORDER BY created_at ASC LIMIT 1"
                row = conn.execute(sql, params).fetchone()
                if row is None:
                    conn.commit()
                    return None
                task = _row_to_dict(row)
                claim_token = uuid.uuid4().hex
                next_status = "claimed"
                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?, claim_owner = ?, claim_token = ?, claim_expires_at = ?,
                        claim_heartbeat_at = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (next_status, worker_id, claim_token, lease_until, now_iso, now_iso, task["task_id"]),
                )
                cls._append_event_conn(
                    conn,
                    task_id=str(task["task_id"] or ""),
                    kind="claimed",
                    source=f"worker:{normalized_channel}",
                    payload={"worker_id": worker_id, "channel": normalized_channel},
                )
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
                claimed_task = cls._task_row_to_task(_row_to_dict(row))
                claimed_task["claim_token"] = claim_token
                return claimed_task
            finally:
                conn.close()

    @classmethod
    def heartbeat(
        cls,
        *,
        task_id: str,
        claim_token: str,
        worker_id: str,
        lease_seconds: int,
        status: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        result_summary: str = "",
        error_summary: str = "",
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=max(10, int(lease_seconds or 30)))).isoformat()
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in _TASK_STATUSES:
            normalized_status = ""
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
                if row is None:
                    raise ValueError(f"task not found: {task_id}")
                task = _row_to_dict(row)
                if str(task.get("status", "") or "").strip().lower() in _TASK_TERMINAL_STATUSES:
                    conn.commit()
                    issue = cls._task_row_to_issue(task)
                    issue["claim_token"] = ""
                    return issue
                cls._require_matching_claim_token(task, claim_token)
                workspace = _parse_json_text(task.get("workspace_json"), {})
                if isinstance(workspace_patch, dict) and workspace_patch:
                    workspace = _merge_workspace(workspace, workspace_patch)
                next_status = normalized_status or str(task.get("status", "") or "")
                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?, claim_owner = ?, claim_expires_at = ?, claim_heartbeat_at = ?,
                        workspace_json = ?, result_summary = ?, error_summary = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        next_status,
                        worker_id or str(task.get("claim_owner", "") or ""),
                        lease_until,
                        now_iso,
                        _normalize_json_text(workspace),
                        result_summary or str(task.get("result_summary", "") or ""),
                        error_summary or str(task.get("error_summary", "") or ""),
                        now_iso,
                        task_id,
                    ),
                )
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ?", (task_id,)).fetchone()
                issue = cls._task_row_to_issue(_row_to_dict(row))
                issue["claim_token"] = claim_token
                return issue
            finally:
                conn.close()

    @classmethod
    def attach_job_ref(
        cls,
        *,
        task_id: str,
        claim_token: str,
        job_ref: dict[str, Any],
        workspace_patch: Optional[dict[str, Any]] = None,
        worker_id: str = "",
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        now_iso = _utc_now_iso()
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
                if row is None:
                    raise ValueError(f"task not found: {task_id}")
                task = _row_to_dict(row)
                if str(task.get("status", "") or "").strip().lower() in _TASK_TERMINAL_STATUSES:
                    conn.commit()
                    issue = cls._task_row_to_issue(task)
                    issue["claim_token"] = ""
                    return issue
                cls._require_matching_claim_token(task, claim_token)
                workspace = _parse_json_text(task.get("workspace_json"), {})
                if isinstance(workspace_patch, dict) and workspace_patch:
                    workspace = _merge_workspace(workspace, workspace_patch)
                if isinstance(job_ref, dict):
                    workspace["job_ref"] = dict(job_ref)
                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = 'running',
                        claim_owner = ?,
                        workspace_json = ?,
                        job_ref_json = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        worker_id or str(task.get("claim_owner", "") or ""),
                        _normalize_json_text(workspace),
                        _normalize_json_text(job_ref if isinstance(job_ref, dict) else {}),
                        now_iso,
                        task_id,
                    ),
                )
                cls._append_event_conn(
                    conn,
                    task_id=task_id,
                    kind="started",
                    source="worker",
                    payload={"job_ref": job_ref if isinstance(job_ref, dict) else {}},
                )
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ?", (task_id,)).fetchone()
                issue = cls._task_row_to_issue(_row_to_dict(row))
                issue["claim_token"] = claim_token
                return issue
            finally:
                conn.close()

    @classmethod
    def _finish_task(
        cls,
        *,
        task_id: str,
        claim_token: str,
        final_status: str,
        source: str,
        result_summary: str = "",
        error_summary: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        job_ref: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        cls.ensure_initialized(workdir)
        normalized_status = str(final_status or "").strip().lower()
        if normalized_status not in _TASK_STATUSES:
            raise ValueError("invalid final status")
        now_iso = _utc_now_iso()
        with _LOCK:
            conn = cls._connect(workdir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
                if row is None:
                    raise ValueError(f"task not found: {task_id}")
                task = _row_to_dict(row)
                cls._require_matching_claim_token(task, claim_token)
                workspace = _parse_json_text(task.get("workspace_json"), {})
                if isinstance(workspace_patch, dict) and workspace_patch:
                    workspace = _merge_workspace(workspace, workspace_patch)
                if isinstance(job_ref, dict) and job_ref:
                    workspace["job_ref"] = dict(job_ref)
                workspace["status"] = normalized_status
                workspace["issue_state"] = normalized_status
                workspace["tracker_status"] = normalized_status
                workspace["lifecycle_status"] = normalized_status
                try:
                    from ..task.orchestration import build_action_policy

                    existing_job_ref = job_ref if isinstance(job_ref, dict) and job_ref else _parse_json_text(task.get("job_ref_json"), {})
                    workspace["action_policy"] = build_action_policy(
                        lifecycle_status=normalized_status,
                        job_id=str((existing_job_ref or {}).get("job_id", "") or ""),
                        has_submit_input=bool(_parse_json_text(task.get("submit_input_json"), {})),
                        resume_supported=bool(workspace.get("resume_supported", False)),
                        resume_command=str(workspace.get("resume_command", "") or ""),
                        review_status=str((workspace.get("review_lineage") or {}).get("status", "") or "")
                        if isinstance(workspace.get("review_lineage"), dict)
                        else "",
                    )
                except Exception:
                    pass
                conn.execute(
                    """
                    UPDATE tracker_tasks
                    SET status = ?, workspace_json = ?, job_ref_json = ?, result_summary = ?, error_summary = ?,
                        claim_owner = '', claim_token = '', claim_expires_at = '',
                        claim_heartbeat_at = '', updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        normalized_status,
                        _normalize_json_text(workspace),
                        _normalize_json_text(job_ref if isinstance(job_ref, dict) else _parse_json_text(task.get("job_ref_json"), {})),
                        result_summary or str(task.get("result_summary", "") or ""),
                        error_summary or str(task.get("error_summary", "") or ""),
                        now_iso,
                        task_id,
                    ),
                )
                event_kind = {
                    "completed": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "duplicate": "duplicate",
                }.get(normalized_status, normalized_status)
                cls._append_event_conn(
                    conn,
                    task_id=task_id,
                    kind=event_kind,
                    source=source,
                    payload={
                        "result_summary": result_summary,
                        "error_summary": error_summary,
                        "job_ref": job_ref if isinstance(job_ref, dict) else {},
                    },
                )
                cls._materialize_issue_store_conn(conn, workdir)
                conn.commit()
                row = conn.execute("SELECT * FROM tracker_tasks WHERE task_id = ?", (task_id,)).fetchone()
                row_data = _row_to_dict(row)
                _reconcile_orchestration_task(cls._task_row_to_task(row_data), workdir)
                return cls._task_row_to_issue(row_data)
            finally:
                conn.close()

    @classmethod
    def complete_task(
        cls,
        *,
        task_id: str,
        claim_token: str,
        result_summary: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        job_ref: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        return cls._finish_task(
            task_id=task_id,
            claim_token=claim_token,
            final_status="completed",
            source="worker",
            result_summary=result_summary,
            workspace_patch=workspace_patch,
            job_ref=job_ref,
            workdir=workdir,
        )

    @classmethod
    def fail_task(
        cls,
        *,
        task_id: str,
        claim_token: str,
        error_summary: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        job_ref: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        return cls._finish_task(
            task_id=task_id,
            claim_token=claim_token,
            final_status="failed",
            source="worker",
            error_summary=error_summary,
            workspace_patch=workspace_patch,
            job_ref=job_ref,
            workdir=workdir,
        )

    @classmethod
    def cancel_task(
        cls,
        *,
        task_id: str,
        claim_token: str = "",
        result_summary: str = "",
        workspace_patch: Optional[dict[str, Any]] = None,
        source: str = "api",
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        return cls._finish_task(
            task_id=task_id,
            claim_token=claim_token,
            final_status="cancelled",
            source=source,
            result_summary=result_summary,
            workspace_patch=workspace_patch,
            workdir=workdir,
        )
