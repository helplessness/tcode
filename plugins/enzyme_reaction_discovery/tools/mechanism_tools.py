"""持久化模型生成的机制假说，可引用的外部事实仅来自先前回读的酶 Wiki。

No chemical templates, feature scoring, cofactor mappings, fixed concern
vocabulary, or target-specific intermediate generation live here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .wiki_tools import claim_index, require_wiki_refs, wiki_packet, citation_index
from .provenance import reasoning_provenance
from .result_limits import MAX_RESULT_LIMIT, requested_result_limits


def build_mechanism_context(snapshot: Mapping[str, Any], library: Any, evidence_packet=None) -> dict[str, Any]:
    """组装机制阶段所需的反应、结果上限和已授权 Wiki 证据。"""
    run = snapshot["run"]
    document = snapshot["documents"]["01_reaction_analysis.json"]
    if document["run_id"] != run["run_id"]:
        raise ValueError("reaction artifact belongs to another run")
    data = {
        "run_id": run["run_id"],
        "reaction": document["reaction_analysis"],
        "result_limits": requested_result_limits(run.get("reaction_input") or {}),
        **(evidence_packet if evidence_packet is not None else wiki_packet(library, snapshot["run"])),
        "source_boundaries": {
            "external_facts": "enzyme_wiki_only",
            "mechanism_fragment_source": "markdown_wiki_runtime_projection",
            "model_knowledge": "allowed_as_unverified_hypotheses_not_external_evidence",
            "cross_plugin_knowledge": False,
            "preset_mechanisms": False,
            "preselected_cofactors": False,
            "target_activity_in_wiki_is_not_assumed": True,
        },
    }
    data["citation_index"] = citation_index(data)
    data["context_digest"] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def normalize_mechanism_references(item, packet):
    """把精确短编号还原为已授权 ref；仅处理引用字段，不改科学内容。"""
    claims = claim_index(packet)
    result = copy.deepcopy(item)
    def resolve(row, key):
        if row.get(key):
            row[key] = require_wiki_refs(row[key], claims)
    for key in ("wiki_refs", "anchor_wiki_refs"):
        resolve(result, key)
    for event in result.get("events", []):
        if isinstance(event, Mapping) and isinstance(event.get("evidence"), Mapping):
            resolve(event["evidence"], "wiki_refs")
    for evidence in result.get("event_evidence", []):
        resolve(evidence, "wiki_refs")
    return result


def mechanism_draft_is_current(draft, reaction):
    """同一运行、同一反应版本才可复用暂存机制；没有指纹的旧槽位也不可混用。"""
    digest = hashlib.sha256(json.dumps(reaction, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return draft.get("run_id") == reaction.get("run_id") and draft.get("reaction_digest") == digest


def saved_mechanism_rows(documents):
    """读取当前反应版本的正式机制或有效暂存槽；不把旧版本/被拒条目当作当前草稿。"""
    draft = documents.get("mechanism_hypotheses.draft.json", {})
    reaction = documents.get("01_reaction_analysis.json")
    if draft.get("slots") and reaction:
        if mechanism_draft_is_current(draft, reaction):
            return [{"slot": int(slot), "summary": row.get("summary", ""), "draft": True}
                    for slot, row in sorted(draft["slots"].items(), key=lambda pair: int(pair[0]))]
        return []  # 有失效草稿时不能退回旧版正式机制并冒充当前反应结果。
    return [{"summary": row.get("summary", ""), "draft": False}
            for row in documents.get("02_mechanisms.json", {}).get("mechanism_hypotheses", [])]


def render_saved_mechanisms(rows):
    """部分交付只展示已保存的科学内容，并明确暂存/审核状态。"""
    if not rows:
        return ""
    heading = "已保存的机制草稿（尚未正式提交，未完成科学审核）" if any(row["draft"] for row in rows) else "已保存的机制假说（未完成最终审核）"
    return "## " + heading + "\n\n" + "\n".join("- " + str(row["summary"]) for row in rows) + "\n"


def normalize_mechanism_events(hypothesis):
    """将事件内证据投影为旧产物格式；只建立位置关系，不补科学内容。"""
    events = hypothesis.get("events", [])
    if not any(isinstance(event, Mapping) for event in events):
        return hypothesis
    if not all(isinstance(event, Mapping) for event in events):
        raise ValueError("events must use either all text or all event objects")
    if hypothesis.get("event_evidence"):
        raise ValueError("inline event evidence cannot be mixed with legacy event_evidence")
    result = copy.deepcopy(hypothesis)
    result["events"], result["event_evidence"] = [], []
    for index, event in enumerate(events, 1):
        if set(event) - {"text", "evidence"} or not isinstance(event.get("text"), str):
            raise ValueError("event object requires text and optional evidence only")
        result["events"].append(event["text"])
        if "evidence" in event:
            evidence = event["evidence"]
            if not isinstance(evidence, Mapping) or set(evidence) != {"status", "wiki_refs", "reason"}:
                raise ValueError("inline evidence requires status, wiki_refs and reason")
            result["event_evidence"].append({"event_index": index, **copy.deepcopy(evidence)})
    return result


def build_mechanism_documents(
    snapshot: Mapping[str, Any], library: Any,
    hypotheses: Sequence[Mapping[str, Any]],
    cofactors: Sequence[Mapping[str, Any]],
    knowledge_gaps: Sequence[str] = (),
    *, evidence_packet=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """只负责分配 ID 和建立关联；所有科学断言均来自模型提交。"""
    run_id = snapshot["run"]["run_id"]
    source = snapshot["documents"]["01_reaction_analysis.json"]
    if source["run_id"] != run_id:
        raise ValueError("reaction artifact belongs to another run")
    limits = requested_result_limits(snapshot["run"].get("reaction_input") or {})
    if len(hypotheses) > MAX_RESULT_LIMIT or len(cofactors) > 3:
        raise ValueError("at most 10 hypotheses and 3 cofactor candidates")
    if len(hypotheses) > limits["mechanisms"]:
        raise ValueError("hypothesis count exceeds this run's requested mechanism result limit")
    if not hypotheses and not knowledge_gaps:
        raise ValueError("no mechanism requires an explicit knowledge gap")
    packet = evidence_packet if evidence_packet is not None else wiki_packet(library, snapshot["run"])
    claims = claim_index(packet)
    ledger = copy.deepcopy(source["evidence_ledger"])
    mechanisms, capabilities, cofactor_rows = [], [], []

    def statement(identifier, kind, text, refs):
        ledger.append({"statement_id": identifier, "kind": kind,
                       "statement": text, "source_refs": refs})
        return identifier

    def provenance(spec, identifier):
        origin = reasoning_provenance(spec)
        raw_refs = list(spec.get("wiki_refs", []))
        # 空引用仅允许显式的模型假说，不能把拼错/未读的 Wiki 引用降级洗白。
        refs = require_wiki_refs(raw_refs, claims) if raw_refs or origin["evidence_basis"] == "wiki_analogy" else []
        return origin, refs or [f"model_hypothesis:{identifier}"]

    for i, hypothesis in enumerate(hypotheses, 1):
        hypothesis = normalize_mechanism_events(normalize_mechanism_references(hypothesis, packet))
        identifier = f"M{i}"
        origin, refs = provenance(hypothesis, identifier)
        raw_anchor_refs = list(hypothesis.get("anchor_wiki_refs") or [])
        anchor_refs = require_wiki_refs(raw_anchor_refs, claims) if raw_anchor_refs else []
        if not set(anchor_refs) <= set(refs):
            raise ValueError("anchor_wiki_refs must be a subset of this hypothesis's wiki_refs")
        anchor_card_ids = set()
        for ref in anchor_refs:
            card_id = str(claims[ref].get("card_id") or "")
            if not card_id:
                match = re.match(r"^(ERD-CARD-[0-9]{3,}):", ref)
                card_id = match.group(1) if match else ""
            if card_id:
                anchor_card_ids.add(card_id)
        anchor_card_ids = sorted(anchor_card_ids)
        support_id = statement(f"{identifier}-analogy", "hypothesis" if origin["evidence_basis"] == "model_hypothesis" else "inference", hypothesis["analogy_reason"], refs)
        concerns = [statement(f"{identifier}-gap-{j}", "unknown", text, [f"hypothesis:{identifier}"])
                    for j, text in enumerate(hypothesis["uncertainties"], 1)]
        event_evidence, covered = [], set()
        for evidence in hypothesis.get("event_evidence", []):
            pos = evidence["event_index"]
            if pos in covered or not 1 <= pos <= len(hypothesis["events"]):
                raise ValueError("event_evidence must refer to distinct events in this hypothesis")
            covered.add(pos)
            event_refs = require_wiki_refs(evidence["wiki_refs"], claims) if evidence["wiki_refs"] else []
            state = evidence["status"]
            if state in {"local_evidence", "related_analogy", "conflict"} and not event_refs:
                raise ValueError("event evidence status requires already-read Wiki references")
            if state == "local_evidence" and any(claims[r].get("activity_status") in {"related_protein_only", "attribution_requires_review"} for r in event_refs):
                raise ValueError("related/uncertain protein evidence must not be labeled local_evidence")
            event_evidence.append({"event_id": f"{identifier}-event-{pos}", "event": hypothesis["events"][pos-1],
                                   "status": state, "wiki_refs": event_refs, "reason": evidence["reason"]})
        for pos, event in enumerate(hypothesis["events"], 1):
            if pos not in covered:
                event_evidence.append({"event_id": f"{identifier}-event-{pos}", "event": event,
                                       "status": "model_hypothesis", "wiki_refs": [],
                                       "reason": "未单独提交逐事件证据，不能从整条机制类比自动继承支持"})
        bridge_spec = dict(hypothesis.get("counterfactual_bridge") or {})
        shared_events = [
            row["event"]
            for row in event_evidence
            if row["status"] in {"local_evidence", "related_analogy"}
        ]
        missing_events = [
            row["event"]
            for row in event_evidence
            if row["status"] in {"model_hypothesis", "unmatched_capability"}
        ]
        conflicting_events = [
            row["event"] for row in event_evidence if row["status"] == "conflict"
        ]
        counterfactual_bridge = {
            "shared_events": shared_events,
            "missing_events": missing_events,
            "conflicting_events": conflicting_events,
            "native_termination": list(bridge_spec.get("native_termination") or []),
            "target_alternative_termination": list(
                bridge_spec.get("target_alternative_termination") or []
            ),
            "unsupported_assumptions": list(
                bridge_spec.get("unsupported_assumptions")
                or hypothesis["uncertainties"]
            ),
            "validation_signals": list(
                bridge_spec.get("validation_signals") or hypothesis["falsifiers"]
            ),
            "source": "explicit_wiki_alignment" if bridge_spec else "legacy_event_projection",
            "target_activity_status": "hypothesis_unvalidated",
        }
        mechanisms.append({
            "hypothesis_id": identifier,
            **origin,
            "route_type": hypothesis["route_type"],
            "summary": hypothesis["summary"],
            "knowledge_anchor_refs": anchor_refs,
            "knowledge_anchor_card_ids": anchor_card_ids,
            "required_events": list(hypothesis["events"]),
            "event_evidence": event_evidence,
            "counterfactual_bridge": counterfactual_bridge,
            "key_intermediates": list(hypothesis["intermediates"]),
            "supports": [hypothesis["analogy_reason"]],
            "contradictions": conflicting_events,
            "falsifiers": list(hypothesis["falsifiers"]),
            "assumptions": list(hypothesis["uncertainties"]),
            "confidence": hypothesis["confidence"],
        })
        for j, capability in enumerate(hypothesis["capabilities"], 1):
            capabilities.append({
                "capability_id": f"{identifier}-cap-{j}", "description": capability,
                "supports_hypotheses": [identifier], "support_refs": [support_id],
                "contradiction_refs": [], "unknown_refs": concerns,
            })
    seen = set()
    for i, candidate in enumerate(cofactors, 1):
        candidate = normalize_mechanism_references(candidate, packet)
        name = candidate["name"].strip()
        if not name or name.casefold() in seen:
            raise ValueError("cofactor names must be non-empty and unique")
        seen.add(name.casefold())
        origin, refs = provenance(candidate, f"cofactor-{i}")
        mechanism_ids = list(candidate.get("mechanism_ids", []))
        if not set(mechanism_ids) <= {m["hypothesis_id"] for m in mechanisms}:
            raise ValueError("cofactor mechanism_ids must refer to submitted hypotheses")
        statement(f"cofactor-{i}-analogy", "hypothesis" if origin["evidence_basis"] == "model_hypothesis" else "inference", candidate["reason"], refs)
        cofactor_rows.append({
            **origin,
            "rank": i, "cofactor_class": name, "supports": [candidate["reason"]],
            "mechanism_ids": mechanism_ids,
            "contradictions": [], "unknowns": list(candidate["uncertainties"]),
            "confidence": candidate["confidence"],
        })
    for i, gap in enumerate(knowledge_gaps, 1):
        statement(f"mechanism-gap-{i}", "unknown", gap, ["input:reaction"])
    common = {"schema_version": "1.0", "run_id": run_id, "evidence_ledger": ledger}
    return (
        {**common, "mechanism_hypotheses": mechanisms},
        {**common, "catalytic_capabilities": capabilities, "cofactor_candidates": cofactor_rows},
    )
