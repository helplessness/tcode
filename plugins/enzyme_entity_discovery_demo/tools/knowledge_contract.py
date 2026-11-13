"""Backend-neutral evidence envelopes for mechanism-guided discovery.

This is deliberately an adapter contract, not a claim that the Wiki and
ontology stores have the same native representation or retrieval behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CONTRACT_VERSION = "mechanism-evidence/1.0"


def _text(value: Any) -> str:
    return str(value or "").strip()


def ontology_evidence_envelope(
    assertion: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Adapt one read ontology assertion and its quoted evidence."""
    evidence_ids = list(assertion.get("evidence_ids") or [])
    evidence_id = _text(evidence.get("evidence_id"))
    if not evidence_id or evidence_id not in evidence_ids:
        raise ValueError("evidence must be explicitly linked to the ontology assertion")
    polarity = _text(assertion.get("polarity"))
    if polarity not in {"supports", "opposes"}:
        raise ValueError("ontology assertion polarity must be supports or opposes")
    quote = _text(evidence.get("quote"))
    quote_hash = _text(evidence.get("quote_sha256"))
    if not quote or not quote_hash:
        raise ValueError("ontology evidence requires a quote and quote_sha256")
    return {
        "contract_version": CONTRACT_VERSION,
        "backend": "ontology_assertion_graph",
        "claim_ref": _text(assertion.get("assertion_id")),
        "evidence_ref": evidence_id,
        "support_state": polarity,
        "attribution": _text(assertion.get("attribution")) or "unresolved",
        "statement": quote,
        "source_locator": {
            "source_path": _text(evidence.get("snapshot_path")),
            "version": _text(evidence.get("card_version")),
            "line_start": evidence.get("line_start"),
            "line_end": evidence.get("line_end"),
            "content_sha256": quote_hash,
        },
        "source_citations": list(evidence.get("source_citations") or []),
        "missing_means": "unknown_within_current_bundle",
        "ranking_semantics": "evidence_state_only_not_chemical_fitness",
    }


def wiki_evidence_envelope(
    claim: Mapping[str, Any], *, knowledge_version: str
) -> dict[str, Any]:
    """Adapt one production Wiki claim without inventing graph polarity."""
    claim_ref = _text(claim.get("ref"))
    statement = _text(claim.get("statement"))
    if not claim_ref or not statement:
        raise ValueError("Wiki claim requires ref and statement")
    locator = dict(claim.get("source_locator") or {})
    sources = list(claim.get("sources") or [])
    return {
        "contract_version": CONTRACT_VERSION,
        "backend": "markdown_wiki",
        "claim_ref": claim_ref,
        "evidence_ref": claim_ref,
        # Wiki retrieval reports a passage; it does not expose the ontology's
        # explicit supports/opposes edge polarity.
        "support_state": "reported_unspecified_polarity",
        "attribution": _text(claim.get("attribution")) or "unresolved",
        "statement": statement,
        "source_locator": {
            "source_path": _text(locator.get("source_path")),
            "version": _text(knowledge_version),
            "line_start": locator.get("line_start"),
            "line_end": locator.get("line_end"),
            "content_sha256": _text(locator.get("content_sha256")),
        },
        "source_citations": sources,
        "missing_means": "unknown_within_current_wiki",
        "ranking_semantics": "retrieval_order_not_chemical_fitness",
    }


def missing_evidence_envelope(*, backend: str, query: str) -> dict[str, Any]:
    """Represent a backend miss as unknown, never as biological absence."""
    if backend not in {"ontology_assertion_graph", "markdown_wiki"}:
        raise ValueError("unsupported knowledge backend")
    return {
        "contract_version": CONTRACT_VERSION,
        "backend": backend,
        "query": _text(query),
        "support_state": "unknown",
        "missing_means": (
            "unknown_within_current_bundle"
            if backend == "ontology_assertion_graph"
            else "unknown_within_current_wiki"
        ),
        "ranking_semantics": (
            "evidence_state_only_not_chemical_fitness"
            if backend == "ontology_assertion_graph"
            else "retrieval_order_not_chemical_fitness"
        ),
    }
