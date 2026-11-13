from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ...config.manager import ConfigManager
from .base import (
    SandboxConfig,
    SandboxMount,
    SandboxSpec,
    WorkspacePathMap,
    apply_sandbox_config_overrides,
    load_sandbox_config,
    root_runtime_base_profiles,
    root_runtime_profile_name,
    root_runtime_user_allowed,
)
from .pathmap import assert_inside_workspace, container_to_host_path, host_to_container_path

if TYPE_CHECKING:
    from ...tools.base import ToolContext


def _context_workdir(context: Optional[ToolContext]) -> str:
    if context is not None and isinstance(getattr(context, "extra", None), dict):
        value = str(context.extra.get("workdir", "") or "").strip()
        if value:
            return value
    return str(os.environ.get("TCODE_WORKDIR") or os.getcwd())


def _workspace_id_for(host_workspace: Path) -> str:
    return host_workspace.name or "workspace"


def _context_agent_name(context: Optional[ToolContext]) -> str:
    if context is None:
        return ""
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        explicit = str(extra.get("agent_name", "") or "").strip()
        if explicit:
            return explicit
    return str(getattr(context, "agent", "") or "").strip()


def _context_user_id(context: Optional[ToolContext]) -> str:
    extra = getattr(context, "extra", None) if context is not None else None
    return str(extra.get("user_id") or "").strip() if isinstance(extra, dict) else ""


def _route_root_runtime_profile(
    profile_name: str,
    *,
    context: Optional[ToolContext],
    tool_name: str,
) -> tuple[str, bool]:
    normalized = str(profile_name or "").strip()
    root_profile = root_runtime_profile_name()
    allowed = root_runtime_user_allowed(_context_user_id(context))
    if normalized == root_profile:
        if not allowed:
            raise ValueError("authenticated user is not allowed to use root runtime")
        return root_profile, False
    if (
        allowed
        and str(tool_name or "").strip() in {"bash", "exec"}
        and normalized in root_runtime_base_profiles()
    ):
        return root_profile, True
    return normalized, False


def _resolve_profile_name(
    context: Optional[ToolContext],
    *,
    workdir: str,
    tool_name: str,
    requested_profile: Optional[str] = None,
) -> tuple[str, str]:
    explicit = str(requested_profile or "").strip()
    if explicit:
        return explicit, "tool"

    tool_binding = ConfigManager.get_sandbox_tool_profile(tool_name, workdir)
    if tool_binding and tool_binding.get("mode") == "fixed":
        return str(tool_binding["profile"]), "tool-fixed"

    extra = getattr(context, "extra", None) if context is not None else None
    if isinstance(extra, dict):
        hinted = str(extra.get("sandbox_profile", "") or "").strip()
        if hinted:
            return hinted, "context"

    agent_name = _context_agent_name(context)
    if agent_name:
        profile = ConfigManager.get_agent_sandbox_profile(agent_name, workdir)
        if profile:
            return profile, "agent"

    if tool_binding and tool_binding.get("mode") == "default":
        return str(tool_binding["profile"]), "tool-default"

    default_profile = str(os.environ.get("TCODE_SANDBOX_DEFAULT_PROFILE", "") or "").strip()
    if default_profile:
        return default_profile, "env"

    return "", ""


