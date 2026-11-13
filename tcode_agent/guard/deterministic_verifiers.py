"""Deterministic criterion verifiers used by Outcome v2.

These verifiers only consume bounded runtime facts.  They never execute commands, ask a
model to judge its own answer, or grant permission.  Domain plugins can later contribute
additional verifiers through the native Plugin SDK without changing the core arbiter.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..runtime.evidence_ledger import EvidenceLedger
from ..task.outcome_contract import OutcomeContract, OutcomeSuccessCriterion
from .verifier_registry import (
    VERIFIER_INPUT_SCHEMA,
    VERIFIER_OUTPUT_SCHEMA,
    VerifierDefinition,
    VerifierRegistrationError,
    VerifierRegistry,
    verifier_input_digest,
)


@dataclass
class CriterionVerificationResult:
    result_id: str
    criterion_id: str
    status: str
    verifier_id: str = "tcode.core.deterministic"
    verifier_version: str = "1.0.0"
    observations: list[str] = field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    reason_code: str = ""
    retryable: bool = False
    verifier_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def deterministic_verifier_enabled() -> bool:
    raw = str(
        os.environ.get("TCODE_DETERMINISTIC_VERIFIER_ENABLED", "0") or "0"
    ).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _result_id(criterion_id: str, checks: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        {"criterion_id": criterion_id, "checks": checks},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return f"verification_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _safe_artifact_path(raw_path: str, workspace_root: Optional[str]) -> tuple[Optional[Path], str]:
    path = Path(str(raw_path or "").strip()).expanduser()
    root = Path(workspace_root or os.getcwd()).expanduser().resolve()
    candidate = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, "artifact_outside_workspace"
    return candidate, ""


def _artifact_hash_max_bytes() -> int:
    try:
        configured = int(
            os.environ.get("TCODE_OUTCOME_ARTIFACT_HASH_MAX_BYTES", 512 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        configured = 512 * 1024 * 1024
    return max(1024, min(configured, 4 * 1024 * 1024 * 1024))


def _artifact_checks(
    criterion: OutcomeSuccessCriterion,
    *,
    workspace_root: Optional[str],
    check_integrity: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for expectation in criterion.expected_artifacts:
        raw_path = str(expectation.get("path") or "").strip()
        required = bool(expectation.get("required", True))
        candidate, path_error = _safe_artifact_path(raw_path, workspace_root)
        if path_error:
            checks.append(
                {
                    "status": "error",
                    "reason_code": path_error,
                    "path": raw_path,
                    "required": required,
                }
            )
            continue
        assert candidate is not None
        try:
            exists = candidate.is_file()
        except OSError as exc:
            checks.append(
                {
                    "status": "error",
                    "reason_code": "artifact_stat_failed",
                    "path": str(candidate),
                    "error_type": type(exc).__name__,
                }
            )
            continue
        artifact_ref: dict[str, Any] = {
            "path": str(candidate),
            "exists": exists,
            "required": required,
        }
        if exists:
            try:
                stat = candidate.stat()
            except OSError as exc:
                checks.append(
                    {
                        "status": "error",
                        "reason_code": "artifact_stat_failed",
                        "path": str(candidate),
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            artifact_ref.update(
                {
                    "size": stat.st_size,
                    "media_type": mimetypes.guess_type(candidate.name)[0]
                    or "application/octet-stream",
                }
            )
        refs.append(artifact_ref)
        if not exists:
            checks.append(
                {
                    "status": "fail" if required else "pass",
                    "reason_code": "artifact_missing" if required else "optional_artifact_missing",
                    "path": str(candidate),
                    "required": required,
                }
            )
            continue
        if not check_integrity:
            checks.append(
                {
                    "status": "pass",
                    "reason_code": "artifact_exists",
                    "path": str(candidate),
                    "required": required,
                }
            )
            continue

        expected_size = expectation.get("size")
        expected_digest = str(
            expectation.get("sha256") or expectation.get("content_digest") or ""
        ).strip().lower()
        actual_size = int(candidate.stat().st_size)
        try:
            normalized_expected_size = (
                None if expected_size in (None, "") else int(expected_size)
            )
        except (TypeError, ValueError):
            checks.append(
                {
                    "status": "error",
                    "reason_code": "artifact_expected_size_invalid",
                    "path": str(candidate),
                    "expected_size": expected_size,
                }
            )
            continue
        size_valid = (
            normalized_expected_size is None
            or actual_size == normalized_expected_size
        )
        actual_digest = ""
        digest_valid = True
        if expected_digest:
            if actual_size > _artifact_hash_max_bytes():
                checks.append(
                    {
                        "status": "error",
                        "reason_code": "artifact_hash_size_limit_exceeded",
                        "path": str(candidate),
                        "actual_size": actual_size,
                    }
                )
                continue
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                checks.append(
                    {
                        "status": "error",
                        "reason_code": "artifact_read_failed",
                        "path": str(candidate),
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            actual_digest = digest.hexdigest()
            digest_valid = actual_digest == expected_digest
            artifact_ref["sha256"] = actual_digest
        checks.append(
            {
                "status": "pass" if size_valid and digest_valid else "fail",
                "reason_code": (
                    "artifact_integrity_verified"
                    if size_valid and digest_valid
                    else "artifact_integrity_mismatch"
                ),
                "path": str(candidate),
                "required": required,
                "actual_size": actual_size,
                "expected_size": expected_size,
                "actual_sha256": actual_digest,
                "expected_sha256": expected_digest,
            }
        )
    return checks, refs


def _artifact_changed_checks(
    criterion: OutcomeSuccessCriterion,
    *,
    workspace_root: Optional[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare requested files with the host snapshot captured before execution."""

    raw_baselines = criterion.parameters.get("artifact_baselines", [])
    baselines = {
        str(item.get("path") or "").strip(): dict(item)
        for item in raw_baselines
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    checks: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for expectation in criterion.expected_artifacts:
        raw_path = str(expectation.get("path") or "").strip()
        required = bool(expectation.get("required", True))
        baseline = baselines.get(raw_path)
        if baseline is None:
            checks.append(
                {
                    "status": "error",
                    "reason_code": "artifact_baseline_missing",
                    "path": raw_path,
                    "required": required,
                }
            )
            continue
        candidate, path_error = _safe_artifact_path(raw_path, workspace_root)
        if path_error:
            checks.append(
                {
                    "status": "error",
                    "reason_code": path_error,
                    "path": raw_path,
                    "required": required,
                }
            )
            continue
        assert candidate is not None
        try:
            exists = candidate.is_file()
        except OSError as exc:
            checks.append(
                {
                    "status": "error",
                    "reason_code": "artifact_stat_failed",
                    "path": str(candidate),
                    "error_type": type(exc).__name__,
                }
            )
            continue

        artifact_ref: dict[str, Any] = {
            "path": str(candidate),
            "exists": exists,
            "required": required,
        }
        refs.append(artifact_ref)
        if not exists:
            checks.append(
                {
                    "status": "fail" if required else "pass",
                    "reason_code": (
                        "artifact_missing" if required else "optional_artifact_missing"
                    ),
                    "path": str(candidate),
                    "required": required,
                }
            )
            continue

        try:
            stat = candidate.stat()
        except OSError as exc:
            checks.append(
                {
                    "status": "error",
                    "reason_code": "artifact_stat_failed",
                    "path": str(candidate),
                    "error_type": type(exc).__name__,
                }
            )
            continue
        artifact_ref.update({"size": stat.st_size})
        if not bool(baseline.get("existed", False)):
            checks.append(
                {
                    "status": "pass",
                    "reason_code": "artifact_created",
                    "path": str(candidate),
                }
            )
            continue

        previous_digest = str(baseline.get("sha256") or "").strip().lower()
        current_digest = ""
        if previous_digest:
            if stat.st_size > _artifact_hash_max_bytes():
                checks.append(
                    {
                        "status": "error",
                        "reason_code": "artifact_hash_size_limit_exceeded",
                        "path": str(candidate),
                        "actual_size": stat.st_size,
                    }
                )
                continue
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                checks.append(
                    {
                        "status": "error",
                        "reason_code": "artifact_read_failed",
                        "path": str(candidate),
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            current_digest = digest.hexdigest()
            changed = current_digest != previous_digest
            artifact_ref["sha256"] = current_digest
        else:
            changed = (
                int(baseline.get("size", -1)) != stat.st_size
                or int(baseline.get("mtime_ns", -1)) != stat.st_mtime_ns
            )
        checks.append(
            {
                "status": "pass" if changed else "fail",
                "reason_code": "artifact_changed" if changed else "artifact_unchanged",
                "path": str(candidate),
                "previous_sha256": previous_digest,
                "actual_sha256": current_digest,
                "previous_size": baseline.get("size"),
                "actual_size": stat.st_size,
            }
        )
    return checks, refs


def _tool_success_checks(
    criterion: OutcomeSuccessCriterion,
    ledger: EvidenceLedger,
) -> list[dict[str, Any]]:
    raw_names = criterion.parameters.get("tool_names", [])
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    names = {
        str(item).strip().lower()
        for item in raw_names
        if str(item or "").strip()
    }
    entries = [
        entry
        for entry in ledger.entries
        if entry.status != "superseded"
        if not names or str(entry.tool_name or "").strip().lower() in names
    ]
    if not entries:
        return [{"status": "fail", "reason_code": "required_tool_evidence_missing"}]
    # Only trusted pre-execution validation failures can recover. Nested scene
    # validation additionally requires the same intended scene identity.
    recovered = [
        entry for index, entry in enumerate(entries)
        if entry.status in {"error", "failed"}
        and entry.references.get("execution_skipped") is True
        and isinstance(entry.references.get("argument_validation"), dict)
        and entry.references.get("argument_validation", {}).get("accepted") is False
        and (entry.references["argument_validation"].get("validator") == "json-schema-2020-12"
             or entry.references["argument_validation"].get("validator") == "pydantic"
             and entry.tool_name == "canvas" and bool(entry.references.get("operation_key")))
        and any(later.tool_name == entry.tool_name and later.status == "completed"
                and (not entry.references.get("operation_key")
                     or later.references.get("operation_key") == entry.references["operation_key"])
                for later in entries[index + 1:])
    ]
    failures = [entry for entry in entries if entry.status in {"error", "failed"} and entry not in recovered]
    require_all = bool(criterion.parameters.get("require_all", True))
    passed = not failures if require_all else len(failures) < len(entries)
    return [
        {
            "status": "pass" if passed else "fail",
            "reason_code": "tool_execution_verified" if passed else "tool_execution_failed",
            "entry_count": len(entries),
            "failed_count": len(failures),
            "recovered_argument_count": len(recovered),
            "recovered_call_ids": [entry.call_id for entry in recovered if entry.call_id],
            "call_ids": [entry.call_id for entry in entries if entry.call_id],
        }
    ]


def _source_coverage_checks(
    criterion: OutcomeSuccessCriterion,
    ledger: EvidenceLedger,
) -> list[dict[str, Any]]:
    source_keys = {
        "doc_id",
        "record_path",
        "downloaded_pdf_paths",
        "research_grounding",
        "sources",
    }
    source_entries = [
        entry
        for entry in ledger.entries
        if source_keys.intersection(entry.references)
    ]
    try:
        minimum = max(1, int(criterion.parameters.get("min_sources", 1)))
    except Exception:
        minimum = 1
    passed = len(source_entries) >= minimum
    return [
        {
            "status": "pass" if passed else "fail",
            "reason_code": "source_coverage_verified" if passed else "source_coverage_missing",
            "source_entry_count": len(source_entries),
            "minimum": minimum,
            "call_ids": [entry.call_id for entry in source_entries if entry.call_id],
        }
    ]


def _human_approval_checks(
    criterion: OutcomeSuccessCriterion,
    ledger: EvidenceLedger,
) -> list[dict[str, Any]]:
    approvals = [
        entry
        for entry in ledger.entries
        if entry.references.get("approval_id")
        and entry.references.get("approval_status") in {"approved", "allow", True}
    ]
    return [
        {
            "status": "pass" if approvals else "uncertain",
            "reason_code": (
                "human_approval_verified" if approvals else "human_approval_missing"
            ),
            "call_ids": [entry.call_id for entry in approvals if entry.call_id],
        }
    ]


@dataclass(frozen=True, slots=True)
class _DeterministicExecutionContext:
    candidate: str
    criterion: OutcomeSuccessCriterion
    contract: OutcomeContract
    ledger: EvidenceLedger
    schema_validator: Optional[
        Callable[[str, dict[str, Any]], tuple[bool, list[str]]]
    ]
    workspace_root: Optional[str]


_P2_REASON_CODES = {
    "json_schema_verified",
    "artifact_exists",
    "artifact_integrity_verified",
    "tool_execution_verified",
    "source_coverage_verified",
    "human_approval_verified",
}


def _aggregate_core_checks(
    checks: list[dict[str, Any]],
    *,
    artifact_refs: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    if not checks:
        checks = [{"status": "uncertain", "reason_code": "verifier_no_result"}]
    statuses = {
        "uncertain" if str(item.get("status") or "") == "pending" else str(
            item.get("status") or "uncertain"
        )
        for item in checks
    }
    if "error" in statuses:
        status = "error"
    elif "fail" in statuses:
        status = "fail"
    elif "uncertain" in statuses:
        status = "uncertain"
    else:
        status = "pass"
    reason_codes = [
        str(item.get("reason_code") or "verifier_result") for item in checks
    ]
    call_ids = [
        str(call_id)
        for item in checks
        for call_id in (item.get("call_ids") or [])
        if str(call_id or "").strip()
    ]
    achieved = "P1"
    if status == "pass" and set(reason_codes).intersection(_P2_REASON_CODES):
        achieved = "P2"
    return {
        "status": status,
        "reason_code": reason_codes[0] if len(reason_codes) == 1 else "multiple_checks",
        "observations": reason_codes,
        "artifact_refs": list(
            {
                str(item.get("path") or index): item
                for index, item in enumerate(artifact_refs or [])
            }.values()
        ),
        "evidence_refs": list(dict.fromkeys(call_ids)),
        "details": {"checks": checks},
        "retryable": status in {"fail", "error"},
        "_achieved_proof_level": achieved,
    }


def _run_core_verifier(
    kind: str,
    context: _DeterministicExecutionContext,
) -> dict[str, Any]:
    candidate = context.candidate
    criterion = context.criterion
    contract = context.contract
    ledger = context.ledger
    artifact_refs: list[dict[str, Any]] = []

    if kind in {"semantic", "human_review"}:
        return _aggregate_core_checks(
            [{"status": "uncertain", "reason_code": f"{kind}_pending"}]
        )
    if kind == "final_response":
        candidate_text = str(candidate or "")
        expected = criterion.parameters.get("exact")
        if expected is not None:
            expected_text = str(expected)
            if bool(criterion.parameters.get("trim", True)):
                candidate_text = candidate_text.strip()
                expected_text = expected_text.strip()
            exact_match = candidate_text == expected_text
            return _aggregate_core_checks(
                [
                    {
                        "status": "pass" if exact_match else "fail",
                        "reason_code": (
                            "final_response_exact_match"
                            if exact_match
                            else "final_response_exact_mismatch"
                        ),
                        "expected": expected_text[:500],
                    }
                ]
            )
        present = bool(candidate_text.strip())
        return _aggregate_core_checks(
            [
                {
                    "status": "pass" if present else "fail",
                    "reason_code": (
                        "final_response_present" if present else "final_response_missing"
                    ),
                }
            ]
        )
    if kind in {"json", "json_schema"}:
        schema = criterion.parameters.get("schema")
        if not isinstance(schema, dict):
            schema = next(
                (
                    item.schema
                    for item in contract.deliverables
                    if isinstance(item.schema, dict)
                ),
                None,
            )
        try:
            json.loads(str(candidate or ""))
            valid_json = True
        except Exception:
            valid_json = False
        if kind == "json_schema" and not isinstance(schema, dict):
            return _aggregate_core_checks(
                [
                    {
                        "status": "error",
                        "reason_code": "json_schema_missing",
                        "errors": ["No JSON Schema was supplied for this verifier."],
                    }
                ]
            )
        if valid_json and isinstance(schema, dict) and context.schema_validator is None:
            return _aggregate_core_checks(
                [
                    {
                        "status": "error",
                        "reason_code": "json_schema_validator_unavailable",
                        "errors": ["JSON Schema validator is unavailable."],
                    }
                ]
            )
        errors: list[str] = []
        valid_schema = valid_json
        if valid_json and isinstance(schema, dict) and context.schema_validator is not None:
            valid_schema, errors = context.schema_validator(str(candidate or ""), schema)
        schema_verified = valid_json and valid_schema and isinstance(schema, dict)
        return _aggregate_core_checks(
            [
                {
                    "status": "pass" if valid_json and valid_schema else "fail",
                    "reason_code": (
                        "json_schema_verified"
                        if schema_verified
                        else "json_verified"
                        if valid_json and valid_schema
                        else "json_schema_failed"
                        if isinstance(schema, dict)
                        else "json_invalid"
                    ),
                    "errors": [str(item)[:500] for item in errors[:10]],
                }
            ]
        )
    if kind == "artifact_exists":
        checks, artifact_refs = _artifact_checks(
            criterion,
            workspace_root=context.workspace_root,
            check_integrity=False,
        )
        return _aggregate_core_checks(
            checks
            or [{"status": "fail", "reason_code": "artifact_expectation_missing"}],
            artifact_refs=artifact_refs,
        )
    if kind == "artifact_integrity":
        checks, artifact_refs = _artifact_checks(
            criterion,
            workspace_root=context.workspace_root,
            check_integrity=True,
        )
        return _aggregate_core_checks(
            checks
            or [{"status": "fail", "reason_code": "artifact_expectation_missing"}],
            artifact_refs=artifact_refs,
        )
    if kind == "artifact_changed":
        checks, artifact_refs = _artifact_changed_checks(
            criterion,
            workspace_root=context.workspace_root,
        )
        return _aggregate_core_checks(
            checks
            or [{"status": "fail", "reason_code": "artifact_expectation_missing"}],
            artifact_refs=artifact_refs,
        )
    if kind == "tool_success":
        return _aggregate_core_checks(_tool_success_checks(criterion, ledger))
    if kind == "source_coverage":
        return _aggregate_core_checks(_source_coverage_checks(criterion, ledger))
    if kind == "human_approval":
        return _aggregate_core_checks(_human_approval_checks(criterion, ledger))
    return _aggregate_core_checks(
        [{"status": "uncertain", "reason_code": "verifier_not_implemented"}]
    )


_CORE_VERIFIERS = (
    ("final_response", ("final_response", "response_present"), "P1"),
    ("json", ("json",), "P1"),
    ("json_schema", ("json_schema", "schema"), "P2"),
    ("artifact_exists", ("artifact_exists", "artifact_existence"), "P2"),
    ("artifact_integrity", ("artifact_integrity", "artifact_digest"), "P2"),
    ("artifact_changed", ("artifact_changed", "workspace_change"), "P2"),
    ("tool_success", ("tool_success", "command_result", "test_result"), "P2"),
    ("source_coverage", ("source_coverage",), "P2"),
    # Approval evidence is not contract/version/criterion-bound yet, so v1 caps it at P2.
    ("human_approval", ("human_approval",), "P2"),
    ("semantic", ("semantic", "model"), "P1"),
    ("human_review", ("human_review",), "P1"),
)


def ensure_core_verifiers() -> None:
    """Idempotently publish all built-in verifier definitions."""

    for kind, aliases, max_level in _CORE_VERIFIERS:
        verifier_id = f"tcode.core.{kind}"
        if VerifierRegistry.resolve(verifier_id) is not None:
            continue

        def handler(
            _payload: dict[str, Any],
            execution_context: _DeterministicExecutionContext,
            *,
            _kind: str = kind,
        ) -> dict[str, Any]:
            return _run_core_verifier(_kind, execution_context)

        try:
            VerifierRegistry.register(
                VerifierDefinition(
                    verifier_id=verifier_id,
                    verifier_version="1.0.0",
                    provider="core",
                    input_schema=VERIFIER_INPUT_SCHEMA,
                    output_schema=VERIFIER_OUTPUT_SCHEMA,
                    admitted_max_proof_level=max_level,
                    timeout_seconds=5.0,
                    required_capabilities=(),
                    isolation_policy="host-core",
                    aliases=aliases,
                    description=f"TCode core {kind} verifier",
                ),
                handler,
                source="tcode.core",
                replace=False,
            )
        except VerifierRegistrationError:
            current = VerifierRegistry.resolve(verifier_id)
            if current is None or current.definition.provider != "core":
                raise


def _verifier_input_payload(
    candidate: str,
    criterion: OutcomeSuccessCriterion,
    contract: OutcomeContract,
    ledger: EvidenceLedger,
    *,
    workspace_root: Optional[str],
) -> dict[str, Any]:
    contract_payload = contract.to_v2_dict()
    if ledger.applied_user_guidance:
        contract_payload["metadata"]["applied_user_guidance"] = list(ledger.applied_user_guidance)
    return {
        "candidate": str(candidate or ""),
        "criterion": asdict(criterion),
        "contract": contract_payload,
        "evidence": [entry.to_dict() for entry in ledger.entries[-20:]],
        "workspace_root": str(workspace_root) if workspace_root else None,
    }


def verifier_input_digests(
    candidate: str,
    contract: OutcomeContract,
    ledger: EvidenceLedger,
    *,
    workspace_root: Optional[str] = None,
) -> dict[str, str]:
    """Compute the current input binding for every criterion in a contract."""

    normalized_candidate = str(candidate or "").strip()
    return {
        criterion.criterion_id: verifier_input_digest(
            _verifier_input_payload(
                normalized_candidate,
                criterion,
                contract,
                ledger,
                workspace_root=workspace_root,
            )
        )
        for criterion in contract.success_criteria
    }


def verify_criterion_deterministic(
    candidate: str,
    criterion: OutcomeSuccessCriterion,
    contract: OutcomeContract,
    ledger: EvidenceLedger,
    *,
    schema_validator: Optional[Callable[[str, dict[str, Any]], tuple[bool, list[str]]]] = None,
    workspace_root: Optional[str] = None,
) -> CriterionVerificationResult:
    """Run all declared core verifiers for one criterion without side effects."""
    ensure_core_verifiers()
    refs = [str(item or "").strip().lower() for item in criterion.verifier_refs]
    execution_context = _DeterministicExecutionContext(
        candidate=str(candidate or ""),
        criterion=criterion,
        contract=contract,
        ledger=ledger,
        schema_validator=schema_validator,
        workspace_root=workspace_root,
    )
    input_payload = _verifier_input_payload(
        candidate,
        criterion,
        contract,
        ledger,
        workspace_root=workspace_root,
    )
    verifier_results = [
        VerifierRegistry.execute(
            verifier_ref,
            input_payload,
            execution_context=execution_context,
        )
        for verifier_ref in refs
    ]
    if not verifier_results:
        verifier_results = [
            {
                "requested_verifier_ref": "",
                "status": "uncertain",
                "reason_code": "verifier_not_declared",
                "observations": ["verifier_not_declared"],
                "artifact_refs": [],
                "evidence_refs": [],
                "details": {},
                "retryable": False,
                "registry_verified": False,
                "admitted_proof_level": "P0",
            }
        ]
    statuses = {str(item.get("status") or "uncertain") for item in verifier_results}
    if "fail" in statuses:
        status = "fail"
    elif "error" in statuses:
        status = "error"
    elif "uncertain" in statuses:
        status = "uncertain"
    else:
        status = "pass"
    observations = [
        str(observation)
        for item in verifier_results
        for observation in (item.get("observations") or [])
        if str(observation or "").strip()
    ]
    reason_codes = [str(item.get("reason_code") or "") for item in verifier_results]
    artifact_refs = [
        dict(ref)
        for item in verifier_results
        for ref in (item.get("artifact_refs") or [])
        if isinstance(ref, dict)
    ]
    call_ids = [
        str(call_id)
        for item in verifier_results
        for call_id in (item.get("evidence_refs") or [])
        if str(call_id or "").strip()
    ]
    return CriterionVerificationResult(
        result_id=_result_id(criterion.criterion_id, verifier_results),
        criterion_id=criterion.criterion_id,
        status=status,
        verifier_id="tcode.verifier-registry",
        verifier_version="1.0.0",
        observations=observations,
        artifact_refs=list(
            {
                str(item.get("path") or index): item
                for index, item in enumerate(artifact_refs)
            }.values()
        ),
        evidence_refs=list(dict.fromkeys(call_ids)),
        reason_code=reason_codes[0] if len(reason_codes) == 1 else "multiple_checks",
        retryable=any(bool(item.get("retryable")) for item in verifier_results),
        verifier_results=verifier_results,
    )


def verify_contract_criteria(
    candidate: str,
    contract: OutcomeContract,
    ledger: EvidenceLedger,
    *,
    schema_validator: Optional[Callable[[str, dict[str, Any]], tuple[bool, list[str]]]] = None,
    workspace_root: Optional[str] = None,
) -> list[CriterionVerificationResult]:
    return [
        verify_criterion_deterministic(
            candidate,
            criterion,
            contract,
            ledger,
            schema_validator=schema_validator,
            workspace_root=workspace_root,
        )
        for criterion in contract.success_criteria
    ]
