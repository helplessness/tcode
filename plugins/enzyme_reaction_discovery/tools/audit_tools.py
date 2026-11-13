"""只核对本次实际引用的已读证据，不重新搜索或挑每卡第一条反应。"""
from __future__ import annotations
from .reasoning_policy import draft_rationale, RATIONALE_FIELD_CONTRACT, REVIEW_ISSUE_TYPES, REVIEW_EVIDENCE_POLICY
from .screening_tools import exclusion_review_tasks, read_card_exclusion_review
from .scientific_checks import scientific_review

import copy
import hashlib
import re
from typing import Any, Mapping
from .retrieval_tools import canonical

EVIDENCE_PAGE_CHARS = 12500  # 原文分页额度独立于阶段摘要，不截断证据正文。
AUDIT_SUMMARY_CHARS = 42000  # 为同页已读原文保留空间，而非扩大模型上下文。
AUDIT_CONTEXT_PROTOCOL = 20


def review_id_aliases(documents):
    """审核只解析当前产物中唯一的机器 ID；不按蛋白名或语义相似度认人。"""
    aliases = {}
    for row in documents.get('04_candidates.json', {}).get('enzyme_candidates', []):
        keys = {row.get(key) for key in ('candidate_id', 'proposal_id', 'card_id')} | set(row.get('card_ids', []))
        prefix = str(row.get('proposal_id') or '').split('_', 1)[0]
        if re.fullmatch(r'[A-Za-z]+[0-9]+', prefix):
            keys.add(prefix)  # 如 P1_... 的 P1；必须在当前候选全集中唯一才可使用。
        for key in keys - {None, ''}:
            aliases.setdefault(key, set()).add(row['candidate_id'])
    return ({key: next(iter(values)) for key, values in aliases.items() if len(values) == 1},
            {key: sorted(values) for key, values in aliases.items() if len(values) > 1})


def review_object_mentions(row, documents):
    """同一正式审核内显式写出的稳定 ID 与其唯一别名统一关联，不生成新证据。"""
    aliases, _ambiguous = review_id_aliases(documents)
    refs = set(row.get('affected_ids', [])) | set(row.get('evidence_refs', []))
    text = str(row.get('reason') or row.get('statement') or '')
    refs.update(alias for alias in aliases if re.search(
        r'(?<![A-Za-z0-9_.-])' + re.escape(alias) + r'(?![A-Za-z0-9_.-])', text))
    canonical = {aliases[ref] for ref in refs if ref in aliases}
    return refs | canonical | {alias for alias, target in aliases.items() if target in canonical}


def deterministic_review_receipt(documents):
    """可重复交付的小型程序事实索引；不替代语义审核，不修改分数、角色或处置。"""
    checks = scientific_review(documents)
    aliases, ambiguous = review_id_aliases(documents)
    ranking = checks['ranking_consistency']
    unresolved = {}
    for row in ranking:
        if row['status'] not in {'consistent', 'factor_specific'}:
            unresolved.setdefault(row['id'], set()).add(row['status'])
    return {
        'source_digest': audit_digest({'documents': documents}),
        'id_aliases': {key: value for key, value in aliases.items() if key != value},
        'ambiguous_aliases': ambiguous,
        'ranking_checks': {'checked': len(ranking),
            'consistent': sum(row['status'] == 'consistent' for row in ranking),
            'factor_specific': sum(row['status'] == 'factor_specific' for row in ranking),
            'unresolved': [{'id': key, 'statuses': sorted(values)} for key, values in unresolved.items()]},
        'route_accounting': [{key: row.get(key) for key in
            ('route_id', 'status', 'delivery_source', 'missing_equation_steps', 'unknown_formulas')}
            for row in checks['stoichiometry']],
        'read_card_review_tasks': [{key: row.get(key) for key in
            ('card_id', 'decision', 'comparison_peer_ids', 'compare_candidate_ids')}
            for row in read_card_exclusion_review(documents.get('04_candidates.json', {}))],
        'rule': 'ID别名已按当前产物精确解析；不要求模型改写为长ID。missing_equation_steps=[]表示步骤方程已交付，unknown不是文件缺失。'
                'consistent仅指比较方向和分数组别一致，不证明化学排序合理；算术通过不证明机制、活性或选择性。'
                '仍须审核真实化学问题，并显式核对read_card_review_tasks中的卡片与实际保留的比较对象，不强制提名。'}


def audit_context_complete(state):
    """PASS 门控与最终限制说明共用同一份当前交付状态。"""
    return (state.get("context_protocol") == AUDIT_CONTEXT_PROTOCOL
            and state.get("context_total") is not None
            and state.get("context_delivered", 0) == state["context_total"])


def read_audit_section(store, snapshot, section, query='', offset=0):
    """回读本运行已交付的审核字段，不重新分页、不新增证据授权。"""
    state = store._read_json_if_exists(store._run_path(snapshot['run']['run_id'], 'report_evidence_audit.json')) or {}
    if state.get('digest') != audit_digest(snapshot) or state.get('context_protocol') != AUDIT_CONTEXT_PROTOCOL:
        return {'found': False, 'request_status': 'context_required',
                'instruction': '当前审核缓存不可用，先调用 enzyme_report_context，不要猜资源名。'}
    data = dict(state.get('pages', {}).get('0', {}))
    previous = store._read_json_if_exists(store._run_path(snapshot['run']['run_id'], '06_critique.json')) or {}
    data['previous_review'] = {'historical_only': True, 'review': previous}
    if section not in {'review_context', 'scientific_checks', 'previous_review'}:
        raise ValueError('unknown audit section')
    if section != 'review_context':
        data = data.get(section, {})
    if query == section:
        query = ''
    if query:
        original = None
        if section == 'review_context':
            for file, collection, keys in (
                ('04_candidates.json','enzyme_candidates',('candidate_id','proposal_id')),
                ('05_routes.json','routes',('route_id',)),
                ('02_mechanisms.json','mechanism_hypotheses',('hypothesis_id',)),
                ('03_capabilities.json','catalytic_capabilities',('capability_id',)),
                ('04_candidates.json','read_card_reviews',('card_id',))):
                matches = [row for row in snapshot['documents'].get(file, {}).get(collection, [])
                           if any(row.get(key) == query for key in keys)]
                if len(matches) == 1:
                    original = matches[0]
                    break
        if original is not None:
            data = original
        elif not isinstance(data, dict) or query not in data:
            return {'found': False, 'resource': section, 'available_fields': list(data) if isinstance(data, dict) else [],
                    'instruction': '选择已列字段，未命中不是工具故障，不重复同一查询。'}
        else:
            data = data[query]
    text = canonical(data)
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid audit section offset')
    if offset > len(text):
        return {'found': False, 'resource': section, 'query': query, 'object_length': len(text), 'suggested_offset': 0}
    end = min(len(text), offset + 6000)
    return {'found': True, 'resource': section, 'query': query, 'offset': offset, 'text': text[offset:end],
            'next_offset': end if end < len(text) else None, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'next_audit_page': state.get('pages', {}).get(str(len(state.get('pages', {}))-1), {}).get('evidence_audit', {}).get('next_page'),
            'scope': '已交付审核上下文回读；旧审核仅供对照，不代替当前科学审查'}


def _audit_text(value: Any, limit: int = 160) -> str:
    """审计视图的短文本；原始阶段产物不在这里被修改。"""
    text = str(value or "").replace("\x00", " ").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _audit_strings(value: Any, *, count: int = 3, chars: int = 140) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        text for text in (_audit_text(item, chars) for item in value[:count]) if text
    ))


