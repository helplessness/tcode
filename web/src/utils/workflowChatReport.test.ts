import { describe, expect, it } from 'vitest';
import type { WorkflowRun } from '../api/client';
import type { Message } from '../stores/session';
import { needsWorkflowReportRecovery, recoverWorkflowReport } from './workflowChatReport';

describe('workflow chat report recovery', () => {
  const report = '完整报告：' + '实验依据。'.repeat(1500) + '报告结尾。';
  const run = {
    run_id: 'wf-test',
    template_key: 'test',
    session_id: 'session-test',
    status: 'completed',
    current_stage_id: '',
    input_payload: {},
    stage_states: [],
    candidates: [],
    proposals: [],
    events: [],
    metadata: { final_report: report },
    version: 1,
  } satisfies WorkflowRun;
  const message = {
    id: 'workflow-status:wf-test:completed:none:0',
    role: 'assistant',
    content: `所有步骤都已完成。以下是最终结果：\n\n${report.slice(0, 5000)}\n\n...（内容已截断，完整结果见运行详情）`,
  } as Message;

  it('restores a previously truncated completed-run message from the durable report', () => {
    expect(needsWorkflowReportRecovery(message)).toBe(true);
    const recovered = recoverWorkflowReport(message, run);
    expect(recovered).toContain(report);
    expect(recovered).toContain('报告结尾。');
    expect(recovered).not.toContain('内容已截断');
  });

  it('does not change an unrelated message', () => {
    const unrelated = { ...message, id: 'normal-answer' };
    expect(recoverWorkflowReport(unrelated, run)).toBe(unrelated.content);
  });

  it('ignores a truncated answer projection and uses the durable stage response', () => {
    const durable = '最终完整报告结尾。';
    const withTruncatedProjection = {
      ...run,
      metadata: { answer_projection: { text: message.content } },
      stage_states: [{
        stage_id: 'final', label: '最终报告', owner: 'build', call_type: 'agent',
        status: 'committed', artifact_refs: {}, attempt: 1, draft_result: {},
        committed_result: { response: durable }, validation_report: {}, metadata: {},
      }],
    } satisfies WorkflowRun;
    expect(recoverWorkflowReport(message, withTruncatedProjection)).toContain(durable);
    expect(recoverWorkflowReport(message, withTruncatedProjection)).not.toContain('内容已截断');
  });
});
