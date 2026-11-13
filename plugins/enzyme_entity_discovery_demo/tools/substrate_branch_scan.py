"""Deterministic same-substrate reaction and branch scanning.

This is a small composition primitive for the mechanism Agent.  It joins the
relations that are otherwise easy to miss when an Agent performs several
single-hop calls: a reported reaction containing the substrate, its catalysts,
their cofactors, the other participants, and any recorded intermediates.  The
result exposes trigger signals for a *weak* branch hypothesis; it never claims
that the catalyst performs the user's target reaction.
"""

from __future__ import annotations

from typing import Any

from .entity_store import EntityStore


_DIRECT_ROLES = {"left", "reactant"}
_SECOND_SUBSTRATE_ROLES = {"left", "reactant", "cosubstrate"}
_COVALENT_COFACTOR_TERMS = (
    "thdp",
    "tpp",
    "硫胺素二磷酸",
    "plp",
    "吡哆醛",
    "吡哆胺",
    "pyridoxal",
    "pyridoxamine",
    "fad",
    "黄素腺嘌呤二核苷酸",
    "sam",
    "s adenosyl",
    "coenzyme a",
    "辅酶a",
)


def _active_supports(store: EntityStore, entity_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in store.outgoing.get(entity_id, ())
        if row.get("state") == "active" and row.get("polarity") == "supports"
    ]


def _entity(store: EntityStore, entity_id: str) -> dict[str, Any]:
    return store.entity_summary(entity_id)


def _row(store: EntityStore, assertion: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity": _entity(store, assertion["object_id"]),
        "assertion_id": assertion["assertion_id"],
        "qualifiers": dict(assertion.get("qualifiers") or {}),
        "evidence_ids": list(assertion.get("evidence_ids") or []),
    }


def _looks_covalent_cofactor(entity: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(entity.get("label") or ""),
            *(str(item) for item in entity.get("aliases") or []),
        ]
    ).casefold()
    return any(term in text for term in _COVALENT_COFACTOR_TERMS)


def _catalyst_rows(store: EntityStore, reaction_id: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in store.incoming.get(reaction_id, ())
        if row.get("predicate") == "catalyzes_reported"
        and row.get("state") == "active"
        and row.get("polarity") == "supports"
    ]
    return [
        {
            "entity": _entity(store, row["subject_id"]),
            "assertion_id": row["assertion_id"],
            "attribution": row.get("attribution"),
            "evidence_ids": list(row.get("evidence_ids") or []),
        }
        for row in rows
    ]


