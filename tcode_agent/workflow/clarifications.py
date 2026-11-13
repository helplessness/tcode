"""Shared helpers for workflow input resolution and clarification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


_RECEPTOR_SUFFIXES = {".pdb", ".cif", ".mmcif"}
_LIGAND_SUFFIXES = {".sdf", ".mol2", ".mol", ".smi", ".smiles"}
_AMBIGUOUS_SUFFIXES = {".pdbqt"}

_RECEPTOR_HINTS = (
    "receptor",
    "protein",
    "target",
    "apo",
    "chain",
)
_LIGAND_HINTS = (
    "ligand",
    "compound",
    "lead",
    "pose",
    "dock",
)
_AUTOBOX_HINTS = (
    "autobox",
    "reference",
    "ref_lig",
    "box",
    "cofactor",
)

_DEFAULT_REQUIRED_FIELDS = ("target_description", "receptor_path", "ligand_path")
_DEFAULT_OPTIONAL_FIELDS = ("constraints", "success_metrics")

_FIELD_QUESTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "target_description": {
        "question": "请补充这次 protein-ligand 设计的目标描述，例如目标蛋白、希望优化的性质、预期输出。",
    },
    "receptor_path": {
        "question": "请提供 receptor 结构文件路径（如 PDB/mmCIF）。如果已经上传，请直接确认要用哪个文件。",
    },
    "ligand_path": {
        "question": "请提供 ligand 文件路径（如 SDF/MOL2）。如果已经上传，请直接确认要用哪个文件。",
    },
    "autobox_ligand_path": {
        "question": "如果需要用参考配体定义 docking box，请提供 autobox/reference ligand 路径；如果不需要可留空。",
        "recommendation": "没有单独参考配体时，通常可以先沿用 ligand_path。",
        "ask_optional": False,
    },
    "constraints": {
        "question": "如有设计约束，请补充（例如保留口袋残基、限制可变位点、亲和力阈值等）。没有可留空。",
        "recommendation": "如果暂时没有明确约束，可以回复“无额外约束”。",
        "ask_optional": True,
    },
    "success_metrics": {
        "question": "如有成功标准，请补充（例如 docking 分数、保守位点保持、溶解性区间等）。没有可留空。",
        "recommendation": "如果暂时没有明确指标，可以回复“先给出可比较候选”。",
        "ask_optional": True,
    },
}


def _normalize_uploaded_files(resource_context: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(resource_context, dict):
        return []
    items = resource_context.get("uploaded_files")
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        normalized.append(
            {
                "name": str(item.get("name") or Path(path).name),
                "path": path,
                "kind": str(item.get("kind") or "file").strip().lower() or "file",
            }
        )
    return normalized


def _score_name(name: str, hints: tuple[str, ...]) -> int:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return 0
    return sum(1 for hint in hints if hint in normalized)


def classify_protein_input_candidates(
    resource_context: Optional[dict[str, Any]],
) -> dict[str, list[str]]:
    uploaded_files = _normalize_uploaded_files(resource_context)
    receptor_candidates: list[tuple[int, str]] = []
    ligand_candidates: list[tuple[int, str]] = []
    autobox_candidates: list[tuple[int, str]] = []

    for item in uploaded_files:
        path = str(item.get("path") or "").strip()
        name = str(item.get("name") or Path(path).name)
        suffix = Path(path).suffix.lower()
        receptor_score = _score_name(name, _RECEPTOR_HINTS)
        ligand_score = _score_name(name, _LIGAND_HINTS)
        autobox_score = _score_name(name, _AUTOBOX_HINTS)

        if suffix in _RECEPTOR_SUFFIXES:
            receptor_candidates.append((100 + receptor_score, path))
            continue
        if suffix in _LIGAND_SUFFIXES:
            ligand_candidates.append((100 + ligand_score, path))
            if autobox_score:
                autobox_candidates.append((200 + autobox_score, path))
            continue
        if suffix in _AMBIGUOUS_SUFFIXES:
            if receptor_score > ligand_score:
                receptor_candidates.append((50 + receptor_score, path))
            elif ligand_score > receptor_score:
                ligand_candidates.append((50 + ligand_score, path))
            elif autobox_score:
                autobox_candidates.append((50 + autobox_score, path))

    def _sort(items: list[tuple[int, str]]) -> list[str]:
        return [path for _, path in sorted(items, key=lambda item: (-item[0], item[1]))]

    resolved = {
        "receptor_path": _sort(receptor_candidates),
        "ligand_path": _sort(ligand_candidates),
        "autobox_ligand_path": _sort(autobox_candidates),
    }
    if not resolved["autobox_ligand_path"] and len(resolved["ligand_path"]) == 1:
        resolved["autobox_ligand_path"] = list(resolved["ligand_path"])
    return resolved


def _clean_mapping(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return cleaned
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            cleaned[key] = normalized
            continue
        cleaned[key] = value
    return cleaned


def resolve_protein_workflow_inputs(
    input_payload: Optional[dict[str, Any]],
    resource_context: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = _clean_mapping(input_payload)
    autofilled: dict[str, Any] = {}
    candidates = classify_protein_input_candidates(resource_context)
    for field in ("receptor_path", "ligand_path", "autobox_ligand_path"):
        if resolved.get(field):
            continue
        options = list(candidates.get(field) or [])
        if len(options) != 1:
            continue
        resolved[field] = options[0]
        autofilled[field] = options[0]
    return resolved, autofilled


def serialize_answer(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _contract_items(input_contract: Optional[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    contract = input_contract if isinstance(input_contract, dict) else {}
    raw_items = contract.get(key)
    if not isinstance(raw_items, list):
        return []

    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, str):
            field = raw.strip()
            if field:
                items.append({"field": field})
            continue
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or raw.get("name") or raw.get("key") or "").strip()
        if not field:
            continue
        item = dict(raw)
        item["field"] = field
        items.append(item)
    return items


def _workflow_input_contract_items(
    input_contract: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析 Workflow 输入合同，并仅为完全缺失的旧合同保留 Protein 默认值。

    已声明的合同具有完整语义：合同中没有 ``required`` 或 ``optional``
    就表示对应集合为空。只有调用方完全没有提供合同（``None``）时，才按
    旧版 Protein Workflow 的字段集合兼容，避免通用插件被注入领域字段。
    """

    contract = input_contract if isinstance(input_contract, dict) else None
    required = _contract_items(contract, "required")
    optional = _contract_items(contract, "optional")
    if contract is None:
        required = [{"field": field} for field in _DEFAULT_REQUIRED_FIELDS]
        optional = [
            {"field": field, "ask": True}
            for field in _DEFAULT_OPTIONAL_FIELDS
        ]
    return required, optional


