"""把 Hook、Tool、Config/Env 与 Transcript 收敛为一个权威目录。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import config_catalog, hook_catalog, tool_catalog
from .common import check_or_write, repo_root
from .transcript import default_fixture_root, fixture_summary


SCHEMA = "tcode.dsh-current-catalog.v1"
DEFAULT_JSON_PATH = Path("docs/dsh/generated/current-catalog.json")
DEFAULT_MARKDOWN_PATH = Path("docs/dsh/generated/current-catalog.md")


def _combined_digest(components: dict[str, dict[str, Any]]) -> str:
    """根据各子目录的源摘要计算与生成时间无关的组合摘要。"""

    digest = hashlib.sha256()
    for name in sorted(components):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(components[name].get("source_digest") or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_catalog(root: Path) -> dict[str, Any]:
    """构建不导入应用模块、不读取 Secret 值的综合 DSH 目录。"""

    root = root.resolve()
    hooks = hook_catalog.build_catalog(root)
    tools = tool_catalog.build_catalog(root)
    configuration = config_catalog.build_catalog(root)
    transcripts = fixture_summary(default_fixture_root(root))
    components = {
        "hook_events": hooks,
        "tools": tools,
        "config_env": configuration,
        "transcripts": transcripts,
    }
    source_files = sorted(
        {
            str(path)
            for component in components.values()
            for path in component.get("source_files", [])
        }
    )
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "generated_by": "python scripts/dsh.py catalog",
        "source_digest": _combined_digest(components),
        "source_files": source_files,
        "controls": {
            "imports_application_modules": False,
            "reads_process_environment_values": False,
            "starts_external_services": False,
            "changes_runtime_behavior": False,
        },
        "summary": {
            "hook_event_count": hooks["summary"]["declared_event_count"],
            "static_tool_count": tools["summary"]["static_tool_count"],
            "tool_warning_count": len(tools["warnings"]),
            "environment_key_count": configuration["summary"]["environment_key_count"],
            "config_key_count": configuration["summary"]["config_key_count"],
            "transcript_case_count": transcripts["case_count"],
            "source_file_count": len(source_files),
        },
        "components": components,
    }


def render_json(catalog: dict[str, Any]) -> str:
    """渲染可供 CI 漂移门禁使用的稳定综合 JSON。"""

    return json.dumps(
        catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def render_markdown(catalog: dict[str, Any]) -> str:
    """渲染只保留当前事实和风险的紧凑综合目录。"""

    summary = catalog["summary"]
    tool_component = catalog["components"]["tools"]
    config_component = catalog["components"]["config_env"]
    transcript_component = catalog["components"]["transcripts"]
    lines = [
        "# DSH 当前权威目录",
        "",
        "> 由 `python scripts/dsh.py catalog` 静态生成，请勿手工编辑。",
        f"> Schema：`{catalog['schema']}`；Source digest：`{catalog['source_digest']}`。",
        "",
        "## 摘要",
        "",
        "| 事实 | 数量 |",
        "| --- | ---: |",
        f"| Hook/Event | {summary['hook_event_count']} |",
        f"| 静态 Tool | {summary['static_tool_count']} |",
        f"| Tool 诊断 | {summary['tool_warning_count']} |",
        f"| 环境变量键 | {summary['environment_key_count']} |",
        f"| JSON/Schema 配置键 | {summary['config_key_count']} |",
        f"| Transcript 场景 | {summary['transcript_case_count']} |",
        f"| 静态来源文件 | {summary['source_file_count']} |",
        "",
        "## Tool 诊断",
        "",
        "| 代码 | 数量 |",
        "| --- | ---: |",
    ]
    warning_counts = tool_component["summary"].get("warning_counts", {})
    for code, count in sorted(warning_counts.items()):
        lines.append(f"| `{code}` | {count} |")
    lines.extend(
        [
            "",
            "Catalog 诊断不修改 Capability、Permission、Approval、Sandbox 或生产默认。",
            "两个 SDK Capability mismatch 仍采用更保守的有效值；在第二批单轨化时统一语义来源。",
            "",
            "## Config / Env 安全控制",
            "",
            f"- Secret 环境键：{config_component['summary']['secret_environment_key_count']} 个，值全部脱敏。",
            f"- Secret 配置键：{config_component['summary']['secret_config_key_count']} 个，值全部脱敏。",
            "- 未读取当前进程环境变量值，未展开 env 文件，未改变运行时配置。",
            "",
            "## Transcript 基线",
            "",
            f"- 场景：{transcript_component['case_count']} 个。",
            f"- 事件：{transcript_component['event_count']} 条。",
            f"- Tool 结果分布：`{transcript_component['tool_outcomes']}`。",
            "- 回放不需要 API key，也不会连接外部 LLM、MCP 或 Sandbox。",
            "",
        ]
    )
    return "\n".join(lines)


def catalog_paths(root: Path | None = None) -> tuple[Path, Path]:
    """返回指定仓库下综合 JSON 和 Markdown 的默认绝对路径。"""

    base = root or repo_root()
    return base / DEFAULT_JSON_PATH, base / DEFAULT_MARKDOWN_PATH


def generate(root: Path, *, check: bool) -> tuple[bool, dict[str, Any]]:
    """生成或只读检查综合目录，并返回整体状态与目录对象。"""

    catalog = build_catalog(root)
    json_path, markdown_path = catalog_paths(root)
    json_ok = check_or_write(json_path, render_json(catalog), check=check)
    markdown_ok = check_or_write(markdown_path, render_markdown(catalog), check=check)
    return json_ok and markdown_ok, catalog
