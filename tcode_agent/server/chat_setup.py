"""Model selection and agent-mode transition preparation.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from ..agent.agents import BuiltinAgents
from ..config.manager import ConfigManager
from ..mcp.manager_auto import MCPManager
from ..session.manager import SessionManager
from ..skill.manager import SkillManager
from ..team.catalog import TeamCatalog
from .config_files import (
    _load_tcode_config,
    _save_tcode_config,
)
from .state import ServerState


def _persist_model_selection_for_workdir(
    model_ref: str,
    *,
    workdir: str,
    prefer_local: bool,
) -> str:
    normalized = str(model_ref or "").strip()
    parsed = ConfigManager.parse_model_ref(normalized)
    if not parsed:
        raise HTTPException(400, "Invalid model format, expected provider/model")

    provider_id, model_id = parsed
    target_workdir = str(workdir or ServerState.get_workdir()).strip() or ServerState.get_workdir()
    config = _load_tcode_config(target_workdir)
    providers = config.get("provider", {})
    if not isinstance(providers, dict):
        raise HTTPException(400, "No providers configured")

    provider_cfg = providers.get(provider_id)
    if not isinstance(provider_cfg, dict):
        raise HTTPException(404, f"Provider not found: {provider_id}")

    model_map = provider_cfg.get("models", {})
    if not isinstance(model_map, dict) or model_id not in model_map:
        raise HTTPException(404, f"Model not found: {normalized}")

    if prefer_local:
        local_config_path = Path(ConfigManager.get_local_config_path(target_workdir))
        local_config: dict[str, Any] = {}
        if local_config_path.exists():
            try:
                with open(local_config_path, "r", encoding="utf-8") as f:
                    loaded_local = json.load(f)
                if isinstance(loaded_local, dict):
                    local_config = loaded_local
            except Exception:
                local_config = {}
        local_config["model"] = normalized
        _save_tcode_config(local_config, workdir=target_workdir, prefer_local=True)
    else:
        config["model"] = normalized
        _save_tcode_config(config, workdir=target_workdir, prefer_local=False)

    ConfigManager.reload(target_workdir)
    return normalized


def _mode_switch_display_name(agent_name: str) -> str:
    normalized = str(agent_name or "").strip()
    if not normalized:
        return ""
    if normalized == "manager":
        return "团队编排"
    entry = TeamCatalog.get_by_agent_name(normalized, available_only=False)
    if entry is not None:
        return entry.display_name or entry.visible_agent_name or entry.runtime_agent
    agent_info = BuiltinAgents.get(normalized)
    if agent_info is not None:
        options = getattr(agent_info, "options", {}) or {}
        return str(options.get("display_name") or agent_info.name).strip()
    return normalized


def _team_member_switch_summary(agent_name: str) -> str:
    entry = TeamCatalog.get_by_agent_name(agent_name, available_only=False)
    if entry is None:
        return ""

    members: list[str] = []
    for worker in getattr(entry, "workers", ()) or ():
        if not bool(getattr(worker, "enabled", True)):
            continue
        if not bool(getattr(worker, "visible", True)):
            continue
        name = str(getattr(worker, "name", "") or "").strip()
        if not name:
            continue
        role = str(getattr(worker, "role", "") or "").strip()
        if role.lower() == name.lower():
            role = ""
        qualifiers: list[str] = []
        if role:
            qualifiers.append(role)
        if bool(getattr(worker, "resident", False)):
            qualifiers.append("常驻")
        members.append(f"{name}({', '.join(qualifiers)})" if qualifiers else name)

    if not members:
        for name in getattr(entry, "worker_names", ()) or ():
            value = str(name or "").strip()
            if value:
                members.append(value)

    if not members:
        return f"当前团队 key={entry.key}，未配置可见固定成员。"

    preview = ", ".join(members[:12])
    if len(members) > 12:
        preview += f", ...(+{len(members) - 12})"
    return f"当前团队 key={entry.key}，可见固定成员 {len(members)} 个: {preview}。"


def _is_mode_switch_message(message: Any) -> bool:
    return (
        str(getattr(message, "role", "") or "") == "system"
        and str(getattr(message, "content", "") or "").lstrip().startswith("[MODE SWITCH]")
    )


def _without_mode_switch_messages(messages: list[Any]) -> list[Any]:
    return [message for message in messages if not _is_mode_switch_message(message)]


async def _initialize_chat_turn_runtime(
    session: dict[str, Any],
    *,
    fallback_workdir: str,
) -> str:
    workdir = str(session.get("workdir", "") or fallback_workdir).strip()
    print(f"[WS] Initializing MCP for workdir: {workdir}")
    await MCPManager.initialize(workdir)
    print(f"[WS] MCP initialization complete for workdir: {workdir}")
    await SkillManager.load(workdir=workdir)
    print(f"[WS] Skill loading complete for workdir: {workdir}")
    return workdir


async def _record_agent_mode_switch(
    session_id: str,
    *,
    from_agent: str,
    to_agent: str,
    force: bool = False,
    owner_kwargs: Optional[dict[str, str]] = None,
) -> Optional[dict[str, Any]]:
    previous = str(from_agent or "").strip()
    current = str(to_agent or "").strip()
    if not session_id or not current or (previous == current and not force):
        return None

    runtime = ServerState.sessions.get(session_id)
    runtime_messages = list((runtime or {}).get("messages", []) or [])
    owner = dict(owner_kwargs or {})
    persisted = await SessionManager.get(session_id, **owner)
    persisted_messages = list(getattr(persisted, "messages", []) or [])
    base_messages = _without_mode_switch_messages(persisted_messages or runtime_messages)
    switch_metadata = {
        "from": previous,
        "to": current,
        "at": datetime.now().timestamp(),
        "message_count": len(base_messages),
        "display_name": _mode_switch_display_name(current),
        "team_summary": _team_member_switch_summary(current),
    }

    if runtime is not None:
        runtime["agent_name"] = current
        runtime["messages"] = list(base_messages)

    try:
        if persisted is not None:
            metadata = dict(getattr(persisted, "metadata", {}) or {})
            metadata["last_mode_switch"] = dict(switch_metadata)
            await SessionManager.update(
                session_id,
                **owner,
                agent_name=current,
                messages=list(base_messages),
                metadata=metadata,
            )
    except Exception:
        pass

    return switch_metadata
