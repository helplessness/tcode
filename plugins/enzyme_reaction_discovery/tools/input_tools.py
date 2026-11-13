"""Conservative text-only reaction input validation.

This module deliberately does not claim chemical structure validation.  It only
normalizes the public input contract and performs checks that are safe without
RDKit or atom mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from .request_parser import extract_explicit_reaction_components, extract_labeled_reaction_smiles, extract_markdown_reaction_components


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALLOWED_ROLES = {
    "substrate",
    "co_substrate",
    "target",
    "byproduct",
    "salt",
    "solvent",
    "spectator",
    "unknown",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _balanced(text: str, opening: str, closing: str) -> bool:
    depth = 0
    for character in text:
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _component(
    raw: Any,
    *,
    side: str,
    index: int,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    assumptions: list[str],
) -> dict[str, Any] | None:
    location = f"{side}[{index}]"
    if isinstance(raw, str):
        # The public input accepts the compact form ``["SMILES"]`` at the
        # plugin boundary. Keep it backwards compatible and
        # normalize it deterministically instead of making the model discover
        # the component schema through repeated tool calls.
        raw = {"smiles": raw}
        warnings.append(
            {
                "code": "smiles_shorthand_normalized",
                "location": location,
                "message": "字符串 SMILES 简写已规范化为组分对象；原始文本未改写。",
            }
        )
    if not isinstance(raw, Mapping):
        errors.append({"code": "component_not_object", "location": location, "message": "反应组分必须是对象。"})
        return None
    unknown_keys = sorted(
        set(raw) - {"smiles", "name", "role", "stoichiometry", "notes"}
    )
    if unknown_keys:
        errors.append({"code": "unknown_component_fields", "location": location, "message": f"未知字段：{', '.join(unknown_keys)}"})
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        errors.append({"code": "invalid_name", "location": f"{location}.name", "message": "name 必须是字符串或 null。"})
        name = None
    name = name.strip() if isinstance(name, str) else None
    smiles = raw.get("smiles")
    if smiles is not None and not isinstance(smiles, str):
        errors.append({"code": "invalid_smiles", "location": f"{location}.smiles", "message": "smiles 必须是字符串或 null。"})
        smiles = ""
    smiles = smiles.strip() if isinstance(smiles, str) else ""
    if not smiles and not name:
        errors.append({"code": "empty_component_identity", "location": location, "message": "组分至少需要非空 name 或 SMILES。"})
        return None
    if not smiles:
        assumption = f"{location}（{name}）仅提供名称，结构与原子映射尚未核验。"
        if assumption not in assumptions:
            assumptions.append(assumption)
        warnings.append({"code": "missing_smiles_preserved", "location": f"{location}.smiles", "message": "该组分仅提供名称；已保留进入流程，结构相关判断必须标为未核验。"})
    if smiles and len(smiles) > 4000:
        errors.append({"code": "smiles_too_long", "location": f"{location}.smiles", "message": "SMILES 超过 4000 字符。"})
    if smiles and _CONTROL_CHARACTERS.search(smiles):
        errors.append({"code": "control_character", "location": f"{location}.smiles", "message": "SMILES 含控制字符。"})
    if smiles and not _balanced(smiles, "(", ")"):
        errors.append({"code": "unbalanced_parentheses", "location": f"{location}.smiles", "message": "SMILES 圆括号不配对。"})
    if smiles and not _balanced(smiles, "[", "]"):
        errors.append({"code": "unbalanced_brackets", "location": f"{location}.smiles", "message": "SMILES 方括号不配对。"})
    if smiles and ">" in smiles:
        errors.append({"code": "reaction_separator_in_component", "location": f"{location}.smiles", "message": "单个组分中不得包含反应分隔符。"})
    if smiles and "." in smiles:
        assumption = f"{location} 含点分组分；V1 原样保留，不自动拆盐或判定旁观离子。"
        if assumption not in assumptions:
            assumptions.append(assumption)
        warnings.append({"code": "dot_disconnected_component", "location": f"{location}.smiles", "message": "检测到点分组分；各片段角色将作为未决歧义保留，不会阻塞自动流程。"})
    role = str(raw.get("role") or "unknown").strip()
    if role not in _ALLOWED_ROLES:
        errors.append({"code": "invalid_role", "location": f"{location}.role", "message": f"不支持的组分角色：{role}"})
        role = "unknown"
    if role == "unknown":
        assumption = f"{location} 的角色尚未确定。"
        if assumption not in assumptions:
            assumptions.append(assumption)
    stoichiometry = raw.get("stoichiometry")
    if stoichiometry is not None and (
        isinstance(stoichiometry, bool)
        or not isinstance(stoichiometry, (int, float))
        or float(stoichiometry) <= 0
    ):
        errors.append({"code": "invalid_stoichiometry", "location": f"{location}.stoichiometry", "message": "化学计量数必须是正数或 null。"})
        stoichiometry = None
    notes = raw.get("notes") or []
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        errors.append({"code": "invalid_notes", "location": f"{location}.notes", "message": "notes 必须是字符串数组。"})
        notes = []
    return {
        "smiles": smiles,
        "name": name,
        "role": role,
        "stoichiometry": stoichiometry,
        "notes": [item.strip() for item in notes if item.strip()],
    }


def validate_reaction_input(
    reaction: Any,
    *,
    configured_max_route_steps: int = 8,
) -> dict[str, Any]:
    """Normalize and conservatively validate one reaction input document."""

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(reaction, Mapping):
        return {
            "valid": False,
            "normalized_reaction": None,
            "errors": [{"code": "reaction_not_object", "location": "reaction", "message": "reaction 必须是对象。"}],
            "warnings": [],
            "validation_scope": {
                "text_checks_completed": True,
                "chemical_structure_validated": False,
                "atom_mapping_completed": False,
                "mass_balance_validated": False,
            },
            "has_unresolved_input_ambiguity": True,
        }
    source = deepcopy(dict(reaction))
    # A workflow may transport raw user text without asking the supervisor to
    # reconstruct domain fields. Only this plugin knows how to parse SMILES.
    request = source.get("natural_language_request")
    if isinstance(request, str):
        explicit_components = extract_explicit_reaction_components(request)
        for side, values in explicit_components.items():
            if side not in source:
                source[side] = values
                warnings.append({"code": "explicit_reaction_equation_extracted", "location": side,
                                 "message": "从明确标注的反应式提取组分名称；未从名称推断结构或角色。"})
        for side, values in extract_markdown_reaction_components(request).items():
            if side not in source or source.get(side) == []:
                source[side] = values
                warnings.append({"code": "markdown_components_extracted", "location": side,
                                 "message": "按编号保留实体及其结构表示；同条目的其他表示保留在 notes，不作为额外组分。"})
        for side, values in extract_labeled_reaction_smiles(request).items():
            if side not in source or source.get(side) == []:
                source[side] = values
                warnings.append({"code": "labeled_smiles_extracted", "location": side,
                                 "message": "从原始请求显式标注提取 SMILES；未推断结构或角色。"})
    if source.get("schema_version", "1.0") != "1.0":
        errors.append({"code": "unsupported_schema_version", "location": "schema_version", "message": "schema_version 必须为 1.0。"})
    unknown_keys = sorted(
        set(source)
        - {
            "schema_version",
            "reaction_id",
            "reactants",
            "products",
            "context",
            "component_assumptions",
            "user_confirmed",
            "target_description",
            "natural_language_request",
            "design_goal",
            "success_metrics",
            "substrate_or_product",
            "constraints",
            "reaction_name",
            "reactant_names",
            "product_names",
            "closed_knowledge_only",
        }
    )
    if unknown_keys:
        errors.append({"code": "unknown_reaction_fields", "location": "reaction", "message": f"未知字段：{', '.join(unknown_keys)}"})
    assumptions = source.get("component_assumptions") or []
    if not isinstance(assumptions, list) or not all(isinstance(item, str) for item in assumptions):
        errors.append({"code": "invalid_component_assumptions", "location": "component_assumptions", "message": "component_assumptions 必须是字符串数组。"})
        assumptions = []
    assumptions = [item.strip() for item in assumptions if item.strip()]
    normalized_sides: dict[str, list[dict[str, Any]]] = {}
    for side in ("reactants", "products"):
        raw_components = source.get(side)
        if isinstance(raw_components, (str, Mapping)):
            raw_components = [raw_components]
            warnings.append({"code": "single_component_normalized", "location": side,
                             "message": "单个 SMILES/组分已包成数组；不拆分、不改写原文。"})
        if not isinstance(raw_components, list):
            errors.append({"code": f"invalid_{side}_type", "location": side,
                           "message": f"{side} 必须是 SMILES 字符串、组分对象或数组。"})
            raw_components = []
        if not raw_components:
            errors.append({"code": f"empty_{side}", "location": side, "message": f"{side} 至少需要一个组分。"})
            raw_components = []
        if len(raw_components) > 32:
            errors.append({"code": f"too_many_{side}", "location": side, "message": f"{side} 最多允许 32 个组分。"})
        components: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_components[:32]):
            normalized = _component(
                raw,
                side=side,
                index=index,
                errors=errors,
                warnings=warnings,
                assumptions=assumptions,
            )
            if normalized is not None:
                components.append(normalized)
        normalized_sides[side] = components
    raw_context = source.get("context")
    if raw_context is None:
        raw_context = {}
    elif isinstance(raw_context, str):
        # Generic chat transport may supply free text here. Preserve it as
        # unverified notes, never interpret it as structured execution policy.
        note = raw_context.strip()
        raw_context = {"notes": [note[i:i + 1200] for i in range(0, len(note), 1200)]}
        warnings.append({"code": "context_text_preserved", "location": "context.notes",
                         "message": "context 文本已保留为未核验备注；未推断结构化条件或权限。"})
    if not isinstance(raw_context, Mapping):
        errors.append({"code": "invalid_context", "location": "context", "message": "context 必须是备注字符串、结构化对象或 null。"})
        raw_context = {}
    else:
        raw_context = deepcopy(dict(raw_context))
    # Preserve common descriptive transport annotations, not as verified facts
    # or runtime permission settings. Other unknown fields remain errors.
    for field, expected_type in (("reaction_name", str), ("reactant_names", list),
                                 ("product_names", list), ("closed_knowledge_only", bool)):
        if field not in source:
            continue
        value = source[field]
        if not isinstance(value, expected_type) or (
            expected_type is list and not all(isinstance(item, str) for item in value)
        ):
            errors.append({"code": f"invalid_{field}", "location": field,
                           "message": f"{field} 的描述字段类型不正确。"})
            continue
        notes = raw_context.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(f"Unverified input annotation {field}: {_stable_json(value)}")
    # These are transport fields emitted by the generic workflow_run tool.
    # Preserve their text, without interpreting it as verified chemistry or
    # changing execution permissions. Unknown reaction fields remain errors.
    for description_field in (
        "target_description",
        "natural_language_request",
        "design_goal",
        "success_metrics",
        "substrate_or_product",
        "constraints",
    ):
        target_description = source.get(description_field)
        if target_description is None:
            continue
        if description_field in {"constraints", "success_metrics"} and isinstance(target_description, list):
            if all(isinstance(item, str) for item in target_description):
                target_description = "\n".join(target_description)
        if not isinstance(target_description, str):
            errors.append(
                {
                    "code": f"invalid_{description_field}",
                    "location": description_field,
                    "message": (
                        f"{description_field} 必须是字符串、字符串数组或 null。"
                        if description_field in {"constraints", "success_metrics"}
                        else f"{description_field} 必须是字符串或 null。"
                    ),
                }
            )
        elif target_description.strip():
            context_notes = raw_context.get("notes") or []
            if isinstance(context_notes, list) and all(
                isinstance(item, str) for item in context_notes
            ):
                normalized_description = target_description.strip()
                # Schema limits each note to 1200 characters. Preserve long
                # transport text losslessly as ordered chunks; never truncate
                # a user request just to satisfy the notes schema.
                chunks = [
                    normalized_description[index:index + 1200]
                    for index in range(0, len(normalized_description), 1200)
                ]
                existing = set(context_notes)
                raw_context["notes"] = [
                    *context_notes,
                    *(chunk for chunk in chunks if chunk not in existing),
                ]
                warnings.append(
                    {
                        "code": f"{description_field}_preserved",
                        "location": "context.notes",
                        "message": f"顶层 {description_field} 已按 1200 字符分段、原样保留到 context.notes。",
                    }
                )
    unknown_context_keys = sorted(
        set(raw_context)
        - {
            "organism",
            "compartment",
            "ph",
            "temperature_c",
            "allowed_cofactors",
            "forbidden_cofactors",
            "single_enzyme_preferred",
            "max_route_steps",
            "notes",
        }
    )
    if unknown_context_keys:
        errors.append({"code": "unknown_context_fields", "location": "context", "message": f"未知字段：{', '.join(unknown_context_keys)}"})
    requested_steps = raw_context.get("max_route_steps", configured_max_route_steps)
    if isinstance(requested_steps, bool) or not isinstance(requested_steps, int) or not 1 <= requested_steps <= 8:
        errors.append({"code": "invalid_max_route_steps", "location": "context.max_route_steps", "message": "max_route_steps 必须是 1 到 8 的整数。"})
        requested_steps = configured_max_route_steps
    effective_steps = min(int(requested_steps), int(configured_max_route_steps))
    if effective_steps != requested_steps:
        warnings.append({"code": "route_steps_clamped", "location": "context.max_route_steps", "message": f"max_route_steps 已按插件配置限制为 {effective_steps}。"})
    organism = raw_context.get("organism")
    compartment = raw_context.get("compartment")
    for field, value in (("organism", organism), ("compartment", compartment)):
        if value is not None and (not isinstance(value, str) or len(value) > 500):
            errors.append({"code": f"invalid_{field}", "location": f"context.{field}", "message": f"{field} 必须是最多 500 字符的字符串或 null。"})
            if field == "organism":
                organism = None
            else:
                compartment = None
    def context_string_list(field: str) -> list[str]:
        raw_value = raw_context.get(field) or []
        if not isinstance(raw_value, list) or not all(isinstance(item, str) for item in raw_value):
            errors.append({"code": f"invalid_{field}", "location": f"context.{field}", "message": f"{field} 必须是字符串数组。"})
            return []
        return [item.strip() for item in raw_value if item.strip()]

    single_enzyme_preferred = raw_context.get("single_enzyme_preferred", False)
    if not isinstance(single_enzyme_preferred, bool):
        errors.append({"code": "invalid_single_enzyme_preferred", "location": "context.single_enzyme_preferred", "message": "single_enzyme_preferred 必须是布尔值。"})
        single_enzyme_preferred = False
    context = {
        "organism": organism,
        "compartment": compartment,
        "ph": raw_context.get("ph"),
        "temperature_c": raw_context.get("temperature_c"),
        "allowed_cofactors": context_string_list("allowed_cofactors"),
        "forbidden_cofactors": context_string_list("forbidden_cofactors"),
        "single_enzyme_preferred": single_enzyme_preferred,
        "max_route_steps": effective_steps,
        "notes": context_string_list("notes"),
    }
    if context["ph"] is not None and (
        isinstance(context["ph"], bool)
        or not isinstance(context["ph"], (int, float))
        or not 0 <= float(context["ph"]) <= 14
    ):
        errors.append({"code": "invalid_ph", "location": "context.ph", "message": "pH 必须在 0 到 14 之间或为 null。"})
        context["ph"] = None
    if context["temperature_c"] is not None and (
        isinstance(context["temperature_c"], bool)
        or not isinstance(context["temperature_c"], (int, float))
    ):
        errors.append({"code": "invalid_temperature", "location": "context.temperature_c", "message": "temperature_c 必须是数值或 null。"})
        context["temperature_c"] = None
    reaction_id = source.get("reaction_id")
    if reaction_id is not None and (
        not isinstance(reaction_id, str) or len(reaction_id) > 200
    ):
        errors.append({"code": "invalid_reaction_id", "location": "reaction_id", "message": "reaction_id 必须是最多 200 字符的字符串或 null。"})
        reaction_id = None
    user_confirmed = source.get("user_confirmed", False)
    if not isinstance(user_confirmed, bool):
        errors.append({"code": "invalid_user_confirmed", "location": "user_confirmed", "message": "user_confirmed 必须是布尔值。"})
        user_confirmed = False
    normalized = {
        "schema_version": "1.0",
        "reaction_id": reaction_id,
        "reactants": normalized_sides["reactants"],
        "products": normalized_sides["products"],
        "context": context,
        "component_assumptions": assumptions,
        "user_confirmed": user_confirmed,
    }
    if not normalized["reaction_id"]:
        digest_source = dict(normalized)
        digest_source["reaction_id"] = None
        normalized["reaction_id"] = "rxn_" + hashlib.sha256(
            _stable_json(digest_source).encode("utf-8")
        ).hexdigest()[:16]
    has_unresolved_input_ambiguity = bool(assumptions or warnings)
    return {
        "valid": not errors,
        "normalized_reaction": normalized,
        "errors": errors,
        "warnings": warnings,
        "validation_scope": {
            "text_checks_completed": True,
            "chemical_structure_validated": False,
            "atom_mapping_completed": False,
            "mass_balance_validated": False,
        },
        "has_unresolved_input_ambiguity": has_unresolved_input_ambiguity,
    }


__all__ = ["validate_reaction_input"]
