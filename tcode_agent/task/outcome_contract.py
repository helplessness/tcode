"""Versioned outcome contracts for deciding when an agent turn is complete.

The public ``compile`` entry point intentionally accepts both the original lightweight
contract and the native v2 shape.  The runtime therefore has one adjudication path while
older WebSocket/API callers can migrate without a flag day.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _message_requests_json(user_message: str) -> bool:
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    # An omission constraint is not a delivery request. Remove only the negated
    # format phrase, keeping independent positive requests in the same message.
    # “不要 JSON 外的解释” excludes prose, not JSON, so leave that phrase intact.
    text = re.sub(
        r"(?:不要|不需要|无需|不用|不必|禁止|不得|不允许|不)\s*"
        r"(?:(?:再|向用户|为用户|直接|单独|额外)\s*)*"
        r"(?:(?:展示|显示|输出|返回|生成|提供|呈现|包含|使用|采用|以|用)\s*)?"
        r"(?:(?:内部|任何|原始|严格的?|完整的?|有效的?|结构化的?)\s*)*"
        r"json(?!\s*(?:以外|之外|外))",
        "",
        text,
    )
    text = re.sub(
        r"\b(?:do\s+not|don't|never|without|avoid|no(?:\s+need\s+to)?)\s+"
        r"(?:(?:show(?:ing)?|display(?:ing)?|return(?:ing)?|output(?:ting)?|"
        r"generat(?:e|ing)|provid(?:e|ing)|us(?:e|ing)|include|including)\s+)?"
        r"(?:(?:any|the|internal|raw|strict|valid|structured)\s+)*json\b",
        "",
        text,
    )
    compact = "".join(text.split())
    json_terms = (
        "strict json",
        "valid json",
        "return json",
        "json only",
        "only json",
        "json format",
        "json schema",
        "输出严格json",
        "严格json",
        "只输出json",
        "仅输出json",
        "json格式",
        "不要json外",
    )
    if any(term in text or term in compact for term in json_terms):
        return True
    # Accept ordinary delivery language such as “生成一个符合标准的 JSON”，while
    # avoiding explanatory questions such as “如何生成 JSON”.
    explanatory = re.search(
        r"(?:如何|怎么|怎样|为什么|解释|介绍|what\s+is|how\s+to|explain)",
        text,
        flags=re.IGNORECASE,
    )
    delivery = re.search(
        r"(?:生成|输出|返回|给我|创建|create|generate|return|output|produce)"
        r"[^，,。.!?;；\n]{0,48}json",
        text,
        flags=re.IGNORECASE,
    )
    return bool(delivery and not explanatory)


@dataclass
class OutcomeDeliverable:
    name: str = "final_response"
    kind: str = "message"
    required: bool = True
    schema: Optional[dict[str, Any]] = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutcomeDeliverable":
        return cls(
            name=str(value.get("name") or "final_response").strip(),
            kind=str(value.get("kind") or "message").strip().lower(),
            required=bool(value.get("required", True)),
            schema=dict(value.get("schema") or {})
            if isinstance(value.get("schema"), dict)
            else None,
        )


@dataclass
class OutcomeConstraint:
    rule: str
    required: bool = True
    verification: str = "semantic"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutcomeConstraint":
        return cls(
            rule=str(value.get("rule") or "").strip(),
            required=bool(value.get("required", True)),
            verification=str(value.get("verification") or "semantic").strip().lower(),
        )


def _stable_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return [str(item).strip() for item in values if str(item or "").strip()]


@dataclass
class OutcomeSuccessCriterion:
    """A versioned, individually adjudicable success condition."""

    criterion_id: str
    description: str
    required: bool = True
    verifier_refs: list[str] = field(default_factory=list)
    expected_artifacts: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        goal: str,
        index: int,
        default_verifier: str = "semantic",
    ) -> "OutcomeSuccessCriterion":
        if isinstance(value, dict):
            description = str(
                value.get("description") or value.get("criterion") or ""
            ).strip()
            verifier_refs = _string_list(
                value.get("verifier_refs") or value.get("verifiers")
            )
            if not verifier_refs and default_verifier:
                verifier_refs = [default_verifier]
            raw_artifacts = value.get("expected_artifacts") or []
            expected_artifacts: list[dict[str, Any]] = []
            for artifact in raw_artifacts if isinstance(raw_artifacts, list) else []:
                if isinstance(artifact, str) and artifact.strip():
                    expected_artifacts.append({"path": artifact.strip(), "required": True})
                elif isinstance(artifact, dict):
                    normalized = {
                        key: item
                        for key, item in dict(artifact).items()
                        if item is not None
                    }
                    if str(normalized.get("path") or "").strip():
                        normalized["path"] = str(normalized["path"]).strip()
                        normalized.setdefault("required", True)
                        expected_artifacts.append(normalized)
            if expected_artifacts and "artifact_exists" not in verifier_refs:
                verifier_refs.append("artifact_exists")
            if any(
                item.get("sha256") or item.get("content_digest") or item.get("size") is not None
                for item in expected_artifacts
            ) and "artifact_integrity" not in verifier_refs:
                verifier_refs.append("artifact_integrity")
            criterion_id = str(value.get("criterion_id") or "").strip()
            parameters = (
                dict(value.get("parameters") or {})
                if isinstance(value.get("parameters"), dict)
                else {}
            )
            required = bool(value.get("required", True))
        else:
            description = str(value or "").strip()
            verifier_refs = [default_verifier] if default_verifier else []
            expected_artifacts = []
            criterion_id = ""
            parameters = {}
            required = True
        description = description or f"Success criterion {index + 1}"
        if not criterion_id:
            criterion_id = _stable_id(
                "criterion",
                {"goal": goal, "description": description, "index": index},
            )
        return cls(
            criterion_id=criterion_id,
            description=description,
            required=required,
            verifier_refs=verifier_refs,
            expected_artifacts=expected_artifacts,
            parameters=parameters,
        )


@dataclass
class OutcomeContract:
    goal: str
    deliverables: list[OutcomeDeliverable] = field(default_factory=list)
    constraints: list[OutcomeConstraint] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)
    partial_result_policy: str = "return_partial_with_warnings"
    completion_policy: str = "all_required_checks_pass"
    semantic_validation: str = "never"
    max_repair_attempts: int = 1
    explicit: bool = False
    schema_version: str = "tcode.outcome/v2"
    contract_id: str = ""
    goal_id: str = ""
    version: int = 1
    title: str = ""
    intent: str = ""
    risk_level: str = "low"
    proof_level: str = "P0"
    success_criteria: list[OutcomeSuccessCriterion] = field(default_factory=list)
    execution_constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    compatibility_mode: str = "legacy_v1_adapter"

    @classmethod
    def compile(
        cls,
        user_message: str,
        *,
        output_format: Optional[dict[str, Any]] = None,
        supplied: Optional[dict[str, Any]] = None,
    ) -> "OutcomeContract":
        """Build a generic contract without classifying the task by domain."""

        supplied = dict(supplied or {}) if isinstance(supplied, dict) else {}
        schema = None
        if (
            isinstance(output_format, dict)
            and output_format.get("type") == "json_schema"
            and isinstance(output_format.get("schema"), dict)
        ):
            schema = dict(output_format["schema"])

        raw_deliverables = supplied.get("deliverables")
        deliverables = [
            OutcomeDeliverable.from_dict(item)
            for item in (raw_deliverables or [])
            if isinstance(item, dict)
        ]
        wants_json = bool(schema) or _message_requests_json(user_message)

        if not deliverables:
            deliverables = [
                OutcomeDeliverable(
                    name="final_response",
                    kind="json" if wants_json else "message",
                    required=True,
                    schema=schema,
                )
            ]
        elif schema and not any(item.schema for item in deliverables):
            deliverables[0].schema = schema
            deliverables[0].kind = "json"

        raw_constraints = supplied.get("constraints")
        constraint_rules = (
            raw_constraints
            if isinstance(raw_constraints, list)
            else list((raw_constraints or {}).get("rules") or [])
            if isinstance(raw_constraints, dict)
            else []
        )
        constraints = [
            OutcomeConstraint.from_dict(item)
            for item in constraint_rules
            if isinstance(item, dict) and str(item.get("rule") or "").strip()
        ]
        criteria = [
            str(item).strip()
            for item in (supplied.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ]
        if not criteria:
            criteria = [
                "The final deliverable directly satisfies the user's original request.",
                "Explicit output, scope, source, and omission constraints are respected.",
                "Intermediate progress is not presented as the completed deliverable.",
            ]

        try:
            max_repairs = int(supplied.get("max_repair_attempts", 1))
        except Exception:
            max_repairs = 1

        goal = str(supplied.get("goal") or user_message or "").strip()
        native_v2 = bool(
            supplied.get("success_criteria") is not None
            or str(supplied.get("schema_version") or "").strip()
            == "tcode.outcome/v2"
        )
        raw_success_criteria = supplied.get("success_criteria")
        if isinstance(raw_success_criteria, list):
            success_criteria = [
                OutcomeSuccessCriterion.from_value(item, goal=goal, index=index)
                for index, item in enumerate(raw_success_criteria)
                if isinstance(item, (str, dict))
            ]
        else:
            success_criteria = [
                OutcomeSuccessCriterion.from_value(item, goal=goal, index=index)
                for index, item in enumerate(criteria)
            ]
        if native_v2 and not success_criteria:
            success_criteria = [
                OutcomeSuccessCriterion.from_value(
                    {
                        "description": "The required final response is present.",
                        "verifier_refs": ["final_response"],
                    },
                    goal=goal,
                    index=0,
                )
            ]

        risk_level = str(supplied.get("risk_level") or "low").strip().lower()
        if risk_level not in {"low", "medium", "high", "critical"}:
            risk_level = "low"
        proof_level = str(
            supplied.get("required_proof_level")
            or supplied.get("proof_level")
            or ("P1" if native_v2 else "P0")
        ).strip().upper()
        if proof_level not in {"P0", "P1", "P2", "P3", "P4"}:
            proof_level = "P0"
        try:
            version = max(1, int(supplied.get("version", 1)))
        except Exception:
            version = 1

        raw_execution_constraints = supplied.get("execution_constraints")
        execution_constraints = (
            dict(raw_constraints or {})
            if isinstance(raw_constraints, dict)
            else dict(raw_execution_constraints or {})
            if isinstance(raw_execution_constraints, dict)
            else {}
        )
        execution_constraints.pop("rules", None)
        execution_constraints.setdefault("time_limit_ms", None)
        execution_constraints.setdefault("cost_limit", None)
        execution_constraints.setdefault("requested_effect_classes", [])

        goal_id = str(supplied.get("goal_id") or "").strip() or _stable_id("goal", goal)
        contract_seed = {
            "goal_id": goal_id,
            "goal": goal,
            "version": version,
            "deliverables": [asdict(item) for item in deliverables],
            "criteria": [asdict(item) for item in success_criteria],
            "proof_level": proof_level,
        }
        contract_id = str(supplied.get("contract_id") or "").strip() or _stable_id(
            "outcome", contract_seed
        )
        raw_metadata = supplied.get("metadata")
        metadata = dict(raw_metadata or {}) if isinstance(raw_metadata, dict) else {}
        metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        metadata.setdefault("created_by", "user" if supplied else "harness")
        metadata.setdefault("template_ref", None)

        return cls(
            goal=goal,
            deliverables=deliverables,
            constraints=constraints,
            acceptance_criteria=criteria,
            source_policy=dict(supplied.get("source_policy") or {})
            if isinstance(supplied.get("source_policy"), dict)
            else {},
            partial_result_policy=str(
                supplied.get("partial_result_policy")
                or "return_partial_with_warnings"
            ).strip(),
            completion_policy=str(
                supplied.get("completion_policy") or "all_required_checks_pass"
            ).strip(),
            semantic_validation=str(
                supplied.get("semantic_validation") or "never"
            ).strip().lower(),
            max_repair_attempts=max(0, min(max_repairs, 5)),
            explicit=bool(supplied),
            contract_id=contract_id,
            goal_id=goal_id,
            version=version,
            title=str(supplied.get("title") or goal[:160]).strip(),
            intent=str(supplied.get("intent") or goal).strip(),
            risk_level=risk_level,
            proof_level=proof_level,
            success_criteria=success_criteria,
            execution_constraints=execution_constraints,
            metadata=metadata,
            compatibility_mode="native_v2" if native_v2 else "legacy_v1_adapter",
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_proof_level"] = self.required_proof_level
        return payload

    @property
    def required_proof_level(self) -> str:
        """Canonical minimum proof level; ``proof_level`` remains a v2 alias."""

        return self.proof_level

    def to_v2_dict(self) -> dict[str, Any]:
        """Return the canonical v2 projection without removing legacy fields."""

        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "goal_id": self.goal_id,
            "version": self.version,
            "title": self.title,
            "intent": self.intent,
            "risk_level": self.risk_level,
            "proof_level": self.proof_level,
            "required_proof_level": self.required_proof_level,
            "success_criteria": [asdict(item) for item in self.success_criteria],
            "deliverables": [asdict(item) for item in self.deliverables],
            "constraints": {
                **self.execution_constraints,
                "rules": [asdict(item) for item in self.constraints],
            },
            "completion_policy": {
                "mode": self.completion_policy,
                "all_required_must_pass": True,
                "uncertain_requires_review": True,
            },
            "metadata": dict(self.metadata),
            "compatibility_mode": self.compatibility_mode,
        }

    def digest(self) -> str:
        payload = self.to_v2_dict()
        # The new name is an additive alias for the existing value. Exclude it here so
        # stored v2 contract identities remain stable across this compatibility upgrade.
        payload.pop("required_proof_level", None)
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("created_at", None)
        payload["metadata"] = metadata
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="ignore")
        return hashlib.sha256(encoded).hexdigest()
