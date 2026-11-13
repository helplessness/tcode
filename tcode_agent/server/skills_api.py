"""Skill catalog inspection and activation endpoints.

Routes retain their original paths and are registered by app.py.
"""

from pathlib import Path
from typing import Any

import aiohttp

from fastapi import APIRouter, HTTPException, Query, Request

from ..agent.agents import BuiltinAgents
from ..skill.activation import SkillActivationStore
from ..skill.manager import SkillManager
from ..skill.installer import install_skill_from_github
from ..skill.source_resolver import resolve_skill_source, select_skill_source
from ..skill.runtime_binding import skill_runtime_binding
from .auth_api import _require_tenant_admin
from .plugin_warmup import plugin_request_wait_seconds, wait_for_plugin_warmup
from .state import ServerState
from . import workdirs
from .workspace_scope import _resolve_user_workdir_path, _workspace_context_from_request

router = APIRouter()


def _skill_workdir(request: Request, workspace_id: str = "") -> str:
    workspace = str(workspace_id or "").strip()
    if not workspace:
        return ServerState.get_workdir()
    context = _workspace_context_from_request(request, workspace_id=workspace)
    workdirs.assert_workspace_active(ServerState.get_workdir(), context)
    return str(_resolve_user_workdir_path(context.workspace_id, context))


@router.post("/api/skills/install")
async def install_skill(payload: dict[str, Any], request: Request):
    from ..runtime.workspace_registry import registry_workspace

    _require_tenant_admin(request)
    workdir = _skill_workdir(request, str(payload.get("workspace_id") or ""))
    try:
        introduction_url = str(payload.get("introduction_url") or "")
        source_url = str(payload.get("source_url") or "")
        path, ref = str(payload.get("path") or ""), str(payload.get("ref") or "main")
        if introduction_url:
            if payload.get("confirmed") is not True:
                raise ValueError("请先确认技能来源及安装范围")
            selected = await select_skill_source(introduction_url, source_url, path, str(payload.get("ref") or ""))
            path, ref = selected["install_path"], selected["ref"]
        with registry_workspace(workdir):
            return await install_skill_from_github(
                source_url=source_url,
                path=path,
                ref=ref,
                name=str(payload.get("name") or ""),
                scope=str(payload.get("scope") or "workspace"),
                method=str(payload.get("method") or "auto"),
                workdir=workdir,
            )
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "来源读取超时，请重试或提供精确 GitHub 地址") from exc
    except aiohttp.ClientError as exc:
        raise HTTPException(502, "无法读取技能来源，请检查网络后重试") from exc


@router.post("/api/skills/resolve-source")
async def resolve_source(payload: dict[str, Any], request: Request):
    _require_tenant_admin(request)
    _skill_workdir(request, str(payload.get("workspace_id") or ""))
    try:
        return await resolve_skill_source(str(payload.get("url") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "来源读取超时，请重试或提供精确 GitHub 地址") from exc
    except Exception as exc:
        raise HTTPException(502, "无法读取技能来源，请检查网络或提供精确 GitHub 地址") from exc


def _skill_catalog_agents() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for agent in BuiltinAgents.list():
        if agent.hidden:
            continue
        options = dict(agent.options or {})
        result.append(
            {
                "name": agent.name,
                "display_name": str(options.get("display_name") or agent.name),
                "description": agent.description,
                "mode": agent.mode.value,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["name"] != "build",
            item["mode"] not in {"primary", "manager"},
            item["display_name"],
        ),
    )


def _skill_category(skill) -> str:
    metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
    for key in ("category", "skill_role", "domain"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    if skill.plugin:
        return "plugin"
    if skill.source_kind == "home":
        return "personal"
    return "workspace"


def _skill_agent_states(
    skill,
    agents: list[dict[str, Any]],
    workdir: str,
) -> dict[str, dict[str, bool]]:
    binding = skill_runtime_binding(skill.name)
    states: dict[str, dict[str, bool]] = {}
    for agent in agents:
        name = str(agent["name"])
        available = binding is None or binding.agent.lower() == name.lower()
        states[name] = {
            "available": available,
            "enabled": available
            and SkillActivationStore.is_enabled(skill.name, name, workdir),
        }
    return states


def _serialize_skill_summary(skill, agents: list[dict[str, Any]], workdir: str) -> dict[str, Any]:
    metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
    tags = metadata.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    if not isinstance(tags, list):
        tags = []
    binding = skill_runtime_binding(skill.name)
    return {
        "name": skill.name,
        "skill_id": skill.skill_id,
        "description": skill.description,
        "location": skill.location,
        "source": skill.source or "",
        "source_kind": skill.source_kind,
        "plugin": skill.plugin or "",
        "version": skill.version,
        "category": _skill_category(skill),
        "tags": [str(item) for item in tags if str(item).strip()],
        "required_agent": binding.agent if binding else "",
        "agent_states": _skill_agent_states(skill, agents, workdir),
    }


def _skill_package_files(skill) -> list[dict[str, Any]]:
    skill_file = Path(skill.location).expanduser().resolve(strict=False)
    skill_dir = skill_file.parent
    files: list[dict[str, Any]] = []
    try:
        candidates = sorted(path for path in skill_dir.rglob("*") if path.is_file())
    except Exception:
        candidates = [skill_file] if skill_file.is_file() else []
    for path in candidates[:100]:
        try:
            relative = path.relative_to(skill_dir).as_posix()
            size = path.stat().st_size
        except Exception:
            continue
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "relative_path": relative,
                "size": size,
                "primary": path.resolve(strict=False) == skill_file,
            }
        )
    return files


