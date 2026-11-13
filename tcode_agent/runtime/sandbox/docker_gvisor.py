from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...browser.live import cleanup_browser_live_workspace
from ..output_budget import BoundedBytesBuffer
from .base import (
    SandboxConfig,
    SandboxJob,
    SandboxJobStatus,
    SandboxResult,
    SandboxSpec,
    root_runtime_profile_name,
    root_runtime_user_allowed,
)
from .workspace_snapshot import WorkspaceSnapshotManager
from .service_policy import WorkspaceServicePolicy
from .runtime_registry import PersistentRuntimeRecord, PersistentRuntimeRegistry
from .workspace_protection import workspace_protection_contract


_ROOT_PACKAGE_CAPABILITIES = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SETFCAP",
    "SYS_CHROOT",
)

_PERSISTENT_JOB_ONLY_ENV_KEYS = frozenset(
    {
        # The browser worker configuration contains the browser/agent session
        # ids and per-request viewport/timeouts. Persistent jobs receive their
        # own env via `docker exec -e`; changing this value must not invalidate
        # the reusable container profile.
        "TCODE_BROWSER_WORKER_CONFIG",
    }
)

_STALE_WORKSPACE_MOUNT_ERROR_MARKERS = (
    "current working directory is outside of container mount namespace root",
    "possible container breakout detected",
)


_PERSISTENT_OUTPUT_CAPTURE_SCRIPT = r"""
import os
import subprocess
import sys
import time

output_path, pid_path, exit_path, starting_path, max_bytes_text, command = sys.argv[1:7]
max_bytes = max(65536, int(max_bytes_text))
head_limit = max(1, int(max_bytes * 0.7))
tail_limit = max(1, max_bytes - head_limit)
head = bytearray()
tail = bytearray()
total = 0
job_dir = os.path.dirname(output_path)
dir_fd = os.open(job_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))

def unlink_name(path):
    try:
        os.unlink(os.path.basename(path), dir_fd=dir_fd)
    except FileNotFoundError:
        pass

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]

for path in (output_path, pid_path, exit_path):
    unlink_name(path)

output_fd = os.open(
    os.path.basename(output_path),
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
    dir_fd=dir_fd,
)
pid_fd = os.open(
    os.path.basename(pid_path),
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
    dir_fd=dir_fd,
)
write_all(pid_fd, str(os.getpid()).encode("ascii"))
os.close(pid_fd)
unlink_name(starting_path)

def append(data):
    global total
    total += len(data)
    if len(head) < head_limit:
        take = min(head_limit - len(head), len(data))
        head.extend(data[:take])
        data = data[take:]
    if data:
        tail.extend(data)
        if len(tail) > tail_limit:
            del tail[:len(tail) - tail_limit]

def rendered():
    if total <= max_bytes:
        return bytes(head + tail)
    marker = ("\n...[persistent output truncated: original_bytes=%d]...\n" % total).encode("ascii")
    available = max(0, max_bytes - len(marker))
    keep_head = min(len(head), int(available * 0.7))
    keep_tail = min(len(tail), max(0, available - keep_head))
    return bytes(head[:keep_head]) + marker + (bytes(tail[-keep_tail:]) if keep_tail else b"")

def snapshot():
    data = rendered()
    os.lseek(output_fd, 0, os.SEEK_SET)
    write_all(output_fd, data)
    os.ftruncate(output_fd, len(data))

proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    bufsize=0,
)
last_snapshot = 0.0
while True:
    chunk = os.read(proc.stdout.fileno(), 65536)
    if not chunk:
        break
    append(chunk)
    now = time.monotonic()
    if now - last_snapshot >= 0.5:
        snapshot()
        last_snapshot = now
return_code = int(proc.wait())
snapshot()
os.fsync(output_fd)
os.close(output_fd)
exit_fd = os.open(
    os.path.basename(exit_path),
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
    dir_fd=dir_fd,
)
write_all(exit_fd, str(return_code).encode("ascii"))
os.close(exit_fd)
os.close(dir_fd)
sys.exit(return_code)
""".strip()


