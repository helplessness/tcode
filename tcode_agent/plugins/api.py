"""HTTP API for plugin management."""

from __future__ import annotations

from pathlib import Path
import asyncio
import tempfile
import zipfile
from typing import Any, Optional

from fastapi import APIRouter, Query, Request, Depends, HTTPException
from contextvars import ContextVar
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..agent.agents import BuiltinAgents
from ..skill.manager import SkillManager
from ..prompt.complete import CompletePromptRegistry
from ..prompt.sections import prompt_section_contract_document
from ..runtime.plugin_admission import plugin_admission_policy, public_workspace_id
from ..runtime.permission_guard import permission_contract_document
from ..runtime.provider_api import provider_api_catalog_document
from ..runtime.sdk_readiness import plugin_sdk_v1_readiness_document
from ..runtime.tool_pipeline import tool_pipeline_contract_document
from .command_registry import PluginCommandRegistry
from .hooks import HookRegistry
from .manager import PluginManager
from .registry import PluginRegistry


def _get_server_state():
    from ..server.app import ServerState

    return ServerState


_authorized_workdir: ContextVar[str | None] = ContextVar("authorized_plugin_workdir", default=None)


async def _authorize_plugin_request(request: Request):
    from ..server import _shared
    from ..server.auth import auth_required
    user = getattr(request.state, "current_user", None)
    supplied = str(request.query_params.get("workdir") or "").strip()
    importing = request.url.path == '/api/plugins/install'
    body = await request.json() if request.method == "POST" and not importing and await request.body() else {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    supplied = str(body.get("workdir") or supplied).strip()
    if auth_required():
        if user is None:
            raise HTTPException(401, "Authentication required")
        if request.method == "POST" and user.role not in {"admin", "owner"}:
            raise HTTPException(403, "Plugin changes require an administrator")
        context = _shared._workspace_context_from_request(request)
        owner_root = _shared._user_workdirs_root(context).resolve()
        target = Path(supplied).expanduser().resolve() if supplied else _shared._resolve_user_workdir_path(context.workspace_id, context).resolve()
        if target == owner_root or not target.is_relative_to(owner_root):
            raise HTTPException(403, "Workspace is not owned by the current user")
    else:
        target = Path(supplied or _get_server_state().get_workdir()).resolve()
    token = _authorized_workdir.set(str(target))
    try:
        yield
    finally:
        _authorized_workdir.reset(token)


router = APIRouter(prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(_authorize_plugin_request)])


class PluginLoadRequest(BaseModel):
    name: str
    path: str
    enabled: bool = True
    config: Optional[dict[str, Any]] = None
    workdir: Optional[str] = None


class PluginUnloadRequest(BaseModel):
    name: str
    workdir: Optional[str] = None


class PluginDeleteRequest(PluginUnloadRequest):
    confirmed: bool = False


async def _get_plugin_manager(workdir: Optional[str] = None) -> PluginManager:
    """取得供管理 API 使用的插件运行时且不在查询阶段隐式补载。

    首次创建管理器仍会执行一次确定性的配置加载；之后的列表、诊断和目录
    查询保持只读。插件状态变化只能由显式 load、unload 或 reload-config 完成。
    """

    ServerState = _get_server_state()
    target_workdir = str(_authorized_workdir.get() or workdir or ServerState.get_workdir()).strip()
    return await PluginManager.get_global(
        target_workdir or ServerState.get_workdir(),
        config_path=str(Path(target_workdir) / ".tcode" / "plugins.json") if _authorized_workdir.get() else None,
        ensure_configured=False,
    )


@router.get("/list")
async def list_plugins(workdir: Optional[str] = Query(default=None)):
    manager = await _get_plugin_manager(workdir)
    return {
        "success": True,
        "plugins": [plugin.to_dict() for plugin in PluginRegistry.list_all()],
        "errors": manager.get_load_errors(),
        "diagnostics": manager.get_load_diagnostics(),
        "workdir": manager.workdir,
    }


@router.get("/available")
async def list_available_plugins(workdir: Optional[str] = Query(default=None)):
    manager = await _get_plugin_manager(workdir)
    return {"success": True, "plugins": manager.list_available_plugins(), "workdir": manager.workdir}


