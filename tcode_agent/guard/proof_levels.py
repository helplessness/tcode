"""Proof-level calculation for Outcome v2 adjudication.

The contract declares the minimum proof level it requires. The arbiter derives the
achieved level exclusively from passed verifier results; a caller cannot raise the
achieved level by merely writing a larger value into the contract.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..task.outcome_contract import OutcomeContract


PROOF_LEVELS = ("P0", "P1", "P2", "P3", "P4")
_PROOF_LEVEL_RANK = {level: index for index, level in enumerate(PROOF_LEVELS)}


def normalize_proof_level(value: Any, *, default: str = "P0") -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in _PROOF_LEVEL_RANK else default


def proof_level_satisfies(achieved: Any, required: Any) -> bool:
    """Return whether a verified level meets a non-observational requirement.

    P0 intentionally never satisfies a verified-completion gate. It represents an
    observational/legacy trace, not proof that the user's goal was achieved.
    """

    achieved_level = normalize_proof_level(achieved)
    required_level = normalize_proof_level(required)
    if required_level == "P0":
        return False
    return _PROOF_LEVEL_RANK[achieved_level] >= _PROOF_LEVEL_RANK[required_level]


def calculate_achieved_proof_level(
    contract: OutcomeContract,
    criterion_results: list[dict[str, Any]],
    *,
    input_digests: Mapping[str, str] | None = None,
) -> str:
    """Derive the weakest required-criterion level from current admitted results."""

    if contract.compatibility_mode != "native_v2" or not input_digests:
        return "P0"

    result_by_id = {
        str(item.get("criterion_id") or ""): item
        for item in criterion_results
        if isinstance(item, dict)
    }
    required_criteria = [item for item in contract.success_criteria if item.required]
    if not required_criteria or any(
        str(result_by_id.get(item.criterion_id, {}).get("status") or "uncertain")
        != "pass"
        for item in required_criteria
    ):
        return "P0"

    # Import lazily to keep registry definitions dependent on this module without a
    # module-import cycle.
    from .verifier_registry import VerifierRegistry

    criterion_levels: list[str] = []
    for criterion in required_criteria:
        result = result_by_id.get(criterion.criterion_id, {})
        admitted_levels: list[str] = []
        for verifier_result in result.get("verifier_results") or []:
            if not isinstance(verifier_result, Mapping):
                continue
            if VerifierRegistry.is_admitted_result(
                verifier_result,
                contract_id=contract.contract_id,
                contract_version=contract.version,
                criterion_id=criterion.criterion_id,
                input_digest=str(input_digests.get(criterion.criterion_id) or ""),
            ):
                admitted_levels.append(
                    normalize_proof_level(
                        verifier_result.get("admitted_proof_level"),
                        default="P0",
                    )
                )
        if not admitted_levels:
            return "P0"
        criterion_levels.append(
            max(admitted_levels, key=lambda level: _PROOF_LEVEL_RANK[level])
        )

    if not criterion_levels:
        return "P0"
    return min(criterion_levels, key=lambda level: _PROOF_LEVEL_RANK[level])
