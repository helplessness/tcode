"""Resolve MCP configs for background execution-plane tool calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG_ROOT = PROJECT_ROOT / ".tcode-local"
DEFAULT_EXECUTOR_CONFIG = PROJECT_ROOT / "config" / "templates" / "mcp_exec_only.example.json"
DEFAULT_FULL_CONFIG = PROJECT_ROOT / "config" / "templates" / "mcp.example.json"


def broker_servers() -> set[str]:
    raw = os.getenv("TCODE_MCP_BROKER_SERVERS", "job-protocol-mcp")
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }


def config_contains_server(path_value: str | os.PathLike | None, server_name: str) -> bool:
    path_text = str(path_value or "").strip()
    server = str(server_name or "").strip()
    if not path_text or not server:
        return False
    try:
        data = json.loads(Path(path_text).expanduser().read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get(server), dict)


def _split_paths(raw: str) -> list[str]:
    return [
        item.strip()
        for item in str(raw or "").split(",")
        if item.strip()
    ]


def _resolve_candidate_path(
    value: str | os.PathLike,
    *,
    workdir: str | os.PathLike | None = None,
    config_root: str | os.PathLike | None = None,
) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path

    bases: list[Path] = []
    for raw in (config_root, os.getenv("TCODE_CONFIG_ROOT"), workdir, PROJECT_ROOT, Path.cwd()):
        if raw:
            bases.append(Path(raw).expanduser())

    for base in bases:
        candidate = base / path
        if candidate.exists():
            return candidate
    return (PROJECT_ROOT / path).resolve(strict=False)


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = path.resolve(strict=False).as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def executor_config_candidates(
    *,
    workdir: str | os.PathLike | None = None,
    config_root: str | os.PathLike | None = None,
    include_foreground: bool = True,
) -> list[Path]:
    """Return ordered execution-plane MCP config candidates.

    ``TCODE_EXECUTOR_MCP_CONFIGS`` allows multiple config files for future MCP
    partitions. ``TCODE_EXECUTOR_MCP_CONFIG`` is kept for the common single-file
    case. Relative paths are resolved against TCODE_CONFIG_ROOT, workdir,
    project root, then cwd.
    ``include_foreground=False`` excludes only implicit foreground fallbacks;
    explicitly configured executor files remain eligible regardless of name.
    """
    candidates: list[Path] = []

    for value in _split_paths(os.getenv("TCODE_EXECUTOR_MCP_CONFIGS", "")):
        candidates.append(
            _resolve_candidate_path(value, workdir=workdir, config_root=config_root)
        )

    explicit = str(os.getenv("TCODE_EXECUTOR_MCP_CONFIG", "") or "").strip()
    if explicit:
        candidates.append(
            _resolve_candidate_path(explicit, workdir=workdir, config_root=config_root)
        )

    for root in (config_root, os.getenv("TCODE_CONFIG_ROOT"), workdir):
        if root:
            base = Path(root).expanduser()
            candidates.append(base / "mcp_exec_only.json")
            if include_foreground:
                candidates.append(base / "mcp.json")

    if os.getenv("TCODE_DESKTOP_TEST_INSTANCE") == "1":
        return _unique_paths(candidates)

    # 源码仓库的私有配置是显式候选项，但不能改变上面的模块常量；
    # 这样同一构建在开发机和干净环境中的默认值保持一致。
    candidates.append(LOCAL_CONFIG_ROOT / "mcp_exec_only.json")
    if include_foreground:
        candidates.append(LOCAL_CONFIG_ROOT / "mcp.json")
    candidates.append(DEFAULT_EXECUTOR_CONFIG)
    if include_foreground:
        candidates.append(DEFAULT_FULL_CONFIG)
    return _unique_paths(candidates)


def resolve_executor_mcp_config(
    server_name: str,
    *,
    current_config: str | os.PathLike | None = None,
    workdir: str | os.PathLike | None = None,
    config_root: str | os.PathLike | None = None,
) -> Optional[Path]:
    """Find an MCP config that contains ``server_name``.

    Broker servers such as job-protocol-mcp remain on the foreground config.
    Domain MCPs are resolved from execution-plane configs.
    """
    server = str(server_name or "").strip()
    if not server:
        return None

    current = str(current_config or os.getenv("TCODE_MCP_CONFIG", "") or "").strip()
    if server in broker_servers():
        if current:
            return Path(current).expanduser().resolve(strict=False)
        return None

    if config_contains_server(current, server):
        return Path(current).expanduser().resolve(strict=False)

    for candidate in executor_config_candidates(workdir=workdir, config_root=config_root):
        if config_contains_server(candidate, server):
            return candidate.resolve(strict=False)
    return None


def ensure_executor_mcp_env(
    server_name: str,
    *,
    workdir: str | os.PathLike | None = None,
    config_root: str | os.PathLike | None = None,
) -> Optional[Path]:
    """Set TCODE_MCP_CONFIG for a background target MCP call if needed."""
    server = str(server_name or "").strip()
    if not server or server in broker_servers():
        return None

    resolved = resolve_executor_mcp_config(
        server,
        workdir=workdir,
        config_root=config_root,
    )
    if not resolved:
        return None

    os.environ["TCODE_MCP_CONFIG"] = resolved.as_posix()
    os.environ["SYMPHONY_ALLOW_NON_DIRECT_MCP"] = "1"
    return resolved
