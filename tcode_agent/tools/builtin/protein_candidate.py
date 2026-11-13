"""Manage candidate comparison and promotion inside a protein run workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...types import ToolResult
from ...workspace.protein_candidates import (
    compare_candidates,
    load_candidate_index,
    promote_candidate,
    register_candidate,
    sync_candidate_index,
)
from ..base import ParameterSchema, Tool, ToolContext


class ProteinCandidateParameters(ParameterSchema):
    action: str
    candidate_id: str | None = ""
    candidate_ids: list[str] | None = None
    artifact_paths: list[str] | None = None
    metrics: dict[str, Any] | None = None
    metric: str | None = ""
    direction: str | None = "max"
    evidence: list[str] | None = None
    reason: str | None = ""
    review_verdict: str | None = ""
    notes: str | None = ""


def _protein_run_root(context: ToolContext) -> Path:
    extra = context.extra if isinstance(context.extra, dict) else {}
    resources = extra.get("resource_context")
    marker = resources.get("_workspace") if isinstance(resources, dict) else None
    if not isinstance(marker, dict) or marker.get("strategy") != "protein_run":
        raise ValueError("protein_candidate requires an active protein Run Workspace")
    run_root = Path(str(marker.get("run_root") or "")).expanduser().resolve(strict=True)
    workdir = Path(str(extra.get("workdir") or "")).expanduser().resolve(strict=True)
    if run_root != workdir:
        raise ValueError("protein candidate registry does not match the active workspace")
    return run_root


class ProteinCandidateTool(Tool):
    @property
    def id(self) -> str:
        return "protein_candidate"

    @property
    def description(self) -> str:
        return """Index, compare, register, and promote protein candidates in the active Run Workspace.

Actions:
- list: discover candidates from input/output FASTA artifacts and return the registry
- register: attach metrics/evidence to a candidate using auditable input/output artifact paths
- compare: rank registered/discovered candidates by a named numeric metric (direction=max|min)
- promote: materialize one candidate under outputs/promoted; requires verifier/report/orchestrator role, review_verdict=PASS, reason, and evidence

Never treat a narrative answer alone as a promoted candidate. Promotion always creates a decision ledger and hashed artifacts."""

    @property
    def parameters(self) -> type:
        return ProteinCandidateParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ProteinCandidateParameters(**args)
        action = str(parsed.action or "").strip().lower()
        run_root = _protein_run_root(context)
        try:
            if action == "list":
                index = sync_candidate_index(run_root)
                payload: Any = index
                title = "Protein candidates"
            elif action == "register":
                if not str(parsed.candidate_id or "").strip():
                    raise ValueError("register requires candidate_id")
                payload = register_candidate(
                    run_root,
                    candidate_id=str(parsed.candidate_id),
                    artifact_paths=list(parsed.artifact_paths or []),
                    metrics=dict(parsed.metrics or {}),
                    evidence=list(parsed.evidence or []),
                    notes=str(parsed.notes or ""),
                    actor=context.agent,
                )
                title = "Protein candidate registered"
            elif action == "compare":
                payload = {
                    "metric": str(parsed.metric or ""),
                    "direction": str(parsed.direction or "max"),
                    "ranking": compare_candidates(
                        run_root,
                        metric=str(parsed.metric or ""),
                        direction=str(parsed.direction or "max"),
                        candidate_ids=list(parsed.candidate_ids or []),
                    ),
                }
                title = "Protein candidate comparison"
            elif action == "promote":
                if not str(parsed.candidate_id or "").strip():
                    raise ValueError("promote requires candidate_id")
                payload = promote_candidate(
                    run_root,
                    candidate_id=str(parsed.candidate_id),
                    reason=str(parsed.reason or ""),
                    evidence=list(parsed.evidence or []),
                    review_verdict=str(parsed.review_verdict or ""),
                    actor=context.agent,
                )
                title = "Protein candidate promoted"
            else:
                raise ValueError("action must be one of: list, register, compare, promote")
        except (KeyError, OSError, PermissionError, TypeError, ValueError) as exc:
            return ToolResult(
                title="Protein candidate",
                output="",
                error=str(exc),
                metadata={
                    "protein_candidate": True,
                    "action": action,
                    "run_root": run_root.as_posix(),
                },
            )

        index = load_candidate_index(run_root)
        output = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if len(output) > 24_000:
            output = output[:24_000] + "\n...[candidate registry truncated]"
        return ToolResult(
            title=title,
            output=output,
            metadata={
                "protein_candidate": True,
                "action": action,
                "run_root": run_root.as_posix(),
                "candidate_registry_path": (run_root / "candidates.json").as_posix(),
                "candidate_count": len(index.get("candidates") or []),
                "promoted_count": sum(
                    1
                    for item in list(index.get("candidates") or [])
                    if isinstance(item, dict) and item.get("stage") == "promoted"
                ),
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())

