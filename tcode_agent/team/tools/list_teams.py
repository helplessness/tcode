"""Tool: list_teams - show available team catalogs and runtime agents."""

import os
from typing import Any

from ...agent.agents import BuiltinAgents
from ...tools.base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ..catalog import TeamCatalog
from ..protein_team_config import load_protein_team_config


class ListTeamsParameters(ParameterSchema):
    include_unavailable: bool = False


class ListTeamsTool(Tool):
    @staticmethod
    def _expected_members_for_team(entry: Any) -> list[dict[str, str]]:
        team_key = getattr(entry, "key", "")
        key = str(team_key or "").strip().lower()
        if key == "protein":
            cfg = load_protein_team_config()
            rows: list[dict[str, str]] = []
            for item in cfg.get("workers", []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                if not name:
                    continue
                rows.append(
                    {
                        "name": name,
                        "role": str(item.get("role", "") or "").strip(),
                        "agent_name": f"worker_{name}",
                    }
                )
            return rows

        if key == "paper":
            rows: list[dict[str, str]] = []
            for agent in BuiltinAgents.list():
                name = str(getattr(agent, "name", "") or "").strip()
                if not name.startswith("paper_"):
                    continue
                if name == "paper_auto_coordinator":
                    continue
                rows.append(
                    {
                        "name": name,
                        "role": str(getattr(agent, "description", "") or "").strip(),
                        "agent_name": name,
                    }
                )
            rows.sort(key=lambda item: item.get("name", ""))
            return rows

        rows: list[dict[str, str]] = []
        for worker in getattr(entry, "workers", ()) or ():
            enabled = bool(getattr(worker, "enabled", True))
            visible = bool(getattr(worker, "visible", True))
            if not enabled or not visible:
                continue
            name = str(getattr(worker, "name", "") or "").strip()
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "role": str(getattr(worker, "role", "") or "").strip(),
                    "agent_name": str(getattr(worker, "agent_name", "") or "").strip(),
                }
            )
        return rows

    @property
    def id(self) -> str:
        return "list_teams"

    @property
    def description(self) -> str:
        return "列出当前可用团队（team_key、显示名、runtime agent、路由原因和可用性）。"

    @property
    def parameters(self) -> type:
        return ListTeamsParameters

    async def _ensure_plugin_catalog_loaded(self, workdir: str) -> None:
        from ...plugins.manager import PluginManager

        candidates = [
            str(os.environ.get("TCODE_WORKDIR", "") or "").strip(),
            os.getcwd(),
            str(workdir or "").strip(),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                await PluginManager.get_global(candidate)
            except Exception:
                continue

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ListTeamsParameters(**args)
        include_unavailable = bool(parsed.include_unavailable)
        workdir = str((getattr(context, "extra", None) or {}).get("workdir") or "").strip()
        await self._ensure_plugin_catalog_loaded(workdir)

        entries = TeamCatalog.list(available_only=False)
        if not entries:
            return ToolResult(title="list_teams", output="当前未注册任何团队。")

        rows: list[dict[str, Any]] = []
        for entry in entries:
            available = BuiltinAgents.get(entry.runtime_agent) is not None
            if not include_unavailable and not available:
                continue
            rows.append(
                {
                    "team_key": entry.key,
                    "display_name": entry.display_name,
                    "runtime_agent": entry.runtime_agent,
                    "visible_agent_name": entry.visible_agent_name,
                    "delegation_reason": entry.delegation_reason,
                    "source": entry.source,
                    "available": available,
                    "expected_members": self._expected_members_for_team(entry),
                }
            )

        if not rows:
            return ToolResult(
                title="list_teams",
                output="当前没有可用团队。可使用 include_unavailable=true 查看已注册但不可用的团队。",
                metadata={"teams": []},
            )

        lines = ["当前团队目录：", ""]
        for idx, row in enumerate(rows, 1):
            status = "available" if row["available"] else "unavailable"
            reason = str(row.get("delegation_reason") or "")
            lines.append(
                f"{idx}. {row['display_name']} (key={row['team_key']}, runtime={row['runtime_agent']}, status={status})"
            )
            if reason:
                lines.append(f"   reason: {reason}")
            expected_members = row.get("expected_members") or []
            if expected_members:
                lines.append(f"   expected_members: {len(expected_members)}")
                preview = expected_members[:8]
                for member in preview:
                    name = str(member.get("name", "") or "").strip()
                    role = str(member.get("role", "") or "").strip()
                    if role:
                        lines.append(f"   - {name}: {role}")
                    else:
                        lines.append(f"   - {name}")
                if len(expected_members) > len(preview):
                    lines.append(
                        f"   - ... 还有 {len(expected_members) - len(preview)} 个成员模板"
                    )

        return ToolResult(
            title="list_teams",
            output="\n".join(lines),
            metadata={"teams": rows},
        )

    @classmethod
    def register(cls) -> None:
        from ...tools.registry import ToolRegistry

        ToolRegistry.register(cls())
