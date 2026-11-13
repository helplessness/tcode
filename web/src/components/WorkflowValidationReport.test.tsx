import { renderToString } from 'solid-js/web';
import { describe, expect, it } from 'vitest';

import type { WorkflowRun } from '../api/client';
import { WorkflowBlockedValidation } from './WorkflowValidationReport';

function blockedRun(): WorkflowRun {
  return {
    run_id: 'run-blocked-1',
    template_key: 'validation-test',
    session_id: 'session-1',
    status: 'blocked',
    current_stage_id: 'review-output',
    input_payload: {},
    candidates: [],
    proposals: [],
    events: [],
    metadata: {},
    version: 1,
    stage_states: [
      {
        stage_id: 'review-output',
        label: '结果审核',
        owner: 'reviewer',
        call_type: 'agent',
        status: 'failed',
        artifact_refs: {},
        attempt: 1,
        draft_result: {},
        committed_result: {},
        metadata: {},
        validation_report: {
          ok: false,
          phase: 'output',
          summary: 'stage output rejected',
          checks: [
            {
              validator: 'required_artifacts',
              ok: false,
              code: 'missing_artifact',
              message: 'Missing artifacts: evidence_summary',
              evidence: { missing_artifacts: ['evidence_summary'] },
              retryable: true,
            },
            {
              validator: 'review_decision',
              ok: false,
              code: 'missing_review_decision',
              message: 'Review gate must return PASS, REVISE, or ESCALATE.',
              evidence: { decision: '' },
              retryable: true,
            },
          ],
        },
      },
    ],
  };
}

describe('workflow blocked validation UI', () => {
  it('renders validation_report failures, evidence, codes and next-step hints', () => {
    const html = renderToString(() => <WorkflowBlockedValidation run={blockedRun()} />);

    expect(html).toContain('校验阻塞详情');
    expect(html).toContain('required_artifacts');
    expect(html).toContain('missing_artifact');
    expect(html).toContain('evidence_summary');
    expect(html).toContain('review_decision');
    expect(html).toContain('missing_review_decision');
    expect(html).toContain('Review gate 原因');
    expect(html).toContain('下一步');
    expect(html).toContain('PASS、REVISE 或 ESCALATE');
  });

  it('does not render the validation blocker for a non-blocked run', () => {
    const run = { ...blockedRun(), status: 'running' };
    const html = renderToString(() => <WorkflowBlockedValidation run={run} />);

    expect(html).not.toContain('校验阻塞详情');
    expect(html).not.toContain('missing_artifact');
  });
});
