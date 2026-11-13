"""将模型提交的候选酶与路线判断组装为可校验结构。"""
from __future__ import annotations

import hashlib
import copy
import json
import re
from collections.abc import Mapping
from typing import Any
from jsonschema import Draft202012Validator

from .wiki_tools import claim_index, require_wiki_refs, wiki_packet
from .provenance import CAPABILITY, MODEL_ENZYME, WIKI_ENZYME, candidate_provenance
from .result_limits import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT, requested_result_limits
from .reasoning_policy import RANKING_POLICY, RANKING_FACTORS_SCHEMA, chemistry_rank_key, chemistry_score, draft_rationale
from .scientific_checks import route_accounting, cofactor_system_review


CANDIDATE_REVIEW_RULES = {
    "specific_discovery": "先独立提名，再按每条机制的每个催化步骤回想通行酶名、物种蛋白或已知变体；不得停在宽家族，也不得按目标反应创造酶名。能想到的具体身份沿用或新增 proposal_id，与 Wiki 候选平行比较；想不到则保留能力缺口。",
    "sequential_catalysis": "同一酶先后执行多次催化可以释放中间体后再结合；连续催化不等于 processive，不要求中间体始终留在活性位点。分别核对第二次底物接受与选择性。",
    "supply_and_clearance": "辅因子供应、副产物清除与闭合再生分别核对。MAT 由甲硫氨酸和 ATP 供应 SAM；MTAN 裂解 SAH 为腺嘌呤和 S-核糖同型半胱氨酸，不单独再生甲硫氨酸或 SAM。闭环必须列出其余步骤和收支。",
    "reuse": "先按中间体仍可反应的官能团检查同一催化动作复用，再比较不同酶接力；家族内不同成员的能力不能拼成一个蛋白。",
    "merge": "同一步能力或酶家族重叠不等于候选等价；合并须核对身份、全部目标步骤、底物状态与终止分支，不能用 Wiki 实体替换整个未核实家族。",
    "identity_and_activity": "蛋白身份与目标活性分开。已读卡的蛋白标识、名称和物种可核实原 proposal 的身份；使用 identity_binding，不另造旧注释酶或换 ID，不据此升级目标活性。旧注释的供体、辅因子或产物不同只说明该条反应不支持目标；不能单凭此排除同一蛋白的另一活性。确有目标化学冲突才可排除，须说明目标步骤矛盾的直接依据。",
    "ranking": RANKING_POLICY,
    "role_transfer": "证据范围与催化角色分开：substrate_relation=mechanistic_analogy 只表示天然反应提供类比，不等于 catalyst_scope=analogy_only。先逐项比较目标的氧化剂/供体活化、成断键、中间体和终止方式，判断共同能力能否迁移。天然产物不同、目标活性未报道或选择性未知不能单独推出只能作参考；若有合理迁移链，可作为 hypothesis 催化者参与同尺度比较，并保留真实未知。只能类比须解释缺少哪个目标事件、迁移桥为何不足，以及为何仍保留的同类候选没有同一问题。没有充分迁移理由时可暂缓，不强制绑定、保留或加分；共用机制也不证明底物适配。",
    "exclusion_consistency": "来源盲评：底物/供体/天然位点不同与目标未报道，先记为适配未知；Wiki 与库外候选同尺度。排除须比较仍保留的同类候选，并区分明确冲突、相对劣势与预算。不能因具体身份更明确反而额外处罚；局部步骤可用性须独立于单酶全程能力判断。",
    "balance": "分别判断主底物/中间体承接、共底物计量和再生要求。允许外加ATP、氧化还原辅因子或活化供体；净消耗不等于物料断裂。只有声称闭环或用户要求再生时才要求再生模块；未配平仍记未知，不能自动判连续或已平衡。",
    "protein_diversity": "路线方向可按相同物料步骤归组；具体蛋白不能仅因承担同一步骤而视为冗余。排除前按原 proposal_id 核对提名：已具名不等于身份已核实，但不能说未具名；身份歧义须明确 identity_issue 与 identity_basis，化学排除须用 scope_basis 说明独立的目标步骤矛盾。功能重叠或预算不足只记待评估，不作化学淘汰，也不强行推荐。",
    "read_comparison": "新增宽泛检索前先处理已读竞争线索，按原文比较共享催化动作、目标缺口和保留候选的同类未知。read_card_reviews.comparison 简记 shared_events、target_gaps、peer_ids、basis；预算不足用 coverage_limit，只是未比较，不是化学排除。可提名、维持不选或暂缓，不强制入选。",
    'nomination_scope': '角色按目标事件覆盖判断，不按Wiki证据是否为类比判断。能够提出完整合理的目标迁移链时，可作为primary单酶假说并以hypothesis绑定；只覆盖局部且确有尚未桥接的关键动作时才保留未绑定route_component。天然与目标终止分支改变可以改变氧化剂/亲核体/还原剂的净消耗，要核算目标支路，不能把与天然总式不同本身当不可迁移的反证。只缺目标实测、入袋或选择性属于未知，不独立构成关键动作障碍；与真实竞争酶同尺度比较，也不因共用辅因子就自动提名或绑定。',
}


def discovery_tasks(mechanisms, candidates=(), routes=()):
    """把机制事件变为有界的具体酶发现任务，不包含任何目标答案。"""
    tasks = []
    for mechanism in mechanisms[:MAX_RESULT_LIMIT]:
        identifier = mechanism.get("hypothesis_id")
        linked = [row for row in candidates if identifier in (row.get("mechanism_ids") or [])]
        tasks.append({
            "mechanism_id": identifier, "route_type": mechanism.get("route_type"),
            "event_hints": [str(event)[:120] for event in (mechanism.get("required_events") or [])[:8]],
            "search_angles": ["常规正向活性", "已知逆向反应/替代供体或受体", "同类中间体的跨家族催化与旁活性"],
            "capability_axes": ["供体与受体各自的反应位点", "离去基与活化当量", "共底物/能量来源", "可迁移的催化动作与目标终止方式"],
            "specific_candidates": [row.get("specific_name") or row.get("enzyme_name") or row.get("card_id") for row in linked
                                    if row.get("candidate_kind") != CAPABILITY and row.get("identity_level") != "family"
                                    and row.get("candidate_role") != "diagnostic_control" and not row.get("hard_conflict")],
            "family_or_capability_leads": [row.get("enzyme_name") or row.get("required_capability") for row in linked
                                          if row.get("identity_level") == "family" or row.get("candidate_kind") == CAPABILITY],
            "instruction": "逐个催化步骤比较具体酶与可能的旁活性、逆向反应及替代底物。优先回想已有通行名和物种蛋白；身份依据与目标活性分开。未知允许保留，不编名、不强制凑数。多酶机制须逐步组装，不能只提交各酶清单。",
        })
        # 查询依据是实际步骤端点，不只沿用整体反应名或当前命中的家族名。
        bindings = {row[key]: row for row in candidates for key in ("card_id", "proposal_id", "candidate_id") if row.get(key)}
        tasks[-1]["step_targets"] = [{"route_id": route["route_id"], "step": position,
            "input_hint": str(step.get("input", ""))[:120], "output_hint": str(step.get("output", ""))[:120],
            "current_identity": next((bindings[step[key]].get("specific_name") or bindings[step[key]].get("enzyme_name") or step[key]
                                      for key in ("card_id", "proposal_id", "candidate_id") if step.get(key) in bindings), "未匹配能力")}
            for route in routes if route.get("route_type") == mechanism.get("route_type")
            for position, step in enumerate(route.get("steps", [])[:8], 1) if step.get("step_kind") != "nonenzymatic"][:8]
        # 尚未组装路线时也提供逐事件任务，避免发现入口依赖已经找到催化者。
        tasks[-1]["intermediate_hints"] = [str(value)[:120] for value in (mechanism.get("key_intermediates") or [])[:4]]
        tasks[-1]["event_targets"] = [
            {"event_index": index, "event": str(event)[:180],
             "evidence_status": next((row.get("status", "model_hypothesis")
                                     for row in mechanism.get("event_evidence", [])
                                     if row.get("event") == event), "unassessed")}
            for index, event in enumerate((mechanism.get("required_events") or [])[:8], 1)]
        for step in tasks[-1]["step_targets"]:
            step["discovery_priority"] = "核对本步催化者与替代者；未知不要求强行填酶"
        tasks[-1]["discovery_order"] = [
            row["event_index"] for row in sorted(tasks[-1]["event_targets"],
                key=lambda row: row["evidence_status"] in {"local_evidence", "conflict"})]
        # 这里只给线索摘要；完整候选保留在原暂存和产物中，不重复塞入上下文。
        for field in ("specific_candidates", "family_or_capability_leads"):
            tasks[-1][field + "_count"] = len(tasks[-1][field])
            tasks[-1][field] = tasks[-1][field][:4]
    return tasks


def exclusion_review_tasks(document):
    """把具名候选的淘汰送入来源无关的对照审核，不自动恢复或推荐候选。"""
    generation = document.get("candidate_generation", {})
    draft = {row["proposal_id"]: row for row in candidate_identity_baseline(generation)}
    peers = [row for row in document.get("enzyme_candidates", [])
             if row.get("candidate_kind") != CAPABILITY and row.get("candidate_role") != "diagnostic_control"]
    return [{"proposal_id": row["proposal_id"], "enzyme_name": draft[row["proposal_id"]]["enzyme_name"],
             "exclusion_basis": row.get("exclusion_basis"), "reason": row.get("reason"),
             "original_identity_level": draft[row["proposal_id"]].get("identity_level"),
             **{key: row[key] for key in ("identity_issue", "identity_basis", "scope_basis") if row.get(key)},
             "original_rationale": draft_rationale(draft[row["proposal_id"]]),
             "compare_candidate_ids": [peer["candidate_id"] for peer in peers],
             "instruction": "逐项对照保留候选：相同底物/位点/供体偏差须同尺度。先核对是否与已保留候选为同一蛋白的不同名称或旧反应注释，不能把旧供体/产物不支持目标误作该蛋白不能具有目标活性；不能按旧活性另造水解酶或氧化酶身份。结论可维持排除或要求修订；明确目标步骤直接矛盾与天然反应差异的区别，不把未报道当反证。"}
            for row in generation.get("dispositions", [])
            if row.get("decision") == "excluded"
            and draft.get(row["proposal_id"], {}).get("identity_level") in {"protein", "named_enzyme"}]


