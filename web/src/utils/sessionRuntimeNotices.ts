import type { SessionTaskRuntimeSummary, TaskRuntimeItem } from '../api/client';
import type { HumanReviewItem, SymphonyLocalIssue, WorkflowRun } from '../api/client';
import type { Session } from '../stores/session';
import { hasAcceptedSubmission, isDispatchingSubmission } from './unresolvedSubmission';

export type RuntimeNotice = {
  kind: 'review' | 'workflow' | 'async';
  page: 'review' | 'workflow' | 'runtime-workflow' | 'jobs';
  sessionId: string;
  runtimeId: string;
  title: string;
  externalId: string;
  reviewId?: string;
  revision?: string;
};

export function sessionWorkspaceId(session?: Pick<Session, 'workspace_id' | 'workdir_name' | 'workdir'> | null): string {
  const explicit = String(session?.workspace_id || session?.workdir_name || '').trim();
  if (explicit) return explicit;
  const workdir = String(session?.workdir || '').trim().replace(/\/+$/, '');
  return workdir ? workdir.split('/').pop() || '' : '';
}

export function activeRuntimeNotices(
  sessions: Session[], workspaceId: string,
  workflowRuns: WorkflowRun[] | null = null,
  reviews: HumanReviewItem[] | null = null,
  issues: SymphonyLocalIssue[] | null = null,
): RuntimeNotice[] {
  const notices: RuntimeNotice[] = [];
  const seen = new Set<string>();
  const issueIds = new Set((issues || []).map(issue => String(issue.identifier || issue.id)));
  const sessionIds = new Set(sessions.filter(session => {
    return !workspaceId || sessionWorkspaceId(session) === workspaceId;
  }).map(session => session.session_id));
  for (const session of sessions) {
    if (!sessionIds.has(session.session_id)) continue;
    for (const item of session.task_runtime?.items || []) {
      const status = String(item.status || '').toLowerCase();
      if (workflowRuns && item.runtime_kind === 'workflow_run') continue;
      if (reviews && item.runtime_kind === 'human_review') continue;
      if (issues && item.runtime_kind === 'mcp_job' && issueIds.has(String(item.external_id || ''))) continue;
      const kind = item.runtime_kind === 'human_review' && status === 'waiting_review'
        ? 'review'
        : item.runtime_kind === 'workflow_run' && status === 'waiting_review'
          ? 'review'
          : item.runtime_kind === 'workflow_run' && ['running', 'waiting_external'].includes(status)
            ? 'workflow'
            : item.runtime_kind === 'mcp_job' && ['running', 'waiting_external'].includes(status)
              ? 'async'
              : null;
      if (!kind || seen.has(item.runtime_id)) continue;
      seen.add(item.runtime_id);
      notices.push({
        kind,
        page: item.runtime_kind === 'workflow_run' ? 'workflow' : item.runtime_kind === 'human_review' ? 'review' : 'jobs',
        sessionId: session.session_id,
        runtimeId: item.runtime_id,
        title: taskRuntimeItemTitle(item),
        externalId: String(item.external_id || ''),
        reviewId: typeof item.metadata?.id === 'string' ? item.metadata.id : undefined,
      });
    }
  }
  for (const run of workflowRuns || []) {
    if (!sessionIds.has(run.session_id)) continue;
    const status = String(run.status || '').toLowerCase();
    if (['completed', 'failed', 'error', 'cancelled', 'canceled', 'terminated'].includes(status)) continue;
    const needsReview = status === 'review_pending' || (run.proposals || []).some(proposal => proposal.status === 'pending');
    const kind = needsReview ? 'review' : ['running', 'queued', 'waiting_external_job', 'collecting', 'validating'].includes(status) ? 'workflow' : null;
    if (!kind || seen.has(`workflow:${run.run_id}`)) continue;
    seen.add(`workflow:${run.run_id}`);
    const temporary = Boolean(run.metadata?.workflow_draft_id || run.metadata?.created_from === 'temporary_workflow_draft');
    notices.push({ kind, page: temporary ? 'runtime-workflow' : 'workflow', sessionId: run.session_id, runtimeId: `workflow:${run.run_id}`, title: String(run.metadata?.template_snapshot?.display_name || run.template_key || run.run_id), externalId: run.run_id, revision: String(run.version || run.updated_at || run.status) });
  }
  for (const review of reviews || []) {
    if (review.status !== 'pending_review') continue;
    const sessionId = String(review.review_data?.session_id || review.review_data?.parent_session_id || '');
    if (!sessionIds.has(sessionId)) continue;
    const id = String(review.identifier || review.id);
    if (seen.has(`review:${id}`)) continue;
    seen.add(`review:${id}`);
    notices.push({ kind: 'review', page: 'review', sessionId, runtimeId: `review:${id}`, title: `${review.tool_server}.${review.tool_name}`, externalId: id, reviewId: review.id });
  }
  for (const issue of issues || []) {
    const state = String(issue.state || '').toLowerCase();
    const tracker = String(issue.tracker_status || '').toLowerCase();
    if (['done', 'completed', 'closed', 'failed', 'error', 'cancelled', 'canceled', 'duplicate'].includes(state)
      || ['completed', 'failed', 'cancelled', 'duplicate'].includes(tracker)) continue;
    if (isDispatchingSubmission(issue)) continue;
    if (!hasAcceptedSubmission(issue) && !['in progress', 'running', 'processing'].includes(state) && !['claimed', 'running', 'retrying'].includes(tracker)) continue;
    const id = String(issue.identifier || issue.id);
    if (notices.some(notice => notice.page === 'jobs' && notice.externalId === id)) continue;
    const sessionId = String(issue.workspace?.session_id || issue.workspace?.parent_session_id || '');
    notices.push({ kind: 'async', page: 'jobs', sessionId: sessionIds.has(sessionId) ? sessionId : '', runtimeId: `issue:${id}`, title: issue.title || id, externalId: id });
  }
  return notices.sort((left, right) => ['review', 'workflow', 'async'].indexOf(left.kind) - ['review', 'workflow', 'async'].indexOf(right.kind));
}

