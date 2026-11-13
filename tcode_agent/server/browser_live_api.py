"""Browser live-session WebSocket transport.

Routes retain their original paths and are registered by app.py.
"""

import asyncio
import json
import math
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..browser import (
    browser_live_paths,
    read_browser_live_state,
    safe_browser_session_id,
)
from . import workdirs as workdir_utils
from .workspace_scope import (
    _resolve_user_workdir_path,
    _workspace_context_from_websocket,
)
from .auth import auth_required, current_user_from_websocket
from .state import ServerState

router = APIRouter()

_CONTROL_ACTIONS = {
    "set_control", "click_at", "move", "drag", "scroll", "key_down", "key_up",
    "confirm_pending", "reject_pending",
}


def _write_control_request(paths, payload: dict) -> None:
    request_id = f"ui-{uuid.uuid4().hex}"
    target = paths.requests / f"{request_id}.json"
    temporary = target.with_name(f".{target.name}.tmp")
    paths.requests.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def _write_confirmation_decision(paths, *, confirmation_id: str, approved: bool) -> None:
    target = paths.confirmation
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps({"confirmation_id": confirmation_id, "approved": approved}),
        encoding="utf-8",
    )
    os.replace(temporary, target)


@router.websocket("/ws/browser")
async def websocket_browser_live(websocket: WebSocket):
    """Stream the latest CDP screencast frame for an owned workspace session.

    Frames stay inside the authenticated TCode connection.  The browser
    container's CDP endpoint is never exposed to the client.
    """
    if auth_required():
        user = current_user_from_websocket(websocket)
        if user is None:
            await websocket.close(code=4401)
            return
        if user.tenant_status != "active":
            await websocket.close(code=4423)
            return
        websocket.state.current_user = user

    browser_session_id = str(
        websocket.query_params.get("browser_session_id") or ""
    ).strip()
    workspace_id = str(websocket.query_params.get("workspace_id") or "").strip()
    if (
        not browser_session_id
        or browser_session_id != safe_browser_session_id(browser_session_id)
        or not workspace_id
    ):
        await websocket.close(code=4400)
        return

    try:
        workspace_context = _workspace_context_from_websocket(
            websocket,
            workspace_id=workspace_id,
        )
        workdir_utils.assert_workspace_active(
            ServerState.get_workdir(),
            workspace_context,
        )
        workspace = _resolve_user_workdir_path(workspace_id, workspace_context)
        paths = browser_live_paths(workspace, browser_session_id)
    except Exception:
        await websocket.close(code=4403)
        return
    if not workspace.is_dir() or not paths.root.is_dir():
        await websocket.close(code=4404)
        return

    state = read_browser_live_state(workspace, browser_session_id)
    recorded_workspace_id = str(state.get("workspace_id") or "").strip()
    if recorded_workspace_id and recorded_workspace_id != workspace_context.workspace_id:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    last_state_signature = ""
    last_frame_seq = -1
    last_heartbeat = 0.0
    try:
        configured_max_frame_bytes = int(
            os.environ.get("TCODE_BROWSER_LIVE_MAX_FRAME_BYTES", 4 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        configured_max_frame_bytes = 4 * 1024 * 1024
    max_frame_bytes = max(
        64 * 1024,
        min(configured_max_frame_bytes, 16 * 1024 * 1024),
    )
    terminal_state_seen_at: float | None = None
    try:
        while True:
            state = await asyncio.to_thread(
                read_browser_live_state,
                workspace,
                browser_session_id,
            )
            state_payload = {
                "type": "browser_state",
                "browser_session_id": browser_session_id,
                "status": str(state.get("status") or "waiting"),
                "action": str(state.get("action") or ""),
                "url": str(state.get("url") or ""),
                "title": str(state.get("title") or ""),
                "frame_seq": int(state.get("frame_seq") or 0),
                "updated_at": str(state.get("updated_at") or ""),
                "error": str(state.get("error") or ""),
                "control_mode": str(state.get("control_mode") or "agent"),
                "viewport": dict(state.get("viewport") or {}),
                "pending_confirmation": state.get("pending_confirmation"),
                "desktop_browser": state.get("desktop_browser") is True,
            }
            signature = json.dumps(state_payload, sort_keys=True, ensure_ascii=False)
            if signature != last_state_signature:
                await websocket.send_json(state_payload)
                last_state_signature = signature

            frame_seq = int(state.get("frame_seq") or 0)
            if frame_seq > last_frame_seq and paths.frame.is_file():
                frame = await asyncio.to_thread(paths.frame.read_bytes)
                if 0 < len(frame) <= max_frame_bytes:
                    await websocket.send_bytes(frame)
                    last_frame_seq = frame_seq

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= 15:
                await websocket.send_json(
                    {
                        "type": "browser_heartbeat",
                        "browser_session_id": browser_session_id,
                    }
                )
                last_heartbeat = now
            if state_payload["status"] in {
                "closed",
                "idle_timeout",
                "stopped",
            }:
                terminal_state_seen_at = terminal_state_seen_at or now
                if now - terminal_state_seen_at >= 1.0:
                    await websocket.close(code=1000)
                    return
            else:
                terminal_state_seen_at = None
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            action = str(message.get("action") or "").strip()
            if state.get("desktop_browser") is True:
                await websocket.send_json({"type": "browser_control_error", "error": "请在 Tcode 浏览器窗口中操作；此面板展示每次动作后的页面观察。"})
                continue
            if action not in _CONTROL_ACTIONS:
                await websocket.send_json({"type": "browser_control_error", "error": "unsupported action"})
                continue
            if action in {"confirm_pending", "reject_pending"}:
                pending = state.get("pending_confirmation")
                confirmation_id = str(
                    pending.get("confirmation_id") if isinstance(pending, dict) else ""
                ).strip()
                if not confirmation_id:
                    await websocket.send_json({"type": "browser_control_error", "error": "no pending confirmation"})
                    continue
                await asyncio.to_thread(
                    _write_confirmation_decision,
                    paths,
                    confirmation_id=confirmation_id,
                    approved=action == "confirm_pending",
                )
                await websocket.send_json({"type": "browser_control_accepted", "action": action})
                continue
            payload = {
                key: message.get(key)
                for key in ("action", "mode", "x", "y", "end_x", "end_y", "delta_x", "delta_y", "key")
                if message.get(key) is not None
            }
            for key in ("x", "y", "end_x", "end_y", "delta_x", "delta_y"):
                if key in payload and (
                    not isinstance(payload[key], (int, float))
                    or isinstance(payload[key], bool)
                    or not math.isfinite(payload[key])
                    or abs(payload[key]) > 100_000
                ):
                    await websocket.send_json({"type": "browser_control_error", "error": f"invalid {key}"})
                    payload = {}
                    break
            if not payload:
                continue
            if "key" in payload and len(str(payload["key"])) > 64:
                await websocket.send_json({"type": "browser_control_error", "error": "invalid key"})
                continue
            payload["origin"] = "human"
            await asyncio.to_thread(_write_control_request, paths, payload)
            await websocket.send_json({"type": "browser_control_accepted", "action": action})
    except (WebSocketDisconnect, RuntimeError):
        return
