"""Transport-independent workflow admission for native Web and AG-UI.

Keep discovery, applicability and run creation here, not in a frontend adapter.
The existing workflow executor owns stages, tools, retries and acceptance.
Imports of app are lazy while the legacy executor is being modularized.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import import_module
from typing import Any


async def discover_workflow(*, workdir, session_id, message, requested_agent_name="", resource_context=None):
    runtime = import_module("tcode_agent.server.app")
    from ..runtime.workspace_registry import bind_plugin_workspace

    bind_plugin_workspace(workdir)
    ready = await runtime.wait_for_plugin_warmup(
        workdir, timeout=runtime.plugin_request_wait_seconds(),
    )
    decision = runtime.detect_workflow_route(
        message, resource_context=resource_context,
        requested_agent_name=requested_agent_name, workdir=workdir,
    )
    if ready:
        decision = await runtime._refine_workflow_route_with_model(
            workdir=workdir, session_id=session_id, message=message,
            requested_agent_name=requested_agent_name,
            heuristic_decision=decision, resource_context=resource_context,
        )
    else:
        decision.should_start = False
        decision.signals["workflow_discovery"] = {
            "status": "catalog_loading", "coverage_complete": False,
        }
    return runtime.guard_protein_design_brainstorming_route(
        decision, requested_agent_name, workdir=workdir, message=message,
    )


def resume_workflow_route(*, decision, session_id, workdir, message, requested_agent_name=""):
    runtime = import_module("tcode_agent.server.app")

    active = runtime._active_workflow_run_for_session(session_id=session_id, workdir=workdir)
    if (active is None or decision.should_start or decision.intent_family == "brainstorm"
            or not runtime.should_auto_route_workflow_follow_up(message, active)):
        return decision
    payload = dict(decision.input_payload or {})
    payload.pop("target_description", None)  # continuation is control, not a new scientific task
    snapshot = (active.metadata or {}).get("template_snapshot") or {}
    contract = snapshot.get("input_contract") or {}
    fields = {
        str(item.get("field") or item.get("name") or item.get("key") or "")
        if isinstance(item, dict) else str(item or "")
        for item in [*list(contract.get("required") or []), *list(contract.get("optional") or [])]
    }
    if "target_description" in fields and not str((active.input_payload or {}).get("target_description") or "").strip():
        payload["target_description"] = str(message or "").strip()[:2000]
    return replace(
        decision, should_start=True, intent=active.template_key, template_key=active.template_key,
        reason=f"继续未完成 workflow run {active.run_id}", input_payload=payload,
        recommended_agent=runtime._workflow_recommended_primary_agent(active, fallback=requested_agent_name or "build"),
        should_delegate_to_recommended_agent=False,
    )


@dataclass
class WorkflowAdmission:
    decision: Any
    run: Any = None
    declined_decision: Any = None


async def prepare_workflow(*, session_id, workdir, message, decision, agent,
                           owner_kwargs, question_handler):
    """One admission policy for every native harness transport; no silent fallback on error."""
    runtime = import_module("tcode_agent.server.app")

    runtime._inject_workflow_discovery_instruction(agent, decision=decision)
    if not decision.should_start:
        return WorkflowAdmission(decision)
    force_new_run = bool((decision.signals or {}).get("force_new_run"))
    existing = None if force_new_run else runtime._active_workflow_run_for_session(
        session_id=session_id, workdir=workdir, template_key=decision.template_key,
    )
    if existing is None:
        template = runtime.WorkflowTemplateRegistry.get(decision.template_key)
        if template is None:
            raise ValueError(f"workflow template not found: {decision.template_key}")
        assessment = await runtime.assess_workflow_applicability(
            template, user_request=str(message or "").strip(),
            input_payload=dict(decision.input_payload or {}), provider=getattr(agent, "provider", None),
        )
        applicability = assessment.to_dict()
    else:
        applicability = dict((existing.metadata or {}).get("applicability") or {})
    source = ""
    if existing is None and (applicability.get("requires_confirmation")
                             or runtime._workflow_auto_route_needs_confirmation(decision)
                             or callable(getattr(agent, '_goal_checkpoint_callback', None))):
        confirmed = await runtime._confirm_workflow_auto_route(
            session_id=session_id, workdir=workdir, decision=decision,
            question_handler=question_handler, applicability_assessment=applicability,
        )
        if not confirmed:
            declined = replace(decision, should_start=False, intent="protein_domain_task",
                               intent_family="domain_task", missing_required=[])
            return WorkflowAdmission(replace(declined, template_key=""), declined_decision=declined)
        source = "user_override" if applicability.get("requires_confirmation") else "user_start_confirmation"
    run = runtime._select_or_create_auto_workflow_run(
        session_id=session_id, workdir=workdir, decision=decision, message=message,
        owner_kwargs=owner_kwargs, applicability_assessment=applicability,
        applicability_confirmed=bool(source), applicability_confirmation_source=source,
    )
    runtime.ServerState.sessions[session_id]["active_workflow_run_id"] = run.run_id
    return WorkflowAdmission(decision, run=run)
