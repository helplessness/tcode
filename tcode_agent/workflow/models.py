"""Core models for template-driven workflow execution."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


def _timestamp() -> float:
    """Return the current timestamp in seconds."""

    return datetime.now().timestamp()


def _truthy(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        return default
    return bool(value)


_PROMPT_CONTENT_METADATA_KEYS = {"prompt", "prompt_markdown", "prompt_sections"}

COMMITTED_STAGE_STATUSES = {"committed"}


def _without_prompt_content(metadata: dict[str, Any]) -> dict[str, Any]:
    data = dict(metadata or {})
    for key in _PROMPT_CONTENT_METADATA_KEYS:
        data.pop(key, None)
    return data


@dataclass(slots=True)
class WorkflowStageTemplate:
    """Declarative definition of a workflow stage."""

    stage_id: str
    label: str
    owner: str
    call_type: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    input_contract: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    expected_artifacts: list[str] = field(default_factory=list)
    success_checks: list[str] = field(default_factory=list)
    validators: list[dict[str, Any]] = field(default_factory=list)
    retry_policy: dict[str, Any] = field(default_factory=dict)
    timeout_policy: dict[str, Any] = field(default_factory=dict)
    failure_policy: dict[str, Any] = field(default_factory=dict)
    fallback_stage_ids: list[str] = field(default_factory=list)
    review_gate: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowStageTemplate":
        """Build a stage template from a plain dictionary."""

        data = dict(payload or {})
        metadata = dict(data.get("metadata", {}) or {})
        for key in (
            "prompt_file",
            "prompt",
            "prompt_markdown",
            "prompt_sections",
            "prompt_title",
            "prompt_source",
        ):
            if key in data and key not in metadata:
                metadata[key] = data.get(key)
        return cls(
            stage_id=str(data.get("stage_id", "") or "").strip(),
            label=str(data.get("label", "") or "").strip(),
            owner=str(data.get("owner", "") or "").strip(),
            call_type=str(data.get("call_type", "") or "").strip(),
            description=str(data.get("description", "") or "").strip(),
            depends_on=[str(item).strip() for item in data.get("depends_on", []) or [] if str(item).strip()],
            allowed_tools=[str(item).strip() for item in data.get("allowed_tools", []) or [] if str(item).strip()],
            required_skills=[str(item).strip() for item in data.get("required_skills", []) or [] if str(item).strip()],
            input_contract=dict(data.get("input_contract", {}) or {}),
            output_contract=dict(data.get("output_contract", {}) or {}),
            expected_artifacts=[str(item).strip() for item in data.get("expected_artifacts", []) or [] if str(item).strip()],
            success_checks=[str(item).strip() for item in data.get("success_checks", []) or [] if str(item).strip()],
            validators=[dict(item) for item in data.get("validators", []) or [] if isinstance(item, dict)],
            retry_policy=dict(data.get("retry_policy", {}) or {}),
            timeout_policy=dict(data.get("timeout_policy", {}) or {}),
            failure_policy=dict(data.get("failure_policy", {}) or {}),
            fallback_stage_ids=[str(item).strip() for item in data.get("fallback_stage_ids", []) or [] if str(item).strip()],
            review_gate=bool(data.get("review_gate", False)),
            metadata=metadata,
        )

    def to_dict(self, *, include_prompt_content: bool = True) -> dict[str, Any]:
        """Serialize the stage template to a plain dictionary."""

        data = asdict(self)
        if not include_prompt_content:
            data["metadata"] = _without_prompt_content(data.get("metadata", {}) or {})
        return data


@dataclass(slots=True)
class WorkflowTemplate:
    """Declarative workflow template definition."""

    template_key: str
    display_name: str
    domain: str
    description: str = ""
    input_contract: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    stage_defaults: dict[str, Any] = field(default_factory=dict)
    stages: list[WorkflowStageTemplate] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowTemplate":
        """Build a workflow template from a plain dictionary."""

        data = dict(payload or {})
        metadata = dict(data.get("metadata", {}) or {})
        stage_defaults = dict(data.get("stage_defaults", {}) or {})
        stage_items: list[WorkflowStageTemplate] = []
        for raw_stage in data.get("stages", []) or []:
            if not isinstance(raw_stage, dict):
                continue
            merged_stage = {**stage_defaults, **raw_stage}
            stage = WorkflowStageTemplate.from_dict(merged_stage)
            stage_items.append(stage)
        return cls(
            template_key=str(data.get("template_key", "") or "").strip(),
            display_name=str(data.get("display_name", "") or "").strip(),
            domain=str(data.get("domain", "") or "").strip(),
            description=str(data.get("description", "") or "").strip(),
            input_contract=dict(data.get("input_contract", {}) or {}),
            output_contract=dict(data.get("output_contract", {}) or {}),
            stage_defaults=stage_defaults,
            stages=stage_items,
            metadata=metadata,
            enabled=_truthy(data.get("enabled", metadata.get("enabled", True)), default=True),
        )

    def to_dict(self, *, include_prompt_content: bool = True) -> dict[str, Any]:
        """Serialize the workflow template to a plain dictionary."""

        return {
            "template_key": self.template_key,
            "display_name": self.display_name,
            "domain": self.domain,
            "description": self.description,
            "input_contract": dict(self.input_contract or {}),
            "output_contract": dict(self.output_contract or {}),
            "stage_defaults": dict(self.stage_defaults or {}),
            "stages": [
                item.to_dict(include_prompt_content=include_prompt_content)
                for item in self.stages
            ],
            "metadata": dict(self.metadata or {}),
            "enabled": bool(self.enabled),
        }


@dataclass(slots=True)
class WorkflowStageState:
    """Runtime state for a single workflow stage."""

    stage_id: str
    label: str
    owner: str
    call_type: str
    status: str = "pending"
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    artifact_refs: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    last_error: str = ""
    attempt: int = 0
    draft_result: dict[str, Any] = field(default_factory=dict)
    committed_result: dict[str, Any] = field(default_factory=dict)
    validation_report: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_template(cls, template: WorkflowStageTemplate) -> "WorkflowStageState":
        """Create an initial stage state from a stage template."""

        return cls(
            stage_id=template.stage_id,
            label=template.label,
            owner=template.owner,
            call_type=template.call_type,
            metadata={
                "allowed_tools": list(template.allowed_tools),
                "required_skills": list(template.required_skills),
                "depends_on": list(template.depends_on),
                "input_contract": dict(template.input_contract),
                "output_contract": dict(template.output_contract),
                "expected_artifacts": list(template.expected_artifacts),
                "success_checks": list(template.success_checks),
                "validators": [dict(item) for item in template.validators],
                "retry_policy": dict(template.retry_policy),
                "timeout_policy": dict(template.timeout_policy),
                "failure_policy": dict(template.failure_policy),
                "fallback_stage_ids": list(template.fallback_stage_ids),
                "review_gate": bool(template.review_gate),
                **_without_prompt_content(template.metadata or {}),
            },
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowStageState":
        """Build a stage state from persisted JSON content."""

        data = dict(payload or {})
        return cls(
            stage_id=str(data.get("stage_id", "") or "").strip(),
            label=str(data.get("label", "") or "").strip(),
            owner=str(data.get("owner", "") or "").strip(),
            call_type=str(data.get("call_type", "") or "").strip(),
            status=str(data.get("status", "pending") or "pending").strip(),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            artifact_refs=dict(data.get("artifact_refs", {}) or {}),
            result_summary=str(data.get("result_summary", "") or "").strip(),
            last_error=str(data.get("last_error", "") or "").strip(),
            attempt=max(0, int(data.get("attempt", 0) or 0)),
            draft_result=dict(data.get("draft_result", {}) or {}),
            committed_result=dict(data.get("committed_result", {}) or {}),
            validation_report=dict(data.get("validation_report", {}) or {}),
            idempotency_key=str(data.get("idempotency_key", "") or "").strip(),
            metadata=dict(data.get("metadata", {}) or {}),
        )

    def mark_running(self) -> None:
        """Transition the stage into running state."""

        if self.draft_result:
            # 重试保留最近一次失败草稿，避免清空后丢失用户可查看的已有结果。
            self.metadata["previous_attempt_result"] = {
                "attempt": self.attempt, "draft_result": self.draft_result,
                "validation_report": self.validation_report, "last_error": self.last_error,
            }
        self.status = "running"
        self.started_at = _timestamp()
        self.completed_at = None
        self.attempt += 1
        self.idempotency_key = f"{self.stage_id}:attempt:{self.attempt}"
        self.draft_result = {}
        self.validation_report = {}
        self.last_error = ""

    def finish_running_lease(self, final_state: str) -> None:
        """Close the active running lease, if one is present."""

        lease = self.metadata.pop("running_lease", None)
        if not isinstance(lease, dict):
            return
        lease = dict(lease)
        lease["finished_at"] = _timestamp()
        lease["finished_state"] = str(final_state or self.status or "").strip()
        self.metadata["last_running_lease"] = lease
        history = list(self.metadata.get("attempt_history") or [])
        history.append(lease)
        self.metadata["attempt_history"] = history[-20:]

    def mark_waiting(
        self,
        *,
        status: str,
        result_summary: str = "",
        artifact_refs: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Transition the stage into a non-terminal waiting state."""

        self.finish_running_lease(status)
        self.status = str(status or "waiting_external_job").strip() or "waiting_external_job"
        if self.started_at is None:
            self.started_at = _timestamp()
        if result_summary:
            self.result_summary = str(result_summary or "")[:1200]
        if artifact_refs:
            self.artifact_refs.update(dict(artifact_refs))
        if metadata:
            self.metadata.update(dict(metadata))

    def mark_validating(
        self,
        *,
        draft_result: Optional[dict[str, Any]] = None,
        validation_report: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist an uncommitted stage result while validators run."""

        self.finish_running_lease("validating")
        self.status = "validating"
        if draft_result is not None:
            self.draft_result = dict(draft_result)
        if validation_report is not None:
            self.validation_report = dict(validation_report)

    def mark_committed(
        self,
        *,
        result_summary: str = "",
        artifact_refs: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Commit a validated stage result for downstream consumption."""

        self.finish_running_lease("committed")
        self.status = "committed"
        self.completed_at = _timestamp()
        self.result_summary = str(result_summary or "")[:1200]
        self.last_error = ""
        self.committed_result = dict(self.draft_result or {})
        if artifact_refs:
            self.artifact_refs = dict(artifact_refs)
        if metadata:
            self.metadata.update(dict(metadata))

    def mark_failed(self, error: str) -> None:
        """Transition the stage into failed state."""

        self.finish_running_lease("failed")
        self.status = "failed"
        self.completed_at = _timestamp()
        self.last_error = str(error or "")[:1200]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stage state to a plain dictionary."""

        data = asdict(self)
        data["metadata"] = _without_prompt_content(data.get("metadata", {}) or {})
        if self.last_error or self.status in {"failed", "blocked"} or self.metadata.get("review_decision") in {"REVISE", "ESCALATE"}:
            from .diagnostics import workflow_diagnostic
            data["diagnostic"] = workflow_diagnostic(self.last_error, self.validation_report, review_decision=str(self.metadata.get("review_decision") or ""))
            data["diagnostic"]["available_results"] = {
                "committed": bool(self.committed_result),
                "draft": bool(self.draft_result),
                "artifacts": bool(self.artifact_refs or self.draft_result.get("artifact_refs")),
            }
        return data


@dataclass(slots=True)
class WorkflowCandidate:
    """Unified candidate summary emitted by workflow stages."""

    candidate_id: str
    source_stage_id: str
    status: str = "draft"
    rank: Optional[int] = None
    score_card: dict[str, Any] = field(default_factory=dict)
    artifact_refs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowCandidate":
        """Build a candidate from persisted JSON content."""

        data = dict(payload or {})
        return cls(
            candidate_id=str(data.get("candidate_id", "") or "").strip(),
            source_stage_id=str(data.get("source_stage_id", "") or "").strip(),
            status=str(data.get("status", "draft") or "draft").strip(),
            rank=data.get("rank"),
            score_card=dict(data.get("score_card", {}) or {}),
            artifact_refs=dict(data.get("artifact_refs", {}) or {}),
            summary=str(data.get("summary", "") or "").strip(),
            metadata=dict(data.get("metadata", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the candidate to a plain dictionary."""

        return asdict(self)


@dataclass(slots=True)
class WorkflowProposal:
    """Operator-reviewable proposal emitted by workflow workers."""

    proposal_id: str
    proposal_type: str
    source_stage_id: str = ""
    status: str = "pending"
    proposed_by: str = "workflow"
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    raw_marker: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_timestamp)
    resolved_at: Optional[float] = None
    resolved_by: str = ""
    reason: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowProposal":
        """Build a proposal from persisted JSON content."""

        data = dict(payload or {})
        return cls(
            proposal_id=str(data.get("proposal_id", "") or "").strip() or f"wfp-{uuid.uuid4().hex[:10]}",
            proposal_type=str(data.get("proposal_type", "") or "").strip(),
            source_stage_id=str(data.get("source_stage_id", "") or "").strip(),
            status=str(data.get("status", "pending") or "pending").strip(),
            proposed_by=str(data.get("proposed_by", "workflow") or "workflow").strip(),
            payload=dict(data.get("payload", {}) or {}),
            summary=str(data.get("summary", "") or "").strip(),
            raw_marker=str(data.get("raw_marker", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", _timestamp()) or _timestamp()),
            resolved_at=data.get("resolved_at"),
            resolved_by=str(data.get("resolved_by", "") or "").strip(),
            reason=str(data.get("reason", "") or "").strip(),
        )

    def resolve(self, status: str, *, resolved_by: str = "", reason: str = "") -> None:
        """Resolve the proposal as approved or rejected."""

        self.status = str(status or "").strip() or self.status
        self.resolved_at = _timestamp()
        self.resolved_by = str(resolved_by or "").strip()
        self.reason = str(reason or "").strip()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the proposal to a plain dictionary."""

        return asdict(self)


@dataclass(slots=True)
class WorkflowEvent:
    """Small immutable-ish event entry for workflow audit timelines."""

    event_id: str
    kind: str
    source: str = "workflow"
    stage_id: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_timestamp)

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        source: str = "workflow",
        stage_id: str = "",
        summary: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> "WorkflowEvent":
        """Create a new workflow timeline event."""

        return cls(
            event_id=f"wfe-{uuid.uuid4().hex[:12]}",
            kind=str(kind or "").strip(),
            source=str(source or "workflow").strip() or "workflow",
            stage_id=str(stage_id or "").strip(),
            summary=str(summary or "").strip()[:1200],
            payload=dict(payload or {}),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowEvent":
        """Build an event from persisted JSON content."""

        data = dict(payload or {})
        return cls(
            event_id=str(data.get("event_id", "") or "").strip() or f"wfe-{uuid.uuid4().hex[:12]}",
            kind=str(data.get("kind", "") or "").strip(),
            source=str(data.get("source", "workflow") or "workflow").strip(),
            stage_id=str(data.get("stage_id", "") or "").strip(),
            summary=str(data.get("summary", "") or "").strip(),
            payload=dict(data.get("payload", {}) or {}),
            created_at=float(data.get("created_at", _timestamp()) or _timestamp()),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event to a plain dictionary."""

        return asdict(self)


@dataclass(slots=True)
class WorkflowRun:
    """Persisted runtime state for one template-driven workflow execution."""

    run_id: str
    template_key: str
    session_id: str
    status: str = "draft"
    current_stage_id: str = ""
    input_payload: dict[str, Any] = field(default_factory=dict)
    stage_states: list[WorkflowStageState] = field(default_factory=list)
    candidates: list[WorkflowCandidate] = field(default_factory=list)
    proposals: list[WorkflowProposal] = field(default_factory=list)
    events: list[WorkflowEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: float = field(default_factory=_timestamp)
    updated_at: float = field(default_factory=_timestamp)

    @classmethod
    def from_template(
        cls,
        *,
        run_id: str,
        template: WorkflowTemplate,
        session_id: str,
        input_payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "WorkflowRun":
        """Create a new workflow run from a template."""

        stage_states = [WorkflowStageState.from_template(item) for item in template.stages]
        current_stage_id = next(
            (
                stage.stage_id
                for stage in stage_states
                if not (stage.metadata or {}).get("depends_on")
            ),
            "",
        )
        return cls(
            run_id=run_id,
            template_key=template.template_key,
            session_id=session_id,
            status="queued",
            current_stage_id=current_stage_id,
            input_payload=dict(input_payload or {}),
            stage_states=stage_states,
            events=[
                WorkflowEvent.create(
                    kind="run_created",
                    summary=f"Created workflow run from template {template.template_key}",
                    payload={"template_key": template.template_key},
                )
            ],
            metadata={
                "template_display_name": template.display_name,
                "domain": template.domain,
                **dict(template.metadata or {}),
                **dict(metadata or {}),
            },
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowRun":
        """Build a workflow run from persisted JSON content."""

        data = dict(payload or {})
        return cls(
            run_id=str(data.get("run_id", "") or "").strip(),
            template_key=str(data.get("template_key", "") or "").strip(),
            session_id=str(data.get("session_id", "") or "").strip(),
            status=str(data.get("status", "draft") or "draft").strip(),
            current_stage_id=str(data.get("current_stage_id", "") or "").strip(),
            input_payload=dict(data.get("input_payload", {}) or {}),
            stage_states=[
                WorkflowStageState.from_dict(item)
                for item in data.get("stage_states", []) or []
                if isinstance(item, dict)
            ],
            candidates=[
                WorkflowCandidate.from_dict(item)
                for item in data.get("candidates", []) or []
                if isinstance(item, dict)
            ],
            proposals=[
                WorkflowProposal.from_dict(item)
                for item in data.get("proposals", []) or []
                if isinstance(item, dict)
            ],
            events=[
                WorkflowEvent.from_dict(item)
                for item in data.get("events", []) or []
                if isinstance(item, dict)
            ],
            metadata=dict(data.get("metadata", {}) or {}),
            version=max(1, int(data.get("version", 1) or 1)),
            created_at=float(data.get("created_at", _timestamp()) or _timestamp()),
            updated_at=float(data.get("updated_at", _timestamp()) or _timestamp()),
        )

    def touch(self) -> None:
        """Update the run modification timestamp."""

        self.updated_at = _timestamp()

    def append_event(
        self,
        *,
        kind: str,
        source: str = "workflow",
        stage_id: str = "",
        summary: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append a bounded event entry to the workflow timeline."""

        if not kind:
            return
        self.events.append(
            WorkflowEvent.create(
                kind=kind,
                source=source,
                stage_id=stage_id,
                summary=summary,
                payload=payload,
            )
        )
        if len(self.events) > 500:
            self.events = self.events[-500:]

    def next_pending_stage_id(self) -> str:
        """Return the next dependency-ready pending stage identifier, if any."""

        committed = {
            stage.stage_id
            for stage in self.stage_states
            if stage.status in COMMITTED_STAGE_STATUSES
        }
        for stage in self.stage_states:
            dependencies = [
                str(item or "").strip()
                for item in (stage.metadata or {}).get("depends_on", []) or []
                if str(item or "").strip()
            ]
            if stage.status == "pending" and all(item in committed for item in dependencies):
                return stage.stage_id
        return ""

    def dependencies_committed(self, stage_id: str) -> tuple[bool, list[str]]:
        """Return whether a stage may consume all declared upstream outputs."""

        stage = next((item for item in self.stage_states if item.stage_id == stage_id), None)
        if stage is None:
            return False, [str(stage_id or "")]
        committed = {
            item.stage_id
            for item in self.stage_states
            if item.status in COMMITTED_STAGE_STATUSES
        }
        missing = [
            str(item or "").strip()
            for item in (stage.metadata or {}).get("depends_on", []) or []
            if str(item or "").strip() and str(item or "").strip() not in committed
        ]
        return not missing, missing

    def to_dict(self) -> dict[str, Any]:
        """Serialize the workflow run to a plain dictionary."""

        return {
            "run_id": self.run_id,
            "template_key": self.template_key,
            "session_id": self.session_id,
            "status": self.status,
            "current_stage_id": self.current_stage_id,
            "input_payload": dict(self.input_payload or {}),
            "stage_states": [item.to_dict() for item in self.stage_states],
            "candidates": [item.to_dict() for item in self.candidates],
            "proposals": [item.to_dict() for item in self.proposals],
            "events": [item.to_dict() for item in self.events],
            "metadata": dict(self.metadata or {}),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
