import type { SymphonyLocalIssue } from '../api/symphony';

export const issueListSignature = (items: SymphonyLocalIssue[]) => JSON.stringify(
  items.map((issue) => ({
    id: issue.id,
    identifier: issue.identifier,
    state: issue.state,
    tracker_status: issue.tracker_status,
    updated_at: issue.updated_at,
    result_summary: issue.result_summary,
    error_summary: issue.error_summary,
    // The watcher can update the workspace after the cancellation response.
    workspace: issue.workspace && {
      lifecycle_status: issue.workspace.lifecycle_status,
      status: issue.workspace.status,
      tracker_status: issue.workspace.tracker_status,
      cancelled_at: issue.workspace.cancelled_at,
      completed_at: issue.workspace.completed_at,
      action_policy: issue.workspace.action_policy,
      cancellation: issue.workspace.cancellation,
      recovery: issue.workspace.recovery,
      transfer_state: issue.workspace.transfer_state,
      job_id: issue.workspace.job_id,
      job_ref: issue.workspace.job_ref && {
        job_id: issue.workspace.job_ref.job_id,
        process_id: issue.workspace.job_ref.process_id,
      },
      artifacts: issue.workspace.artifacts,
      artifact_entries: issue.workspace.artifact_entries,
    },
    timeline: issue.timeline,
  })),
);
