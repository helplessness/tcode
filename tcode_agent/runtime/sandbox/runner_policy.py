from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .base import (
    SandboxConfig,
    SandboxMount,
    SandboxSpec,
    WorkspacePathMap,
    root_runtime_enabled,
    root_runtime_profile_name,
    root_runtime_user_ids,
    strict_isolation_enabled,
)
from .pathmap import assert_inside_workspace


_PROXY_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(os.environ.get(name, default) or "").split(",")
        if item.strip()
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _mount_roots(name: str) -> tuple[Path, ...]:
    roots = [Path(raw).expanduser().resolve(strict=True) for raw in _env_list(name)]
    # Preserve old runner configurations for the TCode checkout only. Never
    # derive a trusted sibling from a workspace or an arbitrary legacy root.
    projects = {Path(__file__).resolve().parents[3]}
    if configured := os.environ.get("TCODE_PROJECT_ROOT", "").strip():
        projects.add(Path(configured).expanduser().resolve())
    for project in projects:
        if project / ".claude/skills" in roots:
            current = project / ".tcode/skills"
            if current.is_dir() and current.resolve() == current:
                roots.append(current)
    return tuple(dict.fromkeys(roots))


def _parse_memory_bytes(value: str) -> int:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+)([kmgt]?)b?", text)
    if not match:
        raise ValueError(f"invalid memory limit: {value}")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError(f"memory limit must be positive: {value}")
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}[match.group(2)]
    return amount * (1024**power)


def _read_path(value: Any, *, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"{field} contains invalid characters")
    return Path(text).expanduser().resolve(strict=False)


def _inside_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            assert_inside_workspace(path, root)
            return True
        except ValueError:
            continue
    return False


