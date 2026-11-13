#!/usr/bin/env python3
"""在不导入应用模块的前提下静态生成 Hook/Event 目录。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


GENERATOR_VERSION = 3
DEFAULT_JSON_PATH = Path("build/dsh/hook-event-catalog.json")
DEFAULT_MARKDOWN_PATH = Path("build/dsh/hook-event-catalog.md")
DEFAULT_SOURCE_ROOTS = (Path("tcode_agent"), Path("plugins"))
DIRECT_EMIT_METHODS = {
    "dispatch": "dynamic",
    "emit": "serial",
    "emit_parallel": "parallel",
    "emit_serial": "serial",
}


def _repo_root() -> Path:
    """返回当前脚本所属仓库的绝对根目录。"""

    return Path(__file__).resolve().parents[2]


def _relative(path: Path, root: Path) -> str:
    """把源码路径规范化为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(root.resolve()).as_posix()


def _attribute_name(node: ast.AST) -> str:
    """把名称或属性访问 AST 转换为稳定点分表达式。"""

    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _constant_string(node: ast.AST | None) -> str | None:
    """读取并小写规范化非空字符串常量。"""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.strip().lower()
        return value or None
    return None


def _literal_text(node: ast.AST | None) -> str | None:
    """读取字符串常量并保留原始大小写。"""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.strip()
        return value or None
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    """从调用表达式中读取指定关键字参数节点。"""

    for item in call.keywords:
        if item.arg == name:
            return item.value
    return None


def _call_argument(call: ast.Call, position: int, keyword: str) -> ast.AST | None:
    """按关键字优先、位置参数兜底读取调用参数。"""

    value = _keyword(call, keyword)
    if value is not None:
        return value
    if len(call.args) > position:
        return call.args[position]
    return None


def _literal_scalar(node: ast.AST | None) -> Any:
    """读取 JSON 兼容标量常量，动态表达式返回空值。"""

    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        return node.value
    return None


def _enum_member(node: ast.AST, enum_name: str) -> str | None:
    """解析枚举成员或其 value 属性，并返回静态成员名。"""

    if not isinstance(node, ast.Attribute):
        return None
    if isinstance(node.value, ast.Name) and node.value.id == enum_name:
        return node.attr
    if (
        node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == enum_name
    ):
        return node.value.attr
    return None


def _event_value(node: ast.AST | None, enum_values: dict[str, str]) -> str | None:
    """把事件字符串或 HookEvent 成员解析为稳定事件名。"""

    if node is None:
        return None
    literal = _constant_string(node)
    if literal and "." in literal:
        return literal
    member = _enum_member(node, "HookEvent")
    return enum_values.get(member or "")


def _expression_text(node: ast.AST | None) -> str:
    """返回只用于诊断且不执行代码的 AST 表达式文本。"""

    if node is None:
        return "<missing>"
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _iter_python_files(root: Path, source_roots: Sequence[Path]) -> Iterable[Path]:
    """按稳定顺序枚举指定源码根中的 Python 文件。"""

    for relative_root in source_roots:
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _parse_source(path: Path) -> tuple[str, ast.Module]:
    """读取并解析 Python 源码，将语法错误转换为带路径异常。"""

    source = path.read_text(encoding="utf-8")
    try:
        return source, ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"cannot parse {path}: {exc}") from exc


def _find_named_node(tree: ast.Module, name: str) -> ast.AST:
    """在模块顶层查找指定函数、类或静态赋值节点。"""

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return node
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value or node
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
    raise RuntimeError(f"required static definition not found: {name}")


def _parse_enum_values(tree: ast.Module, class_name: str) -> dict[str, str]:
    """从枚举类静态提取成员名到小写字符串值的映射。"""

    class_node = _find_named_node(tree, class_name)
    if not isinstance(class_node, ast.ClassDef):
        raise RuntimeError(f"{class_name} is not a class")
    values: dict[str, str] = {}
    for node in class_node.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = _constant_string(node.value)
        if isinstance(target, ast.Name) and value:
            values[target.id] = value
    if not values:
        raise RuntimeError(f"{class_name} does not contain static string values")
    return values


