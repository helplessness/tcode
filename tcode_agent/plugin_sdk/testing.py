"""Python Plugin SDK v1 可独立使用的静态合同测试工具。"""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import PLUGIN_SDK_VERSION


_SUPPORTED_WORKFLOW_VALIDATORS = frozenset(
    {
        "non_empty",
        "no_failure_signal",
        "json_schema",
        "expected_artifact_coverage",
        "required_artifacts",
        "files_exist",
        "contains_all",
        "contains_any",
        "review_decision",
        "evidence_coverage",
    }
)


@dataclass(frozen=True, slots=True)
class PluginDiagnostic:
    """保存一条不含 Secret 的稳定插件诊断。"""

    level: str
    code: str
    message: str
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        """返回适合 CLI 和注册表存储的诊断字典。"""

        return asdict(self)


def manifest_schema_path() -> Path:
    """返回 SDK 随包发布的 Manifest v2 Schema 路径。"""

    return Path(__file__).with_name("schema") / "plugin-manifest-v2.schema.json"


def load_manifest_schema() -> dict[str, Any]:
    """加载 Manifest v2 Schema，不导入任何插件代码。"""

    return json.loads(manifest_schema_path().read_text(encoding="utf-8"))


def workflow_template_schema_path() -> Path:
    """返回 SDK 随包发布的插件 Workflow 模板 Schema 路径。"""

    return Path(__file__).with_name("schema") / "workflow-template-v1.schema.json"


def load_workflow_template_schema() -> dict[str, Any]:
    """加载声明式 Workflow 模板 Schema，不执行插件入口。"""

    return json.loads(workflow_template_schema_path().read_text(encoding="utf-8"))


def _checked_package_path(
    root: Path,
    raw_path: str,
    *,
    field_name: str,
    errors: list[str],
) -> Path | None:
    """校验 Manifest 引用路径始终位于插件包内。"""

    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        errors.append(f"{field_name}: path must stay inside plugin package")
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        errors.append(f"{field_name}: path escapes plugin package")
        return None
    if not resolved.exists():
        errors.append(f"{field_name}: path does not exist: {raw_path}")
        return None
    return resolved


def _plugin_workflow_paths(
    root: Path,
    entries: Any,
    *,
    errors: list[str],
) -> list[Path]:
    """发现插件声明的 Workflow JSON，并限制路径和模板数量。"""

    if not isinstance(entries, list):
        return []
    discovered: dict[str, Path] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, str):
            continue
        declared = _checked_package_path(
            root,
            entry,
            field_name=f"workflows[{index}]",
            errors=errors,
        )
        if declared is None:
            continue
        if declared.is_file():
            candidates = [declared]
        elif declared.is_dir():
            candidates = [
                path
                for path in declared.rglob("workflow.json")
                if path.is_file()
            ]
            candidates.extend(
                path
                for path in declared.glob("*.json")
                if path.is_file() and path.name != "workflow.json"
            )
        else:
            errors.append(f"workflows[{index}]: must reference a file or directory")
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                errors.append(f"workflows[{index}]: workflow path escapes package")
                continue
            discovered[relative] = resolved
    paths = [discovered[key] for key in sorted(discovered)]
    if entries and not paths:
        errors.append("workflows: declaration contains no workflow templates")
    if len(paths) > 32:
        errors.append("workflows: template count exceeds 32")
        return paths[:32]
    return paths