def resolve_sandbox_config(
    *,
    context: Optional[ToolContext],
    tool_name: str,
    requested_profile: Optional[str] = None,
) -> tuple[SandboxConfig, dict[str, Any]]:
    workdir = _context_workdir(context)
    cfg = load_sandbox_config()
    metadata: dict[str, Any] = {
        "sandbox_tool": str(tool_name or "").strip(),
    }

    ConfigManager.load(workdir)
    profile_name, profile_source = _resolve_profile_name(
        context,
        workdir=workdir,
        tool_name=tool_name,
        requested_profile=requested_profile,
    )
    original_profile_name = profile_name
    profile_name, root_runtime_routed = _route_root_runtime_profile(
        profile_name,
        context=context,
        tool_name=tool_name,
    )
    agent_name = _context_agent_name(context)
    if agent_name:
        metadata["sandbox_agent"] = agent_name

    if profile_name:
        metadata["sandbox_profile_requested"] = profile_name
        metadata["sandbox_profile_source"] = profile_source
    if root_runtime_routed or profile_name == root_runtime_profile_name():
        metadata["sandbox_root_runtime"] = True
        metadata["runtime_privilege"] = "root"
        metadata["sandbox_profile_original"] = original_profile_name

    agent_sandbox = (
        ConfigManager.get_agent_sandbox_config(agent_name, workdir)
        if agent_name
        else {}
    )
    isolated_tool_profile = profile_source in {"tool", "tool-fixed"}
    agent_overrides = (
        {key: value for key, value in agent_sandbox.items() if key not in {"profile", "name"}}
        if isinstance(agent_sandbox, dict) and not isolated_tool_profile
        else {}
    )

    agent_profile_name = ""
    if not requested_profile and profile_source in {"agent", "context"}:
        agent_profile_name = profile_name
    elif not requested_profile and not profile_source:
        agent_profile_name = ""
    elif requested_profile:
        agent_profile_name = ""
    else:
        agent_profile_name = ""

    if agent_profile_name:
        agent_profile = ConfigManager.get_sandbox_profile(agent_profile_name, workdir)
        if isinstance(agent_profile, dict):
            cfg = apply_sandbox_config_overrides(
                cfg,
                agent_profile,
                profile_name=agent_profile_name,
            )
            metadata["sandbox_agent_profile"] = agent_profile_name

    if agent_overrides:
        cfg = apply_sandbox_config_overrides(
            cfg,
            agent_overrides,
            profile_name=cfg.profile_name,
        )
        metadata["sandbox_agent_overrides"] = True

    if profile_name and profile_name != agent_profile_name:
        profile = ConfigManager.get_sandbox_profile(profile_name, workdir)
        if isinstance(profile, dict):
            cfg = apply_sandbox_config_overrides(
                cfg,
                profile,
                profile_name=profile_name,
            )
            metadata["sandbox_profile_found"] = True
        else:
            metadata["sandbox_profile_found"] = False
    elif profile_name and profile_name == agent_profile_name:
        metadata["sandbox_profile_found"] = True

    if cfg.profile_name:
        metadata["sandbox_profile"] = cfg.profile_name

    return cfg, metadata


def _build_pathmap(context: Optional[ToolContext], cfg: SandboxConfig) -> WorkspacePathMap:
    host_workspace = Path(_context_workdir(context)).expanduser().resolve(strict=False)
    if cfg.workspace_root is not None:
        assert_inside_workspace(host_workspace, cfg.workspace_root)
    return WorkspacePathMap(
        workspace_id=_workspace_id_for(host_workspace),
        host_workspace=host_workspace,
        container_workspace=cfg.container_workspace,
    )


def _resolve_cwd(requested_workdir: Optional[str], pathmap: WorkspacePathMap) -> tuple[Path, str]:
    raw = str(requested_workdir or "").strip()
    if not raw:
        host_cwd = pathmap.host_workspace
    elif raw == pathmap.container_workspace or raw.startswith(f"{pathmap.container_workspace}/"):
        host_cwd = container_to_host_path(raw, pathmap)
    else:
        candidate = Path(raw)
        if candidate.is_absolute():
            host_cwd = candidate.expanduser().resolve(strict=False)
        else:
            host_cwd = (pathmap.host_workspace / raw).resolve(strict=False)
    host_cwd = assert_inside_workspace(host_cwd, pathmap.host_workspace)
    return host_cwd, host_to_container_path(host_cwd, pathmap)


def _network_for_tool(tool_name: str, cfg: SandboxConfig) -> str:
    # Workspace file operations never need network access.  Keep them usable
    # when the desktop shares an egress-enabled Web sandbox configuration but
    # intentionally has no outbound proxy.
    if str(tool_name or "").strip().lower() in {"read", "write", "edit", "glob", "grep"}:
        return "none"
    allowed = {
        item.strip()
        for item in str(os.environ.get("TCODE_SANDBOX_NETWORK_ALLOWED_TOOLS", "") or "").split(",")
        if item.strip()
    }
    network = (
        str(os.environ.get("TCODE_SANDBOX_ALLOWED_NETWORK", "egress") or "egress")
        if tool_name in allowed
        else cfg.network
    )
    if (
        os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
        and tool_name in {"bash", "exec"}
        and str(network or "").strip().lower() == "egress"
        and not str(cfg.egress_proxy or "").strip()
    ):
        return "none"
    return network