def _parse_descriptions(tree: ast.Module, enum_values: dict[str, str]) -> dict[str, str]:
    """从事件描述静态映射中提取事件到人工说明的关系。"""

    mapping = _find_named_node(tree, "_EVENT_DESCRIPTIONS")
    if not isinstance(mapping, ast.Dict):
        raise RuntimeError("_EVENT_DESCRIPTIONS must remain a static dict")
    descriptions: dict[str, str] = {}
    for key_node, value_node in zip(mapping.keys, mapping.values):
        if key_node is None:
            continue
        member = _enum_member(key_node, "HookEvent")
        description = _literal_text(value_node)
        event = enum_values.get(member or "")
        if event and description:
            descriptions[event] = description
    return descriptions


def _parse_parallel_allowed_events(
    tree: ast.Module,
    enum_values: dict[str, str],
) -> set[str]:
    """解析 EventSpec 明确允许并行分发的固定事件集合。"""

    value = _find_named_node(tree, "_PARALLEL_ALLOWED_EVENTS")
    if isinstance(value, ast.Call):
        if (
            not isinstance(value.func, ast.Name)
            or value.func.id != "frozenset"
            or len(value.args) != 1
            or value.keywords
        ):
            raise RuntimeError("_PARALLEL_ALLOWED_EVENTS must remain a static frozenset")
        value = value.args[0]
    if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        raise RuntimeError("_PARALLEL_ALLOWED_EVENTS must contain static HookEvent members")
    events: set[str] = set()
    for item in value.elts:
        member = _enum_member(item, "HookEvent")
        event = enum_values.get(member or "")
        if event is None:
            raise RuntimeError(
                "unsupported _PARALLEL_ALLOWED_EVENTS member: " + _expression_text(item)
            )
        if event in events:
            raise RuntimeError(f"duplicate parallel-allowed HookEvent: {event}")
        events.add(event)
    return events


def _parse_static_string_collection(tree: ast.Module, name: str) -> set[str]:
    """解析指定模块级字符串集合，并拒绝动态值和重复项。"""

    value = _find_named_node(tree, name)
    if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        raise RuntimeError(f"{name} must remain a static string collection")
    values: set[str] = set()
    for item in value.elts:
        literal = _constant_string(item)
        if literal is None:
            raise RuntimeError(f"{name} contains a non-string value")
        if literal in values:
            raise RuntimeError(f"{name} contains duplicate value: {literal}")
        values.add(literal)
    return values


def _named_source_digest(source: str, tree: ast.Module, names: Sequence[str]) -> str:
    """对指定静态定义的原始源码片段计算稳定摘要。"""

    segments: list[str] = []
    for name in names:
        node = _find_named_node(tree, name)
        segment = ast.get_source_segment(source, node)
        if segment is None:
            raise RuntimeError(f"cannot extract static source for {name}")
        segments.append(segment.strip())
    return hashlib.sha256("\n".join(segments).encode("utf-8")).hexdigest()


def _eval_static_expr(
    node: ast.AST,
    *,
    event_value: str,
    enum_values: dict[str, str],
) -> Any:
    """在受限语法子集中求值 required_input 的静态条件表达式。"""

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "value":
            return event_value
        raise RuntimeError(f"unsupported static name in _required_input: {node.id}")
    if isinstance(node, ast.Tuple):
        return tuple(
            _eval_static_expr(item, event_value=event_value, enum_values=enum_values)
            for item in node.elts
        )
    if isinstance(node, (ast.List, ast.Set)):
        values = [
            _eval_static_expr(item, event_value=event_value, enum_values=enum_values)
            for item in node.elts
        ]
        return values if isinstance(node, ast.List) else set(values)
    member = _enum_member(node, "HookEvent")
    if member:
        if member not in enum_values:
            raise RuntimeError(f"unknown HookEvent member in _required_input: {member}")
        return enum_values[member]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = _eval_static_expr(
            node.func.value,
            event_value=event_value,
            enum_values=enum_values,
        )
        args = [
            _eval_static_expr(item, event_value=event_value, enum_values=enum_values)
            for item in node.args
        ]
        if node.func.attr == "startswith" and isinstance(receiver, str):
            return receiver.startswith(*args)
    if isinstance(node, ast.BoolOp):
        values = [
            bool(_eval_static_expr(item, event_value=event_value, enum_values=enum_values))
            for item in node.values
        ]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(
            _eval_static_expr(node.operand, event_value=event_value, enum_values=enum_values)
        )
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        left = _eval_static_expr(
            node.left,
            event_value=event_value,
            enum_values=enum_values,
        )
        right = _eval_static_expr(
            node.comparators[0],
            event_value=event_value,
            enum_values=enum_values,
        )
        operator = node.ops[0]
        if isinstance(operator, ast.In):
            return left in right
        if isinstance(operator, ast.NotIn):
            return left not in right
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
    raise RuntimeError(
        "unsupported _required_input expression; update the static catalog parser: "
        + _expression_text(node)
    )


