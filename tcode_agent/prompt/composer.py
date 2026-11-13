"""Modular prompt composer with capability-aware sections and output validation.

Extends the system prompt assembly with:
- Plan context injection: when a structured plan exists, inject plan summary
- Capability-aware sections: add guidance based on available tools
- Workpad format validation: check agent output follows workpad conventions
- Execution profile modifiers: adapt prompt tone/verbosity to profile
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..runtime.execution_profile import get_execution_profile, normalize_execution_profile


class PromptComposer:
    """Composes modular prompt sections based on runtime context."""

    # ── Workpad validation ───────────────────────────────────────

    WORKPAD_REQUIRED_SECTIONS = [
        (r"##\s+Plan", "Plan section"),
        (r"###\s+Acceptance\s+Criteria", "Acceptance Criteria subsection"),
        (r"###\s+Validation", "Validation subsection"),
    ]

    WORKPAD_RECOMMENDED_SECTIONS = [
        (r"###\s+Notes", "Notes subsection"),
        (r"###\s+Confusions", "Confusions subsection (if applicable)"),
    ]

    @classmethod
    def validate_workpad_output(cls, content: str) -> dict[str, Any]:
        """Validate that agent output matches the workpad template convention.

        Returns a dict with:
        - valid: bool
        - missing_sections: list of missing required sections
        - missing_recommended: list of missing recommended sections
        - score: float 0-1
        """
        if not content or not content.strip():
            return {
                "valid": False,
                "missing_sections": ["No output content"],
                "missing_recommended": [],
                "score": 0.0,
            }

        missing_required: list[str] = []
        for pattern, label in cls.WORKPAD_REQUIRED_SECTIONS:
            if not re.search(pattern, content, re.IGNORECASE):
                missing_required.append(label)

        missing_recommended: list[str] = []
        for pattern, label in cls.WORKPAD_RECOMMENDED_SECTIONS:
            if not re.search(pattern, content, re.IGNORECASE):
                missing_recommended.append(label)

        total_checks = len(cls.WORKPAD_REQUIRED_SECTIONS) + len(cls.WORKPAD_RECOMMENDED_SECTIONS)
        passed = total_checks - len(missing_required) - len(missing_recommended)
        score = round(passed / total_checks, 2) if total_checks > 0 else 1.0

        return {
            "valid": len(missing_required) == 0,
            "missing_sections": missing_required,
            "missing_recommended": missing_recommended,
            "score": score,
        }

    @classmethod
    def workpad_validation_prompt(cls) -> str:
        """Return a prompt section that instructs the agent about workpad format."""
        return """# Workpad Output Format

When creating or updating a workpad comment, use this exact structure:

```
## Plan

- [ ] 1. Parent task
  - [ ] 1.1 Child task

### Acceptance Criteria

- [ ] Criterion 1

### Validation

- [ ] targeted tests: `<command>`

### Notes

- <short progress note>

### Confusions

- <only include when something was confusing>
```

