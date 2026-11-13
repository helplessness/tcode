"""确定性科学层对外暴露的结构化工具定义。"""




from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from .argument_validation import validation_issues, draft_input_schema
from .context_delivery import ContextDelivery, strip_matching_transport_fields
from .scientific_checks import (STOICHIOMETRY_SCHEMA, STEP_EQUATION_SCHEMA, route_accounting, ranking_findings, ranking_consistency,
                                validate_balance_proposals, scientific_review, constrain_ranking_revision_tasks)
from .reasoning_policy import (RATIONALE_PROPERTIES, SCORE_PROPERTIES, REVISION_TASK_SCHEMA,
                               FEEDBACK_SCHEMA, READ_CARD_REVIEW_SCHEMA, BALANCE_SCHEMA, revision_target, normalize_revision_tasks, prepare_revision_tasks, feedback_candidate_ids)

from tcode_agent.plugin_sdk import (
    PluginContext,
    ToolCallContext,
    ToolDefinition,
    ToolOutput,
)

from .artifact_tools import ALLOWED_ARTIFACTS, ArtifactStore
from .audit_tools import audit_context, audit_limitations, require_complete_audit
from .audit_tools import audit_digest, require_review_consistency, require_current_review
from .review_scope import object_review_schema, aggregate_review, preserve_unaffected_screening
from .incremental_review import review_plan, merge_review, save_basis, revision_projection, policy_digest
from .audit_tools import review_issue_type
from .reasoning_policy import REVIEW_ISSUE_TYPES, REVIEW_EVIDENCE_POLICY
from .input_tools import validate_reaction_input
from .knowledge_tools import KnowledgeLibrary
from .wiki_tools import claim_index
from .retrieval_tools import RetrievalSession
from .retrieval_tools import WikiIndex
from .semantic_tools import HybridRetriever, retrieval_limitations
from .mechanism_tools import (
    build_mechanism_context,
    build_mechanism_documents,
    normalize_mechanism_references,
    saved_mechanism_rows,
    mechanism_draft_is_current,
    render_saved_mechanisms,
)
from .report_tools import (
    build_final_report,
    build_report_context,
    normalize_mermaid,
    render_final_report_chat,
    render_final_report_markdown,
)
from .screening_tools import (
    CANDIDATE_ROLE_INPUTS,
    DRAFT_DISPOSITION_SCHEMA,
    build_draft_dispositions,
    candidate_identity_baseline,
    build_candidate_baseline_context,
    build_screening_context,
    build_screening_documents,
    preserve_candidate_identity,
    discovery_tasks,
    mechanism_route_coverage,
    read_card_comparison_tasks,
    screening_completion_tasks,
    validate_candidate_name,
    uncompared_anchor_notes,
    normalize_candidate_submission,
    normalize_candidate_source,
    require_candidate_lineage,
    normalize_route_submission, inherit_route_ledger,
    normalize_route_step_binding,
    resolve_route_step_index,
    ranking_fields,
)
from .reasoning_policy import SCREENING_SUBMISSION_POLICY


_SCHEMA_FILES = {
    "reaction_input": "reaction_input.schema.json",
    "reaction_analysis": "reaction_analysis.schema.json",
    "mechanism_hypothesis": "mechanism_hypothesis.schema.json",
    "capability_mapping": "capability_mapping.schema.json",
    "candidate_ranking": "candidate_ranking.schema.json",
    "route_ranking": "route_ranking.schema.json",
    "critique": "critique.schema.json",
    "final_report": "final_report.schema.json",
    "knowledge_card": "knowledge_card.schema.json",
}

_ARTIFACT_SCHEMAS = {
    "01_reaction_analysis.json": "reaction_analysis",
    "02_mechanisms.json": "mechanism_hypothesis",
    "03_capabilities.json": "capability_mapping",
    "04_candidates.json": "candidate_ranking",
    "05_routes.json": "route_ranking",
    "06_critique.json": "critique",
    "07_final_report.json": "final_report",
}