def _load_tool_runtime_policies() -> dict[str, dict[str, Any]]:
    path_text = str(
        os.environ.get("TCODE_SANDBOX_RUNNER_TOOL_POLICY_FILE", "") or ""
    ).strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve(strict=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Unable to load sandbox runner tool policy file {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Sandbox runner tool policy must be a JSON object")

    policies: dict[str, dict[str, Any]] = {}
    for raw_key, raw_policy in payload.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_policy, dict):
            raise RuntimeError("Invalid sandbox runner tool policy entry")

        def _items(name: str) -> tuple[str, ...]:
            raw = raw_policy.get(name) or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise RuntimeError(
                    f"Sandbox runner tool policy '{key}.{name}' must be a list"
                )
            return tuple(
                str(item or "").strip()
                for item in raw
                if str(item or "").strip()
            )

        def _boolean(name: str, default: bool) -> bool:
            value = raw_policy.get(name, default)
            if isinstance(value, bool):
                return value
            raise RuntimeError(
                f"Sandbox runner tool policy '{key}.{name}' must be a boolean"
            )

        policies[key] = {
            "images": _items("images"),
            "networks": _items("networks"),
            "require_read_only": _boolean("require_read_only", False),
            "allow_root": _boolean("allow_root", False),
        }
    return policies


@dataclass(frozen=True)
class SandboxRunnerPolicy:
    workspace_root: Path
    allowed_images: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_networks: tuple[str, ...]
    allowed_mount_roots: tuple[Path, ...]
    max_timeout_seconds: int
    max_memory_bytes: int
    max_shm_bytes: int
    max_cpus: float
    max_pids: int
    max_command_bytes: int
    max_env_entries: int
    max_env_value_bytes: int
    require_owner_metadata: bool = True
    allowed_users: tuple[str, ...] = ()
    allow_container_root: bool = False
    allow_writable_root: bool = False
    root_runtime_enabled: bool = False
    root_runtime_user_ids: tuple[str, ...] = ()
    root_runtime_profile: str = "workspace_root_runtime"
    tool_runtime_policies: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    trusted_mount_roots: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls, config: SandboxConfig) -> "SandboxRunnerPolicy":
        root_text = str(
            os.environ.get("TCODE_SANDBOX_RUNNER_WORKSPACE_ROOT")
            or config.workspace_root
            or ""
        ).strip()
        if not root_text:
            raise RuntimeError(
                "TCODE_SANDBOX_RUNNER_WORKSPACE_ROOT or TCODE_SANDBOX_WORKSPACE_ROOT is required"
            )
        workspace_root = Path(root_text).expanduser().resolve(strict=True)
        if not workspace_root.is_dir():
            raise RuntimeError(f"Sandbox runner workspace root is not a directory: {workspace_root}")

        default_images = ",".join(
            item
            for item in (
                config.image,
                str(os.environ.get("TCODE_BROWSER_SANDBOX_IMAGE", "") or "").strip(),
            )
            if item
        )
        allowed_images = _env_list("TCODE_SANDBOX_RUNNER_ALLOWED_IMAGES", default_images)
        if not allowed_images:
            raise RuntimeError("TCODE_SANDBOX_RUNNER_ALLOWED_IMAGES is empty")

        mount_roots = _mount_roots("TCODE_SANDBOX_RUNNER_ALLOWED_MOUNT_ROOTS")
        for path in mount_roots:
            if not path.is_dir():
                raise RuntimeError(f"Allowed mount root is not a directory: {path}")

        allowed_networks = _env_list(
            "TCODE_SANDBOX_RUNNER_ALLOWED_NETWORKS",
            "none,internal,egress",
        )
        unsupported_networks = sorted(
            set(allowed_networks) - {"none", "internal", "egress"}
        )
        if unsupported_networks:
            raise RuntimeError(
                "TCODE_SANDBOX_RUNNER_ALLOWED_NETWORKS contains unsupported "
                f"policies: {', '.join(unsupported_networks)}"
            )

        return cls(
            workspace_root=workspace_root,
            allowed_images=allowed_images,
            allowed_tools=_env_list(
                "TCODE_SANDBOX_RUNNER_ALLOWED_TOOLS",
                "bash,exec,read,write,edit,glob,grep,browser,skill_run,plugin_runtime,plugin_network,plugin:*",
            ),
            allowed_networks=allowed_networks,
            allowed_mount_roots=mount_roots,
            trusted_mount_roots=_mount_roots("TCODE_SANDBOX_RUNNER_TRUSTED_MOUNT_ROOTS"),
            max_timeout_seconds=max(
                1, _env_int("TCODE_SANDBOX_RUNNER_MAX_TIMEOUT_SECONDS", 3600)
            ),
            max_memory_bytes=_parse_memory_bytes(
                os.environ.get("TCODE_SANDBOX_RUNNER_MAX_MEMORY", "8g")
            ),
            max_shm_bytes=_parse_memory_bytes(
                os.environ.get("TCODE_SANDBOX_RUNNER_MAX_SHM_SIZE", "2g")
            ),
            max_cpus=max(
                0.1,
                float(os.environ.get("TCODE_SANDBOX_RUNNER_MAX_CPUS", "8") or "8"),
            ),
            max_pids=max(1, _env_int("TCODE_SANDBOX_RUNNER_MAX_PIDS", 512)),
            max_command_bytes=max(
                1024, _env_int("TCODE_SANDBOX_RUNNER_MAX_COMMAND_BYTES", 131072)
            ),
            max_env_entries=max(
                1, _env_int("TCODE_SANDBOX_RUNNER_MAX_ENV_ENTRIES", 64)
            ),
            max_env_value_bytes=max(
                128, _env_int("TCODE_SANDBOX_RUNNER_MAX_ENV_VALUE_BYTES", 65536)
            ),
            require_owner_metadata=str(
                os.environ.get(
                    "TCODE_SANDBOX_RUNNER_REQUIRE_OWNER_METADATA",
                    "true",
                )
                or "true"
            ).strip().lower()
            not in {"0", "false", "no", "off"},
            allowed_users=_env_list(
                "TCODE_SANDBOX_RUNNER_ALLOWED_USERS",
                config.user,
            ),
            allow_container_root=str(
                os.environ.get(
                    "TCODE_SANDBOX_RUNNER_ALLOW_CONTAINER_ROOT",
                    "false",
                )
                or "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            allow_writable_root=str(
                os.environ.get(
                    "TCODE_SANDBOX_RUNNER_ALLOW_WRITABLE_ROOT",
                    "false",
                )
                or "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            root_runtime_enabled=root_runtime_enabled(),
            root_runtime_user_ids=root_runtime_user_ids(),
            root_runtime_profile=root_runtime_profile_name(),
            tool_runtime_policies=_load_tool_runtime_policies(),
        )

    @staticmethod
    def _matches(value: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

    def _validate_tool_runtime_policy(
        self,
        *,
        tool_name: str,
        metadata: dict[str, Any],
        image: str,
        network: str,
        user: str = "",
        read_only: bool = True,
    ) -> None:
        policies = dict(self.tool_runtime_policies or {})
        capability = str(metadata.get("sandbox_capability") or "").strip()
        keys = []
        if capability:
            keys.append(f"{tool_name}#{capability}")
        keys.append(tool_name)

        policy = None
        policy_key = ""
        for key in keys:
            if key in policies:
                policy = policies[key]
                policy_key = key
                break
        if not policy:
            return

        images = tuple(policy.get("images") or ())
        if images and not self._matches(image, images):
            raise ValueError(
                f"image is not allowed by runner tool policy '{policy_key}': {image}"
            )
        networks = tuple(policy.get("networks") or ())
        if networks and not self._matches(network, networks):
            raise ValueError(
                f"network is not allowed by runner tool policy "
                f"'{policy_key}': {network}"
            )
        if bool(policy.get("require_read_only")) and read_only is not True:
            raise ValueError(
                f"runner tool policy '{policy_key}' requires a read-only root filesystem"
            )
        is_root = user in {"0", "0:0", "root"} or user.startswith("0:")
        if is_root and not bool(policy.get("allow_root")):
            raise ValueError(
                f"runner tool policy '{policy_key}' refuses a root container user"
            )

    def _validate_env(
        self,
        raw_env: Any,
        *,
        network: str,
        config: SandboxConfig,
    ) -> dict[str, str]:
        if not isinstance(raw_env, dict):
            raw_env = {}
        merged_env = {
            **raw_env,
            **dict(config.extra_env or {}),
        }
        if len(merged_env) > self.max_env_entries:
            raise ValueError("too many sandbox environment variables")

        env: dict[str, str] = {}
        for raw_key, raw_value in merged_env.items():
            key = str(raw_key or "").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment variable name: {key}")
            if key in _PROXY_ENV_KEYS:
                continue
            value = str(raw_value or "")
            if len(value.encode("utf-8")) > self.max_env_value_bytes:
                raise ValueError(f"environment variable is too large: {key}")
            env[key] = value

        if network == "egress":
            proxy = str(config.egress_proxy or "").strip()
            parsed = urlparse(proxy)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("TCODE_SANDBOX_EGRESS_PROXY must be a valid HTTP(S) proxy URL")
            env.update(
                {
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "ALL_PROXY": proxy,
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "all_proxy": proxy,
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                }
            )
        return env

    def validate(self, payload: dict[str, Any], config: SandboxConfig) -> SandboxSpec:
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")

        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name or not self._matches(tool_name, self.allowed_tools):
            raise ValueError(f"tool is not allowed by runner policy: {tool_name}")

        command = str(payload.get("command") or "")
        if not command.strip():
            raise ValueError("command is required")
        if len(command.encode("utf-8")) > self.max_command_bytes:
            raise ValueError("command exceeds runner size limit")

        image = str(payload.get("image") or config.image or "").strip()
        if image not in self.allowed_images:
            raise ValueError(f"image is not allowed by runner policy: {image}")

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if strict_isolation_enabled():
            execution_id = str(metadata.get("execution_id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", execution_id):
                raise ValueError("strict isolation requires a valid execution_id")
        metadata_runtime_workspace_id = str(
            metadata.get("runtime_workspace_id") or ""
        ).strip()
        payload_workspace_id = str(payload.get("workspace_id") or "").strip()
        if (
            metadata_runtime_workspace_id
            and payload_workspace_id
            and metadata_runtime_workspace_id != payload_workspace_id
        ):
            raise ValueError(
                "runtime workspace_id payload and metadata do not match"
            )

        lifecycle = str(payload.get("lifecycle") or "ephemeral").strip().lower()
        if lifecycle not in {"ephemeral", "persistent"}:
            raise ValueError("lifecycle must be ephemeral or persistent")
        if lifecycle == "persistent" and tool_name not in {"bash", "exec", "browser"}:
            raise ValueError(
                "persistent lifecycle is only allowed for bash, exec and the dedicated browser worker"
            )

        runner_workspace_root = self.workspace_root.resolve(strict=True)
        host_workspace = _read_path(payload.get("host_workspace"), field="host_workspace")
        host_workspace = assert_inside_workspace(host_workspace, runner_workspace_root)
        if host_workspace == runner_workspace_root:
            raise ValueError("host_workspace cannot be the runner workspace root")
        if not host_workspace.is_dir():
            raise ValueError("host_workspace must be an existing directory")
        self._validate_workspace_owner(
            host_workspace,
            runner_workspace_root,
            metadata,
            payload,
        )

        host_cwd = _read_path(
            payload.get("host_cwd") or host_workspace,
            field="host_cwd",
        )
        host_cwd = assert_inside_workspace(host_cwd, host_workspace)
        if not host_cwd.is_dir():
            raise ValueError("host_cwd must be an existing directory")

        container_workspace = str(
            payload.get("container_workspace") or config.container_workspace
        ).rstrip("/")
        if container_workspace != config.container_workspace.rstrip("/"):
            raise ValueError("container_workspace cannot be overridden")
        container_cwd = str(payload.get("container_cwd") or container_workspace).strip()
        container_cwd_path = PurePosixPath(container_cwd)
        container_workspace_path = PurePosixPath(container_workspace)
        if (
            not container_cwd_path.is_absolute()
            or ".." in container_cwd_path.parts
            or not (
                container_cwd_path == container_workspace_path
                or container_workspace_path in container_cwd_path.parents
            )
        ):
            raise ValueError("container_cwd escapes container workspace")

        network = str(payload.get("network") or "none").strip().lower()
        if network not in {"none", "internal", "egress"}:
            raise ValueError(f"unsupported network policy: {network}")
        if network not in self.allowed_networks:
            raise ValueError(f"network policy is not allowed: {network}")
        if network == "egress" and (
            not config.egress_proxy or not config.egress_proxy_container
        ):
            raise ValueError(
                "egress policy requires TCODE_SANDBOX_EGRESS_PROXY and "
                "TCODE_SANDBOX_EGRESS_PROXY_CONTAINER"
            )
        timeout = int(payload.get("timeout_seconds") or config.timeout_seconds)
        if timeout < 1 or timeout > self.max_timeout_seconds:
            raise ValueError("timeout exceeds runner policy")

        memory = str(payload.get("memory") or config.memory).strip()
        if _parse_memory_bytes(memory) > self.max_memory_bytes:
            raise ValueError("memory exceeds runner policy")

        cpus = str(payload.get("cpus") or config.cpus).strip()
        try:
            cpus_value = float(cpus)
        except Exception as exc:
            raise ValueError("invalid CPU limit") from exc
        if cpus_value <= 0 or cpus_value > self.max_cpus:
            raise ValueError("CPU limit exceeds runner policy")

        pids_limit = int(payload.get("pids_limit") or config.pids_limit)
        if pids_limit < 1 or pids_limit > self.max_pids:
            raise ValueError("PID limit exceeds runner policy")

        read_only = payload.get("read_only")
        if read_only is None:
            read_only = config.read_only
        if not isinstance(read_only, bool):
            raise ValueError("read_only must be a boolean")
        if not read_only and not self.allow_writable_root:
            raise ValueError("runner requires a read-only container root filesystem")

        user = str(payload.get("user") or config.user).strip()
        if not user or user not in self.allowed_users:
            raise ValueError("container user is not allowed by runner policy")
        is_root = user in {"0", "0:0", "root"} or user.startswith("0:")
        if is_root and not self.allow_container_root:
            raise ValueError("runner refuses root containers")
        owner_user_id = str(metadata.get("user_id") or "").strip()
        profile_name = str(metadata.get("sandbox_profile") or "").strip()
        if lifecycle == "persistent" and tool_name == "browser":
            if (
                profile_name != "browser"
                or metadata.get("browser_live") is not True
                or str(metadata.get("browser_action") or "") != "live_worker"
            ):
                raise ValueError(
                    "persistent browser runtime requires the fixed browser live-worker profile"
                )
        root_user_allowed = bool(
            self.root_runtime_enabled
            and owner_user_id
            and owner_user_id in self.root_runtime_user_ids
        )
        root_profile_requested = profile_name == self.root_runtime_profile
        if is_root and not root_user_allowed:
            raise ValueError("authenticated user is not allowed to use root runtime")
        if root_profile_requested and not root_user_allowed:
            raise ValueError("authenticated user is not allowed to use root runtime")
        if root_profile_requested and lifecycle != "persistent":
            raise ValueError("root runtime profile requires persistent lifecycle")
        if lifecycle == "persistent" and root_profile_requested:
            if not is_root or read_only:
                raise ValueError(
                    "root runtime profile requires root user and writable root filesystem"
                )
        elif lifecycle == "persistent" and (is_root or read_only is not True):
            raise ValueError(
                "persistent runtime requires a read-only root filesystem unless "
                "the authorized root profile is used"
            )
        self._validate_tool_runtime_policy(
            tool_name=tool_name,
            metadata=metadata,
            image=image,
            network=network,
            user=user,
            read_only=read_only,
        )

        home = str(payload.get("home") or config.home).strip()
        if "\x00" in home or "\n" in home or "\r" in home:
            raise ValueError("container home contains invalid characters")
        persistent_home_root = f"{container_workspace}/.tcode-runtime/"
        home_path = PurePosixPath(home)
        persistent_home = (
            home.startswith(persistent_home_root)
            and home_path.is_absolute()
            and ".." not in home_path.parts
            and "." not in home_path.parts
        )
        if lifecycle == "persistent" and not persistent_home:
            raise ValueError("persistent runtime home must be under workspace runtime data")
        if lifecycle != "persistent" and home != config.home:
            raise ValueError("container home cannot be overridden")

        tmpfs = str(payload.get("tmpfs") or config.tmpfs).strip()
        if tmpfs != config.tmpfs:
            raise ValueError("container tmpfs cannot be overridden")

        shm_size = str(payload.get("shm_size") or config.shm_size).strip()
        if _parse_memory_bytes(shm_size) > self.max_shm_bytes:
            raise ValueError("shared memory size exceeds runner policy")

        mounts: list[SandboxMount] = []
        raw_mounts = payload.get("mounts")
        if raw_mounts is None:
            raw_mounts = []
        if not isinstance(raw_mounts, list):
            raise ValueError("mounts must be a list")
        configured_mounts = [
            {
                "source": mount.source.as_posix(),
                "target": mount.target,
                "read_only": mount.read_only,
            }
            for mount in config.static_mounts
        ]
        deduped_mounts: dict[str, SandboxMount] = {}
        for raw_mount in [*raw_mounts, *configured_mounts]:
            if not isinstance(raw_mount, dict):
                raise ValueError("invalid mount entry")
            if raw_mount.get("read_only", True) is not True:
                raise ValueError("external mounts must be read-only")
            source = _read_path(raw_mount.get("source"), field="mount.source")
            if not source.exists():
                raise ValueError(f"external mount does not exist: {source}")
            target = str(raw_mount.get("target") or "").strip()
            external_root = f"{container_workspace}/.external/"
            protein_inputs_root = f"{container_workspace}/inputs"
            target_path = PurePosixPath(target)
            external_target = target.startswith(external_root)
            protein_input_target = (
                target == protein_inputs_root
                or target.startswith(f"{protein_inputs_root}/")
            )
            if (
                not (external_target or protein_input_target)
                or not target_path.is_absolute()
                or ".." in target_path.parts
                or "." in target_path.parts
            ):
                raise ValueError(
                    "external mount target must be under /workspace/.external/ "
                    "or the read-only /workspace/inputs/ tree"
                )
            source_in_workspace = False
            try:
                relative_source = source.relative_to(host_workspace)
                source_in_workspace = True
            except ValueError:
                relative_source = None
            if source_in_workspace:
                expected_target = (
                    f"{container_workspace}/{relative_source.as_posix()}"
                    if relative_source and relative_source.as_posix() != "."
                    else container_workspace
                )
                if not protein_input_target or target != expected_target:
                    raise ValueError(
                        "workspace-internal mount must preserve its /workspace relative path"
                    )
            else:
                if strict_isolation_enabled() and source.is_relative_to(runner_workspace_root):
                    raise ValueError("external mounts cannot expose another execution workspace")
                if not _inside_any_root(source, self.allowed_mount_roots):
                    raise ValueError(f"external mount is outside allowed roots: {source}")
                static = any(
                    source == mount.source.resolve(strict=True)
                    and target == mount.target and mount.read_only
                    for mount in config.static_mounts
                )
                if (self.require_owner_metadata or strict_isolation_enabled()) and not static and not _inside_any_root(source, self.trusted_mount_roots):
                    owner_parts = tuple(str(metadata.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id"))
                    if not any(
                        source.is_relative_to(root.resolve(strict=True).joinpath(*owner_parts))
                        for root in self.allowed_mount_roots
                    ):
                        raise ValueError("external input mount does not belong to this owner")
            deduped_mounts[target] = SandboxMount(
                source=source,
                target=target,
                read_only=True,
            )
        mounts.extend(deduped_mounts.values())

        return SandboxSpec(
            tool_name=tool_name,
            command=command,
            pathmap=WorkspacePathMap(
                workspace_id=str(payload.get("workspace_id") or host_workspace.name),
                host_workspace=host_workspace,
                container_workspace=container_workspace,
            ),
            host_cwd=host_cwd,
            container_cwd=container_cwd,
            timeout_seconds=timeout,
            network=network,
            env=self._validate_env(payload.get("env"), network=network, config=config),
            image=image,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            read_only=read_only,
            user=user,
            home=home,
            tmpfs=tmpfs,
            shm_size=shm_size,
            mounts=mounts,
            expected_artifacts=[
                str(item)
                for item in list(payload.get("expected_artifacts") or [])
                if str(item).strip()
            ][:128],
            metadata=dict(metadata),
            lifecycle=lifecycle,
            idle_stop_seconds=max(0, int(payload.get("idle_stop_seconds") or 0)),
            retention_seconds=max(0, int(payload.get("retention_seconds") or 0)),
        )

    def _validate_workspace_owner(
        self,
        host_workspace: Path,
        workspace_root: Path,
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if not self.require_owner_metadata and not strict_isolation_enabled():
            return
        tenant_id = str(metadata.get("tenant_id") or "").strip()
        user_id = str(metadata.get("user_id") or "").strip()
        workspace_id = str(
            metadata.get("workspace_id")
            or payload.get("workspace_id")
            or ""
        ).strip()
        if not tenant_id or not user_id or not workspace_id:
            raise ValueError("tenant_id, user_id and workspace_id metadata are required")
        safe_tenant = re.sub(r"[^A-Za-z0-9._@-]+", "-", tenant_id).strip(".-_@")
        safe_user = re.sub(r"[^A-Za-z0-9._@-]+", "-", user_id).strip(".-_@")
        safe_workspace = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace_id).strip(".-_")
        if safe_tenant != tenant_id or safe_user != user_id or safe_workspace != workspace_id:
            raise ValueError("workspace owner metadata is not normalized")

        relative = host_workspace.relative_to(workspace_root).parts
        candidates = {
            (safe_tenant, safe_user, safe_workspace),
            ("workdirs", safe_tenant, safe_user, safe_workspace),
        }
        # Isolated task worktrees and Protein Runs are nested below the
        # authenticated user workspace.  They have their own runtime ID but
        # inherit the same owner boundary.  Prefix matching permits those
        # descendants without allowing a sibling user/workspace.
        if not any(
            tuple(relative[: len(candidate)]) == candidate
            for candidate in candidates
        ):
            raise ValueError(
                "host_workspace does not match authenticated user/workspace metadata"
            )
        if strict_isolation_enabled():
            from .execution_scope import validate_execution

            owner_parts = next(candidate for candidate in candidates if tuple(relative[:len(candidate)]) == candidate)
            validate_execution(workspace_root.joinpath(*owner_parts), host_workspace, metadata)