The Plan section MUST exist. Acceptance Criteria and Validation MUST exist.
Notes and Confusions are recommended but optional."""

    # ── Plan context injection ────────────────────────────────────

    @classmethod
    def plan_context_section(
        cls,
        plan_summary: str,
        plan_validation: list[str],
        *,
        plan_stale: bool = False,
    ) -> str:
        """Build a prompt section with structured plan context."""
        lines = ["# Structured Plan Context", ""]
        if plan_stale:
            lines.extend([
                "A structured execution plan exists for this session, but it is stale relative to the current task/todo state.",
                "Do not execute it as authoritative. Re-enter planning or rewrite the todo list before continuing execution.",
            ])
        else:
            lines.extend([
                "A structured execution plan exists for this session. Follow the plan below.",
                "Use the `task_graph_read` tool to inspect dependencies between steps.",
            ])
        lines.extend(["", plan_summary])

        if plan_validation:
            lines.append("")
            lines.append("## Plan Validation Notes")
            for err in plan_validation:
                lines.append(f"- ⚠️ {err}")

        return "\n".join(lines)

    # ── Capability-aware sections ─────────────────────────────────

    @classmethod
    def mcp_capability_section(cls, mcp_tools: list[str]) -> str:
        """Build a prompt section describing available MCP capabilities."""
        if not mcp_tools:
            return ""

        tool_list = "\n".join(f"- `{t}`" for t in sorted(mcp_tools)[:15])
        if len(mcp_tools) > 15:
            tool_list += f"\n- ... and {len(mcp_tools) - 15} more"

        return (
            "# Available MCP Capabilities\n\n"
            "The following remote MCP tools are available. Use them for domain-specific "
            "computation instead of local bash commands when applicable:\n\n"
            f"{tool_list}\n"
        )

    @classmethod
    def team_capability_section(cls, worker_names: list[str]) -> str:
        """Build a prompt section describing available team workers."""
        if not worker_names:
            return ""

        worker_list = "\n".join(f"- `{w}`" for w in worker_names[:10])
        return (
            "# Available Team Workers\n\n"
            "The following workers are available for delegation. Use `assign_task` "
            "to offload specialized work. Always pass the required task text, for "
            "example `assign_task(worker_name=\"worker\", task=\"specific work to "
            "perform\", wait=true)`; goal and deliverable do not replace task:\n\n"
            f"{worker_list}\n"
        )

    # ── Execution profile modifiers ───────────────────────────────

    @classmethod
    def execution_profile_section(cls, profile_name: str) -> str:
        """Return guidance specific to an execution profile."""
        profile = get_execution_profile(profile_name)
        if not profile:
            return ""
        if profile.prompt_modifier:
            return profile.prompt_modifier
        return (
            "# Execution Profile\n\n"
            f"Current execution profile: {profile.name}\n"
            f"{profile.description}"
        )

    # ── Failure recovery guidance ─────────────────────────────────

    @classmethod
    def failure_recovery_section(cls, last_error: str, retry_count: int) -> str:
        """Build a prompt section for failure recovery guidance."""
        if not last_error:
            return ""

        return (
            "# Recovery Context\n\n"
            f"The previous attempt failed (retry #{retry_count}).\n"
            f"Last error: {last_error[:300]}\n\n"
            "Before retrying:\n"
            "- Diagnose the root cause of the failure\n"
            "- Adjust the approach rather than retrying identically\n"
            "- If the same tool failed with the same arguments, try a different tool or path\n"
            "- If blocked by external factors, report the blocker clearly"
        )

    # ── Composite assembly ────────────────────────────────────────

    @classmethod
    def assemble(
        cls,
        *,
        base_prompt: str,
        plan: Optional[Any] = None,
        mcp_tools: Optional[list[str]] = None,
        worker_names: Optional[list[str]] = None,
        execution_profile: str = "",
        last_error: str = "",
        retry_count: int = 0,
        include_workpad_guide: bool = False,
    ) -> str:
        """Assemble a complete system prompt from modular sections."""
        sections: list[str] = [base_prompt]

        # Execution profile modifier (goes first to set tone)
        normalized_profile = normalize_execution_profile(execution_profile)
        if normalized_profile:
            profile_section = cls.execution_profile_section(normalized_profile)
            if profile_section:
                sections.append(profile_section)

        # Plan context
        if plan is not None:
            try:
                plan_summary = plan.render_human() if hasattr(plan, "render_human") else str(plan)
                plan_validation = getattr(plan, "validation_errors", []) or []
                metadata = dict(getattr(plan, "metadata", {}) or {})
                sections.append(cls.plan_context_section(
                    plan_summary,
                    plan_validation,
                    plan_stale=bool(metadata.get("stale")),
                ))
            except Exception:
                pass

        # Capability sections
        if mcp_tools:
            sections.append(cls.mcp_capability_section(mcp_tools))
        if worker_names:
            sections.append(cls.team_capability_section(worker_names))

        # Workpad format guide
        if include_workpad_guide:
            sections.append(cls.workpad_validation_prompt())

        # Failure recovery
        if last_error:
            sections.append(cls.failure_recovery_section(last_error, retry_count))

        return "\n\n".join(sections)
    # ── Budget-aware assembly ─────────────────────────────────────

    @classmethod
    def budget_aware_assemble(
        cls,
        *,
        base_prompt: str,
        plan: Optional[Any] = None,
        mcp_tools: Optional[list[str]] = None,
        worker_names: Optional[list[str]] = None,
        execution_profile: str = "",
        last_error: str = "",
        retry_count: int = 0,
        include_workpad_guide: bool = False,
        total_budget_tokens: int = 4000,
    ) -> str:
        """Assemble prompt sections with a total token budget.

        Each section has a weight (high/medium/low). When the budget is tight,
        lower-weight sections are truncated or skipped.
        """
        # Section weights: (priority, max_tokens_fraction)
        weights = {
            "base_prompt": (1.0, 1.0),       # never cut
            "execution_profile": (0.9, 0.5),  # medium
            "plan_context": (0.8, 0.6),       # medium
            "mcp_capability": (0.7, 0.4),     # low
            "team_capability": (0.7, 0.4),    # low
            "workpad_guide": (0.6, 0.3),      # lowest
            "failure_recovery": (0.9, 0.5),   # medium
        }

        # Estimate tokens (rough: 4 chars ≈ 1 token)
        estimate = lambda text: len(str(text or "")) // 4

        sections: list[tuple[str, float, int, str]] = []
        # Base prompt (always include)
        sections.append(("base_prompt", 1.0, estimate(base_prompt), base_prompt))

        # Collect optional sections
        optional = []
        normalized_profile = normalize_execution_profile(execution_profile)
        if normalized_profile:
            s = cls.execution_profile_section(normalized_profile)
            if s:
                optional.append(("execution_profile", s))
        if plan is not None:
            try:
                ps = plan.render_human() if hasattr(plan, "render_human") else str(plan)
                pv = getattr(plan, "validation_errors", []) or []
                metadata = dict(getattr(plan, "metadata", {}) or {})
                s = cls.plan_context_section(ps, pv, plan_stale=bool(metadata.get("stale")))
                optional.append(("plan_context", s))
            except Exception:
                pass
        if mcp_tools:
            optional.append(("mcp_capability", cls.mcp_capability_section(mcp_tools)))
        if worker_names:
            optional.append(("team_capability", cls.team_capability_section(worker_names)))
        if include_workpad_guide:
            optional.append(("workpad_guide", cls.workpad_validation_prompt()))
        if last_error:
            optional.append(("failure_recovery", cls.failure_recovery_section(last_error, retry_count)))

        # Sort by priority, add within budget
        used = estimate(base_prompt)
        for key, section_text in sorted(optional, key=lambda x: weights.get(x[0], (0.5, 0.2))[0], reverse=True):
            sec_tokens = estimate(section_text)
            _, max_fraction = weights.get(key, (0.5, 0.2))
            budget_left = total_budget_tokens - used
            if budget_left <= 0:
                break

            if sec_tokens <= budget_left * max_fraction:
                sections.append((key, weights.get(key, (0.5, 0.2))[0], sec_tokens, section_text))
                used += sec_tokens
            elif sec_tokens > 0 and budget_left > 200:
                # Truncate to fit
                truncated = str(section_text or "")[: int(budget_left * 4)]
                sections.append((key, 0.3, estimate(truncated), truncated + "\n[...truncated for budget]"))
                used += estimate(truncated)

        return "\n\n".join(text for _, _, _, text in sections)
