"""Domain-independent proposals from registered workflow capabilities."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from .clarifications import missing_required_fields_from_contract
from .input_bindings import bind_workflow_request
from .registry import WorkflowTemplateRegistry
from .router import detect_workflow_route
from .discovery import WorkflowDiscovery

@dataclass(slots=True)
class WorkflowProposalCandidate:
    """A scored workflow template candidate."""

    template_key: str
    display_name: str
    domain: str = ""
    confidence: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    request_capabilities: list[str] = field(default_factory=list)
    template_capabilities: list[str] = field(default_factory=list)
    matched_capabilities: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    route_card: dict[str, Any] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    input_payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["confidence"] = round(float(self.confidence), 3)
        data["score"] = round(float(self.score), 3)
        return data


@dataclass(slots=True)
class WorkflowProposalPlan:
    """Safe Level 3 workflow proposal; never creates a run by itself."""

    decision: str
    reason: str = ""
    template_key: str = ""
    display_name: str = ""
    confidence: float = 0.0
    input_payload: dict[str, Any] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    recommended_workflow_run_args: dict[str, Any] = field(default_factory=dict)
    matched_templates: list[WorkflowProposalCandidate] = field(default_factory=list)
    request_capabilities: list[str] = field(default_factory=list)
    composite_plan: dict[str, Any] = field(default_factory=dict)
    route_decision: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "template_key": self.template_key,
            "display_name": self.display_name,
            "confidence": round(float(self.confidence), 3),
            "input_payload": dict(self.input_payload or {}),
            "missing_required": list(self.missing_required or []),
            "recommended_workflow_run_args": dict(
                self.recommended_workflow_run_args or {}
            ),
            "matched_templates": [item.to_dict() for item in self.matched_templates],
            "request_capabilities": list(self.request_capabilities or []),
            "composite_plan": dict(self.composite_plan or {}),
            "route_decision": dict(self.route_decision or {}),
            "safety": dict(self.safety or {}),
        }



def build_workflow_proposal(
    message: str, *, composite_plan=None, resource_context=None, requested_agent_name: str = "",
    workdir: Optional[str] = None, include_disabled: bool = False, discovery: WorkflowDiscovery | None = None,
) -> WorkflowProposalPlan:
    """Format a suggestion without creating a run or guessing a domain match.

    Synchronous callers may resolve an explicit name. Free-form selection is
    performed by the shared async selector at the tool boundary, not keywords.
    Disabled templates can be listed, never proposed for execution.
    """
    route = detect_workflow_route(message, requested_agent_name=requested_agent_name, workdir=workdir)
    key = route.template_key if route.should_start else ""
    confidence = route.confidence if key else 0.0
    reason = route.reason if key else "需要依据注册目录作语义选择或明确指定模板；未创建运行。"
    if not key and discovery:
        if discovery.status == "matched":
            key, confidence = discovery.template_key, discovery.confidence
        reason = discovery.reason or reason
    safety = {"proposal_only": True, "creates_run": False,
              "requires_user_confirmation_before_workflow_run": True, "recommended_auto_continue": False}
    template = WorkflowTemplateRegistry.get(key) if key else None
    if template is None:
        plan = dict(composite_plan or {})
        explicit_temporary = "临时" in str(message or "") and "workflow" in str(message or "").lower()
        if not explicit_temporary and not bool(plan.get("level3_candidate")):
            return WorkflowProposalPlan(
                "no-workflow", reason=reason, composite_plan=plan,
                route_decision=route.to_dict(), safety=safety,
            )
        return WorkflowProposalPlan(
            "temporary_workflow_proposal",
            reason=reason or "没有匹配的固定模板，可以先生成临时 DAG 供用户编辑和确认。",
            display_name="生成临时 Workflow",
            confidence=max(0.5, float(getattr(discovery, "confidence", 0.0) or 0.0)),
            input_payload={"target_description": message},
            recommended_workflow_run_args={"goal": message, "auto_continue": False},
            composite_plan=plan, route_decision=route.to_dict(), safety=safety,
        )
    payload = bind_workflow_request(dict(route.input_payload), template.input_contract, message)
    missing = missing_required_fields_from_contract(payload, template.input_contract)
    candidate = WorkflowProposalCandidate(key, template.display_name, domain=template.domain,
        confidence=confidence, score=confidence, reasons=[reason], missing_required=missing, input_payload=payload)
    return WorkflowProposalPlan("workflow_proposal", reason=reason, template_key=key, display_name=template.display_name,
        confidence=confidence, input_payload=payload, missing_required=missing,
        recommended_workflow_run_args={"template_key": key, "input_payload": payload, "auto_continue": False},
        matched_templates=[candidate], composite_plan=dict(composite_plan or {}), route_decision=route.to_dict(), safety=safety)
