"""Read and write workspace configuration using ConfigManager path precedence."""

import json
from pathlib import Path
from typing import Any, Optional

from ..config.manager import ConfigManager
from .state import ServerState


def _tcode_config_path_for_workdir(workdir: str, prefer_local: bool = False) -> Path:
    normalized = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    if prefer_local:
        return Path(ConfigManager.get_local_config_path(normalized))

    primary = ConfigManager.get_primary_config_path(normalized)
    if primary:
        return Path(primary)
    return Path(normalized) / "tcode.json"


def _agent_mcp_config_path() -> Optional[Path]:
    primary = Path(
        ConfigManager.get_config_root_file_path(
            "mcp_agent_config.json",
            ServerState.get_workdir(),
            prefer_effective=True,
        )
    )
    if primary.exists():
        return primary

    return None


def _load_tcode_config(workdir: Optional[str] = None) -> dict[str, Any]:
    target_workdir = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    ConfigManager.reload(target_workdir)
    data = ConfigManager.get_config_copy()
    return data if isinstance(data, dict) else {}


def _save_tcode_config(
    config: dict[str, Any],
    workdir: Optional[str] = None,
    prefer_local: bool = False,
):
    path = _tcode_config_path_for_workdir(
        str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir(),
        prefer_local=prefer_local,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _load_agent_mcp_config() -> dict[str, Any]:
    config_path = _agent_mcp_config_path()
    if config_path is None:
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}