def read_card_comparison_tasks(context, candidates=(), reviews=()):
    """只对已授权回读蛋白提供提名前比较入口，不从全库挑选或自动提名。"""
    reviewed = {row['card_id']: row for row in reviews}
    selected = {cid for row in candidates for cid in [row.get('card_id'), *row.get('card_ids', [])] if cid}
    peers = [row.get('candidate_id') or row.get('proposal_id') or row.get('card_id')
             for row in candidates if row.get('candidate_kind') != CAPABILITY and not row.get('hard_conflict')]
    return [{'card_id': card['card_id'], 'enzyme_name': card.get('display_name', ''),
             'read_refs': [claim['ref'] for claim in card.get('claims', [])],
             'cofactors': card.get('cofactor_classes', []),
             'previous_decision': reviewed.get(card['card_id']),
             'compare_peer_ids': peers[:3],
             'event_source': '当前 mechanisms.required_events；参见 candidate_review_rules.read_comparison'}
            for card in context.get('cards', []) if card['card_id'] not in selected
            and (card['card_id'] not in reviewed or not reviewed[card['card_id']].get('comparison'))]


def screening_completion_tasks(context, candidates=(), routes=(), reviews=()):
    """收尾前先消化已读证据与已声明计量；不触发检索、不改变科学判断。"""
    cards = {card['card_id']: card for card in context.get('cards', [])}
    selected = {cid for row in candidates for cid in [row.get('card_id'), *row.get('card_ids', [])] if cid}
    abstract_peers = {str(row[key]) for row in candidates if row.get('candidate_kind') == CAPABILITY
                      for key in ('candidate_id', 'proposal_id') if row.get(key)}
    systems = cofactor_system_review({'02_mechanisms.json': {'mechanism_hypotheses': context.get('mechanisms', [])},
                                     '03_capabilities.json': {'cofactor_candidates': context.get('cofactors', [])},
                                     '05_routes.json': {'routes': routes}})
    return {'cofactor_systems': [r for r in systems if r['status'] == 'requires_review'],
            'route_transfers': route_transfer_tasks(context, candidates, routes, reviews),
            'read_comparisons': read_card_comparison_tasks(context, candidates, reviews),
            'transfer_rechecks': [{'card_id': row['card_id'], 'previous_reason': row['reason'],
                                  'shared_events': row['comparison']['shared_events'],
                                  'target_gaps': row['comparison'].get('target_gaps', []),
                                  'native_reaction': str(cards[row['card_id']].get('native_reaction', ''))[:900],
                                  'read_refs': [claim['ref'] for claim in cards[row['card_id']].get('claims', [])],
                                  'abstract_peer_ids': sorted(abstract_peers & set(row['comparison'].get('peer_ids', []))),
                                  'instruction': '先从已知反应的共底物消耗／副产物推断活化模块，区分正文事实与推断，再用 activation_inference、transfer_test 记录迁移检验。nomination_reason 单独评价是否值得列为未绑定机制启发候选；不能只回答能否绑定完整目标步骤。能力占位只是待寻找的功能，不是可替代真实蛋白的竞争酶；不能因占位已覆盖某方向就排除具名蛋白。若仍不选，应说明独立于功能重叠的实际差异，同类未知按相同尺度比较。允许用 route_component 提名未绑定线索，但不强制提名、改造或保留。'}
                                 for row in reviews if row['card_id'] in cards and row['card_id'] not in selected
                                 and row['decision'] in {'not_selected', 'analogy_only'}
                                 and (row.get('comparison') or {}).get('shared_events')
                                 and (not (row.get('comparison') or {}).get('transfer_test')
                                      or not (row.get('comparison') or {}).get('nomination_reason')
                                      or abstract_peers & set(row['comparison'].get('peer_ids', [])))],
            'route_accounting': [{'route_id': route['route_id'], 'missing_equation_steps': account['missing_equation_steps'],
                                   'check': account['check'],
                                   'instruction': '按已声明输入输出提交每步 equation，或完整 stoichiometry；未知分子式用空串，不臆造系数。'}
                                 for route in routes for account in [route_accounting(route)]
                                 if route.get('route_type') == 'multi_enzyme'
                                 and (account['ledger'] is None or account['check']['status'] == 'conflict')]}


def route_transfer_tasks(context, candidates=(), routes=(), reviews=()):
    """把已提名的具体酶与仍由能力占位承接的步骤配对；不自动绑定或改角色。"""
    def ids(row):
        values = {row.get(k) for k in ('candidate_id', 'proposal_id', 'card_id')} | set(row.get('card_ids', []))
        if row.get('proposal_id'): values.add('candidate-proposed-' + row['proposal_id'])
        for card in [row.get('card_id'), *row.get('card_ids', [])]:
            if card: values.add('candidate-' + card.lower())
        return values - {None, ''}
    abstract = set().union(*(ids(c) for c in candidates if c.get('candidate_kind') == CAPABILITY)) if candidates else set()
    real_peers = set().union(*(ids(c) for c in candidates if c.get('candidate_kind') != CAPABILITY)) if candidates else set()
    considered = {r['card_id'] for r in reviews if r.get('decision') == 'considered'
                  and (r.get('comparison') or {}).get('transfer_test')
                  and ((r.get('comparison') or {}).get('basis') != 'relative_fit'
                       or (real_peers - {r['card_id'], 'candidate-' + r['card_id'].lower()})
                          & set(r['comparison'].get('peer_ids', [])))}
    tasks = []
    for candidate in candidates:
        if (candidate.get('candidate_kind') == CAPABILITY or candidate.get('identity_level') == 'family'
                or candidate.get('candidate_role') == 'diagnostic_control' or candidate.get('hard_conflict')):
            continue
        aliases = ids(candidate)
        if aliases & considered: continue
        if any(aliases & ids(step) for route in routes for step in route.get('steps', [])): continue
        linked = set(candidate.get('mechanism_ids', []))
        targets = [{'route_id': r['route_id'], 'step': s.get('step', i),
                    'mechanism_ids': sorted(linked & set(s.get('mechanism_ids', r.get('mechanism_ids', [])))),
                    'input': s.get('input', ''), 'output': s.get('output', ''),
                    'current_capability': s.get('required_capability', '')}
                   for r in routes for i,s in enumerate(r.get('steps', []), 1)
                   if ids(s) & abstract and linked & set(s.get('mechanism_ids', r.get('mechanism_ids', [])))]
        if not targets: continue
        tasks.append({'candidate_id': candidate.get('candidate_id') or candidate.get('card_id') or candidate.get('proposal_id'),
                      'card_id': candidate.get('card_id') or next(iter(candidate.get('card_ids', [])), None),
                      'targets': targets[:3], 'transfer_basis': candidate.get('support_reason') or candidate.get('ranking_reason', ''),
                      'concrete_peer_ids': sorted(real_peers - aliases)[:12],
                      'instruction': '比较完整目标事件链能否经替代终止完成，不要求目标实测后才允许 catalyst_scope=hypothesis 绑定。天然终止与目标分支改变，本就可能改变氧化剂/亲核体/还原剂净消耗；应核算目标支路，不能用天然总式不同循环否定迁移。只有共享局部而确有无法桥接的关键动作时才限为未绑定线索。若维持相对不利，须与真实竞争酶比较同类未知，不能只和能力占位比较。可用 read_card_reviews 的 considered、transfer_test 与实际 peer_ids 保存比较；允许有据不绑定，不强行改角色或名次。'})
    return tasks


def validate_candidate_name(spec, baseline=()):
    """暂存与提交共用身份边界；不把可选的解释文字当作身份核实。"""
    origin = candidate_provenance(spec)
    specific = str(spec.get('specific_name') or '').strip()
    if specific and (origin['candidate_kind'] != MODEL_ENZYME or origin['identity_level'] == 'family'
                     or not str(spec.get('name_evidence') or '').strip()):
        raise ValueError('specific_name requires a named model enzyme and explicit name_evidence; family clues cannot become proteins by a label')
    if origin['candidate_kind'] == MODEL_ENZYME and (spec.get('name_basis') == 'target_description'
            or (spec.get('name_basis') == 'uncertain' and origin['identity_level'] != 'family')):
        raise ValueError('target descriptions require unmatched_capability; uncertain identities may only be family clues, not named candidates')
    if origin['candidate_kind'] == MODEL_ENZYME and spec.get('name_basis') == 'established_name' and not str(spec.get('name_evidence') or '').strip():
        frozen = any(row.get('proposal_id') == spec.get('proposal_id') and row.get('enzyme_name') == spec.get('enzyme_name')
                     and row.get('identity_level') in {'protein', 'named_enzyme'} for row in baseline)
        if not frozen:
            raise ValueError('established enzyme name requires name_evidence or the same frozen named nomination; an exact target transformation is not identity evidence')
        return '名称为模型提名，未提供额外身份依据；未经过 Wiki 核实，不证明蛋白身份或目标活性'
    return ''


def read_card_exclusion_review(document):
    """实际回读后的负向处置与候选排除同尺度送审；不要求被选中。"""
    selected_cards = {cid for candidate in document.get('enzyme_candidates', [])
                      for cid in candidate.get('card_ids', [])}
    peers = [row["candidate_id"] for row in document.get("enzyme_candidates", [])
             if row.get("candidate_kind") != CAPABILITY and not row.get("hard_conflict")
             and row.get("candidate_role", "primary") == "primary"]
    aliases = {str(row[key]): row['candidate_id'] for row in document.get('enzyme_candidates', [])
               for key in ('candidate_id', 'proposal_id', 'card_id') if row.get(key)}
    eligible = {row['candidate_id'] for row in document.get('enzyme_candidates', [])
                if row.get('candidate_kind') != CAPABILITY and not row.get('hard_conflict')}
    return [{"card_id": row["card_id"], "decision": row["decision"], "reason": row["reason"],
             "comparison": copy.deepcopy(row.get('comparison')),
             "comparison_status": 'recorded_unverified' if row.get('comparison') else 'not_compared',
             "comparison_peer_ids": [aliases.get(value, value) for value in (row.get('comparison') or {}).get('peer_ids', [])],
             "compare_candidate_ids": [aliases[value] for value in (row.get('comparison') or {}).get('peer_ids', [])
                                       if value in aliases and aliases[value] in eligible] or peers}
            for row in document.get("read_card_reviews", []) if row["decision"] in {"not_selected", "analogy_only"}
            and row['card_id'] not in selected_cards]