def _parse_required_input(
    tree: ast.Module,
    event_value: str,
    enum_values: dict[str, str],
) -> list[str]:
    """针对一个固定事件静态解释 required_input 函数的返回字段。"""

    function = _find_named_node(tree, "_required_input")
    if not isinstance(function, ast.FunctionDef):
        raise RuntimeError("_required_input must remain a synchronous function")
    for statement in function.body:
        if isinstance(statement, ast.Assign):
            continue
        if isinstance(statement, ast.If):
            matched = bool(
                _eval_static_expr(
                    statement.test,
                    event_value=event_value,
                    enum_values=enum_values,
                )
            )
            if not matched:
                continue
            returns = [item for item in statement.body if isinstance(item, ast.Return)]
            if len(returns) != 1 or returns[0].value is None:
                raise RuntimeError("unsupported _required_input branch")
            value = _eval_static_expr(
                returns[0].value,
                event_value=event_value,
                enum_values=enum_values,
            )
            return [str(item) for item in value]
        if isinstance(statement, ast.Return):
            if statement.value is None:
                return []
            value = _eval_static_expr(
                statement.value,
                event_value=event_value,
                enum_values=enum_values,
            )
            return [str(item) for item in value]
        raise RuntimeError(
            "unsupported _required_input statement; update the static catalog parser: "
            + type(statement).__name__
        )
    raise RuntimeError("_required_input does not return")


def _runtime_spec_digest(source: str, tree: ast.Module) -> str:
    """计算 EventSpec 权威声明相关源码片段的稳定摘要。"""

    nodes = [
        _find_named_node(tree, "HookDispatchMode"),
        _find_named_node(tree, "HookEvent"),
        _find_named_node(tree, "HookEventSpec"),
        _find_named_node(tree, "_EVENT_DESCRIPTIONS"),
        _find_named_node(tree, "_event_category"),
        _find_named_node(tree, "_required_input"),
        _find_named_node(tree, "_PARALLEL_ALLOWED_EVENTS"),
        _find_named_node(tree, "_allowed_modes"),
        _find_named_node(tree, "_recommended_mode"),
    ]
    segments: list[str] = []
    for node in nodes:
        segment = ast.get_source_segment(source, node)
        if segment is None:
            raise RuntimeError("cannot extract Hook/Event runtime specification source")
        segments.append(segment.strip())
    # 对源码片段而非 ast.dump 计算摘要，避免 Python 版本造成 AST 形状差异。
    normalized = "\n".join(segments)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _location(root: Path, path: Path, node: ast.AST, **extra: Any) -> dict[str, Any]:
    """构造包含相对文件、行号和附加静态事实的位置记录。"""

    return {
        "file": _relative(path, root),
        "line": int(getattr(node, "lineno", 0) or 0),
        **extra,
    }


def _handler_names(node: ast.AST) -> list[str]:
    """把单个或多个 Handler 表达式转换为稳定名称列表。"""

    values = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    return sorted({_expression_text(value) for value in values})


