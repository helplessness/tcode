import type { SymphonyLocalIssue } from '../api/symphony';

export function hasAcceptedSubmission(issue?: SymphonyLocalIssue | null): boolean {
  return !!String(issue?.workspace?.job_ref?.job_id || issue?.workspace?.job_id || '').trim();
}

export function isDispatchingSubmission(issue?: SymphonyLocalIssue | null): boolean {
  const state = String(issue?.state || '').trim().toLowerCase();
  const tracker = String(issue?.tracker_status || '').trim().toLowerCase();
  if (['done', 'completed', 'closed', 'failed', 'error', 'cancelled', 'canceled', 'duplicate'].includes(state)
    || ['completed', 'failed', 'cancelled', 'duplicate'].includes(tracker)) return false;
  return (issue?.execution_channel || issue?.workspace?.execution_channel) === 'bridge'
    && !hasAcceptedSubmission(issue)
    && (['in progress', 'running', 'processing'].includes(state) || ['claimed', 'running', 'retrying'].includes(tracker));
}

export function isUnresolvedSubmission(issue?: SymphonyLocalIssue | null): boolean {
  const workspace = issue?.workspace;
  return workspace?.recovery?.state === 'submission_unknown'
    && ![workspace, workspace.job_ref].some((ref) => String(ref?.job_id || ref?.process_id || '').trim());
}
