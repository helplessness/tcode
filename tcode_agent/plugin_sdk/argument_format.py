"""按工具 Schema 恢复无歧义的 JSON 表示，不补科学结论或裁剪证据。"""
import json
import math
import re
from jsonschema import Draft202012Validator


def restore_missing_object_close(raw, schema):
    """只恢复一枚遗漏的对象右括号；完整 Schema 下有多个解释就拒绝。"""
    if (schema.get("x-tcode-argument-format") != "v1" or not schema.get("properties") or not isinstance(raw, str)
            or len(raw) > 32000):
        return None
    positions, quoted, escaped = [], False, False
    for index, char in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in ",]}":
            positions.append(index)
    if quoted or len(positions) > 512:
        return None  # 未闭合字符串或超大参数不能猜补或截断。
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    validator, recovered = Draft202012Validator(schema), None
    for index in positions:
        try:
            value = json.loads(raw[:index] + "}" + raw[index:], object_pairs_hook=unique_keys,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(value, dict) and validator.is_valid(value):
            if recovered is not None and recovered != value:
                return None
            recovered = value
    return recovered


def _object_properties(schema):
    if schema.get("type") == "object":
        return schema.get("properties", {})
    for branch in schema.get("anyOf", []):
        if branch.get("type") == "object" and branch.get("properties"):
            return branch["properties"]
    return {}


def _restore_field_placement(value, schema, repairs, path):
    """仅在声明的单对象信封内搬移字段；不穿越数组、不猜实体、不丢弃冲突值。"""
    properties = schema.get("properties", {})
    children = {key: _object_properties(properties.get(key, {}))
                for key in schema.get("x-field-placement", []) if isinstance(value.get(key), dict)}
    if len(children) != 1:
        return value
    child, fields = next(iter(children.items()))
    nested = value[child]
    # 子对象不拥有、但外层契约明确拥有的字段，归还信封。
    for key in list(nested):
        if key in properties and key not in fields:
            item = normalize_argument_format(nested[key], properties[key])
            if key not in value or (type(value[key]) is type(item) and value[key] == item):
                value[key] = item
                del nested[key]
                repairs.append(f"{path}.{child}.{key}: 恢复至外层")
    # 外层不拥有的科学字段只放入当前唯一子对象；不创建新候选/机制。
    for key in list(value):
        header_identity = key in schema.get("x-field-placement-prefer-child", {}).get(child, [])
        if key in fields and (key not in properties or header_identity):
            item = normalize_argument_format(value[key], fields[key])
            if key not in nested or (type(nested[key]) is type(item) and nested[key] == item):
                nested[key] = item
                del value[key]
                repairs.append(f"{path}.{key}: 恢复至 {child}")
    return value


def normalize_argument_format(value, schema, *, run_id=None, repairs=None, path="$"):
    repairs = repairs if repairs is not None else []
    # 草稿接收分支只处理 JSON 表示；仍按首个完整对象契约递归恢复字段格式。
    if "type" not in schema:
        schema = next((branch for branch in schema.get("anyOf", [])
                       if branch.get("type") in {"object", "array"} and branch.get("properties")), schema)
    kind = schema.get("type")
    if kind in ("object", "array") and isinstance(value, str):
        raw = value.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            parsed = value
            # 只补数组最外层的右括号，内部有缺失或歧义时仍交原校验拒绝。
            if kind == "array" and error.pos == len(raw) and raw.startswith("[") and raw.endswith(("}", "]")):
                try:
                    parsed = json.loads(raw + "]")
                except json.JSONDecodeError:
                    pass
        if (kind == "object" and isinstance(parsed, dict)) or (kind == "array" and isinstance(parsed, list)):
            value = parsed
            repairs.append(path + ": JSON文本恢复")
    if kind in ("number", "integer") and isinstance(value, str):
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", value.strip()):
            try:
                parsed = json.loads(value.strip())
            except ValueError:
                parsed = None
            if isinstance(parsed, (int, float)) and (isinstance(parsed, int) or math.isfinite(parsed)) and (kind == "number" or isinstance(parsed, int)):
                value = parsed
                repairs.append(path + ": 数值文本恢复")
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        value = value.strip().lower() == "true"
        repairs.append(path + ": 布尔文本恢复")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        result = {}
        for key, item in value.items():
            field = properties.get(key, {})
            # 显式选择加入的封闭对象可省略无内容的未知空字段；不猜字段名，
            # 非空未知字段、必填字段和已知字段仍交完整 Schema 严格校验。
            if (item is None and key not in properties and schema.get("additionalProperties") is False
                    and schema.get("x-drop-unknown-null") is True):
                repairs.append(path + "." + key + ": 省略未声明的空字段")
                continue
            if item is None and key in properties and key not in required and field.get("type") in ("array", "object"):
                repairs.append(path + "." + key + ": 去除可选空值")
                continue
            if (key == "run_id" and type(item) is int and (run_id is None or str(item) == run_id)):
                item = run_id if run_id is not None else str(item)
                repairs.append(path + ".run_id: 恢复当前运行字符串ID")
            result[key] = normalize_argument_format(item, field, run_id=run_id, repairs=repairs, path=path + "." + key)
        return _restore_field_placement(result, schema, repairs, path)
    if isinstance(value, list):
        return [normalize_argument_format(item, schema.get("items", {}), run_id=run_id,
                                         repairs=repairs, path=f"{path}[{index}]")
                for index, item in enumerate(value)]
    return value