def _json_content(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _archive_mechanism_draft(artifacts, run_id, draft):
    """按内容指纹归档，不删除活动草稿；后续写入失败时原记录仍可恢复。"""
    digest = hashlib.sha256(_json_content(draft).encode()).hexdigest()
    path = artifacts._run_path(run_id, f".history/mechanism_hypotheses.draft-{digest}.json")
    if artifacts._read_json_if_exists(path) is None:
        artifacts._atomic_write(path, _json_content(draft) + "\n")
    return path


def _plain_json_value(value: Any) -> Any:
    """把 SDK 的只读 Mapping/序列快照转换为可持久化 JSON 值。"""

    if isinstance(value, Mapping):
        return {
            str(key): _plain_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _merge_artifact_snapshots(*snapshots: Mapping[str, Any]) -> dict[str, Any]:
    """Merge bounded, validated snapshot groups for one immutable run."""

    if not snapshots:
        raise ValueError("at least one artifact snapshot is required")
    first = dict(snapshots[0])
    run = first.get("run")
    documents: dict[str, Any] = {}
    for snapshot in snapshots:
        if snapshot.get("run") != run:
            raise ValueError("artifact snapshot groups do not share the same run")
        documents.update(dict(snapshot.get("documents") or {}))
    latest = dict(snapshots[-1])
    return {
        **first,
        "artifact_manifest": latest.get("artifact_manifest") or first.get("artifact_manifest") or {},
        "artifact_count": latest.get("artifact_count") or first.get("artifact_count") or 0,
        "documents": documents,
    }


def _object_schema(properties: Mapping[str, Any], *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _retrieval_schema():
    return _object_schema({
        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
        "delivery_cursor": {"type": "string", "pattern": "^ctx_[a-f0-9]{64}_[0-9]+$",
                            "description": "完整上下文分包返回的 next_delivery_cursor；仅与 run_id 一起使用，不执行新检索。"},
        "queries": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 240}},
        "read_refs": {"type": "array", "maxItems": 6, "uniqueItems": True,
                      "items": {"type": "string", "maxLength": 120},
                      "description": "复制本轮检索摘要 read_id（R开头）或完整 ref；已读材料也可用 citation_index 短编号。读取句柄不授予引用资格。"},
        "unresolved_gaps": {"type": "array", "maxItems": 3, "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 400},
                            "description": "当前仍缺证据的机制问题；与 cursor 合用时按缺口重排缓存未读片段，不改变化学排名。查询需要改写时另传 queries。"},
        "cursor": {"type": "string", "pattern": "^pg_[a-f0-9]{64}$",
                   "description": "同一阶段检索返回的 next_cursor；取下一批摘要，不重搜。不能与 queries/resume 合用。"},
        "allow_weak": {"type": "boolean", "default": False,
                       "description": "仅关键事件缺证据时允许少量低相关线索；须解释 reason，不能把弱线索当事实。"},
        "reason": {"type": "string", "maxLength": 240},
        "resume": {"type": "boolean", "default": False,
                   "description": "仅阶段重试丢失上下文时恢复本 run 缓存；不增加探索预算。可配合 read_refs 分批恢复。"},
    }, required=("run_id",))


def _screen_retrieval_schema():
    schema = _retrieval_schema()
    schema["properties"]["candidate_draft"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": True,
        "description": "首次 chemistry-only 回包后、Wiki 证据交付前冻结的独立候选草案。",
        "items": _object_schema(
            {
                "proposal_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
                "enzyme_name": {"type": "string", "minLength": 1, "maxLength": 120},
                "enzyme_family": {"type": "string", "minLength": 1, "maxLength": 120},
                "identity_level": {"enum": ["protein", "named_enzyme", "family"]},
                "fit_reason": {"type": "string", "minLength": 1, "maxLength": 400},
                **RATIONALE_PROPERTIES,
            },
            required=("proposal_id", "enzyme_name", "identity_level", "fit_reason"),
        ),
    }
    return schema


class EnzymeToolService:
    """
    酶通路 Workflow 的结构化工具集。

    字段：
    - ``_ctx``：工作区、embedding 与日志能力。
    - ``_config``：离线、检索和产物目录配置。
    - ``_plugin_root`` / ``_plugin_version``：只读资产位置与版本。
    - ``_library`` / ``_retriever``：只读 Wiki 和混合检索器。
    - ``_card_search_states``：有界的卡片检索后端状态缓存。
    - ``_workflow_run_bindings``：阶段会话到已验证 run_id 的绑定。
    - ``_workspace_dir``：工作区内的产物相对目录。
    """

    def __init__(
        self,
        ctx: PluginContext,
        config: Mapping[str, Any],
        *,
        plugin_root: str | Path,
        plugin_version: str,
    ) -> None:
        self._ctx = ctx  # 工具共享运行时上下文。
        self._config = dict(config)  # 副本化后的工具配置。
        self._plugin_root = Path(plugin_root).resolve()  # Schema、提示词与 Wiki 根目录。
        self._plugin_version = plugin_version  # 写入运行元数据的版本。
        self._library = KnowledgeLibrary(ctx.workspace, self._plugin_root)  # 封闭知识库。
        self._retriever = HybridRetriever(getattr(ctx, "embeddings", None), self._config.get("retrieval"))  # FTS5+FAISS 检索器。
        self._card_search_states = OrderedDict()  # 最多保留 32 个卡片检索会话。
        self._workflow_run_bindings: OrderedDict[str, str] = OrderedDict()  # 防止阶段误写其他 run_id。
        self._workspace_dir = str(
            self._config.get("workspace_dir")
            or ".tcode/plugins/enzyme_reaction_discovery"
        )

    def _artifact_store(self, call: ToolCallContext) -> ArtifactStore:
        """优先使用 Host 为本次调用签发的当前工作区文件能力。"""

        workspace = getattr(call, "workspace", None) or self._ctx.workspace
        return ArtifactStore(workspace, self._workspace_dir)

    @staticmethod
    def _is_workflow_agent_call(call: ToolCallContext) -> bool:
        return str(call.session_id or "").startswith("workflow-agent-")

    def _resolve_workflow_run_id(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> tuple[str, str | None]:
        """Bind a workflow-stage session to its first verified plugin run."""

        requested = str(arguments.get("run_id") or "").strip()
        if not self._is_workflow_agent_call(call):
            return requested, None
        session_id = str(call.session_id)
        bound = self._workflow_run_bindings.get(session_id)
        if not bound:
            return requested, None
        self._workflow_run_bindings.move_to_end(session_id)
        return bound, requested if requested != bound else None

    def _bind_workflow_run(self, call: ToolCallContext, run_id: str) -> None:
        if not self._is_workflow_agent_call(call):
            return
        session_id = str(call.session_id)
        normalized = ArtifactStore.validate_run_id(run_id)
        existing = self._workflow_run_bindings.get(session_id)
        if existing and existing != normalized:
            raise ValueError("workflow agent session is already bound to another verified run")
        self._workflow_run_bindings[session_id] = normalized
        self._workflow_run_bindings.move_to_end(session_id)
        while len(self._workflow_run_bindings) > 1024:
            self._workflow_run_bindings.popitem(last=False)

    @staticmethod
    def _attach_run_id_correction(
        result: dict[str, Any],
        corrected_from: str | None,
        run_id: str,
    ) -> dict[str, Any]:
        if corrected_from is not None:
            result["run_id_correction"] = {
                "provided": corrected_from,
                "used": run_id,
                "reason": "同一 Workflow 阶段会话已绑定首次验证成功的 run。",
            }
        return result

    def validate_installation(self) -> dict[str, Any]:
        if not bool(self._config.get("offline_mode", True)):
            raise ValueError("enzyme-reaction-discovery P2 requires offline_mode=true")
        return self._library.validate()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        # Expose the existing structured context contract at the call boundary
        # so a model need not discover its fields through failed tool calls.
        context_schema = json.loads(self._ctx.workspace.read_text(
            str(self._plugin_root / "schemas/reaction_input.schema.json"), encoding="utf-8",
        ))["$defs"]["context"]
        raw_reaction_schema = {
            "type": "object",
            "description": "保留 natural_language_request 原文；自动提取未成功时，将原文明确给出的底物与产物分别填入 reactants/products。",
            "properties": {
                "natural_language_request": {"type": "string"},
                "reactants": {"description": "底物组分数组，每项保留原文中的 smiles 或 name。"},
                "products": {"description": "产物组分数组，每项保留原文中的 smiles 或 name。"},
                "context": {"anyOf": [{"type": "string"}, {"type": "null"}, context_schema]},
            },
        }
        string_array = {"type": "array", "items": {"type": "string"}, "maxItems": 64}
        search_schema = _object_schema(
            {
                "query_text": {"type": "string", "minLength": 1, "maxLength": 800},
                "cofactor_classes": string_array,
                "catalytic_capabilities": string_array,
                "transformation_tags": string_array,
                "substrate_features": string_array,
                "product_features": string_array,
                "query_terms": string_array,
                "mechanistic_roles": string_array,
                "excluded_events": string_array,
                "limit": {"type": "integer", "minimum": 1, "maximum": 15},
            }
        )
        search_schema["anyOf"] = [
            {"required": [field]}
            for field in (
                "query_text",
                "cofactor_classes",
                "catalytic_capabilities",
                "transformation_tags",
                "substrate_features",
                "product_features",
                "query_terms",
                "mechanistic_roles",
                "excluded_events",
            )
        ]
        review_issue_schema = {
            "type": "object",
            "x-drop-unknown-null": True,
            "properties": {
                "issue_type": {"enum": list(REVIEW_ISSUE_TYPES),
                    "description": "明确化学矛盾、表述过度、证据归属错误、诚实未知、交付故障分别分类；不把措辞问题当化学淘汰依据。"},
                "severity": {"enum": ["critical", "major", "minor"]},
                "statement": {"type": "string", "minLength": 1, "maxLength": 800},
                "affected_ids": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "evidence_refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
            "required": ["severity", "statement", "affected_ids", "evidence_refs"],
            "additionalProperties": False,
        }
        compact_evidence_statement_schema = {
            "type": "object",
            "properties": {
                "statement_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "kind": {
                    "enum": [
                        "fact",
                        "inference",
                        "hypothesis",
                        "counterevidence",
                        "unknown",
                    ]
                },
                "statement": {"type": "string", "minLength": 1, "maxLength": 700},
                "source_refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
            "required": ["statement_id", "kind", "statement", "source_refs"],
            "additionalProperties": False,
        }
        compact_net_change_schema = {
            "type": "object",
            "properties": {
                "statement_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                },
                "event": {"type": "string", "minLength": 1, "maxLength": 900},
                "certainty": {"enum": ["observed", "inferred", "uncertain"]},
                "basis": {"type": "string", "minLength": 1, "maxLength": 900},
            },
            "required": ["statement_id", "event", "certainty", "basis"],
            "additionalProperties": False,
        }
        compact_component_interpretation_schema = {
            "type": "object",
            "properties": {
                "component": {"type": "string", "minLength": 1, "maxLength": 200},
                "role_candidates": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 6,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "certainty": {"enum": ["observed", "inferred", "uncertain"]},
                "statement_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                },
            },
            "required": [
                "component",
                "role_candidates",
                "certainty",
                "statement_id",
            ],
            "additionalProperties": False,
        }
        compact_analysis_string_array = {
            "type": "array",
            "maxItems": 12,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 520},
        }
        short_texts = {"type": "array", "maxItems": 6, "items": {"type": "string", "minLength": 1, "maxLength": 260}}
        wiki_refs = {"type": "array", "minItems": 1, "maxItems": 6,
                     "items": {"type": "string", "maxLength": 160}}
        hypothesis_schema = _object_schema({
            "evidence_basis": {"enum": ["wiki_analogy", "model_hypothesis"], "default": "wiki_analogy"},
            "summary": {"type": "string", "minLength": 1, "maxLength": 600},
            "route_type": {"enum": ["direct", "engineered_single_enzyme", "multi_enzyme"]},
            "events": {**short_texts, "minItems": 1},
            "event_evidence": {"type": "array", "maxItems": 6, "items": _object_schema({
                "event_index": {"type": "integer", "minimum": 1, "maximum": 6},
                "status": {"enum": ["local_evidence", "related_analogy", "model_hypothesis", "unmatched_capability", "conflict"]},
                "wiki_refs": {**wiki_refs, "minItems": 0},
                "reason": {"type": "string", "minLength": 1, "maxLength": 180},
            }, required=("event_index", "status", "wiki_refs", "reason"))},
            "intermediates": short_texts,
            "capabilities": {**short_texts, "minItems": 1, "maxItems": 4},
            "wiki_refs": {**wiki_refs, "minItems": 0},
            "anchor_wiki_refs": {**wiki_refs, "minItems": 0, "maxItems": 2},
            "analogy_reason": {"type": "string", "minLength": 1, "maxLength": 400},
            "uncertainties": {**short_texts, "minItems": 1},
            "falsifiers": {**short_texts, "minItems": 1, "maxItems": 3},
            "confidence": {"enum": ["high", "medium", "low", "unresolved"]},
            "counterfactual_bridge": _object_schema({
                "native_termination": {**short_texts, "maxItems": 4},
                "target_alternative_termination": {**short_texts, "maxItems": 4},
                "unsupported_assumptions": {**short_texts, "maxItems": 6},
                "validation_signals": {**short_texts, "minItems": 1, "maxItems": 4},
            }, required=("native_termination", "target_alternative_termination",
                         "unsupported_assumptions", "validation_signals")),
        }, required=("summary", "route_type", "events", "intermediates", "capabilities",
                     "analogy_reason", "uncertainties", "falsifiers", "confidence"))
        # 事件与证据同一对象提交，消除模型维护平行数组编号的风险。
        # 旧文本事件仍可读取；最终科学产物和校验契约不变。
        inline_evidence_schema = deepcopy(hypothesis_schema["properties"]["event_evidence"]["items"])
        inline_evidence_schema["properties"].pop("event_index")
        inline_evidence_schema["required"].remove("event_index")
        hypothesis_schema["properties"]["events"]["items"] = {"anyOf": [
            short_texts["items"],
            _object_schema({"text": short_texts["items"], "evidence": inline_evidence_schema}, required=("text",)),
        ]}
        cofactor_schema = _object_schema({
            "evidence_basis": {"enum": ["wiki_analogy", "model_hypothesis"], "default": "wiki_analogy"},
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "mechanism_ids": {"type": "array", "maxItems": 10, "uniqueItems": True, "items": {"type": "string", "pattern": "^M(?:10|[1-9])$"}},
            "reason": {"type": "string", "minLength": 1, "maxLength": 400},
            "wiki_refs": {**wiki_refs, "minItems": 0},
            "uncertainties": {**short_texts, "minItems": 1},
            "confidence": {"enum": ["high", "medium", "low", "unresolved"]},
        }, required=("name", "reason", "uncertainties", "confidence"))
        for schema in (hypothesis_schema, cofactor_schema):
            schema["allOf"] = [{"if": {"required": ["evidence_basis"], "properties": {
                "evidence_basis": {"const": "model_hypothesis"}}}, "else": {
                    "required": ["wiki_refs"], "properties": {"wiki_refs": {"minItems": 1}}}}]
        route_disposition_schema = {
            "type": "object",
            "properties": {
                "route_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "step_count": {"type": "integer", "minimum": 1, "maximum": 8,
                               "description": "完整路线可省略；若回显则必须等于实际 steps 数，工具核对后不重复保存。"},
                "disposition": {"enum": ["retain", "demote", "exclude", "revise"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 800},
                "evidence_refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
            "required": ["route_id", "disposition", "reason", "evidence_refs"],
            "additionalProperties": False,
        }
        compact_candidate_schema = _object_schema({
            **SCORE_PROPERTIES,
            "candidate_kind": {"enum": ["wiki_enzyme", "model_enzyme", "unmatched_capability"], "default": "wiki_enzyme"},
            "card_id": {"type": "string", "pattern": "^ERD-CARD-[0-9]{3,}$"},
            "proposal_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "identity_level": {"enum": ["protein", "named_enzyme", "family"],
                               "description": "库外提议：仅家族用 family；未唯一定位蛋白用 named_enzyme；明确具体物种/蛋白才用 protein。"},
            "enzyme_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "specific_name": {"type": "string", "minLength": 1, "maxLength": 120,
                              "description": "仅模型命名酶可选：在冻结通用酶名之后明确回想出的具体蛋白/物种名称。须有 name_evidence；保留原 proposal_id 和 enzyme_name，最终报告优先展示此名称，仍未核实。"},
            "prior_specific_name": {"type": "string", "minLength": 1, "maxLength": 120,
                                    "description": "只读历史名称；提交回显不作为当前身份依据，由运行记录重新派生。"},
            "name_basis": {"enum": ["established_name", "target_description", "uncertain", "designed_construct"],
                           "description": "名称依据，不是天然/工程化分类。已知或用户提供的具名工程突变体仍用 established_name，说明 name_evidence，并保留原 ID；用户来源另填 user_provided_name 和逐字 user_input_quote。designed_construct 仅限本轮新设想、尚未存在的设计，不得仅因有突变位点或 engineered 字样使用。目标活性未知不改变实体类型。"},
            "identity_binding": _object_schema({
                "card_id": {"type": "string", "pattern": "^ERD-CARD-[0-9]{3,}$"},
                "refs": {**wiki_refs, "minItems": 1},
                "name_quote": {"type": "string", "minLength": 3, "maxLength": 120},
                "organism_quote": {"type": "string", "minLength": 1, "maxLength": 240},
                "basis": {"type": "string", "minLength": 1, "maxLength": 400},
            }, required=("card_id", "refs", "name_quote", "organism_quote", "basis")),
            "user_provided_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "user_input_quote": {"type": "string", "minLength": 1, "maxLength": 240},
            "name_evidence": {"type": "string", "minLength": 1, "maxLength": 240,
                              "description": "说明名称来自模型已有知识、已读 Wiki 或当前用户输入；已具名的工程变体可作为未核实候选，不因此视为假想设计。这里的身份依据不证明目标活性。"},
            "discovery_reason": {"type": "string", "minLength": 1, "maxLength": 400,
                                 "description": "只有真正新增的库外候选使用：说明发现依据及与冻结草案的区别；原有候选必须沿用原 ID 与名称。"},
            "enzyme_family": {"type": "string", "minLength": 1, "maxLength": 120},
            "required_capability": {"type": "string", "minLength": 1, "maxLength": 260},
            "cofactor_classes": {**short_texts, "maxItems": 4},
            "candidate_role": {"enum": list(CANDIDATE_ROLE_INPUTS)},
            "component_function": {"enum": ["oxidation", "dephosphorylation", "dehydration", "alternative_termination", "activation", "other"]},
            "native_reaction_quote": {"type": "string", "maxLength": 240},
            "substrate_relation": {"enum": ["reported_exact", "mechanistic_analogy", "unresolved"]},
            "mechanism_ids": {"type": "array", "maxItems": 10, "uniqueItems": True, "items": {"type": "string", "pattern": "^M(?:10|[1-9])$"}},
            "score": {"type": "number", "minimum": 0, "maximum": 100},
            "confidence": {"enum": ["high", "medium", "low", "unresolved"]},
            "support_refs": {**wiki_refs, "minItems": 0},
            "support_reason": {"type": "string", "minLength": 1, "maxLength": 400},
            "contradiction_refs": {**wiki_refs, "minItems": 0},
            "conflict_reason": {"type": "string", "maxLength": 400},
            "hard_conflict": {"type": "boolean"},
            "engineering_plausibility": {"enum": ["high", "medium", "low", "unknown"]},
            "knowledge_gap": {"type": "string", "minLength": 1, "maxLength": 400},
            "verification_note": {"type": "string", "maxLength": 400,
                                  "description": "只读回显字段；提交时忽略并由工具重新生成。"},
        }, required=("candidate_role", "score", "support_reason", "knowledge_gap"))
        compact_candidate_schema["oneOf"] = [
            {"required": ["card_id", "support_refs"], "properties": {"candidate_kind": {"const": "wiki_enzyme"},
             "support_refs": {"minItems": 1}}, "not": {"anyOf": [{"required": [f]} for f in
             ("proposal_id", "enzyme_name", "enzyme_family", "required_capability", "cofactor_classes")]}},
            {"required": ["candidate_kind", "proposal_id", "enzyme_name"],
             "properties": {"candidate_kind": {"const": "model_enzyme"}, "engineering_plausibility": {"const": "unknown"}},
             "not": {"anyOf": [{"required": ["card_id"]}, {"required": ["required_capability"]}]}},
            {"required": ["candidate_kind", "proposal_id", "required_capability"],
             "properties": {"candidate_kind": {"const": "unmatched_capability"},
                            "candidate_role": {"const": "route_component"}, "engineering_plausibility": {"const": "unknown"}},
             "not": {"anyOf": [{"required": [f]} for f in ("card_id", "enzyme_name", "enzyme_family")]}},
        ]
        compact_candidate_schema["allOf"] = [
            {"if": {"properties": {"substrate_relation": {"const": "reported_exact"}},
                    "required": ["substrate_relation"]},
             "then": {"required": ["native_reaction_quote"],
                      "properties": {"native_reaction_quote": {"minLength": 1}}}},
        ]
        compact_route_step_schema = {
            "type": "object",
            "properties": {
                "step_kind": {"enum": ["enzymatic", "nonenzymatic"],
                              "description": "默认酶催化；非酶水解/自发反应/外加化学处理明确用 nonenzymatic，不创建假酶或能力占位。"},
                "condition_note": {"type": "string", "minLength": 1, "maxLength": 400,
                                   "description": "非酶步骤的条件与可行性边界；不确定须说明，不默认自发或定量完成。"},
                "equation": STEP_EQUATION_SCHEMA,
                "chemistry_check": {
                    "type": "object",
                    "required": ["stereochemistry", "electron_balance", "basis"],
                    "properties": {
                        "stereochemistry": {"enum": ["compatible", "unknown", "conflict"]},
                        "electron_balance": {"enum": ["balanced", "unknown", "conflict"]},
                        "bond_topology": {"enum":["compatible","unknown","conflict"]},
                        "domain_assignment": {"enum":["compatible","unknown","conflict"]},
                        "regioselectivity": {"enum":["compatible","unknown","conflict"]},
                        "catalyst_scope": {"enum":["supported","hypothesis","unknown","analogy_only"]},
                        "broken_bonds": {"type":"array","maxItems":3,"items":{"type":"string","minLength":1,"maxLength":96}},
                        "retained_bonds": {"type":"array","maxItems":3,"items":{"type":"string","minLength":1,"maxLength":96}},
                        "basis": {"type": "string", "minLength": 1, "maxLength": 600,
                                  "description": "化学核对说明，建议300字以内；最多600字，保留必要冲突和限制，不机械截断。"}
                    },
                    "additionalProperties": False
                },
                "intended_proposal_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
                "substitution_basis": {"type": "string", "minLength": 1, "maxLength": 360},
                "substrate_analogy": {"type": "string", "minLength": 1, "maxLength": 1200,
                                      "description": "标准底物/产物与目标步骤的类比及差异说明；不需要 intended_proposal_id，也不证明目标活性。"},
                "mechanism_ids": {"type": "array", "maxItems": 8, "uniqueItems": True,
                                  "items": {"type": "string", "minLength": 1, "maxLength": 64},
                                  "description": "模型明确判断本步骤关联的当前机制 ID；不按路线类型猜测。"},
                "candidate_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "candidate_ref": {"type": "string", "minLength": 1, "maxLength": 160},
                "input": {"type": "string", "minLength": 1, "maxLength": 400},
                "output": {"type": "string", "minLength": 1, "maxLength": 400},
                "required_capability": {"type": "string", "minLength": 1, "maxLength": 260},
                "card_id": {"type": "string", "pattern": "^ERD-CARD-[0-9]{3,}$"},
                "proposal_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
                "evidence_refs": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
                "material_handoff": {"type": "string", "minLength": 1, "maxLength": 520},
            },
            "required": ["input", "output", "evidence_refs"],
            "oneOf": [
                {"required": ["card_id"], "not": {"anyOf": [{"required": ["proposal_id"]}, {"required": ["required_capability"]}]},
                 "properties": {"evidence_refs": {"minItems": 1}}},
                {"required": ["proposal_id"], "not": {"anyOf": [{"required": ["card_id"]}, {"required": ["required_capability"]}]}},
                {"required": ["required_capability"], "not": {"anyOf": [{"required": ["card_id"]}, {"required": ["proposal_id"]}]}},
                {"required": ["candidate_id"], "not": {"anyOf": [{"required": [f]} for f in ("card_id", "proposal_id", "required_capability", "candidate_ref")]}},
                {"required": ["candidate_ref"], "not": {"anyOf": [{"required": [f]} for f in ("card_id", "proposal_id", "required_capability", "candidate_id")]}},
            ],
            "additionalProperties": False,
        }
        catalyst_choices = compact_route_step_schema.pop("oneOf")
        compact_route_step_schema["oneOf"] = [
            {"required": ["step_kind", "condition_note"], "properties": {"step_kind": {"const": "nonenzymatic"}},
             "not": {"anyOf": [{"required": [key]} for key in
                     ("card_id", "proposal_id", "required_capability", "candidate_id", "candidate_ref", "intended_proposal_id", "substitution_basis")]}},
            {"not": {"required": ["step_kind"], "properties": {"step_kind": {"const": "nonenzymatic"}}},
             "oneOf": catalyst_choices},
        ]
        compact_route_schema = {
            "type": "object",
            "properties": {
                **SCORE_PROPERTIES,
                "balance_review": BALANCE_SCHEMA,
                "stoichiometry": STOICHIOMETRY_SCHEMA,
                "route_id": {"type": "string", "minLength": 1, "maxLength": 160},
                "route_type": {"enum": ["direct", "engineered_single_enzyme", "multi_enzyme"]},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": compact_route_step_schema,
                },
                "contradiction_refs": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 3,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
                "unsupported_intermediates": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "confidence": {"enum": ["high", "medium", "low", "unresolved"]},
            },
            "required": [
                "route_id",
                "route_type",
                "steps",
                "score",
            ],
            "additionalProperties": False,
        }
        # 暂存层只保存模型的小批草稿；正式提交前会为模型候选补空引用，
        # 并为 Wiki 候选从已选候选继承实际引用。因此不能在暂存时因可恢复
        # 的缺省 evidence_refs 提前拒绝整条路线。
        staging_route_schema = deepcopy(compact_route_schema)
        staging_step_schema = staging_route_schema["properties"]["steps"]["items"]
        staging_step_schema["required"].remove("evidence_refs")
        staging_step_schema["oneOf"][0].pop("properties", None)
        self._screen_candidate_schema = compact_candidate_schema
        self._screen_route_schema = compact_route_schema
        self._screen_staging_route_schema = staging_route_schema
        route_header_schema = deepcopy(compact_route_schema)
        route_header_schema["properties"].pop("steps")
        route_header_schema["required"].remove("steps")
        route_header_schema["properties"]["step_count"] = {"type": "integer", "minimum": 1, "maximum": 8}
        route_header_schema["required"].append("step_count")
        self._screen_route_header_schema = route_header_schema
        stage_schema = _object_schema({
            "read_card_reviews": {"type": "array", "maxItems": 24, "items": READ_CARD_REVIEW_SCHEMA,
                                  "description": "仅记录本轮实际回读卡的比较或暂缓理由，不强制提名。可随任一候选/路线暂存。"},
            "mechanism_feedback": {"type": "array", "maxItems": 3, "items": FEEDBACK_SCHEMA,
                                   "description": "可随候选暂存机制反馈；use_staged提交自动带入，不在说明正文假称已提交。"},
            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
            "context_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "candidate": draft_input_schema(compact_candidate_schema),
            "route_header": draft_input_schema(route_header_schema),
            "route_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "step_index": {"type": "integer", "minimum": 1, "maximum": 8},
            "route_step": draft_input_schema(staging_step_schema),
            "candidates": {"type": "array", "maxItems": 3, "items": draft_input_schema(compact_candidate_schema),
                           "description": "优先小批保存，每批最多3个候选；逐项校验，成功项保留，只修失败项。"},
            "routes": {"type": "array", "maxItems": 2, "items": draft_input_schema(staging_route_schema),
                       "description": "每批最多2条含完整steps的短路线；长路线仍可分步提交，不省略科学字段。"},
        }, required=("run_id",))
        stage_schema["oneOf"] = [
            {"required": ["candidate"], "not": {"anyOf": [{"required": [key]} for key in
                ("route_header", "route_step", "route_id", "step_index", "candidates", "routes")]}},
            {"required": ["route_header"], "not": {"anyOf": [{"required": [key]} for key in
                ("candidate", "route_step", "route_id", "step_index", "candidates", "routes")]}},
            {"required": ["route_id", "route_step"], "not": {"anyOf": [{"required": [key]} for key in
                ("candidate", "route_header", "candidates", "routes")]}},
            {"anyOf": [{"required": ["candidates"]}, {"required": ["routes"]}],
             "not": {"anyOf": [{"required": [key]} for key in
                ("candidate", "route_header", "route_id", "step_index", "route_step")]}},
            {"required": ["read_card_reviews"], "not": {"anyOf": [{"required": [key]} for key in
                ("candidate", "route_header", "route_id", "step_index", "route_step", "candidates", "routes")]},
             "properties": {"read_card_reviews": {"minItems": 1}}},
        ]
        self._screen_stage_input_schema = stage_schema
        definitions = (
            ToolDefinition(
                name="enzyme_run_init",
                description="接收 run_id 和 reaction 进行校验与初始化；可保留原始请求，并提交从中识别的 reactants/products。净变化判断属于 enzyme_reaction_commit，不得混入本工具。",
                input_schema={
                    **_object_schema(
                        {
                            "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                            "reaction": raw_reaction_schema,
                            "reaction_input": {"type": "object"},
                            "idempotency_key": {"type": "string", "maxLength": 256},
                        }
                    ),
                    "oneOf": [
                        {"required": ["reaction"]},
                        {"required": ["reaction_input"]},
                    ],
                },
                handler=self._run_init,
                version="1.2.3",
                side_effect_level="write",
                result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_input_validate",
                description="规范化反应输入并执行不依赖化学引擎的保守 SMILES 文本检查。",
                input_schema=_object_schema({"reaction": {"type": "object"}}, required=("reaction",)),
                handler=self._input_validate,
                version="1.0.0",
                side_effect_level="none",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_reaction_commit",
                description="读取已初始化的规范化输入，把紧凑净变化判断确定性扩展、校验并提交 01；避免模型复制完整输入或逐层探测阶段 Schema。",
                input_schema=_object_schema(
                    {
                        "run_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
                        },
                        "evidence_statements": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 16,
                            "items": compact_evidence_statement_schema,
                        },
                        "net_changes": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 12,
                            "items": compact_net_change_schema,
                        },
                        "component_interpretations": {
                            "type": "array",
                            "maxItems": 12,
                            "items": compact_component_interpretation_schema,
                        },
                        "assumptions": compact_analysis_string_array,
                        "warnings": compact_analysis_string_array,
                        "unresolved_questions": compact_analysis_string_array,
                        "idempotency_key": {"type": "string", "maxLength": 256},
                        "allow_supersede": {"type": "boolean", "default": False},
                    },
                    required=(
                        "run_id",
                        "evidence_statements",
                        "net_changes",
                        "component_interpretations",
                        "assumptions",
                        "warnings",
                        "unresolved_questions",
                    ),
                ),
                handler=self._reaction_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=3072,
            ),
            ToolDefinition(
                name="enzyme_mechanism_context",
                description="返回反应与本轮检索状态；queries 检索本地索引摘要，read_refs 按需读取证据。无全库注入或预选辅因子。",
                input_schema=_retrieval_schema(),
                handler=self._mechanism_context,
                version="3.6.1",
                side_effect_level="write",
                result_budget_tokens=8192,
            ),
            ToolDefinition(
                name="enzyme_mechanism_hypothesis_stage",
                description="逐条校验并暂存一个机制假说；重复 slot 会覆盖草稿，不提交阶段 artifact。用于避免把全部机制塞进一次大型工具调用。",
                input_schema=_object_schema({
                    "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                    "context_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "slot": {"type": "integer", "minimum": 1, "maximum": 10},
                    "hypothesis": hypothesis_schema,
                }, required=("run_id", "slot", "hypothesis")),
                handler=self._mechanism_hypothesis_stage,
                version="1.0.0", side_effect_level="write", result_budget_tokens=1024,
            ),
            ToolDefinition(
                name="enzyme_mechanism_plan_commit",
                description="原子保存机制与辅因子排序；hypotheses 为空时使用本轮逐条暂存的机制。区分 Wiki 类比与未核实模型假说，不生成科学判断。",
                input_schema=_object_schema({
                    "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                    "context_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "hypotheses": {"type": "array", "maxItems": 10, "items": hypothesis_schema},
                    "cofactors": {"type": "array", "maxItems": 3, "items": cofactor_schema},
                    "knowledge_gaps": {**short_texts, "maxItems": 12,
                        "description": "全局科学缺口最多12条，每条260字符；机制专属缺口优先放各假说uncertainties，不重复已记录内容。"},
                    "finish_exploration": {"type": "boolean", "description": "请求收尾；未检查的缺口仍在既有预算内补查，已检查或预算不足时保留未知，不跳过科学校验。"},
                    "retrieval_questions": {"type": "array", "maxItems": 3, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        "description": "将仍影响机制判断的知识缺口改写为独立的正向科学问题，不夹带未收录结论、实验待办或答案。占用现有检索预算。"},
                    "idempotency_key": {"type": "string", "maxLength": 256},
                    "allow_supersede": {"type": "boolean", "default": False},
                }, required=("run_id", "cofactors")),
                handler=self._mechanism_plan_commit,
                version="3.6.0", side_effect_level="write", result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_library_stats",
                description="返回封闭知识库版本、卡片数及类别统计，不暴露 hidden gold。",
                input_schema=_object_schema({}),
                handler=self._library_stats,
                version="1.0.0",
                side_effect_level="read",
            ),
            ToolDefinition(
                name="enzyme_screen_context",
                description="先返回不含 Wiki 的反应上下文并冻结独立候选草案，再允许 queries 补查 Wiki 证据；Wiki 不是候选白名单或排序加分项。",
                input_schema=_screen_retrieval_schema(),
                handler=self._screen_context,
                version="3.7.1",
                side_effect_level="write",
                result_budget_tokens=16384,
            ),
            ToolDefinition(
                name="enzyme_screen_commit",
                description="保存 wiki_enzyme、未核实 model_enzyme 或 unmatched_capability；后两类使用 proposal_id，路线按同一引用连接。只为 Wiki 酶补齐身份，不重算科学分数。",
                input_schema=_object_schema(
                    {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "context_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        "use_staged": {"type": "boolean", "default": False,
                            "description": "使用同一上下文暂存的候选和路线；此时 candidates/routes 可省略或传空数组。"},
                        "defer_incomplete_routes": {"type": "boolean", "default": False,
                            "description": "仅 use_staged=true 时可用。无法补齐的路线显式留在草稿并记作缺口，不作为完整路线交付，不丢弃已存步骤。"},
                        "candidates": {"type": "array", "maxItems": 30, "items": compact_candidate_schema},
                        "routes": {"type": "array", "maxItems": 10, "items": {"type": "object"}},
                        "draft_dispositions": {"type": "array", "maxItems": 30, "items": draft_input_schema(DRAFT_DISPOSITION_SCHEMA),
                            "description": "逐项核对处置。不安全合并、未知/重复 ID 或与已保留项矛盾的处置单独拒绝，不阻断其他候选与路线提交。"},
                        "mechanism_feedback": {"type": "array", "maxItems": 3, "items": FEEDBACK_SCHEMA,
                            "description": "候选催化能力提示的新机制或对既有机制的修正；只记录待审核假说，不自动改写上游。"},
                        "read_card_reviews": {"type": "array", "maxItems": 24, "items": READ_CARD_REVIEW_SCHEMA},
                        "knowledge_gaps": {
                            "type": "array",
                            "description": "建议归并为12个以内的独立缺口；不因条数阻断提交，完整保留原说明。",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        },
                        "finish_exploration": {"type": "boolean", "description": "请求收尾；未检查的缺口仍在既有预算内补查，已检查或预算不足时保留未知，不跳过科学校验。"},
                        "retrieval_questions": {"type": "array", "maxItems": 3, "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1, "maxLength": 240},
                            "description": "将仍影响机制判断的知识缺口改写为独立的正向科学问题，不夹带未收录结论、实验待办或答案。占用现有检索预算。"},
                        "idempotency_key": {"type": "string", "maxLength": 256},
                        "allow_supersede": {"type": "boolean", "default": False},
                    },
                    required=("run_id",),
                ),
                handler=self._screen_commit,
                version="3.6.1",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_screen_items_stage",
                description="优先小批暂存：candidates最多3项、routes最多2条完整短路线，可同批先存候选再存路线；逐项校验与保留，失败只修当前项。长路线仍可用route_header/route_step。暂存不是科学通过；按回执ID绑定路线，预留取证和use_staged=true正式提交窗口。",
                input_schema=stage_schema,
                handler=self._screen_items_stage, version="2.0.0",
                side_effect_level="write", result_budget_tokens=1024,
            ),
            ToolDefinition(
                name="enzyme_card_search",
                description="融合词面和语义相关性检索酶 Wiki；向量不可用会明确降级，检索分不是催化适合度，不做化学评分。",
                input_schema=search_schema,
                handler=self._card_search,
                version="3.3.0",
                side_effect_level="write",
                result_budget_tokens=6144,
            ),
            ToolDefinition(
                name="enzyme_card_get",
                description="按严格 card_id 白名单渐进读取酶 Wiki；优先 summary，再按需读取 sections，只有最终审计才读取 full。",
                input_schema=_object_schema(
                    {
                        "card_id": {"type": "string", "pattern": "^ERD-CARD-[0-9]{3,}$"},
                        "detail_level": {
                            "enum": ["summary", "sections", "full"],
                            "default": "full"
                        },
                        "sections": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 9,
                            "uniqueItems": True,
                            "items": {
                                "enum": [
                                    "identity",
                                    "wiki",
                                    "classification",
                                    "mechanism",
                                    "evidence",
                                    "known_limitations"
                                ]
                            }
                        }
                    },
                    required=("card_id",),
                ),
                handler=self._card_get,
                version="2.0.0",
                side_effect_level="read",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_schema_validate",
                description="使用插件内固定的 Draft 2020-12 Schema 校验阶段 JSON。",
                input_schema=_object_schema(
                    {
                        "schema_name": {"enum": sorted(_SCHEMA_FILES)},
                        "document": {},
                    },
                    required=("schema_name", "document"),
                ),
                handler=self._schema_validate,
                version="1.0.0",
                side_effect_level="read",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_artifact_commit",
                description="校验后将白名单内阶段产物原子、幂等地提交到当前 run。",
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "artifact_name": {"enum": sorted(ALLOWED_ARTIFACTS)},
                        "document": {},
                        "text": {"type": "string", "minLength": 1},
                        "schema_name": {"enum": sorted(_SCHEMA_FILES)},
                        "idempotency_key": {"type": "string", "maxLength": 256},
                        "allow_supersede": {"type": "boolean", "default": False},
                        "render_companions": {"type": "boolean", "default": False},
                    },
                    "required": ["run_id", "artifact_name"],
                    "oneOf": [{"required": ["document"]}, {"required": ["text"]}],
                    "additionalProperties": False,
                },
                handler=self._artifact_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=16384,
            ),
            ToolDefinition(
                name="enzyme_report_context",
                description="核对 01—05 实际引用证据；按 evidence_audit.next_page 有界分页。section可回读已交付的scientific_checks/review_context/previous_review字段，query选字段、offset按next_offset，不重新搜索。",
                input_schema=_object_schema(
                    {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "page": {"type": "integer", "minimum": 0, 'description': '省略则自动继续下一未读页；显式页码可回看缓存。'},
                        'section': {'enum': ['scientific_checks', 'review_context', 'previous_review']},
                        'query': {'type': 'string', 'maxLength': 200},
                        'offset': {'type': 'integer', 'minimum': 0},
                    },
                    required=("run_id",),
                ),
                handler=self._report_context,
                version="1.0.0",
                side_effect_level="read",
                result_budget_tokens=16384,
            ),
            ToolDefinition(
                name="enzyme_review_prepare",
                description="仅在用户明确要求时重开已交付运行的第四阶段；校验并备份旧审核与报告，禁止改写上游。",
                input_schema=_object_schema({"run_id": {"type": "string", "minLength": 1},
                                             "confirm": {"const": True}}, required=("run_id", "confirm")),
                handler=self._review_prepare,
                version="1.0.0", side_effect_level="write", result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_critique_commit",
                description="把短小的自动批判判定确定性扩展为 06_critique.json、校验并提交；无需模型手写完整 Schema。",
                input_schema=_object_schema(
                    {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "review_decision": {"enum": ["PASS", "PARTIAL_PASS", "REVISE"]},
                        "candidate_reviews": object_review_schema("candidate_id"),
                        "mechanism_reviews": object_review_schema("hypothesis_id"),
                        "excluded_candidate_ids": {"type": "array", "maxItems": 10, "uniqueItems": True,
                                                   "items": {"type": "string", "minLength": 1}},
                        "excluded_mechanism_ids": {"type": "array", "maxItems": 8, "uniqueItems": True,
                                                   "items": {"type": "string", "minLength": 1}},
                        "hard_conflicts": {
                            "type": "array",
                            "maxItems": 4,
                            "items": review_issue_schema,
                        },
                        "evidence_gaps": {
                            "type": "array",
                            "maxItems": 5,
                            "items": review_issue_schema,
                        },
                        "route_dispositions": {
                            "type": "array",
                            "maxItems": 10,
                            "items": route_disposition_schema,
                        },
                        "required_revisions": {
                            "type": "array",
                            "maxItems": 5,
                            "items": {"type": "string", "minLength": 1, "maxLength": 800},
                        },
                        "revision_tasks": {"type": "array", "maxItems": 5, "items": REVISION_TASK_SCHEMA,
                            "description": "与修订意见对应的最小问题范围；不增加修订次数。纯交付故障用 delivery，不重跑化学。"},
                        "idempotency_key": {"type": "string", "maxLength": 256},
                        "allow_supersede": {"type": "boolean", "default": False},
                    },
                    required=(
                        "run_id",
                        "review_decision",
                        "hard_conflicts",
                        "evidence_gaps",
                        "route_dispositions",
                        "required_revisions",
                    ),
                ),
                handler=self._critique_commit,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=2048,
            ),
            ToolDefinition(
                name="enzyme_report_finalize",
                description="从已提交的 01—06 汇总并落盘统一的最终 JSON、Markdown 和 Mermaid；保留 PASS/REVISE 审核状态与修订意见。",
                input_schema=_object_schema(
                    {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "idempotency_key": {"type": "string", "maxLength": 256},
                        "allow_supersede": {"type": "boolean", "default": False},
                    },
                    required=("run_id",),
                ),
                handler=self._report_finalize,
                version="1.0.0",
                side_effect_level="write",
                result_budget_tokens=4096,
            ),
            ToolDefinition(
                name="enzyme_run_snapshot",
                description="读取一次运行的元数据、审批摘要和已提交产物清单。",
                input_schema=_object_schema(
                    {
                        "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                        "artifact_names": {
                            "type": "array",
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {"enum": sorted(ALLOWED_ARTIFACTS)},
                        },
                    },
                    required=("run_id",),
                ),
                handler=self._run_snapshot,
                version="1.0.0",
                side_effect_level="read",
                result_budget_tokens=32768,
            ),
        )
        # 恢复仅作用于明确的单对象信封；数组中多个候选/步骤的字段不自动归属。
        for definition in definitions:
            schema = definition.input_schema
            schema["x-tcode-argument-format"] = "v1"
            schema["x-field-placement"] = [key for key in
                ("reaction", "hypothesis", "candidate", "route_header", "route_step")
                if key in schema.get("properties", {})]
            if "route_header" in schema["x-field-placement"]:
                schema["x-field-placement-prefer-child"] = {"route_header": ["route_id"]}
        return definitions

    async def _call(
        self,
        title: str,
        operation: Callable[[], Any | Awaitable[Any]],
    ) -> ToolOutput:
        metadata: dict[str, Any] = {}
        try:
            result = operation()
            if hasattr(result, "__await__"):
                result = await result
            payload = {"ok": True, "result": result}
            if isinstance(result, Mapping):
                if (result.get("followup_required") or result.get('committed') is False
                        or result.get('context_delivery') == 'paged'):
                    metadata["stage_continuation"] = True
                if result.get("accepted") is False:
                    metadata["item_validation_failed"] = True
                if isinstance(result.get("stage_receipt"), Mapping):
                    metadata["stage_receipt"] = _plain_json_value(result["stage_receipt"])
                raw_artifacts: list[Any] = []
                if isinstance(result.get("artifact"), Mapping):
                    raw_artifacts.append(result.get("artifact"))
                for key in ("artifacts", "committed_artifacts"):
                    value = result.get(key)
                    if isinstance(value, (list, tuple)):
                        raw_artifacts.extend(value)
                artifacts: list[dict[str, Any]] = []
                seen_artifacts: set[tuple[str, str]] = set()
                for item in raw_artifacts:
                    if not isinstance(item, Mapping):
                        continue
                    normalized = dict(item)
                    fingerprint = (
                        str(normalized.get("name") or ""),
                        str(normalized.get("path") or ""),
                    )
                    if fingerprint in seen_artifacts:
                        continue
                    seen_artifacts.add(fingerprint)
                    artifacts.append(normalized)
                if artifacts:
                    metadata["artifacts"] = artifacts[:32]
                    metadata["artifact_refs"] = {
                        str(item.get("name") or f"artifact_{index}"): str(item.get("path") or "")
                        for index, item in enumerate(artifacts[:32], start=1)
                        if str(item.get("path") or "").strip()
                    }
        except Exception as exc:
            payload = {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            # A JSON ok:false alone is not a Host tool failure. Publish the
            # explicit SDK error so logging and consecutive-failure guards see
            # it; only parameter/data validation permits a bounded correction.
            # A missing run caused by a copied/typed run_id is an input error,
            # not a storage outage. Let the bounded agent policy correct it
            # once. Missing manifests/artifact files and other I/O failures
            # remain terminal so corruption cannot turn into a retry loop.
            missing_run_argument = isinstance(exc, FileNotFoundError) and str(exc).startswith(
                ("run not found: ", "run is not initialized: ")
            )
            retryable = isinstance(exc, (ValueError, KeyError)) or missing_run_argument
            metadata.update(error=str(exc)[:2000] or type(exc).__name__,
                            retryable=retryable, terminal_tool_error=not retryable,
                            should_end_turn=not retryable)
        return ToolOutput(title, _json_content(payload), {"ok": payload["ok"], **metadata})

    async def _run_init(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation() -> dict[str, Any]:
            artifacts = self._artifact_store(call)
            raw_reaction = (
                arguments.get("reaction")
                if arguments.get("reaction") is not None
                else arguments.get("reaction_input")
            )
            # Both public aliases pass through the same tolerant boundary.
            # ``reaction_input`` is retained for backwards compatibility, but
            # it must not force callers to discover the normalized schema by
            # trial-and-error before a run can be initialized.
            input_validation = validate_reaction_input(
                raw_reaction,
                configured_max_route_steps=int(
                    self._config.get("max_route_steps") or 8
                ),
            )
            if not input_validation["valid"]:
                raise ValueError(
                    "reaction input validation failed: "
                    + _json_content(input_validation["errors"][:10])
                )
            reaction_input = input_validation["normalized_reaction"]
            validation = self._validate_document("reaction_input", reaction_input)
            if not validation["valid"]:
                raise ValueError(
                    "reaction_input schema validation failed: "
                    + _json_content(validation["errors"][:5])
                )
            run_id = str(arguments.get("run_id") or "").strip()
            if not run_id:
                identity = f"{call.session_id}:{call.message_id}:{call.call_id or ''}"
                run_id = "erd_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            stats = self._library.manifest()
            result = artifacts.init_run(
                run_id=run_id,
                reaction_input=reaction_input,
                idempotency_key=str(arguments.get("idempotency_key") or ""),
                plugin_version=self._plugin_version,
                knowledge_version=stats["knowledge_version"],
                knowledge_fingerprint=self._library.fingerprint(),
            )
            self._bind_workflow_run(call, run_id)
            result["input_validation"] = {
                "valid": True,
                "has_unresolved_input_ambiguity": bool(
                    input_validation.get("has_unresolved_input_ambiguity")
                ),
                "validation_scope": input_validation.get("validation_scope"),
                "warnings": input_validation.get("warnings"),
                "reaction_id": reaction_input.get("reaction_id"),
            }
            return result

        return await self._call("初始化酶发现运行", operation)

    async def _input_validate(self, arguments: Mapping[str, Any], _call: ToolCallContext) -> ToolOutput:
        return await self._call(
            "校验反应输入",
            lambda: validate_reaction_input(
                arguments.get("reaction"),
                configured_max_route_steps=int(self._config.get("max_route_steps") or 8),
            ),
        )

    async def _reaction_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        def operation() -> dict[str, Any]:
            run_id, _corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(run_id)
            self._bind_workflow_run(call, run_id)
            run = _plain_json_value(snapshot.get("run") or {})
            reaction_input = _plain_json_value(run.get("reaction_input") or {})
            if not reaction_input:
                raise ValueError("initialized run is missing normalized reaction_input")
            if str(run.get("run_id") or "") != run_id:
                raise ValueError("initialized run_id does not match the requested run")

            evidence_ledger = list(
                _plain_json_value(arguments.get("evidence_statements") or [])
            )
            statement_ids: set[str] = set()
            for statement in evidence_ledger:
                if not isinstance(statement, Mapping):
                    raise ValueError("evidence statements must be objects")
                statement_id = str(statement.get("statement_id") or "").strip()
                if not statement_id:
                    raise ValueError("evidence statement_id is required")
                if statement_id in statement_ids:
                    raise ValueError(f"duplicate evidence statement_id: {statement_id}")
                statement_ids.add(statement_id)

            net_changes = list(
                _plain_json_value(arguments.get("net_changes") or [])
            )
            component_interpretations = list(
                _plain_json_value(arguments.get("component_interpretations") or [])
            )
            # A net-change id is useful for downstream references but models may
            # reasonably use a fresh id instead of duplicating the same sentence
            # in evidence_statements.  Complete those ledger rows
            # deterministically rather than forcing a schema-repair round trip.
            for index, item in enumerate(net_changes):
                if not isinstance(item, Mapping):
                    continue
                statement_id = str(item.get("statement_id") or "").strip()
                if statement_id and statement_id not in statement_ids:
                    evidence_ledger.append(
                        {
                            "statement_id": statement_id,
                            "kind": "inference",
                            "statement": str(item.get("event") or "").strip(),
                            "source_refs": [f"stage1.net_changes.{index}.basis"],
                        }
                    )
                    statement_ids.add(statement_id)
            for index, item in enumerate(component_interpretations):
                if not isinstance(item, Mapping):
                    continue
                statement_id = str(item.get("statement_id") or "").strip()
                if statement_id and statement_id not in statement_ids:
                    roles = ", ".join(
                        str(value)
                        for value in item.get("role_candidates") or []
                    )
                    evidence_ledger.append(
                        {
                            "statement_id": statement_id,
                            "kind": "hypothesis",
                            "statement": (
                                f"{item.get('component')}: 候选角色为 {roles}"
                            ),
                            "source_refs": [
                                f"stage1.component_interpretations.{index}"
                            ],
                        }
                    )
                    statement_ids.add(statement_id)

            reaction_document = {
                "schema_version": "1.0",
                "run_id": run_id,
                "evidence_ledger": evidence_ledger,
                "reaction_analysis": {
                    # The model never copies normalized input back through a tool
                    # argument.  The committed fact boundary comes from run_init.
                    "normalized_components": reaction_input,
                    "net_changes": net_changes,
                    "component_interpretations": component_interpretations,
                    "assumptions": _plain_json_value(arguments.get("assumptions") or []),
                    "warnings": _plain_json_value(arguments.get("warnings") or []),
                    "unresolved_questions": _plain_json_value(
                        arguments.get("unresolved_questions") or []
                    ),
                    "validation_scope": {
                        "text_checks_completed": True,
                        "chemical_structure_validated": False,
                        "atom_mapping_completed": False,
                    },
                },
            }
            validation = self._validate_document(
                "reaction_analysis",
                reaction_document,
            )
            if not validation["valid"]:
                raise ValueError(
                    "deterministic stage-1 assembly failed schema validation: "
                    + _json_content(validation["errors"][:20])
                )

            result = artifacts.commit(
                run_id=run_id,
                artifact_name="01_reaction_analysis.json",
                content=json.dumps(
                    reaction_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                media_type="application/json",
                schema_name="reaction_analysis",
                idempotency_key=str(arguments.get("idempotency_key") or ""),
                allow_supersede=bool(arguments.get("allow_supersede", False)),
            )
            artifact = result["artifact"]
            return {
                "committed": bool(result.get("committed")),
                "idempotent": bool(result.get("idempotent")),
                "artifact": artifact,
                "artifact_refs": {artifact["name"]: artifact["path"]},
                "stage_receipt": {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "committed_artifacts": ["01_reaction_analysis.json"],
                    "net_change_count": len(net_changes),
                    "warning_count": len(reaction_document["reaction_analysis"]["warnings"]),
                    "unresolved_question_count": len(
                        reaction_document["reaction_analysis"]["unresolved_questions"]
                    ),
                },
                "schema_valid": True,
            }

        return await self._call("确定性提交反应定义与净变化", operation)

    async def _library_stats(self, _arguments: Mapping[str, Any], _call: ToolCallContext) -> ToolOutput:
        return await self._call("封闭知识库统计", self._library.stats)

    def _revision_context(self, artifacts, run_id):
        """仅传递定向修订任务和旧提名理由；不继承旧评分、审核有效性或目标活性。"""
        review = artifacts._read_json_if_exists(artifacts._run_path(run_id, "06_critique.json")) or {}
        if review.get("review_decision") not in {"REVISE", "PARTIAL_PASS"}:
            return {}
        candidates = artifacts._read_json_if_exists(artifacts._run_path(run_id, "04_candidates.json")) or {}
        generation = candidates.get("candidate_generation") or {}
        tasks, _ = constrain_ranking_revision_tasks(review.get("critique", {}).get("revision_tasks", []))
        affected = {identifier for task in tasks for identifier in task["affected_ids"]}
        object_states = review.get('review_summary', {}).get('objects', [])
        affected.update(row['object_id'] for row in object_states if row['status'] == 'needs_revision')
        feedback = candidates.get("mechanism_feedback", [])
        affected.update(identifier for item in feedback for identifier in item["candidate_ids"])
        relevant = [row for row in candidates.get("enzyme_candidates", []) if
                    not tasks or "04_candidates.json" in affected or "05_routes.json" in affected
                    or row.get("candidate_id") in affected or row.get("proposal_id") in affected]
        return {
            "review_decision": review["review_decision"],
            "object_review_scope": [{key: row[key] for key in ('object_id', 'status', 'assessment', 'ranking', 'affected_by')}
                                    for row in object_states],
            "tasks": tasks,
            "balance_proposal_checks": [{key: value for key, value in row.items() if key != "original_instruction"}
                for row in review.get("critique", {}).get("revision_proposal_checks", [])],
            "evidence_change_policy": deepcopy(REVIEW_EVIDENCE_POLICY),
            "current_evidence": [{key: deepcopy(row[key]) for key in
                ("candidate_id", "proposal_id", "candidate_kind", "identity_level", "candidate_role", "component_function", "evidence_scope", "card_ids", "cofactor_classes", "score", "chemistry_fit_score",
                 "identity_verification", "prior_specific_name", "supports", "contradictions", "ranking_reason", "ranking_factors", "verification_note")
                if key in row} for row in relevant[:30]],
            "required_revisions": [task["instruction"] for task in tasks] if tasks else review.get("critique", {}).get("required_revisions", []),
            "scientific_checks": scientific_review({"04_candidates.json": candidates,
                "05_routes.json": artifacts._read_json_if_exists(artifacts._run_path(run_id, "05_routes.json")) or {}}),
            "current_routes": [{key: deepcopy(row[key]) for key in
                ("route_id", "route_type", "steps", "balance_review", "stoichiometry", "ranking_reason", "unsupported_intermediates") if key in row}
                for row in (artifacts._read_json_if_exists(artifacts._run_path(run_id, "05_routes.json")) or {}).get("routes", [])
                if not tasks or "05_routes.json" in affected or row["route_id"] in affected][:10],
            "mechanism_feedback": feedback,
            "candidate_rationales": [{"proposal_rationale_status": "historical_unverified", **{key: row[key] for key in
                ("candidate_id", "proposal_id", "card_ids", "enzyme_name", "specific_name", "proposal_rationale") if key in row}}
                for row in relevant[:30]],
            "previous_candidates": [{key: row[key] for key in
                ("candidate_id", "proposal_id", "card_ids", "enzyme_name", "specific_name", "mechanism_ids") if key in row}
                for row in candidates.get("enzyme_candidates", [])[:30]],
            "previous_routes": [{"route_id": row["route_id"],
                "steps": [{key: step[key] for key in ("candidate_id", "card_ids", "mechanism_ids") if key in step}
                          for step in row.get("steps", [])]}
                for row in (artifacts._read_json_if_exists(artifacts._run_path(run_id, "05_routes.json")) or {}).get("routes", [])[:10]],
            "original_proposals": generation.get("draft", [])[:8],
            "instruction": "只修改任务涉及对象和受影响下游，沿用身份与路线 ID。current_evidence 的分数是修订前当前值，用于校准比较，不是要求维持的目标值；previous_candidates/previous_routes 仅为历史索引。保留、排除、替换或暂缓由模型决定并说明理由，不因未重新检索删除候选；评分须复核，同知识指纹的封存原文可复用。",
        }

    async def _mechanism_context(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        async def operation() -> dict[str, Any]:
            run_id, corrected_from = self._resolve_workflow_run_id(arguments, call)
            snapshot = self._artifact_store(call).snapshot(
                run_id,
                artifact_names=["01_reaction_analysis.json"],
            )
            self._bind_workflow_run(call, run_id)
            delivery = ContextDelivery(self._artifact_store(call), snapshot, "mechanism", call.session_id)
            retrieval = RetrievalSession(self._artifact_store(call), self._library, "mechanism", snapshot)
            window = retrieval.mechanism_action_window()
            if arguments.get("delivery_cursor") and not window["commit_required"]:
                if set(arguments) - {"run_id", "delivery_cursor"}:
                    raise ValueError("delivery_cursor cannot be combined with retrieval arguments")
                page = delivery.page(arguments["delivery_cursor"])
                page["submission_window"] = window
                return page
            retrieval_arguments = dict(arguments)
            retrieval_arguments["run_id"] = run_id
            if window["commit_required"]:
                response = {"committed": False, "notices": ["正式提交窗口：本次未执行取证，请提交已有草稿与未知。"]}
            else:
                deferred_search = False
                if window["phase"] == "closing":
                    deferred_search = any(retrieval_arguments.get(key) for key in ("queries", "cursor", "unresolved_gaps"))
                    retrieval_arguments = {key: value for key, value in retrieval_arguments.items()
                                           if key in {"run_id", "read_refs", "resume", "reason"}}
                response = (retrieval.packet() if deferred_search and not any(
                    retrieval_arguments.get(key) for key in ("read_refs", "resume")) else
                    await retrieval.context(retrieval_arguments, self._retriever))
                if deferred_search:
                    response.setdefault("notices", []).append("收尾窗口未执行广泛检索参数；只保留必要原文回读，未完成方向保持未知。")
            context = build_mechanism_context(snapshot, self._library, retrieval.packet())
            context = retrieval.delivery(context, response)
            context["revision_context"] = self._revision_context(self._artifact_store(call), run_id)
            draft = self._artifact_store(call)._read_json_if_exists(
                self._artifact_store(call)._run_path(run_id, "mechanism_hypotheses.draft.json")) or {}
            current_draft = bool(draft) and mechanism_draft_is_current(draft, snapshot["documents"]["01_reaction_analysis.json"])
            slots = draft.get("slots", {}) if current_draft else {}
            context["submission_window"] = window
            context["saved_mechanisms"] = [{**row, "summary": row["summary"][:240], "preview": True} for row in
                saved_mechanism_rows({**snapshot["documents"], "mechanism_hypotheses.draft.json": draft})]
            context["partial_report"] = {"path": self._artifact_store(call)._run_path(run_id, "partial_report.md"),
                                         "status": "unverified_draft"}
            context["mechanism_slots"] = {
                "draft_status": "current" if current_draft else ("stale_reaction" if draft else "empty"),
                "limit": context["result_limits"]["mechanisms"],
                "occupied": [{"slot": int(key), "summary": str(value.get("summary", ""))[:240]}
                             for key, value in slots.items()],
                "instruction": ("反应分析已更新，旧槽位不再适用。按当前反应重新暂存，新草稿通过校验后旧草稿自动归档；也可直接提交非空 hypotheses。"
                                if draft and not current_draft else
                                "修订复用已有 slot；名额满时先比较，明确替换一个已有 slot 或将未展开方向记为缺口，不追加超限 slot。")}
            context = delivery.publish(context)
            self._check_retrieval_budget(context)
            retrieval.save()
            return self._attach_run_id_correction(context, corrected_from, run_id)

        return await self._call("读取反应与酶 Wiki 证据", operation)

    async def _mechanism_plan_commit(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        async def operation() -> dict[str, Any]:
            run_id, _corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(
                run_id,
                artifact_names=["01_reaction_analysis.json"],
            )
            self._bind_workflow_run(call, run_id)
            retrieval = RetrievalSession(artifacts, self._library, "mechanism", snapshot)
            window = retrieval.mechanism_action_window()
            packet = retrieval.packet()
            context = build_mechanism_context(snapshot, self._library, packet)
            if ContextDelivery(artifacts, snapshot, "mechanism", call.session_id).submitted_digest(arguments, context["context_digest"]) != context["context_digest"]:
                raise ValueError("mechanism context is stale or does not belong to this run; "
                                 "compare with the most recent context receipt, not an earlier read: "
                                 f"latest context_digest={context['context_digest']}")
            continuation = await retrieval.continue_gaps(arguments.get("knowledge_gaps", []), self._retriever,
                questions=arguments.get("retrieval_questions"), finish=bool(arguments.get("finish_exploration")) or window["phase"] != "explore")
            if continuation:
                updated = build_mechanism_context(snapshot, self._library, retrieval.packet())
                return ContextDelivery(artifacts, snapshot, "mechanism", call.session_id).publish({
                    **continuation, "context_digest": updated["context_digest"],
                    "citation_index": updated["citation_index"], "submission_window": window})
            hypotheses = _plain_json_value(arguments.get("hypotheses") or [])
            draft_path = artifacts._run_path(run_id, "mechanism_hypotheses.draft.json")
            draft = artifacts._read_json_if_exists(draft_path)
            if draft and draft.get("run_id") != run_id:
                raise ValueError("mechanism draft belongs to another run")
            stale_draft = bool(draft) and not mechanism_draft_is_current(
                draft, snapshot["documents"]["01_reaction_analysis.json"])
            if not hypotheses and draft:
                if stale_draft:
                    return {"committed": False, "status": "mechanism_draft_refresh_required",
                            "context_digest": context["context_digest"], "citation_index": context["citation_index"],
                            "submission_window": window,
                            "instruction": "反应分析已更新，未提交旧机制，也未改写原草稿。请按当前反应提交非空 hypotheses（收尾窗口可直接提交），或重新逐条暂存后提交。"}
                hypotheses = [
                    item
                    for _, item in sorted(
                        dict(draft.get("slots") or {}).items(),
                        key=lambda pair: int(pair[0]),
                    )
                ]
            mechanism_document, capability_document = build_mechanism_documents(
                snapshot, self._library,
                hypotheses,
                _plain_json_value(arguments.get("cofactors") or []),
                _plain_json_value(arguments.get("knowledge_gaps") or []),
                evidence_packet=packet,
            )
            validations = {
                "mechanism_hypothesis": self._validate_document(
                    "mechanism_hypothesis", mechanism_document
                ),
                "capability_mapping": self._validate_document(
                    "capability_mapping", capability_document
                ),
            }
            invalid = {
                name: result["errors"][:20]
                for name, result in validations.items()
                if not result["valid"]
            }
            if invalid:
                raise ValueError(
                    "deterministic stage-2 assembly failed schema validation: "
                    + _json_content(invalid)
                )

            base_key = str(arguments.get("idempotency_key") or f"{run_id}:stage2")
            allow_supersede = bool(arguments.get("allow_supersede", False))
            archive_path = _archive_mechanism_draft(artifacts, run_id, draft) if stale_draft else None
            mechanism_result = artifacts.commit(
                run_id=run_id,
                artifact_name="02_mechanisms.json",
                content=json.dumps(
                    mechanism_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                media_type="application/json",
                schema_name="mechanism_hypothesis",
                idempotency_key=f"{base_key}:02" if base_key else "",
                allow_supersede=allow_supersede,
            )
            capability_result = artifacts.commit(
                run_id=run_id,
                artifact_name="03_capabilities.json",
                content=json.dumps(
                    capability_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                media_type="application/json",
                schema_name="capability_mapping",
                idempotency_key=f"{base_key}:03" if base_key else "",
                allow_supersede=allow_supersede,
            )
            committed_artifacts = [
                mechanism_result["artifact"],
                capability_result["artifact"],
            ]
            if stale_draft:
                # 收尾窗口也允许直接提交完整 hypotheses；同步活动草稿，避免交付又读到旧槽位。
                artifacts._atomic_write(draft_path, _json_content({
                    "schema_version": "1.0", "run_id": run_id,
                    "reaction_digest": hashlib.sha256(_json_content(snapshot["documents"]["01_reaction_analysis.json"]).encode()).hexdigest(),
                    "context_digest": context["context_digest"],
                    "slots": {str(index): row for index, row in enumerate(hypotheses, 1)},
                }) + "\n")
            handoff = retrieval.seal_handoff(
                {
                    "02_mechanisms.json": mechanism_document,
                    "03_capabilities.json": capability_document,
                },
                packet,
            )
            retrieval.state["mechanism_actions"] = 0  # 只有正式提交成功才为下一轮科学修订重置操作窗口。
            retrieval.save()
            return {
                "committed": any(
                    bool(item.get("committed"))
                    for item in (mechanism_result, capability_result)
                ),
                "idempotent": all(
                    bool(item.get("idempotent"))
                    for item in (mechanism_result, capability_result)
                ),
                "artifacts": committed_artifacts,
                "artifact_refs": {
                    item["name"]: item["path"] for item in committed_artifacts
                },
                "stage_receipt": {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "committed_artifacts": [
                        "02_mechanisms.json",
                        "03_capabilities.json",
                    ],
                    "mechanism_count": len(mechanism_document["mechanism_hypotheses"]),
                    "top_cofactor_classes": [
                        item["cofactor_class"]
                        for item in capability_document["cofactor_candidates"]
                    ][:3],
                    "evidence_handoff": {
                        "schema_version": handoff["schema_version"],
                        "ref_count": len(handoff["refs"]),
                        "handoff_digest": handoff["handoff_digest"],
                    },
                },
                "schema_valid": True,
                **({"replaced_stale_draft": True, "previous_draft_archive": archive_path} if archive_path else {}),
            }

        return await self._call("校验并保存模型提出的机制与能力", operation)

    async def _mechanism_hypothesis_stage(
        self,
        arguments: Mapping[str, Any],
        call: ToolCallContext,
    ) -> ToolOutput:
        def operation() -> dict[str, Any]:
            run_id, _corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(run_id, artifact_names=["01_reaction_analysis.json"])
            self._bind_workflow_run(call, run_id)
            retrieval = RetrievalSession(artifacts, self._library, "mechanism", snapshot)
            window = retrieval.mechanism_action_window()
            packet = retrieval.packet()
            context = build_mechanism_context(snapshot, self._library, packet)
            if ContextDelivery(artifacts, snapshot, "mechanism", call.session_id).submitted_digest(arguments, context["context_digest"]) != context["context_digest"]:
                raise ValueError(
                    "mechanism context is stale or does not belong to this run; "
                    f"latest context_digest={context['context_digest']}"
                )
            slot = int(arguments["slot"])
            if slot > int(context["result_limits"]["mechanisms"]):
                draft = artifacts._read_json_if_exists(
                    artifacts._run_path(run_id, "mechanism_hypotheses.draft.json")) or {}
                return {
                    "drafted": False, "status": "mechanism_capacity_reached",
                    "limit": int(context["result_limits"]["mechanisms"]),
                    "occupied_slots": sorted(int(key) for key in draft.get("slots", {})),
                    "instruction": "本条未写入，也未自动排除任何机制。比较已存机制后，明确选择合法 slot 替换，或将该方向记为未展开缺口并提交。不得重复提交超限 slot。"}
            path = artifacts._run_path(run_id, "mechanism_hypotheses.draft.json")
            draft = artifacts._read_json_if_exists(path) or {
                "schema_version": "1.0",
                "run_id": run_id,
                "slots": {},
            }
            if draft.get("run_id") != run_id:
                raise ValueError("mechanism draft belongs to another run")
            reaction_digest = hashlib.sha256(
                _json_content(snapshot["documents"]["01_reaction_analysis.json"]).encode()
            ).hexdigest()
            stale_draft = bool(draft.get("slots")) and not mechanism_draft_is_current(
                draft, snapshot["documents"]["01_reaction_analysis.json"])
            if window["commit_required"] and not stale_draft:
                return {"drafted": False, "committed": False, "submission_window": window,
                        "context_digest": context["context_digest"], "citation_index": context["citation_index"],
                        "instruction": "正式提交窗口：本次未改动草稿。请用 hypotheses=[] 提交已有有效槽位，并明确未完成方向。"}
            previous_draft = draft if stale_draft else None
            if stale_draft:
                draft = {"schema_version": "1.0", "run_id": run_id, "slots": {}}
            slots = dict(draft.get("slots") or {})
            slots[str(slot)] = normalize_mechanism_references(_plain_json_value(arguments["hypothesis"]), packet)
            ordered = [item for _, item in sorted(slots.items(), key=lambda pair: int(pair[0]))]
            # Reuse the final deterministic builder so each small submission is
            # rejected immediately if its refs or structure cannot be committed.
            build_mechanism_documents(
                snapshot,
                self._library,
                ordered,
                [],
                ["机制仍在逐条组装，尚未最终提交。"],
                evidence_packet=packet,
            )
            archive_path = None
            if previous_draft:
                # 先校验新机制、再留存旧版本，最后原子替换；失败时原草稿仍可恢复。
                archive_path = _archive_mechanism_draft(artifacts, run_id, previous_draft)
            artifacts._atomic_write(
                path,
                json.dumps(
                    {
                        **draft,
                        "context_digest": context["context_digest"],
                        "reaction_digest": reaction_digest,
                        "slots": slots,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            result = {
                "drafted": True,
                "committed": False,
                "run_id": run_id,
                "slot": slot,
                "staged_count": len(slots),
                **({"replaced_stale_draft": True, "previous_draft_archive": archive_path} if archive_path else {}),
                "submission_window": window,
                "remaining_slots": max(0, int(context["result_limits"]["mechanisms"]) - len(slots)),
                "instruction": ("名额已满；新增方向须明确替换已有 slot，或记为缺口。"
                                if len(slots) >= int(context["result_limits"]["mechanisms"]) else "按剩余名额继续逐条暂存。")
                               + "完成后用 hypotheses=[] 调用 enzyme_mechanism_plan_commit。",
            }
            checkpoint = retrieval.queue_gap_check(arguments["hypothesis"].get("uncertainties", []))
            if checkpoint:
                result["gap_check"] = checkpoint
                result["instruction"] += " " + checkpoint["instruction"]
            rows = saved_mechanism_rows({**snapshot["documents"], "mechanism_hypotheses.draft.json": {
                **draft, "reaction_digest": reaction_digest, "slots": slots}})
            partial = ("# 酶通路探索：部分结果\n\n机制草稿已保存，第二阶段尚未正式提交；不作为审核通过的结论。\n\n"
                       + render_saved_mechanisms(rows))
            result["partial_report"] = {"path": artifacts._run_path(run_id, "partial_report.md"),
                                        "status": "unverified_draft", "mechanism_count": len(rows)}
            try:
                artifacts._atomic_write(artifacts._run_path(run_id, "partial_report.md"), partial)
                result["artifact_refs"] = {"partial_report.md": artifacts._run_path(run_id, "partial_report.md")}
            except OSError as exc:
                # 科学草稿已成功保存；展示文件故障不能伪装成草稿保存失败并诱发重交。
                result["partial_report"].update(status="write_failed", error_type=type(exc).__name__)
            return result

        return await self._call("逐条校验并暂存机制假说", operation)

    async def _screen_context(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        async def operation() -> dict[str, Any]:
            run_id, corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(
                run_id,
                artifact_names=[
                    "01_reaction_analysis.json",
                    "02_mechanisms.json",
                    "03_capabilities.json",
                ],
            )
            self._bind_workflow_run(call, run_id)
            delivery = ContextDelivery(artifacts, snapshot, "screen", call.session_id)
            if arguments.get("delivery_cursor"):
                if set(arguments) - {"run_id", "delivery_cursor"}:
                    raise ValueError("delivery_cursor cannot be combined with retrieval arguments")
                return delivery.page(arguments["delivery_cursor"])
            retrieval = RetrievalSession(self._artifact_store(call), self._library, "screen", snapshot)
            draft_argument = arguments.get("candidate_draft")
            lookup_requested = any(arguments.get(key) for key in ("queries", "read_refs", "cursor", "resume"))
            if not retrieval.state.get("candidate_baseline_sent") or (
                    not retrieval.candidate_draft() and draft_argument is None):
                # 乱序请求只交付化学基线，不执行检索或泄露 Wiki；让模型先补草案，
                # 无需把可恢复的协议顺序错误当成工具失败并消耗重试额度。
                baseline = retrieval.candidate_baseline()
                context = build_candidate_baseline_context(snapshot)
                context.update({
                    "prior_candidate_identities": retrieval.candidate_identities(),
                    "context_delivery": "candidate_baseline",
                    "candidate_draft_status": baseline,
                    "retrieval": {"status": "not_started_before_candidate_draft", "results": []},
                    "notices": [baseline["instruction"],
                                "尚未冻结独立候选草案；本次检索参数未执行。下一次只提交 candidate_draft，勿重复查询。"],
                    "commit_receipt": {
                        "context_digest": context["context_digest"],
                        "committable": False,
                        "instruction": "此指纹仅对应候选生成基线；提交 candidate_draft 并完成必要核验后使用新指纹。",
                    },
                })
                context = delivery.publish(context)
                self._check_retrieval_budget(context)
                return self._attach_run_id_correction(context, corrected_from, run_id)
            if not retrieval.candidate_draft():
                if draft_argument is None:
                    raise ValueError("candidate_draft is required before Wiki retrieval or screening commit")
                retrieval.bind_candidate_draft(_plain_json_value(draft_argument))
            # The first accepted draft is immutable.  Models may echo or
            # paraphrase it on later read requests; ignore that redundant
            # argument rather than failing the evidence read and retrying the
            # entire workflow stage.  The persisted draft remains authoritative.
            ignored_late_draft = draft_argument is not None and bool(retrieval.candidate_draft())
            retrieval_arguments = {key: value for key, value in arguments.items() if key != "candidate_draft"}
            retrieval_arguments["run_id"] = run_id
            response = await retrieval.context(retrieval_arguments, self._retriever)
            packet = retrieval.packet()
            packet["candidate_draft"] = retrieval.candidate_draft()
            context = build_screening_context(snapshot, self._library, packet)
            context = retrieval.delivery(context, response)
            context["submission_policy"] = dict(SCREENING_SUBMISSION_POLICY)
            # 续跑只补缺失项；以当前上游指纹绑定草稿，不注入旧版本候选。
            context["revision_context"] = self._revision_context(artifacts, run_id)
            staged = artifacts._read_json_if_exists(artifacts._run_path(run_id, "screening_items.draft.json"))
            upstream_digest = hashlib.sha256(_json_content(snapshot["documents"]).encode()).hexdigest()
            if staged and staged.get("upstream_digest") == upstream_digest:
                context["protein_discovery_tasks"] = discovery_tasks(
                    snapshot["documents"]["02_mechanisms.json"]["mechanism_hypotheses"],
                    list(staged.get("candidates", {}).values()), list(staged.get("routes", {}).values()))
                context["route_assembly_tasks"] = mechanism_route_coverage(
                    snapshot["documents"]["02_mechanisms.json"]["mechanism_hypotheses"],
                    list(staged.get("routes", {}).values()), list(staged.get("candidates", {}).values()))
                context["staged_progress"] = {
                    "mechanism_feedback": staged.get("mechanism_feedback", []),
                    "read_card_reviews": staged.get("read_card_reviews", []),
                    "unassessed_read_cards": [{"card_id": card["card_id"], "enzyme_name": card.get("display_name", "")}
                        for card in context.get("cards", []) if card["card_id"] not in
                        {row["card_id"] for row in staged.get("read_card_reviews", [])}
                        and card["card_id"] not in staged.get("candidates", {})],
                    "mechanism_link_review": [key for key, row in staged.get("candidates", {}).items()
                        if row.get("candidate_role") == "primary" and not row.get("mechanism_ids")
                        and not any(key in item["candidate_ids"] for item in staged.get("mechanism_feedback", []))],
                    "candidates": [{"id": key, "kind": row.get("candidate_kind", "wiki_enzyme"),
                                    "name": row.get("enzyme_name") or row.get("required_capability") or row.get("card_id")}
                                   for key, row in staged.get("candidates", {}).items()],
                    "route_ids": list(staged.get("routes", {})),
                    "pending_repairs": list(staged.get("item_feedback", {}).values())[:8],
                    "submission_policy": dict(SCREENING_SUBMISSION_POLICY),
                    "route_parts": {key: {"header": part["header"], "saved_steps": sorted(part["steps"]),
                                         "missing_steps": [i for i in range(1, part["header"]["step_count"] + 1)
                                                           if str(i) not in part["steps"]]}
                                    for key, part in staged.get("route_parts", {}).items()},
                    "instruction": "以上是当前版本已暂存内容，沿用这些 ID，仅补充缺项或明确修订。无需重建候选；use_staged=true 会提交已保存内容，但仍需通过科学校验。"}
                # 修订草稿才是待提交输入；旧正式产物只代表上次审核对象，不能冒充当前状态。
                for key in ('current_evidence', 'current_routes', 'scientific_checks'):
                    context['revision_context'].pop(key, None)
                if arguments.get('resume') or (context['revision_context'] and not lookup_requested):
                    context['staged_progress']['saved_submission'] = {
                        key: deepcopy(staged.get(key, [] if key in ('read_card_reviews', 'mechanism_feedback') else {}))
                        for key in ('candidates', 'routes', 'read_card_reviews', 'mechanism_feedback')}
                    context['staged_progress']['instruction'] += (
                        ' saved_submission 是已通过暂存格式检查的当前输入，不是审核通过结论。'
                        '先 use_staged=true 提交以获得当前校验待办；不要从旧04/05或历史回执重建对象，'
                        '不把派生输出字段复制回暂存输入。只按新回执修必要项。')
            if ignored_late_draft:
                context.setdefault("notices", []).append(
                    "candidate_draft 已冻结；本次重复提交已忽略，原始草案保持不变。"
                )
            context["read_card_comparison_tasks"] = read_card_comparison_tasks(context,
                list((staged or {}).get("candidates", {}).values()), (staged or {}).get("read_card_reviews", []))
            context['completion_tasks'] = screening_completion_tasks(context,
                list((staged or {}).get('candidates', {}).values()), list((staged or {}).get('routes', {}).values()),
                (staged or {}).get('read_card_reviews', []))
            if context.get('revision_context', {}).get('review_decision') == 'PARTIAL_PASS':
                full_snapshot = artifacts.internal_snapshot(run_id, artifact_names=[
                    '01_reaction_analysis.json','02_mechanisms.json','03_capabilities.json','04_candidates.json','05_routes.json'])
                context = revision_projection(artifacts, full_snapshot, context)
            context = delivery.publish(context)
            self._check_retrieval_budget(context)
            retrieval.save()
            return self._attach_run_id_correction(context, corrected_from, run_id)

        return await self._call("生成紧凑候选筛选包", operation)

    async def _screen_items_stage(self, raw_arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        if "candidates" in raw_arguments or (
                isinstance(raw_arguments.get("routes"), list) and len(raw_arguments["routes"]) > 1):
            async def batch():
                problems = validation_issues(self._screen_stage_input_schema, raw_arguments)
                if problems:
                    raise ValueError("invalid staging batch: " + "; ".join(row["message"] for row in problems))
                envelope = {key: raw_arguments[key] for key in
                            ("run_id", "context_digest", "mechanism_feedback", "read_card_reviews") if key in raw_arguments}
                entries = [("candidate", row) for row in raw_arguments.get("candidates", [])]
                entries += [("route", row) for row in raw_arguments.get("routes", [])]
                if not entries:
                    raise ValueError("staging batch must contain at least one item")
                results, latest = [], {}
                for kind, row in entries:
                    item = {"candidate": row} if kind == "candidate" else {"routes": [row]}
                    output = await self._screen_items_stage({**envelope, **item}, call)
                    body = json.loads(output.content)
                    if not body.get("ok"):
                        # 上下文/运行错误不对其余项重复调用；已成功项仍留在磁盘。
                        results.append({"kind": kind, "accepted": False, "error": body.get("error")})
                        break
                    result = body["result"]
                    latest = result
                    results.append({"kind": kind,
                        "id": row.get("proposal_id") or row.get("card_id") or row.get("route_id"),
                        "accepted": result.get("accepted", False), "drafted": result.get("drafted", False),
                        **({"repair_request": result["repair_request"]} if result.get("repair_request") else {})})
                return {**latest, "accepted": all(row["accepted"] for row in results),
                    "drafted": any(row.get("drafted") for row in results), "batch_results": results,
                    "unprocessed_items": len(entries) - len(results),
                    "instruction": "本批成功项已分别保存，不重发；仅按batch_results修失败项。"
                        + latest.get("instruction", "")}
            return await self._call("小批校验并暂存候选与路线", batch)
        feedback = {}
        issues = []
        def operation() -> dict[str, Any]:
            submitted = dict(raw_arguments)
            arguments = dict(submitted)
            run_id, _ = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(run_id, artifact_names=[
                "01_reaction_analysis.json", "02_mechanisms.json", "03_capabilities.json"])
            retrieval = RetrievalSession(artifacts, self._library, "screen", snapshot)
            if not retrieval.candidate_draft():
                raise ValueError("freeze candidate_draft before staging screening items")
            packet = retrieval.packet()
            packet["candidate_draft"] = retrieval.candidate_draft()
            context = build_screening_context(snapshot, self._library, packet)
            if ContextDelivery(artifacts, snapshot, "screen", call.session_id).submitted_digest(arguments, context["context_digest"]) != context["context_digest"]:
                raise ValueError(f"screening context is stale; latest context_digest={context['context_digest']}")
            for key in ("candidate", "route_header", "route_step"):
                if key in submitted:
                    submitted[key] = strip_matching_transport_fields(submitted[key], run_id, context["context_digest"])
            for key in ("candidates", "routes"):
                if isinstance(submitted.get(key), list):
                    submitted[key] = [strip_matching_transport_fields(row, run_id, context["context_digest"]) for row in submitted[key]]
            arguments = dict(submitted)
            upstream_digest = hashlib.sha256(_json_content(snapshot["documents"]).encode()).hexdigest()
            path = artifacts._run_path(run_id, "screening_items.draft.json")
            staged = artifacts._read_json_if_exists(path) or {
                "run_id": run_id, "upstream_digest": upstream_digest, "candidates": {}, "routes": {}}
            if staged["upstream_digest"] != upstream_digest:
                # 自动科学修订从新上游开始；归档旧草稿，明确报告，不混用旧候选。
                artifacts._atomic_write(path + ".previous", _json_content(staged))
                staged = {"run_id": run_id, "upstream_digest": upstream_digest, "candidates": {}, "routes": {}}
            feedback.update(artifacts=artifacts, path=path, run_id=run_id, upstream_digest=upstream_digest)
            # 正式产物中的自动能力占位可能未显式暂存，修订时仍须识别其原始稳定 ID。
            published = artifacts._read_json_if_exists(artifacts._run_path(run_id, '04_candidates.json')) or {}
            published_capabilities = [row for row in published.get('enzyme_candidates', [])
                if row.get('candidate_kind') == 'unmatched_capability' and row.get('required_capability')
                and row.get('proposal_id') not in staged['candidates']]
            parts = staged.setdefault("route_parts", {})
            modes = [key for key in ("candidate", "route_header", "route_step") if key in submitted]
            if len(modes) > 1 or (modes and any(key in submitted for key in ("candidates", "routes"))):
                raise ValueError("submit exactly one candidate, route_header or route_step; do not mix protocols")
            if modes == ["candidate"]:
                arguments = {"candidates": [submitted["candidate"]]}
            elif modes == ["route_header"]:
                header = dict(submitted["route_header"])
                explicit_ledger = "stoichiometry" in header
                normalize_route_submission(header)
                issues.extend(validation_issues(self._screen_route_header_schema, header, "$.route_header"))
                if issues:
                    raise ValueError("route header requires correction")
                route_id = header["route_id"]
                if header["step_count"] > int(context.get("max_route_steps", 8)):
                    raise ValueError("step_count exceeds this run's existing max_route_steps")
                if route_id in staged["routes"] and route_id not in parts:
                    # 修订既有完整路线时允许切换分项提交；继承原步骤，不重发或丢弃。
                    previous = deepcopy(staged["routes"][route_id])
                    saved_steps = previous.pop("steps")
                    if len(saved_steps) > header["step_count"]:
                        raise ValueError("updated step_count would drop saved steps; no step was removed")
                    header = {**previous, **header}
                    parts[route_id] = {"header": header,
                        "steps": {str(index): step for index, step in enumerate(saved_steps, 1)}}
                if route_id in parts and any(int(index) > header["step_count"] for index in parts[route_id]["steps"]):
                    raise ValueError("updated step_count would drop saved steps; no step was removed")
                part = parts.setdefault(route_id, {"header": header, "steps": {}})
                part["ledger_inherited"] = not explicit_ledger and "stoichiometry" in header
                part["header"] = header
                # 暂存使用既有内部容量；最终 Top N 只约束交付，避免提前拒绝竞争路线。
                if len(set(parts) | set(staged["routes"])) > 10:
                    raise ValueError("route headers exceed existing route limit; no route was dropped")
                arguments = {}
                if all(str(position) in part["steps"] for position in range(1, header["step_count"] + 1)):
                    route = {key: value for key, value in header.items() if key != "step_count"}
                    route["steps"] = [part["steps"][str(position)] for position in range(1, header["step_count"] + 1)]
                    arguments = {"routes": [route]}
            elif modes == ["route_step"]:
                route_id = submitted.get("route_id")
                index = submitted.get("step_index")
                if route_id not in parts:
                    raise ValueError("save route_header before its steps")
                part = parts[route_id]
                step = dict(submitted["route_step"])
                normalize_route_step_binding(step, [*staged['candidates'].values(), *published_capabilities], require_selected=True,
                                             staged_routes=staged['routes'].values())
                step.setdefault("evidence_refs", [])
                index = resolve_route_step_index(step, index, part)
                issues.extend(validation_issues(self._screen_staging_route_schema["properties"]["steps"]["items"], step, "$.route_step"))
                if issues:
                    raise ValueError("route step requires correction")
                binding = step.get("card_id") or step.get("proposal_id")
                if binding and binding not in staged["candidates"]:
                    raise ValueError("route step must use an already-staged candidate ID; copy it from candidate_ids receipt")
                # 与旧暂存协议一致：省略引用只恢复空数组，正式提交仍核对来源。
                step.setdefault("evidence_refs", [])
                part["steps"][str(index)] = step
                count = part["header"]["step_count"]
                if all(str(position) in part["steps"] for position in range(1, count + 1)):
                    route = {key: value for key, value in part["header"].items() if key != "step_count"}
                    route["steps"] = [part["steps"][str(position)] for position in range(1, count + 1)]
                    if part.get("ledger_inherited") and all(row.get("equation") is not None for row in route["steps"]):
                        route.pop("stoichiometry", None)
                    arguments = {"routes": [route]}
                else:
                    arguments = {}
            if not modes and not arguments.get("candidates") and not arguments.get("routes") and not submitted.get('read_card_reviews'):
                raise ValueError("staging requires at least one candidate or route")
            for field, schema, maximum, batch_limit in (
                ("candidates", self._screen_candidate_schema, 30, 3),
                ("routes", self._screen_staging_route_schema, 10, 2),
            ):
                items = arguments.get(field) or []
                if not isinstance(items, list) or len(items) > batch_limit:
                    raise ValueError(f"{field}: at most {batch_limit} items per staging call")
                for raw in items:
                    if field == "candidates":
                        raw = dict(raw)
                        if not raw.get("candidate_kind"):
                            if raw.get("card_id") and not raw.get("proposal_id"):
                                raw["candidate_kind"] = "wiki_enzyme"
                            elif raw.get("proposal_id") and not raw.get("card_id"):
                                raw["candidate_kind"] = "unmatched_capability" if raw.get("required_capability") else "model_enzyme"
                        # unknown 是未作判断，不是替模型补充科学结论。
                        repair_notes = normalize_candidate_submission(raw)
                        if repair_notes:
                            staged.setdefault("normalization_notes", {})[str(raw.get("proposal_id") or raw.get("card_id"))] = repair_notes
                        baseline = candidate_identity_baseline(context.get("candidate_generation") or {"draft": retrieval.candidate_draft()})
                        original = next((r for r in baseline if r["proposal_id"] == raw.get("proposal_id")), None)
                        if original and raw.get("candidate_kind") == "model_enzyme" and "enzyme_name" not in raw:
                            raw["enzyme_name"] = original["enzyme_name"]
                        source_context = {**context, "candidate_generation": {"draft": baseline}}
                        repair_notes.extend(normalize_candidate_source(raw, source_context))
                        issues.extend(validation_issues(schema, raw, "$.candidate"))
                        if issues:
                            raise ValueError("candidate requires correction")
                        require_candidate_lineage(raw, candidate_identity_baseline(context.get("candidate_generation") or {"draft": retrieval.candidate_draft()}))
                        identifier = raw.get("card_id") or raw.get("proposal_id")
                        previous = staged["candidates"].get(identifier) or context.get("identity_bindings", {}).get(identifier)
                        raw = preserve_candidate_identity(raw, previous,
                            {card["card_id"]: card for card in context.get("cards", [])},
                            {claim["ref"]: claim for card in context.get("cards", []) for claim in card["claims"]})
                        name_gap = validate_candidate_name(raw, baseline)
                        if name_gap:
                            repair_notes.append(name_gap)
                        if previous and previous.get("candidate_kind", "wiki_enzyme") != raw.get("candidate_kind", "wiki_enzyme"):
                            raise ValueError("candidate ID cannot change between enzyme and capability kinds; use a distinct ID")
                        if previous and previous.get("enzyme_name") != raw.get("enzyme_name"):
                            raise ValueError("candidate ID cannot be overwritten with a different enzyme_name")
                        # 未匹配能力没有酶名；两个缺失名称不构成同一酶的身份依据。
                        if raw.get("proposal_id") and raw.get("enzyme_name") and any(
                                key != identifier and row.get("enzyme_name") == raw.get("enzyme_name")
                                for key, row in staged["candidates"].items() if row.get("proposal_id")):
                            raise ValueError("same proposed enzyme_name already has a staged ID; reuse that ID")
                    if field == "routes" and isinstance(raw, Mapping):
                        raw = dict(raw)
                        normalize_route_submission(raw)
                        previous_route = staged["routes"].get(raw.get("route_id"), {})
                        inherit_route_ledger(previous_route, raw)
                        if isinstance(raw.get("steps"), list):
                            raw["steps"] = [
                                {**step, "evidence_refs": []}
                                if isinstance(step, Mapping) and "evidence_refs" not in step else step
                                for step in raw["steps"]
                            ]
                            for step in raw['steps']:
                                normalize_route_step_binding(step, [*staged['candidates'].values(), *published_capabilities], require_selected=True,
                                                             staged_routes=staged['routes'].values())
                    issues.extend(validation_issues(schema, raw, '$.' + field))
                    if issues:
                        raise ValueError("item requires correction")
                    if "ranking_factors" in raw:
                        ranking_fields(raw, claim_index(context))
                    identity = raw.get("card_id") or raw.get("proposal_id") if field == "candidates" else raw.get("route_id")
                    if not identity:
                        raise ValueError(f"{field} requires a stable card_id/proposal_id/route_id")
                    staged[field][identity] = dict(raw)
                    staged.get("item_feedback", {}).pop(str(identity), None)
                    if field == "routes":
                        for key in list(staged.get("item_feedback", {})):
                            if key.startswith(str(identity) + ':step:'):
                                staged["item_feedback"].pop(key)
                if len(staged[field]) > maximum:
                    raise ValueError(f"staged {field} exceeds existing limit={maximum}; no items were dropped")
            if submitted.get("mechanism_feedback"):
                mechanism_feedback_items = list(staged.get("mechanism_feedback", []))
                for item in submitted["mechanism_feedback"]:
                    if item not in mechanism_feedback_items:
                        mechanism_feedback_items.append(_plain_json_value(item))
                if len(mechanism_feedback_items) > 3:
                    raise ValueError("mechanism_feedback exceeds existing limit=3")
                staged["mechanism_feedback"] = mechanism_feedback_items
            if submitted.get("read_card_reviews"):
                read_ids = {card["card_id"] for card in context.get("cards", [])}
                reviews = {row["card_id"]: row for row in staged.get("read_card_reviews", [])}
                for row in submitted["read_card_reviews"]:
                    if row["card_id"] not in read_ids:
                        raise ValueError("read_card_reviews must refer to actually read cards")
                    reviews[row["card_id"]] = _plain_json_value(row)
                staged["read_card_reviews"] = list(reviews.values())
            staged["context_digest"] = context["context_digest"]
            retrieval.remember_candidate_identities(list(staged["candidates"].values()))
            artifacts._atomic_write(path, _json_content(staged))
            retrieval.save()
            result = {"accepted": True, "drafted": True, "run_id": run_id,
                    "submission_policy": dict(SCREENING_SUBMISSION_POLICY),
                    "normalization_notes": repair_notes if modes == ["candidate"] else [],
                    "candidate_ids": list(staged["candidates"]), "route_ids": list(staged["routes"]),
                    "staged_mechanism_feedback": staged.get("mechanism_feedback", []),
                    "read_card_review_receipts": [{"card_id": row['card_id'], 'decision': row['decision'],
                        'compared': bool(row.get('comparison'))} for row in staged.get('read_card_reviews', [])],
                    "unassessed_read_cards": [{"card_id": card["card_id"], "enzyme_name": card.get("display_name", "")}
                        for card in context.get("cards", []) if card["card_id"] not in
                        {r["card_id"] for r in staged.get("read_card_reviews", [])}
                        and card["card_id"] not in staged["candidates"]],
                    "mechanism_link_review": [key for key, row in staged["candidates"].items()
                        if row.get("candidate_role") == "primary" and not row.get("mechanism_ids")
                        and not any(key in f["candidate_ids"] for f in staged.get("mechanism_feedback", []))],
                    "incomplete_routes": {route_id: [position for position in range(1, part["header"]["step_count"] + 1)
                                                    if str(position) not in part["steps"]]
                                          for route_id, part in parts.items()
                                          if len(part["steps"]) != part["header"]["step_count"]},
                    "instruction": "复制回执中的候选 ID 绑定路线，不改名或重编 ID。完成全部声明步骤后以 use_staged=true、candidates=[]、routes=[] 提交；暂存不等于科学校验通过。"}
            gaps = [row.get("knowledge_gap", "") for row in arguments.get("candidates", [])]
            result["scientific_checks"] = {
                "ranking_consistency": ranking_consistency(list(staged["candidates"].values()), list(staged["routes"].values())),
                "stoichiometry": [{"route_id": row["route_id"], **account["check"],
                                   "delivery_source": account["source"], "missing_equation_steps": account["missing_equation_steps"]}
                    for row in arguments.get("routes", []) for account in [route_accounting(row)]],
                "ranking_basis": [{"id": row.get("route_id") or row.get("proposal_id") or row.get("card_id"), "findings": findings}
                    for row in [*arguments.get("candidates", []), *arguments.get("routes", [])]
                    if (findings := ranking_findings(row))],
                "instruction": "这是本次提交的计算/依据回执，不额外重试；ranking_consistency 的 conflict/incomplete 在既有预算内只修受影响比较：复核理由、对象和分数，不由程序改分或淘汰。明确计量冲突须修改该对象或保留为待修订，不得冒充已配平。"}
            # 只提示未组装对象，不自动补步骤、不把机制关联当化学闭合证明。
            result["route_assembly_gaps"] = [
                {"mechanism_id": row["mechanism_id"], "route_type": row["route_type"]}
                for row in mechanism_route_coverage(context.get("mechanisms", []),
                    list(staged["routes"].values()), list(staged["candidates"].values()))
                if not row["linked_route_ids"]]
            result['route_assembly_tasks'] = [row for row in mechanism_route_coverage(context.get('mechanisms', []),
                list(staged['routes'].values()), list(staged['candidates'].values())) if not row['linked_route_ids']]
            result["read_card_comparison_tasks"] = read_card_comparison_tasks(context,
                list(staged["candidates"].values()), staged.get("read_card_reviews", []))
            result['completion_tasks'] = screening_completion_tasks(context, list(staged['candidates'].values()),
                list(staged['routes'].values()), staged.get('read_card_reviews', []))
            if result["route_assembly_gaps"]:
                result["instruction"] += " 提交前补齐拟保留机制的路线，或在 knowledge_gaps 明确不组装的原因；不得仅在文字中声称路线已完成。"
            checkpoint = retrieval.queue_gap_check(gaps)
            if checkpoint:
                result["gap_check"] = checkpoint
                result["instruction"] += " " + checkpoint["instruction"]
            return result
        def accept_or_request_repair():
            try:
                return operation()
            except (ValueError, KeyError, ValidationError) as exc:
                if not feedback:
                    raise  # 运行或上下文尚未核实，不写入任何草稿。
                submitted = dict(raw_arguments)
                item = (submitted.get("candidate") or submitted.get("route_header")
                        or next(iter(submitted.get("routes") or []), {}))
                item_kind = "candidate" if "candidate" in submitted else "route"
                identifier = str(item.get("proposal_id") or item.get("card_id") or item.get("route_id")
                                 or submitted.get("route_id") or "missing_identity")[:160]
                if "route_step" in submitted:
                    identifier += ':step:' + str(submitted.get("step_index"))
                if not issues:
                    issues.append({"path": "$", "rule": "identity_or_binding", "message": str(exc)[:600]})
                # 从磁盘重读，绝不把失败操作中局部修改的候选或路线写回。
                store, path = feedback["artifacts"], feedback["path"]
                saved = store._read_json_if_exists(path) or {}
                if saved.get("upstream_digest") != feedback["upstream_digest"]:
                    saved = {"run_id": feedback["run_id"], "upstream_digest": feedback["upstream_digest"],
                             "candidates": {}, "routes": {}, "route_parts": {}}
                pending = saved.setdefault("item_feedback", {})
                attempts = pending.get(identifier, {}).get("attempts", 0) + 1
                repair = {"item_id": identifier, "item_kind": item_kind,
                          "owner_id": str(item.get("proposal_id") or item.get("card_id") or item.get("route_id")
                                          or submitted.get("route_id") or "missing_identity"),
                          "phase": "staging", "attempts": attempts, "issues": issues,
                          "action": "resubmit_item" if attempts <= 2 else "leave_unresolved",
                          "instruction": "只修当前对象；不换 ID、不重发其他有效项。该项无法修正则明确保留为未完成，不得声称已排除或通过。"}
                pending[identifier] = repair
                store._atomic_write(path, _json_content(saved))
                return {"accepted": False, "drafted": False, "run_id": feedback["run_id"],
                        "repair_request": repair, "candidate_ids": list(saved["candidates"]),
                        "route_ids": list(saved["routes"])}
        return await self._call("小批暂存候选与路线", accept_or_request_repair)

    async def _screen_commit(self, raw_arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        async def operation() -> dict[str, Any]:
            arguments = dict(raw_arguments)
            run_id, corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            snapshot = artifacts.snapshot(
                run_id,
                artifact_names=[
                    "01_reaction_analysis.json",
                    "02_mechanisms.json",
                    "03_capabilities.json",
                ],
            )
            self._bind_workflow_run(call, run_id)
            retrieval = RetrievalSession(artifacts, self._library, "screen", snapshot)
            draft = retrieval.candidate_draft()
            if not draft:
                raise ValueError(
                    "screening commit requires a chemistry-first candidate_draft created before Wiki evidence"
                )
            packet = retrieval.packet()
            packet["candidate_draft"] = draft
            context = build_screening_context(snapshot, self._library, packet)
            expected_digest = ContextDelivery(artifacts, snapshot, "screen", call.session_id).submitted_digest(arguments, context["context_digest"])
            if expected_digest != context["context_digest"]:
                raise ValueError("screening context is stale or does not belong to this run; "
                                 "compare with the most recent context receipt, not an earlier read: "
                                 f"latest context_digest={context['context_digest']}")
            staged_before_close = artifacts._read_json_if_exists(artifacts._run_path(run_id, 'screening_items.draft.json')) or {}
            upstream = hashlib.sha256(_json_content(snapshot['documents']).encode()).hexdigest()
            reusable = staged_before_close if staged_before_close.get('upstream_digest') == upstream else {}
            closing_candidates = list(reusable.get('candidates', {}).values()) if arguments.get('use_staged') else arguments.get('candidates', [])
            closing_routes = list(reusable.get('routes', {}).values()) if arguments.get('use_staged') else arguments.get('routes', [])
            closing_reviews = {row['card_id']: row for row in reusable.get('read_card_reviews', [])}
            closing_reviews.update({row['card_id']: row for row in arguments.get('read_card_reviews', [])})
            completion = screening_completion_tasks(context, closing_candidates, closing_routes, list(closing_reviews.values()))
            checkpoint_key = hashlib.sha256(_json_content({key: [row.get('card_id') or row.get('route_id') or row.get('candidate_id') for row in rows]
                                                          for key, rows in completion.items()}).encode()).hexdigest()
            if any(completion.values()) and checkpoint_key not in retrieval.state.setdefault('completion_checkpoints', []):
                # 一组待办只交付一次，占既有阶段预算；先处理已读内容，不再花预算搜同一问题。
                retrieval.state['completion_checkpoints'].append(checkpoint_key)
                retrieval.save()
                cards = {card['card_id']: card for card in context.get('cards', [])}
                for task in completion['read_comparisons']:
                    task['evidence_preview'] = [{'ref': claim['ref'], 'text': str(claim.get('statement', ''))[:600],
                                                'truncated': len(str(claim.get('statement', ''))) > 600}
                                               for claim in cards[task['card_id']].get('claims', [])[:2]]
                return ContextDelivery(artifacts, snapshot, 'screen', call.session_id).publish({
                    'committed': False, 'completion_required': True, 'context_digest': context['context_digest'],
                    'completion_tasks': completion,
                    'instruction': '先完成已读线索的共享事件／缺口比较，再决定提名、不选或明确暂缓。可单独调用 enzyme_screen_items_stage(read_card_reviews=...)，无需重发候选。多酶路线按原输入输出补 equation 或完整账本；未知公式可为空，不臆造。原候选和路线均已保存，沿用 ID；本回包不是失败重试，不提高预算或强制任何酶入选。'})
            continuation = await retrieval.continue_gaps(arguments.get("knowledge_gaps", []), self._retriever,
                questions=arguments.get("retrieval_questions"), finish=bool(arguments.get("finish_exploration")))
            if continuation:
                updated_packet = retrieval.packet()
                updated_packet["candidate_draft"] = draft
                updated = build_screening_context(snapshot, self._library, updated_packet)
                return ContextDelivery(artifacts, snapshot, "screen", call.session_id).publish({
                    **continuation, "context_digest": updated["context_digest"]})
            # 回执校验完成后补齐本轮新出现的身份去向；不改变已交付上下文，
            # 不恢复旧评分或目标活性。身份引用仍由构建器按当前授权原文复核。
            context["candidate_generation"]["prior_candidates"] = retrieval.candidate_identities()
            context["identity_bindings"] = dict(retrieval.state.get("identity_bindings", {}))
            current_staged = artifacts._read_json_if_exists(artifacts._run_path(run_id, "screening_items.draft.json"))
            current_upstream_digest = hashlib.sha256(_json_content(snapshot["documents"]).encode()).hexdigest()
            incomplete = []
            if current_staged and current_staged.get("upstream_digest") == current_upstream_digest:
                incomplete = [key for key, part in current_staged.get("route_parts", {}).items()
                              if set(part["steps"]) != {str(position) for position in range(1, part["header"]["step_count"] + 1)}]
                if incomplete and not (arguments.get("use_staged") and arguments.get("defer_incomplete_routes")):
                    raise ValueError("unfinished route steps remain; complete declared steps before commit, do not drop them")
            if arguments.get("defer_incomplete_routes") and not arguments.get("use_staged"):
                raise ValueError("defer_incomplete_routes requires use_staged=true")
            if arguments.get("use_staged"):
                if arguments.get("candidates") or arguments.get("routes"):
                    raise ValueError("use_staged requires candidates=[] and routes=[]; do not mix staged and inline items")
                staged = current_staged
                upstream_digest = current_upstream_digest
                if not staged or staged.get("upstream_digest") != upstream_digest:
                    raise ValueError("screening draft missing or predates upstream revision; restage items")
                arguments["candidates"] = list(staged["candidates"].values())
                arguments["mechanism_feedback"] = list(dict.fromkeys(
                    _json_content(item) for item in [*staged.get("mechanism_feedback", []),
                                                    *arguments.get("mechanism_feedback", [])]))
                arguments["mechanism_feedback"] = [json.loads(item) for item in arguments["mechanism_feedback"]]
                reviews = {r["card_id"]: r for r in staged.get("read_card_reviews", [])}
                reviews.update({r["card_id"]: r for r in arguments.get("read_card_reviews", [])})
                arguments["read_card_reviews"] = list(reviews.values())
                arguments["routes"] = [route for key, route in staged["routes"].items() if key not in incomplete]
                notes = list(arguments.get("knowledge_gaps") or [])
                for identifier in incomplete:
                    notes.append(f"路线 {identifier} 声明步骤未完成，显式延后；已存步骤保留在草稿，不作为完整路线交付。")
                for identifier, request in staged.get("item_feedback", {}).items():
                    fields = '、'.join(issue['path'] for issue in request.get('issues', [])[:3])
                    notes.append(f"条目 {identifier} 尚未通过提交校验（{fields}）；保留为未完成，不表示化学上不可行或已被排除。")
                arguments["knowledge_gaps"] = notes
            accepted_candidates: list[dict[str, Any]] = []
            accepted_routes: list[dict[str, Any]] = []
            rejected_items: list[dict[str, Any]] = []
            candidate_aliases: dict[str, dict[str, str]] = {}

            def reject(kind: str, index: int, spec: Mapping[str, Any], reason: str) -> None:
                identifier = str(spec.get("card_id") or spec.get("proposal_id") or spec.get("route_id") or index)
                rejected_items.append({"kind": kind, "id": identifier, "reason": reason[:300]})

            # A route may need more distinct enzymes than the user's Top-N display.
            # Keep the existing 9 named + 3 unmatched internal bound; reporting
            # still applies result_limits.candidates to the visible ranking.
            candidate_limit = 30
            for index, raw in enumerate(arguments.get("candidates") or [], 1):
                spec = dict(raw)
                alias = str(spec.pop("candidate_id", "") or "").strip()
                spec.pop("identity_type", None)
                # This is a deterministic output field.  A model may echo it
                # from earlier context; ignore the echo instead of discarding
                # an otherwise valid candidate.
                spec.pop("verification_note", None)
                candidate_type = str(spec.pop("candidate_type", "") or "").strip()
                if candidate_type in {"wiki_enzyme", "model_enzyme", "unmatched_capability"}:
                    spec.setdefault("candidate_kind", candidate_type)
                if isinstance(spec.get("knowledge_gap"), list):
                    spec["knowledge_gap"] = "；".join(str(item) for item in spec["knowledge_gap"] if str(item).strip())
                if not spec.get("candidate_kind"):
                    spec["candidate_kind"] = (
                        "wiki_enzyme" if spec.get("card_id") else
                        "unmatched_capability" if spec.get("required_capability") else "model_enzyme"
                    )
                if spec.get("card_id") and spec.get("candidate_kind", "wiki_enzyme") == "wiki_enzyme":
                    for redundant in ("proposal_id", "enzyme_name", "enzyme_family", "cofactor_classes"):
                        spec.pop(redundant, None)  # Wiki identity always comes from the read card.
                    card = next((item for item in context["cards"] if item["card_id"] == spec["card_id"]), None)
                    if card is not None:
                        quote = str(spec.get("native_reaction_quote") or "").strip()
                        native = str(card.get("native_reaction") or "")
                        if quote and quote not in native:
                            def equivalent_reaction(value: str) -> str:
                                return "".join(value.replace("→", "=").replace("⇌", "=").strip("*。. ").split())
                            matching_line = next((line.strip("* ") for line in native.splitlines()
                                if equivalent_reaction(line) == equivalent_reaction(quote)), "")
                            if matching_line and len(matching_line) <= 240:
                                spec["native_reaction_quote"] = matching_line
                            else:
                                spec["native_reaction_quote"] = ""
                                spec["substrate_relation"] = "unresolved"
                                note = "天然反应引文未与已读 Wiki 原文逐字吻合，底物关系降为待核对"
                                spec["knowledge_gap"] = "；".join(filter(None, [str(spec.get("knowledge_gap") or ""), note]))
                if isinstance(spec.get("confidence"), (int, float)):
                    spec["confidence"] = "unresolved"  # A numeric probability is not calibrated evidence.
                if isinstance(spec.get("score"), (int, float)) and not isinstance(spec["score"], bool) and 0 <= spec["score"] <= 1:
                    spec["score"] *= 100
                # Fixable enum synonyms (e.g. "main_candidate", "unknown") must not
                # discard an otherwise usable candidate.
                enum_notes = normalize_candidate_submission(spec)
                if enum_notes:
                    spec["knowledge_gap"] = "；".join(filter(None, [
                        str(spec.get("knowledge_gap") or ""), *enum_notes,
                    ]))[:400]
                errors = list(Draft202012Validator(self._screen_candidate_schema).iter_errors(spec))
                if errors:
                    reject("candidate", index, spec, '; '.join(r['path'] + ': ' + r['message']
                           for r in validation_issues(self._screen_candidate_schema, spec)))
                    continue
                if len(accepted_candidates) >= candidate_limit:
                    reject("candidate", index, spec, f"超过本轮候选上限 {candidate_limit}")
                    continue
                try:
                    trial, _ = build_screening_documents(
                        context, candidate_specs=[*accepted_candidates, spec], route_specs=[],
                        knowledge_gaps=["逐项校验中"],
                    )
                    validation = self._validate_document("candidate_ranking", trial)
                    if not validation["valid"]:
                        raise ValueError(str(validation["errors"][0]))
                except (KeyError, TypeError, ValueError) as exc:
                    reject("candidate", index, spec, str(exc))
                    continue
                binding = {key: str(spec[key]) for key in ("card_id", "proposal_id") if spec.get(key)}
                if alias and alias in candidate_aliases and candidate_aliases[alias] != binding:
                    reject("candidate", index, spec, "candidate alias already belongs to a different identity; use a distinct alias")
                    continue
                accepted_candidates.append(spec)
                if alias:
                    candidate_aliases[alias] = {
                        key: str(spec[key]) for key in ("card_id", "proposal_id") if spec.get(key)
                    }

            anchor_notes = uncompared_anchor_notes(context, accepted_candidates)

            if arguments.get("candidates") and not accepted_candidates:
                reasons = "；".join(item["reason"] for item in rejected_items if item["kind"] == "candidate")
                raise ValueError("all submitted candidates were rejected: " + reasons[:900])

            for index, raw in enumerate(arguments.get("routes") or [], 1):
                spec = dict(raw)
                for redundant in ("label", "evidence_refs", "mechanism_ids", "material_handoff", "routes_summary"):
                    spec.pop(redundant, None)
                # Keep explanatory prose as gaps, but do not let invented keys
                # discard an otherwise valid route.
                extra_details = []
                for extra in ("counterfactual_coverage", "support_reason"):
                    detail = spec.pop(extra, "")
                    if isinstance(detail, str) and detail.strip():
                        extra_details.append(detail.strip()[:500])
                spec.setdefault("contradiction_refs", [])
                extra_gap = str(spec.pop("knowledge_gap", "") or "").strip()
                gaps = list(spec.get("unsupported_intermediates") or [])
                gaps.extend(extra_details)
                gaps.extend(str(item) for item in (spec.pop("knowledge_gaps", []) or []) if str(item).strip())
                gaps.extend(str(item) for item in (spec.pop("gaps", []) or []) if str(item).strip())
                gaps.extend(str(item) for item in (spec.pop("missing_events", []) or []) if str(item).strip())
                spec.pop("shared_events", None)
                native_conflict = str(spec.pop("native_termination_conflict", "") or "").strip()
                if native_conflict:
                    gaps.append(native_conflict)
                if extra_gap:
                    gaps.append(extra_gap)
                steps = []
                for raw_step in spec.get("steps") or []:
                    if not isinstance(raw_step, Mapping):
                        steps.append(raw_step)
                        continue
                    step = dict(raw_step)
                    normalize_route_step_binding(step, accepted_candidates)
                    catalyst = step.pop("catalyst", None)
                    if isinstance(catalyst, Mapping) and not (step.get("card_id") or step.get("proposal_id")):
                        step.update({key: catalyst[key] for key in ("card_id", "proposal_id") if catalyst.get(key)})
                    if step.get("enzyme_card_id") and not step.get("card_id"):
                        step["card_id"] = step.pop("enzyme_card_id")
                    alias = str(step.pop("candidate_ref", "") or "").strip()
                    if alias and not (step.get("card_id") or step.get("proposal_id")):
                        step.update(candidate_aliases.get(alias) or {})
                    step.pop("cofactors", None)  # Cofactor identity comes from the selected candidate.
                    for redundant in ("step_index", "step", "order", "cofactor", "unclosed", "cofactor_regeneration", "gap"):
                        detail = str(step.pop(redundant, "") or "").strip()
                        if redundant not in {"step_index", "step", "order"} and detail:
                            gaps.append(detail)
                    for extra in ("counterfactual_coverage", "support_reason"):
                        detail = step.pop(extra, "")
                        if isinstance(detail, str) and detail.strip():
                            gaps.append(detail.strip()[:500])
                    if not isinstance(step.get("evidence_refs"), list):
                        step["evidence_refs"] = []
                    step.setdefault("evidence_refs", [])
                    if step.get("card_id") and not step["evidence_refs"]:
                        cited_candidate = next((item for item in accepted_candidates if item.get("card_id") == step["card_id"]), None)
                        if cited_candidate:
                            step["evidence_refs"] = list(cited_candidate.get("support_refs") or [])[:2]
                    steps.append(step)
                spec["steps"] = steps
                spec["unsupported_intermediates"] = list(dict.fromkeys(gaps))[:8]
                if isinstance(spec.get("confidence"), (int, float)):
                    spec["confidence"] = "unresolved"
                if isinstance(spec.get("score"), (int, float)) and not isinstance(spec["score"], bool) and 0 <= spec["score"] <= 1:
                    spec["score"] *= 100
                # Same enum repair policy as candidates: a synonym typo must not
                # discard an otherwise usable route.
                enum_notes = normalize_route_submission(spec)
                if str(spec.get("route_type") or "").strip().casefold() in {"", "unknown", "unresolved", "none", "n/a"}:
                    enum_notes.append(
                        f"route_type {spec.get('route_type')!r} 无法判定，改按步骤数回退为单酶/多酶路线"
                    )
                    spec.pop("route_type", None)
                if spec.get("route_type") == "single_enzyme":
                    spec["route_type"] = "direct"
                spec.setdefault("route_type", "multi_enzyme" if len(steps) > 1 else "direct")
                if enum_notes:
                    spec["unsupported_intermediates"] = list(dict.fromkeys(
                        [*spec["unsupported_intermediates"], *enum_notes]
                    ))[:8]
                errors = list(Draft202012Validator(self._screen_route_schema).iter_errors(spec))
                if errors:
                    reject("route", index, spec, '; '.join(r['path'] + ': ' + r['message']
                           for r in validation_issues(self._screen_route_schema, spec)))
                    continue
                try:
                    _, trial = build_screening_documents(
                        context, candidate_specs=accepted_candidates,
                        route_specs=[*accepted_routes, spec], knowledge_gaps=["逐项校验中"],
                    )
                    validation = self._validate_document("route_ranking", trial)
                    if not validation["valid"]:
                        raise ValueError(str(validation["errors"][0]))
                except (KeyError, TypeError, ValueError) as exc:
                    reject("route", index, spec, str(exc))
                    continue
                accepted_routes.append(spec)

            issue_notes = [f"{item['kind']} {item['id']} 校验未通过：{item['reason']}" for item in rejected_items]
            candidate_document, route_document = build_screening_documents(
                context, candidate_specs=accepted_candidates, route_specs=accepted_routes,
                knowledge_gaps=[*(arguments.get("knowledge_gaps") or []), *issue_notes, *anchor_notes],
            )
            def quarantine_optional(kind, index, raw, reason):
                identifier = f"{'feedback' if kind == 'mechanism_feedback' else kind}-{index}"
                rejected_items.append({"kind": kind, "id": identifier,
                    "reason": str(reason)[:300], "submitted_value": deepcopy(raw)})
                candidate_document['knowledge_gaps'].append(
                    f"辅助条目 {identifier} 尚未通过引用／格式核对，原文保留在提交审计中；不据此否定候选或路线。")
            feedback = []
            feedback_candidates = {row["candidate_id"] for row in candidate_document["enzyme_candidates"]}
            feedback_candidates.update(row["proposal_id"] for row in candidate_document["enzyme_candidates"] if row.get("proposal_id"))
            feedback_mechanisms = {row["hypothesis_id"] for row in context.get("mechanisms", [])}
            authorized_refs = {ref for card in packet.get("cards", []) for ref in (claim["ref"] for claim in card.get("claims", []))}
            for index, raw in enumerate(_plain_json_value(arguments.get("mechanism_feedback") or []), 1):
                item = deepcopy(raw)
                try:
                    if validation_issues(FEEDBACK_SCHEMA, item):
                        raise ValueError("mechanism_feedback does not match its declared schema")
                    item["candidate_ids"] = feedback_candidate_ids(item["candidate_ids"], candidate_document["enzyme_candidates"])
                    if not set(item["candidate_ids"]) <= feedback_candidates or not set(item["mechanism_ids"]) <= feedback_mechanisms:
                        raise ValueError("mechanism_feedback must refer to selected candidate and current mechanism IDs")
                    if not set(item["evidence_refs"]) <= authorized_refs:
                        raise ValueError("mechanism_feedback references unread Wiki evidence")
                except ValueError as exc:
                    # 辅助建议单项隔离，不能迫使模型重交有效候选；原文送审，不当作有效反馈或证据。
                    quarantine_optional('mechanism_feedback', index, raw, exc)
                    continue
                item.update(feedback_id=f"feedback-{index}", status="hypothesis_pending_review")
                feedback.append(item)
            candidate_document["mechanism_feedback"] = feedback
            read_ids = {card["card_id"] for card in context.get("cards", [])}
            submitted_reviews = list(arguments.get("read_card_reviews") or [])
            reviews = []
            ids = [row.get('card_id') for row in submitted_reviews if isinstance(row, Mapping)]
            for index, row in enumerate(submitted_reviews, 1):
                if (not Draft202012Validator(READ_CARD_REVIEW_SCHEMA).is_valid(row)
                        or row['card_id'] not in read_ids or ids.count(row['card_id']) != 1):
                    quarantine_optional('read_card_review', index, row,
                        'read-card review must have valid fields and uniquely reference an actually read card')
                    continue
                reviews.append(row)
            candidate_document["read_card_reviews"] = reviews
            selected_cards = {cid for row in candidate_document["enzyme_candidates"] for cid in row.get("card_ids", [])}
            assessed = {row["card_id"] for row in reviews if row.get('comparison')} | selected_cards
            candidate_document["unassessed_read_cards"] = sorted(read_ids - assessed)
            route_limit = int(context["result_limits"]["routes"])
            # 处置只是模型对去向的声明，不能让无效合并/多余声明阻断已验证的科学产物。
            valid_dispositions, disposition_rejections = [], []
            for raw in arguments.get("draft_dispositions") or []:
                issues = validation_issues(DRAFT_DISPOSITION_SCHEMA, raw, "$.draft_dispositions")
                try:
                    if issues:
                        raise ValueError('; '.join(r['path'] + ': ' + r['message'] for r in issues))
                    build_draft_dispositions(candidate_document["candidate_generation"],
                                             candidate_document["enzyme_candidates"], [*valid_dispositions, raw])
                except (ValueError, KeyError) as exc:
                    identifier = str(raw.get('proposal_id') or 'missing_identity')[:64]
                    note = f"候选处置 {identifier} 未被采用：{str(exc)[:600]}；保持实际已保留或未评估状态，不认定为已合并/已排除。"
                    disposition_rejections.append({"id": identifier, "reason": str(exc)[:600]})
                    candidate_document['knowledge_gaps'].append(note)
                else:
                    valid_dispositions.append(raw)
            candidate_document["candidate_generation"]["dispositions"] = build_draft_dispositions(
                candidate_document["candidate_generation"], candidate_document["enzyme_candidates"], valid_dispositions)
            candidate_limit = int(context["result_limits"].get("candidates", 5))
            old_review = artifacts._read_json_if_exists(artifacts._run_path(run_id, '06_critique.json')) or {}
            previous = {**snapshot['documents'], **{name: artifacts._read_json_if_exists(artifacts._run_path(run_id, name)) or {}
                for name in ('04_candidates.json', '05_routes.json')}}
            old_runtime = artifacts._read_json_if_exists(artifacts._run_path(run_id, 'review_runtime.json')) or {}
            candidate_document, route_document, local_revision = preserve_unaffected_screening(
                previous, old_review, candidate_document, route_document,
                review_policy_current=(old_review.get('audited_policy_digest') or old_runtime.get('policy_digest')) == policy_digest())
            validations = {
                "candidate_ranking": self._validate_document("candidate_ranking", candidate_document),
                "route_ranking": self._validate_document("route_ranking", route_document),
            }
            invalid = {
                name: result["errors"][:5]
                for name, result in validations.items()
                if not result["valid"]
            }
            if invalid:
                raise ValueError("deterministic stage-3 assembly failed schema validation: " + _json_content(invalid))
            base_key = str(arguments.get("idempotency_key") or "")
            allow_supersede = bool(arguments.get("allow_supersede", False))
            candidate_result = artifacts.commit(
                run_id=run_id,
                artifact_name="04_candidates.json",
                content=json.dumps(candidate_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                media_type="application/json",
                schema_name="candidate_ranking",
                idempotency_key=f"{base_key}:04" if base_key else "",
                allow_supersede=allow_supersede,
            )
            route_result = artifacts.commit(
                run_id=run_id,
                artifact_name="05_routes.json",
                content=json.dumps(route_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                media_type="application/json",
                schema_name="route_ranking",
                idempotency_key=f"{base_key}:05" if base_key else "",
                allow_supersede=allow_supersede,
            )
            committed_artifacts = [
                candidate_result["artifact"],
                route_result["artifact"],
            ]
            receipt = {
                "schema_version": "1.0",
                "run_id": run_id,
                "committed_artifacts": ["04_candidates.json", "05_routes.json"],
                "top_candidate_ids": [
                    item["candidate_id"]
                    for item in candidate_document["enzyme_candidates"]
                    if not item["hard_conflict"]
                    and item.get("candidate_role") != "diagnostic_control"
                ][:candidate_limit],
                "top_route_ids": [item["route_id"] for item in route_document["routes"][:route_limit]],
                "rejected_items": [{key: value for key, value in row.items() if key != 'submitted_value'}
                                   for row in rejected_items],
                "rejected_dispositions": disposition_rejections,
                "draft_dispositions": candidate_document["candidate_generation"]["dispositions"],
                "mechanism_route_coverage": mechanism_route_coverage(context.get("mechanisms", []), route_document["routes"], candidate_document["enzyme_candidates"]),
            }
            # 审核只消费绑定当前 04/05 的结构化提交回执，不从历史自然语言猜测错误归属。
            artifacts._atomic_write(artifacts._run_path(run_id, "screening_submission.json"), _json_content({
                "run_id": run_id,
                "artifact_digests": {name: hashlib.sha256(_json_content(doc).encode()).hexdigest()
                    for name, doc in (("04_candidates.json", candidate_document), ("05_routes.json", route_document))},
                "rejected_items": [{**item, "phase": "commit", "status": "not_submitted"} for item in rejected_items],
                "pending_repairs": list((current_staged or {}).get("item_feedback", {}).values()),
                "rule": "只表示特定对象的提交/引用校验状态，不是化学反证；已修复项不在 pending_repairs 中，不得把一个路线的错误归到另一条。",
            }))
            return self._attach_run_id_correction({
                "local_revision": local_revision,
                "committed": any(bool(item.get("committed")) for item in (candidate_result, route_result)),
                "idempotent": all(bool(item.get("idempotent")) for item in (candidate_result, route_result)),
                "artifacts": committed_artifacts,
                "artifact_refs": {item["name"]: item["path"] for item in committed_artifacts},
                "stage_receipt": receipt,
                "top_candidates": [
                    {
                        "candidate_id": item["candidate_id"],
                        "candidate_role": item.get("candidate_role", "primary"),
                        "enzyme_family": item["enzyme_family"],
                        **{field: item[field] for field in ("enzyme_name", "enzyme_id", "ec_number", "required_capability", "proposal_id", "candidate_kind", "verification_note") if field in item},
                        "card_ids": item["card_ids"],
                        "score": item["score"],
                        "confidence": item["confidence"],
                        "hard_conflict": item["hard_conflict"],
                    }
                    for item in candidate_document["enzyme_candidates"][:candidate_limit + 4]
                ],
                "top_routes": [
                    {
                        "route_id": item["route_id"],
                        "route_type": item["route_type"],
                        "score": item["score"],
                        "confidence": item["confidence"],
                    }
                    for item in route_document["routes"][:route_limit]
                ],
                "schema_valid": True,
            }, corrected_from, run_id)

        return await self._call("确定性提交候选与路线", operation)

    @staticmethod
    def _check_retrieval_budget(context):
        if len(_json_content({"ok": True, "result": context})) > 64000:
            raise ValueError("retrieval context exceeds 64000 characters; stop and reduce upstream verbosity, do not retry unchanged")

    async def _card_search(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        store = self._artifact_store(call)
        async def operation():
            text = " ".join(str(v) for k, v in arguments.items() if k != "limit" and isinstance(v, (str, list, tuple)))
            index = WikiIndex(self._library, store._workspace, store._workspace_dir)
            key = (store._workspace.resolve_write_path(
                str(Path(store._workspace_dir) / "card-search-state.json")), call.session_id)
            backend = self._card_search_states.setdefault(key, {})
            self._card_search_states.move_to_end(key)
            while len(self._card_search_states) > 32:
                self._card_search_states.popitem(last=False)
            result = await self._retriever.search(index, [text], backend=backend,
                                                  limit=max(1, min(15, int(arguments.get("limit") or 8))))
            result["results"] = [{**r, "retrieval": {"mode": result["retrieval_mode"],
                "matched_passages": [{"field_path": r["field_path"], "text": r["excerpt"]}]}} for r in result["results"]]
            return result
        return await self._call("混合检索生产知识卡", operation)

    async def _card_get(self, arguments: Mapping[str, Any], _call: ToolCallContext) -> ToolOutput:
        return await self._call(
            "渐进读取酶 Wiki",
            lambda: self._library.card(
                str(arguments.get("card_id") or ""),
                detail_level=str(arguments.get("detail_level") or "full"),
                sections=[str(item) for item in arguments.get("sections") or []],
            ),
        )

    def _validate_document(self, schema_name: str, document: Any) -> dict[str, Any]:
        normalized = str(schema_name or "").strip()
        try:
            filename = _SCHEMA_FILES[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown schema_name: {normalized}") from exc
        path = self._plugin_root / "schemas" / filename
        schema = json.loads(self._ctx.workspace.read_text(str(path), encoding="utf-8"))
        validator = Draft202012Validator(schema)
        errors = []
        for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path)):
            errors.append(
                {
                    "location": ".".join(str(item) for item in error.absolute_path) or "<root>",
                    "message": error.message,
                    "validator": error.validator,
                }
            )
        return {"valid": not errors, "schema_name": normalized, "errors": errors}

    async def _schema_validate(self, arguments: Mapping[str, Any], _call: ToolCallContext) -> ToolOutput:
        return await self._call(
            "校验阶段 JSON",
            lambda: self._validate_document(str(arguments.get("schema_name") or ""), arguments.get("document")),
        )

    async def _artifact_commit(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation() -> dict[str, Any]:
            artifacts = self._artifact_store(call)
            has_document = "document" in arguments
            has_text = "text" in arguments
            if has_document == has_text:
                raise ValueError("provide exactly one of document or text")
            artifact_name = str(arguments.get("artifact_name") or "")
            schema_name = str(arguments.get("schema_name") or "").strip() or None
            render_companions = bool(arguments.get("render_companions", False))
            allow_supersede = bool(arguments.get("allow_supersede", False))
            if render_companions and artifact_name != "07_final_report.json":
                raise ValueError("render_companions is only valid for 07_final_report.json")
            if has_document:
                document = _plain_json_value(arguments.get("document"))
                expected_schema = _ARTIFACT_SCHEMAS.get(artifact_name)
                if expected_schema and schema_name != expected_schema:
                    raise ValueError(
                        f"{artifact_name} requires schema_name={expected_schema}"
                    )
                if artifact_name.endswith(".json") and expected_schema is None:
                    raise ValueError(f"JSON artifact is not mapped to a stage schema: {artifact_name}")
                validation = self._validate_document(str(schema_name), document)
                if not validation["valid"]:
                    raise ValueError("artifact schema validation failed: " + _json_content(validation["errors"][:5]))
                content = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                media_type = "application/json"
            else:
                if schema_name:
                    raise ValueError("schema_name is only valid with document artifacts")
                if artifact_name.endswith(".json"):
                    raise ValueError("JSON artifacts require a document and the mapped schema_name")
                content = str(arguments.get("text") or "")
                media_type = "text/markdown" if artifact_name.endswith(".md") else "text/plain"
            base_key = str(arguments.get("idempotency_key") or "")
            primary = artifacts.commit(
                run_id=str(arguments.get("run_id") or ""),
                artifact_name=artifact_name,
                content=content,
                media_type=media_type,
                schema_name=schema_name,
                idempotency_key=base_key,
                allow_supersede=allow_supersede,
            )
            if not render_companions:
                return primary

            report = document
            if not isinstance(report, Mapping):
                raise ValueError("final report companions require an object document")
            markdown = render_final_report_markdown(report)
            mermaid = normalize_mermaid(report.get("mermaid_diagram"))
            companion_specs = (
                ("07_final_report.md", markdown, "text/markdown", "markdown"),
                ("mechanism_routes.mmd", mermaid, "text/plain", "mermaid"),
            )
            commit_results = [primary]
            committed_artifacts = [primary["artifact"]]
            for name, companion_content, companion_media_type, suffix in companion_specs:
                result = artifacts.commit(
                    run_id=str(arguments.get("run_id") or ""),
                    artifact_name=name,
                    content=companion_content,
                    media_type=companion_media_type,
                    schema_name=None,
                    idempotency_key=f"{base_key}:{suffix}" if base_key else "",
                    allow_supersede=allow_supersede,
                )
                commit_results.append(result)
                committed_artifacts.append(result["artifact"])
            return {
                "committed": any(bool(item.get("committed")) for item in commit_results),
                "idempotent": all(bool(item.get("idempotent")) for item in commit_results),
                "artifact": primary["artifact"],
                "committed_artifacts": committed_artifacts,
                "artifact_refs": {
                    str(item["name"]): str(item["path"])
                    for item in committed_artifacts
                },
                "chat_report_markdown": markdown,
            }

        return await self._call("提交酶发现产物", operation)

    async def _review_prepare(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation():
            if arguments.get("confirm") is not True:
                raise ValueError("explicit review-only confirmation is required")
            run_id, _ = self._resolve_workflow_run_id(arguments, call)
            store = self._artifact_store(call)
            for name, schema in _ARTIFACT_SCHEMAS.items():
                if not name.startswith(("01_", "02_", "03_", "04_", "05_")):
                    continue
                document = store.snapshot(run_id, artifact_names=[name])["documents"][name]
                validation = self._validate_document(schema, document)
                if not validation["valid"]:
                    raise ValueError("review-only invalid upstream schema: " + name)
            state = store.prepare_review(run_id, self._library.fingerprint())
            return {**state, "run_id": run_id, "target_stage_id": "critical_report",
                    "instruction": "仅调用第四阶段审核与最终化工具；不得执行前三阶段或增加自动修订。旧交付已备份。"}
        return await self._call("安全重开第四阶段审核", operation)

    async def _report_context(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation() -> dict[str, Any]:
            run_id, corrected_from = self._resolve_workflow_run_id(arguments, call)
            artifacts = self._artifact_store(call)
            upstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "01_reaction_analysis.json",
                    "02_mechanisms.json",
                    "03_capabilities.json",
                ],
            )
            downstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "04_candidates.json",
                    "05_routes.json",
                ],
            )
            snapshot = _merge_artifact_snapshots(upstream_snapshot, downstream_snapshot)
            self._bind_workflow_run(call, run_id)
            candidate_document = dict(
                (snapshot.get("documents") or {}).get("04_candidates.json") or {}
            )
            if snapshot["run"].get("knowledge_fingerprint") != self._library.fingerprint():
                raise ValueError("Wiki changed before audit; initialize a fresh run")
            if arguments.get('section'):
                from .audit_tools import read_audit_section
                return read_audit_section(artifacts, snapshot, arguments['section'], arguments.get('query', ''), arguments.get('offset', 0))
            base = build_report_context(snapshot)
            reentry = artifacts.review_reentry(run_id)
            if reentry.get("status") == "active":
                base["review_reentry"] = {"review_id": reentry["review_id"], "review_only": True,
                    "instruction": "按当前规则重审现有产物，不重写上游。旧审核与修订任务仅作历史参考，不能照抄旧 required_revisions；旧规则生成的覆盖元数据不代表科学错误。合理未知可带说明交付，当前实质冲突仍应 REVISE。"}
            page = arguments.get('page')
            result = audit_context(artifacts, snapshot, base, page=int(page) if page is not None else None)
            if reentry.get("status") == "active":
                result["review_reentry"] = base["review_reentry"]
            self._check_retrieval_budget(result)
            return self._attach_run_id_correction(result, corrected_from, run_id)

        return await self._call("生成轻量最终审计包", operation)

    async def _critique_commit(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation() -> dict[str, Any]:
            normalized = _plain_json_value(arguments)
            run_id, _corrected_from = self._resolve_workflow_run_id(normalized, call)
            normalized["run_id"] = run_id
            decision = str(normalized.get("review_decision") or "").strip().upper()
            revisions = [
                str(item).strip()
                for item in normalized.get("required_revisions") or []
                if str(item).strip()
            ]
            if decision == "PASS" and revisions:
                raise ValueError("PASS requires required_revisions=[]")
            if decision in {"REVISE", "PARTIAL_PASS"} and not revisions:
                raise ValueError("REVISE requires at least one required revision")

            artifacts = self._artifact_store(call)
            upstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "01_reaction_analysis.json",
                    "02_mechanisms.json",
                    "03_capabilities.json",
                ],
            )
            downstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "04_candidates.json",
                    "05_routes.json",
                ],
            )
            snapshot = _merge_artifact_snapshots(upstream_snapshot, downstream_snapshot)
            self._bind_workflow_run(call, run_id)
            documents = dict(snapshot.get("documents") or {})
            plan = review_plan(artifacts, snapshot)
            if decision in {"PASS", "PARTIAL_PASS"}:
                require_complete_audit(artifacts, snapshot)
            route_ids = {
                str(dict(item).get("route_id") or "").strip()
                for item in (dict(documents.get("05_routes.json") or {}).get("routes") or [])
                if isinstance(item, Mapping)
            }

            def normalize_issues(raw_items: Any, prefix: str) -> list[dict[str, Any]]:
                issues: list[dict[str, Any]] = []
                for index, raw in enumerate(raw_items or [], start=1):
                    item = dict(raw) if isinstance(raw, Mapping) else {}
                    issues.append(
                        {
                            "issue_id": f"{prefix}-{index}",
                            "issue_type": item.get("issue_type") or ("evidence_gap" if prefix == "gap" else review_issue_type(item)),
                            "severity": str(item.get("severity") or "minor"),
                            "statement": str(item.get("statement") or "").strip(),
                            "affected_ids": [
                                str(value).strip()
                                for value in item.get("affected_ids") or []
                                if str(value).strip()
                            ],
                            "evidence_refs": list(
                                dict.fromkeys(
                                    str(value).strip()
                                    for value in item.get("evidence_refs") or []
                                    if str(value).strip()
                                )
                            ),
                        }
                    )
                return issues

            hard_conflicts = normalize_issues(normalized.get("hard_conflicts"), "hard-conflict")
            evidence_gaps = normalize_issues(normalized.get("evidence_gaps"), "gap")
            route_dispositions: list[dict[str, Any]] = []
            for raw in normalized.get("route_dispositions") or []:
                item = dict(raw) if isinstance(raw, Mapping) else {}
                route_id = str(item.get("route_id") or "").strip()
                if route_id not in route_ids:
                    raise ValueError(f"route disposition references an unknown route_id: {route_id}")
                route_dispositions.append(
                    {
                        "route_id": route_id,
                        "disposition": str(item.get("disposition") or ""),
                        "reason": str(item.get("reason") or "").strip(),
                        "evidence_refs": list(
                            dict.fromkeys(
                                str(value).strip()
                                for value in item.get("evidence_refs") or []
                                if str(value).strip()
                            )
                        ),
                    }
                )
            if decision == "PASS" and route_ids and not route_dispositions and not plan:
                raise ValueError("PASS requires at least one route disposition")

            review_actions = {
                "hard_conflicts": hard_conflicts, "evidence_gaps": evidence_gaps, "required_revisions": revisions,
                "route_dispositions": route_dispositions,
                "excluded_candidate_ids": list(normalized.get("excluded_candidate_ids") or []),
                "excluded_mechanism_ids": list(normalized.get("excluded_mechanism_ids") or []),
                "candidate_reviews": list(normalized.get("candidate_reviews") or []),
                "mechanism_reviews": list(normalized.get("mechanism_reviews") or []),
            }
            if plan:
                audit_state = artifacts._read_json_if_exists(artifacts._run_path(run_id, 'report_evidence_audit.json')) or {}
                if audit_state.get('incremental_review') != plan['proof']:
                    raise ValueError('incremental review scope changed; read current report context before submitting')
                review_actions = merge_review(plan, review_actions)
                hard_conflicts, evidence_gaps = review_actions['hard_conflicts'], review_actions['evidence_gaps']
                route_dispositions = review_actions['route_dispositions']
            tasks = _plain_json_value(normalized.get("revision_tasks") or [])
            if decision == "PASS" and tasks:
                raise ValueError("PASS cannot contain revision_tasks")
            for task in tasks:
                if validation_issues(REVISION_TASK_SCHEMA, task):
                    raise ValueError("revision_tasks does not match its declared schema")
            staged_routes = artifacts._read_json_if_exists(artifacts._run_path(run_id, "screening_items.draft.json"))
            # PASS 的冲突必须已由排除/路线处置闭合；不能再次制造 REVISE 任务。
            # 未处置冲突仍由下方 require_review_consistency 拒绝。
            tasks = prepare_revision_tasks(tasks, hard_conflicts, documents, draft=staged_routes,
                                           review_actions=review_actions) if decision != "PASS" else []
            tasks, proposal_checks = validate_balance_proposals(tasks, documents)
            tasks, policy_checks = constrain_ranking_revision_tasks(tasks)
            if proposal_checks or policy_checks:
                revisions = list(dict.fromkeys(task["instruction"] for task in tasks))
                review_actions["required_revisions"] = revisions
            review_actions["revision_tasks"] = tasks
            requested_decision = decision
            decision, object_summary = aggregate_review(documents, decision, review_actions)
            if decision == "PARTIAL_PASS":
                require_complete_audit(artifacts, snapshot)
            target = revision_target({"review_decision": decision, "critique": review_actions}, documents)

            first_route = next(
                (
                    item
                    for item in route_dispositions
                    if item.get("disposition") == "retain"
                ),
                route_dispositions[0] if route_dispositions else None,
            )
            first_conflict = hard_conflicts[0] if hard_conflicts else None
            first_gap = evidence_gaps[0] if evidence_gaps else None
            evidence_ledger = [
                {
                    "statement_id": "critique-fact",
                    "kind": "fact",
                    "statement": "自动批判只审计已提交且通过确定性校验的 01—05。",
                    "source_refs": [
                        "01_reaction_analysis.json",
                        "02_mechanisms.json",
                        "03_capabilities.json",
                        "04_candidates.json",
                        "05_routes.json",
                    ],
                },
                {
                    "statement_id": "critique-inference",
                    "kind": "inference",
                    "statement": f"基于冲突、缺口与路线处置，自动批判判定为 {decision}。",
                    "source_refs": ["04_candidates.json", "05_routes.json"],
                },
                {
                    "statement_id": "critique-hypothesis",
                    "kind": "hypothesis",
                    "statement": (
                        str(first_route.get("reason") or "")
                        if first_route
                        else "没有路线被保留为最终假设。"
                    ),
                    "source_refs": (
                        list(first_route.get("evidence_refs") or [])
                        if first_route
                        else ["05_routes.json"]
                    ),
                },
                {
                    "statement_id": "critique-counterevidence",
                    "kind": "counterevidence",
                    "statement": (
                        str(first_conflict.get("statement") or "")
                        if first_conflict
                        else "未发现未处理的硬冲突；候选反证仍保留在 04_candidates.json。"
                    ),
                    "source_refs": (
                        list(first_conflict.get("evidence_refs") or [])
                        if first_conflict
                        else ["04_candidates.json"]
                    ),
                },
                {
                    "statement_id": "critique-unknown",
                    "kind": "unknown",
                    "statement": (
                        str(first_gap.get("statement") or "")
                        if first_gap
                        else "原子映射、结构验证和封闭库覆盖边界仍是未知项。"
                    ),
                    "source_refs": (
                        list(first_gap.get("evidence_refs") or [])
                        if first_gap
                        else ["01_reaction_analysis.json", "04_candidates.json"]
                    ),
                },
            ]
            critique_document = {
                "schema_version": "1.0",
                "audited_upstream_digest": audit_digest(snapshot),
                "audited_policy_digest": policy_digest(),
                "requested_review_decision": requested_decision,
                "review_summary": object_summary,
                "run_id": run_id,
                "evidence_ledger": evidence_ledger,
                "review_decision": decision,
                "critique": {
                    "hard_conflicts": hard_conflicts,
                    "evidence_gaps": evidence_gaps,
                    "route_dispositions": route_dispositions,
                    "required_revisions": revisions,
                    "candidate_reviews": review_actions["candidate_reviews"],
                    "mechanism_reviews": review_actions["mechanism_reviews"],
                    "excluded_candidate_ids": review_actions["excluded_candidate_ids"],
                    "excluded_mechanism_ids": review_actions["excluded_mechanism_ids"],
                    "revision_tasks": tasks,
                    **({"revision_proposal_checks": proposal_checks} if proposal_checks else {}),
                    **({"revision_policy_checks": policy_checks} if policy_checks else {}),
                },
            }
            validation = self._validate_document("critique", critique_document)
            if not validation["valid"]:
                raise ValueError(
                    "generated critique schema validation failed: "
                    + _json_content(validation["errors"][:5])
                )
            content = json.dumps(critique_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            committed = artifacts.commit(
                run_id=run_id,
                artifact_name="06_critique.json",
                content=content,
                media_type="application/json",
                schema_name="critique",
                idempotency_key=str(normalized.get("idempotency_key") or ""),
                allow_supersede=bool(normalized.get("allow_supersede", False) or artifacts.review_reentry(run_id).get("status") == "active"),
            )
            reentry = artifacts.review_reentry(run_id)
            if reentry.get("status") == "active":
                reentry["critique_submitted"] = True
                artifacts._atomic_write(artifacts._run_path(run_id, "review_reentry.json"), _json_content(reentry))
            checkpoint_saved = False
            try:
                checkpoint_saved = bool(save_basis(artifacts, snapshot, critique_document))
            except (ValueError, OSError, AttributeError):
                # 缓存不可用只导致下一轮完整审核，不撤销已经持久化的合法审核。
                pass
            return {
                **committed,
                'review_checkpoint_saved': checkpoint_saved,
                'review_reuse': plan['proof'] if plan else {'mode':'full'},
                "review_decision": decision,
                "revision_request": {"target_stage_id": target, "tasks": tasks,
                    "reason": "；".join(revisions), "automatic_retry_added": False}
                    if decision != "PASS" and not artifacts.review_reentry(run_id).get("review_only") else None,
                "issue_counts": {
                    "hard_conflicts": len(hard_conflicts),
                    "evidence_gaps": len(evidence_gaps),
                    "route_dispositions": len(route_dispositions),
                },
            }

        return await self._call("提交轻量自动批判", operation)

    async def _report_finalize(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        def operation() -> dict[str, Any]:
            artifacts = self._artifact_store(call)
            run_id, _corrected_from = self._resolve_workflow_run_id(arguments, call)
            reentry = artifacts.review_reentry(run_id)
            review_only = bool(reentry.get("review_only"))
            if review_only and not reentry.get("critique_submitted"):
                raise ValueError("review-only requires a fresh critique submission; cannot reuse old PASS")
            allow_supersede = bool(arguments.get("allow_supersede", False) or review_only)
            base_key = str(arguments.get("idempotency_key") or "").strip()
            upstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "01_reaction_analysis.json",
                    "02_mechanisms.json",
                    "03_capabilities.json",
                ],
            )
            self._bind_workflow_run(call, run_id)
            downstream_snapshot = artifacts.internal_snapshot(
                run_id,
                artifact_names=[
                    "04_candidates.json",
                    "05_routes.json",
                    "06_critique.json",
                ],
            )
            # Finalization is an internal deterministic aggregation. Keep the
            # public snapshot budget strict, but read validated artifacts in
            # bounded groups so evidence-rich runs remain finalizable.
            snapshot = _merge_artifact_snapshots(upstream_snapshot, downstream_snapshot)
            report = build_final_report(snapshot, plugin_version=self._plugin_version)
            require_current_review(snapshot)
            decision = report["review_decision"]
            next_revision = revision_target(snapshot["documents"]["06_critique.json"], snapshot["documents"])
            revision_request = ({"target_stage_id": next_revision,
                "tasks": snapshot["documents"]["06_critique.json"].get("critique", {}).get("revision_tasks", []),
                "reason": "；".join(report["required_revisions"])} if decision in {"REVISE", "PARTIAL_PASS"} and not review_only else None)
            if decision in {"PASS", "PARTIAL_PASS"}:
                require_complete_audit(artifacts, snapshot)
            if decision == 'PARTIAL_PASS':
                report['limitations'].append('部分通过：仅待修订对象及其依赖暂缓推荐，无关保留对象正常交付。')
            elif decision != 'PASS':
                report["limitations"].extend([
                    "未通过审核：自动修订已终止，候选与路线仍需根据审核意见复核。",
                    *report["required_revisions"],
                ])
            report["limitations"].extend(retrieval_limitations(artifacts, run_id))
            report["limitations"].extend(audit_limitations(artifacts, run_id, snapshot))
            user_summary = render_final_report_chat(report)
            validation = self._validate_document("final_report", report)
            if not validation["valid"]:
                raise ValueError(
                    "deterministic final report schema validation failed: "
                    + _json_content(validation["errors"][:10])
                )

            content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            primary = artifacts.commit(
                run_id=run_id,
                artifact_name="07_final_report.json",
                content=content,
                media_type="application/json",
                schema_name="final_report",
                idempotency_key=base_key,
                allow_supersede=allow_supersede,
            )
            markdown = render_final_report_markdown(report)
            mermaid = normalize_mermaid(report.get("mermaid_diagram"))
            commit_results = [primary]
            committed_artifacts = [primary["artifact"]]
            for name, companion_content, media_type, suffix in (
                ("07_final_report.md", markdown, "text/markdown", "markdown"),
                ("mechanism_routes.mmd", mermaid, "text/plain", "mermaid"),
            ):
                result = artifacts.commit(
                    run_id=run_id,
                    artifact_name=name,
                    content=companion_content,
                    media_type=media_type,
                    schema_name=None,
                    idempotency_key=f"{base_key}:{suffix}" if base_key else "",
                    allow_supersede=allow_supersede,
                )
                commit_results.append(result)
                committed_artifacts.append(result["artifact"])
            report_summary = {
                "run_id": report["run_id"],
                "review_decision": decision,
                "status": report["status"],
                "source_boundary": "保留审核结论及每项未经过 Wiki 核实的标签",
                "unmatched_capability_count": len(report["unmatched_capabilities"]),
                "top_cofactor": (
                    report["top_cofactor_classes"][0]["name"]
                    if report["top_cofactor_classes"]
                    else "unresolved"
                ),
                "top_enzyme_family": (
                    report["single_enzyme_candidates"][0]["name"]
                    if report["single_enzyme_candidates"]
                    else "unresolved"
                ),
                "top_route": (
                    report["top_routes"][0]["name"]
                    if report["top_routes"]
                    else "unresolved"
                ),
            }
            final_artifacts = [
                "06_critique.json",
                "07_final_report.json",
                "07_final_report.md",
                "mechanism_routes.mmd",
            ]
            if review_only:
                reentry.update(status="completed", review_decision=decision)
                artifacts._atomic_write(artifacts._run_path(run_id, "review_reentry.json"), _json_content(reentry))
            return {
                "committed": any(bool(item.get("committed")) for item in commit_results),
                "idempotent": all(bool(item.get("idempotent")) for item in commit_results),
                "artifact": primary["artifact"],
                "committed_artifacts": committed_artifacts,
                "artifact_refs": {
                    str(item["name"]): str(item["path"])
                    for item in committed_artifacts
                },
                "user_summary": user_summary,
                "report_summary": report_summary,
                # This receipt is the authoritative, bounded stage result.  It
                # prevents Markdown lists in user_summary (or other model prose)
                # from being mistaken for the stage's JSON object by the generic
                # Workflow parser.
                "stage_receipt": {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "review_decision": decision,
                    **({"revision_request": revision_request} if decision in {"REVISE", "PARTIAL_PASS"} else {}),
                    "final_artifacts": final_artifacts,
                    "top_summary": report_summary,
                    "user_summary": user_summary,
                },
            }

        return await self._call("确定性生成最终酶发现报告", operation)

    async def _run_snapshot(self, arguments: Mapping[str, Any], call: ToolCallContext) -> ToolOutput:
        return await self._call(
            "读取酶发现运行快照",
            lambda: self._artifact_store(call).snapshot(
                str(arguments.get("run_id") or ""),
                artifact_names=[
                    str(item)
                    for item in arguments.get("artifact_names") or []
                ],
            ),
        )


__all__ = ["EnzymeToolService"]