export const TASK_RUNTIME_NOTICE_STATUSES = [
  'waiting_user',
  'waiting_review',
  'waiting_external',
  'blocked',
  'running',
] as const;

export type TaskRuntimeNoticeStatus = typeof TASK_RUNTIME_NOTICE_STATUSES[number];

const STATUS_LABELS: Record<TaskRuntimeNoticeStatus, string> = {
  waiting_user: '等待用户输入',
  waiting_review: '待审核',
  waiting_external: '等待外部任务',
  blocked: '已阻塞',
  running: '运行中',
};

const STATUS_BADGE_CLASSES: Record<TaskRuntimeNoticeStatus, string> = {
  waiting_user: 'border-[#4f8cff]/30 bg-[#4f8cff]/12 text-[#9bc0ff]',
  waiting_review: 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffc176]',
  waiting_external: 'border-[#f59e0b]/30 bg-[#f59e0b]/12 text-[#ffd17a]',
  blocked: 'border-[#e5534b]/30 bg-[#e5534b]/12 text-[#ff9c96]',
  running: 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]',
};

const SESSION_OUTCOME_BADGES: Record<string, { label: string; class: string }> = {
  completed: {
    label: '已完成',
    class: 'border-[#38b46a]/30 bg-[#38b46a]/10 text-[#9ee6bb]',
  },
  partial: {
    label: '待验收',
    class: 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffc176]',
  },
  blocked: {
    label: '已阻塞',
    class: 'border-[#e5534b]/30 bg-[#e5534b]/12 text-[#ff9c96]',
  },
  failed: {
    label: '执行失败',
    class: 'border-[#e5534b]/30 bg-[#e5534b]/12 text-[#ff9c96]',
  },
  interrupted: {
    label: '已中断',
    class: 'border-[#f1a02b]/30 bg-[#f1a02b]/12 text-[#ffc176]',
  },
  cancelled: {
    label: '已取消',
    class: 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]',
  },
};

export function taskRuntimeStatusLabel(status: string): string {
  const normalized = String(status || '').trim().toLowerCase() as TaskRuntimeNoticeStatus;
  return STATUS_LABELS[normalized] || normalized || '未知';
}

export function taskRuntimeStatusBadgeClass(status: string): string {
  const normalized = String(status || '').trim().toLowerCase() as TaskRuntimeNoticeStatus;
  return STATUS_BADGE_CLASSES[normalized]
    || 'border-[#3e484f] bg-[#171c20] text-[#9faeb7]';
}

export function taskRuntimeItemTitle(item: TaskRuntimeItem): string {
  return String(
    item.title
    || item.tool_name
    || item.external_id
    || item.runtime_id
    || '运行时条目',
  );
}

export function taskRuntimeNoticeGroups(summary?: SessionTaskRuntimeSummary | null) {
  const items = summary?.items || [];
  return TASK_RUNTIME_NOTICE_STATUSES.map(status => ({
    status,
    label: STATUS_LABELS[status],
    count: Number(summary?.active_counts?.[status] || 0),
    items: items.filter(item => String(item.status || '').toLowerCase() === status),
  }));
}

export function taskRuntimeSessionOutcomeBadge(
  summary?: SessionTaskRuntimeSummary | null,
) {
  const outcomeStatus = String(summary?.session_outcome_status || '').trim().toLowerCase();
  if (!outcomeStatus) return null;
  const badge = SESSION_OUTCOME_BADGES[outcomeStatus];
  return badge ? { status: outcomeStatus, ...badge } : null;
}
