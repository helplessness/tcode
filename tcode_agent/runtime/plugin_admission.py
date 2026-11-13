"""Python Plugin SDK v1 的宿主信任准入与执行模式决策。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


TRUSTED_ALLOWLIST_ENV = "TCODE_PLUGIN_TRUSTED_ALLOWLIST"
_OFFICIAL_PLUGIN_DIRECTORIES = {
    "enzyme-entity-discovery-demo": "enzyme_entity_discovery_demo",
    "enzyme-reaction-discovery": "enzyme_reaction_discovery",
    "example-plugin": "example_plugin",
    "paper-research-workflow": "paper_research_workflow",
    "protein-research": "protein_research",
}


def _plugin_names(value: Any) -> frozenset[str]:
    """解析不支持通配符的精确插件名称集合。"""

    return frozenset(
        name
        for item in str(value or "").split(",")
        if (name := item.strip().lower()) and "*" not in name
    )


def trusted_plugin_allowlist() -> frozenset[str]:
    """返回由宿主运维显式授权进入主进程的精确名称。"""

    return _plugin_names(os.environ.get(TRUSTED_ALLOWLIST_ENV))


def official_plugin_admitted(name: str, package_root: str | Path) -> bool:
    """只在名称和仓库内固定目录同时匹配时认定官方插件。"""

    normalized = str(name or "").strip().lower()
    directory = _OFFICIAL_PLUGIN_DIRECTORIES.get(normalized)
    if directory is None:
        return False
    repository_root = Path(__file__).resolve().parents[2]
    expected = (repository_root / "plugins" / directory).resolve()
    return Path(package_root).resolve() == expected


def plugin_execution_mode(
    name: str,
    requested_trust: str,
    package_root: str | Path,
) -> str:
    """根据宿主授权而非 Manifest 自我声明选择执行模式。"""

    normalized = str(name or "").strip().lower()
    trust = str(requested_trust or "").strip().lower()
    if trust == "quarantined":
        return "quarantined"
    if trust == "isolated":
        return "isolated-worker"
    if official_plugin_admitted(normalized, package_root):
        return "controlled-host"
    if normalized in trusted_plugin_allowlist():
        return "controlled-host"
    return "isolated-worker"


def public_workspace_id(workdir: str | Path) -> str:
    """从本地目录派生不暴露路径的稳定工作区标识。"""

    resolved = str(Path(workdir).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]
    return f"workspace:{digest}"


def plugin_admission_policy() -> dict[str, Any]:
    """返回当前 SDK v1 宿主准入与未知代码隔离策略。"""

    return {
        "sdk_api_version": "1.0",
        "trusted_allowlist": sorted(trusted_plugin_allowlist()),
        "official_plugins": sorted(_OFFICIAL_PLUGIN_DIRECTORIES),
        "unknown_plugin_default": "isolated-worker",
        "unknown_plugin_permissions": "explicit-host-grant-via-capability-broker",
        "unknown_plugin_contributions": ["tools"],
        "unknown_plugin_declarative_contributions": ["workflows", "asyncTargets", "markdown-runner-skills"],
        "unknown_plugin_worker_protocol": "tcode.plugin-worker.v2",
        "tool_consumer": "ToolExecutionPipeline",
        "prompt_consumer": "PromptContributionPipeline",
        "event_consumer": "HookRegistry",
        "single_track": True,
    }


__all__ = [
    "TRUSTED_ALLOWLIST_ENV",
    "official_plugin_admitted",
    "plugin_admission_policy",
    "plugin_execution_mode",
    "public_workspace_id",
    "trusted_plugin_allowlist",
]