class DockerGvisorRuntime:
    """Docker CLI backed sandbox runtime using gVisor runsc by default."""

    def __init__(self, config: SandboxConfig):
        self.config = config
        self.snapshot_manager = (
            WorkspaceSnapshotManager(
                config.snapshot_root or "/tmp/tcode-sandbox-runner/snapshots",
                config.conflict_root or "/tmp/tcode-sandbox-runner/conflicts",
            )
            if config.workspace_snapshots
            else None
        )
        self.job_log_root = Path(
            config.job_log_root or "/tmp/tcode-sandbox-runner/logs"
        ).expanduser().resolve(strict=False)
        self.job_log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.service_policy = WorkspaceServicePolicy(
            config.workspace_service_policy_file
        )
        self._finalize_locks: dict[str, asyncio.Lock] = {}
        self._finalized_jobs: dict[str, dict[str, Any]] = {}
        self.runtime_registry = PersistentRuntimeRegistry(
            config.runtime_state_file
            or "/tmp/tcode-sandbox-runner/runtimes.sqlite"
        )
        self._runtime_locks: dict[str, asyncio.Lock] = {}

    def _workspace_mask_root(self, workspace: Path) -> Path:
        configured = str(
            os.environ.get("TCODE_SANDBOX_WORKSPACE_MASK_ROOT", "") or ""
        ).strip()
        if configured:
            root = Path(configured).expanduser().resolve(strict=False)
            try:
                root.relative_to(workspace)
            except Exception:
                return root
            raise ValueError(
                "TCODE_SANDBOX_WORKSPACE_MASK_ROOT must be outside the Agent workspace"
            )

        runtime_state = Path(
            self.config.runtime_state_file
            or "/tmp/tcode-sandbox-runner/runtimes.sqlite"
        ).expanduser().resolve(strict=False)
        candidates = (
            runtime_state.parent / "workspace-masks",
            Path("/var/tmp/tcode-sandbox-runner/workspace-masks"),
            Path("/tmp/tcode-sandbox-runner/workspace-masks"),
        )
        for candidate in candidates:
            root = candidate.expanduser().resolve(strict=False)
            try:
                root.relative_to(workspace)
            except Exception:
                return root
        raise ValueError(
            "workspace protection mask root must be outside the Agent workspace"
        )

    @staticmethod
    def _protected_host_path(workspace: Path, relative_path: str) -> Path | None:
        current = workspace
        for part in Path(relative_path).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(
                    f"protected workspace path cannot contain symlinks: {relative_path}"
                )
            if not current.exists():
                return None
        return current

    def _workspace_protection_mounts(
        self,
        spec: SandboxSpec,
    ) -> list[tuple[Path, str, str]]:
        workspace = spec.pathmap.host_workspace.expanduser().resolve(strict=True)
        container_workspace = (
            spec.pathmap.container_workspace.rstrip("/") or "/workspace"
        )
        contract = workspace_protection_contract()
        mounts: list[tuple[Path, str, str]] = []
        mask_root: Path | None = None
        browser_worker = (
            spec.tool_name == "browser"
            and spec.lifecycle == "persistent"
            and spec.metadata.get("sandbox_profile") == "browser"
            and spec.metadata.get("browser_live") is True
            and spec.metadata.get("browser_action") == "live_worker"
        )

        for relative_path in contract["hidden"]:
            target = self._protected_host_path(workspace, relative_path)
            if target is None:
                continue
            if mask_root is None:
                mask_root = self._workspace_mask_root(workspace)
                mask_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                mask_root.chmod(0o700)
            if target.is_dir():
                source = mask_root / (
                    "browser-control-parent"
                    if relative_path == ".tcode" and browser_worker
                    else "empty-directory"
                )
                source.mkdir(exist_ok=True, mode=0o555)
                if relative_path == ".tcode" and browser_worker:
                    source.chmod(0o755)
                    (source / "browser").mkdir(exist_ok=True, mode=0o555)
                source.chmod(0o555)
            elif target.is_file():
                source = mask_root / "empty-file"
                source.touch(exist_ok=True, mode=0o444)
                source.chmod(0o444)
            else:
                raise ValueError(
                    f"protected workspace path has unsupported type: {relative_path}"
                )
            mounts.append(
                (
                    source,
                    f"{container_workspace}/{relative_path}",
                    "hidden",
                )
            )

        for relative_path in contract["read_only"]:
            target = self._protected_host_path(workspace, relative_path)
            if target is None:
                continue
            if not target.is_dir() and not target.is_file():
                raise ValueError(
                    f"protected workspace path has unsupported type: {relative_path}"
                )
            mounts.append(
                (
                    target,
                    f"{container_workspace}/{relative_path}",
                    "read_only",
                )
            )
        if browser_worker and ".tcode" in contract["hidden"]:
            browser_root = self._protected_host_path(workspace, ".tcode/browser")
            if browser_root is None or not browser_root.is_dir():
                raise ValueError("browser control directory is missing")
            mounts.append(
                (browser_root, f"{container_workspace}/.tcode/browser", "browser_control")
            )
        return mounts

    def _workspace_protection_fingerprint(
        self,
        spec: SandboxSpec,
    ) -> dict[str, object]:
        mounts = self._workspace_protection_mounts(spec)
        return {
            "contract": workspace_protection_contract(),
            "mounts": [
                {
                    "target": target,
                    "access": access,
                    "kind": "directory" if source.is_dir() else "file",
                }
                for source, target, access in mounts
            ],
        }

    def _container_name(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:18]}"

    @staticmethod
    def _label_value(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9_.:@/-]+", "-", str(value or "").strip())[:128]

    @staticmethod
    def _is_root_user(user: str) -> bool:
        normalized = str(user or "").strip().lower()
        return normalized in {"0", "0:0", "root"} or normalized.startswith("0:")

    @classmethod
    def _runtime_privilege(cls, spec: SandboxSpec) -> str:
        return "root" if cls._is_root_user(spec.user) else "standard"

    @staticmethod
    def _workspace_mount_identity(path: Path) -> str:
        info = path.resolve(strict=True).stat()
        return f"{info.st_dev}:{info.st_ino}"

    @staticmethod
    def _is_stale_workspace_mount_error(detail: str) -> bool:
        normalized = str(detail or "").strip().lower()
        return any(
            marker in normalized
            for marker in _STALE_WORKSPACE_MOUNT_ERROR_MARKERS
        )

    def _spec_fingerprint(self, spec: SandboxSpec) -> str:
        read_only = spec.read_only if spec.read_only is not None else self.config.read_only
        network = str(spec.network or self.config.network or "none").strip().lower()
        service_binding = (
            self.service_policy.binding_for(spec)
            if network in {"internal", "egress"}
            else None
        )
        payload = {
            "image": spec.image or self.config.image,
            "network": network,
            "user": spec.user or self.config.user,
            "read_only": bool(read_only),
            "home": spec.home or self.config.home,
            "tmpfs": spec.tmpfs or self.config.tmpfs,
            "shm_size": spec.shm_size or self.config.shm_size,
            "memory": spec.memory or self.config.memory,
            "cpus": spec.cpus or self.config.cpus,
            "pids_limit": spec.pids_limit or self.config.pids_limit,
            "container_workspace": spec.pathmap.container_workspace,
            "docker_runtime": self.config.docker_runtime,
            "egress_proxy_container": self.config.egress_proxy_container,
            "workspace_protection": self._workspace_protection_fingerprint(spec),
            "workspace_service_containers": (
                list(service_binding.containers) if service_binding is not None else []
            ),
            "workspace_service_env": (
                sorted(service_binding.env.items()) if service_binding is not None else []
            ),
            "idle_stop_seconds": max(0, int(spec.idle_stop_seconds or 0)),
            "retention_seconds": max(0, int(spec.retention_seconds or 0)),
            "env": sorted(
                (str(key), str(value))
                for key, value in spec.env.items()
                if str(key) not in _PERSISTENT_JOB_ONLY_ENV_KEYS
            ),
            "root_package_capabilities": (
                list(_ROOT_PACKAGE_CAPABILITIES)
                if self._is_root_user(spec.user or self.config.user) and not read_only
                else []
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _docker_run_args(self, spec: SandboxSpec, *, name: str, detach: bool, remove: bool) -> list[str]:
        cfg = self.config
        args = [
            cfg.docker_binary,
            "run",
            "--name",
            name,
            "--pull",
            "never",
            "--init",
        ]
        if remove:
            args.append("--rm")
        if detach:
            args.append("-d")
        if cfg.docker_runtime:
            args.extend(["--runtime", cfg.docker_runtime])
        args.extend(
            [
                "--log-driver",
                "json-file",
                "--log-opt",
                f"max-size={cfg.log_max_size}",
                "--log-opt",
                f"max-file={max(1, cfg.log_max_files)}",
            ]
        )
        args.extend(["--network", spec.network or cfg.network or "none"])
        read_only = spec.read_only if spec.read_only is not None else cfg.read_only
        if read_only:
            args.append("--read-only")
        if spec.tmpfs or cfg.tmpfs:
            args.extend(["--tmpfs", spec.tmpfs or cfg.tmpfs])
        if spec.shm_size or cfg.shm_size:
            args.extend(["--shm-size", spec.shm_size or cfg.shm_size])
        args.extend(["--cap-drop", "ALL"])
        if self._is_root_user(spec.user or cfg.user) and not read_only:
            for capability in _ROOT_PACKAGE_CAPABILITIES:
                args.extend(["--cap-add", capability])
        args.extend(["--security-opt", "no-new-privileges"])
        pids_limit = spec.pids_limit if spec.pids_limit > 0 else cfg.pids_limit
        if pids_limit > 0:
            args.extend(["--pids-limit", str(pids_limit)])
        if spec.memory or cfg.memory:
            args.extend(["--memory", spec.memory or cfg.memory])
        if spec.cpus or cfg.cpus:
            args.extend(["--cpus", spec.cpus or cfg.cpus])
        if spec.user or cfg.user:
            args.extend(["--user", spec.user or cfg.user])
        args.extend(["--ulimit", "nofile=1024:1024"])
        args.extend(["--label", "tcode.sandbox=true"])
        for key in (
            "tenant_id",
            "user_id",
            "workspace_id",
            "session_id",
            "sandbox_job_id",
            "runtime_id",
            "sandbox_profile",
            "runtime_privilege",
            "runtime_spec_fingerprint",
            "workspace_mount_identity",
        ):
            value = self._label_value(spec.metadata.get(key))
            if value:
                args.extend(["--label", f"tcode.{key}={value}"])
        if spec.lifecycle == "persistent":
            args.extend(["--label", "tcode.runtime=persistent"])
        env = {"HOME": spec.home or cfg.home, **spec.env}
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(
            [
                "-v",
                f"{spec.pathmap.host_workspace.as_posix()}:{spec.pathmap.container_workspace}:rw",
            ]
        )
        for mount in spec.mounts:
            mode = "ro" if mount.read_only else "rw"
            args.extend(["-v", f"{mount.source.as_posix()}:{mount.target}:{mode}"])
        for source, target, access in self._workspace_protection_mounts(spec):
            mode = "rw" if access == "browser_control" else "ro"
            args.extend(["-v", f"{source.as_posix()}:{target}:{mode}"])
        args.extend(
            [
                "-w",
                spec.container_cwd,
                spec.image or cfg.image,
                "bash",
                "-lc",
                (
                    'if [ -n "${TCODE_SANDBOX_PATH_PREPEND:-}" ]; then '
                    'export PATH="${TCODE_SANDBOX_PATH_PREPEND}:$PATH"; fi; '
                    f"{spec.command}"
                ),
            ]
        )
        return args

    @staticmethod
    def _persistent_requested(spec: SandboxSpec) -> bool:
        return str(spec.lifecycle or "ephemeral").strip().lower() == "persistent"

    @staticmethod
    def _runtime_identity(spec: SandboxSpec) -> tuple[str, str, str, str]:
        metadata = dict(spec.metadata or {})
        tenant_id = str(
            metadata.get("tenant_id")
            or os.environ.get("TCODE_DEFAULT_TENANT_ID")
            or "admin_tenant"
        ).strip()
        user_id = str(metadata.get("user_id") or "local-user").strip()
        workspace_id = str(
            metadata.get("workspace_id") or spec.pathmap.workspace_id
        ).strip()
        profile_name = str(metadata.get("sandbox_profile") or "default").strip()
        return tenant_id, user_id, workspace_id, profile_name

    @staticmethod
    def _cleanup_browser_live_for_runtime(
        record: PersistentRuntimeRecord,
    ) -> int:
        browser_profile = str(
            os.environ.get("TCODE_BROWSER_SANDBOX_PROFILE") or "browser"
        ).strip()
        if record.profile_name != browser_profile:
            return 0
        try:
            return cleanup_browser_live_workspace(record.host_workspace)
        except (OSError, ValueError):
            return 0

    @classmethod
    def _runtime_key(cls, spec: SandboxSpec) -> tuple[str, str]:
        identity = cls._runtime_identity(spec)
        execution_id = str((spec.metadata or {}).get("execution_id") or "").strip()
        from .base import strict_isolation_enabled

        if strict_isolation_enabled() and not execution_id:
            raise ValueError("strict isolation requires execution_id")
        if execution_id:
            identity = (*identity, execution_id)
        digest = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        return f"rt_{digest[:24]}", digest

    async def _container_inspect(self, container_ref: str) -> dict[str, Any] | None:
        rc, stdout, stderr = await self._communicate(
            [self.config.docker_binary, "inspect", container_ref],
            timeout=15,
        )
        if rc != 0:
            detail = (stderr.strip() or stdout.strip() or "docker inspect failed")
            normalized = detail.lower()
            if any(
                marker in normalized
                for marker in ("no such object", "no such container")
            ):
                return None
            raise RuntimeError(f"unable to inspect persistent runtime: {detail}")
        try:
            payload = json.loads(stdout or "[]")
        except Exception as exc:
            raise RuntimeError("invalid Docker inspect response") from exc
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError("invalid Docker inspect response")
        return payload[0]

    @staticmethod
    def _record_attachments(record: PersistentRuntimeRecord) -> list[str]:
        try:
            payload = json.loads(record.network_attachments or "[]")
        except Exception:
            payload = []
        return [str(item) for item in payload if str(item).strip()]

    def _validate_runtime_container(
        self,
        record: PersistentRuntimeRecord,
        inspected: dict[str, Any],
        *,
        allow_legacy_fingerprint: bool = False,
    ) -> dict[str, str]:
        labels = dict((inspected.get("Config") or {}).get("Labels") or {})
        expected_labels = {
            "tcode.runtime": "persistent",
            "tcode.runtime_id": record.runtime_id,
            "tcode.tenant_id": self._label_value(record.tenant_id),
            "tcode.user_id": self._label_value(record.user_id),
            "tcode.workspace_id": self._label_value(record.workspace_id),
            "tcode.sandbox_profile": self._label_value(record.profile_name),
        }
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise RuntimeError("persistent runtime Docker label mismatch")
        actual_fingerprint = str(
            labels.get("tcode.runtime_spec_fingerprint") or ""
        ).strip()
        expected_fingerprint = str(record.spec_fingerprint or "").strip()
        legacy_fingerprint = not actual_fingerprint and not expected_fingerprint
        if legacy_fingerprint:
            if not allow_legacy_fingerprint:
                raise RuntimeError(
                    "persistent runtime security metadata is outdated; delete or reset the runtime"
                )
        elif actual_fingerprint != expected_fingerprint:
            raise RuntimeError("persistent runtime Docker label mismatch")
        attached_networks = set(
            dict((inspected.get("NetworkSettings") or {}).get("Networks") or {})
        )
        expected_networks = {
            record.network_name if record.network_name else record.network_policy
        }
        if attached_networks != expected_networks:
            raise RuntimeError("persistent runtime Docker network mismatch")
        return labels

    def _prepare_host_persistent_control_dirs(self, workspace: Path) -> bool:
        """Create and verify the host-side job control directory.

        A fresh workspace must be initialized by the runner before the
        container can create the same path as its fixed container uid.  False
        means an older container-owned directory exists and needs an in-
        container permission repair.
        """

        probe_name = f".runner-write-probe-{uuid.uuid4().hex[:12]}"
        jobs_fd: int | None = None
        probe_created = False
        try:
            jobs_fd = self._open_jobs_dir(workspace, create=True)
            os.mkdir(probe_name, mode=0o700, dir_fd=jobs_fd)
            probe_created = True
            return True
        except PermissionError:
            return False
        except OSError as exc:
            raise RuntimeError(
                "unable to initialize persistent runtime control directory "
                f"for {workspace}: {exc}"
            ) from exc
        finally:
            if jobs_fd is not None:
                if probe_created:
                    try:
                        os.rmdir(probe_name, dir_fd=jobs_fd)
                    except FileNotFoundError:
                        pass
                os.close(jobs_fd)

    async def _repair_persistent_control_dirs_in_container(
        self,
        spec: SandboxSpec,
        container_ref: str,
    ) -> None:
        container_workspace = spec.pathmap.container_workspace.rstrip("/")
        runtime_dir = f"{container_workspace}/.tcode-runtime"
        jobs_dir = f"{runtime_dir}/jobs"
        rc, stdout, stderr = await self._communicate(
            [
                self.config.docker_binary,
                "exec",
                "--user",
                spec.user or self.config.user,
                "-w",
                container_workspace,
                container_ref,
                "bash",
                "-lc",
                (
                    f"mkdir -p {shlex.quote(jobs_dir)} && "
                    f"chmod 0777 {shlex.quote(runtime_dir)} {shlex.quote(jobs_dir)}"
                ),
            ],
            timeout=30,
        )
        if rc != 0:
            detail = stderr.strip() or stdout.strip() or "permission repair failed"
            raise RuntimeError(
                f"unable to repair persistent runtime control directory: {detail}"
            )
        workspace = self._trusted_workspace_path(spec.pathmap.host_workspace)
        if not self._prepare_host_persistent_control_dirs(workspace):
            raise RuntimeError(
                "persistent runtime control directory remains unwritable after repair: "
                f"{workspace / '.tcode-runtime' / 'jobs'}"
            )

    async def _ensure_persistent_runtime(
        self,
        spec: SandboxSpec,
    ) -> PersistentRuntimeRecord:
        runtime_id, runtime_key = self._runtime_key(spec)
        expected_fingerprint = self._spec_fingerprint(spec)
        lock = self._runtime_locks.setdefault(runtime_id, asyncio.Lock())
        async with lock:
            workspace = self._trusted_workspace_path(spec.pathmap.host_workspace)
            control_dirs_ready = self._prepare_host_persistent_control_dirs(workspace)
            record = self.runtime_registry.get_by_key(runtime_key)
            if record is not None:
                inspected = await self._container_inspect(
                    record.container_id or record.container_name
                )
                if inspected is None:
                    self.runtime_registry.delete(record.runtime_id)
                    record = None
                else:
                    try:
                        labels = self._validate_runtime_container(record, inspected)
                    except RuntimeError as exc:
                        if "metadata is outdated" in str(exc):
                            self.runtime_registry.update(record.runtime_id, status="stale")
                        raise
                    expected_profile = str(
                        spec.metadata.get("sandbox_profile") or "default"
                    ).strip()
                    expected_privilege = self._runtime_privilege(spec)
                    actual_profile = str(labels.get("tcode.sandbox_profile") or "")
                    actual_privilege = str(
                        labels.get("tcode.runtime_privilege") or ""
                    )
                    if (
                        actual_profile != self._label_value(expected_profile)
                        or actual_privilege != expected_privilege
                    ):
                        raise RuntimeError("persistent runtime privilege label mismatch")
                    if (
                        record.host_workspace
                        != spec.pathmap.host_workspace.resolve(strict=False).as_posix()
                    ):
                        raise RuntimeError("persistent runtime workspace mismatch")
                    if record.image != spec.image:
                        self.runtime_registry.update(record.runtime_id, status="stale")
                        raise RuntimeError(
                            "persistent runtime image changed; delete or reset it before retrying"
                        )
                    if record.spec_fingerprint != expected_fingerprint:
                        self.runtime_registry.update(record.runtime_id, status="stale")
                        raise RuntimeError(
                            "persistent runtime profile changed; delete or reset it before retrying"
                        )
                    recorded_mount_identity = str(
                        labels.get("tcode.workspace_mount_identity") or ""
                    ).strip()
                    current_mount_identity = self._workspace_mount_identity(
                        spec.pathmap.host_workspace
                    )
                    if (
                        recorded_mount_identity
                        and recorded_mount_identity != current_mount_identity
                    ):
                        recycled = await self._discard_persistent_runtime_locked(record)
                        if not recycled:
                            self.runtime_registry.update(record.runtime_id, status="stale")
                            raise RuntimeError(
                                "persistent runtime workspace mount changed while jobs are active"
                            )
                        record = None
                    else:
                        state = dict(inspected.get("State") or {})
                        if str(state.get("Status") or "").lower() != "running":
                            rc, stdout, stderr = await self._communicate(
                                [
                                    self.config.docker_binary,
                                    "start",
                                    record.container_id or record.container_name,
                                ],
                                timeout=30,
                            )
                            if rc != 0:
                                raise RuntimeError(
                                    stderr.strip() or stdout.strip() or "runtime start failed"
                                )
                        if not control_dirs_ready:
                            await self._repair_persistent_control_dirs_in_container(
                                spec,
                                record.container_id or record.container_name,
                            )
                        return self.runtime_registry.touch(record.runtime_id) or record

            tenant_id, user_id, workspace_id, profile_name = self._runtime_identity(spec)
            name = f"tcode-runtime-{runtime_key[:20]}"
            prepared_spec = replace(
                spec,
                command=(
                    'mkdir -p "$HOME" "$HOME/.local/bin" '
                    f'{shlex.quote(spec.pathmap.container_workspace)}/.tcode-runtime/jobs '
                    '&& exec tail -f /dev/null'
                ),
                metadata={
                    **dict(spec.metadata or {}),
                    "runtime_id": runtime_id,
                    "runtime_privilege": self._runtime_privilege(spec),
                    "runtime_spec_fingerprint": expected_fingerprint,
                    "workspace_mount_identity": self._workspace_mount_identity(
                        spec.pathmap.host_workspace
                    ),
                },
                lifecycle="persistent",
            )
            prepared_spec, network_name = await self._prepare_network(prepared_spec)
            attachments = list(
                prepared_spec.metadata.get("sandbox_network_attachments") or []
            )
            try:
                args = self._docker_run_args(
                    prepared_spec,
                    name=name,
                    detach=True,
                    remove=False,
                )
                rc, stdout, stderr = await self._communicate(args, timeout=45)
                if rc != 0:
                    raise RuntimeError(
                        stderr.strip() or stdout.strip() or "runtime create failed"
                    )
                container_id = stdout.strip()
                if not control_dirs_ready:
                    await self._repair_persistent_control_dirs_in_container(
                        spec,
                        container_id or name,
                    )
                now = time.time()
                record = PersistentRuntimeRecord(
                    runtime_id=runtime_id,
                    runtime_key=runtime_key,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    profile_name=profile_name,
                    host_workspace=spec.pathmap.host_workspace.resolve(
                        strict=False
                    ).as_posix(),
                    image=spec.image,
                    spec_fingerprint=expected_fingerprint,
                    container_id=container_id,
                    container_name=name,
                    status="running",
                    network_policy=str(spec.network or "none"),
                    network_name=network_name,
                    network_attachments=json.dumps(attachments),
                    idle_stop_seconds=max(0, int(spec.idle_stop_seconds or 0)),
                    retention_seconds=max(0, int(spec.retention_seconds or 0)),
                    created_at=now,
                    last_used_at=now,
                )
                self.runtime_registry.put(record)
                return record
            except Exception:
                await self._cleanup_container(name)
                await self._cleanup_network(network_name, attachments)
                raise

    async def _discard_persistent_runtime_locked(
        self,
        record: PersistentRuntimeRecord,
    ) -> bool:
        """Remove an unusable runtime without deleting workspace-owned data.

        The caller must hold the runtime lock. Returning False means another job
        is active and the container was deliberately left untouched.
        """

        current = self.runtime_registry.get(record.runtime_id)
        if current is None:
            return True
        if (
            current.runtime_key != record.runtime_key
            or current.container_id != record.container_id
            or current.container_name != record.container_name
        ):
            return True
        if self._runtime_has_active_jobs(current):
            return False

        container_ref = current.container_id or current.container_name
        inspected = await self._container_inspect(container_ref)
        if inspected is not None:
            self._validate_runtime_container(
                current,
                inspected,
                allow_legacy_fingerprint=True,
            )
            rc, stdout, stderr = await self._communicate(
                [self.config.docker_binary, "rm", "-f", container_ref],
                timeout=30,
            )
            if rc != 0 and await self._container_inspect(container_ref) is not None:
                raise RuntimeError(
                    stderr.strip() or stdout.strip() or "runtime recycle failed"
                )
        await self._cleanup_network(
            current.network_name,
            self._record_attachments(current),
        )
        self._cleanup_browser_live_for_runtime(current)
        self.runtime_registry.delete(current.runtime_id)
        return True

    async def _recover_stale_workspace_mount(
        self,
        spec: SandboxSpec,
        record: PersistentRuntimeRecord,
        detail: str,
    ) -> bool:
        if not self._is_stale_workspace_mount_error(detail):
            return False
        runtime_id, runtime_key = self._runtime_key(spec)
        lock = self._runtime_locks.setdefault(runtime_id, asyncio.Lock())
        async with lock:
            current = self.runtime_registry.get_by_key(runtime_key)
            if current is None:
                return True
            if (
                current.container_id != record.container_id
                or current.container_name != record.container_name
            ):
                return True
            return await self._discard_persistent_runtime_locked(current)

    @staticmethod
    def _persistent_job_paths(
        spec: SandboxSpec,
        sandbox_job_id: str,
    ) -> tuple[Path, str]:
        relative = Path(".tcode-runtime") / "jobs" / sandbox_job_id
        host = (spec.pathmap.host_workspace / relative).resolve(strict=False)
        container = (
            f"{spec.pathmap.container_workspace.rstrip('/')}/{relative.as_posix()}"
        )
        return host, container

    @staticmethod
    def _validated_job_id(value: str) -> str:
        job_id = str(value or "").strip()
        if not re.fullmatch(r"sbx_[A-Za-z0-9]{1,64}", job_id):
            raise RuntimeError("invalid persistent sandbox job id")
        return job_id

    def _trusted_workspace_path(self, value: str | Path) -> Path:
        workspace = Path(value).expanduser().resolve(strict=True)
        configured_root = self.config.workspace_root
        if configured_root is not None:
            root = configured_root.expanduser().resolve(strict=True)
            try:
                workspace.relative_to(root)
            except ValueError as exc:
                raise RuntimeError("persistent job workspace escaped runner root") from exc
            if workspace == root:
                raise RuntimeError("persistent job workspace cannot be runner root")
        if not workspace.is_dir():
            raise RuntimeError("persistent job workspace is not a directory")
        return workspace

    @staticmethod
    def _open_directory_chain(
        root: Path,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        current_fd = os.open(root, flags)
        try:
            for part in parts:
                if not part or part in {".", ".."} or "/" in part or "\x00" in part:
                    raise RuntimeError("invalid persistent runtime data path")
                created = False
                if create:
                    try:
                        os.mkdir(part, mode=0o777, dir_fd=current_fd)
                        created = True
                    except FileExistsError:
                        pass
                # mkdir's mode is filtered by the runner process umask.  The
                # control tree is shared with a fixed container uid, so make a
                # newly-created component traversable before opening it.  In
                # particular, an inherited 0777 umask would otherwise create
                # mode 000 and make this very next os.open fail with EACCES.
                if created:
                    os.chmod(
                        part,
                        0o777,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                next_fd = os.open(part, flags, dir_fd=current_fd)
                if created:
                    os.fchmod(next_fd, 0o777)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def _open_jobs_dir(self, workspace: Path, *, create: bool) -> int:
        return self._open_directory_chain(
            workspace,
            (".tcode-runtime", "jobs"),
            create=create,
        )

    def _open_job_dir(self, workspace: Path, job_id: str, *, create: bool) -> int:
        normalized = self._validated_job_id(job_id)
        return self._open_directory_chain(
            workspace,
            (".tcode-runtime", "jobs", normalized),
            create=create,
        )

    def _job_location(self, job: SandboxJob) -> tuple[Path, str]:
        metadata = dict(job.metadata or {})
        runtime_id = str(metadata.get("runtime_id") or "").strip()
        record = self.runtime_registry.get(runtime_id) if runtime_id else None
        recorded_workspace = str(
            metadata.get("persistent_host_workspace") or ""
        ).strip()
        if record is not None:
            if recorded_workspace and (
                Path(recorded_workspace).resolve(strict=False)
                != Path(record.host_workspace).resolve(strict=False)
            ):
                raise RuntimeError("persistent job workspace metadata mismatch")
            recorded_workspace = record.host_workspace
        if not recorded_workspace:
            raise RuntimeError("persistent job workspace metadata is missing")
        workspace = self._trusted_workspace_path(recorded_workspace)
        job_id = self._validated_job_id(job.sandbox_job_id)
        expected = workspace / ".tcode-runtime" / "jobs" / job_id
        metadata_path = str(metadata.get("persistent_job_dir") or "").strip()
        if metadata_path and Path(metadata_path) != expected:
            raise RuntimeError("persistent job data path metadata mismatch")
        return workspace, job_id

    @staticmethod
    def _job_file_stat(job_fd: int, name: str) -> os.stat_result | None:
        try:
            info = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"unsafe persistent job control file: {name}")
        return info

    def _read_job_file(
        self,
        workspace: Path,
        job_id: str,
        name: str,
        *,
        max_bytes: int = 65536,
    ) -> str | None:
        job_fd = self._open_job_dir(workspace, job_id, create=False)
        try:
            info = self._job_file_stat(job_fd, name)
            if info is None:
                return None
            if info.st_size > max(1, int(max_bytes)):
                raise RuntimeError(f"persistent job control file is too large: {name}")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_fd = os.open(name, flags, dir_fd=job_fd)
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise RuntimeError(f"unsafe persistent job control file: {name}")
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
            os.close(job_fd)

    def _tail_job_file(
        self,
        workspace: Path,
        job_id: str,
        name: str,
        lines: int,
    ) -> str:
        job_fd = self._open_job_dir(workspace, job_id, create=False)
        try:
            info = self._job_file_stat(job_fd, name)
            if info is None:
                return ""
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_fd = os.open(name, flags, dir_fd=job_fd)
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise RuntimeError(f"unsafe persistent job control file: {name}")
                max_tail_bytes = self.config.job_output_response_max_bytes
                offset = max(0, opened.st_size - max_tail_bytes)
                os.lseek(file_fd, offset, os.SEEK_SET)
                data = os.read(file_fd, min(opened.st_size, max_tail_bytes))
            finally:
                os.close(file_fd)
        finally:
            os.close(job_fd)
        text = data.decode("utf-8", errors="replace")
        if offset:
            text = "[job output tail truncated]\n" + text.split("\n", 1)[-1]
        limit = max(1, min(5000, int(lines or 200)))
        return "\n".join(text.splitlines()[-limit:])

    def _replace_job_file(
        self,
        workspace: Path,
        job_id: str,
        name: str,
        content: str,
    ) -> None:
        job_fd = self._open_job_dir(workspace, job_id, create=True)
        try:
            try:
                os.unlink(name, dir_fd=job_fd)
            except FileNotFoundError:
                pass
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_fd = os.open(name, flags, 0o666, dir_fd=job_fd)
            try:
                # Like the directories above, job control files are exchanged
                # between the host runner and the container uid.  Do not let a
                # service-level umask turn them into unreadable mode-000 files.
                os.fchmod(file_fd, 0o666)
                os.write(file_fd, content.encode("utf-8"))
            finally:
                os.close(file_fd)
        finally:
            os.close(job_fd)

    def _remove_job_dir(self, workspace: Path, job_id: str) -> None:
        normalized = self._validated_job_id(job_id)
        jobs_fd = self._open_jobs_dir(workspace, create=False)
        try:
            try:
                info = os.stat(normalized, dir_fd=jobs_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(normalized, dir_fd=jobs_fd)
            else:
                os.unlink(normalized, dir_fd=jobs_fd)
        finally:
            os.close(jobs_fd)

    @staticmethod
    def _command_prefix() -> str:
        return (
            'if [ -n "${TCODE_SANDBOX_PATH_PREPEND:-}" ]; then '
            'export PATH="${TCODE_SANDBOX_PATH_PREPEND}:$PATH"; fi; '
        )

    def _persistent_exec_args(
        self,
        spec: SandboxSpec,
        record: PersistentRuntimeRecord,
        sandbox_job_id: str,
        *,
        detach: bool,
    ) -> tuple[list[str], Path]:
        host_job_dir, container_job_dir = self._persistent_job_paths(
            spec,
            sandbox_job_id,
        )
        pid_path = f"{container_job_dir}/pid"
        exit_path = f"{container_job_dir}/exit_code"
        output_path = f"{container_job_dir}/output.log"
        terminated_path = f"{container_job_dir}/terminated"
        starting_path = f"{container_job_dir}/starting"
        if detach:
            capture_command = shlex.join(
                [
                    "python3",
                    "-c",
                    _PERSISTENT_OUTPUT_CAPTURE_SCRIPT,
                    output_path,
                    pid_path,
                    exit_path,
                    starting_path,
                    str(self.config.persistent_job_output_max_bytes),
                    self._command_prefix() + spec.command,
                ]
            )
            wrapper = (
                f"mkdir -p {shlex.quote(container_job_dir)} && "
                f"rm -f {shlex.quote(pid_path)} {shlex.quote(exit_path)} "
                f"{shlex.quote(terminated_path)} {shlex.quote(output_path)} && "
                f"exec setsid {capture_command}"
            )
        else:
            inner = "\n".join(
                [
                    f"printf '%s' \"$$\" > {shlex.quote(pid_path)}",
                    f"rm -f {shlex.quote(starting_path)}",
                    self._command_prefix(),
                    "set +e",
                    "(",
                    spec.command,
                    ")",
                    "rc=$?",
                    f"printf '%s' \"$rc\" > {shlex.quote(exit_path)}",
                    "exit \"$rc\"",
                ]
            )
            wrapper = (
                f"mkdir -p {shlex.quote(container_job_dir)} && "
                f"rm -f {shlex.quote(pid_path)} {shlex.quote(exit_path)} "
                f"{shlex.quote(terminated_path)} {shlex.quote(output_path)} && "
                f"exec setsid bash -lc {shlex.quote(inner)}"
            )
        args = [self.config.docker_binary, "exec"]
        if detach:
            args.append("-d")
        args.extend(["-w", spec.container_cwd])
        env = {"HOME": spec.home or self.config.home, **dict(spec.env or {})}
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(
            [
                record.container_id or record.container_name,
                "bash",
                "-lc",
                wrapper,
            ]
        )
        return args, host_job_dir

    async def _terminate_persistent_job(
        self,
        job: SandboxJob,
    ) -> SandboxJobStatus:
        metadata = dict(job.metadata or {})
        workspace, job_id = self._job_location(job)
        container_ref = job.container_id or job.container_name
        pid = str(self._read_job_file(workspace, job_id, "pid") or "").strip()
        if pid.isdigit() and int(pid) > 1:
            command = (
                f"kill -TERM -- -{pid} 2>/dev/null || kill -TERM {pid} 2>/dev/null || true; "
                "sleep 1; "
                f"kill -KILL -- -{pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null || true"
            )
            await self._communicate(
                [
                    self.config.docker_binary,
                    "exec",
                    container_ref,
                    "bash",
                    "-lc",
                    command,
                ],
                timeout=10,
            )
        self._replace_job_file(workspace, job_id, "terminated", "terminated\n")
        return SandboxJobStatus(
            sandbox_job_id=job.sandbox_job_id,
            container_id=job.container_id,
            container_name=job.container_name,
            status="terminated",
            metadata=metadata,
        )

    async def _prepare_network(self, spec: SandboxSpec) -> tuple[SandboxSpec, str]:
        network_policy = str(spec.network or "").strip().lower()
        if network_policy not in {"internal", "egress"}:
            return spec, ""

        proxy_container = str(self.config.egress_proxy_container or "").strip()
        if network_policy == "egress" and not proxy_container:
            raise RuntimeError(
                "TCODE_SANDBOX_EGRESS_PROXY_CONTAINER is required for egress networking"
            )
        service_binding = self.service_policy.binding_for(spec)

        network_name = f"tcode-jobnet-{uuid.uuid4().hex[:18]}"
        rc, stdout, stderr = await self._communicate(
            [
                self.config.docker_binary,
                "network",
                "create",
                "--internal",
                "--label",
                "tcode.sandbox-network=true",
                "--label",
                f"tcode.tenant_id={self._label_value(spec.metadata.get('tenant_id'))}",
                "--label",
                f"tcode.user_id={self._label_value(spec.metadata.get('user_id'))}",
                "--label",
                f"tcode.workspace_id={self._label_value(spec.metadata.get('workspace_id') or spec.pathmap.workspace_id)}",
                network_name,
            ],
            timeout=20,
        )
        if rc != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or "docker network create failed")

        attachments: list[str] = []
        try:
            if network_policy == "egress":
                try:
                    await self._connect_network_container(
                        network_name,
                        proxy_container,
                        "egress-proxy",
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "Sandbox egress proxy container "
                        f"'{proxy_container}' is not network-ready. Recreate the "
                        "proxy container before retrying the browser task. "
                        f"Docker error: {exc}"
                    ) from exc
                attachments.append(proxy_container)
            for container_name, alias in service_binding.containers:
                await self._connect_network_container(
                    network_name,
                    container_name,
                    alias,
                )
                attachments.append(container_name)
        except Exception:
            await self._cleanup_network(network_name, attachments)
            raise

        return replace(
            spec,
            network=network_name,
            env={
                **dict(spec.env or {}),
                **dict(service_binding.env or {}),
            },
            metadata={
                **dict(spec.metadata or {}),
                "sandbox_network_policy": network_policy,
                "sandbox_network_name": network_name,
                "sandbox_network_attachments": attachments,
            },
        ), network_name

    async def _connect_network_container(
        self,
        network_name: str,
        container_name: str,
        alias: str,
    ) -> None:
        rc, stdout, stderr = await self._communicate(
            [
                self.config.docker_binary,
                "network",
                "connect",
                "--alias",
                alias,
                network_name,
                container_name,
            ],
            timeout=20,
        )
        if rc != 0:
            raise RuntimeError(
                stderr.strip() or stdout.strip() or "docker network connect failed"
            )

    async def _prepare_workspace(
        self,
        spec: SandboxSpec,
        *,
        snapshot_id: str = "",
    ) -> tuple[SandboxSpec, dict[str, Any]]:
        if self.snapshot_manager is None:
            return spec, {}
        return await asyncio.to_thread(
            self.snapshot_manager.create,
            spec,
            snapshot_id=snapshot_id,
        )

    async def _finalize_workspace(
        self,
        snapshot: dict[str, Any],
        *,
        commit: bool,
    ) -> dict[str, Any]:
        if not snapshot or self.snapshot_manager is None:
            return {"status": "not_enabled", "changed_paths": [], "conflicts": []}
        return await asyncio.to_thread(
            self.snapshot_manager.finalize,
            snapshot,
            commit=commit,
        )

    async def _discard_workspace(self, snapshot: dict[str, Any]) -> None:
        if not snapshot or self.snapshot_manager is None:
            return
        await asyncio.to_thread(self.snapshot_manager.discard, snapshot)

    async def _cleanup_network(
        self,
        network_name: str,
        attachments: list[str] | None = None,
    ) -> None:
        name = str(network_name or "").strip()
        if not name:
            return
        containers = list(attachments or [])
        if not containers:
            proxy_container = str(self.config.egress_proxy_container or "").strip()
            if proxy_container:
                containers.append(proxy_container)
        for container_name in dict.fromkeys(containers):
            try:
                await self._communicate(
                    [
                        self.config.docker_binary,
                        "network",
                        "disconnect",
                        "-f",
                        name,
                        container_name,
                    ],
                    timeout=15,
                )
            except Exception:
                pass
        try:
            await self._communicate(
                [self.config.docker_binary, "network", "rm", name],
                timeout=15,
            )
        except Exception:
            pass

    async def _communicate(self, args: list[str], timeout: int | None = None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            int(proc.returncode or 0),
            stdout_b.decode("utf-8", errors="replace") if stdout_b else "",
            stderr_b.decode("utf-8", errors="replace") if stderr_b else "",
        )

    async def _communicate_bounded(
        self,
        args: list[str],
        *,
        timeout: int | None,
        max_bytes: int,
    ) -> tuple[int, str, str, dict[str, Any]]:
        """Run a CLI command without allowing stdout/stderr to grow in memory."""

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buffer = BoundedBytesBuffer(max_bytes)
        stderr_buffer = BoundedBytesBuffer(max_bytes)

        async def drain(
            stream: asyncio.StreamReader | None,
            target: BoundedBytesBuffer,
        ) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return
                target.append(chunk)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    drain(proc.stdout, stdout_buffer),
                    drain(proc.stderr, stderr_buffer),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        stdout = stdout_buffer.render(label="stdout truncated")
        stderr = stderr_buffer.render(label="stderr truncated")
        metadata = {
            "stdout_original_bytes": stdout.original_bytes,
            "stderr_original_bytes": stderr.original_bytes,
            "output_truncated": bool(stdout.truncated or stderr.truncated),
            "output_limit_bytes": max_bytes,
        }
        return (
            int(proc.returncode or 0),
            stdout.data.decode("utf-8", errors="replace"),
            stderr.data.decode("utf-8", errors="replace"),
            metadata,
        )

    async def _run_persistent_once(
        self,
        spec: SandboxSpec,
        *,
        _allow_mount_recovery: bool = True,
    ) -> SandboxResult:
        record = await self._ensure_persistent_runtime(spec)
        sandbox_job_id = f"sbx_{uuid.uuid4().hex[:16]}"
        args, job_dir = self._persistent_exec_args(
            spec,
            record,
            sandbox_job_id,
            detach=False,
        )
        workspace = self._trusted_workspace_path(spec.pathmap.host_workspace)
        self._replace_job_file(workspace, sandbox_job_id, "starting", "starting\n")
        job_dir_present = True
        try:
            rc, stdout, stderr, output_metadata = await self._communicate_bounded(
                args,
                timeout=spec.timeout_seconds,
                max_bytes=self.config.job_output_response_max_bytes,
            )
            detail = stderr.strip() or stdout.strip()
            if (
                rc != 0
                and _allow_mount_recovery
                and self._is_stale_workspace_mount_error(detail)
            ):
                self._remove_job_dir(workspace, sandbox_job_id)
                job_dir_present = False
                if await self._recover_stale_workspace_mount(spec, record, detail):
                    recovered = await self._run_persistent_once(
                        spec,
                        _allow_mount_recovery=False,
                    )
                    recovered.metadata["persistent_runtime_recovered"] = True
                    recovered.metadata["persistent_runtime_recovery_reason"] = (
                        "stale_workspace_mount"
                    )
                    return recovered
            self.runtime_registry.touch(record.runtime_id)
            return SandboxResult(
                status="completed" if rc == 0 else "failed",
                exit_code=rc,
                stdout=stdout,
                stderr=stderr,
                sandbox_job_id=sandbox_job_id,
                container_id=record.container_id,
                container_name=record.container_name,
                metadata={
                    "backend": self.config.backend,
                    "runtime": self.config.docker_runtime,
                    "runtime_id": record.runtime_id,
                    "runtime_lifecycle": "persistent",
                    "runtime_privilege": self._runtime_privilege(spec),
                    "sandbox_profile": record.profile_name,
                    "network_policy": record.network_policy,
                    "workspace_commit": {
                        "status": "direct",
                        "changed_paths": [],
                        "conflicts": [],
                    },
                    **output_metadata,
                },
            )
        except asyncio.TimeoutError:
            await self._terminate_persistent_job(
                SandboxJob(
                    sandbox_job_id=sandbox_job_id,
                    container_id=record.container_id,
                    container_name=record.container_name,
                    command=spec.command,
                    metadata={
                        "persistent_runtime": True,
                        "persistent_job_dir": job_dir.as_posix(),
                        "persistent_host_workspace": record.host_workspace,
                        "runtime_id": record.runtime_id,
                    },
                )
            )
            return SandboxResult(
                status="timed_out",
                exit_code=None,
                stdout="",
                stderr=f"sandbox command timed out after {spec.timeout_seconds}s",
                sandbox_job_id=sandbox_job_id,
                container_id=record.container_id,
                container_name=record.container_name,
                metadata={
                    "runtime_id": record.runtime_id,
                    "runtime_lifecycle": "persistent",
                    "runtime_privilege": self._runtime_privilege(spec),
                    "workspace_commit": {"status": "direct"},
                },
            )
        finally:
            if job_dir_present:
                self._remove_job_dir(workspace, sandbox_job_id)

    async def _start_persistent_job(
        self,
        spec: SandboxSpec,
        *,
        _allow_mount_recovery: bool = True,
    ) -> SandboxJob:
        record = await self._ensure_persistent_runtime(spec)
        await self._ensure_persistent_capture_support(record)
        sandbox_job_id = f"sbx_{uuid.uuid4().hex[:16]}"
        args, job_dir = self._persistent_exec_args(
            spec,
            record,
            sandbox_job_id,
            detach=True,
        )
        workspace = self._trusted_workspace_path(spec.pathmap.host_workspace)
        self._replace_job_file(workspace, sandbox_job_id, "starting", "starting\n")
        rc, stdout, stderr = await self._communicate(args, timeout=30)
        if rc != 0:
            self._remove_job_dir(workspace, sandbox_job_id)
            detail = stderr.strip() or stdout.strip() or "persistent exec failed"
            if (
                _allow_mount_recovery
                and self._is_stale_workspace_mount_error(detail)
                and await self._recover_stale_workspace_mount(spec, record, detail)
            ):
                recovered = await self._start_persistent_job(
                    spec,
                    _allow_mount_recovery=False,
                )
                recovered.metadata["persistent_runtime_recovered"] = True
                recovered.metadata["persistent_runtime_recovery_reason"] = (
                    "stale_workspace_mount"
                )
                return recovered
            raise RuntimeError(detail)
        self.runtime_registry.touch(record.runtime_id)
        return SandboxJob(
            sandbox_job_id=sandbox_job_id,
            process_id=sandbox_job_id,
            container_id=record.container_id,
            container_name=record.container_name,
            command=spec.command,
            status="running",
            metadata={
                "backend": self.config.backend,
                "runtime": self.config.docker_runtime,
                "tenant_id": str(spec.metadata.get("tenant_id") or ""),
                "user_id": str(spec.metadata.get("user_id") or ""),
                "workspace_id": str(
                    spec.metadata.get("workspace_id") or spec.pathmap.workspace_id
                ),
                "execution_id": str(spec.metadata.get("execution_id") or ""),
                "runtime_id": record.runtime_id,
                "runtime_lifecycle": "persistent",
                "runtime_privilege": self._runtime_privilege(spec),
                "persistent_runtime": True,
                "persistent_job_dir": job_dir.as_posix(),
                "persistent_host_workspace": record.host_workspace,
                "container_cwd": spec.container_cwd,
                "sandbox_profile": record.profile_name,
                "network_policy": record.network_policy,
                "started_at": time.time(),
                "timeout_seconds": spec.timeout_seconds,
            },
        )

    async def _ensure_persistent_capture_support(
        self,
        record: PersistentRuntimeRecord,
    ) -> None:
        """Fail fast when an executor image cannot run the bounded capturer."""

        container_ref = record.container_id or record.container_name
        rc, stdout, stderr, _ = await self._communicate_bounded(
            [
                self.config.docker_binary,
                "exec",
                container_ref,
                "python3",
                "-c",
                (
                    "import os,shutil,subprocess,sys,time;"
                    "sys.exit(0 if shutil.which('setsid') else 127)"
                ),
            ],
            timeout=15,
            max_bytes=64 * 1024,
        )
        if rc != 0:
            detail = (
                stderr.strip()
                or stdout.strip()
                or "python3 or setsid is unavailable"
            )
            if rc == 127:
                raise RuntimeError(
                    "Persistent Runtime requires python3 and setsid in the executor "
                    f"image for bounded output capture: {detail[:1000]}"
                )
            raise RuntimeError(
                "Unable to verify Persistent Runtime bounded output capture support: "
                f"{detail[:1000]}"
            )

    async def run_once(self, spec: SandboxSpec) -> SandboxResult:
        if self._persistent_requested(spec) and not spec.mounts:
            return await self._run_persistent_once(spec)
        name = self._container_name("tcode-run")
        prepared_spec = spec
        network_name = ""
        network_attachments: list[str] = []
        snapshot: dict[str, Any] = {}
        try:
            prepared_spec, snapshot = await self._prepare_workspace(spec)
            prepared_spec, network_name = await self._prepare_network(prepared_spec)
            network_attachments = list(
                prepared_spec.metadata.get("sandbox_network_attachments") or []
            )
            args = self._docker_run_args(prepared_spec, name=name, detach=False, remove=True)
            rc, stdout, stderr, output_metadata = await self._communicate_bounded(
                args,
                timeout=prepared_spec.timeout_seconds,
                max_bytes=self.config.job_output_response_max_bytes,
            )
            workspace_commit = await self._finalize_workspace(
                snapshot,
                commit=rc == 0,
            )
            snapshot = {}
            result_status = "completed" if rc == 0 else "failed"
            if workspace_commit.get("status") == "conflict":
                result_status = "conflict"
                conflict_text = ", ".join(
                    list(workspace_commit.get("conflicts") or [])[:20]
                )
                stderr = (
                    stderr
                    + "\nWorkspace writeback conflict; no files were committed: "
                    + conflict_text
                ).strip()
            return SandboxResult(
                status=result_status,
                exit_code=rc,
                stdout=stdout,
                stderr=stderr,
                container_name=name,
                metadata={
                    "backend": self.config.backend,
                    "runtime": self.config.docker_runtime,
                    "sandbox_profile": str(prepared_spec.metadata.get("sandbox_profile", "") or ""),
                    "external_mount_count": len(prepared_spec.mounts),
                    "network_policy": str(spec.network or "none"),
                    "workspace_commit": workspace_commit,
                    **output_metadata,
                    **(
                        {"persistent_fallback_reason": "external_mounts"}
                        if self._persistent_requested(spec) and spec.mounts
                        else {}
                    ),
                },
            )
        except asyncio.TimeoutError:
            await self._cleanup_container(name)
            await self._discard_workspace(snapshot)
            snapshot = {}
            return SandboxResult(
                status="timed_out",
                exit_code=None,
                stdout="",
                stderr=f"sandbox command timed out after {prepared_spec.timeout_seconds}s",
                container_name=name,
                metadata={
                    "backend": self.config.backend,
                    "runtime": self.config.docker_runtime,
                    "sandbox_profile": str(prepared_spec.metadata.get("sandbox_profile", "") or ""),
                    "external_mount_count": len(prepared_spec.mounts),
                    "network_policy": str(spec.network or "none"),
                    "workspace_commit": {
                        "status": "discarded",
                        "changed_paths": [],
                        "conflicts": [],
                    },
                },
            )
        except Exception:
            await self._cleanup_container(name)
            await self._discard_workspace(snapshot)
            snapshot = {}
            raise
        finally:
            await self._discard_workspace(snapshot)
            await self._cleanup_network(network_name, network_attachments)

    async def start_job(self, spec: SandboxSpec) -> SandboxJob:
        if self._persistent_requested(spec) and not spec.mounts:
            return await self._start_persistent_job(spec)
        name = self._container_name("tcode-job")
        sandbox_job_id = f"sbx_{uuid.uuid4().hex[:16]}"
        network_name = ""
        network_attachments: list[str] = []
        snapshot: dict[str, Any] = {}
        try:
            prepared_spec, snapshot = await self._prepare_workspace(
                replace(
                    spec,
                    metadata={
                        **dict(spec.metadata or {}),
                        "sandbox_job_id": sandbox_job_id,
                    },
                ),
                snapshot_id=sandbox_job_id,
            )
            prepared_spec, network_name = await self._prepare_network(prepared_spec)
            network_attachments = list(
                prepared_spec.metadata.get("sandbox_network_attachments") or []
            )
            args = self._docker_run_args(prepared_spec, name=name, detach=True, remove=False)
            rc, stdout, stderr = await self._communicate(args, timeout=30)
            if rc != 0:
                raise RuntimeError(stderr.strip() or stdout.strip() or "docker run failed")
        except Exception:
            await self._cleanup_container(name)
            await self._cleanup_network(network_name, network_attachments)
            await self._discard_workspace(snapshot)
            raise
        container_id = stdout.strip()
        log_file = self.job_log_root / f"{sandbox_job_id}.log"
        return SandboxJob(
            sandbox_job_id=sandbox_job_id,
            process_id=sandbox_job_id,
            container_id=container_id,
            container_name=name,
            command=spec.command,
            status="running",
            metadata={
                "backend": self.config.backend,
                "runtime": self.config.docker_runtime,
                "tenant_id": str(prepared_spec.metadata.get("tenant_id") or ""),
                "user_id": str(prepared_spec.metadata.get("user_id") or ""),
                "workspace_id": str(
                    prepared_spec.metadata.get("workspace_id")
                    or prepared_spec.pathmap.workspace_id
                ),
                "execution_id": str(prepared_spec.metadata.get("execution_id") or ""),
                "container_cwd": prepared_spec.container_cwd,
                "sandbox_profile": str(prepared_spec.metadata.get("sandbox_profile", "") or ""),
                "external_mount_count": len(prepared_spec.mounts),
                "network_policy": str(spec.network or "none"),
                "sandbox_network_name": network_name,
                "sandbox_network_attachments": list(
                    prepared_spec.metadata.get("sandbox_network_attachments") or []
                ),
                "workspace_snapshot": snapshot,
                "workspace_finalized": False,
                "job_log_file": log_file.as_posix(),
                "started_at": time.time(),
                "timeout_seconds": prepared_spec.timeout_seconds,
            },
        )

    async def _persistent_job_status(self, job: SandboxJob) -> SandboxJobStatus:
        metadata = dict(job.metadata or {})
        workspace, job_id = self._job_location(job)
        if self._read_job_file(workspace, job_id, "terminated") is not None:
            status = "terminated"
            exit_code = None
        elif (exit_text := self._read_job_file(workspace, job_id, "exit_code")) is not None:
            try:
                exit_code = int(exit_text.strip())
            except Exception:
                exit_code = None
            status = "completed" if exit_code == 0 else "failed"
        else:
            inspected = await self._container_inspect(
                job.container_id or job.container_name
            )
            state = dict((inspected or {}).get("State") or {})
            docker_status = str(state.get("Status") or "missing").lower()
            status = "running" if docker_status == "running" else "missing"
            exit_code = None

        if status == "running" and self._job_expired(job):
            await self._terminate_persistent_job(job)
            status = "timed_out"
            metadata["timed_out"] = True
        runtime_id = str(metadata.get("runtime_id") or "")
        if runtime_id and status == "running":
            self.runtime_registry.touch(runtime_id)
        return SandboxJobStatus(
            sandbox_job_id=job.sandbox_job_id,
            container_id=job.container_id,
            container_name=job.container_name,
            status=status,
            exit_code=exit_code,
            metadata=metadata,
        )

    async def job_status(self, job: SandboxJob | str) -> SandboxJobStatus:
        if isinstance(job, SandboxJob) and bool(
            (job.metadata or {}).get("persistent_runtime")
        ):
            return await self._persistent_job_status(job)
        if isinstance(job, SandboxJob) and bool(
            (job.metadata or {}).get("workspace_finalized")
        ):
            return SandboxJobStatus(
                sandbox_job_id=job.sandbox_job_id,
                container_id=job.container_id,
                container_name=job.container_name,
                status=job.status,
                exit_code=(
                    int(job.metadata["exit_code"])
                    if isinstance((job.metadata or {}).get("exit_code"), int)
                    else None
                ),
                metadata=dict(job.metadata or {}),
            )
        container_ref = self._container_ref(job)
        args = [self.config.docker_binary, "inspect", container_ref]
        rc, stdout, stderr = await self._communicate(args, timeout=15)
        if rc != 0:
            final_metadata: dict[str, Any] = {}
            if isinstance(job, SandboxJob) and not bool(
                (job.metadata or {}).get("workspace_finalized")
            ):
                final_metadata.update(
                    await self._finalize_job(
                        job,
                        exit_code=None,
                        commit=False,
                    )
                )
            return SandboxJobStatus(
                sandbox_job_id=self._job_id(job),
                container_id=container_ref,
                container_name=container_ref,
                status="missing",
                error=stderr.strip() or stdout.strip(),
                metadata=final_metadata,
            )
        data = json.loads(stdout or "[]")
        item: dict[str, Any] = data[0] if isinstance(data, list) and data else {}
        state = item.get("State") if isinstance(item.get("State"), dict) else {}
        docker_status = str(state.get("Status") or "").lower()
        exit_code = state.get("ExitCode")
        mapped = self._map_status(docker_status, exit_code)
        final_metadata: dict[str, Any] = {"docker_status": docker_status}
        if mapped == "running" and isinstance(job, SandboxJob) and self._job_expired(job):
            terminated = await self.terminate_job(job)
            return SandboxJobStatus(
                sandbox_job_id=terminated.sandbox_job_id,
                container_id=terminated.container_id,
                container_name=terminated.container_name,
                status="timed_out",
                exit_code=terminated.exit_code,
                error=terminated.error,
                metadata={
                    **dict(terminated.metadata or {}),
                    "timed_out": True,
                },
            )
        if mapped in {"completed", "failed"} and isinstance(job, SandboxJob):
            final_metadata.update(
                await self._finalize_job(
                    job,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    commit=mapped == "completed",
                )
            )
            if (
                (final_metadata.get("workspace_commit") or {}).get("status")
                == "conflict"
            ):
                mapped = "conflict"
        return SandboxJobStatus(
            sandbox_job_id=self._job_id(job),
            container_id=str(item.get("Id") or container_ref),
            container_name=str(item.get("Name") or "").lstrip("/") or container_ref,
            status=mapped,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            metadata=final_metadata,
        )

    async def job_output(self, job: SandboxJob | str, lines: int = 200) -> str:
        if isinstance(job, SandboxJob) and bool(
            (job.metadata or {}).get("persistent_runtime")
        ):
            workspace, job_id = self._job_location(job)
            return self._tail_job_file(workspace, job_id, "output.log", lines)
        if isinstance(job, SandboxJob):
            log_file = self._job_log_file(job)
            if log_file.is_file() and bool(
                (job.metadata or {}).get("workspace_finalized")
            ):
                return self._tail_file(log_file, lines)
        container_ref = self._container_ref(job)
        limit = max(1, min(5000, int(lines or 200)))
        args = [self.config.docker_binary, "logs", "--tail", str(limit), container_ref]
        rc, stdout, stderr, _ = await self._communicate_bounded(
            args,
            timeout=30,
            max_bytes=self.config.job_output_response_max_bytes,
        )
        if rc != 0:
            combined_text = stderr.strip() or stdout.strip()
        else:
            combined_text = stdout + stderr
        combined = BoundedBytesBuffer(self.config.job_output_response_max_bytes)
        combined.append(combined_text.encode("utf-8", errors="replace"))
        return combined.render(label="sandbox output truncated").data.decode(
            "utf-8",
            errors="replace",
        )

    async def terminate_job(self, job: SandboxJob | str) -> SandboxJobStatus:
        if isinstance(job, SandboxJob) and bool(
            (job.metadata or {}).get("persistent_runtime")
        ):
            return await self._terminate_persistent_job(job)
        container_ref = self._container_ref(job)
        final_metadata: dict[str, Any] = {}
        stop_rc, stop_stdout, stop_stderr = await self._communicate(
            [self.config.docker_binary, "stop", "--time", "2", container_ref],
            timeout=15,
        )
        if isinstance(job, SandboxJob):
            final_metadata.update(
                await self._finalize_job(
                    job,
                    exit_code=None,
                    commit=False,
                    remove_container=False,
                )
            )
        args = [self.config.docker_binary, "rm", "-f", container_ref]
        rc, stdout, stderr = await self._communicate(args, timeout=30)
        if isinstance(job, SandboxJob):
            await self._cleanup_network(
                str((job.metadata or {}).get("sandbox_network_name") or ""),
                list((job.metadata or {}).get("sandbox_network_attachments") or []),
            )
        status = "terminated" if rc == 0 or stop_rc == 0 else "missing"
        return SandboxJobStatus(
            sandbox_job_id=self._job_id(job),
            container_id=container_ref,
            container_name=container_ref,
            status=status,
            error=(
                ""
                if status == "terminated"
                else (
                    stderr.strip()
                    or stdout.strip()
                    or stop_stderr.strip()
                    or stop_stdout.strip()
                )
            ),
            metadata=final_metadata,
        )

    @staticmethod
    def _runtime_owner_matches(
        record: PersistentRuntimeRecord,
        owner: dict[str, str],
    ) -> bool:
        return all(
            not str(owner.get(key) or "").strip()
            or str(getattr(record, key, "") or "").strip()
            == str(owner.get(key) or "").strip()
            for key in ("tenant_id", "user_id", "workspace_id")
        )

    def _runtime_job_ids(self, record: PersistentRuntimeRecord) -> list[str]:
        workspace = self._trusted_workspace_path(record.host_workspace)
        try:
            jobs_fd = self._open_jobs_dir(workspace, create=False)
        except FileNotFoundError:
            return []
        try:
            job_ids: list[str] = []
            with os.scandir(jobs_fd) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        job_ids.append(self._validated_job_id(entry.name))
                    except RuntimeError:
                        continue
            return job_ids
        finally:
            os.close(jobs_fd)

    def _runtime_has_active_jobs(self, record: PersistentRuntimeRecord) -> bool:
        workspace = self._trusted_workspace_path(record.host_workspace)
        for job_id in self._runtime_job_ids(record):
            job_fd = self._open_job_dir(workspace, job_id, create=False)
            try:
                starting = self._job_file_stat(job_fd, "starting") is not None
                pid = self._job_file_stat(job_fd, "pid") is not None
                exited = self._job_file_stat(job_fd, "exit_code") is not None
                terminated = self._job_file_stat(job_fd, "terminated") is not None
                if starting or (pid and not exited and not terminated):
                    return True
            finally:
                os.close(job_fd)
        return False

    def _mark_runtime_jobs_terminated(self, record: PersistentRuntimeRecord) -> None:
        workspace = self._trusted_workspace_path(record.host_workspace)
        for job_id in self._runtime_job_ids(record):
            job_fd = self._open_job_dir(workspace, job_id, create=False)
            try:
                if self._job_file_stat(job_fd, "exit_code") is not None:
                    continue
                try:
                    os.unlink("starting", dir_fd=job_fd)
                except FileNotFoundError:
                    pass
            finally:
                os.close(job_fd)
            self._replace_job_file(
                workspace,
                job_id,
                "terminated",
                "runtime removed\n",
            )

    def _purge_runtime_data(self, record: PersistentRuntimeRecord) -> None:
        workspace = self._trusted_workspace_path(record.host_workspace)
        workspace_fd = self._open_directory_chain(workspace, (), create=False)
        try:
            try:
                info = os.stat(
                    ".tcode-runtime",
                    dir_fd=workspace_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(".tcode-runtime", dir_fd=workspace_fd)
            else:
                os.unlink(".tcode-runtime", dir_fd=workspace_fd)
        finally:
            os.close(workspace_fd)

    async def list_persistent_runtimes(
        self,
        *,
        tenant_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        records = self.runtime_registry.list(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        payload: list[dict[str, Any]] = []
        for record in records:
            inspect_error = ""
            try:
                inspected = await self._container_inspect(
                    record.container_id or record.container_name
                )
            except RuntimeError as exc:
                inspected = None
                inspect_error = str(exc)
            state = dict((inspected or {}).get("State") or {})
            labels: dict[str, str] = {}
            validation_error = inspect_error
            if inspected is not None:
                try:
                    labels = self._validate_runtime_container(record, inspected)
                except RuntimeError as exc:
                    validation_error = str(exc)
                    self.runtime_registry.update(record.runtime_id, status="invalid")
            docker_status = str(
                state.get("Status") or ("unknown" if inspect_error else "missing")
            ).lower()
            if (
                not validation_error
                and docker_status != record.status
                and record.status not in {"stale", "invalid"}
            ):
                self.runtime_registry.update(record.runtime_id, status=docker_status)
            active_jobs: bool | None
            try:
                active_jobs = self._runtime_has_active_jobs(record)
            except (OSError, RuntimeError) as exc:
                active_jobs = None
                job_error = f"persistent job metadata validation failed: {exc}"
                validation_error = (
                    f"{validation_error}; {job_error}"
                    if validation_error
                    else job_error
                )
            payload.append(
                {
                    **record.to_dict(),
                    "docker_status": docker_status,
                    "runtime_privilege": str(
                        labels.get("tcode.runtime_privilege") or "standard"
                    ),
                    "active_jobs": active_jobs,
                    **({"validation_error": validation_error} if validation_error else {}),
                }
            )
        return payload

    async def manage_persistent_runtime(
        self,
        runtime_id: str,
        action: str,
        *,
        owner: dict[str, str] | None = None,
        owner_scope: str = "workspace",
        force: bool = False,
        purge_data: bool = False,
    ) -> dict[str, Any]:
        record = self.runtime_registry.get(runtime_id)
        if record is None:
            raise KeyError("persistent runtime not found")
        fields = {"workspace": ("tenant_id", "user_id", "workspace_id"), "user": ("tenant_id", "user_id"), "tenant": ("tenant_id",)}.get(owner_scope)
        if fields is None or any(not str((owner or {}).get(key) or "").strip() for key in fields):
            raise PermissionError("complete runtime owner identity is required")
        if not self._runtime_owner_matches(record, owner or {}):
            raise PermissionError("persistent runtime owner mismatch")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"start", "stop", "delete", "reset"}:
            raise ValueError("unsupported persistent runtime action")
        lock = self._runtime_locks.setdefault(record.runtime_id, asyncio.Lock())
        async with lock:
            container_ref = record.container_id or record.container_name
            inspected = await self._container_inspect(container_ref)
            labels: dict[str, str] = {}
            if inspected is not None:
                labels = self._validate_runtime_container(
                    record,
                    inspected,
                    allow_legacy_fingerprint=normalized_action
                    in {"stop", "delete", "reset"},
                )
            if normalized_action == "start":
                if record.status == "stale":
                    raise RuntimeError("stale persistent runtime must be deleted or reset")
                if inspected is None:
                    raise RuntimeError("persistent runtime container is missing")
                if (
                    (
                        record.profile_name == root_runtime_profile_name()
                        or labels.get("tcode.runtime_privilege") == "root"
                    )
                    and not root_runtime_user_allowed(record.user_id)
                ):
                    raise PermissionError(
                        "root runtime authorization was revoked; delete or reset it"
                    )
                rc, stdout, stderr = await self._communicate(
                    [self.config.docker_binary, "start", container_ref],
                    timeout=30,
                )
                if rc != 0:
                    raise RuntimeError(stderr.strip() or stdout.strip())
                updated = self.runtime_registry.touch(record.runtime_id) or record
                return {**updated.to_dict(), "action": "started"}

            if not force and self._runtime_has_active_jobs(record):
                raise RuntimeError("persistent runtime has active jobs")

            if normalized_action == "stop":
                if force:
                    try:
                        self._mark_runtime_jobs_terminated(record)
                    except (OSError, RuntimeError):
                        pass
                rc, stdout, stderr = await self._communicate(
                    [self.config.docker_binary, "stop", "--time", "10", container_ref],
                    timeout=20,
                )
                if rc != 0 and await self._container_inspect(container_ref) is not None:
                    raise RuntimeError(stderr.strip() or stdout.strip())
                updated = self.runtime_registry.update(
                    record.runtime_id,
                    status="stopped",
                    stopped_at=time.time(),
                ) or record
                cleaned_sessions = self._cleanup_browser_live_for_runtime(record)
                return {
                    **updated.to_dict(),
                    "action": "stopped",
                    "browser_live_sessions_cleaned": cleaned_sessions,
                }

            if force:
                try:
                    self._mark_runtime_jobs_terminated(record)
                except (OSError, RuntimeError):
                    pass
            rc, stdout, stderr = await self._communicate(
                [self.config.docker_binary, "rm", "-f", container_ref],
                timeout=30,
            )
            if rc != 0 and await self._container_inspect(container_ref) is not None:
                raise RuntimeError(
                    stderr.strip() or stdout.strip() or "runtime delete failed"
                )
            await self._cleanup_network(
                record.network_name,
                self._record_attachments(record),
            )
            should_purge = purge_data or normalized_action == "reset"
            cleaned_sessions = self._cleanup_browser_live_for_runtime(record)
            if should_purge:
                self._purge_runtime_data(record)
            self.runtime_registry.delete(record.runtime_id)
            return {
                **record.to_dict(),
                "status": "deleted",
                "action": normalized_action,
                "runtime_data_purged": should_purge,
                "browser_live_sessions_cleaned": cleaned_sessions,
            }

    async def cleanup_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        purge_data: bool = False,
    ) -> dict[str, Any]:
        owner = {
            "tenant_id": self._label_value(tenant_id),
            "user_id": self._label_value(user_id),
            "workspace_id": self._label_value(workspace_id),
        }
        if any(not value for value in owner.values()):
            raise ValueError("tenant_id, user_id and workspace_id are required")

        deleted_runtime_ids: list[str] = []
        removed_container_ids: list[str] = []
        removed_networks: list[str] = []
        failures: list[str] = []
        runtime_failures: dict[str, str] = {}
        purged_job_logs: list[str] = []
        records = self.runtime_registry.list(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        for record in records:
            try:
                job_ids = self._runtime_job_ids(record) if purge_data else []
                await self.manage_persistent_runtime(
                    record.runtime_id,
                    "delete",
                    owner={
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "workspace_id": workspace_id,
                    },
                    force=True,
                    purge_data=purge_data,
                )
                for job_id in job_ids:
                    log_file = self.job_log_root / f"{job_id[:128]}.log"
                    try:
                        log_file.unlink()
                        purged_job_logs.append(log_file.as_posix())
                    except FileNotFoundError:
                        pass
                deleted_runtime_ids.append(record.runtime_id)
            except Exception as exc:
                runtime_failures[record.runtime_id] = str(exc)

        filters = [
            "--filter",
            "label=tcode.sandbox=true",
        ]
        for key, value in owner.items():
            filters.extend(["--filter", f"label=tcode.{key}={value}"])
        rc, stdout, stderr = await self._communicate(
            [self.config.docker_binary, "ps", "-aq", *filters],
            timeout=30,
        )
        if rc != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or "docker ps failed")

        workspace_networks: set[str] = set()
        container_networks: set[str] = set()
        for container_id in [line.strip() for line in stdout.splitlines() if line.strip()]:
            inspected = await self._container_inspect(container_id)
            if inspected is None:
                continue
            labels = dict((inspected.get("Config") or {}).get("Labels") or {})
            if labels.get("tcode.sandbox") != "true" or any(
                labels.get(f"tcode.{key}") != value for key, value in owner.items()
            ):
                failures.append(f"container {container_id}: Docker label mismatch")
                continue
            discovered_networks = {
                name
                for name in dict(
                    (inspected.get("NetworkSettings") or {}).get("Networks") or {}
                )
                if str(name).startswith("tcode-jobnet-")
            }
            workspace_networks.update(discovered_networks)
            container_networks.update(discovered_networks)
            rm_rc, rm_stdout, rm_stderr = await self._communicate(
                [self.config.docker_binary, "rm", "-f", container_id],
                timeout=30,
            )
            if rm_rc != 0 and await self._container_inspect(container_id) is not None:
                failures.append(
                    f"container {container_id}: "
                    f"{rm_stderr.strip() or rm_stdout.strip() or 'delete failed'}"
                )
                continue
            removed_container_ids.append(container_id)

        network_filters = ["--filter", "label=tcode.sandbox-network=true"]
        for key, value in owner.items():
            network_filters.extend(["--filter", f"label=tcode.{key}={value}"])
        network_rc, network_stdout, network_stderr = await self._communicate(
            [
                self.config.docker_binary,
                "network",
                "ls",
                "-q",
                *network_filters,
            ],
            timeout=30,
        )
        if network_rc != 0:
            raise RuntimeError(
                network_stderr.strip()
                or network_stdout.strip()
                or "docker network ls failed"
            )
        workspace_networks.update(
            line.strip()
            for line in network_stdout.splitlines()
            if line.strip()
        )

        for network_name in sorted(workspace_networks):
            try:
                inspect_rc, inspect_stdout, inspect_stderr = await self._communicate(
                    [self.config.docker_binary, "network", "inspect", network_name],
                    timeout=15,
                )
                if inspect_rc != 0:
                    if "no such network" in (inspect_stderr or inspect_stdout).lower():
                        continue
                    raise RuntimeError(inspect_stderr.strip() or inspect_stdout.strip())
                payload = json.loads(inspect_stdout or "[]")
                network = payload[0] if isinstance(payload, list) and payload else {}
                labels = dict(network.get("Labels") or {}) if isinstance(network, dict) else {}
                if labels.get("tcode.sandbox-network") != "true":
                    raise RuntimeError("Docker network label mismatch")
                if network_name not in container_networks and any(
                    labels.get(f"tcode.{key}") != value
                    for key, value in owner.items()
                ):
                    raise RuntimeError("Docker network owner label mismatch")
                attachments = list(dict(network.get("Containers") or {}).keys())
                await self._cleanup_network(network_name, attachments)
                verify_rc, _, _ = await self._communicate(
                    [self.config.docker_binary, "network", "inspect", network_name],
                    timeout=15,
                )
                if verify_rc == 0:
                    raise RuntimeError("network still exists after cleanup")
                removed_networks.append(network_name)
            except Exception as exc:
                failures.append(f"network {network_name}: {exc}")

        for record in records:
            if self.runtime_registry.get(record.runtime_id) is None:
                continue
            try:
                if await self._container_inspect(record.container_id or record.container_name) is None:
                    await self._cleanup_network(
                        record.network_name,
                        self._record_attachments(record),
                    )
                    self.runtime_registry.delete(record.runtime_id)
                    deleted_runtime_ids.append(record.runtime_id)
                    runtime_failures.pop(record.runtime_id, None)
            except Exception as exc:
                failures.append(f"runtime registry {record.runtime_id}: {exc}")

        failures.extend(
            f"runtime {runtime_id}: {error}"
            for runtime_id, error in sorted(runtime_failures.items())
        )
        if failures:
            raise RuntimeError("; ".join(dict.fromkeys(failures)))
        return {
            "status": "cleaned",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "deleted_runtime_ids": sorted(set(deleted_runtime_ids)),
            "removed_container_ids": sorted(set(removed_container_ids)),
            "removed_networks": sorted(set(removed_networks)),
            "purged_job_logs": sorted(set(purged_job_logs)),
            "runtime_data_purged": bool(purge_data),
        }

    async def restore_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        if any(not str(value or "").strip() for value in (tenant_id, user_id, workspace_id)):
            raise ValueError("tenant_id, user_id and workspace_id are required")
        return {
            "status": "active",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }

    async def reap_persistent_runtimes(self) -> list[str]:
        now = time.time()
        changed: list[str] = []
        for record in self.runtime_registry.list():
            try:
                if (
                    record.status == "running"
                    and record.idle_stop_seconds > 0
                    and now - record.last_used_at >= record.idle_stop_seconds
                    and not self._runtime_has_active_jobs(record)
                ):
                    await self.manage_persistent_runtime(
                        record.runtime_id,
                        "stop",
                        owner={key: getattr(record, key) for key in ("tenant_id", "user_id", "workspace_id")},
                        force=False,
                    )
                    changed.append(record.runtime_id)
                    continue
                if (
                    record.status == "stopped"
                    and record.retention_seconds > 0
                    and record.stopped_at > 0
                    and now - record.stopped_at >= record.retention_seconds
                ):
                    await self.manage_persistent_runtime(
                        record.runtime_id,
                        "delete",
                        owner={key: getattr(record, key) for key in ("tenant_id", "user_id", "workspace_id")},
                        force=False,
                        purge_data=False,
                    )
                    changed.append(record.runtime_id)
            except Exception:
                continue
        return changed

    async def _finalize_job(
        self,
        job: SandboxJob,
        *,
        exit_code: int | None,
        commit: bool,
        remove_container: bool = True,
    ) -> dict[str, Any]:
        job_id = str(job.sandbox_job_id or job.container_id or job.container_name)
        lock = self._finalize_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            cached = self._finalized_jobs.get(job_id)
            if cached is not None:
                job.metadata = dict(cached)
                return dict(cached)
            finalized = await self._finalize_job_locked(
                job,
                exit_code=exit_code,
                commit=commit,
                remove_container=remove_container,
            )
            self._finalized_jobs[job_id] = dict(finalized)
            return finalized

    async def _finalize_job_locked(
        self,
        job: SandboxJob,
        *,
        exit_code: int | None,
        commit: bool,
        remove_container: bool,
    ) -> dict[str, Any]:
        metadata = dict(job.metadata or {})
        if bool(metadata.get("workspace_finalized")):
            return metadata

        log_file = self._job_log_file(job)
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        persisted_log_metadata: dict[str, Any] = {}
        try:
            persisted_log_metadata = await self._persist_container_logs(
                job.container_id or job.container_name,
                log_file,
            )
        except Exception as exc:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"\nUnable to capture container logs: {exc}\n")
            persisted_log_metadata = {"persisted_log_error": str(exc)[:1000]}

        workspace_commit = await self._finalize_workspace(
            dict(metadata.get("workspace_snapshot") or {}),
            commit=commit,
        )
        if remove_container:
            await self._cleanup_container(job.container_id or job.container_name)
        await self._cleanup_network(
            str(metadata.get("sandbox_network_name") or ""),
            list(metadata.get("sandbox_network_attachments") or []),
        )
        finalized = {
            **metadata,
            "workspace_finalized": True,
            "workspace_commit": workspace_commit,
            "job_log_file": log_file.as_posix(),
            "exit_code": exit_code,
            **persisted_log_metadata,
        }
        if workspace_commit.get("status") == "conflict":
            job.status = "conflict"
        job.metadata = finalized
        return finalized

    async def _persist_container_logs(
        self,
        container_ref: str,
        log_file: Path,
    ) -> dict[str, Any]:
        buffer = BoundedBytesBuffer(self.config.persisted_log_max_bytes)
        proc = await asyncio.create_subprocess_exec(
            self.config.docker_binary,
            "logs",
            container_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            if proc.stdout is not None:
                while True:
                    chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=120)
                    if not chunk:
                        break
                    buffer.append(chunk)
            await asyncio.wait_for(proc.wait(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("docker logs timed out after 120s")
        if proc.returncode != 0:
            raise RuntimeError(f"docker logs failed with exit code {proc.returncode}")

        rendered = buffer.render(label="persisted sandbox log truncated")
        temporary = log_file.with_name(f".{log_file.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as handle:
            handle.write(rendered.data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
            os.replace(temporary, log_file)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return {
            "persisted_log_original_bytes": rendered.original_bytes,
            "persisted_log_shown_bytes": rendered.shown_bytes,
            "persisted_log_limit_bytes": self.config.persisted_log_max_bytes,
            "persisted_log_truncated": rendered.truncated,
        }

    def _job_log_file(self, job: SandboxJob) -> Path:
        identifier = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(job.sandbox_job_id or job.container_id or job.container_name),
        ).strip(".-")
        if not identifier:
            identifier = uuid.uuid4().hex
        return self.job_log_root / f"{identifier[:128]}.log"

    @staticmethod
    def _job_expired(job: SandboxJob) -> bool:
        metadata = dict(job.metadata or {})
        try:
            started_at = float(metadata.get("started_at") or 0)
            timeout_seconds = int(metadata.get("timeout_seconds") or 0)
        except (TypeError, ValueError):
            return False
        return (
            started_at > 0
            and timeout_seconds > 0
            and time.time() >= started_at + timeout_seconds
        )

    def _tail_file(self, path: Path, lines: int) -> str:
        limit = max(1, min(5000, int(lines or 200)))
        try:
            max_bytes = self.config.job_output_response_max_bytes
            chunks: list[bytes] = []
            bytes_read = 0
            newline_count = 0
            with path.open("rb") as handle:
                handle.seek(0, 2)
                position = handle.tell()
                while position > 0 and bytes_read < max_bytes:
                    size = min(64 * 1024, position, max_bytes - bytes_read)
                    position -= size
                    handle.seek(position)
                    chunk = handle.read(size)
                    chunks.append(chunk)
                    bytes_read += len(chunk)
                    newline_count += chunk.count(b"\n")
                    if newline_count > limit:
                        break
            text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
            output = "\n".join(text.splitlines()[-limit:])
            if position > 0:
                return f"[log tail truncated]\n{output}"
            return output
        except Exception as exc:
            return f"Unable to read persisted process log: {exc}"

    async def _cleanup_container(self, name: str) -> None:
        try:
            await self._communicate([self.config.docker_binary, "rm", "-f", name], timeout=15)
        except Exception:
            pass

    @staticmethod
    def _map_status(docker_status: str, exit_code: Any) -> str:
        if docker_status in {"running", "created", "restarting", "paused"}:
            return "running"
        if docker_status == "exited":
            return "completed" if exit_code == 0 else "failed"
        if docker_status in {"dead", "removing"}:
            return "failed"
        return docker_status or "unknown"

    @staticmethod
    def _container_ref(job: SandboxJob | str) -> str:
        if isinstance(job, SandboxJob):
            return job.container_id or job.container_name
        return str(job)

    @staticmethod
    def _job_id(job: SandboxJob | str) -> str:
        if isinstance(job, SandboxJob):
            return job.sandbox_job_id
        return str(job)
