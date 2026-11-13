"""TCode SDK definitions for the P1 query surface and P2 hypothesis workflow."""

from __future__ import annotations

import json
import inspect
import re
import threading
from contextvars import ContextVar
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tcode_agent.plugin_sdk import (
    PluginContext,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)

from .entity_query import query_relations, read_evidence, resolve_entities
from .entity_store import EntityStore
from .entity_validation import BundleValidationError, validate_packaged_bundle
from .hypothesis_ledger import HypothesisLedger, private_references
from .mechanism_fragments import search_mechanism_fragments
from .reaction_analogs import find_reaction_analogs
from .route_planner import find_routes
from .substrate_branch_scan import scan_same_substrate_branches


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENTITY_TYPES = [
    "CatalyticCapability",
    "ChemicalClass",
    "ChemicalEntity",
    "EnzymeFamily",
    "MechanisticIntermediate",
    "Protein",
    "ProteinGroup",
    "EnzymeVariant",
    "Pathway",
    "Reaction",
    "TransformationPattern",
]
_PREDICATES = [
    "catalyzes_reported",
    "derived_from",
    "exhibits_transformation",
    "has_intermediate",
    "has_participant",
    "member_of",
    "participates_in_pathway",
    "may_catalyze",
    "requires_capability",
    "requires_cofactor",
    "supports_capability",
    "uses_cofactor",
]
_STAGE_COMMIT_OPERATIONS = {
    "enzyme_entity_reaction_commit",
    "enzyme_entity_hypotheses_commit",
    "enzyme_entity_candidates_commit",
    "enzyme_entity_critical_report_commit",
}

_PUBLIC_KNOWLEDGE_OPERATIONS = {
    "enzyme_entity_resolve",
    "enzyme_entity_relations",
    "enzyme_entity_reaction_analogs",
    "enzyme_entity_substrate_branch_scan",
    "enzyme_entity_mechanism_fragments",
    "enzyme_entity_capability_candidates",
    "enzyme_entity_route_connect",
}


def _collect_evidence_refs(value: Any) -> list[dict[str, str]]:
    """Publish plugin-validated evidence IDs for Workflow coverage checks."""

    evidence_ids: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in {"evidence_id", "evidence_ids", "supporting_evidence_ids"}:
                    if isinstance(child, (list, tuple, set)):
                        evidence_ids.update(
                            str(entry).strip() for entry in child if str(entry).strip()
                        )
                    elif str(child or "").strip():
                        evidence_ids.add(str(child).strip())
                else:
                    walk(child)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child)

    walk(value)
    return [{"evidence_id": evidence_id} for evidence_id in sorted(evidence_ids)]


def _object_schema(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _string_array_schema(maximum: int) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": maximum,
        "items": {"type": "string", "minLength": 1, "maxLength": 1200},
    }


