import type { Session } from '../stores/session';
import { latestDeliveredAssistant } from './verificationFeedback';

export type TaskLifecyclePhase =
  | 'clarifying'
  | 'planning'
  | 'awaiting_authorization'
  | 'executing'
  | 'awaiting_review'
  | 'revision_requested'
  | 'completed'
  | 'blocked'
  | 'failed'
  | 'cancelled';

export type TaskLifecycleInput = {
  session?: Pick<Session, 'messages' | 'task_runtime'> | null;
  isRunning?: boolean;
  hasQuestion?: boolean;
  hasPermissionRequest?: boolean;
};

const terminalOutcomes: Record<string, TaskLifecyclePhase> = {
  completed: 'completed',
  blocked: 'blocked',
  failed: 'failed',
  cancelled: 'cancelled',
  canceled: 'cancelled',
  interrupted: 'cancelled',
};

export function deriveTaskLifecyclePhase(input: TaskLifecycleInput): TaskLifecyclePhase {
  if (input.hasQuestion) return 'clarifying';
  if (input.hasPermissionRequest) return 'awaiting_authorization';
  const activeCounts = input.session?.task_runtime?.active_counts || {};
  if (input.isRunning) return 'executing';
  if (Number(activeCounts.waiting_user || 0) > 0) return 'clarifying';
  if (Number(activeCounts.waiting_review || 0) > 0) return 'awaiting_review';

  const messages = input.session?.messages || [];
  const latest = latestDeliveredAssistant(messages);
  const verification = latest?.verification;
  const lastAssistantIndex = messages.lastIndexOf(latest!);
  const hasNewRequest = messages.slice(lastAssistantIndex + 1).some(message => message.role === 'user' && message.presentation !== 'outcome_confirmation');
  if (verification?.advisory && !hasNewRequest) {
    if (verification.delivery_state === 'accepted') return 'completed';
    if (verification.delivery_state === 'modification_requested') return 'revision_requested';
  }
  if (Number(input.session?.task_runtime?.active_total || 0) > 0
    || Number(activeCounts.running || 0) > 0 || Number(activeCounts.waiting_external || 0) > 0) return 'executing';
  if (verification?.confirmation_state === 'modification_requested') return 'revision_requested';
  if (
    verification?.confirmation_state === 'pending'
    || (verification?.needs_user_confirmation && !verification.user_confirmed)
  ) return 'awaiting_review';

  const outcome = String(input.session?.task_runtime?.session_outcome_status || '').toLowerCase();
  // An explicit user acceptance is newer and more authoritative than a stale
  // session-level `partial` outcome left by the pre-acceptance result.
  if (verification?.confirmation_state === 'accepted' || verification?.user_confirmed) return 'completed';
  if (outcome === 'partial') return 'awaiting_review';
  if (terminalOutcomes[outcome]) return terminalOutcomes[outcome];
  return messages.length ? 'planning' : 'clarifying';
}

export const taskLifecycleLabels: Record<TaskLifecyclePhase, string> = {
  clarifying: '需求', planning: '计划', awaiting_authorization: '待授权', executing: '执行',
  awaiting_review: '待验收', revision_requested: '待修改', completed: '完成', blocked: '阻塞',
  failed: '失败', cancelled: '已取消',
};
