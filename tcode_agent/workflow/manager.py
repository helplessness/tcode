"""Persistence manager for template-driven workflow runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .models import WorkflowCandidate, WorkflowProposal, WorkflowRun, WorkflowStageState, WorkflowTemplate
from .registry import WorkflowTemplateRegistry
from .input_bindings import request_binding_field
from .delivery import build_workflow_delivery_record


def _append_research_workflow_snapshot(
    run: WorkflowRun,
    workdir: Optional[str | Path],
) -> None:
    """Best-effort workflow lifecycle projection into the Research Journal."""

    try:
        from ..research import ResearchEventInput, ResearchEventSink

        metadata = dict(getattr(run, "metadata", {}) or {})
        owner = {
            "tenant_id": str(metadata.get("tenant_id") or ""),
            "user_id": str(metadata.get("user_id") or ""),
            "workspace_id": str(metadata.get("workspace_id") or ""),
        }
        common = {
            **owner,
            "session_id": str(getattr(run, "session_id", "") or ""),
            "turn_id": str(metadata.get("turn_id") or ""),
            "trace_id": str(metadata.get("trace_id") or ""),
            "actor_type": "system",
            "actor_id": "workflow-manager",
            "privacy_state": "private",
        }
        resolved_workdir = str(workdir or "").strip() or None
        ResearchEventSink.append(
            ResearchEventInput(
                event_type="workflow.started",
                **common,
                source_kind="workflow_run",
                source_id=str(run.run_id or ""),
                source_ref=f"workflow:{run.run_id}",
                payload={
                    "run_id": str(run.run_id or ""),
                    "template_key": str(run.template_key or ""),
                    "status": str(run.status or ""),
                },
                stable_state_version="workflow.started",
            ),
            workdir=resolved_workdir,
        )
        for stage in list(getattr(run, "stage_states", []) or []):
            status = str(getattr(stage, "status", "") or "").strip().lower()
            event_type = {
                "running": "workflow.stage.started",
                "committed": "workflow.stage.completed",
                "failed": "workflow.stage.failed",
            }.get(status)
            if not event_type:
                continue
            stage_id = str(getattr(stage, "stage_id", "") or "")
            attempt = max(0, int(getattr(stage, "attempt", 0) or 0))
            stage_metadata = dict(getattr(stage, "metadata", {}) or {})
            stage_common = {
                **common,
                "actor_type": "agent",
                "actor_id": str(
                    stage_metadata.get("agent_name")
                    or stage_metadata.get("worker_name")
                    or getattr(stage, "owner", "")
                    or "workflow-stage"
                ),
            }
            ResearchEventSink.append(
                ResearchEventInput(
                    event_type=event_type,
                    **stage_common,
                    source_kind="workflow_stage",
                    source_id=f"{run.run_id}:{stage_id}:{attempt}",
                    source_ref=f"workflow:{run.run_id}/stage:{stage_id}/attempt:{attempt}",
                    payload={
                        "run_id": str(run.run_id or ""),
                        "template_key": str(run.template_key or ""),
                        "stage_id": stage_id,
                        "status": status,
                        "attempt": attempt,
                        "result_summary": str(
                            getattr(stage, "result_summary", "") or ""
                        ),
                        "last_error": str(getattr(stage, "last_error", "") or ""),
                        "artifact_refs": dict(
                            getattr(stage, "artifact_refs", {}) or {}
                        ),
                    },
                    stable_state_version=f"{status}:{attempt}",
                ),
                workdir=resolved_workdir,
            )
        run_status = str(getattr(run, "status", "") or "").strip().lower()
        terminal_event = (
            "workflow.completed"
            if run_status == "completed"
            else (
                "workflow.failed"
                if run_status in {"failed", "cancelled", "terminated"}
                else ""
            )
        )
        if terminal_event:
            generation = sum(
                max(0, int(getattr(stage, "attempt", 0) or 0))
                for stage in list(getattr(run, "stage_states", []) or [])
            )
            ResearchEventSink.append(
                ResearchEventInput(
                    event_type=terminal_event,
                    **common,
                    source_kind="workflow_run",
                    source_id=str(run.run_id or ""),
                    source_ref=f"workflow:{run.run_id}",
                    payload={
                        "run_id": str(run.run_id or ""),
                        "template_key": str(run.template_key or ""),
                        "status": run_status,
                        "version": int(getattr(run, "version", 0) or 0),
                    },
                    stable_state_version=f"{run_status}:{generation}",
                ),
                workdir=resolved_workdir,
            )
    except Exception:
        pass


class WorkflowVersionConflictError(ValueError):
    """Raised when a workflow run update was based on a stale version."""

    def __init__(self, *, expected_version: int, actual_version: int):
        super().__init__(
            f"workflow run version conflict: expected {expected_version}, actual {actual_version}"
        )
        self.expected_version = int(expected_version)
        self.actual_version = int(actual_version)


class WorkflowRunManager:
    """Create, persist, and evolve workflow runs stored under the workdir."""

    _RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    _LOCK = threading.RLock()
    _DEFAULT_RUNNING_LEASE_SECONDS = 30 * 60
    _ACTIVE_RUN_STATUSES = {
        "pending",
        "queued",
        "running",
        "validating",
        "waiting_external_job",
        "collecting",
        "review_pending",
        "revising",
        "recovering",
    }
    _ACTIVE_STAGE_STATUSES = _ACTIVE_RUN_STATUSES - {"pending", "queued"}

    @classmethod
    def _normalize_run_id(cls, run_id: str) -> str:
        normalized = str(run_id or "").strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or not cls._RUN_ID_PATTERN.fullmatch(normalized)
        ):
            raise ValueError("invalid workflow run_id")
        return normalized

    @classmethod
    def _normalize_workdir(cls, workdir: Optional[str | Path] = None) -> Path:
        if workdir is None:
            base = os.environ.get("TCODE_WORKDIR", os.getcwd())
            return Path(base)
        return Path(workdir)

    @classmethod
    def _storage_root(cls, workdir: Optional[str | Path] = None) -> Path:
        return cls._normalize_workdir(workdir) / ".tcode" / "workflow" / "runs"

    @classmethod
    def _ensure_storage(cls, workdir: Optional[str | Path] = None) -> Path:
        root = cls._storage_root(workdir)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _run_path(cls, run_id: str, workdir: Optional[str | Path] = None) -> Path:
        root = cls._ensure_storage(workdir).resolve()
        normalized = cls._normalize_run_id(run_id)
        path = (root / f"{normalized}.json").resolve()
        try:
            path.relative_to(root)
        except Exception:
            raise ValueError("workflow run_id escapes storage root")
        return path

    @classmethod
    def _save(
        cls,
        run: WorkflowRun,
        workdir: Optional[str | Path] = None,
        *,
        expected_version: Optional[int] = None,
        increment_version: bool = True,
        allow_terminated_restart: bool = False,
    ) -> WorkflowRun:
        with cls._LOCK:
            path = cls._run_path(run.run_id, workdir)
            was_existing = path.exists()
            if was_existing and not allow_terminated_restart:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if stored.get("metadata", {}).get("terminated") and (run.status != "terminated" or not run.metadata.get("terminated")):
                    raise ValueError("workflow has been terminated; late results cannot restart it")
            if expected_version is not None:
                actual_version = 0
                if path.exists():
                    try:
                        existing = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(existing, dict):
                            actual_version = max(1, int(existing.get("version", 1) or 1))
                    except Exception:
                        actual_version = 1
                if actual_version != int(expected_version):
                    raise WorkflowVersionConflictError(
                        expected_version=int(expected_version),
                        actual_version=actual_version,
                    )

            run.version = max(1, int(getattr(run, "version", 1) or 1))
            if increment_version:
                run.version += 1
            run.touch()
            run.metadata["delivery_record"] = build_workflow_delivery_record(run)
            payload = json.dumps(run.to_dict(), ensure_ascii=False, indent=2)
            tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
            if increment_version or not was_existing:
                _append_research_workflow_snapshot(run, workdir)
            return run

    @classmethod
    def _load(
        cls,
        run_id: str,
        workdir: Optional[str | Path] = None,
    ) -> Optional[WorkflowRun]:
        with cls._LOCK:
            path = cls._run_path(run_id, workdir)
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            if not isinstance(payload, dict):
                return None
            return WorkflowRun.from_dict(payload)

    @classmethod
    def _get_stage(cls, run: WorkflowRun, stage_id: str) -> WorkflowStageState:
        normalized = str(stage_id or "").strip()
        for stage in run.stage_states:
            if stage.stage_id == normalized:
                return stage
        raise KeyError(f"workflow stage not found: {normalized}")

    @classmethod
    def _stage_ids(cls, run: WorkflowRun) -> list[str]:
        return [str(stage.stage_id or "").strip() for stage in run.stage_states if str(stage.stage_id or "").strip()]

    @classmethod
    def _run_is_active(cls, run: WorkflowRun) -> bool:
        if str(run.status or "").strip().lower() in cls._ACTIVE_RUN_STATUSES:
            return True
        return any(
            str(stage.status or "").strip().lower() in cls._ACTIVE_STAGE_STATUSES
            for stage in run.stage_states
        )

    @classmethod
    def _float_config(cls, value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @classmethod
    def _running_lease_seconds(cls, metadata: Optional[dict[str, Any]] = None) -> float:
        timeout_policy = (metadata or {}).get("timeout_policy")
        if not isinstance(timeout_policy, dict):
            timeout_policy = {}
        recovery = (metadata or {}).get("recovery")
        if not isinstance(recovery, dict):
            recovery = {}
        configured = (
            timeout_policy.get("seconds")
            or timeout_policy.get("timeout_seconds")
            or recovery.get("running_lease_seconds")
            or recovery.get("stale_after_seconds")
            or (metadata or {}).get("running_lease_seconds")
            or os.environ.get("TCODE_WORKFLOW_RUNNING_LEASE_SECONDS")
            or cls._DEFAULT_RUNNING_LEASE_SECONDS
        )
        return max(30.0, min(cls._float_config(configured, cls._DEFAULT_RUNNING_LEASE_SECONDS), 24 * 60 * 60))

    @classmethod
    def _running_lease_payload(
        cls,
        *,
        stage: WorkflowStageState,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = time.time()
        base = dict(stage.metadata or {})
        incoming = dict(metadata or {})
        lease_seconds = cls._running_lease_seconds({**base, **incoming})
        actor_session_id = str(
            incoming.get("agent_session_id")
            or incoming.get("worker_session_id")
            or base.get("agent_session_id")
            or base.get("worker_session_id")
            or ""
        ).strip()
        return {
            "lease_id": uuid.uuid4().hex,
            "started_at": now,
            "heartbeat_at": now,
            "expires_at": now + lease_seconds,
            "ttl_seconds": lease_seconds,
            "actor_session_id": actor_session_id,
            "attempt": int(stage.attempt or 0),
            "idempotency_key": str(stage.idempotency_key or "").strip(),
        }

    @classmethod
    def _invalidate_stage_outputs(
        cls,
        run: WorkflowRun,
        stage_ids: list[str],
    ) -> None:
        """Remove superseded business outputs from normal downstream context."""

        reset_ids = {str(item or "").strip() for item in stage_ids if str(item or "").strip()}
        if not reset_ids:
            return
        removed_candidates = [
            item.candidate_id
            for item in run.candidates
            if item.source_stage_id in reset_ids
        ]
        run.candidates = [
            item for item in run.candidates if item.source_stage_id not in reset_ids
        ]
        superseded_proposals: list[str] = []
        for proposal in run.proposals:
            if proposal.source_stage_id not in reset_ids or proposal.status != "pending":
                continue
            proposal.resolve(
                "superseded",
                resolved_by="workflow-controller",
                reason="source stage was reset before downstream commit",
            )
            superseded_proposals.append(proposal.proposal_id)
        if removed_candidates or superseded_proposals:
            run.append_event(
                kind="stage_outputs_invalidated",
                summary="Superseded stage outputs removed from downstream context",
                payload={
                    "stage_ids": sorted(reset_ids),
                    "candidate_ids": removed_candidates,
                    "proposal_ids": superseded_proposals,
                },
            )

    @classmethod
    def _stage_and_descendants(
        cls,
        run: WorkflowRun,
        stage_id: str,
    ) -> set[str]:
        """Return one stage and every transitive dependent in the workflow DAG."""

        selected = {str(stage_id or "").strip()}
        changed = True
        while changed:
            changed = False
            for stage in run.stage_states:
                dependencies = {
                    str(item or "").strip()
                    for item in (stage.metadata or {}).get("depends_on", []) or []
                    if str(item or "").strip()
                }
                if stage.stage_id not in selected and dependencies & selected:
                    selected.add(stage.stage_id)
                    changed = True
        selected.discard("")
        return selected

    @classmethod
    def _proposal_revision_target(cls, run: WorkflowRun, proposal: WorkflowProposal) -> str:
        payload = dict(proposal.payload or {})
        stage_ids = cls._stage_ids(run)
        explicit_target = str(
            payload.get("target_stage_id")
            or payload.get("stage_id")
            or payload.get("fallback_stage_id")
            or ""
        ).strip()
        if explicit_target in stage_ids:
            return explicit_target

        source_stage_id = str(proposal.source_stage_id or "").strip()
        if source_stage_id:
            try:
                source_stage = cls._get_stage(run, source_stage_id)
            except KeyError:
                source_stage = None
            fallback_ids = []
            if source_stage is not None:
                fallback_ids = [
                    str(item or "").strip()
                    for item in (source_stage.metadata or {}).get("fallback_stage_ids", []) or []
                    if str(item or "").strip()
                ]
            for fallback_id in fallback_ids:
                if fallback_id in stage_ids:
                    return fallback_id
            if source_stage_id in stage_ids:
                return source_stage_id

        return run.current_stage_id if run.current_stage_id in stage_ids else (stage_ids[0] if stage_ids else "")

    @classmethod
    def _rewind_run_to_stage(
        cls,
        run: WorkflowRun,
        target_stage_id: str,
        *,
        proposal: WorkflowProposal,
    ) -> None:
        """Rewind a workflow branch for an approved revision proposal."""

        cls._reset_run_to_stage(
            run,
            target_stage_id,
            event_kind="workflow_rewound",
            summary=f"Workflow rewound to {target_stage_id} from revision proposal",
            payload={
                "proposal_id": proposal.proposal_id,
                "source_stage_id": proposal.source_stage_id,
            },
        )

    @classmethod
    def _reset_run_to_stage(
        cls,
        run: WorkflowRun,
        target_stage_id: str,
        *,
        event_kind: str,
        summary: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Reset one stage and all descendants while preserving an audit event."""

        target = str(target_stage_id or "").strip()
        stage_ids = cls._stage_ids(run)
        if target not in stage_ids:
            return

        stages_to_reset = cls._stage_and_descendants(run, target)
        reset_stage_ids: list[str] = []
        for stage in run.stage_states:
            if stage.stage_id not in stages_to_reset:
                continue
            if stage.status != "pending" or stage.result_summary or stage.artifact_refs or stage.last_error:
                stage.metadata["superseded_status"] = stage.status
                if stage.result_summary:
                    stage.metadata["superseded_result_summary"] = stage.result_summary
                if stage.artifact_refs:
                    stage.metadata["superseded_artifact_refs"] = dict(stage.artifact_refs)
                if stage.last_error:
                    stage.metadata["superseded_last_error"] = stage.last_error
            stage.status = "pending"
            stage.started_at = None
            stage.completed_at = None
            stage.result_summary = ""
            stage.artifact_refs = {}
            stage.last_error = ""
            stage.draft_result = {}
            stage.committed_result = {}
            stage.validation_report = {}
            stage.idempotency_key = ""
            for metadata_key in (
                "running_lease",
                "async_tool_refs",
                "async_completed_refs",
                "async_ref_count",
                "retry_attempts_used",
                "retry_max_retries",
                "terminated",
                "terminated_by",
                "termination_reason",
                "terminated_at",
                "fallback_selected_stage_id",
                "fallback_attempted_stage_ids",
                "fallback_exhausted",
            ):
                stage.metadata.pop(metadata_key, None)
            reset_stage_ids.append(stage.stage_id)

        cls._invalidate_stage_outputs(run, reset_stage_ids)

        run.status = "queued"
        run.current_stage_id = target
        run.append_event(
            kind=event_kind,
            stage_id=target,
            summary=summary,
            payload={
                **dict(payload or {}),
                "target_stage_id": target,
                "reset_stage_ids": reset_stage_ids,
            },
        )

    @classmethod
    def create_run(
        cls,
        *,
        template_key: str,
        session_id: str,
        input_payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
        workdir: Optional[str | Path] = None,
    ) -> WorkflowRun:
        """Instantiate and persist a workflow run from a declarative template."""

        template = WorkflowTemplateRegistry.get(template_key)
        if template is None:
            raise KeyError(f"workflow template not found: {template_key}")
        return cls.create_run_from_template(
            template=template, session_id=session_id, input_payload=input_payload,
            metadata=metadata, run_id=run_id, workdir=workdir,
        )

    @classmethod
    def create_run_from_template(
        cls, *, template: WorkflowTemplate, session_id: str,
        input_payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None, run_id: Optional[str] = None,
        workdir: Optional[str | Path] = None,
    ) -> WorkflowRun:
        """Instantiate a run from a validated immutable template snapshot."""
        resolved_run_id = cls._normalize_run_id(run_id or f"wf-{uuid.uuid4().hex[:12]}")
        run = WorkflowRun.from_template(
            run_id=resolved_run_id,
            template=template,
            session_id=str(session_id or "").strip(),
            input_payload=input_payload,
            metadata=metadata,
        )
        template_snapshot = template.to_dict(include_prompt_content=True)
        run.metadata["template_snapshot"] = template_snapshot
        run.metadata["template_hash"] = hashlib.sha256(
            json.dumps(
                template_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        run.metadata["template_version"] = str(
            (template.metadata or {}).get("version") or "1"
        )
        return cls._save(run, workdir, increment_version=False)

    @classmethod
    def merge_input_payload(
        cls,
        run_id: str,
        *,
        input_payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Merge workflow inputs or metadata into an existing run."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        if isinstance(input_payload, dict):
            input_payload = dict(input_payload)
            snapshot = (run.metadata or {}).get("template_snapshot") or {}
            contract = snapshot.get("input_contract") if isinstance(snapshot, dict) else {}
            bound_field = request_binding_field(contract)
            if bound_field and run.input_payload.get(bound_field):
                replacement = input_payload.pop(bound_field, run.input_payload[bound_field])
                if replacement != run.input_payload[bound_field]:
                    run.append_event(kind="input_binding_preserved",
                        summary="Preserved original user-request binding during input merge",
                        payload={"field": bound_field})
            for key, value in input_payload.items():
                run.input_payload[key] = value
        if isinstance(metadata, dict):
            run.metadata.update(dict(metadata))
        if isinstance(input_payload, dict) and input_payload:
            run.append_event(
                kind="input_payload_merged",
                summary="Workflow input payload updated",
                payload={"keys": sorted(str(key) for key in input_payload.keys())},
            )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def set_delivery_acceptance(
        cls,
        run_id: str,
        *,
        acceptance: str,
        actor: str = "user",
        feedback: str = "",
        workdir: Optional[str | Path] = None,
    ) -> WorkflowRun:
        """Attach the user's final decision to the durable delivery record."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        record = build_workflow_delivery_record(run)
        record["acceptance"] = str(acceptance or "pending").strip() or "pending"
        record["accepted_by"] = str(actor or "user").strip() or "user"
        record["feedback"] = str(feedback or "").strip()[:4000]
        run.metadata["delivery_record"] = record
        run.append_event(
            kind="delivery_acceptance_updated",
            source="outcome_confirmation",
            summary=f"Delivery acceptance: {record['acceptance']}",
            payload={"acceptance": record["acceptance"], "actor": record["accepted_by"]},
        )
        return cls._save(run, workdir)

    @classmethod
    def terminate(cls, run_id: str, *, actor: str, reason: str, workdir=None, expected_version=None) -> WorkflowRun:
        """Freeze every active stage and persist cleanup intent before external I/O."""
        with cls._LOCK:
            run = cls._load(run_id, workdir)
            if run is None:
                raise KeyError(f"workflow run not found: {run_id}")
            if run.metadata.get("terminated"):
                return run
            run.status = "terminated"
            run.metadata.update(terminated=True, terminated_by=actor, termination_reason=reason,
                                terminated_at=time.time(), termination_cleanup_pending=True)
            for stage in run.stage_states:
                if stage.status in cls._ACTIVE_STAGE_STATUSES | {"queued", "pending"}:
                    stage.mark_failed(reason)
                    stage.metadata["terminated"] = True
            run.append_event(kind="run_terminated", stage_id=run.current_stage_id, summary=reason, payload={"actor": actor})
            return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def update_status(
        cls,
        run_id: str,
        *,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
        current_stage_id: Optional[str] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Persist a workflow run status update."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        run.status = str(status or run.status).strip() or run.status
        if current_stage_id is not None:
            run.current_stage_id = str(current_stage_id or "").strip()
        if isinstance(metadata, dict):
            run.metadata.update(dict(metadata))
        run.append_event(
            kind="run_status_updated",
            stage_id=run.current_stage_id,
            summary=f"Run status updated to {run.status}",
            payload={"status": run.status},
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def append_event(
        cls,
        run_id: str,
        *,
        kind: str,
        source: str = "workflow",
        stage_id: str = "",
        summary: str = "",
        payload: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Persist one platform event without changing the run lifecycle state."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        run.append_event(
            kind=kind,
            source=source,
            stage_id=stage_id,
            summary=summary,
            payload=payload,
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def get(
        cls,
        run_id: str,
        *,
        workdir: Optional[str | Path] = None,
    ) -> Optional[WorkflowRun]:
        """Return one persisted workflow run."""

        return cls._load(run_id, workdir)

    @classmethod
    def rerun_from_stage(
        cls,
        run_id: str,
        stage_id: str,
        *,
        actor: str = "operator",
        reason: str = "",
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Reset one stage and its descendants so execution can resume there."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        if cls._run_is_active(run):
            raise ValueError(
                "workflow run is active; terminate it before rerunning from a stage"
            )

        stage = cls._get_stage(run, stage_id)
        stage_by_id = {item.stage_id: item for item in run.stage_states}
        missing_dependencies = [
            dependency
            for dependency in (stage.metadata or {}).get("depends_on", []) or []
            if dependency not in stage_by_id
            or str(stage_by_id[dependency].status or "").strip().lower()
            != "committed"
        ]
        if missing_dependencies:
            raise ValueError(
                "workflow stage dependencies are not committed: "
                + ", ".join(missing_dependencies)
            )

        normalized_actor = str(actor or "operator").strip() or "operator"
        normalized_reason = str(reason or "").strip() or "rerun requested by operator"
        reset_stage_ids = cls._stage_and_descendants(run, stage.stage_id)
        cls._reset_run_to_stage(
            run,
            stage.stage_id,
            event_kind="workflow_stage_rerun_requested",
            summary=f"Workflow rerun requested from {stage.stage_id}",
            payload={
                "actor": normalized_actor,
                "reason": normalized_reason,
            },
        )

        for proposal in run.proposals:
            if proposal.source_stage_id not in reset_stage_ids:
                continue
            if proposal.status == "superseded":
                continue
            proposal.metadata["superseded_status"] = proposal.status
            proposal.resolve(
                "superseded",
                resolved_by=normalized_actor,
                reason=f"source stage reset for rerun: {stage.stage_id}",
            )

        for metadata_key in (
            "final_report",
            "final_report_proposal_id",
            "final_report_stage_id",
            "terminated",
            "terminated_by",
            "termination_reason",
            "terminated_at",
            "cancelled",
            "cancelled_by",
            "cancel_reason",
            "cancelled_at",
        ):
            run.metadata.pop(metadata_key, None)
        rerun_history = run.metadata.get("rerun_history")
        if not isinstance(rerun_history, list):
            rerun_history = []
        rerun_history.append(
            {
                "stage_id": stage.stage_id,
                "reset_stage_ids": [
                    item.stage_id
                    for item in run.stage_states
                    if item.stage_id in reset_stage_ids
                ],
                "actor": normalized_actor,
                "reason": normalized_reason,
                "requested_at": time.time(),
            }
        )
        run.metadata["rerun_history"] = rerun_history[-50:]
        if run.metadata.get("termination_cleanup_pending"):
            raise ValueError("remote cancellation is still pending; wait before rerunning")
        return cls._save(run, workdir, expected_version=expected_version, allow_terminated_restart=True)

    @classmethod
    def delete_run(
        cls,
        run_id: str,
        *,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Delete one inactive persisted run and return its final snapshot."""

        with cls._LOCK:
            run = cls._load(run_id, workdir)
            if run is None:
                raise KeyError(f"workflow run not found: {run_id}")
            if cls._run_is_active(run):
                raise ValueError(
                    "workflow run is active; terminate it before deletion"
                )
            if expected_version is not None and int(run.version) != int(expected_version):
                raise WorkflowVersionConflictError(
                    expected_version=int(expected_version),
                    actual_version=int(run.version),
                )
            path = cls._run_path(run.run_id, workdir)
            if not path.exists():
                raise KeyError(f"workflow run not found: {run_id}")
            path.unlink()
            return run

    @classmethod
    def list_runs(
        cls,
        *,
        session_id: Optional[str] = None,
        template_key: Optional[str] = None,
        status: Optional[str] = None,
        workdir: Optional[str | Path] = None,
    ) -> list[WorkflowRun]:
        """List workflow runs with optional filters."""

        root = cls._ensure_storage(workdir)
        items: list[WorkflowRun] = []
        for path in sorted(root.glob("*.json")):
            run = cls._load(path.stem, workdir)
            if run is None:
                continue
            if session_id and run.session_id != str(session_id):
                continue
            if template_key and run.template_key != str(template_key):
                continue
            if status and run.status != str(status):
                continue
            items.append(run)
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    @classmethod
    def mark_stage_running(
        cls,
        run_id: str,
        stage_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Mark a stage as running and persist the updated run."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        if stage.status != "pending":
            raise ValueError(
                f"workflow stage is not pending: {stage.stage_id} ({stage.status})"
            )
        dependencies_ok, missing_dependencies = run.dependencies_committed(stage.stage_id)
        if not dependencies_ok:
            raise ValueError(
                "workflow stage dependencies are not committed: "
                + ", ".join(missing_dependencies)
            )
        stage.mark_running()
        stage.idempotency_key = f"{run.run_id}:{stage.stage_id}:attempt:{stage.attempt}"
        running_metadata = dict(metadata or {})
        running_metadata["running_lease"] = cls._running_lease_payload(
            stage=stage,
            metadata=running_metadata,
        )
        stage.metadata.update(running_metadata)
        run.status = "running"
        run.current_stage_id = stage.stage_id
        run.append_event(
            kind="stage_started",
            stage_id=stage.stage_id,
            summary=f"Stage started: {stage.label or stage.stage_id}",
            payload={"owner": stage.owner, "call_type": stage.call_type},
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def refresh_stage_running_lease(
        cls,
        run_id: str,
        stage_id: str,
        *,
        lease_id: str,
        workdir: Optional[str | Path] = None,
    ) -> bool:
        """Refresh a running stage lease. Returns false when the lease is no longer active."""

        normalized_lease = str(lease_id or "").strip()
        if not normalized_lease:
            return False
        run = cls._load(run_id, workdir)
        if run is None:
            return False
        try:
            stage = cls._get_stage(run, stage_id)
        except KeyError:
            return False
        if str(stage.status or "").strip().lower() != "running":
            return False
        lease = stage.metadata.get("running_lease")
        if not isinstance(lease, dict):
            return False
        if str(lease.get("lease_id") or "").strip() != normalized_lease:
            return False
        now = time.time()
        ttl_seconds = max(30.0, cls._float_config(lease.get("ttl_seconds"), cls._running_lease_seconds(stage.metadata)))
        lease["heartbeat_at"] = now
        lease["expires_at"] = now + ttl_seconds
        stage.metadata["running_lease"] = lease
        cls._save(run, workdir, increment_version=False)
        return True

    @classmethod
    def mark_stage_waiting(
        cls,
        run_id: str,
        stage_id: str,
        *,
        status: str = "waiting_external_job",
        result_summary: str = "",
        artifact_refs: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Mark a stage as waiting on an external async action."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        if stage.status not in {
            "running",
            "waiting_external_job",
            "review_pending",
            "collecting",
        }:
            raise ValueError(
                f"workflow stage cannot wait from {stage.status}: {stage.stage_id}"
            )
        normalized_status = str(status or "waiting_external_job").strip() or "waiting_external_job"
        stage.mark_waiting(
            status=normalized_status,
            result_summary=result_summary,
            artifact_refs=artifact_refs,
            metadata=metadata,
        )
        run.status = normalized_status
        run.current_stage_id = stage.stage_id
        run.append_event(
            kind=f"stage_{normalized_status}",
            stage_id=stage.stage_id,
            summary=result_summary or f"Stage waiting: {stage.label or stage.stage_id}",
            payload={
                "status": normalized_status,
                "artifact_keys": sorted(str(key) for key in (artifact_refs or {}).keys()),
            },
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def mark_stage_validating(
        cls,
        run_id: str,
        stage_id: str,
        *,
        draft_result: dict[str, Any],
        validation_report: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Persist an uncommitted result before executable validation."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        if stage.status not in {
            "running",
            "validating",
            "waiting_external_job",
            "review_pending",
            "collecting",
        }:
            raise ValueError(
                f"workflow stage cannot enter validation from {stage.status}: {stage.stage_id}"
            )
        stage.mark_validating(
            draft_result=draft_result,
            validation_report=validation_report,
        )
        if metadata:
            stage.metadata.update(dict(metadata))
        run.status = "validating"
        run.current_stage_id = stage.stage_id
        run.append_event(
            kind="stage_validating",
            stage_id=stage.stage_id,
            summary=f"Stage validating: {stage.label or stage.stage_id}",
            payload={"attempt": stage.attempt},
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def mark_stage_committed(
        cls,
        run_id: str,
        stage_id: str,
        *,
        result_summary: str = "",
        artifact_refs: Optional[dict[str, Any]] = None,
        validation_report: dict[str, Any],
        candidates: Optional[list[WorkflowCandidate | dict[str, Any]]] = None,
        proposals: Optional[list[dict[str, Any]]] = None,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Commit a validated stage result and expose it to downstream stages."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        if stage.status != "validating":
            raise ValueError(
                f"workflow stage is not validating: {stage.stage_id} ({stage.status})"
            )
        normalized_report = dict(validation_report or {})
        if normalized_report.get("ok") is not True:
            raise ValueError(
                "workflow stage cannot commit without a passing validation report"
            )
        stage.validation_report = normalized_report
        stage.mark_committed(
            result_summary=result_summary,
            artifact_refs=artifact_refs,
            metadata=metadata,
        )
        stage.last_error = ""
        next_stage_id = run.next_pending_stage_id()
        run.current_stage_id = next_stage_id
        pending_exists = any(item.status == "pending" for item in run.stage_states)
        run.status = (
            "running"
            if next_stage_id
            else ("blocked" if pending_exists else "completed")
        )
        run.append_event(
            kind="stage_committed",
            stage_id=stage.stage_id,
            summary=f"Stage committed: {stage.label or stage.stage_id}",
            payload={
                "next_stage_id": next_stage_id,
                "artifact_keys": sorted(str(key) for key in (artifact_refs or {}).keys()),
                "validation_summary": str(normalized_report.get("summary") or "")[:500],
            },
        )
        for raw_candidate in candidates or []:
            candidate = (
                raw_candidate
                if isinstance(raw_candidate, WorkflowCandidate)
                else WorkflowCandidate.from_dict(raw_candidate)
            )
            replaced = False
            for index, existing in enumerate(run.candidates):
                if existing.candidate_id == candidate.candidate_id:
                    run.candidates[index] = candidate
                    replaced = True
                    break
            if not replaced:
                run.candidates.append(candidate)
            run.append_event(
                kind="candidate_upserted",
                stage_id=candidate.source_stage_id,
                summary=f"Candidate {'updated' if replaced else 'created'}: {candidate.candidate_id}",
                payload={"candidate_id": candidate.candidate_id, "replaced": replaced},
            )

        for spec in proposals or []:
            proposal_type = str(spec.get("proposal_type") or "").strip()
            if proposal_type not in {"candidate", "revision", "final_report"}:
                raise ValueError(
                    "proposal_type must be candidate, revision, or final_report"
                )
            proposal = WorkflowProposal(
                proposal_id=f"wfp-{uuid.uuid4().hex[:12]}",
                proposal_type=proposal_type,
                source_stage_id=str(spec.get("source_stage_id") or stage.stage_id).strip(),
                proposed_by=str(spec.get("proposed_by") or "workflow").strip() or "workflow",
                payload=dict(spec.get("payload") or {}),
                summary=str(spec.get("summary") or "").strip()[:1200],
                raw_marker=str(spec.get("raw_marker") or "")[:8192],
                metadata=dict(spec.get("metadata") or {}),
            )
            run.proposals.append(proposal)
            if proposal.proposal_type == "revision":
                run.status = "blocked"
                run.current_stage_id = proposal.source_stage_id or run.current_stage_id
                pending = run.metadata.get("pending_revision_proposal_ids")
                if not isinstance(pending, list):
                    pending = []
                pending.append(proposal.proposal_id)
                run.metadata["pending_revision_proposal_ids"] = pending[-20:]
            run.append_event(
                kind="proposal_created",
                stage_id=proposal.source_stage_id,
                summary=f"Proposal created: {proposal.proposal_type}",
                payload={
                    "proposal_id": proposal.proposal_id,
                    "proposal_type": proposal.proposal_type,
                },
            )

        confirmation = (stage.metadata or {}).get("human_confirmation")
        confirmation_config = (
            dict(confirmation) if isinstance(confirmation, dict) else {}
        )
        raw_required = confirmation_config.get("required", False)
        required = (
            str(raw_required).strip().lower()
            not in {"", "0", "false", "no", "off", "disabled"}
            if isinstance(raw_required, str)
            else bool(raw_required)
        )
        if required and run.status != "blocked":
            confirmation_key = (
                f"{run.run_id}:{stage.stage_id}:attempt:{max(1, stage.attempt)}"
            )
            digest = hashlib.sha256(confirmation_key.encode("utf-8")).hexdigest()
            review_id = f"workflow-confirm-{digest[:20]}"
            review_identifier = f"WF-CONFIRM-{digest[:12].upper()}"
            requested_at = time.time()
            stage.status = "review_pending"
            stage.metadata["human_confirmation_state"] = {
                "kind": "workflow_stage_confirmation",
                "status": "pending_review",
                "review_id": review_id,
                "review_identifier": review_identifier,
                "requested_at": requested_at,
                "attempt": max(1, stage.attempt),
                "config": confirmation_config,
            }
            run.status = "review_pending"
            run.current_stage_id = stage.stage_id
            run.append_event(
                kind="stage_human_confirmation_requested",
                stage_id=stage.stage_id,
                summary=str(
                    confirmation_config.get("title")
                    or f"Human confirmation requested: {stage.label or stage.stage_id}"
                )[:1200],
                payload={
                    "review_id": review_id,
                    "review_identifier": review_identifier,
                    "reason": str(confirmation_config.get("reason") or "")[:1200],
                    "on_reject": str(
                        confirmation_config.get("on_reject") or "block"
                    ),
                    "fallback_stage_id": str(
                        confirmation_config.get("fallback_stage_id") or ""
                    ),
                },
            )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def resolve_stage_human_confirmation(
        cls,
        run_id: str,
        stage_id: str,
        *,
        decision: str,
        actor: str = "human",
        reason: str = "",
        review_id: str = "",
        review_identifier: str = "",
        resolved_at: str = "",
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Resolve one declarative stage confirmation and continue or rewind."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        state = (stage.metadata or {}).get("human_confirmation_state")
        if not isinstance(state, dict):
            raise ValueError(
                f"workflow stage has no pending human confirmation: {stage.stage_id}"
            )

        current_status = str(state.get("status") or "").strip().lower()
        normalized_decision = str(decision or "").strip().lower()
        expected_review_id = str(state.get("review_id") or "").strip()
        expected_identifier = str(state.get("review_identifier") or "").strip()
        if review_id and expected_review_id and review_id != expected_review_id:
            raise ValueError("human confirmation review id does not match workflow stage")
        if (
            review_identifier
            and expected_identifier
            and review_identifier != expected_identifier
        ):
            raise ValueError(
                "human confirmation review identifier does not match workflow stage"
            )
        if current_status in {"approved", "rejected"}:
            return run
        if stage.status != "review_pending" or run.status != "review_pending":
            raise ValueError(
                f"workflow stage is not waiting for human confirmation: "
                f"{stage.stage_id} ({stage.status})"
            )
        if normalized_decision not in {"approved", "rejected"}:
            raise ValueError("human confirmation decision must be approved or rejected")

        resolved_state = {
            **dict(state),
            "status": normalized_decision,
            "resolved_by": str(actor or "human").strip() or "human",
            "resolved_at": str(resolved_at or "").strip() or time.time(),
            "reason": str(reason or "")[:1200],
        }
        stage.metadata["human_confirmation_state"] = resolved_state

        if normalized_decision == "approved":
            stage.status = "committed"
            next_stage_id = run.next_pending_stage_id()
            run.current_stage_id = next_stage_id
            pending_exists = any(item.status == "pending" for item in run.stage_states)
            run.status = (
                "running"
                if next_stage_id
                else ("blocked" if pending_exists else "completed")
            )
            run.append_event(
                kind="stage_human_confirmation_approved",
                source="human_review",
                stage_id=stage.stage_id,
                summary=f"Human confirmation approved: {stage.label or stage.stage_id}",
                payload={
                    "review_id": expected_review_id,
                    "review_identifier": expected_identifier,
                    "next_stage_id": next_stage_id,
                    "reason": str(reason or "")[:1200],
                },
            )
            return cls._save(
                run,
                workdir,
                expected_version=expected_version,
            )

        config = (
            dict(resolved_state.get("config") or {})
            if isinstance(resolved_state.get("config"), dict)
            else {}
        )
        on_reject = str(config.get("on_reject") or "block").strip().lower()
        fallback_stage_id = str(config.get("fallback_stage_id") or "").strip()
        if on_reject == "fallback" and fallback_stage_id in cls._stage_ids(run):
            cls._reset_run_to_stage(
                run,
                fallback_stage_id,
                event_kind="stage_human_confirmation_rejected",
                summary=(
                    f"Human confirmation rejected; workflow rewound to "
                    f"{fallback_stage_id}"
                ),
                payload={
                    "source_stage_id": stage.stage_id,
                    "review_id": expected_review_id,
                    "review_identifier": expected_identifier,
                    "decision": "rejected",
                    "reason": str(reason or "")[:1200],
                },
            )
        else:
            stage.status = "failed"
            stage.last_error = str(reason or "human confirmation rejected")[:1200]
            run.status = "blocked"
            run.current_stage_id = stage.stage_id
            run.append_event(
                kind="stage_human_confirmation_rejected",
                source="human_review",
                stage_id=stage.stage_id,
                summary=f"Human confirmation rejected: {stage.label or stage.stage_id}",
                payload={
                    "review_id": expected_review_id,
                    "review_identifier": expected_identifier,
                    "decision": "rejected",
                    "on_reject": on_reject,
                    "reason": str(reason or "")[:1200],
                },
            )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def mark_stage_failed(
        cls,
        run_id: str,
        stage_id: str,
        *,
        error: str,
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Mark a stage as failed.

        If the stage has fallback_stage_ids, automatically transition to the
        first fallback stage instead of blocking the workflow. This enables
        graceful degradation without manual intervention.
        """

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        stage = cls._get_stage(run, stage_id)
        stage.mark_failed(error)
        if metadata:
            stage.metadata.update(dict(metadata))
            if isinstance(metadata.get("validation_report"), dict):
                stage.validation_report = dict(metadata["validation_report"])

        retry_policy = dict((stage.metadata or {}).get("retry_policy") or {})
        try:
            max_attempts = max(1, int(retry_policy.get("max_attempts", 1) or 1))
        except Exception:
            max_attempts = 1
        retryable = bool((metadata or {}).get("retryable"))
        if retryable and stage.attempt < max_attempts:
            stage.status = "pending"
            stage.completed_at = None
            run.status = "running"
            run.current_stage_id = stage.stage_id
            run.append_event(
                kind="stage_retry_scheduled",
                stage_id=stage.stage_id,
                summary=f"Stage retry scheduled: {stage.attempt + 1}/{max_attempts}",
                payload={
                    "error": str(error or "")[:1200],
                    "attempt": stage.attempt,
                    "max_attempts": max_attempts,
                },
            )
            return cls._save(run, workdir, expected_version=expected_version)

        # Auto-fallback chooses the first valid, not-yet-attempted target. Invalid
        # ids and repeated source/target pairs are skipped so a bad template cannot
        # leave a run pointing at a nonexistent stage or loop forever.
        fallback_ids: list[str] = []
        if isinstance(stage.metadata.get("fallback_stage_ids"), list):
            fallback_ids = list(
                dict.fromkeys(
                    str(item).strip()
                    for item in stage.metadata.get("fallback_stage_ids", [])
                    if str(item).strip()
                )
            )
        failure_policy = dict((stage.metadata or {}).get("failure_policy") or {})
        failure_action = str(
            failure_policy.get("action")
            or ("fallback" if fallback_ids else "block")
        ).strip().lower()
        if failure_action != "fallback":
            fallback_ids = []

        configured_fallback_codes = {
            str(item or "").strip()
            for item in (failure_policy.get("fallback_on_codes") or [])
            if str(item or "").strip()
        }
        observed_failure_codes: set[str] = set()
        validation_report = (metadata or {}).get("validation_report")
        if not isinstance(validation_report, dict):
            validation_report = stage.validation_report
        if isinstance(validation_report, dict):
            for check in validation_report.get("checks", []) or []:
                if not isinstance(check, dict) or bool(check.get("ok")):
                    continue
                observed_failure_codes.update(
                    part.strip()
                    for part in str(check.get("code") or "").split(",")
                    if part.strip()
                )
        for key in ("failure_code", "error_code"):
            value = str((metadata or {}).get(key) or "").strip()
            if value:
                observed_failure_codes.add(value)

        if configured_fallback_codes and not (
            configured_fallback_codes & observed_failure_codes
        ):
            fallback_ids = []
            stage.metadata["fallback_condition_not_met"] = {
                "allowed_codes": sorted(configured_fallback_codes),
                "observed_codes": sorted(observed_failure_codes),
            }
        else:
            stage.metadata.pop("fallback_condition_not_met", None)

        fallback_history = run.metadata.get("fallback_history")
        if not isinstance(fallback_history, list):
            fallback_history = []
        attempted_targets = {
            str(item.get("target_stage_id", "") or "").strip()
            for item in fallback_history
            if isinstance(item, dict)
            and str(item.get("source_stage_id", "") or "").strip() == stage.stage_id
        }
        stage_by_id = {item.stage_id: item for item in run.stage_states}
        invalid_targets = [
            item for item in fallback_ids if item not in stage_by_id or item == stage.stage_id
        ]
        exhausted_targets = [item for item in fallback_ids if item in attempted_targets]
        fallback = next(
            (
                item
                for item in fallback_ids
                if item in stage_by_id
                and item != stage.stage_id
                and item not in attempted_targets
            ),
            "",
        )

        if fallback:
            fallback_stage = stage_by_id[fallback]
            stages_to_reset = cls._stage_and_descendants(run, fallback)
            reset_stage_ids: list[str] = []
            for candidate_stage in run.stage_states:
                if candidate_stage.stage_id not in stages_to_reset:
                    continue
                if (
                    candidate_stage.status != "pending"
                    or candidate_stage.result_summary
                    or candidate_stage.artifact_refs
                    or candidate_stage.last_error
                ):
                    candidate_stage.metadata["superseded_status"] = candidate_stage.status
                    if candidate_stage.result_summary:
                        candidate_stage.metadata["superseded_result_summary"] = candidate_stage.result_summary
                    if candidate_stage.artifact_refs:
                        candidate_stage.metadata["superseded_artifact_refs"] = dict(candidate_stage.artifact_refs)
                    if candidate_stage.last_error:
                        candidate_stage.metadata["superseded_last_error"] = candidate_stage.last_error
                candidate_stage.status = "pending"
                candidate_stage.started_at = None
                candidate_stage.completed_at = None
                candidate_stage.result_summary = ""
                candidate_stage.artifact_refs = {}
                candidate_stage.last_error = ""
                candidate_stage.draft_result = {}
                candidate_stage.committed_result = {}
                candidate_stage.validation_report = {}
                candidate_stage.idempotency_key = ""
                reset_stage_ids.append(candidate_stage.stage_id)

            cls._invalidate_stage_outputs(run, reset_stage_ids)

            run.current_stage_id = fallback
            run.status = "running"
            fallback_history.append(
                {
                    "source_stage_id": stage.stage_id,
                    "target_stage_id": fallback,
                    "error": str(error or "")[:1200],
                }
            )
            run.metadata["fallback_history"] = fallback_history[-50:]
            stage.metadata["fallback_selected_stage_id"] = fallback
            stage.metadata["fallback_attempted_stage_ids"] = sorted(attempted_targets | {fallback})
            stage.metadata.pop("fallback_exhausted", None)
            run.append_event(
                kind="stage_failed_with_fallback",
                stage_id=stage.stage_id,
                summary=f"Stage failed: {str(error or 'unknown')[:200]}. Auto-fallback to {fallback}",
                payload={
                    "error": str(error or "")[:1200],
                    "fallback_to": fallback,
                    "fallback_candidates": fallback_ids,
                    "invalid_fallback_stage_ids": invalid_targets,
                    "exhausted_fallback_stage_ids": exhausted_targets,
                    "fallback_on_codes": sorted(configured_fallback_codes),
                    "observed_failure_codes": sorted(observed_failure_codes),
                    "reset_stage_ids": reset_stage_ids,
                },
            )
        else:
            run.status = "blocked"
            run.current_stage_id = stage.stage_id
            if fallback_ids:
                stage.metadata["fallback_exhausted"] = True
                stage.metadata["fallback_attempted_stage_ids"] = sorted(attempted_targets)
            run.append_event(
                kind=("stage_failed_fallback_exhausted" if fallback_ids else "stage_failed"),
                stage_id=stage.stage_id,
                summary=str(error or "stage failed")[:1200],
                payload={
                    "error": str(error or "")[:1200],
                    "fallback_candidates": fallback_ids,
                    "invalid_fallback_stage_ids": invalid_targets,
                    "exhausted_fallback_stage_ids": exhausted_targets,
                    "fallback_on_codes": sorted(configured_fallback_codes),
                    "observed_failure_codes": sorted(observed_failure_codes),
                },
            )

        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def resume_legacy_applicability_pause(
        cls,
        run_id: str,
        *,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Release a retired runtime-fit gate when an existing run is resumed.

        Preserve explicit user rejections, unrelated blockers, and completed
        upstream results. The original warning remains in metadata and events.
        """

        with cls._LOCK:
            run = cls._load(run_id, workdir)
            if run is None:
                raise KeyError(f"workflow run not found: {run_id}")
            state = dict(run.metadata.get("runtime_applicability") or {})
            if run.status != "blocked" or state.get("status") != "pending":
                return run
            stage_id = str(state.get("stage_id") or "").strip()
            if not stage_id or stage_id != run.current_stage_id:
                return run
            stage = cls._get_stage(run, stage_id)
            if stage.status != "blocked" or stage.last_error != state.get("reason"):
                return run
            stage.status = "pending"
            stage.started_at = None
            stage.completed_at = None
            stage.result_summary = ""
            stage.last_error = ""
            stage.metadata.pop("running_lease", None)
            stage.metadata.pop("applicability_override", None)
            run.status = "queued"
            state.update({
                "status": "retired",
                "resolved_by": "preflight_only_policy",
                "resolved_at": time.time(),
            })
            run.metadata["runtime_applicability"] = state
            run.append_event(
                kind="workflow_runtime_applicability_gate_retired",
                source="workflow",
                stage_id=stage_id,
                summary="适用性改为仅启动前检查，旧确认等待已解除",
                payload={"previous_status": "pending", "reason": state.get("reason")},
            )
            return cls._save(
                run,
                workdir,
                expected_version=run.version if expected_version is None else expected_version,
            )

    @classmethod
    def attach_candidate(
        cls,
        run_id: str,
        candidate: WorkflowCandidate | dict[str, Any],
        *,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Attach or replace one candidate card on the workflow run."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        normalized = (
            candidate
            if isinstance(candidate, WorkflowCandidate)
            else WorkflowCandidate.from_dict(candidate)
        )
        source_stage = next(
            (
                item
                for item in run.stage_states
                if item.stage_id == normalized.source_stage_id
            ),
            None,
        )
        if source_stage is None:
            raise ValueError(
                f"candidate source stage not found: {normalized.source_stage_id or '(empty)'}"
            )
        if source_stage.status != "committed":
            raise ValueError(
                "candidate source stage is not committed: "
                f"{source_stage.stage_id} ({source_stage.status})"
            )
        replaced = False
        for index, item in enumerate(run.candidates):
            if item.candidate_id == normalized.candidate_id:
                run.candidates[index] = normalized
                replaced = True
                break
        if not replaced:
            run.candidates.append(normalized)
        run.append_event(
            kind="candidate_upserted",
            stage_id=normalized.source_stage_id,
            summary=f"Candidate {'updated' if replaced else 'created'}: {normalized.candidate_id}",
            payload={"candidate_id": normalized.candidate_id, "replaced": replaced},
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def create_proposal(
        cls,
        run_id: str,
        *,
        proposal_type: str,
        payload: dict[str, Any],
        source_stage_id: str = "",
        proposed_by: str = "workflow",
        summary: str = "",
        raw_marker: str = "",
        metadata: Optional[dict[str, Any]] = None,
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Create a pending workflow proposal for operator review."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        normalized_type = str(proposal_type or "").strip()
        if normalized_type not in {"candidate", "revision", "final_report"}:
            raise ValueError("proposal_type must be candidate, revision, or final_report")
        normalized_source_stage_id = str(source_stage_id or "").strip()
        source_stage = next(
            (
                item
                for item in run.stage_states
                if item.stage_id == normalized_source_stage_id
            ),
            None,
        )
        if source_stage is None or source_stage.status != "committed":
            raise ValueError(
                "proposal source stage must be committed: "
                f"{normalized_source_stage_id or '(empty)'}"
            )
        proposal = WorkflowProposal(
            proposal_id=f"wfp-{uuid.uuid4().hex[:12]}",
            proposal_type=normalized_type,
            source_stage_id=normalized_source_stage_id,
            proposed_by=str(proposed_by or "workflow").strip() or "workflow",
            payload=dict(payload or {}),
            summary=str(summary or "").strip()[:1200],
            raw_marker=str(raw_marker or "")[:8192],
            metadata=dict(metadata or {}),
        )
        run.proposals.append(proposal)
        if proposal.proposal_type == "revision":
            run.status = "blocked"
            run.current_stage_id = proposal.source_stage_id or run.current_stage_id
            pending = run.metadata.get("pending_revision_proposal_ids")
            if not isinstance(pending, list):
                pending = []
            pending.append(proposal.proposal_id)
            run.metadata["pending_revision_proposal_ids"] = pending[-20:]
        run.append_event(
            kind="proposal_created",
            stage_id=proposal.source_stage_id,
            summary=f"Proposal created: {proposal.proposal_type}",
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
            },
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def resolve_proposal(
        cls,
        run_id: str,
        proposal_id: str,
        *,
        action: str,
        actor: str = "operator",
        reason: str = "",
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Approve or reject one proposal and apply approved payloads."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        normalized_id = str(proposal_id or "").strip()
        proposal = next((item for item in run.proposals if item.proposal_id == normalized_id), None)
        if proposal is None:
            raise KeyError(f"workflow proposal not found: {normalized_id}")
        if proposal.status != "pending":
            raise ValueError(f"workflow proposal already resolved: {proposal.status}")

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"approve", "reject"}:
            raise ValueError("proposal action must be approve or reject")

        if normalized_action == "approve":
            proposal.resolve("approved", resolved_by=actor, reason=reason)
            cls._apply_approved_proposal(run, proposal)
            event_kind = "proposal_approved"
        else:
            proposal.resolve("rejected", resolved_by=actor, reason=reason)
            event_kind = "proposal_rejected"

        pending = run.metadata.get("pending_revision_proposal_ids")
        if isinstance(pending, list):
            run.metadata["pending_revision_proposal_ids"] = [
                item for item in pending if str(item or "").strip() != proposal.proposal_id
            ]
        if (
            normalized_action == "reject"
            and proposal.proposal_type == "revision"
            and not run.metadata.get("pending_revision_proposal_ids")
            and run.status == "blocked"
        ):
            next_stage_id = run.next_pending_stage_id()
            run.current_stage_id = next_stage_id or run.current_stage_id
            run.status = "completed" if not next_stage_id else "running"
        run.append_event(
            kind=event_kind,
            stage_id=proposal.source_stage_id,
            summary=f"Proposal {proposal.proposal_id} {proposal.status}",
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "reason": reason,
            },
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def auto_approve_loop_revision(
        cls,
        run_id: str,
        *,
        source_stage_id: str,
        max_iterations: int,
        actor: str = "loop-controller",
        workdir: Optional[str | Path] = None,
        expected_version: Optional[int] = None,
    ) -> WorkflowRun:
        """Approve the newest pending revision while enforcing a bounded loop budget."""

        run = cls._load(run_id, workdir)
        if run is None:
            raise KeyError(f"workflow run not found: {run_id}")
        normalized_source = str(source_stage_id or "").strip()
        pending = [
            proposal
            for proposal in run.proposals
            if proposal.proposal_type == "revision"
            and proposal.status == "pending"
            and (
                not normalized_source
                or proposal.source_stage_id == normalized_source
            )
        ]
        if not pending:
            return run

        try:
            current_iteration = int(run.metadata.get("loop_iteration", 0) or 0)
        except (TypeError, ValueError):
            current_iteration = 0
        try:
            bounded_max = int(max_iterations or 1)
        except (TypeError, ValueError):
            bounded_max = 1
        current_iteration = max(0, current_iteration)
        bounded_max = max(1, min(20, bounded_max))
        proposal = pending[-1]
        target_stage_id = cls._proposal_revision_target(run, proposal)
        if current_iteration >= bounded_max:
            run.status = "blocked"
            run.current_stage_id = normalized_source or proposal.source_stage_id or run.current_stage_id
            run.metadata.update(
                {
                    "loop_status": "budget_exhausted",
                    "loop_iteration": current_iteration,
                    "loop_max_iterations": bounded_max,
                    "loop_escalation_reason": "revision budget exhausted",
                }
            )
            run.append_event(
                kind="loop_budget_exhausted",
                stage_id=run.current_stage_id,
                summary=f"Loop revision budget exhausted at {current_iteration}/{bounded_max}",
                payload={
                    "proposal_id": proposal.proposal_id,
                    "target_stage_id": target_stage_id,
                    "iteration": current_iteration,
                    "max_iterations": bounded_max,
                },
            )
            return cls._save(run, workdir, expected_version=expected_version)

        next_iteration = current_iteration + 1
        proposal.resolve(
            "approved",
            resolved_by=actor,
            reason=f"automatic loop revision {next_iteration}/{bounded_max}",
        )
        cls._apply_approved_proposal(run, proposal)
        pending_ids = run.metadata.get("pending_revision_proposal_ids")
        if isinstance(pending_ids, list):
            run.metadata["pending_revision_proposal_ids"] = [
                item
                for item in pending_ids
                if str(item or "").strip() != proposal.proposal_id
            ]
        history = run.metadata.get("loop_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "iteration": next_iteration,
                "proposal_id": proposal.proposal_id,
                "source_stage_id": proposal.source_stage_id,
                "target_stage_id": target_stage_id,
                "summary": proposal.summary,
            }
        )
        run.metadata.update(
            {
                "loop_status": "running",
                "loop_iteration": next_iteration,
                "loop_max_iterations": bounded_max,
                "loop_history": history[-bounded_max:],
            }
        )
        run.append_event(
            kind="loop_iteration_started",
            stage_id=target_stage_id,
            summary=f"Loop revision {next_iteration}/{bounded_max} started",
            payload={
                "proposal_id": proposal.proposal_id,
                "source_stage_id": proposal.source_stage_id,
                "target_stage_id": target_stage_id,
                "iteration": next_iteration,
                "max_iterations": bounded_max,
            },
        )
        run.append_event(
            kind="proposal_approved",
            stage_id=proposal.source_stage_id,
            summary=f"Proposal {proposal.proposal_id} approved by loop controller",
            payload={
                "proposal_id": proposal.proposal_id,
                "proposal_type": proposal.proposal_type,
                "reason": proposal.reason,
            },
        )
        return cls._save(run, workdir, expected_version=expected_version)

    @classmethod
    def _apply_approved_proposal(cls, run: WorkflowRun, proposal: WorkflowProposal) -> None:
        payload = dict(proposal.payload or {})
        if proposal.proposal_type == "candidate":
            nested_candidate = payload.get("candidate")
            candidate_payload = (
                dict(nested_candidate)
                if isinstance(nested_candidate, dict)
                else dict(payload)
            )
            candidate_payload.pop("candidate", None)
            if not str(candidate_payload.get("candidate_id") or "").strip():
                candidate_payload["candidate_id"] = f"{run.run_id}-cand-{len(run.candidates) + 1}"
            if proposal.source_stage_id and not candidate_payload.get("source_stage_id"):
                candidate_payload["source_stage_id"] = proposal.source_stage_id
            candidate = WorkflowCandidate.from_dict(candidate_payload)
            replaced = False
            for index, item in enumerate(run.candidates):
                if item.candidate_id == candidate.candidate_id:
                    run.candidates[index] = candidate
                    replaced = True
                    break
            if not replaced:
                run.candidates.append(candidate)
            run.append_event(
                kind="candidate_upserted",
                stage_id=candidate.source_stage_id,
                summary=f"Candidate approved from proposal: {candidate.candidate_id}",
                payload={"candidate_id": candidate.candidate_id, "proposal_id": proposal.proposal_id},
            )
            return

        if proposal.proposal_type == "final_report":
            report = str(
                payload.get("final_report")
                or payload.get("report")
                or payload.get("summary")
                or proposal.summary
                or ""
            ).strip()
            if report:
                run.metadata["final_report"] = report[:4000]
                run.metadata["final_report_proposal_id"] = proposal.proposal_id
            return

        if proposal.proposal_type == "revision":
            revisions = run.metadata.get("approved_revision_requests")
            if not isinstance(revisions, list):
                revisions = []
            target_stage_id = cls._proposal_revision_target(run, proposal)
            revisions.append(
                {
                    "proposal_id": proposal.proposal_id,
                    "source_stage_id": proposal.source_stage_id,
                    "target_stage_id": target_stage_id,
                    "payload": payload,
                    "summary": proposal.summary,
                    "approved_at": proposal.resolved_at,
                }
            )
            run.metadata["approved_revision_requests"] = revisions
            cls._rewind_run_to_stage(
                run,
                target_stage_id,
                proposal=proposal,
            )