def _audit_check(value: Any) -> dict[str, Any]:
    """保留化学门控状态，压缩可重复的解释正文。"""
    raw = dict(value) if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("bond_topology", "domain_assignment", "regioselectivity",
                "stereochemistry", "electron_balance", "catalyst_scope"):
        if key in raw:
            result[key] = _audit_text(raw[key], 48)
    if raw.get("basis"):
        result["basis"] = _audit_text(raw["basis"], 140)
    for key in ("broken_bonds", "retained_bonds"):
        if raw.get(key):
            result[key] = _audit_strings(raw[key], count=2, chars=80)
    return result


def _compact_audit_base(base: Mapping[str, Any]) -> dict[str, Any]:
    """把 01—05 的审计摘要压成稳定预算；证据正文仍由分页通道完整交付。"""
    raw = dict(base)
    input_data = dict(raw.get("input") or {})
    input_context = dict(input_data.get("context") or {})
    notes = input_context.pop("notes", [])
    if notes:
        input_context["notes_omitted"] = len(notes)
    compact_input = {
        key: copy.deepcopy(input_data.get(key))
        for key in ("reaction_id", "schema_version", "user_confirmed", "component_assumptions")
        if key in input_data
    }
    compact_input["reactants"] = [
        {key: _audit_text(item.get(key), 500 if key == "smiles" else 100)
         for key in ("name", "role", "smiles", "stoichiometry") if item.get(key) is not None}
        for item in input_data.get("reactants", [])[:8] if isinstance(item, Mapping)
    ]
    compact_input["products"] = [
        {key: _audit_text(item.get(key), 500 if key == "smiles" else 100)
         for key in ("name", "role", "smiles", "stoichiometry") if item.get(key) is not None}
        for item in input_data.get("products", [])[:8] if isinstance(item, Mapping)
    ]
    compact_input["context"] = input_context

    mechanisms = []
    for item in raw.get("mechanisms", [])[:8]:
        if not isinstance(item, Mapping):
            continue
        bridge = dict(item.get("counterfactual_bridge") or {})
        mechanisms.append({
            key: _audit_text(item.get(key), 180 if key == "summary" else 100)
            for key in ("hypothesis_id", "evidence_basis", "verification_note", "route_type", "confidence", "summary")
            if item.get(key) is not None
        } | {
            "required_events": _audit_strings(item.get("required_events"), count=3, chars=100),
            "event_evidence": [
                {
                    "event_id": _audit_text(event.get("event_id"), 60),
                    "status": _audit_text(event.get("status"), 40),
                    "event": _audit_text(event.get("event"), 100),
                    "reason": _audit_text(event.get("reason"), 80),
                    "wiki_refs": _audit_strings(event.get("wiki_refs"), count=1, chars=90),
                }
                for event in item.get("event_evidence", [])[:3] if isinstance(event, Mapping)
            ],
            "counterfactual_bridge": {
                key: (_audit_strings(value, count=2, chars=100) if isinstance(value, list)
                      else _audit_text(value, 100))
                for key, value in bridge.items()
                if key in {"source", "target_activity_status", "native_termination",
                           "target_alternative_termination", "unsupported_assumptions"}
            },
            "key_intermediates": _audit_strings(item.get("key_intermediates"), count=3, chars=80),
            "supports": _audit_strings(item.get("supports"), count=1, chars=100),
            "contradictions": _audit_strings(item.get("contradictions"), count=1, chars=100),
            "falsifiers": _audit_strings(item.get("falsifiers"), count=1, chars=100),
            "unknowns": _audit_strings(item.get("unknowns"), count=1, chars=100),
        })

    candidates = []
    for item in raw.get("candidates", [])[:30]:
        if not isinstance(item, Mapping):
            continue
        row = {
            key: _audit_text(item.get(key), 140 if key in {"name_evidence", "native_reaction_quote"} else 100)
            for key in ("candidate_id", "name_basis", "name_evidence", "candidate_role", "component_function",
                        "substrate_relation", "native_reaction_quote", "enzyme_family", "enzyme_name",
                        "candidate_kind", "identity_level", "evidence_scope", "verification_note", "confidence",
                        "engineering_plausibility") if item.get(key) is not None
        }
        row.update({
            "proposal_rationale": copy.deepcopy(item.get("proposal_rationale", {})),
            "proposal_rationale_status": "historical_unverified",
            "chemistry_fit_score": item.get("chemistry_fit_score", item.get("score")),
            "ranking_reason": item.get("ranking_reason", ""),
            **({"prior_specific_name": item["prior_specific_name"]} if item.get("prior_specific_name") else {}),
            **({"ranking_factors": copy.deepcopy(item["ranking_factors"])} if "ranking_factors" in item else {}),
            "score": item.get("score"), "hard_conflict": bool(item.get("hard_conflict")),
            "knowledge_gaps": _audit_strings(item.get("knowledge_gaps"), count=1, chars=100),
            "card_ids": _audit_strings(item.get("card_ids"), count=3, chars=64),
            "cofactor_classes": _audit_strings(item.get("cofactor_classes"), count=2, chars=60),
            "mechanism_ids": _audit_strings(item.get("mechanism_ids"), count=6, chars=32),
            "supports": _audit_strings(item.get("supports"), count=1, chars=400),
            "contradictions": _audit_strings(item.get("contradictions"), count=1, chars=100),
            "evidence_refs": _audit_strings(item.get("evidence_refs"), count=1, chars=90),
            "contradiction_refs": _audit_strings(item.get("contradiction_refs"), count=1, chars=90),
        })
        candidates.append(row)

    routes = []
    for item in raw.get("routes", [])[:8]:
        if not isinstance(item, Mapping):
            continue
        steps = []
        for step in item.get("steps", [])[:8]:
            if not isinstance(step, Mapping):
                continue
            steps.append({
                "step": step.get("step"),
                **{key: copy.deepcopy(step[key]) for key in ("step_kind", "condition_note") if key in step},
                **{key: _audit_text(step.get(key), 120) for key in
                   ("candidate_id", "intended_proposal_id", "candidate_family", "enzyme_name", "candidate_kind",
                    "identity_level", "evidence_scope", "verification_note") if step.get(key) is not None},
                "input": _audit_text(step.get("input"), 80),
                "output": _audit_text(step.get("output"), 80),
                "material_handoff": _audit_text(step.get("material_handoff"), 90),
                "substitution_basis": _audit_text(step.get("substitution_basis"), 100),
                "substrate_analogy": _audit_text(step.get("substrate_analogy"), 180),
                "chemistry_check": _audit_check(step.get("chemistry_check")),
                "evidence_refs": _audit_strings(step.get("evidence_refs"), count=1, chars=90),
                "mechanism_ids": _audit_strings(step.get("mechanism_ids"), count=3, chars=32),
                "cofactor_classes": _audit_strings(step.get("cofactor_classes"), count=2, chars=60),
                "card_ids": _audit_strings(step.get("card_ids"), count=2, chars=64),
            })
        routes.append({
            **({"ranking_factors": copy.deepcopy(item["ranking_factors"])} if "ranking_factors" in item else {}),
            **({"stoichiometry": copy.deepcopy(item["stoichiometry"])} if item.get("stoichiometry") else {}),
            **({"balance_review": copy.deepcopy(item["balance_review"])} if item.get("balance_review") else {}),
            "route_id": _audit_text(item.get("route_id"), 80),
            "route_type": _audit_text(item.get("route_type"), 60),
            "chemistry_fit_score": item.get("chemistry_fit_score", item.get("score")),
            "ranking_reason": item.get("ranking_reason", ""),
            "score": item.get("score"), "confidence": _audit_text(item.get("confidence"), 30),
            "steps": steps,
            "support_refs": _audit_strings(item.get("support_refs"), count=1, chars=90),
            "contradiction_refs": _audit_strings(item.get("contradiction_refs"), count=1, chars=90),
            "unsupported_intermediates": _audit_strings(item.get("unsupported_intermediates"), count=3, chars=100),
        })

    generation = dict(raw.get("candidate_generation") or {})
    compact_generation = {
        "draft_status": "historical_unverified",
        "mode": _audit_text(generation.get("mode"), 80),
        "draft_count": generation.get("draft_count"),
        "draft_digest": _audit_text(generation.get("draft_digest"), 80),
        "prior_candidates_count": len(generation.get("prior_candidates", [])),
        "prior_candidates": [{key: _audit_text(item.get(key), 120) for key in
                              ("proposal_id", "enzyme_name", "specific_name", "identity_level") if item.get(key)}
                             for item in generation.get("prior_candidates", [])[:30]],
        "ranking_policy": _audit_text(generation.get("ranking_policy"), 160),
        "draft": [
            {**{key: _audit_text(item.get(key), 140) for key in
             ("proposal_id", "enzyme_name", "identity_level") if item.get(key) is not None},
             **draft_rationale(item)}
            for item in generation.get("draft", [])[:8] if isinstance(item, Mapping)
        ],
        "dispositions": [
            {key: _audit_text(item.get(key), 140) for key in
             ("proposal_id", "decision", "replacement_id", "reason", "identity_basis", "scope_basis", "exclusion_basis", "identity_issue")
             if item.get(key) is not None}
            for item in generation.get("dispositions", [])[:30] if isinstance(item, Mapping)
        ],
    }

    flags = dict(raw.get("audit_flags") or {})
    compact_flags = {
        **{key: copy.deepcopy(flags.get(key, [])) for key in
           ("read_card_reviews", "unassessed_read_cards", "mechanism_link_review", "read_card_exclusion_review")},
        "candidate_role_review": copy.deepcopy(flags.get("candidate_role_review", [])),
        "role_transfer_rule": flags.get("role_transfer_rule", ""),
        "rationale_field_contract": copy.deepcopy(RATIONALE_FIELD_CONTRACT),
        "review_evidence_policy": copy.deepcopy(REVIEW_EVIDENCE_POLICY),
        "candidate_exclusion_review": copy.deepcopy(flags.get("candidate_exclusion_review", [])),
        "balance_rule": flags.get("balance_rule", ""),
        "mechanism_route_coverage": copy.deepcopy(flags.get("mechanism_route_coverage", []))[:10],
        "mechanism_feedback": copy.deepcopy(flags.get("mechanism_feedback", [])),
        "ranking_policy": flags.get("ranking_policy", ""),
        "specific_enzyme_discovery": copy.deepcopy(flags.get("specific_enzyme_discovery", []))[:10],
        "named_candidate_identity_review": [
            {key: _audit_text(item.get(key), 120) for key in
             ("candidate_id", "enzyme_name", "name_basis", "name_evidence") if item.get(key) is not None}
            for item in flags.get("named_candidate_identity_review", [])[:12] if isinstance(item, Mapping)
        ],
        "candidate_mechanism_review": [
            {"candidate_id": _audit_text(item.get("candidate_id"), 80),
             "mechanism_ids": _audit_strings(item.get("mechanism_ids"), count=6, chars=32),
             "cofactor_classes": _audit_strings(item.get("cofactor_classes"), count=3, chars=80)}
            for item in flags.get("candidate_mechanism_review", [])[:12] if isinstance(item, Mapping)
        ],
        # 路线详情已经给出完整门控；这里仅保留索引，避免同一段说明被复制两次。
        "route_catalysts_to_review": [
            {"route_id": _audit_text(item.get("route_id"), 80), "step": item.get("step"),
             "candidate_id": _audit_text(item.get("candidate_id"), 100),
             "intended_proposal_id": _audit_text(item.get("intended_proposal_id"), 100),
             "chemistry_status": _audit_text((item.get("chemistry_check") or {}).get("catalyst_scope"), 32),
             "evidence_refs": _audit_strings(item.get("evidence_refs"), count=1, chars=90)}
            for item in flags.get("route_catalysts_to_review", [])[:32] if isinstance(item, Mapping)
        ],
        "capability_reuse_review": _audit_strings(flags.get("capability_reuse_review"), count=8, chars=80),
        "uncompared_mechanism_anchors": _audit_strings(flags.get("uncompared_mechanism_anchors"), count=12, chars=64),
        "unassessed_draft_candidates": _audit_strings(flags.get("unassessed_draft_candidates"), count=12, chars=80),
        "candidate_merges_to_review": [
            {key: _audit_text(item.get(key), 120) for key in
             ("proposal_id", "replacement_id", "identity_basis", "scope_basis", "reason") if item.get(key) is not None}
            for item in flags.get("candidate_merges_to_review", [])[:8] if isinstance(item, Mapping)
        ],
        "hard_conflict_candidates": _audit_strings(flags.get("hard_conflict_candidates"), count=12, chars=80),
        "routes_with_unsupported_intermediates": _audit_strings(flags.get("routes_with_unsupported_intermediates"), count=8, chars=80),
    }
    result = {
        key: copy.deepcopy(raw.get(key)) for key in
        ("run_id", "plugin_version", "knowledge_version", "knowledge_fingerprint", "result_limits", "reaction_cautions", "committed_inputs", "revision_targets", "submission_review", "scientific_checks", "deterministic_review_receipt")
        if key in raw
    }
    result.update(input=compact_input, mechanisms=mechanisms, cofactors=copy.deepcopy(raw.get("cofactors") or [])[:3],
                  candidate_generation=compact_generation, candidates=candidates, routes=routes,
                  card_audit=copy.deepcopy(raw.get("card_audit") or {}), audit_flags=compact_flags)
    return result


