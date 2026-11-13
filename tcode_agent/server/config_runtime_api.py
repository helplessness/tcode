"""Owner 约束的 Effective Config 查询与显式 Reload API。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..config.effective import (
    EffectiveConfigResolver,
    effective_config_contract_document,
)
from ..config.task_snapshot import TaskConfigSnapshotStore
from ..runtime.command_policy import replay_command_policy
from ..session.manager import SessionManager
from .auth import require_current_user
from .config_files import _save_tcode_config
from ..config.manager import ConfigManager


router = APIRouter(prefix="/api/config", tags=["effective-config"])


def _learning_enabled(workdir: str) -> bool:
    ConfigManager.reload(workdir)
    learning = ConfigManager.get_config_copy().get("learning", {})
    configured = bool(learning.get("enabled", True)) if isinstance(learning, dict) else True
    override = os.environ.get("TCODE_LEARNING_ENABLED")
    return configured if override is None else override.strip().lower() in {"1", "true", "yes", "on"}


async def _owned_workdir(
    request: Request,
    *,
    session_id: str,
    workspace_id: str,
) -> str:
    """只允许认证用户解析自己 Session 所绑定的工作目录。"""

    user = require_current_user(request)
    session = await SessionManager.get(
        str(session_id or "").strip(),
        tenant_id=str(user.tenant_id or ""),
        user_id=str(user.user_id or ""),
        workspace_id=str(workspace_id or "").strip(),
    )
    if session is None:
        raise HTTPException(404, "session not found")
    return str(session.workdir or "").strip()


async def _owned_session(
    request: Request,
    *,
    session_id: str,
    workspace_id: str,
) -> Any:
    user = require_current_user(request)
    session = await SessionManager.get(
        str(session_id or "").strip(),
        tenant_id=str(user.tenant_id or ""),
        user_id=str(user.user_id or ""),
        workspace_id=str(workspace_id or "").strip(),
    )
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@router.get("/contract")
async def get_effective_config_contract() -> dict[str, Any]:
    """返回不包含配置值的 Effective Config v1 合同。"""

    return {"success": True, "contract": effective_config_contract_document()}


@router.get("/effective")
async def get_effective_config(
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """返回 Owner 可见工作区的脱敏配置、来源与 Reloadability。"""

    workdir = await _owned_workdir(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    snapshot = EffectiveConfigResolver.resolve(workdir)
    return {"success": True, "config": snapshot.to_dict()}


@router.post("/reload")
async def reload_effective_config(
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """显式重载 Owner 可见工作区的 JSON 配置，并返回脱敏差异。"""

    workdir = await _owned_workdir(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    return {"success": True, **EffectiveConfigResolver.reload(workdir)}


@router.get("/learning")
async def get_learning_setting(
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """返回用户可理解的本地学习总开关，不暴露底层调参。"""

    workdir = await _owned_workdir(request, session_id=session_id, workspace_id=workspace_id)
    return {
        "success": True,
        "enabled": _learning_enabled(workdir),
        "editable": os.environ.get("TCODE_LEARNING_ENABLED") is None,
    }


@router.put("/learning")
async def update_learning_setting(
    request: Request,
    payload: dict[str, Any],
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """在工作区本地配置中保存学习总开关并热重载。"""

    if os.environ.get("TCODE_LEARNING_ENABLED") is not None:
        raise HTTPException(409, "本地经验学习由环境变量管理，无法在界面中修改")
    if not isinstance(payload.get("enabled"), bool):
        raise HTTPException(400, "enabled must be a boolean")

    workdir = await _owned_workdir(request, session_id=session_id, workspace_id=workspace_id)
    local_path = Path(ConfigManager.get_local_config_path(workdir))
    local_config: dict[str, Any] = {}
    if local_path.exists():
        try:
            loaded = json.loads(local_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                local_config = loaded
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"工作区配置无法读取: {exc}") from exc

    learning = local_config.get("learning", {})
    learning = dict(learning) if isinstance(learning, dict) else {}
    learning["enabled"] = payload["enabled"]
    local_config["learning"] = learning
    _save_tcode_config(local_config, workdir=workdir, prefer_local=True)
    ConfigManager.reload(workdir)
    return {"success": True, "enabled": _learning_enabled(workdir), "editable": True}


@router.get("/snapshot")
async def get_task_config_snapshot(
    request: Request,
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """Return the immutable, redacted configuration bound to one owned task."""

    session = await _owned_session(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    metadata = dict(getattr(session, "metadata", {}) or {})
    snapshot_id = str(metadata.get("config_snapshot_id") or "").strip()
    if not snapshot_id:
        raise HTTPException(404, "config snapshot not found")
    try:
        snapshot = TaskConfigSnapshotStore.get(
            snapshot_id,
            str(metadata.get("config_snapshot_workdir") or session.workdir or ""),
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "config snapshot not found")
    return {"success": True, "snapshot": snapshot.to_dict()}


@router.post("/snapshot/replay")
async def replay_task_config_snapshot(
    request: Request,
    payload: dict[str, Any],
    session_id: str = Query(min_length=1),
    workspace_id: str = Query(min_length=1),
) -> dict[str, Any]:
    """Replay command decisions under the task snapshot; commands never execute."""

    session = await _owned_session(
        request,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    metadata = dict(getattr(session, "metadata", {}) or {})
    snapshot_id = str(metadata.get("config_snapshot_id") or "").strip()
    if not snapshot_id:
        raise HTTPException(404, "config snapshot not found")
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list):
        raise HTTPException(400, "cases must be a list")
    try:
        snapshot = TaskConfigSnapshotStore.get(
            snapshot_id,
            str(metadata.get("config_snapshot_workdir") or session.workdir or ""),
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "config snapshot not found")
    return {
        "success": True,
        "snapshot_id": snapshot.snapshot_id,
        "executed": False,
        "results": replay_command_policy(snapshot, cases),
    }


__all__ = ["router"]