@router.post('/install')
async def import_plugin_package(request: Request, filename: str = Query(...)):
    """以有界流接收插件包，静态安装和登记；不隐式加载代码。"""
    from .package_install import MAX_PACKAGE_BYTES, install_package
    if not filename.lower().endswith('.tcode-plugin'):
        raise HTTPException(400, '请选择 .tcode-plugin 文件')
    if request.headers.get('content-type', '').split(';')[0] != 'application/octet-stream':
        raise HTTPException(415, '插件包须以 application/octet-stream 上传')
    manager = await _get_plugin_manager()
    try:
        with tempfile.TemporaryDirectory(prefix='tcode-plugin-upload-') as directory:
            package = Path(directory) / 'upload.tcode-plugin'
            size = 0
            with package.open('wb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_PACKAGE_BYTES:
                        raise HTTPException(413, '插件包不能超过 32 MiB')
                    stream.write(chunk)
            result = await asyncio.to_thread(install_package, manager, package)
        return {'success': True, 'installation': result, 'workdir': manager.workdir}
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, zipfile.BadZipFile, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/load")
async def load_plugin(request: PluginLoadRequest):
    manager = await _get_plugin_manager(request.workdir)
    configured = next(
        (
            item
            for item in manager._load_configured_plugins()
            if str(item.get("name") or "").strip().lower()
            == str(request.name or "").strip().lower()
        ),
        {},
    )
    effective_config = (
        dict(request.config)
        if request.config is not None
        else dict(configured.get("config") or {})
    )
    plugin_config = {
        "name": request.name,
        "path": request.path or str(configured.get("path") or ""),
        "enabled": request.enabled,
        "config": effective_config,
        "source": "api.load",
        "_source_root": str(configured.get("_source_root") or manager.workdir),
    }
    plugin_root = manager._resolve_plugin_root(plugin_config)
    if _authorized_workdir.get() and plugin_root is not None:
        bundled = (Path(__file__).resolve().parents[2] / "plugins").resolve()
        if not plugin_root.is_relative_to(Path(manager.workdir).resolve()) and not plugin_root.is_relative_to(bundled):
            raise HTTPException(403, "Plugin package must belong to this workspace or the bundled catalog")
    try:
        plugin = await manager.load_plugin(plugin_config)
    except Exception as exc:
        diagnostic = manager.get_load_diagnostics().get(request.name, {})
        return JSONResponse(
            {
                "success": False,
                "error": f"Plugin {request.name} failed to load: {exc}",
                "diagnostic": diagnostic,
                "workdir": manager.workdir,
            },
            status_code=500,
        )
    if plugin is None:
        return JSONResponse({"success": False, "error": f"plugin disabled: {request.name}"}, status_code=400)
    manager.persist_plugin_enabled_state(
        name=request.name,
        path=str(plugin_root) if plugin_root else plugin_config["path"],
        enabled=True,
        config=effective_config,
    )
    return {
        "success": True,
        "plugin": plugin.to_dict(),
        "message": f"Plugin {request.name} loaded successfully",
        "workdir": manager.workdir,
    }


@router.post("/unload")
async def unload_plugin(request: PluginUnloadRequest):
    manager = await _get_plugin_manager(request.workdir)
    ok = await manager.unload_plugin(request.name)
    if not ok:
        return JSONResponse({"success": False, "error": f"plugin not found: {request.name}"}, status_code=404)
    manager.persist_plugin_enabled_state(name=request.name, enabled=False)
    return {
        "success": True,
        "message": f"Plugin {request.name} unloaded successfully",
        "workdir": manager.workdir,
    }


@router.post('/delete')
async def delete_plugin(request: PluginDeleteRequest):
    if not request.confirmed:
        raise HTTPException(400, '删除插件需要明确确认')
    from .package_install import delete_package
    manager = await _get_plugin_manager(request.workdir)
    try:
        result = await asyncio.to_thread(delete_package, manager, request.name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'success': True, **result}


@router.post("/reload-config")
async def reload_plugins_from_config(workdir: Optional[str] = Query(default=None)):
    manager = await _get_plugin_manager(workdir)
    plugins = await manager.reload_all()
    return {
        "success": True,
        "loaded_count": len(plugins),
        "plugins": [plugin.to_dict() for plugin in plugins],
        "errors": manager.get_load_errors(),
        "diagnostics": manager.get_load_diagnostics(),
        "workdir": manager.workdir,
    }


@router.get("/tools")
async def list_plugin_tools(workdir: Optional[str] = Query(default=None)):
    await _get_plugin_manager(workdir)
    tools = []
    for plugin in PluginRegistry.list_all():
        for tool in plugin.tools:
            tools.append(
                {
                    "id": tool.id,
                    "description": tool.description,
                    "plugin": plugin.name,
                    "parameters": getattr(tool.parameters, "__name__", str(tool.parameters)),
                }
            )
    return {"success": True, "tools": tools}


@router.get("/commands")
async def list_plugin_commands(workdir: Optional[str] = Query(default=None)):
    await _get_plugin_manager(workdir)
    return {
        "success": True,
        "commands": [command.to_dict() for command in PluginCommandRegistry.list_all()],
    }


@router.get("/skills")
async def list_plugin_skills(workdir: Optional[str] = Query(default=None)):
    await _get_plugin_manager(workdir)
    return {
        "success": True,
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "plugin": skill.plugin,
                "location": skill.location,
            }
            for skill in SkillManager.all()
            if skill.plugin
        ],
    }


