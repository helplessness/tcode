import { describe, expect, it } from 'vitest';
import type { SymphonyLocalIssue } from '../api/symphony';
import { hasAcceptedSubmission, isDispatchingSubmission, isUnresolvedSubmission } from './unresolvedSubmission';

describe('unresolved submission local deletion', () => {
  const issue = (workspace: Record<string, unknown>) => ({ workspace } as SymphonyLocalIssue);
  it('only offers local deletion for unresolved submissions without remote references', () => {
    expect(isUnresolvedSubmission(null)).toBe(false);
    expect(isUnresolvedSubmission(issue({}))).toBe(false);
    expect(isUnresolvedSubmission(issue({ recovery: { state: 'submission_unknown' } }))).toBe(true);
    for (const reference of [{ job_id: 'job-1' }, { process_id: 'process-1' }]) {
      expect(isUnresolvedSubmission(issue({ recovery: { state: 'submission_unknown' }, ...reference }))).toBe(false);
      expect(isUnresolvedSubmission(issue({ recovery: { state: 'submission_unknown' }, job_ref: reference }))).toBe(false);
    }
  });
});

it('distinguishes a local dispatch claim from an accepted remote job', () => {
  const dispatch: SymphonyLocalIssue = { id: 'local', state: 'In Progress', tracker_status: 'claimed', execution_channel: 'bridge' };
  expect(isDispatchingSubmission(dispatch)).toBe(true);
  expect(hasAcceptedSubmission(dispatch)).toBe(false);
  const accepted = { ...dispatch, state: 'queued', workspace: { job_ref: { job_id: 'remote-job' } } };
  expect(hasAcceptedSubmission(accepted)).toBe(true);
  expect(isDispatchingSubmission(accepted)).toBe(false);
  expect(isDispatchingSubmission({ ...dispatch, state: 'Failed' })).toBe(false);
  expect(isDispatchingSubmission({ ...dispatch, execution_channel: 'symphony' })).toBe(false);
});
