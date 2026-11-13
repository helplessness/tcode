"""Authenticated CRUD for remote HTTP MCP server configuration."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..mcp.manager_auto import MCPManager
from ..runtime.workspace_registry import registry_workspace
from .auth import auth_required, require_current_user
from .desktop_config import local_development_services, remote_url
from .state import ServerState

router = APIRouter(prefix="/api/mcp/config", tags=["mcp-config"])
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_edit_lock = asyncio.Lock()


def _config_path() -> Path:
    desktop = os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
    raw = os.environ.get("TCODE_DESKTOP_MCP_CONFIG") if desktop else MCPManager.resolve_mcp_config_path(ServerState.get_workdir())
    if not raw:
        raise HTTPException(409, "MCP 配置文件未初始化")
    return Path(raw).expanduser()


def _read() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "MCP 配置文件无法读取") from exc
    if not isinstance(value, dict):
        raise HTTPException(409, "MCP 配置必须是对象")
    return value


def _write(config: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    # Desktop runs from a filtered effective file. Keep it in sync without
    # copying desktop-only metadata into the MCP manager.
    effective = os.environ.get("TCODE_MCP_CONFIG")
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1" and effective:
        safe = {name: entry for name, entry in config.items() if not name.startswith("$") and isinstance(entry, dict)}
        target = Path(effective)
        target.parent.mkdir(parents=True, exist_ok=True)
        target_temp = target.with_suffix(target.suffix + ".tmp")
        target_temp.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(target_temp, 0o600)
        target_temp.replace(target)


def _require_admin(request: Request) -> None:
    user = require_current_user(request)
    if auth_required() and str(user.role or "").lower() not in {"owner", "admin"}:
        raise HTTPException(403, "需要管理员权限")


def _web_managed_server(name: str) -> bool:
    return os.environ.get("TCODE_DESKTOP_MANAGED") != "1" and name.strip().lower() == "job-protocol-mcp"


def _public(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    web_managed = _web_managed_server(name)
    return {
        "name": name,
        "url": "" if web_managed else str(entry.get("url") or ""),
        "transport": str(entry.get("transport") or "streamable-http"),
        "enabled": entry.get("enabled", True) is not False,
        "invocation_mode": str(entry.get("invocation_mode") or "auto"),
        "submit_via": str(entry.get("submit_via") or "job-protocol-mcp"),
        "connect_timeout": int(entry.get("connect_timeout", 10000) or 10000),
        "timeout": int(entry.get("timeout", 30000) or 30000),
        "header_names": sorted(str(key) for key in headers),
        "dynamic_headers": entry.get('dynamic_headers', {}),
        "editable": not web_managed,
        "managed_message": "Web 部署的 Job Protocol 服务由服务器管理员配置" if web_managed else "",
        "file_transfer": entry.get("file_transfer"),
    }


async def _reload(workdir=None) -> None:
    workdir = workdir or ServerState.get_workdir()
    source = Path(MCPManager.resolve_mcp_config_path(workdir)).resolve()
    # Configuration is shared, while MCP clients are workspace-owned. Refresh
    # every initialized consumer of this file, preserving plugin-owned clients.
    with MCPManager._workspace_state_lock:
        active = [key for key, state in MCPManager._workspace_states.items()
                  if state.get("_initialized")]
    for target in dict.fromkeys([str(workdir), *active]):
        if Path(MCPManager.resolve_mcp_config_path(target or workdir)).resolve() != source:
            continue
        with registry_workspace(target):
            await MCPManager.reload_config(target or workdir)


@router.get("")
async def list_mcp_config(request: Request):
    user = require_current_user(request)
    config = _read()
    return {
        "servers": [await _public_scoped(name, entry, request) for name, entry in sorted(config.items()) if not name.startswith("$") and isinstance(entry, dict)],
        "can_edit": not auth_required() or str(user.role or "").lower() in {"owner", "admin"},
        "config_file": str(_config_path()),
    }


async def _transfer_scope(request):
    from .workspace_scope import _workspace_context_from_request
    from .catalog_api import _resolve_agent_catalog_workdir
    scope = _workspace_context_from_request(request)
    workdir = await _resolve_agent_catalog_workdir(request, workdir_name=request.query_params.get('workdir_name', ''))
    return workdir, f"{scope.tenant_id}/{scope.user_id}"


async def _public_scoped(name, entry, request):
    public = _public(name, entry)
    settings = entry.get('file_transfer')
    if settings:
        from ..mcp.custom_targets import fingerprint, ORIGIN
        from ..plugins.async_targets import binding
        workdir, owner = await _transfer_scope(request)
        state = binding(workdir, owner, ORIGIN, name)
        if settings.get('receiver') == 'server_paths':
            status = 'adapter_required'
        elif not entry.get('enabled', True):
            status = 'disabled'
        else:
            try:
                valid = state.get('active') and state.get('fingerprint') == fingerprint(entry, state.get('declaration'))
            except ValueError:
                valid = False
            status = 'registered' if valid else 'activation_required'
        public['file_transfer_status'] = status
    return public


@router.post("/discover-files")
async def discover_mcp_files(request: Request):
    _require_admin(request)
    payload = await request.json()
    url = str(payload.get("url") or "").strip()
    if not remote_url(url, allow_loopback=local_development_services()):
        raise HTTPException(400, "地址必须是有效的 HTTP(S) MCP URL")
    old = _read().get(str(payload.get("name") or ""), {})
    headers = dict(old.get("headers") or {})
    supplied = payload.get("headers") or {}
    if not isinstance(supplied, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in supplied.items()):
        raise HTTPException(400, "headers 必须是字符串键值对象")
    headers.update(supplied)
    from ..mcp.custom_targets import discover
    try:
        return {"tools": await discover({"url": url, "headers": headers, "transport": payload.get("transport")})}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "无法发现工具，请检查地址、认证和连接；未提交计算") from exc


@router.put("/{name}")
async def put_mcp_config(name: str, request: Request):
    # Discovery/registration now await network I/O between reading and writing.
    # Serialize edits so another save/delete cannot be lost in that interval.
    async with _edit_lock:
        return await _put_mcp_config(name, request)


async def _put_mcp_config(name: str, request: Request):
    _require_admin(request)
    if _web_managed_server(name):
        raise HTTPException(403, "Web 端的 job-protocol-mcp 由平台管理，不能配置")
    if not _NAME.fullmatch(name) or name.startswith("$"):
        raise HTTPException(400, "名称只能包含字母、数字、点、横线和下划线")
    payload = await request.json()
    url = str(payload.get("url") or "").strip()
    transport = str(payload.get("transport") or "streamable-http")
    if not remote_url(url, allow_loopback=local_development_services()):
        raise HTTPException(400, "地址必须是有效的 HTTP(S) MCP URL")
    if transport not in {"streamable-http", "sse"}:
        raise HTTPException(400, "传输方式仅支持 streamable-http 或 sse")
    invocation_mode = str(payload.get("invocation_mode") or "auto").strip().lower()
    submit_via = str(payload.get("submit_via") or "job-protocol-mcp").strip()
    if invocation_mode not in {"auto", "direct", "async"}:
        raise HTTPException(400, "调用方式仅支持 auto、direct 或 async")
    if invocation_mode == "async" and name.lower() == submit_via.lower():
        raise HTTPException(400, "异步 MCP 不能通过自身提交任务")
    if invocation_mode == "async" and submit_via.lower() != "job-protocol-mcp":
        raise HTTPException(400, "当前异步任务网关仅支持 job-protocol-mcp")
    config = _read()
    old = config.get(name) if isinstance(config.get(name), dict) else {}
    headers = dict(old.get("headers") or {}) if isinstance(old.get("headers"), dict) else {}
    supplied = payload.get("headers")
    if supplied is not None:
        if not isinstance(supplied, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in supplied.items()):
            raise HTTPException(400, "headers 必须是字符串键值对象")
        headers.update({k.strip(): v for k, v in supplied.items() if k.strip() and v})
    for key in payload.get("remove_headers") or []:
        headers.pop(str(key), None)
    from ..mcp.dynamic_headers import validate_rules
    try:
        dynamic_headers = validate_rules(payload.get('dynamic_headers', old.get('dynamic_headers', {})), headers)
        from ..mcp.dynamic_headers import augment_schema
        client = MCPManager._clients.get(name)
        for tool in client.tools if client else []:
            augment_schema(tool.get('inputSchema', {}), dynamic_headers)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    config[name] = {
        "url": url,
        "transport": transport,
        "enabled": bool(payload.get("enabled", True)),
        "invocation_mode": invocation_mode,
        "submit_via": submit_via,
        "connect_timeout": max(1000, int(payload.get("connect_timeout", 10000) or 10000)),
        "timeout": max(1000, int(payload.get("timeout", 30000) or 30000)),
        **({"headers": headers} if headers else {}),
        **({'dynamic_headers': dynamic_headers} if dynamic_headers else {}),
    }
    # Absence preserves older clients' settings; explicit null disables transport.
    settings = payload.get("file_transfer", old.get("file_transfer"))
    if settings is not None:
        config[name]["file_transfer"] = settings
    transfer_workdir = None
    if settings is not None or old.get("file_transfer"):
        from ..mcp.custom_targets import configure
        try:
            transfer_workdir, owner = await _transfer_scope(request)
            await configure(transfer_workdir, owner, name, config[name], old)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "文件传输未启用：无法连接远端 MCP 或本地 Job Protocol；原配置未修改，未提交计算") from exc
    _write(config)
    await _reload()
    if transfer_workdir and str(transfer_workdir) != str(ServerState.get_workdir()):
        await _reload(transfer_workdir)
    return {"saved": True, "server": await _public_scoped(name, config[name], request)}


@router.delete("/{name}")
async def delete_mcp_config(name: str, request: Request):
    async with _edit_lock:
        return await _delete_mcp_config(name, request)


async def _delete_mcp_config(name: str, request: Request):
    _require_admin(request)
    if _web_managed_server(name):
        raise HTTPException(403, "Web 端的 job-protocol-mcp 由平台管理，不能删除")
    config = _read()
    if name not in config or name.startswith("$"):
        raise HTTPException(404, "MCP 服务不存在")
    transfer_workdir = None
    if config[name].get("file_transfer"):
        from ..mcp.custom_targets import configure
        transfer_workdir, owner = await _transfer_scope(request)
        await configure(transfer_workdir, owner, name, {}, config[name])
    del config[name]
    _write(config)
    await _reload()
    if transfer_workdir and str(transfer_workdir) != str(ServerState.get_workdir()):
        await _reload(transfer_workdir)
    return {"deleted": True}


@router.post("/reload")
async def reload_mcp_config(request: Request):
    _require_admin(request)
    async with _edit_lock:
        await _reload()
    return {"reloaded": True}
