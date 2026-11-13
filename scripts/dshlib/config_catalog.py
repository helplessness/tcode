"""静态生成 Config/Env Catalog，且绝不读取当前进程中的环境变量值。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .common import source_digest, stable_json


SCHEMA = "tcode.config-env-catalog.v1"
GENERATOR_VERSION = 1
PYTHON_ROOTS = (Path("tcode_agent"), Path("scripts"), Path("plugins"))
_SECRET_MARKERS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "AUTH",
)
_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_BRACED_DEFAULT_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}"
)


def _relative(path: Path, root: Path) -> str:
    """把文件路径转换为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(root.resolve()).as_posix()


def _attribute_name(node: ast.AST) -> str:
    """把名称和属性 AST 节点渲染为点分名称。"""

    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _literal(node: ast.AST | None) -> Any:
    """读取安全 Python 字面量，动态表达式返回空值。"""

    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return None


def _expression(node: ast.AST | None) -> str:
    """返回不执行表达式即可得到的有界诊断文本。"""

    if node is None:
        return "<unset>"
    try:
        return ast.unparse(node)[:160]
    except Exception:
        return type(node).__name__


def _is_secret(name: str) -> bool:
    """依据配置键名称判断值是否必须从目录中完全脱敏。"""

    normalized = str(name or "").upper()
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _safe_default(name: str, value: Any, expression: str) -> str:
    """返回可公开默认值；Secret 配置始终只返回脱敏占位符。"""

    if _is_secret(name):
        return "<redacted>"
    if value is None:
        return expression or "<unset>"
    if isinstance(value, (str, int, float, bool)):
        text = json.dumps(value, ensure_ascii=False)
        return text[:160]
    return f"<{type(value).__name__}>"


def _infer_literal_type(value: Any) -> str:
    """把 Python 字面量类型投影为稳定配置类型名称。"""

    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