class _ReferenceVisitor(ast.NodeVisitor):
    """收集单个源码文件中的 Hook 注册、触发和动态事件引用。"""

    def __init__(
        self,
        *,
        root: Path,
        path: Path,
        enum_values: dict[str, str],
        parallel_canary_events: set[str],
    ) -> None:
        """初始化源码身份、事件映射、结果容器和访问栈。"""

        self.root = root
        self.path = path
        self.enum_values = enum_values
        self.parallel_canary_events = parallel_canary_events
        self.registrations: list[dict[str, Any]] = []
        self.emissions: list[dict[str, Any]] = []
        self.dynamic_references: list[dict[str, Any]] = []
        self._functions: list[str] = []
        self._parents: list[ast.AST] = []

    def visit(self, node: ast.AST) -> Any:
        """维护父节点引用后继续标准 AST 访问，以识别 awaited 调用。"""

        if self._parents:
            parent = self._parents[-1]
            setattr(node, "_catalog_parent", parent)
        self._parents.append(node)
        try:
            return super().visit(node)
        finally:
            self._parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        """记录当前同步函数作用域并扫描其全部子节点。"""

        self._functions.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        """记录当前异步函数作用域并扫描其全部子节点。"""

        self._functions.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._functions.pop()

    def visit_Return(self, node: ast.Return) -> Any:
        """在 get_hooks 中提取字面量返回映射，并继续扫描返回表达式。"""

        if self._functions and self._functions[-1] == "get_hooks":
            self._collect_get_hooks_return(node)
        self.generic_visit(node)

    def _collect_get_hooks_return(self, node: ast.Return) -> None:
        """从插件 get_hooks 的静态字典返回值中收集注册事实。"""

        if not isinstance(node.value, ast.Dict):
            if node.value is not None:
                self.dynamic_references.append(
                    _location(
                        self.root,
                        self.path,
                        node,
                        kind="registration",
                        api="get_hooks",
                        expression=_expression_text(node.value),
                    )
                )
            return
        for key_node, value_node in zip(node.value.keys, node.value.values):
            if key_node is None:
                continue
            event = _event_value(key_node, self.enum_values)
            if event is None:
                self.dynamic_references.append(
                    _location(
                        self.root,
                        self.path,
                        key_node,
                        kind="registration",
                        api="get_hooks",
                        expression=_expression_text(key_node),
                    )
                )
                continue
            self.registrations.append(
                _location(
                    self.root,
                    self.path,
                    key_node,
                    event=event,
                    api="get_hooks",
                    handlers=_handler_names(value_node),
                    source="plugin-literal",
                    priority=0,
                )
            )

    def visit_Call(self, node: ast.Call) -> Any:
        """识别 Registry、trigger 和包装器调用并分派到专用收集逻辑。"""

        qualified = _attribute_name(node.func)
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""

        if qualified.endswith("HookRegistry.register"):
            self._collect_direct_registration(node, qualified)
        elif any(
            qualified.endswith(f"HookRegistry.{candidate}")
            for candidate in DIRECT_EMIT_METHODS
        ):
            self._collect_direct_emission(node, qualified, method)
        elif method == "trigger":
            self._collect_trigger(node, qualified)
        elif self._is_hook_wrapper(method or _attribute_name(node.func)):
            self._collect_wrapper_emission(node, qualified or method)

        self.generic_visit(node)

    @staticmethod
    def _is_hook_wrapper(name: str) -> bool:
        """判断调用名是否符合内部异步 Hook 包装器命名约定。"""

        leaf = name.rsplit(".", 1)[-1]
        return leaf.startswith("_emit_") and leaf.endswith("_hook")

    def _collect_direct_registration(self, call: ast.Call, api: str) -> None:
        """收集 HookRegistry.register 的静态事件、Handler 和优先级。"""

        event_node = _call_argument(call, 0, "event")
        event = _event_value(event_node, self.enum_values)
        if event is None:
            self._record_dynamic(call, "registration", api, event_node)
            return
        self.registrations.append(
            _location(
                self.root,
                self.path,
                call,
                event=event,
                api=api,
                handlers=[_expression_text(_call_argument(call, 2, "handler"))],
                source=str(_literal_scalar(_call_argument(call, 3, "source")) or "<dynamic>"),
                priority=_literal_scalar(_call_argument(call, 4, "priority")),
            )
        )

    def _collect_direct_emission(self, call: ast.Call, api: str, method: str) -> None:
        """收集 HookRegistry 直接分发调用的事件和执行模式。"""

        event_node = _call_argument(call, 0, "event")
        event = _event_value(event_node, self.enum_values)
        if event is None:
            self._record_dynamic(call, "emission", api, event_node)
            return
        mode = DIRECT_EMIT_METHODS[method]
        if method == "dispatch":
            mode = self._mode(call)
        self.emissions.append(self._emission_location(call, event, api, mode))

    def _collect_trigger(self, call: ast.Call, api: str) -> None:
        """收集兼容 trigger 调用，并在缺省时按串行模式记录。"""

        event_node = _call_argument(call, 0, "event")
        event = _event_value(event_node, self.enum_values)
        if event is None:
            self._record_dynamic(call, "emission", api, event_node)
            return
        self.emissions.append(
            self._emission_location(call, event, api, self._mode(call, default="serial"))
        )

    def _collect_wrapper_emission(self, call: ast.Call, api: str) -> None:
        """收集 fire-and-forget 包装器触发的固定事件。"""

        event_node = _call_argument(call, 0, "event")
        event = _event_value(event_node, self.enum_values)
        if event is None:
            self._record_dynamic(call, "emission", api, event_node)
            return
        location = self._emission_location(call, event, api, "serial")
        location["delivery"] = "fire-and-forget-wrapper"
        self.emissions.append(location)

    def _mode(self, call: ast.Call, default: str = "dynamic") -> str:
        """从字符串或 HookDispatchMode 成员解析调用的分发模式。"""

        node = _keyword(call, "mode")
        if node is None:
            return default
        literal = _constant_string(node)
        if literal in {"serial", "parallel"}:
            return literal
        name = _attribute_name(node)
        if name.endswith("HookDispatchMode.SERIAL"):
            return "serial"
        if name.endswith("HookDispatchMode.PARALLEL"):
            return "parallel"
        return "dynamic"

    def _emission_location(
        self,
        call: ast.Call,
        event: str,
        api: str,
        mode: str,
    ) -> dict[str, Any]:
        """构造触发位置，并根据父节点标记调用是否被 await。"""

        parent = getattr(call, "_catalog_parent", None)
        return _location(
            self.root,
            self.path,
            call,
            event=event,
            api=api,
            mode=mode,
            delivery="awaited" if isinstance(parent, ast.Await) else "direct-or-scheduled",
        )

    def _record_dynamic(
        self,
        call: ast.Call,
        kind: str,
        api: str,
        event_node: ast.AST | None,
    ) -> None:
        """记录无法静态解析事件名的注册或触发引用。"""

        self.dynamic_references.append(
            _location(
                self.root,
                self.path,
                call,
                kind=kind,
                api=api,
                expression=_expression_text(event_node),
            )
        )