def _minimal_audit_base(base: Mapping[str, Any]) -> dict[str, Any]:
    """极端长产物的安全降级包：保留全部实体 ID 与门控状态，不能据此 PASS。"""
    result = {key: copy.deepcopy(base.get(key)) for key in
              ("run_id", "plugin_version", "knowledge_version", "knowledge_fingerprint", "result_limits", "revision_targets", "submission_review", "scientific_checks", "deterministic_review_receipt")
              if key in base}
    input_data = dict(base.get("input") or {})
    result["input"] = {
        "reaction_id": input_data.get("reaction_id"),
        "reactants": [{"smiles": _audit_text(item.get("smiles"), 320)}
                      for item in input_data.get("reactants", [])[:8] if isinstance(item, Mapping)],
        "products": [{"smiles": _audit_text(item.get("smiles"), 320)}
                     for item in input_data.get("products", [])[:8] if isinstance(item, Mapping)],
    }
    result["mechanisms"] = [
        {"hypothesis_id": _audit_text(item.get("hypothesis_id"), 60),
         "confidence": _audit_text(item.get("confidence"), 24),
         "summary": _audit_text(item.get("summary"), 80),
         "required_events": _audit_strings(item.get("required_events"), count=2, chars=70),
         "event_evidence": [{"event_id": _audit_text(event.get("event_id"), 48),
                             "status": _audit_text(event.get("status"), 32),
                             "wiki_refs": _audit_strings(event.get("wiki_refs"), count=1, chars=72)}
                            for event in item.get("event_evidence", [])[:2] if isinstance(event, Mapping)]}
        for item in base.get("mechanisms", [])[:8] if isinstance(item, Mapping)
    ]
    result["candidates"] = [
        {"candidate_id": _audit_text(item.get("candidate_id"), 64),
         "enzyme_name": _audit_text(item.get("enzyme_name"), 96),
         "enzyme_family": _audit_text(item.get("enzyme_family"), 80),
         "candidate_kind": _audit_text(item.get("candidate_kind"), 32),
         "identity_level": _audit_text(item.get("identity_level"), 24),
         "candidate_role": _audit_text(item.get("candidate_role"), 32),
         "score": item.get("score"), "confidence": _audit_text(item.get("confidence"), 24),
         "hard_conflict": bool(item.get("hard_conflict")),
         "cofactor_classes": _audit_strings(item.get("cofactor_classes"), count=2, chars=50),
         "mechanism_ids": _audit_strings(item.get("mechanism_ids"), count=4, chars=32),
         "evidence_refs": _audit_strings(item.get("evidence_refs"), count=1, chars=72),
         "contradiction_refs": _audit_strings(item.get("contradiction_refs"), count=1, chars=72)}
        for item in base.get("candidates", [])[:12] if isinstance(item, Mapping)
    ]
    result["routes"] = [
        {"route_id": _audit_text(item.get("route_id"), 64),
         "route_type": _audit_text(item.get("route_type"), 32),
         "score": item.get("score"), "confidence": _audit_text(item.get("confidence"), 24),
         "steps": [
             {"step": step.get("step"), "candidate_id": _audit_text(step.get("candidate_id"), 64),
              **{key: copy.deepcopy(step[key]) for key in ("step_kind", "condition_note") if key in step},
              "input": _audit_text(step.get("input"), 60), "output": _audit_text(step.get("output"), 60),
              "chemistry_check": {key: _audit_text(value, 24) for key, value in
                                  _audit_check(step.get("chemistry_check")).items()
                                  if key not in {"basis", "broken_bonds", "retained_bonds"}},
              "evidence_refs": _audit_strings(step.get("evidence_refs"), count=1, chars=72)}
             for step in item.get("steps", [])[:8] if isinstance(step, Mapping)
         ]}
        for item in base.get("routes", [])[:8] if isinstance(item, Mapping)
    ]
    result["audit_flags"] = {
        **{key: copy.deepcopy((base.get("audit_flags") or {}).get(key, [])) for key in
           ("read_card_reviews", "unassessed_read_cards", "mechanism_link_review", "read_card_exclusion_review")},
        "candidate_role_review": copy.deepcopy((base.get("audit_flags") or {}).get("candidate_role_review", [])),
        "role_transfer_rule": (base.get("audit_flags") or {}).get("role_transfer_rule", ""),
        "rationale_field_contract": copy.deepcopy(RATIONALE_FIELD_CONTRACT),
        "review_evidence_policy": copy.deepcopy(REVIEW_EVIDENCE_POLICY),
        "mechanism_route_coverage": copy.deepcopy((base.get("audit_flags") or {}).get("mechanism_route_coverage", []))[:10],
        "specific_enzyme_discovery": copy.deepcopy((base.get("audit_flags") or {}).get("specific_enzyme_discovery", []))[:10],
        "hard_conflict_candidates": _audit_strings((base.get("audit_flags") or {}).get("hard_conflict_candidates"), count=12, chars=64),
        "unassessed_draft_candidates": _audit_strings((base.get("audit_flags") or {}).get("unassessed_draft_candidates"), count=12, chars=64),
        "routes_with_unsupported_intermediates": _audit_strings((base.get("audit_flags") or {}).get("routes_with_unsupported_intermediates"), count=8, chars=64),
    }
    return result