def mechanism_route_coverage(mechanisms, routes, candidates=()):
    """检查机制是否有同类型路线对象承接；关联不证明物料或化学闭合。"""
    bindings = {row[key]: row for row in candidates for key in ("card_id", "proposal_id", "candidate_id") if row.get(key)}
    def linked_ids(step):
        source = next((bindings[step[key]] for key in ("card_id", "proposal_id", "candidate_id")
                       if step.get(key) in bindings), {})
        return step.get("mechanism_ids") or source.get("mechanism_ids") or []
    return [{"mechanism_id": mechanism.get("hypothesis_id"), "route_type": mechanism.get("route_type"),
             "event_plan": list(mechanism.get('required_events') or [])[:8],
             "intermediates": list(mechanism.get('key_intermediates') or [])[:4],
             "candidate_ids": [row.get('candidate_id') or row.get('proposal_id') or row.get('card_id')
                               for row in candidates if mechanism.get('hypothesis_id') in row.get('mechanism_ids', [])],
             "linked_route_ids": [route["route_id"] for route in routes
                                  if route.get("route_type") == mechanism.get("route_type") and (mechanism.get('hypothesis_id') in route.get('mechanism_ids', []) or any(
                                      mechanism.get("hypothesis_id") in linked_ids(step)
                                      for step in route.get("steps", [])))],
             "note": "仅检查对象承接；逐步底物、产物、活化、计量和选择性仍须审核"}
            for mechanism in mechanisms[:MAX_RESULT_LIMIT]]


def build_candidate_baseline_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """为 Wiki 读取前的防锚定候选轮只返回反应化学信息。"""

    run_id = snapshot["run"]["run_id"]
    reaction = snapshot["documents"]["01_reaction_analysis.json"]
    if reaction["run_id"] != run_id:
        raise ValueError("upstream artifact belongs to another run")
    data = {
        "run_id": run_id,
        "reaction": reaction["reaction_analysis"],
        "candidate_review_rules": dict(CANDIDATE_REVIEW_RULES),
        "result_limits": requested_result_limits(snapshot["run"].get("reaction_input") or {}),
        "max_route_steps": (snapshot["run"].get("reaction_input") or {}).get("context", {}).get("max_route_steps", 8),
        "candidate_generation": {
            "mode": "awaiting_chemistry_first_draft",
            "draft": [],
            "draft_count": 0,
            "draft_digest": hashlib.sha256(b"[]").hexdigest(),
            "ranking_policy": (
                "先独立生成防锚定基线，再按需读取 Wiki；检索后发现的新候选可以加入统一比较，"
                "但 Wiki 命中本身不提供排序加分。"
            ),
        },
    }
    data["context_digest"] = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return data