def _network_env(network: str, cfg: SandboxConfig) -> dict[str, str]:
    if str(network or "").strip().lower() != "egress":
        return {}
    proxy = str(cfg.egress_proxy or "").strip()
    if not proxy:
        raise ValueError(
            "Sandbox egress requires TCODE_SANDBOX_EGRESS_PROXY; direct bridge access is not allowed."
        )
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "all_proxy": proxy,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _external_mounts_from_context(
    context: Optional[ToolContext],
    pathmap: WorkspacePathMap,
) -> list[SandboxMount]:
    extra = getattr(context, "extra", None) if context is not None else None
    resource_context = extra.get("resource_context") if isinstance(extra, dict) else None
    if not isinstance(resource_context, dict):
        return []

    external_mounts = resource_context.get("external_mounts")
    if not isinstance(external_mounts, list):
        return []

    external_root = f"{pathmap.container_workspace.rstrip('/')}/.external/"
    workspace_marker = resource_context.get("_workspace")
    protein_inputs_root: Optional[Path] = None
    if isinstance(workspace_marker, dict) and str(
        workspace_marker.get("strategy") or ""
    ).strip() == "protein_run":
        try:
            declared_run_root = Path(
                str(workspace_marker.get("run_root") or "")
            ).expanduser().resolve(strict=True)
            declared_inputs_root = Path(
                str(workspace_marker.get("inputs_dir") or "")
            ).expanduser().resolve(strict=True)
            expected_inputs_root = (
                pathmap.host_workspace.resolve(strict=True) / "inputs"
            ).resolve(strict=True)
            if (
                declared_run_root == pathmap.host_workspace.resolve(strict=True)
                and declared_inputs_root == expected_inputs_root
            ):
                protein_inputs_root = declared_inputs_root
        except (OSError, RuntimeError, ValueError):
            protein_inputs_root = None

    deduped: dict[str, SandboxMount] = {}
    for item in external_mounts:
        if not isinstance(item, dict):
            continue
        raw_source = str(item.get("source_path") or "").strip()
        raw_target = str(item.get("container_path") or "").strip()
        if not raw_source or not raw_target:
            continue
        target_allowed = raw_target.startswith(external_root)
        if not target_allowed and protein_inputs_root is not None:
            try:
                mount_target = Path(
                    str(item.get("mount_target_path") or "")
                ).expanduser().resolve(strict=True)
                relative_target = mount_target.relative_to(
                    pathmap.host_workspace.resolve(strict=True)
                ).as_posix()
                expected_container_target = (
                    f"{pathmap.container_workspace.rstrip('/')}/{relative_target}"
                )
                target_allowed = (
                    item.get("workspace_input") is True
                    and item.get("read_only") is True
                    and mount_target.is_relative_to(protein_inputs_root)
                    and (
                        mount_target != protein_inputs_root
                        or item.get("workspace_input_root") is True
                    )
                    and raw_target == expected_container_target
                )
            except (OSError, RuntimeError, ValueError):
                target_allowed = False
        if not target_allowed:
            continue
        try:
            source = Path(raw_source).expanduser().resolve(strict=True)
        except Exception:
            continue
        if not source.is_file() and not source.is_dir():
            continue
        deduped[raw_target] = SandboxMount(
            source=source,
            target=raw_target,
            read_only=bool(item.get("read_only", True)),
        )
    return list(deduped.values())


def _merge_mounts(
    dynamic_mounts: list[SandboxMount],
    static_mounts: tuple[SandboxMount, ...],
) -> list[SandboxMount]:
    merged = {mount.target: mount for mount in dynamic_mounts}
    for mount in static_mounts:
        merged[mount.target] = mount
    return list(merged.values())


def _resource_context_from_context(context: Optional[ToolContext]) -> dict[str, Any]:
    extra = getattr(context, "extra", None) if context is not None else None
    resource_context = extra.get("resource_context") if isinstance(extra, dict) else None
    return dict(resource_context or {}) if isinstance(resource_context, dict) else {}


def _replace_path_prefix(command: str, source: str, target: str) -> tuple[str, bool]:
    src = str(source or "").strip().rstrip("/")
    dst = str(target or "").strip().rstrip("/")
    if not src or not dst or src == dst:
        return command, False
    pattern = re.compile(re.escape(src) + r"(?=$|[/\s'\"=:;,&|)<>\]])")
    rewritten = pattern.sub(dst, command)
    return rewritten, rewritten != command


def _host_path_variants(path: str) -> tuple[str, ...]:
    text = str(path or "").strip().rstrip("/")
    if not text:
        return ()
    variants = {text}
    try:
        variants.add(Path(text).expanduser().resolve(strict=False).as_posix().rstrip("/"))
    except Exception:
        pass
    for item in list(variants):
        if item.startswith("/private/var/"):
            variants.add(item[len("/private") :])
        elif item.startswith("/var/"):
            variants.add(f"/private{item}")
    return tuple(sorted(variants, key=len, reverse=True))