def engineering_assertions(documents):
    """提取疑似无条件改造断言，供语义审核参考；词法命中本身不是科学冲突。"""
    obligation = re.compile(r"必须[^。！？;；，,\n]{0,12}?(?:改造|工程化|突变)|(?:需(?:要)?|须)(?:通过)?(?:蛋白)?改造|只能[^。！？;；，,\n]{0,12}?工程化|\b(?:must|requires?|needs?)\b[^.!?;，,\n]{0,30}?(?:engineering|mutat|engineer)", re.I)
    qualified = re.compile(r"可能|或许|是否|未证明|不能认定|不能断定|不等于|无需|不需|不必|不得|禁止|不应|不要|不可|并非|未必|may|might|whether|not proven|does not|must not|not require", re.I)
    findings = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in {"proposal_rationale", "original_rationale", "candidate_generation",
                               "native_reaction_quote", "user_input_quote", "source_refs", "evidence_refs", "wiki_refs", "knowledge_anchor_refs", "contradiction_refs", "support_refs"}:
                    visit(item, path + "." + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, str):
            for match in obligation.finditer(value):
                prefix = re.split(r"[。！？;；，,\n]|但是|然而|但|\bbut\b|\bhowever\b", value[max(0, match.start() - 160):match.start()], flags=re.I)[-1]
                conditional = re.search(r"仅当|只有|若.+才|如果.+才|可选|非必须|非必需|不构成|only if|optional", prefix + match.group(), re.I)
                # 否定断言或证据不等于肯定其宾语；只看当前分句，避免跨句消除真实断言。
                negated_claim = re.search(
                    r"(?:不|未|没有|不能|无法|不应|不可|尚不能|尚未)(?:再|应当|能|可)?"
                    r"(?:声称|宣称|断言|主张|认定|认为|证明|证实|表明|说明|推断|判断|得出|视为|要求)"
                    r"|(?:没有|尚无|缺乏|未有).{0,12}(?:证据|依据)"
                    r"|(?:do(?:es)? not|cannot|can't|no evidence|not established).{0,40}$",
                    prefix, re.I)
                if not qualified.search(prefix + match.group()) and not conditional and not negated_claim:
                    findings.append({"path": path, "statement": value[max(0, match.start() - 50):match.end() + 90]})
                    break

    for name, document in documents.items():
        if name.startswith(("02_", "03_", "04_", "05_")):
            visit(document, name)
    return findings[:12]


def audit_digest(snapshot):
    """仅绑定待审核的 01—05，提交 06 不会使本次证据审计失效。"""
    documents = {k: v for k, v in snapshot["documents"].items()
                 if k.startswith(("01_", "02_", "03_", "04_", "05_"))}
    return hashlib.sha256(canonical(documents).encode()).hexdigest()


def require_complete_audit(store, snapshot):
    """材料不齐不能正式 PASS；科学未知与证据交付故障分开处理。"""
    state = store._read_json_if_exists(store._run_path(snapshot["run"]["run_id"], "report_evidence_audit.json"))
    expected = {ref for refs in actual_references(snapshot["documents"], reviewed_card_read_refs(store, snapshot)).values() for ref in refs}
    if not state or state.get("digest") != audit_digest(snapshot):
        raise ValueError("audit_not_current: call enzyme_report_context page=0 before PASS")
    from .incremental_review import evidence_state
    _, delivered_evidence = evidence_state(store, snapshot)
    current_evidence_digest = hashlib.sha256(canonical({ref: delivered_evidence.get(ref) for ref in sorted(expected)}).encode()).hexdigest()
    if state.get('evidence_digest') != current_evidence_digest:
        raise ValueError('audit evidence inheritance invalid; read full current context again')
    if state.get('incremental_review'):
        from .incremental_review import load_basis, evidence_state, digest
        proof = state['incremental_review']
        basis = load_basis(store, snapshot, proof['basis_id'], bind_active=False)
        _, current_evidence = evidence_state(store, snapshot)
        if (not basis or proof.get('current_evidence_digest') != digest({ref:current_evidence.get(ref) for ref in sorted(expected)})
                or any(ref not in current_evidence or basis['evidence'].get(ref) != current_evidence[ref]
                            for ref in proof['inherited_refs'])):
            raise ValueError('incremental audit inheritance invalid; read full current context again')
    missing = expected - set(state.get("delivered", []))
    if missing:
        next_page = state["pages"][str(max(map(int, state["pages"])))]["evidence_audit"]["next_page"]
        raise ValueError(f"audit_incomplete: {len(missing)} refs not delivered; next_page={next_page}; no formal PASS")
    if not audit_context_complete(state):
        raise ValueError("audit_context_incomplete: read remaining upstream context pages before PASS")