def _scan_reaction(
    store: EntityStore,
    *,
    substrate_id: str,
    substrate_assertion: dict[str, Any],
    reaction_id: str,
) -> dict[str, Any]:
    reaction_rows = _active_supports(store, reaction_id)
    participant_rows = [
        row for row in reaction_rows if row.get("predicate") == "has_participant"
    ]
    participants = [_row(store, row) for row in participant_rows]
    substrate_role = dict(substrate_assertion.get("qualifiers") or {}).get(
        "participant_role", "unknown"
    )
    direct_substrate = substrate_role in _DIRECT_ROLES
    other_substrates = [
        item
        for item in participants
        if item["entity"]["entity_id"] != substrate_id
        and item["qualifiers"].get("participant_role") in _SECOND_SUBSTRATE_ROLES
    ]
    products = [
        item
        for item in participants
        if item["qualifiers"].get("participant_role") in {"right", "product"}
    ]
    catalysts = _catalyst_rows(store, reaction_id)
    cofactor_rows: list[dict[str, Any]] = []
    for catalyst in catalysts:
        for assertion in _active_supports(store, catalyst["entity"]["entity_id"]):
            if assertion.get("predicate") != "uses_cofactor":
                continue
            item = {
                "entity": _entity(store, assertion["object_id"]),
                "assertion_id": assertion["assertion_id"],
                "catalyst_entity_id": catalyst["entity"]["entity_id"],
                "qualifiers": dict(assertion.get("qualifiers") or {}),
                "evidence_ids": list(assertion.get("evidence_ids") or []),
            }
            cofactor_rows.append(item)
    cofactor_rows.sort(key=lambda item: item["assertion_id"])
    covalent_cofactors = [
        item for item in cofactor_rows if _looks_covalent_cofactor(item["entity"])
    ]
    capabilities = [
        _row(store, row)
        for row in reaction_rows
        if row.get("predicate") == "requires_capability"
    ]
    transformations = [
        _row(store, row)
        for row in reaction_rows
        if row.get("predicate") == "exhibits_transformation"
    ]
    intermediates = [
        _row(store, row)
        for row in reaction_rows
        if row.get("predicate") == "has_intermediate"
    ]
    assertion_ids = {
        substrate_assertion["assertion_id"],
        *(row["assertion_id"] for row in participant_rows),
        *(row["assertion_id"] for row in catalysts),
        *(row["assertion_id"] for row in cofactor_rows),
        *(row["assertion_id"] for row in capabilities),
        *(row["assertion_id"] for row in transformations),
        *(row["assertion_id"] for row in intermediates),
    }
    evidence_ids = sorted(
        {
            evidence_id
            for row in [
                substrate_assertion,
                *participant_rows,
                *catalysts,
                *cofactor_rows,
                *capabilities,
                *transformations,
                *intermediates,
            ]
            for evidence_id in row.get("evidence_ids") or []
        }
    )
    signals = [
        {
            "signal_id": "same_direct_substrate",
            "status": "present" if direct_substrate else "not_applicable",
            "basis": "目标底物作为该已报道反应的 left/reactant 参与物。"
            if direct_substrate
            else "该参与物在记录中不是 left/reactant。",
        },
        {
            "signal_id": "covalent_cofactor",
            "status": "present" if covalent_cofactors else "not_recorded",
            "cofactor_ids": [item["entity"]["entity_id"] for item in covalent_cofactors],
            "basis": "催化主体的 uses_cofactor 关系命中 ThDP/PLP/FAD/SAM/CoA 等词面。"
            if covalent_cofactors
            else "当前本体没有为该反应催化主体记录可识别的共价型辅因子。",
        },
        {
            "signal_id": "natural_second_substrate",
            "status": "present" if other_substrates else "not_recorded",
            "entity_ids": [item["entity"]["entity_id"] for item in other_substrates],
            "basis": "反应记录包含除目标底物外的 left/reactant/cosubstrate 参与物；这证明共参与，不证明其具体捕获机理。"
            if other_substrates
            else "当前反应记录没有额外的 left/reactant/cosubstrate 参与物。",
        },
        {
            "signal_id": "alternate_capture_or_release",
            "status": "not_recorded",
            "basis": "替代捕获或终止方式不作为本体事实；需要由 Agent 以类比或预测提出并单独标注。",
        },
    ]
    present_count = sum(item["status"] == "present" for item in signals)
    return {
        "substrate": _entity(store, substrate_id),
        "substrate_assertion_id": substrate_assertion["assertion_id"],
        "substrate_role": substrate_role,
        "reaction": _entity(store, reaction_id),
        "reaction_direction": (store.entities[reaction_id].get("direction") or "unknown"),
        "participants": participants,
        "other_substrates": other_substrates,
        "products": products,
        "catalysts": catalysts,
        "cofactors": cofactor_rows,
        "covalent_cofactors": covalent_cofactors,
        "capabilities": capabilities,
        "transformations": transformations,
        "intermediates": intermediates,
        "trigger_signals": signals,
        "present_trigger_count": present_count,
        "weak_hypothesis_required": present_count >= 2,
        "assertion_ids": sorted(assertion_ids),
        "evidence_ids": evidence_ids,
        "analogy_status": "mechanistic_analogy",
        "target_activity_proven": False,
        "knowledge_boundary": "该扫描只连接本地已报道关系；它不证明催化主体能生成用户目标产物。",
    }


def scan_same_substrate_branches(
    store: EntityStore,
    *,
    substrate_ids: list[str],
    limit: int,
) -> dict[str, Any]:
    """Return bounded reaction branches for each supplied chemical entity."""

    if not substrate_ids:
        raise ValueError("substrate_ids must not be empty")
    if len(substrate_ids) > 4:
        raise ValueError("substrate_ids must contain at most 4 items")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    unknown = sorted(item for item in set(substrate_ids) if item not in store.entities)
    if unknown:
        raise ValueError(f"unknown substrate_ids: {', '.join(unknown)}")
    wrong = sorted(
        item
        for item in set(substrate_ids)
        if store.entities[item]["entity_type"] not in {"ChemicalEntity", "ChemicalClass"}
    )
    if wrong:
        raise ValueError(f"substrate_ids must reference chemical entities: {', '.join(wrong)}")

    scans: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for substrate_id in substrate_ids:
        for assertion in store.incoming.get(substrate_id, ()):
            if (
                assertion.get("predicate") != "has_participant"
                or assertion.get("state") != "active"
                or assertion.get("polarity") != "supports"
            ):
                continue
            reaction_id = assertion["subject_id"]
            if store.entities.get(reaction_id, {}).get("entity_type") != "Reaction":
                continue
            key = (substrate_id, reaction_id)
            if key in seen:
                continue
            seen.add(key)
            scans.append(
                _scan_reaction(
                    store,
                    substrate_id=substrate_id,
                    substrate_assertion=assertion,
                    reaction_id=reaction_id,
                )
            )
    scans.sort(
        key=lambda item: (
            item["substrate"]["entity_id"],
            -item["present_trigger_count"],
            item["reaction"]["label"].casefold(),
            item["reaction"]["entity_id"],
        )
    )
    scans = scans[:limit]
    return {
        "status": "matched" if scans else "no_recorded_same_substrate_reaction",
        "scans": scans,
        "total": len(scans),
        "evidence_required": bool(scans),
        "knowledge_boundary": "同底物命中和触发信号是弱假说的结构线索，不是目标反应活性证据。",
    }
