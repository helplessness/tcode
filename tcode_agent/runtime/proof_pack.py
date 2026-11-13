"""Proof Pack v1 projection built from Outcome, Evidence and Verification facts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..guard.deterministic_verifiers import verifier_input_digests
from ..guard.proof_levels import (
    calculate_achieved_proof_level,
    proof_level_satisfies,
)
from ..task.outcome_contract import OutcomeContract
from .evidence_ledger import EvidenceLedger
from .telemetry import redact_telemetry_value


def proof_pack_enabled() -> bool:
    raw = str(os.environ.get("TCODE_PROOF_PACK_ENABLED", "0") or "0").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(encoded).hexdigest()


def _goal_status(verdict: str, validator: str) -> str:
    if validator == "disabled":
        return "UNVERIFIED"
    return {
        "pass": "SATISFIED",
        "repair": "ACTIVE",
        "partial": "BLOCKED",
        "blocked": "BLOCKED",
    }.get(str(verdict or "").strip().lower(), "BLOCKED")


@dataclass(frozen=True)
class ProofPack:
    """A JSON/Markdown dual projection over one immutable dictionary."""

    projection: dict[str, Any]

    @classmethod
    def build(
        cls,
        *,
        contract: OutcomeContract,
        validation: Any,
        ledger: EvidenceLedger,
        candidate: str,
        workspace_root: str | None = None,
    ) -> "ProofPack":
        validation_dict = (
            validation.to_dict()
            if callable(getattr(validation, "to_dict", None))
            else dict(validation or {})
            if isinstance(validation, dict)
            else {}
        )
        criterion_by_id = {
            item.criterion_id: item for item in contract.success_criteria
        }
        criterion_results: list[dict[str, Any]] = []
        for raw in validation_dict.get("criterion_results") or []:
            if not isinstance(raw, dict):
                continue
            result = dict(raw)
            criterion = criterion_by_id.get(str(result.get("criterion_id") or ""))
            if criterion is not None:
                result.setdefault("description", criterion.description)
                result.setdefault("required", criterion.required)
            criterion_results.append(result)

        candidate_text = str(candidate or "")
        candidate_digest = hashlib.sha256(
            candidate_text.encode("utf-8", errors="ignore")
        ).hexdigest()
        evidence_entries = [
            {
                "tool_name": entry.tool_name,
                "call_id": entry.call_id,
                "status": entry.status,
                "content_hash": entry.content_hash,
                "output_path": entry.output_path,
                "truncated": entry.truncated,
                "references": entry.references,
            }
            for entry in ledger.entries[-20:]
        ]
        validator = str(validation_dict.get("validator") or "unknown")
        verdict = str(validation_dict.get("verdict") or "partial").strip().lower()
        required_proof_level = str(
            validation_dict.get("required_proof_level")
            or contract.required_proof_level
        ).strip().upper()
        achieved_proof_level = calculate_achieved_proof_level(
            contract,
            criterion_results,
            input_digests=verifier_input_digests(
                candidate_text,
                contract,
                ledger,
                workspace_root=workspace_root,
            ),
        )
        goal_status = str(validation_dict.get("goal_status") or "").strip().upper()
        goal_status = goal_status or _goal_status(verdict, validator)
        if goal_status == "SATISFIED" and not proof_level_satisfies(
            achieved_proof_level,
            required_proof_level,
        ):
            goal_status = "UNVERIFIED"
        verifier_snapshots = {
            (
                str(verifier_result.get("verifier_id") or ""),
                str(verifier_result.get("verifier_version") or ""),
                str(verifier_result.get("definition_digest") or ""),
                str(verifier_result.get("plugin_instance_id") or ""),
            ): {
                "verifier_id": str(verifier_result.get("verifier_id") or ""),
                "verifier_version": str(
                    verifier_result.get("verifier_version") or ""
                ),
                "provider": str(verifier_result.get("provider") or ""),
                "source": str(verifier_result.get("source") or ""),
                "plugin_instance_id": str(
                    verifier_result.get("plugin_instance_id") or ""
                ),
                "definition_digest": str(
                    verifier_result.get("definition_digest") or ""
                ),
                "contract_id": str(verifier_result.get("contract_id") or ""),
                "contract_version": int(
                    verifier_result.get("contract_version") or 0
                ),
                "criterion_id": str(verifier_result.get("criterion_id") or ""),
                "input_digest": str(verifier_result.get("input_digest") or ""),
                "admitted_proof_level": str(
                    verifier_result.get("admitted_proof_level") or "P0"
                ),
                "admission_receipt": str(
                    verifier_result.get("admission_receipt") or ""
                ),
            }
            for criterion_result in criterion_results
            for verifier_result in (criterion_result.get("verifier_results") or [])
            if isinstance(verifier_result, dict) and verifier_result.get("verifier_id")
        }
        core = {
            "schema_version": "tcode.proof-pack/v1",
            "contract": {
                "schema_version": contract.schema_version,
                "contract_id": contract.contract_id,
                "contract_version": contract.version,
                "contract_digest": contract.digest(),
                "goal_id": contract.goal_id,
                "title": contract.title,
                "risk_level": contract.risk_level,
                "proof_level": contract.proof_level,
                "required_proof_level": required_proof_level,
                "compatibility_mode": contract.compatibility_mode,
            },
            "outcome": {
                "status": goal_status,
                "verdict": verdict,
                "validator": validator,
                "achieved_proof_level": achieved_proof_level,
                "criterion_results": criterion_results,
                "failed_criteria": list(validation_dict.get("failed_criteria") or []),
                "missing_deliverables": list(
                    validation_dict.get("missing_deliverables") or []
                ),
                "unsupported_claims": list(
                    validation_dict.get("unsupported_claims") or []
                ),
                "warnings": list(validation_dict.get("warnings") or []),
            },
            "deliverables": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "required": item.required,
                    "present": bool(candidate_text.strip())
                    if item.name == "final_response"
                    else None,
                    "content_size": len(candidate_text)
                    if item.name == "final_response"
                    else None,
                    "content_digest": candidate_digest
                    if item.name == "final_response"
                    else None,
                }
                for item in contract.deliverables
            ],
            "evidence": {
                "entry_count": len(ledger.entries),
                "failed_count": sum(
                    1 for entry in ledger.entries if entry.status in {"error", "failed"}
                ),
                "truncated_count": sum(1 for entry in ledger.entries if entry.truncated),
                "entries": evidence_entries,
            },
            "reproducibility": {
                "candidate_digest": candidate_digest,
                "contract_digest": contract.digest(),
                "verifier_versions": sorted(
                    {
                        f"{snapshot['verifier_id']}@{snapshot['verifier_version']}"
                        for snapshot in verifier_snapshots.values()
                    }
                ),
                "verifier_definitions": sorted(
                    verifier_snapshots.values(),
                    key=lambda item: (
                        item["verifier_id"],
                        item["verifier_version"],
                        item["definition_digest"],
                    ),
                ),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        safe_core = redact_telemetry_value(
            core,
            text_limit=2_000,
            list_limit=100,
            dict_limit=100,
            depth_limit=8,
        )
        assert isinstance(safe_core, dict)
        identity = dict(safe_core)
        identity.pop("generated_at", None)
        safe_core["proof_pack_id"] = f"proof_{_digest(identity)[:20]}"
        return cls(projection=safe_core)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.projection, ensure_ascii=False))

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.projection, ensure_ascii=False, indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        data = self.projection
        contract = dict(data.get("contract") or {})
        outcome = dict(data.get("outcome") or {})
        evidence = dict(data.get("evidence") or {})
        lines = [
            "# Proof Pack",
            "",
            f"- Proof Pack：`{data.get('proof_pack_id', '')}`",
            f"- Goal：`{contract.get('goal_id', '')}`",
            f"- Outcome：`{outcome.get('status', 'UNVERIFIED')}`",
            "- Required Proof Level：`"
            f"{contract.get('required_proof_level') or contract.get('proof_level', 'P0')}`",
            f"- Achieved Proof Level：`{outcome.get('achieved_proof_level', 'P0')}`",
            f"- Contract：`{contract.get('contract_id', '')}` v{contract.get('contract_version', 1)}",
            "",
            "## Success Criteria",
            "",
        ]
        criteria = list(outcome.get("criterion_results") or [])
        if not criteria:
            lines.append("- 没有逐项验证结果；该结果尚未达到可验证完成级别。")
        for item in criteria:
            required = "required" if item.get("required", True) else "optional"
            lines.append(
                f"- `{item.get('status', 'uncertain')}` ({required}) "
                f"{item.get('description') or item.get('criterion_id') or 'criterion'}"
            )
        lines.extend(
            [
                "",
                "## Evidence Summary",
                "",
                f"- Entries：{evidence.get('entry_count', 0)}",
                f"- Failed：{evidence.get('failed_count', 0)}",
                f"- Truncated：{evidence.get('truncated_count', 0)}",
            ]
        )
        failed = list(outcome.get("failed_criteria") or [])
        missing = list(outcome.get("missing_deliverables") or [])
        unsupported = list(outcome.get("unsupported_claims") or [])
        warnings = list(outcome.get("warnings") or [])
        if failed or missing or unsupported or warnings:
            lines.extend(["", "## Gaps and Risks", ""])
            lines.extend(f"- Failed：{item}" for item in failed)
            lines.extend(f"- Missing：{item}" for item in missing)
            lines.extend(f"- Unsupported：{item}" for item in unsupported)
            lines.extend(f"- Warning：{item}" for item in warnings)
        lines.extend(
            [
                "",
                "## Reproducibility",
                "",
                f"- Contract digest：`{contract.get('contract_digest', '')}`",
                "- Candidate digest：`"
                f"{dict(data.get('reproducibility') or {}).get('candidate_digest', '')}`",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"
