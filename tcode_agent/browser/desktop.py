"""Private desktop browser bridge; credentials arrive over startup stdin only."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from datetime import UTC, datetime

from .live import browser_live_paths, safe_browser_session_id, read_browser_live_state

from ..types import ToolResult

_config: dict | None = None


def configure_desktop_browser(payload: dict | None) -> None:
    global _config
    _config = payload
    os.environ["TCODE_DESKTOP_BROWSER_READY"] = "1" if payload else "0"


async def execute_desktop_browser(args: dict, context) -> ToolResult:
    if not _config:
        return ToolResult(title="browser", output="", error="Tcode 内置浏览器未就绪。不会自动切换到本地浏览器。")
    extra = context.extra or {}
    session = str(context.session_id)
    owner = ":".join(str(extra.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id")) + ":" + session
    # A model-supplied session name never changes the authenticated owner.
    args = dict(args, session=safe_browser_session_id(args.get("session") or session))
    workdir = Path(str(extra.get("control_workdir") or extra.get("workdir") or os.environ.get("TCODE_WORKDIR") or os.getcwd())).resolve()
    paths = browser_live_paths(workdir, args["session"])
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(_config["socket_path"], limit=12 * 1024 * 1024), 3)
    try:
        request = {"token": _config["secret"], "owner": owner, "call_id": str(getattr(context, "call_id", "") or uuid.uuid4()), "args": args}
        writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        try:
            data = json.loads(await asyncio.wait_for(reader.readline(), 12 * 60))
        except asyncio.TimeoutError:
            return ToolResult(
                title=f"browser {args['action']}",
                output="",
                error="桌面浏览器响应超时。当前操作已停止；如果正在接管浏览器，请先交还控制后重试。",
                metadata={
                    "browser": True,
                    "desktop_browser": True,
                    "browser_session_id": args["session"],
                    "terminal_tool_error": True,
                    "retryable": True,
                    "should_end_turn": True,
                },
            )
    finally:
        writer.close()
        await writer.wait_closed()
    screenshot = data.pop("screenshot", "")
    image_path = ""
    if screenshot:
        root = Path(str(extra.get("workdir") or workdir)).resolve() / ".tcode" / "browser" / "desktop" / session
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / f"{uuid.uuid4().hex}.jpg"
        target.write_bytes(base64.b64decode(screenshot, validate=True))
        target.chmod(0o600)
        image_path = str(target)
        temp_frame = paths.frame.with_suffix(".tmp")
        temp_frame.write_bytes(target.read_bytes())
        temp_frame.chmod(0o600)
        temp_frame.replace(paths.frame)
    error = str(data.pop("error", ""))
    error_code = error.split(":", 1)[0]
    user_errors = {
        "browser_human_control_active": "你正在接管浏览器。请点击“交还给 Agent”，再让任务继续；本次结果尚未验证。",
        "browser_observation_timeout": "页面读取超时，无法确认本次操作结果。请重新观察页面，不要把先前内容当作已验证结果。",
        "browser_navigation_timeout": "网页加载超时，请检查网络或换个网址重试。",
        "stale_ref": "页面已更新，之前的元素编号已失效。请重新观察，并使用新的元素编号和页面版本；不要原样重试。",
    }
    error = user_errors.get(error_code, error)
    previous = read_browser_live_state(workdir, args["session"])
    if error:
        data.update({"url": previous.get("url", ""), "title": previous.get("title", ""), "page_revision": previous.get("frame_seq", 0),
                     "verified": False, "instruction": "任务未完成。仅报告已经观察到的事实，不得猜测搜索结果顺序或广告属性。"})
    else:
        # Keep titles and page identity before links, within the tool's text budget.
        elements = data.pop("elements", [])
        data["headings"] = [str(title)[:160] for title in data.get("headings", [])][:20]
        data["text"] = str(data.get("text", ""))[:4000]
        data["elements"] = []
        for element in elements:
            data["elements"].append(element)
            if len(json.dumps(data, ensure_ascii=False)) > 10000:
                data["elements"].pop()
                data["elements_truncated"] = True
                break
    if args["action"] == "close":
        paths.frame.unlink(missing_ok=True)
    state = {"desktop_browser": True, "workspace_id": str(extra.get("workspace_id") or ""),
        "agent_session_id": session, "status": "closed" if data.get("closed") else "paused" if error_code == "browser_human_control_active" else "error" if error else "ready",
        "url": data.get("url", ""), "title": data.get("title", ""), "action": args["action"],
        "frame_seq": data.get("page_revision", 0), "control_mode": "human" if error_code == "browser_human_control_active" else "agent", "error": error,
        "updated_at": datetime.now(UTC).isoformat()}
    temp_state = paths.state.with_suffix(".tmp")
    temp_state.write_text(json.dumps(state), encoding="utf-8")
    temp_state.chmod(0o600)
    temp_state.replace(paths.state)
    return ToolResult(title=f"browser {args['action']}", output=json.dumps(data, ensure_ascii=False), error=error or None,
        metadata={"browser": True, "desktop_browser": True, "browser_session_id": args["session"],
            "error_code": error_code, "verification_status": "UNVERIFIED" if error else "",
            "terminal_tool_error": error_code in {"browser_human_control_active", "browser_navigation_timeout", "browser_observation_timeout"},
            "should_end_turn": error_code in {"browser_human_control_active", "browser_navigation_timeout", "browser_observation_timeout"},
            "browser_live": True, "browser_live_frame_seq": state["frame_seq"],
            "page_revision": data.get("page_revision", 0), "url": data.get("url", ""), "title": data.get("title", ""),
            "model_image_path": image_path, "model_image_mime": "image/jpeg" if image_path else "",
            "execution_environment": {"location": "desktop_browser", "label": "Tcode 内置浏览器", "sandboxed": True,
                "isolation": "chromium_renderer", "container_isolation": False}})
