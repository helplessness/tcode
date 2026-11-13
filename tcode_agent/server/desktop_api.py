"""Native resource resolution, installed only by the desktop launcher."""
from __future__ import annotations

import hmac
import asyncio
import os
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .auth import require_current_user
from .state import ServerState
from . import workdirs
from .workspace_scope import _workspace_context_from_request, _resolve_user_workdir_path


async def _probe_artifact_service(url: str, service_id: str = '') -> str:
    from .desktop_config import remote_services_paused
    if remote_services_paused():
        return 'unconfigured'
    if not url:
        return "unconfigured"
    try:
        from ..artifact_transport import _artifact_endpoint
        _, configured_url, headers = _artifact_endpoint(service_id)
        if configured_url != url.rstrip('/'):
            return 'disconnected'
        import httpx
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            async with client.stream('GET', f"{url.rstrip('/')}/health", headers=headers) as response:
                return "connected" if 200 <= response.status_code < 300 else "disconnected"
    except Exception:
        return "disconnected"


def desktop_router(token: str) -> APIRouter:
    router = APIRouter(prefix="/api/desktop", include_in_schema=False)

    @router.post('/model-settings')
    async def model_settings_route(request: Request, payload: dict):
        if not hmac.compare_digest(request.headers.get('x-tcode-desktop-native', ''), token):
            raise HTTPException(403, '模型配置只能通过桌面设置修改')
        from .auth_api import _require_tenant_admin
        _require_tenant_admin(request)
        from .desktop_models import model_settings
        return await model_settings(payload)

    @router.get("/services")
    async def service_status(request: Request, retry: bool = False):
        require_current_user(request)
        from ..mcp.manager_auto import MCPManager
        from ..runtime.workspace_registry import registry_workspace

        state = os.environ.get("TCODE_DESKTOP_REMOTE_STATE", "unconfigured")
        if state == "configured":
            try:
                with registry_workspace(ServerState.get_workdir()):
                    await asyncio.wait_for(MCPManager.initialize(ServerState.get_workdir()), timeout=12)
                    if retry:
                        await asyncio.wait_for(MCPManager.reconnect_server("job-protocol-mcp"), timeout=12)
                    status = (await MCPManager.get_status()).get("job-protocol-mcp")
                    state = "connected" if status and status.status == "connected" else "disconnected"
            except Exception:
                state = "disconnected"
        # Do not expose endpoint credentials or raw network exception strings.
        from .desktop_config import local_development_services, remote_url
        source = Path(os.environ.get("TCODE_DESKTOP_MCP_CONFIG", ""))
        try:
            config = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        except (OSError, ValueError):
            config = {}
        job = config.get("job-protocol-mcp", {}) if isinstance(config, dict) else {}
        managed = bool(os.environ.get("TCODE_INTERNAL_JOB_PROTOCOL_URL"))
        job_url = "" if managed else str(job.get("url") or "")
        try:
            from ..artifact_transport import _artifact_service_url
            artifact_url = _artifact_service_url()
        except (ValueError, RuntimeError):
            artifact_url = ''
        artifact_configured = remote_url(artifact_url, allow_loopback=local_development_services())
        artifact_state = "unconfigured"
        artifacts = []
        try:
            from .artifact_services_api import artifact_service_status
            artifacts = await artifact_service_status()
            artifact_state = next((item['state'] for item in artifacts if item['default']), 'unconfigured')
        except (ValueError, TypeError):
            artifact_state = 'invalid'
        return {"remote": state, "config_file": os.environ.get("TCODE_DESKTOP_MCP_CONFIG", ""),
                "job_protocol": {"state": state, "url": job_url, "managed": managed},
                "artifact": {"state": artifact_state, "url": artifact_url},
                "artifacts": artifacts,
                "artifact_configured": artifact_configured,
                "dev_local_services": local_development_services(),
                "local_execution": "offline-single-process" if os.environ.get("TCODE_DESKTOP_NATIVE_READY") == "1" else "unavailable", "local_runner_required": False}

    @router.get("/services/config")
    async def read_service_config(request: Request):
        user = require_current_user(request)
        source = Path(os.environ.get("TCODE_DESKTOP_MCP_CONFIG", ""))
        config = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        job = config.get("job-protocol-mcp", {}) if isinstance(config, dict) else {}
        desktop_entry = config.get("$desktop", {}) if isinstance(config, dict) else {}
        from .desktop_config import artifact_configuration
        try:
            artifacts = artifact_configuration(desktop_entry)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            **artifacts,
            "can_edit": user.role in {'owner', 'admin'},
            "job_protocol_url": str(job.get("url") or ""),
            "job_protocol_enabled": job.get("enabled", True) is not False,
            "artifact_service_url": str(desktop_entry.get("artifact_service_url") or os.environ.get("TCODE_DESKTOP_ARTIFACT_SERVICE_URL", "")),
            "transport": str(job.get("transport") or "streamable-http"),
            "restart_required": False,
        }

    @router.put("/services/config")
    async def write_service_config(request: Request):
        user = require_current_user(request)
        if user.role not in {"owner", "admin"}:
            raise HTTPException(403, "Tenant admin role is required")
        payload = await request.json()
        from .desktop_config import artifact_configuration, local_development_services, remote_url
        if not isinstance(payload, dict):
            raise HTTPException(400, '配置必须是 JSON 对象')
        has_job = any(key in payload for key in ("job_protocol_url", "job_protocol_enabled", "transport"))
        job_url = str(payload.get("job_protocol_url") or "").strip()
        artifact_url = str(payload.get("artifact_service_url") or "").strip()
        enabled = bool(payload.get("job_protocol_enabled"))
        transport = str(payload.get("transport") or "streamable-http")
        allow_loopback = local_development_services()
        if has_job and enabled and not remote_url(job_url, allow_loopback=allow_loopback):
            raise HTTPException(400, "Job Protocol 地址必须是有效的远程 HTTP(S) URL")
        if artifact_url and not remote_url(artifact_url, allow_loopback=allow_loopback):
            raise HTTPException(400, "Artifact 地址必须是有效的远程 HTTP(S) URL")
        if transport not in {"streamable-http", "sse"}:
            raise HTTPException(400, "不支持的 MCP transport")
        source = Path(os.environ.get("TCODE_DESKTOP_MCP_CONFIG", ""))
        if not os.environ.get("TCODE_DESKTOP_MCP_CONFIG"):
            raise HTTPException(409, "桌面配置文件未初始化")
        try:
            current = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        except (OSError, ValueError) as exc:
            raise HTTPException(409, '无法读取现有桌面配置，未覆盖原文件') from exc
        if not isinstance(current, dict):
            raise HTTPException(409, '桌面配置格式无效，未覆盖原文件')
        existing = current.get("job-protocol-mcp", {}) if isinstance(current, dict) else {}
        if has_job:
            current["job-protocol-mcp"] = {
                **({"headers": existing["headers"]} if isinstance(existing, dict) and "headers" in existing else {}),
                "url": job_url,
                "transport": transport,
                "enabled": enabled,
                "connect_timeout": int(existing.get("connect_timeout", 10000)) if isinstance(existing, dict) else 10000,
                "timeout": int(existing.get("timeout", 300000)) if isinstance(existing, dict) else 300000,
            }
        desktop_entry = current.get("$desktop", {}) if isinstance(current, dict) else {}
        artifact_changes = {}
        if 'artifact_services' in payload:
            try:
                if not isinstance(payload['artifact_services'], dict):
                    raise ValueError('Artifact 服务配置必须是对象')
                artifact_changes = artifact_configuration({
                    'artifact_services': payload['artifact_services'],
                    'artifact_default_service_id': payload.get('artifact_default_service_id', ''),
                    'artifact_target_services': payload.get('artifact_target_services', {}),
                })
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            default_entry = artifact_changes['artifact_services'].get(artifact_changes['artifact_default_service_id'], {})
            artifact_changes['artifact_service_url'] = default_entry.get('url', '')
        elif 'artifact_service_url' in payload:
            artifact_changes = {'artifact_service_url': artifact_url}
        current["$desktop"] = {
            **(desktop_entry if isinstance(desktop_entry, dict) else {}),
            **artifact_changes,
            **({"managed_job_protocol": False} if has_job else {}),
        }
        source.parent.mkdir(parents=True, exist_ok=True)
        temp = source.with_suffix(source.suffix + ".tmp")
        temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(source)
        # The launcher owns environment and MCP lifecycle; apply on next restart.
        return {"saved": True, "restart_required": True}

    @router.get("/workspace")
    async def resolve_workspace(request: Request, workspace_id: str):
        # HttpOnly gate cookie alone is insufficient: only the main process
        # knows this extra credential, which is never exposed via preload.
        if not hmac.compare_digest(request.headers.get("x-tcode-desktop-native", ""), token):
            raise HTTPException(403, "Native desktop access required")
        require_current_user(request)
        context = _workspace_context_from_request(request, workspace_id=workspace_id)
        workdirs.assert_workspace_active(ServerState.get_workdir(), context)
        root = _resolve_user_workdir_path(context.workspace_id, context).resolve(strict=True)
        return {"path": str(root)}

    @router.get("/file")
    async def resolve_workspace_file(request: Request, workspace_id: str, path: str):
        if not hmac.compare_digest(request.headers.get("x-tcode-desktop-native", ""), token):
            raise HTTPException(403, "Native desktop access required")
        require_current_user(request)
        context = _workspace_context_from_request(request, workspace_id=workspace_id)
        workdirs.assert_workspace_active(ServerState.get_workdir(), context)
        from .files_api import _resolve_workspace_file_path
        _root, target = await asyncio.to_thread(_resolve_workspace_file_path, path, context)
        if not target.is_file() or target.is_symlink():
            raise HTTPException(404, "File not found")
        return {"path": str(target)}

    return router
