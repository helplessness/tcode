"""Standardized subagent summary output contracts.

Defines the structured summary schema that subagents should return.
Enables parent agents and orchestrators to programmatically inspect
subagent results without parsing free-text outputs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class SummaryVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    REVISE = "revise"


class EvidenceLevel(str, Enum):
    STRONG = "strong"
    ADEQUATE = "adequate"
    WEAK = "weak"
    NONE = "none"


@dataclass(slots=True)
class ArtifactRef:
    """Reference to an output artifact produced by a subagent."""

    path: str
    description: str = ""
    artifact_type: str = "file"  # file, directory, url, mcp_resource
    size_bytes: Optional[int] = None
    checksum: str = ""


@dataclass(slots=True)
class SubagentSummary:
    """Standardized summary contract for subagent task results.

    Every subagent should produce a summary matching this schema so the
    parent agent (or orchestrator) can:
    - Determine success/failure without parsing raw output
    - Extract key findings and recommendations
    - Reference output artifacts by canonical paths
    - Assess evidence quality for verification decisions
    - Decide next actions based on structured verdict
    """

    summary_id: str
    task_id: str
    agent_name: str
    verdict: SummaryVerdict = SummaryVerdict.PASS
    summary: str = ""  # One-paragraph executive summary
    key_findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    evidence_level: EvidenceLevel = EvidenceLevel.ADEQUATE
    confidence: float = 0.8  # 0.0 - 1.0
    limitations: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    duration_seconds: float = 0.0

    # ── Factory ──────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        agent_name: str,
        verdict: SummaryVerdict | str = SummaryVerdict.PASS,
        summary: str = "",
        **kwargs,
    ) -> "SubagentSummary":
        if isinstance(verdict, str):
            try:
                verdict = SummaryVerdict(str(verdict).strip().lower())
            except ValueError:
                verdict = SummaryVerdict.PASS
        return cls(
            summary_id=f"sum-{uuid.uuid4().hex[:10]}",
            task_id=str(task_id).strip(),
            agent_name=str(agent_name).strip(),
            verdict=verdict,
            summary=str(summary or "").strip(),
            **kwargs,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SubagentSummary":
        data = dict(payload or {})
        try:
            verdict = SummaryVerdict(str(data.get("verdict", "pass")).strip().lower())
        except ValueError:
            verdict = SummaryVerdict.PASS
        try:
            evidence_level = EvidenceLevel(str(data.get("evidence_level", "adequate")).strip().lower())
        except ValueError:
            evidence_level = EvidenceLevel.ADEQUATE

        return cls(
            summary_id=str(data.get("summary_id", "") or "").strip() or f"sum-{uuid.uuid4().hex[:10]}",
            task_id=str(data.get("task_id", "") or "").strip(),
            agent_name=str(data.get("agent_name", "") or "").strip(),
            verdict=verdict,
            summary=str(data.get("summary", "") or "").strip(),
            key_findings=[str(s).strip() for s in (data.get("key_findings") or []) if str(s).strip()],
            recommendations=[str(s).strip() for s in (data.get("recommendations") or []) if str(s).strip()],
            artifacts=[ArtifactRef(**a) if isinstance(a, dict) else a for a in (data.get("artifacts") or []) if isinstance(a, dict)],
            evidence_level=evidence_level,
            confidence=float(data.get("confidence", 0.8) or 0.8),
            limitations=[str(s).strip() for s in (data.get("limitations") or []) if str(s).strip()],
            open_questions=[str(s).strip() for s in (data.get("open_questions") or []) if str(s).strip()],
            next_actions=[str(s).strip() for s in (data.get("next_actions") or []) if str(s).strip()],
            metrics=dict(data.get("metrics", {}) or {}),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", datetime.now().timestamp()) or datetime.now().timestamp()),
            duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "key_findings": list(self.key_findings),
            "recommendations": list(self.recommendations),
            "artifacts": [
                {
                    "path": a.path,
                    "description": a.description,
                    "artifact_type": a.artifact_type,
                    "size_bytes": a.size_bytes,
                    "checksum": a.checksum,
                }
                for a in self.artifacts
            ],
            "evidence_level": self.evidence_level.value,
            "confidence": self.confidence,
            "limitations": list(self.limitations),
            "open_questions": list(self.open_questions),
            "next_actions": list(self.next_actions),
            "metrics": dict(self.metrics),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
        }

    def is_actionable(self) -> bool:
        """Return True if the summary provides enough information to act on."""
        return (
            bool(self.summary.strip())
            and self.verdict != SummaryVerdict.BLOCKED
            and self.evidence_level != EvidenceLevel.NONE
        )

    def render_prompt(self) -> str:
        """Render the summary as a prompt section for the parent agent."""
        lines = [
            f"## Subagent Result: {self.agent_name}",
            f"**Task**: {self.task_id}",
            f"**Verdict**: {self.verdict.value.upper()}",
            f"**Evidence**: {self.evidence_level.value}",
            f"**Confidence**: {self.confidence:.0%}",
            "",
            f"**Summary**: {self.summary}",
            "",
        ]

        if self.key_findings:
            lines.append("**Key Findings**:")
            for f in self.key_findings:
                lines.append(f"- {f}")
            lines.append("")

        if self.recommendations:
            lines.append("**Recommendations**:")
            for r in self.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        if self.artifacts:
            lines.append("**Artifacts**:")
            for a in self.artifacts:
                lines.append(f"- `{a.path}` — {a.description}" if a.description else f"- `{a.path}`")
            lines.append("")

        if self.limitations:
            lines.append("**Limitations**:")
            for l in self.limitations:
                lines.append(f"- {l}")
            lines.append("")

        if self.next_actions:
            lines.append("**Next Actions**:")
            for n in self.next_actions:
                lines.append(f"- {n}")

        return "\n".join(lines)


class SubagentSummaryValidator:
    """Validate subagent output against the summary contract."""

    REQUIRED_FIELDS = ["summary", "verdict"]
    RECOMMENDED_FIELDS = ["key_findings", "artifacts", "evidence_level", "confidence"]

    @classmethod
    def validate(cls, summary: SubagentSummary) -> dict[str, Any]:
        """Validate a subagent summary and return issues found."""
        issues: list[str] = []
        warnings: list[str] = []

        # Required fields
        if not summary.summary.strip():
            issues.append("summary: must not be empty")
        try:
            SummaryVerdict(str(summary.verdict.value))
        except ValueError:
            issues.append(f"verdict: invalid value '{summary.verdict}'")

        # Recommended fields
        if not summary.key_findings:
            warnings.append("key_findings: empty — consider adding at least one finding")
        if not summary.artifacts and summary.verdict == SummaryVerdict.PASS:
            warnings.append("artifacts: empty — PASS verdict should reference at least one output artifact")
        if summary.confidence < 0.5:
            warnings.append(f"confidence: low ({summary.confidence:.0%}) — consider documenting limitations")
        if summary.verdict == SummaryVerdict.BLOCKED and not summary.limitations:
            warnings.append("limitations: BLOCKED verdict should explain what is blocking")

        valid = len(issues) == 0
        return {
            "valid": valid,
            "issues": issues,
            "warnings": warnings,
            "actionable": summary.is_actionable(),
        }

    @classmethod
    def validate_raw_output(cls, raw_output: str) -> dict[str, Any]:
        """Try to parse and validate a raw subagent output string as a summary."""
        if not raw_output or not raw_output.strip():
            return {"valid": False, "issues": ["Empty output"], "warnings": [], "actionable": False}

        # Try JSON parse
        try:
            data = json.loads(raw_output.strip())
            if isinstance(data, dict):
                summary = SubagentSummary.from_dict(data)
                return cls.validate(summary)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to find JSON block in markdown
        import re
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw_output, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    summary = SubagentSummary.from_dict(data)
                    return cls.validate(summary)
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: construct minimal summary from raw text
        return {
            "valid": False,
            "issues": ["Output is not a structured summary — free-text detected"],
            "warnings": ["Consider using the standardized summary JSON format for subagent outputs"],
            "actionable": bool(raw_output.strip()),
        }