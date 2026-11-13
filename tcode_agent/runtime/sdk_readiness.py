"""用于冻结公开 Plugin SDK v1 的只读就绪度审计。"""

from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SDK_V1_READINESS_CONTRACT = "tcode.plugin-sdk-v1.readiness"
SDK_V1_REFERENCE_PLUGIN_TARGET = 1
SDK_V1_INTERFACE_REFERENCE_TARGET = 1
_PUBLIC_SURFACES = (
    "config",
    "events",
    "logger",
    "memory",
    "process",
    "provider",
    "prompt",
    "tools",
    "workspace",
    "workflows",
)
_REFERENCE_REQUIRED_SURFACES = frozenset(
    {"config", "events", "logger", "memory", "provider", "prompt", "tools", "workflows"}
)
_ALLOWED_STATUSES = frozenset({"pass", "partial", "blocked", "not-started"})


@dataclass(frozen=True, slots=True)
class ReadinessGate:
    """描述一个可复现的 SDK 冻结门禁。"""

    id: str
    title: str
    status: str
    required_for_freeze: bool
    summary: str
    evidence: tuple[str, ...]
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """校验状态并返回可序列化门禁。"""

        if self.status not in _ALLOWED_STATUSES:
            raise ValueError(f"unsupported SDK readiness status: {self.status}")
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        payload["blockers"] = list(self.blockers)
        return payload


