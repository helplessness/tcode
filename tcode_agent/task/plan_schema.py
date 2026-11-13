"""Structured Plan model with JSON Schema, dependency tracking, and validation.

This module provides a machine-readable Plan data model that complements the
human-readable todo/workpad system. Plans can be validated before execution,
rendered as human-readable checklists, and inspected programmatically by the
orchestrator or verification agents.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class PlanStatus(str, Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REVISED = "revised"


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"


class StepCategory(str, Enum):
    ANALYZE = "analyze"
    DESIGN = "design"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    INTEGRATE = "integrate"
    DOCUMENT = "document"
    DECIDE = "decide"


@dataclass(slots=True)
class PlanStep:
    """A single step in a structured plan.

    Each step has a unique ID, belongs to a category, can depend on other steps,
    and carries acceptance criteria that must be satisfied before the step is
    considered complete.
    """

    step_id: str
    content: str
    category: StepCategory = StepCategory.IMPLEMENT
    status: StepStatus = StepStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    estimated_effort: str = ""  # e.g. "5min", "1h", "1d"
    notes: str = ""
    assigned_to: str = ""  # worker/agent name
    artifact_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanStep":
        data = dict(payload or {})
        try:
            category = StepCategory(str(data.get("category", "implement")).strip().lower())
        except ValueError:
            category = StepCategory.IMPLEMENT
        try:
            status = StepStatus(str(data.get("status", "pending")).strip().lower())
        except ValueError:
            status = StepStatus.PENDING

        return cls(
            step_id=str(data.get("step_id", "") or "").strip() or f"step-{uuid.uuid4().hex[:8]}",
            content=str(data.get("content", "") or "").strip(),
            category=category,
            status=status,
            depends_on=[str(s).strip() for s in (data.get("depends_on") or []) if str(s).strip()],
            acceptance_criteria=[str(s).strip() for s in (data.get("acceptance_criteria") or []) if str(s).strip()],
            estimated_effort=str(data.get("estimated_effort", "") or "").strip(),
            notes=str(data.get("notes", "") or "").strip(),
            assigned_to=str(data.get("assigned_to", "") or "").strip(),
            artifact_refs=[str(s).strip() for s in (data.get("artifact_refs") or []) if str(s).strip()],
            metadata=dict(data.get("metadata", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "content": self.content,
            "category": self.category.value,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "estimated_effort": self.estimated_effort,
            "notes": self.notes,
            "assigned_to": self.assigned_to,
            "artifact_refs": list(self.artifact_refs),
            "metadata": dict(self.metadata),
        }

    def is_blocked(self, completed_step_ids: set[str]) -> bool:
        """Check if this step is blocked by incomplete dependencies."""
        if not self.depends_on:
            return False
        return not all(dep in completed_step_ids for dep in self.depends_on)

    def ready_steps(self, all_steps: list["PlanStep"]) -> list[str]:
        """Return step IDs that are unblocked by this step's completion."""
        return [
            s.step_id for s in all_steps
            if self.step_id in s.depends_on and s.status == StepStatus.PENDING
        ]


