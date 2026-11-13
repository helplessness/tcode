"""Shared transaction boundary for workflow stage validation and commit."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .manager import WorkflowRunManager
from .human_confirmation import ensure_workflow_confirmation_review
from .models import WorkflowCandidate, WorkflowRun, WorkflowStageTemplate, WorkflowTemplate
from .validation import (
    declared_review_decisions,
    WorkflowValidationReport,
    normalize_stage_output_response,
    validate_stage_output,
)


@dataclass(slots=True)
class WorkflowStageCommitResult:
    run: WorkflowRun
    validation_report: WorkflowValidationReport
    committed: bool


def validate_and_commit_stage(
    *,
    run_id: str,
    stage: WorkflowStageTemplate,
    response: str,
    result_summary: Optional[str] = None,
    artifact_refs: Optional[dict[str, Any]] = None,
    candidates: Optional[list[WorkflowCandidate | dict[str, Any]]] = None,
    proposals: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
    draft_metadata: Optional[dict[str, Any]] = None,
    workdir: str = "",
    validation_workdir: str = "",
    expected_version: Optional[int] = None,
) -> WorkflowStageCommitResult:
    """Validate an isolated draft and publish it only after every check passes."""

    artifacts = dict(artifact_refs or {})
    normalized_response, output_normalization = normalize_stage_output_response(
        stage,
        response,
    )
    normalized_metadata = dict(metadata or {})
    if output_normalization.get("applied"):
        normalized_metadata["output_normalization"] = output_normalization
    candidate_payloads = [
        item.to_dict() if isinstance(item, WorkflowCandidate) else dict(item)
        for item in candidates or []
    ]
    proposal_payloads = [dict(item) for item in proposals or []]
    WorkflowRunManager.mark_stage_validating(
        run_id,
        stage.stage_id,
        draft_result={
            "response": normalized_response,
            "artifact_refs": artifacts,
            "candidates": candidate_payloads,
            "proposals": proposal_payloads,
            **dict(draft_metadata or {}),
        },
        validation_report={
            "ok": None,
            "phase": "output",
            "summary": "output validation pending",
        },
        metadata=normalized_metadata,
        workdir=workdir,
        expected_version=expected_version,
    )
    validation = validate_stage_output(
        stage,
        response=normalized_response,
        artifact_refs=artifacts,
        workdir=validation_workdir or workdir,
    )
    validation_payload = validation.to_dict()
    if stage.review_gate:
        # 明确的业务审核结论用于诊断；缺少审核字段仍属于输出合同问题。
        choices = declared_review_decisions(stage)
        pattern = "|".join(re.escape(value) for value in sorted(choices, key=len, reverse=True))
        match = re.search(r'(?im)^\s*(?:review_decision\s*[:：]\s*|["\']review_decision["\']\s*:\s*["\'])?(' + pattern + r')\b', normalized_response)
        decision = match.group(1).upper() if match else ""
        if decision in choices:
            normalized_metadata["review_decision"] = decision
            if not validation.ok:
                from .diagnostics import workflow_diagnostic
                validation_payload["diagnostic"] = workflow_diagnostic(validation.summary, validation_payload, review_decision=decision)
    if validation.ok:
        run = WorkflowRunManager.mark_stage_committed(
            run_id,
            stage.stage_id,
            result_summary=str(
                result_summary
                if result_summary is not None
                else normalized_response
            ).strip(),
            artifact_refs=artifacts,
            validation_report=validation_payload,
            candidates=candidate_payloads,
            proposals=proposal_payloads,
            metadata=normalized_metadata,
            workdir=workdir,
        )
        ensure_workflow_confirmation_review(run, workdir=workdir)
        return WorkflowStageCommitResult(
            run=run,
            validation_report=validation,
            committed=True,
        )

    retryable = any(item.retryable for item in validation.checks if not item.ok)
    run = WorkflowRunManager.mark_stage_failed(
        run_id,
        stage.stage_id,
        error=validation.summary,
        metadata={
            **normalized_metadata,
            "validation_report": validation_payload,
            "retryable": retryable,
        },
        workdir=workdir,
    )
    return WorkflowStageCommitResult(
        run=run,
        validation_report=validation,
        committed=False,
    )


def recover_validating_stage(run: WorkflowRun, *, workdir: str = "") -> WorkflowRun:
    """Finish a validation transaction interrupted after the draft was persisted."""

    if str(run.status or "").strip().lower() != "validating":
        return run
    stage_state = next(
        (
            item
            for item in run.stage_states
            if item.stage_id == run.current_stage_id and item.status == "validating"
        ),
        None,
    )
    if stage_state is None:
        return run
    snapshot = (run.metadata or {}).get("template_snapshot")
    if not isinstance(snapshot, dict):
        return WorkflowRunManager.mark_stage_failed(
            run.run_id,
            stage_state.stage_id,
            error="workflow template snapshot missing during validation recovery",
            metadata={"retryable": False},
            workdir=workdir,
        )
    template = WorkflowTemplate.from_dict(snapshot)
    stage = next(
        (item for item in template.stages if item.stage_id == stage_state.stage_id),
        None,
    )
    if stage is None:
        return WorkflowRunManager.mark_stage_failed(
            run.run_id,
            stage_state.stage_id,
            error="workflow stage missing from template snapshot during validation recovery",
            metadata={"retryable": False},
            workdir=workdir,
        )
    draft = dict(stage_state.draft_result or {})
    commit_result = validate_and_commit_stage(
        run_id=run.run_id,
        stage=stage,
        response=str(draft.get("response") or ""),
        artifact_refs=dict(draft.get("artifact_refs") or {}),
        candidates=[dict(item) for item in draft.get("candidates", []) or [] if isinstance(item, dict)],
        proposals=[dict(item) for item in draft.get("proposals", []) or [] if isinstance(item, dict)],
        metadata={"recovered_validation_transaction": True},
        draft_metadata={"recovered_validation_transaction": True},
        workdir=workdir,
    )
    return commit_result.run


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _running_stage(run: WorkflowRun):
    current_stage_id = str(getattr(run, "current_stage_id", "") or "").strip()
    if current_stage_id:
        stage = next(
            (
                item
                for item in run.stage_states
                if item.stage_id == current_stage_id
                and str(item.status or "").strip().lower() == "running"
            ),
            None,
        )
        if stage is not None:
            return stage
    return next(
        (
            item
            for item in run.stage_states
            if str(item.status or "").strip().lower() == "running"
        ),
        None,
    )


def _stage_recovery_config(stage) -> dict[str, Any]:
    metadata = dict(getattr(stage, "metadata", {}) or {})
    recovery = metadata.get("recovery")
    return dict(recovery or {}) if isinstance(recovery, dict) else {}


def _stale_running_policy(stage) -> str:
    metadata = dict(getattr(stage, "metadata", {}) or {})
    recovery = _stage_recovery_config(stage)
    policy = str(
        recovery.get("stale_running_policy")
        or recovery.get("interrupted_policy")
        or metadata.get("stale_running_policy")
        or "auto"
    ).strip().lower()
    if policy in {"none", "off", "disabled", "disable"}:
        return "disabled"
    if policy not in {"auto", "retry", "fallback", "block"}:
        return "auto"
    return policy


def _stale_after_seconds(stage) -> float:
    metadata = dict(getattr(stage, "metadata", {}) or {})
    recovery = _stage_recovery_config(stage)
    configured = (
        recovery.get("stale_after_seconds")
        or recovery.get("running_lease_seconds")
        or metadata.get("stale_after_seconds")
        or os.environ.get("TCODE_WORKFLOW_STALE_RUNNING_SECONDS")
        or os.environ.get("TCODE_WORKFLOW_RUNNING_LEASE_SECONDS")
        or 60 * 60
    )
    return max(30.0, min(_float_value(configured, 60 * 60), 7 * 24 * 60 * 60))


def _actor_session_id(stage, lease: dict[str, Any]) -> str:
    metadata = dict(getattr(stage, "metadata", {}) or {})
    return str(
        lease.get("actor_session_id")
        or metadata.get("agent_session_id")
        or metadata.get("worker_session_id")
        or ""
    ).strip()


def _running_stage_is_stale(
    stage,
    *,
    now: float,
) -> tuple[bool, dict[str, Any]]:
    metadata = dict(getattr(stage, "metadata", {}) or {})
    lease = metadata.get("running_lease")
    if isinstance(lease, dict):
        lease_payload = dict(lease)
        expires_at = _float_value(lease_payload.get("expires_at"), 0.0)
        if expires_at <= 0:
            heartbeat_at = _float_value(lease_payload.get("heartbeat_at"), 0.0)
            ttl_seconds = _float_value(lease_payload.get("ttl_seconds"), _stale_after_seconds(stage))
            expires_at = heartbeat_at + ttl_seconds if heartbeat_at > 0 else 0.0
        if expires_at > 0 and now < expires_at:
            return False, lease_payload
        return True, lease_payload

    started_at = _float_value(getattr(stage, "started_at", None), 0.0)
    if started_at <= 0:
        return False, {}
    return (now - started_at) >= _stale_after_seconds(stage), {}


def recover_stale_running_stage(
    run: WorkflowRun,
    *,
    workdir: str = "",
    now: Optional[float] = None,
    actor_is_active: Optional[Callable[[str], bool]] = None,
) -> WorkflowRun:
    """Recover a workflow run left in running state after an interrupted process.

    The function is intentionally conservative: only a running stage with an
    expired lease (or an old no-lease running stage) is converted into the
    existing failed/retry/fallback/block pipeline.
    """

    if str(getattr(run, "status", "") or "").strip().lower() != "running":
        return run
    stage = _running_stage(run)
    if stage is None:
        return run
    policy = _stale_running_policy(stage)
    if policy == "disabled":
        return run

    checked_at = float(now if now is not None else time.time())
    stale, lease = _running_stage_is_stale(stage, now=checked_at)
    if not stale:
        return run

    actor_session = _actor_session_id(stage, lease)
    if actor_session and actor_is_active is not None:
        try:
            if actor_is_active(actor_session):
                return run
        except Exception:
            pass

    recovery_metadata: dict[str, Any] = {
        "interrupted": True,
        "stale_running_recovery": True,
        "stale_running_policy": policy,
        "stale_running_detected_at": checked_at,
        "stale_running_actor_session_id": actor_session,
    }
    if lease:
        recovery_metadata["stale_running_lease"] = lease

    if policy in {"auto", "retry"}:
        recovery_metadata["retryable"] = True
        if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
            # A desktop restart loses the local process, not the durable task.
            # Give the interrupted stage a bounded retry budget while keeping
            # explicit cancellation and remote job identifiers intact.
            retry_policy = dict(getattr(stage, "metadata", {}).get("retry_policy") or {})
            configured = os.environ.get("TCODE_DESKTOP_RESTART_MAX_ATTEMPTS", "3")
            try:
                desktop_max_attempts = max(2, min(int(configured), 10))
            except (TypeError, ValueError):
                desktop_max_attempts = 3
            retry_policy["max_attempts"] = max(
                int(retry_policy.get("max_attempts", 1) or 1),
                desktop_max_attempts,
            )
            recovery_metadata["retry_policy"] = retry_policy
    else:
        recovery_metadata["retryable"] = False

    if policy == "fallback":
        recovery_metadata["failure_policy"] = {"action": "fallback"}
    elif policy == "block":
        recovery_metadata["failure_policy"] = {"action": "block"}

    return WorkflowRunManager.mark_stage_failed(
        run.run_id,
        stage.stage_id,
        error="workflow stage interrupted: running lease expired",
        metadata=recovery_metadata,
        workdir=workdir,
    )
