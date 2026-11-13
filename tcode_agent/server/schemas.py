from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class PermissionReplyRequest(BaseModel):
    session_id: str
    request_id: str
    response: str


class QuestionReplyRequest(BaseModel):
    session_id: str
    request_id: str = ''
    answer: str = ''
    answers: dict[str, str] | None = None


class OutcomeConfirmationRequest(BaseModel):
    session_id: str
    message_id: str
    decision: Literal["accept", "modify"]
    feedback: Optional[str] = None
    client_request_id: str
    max_repair_attempts: Optional[int] = None


class ChatGuideRequest(BaseModel):
    session_id: str
    message: str
    mode: Optional[Literal["steer", "queue"]] = None
    client_request_id: Optional[str] = None
    target_turn_id: Optional[str] = None
    apply_policy: Literal["next_safe_point", "after_current_tool"] = "next_safe_point"
    descendant_policy: Literal[
        "preserve", "cancel_invalidated", "cancel_all"
    ] = "preserve"
    supersedes_guidance_id: Optional[str] = None
    file_refs: Optional[list[dict[str, Any]]] = None
    interrupt: Optional[bool] = False
    cancel_descendants: Optional[bool] = False
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None


class SelectModelRequest(BaseModel):
    model: str
    session_id: Optional[str] = None


class SubagentChatRequest(BaseModel):
    parent_session_id: str
    subagent_session_id: str
    message: str
    integrate_to_parent: Optional[bool] = None
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None
    background: bool = False


class SideQuestionRequest(BaseModel):
    parent_session_id: str
    message: str = ""
    file_refs: Optional[list[dict[str, Any]]] = None
    agent_name: Optional[str] = None
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None
    create_only: bool = False
    source_message_id: str = ""


class SymphonyIssueRequest(BaseModel):
    title: Optional[str] = None
    description: str
    project_slug: Optional[str] = None


class SymphonyLocalIssueRequest(BaseModel):
    title: Optional[str] = None
    description: str


class SymphonyLocalIssueUpdateRequest(BaseModel):
    id: Optional[str] = None
    identifier: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    state: Optional[str] = None
    labels: Optional[list[str]] = None


class SymphonyLocalIssueDeleteRequest(BaseModel):
    id: Optional[str] = None
    identifier: Optional[str] = None
    forget_unresolved_submission: bool = False


class SymphonyLocalIssueBatchDeleteRequest(BaseModel):
    issues: list[SymphonyLocalIssueDeleteRequest]
    cancel_active: bool = True


class SymphonyJobCallbackRequest(BaseModel):
    identifier: str
    state: str = "Done"
    result_summary: Optional[str] = None


class HumanReviewSubmitRequest(BaseModel):
    issue_id: Optional[str] = None
    issue_identifier: Optional[str] = None
    tool_server: str
    tool_name: str
    tool_args: dict[str, Any]
    reason: Optional[str] = None
    review_data: Optional[dict[str, Any]] = None


class HumanReviewApproveRequest(BaseModel):
    issue_id: Optional[str] = None
    issue_identifier: Optional[str] = None
    comments: Optional[str] = None
    approved_tool_args_override: Optional[dict[str, Any]] = None
    resume_after_approval: Optional[bool] = True
    resume_instruction: Optional[str] = None
    apply_feedback_before_execution: Optional[bool] = None


class HumanReviewRejectRequest(BaseModel):
    issue_id: Optional[str] = None
    issue_identifier: Optional[str] = None
    reason: str
    comments: Optional[str] = None
    resume_after_rejection: Optional[bool] = True
    resume_instruction: Optional[str] = None


class HumanReviewDeleteRequest(BaseModel):
    issue_id: Optional[str] = None
    issue_identifier: Optional[str] = None


class JobPreviewTicketRequest(BaseModel):
    port: int


class JobActionRequest(BaseModel):
    reuse_issue: Optional[bool] = True
    reason: Optional[str] = None


