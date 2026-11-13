#!/usr/bin/env python3
"""仅通过静态源码生成 Tool Catalog，不导入任何应用模块。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA = "tcode.tool-catalog.v1"
SCHEMA_VERSION = 1
GENERATOR_VERSION = 2
DEFAULT_JSON_PATH = Path("build/dsh/tool-catalog.json")
DEFAULT_MARKDOWN_PATH = Path("build/dsh/tool-catalog.md")
STATIC_SOURCE_ROOTS = (Path("tcode_agent"), Path("plugins"))

CAPABILITY_DEFAULTS: dict[str, Any] = {
    "side_effect_level": "medium",
    "supports_async_submit": False,
    "supports_resume": False,
    "supports_review": False,
    "artifact_contract": "none",
    "concurrency_safe": True,
    "interrupt_behavior": "best_effort",
    "result_budget_tokens": 0,
    "preferred_profiles": [],
    "tags": ["general"],
}

REGISTRY_POLICY_NAMES = {
    "_hidden_from_definitions": "hidden",
    "_profile_scoped_from_definitions": "profile-scoped",
    "_protein_tool_ids": "protein-scoped",
    "_globally_safe_tool_ids": "globally-safe",
    "_team_tool_ids": "team-scoped",
    "_evidence_tool_ids": "evidence-scoped",
}

CAPABILITY_ONLY_CLASSIFICATIONS: dict[str, dict[str, Any]] = {
    "process_runtime": {
        "classification": "internal-runtime-component",
        "owners": ["bash", "exec", "process", "skill_run"],
        "decision": "retain-not-model-visible",
    },
    "todo_read": {
        "classification": "permission-surface",
        "owners": ["todo"],
        "decision": "retain-not-model-visible",
    },
    "todo_write": {
        "classification": "permission-surface",
        "owners": ["todo"],
        "decision": "retain-not-model-visible",
    },
}


def _repo_root() -> Path:
    """返回当前脚本所属仓库的绝对根目录。"""

    return Path(__file__).resolve().parents[2]


def _relative(path: Path, root: Path) -> str:
    """把源码路径规范化为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(root.resolve()).as_posix()


def _attribute_name(node: ast.AST) -> str:
    """将名称或属性访问转换为稳定的点分表达式。"""

    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _expression_text(node: ast.AST | None) -> str:
    """返回只用于诊断的稳定 AST 表达式文本。"""

    if node is None:
        return "<missing>"
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _literal(node: ast.AST | None, default: Any = None) -> Any:
    """安全读取 Python 字面量，动态表达式返回指定默认值。"""

    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return default


def _literal_text(node: ast.AST | None) -> str | None:
    """读取非空静态字符串并保留原始大小写。"""

    value = _literal(node)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    """读取调用中指定名称的关键字参数。"""

    for item in call.keywords:
        if item.arg == name:
            return item.value
    return None


def _call_argument(call: ast.Call, position: int, keyword: str) -> ast.AST | None:
    """按关键字优先、位置参数兜底读取调用参数。"""

    keyword_value = _keyword(call, keyword)
    if keyword_value is not None:
        return keyword_value
    if len(call.args) > position:
        return call.args[position]
    return None


def _iter_python_files(root: Path, roots: Sequence[Path]) -> Iterable[Path]:
    """按稳定顺序枚举指定源码根中的 Python 文件。"""

    for relative_root in roots:
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _parse_source(path: Path) -> tuple[str, ast.Module]:
    """读取并解析源码，语法错误转为带路径的确定性失败。"""

    source = path.read_text(encoding="utf-8")
    try:
        return source, ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"cannot parse {path}: {exc}") from exc


def _find_assignment(tree: ast.Module | ast.ClassDef, name: str) -> ast.AST:
    """在模块或类的直接语句中查找指定静态赋值。"""

    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return node.value
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
    raise RuntimeError(f"required static assignment not found: {name}")


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    """在模块顶层查找指定类定义。"""

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise RuntimeError(f"required class not found: {name}")