class PythonEnvVisitor(ast.NodeVisitor):
    """收集单个 Python 文件内对环境变量的静态读取位置。"""

    def __init__(self, *, relative_file: str) -> None:
        """初始化文件身份、常量表、作用域栈和空引用结果。"""

        self.relative_file = relative_file
        self.constants: dict[str, str] = {}
        self.scope: list[str] = []
        self.parents: list[ast.AST] = []
        self.references: list[dict[str, Any]] = []

    def visit(self, node: ast.AST) -> Any:
        """在标准访问行为外维护父节点栈，以便推断类型包装器。"""

        self.parents.append(node)
        try:
            return super().visit(node)
        finally:
            self.parents.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        """记录静态字符串常量，并继续扫描赋值表达式中的环境读取。"""

        value = _literal(node.value)
        if isinstance(value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.constants[target.id] = value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """记录带类型标注的静态字符串常量。"""

        value = _literal(node.value)
        if isinstance(node.target, ast.Name) and isinstance(value, str):
            self.constants[node.target.id] = value
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """在同步函数作用域内扫描读取，并保留函数名作为重载诊断。"""

        self.scope.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """在异步函数作用域内扫描读取，并保留函数名作为重载诊断。"""

        self.scope.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self.scope.pop()

    def _resolve_name(self, node: ast.AST | None) -> str | None:
        """把字符串字面量或同文件字符串常量解析为环境变量名称。"""

        value = _literal(node)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            return value
        if isinstance(node, ast.Name):
            candidate = self.constants.get(node.id)
            if candidate and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
                return candidate
        return None

    def _value_type(self, default: Any, call: ast.Call | None = None) -> str:
        """结合默认值、帮助函数和外层转换函数推断环境值类型。"""

        call_name = _attribute_name(call.func) if call is not None else ""
        if any(marker in call_name.lower() for marker in ("truthy", "bool")):
            return "boolean"
        for parent in reversed(self.parents[:-1][-3:]):
            if isinstance(parent, ast.Call):
                wrapper = _attribute_name(parent.func).split(".")[-1]
                if wrapper in {"int", "float", "str", "bool"}:
                    return {
                        "int": "integer",
                        "float": "number",
                        "str": "string",
                        "bool": "boolean",
                    }[wrapper]
        return _infer_literal_type(default)

    def _reloadability(self) -> str:
        """依据读取作用域区分模块启动读取和运行期按需读取。"""

        if not self.scope:
            return "startup-only"
        function_name = self.scope[-1].lower()
        if "reload" in function_name or "from_env" in function_name:
            return "reloadable"
        return "read-on-use"

    def _record(
        self,
        *,
        name: str,
        node: ast.AST,
        access: str,
        default_node: ast.AST | None,
        call: ast.Call | None = None,
    ) -> None:
        """追加一条不含实际环境值的静态引用记录。"""

        default = _literal(default_node)
        self.references.append(
            {
                "name": name,
                "file": self.relative_file,
                "line": int(getattr(node, "lineno", 0) or 0),
                "scope": ".".join(self.scope) or "<module>",
                "access": access,
                "type": self._value_type(default, call),
                "default": _safe_default(name, default, _expression(default_node)),
                "secret": _is_secret(name),
                "reloadability": self._reloadability(),
            }
        )

    def visit_Call(self, node: ast.Call) -> None:
        """识别 getenv、environ.get 及常见 env 帮助函数的静态调用。"""

        call_name = _attribute_name(node.func)
        direct = call_name in {"os.getenv", "getenv", "os.environ.get", "environ.get"}
        helper = call_name.split(".")[-1] in {"_truthy_env", "_first_env"}
        if direct and node.args:
            name = self._resolve_name(node.args[0])
            if name:
                default_node = node.args[1] if len(node.args) > 1 else None
                self._record(
                    name=name,
                    node=node,
                    access=call_name,
                    default_node=default_node,
                    call=node,
                )
        elif helper:
            for index, argument in enumerate(node.args):
                name = self._resolve_name(argument)
                if not name:
                    continue
                default_node = node.args[1] if call_name.endswith("_truthy_env") and index == 0 and len(node.args) > 1 else None
                self._record(
                    name=name,
                    node=node,
                    access=call_name,
                    default_node=default_node,
                    call=node,
                )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """识别必须存在的 ``os.environ[NAME]`` 静态读取。"""

        if _attribute_name(node.value) in {"os.environ", "environ"}:
            name = self._resolve_name(node.slice)
            if name:
                self._record(
                    name=name,
                    node=node,
                    access="os.environ[]",
                    default_node=None,
                )
        self.generic_visit(node)


def _iter_python_files(root: Path) -> Iterable[Path]:
    """稳定枚举 DSH 目录范围内的 Python 源文件。"""

    for relative_root in PYTHON_ROOTS:
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _python_references(root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    """解析 Python 源码并返回环境引用与实际参与目录的文件。"""

    references: list[dict[str, Any]] = []
    sources: list[Path] = []
    for path in _iter_python_files(root):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = PythonEnvVisitor(relative_file=_relative(path, root))
        visitor.visit(tree)
        if visitor.references:
            references.extend(visitor.references)
            sources.append(path)
    return references, sources


def _iter_env_files(root: Path) -> Iterable[Path]:
    """枚举仓库配置目录中受控的 env 与 env.example 文件。"""

    config_root = root / "config"
    if not config_root.is_dir():
        return
    candidates = {
        *config_root.rglob("*.env"),
        *config_root.rglob("*.env.example"),
        *config_root.rglob("*.example.env"),
    }
    for path in sorted(candidates):
        if path.is_file():
            yield path


def _env_file_references(root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    """提取 env 文件中的赋值和默认表达式，但从不展开或返回 Secret 值。"""

    references: list[dict[str, Any]] = []
    sources: list[Path] = []
    for path in _iter_env_files(root):
        relative = _relative(path, root)
        matched = False
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            assignment = _ASSIGNMENT_RE.match(line)
            if assignment:
                name, expression = assignment.groups()
                references.append(
                    {
                        "name": name,
                        "file": relative,
                        "line": line_number,
                        "scope": "env-file",
                        "access": "assignment",
                        "type": "string",
                        "default": _safe_default(name, None, "<configured>"),
                        "secret": _is_secret(name),
                        "reloadability": "process-restart",
                    }
                )
                matched = True
            for name, default in _BRACED_DEFAULT_RE.findall(line):
                references.append(
                    {
                        "name": name,
                        "file": relative,
                        "line": line_number,
                        "scope": "env-file",
                        "access": "shell-expansion",
                        "type": "string",
                        "default": _safe_default(
                            name,
                            None,
                            "<configured-default>" if default else "<unset>",
                        ),
                        "secret": _is_secret(name),
                        "reloadability": "process-restart",
                    }
                )
                matched = True
        if matched:
            sources.append(path)
    return references, sources


def _walk_config_keys(
    value: Any,
    *,
    prefix: str,
    file: str,
    from_schema: bool,
) -> Iterable[dict[str, Any]]:
    """递归投影 JSON 配置键或 JSON Schema 属性，不输出实际配置值。"""

    if not isinstance(value, dict):
        return
    if from_schema:
        properties = value.get("properties")
        if isinstance(properties, dict):
            required = set(value.get("required") or [])
            for key, schema in sorted(properties.items()):
                path = f"{prefix}.{key}" if prefix else str(key)
                schema = schema if isinstance(schema, dict) else {}
                secret = _is_secret(path) or bool(schema.get("writeOnly"))
                default = schema.get("default")
                yield {
                    "path": path,
                    "file": file,
                    "type": str(schema.get("type") or "unknown"),
                    "required": key in required,
                    "secret": secret,
                    "default": _safe_default(path, default, "<unset>"),
                    "reloadability": "plugin-reload",
                }
                yield from _walk_config_keys(
                    schema,
                    prefix=path,
                    file=file,
                    from_schema=True,
                )
        items = value.get("items")
        if isinstance(items, dict):
            yield from _walk_config_keys(
                items,
                prefix=f"{prefix}[]",
                file=file,
                from_schema=True,
            )
        return
    for key, nested in sorted(value.items()):
        path = f"{prefix}.{key}" if prefix else str(key)
        yield {
            "path": path,
            "file": file,
            "type": _infer_literal_type(nested),
            "required": False,
            "secret": _is_secret(path),
            "default": "<redacted>" if _is_secret(path) else "<configured>",
            "reloadability": "config-reload",
        }
        if isinstance(nested, dict):
            yield from _walk_config_keys(
                nested,
                prefix=path,
                file=file,
                from_schema=False,
            )


def _config_key_records(root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    """读取配置键名和 Schema 元数据，不把配置值写入目录。"""

    paths: list[tuple[Path, bool]] = []
    primary = root / "config" / "templates" / "tcode.example.json"
    if primary.is_file():
        paths.append((primary, False))
    paths.extend((path, True) for path in sorted((root / "plugins").glob("*/config.schema.json")))
    records: list[dict[str, Any]] = []
    sources: list[Path] = []
    for path, from_schema in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        relative = _relative(path, root)
        records.extend(
            _walk_config_keys(
                document,
                prefix="",
                file=relative,
                from_schema=from_schema,
            )
        )
        sources.append(path)
    unique = {
        (item["path"], item["file"]): item
        for item in records
    }
    return [unique[key] for key in sorted(unique)], sources


def _merge_environment(references: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按环境变量名称合并引用，同时保留全部静态来源和保守属性。"""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for reference in references:
        grouped.setdefault(str(reference["name"]), []).append(reference)
    merged: list[dict[str, Any]] = []
    for name in sorted(grouped):
        items = sorted(grouped[name], key=lambda item: (item["file"], item["line"], item["access"]))
        types = sorted({str(item["type"]) for item in items if item["type"] != "unknown"})
        defaults = sorted({str(item["default"]) for item in items})
        reloadability = sorted({str(item["reloadability"]) for item in items})
        merged.append(
            {
                "name": name,
                "secret": _is_secret(name),
                "types": types or ["unknown"],
                "defaults": ["<redacted>"] if _is_secret(name) else defaults,
                "reloadability": reloadability,
                "sources": items,
            }
        )
    return merged


def build_catalog(root: Path) -> dict[str, Any]:
    """构建只包含静态事实且不读取当前环境值的 Config/Env Catalog。"""

    root = root.resolve()
    python_references, python_sources = _python_references(root)
    env_references, env_sources = _env_file_references(root)
    config_keys, config_sources = _config_key_records(root)
    environment = _merge_environment([*python_references, *env_references])
    digest, source_files = source_digest(
        root,
        [*python_sources, *env_sources, *config_sources],
    )
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "generated_by": "scripts/dsh.py catalog",
        "source_digest": digest,
        "source_files": source_files,
        "controls": {
            "reads_process_environment_values": False,
            "expands_env_files": False,
            "emits_secret_values": False,
            "changes_runtime_configuration": False,
        },
        "summary": {
            "environment_key_count": len(environment),
            "secret_environment_key_count": sum(item["secret"] for item in environment),
            "config_key_count": len(config_keys),
            "secret_config_key_count": sum(item["secret"] for item in config_keys),
            "source_file_count": len(source_files),
        },
        "environment": environment,
        "config_keys": config_keys,
    }


def render_json(catalog: dict[str, Any]) -> str:
    """渲染键排序且不会泄漏进程环境值的 JSON 目录。"""

    return stable_json(catalog)


def render_markdown(catalog: dict[str, Any]) -> str:
    """渲染 Config/Env Catalog 的紧凑人工审计摘要。"""

    summary = catalog["summary"]
    lines = [
        "# Config / Env Catalog",
        "",
        "> 由 `python scripts/dsh.py catalog` 静态生成；不读取当前环境变量值。",
        f"> Source digest：`{catalog['source_digest']}`。",
        "",
        "## 摘要",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 环境变量键 | {summary['environment_key_count']} |",
        f"| Secret 环境变量键 | {summary['secret_environment_key_count']} |",
        f"| JSON/Schema 配置键 | {summary['config_key_count']} |",
        f"| Secret 配置键 | {summary['secret_config_key_count']} |",
        f"| 来源文件 | {summary['source_file_count']} |",
        "",
        "## 环境变量",
        "",
        "| 名称 | 类型 | Secret | 生效方式 | 来源数 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for item in catalog["environment"]:
        lines.append(
            f"| `{item['name']}` | {', '.join(item['types'])} | "
            f"{'是' if item['secret'] else '否'} | {', '.join(item['reloadability'])} | "
            f"{len(item['sources'])} |"
        )
    lines.extend(
        [
            "",
            "Secret 的默认值和实际值均不会出现在本目录中。",
            "目录只描述静态读取点，不代表配置已在生产环境启用。",
            "",
        ]
    )
    return "\n".join(lines)