@router.get("/agents")
async def list_plugin_agents(workdir: Optional[str] = Query(default=None)):
    await _get_plugin_manager(workdir)
    plugin_names = {plugin.name for plugin in PluginRegistry.list_all()}
    return {
        "success": True,
        "agents": [
            {
                "name": agent.name,
                "description": agent.description,
                "mode": agent.mode.value,
            }
            for agent in BuiltinAgents.list()
            if agent.name in plugin_names or any(
                plugin.name == agent.name or agent.name in plugin.agents
                for plugin in PluginRegistry.list_all()
            )
        ],
    }


@router.get("/doctor")
async def plugin_doctor(workdir: Optional[str] = Query(default=None)):
    manager = await _get_plugin_manager(workdir)
    return {"success": True, "reports": manager.get_doctor_report(), "workdir": manager.workdir}


@router.get("/runtime")
async def plugin_runtime_contract(
    workdir: Optional[str] = Query(default=None),
):
    """返回第二批单轨运行时的无业务载荷契约。

    参数:
        workdir: 用于选择受管插件运行时的可选工作区。

    返回:
        Tool、Permission、Prompt 与 Event 唯一 Consumer 契约，不包含工具名、
        参数、Prompt 正文、会话 ID、路径、权限响应或结果正文。
    """

    manager = await _get_plugin_manager(workdir)
    workspace_id = public_workspace_id(manager.workdir)
    return {
        "success": True,
        "policy": plugin_admission_policy(),
        "tool_pipeline": tool_pipeline_contract_document(),
        "permission": permission_contract_document(),
        "prompt_sections": prompt_section_contract_document(),
        "complete_prompt_provider": CompletePromptRegistry.provider_identity(),
        "event_pipeline": {
            "consumer": "HookRegistry",
            "single_track": True,
            "event_count": len(HookRegistry.catalog()),
        },
        "workspace_id": workspace_id,
    }


@router.get("/providers")
async def provider_preview_catalog():
    return {
        "success": True,
        "provider_api": provider_api_catalog_document(),
    }


@router.get("/sdk-readiness")
async def plugin_sdk_readiness():
    return {
        "success": True,
        "readiness": plugin_sdk_v1_readiness_document(),
    }


@router.get("/health")
async def plugin_health(
    workdir: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
):
    manager = await _get_plugin_manager(workdir)
    if refresh:
        await manager.refresh_all_plugin_health()
    return {
        "success": True,
        "instances": manager.get_instance_statuses(),
        "workdir": manager.workdir,
    }


@router.get("/hooks")
async def list_plugin_hooks(workdir: Optional[str] = Query(default=None)):
    await _get_plugin_manager(workdir)
    from .hooks import HookRegistry

    return {
        "success": True,
        "hooks": HookRegistry.list_hooks(),
        "catalog": HookRegistry.catalog(),
    }
