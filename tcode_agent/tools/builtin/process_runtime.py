"""Shared runtime for long-running process management."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Optional

from ...runtime.sandbox import get_sandbox_runtime
from ...runtime.output_budget import bound_text
from ...runtime.local_process import start_local_process, stop_local_process
from ...runtime.sandbox.base import SandboxJob, TERMINAL_STATUSES


def _managed_output_max_chars() -> int:
    try:
        value = int(os.environ.get("TCODE_MANAGED_PROCESS_OUTPUT_MAX_CHARS", "1000000"))
    except Exception:
        value = 1_000_000
    return max(4096, min(value, 8 * 1024 * 1024))


def _bounded_managed_lines(
    values: list[Any],
    *,
    max_lines: int,
    max_chars: int,
) -> list[str]:
    """Normalize persisted output so legacy state cannot bypass new limits."""

    selected: list[str] = []
    total = 0
    for value in reversed(values[-max_lines:]):
        line = bound_text(
            value,
            max_chars=max_chars,
            label="managed process line truncated",
        ).text
        if selected and total + len(line) > max_chars:
            break
        selected.append(line)
        total += len(line)
        if total >= max_chars:
            break
    return list(reversed(selected))


class SandboxControlPlaneError(RuntimeError):
    """The sandbox job may still exist, but its control plane is unavailable."""

    def __init__(
        self,
        *,
        job_id: str,
        phase: str,
        attempts: int,
        cause: BaseException,
    ):
        self.job_id = str(job_id or "unknown")
        self.phase = str(phase or "unknown")
        self.attempts = max(1, int(attempts))
        self.cause_type = type(cause).__name__
        detail = str(cause or "").strip() or "no exception message"
        super().__init__(
            f"Sandbox control plane failed during {self.phase} for "
            f"job {self.job_id} after {self.attempts} attempt(s) "
            f"({self.cause_type}: {detail})"
        )


@dataclass
class ManagedProcess:
    process_id: str
    command: str
    workdir: Optional[str]
    proc: Optional[asyncio.subprocess.Process] = None
    status: str = "running"
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    finished_at: Optional[float] = None
    returncode: Optional[int] = None
    output_lines: list[str] = field(default_factory=list)
    output_seq: int = 0
    max_lines: int = 2000
    max_chars: int = 1_000_000
    output_chars: int = 0
    output_callback: Any = field(default=None, repr=False, compare=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    drain_tasks: list[asyncio.Task] = field(default_factory=list, repr=False)
    exit_task: Optional[asyncio.Task] = field(default=None, repr=False)
    sandbox_job_id: str = ""
    sandbox_container_id: str = ""
    sandbox_container_name: str = ""
    sandbox_metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    session_id: str = ""

    async def append_output(self, source: str, text: str):
        if not text:
            return
        emitted: list[tuple[str, str, int]] = []
        async with self.lock:
            for line in text.splitlines():
                projected = bound_text(
                    f"[{source}] {line}",
                    max_chars=self.max_chars,
                    label="managed process line truncated",
                ).text
                self.output_seq += 1
                self.output_lines.append(projected)
                self.output_chars += len(projected)
                emitted.append((source, line[:4096], self.output_seq))
            if len(self.output_lines) > self.max_lines:
                removed = self.output_lines[: -self.max_lines]
                self.output_lines = self.output_lines[-self.max_lines :]
                self.output_chars -= sum(len(item) for item in removed)
            while len(self.output_lines) > 1 and self.output_chars > self.max_chars:
                self.output_chars -= len(self.output_lines.pop(0))
        callback = self.output_callback
        if callable(callback):
            for emitted_source, emitted_line, emitted_seq in emitted:
                try:
                    result = callback(emitted_source, emitted_line, emitted_seq)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    continue

    async def snapshot_output(self, lines: int = 200) -> str:
        limit = max(1, min(5000, lines))
        async with self.lock:
            data = self.output_lines[-limit:]
        return "\n".join(data)


class ProcessRuntime:
    """Global runtime for process lifecycle and output capture."""

    _processes: dict[str, ManagedProcess] = {}
    _lock = asyncio.Lock()
    _loaded = False

    @classmethod
    def _registry_path(cls) -> Path:
        configured = str(os.environ.get("TCODE_PROCESS_REGISTRY_FILE", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        root = Path(str(os.environ.get("TCODE_WORKDIR") or os.getcwd()))
        return root / ".tcode" / "processes.json"

    @classmethod
    def _serialize(cls, managed: ManagedProcess) -> dict[str, Any]:
        output_lines = _bounded_managed_lines(
            list(managed.output_lines),
            max_lines=managed.max_lines,
            max_chars=managed.max_chars,
        )
        return {
            "process_id": managed.process_id,
            "command": managed.command,
            "workdir": managed.workdir,
            "status": managed.status,
            "created_at": managed.created_at,
            "finished_at": managed.finished_at,
            "returncode": managed.returncode,
            "output_lines": output_lines,
            "output_seq": managed.output_seq,
            "max_lines": managed.max_lines,
            "max_chars": managed.max_chars,
            "sandbox_job_id": managed.sandbox_job_id,
            "sandbox_container_id": managed.sandbox_container_id,
            "sandbox_container_name": managed.sandbox_container_name,
            "sandbox_metadata": dict(managed.sandbox_metadata or {}),
            "tenant_id": managed.tenant_id,
            "user_id": managed.user_id,
            "workspace_id": managed.workspace_id,
            "session_id": managed.session_id,
        }

    @classmethod
    def _deserialize(cls, raw: dict[str, Any]) -> ManagedProcess:
        sandbox_job_id = str(raw.get("sandbox_job_id") or "")
        status = str(raw.get("status") or "missing")
        if not sandbox_job_id and status not in TERMINAL_STATUSES:
            status = "missing"
        try:
            max_lines = max(100, min(10000, int(raw.get("max_lines") or 2000)))
        except (TypeError, ValueError):
            max_lines = 2000
        try:
            max_chars = max(
                4096,
                min(8 * 1024 * 1024, int(raw.get("max_chars") or 1_000_000)),
            )
        except (TypeError, ValueError):
            max_chars = 1_000_000
        raw_lines = raw.get("output_lines")
        output_lines = _bounded_managed_lines(
            list(raw_lines) if isinstance(raw_lines, list) else [],
            max_lines=max_lines,
            max_chars=max_chars,
        )
        return ManagedProcess(
            process_id=str(raw.get("process_id") or ""),
            command=str(raw.get("command") or ""),
            workdir=str(raw.get("workdir") or "") or None,
            status=status,
            created_at=float(raw.get("created_at") or datetime.now().timestamp()),
            finished_at=(
                float(raw["finished_at"])
                if raw.get("finished_at") is not None
                else None
            ),
            returncode=(
                int(raw["returncode"])
                if isinstance(raw.get("returncode"), int)
                else None
            ),
            output_lines=output_lines,
            output_chars=sum(len(item) for item in output_lines),
            output_seq=max(0, int(raw.get("output_seq") or 0)),
            max_lines=max_lines,
            max_chars=max_chars,
            sandbox_job_id=sandbox_job_id,
            sandbox_container_id=str(raw.get("sandbox_container_id") or ""),
            sandbox_container_name=str(raw.get("sandbox_container_name") or ""),
            sandbox_metadata=dict(raw.get("sandbox_metadata") or {}),
            tenant_id=str(raw.get("tenant_id") or ""),
            user_id=str(raw.get("user_id") or ""),
            workspace_id=str(raw.get("workspace_id") or ""),
            session_id=str(raw.get("session_id") or ""),
        )

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return
        path = cls._registry_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        cls._processes = {}
        if isinstance(data, dict):
            for key, raw in data.items():
                if not isinstance(raw, dict):
                    continue
                managed = cls._deserialize(raw)
                if managed.process_id:
                    cls._processes[str(key)] = managed
        cls._loaded = True

    @classmethod
    def _save(cls) -> None:
        path = cls._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(
                {
                    process_id: cls._serialize(managed)
                    for process_id, managed in cls._processes.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    @staticmethod
    def owner_from_context(context: Any) -> dict[str, str]:
        extra = getattr(context, "extra", None)
        extra = extra if isinstance(extra, dict) else {}
        return {
            "tenant_id": str(extra.get("tenant_id") or ""),
            "user_id": str(extra.get("user_id") or ""),
            "workspace_id": str(extra.get("workspace_id") or ""),
            "session_id": str(getattr(context, "session_id", "") or ""),
        }

    @staticmethod
    def _matches_owner(managed: ManagedProcess, owner: Optional[dict[str, str]]) -> bool:
        if not owner:
            return True
        for key in ("tenant_id", "user_id", "workspace_id", "session_id"):
            expected = str(owner.get(key) or "").strip()
            actual = str(getattr(managed, key, "") or "").strip()
            if expected and actual != expected:
                return False
        return True

    @classmethod
    async def terminate_for_sessions(
        cls,
        session_ids: list[str] | tuple[str, ...] | set[str],
        *,
        owner: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Terminate active processes owned by the selected sessions."""
        selected = {
            str(session_id or "").strip()
            for session_id in session_ids
            if str(session_id or "").strip()
        }
        if not selected:
            return {"matched": [], "terminated": [], "failed": {}}

        owner_without_session = {
            key: str(value or "").strip()
            for key, value in dict(owner or {}).items()
            if key in {"tenant_id", "user_id", "workspace_id"}
            and str(value or "").strip()
        }
        async with cls._lock:
            cls._load()
            matched = [
                managed
                for managed in cls._processes.values()
                if str(managed.session_id or "").strip() in selected
                and cls._matches_owner(managed, owner_without_session)
                and str(managed.status or "").strip().lower() not in TERMINAL_STATUSES
            ]

        terminated: list[str] = []
        failed: dict[str, str] = {}
        for managed in matched:
            process_owner = {
                **owner_without_session,
                "session_id": str(managed.session_id or "").strip(),
            }
            try:
                stopped = await cls.terminate(
                    managed.process_id,
                    owner=process_owner,
                )
                if stopped:
                    terminated.append(managed.process_id)
                else:
                    failed[managed.process_id] = (
                        "process ownership changed or exit was not confirmed"
                    )
            except Exception as exc:
                failed[managed.process_id] = str(exc)

        return {
            "matched": [managed.process_id for managed in matched],
            "terminated": terminated,
            "failed": failed,
        }

    @classmethod
    async def purge_for_sessions(
        cls,
        session_ids: list[str] | tuple[str, ...] | set[str],
        *,
        owner: Optional[dict[str, str]] = None,
    ) -> list[str]:
        """Remove terminal process records after their sessions are deleted."""
        selected = {
            str(session_id or "").strip()
            for session_id in session_ids
            if str(session_id or "").strip()
        }
        if not selected:
            return []
        owner_without_session = {
            key: str(value or "").strip()
            for key, value in dict(owner or {}).items()
            if key in {"tenant_id", "user_id", "workspace_id"}
            and str(value or "").strip()
        }
        async with cls._lock:
            cls._load()
            removed = sorted(
                process_id
                for process_id, managed in cls._processes.items()
                if str(managed.session_id or "").strip() in selected
                and cls._matches_owner(managed, owner_without_session)
            )
            for process_id in removed:
                cls._processes.pop(process_id, None)
            if removed:
                cls._save()
        return removed

    @classmethod
    async def start(
        cls,
        command: str,
        workdir: Optional[str],
        *,
        owner: Optional[dict[str, str]] = None,
        output_callback: Any = None,
    ) -> ManagedProcess:
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            raise RuntimeError('桌面本地命令必须通过原生隔离执行器启动')
        process_id = str(uuid.uuid4())
        managed = ManagedProcess(
            process_id=process_id,
            command=command,
            workdir=workdir,
            output_callback=output_callback,
            max_chars=_managed_output_max_chars(),
            **dict(owner or {}),
        )
        proc = await start_local_process(
            command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        managed.proc = proc
        try:
            async with cls._lock:
                cls._load()
                cls._processes[process_id] = managed
                cls._save()
        except BaseException:
            cls._processes.pop(process_id, None)
            if not await stop_local_process(proc, grace=0, discard_output=True):
                raise RuntimeError("本地进程登记失败，且无法确认进程已退出")
            raise

        managed.drain_tasks = [
            asyncio.create_task(cls._drain_stream(managed, managed.proc.stdout, "stdout")),
            asyncio.create_task(cls._drain_stream(managed, managed.proc.stderr, "stderr")),
        ]
        managed.exit_task = asyncio.create_task(cls._watch_exit(managed))
        return managed

    @classmethod
    async def register_sandbox_job(
        cls,
        *,
        job: SandboxJob,
        command: str,
        workdir: Optional[str],
        owner: Optional[dict[str, str]] = None,
        output_callback: Any = None,
    ) -> ManagedProcess:
        process_id = job.process_id or job.sandbox_job_id or str(uuid.uuid4())
        managed = ManagedProcess(
            process_id=process_id,
            command=command,
            workdir=workdir,
            proc=None,
            status=job.status,
            sandbox_job_id=job.sandbox_job_id,
            sandbox_container_id=job.container_id,
            sandbox_container_name=job.container_name,
            sandbox_metadata=dict(job.metadata or {}),
            output_callback=output_callback,
            max_chars=_managed_output_max_chars(),
            **dict(owner or {}),
        )
        async with cls._lock:
            cls._load()
            cls._processes[process_id] = managed
            cls._save()
        return managed

    @classmethod
    async def _drain_stream(
        cls,
        managed: ManagedProcess,
        stream: Optional[asyncio.StreamReader],
        source: str,
    ):
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    break
                await managed.append_output(source, chunk.decode("utf-8", errors="replace"))
        except Exception:
            return

    @classmethod
    async def _watch_exit(cls, managed: ManagedProcess):
        if managed.proc is None:
            return
        try:
            rc = await managed.proc.wait()
            await cls._finalize_local_process(managed, rc)
        except Exception:
            if managed.status not in TERMINAL_STATUSES:
                managed.status = "exit_unconfirmed"
            async with cls._lock:
                cls._save()

    @classmethod
    async def _finalize_local_process(cls, managed: ManagedProcess, rc: int) -> None:
        managed.returncode = rc
        managed.finished_at = managed.finished_at or datetime.now().timestamp()
        if managed.status not in {"terminated", "terminating"}:
            managed.status = "completed" if rc == 0 else "failed"
        if managed.drain_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*managed.drain_tasks, return_exceptions=True),
                    timeout=1,
                )
            except asyncio.TimeoutError:
                pass
        managed.output_callback = None
        async with cls._lock:
            cls._save()

    @classmethod
    async def get(
        cls,
        process_id: str,
        *,
        owner: Optional[dict[str, str]] = None,
    ) -> Optional[ManagedProcess]:
        async with cls._lock:
            cls._load()
            managed = cls._processes.get(process_id)
            return managed if managed and cls._matches_owner(managed, owner) else None

    @classmethod
    async def list(
        cls,
        *,
        owner: Optional[dict[str, str]] = None,
    ) -> list[ManagedProcess]:
        async with cls._lock:
            cls._load()
            return [
                managed
                for managed in cls._processes.values()
                if cls._matches_owner(managed, owner)
            ]

    @classmethod
    async def terminate(
        cls,
        process_id: str,
        *,
        owner: Optional[dict[str, str]] = None,
    ) -> bool:
        managed = await cls.get(process_id, owner=owner)
        if not managed:
            return False
        if managed.sandbox_job_id:
            status = await get_sandbox_runtime().terminate_job(
                SandboxJob(
                    sandbox_job_id=managed.sandbox_job_id,
                    process_id=managed.process_id,
                    container_id=managed.sandbox_container_id,
                    container_name=managed.sandbox_container_name,
                    command=managed.command,
                    status=managed.status,
                    metadata=dict(managed.sandbox_metadata or {}),
                )
            )
            managed.status = status.status
            managed.finished_at = datetime.now().timestamp()
            managed.sandbox_metadata.update(dict(status.metadata or {}))
            async with cls._lock:
                cls._save()
            return status.status in {"terminated", "missing"}
        if managed.proc is None:
            managed.status = "missing"
            managed.finished_at = datetime.now().timestamp()
            async with cls._lock:
                cls._save()
            return False
        async with managed.stop_lock:
            if managed.status in TERMINAL_STATUSES:
                return True
            managed.status = "terminating"
            if not await stop_local_process(managed.proc):
                return False
            managed.status = "terminated"
            managed.returncode = managed.proc.returncode
            managed.finished_at = datetime.now().timestamp()
            async with cls._lock:
                cls._save()
            return True

    @classmethod
    def is_sandbox(cls, managed: ManagedProcess) -> bool:
        return bool(managed.sandbox_job_id)

    @staticmethod
    def describe_error(exc: BaseException) -> str:
        detail = str(exc or "").strip() or "no exception message"
        return f"{type(exc).__name__}: {detail}"

    @staticmethod
    def _persistent_job_dir(managed: ManagedProcess) -> Path | None:
        metadata = dict(managed.sandbox_metadata or {})
        job_id = str(managed.sandbox_job_id or "").strip()
        if (
            not job_id.startswith("sbx_")
            or len(job_id) > 80
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in job_id
            )
        ):
            return None
        workspace_text = str(metadata.get("persistent_host_workspace") or "").strip()
        job_dir_text = str(metadata.get("persistent_job_dir") or "").strip()
        if not workspace_text or not job_dir_text:
            return None
        workspace = Path(workspace_text).expanduser().absolute()
        expected = workspace / ".tcode-runtime" / "jobs" / job_id
        configured = Path(job_dir_text).expanduser().absolute()
        if configured != expected:
            return None
        # The control files are writable from the sandbox. Refuse symlinked
        # directory components before reading them from the host process.
        try:
            for directory in (
                workspace / ".tcode-runtime",
                workspace / ".tcode-runtime" / "jobs",
                expected,
            ):
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    return None
        except OSError:
            return None
        return expected

    @staticmethod
    def _read_job_control_file(
        job_dir: Path,
        name: str,
        *,
        max_bytes: int,
    ) -> str | None:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_fd = os.open(job_dir, directory_flags)
        try:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > max(1, int(max_bytes))
            ):
                return None
            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_fd = os.open(name, file_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    return None
                chunks: list[bytes] = []
                remaining = max(1, int(max_bytes))
                while remaining > 0:
                    chunk = os.read(file_fd, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)

    @classmethod
    def _persistent_job_snapshot(
        cls,
        managed: ManagedProcess,
    ) -> tuple[str, Optional[int], str] | None:
        job_dir = cls._persistent_job_dir(managed)
        if job_dir is None:
            return None
        try:
            terminated = cls._read_job_control_file(
                job_dir,
                "terminated",
                max_bytes=1024,
            )
            exit_text = cls._read_job_control_file(
                job_dir,
                "exit_code",
                max_bytes=1024,
            )
            try:
                persistent_output_limit = int(
                    os.environ.get("TCODE_PERSISTENT_JOB_OUTPUT_MAX_BYTES", str(2 * 1024 * 1024))
                )
            except Exception:
                persistent_output_limit = 2 * 1024 * 1024
            persistent_output_limit = max(
                64 * 1024,
                min(persistent_output_limit, 32 * 1024 * 1024),
            )
            output = cls._read_job_control_file(
                job_dir,
                "output.log",
                max_bytes=persistent_output_limit,
            ) or ""
        except OSError:
            return None
        if terminated is not None:
            return "terminated", None, output
        if exit_text is None:
            return None
        try:
            exit_code = int(exit_text.strip())
        except Exception:
            return None
        return ("completed" if exit_code == 0 else "failed"), exit_code, output

    @classmethod
    async def reconcile_persistent_job(
        cls,
        managed: ManagedProcess,
        *,
        phase: str,
        error: BaseException | None = None,
    ) -> bool:
        snapshot = cls._persistent_job_snapshot(managed)
        if snapshot is None:
            return False
        status, exit_code, output = snapshot
        managed.status = status
        managed.returncode = exit_code
        managed.finished_at = managed.finished_at or datetime.now().timestamp()
        managed.sandbox_metadata.update(
            {
                "sandbox_control_plane_recovered": True,
                "sandbox_recovery_source": "persistent_job_files",
                "sandbox_recovery_phase": str(phase or "unknown"),
            }
        )
        if error is not None:
            managed.sandbox_metadata["sandbox_control_plane_last_error"] = (
                cls.describe_error(error)
            )
        current = str(output or "").splitlines()
        previous = [
            line[len("[sandbox] ") :]
            for line in managed.output_lines
            if line.startswith("[sandbox] ")
        ]
        delta = cls._tail_delta(previous, current)
        if delta:
            await managed.append_output("sandbox", "\n".join(delta))
        async with cls._lock:
            cls._save()
        return True

    @classmethod
    async def refresh(
        cls,
        managed: ManagedProcess,
        *,
        runtime: Any = None,
    ) -> ManagedProcess:
        if not managed.sandbox_job_id:
            return managed
        selected_runtime = runtime or get_sandbox_runtime()
        try:
            status = await selected_runtime.job_status(
                SandboxJob(
                    sandbox_job_id=managed.sandbox_job_id,
                    process_id=managed.process_id,
                    container_id=managed.sandbox_container_id,
                    container_name=managed.sandbox_container_name,
                    command=managed.command,
                    status=managed.status,
                    metadata=dict(managed.sandbox_metadata or {}),
                )
            )
        except Exception as exc:
            if await cls.reconcile_persistent_job(
                managed,
                phase="job_status",
                error=exc,
            ):
                return managed
            raise
        managed.status = status.status
        managed.returncode = status.exit_code
        if status.status in TERMINAL_STATUSES and managed.finished_at is None:
            managed.finished_at = datetime.now().timestamp()
        managed.sandbox_metadata.update(dict(status.metadata or {}))
        async with cls._lock:
            cls._save()
        return managed

    @classmethod
    async def output(
        cls,
        managed: ManagedProcess,
        lines: int = 200,
        *,
        runtime: Any = None,
    ) -> str:
        if managed.sandbox_job_id:
            selected_runtime = runtime or get_sandbox_runtime()
            try:
                return await selected_runtime.job_output(
                    SandboxJob(
                        sandbox_job_id=managed.sandbox_job_id,
                        process_id=managed.process_id,
                        container_id=managed.sandbox_container_id,
                        container_name=managed.sandbox_container_name,
                        command=managed.command,
                        status=managed.status,
                        metadata=dict(managed.sandbox_metadata or {}),
                    ),
                    lines=lines,
                )
            except Exception as exc:
                if await cls.reconcile_persistent_job(
                    managed,
                    phase="job_output",
                    error=exc,
                ):
                    snapshot = cls._persistent_job_snapshot(managed)
                    if snapshot is not None:
                        output_lines = snapshot[2].splitlines()
                        limit = max(1, min(5000, int(lines or 200)))
                        return "\n".join(output_lines[-limit:])
                if managed.status in TERMINAL_STATUSES:
                    managed.sandbox_metadata.update(
                        {
                            "sandbox_output_unavailable": True,
                            "sandbox_control_plane_phase": "job_output",
                            "sandbox_control_plane_last_error": cls.describe_error(
                                exc
                            ),
                        }
                    )
                    return await managed.snapshot_output(lines=lines)
                raise
        return await managed.snapshot_output(lines=lines)

    @staticmethod
    def _sandbox_poll_interval_seconds() -> float:
        try:
            raw = float(os.environ.get("TCODE_TOOL_OUTPUT_POLL_SECONDS", "1"))
        except Exception:
            raw = 1.0
        return max(0.2, min(raw, 10.0))

    @staticmethod
    def _tail_delta(previous: list[str], current: list[str]) -> list[str]:
        if not current:
            return []
        if not previous:
            return current
        max_overlap = min(len(previous), len(current))
        for size in range(max_overlap, 0, -1):
            if previous[-size:] == current[:size]:
                return current[size:]
        return current

    @classmethod
    async def _capture_sandbox_output(
        cls,
        managed: ManagedProcess,
        previous: list[str],
        *,
        lines: int = 500,
        runtime: Any = None,
    ) -> list[str]:
        content = await cls.output(managed, lines=lines, runtime=runtime)
        current = str(content or "").splitlines()
        delta = cls._tail_delta(previous, current)
        if delta:
            await managed.append_output("sandbox", "\n".join(delta))
        return current

    @classmethod
    async def wait(cls, managed: ManagedProcess, timeout: int) -> ManagedProcess:
        if not managed.sandbox_job_id:
            if managed.proc is not None:
                if managed.exit_task is not None:
                    await asyncio.wait_for(asyncio.shield(managed.exit_task), timeout=timeout)
                else:
                    rc = await asyncio.wait_for(managed.proc.wait(), timeout=timeout)
                    await cls._finalize_local_process(managed, rc)
            return managed

        deadline = datetime.now().timestamp() + max(1, int(timeout))
        previous_output: list[str] = []
        poll_interval = cls._sandbox_poll_interval_seconds()
        runtime = get_sandbox_runtime()
        try:
            max_control_failures = int(
                os.environ.get("TCODE_SANDBOX_CONTROL_PLANE_MAX_FAILURES", "3")
            )
        except Exception:
            max_control_failures = 3
        max_control_failures = max(1, min(max_control_failures, 10))
        control_failures = 0
        last_control_error: BaseException | None = None
        while datetime.now().timestamp() < deadline:
            try:
                await cls.refresh(managed, runtime=runtime)
                control_failures = 0
            except Exception as exc:
                last_control_error = exc
                control_failures += 1
                managed.sandbox_metadata.update(
                    {
                        "sandbox_control_plane_failures": control_failures,
                        "sandbox_control_plane_phase": "job_status",
                        "sandbox_control_plane_last_error": cls.describe_error(exc),
                    }
                )
                if control_failures >= max_control_failures:
                    raise SandboxControlPlaneError(
                        job_id=managed.sandbox_job_id,
                        phase="job_status",
                        attempts=control_failures,
                        cause=exc,
                    ) from exc
                await asyncio.sleep(poll_interval)
                continue
            if managed.status in TERMINAL_STATUSES:
                try:
                    await cls._capture_sandbox_output(
                        managed,
                        previous_output,
                        lines=5000,
                        runtime=runtime,
                    )
                except Exception as exc:
                    # A terminal status is authoritative. Preserve completion and
                    # return whatever output was already captured instead of
                    # stranding the Agent turn on a final output read.
                    managed.sandbox_metadata.update(
                        {
                            "sandbox_output_unavailable": True,
                            "sandbox_control_plane_phase": "final_job_output",
                            "sandbox_control_plane_last_error": cls.describe_error(
                                exc
                            ),
                        }
                    )
                managed.output_callback = None
                async with cls._lock:
                    cls._save()
                return managed
            try:
                previous_output = await cls._capture_sandbox_output(
                    managed,
                    previous_output,
                    runtime=runtime,
                )
            except Exception as exc:
                last_control_error = exc
                managed.sandbox_metadata.update(
                    {
                        "sandbox_output_poll_failed": True,
                        "sandbox_control_plane_phase": "job_output",
                        "sandbox_control_plane_last_error": cls.describe_error(exc),
                    }
                )
            await asyncio.sleep(poll_interval)
        if await cls.reconcile_persistent_job(
            managed,
            phase="wait_deadline",
            error=last_control_error,
        ):
            managed.output_callback = None
            return managed
        try:
            await cls._capture_sandbox_output(
                managed,
                previous_output,
                lines=5000,
                runtime=runtime,
            )
        except Exception as exc:
            managed.sandbox_metadata.update(
                {
                    "sandbox_output_unavailable": True,
                    "sandbox_control_plane_phase": "deadline_job_output",
                    "sandbox_control_plane_last_error": cls.describe_error(exc),
                }
            )
        raise asyncio.TimeoutError()