@router.get("/api/skills")
async def list_skills(request: Request, workspace_id: str = Query(default="")):
    from ..runtime.workspace_registry import registry_workspace

    workdir = _skill_workdir(request, workspace_id)
    with registry_workspace(workdir):
        await wait_for_plugin_warmup(workdir, timeout=plugin_request_wait_seconds())
        await SkillManager.load(workdir=workdir)
        agents = _skill_catalog_agents()
        return {
            "skills": [
                _serialize_skill_summary(skill, agents, workdir)
                for skill in SkillManager.all()
            ],
            "agents": agents,
            "activation": SkillActivationStore.snapshot(workdir),
        }


@router.get("/api/skills/{skill_name}")
async def get_skill(skill_name: str, request: Request, workspace_id: str = Query(default="")):
    from ..runtime.workspace_registry import registry_workspace

    workdir = _skill_workdir(request, workspace_id)
    with registry_workspace(workdir):
        await wait_for_plugin_warmup(workdir, timeout=plugin_request_wait_seconds())
        await SkillManager.load(workdir=workdir)
        skill = SkillManager.get(skill_name)
        if not skill:
            raise HTTPException(404, "Skill not found")
        agents = _skill_catalog_agents()
        return {
            **_serialize_skill_summary(skill, agents, workdir),
            "content": skill.content,
            "files": _skill_package_files(skill),
        }


@router.patch("/api/skills/{skill_name}/activation")
async def update_skill_activation(
    skill_name: str,
    payload: dict[str, Any],
    request: Request,
):
    from ..runtime.workspace_registry import registry_workspace

    _require_tenant_admin(request)
    workdir = _skill_workdir(request, str(payload.get("workspace_id") or ""))
    with registry_workspace(workdir):
        await wait_for_plugin_warmup(workdir, timeout=plugin_request_wait_seconds())
        await SkillManager.load(workdir=workdir)
        skill = SkillManager.get(skill_name)
        if not skill:
            raise HTTPException(404, "Skill not found")

        agent_name = str(payload.get("agent") or "").strip()
        enabled = payload.get("enabled")
        if not agent_name:
            raise HTTPException(400, "agent is required")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled must be a boolean")

        agents = _skill_catalog_agents()
        known_agents = {str(item["name"]).lower() for item in agents}
        if agent_name.lower() not in known_agents:
            raise HTTPException(404, "Agent not found")
        binding = skill_runtime_binding(skill.name)
        if binding is not None and binding.agent.lower() != agent_name.lower():
            raise HTTPException(
                409,
                f'Skill is bound to dedicated agent "{binding.agent}"',
            )

        effective = SkillActivationStore.set_enabled(
            skill.name,
            agent_name,
            enabled,
            workdir,
        )
        return {
            "name": skill.name,
            "agent": agent_name,
            "enabled": effective,
            "agent_states": _skill_agent_states(skill, agents, workdir),
        }
