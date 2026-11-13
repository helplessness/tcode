"""Plan mode tools - structured planning, validation, and review.

Integrates with the structured Plan model (plan_schema.py) so plans are both
human-readable (via todo/workpad) and machine-readable (via JSON Schema).
"""

import json
from typing import Any

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...task.plan_schema import Plan, PlanStep, PlanStatus, StepCategory, StepStatus
from ...task.plan_manager import PlanManager
from ...task.plan_freshness import (
    apply_freshness_result,
    current_task_fingerprint,
    stamp_plan_source,
    update_task_fingerprint,
    validate_plan_freshness,
)
from ...session.manager import SessionManager
from .todowrite import TodoManager


class PlanEnterParameters(ParameterSchema):
    goal: str | None = None
    context_summary: str | None = None


class PlanExitParameters(ParameterSchema):
    summary: str | None = None


class PlanValidateParameters(ParameterSchema):
    plan_json: str | None = None


class PlanReviewParameters(ParameterSchema):
    pass


def _build_plan_from_todos(
    session_id: str,
    goal: str,
    context_summary: str = "",
    workdir: str | None = None,
) -> Plan:
    """Build or update a structured Plan from existing todo items."""
    existing = PlanManager.get(session_id, workdir=workdir)
    todos = TodoManager.get_todos(session_id, workdir=workdir)
    session = SessionManager.get_all_sync().get(session_id)
    metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
    fingerprint = current_task_fingerprint(
        metadata,
        todos=todos,
        fallback_goal=goal or str(metadata.get("task_goal", "") or ""),
    )

    if existing and existing.steps:
        existing.sync_from_todos(todos)
        if goal:
            existing.goal = str(goal).strip()
        if context_summary:
            existing.context_summary = str(context_summary).strip()
        stamp_plan_source(existing, fingerprint)
        existing.touch()
        return existing

    steps: list[PlanStep] = []
    for i, todo in enumerate(todos):
        content = str(todo.get("content", "")).strip()
        if not content:
            continue
        todo_status = str(todo.get("status", "pending")).strip().lower()
        step_status = {
            "pending": StepStatus.PENDING,
            "in_progress": StepStatus.IN_PROGRESS,
            "completed": StepStatus.COMPLETED,
            "cancelled": StepStatus.SKIPPED,
        }.get(todo_status, StepStatus.PENDING)

        # Guess category from content keywords
        content_lower = content.lower()
        if any(kw in content_lower for kw in ("analyze", "research", "read", "search", "inspect", "understand", "review", "explore")):
            category = StepCategory.ANALYZE
        elif any(kw in content_lower for kw in ("design", "plan", "architect", "schema", "structure")):
            category = StepCategory.DESIGN
        elif any(kw in content_lower for kw in ("test", "verify", "validate", "check", "confirm", "ensure")):
            category = StepCategory.VERIFY
        elif any(kw in content_lower for kw in ("implement", "code", "build", "create", "write", "develop", "fix", "modify")):
            category = StepCategory.IMPLEMENT
        elif any(kw in content_lower for kw in ("integrate", "deploy", "merge", "connect")):
            category = StepCategory.INTEGRATE
        elif any(kw in content_lower for kw in ("document", "report", "summarize", "write up")):
            category = StepCategory.DOCUMENT
        else:
            category = StepCategory.IMPLEMENT

        steps.append(PlanStep(
            step_id=f"step-{i + 1:02d}",
            content=content,
            category=category,
            status=step_status,
        ))

    plan = Plan.create(
        goal=goal or "Task plan",
        session_id=session_id,
        steps=steps,
        context_summary=context_summary or "",
    )
    stamp_plan_source(plan, fingerprint)

    PlanManager.save_sync(session_id, plan)
    return plan


async def _current_fingerprint_for_session(
    session_id: str,
    *,
    todos: list[dict[str, Any]],
    fallback_goal: str = "",
) -> dict[str, Any]:
    session = await SessionManager.get(session_id)
    metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
    return current_task_fingerprint(metadata, todos=todos, fallback_goal=fallback_goal)


def _validate_plan_against_current_task(
    plan: Plan,
    fingerprint: dict[str, Any],
) -> list[str]:
    structural_errors = PlanManager.validate_plan(plan)
    freshness_errors = validate_plan_freshness(plan, fingerprint)
    errors = structural_errors + freshness_errors
    apply_freshness_result(plan, freshness_errors)
    if errors:
        plan.validation_errors = errors
    return errors


