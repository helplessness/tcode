"""Bounded, deterministic route discovery over reported reaction facts."""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

from .entity_store import EntityStore
from .entity_validation import canonical_bytes


_CHEMICAL_TYPES = {"ChemicalEntity", "ChemicalClass"}
_ENERGY_MARKERS = ("atp", "adp", "nad", "nadh", "nadp", "fadh", "sam")


def _validate_chemical_ids(store: EntityStore, values: list[str], label: str) -> list[str]:
    identifiers = list(dict.fromkeys(str(item) for item in values))
    missing = sorted(item for item in set(identifiers) if item not in store.entities)
    if missing:
        raise ValueError(f"unknown {label}: {', '.join(missing)}")
    wrong = sorted(
        item
        for item in identifiers
        if store.entities[item]["entity_type"] not in _CHEMICAL_TYPES
    )
    if wrong:
        raise ValueError(f"{label} contain non-chemical entities: {', '.join(wrong)}")
    return identifiers


def _reported_reaction_options(store: EntityStore, reaction_ids: list[str]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for reaction_id in reaction_ids:
        if store.entities[reaction_id]["entity_type"] != "Reaction":
            continue
        outgoing = [
            row
            for row in store.outgoing.get(reaction_id, ())
            if row.get("state") == "active" and row.get("polarity") == "supports"
        ]
        participants = [row for row in outgoing if row["predicate"] == "has_participant"]
        catalysts = [
            row
            for row in store.incoming.get(reaction_id, ())
            if row.get("state") == "active"
            and row.get("polarity") == "supports"
            and row.get("predicate") == "catalyzes_reported"
        ]
        if not participants or not catalysts:
            continue
        roles: dict[str, list[dict[str, Any]]] = {}
        for row in participants:
            role = str((row.get("qualifiers") or {}).get("participant_role") or "unknown")
            roles.setdefault(role, []).append(row)
        reversible = any(
            str((row.get("qualifiers") or {}).get("reaction_direction")) == "reversible"
            for row in participants
        ) or any(
            str((row.get("qualifiers") or {}).get("direction")) == "reversible"
            for row in catalysts
        )
        if roles.get("left") or roles.get("right"):
            forward_inputs = roles.get("left", [])
            forward_outputs = roles.get("right", [])
            cosubstrates: list[dict[str, Any]] = []
        else:
            forward_inputs = roles.get("reactant", [])
            forward_outputs = roles.get("product", [])
            cosubstrates = roles.get("cosubstrate", [])
        if forward_inputs and forward_outputs:
            options.append(
                _reaction_option(
                    store,
                    reaction_id,
                    "forward",
                    forward_inputs,
                    cosubstrates,
                    forward_outputs,
                    catalysts,
                    outgoing,
                )
            )
        if reversible and forward_inputs and forward_outputs:
            options.append(
                _reaction_option(
                    store,
                    reaction_id,
                    "reverse",
                    forward_outputs,
                    [],
                    [*forward_inputs, *cosubstrates],
                    catalysts,
                    outgoing,
                )
            )
    return options


def _reaction_option(
    store: EntityStore,
    reaction_id: str,
    direction: str,
    input_rows: list[dict[str, Any]],
    cosubstrate_rows: list[dict[str, Any]],
    output_rows: list[dict[str, Any]],
    catalyst_rows: list[dict[str, Any]],
    outgoing: list[dict[str, Any]],
) -> dict[str, Any]:
    cofactor_rows = [
        row
        for catalyst in catalyst_rows
        for row in store.outgoing.get(catalyst["subject_id"], ())
        if row.get("state") == "active"
        and row.get("polarity") == "supports"
        and row.get("predicate") == "uses_cofactor"
    ]
    cofactor_rows.extend(
        row
        for row in outgoing
        if row.get("predicate") == "requires_cofactor" and row.get("polarity") == "supports"
    )
    nonrequirement_rows = [
        row
        for row in outgoing
        if row.get("predicate") == "requires_cofactor" and row.get("polarity") == "opposes"
    ]
    condition_rows = [row for row in outgoing if row.get("predicate") == "constrained_by"]
    core_rows = {
        row["assertion_id"]: row
        for row in [
            *input_rows,
            *cosubstrate_rows,
            *output_rows,
            *catalyst_rows,
            *cofactor_rows,
            *nonrequirement_rows,
            *condition_rows,
        ]
    }
    return {
        "option_id": f"{reaction_id}:{direction}",
        "reaction_id": reaction_id,
        "direction": direction,
        "primary_input_ids": sorted({row["object_id"] for row in input_rows}),
        "cosubstrate_ids": sorted({row["object_id"] for row in cosubstrate_rows}),
        "output_ids": sorted({row["object_id"] for row in output_rows}),
        "catalyst_ids": sorted({row["subject_id"] for row in catalyst_rows}),
        "cofactor_ids": sorted({row["object_id"] for row in cofactor_rows}),
        "explicit_nonrequired_cofactor_classes": sorted(
            {row["object_id"] for row in nonrequirement_rows}
        ),
        "condition_constraint_ids": sorted({row["object_id"] for row in condition_rows}),
        "assertion_ids": sorted(core_rows),
        "evidence_ids": sorted(
            {
                evidence_id
                for row in core_rows.values()
                for evidence_id in row.get("evidence_ids") or []
            }
        ),
    }


def _is_energy_resource(store: EntityStore, entity_id: str) -> bool:
    entity = store.entities[entity_id]
    text = " ".join(
        [entity_id, str(entity.get("label") or ""), *(entity.get("aliases") or [])]
    ).casefold()
    return any(marker in text for marker in _ENERGY_MARKERS)


def _step_summary(
    store: EntityStore,
    option: dict[str, Any],
    available_before: set[str],
) -> dict[str, Any]:
    required = set(option["primary_input_ids"]) | set(option["cosubstrate_ids"])
    missing_inputs = sorted(required - available_before)
    return {
        "reaction": store.entity_summary(option["reaction_id"]),
        "direction": option["direction"],
        "primary_inputs": [store.entity_summary(item) for item in option["primary_input_ids"]],
        "cosubstrates": [store.entity_summary(item) for item in option["cosubstrate_ids"]],
        "outputs": [store.entity_summary(item) for item in option["output_ids"]],
        "missing_input_ids": missing_inputs,
        "catalysts": [store.entity_summary(item) for item in option["catalyst_ids"]],
        "cofactor_requirements": [store.entity_summary(item) for item in option["cofactor_ids"]],
        "explicit_nonrequired_cofactor_classes": [
            store.entity_summary(item)
            for item in option["explicit_nonrequired_cofactor_classes"]
        ],
        "condition_constraints": [
            store.entity_summary(item) for item in option["condition_constraint_ids"]
        ],
        "assertion_ids": list(option["assertion_ids"]),
        "evidence_ids": list(option["evidence_ids"]),
        "reported_scope": (
            "reported_exact"
            if any(
                store.entities[item]["entity_type"] == "Protein"
                for item in option["catalyst_ids"]
            )
            else "reported_related"
        ),
        "target_pathway_proven": False,
    }


def _route_summary(
    store: EntityStore,
    *,
    path: list[dict[str, Any]],
    start_ids: list[str],
    target_ids: list[str],
    available_ids: list[str],
    missing_inputs: set[str],
) -> dict[str, Any]:
    available = set(start_ids) | set(available_ids)
    steps = []
    for option in path:
        step = _step_summary(store, option, available)
        steps.append(step)
        available.update(option["output_ids"])
    cofactor_ids = sorted({item for option in path for item in option["cofactor_ids"]})
    missing_cofactors = sorted(set(cofactor_ids) - set(available_ids) - set(start_ids))
    required_material_ids = {
        item
        for option in path
        for item in [*option["primary_input_ids"], *option["cosubstrate_ids"]]
    }
    energy_ids = sorted(
        item
        for item in required_material_ids | set(cofactor_ids)
        if _is_energy_resource(store, item)
    )
    missing_energy = sorted(set(energy_ids).intersection(missing_inputs))
    later_inputs = {
        item
        for option in path[1:]
        for item in [*option["primary_input_ids"], *option["cosubstrate_ids"]]
    }
    byproducts = sorted(
        {
            item
            for option in path
            for item in option["output_ids"]
            if item not in set(target_ids) and item not in later_inputs
        }
    )
    conditions_recorded = any(option["condition_constraint_ids"] for option in path)
    assertion_ids = sorted({item for option in path for item in option["assertion_ids"]})
    evidence_ids = sorted({item for option in path for item in option["evidence_ids"]})
    route_core = {
        "start_entity_ids": start_ids,
        "target_entity_ids": target_ids,
        "available_entity_ids": available_ids,
        "step_options": [option["option_id"] for option in path],
    }
    route_ref = "route_" + hashlib.sha256(canonical_bytes(route_core)).hexdigest()[:20]
    auxiliary_needs = []
    if missing_cofactors:
        auxiliary_needs.append("cofactor_supply_or_recycling_unresolved")
    if missing_energy:
        auxiliary_needs.append("energy_supply_or_regeneration_unresolved")
    if byproducts:
        auxiliary_needs.append("byproduct_accumulation_unassessed")
    if not conditions_recorded:
        auxiliary_needs.append("condition_compatibility_unassessed")
    auxiliary_needs.append("intermediate_stability_and_toxicity_unassessed")
    return {
        "route_ref": route_ref,
        "route_kind": "reported_reaction_path",
        "steps": steps,
        "assertion_ids": assertion_ids,
        "evidence_ids": evidence_ids,
        "closure": {
            "material": {
                "status": "closed" if not missing_inputs else "gap",
                "missing_entity_ids": sorted(missing_inputs),
            },
            "direction": {"status": "reported", "unsupported_steps": []},
            "cofactor_resource": {
                "status": "closed" if not missing_cofactors else "gap",
                "required_entity_ids": cofactor_ids,
                "missing_entity_ids": missing_cofactors,
            },
            "energy": {
                "status": "closed" if not missing_energy else "gap",
                "required_entity_ids": energy_ids,
                "missing_entity_ids": missing_energy,
            },
            "byproducts": {
                "status": "unknown" if byproducts else "not_applicable",
                "unconsumed_output_ids": byproducts,
            },
            "conditions": {
                "status": "recorded" if conditions_recorded else "unknown",
                "knowledge_boundary": (
                    "Only explicit ConditionConstraint assertions are reported."
                    if conditions_recorded
                    else "No condition constraints were recorded for these steps in the five-card bundle."
                ),
            },
            "intermediate_safety": {
                "status": "unknown",
                "knowledge_boundary": "Instability, volatility and toxicity are not modeled in the five-card bundle.",
            },
        },
        "auxiliary_needs": auxiliary_needs,
        "target_pathway_proven": False,
    }


def find_routes(
    store: EntityStore,
    *,
    start_entity_ids: list[str],
    target_entity_ids: list[str],
    available_entity_ids: list[str],
    max_steps: int,
    max_routes: int,
) -> dict[str, Any]:
    starts = _validate_chemical_ids(store, start_entity_ids, "start_entity_ids")
    targets = _validate_chemical_ids(store, target_entity_ids, "target_entity_ids")
    available_extra = _validate_chemical_ids(store, available_entity_ids, "available_entity_ids")
    if not starts or not targets:
        raise ValueError("start_entity_ids and target_entity_ids must not be empty")
    if not 1 <= max_steps <= 4:
        raise ValueError("max_steps must be between 1 and 4")
    if not 1 <= max_routes <= 8:
        raise ValueError("max_routes must be between 1 and 8")
    initial = frozenset([*starts, *available_extra])
    target_set = set(targets)
    option_cache: dict[str, list[dict[str, Any]]] = {}
    queue = deque([(initial, [], frozenset())])
    best_missing_by_state: dict[tuple[frozenset[str], int], int] = {(initial, 0): 0}
    found: list[tuple[list[dict[str, Any]], set[str]]] = []
    while queue:
        if len(queue) > 2000:
            raise ValueError("route search expanded beyond 2000 states; narrow the input entities")
        state, path, missing_so_far = queue.popleft()
        if target_set <= set(state) and path:
            found.append((path, set(missing_so_far)))
            continue
        if len(path) >= max_steps:
            continue
        used_reactions = {item["reaction_id"] for item in path}
        reaction_ids = store.candidate_reactions(sorted(state), ["has_participant"])
        if len(reaction_ids) > 1000:
            raise ValueError("route search has too many neighboring reactions; narrow the input entities")
        for reaction_id in reaction_ids:
            if reaction_id not in option_cache:
                option_cache[reaction_id] = _reported_reaction_options(store, [reaction_id])
        for option in (item for reaction_id in reaction_ids for item in option_cache[reaction_id]):
            if option["reaction_id"] in used_reactions:
                continue
            if not set(option["primary_input_ids"]).intersection(state):
                continue
            new_outputs = set(option["output_ids"]) - set(state)
            if not new_outputs:
                continue
            required = set(option["primary_input_ids"]) | set(option["cosubstrate_ids"])
            new_missing = set(missing_so_far) | (required - set(state))
            if len(new_missing) > 6:
                continue
            new_state = frozenset(set(state) | set(option["output_ids"]))
            key = (new_state, len(path) + 1)
            previous = best_missing_by_state.get(key)
            if previous is not None and previous < len(new_missing):
                continue
            best_missing_by_state[key] = len(new_missing)
            queue.append((new_state, [*path, option], frozenset(new_missing)))
    found.sort(
        key=lambda item: (
            len(item[1]),
            len(item[0]),
            [step["option_id"] for step in item[0]],
        )
    )
    routes = [
        _route_summary(
            store,
            path=path,
            start_ids=starts,
            target_ids=targets,
            available_ids=available_extra,
            missing_inputs=missing,
        )
        for path, missing in found[:max_routes]
    ]
    return {
        "status": "matched" if routes else "no_local_route",
        "routes": routes,
        "total": len(found),
        "search_scope": {
            "max_steps": max_steps,
            "reported_reactions_only": True,
            "allows_explicit_material_gaps": True,
            "family_activity_propagation": False,
        },
        "ranking_note": "Routes are ordered by missing inputs, step count and stable IDs; this is not a feasibility score.",
        "knowledge_boundary": (
            "Reported steps remain facts about their recorded reactions; their combination and target-pathway use are hypotheses."
            if routes
            else "No route in the current five-card graph reached the requested target within the step bound."
        ),
    }