def _id_array_schema(maximum: int = 12) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": maximum,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 200},
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class EntityUsageAudit:
    """Persist a bounded, workspace-local record of graph and evidence access."""

    def __init__(self, workspace_dir: str, max_records: int) -> None:
        self.workspace_dir = workspace_dir
        self.max_records = max_records
        self._lock = threading.Lock()

    def record(
        self,
        workspace: Any,
        *,
        run_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
        ok: bool,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        path = (
            Path(self.workspace_dir)
            / "runs"
            / run_id
            / "entity_usage.jsonl"
        )
        record = {
            "schema_version": "1.0",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "operation": operation,
            "call_id": call.call_id,
            "message_id": call.message_id,
            "agent": call.agent,
            "ok": ok,
            "arguments": _plain(arguments),
            "result_summary": self._summary(result or {}),
            "error": error,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                current = workspace.read_text(str(path), encoding="utf-8")
            except FileNotFoundError:
                current = ""
            lines = [item for item in current.splitlines() if item.strip()]
            lines = lines[-(self.max_records - 1) :]
            lines.append(line)
            workspace.write_text(str(path), "\n".join(lines) + "\n", encoding="utf-8")
        return path.as_posix()

    @staticmethod
    def _summary(result: Mapping[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in ("status", "total", "next_cursor", "citation_ready"):
            if key in result:
                summary[key] = result[key]
        for key in (
            "results",
            "relations",
            "analogs",
            "scans",
            "fragments",
            "routes",
            "assertions",
            "evidence",
        ):
            if isinstance(result.get(key), list):
                summary[f"returned_{key}"] = len(result[key])
        if isinstance(result.get("assertions"), list):
            summary["assertion_ids"] = sorted(
                {
                    str(row["assertion_id"])
                    for row in result["assertions"]
                    if isinstance(row, Mapping) and row.get("assertion_id")
                }
            )[:12]
        if isinstance(result.get("evidence"), list):
            summary["evidence_ids"] = sorted(
                {
                    str(row["evidence_id"])
                    for row in result["evidence"]
                    if isinstance(row, Mapping) and row.get("evidence_id")
                }
            )[:12]
        if isinstance(result.get("routes"), list):
            summary["route_refs"] = [
                str(row["route_ref"])
                for row in result["routes"]
                if isinstance(row, Mapping) and row.get("route_ref")
            ][:8]
        if isinstance(result.get("fragments"), list):
            summary["fragment_ids"] = [
                str(row["fragment_id"])
                for row in result["fragments"]
                if isinstance(row, Mapping) and row.get("fragment_id")
            ][:12]
        return summary


class EntityToolService:
    """Own the fixed bundle, query tools and per-run audit trail."""

    def __init__(
        self,
        ctx: PluginContext,
        config: Mapping[str, Any],
        *,
        plugin_root: str | Path,
        plugin_version: str,
    ) -> None:
        self._ctx = ctx
        self._config = dict(config)
        self._plugin_root = Path(plugin_root).resolve()
        self._plugin_version = plugin_version
        if self._config.get("offline_mode", True) is not True:
            raise ValueError("offline_mode must remain true for the entity demo")
        self._knowledge_format = str(
            self._config.get("knowledge_format") or "sqlite"
        ).strip().casefold()
        if self._knowledge_format != "sqlite":
            raise ValueError("knowledge_format must be sqlite")
        self._workspace_dir = str(
            self._config.get("workspace_dir")
            or ".tcode/plugins/enzyme_entity_discovery_demo"
        )
        if self._workspace_dir != ".tcode/plugins/enzyme_entity_discovery_demo":
            raise ValueError("workspace_dir must use the isolated enzyme entity demo path")
        self._max_results = int(self._config.get("max_results") or 20)
        if not 1 <= self._max_results <= 50:
            raise ValueError("max_results must be between 1 and 50")
        audit_log_max_records = int(
            self._config.get("audit_log_max_records") or 1000
        )
        if not 100 <= audit_log_max_records <= 5000:
            raise ValueError("audit_log_max_records must be between 100 and 5000")
        self._audit = EntityUsageAudit(
            self._workspace_dir,
            audit_log_max_records,
        )
        self._call_resources = ContextVar("entity_call_" + str(id(self)), default=None)
        self._default_store = EntityStore(
            ctx.workspace,
            self._plugin_root,
            workspace_dir=self._workspace_dir,
            expected_bundle_digest=self._config.get("expected_bundle_digest"),
            knowledge_format=self._knowledge_format,
        )
        max_hypotheses = int(self._config.get("max_hypotheses") or 4)
        if not 1 <= max_hypotheses <= 8:
            raise ValueError("max_hypotheses must be between 1 and 8")
        max_candidates = int(self._config.get("max_candidates") or 10)
        if not 1 <= max_candidates <= 16:
            raise ValueError("max_candidates must be between 1 and 16")
        self._default_ledger = HypothesisLedger(
            ctx.workspace,
            self._store,
            workspace_dir=self._workspace_dir,
            max_hypotheses=max_hypotheses,
            max_candidates=max_candidates,
        )

    @property
    def _store(self):
        resources = self._call_resources.get()
        return resources[0] if resources else self._default_store

    @property
    def _ledger(self):
        resources = self._call_resources.get()
        return resources[1] if resources else self._default_ledger

    def validate_installation(self) -> dict[str, Any]:
        return self._store.load()

    def validate_source_installation(self) -> dict[str, Any]:
        """Validate packaged facts without writing into the target Workspace."""

        manifest, _files = validate_packaged_bundle(
            self._ctx.workspace,
            self._plugin_root,
        )
        digest = str(manifest["bundle_digest"])
        if self._store.expected_bundle_digest and digest != self._store.expected_bundle_digest:
            raise BundleValidationError(
                "configured expected_bundle_digest does not match the packaged bundle"
            )
        statistics = manifest["statistics"]
        return {
            "bundle_digest": digest,
            "bundle_version": manifest["bundle_version"],
            "source_cards": statistics["source_cards"],
            "entities": statistics["entities"],
            "assertions": statistics["assertions"],
            "evidence": statistics["evidence"],
            "snapshot_root": "pending_first_tool_call",
        }

    def definitions(self) -> tuple[ToolDefinition, ...]:
        run_id = {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        }
        entity_ids = {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        }
        cursor = {
            "type": "string",
            "pattern": "^(rel|ana)_[a-f0-9]{16}_[0-9]{1,6}$",
        }
        digest = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
        claim = _object_schema(
            {
                "claim_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "evidence_level": {"enum": [
                    "reported_exact",
                    "reported_related",
                    "mechanistic_analogy",
                    "computed_prediction",
                    "model_background_unverified",
                ]},
                "assertion_ids": _id_array_schema(8),
                "evidence_ids": _id_array_schema(12),
                "scope_or_method": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            required=("claim_id", "text", "evidence_level", "scope_or_method"),
        )
        counterfactual_bridge = _object_schema(
            {
                "shared_events": _string_array_schema(12),
                "missing_events": _string_array_schema(12),
                "conflicting_events": _string_array_schema(12),
                "native_termination": _string_array_schema(8),
                "target_alternative_termination": _string_array_schema(8),
                "unsupported_assumptions": _string_array_schema(12),
            },
            required=(
                "shared_events",
                "missing_events",
                "conflicting_events",
                "native_termination",
                "target_alternative_termination",
                "unsupported_assumptions",
            ),
        )
        definitions = (
            ToolDefinition(name="enzyme_entity_workspace_query", description="按实体 ID、名称或别名分页查询公共和当前 Workspace 私有事实。多候选需选择 entity_id，支持关系、证据类型与精确条件筛选；不会发布知识。",
                input_schema=_object_schema({"run_id": run_id, "entity_id": {"type": "string"}, "text": {"type": "string"},
                    "predicate": {"type": "string"}, "scope": {"enum": ["public", "private", "both"]},
                    "evidence_basis": {"enum": ["", "experimental", "computational", "literature_report", "hypothesis", "unspecified"]},
                    "polarity": {"enum": ["", "supports", "opposes"]}, "qualifiers": {"type": "object", "maxProperties": 20},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "after": {"type": "string"}}, required=("run_id",)),
                handler=self._workspace_query, side_effect_level="read", result_budget_tokens=8192),
            ToolDefinition(name="enzyme_entity_workspace_report", description="复查逐条引用并核对可选 statement 的实体、关系、方向、条件与所需证据类型；检查未选中的相反主张。自由文本无法自动判断，支持仅限结构化事实，不认证科学正确性，仍需人工复核。",
                input_schema=_object_schema({"run_id": run_id, "claims": {"type": "array", "minItems": 1, "maxItems": 100,
                    "items": _object_schema({"text": {"type": "string", "minLength": 1, "maxLength": 4000}, "assertion_ids": _string_array_schema(20),
                        "required_evidence_basis": {"enum": ["any", "experimental", "computational", "literature_report", "hypothesis"]},
                        "statement": _object_schema({"subject_id": {"type": "string", "minLength": 1}, "predicate": {"type": "string", "minLength": 1}, "object_id": {"type": "string", "minLength": 1},
                            "polarity": {"enum": ["supports", "opposes"]}, "kind": {"enum": ["reported", "hypothesis"]}, "qualifiers": {"type": "object", "maxProperties": 20}},
                            required=("subject_id", "predicate", "object_id"))}, required=("text", "assertion_ids"))}}, required=("run_id", "claims")),
                handler=self._workspace_report, side_effect_level="read", result_budget_tokens=8192),
            ToolDefinition(
                name="enzyme_entity_resolve",
                description="在当前 Workspace 固定的五卡实体包中解析名称、别名、外部 ID 或实体 ID。模糊结果只是词面建议，不确认生物学身份。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "query": {"type": "string", "minLength": 1, "maxLength": 240},
                        "entity_types": {
                            "type": "array",
                            "maxItems": len(_ENTITY_TYPES),
                            "uniqueItems": True,
                            "items": {"enum": _ENTITY_TYPES},
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=("run_id", "query"),
                ),
                handler=self._resolve,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_entity_relations",
                description="按实体、关系、方向、邻居类型和极性读取单跳 Assertion 摘要；摘要不能直接作为最终科学引用。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "entity_id": {"type": "string", "minLength": 1, "maxLength": 200},
                        "predicates": {
                            "type": "array",
                            "maxItems": len(_PREDICATES),
                            "uniqueItems": True,
                            "items": {"enum": _PREDICATES},
                        },
                        "direction": {
                            "enum": ["incoming", "outgoing", "both"],
                            "default": "both",
                        },
                        "neighbor_types": {
                            "type": "array",
                            "maxItems": len(_ENTITY_TYPES),
                            "uniqueItems": True,
                            "items": {"enum": _ENTITY_TYPES},
                        },
                        "polarity": {
                            "enum": ["supports", "opposes", "both"],
                            "default": "supports",
                        },
                        "cursor": cursor,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    },
                    required=("run_id", "entity_id"),
                ),
                handler=self._relations,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=8192,
            ),
            ToolDefinition(
                name="enzyme_entity_reaction_analogs",
                description="按明确参与物、转化模式和催化能力检索已报道反应先例；匹配数量不是催化适用性分数，也不证明目标反应活性。",
                input_schema={
                    **_object_schema(
                        {
                            "run_id": run_id,
                            "participant_ids": entity_ids,
                            "transformation_ids": entity_ids,
                            "capability_ids": entity_ids,
                            "participant_roles": {
                                "type": "array",
                                "maxItems": 5,
                                "uniqueItems": True,
                                "items": {
                                    "enum": ["reactant", "product", "cosubstrate", "left", "right"]
                                },
                            },
                            "minimum_matches": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 22,
                                "default": 1,
                            },
                            "cursor": cursor,
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        required=("run_id",),
                    ),
                    "anyOf": [
                        {"required": ["participant_ids"]},
                        {"required": ["transformation_ids"]},
                        {"required": ["capability_ids"]},
                    ],
                },
                handler=self._reaction_analogs,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=8192,
            ),
            ToolDefinition(
                name="enzyme_entity_substrate_branch_scan",
                description="对每个直接底物一次性扫描本地已报道同底物反应，并联结催化主体、辅因子、第二底物、产物、中间体及弱假说触发信号；只整理关系，不证明目标活性。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "substrate_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=("run_id", "substrate_ids"),
                ),
                handler=self._substrate_branch_scan,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=12288,
            ),
            ToolDefinition(
                name="enzyme_entity_mechanism_fragments",
                description="从当前固定本体的反应、参与物、转化、能力、中间体、辅因子和催化主体关系确定性派生机制片段；命中不是目标活性证据。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "participant_ids": _id_array_schema(12),
                        "transformation_ids": _id_array_schema(12),
                        "capability_ids": _id_array_schema(12),
                        "cofactor_ids": _id_array_schema(12),
                        "intermediate_ids": _id_array_schema(12),
                        "required_terms": _string_array_schema(8),
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=("run_id",),
                ),
                handler=self._mechanism_fragments,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=12288,
            ),
            ToolDefinition(
                name="enzyme_entity_read_evidence",
                description="仅在本插件已初始化的真实 run_id 中，按 Assertion ID、Evidence ID 或 wiki_ref 回读冻结领域知识卡的逐字原文、行号和来源。不得猜测或虚构 run_id；普通当前 Workspace 的事实与证据由平台 ontology_query 返回，不需要此插件运行。",
                input_schema={
                    **_object_schema(
                        {
                            "run_id": run_id,
                            "assertion_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 4,
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                            },
                            "evidence_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 8,
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                            },
                            "wiki_refs": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 8,
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1, "maxLength": 160},
                            },
                        },
                        required=("run_id",),
                    ),
                    "anyOf": [
                        {"required": ["assertion_ids"]},
                        {"required": ["evidence_ids"]},
                        {"required": ["wiki_refs"]},
                    ],
                },
                handler=self._read_evidence,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=12288,
            ),
            ToolDefinition(
                name="enzyme_entity_run_init",
                description="初始化一次独立的实体酶发现运行，原样保存目标反应并固定本地实体包摘要；Workflow 阶段必须使用上下文给出的 run_id。自然语言前端输入优先原样放入 target_description。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "target_reaction": {
                            "$schema": "https://json-schema.org/draft/2020-12/schema",
                            "type": "object",
                            "properties": {
                                "schema_version": {"type": "string"},
                                "reaction_id": {"type": "string"},
                                "reaction_name": {"type": "string"},
                                "reactants": {"type": "array", "maxItems": 16},
                                "products": {"type": "array", "maxItems": 16},
                                "reactant_names": _string_array_schema(16),
                                "product_names": _string_array_schema(16),
                                "natural_language_request": {"type": "string", "maxLength": 12000},
                                "target_description": {"type": "string", "maxLength": 12000},
                                "context": {},
                                "constraints": {},
                                "component_assumptions": {"type": "array", "maxItems": 16},
                                "substrate_or_product": {},
                            },
                            "additionalProperties": False,
                            "minProperties": 1,
                            "maxProperties": 14,
                        },
                    },
                    required=("run_id", "target_reaction"),
                ),
                handler=self._run_init,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_entity_reaction_commit",
                description="提交文本级目标反应分析。该阶段不得出现候选酶、实体引用或声称已完成原子映射。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "analysis": _object_schema(
                            {
                                "direction": {"enum": ["forward", "reverse", "reversible", "unknown"]},
                                "reaction_summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                                "net_changes": {
                                    "type": "array",
                                    "maxItems": 12,
                                    "items": _object_schema(
                                        {
                                            "change_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                            "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                                            "confidence": {"enum": ["high", "medium", "low", "unknown"]},
                                        },
                                        required=("change_id", "description", "confidence"),
                                    ),
                                },
                                "assumptions": _string_array_schema(12),
                                "warnings": _string_array_schema(12),
                                "unresolved_questions": _string_array_schema(12),
                            },
                            required=(
                                "direction",
                                "reaction_summary",
                                "net_changes",
                                "assumptions",
                                "warnings",
                                "unresolved_questions",
                            ),
                        ),
                    },
                    required=("run_id", "analysis"),
                ),
                handler=self._reaction_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_entity_run_context",
                description="按阶段读取本轮已提交的紧凑产物及 context_digest；不返回未请求的全量知识库，也不修改事实本体。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "stage": {"enum": [
                            "mechanism_and_capability",
                            "family_screening_and_routes",
                            "critical_report",
                        ]},
                    },
                    required=("run_id", "stage"),
                ),
                handler=self._run_context,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=12288,
            ),
            ToolDefinition(
                name="enzyme_entity_hypothesis_stage",
                description="逐条暂存一个机制假说；必须包含完整反事实桥接。最终提交用 hypotheses=[] 原子校验全部草稿。",
                input_schema=_object_schema(
                    {"run_id": run_id, "context_digest": digest,
                     "slot": {"type": "integer", "minimum": 1, "maximum": 8},
                     "hypothesis": {"type": "object"}},
                    required=("run_id", "context_digest", "slot", "hypothesis"),
                ),
                handler=self._hypothesis_stage,
                version="1.0.0", side_effect_level="write", result_budget_tokens=1024,
            ),
            ToolDefinition(
                name="enzyme_entity_hypotheses_commit",
                description="提交机制假说账本。reported_* 事实必须引用本轮已回读且与 Assertion 相连的 Evidence；类比和模型预测保持独立字段。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "context_digest": digest,
                        "hypotheses": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 8,
                            "items": _object_schema(
                                {
                                    "hypothesis_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                    "title": {"type": "string", "minLength": 1, "maxLength": 240},
                                    "proposed_mechanism": {"type": "string", "minLength": 1, "maxLength": 4000},
                                    "required_capability_ids": _id_array_schema(),
                                    "required_cofactor_ids": _id_array_schema(),
                                    "intermediate_ids": _id_array_schema(),
                                    "fact_claims": {"type": "array", "maxItems": 12, "items": claim},
                                    "analogy_claims": {"type": "array", "maxItems": 12, "items": claim},
                                    "prediction_claims": {"type": "array", "maxItems": 12, "items": claim},
                                    "counterarguments": _string_array_schema(8),
                                    "evidence_gaps": _string_array_schema(8),
                                    "falsification_tests": _string_array_schema(8),
                                    "counterfactual_bridge": counterfactual_bridge,
                                },
                                required=(
                                    "hypothesis_id",
                                    "title",
                                    "proposed_mechanism",
                                    "required_capability_ids",
                                    "required_cofactor_ids",
                                    "intermediate_ids",
                                    "fact_claims",
                                    "analogy_claims",
                                    "prediction_claims",
                                    "counterarguments",
                                    "evidence_gaps",
                                    "falsification_tests",
                                ),
                            ),
                        },
                        "global_knowledge_gaps": _string_array_schema(12),
                    },
                    required=("hypotheses", "global_knowledge_gaps"),
                ),
                handler=self._hypotheses_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_entity_baseline_candidates_commit",
                description="在候选实体检索前冻结 Agent 自主候选草案；拒绝 entity_id、Assertion、Evidence、card_id 等知识库引用。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "context_digest": digest,
                        "candidates": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 16,
                            "items": _object_schema(
                                {
                                    "proposal_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                    "label": {"type": "string", "minLength": 1, "maxLength": 240},
                                    "identity_level": {"enum": ["protein", "named_enzyme", "family", "capability_gap"]},
                                    "fit_reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                                    "knowledge_gaps": _string_array_schema(6),
                                },
                                required=("proposal_id", "label", "identity_level", "fit_reason", "knowledge_gaps"),
                            ),
                        },
                    },
                    required=("run_id", "context_digest", "candidates"),
                ),
                handler=self._baseline_candidates_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=3072,
            ),
            ToolDefinition(
                name="enzyme_entity_capability_candidates",
                description="在基线草案冻结后，按明确催化能力及可选辅因子/中间体查找家族或蛋白线索；只返回事实连接，不计算适用性分数，不证明目标活性。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "capability_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "cofactor_ids": _id_array_schema(8),
                        "intermediate_ids": _id_array_schema(8),
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=("run_id", "capability_ids"),
                ),
                handler=self._capability_candidates,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=8192,
            ),
            ToolDefinition(
                name="enzyme_entity_route_connect",
                description="在已报道反应图上执行最多四步的有界路线连接，同时返回物料、方向、辅因子、能量、副产物、条件和中间体安全缺口；结果不是可行性分数。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "start_entity_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "target_entity_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "available_entity_ids": _id_array_schema(12),
                        "max_steps": {"type": "integer", "minimum": 1, "maximum": 4},
                        "max_routes": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    required=("run_id", "start_entity_ids", "target_entity_ids"),
                ),
                handler=self._route_connect,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=12288,
            ),
            ToolDefinition(
                name="enzyme_entity_candidates_commit",
                description="提交基线、实体扩展候选和路线假说；已报道路线步骤必须回读证据，每条路线必须完成七项闭合检查和辅助步骤判断。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "context_digest": digest,
                        "candidates": {
                            "type": "array",
                            "maxItems": 16,
                            "items": _object_schema(
                                {
                                    "candidate_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                    "candidate_kind": {"enum": [
                                        "entity_protein",
                                        "entity_protein_group",
                                        "entity_family",
                                        "model_candidate",
                                        "unmatched_capability",
                                    ]},
                                    "candidate_role": {"enum": ["primary", "route_component", "diagnostic_control", "family_hint", "unmatched_capability"]},
                                    "label": {"type": "string", "minLength": 1, "maxLength": 240},
                                    "entity_id": {"type": "string", "maxLength": 200},
                                    "proposal_id": {"type": "string", "maxLength": 96},
                                    "evidence_level": {"enum": ["mechanistic_analogy", "model_background_unverified"]},
                                    "source_fact_scope": {"enum": ["reported_exact", "reported_related", "none"]},
                                    "fit_reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                                    "supporting_assertion_ids": _id_array_schema(8),
                                    "supporting_evidence_ids": _id_array_schema(12),
                                    "conflicts": _string_array_schema(6),
                                    "knowledge_gaps": _string_array_schema(8),
                                },
                                required=(
                                    "candidate_id",
                                    "candidate_kind",
                                    "label",
                                    "evidence_level",
                                    "source_fact_scope",
                                    "fit_reason",
                                    "supporting_assertion_ids",
                                    "supporting_evidence_ids",
                                    "conflicts",
                                    "knowledge_gaps",
                                ),
                            ),
                        },
                        "routes": {
                            "type": "array",
                            "maxItems": 8,
                            "items": _object_schema(
                                {
                                    "route_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                    "title": {"type": "string", "minLength": 1, "maxLength": 240},
                                    "route_kind": {"enum": [
                                        "reported_path_hypothesis",
                                        "hybrid_path_hypothesis",
                                        "model_path_hypothesis",
                                    ]},
                                    "route_ref": {"type": "string", "maxLength": 96},
                                    "hypothetical_steps": {
                                        "type": "array",
                                        "maxItems": 8,
                                        "items": _object_schema(
                                            {
                                                "step_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                                "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                                                "input_labels": _string_array_schema(12),
                                                "output_labels": _string_array_schema(12),
                                                "candidate_ids": _id_array_schema(8),
                                                "evidence_level": {"enum": ["mechanistic_analogy", "model_background_unverified"]},
                                                "knowledge_gaps": _string_array_schema(8),
                                            },
                                            required=(
                                                "step_id",
                                                "description",
                                                "input_labels",
                                                "output_labels",
                                                "candidate_ids",
                                                "evidence_level",
                                                "knowledge_gaps",
                                            ),
                                        ),
                                    },
                                    "closure_checks": {
                                        "type": "array",
                                        "minItems": 7,
                                        "maxItems": 7,
                                        "items": _object_schema(
                                            {
                                                "check_id": {"enum": [
                                                    "material_handoff",
                                                    "direction",
                                                    "cofactor_resource",
                                                    "energy_balance",
                                                    "byproduct_control",
                                                    "condition_compatibility",
                                                    "intermediate_stability",
                                                ]},
                                                "status": {"enum": ["pass", "gap", "unknown", "not_applicable"]},
                                                "finding": {"type": "string", "minLength": 1, "maxLength": 4000},
                                            },
                                            required=("check_id", "status", "finding"),
                                        ),
                                    },
                                    "auxiliary_steps": {
                                        "type": "array",
                                        "maxItems": 8,
                                        "items": _object_schema(
                                            {
                                                "auxiliary_id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"},
                                                "function": {"enum": [
                                                    "cofactor_supply_or_regeneration",
                                                    "energy_supply_or_regeneration",
                                                    "byproduct_mitigation",
                                                    "intermediate_protection",
                                                    "condition_staging",
                                                    "other",
                                                ]},
                                                "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                                                "required_status": {"enum": ["required", "optional", "unknown"]},
                                            },
                                            required=("auxiliary_id", "function", "description", "required_status"),
                                        ),
                                    },
                                    "validation_priority": {"type": "string", "minLength": 1, "maxLength": 4000},
                                },
                                required=(
                                    "route_id",
                                    "title",
                                    "route_kind",
                                    "hypothetical_steps",
                                    "closure_checks",
                                    "auxiliary_steps",
                                    "validation_priority",
                                ),
                            ),
                        },
                        "comparison_notes": _string_array_schema(12),
                        "route_gaps": _string_array_schema(12),
                    },
                    required=("run_id", "context_digest", "candidates", "routes", "comparison_notes", "route_gaps"),
                ),
                handler=self._candidates_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_entity_critical_report_commit",
                description="执行并保存批判检查；除事实边界外，验证每条路线已完成物料、资源、条件和辅助步骤检查。",
                input_schema=_object_schema(
                    {
                        "run_id": run_id,
                        "context_digest": digest,
                        "review_decision": {"enum": ["PASS", "REVISE"]},
                        "checks": {
                            "type": "array",
                            "minItems": 6,
                            "maxItems": 6,
                            "items": _object_schema(
                                {
                                    "check_id": {"enum": [
                                        "cofactor_not_activity",
                                        "family_attribution",
                                        "evidence_read",
                                        "fact_prediction_separation",
                                        "target_activity_unproven",
                                        "route_closure_and_auxiliary_steps",
                                    ]},
                                    "status": {"enum": ["pass", "fail", "unknown"]},
                                    "finding": {"type": "string", "minLength": 1, "maxLength": 4000},
                                },
                                required=("check_id", "status", "finding"),
                            ),
                        },
                        "limitations": _string_array_schema(16),
                        "next_actions": _string_array_schema(16),
                        # The field is part of the stage receipt returned by
                        # this tool.  Accepting an optional copy also makes
                        # retries tolerant of agents that echo the output
                        # contract back into the commit arguments; the
                        # deterministic ledger remains the source of truth.
                        "top_summary": {"type": "object"},
                        "user_summary": {"type": "string", "minLength": 1, "maxLength": 6000},
                        "allow_supersede": {"type": "boolean", "default": False},
                        "resolved_findings": {
                            "type": "array",
                            "maxItems": 6,
                            "items": _object_schema(
                                {
                                    "check_id": {"enum": [
                                        "cofactor_not_activity",
                                        "family_attribution",
                                        "evidence_read",
                                        "fact_prediction_separation",
                                        "target_activity_unproven",
                                        "route_closure_and_auxiliary_steps",
                                    ]},
                                    "resolution": {"type": "string", "minLength": 1, "maxLength": 4000},
                                },
                                required=("check_id", "resolution"),
                            ),
                        },
                    },
                    required=(
                        "run_id",
                        "context_digest",
                        "review_decision",
                        "checks",
                        "limitations",
                        "next_actions",
                        "user_summary",
                    ),
                ),
                handler=self._critical_report_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
        )
        return definitions

    def _limit(self, arguments: Mapping[str, Any], default: int) -> int:
        value = int(arguments.get("limit", default))
        if not 1 <= value <= 50:
            raise ValueError("limit must be between 1 and 50")
        return min(value, self._max_results)

    @staticmethod
    def _run_id(arguments: Mapping[str, Any]) -> str:
        value = str(arguments.get("run_id") or "")
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id has an invalid format")
        return value

    @staticmethod
    def _workflow_run_id(call: ToolCallContext, stage_id: str) -> str:
        """Recover the run id from a workflow worker session when the model omits it."""

        session_id = str(call.session_id or "")
        prefix = "workflow-agent-"
        marker = f"-{stage_id}-"
        if not session_id.startswith(prefix) or marker not in session_id:
            return ""
        value = session_id[len(prefix) :].split(marker, 1)[0]
        return value if _RUN_ID.fullmatch(value) else ""

    async def _call(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
        function: Callable[[], dict[str, Any]],
    ) -> ToolOutput:
        run_id = ""
        workspace = getattr(call, "workspace", None) or self._ctx.workspace
        local_store = EntityStore(workspace, self._plugin_root, workspace_dir=self._workspace_dir,
                                  expected_bundle_digest=self._config.get("expected_bundle_digest"), knowledge_format=self._knowledge_format)
        local_ledger = HypothesisLedger(workspace, local_store, workspace_dir=self._workspace_dir,
                                       max_hypotheses=self._default_ledger.max_hypotheses, max_candidates=self._default_ledger.max_candidates)
        token = self._call_resources.set((local_store, local_ledger))
        source_policy = None
        try:
            run_id = self._run_id(arguments)
            capability = getattr(call, 'ontology', None)
            source_policy = capability.source_policy() if capability is not None else None
            # Submission is independent of public lookup permission. Evidence
            # references still obey the selected sources, including on replay.
            if source_policy is not None and not source_policy['public']:
                def public_references(value):
                    if isinstance(value, Mapping):
                        for key, child in value.items():
                            if key.endswith(('assertion_id', 'assertion_ids', 'evidence_id', 'evidence_ids')):
                                ids = child if isinstance(child, (list, tuple)) else [child]
                                if any(str(item).strip() and not str(item).startswith(('private-assertion:', 'private-evidence:')) for item in ids):
                                    return True
                            elif public_references(child):
                                return True
                    elif isinstance(value, (list, tuple)):
                        return any(public_references(item) for item in value)
                    return False
                if public_references(arguments) or arguments.get('wiki_refs'):
                    raise PermissionError('workspace public ontology evidence is disabled')
            if operation in _PUBLIC_KNOWLEDGE_OPERATIONS:
                if capability is None:
                    raise PermissionError('managed Workspace ontology capability is required')
                if not source_policy['public']:
                    raise PermissionError('workspace public ontology source is disabled')
            self._store.load()
            references = sorted(private_references(arguments) | set(self._ledger.private_references(run_id)))
            if references:
                if capability is None:
                    raise ValueError('private evidence requires current managed Workspace capability')
                for start in range(0, len(references), 100):
                    private_facts = capability.read_facts(references[start:start+100])
                    if inspect.isawaitable(private_facts):
                        private_facts = await private_facts
                    self._store.attach_private_facts(private_facts)
            if operation in {
                "enzyme_entity_resolve",
                "enzyme_entity_relations",
                "enzyme_entity_reaction_analogs",
                "enzyme_entity_substrate_branch_scan",
                "enzyme_entity_read_evidence",
                "enzyme_entity_workspace_query",
            }:
                self._ledger.enforce_candidate_baseline_gate(run_id)
            result = function()
            if inspect.isawaitable(result):
                result = await result
            if operation == 'enzyme_entity_critical_report_commit' and references:
                # Recheck even on replay; the historical receipt is never a current grant.
                for start in range(0, len(references), 20):
                    validation = capability.validate_report([{'text': 'Workflow evidence references', 'assertion_ids': references[start:start+20]}])
                    if inspect.isawaitable(validation):
                        await validation
            audit_ref = self._audit.record(
                workspace,
                run_id=run_id,
                operation=operation,
                arguments=arguments,
                call=call,
                ok=True,
                result=result,
            )
            payload = {
                "ok": True,
                "operation": operation,
                "bundle_digest": self._store.bundle_digest,
                "audit_ref": audit_ref,
                "source_policy": source_policy,
                "result": result,
            }
            metadata: dict[str, Any] = {
                "ok": True,
                "bundle_digest": self._store.bundle_digest,
                "audit_ref": audit_ref,
                "source_policy": source_policy,
            }
            if operation in _STAGE_COMMIT_OPERATIONS:
                metadata["stage_receipt"] = _plain(result)
                evidence_ids = {
                    row["evidence_id"] for row in _collect_evidence_refs(arguments)
                }
                # A commit receipt is the stable boundary between plugin evidence
                # validation and Workflow validation. Include evidence actually read
                # by this stage's agent even when the commit schema contains only
                # prose checks, as the final critical report does.
                _, audited_evidence_ids = self._ledger.evidence_read_ids(
                    run_id,
                    agent=call.agent,
                )
                evidence_ids.update(audited_evidence_ids)
                evidence_refs = [
                    {"evidence_id": evidence_id}
                    for evidence_id in sorted(evidence_ids)
                ]
                if evidence_refs:
                    metadata["evidence_refs"] = evidence_refs
                artifact_names = result.get("committed_artifacts")
                if not isinstance(artifact_names, list):
                    artifact_names = result.get("final_artifacts")
                if isinstance(artifact_names, list):
                    metadata["artifact_refs"] = {
                        str(name): (
                            Path(self._workspace_dir)
                            / "runs"
                            / run_id
                            / str(name)
                        ).as_posix()
                        for name in artifact_names[:32]
                        if str(name or "").strip()
                    }
                if evidence_refs:
                    metadata.setdefault("artifact_refs", {})["evidence_refs"] = evidence_refs
            return ToolOutput(
                operation,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                metadata,
            )
        except (ValueError, KeyError, PermissionError) as exc:
            audit_ref = None
            if run_id:
                try:
                    audit_ref = self._audit.record(
                        workspace,
                        run_id=run_id,
                        operation=operation,
                        arguments=arguments,
                        call=call,
                        ok=False,
                        error=str(exc),
                    )
                except Exception as audit_exc:
                    payload = {
                        "ok": False,
                        "operation": operation,
                        "error": f"audit write failed: {audit_exc}",
                        "error_type": type(audit_exc).__name__,
                    }
                    return ToolOutput(
                        f"{operation} failed",
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        {
                            "ok": False,
                            "retryable": False,
                            "terminal_tool_error": True,
                            "should_end_turn": True,
                        },
                    )
            payload = {
                "ok": False,
                "operation": operation,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "audit_ref": audit_ref,
                "source_policy": source_policy,
            }
            if isinstance(exc, PermissionError):
                payload["recovery"] = (
                    "Do not retry this disabled source. Use only enabled sources; "
                    "if no relevant evidence is available, submit model_background_unverified "
                    "claims with empty evidence/assertion references and explicit knowledge gaps."
                )
            return ToolOutput(
                f"{operation} failed",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                {"ok": False, "retryable": not isinstance(exc, PermissionError),
                 "terminal_tool_error": False, "should_end_turn": False,
                 "audit_ref": audit_ref, "source_policy": source_policy},
            )
        except Exception as exc:
            payload = {
                "ok": False,
                "operation": operation,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "source_policy": source_policy,
            }
            return ToolOutput(
                f"{operation} failed",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                {
                    "ok": False,
                    "retryable": False,
                    "terminal_tool_error": True,
                    "should_end_turn": True,
                    "source_policy": source_policy,
                },
            )

        finally:
            self._call_resources.reset(token)
            local_store.close()

    async def _workspace_query(self, arguments, call):
        def query():
            self._ledger.enforce_candidate_baseline_gate(self._run_id(arguments))
            capability = getattr(call, 'ontology', None)
            if capability is None:
                return {'available': False, 'reason': 'No managed Workspace ontology capability; public tools remain available.'}
            return capability.query(**{k: v for k, v in arguments.items() if k != 'run_id'})
        return await self._call('enzyme_entity_workspace_query', arguments, call, query)

    async def _workspace_report(self, arguments, call):
        async def validate():
            capability = getattr(call, 'ontology', None)
            if capability is None:
                raise ValueError('Workspace ontology requires a managed TCode Host context')
            validation = capability.validate_report(arguments['claims'])
            return await validation if inspect.isawaitable(validation) else validation
        return await self._call('enzyme_entity_workspace_report', arguments, call, validate)

    async def _resolve(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_resolve",
            arguments,
            call,
            lambda: resolve_entities(
                self._store,
                query=str(arguments.get("query") or ""),
                entity_types=list(arguments.get("entity_types") or []),
                limit=self._limit(arguments, 10),
            ),
        )

    async def _relations(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_relations",
            arguments,
            call,
            lambda: query_relations(
                self._store,
                entity_id=str(arguments.get("entity_id") or ""),
                predicates=list(arguments.get("predicates") or []),
                direction=str(arguments.get("direction") or "both"),
                neighbor_types=list(arguments.get("neighbor_types") or []),
                polarity=str(arguments.get("polarity") or "supports"),
                cursor=str(arguments.get("cursor") or "") or None,
                limit=self._limit(arguments, 20),
            ),
        )

    async def _reaction_analogs(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_reaction_analogs",
            arguments,
            call,
            lambda: find_reaction_analogs(
                self._store,
                participant_ids=list(arguments.get("participant_ids") or []),
                transformation_ids=list(arguments.get("transformation_ids") or []),
                capability_ids=list(arguments.get("capability_ids") or []),
                participant_roles=list(arguments.get("participant_roles") or []),
                minimum_matches=int(arguments.get("minimum_matches") or 1),
                cursor=str(arguments.get("cursor") or "") or None,
                limit=self._limit(arguments, 10),
            ),
        )

    async def _substrate_branch_scan(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_substrate_branch_scan",
            arguments,
            call,
            lambda: scan_same_substrate_branches(
                self._store,
                substrate_ids=list(arguments.get("substrate_ids") or []),
                limit=self._limit(arguments, 10),
            ),
        )

    async def _mechanism_fragments(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_mechanism_fragments",
            arguments,
            call,
            lambda: search_mechanism_fragments(
                self._store,
                arguments,
                limit=self._limit(arguments, 8),
            ),
        )

    async def _read_evidence(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_read_evidence",
            arguments,
            call,
            lambda: read_evidence(
                self._store,
                assertion_ids=list(arguments.get("assertion_ids") or []),
                evidence_ids=list(arguments.get("evidence_ids") or []),
                wiki_refs=list(arguments.get("wiki_refs") or []),
            ),
        )

    async def _run_init(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_run_init",
            arguments,
            call,
            lambda: self._ledger.initialize(
                self._run_id(arguments),
                arguments.get("target_reaction"),
            ),
        )

    async def _reaction_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_reaction_commit",
            arguments,
            call,
            lambda: self._ledger.commit_reaction(
                self._run_id(arguments),
                arguments.get("analysis"),
            ),
        )

    async def _run_context(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_run_context",
            arguments,
            call,
            lambda: self._ledger.context(
                self._run_id(arguments),
                str(arguments.get("stage") or ""),
            ),
        )

    async def _hypotheses_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        normalized = dict(arguments)
        workflow_run_id = self._workflow_run_id(call, "mechanism_and_capability")
        supplied_run_id = str(normalized.get("run_id") or "")
        if not _RUN_ID.fullmatch(supplied_run_id) and workflow_run_id:
            normalized["run_id"] = workflow_run_id
        run_id = self._run_id(normalized)
        workflow_scoped = workflow_run_id == run_id

        def commit() -> dict[str, Any]:
            context_digest = normalized.get("context_digest")
            if workflow_scoped:
                current_digest = self._ledger.context_digest(run_id)
                if str(context_digest or "") != current_digest:
                    context_digest = current_digest
            return self._ledger.commit_hypotheses(
                run_id,
                context_digest,
                normalized.get("hypotheses"),
                normalized.get("global_knowledge_gaps"),
            )

        return await self._call(
            "enzyme_entity_hypotheses_commit",
            normalized,
            call,
            commit,
        )

    async def _hypothesis_stage(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        return await self._call(
            "enzyme_entity_hypothesis_stage", arguments, call,
            lambda: self._ledger.stage_hypothesis(
                self._run_id(arguments), arguments.get("context_digest"),
                arguments.get("slot"), arguments.get("hypothesis"),
            ),
        )

    async def _baseline_candidates_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_baseline_candidates_commit",
            arguments,
            call,
            lambda: self._ledger.commit_baseline(
                self._run_id(arguments),
                arguments.get("context_digest"),
                arguments.get("candidates"),
            ),
        )

    async def _capability_candidates(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_capability_candidates",
            arguments,
            call,
            lambda: self._ledger.capability_candidates(
                self._run_id(arguments),
                capability_ids=list(arguments.get("capability_ids") or []),
                cofactor_ids=list(arguments.get("cofactor_ids") or []),
                intermediate_ids=list(arguments.get("intermediate_ids") or []),
                limit=self._limit(arguments, 10),
            ),
        )

    def _find_and_record_routes(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._run_id(arguments)
        result = find_routes(
            self._store,
            start_entity_ids=list(arguments.get("start_entity_ids") or []),
            target_entity_ids=list(arguments.get("target_entity_ids") or []),
            available_entity_ids=list(arguments.get("available_entity_ids") or []),
            max_steps=int(arguments.get("max_steps") or 4),
            max_routes=int(arguments.get("max_routes") or 5),
        )
        result["route_options_ref"] = self._ledger.record_route_options(run_id, result)
        return result

    async def _route_connect(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_route_connect",
            arguments,
            call,
            lambda: self._find_and_record_routes(arguments),
        )

    async def _candidates_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_candidates_commit",
            arguments,
            call,
            lambda: self._ledger.commit_candidates(
                self._run_id(arguments),
                arguments.get("context_digest"),
                arguments.get("candidates"),
                arguments.get("routes"),
                arguments.get("comparison_notes"),
                arguments.get("route_gaps"),
            ),
        )

    async def _critical_report_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        return await self._call(
            "enzyme_entity_critical_report_commit",
            arguments,
            call,
            lambda: self._ledger.commit_critical_report(
                self._run_id(arguments),
                arguments.get("context_digest"),
                arguments.get("review_decision"),
                arguments.get("checks"),
                arguments.get("limitations"),
                arguments.get("next_actions"),
                arguments.get("user_summary"),
                bool(arguments.get("allow_supersede")),
                arguments.get("resolved_findings"),
            ),
        )

    def close(self) -> None:
        self._store.close()