class PlanEnterTool(Tool):
    @property
    def id(self) -> str:
        return "plan_enter"

    @property
    def description(self) -> str:
        return """Enter planning mode for the current session.

Planning mode is read-only and is intended for:
- exploring context (read, grep, glob, webfetch)
- drafting a structured plan with todo items
- inspecting the task graph with task_graph_read

When entering plan mode, existing todo items are automatically converted
into a structured Plan with dependency tracking and validation gates.

Use `plan_validate` to check the plan before exiting, and `plan_exit`
when the plan is complete and validated."""

    @property
    def parameters(self) -> type:
        return PlanEnterParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = PlanEnterParameters(**args)
        callback = (context.extra or {}).get("__set_plan_mode__")
        if not callback:
            return ToolResult(
                title="plan_enter",
                output="",
                error="Plan mode callback is unavailable.",
            )

        result = callback(
            enabled=True,
            goal=(parsed.goal or "").strip(),
            source_agent=context.agent,
        )
        if hasattr(result, "__await__"):
            result = await result

        workdir = (context.extra or {}).get("workdir", "") or None
        goal = (parsed.goal or "").strip()
        todos = TodoManager.get_todos(context.session_id, workdir=workdir)
        session = await SessionManager.get(context.session_id)
        if session:
            metadata = dict(getattr(session, "metadata", {}) or {})
            updated_metadata, _fingerprint = update_task_fingerprint(
                metadata,
                goal=goal or str(metadata.get("task_goal", "") or ""),
                todos=todos,
            )
            await SessionManager.update(context.session_id, metadata=updated_metadata)

        # Build structured plan from existing todos
        plan = _build_plan_from_todos(
            session_id=context.session_id,
            goal=goal,
            context_summary=(parsed.context_summary or "").strip(),
            workdir=workdir,
        )
        PlanManager.save_sync(context.session_id, plan)

        # Show validation status
        fingerprint = await _current_fingerprint_for_session(
            context.session_id,
            todos=todos,
            fallback_goal=goal or plan.goal,
        )
        errors = _validate_plan_against_current_task(plan, fingerprint)
        PlanManager.save_sync(context.session_id, plan)
        validation_status = "PASSED" if not errors else f"HAS {len(errors)} ISSUE(S)"

        todos = TodoManager.get_todos(context.session_id, workdir=workdir)
        progress = plan.progress()

        lines = [
            "Plan mode is now active.",
            f"Current mode: {result.get('mode', 'plan') if isinstance(result, dict) else 'plan'}",
            f"Plan ID: {plan.plan_id}",
            f"Validation: {validation_status}",
            "",
            "Allowed workflow:",
            "1. Explore context with read/grep/glob/webfetch tools",
            "2. Update the plan using the `todo` tool (add/modify/reorder steps)",
            "3. Add acceptance criteria for each implementation/verification step",
            "4. Run `plan_validate` to check the plan before execution",
            "5. Set step dependencies with `plan_edit` to ensure correct ordering",
            "6. Call `plan_exit` when the plan is complete and validated",
            "",
        ]

        if parsed.goal:
            lines.append(f"Goal: {parsed.goal.strip()}")
        if todos:
            lines.append(f"Todo items: {len(todos)} — {progress['completed']}/{progress['total']} completed")
        if errors:
            lines.append("")
            lines.append("⚠️ Validation issues found:")
            for err in errors[:5]:
                lines.append(f"  - {err}")
            if len(errors) > 5:
                lines.append(f"  ... and {len(errors) - 5} more")
            lines.append("Fix these before calling plan_exit.")
        else:
            lines.append("✅ Plan structure is valid. Ready for review and execution.")

        return ToolResult(
            title="plan_enter",
            output="\n".join(lines),
            metadata={
                **(result if isinstance(result, dict) else {}),
                "plan_id": plan.plan_id,
                "plan_validation_errors": errors,
                "plan_progress": progress,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


class PlanExitTool(Tool):
    @property
    def id(self) -> str:
        return "plan_exit"

    @property
    def description(self) -> str:
        return """Exit planning mode and return to normal execution mode.

Before exiting, the plan is validated. If validation errors exist, you
will be warned but can still exit. Use plan_validate first to check.

The structured plan is persisted to session metadata and will guide
execution order and dependency resolution."""

    @property
    def parameters(self) -> type:
        return PlanExitParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = PlanExitParameters(**args)
        callback = (context.extra or {}).get("__set_plan_mode__")
        if not callback:
            return ToolResult(
                title="plan_exit",
                output="",
                error="Plan mode callback is unavailable.",
            )

        result = callback(
            enabled=False,
            reason=(parsed.summary or "").strip(),
            source_agent=context.agent,
        )
        if hasattr(result, "__await__"):
            result = await result

        workdir = (context.extra or {}).get("workdir", "") or None
        plan = PlanManager.get(context.session_id, workdir=workdir)
        todos = TodoManager.get_todos(context.session_id, workdir=workdir)

        # Validate before exit
        validation_errors: list[str] = []
        if plan is not None:
            plan.sync_from_todos(todos)
            fingerprint = await _current_fingerprint_for_session(
                context.session_id,
                todos=todos,
                fallback_goal=plan.goal,
            )
            validation_errors = _validate_plan_against_current_task(plan, fingerprint)
            if not validation_errors:
                plan.mark_reviewed()
            PlanManager.save_sync(context.session_id, plan)

        remaining = [
            item
            for item in todos
            if str(item.get("status", "pending")).strip().lower()
            in {"pending", "in_progress"}
        ]

        lines = [
            "Plan mode is now inactive.",
            f"Current mode: {result.get('mode', 'execute') if isinstance(result, dict) else 'execute'}",
        ]

        if plan is not None:
            lines.append(f"Plan ID: {plan.plan_id}")
            progress = plan.progress()
            lines.append(f"Progress: {progress['completed']}/{progress['total']} steps complete ({progress['percent']}%)")

        if parsed.summary:
            lines.append(f"Planning summary: {parsed.summary.strip()}")

        if validation_errors:
            lines.append("")
            lines.append("⚠️ Plan validation issues (should be addressed during execution):")
            for err in validation_errors[:5]:
                lines.append(f"  - {err}")
        elif plan is not None and plan.is_valid():
            lines.append("✅ Plan validated successfully. Ready for execution.")

        if remaining:
            lines.append(f"Open todo items: {len(remaining)}")
            if plan is not None:
                ready = plan.next_ready_steps()
                if ready:
                    lines.append(f"Next ready steps: {', '.join(s.step_id for s in ready[:3])}")
        else:
            lines.append("All todo items are complete or no todo list has been recorded.")

        return ToolResult(
            title="plan_exit",
            output="\n".join(lines),
            metadata={
                **(result if isinstance(result, dict) else {}),
                "plan_id": plan.plan_id if plan else None,
                "plan_validation_errors": validation_errors,
                "plan_progress": plan.progress() if plan else None,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


class PlanValidateTool(Tool):
    """Validate the current structured plan against the JSON Schema and gates."""

    @property
    def id(self) -> str:
        return "plan_validate"

    @property
    def description(self) -> str:
        return """Validate the current session plan against the structured plan schema.

Checks:
- Goal is non-empty
- At least one step exists
- At least one acceptance criterion at plan level
- All step dependency references are valid (no dangling refs)
- No circular dependencies between steps
- Implementation/verification steps have acceptance criteria
- Plans with 5+ steps document at least one risk

Returns PASS or a list of specific issues to fix."""

    @property
    def parameters(self) -> type:
        return PlanValidateParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = PlanValidateParameters(**args)
        workdir = (context.extra or {}).get("workdir", "") or None

        plan: Plan | None = None

        # If plan_json is provided, validate external plan
        if parsed.plan_json and str(parsed.plan_json).strip():
            try:
                raw = json.loads(str(parsed.plan_json).strip())
                plan = Plan.from_dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                return ToolResult(
                    title="plan_validate",
                    output="",
                    error=f"Invalid plan JSON: {e}",
                )

        # Otherwise validate the session plan
        if plan is None:
            plan = PlanManager.get(context.session_id, workdir=workdir)
            if plan is None:
                return ToolResult(
                    title="plan_validate",
                    output="No plan found for this session. Use plan_enter to create one.",
                    metadata={"valid": False, "errors": ["No plan exists for this session"]},
                )

        # Sync from todos if available
        todos = TodoManager.get_todos(context.session_id, workdir=workdir)
        if todos:
            plan.sync_from_todos(todos)

        fingerprint = await _current_fingerprint_for_session(
            context.session_id,
            todos=todos,
            fallback_goal=plan.goal,
        )
        errors = _validate_plan_against_current_task(plan, fingerprint)
        PlanManager.save_sync(context.session_id, plan)

        if not errors:
            progress = plan.progress()
            ready = plan.next_ready_steps()
            ready_text = ", ".join(s.step_id for s in ready[:5]) if ready else "none"

            return ToolResult(
                title="plan_validate",
                output=(
                    "✅ Plan validation PASSED\n"
                    f"Plan ID: {plan.plan_id}\n"
                    f"Steps: {progress['total']} total, "
                    f"{progress['completed']} completed ({progress['percent']}%)\n"
                    f"Next ready: {ready_text}\n"
                    f"Status: {plan.status.value}"
                ),
                metadata={
                    "valid": True,
                    "errors": [],
                    "plan_id": plan.plan_id,
                    "progress": progress,
                    "next_ready_steps": [s.step_id for s in ready],
                    "plan_json": plan.to_dict(),
                },
            )

        return ToolResult(
            title="plan_validate",
            output=(
                f"❌ Plan validation FAILED with {len(errors)} issue(s):\n\n"
                + "\n".join(f"  {i + 1}. {err}" for i, err in enumerate(errors))
                + "\n\nFix these issues before calling plan_exit."
            ),
            metadata={
                "valid": False,
                "errors": errors,
                "plan_id": plan.plan_id,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


class PlanReviewTool(Tool):
    """Independent review of a plan by a subagent or critic."""

    @property
    def id(self) -> str:
        return "plan_review"

    @property
    def description(self) -> str:
        return """Request an independent review of the current plan.

Launches a critic subagent to review the plan for:
- Completeness: are all necessary steps included?
- Dependency correctness: is the ordering logical?
- Risk coverage: are risks and mitigations identified?
- Acceptance criteria quality: are criteria measurable and sufficient?

Returns a structured review with PASS/REVISE decision and specific recommendations."""

    @property
    def parameters(self) -> type:
        return PlanReviewParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        _parsed = PlanReviewParameters(**args)
        workdir = (context.extra or {}).get("workdir", "") or None

        plan = PlanManager.get(context.session_id, workdir=workdir)
        if plan is None:
            return ToolResult(
                title="plan_review",
                output="No plan found for this session. Use plan_enter to create one.",
                metadata={"reviewed": False, "plan_id": None},
            )

        todos = TodoManager.get_todos(context.session_id, workdir=workdir)
        if todos:
            plan.sync_from_todos(todos)

        # Run structural validation first
        fingerprint = await _current_fingerprint_for_session(
            context.session_id,
            todos=todos,
            fallback_goal=plan.goal,
        )
        errors = _validate_plan_against_current_task(plan, fingerprint)

        # Build the review — in production, this would launch a critic subagent
        # For now, provide a deterministic structural review
        review_items: list[str] = []
        passed = 0
        total = 6

        # Check 1: Goal clarity
        if len(plan.goal.split()) >= 3:
            passed += 1
            review_items.append("✅ Goal is specific and actionable")
        else:
            review_items.append("⚠️ Goal is too brief — consider adding more context")

        # Check 2: Step count
        if len(plan.steps) >= 2:
            passed += 1
            review_items.append(f"✅ {len(plan.steps)} steps defined")
        else:
            review_items.append("⚠️ Only 1 step — consider breaking down further")

        # Check 3: Categories
        categories = {s.category for s in plan.steps}
        if len(categories) >= 2:
            passed += 1
            review_items.append(f"✅ Multiple step categories used: {', '.join(c.value for c in categories)}")
        else:
            review_items.append("⚠️ All steps are the same category — consider mixing analyze/design/verify")

        # Check 4: Acceptance criteria
        ac_count = sum(len(s.acceptance_criteria) for s in plan.steps)
        if ac_count >= len(plan.steps):
            passed += 1
            review_items.append(f"✅ {ac_count} step-level acceptance criteria across {len(plan.steps)} steps")
        else:
            review_items.append("⚠️ Some steps are missing acceptance criteria")

        # Check 5: Dependencies
        dep_count = sum(len(s.depends_on) for s in plan.steps)
        if dep_count > 0:
            passed += 1
            review_items.append(f"✅ Dependencies defined between steps ({dep_count} edges)")
        elif len(plan.steps) >= 3:
            review_items.append("⚠️ No step dependencies defined — steps may run in wrong order")
        else:
            passed += 1
            review_items.append("✅ Dependencies not needed for simple plans")

        # Check 6: Risk documentation
        if plan.risks:
            passed += 1
            review_items.append(f"✅ {len(plan.risks)} risks documented")
        elif len(plan.steps) < 5:
            passed += 1
            review_items.append("✅ Risk documentation may not be needed for small plans")
        else:
            review_items.append("⚠️ No risks documented for a plan with 5+ steps")

        decision = "PASS" if passed >= total * 0.7 and not errors else "REVISE"

        plan.log_event(
            "plan_reviewed",
            summary=f"Review: {decision} ({passed}/{total} checks passed)",
            payload={"checks_passed": passed, "checks_total": total, "decision": decision},
        )
        PlanManager.save_sync(context.session_id, plan)

        output_lines = [
            f"Plan Review: {decision}",
            f"Score: {passed}/{total} checks passed",
            f"Plan ID: {plan.plan_id}",
            f"Goal: {plan.goal}",
            "",
            *review_items,
            "",
        ]

        if errors:
            output_lines.append("Structural validation errors:")
            for err in errors:
                output_lines.append(f"  ❌ {err}")
            output_lines.append("")

        if decision == "PASS":
            output_lines.append("✅ Plan is ready for execution. Use plan_exit to proceed.")
        else:
            output_lines.append("⚠️ Plan needs revision. Address the items above before calling plan_exit.")

        return ToolResult(
            title="plan_review",
            output="\n".join(output_lines),
            metadata={
                "reviewed": True,
                "plan_id": plan.plan_id,
                "decision": decision,
                "checks_passed": passed,
                "checks_total": total,
                "validation_errors": errors,
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())