@dataclass(slots=True)
class Plan:
    """A structured, machine-readable plan for agent task execution.

    A Plan contains:
    - A goal statement
    - Ordered steps with dependencies
    - Acceptance criteria at the plan level
    - Validation rules that gate plan readiness
    - An audit trail of events (creation, review, revision, completion)
    """

    plan_id: str
    goal: str
    session_id: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    steps: list[PlanStep] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    estimated_total_effort: str = ""
    context_summary: str = ""
    validation_errors: list[str] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now().timestamp())

    # ── JSON Schema ──────────────────────────────────────────────

    JSON_SCHEMA = {
        "type": "object",
        "required": ["plan_id", "goal", "steps", "acceptance_criteria"],
        "properties": {
            "plan_id": {"type": "string", "description": "Unique plan identifier"},
            "goal": {"type": "string", "description": "One-sentence goal of the plan"},
            "status": {
                "type": "string",
                "enum": [s.value for s in PlanStatus],
                "description": "Current plan lifecycle status",
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["step_id", "content", "category"],
                    "properties": {
                        "step_id": {"type": "string"},
                        "content": {"type": "string", "minLength": 1},
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in StepCategory],
                        },
                        "status": {
                            "type": "string",
                            "enum": [s.value for s in StepStatus],
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "estimated_effort": {"type": "string"},
                        "notes": {"type": "string"},
                        "assigned_to": {"type": "string"},
                        "artifact_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "acceptance_criteria": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "estimated_total_effort": {"type": "string"},
            "context_summary": {"type": "string"},
        },
    }

    # ── Factory ──────────────────────────────────────────────────

    @classmethod
    def create(cls, *, goal: str, session_id: str = "", steps: Optional[list[PlanStep]] = None, **kwargs) -> "Plan":
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        return cls(
            plan_id=plan_id,
            goal=str(goal).strip(),
            session_id=str(session_id or "").strip(),
            steps=list(steps or []),
            **kwargs,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Plan":
        data = dict(payload or {})
        try:
            status = PlanStatus(str(data.get("status", "draft")).strip().lower())
        except ValueError:
            status = PlanStatus.DRAFT

        return cls(
            plan_id=str(data.get("plan_id", "") or "").strip() or f"plan-{uuid.uuid4().hex[:12]}",
            goal=str(data.get("goal", "") or "").strip(),
            session_id=str(data.get("session_id", "") or "").strip(),
            status=status,
            steps=[PlanStep.from_dict(s) for s in (data.get("steps") or []) if isinstance(s, dict)],
            acceptance_criteria=[str(s).strip() for s in (data.get("acceptance_criteria") or []) if str(s).strip()],
            risks=[str(s).strip() for s in (data.get("risks") or []) if str(s).strip()],
            assumptions=[str(s).strip() for s in (data.get("assumptions") or []) if str(s).strip()],
            estimated_total_effort=str(data.get("estimated_total_effort", "") or "").strip(),
            context_summary=str(data.get("context_summary", "") or "").strip(),
            validation_errors=[str(s).strip() for s in (data.get("validation_errors") or []) if str(s).strip()],
            audit_events=list(data.get("audit_events") or []),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", datetime.now().timestamp()) or datetime.now().timestamp()),
            updated_at=float(data.get("updated_at", datetime.now().timestamp()) or datetime.now().timestamp()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "session_id": self.session_id,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "acceptance_criteria": list(self.acceptance_criteria),
            "risks": list(self.risks),
            "assumptions": list(self.assumptions),
            "estimated_total_effort": self.estimated_total_effort,
            "context_summary": self.context_summary,
            "validation_errors": list(self.validation_errors),
            "audit_events": list(self.audit_events),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    # ── Validation ───────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Validate the plan and return a list of issues.

        Returns an empty list if the plan passes all validation gates.
        """
        errors: list[str] = []

        # Gate 1: Goal must be non-empty
        if not self.goal.strip():
            errors.append("plan.goal: goal must not be empty")

        # Gate 2: At least one step
        if not self.steps:
            errors.append("plan.steps: at least one step is required")

        # Gate 3: At least one acceptance criterion at plan level
        if not self.acceptance_criteria:
            errors.append("plan.acceptance_criteria: at least one acceptance criterion is required")

        # Gate 4: Every step has content
        for step in self.steps:
            if not step.content.strip():
                errors.append(f"plan.steps[{step.step_id}]: content must not be empty")

        # Gate 5: Dependency integrity — all depends_on references must exist
        step_ids = {s.step_id for s in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    errors.append(
                        f"plan.steps[{step.step_id}]: depends_on '{dep}' "
                        f"references a step that does not exist"
                    )

        # Gate 6: No circular dependencies
        cycle = self._detect_cycle()
        if cycle:
            errors.append(f"plan.steps: circular dependency detected: {' → '.join(cycle)}")

        # Gate 7: Step-level acceptance criteria exist for implement/verify steps
        for step in self.steps:
            if step.category in {StepCategory.IMPLEMENT, StepCategory.VERIFY}:
                if not step.acceptance_criteria:
                    errors.append(
                        f"plan.steps[{step.step_id}]: {step.category.value} step "
                        f"should have at least one acceptance criterion"
                    )

        # Gate 8: Risk awareness — large plans need risk documentation
        if len(self.steps) >= 5 and not self.risks:
            errors.append("plan.risks: plans with 5+ steps should document at least one risk")

        self.validation_errors = errors
        return errors

    def _detect_cycle(self) -> list[str]:
        """Detect circular dependencies using DFS. Returns cycle path or empty list."""
        step_ids = {s.step_id for s in self.steps}
        dep_map = {s.step_id: s.depends_on for s in self.steps}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {sid: WHITE for sid in step_ids}
        parent: dict[str, Optional[str]] = {sid: None for sid in step_ids}

        def dfs(node: str) -> Optional[list[str]]:
            color[node] = GRAY
            for neighbor in dep_map.get(node, []):
                if neighbor not in color:
                    continue
                if color[neighbor] == GRAY:
                    # Found back edge → cycle
                    cycle = [neighbor, node]
                    cur = node
                    while parent.get(cur) and parent[cur] != neighbor:
                        cur = parent[cur] or ""
                        if cur:
                            cycle.append(cur)
                    cycle.append(neighbor)
                    cycle.reverse()
                    return cycle
                if color[neighbor] == WHITE:
                    parent[neighbor] = node
                    result = dfs(neighbor)
                    if result:
                        return result
            color[node] = BLACK
            return None

        for sid in step_ids:
            if color[sid] == WHITE:
                result = dfs(sid)
                if result:
                    return result
        return []

    def is_valid(self) -> bool:
        """Return True if the plan passes all validation gates."""
        return len(self.validate()) == 0

    # ── Lifecycle ────────────────────────────────────────────────

    def touch(self) -> None:
        self.updated_at = datetime.now().timestamp()

    def log_event(self, kind: str, summary: str = "", payload: Optional[dict[str, Any]] = None) -> None:
        self.audit_events.append({
            "kind": str(kind).strip(),
            "summary": str(summary or "").strip(),
            "payload": dict(payload or {}),
            "timestamp": datetime.now().timestamp(),
        })
        if len(self.audit_events) > 200:
            self.audit_events = self.audit_events[-200:]
        self.touch()

    def mark_reviewed(self) -> None:
        self.status = PlanStatus.REVIEWED
        self.log_event("plan_reviewed", "Plan passed validation review")

    def mark_approved(self) -> None:
        self.status = PlanStatus.APPROVED
        self.log_event("plan_approved", "Plan approved for execution")

    def mark_executing(self) -> None:
        self.status = PlanStatus.EXECUTING
        self.log_event("plan_executing", "Plan execution started")

    def mark_completed(self) -> None:
        self.status = PlanStatus.COMPLETED
        self.log_event("plan_completed", "All steps completed")

    # ── Step helpers ─────────────────────────────────────────────

    def completed_step_ids(self) -> set[str]:
        return {s.step_id for s in self.steps if s.status == StepStatus.COMPLETED}

    def next_ready_steps(self) -> list[PlanStep]:
        """Return pending steps whose dependencies are all satisfied."""
        completed = self.completed_step_ids()
        return [
            s for s in self.steps
            if s.status == StepStatus.PENDING and not s.is_blocked(completed)
        ]

    def progress(self) -> dict[str, Any]:
        total = len(self.steps)
        if total == 0:
            return {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "failed": 0, "percent": 0}
        counts = {
            "total": total,
            "completed": sum(1 for s in self.steps if s.status == StepStatus.COMPLETED),
            "in_progress": sum(1 for s in self.steps if s.status == StepStatus.IN_PROGRESS),
            "pending": sum(1 for s in self.steps if s.status == StepStatus.PENDING),
            "failed": sum(1 for s in self.steps if s.status == StepStatus.FAILED),
            "blocked": sum(1 for s in self.steps if s.status == StepStatus.BLOCKED),
        }
        counts["percent"] = round(counts["completed"] / total * 100, 1) if total > 0 else 0
        return counts

    # ── Rendering ────────────────────────────────────────────────

    def render_human(self, *, include_schema: bool = False) -> str:
        """Render the plan as a human-readable workpad section."""
        lines = [
            "## Plan",
            "",
            f"**Goal**: {self.goal}",
            f"**Status**: {self.status.value}",
            "",
        ]

        if self.context_summary:
            lines.append(f"**Context**: {self.context_summary}")
            lines.append("")

        if self.assumptions:
            lines.append("**Assumptions**:")
            for a in self.assumptions:
                lines.append(f"- {a}")
            lines.append("")

        # Render steps with dependency annotations
        lines.append("### Steps")
        lines.append("")
        for i, step in enumerate(self.steps, 1):
            status_icon = {
                StepStatus.PENDING: "⬜",
                StepStatus.IN_PROGRESS: "🔄",
                StepStatus.COMPLETED: "✅",
                StepStatus.SKIPPED: "⏭️",
                StepStatus.FAILED: "❌",
                StepStatus.BLOCKED: "🚫",
            }.get(step.status, "⬜")

            dep_note = ""
            if step.depends_on:
                dep_labels = [d for d in step.depends_on]
                dep_note = f" (depends on: {', '.join(dep_labels)})"

            lines.append(f"{status_icon} **{i}. {step.content}** [{step.category.value}]{dep_note}")
            if step.estimated_effort:
                lines.append(f"   ⏱️ Effort: {step.estimated_effort}")
            if step.assigned_to:
                lines.append(f"   👤 Assigned: {step.assigned_to}")
            if step.acceptance_criteria:
                for ac in step.acceptance_criteria:
                    lines.append(f"   - [ ] {ac}")
            if step.notes:
                lines.append(f"   📝 {step.notes}")
            lines.append("")

        # Render plan-level acceptance criteria
        if self.acceptance_criteria:
            lines.append("### Acceptance Criteria")
            lines.append("")
            for ac in self.acceptance_criteria:
                lines.append(f"- [ ] {ac}")
            lines.append("")

        if self.risks:
            lines.append("### Risks")
            lines.append("")
            for r in self.risks:
                lines.append(f"- ⚠️ {r}")
            lines.append("")

        if self.estimated_total_effort:
            lines.append(f"**Estimated Total Effort**: {self.estimated_total_effort}")
            lines.append("")

        if include_schema:
            lines.append("---")
            lines.append("### Machine-Readable Plan (JSON)")
            lines.append("```json")
            lines.append(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
            lines.append("```")

        return "\n".join(lines)

    def render_todos(self) -> list[dict[str, str]]:
        """Convert plan steps to todo items for the TodoManager."""
        todos: list[dict[str, str]] = []
        for step in self.steps:
            status_map = {
                StepStatus.PENDING: "pending",
                StepStatus.IN_PROGRESS: "in_progress",
                StepStatus.COMPLETED: "completed",
                StepStatus.FAILED: "cancelled",
                StepStatus.SKIPPED: "cancelled",
                StepStatus.BLOCKED: "pending",
            }
            content = step.content
            if step.assigned_to:
                content = f"[{step.assigned_to}] {content}"
            todos.append({
                "content": content,
                "status": status_map.get(step.status, "pending"),
            })
        return todos

    def sync_from_todos(self, todos: list[dict[str, str]]) -> None:
        """Update step statuses from a todo list (reverse mapping)."""
        status_map = {
            "pending": StepStatus.PENDING,
            "in_progress": StepStatus.IN_PROGRESS,
            "completed": StepStatus.COMPLETED,
            "cancelled": StepStatus.SKIPPED,
        }
        for i, todo in enumerate(todos):
            if i < len(self.steps):
                new_status = status_map.get(
                    str(todo.get("status", "pending")).strip().lower(),
                    StepStatus.PENDING,
                )
                self.steps[i].status = new_status
        self.touch()