def missing_required_fields_from_contract(
    input_payload: Optional[dict[str, Any]],
    input_contract: Optional[dict[str, Any]] = None,
) -> list[str]:
    resolved = _clean_mapping(input_payload)
    required, _ = _workflow_input_contract_items(input_contract)
    missing: list[str] = []
    for item in required:
        field = str(item.get("field") or "").strip()
        if field and not serialize_answer(resolved.get(field)):
            missing.append(field)
    return missing


def _question_definition(
    item: dict[str, Any],
    *,
    required: bool,
) -> Optional[dict[str, Any]]:
    """把一个合同字段转换为可执行问题定义。

    必填字段缺失时始终允许询问；可选字段默认跳过，只有插件在合同中显式
    声明 ``ask: true`` 或 ``ask_optional: true`` 时才进入同步问答。
    """

    field = str(item.get("field") or "").strip()
    if not field:
        return None

    defaults = dict(_FIELD_QUESTION_DEFAULTS.get(field, {}))
    question = str(
        item.get("question")
        or item.get("prompt")
        or defaults.get("question")
        or f"请补充 workflow 输入字段 `{field}`。"
    ).strip()
    recommendation = item.get("recommendation", defaults.get("recommendation"))
    ask_optional = item.get("ask", item.get("ask_optional", False))
    return {
        "field": field,
        "question": question,
        "required": required,
        "recommendation": str(recommendation or "").strip(),
        "ask_optional": bool(ask_optional),
    }


def infer_answer_from_context(
    payload: Optional[dict[str, Any]],
    resource_context: Optional[dict[str, Any]],
) -> Optional[str]:
    request = dict(payload or {})
    metadata = dict(request.get("metadata") or {})
    workflow_inputs = _clean_mapping(metadata.get("workflow_inputs") if isinstance(metadata, dict) else {})
    resolved_inputs, _ = resolve_protein_workflow_inputs(workflow_inputs, resource_context)
    field = str(metadata.get("workflow_field") or "").strip()
    if field:
        answer = serialize_answer(resolved_inputs.get(field))
        return answer or None

    combined_text = " ".join(
        str(request.get(key) or "")
        for key in ("question", "display", "goal")
    ).lower()
    if not combined_text:
        return None

    if "autobox" in combined_text or "reference ligand" in combined_text:
        answer = serialize_answer(resolved_inputs.get("autobox_ligand_path"))
        return answer or None
    if "ligand" in combined_text or "配体" in combined_text:
        answer = serialize_answer(resolved_inputs.get("ligand_path"))
        return answer or None
    if "receptor" in combined_text or "protein" in combined_text or "受体" in combined_text:
        answer = serialize_answer(resolved_inputs.get("receptor_path"))
        return answer or None
    if "constraint" in combined_text or "约束" in combined_text:
        answer = serialize_answer(resolved_inputs.get("constraints"))
        return answer or None
    if "success" in combined_text or "metric" in combined_text or "成功标准" in combined_text:
        answer = serialize_answer(resolved_inputs.get("success_metrics"))
        return answer or None
    return None