def _audit_segments(value):
    """按完整小对象无损分块，避免每个字面量重复长路径；重复对象仍合并。"""
    grouped = {}

    def visit(item, path):
        encoded = canonical(item)
        if len(encoded) <= 3000:
            grouped.setdefault(encoded, []).append(path)
        elif isinstance(item, dict) and item:
            for key, child in item.items():
                visit(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(item, list) and item:
            for index, child in enumerate(item):
                visit(child, path + "/" + str(index))
        else:
            # 重复支持正文、账本与引用仅送达一次，保留全部归属路径。
            grouped.setdefault(encoded, []).append(path)

    visit(value, "")
    segments = []
    for encoded, paths in grouped.items():
        for offset in range(0, len(encoded), 3000):
            for start in range(0, len(paths), 12):
                segments.append({"paths": paths[start:start + 12], "json_offset": offset,
                                 "json_total": len(encoded), "json_text": encoded[offset:offset + 3000]})
    return segments


def _incremental_segments(value):
    """增量包按完整值分块，避免为每个字面量重复长 JSON Pointer。原文不截断。"""
    return [{'paths':['/'+key], 'json_offset':offset, 'json_total':len(encoded),
             'json_text':encoded[offset:offset+3000]}
            for key,item in value.items() for encoded in [canonical(item)]
            for offset in range(0,len(encoded),3000)]


def review_issue_type(issue):
    """审核交付失败不能作为化学反证；兼容旧审核的稳定引用标识。"""
    explicit = issue.get("issue_type")
    if explicit in REVIEW_ISSUE_TYPES:
        return explicit
    refs = issue.get("evidence_refs", [])
    if refs and any(str(ref).startswith("audit_compaction") for ref in refs) and all(
            str(ref).startswith(("audit_compaction", "evidence_audit")) for ref in refs):
        return "audit_delivery"
    return "chemical_conflict"


def require_read_card_review(documents, critique):
    """负向回读处置是整体审核责任，不能在部分交付投影中被过滤掉。"""
    missing = []
    for task in read_card_exclusion_review(documents.get("04_candidates.json", {})):
        compared = any(task["card_id"] in issue.get("affected_ids", [])
            and (not task["compare_candidate_ids"] or set(task["compare_candidate_ids"]) & review_object_mentions(issue, documents))
            and str(issue.get("statement") or "").strip()
            for issue in [*critique.get("evidence_gaps", []), *critique.get("hard_conflicts", [])])
        if not compared:
            missing.append(task["card_id"])
    if missing:
        raise ValueError("PASS requires source-neutral read-card disposition review for " + ", ".join(missing)
            + "; acknowledge all listed reasons and a retained comparator, or REVISE the disposition; no forced nomination")


def formal_transfer_comparison(task, documents, critique):
    """正式候选审核也可承载步骤比较，不强迫把同一结论复制到问题列表。"""
    routes = documents.get("05_routes.json", {}).get("routes", [])
    step_paths = set()
    for target in task["targets"]:
        for index, route in enumerate(routes):
            if route["route_id"] != target["route_id"]:
                continue
            for position, step in enumerate(route.get("steps", []), 1):
                if step.get("step", position) == target["step"]:
                    step_paths.add(f"05_routes.json/routes/{index}/steps/{position - 1}")
    for review in critique.get("candidate_reviews", []):
        if review.get("candidate_id") != task["candidate_id"] or review.get("disposition") == "revise":
            continue
        reason = str(review.get("reason") or "").strip()
        refs = [str(ref).lstrip("/") for ref in review.get("evidence_refs", [])]
        # 必须指向本次任务的实际步骤，泛指整条路线/整个文件不足以证明完成比较。
        if not reason or not any(ref == pointer or ref.startswith(pointer + "/")
                                 for ref in refs for pointer in step_paths):
            continue
        # 仅识别当前已知稳定 ID 的精确出现，不按酶名相似度或中文措辞猜测竞争者。
        peers = task["concrete_peer_ids"]
        if not peers or set(peers) & review_object_mentions(review, documents):
            return True
    return False


def require_review_consistency(documents, decision, critique):
    """审核处置必须作用于当前产物；未知可保留，明确错误不能只加免责声明。"""
    if decision in {'PASS', 'PARTIAL_PASS'} and documents.get('04_candidates.json', {}).get('unassessed_read_cards'):
        raise ValueError('read comparison incomplete; unresolved read-card tasks require screening, not blanket PASS')
    if decision == "PARTIAL_PASS":
        require_read_card_review(documents, critique)
        from .review_scope import scoped_review, retained_projection
        summary = scoped_review(documents, critique)
        if not summary['can_partially_deliver'] or not summary['pending_ids']:
            raise ValueError('PARTIAL_PASS requires scoped pending items and at least one unaffected reviewed route; global failures cannot be waived')
        clean, review = retained_projection(documents, critique, summary)
        clean.get("04_candidates.json", {})["read_card_reviews"] = []  # 原完整视图已核对，不在依赖投影中重复核对。
        require_review_consistency(clean, 'PASS', review)
        return
    routes = documents.get("05_routes.json", {}).get("routes", [])
    candidates = documents.get("04_candidates.json", {}).get("enzyme_candidates", [])
    mechanisms = documents.get("02_mechanisms.json", {}).get("mechanism_hypotheses", [])
    route_ids = {row["route_id"] for row in routes}
    candidate_ids = {row["candidate_id"] for row in candidates}
    mechanism_ids = {row["hypothesis_id"] for row in mechanisms}
    excluded_candidates = set(critique.get("excluded_candidate_ids", []))
    excluded_mechanisms = set(critique.get("excluded_mechanism_ids", []))
    excluded_candidates.update(row['candidate_id'] for row in critique.get('candidate_reviews', []) if row['disposition'] == 'exclude')
    excluded_mechanisms.update(row['hypothesis_id'] for row in critique.get('mechanism_reviews', []) if row['disposition'] == 'exclude')
    if not excluded_candidates <= candidate_ids or not excluded_mechanisms <= mechanism_ids:
        raise ValueError("review exclusions reference unknown candidate or mechanism IDs")
    rows = critique.get("route_dispositions", [])
    dispositions = {row["route_id"]: row["disposition"] for row in rows}
    if len(dispositions) != len(rows) or not set(dispositions) <= route_ids:
        raise ValueError("route dispositions must use unique current route IDs")
    if decision != "PASS":
        return
    # 净式只描述收支，不能单独确证隐藏中间体或逐步机理；不涉及具体酶或反应答案。
    for row in [*critique.get('candidate_reviews', []), *critique.get('mechanism_reviews', [])]:
        for sentence in re.split(r'[。；;\n]', str(row.get('reason') or '')):
            if re.search(r'不能|不(?:等于|足以|证明)|未能|并非|不足以', sentence):
                continue
            if re.search(r'(?:净(?:反应)?式|反应式).{0,20}(?:确证|证实|实测证明|证明).{0,80}(?:活化模块|中间体|逐步机理|反应机制)', sentence):
                raise ValueError('net reaction equation cannot establish a measured intermediate/mechanism; keep the net fact and label mechanistic interpretation as inference')
    for field, key, expected in [('candidate_reviews', 'candidate_id', candidate_ids - excluded_candidates),
                                 ('mechanism_reviews', 'hypothesis_id', mechanism_ids - excluded_mechanisms)]:
        reviews = critique.get(field, [])
        if any(row['disposition'] == 'revise' for row in reviews):
            raise ValueError('PASS cannot contain unresolved per-object reviews; use PARTIAL_PASS or REVISE')
        if reviews and not expected <= {row[key] for row in reviews}:
            raise ValueError('per-object review is missing current objects')
    computed = scientific_review(documents)
    excluded_routes = {key for key, value in dispositions.items() if value == "exclude"}
    for task in computed['transfer_comparisons']:
        if task['candidate_id'] in excluded_candidates:
            continue
        addressed = any(task['candidate_id'] in issue.get('affected_ids', [])
                        and {t['route_id'] for t in task['targets']} & set(issue.get('affected_ids', []))
                        and (not task['concrete_peer_ids'] or set(task['concrete_peer_ids']) & review_object_mentions(issue, documents))
                        and str(issue.get('statement') or '').strip()
                        for issue in [*critique.get('hard_conflicts', []), *critique.get('evidence_gaps', [])])
        addressed = addressed or formal_transfer_comparison(task, documents, critique)
        if not addressed:
            raise ValueError('PASS requires concrete route-transfer comparison for ' + task['candidate_id']
                + '; reference the actual step and a concrete peer in candidate_reviews or evidence_gaps, '
                  'or request targeted revision; no duplicate issue or forced binding')
    for check in computed['cofactor_systems']:
        if check['status'] != 'requires_review' or check['route_id'] in excluded_routes:
            continue
        addressed = any(check['route_id'] in issue.get('affected_ids', [])
                        and set(check['mechanism_ids']) & set([*issue.get('affected_ids', []), *issue.get('evidence_refs', [])])
                        and str(issue.get('statement') or '').strip()
                        for issue in [*critique.get('hard_conflicts', []), *critique.get('evidence_gaps', [])])
        # 同一科学核对可写在正式路线处置中，不强迫模型在缺口数组重复一遍。
        mechanism_refs = {ref for key in check['mechanism_ids'] for ref in
                          (key, 'mechanism:' + key, '02_mechanisms.json#' + key)}
        addressed = addressed or any(
            row.get('route_id') == check['route_id'] and str(row.get('reason') or '').strip()
            and mechanism_refs.intersection(row.get('evidence_refs', []))
            and any(str(ref).startswith(('scientific_checks.cofactor_systems',
                                         'review_context/scientific_checks/cofactor_systems'))
                    for ref in row.get('evidence_refs', []))
            for row in critique.get('route_dispositions', []))
        if not addressed:
            raise ValueError('PASS requires explicit mechanism/route cosubstrate review for ' + check['route_id']
                + '; reference the route and mechanism IDs, resolve a true system conflict or explain uncertainty; arithmetic balance is insufficient')
    if any(row["status"] == "conflict" and row["route_id"] not in excluded_routes for row in computed["stoichiometry"]):
        raise ValueError("PASS requires repair or exclusion of a computed stoichiometry conflict; text labels cannot override arithmetic")
    if any(row["status"] in {"conflict", "incomplete"}
           and row["id"] not in excluded_candidates | excluded_routes for row in computed["ranking_consistency"]):
        raise ValueError("PASS requires resolving ranking direction/score or peer binding inconsistencies; a limitation note cannot repair them")
    for finding in computed["ranking_basis"]:
        if finding["id"] in excluded_candidates | excluded_routes:
            continue
        reviewed = any(finding["id"] in issue.get("affected_ids", []) and str(issue.get("statement") or "").strip()
                       for issue in critique.get("evidence_gaps", []))
        if not reviewed:
            raise ValueError("PASS requires source-neutral ranking review for " + finding["id"] + "; a lexical hint alone is not a chemical conflict")
    # 不靠关键词猜测排除是否合理；要求审核者显式比较原提名与仍保留的候选。
    for task in exclusion_review_tasks(documents.get("04_candidates.json", {})):
        compared = any(
            task["proposal_id"] in issue.get("affected_ids", [])
            and (not task["compare_candidate_ids"] or set(task["compare_candidate_ids"]) & review_object_mentions(issue, documents))
            and str(issue.get("statement") or "").strip()
            for issue in [*critique.get("evidence_gaps", []), *critique.get("hard_conflicts", [])])
        if not compared:
            raise ValueError("PASS requires source-neutral exclusion comparison for proposal " + task["proposal_id"]
                             + "; reference it and compared candidate IDs in evidence_gaps, or REVISE screening")
    require_read_card_review(documents, critique)
    feedback_ids = {row["feedback_id"] for row in documents.get("04_candidates.json", {}).get("mechanism_feedback", [])}
    handled_feedback = {identifier for issue in [*critique.get("hard_conflicts", []), *critique.get("evidence_gaps", [])]
                        for identifier in issue.get("affected_ids", [])}
    if not feedback_ids <= handled_feedback:
        raise ValueError("PASS requires mechanism feedback disposition in evidence_gaps (reject/defer with reason), or REVISE to mechanism")
    if critique.get("required_revisions") or critique.get("revision_tasks") or "revise" in dispositions.values():
        raise ValueError("PASS cannot retain required revisions or revise routes")
    if set(dispositions) != route_ids:
        raise ValueError("PASS requires disposition of every route")
    excluded_routes = {key for key, value in dispositions.items() if value == "exclude"}
    for row in candidates:
        if row["candidate_id"] not in excluded_candidates and (
                row.get("hard_conflict") or excluded_mechanisms.intersection(row.get("mechanism_ids", []))):
            raise ValueError("PASS requires repair or exclusion of conflicting candidates")
    for route in routes:
        if route["route_id"] in excluded_routes:
            continue
        balance = route.get("balance_review") or {}
        needs_balance_note = (balance.get("main_material_flow") == "gap"
                              or balance.get("cosubstrate_accounting") == "incomplete"
                              or balance.get("regeneration") == "required_but_missing")
        # 审核语义绑定路线身份，不绑定解释所在字段；不要求重复提交同一缺口。
        balance_acknowledged = any(
            route["route_id"] in issue.get("affected_ids", []) and str(issue.get("statement") or "").strip()
            for issue in critique.get("evidence_gaps", [])) or any(
            row.get("route_id") == route["route_id"] and str(row.get("reason") or "").strip()
            and (row.get("disposition") == "demote" or any(
                str(ref).startswith("05_routes.json") or ref == 'route:' + route['route_id']
                for ref in row.get("evidence_refs", [])))
            for row in rows)
        if needs_balance_note and not balance_acknowledged:
            raise ValueError("PASS requires a reason for the route's main-material/accounting gap in evidence_gaps or route_dispositions; cite the route artifact when retaining, or explicitly demote")
        if any(ref == f"04_candidates.json:{identifier}"
               for ref in route.get("support_refs", []) for identifier in excluded_candidates):
            raise ValueError("PASS route contains excluded catalyst in its supporting references")
        for step in route.get("steps", []):
            check = step.get("chemistry_check") or {}
            if (step.get("candidate_id") in excluded_candidates
                    or excluded_mechanisms.intersection(step.get("mechanism_ids", []))
                    or "conflict" in [check.get(key) for key in
                        ("bond_topology", "domain_assignment", "regioselectivity", "stereochemistry", "electron_balance")]
                    or check.get("catalyst_scope") == "analogy_only"):
                raise ValueError("PASS route contains excluded catalyst, mechanism or explicit chemical conflict")
    excluded = excluded_routes | excluded_candidates | excluded_mechanisms
    # 表述与证据错误也不能以免责声明冒充已修复，但不由其自动推断化学淘汰。
    for issue in [*critique.get("hard_conflicts", []), *critique.get("evidence_gaps", [])]:
        if review_issue_type(issue) in {"overstatement", "evidence_attribution", "ranking_inconsistency"}:
            affected = set(issue.get("affected_ids", [])) - set(documents)
            if not affected or not affected <= excluded:
                raise ValueError("PASS requires repair of current overstatement/evidence_attribution/ranking_inconsistency, not a limitation note")
    for issue in critique.get("hard_conflicts", []):
        if review_issue_type(issue) == "audit_delivery":
            raise ValueError("PASS requires resolving audit delivery limitations, not excluding chemical routes")
        if review_issue_type(issue) != "chemical_conflict":
            continue
        if issue.get("severity") in {"major", "critical"}:
            affected = set(issue.get("affected_ids", []))
            # 具体对象与产物位置分开核对：文件名不是可排除的候选。
            # 仅有文件位置或存在未知 ID 时仍阻断，不能忽略未处置对象。
            concrete = affected & (route_ids | candidate_ids | mechanism_ids)
            if concrete:
                affected -= set(documents)
            if not affected or not affected <= excluded:
                raise ValueError("PASS hard conflict requires targeted repair or exclusion, not a limitation note")


def require_current_review(snapshot):
    """01—05 改动后必须重新审核，不能仅重新读取证据就沿用旧 PASS。"""
    review = snapshot["documents"].get("06_critique.json", {})
    digest = review.get("audited_upstream_digest")
    if digest and digest != audit_digest(snapshot):
        raise ValueError("review_not_current: upstream artifacts changed; submit a fresh critique")
    require_review_consistency(snapshot["documents"], review.get("review_decision"), review.get("critique", {}))


def actual_references(documents, read_refs=()):
    """收集当前候选和路线真正使用的 Wiki 片段引用。"""
    owners = {}
    for row in documents.get("04_candidates.json", {}).get("read_card_reviews", []):
        owners["read_card_review:" + row["card_id"]] = [ref for ref in read_refs
            if ref.startswith(row["card_id"] + ":")]
    for card_id in documents.get('04_candidates.json', {}).get('unassessed_read_cards', []):
        owners['read_card_review:' + card_id] = [ref for ref in read_refs if ref.startswith(card_id + ':')]
    for mechanism in documents.get("02_mechanisms.json", {}).get("mechanism_hypotheses", []):
        refs = list(mechanism.get("knowledge_anchor_refs", mechanism.get("anchor_wiki_refs", [])))
        refs.extend(ref for event in mechanism.get("event_evidence", []) for ref in event.get("wiki_refs", []))
        owners["mechanism:" + mechanism["hypothesis_id"]] = list(dict.fromkeys(r for r in refs if r.startswith("ERD-CARD-")))
    for candidate in documents.get("04_candidates.json", {}).get("enzyme_candidates", []):
        refs = [ref for key in ("supports", "contradictions") for row in candidate.get(key, [])
                for ref in row.get("source_refs", []) if ref.startswith("ERD-CARD-")]
        refs.extend(f"{row['card_id']}:{row['field_path']}" for key in ("supports", "contradictions")
                    for row in candidate.get(key, []) if row.get("card_id") and row.get("field_path"))
        refs.extend(ref for factor in candidate.get("ranking_factors", []) if factor.get("source") == "wiki"
                    for ref in factor.get("refs", []))
        owners[candidate["candidate_id"]] = list(dict.fromkeys(refs))
        # 身份引文必须进入原文审核，但与该候选的目标活性支持分开归属。
        binding = candidate.get("identity_verification") or {}
        if binding.get("status") == "wiki_identity_linked":
            owners["identity:" + candidate["candidate_id"]] = list(dict.fromkeys(
                ref for ref in binding.get("refs", []) if ref.startswith("ERD-CARD-")))
    for feedback in documents.get("04_candidates.json", {}).get("mechanism_feedback", []):
        owners[feedback["feedback_id"]] = [ref for ref in feedback.get("evidence_refs", [])
                                           if ref.startswith("ERD-CARD-")]
    for route in documents.get("05_routes.json", {}).get("routes", []):
        refs = [ref for key in ("support_refs", "contradiction_refs") for ref in route.get(key, [])]
        refs.extend(ref for step in route.get("steps", []) for ref in step.get("evidence_refs", []))
        refs.extend(ref for factor in route.get("ranking_factors", []) if factor.get("source") == "wiki"
                    for ref in factor.get("refs", []))
        owners["route:" + route["route_id"]] = list(dict.fromkeys(r for r in refs if r.startswith("ERD-CARD-")))
    return owners


def submission_review(store, snapshot):
    """只交付属于当前 04/05 的提交故障，明确对象和阶段；不据此作化学否定。"""
    record = store._read_json_if_exists(store._run_path(snapshot["run"]["run_id"], "screening_submission.json")) or {}
    expected = {name: hashlib.sha256(canonical(snapshot["documents"].get(name)).encode()).hexdigest()
                for name in ("04_candidates.json", "05_routes.json")}
    current = (record.get("run_id") == snapshot["run"]["run_id"]
               and record.get("artifact_digests") == expected)
    return {"status": "current" if current else "not_recorded_or_stale",
            "rejected_items": copy.deepcopy(record.get("rejected_items", [])) if current else [],
            "pending_repairs": copy.deepcopy(record.get("pending_repairs", [])) if current else [],
            "rule": "按 kind/id 或 item_kind/owner_id 逐项归因，phase 区分暂存与正式提交；不把错误路径中的数组下标当作另一条路线的步骤。已修复项不再阻断；未绑定对象的旧记录只能列交付未知，不能用于否定具名酶或化学路线。"}


def reviewed_card_read_refs(store, snapshot):
    """处置卡片只能回查本轮已授权原文；不按卡名搜索，不读取摘要或全库。"""
    refs = set()
    for stage in ("mechanism", "screen"):
        record = store._read_json_if_exists(store._run_path(snapshot["run"]["run_id"], f"retrieval_{stage}.json")) or {}
        if (snapshot["run"].get("knowledge_fingerprint")
                and record.get("knowledge_fingerprint") == snapshot["run"]["knowledge_fingerprint"]):
            refs.update(record.get("selected_refs", []))
            refs.update(record.get("inherited_refs", []))
    return sorted(refs)


def audit_context(store, snapshot, base, *, page=None):
    """构造批判阶段的有界证据审计包。"""
    run = snapshot["run"]
    owners = actual_references(snapshot["documents"], reviewed_card_read_refs(store, snapshot))
    requested = list(dict.fromkeys(ref for refs in owners.values() for ref in refs))
    path = store._run_path(run["run_id"], "report_evidence_audit.json")
    digest = audit_digest(snapshot)
    base = {**base, "submission_review": submission_review(store, snapshot), "scientific_checks": scientific_review(snapshot["documents"])}
    from .incremental_review import review_plan, audit_projection, evidence_state
    _, bound_evidence = evidence_state(store, snapshot)
    evidence_digest = hashlib.sha256(canonical({ref: bound_evidence.get(ref) for ref in sorted(requested)}).encode()).hexdigest()
    plan = review_plan(store, snapshot)
    delta = None
    if plan:
        base, delta = audit_projection(snapshot, base, plan)
    base = {**base, 'deterministic_review_receipt': deterministic_review_receipt(snapshot['documents'])}
    original_summary_chars = len(canonical(base))
    compact_base = _compact_audit_base(base)
    delivery_mode = "compact"
    if len(canonical(compact_base)) > AUDIT_SUMMARY_CHARS:
        compact_base = _minimal_audit_base(compact_base)
        delivery_mode = "limited"
    previous = store._read_json_if_exists(path) or {}
    state = previous if (previous.get("digest") == digest and previous.get("context_protocol") == AUDIT_CONTEXT_PROTOCOL
                         and previous.get('evidence_digest') == evidence_digest
                         and previous.get('incremental_review') == (plan['proof'] if plan else None)) else {
        "digest": digest, "pages": {}, "delivered": [], "calls": 0, "context_protocol": AUDIT_CONTEXT_PROTOCOL,
        "context_delivered": 0, 'evidence_digest': evidence_digest}
    if not state.get('pages') and plan:
        state['delivered'] = sorted(plan['inherited_refs'])
        state['incremental_review'] = plan['proof']
    if "context_segments" not in state:
        review = store._read_json_if_exists(store._run_path(run["run_id"], "06_critique.json")) or {}
        critique = review.get("critique", {})
        prior = {key: critique.get(key, []) for key in
                 ("hard_conflicts", "evidence_gaps", "revision_tasks", "required_revisions")}
        # 紧凑视图只作导航；完整上游产物和上一轮问题通过同一分页通道送达。
        state["context_segments"] = _audit_segments({
            "upstream": {key: value for key, value in snapshot["documents"].items()
                         if key.startswith(("01_", "02_", "03_", "04_", "05_"))},
            "review_context": base, "previous_review": prior}) if not plan else _incremental_segments({
                'upstream_delta': delta, 'review_context':base, 'previous_review':prior})
        state["context_total"] = len(state["context_segments"])
    if page is None:
        last = state['pages'].get(str(len(state['pages']) - 1))
        if last and last.get('evidence_audit', {}).get('next_page') is None and audit_context_complete(state):
            return last
        page = len(state['pages'])
    if str(page) in state["pages"]:
        return state["pages"][str(page)]
    if type(page) is not int or page < 0:
        raise ValueError("audit page must be a nonnegative integer")
    # ``page`` 是证据交付协议，而不是科学判断。模型偶尔会在首轮直接请求
    # page=1；保留顺序约束，但服务当前唯一可交付的下一页，避免把一次
    # 可恢复的分页跳号升级为整个 Workflow 的失败。
    requested_page = page
    page = len(state["pages"])
    evidence = {}
    for stage in ("mechanism", "screen"):
        record = store._read_json_if_exists(store._run_path(run["run_id"], f"retrieval_{stage}.json")) or {}
        if record.get("knowledge_fingerprint") == run["knowledge_fingerprint"]:
            authorized = set(record.get("selected_refs", [])) | set(record.get("inherited_refs", []))
            evidence.update({r: c for r, c in record.get("evidence_snapshots", {}).items() if r in authorized})
    response = dict(compact_base) if page == 0 else {"run_id": run["run_id"], "context_delivery": "audit_delta"}
    if page == 0 and len(canonical(compact_base)) < original_summary_chars:
        response["audit_compaction"] = {
            "summary_chars_before": original_summary_chars,
            "summary_chars_after": len(canonical(compact_base)),
            "delivery_mode": delivery_mode,
            "rule": ("审计摘要已压缩；实际引用的 Wiki 原文仍由 evidence_audit 分页交付。"
                     if delivery_mode == "compact" else
                     "首屏为导航最小视图；完整上游内容由 upstream_audit 分页送达，全部送达后可审核 PASS。"),
        }
    if requested_page != page:
        response["audit_page_correction"] = {
            "requested_page": requested_page,
            "served_page": page,
            "reason": "证据审计必须顺序交付；已返回当前唯一可用的下一页。",
        }
    if page == 0:
        response["audit_flags"] = {**response.get("audit_flags", {}),
                                   "unqualified_engineering_claims": engineering_assertions(snapshot["documents"])}
        if plan:
            response['incremental_review'] = base['incremental_review']
    audit = {"candidate_route_refs": owners, "claims": {}, "missing_refs": [],
             "coverage": "not_complete", "next_page": None,
             "scope": "仅本轮实际引用片段及来源，不是完整知识卡或全库；片段未覆盖某项描述只能判为引用待核对，不能推断完整原文没有记载。送达原文不代表化学充分性或活性验证。"}
    response["evidence_audit"] = audit
    upstream = {"segments": [], "coverage": "incomplete",
                "rule": "JSON Pointer 路径对应当前产物；同值多路径仅交付一次。json_text 按 json_offset 拼接为完整 JSON 值。上一轮问题须对照当前正文复核：已修正则关闭，不照抄旧问题；未知可带说明交付，明确化学矛盾仍须修正或排除。"}
    response["upstream_audit"] = upstream
    for segment in state["context_segments"][state["context_delivered"]:]:
        upstream["segments"].append(segment)
        # 以整个回包的剩余容量分页，不让路径/摘要重复占位浪费半页预算。
        if len(canonical(response)) > 57000:
            upstream["segments"].pop()
            break
        state["context_delivered"] += 1
    upstream["remaining_segments"] = state["context_total"] - state["context_delivered"]
    upstream["coverage"] = "complete" if not upstream["remaining_segments"] else "incomplete"
    pending = [ref for ref in requested if ref not in state["delivered"]]
    if pending:
        for ref in pending:
            if ref not in evidence:
                continue
            audit["claims"][ref] = evidence[ref]
            if len(canonical(audit["claims"])) > EVIDENCE_PAGE_CHARS or len(canonical(response)) > 59000:
                del audit["claims"][ref]
                continue
            state["delivered"].append(ref)
    state["calls"] += 1
    audit["missing_refs"] = [ref for ref in requested if ref not in state["delivered"]]
    audit["unavailable_refs"] = [ref for ref in requested if ref not in evidence]
    audit["coverage"] = "all_referenced_text_delivered" if not audit["missing_refs"] else "incomplete"
    if upstream["remaining_segments"] or any(r in evidence for r in audit["missing_refs"]):
        if page == 0 or audit["claims"] or upstream["segments"]:
            audit["next_page"] = page + 1
    audit["budget_exhausted"] = bool(audit["missing_refs"] or upstream["remaining_segments"]) and audit["next_page"] is None
    if len(canonical(response)) > 60000:
        raise ValueError("audit summary exceeds delivery budget; reduce upstream verbosity, do not retry unchanged")
    state.update(requested_refs=requested, missing_refs=audit["missing_refs"])
    state["pages"][str(page)] = response
    store._atomic_write(path, canonical(state) + "\n")
    return response


def audit_limitations(store, run_id, snapshot=None):
    """把证据未送达、超预算等状态转成用户可读限制。"""
    state = store._read_json_if_exists(store._run_path(run_id, "report_evidence_audit.json"))
    if state is None:
        return ["证据核对限制：未生成本轮实际引用审计记录，不能声称全部候选证据已核对。"]
    if snapshot is not None and state.get("digest") != audit_digest(snapshot):
        return ["审核交付限制：现有审计记录不属于当前上游版本，不能沿用旧版送达状态。"]
    limitations = []
    if state.get("missing_refs"):
        limitations.append(f"证据核对限制：{len(state['missing_refs'])} 个实际引用片段未在第四阶段完整送达；相关候选仅作探索线索，不能声称全部证据已核对。")
    if not audit_context_complete(state):
        limitations.append("审核交付限制：完整上游材料尚未送达；这不是化学反证，不应为此重跑化学分析。")
    return limitations
