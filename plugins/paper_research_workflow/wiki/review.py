from __future__ import annotations

import hashlib
import json
from typing import Any


PAGE_REVIEW_FIELDS = (
    "page_type",
    "title",
    "summary",
    "content",
    "status",
    "aliases",
    "source_doc_ids",
    "evidence_ids",
)

CLAIM_COMPARE_FIELDS = (
    "statement",
    "scope",
    "evidence_ids",
    "evidence_links",
    "support_status",
    "confidence",
    "origin",
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _claim_key(claim: dict[str, Any]) -> str:
    claim_id = str(claim.get("claim_id") or "").strip()
    if claim_id:
        return claim_id
    identity = {
        "statement": str(claim.get("statement") or "").strip(),
        "evidence_ids": list(claim.get("evidence_ids") or []),
    }
    return "anonymous-" + hashlib.sha256(_stable_json(identity).encode("utf-8")).hexdigest()[:24]


def _claim_semantic(claim: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(claim or {})
    return {field: source.get(field) for field in CLAIM_COMPARE_FIELDS}


def _change_id(operation_index: int, kind: str, claim_key: str) -> str:
    raw = f"{operation_index}:{kind}:{claim_key}"
    return "change-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_review_model(operations: list[dict[str, Any]]) -> dict[str, Any]:
    review_operations: list[dict[str, Any]] = []
    for operation_index, operation in enumerate(operations):
        op_type = str(operation.get("type") or "")
        if op_type != "upsert_page":
            review_operations.append(
                {
                    "operation_index": operation_index,
                    "type": op_type,
                    "page_id": str(operation.get("page_id") or ""),
                    "title": str(operation.get("page_id") or op_type),
                    "is_new": False,
                    "page_fields_changed": True,
                    "page_field_changes": [],
                    "claim_changes": [],
                }
            )
            continue

        after_page = dict(operation.get("page") or {})
        before_page = dict(operation.get("before") or {})
        is_new = not bool(before_page)
        page_field_changes = [
            {
                "field": field,
                "before": before_page.get(field),
                "after": after_page.get(field),
            }
            for field in PAGE_REVIEW_FIELDS
            if is_new or before_page.get(field) != after_page.get(field)
        ]

        before_claims = {
            _claim_key(dict(claim)): dict(claim)
            for claim in before_page.get("claims") or []
            if isinstance(claim, dict)
        }
        after_claims = {
            _claim_key(dict(claim)): dict(claim)
            for claim in after_page.get("claims") or []
            if isinstance(claim, dict)
        }
        claim_changes: list[dict[str, Any]] = []
        ordered_keys = list(after_claims)
        ordered_keys.extend(key for key in before_claims if key not in after_claims)
        for key in ordered_keys:
            before_claim = before_claims.get(key)
            after_claim = after_claims.get(key)
            if before_claim is None:
                kind = "added"
            elif after_claim is None:
                kind = "removed"
            elif _claim_semantic(before_claim) != _claim_semantic(after_claim):
                kind = "modified"
            else:
                continue
            claim_changes.append(
                {
                    "change_id": _change_id(operation_index, kind, key),
                    "claim_key": key,
                    "kind": kind,
                    "before": before_claim,
                    "after": after_claim,
                }
            )

        review_operations.append(
            {
                "operation_index": operation_index,
                "type": op_type,
                "page_id": str(after_page.get("page_id") or before_page.get("page_id") or ""),
                "title": str(after_page.get("title") or before_page.get("title") or "Wiki page"),
                "is_new": is_new,
                "page_fields_changed": bool(page_field_changes),
                "page_field_changes": page_field_changes,
                "claim_changes": claim_changes,
            }
        )
    return {
        "operations": review_operations,
        "change_count": sum(len(item["claim_changes"]) for item in review_operations),
    }


def default_review_selection(review_model: dict[str, Any]) -> dict[str, Any]:
    return {
        "operations": [
            {
                "operation_index": int(item["operation_index"]),
                "include_operation": True,
                "include_page_fields": bool(item.get("page_fields_changed") or item.get("is_new")),
                "accepted_claim_change_ids": [
                    str(change["change_id"]) for change in item.get("claim_changes") or []
                ],
            }
            for item in review_model.get("operations") or []
        ]
    }


def normalize_review_selection(
    review_model: dict[str, Any],
    selection: dict[str, Any] | None,
) -> dict[str, Any]:
    if selection is None:
        return default_review_selection(review_model)
    raw_operations = selection.get("operations") if isinstance(selection, dict) else None
    if not isinstance(raw_operations, list):
        raise ValueError("review selection requires an operations list")
    requested: dict[int, dict[str, Any]] = {}
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise ValueError("review selection operation must be an object")
        index = int(raw.get("operation_index", -1))
        if index in requested:
            raise ValueError(f"duplicate selection for operation {index}")
        requested[index] = raw

    normalized: list[dict[str, Any]] = []
    known_indices: set[int] = set()
    for item in review_model.get("operations") or []:
        index = int(item["operation_index"])
        known_indices.add(index)
        raw = requested.get(index, {})
        allowed_changes = {
            str(change["change_id"]) for change in item.get("claim_changes") or []
        }
        accepted = {
            str(change_id)
            for change_id in raw.get("accepted_claim_change_ids") or []
            if str(change_id)
        }
        unknown = accepted - allowed_changes
        if unknown:
            raise ValueError(
                f"selection contains unknown claim changes for operation {index}: {sorted(unknown)}"
            )
        include_operation = bool(raw.get("include_operation", False))
        include_page_fields = bool(raw.get("include_page_fields", False))
        if str(item.get("type") or "") != "upsert_page":
            include_page_fields = False
            accepted = set()
        if bool(item.get("is_new")) and not include_operation:
            include_page_fields = False
            accepted = set()
        normalized.append(
            {
                "operation_index": index,
                "include_operation": include_operation,
                "include_page_fields": include_page_fields,
                "accepted_claim_change_ids": sorted(accepted),
            }
        )
    unknown_indices = set(requested) - known_indices
    if unknown_indices:
        raise ValueError(f"selection contains unknown operations: {sorted(unknown_indices)}")
    return {"operations": normalized}


def derive_selected_operations(
    operations: list[dict[str, Any]],
    selection: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    review_model = build_review_model(operations)
    normalized = normalize_review_selection(review_model, selection)
    selected_by_index = {
        int(item["operation_index"]): item for item in normalized["operations"]
    }
    review_by_index = {
        int(item["operation_index"]): item for item in review_model["operations"]
    }
    derived: list[dict[str, Any]] = []

    for operation_index, operation in enumerate(operations):
        selected = selected_by_index[operation_index]
        op_type = str(operation.get("type") or "")
        if op_type != "upsert_page":
            if selected["include_operation"]:
                derived.append(dict(operation))
            continue

        review = review_by_index[operation_index]
        before_page = dict(operation.get("before") or {})
        after_page = dict(operation.get("page") or {})
        is_new = bool(review.get("is_new"))
        accepted = set(selected["accepted_claim_change_ids"])
        if is_new and not selected["include_operation"]:
            continue
        if not is_new and not selected["include_page_fields"] and not accepted:
            continue

        if is_new or selected["include_page_fields"]:
            target_page = dict(after_page)
        else:
            target_page = dict(before_page)
            target_page["page_id"] = str(
                after_page.get("page_id") or before_page.get("page_id") or ""
            )

        before_claims = {
            _claim_key(dict(claim)): dict(claim)
            for claim in before_page.get("claims") or []
            if isinstance(claim, dict)
        }
        result_claims = dict(before_claims)
        changes = {str(item["change_id"]): item for item in review.get("claim_changes") or []}
        for change_id in accepted:
            change = changes[change_id]
            key = str(change["claim_key"])
            if change["kind"] == "removed":
                result_claims.pop(key, None)
            else:
                result_claims[key] = dict(change.get("after") or {})

        after_order = [
            _claim_key(dict(claim))
            for claim in after_page.get("claims") or []
            if isinstance(claim, dict)
        ]
        ordered_keys = [key for key in after_order if key in result_claims]
        ordered_keys.extend(key for key in result_claims if key not in ordered_keys)
        target_page["claims"] = [result_claims[key] for key in ordered_keys]

        claim_evidence = {
            str(evidence_id)
            for claim in target_page["claims"]
            for evidence_id in claim.get("evidence_ids") or []
            if str(evidence_id)
        }
        before_claim_evidence = {
            str(evidence_id)
            for claim in before_page.get("claims") or []
            if isinstance(claim, dict)
            for evidence_id in claim.get("evidence_ids") or []
            if str(evidence_id)
        }
        after_claim_evidence = {
            str(evidence_id)
            for claim in after_page.get("claims") or []
            if isinstance(claim, dict)
            for evidence_id in claim.get("evidence_ids") or []
            if str(evidence_id)
        }
        if is_new or selected["include_page_fields"]:
            page_level_evidence = set(after_page.get("evidence_ids") or []) - after_claim_evidence
        else:
            page_level_evidence = set(before_page.get("evidence_ids") or []) - before_claim_evidence
        target_page["evidence_ids"] = sorted(page_level_evidence | claim_evidence)
        derived.append({"type": "upsert_page", "page": target_page})

    return derived, normalized, review_model


def approval_hash(
    *,
    proposal_hash: str,
    selection: dict[str, Any],
    applied_operations: list[dict[str, Any]],
) -> str:
    payload = {
        "proposal_hash": str(proposal_hash),
        "selection": selection,
        "applied_operations": applied_operations,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
