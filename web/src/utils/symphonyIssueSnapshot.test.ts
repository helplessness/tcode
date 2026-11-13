import { expect, it } from 'vitest';
import { issueListSignature } from './symphonyIssueSnapshot';
import type { SymphonyLocalIssue } from '../api/symphony';

it('refreshes cancellation details even when the outer issue is unchanged', () => {
  const issue: SymphonyLocalIssue = {
    id: 'test', state: 'Cancelled', updated_at: 'unchanged',
    workspace: { lifecycle_status: 'running', action_policy: { cancel: { available: true } } },
  };
  const before = issueListSignature([issue]);
  const after = issueListSignature([{
    ...issue,
    workspace: { lifecycle_status: 'cancelled', cancelled_at: 'now', action_policy: { cancel: { available: false } } },
  }]);
  expect(after).not.toBe(before);
  expect(issueListSignature([{ ...issue }])).toBe(before);
  expect(issueListSignature([{ ...issue, workspace: { ...issue.workspace, last_observed_at: 'later' } }])).toBe(before);
});

it('refreshes when a remote acceptance receipt lands without a state change', () => {
  const issue: SymphonyLocalIssue = { id: 'local', state: 'In Progress', updated_at: 'same', workspace: {} };
  expect(issueListSignature([{ ...issue, workspace: { job_ref: { job_id: 'remote-job' } } }])).not.toBe(issueListSignature([issue]));
});
