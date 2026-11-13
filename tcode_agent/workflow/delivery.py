"""Stable delivery projection for a workflow run."""

from __future__ import annotations

from typing import Any

from .models import WorkflowRun


def build_workflow_delivery_record(run: WorkflowRun) -> dict[str, Any]:
    stages = list(run.stage_states or [])
    artifacts: list[dict[str, Any]] = []
    checks_total = 0
    checks_passed = 0
    unresolved: list[dict[str, str]] = []
    for stage in stages:
        for name, value in dict(stage.artifact_refs or {}).items():
            artifacts.append({"stage_id": stage.stage_id, "name": str(name), "value": value})
        checks = list(dict(stage.validation_report or {}).get("checks") or [])
        checks_total += len(checks)
        checks_passed += sum(1 for check in checks if isinstance(check, dict) and check.get("ok") is True)
        if stage.status in {"failed", "blocked"} or stage.last_error:
            unresolved.append({
                "stage_id": stage.stage_id,
                "status": stage.status,
                "reason": str(stage.last_error or stage.result_summary or "节点未完成"),
            })
    completed = sum(1 for stage in stages if stage.status == "committed")
    previous = dict((run.metadata or {}).get("delivery_record") or {})
    acceptance = str(previous.get("acceptance") or "").strip()
    if not acceptance or (acceptance == "not_ready" and run.status == "completed"):
        acceptance = "pending" if run.status == "completed" else "not_ready"
    record = {
        "schema": "tcode.workflow-delivery.v1",
        "run_id": run.run_id,
        "template_key": run.template_key,
        "status": run.status,
        "stage_summary": {"completed": completed, "total": len(stages)},
        "attempts": sum(max(0, int(stage.attempt or 0)) for stage in stages),
        "validation": {"passed": checks_passed, "total": checks_total},
        "artifacts": artifacts,
        "unresolved": unresolved,
        "final_report": str((run.metadata or {}).get("final_report") or ""),
        "acceptance": acceptance,
    }
    for key in ("accepted_by", "feedback", "decided_at"):
        if previous.get(key) not in (None, ""):
            record[key] = previous[key]
    return record