def _find_method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """在类的直接成员中查找同步或异步方法。"""

    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _find_inherited_member(
    tree: ast.Module,
    class_node: ast.ClassDef,
    name: str,
    *,
    visited: set[str] | None = None,
) -> ast.AST | None:
    """查找类属性或方法，尊重当前类对同模块父类的覆盖。"""

    try:
        return _find_assignment(class_node, name)
    except RuntimeError:
        pass

    method = _find_method(class_node, name)
    if method is not None:
        return method
    seen = set(visited or ())
    if class_node.name in seen:
        return None
    seen.add(class_node.name)
    for base in class_node.bases:
        base_name = _attribute_name(base).split(".")[-1]
        if not base_name or base_name in seen:
            continue
        try:
            base_class = _find_class(tree, base_name)
        except RuntimeError:
            continue
        inherited = _find_inherited_member(
            tree,
            base_class,
            name,
            visited=seen,
        )
        if inherited is not None:
            return inherited
    return None


class _ReturnCollector(ast.NodeVisitor):
    """收集单个方法内的返回值，同时跳过嵌套函数和类。"""

    def __init__(self) -> None:
        """初始化空返回值列表。"""

        self.values: list[ast.AST | None] = []

    def visit_Return(self, node: ast.Return) -> None:
        """记录当前方法的返回表达式。"""

        self.values.append(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """跳过当前方法内部定义的嵌套同步函数。"""

        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """跳过当前方法内部定义的嵌套异步函数。"""

        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """跳过当前方法内部定义的嵌套类。"""

        return None


def _method_returns(method: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[ast.AST | None]:
    """收集方法自身控制流中的全部返回表达式。"""

    if method is None:
        return []
    collector = _ReturnCollector()
    for statement in method.body:
        collector.visit(statement)
    return collector.values


def _single_static_return(method: ast.AST | None) -> Any:
    """仅在方法所有返回值均为同一字面量时返回该值。"""

    if method is not None and not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _literal(method)
    returns = _method_returns(method)
    if not returns:
        return None
    values: list[Any] = []
    for value_node in returns:
        sentinel = object()
        value = _literal(value_node, sentinel)
        if value is sentinel:
            return None
        values.append(value)
    first = values[0]
    return first if all(item == first for item in values) else None


def _return_expression(method: ast.AST | None) -> str:
    """返回单一返回表达式的文本，多个分支时标记为动态。"""

    if method is not None and not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _expression_text(method)
    returns = _method_returns(method)
    if len(returns) != 1:
        return "<dynamic>"
    return _expression_text(returns[0])


def _tool_member(tree: ast.Module, class_node: ast.ClassDef, name: str, *, visited: frozenset[str] = frozenset()) -> ast.AST | None:
    """解析简单的 self 属性转发，绝不执行调用或动态表达式。"""

    if name in visited:
        return None
    member = _find_inherited_member(tree, class_node, name)
    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
        body = member.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if len(body) == 1 and isinstance(body[0], ast.Return):
            value = body[0].value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
                return _tool_member(tree, class_node, value.attr, visited=visited | {name})
    return member


def _resolve_imports(path: Path, tree: ast.Module) -> dict[str, Path]:
    """解析聚合模块中的相对 class import 到具体源码文件。"""

    imports: dict[str, Path] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level < 1 or not node.module:
            continue
        base = path.parent
        for _ in range(max(0, node.level - 1)):
            base = base.parent
        module_path = base / Path(*node.module.split("."))
        source_path = module_path.with_suffix(".py")
        for alias in node.names:
            imports[alias.asname or alias.name] = source_path
    return imports


def _name_list(node: ast.AST) -> list[str]:
    """从静态列表或列表加法表达式中提取 class 名称。"""

    if isinstance(node, (ast.List, ast.Tuple)):
        return [item.id for item in node.elts if isinstance(item, ast.Name)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _name_list(node.left) + _name_list(node.right)
    return []


def _aggregate_tool_classes(path: Path) -> list[tuple[str, Path, bool]]:
    """解析 builtin/team 聚合模块的固定工具类和可选工具标记。"""

    _, tree = _parse_source(path)
    imports = _resolve_imports(path, tree)
    fixed_names = _name_list(_find_assignment(tree, "_TOOLS"))
    optional_names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            _attribute_name(node.func.value) == "_OPTIONAL_TOOLS"
            and node.func.attr == "append"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            optional_names.append(node.args[0].id)
    ordered_names = list(dict.fromkeys([*fixed_names, *optional_names]))
    results: list[tuple[str, Path, bool]] = []
    for class_name in ordered_names:
        source_path = imports.get(class_name)
        if source_path is None or not source_path.is_file():
            raise RuntimeError(f"cannot resolve {class_name} imported by {path}")
        results.append((class_name, source_path, class_name in optional_names))
    return results


def _parse_string_set(class_node: ast.ClassDef, name: str) -> set[str]:
    """读取 ToolRegistry 类中的静态字符串集合。"""

    value = _literal(_find_assignment(class_node, name))
    if not isinstance(value, (set, frozenset, list, tuple)):
        raise RuntimeError(f"{name} must remain a static string collection")
    normalized = {str(item).strip() for item in value if str(item).strip()}
    if len(normalized) != len(value):
        raise RuntimeError(f"{name} contains empty or duplicate values")
    return normalized


def _parse_registry_policies(root: Path) -> dict[str, set[str]]:
    """从 ToolRegistry 静态集合读取模型可见性和安全分组。"""

    path = root / "tcode_agent/tools/registry.py"
    _, tree = _parse_source(path)
    registry = _find_class(tree, "ToolRegistry")
    return {
        policy: _parse_string_set(registry, source_name)
        for source_name, policy in REGISTRY_POLICY_NAMES.items()
    }


def _parse_capability_call(call: ast.Call) -> dict[str, Any]:
    """把静态 ToolCapability 调用转换为机器字段。"""

    capability = dict(CAPABILITY_DEFAULTS)
    positional_fields = [
        "tool_id",
        "side_effect_level",
        "supports_async_submit",
        "supports_resume",
        "supports_review",
        "artifact_contract",
        "concurrency_safe",
        "interrupt_behavior",
        "result_budget_tokens",
        "preferred_profiles",
        "tags",
    ]
    for index, node in enumerate(call.args):
        if index >= len(positional_fields):
            break
        value = _literal(node)
        if value is not None:
            capability[positional_fields[index]] = value
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        value = _literal(keyword.value)
        if value is not None:
            capability[keyword.arg] = value
    for key in ("preferred_profiles", "tags"):
        capability[key] = list(capability.get(key) or [])
    capability.pop("tool_id", None)
    return capability


def _parse_capability_map(root: Path) -> dict[str, dict[str, Any]]:
    """从 `_CAPABILITY_MAP` 读取显式工具能力元数据。"""

    path = root / "tcode_agent/tools/capabilities.py"
    _, tree = _parse_source(path)
    mapping = _find_assignment(tree, "_CAPABILITY_MAP")
    if not isinstance(mapping, ast.Dict):
        raise RuntimeError("_CAPABILITY_MAP must remain a static dict")
    result: dict[str, dict[str, Any]] = {}
    for key_node, value_node in zip(mapping.keys, mapping.values):
        tool_id = _literal_text(key_node)
        if not tool_id or not isinstance(value_node, ast.Call):
            raise RuntimeError("_CAPABILITY_MAP contains a non-static entry")
        if _attribute_name(value_node.func).split(".")[-1] != "ToolCapability":
            raise RuntimeError(f"unsupported capability entry: {tool_id}")
        if tool_id in result:
            raise RuntimeError(f"duplicate capability entry: {tool_id}")
        result[tool_id] = _parse_capability_call(value_node)
    return result


def _visibility(tool_id: str, policies: dict[str, set[str]]) -> dict[str, Any]:
    """根据 ToolRegistry 静态集合解释工具的模型定义可见性。"""

    groups = sorted(
        policy for policy, members in policies.items() if tool_id in members
    )
    if "hidden" in groups:
        mode = "hidden"
    elif tool_id == "browser":
        mode = "browser-agent-only"
    elif any(group.endswith("-scoped") for group in groups):
        mode = "context-scoped"
    else:
        mode = "default-visible"
    return {"mode": mode, "registry_groups": groups}


def _class_tool_record(
    *,
    root: Path,
    path: Path,
    class_name: str,
    provider_kind: str,
    provider_id: str,
    registration: str,
    optional: bool,
    capability_map: dict[str, dict[str, Any]],
    policies: dict[str, set[str]],
    plugin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从一个静态 Tool 类构造不执行应用代码的目录记录。"""

    _, tree = _parse_source(path)
    class_node = _find_class(tree, class_name)
    tool_id = _single_static_return(_tool_member(tree, class_node, "id"))
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise RuntimeError(f"{path}:{class_node.lineno} {class_name}.id is not static")
    tool_id = tool_id.strip()
    description = _single_static_return(
        _tool_member(tree, class_node, "description")
    )
    parameters = _return_expression(
        _tool_member(tree, class_node, "parameters")
    )
    dependencies = _single_static_return(
        _tool_member(tree, class_node, "permission_dependencies")
    )
    if dependencies is None:
        dependencies = ()
    if not isinstance(dependencies, (tuple, list)):
        raise RuntimeError(f"{class_name}.permission_dependencies is not static")
    effective_capability = dict(
        capability_map.get(tool_id, CAPABILITY_DEFAULTS)
    )
    record: dict[str, Any] = {
        "tool_id": tool_id,
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "registration": registration,
        "optional": bool(optional),
        "class_name": class_name,
        "source": {
            "file": _relative(path, root),
            "line": class_node.lineno,
        },
        "description": {
            "mode": "static" if isinstance(description, str) else "dynamic",
            "text": description if isinstance(description, str) else "",
        },
        "parameters": {"schema_expression": parameters},
        "permission_dependencies": [str(item) for item in dependencies],
        "capability": {
            "source": "explicit-map" if tool_id in capability_map else "default-fallback",
            **effective_capability,
        },
        "visibility": _visibility(tool_id, policies),
    }
    if plugin is not None:
        record["plugin"] = plugin
    return record


def _manifest_metadata(path: Path, root: Path) -> dict[str, Any]:
    """读取仓库插件 Manifest 中与 Tool Catalog 有关的非敏感字段。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"plugin manifest is not an object: {path}")
    return {
        "name": str(payload.get("name") or "").strip(),
        "version": str(payload.get("version") or "").strip(),
        "api_version": str(payload.get("apiVersion") or "").strip(),
        "requested_trust": str(payload.get("requestedTrust") or "").strip(),
        "manifest": _relative(path, root),
    }


def _returned_tool_classes(method: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """从 legacy `get_tools()` 的静态返回列表提取工具类名称。"""

    returns = _method_returns(method)
    if len(returns) != 1 or not isinstance(returns[0], (ast.List, ast.Tuple)):
        return []
    names: list[str] = []
    for item in returns[0].elts:
        if not isinstance(item, ast.Call):
            return []
        name = _attribute_name(item.func).split(".")[-1]
        if not name:
            return []
        names.append(name)
    return names


def _legacy_plugin_records(
    root: Path,
    capability_map: dict[str, dict[str, Any]],
    policies: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], set[Path]]:
    """枚举仓库 legacy 插件 `get_tools()` 中的静态工具。"""

    records: list[dict[str, Any]] = []
    sources: set[Path] = set()
    for manifest_path in sorted((root / "plugins").glob("*/plugin.json")):
        plugin_path = manifest_path.parent / "plugin.py"
        if not plugin_path.is_file():
            continue
        _, tree = _parse_source(plugin_path)
        plugin_meta = _manifest_metadata(manifest_path, root)
        for class_node in tree.body:
            if not isinstance(class_node, ast.ClassDef):
                continue
            method = _find_method(class_node, "get_tools")
            if method is None:
                continue
            class_names = _returned_tool_classes(method)
            if not class_names:
                continue
            for tool_class in class_names:
                records.append(
                    _class_tool_record(
                        root=root,
                        path=plugin_path,
                        class_name=tool_class,
                        provider_kind="repository-plugin-legacy",
                        provider_id=f"plugin:{plugin_meta['name']}",
                        registration="legacy-get-tools",
                        optional=True,
                        capability_map=capability_map,
                        policies=policies,
                        plugin=plugin_meta,
                    )
                )
            sources.update({manifest_path, plugin_path})
    return records, sources


def _sdk_definition_call(node: ast.Call) -> ast.Call | None:
    """识别直接或先聚合后注册的静态 ``ToolDefinition`` 声明。"""

    if _attribute_name(node.func).split(".")[-1] == "ToolDefinition":
        return node
    return None


def _sdk_capability(
    tool_id: str,
    declared_side_effect: str,
    result_budget: int,
) -> dict[str, Any]:
    """复现 ``SDKToolAdapter`` 对公开副作用声明的生产投影。"""

    readonly = declared_side_effect in {"none", "read"}
    process = declared_side_effect == "process"
    tags = ["plugin", "read_only"] if readonly else ["plugin", "write"]
    if process:
        tags = ["plugin", "shell", "sandbox_required"]
    return {
        "side_effect_level": "none" if readonly else "high",
        "supports_async_submit": False,
        "supports_resume": False,
        "supports_review": False,
        "artifact_contract": "none",
        "concurrency_safe": readonly,
        "interrupt_behavior": "cancel_process" if process else "best_effort",
        "result_budget_tokens": int(result_budget),
        "preferred_profiles": [],
        "tags": tags,
        "tool_id": tool_id,
    }


def _sdk_plugin_records(
    root: Path,
    capability_map: dict[str, dict[str, Any]],
    policies: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], set[Path], list[dict[str, Any]]]:
    """枚举仓库公共 Plugin SDK 的静态 ToolDefinition 贡献。"""

    records: list[dict[str, Any]] = []
    sources: set[Path] = set()
    dynamic_sites: list[dict[str, Any]] = []
    for manifest_path in sorted((root / "plugins").glob("*/plugin.json")):
        plugin_path = manifest_path.parent / "plugin.py"
        if not plugin_path.is_file():
            continue
        _, tree = _parse_source(plugin_path)
        plugin_meta = _manifest_metadata(manifest_path, root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            definition = _sdk_definition_call(node)
            if definition is None:
                continue
            name_node = _call_argument(definition, 0, "name")
            description_node = _call_argument(definition, 1, "description")
            tool_id = _literal_text(name_node)
            description = _literal_text(description_node)
            sources.update({manifest_path, plugin_path})
            if not tool_id or description is None:
                # 不导入插件求值，也不把无法解析的工具伪装成静态契约。
                dynamic_sites.append({
                    "api": _attribute_name(definition.func),
                    "expression": _expression_text(name_node if not tool_id else description_node),
                    "file": _relative(plugin_path, root),
                    "line": node.lineno,
                })
                continue
            version = _literal_text(_keyword(definition, "version")) or "1.0.0"
            declared_side_effect = (
                _literal_text(_keyword(definition, "side_effect_level")) or "none"
            )
            result_budget = _literal(_keyword(definition, "result_budget_tokens"), 2048)
            effective_capability = _sdk_capability(
                tool_id,
                declared_side_effect,
                int(result_budget),
            )
            records.append(
                {
                    "tool_id": tool_id,
                    "provider_kind": "plugin-sdk-v1",
                    "provider_id": f"plugin:{plugin_meta['name']}",
                    "registration": "sdk-contribution-buffer",
                    "optional": True,
                    "class_name": "ToolDefinition",
                    "source": {
                        "file": _relative(plugin_path, root),
                        "line": node.lineno,
                    },
                    "description": {"mode": "static", "text": description},
                    "parameters": {"schema_expression": "inline-json-schema"},
                    "permission_dependencies": [],
                    "declared_contract": {
                        "version": version,
                        "side_effect_level": declared_side_effect,
                        "result_budget_tokens": int(result_budget),
                    },
                    "capability": {
                        "source": "sdk-declared",
                        **effective_capability,
                    },
                    "visibility": _visibility(tool_id, policies),
                    "plugin": plugin_meta,
                }
            )
    return records, sources, dynamic_sites


def _dynamic_registration_sites(root: Path) -> list[dict[str, Any]]:
    """记录无法静态枚举具体 Tool ID 的运行时注册调用点。"""

    sites: list[dict[str, Any]] = []
    for path in _iter_python_files(root, STATIC_SOURCE_ROOTS):
        _, tree = _parse_source(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            api = _attribute_name(node.func)
            if not api.endswith("ToolRegistry.register_custom"):
                continue
            expression = _expression_text(node.args[0] if node.args else None)
            sites.append(
                {
                    "api": api,
                    "expression": expression,
                    "file": _relative(path, root),
                    "line": node.lineno,
                }
            )
    return sorted(
        sites,
        key=lambda item: (item["file"], item["line"], item["api"]),
    )


def _source_digest(root: Path, paths: Iterable[Path]) -> tuple[str, list[str]]:
    """计算参与目录构建的仓库相对文件清单和内容摘要。"""

    unique_paths = sorted({path.resolve() for path in paths})
    digest = hashlib.sha256()
    relative_paths: list[str] = []
    for path in unique_paths:
        relative = _relative(path, root)
        relative_paths.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), relative_paths


def _catalog_warnings(
    tools: list[dict[str, Any]],
    capability_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成重复 ID、能力回退和 SDK 声明差异等只读诊断。"""

    warnings: list[dict[str, Any]] = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        by_id.setdefault(tool["tool_id"], []).append(tool)
        if tool["capability"]["source"] == "default-fallback":
            warnings.append(
                {
                    "code": "capability-default-fallback",
                    "tool_id": tool["tool_id"],
                    "provider_id": tool["provider_id"],
                }
            )
        declared = tool.get("declared_contract")
        declared_level = str((declared or {}).get("side_effect_level") or "")
        expected_level = (
            "none" if declared_level in {"none", "read"} else "high"
        )
        if isinstance(declared, dict) and expected_level != tool["capability"].get("side_effect_level"):
            warnings.append(
                {
                    "code": "sdk-effective-capability-mismatch",
                    "tool_id": tool["tool_id"],
                    "provider_id": tool["provider_id"],
                    "declared": declared.get("side_effect_level"),
                    "effective": tool["capability"].get("side_effect_level"),
                }
            )
    for tool_id, records in by_id.items():
        if len(records) > 1:
            warnings.append(
                {
                    "code": "static-tool-id-conflict",
                    "tool_id": tool_id,
                    "providers": sorted(item["provider_id"] for item in records),
                }
            )
    static_ids = set(by_id)
    for tool_id in sorted(
        set(capability_map) - static_ids - set(CAPABILITY_ONLY_CLASSIFICATIONS)
    ):
        warnings.append(
            {"code": "capability-without-static-tool", "tool_id": tool_id}
        )
    return sorted(
        warnings,
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("tool_id") or ""),
            str(item.get("provider_id") or ""),
        ),
    )


def build_catalog(root: Path) -> dict[str, Any]:
    """构建包含核心、仓库插件和动态工具族边界的 Tool Catalog。"""

    root = root.resolve()
    capability_path = root / "tcode_agent/tools/capabilities.py"
    registry_path = root / "tcode_agent/tools/registry.py"
    builtin_init = root / "tcode_agent/tools/builtin/__init__.py"
    team_init = root / "tcode_agent/team/tools/__init__.py"
    tools_init = root / "tcode_agent/tools/__init__.py"
    skill_path = root / "tcode_agent/skill/tool.py"
    capability_map = _parse_capability_map(root)
    policies = _parse_registry_policies(root)
    records: list[dict[str, Any]] = []
    source_paths: set[Path] = {
        capability_path,
        registry_path,
        builtin_init,
        team_init,
        tools_init,
        skill_path,
    }

    for class_name, source_path, optional in _aggregate_tool_classes(builtin_init):
        records.append(
            _class_tool_record(
                root=root,
                path=source_path,
                class_name=class_name,
                provider_kind="builtin",
                provider_id="tcode.builtin",
                registration="builtin-import",
                optional=optional,
                capability_map=capability_map,
                policies=policies,
            )
        )
        source_paths.add(source_path)
    for class_name, source_path, optional in _aggregate_tool_classes(team_init):
        records.append(
            _class_tool_record(
                root=root,
                path=source_path,
                class_name=class_name,
                provider_kind="team",
                provider_id="tcode.team",
                registration="lazy-team-import",
                optional=optional,
                capability_map=capability_map,
                policies=policies,
            )
        )
        source_paths.add(source_path)
    records.append(
        _class_tool_record(
            root=root,
            path=skill_path,
            class_name="SkillTool",
            provider_kind="skill",
            provider_id="tcode.skill",
            registration="best-effort-tools-import",
            optional=False,
            capability_map=capability_map,
            policies=policies,
        )
    )

    legacy_records, legacy_sources = _legacy_plugin_records(
        root, capability_map, policies
    )
    sdk_records, sdk_sources, sdk_dynamic_sites = _sdk_plugin_records(root, capability_map, policies)
    records.extend(legacy_records)
    records.extend(sdk_records)
    source_paths.update(legacy_sources)
    source_paths.update(sdk_sources)
    records.sort(key=lambda item: (item["tool_id"], item["provider_id"]))

    dynamic_sites = sorted(
        _dynamic_registration_sites(root) + sdk_dynamic_sites,
        key=lambda item: (item["file"], item["line"], item["api"]),
    )
    for site in dynamic_sites:
        source_paths.add(root / site["file"])
    digest, source_files = _source_digest(root, source_paths)
    warnings = _catalog_warnings(records, capability_map)
    provider_counts: dict[str, int] = {}
    for record in records:
        provider_counts[record["provider_kind"]] = (
            provider_counts.get(record["provider_kind"], 0) + 1
        )
    warning_counts: dict[str, int] = {}
    for warning in warnings:
        code = warning["code"]
        warning_counts[code] = warning_counts.get(code, 0) + 1
    capability_only_entries = [
        {"capability_id": tool_id, **CAPABILITY_ONLY_CLASSIFICATIONS[tool_id]}
        for tool_id in sorted(CAPABILITY_ONLY_CLASSIFICATIONS)
        if tool_id in capability_map and tool_id not in {item["tool_id"] for item in records}
    ]

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "generated_by": "scripts/dsh.py catalog",
        "source_digest": digest,
        "source_files": source_files,
        "summary": {
            "static_tool_count": len(records),
            "core_tool_count": sum(
                item["provider_kind"] in {"builtin", "team", "skill"}
                for item in records
            ),
            "repository_plugin_tool_count": sum(
                item["provider_kind"].startswith("repository-plugin")
                or item["provider_kind"].startswith("plugin-sdk")
                for item in records
            ),
            "provider_kind_counts": dict(sorted(provider_counts.items())),
            "explicit_capability_count": sum(
                item["capability"]["source"] == "explicit-map" for item in records
            ),
            "default_capability_count": sum(
                item["capability"]["source"] == "default-fallback" for item in records
            ),
            "sdk_declared_capability_count": sum(
                item["capability"]["source"] == "sdk-declared" for item in records
            ),
            "dynamic_registration_site_count": len(dynamic_sites),
            "classified_capability_only_count": len(capability_only_entries),
            "warning_counts": dict(sorted(warning_counts.items())),
            "source_file_count": len(source_files),
        },
        "scope": {
            "static_sources": [path.as_posix() for path in STATIC_SOURCE_ROOTS],
            "imports_application_modules": False,
            "starts_external_mcp": False,
            "authoritative_for": [
                "repository static tool declarations",
                "ToolRegistry static visibility groups",
                "explicit ToolCapability map entries",
            ],
            "not_authoritative_for": [
                "runtime MCP server tool names",
                "external plugin contributions",
                "per-agent permission decisions",
                "production enablement",
            ],
        },
        "dynamic_families": [
            {
                "family": "mcp",
                "identity": "runtime server name + runtime tool name",
                "patterns": ["mcp_{server}_{tool}", "{client}_{tool}"],
                "capability_policy": "mcp dynamic fallback",
                "production_authorized_by_catalog": False,
            },
            {
                "family": "plugin-sdk",
                "identity": "validated ToolDefinition.name",
                "patterns": ["^[a-z][a-z0-9_-]{0,63}$"],
                "capability_policy": "explicit map or registry default fallback",
                "production_authorized_by_catalog": False,
            },
            {
                "family": "legacy-repository-plugin",
                "identity": "Tool.id returned by configured get_tools()",
                "patterns": ["plugin-defined"],
                "capability_policy": "explicit map or registry default fallback",
                "production_authorized_by_catalog": False,
            },
        ],
        "registry_policies": {
            key: sorted(values) for key, values in sorted(policies.items())
        },
        "dynamic_registration_sites": dynamic_sites,
        "capability_only_entries": capability_only_entries,
        "warnings": warnings,
        "tools": records,
    }


def render_json(catalog: dict[str, Any]) -> str:
    """以固定 UTF-8 格式渲染机器 JSON。"""

    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _markdown_cell(value: Any) -> str:
    """转义适合 Markdown 表格单元格的短文本。"""

    text = str(value or "").replace("\n", " ").replace("|", "\\|")
    return text


def render_markdown(catalog: dict[str, Any]) -> str:
    """渲染便于人工审计的 Tool Catalog Markdown。"""

    summary = catalog["summary"]
    lines = [
        "# Tool Catalog",
        "",
        "> 自动生成，请勿手工编辑。",
        "> 生成命令：`python scripts/dsh.py catalog`",
        f"> Schema：`{catalog['schema']}`；Generator：`v{catalog['generator_version']}`。",
        f"> Source digest：`{catalog['source_digest']}`。",
        "",
        "## 摘要",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 静态工具 | {summary['static_tool_count']} |",
        f"| 核心工具 | {summary['core_tool_count']} |",
        f"| 仓库插件工具 | {summary['repository_plugin_tool_count']} |",
        f"| 显式 Capability | {summary['explicit_capability_count']} |",
        f"| 默认 Capability 回退 | {summary['default_capability_count']} |",
        f"| 动态注册点 | {summary['dynamic_registration_site_count']} |",
        f"| 已分类非 Tool Capability | {summary['classified_capability_only_count']} |",
        f"| 源文件 | {summary['source_file_count']} |",
        "",
        "本目录只描述仓库静态事实，不代表工具已对任意 Agent、Workspace 或生产环境启用。",
        "MCP 和外部插件的具体工具名必须在运行时目录中观察，不能由本文件推定。",
        "",
        "## 静态工具",
        "",
        "| Tool ID | Provider | 类型 | 注册方式 | 可见性 | Capability | 副作用 | 参数 Schema | 来源 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for tool in catalog["tools"]:
        source = tool["source"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{_markdown_cell(tool['tool_id'])}`",
                    f"`{_markdown_cell(tool['provider_id'])}`",
                    _markdown_cell(tool["provider_kind"]),
                    _markdown_cell(tool["registration"]),
                    _markdown_cell(tool["visibility"]["mode"]),
                    _markdown_cell(tool["capability"]["source"]),
                    _markdown_cell(tool["capability"]["side_effect_level"]),
                    f"`{_markdown_cell(tool['parameters']['schema_expression'])}`",
                    f"`{source['file']}:{source['line']}`",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 动态工具族",
            "",
            "| 工具族 | Identity | 模式 | Capability 策略 | 本目录是否授权生产 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for family in catalog["dynamic_families"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(family["family"]),
                    _markdown_cell(family["identity"]),
                    "、".join(f"`{item}`" for item in family["patterns"]),
                    _markdown_cell(family["capability_policy"]),
                    "否",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 只读诊断",
            "",
            "诊断不会修改 ToolRegistry、Capability、Permission、Sandbox 或生产 Consumer。",
            "",
            "| 代码 | Tool ID | Provider/明细 |",
            "| --- | --- | --- |",
        ]
    )
    for warning in catalog["warnings"]:
        details = warning.get("provider_id") or ", ".join(warning.get("providers") or [])
        if warning["code"] == "sdk-effective-capability-mismatch":
            details = f"declared={warning['declared']}, effective={warning['effective']}"
        lines.append(
            f"| `{warning['code']}` | `{_markdown_cell(warning.get('tool_id'))}` | {_markdown_cell(details)} |"
        )
    lines.extend(
        [
            "",
            "## 动态注册点",
            "",
            "| API | 表达式 | 来源 |",
            "| --- | --- | --- |",
        ]
    )
    for site in catalog["dynamic_registration_sites"]:
        lines.append(
            f"| `{site['api']}` | `{_markdown_cell(site['expression'])}` | `{site['file']}:{site['line']}` |"
        )
    lines.extend(["", "## Source Files", ""])
    lines.extend(f"- `{path}`" for path in catalog["source_files"])
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, content: str) -> bool:
    """仅在字节变化时写入生成文件并返回是否发生修改。"""

    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _check_current(path: Path, expected: str) -> bool:
    """检查生成文件是否存在且与期望字节完全一致。"""

    return path.is_file() and path.read_text(encoding="utf-8") == expected


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析生成、输出路径和只读漂移检查参数。"""

    parser = argparse.ArgumentParser(
        description="静态生成 Tool Catalog，不导入应用模块或启动外部 MCP。"
    )
    parser.add_argument("--root", default=str(_repo_root()))
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_PATH))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_PATH))
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """生成目录，或在 `--check` 模式下只报告字节漂移。"""

    args = _parse_args(argv)
    root = Path(args.root).resolve()
    json_path = Path(args.json_output)
    markdown_path = Path(args.markdown_output)
    if not json_path.is_absolute():
        json_path = root / json_path
    if not markdown_path.is_absolute():
        markdown_path = root / markdown_path
    catalog = build_catalog(root)
    rendered_json = render_json(catalog)
    rendered_markdown = render_markdown(catalog)
    if args.check:
        stale = [
            path
            for path, expected in (
                (json_path, rendered_json),
                (markdown_path, rendered_markdown),
            )
            if not _check_current(path, expected)
        ]
        for path in stale:
            print(f"generated file is stale: {path}", file=sys.stderr)
        return 1 if stale else 0
    changed = [
        path
        for path, content in (
            (json_path, rendered_json),
            (markdown_path, rendered_markdown),
        )
        if _write_if_changed(path, content)
    ]
    print(
        f"[tool-catalog] tools={catalog['summary']['static_tool_count']} "
        f"warnings={len(catalog['warnings'])} changed={len(changed)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
