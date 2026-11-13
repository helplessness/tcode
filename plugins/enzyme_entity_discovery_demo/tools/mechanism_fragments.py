"""Derive reaction-scoped mechanism fragments from the validated entity graph."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from .entity_store import EntityStore


class MechanismFragmentError(ValueError):
    """Signal an invalid bounded fragment query."""


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", text).split())


def _values(value: Any, label: str, maximum: int = 12) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise MechanismFragmentError(f"{label} must contain at most {maximum} items")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or len(text) > 240:
            raise MechanismFragmentError(f"{label} contains an invalid value")
        if text not in result:
            result.append(text)
    return result


def _derive_fragments(store: EntityStore, reaction_ids: list[str]) -> list[dict[str, Any]]:
    """Project graph facts without adding target-specific chemistry."""

    store.load()
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    related = set(reaction_ids)
    for reaction_id in reaction_ids:
        related.update(row["subject_id"] for row in store.incoming.get(reaction_id, ()) if row["predicate"] == "catalyzes_reported")
    for entity_id in related:
        rows = store.outgoing.get(entity_id, ())
        for row in rows:
            if row.get("state") != "active" or row.get("polarity") != "supports":
                continue
            outgoing[str(row["subject_id"])].append(row)
            incoming[str(row["object_id"])].append(row)
    for reaction_id in reaction_ids:
        for row in store.incoming.get(reaction_id, ()):
            if row.get("state") != "active" or row.get("polarity") != "supports":
                continue
            incoming[str(row["object_id"])].append(row)

    fragments: list[dict[str, Any]] = []
    for reaction_id in reaction_ids:
        reaction = store.entities[reaction_id]
        if reaction.get("entity_type") != "Reaction":
            continue
        reaction_rows = outgoing.get(reaction_id, [])
        participants = [r for r in reaction_rows if r.get("predicate") == "has_participant"]
        transformations = [
            r for r in reaction_rows if r.get("predicate") == "exhibits_transformation"
        ]
        intermediates = [r for r in reaction_rows if r.get("predicate") == "has_intermediate"]
        capabilities = [r for r in reaction_rows if r.get("predicate") == "requires_capability"]
        catalysts = [r for r in incoming.get(reaction_id, []) if r.get("predicate") == "catalyzes_reported"]
        catalyst_ids = {str(r["subject_id"]) for r in catalysts}
        cofactors = [
            r
            for catalyst_id in catalyst_ids
            for r in outgoing.get(catalyst_id, [])
            if r.get("predicate") == "uses_cofactor"
        ]
        memberships = [
            r
            for catalyst_id in catalyst_ids
            for r in outgoing.get(catalyst_id, [])
            if r.get("predicate") == "member_of"
        ]
        fact_rows = [
            *participants,
            *transformations,
            *intermediates,
            *capabilities,
            *catalysts,
            *cofactors,
            *memberships,
        ]

        def summaries(rows: list[dict[str, Any]], endpoint: str) -> list[dict[str, Any]]:
            return [store.entity_summary(str(row[endpoint])) for row in rows]

        left_roles = {"reactant", "cosubstrate", "left"}
        right_roles = {"product", "right"}
        fragments.append(
            {
                "fragment_id": f"fragment:{reaction_id}",
                "reaction": store.entity_summary(reaction_id),
                "participants": [
                    {
                        "entity": store.entity_summary(str(row["object_id"])),
                        "role": str((row.get("qualifiers") or {}).get("participant_role") or "unknown"),
                        "assertion_id": row["assertion_id"],
                        "evidence_ids": list(row.get("evidence_ids") or []),
                    }
                    for row in participants
                ],
                "transformations": summaries(transformations, "object_id"),
                "capabilities": summaries(capabilities, "object_id"),
                "intermediates": summaries(intermediates, "object_id"),
                "cofactors": summaries(cofactors, "object_id"),
                "catalysts": summaries(catalysts, "subject_id"),
                "families": summaries(memberships, "object_id"),
                "native_entry_entity_ids": sorted(
                    {
                        str(row["object_id"])
                        for row in participants
                        if str((row.get("qualifiers") or {}).get("participant_role") or "")
                        in left_roles
                    }
                ),
                "native_termination_entity_ids": sorted(
                    {
                        str(row["object_id"])
                        for row in participants
                        if str((row.get("qualifiers") or {}).get("participant_role") or "")
                        in right_roles
                    }
                ),
                "assertion_ids": sorted({str(row["assertion_id"]) for row in fact_rows}),
                "evidence_ids": sorted(
                    {
                        str(evidence_id)
                        for row in fact_rows
                        for evidence_id in row.get("evidence_ids") or []
                    }
                ),
                "derivation": {
                    "method": "deterministic_entity_graph_projection",
                    "bundle_digest": store.bundle_digest,
                    "target_specific_rule": False,
                },
                "target_activity_proven": False,
            }
        )
    return fragments


def search_mechanism_fragments(
    store: EntityStore,
    arguments: Mapping[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    """Match explicit graph dimensions; never turn match counts into fitness."""

    query = {
        "participant": _values(arguments.get("participant_ids"), "participant_ids"),
        "transformation": _values(arguments.get("transformation_ids"), "transformation_ids"),
        "capability": _values(arguments.get("capability_ids"), "capability_ids"),
        "cofactor": _values(arguments.get("cofactor_ids"), "cofactor_ids"),
        "intermediate": _values(arguments.get("intermediate_ids"), "intermediate_ids"),
        "term": _values(arguments.get("required_terms"), "required_terms", 8),
    }
    if not any(query.values()):
        raise MechanismFragmentError("mechanism fragment search requires one query dimension")

    results: list[dict[str, Any]] = []
    for fragment in _derive_fragments(store, store.fragment_reactions(query)):
        actual = {
            "participant": {
                str(row["entity"]["entity_id"]) for row in fragment["participants"]
            },
            "transformation": {str(row["entity_id"]) for row in fragment["transformations"]},
            "capability": {str(row["entity_id"]) for row in fragment["capabilities"]},
            "cofactor": {str(row["entity_id"]) for row in fragment["cofactors"]},
            "intermediate": {str(row["entity_id"]) for row in fragment["intermediates"]},
        }
        text_rows = [
            fragment["reaction"],
            *[row["entity"] for row in fragment["participants"]],
            *fragment["transformations"],
            *fragment["capabilities"],
            *fragment["cofactors"],
            *fragment["intermediates"],
            *fragment["catalysts"],
            *fragment["families"],
        ]
        haystack = " ".join(
            _normalize(value)
            for row in text_rows
            for value in [row.get("label"), *(row.get("aliases") or [])]
        )
        matched: dict[str, list[str]] = {}
        missing: dict[str, list[str]] = {}
        for dimension, requested in query.items():
            if not requested:
                continue
            hits = (
                [value for value in requested if _normalize(value) in haystack]
                if dimension == "term"
                else [value for value in requested if value in actual[dimension]]
            )
            if hits:
                matched[dimension] = hits
            absent = [value for value in requested if value not in hits]
            if absent:
                missing[dimension] = absent
        if not matched:
            continue
        flags: list[str] = []
        if set(matched) == {"cofactor"}:
            flags.append("仅共享辅因子，不能据此推断目标机制或目标活性。")
        if "participant" in matched and any(
            query[name] and name not in matched for name in ("transformation", "capability")
        ):
            flags.append("共享参与物，但所请求的转化或催化能力没有命中。")
        results.append(
            {
                **fragment,
                "matched_dimensions": matched,
                "missing_query_dimensions": missing,
                "contrast_flags": flags,
                "match_dimension_count": len(matched),
                "match_item_count": sum(len(values) for values in matched.values()),
            }
        )
    results.sort(
        key=lambda row: (
            -row["match_dimension_count"],
            -row["match_item_count"],
            row["fragment_id"],
        )
    )
    bounded = results[:limit]
    return {
        "status": "matched" if bounded else "no_recorded_fragment",
        "query": query,
        "fragments": bounded,
        "total": len(results),
        "bundle_digest": store.bundle_digest,
        "evidence_required": bool(bounded),
        "ranking_note": "按显式图维度命中排序；该次序不是催化适用性或工程成功率。",
        "boundary": "片段是本体事实投影；目标对齐、替代终止和工程可行性仍是未验证假说。",
    }


__all__ = ["MechanismFragmentError", "search_mechanism_fragments"]
