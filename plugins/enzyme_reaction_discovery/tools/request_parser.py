"""插件私有的 SMILES 文本提取；不是结构解析或化学推断。"""
from __future__ import annotations

import re

_REACTION_SIDE_LABEL = r"(?:reactants?|substrates?|products?|反应物|底物|目标产物|产物)"
_REACTION_SIDE_LINE_PATTERN = re.compile(
    r"(?<![\w])(?P<label>" + _REACTION_SIDE_LABEL + r")"
    r"[ \t]*(?:smiles?)?[ \t]*[:：][ \t]*(?P<value>[^\r\n。]+?)"
    r"(?=\s*[;；]\s*" + _REACTION_SIDE_LABEL + r"\s*(?:smiles?)?\s*[:：]|[\r\n。]|$)",
    flags=re.IGNORECASE | re.MULTILINE,
)
_REACTION_SIDE_HEADING_PATTERN = re.compile(
    r"^[ \t]*(?P<label>" + _REACTION_SIDE_LABEL + r")"
    r"\s*(?:smiles?)?\s*[:：][ \t]*(?P<value>[^\r\n]*)$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_NUMBERED_SMILES_PATTERN = re.compile(
    r"^[ \t]*(?:[-*•][ \t]+|\d{1,2}[.)、][ \t]*)(?P<value>\S.*?)[ \t]*$"
)
# 这只是词法边界，不是化学校验器；不得将分子名猜成结构，也不得丢弃带电组分。
_SMILES_TEXT = re.compile(r"(?:\[[A-Za-z0-9@+*\-:]+\]|Br|Cl|[BCNOFPSIHKbcnops0-9@+\-=#$%\\/().:*])+")
_TARGET_REACTION_BLOCK = re.compile(
    r"(?:^|\n)[ \t]*(?:目标反应|反应式|reaction)[ \t]*[:：][ \t]*\n+"
    r"(?P<equation>.*?)(?=\n[ \t]*\n|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)
_REACTION_ARROW = re.compile(r"(?:<=>|<->|=>|->|→|⇌|↔)")
_NAMED_SMILES_LINE = re.compile(
    r"^[ \t]*(?P<name>[^\r\n:：]{1,120}?)[ \t]+SMILES[ \t]*[:：][ \t]*(?P<smiles>\S+)[ \t]*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def extract_markdown_reaction_components(text: str) -> dict[str, list[dict]]:
    """保留编号实体与其结构表示；同一实体的参考结构不升级为额外反应物。"""
    source = str(text or "")
    result, ambiguous = {}, set()
    headings = list(re.finditer(
        r"^[ \t]*#{1,6}[ \t]+(?P<label>" + _REACTION_SIDE_LABEL + r")[ \t]*[:：]?[ \t]*$",
        source, re.I | re.M))
    structure = re.compile(r"(?P<label>[^\n:：]*?SMILES(?:[（(][^\n:：]*?[）)])?)[ \t]*[:：][ \t]*(?P<value>[^\r\n]+)", re.I)
    for heading in headings:
        side = "products" if heading["label"].lower() in {"product", "products", "产物", "目标产物"} else "reactants"
        remainder = source[heading.end():]
        end = re.search(r"^[ \t]*#{1,6}[ \t]+", remainder, re.M)
        block = remainder[:end.start()] if end else remainder
        items = list(re.finditer(r"^[ \t]*\d{1,2}[.)、][ \t]+(?P<name>[^\n]+)", block, re.M))
        if not items:
            continue
        components = []
        for index, item in enumerate(items):
            body = item["name"] + "\n" + block[item.end():items[index + 1].start() if index + 1 < len(items) else len(block)]
            rows = [(m["label"].strip(), m["value"].strip().strip("` ")) for m in structure.finditer(body)]
            valid = [(label, value) for label, value in rows if _SMILES_TEXT.fullmatch(value)]
            models = [(label, value) for label, value in valid if re.search(r"反应端|反应模型|模型|model", label, re.I)]
            chosen = models[0] if len(models) == 1 else valid[0] if len(valid) == 1 else None
            name = re.split(r"\s+SMILES", item["name"], maxsplit=1, flags=re.I)[0]
            component = {"name": name.strip().strip("*` "), "notes": []}
            if chosen:
                component["smiles"] = chosen[1]
                component["notes"].append("结构表示标签：" + chosen[0] + "；仅作文本表示，不等于完整实体结构已验证。")
            elif rows:
                component["notes"].append("同一实体存在多个或不可解析的结构表示，未自动选择；结构对应需核实。")
            for label, value in rows:
                if chosen != (label, value):
                    component["notes"].append("同条目的其他结构表示（非额外反应组分）：" + label + "：" + value)
            components.append(component)
        if side in result:
            ambiguous.add(side)
        result[side] = components
    return {side: rows for side, rows in result.items() if side not in ambiguous}


def extract_labeled_reaction_smiles(text: str) -> dict[str, list[str]]:
    """提取明确标注的反应物/产物 SMILES，不解释结构、角色或机制。"""

    source = str(text or "")
    result: dict[str, list[str]] = {}
    ambiguous: set[str] = set()

    def assign(side: str, values: list[str], *, explicit_list: bool = False) -> None:
        if side in result and result[side] != values:
            # 多行明确的 SMILES 标签表示组分列表；不推断无标签名称或结构。
            if explicit_list and len(result[side]) + len(values) <= 32:
                result[side].extend(values)
            else:
                ambiguous.add(side)
        elif values:
            result[side] = values

    # 单行反应 SMILES 的点号表示同一侧的独立组分，保留原始结构和顺序。
    for match in re.finditer(
        r"^[ \t]*(?:反应\s*SMILES|reaction\s*SMILES)[ \t]*[:：][ \t]*([^\r\n]+)",
        source, re.I | re.M,
    ):
        sides = match[1].strip(" `。；;").split(">>")
        if len(sides) != 2:
            continue
        groups = [[item.strip() for item in side.split(".")] for side in sides]
        if all(1 <= len(group) <= 32 and all(_SMILES_TEXT.fullmatch(item) for item in group)
               for group in groups):
            for side, group in zip(("reactants", "products"), groups, strict=True):
                assign(side, group)

    for match in _REACTION_SIDE_LINE_PATTERN.finditer(source):
        label = str(match.group("label") or "").strip().lower()
        side = "products" if label in {"product", "products", "产物", "目标产物"} else "reactants"
        raw = str(match.group("value") or "").strip()
        raw = raw.rstrip("。；; ").strip("` ")
        # 带空格的加号或分号是常见多组分分隔符；SMILES 内部电荷（如 [Na+]）保留。
        values = [
            item.strip().strip("` ")
            for item in re.split(r"\s+\+\s+|[;；]", raw)
        ]
        if (not values or len(values) > 32
                or any(not _SMILES_TEXT.fullmatch(value) for value in values)):
            ambiguous.add(side)
            continue
        explicit_list = bool(re.match(
            _REACTION_SIDE_LABEL + r"[ \t]+smiles?[ \t]*[:：]", match.group(0), flags=re.IGNORECASE))
        assign(side, values, explicit_list=explicit_list)

    # 聊天提示常将反应侧标题单独放一行，下方列编号 SMILES。
    # 只解析这种狭义已标注形式，任意散文不属于本文本适配器。
    headings = list(_REACTION_SIDE_HEADING_PATTERN.finditer(source))
    for index, match in enumerate(headings):
        if str(match.group("value") or "").strip():
            continue
        label = str(match.group("label") or "").strip().lower()
        side = "products" if label in {"product", "products", "产物", "目标产物"} else "reactants"
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        values: list[str] = []
        for line in source[match.end():end].splitlines():
            stripped = line.strip()
            if not stripped or stripped in {"```", "```text", "```smiles"}:
                continue
            item = _NUMBERED_SMILES_PATTERN.fullmatch(line)
            if item is None:
                if values:
                    break
                continue
            value = str(item.group("value") or "").strip().strip("` ")
            if not _SMILES_TEXT.fullmatch(value):
                ambiguous.add(side)
                values = []
                break
            values.append(value)
            if len(values) > 32:
                ambiguous.add(side)
                values = []
                break
        assign(side, values)
    # Markdown 的反应侧标题下可列出“名称 SMILES：结构”；仅提取显式结构。
    markdown_headings = list(re.finditer(
        r"^[ \t]*#{1,6}[ \t]+(?P<label>" + _REACTION_SIDE_LABEL + r")[ \t]*[:：]?[ \t]*$",
        source, flags=re.IGNORECASE | re.MULTILINE,
    ))
    for heading in markdown_headings:
        label = heading.group("label").lower()
        side = "products" if label in {"product", "products", "产物", "目标产物"} else "reactants"
        remainder = source[heading.end():]
        next_heading = re.search(r"^[ \t]*#{1,6}[ \t]+", remainder, flags=re.MULTILINE)
        block = remainder[:next_heading.start()] if next_heading else remainder
        values = []
        for match in re.finditer(r"\bSMILES[ \t]*[:：][ \t]*([^\r\n]+)", block, flags=re.IGNORECASE):
            value = match.group(1).strip().strip("` ")
            if not _SMILES_TEXT.fullmatch(value):
                ambiguous.add(side)
                break
            values.append(value)
        if values and len(values) <= 32:
            assign(side, values)
    return {key: value for key, value in result.items() if key not in ambiguous}


def extract_explicit_reaction_components(text: str) -> dict[str, list[dict[str, str]]]:
    """从明确标注的反应方程式提取名称，不解析名称、推断结构或猜测任意散文。"""

    source = str(text or "")
    block = _TARGET_REACTION_BLOCK.search(source)
    if block is None:
        return {}
    equation = " ".join(
        line.strip() for line in str(block.group("equation") or "").splitlines()
        if line.strip()
    )
    sides = _REACTION_ARROW.split(equation, maxsplit=1)
    if len(sides) != 2:
        return {}
    named_smiles = {
        match.group("name").strip(): match.group("smiles").strip().strip("` ")
        for match in _NAMED_SMILES_LINE.finditer(source)
        if _SMILES_TEXT.fullmatch(match.group("smiles").strip().strip("` "))
    }
    result: dict[str, list[dict[str, str]]] = {}
    for side, raw_side in zip(("reactants", "products"), sides, strict=True):
        names = [item.strip(" `。；;") for item in re.split(r"\s+\+\s+", raw_side)]
        if not names or len(names) > 32 or any(not name for name in names):
            return {}
        result[side] = [
            {"name": name, **({"smiles": named_smiles[name]} if name in named_smiles else {})}
            for name in names
        ]
    return result
