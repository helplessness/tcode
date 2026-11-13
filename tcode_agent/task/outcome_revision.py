"""Runtime revision planning for versioned outcome contracts.

The revision layer is deliberately separate from turn planning. A runtime
message may be a question, clarification, scope change, goal replacement, or
cancellation. Only an approved Contract change can alter the active outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..providers.base import ChatMessage
from ..runtime.verification import verification_chat
from .outcome_contract import OutcomeContract


_PROOF_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_ADMITTED_REFS = {
    "final_response",
    "json",
    "json_schema",
    "artifact_exists",
    "artifact_changed",
    "artifact_integrity",
    "command_result",
    "test_result",
    "tool_success",
    "source_coverage",
    "human_approval",
    "semantic",
}


@dataclass
class DescendantImpact:
    policy: str = "preserve"
    candidate_session_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutcomeContractRevision:
    classification: str
    base_contract_id: str = ""
    base_version: int = 0
    proposed_version: int = 0
    reason: str = ""
    relationship: str = "none"
    changes: list[str] = field(default_factory=list)
    proposed_by: str = "user"
    approval_required: bool = False
    planner_source: str = "host"
    confidence: float = 0.0
    needs_user_confirmation: bool = False
    revised_contract: Optional[OutcomeContract] = None
    descendant_impact: DescendantImpact = field(default_factory=DescendantImpact)

    @property
    def can_apply(self) -> bool:
        return bool(self.revised_contract) and not self.approval_required

    @property
    def invalidates_prior_validation(self) -> bool:
        return self.can_apply

    def to_dict(self, *, include_contract: bool = True) -> dict[str, Any]:
        payload = {
            "classification": self.classification,
            "base_contract_id": self.base_contract_id,
            "base_version": self.base_version,
            "proposed_version": self.proposed_version,
            "reason": self.reason,
            "relationship": self.relationship,
            "changes": list(self.changes),
            "proposed_by": self.proposed_by,
            "approval_required": self.approval_required,
            "invalidates_prior_validation": self.invalidates_prior_validation,
            "planner_source": self.planner_source,
            "confidence": self.confidence,
            "needs_user_confirmation": self.needs_user_confirmation,
            "can_apply": self.can_apply,
            "descendant_impact": self.descendant_impact.to_dict(),
        }
        if include_contract and self.revised_contract is not None:
            payload["contract"] = self.revised_contract.to_dict()
        return payload


class OutcomeContractRevisionPlanner:
    """Classify guidance and, when needed, produce one safe Contract revision."""

    @classmethod
    def classify(cls, message: str) -> str:
        """Conservative structural fallback used only when the planner is unavailable."""

        text = str(message or "").strip()
        if not text:
            return "QUESTION"
        if text.endswith(("?", "？")):
            return "QUESTION"
        return "CLARIFICATION"

    @classmethod
    async def plan(
        cls,
        base_contract: Optional[OutcomeContract],
        message: str,
        *,
        provider: Any = None,
        proposed_by: str = "user",
        conversation_context: Optional[list[Any]] = None,
        active_descendants: Optional[list[dict[str, Any]]] = None,
        usage_session: Any = None,
    ) -> OutcomeContractRevision:
        text = str(message or "").strip()
        proposed_by = str(proposed_by or "user").strip().lower() or "user"
        base_id = str(getattr(base_contract, "contract_id", "") or "")
        base_version = int(getattr(base_contract, "version", 0) or 0)
        decision = (
            cls._deterministic_revision_decision(base_contract, text)
            if proposed_by == "user"
            else {}
        )
        planner_source = "deterministic_explicit_revision" if decision else ""
        if not decision and cls._planner_enabled() and provider is not None:
            decision = await cls._ask_revision_planner(
                base_contract,
                text,
                provider=provider,
                conversation_context=conversation_context,
                active_descendants=active_descendants,
                usage_session=usage_session,
            )
            if decision:
                planner_source = "revision_agent"
        if not planner_source:
            planner_source = "conservative_fallback"
        classification = str(decision.get("classification") or "").strip().upper()
        if classification not in {
            "QUESTION",
            "CLARIFICATION",
            "SCOPE_CHANGE",
            "GOAL_REPLACEMENT",
            "CANCEL",
        }:
            classification = cls.classify(text)
        try:
            confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        if not decision:
            confidence = 0.35 if classification == "QUESTION" else 0.0
        descendant_impact = cls._descendant_impact(
            decision,
            active_descendants=active_descendants,
        )
        raw_changes_contract = decision.get(
            "changes_contract",
            classification in {
                "CLARIFICATION",
                "SCOPE_CHANGE",
                "GOAL_REPLACEMENT",
            },
        )
        changes_contract = (
            str(raw_changes_contract).strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_changes_contract, str)
            else bool(raw_changes_contract)
        )
        confidence_floor = (
            0.9
            if classification == "CANCEL"
            else 0.85
            if classification == "GOAL_REPLACEMENT"
            else 0.72
        )
        needs_confirmation = not decision or confidence < confidence_floor

        if (
            classification in {"QUESTION", "CANCEL"}
            or not changes_contract
            or base_contract is None
            or needs_confirmation
        ):
            return OutcomeContractRevision(
                classification=classification,
                base_contract_id=base_id,
                base_version=base_version,
                proposed_version=base_version,
                reason=(
                    str(decision.get("reason") or "").strip()
                    or (
                        "revision_intent_needs_confirmation"
                        if needs_confirmation
                        else "guidance_does_not_change_acceptance_contract"
                        if base_contract is not None
                        else "no_active_contract_to_revise"
                    )
                ),
                proposed_by=str(proposed_by or "user"),
                approval_required=needs_confirmation,
                planner_source=planner_source,
                confidence=confidence,
                needs_user_confirmation=needs_confirmation,
                descendant_impact=descendant_impact,
            )

        default_relationship = (
            "replace"
            if classification in {"SCOPE_CHANGE", "GOAL_REPLACEMENT"}
            else "strengthen"
        )
        relationship = str(decision.get("relationship") or default_relationship).lower()
        if relationship not in {"strengthen", "replace", "weaken"}:
            relationship = default_relationship

        approval_required = proposed_by != "user" and (
            classification == "GOAL_REPLACEMENT"
            or relationship in {"replace", "weaken"}
        )
        if classification == "GOAL_REPLACEMENT":
            revised = cls._compile_goal_replacement(
                base_contract,
                text,
                decision=decision,
                proposed_by=proposed_by,
            )
        else:
            revised = cls._compile_revision(
                base_contract,
                text,
                classification=classification,
                relationship=relationship,
                decision=decision,
                proposed_by=proposed_by,
            )
        return OutcomeContractRevision(
            classification=classification,
            base_contract_id=base_contract.contract_id,
            base_version=base_contract.version,
            proposed_version=revised.version,
            reason=str(decision.get("reason") or "runtime_guidance_changes_contract"),
            relationship=relationship,
            changes=[text[:2000]],
            proposed_by=proposed_by,
            approval_required=approval_required,
            planner_source=planner_source,
            confidence=confidence,
            revised_contract=revised,
            descendant_impact=descendant_impact,
        )

    @staticmethod
    def _descendant_impact(
        decision: dict[str, Any],
        *,
        active_descendants: Optional[list[dict[str, Any]]],
    ) -> DescendantImpact:
        raw = decision.get("descendant_impact")
        if not isinstance(raw, dict):
            return DescendantImpact()
        policy = str(raw.get("policy") or "preserve").strip().lower()
        if policy not in {"preserve", "cancel_all", "cancel_candidates"}:
            policy = "preserve"
        active_ids = {
            str(item.get("session_id") or "").strip()
            for item in list(active_descendants or [])
            if isinstance(item, dict) and str(item.get("session_id") or "").strip()
        }
        candidates = []
        for value in list(raw.get("candidate_session_ids") or []):
            session_id = str(value or "").strip()
            if session_id and session_id in active_ids and session_id not in candidates:
                candidates.append(session_id)
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if policy == "cancel_all":
            candidates = sorted(active_ids)
        if policy == "preserve":
            candidates = []
        return DescendantImpact(
            policy=policy,
            candidate_session_ids=candidates,
            confidence=confidence,
            reason=str(raw.get("reason") or "")[:1000],
        )

    @staticmethod
    def _planner_enabled() -> bool:
        raw = os.environ.get(
            "TCODE_OUTCOME_REVISION_PLANNER_ENABLED",
            os.environ.get("TCODE_OUTCOME_INTENT_PLANNER_ENABLED", "0"),
        )
        return str(raw or "0").strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _timeout_seconds() -> float:
        try:
            value = float(os.environ.get("TCODE_OUTCOME_REVISION_TIMEOUT_SECONDS", "12"))
        except (TypeError, ValueError):
            value = 12.0
        try:
            classify_timeout = float(
                os.environ.get("TCODE_GUIDANCE_CLASSIFY_TIMEOUT_SECONDS", "12")
            )
        except (TypeError, ValueError):
            classify_timeout = 12.0
        # The revision planner runs inside the Guidance classifier. Leave enough
        # time for the outer layer to persist the decision instead of racing two
        # identical 12-second deadlines and degrading an otherwise valid revision.
        inner_ceiling = max(1.0, classify_timeout - 1.0)
        return max(1.0, min(value, inner_ceiling, 30.0))

    @classmethod
    def _deterministic_revision_decision(
        cls,
        base_contract: Optional[OutcomeContract],
        message: str,
    ) -> dict[str, Any]:
        """Recognize explicit, exactly verifiable final-answer replacements.

        This narrow path intentionally handles only instructions whose acceptance
        condition can be compiled without semantic guessing. All other Guidance
        continues through the revision planner or conservative fallback.
        """

        if base_contract is None:
            return {}
        text = str(message or "").strip()
        if not text:
            return {}

        patterns = (
            re.compile(
                r"(?:最终|最后|改为)\s*(?:只|仅)\s*"
                r"(?:回复|回答|输出|返回)\s*(?:为|成)?\s*[:：]?\s*"
                r"(?P<value>[^\n。！？!?；;，,]{1,200})",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:final(?:ly|\s+answer)?|instead)\s*[,：:]?\s*"
                r"(?:reply|respond|return|output)\s+(?:with\s+)?(?:only\s+)?"
                r"(?P<value>[^\n.!?;,]{1,200})",
                re.IGNORECASE,
            ),
        )
        match = None
        for pattern in patterns:
            match = pattern.search(text)
            if match is not None:
                break
        if match is None:
            return {}
        raw_value = str(match.group("value") or "").strip()
        quoted = len(raw_value) >= 2 and raw_value[0] in "`'\"“‘「『" and raw_value[-1] in "`'\"”’」』"
        exact_value = raw_value.strip(" \t`'\"“”‘’「」『』")
        if not exact_value or len(exact_value) > 160:
            return {}
        placeholders = {
            "一句",
            "一句话",
            "内容",
            "结果",
            "新结果",
            "新的结果",
            "答案",
            "结论",
            "文本",
            "消息",
            "json",
            "代码",
            "one sentence",
            "the result",
            "result",
            "answer",
        }
        if not quoted and exact_value.strip().lower() in placeholders:
            return {}

        updated_goal = f"最终响应必须严格等于：{exact_value}"
        return {
            "classification": "SCOPE_CHANGE",
            "confidence": 0.99,
            "changes_contract": True,
            "relationship": "replace",
            "reason": "deterministic_explicit_acceptance_revision",
            "updated_goal": updated_goal,
            "risk_level": "low",
            "required_proof_level": "P1",
            "semantic_validation": "never",
            "explicit_user_override": True,
            "execution_constraints": {
                "supersede_running_tool": True,
                "retry_superseded_tool": False,
            },
            "success_criteria": [
                {
                    "description": updated_goal,
                    "required": True,
                    "verifier_refs": ["final_response"],
                    "parameters": {"exact": exact_value, "trim": True},
                }
            ],
        }

    @classmethod
    async def _ask_revision_planner(
        cls,
        base_contract: Optional[OutcomeContract],
        message: str,
        *,
        provider: Any,
        conversation_context: Optional[list[Any]] = None,
        active_descendants: Optional[list[dict[str, Any]]] = None,
        usage_session: Any = None,
    ) -> dict[str, Any]:
        if not callable(getattr(provider, "chat", None)):
            return {}
        context_rows: list[dict[str, str]] = []
        for item in list(conversation_context or [])[-8:]:
            role = str(getattr(item, "role", "") or "").strip().lower()
            content = str(getattr(item, "content", "") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            context_rows.append({"role": role, "content": content[:2000]})
        prompt = (
            "Classify the latest user message in the context of a currently running task. "
            "Use meaning and conversational context, not keyword matching. Do not execute "
            "the task. Return exactly one JSON object with: classification "
            "(QUESTION|CLARIFICATION|SCOPE_CHANGE|GOAL_REPLACEMENT|CANCEL), "
            "confidence (0..1), "
            "changes_contract (boolean), relationship (strengthen|replace|weaken|none), "
            "reason, updated_goal, risk_level "
            "(low|medium|high|critical), required_proof_level (P0..P4), and "
            "success_criteria (1-8 objects). Each criterion may contain description, "
            "required, verifier_refs, expected_artifacts, and parameters. Use only these "
            "verifier refs: "
            + ", ".join(sorted(_ADMITTED_REFS))
            + ". The latest user guidance wins where it conflicts with the old goal. "
            "Use GOAL_REPLACEMENT only when the fundamental objective or primary "
            "deliverable changes; use SCOPE_CHANGE when the same objective only changes "
            "scope, constraints, format, or acceptance criteria. "
            "Do not invent requirements.\n\nActive contract:\n"
            + json.dumps(
                base_contract.to_v2_dict() if base_contract is not None else None,
                ensure_ascii=False,
            )[:24_000]
            + "\n\nRecent conversation context:\n"
            + json.dumps(context_rows, ensure_ascii=False)[:12_000]
            + "\n\nActive descendant delegations (only these IDs may be candidates):\n"
            + json.dumps(list(active_descendants or [])[:64], ensure_ascii=False)[:12_000]
            + "\nReturn descendant_impact with policy "
            "(preserve|cancel_all|cancel_candidates), candidate_session_ids, "
            "confidence, and reason. Preserve unless the revised scope clearly "
            "invalidates a listed delegation."
            + "\n\nLatest user guidance:\n"
            + message[:8_000]
        )
        try:
            response = await asyncio.wait_for(
                verification_chat(
                    provider, source="automatic_contract_revision", session=usage_session,
                    messages=[
                        ChatMessage(
                            role="system",
                            content="You are an independent outcome-contract revision planner.",
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    tools=None,
                    temperature=0,
                ),
                timeout=cls._timeout_seconds(),
            )
        except Exception:
            return {}
        return cls._extract_json_object(str(getattr(response, "content", "") or ""))

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        normalized = str(text or "").strip()
        if normalized.startswith("```") and normalized.endswith("```"):
            normalized = "\n".join(normalized.splitlines()[1:-1]).strip()
        try:
            value = json.loads(normalized)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(normalized):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(normalized[index:])
            except Exception:
                continue
            if isinstance(value, dict):
                return dict(value)
        return {}

    @classmethod
    def _compile_revision(
        cls,
        base: OutcomeContract,
        guidance: str,
        *,
        classification: str,
        relationship: str,
        decision: dict[str, Any],
        proposed_by: str,
    ) -> OutcomeContract:
        proposed_goal = str(decision.get("updated_goal") or "").strip()
        updated_goal = proposed_goal or (
            f"{base.goal}\n\n[运行中修订 v{base.version + 1}]\n"
            f"{guidance}\n若与旧要求冲突，以本次修订为准。"
        )
        planned_criteria = cls._sanitize_criteria(decision.get("success_criteria"))
        base_criteria = [asdict(item) for item in base.success_criteria]
        if relationship == "strengthen":
            criteria = cls._merge_criteria(base_criteria, planned_criteria)
            if len(criteria) == len(base_criteria):
                criteria.append(cls._fallback_criterion(base, guidance))
        else:
            criteria = planned_criteria or [cls._fallback_criterion(base, guidance)]

        requested_proof = str(
            decision.get("required_proof_level") or base.required_proof_level
        ).upper()
        if requested_proof not in _PROOF_RANK:
            requested_proof = base.required_proof_level
        explicit_user_replacement = proposed_by == "user" and relationship in {
            "replace",
            "weaken",
        }
        proof_level = (
            requested_proof
            if explicit_user_replacement
            else max(
                (base.required_proof_level, requested_proof),
                key=lambda item: _PROOF_RANK.get(item, 0),
            )
        )
        requested_risk = str(decision.get("risk_level") or base.risk_level).lower()
        if requested_risk not in _RISK_RANK:
            requested_risk = base.risk_level
        risk_level = max(
            (base.risk_level, requested_risk),
            key=lambda item: _RISK_RANK.get(item, 0),
        )

        history = list(base.metadata.get("revision_history") or [])
        history.append(
            {
                "base_contract_id": base.contract_id,
                "base_version": base.version,
                "proposed_version": base.version + 1,
                "classification": classification,
                "relationship": relationship,
                "guidance": guidance[:2000],
                "proposed_by": proposed_by,
            }
        )
        metadata = dict(base.metadata)
        metadata["revision_history"] = history[-20:]
        metadata["revision"] = history[-1]
        metadata["created_by"] = proposed_by

        requested_semantic = str(
            decision.get("semantic_validation") or base.semantic_validation
        ).strip().lower()
        if requested_semantic not in {"auto", "always", "never"}:
            requested_semantic = base.semantic_validation
        decision_constraints = decision.get("execution_constraints")
        allowed_decision_constraints = {}
        if isinstance(decision_constraints, dict):
            for key in (
                "supersede_running_tool",
                "retry_superseded_tool",
                "requested_effect_classes",
            ):
                if key in decision_constraints:
                    allowed_decision_constraints[key] = decision_constraints[key]

        return OutcomeContract.compile(
            updated_goal,
            supplied={
                "schema_version": "tcode.outcome/v2",
                "goal": updated_goal,
                "goal_id": base.goal_id,
                "version": base.version + 1,
                "title": base.title,
                "intent": updated_goal,
                "risk_level": risk_level,
                "required_proof_level": proof_level,
                "success_criteria": criteria,
                "deliverables": [asdict(item) for item in base.deliverables],
                "constraints": {
                    **dict(base.execution_constraints),
                    **allowed_decision_constraints,
                    "rules": [asdict(item) for item in base.constraints],
                },
                "source_policy": dict(base.source_policy),
                "partial_result_policy": base.partial_result_policy,
                "completion_policy": base.completion_policy,
                "semantic_validation": requested_semantic,
                "max_repair_attempts": base.max_repair_attempts,
                "metadata": metadata,
            },
        )

    @classmethod
    def _compile_goal_replacement(
        cls,
        base: OutcomeContract,
        guidance: str,
        *,
        decision: dict[str, Any],
        proposed_by: str,
    ) -> OutcomeContract:
        """Compile a fresh Contract identity without inheriting the old goal's proof."""

        updated_goal = str(decision.get("updated_goal") or guidance or "").strip()
        requested_proof = str(
            decision.get("required_proof_level") or "P1"
        ).strip().upper()
        if requested_proof not in _PROOF_RANK:
            requested_proof = "P1"
        requested_risk = str(decision.get("risk_level") or "low").strip().lower()
        if requested_risk not in _RISK_RANK:
            requested_risk = "low"

        criteria = cls._sanitize_criteria(decision.get("success_criteria"))
        if not criteria:
            verifier_refs = ["semantic"]
            if _PROOF_RANK[requested_proof] >= _PROOF_RANK["P2"]:
                verifier_refs.append("tool_success")
            criteria = [
                {
                    "description": (
                        "The final result directly satisfies the replacement goal: "
                        + updated_goal[:1200]
                    ),
                    "required": True,
                    "verifier_refs": verifier_refs,
                    "parameters": {"require_all": True},
                }
            ]

        goal_seed = json.dumps(
            {
                "kind": "goal_replacement",
                "previous_goal_id": base.goal_id,
                "updated_goal": updated_goal,
                "guidance": guidance,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        new_goal_id = "goal_" + hashlib.sha256(goal_seed.encode("utf-8")).hexdigest()[:20]
        replacement = {
            "previous_goal_id": base.goal_id,
            "previous_contract_id": base.contract_id,
            "previous_contract_version": base.version,
            "reuse_policy": "candidate_unverified",
            "guidance": guidance[:2000],
        }
        return OutcomeContract.compile(
            updated_goal,
            supplied={
                "schema_version": "tcode.outcome/v2",
                "goal": updated_goal,
                "goal_id": new_goal_id,
                "version": 1,
                "title": updated_goal[:160],
                "intent": updated_goal,
                "risk_level": requested_risk,
                "required_proof_level": requested_proof,
                "success_criteria": criteria,
                "semantic_validation": "always",
                "max_repair_attempts": base.max_repair_attempts,
                "metadata": {
                    "created_by": proposed_by,
                    "goal_replacement": replacement,
                },
            },
        )

    @staticmethod
    def _fallback_criterion(
        base: OutcomeContract,
        guidance: str,
    ) -> dict[str, Any]:
        verifier_refs = ["semantic"]
        if _PROOF_RANK.get(base.required_proof_level, 0) >= _PROOF_RANK["P2"]:
            verifier_refs.append("tool_success")
        return {
            "description": (
                "The final result satisfies the user-approved runtime revision; "
                "the latest guidance takes precedence over conflicting older wording: "
                + guidance[:1200]
            ),
            "required": True,
            "verifier_refs": verifier_refs,
            "parameters": {"require_all": True},
        }

    @staticmethod
    def _sanitize_criteria(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        criteria: list[dict[str, Any]] = []
        for item in value[:8]:
            if isinstance(item, str):
                description = item.strip()
                raw: dict[str, Any] = {}
            elif isinstance(item, dict):
                raw = dict(item)
                description = str(raw.get("description") or "").strip()
            else:
                continue
            if not description:
                continue
            refs = [
                str(ref).strip()
                for ref in raw.get("verifier_refs", ["semantic"])
                if str(ref or "").strip() in _ADMITTED_REFS
            ]
            criteria.append(
                {
                    "description": description[:2000],
                    "required": bool(raw.get("required", True)),
                    "verifier_refs": refs or ["semantic"],
                    "expected_artifacts": [
                        dict(artifact)
                        for artifact in raw.get("expected_artifacts", [])
                        if isinstance(artifact, dict)
                        and str(artifact.get("path") or "").strip()
                    ][:12],
                    "parameters": dict(raw.get("parameters") or {})
                    if isinstance(raw.get("parameters"), dict)
                    else {},
                }
            )
        return criteria

    @staticmethod
    def _merge_criteria(
        base: list[dict[str, Any]],
        planned: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = [dict(item) for item in base]
        descriptions = {
            str(item.get("description") or "").strip().lower() for item in result
        }
        for item in planned:
            marker = str(item.get("description") or "").strip().lower()
            if marker and marker not in descriptions:
                result.append(dict(item))
                descriptions.add(marker)
        return result[:12]


def invalidate_superseded_message_verifications(
    messages: list[Any],
    *,
    contract_id: str,
    contract_version: int,
    superseding_contract: OutcomeContract,
) -> int:
    """Invalidate already-persisted badges that refer to an older Contract version."""

    changed = 0
    for message in messages:
        verification = getattr(message, "verification", None)
        if not isinstance(verification, dict):
            continue
        if str(verification.get("contract_id") or "") != str(contract_id or ""):
            continue
        if int(verification.get("contract_version", 0) or 0) != int(contract_version):
            continue
        next_value = dict(verification)
        next_value.update(
            {
                "verification_status": "UNVERIFIED",
                "confidence": 0.0,
                "superseded": True,
                "superseded_by_contract_version": int(superseding_contract.version),
                "superseded_by_goal_id": str(superseding_contract.goal_id),
                "superseded_by_contract_id": str(superseding_contract.contract_id),
            }
        )
        message.verification = next_value
        changed += 1
    return changed