def _sorted_locations(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按事件、文件、行号、API 和完整内容稳定排序位置记录。"""

    return sorted(
        items,
        key=lambda item: (
            str(item.get("event", "")),
            str(item.get("file", "")),
            int(item.get("line", 0)),
            str(item.get("api", "")),
            json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
    )


def build_catalog(
    root: Path | None = None,
    source_roots: Sequence[Path] = DEFAULT_SOURCE_ROOTS,
) -> dict[str, Any]:
    """扫描 Hook/Event 声明与引用，构建不导入应用模块的静态目录。"""

    repo_root = (root or _repo_root()).resolve()
    hooks_path = repo_root / "tcode_agent/plugins/hooks.py"
    hooks_source, hooks_tree = _parse_source(hooks_path)
    enum_values = _parse_enum_values(hooks_tree, "HookEvent")
    categories = set(_parse_enum_values(hooks_tree, "HookCategory").values())
    descriptions = _parse_descriptions(hooks_tree, enum_values)
    parallel_allowed_events = _parse_parallel_allowed_events(hooks_tree, enum_values)
    # 第二批完成后不再维护独立 Event Canary；并行只由 EventSpec 与调用点的
    # 显式 mode 共同决定，静态目录继续保留空集合字段以兼容目录消费者。
    parallel_canary_events: set[str] = set()

    registrations: list[dict[str, Any]] = []
    emissions: list[dict[str, Any]] = []
    dynamic_references: list[dict[str, Any]] = []
    scanned_files: list[str] = []

    for path in _iter_python_files(repo_root, source_roots):
        _, tree = _parse_source(path)
        scanned_files.append(_relative(path, repo_root))
        visitor = _ReferenceVisitor(
            root=repo_root,
            path=path,
            enum_values=enum_values,
            parallel_canary_events=parallel_canary_events,
        )
        visitor.visit(tree)
        registrations.extend(visitor.registrations)
        emissions.extend(visitor.emissions)
        dynamic_references.extend(visitor.dynamic_references)

    registrations = _sorted_locations(registrations)
    emissions = _sorted_locations(emissions)
    dynamic_references = _sorted_locations(dynamic_references)
    by_event_registrations: dict[str, list[dict[str, Any]]] = {}
    by_event_emissions: dict[str, list[dict[str, Any]]] = {}
    for item in registrations:
        by_event_registrations.setdefault(item["event"], []).append(item)
    for item in emissions:
        by_event_emissions.setdefault(item["event"], []).append(item)

    all_events = sorted(
        set(enum_values.values())
        | set(by_event_registrations)
        | set(by_event_emissions)
    )
    declared_events = set(enum_values.values())
    event_records: list[dict[str, Any]] = []
    for event in all_events:
        event_registrations = by_event_registrations.get(event, [])
        event_emissions = by_event_emissions.get(event, [])
        declared = event in declared_events
        if event_emissions and event_registrations:
            status = "emitted-and-registered"
        elif event_emissions:
            status = "emitted"
        elif event_registrations:
            status = "registered-only"
        elif declared:
            status = "declared-only"
        else:
            status = "custom"
        category = event.split(".", 1)[0]
        if declared and category not in categories:
            raise RuntimeError(f"unsupported HookEvent category for {event}: {category}")
        event_records.append(
            {
                "event": event,
                "category": category,
                "declared": declared,
                "description": descriptions.get(
                    event,
                    event.replace(".", " ").capitalize() + ".",
                ),
                "required_input": (
                    _parse_required_input(hooks_tree, event, enum_values)
                    if declared
                    else []
                ),
                "allowed_modes": (
                    ["serial", "parallel"]
                    if event in parallel_allowed_events
                    else (["serial"] if declared else [])
                ),
                "recommended_mode": "serial" if declared else None,
                "canary_status": (
                    "gated-default-off"
                    if event in parallel_canary_events
                    else "not-selected"
                ),
                "status": status,
                "current_modes": sorted(
                    {str(item["mode"]) for item in event_emissions}
                ),
                "registrations": event_registrations,
                "emissions": event_emissions,
            }
        )

    summary = {
        "declared_event_count": len(declared_events),
        "catalog_event_count": len(event_records),
        "emitted_event_count": sum(bool(item["emissions"]) for item in event_records),
        "registered_event_count": sum(
            bool(item["registrations"]) for item in event_records
        ),
        "declared_only_count": sum(
            item["status"] == "declared-only" for item in event_records
        ),
        "registered_only_count": sum(
            item["status"] == "registered-only" for item in event_records
        ),
        "custom_event_count": sum(not item["declared"] for item in event_records),
        "dynamic_reference_count": len(dynamic_references),
        "parallel_allowed_event_count": len(parallel_allowed_events),
        "parallel_canary_event_count": len(parallel_canary_events),
        "scanned_file_count": len(scanned_files),
    }
    return {
        "schema_version": 3,
        "generator_version": GENERATOR_VERSION,
        "source_roots": [path.as_posix() for path in source_roots],
        "runtime_spec_source": "tcode_agent/plugins/hooks.py",
        "runtime_spec_digest": _runtime_spec_digest(hooks_source, hooks_tree),
        "parallel_canary_source": "",
        "parallel_canary_digest": "",
        "summary": summary,
        "events": event_records,
        "dynamic_references": dynamic_references,
    }


def render_json(catalog: dict[str, Any]) -> str:
    """把 Hook/Event 目录渲染为键排序的稳定 JSON。"""

    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _markdown_cell(value: Any) -> str:
    """转义适合 Markdown 表格单元格的短文本。"""

    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(catalog: dict[str, Any]) -> str:
    """渲染 Hook/Event 摘要、活动事件和动态引用说明。"""

    summary = catalog["summary"]
    lines = [
        "# Hook/Event Catalog",
        "",
        "状态：`Generated`",
        "",
        "> 本文件由 `python scripts/dsh.py catalog` 静态生成，禁止手工修改。",
        "> Catalog 描述当前源码事实与 EventSpec 模式声明；生产事件不再经过独立 Canary。",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in sorted(summary):
        lines.append(f"| `{key}` | {summary[key]} |")
    lines.extend(
        [
            "",
            "## Events",
            "",
            "| Event | Category | Status | Current | Allowed | Recommended | Canary | Required input | Registrations | Emissions |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for item in catalog["events"]:
        modes = ", ".join(item["current_modes"]) or "-"
        allowed = ", ".join(item["allowed_modes"]) or "-"
        recommended = item["recommended_mode"] or "-"
        required = ", ".join(item["required_input"]) or "-"
        lines.append(
            "| `{event}` | {category} | `{status}` | {modes} | {allowed} | {recommended} | {canary} | {required} | {registrations} | {emissions} |".format(
                event=_markdown_cell(item["event"]),
                category=_markdown_cell(item["category"]),
                status=_markdown_cell(item["status"]),
                modes=_markdown_cell(modes),
                allowed=_markdown_cell(allowed),
                recommended=_markdown_cell(recommended),
                canary=_markdown_cell(item["canary_status"]),
                required=_markdown_cell(required),
                registrations=len(item["registrations"]),
                emissions=len(item["emissions"]),
            )
        )

    active = [
        item
        for item in catalog["events"]
        if item["registrations"] or item["emissions"]
    ]
    lines.extend(["", "## Active event details", ""])
    for item in active:
        lines.extend(
            [
                f"### `{item['event']}`",
                "",
                f"- Status: `{item['status']}`",
                f"- Description: {item['description']}",
                "- Required input: "
                + (", ".join(f"`{key}`" for key in item["required_input"]) or "none"),
                "- Current modes: "
                + (", ".join(f"`{mode}`" for mode in item["current_modes"]) or "none"),
                "- Allowed modes: "
                + (", ".join(f"`{mode}`" for mode in item["allowed_modes"]) or "none"),
                f"- Recommended mode: `{item['recommended_mode'] or 'none'}`",
                f"- Parallel canary: `{item['canary_status']}`",
            ]
        )
        if item["registrations"]:
            lines.append("- Registrations:")
            for reference in item["registrations"]:
                handlers = ", ".join(reference.get("handlers", [])) or "<dynamic>"
                lines.append(
                    f"  - `{reference['file']}:{reference['line']}` via `{reference['api']}`; handlers: `{handlers}`"
                )
        if item["emissions"]:
            lines.append("- Emissions:")
            for reference in item["emissions"]:
                lines.append(
                    f"  - `{reference['file']}:{reference['line']}` via `{reference['api']}`; mode `{reference['mode']}`; delivery `{reference['delivery']}`"
                )
        lines.append("")

    lines.extend(
        [
            "## Dynamic references",
            "",
            "这些调用的事件名无法通过静态分析确定。它们不会被猜测为某个固定事件。",
            "",
            "| Kind | API | Expression | Location |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in catalog["dynamic_references"]:
        lines.append(
            f"| {item['kind']} | `{_markdown_cell(item['api'])}` | `{_markdown_cell(item['expression'])}` | `{item['file']}:{item['line']}` |"
        )
    lines.extend(
        [
            "",
            "## Status definitions",
            "",
            "- `emitted-and-registered`: 源码中同时发现触发点和静态注册点。",
            "- `emitted`: 发现触发点，但当前仓库未发现静态插件注册。",
            "- `registered-only`: 发现静态插件注册，但未发现固定事件触发点。",
            "- `declared-only`: HookEvent 已声明，但没有发现固定注册或触发点。",
            "- `custom`: 未在 HookEvent 中声明的静态自定义事件。",
            "",
        ]
    )
    return "\n".join(lines)


def _check_file(path: Path, expected: str) -> bool:
    """只读检查生成文件是否存在且与期望字节完全一致。"""

    if not path.is_file():
        print(f"missing generated file: {path}", file=sys.stderr)
        return False
    actual = path.read_text(encoding="utf-8")
    if actual == expected:
        return True
    print(f"generated file is stale: {path}", file=sys.stderr)
    return False


def _write_file(path: Path, content: str) -> None:
    """创建父目录并以 UTF-8 写入生成文本。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """生成独立 Hook/Event 目录，或执行只读漂移检查。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_repo_root())
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_PATH)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    json_path = args.json_output
    markdown_path = args.markdown_output
    if not json_path.is_absolute():
        json_path = root / json_path
    if not markdown_path.is_absolute():
        markdown_path = root / markdown_path

    catalog = build_catalog(root)
    json_content = render_json(catalog)
    markdown_content = render_markdown(catalog)
    if args.check:
        valid = _check_file(json_path, json_content)
        valid = _check_file(markdown_path, markdown_content) and valid
        return 0 if valid else 1

    _write_file(json_path, json_content)
    _write_file(markdown_path, markdown_content)
    print(f"generated {json_path.relative_to(root)}")
    print(f"generated {markdown_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
