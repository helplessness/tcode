"""Controlled entity resolution, one-hop relations and evidence disclosure."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .entity_store import EntityStore, normalize_term
from .entity_validation import canonical_bytes


_CURSOR = re.compile(r"^(rel|ana)_([a-f0-9]{16})_([0-9]{1,6})$")


def _page(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    criteria: Mapping[str, Any],
    cursor: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None, int]:
    signature = hashlib.sha256(canonical_bytes(dict(criteria))).hexdigest()[:16]
    offset = 0
    if cursor:
        match = _CURSOR.fullmatch(str(cursor))
        if not match or match.group(1) != kind or match.group(2) != signature:
            raise ValueError("cursor does not belong to this query")
        offset = int(match.group(3))
        if offset > len(rows):
            raise ValueError("cursor offset is outside the result set")
    page = rows[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        f"{kind}_{signature}_{next_offset}" if next_offset < len(rows) else None
    )
    return page, next_cursor, len(rows)


def resolve_entities(
    store: EntityStore,
    *,
    query: str,
    entity_types: list[str] | None,
    limit: int,
) -> dict[str, Any]:
    normalized_query = normalize_term(query)
    if not normalized_query:
        raise ValueError("query must contain a searchable entity name or ID")
    allowed_types = set(entity_types or [])
    known_types = store.entity_types()
    unknown_types = sorted(allowed_types - known_types)
    if unknown_types:
        raise ValueError(f"unknown entity_types: {', '.join(unknown_types)}")

    exact_ids = {
        entity_id
        for entity_id in store.alias_index.get(normalized_query, set())
        if not allowed_types or store.entities[entity_id]["entity_type"] in allowed_types
    }
    if exact_ids:
        results = []
        for entity_id in sorted(exact_ids):
            row = store.entity_summary(entity_id)
            row["match_kind"] = "exact_normalized"
            results.append(row)
        status = "matched" if len(results) == 1 else "ambiguous"
        return {
            "status": status,
            "normalized_query": normalized_query,
            "results": results[:limit],
            "total": len(results),
            "knowledge_boundary": "Identity matches are local facts; scientific claims still require evidence reading.",
        }

    query_tokens = set(normalized_query.split())
    candidates: list[tuple[int, str, str]] = []
    for entity_id in store.suggest_ids(normalized_query):
        entity = store.entities[entity_id]
        if allowed_types and entity["entity_type"] not in allowed_types:
            continue
        terms = [entity_id, entity["label"], *(entity.get("aliases") or [])]
        best_rank = 0
        best_kind = ""
        for term in terms:
            normalized_term = normalize_term(term)
            term_tokens = set(normalized_term.split())
            if normalized_query in normalized_term or normalized_term in normalized_query:
                rank, kind = 3, "substring"
            elif query_tokens and query_tokens <= term_tokens:
                rank, kind = 2, "all_tokens"
            elif query_tokens.intersection(term_tokens):
                rank, kind = 1, "token_overlap"
            else:
                continue
            if rank > best_rank:
                best_rank, best_kind = rank, kind
        if best_rank:
            candidates.append((best_rank, entity_id, best_kind))
    candidates.sort(key=lambda item: (-item[0], store.entities[item[1]]["label"].casefold(), item[1]))
    results = []
    for _, entity_id, kind in candidates[:limit]:
        row = store.entity_summary(entity_id)
        row["match_kind"] = kind
        results.append(row)
    return {
        "status": "suggestions" if results else "no_match",
        "normalized_query": normalized_query,
        "results": results,
        "total": len(candidates),
        "knowledge_boundary": (
            "Suggestions are lexical retrieval results, not identity confirmation."
            if results
            else "no_recorded_entity means only that the current five-card bundle has no match."
        ),
    }


def query_relations(
    store: EntityStore,
    *,
    entity_id: str,
    predicates: list[str] | None,
    direction: str,
    neighbor_types: list[str] | None,
    polarity: str,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if entity_id not in store.entities:
        raise ValueError(f"unknown entity_id: {entity_id}")
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("direction must be incoming, outgoing or both")
    if polarity not in {"supports", "opposes", "both"}:
        raise ValueError("polarity must be supports, opposes or both")
    predicate_filter = set(predicates or [])
    unknown_predicates = sorted(predicate_filter - set(store.predicates))
    if unknown_predicates:
        raise ValueError(f"unknown predicates: {', '.join(unknown_predicates)}")
    type_filter = set(neighbor_types or [])
    known_types = store.entity_types()
    unknown_types = sorted(type_filter - known_types)
    if unknown_types:
        raise ValueError(f"unknown neighbor_types: {', '.join(unknown_types)}")
    rows: list[dict[str, Any]] = []
    for assertion in store.active_assertions(entity_id, direction=direction):
        if predicate_filter and assertion["predicate"] not in predicate_filter:
            continue
        if polarity != "both" and assertion["polarity"] != polarity:
            continue
        if assertion["subject_id"] == entity_id:
            edge_direction = "outgoing"
            neighbor_id = assertion["object_id"]
        else:
            edge_direction = "incoming"
            neighbor_id = assertion["subject_id"]
        if direction != "both" and edge_direction != direction:
            continue
        if type_filter and store.entities[neighbor_id]["entity_type"] not in type_filter:
            continue
        rows.append(
            {
                "assertion_id": assertion["assertion_id"],
                "direction": edge_direction,
                "predicate": assertion["predicate"],
                "subject": store.entity_summary(assertion["subject_id"]),
                "object": store.entity_summary(assertion["object_id"]),
                "polarity": assertion["polarity"],
                "epistemic_kind": assertion["epistemic_kind"],
                "evidence_basis": assertion["evidence_basis"],
                "attribution": assertion["attribution"],
                "qualifiers": dict(assertion.get("qualifiers") or {}),
                "evidence_ids": list(assertion.get("evidence_ids") or []),
            }
        )
    rows.sort(key=lambda row: (row["predicate"], row["direction"], row["assertion_id"]))
    criteria = {
        "entity_id": entity_id,
        "predicates": sorted(predicate_filter),
        "direction": direction,
        "neighbor_types": sorted(type_filter),
        "polarity": polarity,
    }
    page, next_cursor, total = _page(
        rows,
        kind="rel",
        criteria=criteria,
        cursor=cursor,
        limit=limit,
    )
    return {
        "status": "matched" if total else "no_recorded_relation",
        "entity": store.entity_summary(entity_id),
        "relations": page,
        "total": total,
        "next_cursor": next_cursor,
        "evidence_required": True,
        "retrieval_contract": {
            "backend": "ontology_assertion_graph",
            "support_state": "explicit_polarity",
            "attribution_state": "explicit",
            "missing_relation_means": "unknown_within_current_bundle",
            "ranking_semantics": "retrieval_order_not_chemical_fitness",
        },
        "knowledge_boundary": (
            "Call enzyme_entity_read_evidence before citing a relation as a scientific fact."
            if total
            else "no_recorded_relation describes this bundle only and is not evidence of biological absence."
        ),
    }


def read_evidence(
    store: EntityStore,
    *,
    assertion_ids: list[str],
    evidence_ids: list[str],
    wiki_refs: list[str],
) -> dict[str, Any]:
    if not assertion_ids and not evidence_ids and not wiki_refs:
        raise ValueError("provide at least one assertion_id, evidence_id or wiki_ref")
    missing_assertions = sorted(item for item in set(assertion_ids) if item not in store.assertions)
    missing_evidence = sorted(item for item in set(evidence_ids) if item not in store.evidence)
    missing_wiki_refs = sorted(item for item in set(wiki_refs) if item not in store.evidence_by_wiki_ref)
    if missing_assertions:
        raise ValueError(f"unknown assertion_ids: {', '.join(missing_assertions)}")
    if missing_evidence:
        raise ValueError(f"unknown evidence_ids: {', '.join(missing_evidence)}")
    if missing_wiki_refs:
        raise ValueError(f"unknown wiki_refs: {', '.join(missing_wiki_refs)}")

    selected_evidence = set(evidence_ids)
    selected_evidence.update(store.evidence_by_wiki_ref[ref] for ref in wiki_refs)
    assertions = []
    for assertion_id in dict.fromkeys(assertion_ids):
        row = store.assertions[assertion_id]
        selected_evidence.update(row.get("evidence_ids") or [])
        assertions.append(
            {
                "assertion_id": row["assertion_id"],
                "subject": store.entity_summary(row["subject_id"]),
                "predicate": row["predicate"],
                "object": store.entity_summary(row["object_id"]),
                "polarity": row["polarity"],
                "epistemic_kind": row["epistemic_kind"],
                "evidence_basis": row["evidence_basis"],
                "attribution": row["attribution"],
                "review_status": row["review_status"],
                "scope": row.get("scope", "public"),
                "conflict_state": row.get("conflict_state", "none"),
                "conflicting_assertion_ids": row.get("conflicting_assertion_ids", []),
                "qualifiers": dict(row.get("qualifiers") or {}),
                "evidence_ids": list(row.get("evidence_ids") or []),
            }
        )

    if len(selected_evidence) > 12:
        raise ValueError(
            "evidence request expands beyond 12 passages; read fewer assertions or evidence IDs"
        )

    evidence_rows = []
    for evidence_id in sorted(selected_evidence):
        row = store.evidence[evidence_id]
        evidence_rows.append(
            {
                "evidence_id": row["evidence_id"],
                "wiki_ref": row["wiki_ref"],
                "card_id": row["card_id"],
                "card_version": row["card_version"],
                "card_sha256": row["card_sha256"],
                "heading_path": list(row["heading_path"]),
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "quote": row["quote"],
                "quote_sha256": row["quote_sha256"],
                "source_citations": list(row["source_citations"]),
                "source_scope": row["source_scope"],
                "fragment_version": row["fragment_version"],
                "snapshot_path": row["snapshot_path"],
                "locator": row.get("locator"),
            }
        )
    return {
        "assertions": assertions,
        "evidence": evidence_rows,
        "citation_ready": True,
        "retrieval_contract": {
            "backend": "ontology_assertion_evidence",
            "citation_locator": "versioned_quote_with_line_range_and_hash",
            "supports_opposes_unknown_preserved": True,
            "inference_is_not_fact": True,
        },
        "knowledge_boundary": "Quotes are local source slices; attribution and qualifiers remain part of the claim.",
    }
