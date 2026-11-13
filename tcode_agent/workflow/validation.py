"""Executable validation gates for workflow stage inputs and outputs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import WorkflowRun, WorkflowStageTemplate


def declared_review_decisions(stage, validator=None):
    """审核状态由阶段契约声明；未声明的工作流沿用原有三种状态。"""
    configured = (validator or {}).get("allowed_decisions") or (
        (stage.output_contract or {}).get("properties", {}).get("review_decision", {}).get("enum"))
    if isinstance(configured, list) and configured and all(isinstance(item, str) and item.strip() for item in configured):
        return tuple(str(item).upper() for item in configured)
    return ("PASS", "REVISE", "ESCALATE")


SUPPORTED_STAGE_VALIDATORS = {
    "non_empty",
    "no_failure_signal",
    "json_schema",
    "expected_artifact_coverage",
    "required_artifacts",
    "files_exist",
    "contains_all",
    "contains_any",
    "review_decision",
    "evidence_coverage",
}

_EXPLICIT_FAILURE_RE = re.compile(
    r"(?:^|\n)\s*(?:[#>*-]+\s*)?(?:BLOCKED|FAILED|ERROR|FAILURE|阻塞|失败|执行失败)(?:\b|\s*[:：])",
    flags=re.IGNORECASE | re.MULTILINE,
)


@dataclass(slots=True)
class WorkflowValidationCheck:
    validator: str
    ok: bool
    code: str = ""
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowValidationReport:
    ok: bool
    phase: str
    checks: list[WorkflowValidationCheck] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        from .diagnostics import workflow_diagnostic
        payload = {
            "ok": bool(self.ok),
            "phase": self.phase,
            "checks": [item.to_dict() for item in self.checks],
            "summary": self.summary,
        }
        if not self.ok:
            payload["diagnostic"] = workflow_diagnostic(self.summary, payload)
        return payload


def _contract_required_fields(contract: dict[str, Any]) -> list[str]:
    required: list[str] = []
    for item in (contract or {}).get("required", []) or []:
        if isinstance(item, dict):
            value = item.get("field") or item.get("name") or item.get("key")
        else:
            value = item
        normalized = str(value or "").strip()
        if normalized and normalized not in required:
            required.append(normalized)
    return required


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def validate_stage_inputs(run: WorkflowRun, stage: WorkflowStageTemplate) -> WorkflowValidationReport:
    checks: list[WorkflowValidationCheck] = []
    dependencies_ok, missing_dependencies = run.dependencies_committed(stage.stage_id)
    checks.append(
        WorkflowValidationCheck(
            validator="dependencies_committed",
            ok=dependencies_ok,
            code="" if dependencies_ok else "dependency_not_committed",
            message=(
                "All stage dependencies are committed."
                if dependencies_ok
                else f"Dependencies are not committed: {', '.join(missing_dependencies)}"
            ),
            evidence={"missing_dependencies": missing_dependencies},
        )
    )

    missing_inputs = [
        field_name
        for field_name in _contract_required_fields(stage.input_contract)
        if not _has_value((run.input_payload or {}).get(field_name))
    ]
    checks.append(
        WorkflowValidationCheck(
            validator="input_contract",
            ok=not missing_inputs,
            code="" if not missing_inputs else "missing_stage_inputs",
            message=(
                "Stage input contract is satisfied."
                if not missing_inputs
                else f"Missing stage inputs: {', '.join(missing_inputs)}"
            ),
            evidence={"missing_inputs": missing_inputs},
        )
    )
    ok = all(item.ok for item in checks)
    return WorkflowValidationReport(
        ok=ok,
        phase="input",
        checks=checks,
        summary="stage inputs accepted" if ok else "stage inputs rejected",
    )


def _json_fingerprint(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return repr(value)


def _repair_json_syntax(text: str) -> str:
    """Repair only lossless JSON presentation mistakes made by language models.

    Literal control characters inside a quoted JSON string and trailing commas
    are unambiguous serialization errors.  Escaping/removing them does not add,
    delete, or reinterpret scientific fields.
    """

    source = str(text or "")
    escaped: list[str] = []
    in_string = False
    escape_next = False
    for char in source:
        if in_string:
            if escape_next:
                escaped.append(char)
                escape_next = False
            elif char == "\\":
                escaped.append(char)
                escape_next = True
            elif char == '"':
                escaped.append(char)
                in_string = False
            elif char == "\n":
                escaped.append("\\n")
            elif char == "\r":
                escaped.append("\\r")
            elif char == "\t":
                escaped.append("\\t")
            else:
                escaped.append(char)
        else:
            escaped.append(char)
            if char == '"':
                in_string = True

    normalized = "".join(escaped)
    without_trailing_commas: list[str] = []
    in_string = False
    escape_next = False
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if in_string:
            without_trailing_commas.append(char)
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            without_trailing_commas.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(normalized) and normalized[lookahead].isspace():
                lookahead += 1
            if lookahead < len(normalized) and normalized[lookahead] in "}]":
                index += 1
                continue
        without_trailing_commas.append(char)
        index += 1
    return "".join(without_trailing_commas)


def _json_candidates(response: str) -> list[tuple[Any, str]]:
    """Return every bounded JSON candidate instead of trusting the first one.

    Agent responses can contain a short list in explanatory prose before the
    authoritative object.  Selecting the first decodable value makes a valid
    stage fail with ``expected object, got list``.  Keep all distinct values so
    the output contract can select the matching candidate deterministically.
    """

    text = str(response or "").strip()
    candidates: list[tuple[Any, str]] = []
    seen: set[str] = set()

    def _append(value: Any, source: str) -> None:
        fingerprint = _json_fingerprint(value)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        candidates.append((value, source))

    for block_index, candidate in enumerate(
        re.findall(
            r"```(?:json)?\s*([\s\S]*?)```",
            text,
            flags=re.IGNORECASE,
        ),
        start=1,
    ):
        block = str(candidate or "").strip()
        try:
            _append(json.loads(block), f"fenced:{block_index}")
        except Exception:
            repaired = _repair_json_syntax(block)
            if repaired == block:
                continue
            try:
                _append(
                    json.loads(repaired),
                    f"fenced:{block_index}.syntax_repair",
                )
            except Exception:
                continue

    decoder = json.JSONDecoder()
    raw_index = 0
    decode_attempts = 0
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        decode_attempts += 1
        if decode_attempts > 256 or len(candidates) >= 64:
            break
        try:
            value, _ = decoder.raw_decode(text[index:])
        except Exception:
            repaired = _repair_json_syntax(text[index:])
            try:
                value, _ = decoder.raw_decode(repaired)
            except Exception:
                continue
            raw_index += 1
            _append(value, f"inline:{raw_index}.syntax_repair")
            continue
        raw_index += 1
        _append(value, f"inline:{raw_index}")
    return candidates


def _unwrap_stage_receipt_candidates(payload: Any) -> list[tuple[Any, str]]:
    """Expose lossless, well-known commit-tool envelopes as candidates."""

    candidates = [(payload, "direct")]
    if not isinstance(payload, dict):
        return candidates

    stage_receipt = payload.get("stage_receipt")
    if set(payload) == {"stage_receipt"} and isinstance(stage_receipt, dict):
        candidates.append((dict(stage_receipt), "stage_receipt"))

    # Some agents copy the complete successful host-tool envelope.  Only
    # unwrap the exact, non-error shape; arbitrary nested objects remain
    # subject to the original schema and are never guessed as stage output.
    result = payload.get("result")
    if (
        set(payload).issubset({"ok", "result"})
        and payload.get("ok", True) is True
        and isinstance(result, dict)
    ):
        if set(payload) == {"result"}:
            candidates.append((dict(result), "result"))
        nested_receipt = result.get("stage_receipt")
        if isinstance(nested_receipt, dict):
            candidates.append((dict(nested_receipt), "result.stage_receipt"))
    return candidates


def _select_json_for_schema(
    response: str,
    schema: dict[str, Any],
) -> tuple[Any, list[str], dict[str, Any]]:
    """Select the candidate with the best unchanged-schema match."""

    ranked: list[tuple[tuple[int, int, int, int], Any, list[str], str]] = []
    candidate_count = 0
    for raw_index, (raw_value, raw_source) in enumerate(_json_candidates(response)):
        for unwrap_index, (value, unwrap_source) in enumerate(
            _unwrap_stage_receipt_candidates(raw_value)
        ):
            candidate_count += 1
            errors = _validate_basic_json_schema(value, schema)
            expected_type = str((schema or {}).get("type") or "").strip()
            type_penalty = int(
                bool(expected_type) and not _json_type_matches(value, expected_type)
            )
            score = (
                0 if not errors else 1,
                type_penalty,
                len(errors),
                raw_index * 10 + unwrap_index,
            )
            source = (
                raw_source
                if unwrap_source == "direct"
                else f"{raw_source}.{unwrap_source}"
            )
            ranked.append((score, value, errors, source))

    if not ranked:
        return None, ["no JSON object or array found"], {
            "candidate_count": 0,
            "selected_source": "",
        }
    _, value, errors, source = min(ranked, key=lambda item: item[0])
    return value, errors, {
        "candidate_count": candidate_count,
        "selected_source": source,
    }


def normalize_stage_output_response(
    stage: WorkflowStageTemplate,
    response: str,
) -> tuple[str, dict[str, Any]]:
    """Canonicalize a valid JSON stage result without inventing any content.

    This is a presentation repair only: it may remove Markdown fences, choose a
    later schema-valid JSON value, or unwrap a successful commit-tool receipt.
    Missing scientific fields and type errors are deliberately not repaired.
    """

    text = str(response or "").strip()
    if _EXPLICIT_FAILURE_RE.search(text):
        return text, {
            "applied": False,
            "reason": "explicit_failure_signal_preserved",
        }
    json_validator = next(
        (
            config
            for config in _normalized_validators(stage)
            if str(config.get("type") or "").strip().lower() == "json_schema"
        ),
        None,
    )
    if json_validator is None:
        return text, {"applied": False, "reason": "json_schema_not_configured"}

    schema = dict(json_validator.get("schema") or stage.output_contract or {})
    selected, errors, selection = _select_json_for_schema(text, schema)
    if selected is None or errors:
        return text, {
            "applied": False,
            "reason": "no_schema_valid_json_candidate",
            "errors": errors,
            **selection,
        }

    canonical = json.dumps(selected, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return canonical, {
        "applied": canonical != text,
        "reason": "schema_valid_json_canonicalized",
        **selection,
    }


def _json_type_matches(value: Any, expected: str) -> bool:
    mapping = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    expected_type = mapping.get(str(expected or "").strip().lower())
    if expected_type is None:
        return False
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, expected_type)


def _validate_basic_json_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_type = str((schema or {}).get("type") or "").strip()
    if expected_type and not _json_type_matches(value, expected_type):
        return [f"expected {expected_type}, got {type(value).__name__}"]
    if isinstance(value, dict):
        for key in (schema or {}).get("required", []) or []:
            normalized = str(key or "").strip()
            if normalized and normalized not in value:
                errors.append(f"missing required property: {normalized}")
        properties = (schema or {}).get("properties")
        if isinstance(properties, dict):
            for key, property_schema in properties.items():
                if key not in value or not isinstance(property_schema, dict):
                    continue
                property_type = str(property_schema.get("type") or "").strip()
                if property_type and not _json_type_matches(value[key], property_type):
                    errors.append(
                        f"property {key} expected {property_type}, got {type(value[key]).__name__}"
                    )
    return errors


def _artifact_values(artifact_refs: dict[str, Any], names: Iterable[str]) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    for raw_name in names:
        name = str(raw_name or "").strip()
        if name:
            values.append((name, (artifact_refs or {}).get(name)))
    return values


def _artifact_name_in_text(name: str, text: str) -> bool:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return False
    haystack = str(text or "").lower()
    candidates = {
        normalized_name,
        normalized_name.replace("_", " "),
        normalized_name.replace("-", " "),
        normalized_name.replace("_", "-"),
        normalized_name.replace("-", "_"),
    }
    return any(candidate.lower() in haystack for candidate in candidates if candidate)


def _artifact_path(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("path") or value.get("artifact_path") or value.get("output_path")
    return str(value or "").strip()


def _resolve_workspace_path(path_value: str, workdir: str) -> Path | None:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    root = Path(workdir or ".").expanduser().resolve()
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except Exception:
        return None
    return resolved


def _validator_enabled(config: dict[str, Any]) -> bool:
    if "enabled" not in config:
        return True
    value = config.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _condition_lookup(payload: Any, field: str) -> Any:
    """Resolve a dotted field path against the parsed stage output."""

    normalized = str(field or "").strip()
    if not normalized:
        return None
    current = payload
    for part in normalized.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _condition_equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().lower() == expected.strip().lower()
    return actual == expected


def _validator_condition_met(
    config: dict[str, Any],
    *,
    payload: Any,
    text: str,
) -> bool:
    """Evaluate an optional ``when`` gate on a validator definition.

    A validator without ``when`` always runs. When a gate is present it is
    matched against the parsed stage output (``field`` + one of ``equals``,
    ``not_equals``, ``in``, ``not_in``, ``matches``, ``exists``). An
    unevaluatable gate fails closed so a misconfigured condition cannot
    silently disable a required check.
    """

    condition = config.get("when")
    if condition is None or condition == {}:
        return True
    if not isinstance(condition, dict):
        return True
    field = str(condition.get("field") or condition.get("path") or "").strip()
    if not field:
        return True
    actual = _condition_lookup(payload, field)
    if "equals" in condition or "eq" in condition:
        return _condition_equals(actual, condition.get("equals", condition.get("eq")))
    if "not_equals" in condition or "ne" in condition:
        return not _condition_equals(actual, condition.get("not_equals", condition.get("ne")))
    if "in" in condition or "one_of" in condition:
        values = condition.get("in", condition.get("one_of"))
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        return any(_condition_equals(actual, item) for item in values)
    if "not_in" in condition:
        values = condition.get("not_in")
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        return not any(_condition_equals(actual, item) for item in values)
    if "matches" in condition or "regex" in condition:
        pattern = str(condition.get("matches", condition.get("regex")) or "")
        try:
            return re.search(pattern, str(actual or "")) is not None
        except re.error:
            return True
    if "exists" in condition:
        return bool(_has_value(actual)) == bool(condition.get("exists"))
    # Unknown gate shape: run the validator rather than skip a required check.
    return True


def _normalized_validators(stage: WorkflowStageTemplate) -> list[dict[str, Any]]:
    validators = [
        {"type": "non_empty"},
        {"type": "no_failure_signal"},
    ]
    validators.extend(
        dict(item)
        for item in stage.validators or []
        if _validator_enabled(dict(item))
    )
    if stage.review_gate and not any(item.get("type") == "review_decision" for item in validators):
        validators.append({"type": "review_decision"})
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in validators:
        fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(item)
    return deduped


def _evidence_ref_ids_from_value(value: Any) -> list[str]:
    """Extract evidence IDs from artifact refs or nested metadata."""

    ids: list[str] = []

    def add(raw: Any):
        text = str(raw or "").strip()
        if text and text not in ids:
            ids.append(text)

    def walk(item: Any):
        if item is None:
            return
        if isinstance(item, str):
            try:
                from ..evidence import extract_evidence_ids

                extracted = extract_evidence_ids(item)
            except Exception:
                extracted = []
            if extracted:
                for evidence_id in extracted:
                    add(evidence_id)
                return
            for part in re.split(r"[,;\s]+", item):
                if part.strip().startswith("ev-"):
                    add(part)
            return
        if isinstance(item, dict):
            for key in ("evidence_id", "id", "citation_id"):
                if item.get(key):
                    add(item.get(key))
            for key in ("evidence_refs", "evidence_ids", "evidence_records", "citations"):
                if key in item:
                    walk(item.get(key))
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child)

    walk(value)
    return ids


def _artifact_evidence_ref_ids(
    artifact_refs: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    keys: list[str] = []
    configured = str(config.get("artifact_key") or "").strip()
    if configured:
        keys.append(configured)
    keys.extend(["evidence_refs", "evidence_ids", "evidence_records", "citations"])

    ids: list[str] = []
    for key in keys:
        if key not in (artifact_refs or {}):
            continue
        for evidence_id in _evidence_ref_ids_from_value(artifact_refs.get(key)):
            if evidence_id not in ids:
                ids.append(evidence_id)
    return ids


def _int_config(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except Exception:
        return default


def _bool_config(config: dict[str, Any], key: str, default: bool) -> bool:
    if key not in config:
        return default
    value = config.get(key)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def validate_stage_output(
    stage: WorkflowStageTemplate,
    *,
    response: str,
    artifact_refs: dict[str, Any] | None = None,
    workdir: str = "",
) -> WorkflowValidationReport:
    text = str(response or "").strip()
    artifacts = dict(artifact_refs or {})
    checks: list[WorkflowValidationCheck] = []
    condition_payload, _, _ = _select_json_for_schema(
        text,
        dict(stage.output_contract or {}),
    )

    for config in _normalized_validators(stage):
        if not _validator_condition_met(
            config,
            payload=condition_payload,
            text=text,
        ):
            continue
        validator_type = str(config.get("type") or "").strip().lower()
        if validator_type == "non_empty":
            ok = bool(text)
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=ok,
                    code="" if ok else "empty_stage_result",
                    message="Stage returned a non-empty result." if ok else "Stage returned no result.",
                    retryable=True,
                )
            )
        elif validator_type == "no_failure_signal":
            failed = bool(_EXPLICIT_FAILURE_RE.search(text))
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=not failed,
                    code="explicit_failure_signal" if failed else "",
                    message=(
                        "No explicit failure signal was returned."
                        if not failed
                        else "Stage explicitly reported failure or a blocker."
                    ),
                    retryable=False,
                )
            )
        elif validator_type == "json_schema":
            parsed, errors, selection = _select_json_for_schema(
                text,
                dict(config.get("schema") or stage.output_contract or {}),
            )
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=not errors,
                    code="invalid_output_schema" if errors else "",
                    message=(
                        "JSON output matches the schema."
                        if not errors
                        else "; ".join(errors)
                    ),
                    evidence={"errors": errors, **selection},
                    retryable=True,
                )
            )
        elif validator_type == "expected_artifact_coverage":
            names = config.get("names") or stage.expected_artifacts
            missing: list[str] = []
            covered: dict[str, str] = {}
            for name, value in _artifact_values(artifacts, names or []):
                if _has_value(value):
                    covered[name] = "artifact_ref"
                    continue
                if _artifact_name_in_text(name, text):
                    covered[name] = "response_text"
                    continue
                missing.append(name)
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=not missing,
                    code="expected_artifact_not_covered" if missing else "",
                    message=(
                        "Expected artifacts are covered by artifact refs or the response."
                        if not missing
                        else f"Expected artifacts not covered: {', '.join(missing)}"
                    ),
                    evidence={
                        "missing_artifacts": missing,
                        "covered_artifacts": covered,
                    },
                    retryable=True,
                )
            )
        elif validator_type == "required_artifacts":
            names = config.get("names") or stage.expected_artifacts
            missing = [name for name, value in _artifact_values(artifacts, names or []) if not _has_value(value)]
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=not missing,
                    code="missing_artifact" if missing else "",
                    message="Required artifacts are present." if not missing else f"Missing artifacts: {', '.join(missing)}",
                    evidence={"missing_artifacts": missing},
                    retryable=True,
                )
            )
        elif validator_type == "files_exist":
            names = config.get("names") or stage.expected_artifacts
            invalid: list[str] = []
            for name, value in _artifact_values(artifacts, names or []):
                resolved = _resolve_workspace_path(_artifact_path(value), workdir)
                if resolved is None or not resolved.is_file():
                    invalid.append(name)
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=not invalid,
                    code="artifact_file_missing" if invalid else "",
                    message="Artifact files exist in the workspace." if not invalid else f"Missing artifact files: {', '.join(invalid)}",
                    evidence={"invalid_artifact_files": invalid},
                    retryable=True,
                )
            )
        elif validator_type in {"contains_all", "contains_any"}:
            terms = [str(item or "").strip() for item in config.get("terms", []) or [] if str(item or "").strip()]
            hits = [item for item in terms if item.lower() in text.lower()]
            ok = len(hits) == len(terms) if validator_type == "contains_all" else bool(hits)
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=ok,
                    code="required_output_text_missing" if not ok else "",
                    message="Required output terms found." if ok else "Required output terms were not found.",
                    evidence={"terms": terms, "hits": hits},
                    retryable=True,
                )
            )
        elif validator_type == "review_decision":
            choices = declared_review_decisions(stage, config)
            pattern = "|".join(re.escape(value) for value in sorted(choices, key=len, reverse=True))
            decision_match = re.search(r"\b(" + pattern + r")\b", text, re.IGNORECASE)
            ok = decision_match is not None
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=ok,
                    code="missing_review_decision" if not ok else "",
                    message="Review decision found." if ok else "Review gate must return one of: " + ", ".join(choices),
                    evidence={"decision": decision_match.group(1).upper() if decision_match else ""},
                    retryable=True,
                )
            )
        elif validator_type == "evidence_coverage":
            try:
                from ..evidence import EvidenceStore, extract_evidence_ids
            except Exception as exc:
                checks.append(
                    WorkflowValidationCheck(
                        validator=validator_type,
                        ok=False,
                        code="evidence_store_unavailable",
                        message=f"Evidence store is unavailable: {exc}",
                        retryable=True,
                    )
                )
                continue

            min_records = _int_config(config, "min_records", 1)
            require_existing = _bool_config(config, "require_existing", True)
            require_response_citations = _bool_config(config, "require_response_citations", False)
            response_ids = extract_evidence_ids(text)
            artifact_ids = _artifact_evidence_ref_ids(artifacts, config)
            candidate_ids: list[str] = []
            for evidence_id in artifact_ids + response_ids:
                if evidence_id not in candidate_ids:
                    candidate_ids.append(evidence_id)

            existing_records = []
            missing_ids: list[str] = []
            for evidence_id in candidate_ids:
                record = EvidenceStore.get(evidence_id, workdir=workdir)
                if record is None:
                    missing_ids.append(evidence_id)
                    continue
                existing_records.append(record)

            required_entities = [
                str(item or "").strip()
                for item in config.get("required_entities", []) or []
                if str(item or "").strip()
            ]
            missing_entities: list[str] = []
            if required_entities:
                available = {
                    entity.lower()
                    for record in existing_records
                    for entity in list(getattr(record, "entities", []) or [])
                }
                missing_entities = [
                    entity for entity in required_entities if entity.lower() not in available
                ]

            required_terms = [
                str(item or "").strip()
                for item in config.get("required_terms", []) or []
                if str(item or "").strip()
            ]
            missing_terms: list[str] = []
            if required_terms:
                haystack = "\n".join(
                    str(getattr(record, "search_text", "") or getattr(record, "content", ""))
                    for record in existing_records
                ).lower()
                missing_terms = [term for term in required_terms if term.lower() not in haystack]

            count_for_gate = len(existing_records) if require_existing else len(candidate_ids)
            failures: list[str] = []
            if count_for_gate < min_records:
                failures.append("not_enough_evidence")
            if require_response_citations and not response_ids:
                failures.append("missing_response_evidence_citation")
            if require_existing and missing_ids:
                failures.append("unknown_evidence_refs")
            if missing_entities:
                failures.append("required_evidence_entities_missing")
            if missing_terms:
                failures.append("required_evidence_terms_missing")

            ok = not failures
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type,
                    ok=ok,
                    code=",".join(failures),
                    message=(
                        "Evidence coverage requirements are satisfied."
                        if ok
                        else "Evidence coverage requirements were not satisfied."
                    ),
                    evidence={
                        "min_records": min_records,
                        "candidate_evidence_ids": candidate_ids,
                        "response_evidence_ids": response_ids,
                        "artifact_evidence_ids": artifact_ids,
                        "verified_evidence_ids": [
                            getattr(record, "evidence_id", "") for record in existing_records
                        ],
                        "missing_evidence_ids": missing_ids,
                        "missing_entities": missing_entities,
                        "missing_terms": missing_terms,
                        "require_existing": require_existing,
                        "require_response_citations": require_response_citations,
                    },
                    retryable=True,
                )
            )
        else:
            checks.append(
                WorkflowValidationCheck(
                    validator=validator_type or "unknown",
                    ok=False,
                    code="unknown_validator",
                    message=f"Unsupported workflow validator: {validator_type or '(empty)'}",
                )
            )

    ok = all(item.ok for item in checks)
    failed_codes = [item.code for item in checks if not item.ok and item.code]
    return WorkflowValidationReport(
        ok=ok,
        phase="output",
        checks=checks,
        summary=(
            "stage output validated and ready to commit"
            if ok
            else "stage output rejected: " + ", ".join(failed_codes)
        ),
    )
