import type { WorkflowRun } from '../api/client';
import type { Message } from '../stores/session';
import { workflowFinalReport } from '../components/WorkflowWorkspacePanel.helpers';

const LEGACY_TRUNCATION_MARKER = '...（内容已截断，完整结果见运行详情）';

export function completedWorkflowRunId(message: Message): string {
  if (message.role !== 'assistant') return '';
  return /^workflow-status:([^:]+):completed:[^:]+:\d+$/.exec(message.id || '')?.[1] || '';
}

export function needsWorkflowReportRecovery(message: Message): boolean {
  return Boolean(completedWorkflowRunId(message)
    && String(message.content || '').includes(LEGACY_TRUNCATION_MARKER));
}

export function recoverWorkflowReport(message: Message, run: WorkflowRun): string {
  if (!needsWorkflowReportRecovery(message) || run.run_id !== completedWorkflowRunId(message)) {
    return message.content;
  }
  const composed = String(run.metadata?.answer_projection?.text || '').trim();
  if (composed && !composed.includes(LEGACY_TRUNCATION_MARKER)) return composed;

  const report = workflowFinalReport(run);
  if (!report) return message.content;
  return report;
}