def build_screening_context(snapshot: Mapping[str, Any], library: Any, evidence_packet=None) -> dict[str, Any]:
    """组装候选筛选所需的上游机制、辅因子和已授权 Wiki 证据。"""
    run_id = snapshot["run"]["run_id"]
    documents = snapshot["documents"]
    for name in ("01_reaction_analysis.json", "02_mechanisms.json", "03_capabilities.json"):
        if documents[name]["run_id"] != run_id:
            raise ValueError("upstream artifact belongs to another run")
    packet = dict(evidence_packet) if evidence_packet is not None else wiki_packet(library, snapshot["run"])
    draft = [dict(item) for item in packet.pop("candidate_draft", [])]
    prior = [dict(item) for item in packet.pop("candidate_nominations", [])]
    prior_wiki = [dict(item) for item in packet.pop('wiki_candidate_nominations', [])]
    data = {
        "run_id": run_id, **packet,
        "user_input_text": " ".join(str(note) for note in (snapshot["run"].get("reaction_input") or {}).get("context", {}).get("notes", [])),
        "reaction": documents["01_reaction_analysis.json"]["reaction_analysis"],
        "candidate_review_rules": dict(CANDIDATE_REVIEW_RULES),
        "result_limits": requested_result_limits(snapshot["run"].get("reaction_input") or {}),
        "max_route_steps": (snapshot["run"].get("reaction_input") or {}).get("context", {}).get("max_route_steps", 8),
        "mechanisms": documents["02_mechanisms.json"]["mechanism_hypotheses"],
        "protein_discovery_tasks": discovery_tasks(documents["02_mechanisms.json"]["mechanism_hypotheses"]),
        "route_assembly_tasks": mechanism_route_coverage(documents["02_mechanisms.json"]["mechanism_hypotheses"], []),
        "counterfactual_bridges": [
            {
                "hypothesis_id": item["hypothesis_id"],
                **dict(item.get("counterfactual_bridge") or {}),
            }
            for item in documents["02_mechanisms.json"]["mechanism_hypotheses"]
        ],
        "cofactors": documents["03_capabilities.json"]["cofactor_candidates"],
        "catalytic_capabilities": documents["03_capabilities.json"]["catalytic_capabilities"],
        "evidence_ledger": list({json.dumps(row, sort_keys=True, ensure_ascii=False): row
            for name in ("01_reaction_analysis.json", "02_mechanisms.json", "03_capabilities.json")
            for row in documents[name].get("evidence_ledger", [])}.values()),
        "knowledge_gaps": [row["statement"] for row in documents["02_mechanisms.json"]["evidence_ledger"]
                           if row["kind"] == "unknown"],
        "candidate_generation": {
            "mode": "chemistry_first_before_wiki_evidence" if draft else "awaiting_chemistry_first_draft",
            "draft": draft,
            "prior_candidates": prior,
            "prior_wiki_candidates": prior_wiki,
            "draft_count": len(draft),
            "draft_digest": hashlib.sha256(
                json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest(),
            "ranking_policy": (
                "草案是防锚定基线，不是封闭候选池；已读 Wiki 可以发现草案外候选。"
                "全部候选按目标净反应、底物/产物特异性、辅因子计量、物料闭合和冲突统一比较，"
                "Wiki 命中本身不提供排序加分。"
            ),
        },
    }
    data["context_digest"] = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return data


DRAFT_DISPOSITION_SCHEMA = {
    "type": "object",
    "required": ["proposal_id", "decision", "reason"],
    "properties": {
        "proposal_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "decision": {"enum": ["merged", "excluded"]},
        "replacement_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "reason": {"type": "string", "minLength": 1, "maxLength": 300},
        "identity_basis": {"type": "string", "minLength": 1, "maxLength": 240},
        "scope_basis": {"type": "string", "minLength": 1, "maxLength": 300},
        "exclusion_basis": {"enum": ["chemical_conflict", "target_mismatch", "insufficient_identity", "budget", "functional_overlap"]},
        "identity_issue": {"enum": ["missing_name", "ambiguous_identity", "identity_conflict"],
            "description": "仅身份排除使用；已具名的原始提名不能填 missing_name；歧义或冲突另写 identity_basis，未核实不是未提名。"},
    },
    "additionalProperties": False,
}


def candidate_identity_baseline(generation):
    """当前草案与跨修订提名合并成身份核对清单，不合并不同 ID 的蛋白。"""
    rows = {row["proposal_id"]: row for row in generation.get("prior_candidates", [])}
    for row in generation.get("draft", []):
        previous = rows.get(row["proposal_id"])
        if previous and previous["enzyme_name"] != row["enzyme_name"]:
            raise ValueError("candidate draft ID conflicts with a prior protein nomination")
        rows[row["proposal_id"]] = {**(previous or {}), **row}
    return list(rows.values())


def build_draft_dispositions(generation, candidates, decisions=()):
    """记录草案去向；只验引用与合并材料，不替模型证明催化等价。"""
    draft = {row["proposal_id"]: row for row in candidate_identity_baseline(generation)}
    selected = {}
    retained = {}
    for candidate in candidates:
        for key in [candidate["candidate_id"], candidate.get("proposal_id"), *candidate.get("card_ids", [])]:
            if key:
                selected[key] = candidate
        if candidate.get("proposal_id") in draft and (candidate.get("candidate_kind") != CAPABILITY
                or draft[candidate["proposal_id"]].get("identity_level") in {"family", "capability"}):
            retained[candidate["proposal_id"]] = candidate
    explicit = {}
    for raw in decisions:
        row = dict(raw)
        proposal = row["proposal_id"]
        if proposal not in draft or proposal in explicit or proposal in retained:
            raise ValueError("draft disposition must identify a distinct omitted proposal")
        if row["decision"] == "merged":
            target = selected.get(row.get("replacement_id"))
            if target and draft[proposal].get("identity_level") in {"protein", "named_enzyme"}:
                raise ValueError(
                    f"unsafe_candidate_merge:{proposal}: 具体命名候选不能仅凭名称、EC、家族或反应式"
                    "合并到另一候选。请保留原 proposal_id，按 model_enzyme 单独提交并标注未核实；"
                    "若不适合目标，提交 excluded 与针对目标的理由。当前草案协议没有稳定蛋白身份字段，"
                    "不能证明跨候选蛋白身份等价。"
                )
            if not target:
                row = {
                    "proposal_id": proposal,
                    "decision": "unassessed",
                    "reason": "合并未确认：未提供已选替换候选；保留为未评估草案。",
                }
            elif (draft[proposal].get("identity_level") in {"family", "capability"}
                  and target.get("candidate_kind") == CAPABILITY
                  and target.get("identity_level") == "capability"):
                # 能力范围承接不是蛋白实体合并；范围由模型明确给出，程序不按名称猜等价。
                if not str(row.get("scope_basis") or "").strip():
                    row = {"proposal_id": proposal, "decision": "unassessed",
                           "reason": "能力承接未确认：缺少 scope_basis，不能推定两个目标能力范围相同。"}
                else:
                    row["replacement_id"] = target["candidate_id"]
                    row.setdefault("identity_basis", "仅承接家族探索方向的目标能力，不主张任何具体蛋白身份等价")
            elif not row.get("identity_basis", "").strip() or not row.get("scope_basis", "").strip():
                row = {
                    "proposal_id": proposal,
                    "decision": "unassessed",
                    "reason": "合并未确认：缺少身份等价或完整目标范围依据；保留为未评估草案。",
                }
            elif draft[proposal].get("identity_level") == "family" and target.get("identity_level") != "family":
                row = {"proposal_id": proposal, "decision": "unassessed",
                       "reason": "家族探索方向不能合并为单个蛋白；应分别保留或提交独立淘汰依据"}
            else:
                row["replacement_id"] = target["candidate_id"]
        elif row["decision"] != "excluded":
            raise ValueError("unknown draft disposition")
        else:
            issue = exclusion_disposition_issue(draft[proposal], row)
            if issue:
                # 只撤销未成立的排除声明，不补酶、不改分、不重试；原始请求仍在调用记录。
                row = {"proposal_id": proposal, "decision": "unassessed",
                       "reason": issue + "；保持待评估，不自动进入推荐，不表示目标活性已验证。"}
        if not row.get("reason", "").strip():
            raise ValueError("draft disposition needs an explicit reason")
        explicit[proposal] = row
    return [
        {"proposal_id": proposal, "decision": "retained",
         "replacement_id": retained[proposal]["candidate_id"],
         "reason": ("同一草案 ID 已在未匹配能力层保留，不代表具体蛋白已发现；目标活性仍待核实"
                    if retained[proposal].get("candidate_kind") == CAPABILITY else
                    "草案身份仍在候选中；角色和排序另行审核")}
        if proposal in retained else explicit.get(proposal, {
            "proposal_id": proposal, "decision": "unassessed",
            "reason": "未提交逐项处置；不能从知识缺口自由文本推定已合并或淘汰",
        }) for proposal in draft
    ]


def exclusion_disposition_issue(nomination, decision):
    """核对排除的结构化前提，不用自由文本关键词替模型判断化学适配。"""
    basis = decision.get("exclusion_basis")
    named = (nomination.get("identity_level") in {"protein", "named_enzyme"}
             or bool(nomination.get("specific_name") or nomination.get("prior_specific_name")))
    if basis == "functional_overlap":
        return "功能重叠不证明蛋白身份相同，也不是目标步骤的排除依据"
    if basis == "budget":
        return "预算或展示限额只说明尚未完成比较，不构成科学排除"
    if basis == "insufficient_identity":
        kind = decision.get("identity_issue")
        if named and kind in (None, "missing_name"):
            return "原始提名已具名，不能用未具名或笼统未核实排除；具体身份歧义或冲突尚未说明"
        if kind not in {"missing_name", "ambiguous_identity", "identity_conflict"} or not str(decision.get("identity_basis") or "").strip():
            return "身份排除缺少明确 identity_issue 与 identity_basis，未核实不等于没有提名"
    elif named and basis not in {"chemical_conflict", "target_mismatch"}:
        return "具体蛋白排除类别未确认；同一步已有其他蛋白不证明身份冗余"
    if named and basis in {"chemical_conflict", "target_mismatch"} and not str(decision.get("scope_basis") or "").strip():
        return "化学排除缺少 scope_basis：须说明该候选与目标步骤的独立矛盾，不能只用已有功能代表"
    return None


def ranking_fields(spec, claims):
    """逐项封存排序来源；Wiki 引文真实性不等于对目标反应的语义证明。"""
    if "ranking_factors" not in spec:
        return {"ranking_reason": spec["ranking_reason"]} if spec.get("ranking_reason") else {}
    factors = spec["ranking_factors"]
    Draft202012Validator(RANKING_FACTORS_SCHEMA).validate(factors)
    normalized = lambda text: re.sub(r"\s+", "", str(text))
    labels = {"wiki": "Wiki原文依据（非目标实证）", "model_knowledge": "模型知识（未经Wiki核实）", "inference": "推断（待验证）"}
    for factor in factors:
        if factor["source"] == "wiki":
            refs = require_wiki_refs(factor["refs"], claims)
            if any(normalized(factor["quote"]) not in normalized(claims[ref]["statement"]) for ref in refs):
                raise ValueError("ranking Wiki quote must occur in each cited authorized fragment; keep unsupported knowledge as model_knowledge or inference, never fabricate a citation")
    return {"ranking_factors": copy.deepcopy(factors),
            "ranking_reason": "；".join(labels[factor["source"]] + "：" + factor["statement"] for factor in factors)}


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("score must be a number from 0 to 100")
    return value


_COMPONENT_FUNCTIONS = {"oxidation", "dephosphorylation", "dehydration", "alternative_termination", "activation", "other"}
_SUBSTRATE_RELATIONS = {"reported_exact", "mechanistic_analogy", "unresolved"}


# 提交时枚举修复：Agent 偶尔会写入同义词，而非 Schema 枚举值。
# 仅映射已知同义词；其他值保持不变，让真实非法值仍然校验失败。
_CONFIDENCE_ALIASES = {
    "high": "high", "medium": "medium", "mid": "medium", "moderate": "medium", "low": "low",
    "unresolved": "unresolved", "unknown": "unresolved", "none": "unresolved",
    "n/a": "unresolved", "na": "unresolved", "pending": "unresolved",
    "undetermined": "unresolved", "undecided": "unresolved",
    "高": "high", "中": "medium", "低": "low",
    "未知": "unresolved", "待定": "unresolved", "不确定": "unresolved",
}
_CANDIDATE_ROLE_ALIASES = {
    "primary": "primary", "main": "primary", "main_candidate": "primary",
    "primary_candidate": "primary", "core": "primary", "top": "primary",
    "route_component": "route_component", "routecomponent": "route_component",
    "component": "route_component", "component_candidate": "route_component",
    "partial": "route_component",
    "diagnostic_control": "diagnostic_control", "diagnostic": "diagnostic_control",
    "control": "diagnostic_control", "diagnostic_candidate": "diagnostic_control",
    "主候选": "primary", "路线组件": "route_component", "诊断对照": "diagnostic_control",
    # 现场：模型把提示词里的描述性说法“低置信度探索候选”当成角色枚举，直接传
    # candidate_role="exploratory_candidate"，导致候选被整条丢弃。这类候选的语义
    # 是“与主候选竞争完整目标反应”，因此归一为 primary；只承担局部步骤的情况仍
    # 会在构建阶段按 mechanism_ids/component_function 规则重新分层。
    "exploratory_candidate": "primary", "exploratory": "primary",
    "exploration": "primary", "exploratory_primary": "primary",
    "exploratory_route_component": "route_component",
    "exploratory_component": "route_component",
    "exploratory_diagnostic_control": "diagnostic_control",
    "探索候选": "primary", "探索方向候选": "primary",
}
CANDIDATE_ROLE_INPUTS = tuple(_CANDIDATE_ROLE_ALIASES)
_COMPONENT_FUNCTION_ALIASES = {
    "oxidation": "oxidation", "oxidoreduction": "oxidation", "redox": "oxidation",
    "dephosphorylation": "dephosphorylation", "dephosphorylate": "dephosphorylation",
    "phosphatase": "dephosphorylation",
    "dehydration": "dehydration", "dehydratase": "dehydration", "elimination": "dehydration",
    "alternative_termination": "alternative_termination", "termination": "alternative_termination",
    "alternative_release": "alternative_termination",
    "activation": "activation", "activation_step": "activation",
    "other": "other", "unknown": "other", "unresolved": "other", "none": "other",
    "氧化": "oxidation", "脱磷酸": "dephosphorylation", "脱水": "dehydration",
    "替代终止": "alternative_termination", "活化": "activation", "其他": "other",
}
_SUBSTRATE_RELATION_ALIASES = {
    "reported_exact": "reported_exact", "exact": "reported_exact",
    "reported": "reported_exact", "same_substrate": "reported_exact",
    "mechanistic_analogy": "mechanistic_analogy", "analogy": "mechanistic_analogy",
    "analogous": "mechanistic_analogy",
    "unresolved": "unresolved", "unknown": "unresolved", "none": "unresolved",
    "待核对": "unresolved", "未确定": "unresolved",
}
_ROUTE_TYPE_ALIASES = {
    "direct": "direct", "direct_route": "direct",
    "engineered_single_enzyme": "engineered_single_enzyme",
    "engineered_single": "engineered_single_enzyme", "single_enzyme": "direct",
    "engineered": "engineered_single_enzyme",
    "multi_enzyme": "multi_enzyme", "multi": "multi_enzyme",
    "multienzyme": "multi_enzyme", "multi_step": "multi_enzyme",
}
# 模型生成的 Schema 外辅助字段转入缺口记录，不因此丢弃整个候选或路线。
_IGNORED_SUBMISSION_KEYS = ("evidence_uncertainty", "target_activity_status")
_CANDIDATE_ENUM_FIELDS = (
    ("candidate_role", _CANDIDATE_ROLE_ALIASES),
    ("confidence", _CONFIDENCE_ALIASES),
    ("component_function", _COMPONENT_FUNCTION_ALIASES),
    ("substrate_relation", _SUBSTRATE_RELATION_ALIASES),
)
_ROUTE_ENUM_FIELDS = (
    ("route_type", _ROUTE_TYPE_ALIASES),
    ("confidence", _CONFIDENCE_ALIASES),
)


def _normalize_enum(value, aliases):
    text = str(value or "").strip()
    if not text:
        return text
    return aliases.get(text.casefold().replace("-", "_").replace(" ", "_"), text)


def _repair_enums(spec: dict[str, Any], fields) -> list[str]:
    """Normalize synonym enums in place; return human-readable audit notes."""

    notes = []
    for field, aliases in fields:
        if field not in spec:
            continue
        original = spec[field]
        repaired = _normalize_enum(original, aliases)
        if repaired != original:
            spec[field] = repaired
            notes.append(f"{field} 由 {original!r} 归一为 {repaired}")
    for key in _IGNORED_SUBMISSION_KEYS:
        if key in spec:
            value = spec.pop(key)
            notes.append(f"忽略非 schema 字段 {key}={value!r}")
    return notes


def require_candidate_lineage(spec, draft):
    """冻结 ID 和名称不得漂移；库外新提名必须显式说明发现来源，不按名称猜身份。"""
    if not spec.get("proposal_id"):
        return
    originals = {row["proposal_id"]: row for row in draft}
    if spec.get("candidate_kind") == CAPABILITY or spec.get("name_basis") == "designed_construct":
        if spec["proposal_id"] in originals:
            raise ValueError(
                f"candidate identity drift: {spec['proposal_id']} is a frozen enzyme proposal; "
                "capability/design must use its own ID, not a frozen enzyme proposal_id. "
                "已知或用户提供的具名工程变体不等于假想设计：若仍指原酶，保留原 ID/名称，"
                "使用 established_name 并说明 name_evidence；用户来源须提供逐字输入引文。"
                "若确为本轮假想设计，另建能力 ID，原提名另行处置。不要自动改类或换名绕过校验。")
        return
    if not originals:
        return  # 兼容没有独立提名阶段的历史调用。
    identifier = spec["proposal_id"]
    if identifier in originals:
        original = originals[identifier]
        if original.get("identity_level") == spec.get("identity_level") == "family":
            # 家族标签的空格/连字符差异不改变身份；不用于具体蛋白、突变体或别名猜测。
            key = lambda value: "".join(c for c in str(value).casefold() if c.isalnum())
            if key(spec.get("enzyme_name")) == key(original.get("enzyme_name")):
                spec["enzyme_name"] = original["enzyme_name"]
        if spec.get("enzyme_name") != original.get("enzyme_name"):
            raise ValueError(
                f"candidate identity drift: {identifier} must keep frozen enzyme_name={original.get('enzyme_name')!r}; "
                "do not rename or substitute a protein under an existing ID")
        return
    if not str(spec.get("discovery_reason") or "").strip():
        raise ValueError(
            "new proposal_id requires discovery_reason explaining a genuinely new candidate; "
            "an existing frozen candidate must reuse its original proposal_id and enzyme_name")
    if any(spec.get("enzyme_name") == row.get("enzyme_name") for row in draft):
        raise ValueError("same frozen enzyme_name cannot be resubmitted under a new proposal_id")


def normalize_candidate_source(spec, context):
    """核对用户归属；已有独立草案可撤销错误归属，不能据此改蛋白或提升证据。"""
    name, quote = spec.get("user_provided_name"), spec.get("user_input_quote")
    if not name and not quote:
        return []
    if name and quote and name in quote and quote in context.get("user_input_text", ""):
        return []
    baseline = candidate_identity_baseline(context.get("candidate_generation", {}))
    original = next((row for row in baseline if row.get("proposal_id") == spec.get("proposal_id")), {})
    baseline_match = bool(original.get("enzyme_name") and original["enzyme_name"] == spec.get("enzyme_name"))
    explicit_model_source = (spec.get("name_basis") in {"established_name", "uncertain"}
                             and bool(spec.get("enzyme_name")))
    if spec.get("candidate_kind") != MODEL_ENZYME or not (baseline_match or explicit_model_source):
        raise ValueError("unverified user attribution: requires exact name and quote or a traceable model nomination")
    spec.pop("user_provided_name", None)
    spec.pop("user_input_quote", None)
    note = "用户指定归属未核对通过；按已声明模型来源保留原 ID 和名称，身份和目标活性仍未核实"
    spec["knowledge_gap"] = "；".join(filter(None, [spec.get("knowledge_gap", ""), note]))[:400]
    return [note]


def normalize_candidate_submission(spec: dict[str, Any]) -> list[str]:
    """在候选校验前修正可无损归一化的枚举与格式差异。"""

    # 未引用硬冲突证据时，省略与显式 false 等价；true 仍由后续强制核对引用。
    spec.setdefault("hard_conflict", False)
    spec.setdefault("engineering_plausibility", "unknown")
    # 省略辅助信息只表示未提供，绝不自动补成高置信度或已验证。
    spec.setdefault("confidence", "unresolved")
    spec.setdefault("contradiction_refs", [])
    spec.setdefault("conflict_reason", "")
    spec.setdefault("component_function", "other")
    if spec.get("candidate_kind") == MODEL_ENZYME and spec.get("name_basis") == "target_description":
        raise ValueError("target descriptions require unmatched_capability: submit required_capability and candidate_role=route_component with a distinct capability proposal_id; keep the frozen enzyme ID unchanged")
    if spec.get("card_id") or spec.get("candidate_kind") == WIKI_ENZYME:
        spec.setdefault("substrate_relation", "unresolved")
        spec.setdefault("native_reaction_quote", "")
    notes = _repair_enums(spec, _CANDIDATE_ENUM_FIELDS)
    if spec.get("candidate_kind") in {MODEL_ENZYME, CAPABILITY} and spec.get("engineering_plausibility") in {"high", "medium", "low"}:
        notes.append(f"未核实候选的工程可行性 {spec['engineering_plausibility']} 未作为事实采用，按 unknown 交付")
        spec["engineering_plausibility"] = "unknown"
    return notes


def inherit_route_ledger(previous, submitted):
    """完整新分步式自行加和；不把历史账本拼接成模型本轮的显式声明。"""
    steps = submitted.get("steps", [])
    if ("stoichiometry" not in submitted and "stoichiometry" in previous
            and not (steps and all(step.get("equation") is not None for step in steps))):
        submitted["stoichiometry"] = copy.deepcopy(previous["stoichiometry"])


def normalize_route_submission(spec: dict[str, Any]) -> list[str]:
    """在路线校验前修正可无损归一化的枚举与格式差异。"""
    if "steps" in spec and "step_count" in spec:
        if type(spec["step_count"]) is not int or spec["step_count"] != len(spec["steps"]):
            raise ValueError("step_count does not match actual route steps; no step was added or removed")
        spec.pop("step_count")  # 已复核的冗余计数，不是新增科学步骤。
    spec.setdefault("confidence", "unresolved")
    spec.setdefault("contradiction_refs", [])
    spec.setdefault("unsupported_intermediates", [])
    return _repair_enums(spec, _ROUTE_ENUM_FIELDS)


def resolve_route_step_index(step, explicit_index, part):
    """恢复步骤位置，不猜测反应顺序、不覆盖不同的已存步骤。"""
    indices = [] if explicit_index is None else [explicit_index]
    for key in ("step_index", "step", "order"):
        if key in step:
            indices.append(step.pop(key))
    count = part["header"]["step_count"]
    if indices:
        if any(type(value) is not int or not 1 <= value <= count for value in indices):
            raise ValueError("step_index must belong to declared route step_count")
        if len(set(indices)) != 1:
            raise ValueError("conflicting route step indices; no step was overwritten")
        return indices[0]
    matches = [int(index) for index, saved in part["steps"].items() if saved == step]
    if len(matches) == 1:
        return matches[0]  # 完全相同的重复提交是幂等操作，不追加成另一步。
    if matches:
        raise ValueError("identical steps occupy multiple positions; provide step_index")
    missing = [index for index in range(1, count + 1) if str(index) not in part["steps"]]
    if len(missing) == 1:
        return missing[0]
    raise ValueError(f"ambiguous route step position; provide step_index (unfilled positions: {missing}); no step was overwritten")


def normalize_route_step_binding(step, candidates, *, require_selected=False, staged_routes=()):
    """只按已选候选的精确 ID 恢复引用字段，不按名称或功能猜催化者。"""
    candidates = list(candidates)
    for route in staged_routes:
        for saved in route.get('steps', []):
            capability = saved.get('required_capability')
            if capability:
                proposal = 'cap-' + hashlib.sha256(str(capability).encode()).hexdigest()[:16]
                if not any(row.get('proposal_id') == proposal for row in candidates):
                    candidates.append({'proposal_id': proposal, 'candidate_kind': CAPABILITY,
                                       'required_capability': capability})
    if step.get("step_kind") == "nonenzymatic":
        if any(key in step for key in ("candidate_id", "candidate_ref", "card_id", "proposal_id", "required_capability", "intended_proposal_id", "substitution_basis")):
            raise ValueError("nonenzymatic steps cannot bind an enzyme or capability identity")
        if not str(step.get("condition_note") or "").strip():
            raise ValueError("nonenzymatic steps require explicit conditions and feasibility limits")
        return []
    alias = step.get("candidate_id") or step.get("candidate_ref")
    if alias:
        matches = [row for row in candidates if alias in (
            row.get("candidate_id"), row.get("card_id"), row.get("proposal_id"),
            "candidate-" + str(row.get("card_id", "")).lower() if row.get("card_id") else None,
            "candidate-proposed-" + str(row.get("proposal_id", "")) if row.get("proposal_id") else None)]
        if len(matches) != 1:
            raise ValueError("route candidate reference must match exactly one selected identity")
        row = matches[0]
        if row.get('candidate_kind') == CAPABILITY:
            if any(step.get(key) and step[key] != row.get(key) for key in ('card_id', 'proposal_id')):
                raise ValueError('capability reference conflicts with explicit catalyst identity')
            if step.get('required_capability') not in (None, row['required_capability']):
                raise ValueError('capability reference conflicts with explicit required capability')
            for key in ('candidate_id', 'candidate_ref', 'proposal_id'):
                step.pop(key, None)
            step['required_capability'] = row['required_capability']
            return []
        field = "card_id" if row.get("card_id") else "proposal_id"
        if any(step.get(key) and step[key] != row.get(key) for key in ("card_id", "proposal_id")):
            raise ValueError("route candidate reference conflicts with explicit catalyst identity")
        step[field] = row[field]
        step.pop("candidate_id", None)
        step.pop("candidate_ref", None)
    supplied = [(key, step[key]) for key in ('card_id', 'proposal_id') if step.get(key)]
    if len(supplied) != 1 or step.get('required_capability'):
        return []
    field, identifier = supplied[0]
    matches = [row for row in candidates if identifier in (row.get('card_id'), row.get('proposal_id'))]
    if len(matches) != 1:
        if require_selected:
            raise ValueError("route step must reference exactly one selected candidate; copy its ID from candidate_ids receipt or submit required_capability without an enzyme ID")
        return []
    row = matches[0]
    expected = 'card_id' if row.get('candidate_kind', WIKI_ENZYME) == WIKI_ENZYME else 'proposal_id'
    if expected == field or row.get(expected) != identifier:
        return []
    step.pop(field)
    step[expected] = identifier
    return [f'按已选候选精确 ID 将引用字段 {field} 恢复为 {expected}；未更换催化者']


_MECHANISM_ID = re.compile(r"^M(?:10|[1-9])$")


def uncompared_anchor_notes(context, candidate_specs):
    """锚点只保留为待比较证据；程序不生成身份、分数或科学候选。"""
    compared = {row.get("card_id") for row in candidate_specs if row.get("card_id")}
    anchors = sorted({card_id for mechanism in context.get("mechanisms", [])
                      for card_id in mechanism.get("knowledge_anchor_card_ids", [])} - compared)
    return [f"机制锚点卡 {card_id} 未被模型逐一比较目标催化范围；仅保留为机制证据，不自动补入候选。"
            for card_id in anchors]


def route_chemistry_check(step):
    """记录模型的构型及电子检查；明确冲突的路线不提交，未知仍可探索。"""
    check = step.get("chemistry_check")
    if check is None:
        return {}
    if (check.get("stereochemistry") not in {"compatible", "unknown", "conflict"}
            or check.get("electron_balance") not in {"balanced", "unknown", "conflict"}
            or not str(check.get("basis") or "").strip()):
        raise ValueError("chemistry_check requires valid states and an explicit basis")
    if "conflict" in (check["stereochemistry"], check["electron_balance"]):
        raise ValueError("route step has unresolved stereochemistry or electron balance conflict; revise or exclude this route")
    # 这里只检查模型声明的一致性；未做原子映射，不能证明切键或区域选择性正确。
    for field in ("bond_topology", "domain_assignment", "regioselectivity"):
        if field in check and check[field] not in {"compatible", "unknown", "conflict"}:
            raise ValueError(f"invalid chemistry_check state: {field}")
        if check.get(field) == "conflict":
            raise ValueError(f"route step has unresolved {field} conflict; revise or exclude this route")
    for field in ("broken_bonds", "retained_bonds"):
        bonds = check.get(field, [])
        if not isinstance(bonds, list) or len(bonds) > 3 or any(
                not isinstance(bond, str) or not bond.strip() or len(bond) > 96 for bond in bonds):
            raise ValueError(f"invalid chemistry_check bond labels: {field}")
    if set(b.strip() for b in check.get("broken_bonds", [])) & set(
            b.strip() for b in check.get("retained_bonds", [])):
        raise ValueError("the same bond cannot be broken and retained")
    scope = check.get("catalyst_scope")
    if scope is not None and scope not in {"supported", "hypothesis", "unknown", "analogy_only"}:
        raise ValueError("invalid catalyst_scope")
    if scope == "analogy_only":
        raise ValueError("local analogy is not an actual route catalyst; retain as mechanism evidence or unmatched capability")
    return {"chemistry_check": dict(check)}


def route_catalyst_binding(step, candidate, draft):
    """保留草案催化者与实际催化者；只验引用，不替模型判定底物适配。"""
    proposal = step.get("intended_proposal_id")
    basis = str(step.get("substitution_basis") or "").strip()
    analogy = str(step.get("substrate_analogy") or "").strip()
    if not proposal:
        # 旧客户端常将底物外推写入 substitution_basis；没有草案替换对象时，
        # 将其明确归入底物类比，不猜造原候选 ID，也不证明实际活性。
        analogy = "；".join(dict.fromkeys(value for value in (analogy, basis) if value))
        if len(analogy) > 1200:
            raise ValueError("substrate_analogy exceeds 1200 characters; preserve the key substrate differences concisely")
        return {"substrate_analogy": analogy} if analogy else {}
    original = next((row for row in draft if row["proposal_id"] == proposal), None)
    if original is None:
        raise ValueError("intended_proposal_id must reference the frozen candidate draft")
    if candidate.get("proposal_id") != proposal and not basis:
        raise ValueError("changing a draft catalyst requires substrate-specific substitution_basis")
    return {"intended_proposal_id": proposal,
            "substitution_basis": basis or "沿用原草案催化者；目标活性另行审核",
            **({"substrate_analogy": analogy} if analogy else {})}


def _selection_key(spec):
    card_id, proposal_id = spec.get("card_id"), spec.get("proposal_id")
    if bool(card_id) == bool(proposal_id):
        raise ValueError("provide exactly one of card_id or proposal_id")
    if proposal_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", proposal_id):
        raise ValueError("invalid proposal_id")
    return ("wiki", card_id) if card_id else ("proposal", proposal_id)


def preserve_candidate_identity(spec, previous, cards, claims):
    """只继承同一候选已核实的身份；不继承旧评分、辅因子或目标活性。"""
    result = copy.deepcopy(spec)
    result.pop("prior_specific_name", None)  # 历史名称由本运行旧记录推导，不采用模型回填。
    if result.get("candidate_kind") == MODEL_ENZYME and result.get("identity_binding"):
        # 身份级别由当前授权的精确名称/物种/UniProt 绑定推导，不由回填枚举决定。
        # 完整复用同一核实函数；不接受失效引用、不改变蛋白或目标活性状态。
        if result.get("identity_level") not in (None, "named_enzyme", "protein"):
            raise ValueError("a family/capability cannot carry a protein identity binding")
        verify_identity_binding({**result, "identity_level": "protein"}, cards, claims)
        result["identity_level"] = "protein"
    if not previous or result.get("candidate_kind") != MODEL_ENZYME:
        return result
    if result.get("enzyme_name") != previous.get("enzyme_name"):
        raise ValueError("candidate ID cannot change protein identity while restoring its identity binding")
    if previous.get("identity_level"):
        result.setdefault("identity_level", previous["identity_level"])
    if previous.get("specific_name"):
        if result.get("specific_name") and result["specific_name"] != previous["specific_name"]:
            raise ValueError("specific_name cannot change protein identity under an existing candidate ID")
        if result.get("identity_level") == "family":
            # 明确只保留家族时，不能塞回旧具体名制造自相矛盾，再在提交时丢掉整条候选。
            result.pop("specific_name", None)
            result["prior_specific_name"] = previous["specific_name"]
        else:
            result["specific_name"] = previous["specific_name"]
            result["name_evidence"] = result.get("name_evidence") or previous.get("name_evidence", "")
    elif previous.get("prior_specific_name"):
        if result.get("specific_name") and result["specific_name"] != previous["prior_specific_name"]:
            raise ValueError("specific_name cannot change protein identity under an existing candidate ID")
        result["prior_specific_name"] = previous["prior_specific_name"]
    old = previous.get("identity_binding")
    verified = previous.get("identity_verification") or {}
    if not old and verified.get("status") == "wiki_identity_linked":
        old = {key: verified[key] for key in ("card_id", "refs", "name_quote", "organism_quote", "basis")}
    if old and result.get("identity_level") == "protein":
        # 原文必须仍在当前授权包中；不从旧日志恢复失效引用，更不凭名称推断链接。
        if old.get("card_id") in cards and set(old.get("refs", [])) <= set(claims):
            restored = verify_identity_binding({**result, "identity_binding": old}, cards, claims)
            if result.get("identity_binding"):
                explicit = verify_identity_binding(result, cards, claims)
                if explicit["enzyme_id"] != restored["enzyme_id"]:
                    raise ValueError("identity_binding cannot replace a protein under an existing proposal_id")
            else:
                result["identity_binding"] = copy.deepcopy(old)
    return result


def verify_identity_binding(spec, cards, claims):
    """只核对已读蛋白身份；不复制天然辅因子、反应或活性评分到目标假说。"""
    binding = spec["identity_binding"]
    card_id = binding.get("card_id")
    card = cards.get(card_id)
    if (spec.get("candidate_kind") != MODEL_ENZYME or spec.get("identity_level") != "protein"
            or not card or card.get("identity_level", "protein") != "protein"
            or not str(card.get("enzyme_id", "")).startswith("UniProt:")):
        raise ValueError("identity_binding requires a named protein and an authorized UniProt protein card")
    refs = require_wiki_refs(binding.get("refs") or [], claims)
    if not refs or any(not ref.startswith(card_id + ":") for ref in refs):
        raise ValueError("identity_binding refs must belong to its already-read protein card")
    name = str(binding.get("name_quote") or "").strip()
    organism = str(binding.get("organism_quote") or "").strip()
    proposal = " ".join(str(spec.get(key) or "") for key in ("enzyme_name", "specific_name", "name_evidence"))
    # 显式引用名称与物种，不以同 EC、同家族、模糊相似度或目标反应名判同一蛋白。
    pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
    if (len(name) < 3 or not re.search(pattern, str(card.get("display_name", "")), re.I)
            or not re.search(pattern, proposal, re.I)
            or not organism or organism != str(card.get("organism") or "").strip()
            or not str(binding.get("basis") or "").strip()):
        raise ValueError("identity_binding needs a shared verbatim protein name, exact source organism and explicit identity basis")
    ec = str(card.get("ec_number") or "")
    return {"status": "wiki_identity_linked", "target_activity_status": "unverified",
            "card_id": card_id, "enzyme_id": card["enzyme_id"],
            **({"ec_number": ec} if re.fullmatch(r"[1-7]\.[0-9]+\.[0-9]+\.(?:[0-9]+|-)", ec) else {}),
            "refs": refs, "name_quote": name, "organism_quote": organism, "basis": binding["basis"]}


def build_screening_documents(
    context: Mapping[str, Any], *, candidate_specs: Any, route_specs: Any, knowledge_gaps: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """保留模型判断；工具不额外添加辅因子/特征权重或路线惩罚。"""
    run_id = context["run_id"]
    candidate_specs = copy.deepcopy(candidate_specs)
    route_specs = copy.deepcopy(route_specs)
    capability_mechanisms = {}
    # 局部类比可启发化学步骤，但不能冒充实际催化者。仅撤销该绑定，保留步骤和缺口。
    # 已声明的化学冲突仍由原检查拒绝；此处不猜具体酶、不补写反应或计量。
    for route in route_specs:
        for position, step in enumerate(route["steps"], 1):
            normalize_route_step_binding(step, candidate_specs)
            if not set(step.get("mechanism_ids") or []) <= {m["hypothesis_id"] for m in context.get("mechanisms", [])}:
                raise ValueError("route step mechanism_ids must refer to current hypotheses")
            check = step.get("chemistry_check") or {}
            if check.get("catalyst_scope") != "analogy_only":
                continue
            original = step.get("card_id") or step.get("proposal_id")
            if not original or step.get("required_capability"):
                raise ValueError("analogy-only step requires exactly one original catalyst binding")
            _selection_key(step)
            source = next((item for item in candidate_specs if original in (item.get("card_id"), item.get("proposal_id"))), None)
            if source is None:
                raise ValueError("analogy-only step must reference a selected source candidate")
            if source.get("hard_conflict"):
                raise ValueError("analogy-only catalyst has an explicit hard conflict; revise or exclude before rebinding")
            original_binding = route_catalyst_binding(step, source, context.get("candidate_generation", {}).get("draft", []))
            if original_binding.get("substrate_analogy"):
                step["substrate_analogy"] = original_binding["substrate_analogy"]
            if original_binding.get("substitution_basis"):
                route.setdefault("unsupported_intermediates", []).append(
                    f"第 {position} 步原类比来源的候选替换说明（不证明能力占位已匹配）：{original_binding['substitution_basis']}")
            # 保留并核对其他化学声明；只将不适用的催化者关联移至缺口。
            retained_check = {**check, "catalyst_scope": "unknown"}
            route_chemistry_check({"chemistry_check": retained_check})
            note = f"第 {position} 步原催化者 {original} 仅提供局部类比，已移出实际催化者；该步能力与酶身份未匹配，目标活性未验证"
            route.setdefault("unsupported_intermediates", []).append(note)
            for key in ("card_id", "proposal_id", "intended_proposal_id", "substitution_basis"):
                step.pop(key, None)
            step.update(required_capability=f"路线 {route['route_id']} 第 {position} 步所需催化能力（详见步骤输入输出）",
                        evidence_refs=[], chemistry_check=retained_check)
            capability_mechanisms[step["required_capability"]] = [identifier for identifier in source.get("mechanism_ids", [])
                if any(m.get("hypothesis_id") == identifier and m.get("route_type") == route.get("route_type")
                       for m in context.get("mechanisms", []))]
    # 能力占位只补齐催化者绑定，不编造具体酶或已验证活性。
    for route in route_specs:
        for step in route["steps"]:
            capability = step.pop("required_capability", None)
            if capability is None:
                continue
            if step.get("card_id") or step.get("proposal_id") or not str(capability).strip():
                raise ValueError("required_capability must be nonempty and cannot accompany enzyme identity")
            proposal = "cap-" + hashlib.sha256(str(capability).encode()).hexdigest()[:16]
            step.setdefault("evidence_refs", [])
            existing = next((row for row in candidate_specs if row.get("proposal_id") == proposal), None)
            if existing and (existing.get("candidate_kind") != CAPABILITY or existing.get("required_capability") != str(capability)):
                raise ValueError("generated capability ID collides with a different candidate; provide a distinct explicit capability ID")
            if existing is None:
                existing = next((row for row in candidate_specs if row.get("candidate_kind") == CAPABILITY
                                 and row.get("required_capability") == str(capability)), None)
            if existing is not None:
                proposal = existing["proposal_id"]
            step["proposal_id"] = proposal
            if existing is None:
                candidate_specs.append({
                    "proposal_id": proposal, "candidate_kind": CAPABILITY,
                    "required_capability": str(capability), "candidate_role": "route_component",
                    "component_function": "other", "mechanism_ids": list(step.get("mechanism_ids") or capability_mechanisms.get(str(capability), [])),
                    "score": route["score"], "confidence": "unresolved",
                    "support_reason": "路线所需能力占位；尚未匹配具体酶",
                    "contradiction_refs": [], "conflict_reason": "", "hard_conflict": False,
                    "engineering_plausibility": "unknown", "knowledge_gap": "催化者身份与目标活性未核实",
                })
    cards = {card["card_id"]: card for card in context["cards"]}
    claims = claim_index(context)
    capabilities_count = sum(s.get("candidate_kind") == CAPABILITY for s in candidate_specs)
    # 内部比较容量与最终展示 Top N 分开；报告仍遵守 result_limits。
    if len(candidate_specs) > 30 or len(route_specs) > MAX_RESULT_LIMIT:
        raise ValueError("candidate or route count exceeds this run's result limit")
    anchor_cards = {
        card_id
        for mechanism in context.get("mechanisms", [])
        for card_id in mechanism.get("knowledge_anchor_card_ids") or []
    }
    gaps = list(knowledge_gaps)
    compared_cards = {spec.get("card_id") for spec in candidate_specs if spec.get("card_id")}
    missing_anchors = sorted(anchor_cards - compared_cards)
    if missing_anchors:
        gaps.append(
            "本轮未完成机制锚点卡逐一比较：" + "、".join(missing_anchors)
            + "；这些卡未被判定为不适合目标反应，候选覆盖不完整。"
        )
    if not candidate_specs and not gaps:
        raise ValueError("an empty candidate set requires a knowledge gap")
    candidates, ledger, seen = [], [], set()
    def claim(kind, reason, refs, identifier):
        row = {"statement_id": identifier, "kind": kind, "statement": reason,
               "source_refs": refs}
        if kind != "hypothesis":
            card_id, path = refs[0].split(":", 1)
            row.update(card_id=card_id, field_path=path)
        ledger.append(row)
        return row

    selected, proposed_names = {}, set()
    for i, spec in enumerate(candidate_specs, 1):
        previous_identity = context.get("identity_bindings", {}).get(spec.get("proposal_id")) or next(
            (row for row in candidate_identity_baseline(context.get("candidate_generation", {}))
             if row.get("proposal_id") == spec.get("proposal_id")
             and (row.get("specific_name") or row.get("prior_specific_name"))), None)
        spec = preserve_candidate_identity(spec, previous_identity, cards, claims)
        require_candidate_lineage(spec, candidate_identity_baseline(context.get("candidate_generation", {})))
        # 设计构建物保持为能力／设计假说；不为尚未存在的融合蛋白创建实体。
        if spec.get("name_basis") == "designed_construct":
            spec["required_capability"] = str(spec.pop("enzyme_name", spec.get("required_capability", "")))
            spec.pop("enzyme_family", None)
            spec.pop("identity_level", None)
            spec.update(candidate_kind=CAPABILITY, candidate_role="route_component",
                        component_function="other", engineering_plausibility="unknown")
        normalize_candidate_source(spec, context)
        input_name = str(spec.get("user_provided_name") or "").strip()
        input_quote = str(spec.get("user_input_quote") or "").strip()
        if input_name or input_quote:
            if (spec.get("candidate_kind") != MODEL_ENZYME or not input_name or not input_quote
                    or input_name not in input_quote or input_quote not in context.get("user_input_text", "")):
                raise ValueError("user-provided candidate requires exact name and quote from current input")
            spec["enzyme_name"] = input_name
        component_function = spec.get("component_function") or "other"
        substrate_relation = spec.get("substrate_relation") or "unresolved"
        if component_function not in _COMPONENT_FUNCTIONS or substrate_relation not in _SUBSTRATE_RELATIONS:
            raise ValueError("invalid component_function or substrate_relation")
        origin = candidate_provenance(spec)
        kind = origin["candidate_kind"]
        name_gap = validate_candidate_name(spec, candidate_identity_baseline(context.get('candidate_generation', {})))
        key = _selection_key(spec)
        if key in seen:
            raise ValueError("duplicate candidate card or proposal_id")
        seen.add(key)
        if kind == WIKI_ENZYME:
            card_id = spec.get("card_id")
            if key[0] != "wiki" or card_id not in cards:
                raise ValueError(f"candidate card evidence is not authorized in current context: {card_id}; restore sealed evidence, do not treat as chemical exclusion")
            card = cards[card_id]
            native_quote = str(spec.get("native_reaction_quote") or "").strip()
            native_reaction = str(card.get("native_reaction") or "")
            if native_quote and native_quote not in native_reaction:
                raise ValueError(
                    f"candidate {card_id} native_reaction_quote must occur verbatim in its native reaction section"
                )
            if substrate_relation == "reported_exact" and not native_quote:
                raise ValueError(
                    f"candidate {card_id} claims an exact reported substrate without a native reaction quote"
                )
            refs = require_wiki_refs(spec["support_refs"], claims)
            foreign_refs = [ref for ref in refs if not ref.startswith(card_id + ":")]
            if foreign_refs:
                own_refs = [ref for ref in claims if ref.startswith(card_id + ":")]
                raise ValueError(
                    f"arguments.candidates[{i - 1}].support_refs: candidate {card_id} must cite its own Wiki record; "
                    f"foreign refs={foreign_refs}; allowed already-read own refs={own_refs}. "
                    "Keep other-enzyme analogies in mechanism/route evidence, not in this candidate's own activity support."
                )
            statuses = {claims[ref].get("activity_status") for ref in refs}
            scope = ("related_only" if statuses == {"related_protein_only"} else
                     "mixed_or_uncertain" if statuses & {"related_protein_only", "attribution_requires_review"} else "wiki_prior")
            origin = candidate_provenance({"candidate_kind": WIKI_ENZYME, "evidence_scope": scope,
                                           "identity_level": card.get("identity_level", "protein")})
            identity = {"candidate_id": f"candidate-{card_id.lower()}",
                        "enzyme_family": card["enzyme_family"], "card_ids": [card_id],
            "enzyme_name": card["display_name"], "enzyme_id": card["enzyme_id"],
                        "ec_number": card["ec_number"], "cofactor_classes": list(card["cofactor_classes"])}
        else:
            if key[0] != "proposal" or any(spec.get(f) for f in ("enzyme_id", "ec_number")):
                raise ValueError("unverified proposals cannot carry Wiki/UniProt/EC identities")
            if spec["engineering_plausibility"] != "unknown":
                raise ValueError("unverified proposals require engineering_plausibility=unknown")
            field = "enzyme_name" if kind == MODEL_ENZYME else "required_capability"
            name = str(spec.get(field) or "").strip()
            if not name or (kind, name.casefold()) in proposed_names:
                raise ValueError(f"{field} is required and must not duplicate another proposal")
            proposed_names.add((kind, name.casefold()))
            if kind == CAPABILITY and spec["candidate_role"] != "route_component":
                raise ValueError("unmatched_capability must be a route_component, not a named enzyme")
            support_refs = spec.get("support_refs") or []
            refs = require_wiki_refs(support_refs, claims) if support_refs else []
            identity = {"candidate_id": f"candidate-proposed-{key[1]}", "proposal_id": key[1],
                        "card_ids": [], "cofactor_classes": list(spec.get("cofactor_classes") or []),
                        "enzyme_family": str(spec.get("enzyme_family") or name) if kind == MODEL_ENZYME else "未匹配酶的催化能力",
                        field: name}
        if spec.get("identity_binding"):
            binding = verify_identity_binding(spec, cards, claims)
            identity["enzyme_id"] = binding["enzyme_id"]
            if binding.get("ec_number"):
                identity["ec_number"] = binding["ec_number"]
            origin = candidate_provenance({**spec, "identity_verification": binding})
        provenance_refs = refs or ([f"input_candidate:{identity['candidate_id']}"] if input_name else [f"model_proposal:{identity['candidate_id']}"])
        support = claim("inference" if kind == WIKI_ENZYME else "hypothesis", spec["support_reason"], provenance_refs, f"{identity['candidate_id']}-support")
        conflicts = []
        conflict_refs = list(spec["contradiction_refs"])
        if conflict_refs:
            conflict_refs = require_wiki_refs(conflict_refs, claims)
            if not spec["conflict_reason"].strip():
                raise ValueError("contradiction refs require an explicit conflict_reason")
            conflicts.append(claim("inference", spec["conflict_reason"], conflict_refs, f"{identity['candidate_id']}-conflict"))
        elif spec["conflict_reason"].strip():
            conflicts.append(claim("unknown" if kind == WIKI_ENZYME else "hypothesis", spec["conflict_reason"], provenance_refs, f"{identity['candidate_id']}-unknown"))
        if spec["hard_conflict"] and not conflict_refs:
            raise ValueError("hard_conflict requires cited conflicting evidence, not missing data")
        mechanism_ids = list(spec.get("mechanism_ids") or [])
        association_gap = ""
        available_mechanisms = {m["hypothesis_id"] for m in context.get("mechanisms", [])}
        if not set(mechanism_ids) <= available_mechanisms:
            raise ValueError("candidate mechanism_ids must refer to submitted hypotheses")
        if kind == WIKI_ENZYME:
            anchored_ids = [
                mechanism["hypothesis_id"]
                for mechanism in context.get("mechanisms", [])
                if set(identity.get("card_ids") or []) & set(mechanism.get("knowledge_anchor_card_ids") or [])
                # 候选/路线 Schema 只接受 M1–M10；上游非标准 ID 不应使合法候选失效。
                and _MECHANISM_ID.fullmatch(str(mechanism.get("hypothesis_id") or ""))
            ]
            late_links = set(mechanism_ids) - set(anchored_ids)
            if late_links:
                association_gap = (
                    "该知识卡在候选筛选阶段才被发现；其与上游机制的关联是筛选阶段提出的待验证桥接，"
                    "不是阶段二已建立的知识锚点"
                )
            # 同一卡可能提供不同机制的局部类比；锚点不是候选机制适配证明。
            # 仅保留明确提交的关联，交给既有科学审核核对辅因子与步骤。
        role = spec["candidate_role"]
        # Wiki 锚点和库外来源均不决定角色；角色理由由同一科学审核核对。
        # 机制关联只提供类比，不证明该蛋白覆盖整个转化；保留模型提交的角色。
        # 单个局部功能不等于端到端单酶路线。
        # 氧化/脱水等功能既可能是完整目标转化，也可能只是局部步骤。
        # 不按功能词或证据来源自动降级；保留声明角色，交给端到端覆盖审核。
        gap = spec["knowledge_gap"].strip()
        if gap:
            gaps.append(gap)
            ledger.append({"statement_id": f"{identity['candidate_id']}-gap", "kind": "unknown", "statement": gap, "source_refs": provenance_refs})
        if association_gap:
            gaps.append(association_gap)
            ledger.append({"statement_id": f"{identity['candidate_id']}-association-gap", "kind": "unknown",
                           "statement": association_gap, "source_refs": provenance_refs})
        # 跨阶段保留事件级不确定性；Wiki 候选身份不证明目标路线可行。
        event_gaps = []
        for mechanism in context.get("mechanisms", []):
            if mechanism["hypothesis_id"] not in mechanism_ids:
                continue
            for event in mechanism.get("event_evidence", []):
                status = event.get("status", "model_hypothesis")
                if status == "local_evidence":
                    continue
                label = {
                    "related_analogy": "类比，目标步骤未验证",
                    "model_hypothesis": "模型假说",
                    "unmatched_capability": "未匹配催化能力",
                    "conflict": "存在证据冲突",
                }.get(status, "证据状态未确定")
                detail = str(event.get("event") or "未说明的催化事件")[:180]
                event_gaps.append(f"{mechanism['hypothesis_id']}：{label}；{detail}")
        candidate_gaps = list(dict.fromkeys(([gap] if gap else []) + ([name_gap] if name_gap else []) + ([association_gap] if association_gap else []) + event_gaps))[:6]
        gaps.extend(item for item in candidate_gaps if item not in gaps)
        candidate = {
            "chemistry_fit_score": chemistry_score(spec),
            **ranking_fields(spec, claims),
            "proposal_rationale": draft_rationale(next((row for row in candidate_identity_baseline(
                context.get("candidate_generation", {})) if row.get("proposal_id") == spec.get("proposal_id")), {})),
            "proposal_rationale_status": "historical_unverified",
            **{key: spec[key] for key in ("name_basis", "name_evidence", "specific_name", "prior_specific_name") if key in spec},
            **identity, **origin, "candidate_role": role,
            "component_function": component_function,
            "substrate_relation": substrate_relation,
            "native_reaction_quote": str(spec.get("native_reaction_quote") or "").strip(),
            "score": _score(spec["score"]), "confidence": spec["confidence"],
            "mechanism_ids": mechanism_ids, "knowledge_gaps": candidate_gaps,
            "supports": [support], "contradictions": conflicts,
            "hard_conflict": spec["hard_conflict"],
            "engineering_plausibility": spec["engineering_plausibility"],
            "sources": provenance_refs,
        }
        candidates.append(candidate)
        selected[key] = candidate
    # 分数和角色由模型提交；同分时保持提交顺序。
    candidates.sort(key=chemistry_rank_key)
    routes, route_ledger, route_ids = [], [], set()
    for i, spec in enumerate(route_specs, 1):
        route_id = spec["route_id"]
        if route_id in route_ids:
            raise ValueError("duplicate route_id")
        route_ids.add(route_id)
        if not 1 <= len(spec["steps"]) <= int(context.get("max_route_steps", 8)):
            raise ValueError("route exceeds this run's max_route_steps")
        steps, supports = [], []
        for j, step in enumerate(spec["steps"], 1):
            if step.get("step_kind") == "nonenzymatic":
                normalize_route_step_binding(step, [])
                refs = require_wiki_refs(step["evidence_refs"], claims) if step["evidence_refs"] else []
                steps.append({"step_kind": "nonenzymatic", "condition_note": step["condition_note"],
                    **route_chemistry_check(step),
                    **({"equation": copy.deepcopy(step["equation"])} if "equation" in step else {}),
                    "step": j, "input": step["input"], "output": step["output"],
                    "candidate_family": "非酶辅助步骤", "card_ids": [], "cofactor_classes": [],
                    "mechanism_ids": list(step.get("mechanism_ids", [])),
                    "evidence_refs": refs or [f"model_step:{route_id}:{j}"],
                    "verification_note": "非酶步骤假说；条件、速率与目标体系可行性待核实，不是酶候选",
                    "material_handoff": step.get("material_handoff") or "辅助步骤物料承接未核实"})
                supports.extend(refs)
                continue
            key = _selection_key(step)
            if key not in selected:
                raise ValueError("route step must reference a selected candidate")
            candidate = selected[key]
            binding = route_catalyst_binding(
                step, candidate, context.get("candidate_generation", {}).get("draft", []))
            chemistry = route_chemistry_check(step)
            origin = candidate_provenance(candidate)
            refs = require_wiki_refs(step["evidence_refs"], claims) if step["evidence_refs"] or origin["candidate_kind"] == WIKI_ENZYME else []
            if origin["candidate_kind"] == WIKI_ENZYME and any(
                not any(ref.startswith(cid + ":") for cid in candidate["card_ids"]) for ref in refs
            ):
                raise ValueError("Wiki route step must cite its actual catalyst, not another enzyme's analogy")
            supports.extend(refs)
            steps.append({
                **origin, **binding, **chemistry, "candidate_id": candidate["candidate_id"],
                **({"equation": copy.deepcopy(step["equation"])} if "equation" in step else {}),
                "step": j, "input": step["input"], "output": step["output"],
                "candidate_family": candidate["enzyme_family"],
                **{field: candidate[field] for field in ("enzyme_name", "enzyme_id", "ec_number", "required_capability") if field in candidate},
                **({"enzyme_name": candidate["specific_name"]} if candidate.get("specific_name") else {}),
                "cofactor_classes": candidate["cofactor_classes"],
                "mechanism_ids": list(step["mechanism_ids"] if "mechanism_ids" in step else candidate["mechanism_ids"]),
                "card_ids": candidate["card_ids"], "evidence_refs": refs or [f"candidate:{candidate['candidate_id']}"],
                "material_handoff": step.get("material_handoff") or "未提供物料衔接说明；需结合相邻步骤核对，不视为已闭合",
            })
        conflicts = list(spec["contradiction_refs"])
        if conflicts:
            require_wiki_refs(conflicts, claims)
        route_gaps = list(spec["unsupported_intermediates"])
        for index, original_step in enumerate(spec["steps"], 1):
            if not original_step.get("material_handoff"):
                route_gaps.append(f"第 {index} 步未提供物料衔接说明；物料连续性待核对")
        for row in steps:
            check = row.get("chemistry_check")
            if check is not None:
                missing = [field for field in ("bond_topology", "domain_assignment", "regioselectivity", "catalyst_scope")
                           if check.get(field) in (None, "unknown", "hypothesis")]
                if missing:
                    route_gaps.append(f"第 {row['step']} 步：切键／催化域／区域选择性／催化者适配待审核（{', '.join(missing)}）；不视为目标活性已证实")
            if check is None:
                route_gaps.append(f"第 {row['step']} 步：未提交构型和电子收支检查；需审核，不能视为已配平")
            elif "unknown" in (check["stereochemistry"], check["electron_balance"]):
                route_gaps.append(f"第 {row['step']} 步：构型／电子收支仍有未知；{check['basis']}")
        route_gaps.extend(f"第 {s['step']} 步：{s['verification_note']}" for s in steps if s.get("candidate_kind") != WIKI_ENZYME)
        route_gaps = list(dict.fromkeys(route_gaps))
        for j, gap in enumerate(route_gaps, 1):
            route_ledger.append({"statement_id": f"{route_id}-gap-{j}", "kind": "unknown",
                                 "statement": gap, "source_refs": [f"route:{route_id}"]})
        account = route_accounting(spec)
        enzyme_steps = [row for row in steps if row.get("step_kind") != "nonenzymatic"]
        routes.append({
            **({"stoichiometry": account["ledger"]} if account["ledger"] is not None else {}),
            **({"balance_review": copy.deepcopy(spec["balance_review"])} if spec.get("balance_review") else {}),
            "route_id": route_id, "route_type": spec["route_type"], "steps": steps,
            "score": _score(spec["score"]), "confidence": spec["confidence"],
            "support_refs": list(dict.fromkeys(supports)), "contradiction_refs": conflicts,
            "unsupported_intermediates": route_gaps,
            "support_status": "partially_supported" if route_gaps else "unresolved",
            "chemistry_fit_score": chemistry_score(spec),
            **ranking_fields(spec, claims),
            "wiki_evidence_score": round(100 * sum(s["candidate_kind"] == WIKI_ENZYME for s in enzyme_steps) / max(1, len(enzyme_steps)), 3),
            "overall_route_score": _score(spec["score"]),
        })
    routes.sort(key=chemistry_rank_key)
    return (
        {"schema_version": "1.0", "run_id": run_id, "evidence_ledger": ledger,
         "enzyme_candidates": candidates, "knowledge_gaps": list(dict.fromkeys(gaps)),
         "candidate_generation": dict(context.get("candidate_generation") or {
             "mode": "legacy_direct_assembly", "draft": [], "draft_count": 0, "draft_digest": "",
             "ranking_policy": "Model-authored ranking; Wiki evidence is not a whitelist or score bonus.",
         }),
         "merge_policy": (
             "The chemistry-first draft is an anti-anchoring baseline, not a closed candidate pool. Read Wiki evidence "
             "may introduce additional candidates; all candidates compete on target-reaction fit, material closure and "
             "conflicts. Wiki retrieval changes evidence state only and adds no hidden ranking bonus."
         )},
        {"schema_version": "1.0", "run_id": run_id, "evidence_ledger": route_ledger, "routes": routes},
    )