def build_workflow_questions(
    input_payload: Optional[dict[str, Any]],
    resource_context: Optional[dict[str, Any]],
    input_contract: Optional[dict[str, Any]] = None,
    template_key: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """根据声明式合同解析输入，并生成最少量的阻塞式澄清问题。

    通用插件只会收到自己声明的字段。只有合同需要 Protein 文件字段，或
    调用方未提供合同而进入旧版兼容路径时，才从上传资源自动识别 receptor、
    ligand 和 autobox 文件。返回值依次为已解析输入、待询问题和自动填充记录。
    """

    required_items, optional_items = _workflow_input_contract_items(input_contract)
    contract_fields = {
        str(item.get("field") or "").strip()
        for item in [*required_items, *optional_items]
        if str(item.get("field") or "").strip()
    }
    protein_file_fields = {
        "receptor_path",
        "ligand_path",
        "autobox_ligand_path",
    }
    if input_contract is None or contract_fields & protein_file_fields:
        resolved_inputs, autofilled = resolve_protein_workflow_inputs(
            input_payload,
            resource_context,
        )
        if input_contract is not None:
            resolved_inputs = {
                key: value
                for key, value in resolved_inputs.items()
                if key in contract_fields or key in _clean_mapping(input_payload)
            }
            autofilled = {
                key: value
                for key, value in autofilled.items()
                if key in contract_fields
            }
    else:
        resolved_inputs = _clean_mapping(input_payload)
        autofilled = {}
    questions: list[dict[str, Any]] = []

    def _append_question(definition: dict[str, Any]) -> None:
        field = str(definition.get("field") or "").strip()
        required = bool(definition.get("required"))
        if not required and not bool(definition.get("ask_optional", True)):
            return
        if serialize_answer(resolved_inputs.get(field)):
            return
        questions.append(
            {
                "question": str(definition.get("question") or "").strip(),
                "required": required,
                "type": "custom",
                "known_context": _workflow_context_summary(resolved_inputs, autofilled),
                "recommendation": str(definition.get("recommendation") or "").strip(),
                "metadata": {
                    "workflow_field": field,
                    "workflow_template_key": str(template_key or "").strip(),
                    "workflow_inputs": dict(resolved_inputs),
                    "autofilled_fields": dict(autofilled),
                },
            }
        )

    seen: set[str] = set()
    for raw_item, required in [(item, True) for item in required_items] + [(item, False) for item in optional_items]:
        definition = _question_definition(raw_item, required=required)
        if definition is None:
            continue
        field = str(definition.get("field") or "")
        dedupe_key = f"{field}:{definition.get('required')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        _append_question(definition)

    return resolved_inputs, questions, autofilled


def build_protein_workflow_questions(
    input_payload: Optional[dict[str, Any]],
    resource_context: Optional[dict[str, Any]],
    input_contract: Optional[dict[str, Any]] = None,
    template_key: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """兼容旧调用名称，并转发到通用 Workflow 问题构建器。"""

    return build_workflow_questions(
        input_payload,
        resource_context,
        input_contract=input_contract,
        template_key=template_key,
    )


def _workflow_context_summary(
    resolved_inputs: dict[str, Any],
    autofilled: Optional[dict[str, Any]] = None,
) -> str:
    """生成供澄清界面展示的有限输入摘要，兼容领域字段和插件自定义字段。"""

    lines: list[str] = []
    preferred_fields = (
        "target_description",
        "receptor_path",
        "ligand_path",
        "autobox_ligand_path",
        "constraints",
        "success_metrics",
    )
    remaining_fields = sorted(
        str(field)
        for field in resolved_inputs
        if str(field) not in preferred_fields
    )
    for field in [*preferred_fields, *remaining_fields[:24]]:
        value = serialize_answer(resolved_inputs.get(field))
        if not value:
            continue
        lines.append(f"{field}: {value[:400]}")
    if autofilled:
        fields = ", ".join(sorted(str(key) for key in autofilled if autofilled.get(key)))
        if fields:
            lines.append(f"autofilled_fields: {fields}")
    return "\n".join(lines)
