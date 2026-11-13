"""Install a Tcode skill from a GitHub repository."""

from __future__ import annotations

import os

from pydantic import Field

from ...skill.installer import install_skill_from_github
from ...skill.source_resolver import select_skill_source
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


class SkillInstallParameters(ParameterSchema):
    source_url: str
    path: str = ""
    ref: str = "main"
    name: str = ""
    scope: str = "workspace"
    method: str = Field(default="auto", pattern="^(auto|download|git)$")
    introduction_url: str = Field(default="", description="Original introduction page when installing a discovered skill; requires source confirmation")


class SkillInstallTool(Tool):
    @property
    def id(self) -> str:
        return "skill_install"

    @property
    def description(self) -> str:
        return (
            "Install a Tcode Skill from a GitHub repository URL or owner/repo path. "
            "Use this when the user explicitly asks to install, add, or import a Skill. "
            "A /tree/<ref>/<path> URL needs no separate path; a repository URL requires path. "
            "Public repositories download directly and private repositories can use existing git credentials. "
            "Existing Skill directories are never overwritten."
            " For introduction-page URLs, first discover sources with skill_source; pass introduction_url "
            "and the selected source_url/path/ref here. This asks for explicit approval of the verified source."
        )

    @property
    def parameters(self) -> type:
        return SkillInstallParameters

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        parsed = SkillInstallParameters(**args)
        extra = context.extra if isinstance(context.extra, dict) else {}
        workdir = str(extra.get("workdir") or os.environ.get("TCODE_WORKDIR") or os.getcwd())
        try:
            if parsed.introduction_url:
                candidate = await select_skill_source(parsed.introduction_url, parsed.source_url, parsed.path, parsed.ref)
                prompt = extra.get("__permission_prompt__")
                # Do not use context.ask's permissive no-callback fallback for discovered code.
                if not callable(prompt):
                    raise ValueError("当前执行环境无法展示来源确认，请在技能库中解析并确认安装")
                decision = prompt("skill_install", {
                    "operation": "install_discovered_skill", "source_url": candidate["source_url"],
                    "introduction_url": parsed.introduction_url, "name": parsed.name or candidate["name"],
                    "description": candidate["description"], "path": candidate["install_path"],
                    "ref": candidate["ref"], "scope": parsed.scope,
                    "notice": "仅安装技能文件，不执行脚本；来源内容未通过安全审计。",
                })
                if hasattr(decision, "__await__"):
                    decision = await decision
                if decision is not True and str(decision or "").strip().lower() not in {"allow", "once", "y", "yes", "true", "always", "allow_always"}:
                    return ToolResult(title="已取消技能安装", output="未安装技能，也未修改已有技能。", metadata={"cancelled": True})
                parsed.path, parsed.ref = candidate["install_path"], candidate["ref"]
            installed = await install_skill_from_github(
                source_url=parsed.source_url,
                path=parsed.path,
                ref=parsed.ref,
                name=parsed.name,
                scope=parsed.scope,
                method=parsed.method,
                workdir=workdir,
            )
        except Exception as exc:
            terminal = isinstance(exc, (ValueError, FileExistsError))
            return ToolResult(
                title="Skill 安装失败",
                output="",
                error=str(exc),
                metadata={
                    "retryable": not terminal,
                    "terminal_tool_error": terminal,
                },
            )
        return ToolResult(
            title=f"Installed {installed['name']}",
            output=(
                f"Skill {installed['name']} installed successfully.\n"
                f"Location: {installed['location']}\n"
                "It is available to Tcode agents immediately."
            ),
            metadata={"skill_install": True, **installed},
        )

    @classmethod
    def register(cls) -> None:
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
