"""Agent-scoped capability discovery and permission-preserving tool activation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import ConfigDict, Field

from ...agent.agents import BuiltinAgents
from ...mcp.agent_specific_mcp import AgentSpecificMCPManager
from ...mcp.manager_auto import MCPManager
from ...runtime.workspace_registry import registry_workspace
from ...skill.manager import SkillManager
from ...skill.runtime_binding import skill_allowed_for_agent
from ...skill.tool import SkillTool
from ...team.catalog import TeamCatalog
from ...team.registry import WorkerRegistry
from ...types import ToolResult
from ...workflow.registry import WorkflowTemplateRegistry
from ..base import ParameterSchema, Tool, ToolContext


CapabilityKind = Literal["tools", "skills", "mcp", "teams", "workflows"]


class CapabilityCatalogParameters(ParameterSchema):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "search", "activate", "reset"] = "list"
    tool_ids: list[str] = Field(default_factory=list, max_length=32)
    kinds: list[CapabilityKind] = Field(
        default_factory=lambda: ["tools", "skills", "mcp", "teams", "workflows"]
    )
    query: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=200)
    detail: Literal["summary", "compact", "full"] = "compact"
    include_unavailable: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


def _permission_action(context: ToolContext, tool_id: str) -> str:
    extra = context.extra if isinstance(context.extra, dict) else {}
    resolver = extra.get("__permission_resolve__")
    if callable(resolver):
        try:
            value = resolver(tool_id, [tool_id])
        except TypeError:
            value = resolver(tool_id)
        normalized = str(value or "").strip().lower()
        if normalized in {"allow", "ask", "deny"}:
            return normalized
    permission = extra.get("agent_permission")
    if not isinstance(permission, dict):
        return "allow"
    action = "allow"
    for pattern, value in permission.items():
        if not isinstance(value, str):
            continue
        if pattern == "*" or pattern == tool_id or fnmatch.fnmatch(tool_id, pattern):
            action = value.strip().lower()
    return action if action in {"allow", "ask", "deny"} else "allow"


def _current_tool_ids(context: ToolContext) -> set[str]:
    extra = context.extra if isinstance(context.extra, dict) else {}
    return {
        str(item or "").strip()
        for item in extra.get("current_tool_ids", [])
        if str(item or "").strip()
    }


def _discovery_result(payload: dict[str, Any], *, title: str = "Capability Catalog") -> ToolResult:
    # Queries and pagination offsets are not new evidence. Full schema details are.
    semantic = payload.get("items", payload.get("active_tool_ids", []))
    fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    metadata = {**payload, "semantic_readonly": True, "result_fingerprint": fingerprint}
    return ToolResult(title=title, output=json.dumps(payload, ensure_ascii=False, indent=2), metadata=metadata)


def _schema_parameters(schema: Any) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = set(schema.get("required") or [])
    result = []
    for name, raw in properties.items():
        item = raw if isinstance(raw, dict) else {}
        result.append(
            {
                "name": str(name),
                "type": item.get("type") or "any",
                "required": name in required,
                "description": str(item.get("description") or "")[:500],
            }
        )
    return result


class CapabilityCatalogTool(Tool):
    @property
    def id(self) -> str:
        return "capability_catalog"

    @property
    def description(self) -> str:
        return (
            "Inspect the skills, MCP services, team templates/runtime members, and workflow "
            "templates available to this agent in the current workspace. Returns compact, "
            "permission-filtered summaries by default; request one exact name with detail=full "
            "to load bounded details. Use action=search to find admitted tools, then "
            "action=activate with exact tool_ids to expose their complete schemas on the next "
            "model call. Activation never executes tools or grants permission. "
            "A rejection with next_action=stop_discovery is final for this agent; do not retry synonyms "
            "or imitate a workflow by directly calling its stage tools. "
            "Use kinds=['tools'] for tool search. Use action=reset when starting an unrelated task "
            "to clear retained tool definitions. Computational MCP targets must keep their existing asynchronous submission route."
        )

    @property
    def parameters(self) -> type:
        return CapabilityCatalogParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = CapabilityCatalogParameters(**args)
        extra = context.extra if isinstance(context.extra, dict) else {}
        if parsed.action in {"activate", "reset"}:
            activate = extra.get("__tool_discovery_activate__")
            if not callable(activate) or (parsed.action == "activate" and not parsed.tool_ids):
                return ToolResult(title="Tool activation", output="Activation requires exact tool_ids and an active agent runtime.", error="Tool activation unavailable")
            payload = (await activate([], reset=True) if parsed.action == "reset"
                       else await activate(parsed.tool_ids))
            return _discovery_result(payload, title="Tool activation")
        workdir = str(extra.get("workdir") or "").strip()
        with registry_workspace(workdir):
            items: list[dict[str, Any]] = []
            kinds = list(dict.fromkeys(parsed.kinds))
            if "tools" in kinds:
                getter = extra.get("__tool_discovery_catalog__")
                current = _current_tool_ids(context)
                for tool in getter() if callable(getter) else []:
                    if _permission_action(context, tool.name) == "deny":
                        continue
                    from ..capabilities import get_tool_capability
                    tags = list(get_tool_capability(tool.name).tags)
                    aliases = {"structure": "蛋白 三维结构 残基 选区 颜色 测距", "local_fs": "本地 文件 读取 写入 检查",
                               "web": "网页 联网 搜索", "research": "研究 文献", "evidence": "证据 核实",
                               "planning": "计划 待办", "orchestration": "协作 子代理 任务", "memory": "记忆 历史"}
                    item = {"kind": "tool", "id": tool.name, "name": tool.name,
                            "summary": tool.description, "available": True,
                            "tags": tags, "search_terms": " ".join(aliases.get(tag, tag) for tag in tags),
                            "loaded": tool.name in current,
                            "execution_route": "direct",
                            "activate_via": {"tool": self.id, "arguments": {
                                "action": "activate", "tool_ids": [tool.name]}}}
                    if parsed.detail == "full":
                        item["parameters"] = tool.parameters
                    items.append(item)
            if "skills" in kinds:
                items.extend(await self._skills(context, workdir, parsed.detail))
            if "mcp" in kinds:
                items.extend(await self._mcp(context, parsed.detail, parsed.include_unavailable))
            if "teams" in kinds:
                items.extend(await self._teams(context, parsed.detail, parsed.include_unavailable))
            if "workflows" in kinds:
                items.extend(self._workflows(context, parsed.detail, parsed.include_unavailable))

        needle = (parsed.name or parsed.query).strip().lower()
        if needle:
            words = re.findall(r"[a-z0-9_\-.]+|[\u4e00-\u9fff]+", needle)
            words = list(dict.fromkeys(words + [word[i:i+2] for word in words
                          if re.fullmatch(r"[\u4e00-\u9fff]+", word)
                          for i in range(len(word)-1)]))
            def score(item: dict[str, Any]) -> int:
                name = str(item.get("id") or "").lower()
                text = " ".join(str(item.get(key) or "") for key in ("id", "name", "summary", "search_terms")).lower()
                return (100 if name == needle else 20 if name in words else 0) + sum(1 for word in words if word in text)
            items = [item for item in items if (
                str(item.get("id") or "").lower() == needle or str(item.get("name") or "").lower() == needle
            )] if parsed.name else [item for item in items if score(item)]
            items.sort(key=lambda item: (-score(item), str(item.get("kind")), str(item.get("id"))))
        else:
            items.sort(key=lambda item: (str(item.get("kind")), str(item.get("id"))))
        total = len(items)
        page = items[parsed.offset : parsed.offset + parsed.limit]
        if parsed.detail != "full":
            page = [{**item, "summary": str(item.get("summary") or "")[:500]}
                    if item.get("kind") == "tool" else item for item in page]
        next_offset = parsed.offset + len(page)
        has_more = next_offset < total
        payload = {
            "scope": {
                "workspace_id": str(extra.get("workspace_id") or ""),
                "session_id": context.session_id,
                "agent": context.agent,
                "execution_profile": str(extra.get("execution_profile") or ""),
            },
            "items": page,
            "total": total,
            "offset": parsed.offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
        }
        if parsed.name and not page:
            payload["message"] = "No visible capability matched that name."
            status = extra.get("__tool_discovery_status__")
            if "tools" in parsed.kinds and callable(status):
                payload["availability"] = status(parsed.name)
        return _discovery_result(payload)

    async def _skills(
        self, context: ToolContext, workdir: str, detail: str
    ) -> list[dict[str, Any]]:
        await SkillManager.load(workdir=workdir)
        current = _current_tool_ids(context)
        result = []
        for skill in SkillManager.all():
            if not skill_allowed_for_agent(skill.name, context.agent, workdir):
                continue
            if not SkillTool._is_skill_visible(skill, context.extra):
                continue
            item = {
                "kind": "skill",
                "id": skill.name,
                "name": skill.name,
                "summary": skill.description,
                "source": skill.source_kind,
                "registered": True,
                "visible": True,
                "callable": "skill" in current if current else None,
                "call_via": {"tool": "skill", "arguments": {"name": skill.name}},
                "detail_loader": self._detail_loader("skills", skill.name),
            }
            if detail == "full":
                content = skill.content[:50_000]
                item.update(
                    {
                        "version": skill.version,
                        "plugin": skill.plugin or "",
                        "actions": sorted(skill.actions),
                        "content": content,
                        "content_truncated": len(content) < len(skill.content),
                    }
                )
            result.append(item)
        return result

    async def _mcp(
        self, context: ToolContext, detail: str, include_unavailable: bool
    ) -> list[dict[str, Any]]:
        if not MCPManager.is_initialized():
            return []
        status = await MCPManager.get_status()
        tools = await MCPManager.get_tools()
        agent = BuiltinAgents.get(context.agent)
        filter_manager = AgentSpecificMCPManager()
        admitted_tools = []
        if agent is not None:
            from ...mcp.agent_integration import MCPTool

            adapters = [
                MCPTool(
                    str(value.get("server") or ""),
                    str(value.get("tool") or ""),
                    str(value.get("description") or ""),
                    value.get("inputSchema") or {},
                    value.get("_meta") or {},
                )
                for value in tools.values()
            ]
            admitted_tools = filter_manager._filter_tools_by_agent(agent, adapters)
        admitted_ids = {tool.id for tool in admitted_tools}
        current = _current_tool_ids(context)
        result: list[dict[str, Any]] = []
        for tool_id, value in tools.items():
            if tool_id not in admitted_ids:
                continue
            server = str(value.get("server") or "")
            route = MCPManager.get_server_route_info(server)
            connected = str(getattr(status.get(server), "status", "")) == "connected"
            callable_now = connected and bool(route.get("user_callable"))
            item = {
                "kind": "mcp_tool",
                "id": tool_id,
                "name": str(value.get("tool") or tool_id),
                "summary": str(value.get("description") or ""),
                "source": f"mcp:{server}",
                "registered": True,
                "connected": connected,
                "visible": True,
                "callable": callable_now and tool_id in current,
                "available": connected,
                "loaded": tool_id in current,
                "execution_route": "direct" if callable_now else "async_submission",
                "route_mode": route.get("route_mode"),
                "submit_via": route.get("submit_via"),
                "call_via": {"tool": tool_id} if callable_now else None,
                "detail_loader": self._detail_loader("mcp", tool_id),
            }
            if detail == "full":
                item["parameters"] = _schema_parameters(value.get("inputSchema"))
            result.append(item)

        if include_unavailable:
            visible_servers = {
                str(item.get("source") or "").removeprefix("mcp:")
                for item in result
            }
            configured = filter_manager._configured_allowed_tools(context.agent)
            may_see_all = configured == "all" or (
                configured is None and not context.agent.lower().startswith("worker_")
            )
            allowed = configured if isinstance(configured, list) else []
            for server, server_status in status.items():
                if server in visible_servers:
                    continue
                normalized = filter_manager._normalize_tool_key(server)
                allowed_server = may_see_all or any(
                    filter_manager._normalize_tool_key(value) in {normalized, f"{normalized}-mcp"}
                    or filter_manager._normalize_tool_key(value) in normalized
                    for value in allowed
                )
                if not allowed_server:
                    continue
                route = MCPManager.get_server_route_info(server)
                result.append(
                    {
                        "kind": "mcp_server",
                        "id": server,
                        "name": server,
                        "summary": "Configured MCP service without currently visible tools.",
                        "source": f"mcp:{server}",
                        "registered": True,
                        "connected": str(server_status.status) == "connected",
                        "visible": True,
                        "callable": False,
                        "route_mode": route.get("route_mode"),
                        "submit_via": route.get("submit_via"),
                    }
                )
        return result

    async def _teams(
        self, context: ToolContext, detail: str, include_unavailable: bool
    ) -> list[dict[str, Any]]:
        can_inspect_runtime = _permission_action(context, "list_workers") != "deny"
        current = _current_tool_ids(context)
        result = []
        for team in TeamCatalog.list(available_only=False):
            available = BuiltinAgents.get(team.runtime_agent) is not None
            if not available and not include_unavailable:
                continue
            item = {
                "kind": "team_template",
                "id": team.key,
                "name": team.display_name,
                "summary": team.delegation_reason,
                "source": team.source,
                "registered": True,
                "available": available,
                "visible": True,
                "callable": _permission_action(context, "assign_task") != "deny"
                and ("assign_task" in current if current else True),
                "runtime_agent": team.runtime_agent,
                "expected_member_count": len(team.workers),
                "detail_loader": self._detail_loader("teams", team.key),
            }
            if detail == "full":
                item["expected_members"] = [
                    {
                        "name": worker.name,
                        "role": worker.role,
                        "agent_name": worker.agent_name,
                    }
                    for worker in team.workers
                    if worker.enabled and worker.visible
                ]
                if can_inspect_runtime:
                    item["runtime_members"] = [
                        {
                            "name": worker.name,
                            "role": worker.role,
                            "status": worker.status,
                            "session_id": worker.session_id,
                        }
                        for worker in WorkerRegistry.list_for_team(context.session_id)
                    ]
            result.append(item)
        return result

    def _workflows(
        self, context: ToolContext, detail: str, include_unavailable: bool
    ) -> list[dict[str, Any]]:
        extra = context.extra if isinstance(context.extra, dict) else {}
        user_id = str(extra.get("user_id") or "")
        current = _current_tool_ids(context)
        getter = extra.get("__tool_discovery_catalog__")
        admitted = {tool.name for tool in getter()} if callable(getter) else set()
        can_activate = "workflow_run" in admitted and _permission_action(context, "workflow_run") != "deny"
        selected = str(extra.get("selected_workflow_key") or "")
        result = []
        for template in WorkflowTemplateRegistry.list_templates(
            include_disabled=include_unavailable
        ):
            metadata = dict(template.metadata or {})
            owner = str(metadata.get("owner_user_id") or "")
            if bool(metadata.get("private")) and owner and owner != user_id:
                continue
            admitted_template = can_activate and (not selected or selected == template.template_key)
            item = {
                "kind": "workflow_template",
                "id": template.template_key,
                "name": template.display_name,
                "summary": template.description[:500],
                "source": str(metadata.get("source") or "builtin"),
                "registered": True,
                "enabled": bool(template.enabled),
                "visible": True,
                "callable": bool(template.enabled) and admitted_template and "workflow_run" in current,
                "activatable": bool(template.enabled) and admitted_template,
                "availability_reason": ("template_disabled" if not template.enabled else
                    "different_from_explicit_selection" if selected and selected != template.template_key else
                    "loaded" if admitted_template and "workflow_run" in current else
                    "requires_activation" if admitted_template else "workflow_run_not_admitted"),
                "stage_count": len(template.stages),
                "call_via": (
                    {"tool": "workflow_run", "arguments": {"template_key": template.template_key}}
                    if template.enabled and admitted_template
                    else None
                ),
                "detail_loader": self._detail_loader("workflows", template.template_key),
            }
            if detail == "full":
                item["stages"] = [
                    {
                        "stage_id": stage.stage_id,
                        "owner": stage.owner,
                        "call_type": stage.call_type,
                        "required_skills": list(stage.required_skills or []),
                        "allowed_tools": list(stage.allowed_tools or []),
                    }
                    for stage in template.stages
                ]
            result.append(item)
        return result

    @staticmethod
    def _detail_loader(kind: CapabilityKind, name: str) -> dict[str, Any]:
        return {
            "tool": "capability_catalog",
            "arguments": {"kinds": [kind], "name": name, "detail": "full"},
        }

    @classmethod
    def register(cls) -> None:
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
