"""工具参数的生产级只校验适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable


@dataclass(frozen=True, slots=True)
class ToolArgumentValidation:
    """保存不修改参数的校验结果。"""

    accepted: bool
    reason: str = ""
    location: str = ""
    validator: str = "json-schema-2020-12"
    skipped: bool = False


def allow_tool_correction(counts, name, slot, error):
    """不同错误分别纠正；同错最多纠正两次，每工具每阶段最多四次失败。"""
    total = (name, "total_errors")
    signature = (name, f"{slot}:{error}")
    counts[total] = counts.get(total, 0) + 1
    counts[signature] = counts.get(signature, 0) + 1
    return counts[total] <= 4 and counts[signature] <= 2


def _tool_schema(tool: Any) -> dict[str, Any] | None:
    """从工具公开参数类型提取 JSON Schema。

    参数:
        tool: 已由真实执行注册表解析的工具对象。

    返回:
        可校验的对象 Schema；没有公开 Schema 时返回 ``None``。
    """

    if isinstance(getattr(tool, "_input_schema", None), dict):
        return dict(tool._input_schema)
    parameters = getattr(tool, "parameters", None)
    if parameters is None:
        return None
    if callable(getattr(parameters, "model_json_schema", None)):
        schema = parameters.model_json_schema()
    elif callable(getattr(parameters, "schema", None)):
        schema = parameters.schema()
    else:
        return None
    return dict(schema) if isinstance(schema, dict) else None


def validate_tool_arguments(tool: Any, arguments: Any) -> ToolArgumentValidation:
    """使用真实执行 Schema 校验 post-hook 参数但不写回默认值。

    参数:
        tool: 真实执行注册表返回的本地、SDK 或 MCP 工具；``None`` 表示
            尚未解析的路由。
        arguments: Hook 完成后的原始参数映射。

    返回:
        接受、拒绝或跳过结果。Schema 自身非法时失败关闭。
    """

    if tool is None:
        return ToolArgumentValidation(accepted=True, skipped=True)
    if not isinstance(arguments, dict):
        return ToolArgumentValidation(
            accepted=False,
            reason="工具参数必须是 JSON 对象",
            location="arguments",
        )
    proxy_validator = getattr(tool, "validate_proxy_arguments", None)
    if callable(proxy_validator):
        proxy_validation = proxy_validator(arguments)
        if proxy_validation is not None:
            return proxy_validation
    schema = _tool_schema(tool)
    if schema is None:
        return ToolArgumentValidation(accepted=True, skipped=True)
    try:
        normalizer = getattr(tool, "arguments_for_validation", None)
        if callable(normalizer):
            arguments = normalizer(arguments)
        validator = validator_for(schema, default=Draft202012Validator)
        validator.check_schema(schema)
        # MCP schemas are untrusted: local $defs work, but validation must not
        # fetch external references (network I/O or internal-service access).
        errors = list(validator(schema, registry=Registry()).iter_errors(arguments))
    except (SchemaError, Unresolvable):
        return ToolArgumentValidation(
            accepted=False,
            reason="工具参数 Schema 无效或引用无法本地解析，已阻止执行",
            location="schema",
        )
    if not errors:
        return ToolArgumentValidation(accepted=True)
    selected = best_match(errors) or errors[0]
    suffix = ".".join(str(item) for item in selected.absolute_path)
    location = f"arguments.{suffix}" if suffix else "arguments"
    return ToolArgumentValidation(
        accepted=False,
        reason=str(selected.message or "工具参数不符合 Schema")[:500],
        location=location,
    )


__all__ = ["ToolArgumentValidation", "validate_tool_arguments"]