def _private_runtime_imports(path: Path) -> list[str]:
    """静态列出插件入口对私有运行时的导入。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"parse-error:{type(exc).__name__}"]
    violations: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        for name in names:
            if name == "tcode_agent" or (
                name.startswith("tcode_agent.")
                and not name.startswith("tcode_agent.plugin_sdk")
            ):
                violations.append(f"line {node.lineno}: {name}")
    return sorted(set(violations))


def _used_public_surfaces(document: dict[str, Any], entry_path: Path) -> list[str]:
    """合并 Manifest 声明和入口代码实际使用的公开接口。"""

    used = {
        str(item).strip()
        for item in document.get("contributes", [])
        if str(item).strip() in _PUBLIC_SURFACES
    }
    if document.get("configSchema"):
        used.add("config")
    if document.get("workflows"):
        used.add("workflows")
    try:
        tree = ast.parse(entry_path.read_text(encoding="utf-8"), filename=str(entry_path))
    except (OSError, SyntaxError):
        return sorted(used)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _PUBLIC_SURFACES:
            used.add(node.attr)
        if isinstance(node, ast.ImportFrom) and node.module == "tcode_agent.plugin_sdk":
            if any(alias.name.startswith("Provider") for alias in node.names):
                used.add("provider")
    return sorted(used)


def scan_sdk_reference_plugins(repo_root: str | Path | None = None) -> list[dict[str, Any]]:
    """不导入、不执行插件代码地扫描仓库参考插件。"""

    root = Path(repo_root).resolve() if repo_root else Path(__file__).resolve().parents[2]
    plugins_root = root / "plugins"
    results: list[dict[str, Any]] = []
    for manifest_path in sorted(plugins_root.glob("*/plugin.json")):
        relative_manifest = manifest_path.relative_to(root).as_posix()
        errors: list[str] = []
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "name": manifest_path.parent.name,
                    "manifest": relative_manifest,
                    "entry": "",
                    "declared_contributions": [],
                    "used_public_surfaces": [],
                    "private_runtime_imports": [],
                    "package_errors": [f"plugin.json:{type(exc).__name__}"],
                    "public_sdk_only": False,
                    "reference_qualified": False,
                }
            )
            continue

        entry = str(document.get("entry") or "").split(":", 1)[0].strip()
        entry_path = manifest_path.parent / entry
        try:
            relative_entry = entry_path.relative_to(root).as_posix()
        except ValueError:
            relative_entry = ""
            errors.append("entry:path-escapes-repository")
        if not entry or not entry_path.is_file():
            errors.append("entry:file-not-found")
        private_imports = _private_runtime_imports(entry_path) if entry_path.is_file() else []
        contributions = sorted(
            {
                str(item).strip()
                for item in document.get("contributes", [])
                if str(item).strip()
            }
        )
        surfaces = _used_public_surfaces(document, entry_path) if entry_path.is_file() else []
        public_sdk_only = not errors and not private_imports
        results.append(
            {
                "name": str(document.get("name") or manifest_path.parent.name),
                "manifest": relative_manifest,
                "entry": relative_entry,
                "declared_contributions": contributions,
                "used_public_surfaces": surfaces,
                "private_runtime_imports": private_imports,
                "package_errors": errors,
                "public_sdk_only": public_sdk_only,
                "reference_qualified": bool(public_sdk_only and contributions),
            }
        )
    return results


def _static_gates() -> tuple[ReadinessGate, ...]:
    """返回不依赖参考插件数量的稳定冻结门禁。"""

    return (
        ReadinessGate(
            id="single_track_runtime",
            title="Tool、Prompt、Event 单轨运行时",
            status="pass",
            required_for_freeze=True,
            summary="Tool、Prompt 与 Event 保持单轨，SDK v1 宿主准入在插件导入前完成。",
            evidence=(
                "tcode_agent/runtime/plugin_admission.py",
                "tcode_agent/runtime/tool_pipeline.py",
                "tcode_agent/prompt/pipeline.py",
                "tcode_agent/plugins/hooks.py",
                "tests/test_plugin_context_v1.py",
            ),
        ),
        ReadinessGate(
            id="contract_test_and_doctor",
            title="Contract Test 与 Plugin Doctor",
            status="pass",
            required_for_freeze=True,
            summary="SDK v1 已提供 validate、test、doctor、脱敏诊断和独立合同 Fixture。",
            evidence=(
                "tcode_agent/plugin_sdk/testing.py",
                "tcode_agent/plugins/api.py",
                "tcode_agent/plugin_sdk/cli.py",
                "tests/test_plugin_sdk_v1.py",
            ),
        ),
        ReadinessGate(
            id="job_owner_cursor",
            title="Job Owner、Cursor 与取消结算",
            status="pass",
            required_for_freeze=False,
            summary="TaskRuntime Job Provider 已统一 Owner、单调 Cursor、脱敏诊断和失败关闭的取消结算。",
            evidence=(
                "tcode_agent/runtime/jobs.py",
                "tcode_agent/server/job_runtime_api.py",
                "tests/test_stage4_protocol_contracts.py",
            ),
        ),
        ReadinessGate(
            id="lifecycle_ownership",
            title="生命周期、事务与精确所有权",
            status="pass",
            required_for_freeze=True,
            summary="实例 Scope、事务发布、失败回滚、精确 disposer 和循环测试已落地。",
            evidence=(
                "tcode_agent/runtime/effects.py",
                "tcode_agent/plugins/manager.py",
                "tests/test_plugin_load_transaction.py",
            ),
        ),
        ReadinessGate(
            id="manifest_context_boundary",
            title="Manifest、Context 与权限边界",
            status="pass",
            required_for_freeze=True,
            summary="Manifest v2 单轨、导入前校验、受控 Context 和 Host 授权边界已实现。",
            evidence=(
                "tcode_agent/plugin_sdk/schema/plugin-manifest-v2.schema.json",
                "tcode_agent/plugins/context.py",
                "tests/test_plugin_manifest_v2.py",
            ),
        ),
        ReadinessGate(
            id="prompt_production_contract",
            title="Prompt 生产契约",
            status="pass",
            required_for_freeze=True,
            summary="通过准入的 Prompt contribution 已进入有预算、稳定排序的唯一生产组装管线。",
            evidence=(
                "tcode_agent/prompt/registry.py",
                "tcode_agent/prompt/pipeline.py",
                "tcode_agent/harness/prompt_builder.py",
                "tests/test_prompt_builder_context_budget.py",
            ),
        ),
        ReadinessGate(
            id="provider_compatibility_matrix",
            title="Provider 兼容与暴露矩阵",
            status="pass",
            required_for_freeze=True,
            summary="SDK v1 冻结窄 Provider DTO/Protocol，8 类内部 Provider 仍保持宿主策略边界。",
            evidence=(
                "tcode_agent/plugins/contracts/plugin_api_v1.json",
                "tcode_agent/runtime/provider_api.py",
                "tcode_agent/plugins/contracts/provider_api_internal_v1.json",
                "tests/test_provider_api_internal.py",
            ),
        ),
        ReadinessGate(
            id="public_api_stability",
            title="公共 API 稳定性",
            status="pass",
            required_for_freeze=True,
            summary="SDK v1 export、Manifest、Tool/Prompt/Event/Provider DTO 和精确签名快照已冻结。",
            evidence=(
                "tcode_agent/plugin_sdk/__init__.py",
                "tcode_agent/plugin_sdk/contracts.py",
                "tcode_agent/plugins/contracts/plugin_api_v1.json",
                "tests/test_plugin_sdk_v1.py",
            ),
        ),
        ReadinessGate(
            id="release_support_policy",
            title="发行、支持与弃用政策",
            status="pass",
            required_for_freeze=True,
            summary="已明确 v1.x 语义化版本、安全修复、弃用和两个稳定发布观察政策。",
            evidence=(
                "docs/plugin-sdk/README.md",
                "docs/plugin-sdk/05-测试打包发布与安装.md",
                "docs/plugin-sdk/06-故障排查与发布检查表.md",
            ),
        ),
        ReadinessGate(
            id="session_event_projection",
            title="Session Event 与 Projection",
            status="pass",
            required_for_freeze=False,
            summary="Session Event v1 已成为追加式权威来源，SQLite 作为可重建 Projection，并具备重放、恢复和保留审计链的 Compaction。",
            evidence=(
                "tcode_agent/session/events.py",
                "tcode_agent/session/store_sqlite.py",
                "tests/test_stage2_reliability.py",
            ),
        ),
        ReadinessGate(
            id="telemetry_redaction_backpressure",
            title="Telemetry 脱敏与反压",
            status="pass",
            required_for_freeze=False,
            summary="所有遥测在 exporter 前统一脱敏，并通过有界非阻塞队列批量导出；队列满时只丢遥测，不拖慢任务。",
            evidence=(
                "tcode_agent/runtime/telemetry.py",
                "tcode_agent/runtime/artifacts.py",
                "tests/test_stage2_reliability.py",
                "tcode_agent/plugins/contracts/provider_api_internal_v1.json",
            ),
        ),
        ReadinessGate(
            id="tool_pipeline_single_track",
            title="Tool Definition 与统一执行流水线",
            status="pass",
            required_for_freeze=True,
            summary="工具定义、参数验证、Policy、Permission、Sandbox、Body 与 Finalize 已收敛到唯一顺序管线。",
            evidence=(
                "tcode_agent/runtime/tool_pipeline.py",
                "tcode_agent/runtime/tool_arguments.py",
                "tcode_agent/harness/tool_executor.py",
                "tcode_agent/plugins/contributions.py",
                "tests/test_plugin_sdk_v1.py",
                "scripts/dsh.py",
            ),
        ),
        ReadinessGate(
            id="trusted_release_scope",
            title="Trusted 首发范围与未知插件拒绝",
            status="pass",
            required_for_freeze=True,
            summary="官方路径或宿主精确授权才能进入主进程，Manifest 不能自授信任。",
            evidence=(
                "tcode_agent/plugins/contracts/plugin_api_v1.json",
                "tcode_agent/runtime/plugin_admission.py",
                "tests/test_plugin_container_broker.py",
            ),
        ),
        ReadinessGate(
            id="community_registry_supply_chain",
            title="自有社区注册表与供应链",
            status="pass",
            required_for_freeze=True,
            summary="注册表已固定版本、SDK/Python 范围、SHA-256、Ed25519、权限与审核/撤销状态。",
            evidence=(
                "tcode_agent/plugin_sdk/community.py",
                "tcode_agent/plugin_sdk/package.py",
                "tests/test_plugin_sdk_v1.py",
            ),
        ),
        ReadinessGate(
            id="isolated_community_worker",
            title="未知社区插件隔离 Worker",
            status="pass",
            required_for_freeze=True,
            summary="未知代码默认进入独立容器，受控能力只能经 Host Broker 重新进入权限、Tool 与 Sandbox 管线。",
            evidence=(
                "tcode_agent/plugins/isolation.py",
                "tcode_agent/plugins/capability_broker.py",
                "tcode_agent/plugin_sdk/worker.py",
                "tests/test_plugin_container_broker.py",
            ),
        ),
        ReadinessGate(
            id="final_legacy_cleanup",
            title="最终旧路径清理",
            status="pass",
            required_for_freeze=False,
            summary="经用户明确接受提前执行风险，Legacy Service、Provider Preview 命名和 Tracker 旧 JSON 回灌已删除；运行时只保留当前原生路径。",
            evidence=(
                "tcode_agent/runtime/service_providers.py",
                "tcode_agent/runtime/provider_api.py",
                "tcode_agent/runtime/shared_tracker.py",
                "tests/test_stage4_protocol_contracts.py",
            ),
        ),
        ReadinessGate(
            id="stable_release_observation",
            title="两个稳定发布观察窗口",
            status="blocked",
            required_for_freeze=False,
            summary="真实两个稳定发布周期仍需经过实际时间和线上观察；本门禁不再阻塞已获明确授权的提前清理。",
            evidence=(
                "docs/plugin-sdk/05-测试打包发布与安装.md",
                "docs/plugin-sdk/06-故障排查与发布检查表.md",
            ),
            blockers=("尚未经历两个真实稳定发布窗口，线上稳定性仍需独立观察",),
        ),
    )


def plugin_sdk_v1_readiness_document(
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """返回确定性的仓库 SDK 冻结就绪度报告。"""

    scanned = scan_sdk_reference_plugins(repo_root)
    qualified = [item for item in scanned if item["reference_qualified"]]
    coverage = {
        surface: sum(surface in item["used_public_surfaces"] for item in qualified)
        for surface in _PUBLIC_SURFACES
    }
    coverage_deficits = {
        surface: SDK_V1_INTERFACE_REFERENCE_TARGET - count
        for surface, count in coverage.items()
        if (
            surface in _REFERENCE_REQUIRED_SURFACES
            and count < SDK_V1_INTERFACE_REFERENCE_TARGET
        )
    }
    reference_ready = (
        len(qualified) >= SDK_V1_REFERENCE_PLUGIN_TARGET and not coverage_deficits
    )
    reference_gate = ReadinessGate(
        id="official_reference_coverage",
        title="官方公共 SDK 参考插件覆盖",
        status="pass" if reference_ready else "blocked",
        required_for_freeze=True,
        summary=(
            f"发现 {len(qualified)} 个只使用公共 SDK 且声明 Contribution 的参考插件；"
            f"目标至少 {SDK_V1_REFERENCE_PLUGIN_TARGET} 个，每个核心参考接口至少由 "
            f"{SDK_V1_INTERFACE_REFERENCE_TARGET} 个参考插件使用。"
        ),
        evidence=tuple(item["manifest"] for item in qualified)
        or ("plugins/example_plugin/plugin.json",),
        blockers=()
        if reference_ready
        else (
            f"参考插件还缺 {max(0, SDK_V1_REFERENCE_PLUGIN_TARGET - len(qualified))} 个",
            "接口覆盖不足: "
            + ", ".join(
                f"{surface} 缺 {deficit}"
                for surface, deficit in sorted(coverage_deficits.items())
            ),
        ),
    )
    gates = sorted((*_static_gates(), reference_gate), key=lambda item: item.id)
    required_blockers = [
        item.id
        for item in gates
        if item.required_for_freeze and item.status != "pass"
    ]
    status_counts = Counter(item.status for item in gates)
    ready = not required_blockers
    return {
        "schema_version": 1,
        "contract_id": SDK_V1_READINESS_CONTRACT,
        "audit_date": "2026-08-21",
        "audit_scope": "repository-static-readiness",
        "ready_to_freeze_v1": ready,
        "decision": "freeze-v1" if ready else "keep-v0-internal-experimental",
        "status": "pass" if ready else "blocked",
        "production_behavior_changed": True,
        "release_scope": "official-or-explicit-trusted-host;unknown-isolated-worker",
        "unknown_community_plugin_policy": "container-worker-explicit-capability-grants",
        "worker_protocol": "tcode.plugin-worker.v2",
        "final_legacy_cleanup_ready": True,
        "final_cleanup_execution": {
            "status": "completed-early",
            "authorization": "explicit-user-risk-acceptance",
            "observation_requirement_overridden": True,
            "stable_release_observation_completed": False,
        },
        "targets": {
            "official_reference_plugins": SDK_V1_REFERENCE_PLUGIN_TARGET,
            "references_per_frozen_interface": SDK_V1_INTERFACE_REFERENCE_TARGET,
            "public_surfaces": list(_PUBLIC_SURFACES),
        },
        "reference_plugins": {
            "scanned_count": len(scanned),
            "qualified_count": len(qualified),
            "qualified_names": [item["name"] for item in qualified],
            "surface_coverage": coverage,
            "surface_deficits": coverage_deficits,
            "packages": scanned,
        },
        "summary": {
            "gate_count": len(gates),
            "pass": status_counts["pass"],
            "partial": status_counts["partial"],
            "blocked": status_counts["blocked"],
            "not_started": status_counts["not-started"],
            "required_blocker_count": len(required_blockers),
        },
        "required_blockers": required_blockers,
        "gates": [item.to_dict() for item in gates],
    }


__all__ = [
    "SDK_V1_INTERFACE_REFERENCE_TARGET",
    "SDK_V1_READINESS_CONTRACT",
    "SDK_V1_REFERENCE_PLUGIN_TARGET",
    "ReadinessGate",
    "plugin_sdk_v1_readiness_document",
    "scan_sdk_reference_plugins",
]
