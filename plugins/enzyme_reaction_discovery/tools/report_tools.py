

from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping, Sequence
from .provenance import CAPABILITY, MODEL_ENZYME, WIKI_ENZYME, candidate_provenance, reasoning_provenance
from .result_limits import MAX_RESULT_LIMIT, requested_result_limits
from .audit_tools import require_current_review
from .audit_tools import review_issue_type
from .provenance import candidate_evidence_level
from .screening_tools import mechanism_route_coverage, discovery_tasks, candidate_identity_baseline, exclusion_review_tasks, read_card_exclusion_review, CANDIDATE_REVIEW_RULES
from .reasoning_policy import chemistry_rank_key, chemistry_score, draft_rationale, RANKING_POLICY, revision_owners
from .reasoning_policy import RATIONALE_FIELD_CONTRACT, REVIEW_EVIDENCE_POLICY


_CONFIDENCE = {"high", "medium", "low", "unresolved"}
_ENGINEERING = {"high", "medium", "low", "unknown"}
_ROUTE_TYPES = {"direct", "engineered_single_enzyme", "multi_enzyme"}
_EVIDENCE_KINDS = ("fact", "inference", "hypothesis", "counterevidence", "unknown")
_COFACTOR_LABELS = {
    "thiamine_diphosphate": "ThDP（硫胺素二磷酸）",
    "divalent_metal": "二价金属离子",
    "nad_oxidoreductant": "NAD(P) 类氧化还原辅因子",
    "flavin": "黄素类辅因子",
    "pyridoxal_phosphate": "PLP（磷酸吡哆醛）",
    "cobalamin": "钴胺素类辅因子",
    "tetrahydrofolate": "四氢叶酸",
    "biotin": "生物素",
    "heme": "血红素",
    "iron_sulfur": "铁硫簇",
    "manganese": "锰离子",
    "nickel": "镍离子",
}
_LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低", "unknown": "未知", "unresolved": "未确定"}
_ROUTE_LABELS = {"direct": "直接单酶", "engineered_single_enzyme": "工程化单酶", "multi_enzyme": "多酶路线"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clip(value: Any, limit: int = 280) -> str:
    text = _text(value).replace("\x00", " ")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _enzyme_identity(item: Mapping[str, Any]) -> dict[str, str]:
    """保留显式提名的具体名称；旧产物没有该字段时沿用原酶名。"""
    keys = ("enzyme_name", "enzyme_id", "ec_number")
    result = {key: _text(item[key]) for key in keys if item.get(key)}
    if item.get("specific_name"):
        result["enzyme_name"] = _text(item["specific_name"])
    return result


def _enzyme_label(item: Mapping[str, Any]) -> str:
    if item.get("step_kind") == "nonenzymatic":
        return "非酶辅助步骤"
    name = _text(item.get("required_capability") or item.get("specific_name") or item.get("enzyme_name") or item.get("enzyme_family") or item.get("candidate_family"))
    ec = _text(item.get("ec_number"))
    return f"{name}（EC {ec}）" if ec else name


def candidate_identity_matches(candidate, step):
    """核对显式身份，供科学冲突检查使用；身份相同不代表已承担路线步骤。"""
    stable_match = False
    for key in ("candidate_id", "proposal_id"):
        if candidate.get(key) and step.get(key):
            if candidate[key] != step[key]:
                return False
            stable_match = True
    left, right = candidate.get("enzyme_id"), step.get("enzyme_id")
    if left and right:
        if left != right:
            return False
    if stable_match:
        return True
    if left and right:
        if not str(left).startswith("EC:"):
            return True
    cards, step_cards = set(candidate.get("card_ids", [])), set(step.get("card_ids", []))
    if cards and step_cards:
        return bool(cards & step_cards)
    if str(left or "").startswith("EC:") or str(right or "").startswith("EC:"):
        return False
    # 旧报告缺少稳定 ID 时仅兼容完整名称相等，不用家族、EC 或名称相似度补绑定。
    return bool(candidate.get("enzyme_name") and candidate["enzyme_name"] == step.get("enzyme_name"))


def candidate_step_matches(candidate, step):
    """核对实际催化者绑定，不把类比或非酶步骤当成实际绑定。"""
    if (step.get("step_kind") == "nonenzymatic" or step.get("candidate_kind") == CAPABILITY
            or (step.get("chemistry_check") or {}).get("catalyst_scope") == "analogy_only"):
        return False
    return candidate_identity_matches(candidate, step)


def candidate_route_matches(candidate, routes):
    return [(route, step) for route in routes if route.get("review_disposition") != "exclude"
            for step in route.get("steps", []) if candidate_step_matches(candidate, step)]


def candidate_display_role(candidate, routes, exploratory_routes=()):
    """只派生展示用途；不改变原始角色、候选去留、分数或排序。"""
    role = candidate.get("candidate_role", "primary")
    if role == "diagnostic_control":
        return "机制诊断对照"
    if role == "primary":
        return "单酶探索方向"
    if candidate_route_matches(candidate, routes):
        return ("已绑定辅助／再生步骤（未验证）" if candidate.get("component_function") == "cofactor_regeneration"
                else "已绑定路线步骤（未验证）")
    if candidate_route_matches(candidate, exploratory_routes):
        return "非推荐探索路线步骤"
    if candidate.get("component_function") == "cofactor_regeneration":
        return "辅助／再生线索（未绑定路线）"
    if (candidate.get("evidence_level") or candidate_evidence_level(candidate)) == "Wiki 仅支持机制类比":
        return "机制类比线索（未绑定路线）"
    return "局部催化候选（未绑定路线）"


def candidate_role_review_tasks(candidates, routes):
    """定位未绑定的类比/局部候选并给出同机制比较索引；不自动作化学判断。"""
    tasks = []
    for item in candidates:
        if (item.get("candidate_kind") == CAPABILITY or item.get("hard_conflict")
                or item.get("candidate_role", "primary") == "primary"
                or candidate_route_matches(item, routes)):
            continue
        mechanisms = set(item.get("mechanism_ids") or [])
        peers = [peer for peer in candidates
                 if peer.get("candidate_id") != item.get("candidate_id")
                 and peer.get("candidate_kind") != CAPABILITY and not peer.get("hard_conflict")
                 and mechanisms.intersection(peer.get("mechanism_ids") or [])
                 and (peer.get("candidate_role", "primary") == "primary"
                      or candidate_route_matches(peer, routes))]
        tasks.append({"candidate_id": item["candidate_id"],
                      "mechanism_ids": sorted(mechanisms),
                      "substrate_relation": item.get("substrate_relation", "unresolved"),
                      "compare_candidate_ids": [peer["candidate_id"] for peer in peers],
                      "status": "needs_semantic_review_not_a_confirmed_conflict"})
    return tasks


def _level_label(value: Any) -> str:
    return _LEVEL_LABELS.get(_text(value), _text(value))


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _strings(value: Any, *, count: int = 3, chars: int = 240) -> list[str]:
    result: list[str] = []
    for item in _sequence(value):
        text = _clip(item, chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= count:
            break
    return result


def _confidence(value: Any, default: str = "low") -> str:
    normalized = _text(value).lower()
    return normalized if normalized in _CONFIDENCE else default


def _engineering(value: Any) -> str:
    normalized = _text(value).lower()
    return normalized if normalized in _ENGINEERING else "unknown"


def _score(value: Any) -> float:
    try:
        return round(max(0.0, min(100.0, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0


def _statement_texts(value: Any, *, count: int = 2) -> list[str]:
    rendered: list[str] = []
    for raw in _sequence(value):
        item = _mapping(raw)
        text = _clip(item.get("statement") if item else raw, 260)
        if text and text not in rendered:
            rendered.append(text)
        if len(rendered) >= count:
            break
    return rendered


def _statement_refs(value: Any, *, count: int = 5) -> list[str]:
    refs: list[str] = []
    for raw in _sequence(value):
        item = _mapping(raw)
        for ref in _sequence(item.get("source_refs")):
            text = _clip(ref, 220)
            if text and text not in refs:
                refs.append(text)
            if len(refs) >= count:
                return refs
        card_id = _clip(item.get("card_id"), 64)
        field_path = _clip(item.get("field_path"), 160)
        if card_id:
            ref = f"{card_id}:{field_path}" if field_path else card_id
            if ref not in refs:
                refs.append(ref)
    return refs[:count]


def _mermaid_label(value: Any, limit: int = 90) -> str:
    return _clip(value, limit).replace('"', "'").replace("\n", " ").replace("[", "(").replace("]", ")")


def _route_step_view(step):
    if step.get("step_kind") == "nonenzymatic":
        return {key: copy.deepcopy(step[key]) for key in
                ("step_kind", "condition_note", "equation", "chemistry_check", "verification_note",
                 "step", "input", "output", "candidate_family", "card_ids", "evidence_refs", "material_handoff", "mechanism_ids", "cofactor_classes")
                if key in step}
    return {
        **({"equation": copy.deepcopy(step["equation"])} if "equation" in step else {}),
        **({"chemistry_check": copy.deepcopy(step["chemistry_check"])} if step.get("chemistry_check") else {}),
        **{key: step[key] for key in ("candidate_id", "intended_proposal_id", "substitution_basis", "substrate_analogy") if key in step},
        "evidence_refs": _strings(step.get("evidence_refs"), count=2, chars=240),
        "step": int(step.get("step") or 1),
        "input": _clip(step.get("input"), 100), "output": _clip(step.get("output"), 100),
        "material_handoff": _clip(step.get("material_handoff"), 180),
        "candidate_family": _clip(step.get("candidate_family"), 130),
        **_enzyme_identity(step), **candidate_provenance(step),
        "mechanism_ids": _strings(step.get("mechanism_ids"), count=10, chars=20),
        "cofactor_classes": _strings(step.get("cofactor_classes"), count=4, chars=100),
        "card_ids": _strings(step.get("card_ids"), count=4, chars=64),
        **({"required_capability": _clip(step["required_capability"], 260)} if step.get("required_capability") else {}),
    }


def build_report_context(
    snapshot: Mapping[str, Any],
    *,
    card_summaries: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """将已提交的 01—05 产物压缩为面向审计的紧凑视图。"""

    documents = _mapping(snapshot.get("documents"))
    run = _mapping(snapshot.get("run"))
    reaction_analysis = _mapping(_mapping(documents.get("01_reaction_analysis.json")).get("reaction_analysis"))
    mechanisms = _sequence(_mapping(documents.get("02_mechanisms.json")).get("mechanism_hypotheses"))
    cofactors = _sequence(_mapping(documents.get("03_capabilities.json")).get("cofactor_candidates"))
    candidates = _sequence(_mapping(documents.get("04_candidates.json")).get("enzyme_candidates"))
    candidate_generation = _mapping(_mapping(documents.get("04_candidates.json")).get("candidate_generation"))
    routes = _sequence(_mapping(documents.get("05_routes.json")).get("routes"))
    result_limits = requested_result_limits(_mapping(run.get("reaction_input")))

    compact_mechanisms = []
    for raw in mechanisms[: result_limits["mechanisms"]]:
        item = _mapping(raw)
        compact_mechanisms.append(
            {
                "hypothesis_id": _text(item.get("hypothesis_id")),
                **reasoning_provenance(item),
                "route_type": _text(item.get("route_type")),
                "confidence": _confidence(item.get("confidence")),
                "summary": _clip(item.get("summary"), 280),
                "required_events": _strings(item.get("required_events"), count=6, chars=120),
                "event_evidence": copy.deepcopy(item.get("event_evidence", [])),
                "counterfactual_bridge": copy.deepcopy(
                    item.get("counterfactual_bridge", {})
                ),
                "key_intermediates": _strings(item.get("key_intermediates"), count=6, chars=120),
                "supports": _strings(item.get("supports"), count=1, chars=180),
                "contradictions": _strings(item.get("contradictions"), count=1, chars=180),
                "falsifiers": _strings(item.get("falsifiers"), count=1, chars=180),
                "unknowns": _strings(item.get("assumptions"), count=3, chars=180),
            }
        )

    compact_cofactors = []
    for raw in cofactors[:3]:
        item = _mapping(raw)
        compact_cofactors.append(
            {
                "rank": int(item.get("rank") or len(compact_cofactors) + 1),
                "name": _clip(item.get("cofactor_class"), 120),
                "mechanism_ids": _strings(item.get("mechanism_ids"), count=10, chars=20),
                **reasoning_provenance(item),
                "unknowns": _strings(item.get("unknowns"), count=2, chars=180),
                "confidence": _confidence(item.get("confidence")),
                "supports": _strings(item.get("supports"), count=1, chars=180),
                "contradictions": _strings(item.get("contradictions"), count=1, chars=180),
            }
        )

    compact_candidates = []
    # 三种候选都要可审计；最多九条，不能让能力缺口被 Top 酶截断隐藏。
    for raw in candidates[:30]:
        item = _mapping(raw)
        compact_candidates.append(
            {
                "candidate_id": _text(item.get("candidate_id")),
                "name_basis": _text(item.get("name_basis")),
                **({"prior_specific_name": item["prior_specific_name"]} if item.get("prior_specific_name") else {}),
                "name_evidence": _clip(item.get("name_evidence"), 240),
                "candidate_role": _text(item.get("candidate_role") or "primary"),
                "component_function": _text(item.get("component_function") or "other"),
                "substrate_relation": _text(item.get("substrate_relation") or "unresolved"),
                "native_reaction_quote": _clip(item.get("native_reaction_quote"), 240),
                "enzyme_family": _clip(item.get("enzyme_family"), 140),
                **_enzyme_identity(item),
                **candidate_provenance(item),
                **({"required_capability": _clip(item["required_capability"], 260)} if item.get("required_capability") else {}),
                "score": _score(item.get("score")),
                "confidence": _confidence(item.get("confidence")),
                "hard_conflict": bool(item.get("hard_conflict", False)),
                "chemistry_fit_score": chemistry_score(item),
                "ranking_reason": item.get("ranking_reason", ""),
                **({"ranking_factors": copy.deepcopy(item["ranking_factors"])} if "ranking_factors" in item else {}),
                "proposal_rationale": copy.deepcopy(item.get("proposal_rationale", {})),
                "proposal_rationale_status": "historical_unverified",
                "engineering_plausibility": _engineering(item.get("engineering_plausibility")),
                "knowledge_gaps": _strings(item.get("knowledge_gaps"), count=2, chars=180),
                "card_ids": _strings(item.get("card_ids"), count=5, chars=64),
                "cofactor_classes": _strings(item.get("cofactor_classes"), count=4, chars=100),
                "supports": _statement_texts(item.get("supports"), count=1),
                "contradictions": _statement_texts(item.get("contradictions"), count=1),
                "evidence_refs": _statement_refs(item.get("supports"), count=3),
                "contradiction_refs": _statement_refs(item.get("contradictions"), count=3),
                "mechanism_ids": _strings(item.get("mechanism_ids"), count=10, chars=20),
            }
        )

    compact_routes = []
    # Every route eligible for this run's requested result limit and all enzyme steps must
    # remain visible; otherwise a missing release/handoff can evade review.
    for raw in routes[: result_limits["routes"]]:
        item = _mapping(raw)
        steps = []
        for raw_step in _sequence(item.get("steps")):
            step = _mapping(raw_step)
            steps.append(_route_step_view(step))
        compact_routes.append(
            {
                "route_id": _text(item.get("route_id")),
                **({"ranking_factors": copy.deepcopy(item["ranking_factors"])} if "ranking_factors" in item else {}),
                **({"stoichiometry": copy.deepcopy(item["stoichiometry"])} if item.get("stoichiometry") else {}),
                **({"balance_review": copy.deepcopy(item["balance_review"])} if item.get("balance_review") else {}),
                "route_type": _text(item.get("route_type")),
                "score": _score(item.get("score")),
                "chemistry_fit_score": chemistry_score(item),
                "ranking_reason": item.get("ranking_reason", ""),
                "confidence": _confidence(item.get("confidence")),
                "steps": steps,
                "support_refs": _strings(item.get("support_refs"), count=2, chars=160),
                "contradiction_refs": _strings(item.get("contradiction_refs"), count=2, chars=160),
                "unsupported_intermediates": _strings(item.get("unsupported_intermediates"), count=8, chars=180),
            }
        )

    compact_cards: dict[str, Any] = {}
    for card_id, raw in (card_summaries or {}).items():
        card = _mapping(raw)
        identity = _mapping(card.get("identity"))
        reactions = []
        for raw_reaction in _sequence(card.get("known_reactions")):
            reaction = _mapping(raw_reaction)
            reactions.append(
                {
                    "reaction_id": _text(reaction.get("reaction_id")),
                    "activity_status": _text(reaction.get("activity_status")),
                    "mechanism_status": _text(reaction.get("mechanism_status")),
                    "substrates": _strings(reaction.get("substrates"), count=5, chars=100),
                    "products": _strings(reaction.get("products"), count=5, chars=100),
                    "mechanistic_role": _clip(reaction.get("mechanistic_role"), 140),
                    "scope": _clip(reaction.get("scope"), 180),
                }
            )
        compact_cards[str(card_id)] = {
            "enzyme_family": _clip(identity.get("enzyme_family"), 140),
            "enzyme_name": _clip(identity.get("display_name"), 140),
            "ec_number": _text(identity.get("ec_class")),
            "cofactor_classes": _strings(card.get("cofactor_classes"), count=4, chars=100),
            "cofactor_notes": _clip(card.get("cofactor_notes"), 180),
            "known_reactions": reactions,
            "wiki_summary": _clip(card.get("wiki_summary"), 180),
        }

    manifest_entries = []
    for raw in _sequence(_mapping(snapshot.get("artifact_manifest")).get("artifacts")):
        item = _mapping(raw)
        name = _text(item.get("name"))
        if name in {
            "01_reaction_analysis.json",
            "02_mechanisms.json",
            "03_capabilities.json",
            "04_candidates.json",
            "05_routes.json",
        }:
            manifest_entries.append(
                {
                    "name": name,
                    "schema_name": _text(item.get("schema_name")),
                    "sha256": _text(item.get("sha256"))[:16],
                }
            )

    return {
        "run_id": _text(run.get("run_id")),
        "plugin_version": _text(run.get("plugin_version")),
        "knowledge_version": _text(run.get("knowledge_version")),
        "knowledge_fingerprint": _text(run.get("knowledge_fingerprint")),
        "input": copy.deepcopy(run.get("reaction_input") or {}),
        "result_limits": result_limits,
        "role_review_required": [item["candidate_id"] for item in compact_candidates
                                 if item["candidate_role"] == "diagnostic_control"],
        "role_review_rule": CANDIDATE_REVIEW_RULES["role_transfer"],
        "reaction_cautions": {
            "assumptions": _strings(reaction_analysis.get("assumptions"), count=2, chars=180),
            "warnings": _strings(reaction_analysis.get("warnings"), count=2, chars=180),
            "unresolved_questions": _strings(reaction_analysis.get("unresolved_questions"), count=2, chars=180),
        },
        "committed_inputs": manifest_entries,
        "revision_targets": {scope: sorted(ids) for scope, ids in revision_owners(documents).items()},
        "mechanisms": compact_mechanisms,
        "cofactors": compact_cofactors,
        "candidate_generation": {
            "mode": _text(candidate_generation.get("mode")),
            "draft_count": int(candidate_generation.get("draft_count") or 0),
            "dispositions": copy.deepcopy(candidate_generation.get("dispositions", [])),
            "prior_candidates": copy.deepcopy(candidate_generation.get("prior_candidates", [])),
            "draft_digest": _text(candidate_generation.get("draft_digest")),
            "ranking_policy": _clip(candidate_generation.get("ranking_policy"), 260),
            "draft": [
                {
                    "proposal_id": _text(_mapping(item).get("proposal_id")),
                    "enzyme_name": _clip(_mapping(item).get("enzyme_name"), 120),
                    "identity_level": _text(_mapping(item).get("identity_level")),
                    **draft_rationale(_mapping(item)),
                }
                for item in _sequence(candidate_generation.get("draft"))[:8]
            ],
        },
        "candidates": compact_candidates,
        "routes": compact_routes,
        "card_audit": compact_cards,
        "audit_flags": {
            "read_card_exclusion_review": read_card_exclusion_review(_mapping(documents.get("04_candidates.json"))),
            "read_card_reviews": copy.deepcopy(_mapping(documents.get("04_candidates.json")).get("read_card_reviews", [])),
            "unassessed_read_cards": copy.deepcopy(_mapping(documents.get("04_candidates.json")).get("unassessed_read_cards", [])),
            "mechanism_link_review": [item["candidate_id"] for item in compact_candidates
                if item["candidate_role"] == "primary" and not item["mechanism_ids"]
                and not any(item["candidate_id"] in f["candidate_ids"] for f in
                    _mapping(documents.get("04_candidates.json")).get("mechanism_feedback", []))],
            "candidate_role_review": candidate_role_review_tasks(candidates, routes),
            "role_transfer_rule": CANDIDATE_REVIEW_RULES["role_transfer"],
            "rationale_field_contract": copy.deepcopy(RATIONALE_FIELD_CONTRACT),
            "review_evidence_policy": copy.deepcopy(REVIEW_EVIDENCE_POLICY),
            "identity_activity_rule": CANDIDATE_REVIEW_RULES["identity_and_activity"],
            "candidate_exclusion_review": exclusion_review_tasks(_mapping(documents.get("04_candidates.json"))),
            "balance_rule": CANDIDATE_REVIEW_RULES["balance"],
            "ranking_policy": RANKING_POLICY,
            "mechanism_feedback": copy.deepcopy(_mapping(documents.get("04_candidates.json")).get("mechanism_feedback", [])),
            "mechanism_route_coverage": mechanism_route_coverage(mechanisms, routes, candidates),
            "specific_enzyme_discovery": [{"mechanism_id": row["mechanism_id"],
                                          "specific_candidates": row["specific_candidates"],
                                          "family_or_capability_leads": row["family_or_capability_leads"]}
                                         for row in discovery_tasks(mechanisms, candidates)],
            "named_candidate_identity_review": [
                {"candidate_id": item["candidate_id"], "enzyme_name": item.get("enzyme_name"),
                 "name_basis": item["name_basis"], "name_evidence": item["name_evidence"]}
                for item in compact_candidates
                if item["candidate_kind"] == "model_enzyme" and item["identity_level"] != "family"
            ],
            "candidate_mechanism_review": [
                {"candidate_id": item["candidate_id"], "mechanism_ids": item["mechanism_ids"],
                 "cofactor_classes": item["cofactor_classes"]}
                for item in compact_candidates if item["mechanism_ids"]
            ],
            "route_catalysts_to_review": [
                {"route_id": route["route_id"], "step": step["step"],
                 "candidate_id": step.get("candidate_id"),
                 "intended_proposal_id": step.get("intended_proposal_id"),
                 "substitution_basis": step.get("substitution_basis"),
                 "chemistry_check": step.get("chemistry_check"),
                 "evidence_refs": step["evidence_refs"]}
                for route in compact_routes for step in route["steps"]
            ],
            "capability_reuse_review": [
                item["hypothesis_id"] for item in compact_mechanisms
                if len(item["required_events"]) > 1
            ],
            "uncompared_mechanism_anchors": sorted({
                card_id for item in mechanisms
                for card_id in _mapping(item).get("knowledge_anchor_card_ids", [])
            } - {card_id for item in candidates for card_id in _mapping(item).get("card_ids", [])}),
            "unassessed_draft_candidates": [
                row["proposal_id"] for row in candidate_generation.get("dispositions", [])
                if row["decision"] == "unassessed"
            ],
            "candidate_merges_to_review": [
                copy.deepcopy(row) for row in candidate_generation.get("dispositions", [])
                if row["decision"] == "merged"
            ],
            "hard_conflict_candidates": [
                item["candidate_id"] for item in compact_candidates if item["hard_conflict"]
            ],
            "routes_with_unsupported_intermediates": [
                item["route_id"] for item in compact_routes if item["unsupported_intermediates"]
            ],
        },
    }


def _compact_evidence_ledger(documents: Mapping[str, Any]) -> list[dict[str, Any]]:
    critique = _mapping(documents.get("06_critique.json"))
    sources = [critique]
    for name in (
        "01_reaction_analysis.json",
        "02_mechanisms.json",
        "03_capabilities.json",
        "04_candidates.json",
        "05_routes.json",
    ):
        sources.append(_mapping(documents.get(name)))

    by_kind: dict[str, dict[str, Any]] = {}
    for source in sources:
        for raw in _sequence(source.get("evidence_ledger")):
            item = _mapping(raw)
            kind = _text(item.get("kind"))
            if kind not in _EVIDENCE_KINDS or kind in by_kind:
                continue
            refs = _strings(item.get("source_refs"), count=4, chars=220)
            if not refs:
                continue
            normalized = {
                "statement_id": _clip(item.get("statement_id") or f"final-{kind}", 100),
                "kind": kind,
                "statement": _clip(item.get("statement"), 360),
                "source_refs": refs,
            }
            card_id = _text(item.get("card_id"))
            field_path = _text(item.get("field_path"))
            if card_id:
                normalized["card_id"] = card_id
            if field_path:
                normalized["field_path"] = field_path
            by_kind[kind] = normalized

    fallbacks = {
        "fact": ("已审计 01—06 的已提交结构化产物。", ["01_reaction_analysis.json", "06_critique.json"]),
        "inference": ("最终排序是已校验上游排序的确定性投影。", ["03_capabilities.json", "04_candidates.json", "05_routes.json"]),
        "hypothesis": ("保留的首选路线仍是待实验区分的假设。", ["05_routes.json", "06_critique.json"]),
        "counterevidence": ("候选冲突与硬冲突降级结果保留在最终报告。", ["04_candidates.json", "06_critique.json"]),
        "unknown": ("结构验证、原子映射与知识库覆盖缺口仍未解决。", ["01_reaction_analysis.json", "06_critique.json"]),
    }
    for kind in _EVIDENCE_KINDS:
        if kind in by_kind:
            continue
        statement, refs = fallbacks[kind]
        by_kind[kind] = {
            "statement_id": f"final-{kind}",
            "kind": kind,
            "statement": statement,
            "source_refs": refs,
        }
    return [by_kind[kind] for kind in _EVIDENCE_KINDS]


def _mechanism_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in _sequence(document.get("mechanism_hypotheses"))[:MAX_RESULT_LIMIT]:
        item = _mapping(raw)
        route_type = _text(item.get("route_type"))
        row = {
                "hypothesis_id": _text(item.get("hypothesis_id")) or f"mechanism-{len(rows) + 1}",
                **reasoning_provenance(item),
                "route_type": route_type if route_type in _ROUTE_TYPES else "direct",
                "summary": _clip(item.get("summary"), 520),
                "required_events": _strings(item.get("required_events"), count=4, chars=220) or ["未解析的催化事件"],
                "event_evidence": copy.deepcopy(item.get("event_evidence", [])),
                "key_intermediates": _strings(item.get("key_intermediates"), count=3, chars=180),
                "supports": _strings(item.get("supports"), count=3, chars=220),
                "contradictions": _strings(item.get("contradictions"), count=3, chars=220),
                "falsifiers": _strings(item.get("falsifiers"), count=2, chars=220) or ["缺少可区分实验时维持低置信。"],
                "assumptions": _strings(item.get("assumptions"), count=3, chars=200),
                "confidence": _confidence(item.get("confidence")),
            }
        if item.get("counterfactual_bridge"):
            row["counterfactual_bridge"] = copy.deepcopy(item["counterfactual_bridge"])
        rows.append(row)
    return rows


def candidate_accountability(document, displayed_ids=(), excluded_ids=()):
    """逐项保留草案去向；未评估候选不冒充已筛选推荐。"""
    generation = document.get("candidate_generation") or {}
    decisions = {row["proposal_id"]: row for row in generation.get("dispositions", [])}
    selected = {row.get("proposal_id"): row for row in document.get("enzyme_candidates", [])
                if row.get("proposal_id")}
    rows = []
    for draft in candidate_identity_baseline(generation):
        proposal = draft["proposal_id"]
        disposition = decisions.get(proposal, {})
        candidate = selected.get(proposal)
        status = disposition.get("decision", "unassessed")
        reason = disposition.get("reason", "未完成逐项处置，保留原始提名以供核对")
        replacement = next((row for row in document.get("enzyme_candidates", [])
                            if row.get("candidate_id") == disposition.get("replacement_id")), {})
        if (status == "merged" and draft.get("identity_level") in {"family", "capability"}
                and replacement.get("candidate_kind") == "unmatched_capability"):
            status = "capability_handoff"
        if candidate:
            identifier = candidate.get("candidate_id")
            if identifier in excluded_ids or candidate.get("hard_conflict"):
                status = "excluded_by_review_or_conflict"
                reason = "候选已提交，但被审核或明确冲突过滤；不作为推荐"
            elif identifier in displayed_ids:
                status = "diagnostic_only" if candidate.get("candidate_role") == "diagnostic_control" else "displayed"
                reason = ("仅在诊断对照区展示，不属于推荐候选或目标酶命中"
                          if candidate.get("candidate_role") == "diagnostic_control" else
                          "已进入候选、组件、家族线索或能力展示；核实状态另列")
            else:
                status = "not_displayed"
                reason = ("诊断对照展示名额不足；未作为推荐，不据此推断化学不成立"
                          if candidate.get("candidate_role") == "diagnostic_control" else
                          "候选已提交，未进入当前展示集合；不据此推断化学不成立")
        rows.append({"proposal_id": proposal, "enzyme_name": (candidate or {}).get("specific_name") or draft.get("specific_name") or draft.get("enzyme_name", proposal),
                     "status": status, "reason": reason,
                     "replacement_id": disposition.get("replacement_id", "")})
    for prior in generation.get('prior_wiki_candidates', []):
        card_id = prior['card_id']
        selected_wiki = next((row for row in document.get('enzyme_candidates', []) if card_id in row.get('card_ids', [])), None)
        review = next((row for row in document.get('read_card_reviews', []) if row['card_id'] == card_id), {})
        identifier = (selected_wiki or {}).get('candidate_id')
        rows.append({'proposal_id': card_id, 'enzyme_name': prior['enzyme_name'],
                     'status': ('excluded_by_review_or_conflict' if identifier in excluded_ids else
                                'displayed' if identifier in displayed_ids else 'unassessed'),
                     'reason': review.get('reason') or '既往 Wiki 提名；本轮需重新比较，不因未绑定路线而自动删除或推荐',
                     'replacement_id': ''})
    return rows


def _accountability_table(report):
    rows = report.get("candidate_accountability") or []
    if not rows:
        return []
    labels = {"displayed": "已展示", "diagnostic_only": "仅作诊断对照（非推荐）",
              "not_displayed": "已提交但未展示", "unassessed": "未完成评估",
              "excluded": "已排除", "merged": "已合并", "retained": "已保留",
              "capability_handoff": "按能力承接（未匹配具体酶）",
              "excluded_by_review_or_conflict": "审核／冲突排除"}
    return ["", "## 独立草案候选去向（不是新增推荐）", "",
            "| 原始候选 | 去向 | 原因 |", "|---|---|---|",
            *["| " + " | ".join(_cell(value) for value in (
                row["enzyme_name"], labels.get(row["status"], row["status"]), row["reason"])) + " |" for row in rows]]


def build_final_report(snapshot: Mapping[str, Any], *, plugin_version: str) -> dict[str, Any]:
    """将已校验的 01—06 产物汇总为有界最终报告文档。"""

    documents = _mapping(snapshot.get("documents"))
    required = {
        "01_reaction_analysis.json",
        "02_mechanisms.json",
        "03_capabilities.json",
        "04_candidates.json",
        "05_routes.json",
        "06_critique.json",
    }
    missing = sorted(required - set(documents))
    if missing:
        raise ValueError("final report requires committed artifacts: " + ", ".join(missing))

    run = _mapping(snapshot.get("run"))
    run_id = _text(run.get("run_id"))
    critique_document = _mapping(documents.get("06_critique.json"))
    require_current_review(snapshot)
    decision = _text(critique_document.get("review_decision")).upper()
    if decision not in {"PASS", "PARTIAL_PASS", "REVISE"}:
        raise ValueError("final report requires a recorded PASS or REVISE decision")

    mechanism_document = _mapping(documents.get("02_mechanisms.json"))
    capability_document = _mapping(documents.get("03_capabilities.json"))
    candidate_document = copy.deepcopy(_mapping(documents.get("04_candidates.json")))
    route_document = _mapping(documents.get("05_routes.json"))
    reaction_document = _mapping(documents.get("01_reaction_analysis.json"))
    result_limits = requested_result_limits(_mapping(run.get("reaction_input")))
    mechanisms = _mechanism_rows(mechanism_document)

    critique = _mapping(critique_document.get("critique"))
    from .review_scope import aggregate_review
    decision, review_summary = aggregate_review(documents, decision, critique, validate=False)
    object_review = {row['object_id']: row for row in review_summary['objects']}
    held_candidates = {identifier for identifier, row in object_review.items()
                       if row['object_type'] == 'candidate' and row['status'] != 'retained'} if decision == 'PARTIAL_PASS' else set()
    excluded_candidates = set(critique.get("excluded_candidate_ids", []))
    excluded_mechanisms = set(critique.get("excluded_mechanism_ids", []))
    excluded_candidates.update(key for key, row in object_review.items() if row['object_type'] == 'candidate' and row['assessment'] == 'excluded')
    excluded_mechanisms.update(key for key, row in object_review.items() if row['object_type'] == 'mechanism' and row['assessment'] == 'excluded')
    unresolved_routes = set()
    if _text(critique_document.get("review_decision")).upper() == "REVISE":
        # 预算耗尽的探索报告仍不能推荐已知错误；按审核定位传递到依赖路线。
        affected = {identifier for issue in critique.get("hard_conflicts", [])
                    if issue.get("severity") in {"major", "critical"} and review_issue_type(issue) == "chemical_conflict"
                    for identifier in issue.get("affected_ids", [])}
        excluded_candidates.update(item["candidate_id"] for item in candidate_document.get("enzyme_candidates", [])
                                   if item["candidate_id"] in affected)
        excluded_mechanisms.update(item["hypothesis_id"] for item in mechanisms if item["hypothesis_id"] in affected)
        unresolved_routes.update(affected)
    # 排除意见同步应用到候选表、机制表和流程图；上游原产物保留以供追溯。
    candidate_document["enzyme_candidates"] = [
        item for item in _sequence(candidate_document.get("enzyme_candidates"))
        if _mapping(item).get("candidate_id") not in excluded_candidates
    ]
    mechanisms = [item for item in mechanisms if item.get("hypothesis_id") not in excluded_mechanisms][:result_limits["mechanisms"]]
    dispositions = {
        _text(_mapping(item).get("route_id")): _text(_mapping(item).get("disposition"))
        for item in _sequence(critique.get("route_dispositions"))
    }
    for identifier, row in object_review.items():
        if row['object_type'] == 'route' and row['status'] == 'needs_revision' and dispositions.get(identifier) != 'exclude':
            dispositions[identifier] = 'demote'
    for route in route_document.get("routes", []):
        if route["route_id"] in unresolved_routes or any(
                ref == f"04_candidates.json:{identifier}"
                for ref in route.get("support_refs", []) for identifier in excluded_candidates) or any(
                step.get("candidate_id") in excluded_candidates
                or excluded_mechanisms.intersection(step.get("mechanism_ids", []))
                for step in route.get("steps", [])):
            dispositions[route["route_id"]] = "exclude"
    gaps = _sequence(critique.get("evidence_gaps"))
    gap_statements = [_clip(_mapping(item).get("statement"), 300) for item in gaps[:3] if _clip(_mapping(item).get("statement"), 300)]

    retained_routes = [
        _mapping(item) for item in _sequence(route_document.get("routes"))
        if dispositions.get(_text(_mapping(item).get("route_id"))) != "exclude"
    ]
    retained_routes.sort(key=lambda item: (
        {"retain": 0, "revise": 1, "demote": 2, "": 2}.get(
            dispositions.get(_text(item.get("route_id")), ""), 3
        ),
        *chemistry_rank_key(item),
    ))
    preferred_mechanisms = set()
    if retained_routes:
        preferred_mechanisms = {
            mechanism_id
            for step in _sequence(retained_routes[0].get("steps"))
            for mechanism_id in _strings(_mapping(step).get("mechanism_ids"), count=10, chars=20)
        }
    if not preferred_mechanisms:
        ranked_candidates = [
            _mapping(item) for item in _sequence(candidate_document.get("enzyme_candidates"))
            if not bool(_mapping(item).get("hard_conflict", False))
            and _text(_mapping(item).get("candidate_role")) != "diagnostic_control"
        ]
        ranked_candidates.sort(key=chemistry_rank_key)
        if ranked_candidates:
            preferred_mechanisms.update(
                _strings(ranked_candidates[0].get("mechanism_ids"), count=10, chars=20)
            )

    cofactor_candidates = [_mapping(item) for item in _sequence(capability_document.get("cofactor_candidates"))]
    if preferred_mechanisms:
        cofactor_candidates = [
            item for item in cofactor_candidates
            if preferred_mechanisms.intersection(_strings(item.get("mechanism_ids"), count=10, chars=20))
        ]

    top_cofactors = []
    for item in cofactor_candidates[:3]:
        source_rank = int(item.get("rank") or len(top_cofactors) + 1)
        rank = len(top_cofactors) + 1
        top_cofactors.append(
            {
                "rank": rank,
                "name": _clip(item.get("cofactor_class"), 140),
                "mechanism_ids": _strings(item.get("mechanism_ids"), count=10, chars=20),
                **reasoning_provenance(item),
                "confidence": _confidence(item.get("confidence")),
                "supports": _strings(item.get("supports"), count=2, chars=240),
                "contradictions": _strings(item.get("contradictions"), count=2, chars=240),
                "evidence_refs": [f"03_capabilities.json:cofactor_candidates.{source_rank - 1}"],
                "knowledge_gaps": _strings(item.get("unknowns"), count=3, chars=240),
            }
        )

    candidate_limit = result_limits["candidates"]
    mechanism_types = {
        _text(item.get("hypothesis_id")): _text(item.get("route_type"))
        for item in mechanisms
    }

    def effective_candidate_role(item: Mapping[str, Any]) -> str:
        role = _text(item.get("candidate_role") or "primary")
        linked = _strings(item.get("mechanism_ids"), count=10, chars=20)
        if role == "primary" and linked and all(
            mechanism_types.get(mechanism_id) == "multi_enzyme"
            for mechanism_id in linked
        ):
            return "route_component"
        return role

    all_named_candidates = [
        _mapping(item)
        for item in _sequence(candidate_document.get("enzyme_candidates"))
        if not bool(_mapping(item).get("hard_conflict", False))
        and item.get('candidate_id') not in held_candidates
        and _mapping(item).get("candidate_kind") != CAPABILITY
        and _mapping(item).get("identity_level") != "family"
    ]
    # 单酶 Top N 与局部候选属于不同视图，不能先全局截断而吞掉多酶组件。
    # 每组仍有同一展示上限，分数、科学处置及实际路线绑定保持不变。
    eligible_candidates = []
    for group in ('primary', 'route_component'):
        eligible_candidates.extend(sorted(
            (item for item in all_named_candidates if effective_candidate_role(item) == group),
            key=chemistry_rank_key)[:candidate_limit])
    # 对照不是补齐 Top N 的候选；单独有界展示，不受机制是否已覆盖影响。
    diagnostic_candidates = sorted(
        (item for item in all_named_candidates if effective_candidate_role(item) == "diagnostic_control"),
        key=lambda item: -_score(item.get("score")),
    )[:candidate_limit]
    role_order = {"primary": 0, "diagnostic_control": 1, "route_component": 2}
    function_order = {"oxidation": 0, "dephosphorylation": 1, "dehydration": 2,
                      "alternative_termination": 3, "activation": 4, "other": 5}
    eligible_candidates.sort(
        key=lambda item: (
            role_order.get(effective_candidate_role(item), 3),
            function_order.get(_text(item.get("component_function") or "other"), 5)
            if effective_candidate_role(item) == "route_component" else 0,
            *chemistry_rank_key(item),
        )
    )
    top_families = []
    candidate_table = []
    group_ranks = {}
    for item in [*eligible_candidates, *diagnostic_candidates]:
        role = effective_candidate_role(item)
        component_function = _text(item.get("component_function") or "other")
        rank_key = (role, component_function if role == "route_component" else "")
        group_ranks[rank_key] = group_ranks.get(rank_key, 0) + 1
        supports = _statement_texts(item.get("supports"), count=2)
        contradictions = _statement_texts(item.get("contradictions"), count=2)
        refs = _statement_refs(item.get("supports"), count=4) or _strings(item.get("card_ids"), count=4, chars=64)
        card_ids = _strings(item.get("card_ids"), count=5, chars=64)
        if not card_ids and candidate_provenance(item)["candidate_kind"] == WIKI_ENZYME:
            raise ValueError("eligible final-report candidates must retain at least one card_id")
        family = _clip(item.get("enzyme_family"), 160)
        top_families.append(
            {
                "candidate_id": item["candidate_id"],
                "rank": group_ranks[rank_key],
                "candidate_role": role,
                "component_function": component_function,
                "name": _enzyme_label(item),
                "mechanism_ids": _strings(item.get("mechanism_ids"), count=10, chars=20),
                **candidate_provenance(item),
                "score": chemistry_score(item),
                "evidence_level": candidate_evidence_level(item),
                "chemistry_fit_score": chemistry_score(item),
                "wiki_evidence_score": 100 if candidate_provenance(item)["candidate_kind"] == WIKI_ENZYME and item.get("substrate_relation") == "reported_exact" else (50 if candidate_provenance(item)["candidate_kind"] == WIKI_ENZYME else 0),
                "confidence": _confidence(item.get("confidence")),
                "supports": supports,
                "contradictions": contradictions,
                "evidence_refs": refs or ["04_candidates.json"],
                "knowledge_gaps": _strings(item.get("knowledge_gaps"), count=3, chars=240),
            }
        )
        if role == "diagnostic_control":
            continue
        candidate_table.append(
            {
                "candidate_id": item["candidate_id"],
                "rank": group_ranks[rank_key],
                "candidate_role": role,
                "component_function": component_function,
                "enzyme_family": family,
                **_enzyme_identity(item),
                **candidate_provenance(item),
                "evidence_level": candidate_evidence_level(item),
                "cofactor_classes": _strings(item.get("cofactor_classes"), count=4, chars=100),
                "support_mechanism": _strings(item.get("mechanism_ids"), count=10, chars=100),
                "support_evidence": supports,
                **{key: copy.deepcopy(item[key]) for key in ("ranking_factors", "ranking_reason") if key in item},
                "proposal_rationale": copy.deepcopy(item.get("proposal_rationale", {})),
                "proposal_rationale_status": "historical_unverified",
                "contradictions": contradictions,
                "engineering_plausibility": _engineering(item.get("engineering_plausibility")),
                "confidence": _confidence(item.get("confidence")),
                "card_ids": card_ids,
            }
        )

    family_hints = []
    for raw in _sequence(candidate_document.get("enzyme_candidates")):
        item = _mapping(raw)
        if item.get("identity_level") == "family" and not item.get("hard_conflict"):
            family_hints.append({"rank": len(family_hints) + 1, "name": _enzyme_label(item),
                                 "candidate_id": item['candidate_id'],
                                 **candidate_provenance(item), "confidence": _confidence(item.get("confidence")),
                                 "supports": _statement_texts(item.get("supports"), count=1),
                                 "contradictions": _statement_texts(item.get("contradictions"), count=1),
                                 "evidence_refs": _statement_refs(item.get("supports")) or ["04_candidates.json"],
                                 "knowledge_gaps": _strings(item.get("knowledge_gaps"), count=3, chars=240)})
    unmatched_capabilities = []
    for raw in _sequence(candidate_document.get("enzyme_candidates")):
        item = _mapping(raw)
        if item.get("candidate_kind") != CAPABILITY:
            continue
        unmatched_capabilities.append({
            "rank": len(unmatched_capabilities) + 1, "name": _enzyme_label(item),
            **candidate_provenance(item), "confidence": _confidence(item.get("confidence")),
            "supports": _statement_texts(item.get("supports"), count=1),
            "contradictions": _statement_texts(item.get("contradictions"), count=1),
            "evidence_refs": _statement_refs(item.get("supports"), count=3) or ["04_candidates.json"],
            "knowledge_gaps": _strings(item.get("knowledge_gaps"), count=3, chars=240),
        })

    disposition_order = {"retain": 0, "revise": 1, "demote": 2, "": 2, "exclude": 9}
    route_items = [_mapping(item) for item in _sequence(route_document.get("routes"))]
    route_delivery_failed = any(
        "route " in _text(item) and "校验未通过" in _text(item)
        for item in _sequence(candidate_document.get("knowledge_gaps"))
    )
    route_items.sort(
        key=lambda item: (
            disposition_order.get(dispositions.get(_text(item.get("route_id")), ""), 3),
            *chemistry_rank_key(item),
        )
    )
    top_routes = []
    for item in route_items:
        route_id = _text(item.get("route_id"))
        if dispositions.get(route_id) == "exclude":
            continue
        steps = _sequence(item.get("steps"))
        families = [
            _clip(_enzyme_label(_mapping(step)), 120)
            for step in steps
            if _enzyme_label(_mapping(step))
        ]
        unsupported = _strings(item.get("unsupported_intermediates"), count=8, chars=220)
        route_note = ("机制路线假说；包含未经过 Wiki 核实的酶或未匹配催化能力；路线尚未验证"
                      if any(_mapping(s).get("candidate_kind", WIKI_ENZYME) != WIKI_ENZYME for s in steps)
                      else "Wiki 类比支持；完整目标路线未验证")
        top_routes.append(
            {
                "rank": len(top_routes) + 1,
                "route_id": route_id,
                "review_disposition": dispositions.get(route_id, "unspecified"),
                "review_reason": next((_clip(row.get("reason"), 400) for row in
                    _sequence(critique.get("route_dispositions")) if row.get("route_id") == route_id), ""),
                **({"balance_review": copy.deepcopy(item["balance_review"])} if item.get("balance_review") else {}),
                "name": _clip(f"{route_id}: {' → '.join(families) or _text(item.get('route_type'))}", 180),
                **{key: copy.deepcopy(item[key]) for key in ("ranking_factors", "ranking_reason") if key in item},
                **({"stoichiometry": copy.deepcopy(item["stoichiometry"])} if item.get("stoichiometry") else {}),
                "route_type": _text(item.get("route_type")),
                "score": chemistry_score(item),
                "confidence": _confidence(item.get("confidence")),
                "supports": _strings(item.get("support_refs"), count=3, chars=200),
                "contradictions": _strings(item.get("contradiction_refs"), count=3, chars=200),
                "evidence_refs": _strings(item.get("support_refs"), count=4, chars=200) or [f"05_routes.json:{route_id}"],
                "knowledge_gaps": unsupported,
                "verification_note": route_note,
                "steps": [_route_step_view(_mapping(step)) for step in steps],
            }
        )
    # 两榜共用同一组已审核路线；证据覆盖按步骤比例计算，重复引用不加分。
    candidate_index = {item.get("candidate_id"): item for item in candidate_document.get("enzyme_candidates", [])}
    evidence_route_sources = {}
    for route, source in zip(top_routes, [item for item in route_items if dispositions.get(_text(item.get("route_id"))) != "exclude"]):
        evidence_route_sources[id(route)] = source
        source_steps = [step for step in source.get("steps", []) if step.get("step_kind") != "nonenzymatic"]
        covered = sum(
            candidate_provenance(candidate_index.get(step.get("candidate_id"), step))["candidate_kind"] == WIKI_ENZYME
            for step in source_steps
        )
        route["wiki_coverage"] = round(covered / max(1, len(source_steps)), 3)
        route["chemistry_fit_score"] = route["score"]
        route["wiki_evidence_score"] = round(100 * route["wiki_coverage"], 3)
        route["overall_route_score"] = route["score"]
        route_review = object_review.get(source['route_id'], {})
        route["support_status"] = "unresolved" if (review_summary['global_reasons']
            or route_review.get('assessment') != 'retained' or dispositions.get(source["route_id"]) == "revise") else "partially_supported"
        flow = (source.get("balance_review") or {}).get("main_material_flow", "unknown")
        route["closure_status"] = {"continuous": "主物料连续（机制假说）；目标活性仍待验证",
                                   "gap": "主物料存在断点；不代表完整路线",
                                   "unknown": "主物料连续性未单独判定；目标活性待核实"}[flow]
        route["verification_note"] = route["closure_status"] + "；" + route["verification_note"]
    def evidence_strength(route):
        source = evidence_route_sources[id(route)]
        strengths = []
        for step in source.get("steps", []):
            if step.get("step_kind") == "nonenzymatic":
                continue
            candidate = candidate_index.get(step.get("candidate_id"), step)
            kind = candidate_provenance(candidate)["candidate_kind"]
            strengths.append(
                (1 if candidate.get("substrate_relation") == "reported_exact" else 0.5)
                if kind == WIKI_ENZYME else
                (0.25 if candidate.get("identity_level") == "family"
                 and any(str(ref).startswith("ERD-CARD-") for ref in candidate.get("sources", [])) else 0)
            )
        return sum(strengths) / max(1, len(strengths))
    for route in top_routes:
        route["wiki_evidence_score"] = round(100 * evidence_strength(route), 3)
    # retain 只表示保留假说；明确的主物料断点不能靠保留标签进入完整路线推荐。
    # 未判定/目标活性未知不按断点处理；ATP外加与共底物待配平也不等于主物料断裂。
    def exploration_only(row):
        return (row["review_disposition"] == "demote"
                or (row.get("balance_review") or {}).get("main_material_flow") == "gap")
    exploratory_routes = [row for row in top_routes if exploration_only(row)]
    top_routes = [row for row in top_routes if not exploration_only(row)]
    for group in (top_routes, exploratory_routes):
        for index, row in enumerate(group, 1):
            row["rank"] = index
    for row in [*candidate_table, *top_families]:
        row["display_role"] = candidate_display_role(row, top_routes, exploratory_routes)
    evidence_routes = sorted(top_routes, key=lambda row: (-evidence_strength(row), -row["wiki_coverage"], -row["score"]))
    evidence_ranking = [{"rank": i, "name": row["name"], "wiki_coverage": row["wiki_coverage"]}
                        for i, row in enumerate(evidence_routes[:result_limits["routes"]], 1)]
    top_routes = top_routes[:result_limits["routes"]]

    reaction_input = copy.deepcopy(run.get("reaction_input") or {})
    reaction_analysis = _mapping(reaction_document.get("reaction_analysis"))
    input_warnings = _strings(reaction_analysis.get("warnings"), count=8, chars=260)
    assumptions = [row["summary"] for row in mechanisms] + gap_statements

    # Draw only declared route-step associations, not independent rank-1 lists.
    diagram = ["flowchart LR"]
    for ri, route in enumerate(top_routes, 1):
        previous = None
        for si, step in enumerate(route.get("steps", []), 1):
            node = f"R{ri}S{si}"
            label = f"{step['input']} → {step['output']}：{_enzyme_label(step)}；{step['verification_note']}"
            diagram.append(f'  {node}["{_mermaid_label(label, 180)}"]')
            if previous:
                flow = (route.get("balance_review") or {}).get("main_material_flow", "unknown")
                edge = "-->|主物料衔接（假说）|" if flow == "continuous" else "-.物料衔接待核对.->"
                diagram.append(f"  {previous} {edge} {node}")
            previous = node
    if not top_routes:
        diagram.append('  U["仅有非推荐探索路线；已降级分支不进入推荐图"]' if exploratory_routes else
                       '  U["路线提交校验失败；保留机制假说与未匹配能力"]'
                       if route_delivery_failed else '  U["本轮未提交可排名路线；保留机制假说与未匹配能力"]')
    mermaid = "\n".join(diagram)

    cofactor_names = [item["name"] for item in top_cofactors]
    route_names = [item["name"] for item in top_routes]
    mechanism_names = [item["hypothesis_id"] for item in mechanisms]
    experiments = [
        {
            "title": "辅因子依赖概念对照",
            "purpose": "区分当前 Top 辅因子类别，而不把家族共因子等同于目标活性。",
            "distinguishes": cofactor_names or ["无充分辅因子候选"],
            "controls": ["去辅因子基线", "候选辅因子回补对照"],
            "readout": "比较目标产物信号是否随候选辅因子移除与回补而改变。",
            "safety_boundary": "conceptual_only_no_operational_parameters",
        },
        {
            "title": "竞争机制与路线判别",
            "purpose": "用中间体或产物谱区分保留的竞争机制和路线。",
            "distinguishes": mechanism_names + route_names[:2] or ["当前保留路线"],
            "controls": ["无酶背景", "已知标准反应对照"],
            "readout": "比较关键中间体类别、目标产物与旁路产物的相对出现。",
            "safety_boundary": "conceptual_only_no_operational_parameters",
        },
    ]

    limitations = [
        "外部可追溯证据仅来自本轮已读 Wiki；模型提出的库外酶和机制必须标注未经过 Wiki 核实，未召回不等于不存在。",
        "尚未验证具体原子对应、反应配平与结构层面的可行性；机制仍是可证伪假设。",
        "知识卡标准反应的类比不等于目标反应已经被实验验证。",
        "排序分数是模型的相对判断，不是实验成功概率；来源标签与置信度互不替代。",
        "最终报告由已提交的 01—06 确定性聚合，不新增候选或分数。",
        *gap_statements[:2],
    ]
    limitations.extend(
        f"机制 {row['mechanism_id']}（{row['route_type']}）没有对应的已提交路线对象；事件级机制不等于完成分步路线，需组装或说明不继续的理由。"
        for row in mechanism_route_coverage(mechanisms, retained_routes, candidate_document['enzyme_candidates']) if not row["linked_route_ids"]
    )
    limitations.extend(
        "审核处置：" + _clip(row.get("route_id"), 80) + " " + row.get("disposition", "")
        + "；" + _clip(row.get("reason"), 220)
        for row in _sequence(critique.get("route_dispositions"))
        if row.get("disposition") in {"retain", "exclude", "revise", "demote"}
    )
    limitations.extend("审计交付限制（不构成化学反证）：" + _clip(issue.get("statement"), 350)
                       for issue in critique.get("hard_conflicts", [])
                       if review_issue_type(issue) == "audit_delivery")
    assertion_labels = {"overstatement": "待修正的表述过度", "evidence_attribution": "待修正的证据归属"}
    limitations.extend(assertion_labels[review_issue_type(issue)] + "（不自动表示化学不成立）："
                       + _clip(issue.get("statement"), 350)
                       for issue in [*critique.get("hard_conflicts", []), *critique.get("evidence_gaps", [])]
                       if review_issue_type(issue) in assertion_labels)
    if excluded_candidates or excluded_mechanisms:
        limitations.append("审核已从推荐结果移除候选／机制：" + "、".join(sorted(excluded_candidates | excluded_mechanisms)))
    limitations.extend(
        _clip(item, 350)
        for item in _sequence(candidate_document.get("knowledge_gaps"))
        if "校验未通过：" in _text(item)
    )

    return {
        "schema_version": "1.0",
        "status": "completed" if decision == "PASS" else "partial_pass" if decision == "PARTIAL_PASS" else "exploratory_unverified",
        "review_decision": decision,
        "review_summary": review_summary,
        "required_revisions": copy.deepcopy(critique.get("required_revisions") or []),
        "family_hints": family_hints,
        "run_id": run_id,
        "plugin_version": _text(plugin_version),
        "knowledge_version": _text(run.get("knowledge_version")),
        "knowledge_fingerprint": _text(run.get("knowledge_fingerprint")),
        "original_input": reaction_input,
        "normalized_input": copy.deepcopy(reaction_input),
        "input_warnings": input_warnings,
        "assumptions_and_hypotheses": [_clip(item, 420) for item in assumptions[:6]],
        "evidence_ledger": _compact_evidence_ledger(documents),
        "mechanism_hypotheses": mechanisms,
        "top_cofactor_classes": top_cofactors,
        "top_enzyme_families": [item for item in top_families if item["candidate_role"] != "diagnostic_control"][:MAX_RESULT_LIMIT],
        "single_enzyme_candidates": [
            {**item, "rank": index}
            for index, item in enumerate((item for item in top_families if item["candidate_role"] == "primary"), 1)
        ],
        "route_components": [
            item for item in top_families if item["candidate_role"] == "route_component"
        ],
        "diagnostic_controls": [
            {**item, "rank": index}
            for index, item in enumerate((item for item in top_families if item["candidate_role"] == "diagnostic_control"), 1)
        ],
        "top_routes": top_routes,
        "exploratory_routes": exploratory_routes[:result_limits["routes"]],
        "wiki_evidence_route_ranking": evidence_ranking,
        "candidate_table": candidate_table,
        "candidate_accountability": candidate_accountability(
            documents["04_candidates.json"],
            {item.get("candidate_id") for item in [*eligible_candidates, *diagnostic_candidates]} | {
                item.get("candidate_id") for item in candidate_document.get("enzyme_candidates", [])
                if item.get("identity_level") == "family" or item.get("candidate_kind") == CAPABILITY},
            excluded_candidates),
        "unmatched_capabilities": unmatched_capabilities,
        "mermaid_diagram": mermaid,
        "experimental_validation_suggestions": experiments,
        "limitations": limitations,
        "next_actions": [
            "优先区分首选机制与主要替代机制。",
            "验证首选家族的辅因子依赖和目标产物释放边界。",
            "获得新证据后从相应 Workflow 阶段重跑，而不是改写既有证据。",
        ],
    }


def _cell(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        rendered = "; ".join(_text(item) for item in value if _text(item))
    elif isinstance(value, dict):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = _text(value)
    return rendered.replace("|", "\\|").replace("\n", " ") or "—"


_COMPONENT_FUNCTION_LABELS = {
    "oxidation": "氧化", "dephosphorylation": "脱磷酸", "dehydration": "脱水/成双键",
    "alternative_termination": "替代终止", "activation": "底物活化", "other": "其他局部能力",
}


def _ranked_table(title: str, rows: Sequence[Any], *, empty_reason: str = "本轮没有可排名项") -> list[str]:
    components = title.startswith(("多酶路线组件", "局部候选"))
    lines = [f"## {title}", ""]
    if components:
        lines.append("各功能组内单独排序；是否绑定路线见用途标签，未绑定线索不代表已承担催化步骤。")
        lines.append("")
    lines.extend(["| 功能 | 组内排名 | 名称 | 分数 | 置信度 | 证据引用 | 主要反证 |" if components else "| 排名 | 名称 | 分数 | 置信度 | 证据引用 | 主要反证 |",
                  "|---|---:|---|---:|---|---|---|" if components else "|---:|---|---:|---|---|---|"])
    for row in rows:
        item = dict(row) if isinstance(row, Mapping) else {}
        lines.append(
            ("| {function} | {rank} | {name} | {score} | {confidence} | {evidence} | {conflicts} |" if components else "| {rank} | {name} | {score} | {confidence} | {evidence} | {conflicts} |").format(
                function=_cell(_COMPONENT_FUNCTION_LABELS.get(item.get("component_function"), "其他局部能力")),
                rank=_cell(item.get("rank")),
                name=_cell(str(_COFACTOR_LABELS.get(_text(item.get("name")), item.get("name")))
                    + ("；" + item.get("display_role", "局部候选（路线绑定待核对）") if components else "")
                    + ("；" + item["verification_note"] if item.get("verification_note") else "")),
                score=_cell(item.get("score")),
                confidence=_cell(_level_label(item.get("confidence"))),
                evidence=_cell(item.get("evidence_refs")),
                conflicts=_cell(item.get("contradictions")),
            )
        )
    if not rows:
        lines.append(f"| — | 无可排名项 | — | 未确定 | — | {_cell(empty_reason)} |")
    lines.append("")
    return lines


def _mechanism_table(rows: Sequence[Any]) -> list[str]:
    lines = [
        "## 并列机制假设",
        "",
        "| 机制 ID | 路线类型 | 摘要 | 关键事件 | 反事实桥接 | 中间体类别 | 主要反证 | 证伪条件 | 置信度 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        item = dict(row) if isinstance(row, Mapping) else {}
        bridge = dict(item.get("counterfactual_bridge") or {})
        bridge_summary = []
        if bridge.get("shared_events"):
            bridge_summary.append("共享：" + "；".join(_strings(bridge["shared_events"], count=3, chars=120)))
        if bridge.get("missing_events"):
            bridge_summary.append("缺失：" + "；".join(_strings(bridge["missing_events"], count=3, chars=120)))
        if bridge.get("conflicting_events"):
            bridge_summary.append("冲突：" + "；".join(_strings(bridge["conflicting_events"], count=2, chars=120)))
        if bridge.get("target_alternative_termination"):
            bridge_summary.append("替代终止：" + "；".join(_strings(bridge["target_alternative_termination"], count=2, chars=120)))
        lines.append(
            "| {identifier} | {route_type} | {summary} | {events} | {bridge} | {intermediates} | {conflicts} | {falsifiers} | {confidence} |".format(
                identifier=_cell(item.get("hypothesis_id")),
                route_type=_cell(_ROUTE_LABELS.get(_text(item.get("route_type")), item.get("route_type"))),
                summary=_cell(_text(item.get("summary")) + ("；" + item["verification_note"] if item.get("verification_note") else "")),
                events=_cell(item.get("required_events")),
                bridge=_cell(bridge_summary or ["未提交显式桥接"]),
                intermediates=_cell(item.get("key_intermediates")),
                conflicts=_cell(item.get("contradictions")),
                falsifiers=_cell(item.get("falsifiers")),
                confidence=_cell(_level_label(item.get("confidence"))),
            )
        )
    lines.append("")
    return lines


def normalize_mermaid(value: Any) -> str:
    """返回不含 Markdown 代码围栏的安全 Mermaid 源码。"""

    text = _text(value)
    fenced = re.fullmatch(r"```(?:mermaid)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fenced:
        text = _text(fenced.group(1))
    if not re.match(r"^(?:flowchart|graph)\s+(?:LR|RL|TB|BT|TD)\b", text, flags=re.IGNORECASE):
        raise ValueError("mermaid_diagram must start with a flowchart/graph direction")
    lowered = text.casefold()
    forbidden = ("<script", "javascript:", "%%{init", "click ", "http://", "https://")
    if any(item in lowered for item in forbidden):
        raise ValueError("mermaid_diagram contains a forbidden directive or external link")
    if len(text) > 50_000:
        raise ValueError("mermaid_diagram exceeds 50000 characters")
    return text.rstrip() + "\n"


def _object_review_table(report):
    rows = report.get('review_summary', {}).get('objects', [])
    if not rows:
        return []
    labels = {'retained': '保留假说', 'unreviewed': '待逐条审核', 'needs_revision': '局部待修订', 'excluded': '已排除'}
    lines = ['## 逐条审核与局部待办', '', '保留假说不等于目标活性已验证；待修订项不参与已保留结果的正式排名。', '',
             '| 对象 | 类型 | 审核状态 | 机制／证据判断 | 排序判断 | 原因或依赖 |', '|---|---|---|---|---|---|']
    for row in rows:
        lines.append('| ' + ' | '.join(_cell(value) for value in (row.get('name', row['object_id']),
            {'route': '路线', 'candidate': '候选', 'mechanism': '机制'}[row['object_type']], labels[row['status']],
            {'retained': '可保留', 'unreviewed': '未审核', 'revise': '待修订', 'excluded': '已排除'}[row['assessment']],
            '待修订' if row['ranking'] == 'pending' else '无已定位排序问题',
            '；'.join(row.get('reasons', []))[:400])) + ' |')
    return lines + ['']


def render_final_report_markdown(report: Mapping[str, Any]) -> str:
    """仅根据已校验的最终 JSON 渲染 Markdown 报告。"""

    run_id = _text(report.get("run_id"))
    lines = [
        "# 机制引导的酶逆向发现报告",
        "",
        f"- Run ID：`{run_id}`",
        "- 审核状态：" + {"REVISE": "仍需修订（REVISE），请逐条查看审核状态，不代表全部对象失败。",
            "PARTIAL_PASS": "部分通过（PARTIAL_PASS）：可保留对象正常交付，待修订项及其依赖单独列出。",
            "PASS": "已通过审核（PASS）；保留假说不代表目标酶活性已验证。"}.get(report.get('review_decision'), '未审核'),
        f"- 插件版本：`{_text(report.get('plugin_version'))}`",
        f"- 知识库版本：`{_text(report.get('knowledge_version'))}`",
        "- 结论边界：Wiki 证据与模型未核实假说分开；排序分数不是实验成功概率。",
        "",
        "## 规范化反应输入",
        "",
        "| 组分 | 名称 | SMILES | 化学计量 |",
        "|---|---|---|---|",
    ]
    normalized_input = report.get("normalized_input") or {}
    for key, label in (("reactants", "反应物"), ("products", "产物")):
        for component in normalized_input.get(key) or []:
            item = dict(component) if isinstance(component, Mapping) else {"smiles": component}
            lines.append(f"| {label} | {_cell(item.get('name'))} | `{_cell(item.get('smiles'))}` | {_cell(item.get('stoichiometry'))} |")
    lines.append("")
    warnings = report.get("input_warnings") or []
    if warnings:
        lines.extend(["输入警告：", "", *[f"- {_text(item)}" for item in warnings], ""])

    lines.extend(_object_review_table(report))

    lines.extend(_mechanism_table(report.get("mechanism_hypotheses") or []))
    lines.extend(_ranked_table("Top 辅因子类别", report.get("top_cofactor_classes") or []))
    lines.extend(_ranked_table("单酶探索方向（组内排序）", report.get("single_enzyme_candidates") or []))
    lines.extend(_ranked_table("局部候选与机制类比线索", report.get("route_components") or []))
    if report.get("diagnostic_controls"):
        lines.extend(_ranked_table("机制诊断对照（不参与候选排名）", report["diagnostic_controls"]))
    rejected_routes = any("route " in str(item) and "校验未通过" in str(item)
                          for item in report.get("limitations") or [])
    route_empty_reason = ("本轮路线提交校验失败，不能据此判定无可行路线"
                          if rejected_routes else "本轮未提交可排名路线，不能据此判定无可行路线")
    if report.get("exploratory_routes") and not report.get("top_routes"):
        route_empty_reason = "仅有降级的非推荐探索分支，见下表；不代表不存在可行路线"
    lines.extend(_ranked_table("化学机制最匹配路线", report.get("top_routes") or [], empty_reason=route_empty_reason))
    lines.extend(_exploratory_route_table(report))
    lines.extend(_wiki_route_table(report))
    lines.extend(_auxiliary_steps_table(report))
    if report.get("unmatched_capabilities"):
        lines.extend(_ranked_table("尚未匹配到酶的催化能力（不是具体酶）", report["unmatched_capabilities"]))
    if report.get("family_hints"):
        lines.extend(_ranked_table("家族线索（不计入具体蛋白数量）", report["family_hints"]))

    for role, title in (("primary", "单酶探索方向候选表"), ("route_component", "局部候选与机制类比表")):
        rows = [item for item in report.get("candidate_table") or [] if item.get("candidate_role") == role]
        components = role == "route_component"
        lines.extend([f"## {title}", "", "| 功能 | 组内排名 | 酶名（EC 编号） | 辅因子类别 | 支持机制 | 关键支持证据 | 主要冲突 | 工程化可能性 | 置信度 |" if components else "| 组内排名 | 酶名（EC 编号） | 辅因子类别 | 支持机制 | 关键支持证据 | 主要冲突 | 工程化可能性 | 置信度 |", "|---|---:|---|---|---|---|---|---|---|" if components else "|---:|---|---|---|---|---|---|---|"])
        for item in rows:
            lines.append(
                ("| {function} | {rank} | {family} | {cofactors} | {mechanism} | {evidence} | {conflicts} | {engineering} | {confidence} |" if components else "| {rank} | {family} | {cofactors} | {mechanism} | {evidence} | {conflicts} | {engineering} | {confidence} |").format(
                    function=_cell(_COMPONENT_FUNCTION_LABELS.get(item.get("component_function"), "其他局部能力")),
                    rank=_cell(item.get("rank")),
                    family=_cell(_enzyme_label(item)
                        + ("；" + item.get("display_role", "局部候选（路线绑定待核对）") if components else "")
                        + ("；" + item["verification_note"] if item.get("verification_note") else "")),
                    cofactors=_cell([_COFACTOR_LABELS.get(c, c) for c in item.get("cofactor_classes", [])]),
                    mechanism=_cell(item.get("support_mechanism")),
                    evidence=_cell(item.get("support_evidence")),
                    conflicts=_cell(item.get("contradictions")),
                    engineering=_cell(_level_label(item.get("engineering_plausibility"))),
                    confidence=_cell(_level_label(item.get("confidence"))),
                )
            )
        if not rows:
            lines.append("| — | 无充分候选 | — | — | — | 知识库缺口 | 未知 | 未确定 |")
        lines.append("")
    lines.extend(["", "## 机制路线图", "", "```mermaid", normalize_mermaid(report.get("mermaid_diagram")).rstrip(), "```", ""])

    lines.extend(["## 轻量实验区分建议", ""])
    for item in report.get("experimental_validation_suggestions") or []:
        suggestion = dict(item) if isinstance(item, Mapping) else {}
        lines.append(f"- **{_text(suggestion.get('title'))}**：{_text(suggestion.get('purpose'))}")
        distinguishes = suggestion.get("distinguishes") or []
        controls = suggestion.get("controls") or []
        readout = _text(suggestion.get("readout"))
        if distinguishes:
            lines.append(f"  - 区分：{_cell(distinguishes)}")
        if controls:
            lines.append(f"  - 对照：{_cell(controls)}")
        if readout:
            lines.append(f"  - 观察：{readout}")
    if not report.get("experimental_validation_suggestions"):
        lines.append("- 暂无足够证据提出区分性建议。")

    lines.extend(_accountability_table(report))
    lines.extend(["", "## 限制", "", *[f"- {_text(item)}" for item in report.get("limitations") or []]])
    lines.extend(["", "## 下一步", "", *[f"- {_text(item)}" for item in report.get("next_actions") or []]])
    lines.append("")
    return "\n".join(lines)


def _auxiliary_steps_table(report):
    """辅助反应不是酶候选；在用户报告中明确显示条件与实际输入输出。"""
    rows = [(route, step) for route in [*report.get("top_routes", []), *report.get("exploratory_routes", [])]
            for step in route.get("steps", []) if step.get("step_kind") == "nonenzymatic"]
    if not rows:
        return []
    return ["", "### 非酶辅助步骤（参与路线计量，不计为酶候选）", "",
            "| 路线／步骤 | 输入 → 输出 | 条件与不确定性 |", "|---|---|---|",
            *[f"| {_cell(route.get('name', '').split(':', 1)[0])}／{step['step']} | {_cell(step['input'])} → {_cell(step['output'])} | {_cell(step.get('condition_note'))} |"
              for route, step in rows], ""]


def _wiki_route_table(report):
    """证据榜只引用路线名称，不重复大段步骤，控制报告体积。"""
    rows = report.get("wiki_evidence_route_ranking") or []
    return ["", "### Wiki 证据最充分路线", "", "覆盖率仅表示步骤酶的 Wiki 来源覆盖，不证明目标底物活性。", "",
            "| 排名 | 路线 | Wiki 步骤覆盖率 |", "|---:|---|---:|",
            *[f"| {row['rank']} | {_cell(row['name'])} | {row['wiki_coverage']:.0%} |" for row in rows], ""]


def _exploratory_route_table(report):
    rows = report.get("exploratory_routes") or []
    if not rows:
        return []
    return ["", "## 非推荐探索路线（已降级）", "",
            "| 编号 | 路线 | 审核处置 | 原因 |",
            "|---:|---|---|---|",
            *[f"| {row['rank']} | {_cell(row['name'])} | {_cell(row['review_disposition'])} | {_cell(row['review_reason'])} |"
              for row in rows], ""]


def render_final_report_chat(report: Mapping[str, Any]) -> str:
    """渲染有界的默认聊天视图，不暴露内部审计 JSON。"""

    single_candidates = [_mapping(item) for item in _sequence(report.get("single_enzyme_candidates"))[:MAX_RESULT_LIMIT]]
    route_components = [_mapping(item) for item in _sequence(report.get("route_components"))[:MAX_RESULT_LIMIT]]
    cofactors = [_mapping(item) for item in _sequence(report.get("top_cofactor_classes"))[:3]]
    mechanisms = [_mapping(item) for item in _sequence(report.get("mechanism_hypotheses"))]
    routes = [_mapping(item) for item in _sequence(report.get("top_routes"))]
    notes = _sequence(_mapping(_mapping(report.get("original_input")).get("context")).get("notes"))
    request = " ".join(map(str, notes))
    route_first = bool(re.search(r"路线|通路", request) and re.search(r"分步|逐步|每一步|先给出", request))

    cofactor_labels = _COFACTOR_LABELS

    top_family = _text(single_candidates[0].get("name")) if single_candidates else "尚未匹配单酶方向"
    if single_candidates and single_candidates[0].get("candidate_kind") == MODEL_ENZYME:
        top_family += "（未经过 Wiki 核实）"
    lines = [
        "## 酶发现结论",
        "",
        f"单酶探索方向优先考虑：**{top_family}**。组内顺序只表示相对判断，不代表目标活性已验证；局部候选与机制类比只评价局部能力，路线绑定另列，两组不作总排名。",
        "",
    ]
    lines.extend(_wiki_route_table(report))
    lines.extend(_auxiliary_steps_table(report))
    if route_first:
        route = next((item for item in routes if item.get("steps")), None)
        prefix = ["## 建议路线", ""]
        if route:
            prefix.extend([
                f"**{_ROUTE_LABELS.get(_text(route.get('route_type')), '候选路线')}**；置信度 {_level_label(_confidence(route.get('confidence')))}。天然反应的连接与目标底物替换需分别核实。",
                "", "| 步骤 | 输入 → 输出 | 候选酶或所需能力 | Wiki 依据 | 目标路线的证据状态 |",
                "|---:|---|---|---|---|",
            ])
            for step in route["steps"]:
                item = _mapping(step)
                cards = _strings(item.get("card_ids"), count=2, chars=64)
                prefix.append(
                    f"| {item.get('step')} | {_cell(item.get('input'))} → {_cell(item.get('output'))} "
                    f"| {_cell(_enzyme_label(item))} | {_cell(cards or ['本轮 Wiki 未核实'])} "
                    f"| {_cell(item.get('verification_note') or '目标底物替换与路线衔接未验证')} |"
                )
            gaps = _strings(route.get("knowledge_gaps"), count=2, chars=180)
            if gaps:
                prefix.extend(["", "关键缺口：" + "；".join(gaps) + "。"])
        else:
            prefix.append("本轮仅有已降级的非推荐探索路线，见对应分区；不能据此判定不存在可行路线。"
                          if report.get("exploratory_routes") else
                          "本轮没有提交可审计的分步路线；不能据此判定不存在可行路线。")
        lines = [*prefix, "", *lines]

    lines.extend(_exploratory_route_table(report))
    for heading, families in (("单酶探索方向", single_candidates), ("局部候选与机制类比线索", route_components)):
        components = heading.startswith("局部候选")
        lines.extend(["", f"### {heading}", ""])
        if components:
            lines.extend(["只在相同化学功能内排序；用途标签区分实际路线绑定与未绑定线索，不表示目标活性已验证。", ""])
        lines.extend(["| 功能 | 组内排名 | 候选酶 | 辅因子 | 可能机制 | 主要支持证据 | 主要冲突或不确定性 | 证据状态 |" if components else "| 组内排名 | 候选酶 | 辅因子 | 可能机制 | 主要支持证据 | 主要冲突或不确定性 | 证据状态 |", "|---|---:|---|---|---|---|---|---|" if components else "|---:|---|---|---|---|---|---|"])
        if not families:
            lines.append("| — | — | 暂无充分候选 | — | — | — | 当前证据不足 | 未确定 |" if components else "| — | 暂无充分候选 | — | — | — | 当前证据不足 | 未确定 |")
        for index, item in enumerate(families, start=1):
            name = _text(item.get("name")) or "未命名候选"
            if components:
                name += "；" + item.get("display_role", "局部候选（路线绑定待核对）")
            confidence = _level_label(_confidence(item.get("confidence")))
            score = _score(item.get("score"))
            note = item.get("evidence_level") or candidate_provenance(item)["verification_note"]
            supports = _strings(item.get("supports"), count=1, chars=180)
            contradictions = _strings(item.get("contradictions"), count=1, chars=180)
            linked = [c for c in cofactors if set(c.get("mechanism_ids", [])) & set(item.get("mechanism_ids", []))]
            labels = "、".join(cofactor_labels.get(c["name"], c["name"]) for c in linked) or "未确定"
            linked_mechanisms = [m for m in _sequence(report.get("mechanism_hypotheses")) if _text(m.get("hypothesis_id")) in set(item.get("mechanism_ids", []))]
            mechanism = _clip(linked_mechanisms[0].get("summary"), 180) if linked_mechanisms else "未明确关联机制"
            event_unknowns = [
                _clip(event.get("event"), 100)
                for mechanism_row in linked_mechanisms
                for event in _sequence(mechanism_row.get("event_evidence"))
                if event.get("status") != "local_evidence"
            ]
            conflict_parts = contradictions + _strings(item.get("knowledge_gaps"), count=2, chars=180)
            if event_unknowns:
                note += "；关联机制含未验证或冲突事件"
                conflict_parts.append("未闭合事件：" + "；".join(dict.fromkeys(event_unknowns))[:220])
            conflict = "；".join(dict.fromkeys(conflict_parts)) or "仍需实验验证"
            lines.append(
                ("| {function} | {rank} | {name}（相对分 {score:g}，置信度 {confidence}） | {cofactors} | {mechanism} | {support} | {conflict} | {status} |" if components else "| {rank} | {name}（相对分 {score:g}，置信度 {confidence}） | {cofactors} | {mechanism} | {support} | {conflict} | {status} |").format(
                    function=_cell(_COMPONENT_FUNCTION_LABELS.get(item.get("component_function"), "其他局部能力")),
                    rank=item.get("rank", index),
                    name=_cell(name),
                    score=score,
                    confidence=_cell(confidence),
                    cofactors=_cell(labels),
                    mechanism=_cell(mechanism),
                    support=_cell(supports[0] if supports else "暂无直接支持"),
                    conflict=_cell(conflict),
                    status=_cell(note),
                )
            )

    if report.get("family_hints"):
        lines.extend(["", "### 家族线索（不计入具体蛋白数量）", ""])
        for item in report["family_hints"]:
            lines.append(f"- {item['name']}：{candidate_provenance(item)['verification_note']}")

    capabilities = _sequence(report.get("unmatched_capabilities"))
    if capabilities:
        lines.extend(["", "### 尚未匹配到酶的催化能力", ""])
        for raw in capabilities[:3]:
            item = _mapping(raw)
            lines.append(f"- **{_clip(item.get('name'), 260)}**：{candidate_provenance(item)['verification_note']}。")

    if report.get("diagnostic_controls"):
        lines.extend(_ranked_table("机制诊断对照（非推荐，不计入目标酶命中）", report["diagnostic_controls"]))

    if cofactors:
        names = [
            cofactor_labels.get(_text(item.get("name")), _text(item.get("name"))) + ("（未经过 Wiki 核实）" if item.get("evidence_basis") == "model_hypothesis" else "")
            for item in cofactors
            if _text(item.get("name"))
        ]
        if names:
            lines.extend(["", "### 辅因子方向", "", "、".join(names)])

    if mechanisms or routes:
        lines.extend(["", "### 可能的催化思路", ""])
    for index, mechanism_row in enumerate(mechanisms, 1):
        lines.append(
            f"- 机制 {index}：{_clip(mechanism_row.get('summary'), 260)}；"
            f"{reasoning_provenance(mechanism_row)['verification_note']}。"
        )
    for index, route in enumerate(routes, 1):
        route_name = _text(route.get("name"))
        if ": " in route_name:
            route_name = route_name.split(": ", 1)[1]
        lines.append(
            f"- 化学机制匹配路线 {index}：{_clip(route_name, 180)}（置信度 {_level_label(_confidence(route.get('confidence')))}）"
        )
        if route.get("verification_note"):
            lines.append(f"  - {route['verification_note']}")
        if route.get("support_status") == "unresolved":
            lines.append("  - 审核要求复核；此路线仅作为待解决的探索假说保留。")

    gaps: list[str] = []
    for route in routes:
        gaps.extend(_strings(route.get("knowledge_gaps"), count=2, chars=220))
    for limitation in _sequence(report.get("limitations")):
        text = _clip(limitation, 220)
        if text and text not in gaps and "未执行原子映射" in text:
            gaps.append(text)
    if gaps:
        lines.extend(["", "### 当前最重要的不确定性", ""])
        lines.extend(f"- {item}" for item in gaps[:3])

    for limitation in _sequence(report.get("limitations")):
        if str(limitation).startswith(("检索覆盖限制：", "证据核对限制：")):
            lines.extend(["", str(limitation)])
    rejected = [str(item) for item in _sequence(report.get("limitations")) if "校验未通过：" in str(item)]
    if rejected:
        lines.extend(["", "### 未纳入结果的条目", ""])
        lines.extend(f"- {item}" for item in rejected[:5])

    if report.get("review_decision") == "REVISE":
        lines = ["**未通过审核（REVISE）**：以下为待复核结果。", "", *lines,
                 "", "### 待解决的审核问题", "",
                 *["- " + _text(item) for item in report.get("required_revisions") or []]]
    elif report.get('review_decision') == 'PARTIAL_PASS':
        lines = ['**部分通过（PARTIAL_PASS）**：以下正常交付可保留对象，局部待办见逐条审核。', '', *lines]
    lines.extend(_object_review_table(report))
    lines.extend(_accountability_table(report))
    return "\n".join(lines).strip()


__all__ = [
    "build_final_report",
    "build_report_context",
    "normalize_mermaid",
    "render_final_report_chat",
    "render_final_report_markdown",
]
"""将已校验的阶段产物组装为审计包、最终 JSON、Markdown 和 Mermaid。"""
