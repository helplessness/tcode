"""通用的化学排序与定向修订规则；不包含目标酶、反应模板或知识答案。"""
from __future__ import annotations

from copy import deepcopy
import math
import hashlib
import json
import re
from .scientific_checks import BALANCE_PROPOSALS_SCHEMA

REVIEW_ISSUE_TYPES = ("chemical_conflict", "overstatement", "evidence_attribution", "ranking_inconsistency", "evidence_gap", "audit_delivery")
REVIEW_EVIDENCE_POLICY = {
    "issue_types": {
        "chemical_conflict": "明确的成断键、区域/立体、催化域、辅因子或电子收支矛盾；不能用低置信度放行。",
        "overstatement": "唯一、最优、必需、已验证等结论超出当前依据；修改当前断言，不等于酶或路线化学不成立。",
        "evidence_attribution": "把模型/用户知识、身份注释或其他反应的证据误作本候选目标活性证据；须纠正来源和适用范围。",
        "ranking_inconsistency": "同组比较方向、理由与实际化学分/排序矛盾；不是活性未知，须定向复核，不能只加免责声明通过，也不自动升降或排除候选。",
        "evidence_gap": "已诚实标注的科学未知；不是反证，可保留假说。",
        "audit_delivery": "原文/产物送达故障；不作化学反证。",
    },
    "revision_rule": "修订建议不是证据。优先删除无依据的绝对结论，保留可支持的化学理由与真实来源；不能以本轮已读卡、已表征、已证实等新断言替换旧断言。新增事实须有本次已读且内容匹配的原文，身份引文不证明目标活性，模型知识仍标未经过 Wiki 核实。不强制维持或改变评分，只有评分依据实质改变时重新评价。",
    "disposition_rule": "表述过度/证据归属错误必须修正，但不能仅凭这些标签自动删除候选或整条路线；真正化学矛盾仍定点修复或排除。只读历史不重写。",
}

RATIONALE_FIELD_CONTRACT = {
    "read_only": ["proposal_rationale", "original_rationale", "candidate_generation.draft"],
    "editable": ["support_reason", "conflict_reason", "ranking_reason", "ranking_factors", "knowledge_gap", "substrate_relation"],
    "rule": "原始提名理由是只读、未核实历史，不是当前事实或证据。审核当前 supports/contradictions/ranking_reason/knowledge_gaps 与原文是否一致；旧假设已被当前判断纠正时，不要求改写历史，也不单凭旧措辞排除候选。当前错误归属或化学矛盾仍必须修订。",
}


def require_editable_revision(task):
    """拒绝明确要求重写历史字段的任务；不自动改变审核决定或科学判断。"""
    text = str(task.get("instruction") or "")
    for match in re.finditer(r"proposal_rationale(?:\.fit_reason)?|original_rationale|candidate_generation[./]draft", text):
        start = max(text.rfind(char, 0, match.start()) for char in "。；;，,\n") + 1
        prefix = text[start:match.start()]
        edits = list(re.finditer(r"修改|改写|重写|覆盖|更新|rewrite|replace|\bedit\b|把", prefix, re.I))
        if not edits:
            continue
        target_prefix = prefix[edits[-1].end():]
        if any(field in target_prefix for field in RATIONALE_FIELD_CONTRACT["editable"]):
            continue  # 修改当前判断并对照历史，不是编辑历史。
        if edits[-1].group() == "把" and not re.search(r"改为|改成|改写|修改|更新|覆盖", text[match.end():match.end() + 160]):
            continue
        raise ValueError("revision target is read-only history; revise current support_reason/conflict_reason/ranking_reason/knowledge_gap instead, and keep the original proposal unchanged")

RANKING_POLICY = (
    "同一角色/功能内，先排除明确化学冲突，再按核心事件覆盖、底物/产物适配、"
    "选择性及物料闭合评价 chemistry_fit_score。未知不等于冲突。"
    "Wiki 核实程度、引用数量与检索分单独展示，不进入化学分；同分保持原顺序。"
    "相对排序理由用 ranking_factors.comparison 绑定同组 peer_ids 与 higher/equal/lower/unresolved；"
    "比较方向须与实际化学分一致，程序不从自然语言猜对象、不自动改分。"
)