class TrackerTaskCreateRequest(BaseModel):
    task: dict[str, Any]
    source: Optional[str] = "api"
    append_event: Optional[bool] = True


class TrackerClaimNextRequest(BaseModel):
    worker_id: str
    channel: str
    lease_seconds: Optional[int] = 30
    allowed_servers: Optional[list[str]] = None


class TrackerHeartbeatRequest(BaseModel):
    claim_token: str
    worker_id: str
    lease_seconds: Optional[int] = 30
    status: Optional[str] = None
    workspace_patch: Optional[dict[str, Any]] = None
    result_summary: Optional[str] = None
    error_summary: Optional[str] = None


class TrackerAttachJobRefRequest(BaseModel):
    claim_token: str
    worker_id: Optional[str] = None
    job_ref: dict[str, Any]
    workspace_patch: Optional[dict[str, Any]] = None


class TrackerCompleteRequest(BaseModel):
    claim_token: str
    result_summary: Optional[str] = None
    workspace_patch: Optional[dict[str, Any]] = None
    job_ref: Optional[dict[str, Any]] = None


class TrackerFailRequest(BaseModel):
    claim_token: str
    error_summary: Optional[str] = None
    workspace_patch: Optional[dict[str, Any]] = None
    job_ref: Optional[dict[str, Any]] = None


class TrackerCancelRequest(BaseModel):
    claim_token: str
    result_summary: Optional[str] = None
    workspace_patch: Optional[dict[str, Any]] = None
    source: Optional[str] = "api"


class TrackerExecutionReportRequest(BaseModel):
    status: str
    result_summary: Optional[str] = None
    error_summary: Optional[str] = None
    workspace_patch: Optional[dict[str, Any]] = None
    metadata_patch: Optional[dict[str, Any]] = None
    job_ref: Optional[dict[str, Any]] = None
    source: Optional[str] = "symphony_adapter"


class BootstrapSessionRequest(BaseModel):
    agent_name: str
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None


class WorkdirCreateRequest(BaseModel):
    name: str


class WorkflowTemplateUpsertRequest(BaseModel):
    template: dict[str, Any]
    overwrite: Optional[bool] = False


class WorkflowDraftCreateRequest(BaseModel):
    goal: str
    session_id: str
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    model: Optional[str] = None
    template: Optional[dict[str, Any]] = None


class WorkflowDraftUpdateRequest(BaseModel):
    name: str
    template: dict[str, Any]
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None


class WorkflowDraftActionRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    input_payload: Optional[dict[str, Any]] = None
    template_key: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    overwrite: Optional[bool] = False


class WorkflowRunCreateRequest(BaseModel):
    template_key: str
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    input_payload: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    run_id: Optional[str] = None


class WorkflowStageTransitionRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None
    status: str
    result_summary: Optional[str] = None
    artifact_refs: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class WorkflowStageExecuteRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None


class WorkflowRunTerminateRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None
    reason: Optional[str] = None
    actor: Optional[str] = "operator"


class WorkflowRunRerunRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None
    reason: Optional[str] = None
    actor: Optional[str] = "operator"
    auto_continue: Optional[bool] = True


class WorkflowCandidateUpsertRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None
    candidate: dict[str, Any]


class WorkflowProposalActionRequest(BaseModel):
    session_id: Optional[str] = None
    workdir_name: Optional[str] = None
    workdir_path: Optional[str] = None
    expected_version: Optional[int] = None
    reason: Optional[str] = None
    actor: Optional[str] = "operator"


class TeamPeerSettingsRequest(BaseModel):
    enabled: bool


class TeamChatRequest(BaseModel):
    worker_name: str
    message: str
    output_format: Optional[dict[str, Any]] = None
    outcome_contract: Optional[dict[str, Any]] = None


class TeamRouteInspectRequest(BaseModel):
    message: str
    requested_agent_name: str = "manager"
    requested_execution_profile: str = "orchestrated"