def _validate_workflow_graph(
    document: dict[str, Any],
    *,
    label: str,
    errors: list[str],
) -> None:
    """校验阶段唯一性、依赖存在性和 DAG 无环约束。"""

    stages = [item for item in document.get("stages", []) if isinstance(item, dict)]
    stage_ids = [str(item.get("stage_id") or "") for item in stages]
    if len(stage_ids) != len(set(stage_ids)):
        errors.append(f"{label}: duplicate workflow stage_id")
        return
    known = set(stage_ids)
    graph: dict[str, list[str]] = {}
    for stage in stages:
        stage_id = str(stage.get("stage_id") or "")
        raw_dependencies = stage.get("depends_on", [])
        dependencies = (
            [str(item) for item in raw_dependencies]
            if isinstance(raw_dependencies, list)
            else []
        )
        graph[stage_id] = dependencies
        for dependency in dependencies:
            if dependency not in known:
                errors.append(
                    f"{label}: dependency stage not found: {stage_id} -> {dependency}"
                )
            if dependency == stage_id:
                errors.append(f"{label}: stage cannot depend on itself: {stage_id}")
        raw_fallbacks = stage.get("fallback_stage_ids", [])
        fallbacks = (
            [str(item) for item in raw_fallbacks]
            if isinstance(raw_fallbacks, list)
            else []
        )
        for fallback in fallbacks:
            if fallback not in known:
                errors.append(
                    f"{label}: fallback stage not found: {stage_id} -> {fallback}"
                )
            if fallback == stage_id:
                errors.append(f"{label}: stage cannot fallback to itself: {stage_id}")
        raw_validators = stage.get("validators", [])
        for validator in raw_validators if isinstance(raw_validators, list) else []:
            if not isinstance(validator, dict):
                continue
            validator_type = str(validator.get("type") or "").strip().lower()
            if validator_type not in _SUPPORTED_WORKFLOW_VALIDATORS:
                errors.append(
                    f"{label}: unsupported workflow validator: "
                    f"{stage_id} -> {validator_type or '(empty)'}"
                )
        metadata = stage.get("metadata")
        if isinstance(metadata, dict) and "human_confirmation" in metadata:
            confirmation = metadata.get("human_confirmation")
            if not isinstance(confirmation, dict):
                errors.append(
                    f"{label}: human_confirmation must be an object: {stage_id}"
                )
                continue
            on_reject = str(confirmation.get("on_reject") or "block").strip()
            fallback_stage_id = str(
                confirmation.get("fallback_stage_id") or ""
            ).strip()
            if on_reject == "fallback" and fallback_stage_id not in known:
                errors.append(
                    f"{label}: human confirmation fallback stage not found: "
                    f"{stage_id} -> {fallback_stage_id or '(empty)'}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(stage_id: str) -> None:
        """深度遍历单个阶段并检测依赖环。"""

        if stage_id in visiting:
            raise ValueError(stage_id)
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dependency in graph.get(stage_id, []):
            if dependency in known:
                _visit(dependency)
        visiting.remove(stage_id)
        visited.add(stage_id)

    try:
        for stage_id in graph:
            _visit(stage_id)
    except ValueError as exc:
        errors.append(f"{label}: workflow dependency cycle detected at {exc}")


def _validate_plugin_workflows(
    root: Path,
    document: dict[str, Any],
    *,
    errors: list[str],
) -> None:
    """静态校验插件 Workflow Schema、命名空间和 Prompt 文件边界。"""

    entries = document.get("workflows") or []
    paths = _plugin_workflow_paths(root, entries, errors=errors)
    if not paths:
        return
    schema = load_workflow_template_schema()
    plugin_name = str(document.get("name") or "")
    local_keys: set[str] = set()
    for path in paths:
        label = path.relative_to(root).as_posix()
        if path.stat().st_size > 512 * 1024:
            errors.append(f"{label}: workflow JSON exceeds 524288 bytes")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{label}: invalid workflow JSON: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{label}: workflow root must be an object")
            continue
        validator = Draft202012Validator(schema)
        for error in sorted(
            validator.iter_errors(payload),
            key=lambda item: list(item.absolute_path),
        ):
            location = ".".join(str(item) for item in error.absolute_path) or "<root>"
            errors.append(f"{label}.{location}: {error.message}")
        local_key = str(payload.get("template_key") or "")
        if local_key in local_keys:
            errors.append(f"{label}: duplicate workflow template_key: {local_key}")
        local_keys.add(local_key)
        if len(f"{plugin_name}.{local_key}") > 96:
            errors.append(f"{label}: namespaced workflow template_key is too long")
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        raw_routing = metadata.get("routing")
        routing = raw_routing if isinstance(raw_routing, dict) else {}
        if bool(routing.get("auto_start")):
            errors.append(f"{label}: plugin workflow routing.auto_start must be false")
        _validate_workflow_graph(payload, label=label, errors=errors)
        template_dir = path.parent
        default_prompt_dir = str(metadata.get("prompt_dir") or "prompts")
        prompt_dir_path = Path(default_prompt_dir)
        if prompt_dir_path.is_absolute() or ".." in prompt_dir_path.parts:
            errors.append(f"{label}: metadata.prompt_dir must stay inside workflow directory")
            default_prompt_dir = "prompts"
        raw_stages = payload.get("stages", [])
        for stage in raw_stages if isinstance(raw_stages, list) else []:
            if not isinstance(stage, dict) or not stage.get("prompt_file"):
                continue
            raw_prompt = Path(str(stage["prompt_file"]))
            if raw_prompt.is_absolute() or ".." in raw_prompt.parts:
                errors.append(f"{label}: prompt_file must stay inside workflow directory")
                continue
            if len(raw_prompt.parts) == 1:
                raw_prompt = Path(default_prompt_dir) / raw_prompt
            prompt_path = (template_dir / raw_prompt).resolve()
            try:
                prompt_path.relative_to(template_dir.resolve())
            except ValueError:
                errors.append(f"{label}: prompt_file escapes workflow directory")
                continue
            if not prompt_path.is_file():
                errors.append(f"{label}: prompt_file does not exist: {raw_prompt}")
            elif prompt_path.stat().st_size > 120_000:
                errors.append(f"{label}: prompt_file exceeds 120000 bytes: {raw_prompt}")


def validate_plugin_package(plugin_root: str | Path) -> list[str]:
    """不导入插件代码，校验 Manifest、Schema 和包内路径。"""

    root = Path(plugin_root).resolve()
    manifest_path = root / "plugin.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"plugin.json: {type(exc).__name__}: {exc}"]
    validator = Draft202012Validator(load_manifest_schema())
    errors = [
        f"{'.'.join(str(item) for item in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(document),
            key=lambda item: list(item.absolute_path),
        )
    ]
    entry = str(document.get("entry") or "").split(":", 1)[0]
    if entry:
        entry_path = _checked_package_path(
            root,
            entry,
            field_name="entry",
            errors=errors,
        )
        if entry_path is not None and not entry_path.is_file():
            errors.append(f"entry: must reference a file: {entry}")
    for field_name in ("commands", "skills", "agents", "mcpServers", "asyncTargets", "workflows"):
        values = document.get(field_name) or []
        if isinstance(values, list):
            for index, value in enumerate(values):
                if isinstance(value, str):
                    _checked_package_path(
                        root,
                        value,
                        field_name=f"{field_name}[{index}]",
                        errors=errors,
                    )
    config_schema = document.get("configSchema")
    if isinstance(config_schema, str) and config_schema:
        config_path = _checked_package_path(
            root,
            config_schema,
            field_name="configSchema",
            errors=errors,
        )
        if config_path is not None and config_path.is_file():
            try:
                config_document = json.loads(config_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(config_document)
            except Exception as exc:
                errors.append(
                    "configSchema: invalid Draft 2020-12 schema: "
                    f"{type(exc).__name__}: {exc}"
                )
    _validate_plugin_workflows(root, document, errors=errors)
    schema = json.loads((Path(__file__).with_name('schema') / 'plugin-async-targets-v1.schema.json').read_text())
    for name in document.get('asyncTargets') or []:
        path = _checked_package_path(root, name, field_name='asyncTargets', errors=errors)
        if path is None:
            continue
        try:
            if path.stat().st_size > 256_000:
                raise ValueError('declaration exceeds 256 KB')
            Draft202012Validator(schema).validate(json.loads(path.read_text()))
        except Exception as exc:
            errors.append(f'asyncTargets: {exc}')
    return errors


def find_private_runtime_imports(python_file: str | Path) -> list[str]:
    """返回插件对 ``tcode_agent`` 私有运行时的静态依赖。"""

    path = Path(python_file)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    return violations


def contract_test_plugin(plugin_root: str | Path) -> list[PluginDiagnostic]:
    """运行 SDK v1 静态合同、语法和私有导入门禁。"""

    root = Path(plugin_root).resolve()
    diagnostics = [
        PluginDiagnostic("error", "package.invalid", message, str(root))
        for message in validate_plugin_package(root)
    ]
    try:
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        entry = str(manifest.get("entry") or "").split(":", 1)[0]
        entry_path = (root / entry).resolve()
        entry_path.relative_to(root)
        compile(
            entry_path.read_text(encoding="utf-8"),
            str(entry_path),
            "exec",
            dont_inherit=True,
        )
        diagnostics.extend(
            PluginDiagnostic(
                "error",
                "import.private_runtime",
                violation,
                str(entry_path),
            )
            for violation in find_private_runtime_imports(entry_path)
        )
    except Exception as exc:
        diagnostics.append(
            PluginDiagnostic(
                "error",
                "entry.compile",
                f"{type(exc).__name__}: {exc}",
                str(root),
            )
        )
    return diagnostics


def doctor_plugin(plugin_root: str | Path) -> dict[str, Any]:
    """返回适合人工审核和机器门禁的脱敏 Doctor 报告。"""

    root = Path(plugin_root).resolve()
    diagnostics = contract_test_plugin(root)
    return {
        "schema_version": 1,
        "sdk_version": PLUGIN_SDK_VERSION,
        "plugin_root": str(root),
        "status": "pass" if not diagnostics else "failed",
        "diagnostics": [item.to_dict() for item in diagnostics],
    }


__all__ = [
    "PluginDiagnostic",
    "contract_test_plugin",
    "doctor_plugin",
    "find_private_runtime_imports",
    "load_manifest_schema",
    "load_workflow_template_schema",
    "manifest_schema_path",
    "validate_plugin_package",
    "workflow_template_schema_path",
]