SCREENING_SUBMISSION_POLICY = {
    "candidate_batch_size": 3, "route_batch_size": 2,
    "closing_turns": 6, "first_commit_remaining": 4,
    "instruction": "优先每批3个候选、最多2条完整短路线；余6轮停止新增方向，仅完成已声明步骤、必要原文读取与局部修正；余4轮必须首次正式提交，为可能的缺口取证交接及再次提交留窗口。已暂存完整时立即提交，不重发对象；总轮数不增加。",
}


MECHANISM_SUBMISSION_POLICY = {
    "max_steps": 16, "closing_turns": 6, "first_commit_remaining": 4,
    "instruction": "余6轮停止广泛检索，仅暂存已有机制、必要原文回读及提交；余4、2、1轮只接受正式提交。缺口保留未知，草稿不等于审核通过；不增加预算或重试。",
}


def mechanism_submission_window(completed_calls, max_steps=None):
    """在既有预算内交替安排首次提交、必要取证/修正和最后提交。"""
    remaining = max(1, (max_steps or MECHANISM_SUBMISSION_POLICY["max_steps"]) - completed_calls)
    phase = ("commit" if remaining in {MECHANISM_SUBMISSION_POLICY["first_commit_remaining"], 2, 1}
             else "closing" if remaining <= MECHANISM_SUBMISSION_POLICY["closing_turns"] else "explore")
    return {"phase": phase, "remaining_calls": remaining,
            "commit_required": phase == "commit", "instruction": MECHANISM_SUBMISSION_POLICY["instruction"]}


def screening_commit_due(remaining):
    """既有阶段预算内预留取证/修正间隔，不能等最后一轮才首次提交。"""
    return remaining in {SCREENING_SUBMISSION_POLICY["first_commit_remaining"], 2, 1}

RATIONALE_PROPERTIES = {
    "catalytic_events": {"type": "array", "maxItems": 3,
                         "items": {"type": "string", "minLength": 1, "maxLength": 160}},
    "route_role": {"enum": ["single_enzyme", "route_component", "alternative"]},
    "main_uncertainty": {"type": "string", "minLength": 1, "maxLength": 240},
}
RANKING_FACTORS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 3,
    "items": {"type": "object", "required": ["source", "statement"],
        "properties": {"source": {"enum": ["wiki", "model_knowledge", "inference"]},
                       "statement": {"type": "string", "minLength": 1, "maxLength": 96},
                       "refs": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1, "maxLength": 240}},
                       "quote": {"type": "string", "minLength": 3, "maxLength": 240},
                       "comparison": {"type": "object", "required": ["peer_ids", "relation"],
                           "properties": {"peer_ids": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                               "items": {"type": "string", "minLength": 1, "maxLength": 128}},
                               "relation": {"enum": ["higher", "equal", "lower", "unresolved"]},
                               "scope": {"enum": ["overall", "factor"], "description": "overall比较总适配分；factor只比较本条因素，不决定总名次。省略按overall兼容。"}},
                           "additionalProperties": False,
                           "description": "本条相对判断的同组对象及方向；复制候选/提名/卡/路线稳定ID，不用酶名。未知可填unresolved，不宣称相对名次。"}},
        "if": {"properties": {"source": {"const": "wiki"}}},
        "then": {"required": ["refs", "quote"]},
        "else": {"not": {"anyOf": [{"required": ["refs"]}, {"required": ["quote"]}]}},
        "additionalProperties": False},
    "description": "发生排序证据归属问题时逐项标来源，最多3条短依据。wiki 必须附已读原文 refs/quote；model_knowledge 与 inference 不填 Wiki 引用。程序生成带来源标签的 ranking_reason，不自动改分或验证化学事实。",
}
SCORE_PROPERTIES = {
    "ranking_factors": RANKING_FACTORS_SCHEMA,
    "chemistry_fit_score": {"type": "number", "minimum": 0, "maximum": 100,
                           "description": "纯目标化学适配分，不计 Wiki 命中或引用数量；省略时沿用 score。"},
    "ranking_reason": {"type": "string", "minLength": 1, "maxLength": 400},
}
BALANCE_SCHEMA = {
    "type": "object",
    "required": ["main_material_flow", "cosubstrate_accounting", "regeneration", "basis"],
    "properties": {
        "main_material_flow": {"enum": ["continuous", "gap", "unknown"]},
        "cosubstrate_accounting": {"enum": ["balanced", "incomplete", "unknown"]},
        "regeneration": {"enum": ["external_supply", "closed", "required_but_missing", "unspecified"]},
        "basis": {"type": "string", "minLength": 1, "maxLength": 600},
    }, "additionalProperties": False,
}
REVISION_TASK_SCHEMA = {
    "type": "object", "required": ["scope", "affected_ids", "instruction"],
    "properties": {
        "scope": {"enum": ["reaction", "mechanism", "screening", "audit", "delivery"]},
        "balance_proposals": BALANCE_PROPOSALS_SCHEMA,
        "affected_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                         "items": {"type": "string", "minLength": 1, "maxLength": 160}},
        "instruction": {"type": "string", "minLength": 1, "maxLength": 800,
                        "description": "仅改当前判断，不改只读提名历史。修订建议不是证据；删除无依据断言，不得凭措辞把模型知识或身份引用升级为Wiki目标活性事实。新增事实须匹配本次已读原文。"},
    }, "additionalProperties": False,
}
READ_CARD_REVIEW_SCHEMA = {
    "type": "object", "required": ["card_id", "decision", "reason"],
    "properties": {"card_id": {"type": "string", "minLength": 1, "maxLength": 80},
                   "decision": {"enum": ["considered", "analogy_only", "not_selected", "deferred"]},
                   "reason": {"type": "string", "minLength": 1, "maxLength": 240},
                   "comparison": {"type": "object", "required": ["shared_events", "target_gaps", "peer_ids", "basis"],
                       "properties": {
                           "shared_events": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 120}},
                           "target_gaps": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 120}},
                           "peer_ids": {"type": "array", "maxItems": 3, "uniqueItems": True, "items": {"type": "string"}},
                           'activation_inference': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                           'transfer_test': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                           'nomination_reason': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                           "basis": {"enum": ["target_conflict", "relative_fit", "coverage_limit"]}},
                       "additionalProperties": False}},
    "additionalProperties": False,
}


