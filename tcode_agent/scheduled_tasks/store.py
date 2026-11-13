from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from ..sqlite_runtime import connect as sqlite_connect


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        parsed = json.loads(str(value))
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


class ScheduledTaskStore:
    def __init__(self, db_path: str | Path | None = None):
        self._explicit_path = Path(db_path) if db_path else None
        self._lock = threading.RLock()
        self.ensure_initialized()

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        base = Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip())
        return base / ".tcode" / "scheduled_tasks.sqlite"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(path.as_posix(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def ensure_initialized(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    next_run_at REAL,
                    last_run_at REAL,
                    last_status TEXT,
                    last_error TEXT,
                    source_session_id TEXT,
                    execution_session_id TEXT,
                    agent_name TEXT NOT NULL DEFAULT 'build',
                    workdir TEXT,
                    created_via TEXT NOT NULL DEFAULT 'api',
                    claimed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                ON scheduled_tasks(status, next_run_at, claimed_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_owner
                ON scheduled_tasks(tenant_id, user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scheduled_for REAL NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task
                ON scheduled_task_runs(task_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS scheduled_notifications (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT,
                    run_id TEXT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_owner
                ON scheduled_notifications(tenant_id, user_id, is_read, created_at DESC);

                CREATE TABLE IF NOT EXISTS scheduled_task_drafts (
                    token TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_task_preferences (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, user_id)
                );
                """
            )
            conn.commit()

    @staticmethod
    def _task_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["schedule"] = _loads(item.pop("schedule_json", "{}"), {})
        item["claimed"] = bool(item.get("claimed_at"))
        return item

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["is_read"] = bool(item.get("is_read"))
        return item

    def create_draft(
        self,
        spec: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        ttl_seconds: float = 900,
    ) -> dict[str, Any]:
        token = uuid.uuid4().hex
        now = time.time()
        expires_at = now + max(60, float(ttl_seconds))
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM scheduled_task_drafts WHERE expires_at <= ?", (now,))
            conn.execute(
                """
                INSERT INTO scheduled_task_drafts
                (token, tenant_id, user_id, workspace_id, spec_json, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token, tenant_id, user_id, workspace_id, _dumps(spec), expires_at, now),
            )
            conn.commit()
        return {"token": token, "expires_at": expires_at, "spec": dict(spec)}

    def get_preferences(self, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT timezone, updated_at FROM scheduled_task_preferences
                WHERE tenant_id = ? AND user_id = ?
                """,
                (tenant_id, user_id),
            ).fetchone()
        if row is None:
            return {"timezone": "Asia/Shanghai", "updated_at": None}
        return {"timezone": str(row["timezone"]), "updated_at": row["updated_at"]}

    def set_preferences(
        self,
        *,
        tenant_id: str,
        user_id: str,
        timezone_name: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_task_preferences
                (tenant_id, user_id, timezone, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, user_id, timezone_name, now),
            )
            conn.commit()
        return {"timezone": timezone_name, "updated_at": now}

    def confirm_draft(
        self,
        token: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM scheduled_task_drafts
                WHERE token = ? AND tenant_id = ? AND user_id = ?
                """,
                (str(token or "").strip(), tenant_id, user_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError("确认信息不存在或不属于当前用户")
            if float(row["expires_at"] or 0) <= now:
                conn.execute("DELETE FROM scheduled_task_drafts WHERE token = ?", (token,))
                conn.commit()
                raise ValueError("确认信息已过期，请重新创建预览")
            spec = _loads(row["spec_json"], {})
            # Confirmation is the creation boundary. Relative delays and the
            # next occurrence of recurring schedules are recalculated here.
            from .schedule import normalize_task_spec

            spec = normalize_task_spec(spec, now_ts=now)
            task = self._insert_task(
                conn,
                spec,
                tenant_id=str(row["tenant_id"]),
                user_id=str(row["user_id"]),
                workspace_id=str(row["workspace_id"]),
                now=now,
            )
            conn.execute("DELETE FROM scheduled_task_drafts WHERE token = ?", (token,))
            conn.commit()
            return task

    def _insert_task(
        self,
        conn: sqlite3.Connection,
        spec: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        now: float,
    ) -> dict[str, Any]:
        task_id = str(spec.get("id") or uuid.uuid4())
        execution_session_id = str(
            spec.get("execution_session_id") or f"scheduled-{task_id}"
        )
        values = (
            task_id,
            tenant_id,
            user_id,
            workspace_id,
            str(spec.get("name") or "定时任务"),
            str(spec.get("instruction") or ""),
            str(spec.get("schedule_type") or ""),
            _dumps(spec.get("schedule") or {}),
            str(spec.get("timezone") or "Asia/Shanghai"),
            float(spec["next_run_at"]) if spec.get("next_run_at") is not None else None,
            str(spec.get("source_session_id") or ""),
            execution_session_id,
            str(spec.get("agent_name") or "build"),
            str(spec.get("workdir") or ""),
            str(spec.get("created_via") or "api"),
            now,
            now,
        )
        conn.execute(
            """
            INSERT INTO scheduled_tasks (
                id, tenant_id, user_id, workspace_id, name, instruction,
                schedule_type, schedule_json, timezone, status, next_run_at,
                source_session_id, execution_session_id, agent_name, workdir,
                created_via, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) or {}

    def create_task(
        self,
        spec: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            task = self._insert_task(
                conn,
                spec,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
                now=now,
            )
            conn.commit()
            return task

    def get_task(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        clauses = ["id = ?", "tenant_id = ?"]
        params: list[Any] = [task_id, tenant_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM scheduled_tasks WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return self._task_from_row(row)

    def list_tasks(
        self,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?", "deleted_at IS NULL"]
        params: list[Any] = [tenant_id]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        params.append(max(1, min(int(limit), 1000)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM scheduled_tasks
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                         COALESCE(next_run_at, 99999999999), created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._task_from_row(row) or {} for row in rows]

    def update_task(
        self,
        task_id: str,
        spec: dict[str, Any],
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id, tenant_id=tenant_id, user_id=user_id)
        if task is None:
            raise KeyError("任务不存在")
        if task.get("claimed_at"):
            raise RuntimeError("任务正在执行，暂时不能修改")
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET
                    name = ?, instruction = ?, schedule_type = ?, schedule_json = ?,
                    timezone = ?, status = 'active', next_run_at = ?, last_error = NULL,
                    source_session_id = ?, agent_name = ?, workdir = ?, claimed_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(spec.get("name") or task["name"]),
                    str(spec.get("instruction") or task["instruction"]),
                    str(spec.get("schedule_type") or task["schedule_type"]),
                    _dumps(spec.get("schedule") or task["schedule"]),
                    str(spec.get("timezone") or task["timezone"]),
                    spec.get("next_run_at"),
                    str(spec.get("source_session_id") or task.get("source_session_id") or ""),
                    str(spec.get("agent_name") or task.get("agent_name") or "build"),
                    str(spec.get("workdir") or task.get("workdir") or ""),
                    now,
                    task_id,
                ),
            )
            conn.commit()
        updated = self.get_task(task_id, tenant_id=tenant_id, user_id=user_id)
        if updated is None:
            raise KeyError("任务不存在")
        return updated

    def set_status(
        self,
        task_id: str,
        status: str,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        if status not in {"active", "paused"}:
            raise ValueError("状态只允许 active 或 paused")
        task = self.get_task(task_id, tenant_id=tenant_id, user_id=user_id)
        if task is None:
            raise KeyError("任务不存在")
        if task.get("claimed_at"):
            raise RuntimeError("任务正在执行，暂时不能切换状态")
        current_status = str(task.get("status") or "")
        if status == "paused" and current_status != "active":
            raise ValueError("只有运行中的任务可以暂停")
        if status == "active" and current_status != "paused":
            raise ValueError("只有已暂停的任务可以恢复")
        resolved_next = next_run_at if status == "active" else task.get("next_run_at")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, next_run_at = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status, resolved_next, time.time(), task_id),
            )
            conn.commit()
        return self.get_task(task_id, tenant_id=tenant_id, user_id=user_id) or {}

    def delete_task(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        task = self.get_task(task_id, tenant_id=tenant_id, user_id=user_id)
        if task is None:
            return False
        if task.get("claimed_at"):
            raise RuntimeError("任务正在执行，暂时不能删除")
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'deleted', deleted_at = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            conn.commit()
        return True

    def recover_interrupted(self) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            running = conn.execute(
                "SELECT id FROM scheduled_task_runs WHERE status = 'running'"
            ).fetchall()
            conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = 'failed', error = '服务重启导致上次执行中断', finished_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.execute(
                "UPDATE scheduled_tasks SET claimed_at = NULL WHERE claimed_at IS NOT NULL"
            )
            conn.commit()
            return len(running)

    def _claim_row(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        scheduled_for: float,
        trigger: str,
    ) -> dict[str, Any] | None:
        now = time.time()
        updated = conn.execute(
            """
            UPDATE scheduled_tasks SET claimed_at = ?, updated_at = ?
            WHERE id = ? AND claimed_at IS NULL AND deleted_at IS NULL
            """,
            (now, now, task["id"]),
        )
        if updated.rowcount != 1:
            return None
        run_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO scheduled_task_runs
            (id, task_id, tenant_id, user_id, scheduled_for, trigger, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                run_id,
                task["id"],
                task["tenant_id"],
                task["user_id"],
                float(scheduled_for),
                trigger,
                now,
            ),
        )
        task_data = self._task_from_row(task) or {}
        return {"run_id": run_id, "task": task_data, "trigger": trigger}

    def claim_due(self, *, now_ts: float | None = None, limit: int = 10) -> list[dict[str, Any]]:
        now = float(time.time() if now_ts is None else now_ts)
        claims: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND deleted_at IS NULL AND claimed_at IS NULL
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC LIMIT ?
                """,
                (now, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                claim = self._claim_row(
                    conn,
                    row,
                    scheduled_for=float(row["next_run_at"]),
                    trigger="scheduled",
                )
                if claim:
                    claims.append(claim)
            conn.commit()
        return claims

    def claim_now(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clauses = ["id = ?", "tenant_id = ?", "deleted_at IS NULL"]
            params: list[Any] = [task_id, tenant_id]
            if user_id is not None:
                clauses.append("user_id = ?")
                params.append(user_id)
            row = conn.execute(
                f"SELECT * FROM scheduled_tasks WHERE {' AND '.join(clauses)}", params
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError("任务不存在")
            claim = self._claim_row(
                conn,
                row,
                scheduled_for=time.time(),
                trigger=trigger,
            )
            if claim is None:
                conn.rollback()
                raise RuntimeError("任务正在执行，请稍后再试")
            conn.commit()
            return claim

    def finish_run(
        self,
        run_id: str,
        *,
        success: bool,
        output: str = "",
        error: str = "",
        next_run_at: float | None = None,
        preserve_schedule: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                conn.rollback()
                raise KeyError("执行记录不存在")
            task = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (run["task_id"],)
            ).fetchone()
            if task is None:
                conn.rollback()
                raise KeyError("任务不存在")
            run_status = "succeeded" if success else "failed"
            safe_output = str(output or "")[:100_000]
            safe_error = str(error or "")[:20_000]
            conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = ?, output = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (run_status, safe_output, safe_error, now, run_id),
            )

            recurring = str(task["schedule_type"]) not in {"at", "delay"}
            current_status = str(task["status"])
            if preserve_schedule:
                next_value = task["next_run_at"]
                if recurring:
                    status = current_status
                else:
                    status = "completed" if success else "failed"
            else:
                next_value = next_run_at if recurring else None
                status = "active" if recurring else ("completed" if success else "failed")
                if current_status == "deleted":
                    status = "deleted"
            conn.execute(
                """
                UPDATE scheduled_tasks SET
                    status = ?, next_run_at = ?, last_run_at = ?, last_status = ?,
                    last_error = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_value,
                    now,
                    run_status,
                    safe_error if not success else None,
                    now,
                    task["id"],
                ),
            )
            notification_id = str(uuid.uuid4())
            if success:
                title = f"任务完成：{task['name']}"
                content = safe_output or "任务已执行完成。"
                kind = "task_succeeded"
            else:
                title = f"任务失败：{task['name']}"
                content = safe_error or "任务执行失败，可在任务中心手动重试。"
                kind = "task_failed"
            conn.execute(
                """
                INSERT INTO scheduled_notifications
                (id, tenant_id, user_id, task_id, run_id, kind, title, content, is_read, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    notification_id,
                    task["tenant_id"],
                    task["user_id"],
                    task["id"],
                    run_id,
                    kind,
                    title,
                    content,
                    now,
                ),
            )
            conn.commit()
        updated = self.get_task(
            str(task["id"]),
            tenant_id=str(task["tenant_id"]),
            user_id=str(task["user_id"]),
            include_deleted=True,
        ) or {}
        return {
            "task": updated,
            "run_id": run_id,
            "notification_id": notification_id,
            "status": run_status,
        }

    def list_runs(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        task = self.get_task(
            task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            include_deleted=True,
        )
        if task is None:
            raise KeyError("任务不存在")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE task_id = ? ORDER BY started_at DESC LIMIT ?
                """,
                (task_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_notifications(
        self,
        *,
        tenant_id: str,
        user_id: str,
        unread_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = ?", "user_id = ?"]
        params: list[Any] = [tenant_id, user_id]
        if unread_only:
            clauses.append("is_read = 0")
        params.append(max(1, min(int(limit), 1000)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    n.*,
                    t.source_session_id AS source_session_id,
                    t.execution_session_id AS execution_session_id
                FROM scheduled_notifications AS n
                LEFT JOIN scheduled_tasks AS t ON t.id = n.task_id
                WHERE {' AND '.join(f'n.{clause}' for clause in clauses)}
                ORDER BY n.created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def unread_count(self, *, tenant_id: str, user_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM scheduled_notifications
                WHERE tenant_id = ? AND user_id = ? AND is_read = 0
                """,
                (tenant_id, user_id),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def mark_notification_read(
        self,
        notification_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_notifications SET is_read = 1
                WHERE id = ? AND tenant_id = ? AND user_id = ?
                """,
                (notification_id, tenant_id, user_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def mark_all_notifications_read(self, *, tenant_id: str, user_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_notifications SET is_read = 1
                WHERE tenant_id = ? AND user_id = ? AND is_read = 0
                """,
                (tenant_id, user_id),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def delete_tenant_data(self, *, tenant_id: str) -> dict[str, int]:
        """Permanently remove all scheduled-task records owned by one tenant."""

        normalized_tenant = str(tenant_id or "").strip()
        if not normalized_tenant:
            raise ValueError("tenant_id is required")
        deleted: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for table in (
                "scheduled_notifications",
                "scheduled_task_drafts",
                "scheduled_task_preferences",
                "scheduled_task_runs",
                "scheduled_tasks",
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ?",
                    (normalized_tenant,),
                )
                deleted[table] = int(cursor.rowcount or 0)
            conn.commit()
        return deleted

    def delete_workspace_data(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, int]:
        """Permanently remove scheduled records for one exact workspace owner."""

        owner = tuple(str(value or "").strip() for value in (tenant_id, user_id, workspace_id))
        if any(not value for value in owner):
            raise ValueError("tenant_id, user_id and workspace_id are required")
        deleted = {
            "scheduled_notifications": 0,
            "scheduled_task_drafts": 0,
            "scheduled_task_runs": 0,
            "scheduled_tasks": 0,
        }
        with self._lock, self._connect() as conn:
            task_rows = conn.execute(
                """
                SELECT id FROM scheduled_tasks
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                owner,
            ).fetchall()
            task_ids = [str(row["id"] or "") for row in task_rows if str(row["id"] or "")]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                cursor = conn.execute(
                    f"DELETE FROM scheduled_notifications WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                )
                deleted["scheduled_notifications"] = int(cursor.rowcount or 0)
                cursor = conn.execute(
                    f"DELETE FROM scheduled_task_runs WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                )
                deleted["scheduled_task_runs"] = int(cursor.rowcount or 0)
            cursor = conn.execute(
                """
                DELETE FROM scheduled_task_drafts
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                owner,
            )
            deleted["scheduled_task_drafts"] = int(cursor.rowcount or 0)
            cursor = conn.execute(
                """
                DELETE FROM scheduled_tasks
                WHERE tenant_id = ? AND user_id = ? AND workspace_id = ?
                """,
                owner,
            )
            deleted["scheduled_tasks"] = int(cursor.rowcount or 0)
            conn.commit()
        return deleted

    def tenant_has_inflight_runs(self, *, tenant_id: str) -> bool:
        normalized_tenant = str(tenant_id or "").strip()
        if not normalized_tenant:
            raise ValueError("tenant_id is required")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM scheduled_tasks tasks
                LEFT JOIN scheduled_task_runs runs
                  ON runs.task_id = tasks.id AND runs.status = 'running'
                WHERE tasks.tenant_id = ?
                  AND (tasks.claimed_at IS NOT NULL OR runs.id IS NOT NULL)
                LIMIT 1
                """,
                (normalized_tenant,),
            ).fetchone()
        return row is not None

    def set_tenant_suspended(self, *, tenant_id: str, suspended: bool) -> int:
        """Pause or resume only tasks whose state was changed by tenant suspension."""

        normalized_tenant = str(tenant_id or "").strip()
        if not normalized_tenant:
            raise ValueError("tenant_id is required")
        source, target = (
            ("active", "tenant_suspended")
            if suspended
            else ("tenant_suspended", "active")
        )
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, claimed_at = NULL, updated_at = ?
                WHERE tenant_id = ? AND status = ? AND deleted_at IS NULL
                """,
                (target, time.time(), normalized_tenant, source),
            )
            conn.commit()
            return int(cursor.rowcount or 0)


_STORE: ScheduledTaskStore | None = None
_STORE_KEY = ""


def get_scheduled_task_store() -> ScheduledTaskStore:
    global _STORE, _STORE_KEY
    key = str(os.environ.get("TCODE_WORKDIR") or os.getcwd()).strip()
    if _STORE is None or _STORE_KEY != key:
        _STORE = ScheduledTaskStore()
        _STORE_KEY = key
    return _STORE