def _sandbox_host_python_prefixes() -> tuple[str, ...]:
    raw = str(os.environ.get("TCODE_SANDBOX_HOST_PATH_PREFIXES", "/home/,/Users/") or "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _rewrite_host_python_for_sandbox(command: str) -> tuple[str, bool]:
    raw = str(os.environ.get("TCODE_SANDBOX_REWRITE_HOST_PYTHON", "true") or "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return command, False

    prefixes = _sandbox_host_python_prefixes()
    sandbox_python = str(os.environ.get("TCODE_SANDBOX_PYTHON_BIN", "python") or "python").strip() or "python"
    pattern = re.compile(r"(?<!\S)(/[^\s'\";&|]+/bin/python(?:3(?:\.\d+)?)?)(?=\s|$)")

    changed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal changed
        path = match.group(1)
        if path.startswith("/workspace/"):
            return path
        if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
            return path
        changed = True
        return sandbox_python

    return pattern.sub(_replace, command), changed


def _rewrite_command_for_sandbox(
    command: str,
    *,
    context: Optional[ToolContext],
    pathmap: WorkspacePathMap,
) -> tuple[str, dict[str, Any]]:
    original = str(command or "")
    rewritten = original
    changed = False

    for variant in _host_path_variants(pathmap.host_workspace.as_posix()):
        rewritten, path_changed = _replace_path_prefix(
            rewritten,
            variant,
            pathmap.container_workspace,
        )
        changed = changed or path_changed

    resource_context = _resource_context_from_context(context)
    for item in list(resource_context.get("external_mounts") or []):
        if not isinstance(item, dict):
            continue
        container_path = str(item.get("container_path") or "").strip()
        for key in ("source_path", "mount_target_path"):
            host_path = str(item.get(key) or "").strip()
            for variant in _host_path_variants(host_path):
                rewritten, path_changed = _replace_path_prefix(
                    rewritten,
                    variant,
                    container_path,
                )
                changed = changed or path_changed

    rewritten, python_changed = _rewrite_host_python_for_sandbox(rewritten)
    changed = changed or python_changed

    if not changed:
        return original, {}
    return rewritten, {
        "sandbox_command_rewritten": True,
        "sandbox_original_command": original,
    }


def build_tool_sandbox_spec(
    *,
    tool_name: str,
    command: str,
    requested_workdir: Optional[str],
    timeout_seconds: Optional[int],
    context: Optional[ToolContext],
    expected_artifacts: Optional[list[str]] = None,
    extra_env: Optional[dict[str, str]] = None,
    image: Optional[str] = None,
    sandbox_profile: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> SandboxSpec:
    cfg, profile_metadata = resolve_sandbox_config(
        context=context,
        tool_name=tool_name,
        requested_profile=sandbox_profile,
    )
    pathmap = _build_pathmap(context, cfg)
    host_cwd, container_cwd = _resolve_cwd(requested_workdir, pathmap)
    timeout = max(1, min(24 * 3600, int(timeout_seconds or cfg.timeout_seconds)))
    mounts = _external_mounts_from_context(context, pathmap)
    mounts = _merge_mounts(mounts, cfg.static_mounts)
    network = _network_for_tool(tool_name, cfg)
    sandbox_command, command_metadata = _rewrite_command_for_sandbox(
        command,
        context=context,
        pathmap=pathmap,
    )
    return SandboxSpec(
        tool_name=tool_name,
        command=sandbox_command,
        pathmap=pathmap,
        host_cwd=host_cwd,
        container_cwd=container_cwd,
        timeout_seconds=timeout,
        network=network,
        env={
            "TALL_WORKSPACE": pathmap.container_workspace,
            **{str(k): str(v) for k, v in dict(extra_env or {}).items()},
            # Desktop native jobs use their own clean environment, not Docker
            # profile cache/PATH settings inherited from the shared Web config.
            **({} if os.environ.get("TCODE_DESKTOP_MANAGED") == "1" else dict(cfg.extra_env or {})),
            **_network_env(network, cfg),
        },
        image=str(image or cfg.image or "").strip(),
        memory=cfg.memory,
        cpus=cfg.cpus,
        pids_limit=cfg.pids_limit,
        read_only=cfg.read_only,
        user=cfg.user,
        home=cfg.home,
        tmpfs=cfg.tmpfs,
        shm_size=cfg.shm_size,
        mounts=mounts,
        expected_artifacts=list(expected_artifacts or []),
        metadata={
            **dict(metadata or {}),
            **profile_metadata,
            **(
                {
                    "tenant_id": str((context.extra or {}).get("tenant_id") or ""),
                    "user_id": str((context.extra or {}).get("user_id") or ""),
                    "workspace_id": str((context.extra or {}).get("workspace_id") or ""),
                    "session_id": str(getattr(context, "session_id", "") or ""),
                    "execution_id": str(
                        (context.extra or {}).get("execution_id")
                        or ((context.extra or {}).get("agent_workspace") or {}).get("task_id")
                        or getattr(context, "session_id", "")
                        or ""
                    ),
                }
                if context is not None and isinstance(getattr(context, "extra", None), dict)
                else {}
            ),
            **({"external_mount_count": len(mounts)} if mounts else {}),
            **command_metadata,
        },
        lifecycle=str(cfg.lifecycle or "ephemeral").strip().lower(),
        idle_stop_seconds=max(0, int(cfg.idle_stop_seconds or 0)),
        retention_seconds=max(0, int(cfg.retention_seconds or 0)),
    )
