from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Protocol


TERMINAL_STATUSES = {
    "completed",
    "conflict",
    "failed",
    "timed_out",
    "terminated",
    "missing",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def sandbox_enabled() -> bool:
    if strict_isolation_enabled():
        return True
    enabled_raw = os.environ.get("TCODE_SANDBOX_ENABLED")
    if enabled_raw is not None and enabled_raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    mode = str(os.environ.get("TCODE_SANDBOX_MODE", "") or "").strip().lower()
    if mode:
        return mode not in {"0", "false", "no", "off", "disabled"}
    return _env_bool("TCODE_SANDBOX_ENABLED", default=False)


def strict_isolation_enabled() -> bool:
    mode = str(os.environ.get("TCODE_ISOLATION_MODE", "") or "").strip().lower()
    if mode not in {"", "development", "strict"}:
        raise RuntimeError(f"unsupported TCODE_ISOLATION_MODE: {mode}")
    return mode == "strict"


def file_isolation_enabled() -> bool:
    return strict_isolation_enabled() or os.environ.get("TCODE_DESKTOP_MANAGED") == "1"


@dataclass(frozen=True)
class SandboxMount:
    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool
    backend: str
    docker_runtime: str
    docker_binary: str
    image: str
    workspace_root: Optional[Path]
    container_workspace: str
    network: str
    memory: str
    cpus: str
    pids_limit: int
    timeout_seconds: int
    read_only: bool
    user: str
    home: str
    tmpfs: str
    shm_size: str
    runner_url: str = ""
    runner_token: str = ""
    runner_token_file: str = ""
    require_runner: bool = False
    egress_proxy: str = ""
    egress_proxy_container: str = ""
    workspace_snapshots: bool = False
    snapshot_root: str = ""
    conflict_root: str = ""
    job_log_root: str = ""
    log_max_size: str = "10m"
    log_max_files: int = 2
    job_output_response_max_bytes: int = 512 * 1024
    persisted_log_max_bytes: int = 2 * 1024 * 1024
    persistent_job_output_max_bytes: int = 2 * 1024 * 1024
    workspace_service_policy_file: str = ""
    profile_name: str = ""
    lifecycle: str = "ephemeral"
    idle_stop_seconds: int = 3600
    retention_seconds: int = 0
    runtime_state_file: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    static_mounts: tuple[SandboxMount, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkspacePathMap:
    workspace_id: str
    host_workspace: Path
    container_workspace: str = "/workspace"


@dataclass(frozen=True)
class SandboxSpec:
    tool_name: str
    command: str
    pathmap: WorkspacePathMap
    host_cwd: Path
    container_cwd: str
    timeout_seconds: int
    network: str
    env: dict[str, str] = field(default_factory=dict)
    image: str = ""
    memory: str = ""
    cpus: str = ""
    pids_limit: int = 0
    read_only: Optional[bool] = None
    user: str = ""
    home: str = ""
    tmpfs: str = ""
    shm_size: str = ""
    mounts: list[SandboxMount] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    lifecycle: str = "ephemeral"
    idle_stop_seconds: int = 3600
    retention_seconds: int = 0


@dataclass
class SandboxResult:
    status: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    sandbox_job_id: str = ""
    container_id: str = ""
    container_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxJob:
    sandbox_job_id: str
    container_id: str
    container_name: str
    command: str
    status: str = "running"
    process_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxJobStatus:
    sandbox_job_id: str
    container_id: str
    container_name: str
    status: str
    exit_code: Optional[int] = None
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class SandboxRuntime(Protocol):
    async def run_once(self, spec: SandboxSpec) -> SandboxResult:
        ...

    async def start_job(self, spec: SandboxSpec) -> SandboxJob:
        ...

    async def job_status(self, job: SandboxJob | str) -> SandboxJobStatus:
        ...

    async def job_output(self, job: SandboxJob | str, lines: int = 200) -> str:
        ...

    async def terminate_job(self, job: SandboxJob | str) -> SandboxJobStatus:
        ...

    async def list_persistent_runtimes(
        self,
        *,
        tenant_id: str = "",
        user_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        ...

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
        ...

    async def cleanup_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        purge_data: bool = False,
    ) -> dict[str, Any]:
        ...

    async def restore_workspace(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        ...


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = str(
        os.environ.get(name, "true" if default else "false") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def root_runtime_enabled() -> bool:
    return _env_enabled("TCODE_SANDBOX_ROOT_RUNTIME_ENABLED", False)


def root_runtime_profile_name() -> str:
    return str(
        os.environ.get(
            "TCODE_SANDBOX_ROOT_RUNTIME_PROFILE",
            "workspace_root_runtime",
        )
        or "workspace_root_runtime"
    ).strip()


def root_runtime_base_profiles() -> tuple[str, ...]:
    raw = str(
        os.environ.get(
            "TCODE_SANDBOX_ROOT_RUNTIME_BASE_PROFILES",
            "workspace_runtime,executor,networked,default",
        )
        or ""
    )
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def root_runtime_user_ids() -> tuple[str, ...]:
    raw = str(os.environ.get("TCODE_SANDBOX_ROOT_RUNTIME_USER_IDS", "") or "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def root_runtime_user_allowed(user_id: str) -> bool:
    normalized = str(user_id or "").strip()
    return bool(
        normalized
        and root_runtime_enabled()
        and normalized in root_runtime_user_ids()
    )


def _load_static_resources(
    container_workspace: str,
) -> tuple[tuple[SandboxMount, ...], dict[str, str]]:
    config_file = str(
        os.environ.get("TCODE_SANDBOX_STATIC_RESOURCES_FILE", "") or ""
    ).strip()
    if not config_file:
        return (), {}

    path = Path(config_file).expanduser().resolve(strict=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Unable to load sandbox static resources file {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Sandbox static resources file must contain a JSON object")

    external_root = f"{container_workspace.rstrip('/')}/.external/"
    mounts: list[SandboxMount] = []
    for raw_mount in list(payload.get("mounts") or []):
        if not isinstance(raw_mount, dict):
            raise RuntimeError("Sandbox static resource mounts must be JSON objects")
        source_text = str(raw_mount.get("source") or "").strip()
        target = str(raw_mount.get("target") or "").strip()
        if not source_text or not target:
            raise RuntimeError("Sandbox static resource mount requires source and target")
        if raw_mount.get("read_only", True) is not True:
            raise RuntimeError("Sandbox static resource mounts must be read-only")
        target_path = PurePosixPath(target)
        if (
            not target.startswith(external_root)
            or not target_path.is_absolute()
            or ".." in target_path.parts
            or "." in target_path.parts
        ):
            raise RuntimeError(
                f"Sandbox static resource target must be under {external_root}: {target}"
            )
        mounts.append(
            SandboxMount(
                source=Path(source_text).expanduser().resolve(strict=False),
                target=target,
                read_only=True,
            )
        )

    raw_env = payload.get("env") or {}
    if not isinstance(raw_env, dict):
        raise RuntimeError("Sandbox static resource env must be a JSON object")
    env: dict[str, str] = {}
    for raw_key, raw_value in raw_env.items():
        key = str(raw_key or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise RuntimeError(f"Invalid sandbox static resource env name: {key}")
        env[key] = str(raw_value or "")

    path_prepend = payload.get("path_prepend") or []
    if not isinstance(path_prepend, list):
        raise RuntimeError("Sandbox static resource path_prepend must be a JSON list")
    normalized_paths: list[str] = []
    for raw_path in path_prepend:
        item = str(raw_path or "").strip().rstrip("/")
        item_path = PurePosixPath(item)
        if not item or not item_path.is_absolute() or ".." in item_path.parts:
            raise RuntimeError(f"Invalid sandbox PATH entry: {item}")
        normalized_paths.append(item)
    if normalized_paths:
        env["TCODE_SANDBOX_PATH_PREPEND"] = ":".join(normalized_paths)

    deduped_mounts = {mount.target: mount for mount in mounts}
    return tuple(deduped_mounts.values()), env


def _parse_profile_mounts(
    raw_mounts: Any,
    *,
    container_workspace: str,
) -> tuple[SandboxMount, ...]:
    if raw_mounts is None:
        return ()
    if not isinstance(raw_mounts, list):
        raise ValueError("sandbox profile mounts must be a list")

    external_root = f"{container_workspace.rstrip('/')}/.external/"
    mounts: dict[str, SandboxMount] = {}
    for raw_mount in raw_mounts:
        if not isinstance(raw_mount, dict):
            raise ValueError("sandbox profile mount must be an object")
        if raw_mount.get("read_only", True) is not True:
            raise ValueError("sandbox profile mounts must be read-only")
        source_text = str(raw_mount.get("source") or "").strip()
        target = str(raw_mount.get("target") or "").strip()
        target_path = PurePosixPath(target)
        if not source_text or not target:
            raise ValueError("sandbox profile mount requires source and target")
        if (
            not target.startswith(external_root)
            or not target_path.is_absolute()
            or ".." in target_path.parts
            or "." in target_path.parts
        ):
            raise ValueError(
                f"sandbox profile mount target must be under {external_root}"
            )
        mounts[target] = SandboxMount(
            source=Path(source_text).expanduser().resolve(strict=False),
            target=target,
            read_only=True,
        )
    return tuple(mounts.values())


def load_sandbox_config() -> SandboxConfig:
    workspace_root_raw = str(os.environ.get("TCODE_SANDBOX_WORKSPACE_ROOT", "") or "").strip()
    workspace_root = None
    if workspace_root_raw:
        workspace_root = Path(workspace_root_raw).expanduser()
        if not workspace_root.is_absolute():
            configured_base = str(
                os.environ.get("TCODE_SERVER_WORKDIR")
                or os.environ.get("TCODE_CONFIG_ROOT")
                or ""
            ).strip()
            base = (
                Path(configured_base).expanduser()
                if configured_base
                else Path(__file__).resolve().parents[3]
            )
            workspace_root = base / workspace_root
        workspace_root = workspace_root.resolve(strict=False)
    uid_gid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    docker_runtime_raw = os.environ.get("TCODE_SANDBOX_DOCKER_RUNTIME")
    container_workspace = str(
        os.environ.get("TCODE_SANDBOX_CONTAINER_WORKDIR", "/workspace")
        or "/workspace"
    ).rstrip("/") or "/workspace"
    static_mounts, static_env = _load_static_resources(container_workspace)
    return SandboxConfig(
        enabled=sandbox_enabled(),
        backend=str(os.environ.get("TCODE_SANDBOX_BACKEND", "docker-gvisor") or "docker-gvisor").strip(),
        docker_runtime=(
            str(docker_runtime_raw).strip()
            if docker_runtime_raw is not None
            else "runsc"
        ),
        docker_binary=str(os.environ.get("TCODE_SANDBOX_DOCKER_BINARY", "docker") or "docker").strip(),
        image=str(os.environ.get("TCODE_SANDBOX_IMAGE", "tcode-executor:py312") or "tcode-executor:py312").strip(),
        workspace_root=workspace_root,
        container_workspace=container_workspace,
        network=str(os.environ.get("TCODE_SANDBOX_DEFAULT_NETWORK", "none") or "none").strip(),
        memory=str(os.environ.get("TCODE_SANDBOX_DEFAULT_MEMORY", "2g") or "2g").strip(),
        cpus=str(os.environ.get("TCODE_SANDBOX_DEFAULT_CPUS", "2") or "2").strip(),
        pids_limit=_env_int("TCODE_SANDBOX_DEFAULT_PIDS_LIMIT", 256),
        timeout_seconds=_env_int("TCODE_SANDBOX_DEFAULT_TIMEOUT_SECONDS", 120),
        read_only=_env_bool("TCODE_SANDBOX_READ_ONLY", default=True),
        user=str(os.environ.get("TCODE_SANDBOX_USER", uid_gid) or uid_gid).strip(),
        home=str(os.environ.get("TCODE_SANDBOX_HOME", "/tmp/home") or "/tmp/home").strip(),
        tmpfs=str(os.environ.get("TCODE_SANDBOX_TMPFS", "/tmp:rw,nosuid,nodev,size=512m") or "/tmp:rw,nosuid,nodev,size=512m").strip(),
        shm_size=str(os.environ.get("TCODE_SANDBOX_SHM_SIZE", "1g") or "1g").strip(),
        runner_url=str(os.environ.get("TCODE_SANDBOX_RUNNER_URL", "") or "").strip().rstrip("/"),
        runner_token=str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN", "") or "").strip(),
        runner_token_file=str(os.environ.get("TCODE_SANDBOX_RUNNER_TOKEN_FILE", "") or "").strip(),
        require_runner=_env_bool("TCODE_SANDBOX_REQUIRE_RUNNER", default=False),
        egress_proxy=str(os.environ.get("TCODE_SANDBOX_EGRESS_PROXY", "") or "").strip(),
        egress_proxy_container=str(
            os.environ.get("TCODE_SANDBOX_EGRESS_PROXY_CONTAINER", "") or ""
        ).strip(),
        workspace_snapshots=_env_bool(
            "TCODE_SANDBOX_WORKSPACE_SNAPSHOTS",
            default=False,
        ),
        snapshot_root=str(
            os.environ.get(
                "TCODE_SANDBOX_SNAPSHOT_ROOT",
                "/tmp/tcode-sandbox-runner/snapshots",
            )
            or "/tmp/tcode-sandbox-runner/snapshots"
        ).strip(),
        conflict_root=str(
            os.environ.get(
                "TCODE_SANDBOX_CONFLICT_ROOT",
                "/tmp/tcode-sandbox-runner/conflicts",
            )
            or "/tmp/tcode-sandbox-runner/conflicts"
        ).strip(),
        job_log_root=str(
            os.environ.get(
                "TCODE_SANDBOX_JOB_LOG_ROOT",
                "/tmp/tcode-sandbox-runner/logs",
            )
            or "/tmp/tcode-sandbox-runner/logs"
        ).strip(),
        log_max_size=str(
            os.environ.get("TCODE_SANDBOX_LOG_MAX_SIZE", "10m") or "10m"
        ).strip(),
        log_max_files=max(1, _env_int("TCODE_SANDBOX_LOG_MAX_FILES", 2)),
        job_output_response_max_bytes=max(
            64 * 1024,
            min(
                _env_int("TCODE_SANDBOX_JOB_OUTPUT_RESPONSE_MAX_BYTES", 512 * 1024),
                8 * 1024 * 1024,
            ),
        ),
        persisted_log_max_bytes=max(
            64 * 1024,
            min(
                _env_int("TCODE_SANDBOX_PERSISTED_LOG_MAX_BYTES", 2 * 1024 * 1024),
                32 * 1024 * 1024,
            ),
        ),
        persistent_job_output_max_bytes=max(
            64 * 1024,
            min(
                _env_int("TCODE_PERSISTENT_JOB_OUTPUT_MAX_BYTES", 2 * 1024 * 1024),
                32 * 1024 * 1024,
            ),
        ),
        workspace_service_policy_file=str(
            os.environ.get("TCODE_SANDBOX_WORKSPACE_SERVICE_POLICY_FILE", "") or ""
        ).strip(),
        lifecycle=str(
            os.environ.get("TCODE_SANDBOX_LIFECYCLE", "ephemeral") or "ephemeral"
        ).strip().lower(),
        idle_stop_seconds=max(
            0, _env_int("TCODE_SANDBOX_RUNTIME_IDLE_STOP_SECONDS", 3600)
        ),
        retention_seconds=max(
            0, _env_int("TCODE_SANDBOX_RUNTIME_RETENTION_SECONDS", 0)
        ),
        runtime_state_file=str(
            os.environ.get(
                "TCODE_SANDBOX_RUNTIME_STATE_FILE",
                "/tmp/tcode-sandbox-runner/runtimes.sqlite",
            )
            or "/tmp/tcode-sandbox-runner/runtimes.sqlite"
        ).strip(),
        extra_env=static_env,
        static_mounts=static_mounts,
    )


def apply_sandbox_config_overrides(
    base: SandboxConfig,
    overrides: dict[str, Any],
    *,
    profile_name: str = "",
) -> SandboxConfig:
    if not isinstance(overrides, dict) or not overrides:
        if profile_name and profile_name != base.profile_name:
            return replace(base, profile_name=profile_name)
        return base

    updated: dict[str, Any] = {}

    def _pick(*keys: str) -> Any:
        for key in keys:
            if key in overrides:
                return overrides.get(key)
        return None

    def _string_field(field_name: str, *keys: str) -> None:
        value = _pick(*keys)
        if value is None:
            return
        text = str(value or "").strip()
        if text:
            updated[field_name] = text

    def _int_field(field_name: str, *keys: str) -> None:
        value = _pick(*keys)
        if value is None or value == "":
            return
        try:
            updated[field_name] = int(value)
        except Exception:
            return

    def _bool_field(field_name: str, *keys: str) -> None:
        value = _pick(*keys)
        if value is None:
            return
        if isinstance(value, bool):
            updated[field_name] = value
            return
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            updated[field_name] = True
        elif text in {"0", "false", "no", "off"}:
            updated[field_name] = False

    _string_field("image", "image")
    _string_field("network", "network")
    _string_field("memory", "memory")
    _string_field("cpus", "cpus")
    _string_field("user", "user")
    _string_field("home", "home")
    _string_field("tmpfs", "tmpfs")
    _string_field("shm_size", "shm_size", "shmSize")
    _string_field("lifecycle", "lifecycle")
    _int_field("pids_limit", "pids_limit", "pidsLimit")
    _int_field("timeout_seconds", "timeout_seconds", "timeoutSeconds")
    _int_field("idle_stop_seconds", "idle_stop_seconds", "idleStopSeconds")
    _int_field("retention_seconds", "retention_seconds", "retentionSeconds")
    _bool_field("read_only", "read_only", "readOnly")

    env_overrides = _pick("env", "extra_env", "extraEnv")
    if isinstance(env_overrides, dict):
        updated["extra_env"] = {
            **dict(base.extra_env or {}),
            **{
                str(key): str(value)
                for key, value in env_overrides.items()
                if str(key).strip()
            },
        }

    mounts_override = _pick("mounts", "static_mounts", "staticMounts")
    if mounts_override is not None:
        profile_mounts = _parse_profile_mounts(
            mounts_override,
            container_workspace=base.container_workspace,
        )
        merged_mounts = {
            mount.target: mount
            for mount in (*base.static_mounts, *profile_mounts)
        }
        updated["static_mounts"] = tuple(merged_mounts.values())

    resolved_profile = str(profile_name or _pick("name", "profile") or "").strip()
    if resolved_profile:
        updated["profile_name"] = resolved_profile

    if not updated:
        return base
    return replace(base, **updated)


def get_sandbox_runtime(config: Optional[SandboxConfig] = None) -> SandboxRuntime:
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        if os.environ.get("TCODE_DESKTOP_NATIVE_READY") != "1":
            raise RuntimeError("macOS 本地隔离自检未通过，不会启动 Sandbox Runner 或退回宿主执行")
        from .macos_native import MacOSNativeRuntime
        return MacOSNativeRuntime()
    cfg = config or load_sandbox_config()
    if strict_isolation_enabled():
        if not cfg.enabled or cfg.docker_runtime != "runsc":
            raise RuntimeError("strict isolation requires an enabled runsc sandbox")
        if not cfg.runner_url or not cfg.require_runner:
            raise RuntimeError("strict isolation requires the authenticated sandbox Runner")
        if cfg.container_workspace != "/workspace":
            raise RuntimeError("strict isolation requires /workspace as the container workdir")
    if cfg.runner_url:
        from .http_runner import HttpSandboxRuntime

        return HttpSandboxRuntime(cfg)
    if cfg.require_runner:
        raise RuntimeError(
            "TCODE_SANDBOX_REQUIRE_RUNNER=true but TCODE_SANDBOX_RUNNER_URL is empty"
        )
    if cfg.backend not in {"docker-gvisor", "docker", "local-docker"}:
        raise RuntimeError(f"Unsupported sandbox backend: {cfg.backend}")
    from .docker_gvisor import DockerGvisorRuntime

    return DockerGvisorRuntime(cfg)
