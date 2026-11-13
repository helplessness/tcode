"""Deterministic retrieval of reported reactions sharing explicit graph features."""

from __future__ import annotations

from typing import Any

from .entity_query import _page
from .entity_store import EntityStore


def _validate_feature_ids(
    store: EntityStore,
    values: list[str],
    allowed_types: set[str],
    label: str,
) -> None:
    missing = sorted(item for item in set(values) if item not in store.entities)
    if missing:
        raise ValueError(f"unknown {label}: {', '.join(missing)}")
    wrong = sorted(
        value
        for value in set(values)
        if store.entities[value]["entity_type"] not in allowed_types
    )
    if wrong:
        raise ValueError(f"{label} have incompatible entity types: {', '.join(wrong)}")


def find_reaction_analogs(
    store: EntityStore,
    *,
    participant_ids: list[str],
    transformation_ids: list[str],
    capability_ids: list[str],
    participant_roles: list[str],
    minimum_matches: int,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if not participant_ids and not transformation_ids and not capability_ids:
        raise ValueError(
            "provide at least one participant_id, transformation_id or capability_id"
        )
    _validate_feature_ids(
        store,
        participant_ids,
        {"ChemicalEntity", "ChemicalClass"},
        "participant_ids",
    )
    _validate_feature_ids(
        store,
        transformation_ids,
        {"TransformationPattern"},
        "transformation_ids",
    )
    _validate_feature_ids(
        store,
        capability_ids,
        {"CatalyticCapability"},
        "capability_ids",
    )
    role_filter = set(participant_roles)
    unknown_roles = sorted(
        role_filter - {"reactant", "product", "cosubstrate", "left", "right"}
    )
    if unknown_roles:
        raise ValueError(f"unknown participant_roles: {', '.join(unknown_roles)}")
    if not 1 <= minimum_matches <= 22:
        raise ValueError("minimum_matches must be between 1 and 22")
    requested_participants = set(participant_ids)
    requested_transformations = set(transformation_ids)
    requested_capabilities = set(capability_ids)

    results: list[dict[str, Any]] = []
    feature_ids = [*participant_ids, *transformation_ids, *capability_ids]
    reaction_ids = store.candidate_reactions(feature_ids, ["has_participant", "exhibits_transformation", "requires_capability"])
    if len(reaction_ids) > 1000:
        raise ValueError("analogy search has too many reactions; narrow the feature IDs")
    for reaction_id in reaction_ids:
        reaction_assertions = [
            row
            for row in store.outgoing.get(reaction_id, ())
            if row.get("state") == "active" and row.get("polarity") == "supports"
        ]
        participant_rows = [
            row
            for row in reaction_assertions
            if row["predicate"] == "has_participant"
            and (
                not role_filter
                or str((row.get("qualifiers") or {}).get("participant_role"))
                in role_filter
            )
        ]
        transformation_rows = [
            row
            for row in reaction_assertions
            if row["predicate"] == "exhibits_transformation"
        ]
        capability_rows = [
            row
            for row in reaction_assertions
            if row["predicate"] == "requires_capability"
        ]
        matched_participants = [
            row for row in participant_rows if row["object_id"] in requested_participants
        ]
        matched_transformations = [
            row
            for row in transformation_rows
            if row["object_id"] in requested_transformations
        ]
        matched_capabilities = [
            row for row in capability_rows if row["object_id"] in requested_capabilities
        ]
        matched_rows = [
            *matched_participants,
            *matched_transformations,
            *matched_capabilities,
        ]
        if len(matched_rows) < minimum_matches:
            continue

        catalyst_rows = [
            row
            for row in store.incoming.get(reaction_id, ())
            if row["predicate"] == "catalyzes_reported"
            and row.get("state") == "active"
            and row.get("polarity") == "supports"
        ]
        catalysts = []
        for row in catalyst_rows:
            catalysts.append(
                {
                    "entity": store.entity_summary(row["subject_id"]),
                    "assertion_id": row["assertion_id"],
                    "attribution": row["attribution"],
                    "evidence_ids": list(row.get("evidence_ids") or []),
                }
            )
        catalyst_types = {
            store.entities[row["subject_id"]]["entity_type"] for row in catalyst_rows
        }
        if "Protein" in catalyst_types:
            reported_scope = "reported_exact"
        elif "ProteinGroup" in catalyst_types:
            reported_scope = "reported_related"
        else:
            reported_scope = "unresolved"

        def match_item(row: dict[str, Any], kind: str) -> dict[str, Any]:
            result = {
                "kind": kind,
                "entity": store.entity_summary(row["object_id"]),
                "assertion_id": row["assertion_id"],
                "evidence_ids": list(row.get("evidence_ids") or []),
            }
            if kind == "participant":
                result["qualifiers"] = dict(row.get("qualifiers") or {})
            return result

        matches = [
            *(match_item(row, "participant") for row in matched_participants),
            *(match_item(row, "transformation") for row in matched_transformations),
            *(match_item(row, "capability") for row in matched_capabilities),
        ]
        matches.sort(key=lambda row: (row["kind"], row["entity"]["entity_id"]))
        all_evidence_ids = sorted(
            {
                evidence_id
                for row in [*matched_rows, *catalyst_rows]
                for evidence_id in row.get("evidence_ids") or []
            }
        )
        results.append(
            {
                "reaction": store.entity_summary(reaction_id),
                "reported_scope": reported_scope,
                "match_count": len(matched_rows),
                "matches": matches,
                "catalysts": catalysts,
                "evidence_ids": all_evidence_ids,
                "analogy_status": "mechanistic_analogy",
                "target_activity_proven": False,
            }
        )

    results.sort(
        key=lambda row: (
            -row["match_count"],
            row["reaction"]["label"].casefold(),
            row["reaction"]["entity_id"],
        )
    )
    criteria = {
        "participant_ids": sorted(requested_participants),
        "transformation_ids": sorted(requested_transformations),
        "capability_ids": sorted(requested_capabilities),
        "participant_roles": sorted(role_filter),
        "minimum_matches": minimum_matches,
    }
    page, next_cursor, total = _page(
        results,
        kind="ana",
        criteria=criteria,
        cursor=cursor,
        limit=limit,
    )
    return {
        "status": "matched" if total else "no_recorded_analog",
        "analogs": page,
        "total": total,
        "next_cursor": next_cursor,
        "ranking_note": "Results are ordered by explicit matched graph features, not by catalytic fitness.",
        "evidence_required": True,
        "knowledge_boundary": (
            "Known reaction matches are precedents only. They do not establish activity on the target reaction."
            if total
            else "No matching precedent was recorded in the current five-card bundle."
        ),
    }