FEEDBACK_SCHEMA = {
    "type": "object", "required": ["candidate_ids", "mechanism_ids", "proposal", "reason", "evidence_refs"],
    "properties": {
        "candidate_ids": {"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                          "items": {"type": "string", "minLength": 1}},
        "mechanism_ids": {"type": "array", "maxItems": 3, "uniqueItems": True,
                          "items": {"type": "string", "pattern": "^M(?:10|[1-9])$"}},
        "proposal": {"type": "string", "minLength": 1, "maxLength": 600},
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
        "evidence_refs": {"type": "array", "maxItems": 6, "uniqueItems": True,
                          "items": {"type": "string", "minLength": 1}},
    }, "additionalProperties": False,
}


def chemistry_score(row):
    """旧 score 兼容为化学分；新提交可显式分开，拒绝非有限值。"""
    value = row.get("chemistry_fit_score", row.get("score", 0))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("chemistry_fit_score must be a finite number from 0 to 100")
    return float(value)


def chemistry_rank_key(row):
    """只使用已声明的明确冲突与化学分，不从未知、来源或引用数量推断冲突。"""
    checks = [row.get("chemistry_check") or {}, *[step.get("chemistry_check") or {} for step in row.get("steps", [])]]
    conflict = bool(row.get("hard_conflict")) or any(
        check.get(field) == "conflict" for check in checks
        for field in ("stereochemistry", "electron_balance", "bond_topology", "domain_assignment", "regioselectivity"))
    return (conflict, -chemistry_score(row))


def draft_rationale(item):
    """有界原始提名理由完整传递；它始终是待检验假说，不是证据。"""
    return deepcopy({key: item[key] for key in ("fit_reason", *RATIONALE_PROPERTIES) if key in item})


def feedback_candidate_ids(identifiers, candidates):
    """只把本次已选候选的精确卡号/提名号统一为最终 ID，不按名称猜身份。"""
    resolved = []
    for identifier in identifiers:
        matches = {row["candidate_id"] for row in candidates
                   if identifier in {row["candidate_id"], row.get("proposal_id"), *row.get("card_ids", [])}}
        if len(matches) != 1:
            raise ValueError("mechanism_feedback candidate reference must identify exactly one selected candidate: " + identifier)
        value = matches.pop()
        if value not in resolved:
            resolved.append(value)
    return resolved


def revision_owners(documents):
    """登记完整候选生命周期，而非只登记已经入选的蛋白；不据此证明身份等价。"""
    owners = {"reaction": {"01_reaction_analysis.json"},
              "mechanism": {"02_mechanisms.json", "03_capabilities.json"},
              "screening": {"04_candidates.json", "05_routes.json"},
              "audit": {"06_critique.json"}, "delivery": {"audit_delivery"}}
    for row in documents.get("02_mechanisms.json", {}).get("mechanism_hypotheses", []):
        owners["mechanism"].add(row["hypothesis_id"])
        owners["mechanism"].update(event["event_id"] for event in row.get("event_evidence", []) if event.get("event_id"))
        owners["screening"].update(row.get("knowledge_anchor_card_ids", []))
    candidate_document = documents.get("04_candidates.json", {})
    owners["screening"].update(row["card_id"] for row in candidate_document.get("read_card_reviews", []))
    owners['screening'].update(candidate_document.get('unassessed_read_cards', []))
    for row in candidate_document.get("enzyme_candidates", []):
        owners["screening"].update(str(row[key]) for key in ("candidate_id", "proposal_id") if row.get(key))
        owners["screening"].update(row.get("card_ids", []))
    generation = candidate_document.get("candidate_generation") or {}
    for field in ("draft", "prior_candidates", "dispositions"):
        owners["screening"].update(row["proposal_id"] for row in generation.get(field, []) if row.get("proposal_id"))
    for row in candidate_document.get("mechanism_feedback", []):
        owners["mechanism"].add(row["feedback_id"])
    for row in documents.get("05_routes.json", {}).get("routes", []):
        owners["screening"].add(row["route_id"])
    return owners


def normalize_revision_tasks(tasks, documents):
    """按精确 ID 的真实归属拆分任务；不让模型猜错 scope 导致再次提交。"""
    owners = revision_owners(documents)
    artifacts = {name for values in owners.values() for name in values if name.endswith(".json")}
    aliases = {alias: name for name in artifacts for alias in (name[:2], name[:-5])}
    normalized = []
    for task in deepcopy(tasks):
        require_editable_revision(task)
        grouped = {}
        for identifier in task["affected_ids"]:
            value = aliases.get(identifier, identifier)
            for prefix in ("candidate:", "route:", "mechanism:", "feedback:"):
                if value.startswith(prefix) and any(value[len(prefix):] in values for values in owners.values()):
                    value = value[len(prefix):]
                    break
            matches = [scope for scope, values in owners.items() if value in values]
            if not matches:
                examples = ", ".join(sorted(owners[task['scope']])[:8])
                raise ValueError(f"revision task scope={task['scope']}: unknown or foreign reference {identifier!r}; "
                                 f"use an existing ID or full artifact filename; valid examples: {examples}")
            if len(matches) != 1:
                raise ValueError(f"revision reference has ambiguous ownership: {identifier}")
            resolved = grouped.setdefault(matches[0], [])
            if value not in resolved:
                resolved.append(value)
        normalized.extend({**task, "scope": scope, "affected_ids": resolved} for scope, resolved in grouped.items())
    return normalized


def _targeted_downstream_gap(issue, tasks, owners):
    """证据缺口已有具体下游修复任务时，关联机制是影响范围，不自动变成改写目标。"""
    affected = set(issue.get("affected_ids", []))
    kind = issue.get("issue_type")
    targets = owners["screening"] if kind == "audit_delivery" else {
        ref for ref in owners["screening"] if not ref.endswith(".json")}
    return kind in {"evidence_gap", "audit_delivery", "overstatement", "evidence_attribution", "ranking_inconsistency"} and any(
        task["scope"] == "screening" and
        (set(task["affected_ids"]) & affected & targets)
        for task in tasks)


def prepare_revision_tasks(tasks, conflicts, documents, draft=None, review_actions=None):
    """补齐已报告重大冲突的编辑目标，保留原判断；不自动改科学结论或扩大重试。"""
    tasks = deepcopy(tasks)
    conflicts = list(conflicts)
    # 逐项处置本身也是编辑目标，不能因未重复填写 hard_conflicts 而回退整个机制阶段。
    for field, key in (("candidate_reviews", "candidate_id"), ("mechanism_reviews", "hypothesis_id"),
                       ("route_dispositions", "route_id")):
        for review in (review_actions or {}).get(field, []):
            if review.get("disposition") == "revise":
                conflicts.append({"severity": "major", "issue_type": "overstatement",
                    "affected_ids": [review[key]], "statement": review.get("reason") or "修订该对象的当前审核问题"})
    upstream = {key: documents[key] for key in ("01_reaction_analysis.json", "02_mechanisms.json", "03_capabilities.json") if key in documents}
    expected = hashlib.sha256(json.dumps(upstream, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    run_id = documents.get("04_candidates.json", {}).get("run_id")
    if draft and run_id and draft.get("run_id") == run_id and draft.get("upstream_digest") == expected:
        saved = set(draft.get("routes", {})) | set(draft.get("route_parts", {}))
        committed = {row["route_id"] for row in documents.get("05_routes.json", {}).get("routes", [])}
        for task in tasks:
            deferred = [ref for ref in task["affected_ids"] if ref in saved - committed]
            if deferred:
                # 只映射本运行/本上游真实保存的延后路线；最终任务仍引用已提交 artifact。
                task["affected_ids"] = list(dict.fromkeys("05_routes.json" if ref in deferred else ref for ref in task["affected_ids"]))
                task["instruction"] = "草稿路线目标：" + "、".join(deferred) + "。\n" + task["instruction"]
                if len(task["instruction"]) > 800:
                    raise ValueError("deferred route instruction exceeds existing maxLength=800; no text was dropped")
    normalized = normalize_revision_tasks(tasks, documents)
    owners = revision_owners(documents)
    order = ["reaction", "mechanism", "screening"]
    for issue in conflicts:
        if issue.get("severity") not in {"major", "critical"}:
            continue
        if _targeted_downstream_gap(issue, normalized, owners):
            continue
        for position, scope in enumerate(order):
            affected = sorted(set(issue.get("affected_ids", [])) & owners[scope])
            if not affected:
                continue
            if not any(task["scope"] in order[:position + 1] and (
                    task["scope"] != scope or set(task["affected_ids"]) & set(affected)
                    or any(ref.endswith(".json") for ref in task["affected_ids"])) for task in normalized):
                statement = str(issue.get("statement") or "").strip()
                if not statement or len(statement) > 800:
                    raise ValueError("upstream hard conflict needs a bounded explicit revision instruction")
                # 超过8个目标用该范围的完整 artifact 表示，不丢弃目标或冲突正文。
                targets = affected if len(affected) <= 8 else sorted(x for x in owners[scope] if x.endswith(".json"))
                normalized.append({"scope": scope, "affected_ids": targets, "instruction": statement})
            break  # 最早实际受影响阶段会使下游重新校验，不逐阶段制造冗余任务。
    # 拆分后的同范围任务可无损合并，但不扩大原有字段长度/数量限制。
    while len(normalized) > 5:
        merged = False
        for i, first in enumerate(normalized):
            for j in range(i + 1, len(normalized)):
                second = normalized[j]
                refs = list(dict.fromkeys([*first["affected_ids"], *second["affected_ids"]]))
                instruction = first["instruction"] + "\n" + second["instruction"]
                if first["scope"] == second["scope"] and len(refs) <= 8 and len(instruction) <= 800:
                    normalized[i] = {"scope": first["scope"], "affected_ids": refs, "instruction": instruction}
                    normalized.pop(j)
                    merged = True
                    break
            if merged:
                break
        if not merged:
            break
    if len(normalized) > 5:
        raise ValueError("normalized revision tasks exceed existing maxItems=5; no instruction was dropped")
    return normalized


def revision_target(critique, documents=None):
    """按显式问题范围选最早必要阶段；旧审核保守回到机制，交付故障不重跑化学。"""
    if critique.get("review_decision") == "PASS":
        return None
    body = critique.get("critique", critique)
    tasks = body.get("revision_tasks") or []
    if not tasks:
        return "mechanism_and_capability"
    stages = {"reaction": "reaction_analysis", "mechanism": "mechanism_and_capability",
              "screening": "family_screening_and_routes", "audit": "critical_report"}
    order = list(stages)
    if documents is not None:
        owners = revision_owners(documents)
        tasks = normalize_revision_tasks(tasks, documents)
        # 不能把已定位到上游的重大问题伪装成只改审核措辞。
        for issue in body.get("hard_conflicts", []):
            if issue.get("severity") not in {"major", "critical"}:
                continue
            if _targeted_downstream_gap(issue, tasks, owners):
                continue
            for scope in order[:3]:
                if set(issue.get("affected_ids", [])) & owners[scope] and not any(
                        task["scope"] in order[:order.index(scope) + 1] for task in tasks):
                    raise ValueError("revision tasks do not cover an upstream hard conflict")
    active = [scope for scope in order if any(task["scope"] == scope for task in tasks)]
    return stages[active[0]] if active else None

