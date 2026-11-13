"""把嵌套 Schema 错误变为有界字段反馈，不回显整份科学参数。"""
from collections import defaultdict
from jsonschema import Draft202012Validator


def validation_issues(schema, value, prefix="$"):
    """仅展开当前类型对应的 oneOf 分支；返回路径、规则和期望值。"""
    def leaves(error):
        if error.validator in {"oneOf", "anyOf"} and error.context:
            groups = defaultdict(list)
            for child in error.context:
                groups[child.schema_path[0]].append(child)
            branches = error.schema[error.validator]
            matching = []
            if isinstance(error.instance, dict):
                for index, branch in enumerate(branches):
                    discriminators = {k: s["const"] for k, s in branch.get("properties", {}).items()
                                      if "const" in s and k in error.instance}
                    if "candidate_kind" in discriminators:
                        discriminators = {"candidate_kind": discriminators["candidate_kind"]}
                    if discriminators and all(error.instance[k] == v for k, v in discriminators.items()):
                        matching.append(index)
            chosen = matching if matching else [min(groups, key=lambda i: len(groups[i]))]
            for index in chosen:
                for child in groups.get(index, []):
                    yield from leaves(child)
        else:
            yield error

    rows = []
    for root in Draft202012Validator(schema).iter_errors(value):
        for error in leaves(root):
            path = prefix + ''.join(f'[{p}]' if isinstance(p, int) else '.' + str(p)
                                    for p in error.absolute_path)
            rule = error.validator
            expected = error.validator_value
            if rule == "required":
                for field in expected:
                    if field not in error.instance:
                        rows.append({"path": path + '.' + field, "rule": rule, "message": "缺少必填字段"})
                continue
            if rule == "additionalProperties":
                extras = sorted(set(error.instance) - set(error.schema.get("properties", {})))
                message = "未知字段：" + '、'.join(str(k)[:60] for k in extras[:8])
            elif rule in {"maxItems", "minItems", "maxLength", "minLength"}:
                message = f"{rule}={expected}; actual={len(error.instance)}"
                if rule == "maxItems" and path == "$.queries":
                    message += "; 按当前缺口选取限定数量的问题；不得原样重试"
            elif rule == "type":
                message = f"expected {expected}; actual {type(error.instance).__name__}"
            elif rule in {"enum", "const", "pattern", "minimum", "maximum"}:
                message = f"expected {rule}={expected}"
            else:
                message = "不符合字段契约"
            rows.append({"path": path, "rule": rule, "message": message[:240]})
    unique = {tuple(row.values()): row for row in rows}
    return list(unique.values())[:8]


def draft_input_schema(schema):
    """公开完整生成契约，同时允许对象草稿进入处理器领取纠错回执。

    第二分支不授权落盘；处理器必须再用原始完整 Schema 校验。
    """
    return {"anyOf": [schema, {"type": "object", "description":
            "未通过上述契约的对象仅作为草稿接收，工具将逐字段返回纠错请求；不会作为有效科学结果保存。"}]}
